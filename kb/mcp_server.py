"""Read-only MCP tools over stdio or local Streamable HTTP."""
import argparse
import logging
import asyncio
from importlib import import_module
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from kb.config import library_settings, load_settings
from kb.database import Database
from kb.models import ModelError
from kb.service import KnowledgeService


def _transport_security(settings, port):
    """Keep MCP's Host/Origin checks when the Web app accepts LAN requests."""
    hosts = getattr(settings, "web_allowed_hosts", ("127.0.0.1", "localhost", "::1"))
    public_url = urlsplit(getattr(settings, "public_base_url", "http://127.0.0.1"))
    public_port = public_url.port or (443 if public_url.scheme == "https" else 80)
    scheme = public_url.scheme if public_port == port and public_url.scheme in {"http", "https"} else "http"
    authorities, origins = [], []
    for host in hosts:
        # Settings contains hostnames, not authorities; IPv6 needs brackets on
        # the wire. No wildcard ports or wildcard hosts are added here.
        authority = f"[{host}]" if ":" in host else host
        authorities.append(f"{authority}:{port}")
        origins.append(f"{scheme}://{authority}:{port}")
        if (scheme, port) in {("http", 80), ("https", 443)}:
            authorities.append(authority)
            origins.append(f"{scheme}://{authority}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(authorities)),
        allowed_origins=list(dict.fromkeys(origins)),
    )


def create_server(service=None, *, streamable_http_path="/mcp", preload_local_runtime=True, port=8101,
                  library="default"):
    if service is None:
        settings = library_settings(load_settings(), library)
        database = Database(settings)
        database.check()
        service = KnowledgeService(settings, database)
    else:
        settings = service.settings
        database = service.database
    if preload_local_runtime and getattr(settings, "embedding_backend", "http") == "local":
        # On Windows, initialize numerical extensions before stdio starts its
        # blocking reader thread. Model weights are still loaded on first use.
        import_module("sentence_transformers")
    server = FastMCP(
        "售前图文知识库" if getattr(settings, "public_base_url", "").endswith("/presales") else "售后图文知识库",
        host="127.0.0.1", port=port,
        streamable_http_path=streamable_http_path, stateless_http=True, json_response=True,
        transport_security=_transport_security(settings, port),
    )

    @server.tool()
    async def search_knowledge(query: str, limit: int | None = None) -> dict[str, Any]:
        """混合粗排、余弦过滤及切片去重，返回正文、相似度和全部 sources 原图/位置。省略 limit 使用配置数量。"""
        try:
            return await asyncio.to_thread(service.search, query, limit)
        except (ValueError, ModelError) as error:
            raise ValueError(str(error)) from None
        except Exception as error:
            logging.error("MCP search failed (%s)", type(error).__name__)
            raise ValueError("知识库查询失败，请检查数据库和模型服务") from None

    @server.tool()
    async def generate_report(query: str, style: str = "report",
                              top_k: int = 8) -> dict[str, Any]:
        """先混合检索再整合成报告或摘要；style 可选 report、summary、qa，返回正文、引用来源与生成方式。"""
        try:
            return await asyncio.to_thread(service.report, query, style, top_k)
        except (ValueError, ModelError) as error:
            raise ValueError(str(error)) from None
        except Exception as error:
            logging.error("MCP report failed (%s)", type(error).__name__)
            raise ValueError("报告生成失败，请检查数据库和模型服务") from None

    @server.tool()
    def list_documents() -> dict[str, Any]:
        """列出最近 200 份资料及入库状态。"""
        try:
            return {"documents": database.list_documents()}
        except Exception:
            raise ValueError("读取文档列表失败，请检查数据库") from None

    @server.tool()
    def get_document(document_id: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """分页读取某文档的证据片段，最多 50 条，含原件和原图链接。"""
        try:
            return service.document_detail(UUID(document_id), offset, limit)
        except ValueError as error:
            raise ValueError(str(error)) from None
        except Exception:
            raise ValueError("读取文档详情失败，请检查数据库") from None

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="文档知识库 MCP 服务（售后/售前）")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--library", choices=("default", "presales"), default="default")
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    options = {"port": arguments.port}
    if arguments.library != "default":
        options["library"] = arguments.library
    create_server(**options).run(transport=arguments.transport)


if __name__ == "__main__":
    main()
