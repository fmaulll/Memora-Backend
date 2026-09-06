"""add exams and user exam progression

Revision ID: 9b2f7c1d4e6a
Revises: 8575458913f7
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9b2f7c1d4e6a"
down_revision: Union[str, Sequence[str], None] = "cf33830d223e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exams",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("deck_id", sa.UUID(), nullable=False),
        sa.Column("exam_type", sa.String(length=20), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("passing_score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deck_id", "exam_type", name="uq_exams_deck_type"),
    )
    op.create_index(op.f("ix_exams_deck_id"), "exams", ["deck_id"], unique=False)

    op.create_table(
        "user_exam_progressions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("deck_id", sa.UUID(), nullable=False),
        sa.Column("first_half_passed", sa.Boolean(), nullable=False),
        sa.Column("second_half_passed", sa.Boolean(), nullable=False),
        sa.Column("final_passed", sa.Boolean(), nullable=False),
        sa.Column("first_half_best_score", sa.Integer(), nullable=True),
        sa.Column("second_half_best_score", sa.Integer(), nullable=True),
        sa.Column("final_best_score", sa.Integer(), nullable=True),
        sa.Column("first_half_attempt_count", sa.Integer(), nullable=False),
        sa.Column("second_half_attempt_count", sa.Integer(), nullable=False),
        sa.Column("final_attempt_count", sa.Integer(), nullable=False),
        sa.Column("first_half_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("second_half_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "deck_id", name="uq_user_exam_progression"),
    )
    op.create_index(
        op.f("ix_user_exam_progressions_user_id"),
        "user_exam_progressions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_exam_progressions_deck_id"),
        "user_exam_progressions",
        ["deck_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_exam_progressions_deck_id"), table_name="user_exam_progressions")
    op.drop_index(op.f("ix_user_exam_progressions_user_id"), table_name="user_exam_progressions")
    op.drop_table("user_exam_progressions")
    op.drop_index(op.f("ix_exams_deck_id"), table_name="exams")
    op.drop_table("exams")