"""Persist chapter-level study schedule projections.

Revision ID: b82e5c9d1a40
Revises: afc18ff984f6
"""
from alembic import op
import sqlalchemy as sa

revision = "b82e5c9d1a40"
down_revision = "afc18ff984f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "study_plans",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("parent_deck_id", sa.UUID(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("requested_target_date", sa.Date(), nullable=True),
        sa.Column("estimated_finish_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("study_weekdays_mask", sa.Integer(), nullable=False),
        sa.Column("daily_card_limit", sa.Integer(), nullable=False),
        sa.Column("required_daily_card_count", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("algorithm_version", sa.String(50), nullable=False),
        sa.Column("count_source", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("parent_deck_id", name="uq_study_plans_parent_deck"),
        sa.CheckConstraint("study_weekdays_mask BETWEEN 1 AND 127", name="ck_study_plans_weekdays"),
        sa.CheckConstraint("daily_card_limit > 0", name="ck_study_plans_daily_limit"),
        sa.CheckConstraint("revision > 0", name="ck_study_plans_revision"),
        sa.CheckConstraint("required_daily_card_count >= 0", name="ck_study_plans_required_count"),
        sa.CheckConstraint("count_source IN ('planned', 'actual')", name="ck_study_plans_count_source"),
        sa.CheckConstraint("requested_target_date >= start_date", name="ck_study_plans_target"),
        sa.CheckConstraint("estimated_finish_date >= start_date", name="ck_study_plans_finish"),
    )
    op.create_index("ix_study_plans_user_id", "study_plans", ["user_id"])
    op.create_table(
        "study_plan_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("study_plan_id", sa.UUID(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("item_type", sa.String(20), nullable=False),
        sa.Column("chapter_id", sa.UUID(), nullable=True),
        sa.Column("target_card_count", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["study_plan_id"], ["study_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chapter_id"], ["decks.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("study_plan_id", "position", name="uq_study_plan_items_position"),
        sa.CheckConstraint("position >= 0", name="ck_study_plan_items_position"),
        sa.CheckConstraint(
            "(item_type = 'learn' AND chapter_id IS NOT NULL AND target_card_count IS NOT NULL AND target_card_count > 0) "
            "OR (item_type IN ('first_half_exam', 'second_half_exam', 'final_exam') "
            "AND chapter_id IS NULL AND target_card_count IS NULL)",
            name="ck_study_plan_items_shape",
        ),
    )
    op.create_index("ix_study_plan_items_plan_date", "study_plan_items", ["study_plan_id", "scheduled_date"])
    op.create_index("ix_study_plan_items_chapter_id", "study_plan_items", ["chapter_id"])


def downgrade() -> None:
    op.drop_table("study_plan_items")
    op.drop_table("study_plans")
