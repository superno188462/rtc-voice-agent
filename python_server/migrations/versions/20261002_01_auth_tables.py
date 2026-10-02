"""Add local user accounts and revocable sessions.

Revision ID: 20261002_01
Revises:
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa


revision = "20261002_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_app_users_username"),
    )
    op.create_table(
        "app_auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_app_auth_sessions_token_hash"),
    )
    op.create_index("ix_app_auth_sessions_user_id", "app_auth_sessions", ["user_id"])
    op.create_index("ix_app_auth_sessions_expires_at", "app_auth_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_app_auth_sessions_expires_at", table_name="app_auth_sessions")
    op.drop_index("ix_app_auth_sessions_user_id", table_name="app_auth_sessions")
    op.drop_table("app_auth_sessions")
    op.drop_table("app_users")
