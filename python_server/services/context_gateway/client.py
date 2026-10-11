"""Agent Gateway 项目上下文 API 的服务端适配器。"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from gateway_client import GatewayContextError as _GatewayContextError

from services.gateway import create_gateway_client
from services.context_gateway.templates import ANSWER_TEMPLATE_CATEGORY, ANSWER_TEMPLATE_NAME


class ContextGatewayError(RuntimeError):
    """上下文网关调用失败；只暴露脱敏错误类别和 HTTP 状态。"""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _normalize_sdk_error(error: _GatewayContextError) -> ContextGatewayError:
    """Keep this project's established error codes while exposing SDK failures."""

    code = {
        "GATEWAY_CONTEXT_VERSION_CONFLICT": "CONTEXT_VERSION_CONFLICT",
        "GATEWAY_API_KEY_INVALID": "CONTEXT_AUTH_FAILED",
        "GATEWAY_CONTEXT_ACCESS_DENIED": "CONTEXT_AUTH_FAILED",
        "GATEWAY_CONTEXT_NOT_FOUND": "CONTEXT_RESOURCE_NOT_FOUND",
        "GATEWAY_BASE_URL_NOT_CONFIGURED": "CONTEXT_SERVICE_NOT_CONFIGURED",
        "GATEWAY_CONTEXT_TIMEOUT": "CONTEXT_SERVICE_TIMEOUT",
        "GATEWAY_CONTEXT_NETWORK_ERROR": "CONTEXT_SERVICE_NETWORK_ERROR",
    }.get(error.code, error.code)
    return ContextGatewayError(code, error.status_code)


class ContextGatewayClient:
    """业务适配层：已由 SDK 覆盖的 Context 操作统一委托 SDK。

    模板读写和短期消息追加当前尚未由 SDK 封装，暂留在本适配层的兼容请求中。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.transport = transport
        self._sdk = create_gateway_client(
            base_url=base_url,
            api_key=api_key,
            transport=transport,
        )
        # Reuse the SDK-normalized API root and credential for the few context
        # routes not exposed by the current SDK (templates and message append).
        self.base_url = f"{self._sdk.context.base_url}/context"
        self.api_key = self._sdk.context.api_key

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

    async def get_template(
        self,
        category: str = ANSWER_TEMPLATE_CATEGORY,
        name: str = ANSWER_TEMPLATE_NAME,
    ) -> str:
        """Read a project template by the stable category/name contract."""

        result = await self.get_template_resource(category, name)
        content = _template_content(result)
        if not content:
            raise ContextGatewayError("CONTEXT_TEMPLATE_INVALID")
        return content

    async def get_template_resource(self, category: str, name: str) -> Any:
        """Fetch one Gateway template resource without interpreting its content."""

        path = f"/templates/{quote(category, safe='')}/{quote(name, safe='')}"
        return await self._request("GET", path)

    async def ensure_template(self, category: str, name: str, content: str) -> str:
        """Create or synchronize one Gateway prompt with the local seed.

        Existing Gateway templates are updated only when their content differs.
        Updates use the version read from Gateway; on a conflict we reread once
        and retry only if the latest content still differs from the seed.
        """

        try:
            resource = await self.get_template_resource(category, name)
        except ContextGatewayError as exc:
            if exc.status_code != 404:
                raise
            try:
                await self._request(
                    "POST",
                    "/templates",
                    json_body={"category": category, "name": name, "content": content},
                )
                return "created"
            except ContextGatewayError as create_exc:
                if create_exc.status_code != 409:
                    raise
                # Another instance may have created the named template first.
                resource = await self.get_template_resource(category, name)

        if _template_content(resource) == content.strip():
            return "unchanged"

        for attempt in range(2):
            template_id, version = _template_identity(resource)
            try:
                await self._request(
                    "PATCH",
                    f"/templates/{quote(template_id, safe='')}",
                    json_body={
                        "expected_version": version,
                        "name": name,
                        "content": content,
                    },
                )
                return "updated"
            except ContextGatewayError as exc:
                if exc.status_code != 409 or attempt:
                    raise
                resource = await self.get_template_resource(category, name)
                if _template_content(resource) == content.strip():
                    return "unchanged"
        raise ContextGatewayError("CONTEXT_TEMPLATE_UPDATE_FAILED")

    async def get_messages(self, external_user_id: str, session_id: str, limit: int = 50) -> list[dict[str, str]]:
        """读取会话最近消息；最多读取 API 允许的 100 条。"""

        try:
            result = await self._sdk.context.list_session_messages(
                external_user_id, session_id, min(100, max(1, limit))
            )
        except _GatewayContextError as error:
            exc = _normalize_sdk_error(error)
            # A never-used short-term session may have no message resource yet.
            if exc.status_code == 404:
                return []
            raise exc from error
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

        try:
            return await self._sdk.context.list_memories(
                external_user_id, page, min(100, max(1, page_size))
            )
        except _GatewayContextError as error:
            raise _normalize_sdk_error(error) from error

    async def create_memory(
        self,
        external_user_id: str,
        content: str,
        tags: list[str],
        metadata: dict[str, Any],
    ) -> Any:
        """保存用户明确要求记住的稳定偏好或事实。"""

        try:
            return await self._sdk.context.create_memory(external_user_id, content, tags, metadata)
        except _GatewayContextError as error:
            raise _normalize_sdk_error(error) from error

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

        try:
            return await self._sdk.context.update_memory(
                external_user_id,
                memory_id,
                expected_version=expected_version,
                content=content,
                tags=tags,
                metadata=metadata,
            )
        except _GatewayContextError as error:
            raise _normalize_sdk_error(error) from error

    async def get_profile(self, external_user_id: str) -> tuple[dict[str, Any], int | None]:
        """读取项目定义的当前用户画像及版本号。"""

        try:
            result = await self._sdk.context.get_profile(external_user_id)
        except _GatewayContextError as error:
            exc = _normalize_sdk_error(error)
            # A new account has no profile until the first explicit preference is saved.
            if exc.status_code == 404:
                return {}, None
            raise exc from error
        body = result.get("profile", {}) if isinstance(result, dict) else {}
        if not isinstance(body, dict):
            raise ContextGatewayError("CONTEXT_PROFILE_INVALID")
        version_source = result
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

        try:
            return await self._sdk.context.put_profile(external_user_id, profile, expected_version)
        except _GatewayContextError as error:
            raise _normalize_sdk_error(error) from error


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


def _template_content(result: Any) -> str | None:
    """Extract template content from the current Gateway response envelope."""

    body = result
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    if isinstance(body, dict):
        body = body.get("template", body)
    if isinstance(body, dict):
        content = body.get("content") or body.get("template_content") or body.get("prompt")
    else:
        content = body
    return content.strip() if isinstance(content, str) and content.strip() else None


def _template_identity(result: Any) -> tuple[str, int]:
    """Extract the resource ID and optimistic version required by Gateway PATCH."""

    body = result
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    if isinstance(body, dict):
        body = body.get("template", body)
    if not isinstance(body, dict):
        raise ContextGatewayError("CONTEXT_TEMPLATE_INVALID")
    template_id = body.get("id", body.get("template_id"))
    version = body.get("version")
    if not isinstance(template_id, str) or not template_id.strip():
        raise ContextGatewayError("CONTEXT_TEMPLATE_INVALID")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ContextGatewayError("CONTEXT_TEMPLATE_INVALID")
    return template_id, version


def get_context_gateway() -> ContextGatewayClient:
    """在请求边界创建轻量客户端，密钥始终只从后端环境读取。"""

    return ContextGatewayClient()
