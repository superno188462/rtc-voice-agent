"""对话项目实体的用户隔离目录与名称解析。"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from services.auth.database import get_session_factory
from services.context_gateway.models import UserProject


def normalize_project_name(name: str) -> str:
    """规范化项目名称用于精确去重，不为名称生成或推断业务 ID。"""

    normalized = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)


def project_payload(project: UserProject) -> dict:
    """返回可安全提供给记忆分析模型的项目名称信息，不暴露数据库对象。"""

    return {
        "id": project.id,
        "name": project.name,
        "aliases": list(project.aliases or []),
    }


async def list_user_projects(user_id: str) -> list[dict]:
    """读取身份中心用户自己的项目目录，按最近更新时间排序。"""

    factory = get_session_factory()
    async with factory() as db:
        rows = await db.scalars(
            select(UserProject).where(UserProject.user_id == user_id).order_by(UserProject.updated_at.desc())
        )
        return [project_payload(row) for row in rows]


async def resolve_or_create_user_project(user_id: str, name: str) -> dict:
    """按用户和规范化名称解析项目；首次出现时由程序生成 UUID。"""

    clean_name = " ".join(name.strip().split())[:120]
    normalized = normalize_project_name(clean_name)
    if not clean_name or not normalized:
        raise ValueError("PROJECT_NAME_REQUIRED")
    project_id = str(uuid.uuid4())
    factory = get_session_factory()
    async with factory.begin() as db:
        await db.execute(
            insert(UserProject)
            .values(
                id=project_id,
                user_id=user_id,
                name=clean_name,
                normalized_name=normalized,
                aliases=[clean_name],
                updated_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(
                index_elements=[UserProject.user_id, UserProject.normalized_name]
            )
        )
        project = await db.scalar(
            select(UserProject).where(
                UserProject.user_id == user_id,
                UserProject.normalized_name == normalized,
            )
        )
        if project is None:
            raise RuntimeError("AGENT_PROJECT_RESOLUTION_FAILED")
        project.updated_at = datetime.now(timezone.utc)
        return project_payload(project)


def _project_mentions(projects: list[dict], text: str) -> list[tuple[int, int, str]]:
    normalized_text = normalize_project_name(text)
    matches: list[tuple[int, int, str]] = []
    for project in projects:
        project_id = project.get("id")
        if not isinstance(project_id, str):
            continue
        names = [project.get("name"), *(project.get("aliases") or [])]
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            normalized_name = normalize_project_name(name)
            if not normalized_name:
                continue
            if normalized_name.isascii() and normalized_name.isalnum():
                positions = [
                    match.start()
                    for match in re.finditer(
                        rf"(?<![a-z0-9]){re.escape(normalized_name)}(?![a-z0-9])",
                        normalized_text,
                    )
                ]
                position = positions[-1] if positions else -1
            else:
                position = normalized_text.rfind(normalized_name)
            if position >= 0:
                matches.append((position, len(normalized_name), project_id))
    return matches


def resolve_project_ids(
    projects: list[dict], current_text: str, recent_context: str = ""
) -> list[str]:
    """Resolve all projects named in this turn; otherwise use the latest recent project."""

    current_matches = _project_mentions(projects, current_text)
    if current_matches:
        # Keep only longest aliases at a shared occurrence to avoid matching a
        # short alias and its full project name as two different projects.
        by_position: dict[int, list[tuple[int, int, str]]] = {}
        for match in current_matches:
            by_position.setdefault(match[0], []).append(match)
        selected_ids: list[str] = []
        for position_matches in by_position.values():
            longest = max(match[1] for match in position_matches)
            for match in position_matches:
                if match[1] == longest and match[2] not in selected_ids:
                    selected_ids.append(match[2])
        return selected_ids

    matches = _project_mentions(projects, recent_context)
    if not matches:
        return []
    latest_position = max(match[0] for match in matches)
    latest = [match for match in matches if match[0] == latest_position]
    longest = max(match[1] for match in latest)
    winners = {match[2] for match in latest if match[1] == longest}
    return list(winners) if len(winners) == 1 else []


def resolve_project_id(projects: list[dict], text: str) -> str | None:
    """Compatibility helper returning a single unambiguous project ID."""

    matches = resolve_project_ids(projects, text)
    return matches[0] if len(matches) == 1 else None
