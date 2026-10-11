"""Track one-time legacy memory vector backfills per user and collection.

Revision ID: 20261008_09
Revises: 20261008_08
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa


revision = "20261008_09"
down_revision = "20261008_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_memory_vector_backfills",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("collection", sa.String(length=200), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_agent_memory_vector_backfills_user_collection",
        "agent_memory_vector_backfills",
        ["user_id", "collection"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_agent_memory_vector_backfills_user_collection",
        table_name="agent_memory_vector_backfills",
    )
    op.drop_table("agent_memory_vector_backfills")
