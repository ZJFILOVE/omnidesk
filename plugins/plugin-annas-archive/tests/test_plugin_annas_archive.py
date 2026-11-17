"""Offline contract, search/detail/download, auth, and capability handoff tests for plugin-annas-archive."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(REPO_ROOT), str(PLUGIN_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from annas_test_helpers import MD5_DDIA, MD5_SICP, MockAnnasState, run_mock_annas_server  # noqa: E402
from omnidesk_core.manifest import load_manifest  # noqa: E402
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_annas_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_ctx(tmp_path: Path, manifest: dict[str, Any]) -> PluginContext:
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        config_schema=manifest["config_schema"],
    )


def test_manifest_matches_v2_schema_and_no_private_defaults() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    loaded = load_manifest(PLUGIN_DIR)
    assert loaded.id == "plugin-annas-archive"
    assert manifest["schema_version"] == 2
    assert manifest["kind"] == "hybrid"
    assert manifest["domain"] == "book"
    assert manifest["provides"] == ["book.search.v1"]

    assert set(manifest.keys()).issubset(set(schema["properties"].keys()))
    for req_key in schema["required"]:
        assert req_key in manifest
    assert re.match(schema["properties"]["provides"]["items"]["pattern"], "book.search.v1")

    fields = {item["key"]: item for item in manifest["config_schema"]}
    assert {"base_url", "mirror_url", "download_root", "max_download_mb", "api_key", "cookie_header"}.issubset(fields)
    assert fields["api_key"]["type"] == "secret"
    assert fields["cookie_header"]["type"] == "secret"
    assert fields["download_root"]["default"] == ""
    assert "127.0.0.1" not in fields["base_url"]["default"]
    assert "192.168." not in fields["base_url"]["default"]


@pytest.mark.asyncio
async def test_search_detail_download_and_slow_html_resolution_offline(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    assert isinstance(plugin, BasePlugin)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    state = MockAnnasState()
    dl_root = tmp_path / "annas_books"

    with run_mock_annas_server(state) as mirror_url:
        ctx = _make_ctx(tmp_path, manifest)
        ctx.set_config({
            "base_url": "https://annas-archive.org",
            "mirror_url": mirror_url,
            "download_root": str(dl_root),
            "max_download_mb": 1,
        })
        await invoke_lifecycle_hook(plugin.on_load, ctx)
        assert (await invoke_lifecycle_hook(plugin.on_health_check))["ok"] is True
        assert ctx.capabilities.has("book.search.v1")
        assert len(ctx.domains.workspace_tabs("book")) == 1

        # Search parses both direct and comment-wrapped cards
        res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp", "limit": 10})
        assert res.ok is True
        assert res.data["count"] == 2
        first = res.data["results"][0]
        assert first["item_id"] == MD5_SICP
        assert first["title"] == "Structure and Interpretation of Computer Programs"
        assert first["author"] == "Harold Abelson, Gerald Jay Sussman"
        assert first["year"] == "1996"
        assert first["format"] == "pdf"
        assert first["size"] == "14.2 MB"
        assert first["language"] == "English"
        assert first["detail_url"] == f"{mirror_url}/md5/{MD5_SICP}"
        assert first["download_token"]
        assert res.data["results"][1]["item_id"] == MD5_DDIA

        # Format filter & limit
        epub_only = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "ddia", "format": "epub", "limit": 1})
        assert epub_only.ok is True
        assert epub_only.data["count"] == 1
        assert epub_only.data["results"][0]["format"] == "epub"

        # Empty result returns results=[]
        state.mode = "empty"
        empty_res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "doesnotexist"})
        assert empty_res.ok is True and empty_res.data["results"] == []
        state.mode = "normal"

        # Detail found vs unknown MD5 not_found
        det_res = await ctx.capabilities.call("book.search.v1", {"action": "detail", "item_id": MD5_SICP})
        assert det_res.ok is True
        assert det_res.data["title"] == "Structure and Interpretation of Computer Programs"
        assert set(det_res.data["available_formats"]) == {"pdf", "epub"}

        nf_res = await ctx.capabilities.call("book.search.v1", {"action": "detail", "item_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
        assert nf_res.ok is False and nf_res.data["error_code"] == "not_found"

        # Download via /slow_download/ HTML intermediate link, plus direct EPUB, MOBI, AZW3, TXT
        for fmt, tok, expected_bytes, dest in (
            ("pdf", f"/slow_download/{MD5_SICP}/0/0", state.pdf_bytes, "sicp.pdf"),
            ("epub", f"/dl/{MD5_SICP}/epub", state.epub_bytes, "nested/patterns.epub"),
            ("mobi", f"/dl/{MD5_SICP}/mobi", state.mobi_bytes, "book.mobi"),
            ("azw3", f"/dl/{MD5_SICP}/mobi", state.mobi_bytes, "book.azw3"),
            ("txt", f"/dl/{MD5_SICP}/txt", state.txt_bytes, "notes.txt"),
        ):
            dl_res = await ctx.capabilities.call(
                "book.search.v1",
                {"action": "download", "item_id": MD5_SICP, "format": fmt, "download_token": tok, "destination": dest},
            )
            assert dl_res.ok is True, f"{fmt} failed: {dl_res}"
            saved = Path(dl_res.data["file_path"])
            assert saved.read_bytes() == expected_bytes
            assert dl_res.data["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
        assert list(dl_root.rglob("*.tmp")) == []

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
        assert not ctx.capabilities.has("book.search.v1")


@pytest.mark.asyncio
async def test_fast_api_auth_priority_redaction_and_handoff(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    state = MockAnnasState()
    vault_cookie = "aa_session=vault-secret-cookie-111"
    chrome_cookie = "aa_session=chrome-secret-cookie-222"
    fast_key = "aa-fast-member-secret-key-333"

    with run_mock_annas_server(state) as mirror_url:
        ctx = _make_ctx(tmp_path, manifest)
        ctx.set_config({"mirror_url": mirror_url, "download_root": str(tmp_path / "dl")})
        ctx.set_secret("cookie_header", vault_cookie)
        ctx.set_secret("api_key", fast_key)

        cfg_file = tmp_path / "cfg" / "plugins" / "plugin-annas-archive.json"
        if cfg_file.exists():
            raw_cfg = cfg_file.read_text(encoding="utf-8")
            assert vault_cookie not in raw_cfg
            assert fast_key not in raw_cfg

        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Vault cookie used when web.cookies.v1 absent; fast_download API downloads using api_key
        dl_res = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "destination": "fast.pdf"},
        )
        assert dl_res.ok is True
        assert any(r["path"] == "/dyn/api/fast_download.json" and f"key={urllib.parse.quote(fast_key)}" in r["query"] for r in state.requests)
        assert any(r["cookie"] == vault_cookie for r in state.requests)

        # Fast API error never leaks api_key
        state.fast_error = f"Invalid secret key {fast_key}"
        fast_err = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "destination": "fast2.pdf"},
        )
        assert fast_err.ok is False and fast_err.data["error_code"] == "auth_required"
        assert fast_key not in fast_err.error
        assert fast_key not in caplog.text
        state.fast_error = ""

        # web.cookies.v1 overrides Vault cookie
        ctx.capabilities.register(
            "web.cookies.v1",
            lambda req: CapabilityResult.ok({"cookies": [{"name": "aa_session", "value": "chrome-secret-cookie-222"}]}),
            plugin_id="plugin-chrome-web",
        )
        await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
        assert state.requests[-1]["cookie"] == chrome_cookie

        # 401 -> web.fetch.v1 fallback
        state.mode = "401"
        err_401 = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
        assert err_401.ok is False and err_401.data["error_code"] == "auth_required"

        ctx.capabilities.register(
            "web.fetch.v1",
            lambda req: CapabilityResult.ok({"status": 200, "json": {"results": [{"md5": MD5_SICP, "title": "Chrome Fetched", "format": "pdf"}]}}),
            plugin_id="plugin-chrome-web",
        )
        chrome_res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
        assert chrome_res.ok is True and chrome_res.data["results"][0]["title"] == "Chrome Fetched"
        state.mode = "normal"

        # Downstream handoff degradation & forwarding
        for action in ("open_in_babeldoc", "open_in_ebook", "send_to_nas"):
            miss = await ctx.capabilities.call("book.search.v1", {"action": action, "file_path": str(tmp_path / "dl" / "fast.pdf")})
            assert miss.ok is False and miss.data["error_code"] == "capability_unavailable"

        # WorkspaceTab action + cold download (detail resolution without prior search)
        tab = ctx.domains.workspace_tabs("book")[0]
        assert tab.on_action is not None
        tab_res = await tab.on_action("search", {"query": "sicp"})
        assert tab_res["ok"] is True

        ctx.set_secret("api_key", "")
        cold_dl = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": MD5_SICP, "format": "pdf", "destination": "cold.pdf"},
        )
        assert cold_dl.ok is True and Path(cold_dl.data["file_path"]).read_bytes() == state.pdf_bytes

        ctx.capabilities.register("book.bilingual_pdf.v1", lambda r: CapabilityResult.ok({"queued": True}), plugin_id="plugin-pdf-babeldoc")
        ctx.capabilities.register("book.source.v1", lambda r: CapabilityResult.ok({"opened": True}), plugin_id="plugin-ebook")
        ctx.capabilities.register("nas.connection.v1", lambda r: CapabilityResult.ok({"synced": True}), plugin_id="plugin-fnos-nas")
        bad_handoff = await ctx.capabilities.call("book.search.v1", {"action": "send_to_nas", "file_path": "../escape.pdf"})
        assert bad_handoff.ok is False and bad_handoff.data["error_code"] == "path_safety"

        for action, expected_cap in (
            ("open_in_babeldoc", "book.bilingual_pdf.v1"),
            ("open_in_ebook", "book.source.v1"),
            ("send_to_nas", "nas.connection.v1"),
        ):
            ok_fwd = await ctx.capabilities.call("book.search.v1", {"action": action, "item_id": MD5_SICP})
            assert ok_fwd.ok is True and ok_fwd.data["forwarded_capability"] == expected_cap

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_network_offline_and_500_return_service_offline(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]

    ctx = _make_ctx(tmp_path, manifest)
    ctx.set_config({"mirror_url": f"http://127.0.0.1:{dead_port}", "request_timeout_sec": 1})
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    offline = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
    assert offline.ok is False and offline.data["error_code"] == "service_offline"

    state = MockAnnasState()
    state.mode = "500"
    with run_mock_annas_server(state) as mirror_url:
        ctx.set_config({"mirror_url": mirror_url})
        err_500 = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
        assert err_500.ok is False and err_500.data["error_code"] == "service_offline"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
