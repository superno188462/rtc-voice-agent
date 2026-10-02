"""场景读取、兼容校验和响应包装工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable


class ContractError(ValueError):
    """输入或场景配置不满足原服务契约。"""


def assert_value(value: Any, message: str) -> None:
    """复刻 Node util.assert：空值或包含空格的字符串视为无效。"""

    if not value or (isinstance(value, str) and " " in value):
        raise ContractError(message)


def read_files(directory: Path, suffix: str = ".json") -> dict[str, dict[str, Any]]:
    """启动时读取目录内的场景 JSON，以文件名作为稳定场景 ID。"""

    scenes: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix == suffix and not path.name.endswith(".example.json"):
            with path.open("r", encoding="utf-8") as file:
                scenes[path.stem] = json.load(file)
    return scenes


async def wrapper(
    action: str,
    logic: Callable[[], Awaitable[Any]],
    contain_response_metadata: bool = True,
) -> dict[str, Any]:
    """执行逻辑并生成与 Node wrapper 一致的响应。"""

    metadata: dict[str, Any] = {"Action": action}
    try:
        result = await logic()
        if contain_response_metadata:
            return {"ResponseMetadata": metadata, "Result": result}
        return result
    except Exception as exc:  # noqa: BLE001 - 契约要求统一转换所有异常
        metadata["Error"] = {"Code": -1, "Message": f"Error: {exc}"}
        return {"ResponseMetadata": metadata}
