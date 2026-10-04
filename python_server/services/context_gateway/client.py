"""Agent Gateway 项目上下文 API 的服务端适配器。"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx


class ContextGatewayError(RuntimeError):
    """上下文网关调用失败；只暴露脱敏错误类别和 HTTP 状态。"""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class ContextGatewayClient:
    """封装提示词模板、短期消息、长期记忆和用户画像接口。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else os.getenv("GATEWAY_CONTEXT_BASE_URL", "")).strip().rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("GATEWAY_PROJECT_API_KEY", "")
        self.transport = transport

    def _configuration(self) -> tuple[str, str]:
        if not self.base_url or not self.api_key:
            raise ContextGatewayError("CONTEXT_SERVICE_NOT_CONFIGURED")
        return self.base_url, self.api_key

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        base_url, api_key = self._configuration()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=3.0),
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{base_url}/{path.lstrip('/')}",
                    headers=headers,
                    params=params,
                    json=json_body,
                )
        except httpx.TimeoutException as exc:
            raise ContextGatewayError("CONTEXT_SERVICE_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ContextGatewayError("CONTEXT_SERVICE_NETWORK_ERROR") from exc

        if response.status_code == 409:
            raise ContextGatewayError("CONTEXT_VERSION_CONFLICT", 409)
        if response.status_code in {401, 403}:
            raise ContextGatewayError("CONTEXT_AUTH_FAILED", response.status_code)
        if response.status_code == 404:
            raise ContextGatewayError("CONTEXT_RESOURCE_NOT_FOUND", 404)
        if response.status_code >= 400:
            raise ContextGatewayError("CONTEXT_SERVICE_ERROR", response.status_code)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ContextGatewayError("CONTEXT_INVALID_RESPONSE", response.status_code) from exc

    async def get_template(self) -> str:
        """读取当前配置的项目 system 模板，未配置或未找到时明确失败。"""

        template_id = os.getenv("GATEWAY_CONTEXT_TEMPLATE_ID", "").strip()
        if template_id:
            path = f"/templates/by-id/{quote(template_id, safe='')}"
        else:
            category = os.getenv("GATEWAY_CONTEXT_TEMPLATE_CATEGORY", "system").strip()
            name = os.getenv("GATEWAY_CONTEXT_TEMPLATE_NAME", "agent").strip()
            path = f"/templates/{quote(category, safe='')}/{quote(name, safe='')}"
        result = await self._request("GET", path)
        body = result
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            body = body["data"]
        if isinstance(body, dict):
            body = body.get("template", body)
        if isinstance(body, dict):
            content = body.get("content") or body.get("template_content") or body.get("prompt")
        else:
            content = body
        if not isinstance(content, str) or not content.strip():
            raise ContextGatewayError("CONTEXT_TEMPLATE_INVALID")
        return content

    async def get_messages(self, external_user_id: str, session_id: str, limit: int = 50) -> list[dict[str, str]]:
        """读取会话最近消息；最多读取 API 允许的 100 条。"""

        try:
            result = await self._request(
                "GET",
                f"/users/{quote(external_user_id, safe='')}/sessions/{quote(session_id, safe='')}/messages",
                params={"limit": min(100, max(1, limit))},
            )
        except ContextGatewayError as exc:
            # A never-used short-term session may have no message resource yet.
            if exc.status_code == 404:
                return []
            raise
        raw = _list_payload(result, ("messages", "items"))
        return [
            {"role": item["role"], "content": item["content"]}
            for item in raw
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ]

    async def append_messages(
        self, external_user_id: str, session_id: str, messages: list[dict[str, str]]
    ) -> None:
        """一次性追加一轮 user/assistant 消息，短期记忆 TTL 由环境配置决定。"""

        ttl = min(30 * 24 * 3600, max(60, int(os.getenv("GATEWAY_CONTEXT_TTL_SECONDS", "86400"))))
        await self._request(
            "POST",
            f"/users/{quote(external_user_id, safe='')}/sessions/{quote(session_id, safe='')}/messages",
            json_body={"messages": messages, "ttl_seconds": ttl},
        )

    async def get_memory_page(self, external_user_id: str, page: int, page_size: int = 50) -> Any:
        """读取一页用户级长期记忆。"""

        return await self._request(
            "GET",
            f"/users/{quote(external_user_id, safe='')}/memories",
            params={"page": page, "page_size": min(100, max(1, page_size))},
        )

    async def create_memory(
        self,
        external_user_id: str,
        content: str,
        tags: list[str],
        metadata: dict[str, Any],
    ) -> Any:
        """保存用户明确要求记住的稳定偏好或事实。"""

        return await self._request(
            "POST",
            f"/users/{quote(external_user_id, safe='')}/memories",
            json_body={"content": content, "tags": tags, "metadata": metadata},
        )

    async def update_memory(
        self,
        external_user_id: str,
        memory_id: str,
        content: str,
        tags: list[str],
        metadata: dict[str, Any],
        expected_version: int,
    ) -> Any:
        """Update one existing long-term memory using optimistic version control."""

        return await self._request(
            "PATCH",
            f"/users/{quote(external_user_id, safe='')}/memories/{quote(memory_id, safe='')}",
            json_body={
                "content": content,
                "tags": tags,
                "metadata": metadata,
                "expected_version": expected_version,
            },
        )

    async def get_profile(self, external_user_id: str) -> tuple[dict[str, Any], int | None]:
        """读取项目定义的当前用户画像及版本号。"""

        try:
            result = await self._request("GET", f"/users/{quote(external_user_id, safe='')}/profile")
        except ContextGatewayError as exc:
            # A new account has no profile until the first explicit preference is saved.
            if exc.status_code == 404:
                return {}, None
            raise
        body = result.get("data", result) if isinstance(result, dict) else {}
        if isinstance(body, dict):
            body = body.get("profile", body)
        if not isinstance(body, dict):
            raise ContextGatewayError("CONTEXT_PROFILE_INVALID")
        version_source = result.get("data", result) if isinstance(result, dict) else {}
        version = (
            version_source.get("version", version_source.get("profile_version"))
            if isinstance(version_source, dict)
            else None
        )
        return body, int(version) if version is not None else None

    async def put_profile(
        self, external_user_id: str, profile: dict[str, Any], expected_version: int | None
    ) -> Any:
        """创建画像或按最近读取版本替换；冲突由调用方重新读取并合并。"""

        body: dict[str, Any] = {"profile": profile}
        if expected_version is not None:
            body["expected_version"] = expected_version
        return await self._request(
            "PUT", f"/users/{quote(external_user_id, safe='')}/profile", json_body=body
        )


def _list_payload(result: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            return value
    data = result.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def get_context_gateway() -> ContextGatewayClient:
    """在请求边界创建轻量客户端，密钥始终只从后端环境读取。"""

    return ContextGatewayClient()
