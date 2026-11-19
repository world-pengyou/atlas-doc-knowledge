"""Check the two worker phases with real snapshot files and isolated model/database doubles."""
import json
import shutil
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from PIL import Image

from kb.database import Database
from kb.models import ModelError
from kb.service import KnowledgeService
from kb.types import ParsedBlock


class PreviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("preview-fixture-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        settings = SimpleNamespace(data_dir=self.root, public_base_url="http://127.0.0.1:8000",
                                   vision_base_url="http://127.0.0.1:9876", vision_model="offline-vision")
        self.db = MagicMock(spec=Database)
        self.service = KnowledgeService(settings, self.db)
        self.service.models = MagicMock()
        self.stop = threading.Event()
        self.document = {"id": uuid4(), "filename": "manual.pdf", "source_path": "manual.pdf",
                         "status": "processing", "confirmed_at": None, "preview_token": None,
                         "preview_path": None, "claim_token": uuid4(), "chunk_count": 0,
                         "image_count": 0, "error": None}
        (self.root / self.document["source_path"]).write_bytes(b"isolated parser input")
        self.service.models.describe_images.side_effect = (
            lambda batch: [f"视觉结果 {path.stem}" for path, _ in batch]
        )
        self.service.models.embed.side_effect = lambda texts: [[0.25, 0.75] for _ in texts]

    def prepare(self, image_count=0):
        def parsed(_source, assets):
            blocks = [ParsedBlock("材料正文段落", "text", {"page": 1})]
            for index in range(1, image_count + 1):
                image = assets / f"figure-{index}.png"
                with Image.new("RGB", (2, 2), (index, 80, 12)) as canvas:
                    canvas.save(image)
                blocks.append(ParsedBlock(f"图 {index}", "image",
                                          {"page": index, "preview": image.name, "bbox": [5, 6, 10, 16]},
                                          str(image)))
            return blocks

        with patch("kb.service.parse_document", side_effect=parsed) as parser:
            self.service.ingest(self.document, self.stop)
        parser.assert_called_once()
        self.db.publish.assert_not_called()
        self.service.models.embed.assert_not_called()
        self.db.save_preview.assert_called_once()
        doc_id, token, path, count, visuals = self.db.save_preview.call_args.args
        self.assertEqual(str(doc_id), str(self.document["id"]))
        self.assertEqual(token, self.document["claim_token"])
        self.assertEqual(count, image_count + 1)
        self.assertEqual(visuals, image_count)
        row = {**self.document, "status": "preview", "claim_token": None,
               "preview_path": path, "preview_token": token, "chunk_count": count, "image_count": visuals}
        self.db.document.return_value = row
        return row

    def test_unconfirmed_preview_batches_21_images_without_embedding(self):
        self.prepare(image_count=21)
        calls = self.service.models.describe_images.call_args_list
        sizes = [len(call.args[0]) for call in calls]
        self.assertEqual(sizes, [10, 10, 1])
        self.assertEqual([image.name for call in calls for image, _ in call.args[0]],
                         [f"figure-{n}.png" for n in range(1, 22)])

    def test_preview_paginates_original_picture_and_source_coordinates(self):
        row = self.prepare(image_count=2)
        preview = self.service.preview_document(row["id"], offset=1, limit=1)
        chunk, = preview["chunks"]
        self.assertEqual((preview["offset"], preview["chunk_count"], preview["image_count"]), (1, 3, 2))
        self.assertEqual(preview["preview_token"], str(row["preview_token"]))
        self.assertTrue(preview["can_confirm"])
        self.assertIn("视觉结果 figure-1", chunk["text"])
        self.assertTrue(chunk["asset_url"].endswith(f"{row['preview_token']}/figure-1.png"))
        self.assertEqual(chunk["location"]["preview_url"], chunk["asset_url"])
        self.assertEqual(chunk["location"]["bbox"], [5, 6, 10, 16])
        self.assertTrue(chunk["source_url"].endswith("#page=1"))
        self.assertEqual(self.service.preview_document(row["id"], offset=3, limit=1)["chunks"], [])
        for offset, limit in ((-1, 1), (0, 0), (0, 51)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(ValueError):
                self.service.preview_document(row["id"], offset, limit)
        self.db.connect.assert_not_called()

    def test_confirmed_retry_indexes_exact_preview_without_source_or_new_vision(self):
        row = self.prepare(image_count=2)
        preview = self.service.preview_document(row["id"])["chunks"]
        snapshot = self.root / row["preview_path"]
        original_snapshot = snapshot.read_bytes()
        confirmed = {**row, "status": "processing", "confirmed_at": datetime.now(timezone.utc),
                     "claim_token": uuid4()}
        self.service.models.describe_images.reset_mock()
        self.service.models.embed.side_effect = ModelError("临时向量服务故障")
        (self.root / row["source_path"]).unlink()
        with patch("kb.service.parse_document", side_effect=AssertionError("确认后重复解析")):
            with self.assertRaises(ModelError):
                self.service.ingest(confirmed, self.stop)
            self.db.publish.assert_not_called()
            self.service.models.embed.side_effect = lambda texts: [[0.25, 0.75] for _ in texts]
            confirmed["claim_token"] = uuid4()
            self.service.ingest(confirmed, self.stop)
        self.service.models.describe_images.assert_not_called()
        self.service.models.describe_image.assert_not_called()
        self.assertEqual(snapshot.read_bytes(), original_snapshot)
        self.db.publish.assert_called_once()
        doc_id, claim_token, chunks, image_count = self.db.publish.call_args.args
        self.assertEqual((doc_id, claim_token, image_count), (row["id"], confirmed["claim_token"], 2))
        self.assertEqual([c["text"] for c in chunks], [c["text"] for c in preview])
        for chunk, displayed in zip(chunks, preview):
            self.assertEqual(chunk["kind"], displayed["kind"])
            self.assertEqual(chunk["location"], {k: v for k, v in displayed["location"].items() if k != "preview_url"})
            self.assertEqual(chunk["embedding"], [0.25, 0.75])
            self.assertTrue(chunk["tokens"])

    def test_preview_rejects_foreign_path_token_and_snapshot_identity(self):
        row = self.prepare()
        for change in ({"id": uuid4()}, {"preview_token": uuid4()},
                       {"preview_path": str(Path("uploads") / (str(row["preview_token"]) + ".json"))}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "预览路径无效"):
                self.db.document.return_value = {**row, **change}
                self.service.preview_document(self.db.document.return_value["id"])
        self.db.document.return_value = row
        path = self.root / row["preview_path"]
        original = json.loads(path.read_text(encoding="utf-8"))
        for key in ("document_id", "preview_token"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "版本与文档不一致"):
                path.write_text(json.dumps({**original, key: str(uuid4())}), encoding="utf-8")
                self.service.preview_document(row["id"])
        self.service.models.embed.assert_not_called()
        self.db.publish.assert_not_called()

    def test_preexisting_ready_document_displays_chunks_without_snapshot(self):
        row = {**self.document, "status": "ready", "chunk_count": 1}
        self.db.document.return_value = row
        connection = self.db.connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = [
            {"document_id": row["id"], "filename": row["filename"], "text": "历史片段",
             "kind": "text", "location": {"page": 2}, "asset_name": None}
        ]
        preview = self.service.preview_document(row["id"], 0, 1)
        chunk, = preview["chunks"]
        self.assertEqual(chunk["text"], "历史片段")
        self.assertIsNone(preview["preview_token"])
        self.assertFalse(preview["can_confirm"])
        self.assertTrue(chunk["source_url"].endswith("#page=2"))
        sql, params = connection.execute.call_args.args
        self.assertIn("kb002.chunks", sql)
        self.assertIn("d.status = 'ready'", sql)
        self.assertEqual(params, (row["id"], 1, 0))

    def test_confirmation_rejects_stale_token_and_is_idempotent_after_success(self):
        database = Database(SimpleNamespace())
        database.connect = MagicMock()
        connection = database.connect.return_value.__enter__.return_value
        token = uuid4()
        row = {"id": self.document["id"], "preview_token": token, "status": "preview",
               "confirmed_at": None}
        cursor = connection.execute.return_value
        cursor.fetchone.return_value = row
        with self.assertRaises(ValueError):
            database.confirm(row["id"], uuid4())
        self.assertEqual(connection.execute.call_count, 1)
        self.assertIn("FOR UPDATE", connection.execute.call_args.args[0])
        connection.execute.reset_mock()
        confirmed = {**row, "status": "pending", "confirmed_at": datetime.now(timezone.utc)}
        cursor.fetchone.side_effect = [row, confirmed]
        self.assertEqual(database.confirm(row["id"], token), confirmed)
        self.assertEqual(connection.execute.call_count, 2)
        self.assertIn("UPDATE kb002.documents", connection.execute.call_args.args[0])
        for status in ("pending", "processing", "ready"):
            with self.subTest(status=status):
                connection.execute.reset_mock()
                replay = {**confirmed, "status": status}
                cursor.fetchone.side_effect = None
                cursor.fetchone.return_value = replay
                self.assertEqual(database.confirm(row["id"], token), replay)
                self.assertEqual(connection.execute.call_count, 1)
