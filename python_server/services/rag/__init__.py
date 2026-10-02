"""RAG 检索能力；Agent 当前装配空结果 Mock，真实适配器保留待后续切换。"""

from .mock import MockRAGService, mock_rag_service
from .volc_knowledge_base import RAGService


def get_rag_service() -> MockRAGService:
    """返回当前开发环境装配的 RAG Mock，不发起外部检索请求。"""

    return mock_rag_service

__all__ = ["MockRAGService", "RAGService", "get_rag_service"]
