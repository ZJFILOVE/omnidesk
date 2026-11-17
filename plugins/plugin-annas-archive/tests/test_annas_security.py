"""Security, path traversal, SSRF/redirect, size-limit, magic, and temp-cleanup tests for plugin-annas-archive."""

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

from annas_test_helpers import MD5_SICP, MockAnnasState, run_mock_annas_server  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_annas_sec_under_test", PLUGIN_DIR / "plugin.py")
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
    state = MockAnnasState()

    with run_mock_annas_server(state) as mirror_url:
        ctx.set_config({"mirror_url": mirror_url, "download_root": "", "max_download_mb": 1, "request_timeout_sec": 0.2})
        await invoke_lifecycle_hook(plugin.on_load, ctx)
        no_root = await ctx.capabilities.call("book.search.v1", {"action": "download", "item_id": MD5_SICP, "download_token": f"/dl/{MD5_SICP}/pdf"})
        assert no_root.ok is False and no_root.data["error_code"] == "not_configured"

        ctx.set_config({"download_root": str(dl_root)})
        dl_root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (dl_root / "symdir").symlink_to(outside)

        for bad_dest in ("../escape.pdf", "/etc/passwd", str(tmp_path / "outside.pdf"), "sub/../../escape.pdf", "..\\win.pdf", "symdir/evil.pdf"):
            res = await ctx.capabilities.call(
                "book.search.v1",
                {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": f"/dl/{MD5_SICP}/pdf", "destination": bad_dest},
            )
            assert res.ok is False and res.data["error_code"] == "path_safety"

        big_hdr = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": "/dl/oversized-header", "destination": "bighdr.pdf"},
        )
        assert big_hdr.ok is False and big_hdr.data["error_code"] == "file_too_large"
        assert not (dl_root / "bighdr.pdf").exists()

        big_stream = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": "/dl/oversized-stream", "destination": "bigstream.pdf"},
        )
        assert big_stream.ok is False and big_stream.data["error_code"] == "file_too_large"
        assert not (dl_root / "bigstream.pdf").exists()

        wrong_magic = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": "/dl/wrong-magic", "destination": "bad.pdf"},
        )
        assert wrong_magic.ok is False and wrong_magic.data["error_code"] == "invalid_magic"
        assert not (dl_root / "bad.pdf").exists()

        bomb = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "epub", "download_token": "/dl/zip-bomb", "destination": "bomb.epub"},
        )
        assert bomb.ok is False and bomb.data["error_code"] == "zip_bomb"
        assert not (dl_root / "bomb.epub").exists()

        existing = dl_root / "keep.pdf"
        existing.write_bytes(b"%PDF-1.7\noriginal")
        state.slow_delay_sec = 0.8
        t_out = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": "/dl/timeout-mid", "destination": "keep.pdf"},
        )
        assert t_out.ok is False and t_out.data["error_code"] == "timeout"
        assert existing.read_bytes() == b"%PDF-1.7\noriginal"

        state.mode = "500"
        http_err = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": f"/dl/{MD5_SICP}/pdf", "destination": "err500.pdf"},
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
    state = MockAnnasState()

    with run_mock_annas_server(state) as mirror_url:
        ctx.set_config({"mirror_url": mirror_url, "download_root": str(dl_root)})
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        ok_redir = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": "/dl/redirect-ok", "destination": "redir.pdf"},
        )
        assert ok_redir.ok is True
        assert (dl_root / "redir.pdf").read_bytes() == state.pdf_bytes

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
                {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": "/dl/redirect-unsafe", "destination": "evil.pdf"},
            )
            assert bad_redir.ok is False and bad_redir.data["error_code"] == "unsafe_url"
            assert not (dl_root / "evil.pdf").exists()

        # Search timeout returns timeout cleanly
        ctx.set_config({"request_timeout_sec": 0.2})
        state.mode = "timeout"
        state.slow_delay_sec = 0.8
        search_to = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
        assert search_to.ok is False and search_to.data["error_code"] == "timeout"
        state.mode = "normal"

        # Cross-origin redirect strips Cookie/Authorization/X-Api-Key headers
        import urllib.request
        from annas_http import _SafeRedirectHandler
        from annas_security import parse_origin
        o1 = parse_origin("https://mirror-a.example")
        o2 = parse_origin("https://cdn-b.example")
        handler = _SafeRedirectHandler(frozenset({o1, o2}), resolve_dns=False)
        req_orig = urllib.request.Request(
            "https://mirror-a.example/dl/1",
            headers={"Cookie": "secret=1", "Authorization": "Bearer abc", "X-Api-Key": "ak", "User-Agent": "UA"},
        )
        req_next = handler.redirect_request(req_orig, None, 302, "Found", {}, "https://cdn-b.example/file.pdf")
        assert req_next is not None
        all_hdrs = {k.lower() for k in {**req_next.headers, **req_next.unredirected_hdrs}}
        assert "cookie" not in all_hdrs
        assert "authorization" not in all_hdrs
        assert "x-api-key" not in all_hdrs

        for direct_bad in ("file:///etc/passwd", "http://127.0.0.1:1/x.pdf", "http://[::1]:8080/x.pdf"):
            res = await ctx.capabilities.call(
                "book.search.v1",
                {"action": "download", "item_id": MD5_SICP, "format": "pdf", "download_token": direct_bad, "destination": "bad2.pdf"},
            )
            assert res.ok is False and res.data["error_code"] == "unsafe_url"

        ctx.set_config({"mirror_url": "file:///etc/passwd"})
        cfg_bad = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "test"})
        assert cfg_bad.ok is False and cfg_bad.data["error_code"] == "unsafe_url"

        assert list(dl_root.rglob("*.tmp")) == []
        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
