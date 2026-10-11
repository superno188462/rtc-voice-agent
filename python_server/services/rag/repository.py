"""知识库目录、哈希和 Gateway 向量 ID 映射；新写入不保留文档/切片正文。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from services.auth.database import get_session_factory
from services.rag.models import KnowledgeChunk, KnowledgeDocument


class KnowledgeBaseRepository:
    """持久化虚拟路径、内容哈希、切片序号与向量 ID 映射。"""

    async def stage_document(
        self,
        *,
        document_id: str,
        title: str,
        path: str,
        file_name: str | None,
        content_sha256: str,
        metadata: dict[str, Any],
        chunks: list[dict[str, Any]],
    ) -> list[str]:
        """先持久化目录项和目标切片映射，正文只发送 Gateway、不写入本地库。"""

        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                document = await session.get(KnowledgeDocument, document_id, with_for_update=True)
                old_ids = list((await session.scalars(
                    select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == document_id)
                )).all())
                if document is None:
                    document = KnowledgeDocument(
                        id=document_id, title=title, path=path, content=None, file_name=file_name,
                        content_sha256=content_sha256, metadata_json=metadata, sync_status="syncing",
                    )
                    session.add(document)
                else:
                    document.title = title
                    document.path = path
                    document.content = None
                    document.file_name = file_name
                    document.content_sha256 = content_sha256
                    document.metadata_json = metadata
                    document.sync_status = "syncing"
                for chunk in chunks:
                    row = await session.get(KnowledgeChunk, chunk["id"])
                    if row is None:
                        row = KnowledgeChunk(id=chunk["id"], document_id=document_id)
                        session.add(row)
                    row.chunk_index = chunk["chunk_index"]
                    row.content = None
                    row.metadata_json = chunk["metadata"]
                    row.is_active = False
            return old_ids

    async def finish_sync(self, document_id: str, active_ids: list[str]) -> None:
        """标记目标切片生效并清除已从 Gateway 删除的旧映射。"""

        factory = get_session_factory()
        active = set(active_ids)
        async with factory() as session:
            async with session.begin():
                document = await session.get(KnowledgeDocument, document_id, with_for_update=True)
                if document is None:
                    return
                rows = list((await session.scalars(
                    select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
                )).all())
                for row in rows:
                    if row.id in active:
                        row.is_active = True
                    else:
                        await session.delete(row)
                document.sync_status = "ready"

    async def mark_failed(self, document_id: str) -> None:
        """保留全部可能已写入 Gateway 的 ID，以便重试或删除时清理。"""

        factory = get_session_factory()
        async with factory() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is not None:
                document.sync_status = "failed"
                await session.commit()

    async def list_documents(self) -> list[dict[str, Any]]:
        """列出本项目已登记文档，不返回正文。"""

        factory = get_session_factory()
        async with factory() as session:
            rows = (await session.scalars(
                select(KnowledgeDocument)
                .options(selectinload(KnowledgeDocument.chunks))
                .order_by(KnowledgeDocument.updated_at.desc())
            )).all()
            return [
                {
                    "document_id": row.id,
                    "title": row.title,
                    "path": row.path,
                    "name": PurePosixPath(row.path).name if row.path else row.file_name or row.title,
                    "directory": str(PurePosixPath(row.path).parent) if row.path and str(PurePosixPath(row.path).parent) != "." else "",
                    "source_type": (row.metadata_json or {}).get("source_type", "file" if row.file_name else "text"),
                    "file_name": row.file_name,
                    "content_sha256": row.content_sha256,
                    "sync_status": row.sync_status,
                    "chunk_count": len(row.chunks),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ]

    async def get_document_by_path(self, path: str) -> dict[str, Any] | None:
        """按当前知识库的规范化虚拟路径读取目录项，用于稳定识别更新对象。"""

        factory = get_session_factory()
        async with factory() as session:
            legacy_name = PurePosixPath(path).name
            match_legacy_root = "/" not in path
            path_predicates = [KnowledgeDocument.path == path]
            if match_legacy_root:
                path_predicates.append(and_(
                    KnowledgeDocument.path.is_(None),
                    or_(KnowledgeDocument.file_name == legacy_name,
                        and_(KnowledgeDocument.file_name.is_(None), KnowledgeDocument.title == legacy_name)),
                ))
            row = await session.scalar(
                select(KnowledgeDocument)
                .where(or_(*path_predicates))
                .order_by(KnowledgeDocument.updated_at.desc())
                .limit(1)
                .with_for_update()
            )
            if row is None:
                return None
            return {
                "document_id": row.id,
                "content_sha256": row.content_sha256,
                "title": row.title,
                "file_name": row.file_name,
                "metadata": row.metadata_json,
                "sync_status": row.sync_status,
                "chunk_count": await session.scalar(
                    select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.document_id == row.id)
                ),
            }

    async def get_chunk_ids(self, document_id: str) -> list[str]:
        """读取文档关联的全部向量 ID（包括同步未完成时的待清理 ID）。"""

        factory = get_session_factory()
        async with factory() as session:
            return list((await session.scalars(
                select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == document_id)
            )).all())

    async def delete_local_document(self, document_id: str) -> bool:
        """在 Gateway 向量成功删除后，删除本地文档与切片映射。"""

        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                document = await session.get(KnowledgeDocument, document_id, with_for_update=True)
                if document is None:
                    return False
                await session.delete(document)
            return True
