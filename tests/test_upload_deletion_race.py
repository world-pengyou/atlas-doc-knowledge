"""Cancellation must not expose a still-registering upload batch to deletion."""
import asyncio
import io
import shutil
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import UploadFile

from kb.app import upload_documents


class UploadDeletionRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_registration_keeps_batch_marker_until_result_is_known(self):
        root = Path(__file__).parent / ("delete-upload-race-" + uuid4().hex)
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def register(document_id, filename, digest, source_path):
            entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError("test registration release timed out")
                return {"id": document_id, "filename": filename, "status": "pending", "created": True}
            finally:
                finished.set()

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            worker=SimpleNamespace(error=None), service=SimpleNamespace(
                settings=SimpleNamespace(data_dir=root), database=SimpleNamespace(add_document=register)))))
        upload = UploadFile(filename="cancelled.md", file=io.BytesIO(b"synthetic cancellation fixture"))
        task = asyncio.create_task(upload_documents(request, [upload]))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            batch, = (root / "uploads").iterdir()
            self.assertTrue((batch / ".uploading").is_file())
            self.assertTrue((batch / "cancelled.md").is_file())
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 3))
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    unittest.main()
