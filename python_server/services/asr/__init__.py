"""ASR 服务适配器；负责语音输入转写，不承担 Agent 对话编排。"""

from .client import ASRClient, GatewayASRClient, VolcEngineASRClient, create_asr_client
from .volc_seed import (
    build_audio_frame,
    build_start_request,
    extract_transcript_events,
    parse_server_frame,
)

__all__ = [
    "ASRClient",
    "GatewayASRClient",
    "VolcEngineASRClient",
    "create_asr_client",
    "build_audio_frame",
    "build_start_request",
    "extract_transcript_events",
    "parse_server_frame",
]
