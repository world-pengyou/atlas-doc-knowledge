"""Real presales DOCX acceptance with synthetic text, table and one image.

The existing server calls its configured visual endpoint for this new fixture.
Only synthetic content is uploaded. Existing documents are never edited/deleted.
"""
import argparse
import asyncio
import hashlib
import io
import json
import logging
import math
import re
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from zipfile import ZipFile

import httpx
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image, ImageChops, ImageDraw, ImageFont

from kb.config import ROOT, library_settings, load_settings
from kb.database import Database
from scripts.verify_presales import DOCX_TYPE, get_json, original_summary, tool_result


DIRECTORY = ROOT / "data" / "smoke-inputs" / "presales-word-images-20261119"
FIXTURE = DIRECTORY / "售前库图文验收样本.docx"
PICTURE = DIRECTORY / "review-flow.png"
REPORT = ROOT / "data" / "presales-images-verification.json"
TITLE = "售前库图文验收样本"
BODY = "BODYQ482 售前验证计划要求在周三下午三点完成兼容性检查，负责人为方案顾问，检查记录由项目经理复核。"
TABLE = (("演示方案", "验收说明"),
         ("基础演示", "TABLEM637 基础演示包包含六台离线演示机，设备清单由交付顾问核对。"),
         ("扩展演示", "扩展演示包增加一套备用投影设备，现场连接由实施顾问检查。"))
IMAGE_CODE = "IMGONLYZX719"
IMAGE_ONLY_TERMS = (IMAGE_CODE, "售前修改", "驳回", "客户演示")
TEXT_QUERY = "兼容性检查应在周几几点完成，由谁负责和复核？"
TABLE_QUERY = "基础演示包包含几台离线演示机，设备清单由谁核对？"
IMAGE_QUERY = "图中的方案评审被驳回后由谁修改，修改后返回哪个环节？"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def draw_picture():
    image = Image.new("RGB", (1400, 780), "white")
    draw = ImageDraw.Draw(image)
    font_path = "C:/Windows/Fonts/msyh.ttc"
    font = ImageFont.truetype(font_path, 42)
    small = ImageFont.truetype(font_path, 32)
    draw.text((55, 30), "方案评审流程", fill="#14263d", font=font)
    draw.text((970, 37), IMAGE_CODE, fill="#14263d", font=small)

    def box(rect, text, fill):
        draw.rounded_rectangle(rect, radius=18, fill=fill, outline="#3c536f", width=3)
        bounds = draw.textbbox((0, 0), text, font=font)
        x = (rect[0] + rect[2] - (bounds[2] - bounds[0])) / 2
        y = (rect[1] + rect[3] - (bounds[3] - bounds[1])) / 2 - bounds[1]
        draw.text((x, y), text, fill="#14263d", font=font)

    def arrow(points, label=None, label_at=None, color="#3c536f"):
        draw.line(points, fill=color, width=6, joint="curve")
        x, y = points[-1]
        before_x, before_y = points[-2]
        angle = math.atan2(y - before_y, x - before_x)
        sides = [(x - 22 * math.cos(angle + turn), y - 22 * math.sin(angle + turn))
                 for turn in (-0.45, 0.45)]
        draw.polygon([(x, y), *sides], fill=color)
        if label:
            draw.text(label_at, label, fill=color, font=small)

    box((65, 230, 365, 350), "需求澄清", "#edf4fb")
    box((550, 230, 850, 350), "方案评审", "#edf4fb")
    box((1035, 230, 1335, 350), "客户演示", "#edf8ef")
    box((550, 560, 850, 680), "售前修改", "#fff1e9")
    arrow([(365, 290), (550, 290)])
    arrow([(850, 290), (1035, 290)], "通过", (900, 235), "#267243")
    arrow([(700, 350), (700, 560)], "驳回", (720, 430), "#a43a24")
    arrow([(550, 620), (440, 620), (440, 345), (550, 345)],
          "重新提交", (290, 465), "#a43a24")
    image.save(PICTURE, "PNG")


def prepare_fixture():
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if not FIXTURE.exists():
        draw_picture()
        document = Document()
        section = document.sections[0]
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.top_margin = section.bottom_margin = Cm(1.8)
        section.left_margin = section.right_margin = Cm(1.8)
        for style_name in ("Normal", "Title", "Heading 1"):
            style = document.styles[style_name]
            style.font.name = "Microsoft YaHei"
            style.font.color.rgb = RGBColor(0, 0, 0)
            style.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        document.styles["Normal"].font.size = Pt(11)
        document.styles["Normal"].paragraph_format.space_after = Pt(6)
        document.styles["Title"].font.size = Pt(22)
        document.styles["Heading 1"].font.size = Pt(14)
        document.core_properties.author = "Local acceptance test"
        document.core_properties.title = TITLE
        document.add_paragraph(TITLE, style="Title")
        document.add_heading("正文说明", level=1)
        document.add_paragraph(BODY)
        document.add_heading("方案交付表", level=1)
        table = document.add_table(rows=len(TABLE), cols=2)
        table.autofit = False
        table.columns[0].width, table.columns[1].width = Cm(3.2), Cm(14.2)
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            border = OxmlElement("w:" + edge)
            for name, value in (("val", "single"), ("sz", "6"), ("color", "D9D9D9")):
                border.set(qn("w:" + name), value)
            borders.append(border)
        table._tbl.tblPr.append(borders)
        for row_number, values in enumerate(TABLE):
            for cell_number, text in enumerate(values):
                cell = table.cell(row_number, cell_number)
                cell.text = text
                if row_number == 0:
                    shading = OxmlElement("w:shd")
                    shading.set(qn("w:fill"), "E8EFF7")
                    cell._tc.get_or_add_tcPr().append(shading)
                    for run in cell.paragraphs[0].runs:
                        run.bold = True
        document.add_heading("评审流程", level=1)
        document.add_paragraph("评审的条件、流向及责任人见下图。")
        shape = document.add_paragraph().add_run().add_picture(str(PICTURE), width=Cm(17.0))
        shape._inline.docPr.set("descr", "评审流程示意图")
        with FIXTURE.open("xb") as output:
            document.save(output)
    with ZipFile(FIXTURE) as package:
        parts = package.namelist()
        xml = b"\n".join(package.read(name) for name in parts if name.endswith((".xml", ".rels")))
        for term in IMAGE_ONLY_TERMS:
            assert term.encode("utf-8") not in xml, "Image-only evidence leaked into DOCX text or metadata"
        media = [name for name in parts if name.startswith("word/media/")]
        assert len(media) == 1, "Fixture must contain exactly one embedded picture"
        assert package.read(media[0]) == PICTURE.read_bytes(), "Embedded picture differs from ground truth"
    document = Document(FIXTURE)
    assert BODY in [paragraph.text for paragraph in document.paragraphs], "Fixture body mismatch"
    assert tuple(tuple(cell.text for cell in row.cells) for row in document.tables[0].rows) == TABLE
    return FIXTURE


def complete_preview(client, document_id):
    base = f"/presales/api/documents/{document_id}/preview"
    snapshot = get_json(client, base + "?offset=0&limit=50")
    chunks = list(snapshot["chunks"])
    while len(chunks) < snapshot["chunk_count"]:
        page = get_json(client, base + f"?offset={len(chunks)}&limit=50")
        assert page["preview_token"] == snapshot["preview_token"], "Preview version changed while reading"
        assert page["chunks"], "Preview pagination ended before all chunks were read"
        chunks.extend(page["chunks"])
    return {**snapshot, "chunks": chunks}


def assert_preview(snapshot, document_id):
    chunks = snapshot["chunks"]
    expected = {TITLE, "正文说明", BODY, "方案交付表", "评审流程",
                "评审的条件、流向及责任人见下图。", *(value for row in TABLE for value in row)}
    assert {chunk["text"] for chunk in chunks if chunk["kind"] == "text"} == expected, "Body or table text omitted"
    images = [chunk for chunk in chunks if chunk["kind"] == "image"]
    assert snapshot["image_count"] == 1 and images, "Embedded image was not parsed"
    image_text = "\n".join(chunk["text"] for chunk in images)
    for term in IMAGE_ONLY_TERMS:
        assert term.casefold() in image_text.casefold(), "Visual result omitted required image-only evidence"
    assert_flow_relationships(image_text)
    assert "图片模型解析" in image_text, "Image lacks a real visual description"
    for chunk in chunks:
        location = chunk["location"]
        assert location["format"] == "docx" and location["paragraph"] > 0 and "page" not in location
        assert urlsplit(chunk["source_url"]).path == f"/presales/api/documents/{document_id}/source"
        if chunk["text"] == BODY:
            assert location["paragraph"] == 3 and location["heading"] == "正文说明"
        if "table" in location:
            assert location["table"] == 1
            assert TABLE[location["row"] - 1][location["cell"] - 1] == chunk["text"]
        if chunk["kind"] == "image":
            assert location["paragraph"] == 13 and location["image"] == 1 and location["heading"] == "评审流程"
            assert chunk["asset_url"] and location["preview_url"] == chunk["asset_url"]
    return image_text


def assert_flow_relationships(text):
    """For this fixture, verify branch direction as well as individual labels."""
    normalized = text.replace("分支条件", "").replace("返回路径", "")
    normalized = re.sub(r"[\s*`（）():：]", "", normalized)
    assert "方案评审→驳回→售前修改" in normalized, "Visual result reversed or omitted the rejected branch"
    assert "售前修改→重新提交→方案评审" in normalized, "Visual result reversed or omitted the return path"


def chunk_projection(chunks):
    return [{key: chunk[key] for key in ("text", "kind", "location", "asset_url", "source_url")}
            for chunk in chunks]


def verify_assets(client, snapshot, document_id):
    names = {chunk["asset_url"] for chunk in snapshot["chunks"] if chunk["asset_url"]}
    assert len(names) == 1, "Expected one original image URL"
    records = []
    for url in names:
        path = urlsplit(url).path
        assert path.startswith(f"/presales/api/documents/{document_id}/assets/")
        response = client.get(url)
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        with Image.open(PICTURE) as original, Image.open(io.BytesIO(response.content)) as downloaded:
            assert original.size == downloaded.size
            assert ImageChops.difference(original.convert("RGB"), downloaded.convert("RGB")).getbbox() is None
        assert client.get(path.removeprefix("/presales")).status_code == 404
        records.append({"url": url, "status": 200, "sha256": digest(response.content),
                        "original_pixels_unchanged": True, "original_library_status": 404})
    return records


def find_evidence(payload, document_id, category):
    matches = []
    for hit in payload["results"]:
        evidence = [hit, *hit.get("sources", [])]
        if not any(item["document_id"] == document_id for item in evidence):
            continue
        if category == "body":
            valid = hit["kind"] == "text" and BODY in hit["text"] and hit["location"].get("paragraph") == 3
        elif category == "table":
            valid = (hit["kind"] == "text" and TABLE[1][1] in hit["text"]
                     and all(hit["location"].get(key) == value for key, value in (("table", 1), ("row", 2), ("cell", 2))))
        else:
            valid = (hit["kind"] == "image" and "售前修改" in hit["text"] and "驳回" in hit["text"]
                     and hit["location"].get("image") == 1 and hit.get("asset_url"))
        if valid:
            if category == "image":
                assert_flow_relationships(hit["text"])
            assert hit["similarity"] >= payload["similarity_threshold"]
            matches.append(hit)
    assert matches, "Natural-language retrieval did not return the required evidence"
    hit = matches[0]
    assert hit["document_id"] == document_id
    return {"category": category, "document_id": document_id, "kind": hit["kind"],
            "similarity": hit["similarity"], "location": hit["location"], "text": hit["text"],
            "asset_url": hit.get("asset_url"), "source_url": hit["source_url"],
            "merged_chunk_count": hit.get("merged_chunk_count", 1)}


async def verify_mcp(settings, document_id):
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), trust_env=False) as http:
        async with streamable_http_client(settings.public_base_url + "/presales/mcp", http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=180)) as session:
                await session.initialize()
                listing = await tool_result(session, "list_documents", {})
                assert document_id in {item["id"] for item in listing["documents"]}
                results = {}
                for category, query in (("body", TEXT_QUERY), ("table", TABLE_QUERY), ("image", IMAGE_QUERY)):
                    payload = await tool_result(session, "search_knowledge", {"query": query, "limit": 20})
                    results[category] = find_evidence(payload, document_id, category)
                detail = await tool_result(session, "get_document", {"document_id": document_id, "limit": 50})
                assert detail["status"] == "ready" and any(chunk["kind"] == "image" for chunk in detail["chunks"])
                return {"searches": results, "listed": True, "get_document_image_present": True}


def run_verification(report):
    settings = load_settings()
    presales = library_settings(settings, "presales")
    assert settings.embedding_backend == "local", "This probe requires the existing local embedding model"
    assert settings.vision_base_url and settings.vision_model, "Visual model is not configured"
    before = original_summary(settings)
    report["original_before"] = before
    database = Database(presales)
    try:
        fixture = prepare_fixture()
        content = fixture.read_bytes()
        content_hash = digest(content)
        report["fixture"] = {"path": str(fixture), "sha256": content_hash,
                             "embedded_picture_sha256": digest(PICTURE.read_bytes()), "images": 1,
                             "image_evidence_absent_from_docx_text": True}
        with database.connect() as connection:
            previous = connection.execute("SELECT id, status, confirmed_at FROM kb002.documents WHERE content_hash=%s",
                                          (content_hash,)).fetchone()
        if previous:
            assert previous["status"] in {"pending", "processing", "preview"} and previous["confirmed_at"] is None, \
                "Fixture already indexed; preserve the first report and use a distinct approved fixture for a new ingestion"
            report["resumed_unconfirmed_fixture"] = True
        else:
            report["first_ingestion"] = True
        with httpx.Client(base_url=settings.public_base_url, timeout=180, trust_env=False) as client:
            for route in ("/health", "/presales/health"):
                assert get_json(client, route)["status"] == "ok"
            report["stage"] = "upload"
            response = client.post("/presales/api/documents", files={"files": (fixture.name, content, DOCX_TYPE)})
            assert response.status_code == 202, "Synthetic image Word upload failed"
            uploaded, = response.json()["documents"]
            document_id = uploaded["id"]
            UUID(document_id)
            report["document_id"] = document_id
            report["states"] = [uploaded["status"]]
            deadline = time.monotonic() + 300
            report["stage"] = "waiting_for_visual_preview"
            while time.monotonic() < deadline:
                snapshot = complete_preview(client, document_id)
                if snapshot["status"] != report["states"][-1]:
                    report["states"].append(snapshot["status"])
                assert snapshot["status"] != "failed", "Visual preview failed; inspect the stored safe document error"
                assert snapshot["status"] != "ready", "Document was indexed without this probe's preview confirmation"
                if snapshot["status"] == "preview":
                    break
                time.sleep(1)
            else:
                raise TimeoutError("Visual preview did not complete within the bounded deadline")
            report["stage"] = "validate_preview"
            report["visual_text"] = assert_preview(snapshot, document_id)
            report["visual_flow_directions_verified"] = True
            report["preview_token"] = snapshot["preview_token"]
            report["preview_chunks"] = snapshot["chunk_count"]
            report["preview_page"] = settings.public_base_url + "/presales/preview?document_id=" + document_id
            assert snapshot["can_confirm"] is True
            with database.connect() as connection:
                count = connection.execute("SELECT count(*) AS count FROM kb002.chunks WHERE document_id=%s",
                                           (document_id,)).fetchone()["count"]
            assert count == 0, "Preview-only document already has searchable index chunks"
            report["indexed_before_confirmation"] = count
            report["assets"] = verify_assets(client, snapshot, document_id)
            cache = presales.data_dir / "assets" / document_id / "analysis-cache"
            cache_files = list(cache.glob("*.json"))
            assert len(cache_files) == 1, "One embedded image must produce one cached visual result"
            report["vision_evidence"] = {"result_count": 1, "cache_created": True,
                                        "network_request_count": None,
                                        "expected_requests_from_code_path": 1,
                                        "request_count_basis": "Single new image with no existing per-document cache; network count not instrumented"}
            report["stage"] = "confirm_current_snapshot"
            token = snapshot["preview_token"]
            response = client.post(f"/presales/api/documents/{document_id}/confirm", json={"preview_token": token})
            assert response.status_code == 200 and response.json()["preview_token"] == token
            report["confirmation_used_current_token"] = True
            report["stage"] = "waiting_for_index"
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                ready = complete_preview(client, document_id)
                if ready["status"] != report["states"][-1]:
                    report["states"].append(ready["status"])
                assert ready["status"] != "failed", "Confirmed Word indexing failed"
                if ready["status"] == "ready":
                    break
                time.sleep(1)
            else:
                raise TimeoutError("Confirmed Word did not become ready within deadline")
            assert ready["preview_token"] == token
            assert chunk_projection(ready["chunks"]) == chunk_projection(snapshot["chunks"]), "Indexed content differs from confirmed preview"
            report["confirmed_preview_matches_index"] = True
            with database.connect() as connection:
                rows = connection.execute("SELECT ordinal,text,kind,location,asset_name,embedding::text AS embedding FROM kb002.chunks WHERE document_id=%s ORDER BY ordinal",
                                          (document_id,)).fetchall()
            assert len(rows) == snapshot["chunk_count"]
            for ordinal, (row, preview) in enumerate(zip(rows, snapshot["chunks"])):
                vector = json.loads(row["embedding"])
                assert row["ordinal"] == ordinal and row["text"] == preview["text"] and row["kind"] == preview["kind"]
                assert len(vector) == presales.embedding_dimension and all(math.isfinite(value) for value in vector) and any(vector)
                expected_location = {key: value for key, value in preview["location"].items() if key != "preview_url"}
                assert row["location"] == expected_location
            report["vectors"] = {"chunks": len(rows), "dimension": presales.embedding_dimension,
                                 "all_finite_nonzero": True, "all_ordinals_and_locations_match_preview": True}
            report["stage"] = "http_retrieval"
            report["http_searches"] = {}
            for category, query in (("body", TEXT_QUERY), ("table", TABLE_QUERY), ("image", IMAGE_QUERY)):
                response = client.post("/presales/api/search", json={"query": query, "limit": 20})
                assert response.status_code == 200
                report["http_searches"][category] = find_evidence(response.json(), document_id, category)
            source = client.get(f"/presales/api/documents/{document_id}/source")
            assert source.status_code == 200 and source.content == content
            assert source.headers["content-type"].split(";", 1)[0] == DOCX_TYPE
            original_list = get_json(client, "/api/documents")["documents"]
            assert document_id not in {item["id"] for item in original_list}
            assert client.get(f"/api/documents/{document_id}/source").status_code == 404
            report["source_bytes_unchanged"] = True
            report["original_library_excludes_fixture"] = True
        report["stage"] = "mcp_retrieval"
        report["mcp"] = asyncio.run(verify_mcp(settings, document_id))
    finally:
        after = original_summary(settings)
        report["original_after"] = after
        report["original_unchanged"] = before == after
        assert before == after, "Original library changed during the probe; review concurrent activity"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.prepare_only:
        print(prepare_fixture())
        return 0
    report = {"ok": False, "date": "2026-11-19", "stage": "configuration"}
    started = time.monotonic()
    try:
        run_verification(report)
        report.update(ok=True, stage="complete")
    except Exception as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, AssertionError):
            report["assertion"] = str(error)
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("ok", "stage", "document_id", "preview_chunks",
                                                    "original_unchanged", "elapsed_seconds", "error_type", "assertion")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
