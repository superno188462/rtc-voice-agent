"""用本地 16 kHz PCM WAV 文件验证豆包 Seed ASR 2.0 Plan WebSocket 接入。"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import struct
import sys
import uuid
import wave
from pathlib import Path

import websockets
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"


def _header(message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    """构造协议四字节头。"""

    return bytes((0x11, (message_type << 4) | flags, (serialization << 4) | compression, 0))


def _build_frame(message_type: int, flags: int, seq: int, payload: bytes, *, is_json: bool) -> bytes:
    """构造含序号、长度和 gzip 载荷的客户端请求帧。"""

    compressed = gzip.compress(payload)
    return (
        _header(message_type, flags, 1 if is_json else 0, 1)
        + struct.pack(">iI", seq, len(compressed))
        + compressed
    )


def _start_request(seq: int, user_id: str) -> bytes:
    """生成官方示例格式的初始化帧。"""

    payload = {
        "user": {"uid": user_id},
        "audio": {
            "format": "wav",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
            "language": "zh-CN",
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "result_type": "full",
        },
    }
    return _build_frame(
        0x1, 0x1, seq, json.dumps(payload, ensure_ascii=False).encode("utf-8"), is_json=True
    )


def _audio_request(seq: int, audio: bytes, *, final: bool) -> bytes:
    """生成音频帧；最后一帧使用负序号和 NEG_WITH_SEQUENCE 标记。"""

    flags = 0x3 if final else 0x1
    return _build_frame(0x2, flags, -seq if final else seq, audio, is_json=False)


def _parse_response(data: bytes) -> dict:
    """按响应头字段解析火山二进制协议 JSON 响应。"""

    if len(data) < 4:
        raise ValueError("响应帧长度不足")
    header_size = (data[0] & 0x0F) * 4
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    offset = header_size
    if flags & 0x01:
        offset += 4
    if flags & 0x04:
        offset += 4

    code = 0
    if message_type == 0x0F:
        if len(data) < offset + 8:
            raise ValueError("ASR 错误帧格式无效")
        code = struct.unpack_from(">I", data, offset)[0]
        offset += 4
    elif message_type not in (0x9, 0xB):
        return {"message_type": message_type, "is_last": bool(flags & 0x02)}

    if len(data) < offset + 4:
        raise ValueError("ASR 响应帧缺少载荷长度")
    payload_size = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    payload = data[offset : offset + payload_size]
    if compression == 1 and payload:
        payload = gzip.decompress(payload)
    decoded = json.loads(payload.decode("utf-8")) if serialization == 1 and payload else {}
    return {
        "message_type": message_type,
        "code": code,
        "is_last": bool(flags & 0x02),
        "payload": decoded,
    }


def _get_audio(path: Path, segment_ms: int) -> tuple[bytes, int]:
    """验证 WAV 音频格式并返回原 WAV 字节和每个音频包大小。"""

    with wave.open(str(path), "rb") as wav_file:
        params = wav_file.getparams()
        if (params.nchannels, params.sampwidth, params.framerate, params.comptype) != (
            1,
            2,
            16000,
            "NONE",
        ):
            raise ValueError("音频必须是 16 kHz、16-bit、单声道 PCM WAV；可用 ffmpeg 转换")
    wav_bytes = path.read_bytes()
    bytes_per_second = 16000 * 2
    return wav_bytes, bytes_per_second * segment_ms // 1000


def _transcript_events(payload: dict) -> list[tuple[str, str]]:
    """提取服务端 partial/final 转写内容。"""

    result = payload.get("result") or payload.get("payload", {}).get("result") or {}
    utterances = result.get("utterances") or []
    if utterances:
        return [
            ("FINAL" if item.get("definite") else "PARTIAL", item["text"])
            for item in utterances
            if isinstance(item.get("text"), str) and item["text"].strip()
        ]
    text = result.get("text")
    if isinstance(text, str) and text.strip():
        return [("FINAL" if result.get("definite") else "PARTIAL", text)]
    return []


async def run_test(audio_path: Path, segment_ms: int) -> int:
    """连接 ASR Plan 端点、流式推送 WAV 并打印识别结果。"""

    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)
    api_key = os.getenv("ARK_ASR_API_KEY", "").strip()
    if not api_key:
        print(f"未配置 ARK_ASR_API_KEY，请填写 {env_path}")
        return 2

    url = os.getenv("ARK_ASR_FILE_WS_URL", DEFAULT_URL).strip() or DEFAULT_URL
    resource_id = os.getenv("ARK_ASR_RESOURCE_ID", DEFAULT_RESOURCE_ID).strip()
    audio_bytes, segment_size = _get_audio(audio_path, segment_ms)
    if not audio_bytes:
        print("音频文件为空")
        return 2

    request_id = str(uuid.uuid4())
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Connect-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    sequence = 1
    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            proxy=None,
            open_timeout=10,
            close_timeout=3,
            ping_interval=20,
            max_size=4 * 1024 * 1024,
        ) as connection:
            await connection.send(_start_request(sequence, "seed-asr-test"))
            sequence += 1
            print("ASR WebSocket 已连接，开始发送音频。")

            async def send_audio() -> None:
                nonlocal sequence
                chunks = [
                    audio_bytes[index : index + segment_size]
                    for index in range(0, len(audio_bytes), segment_size)
                ]
                for index, chunk in enumerate(chunks):
                    is_final = index == len(chunks) - 1
                    await connection.send(_audio_request(sequence, chunk, final=is_final))
                    sequence += 1
                    if not is_final:
                        await asyncio.sleep(segment_ms / 1000)

            async def receive_results() -> None:
                status_reported = False
                async for message in connection:
                    if not isinstance(message, bytes):
                        continue
                    response = _parse_response(message)
                    if response["code"]:
                        raise RuntimeError(
                            f"ASR 返回错误码 {response['code']}: {response['payload']}"
                        )
                    events = _transcript_events(response["payload"])
                    if events:
                        for kind, text in events:
                            print(f"[{kind}] {text}")
                    elif response["payload"] and not status_reported:
                        print("ASR 初始化/状态响应已收到。")
                        status_reported = True
                    if response["is_last"]:
                        return

            sender = asyncio.create_task(send_audio())
            receiver = asyncio.create_task(receive_results())
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = task.exception()
                if error:
                    raise error
            if receiver in done and sender not in done:
                await sender
            print("ASR 测试完成。")
            return 0
    except websockets.exceptions.InvalidStatus as exc:
        response = exc.response
        body = getattr(response, "body", b"")
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        print(f"ASR 握手失败 HTTP {response.status_code}: {str(body)[:500]}")
        print(f"X-Tt-Logid: {response.headers.get('X-Tt-Logid', 'unavailable')}")
        return 1
    except Exception as exc:
        print(f"ASR 测试失败：{type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    """解析 CLI 参数并启动异步测试。"""

    parser = argparse.ArgumentParser(description="豆包 Seed ASR 2.0 Plan WebSocket 测试")
    parser.add_argument("audio", type=Path, help="16 kHz / 16-bit / mono PCM WAV 文件")
    parser.add_argument("--segment-ms", type=int, default=200, choices=(100, 200))
    args = parser.parse_args()
    if not args.audio.is_file():
        parser.error(f"找不到音频文件：{args.audio}")
    return asyncio.run(run_test(args.audio, args.segment_ms))


if __name__ == "__main__":
    raise SystemExit(main())
