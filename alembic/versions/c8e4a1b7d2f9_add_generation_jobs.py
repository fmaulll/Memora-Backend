"""add durable deck generation jobs

Revision ID: c8e4a1b7d2f9
Revises: b7d9e2f4a1c6
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e4a1b7d2f9"
down_revision: Union[str, Sequence[str], None] = "b7d9e2f4a1c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("parent_deck_id", sa.UUID(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["parent_deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_deck_id", name="uq_generation_jobs_parent_deck"),
    )
    op.create_index(
        op.f("ix_generation_jobs_parent_deck_id"),
        "generation_jobs",
        ["parent_deck_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_generation_jobs_parent_deck_id"),
        table_name="generation_jobs",
    )
    op.drop_table("generation_jobs")