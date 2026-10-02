"""火山知识库检索服务。"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from volcengine.Credentials import Credentials
from volcengine.auth.SignerV4 import SignerV4
from volcengine.base.Request import Request


class RAGService:
    def __init__(self) -> None:
        self.domain = os.getenv(
            "RAG_KNOWLEDGE_BASE_DOMAIN",
            "api-knowledgebase.mlp.cn-beijing.volces.com",
        )
        self.scheme = os.getenv("RAG_SCHEME", "http")

    async def search(self, query: str) -> str:
        """检索知识库并整理为可注入 LLM 的参考文本。"""
        access_key = os.getenv("RAG_ACCESS_KEY", "")
        secret_key = os.getenv("RAG_SECRET_KEY", "")
        collection = os.getenv("RAG_COLLECTION_NAME", "")
        project = os.getenv("RAG_PROJECT_NAME", "default")
        if not access_key or not secret_key or not collection or not query.strip():
            return ""
        payload: dict[str, Any] = {
            "project": project,
            "name": collection,
            "query": query,
            "limit": int(os.getenv("RAG_LIMIT", "5")),
            "pre_processing": {
                "need_instruction": True,
                "return_token_usage": True,
                "messages": [{"role": "user", "content": query}]
            },
            "dense_weight": float(os.getenv("RAG_DENSE_WEIGHT", "0.5")),
            "post_processing": {
                "get_attachment_link": True,
                "rerank_only_chunk": False,
                "rerank_switch": False,
            },
        }
        request = Request()
        request.set_shema(self.scheme)
        request.set_method("POST")
        request.set_connection_timeout(10)
        request.set_socket_timeout(15)
        request.set_headers({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.domain,
        })
        request.set_host(self.domain)
        request.set_path("/api/knowledge/collection/search_knowledge")
        request.set_body(json.dumps(payload, ensure_ascii=False))
        SignerV4.sign(
            request,
            Credentials(access_key, secret_key, "air", "cn-north-1"),
        )
        url = f"{self.scheme}://{self.domain}{request.path}"
        headers = dict(request.headers)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(url, headers=headers, content=request.body)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"RAG request failed ({response.status_code}): {response.text}"
                )
            result = response.json()
        return self._format_result(result)

    @staticmethod
    def _format_result(result: Any) -> str:
        texts: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in {"content", "text", "chunk"} and isinstance(item, str):
                        if item.strip() and item not in texts:
                            texts.append(item.strip())
                    elif isinstance(item, (dict, list)):
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(result)
        return "\n\n".join(texts)


rag_service = RAGService()


def get_rag_service() -> RAGService:
    return rag_service
