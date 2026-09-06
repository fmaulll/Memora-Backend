"""add exam attempts

Revision ID: b7d9e2f4a1c6
Revises: a4c8e1f2b6d7
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d9e2f4a1c6"
down_revision: Union[str, Sequence[str], None] = "a4c8e1f2b6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exam_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("exam_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("total_questions", sa.Integer(), nullable=False),
        sa.Column("correct_answers", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "exam_id",
            "attempt_number",
            name="uq_exam_attempts_number",
        ),
    )
    op.create_index(op.f("ix_exam_attempts_user_id"), "exam_attempts", ["user_id"], unique=False)
    op.create_index(op.f("ix_exam_attempts_exam_id"), "exam_attempts", ["exam_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_exam_attempts_exam_id"), table_name="exam_attempts")
    op.drop_index(op.f("ix_exam_attempts_user_id"), table_name="exam_attempts")
    op.drop_table("exam_attempts")