"""Tests for SSRF, redirect, timeout, size, and redaction guarantees in SafeHttpClient."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from omnidesk_sdk import (
    HttpErrorCode,
    HttpTimeoutError,
    ResponseTooLargeError,
    SafeHttpClient,
    SafeHttpError,
    SsrfBlockedError,
    UpstreamStatusError,
)


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


class _Handler(BaseHTTPRequestHandler):
    received: dict[str, dict[str, str]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _send(self, status: int, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        type(self).received[self.path] = {key.lower(): value for key, value in self.headers.items()}

        if self.path.startswith("http://"):
            self._send(200, b'{"proxied": true}', headers={"Content-Type": "application/json"})
            return
        if self.path == "/ok":
            self._send(200, b'{"ok": true}', headers={"Content-Type": "application/json"})
            return
        if self.path == "/redirect":
            target = type(self).received.get("_redirect_target", {}).get("target", "")
            self._send(302, b"", headers={"Location": target})
            return
        if self.path == "/slow":
            time.sleep(0.5)
            self._send(200, b"late")
            return
        if self.path == "/large":
            self._send(200, b"x" * 512)
            return
        if self.path.startswith("/fail"):
            self._send(
                401,
                b"upstream echoed Bearer sk-live-secret and api_key=query-secret",
                headers={"Content-Type": "text/plain"},
            )
            return
        self._send(404, b"not found")


@contextmanager
def _start_server() -> Iterator[str]:
    server = _QuietServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _local_client(**kwargs: Any) -> SafeHttpClient:
    options = {"allow_localhost_http": True, "trust_env": False}
    options.update(kwargs)
    return SafeHttpClient(**options)


@pytest.mark.asyncio
async def test_client_rejects_localhost_and_plain_http_by_default() -> None:
    with pytest.raises(SsrfBlockedError) as exc_info:
        await SafeHttpClient().get("http://127.0.0.1:9/")
    assert exc_info.value.code == HttpErrorCode.SSRF

    with pytest.raises(SsrfBlockedError):
        await SafeHttpClient().get("http://example.com/")

    disabled = SafeHttpClient(trust_env=False)
    assert disabled.trust_env is False


@pytest.mark.asyncio
async def test_explicit_http_proxy_uses_absolute_uri() -> None:
    with _start_server() as proxy_url:
        client = _local_client(proxy_url=proxy_url)
        async with client:
            response = await client.get("http://127.0.0.1:9/unreachable")

    assert response.status_code == 200
    assert response.json() == {"proxied": True}
    assert "http://127.0.0.1:9/unreachable" in _Handler.received


@pytest.mark.asyncio
async def test_environment_http_proxy_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    with _start_server() as proxy_url:
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("http_proxy", proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        async with SafeHttpClient(allow_localhost_http=True) as client:
            response = await client.get("http://127.0.0.1:9/via-env")

    assert response.status_code == 200
    assert response.json() == {"proxied": True}
    assert "http://127.0.0.1:9/via-env" in _Handler.received


@pytest.mark.asyncio
async def test_local_get_and_json_decode() -> None:
    with _start_server() as base_url:
        async with _local_client() as client:
            response = await client.get(f"{base_url}/ok")
            payload = await client.request_json("GET", f"{base_url}/ok")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert payload == {"ok": True}


@pytest.mark.asyncio
async def test_cross_origin_redirect_strips_authorization_and_cookie() -> None:
    with _start_server() as first_url, _start_server() as second_url:
        _Handler.received["_redirect_target"] = {"target": f"{second_url}/ok"}
        async with _local_client() as client:
            response = await client.get(
                f"{first_url}/redirect",
                headers={"Authorization": "Bearer sk-live-secret", "Cookie": "sid=secret"},
            )

    assert response.status_code == 200
    echoed = _Handler.received["/ok"]
    assert "authorization" not in echoed
    assert "cookie" not in echoed


@pytest.mark.asyncio
async def test_timeout_is_classified() -> None:
    with _start_server() as base_url:
        async with _local_client(timeout_sec=0.1) as client:
            with pytest.raises(HttpTimeoutError) as exc_info:
                await client.get(f"{base_url}/slow")

    assert exc_info.value.code == HttpErrorCode.TIMEOUT


@pytest.mark.asyncio
async def test_content_length_over_limit_is_rejected() -> None:
    with _start_server() as base_url:
        async with _local_client(max_bytes=128) as client:
            with pytest.raises(ResponseTooLargeError) as exc_info:
                await client.get(f"{base_url}/large")

    assert exc_info.value.code == HttpErrorCode.TOO_LARGE


@pytest.mark.asyncio
async def test_upstream_status_and_error_redaction() -> None:
    with _start_server() as base_url:
        async with _local_client() as client:
            with pytest.raises(UpstreamStatusError) as exc_info:
                await client.get(
                    f"{base_url}/fail?api_key=query-secret",
                    headers={"Authorization": "Bearer sk-live-secret"},
                )

    error = exc_info.value
    assert error.code == HttpErrorCode.UPSTREAM_STATUS
    assert error.status_code == 401
    assert "sk-live-secret" not in str(error)
    assert "query-secret" not in str(error)
    assert "[REDACTED]" in str(error)
    assert "query-secret" not in error.url


@pytest.mark.asyncio
async def test_raise_for_status_false_returns_response() -> None:
    with _start_server() as base_url:
        async with _local_client(raise_for_status=False) as client:
            response = await client.get(f"{base_url}/fail")

    assert response.status_code == 401
    with pytest.raises(UpstreamStatusError):
        response.raise_for_status()


@pytest.mark.asyncio
async def test_invalid_request_arguments_fail_closed() -> None:
    with _start_server() as base_url:
        async with _local_client() as client:
            with pytest.raises(SafeHttpError) as exc_info:
                await client.get(f"{base_url}/ok", headers={"X-Bad": "line\r\nbreak"})
            with pytest.raises(SafeHttpError):
                await client.request("GET", f"{base_url}/ok", content=b"a", json={"b": 1})

    assert exc_info.value.code == HttpErrorCode.INVALID_ARGUMENT
