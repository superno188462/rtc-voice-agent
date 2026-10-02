"""原 Server/app.js 的 FastAPI 等价实现。"""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from services.llm import get_llm_service
from services.rag import get_rag_service
from services.agent import agent_service
from services.agent.conversation_repository import (
    TurnStateError,
    get_or_create_user_conversation,
)
from services.trace_service import mark_turn_event
from services.asr import create_asr_client
from services.context_gateway import ContextGatewayError, get_context_gateway
from tools.rtc_token import AccessToken, Privileges
from tools.util import assert_value, read_files, wrapper

BASE_DIR = Path(__file__).resolve().parent
LOCAL_SCENES_DIR = BASE_DIR / "scenes"
# 运行参数放在 python_server/.env；场景业务配置仍由 scenes/*.json 提供。
load_dotenv(BASE_DIR / ".env")
SCENES = read_files(LOCAL_SCENES_DIR, ".json")

from services.auth.database import get_db_session, get_session_factory
from services.auth.models import User
from services.auth.service import (
    LoginRateLimited,
    create_auth_session,
    get_user_by_token,
    login_user,
    register_user,
    revoke_token,
    session_ttl,
)


def configure_custom_llm_urls() -> None:
    """用生产环境公网前缀拼接 CustomLLM 回调地址。"""

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    callback_path = os.getenv("CUSTOM_LLM_PATH", "/api/chat_callback").lstrip("/")
    if not public_base_url:
        return
    for scene in SCENES.values():
        voice_chat = scene.get("VoiceChat") or {}
        config = voice_chat.get("Config") or {}
        llm_config = config.get("LLMConfig") or {}
        if llm_config.get("Mode") == "CustomLLM" and public_base_url:
            llm_config["Url"] = f"{public_base_url}/{callback_path}"


configure_custom_llm_urls()

app = FastAPI(title="RTC AIGC Python Server", version="1.0.0")
configured_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: str  # user 或 assistant
    content: str


class DebugRequest(BaseModel):
    history: Optional[List[ChatMessage]] = []
    question: str


class AgentSessionRequest(BaseModel):
    scene_id: str
    room_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)


class AgentTurnRequest(BaseModel):
    text: str = Field(description="ASR 确认后的用户转写文本。")
    turn_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="前端为单轮语音分配的唯一 ID，用于耗时关联和防止重复写入。",
    )


class AuthCredentials(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128)


def _user_payload(user: User) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "role": user.role, "permissions": []}


def _validate_request_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in configured_origins:
        raise HTTPException(status_code=403, detail={"code": "ORIGIN_NOT_ALLOWED"})


def _cookie_security() -> tuple[bool, str]:
    secure_setting = os.getenv("AUTH_COOKIE_SECURE")
    secure = (
        secure_setting.strip().lower() == "true"
        if secure_setting is not None
        else os.getenv("APP_ENV", "development").strip().lower() == "production"
    )
    same_site = os.getenv("AUTH_COOKIE_SAMESITE", "lax").strip().lower()
    if same_site not in {"lax", "strict", "none"} or (same_site == "none" and not secure):
        raise RuntimeError("AUTH_COOKIE_SAMESITE_NONE_REQUIRES_SECURE_COOKIE")
    return secure, same_site


async def require_current_user(
    request: Request, db: AsyncSession = Depends(get_db_session)
) -> User:
    _validate_request_origin(request)
    user = await get_user_by_token(db, request.cookies.get("rtc_agent_session"))
    if not user:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    return user


def _set_auth_cookie(response: JSONResponse, token: str) -> None:
    secure, same_site = _cookie_security()
    response.set_cookie(
        key="rtc_agent_session",
        value=token,
        max_age=session_ttl(),
        httponly=True,
        secure=secure,
        samesite=same_site,
        # 登录还保护兼容路由 /getScenes、/proxy 和 /debug/chat。
        path="/",
    )


@app.post("/api/auth/register", status_code=201)
async def auth_register(
    credentials: AuthCredentials,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """本地账号注册。首版不依赖网关的认证或数据库。"""
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    _validate_request_origin(request)
    if os.getenv("AUTH_ALLOW_REGISTRATION", "true").strip().lower() != "true":
        raise HTTPException(status_code=403, detail={"code": "REGISTRATION_DISABLED"})
    try:
        user = await register_user(db, credentials.username, credentials.password)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "USERNAME_TAKEN" else 422
        raise HTTPException(status_code=status, detail={"code": code}) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "USERNAME_TAKEN"}) from exc

    token, _ = await create_auth_session(db, user)
    response = JSONResponse({"user": _user_payload(user)}, status_code=201)
    _set_auth_cookie(response, token)
    return response


@app.post("/api/auth/login")
async def auth_login(
    credentials: AuthCredentials,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    _validate_request_origin(request)
    client_ip = request.client.host if request.client else "unknown"
    try:
        result = await login_user(db, credentials.username, credentials.password, client_ip)
    except LoginRateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "LOGIN_RATE_LIMITED", "message": "登录尝试过于频繁，请稍后重试"},
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except ValueError as exc:
        # 避免通过不同响应枚举账号格式与存在性。
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"}) from exc
    if not result:
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    user, token, _ = result
    response = JSONResponse({"user": _user_payload(user)})
    _set_auth_cookie(response, token)
    return response


@app.get("/api/auth/me")
async def auth_me(user: User = Depends(require_current_user)) -> dict[str, object]:
    return {"user": _user_payload(user)}


@app.post("/api/auth/logout")
async def auth_logout(
    request: Request, db: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    _validate_request_origin(request)
    await revoke_token(db, request.cookies.get("rtc_agent_session"))
    secure, same_site = _cookie_security()
    response = JSONResponse({"logged_out": True})
    response.delete_cookie(
        "rtc_agent_session",
        path="/",
        httponly=True,
        secure=secure,
        samesite=same_site,
    )
    return response


@app.websocket("/api/agent/sessions/{session_id}/asr")
async def agent_asr_stream(websocket: WebSocket, session_id: str) -> None:
    """代理浏览器 RTC PCM 与配置的 ASR 上游，并返回统一识别事件。

    浏览器接口保持稳定；ASR_PROVIDER 选择网关或火山适配器，统一输出 asr_partial、
    asr_final、error 事件。输入音频为 16 kHz、16-bit、单声道 PCM 二进制帧。
    """

    session = await agent_service.get_session(session_id)
    if not session:
        await websocket.close(code=4404, reason="SESSION_NOT_FOUND")
        return
    origin = websocket.headers.get("origin")
    if origin and origin not in configured_origins:
        await websocket.close(code=4403, reason="ORIGIN_NOT_ALLOWED")
        return
    try:
        factory = get_session_factory()
        async with factory() as db:
            user = await get_user_by_token(
                db, websocket.cookies.get("rtc_agent_session")
            )
    except Exception:
        await websocket.close(code=1011, reason="AUTH_DATABASE_UNAVAILABLE")
        return
    if not user:
        await websocket.close(code=4401, reason="AUTH_REQUIRED")
        return
    if session.account_id != user.id:
        await websocket.close(code=4403, reason="SESSION_FORBIDDEN")
        return
    await websocket.accept()
    try:
        asr_client = create_asr_client(session.user_id)
    except ValueError as exc:
        error_code = str(exc)
        await websocket.send_json(
            {"type": "error", "code": "ASR_NOT_CONFIGURED" if error_code.endswith("_REQUIRED") else error_code}
        )
        await websocket.close(code=1011, reason="ASR_NOT_CONFIGURED")
        return

    provider = os.getenv("ASR_PROVIDER", "volcengine").strip().lower()
    phase = "upstream_connect"
    pending_turn_ids: list[str] = []
    partial_turn_ids: set[str] = set()
    try:
        await asr_client.connect()
        phase = "send_start_request"
        await websocket.send_json({"type": "asr_ready"})
        phase = "streaming"

        async def receive_provider_results() -> None:
            async for event in asr_client.receive_events():
                event_type = event.get("type")
                turn_id = None
                if event_type == "asr_partial":
                    turn_id = next(
                        (candidate for candidate in pending_turn_ids if candidate not in partial_turn_ids),
                        None,
                    )
                    if turn_id:
                        partial_turn_ids.add(turn_id)
                        mark_turn_event(turn_id, "asr_first_partial")
                elif event_type == "asr_final" and pending_turn_ids:
                    turn_id = pending_turn_ids.pop(0)
                    partial_turn_ids.discard(turn_id)
                    event["turn_id"] = turn_id
                    mark_turn_event(turn_id, "asr_final")
                await websocket.send_json(event)
                if event_type == "error":
                    return

        async def forward_audio() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                pcm = message.get("bytes")
                if pcm:
                    # RTC 回调提供短 PCM 帧；适配器按对应上游协议即时转发。
                    await asr_client.send_audio(pcm)
                elif message.get("text"):
                    # 前端 VAD 控制帧仅用于耗时关联，不转发到 ASR 上游。
                    try:
                        control = json.loads(message["text"])
                    except (TypeError, ValueError):
                        continue
                    if control.get("type") != "trace":
                        continue
                    turn_id = control.get("turn_id")
                    trace_event = control.get("event")
                    if not isinstance(turn_id, str) or not turn_id:
                        continue
                    if trace_event == "speech_start":
                        pending_turn_ids.append(turn_id)
                        mark_turn_event(turn_id, "speech_start")
                    elif trace_event == "speech_end":
                        mark_turn_event(turn_id, "speech_end")

        provider_task = asyncio.create_task(receive_provider_results())
        audio_task = asyncio.create_task(forward_audio())
        done, pending = await asyncio.wait(
            {provider_task, audio_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exception = task.exception()
            if exception:
                raise exception
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # 不向浏览器暴露上游响应正文或凭证；服务端日志保留错误类别和 request id。
        status = getattr(getattr(exc, "response", None), "status_code", None)
        provider_logid = getattr(getattr(exc, "response", None), "headers", {}).get(
            "X-Tt-Logid", "unavailable"
        )
        print(
            f"[asr] stream failed provider={provider} phase={phase} "
            f"error={type(exc).__name__} detail={str(exc)[:240]!r} "
            f"http_status={status or 'unavailable'} logid={provider_logid}"
        )
        try:
            await websocket.send_json({"type": "error", "code": "ASR_UPSTREAM_FAILED"})
        except Exception:
            pass
    finally:
        try:
            await asr_client.finish()
        except Exception:
            pass
        try:
            await asr_client.close()
        except Exception:
            pass


def _session_is_owned(session, user: User) -> bool:
    return bool(session and session.account_id == user.id)


@app.post("/api/agent/sessions/{session_id}/mock-llm-turns")
async def mock_llm_turn(
    session_id: str,
    request: AgentTurnRequest,
    user: User = Depends(require_current_user),
):
    """接收真实 ASR 结果，流式返回固定 mock LLM 文本，不调用外部模型。"""

    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    if not request.text.strip():
        return JSONResponse({"error": {"code": "EMPTY_TEXT"}}, status_code=400)

    async def stream_response():
        chunks = [
            "你好，我是由本项目自主编排的智能体。",
            "ASR 已接入豆包 Seed ASR 2.0，LLM 当前仍返回固定 mock 文本。",
        ]
        for chunk in chunks:
            yield f"data: {json.dumps({'type': 'text_delta', 'text': chunk}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.12)
        yield 'data: {"type":"done"}\n\n'

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent/sessions")
async def create_agent_session(
    request: AgentSessionRequest,
    user: User = Depends(require_current_user),
) -> dict[str, Any]:
    """创建本次临时 RTC Agent 连接，并关联当前用户的稳定逻辑对话。"""

    scene = SCENES.get(request.scene_id)
    if not scene:
        return JSONResponse({"error": {"code": "SCENE_NOT_FOUND"}}, status_code=404)
    agent_config = scene.get("AgentConfig") or {}
    conversation_id = await get_or_create_user_conversation(user.id)
    session = await agent_service.create_session(
        request.scene_id,
        request.room_id,
        request.user_id,
        user.id,
        conversation_id,
        agent_config.get("GatewayProfileId", ""),
    )
    return {
        "session_id": session.session_id,
        "conversation_id": conversation_id,
        "status": "created",
        "media_status": "not_connected",
    }


@app.post("/api/agent/sessions/{session_id}/turns")
async def agent_turn(
    session_id: str,
    request: AgentTurnRequest,
    user: User = Depends(require_current_user),
):
    """提交 ASR 定稿文本并以 SSE 返回已配置 LLM 服务的生成增量。"""

    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    if not request.text.strip():
        return JSONResponse({"error": {"code": "EMPTY_TEXT"}}, status_code=400)

    async def stream_response():
        try:
            async for text_delta in agent_service.stream_turn(
                session, request.text.strip(), request.turn_id
            ):
                yield f"data: {json.dumps({'type': 'text_delta', 'text': text_delta}, ensure_ascii=False)}\n\n"
            yield 'data: {"type":"done"}\n\n'
        except ContextGatewayError as exc:
            print(f"[agent] context turn failed code={exc.code} status={exc.status_code or 'none'}")
            yield f"data: {json.dumps({'type': 'error', 'code': exc.code})}\n\n"
        except TurnStateError as exc:
            yield f"data: {json.dumps({'type': 'error', 'code': exc.code})}\n\n"
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, IndexError) as exc:
            # 客户端只收稳定错误类型；服务端记录异常类别，不记录凭据或对话内容。
            print(f"[agent] LLM turn failed: {type(exc).__name__}")
            yield 'data: {"type":"error","code":"LLM_REQUEST_FAILED"}\n\n'

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agent/sessions/{session_id}/messages")
async def agent_session_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(require_current_user),
) -> dict[str, Any]:
    """读取当前逻辑会话的 Gateway 短期消息，用于新 RTC 房间恢复字幕。"""

    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    try:
        messages = await get_context_gateway().get_messages(
            user.id, session.conversation_id, limit=limit
        )
    except ContextGatewayError as exc:
        print(f"[agent] context history failed code={exc.code} status={exc.status_code or 'none'}")
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code},
        ) from exc
    return {"conversation_id": session.conversation_id, "messages": messages}


@app.post("/api/agent/sessions/{session_id}/mock-turns")
async def mock_agent_turn(
    session_id: str,
    user: User = Depends(require_current_user),
):
    """模拟一次语音轮次：返回固定 ASR 识别文本及固定 LLM 回复，不调用外部模型。"""

    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)

    async def stream_response():
        yield f"data: {json.dumps({'type': 'asr_final', 'text': MOCK_ASR_TEXT}, ensure_ascii=False)}\n\n"
        # 以词组为单位分片，前端可以用它展示 LLM 流式输出。
        chunks = ["你好，", "我是由本项目自主编排的智能体。", "当前 ASR 和 LLM 使用固定 mock 返回，", "尚未接入真实模型，也不会调用火山 Agent。"]
        for chunk in chunks:
            yield f"data: {json.dumps({'type': 'text_delta', 'text': chunk}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.12)
        yield 'data: {"type":"done"}\n\n'

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/api/agent/sessions/{session_id}")
async def close_agent_session(
    session_id: str,
    user: User = Depends(require_current_user),
) -> dict[str, bool]:
    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    return {"closed": await agent_service.close_session(session_id)}


@app.post("/api/agent/sessions/{session_id}/interrupt")
async def interrupt_agent_session(
    session_id: str,
    user: User = Depends(require_current_user),
) -> dict[str, bool]:
    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    return {"interrupted": await agent_service.interrupt(session)}


@app.post("/debug/chat")
async def debug_chat(
    request: DebugRequest,
    user: User = Depends(require_current_user),
) -> dict[str, Any]:
    """构造测试请求并直接调用正式 chat_callback，收集其 SSE 输出。"""
    del user
    started_at = time.perf_counter()
    messages = [item.model_dump() for item in (request.history or [])]
    messages.append({"role": "user", "content": request.question})
    raw_body = json.dumps(
        {"messages": messages},
        ensure_ascii=False,
    ).encode("utf-8")
    consumed = False

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        if consumed:
            return {"type": "http.disconnect"}
        consumed = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    internal_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/chat_callback",
            "headers": [
                (
                    b"authorization",
                    f"Bearer {os.getenv('CUSTOM_LLM_API_KEY', '')}".encode(),
                )
            ],
        },
        receive,
    )
    response = await chat_callback(internal_request)
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    answer_parts: list[str] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
                content = event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if content:
                    answer_parts.append(content)
            except (ValueError, TypeError, IndexError, AttributeError):
                continue
    answer = "".join(answer_parts)
    messages.append({"role": "assistant", "content": answer})
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    print(f"[debug/chat] completed in {elapsed_ms} ms")
    return {
        "question": request.question,
        "history": messages,
        "elapsed_ms": elapsed_ms,
    }


def _hmac(key: bytes | str, value: str) -> bytes:
    key_bytes = key.encode() if isinstance(key, str) else key
    return hmac.new(key_bytes, value.encode(), hashlib.sha256).digest()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _escape(value: Any) -> str:
    return quote(str(value), safe="-_.~")


def sign_headers(
    account: dict[str, Any], action: str, version: str, body: str
) -> dict[str, str]:
    """生成与原 `@volcengine/openapi` Signer 一致的 HMAC-SHA256 请求头。"""

    x_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    region = "cn-north-1"
    service = "rtc"
    scope = f"{short_date}/{region}/{service}/request"
    headers = {
        "Host": "rtc.volcengineapi.com",
        "Content-type": "application/json",
        "X-Date": x_date,
        "X-Content-Sha256": _sha256(body),
    }
    signable_headers = sorted(
        (key.lower(), " ".join(value.split()))
        for key, value in headers.items()
        if key.lower() != "content-type"
    )
    canonical_headers = "\n".join(f"{key}:{value}" for key, value in signable_headers)
    signed_headers = ";".join(key for key, _ in signable_headers)
    query = "&".join(
        f"{_escape(key)}={_escape(value)}"
        for key, value in sorted({"Action": action, "Version": version}.items())
    )
    canonical_request = "\n".join(
        ["POST", "/", query, f"{canonical_headers}\n", signed_headers, headers["X-Content-Sha256"]]
    )
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, scope, _sha256(canonical_request)])
    signing_key = _hmac(account["secretKey"], short_date)
    signing_key = _hmac(signing_key, region)
    signing_key = _hmac(signing_key, service)
    signing_key = _hmac(signing_key, "request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={account['accessKeyId']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


@app.post("/api/chat_callback")
async def chat_callback(request: Request):
    """接收火山 CustomLLM 请求，并以 OpenAI SSE 格式流式返回模型回答。

    火山引擎会向此地址发送 Bearer APIKey 和 JSON 请求，消息格式为
    OpenAI Chat Completions 的 `messages`。本函数将请求转发到由
    `LLM_API_BASE`、`LLM_API_KEY`、`LLM_MODEL` 配置的 OpenAI 兼容服务。
    """

    expected_api_key = os.getenv("CUSTOM_LLM_API_KEY", "").strip()
    if not expected_api_key:
        return JSONResponse({"error": {"code": "CALLBACK_NOT_CONFIGURED"}}, status_code=503)
    authorization = request.headers.get("authorization", "")
    scheme, _, provided_api_key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        provided_api_key.strip(), expected_api_key
    ):
        return JSONResponse({"error": {"code": "UNAUTHORIZED"}}, status_code=401)

    callback_started = time.perf_counter()
    print(f"[trace][callback] request_received t={time.time():.3f}")
    try:
        payload = await request.json()
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return JSONResponse(
                {"error": {"message": "messages must be a non-empty array"}},
                status_code=400,
            )
        rag_context = ""
        last_user_message = next(
            (item.get("content", "") for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        if isinstance(last_user_message, str):
            rag_started = time.perf_counter()
            print(f"[trace][rag] request_start t={time.time():.3f}")
            try:
                rag_context = await get_rag_service().search(last_user_message)
                print(f"[trace][rag] request_end t={time.time():.3f} elapsed_ms={(time.perf_counter() - rag_started) * 1000:.2f} context_chars={len(rag_context)}")
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                # 知识库暂时不可用时仍允许模型直接回答。
                print(f"[trace][rag] failed t={time.time():.3f} elapsed_ms={(time.perf_counter() - rag_started) * 1000:.2f} error={exc}")
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=400)

    async def stream_response():
        print(f"[trace][callback] sse_start t={time.time():.3f}")
        try:
            async for content in get_llm_service().chat(messages, rag_context):
                yield f"data: {json.dumps({'choices': [{'delta': {'content': content}}]}, ensure_ascii=False)}\n\n"
        except (httpx.HTTPError, RuntimeError) as exc:
            error = {"error": {"message": f"LLM request failed: {exc}"}}
            yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
        finally:
            yield "data: [DONE]\n\n"
            print(f"[trace][callback] request_end t={time.time():.3f} elapsed_ms={(time.perf_counter() - callback_started) * 1000:.2f}")

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/proxy")
async def proxy(
    payload: dict[str, Any] = Body(default={}),
    Action: str | None = Query(None),
    Version: str = Query("2024-12-01"),
    user: User = Depends(require_current_user),
) -> dict[str, Any]:
    """代理 AIGC VoiceChat OpenAPI 请求。"""
    del user

    async def logic() -> dict[str, Any]:
        action = Action or ""
        assert_value(action, "Action 不能为空")
        assert_value(Version, "Version 不能为空")
        scene_id = payload.get("SceneID", "")
        assert_value(scene_id, "SceneID 不能为空, SceneID 用于指定场景的 JSON")
        json_data = SCENES.get(scene_id)
        assert_value(json_data, f"{scene_id} 不存在, 请先在 python_server/scenes 下定义该场景的 JSON.")

        voice_chat = json_data.get("VoiceChat") or {}
        account_config = json_data.get("AccountConfig") or {}
        assert_value(account_config.get("accessKeyId"), "AccountConfig.accessKeyId 不能为空")
        assert_value(account_config.get("secretKey"), "AccountConfig.secretKey 不能为空")

        if action == "StartVoiceChat":
            body = voice_chat
            llm_config = ((body.get("Config") or {}).get("LLMConfig") or {})
            if llm_config.get("Mode") == "CustomLLM":
                assert_value(llm_config.get("Url"), "CustomLLM.LLMConfig.Url 不能为空")
        elif action == "StopVoiceChat":
            app_id = voice_chat.get("AppId")
            room_id = voice_chat.get("RoomId")
            task_id = voice_chat.get("TaskId")
            assert_value(app_id, "VoiceChat.AppId 不能为空")
            assert_value(room_id, "VoiceChat.RoomId 不能为空")
            assert_value(task_id, "VoiceChat.TaskId 不能为空")
            body = {"AppId": app_id, "RoomId": room_id, "TaskId": task_id}
        else:
            body = {}

        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = sign_headers(account_config, action, Version, body_text)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://rtc.volcengineapi.com",
                params={"Action": action, "Version": Version},
                headers=headers,
                content=body_text.encode("utf-8"),
            )
            response.raise_for_status()
            return response.json()

    return await wrapper("proxy", logic, contain_response_metadata=False)


@app.post("/getScenes")
async def get_scenes(user: User = Depends(require_current_user)) -> dict[str, Any]:
    """读取场景并为该次登录后的页面访问签发独立 RTC 房间和身份。"""

    async def logic() -> dict[str, Any]:
        scenes: list[dict[str, Any]] = []
        for scene_id, json_data in SCENES.items():
            scene_config = json_data.get("SceneConfig") or {}
            rtc_config = json_data.get("RTCConfig") or {}
            # VoiceChat is optional and read only for backward compatibility with the demo.
            voice_chat = json_data.get("VoiceChat") or {}
            legacy_agent_config = voice_chat.get("AgentConfig") or {}
            agent_config = json_data.get("AgentConfig") or legacy_agent_config
            media_config = json_data.get("MediaConfig") or {}

            app_id = rtc_config.get("AppId")
            assert_value(app_id, f"{scene_id} 场景的 RTCConfig.AppId 不能为空")
            app_key = rtc_config.get("AppKey")
            assert_value(app_key, f"为登录用户签发 Token 时, {scene_id} 场景的 AppKey 不可为空")
            room_id = str(uuid.uuid4())
            user_id = str(uuid.uuid4())
            key = AccessToken(app_id, app_key, room_id, user_id)
            key.add_privilege(Privileges.PrivSubscribeStream, 0)
            key.add_privilege(Privileges.PrivPublishStream, 0)
            token_ttl = max(300, int(os.getenv("RTC_TOKEN_TTL_SECONDS", "3600")))
            key.expire_time(int(time.time()) + token_ttl)
            safe_rtc = dict(rtc_config)
            safe_rtc.pop("AppKey", None)
            safe_rtc.update({"RoomId": room_id, "UserId": user_id, "Token": key.serialize()})

            config = voice_chat.get("Config") or {}
            vision_config = (config.get("LLMConfig") or {}).get("VisionConfig") or {}
            snapshot_config = vision_config.get("SnapshotConfig") or {}
            avatar_config = config.get("AvatarConfig") or {}
            scene_config["id"] = scene_id
            scene_config["botName"] = agent_config.get("BotUserId") or agent_config.get("UserId") or "assistant"
            scene_config.setdefault(
                "isInterruptMode",
                agent_config.get("InterruptEnabled", config.get("InterruptMode") == 0),
            )
            scene_config.setdefault("isVision", media_config.get("EnableVideo", vision_config.get("Enable", False)))
            scene_config.setdefault("isScreenMode", media_config.get("ScreenMode", snapshot_config.get("StreamType") == 1))
            scene_config.setdefault(
                "isAvatarScene",
                bool(media_config.get("AvatarProfileId")) or avatar_config.get("Enabled", False),
            )
            scene_config.setdefault(
                "avatarBgUrl",
                media_config.get("AvatarBackgroundUrl", avatar_config.get("BackgroundUrl", "")),
            )

            scenes.append({"scene": dict(scene_config), "rtc": safe_rtc})
        return {"scenes": scenes}

    return await wrapper("getScenes", logic)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=3001, reload=True)
