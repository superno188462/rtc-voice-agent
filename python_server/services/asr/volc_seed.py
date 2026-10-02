"""火山豆包流式 ASR 2.0 WebSocket 协议适配器。"""

from __future__ import annotations

import gzip
import json
import struct


def _header(message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    """构造火山二进制协议固定四字节头。"""

    return bytes((0x11, (message_type << 4) | flags, (serialization << 4) | compression, 0))


def _frame(
    message_type: int,
    flags: int,
    payload: bytes,
    *,
    json_payload: bool,
    sequence: int,
) -> bytes:
    """构造带长度及 gzip 压缩载荷的客户端消息。"""

    packed = gzip.compress(payload) if payload else b""
    return (
        _header(message_type, flags, 1 if json_payload else 0, 1)
        + struct.pack(">i", sequence)
        + struct.pack(">I", len(packed))
        + packed
    )


def build_start_request(
    user_id: str, sequence: int = 1, *, end_window_size: int = 800
) -> bytes:
    """生成 ASR 初始化帧；固定音频格式，并开启 VAD 判停后的二遍识别。"""

    payload = {
        "user": {"uid": user_id[:128]},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
        "request": {
            "model_name": "bigmodel",
            # bigmodel_async 只有启用二遍识别才会在 VAD 判停后给出句级定稿。
            "enable_nonstream": True,
            "end_window_size": end_window_size,
            "show_utterances": True,
            "enable_itn": True,
            "enable_punc": True,
        },
    }
    return _frame(
        0x1,
        0x1,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        json_payload=True,
        sequence=sequence,
    )


def build_audio_frame(pcm: bytes, *, sequence: int, last: bool = False) -> bytes:
    """封装一帧 16-bit mono PCM；last 用于正常结束 ASR 输入流。"""

    return _frame(
        0x2,
        0x3 if last else 0x1,
        pcm,
        json_payload=False,
        sequence=-sequence if last else sequence,
    )


def parse_server_frame(frame: bytes) -> dict | None:
    """解码火山服务端帧，返回 JSON 结果或标准化服务端错误。"""

    if len(frame) < 8:
        raise ValueError("ASR_FRAME_TOO_SHORT")
    header_size = (frame[0] & 0x0F) * 4
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    serialization = frame[2] >> 4
    compression = frame[2] & 0x0F
    offset = header_size
    if flags & 0x01:
        offset += 4
    if flags & 0x04:
        offset += 4
    if message_type == 0x0F:
        if len(frame) < offset + 8:
            raise ValueError("ASR_ERROR_FRAME_INVALID")
        code = struct.unpack_from(">I", frame, offset)[0]
        size = struct.unpack_from(">I", frame, offset + 4)[0]
        payload = frame[offset + 8 : offset + 8 + size]
        if compression == 1:
            payload = gzip.decompress(payload)
        try:
            detail = json.loads(payload.decode("utf-8")) if payload else {}
        except (ValueError, UnicodeDecodeError):
            detail = {}
        return {"type": "error", "code": f"ASR_UPSTREAM_{code}", "message": detail.get("message", "")}
    if message_type not in (0x9, 0xB):
        return None
    if len(frame) < offset + 4:
        raise ValueError("ASR_RESPONSE_FRAME_INVALID")
    size = struct.unpack_from(">I", frame, offset)[0]
    payload = frame[offset + 4 : offset + 4 + size]
    if compression == 1:
        payload = gzip.decompress(payload)
    if serialization == 1 and payload:
        return json.loads(payload.decode("utf-8"))
    return None


def extract_transcript_events(payload: dict) -> list[dict[str, str]]:
    """将服务端 utterance 转成带稳定句子标识的 partial/final 事件。"""

    result = payload.get("result") or payload.get("payload", {}).get("result") or {}
    utterances = result.get("utterances") or []
    events: list[dict[str, str]] = []
    if utterances:
        for utterance in utterances:
            text = utterance.get("text")
            if isinstance(text, str) and text.strip():
                event = {
                    "type": "asr_final" if utterance.get("definite") else "asr_partial",
                    "text": text,
                }
                start_time = utterance.get("start_time")
                end_time = utterance.get("end_time")
                if start_time is not None and end_time is not None:
                    event["utterance_id"] = f"{start_time}:{end_time}"
                events.append(event)
        return events
    text = result.get("text")
    if isinstance(text, str) and text.strip():
        definite = bool(result.get("definite") or payload.get("is_final"))
        events.append({"type": "asr_final" if definite else "asr_partial", "text": text})
    return events
