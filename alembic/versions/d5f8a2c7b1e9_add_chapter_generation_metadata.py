"""add chapter generation metadata

Revision ID: d5f8a2c7b1e9
Revises: c8e4a1b7d2f9
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5f8a2c7b1e9"
down_revision: Union[str, Sequence[str], None] = "c8e4a1b7d2f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("decks", sa.Column("key_concepts", sa.JSON(), nullable=True))
    op.add_column("decks", sa.Column("card_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("decks", "card_count")
    op.drop_column("decks", "key_concepts")