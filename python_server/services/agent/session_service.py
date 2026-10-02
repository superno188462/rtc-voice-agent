"""自有 Agent 会话编排基础层；通过平级服务调用 ASR、RAG、LLM 和 TTS。"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass, field

from services.llm import get_llm_service
from services.rag import get_rag_service


@dataclass
class AgentSession:
    session_id: str
    scene_id: str
    room_id: str
    user_id: str
    gateway_profile_id: str = ""
    created_at: float = field(default_factory=time.time)
    messages: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_task: asyncio.Task | None = None


class AgentService:
    """进程内会话存储仅用于单实例迁移阶段；生产需换成共享存储。"""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self, scene_id: str, room_id: str, user_id: str, gateway_profile_id: str = ""
    ) -> AgentSession:
        session = AgentSession(
            session_id=secrets.token_urlsafe(32),
            scene_id=scene_id,
            room_id=room_id,
            user_id=user_id,
            gateway_profile_id=gateway_profile_id,
        )
        async with self._lock:
            self._sessions[session.session_id] = session
        return session

    async def get_session(self, session_id: str) -> AgentSession | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            ttl = int(os.getenv("AGENT_SESSION_TTL_SECONDS", "3600"))
            if session and time.time() - session.created_at > ttl:
                del self._sessions[session_id]
                return None
            return session

    async def close_session(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if not session:
            return False
        await self.interrupt(session)
        return True

    async def stream_turn(self, session: AgentSession, text: str, turn_id: str | None = None):
        """按轮次编排 RAG 检索与独立 LLM 服务，并流式返回生成文本。"""

        async with session.lock:
            session.active_task = asyncio.current_task()
            if session.active_task:
                active_task = session.active_task

                def clear_active_task(completed_task: asyncio.Task) -> None:
                    if session.active_task is completed_task:
                        session.active_task = None

                active_task.add_done_callback(clear_active_task)
            session.messages.append({"role": "user", "content": text})
            try:
                rag_context = await get_rag_service().search(text)
            except Exception as exc:
                # RAG is an optional peer capability; temporary retrieval failure should not
                # prevent an otherwise valid LLM turn.
                print(f"[agent] rag unavailable error={type(exc).__name__}")
                rag_context = ""

            try:
                async for delta in get_llm_service().chat(
                    session.messages, rag_context, turn_id=turn_id
                ):
                    yield delta
            except Exception:
                # Do not retain an unmatched user turn if the provider request failed.
                if session.messages and session.messages[-1] == {"role": "user", "content": text}:
                    session.messages.pop()
                raise

    async def interrupt(self, session: AgentSession) -> bool:
        """取消当前网关流；HTTP 客户端断开时也会由 ASGI 取消对应任务。"""

        task = session.active_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            return True
        return False


agent_service = AgentService()
