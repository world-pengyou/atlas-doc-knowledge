import threading
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from kb.database import Database
from kb.models import ModelError
from kb.service import KnowledgeService, query_tokens, split_text


class ServiceTests(unittest.TestCase):
    def test_long_unpunctuated_text_keeps_all_characters(self):
        text = "甲" * 2200
        parts = list(split_text(text))
        self.assertTrue(all(len(part) <= 700 for part, _, _ in parts))
        self.assertEqual(parts[0][1], 0)
        self.assertEqual(parts[-1][2], len(text))
        self.assertTrue(all(parts[i + 1][1] <= part[2] for i, part in enumerate(parts[:-1])))

    def test_chinese_query_uses_safe_or_lexemes(self):
        value = query_tokens("主管驳回后修改 TS-001")
        self.assertIn(" | ", value)
        self.assertIn("修改", value)
        self.assertNotIn(";", query_tokens("x'; SELECT @foo"))

    def test_failure_never_publishes_partial_chunks(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            settings = SimpleNamespace(data_dir=Path(temporary), public_base_url="http://127.0.0.1:8000")
            database = Mock()
            service = KnowledgeService(settings, database)
            service.models = Mock()
            service.models.embed.side_effect = ModelError("服务不可用")
            document_id, preview_token = uuid4(), uuid4()
            preview = settings.data_dir / "previews" / str(document_id) / (str(preview_token) + ".json")
            preview.parent.mkdir(parents=True)
            preview.write_text(json.dumps({"document_id": str(document_id), "preview_token": str(preview_token),
                                          "chunks": [{"text": "审批流程", "kind": "text",
                                                      "location": {"line_start": 1}, "asset_name": None}]}), encoding="utf-8")
            document = {"id": document_id, "claim_token": uuid4(), "confirmed_at": "confirmed",
                        "preview_token": preview_token, "preview_path": str(preview.relative_to(settings.data_dir)),
                        "chunk_count": 1, "image_count": 0}
            with self.assertRaises(ModelError):
                service.ingest(document, threading.Event())
            database.publish.assert_not_called()

    def test_publish_checks_claim_before_deleting_chunks(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = None
        database = Database(SimpleNamespace())

        @contextmanager
        def connect():
            yield connection

        database.connect = connect
        with self.assertRaises(InterruptedError):
            database.publish(uuid4(), uuid4(), [], 0)
        self.assertEqual(connection.execute.call_count, 1)
        self.assertIn("FOR UPDATE", connection.execute.call_args.args[0])

    def test_image_and_page_urls_keep_attempt_and_coordinates(self):
        service = KnowledgeService(SimpleNamespace(public_base_url="http://127.0.0.1:8000"), Mock())
        attempt = str(uuid4())
        result = service.result({"document_id": uuid4(), "filename": "流程.pdf", "text": "申请→审批",
                                 "kind": "image", "location": {"page": 3, "bbox": [1, 2, 3, 4],
                                 "preview": attempt + "/page-3.png"},
                                 "asset_name": attempt + "/image.png"})
        self.assertTrue(result["source_url"].endswith("#page=3"))
        self.assertIn(attempt, result["asset_url"])
        self.assertEqual(result["location"]["bbox"], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
