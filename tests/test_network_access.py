"""Exercise LAN configuration and HTTP boundaries without sockets or services."""
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from starlette.responses import Response

from kb.__main__ import main
from kb.app import local_origin
from kb.config import load_settings


MISSING = object()


class NetworkConfigTests(unittest.TestCase):
    def load(self, **changes):
        environment = {
            "DATABASE_URL": "postgresql://example:synthetic@127.0.0.1/network_fixture",
            "EMBEDDING_BACKEND": "http",
            "EMBEDDING_BASE_URL": "http://127.0.0.1:9000/v1",
        }
        environment.update(changes)
        with patch.dict("os.environ", environment, clear=True), patch("kb.config.load_dotenv"):
            return load_settings()

    def lan(self, **changes):
        environment = {
            "WEB_HOST": "0.0.0.0",
            "WEB_ALLOWED_HOSTS": "127.0.0.1,localhost,::1,192.168.50.7",
            "WEB_ALLOWED_NETWORKS": "127.0.0.0/8,::1/128,192.168.50.0/24",
            "PUBLIC_BASE_URL": "http://192.168.50.7:8100",
        }
        environment.update(changes)
        return self.load(**environment)

    def test_defaults_only_bind_and_allow_loopback(self):
        settings = self.load()
        self.assertEqual(settings.web_host, "127.0.0.1")
        self.assertEqual(settings.web_allowed_hosts, ("127.0.0.1", "localhost", "::1"))
        self.assertEqual(settings.web_allowed_networks, ("127.0.0.0/8", "::1/128"))

    def test_explicit_lan_settings_normalize_hosts_networks_and_url(self):
        settings = self.lan(WEB_ALLOWED_HOSTS=" LOCALHOST, 192.168.50.7, ::1 ",
                            WEB_ALLOWED_NETWORKS="127.0.0.1/8,192.168.50.77/24,::1/128",
                            PUBLIC_BASE_URL="http://192.168.50.7:8100/")
        self.assertEqual(settings.web_host, "0.0.0.0")
        self.assertEqual(settings.web_allowed_hosts, ("localhost", "192.168.50.7", "::1"))
        self.assertEqual(settings.web_allowed_networks, ("127.0.0.0/8", "192.168.50.0/24", "::1/128"))
        self.assertEqual(settings.public_base_url, "http://192.168.50.7:8100")

    def test_bind_accepts_only_ip_literals_or_localhost(self):
        for value in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(value=value):
                self.assertEqual(self.load(WEB_HOST=value).web_host, value)
        for value in ("example.test", "http://0.0.0.0", "0.0.0.0:8100", "[::1]", "*", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "WEB_HOST"):
                self.load(WEB_HOST=value)

    def test_allowed_hosts_reject_wildcards_ports_paths_and_malformed_names(self):
        for value in ("*", "localhost,*", "http://localhost", "localhost/path", "127.0.0.1:8100",
                      "[::1]", "localhost?x=1", "user@localhost", "bad host", "bad..host", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "WEB_ALLOWED_HOSTS"):
                self.load(WEB_ALLOWED_HOSTS=value)

    def test_networks_reject_global_catchall_public_and_invalid_ranges(self):
        for value in ("0.0.0.0/0", "::/0", "8.8.8.0/24", "100.64.0.0/10",
                      "192.168.50.0/33", "synthetic-invalid", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "WEB_ALLOWED_NETWORKS") as caught:
                self.load(WEB_ALLOWED_NETWORKS=value)
            self.assertNotIn("synthetic-invalid", str(caught.exception))

    def test_nonloopback_binding_requires_explicit_lan_network(self):
        for binding in ("0.0.0.0", "::", "192.168.50.7"):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                self.load(WEB_HOST=binding, PUBLIC_BASE_URL="http://192.168.50.7:8100",
                          WEB_ALLOWED_HOSTS="192.168.50.7")
        with self.assertRaises(ValueError):
            self.lan(WEB_ALLOWED_NETWORKS="127.0.0.0/8,::1/128")

    def test_lan_public_url_must_be_allowlisted_private_ip_within_network(self):
        cases = [
            {"PUBLIC_BASE_URL": "http://127.0.0.1:8100"},
            {"PUBLIC_BASE_URL": "http://192.168.50.8:8100"},
            {"PUBLIC_BASE_URL": "http://192.168.51.7:8100", "WEB_ALLOWED_HOSTS": "192.168.51.7"},
            {"PUBLIC_BASE_URL": "http://8.8.8.8:8100", "WEB_ALLOWED_HOSTS": "8.8.8.8"},
            {"PUBLIC_BASE_URL": "http://knowledge.lan:8100", "WEB_ALLOWED_HOSTS": "knowledge.lan"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.lan(**changes)

    def test_lan_ipv6_binding_and_public_url_are_supported(self):
        settings = self.lan(WEB_HOST="::", WEB_ALLOWED_HOSTS="::1,fd12:3456::7",
                            WEB_ALLOWED_NETWORKS="::1/128,fd12:3456::7/64",
                            PUBLIC_BASE_URL="http://[fd12:3456::7]:8100")
        self.assertEqual(settings.web_host, "::")
        self.assertEqual(settings.web_allowed_networks, ("::1/128", "fd12:3456::/64"))

    def test_public_url_rejects_invalid_ports_paths_and_credentials_without_echo(self):
        values = ["http://127.0.0.1:notaport", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                  "http://127.0.0.1:-1", "http://127.0.0.1:8100/app", "http://127.0.0.1:8100//",
                  "http://127.0.0.1:8100?token=synthetic-secret-never-echo",
                  "http://user:synthetic-secret-never-echo@127.0.0.1:8100",
                  "http://[broken-ipv6", "ftp://127.0.0.1:8100"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                self.load(PUBLIC_BASE_URL=value)
            self.assertNotIn("synthetic-secret-never-echo", str(caught.exception))
            self.assertNotIn("broken-ipv6", str(caught.exception))

    def test_network_changes_do_not_change_embedding_identity(self):
        self.assertEqual(self.load().embedding_fingerprint, self.lan().embedding_fingerprint)

    def test_cli_uses_config_host_and_disables_proxy_headers(self):
        settings = self.lan()
        with patch.object(sys, "argv", ["kb", "serve"]), \
                patch("kb.config.load_settings", return_value=settings), \
                patch("uvicorn.run") as run:
            main()
        self.assertEqual(run.call_args.args, ("kb.app:app",))
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["port"], 8100)
        self.assertIs(run.call_args.kwargs["proxy_headers"], False)

    def test_cli_host_override_is_validated_and_does_not_change_environment(self):
        settings = self.lan()
        environment = {"NETWORK_TEST_SENTINEL": "unchanged"}
        with patch.dict("os.environ", environment, clear=True), \
                patch.object(sys, "argv", ["kb", "serve", "--host", "192.168.50.7", "--port", "8100"]), \
                patch("kb.config.load_settings", return_value=settings), \
                patch("uvicorn.run") as run:
            main()
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(run.call_args.kwargs["host"], "192.168.50.7")
        self.assertEqual(run.call_args.kwargs["port"], 8100)
        self.assertIs(run.call_args.kwargs["proxy_headers"], False)

    def test_cli_uses_effective_standard_port_when_public_url_has_no_explicit_port(self):
        for scheme, expected in (("http", 80), ("https", 443)):
            with self.subTest(scheme=scheme):
                settings = self.load(PUBLIC_BASE_URL=f"{scheme}://127.0.0.1")
                with patch.object(sys, "argv", ["kb", "serve"]), \
                        patch("kb.config.load_settings", return_value=settings), \
                        patch("uvicorn.run") as run:
                    main()
                self.assertEqual(run.call_args.kwargs["port"], expected)
                self.assertIs(run.call_args.kwargs["proxy_headers"], False)

    def test_cli_rejects_explicit_port_that_disagrees_with_public_url(self):
        for public_url, port in (("http://192.168.50.7:8100", "8200"),
                                 ("http://192.168.50.7", "8100"),
                                 ("https://192.168.50.7", "80")):
            with self.subTest(public_url=public_url, port=port):
                settings = self.lan(PUBLIC_BASE_URL=public_url)
                with patch.object(sys, "argv", ["kb", "serve", "--port", port]), \
                        patch("kb.config.load_settings", return_value=settings), \
                        patch("uvicorn.run") as run, patch("sys.stderr"):
                    with self.assertRaises(SystemExit) as caught:
                        main()
                self.assertEqual(caught.exception.code, 2)
                run.assert_not_called()

    def test_cli_cannot_override_local_configuration_to_unrestricted_binding(self):
        settings = self.load()
        with patch.object(sys, "argv", ["kb", "serve", "--host", "0.0.0.0"]), \
                patch("kb.config.load_settings", return_value=settings), \
                patch("uvicorn.run") as run, patch("sys.stderr"):
            with self.assertRaises((SystemExit, ValueError)):
                main()
        run.assert_not_called()


class NetworkMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            web_host="0.0.0.0",
            web_allowed_hosts=("127.0.0.1", "localhost", "::1", "192.168.50.7", "knowledge.lan", "fd12:3456::7"),
            web_allowed_networks=("127.0.0.0/8", "::1/128", "192.168.50.0/24", "fd12:3456::/64"),
        )

    async def respond(self, *, host="192.168.50.7:8100", peer="192.168.50.22", origin=MISSING,
                      scheme="http", extra=(), settings=None, method="POST", path="/mcp"):
        headers = [(b"host", host.encode("ascii"))]
        if origin is not MISSING:
            headers.append((b"origin", origin.encode("ascii")))
        headers.extend((name.lower().encode("ascii"), value.encode("ascii")) for name, value in extra)
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": scheme, "path": path, "raw_path": path.encode("ascii"),
            "root_path": "", "query_string": b"", "headers": headers,
            "client": (peer, 45678) if peer is not None else None,
            "server": ("192.168.50.7", 8100),
            "app": SimpleNamespace(state=SimpleNamespace(service=SimpleNamespace(settings=settings or self.settings))),
        }
        request = Request(scope)
        next_call = AsyncMock(return_value=Response(status_code=204))
        response = await local_origin(request, next_call)
        if response.status_code == 204:
            next_call.assert_awaited_once_with(request)
        else:
            next_call.assert_not_awaited()
        return response

    async def check(self, **kwargs):
        return (await self.respond(**kwargs)).status_code

    async def test_allowed_lan_peer_and_same_origin_or_absent_origin_are_accepted(self):
        self.assertEqual(await self.check(), 204)
        self.assertEqual(await self.check(origin="http://192.168.50.7:8100"), 204)
        self.assertEqual(await self.check(host="KNOWLEDGE.LAN:8100", origin="http://knowledge.lan:8100"), 204)

    async def test_unknown_or_misleading_host_is_rejected(self):
        for host in ("attacker.test:8100", "192.168.50.7.attacker.test:8100", "192.168.50.70:8100",
                     "192.168.50.7@attacker.test:8100"):
            with self.subTest(host=host):
                self.assertEqual(await self.check(host=host), 403)

    async def test_peer_must_be_a_real_ip_in_an_explicit_allowed_network(self):
        for peer in ("192.168.51.22", "8.8.8.8", "100.64.0.1", "testclient", "unknown", None):
            with self.subTest(peer=peer):
                self.assertEqual(await self.check(peer=peer), 403)

    async def test_cross_origin_scheme_host_and_port_are_rejected(self):
        for origin in ("https://192.168.50.7:8100", "http://192.168.50.7:8101",
                       "http://localhost:8100", "http://knowledge.lan:8100", "http://attacker.test:8100"):
            with self.subTest(origin=origin):
                self.assertEqual(await self.check(origin=origin), 403)

    async def test_malformed_empty_and_opaque_origins_are_rejected(self):
        for origin in ("", "null", "http://192.168.50.7:8100/path", "http://192.168.50.7:8100/",
                       "http://192.168.50.7:8100?x=1", "http://192.168.50.7:8100#fragment",
                       "http://user:password@192.168.50.7:8100", "http://192.168.50.7:99999",
                       "http://[broken", "http://192.168.50.7:8100, http://attacker.test"):
            with self.subTest(origin=origin):
                self.assertEqual(await self.check(origin=origin), 403)

    async def test_standard_default_ports_are_equivalent(self):
        for host, scheme, origin in (("knowledge.lan", "http", "http://knowledge.lan:80"),
                                     ("knowledge.lan:80", "http", "http://knowledge.lan"),
                                     ("knowledge.lan", "https", "https://knowledge.lan:443"),
                                     ("knowledge.lan:443", "https", "https://knowledge.lan")):
            with self.subTest(host=host, origin=origin):
                self.assertEqual(await self.check(host=host, scheme=scheme, origin=origin), 204)

    async def test_ipv6_lan_and_loopback_peers_are_supported(self):
        self.assertEqual(await self.check(host="[fd12:3456::7]:8100", peer="fd12:3456::22",
                                          origin="http://[fd12:3456::7]:8100"), 204)
        self.assertEqual(await self.check(host="[::1]:8100", peer="::1", origin="http://[::1]:8100"), 204)
        self.assertEqual(await self.check(host="[fd12:3456::7]:8100", peer="fd12:3457::22"), 403)

    async def test_forwarded_headers_cannot_override_real_peer_host_or_scheme(self):
        forwarding = (("X-Forwarded-For", "192.168.50.22"), ("X-Forwarded-Host", "192.168.50.7:8100"),
                      ("Forwarded", 'for=192.168.50.22;host="192.168.50.7:8100";proto=http'))
        self.assertEqual(await self.check(peer="8.8.8.8", extra=forwarding), 403)
        self.assertEqual(await self.check(host="attacker.test:8100", extra=forwarding), 403)
        self.assertEqual(await self.check(extra=(("X-Forwarded-Proto", "https"),),
                                          origin="https://192.168.50.7:8100"), 403)
        self.assertEqual(await self.check(extra=(("X-Forwarded-For", "8.8.8.8"),
                                                ("X-Forwarded-Host", "attacker.test"),
                                                ("X-Forwarded-Proto", "https")),
                                          origin="http://192.168.50.7:8100"), 204)

    async def test_browser_rejection_explains_the_reason_and_client_address(self):
        response = await self.respond(peer="192.168.51.22", method="GET", path="/",
                                      extra=(("Accept", "text/html,application/xhtml+xml"),))
        self.assertEqual(response.status_code, 403)
        self.assertIn("text/html", response.headers["content-type"])
        body = response.body.decode("utf-8")
        self.assertIn("访问来源不在允许的局域网范围内", body)
        self.assertIn("192.168.51.22", body)
        self.assertIn("192.168.50.0/24", body)
        self.assertIn("knowledge.lan", body)

    async def test_programmatic_clients_still_receive_json(self):
        for accept in ("*/*", "application/json"):
            with self.subTest(accept=accept):
                response = await self.respond(peer="192.168.51.22", extra=(("Accept", accept),))
                self.assertEqual(response.status_code, 403)
                self.assertIn("application/json", response.headers["content-type"])
                self.assertEqual(json.loads(response.body),
                                 {"detail": "访问来源不在允许的局域网范围内"})

    async def test_host_rejection_page_lists_configured_hosts(self):
        response = await self.respond(host="attacker.test:8100", method="GET", path="/",
                                      extra=(("Accept", "text/html"),))
        body = response.body.decode("utf-8")
        self.assertIn("请求主机不在允许访问的地址中", body)
        self.assertIn("192.168.50.7", body)

    async def test_legacy_fake_settings_fall_back_to_loopback_only(self):
        legacy = SimpleNamespace(public_base_url="http://127.0.0.1:8100")
        self.assertEqual(await self.check(settings=legacy, host="127.0.0.1:8100", peer="127.0.0.1"), 204)
        self.assertEqual(await self.check(settings=legacy, host="localhost:8100", peer="::1"), 204)
        self.assertEqual(await self.check(settings=legacy), 403)
        self.assertEqual(await self.check(settings=legacy, host="localhost:8100", peer="192.168.50.22"), 403)


if __name__ == "__main__":
    unittest.main()
