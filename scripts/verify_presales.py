"""Verify the local presales library with one retained, synthetic DOCX.

Uses the running Web/HTTP MCP server and the configured local embedding model.
Does not call a visual service, upload user documents, or remove existing data.
Only row counts and SHA-256 summaries of the original library are recorded.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from docx import Document
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from psycopg import sql

from kb.config import ROOT, library_settings, load_settings
from kb.database import Database


TAG = "PRESALESWORD20261119"
TITLE = "售前分库 Word 合成验证"
BODY = (TAG + "：售前顾问在需求澄清后整理客户目标、业务场景和约束条件，"
        "输出方案摘要；完成内部评审后再向客户说明演示安排。")
TABLE = (("售前环节", "交付物"), ("需求澄清", "客户目标与业务约束"),
         ("方案评审", "演示范围与验收条件"))
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TOOLS = {"search_knowledge", "list_documents", "get_document"}
REPORT_PATH = ROOT / "data" / "presales-verification.json"


def original_summary(settings):
    """Hash every stored field in a consistent, read-only four-table snapshot."""
    summaries = {}
    with Database(settings).connect() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        active = connection.execute("""
            SELECT count(*) AS count FROM kb002.documents
            WHERE status IN ('pending', 'processing')
        """).fetchone()["count"]
        assert active == 0, "Original library has active ingestion; postpone the probe"
        for table, key in (("documents", "id"), ("chunks", "id"),
                           ("settings", "id"), ("deletions", "document_id")):
            query = sql.SQL("SELECT to_jsonb(t)::text AS item FROM {}.{} t ORDER BY {}")
            rows = connection.execute(query.format(sql.Identifier("kb002"),
                                                   sql.Identifier(table), sql.Identifier(key)))
            digest = hashlib.sha256()
            count = 0
            for row in rows:
                encoded = row["item"].encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                count += 1
            summaries[table] = {"count": count, "sha256": digest.hexdigest()}
    return summaries


def fixture_file():
    """Preserve an existing exact fixture so ZIP timestamps do not change its hash."""
    fixture = ROOT / "data" / "smoke-inputs" / "售前分库-Word合成验证-20261119.docx"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if not fixture.exists():
        document = Document()
        document.core_properties.author = "Local synthetic verification"
        document.core_properties.title = TITLE
        document.add_heading(TITLE, level=1)
        document.add_paragraph(BODY)
        table = document.add_table(rows=len(TABLE), cols=2)
        for row_number, row in enumerate(TABLE):
            for cell_number, text in enumerate(row):
                table.cell(row_number, cell_number).text = text
        with fixture.open("xb") as stream:
            document.save(stream)
    document = Document(fixture)
    assert [paragraph.text for paragraph in document.paragraphs] == [TITLE, BODY], "Fixture body mismatch"
    assert len(document.tables) == 1 and len(document.inline_shapes) == 0, "Fixture must have one table and no images"
    assert tuple(tuple(cell.text for cell in row.cells) for row in document.tables[0].rows) == TABLE, "Fixture table mismatch"
    return fixture


def get_json(client, path):
    response = client.get(path)
    assert response.status_code == 200, "Unexpected HTTP read status"
    return response.json()


def validate_chunks(payload, document_id):
    assert payload["id"] == document_id, "Wrong document in preview or detail"
    chunks = payload["chunks"]
    assert len(chunks) == 2 + sum(len(row) for row in TABLE), "Unexpected fixture chunk count"
    assert payload["chunk_count"] == len(chunks), "Incomplete fixture preview"
    assert {chunk["text"] for chunk in chunks} == {TITLE, BODY, *(cell for row in TABLE for cell in row)}, "Fixture text or table omitted"
    assert all(chunk["kind"] == "text" and not chunk.get("asset_url") for chunk in chunks), "Fixture must not invoke visual parsing"
    for chunk in chunks:
        location = chunk["location"]
        assert location["format"] == "docx" and isinstance(location["paragraph"], int), "Word paragraph position missing"
        assert location["paragraph"] > 0 and "page" not in location, "Word must use honest paragraph positions"
        assert location["heading"] == TITLE, "Word heading path missing"
        assert urlsplit(chunk["source_url"]).path == f"/presales/api/documents/{document_id}/source", "Source escaped the presales library"
        if chunk["text"] in {TITLE, BODY}:
            assert "table" not in location, "Body incorrectly located inside table"
        else:
            assert location["table"] == 1, "Word table position missing"
            row, cell = location["row"], location["cell"]
            assert TABLE[row - 1][cell - 1] == chunk["text"], "Word table cell position incorrect"
    return len(chunks)


def validate_search(payload, document_id):
    results = payload["results"]
    matches = [hit for hit in results if hit["document_id"] == document_id and hit["text"] == BODY]
    assert len(matches) == 1, "Exact fixture paragraph missing from real local retrieval"
    hit = matches[0]
    assert hit["similarity"] >= payload["similarity_threshold"], "Returned result below threshold"
    assert hit["similarity"] > 0.99, "Identical paragraph should have near-identical local embedding"
    assert hit["location"]["format"] == "docx" and hit["location"]["paragraph"] == 2, "Retrieved Word position incorrect"
    assert urlsplit(hit["source_url"]).path == f"/presales/api/documents/{document_id}/source", "Retrieved source escaped the library"
    assert hit["sources"] and all(source["document_id"] == document_id for source in hit["sources"]), "Unexpected merged source"
    return {"results": len(results), "fixture_similarity": hit["similarity"],
            "similarity_threshold": payload["similarity_threshold"], "word_paragraph": 2}


def check_http(settings, report):
    fixture = fixture_file()
    content = fixture.read_bytes()
    report["fixture"] = {"filename": fixture.name, "sha256": hashlib.sha256(content).hexdigest(),
                         "bytes": len(content), "images": 0}
    with httpx.Client(base_url=settings.public_base_url, timeout=180, trust_env=False,
                      follow_redirects=False) as client:
        report["stage"] = "http_health"
        assert get_json(client, "/health")["status"] == "ok"
        assert get_json(client, "/presales/health")["status"] == "ok"
        report["stage"] = "http_upload"
        response = client.post("/presales/api/documents", files={"files": (fixture.name, content, DOCX_TYPE)})
        assert response.status_code == 202, "Synthetic Word upload failed"
        documents = response.json()["documents"]
        assert len(documents) == 1, "Unexpected upload response"
        document_id = documents[0]["id"]
        report["document_id"] = document_id
        deadline = time.monotonic() + 240
        confirmed = False
        while time.monotonic() < deadline:
            report["stage"] = "http_preview"
            snapshot = get_json(client, f"/presales/api/documents/{document_id}/preview?limit=50")
            assert snapshot["status"] != "failed", "Fixture ingestion failed"
            if snapshot["status"] in {"preview", "ready"}:
                report["indexed_chunks"] = validate_chunks(snapshot, document_id)
                assert snapshot["image_count"] == 0, "Image-free fixture must not call vision"
                if snapshot["status"] == "preview":
                    token = snapshot["preview_token"]
                    assert token and snapshot["can_confirm"] is True, "Preview version cannot be confirmed"
                    report["stage"] = "http_confirm"
                    response = client.post(f"/presales/api/documents/{document_id}/confirm", json={"preview_token": token})
                    assert response.status_code == 200, "Confirming current preview version failed"
                    assert response.json()["preview_token"] == token, "Confirmation used a different preview version"
                    confirmed = True
                else:
                    break
            time.sleep(1)
        else:
            raise TimeoutError("Synthetic Word indexing timeout")
        report["confirmed_current_preview"] = confirmed
        report["reused_ready_fixture"] = not confirmed
        report["stage"] = "http_search"
        response = client.post("/presales/api/search", json={"query": BODY, "limit": 20})
        assert response.status_code == 200, "Real local model search failed"
        report["http_search"] = validate_search(response.json(), document_id)
        report["stage"] = "http_source"
        source = client.get(f"/presales/api/documents/{document_id}/source")
        assert source.status_code == 200, "Word source unavailable"
        assert source.headers.get("content-type", "").split(";", 1)[0] == DOCX_TYPE, "Word source MIME incorrect"
        assert source.content == content, "Word source bytes changed"
        report["source"] = {"http_status": 200, "mime": DOCX_TYPE, "bytes_unchanged": True}
        report["stage"] = "http_isolation"
        original = get_json(client, "/api/documents")["documents"]
        assert document_id not in {document["id"] for document in original}, "Presales document leaked into original list"
        assert client.get(f"/api/documents/{document_id}/source").status_code == 404, "Original source exposed presales bytes"
        report["http_isolation"] = {"original_list_excludes_fixture": True, "original_source_status": 404}
    return document_id


async def tool_result(session, name, arguments):
    result = await session.call_tool(name, arguments)
    assert not result.isError and isinstance(result.structuredContent, dict), "MCP tool failed or omitted structured result"
    return result.structuredContent


async def check_mcp(settings, document_id, report):
    for prefix, key in (("/presales", "presales_mcp"), ("", "original_mcp")):
        report["stage"] = key
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), trust_env=False,
                                     follow_redirects=False) as client:
            async with streamable_http_client(settings.public_base_url + prefix + "/mcp", http_client=client) as (reader, writer, _):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=180)) as session:
                    await session.initialize()
                    inventory = await session.list_tools()
                    tools = {tool.name for tool in inventory.tools}
                    assert tools == TOOLS, "MCP must expose exactly three read-only tools"
                    listing = await tool_result(session, "list_documents", {})
                    ids = {document["id"] for document in listing["documents"]}
                    if prefix:
                        assert document_id in ids, "Presales MCP list omitted fixture"
                        detail = await tool_result(session, "get_document", {"document_id": document_id, "limit": 50})
                        chunks = validate_chunks(detail, document_id)
                        search = await tool_result(session, "search_knowledge", {"query": BODY, "limit": 20})
                        report[key] = {"tools": sorted(tools), "get_document_chunks": chunks,
                                       "search": validate_search(search, document_id), "listed_fixture": True}
                    else:
                        assert document_id not in ids, "Original MCP list exposed presales fixture"
                        report[key] = {"tools": sorted(tools), "listed_fixture": False}


def main():
    logging.basicConfig(level=logging.WARNING)
    report = {"ok": False, "date": "2026-11-19", "stage": "configuration", "synthetic_tag": TAG,
              "embedding_backend": "local", "vision_calls": 0}
    start = time.monotonic()
    settings = None
    before = None
    try:
        settings = load_settings()
        assert settings.embedding_backend == "local", "Probe only permits a local embedding model"
        presales = library_settings(settings, "presales")
        assert presales.data_dir != settings.data_dir, "Library files must be separate"
        Database(settings).check()
        Database(presales).check()
        report["stage"] = "original_snapshot_before"
        before = original_summary(settings)
        report["original_before"] = before
        document_id = check_http(settings, report)
        asyncio.run(check_mcp(settings, document_id, report))
        report["ok"] = True
    except Exception as error:
        # Neither exception strings nor server/model payloads enter logs/reports.
        report["error_type"] = type(error).__name__
    finally:
        if settings is not None and before is not None:
            try:
                after = original_summary(settings)
                report["original_after"] = after
                report["original_unchanged"] = before == after
                if before != after:
                    report["ok"] = False
                    report["snapshot_error"] = "OriginalSummaryChanged"
            except Exception as error:
                report["ok"] = False
                report["snapshot_error"] = type(error).__name__
        report["elapsed_seconds"] = round(time.monotonic() - start, 1)
        if report["ok"]:
            report["stage"] = "complete"
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
