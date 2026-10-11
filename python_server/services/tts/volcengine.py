"""Volcengine Agent Plan 单向流式 TTS 适配器。"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import AsyncIterator

import httpx

from .provider import TTSProviderError


class VolcEngineTTSProvider:
    """通过火山 HTTP Chunked TTS 接口取得 MP3 音频块。"""

    def __init__(self) -> None:
        self.api_key = os.getenv("VOLC_TTS_API_KEY", "").strip()
        self.resource_id = os.getenv("VOLC_TTS_RESOURCE_ID", "seed-tts-2.0").strip()
        self.speaker = os.getenv("VOLC_TTS_SPEAKER", "").strip()
        self.url = os.getenv(
            "VOLC_TTS_URL", "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
        ).strip()
        self.timeout_seconds = float(os.getenv("VOLC_TTS_TIMEOUT_SECONDS", "45"))

    def validate(self) -> None:
        if not self.api_key:
            raise TTSProviderError("TTS_NOT_CONFIGURED")
        if not self.speaker:
            raise TTSProviderError("TTS_SPEAKER_NOT_CONFIGURED")

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        audio_format: str = "mp3",
    ) -> AsyncIterator[bytes]:
        self.validate()
        if not text.strip():
            raise TTSProviderError("TTS_EMPTY_TEXT")

        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        payload = {
            "req_params": {
                "text": text,
                "speaker": voice_id or self.speaker,
                "audio_params": {"format": audio_format, "sample_rate": 24000},
            }
        }
        timeout = httpx.Timeout(self.timeout_seconds, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", self.url, headers=headers, json=payload) as response:
                    if response.is_error:
                        raise TTSProviderError(f"TTS_UPSTREAM_HTTP_{response.status_code}")
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise TTSProviderError("TTS_INVALID_RESPONSE") from exc
                        code = event.get("code", 0)
                        audio = event.get("data")
                        if audio:
                            try:
                                chunk = base64.b64decode(audio, validate=True)
                            except (ValueError, TypeError) as exc:
                                raise TTSProviderError("TTS_INVALID_AUDIO_CHUNK") from exc
                            if chunk:
                                yield chunk
                        if isinstance(code, int) and code not in (0, 20000000):
                            raise TTSProviderError("TTS_UPSTREAM_ERROR")
        except httpx.TimeoutException as exc:
            raise TTSProviderError("TTS_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise TTSProviderError("TTS_UPSTREAM_UNAVAILABLE") from exc


def create_tts_provider() -> VolcEngineTTSProvider:
    """当前项目固定使用火山 TTS；ASR provider 独立配置。"""

    return VolcEngineTTSProvider()
