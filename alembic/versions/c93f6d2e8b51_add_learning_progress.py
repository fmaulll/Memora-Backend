"""Exact-card learned facts and retry-safe resets/submissions.

Revision ID: c93f6d2e8b51
Revises: b82e5c9d1a40
"""
from alembic import op
import sqlalchemy as sa

revision = "c93f6d2e8b51"
down_revision = "b82e5c9d1a40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decks", sa.Column("progress_epoch", sa.UUID(), nullable=True))
    op.execute("UPDATE decks SET progress_epoch = gen_random_uuid()")
    op.alter_column("decks", "progress_epoch", nullable=False)
    op.create_table(
        "card_progress",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("card_id", sa.UUID(), nullable=False),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "card_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_card_progress_card_id", "card_progress", ["card_id"])
    op.create_table(
        "study_progress_receipts",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "operation_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("operation IN ('submission', 'reset')", name="ck_study_progress_receipts_operation"),
    )


def downgrade() -> None:
    op.drop_table("study_progress_receipts")
    op.drop_table("card_progress")
    op.drop_column("decks", "progress_epoch")
