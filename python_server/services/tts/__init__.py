"""TTS 服务边界；具体供应商适配器将在接入语音合成时实现。"""

from .provider import TTSProvider

__all__ = ["TTSProvider"]
