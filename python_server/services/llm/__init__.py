"""LLM 服务适配器；负责接收上下文并生成文本回复。"""

from .openai_compatible import LLMService, get_llm_service

__all__ = ["LLMService", "get_llm_service"]
