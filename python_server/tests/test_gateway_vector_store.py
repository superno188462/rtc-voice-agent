"""Gateway 向量 API 与本地知识库用例合同测试，网络由 MockTransport 截获。"""

from __future__ import annotations

import json
import unittest

import httpx

from services.rag.gateway_vector_store import GatewayVectorStore, GatewayVectorStoreError
from services.rag.knowledge_base import KnowledgeBaseService


class MemoryRepository:
    """让业务流程测试不连接 PostgreSQL。"""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.chunks: dict[str, dict[str, dict]] = {}

    async def get_document_by_path(self, path):
        document = next((item for item in self.documents.values() if item.get("path") == path), None)
        if document is None:
            return None
        return {
            "document_id": document["document_id"],
            "content_sha256": document["content_sha256"],
            "title": document["title"],
            "metadata": document["metadata"],
        }

    async def stage_document(self, **values):
        document_id = values["document_id"]
        old_ids = list(self.chunks.get(document_id, {}))
        self.documents[document_id] = {**values, "document_id": document_id, "sync_status": "syncing"}
        current = self.chunks.setdefault(document_id, {})
        current.update({chunk["id"]: chunk for chunk in values["chunks"]})
        return old_ids

    async def finish_sync(self, document_id, active_ids):
        self.chunks[document_id] = {
            chunk_id: chunk for chunk_id, chunk in self.chunks[document_id].items()
            if chunk_id in set(active_ids)
        }
        self.documents[document_id]["sync_status"] = "ready"

    async def mark_failed(self, document_id):
        self.documents[document_id]["sync_status"] = "failed"

    async def get_chunk_ids(self, document_id):
        return list(self.chunks.get(document_id, {}))

    async def delete_local_document(self, document_id):
        self.documents.pop(document_id, None)
        self.chunks.pop(document_id, None)

    async def list_documents(self):
        return [
            {"document_id": key, "title": value["title"], "sync_status": value["sync_status"],
             "chunk_count": len(self.chunks.get(key, {}))}
            for key, value in self.documents.items()
        ]


class GatewayVectorStoreTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, handler) -> GatewayVectorStore:
        return GatewayVectorStore(
            "https://gateway.example/gateway/v1/",
            "project-key-for-test",
            "rtc-kb-test",
            transport=httpx.MockTransport(handler),
        )

    async def test_environment_requires_only_url_key_and_collection(self) -> None:
        import os

        names = {
            "GATEWAY_BASE_URL": "https://gateway.example/v1",
            "GATEWAY_API_KEY": "test-only-key",
            "GATEWAY_COLLECTION": "rtc-kb-test",
        }
        previous = {key: os.environ.get(key) for key in names}
        try:
            for key in names:
                os.environ.pop(key, None)
            store = GatewayVectorStore(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[])))
            for key, value in names.items():
                os.environ[key] = value
            self.assertEqual(await store.list_collections(), [])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def test_collection_creation_fixes_model_and_record_limit(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(201, json={
                "collection": "rtc-kb-test", "model": "example/embed-small",
                "vector_dimensions": 768, "chunk_size": 700, "chunk_overlap": 0,
            })

        item = await self.make_store(handler).ensure_collection(
            model="example/embed-small", description="test", chunk_size=700
        )
        self.assertEqual([request.method for request in requests], ["GET", "POST"])
        self.assertEqual(json.loads(requests[1].content), {
            "name": "rtc-kb-test", "description": "test", "model": "example/embed-small",
            "chunk_size": 700, "chunk_overlap": 0,
        })
        self.assertEqual(item["vector_dimensions"], 768)

        mismatch = self.make_store(lambda _request: httpx.Response(200, json=[{
            "collection": "rtc-kb-test", "model": "different", "chunk_size": 700,
        }]))
        with self.assertRaises(GatewayVectorStoreError) as caught:
            await mismatch.ensure_collection(model="example/embed-small", description="test", chunk_size=700)
        self.assertEqual(caught.exception.code, "VECTOR_COLLECTION_MODEL_MISMATCH")

    async def test_add_vectors_sends_individual_records_and_metadata(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"upserted": [{"external_id": "doc-0", "vectors": 1}]})

        store = self.make_store(handler)
        vectors = [{"id": "doc-0", "content": "pre-split text", "metadata": {"document_id": "doc"}}]
        await store.add_vectors(vectors)
        self.assertEqual(captured[0].url.path, "/gateway/v1/vector-stores/collections/rtc-kb-test/upsert")
        self.assertEqual(json.loads(captured[0].content), {"vectors": vectors})

    async def test_document_upsert_splits_persists_and_removes_stale_vectors(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=[{
                    "collection": "rtc-kb-test", "model": "example/embed-small", "chunk_size": 100,
                    "vector_dimensions": 768,
                }])
            if request.url.path.endswith("/upsert"):
                vectors = json.loads(request.content)["vectors"]
                return httpx.Response(200, json={"upserted": [{"external_id": item["id"]} for item in vectors]})
            if request.url.path.endswith("/delete"):
                return httpx.Response(200, json={"deleted": len(json.loads(request.content)["ids"])})
            return httpx.Response(200, json={"results": []})

        repository = MemoryRepository()
        service = KnowledgeBaseService(self.make_store(handler), repository=repository)
        first = await service.upsert_document(
            document_id="manual-1", title="说明", file_name="guide.md",
            content="连接配置说明。" * 60, metadata={"category": "technical"},
        )
        first_vectors = [
            json.loads(request.content)["vectors"]
            for request in requests if request.url.path.endswith("/upsert")
        ][0]
        self.assertGreater(len(first_vectors), 1)
        self.assertTrue(all(len(item["content"]) <= 100 for item in first_vectors))
        self.assertEqual(first_vectors[0]["metadata"]["document_id"], "manual-1")
        self.assertEqual(first_vectors[0]["metadata"]["chunk_index"], 0)
        self.assertEqual(first_vectors[0]["metadata"]["file_name"], "guide.md")
        self.assertEqual(first["result"]["chunks"], len(first_vectors))

        old_ids = [item["id"] for item in first_vectors]
        second = await service.upsert_document(
            document_id="manual-1", title="说明", file_name="guide.md",
            content="更新后文本。", metadata={"category": "technical"},
        )
        self.assertEqual(second["id"], "manual-1")
        self.assertEqual(len(repository.chunks["manual-1"]), 1)
        upsert_requests = [request for request in requests if request.url.path.endswith("/upsert")]
        second_vectors = json.loads(upsert_requests[-1].content)["vectors"]
        self.assertEqual(second_vectors[0]["id"], first_vectors[0]["id"])
        deleted_requests = [request for request in requests if request.url.path.endswith("/delete")]
        self.assertEqual(json.loads(deleted_requests[-1].content)["ids"], old_ids[1:])

    async def test_metadata_filter_results_group_by_parent_document(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"results": [
                {"chunk_id": "c1", "external_id": "v1", "title": "Doc", "content": "second",
                 "score": .9, "metadata": {"document_id": "d1", "chunk_index": 1, "category": "guide"}},
                {"chunk_id": "c0", "external_id": "v0", "title": "Doc", "content": "first",
                 "score": .8, "metadata": {"document_id": "d1", "chunk_index": 0, "category": "guide"}},
            ]})

        service = KnowledgeBaseService(self.make_store(handler), repository=MemoryRepository())
        hits = await service.search("query", top_k=2, metadata={"category": "guide"})
        self.assertEqual(hits[0]["document_id"], "d1")
        self.assertEqual(json.loads(requests[0].content)["metadata"], {"category": "guide"})
        context = await service.search_context("query")
        self.assertLess(context.index("first"), context.index("second"))

    async def test_delete_removes_every_record_before_local_document(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"results": []} if request.url.path.endswith("/query") else {"deleted": 2})

        repository = MemoryRepository()
        repository.documents["doc"] = {"title": "doc", "sync_status": "ready"}
        repository.chunks["doc"] = {"v1": {}, "v2": {}}
        service = KnowledgeBaseService(self.make_store(handler), repository=repository)
        await service.delete_document("doc")
        self.assertEqual(json.loads(requests[0].content), {"ids": ["v1", "v2"]})
        self.assertNotIn("doc", repository.documents)

    async def test_gateway_error_does_not_expose_upstream_detail_or_key(self) -> None:
        store = self.make_store(lambda _request: httpx.Response(401, json={"detail": "private secret"}))
        with self.assertRaises(GatewayVectorStoreError) as caught:
            await store.list_collections()
        self.assertEqual(caught.exception.code, "VECTOR_GATEWAY_AUTH_FAILED")
        self.assertNotIn("private secret", str(caught.exception))
        self.assertNotIn("project-key-for-test", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
