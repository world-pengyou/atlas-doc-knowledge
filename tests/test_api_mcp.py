"""Exercise real HTTP routes and in-memory MCP sessions with offline dependencies."""
import io
import shutil
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from kb import mcp_server
from kb.app import app
from kb.database import Database
from kb.models import ModelError


BASE_URL = "http://127.0.0.1:8000"


def image_bytes():
    stream = io.BytesIO()
    with Image.new("RGB", (4, 4), (10, 120, 80)) as image:
        image.save(stream, format="PNG")
    return stream.getvalue()


def image_hit(document_id, attempt):
    return {
        "document_id": document_id, "filename": "流程.pdf", "kind": "image",
        "text": "审批通过后归档", "asset_name": f"{attempt}/crop.png",
        "location": {"page": 2, "bbox": [20, 40, 90, 80], "page_width": 200,
                     "page_height": 300, "preview": f"{attempt}/page-2.png"},
        "score": 0.03, "similarity": 0.85, "channels": ["vector", "text"],
    }


class WebRouteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("http-fixture-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        stack = ExitStack()
        self.addCleanup(stack.close)
        settings = SimpleNamespace(data_dir=self.root, public_base_url=BASE_URL,
                                   search_similarity_threshold=0.4, search_top_k=5)
        self.db = MagicMock(spec=Database)
        stack.enter_context(patch("kb.app.load_settings", return_value=settings))
        stack.enter_context(patch("kb.app.Database", return_value=self.db))
        worker = stack.enter_context(patch("kb.app.IngestWorker")).return_value
        worker.error = None
        self.model = stack.enter_context(patch("kb.service.ModelClient")).return_value
        self.model.embed.return_value = [[0.0, 1.0]]
        stack.enter_context(patch("kb.service.query_tokens", return_value="'审批'"))
        self.client = stack.enter_context(TestClient(app, base_url=BASE_URL, client=("127.0.0.1", 50000)))

    def test_markdown_upload_preserves_relative_image_and_source(self):
        body = "# 流程\n![详情](images/chart.png)".encode()
        image = image_bytes()

        def register(document_id, filename, digest, source_path):
            self.db.document.return_value = {"filename": filename, "source_path": source_path}
            return {"id": document_id, "filename": filename, "status": "pending", "error": None,
                    "created": True}

        self.db.add_document.side_effect = register
        response = self.client.post("/api/documents", files=[
            ("files", ("manual/readme.md", body, "text/markdown")),
            ("files", ("manual/images/chart.png", image, "image/png")),
        ])
        self.assertEqual(response.status_code, 202, response.text)
        document, = response.json()["documents"]
        self.assertEqual((document["filename"], document["status"]), ("manual/readme.md", "pending"))
        self.assertNotIn("created", document)
        self.db.add_document.assert_called_once()
        source_file = self.root / self.db.document.return_value["source_path"]
        self.assertEqual(source_file.read_bytes(), body)
        self.assertEqual((source_file.parent / "images" / "chart.png").read_bytes(), image)
        source = self.client.get("/api/documents/" + document["id"] + "/source")
        self.assertEqual(source.status_code, 200)
        self.assertEqual(source.content, body)
        self.assertTrue(source.headers["content-type"].startswith("text/plain"))

    def test_existing_document_returns_original_id_and_cleans_unreferenced_batch(self):
        existing_id = uuid4()
        self.db.add_document.return_value = {
            "id": existing_id, "filename": "guide.md", "status": "ready", "error": None, "created": False,
        }
        response = self.client.post("/api/documents", files={"files": ("guide.md", b"notes", "text/markdown")})
        self.assertEqual(response.status_code, 202, response.text)
        document, = response.json()["documents"]
        self.assertEqual(document["id"], str(existing_id))
        self.assertNotIn("created", document)
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_rejects_duplicate_names_invalid_paths_and_unsupported_uploads(self):
        cases = [
            ["guide.md", "GUIDE.MD"], ["chapter/CON.md"], ["chapter/note.doc"],
            ["D:/public/guide.md"], ["picture.png"],
        ]
        for filenames in cases:
            with self.subTest(filenames=filenames):
                files = [("files", (name, b"fixture", "application/octet-stream")) for name in filenames]
                response = self.client.post("/api/documents", files=files)
                self.assertEqual(response.status_code, 400, response.text)
        self.db.add_document.assert_not_called()
        self.assertFalse((self.root / "uploads").exists())

    def test_empty_attachment_rejects_whole_batch_and_removes_saved_upload(self):
        response = self.client.post("/api/documents", files=[
            ("files", ("guide.md", b"notes", "text/markdown")),
            ("files", ("chart.png", b"", "image/png")),
        ])
        self.assertEqual(response.status_code, 400, response.text)
        self.db.add_document.assert_not_called()
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_search_returns_resolvable_pdf_image_and_page_location(self):
        document_id, attempt = uuid4(), uuid4()
        files = self.root / "assets" / str(document_id) / str(attempt)
        files.mkdir(parents=True)
        fixture = image_bytes()
        for name in ("crop.png", "page-2.png"):
            (files / name).write_bytes(fixture)
        self.db.document.return_value = {"id": document_id}
        self.db.search.return_value = [image_hit(document_id, attempt)]
        response = self.client.post("/api/search", json={"query": "审批", "limit": 5})
        self.assertEqual(response.status_code, 200, response.text)
        hit, = response.json()["results"]
        source = f"{BASE_URL}/api/documents/{document_id}"
        self.assertEqual(hit["source_url"], source + "/source#page=2")
        self.assertEqual(hit["location"]["bbox"], [20, 40, 90, 80])
        self.assertEqual(hit["asset_url"], source + f"/assets/{attempt}/crop.png")
        self.assertEqual(hit["location"]["preview_url"], source + f"/assets/{attempt}/page-2.png")
        for url in (hit["asset_url"], hit["location"]["preview_url"]):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertEqual(response.content, fixture)
        for missing in ("crop.png", f"{attempt}/missing.png"):
            self.assertEqual(self.client.get(source + "/assets/" + missing).status_code, 404)
        self.model.embed.assert_called_once_with(["审批"])
        self.db.search.assert_called_once()

    def test_query_validation_and_model_or_database_errors(self):
        for payload, status in (({"query": " "}, 400), ({"query": "审批", "limit": 0}, 422)):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/search", json=payload).status_code, status)
        self.model.embed.assert_not_called()
        self.model.embed.side_effect = ModelError("向量服务暂不可用")
        response = self.client.post("/api/search", json={"query": "审批"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "向量服务暂不可用")
        self.db.search.assert_not_called()
        self.model.embed.side_effect = None
        self.db.search.side_effect = psycopg.OperationalError("mock database unavailable")
        response = self.client.post("/api/search", json={"query": "审批"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("mock database unavailable", response.text)

    def test_duplicate_results_preserve_all_resolvable_original_images(self):
        rows = []
        for index in range(2):
            document_id, attempt = uuid4(), uuid4()
            directory = self.root / "assets" / str(document_id) / str(attempt)
            directory.mkdir(parents=True)
            fixture = image_bytes()
            for name in ("crop.png", "page-2.png"):
                (directory / name).write_bytes(fixture)
            hit = image_hit(document_id, attempt)
            hit.update(id=index + 1, ordinal=0, score=0.03 - index * 0.001)
            rows.append(hit)
        self.db.document.return_value = {"id": rows[0]["document_id"]}
        self.db.search.return_value = rows

        response = self.client.post("/api/search", json={"query": "审批"})

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["deduplicated_count"], 1)
        hit, = payload["results"]
        self.assertEqual(hit["merged_chunk_count"], 2)
        self.assertEqual(hit["text"], rows[0]["text"])
        self.assertEqual({source["document_id"] for source in hit["sources"]},
                         {str(row["document_id"]) for row in rows})
        for source in hit["sources"]:
            self.assertNotIn("text", source)
            for url in (source["asset_url"], source["location"]["preview_url"]):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_document_states_and_retry_conflict(self):
        document_id = uuid4()
        document = {"id": document_id, "filename": "guide.md", "status": "failed", "error": "缺少图片"}
        self.db.list_documents.return_value = [document]
        listed = self.client.get("/api/documents")
        self.assertEqual(listed.status_code, 200)
        row, = listed.json()["documents"]
        self.assertEqual((row["id"], row["status"], row["error"]), (str(document_id), "failed", "缺少图片"))
        self.db.retry.side_effect = [{"id": document_id, "status": "pending"}, None]
        url = f"/api/documents/{document_id}/retry"
        self.assertEqual(self.client.post(url).json(), {"id": str(document_id), "status": "pending"})
        self.assertEqual(self.client.post(url).status_code, 409)


class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_tool_discovery_calls_and_errors(self):
        document_id, attempt = uuid4(), uuid4()
        database = MagicMock(spec=Database)
        row = {"id": document_id, "filename": "流程.pdf", "status": "ready", "error": None, "chunk_count": 1}
        database.list_documents.return_value = [row]
        database.document.return_value = row
        hit = image_hit(document_id, attempt)
        duplicate = {**hit, "document_id": uuid4(), "filename": "另一个来源.pdf"}
        database.search.return_value = [hit, duplicate]
        connection = database.connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = [hit]
        with ExitStack() as stack:
            stack.enter_context(patch.object(mcp_server, "load_settings", return_value=SimpleNamespace(
                public_base_url=BASE_URL, search_similarity_threshold=0.4, search_top_k=5)))
            stack.enter_context(patch.object(mcp_server, "Database", return_value=database))
            model = stack.enter_context(patch("kb.service.ModelClient")).return_value
            model.embed.return_value = [[0.0, 1.0]]
            stack.enter_context(patch("kb.service.query_tokens", return_value="'审批'"))
            server = mcp_server.create_server()
            async with create_connected_server_and_client_session(server, timedelta(seconds=10)) as session:
                capabilities = session.get_server_capabilities()
                inventory = await session.list_tools()
                listing = await session.call_tool("list_documents", {})
                detail = await session.call_tool("get_document", {"document_id": str(document_id), "limit": 1})
                search = await session.call_tool("search_knowledge", {"query": "审批"})
                invalid = await session.call_tool("get_document", {"document_id": "not-a-uuid"})
                model.embed.side_effect = ModelError("向量服务暂不可用")
                unavailable = await session.call_tool("search_knowledge", {"query": "审批"})
        self.assertIsNotNone(capabilities.tools)
        self.assertEqual({tool.name for tool in inventory.tools}, {"list_documents", "get_document", "search_knowledge"})
        for result in (listing, detail, search):
            self.assertFalse(result.isError, result.content)
        self.assertEqual(listing.structuredContent["documents"][0]["id"], str(document_id))
        detail_chunk, = detail.structuredContent["chunks"]
        search_hit, = search.structuredContent["results"]
        self.assertEqual(detail_chunk["asset_url"], search_hit["asset_url"])
        self.assertEqual(search_hit["source_url"], f"{BASE_URL}/api/documents/{document_id}/source#page=2")
        self.assertEqual(search_hit["location"]["bbox"], hit["location"]["bbox"])
        self.assertEqual(search.structuredContent["deduplicated_count"], 1)
        self.assertEqual(search_hit["merged_chunk_count"], 2)
        self.assertEqual({source["document_id"] for source in search_hit["sources"]},
                         {str(document_id), str(duplicate["document_id"])})
        self.assertTrue(all(source["asset_url"] and source["location"]["bbox"]
                            for source in search_hit["sources"]))
        self.assertTrue(invalid.isError)
        self.assertTrue(unavailable.isError)
        self.assertIn("向量服务暂不可用", unavailable.content[0].text)
        database.check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
