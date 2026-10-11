"""Add user-scoped project entities for organizing Agent memories.

Revision ID: 20261008_07
Revises: 20261007_06
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261008_07"
down_revision = "20261007_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_user_projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_agent_user_projects_user_normalized_name",
        "agent_user_projects",
        ["user_id", "normalized_name"],
        unique=True,
    )
    op.create_index(
        "ix_agent_user_projects_user_updated_at",
        "agent_user_projects",
        ["user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_user_projects_user_updated_at", table_name="agent_user_projects")
    op.drop_index("uq_agent_user_projects_user_normalized_name", table_name="agent_user_projects")
    op.drop_table("agent_user_projects")
