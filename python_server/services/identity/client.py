"""Server-side client for the shared Identity Center API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from http.cookies import SimpleCookie
from typing import Any

import httpx


@dataclass(frozen=True)
class IdentityUser:
    """Canonical user returned by Identity Center after session verification."""

    id: str
    username: str
    role: str = "user"
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityAuthResult:
    """Authenticated user and opaque session value returned by the center."""

    user: IdentityUser
    session_cookie: str
    max_age: int | None = None


class IdentityCenterError(RuntimeError):
    """Safe, stable error raised for Identity Center failures."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class IdentityCenterClient:
    """Encapsulate all HTTP calls to Identity Center, including its opaque cookie."""

    def __init__(
        self, base_url: str, timeout_seconds: float = 3.0, transport: Any = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def register(self, username: str, password: str) -> IdentityAuthResult:
        return await self._authenticate("/api/auth/register", username, password)

    async def login(self, username: str, password: str) -> IdentityAuthResult:
        return await self._authenticate("/api/auth/login", username, password)

    async def logout(self, identity_session: str | None) -> None:
        if not identity_session:
            return
        response = await self._request(
            "POST", "/api/auth/logout", identity_session=identity_session
        )
        if response.status_code not in (200, 401):
            self._raise_for_response(response)

    async def current_user(self, identity_session: str | None) -> IdentityUser:
        if not identity_session:
            raise IdentityCenterError("AUTH_REQUIRED", 401)
        response = await self._request(
            "GET", "/api/auth/me", identity_session=identity_session
        )
        if response.status_code != 200:
            self._raise_for_response(response)
        return self._parse_user(response)

    async def _authenticate(
        self, path: str, username: str, password: str
    ) -> IdentityAuthResult:
        response = await self._request(
            "POST", path, json_body={"username": username, "password": password}
        )
        if response.status_code not in (200, 201):
            self._raise_for_response(response)
        try:
            session_cookie = response.cookies.get("identity_session")
            if not session_cookie:
                raise ValueError
            cookie = SimpleCookie()
            cookie.load(response.headers.get("set-cookie", ""))
            cookie_morsel = cookie.get("identity_session")
            raw_max_age = cookie_morsel["max-age"] if cookie_morsel else ""
            max_age = int(raw_max_age) if raw_max_age else None
            return IdentityAuthResult(
                user=self._parse_user(response),
                session_cookie=session_cookie,
                max_age=max_age,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise IdentityCenterError("IDENTITY_CENTER_BAD_RESPONSE", 502) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, str] | None = None,
        identity_session: str | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                cookies=(
                    {"identity_session": identity_session}
                    if identity_session
                    else None
                ),
            ) as client:
                return await client.request(
                    method, f"{self.base_url}{path}", json=json_body
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError) as exc:
            raise IdentityCenterError("IDENTITY_CENTER_UNAVAILABLE") from exc

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        try:
            code = response.json().get("detail", {}).get("code")
        except (ValueError, AttributeError):
            code = None
        if response.status_code == 401:
            raise IdentityCenterError(code or "AUTH_REQUIRED", 401)
        if response.status_code in (403, 409, 422, 429):
            raise IdentityCenterError(
                code or "IDENTITY_REQUEST_REJECTED", response.status_code
            )
        raise IdentityCenterError(
            "IDENTITY_CENTER_UNAVAILABLE", response.status_code
        )

    @staticmethod
    def _parse_user(response: httpx.Response) -> IdentityUser:
        try:
            data = response.json()["user"]
            user_id = str(data["id"]).strip()
            username = str(data["username"])
            if not user_id:
                raise ValueError
            permissions = data.get("permissions") or []
            if not isinstance(permissions, list):
                permissions = []
            return IdentityUser(
                id=user_id,
                username=username,
                role=str(data.get("role") or "user"),
                permissions=tuple(str(item) for item in permissions),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise IdentityCenterError("IDENTITY_CENTER_BAD_RESPONSE", 502) from exc


@lru_cache(maxsize=1)
def get_identity_center_client() -> IdentityCenterClient:
    """Build the server-only client from environment configuration."""

    base_url = os.getenv("IDENTITY_CENTER_BASE_URL", "").strip()
    if not base_url:
        raise IdentityCenterError("IDENTITY_CENTER_NOT_CONFIGURED", 503)
    try:
        timeout = max(0.2, float(os.getenv("IDENTITY_CENTER_TIMEOUT_SECONDS", "3")))
    except ValueError as exc:
        raise IdentityCenterError("IDENTITY_CENTER_NOT_CONFIGURED", 503) from exc
    return IdentityCenterClient(base_url, timeout)
