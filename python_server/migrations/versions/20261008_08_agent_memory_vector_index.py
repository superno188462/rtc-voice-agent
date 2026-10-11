"""Track per-user long-term memory vector synchronization without storing content.

Revision ID: 20261008_08
Revises: 20261008_07
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa


revision = "20261008_08"
down_revision = "20261008_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_memory_vector_index",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("collection", sa.String(length=200), nullable=False),
        sa.Column("memory_id", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata_sha256", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_agent_memory_vector_index_user_collection_memory",
        "agent_memory_vector_index",
        ["user_id", "collection", "memory_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_memory_vector_index_user_collection",
        "agent_memory_vector_index",
        ["user_id", "collection"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_memory_vector_index_user_collection", table_name="agent_memory_vector_index")
    op.drop_index("uq_agent_memory_vector_index_user_collection_memory", table_name="agent_memory_vector_index")
    op.drop_table("agent_memory_vector_index")
