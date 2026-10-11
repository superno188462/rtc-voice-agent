"""Move authentication to Identity Center and reset local Agent conversations.

Revision ID: 20261007_06
Revises: 20261005_05
Create Date: 2026-10-07

This migration intentionally drops local account/session data and prior Agent
conversation/turn receipts. Agent conversations are recreated with user IDs
provided by Identity Center and no foreign key to a local users table.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261007_06"
down_revision = "20261005_05"
branch_labels = None
depends_on = None


def _create_agent_tables(*, legacy_user_fk: bool) -> None:
    constraints = [sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("user_id", name="uq_agent_conversations_user_id")]
    if legacy_user_fk:
        constraints.append(sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"))
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        *constraints,
    )
    op.create_table(
        "agent_turns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("pair_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_agent_turn_conversation_turn", "agent_turns", ["conversation_id", "turn_id"], unique=True)
    op.create_index("ix_agent_turn_updated_at", "agent_turns", ["updated_at"])


def _create_legacy_auth_tables() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("role", sa.String(length=16), server_default="user", nullable=False),
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
    op.create_table(
        "app_login_attempts",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "key_hash", name="pk_app_login_attempts"),
    )
    op.create_index("ix_app_login_attempts_updated_at", "app_login_attempts", ["updated_at"])


def upgrade() -> None:
    op.drop_index("ix_agent_turn_updated_at", table_name="agent_turns")
    op.drop_index("uq_agent_turn_conversation_turn", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_table("agent_conversations")
    op.drop_index("ix_app_login_attempts_updated_at", table_name="app_login_attempts")
    op.drop_table("app_login_attempts")
    op.drop_index("ix_app_auth_sessions_expires_at", table_name="app_auth_sessions")
    op.drop_index("ix_app_auth_sessions_user_id", table_name="app_auth_sessions")
    op.drop_table("app_auth_sessions")
    op.drop_table("app_users")
    _create_agent_tables(legacy_user_fk=False)


def downgrade() -> None:
    op.drop_index("ix_agent_turn_updated_at", table_name="agent_turns")
    op.drop_index("uq_agent_turn_conversation_turn", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_table("agent_conversations")
    _create_legacy_auth_tables()
    _create_agent_tables(legacy_user_fk=True)
