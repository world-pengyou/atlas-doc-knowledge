"""Verify deletion using only two disposable documents created by this probe."""
import io
import json
import time
from uuid import UUID, uuid4

import httpx
from PIL import Image

from kb.config import ROOT, load_settings
from kb.database import Database
from kb.storage import deletion_paths


def snapshot(database):
    with database.connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        rows = connection.execute("""
            SELECT d.id::text, d.status, d.chunk_count, d.image_count,
                md5(coalesce(string_agg(ROW(c.id,c.ordinal,c.text,c.location,c.asset_name,c.embedding)::text,
                    E'\\n' ORDER BY c.id), '')) AS digest
            FROM kb002.documents d LEFT JOIN kb002.chunks c ON c.document_id=d.id
            GROUP BY d.id ORDER BY d.id
        """).fetchall()
    return {row["id"]: row for row in rows}


def main():
    settings = load_settings()
    assert settings.embedding_backend == "local", "本探针仅使用本地向量模型"
    database = Database(settings)
    database.check()
    original = snapshot(database)
    tag = "DELETESMOKE" + uuid4().hex
    names = [tag + "-a.md", tag + "-b.md"]
    common = tag + " 合成删除验证内容，仅用于本轮测试。"
    bodies = [("# 临时样本 " + name[-4] + "\n\n" + common + "\n").encode() for name in names]
    picture = io.BytesIO()
    Image.new("RGB", (8, 8), "#287654").save(picture, format="PNG")
    image = picture.getvalue()
    report = {"ok": False, "scope": "only documents created by this probe", "tag": tag,
              "original_documents": len(original)}
    report_path = ROOT / "data" / "delete-verification.json"
    with httpx.Client(base_url=settings.public_base_url, timeout=150, trust_env=False) as client:
        client.get("/health").raise_for_status()
        response = client.post("/api/documents", files=[
            ("files", (names[0], bodies[0], "text/markdown")),
            ("files", (names[1], bodies[1], "text/markdown")),
            ("files", ("images/shared.png", image, "image/png")),
        ])
        response.raise_for_status()
        uploaded = response.json()["documents"]
        assert {row["filename"] for row in uploaded} == set(names)
        identifiers = [row["id"] for row in uploaded]
        report["owned_test_documents"] = identifiers
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已创建两份本轮专用临时文档", flush=True)
        deadline = time.monotonic() + 180
        status_gate_checked = False
        while time.monotonic() < deadline:
            states = []
            for identifier in identifiers:
                detail = client.get("/api/documents/" + identifier)
                detail.raise_for_status()
                status = detail.json()["status"]
                states.append(status)
                assert status != "failed", "测试样本入库失败"
                if status == "preview":
                    assert client.delete("/api/documents/" + identifier).status_code == 409
                    status_gate_checked = True
                    preview = client.get("/api/documents/" + identifier + "/preview")
                    preview.raise_for_status()
                    assert preview.json()["image_count"] == 0
                    confirmed = client.post("/api/documents/" + identifier + "/confirm",
                                            json={"preview_token": preview.json()["preview_token"]})
                    confirmed.raise_for_status()
            if all(status == "ready" for status in states):
                break
            time.sleep(1)
        else:
            raise TimeoutError("等待本轮临时文档入库超时")
        plans, assets = [], []
        for identifier, expected_body in zip(identifiers, bodies):
            document = database.document(UUID(identifier))
            assert document["filename"] in names and document["status"] == "ready"
            assert client.get("/api/documents/" + identifier + "/source").content == expected_body
            plan = deletion_paths(settings.data_dir, identifier, document["source_path"])
            plans.append(plan)
            attempt = str(uuid4())
            path = plan.assets / attempt / "fixture.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(image)
            url = "/api/documents/" + identifier + "/assets/" + attempt + "/fixture.png"
            assert client.get(url).status_code == 200
            assets.append(url)
        assert plans[0].batch == plans[1].batch
        batch = plans[0].batch
        assert (batch / "images" / "shared.png").is_file()

        def search_sources():
            found = client.post("/api/search", json={"query": tag, "limit": 20})
            found.raise_for_status()
            return [source["document_id"] for hit in found.json()["results"] for source in hit["sources"]]

        assert set(identifiers).issubset(set(search_sources()))
        first = client.delete("/api/documents/" + identifiers[0])
        first.raise_for_status()
        assert first.json()["deleted"] and not first.json()["cleanup_pending"]
        assert first.json()["shared_uploads_retained"]
        assert batch.is_dir() and (batch / "images" / "shared.png").is_file()
        assert not plans[0].assets.exists() and not plans[0].previews.exists()
        assert client.get("/api/documents/" + identifiers[0] + "/source").status_code == 404
        assert client.get(assets[0]).status_code == 404
        assert client.get(assets[1]).status_code == 200
        assert client.get("/api/documents/" + identifiers[1] + "/source").status_code == 200
        sources = search_sources()
        assert identifiers[0] not in sources and identifiers[1] in sources
        print("已验证首份删除、共享附件保留及另一来源继续检索", flush=True)

        # This probe owns the entire new batch; simulate registration in flight
        # to verify durable cleanup/retry, without changing permissions or locks.
        marker = batch / ".uploading"
        assert not marker.exists()
        marker.touch(exist_ok=False)
        try:
            pending = client.delete("/api/documents/" + identifiers[1])
            pending.raise_for_status()
            assert pending.json()["deleted"] and pending.json()["cleanup_pending"]
            assert client.get("/api/documents/" + identifiers[1] + "/source").status_code == 404
            assert client.get(assets[1]).status_code == 404
            listing = client.get("/api/documents").json()["documents"]
            assert any(row["id"] == identifiers[1] and row["status"] == "cleanup_pending" for row in listing)
            assert batch.exists()
        finally:
            marker.unlink(missing_ok=True)
        retried = client.delete("/api/documents/" + identifiers[1])
        retried.raise_for_status()
        assert not retried.json()["cleanup_pending"]
        assert not batch.exists()
        assert all(not plan.assets.exists() and not plan.previews.exists() for plan in plans)
        assert all(identifier not in search_sources() for identifier in identifiers)
        assert client.delete("/api/documents/" + identifiers[1]).status_code == 404
    after = snapshot(database)
    assert all(after.get(identifier) == row for identifier, row in original.items()), "原有文档或切片发生变化"
    assert all(identifier not in after for identifier in identifiers)
    with database.connect() as connection:
        for identifier in identifiers:
            assert connection.execute("SELECT 1 FROM kb002.deletions WHERE document_id=%s",
                                      (UUID(identifier),)).fetchone() is None
    report.update(ok=True, preview_delete_blocked=status_gate_checked, shared_batch_preserved=True,
                  deleted_sources_404=True, other_document_searchable=True, cleanup_retry_passed=True,
                  last_reference_batch_removed=True, private_assets_and_previews_removed=True,
                  original_documents_and_chunk_digests_unchanged=True, cleanup_jobs_removed=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
