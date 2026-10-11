"""Create the Agent's default Gateway prompts when they are missing."""

from __future__ import annotations

from pathlib import Path

from services.context_gateway.client import ContextGatewayClient, get_context_gateway
from services.context_gateway.templates import (
    ANSWER_TEMPLATE_CATEGORY,
    ANSWER_TEMPLATE_NAME,
    MEMORY_TEMPLATE_CATEGORY,
    MEMORY_TEMPLATE_NAME,
)


_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


async def ensure_default_templates(
    client: ContextGatewayClient | None = None,
) -> dict[str, str]:
    """Create missing templates and synchronize changed local seed prompts.

    The maintained prompt files are authoritative: startup updates Gateway only
    when their contents differ, using Gateway's optimistic version check.
    """

    gateway = client or get_context_gateway()
    templates = (
        (
            ANSWER_TEMPLATE_CATEGORY,
            ANSWER_TEMPLATE_NAME,
            _read_prompt("answer_system.md"),
        ),
        (
            MEMORY_TEMPLATE_CATEGORY,
            MEMORY_TEMPLATE_NAME,
            _read_prompt("memory_extraction_system.md"),
        ),
    )
    created: dict[str, bool] = {}
    for category, name, content in templates:
        created[f"{category}/{name}"] = await gateway.ensure_template(
            category, name, content
        )
    return created


def _read_prompt(filename: str) -> str:
    """Read a maintained UTF-8 default prompt bundled with the backend."""

    content = (_PROMPT_DIR / filename).read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError("DEFAULT_PROMPT_EMPTY")
    return content
