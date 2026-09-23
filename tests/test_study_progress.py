"""Phase 2A API contracts, exact-card facts, and synchronization semantics."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_user
from app.db.database import Base, get_db
from app.models.card import Card
from app.models.deck import Deck
from app.models.study_plan import StudyPlan
from app.models.study_progress import CardProgress, StudyProgressReceipt
from app.models.user import User
from app.routers.cards import router as cards_router
from app.routers.decks import router as decks_router
from app.routers.study_progress import router
from app.schemas.study_plan import StudyPlanCreate
from app.services.exam import ExamService
from app.services.study_plan import create_plan, plan_response


class StudyProgressAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.user = User(name="Learner", email="learner@example.com", password_hash="unused")
        self.other = User(name="Other", email="other@example.com", password_hash="unused")
        self.db.add_all([self.user, self.other])
        self.db.flush()
        self.parent = self.make_deck("Parent")
        self.chapter_a = self.make_deck("Z chapter", self.parent.id, position=0)
        self.chapter_b = self.make_deck("A chapter", self.parent.id, position=1)
        self.standalone = self.make_deck("Standalone")
        self.foreign_deck = self.make_deck("Foreign", user_id=self.other.id)
        self.cards = {deck.id: self.make_cards(deck, 3) for deck in
                      (self.chapter_a, self.chapter_b, self.standalone, self.foreign_deck)}
        self.db.commit()
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.include_router(cards_router)
        self.app.include_router(decks_router)
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)
        self.completed_at = datetime.now(timezone.utc).isoformat()

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def make_deck(self, title, parent_id=None, position=0, user_id=None):
        deck = Deck(user_id=user_id or self.user.id, parent_deck_id=parent_id, title=title,
                    position=position, subject="Science", education_level="School")
        self.db.add(deck)
        self.db.flush()
        return deck

    def make_cards(self, deck, count):
        cards = [Card(deck_id=deck.id, front=f"Question {i}", back="Answer") for i in range(count)]
        self.db.add_all(cards)
        self.db.flush()
        return cards

    def payload(self, *decks):
        return dict(session_id=str(uuid.uuid4()), completed_at=self.completed_at, decks=[dict(
            deck_id=str(deck.id), progress_epoch=str(deck.progress_epoch),
            learned_cards=[dict(card_id=str(self.cards[deck.id][0].id), phase="review", answer="got_it")],
        ) for deck in (decks or (self.chapter_a,))])

    def submit(self, payload=None):
        return self.client.post("/study/progress/submissions", json=payload or self.payload())

    def snapshot(self, deck=None):
        return self.client.get(f"/decks/{(deck or self.parent).id}/study-progress")

    def reset_request(self, deck=None):
        return dict(reset_id=str(uuid.uuid4()), expected_decks=[dict(
            deck_id=item["deck_id"], progress_epoch=item["progress_epoch"],
        ) for item in self.snapshot(deck).json()["decks"]])

    def reset(self, deck=None, request=None):
        deck = deck or self.parent
        return self.client.post(f"/decks/{deck.id}/study-progress/reset", json=request or self.reset_request(deck))

    def learned_count(self):
        return self.db.scalar(select(func.count()).select_from(CardProgress).where(CardProgress.learned_at.is_not(None)))

    def test_review_got_it_learns_exact_card_and_exposes_fact(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["decks"][0]["accepted_card_ids"], [str(self.cards[self.chapter_a.id][0].id)])
        snapshot = self.snapshot().json()
        self.assertEqual([deck["deck_id"] for deck in snapshot["decks"]], [str(self.chapter_a.id), str(self.chapter_b.id)])
        facts = [card for deck in snapshot["decks"] for card in deck["cards"]]
        self.assertEqual(sum(card["learned_at"] is not None for card in facts), 1)
        self.assertFalse(snapshot["decks"][0]["completed"])
        self.assertEqual(self.learned_count(), 1)

    def test_new_and_again_are_rejected_and_cannot_clear_learning(self):
        for phase, answer in [("new", "got_it"), ("new", "again"), ("review", "again")]:
            payload = self.payload()
            payload["decks"][0]["learned_cards"][0].update(phase=phase, answer=answer)
            self.assertEqual(self.submit(payload).status_code, 422)
        self.assertEqual(self.learned_count(), 0)
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit(payload).status_code, 422)
        self.assertEqual(self.learned_count(), 1)

    def test_already_learned_card_is_monotonic_across_sessions(self):
        original = self.submit().json()
        before = self.snapshot().json()
        payload = self.payload()
        payload["completed_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        response = self.submit(payload).json()
        self.assertEqual(response["decks"][0]["accepted_card_ids"], [])
        self.assertEqual(response["decks"][0]["already_learned_card_ids"], original["decks"][0]["accepted_card_ids"])
        self.assertEqual(self.snapshot().json(), before)

    def test_duplicate_session_and_card_ids_are_normalized(self):
        payload = self.payload(self.chapter_a, self.chapter_b)
        original = self.submit(payload)
        self.assertEqual(original.status_code, 200, original.text)
        payload["decks"].reverse()
        payload["decks"][0]["learned_cards"] *= 2
        payload["completed_at"] = datetime.fromisoformat(self.completed_at).astimezone(timezone(timedelta(hours=7))).isoformat()
        self.assertEqual(self.submit(payload).json(), original.json())
        self.assertEqual(self.learned_count(), 2)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 1)

    def test_session_reuse_with_different_content_conflicts(self):
        payload = self.payload()
        self.submit(payload)
        payload["decks"][0]["learned_cards"][0]["card_id"] = str(self.cards[self.chapter_a.id][1].id)
        response = self.submit(payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "idempotency_conflict")
        self.assertEqual(self.learned_count(), 1)

    def test_foreign_deck_and_foreign_card_rejected_atomically(self):
        self.assertEqual(self.submit(self.payload(self.foreign_deck)).status_code, 404)
        payload = self.payload(self.chapter_a, self.chapter_b)
        payload["decks"][1]["learned_cards"][0]["card_id"] = str(self.cards[self.foreign_deck.id][0].id)
        self.assertEqual(self.submit(payload).status_code, 422)
        self.assertEqual(self.learned_count(), 0)
        self.assertEqual(self.snapshot(self.foreign_deck).status_code, 404)
        reset_request = dict(reset_id=str(uuid.uuid4()), expected_decks=[dict(
            deck_id=str(self.foreign_deck.id), progress_epoch=str(self.foreign_deck.progress_epoch))])
        self.assertEqual(self.reset(self.foreign_deck, reset_request).status_code, 404)

    def test_same_user_wrong_chapter_and_duplicate_cross_deck_card_rejected(self):
        payload = self.payload(self.chapter_a, self.chapter_b)
        payload["decks"][1]["learned_cards"][0]["card_id"] = payload["decks"][0]["learned_cards"][0]["card_id"]
        self.assertEqual(self.submit(payload).status_code, 422)
        self.assertEqual(self.learned_count(), 0)

    def test_deleted_card_rejected_but_receipt_survives(self):
        payload = self.payload()
        receipt = self.submit(payload).json()
        card_id = payload["decks"][0]["learned_cards"][0]["card_id"]
        self.assertEqual(self.client.delete(f"/cards/{card_id}").status_code, 204)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(CardProgress)), 0)
        self.assertEqual(self.submit(payload).json(), receipt)
        payload["session_id"] = str(uuid.uuid4())
        self.assertEqual(self.submit(payload).status_code, 422)

    def test_added_card_is_unlearned_and_bulk_deletion_removes_current_fact(self):
        self.submit()
        response = self.client.post(f"/decks/{self.chapter_a.id}/cards", json={"front": "New", "back": "Answer"})
        self.assertEqual(response.status_code, 201, response.text)
        facts = {card["card_id"]: card["learned_at"] for card in self.snapshot(self.chapter_a).json()["decks"][0]["cards"]}
        self.assertIsNone(facts[response.json()["id"]])
        self.assertEqual(self.client.post(f"/decks/{self.chapter_a.id}/cards/bulk", json={"cards": []}).status_code, 200)
        self.assertEqual(self.snapshot(self.chapter_a).json()["decks"][0]["cards"], [])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(CardProgress)), 0)

    def test_chapter_reset_only_affects_selected_chapter(self):
        self.submit(self.payload(self.chapter_a, self.chapter_b))
        epoch_b = self.chapter_b.progress_epoch
        response = self.reset(self.chapter_a)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["decks"][0]["cleared_card_count"], 1)
        self.assertEqual(self.learned_count(), 1)
        self.assertEqual(self.chapter_b.progress_epoch, epoch_b)

    def test_parent_and_standalone_reset_scopes(self):
        self.submit(self.payload(self.chapter_a, self.chapter_b, self.standalone))
        response = self.reset()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({item["deck_id"] for item in response.json()["decks"]}, {str(self.chapter_a.id), str(self.chapter_b.id)})
        self.assertEqual(self.learned_count(), 1)
        self.assertEqual(self.reset(self.standalone).status_code, 200)
        self.assertEqual(self.learned_count(), 0)

    def test_stale_offline_submission_cannot_restore_after_reset(self):
        offline = self.payload(self.chapter_a, self.chapter_b)
        self.reset(self.chapter_a)
        response = self.submit(offline)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "stale_progress_epoch")
        self.assertEqual(self.learned_count(), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 1)

    def test_replayed_pre_reset_submission_is_only_historical_acknowledgement(self):
        payload = self.payload()
        original = self.submit(payload).json()
        self.reset(self.chapter_a)
        self.assertEqual(self.submit(payload).json(), original)
        self.assertEqual(self.learned_count(), 0)

    def test_duplicate_reset_does_not_clear_subsequent_learning(self):
        request = self.reset_request(self.chapter_a)
        original = self.reset(self.chapter_a, request).json()
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.reset(self.chapter_a, request).json(), original)
        self.assertEqual(self.learned_count(), 1)
        request["reset_id"] = str(uuid.uuid4())
        self.assertEqual(self.reset(self.chapter_a, request).status_code, 409)
        self.assertEqual(self.learned_count(), 1)

    def test_reset_scope_change_requires_fresh_confirmation(self):
        request = self.reset_request()
        self.make_deck("Added chapter", self.parent.id)
        self.db.commit()
        response = self.reset(request=request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "progress_scope_changed")

    def test_reusing_operation_uuid_for_different_kind_conflicts(self):
        payload = self.payload()
        self.submit(payload)
        reset = self.reset_request(self.chapter_a)
        reset["reset_id"] = payload["session_id"]
        self.assertEqual(self.reset(self.chapter_a, reset).status_code, 409)

    def test_submission_receipt_failure_rolls_back_learning(self):
        with patch("app.services.study_progress._remember", side_effect=RuntimeError("Receipt unavailable")):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertEqual(self.learned_count(), 0)

    def test_reset_receipt_failure_rolls_back_epoch_and_learning(self):
        self.submit()
        before = self.snapshot().json()
        with patch("app.services.study_progress._remember", side_effect=RuntimeError("Receipt unavailable")):
            with self.assertRaises(RuntimeError):
                self.reset()
        self.assertEqual(self.snapshot().json(), before)

    def test_invalid_timestamp_and_extra_client_aggregates_rejected(self):
        for timestamp in ("2026-09-24T01:00:00", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()):
            payload = self.payload()
            payload["completed_at"] = timestamp
            self.assertEqual(self.submit(payload).status_code, 422)
        payload = self.payload()
        payload["correctCount"] = 100
        self.assertEqual(self.submit(payload).status_code, 422)
        self.assertEqual(self.learned_count(), 0)

    def test_no_authentication_no_progress_access(self):
        del self.app.dependency_overrides[get_current_user]
        self.assertIn(self.submit().status_code, {401, 403})
        self.assertIn(self.snapshot().status_code, {401, 403})

    def test_pending_generation_does_not_invent_learning(self):
        self.chapter_a.generation_status = "generating"
        self.db.commit()
        facts = self.snapshot(self.chapter_a).json()["decks"][0]
        self.assertEqual(facts["generation_status"], "generating")
        self.assertTrue(all(card["learned_at"] is None for card in facts["cards"]))

    def test_phase_2a_does_not_mutate_timeline_or_exam_status(self):
        plan = create_plan(self.db, self.parent.id, self.user.id, StudyPlanCreate(timezone="Asia/Jakarta"))
        self.db.commit()
        before_plan = plan_response(self.db, plan).model_dump(mode="json")
        before_exams = ExamService().get_status(self.parent.id, self.db, self.user)
        self.submit(self.payload(self.chapter_a, self.chapter_b))
        self.reset()
        self.assertEqual(plan_response(self.db, plan).model_dump(mode="json"), before_plan)
        self.assertEqual(ExamService().get_status(self.parent.id, self.db, self.user), before_exams)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 1)

    def test_recreated_deck_id_cannot_accept_old_epoch(self):
        offline = self.payload(self.standalone)
        deck_id = self.standalone.id
        card_id = self.cards[deck_id][0].id
        self.assertEqual(self.client.delete(f"/decks/{deck_id}").status_code, 204)
        self.db.add(Deck(id=deck_id, user_id=self.user.id, title="Recreated", subject="Science", education_level="School"))
        self.db.flush()
        self.db.add(Card(id=card_id, deck_id=deck_id, front="Restored", back="Answer"))
        self.db.commit()
        self.assertEqual(self.submit(offline).status_code, 409)
        self.assertEqual(self.learned_count(), 0)

    def test_operation_ids_are_scoped_to_authenticated_user(self):
        payload = self.payload()
        self.assertEqual(self.submit(payload).status_code, 200)
        other_payload = self.payload(self.foreign_deck)
        other_payload["session_id"] = payload["session_id"]
        self.user = self.other
        self.assertEqual(self.submit(other_payload).status_code, 200)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 2)

    def test_duplicate_decks_and_missing_phase_rejected(self):
        payload = self.payload()
        payload["decks"] *= 2
        self.assertEqual(self.submit(payload).status_code, 422)
        payload = self.payload()
        del payload["decks"][0]["learned_cards"][0]["phase"]
        self.assertEqual(self.submit(payload).status_code, 422)


if __name__ == "__main__":
    unittest.main()
