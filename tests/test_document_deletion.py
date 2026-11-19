"""Offline checks for transactional deletion and bounded local-file cleanup."""
import shutil
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from kb.app import app
from kb.database import Database
from kb.errors import DocumentConflict, DocumentNotFound, StorageCheckError, StoragePathError
from kb.service import KnowledgeService
from kb.storage import deletion_paths


BASE_URL = "http://127.0.0.1:8000"


class DeletionDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.document_id = uuid4()
        self.batch_id = uuid4()
        self.document = {
            "id": self.document_id, "filename": "manual.md", "status": "ready",
            "source_path": f"uploads/{self.batch_id}/manual.md", "chunk_count": 3,
        }
        self.job = {
            "document_id": self.document_id, "filename": "manual.md",
            "source_path": self.document["source_path"], "removed_chunks": 3,
        }
        self.connection = Mock()
        self.database = Database(SimpleNamespace())

        @contextmanager
        def connect():
            yield self.connection

        self.database.connect = connect

    def scripted_delete(self, *, document=True, job=False):
        def execute(sql, parameters=()):
            normalized = " ".join(sql.lower().split())
            cursor = Mock()
            if normalized.startswith("select") and "from kb002.documents" in normalized:
                cursor.fetchone.return_value = self.document if document else None
            elif normalized.startswith("select") and "from kb002.deletions" in normalized:
                cursor.fetchone.return_value = self.job if job else None
            elif normalized.startswith("delete from kb002.chunks"):
                cursor.rowcount = 3
            elif normalized.startswith("insert into kb002.deletions"):
                cursor.fetchone.return_value = self.job
            elif normalized.startswith("delete from kb002.documents"):
                cursor.rowcount = 1
            else:
                raise AssertionError(f"Unexpected deletion SQL: {normalized}")
            return cursor

        self.connection.execute.side_effect = execute

    def test_locks_and_validates_ready_document_before_removing_chunks_and_record(self):
        self.scripted_delete()
        events = []
        execute = self.connection.execute.side_effect

        def record_sql(sql, parameters=()):
            events.append(" ".join(sql.lower().split()))
            return execute(sql, parameters)

        self.connection.execute.side_effect = record_sql

        def validate(source_path):
            events.append("validate")
            self.assertEqual(source_path, self.document["source_path"])

        job = self.database.delete_document(self.document_id, validate)

        self.assertIn("for update", events[0])
        validate_index = events.index("validate")
        chunks_index = next(i for i, sql in enumerate(events) if sql.startswith("delete from kb002.chunks"))
        document_index = next(i for i, sql in enumerate(events) if sql.startswith("delete from kb002.documents"))
        job_index = next(i for i, sql in enumerate(events) if sql.startswith("insert into kb002.deletions"))
        self.assertLess(validate_index, chunks_index)
        self.assertLess(chunks_index, job_index)
        self.assertLess(job_index, document_index)
        self.assertEqual(job["document_id"], self.document_id)
        self.assertEqual(job["removed_chunks"], 3)
        for call in self.connection.execute.call_args_list:
            self.assertNotIn(str(self.document_id), call.args[0], "IDs must remain SQL parameters")

    def test_only_ready_documents_can_be_deleted(self):
        for status in ("pending", "processing", "preview", "failed"):
            with self.subTest(status=status):
                self.document["status"] = status
                self.scripted_delete()
                self.connection.execute.reset_mock()
                validate = Mock()

                with self.assertRaises(DocumentConflict):
                    self.database.delete_document(self.document_id, validate)

                validate.assert_not_called()
                self.assertEqual(self.connection.execute.call_count, 1)
                self.assertIn("for update", self.connection.execute.call_args.args[0].lower())

    def test_missing_document_without_cleanup_task_is_not_found(self):
        self.scripted_delete(document=False)
        validate = Mock()

        with self.assertRaises(DocumentNotFound):
            self.database.delete_document(self.document_id, validate)

        self.assertFalse(any(call.args[0].lstrip().lower().startswith("delete")
                             for call in self.connection.execute.call_args_list))

    def test_pending_cleanup_task_is_reused_without_deleting_rows_again(self):
        self.scripted_delete(document=False, job=True)
        validate = Mock(side_effect=AssertionError("An already-deleted index must not run deletion preflight"))

        result = self.database.delete_document(self.document_id, validate)

        self.assertEqual(result["document_id"], self.document_id)
        self.assertEqual(result["source_path"], self.job["source_path"])
        validate.assert_not_called()
        self.assertFalse(any(call.args[0].lstrip().lower().startswith(("delete", "insert"))
                             for call in self.connection.execute.call_args_list))

    def test_invalid_storage_path_prevents_every_delete(self):
        self.scripted_delete()
        validate = Mock(side_effect=StoragePathError("测试路径无效"))

        with self.assertRaises(StoragePathError):
            self.database.delete_document(self.document_id, validate)

        self.assertEqual(self.connection.execute.call_count, 1)

    def cleanup_connection(self, *, shared=False, existing=True):
        def execute(sql, parameters=()):
            normalized = " ".join(sql.lower().split())
            cursor = Mock()
            if "from kb002.deletions" in normalized and normalized.startswith("select"):
                cursor.fetchone.return_value = self.job if existing else None
            elif "pg_advisory_xact_lock" in normalized:
                pass
            elif "from kb002.documents" in normalized:
                cursor.fetchone.return_value = {"present": 1} if shared else None
            elif normalized.startswith("delete from kb002.deletions"):
                cursor.rowcount = 1
            else:
                raise AssertionError(f"Unexpected cleanup SQL: {normalized}")
            return cursor

        self.connection.execute.side_effect = execute

    def test_cleanup_locks_batch_and_checks_every_status_with_windows_paths(self):
        self.job["source_path"] = self.job["source_path"].replace("/", "\\")
        self.cleanup_connection(shared=True)
        callback = Mock(return_value=True)

        result = self.database.cleanup_deletion(self.document_id, callback)

        self.assertEqual(result, {"cleanup_pending": False, "shared_uploads_retained": True})
        callback.assert_called_once_with(self.job, True)
        calls = self.connection.execute.call_args_list
        self.assertIn("for update", calls[0].args[0].lower())
        lock_index = next(i for i, call in enumerate(calls) if "pg_advisory_xact_lock" in call.args[0])
        shared_index = next(i for i, call in enumerate(calls) if "FROM kb002.documents" in call.args[0])
        self.assertLess(lock_index, shared_index)
        self.assertEqual(calls[shared_index].args[1], (f"uploads/{self.batch_id}/",))
        self.assertNotIn("status", calls[shared_index].args[0].lower())
        self.assertIn("replace(source_path", calls[shared_index].args[0].lower())
        self.assertTrue(calls[-1].args[0].lstrip().lower().startswith("delete from kb002.deletions"))

    def test_incomplete_cleanup_retains_task(self):
        self.cleanup_connection()

        result = self.database.cleanup_deletion(self.document_id, Mock(return_value=False))

        self.assertTrue(result["cleanup_pending"])
        self.assertFalse(any(call.args[0].lstrip().lower().startswith("delete")
                             for call in self.connection.execute.call_args_list))

    def test_cleanup_callback_error_never_removes_task(self):
        self.cleanup_connection()

        with self.assertRaises(OSError):
            self.database.cleanup_deletion(self.document_id, Mock(side_effect=OSError("offline fixture")))

        self.assertFalse(any(call.args[0].lstrip().lower().startswith("delete")
                             for call in self.connection.execute.call_args_list))

    def test_cleanup_task_already_completed_is_safe_and_does_not_touch_files(self):
        self.cleanup_connection(existing=False)
        callback = Mock()

        result = self.database.cleanup_deletion(self.document_id, callback)

        self.assertEqual(result, {"cleanup_pending": False, "shared_uploads_retained": False})
        callback.assert_not_called()
        self.assertEqual(self.connection.execute.call_count, 1)


class StorageFixture:
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("deletion-fixture-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.document_id, self.batch_id, self.attempt = uuid4(), uuid4(), uuid4()
        self.source_path = f"uploads/{self.batch_id}/manual/readme.md"
        self.source = self.root / self.source_path
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"# offline fixture")
        self.image_dependency = self.source.parent / "images" / "chart.png"
        self.image_dependency.parent.mkdir()
        self.image_dependency.write_bytes(b"offline source image")
        self.asset_root = self.root / "assets" / str(self.document_id)
        self.asset = self.asset_root / str(self.attempt) / "figure.png"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_bytes(b"offline asset")
        self.preview_root = self.root / "previews" / str(self.document_id)
        self.preview_root.mkdir(parents=True)
        self.preview = self.preview_root / f"{self.attempt}.json"
        self.preview.write_text("{}", encoding="utf-8")


class DeletionStorageTests(StorageFixture, unittest.TestCase):
    def test_preflight_accepts_upload_paths_and_windows_separators_without_mutation(self):
        for source in (self.source_path, self.source_path.replace("/", "\\")):
            with self.subTest(source=source):
                deletion_paths(self.root, self.document_id, source)
        self.assertTrue(self.source.is_file())
        self.assertTrue(self.asset.is_file())
        self.assertTrue(self.preview.is_file())

    def test_preflight_rejects_paths_outside_canonical_upload_batch(self):
        invalid = [str(self.source.resolve()), "../manual.md", "uploads/../manual.md",
                   "uploads/not-a-uuid/manual.md", "uploads/" + str(self.batch_id),
                   f"uploads/{str(self.batch_id).upper()}/manual.md", "assets/figure.png",
                   f"uploads/{self.batch_id}/manual/../../outside.md"]
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(StoragePathError):
                deletion_paths(self.root, self.document_id, source)
        self.assertTrue(self.source.is_file())
        self.assertTrue(self.asset.is_file())

    def test_preflight_rejects_private_tree_link_without_touching_target(self):
        target = self.root / "unrelated"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        link = self.asset_root / "linked-directory"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"The current Windows account cannot create a directory symlink: {error}")

        with self.assertRaises(StoragePathError):
            deletion_paths(self.root, self.document_id, self.source_path)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_preflight_rejects_windows_reparse_metadata_without_removing_files(self):
        reparse = self.asset_root / "junction-fixture"
        reparse.mkdir()
        original_lstat = Path.lstat

        def metadata(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            if path == reparse:
                return SimpleNamespace(st_mode=result.st_mode,
                                       st_file_attributes=getattr(result, "st_file_attributes", 0) | 0x400)
            return result

        with patch.object(Path, "lstat", metadata):
            with self.assertRaises(StoragePathError):
                deletion_paths(self.root, self.document_id, self.source_path)

        self.assertTrue(self.source.is_file())
        self.assertTrue(self.asset.is_file())
        self.assertTrue(self.preview.is_file())


class DeletionServiceTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.database = Mock(spec=Database)
        self.settings = SimpleNamespace(data_dir=self.root, public_base_url=BASE_URL,
                                        search_similarity_threshold=0.4, search_top_k=5)
        self.service = KnowledgeService(self.settings, self.database)
        self.service.models = Mock()
        self.service.models.embed.return_value = [[0.25, 0.75]]
        self.document = {"id": self.document_id, "filename": "manual/readme.md", "status": "ready",
                         "source_path": self.source_path, "chunk_count": 3}
        self.job = {"document_id": self.document_id, "filename": self.document["filename"],
                    "source_path": self.source_path, "removed_chunks": 3}
        self.documents = {self.document_id: self.document}
        self.jobs = {}
        self.database.delete_document.side_effect = self.delete_index
        self.database.cleanup_deletion.side_effect = self.cleanup_index
        self.database.document.side_effect = lambda identifier: self.documents.get(identifier)
        self.database.list_documents.side_effect = lambda: list(self.documents.values()) + [
            {"id": job["document_id"], "filename": job["filename"], "status": "cleanup_pending",
             "error": "检索内容已删除，本地文件清理待重试", "created_at": "2026-11-19T00:00:00Z",
             "chunk_count": 0, "image_count": 0, "preview_token": None, "confirmed_at": None}
            for job in self.jobs.values()]

    def delete_index(self, identifier, validate):
        document = self.documents.get(identifier)
        if document is None:
            job = self.jobs.get(identifier)
            if job is None:
                raise DocumentNotFound("文档不存在或已完成删除")
            return job
        if document["status"] != "ready":
            raise DocumentConflict("仅支持删除已入库文档")
        validate(document["source_path"])
        self.documents.pop(identifier)
        self.jobs[identifier] = self.job
        return self.job

    def cleanup_index(self, identifier, callback):
        job = self.jobs.get(identifier)
        if job is None:
            return {"cleanup_pending": False, "shared_uploads_retained": False}
        prefix = "/".join(job["source_path"].replace("\\", "/").split("/")[:2]) + "/"
        shared = any(document["source_path"].replace("\\", "/").startswith(prefix)
                     for document in self.documents.values())
        complete = callback(job, shared)
        if complete:
            self.jobs.pop(identifier)
        return {"cleanup_pending": not complete, "shared_uploads_retained": shared}

    def client(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(app.state, "service", self.service, create=True))
        # Do not enter the client's lifespan: no .env, worker, model or database is started.
        client = TestClient(app, base_url=BASE_URL, client=("127.0.0.1", 50000))
        self.addCleanup(client.close)
        return client

    def test_success_removes_private_files_and_unreferenced_source_batch(self):
        unrelated = self.root / "assets" / str(uuid4())
        unrelated.mkdir(parents=True)
        sentinel = unrelated / "keep.png"
        sentinel.write_bytes(b"other document")

        result = self.service.delete_document(self.document_id)

        self.assertEqual(result, {"id": str(self.document_id), "filename": self.document["filename"],
                                  "deleted": True, "removed_chunks": 3, "cleanup_pending": False,
                                  "shared_uploads_retained": False})
        self.assertFalse(self.asset_root.exists())
        self.assertFalse(self.preview_root.exists())
        self.assertFalse((self.root / "uploads" / str(self.batch_id)).exists())
        self.assertEqual(sentinel.read_bytes(), b"other document")
        self.assertNotIn(self.document_id, self.documents)
        self.assertNotIn(self.document_id, self.jobs)

    def test_other_document_in_shared_batch_preserves_sources_and_image_dependencies(self):
        sibling_id = uuid4()
        sibling = self.source.parent / "sibling.md"
        sibling.write_bytes(b"![shared diagram](images/chart.png)")
        self.documents[sibling_id] = {"id": sibling_id, "status": "failed", "filename": "sibling.md",
                                      "source_path": str(sibling.relative_to(self.root)).replace("/", "\\")}

        result = self.service.delete_document(self.document_id)

        self.assertFalse(result["cleanup_pending"])
        self.assertTrue(result["shared_uploads_retained"])
        self.assertTrue(sibling.is_file())
        self.assertTrue(self.source.is_file())
        self.assertEqual(self.image_dependency.read_bytes(), b"offline source image")
        self.assertFalse(self.asset_root.exists())
        self.assertFalse(self.preview_root.exists())

    def test_upload_in_progress_defers_batch_cleanup_and_retry_finishes(self):
        marker = self.root / "uploads" / str(self.batch_id) / ".uploading"
        marker.write_text("uploading", encoding="utf-8")

        first = self.service.delete_document(self.document_id)

        self.assertTrue(first["cleanup_pending"])
        self.assertNotIn(self.document_id, self.documents)
        self.assertIn(self.document_id, self.jobs)
        self.assertFalse(self.asset_root.exists())
        self.assertTrue(self.source.is_file())
        marker.unlink()

        retried = self.service.delete_document(self.document_id)

        self.assertFalse(retried["cleanup_pending"])
        self.assertEqual(retried["removed_chunks"], 3)
        self.assertNotIn(self.document_id, self.jobs)
        self.assertFalse(self.source.exists())

    def test_upload_marker_disappearing_during_shared_query_still_defers_cleanup(self):
        marker = self.root / "uploads" / str(self.batch_id) / ".uploading"
        marker.write_text("uploading", encoding="utf-8")

        def query_after_registration(identifier, callback):
            marker.unlink()
            return self.cleanup_index(identifier, callback)

        self.database.cleanup_deletion.side_effect = query_after_registration

        result = self.service.delete_document(self.document_id)

        self.assertTrue(result["cleanup_pending"])
        self.assertTrue(self.source.is_file())
        self.assertTrue(self.image_dependency.is_file())
        self.assertFalse(self.asset_root.exists())
        self.assertIn(self.document_id, self.jobs)

    def test_retry_path_error_reports_deleted_index_and_retains_cleanup_task(self):
        with patch("kb.service.clean_deletion_files", side_effect=PermissionError("synthetic access denial")):
            first = self.service.delete_document(self.document_id)
        self.assertTrue(first["cleanup_pending"])
        self.job["source_path"] = "../outside.md"

        retried = self.service.delete_document(self.document_id)

        self.assertTrue(retried["deleted"])
        self.assertTrue(retried["cleanup_pending"])
        self.assertNotIn(self.document_id, self.documents)
        self.assertIn(self.document_id, self.jobs)
        self.assertTrue(self.asset.is_file())

    def test_database_commit_error_prevents_every_filesystem_cleanup(self):
        connection = Mock()
        database = Database(self.settings)

        def execute(sql, parameters=()):
            cursor = Mock()
            if "SELECT * FROM kb002.documents" in sql:
                cursor.fetchone.return_value = self.document
            elif "INSERT INTO kb002.deletions" in sql:
                cursor.fetchone.return_value = self.job
            cursor.rowcount = 3
            return cursor

        connection.execute.side_effect = execute

        @contextmanager
        def failing_commit():
            yield connection
            raise psycopg.OperationalError("synthetic commit failure")

        database.connect = failing_commit
        database.cleanup_deletion = Mock()
        self.service.database = database

        with self.assertRaises(psycopg.OperationalError):
            self.service.delete_document(self.document_id)

        self.assertTrue(self.source.is_file())
        self.assertTrue(self.asset.is_file())
        self.assertTrue(self.preview.is_file())
        database.cleanup_deletion.assert_not_called()

    def test_storage_preflight_io_error_preserves_index_and_all_files(self):
        with patch("kb.service.deletion_paths", side_effect=PermissionError("synthetic access denial")):
            with self.assertRaises(StorageCheckError):
                self.service.delete_document(self.document_id)

        self.assertIn(self.document_id, self.documents)
        self.database.cleanup_deletion.assert_not_called()
        self.assertTrue(self.asset.is_file())
        self.assertTrue(self.source.is_file())

    def test_cleanup_oserror_returns_pending_and_retry_uses_durable_job(self):
        with patch("kb.service.clean_deletion_files", side_effect=PermissionError("synthetic access denial")):
            first = self.service.delete_document(self.document_id)

        self.assertTrue(first["deleted"])
        self.assertTrue(first["cleanup_pending"])
        self.assertNotIn(self.document_id, self.documents)
        self.assertIn(self.document_id, self.jobs)
        self.assertTrue(self.asset.is_file())

        retried = self.service.delete_document(self.document_id)

        self.assertFalse(retried["cleanup_pending"])
        self.assertFalse(self.asset.exists())
        self.assertNotIn(self.document_id, self.jobs)

    def test_cleanup_database_error_is_reported_as_pending_after_index_removal(self):
        self.database.cleanup_deletion.side_effect = psycopg.OperationalError("synthetic cleanup transaction failure")

        result = self.service.delete_document(self.document_id)

        self.assertTrue(result["deleted"])
        self.assertTrue(result["cleanup_pending"])
        self.assertNotIn(self.document_id, self.documents)
        self.assertTrue(self.asset.is_file())
        self.assertIn(self.document_id, self.jobs)

    def test_http_deletion_conflict_missing_and_invalid_identifier(self):
        client = self.client()
        for status in ("pending", "processing", "preview", "failed"):
            with self.subTest(status=status):
                self.document["status"] = status
                response = client.delete(f"/api/documents/{self.document_id}")
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn(self.document_id, self.documents)
        self.assertEqual(client.delete(f"/api/documents/{uuid4()}").status_code, 404)
        self.assertEqual(client.delete("/api/documents/not-a-uuid").status_code, 422)
        self.assertTrue(self.asset.is_file())
        self.assertTrue(self.source.is_file())

    def test_http_invalid_source_path_is_rejected_before_index_removal(self):
        self.document["source_path"] = "../outside.md"

        response = self.client().delete(f"/api/documents/{self.document_id}")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn(self.document_id, self.documents)
        self.database.cleanup_deletion.assert_not_called()
        self.assertTrue(self.asset.is_file())

    def test_http_database_and_preflight_errors_return_503_without_success(self):
        client = self.client()
        self.database.delete_document.side_effect = psycopg.OperationalError("synthetic private database details")

        unavailable = client.delete(f"/api/documents/{self.document_id}")

        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertNotIn("synthetic private database details", unavailable.text)
        self.database.delete_document.side_effect = self.delete_index
        with patch("kb.service.deletion_paths", side_effect=PermissionError("synthetic access denial")):
            denied = client.delete(f"/api/documents/{self.document_id}")
        self.assertEqual(denied.status_code, 503, denied.text)
        self.assertIn(self.document_id, self.documents)
        self.database.cleanup_deletion.assert_not_called()
        self.assertTrue(self.source.is_file())
        self.assertTrue(self.asset.is_file())

    def test_http_deleted_document_and_original_assets_are_inaccessible_even_if_files_remain(self):
        client = self.client()
        base = f"/api/documents/{self.document_id}"
        self.assertEqual(client.get(base + "/source").status_code, 200)
        self.assertEqual(client.get(base + f"/assets/{self.attempt}/figure.png").status_code, 200)
        with patch("kb.service.clean_deletion_files", side_effect=PermissionError("synthetic cleanup denial")):
            deleted = client.delete(base)

        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["cleanup_pending"])
        self.assertTrue(self.asset.is_file(), "the route must deny a deleted record even before file cleanup")
        self.assertEqual(client.get(base + "/source").status_code, 404)
        self.assertEqual(client.get(base + f"/assets/{self.attempt}/figure.png").status_code, 404)
        pending, = client.get("/api/documents").json()["documents"]
        self.assertEqual((pending["id"], pending["status"]), (str(self.document_id), "cleanup_pending"))
        self.assertEqual(pending["filename"], self.document["filename"])
        self.assertEqual((pending["chunk_count"], pending["image_count"]), (0, 0))
        self.assertIn("清理待重试", pending["error"])
        retried = client.delete(base)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertFalse(retried.json()["cleanup_pending"])
        self.assertEqual(client.delete(base).status_code, 404)

    def test_http_shared_duplicate_keeps_other_document_searchable_and_its_image_accessible(self):
        sibling_id, sibling_attempt = uuid4(), uuid4()
        sibling_source = self.source.parent / "sibling.md"
        sibling_source.write_text("共同审批流程", encoding="utf-8")
        self.documents[sibling_id] = {"id": sibling_id, "filename": "sibling.md", "status": "ready",
                                      "source_path": str(sibling_source.relative_to(self.root))}
        sibling_asset = self.root / "assets" / str(sibling_id) / str(sibling_attempt) / "figure.png"
        sibling_asset.parent.mkdir(parents=True)
        sibling_asset.write_bytes(b"other original diagram")
        rows = []
        for index, (identifier, attempt) in enumerate(((self.document_id, self.attempt),
                                                       (sibling_id, sibling_attempt)), 1):
            rows.append({"id": index, "ordinal": 0, "document_id": identifier, "filename": "manual.md",
                         "text": "共同审批流程", "kind": "image", "asset_name": f"{attempt}/figure.png",
                         "location": {"line_start": 1, "line_end": 1, "heading": "流程"},
                         "score": 0.04 - index / 1000, "similarity": 0.8, "channels": ["semantic"]})
        self.database.search.side_effect = lambda *_: [row for row in rows if row["document_id"] in self.documents]
        client = self.client()
        with patch("kb.service.query_tokens", return_value="'审批'"):
            before = client.post("/api/search", json={"query": "审批"})
            self.assertEqual(before.status_code, 200, before.text)
            self.assertEqual(before.json()["results"][0]["merged_chunk_count"], 2)

            removed = client.delete(f"/api/documents/{self.document_id}")
            after = client.post("/api/search", json={"query": "审批"})

        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertTrue(removed.json()["shared_uploads_retained"])
        self.assertEqual(after.status_code, 200, after.text)
        hit, = after.json()["results"]
        self.assertEqual(hit["document_id"], str(sibling_id))
        self.assertEqual([source["document_id"] for source in hit["sources"]], [str(sibling_id)])
        self.assertEqual(client.get(hit["asset_url"]).content, b"other original diagram")
        self.assertEqual(client.get(f"/api/documents/{sibling_id}/source").status_code, 200)
        self.assertEqual(client.get(f"/api/documents/{self.document_id}/source").status_code, 404)


if __name__ == "__main__":
    unittest.main()
