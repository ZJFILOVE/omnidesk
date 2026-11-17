"""Book sources recover from interrupted TLS without relaxing certificate checks."""
import importlib
import ssl
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from omnidesk_sdk import CapabilityResult


@pytest.mark.parametrize("name,folder", [("zlib", "plugin-zlib"), ("annas", "plugin-annas-archive")])
@pytest.mark.parametrize("failure", ["tls_eof", "timeout", "certificate", "unsafe_url"])
@pytest.mark.asyncio
async def test_browser_fallback_only_for_recoverable_failures(monkeypatch, name, folder, failure):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "plugins" / folder))
    http = importlib.import_module(f"{name}_http")
    auth = importlib.import_module(f"{name}_auth").AuthMaterial()
    errors = {
        "tls_eof": urllib.error.URLError(ssl.SSLEOFError("unexpected EOF")),
        "timeout": TimeoutError(),
        "certificate": urllib.error.URLError(ssl.SSLCertVerificationError("bad certificate")),
        "unsafe_url": http.HttpFetchError("unsafe_url", "unsafe redirect"),
    }
    def fail(*args, **kwargs):
        raise http._map_transport_exc(errors[failure], ())
    monkeypatch.setattr(http, "_sync_fetch_bytes", fail)
    call = AsyncMock(return_value=CapabilityResult.ok({"status": 200, "body": "books"}))
    ctx = SimpleNamespace(capabilities=SimpleNamespace(has=lambda cap: True, call=call))
    kwargs = dict(auth=auth, timeout_sec=2, max_bytes=1024, configured_origins=frozenset())
    if failure in {"certificate", "unsafe_url"}:
        with pytest.raises(http.HttpFetchError):
            await http.fetch_with_chrome_fallback(ctx, "https://example.com/search", **kwargs)
        call.assert_not_called()
    else:
        result = await http.fetch_with_chrome_fallback(ctx, "https://example.com/search", **kwargs)
        assert result.body == b"books" and result.via == "web.fetch.v1"
        payload = call.call_args.args[1]
        assert payload["headless"] and payload["open_tab_if_missing"]


@pytest.mark.asyncio
async def test_zlib_post_body_survives_browser_fallback(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'plugins/plugin-zlib'))
    http = importlib.import_module('zlib_http')
    auth = importlib.import_module('zlib_auth').AuthMaterial()
    def fail(url, **kwargs):
        assert kwargs['method'] == 'POST' and kwargs['body'] == b'message=hello'
        assert kwargs['headers']['Content-Type'] == 'application/x-www-form-urlencoded'
        raise http.HttpFetchError('auth_required', 'challenge')
    monkeypatch.setattr(http, '_sync_fetch_bytes', fail)
    call = AsyncMock(return_value=CapabilityResult.ok({'status': 200, 'json': {'success': 1, 'books': []}}))
    ctx = SimpleNamespace(capabilities=SimpleNamespace(has=lambda cap: True, call=call))
    await http.fetch_with_chrome_fallback(ctx, 'https://example.com/eapi/book/search', auth=auth, timeout_sec=2,
        max_bytes=1024, configured_origins=frozenset(), method='POST', body='message=hello')
    payload = call.call_args.args[1]
    assert payload['method'] == 'POST' and payload['body'] == 'message=hello'
