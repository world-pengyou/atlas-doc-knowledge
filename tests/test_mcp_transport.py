"""Verify MCP transport setup with isolated dependencies."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import HTTPConnection

from kb import mcp_server


class McpTransportTests(unittest.TestCase):
    def service(self, backend="http"):
        return SimpleNamespace(
            settings=SimpleNamespace(embedding_backend=backend),
            database=MagicMock(), search=MagicMock(), document_detail=MagicMock(),
        )

    def test_injected_service_reuses_dependencies_and_exposes_http_settings(self):
        service = self.service()
        with patch.object(mcp_server, "load_settings") as load, patch.object(mcp_server, "Database") as database:
            server = mcp_server.create_server(service, streamable_http_path="/", port=8102)
        load.assert_not_called()
        database.assert_not_called()
        service.database.check.assert_not_called()
        self.assertEqual(server.settings.host, "127.0.0.1")
        self.assertEqual(server.settings.port, 8102)
        self.assertEqual(server.settings.streamable_http_path, "/")
        self.assertTrue(server.settings.stateless_http)
        self.assertTrue(server.settings.json_response)
        security = server.settings.transport_security
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertEqual(set(security.allowed_hosts), {"127.0.0.1:8102", "localhost:8102", "[::1]:8102"})
        self.assertEqual(set(security.allowed_origins), {
            "http://127.0.0.1:8102", "http://localhost:8102", "http://[::1]:8102",
        })
        self.assertIsNotNone(server.streamable_http_app())

    def test_lan_authorities_use_the_embedded_port_and_preserve_local_codex(self):
        service = self.service()
        service.settings.web_allowed_hosts = ("127.0.0.1", "localhost", "::1", "192.0.2.10")
        service.settings.public_base_url = "http://192.0.2.10:8100"
        server = mcp_server.create_server(service, port=8100)
        self.assertEqual(server.settings.host, "127.0.0.1")
        security = server.settings.transport_security
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertEqual(set(security.allowed_hosts), {
            "127.0.0.1:8100", "localhost:8100", "[::1]:8100", "192.0.2.10:8100",
        })
        self.assertEqual(set(security.allowed_origins), {
            "http://127.0.0.1:8100", "http://localhost:8100", "http://[::1]:8100", "http://192.0.2.10:8100",
        })
        self.assertFalse(any("*" in value for value in security.allowed_hosts + security.allowed_origins))

    def test_default_http_and_https_ports_accept_canonical_authorities(self):
        for scheme, port in (("http", 80), ("https", 443)):
            with self.subTest(scheme=scheme):
                service = self.service()
                service.settings.web_allowed_hosts = ("knowledge.test", "::1")
                service.settings.public_base_url = scheme + "://knowledge.test"
                security = mcp_server.create_server(service, port=port).settings.transport_security
                self.assertEqual(set(security.allowed_hosts), {
                    "knowledge.test", f"knowledge.test:{port}", "[::1]", f"[::1]:{port}",
                })
                self.assertEqual(set(security.allowed_origins), {
                    scheme + "://knowledge.test", f"{scheme}://knowledge.test:{port}",
                    scheme + "://[::1]", f"{scheme}://[::1]:{port}",
                })

    def test_standalone_port_does_not_inherit_a_different_public_port(self):
        service = self.service()
        service.settings.public_base_url = "https://knowledge.test"
        security = mcp_server.create_server(service, port=8101).settings.transport_security
        self.assertEqual(set(security.allowed_hosts), {"127.0.0.1:8101", "localhost:8101", "[::1]:8101"})
        self.assertTrue(all(origin.startswith("http://") and origin.endswith(":8101")
                            for origin in security.allowed_origins))

    def test_standalone_factory_initializes_existing_dependencies(self):
        settings = SimpleNamespace(embedding_backend="http")
        database = MagicMock()
        service = self.service()
        with patch.object(mcp_server, "load_settings", return_value=settings), \
                patch.object(mcp_server, "Database", return_value=database) as factory, \
                patch.object(mcp_server, "KnowledgeService", return_value=service) as knowledge:
            server = mcp_server.create_server()
        factory.assert_called_once_with(settings)
        database.check.assert_called_once()
        knowledge.assert_called_once_with(settings, database)
        self.assertEqual(server.settings.streamable_http_path, "/mcp")

    def test_local_runtime_preload_can_be_disabled_for_embedded_service(self):
        service = self.service("local")
        with patch.object(mcp_server, "import_module") as preload:
            mcp_server.create_server(service)
        preload.assert_called_once_with("sentence_transformers")
        with patch.object(mcp_server, "import_module") as preload:
            mcp_server.create_server(service, preload_local_runtime=False)
        preload.assert_not_called()

    def test_cli_defaults_to_stdio(self):
        with patch.object(mcp_server, "create_server") as factory:
            mcp_server.main([])
        factory.assert_called_once_with(port=8101)
        factory.return_value.run.assert_called_once_with(transport="stdio")

    def test_cli_selects_streamable_http_and_port(self):
        with patch.object(mcp_server, "create_server") as factory:
            mcp_server.main(["--transport", "streamable-http", "--port", "8102"])
        factory.assert_called_once_with(port=8102)
        factory.return_value.run.assert_called_once_with(transport="streamable-http")

    def test_cli_rejects_invalid_transport_and_port(self):
        for arguments in (["--transport", "sse"], ["--port", "0"], ["--port", "65536"]):
            with self.subTest(arguments=arguments), patch.object(mcp_server, "create_server") as factory:
                with patch("sys.stderr"), self.assertRaises(SystemExit) as failure:
                    mcp_server.main(arguments)
                self.assertEqual(failure.exception.code, 2)
                factory.assert_not_called()


class InjectedServiceToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_security_rejects_rebinding_foreign_origins_and_wrong_ports(self):
        service = SimpleNamespace(
            settings=SimpleNamespace(
                embedding_backend="http", web_allowed_hosts=("127.0.0.1", "localhost", "::1", "192.0.2.10"),
                public_base_url="http://192.0.2.10:8100",
            ), database=MagicMock(),
        )
        security = mcp_server.create_server(service, port=8100).settings.transport_security
        middleware = TransportSecurityMiddleware(security)
        cases = [
            ("127.0.0.1:8100", None, None),
            ("192.0.2.10:8100", "http://192.0.2.10:8100", None),
            ("[::1]:8100", "http://[::1]:8100", None),
            ("attacker.test:8100", None, 421),
            ("192.0.2.10:8101", None, 421),
            ("192.0.2.10:8100", "http://attacker.test:8100", 403),
            ("192.0.2.10:8100", "http://192.0.2.10.attacker.test:8100", 403),
            ("192.0.2.10:8100", "http://192.0.2.10:8101", 403),
            ("192.0.2.10:8100", "null", 403),
        ]
        for host, origin, expected in cases:
            with self.subTest(host=host, origin=origin):
                headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
                if origin is not None:
                    headers.append((b"origin", origin.encode()))
                request = HTTPConnection({"type": "http", "headers": headers})
                response = await middleware.validate_request(request, is_post=True)
                self.assertEqual(response.status_code if response else None, expected)

    async def test_tools_use_the_injected_service_and_database(self):
        database = MagicMock()
        database.list_documents.return_value = [{"filename": "synthetic.md"}]
        service = SimpleNamespace(
            settings=SimpleNamespace(embedding_backend="http"), database=database,
            search=MagicMock(return_value={"query": "合成查询", "results": []}),
            document_detail=MagicMock(return_value={"chunks": []}),
        )
        server = mcp_server.create_server(service)
        document_id = uuid4()
        await server.call_tool("search_knowledge", {"query": "合成查询", "limit": 3})
        await server.call_tool("list_documents", {})
        await server.call_tool("get_document", {"document_id": str(document_id), "offset": 2, "limit": 4})
        service.search.assert_called_once_with("合成查询", 3)
        database.list_documents.assert_called_once_with()
        service.document_detail.assert_called_once_with(document_id, 2, 4)


if __name__ == "__main__":
    unittest.main()
