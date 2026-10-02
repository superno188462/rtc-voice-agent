"""开发阶段的空 RAG Mock；保留 Agent 到 RAG 的调用边界，不访问外部服务。"""

from __future__ import annotations


class MockRAGService:
    """模拟一次无命中检索，让 Agent 在没有知识库资料时仍可调用 LLM。"""

    async def search(self, query: str) -> str:
        """返回空参考资料；query 仅用于匹配真实 RAG 服务的接口契约。"""

        del query
        return ""


mock_rag_service = MockRAGService()
