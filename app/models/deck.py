import uuid

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Deck(Base):
    __tablename__ = "decks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )

    parent_deck_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decks.id"),
        nullable=True,
    )

    parent_deck: Mapped["Deck | None"] = relationship(
        "Deck",
        remote_side="Deck.id",
        back_populates="child_decks",
    )

    child_decks: Mapped[list["Deck"]] = relationship(
        "Deck",
        back_populates="parent_deck",
    )

    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    key_concepts: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    card_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    subject: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    education_level: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    learning_language: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    is_favorite: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    generation_status: Mapped[str] = mapped_column(
        String(20),
        default="completed",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    cards = relationship(
        "Card",
        back_populates="deck",
        cascade="all, delete-orphan",
    )

    exams = relationship(
        "Exam",
        back_populates="deck",
        cascade="all, delete-orphan",
    )