"""Security, path traversal, SSRF/redirect, size-limit, magic, and temp-cleanup tests for plugin-zlib."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(REPO_ROOT), str(PLUGIN_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402
from zlib_test_helpers import MockZlibState, run_mock_zlib_server  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_zlib_sec_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_ctx(tmp_path: Path) -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        config_schema=manifest["config_schema"],
    )


@pytest.mark.asyncio
async def test_download_security_caps_magic_and_temp_cleanup(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)
    dl_root = tmp_path / "downloads"
    state = MockZlibState()

    with run_mock_zlib_server(state) as mirror_url:
        # 1) Unconfigured download_root fails closed
        ctx.set_config({"mirror_url": mirror_url, "download_root": "", "max_download_mb": 1, "request_timeout_sec": 0.2})
        await invoke_lifecycle_hook(plugin.on_load, ctx)
        no_root = await ctx.capabilities.call("book.search.v1", {"action": "download", "item_id": "101:abc101", "download_token": "/dl/101/pdf101"})
        assert no_root.ok is False
        assert no_root.data["error_code"] == "not_configured"

        # 2) Path traversal attempts blocked
        ctx.set_config({"download_root": str(dl_root)})
        dl_root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (dl_root / "symdir").symlink_to(outside)

        for bad_dest in ("../escape.pdf", "/etc/passwd", str(tmp_path / "outside.pdf"), "sub/../../escape.pdf", "..\\win.pdf", "symdir/evil.pdf"):
            res = await ctx.capabilities.call(
                "book.search.v1",
                {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/pdf101", "destination": bad_dest},
            )
            assert res.ok is False, f"expected failure for {bad_dest}"
            assert res.data["error_code"] == "path_safety"

        # 3) Oversized Content-Length and streaming cap
        big_hdr = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/oversized-header", "destination": "bighdr.pdf"},
        )
        assert big_hdr.ok is False and big_hdr.data["error_code"] == "file_too_large"
        assert not (dl_root / "bighdr.pdf").exists()

        big_stream = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/oversized-stream", "destination": "bigstream.pdf"},
        )
        assert big_stream.ok is False and big_stream.data["error_code"] == "file_too_large"
        assert not (dl_root / "bigstream.pdf").exists()

        # 4) Wrong magic & EPUB zip bomb cleanup
        wrong_magic = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/wrong-magic", "destination": "bad.pdf"},
        )
        assert wrong_magic.ok is False and wrong_magic.data["error_code"] == "invalid_magic"
        assert not (dl_root / "bad.pdf").exists()

        bomb = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "epub", "download_token": "/dl/101/zip-bomb", "destination": "bomb.epub"},
        )
        assert bomb.ok is False and bomb.data["error_code"] == "zip_bomb"
        assert not (dl_root / "bomb.epub").exists()

        # 5) Timeout and HTTP 500 clean up temp files and preserve pre-existing files
        existing = dl_root / "keep.pdf"
        existing.write_bytes(b"%PDF-1.7\noriginal")
        state.slow_delay_sec = 0.8
        t_out = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/timeout-mid", "destination": "keep.pdf"},
        )
        assert t_out.ok is False and t_out.data["error_code"] == "timeout"
        assert existing.read_bytes() == b"%PDF-1.7\noriginal"

        state.mode = "500"
        http_err = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/pdf101", "destination": "err500.pdf"},
        )
        assert http_err.ok is False and http_err.data["error_code"] == "service_offline"
        assert not (dl_root / "err500.pdf").exists()
        assert list(dl_root.rglob("*.tmp")) == []

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_ssrf_and_redirect_protections(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)
    dl_root = tmp_path / "dl"
    state = MockZlibState()

    with run_mock_zlib_server(state) as mirror_url:
        ctx.set_config({"mirror_url": mirror_url, "download_root": str(dl_root)})
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Same-origin redirect works
        ok_redir = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/redirect-ok", "destination": "redir.pdf"},
        )
        assert ok_redir.ok is True
        assert (dl_root / "redir.pdf").read_bytes() == state.pdf_bytes

        # Redirects to file://, ftp://, metadata IP, LAN IP, or unconfigured loopback port are blocked
        for target in (
            "file:///etc/passwd",
            "ftp://example.com/book.pdf",
            "http://169.254.169.254/latest/meta-data",
            "http://192.168.1.10/admin",
            "http://10.0.0.1/secret",
            "http://127.0.0.1:9/other-port",
            "http://localhost:3425/v1/magpie/quotas",
        ):
            state.redirect_location = target
            bad_redir = await ctx.capabilities.call(
                "book.search.v1",
                {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": "/dl/101/redirect-unsafe", "destination": "evil.pdf"},
            )
            assert bad_redir.ok is False, f"expected SSRF block for redirect to {target}"
            assert bad_redir.data["error_code"] == "unsafe_url"
            assert not (dl_root / "evil.pdf").exists()

        # Search timeout returns timeout error_code cleanly
        ctx.set_config({"request_timeout_sec": 0.2})
        state.mode = "timeout"
        state.slow_delay_sec = 0.8
        search_to = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
        assert search_to.ok is False and search_to.data["error_code"] == "timeout"
        state.mode = "normal"

        # Cross-origin redirect strips Cookie/Authorization/remix-key headers
        import urllib.request
        from zlib_http import _SafeRedirectHandler
        from zlib_security import parse_origin
        o1 = parse_origin("https://mirror-a.example")
        o2 = parse_origin("https://cdn-b.example")
        handler = _SafeRedirectHandler(frozenset({o1, o2}), resolve_dns=False)
        req_orig = urllib.request.Request(
            "https://mirror-a.example/dl/1",
            headers={"Cookie": "secret=1", "Authorization": "Bearer abc", "remix-key": "rk", "User-Agent": "UA"},
        )
        req_next = handler.redirect_request(req_orig, None, 302, "Found", {}, "https://cdn-b.example/file.pdf")
        assert req_next is not None
        all_hdrs = {k.lower() for k in {**req_next.headers, **req_next.unredirected_hdrs}}
        assert "cookie" not in all_hdrs
        assert "authorization" not in all_hdrs
        assert "remix-key" not in all_hdrs

        # Direct file:// or unconfigured private URL in token/detail_url is blocked
        for direct_bad in ("file:///etc/passwd", "http://127.0.0.1:1/x.pdf", "http://[::1]:8080/x.pdf"):
            res = await ctx.capabilities.call(
                "book.search.v1",
                {"action": "download", "item_id": "101:abc101", "format": "pdf", "download_token": direct_bad, "destination": "bad2.pdf"},
            )
            assert res.ok is False and res.data["error_code"] == "unsafe_url"

        # file:// in config base_url/mirror_url is rejected too
        ctx.set_config({"mirror_url": "file:///etc/passwd"})
        cfg_bad = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "test"})
        assert cfg_bad.ok is False and cfg_bad.data["error_code"] == "unsafe_url"

        assert list(dl_root.rglob("*.tmp")) == []
        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
