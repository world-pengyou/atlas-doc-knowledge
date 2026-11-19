"""Publish one named synthetic text fixture and verify real adjacent retrieval.

Uses the running local Web/HTTP MCP and local embeddings. No visual or remote
model calls. Repeated runs reuse the fixture's existing content hash.
"""
import json
import time
from pathlib import Path

import httpx

from kb.config import ROOT, load_settings
from scripts.verify_codex_mcp import CodexClient


TAG = "DEDUPREAL20261119"


def verify_hit(payload, document_id, paragraph):
    matches = [hit for hit in payload["results"]
               if any(source["document_id"] == document_id for source in hit["sources"])]
    assert len(matches) == 1, "相同测试正文应只占一个返回名额"
    hit = matches[0]
    assert hit["text"] == paragraph, "合并正文应与完整原始段落一致"
    assert hit["merged_chunk_count"] == 4, "两份重复段落的四个相邻切片应合为一组"
    assert len(hit["sources"]) == 4
    assert {source["ordinal"] for source in hit["sources"]} == {1, 2, 3, 4}
    assert {source["location"]["line_start"] for source in hit["sources"]} == {3, 5}
    assert all(source["document_id"] == document_id for source in hit["sources"])
    assert all(source["asset_url"] is None for source in hit["sources"])
    return hit


def main():
    settings = load_settings()
    assert settings.embedding_backend == "local", "本探针仅使用本地向量模型"
    paragraph = "".join(
        f"记录{i:02d}：主管驳回后由申请人修改，修改后重新提交主管审核；"
        f"财务仅在审批通过后核对付款，合成验证编号为{TAG}。"
        for i in range(1, 13)
    )
    assert 700 < len(paragraph) <= 1300
    body = f"# 相邻与重复切片合成验证\n\n{paragraph}\n\n{paragraph}\n"
    fixture = ROOT / "data" / "smoke-inputs" / "去重实测-20261119.md"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if fixture.exists():
        assert fixture.read_text(encoding="utf-8") == body, "已有测试文件内容不同，停止覆盖"
    else:
        fixture.write_text(body, encoding="utf-8")
    with httpx.Client(base_url=settings.public_base_url, timeout=150, trust_env=False) as client:
        client.get("/health").raise_for_status()
        with fixture.open("rb") as stream:
            response = client.post("/api/documents", files={"files": (fixture.name, stream, "text/markdown")})
        response.raise_for_status()
        document_id = response.json()["documents"][0]["id"]
        print("合成测试文档已登记 " + document_id, flush=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get("/api/documents/" + document_id)
            response.raise_for_status()
            status = response.json()["status"]
            if status == "failed":
                raise RuntimeError("合成测试文档入库失败，请查看该文档状态")
            if status == "preview":
                preview = client.get("/api/documents/" + document_id + "/preview")
                preview.raise_for_status()
                snapshot = preview.json()
                assert snapshot["image_count"] == 0 and snapshot["chunk_count"] == 5
                confirmed = client.post("/api/documents/" + document_id + "/confirm",
                                        json={"preview_token": snapshot["preview_token"]})
                confirmed.raise_for_status()
                print("已核对5个纯文本切片并确认合成样本入库", flush=True)
            if status == "ready":
                break
            time.sleep(1)
        else:
            raise TimeoutError("合成测试文档入库等待超时")
        original = client.get("/api/documents/" + document_id).json()["chunks"]
        response = client.post("/api/search", json={"query": TAG, "limit": 20})
        response.raise_for_status()
        payload = response.json()
        hit = verify_hit(payload, document_id, paragraph)
        client.get(hit["source_url"]).raise_for_status()
        after = client.get("/api/documents/" + document_id).json()["chunks"]
        assert original == after, "检索不得修改原始索引切片"
    print("真实 HTTP 相邻拼接与重复去重通过", flush=True)
    with CodexClient() as codex:
        codex.request("initialize", {"clientInfo": {"name": "dedup_check", "version": "0.1.0"},
                                     "capabilities": {"experimentalApi": True}})
        codex.send({"method": "initialized"})
        thread_id = codex.request("thread/start", {"cwd": str(ROOT), "ephemeral": True})["thread"]["id"]
        result = codex.request("mcpServer/tool/call", {"threadId": thread_id, "server": "knowledge_002",
                              "tool": "search_knowledge", "arguments": {"query": TAG, "limit": 20}})
        assert not result.get("isError"), "Codex MCP 检索应成功"
        verify_hit(result["structuredContent"], document_id, paragraph)
    report = {"ok": True, "date": "2026-11-19", "document_id": document_id,
              "filename": fixture.name, "query": TAG, "indexed_chunks": len(original),
              "paragraph_length": len(paragraph), "merged_chunk_count": hit["merged_chunk_count"],
              "http": "passed", "codex_mcp": "passed", "original_chunks_unchanged": True,
              "result": hit}
    (ROOT / "data" / "search-dedup-adjacent-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "result"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
