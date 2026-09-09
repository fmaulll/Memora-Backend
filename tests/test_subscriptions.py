"""Offline API tests; Apple network is mocked, never the entitlement decisions."""
import unittest
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.Type import Type
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

from app.core.auth import get_current_user
from app.core.security import create_refresh_token, hash_password
from app.db.database import Base, get_db
from app.models.user import User
from app.models.deck import Deck
from app.models.generation_job import GenerationJob
from app.models.subscription import Subscription, AppleNotification, AIGenerationRequest
from app.routers.auth import router as auth_router
from app.routers.ai import router as ai_router
from app.routers.decks import router as deck_router
from app.routers.exams import router as exam_router
from app.routers.subscriptions import router as subscription_router
from app.schemas.ai import GenerateDeckRequest
from app.schemas.auth import RegisterRequest, LoginRequest
from app.services.accounts import upgrade_account, merge_account
from app.services.apple import AppleGateway, AppleSnapshot, utcnow
from app.services.subscriptions import (apply_snapshot, entitlement, process_notification, reconcile_user,
                                       require_paid, token_owner, verify_purchase)


def snapshot(user, status="active", days=30, auto_renew=1, original_id="original-1", revoked=False):
    now = utcnow()
    tx = SimpleNamespace(
        originalTransactionId=original_id, appAccountToken=str(user.app_account_token),
        productId="nudge.monthly", environment=Environment.SANDBOX,
        expiresDate=int((now + timedelta(days=days)).timestamp() * 1000),
        revocationDate=int(now.timestamp() * 1000) if revoked else None,
        bundleId="app.nudge", type=Type.AUTO_RENEWABLE_SUBSCRIPTION,
    )
    renewal = SimpleNamespace(
        autoRenewStatus=auto_renew, gracePeriodExpiresDate=int((now + timedelta(days=3)).timestamp() * 1000)
        if status == "grace_period" else None,
    )
    return AppleSnapshot(tx, renewal, status, now)


def generation_payload():
    return dict(plan=dict(title="Cells", subject="Biology", education_level="High school",
        learning_language="English", chapters=[dict(title="Chapter 1", description="Cells",
        key_concepts=["Cells"], card_count=2)]), study_purpose="general")


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(name="Guest", email="guest@example.com", password_hash=hash_password("password1"), is_anonymous=True)
        self.db.add(self.user)
        self.db.commit()
        self.app = FastAPI()
        for router in (auth_router, subscription_router, ai_router, deck_router, exam_router):
            self.app.include_router(router)
        def session():
            try:
                yield self.db
            finally:
                self.db.rollback()
        self.app.dependency_overrides[get_db] = session
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def save(self, **kwargs):
        value = snapshot(self.user, **kwargs)
        sub = apply_snapshot(self.db, self.user, value)
        self.db.commit()
        return sub

    def test_renewal_and_cancellation_keep_paid_period(self):
        sub = self.save(days=1)
        self.save(days=30, auto_renew=0)
        result = entitlement(self.db, self.user.id)
        self.assertTrue(result.is_subscribed)
        self.assertFalse(result.auto_renew)
        self.assertGreater(result.expires_at, utcnow() + timedelta(days=29))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Subscription)), 1)

    def test_expiry_grace_billing_retry_refund(self):
        for kwargs, access, status in (
            ({"days": -1}, False, "expired"),
            ({"status": "grace_period", "days": -1}, True, "grace_period"),
            ({"status": "billing_retry", "days": -1}, False, "billing_retry"),
            ({"revoked": True}, False, "revoked"),
        ):
            with self.subTest(kwargs=kwargs):
                self.save(**kwargs)
                result = entitlement(self.db, self.user.id)
                self.assertEqual(result.is_subscribed, access)
                self.assertEqual(result.status, status)

    def test_expired_grace_period_denies(self):
        sub = self.save(status="grace_period", days=-1)
        sub.grace_period_expires_at = utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.assertFalse(entitlement(self.db, self.user.id).is_subscribed)

    def test_stale_state_fails_closed_and_reconciles(self):
        sub = self.save()
        sub.last_verified_at = utcnow() - timedelta(hours=2)
        self.db.commit()
        self.assertEqual(entitlement(self.db, self.user.id).status, "verification_required")
        gateway = Mock()
        gateway.snapshot.return_value = snapshot(self.user)
        reconcile_user(self.db, self.user, gateway)
        self.db.commit()
        self.assertTrue(entitlement(self.db, self.user.id).is_subscribed)

    def test_invalid_signature_uses_real_apple_verifier(self):
        gateway = AppleGateway.__new__(AppleGateway)
        gateway.verifier = SignedDataVerifier([], True, Environment.SANDBOX, "app.nudge", None)
        for method in (gateway.transaction, gateway.notification):
            with self.assertRaises(HTTPException) as error:
                method("not.a.valid-jws")
            self.assertEqual(error.exception.detail["code"], "invalid_apple_signature")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Subscription)), 0)

    def test_identity_environment_product_and_type_rejected(self):
        gateway = AppleGateway.__new__(AppleGateway)
        gateway.environment = Environment.SANDBOX
        gateway.config = SimpleNamespace(apple_bundle_id="app.nudge", apple_product_ids=["nudge.monthly"])
        gateway.verifier = Mock()
        for field, value in (("bundleId", "other"), ("environment", Environment.PRODUCTION),
                             ("productId", "other"), ("type", Type.CONSUMABLE)):
            with self.subTest(field=field):
                tx = snapshot(self.user).transaction
                setattr(tx, field, value)
                gateway.verifier.verify_and_decode_signed_transaction.return_value = tx
                with self.assertRaises(HTTPException):
                    gateway.transaction("signed")

    def test_client_flag_cannot_grant_access(self):
        response = self.client.post("/subscriptions/apple/verify", json={"is_subscribed": True})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.client.get("/subscriptions/me").json()["entitlement"]["is_subscribed"])

    def test_ownership_conflicts(self):
        self.save()
        other = User(name="Other", email="other@example.com", password_hash="unused")
        self.db.add(other)
        self.db.commit()
        gateway = Mock()
        gateway.transaction.return_value = snapshot(self.user).transaction
        with self.assertRaises(HTTPException) as error:
            verify_purchase(self.db, other, "signed", gateway)
        self.assertEqual(error.exception.detail["code"], "subscription_owned_by_another_account")
        gateway.snapshot.assert_not_called()

    def test_unowned_transaction_requires_matching_token(self):
        gateway = Mock()
        tx = snapshot(self.user).transaction
        tx.appAccountToken = None
        gateway.transaction.return_value = tx
        with self.assertRaises(HTTPException) as error:
            verify_purchase(self.db, self.user, "signed", gateway)
        self.assertEqual(error.exception.detail["code"], "app_account_token_mismatch")

    def test_verify_uses_current_status_not_client_transaction(self):
        gateway = Mock()
        gateway.transaction.return_value = snapshot(self.user).transaction
        gateway.snapshot.return_value = snapshot(self.user, revoked=True)
        response = verify_purchase(self.db, self.user, "signed", gateway)
        self.assertFalse(response.entitlement.is_subscribed)
        self.assertEqual(response.entitlement.status, "revoked")

    def test_duplicate_and_out_of_order_notifications(self):
        self.save()
        gateway = Mock()
        notification = SimpleNamespace(notificationUUID="event-1", signedDate=200,
            notificationType="DID_RENEW", data=SimpleNamespace(signedTransactionInfo="tx", signedRenewalInfo="renew"))
        gateway.notification.return_value = notification
        gateway.transaction.return_value = snapshot(self.user).transaction
        gateway.snapshot.return_value = snapshot(self.user, days=40)
        self.assertEqual(process_notification(self.db, "signed", gateway)["status"], "processed")
        self.assertEqual(process_notification(self.db, "signed", gateway)["status"], "duplicate")
        self.assertEqual(gateway.snapshot.call_count, 1)
        notification.notificationUUID = "old-expiration"
        notification.signedDate = 100
        notification.notificationType = "EXPIRED"
        process_notification(self.db, "signed", gateway)
        self.assertTrue(entitlement(self.db, self.user.id).is_subscribed)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AppleNotification)), 2)

    def test_notification_outage_not_acknowledged_or_persisted(self):
        gateway = Mock()
        gateway.notification.return_value = SimpleNamespace(notificationUUID="failed-event", signedDate=1,
            notificationType="DID_RENEW", data=SimpleNamespace(signedTransactionInfo="tx", signedRenewalInfo=None))
        gateway.transaction.return_value = snapshot(self.user).transaction
        gateway.snapshot.side_effect = HTTPException(503, "unavailable")
        with self.assertRaises(HTTPException):
            process_notification(self.db, "signed", gateway)
        self.db.rollback()
        self.assertIsNone(self.db.get(AppleNotification, "failed-event"))

    def test_upgrade_preserves_token_subscription_and_usage(self):
        self.save()
        user_id, token = self.user.id, self.user.app_account_token
        self.user.free_ai_deck_used = True
        self.db.commit()
        result = upgrade_account(self.db, self.user, RegisterRequest(name="Person", email="person@example.com", password="password2"))
        self.assertEqual((result.id, result.app_account_token), (user_id, token))
        self.assertFalse(result.is_anonymous)
        self.assertTrue(result.free_ai_deck_used)
        self.assertTrue(entitlement(self.db, result.id).is_subscribed)

    def test_explicit_merge_preserves_ownership_and_history(self):
        self.save()
        deck = self.client.post("/ai/decks/generate", json=generation_payload(), headers={"Idempotency-Key": str(uuid.uuid4())})
        self.assertEqual(deck.status_code, 200, deck.text)
        self.user.free_ai_deck_used = True
        target = User(name="Person", email="person@example.com", password_hash=hash_password("password2"))
        self.db.add(target)
        self.db.commit()
        old_id, old_token = self.user.id, self.user.app_account_token
        merged = merge_account(self.db, self.user, LoginRequest(email=target.email, password="password2"))
        self.assertIsNone(self.db.get(User, old_id))
        self.assertEqual(token_owner(self.db, old_token), merged.id)
        self.assertTrue(merged.free_ai_deck_used)
        self.assertTrue(entitlement(self.db, merged.id).is_subscribed)
        self.assertEqual(self.db.scalar(select(AIGenerationRequest)).user_id, merged.id)
        gateway = Mock()
        tx = snapshot(merged)
        tx.transaction.appAccountToken = str(old_token)
        gateway.transaction.return_value = tx.transaction
        gateway.snapshot.return_value = tx
        self.assertTrue(verify_purchase(self.db, merged, "signed", gateway).entitlement.is_subscribed)

    def test_login_does_not_merge_and_upgrade_conflict_is_clear(self):
        other = User(name="Other", email="other@example.com", password_hash=hash_password("password2"))
        self.db.add(other)
        self.db.commit()
        self.save()
        response = self.client.post("/auth/login", json={"email": other.email, "password": "password2"})
        self.assertFalse(response.json()["user"]["entitlement"]["is_subscribed"])
        response = self.client.post("/auth/upgrade", json={"name": "Other", "email": other.email, "password": "password2"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "email_already_registered")

    def test_free_deck_idempotency_and_failure_retains_allowance(self):
        key = {"Idempotency-Key": str(uuid.uuid4())}
        first = self.client.post("/ai/decks/generate", json=generation_payload(), headers=key)
        self.assertEqual(first.status_code, 200, first.text)
        repeated = self.client.post("/ai/decks/generate", json=generation_payload(), headers=key)
        self.assertEqual(repeated.json(), first.json())
        denied = self.client.post("/ai/decks/generate", json=generation_payload(), headers={"Idempotency-Key": str(uuid.uuid4())})
        self.assertEqual(denied.status_code, 402)
        job = self.db.scalar(select(GenerationJob))
        job.status = "failed"
        self.db.commit()
        retried = self.client.post(f"/ai/decks/{first.json()['deck']['id']}/retry")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertTrue(self.db.get(User, self.user.id).free_ai_deck_used)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(GenerationJob)), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AIGenerationRequest)), 1)

    def test_idempotency_body_conflict_and_missing_key(self):
        payload = generation_payload()
        self.assertEqual(self.client.post("/ai/decks/generate", json=payload).status_code, 422)
        key = {"Idempotency-Key": str(uuid.uuid4())}
        self.client.post("/ai/decks/generate", json=payload, headers=key)
        payload["plan"]["title"] = "Another title"
        self.assertEqual(self.client.post("/ai/decks/generate", json=payload, headers=key).status_code, 409)

    def test_rollback_releases_uncommitted_allowance(self):
        with patch("app.routers.ai.StudyTimelineService.generate", side_effect=RuntimeError("failure before admission")):
            with self.assertRaises(RuntimeError):
                self.client.post("/ai/decks/generate", json=generation_payload(), headers={"Idempotency-Key": str(uuid.uuid4())})
        self.assertFalse(self.db.get(User, self.user.id).free_ai_deck_used)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Deck)), 0)

    def test_refresh_token_rejected_as_access_token(self):
        del self.app.dependency_overrides[get_current_user]
        result = self.client.get("/auth/me", headers={"Authorization": "Bearer " + create_refresh_token(str(self.user.id))})
        self.assertEqual(result.status_code, 401)

    def test_generated_metadata_cannot_expand_free_retry(self):
        response = self.client.post("/ai/decks/generate", json=generation_payload(), headers={"Idempotency-Key": str(uuid.uuid4())})
        chapter_id = response.json()["deck"]["chapters"][0]["id"]
        edited = self.client.put(f"/decks/{chapter_id}", json={"card_count": 100})
        self.assertEqual(edited.status_code, 409)
        self.assertEqual(edited.json()["detail"]["code"], "generated_deck_structure_locked")
        edited = self.client.put(f"/decks/{chapter_id}", json={"title": "New title"})
        self.assertEqual(edited.status_code, 200)

    def test_deleted_deck_does_not_restore_allowance(self):
        key = {"Idempotency-Key": str(uuid.uuid4())}
        response = self.client.post("/ai/decks/generate", json=generation_payload(), headers=key)
        deck_id = response.json()["deck"]["id"]
        self.assertEqual(self.client.delete(f"/decks/{deck_id}").status_code, 204)
        repeated = self.client.post("/ai/decks/generate", json=generation_payload(), headers=key)
        self.assertEqual(repeated.status_code, 410)
        self.assertTrue(self.db.get(User, self.user.id).free_ai_deck_used)

    def test_paid_exam_generation_is_enforced_at_endpoint(self):
        response = self.client.post("/ai/decks/generate", json=generation_payload(), headers={"Idempotency-Key": str(uuid.uuid4())})
        deck_id = response.json()["deck"]["id"]
        response = self.client.post(f"/decks/{deck_id}/exams/first_half/generate")
        self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(response.json()["detail"]["code"], "subscription_required")

    def test_generation_limits_reject_before_consumption(self):
        payload = generation_payload()
        payload["plan"]["chapters"][0]["card_count"] = 101
        response = self.client.post("/ai/decks/generate", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.db.get(User, self.user.id).free_ai_deck_used)

    def test_auth_contract_and_paid_guard(self):
        response = self.client.get("/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("entitlement", response.json())
        self.assertEqual(response.json()["app_account_token"], str(self.user.app_account_token))
        with self.assertRaises(HTTPException) as error:
            require_paid(self.db, self.user)
        self.assertEqual(error.exception.status_code, 402)
        self.user.free_ai_deck_used = True
        self.db.commit()
        self.assertEqual(self.client.post("/ai/decks/plan", json={"topic": "Cells", "education_level": "School",
            "study_purpose": "general", "learning_language": "English"}).status_code, 402)


if __name__ == "__main__":
    unittest.main()
