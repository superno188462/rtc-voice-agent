"""TTS 服务的供应商无关契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class TTSProvider(Protocol):
    """将文本合成为可流式播放的音频数据。"""

    def synthesize(self, text: str, *, voice_id: str | None = None) -> AsyncIterator[bytes]:
        """按顺序产出音频块；当前项目尚未装配具体 TTS 实现。"""

