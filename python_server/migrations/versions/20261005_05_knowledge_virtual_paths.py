"""Add virtual file paths for knowledge documents.

Revision ID: 20261005_05
Revises: 20261005_04
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa


revision = "20261005_05"
down_revision = "20261005_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_documents", sa.Column("path", sa.String(length=1000), nullable=True))
    op.create_index("uq_knowledge_documents_path", "knowledge_documents", ["path"], unique=True)
    op.alter_column("knowledge_documents", "content", existing_type=sa.Text(), nullable=True)
    op.alter_column("knowledge_chunks", "content", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.alter_column("knowledge_chunks", "content", existing_type=sa.Text(), nullable=False)
    op.alter_column("knowledge_documents", "content", existing_type=sa.Text(), nullable=False)
    op.drop_index("uq_knowledge_documents_path", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "path")
