"""从 Gateway 读取并组装每轮 LLM 上下文，且只按明确规则写入记忆。"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import Any

from services.context_gateway.client import ContextGatewayClient, ContextGatewayError


async def load_context(
    client: ContextGatewayClient, external_user_id: str, session_id: str, current_text: str
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
    for page in range(1, max_pages + 1):
        payload = await client.get_memory_page(external_user_id, page, page_size=50)
        items = _memory_items(payload)
        memories.extend(item for item in items if isinstance(item, dict) and isinstance(item.get("content"), str))
        if not items or not _has_next_page(payload, page, max_pages):
            break

    selected = select_relevant_memories(memories, current_text)
    memory_text = "\n".join(f"- {item['content']}" for item in selected)
    profile_text = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))[:4000]
    variables = {"profile": profile_text, "long_term_memories": memory_text}
    profile_is_template_variable = "{{profile}}" in template
    memories_are_template_variable = "{{long_term_memories}}" in template
    system_prompt = render_template(template, variables)
    if profile and not profile_is_template_variable:
        system_prompt += "\n\n当前用户画像（仅作背景信息）：\n" + profile_text
    if memory_text and not memories_are_template_variable:
        system_prompt += "\n\n与当前问题相关的长期记忆（仅作背景信息）：\n" + memory_text
    return system_prompt, history[-50:], profile, selected


def render_template(template: str, variables: dict[str, str]) -> str:
    """仅替换当前项目明确提供的模板变量，未知变量原样保留。"""

    pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
    return pattern.sub(lambda match: variables.get(match.group(1), match.group(0)), template)


def select_relevant_memories(
    memories: list[dict[str, Any]], query: str, limit: int = 4
) -> list[dict[str, Any]]:
    """用轻量词项重叠筛选 Gateway 分页结果，不假设其提供语义检索。"""

    query_terms = _terms(query)
    if not query_terms:
        return []
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(memories):
        content = item.get("content", "")
        tags = item.get("tags", [])
        searchable = content + " " + " ".join(tag for tag in tags if isinstance(tag, str))
        overlap = len(query_terms & _terms(searchable))
        if overlap:
            ranked.append((overlap, -index, item))
    ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [entry[2] for entry in ranked[:limit]]


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


def _validated_profile_updates(
    candidates: Any, user_messages: list[str], min_confidence: float
) -> list[dict[str, str]]:
    if not isinstance(candidates, list):
        return []
    valid: list[dict[str, str]] = []
    for item in candidates[:10]:
        if not isinstance(item, dict) or not _evidence_is_user_sourced(item.get("evidence"), user_messages):
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        field = item.get("field")
        action = item.get("action")
        value = item.get("value")
        if not math.isfinite(confidence) or not min_confidence <= confidence <= 1:
            continue
        if not isinstance(field, str) or not isinstance(action, str):
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[:80]
        if _is_sensitive(value):
            continue
        if field == "preferred_language" and action == "set" and value in PROFILE_LANGUAGE_VALUES:
            valid.append({"field": field, "action": action, "value": value})
        elif field == "response_style" and action == "set" and value in PROFILE_STYLE_VALUES:
            valid.append({"field": field, "action": action, "value": value})
        elif field in PROFILE_LIST_FIELDS and action in {"add", "remove"}:
            valid.append({"field": field, "action": action, "value": value})
    return valid


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
) -> tuple[dict[str, int], list[str]]:
    """校验证据、置信度与画像 schema 后，写入候选画像更新及长期记忆。"""

    evidence_messages = [*recent_user_messages[-6:], current_user_text]
    try:
        min_confidence = max(
            0.5, min(1.0, float(os.getenv("AGENT_MEMORY_MIN_CONFIDENCE", "0.75")))
        )
    except ValueError:
        min_confidence = 0.75
    raw_profile_updates = extraction.get("profile_updates")
    profile_updates = _validated_profile_updates(
        raw_profile_updates, evidence_messages, min_confidence=min_confidence
    )
    raw_memories = extraction.get("long_term_memories")
    memories: list[dict[str, Any]] = []
    rejected = {"evidence": 0, "confidence": 0, "content": 0, "sensitive": 0}
    if isinstance(raw_memories, list):
        for item in raw_memories[:5]:
            if not isinstance(item, dict):
                rejected["content"] += 1
                continue
            if not _evidence_is_user_sourced(item.get("evidence"), evidence_messages):
                rejected["evidence"] += 1
                continue
            content = item.get("content")
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                rejected["confidence"] += 1
                continue
            if not math.isfinite(confidence) or not min_confidence <= confidence <= 1:
                rejected["confidence"] += 1
                continue
            if not isinstance(content, str) or not 3 <= len(content.strip()) <= 300:
                rejected["content"] += 1
                continue
            content = content.strip()
            if _is_sensitive(content) or _is_sensitive(item["evidence"]):
                rejected["sensitive"] += 1
                continue
            proposed_tags = _clean_string_list(item.get("tags"), limit=5, max_length=32)
            tags = [tag for tag in proposed_tags if tag in ALLOWED_MEMORY_TAGS] or ["其他"]
            memories.append({"content": content, "tags": tags, "confidence": min(1.0, confidence)})

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
    duplicate_memories = 0
    write_errors: list[str] = []
    if memories:
        try:
            existing: set[str] = set()
            max_pages = min(20, max(1, int(os.getenv("GATEWAY_CONTEXT_MEMORY_MAX_PAGES", "5"))))
            for page in range(1, max_pages + 1):
                payload = await client.get_memory_page(external_user_id, page, page_size=50)
                entries = _memory_items(payload)
                existing.update(
                    item["content"].strip().casefold()
                    for item in entries
                    if isinstance(item, dict) and isinstance(item.get("content"), str)
                )
                if not entries or not _has_next_page(payload, page, max_pages):
                    break
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            suffix = f":{status_code}" if isinstance(status_code, int) else ""
            write_errors.append(f"memory_read:{type(exc).__name__}{suffix}")
        else:
            for item in memories:
                key = item["content"].casefold()
                if key in existing:
                    duplicate_memories += 1
                    continue
                try:
                    await client.create_memory(
                        external_user_id,
                        item["content"],
                        [*item["tags"], "模型提取"],
                        {"source": "llm_extraction", "confidence": item["confidence"], "schema_version": 1},
                    )
                    existing.add(key)
                    created_memories += 1
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    suffix = f":{status_code}" if isinstance(status_code, int) else ""
                    write_errors.append(f"memory_write:{type(exc).__name__}{suffix}")
    accepted_memory_count = len(memories)
    stats = {
        "profile_candidates": len(raw_profile_updates) if isinstance(raw_profile_updates, list) else 0,
        "profile_updates_accepted": len(profile_updates),
        "profile_updates_written": applied_profile_updates,
        "memory_candidates": len(raw_memories) if isinstance(raw_memories, list) else 0,
        "memory_candidates_accepted": accepted_memory_count,
        "memory_rejected_evidence": rejected["evidence"],
        "memory_rejected_confidence": rejected["confidence"],
        "memory_rejected_content": rejected["content"],
        "memory_rejected_sensitive": rejected["sensitive"],
        "memory_duplicates": duplicate_memories,
        "long_term_memories_written": created_memories,
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
            await client.create_memory(
                external_user_id,
                memory,
                ["用户明确记录"],
                {"source": "explicit_user_request", "confidence": 1.0},
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
