"""Exercise library routing with separate databases and real isolated files."""
import shutil
import unittest
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.responses import JSONResponse

from kb import config, mcp_server
from kb.app import app, lifespan
from kb.database import Database
from kb.service import KnowledgeService


BASE_URL = "http://127.0.0.1:8000"


def settings_for(root, library="default"):
    return SimpleNamespace(
        data_dir=root, public_base_url=BASE_URL + ("/presales" if library == "presales" else ""),
        search_similarity_threshold=0.4, search_top_k=5,
        embedding_backend="http", web_allowed_hosts=("127.0.0.1", "localhost", "::1"),
        web_allowed_networks=("127.0.0.0/8", "::1/128"),
    )


class LibrarySettingsTests(unittest.TestCase):
    def settings(self, **changes):
        values = dict(
            database_url="postgresql://fixture:synthetic@127.0.0.1:5432/knowledge",
            presales_database_url="postgresql://fixture:synthetic@127.0.0.1:5432/knowledge_presales",
            data_dir=Path(__file__).resolve().parent / "unused-library-fixture",
            public_base_url=BASE_URL, vision_base_url="", vision_api_key="", vision_model="",
            embedding_base_url="http://127.0.0.1:9000", embedding_api_key="",
            embedding_model="fixture", embedding_dimension=2, embedding_backend="http",
        )
        values.update(changes)
        return config.Settings(**values)

    def test_presales_uses_separate_connection_files_and_source_prefix(self):
        original = self.settings()
        selected = config.library_settings(original, "presales")
        self.assertEqual(selected.database_url, original.presales_database_url)
        self.assertNotEqual(selected.database_url, original.database_url)
        self.assertEqual(selected.data_dir, original.data_dir / "presales")
        self.assertEqual(selected.public_base_url, BASE_URL + "/presales")
        self.assertEqual(selected.embedding_fingerprint, original.embedding_fingerprint)
        self.assertEqual(selected.web_allowed_hosts, original.web_allowed_hosts)
        self.assertEqual(selected.web_allowed_networks, original.web_allowed_networks)
        self.assertEqual(config.library_settings(original, "default"), original)
        self.assertEqual(original.public_base_url, BASE_URL)

    def test_disabled_or_unknown_library_does_not_fall_back_to_default(self):
        for library, settings in (
            ("presales", self.settings(presales_database_url="")),
            ("unknown", self.settings()),
        ):
            with self.subTest(library=library), self.assertRaises(ValueError):
                config.library_settings(settings, library)

    def test_alias_or_credentials_cannot_disguise_reusing_original_database(self):
        for value in (
            "postgresql://other:synthetic@localhost:5432/knowledge?connect_timeout=1",
            "host=127.0.0.1 port=5432 dbname=knowledge user=other password=synthetic",
            "postgresql://fixture:synthetic@127.0.0.1:5432/",
            "postgresql://fixture:synthetic@127.0.0.1:invalid/knowledge_presales",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    config.library_settings(self.settings(presales_database_url=value), "presales")
                self.assertNotIn("synthetic", str(caught.exception))


class LibraryHttpTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("library-fixture-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.identifier, self.attempt, self.batch = uuid4(), uuid4(), uuid4()
        self.databases, self.services, self.workers, self.rows = {}, {}, {}, {}
        for library in ("default", "presales"):
            directory = self.root / "data"
            if library == "presales":
                directory /= "presales"
            database = MagicMock(spec=Database)
            service = KnowledgeService(settings_for(directory, library), database)
            service.models = MagicMock()
            service.models.embed.return_value = [[0.0, 1.0]]
            source_path = f"uploads/{self.batch}/manual.md"
            row = {
                "id": self.identifier, "filename": library + ".md", "status": "ready", "error": None,
                "source_path": source_path, "chunk_count": 1, "image_count": 1,
            }
            hit = {
                "id": 1, "ordinal": 0, "document_id": self.identifier, "filename": row["filename"],
                "text": library + "审批证据", "kind": "image", "asset_name": f"{self.attempt}/picture.png",
                "location": {"format": "docx", "paragraph": 2, "preview": f"{self.attempt}/picture.png"},
                "score": 0.03, "similarity": 0.85, "channels": ["semantic"],
            }
            database.document.return_value = row
            database.list_documents.return_value = [row]
            database.search.return_value = [hit]
            database.connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [hit]
            database.retry.return_value = {"id": self.identifier, "status": "pending", "filename": row["filename"]}
            database.confirm.return_value = {"id": self.identifier, "status": "pending", "filename": row["filename"]}
            (directory / source_path).parent.mkdir(parents=True)
            (directory / source_path).write_text(library + "原件", encoding="utf-8")
            asset = directory / "assets" / str(self.identifier) / str(self.attempt) / "picture.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(library.encode("ascii"))
            self.databases[library], self.services[library], self.rows[library] = database, service, row
            self.workers[library] = SimpleNamespace(error=None, thread=SimpleNamespace(is_alive=lambda: True))
        self.stack.enter_context(patch.dict(app.state._state, {
            "service": self.services["default"], "worker": self.workers["default"],
            "services": self.services, "workers": self.workers,
        }))
        self.stack.enter_context(patch("kb.service.query_tokens", return_value="'审批'"))
        # No context manager: fixtures install state without starting real workers or databases.
        self.client = TestClient(app, base_url=BASE_URL, client=("127.0.0.1", 50000))
        self.addCleanup(self.client.close)

    def test_lists_searches_and_generated_links_are_bound_to_selected_library(self):
        for library, prefix in (("default", ""), ("presales", "/presales")):
            with self.subTest(library=library):
                response = self.client.get(prefix + "/api/documents")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["documents"][0]["filename"], library + ".md")
                response = self.client.post(prefix + "/api/search", json={"query": "审批"})
                self.assertEqual(response.status_code, 200, response.text)
                result, = response.json()["results"]
                self.assertEqual(result["text"], library + "审批证据")
                expected = BASE_URL + prefix + f"/api/documents/{self.identifier}"
                self.assertEqual(result["source_url"], expected + "/source")
                self.assertEqual(result["asset_url"], expected + f"/assets/{self.attempt}/picture.png")
                self.assertEqual(result["location"]["preview_url"], result["asset_url"])
                self.assertEqual(result["sources"][0]["source_url"], result["source_url"])
        for database in self.databases.values():
            database.search.assert_called_once()
            database.list_documents.assert_called_once()

    def test_same_document_id_and_relative_paths_resolve_to_different_source_and_assets(self):
        for library, prefix in (("default", ""), ("presales", "/presales")):
            with self.subTest(library=library):
                base = prefix + f"/api/documents/{self.identifier}"
                self.assertEqual(self.client.get(base + "/source").text, library + "原件")
                self.assertEqual(self.client.get(base + f"/assets/{self.attempt}/picture.png").content,
                                 library.encode("ascii"))
                detail = self.client.get(base)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["chunks"][0]["text"], library + "审批证据")
                preview = self.client.get(base + "/preview")
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["filename"], library + ".md")

    def test_presales_upload_writes_only_presales_root_and_connection(self):
        database = self.databases["presales"]
        database.add_document.return_value = {"id": uuid4(), "filename": "新售前.md", "status": "pending",
                                              "error": None, "created": True}
        response = self.client.post("/presales/api/documents", files={
            "files": ("新售前.md", "售前专用材料".encode("utf-8"), "text/markdown"),
        })
        self.assertEqual(response.status_code, 202, response.text)
        database.add_document.assert_called_once()
        self.databases["default"].add_document.assert_not_called()
        relative = database.add_document.call_args.args[3]
        selected = self.services["presales"].settings.data_dir / relative
        self.assertEqual(selected.read_text(encoding="utf-8"), "售前专用材料")
        self.assertFalse((self.services["default"].settings.data_dir / relative).exists())

    def test_confirm_and_retry_use_only_selected_connection_and_worker(self):
        base = f"/presales/api/documents/{self.identifier}"
        token = uuid4()
        confirmed = self.client.post(base + "/confirm", json={"preview_token": str(token)})
        retried = self.client.post(base + "/retry")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.databases["presales"].confirm.assert_called_once_with(self.identifier, token)
        self.databases["presales"].retry.assert_called_once_with(self.identifier)
        self.databases["default"].confirm.assert_not_called()
        self.databases["default"].retry.assert_not_called()
        self.workers["presales"].error = "售前队列暂停"
        self.assertEqual(self.client.post(base + "/retry").status_code, 503)
        self.assertEqual(self.client.post(f"/api/documents/{self.identifier}/retry").status_code, 200)

    def test_deleting_presales_document_preserves_same_id_default_files(self):
        database = self.databases["presales"]
        row = self.rows["presales"]
        job = {"document_id": self.identifier, "filename": row["filename"],
               "source_path": row["source_path"], "removed_chunks": 1}

        def delete(identifier, validate):
            self.assertEqual(identifier, self.identifier)
            validate(job["source_path"])
            return job

        def cleanup(identifier, callback):
            complete = callback(job, False)
            return {"cleanup_pending": not complete, "shared_uploads_retained": False}

        database.delete_document.side_effect = delete
        database.cleanup_deletion.side_effect = cleanup
        response = self.client.delete(f"/presales/api/documents/{self.identifier}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])
        self.assertFalse(response.json()["cleanup_pending"])
        self.assertFalse((self.services["presales"].settings.data_dir / row["source_path"]).exists())
        self.assertTrue((self.services["default"].settings.data_dir / row["source_path"]).is_file())
        self.assertTrue((self.services["default"].settings.data_dir / "assets" / str(self.identifier)).is_dir())
        self.databases["default"].delete_document.assert_not_called()

    def test_disabled_presales_returns_404_for_read_and_write_without_default_fallback(self):
        self.services.pop("presales")
        self.workers.pop("presales")
        paths = ["/presales/", "/presales/mcp", "/presales/api/documents", f"/presales/api/documents/{self.identifier}/source",
                 f"/presales/api/documents/{self.identifier}/assets/{self.attempt}/picture.png"]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404, response.text)
        response = self.client.post("/presales/api/search", json={"query": "审批"})
        self.assertEqual(response.status_code, 404, response.text)
        self.databases["default"].list_documents.assert_not_called()
        self.databases["default"].document.assert_not_called()
        self.databases["default"].search.assert_not_called()

    def test_http_mcp_endpoints_dispatch_to_separate_protocol_apps(self):
        def endpoint(library):
            async def asgi(scope, receive, send):
                await JSONResponse({"library": library})(scope, receive, send)
            return asgi

        with patch.dict(app.state._state, {
            "mcp_http": endpoint("default"), "presales_mcp_http": endpoint("presales"),
        }):
            for library, path in (("default", "/mcp"), ("presales", "/presales/mcp")):
                with self.subTest(library=library):
                    response = self.client.post(path, json={})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {"library": library})


class LibraryLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_library_startup_failure_stops_first_worker_and_protocol_manager(self):
        settings = LibrarySettingsTests().settings()
        databases = [MagicMock(spec=Database), MagicMock(spec=Database)]
        databases[1].check.side_effect = RuntimeError("isolated second database unavailable")
        events = []

        @asynccontextmanager
        async def protocol_manager():
            events.append("mcp_started")
            try:
                yield
            finally:
                events.append("mcp_stopped")

        server = SimpleNamespace(session_manager=SimpleNamespace(run=protocol_manager),
                                 streamable_http_app=MagicMock())
        service = SimpleNamespace(settings=settings, database=databases[0], models=MagicMock())
        worker = MagicMock()
        state = SimpleNamespace()
        isolated_app = SimpleNamespace(state=state)
        with patch("kb.app.load_settings", return_value=settings), \
                patch("kb.app.Database", side_effect=databases), \
                patch("kb.app.KnowledgeService", return_value=service), \
                patch("kb.app.IngestWorker", return_value=worker), \
                patch("kb.app.create_server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "second database unavailable"):
                async with lifespan(isolated_app):
                    self.fail("An unavailable configured database must prevent startup")
        worker.start.assert_called_once()
        worker.stop.assert_called_once()
        self.assertEqual(events, ["mcp_started", "mcp_stopped"])


class LibraryMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_servers_bind_all_three_tools_to_their_own_library(self):
        identifier = uuid4()
        for library, prefix in (("default", ""), ("presales", "/presales")):
            with self.subTest(library=library):
                database = MagicMock(spec=Database)
                row = {"id": identifier, "filename": library + ".md", "status": "ready", "error": None,
                       "chunk_count": 1}
                hit = {"id": 1, "ordinal": 0, "document_id": identifier, "filename": row["filename"],
                       "text": library + "检索证据", "kind": "text", "location": {}, "asset_name": None,
                       "score": 0.03, "similarity": 0.9, "channels": ["semantic"]}
                database.list_documents.return_value = [row]
                database.document.return_value = row
                database.search.return_value = [hit]
                database.connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [hit]
                service = KnowledgeService(settings_for(Path(__file__).parent, library), database)
                service.models = MagicMock()
                service.models.embed.return_value = [[0.0, 1.0]]
                with patch("kb.service.query_tokens", return_value="'售前'"):
                    server = mcp_server.create_server(service, streamable_http_path=prefix + "/mcp",
                                                      preload_local_runtime=False, port=8000)
                    async with create_connected_server_and_client_session(server, timedelta(seconds=10)) as session:
                        tools = await session.list_tools()
                        listed = await session.call_tool("list_documents", {})
                        detail = await session.call_tool("get_document", {"document_id": str(identifier)})
                        result = await session.call_tool("search_knowledge", {"query": "售前"})
                self.assertEqual({tool.name for tool in tools.tools},
                                 {"search_knowledge", "list_documents", "get_document"})
                for response in (listed, detail, result):
                    self.assertFalse(response.isError, response.content)
                self.assertEqual(listed.structuredContent["documents"][0]["filename"], library + ".md")
                self.assertEqual(detail.structuredContent["chunks"][0]["text"], hit["text"])
                selected, = result.structuredContent["results"]
                self.assertEqual(selected["text"], hit["text"])
                self.assertEqual(selected["source_url"],
                                 BASE_URL + prefix + f"/api/documents/{identifier}/source")


if __name__ == "__main__":
    unittest.main()
