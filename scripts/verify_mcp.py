"""Verify a real stdio MCP session against the two local smoke-test documents."""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client import stdio
from kb.config import load_settings


ROOT = Path(__file__).resolve().parent.parent
SAMPLES = {"approval-flow.pdf": "pdf", "approval-guide.md": "markdown"}
TOOLS = {"get_document", "list_documents", "search_knowledge"}
QUERY = "主管驳回后由谁修改"


async def read_server_diagnostics(stream, report):
    # Save only event labels from stderr. Raw server output can include private paths.
    indicators = {
        "model_load": ("loading weights", "loading checkpoint", "load pretrained sentencetransformer"),
        "torch_warning": ("from torch", "pytorch", "torch warning"),
        "memory_error": ("out of memory", "not enough memory", "memoryerror", "paging file"),
        "module_error": ("importerror", "modulenotfounderror", "dll load failed"),
        "cache_error": ("permissionerror", "permission denied", "access is denied"),
        "connection_error": ("connectionerror", "connecttimeout", "connection refused"),
        "timeout": ("timeouterror", "readtimeout"), "traceback": ("traceback (most recent call last)",),
        "tokenizer_start": ("building prefix dict", "loading model from cache"),
        "tokenizer_ready": ("prefix dict has been built",),
    }
    try:
        async for block in stream:
            lower = block.decode("utf-8", errors="replace").lower()
            for label, terms in indicators.items():
                if label not in report["server_signals"] and any(term in lower for term in terms):
                    report["server_signals"].append(label)
                    print("MCP server: " + label, file=sys.stderr, flush=True)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        pass


def located_image(chunk, document_id, kind):
    location = chunk.get("location") or {}
    if chunk.get("kind") != "image" or chunk.get("document_id") != document_id:
        return False
    prefix = f"/api/documents/{document_id}"
    source = urlsplit(chunk.get("source_url") or "")
    asset = urlsplit(chunk.get("asset_url") or "")
    if source.path != prefix + "/source" or not asset.path.startswith(prefix + "/assets/"):
        return False
    if kind == "markdown":
        return location.get("line_start") == 7 and location.get("line_end") == 7
    preview = urlsplit(location.get("preview_url") or "")
    bbox = location.get("bbox")
    return (location.get("page") == 1 and source.fragment == "page=1"
            and preview.path.startswith(prefix + "/assets/") and isinstance(bbox, list)
            and len(bbox) == 4 and bbox[2] > bbox[0] and bbox[3] > bbox[1])


async def structured(session, name, arguments):
    result = await session.call_tool(name, arguments)
    if result.isError or not isinstance(result.structuredContent, dict):
        # Forward only a backend exception class, never model output or a tool's full message.
        text = next((part.text for part in result.content if part.type == "text"), "")
        match = re.search(r"错误类型 ([A-Za-z][A-Za-z0-9_]{0,80})", text)
        raise ToolFailure(match.group(1) if match else "MCP tool returned no structured result")
    return result.structuredContent


class ToolFailure(RuntimeError):
    pass


def safe_errors(error):
    if hasattr(error, "exceptions"):
        return [summary for cause in error.exceptions for summary in safe_errors(cause)]
    summary = {"type": type(error).__name__}
    code = getattr(getattr(error, "error", None), "code", None)
    if isinstance(code, int):
        summary["code"] = code
    if isinstance(error, ToolFailure):
        summary["detail"] = str(error)
    return [summary]


async def link_status(client, url, media_type):
    target = urlsplit(url)
    expected = urlsplit(load_settings().public_base_url)
    if (target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost"}
            or target.port != expected.port or target.username or target.password or target.query):
        raise ValueError("Expected a local source link on the configured Web port")
    async with client.stream("GET", url) as response:
        if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != media_type:
            raise RuntimeError("Source link returned an unexpected status or media type")
        return response.status_code


async def check_search(session, rows, report):
    report["stage"] = "search_knowledge"
    print("MCP search_knowledge: started", file=sys.stderr, flush=True)
    response = await structured(session, "search_knowledge", {"query": QUERY, "limit": 20})
    results = response["results"]
    report["search_results"] = len(results)
    report["stage"] = "source_http"
    ready = {row["id"]: SAMPLES[row["filename"]] for row in rows
             if row["status"] == "ready" and row["filename"] in SAMPLES}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        for rank, hit in ((rank, source) for rank, group in enumerate(results, 1)
                          for source in group.get("sources", [group])):
            kind = ready.get(hit["document_id"])
            if not kind or kind in report["search_sources"] or not located_image(hit, hit["document_id"], kind):
                continue
            media_type = "application/pdf" if kind == "pdf" else "text/plain"
            asset_status = await link_status(client, hit["asset_url"], "image/png")
            source_status = await link_status(client, hit["source_url"], media_type)
            fields = ("page", "bbox") if kind == "pdf" else ("line_start", "line_end")
            report["search_sources"][kind] = {
                "rank": rank, "channels": hit["channels"], "filename": hit["filename"],
                "location": {key: hit["location"][key] for key in fields},
                "asset_url": hit["asset_url"], "asset_http": asset_status,
                "source_url": hit["source_url"], "source_http": source_status,
            }


async def verification(report):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-X", "utf8", "-m", "kb.mcp_server"], cwd=ROOT,
    )
    process = None
    diagnostic_task = None

    async def start_in_foreground(command, args, env, errlog, cwd):
        # The pinned Windows SDK normally requests a hidden console. Keep this
        # one probe attached to its foreground session and retain its OS handle.
        nonlocal process, diagnostic_task
        process = await anyio.open_process([command, *args], env=env, stderr=subprocess.PIPE, cwd=cwd)
        print(f"MCP subprocess PID {process.pid}", file=sys.stderr, flush=True)
        diagnostic_task = asyncio.create_task(read_server_diagnostics(process.stderr, report))
        return process

    with patch.object(stdio, "_create_platform_compatible_process", start_in_foreground):
        with open(os.devnull, "w") as diagnostics:
            async with stdio.stdio_client(parameters, errlog=diagnostics) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=180)) as session:
                    report["stage"] = "initialize"
                    await session.initialize()
                    report["stage"] = "list_tools"
                    inventory = await session.list_tools()
                    report["tools"] = sorted(tool.name for tool in inventory.tools)
                    report["stage"] = "list_documents"
                    listed = await structured(session, "list_documents", {})
                    rows = listed["documents"]
                    report["listed_documents"] = len(rows)
                    report["stage"] = "get_document"
                    for name, kind in SAMPLES.items():
                        row = next((row for row in rows if row["filename"] == name and row["status"] == "ready"), None)
                        if row:
                            detail = await structured(session, "get_document", {"document_id": row["id"], "limit": 50})
                            report["source_locations"][kind] = any(
                                located_image(chunk, row["id"], kind) for chunk in detail["chunks"]
                            )
                    await check_search(session, rows, report)
                    report["stage"] = "session_shutdown"
    # The SDK closes stdin and waits on this exact child on context exit.
    if diagnostic_task is not None:
        await diagnostic_task
    return (process is not None and process.returncode == 0
            and set(report["tools"]) == TOOLS and all(report["source_locations"].values())
            and set(report["search_sources"]) == set(SAMPLES.values()))


def main():
    report = {"tools": [], "listed_documents": 0, "source_locations": {"pdf": False, "markdown": False},
              "search_results": 0, "search_sources": {}, "stage": "spawn", "server_signals": []}
    start = time.monotonic()
    try:
        ok = asyncio.run(verification(report))
    except Exception as error:
        # Driver and protocol messages can contain file contents or connection details.
        report["errors"] = safe_errors(error)
        ok = False
    report["elapsed_seconds"] = round(time.monotonic() - start, 1)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
