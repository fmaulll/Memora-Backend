"""Apple is the only authority for subscription state. No unsigned JWS decoding."""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from appstoreserverlibrary.api_client import AppStoreServerAPIClient, APIException
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.Type import Type
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier, VerificationException, VerificationStatus
from requests import RequestException
from fastapi import HTTPException

from app.db.database import settings


def fail(status, code, message):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def milliseconds(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc) if value is not None else None


@dataclass
class AppleSnapshot:
    transaction: object
    renewal: object
    status: str
    started_at: datetime


class AppleGateway:
    def __init__(self, config=settings):
        self.config = config
        environments = {"sandbox": Environment.SANDBOX, "production": Environment.PRODUCTION}
        if (config.apple_environment not in environments or not config.apple_bundle_id
                or not config.apple_product_ids or not config.apple_root_certificate_paths
                or not config.apple_private_key_path or not config.apple_key_id
                or not config.apple_issuer_id
                or (config.apple_environment == "production" and not config.apple_app_id)):
            fail(503, "apple_not_configured", "Apple subscription verification is not configured.")
        self.environment = environments[config.apple_environment]
        try:
            roots = [Path(path).read_bytes() for path in config.apple_root_certificate_paths]
            key = Path(config.apple_private_key_path).read_bytes()
            self.verifier = SignedDataVerifier(
                roots, True, self.environment, config.apple_bundle_id, config.apple_app_id,
            )
            self.client = AppStoreServerAPIClient(
                key, config.apple_key_id, config.apple_issuer_id,
                config.apple_bundle_id, self.environment,
            )
        except (OSError, ValueError):
            fail(503, "apple_not_configured", "Apple verification configuration is invalid.")

    def _verify(self, method, signed):
        try:
            return method(signed)
        except VerificationException as error:
            if error.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
                fail(503, "apple_unavailable", "Apple certificate verification is temporarily unavailable.")
            fail(400, "invalid_apple_signature", "Apple signed data could not be verified.")

    def transaction(self, signed):
        transaction = self._verify(self.verifier.verify_and_decode_signed_transaction, signed)
        if (transaction.bundleId != self.config.apple_bundle_id
                or transaction.environment != self.environment
                or transaction.type != Type.AUTO_RENEWABLE_SUBSCRIPTION
                or not transaction.originalTransactionId or transaction.expiresDate is None):
            fail(400, "invalid_apple_transaction", "Transaction identity or type is invalid.")
        if transaction.productId not in self.config.apple_product_ids:
            fail(400, "unsupported_product", "This subscription product is not supported.")
        if getattr(transaction, "inAppOwnershipType", None) == "FAMILY_SHARED":
            fail(400, "family_sharing_unsupported", "Family-shared subscriptions are not supported.")
        return transaction

    def renewal(self, signed, original_id):
        renewal = self._verify(self.verifier.verify_and_decode_renewal_info, signed)
        if (renewal.originalTransactionId != original_id
                or renewal.environment != self.environment
                or renewal.productId not in self.config.apple_product_ids
                or renewal.autoRenewStatus is None):
            fail(400, "invalid_apple_renewal", "Subscription renewal identity is invalid.")
        return renewal

    def notification(self, signed):
        notification = self._verify(self.verifier.verify_and_decode_notification, signed)
        if not notification.notificationUUID or notification.signedDate is None:
            fail(400, "invalid_apple_notification", "Missing notification identity.")
        return notification

    def snapshot(self, original_id):
        started_at = utcnow()
        try:
            response = self.client.get_all_subscription_statuses(original_id)
        except (APIException, RequestException):
            fail(503, "apple_unavailable", "Apple status retrieval failed. Retry later.")
        if (response.bundleId != self.config.apple_bundle_id
                or response.environment != self.environment
                or (self.environment == Environment.PRODUCTION
                    and response.appAppleId != self.config.apple_app_id)):
            fail(503, "invalid_apple_response", "Apple status identity did not match configuration.")
        for group in response.data or []:
            for item in group.lastTransactions or []:
                if item.originalTransactionId != original_id:
                    continue
                transaction = self.transaction(item.signedTransactionInfo)
                renewal = self.renewal(item.signedRenewalInfo, original_id)
                if transaction.originalTransactionId != original_id:
                    fail(503, "invalid_apple_response", "Apple transaction identity did not match.")
                status = {1: "active", 2: "expired", 3: "billing_retry", 4: "grace_period", 5: "revoked"}.get(item.status)
                if status is None:
                    fail(503, "invalid_apple_response", "Unknown Apple subscription status.")
                return AppleSnapshot(transaction, renewal, status, started_at)
        fail(503, "apple_status_missing", "Apple has not returned this subscription yet. Retry later.")


@lru_cache(maxsize=1)
def get_apple_gateway():
    return AppleGateway()
