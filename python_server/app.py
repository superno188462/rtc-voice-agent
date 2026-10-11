"""原 Server/app.js 的 FastAPI 等价实现。"""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import base64
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from services.llm import get_llm_service
from services.rag import get_rag_service
from services.rag.gateway_vector_store import GatewayVectorStoreError
from services.rag.knowledge_base import get_knowledge_base_service
from services.agent import agent_service
from services.agent.conversation_repository import (
    TurnStateError,
    get_or_create_user_conversation,
)
from services.trace_service import mark_turn_event
from services.asr import create_asr_client
from services.tts import TTSProviderError, create_tts_provider
from services.context_gateway import ContextGatewayError, get_context_gateway
from services.context_gateway.bootstrap import ensure_default_templates
from services.context_gateway.memory_vector_index import (
    ensure_memory_vector_collection,
    memory_vector_enabled,
    query_user_memory_records,
)
from tools.rtc_token import AccessToken, Privileges
from tools.util import assert_value, read_files, wrapper

BASE_DIR = Path(__file__).resolve().parent
LOCAL_SCENES_DIR = BASE_DIR / "scenes"
# 运行参数放在 python_server/.env；场景业务配置仍由 scenes/*.json 提供。
load_dotenv(BASE_DIR / ".env")
SCENES = read_files(LOCAL_SCENES_DIR, ".json")

from services.identity.client import (
    IdentityAuthResult,
    IdentityCenterError,
    IdentityUser,
    get_identity_center_client,
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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时同步默认提示词并校验独立的长期记忆向量集合。"""

    try:
        results = await ensure_default_templates()
        for template_name, state in results.items():
            print(f"[context] template={template_name} startup={state}")
    except ContextGatewayError as exc:
        # 不输出请求头、响应正文或提示词；Gateway 暂时不可用时仍允许服务启动。
        print(
            f"[context] template_bootstrap_failed code={exc.code} "
            f"status={exc.status_code or 'unavailable'}"
        )
    except Exception as exc:
        print(f"[context] template_bootstrap_failed error={type(exc).__name__}")
    if memory_vector_enabled():
        try:
            await ensure_memory_vector_collection()
        except GatewayVectorStoreError as exc:
            print(f"[memory_vector] collection_bootstrap_failed code={exc.code} status={exc.status_code or 'unavailable'}")
        except Exception as exc:
            # Context memory remains available if optional semantic indexing is down.
            print(f"[memory_vector] collection_bootstrap_failed error={type(exc).__name__}")
    else:
        print("[memory_vector] disabled missing=GATEWAY_MEMORY_EMBEDDING_MODEL_or_gateway_config")
    yield


app = FastAPI(title="RTC AIGC Python Server", version="1.0.0", lifespan=lifespan)
configured_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS", "http://localhost:3002,http://127.0.0.1:3002"
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
    synthesize_speech: bool = Field(
        default=True,
        description="是否将回答通过 TTS 合成；语音输入默认开启，手动文本可关闭。",
    )


class AuthCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class KnowledgeBaseInitializeRequest(BaseModel):
    description: str = Field(default="RTC Agent 项目知识库", max_length=1000, description="集合说明。")
    embedding_model: str = Field(min_length=1, max_length=200, description="该集合使用的 Gateway Embedding 模型名。")
    chunk_size: int = Field(default=1000, ge=100, le=6000, description="项目切片的单条字符上限，必须与集合上限一致。")


class KnowledgeDocumentRequest(BaseModel):
    document_id: str | None = Field(default=None, min_length=1, max_length=200, description="内部兼容字段；新文档 ID 由后端生成，同路径更新沿用既有 ID。")
    directory: str = Field(default="", max_length=1000, description="知识库虚拟目录，不含文件名；使用 / 分隔目录层级。")
    name: str | None = Field(default=None, min_length=1, max_length=500, description="虚拟文件名；文件和直接输入内容统一作为虚拟文件管理。")
    source_type: str | None = Field(default=None, description="输入来源：file 或 text；兼容旧调用时可省略。")
    file_name: str | None = Field(default=None, min_length=1, max_length=500, description="旧客户端兼容字段；新客户端使用 name。")
    title: str = Field(min_length=1, max_length=500, description="用于检索结果展示的标题。")
    content: str = Field(min_length=1, max_length=200_000, description="解析后的文本内容；项目按集合限制切片并逐条调用 Gateway。")
    metadata: dict[str, Any] = Field(default_factory=dict, description="用于检索精确过滤的 JSON metadata。")


class KnowledgeDocumentBatchRequest(BaseModel):
    documents: list[KnowledgeDocumentRequest] = Field(min_length=1, max_length=64, description="最多 64 个虚拟文件；项目负责切片，网关逐片嵌入。")


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000, description="相似度检索问题。")
    top_k: int = Field(default=5, ge=1, le=20, description="最多返回的相关切片数。")
    metadata: dict[str, Any] | None = Field(default=None, description="可选 metadata 精确过滤条件。")


class MemoryVectorSearchRequest(BaseModel):
    """当前已登录用户的长期记忆向量检索输入。"""

    query: str = Field(min_length=1, max_length=10_000, description="用于语义检索的查询文本，不会写入长期记忆。")
    top_k: int = Field(default=5, ge=1, le=20, description="最多返回的长期记忆条数。")


def _user_payload(user: IdentityUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "permissions": list(user.permissions),
    }


def _validate_request_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in configured_origins:
        raise HTTPException(status_code=403, detail={"code": "ORIGIN_NOT_ALLOWED"})


def _auth_cookie_settings() -> tuple[bool, str, str | None]:
    secure_setting = os.getenv("AUTH_COOKIE_SECURE")
    secure = (
        secure_setting.strip().lower() == "true"
        if secure_setting is not None
        else os.getenv("APP_ENV", "development").strip().lower() == "production"
    )
    same_site = os.getenv("AUTH_COOKIE_SAMESITE", "lax").strip().lower()
    domain = os.getenv("AUTH_COOKIE_DOMAIN", "").strip() or None
    if same_site not in {"lax", "strict", "none"} or (same_site == "none" and not secure):
        raise RuntimeError("AUTH_COOKIE_SAMESITE_NONE_REQUIRES_SECURE_COOKIE")
    return secure, same_site, domain


async def require_current_user(request: Request) -> IdentityUser:
    _validate_request_origin(request)
    try:
        return await get_identity_center_client().current_user(
            request.cookies.get("identity_session")
        )
    except IdentityCenterError as exc:
        raise _identity_http_error(exc) from exc


def _identity_http_error(error: IdentityCenterError) -> HTTPException:
    """Map center responses to stable, non-sensitive application errors."""

    if error.status_code == 401:
        return HTTPException(status_code=401, detail={"code": error.code})
    if error.code == "IDENTITY_CENTER_BAD_RESPONSE":
        return HTTPException(status_code=502, detail={"code": error.code})
    if error.status_code in (403, 409, 422, 429):
        return HTTPException(
            status_code=error.status_code, detail={"code": error.code}
        )
    return HTTPException(
        status_code=503, detail={"code": "IDENTITY_CENTER_UNAVAILABLE"}
    )


def _vector_gateway_error(error: GatewayVectorStoreError) -> HTTPException:
    """将向量网关异常转成脱敏且稳定的前端错误契约。"""

    if error.code in {"VECTOR_STORE_NOT_CONFIGURED", "VECTOR_DIMENSIONS_CONFIG_INVALID"}:
        status_code = 503
    elif error.code in {"VECTOR_COLLECTION_MODEL_MISMATCH", "VECTOR_COLLECTION_DIMENSIONS_MISMATCH", "VECTOR_COLLECTION_CHUNKING_MISMATCH", "VECTOR_COLLECTION_CHUNK_SIZE_MISMATCH"}:
        status_code = 409
    elif error.code in {
        "VECTOR_EMBEDDING_QUOTA_EXCEEDED",
        "VECTOR_USER_QUOTA_EXCEEDED",
        "VECTOR_PROJECT_QUOTA_EXCEEDED",
    } or error.status_code == 429:
        status_code = 429
    elif error.code == "VECTOR_GATEWAY_AUTH_FAILED":
        status_code = 502
    elif error.code == "VECTOR_GATEWAY_SERVICE_NOT_ENABLED":
        status_code = 403
    elif error.code == "VECTOR_GATEWAY_TIMEOUT":
        status_code = 504
    elif error.status_code == 404:
        status_code = 404
    elif error.status_code in {502, 503, 504}:
        status_code = 503 if error.status_code != 504 else 504
    elif error.status_code == 409:
        status_code = 409
    else:
        status_code = 502
    messages = {
        "VECTOR_STORE_NOT_CONFIGURED": "知识库网关配置不完整，请检查后端环境变量。",
        "VECTOR_DIMENSIONS_CONFIG_INVALID": "向量维度配置无效。",
        "VECTOR_COLLECTION_MODEL_MISMATCH": "已有集合绑定了不同的 Embedding 模型，请更换集合名或模型配置。",
        "VECTOR_COLLECTION_DIMENSIONS_MISMATCH": "Embedding 维度与已有集合不匹配。",
        "VECTOR_COLLECTION_CHUNKING_MISMATCH": "已有集合使用了不同的切片配置。",
        "VECTOR_COLLECTION_CHUNK_SIZE_MISMATCH": "已有集合的单条文本长度上限与所选值不一致。",
        "VECTOR_GATEWAY_AUTH_FAILED": "知识库网关鉴权失败，请检查项目 API Key 和服务授权。",
        "VECTOR_GATEWAY_TIMEOUT": "知识库网关请求超时，请稍后重试。",
        "VECTOR_GATEWAY_NETWORK_ERROR": "暂时无法连接知识库网关。",
        "VECTOR_GATEWAY_INVALID_RESPONSE": "知识库网关返回了无法识别的响应。",
        "VECTOR_COLLECTION_NOT_FOUND": "配置的知识库集合不存在，请先初始化集合。",
        "VECTOR_GATEWAY_SERVICE_NOT_ENABLED": "网关项目尚未开通向量知识库服务。",
        "VECTOR_EMBEDDING_QUOTA_EXCEEDED": "Embedding 调用额度不足，请检查 Gateway 配额。",
        "VECTOR_USER_QUOTA_EXCEEDED": "当前用户的向量服务额度不足。",
        "VECTOR_PROJECT_QUOTA_EXCEEDED": "当前项目的向量服务额度不足。",
        "VECTOR_QUOTA_CONFIGURATION_ERROR": "Gateway 向量额度配置异常，请联系管理员。",
        "VECTOR_GATEWAY_ERROR": "Gateway 向量服务暂时不可用，请稍后重试。",
    }
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": messages.get(error.code, "知识库操作失败，请检查网关状态。")},
    )


def _memory_vector_gateway_error(error: GatewayVectorStoreError) -> HTTPException:
    """Map vector adapter errors to messages appropriate for long-term memory search."""

    mapped = _vector_gateway_error(error)
    messages = {
        "VECTOR_STORE_NOT_CONFIGURED": "长期记忆向量检索未配置，请检查后端 Gateway Memory 配置。",
        "VECTOR_GATEWAY_AUTH_FAILED": "长期记忆向量服务鉴权失败，请检查后端项目 API Key。",
        "VECTOR_GATEWAY_TIMEOUT": "长期记忆向量检索超时，请稍后重试。",
        "VECTOR_GATEWAY_NETWORK_ERROR": "暂时无法连接长期记忆向量服务。",
        "VECTOR_GATEWAY_SERVICE_NOT_ENABLED": "Gateway 项目尚未开通向量检索服务。",
        "VECTOR_COLLECTION_NOT_FOUND": "长期记忆向量集合不存在，请检查后端配置并重启服务。",
        "VECTOR_COLLECTION_MODEL_MISMATCH": "长期记忆向量集合绑定的 Embedding 模型与配置不一致。",
        "VECTOR_COLLECTION_CHUNK_SIZE_MISMATCH": "长期记忆向量集合的文本长度配置与项目配置不一致。",
        "VECTOR_EMBEDDING_QUOTA_EXCEEDED": "Embedding 调用额度不足，请检查 Gateway 配额。",
        "VECTOR_USER_QUOTA_EXCEEDED": "当前用户的向量服务额度不足。",
        "VECTOR_PROJECT_QUOTA_EXCEEDED": "当前项目的向量服务额度不足。",
    }
    return HTTPException(
        status_code=mapped.status_code,
        detail={"code": error.code, "message": messages.get(error.code, "长期记忆向量检索失败，请检查 Gateway 配置。")},
    )


def _set_identity_cookie(response: JSONResponse, result: IdentityAuthResult) -> None:
    secure, same_site, domain = _auth_cookie_settings()
    response.set_cookie(
        key="identity_session",
        value=result.session_cookie,
        max_age=result.max_age,
        httponly=True,
        secure=secure,
        samesite=same_site,
        domain=domain,
        path="/",
    )


def _delete_identity_cookie(response: JSONResponse) -> None:
    secure, same_site, domain = _auth_cookie_settings()
    response.delete_cookie(
        "identity_session",
        path="/",
        domain=domain,
        httponly=True,
        secure=secure,
        samesite=same_site,
    )


@app.get("/api/knowledge-base")
async def knowledge_base_status(user: IdentityUser = Depends(require_current_user)) -> dict[str, Any]:
    """读取当前服务端配置集合的状态；不向浏览器返回项目 API Key。"""

    del user
    try:
        return await get_knowledge_base_service().status()
    except GatewayVectorStoreError as error:
        raise _vector_gateway_error(error) from error


@app.post("/api/knowledge-base/initialize")
async def initialize_knowledge_base(
    payload: KnowledgeBaseInitializeRequest,
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, Any]:
    """创建配置指定的 Gateway collection，或验证其模型/切片参数。"""

    del user
    try:
        return await get_knowledge_base_service().initialize(
            description=payload.description,
            model=payload.embedding_model,
            chunk_size=payload.chunk_size,
        )
    except GatewayVectorStoreError as error:
        raise _vector_gateway_error(error) from error


@app.get("/api/knowledge-base/documents")
async def list_knowledge_documents(user: IdentityUser = Depends(require_current_user)) -> dict[str, Any]:
    """列出本项目持久化的知识文档，不返回正文。"""

    del user
    return {"documents": await get_knowledge_base_service().list_documents()}


@app.post("/api/knowledge-base/documents")
async def upsert_knowledge_document(
    payload: KnowledgeDocumentRequest,
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, Any]:
    """按知识库虚拟路径创建或更新条目，项目切片后由 Gateway 逐片生成向量。"""

    del user
    try:
        result = await get_knowledge_base_service().upsert_document(
            document_id=payload.document_id,
            title=payload.title,
            content=payload.content,
            metadata=payload.metadata,
            file_name=payload.file_name,
            directory=payload.directory,
            name=payload.name,
            source_type=payload.source_type,
        )
        return {"document_id": result["id"], "result": result["result"]}
    except GatewayVectorStoreError as error:
        raise _vector_gateway_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail={"code": "INVALID_KNOWLEDGE_DOCUMENT", "message": str(error)}) from error


@app.post("/api/knowledge-base/documents/batch")
async def upsert_knowledge_documents(
    payload: KnowledgeDocumentBatchRequest,
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, Any]:
    """批量写入 1 至 64 个虚拟文件；相同目录/名称会更新原条目。"""

    del user
    try:
        documents = [item.model_dump() for item in payload.documents]
        results = await get_knowledge_base_service().upsert_documents(documents)
        return {"documents": [{"document_id": item["id"], "result": item["result"]} for item in results]}
    except GatewayVectorStoreError as error:
        raise _vector_gateway_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail={"code": "INVALID_KNOWLEDGE_DOCUMENT", "message": str(error)}) from error


@app.delete("/api/knowledge-base/documents")
async def delete_knowledge_document(
    document_id: str = Query(min_length=1, max_length=200),
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, Any]:
    """按稳定文档 ID 删除该文档及网关保存的向量切片。"""

    del user
    try:
        return await get_knowledge_base_service().delete_document(document_id)
    except GatewayVectorStoreError as error:
        raise _vector_gateway_error(error) from error


@app.post("/api/knowledge-base/search")
async def search_knowledge_base(
    payload: KnowledgeSearchRequest,
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, Any]:
    """提供管理页的检索预览，响应字段与 Agent RAG 适配层一致。"""

    del user
    try:
        results = await get_knowledge_base_service().search(
            payload.query,
            top_k=payload.top_k,
            metadata=payload.metadata,
        )
        return {"results": results}
    except GatewayVectorStoreError as error:
        raise _vector_gateway_error(error) from error


@app.post("/api/agent/memories/search", summary="检索当前用户的长期记忆向量")
async def search_long_term_memory_vectors(
    payload: MemoryVectorSearchRequest,
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, Any]:
    """Query all scopes in the dedicated memory collection for the authenticated user only."""

    try:
        results = await query_user_memory_records(
            get_context_gateway(), user.id, payload.query, top_k=payload.top_k
        )
        return {"results": results}
    except GatewayVectorStoreError as error:
        raise _memory_vector_gateway_error(error) from error
    except ContextGatewayError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": error.code, "message": "无法读取长期记忆，请检查 Gateway 上下文服务后重试。"},
        ) from error


@app.post("/api/auth/register", status_code=201)
async def auth_register(
    credentials: AuthCredentials,
    request: Request,
) -> JSONResponse:
    """代理身份中心注册，并在本服务域名下设置 HttpOnly 会话 Cookie。"""

    _validate_request_origin(request)
    try:
        result = await get_identity_center_client().register(
            credentials.username, credentials.password
        )
    except IdentityCenterError as exc:
        raise _identity_http_error(exc) from exc
    response = JSONResponse({"user": _user_payload(result.user)}, status_code=201)
    _set_identity_cookie(response, result)
    return response


@app.post("/api/auth/login")
async def auth_login(
    credentials: AuthCredentials,
    request: Request,
) -> JSONResponse:
    _validate_request_origin(request)
    try:
        result = await get_identity_center_client().login(
            credentials.username, credentials.password
        )
    except IdentityCenterError as exc:
        raise _identity_http_error(exc) from exc
    response = JSONResponse({"user": _user_payload(result.user)})
    _set_identity_cookie(response, result)
    return response


@app.get("/api/auth/me")
async def auth_me(user: IdentityUser = Depends(require_current_user)) -> dict[str, object]:
    return {"user": _user_payload(user)}


@app.post("/api/auth/logout")
async def auth_logout(
    request: Request,
) -> JSONResponse:
    """Revoke the upstream session and clear this application's HttpOnly cookie."""

    _validate_request_origin(request)
    try:
        await get_identity_center_client().logout(
            request.cookies.get("identity_session")
        )
        response = JSONResponse({"logged_out": True})
    except IdentityCenterError:
        response = JSONResponse(
            {"detail": {"code": "IDENTITY_CENTER_UNAVAILABLE"}}, status_code=503
        )
    _delete_identity_cookie(response)
    return response


@app.websocket("/api/agent/sessions/{session_id}/asr")
async def agent_asr_stream(websocket: WebSocket, session_id: str) -> None:
    """代理浏览器 RTC PCM 与配置的 ASR 上游，并返回统一识别事件。

    浏览器接口保持稳定；ASR_PROVIDER 选择网关或火山适配器，统一输出 asr_partial、
    asr_final、error 事件。输入音频为 16 kHz、16-bit、单声道 PCM 二进制帧。
    上游空闲达到 ASR_IDLE_CLOSE_SECONDS 后结束并关闭当前识别会话；浏览器连接和 Agent
    逻辑会话不关闭，后续 PCM 会创建新上游连接。asr_ready 事件的 generation 标识连接代次。
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
        user = await get_identity_center_client().current_user(
            websocket.cookies.get("identity_session")
        )
    except IdentityCenterError as exc:
        if exc.status_code == 401 or exc.code == "AUTH_REQUIRED":
            await websocket.close(code=4401, reason="AUTH_REQUIRED")
        else:
            await websocket.close(code=1011, reason="IDENTITY_CENTER_UNAVAILABLE")
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
    turn_generations: dict[str, int] = {}
    failed_generations: set[int] = set()
    audio_queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=128)
    idle_close_seconds = max(1.0, float(os.getenv("ASR_IDLE_CLOSE_SECONDS", "5")))
    finalize_timeout_seconds = max(
        0.5, float(os.getenv("ASR_FINALIZE_TIMEOUT_SECONDS", "3"))
    )

    async def receive_browser_messages() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                pcm = message.get("bytes")
                if pcm:
                    await audio_queue.put(("audio", pcm))
                elif message.get("text"):
                    await audio_queue.put(("control", message["text"]))
        finally:
            await audio_queue.put(None)

    async def receive_provider_results(client, generation: int) -> None:
        async for event in client.receive_events():
            event_type = event.get("type")
            turn_id = None
            if event_type == "asr_partial":
                turn_id = next(
                    (
                        candidate
                        for candidate in pending_turn_ids
                        if turn_generations.get(candidate) == generation
                        and candidate not in partial_turn_ids
                    ),
                    None,
                )
                if turn_id:
                    partial_turn_ids.add(turn_id)
                    mark_turn_event(turn_id, "asr_first_partial")
            elif event_type == "asr_final":
                turn_index = next(
                    (
                        index
                        for index, candidate in enumerate(pending_turn_ids)
                        if turn_generations.get(candidate) == generation
                    ),
                    None,
                )
                if turn_index is not None:
                    turn_id = pending_turn_ids.pop(turn_index)
                    turn_generations.pop(turn_id, None)
                    partial_turn_ids.discard(turn_id)
            if event_type == "asr_final" and turn_id:
                partial_turn_ids.discard(turn_id)
                event["turn_id"] = turn_id
                mark_turn_event(turn_id, "asr_final")
            elif event_type == "error":
                failed_generations.add(generation)
                turn_id = next(
                    (
                        candidate
                        for candidate in pending_turn_ids
                        if turn_generations.get(candidate) == generation
                    ),
                    None,
                )
                if turn_id:
                    event["turn_id"] = turn_id
                print(
                    f"[asr] upstream_error provider={provider} generation={generation} "
                    f"code={event.get('code', 'ASR_UPSTREAM_FAILED')} "
                    f"request_id={event.get('request_id', 'unavailable')} "
                    f"message={str(event.get('message', ''))[:240]!r}"
                )
            event["asr_generation"] = generation
            try:
                await websocket.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                return
            if event_type == "error":
                return

    async def close_upstream(client, reader_task: asyncio.Task | None) -> None:
        if not client:
            return
        try:
            await client.finish()
        except Exception as exc:
            print(f"[asr] upstream_finish_failed error={type(exc).__name__}")
        if reader_task:
            done, _ = await asyncio.wait(
                {reader_task}, timeout=finalize_timeout_seconds
            )
            if not done:
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
            else:
                try:
                    reader_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    print(f"[asr] upstream_reader_ended error={type(exc).__name__}")
        try:
            await client.close()
        except Exception as exc:
            print(f"[asr] upstream_close_failed error={type(exc).__name__}")

    async def retire_unfinalized_turns(
        generation: int, *, suppress_warning: bool = False
    ) -> None:
        unfinished = [
            turn_id
            for turn_id in pending_turn_ids
            if turn_generations.get(turn_id) == generation
        ]
        for turn_id in unfinished:
            pending_turn_ids.remove(turn_id)
            partial_turn_ids.discard(turn_id)
            turn_generations.pop(turn_id, None)
            print(
                f"[asr] turn_unfinalized generation={generation} turn_id={turn_id}"
            )
            try:
                await websocket.send_json(
                    {
                        "type": "asr_turn_incomplete",
                        "code": "ASR_TURN_FINAL_TIMEOUT",
                        "turn_id": turn_id,
                        "generation": generation,
                        "suppress_warning": suppress_warning,
                    }
                )
            except (WebSocketDisconnect, RuntimeError):
                return

    async def manage_upstream() -> None:
        nonlocal phase
        client = asr_client
        generation = 0
        reader_task: asyncio.Task | None = None
        audio_bytes = 0
        last_audio_at = asyncio.get_running_loop().time()

        async def connect_client(new_client, *, reconnect: bool) -> tuple[Any, asyncio.Task]:
            nonlocal generation, phase
            phase = "upstream_connect"
            await new_client.connect()
            generation += 1
            print(
                f"[asr] upstream_connected provider={provider} generation={generation}"
            )
            task = asyncio.create_task(
                receive_provider_results(new_client, generation)
            )
            await websocket.send_json(
                {"type": "asr_ready", "reconnected": reconnect, "generation": generation}
            )
            phase = "streaming"
            return new_client, task

        try:
            client, reader_task = await connect_client(client, reconnect=False)
            last_audio_at = asyncio.get_running_loop().time()
            while True:
                elapsed = asyncio.get_running_loop().time() - last_audio_at
                wait_seconds = max(0.0, idle_close_seconds - elapsed) if client else None
                queue_task = asyncio.create_task(audio_queue.get())
                wait_tasks = {queue_task}
                if reader_task:
                    wait_tasks.add(reader_task)
                done, _ = await asyncio.wait(
                    wait_tasks,
                    timeout=wait_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    queue_task.cancel()
                    await asyncio.gather(queue_task, return_exceptions=True)
                    if client:
                        print(
                            f"[asr] upstream_idle_close provider={provider} "
                            f"generation={generation} idle_seconds={idle_close_seconds:g} "
                            f"audio_bytes={audio_bytes} audio_ms={audio_bytes / 32:.1f}"
                        )
                        await close_upstream(client, reader_task)
                        await retire_unfinalized_turns(
                            generation,
                            suppress_warning=generation in failed_generations,
                        )
                        failed_generations.discard(generation)
                        client = None
                        reader_task = None
                        audio_bytes = 0
                    last_audio_at = asyncio.get_running_loop().time()
                    continue

                if reader_task and reader_task in done:
                    try:
                        reader_task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        print(
                            f"[asr] upstream_reader_ended provider={provider} "
                            f"generation={generation} error={type(exc).__name__}"
                        )
                    await close_upstream(client, reader_task)
                    await retire_unfinalized_turns(
                        generation,
                        suppress_warning=generation in failed_generations,
                    )
                    failed_generations.discard(generation)
                    client = None
                    reader_task = None
                    audio_bytes = 0
                if queue_task not in done:
                    queue_task.cancel()
                    await asyncio.gather(queue_task, return_exceptions=True)
                    continue
                queued = queue_task.result()

                if queued is None:
                    await close_upstream(client, reader_task)
                    client = None
                    reader_task = None
                    return

                kind, payload = queued
                if kind == "control":
                    try:
                        control = json.loads(payload)
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
                    elif trace_event == "asr_final":
                        mark_turn_event(turn_id, "asr_final")
                    continue

                if reader_task and reader_task.done():
                    await close_upstream(client, reader_task)
                    client = None
                    reader_task = None
                if client is None:
                    client = create_asr_client(session.user_id)
                    client, reader_task = await connect_client(client, reconnect=True)
                    audio_bytes = 0
                phase = "streaming"
                for candidate in pending_turn_ids:
                    if candidate not in turn_generations:
                        turn_generations[candidate] = generation
                await client.send_audio(payload)
                audio_bytes += len(payload)
                last_audio_at = asyncio.get_running_loop().time()
        finally:
            await close_upstream(client, reader_task)

    browser_task = asyncio.create_task(receive_browser_messages())
    upstream_task = asyncio.create_task(manage_upstream())
    try:
        done, pending = await asyncio.wait(
            {browser_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exception = task.exception()
            if exception:
                raise exception
        if browser_task in done:
            await upstream_task
        else:
            browser_task.cancel()
            await asyncio.gather(browser_task, return_exceptions=True)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
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
        for task in (browser_task, upstream_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(browser_task, upstream_task, return_exceptions=True)


def _session_is_owned(session, user: IdentityUser) -> bool:
    return bool(session and session.account_id == user.id)


@app.post("/api/agent/sessions/{session_id}/mock-llm-turns")
async def mock_llm_turn(
    session_id: str,
    request: AgentTurnRequest,
    user: IdentityUser = Depends(require_current_user),
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
    user: IdentityUser = Depends(require_current_user),
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
    user: IdentityUser = Depends(require_current_user),
):
    """提交 ASR 定稿文本并以 SSE 返回已配置 LLM 服务的生成增量。"""

    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    if not request.text.strip():
        return JSONResponse({"error": {"code": "EMPTY_TEXT"}}, status_code=400)

    async def stream_response():
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        sentences: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        tts_provider = None
        if request.synthesize_speech:
            try:
                tts_provider = create_tts_provider()
                tts_provider.validate()
            except TTSProviderError as exc:
                tts_provider = None
                await events.put({"type": "tts_error", "code": str(exc)})

        async def synthesize_sentences() -> None:
            if tts_provider is None:
                # 消费掉生产者的结束标记；整体 done 还需等记忆分析轨迹完成。
                while await sentences.get() is not None:
                    pass
                await events.put({"type": "audio_done"})
                return

            tts_available = True
            while True:
                item = await sentences.get()
                if item is None:
                    break
                sequence, sentence = item
                if not tts_available:
                    continue
                try:
                    chunk_index = 0
                    async for audio_chunk in tts_provider.synthesize(
                        sentence, audio_format="pcm"
                    ):
                        if audio_chunk:
                            await events.put(
                                {
                                    "type": "audio_chunk",
                                    "sequence": sequence,
                                    "chunk_index": chunk_index,
                                    "audio_base64": base64.b64encode(audio_chunk).decode("ascii"),
                                    "sample_rate": 24000,
                                }
                            )
                            chunk_index += 1
                    if chunk_index == 0:
                        await events.put({"type": "tts_error", "code": "TTS_EMPTY_AUDIO"})
                except TTSProviderError as exc:
                    await events.put({"type": "tts_error", "code": str(exc)})
                    tts_available = False
                except Exception as exc:
                    print(f"[tts] synthesis_failed turn_id={request.turn_id or 'unavailable'} error={type(exc).__name__}")
                    await events.put({"type": "tts_error", "code": "TTS_UPSTREAM_FAILED"})
                    tts_available = False
            await events.put({"type": "audio_done"})

        async def generate_answer() -> None:
            sentence_buffer = ""
            sentence_sequence = 0
            try:
                async def publish_trace(trace_event: dict[str, Any]) -> None:
                    await events.put(trace_event)

                async for text_delta in agent_service.stream_turn(
                    session,
                    request.text.strip(),
                    request.turn_id,
                    on_trace=publish_trace,
                ):
                    await events.put({"type": "text_delta", "text": text_delta})
                    if tts_provider is None:
                        continue
                    sentence_buffer += text_delta
                    while sentence_buffer:
                        match = re.search(r"[。！？!?；;\n]+", sentence_buffer)
                        if match:
                            boundary = match.end()
                        elif len(sentence_buffer) >= 500:
                            boundary = 500
                        else:
                            break
                        sentence = sentence_buffer[:boundary].strip()
                        sentence_buffer = sentence_buffer[boundary:]
                        if sentence:
                            await sentences.put((sentence_sequence, sentence))
                            sentence_sequence += 1
                if sentence_buffer.strip() and tts_provider is not None:
                    await sentences.put((sentence_sequence, sentence_buffer.strip()))
                await events.put({"type": "llm_done"})
            except ContextGatewayError as exc:
                print(f"[agent] context turn failed code={exc.code} status={exc.status_code or 'none'}")
                await events.put({"type": "error", "code": exc.code})
            except TurnStateError as exc:
                await events.put({"type": "error", "code": exc.code})
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError, IndexError) as exc:
                # 客户端只收稳定错误类型；服务端记录异常类别，不记录凭据或对话内容。
                print(f"[agent] LLM turn failed: {type(exc).__name__}")
                await events.put({"type": "error", "code": "LLM_REQUEST_FAILED"})
            finally:
                await sentences.put(None)

        tts_task = asyncio.create_task(synthesize_sentences())
        answer_task = asyncio.create_task(generate_answer())
        audio_done = False
        memory_analysis_done = False
        try:
            while True:
                event = await events.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] == "audio_done":
                    audio_done = True
                elif event["type"] == "trace" and event.get("stage") == "memory_analysis":
                    memory_analysis_done = event.get("status") in {"completed", "degraded", "failed"}
                elif event["type"] == "error":
                    break
                if audio_done and memory_analysis_done:
                    yield 'data: {"type":"done"}\n\n'
                    break
        finally:
            for task in (answer_task, tts_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(answer_task, tts_task, return_exceptions=True)

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agent/sessions/{session_id}/messages")
async def agent_session_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    user: IdentityUser = Depends(require_current_user),
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
    user: IdentityUser = Depends(require_current_user),
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
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, bool]:
    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    return {"closed": await agent_service.close_session(session_id)}


@app.post("/api/agent/sessions/{session_id}/interrupt")
async def interrupt_agent_session(
    session_id: str,
    user: IdentityUser = Depends(require_current_user),
) -> dict[str, bool]:
    session = await agent_service.get_session(session_id)
    if not _session_is_owned(session, user):
        return JSONResponse({"error": {"code": "SESSION_NOT_FOUND"}}, status_code=404)
    return {"interrupted": await agent_service.interrupt(session)}


@app.post("/debug/chat")
async def debug_chat(
    request: DebugRequest,
    user: IdentityUser = Depends(require_current_user),
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
    user: IdentityUser = Depends(require_current_user),
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
async def get_scenes(user: IdentityUser = Depends(require_current_user)) -> dict[str, Any]:
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
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="运行 RTC AIGC Python Server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8002")),
        help="监听端口（默认读取 PORT 环境变量，未设置时为 8002）",
    )
    args = parser.parse_args()
    uvicorn.run("app:app", host="0.0.0.0", port=args.port, reload=True)
