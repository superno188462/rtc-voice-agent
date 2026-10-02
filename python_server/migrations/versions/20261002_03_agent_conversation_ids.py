"""Add stable per-user Agent conversation IDs and turn receipts.

Revision ID: 20261002_03
Revises: 20261002_02
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa


revision = "20261002_03"
down_revision = "20261002_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_agent_conversations_user_id"),
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
    op.create_index(
        "uq_agent_turn_conversation_turn",
        "agent_turns",
        ["conversation_id", "turn_id"],
        unique=True,
    )
    op.create_index("ix_agent_turn_updated_at", "agent_turns", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_turn_updated_at", table_name="agent_turns")
    op.drop_index("uq_agent_turn_conversation_turn", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_table("agent_conversations")
