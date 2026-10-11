"""Long-term-memory vector retrieval and synchronization through Agent Gateway."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from services.auth.database import get_session_factory
from services.context_gateway.models import MemoryVectorBackfill, MemoryVectorIndex
from services.rag.gateway_vector_store import GatewayVectorStore, GatewayVectorStoreError

logger = logging.getLogger(__name__)
_store: GatewayVectorStore | None = None
_collection_ready = False


def _configured_values() -> tuple[str, str, str, str]:
    return (
        os.getenv("GATEWAY_BASE_URL", "").strip().rstrip("/"),
        os.getenv("GATEWAY_API_KEY", ""),
        os.getenv("GATEWAY_MEMORY_COLLECTION", "rtc-agent-memory").strip(),
        os.getenv("GATEWAY_MEMORY_EMBEDDING_MODEL", "").strip(),
    )


def memory_vector_enabled() -> bool:
    """Return whether the dedicated memory collection has complete configuration."""

    base_url, api_key, collection, model = _configured_values()
    return bool(base_url and api_key and collection and model)


def get_memory_vector_store() -> GatewayVectorStore:
    """Return the dedicated memory collection client, separate from the RAG collection."""

    global _store
    base_url, api_key, collection, _model = _configured_values()
    if _store is None or _store.collection != collection:
        _store = GatewayVectorStore(
            base_url=base_url,
            api_key=api_key,
            collection=collection,
            timeout=float(os.getenv("GATEWAY_MEMORY_VECTOR_TIMEOUT_SECONDS", "20")),
            embedding_model=_model,
        )
    return _store


async def ensure_memory_vector_collection() -> bool:
    """Create or validate the configured memory collection; disabled config is a no-op."""

    global _collection_ready
    if not memory_vector_enabled():
        return False
    if _collection_ready:
        return True
    _base_url, _api_key, collection, model = _configured_values()
    await get_memory_vector_store().ensure_collection(
        model=model,
        description="RTC Agent 用户长期记忆语义检索；每条 Gateway Context 记忆对应一条记录。",
        chunk_size=int(os.getenv("GATEWAY_MEMORY_CHUNK_SIZE", "1000")),
    )
    _collection_ready = True
    logger.info("memory_vector collection_ready collection=%s", collection)
    return True


def _memory_id(item: dict[str, Any]) -> str | None:
    value = item.get("id", item.get("memory_id", item.get("uuid")))
    return value.strip() if isinstance(value, str) and value.strip() else None


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("metadata", item.get("metadata_json"))
    return value if isinstance(value, dict) else {}


def _record(user_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    memory_id = _memory_id(item)
    content = item.get("content")
    if not memory_id or not isinstance(content, str) or not content.strip():
        return None
    source = _metadata(item)
    scope = source.get("scope", "global")
    if scope not in {"global", "project"}:
        scope = "global"
    metadata = {
        "external_user_id": user_id,
        "memory_id": memory_id,
        "scope": scope,
        "subject_project_id": source.get("subject_project_id") if scope == "project" else None,
        "project_name": source.get("project_name") if scope == "project" else None,
        "memory_type": source.get("memory_type", "semantic"),
        "memory_role": source.get("memory_role", "fact"),
        "status": source.get("status", "active"),
        "is_active": source.get("is_active", True),
        "valid_until": source.get("valid_until"),
        "occurred_at": source.get("occurred_at"),
        "created_at": source.get("created_at"),
        "updated_at": source.get("updated_at"),
        "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
        "schema_version": source.get("schema_version", 1),
    }
    return {
        "id": f"ltm:{memory_id}",
        "content": content.strip(),
        "metadata": metadata,
    }


def _sha256(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def sync_memory_entry(user_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """Upsert one newly created or changed Context memory by its stable Gateway ID."""

    if not await ensure_memory_vector_collection():
        return None
    record = _record(user_id, item)
    if record is None:
        return None
    store = get_memory_vector_store()
    result = await store.add_vectors([record])
    await _save_index_state(user_id, record)
    return result


async def sync_user_memories(
    user_id: str,
    memories: list[dict[str, Any]],
    *,
    listing_complete: bool,
) -> int:
    """Upsert only new/changed entries and remove stale vectors after a complete listing.

    The local index table stores only Gateway IDs and hashes, never memory text.
    """

    if not await ensure_memory_vector_collection():
        return 0
    _base_url, _api_key, collection, _model = _configured_values()
    factory = get_session_factory()
    async with factory() as session:
        current: dict[str, dict[str, Any]] = {}
        for item in memories:
            memory_id = _memory_id(item)
            record = _record(user_id, item)
            if memory_id and record is not None:
                current[memory_id] = record
        rows = list((await session.scalars(select(MemoryVectorIndex).where(
            MemoryVectorIndex.user_id == user_id,
            MemoryVectorIndex.collection == collection,
        ))).all())
        known = {row.memory_id: row for row in rows}

    pending: list[tuple[str, dict[str, Any], str, str]] = []
    for memory_id, record in current.items():
        if record is None:
            continue
        content_hash = _sha256(record["content"])
        metadata_hash = _sha256(record["metadata"])
        row = known.get(memory_id)
        if row is None or row.content_sha256 != content_hash or row.metadata_sha256 != metadata_hash:
            pending.append((memory_id, record, content_hash, metadata_hash))

    store = get_memory_vector_store()
    if pending:
        await store.add_vectors([record for _memory_id_value, record, _content, _metadata_hash in pending])
        for memory_id, record, content_hash, metadata_hash in pending:
            await _save_index_state(user_id, record, content_hash, metadata_hash)
    indexed = len(pending)

    if listing_complete:
        stale_ids = [memory_id for memory_id in known if memory_id not in current]
        if stale_ids:
            await store.delete([f"ltm:{memory_id}" for memory_id in stale_ids])
            async with factory.begin() as session:
                await session.execute(delete(MemoryVectorIndex).where(
                    MemoryVectorIndex.user_id == user_id,
                    MemoryVectorIndex.collection == collection,
                    MemoryVectorIndex.memory_id.in_(stale_ids),
                ))
    return indexed


async def _save_index_state(
    user_id: str,
    record: dict[str, Any],
    content_hash: str | None = None,
    metadata_hash: str | None = None,
) -> None:
    _base_url, _api_key, collection, _model = _configured_values()
    memory_id = str(record["metadata"]["memory_id"])
    values = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "collection": collection,
        "memory_id": memory_id,
        "content_sha256": content_hash or _sha256(record["content"]),
        "metadata_sha256": metadata_hash or _sha256(record["metadata"]),
    }
    factory = get_session_factory()
    async with factory.begin() as session:
        statement = insert(MemoryVectorIndex).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "collection", "memory_id"],
            set_={
                "content_sha256": statement.excluded.content_sha256,
                "metadata_sha256": statement.excluded.metadata_sha256,
                "updated_at": statement.excluded.updated_at,
            },
        )
        await session.execute(statement)


async def _memory_backfill_completed(user_id: str, collection: str) -> bool:
    factory = get_session_factory()
    async with factory() as session:
        marker_id = await session.scalar(
            select(MemoryVectorBackfill.id).where(
                MemoryVectorBackfill.user_id == user_id,
                MemoryVectorBackfill.collection == collection,
            )
        )
    return marker_id is not None


async def _mark_memory_backfill_completed(user_id: str, collection: str) -> None:
    factory = get_session_factory()
    values = {"id": str(uuid.uuid4()), "user_id": user_id, "collection": collection}
    async with factory.begin() as session:
        statement = insert(MemoryVectorBackfill).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=["user_id", "collection"]
        )
        await session.execute(statement)


async def _backfill_legacy_memories(
    context_client: Any, user_id: str, collection: str
) -> int:
    """Import legacy Context memories once; normal searches then read vectors only."""

    if await _memory_backfill_completed(user_id, collection):
        return 0
    max_pages = min(20, max(1, int(os.getenv("GATEWAY_CONTEXT_MEMORY_MAX_PAGES", "5"))))
    memories: list[dict[str, Any]] = []
    listing_complete = True
    for page in range(1, max_pages + 1):
        payload = await context_client.get_memory_page(user_id, page, page_size=100)
        items = _memory_page_items(payload)
        memories.extend(
            item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        )
        has_next = _memory_page_has_next(payload, page, max_pages + 1)
        if not items or not _memory_page_has_next(payload, page, max_pages):
            if page == max_pages and has_next:
                listing_complete = False
            break

    indexed = await sync_user_memories(
        user_id, memories, listing_complete=listing_complete
    )
    if listing_complete:
        await _mark_memory_backfill_completed(user_id, collection)
    logger.info(
        "memory_vector operation=legacy_backfill collection=%s indexed=%s complete=%s",
        collection,
        indexed,
        listing_complete,
    )
    return indexed


async def search_user_memories(
    user_id: str,
    query: str,
    *,
    active_project_ids: list[str],
    top_k: int,
) -> list[dict[str, Any]]:
    """Search global and active-project memory partitions, preserving Gateway ranking."""

    if not await ensure_memory_vector_collection() or not query.strip():
        return []
    store = get_memory_vector_store()
    # Gateway QueryRequest accepts top_k from 1 through 20.
    top_k = min(20, max(1, top_k))
    filters = [{"external_user_id": user_id, "scope": "global"}]
    filters.extend(
        {"external_user_id": user_id, "scope": "project", "subject_project_id": project_id}
        for project_id in dict.fromkeys(active_project_ids)
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for metadata_filter in filters:
        hits = await store.similarity_search(query, k=top_k, metadata=metadata_filter)
        for hit in hits:
            hit_metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
            memory_id = hit_metadata.get("memory_id")
            if not isinstance(memory_id, str):
                vector_id = hit.get("id", hit.get("chunk_id"))
                memory_id = vector_id.removeprefix("ltm:") if isinstance(vector_id, str) else None
            if isinstance(memory_id, str) and memory_id not in seen:
                seen.add(memory_id)
                results.append({"memory_id": memory_id, "score": hit.get("score", hit.get("similarity"))})
    return results


async def query_user_memory_records(
    context_client: Any,
    user_id: str,
    query: str,
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    """Backfill legacy records once, then query and return Gateway vector hits directly."""

    if not memory_vector_enabled():
        raise GatewayVectorStoreError("VECTOR_STORE_NOT_CONFIGURED")
    if not query.strip():
        return []
    _base_url, _api_key, collection, _model = _configured_values()
    await _backfill_legacy_memories(context_client, user_id, collection)
    logger.info("memory_vector operation=query collection=%s top_k=%s", collection, top_k)
    hits = await get_memory_vector_store().similarity_search(
        query,
        k=min(20, max(1, top_k)),
        metadata={"external_user_id": user_id},
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        vector_metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        if vector_metadata.get("external_user_id") != user_id:
            continue
        memory_id = vector_metadata.get("memory_id")
        if not isinstance(memory_id, str):
            vector_id = hit.get("id", hit.get("chunk_id"))
            memory_id = vector_id.removeprefix("ltm:") if isinstance(vector_id, str) else None
        if not isinstance(memory_id, str) or memory_id in seen:
            continue
        metadata = dict(vector_metadata)
        if metadata.get("status") in {"inactive", "superseded", "deleted"} or metadata.get("is_active") is False:
            continue
        expires = metadata.get("valid_until")
        if isinstance(expires, str):
            try:
                expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(expiry.tzinfo):
                    continue
            except ValueError:
                pass
        seen.add(memory_id)
        content = hit.get("content", hit.get("text"))
        if not isinstance(content, str):
            continue
        metadata.pop("external_user_id", None)
        tags = hit.get("tags", metadata.get("tags", []))
        results.append({
            "id": memory_id,
            "content": content,
            "score": hit.get("score", hit.get("similarity")),
            "tags": tags if isinstance(tags, list) else [],
            "metadata": metadata,
        })
    return results


def _memory_page_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "memories", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "memories", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _memory_page_has_next(payload: Any, page: int, max_pages: int) -> bool:
    if page >= max_pages or not isinstance(payload, dict):
        return False
    if isinstance(payload.get("data"), dict):
        return _memory_page_has_next(payload["data"], page, max_pages)
    if isinstance(payload.get("has_next"), bool):
        return payload["has_next"]
    if isinstance(payload.get("total_pages"), int):
        return page < payload["total_pages"]
    if isinstance(payload.get("total"), int):
        return page * int(payload.get("page_size", 100)) < payload["total"]
    return len(_memory_page_items(payload)) >= 100


def reset_collection_cache() -> None:
    """Clear startup state when configuration is reloaded in a development process."""

    global _collection_ready, _store
    _collection_ready = False
    _store = None
