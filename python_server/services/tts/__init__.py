"""TTS 服务边界与供应商适配器。"""

from .provider import TTSProvider, TTSProviderError
from .volcengine import VolcEngineTTSProvider, create_tts_provider

__all__ = ["TTSProvider", "TTSProviderError", "VolcEngineTTSProvider", "create_tts_provider"]
