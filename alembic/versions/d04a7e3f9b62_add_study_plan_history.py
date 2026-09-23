"""Preserve closed targets and their learning cycle for adaptive schedules.

Revision ID: d04a7e3f9b62
Revises: c93f6d2e8b51
"""
from alembic import op
import sqlalchemy as sa

revision = "d04a7e3f9b62"
down_revision = "c93f6d2e8b51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("study_plan_items", sa.Column("learning_epoch", sa.UUID(), nullable=True))
    op.add_column("study_plan_items", sa.Column("actual_learned_count", sa.Integer(), nullable=True))
    op.add_column("study_plan_items", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("study_plan_items", sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE study_plan_items AS item SET learning_epoch = deck.progress_epoch "
               "FROM decks AS deck WHERE item.chapter_id = deck.id AND item.item_type = 'learn'")
    op.create_check_constraint("ck_study_plan_items_actual", "study_plan_items",
                               "actual_learned_count IS NULL OR (item_type = 'learn' AND closed_at IS NOT NULL "
                               "AND actual_learned_count BETWEEN 0 AND target_card_count)")
    op.create_check_constraint("ck_study_plan_items_achievement", "study_plan_items",
                               "achieved_at IS NULL OR item_type <> 'learn'")


def downgrade() -> None:
    op.drop_constraint("ck_study_plan_items_achievement", "study_plan_items", type_="check")
    op.drop_constraint("ck_study_plan_items_actual", "study_plan_items", type_="check")
    for column in ("achieved_at", "closed_at", "actual_learned_count", "learning_epoch"):
        op.drop_column("study_plan_items", column)
