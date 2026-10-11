"""RAG 检索能力；Agent 与 Gateway 向量数据库之间保持稳定 search 接口。"""

from .knowledge_base import get_knowledge_base_service


class GatewayRAGService:
    """将 Gateway 命中结果接入 Agent 当前使用的文本上下文接口。"""

    async def search(self, query: str) -> str:
        """返回供 LLM 使用的相关知识片段；网关错误由 Agent 的 RAG 降级策略处理。"""

        return await get_knowledge_base_service().search_context(query)

    async def search_hits(self, query: str, *, top_k: int = 5) -> list[dict]:
        """返回带 metadata 与已展开父块上下文的 Gateway 子片命中。"""

        return await get_knowledge_base_service().search_with_context(query, top_k=top_k)


_gateway_rag_service = GatewayRAGService()


def get_rag_service() -> GatewayRAGService:
    """返回通过 Agent Gateway 检索的应用级 RAG 服务。"""

    return _gateway_rag_service


__all__ = ["GatewayRAGService", "get_rag_service"]
