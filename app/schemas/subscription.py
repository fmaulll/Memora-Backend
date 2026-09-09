import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class Entitlement(BaseModel):
    is_subscribed: bool = False
    status: str = "none"
    product_id: str | None = None
    expires_at: datetime | None = None
    auto_renew: bool = False
    grace_period_expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_verified_at: datetime | None = None


class SubscriptionResponse(BaseModel):
    app_account_token: uuid.UUID
    entitlement: Entitlement
    free_ai_deck_available: bool


class VerifyPurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signed_transaction: str = Field(min_length=1, max_length=100000)


class AppleNotificationRequest(BaseModel):
    signedPayload: str = Field(min_length=1, max_length=200000)
