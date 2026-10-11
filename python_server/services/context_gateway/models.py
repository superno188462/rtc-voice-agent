"""用户在长期对话中提到的项目实体目录。"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.auth.database import Base


class UserProject(Base):
    """为用户对话中稳定出现的项目维护程序生成的 ID 与名称/别名。"""

    __tablename__ = "agent_user_projects"
    __table_args__ = (
        Index("uq_agent_user_projects_user_normalized_name", "user_id", "normalized_name", unique=True),
        Index("ix_agent_user_projects_user_updated_at", "user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemoryVectorIndex(Base):
    """只保存长期记忆向量索引同步状态，不保留记忆正文。"""

    __tablename__ = "agent_memory_vector_index"
    __table_args__ = (
        Index("uq_agent_memory_vector_index_user_collection_memory", "user_id", "collection", "memory_id", unique=True),
        Index("ix_agent_memory_vector_index_user_collection", "user_id", "collection"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    collection: Mapped[str] = mapped_column(String(200), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemoryVectorBackfill(Base):
    """记录用户在指定长期记忆 collection 上是否完成过历史数据向量化。"""

    __tablename__ = "agent_memory_vector_backfills"
    __table_args__ = (
        Index("uq_agent_memory_vector_backfills_user_collection", "user_id", "collection", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    collection: Mapped[str] = mapped_column(String(200), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
