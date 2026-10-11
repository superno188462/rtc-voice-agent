"""Agent Gateway 上下文适配器合同与隐私验证。"""

from __future__ import annotations

import json
import unittest

import httpx

from services.context_gateway.assembler import (
    _validated_profile_updates,
    explicit_memory_request,
    load_context,
    persist_explicit_preferences,
)
from services.context_gateway.client import ContextGatewayClient, ContextGatewayError


class ContextGatewayTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler) -> ContextGatewayClient:
        return ContextGatewayClient(
            "https://gateway.example/v1/context",
            "test-project-key-do-not-log",
            transport=httpx.MockTransport(handler),
        )

    def test_explicit_food_preference_is_accepted_as_profile_update(self) -> None:
        updates, rejected = _validated_profile_updates(
            [
                {
                    "field": "food_preferences.likes",
                    "action": "add",
                    "value": "草莓",
                    "evidence": "我喜欢吃草莓",
                    "confidence": 0.95,
                }
            ],
            ["我喜欢吃草莓"],
            min_confidence=0.75,
        )
        self.assertEqual(
            updates,
            [{"field": "food_preferences.likes", "action": "add", "value": "草莓"}],
        )
        self.assertEqual(sum(rejected.values()), 0)

    def test_list_profile_set_action_is_normalized_to_add(self) -> None:
        updates, rejected = _validated_profile_updates(
            [
                {
                    "field": "food_preferences.likes",
                    "action": "set",
                    "value": "草莓",
                    "evidence": "我喜欢吃草莓",
                    "confidence": 0.95,
                }
            ],
            ["我喜欢吃草莓"],
            min_confidence=0.75,
        )
        self.assertEqual(updates[0]["action"], "add")
        self.assertEqual(sum(rejected.values()), 0)

    async def test_template_and_short_memory_use_project_auth(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if request.url.path.endswith("/templates/system/agent"):
                return httpx.Response(200, json={"template": {"content": "system {{profile}}"}})
            if request.url.path.endswith("/messages") and request.method == "GET":
                return httpx.Response(200, json={"messages": [{"role": "user", "content": "旧问题"}]})
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json={"profile": {"preferred_language": "zh-CN"}, "version": 2})
            if request.url.path.endswith("/memories"):
                return httpx.Response(200, json={"items": [], "total_pages": 1})
            return httpx.Response(200, json={})

        client = self.make_client(handler)
        prompt, history, profile, memories = await load_context(client, "user-1", "chat-1", "新问题")
        self.assertIn('system {"preferred_language":"zh-CN"}', prompt)
        self.assertIn("preferred_language", prompt)
        self.assertEqual(history, [{"role": "user", "content": "旧问题"}])
        self.assertEqual(profile["preferred_language"], "zh-CN")
        self.assertEqual(memories, [])
        self.assertTrue(observed)
        self.assertTrue(all(item.headers["Authorization"] == "Bearer test-project-key-do-not-log" for item in observed))
        self.assertTrue(all("project_id" not in item.url.params for item in observed))

    async def test_ensure_template_creates_only_when_missing(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(201, json={"id": "template-1"})

        created = await self.make_client(handler).ensure_template(
            "system", "agent", "seed prompt"
        )
        self.assertEqual(created, "created")
        self.assertEqual(
            requests,
            [
                ("GET", "/v1/context/templates/system/agent"),
                ("POST", "/v1/context/templates"),
            ],
        )

    async def test_ensure_template_preserves_existing_gateway_prompt(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.method)
            return httpx.Response(
                200,
                json={"id": "template-1", "version": 1, "content": "local seed prompt"},
            )

        created = await self.make_client(handler).ensure_template(
            "system", "agent", "local seed prompt"
        )
        self.assertEqual(created, "unchanged")
        self.assertEqual(requests, ["GET"])

    async def test_ensure_template_updates_changed_prompt_with_expected_version(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={"id": "template-1", "version": 4, "content": "old prompt"},
                )
            return httpx.Response(
                200,
                json={"id": "template-1", "version": 5, "content": "new prompt"},
            )

        updated = await self.make_client(handler).ensure_template(
            "system", "agent", "new prompt"
        )
        self.assertEqual(updated, "updated")
        self.assertEqual(requests[1].method, "PATCH")
        self.assertEqual(requests[1].url.path, "/v1/context/templates/template-1")
        body = json.loads(requests[1].content)
        self.assertEqual(body["expected_version"], 4)
        self.assertEqual(body["name"], "agent")
        self.assertEqual(body["content"], "new prompt")

    async def test_ensure_template_handles_concurrent_creation(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.method)
            if request.method == "GET":
                if requests.count("GET") == 1:
                    return httpx.Response(404, json={"detail": "not found"})
                return httpx.Response(
                    200,
                    json={"id": "template-1", "version": 1, "content": "seed prompt"},
                )
            return httpx.Response(409, json={"detail": "already exists"})

        created = await self.make_client(handler).ensure_template(
            "system", "agent", "seed prompt"
        )
        self.assertEqual(created, "unchanged")
        self.assertEqual(requests, ["GET", "POST", "GET"])

    async def test_append_sends_ordered_pair_with_ttl_and_messages_are_scoped(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"data": {"messages": [{"role": "assistant", "content": "之前"}]}})
            return httpx.Response(200, json={"ok": True})

        client = self.make_client(handler)
        messages = await client.get_messages("user-1", "chat-1")
        await client.append_messages(
            "user-1", "chat-1", [{"role": "user", "content": "新问题"}, {"role": "assistant", "content": "新回答"}]
        )
        self.assertEqual(messages, [{"role": "assistant", "content": "之前"}])
        self.assertEqual(seen[0].url.params["limit"], "50")
        self.assertEqual(seen[0].url.path, "/v1/context/users/user-1/sessions/chat-1/messages")
        body = json.loads(seen[1].content)
        self.assertEqual([item["role"] for item in body["messages"]], ["user", "assistant"])
        self.assertEqual(body["ttl_seconds"], 86400)

    async def test_long_term_memories_are_paginated_then_locally_selected(self) -> None:
        memory_pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/templates/system/agent"):
                return httpx.Response(200, json={"content": "你是助手"})
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json={"profile": {}})
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"messages": []})
            page = request.url.params["page"]
            memory_pages.append(page)
            if page == "1":
                return httpx.Response(200, json={"items": [{"content": "用户喜欢摄影", "tags": ["兴趣"]}], "total_pages": 2})
            return httpx.Response(200, json={"items": [{"content": "用户偏好简洁回复", "tags": ["表达方式"]}], "total_pages": 2})

        client = self.make_client(handler)
        prompt, _, _, selected = await load_context(client, "user-1", "chat-1", "摄影建议")
        self.assertEqual(memory_pages, ["1", "2"])
        self.assertEqual([item["content"] for item in selected], ["用户喜欢摄影"])
        self.assertIn("用户喜欢摄影", prompt)
        self.assertNotIn("用户偏好简洁回复", prompt)

    async def test_profile_version_conflict_rereads_and_merges(self) -> None:
        put_bodies: list[dict] = []
        profile_reads = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal profile_reads
            if request.method == "GET":
                profile_reads += 1
                profile = {"preferred_language": "zh-CN"} if profile_reads == 1 else {"preferred_language": "zh-CN", "interests": ["摄影"]}
                return httpx.Response(200, json={"profile": profile, "version": profile_reads})
            put_bodies.append(json.loads(request.content))
            if len(put_bodies) == 1:
                return httpx.Response(409, json={"code": "version_conflict"})
            return httpx.Response(200, json={"version": 3})

        await persist_explicit_preferences(
            self.make_client(handler), "user-1", "以后请用简洁中文回答。"
        )
        self.assertEqual(profile_reads, 2)
        self.assertEqual(put_bodies[0]["expected_version"], 1)
        self.assertEqual(put_bodies[1]["expected_version"], 2)
        self.assertEqual(put_bodies[1]["profile"]["interests"], ["摄影"])
        self.assertEqual(put_bodies[1]["profile"]["response_style"], "concise")

    async def test_auth_error_does_not_expose_key_or_response_body(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "sensitive gateway internals"})

        with self.assertRaises(ContextGatewayError) as caught:
            await self.make_client(handler).get_template()
        self.assertEqual(caught.exception.code, "CONTEXT_AUTH_FAILED")
        self.assertNotIn("test-project-key-do-not-log", str(caught.exception))
        self.assertNotIn("sensitive gateway internals", str(caught.exception))

    async def test_timeout_is_reported_without_request_or_response_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("private request text must not leak", request=request)

        with self.assertRaises(ContextGatewayError) as caught:
            await self.make_client(handler).get_template()
        self.assertEqual(caught.exception.code, "CONTEXT_SERVICE_TIMEOUT")
        self.assertNotIn("private request text", str(caught.exception))

    async def test_uninitialized_profile_and_short_session_are_empty(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "not initialized"})

        client = self.make_client(handler)
        self.assertEqual(await client.get_profile("user-1"), ({}, None))
        self.assertEqual(await client.get_messages("user-1", "chat-1"), [])

    async def test_explicit_memory_write_uses_tags_and_metadata(self) -> None:
        posted: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posted.append(json.loads(request.content))
            return httpx.Response(200, json={"profile": {}, "version": 1})

        await persist_explicit_preferences(
            self.make_client(handler), "user-1", "请记住我喜欢摄影和徒步。"
        )
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["content"], "我喜欢摄影和徒步。")
        self.assertEqual(posted[0]["tags"], ["用户明确记录"])
        self.assertEqual(posted[0]["metadata"]["source"], "explicit_user_request")

    def test_explicit_memory_request_excludes_credentials_and_non_explicit_text(self) -> None:
        self.assertEqual(explicit_memory_request("请记住我喜欢简洁的中文回答"), "我喜欢简洁的中文回答")
        self.assertIsNone(explicit_memory_request("我喜欢简洁的中文回答"))
        self.assertIsNone(explicit_memory_request("请记住我的 API Key 是 abc123"))


if __name__ == "__main__":
    unittest.main()
