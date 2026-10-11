"""知识库向量适配层，所有 Gateway 网络操作委托统一 Python SDK。"""

from __future__ import annotations

import os
from typing import Any

from gateway_client import GatewayVectorStoreError

from services.gateway import create_gateway_client


class GatewayVectorStore:
    """维持知识库现有接口，同时由 SDK 执行 Gateway 请求。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        collection: str | None = None,
        *,
        timeout: float | None = None,
        transport: Any = None,
        embedding_model: str = "",
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._collection = collection
        self.timeout = timeout
        self.transport = transport
        self.embedding_model = embedding_model

    @property
    def collection(self) -> str:
        return (self._collection if self._collection is not None else os.getenv("GATEWAY_COLLECTION", "")).strip()

    def ensure_configured(self) -> None:
        if not (self._base_url or os.getenv("GATEWAY_BASE_URL", "").strip()):
            raise GatewayVectorStoreError("VECTOR_STORE_NOT_CONFIGURED")
        if not (self._api_key if self._api_key is not None else os.getenv("GATEWAY_API_KEY", "")):
            raise GatewayVectorStoreError("VECTOR_STORE_NOT_CONFIGURED")
        if not self.collection:
            raise GatewayVectorStoreError("VECTOR_STORE_NOT_CONFIGURED")

    def _sdk_store(self):
        self.ensure_configured()
        return create_gateway_client(
            base_url=self._base_url,
            api_key=self._api_key,
            collection=self.collection,
            embedding_model=self.embedding_model,
            vector_timeout=self.timeout or float(os.getenv("GATEWAY_VECTOR_TIMEOUT_SECONDS", "15")),
            transport=self.transport,
        ).vectors

    async def list_collections(self) -> list[dict[str, Any]]:
        return await self._sdk_store().list_collections()

    async def ensure_collection(self, *, model: str, description: str, chunk_size: int) -> dict[str, Any]:
        return await self._sdk_store().ensure_collection(
            model=model,
            description=description,
            chunk_size=chunk_size,
        )

    async def add_vectors(self, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._sdk_store().add_vectors(vectors)

    async def delete(self, ids: list[str]) -> dict[str, Any]:
        return await self._sdk_store().delete_vectors(ids)

    async def similarity_search(
        self,
        query: str,
        *,
        k: int = 5,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._sdk_store().similarity_search(query, k=k, metadata=metadata)

    async def similarity_search_with_context(
        self,
        query: str,
        *,
        k: int = 5,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """检索子片并通过 SDK 按 parent_id 补回父块正文。"""

        self.ensure_configured()
        return await create_gateway_client(
            base_url=self._base_url,
            api_key=self._api_key,
            collection=self.collection,
            embedding_model=self.embedding_model,
            vector_timeout=self.timeout or float(os.getenv("GATEWAY_VECTOR_TIMEOUT_SECONDS", "15")),
            transport=self.transport,
        ).similarity_search_with_context(query, k=k, metadata=metadata)


_gateway_vector_store = GatewayVectorStore()


def get_gateway_vector_store() -> GatewayVectorStore:
    """返回应用级适配器；环境变量在首次 Gateway 调用时解析。"""

    return _gateway_vector_store
