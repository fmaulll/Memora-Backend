"""add exam questions

Revision ID: a4c8e1f2b6d7
Revises: 9b2f7c1d4e6a
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4c8e1f2b6d7"
down_revision: Union[str, Sequence[str], None] = "9b2f7c1d4e6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exam_questions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("exam_id", sa.UUID(), nullable=False),
        sa.Column("question_type", sa.String(length=30), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("correct_answer", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("source_card_id", sa.UUID(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_card_id"], ["cards.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exam_id", "position", name="uq_exam_questions_position"),
    )
    op.create_index(
        op.f("ix_exam_questions_exam_id"),
        "exam_questions",
        ["exam_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_exam_questions_source_card_id"),
        "exam_questions",
        ["source_card_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_exam_questions_source_card_id"),
        table_name="exam_questions",
    )
    op.drop_index(
        op.f("ix_exam_questions_exam_id"),
        table_name="exam_questions",
    )
    op.drop_table("exam_questions")