"""Exercise the real running Web service, PostgreSQL and configured models."""
import argparse
import json
import time
from pathlib import Path

import fitz
import httpx


ROOT = Path(__file__).resolve().parent.parent


def create_samples():
    directory = ROOT / "data" / "smoke-inputs"
    directory.mkdir(parents=True, exist_ok=True)
    pdf_path = directory / "approval-flow.pdf"
    with fitz.open() as document:
        page = document.new_page(width=600, height=500)
        page.insert_text((45, 40), "Synthetic approval manual / FLOW-002", fontsize=18)
        boxes = [(fitz.Rect(50, 100, 190, 160), "Applicant submits"),
                 (fitz.Rect(340, 100, 480, 160), "Manager reviews"),
                 (fitz.Rect(340, 300, 480, 360), "Applicant revises")]
        for rectangle, text in boxes:
            page.draw_rect(rectangle, color=(0.1, 0.3, 0.6), fill=(0.9, 0.95, 1))
            page.insert_textbox(rectangle + (8, 20, -4, -4), text, fontsize=12)
        # The reject branch is drawn as vectors, without an embedded image.
        page.draw_line((190, 130), (340, 130))
        page.draw_polyline([(330, 125), (340, 130), (330, 135)])
        page.draw_line((410, 160), (410, 300))
        page.draw_polyline([(405, 290), (410, 300), (415, 290)])
        page.insert_text((420, 230), "Reject", fontsize=12)
        page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(str(directory / "flow.png"))
        document.save(pdf_path)
    markdown = directory / "approval-guide.md"
    markdown.write_text(
        "# 测试审批说明\n\n资料编号 MD-FLOW-003。\n\n"
        "## 驳回处理\n\n![审批退回流程](flow.png)\n\n"
        "申请人提交后进入主管审核。主管驳回时，返回申请人修改。\n",
        encoding="utf-8",
    )
    return pdf_path, markdown, directory / "flow.png"


def run(base_url, timeout):
    files = create_samples()
    with httpx.Client(base_url=base_url, timeout=130) as client:
        client.get("/health").raise_for_status()
        handles = [path.open("rb") for path in files]
        try:
            response = client.post("/api/documents", files=[
                ("files", (path.name, handle)) for path, handle in zip(files, handles)
            ])
            response.raise_for_status()
            documents = response.json()["documents"]
        finally:
            for handle in handles:
                handle.close()
        expected = {document["id"] for document in documents}
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            response = client.get("/api/documents")
            response.raise_for_status()
            rows = [row for row in response.json()["documents"] if row["id"] in expected]
            status = {row["filename"]: row["status"] for row in rows}
            if status != previous:
                print(json.dumps(status, ensure_ascii=False), flush=True)
                previous = status
            failed = [row for row in rows if row["status"] == "failed"]
            if failed:
                raise RuntimeError("入库失败 " + json.dumps(failed, ensure_ascii=False))
            for row in rows:
                if row["status"] == "preview":
                    response = client.get(f"/api/documents/{row['id']}/preview")
                    response.raise_for_status()
                    preview = response.json()
                    assert preview["can_confirm"] and preview["chunks"], "切片预览未准备完成"
                    response = client.post(f"/api/documents/{row['id']}/confirm",
                                           json={"preview_token": preview["preview_token"]})
                    response.raise_for_status()
                    print("已验证切片预览并确认入库 " + row["filename"], flush=True)
            if len(rows) == len(expected) and all(row["status"] == "ready" for row in rows):
                break
            time.sleep(2)
        else:
            raise TimeoutError("等待入库超时，请查看前端任务状态")
        for document in documents:
            response = client.get(f"/api/documents/{document['id']}", params={"limit": 50})
            response.raise_for_status()
            chunks = response.json()["chunks"]
            image_chunks = [chunk for chunk in chunks if chunk["kind"] == "image"]
            if not image_chunks:
                raise AssertionError("文档缺少图片模型解析结果")
            image = image_chunks[0]
            client.get(image["asset_url"]).raise_for_status()
            client.get(image["source_url"].split("#")[0]).raise_for_status()
            if document["filename"].endswith(".pdf"):
                assert image["location"]["page"] == 1
                assert len(image["location"]["bbox"]) == 4
            else:
                assert image["location"]["line_start"] == 7
        for query in ("FLOW-002", "MD-FLOW-003", "主管驳回后由谁修改", "Reject Applicant revises"):
            response = client.post("/api/search", json={"query": query, "limit": 20})
            response.raise_for_status()
            hits = response.json()["results"]
            assert hits, f"检索没有结果 {query}"
            sources = [source for hit in hits for source in hit.get("sources", [hit])]
            assert any(hit["document_id"] in expected for hit in sources), f"未命中测试文档 {query}"
            if query in {"FLOW-002", "MD-FLOW-003"}:
                assert any("lexical" in hit["channels"] for hit in sources), "全文检索未参与粗排"
            print(f"检索通过 {query}，返回 {len(hits)} 条", flush=True)
    print("真实端到端检查通过，请在前端复核流程图箭头与来源位置", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=600)
    arguments = parser.parse_args()
    run(arguments.base_url, arguments.timeout)
