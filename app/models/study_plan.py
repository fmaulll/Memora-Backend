import uuid
from datetime import date, datetime, timezone

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StudyPlan(Base):
    __tablename__ = "study_plans"
    __table_args__ = (
        UniqueConstraint("parent_deck_id", name="uq_study_plans_parent_deck"),
        CheckConstraint("study_weekdays_mask BETWEEN 1 AND 127", name="ck_study_plans_weekdays"),
        CheckConstraint("daily_card_limit > 0", name="ck_study_plans_daily_limit"),
        CheckConstraint("revision > 0", name="ck_study_plans_revision"),
        CheckConstraint("required_daily_card_count >= 0", name="ck_study_plans_required_count"),
        CheckConstraint("count_source IN ('planned', 'actual')", name="ck_study_plans_count_source"),
        CheckConstraint("requested_target_date >= start_date", name="ck_study_plans_target"),
        CheckConstraint("estimated_finish_date >= start_date", name="ck_study_plans_finish"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_deck_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decks.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    requested_target_date: Mapped[date | None] = mapped_column(Date)
    estimated_finish_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(100))
    # Seven preference bits, Monday=bit 0. Scheduling items remain relational.
    study_weekdays_mask: Mapped[int] = mapped_column(Integer)
    daily_card_limit: Mapped[int] = mapped_column(Integer)
    required_daily_card_count: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    algorithm_version: Mapped[str] = mapped_column(String(50))
    count_source: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    parent_deck = relationship("Deck", back_populates="study_plan")
    items: Mapped[list["StudyPlanItem"]] = relationship(
        back_populates="study_plan", cascade="all, delete-orphan", order_by="StudyPlanItem.position",
    )

    @property
    def study_weekdays(self) -> list[int]:
        return [day for day in range(7) if self.study_weekdays_mask & (1 << day)]


class StudyPlanItem(Base):
    __tablename__ = "study_plan_items"
    __table_args__ = (
        UniqueConstraint("study_plan_id", "position", name="uq_study_plan_items_position"),
        CheckConstraint("position >= 0", name="ck_study_plan_items_position"),
        CheckConstraint("actual_learned_count IS NULL OR (item_type = 'learn' AND closed_at IS NOT NULL "
                        "AND actual_learned_count BETWEEN 0 AND target_card_count)",
                        name="ck_study_plan_items_actual"),
        CheckConstraint("achieved_at IS NULL OR item_type <> 'learn'", name="ck_study_plan_items_achievement"),
        CheckConstraint(
            "(item_type = 'learn' AND chapter_id IS NOT NULL AND target_card_count IS NOT NULL AND target_card_count > 0) "
            "OR (item_type IN ('first_half_exam', 'second_half_exam', 'final_exam') "
            "AND chapter_id IS NULL AND target_card_count IS NULL)",
            name="ck_study_plan_items_shape",
        ),
        Index("ix_study_plan_items_plan_date", "study_plan_id", "scheduled_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("study_plans.id", ondelete="CASCADE"))
    scheduled_date: Mapped[date] = mapped_column(Date)
    item_type: Mapped[str] = mapped_column(String(20))
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decks.id", ondelete="RESTRICT"), index=True)
    target_card_count: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    # Epoch is deliberately not a foreign key: a reset must not rewrite history.
    learning_epoch: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actual_learned_count: Mapped[int | None] = mapped_column(Integer)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    study_plan: Mapped[StudyPlan] = relationship(back_populates="items")
