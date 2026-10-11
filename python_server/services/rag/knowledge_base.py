"""知识库用例：本项目管理原文、切片和 Gateway 向量记录生命周期。"""

from __future__ import annotations

import hashlib
import os
import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Any

from services.rag.chunking import split_text
from services.rag.gateway_vector_store import GatewayVectorStore, get_gateway_vector_store
from services.rag.repository import KnowledgeBaseRepository

CHUNK_OVERLAP = 120


def normalize_virtual_path(directory: str, name: str) -> str:
    """Validate and normalize the user-facing virtual directory and file name."""

    normalized_name = unicodedata.normalize("NFC", name.strip())
    normalized_directory = unicodedata.normalize("NFC", directory.strip().replace("\\", "/"))
    if not normalized_name or normalized_name in {".", ".."} or "/" in normalized_name:
        raise ValueError("名称不能为空，也不能包含目录分隔符。")
    parts = [part.strip() for part in normalized_directory.split("/") if part.strip()]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("目录不能包含 . 或 .. 路径段。")
    path = str(PurePosixPath(*parts, normalized_name))
    if len(path) > 1000:
        raise ValueError("目录和名称合计不能超过 1000 个字符。")
    return path


class KnowledgeBaseService:
    """为管理 API 与 Agent RAG 提供项目级知识库操作。"""

    def __init__(self, vector_store: GatewayVectorStore | None = None, repository: KnowledgeBaseRepository | None = None) -> None:
        self.vector_store = vector_store or get_gateway_vector_store()
        self.repository = repository or KnowledgeBaseRepository()

    async def status(self) -> dict[str, Any]:
        """读取配置集合状态；未配置时返回可诊断的未配置状态。"""

        try:
            self.vector_store.ensure_configured()
        except Exception:
            return {"configured": False, "exists": False}
        collections = await self.vector_store.list_collections()
        item = next(
            (entry for entry in collections if entry.get("collection") == self.vector_store.collection),
            None,
        )
        return {
            "configured": True,
            "exists": item is not None,
            "collection": self.vector_store.collection,
            "model": item.get("model") if item else None,
            "vector_dimensions": item.get("vector_dimensions") if item else None,
            "chunk_size": item.get("chunk_size") if item else None,
            "chunk_overlap": min(CHUNK_OVERLAP, int(item.get("chunk_size", 1000)) // 5) if item else None,
        }

    async def initialize(self, *, description: str, model: str, chunk_size: int) -> dict[str, Any]:
        """创建集合或校验已有集合；Embedding 模型在初始化时指定。"""
        return await self.vector_store.ensure_collection(
            model=model,
            description=description,
            chunk_size=chunk_size,
        )

    async def upsert_document(
        self,
        *,
        document_id: str | None,
        title: str,
        content: str,
        metadata: dict[str, Any],
        file_name: str | None = None,
        directory: str = "",
        name: str | None = None,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        """按虚拟路径新增或更新目录项，切片后逐条写入 Gateway。"""

        results = await self.upsert_documents(
            [{"document_id": document_id, "file_name": file_name, "title": title, "content": content,
              "metadata": metadata, "directory": directory, "name": name, "source_type": source_type}]
        )
        return results[0]

    async def upsert_documents(self, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """保存目录/哈希/切片映射，并按 collection 上限写入独立向量记录。"""

        status = await self.status()
        if not status.get("exists"):
            raise ValueError("知识库集合尚未创建")
        chunk_size = int(status.get("chunk_size") or 1000)
        results: list[dict[str, Any]] = []
        submitted_paths: set[str] = set()
        for document in documents:
            title = str(document["title"])
            content = str(document["content"])
            raw_id = document.get("document_id")
            raw_file_name = document.get("file_name")
            requested_name = document.get("name") or raw_file_name or title
            requested_source = str(document.get("source_type") or ("file" if raw_file_name else "text"))
            if requested_source not in {"file", "text"}:
                raise ValueError("source_type 只能是 file 或 text。")
            path = normalize_virtual_path(str(document.get("directory") or ""), str(requested_name))
            if path in submitted_paths:
                raise ValueError(f"批次中包含重复路径：{path}")
            submitted_paths.add(path)
            file_name = str(requested_name) if requested_source == "file" else None
            existing = await self.repository.get_document_by_path(path)
            stable_id = (
                str(existing["document_id"])
                if existing
                else (raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else str(uuid.uuid4()))
            )
            normalized_content = unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n")).strip()
            content_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
            document_metadata = dict(document.get("metadata") or {})
            document_metadata.update({
                "metadata_schema_version": 1,
                "source_type": requested_source,
                "path": path,
                "name": str(requested_name),
                "content_sha256": content_hash,
            })
            if file_name:
                document_metadata.update({
                    "file_name": file_name,
                    "file_extension": PurePosixPath(file_name).suffix.lower().lstrip("."),
                })
            else:
                document_metadata.pop("file_name", None)
                document_metadata.pop("file_extension", None)
            if (
                existing
                and existing.get("sync_status") == "ready"
                and existing["content_sha256"] == content_hash
                and existing["metadata"] == document_metadata
                and existing["title"] == title
            ):
                results.append({
                    "id": stable_id,
                    "result": {"vectors": 0, "chunks": existing.get("chunk_count", 0), "action": "unchanged"},
                })
                continue
            content = normalized_content
            overlap = min(CHUNK_OVERLAP, chunk_size // 5)
            pieces = split_text(content, chunk_size=chunk_size, overlap=overlap)
            if not pieces:
                raise ValueError(f"文档 {title} 没有可写入的文本")
            doc_hash = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:32]
            records = [{
                "id": f"{doc_hash}-{index:06d}",
                "title": title,
                "content": piece,
                "metadata": {
                    **document_metadata,
                    "document_id": stable_id,
                    "chunk_index": index,
                    "chunk_count": len(pieces),
                    "chunk_sha256": hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                },
                "chunk_index": index,
            } for index, piece in enumerate(pieces)]
            old_ids = await self.repository.stage_document(
                document_id=stable_id,
                title=title,
                path=path,
                file_name=file_name,
                content_sha256=content_hash,
                metadata=document_metadata,
                chunks=records,
            )
            target_ids = [record["id"] for record in records]
            try:
                response = await self.vector_store.add_vectors([
                    {key: record[key] for key in ("id", "title", "content", "metadata")}
                    for record in records
                ])
                upserted = response.get("upserted", [])
                written = len(upserted) if isinstance(upserted, list) else len(records)
                target_set = set(target_ids)
                stale_ids = [vector_id for vector_id in old_ids if vector_id not in target_set]
                if stale_ids:
                    await self.vector_store.delete(stale_ids)
                # Remove a legacy whole-document vector after the replacement chunks are stored.
                if not old_ids:
                    await self.vector_store.delete([stable_id])
                await self.repository.finish_sync(stable_id, target_ids)
            except Exception:
                await self.repository.mark_failed(stable_id)
                raise
            results.append({"id": stable_id, "result": {
                "vectors": written, "chunks": len(target_ids),
                "action": "updated" if existing else "created",
            }})
        return results

    async def delete_document(self, document_id: str) -> dict[str, Any]:
        """删除文档关联的所有切片向量；Gateway 成功后再删除本地原文和映射。"""

        ids = await self.repository.get_chunk_ids(document_id)
        if not ids:
            await self.vector_store.delete([document_id])
            await self.repository.delete_local_document(document_id)
            return {"deleted": 0}
        result = await self.vector_store.delete(ids)
        await self.repository.delete_local_document(document_id)
        return result

    async def list_documents(self) -> list[dict[str, Any]]:
        """列出已登记文档以支持刷新后的更新和删除。"""

        return await self.repository.list_documents()

    async def search(self, query: str, *, top_k: int = 5, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """执行 Gateway 相似度查询并返回项目使用的统一检索结果结构。"""

        results = await self.vector_store.similarity_search(query, k=top_k, metadata=metadata)
        return self._normalize_search_results(results)

    async def search_with_context(
        self,
        query: str,
        *,
        top_k: int = 5,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """检索子片并按 Gateway 返回的 parent_id 读取父块上下文。"""

        results = await self.vector_store.similarity_search_with_context(
            query, k=top_k, metadata=metadata
        )
        return self._normalize_search_results(results)

    @staticmethod
    def _normalize_search_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 SDK 命中转换为本项目稳定的子片/父块检索结果。"""

        normalized: list[dict[str, Any]] = []
        for item in results:
            item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            normalized.append(
                {
                    "chunk_id": str(item.get("chunk_id", item.get("id", ""))),
                    "document_id": str(item_metadata.get("document_id", item.get("document_id", item.get("external_id", "")))),
                    "title": str(item.get("title", "")),
                    "content": str(item.get("content", item.get("text", ""))),
                    "score": item.get("score", item.get("similarity")),
                    "metadata": item_metadata,
                    "parent_id": item.get("parent_id") or item_metadata.get("parent_id"),
                    "parent_content": item.get("context_content") or "",
                    "context_status": item.get("context_status") or "unavailable",
                }
            )
        return normalized

    async def search_context(self, query: str) -> str:
        """将命中片段格式化为可注入现有 LLM user 消息的参考资料。"""

        results = await self.search(query, top_k=int(os.getenv("RAG_TOP_K", "5")))
        grouped: dict[str, list[dict[str, Any]]] = {}
        ranked_documents: list[str] = []
        for item in results:
            document_id = item["document_id"] or item["chunk_id"]
            if document_id not in grouped:
                grouped[document_id] = []
                ranked_documents.append(document_id)
            grouped[document_id].append(item)
        blocks = []
        for document_id in ranked_documents:
            chunks = sorted(
                grouped[document_id],
                key=lambda item: int(item["metadata"].get("chunk_index", 0)),
            )
            content = "\n".join(item["content"] for item in chunks if item["content"].strip())
            if content:
                blocks.append(f"[{chunks[0]['title'] or '知识库片段'}]\n{content}")
        return "\n\n".join(blocks)


_knowledge_base_service = KnowledgeBaseService()


def get_knowledge_base_service() -> KnowledgeBaseService:
    """返回共享知识库应用服务。"""

    return _knowledge_base_service
