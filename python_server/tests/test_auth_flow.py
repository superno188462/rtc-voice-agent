"""Identity Center BFF authentication and user isolation tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import app
from services.identity.client import (
    IdentityAuthResult,
    IdentityCenterClient,
    IdentityCenterError,
    IdentityUser,
)


def test_identity_client_contract_and_opaque_cookie_forwarding() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path.endswith("/login"):
            return httpx.Response(
                200,
                json={"user": {"id": "identity-user-1", "username": "alice"}},
                headers={"set-cookie": "identity_session=opaque-value; HttpOnly; Max-Age=3600; Path=/"},
            )
        return httpx.Response(
            200,
            json={"user": {"id": "identity-user-1", "username": "alice"}},
        )

    async def exercise() -> None:
        client = IdentityCenterClient(
            "http://identity.test/identity-center", transport=httpx.MockTransport(handler)
        )
        login = await client.login("alice", "password-123")
        assert login.user.id == "identity-user-1"
        assert login.session_cookie == "opaque-value"
        assert login.max_age == 3600
        current = await client.current_user(login.session_cookie)
        assert current == login.user

    asyncio.run(exercise())
    assert observed[0].url.path == "/identity-center/api/auth/login"
    assert observed[0].headers["content-type"].startswith("application/json")
    assert observed[0].headers.get("authorization") is None
    assert observed[0].read() == b'{"username":"alice","password":"password-123"}'
    assert observed[1].url.path == "/identity-center/api/auth/me"
    assert observed[1].headers["cookie"] == "identity_session=opaque-value"


class FakeIdentityCenter:
    def __init__(self) -> None:
        self.users = {
            "session-alice": IdentityUser("identity-alice", "alice"),
            "session-bob": IdentityUser("identity-bob", "bob"),
        }
        self.logout_values: list[str | None] = []
        self.unavailable = False

    async def register(self, username: str, password: str) -> IdentityAuthResult:
        if self.unavailable:
            raise IdentityCenterError("IDENTITY_CENTER_UNAVAILABLE")
        return IdentityAuthResult(
            IdentityUser("identity-new", username), "session-alice", 1800
        )

    async def login(self, username: str, password: str) -> IdentityAuthResult:
        if self.unavailable:
            raise IdentityCenterError("IDENTITY_CENTER_UNAVAILABLE")
        if username != "alice" or password != "password-123":
            raise IdentityCenterError("INVALID_CREDENTIALS", 401)
        return IdentityAuthResult(
            self.users["session-alice"], "session-alice", 1800
        )

    async def current_user(self, identity_session: str | None) -> IdentityUser:
        if self.unavailable:
            raise IdentityCenterError("IDENTITY_CENTER_UNAVAILABLE")
        if identity_session not in self.users:
            raise IdentityCenterError("AUTH_REQUIRED", 401)
        return self.users[identity_session]

    async def logout(self, identity_session: str | None) -> None:
        if self.unavailable:
            raise IdentityCenterError("IDENTITY_CENTER_UNAVAILABLE")
        self.logout_values.append(identity_session)


def test_auth_routes_cookie_restore_logout_and_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    center = FakeIdentityCenter()
    monkeypatch.setattr("app.get_identity_center_client", lambda: center)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost:8000",
            headers={"Origin": "http://localhost:3000"},
        ) as client:
            registered = await client.post(
                "/api/auth/register",
                json={"username": "new-user", "password": "password-123"},
            )
            assert registered.status_code == 201
            assert registered.json()["user"]["id"] == "identity-new"
            cookie = registered.headers["set-cookie"].lower()
            assert "identity_session=session-alice" in cookie
            assert "httponly" in cookie
            assert "max-age=1800" in cookie

            logged_in = await client.post(
                "/api/auth/login",
                json={"username": "alice", "password": "password-123"},
            )
            assert logged_in.status_code == 200
            restored = await client.get("/api/auth/me")
            assert restored.json()["user"]["id"] == "identity-alice"
            assert "password" not in restored.text

            logged_out = await client.post("/api/auth/logout")
            assert logged_out.status_code == 200
            assert center.logout_values[-1] == "session-alice"
            assert "max-age=0" in logged_out.headers["set-cookie"].lower()

            anonymous = await client.get("/api/auth/me")
            assert anonymous.status_code == 401

        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            preflight = await client.options(
                "/api/auth/login",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
            assert preflight.headers["access-control-allow-credentials"] == "true"

    asyncio.run(exercise())


def test_auth_errors_and_center_outage_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    center = FakeIdentityCenter()
    monkeypatch.setattr("app.get_identity_center_client", lambda: center)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost:8000",
            headers={"Origin": "http://localhost:3000"},
        ) as client:
            invalid = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong"}
            )
            assert invalid.status_code == 401
            assert invalid.json()["detail"]["code"] == "INVALID_CREDENTIALS"

            center.unavailable = True
            unavailable = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "password-123"}
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["detail"]["code"] == "IDENTITY_CENTER_UNAVAILABLE"

            client.cookies.set("identity_session", "session-alice", path="/")
            logout = await client.post("/api/auth/logout")
            assert logout.status_code == 503
            assert "max-age=0" in logout.headers["set-cookie"].lower()

            rejected_origin = await client.post(
                "/api/auth/login",
                headers={"Origin": "https://untrusted.example"},
                json={"username": "alice", "password": "password-123"},
            )
            assert rejected_origin.status_code == 403

    asyncio.run(exercise())


def test_authenticated_users_cannot_access_each_others_voice_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    center = FakeIdentityCenter()
    monkeypatch.setattr("app.get_identity_center_client", lambda: center)
    monkeypatch.setattr(
        "app.agent_service.get_session",
        lambda session_id: asyncio.sleep(
            0,
            result=SimpleNamespace(
                session_id=session_id,
                account_id="identity-alice",
                conversation_id="conversation-alice",
            ),
        ),
    )
    monkeypatch.setattr("app.agent_service.close_session", lambda session_id: asyncio.sleep(0, result=True))

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            client.cookies.set("identity_session", "session-bob", path="/")
            forbidden = await client.delete("/api/agent/sessions/session-1")
            assert forbidden.status_code == 404
            client.cookies.set("identity_session", "session-alice", path="/")
            allowed = await client.delete("/api/agent/sessions/session-1")
            assert allowed.status_code == 200
            assert allowed.json() == {"closed": True}

    asyncio.run(exercise())
