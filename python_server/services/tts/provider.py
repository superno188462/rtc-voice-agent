"""TTS 服务的供应商无关契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class TTSProvider(Protocol):
    """将文本合成为可流式播放的音频数据。"""

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        audio_format: str = "mp3",
    ) -> AsyncIterator[bytes]:
        """按顺序产出音频块。"""


class TTSProviderError(RuntimeError):
    """供应商返回错误或配置不完整；错误消息不包含凭据和文本。"""
