"""Security regressions for the loopback Magpie gateway HTTP client."""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for path_entry in (str(PLUGIN_DIR), str(REPO_ROOT)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)


def _load_gateway_module() -> Any:
    spec = importlib.util.spec_from_file_location("magpie_gateway_security_under_test", PLUGIN_DIR / "magpie_gateway.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Server:
    def __init__(self, handler: type[http.server.BaseHTTPRequestHandler]) -> None:
        self.auth_seen: list[str] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.auth_seen = self.auth_seen  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        auth_seen = server.auth_seen  # type: ignore[attr-defined]
        auth_seen.append(self.headers.get("Authorization", ""))
        body = json.dumps([{"provider": "p", "name": "gateway"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def _redirect_handler(target: str, *, status: int = 302) -> type[http.server.BaseHTTPRequestHandler]:
    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.auth_seen.append(self.headers.get("Authorization", ""))  # type: ignore[attr-defined]
            self.send_response(status)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return _RedirectHandler


@pytest.mark.asyncio
async def test_cross_origin_redirect_strips_authorization() -> None:
    module = _load_gateway_module()
    final = _Server(_JsonHandler)
    redirector = _Server(_redirect_handler(f"{final.base_url}/v1/magpie/quotas"))
    try:
        client = module.MagpieGatewayClient()
        data = await client.fetch_quotas(redirector.base_url, secret="sk-gateway-secret", timeout_sec=3.0)
        assert data == [{"provider": "p", "name": "gateway"}]
        assert redirector.auth_seen == ["Bearer sk-gateway-secret"]
        assert final.auth_seen == [""]
    finally:
        redirector.close()
        final.close()


@pytest.mark.asyncio
async def test_same_origin_redirect_keeps_authorization() -> None:
    module = _load_gateway_module()

    class _SameOriginHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/v1/magpie/quotas":
                self.server.auth_seen.append(self.headers.get("Authorization", ""))  # type: ignore[attr-defined]
                self.send_response(302)
                self.send_header("Location", "/v1/magpie/quotas-final")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            _JsonHandler.do_GET(self)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = _Server(_SameOriginHandler)
    try:
        client = module.MagpieGatewayClient()
        data = await client.fetch_quotas(server.base_url, secret="sk-gateway-secret", timeout_sec=3.0)
        assert data == [{"provider": "p", "name": "gateway"}]
        assert server.auth_seen == ["Bearer sk-gateway-secret", "Bearer sk-gateway-secret"]
    finally:
        server.close()


@pytest.mark.asyncio
async def test_fetch_quotas_fail_closed_on_timeout_and_bad_response() -> None:
    module = _load_gateway_module()

    class _SlowHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(0.4)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    slow = _Server(_SlowHandler)
    try:
        client = module.MagpieGatewayClient()
        started = time.monotonic()
        assert await client.fetch_quotas(slow.base_url, secret="sk-secret", timeout_sec=0.1) is None
        assert time.monotonic() - started < 1.0
    finally:
        slow.close()

    class _UnauthorizedHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    unauthorized = _Server(_UnauthorizedHandler)
    try:
        assert await module.MagpieGatewayClient().fetch_quotas(unauthorized.base_url, secret="sk-secret") is None
    finally:
        unauthorized.close()

    class _MalformedHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"not-json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    malformed = _Server(_MalformedHandler)
    try:
        assert await module.MagpieGatewayClient().fetch_quotas(malformed.base_url, secret="sk-secret") is None
    finally:
        malformed.close()
