"""稳定 Agent 会话标识和轮次幂等状态的 PostgreSQL 持久化。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from services.auth.database import get_session_factory
from services.auth.models import AgentConversation, AgentTurn


class TurnStateError(RuntimeError):
    """轮次 ID 冲突、已完成或正在处理。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TurnReceipt:
    status: str
    pair_hash: str | None = None


async def get_or_create_user_conversation(user_id: str) -> str:
    """为认证用户返回唯一稳定会话 ID；只保存 ID，不保存聊天内容。"""

    factory = get_session_factory()
    async with factory.begin() as db:
        await db.execute(
            insert(AgentConversation)
            .values(id=str(uuid.uuid4()), user_id=user_id)
            .on_conflict_do_nothing(index_elements=[AgentConversation.user_id])
        )
        conversation = await db.scalar(
            select(AgentConversation).where(AgentConversation.user_id == user_id)
        )
        if not conversation:
            raise RuntimeError("AGENT_CONVERSATION_CREATE_FAILED")
        conversation.updated_at = datetime.now(timezone.utc)
        return conversation.id


async def begin_turn(
    conversation_id: str, turn_id: str, request_hash: str
) -> TurnReceipt:
    """登记轮次；相同 ID 的并发请求不会重复触发模型或记忆追加。"""

    factory = get_session_factory()
    async with factory.begin() as db:
        pending = await db.scalar(
            select(AgentTurn)
            .where(
                AgentTurn.conversation_id == conversation_id,
                AgentTurn.status == "append_pending",
                AgentTurn.turn_id != turn_id,
            )
            .with_for_update()
        )
        if pending:
            raise TurnStateError("CONTEXT_APPEND_UNCERTAIN")
        inserted = await db.execute(
            insert(AgentTurn)
            .values(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                turn_id=turn_id,
                request_hash=request_hash,
                status="processing",
            )
            .on_conflict_do_nothing(
                index_elements=[AgentTurn.conversation_id, AgentTurn.turn_id]
            )
        )
        receipt = await db.scalar(
            select(AgentTurn)
            .where(
                AgentTurn.conversation_id == conversation_id,
                AgentTurn.turn_id == turn_id,
            )
            .with_for_update()
        )
        if not receipt:
            raise RuntimeError("AGENT_TURN_RECEIPT_MISSING")
        if receipt.request_hash != request_hash:
            raise TurnStateError("TURN_ID_REUSED_WITH_DIFFERENT_INPUT")
        if receipt.status == "completed":
            raise TurnStateError("TURN_ALREADY_COMPLETED")
        if receipt.status == "processing" and inserted.rowcount == 0:
            stale_before = datetime.now(timezone.utc) - timedelta(minutes=10)
            if receipt.updated_at < stale_before:
                receipt.updated_at = datetime.now(timezone.utc)
                return TurnReceipt("processing")
            raise TurnStateError("TURN_ALREADY_IN_PROGRESS")
        if receipt.status == "append_pending":
            return TurnReceipt(receipt.status, receipt.pair_hash)
        if receipt.status == "failed":
            receipt.status = "processing"
            receipt.pair_hash = None
            return TurnReceipt("processing")
        return TurnReceipt(receipt.status, receipt.pair_hash)


async def mark_turn_append_pending(conversation_id: str, turn_id: str, pair_hash: str) -> None:
    """记录短期消息即将追加，供超时后的对账恢复使用。"""

    await _set_turn_state(conversation_id, turn_id, "append_pending", pair_hash)


async def mark_turn_completed(conversation_id: str, turn_id: str) -> None:
    """标记短期消息已经确认写入。"""

    await _set_turn_state(conversation_id, turn_id, "completed", None)


async def mark_turn_failed(conversation_id: str, turn_id: str) -> None:
    """只允许尚未开始短期写入的失败轮次重试。"""

    factory = get_session_factory()
    async with factory.begin() as db:
        await db.execute(
            update(AgentTurn)
            .where(
                AgentTurn.conversation_id == conversation_id,
                AgentTurn.turn_id == turn_id,
                AgentTurn.status == "processing",
            )
            .values(status="failed", updated_at=datetime.now(timezone.utc))
        )


async def _set_turn_state(
    conversation_id: str, turn_id: str, status: str, pair_hash: str | None
) -> None:
    factory = get_session_factory()
    async with factory.begin() as db:
        result = await db.execute(
            update(AgentTurn)
            .where(
                AgentTurn.conversation_id == conversation_id,
                AgentTurn.turn_id == turn_id,
            )
            .values(
                status=status,
                pair_hash=pair_hash if pair_hash is not None else AgentTurn.pair_hash,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("AGENT_TURN_RECEIPT_MISSING")
