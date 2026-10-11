"""从 Gateway 读取并组装每轮 LLM 上下文，且只按明确规则写入记忆。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.context_gateway.client import ContextGatewayClient, ContextGatewayError
from services.context_gateway.project_registry import (
    normalize_project_name,
    resolve_or_create_user_project,
    resolve_project_ids,
)
from services.context_gateway.memory_vector_index import (
    memory_vector_enabled,
    search_user_memories,
    sync_memory_entry,
    sync_user_memories,
)

logger = logging.getLogger(__name__)


def _memory_created_at() -> str:
    """Return the program-generated UTC time at which a memory is written."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _sync_memory_vector_best_effort(
    user_id: str, saved_memory: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Index a committed Context memory without turning an index outage into a write failure."""

    if not memory_vector_enabled() or not isinstance(saved_memory, dict):
        return None, None
    try:
        result = await sync_memory_entry(user_id, saved_memory)
        return result, None
    except Exception as exc:
        error: dict[str, Any] = {"type": type(exc).__name__}
        for attr, key in (
            ("code", "code"),
            ("upstream_code", "upstream_code"),
            ("status_code", "http_status"),
            ("gateway_trace_id", "gateway_trace_id"),
            ("confirmed_ids", "confirmed_ids"),
            ("uncertain_ids", "uncertain_ids"),
        ):
            value = getattr(exc, attr, None)
            if value not in (None, [], ""):
                error[key] = value
        return None, error


def _occurred_at_from_evidence(evidence: list[str]) -> str | None:
    """Parse only explicit date expressions from user quotes; never infer an event time."""
    text = " ".join(evidence)
    patterns = (
        r"(?<!\d)(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?!\d)",
        r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                event_date = date(*(int(part) for part in match.groups()))
                return datetime.combine(
                    event_date, datetime.min.time(), ZoneInfo("Asia/Shanghai")
                ).isoformat()
            except ValueError:
                return None
    relative_days = (("前天", 2), ("昨天", 1), ("今天", 0))
    for token, days_ago in relative_days:
        if token in text:
            event_date = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=days_ago)
            return datetime.combine(
                event_date, datetime.min.time(), ZoneInfo("Asia/Shanghai")
            ).isoformat()
    return None


async def load_context(
    client: ContextGatewayClient,
    external_user_id: str,
    session_id: str,
    current_text: str,
    user_projects: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, str]], dict[str, Any], list[dict[str, Any]]]:
    """并行读取模板、画像、短期历史，再分页获取长期记忆并按问题筛选。"""

    template, profile_result, history = await asyncio.gather(
        client.get_template(), client.get_profile(external_user_id),
        client.get_messages(external_user_id, session_id, limit=50),
    )
    profile, _profile_version = profile_result
    profile = normalize_user_profile(profile)
    memories: list[dict[str, Any]] = []
    max_pages = min(20, max(1, int(os.getenv("GATEWAY_CONTEXT_MEMORY_MAX_PAGES", "5"))))
    listing_complete = True
    for page in range(1, max_pages + 1):
        payload = await client.get_memory_page(external_user_id, page, page_size=50)
        items = _memory_items(payload)
        memories.extend(item for item in items if isinstance(item, dict) and isinstance(item.get("content"), str))
        gateway_has_next = _has_next_page(payload, page, max_pages + 1)
        has_next = _has_next_page(payload, page, max_pages)
        if not items or not has_next:
            if page == max_pages and gateway_has_next:
                listing_complete = False
            break

    recent_context = " ".join(
        message.get("content", "")
        for message in history[-12:]
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    )
    project_context = f"{recent_context} {current_text}"
    active_project_ids = resolve_project_ids(
        user_projects or [], current_text, recent_context
    )
    ranked_memory_ids: list[str] | None = None
    if memory_vector_enabled():
        try:
            await sync_user_memories(
                external_user_id, memories, listing_complete=listing_complete
            )
            query = " ".join(
                [message.get("content", "") for message in history[-6:] if message.get("role") == "user"]
                + [current_text]
            )[-4000:]
            hits = await search_user_memories(
                external_user_id,
                query,
                active_project_ids=active_project_ids,
                top_k=int(os.getenv("GATEWAY_MEMORY_TOP_K", "12")),
            )
            ranked_memory_ids = [hit["memory_id"] for hit in hits]
        except Exception as exc:
            # The Context API remains the source of truth; keyword filtering is a
            # recoverable fallback while the separate vector service is unavailable.
            logger.warning("memory_vector operation=retrieve error=%s", type(exc).__name__)
    selected = select_relevant_memories(
        memories,
        project_context,
        limit=min(40, max(1, int(os.getenv("GATEWAY_MEMORY_CONTEXT_LIMIT", "12")))),
        active_project_ids=active_project_ids,
        ranked_memory_ids=ranked_memory_ids,
    )
    active_project_names = [
        project["name"]
        for project in user_projects or []
        if project.get("id") in active_project_ids and isinstance(project.get("name"), str)
    ]
    memory_text = "\n".join(
        f"- [{(item.get('metadata') if isinstance(item.get('metadata'), dict) else {}).get('scope', 'global')}:{(item.get('metadata') if isinstance(item.get('metadata'), dict) else {}).get('project_name') or '全局'}; "
        f"{_memory_type(item)}/{_memory_role(item)}] {item['content']}"
        for item in selected
    )
    profile_text = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))[:4000]
    variables = {
        "profile": profile_text,
        "long_term_memories": memory_text,
        "active_project": "、".join(active_project_names),
    }
    profile_is_template_variable = "{{profile}}" in template
    memories_are_template_variable = "{{long_term_memories}}" in template
    system_prompt = render_template(template, variables)
    if profile and not profile_is_template_variable:
        system_prompt += "\n\n当前用户画像（仅作背景信息）：\n" + profile_text
    if active_project_names:
        system_prompt += f"\n\n当前对话关联的用户项目：{'、'.join(active_project_names)}。仅将 project 范围记忆用于这些项目。"
    if memory_text and not memories_are_template_variable:
        system_prompt += "\n\n与当前问题相关的长期记忆（仅作背景信息；有效约束优先于偏好和当前计划）：\n" + memory_text
    return system_prompt, history[-50:], profile, selected


def render_template(template: str, variables: dict[str, str]) -> str:
    """仅替换当前项目明确提供的模板变量，未知变量原样保留。"""

    pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
    return pattern.sub(lambda match: variables.get(match.group(1), match.group(0)), template)


def select_relevant_memories(
    memories: list[dict[str, Any]],
    query: str,
    limit: int = 12,
    active_project_ids: list[str] | None = None,
    ranked_memory_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按 Gateway 向量排序筛选；未提供向量结果时回退到本地关键词重叠。"""

    query_terms = _terms(query)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    vector_rank = {memory_id: index for index, memory_id in enumerate(ranked_memory_ids or [])}
    constraints: list[dict[str, Any]] = []
    for index, item in enumerate(memories):
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        memory_scope = metadata.get("scope", "global")
        if memory_scope == "project" and metadata.get("subject_project_id") not in (active_project_ids or []):
            continue
        if memory_scope not in {"global", "project"}:
            continue
        if metadata.get("status") in {"inactive", "superseded", "deleted"} or metadata.get("is_active") is False:
            continue
        valid_until = metadata.get("valid_until")
        if isinstance(valid_until, str):
            try:
                expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    from datetime import timezone

                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(expiry.tzinfo):
                    continue
            except ValueError:
                # Malformed expiry metadata must not hide otherwise usable legacy entries.
                pass
        content = item.get("content", "")
        if _memory_role(item) == "constraint":
            constraints.append(item)
            continue
        memory_id = _memory_id_for_context(item)
        if ranked_memory_ids is not None:
            if memory_id in vector_rank:
                ranked.append((-vector_rank[memory_id], -index, item))
            continue
        tags = item.get("tags", [])
        searchable = content + " " + " ".join(tag for tag in tags if isinstance(tag, str))
        overlap = len(query_terms & _terms(searchable)) if query_terms else 0
        if overlap:
            ranked.append((overlap, -index, item))
    ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    # Always carry active constraints forward, even when their wording does not
    # overlap with the user's current plan or question.
    return constraints + [entry[2] for entry in ranked[:limit]]


def _memory_id_for_context(item: dict[str, Any]) -> str | None:
    value = item.get("id", item.get("memory_id", item.get("uuid")))
    return value if isinstance(value, str) else None


_CONSTRAINT_TEXT_PATTERN = re.compile(
    r"(?:不能吃|不能使用|不能做|不得|不可以|避免|忌口|过敏|必须遵守|需遵守|应当遵守)"
)


def _scoped_memory_content_key(content: str, metadata: Any) -> str:
    """Keep duplicate detection inside one global or project memory scope."""

    metadata = metadata if isinstance(metadata, dict) else {}
    scope = metadata.get("scope", "global")
    project_id = metadata.get("subject_project_id", "") if scope == "project" else ""
    return f"{scope}:{project_id}:{content.strip().casefold()}"


def _memory_role(item: dict[str, Any]) -> str:
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    role = metadata.get("memory_role")
    if role in {"preference", "constraint", "fact", "event", "procedure"}:
        return role
    tags = item.get("tags")
    if isinstance(tags, list) and any(tag in {"饮食限制", "行为约束", "安全约束"} for tag in tags):
        return "constraint"
    content = item.get("content")
    if isinstance(content, str) and _CONSTRAINT_TEXT_PATTERN.search(content):
        return "constraint"
    return "fact"


def _memory_type(item: dict[str, Any]) -> str:
    """Return the explicit memory kind, treating old untyped entries as semantic."""

    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    memory_type = metadata.get("memory_type", item.get("memory_type"))
    return (
        memory_type
        if isinstance(memory_type, str) and memory_type in {"episodic", "semantic", "procedural"}
        else "semantic"
    )


PROFILE_SCHEMA_VERSION = 1
PROFILE_LANGUAGE_VALUES = {"zh-CN", "en-US"}
PROFILE_STYLE_VALUES = {"concise", "balanced", "detailed"}
PROFILE_LIST_FIELDS = {
    "interests": "interests",
    "food_preferences.likes": "likes",
    "food_preferences.dislikes": "dislikes",
}
ALLOWED_MEMORY_TAGS = {"学习目标", "当前目标", "稳定背景", "项目经历", "其他"}
_MEMORY_SENSITIVE_PATTERN = re.compile(
    r"(?i)(password|api[-_ ]?key|token|密码|密钥|身份证|身份证号|手机号|手机号码|银行卡|住址|家庭住址|病史|疾病|诊断|账户余额)"
)
_UNDERSPECIFIED_MEMORY_PATTERN = re.compile(
    r"^(?:用户)?(?:当前|目前)?正在(?:备考|准备考试|复习|学习|练习)(?:中)?[。.!！]?$"
)


def normalize_user_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """将网关画像限制为本项目定义的版本化字段，忽略未知字段和错误类型。"""

    normalized: dict[str, Any] = {}
    if profile.get("schema_version") == PROFILE_SCHEMA_VERSION:
        normalized["schema_version"] = PROFILE_SCHEMA_VERSION
    language = profile.get("preferred_language")
    if isinstance(language, str) and language in PROFILE_LANGUAGE_VALUES:
        normalized["preferred_language"] = language
    style = profile.get("response_style")
    if isinstance(style, str) and style in PROFILE_STYLE_VALUES:
        normalized["response_style"] = style
    interests = _clean_string_list(profile.get("interests"), limit=20, max_length=80)
    if interests:
        normalized["interests"] = interests
    preferences = profile.get("food_preferences")
    if isinstance(preferences, dict):
        food_preferences = {
            key: _clean_string_list(preferences.get(key), limit=20, max_length=80)
            for key in ("likes", "dislikes")
        }
        if food_preferences["likes"] or food_preferences["dislikes"]:
            normalized["food_preferences"] = food_preferences
    return normalized


def _clean_string_list(value: Any, limit: int, max_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        item = item.strip()[:max_length]
        key = item.casefold()
        if item and key not in seen and not _is_sensitive(item):
            seen.add(key)
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _is_sensitive(value: str) -> bool:
    return bool(_MEMORY_SENSITIVE_PATTERN.search(value) or re.search(r"\b\d{11}\b", value))


def _evidence_is_user_sourced(evidence: Any, user_messages: list[str]) -> bool:
    if not isinstance(evidence, str) or not evidence.strip():
        return False

    def normalize(value: str) -> str:
        return re.sub(r"[\s，。？！、,.!?;；:：\"'“”‘’]+", "", value).casefold()

    quote = normalize(evidence)
    return bool(quote and any(quote in normalize(message) for message in user_messages))


def _evidence_quotes_are_user_sourced(evidence: Any, user_messages: list[str]) -> bool:
    """Require every quote supporting a multi-turn memory to occur in user text."""

    return (
        isinstance(evidence, list)
        and 1 <= len(evidence) <= 4
        and all(_evidence_is_user_sourced(quote, user_messages) for quote in evidence)
    )


def _provided_memory_ids(memories: list[dict[str, Any]] | None) -> set[str]:
    """Collect IDs actually shown to the extractor, limiting which entries it may update."""

    ids: set[str] = set()
    for item in memories or []:
        if not isinstance(item, dict):
            continue
        memory_id = item.get("id", item.get("memory_id", item.get("uuid")))
        if isinstance(memory_id, str):
            ids.add(memory_id)
    return ids


def _validated_profile_updates(
    candidates: Any, user_messages: list[str], min_confidence: float
) -> tuple[list[dict[str, str]], dict[str, int]]:
    rejected = {"evidence": 0, "confidence": 0, "schema": 0, "sensitive": 0}
    if not isinstance(candidates, list):
        return [], rejected
    valid: list[dict[str, str]] = []
    for item in candidates[:10]:
        if not isinstance(item, dict):
            rejected["schema"] += 1
            continue
        if not _evidence_is_user_sourced(item.get("evidence"), user_messages):
            rejected["evidence"] += 1
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            rejected["confidence"] += 1
            continue
        field = item.get("field")
        action = item.get("action")
        value = item.get("value")
        if not math.isfinite(confidence) or not min_confidence <= confidence <= 1:
            rejected["confidence"] += 1
            continue
        if not isinstance(field, str) or not isinstance(action, str):
            rejected["schema"] += 1
            continue
        if not isinstance(value, str) or not value.strip():
            rejected["schema"] += 1
            continue
        value = value.strip()[:80]
        if _is_sensitive(value):
            rejected["sensitive"] += 1
            continue
        if field == "preferred_language" and action == "set" and value in PROFILE_LANGUAGE_VALUES:
            valid.append({"field": field, "action": action, "value": value})
        elif field == "response_style" and action == "set" and value in PROFILE_STYLE_VALUES:
            valid.append({"field": field, "action": action, "value": value})
        elif field in PROFILE_LIST_FIELDS and action in {"add", "remove", "set"}:
            # The extraction JSON Schema permits "set" for every field. For a
            # list field, interpret it as an additive preference to avoid
            # replacing previously collected values.
            valid.append(
                {
                    "field": field,
                    "action": "add" if action == "set" else action,
                    "value": value,
                }
            )
        else:
            rejected["schema"] += 1
    return valid, rejected


def _known_profile_values(
    profile: Any, updates: list[dict[str, str]]
) -> set[str]:
    """Return values represented in the current profile or accepted this turn."""

    values: set[str] = set()
    normalized = normalize_user_profile(profile) if isinstance(profile, dict) else {}
    values.update(normalized.get("interests", []))
    food = normalized.get("food_preferences", {})
    if isinstance(food, dict):
        values.update(food.get("likes", []))
        values.update(food.get("dislikes", []))
    for update in updates:
        if update["field"] not in PROFILE_LIST_FIELDS:
            continue
        if update["action"] == "remove":
            values = {
                value for value in values
                if value.casefold() != update["value"].casefold()
            }
        else:
            values.add(update["value"])
    return {value.strip().casefold() for value in values if isinstance(value, str) and len(value.strip()) >= 2}


def _apply_profile_updates(
    profile: dict[str, Any], updates: list[dict[str, str]]
) -> dict[str, Any]:
    merged = normalize_user_profile(profile)
    merged["schema_version"] = PROFILE_SCHEMA_VERSION
    for update in updates:
        field, action, value = update["field"], update["action"], update["value"]
        if action == "set":
            merged[field] = value
            continue
        if field == "interests":
            values = list(merged.get("interests", []))
            nested = None
        else:
            nested_key, value_key = field.split(".", 1)
            nested = dict(merged.get(nested_key, {}))
            values = list(nested.get(value_key, []))
        existing = {entry.casefold(): entry for entry in values}
        if action == "remove":
            existing.pop(value.casefold(), None)
        elif len(existing) < 20:
            existing.setdefault(value.casefold(), value)
        result = list(existing.values())
        if nested is None:
            merged[field] = result
        else:
            nested[value_key] = result
            merged[nested_key] = nested
    return merged


async def persist_extracted_context_updates(
    client: ContextGatewayClient,
    external_user_id: str,
    extraction: dict[str, Any],
    current_user_text: str,
    recent_user_messages: list[str],
    current_long_term_memories: list[dict[str, Any]] | None = None,
    user_projects: list[dict[str, Any]] | None = None,
    current_profile: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """校验证据、置信度与画像 schema 后，写入候选画像更新及长期记忆。"""

    evidence_messages = [*recent_user_messages[-6:], current_user_text]
    visible_memory_ids = _provided_memory_ids(current_long_term_memories)
    try:
        min_confidence = max(
            0.5, min(1.0, float(os.getenv("AGENT_MEMORY_MIN_CONFIDENCE", "0.75")))
        )
    except ValueError:
        min_confidence = 0.75
    raw_profile_updates = extraction.get("profile_updates")
    profile_updates, profile_rejected = _validated_profile_updates(
        raw_profile_updates, evidence_messages, min_confidence=min_confidence
    )
    known_profile_values = _known_profile_values(current_profile, profile_updates)
    raw_memories = extraction.get("long_term_memories")
    memories: list[dict[str, Any]] = []
    rejected = {"evidence": 0, "confidence": 0, "content": 0, "sensitive": 0, "profile_overlap": 0}
    if isinstance(raw_memories, list):
        for item in raw_memories[:5]:
            if not isinstance(item, dict):
                rejected["content"] += 1
                continue
            if not _evidence_quotes_are_user_sourced(item.get("evidence"), evidence_messages):
                rejected["evidence"] += 1
                continue
            content = item.get("content")
            memory_type = item.get("memory_type")
            memory_role = item.get("memory_role")
            scope = item.get("scope")
            project_name = item.get("project_name")
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                rejected["confidence"] += 1
                continue
            if isinstance(item.get("confidence"), bool) or not math.isfinite(confidence) or not min_confidence <= confidence <= 1:
                rejected["confidence"] += 1
                continue
            required_keys = {
                "action", "memory_id", "memory_type", "memory_role", "scope", "project_name", "content", "tags",
                "valid_until", "source", "confidence", "evidence",
            }
            if set(item) != required_keys:
                rejected["content"] += 1
                continue
            valid_until = item.get("valid_until")
            action = item.get("action")
            memory_id = item.get("memory_id")

            def valid_datetime(value: Any) -> bool:
                if value is None:
                    return True
                if not isinstance(value, str):
                    return False
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return parsed.tzinfo is not None
                except ValueError:
                    return False

            if (
                not isinstance(memory_type, str)
                or memory_type not in {"episodic", "semantic", "procedural"}
                or not isinstance(memory_role, str)
                or memory_role not in {"preference", "constraint", "fact", "event", "procedure"}
                or scope not in {"global", "project"}
                or (scope == "global" and project_name is not None)
                or (scope == "project" and (not isinstance(project_name, str) or not project_name.strip()))
                or (isinstance(project_name, str) and len(project_name.strip()) > 120)
                or not isinstance(action, str)
                or action not in {"create", "update"}
                or (action == "create" and memory_id is not None)
                or (action == "update" and not isinstance(memory_id, str))
                or item.get("source") != "conversation"
                or not isinstance(item.get("tags"), list)
                or len(item["tags"]) > 5
                or any(not isinstance(tag, str) or tag not in ALLOWED_MEMORY_TAGS for tag in item["tags"])
                or not valid_datetime(valid_until)
                or not isinstance(item.get("evidence"), list)
                or not 1 <= len(item["evidence"]) <= 4
                or any(not isinstance(quote, str) or not 1 <= len(quote) <= 500 for quote in item["evidence"])
            ):
                rejected["content"] += 1
                continue
            if scope == "project":
                candidate_name = normalize_project_name(project_name)
                known_names = {
                    normalize_project_name(name)
                    for project in user_projects or []
                    for name in [project.get("name"), *(project.get("aliases") or [])]
                    if isinstance(name, str)
                }
                user_text = " ".join(evidence_messages)
                # Existing aliases may be selected from context; a new project
                # may only be registered when its name appears in user text.
                if candidate_name not in known_names and candidate_name not in normalize_project_name(user_text):
                    rejected["content"] += 1
                    continue
            if not isinstance(content, str) or not 3 <= len(content.strip()) <= 300:
                rejected["content"] += 1
                continue
            content = content.strip()
            if _UNDERSPECIFIED_MEMORY_PATTERN.fullmatch(content):
                rejected["content"] += 1
                continue
            if _is_sensitive(content) or any(_is_sensitive(quote) for quote in item["evidence"]):
                rejected["sensitive"] += 1
                continue
            if (
                memory_role == "preference"
                and scope == "global"
                and any(value in content.casefold() for value in known_profile_values)
            ):
                rejected["profile_overlap"] += 1
                continue
            memories.append({
                "action": item["action"],
                "memory_id": item["memory_id"],
                "memory_type": memory_type,
                "memory_role": memory_role,
                "scope": scope,
                "project_name": project_name.strip() if isinstance(project_name, str) else None,
                "content": content,
                "tags": item["tags"] or ["其他"],
                "confidence": min(1.0, confidence),
                "occurred_at": (
                    _occurred_at_from_evidence(item["evidence"])
                    if memory_type == "episodic"
                    else None
                ),
                "valid_until": valid_until,
            })

    errors: list[str] = []
    applied_profile_updates = 0
    if profile_updates:
        try:
            profile, version = await client.get_profile(external_user_id)
            for attempt in range(2):
                merged = _apply_profile_updates(profile, profile_updates)
                if merged == normalize_user_profile(profile):
                    break
                try:
                    await client.put_profile(external_user_id, merged, version)
                    applied_profile_updates = len(profile_updates)
                    break
                except ContextGatewayError as exc:
                    if exc.code != "CONTEXT_VERSION_CONFLICT" or attempt:
                        raise
                    profile, version = await client.get_profile(external_user_id)
        except Exception as exc:
            errors.append(f"profile:{type(exc).__name__}")

    created_memories = 0
    updated_memories = 0
    duplicate_memories = 0
    rejected_memory_references = 0
    write_errors: list[str] = []
    memory_vector_sync_results: list[dict[str, Any]] = []
    if memories:
        try:
            existing: set[str] = set()
            existing_by_id: dict[str, dict[str, Any]] = {}
            max_pages = min(20, max(1, int(os.getenv("GATEWAY_CONTEXT_MEMORY_MAX_PAGES", "5"))))
            for page in range(1, max_pages + 1):
                payload = await client.get_memory_page(external_user_id, page, page_size=50)
                entries = _memory_items(payload)
                existing.update(
                    _scoped_memory_content_key(item["content"], item.get("metadata"))
                    for item in entries
                    if isinstance(item, dict) and isinstance(item.get("content"), str)
                )
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = entry.get("id", entry.get("memory_id", entry.get("uuid")))
                    if isinstance(entry_id, str):
                        existing_by_id[entry_id] = entry
                if not entries or not _has_next_page(payload, page, max_pages):
                    break
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            suffix = f":{status_code}" if isinstance(status_code, int) else ""
            write_errors.append(f"memory_read:{type(exc).__name__}{suffix}")
        else:
            for item in memories:
                key = item["content"].casefold()
                target: dict[str, Any] | None = None
                expected_version: int | None = None
                project: dict[str, Any] | None = None
                if item["scope"] == "project":
                    try:
                        project = await resolve_or_create_user_project(
                            external_user_id, item["project_name"]
                        )
                    except Exception as exc:
                        write_errors.append(f"project_resolve:{type(exc).__name__}")
                        continue
                scope_key = _scoped_memory_content_key(
                    item["content"],
                    {
                        "scope": item["scope"],
                        "subject_project_id": project["id"] if project else None,
                    },
                )
                if item["action"] == "create" and scope_key in existing:
                    duplicate_memories += 1
                    continue
                if item["action"] == "update":
                    if item["memory_id"] not in visible_memory_ids:
                        rejected_memory_references += 1
                        continue
                    target = existing_by_id.get(item["memory_id"])
                    version_value = target.get("version", target.get("memory_version")) if target else None
                    if target is None or not isinstance(version_value, int):
                        rejected_memory_references += 1
                        continue
                    expected_version = version_value
                    old_content = target.get("content")
                    old_metadata = target.get("metadata")
                    if not isinstance(old_metadata, dict):
                        old_metadata = target.get("metadata_json")
                    if not isinstance(old_metadata, dict):
                        old_metadata = {}
                    if (
                        isinstance(old_content, str)
                        and old_content.strip().casefold() == key
                        and _scoped_memory_content_key(old_content, old_metadata) == scope_key
                        and old_metadata.get("memory_role") == item["memory_role"]
                    ):
                        duplicate_memories += 1
                        continue
                try:
                    metadata = {
                        "memory_type": item["memory_type"],
                        "memory_role": item["memory_role"],
                        "source": "llm_extraction",
                        "confidence": item["confidence"],
                        "valid_until": item["valid_until"],
                        "schema_version": 3,
                        "scope": item["scope"],
                        "subject_project_id": project["id"] if project else None,
                        "project_name": project["name"] if project else None,
                    }
                    tags = [*item["tags"], "模型提取"]
                    if item["action"] == "update":
                        old_metadata = target.get("metadata") if isinstance(target, dict) else None
                        if not isinstance(old_metadata, dict):
                            old_metadata = target.get("metadata_json") if isinstance(target, dict) else None
                        metadata = {**(old_metadata if isinstance(old_metadata, dict) else {}), **metadata}
                        if item["occurred_at"] is not None:
                            metadata["occurred_at"] = item["occurred_at"]
                        else:
                            metadata.setdefault("occurred_at", None)
                        metadata["updated_at"] = _memory_created_at()
                        saved_memory = await client.update_memory(
                            external_user_id,
                            item["memory_id"],
                            item["content"],
                            tags,
                            metadata,
                            expected_version,
                        )
                        updated_memories += 1
                        vector_result, vector_error = await _sync_memory_vector_best_effort(
                            external_user_id, saved_memory
                        )
                        memory_vector_sync_results.append({
                            "action": "update",
                            "memory_id": saved_memory.get("id", item["memory_id"]),
                            "status": "failed" if vector_error else ("synced" if vector_result else "skipped"),
                            "gateway_response": vector_result,
                            "error": vector_error,
                        })
                        if vector_error:
                            write_errors.append(f"memory_vector_sync:{vector_error.get('code') or vector_error['type']}")
                        if isinstance(target, dict) and isinstance(target.get("content"), str):
                            existing.discard(
                                _scoped_memory_content_key(
                                    target["content"], target.get("metadata")
                                )
                            )
                    else:
                        metadata["occurred_at"] = item["occurred_at"]
                        metadata["created_at"] = _memory_created_at()
                        saved_memory = await client.create_memory(
                            external_user_id, item["content"], tags, metadata
                        )
                        created_memories += 1
                        vector_result, vector_error = await _sync_memory_vector_best_effort(
                            external_user_id, saved_memory
                        )
                        memory_vector_sync_results.append({
                            "action": "create",
                            "memory_id": saved_memory.get("id"),
                            "content": item["content"],
                            "status": "failed" if vector_error else ("synced" if vector_result else "skipped"),
                            "gateway_response": vector_result,
                            "error": vector_error,
                        })
                        if vector_error:
                            write_errors.append(f"memory_vector_sync:{vector_error.get('code') or vector_error['type']}")
                    existing.add(scope_key)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    suffix = f":{status_code}" if isinstance(status_code, int) else ""
                    write_errors.append(f"memory_write:{type(exc).__name__}{suffix}")
    accepted_memory_count = len(memories)
    stats = {
        "profile_candidates": len(raw_profile_updates) if isinstance(raw_profile_updates, list) else 0,
        "profile_updates_accepted": len(profile_updates),
        "profile_updates_written": applied_profile_updates,
        "profile_rejected_evidence": profile_rejected["evidence"],
        "profile_rejected_confidence": profile_rejected["confidence"],
        "profile_rejected_schema": profile_rejected["schema"],
        "profile_rejected_sensitive": profile_rejected["sensitive"],
        "memory_candidates": len(raw_memories) if isinstance(raw_memories, list) else 0,
        "memory_candidates_accepted": accepted_memory_count,
        "memory_rejected_evidence": rejected["evidence"],
        "memory_rejected_confidence": rejected["confidence"],
        "memory_rejected_content": rejected["content"],
        "memory_rejected_sensitive": rejected["sensitive"],
        "memory_rejected_profile_overlap": rejected["profile_overlap"],
        "memory_rejected_reference": rejected_memory_references,
        "memory_duplicates": duplicate_memories,
        "long_term_memories_written": created_memories,
        "long_term_memories_updated": updated_memories,
        "memory_vector_sync_results": memory_vector_sync_results,
        "validated_memories": [
            {
                "action": item["action"],
                "memory_type": item["memory_type"],
                "memory_role": item["memory_role"],
                "scope": item["scope"],
                "content": item["content"],
                "tags": item["tags"],
            }
            for item in memories
        ],
        "validated_profile_updates": profile_updates,
    }
    return stats, errors + write_errors


def explicit_memory_request(text: str) -> str | None:
    """只接受用户明确要求记住的条目，并过滤常见凭据和直接身份信息。"""

    match = re.match(r"^\s*(?:请)?(?:记住|记一下|帮我记住)\s*[：:，,]?\s*(.{3,300})\s*$", text)
    if not match:
        return None
    content = match.group(1).strip()
    sensitive = re.compile(
        r"(?i)(password|api[-_ ]?key|token|密码|密钥|身份证|身份证号|手机号|手机号码|银行卡|住址|家庭住址)"
    )
    if sensitive.search(content) or re.search(r"\b\d{11}\b", content):
        return None
    return content


def explicit_profile_patch(text: str) -> dict[str, Any]:
    """仅从明确、稳定的表达偏好语句提取当前项目画像字段。"""

    value = text.strip()
    if not re.search(r"(?:以后|今后|请一直|默认)", value):
        return {}
    patch: dict[str, Any] = {}
    if re.search(r"中文.{0,8}(?:回答|回复)|(?:用|使用|请用|请使用)中文回答|偏好中文", value):
        patch["preferred_language"] = "zh-CN"
    elif re.search(r"英文.{0,8}(?:回答|回复)|(?:用|使用|请用|请使用)英文回答|偏好英文", value):
        patch["preferred_language"] = "en-US"
    if re.search(r"(?:简洁|简短|精炼).{0,12}(?:回答|回复)|(?:回答|回复).{0,12}(?:简洁|简短|精炼)", value):
        patch["response_style"] = "concise"
    return patch


def message_pair_hash(messages: list[dict[str, str]]) -> str:
    """对一轮消息计算不可逆摘要，用于网关追加超时后的尾部核对。"""

    import hashlib

    raw = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _terms(text: str) -> set[str]:
    normalized = text.casefold()
    terms = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _memory_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("memories", "items", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("memories", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _has_next_page(payload: Any, current_page: int, max_pages: int) -> bool:
    if current_page >= max_pages or not isinstance(payload, dict):
        return False
    if isinstance(payload.get("data"), dict):
        return _has_next_page(payload["data"], current_page, max_pages)
    if isinstance(payload.get("has_next"), bool):
        return payload["has_next"]
    if isinstance(payload.get("total_pages"), int):
        return current_page < payload["total_pages"]
    if isinstance(payload.get("total"), int):
        page_size = int(payload.get("page_size", 50))
        return current_page * page_size < payload["total"]
    return len(_memory_items(payload)) >= 50


async def persist_explicit_preferences(
    client: ContextGatewayClient, external_user_id: str, text: str
) -> None:
    """持久化显式请求的稳定信息；画像冲突时重读、合并并最多重试一次。"""

    patch = explicit_profile_patch(text)
    memory = None if patch else explicit_memory_request(text)
    if memory:
        max_pages = min(20, max(1, int(os.getenv("GATEWAY_CONTEXT_MEMORY_MAX_PAGES", "5"))))
        already_saved = False
        for page in range(1, max_pages + 1):
            payload = await client.get_memory_page(external_user_id, page, page_size=50)
            entries = _memory_items(payload)
            if any(isinstance(entry, dict) and entry.get("content") == memory for entry in entries):
                already_saved = True
                break
            if not entries or not _has_next_page(payload, page, max_pages):
                break
        if not already_saved:
            saved_memory = await client.create_memory(
                external_user_id,
                memory,
                ["用户明确记录"],
                {
                    "memory_type": "semantic",
                    "source": "explicit_user_request",
                    "confidence": 1.0,
                    "memory_role": "constraint" if _memory_role({"content": memory}) == "constraint" else "fact",
                    "schema_version": 3,
                    "occurred_at": None,
                    "created_at": _memory_created_at(),
                },
            )
            _vector_result, vector_error = await _sync_memory_vector_best_effort(
                external_user_id, saved_memory
            )
            if vector_error:
                logger.warning(
                    "memory_vector_sync_failed code=%s status=%s",
                    vector_error.get("code", vector_error.get("type")),
                    vector_error.get("http_status", "unavailable"),
                )

    if not patch:
        return
    profile, version = await client.get_profile(external_user_id)
    if not profile:
        version = None
    for attempt in range(2):
        merged = {**profile, **patch}
        try:
            await client.put_profile(external_user_id, merged, version)
            return
        except ContextGatewayError as exc:
            if exc.code != "CONTEXT_VERSION_CONFLICT" or attempt:
                raise
            profile, version = await client.get_profile(external_user_id)
            if not profile:
                version = None
