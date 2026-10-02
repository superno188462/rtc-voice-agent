"""Add local user role and persistent login failure limits.

Revision ID: 20261002_02
Revises: 20261002_01
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa


revision = "20261002_02"
down_revision = "20261002_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_users",
        sa.Column("role", sa.String(length=16), server_default="user", nullable=False),
    )
    op.create_table(
        "app_login_attempts",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("scope", "key_hash", name="pk_app_login_attempts"),
    )
    op.create_index(
        "ix_app_login_attempts_updated_at", "app_login_attempts", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_app_login_attempts_updated_at", table_name="app_login_attempts")
    op.drop_table("app_login_attempts")
    op.drop_column("app_users", "role")
