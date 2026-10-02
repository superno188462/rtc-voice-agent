"""认证边界集成验证；使用 DATABASE_URL 指向的 PostgreSQL，并清理本测试数据。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update

from app import app
from services.agent import agent_service
from services.auth.database import get_session_factory
from services.auth.models import AuthSession, LoginAttempt, User


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_authentication_and_user_scoped_agent_sessions(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录、撤销/过期、失败限流、页面恢复和会话归属均由服务端验证。"""

    caplog.set_level(logging.INFO, logger="auth.security")
    # 使用虚构 RTC 配置验证 Token/房间隔离，不访问真实 RTC 服务或使用项目密钥。
    monkeypatch.setattr(
        "app.SCENES",
        {
            "Custom": {
                "SceneConfig": {"name": "test"},
                "RTCConfig": {
                    "AppId": "0123456789abcdef0123456789abcdef",
                    "AppKey": "0123456789abcdef0123456789abcdef",
                },
                "VoiceChat": {},
            }
        },
    )
    asyncio.run(_exercise_auth_flow(caplog))


async def _exercise_auth_flow(caplog: pytest.LogCaptureFixture) -> None:
    nonce = uuid.uuid4().hex[:12]
    username_a = f"test_a_{nonce}"
    username_b = f"test_b_{nonce}"
    failed_username = f"test_fail_{nonce}"
    password_a = f"Agent-Test-A-{nonce}-LongPassword"
    password_b = f"Agent-Test-B-{nonce}-LongPassword"
    wrong_password = f"Wrong-{nonce}-Password-Only"
    client_ip = "198.51.100.42"
    origin = "http://localhost:3000"
    transport = ASGITransport(app=app, client=(client_ip, 43100))
    client_headers = {"Origin": origin}
    factory = get_session_factory()
    created_session_ids: list[str] = []
    user_ids: list[str] = []
    issued_tokens: list[str] = []

    try:
        async with (
            AsyncClient(transport=transport, base_url=origin, headers=client_headers) as client_a,
            AsyncClient(transport=transport, base_url=origin, headers=client_headers) as client_b,
            AsyncClient(transport=transport, base_url=origin, headers=client_headers) as anonymous,
            AsyncClient(transport=transport, base_url=origin, headers=client_headers) as login_client,
        ):
            registration_a = await client_a.post(
                "/api/auth/register", json={"username": username_a, "password": password_a}
            )
            assert registration_a.status_code == 201
            user_a = registration_a.json()["user"]
            user_ids.append(user_a["id"])
            assert user_a["role"] == "user"
            assert user_a["permissions"] == []
            assert "httponly" in registration_a.headers["set-cookie"].lower()
            assert password_a not in registration_a.text

            # Refresh restores identity from the server-side session, not browser role state.
            current_a = await client_a.get("/api/auth/me")
            assert current_a.status_code == 200
            assert current_a.json()["user"] == user_a

            unauthenticated_me = await anonymous.get("/api/auth/me")
            assert unauthenticated_me.status_code == 401
            unauthenticated_session = await anonymous.post(
                "/api/agent/sessions",
                json={"scene_id": "Custom", "room_id": "r", "user_id": "u"},
            )
            assert unauthenticated_session.status_code == 401
            assert (await anonymous.post("/debug/chat", json={"question": "test"})).status_code == 401
            assert (await anonymous.post("/proxy", json={})).status_code == 401

            # Unknown and known usernames receive the same public login error.
            wrong_known = await anonymous.post(
                "/api/auth/login", json={"username": username_a, "password": wrong_password}
            )
            wrong_unknown = await anonymous.post(
                "/api/auth/login",
                json={"username": f"missing_{nonce}", "password": wrong_password},
            )
            assert wrong_known.status_code == wrong_unknown.status_code == 401
            assert wrong_known.json() == wrong_unknown.json()

            # Five failed attempts are accepted for generic rejection; the next is throttled.
            for _ in range(5):
                failed = await anonymous.post(
                    "/api/auth/login",
                    json={"username": failed_username, "password": wrong_password},
                )
                assert failed.status_code == 401
            limited = await anonymous.post(
                "/api/auth/login",
                json={"username": failed_username, "password": wrong_password},
            )
            assert limited.status_code == 429
            assert int(limited.headers["retry-after"]) > 0

            # Explicit successful login establishes a fresh cookie and /me remains authoritative.
            login = await login_client.post(
                "/api/auth/login", json={"username": username_a, "password": password_a}
            )
            assert login.status_code == 200
            assert login.json()["user"] == user_a
            assert "access_token" not in login.json()
            issued_tokens.append(login_client.cookies.get("rtc_agent_session", ""))
            assert (await login_client.get("/api/auth/me")).json()["user"] == user_a

            registration_b = await client_b.post(
                "/api/auth/register", json={"username": username_b, "password": password_b}
            )
            assert registration_b.status_code == 201
            user_b = registration_b.json()["user"]
            user_ids.append(user_b["id"])

            # Each authenticated scene request receives independent RTC credentials.
            response_scene_a1 = await client_a.post("/getScenes")
            assert "Result" in response_scene_a1.json(), response_scene_a1.json()
            scene_a1 = response_scene_a1.json()["Result"]["scenes"][0]
            scene_a2 = (await client_a.post("/getScenes")).json()["Result"]["scenes"][0]
            scene_b = (await client_b.post("/getScenes")).json()["Result"]["scenes"][0]
            assert scene_a1["rtc"]["RoomId"] != scene_a2["rtc"]["RoomId"]
            assert scene_a1["rtc"]["UserId"] != scene_a2["rtc"]["UserId"]
            assert scene_a1["rtc"]["RoomId"] != scene_b["rtc"]["RoomId"]
            assert "AppKey" not in scene_a1["rtc"]

            # The server links AgentSession to the cookie identity and blocks cross-user access.
            created = await client_b.post(
                "/api/agent/sessions",
                json={
                    "scene_id": scene_b["scene"]["id"],
                    "room_id": scene_b["rtc"]["RoomId"],
                    "user_id": scene_b["rtc"]["UserId"],
                },
            )
            assert created.status_code == 200
            session_id = created.json()["session_id"]
            created_session_ids.append(session_id)
            cross_turn = await client_a.post(
                f"/api/agent/sessions/{session_id}/turns", json={"text": "must not run"}
            )
            assert cross_turn.status_code == 404
            cross_delete = await client_a.delete(f"/api/agent/sessions/{session_id}")
            assert cross_delete.status_code == 404
            assert (await client_b.delete(f"/api/agent/sessions/{session_id}")).json() == {
                "closed": True
            }
            created_session_ids.remove(session_id)

            # Logout revokes this session immediately.
            assert (await client_a.post("/api/auth/logout")).status_code == 200
            assert (await client_a.get("/api/auth/me")).status_code == 401

            # A valid but expired cookie is rejected by the same server-side lookup.
            token_b = client_b.cookies.get("rtc_agent_session", "")
            issued_tokens.append(token_b)
            async with factory.begin() as db:
                await db.execute(
                    update(AuthSession)
                    .where(AuthSession.token_hash == _hash(token_b))
                    .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
                )
            assert (await client_b.get("/api/auth/me")).status_code == 401

            # Invalid random cookie is rejected, and an untrusted Origin cannot use cookie auth.
            anonymous.cookies.set("rtc_agent_session", "invalid-random-value", path="/")
            assert (await anonymous.get("/api/auth/me")).status_code == 401
            untrusted = await login_client.post(
                "/api/auth/login",
                headers={"Origin": "https://attacker.invalid"},
                json={"username": username_a, "password": password_a},
            )
            assert untrusted.status_code == 403

        logs = caplog.text
        assert password_a not in logs and password_b not in logs and wrong_password not in logs
        assert username_a not in logs and username_b not in logs and failed_username not in logs
        assert all(token not in logs for token in issued_tokens if token)
    finally:
        for session_id in created_session_ids:
            await agent_service.close_session(session_id)
        username_keys = {
            _hash(username_a), _hash(username_b), _hash(failed_username),
            _hash(f"missing_{nonce}"),
        }
        async with factory.begin() as db:
            if user_ids:
                await db.execute(delete(AuthSession).where(AuthSession.user_id.in_(user_ids)))
                await db.execute(delete(User).where(User.id.in_(user_ids)))
            for key_hash in username_keys:
                await db.execute(
                    delete(LoginAttempt).where(
                        LoginAttempt.scope == "username", LoginAttempt.key_hash == key_hash
                    )
                )
            await db.execute(
                delete(LoginAttempt).where(
                    LoginAttempt.scope == "ip", LoginAttempt.key_hash == _hash(client_ip)
                )
            )
