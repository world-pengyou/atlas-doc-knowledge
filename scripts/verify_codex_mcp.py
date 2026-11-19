"""Verify the project MCP through Codex's native app-server client."""
import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parent.parent


class CodexClient:
    def __enter__(self):
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("Codex CLI 不在当前路径中")
        self.diagnostics = (ROOT / "data" / "codex-mcp-native.stderr.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [executable, "app-server", "--stdio"], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.diagnostics,
            text=True, encoding="utf-8",
        )
        self.messages = queue.Queue()
        self.identifier = 0
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method, parameters=None, timeout=180):
        self.identifier += 1
        identifier = self.identifier
        self.send({"id": identifier, "method": method,
                   **({"params": parameters} if parameters is not None else {})})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, min(2, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("Codex app-server 已退出")
            if message.get("id") == identifier:
                if "error" in message:
                    error = message["error"]
                    raise RuntimeError(f"Codex {method} 请求失败，code={error.get('code')}，{error.get('message', '')[:300]}")
                return message["result"]
            if "id" in message and "method" in message:
                raise RuntimeError("Codex 请求额外用户输入，方法为 " + message["method"])
        raise TimeoutError("Codex 请求超时，方法为 " + method)

    def __exit__(self, *_):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.diagnostics.close()


def main():
    report = {"client": "Codex app-server", "server": "knowledge_002", "tool": "search_knowledge", "ok": False}
    with CodexClient() as client:
        client.request("initialize", {"clientInfo": {"name": "knowledge_mcp_check", "version": "0.1.0"},
                                      "capabilities": {"experimentalApi": True}})
        client.send({"method": "initialized"})
        started = client.request("thread/start", {"cwd": str(ROOT), "ephemeral": True})
        thread_id = started["thread"]["id"]
        print("Codex 原生只读验收连接已建立", flush=True)
        inventory = client.request("mcpServerStatus/list", {"threadId": thread_id,
                                   "serverName": "knowledge_002", "detail": "toolsAndAuthOnly"})
        (ROOT / "data" / "codex-mcp-inventory.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        result = client.request("mcpServer/tool/call", {"threadId": thread_id, "server": "knowledge_002",
                                "tool": "search_knowledge", "arguments": {"query": "主管驳回后由谁修改", "limit": 20}})
        if result.get("isError"):
            raise RuntimeError("Codex MCP 检索返回工具错误")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            raise RuntimeError("Codex MCP 未返回结构化结果")
        report["query"] = payload["query"]
        report["result_count"] = len(payload["results"])
        report["deduplicated_count"] = payload.get("deduplicated_count", 0)
        print(f"Codex 原生 MCP 检索已返回 {report['result_count']} 条结果", flush=True)
        report["sources"] = []
        with httpx.Client(timeout=15, trust_env=False) as web:
            for filename in ("approval-flow.pdf", "approval-guide.md"):
                hit = next((row for group in payload["results"] for row in group.get("sources", [group])
                            if row["filename"] == filename and row["kind"] == "image"), None)
                if hit is None:
                    raise RuntimeError("实际检索缺少合成样本图片 " + filename)
                entry = {key: hit[key] for key in ("document_id", "filename", "asset_url", "source_url", "location", "channels")}
                for key in ("asset_url", "source_url"):
                    response = web.get(entry[key])
                    response.raise_for_status()
                    entry[key + "_http"] = response.status_code
                report["sources"].append(entry)
    report["ok"] = True
    (ROOT / "docs" / "codex-mcp-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
