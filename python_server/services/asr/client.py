"""统一实时 ASR 流接口及网关、火山上游适配器。"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Protocol
from urllib.parse import urlsplit

import websockets

from .volc_seed import (
    build_audio_frame,
    build_start_request,
    extract_transcript_events,
    parse_server_frame,
)


class ASRClient(Protocol):
    """统一实时识别客户端；音频输入固定为 16 kHz 单声道 S16LE PCM。"""

    async def connect(self) -> None:
        """连接上游并发送识别初始化消息；失败时抛出连接异常。"""

    async def send_audio(self, pcm: bytes) -> None:
        """发送一块 PCM 音频。"""

    async def finish(self) -> None:
        """通知上游音频输入结束；重复调用应安全。"""

    async def receive_events(self):
        """持续产出项目统一的识别事件字典。"""

    async def close(self) -> None:
        """关闭上游连接并释放资源。"""


class VolcEngineASRClient:
    """火山 Seed ASR 适配器，封装厂商鉴权头和二进制帧协议。"""

    def __init__(self, url: str, api_key: str, resource_id: str, user_id: str) -> None:
        self.url = url
        self.api_key = api_key
        self.resource_id = resource_id
        self.user_id = user_id
        self.request_id = str(uuid.uuid4())
        self.sequence = 1
        self.end_window_size = int(os.getenv("ARK_ASR_END_WINDOW_SIZE", "800"))
        self.connection: Any | None = None
        self.finished = False

    async def connect(self) -> None:
        """建立火山 WebSocket，并发送 Seed ASR 初始化帧。"""

        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": self.request_id,
            "X-Api-Connect-Id": self.request_id,
            "X-Api-Sequence": "-1",
        }
        target = urlsplit(self.url)
        print(
            f"[asr] upstream_connect_start provider=volcengine request_id={self.request_id} "
            f"target={target.scheme}://{target.netloc}{target.path}"
        )
        self.connection = await websockets.connect(
            self.url,
            additional_headers=headers,
            # 本机调试时不让 websockets 自动套用系统代理；线上通常没有系统代理配置。
            proxy=None,
            open_timeout=10,
            close_timeout=3,
            ping_interval=20,
            max_size=4 * 1024 * 1024,
        )
        logid = self.connection.response.headers.get("X-Tt-Logid", "unavailable")
        print(
            f"[asr] upstream_connected provider=volcengine request_id={self.request_id} "
            f"resource_id={self.resource_id} logid={logid}"
        )
        self.sequence = 1
        await self.connection.send(
            build_start_request(
                self.user_id,
                self.sequence,
                end_window_size=max(200, min(self.end_window_size, 5000)),
            )
        )
        self.sequence += 1

    async def send_audio(self, pcm: bytes) -> None:
        """以火山二进制协议发送未压缩的 PCM 内容。"""

        if pcm and self.connection and not self.finished:
            await self.connection.send(build_audio_frame(pcm, sequence=self.sequence))
            self.sequence += 1

    async def finish(self) -> None:
        """发送火山协议的负序号结束音频帧。"""

        if self.finished:
            return
        self.finished = True
        if self.connection:
            await self.connection.send(
                build_audio_frame(b"", sequence=self.sequence, last=True)
            )

    async def receive_events(self):
        """解析火山二进制响应并输出 asr_partial/asr_final/error 事件。"""

        if not self.connection:
            return
        async for frame in self.connection:
            if not isinstance(frame, bytes):
                continue
            payload = parse_server_frame(frame)
            if not payload:
                continue
            if payload.get("type") == "error":
                yield payload
                return
            for event in extract_transcript_events(payload):
                yield event

    async def close(self) -> None:
        """关闭火山 WebSocket。"""

        if self.connection:
            await self.connection.close()
            self.connection = None


class GatewayASRClient:
    """网关适配器；使用 start JSON、原始 PCM 二进制帧和 end JSON。"""

    def __init__(self, url: str, model: str) -> None:
        self.url = url
        self.model = model
        self.connection: Any | None = None
        self.finished = False
        self._seen_final_ids: set[str] = set()
        self._finalized_text = ""
        self._last_final_text = ""
        self._final_sequence = 0

    async def connect(self) -> None:
        """连接网关，并按项目网关契约发送 ASR 初始化消息。"""

        target = urlsplit(self.url)
        print(
            f"[asr] upstream_connect_start provider=gateway target="
            f"{target.scheme}://{target.netloc}{target.path} model={self.model}"
        )
        headers = {}
        api_key = os.getenv("ASR_GATEWAY_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.connection = await websockets.connect(
            self.url,
            additional_headers=headers,
            open_timeout=10,
            close_timeout=3,
            ping_interval=20,
            max_size=4 * 1024 * 1024,
        )
        await self.connection.send(
            json.dumps(
                {
                    "type": "start",
                    "model": self.model,
                    "audio": {
                        "format": "pcm",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bits": 16,
                    },
                },
                ensure_ascii=False,
            )
        )

    async def send_audio(self, pcm: bytes) -> None:
        """将 RTC PCM 音频块原样转发给网关。"""

        if pcm and self.connection and not self.finished:
            await self.connection.send(pcm)

    async def finish(self) -> None:
        """按网关契约发送 end 控制消息。"""

        if self.finished:
            return
        self.finished = True
        if self.connection:
            await self.connection.send(json.dumps({"type": "end"}))

    async def receive_events(self):
        """解析网关 JSON 事件并规范化为项目统一事件。"""

        if not self.connection:
            return
        async for message in self.connection:
            try:
                payload = json.loads(message)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            event_type = payload.get("type")
            if event_type in ("transcript.partial", "partial", "asr_partial"):
                payload["type"] = "asr_partial"
            elif event_type in ("transcript.final", "final", "asr_final"):
                payload["type"] = "asr_final"
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                text = text.strip()
                provider_event_id = payload.get("utterance_id") or payload.get("id")
                if provider_event_id is not None:
                    provider_event_id = str(provider_event_id)
                    if provider_event_id in self._seen_final_ids:
                        continue
                    self._seen_final_ids.add(provider_event_id)

                # Some gateways send the whole session transcript on every final event.
                # Convert that snapshot into only the newly finalized suffix before the
                # Agent submits it as a new user turn.
                previous = self._finalized_text
                if provider_event_id is None and previous and text.startswith(previous):
                    text = text[len(previous) :].strip()
                    if not text:
                        continue
                    self._finalized_text = payload["text"].strip()
                elif provider_event_id is None and previous and previous.startswith(text):
                    # A stale cumulative snapshot must not be replayed as a new turn.
                    continue
                elif text == self._last_final_text and provider_event_id is None:
                    # Fallback for gateways that repeat the same final frame without an ID.
                    continue
                else:
                    self._finalized_text = (
                        f"{previous}\n{text}" if previous else text
                    )

                self._last_final_text = text
                if provider_event_id is None:
                    self._final_sequence += 1
                    payload["utterance_id"] = f"gateway-final-{self._final_sequence}"
                payload["text"] = text
            elif event_type == "error":
                payload.setdefault("code", "ASR_UPSTREAM_FAILED")
            elif event_type in ("ready", "asr_ready"):
                payload["type"] = "asr_ready"
            else:
                continue
            yield payload

    async def close(self) -> None:
        """关闭网关 WebSocket。"""

        if self.connection:
            await self.connection.close()
            self.connection = None


def create_asr_client(user_id: str) -> ASRClient:
    """按 ASR_PROVIDER 配置创建一个上游适配器。"""

    provider = os.getenv("ASR_PROVIDER", "volcengine").strip().lower()
    model = os.getenv("ASR_MODEL", "doubao-seed-asr-2.0").strip()
    configured_url = os.getenv("ASR_WS_URL", "").strip()
    if provider == "gateway":
        url = configured_url or os.getenv("ASR_GATEWAY_WS_URL", "").strip()
        if not url:
            raise ValueError("ASR_GATEWAY_WS_URL_REQUIRED")
        return GatewayASRClient(url, model)
    if provider == "volcengine":
        api_key = os.getenv("ARK_ASR_API_KEY", "").strip()
        if not api_key:
            raise ValueError("ARK_ASR_API_KEY_REQUIRED")
        url = configured_url or os.getenv(
            "ARK_ASR_WS_URL",
            "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async",
        ).strip()
        resource_id = os.getenv(
            "ARK_ASR_RESOURCE_ID", "volc.seedasr.sauc.duration"
        ).strip()
        return VolcEngineASRClient(url, api_key, resource_id, user_id)
    raise ValueError("ASR_PROVIDER_UNSUPPORTED")
