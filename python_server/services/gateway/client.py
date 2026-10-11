"""Gateway SDK 的统一装配入口。业务适配层不直接构造 SDK 客户端。"""

from __future__ import annotations

import os
from typing import Any

from gateway_client import GatewayClient


def create_gateway_client(
    *,
    collection: str | None = None,
    embedding_model: str = "",
    api_key: str | None = None,
    base_url: str | None = None,
    vector_timeout: float | None = None,
    context_timeout: float | None = None,
    transport: Any = None,
) -> GatewayClient:
    """Build an explicit SDK client from backend configuration.

    Context and vector APIs share the project's Gateway API root and project key.
    Collection remains an explicit per-use choice for vector operations.
    """

    configured_url = base_url or os.getenv("GATEWAY_BASE_URL", "")
    configured_key = api_key if api_key is not None else os.getenv("GATEWAY_API_KEY", "")
    selected_collection = collection or os.getenv("GATEWAY_COLLECTION", "")

    if vector_timeout is None:
        vector_timeout = float(os.getenv("GATEWAY_VECTOR_TIMEOUT_SECONDS", "15"))
    if context_timeout is None:
        context_timeout = float(os.getenv("GATEWAY_CONTEXT_TIMEOUT_SECONDS", "15"))

    return GatewayClient(
        base_url=configured_url.strip().rstrip("/"),
        api_key=configured_key,
        collection=selected_collection,
        embedding_model=embedding_model,
        vector_timeout=vector_timeout,
        context_timeout=context_timeout,
        transport=transport,
    )
