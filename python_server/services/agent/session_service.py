"""自有 Agent 会话编排基础层；通过平级服务调用 ASR、RAG、LLM 和 TTS。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field

from services.llm import get_llm_service
from services.rag import get_rag_service
from services.agent.conversation_repository import (
    TurnStateError,
    begin_turn,
    mark_turn_append_pending,
    mark_turn_completed,
    mark_turn_failed,
)
from services.context_gateway import ContextGatewayError, get_context_gateway
from services.context_gateway.assembler import (
    load_context,
    message_pair_hash,
    persist_extracted_context_updates,
)


@dataclass
class AgentSession:
    session_id: str
    scene_id: str
    room_id: str
    user_id: str
    account_id: str
    conversation_id: str
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
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._conversation_locks_guard = asyncio.Lock()
        self._memory_tasks: dict[str, asyncio.Task] = {}
        self._memory_tasks_guard = asyncio.Lock()

    async def create_session(
        self,
        scene_id: str,
        room_id: str,
        user_id: str,
        account_id: str,
        conversation_id: str,
        gateway_profile_id: str = "",
    ) -> AgentSession:
        session = AgentSession(
            session_id=secrets.token_urlsafe(32),
            scene_id=scene_id,
            room_id=room_id,
            user_id=user_id,
            account_id=account_id,
            conversation_id=conversation_id,
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
        """读取 Gateway 上下文，调用 RAG/LLM，并在成功后追加完整轮次。"""

        resolved_turn_id = turn_id or secrets.token_urlsafe(24)
        async with self._conversation_locks_guard:
            conversation_lock = self._conversation_locks.setdefault(
                session.conversation_id, asyncio.Lock()
            )
        async with conversation_lock:
            async with self._memory_tasks_guard:
                pending_memory_task = self._memory_tasks.get(session.conversation_id)
            if pending_memory_task and not pending_memory_task.done():
                # Preserve read-after-write ordering without tying extraction to the prior RTC room.
                await asyncio.shield(pending_memory_task)
            session.active_task = asyncio.current_task()
            if session.active_task:
                active_task = session.active_task

                def clear_active_task(completed_task: asyncio.Task) -> None:
                    if session.active_task is completed_task:
                        session.active_task = None

                active_task.add_done_callback(clear_active_task)
            request_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            receipt = await begin_turn(session.conversation_id, resolved_turn_id, request_hash)
            gateway = get_context_gateway()
            if receipt.status == "append_pending":
                try:
                    latest = await gateway.get_messages(
                        session.account_id, session.conversation_id, limit=100
                    )
                except ContextGatewayError as exc:
                    raise ContextGatewayError("CONTEXT_APPEND_UNCERTAIN") from exc
                if receipt.pair_hash and len(latest) >= 2 and message_pair_hash(latest[-2:]) == receipt.pair_hash:
                    await mark_turn_completed(session.conversation_id, resolved_turn_id)
                    raise TurnStateError("TURN_ALREADY_COMPLETED")
                # 网关没有幂等键。状态不确定时禁止盲目重发，避免重复写入。
                raise ContextGatewayError("CONTEXT_APPEND_UNCERTAIN")

            try:
                system_prompt, history, _profile, _memories = await load_context(
                    gateway, session.account_id, session.conversation_id, text
                )
            except asyncio.CancelledError:
                await asyncio.shield(mark_turn_failed(session.conversation_id, resolved_turn_id))
                raise
            except Exception:
                await mark_turn_failed(session.conversation_id, resolved_turn_id)
                raise
            history.append({"role": "user", "content": text})
            try:
                rag_context = await get_rag_service().search(text)
            except asyncio.CancelledError:
                await asyncio.shield(mark_turn_failed(session.conversation_id, resolved_turn_id))
                raise
            except Exception as exc:
                # RAG remains optional; a RAG failure never changes Gateway memory state.
                print(f"[agent] rag unavailable error={type(exc).__name__}")
                rag_context = ""

            recent_user_messages = [
                message["content"]
                for message in history[:-1]
                if message.get("role") == "user" and isinstance(message.get("content"), str)
            ][-6:]

            async def extract_and_persist_context() -> None:
                """与回答生成并行提取候选；保存失败不影响已经生成的回答。"""

                try:
                    extraction = await get_llm_service().extract_user_context(
                        text, recent_user_messages, _profile, turn_id=resolved_turn_id
                    )
                    persistence_stats, persistence_errors = await persist_extracted_context_updates(
                        gateway,
                        session.account_id,
                        extraction,
                        current_user_text=text,
                        recent_user_messages=recent_user_messages,
                    )
                    print(
                        f"[agent][memory] persisted turn_id={resolved_turn_id} "
                        f"profile_updates={persistence_stats['profile_updates_written']} "
                        f"long_term_memories={persistence_stats['long_term_memories_written']} "
                        f"profile_candidates={persistence_stats['profile_candidates']} "
                        f"profile_accepted={persistence_stats['profile_updates_accepted']} "
                        f"memory_candidates={persistence_stats['memory_candidates']} "
                        f"memory_accepted={persistence_stats['memory_candidates_accepted']} "
                        f"memory_rejected_evidence={persistence_stats['memory_rejected_evidence']} "
                        f"memory_rejected_confidence={persistence_stats['memory_rejected_confidence']} "
                        f"memory_rejected_content={persistence_stats['memory_rejected_content']} "
                        f"memory_rejected_sensitive={persistence_stats['memory_rejected_sensitive']} "
                        f"memory_duplicates={persistence_stats['memory_duplicates']} "
                        f"errors={','.join(persistence_errors) if persistence_errors else 'none'}"
                    )
                except Exception as exc:
                    # Keep private content and upstream error bodies out of logs and user responses.
                    status_code = getattr(exc, "status_code", None)
                    print(
                        f"[agent][memory] extraction_or_persistence_failed "
                        f"turn_id={resolved_turn_id} error={type(exc).__name__} "
                        f"status_code={status_code if isinstance(status_code, int) else 'unavailable'}"
                    )

            memory_task = asyncio.create_task(extract_and_persist_context())
            async with self._memory_tasks_guard:
                self._memory_tasks[session.conversation_id] = memory_task

            def clear_memory_task(completed_task: asyncio.Task) -> None:
                async def clear() -> None:
                    async with self._memory_tasks_guard:
                        if self._memory_tasks.get(session.conversation_id) is completed_task:
                            self._memory_tasks.pop(session.conversation_id, None)

                try:
                    asyncio.get_running_loop().create_task(clear())
                except RuntimeError:
                    pass

            memory_task.add_done_callback(clear_memory_task)

            try:
                async for delta in get_llm_service().chat(
                    history, rag_context, turn_id=resolved_turn_id, system_prompt=system_prompt
                ):
                    yield delta
                assistant_text = history[-1]["content"] if history and history[-1]["role"] == "assistant" else ""
                pair = [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": assistant_text},
                ]
                pair_hash = message_pair_hash(pair)
                await mark_turn_append_pending(session.conversation_id, resolved_turn_id, pair_hash)
                try:
                    await gateway.append_messages(session.account_id, session.conversation_id, pair)
                except ContextGatewayError as append_error:
                    # A timed-out POST may already have committed. Read the tail before deciding;
                    # if it cannot be confirmed, keep append_pending and fail closed on retries.
                    try:
                        latest = await gateway.get_messages(
                            session.account_id, session.conversation_id, limit=100
                        )
                    except ContextGatewayError as reconcile_error:
                        raise ContextGatewayError("CONTEXT_APPEND_UNCERTAIN") from reconcile_error
                    if len(latest) < 2 or message_pair_hash(latest[-2:]) != pair_hash:
                        raise ContextGatewayError("CONTEXT_APPEND_UNCERTAIN") from append_error
                await mark_turn_completed(session.conversation_id, resolved_turn_id)
            except asyncio.CancelledError:
                # Room closure may cancel this response task, but memory extraction belongs to
                # the authenticated conversation and must continue independently.
                await asyncio.shield(mark_turn_failed(session.conversation_id, resolved_turn_id))
                raise
            except Exception:
                await mark_turn_failed(session.conversation_id, resolved_turn_id)
                raise

    async def interrupt(self, session: AgentSession) -> bool:
        """取消当前网关流；HTTP 客户端断开时也会由 ASGI 取消对应任务。"""

        task = session.active_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            return True
        return False


agent_service = AgentService()
