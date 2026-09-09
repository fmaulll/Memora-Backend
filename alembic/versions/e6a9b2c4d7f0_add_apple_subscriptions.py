"""Apple subscriptions, account identity and durable AI admission receipts.

Revision ID: e6a9b2c4d7f0
Revises: d5f8a2c7b1e9
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "e6a9b2c4d7f0"
down_revision = "d5f8a2c7b1e9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("app_account_token", UUID(as_uuid=True), nullable=True))
    op.add_column("users", sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("free_ai_deck_used", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Stable token for existing users; no extension required for UUID generation.
    op.execute("UPDATE users SET app_account_token = id")
    op.execute("UPDATE users SET is_anonymous = true WHERE email = 'anonymous-' || CAST(id AS text) || '@memoraapp.com'")
    # Existing accepted AI jobs count toward the lifetime allowance.
    op.execute("UPDATE users SET free_ai_deck_used = true WHERE EXISTS (SELECT 1 FROM decks JOIN generation_jobs ON generation_jobs.parent_deck_id = decks.id WHERE decks.user_id = users.id)")
    op.alter_column("users", "app_account_token", nullable=False)
    op.create_unique_constraint("uq_users_app_account_token", "users", ["app_account_token"])
    op.create_table("subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("original_transaction_id", sa.String(100), nullable=False, unique=True),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("auto_renew", sa.Boolean(), nullable=False),
        sa.Column("grace_period_expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("apple_notifications",
        sa.Column("notification_uuid", sa.String(100), primary_key=True),
        sa.Column("notification_type", sa.String(100), nullable=False),
        sa.Column("original_transaction_id", sa.String(100)),
        sa.Column("signed_date", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("purchase_token_aliases",
        sa.Column("token", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True),
    )
    op.create_table("ai_generation_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("idempotency_key", UUID(as_uuid=True), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("used_free_allowance", sa.Boolean(), nullable=False),
        sa.Column("parent_deck_id", UUID(as_uuid=True), sa.ForeignKey("decks.id", ondelete="SET NULL"), unique=True),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_ai_request_user_key"),
    )


def downgrade():
    for table in ("ai_generation_requests", "purchase_token_aliases", "apple_notifications", "subscriptions"):
        op.drop_table(table)
    op.drop_constraint("uq_users_app_account_token", "users", type_="unique")
    for column in ("free_ai_deck_used", "is_anonymous", "app_account_token"):
        op.drop_column("users", column)
