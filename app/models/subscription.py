import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    original_transaction_id: Mapped[str] = mapped_column(String(100), unique=True)
    product_id: Mapped[str] = mapped_column(String(255))
    environment: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_renew: Mapped[bool] = mapped_column(Boolean)
    grace_period_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Only freshly retrieved server snapshots are applied, never notification state.
    snapshot_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppleNotification(Base):
    __tablename__ = "apple_notifications"

    notification_uuid: Mapped[str] = mapped_column(String(100), primary_key=True)
    notification_type: Mapped[str] = mapped_column(String(100))
    original_transaction_id: Mapped[str | None] = mapped_column(String(100))
    signed_date: Mapped[int] = mapped_column(BigInteger)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PurchaseTokenAlias(Base):
    """Retain Apple's old appAccountToken after an explicit account merge."""
    __tablename__ = "purchase_token_aliases"

    token: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)


class AIGenerationRequest(Base):
    """Durable admission receipt; deleting a deck never restores the allowance."""
    __tablename__ = "ai_generation_requests"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_ai_request_user_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[uuid.UUID] = mapped_column()
    request_hash: Mapped[str] = mapped_column(String(64))
    used_free_allowance: Mapped[bool] = mapped_column(Boolean)
    parent_deck_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decks.id", ondelete="SET NULL"), unique=True)
    response_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
