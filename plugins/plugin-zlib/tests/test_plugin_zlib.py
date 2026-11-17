"""Offline contract, search/detail/download, auth, and capability handoff tests for plugin-zlib."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
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

from omnidesk_core.manifest import load_manifest  # noqa: E402
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, WorkspaceTabSpec, invoke_lifecycle_hook  # noqa: E402
from zlib_test_helpers import MockZlibState, run_mock_zlib_server  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_zlib_under_test", PLUGIN_DIR / "plugin.py")
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
    assert loaded.id == "plugin-zlib"
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
async def test_search_detail_download_and_mirror_priority_offline(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    assert isinstance(plugin, BasePlugin)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    state = MockZlibState()
    dl_root = tmp_path / "books"

    with run_mock_zlib_server(state) as mirror_url:
        ctx = _make_ctx(tmp_path, manifest)
        ctx.set_config({
            "base_url": "https://z-library.sk",
            "mirror_url": mirror_url,
            "download_root": str(dl_root),
            "max_download_mb": 1,
        })
        await invoke_lifecycle_hook(plugin.on_load, ctx)
        assert (await invoke_lifecycle_hook(plugin.on_health_check))["ok"] is True
        assert ctx.capabilities.has("book.search.v1")
        assert len(ctx.domains.workspace_tabs("book")) == 1

        # Search HTML + format filter + limit
        res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "interpreters", "format": "epub", "limit": 5})
        assert res.ok is True
        assert res.data["count"] == 1
        item = res.data["results"][0]
        assert item["title"] == "Crafting Interpreters"
        assert item["author"] == "Robert Nystrom"
        assert item["year"] == "2021"
        assert item["format"] == "epub"
        assert item["size"] == "8.6 MB"
        assert item["language"] == "English"
        assert item["detail_url"].startswith(mirror_url)
        assert item["download_token"] == "/dl/101/abc101"

        # Empty search returns results=[] without placeholder entries
        state.mode = "empty"
        empty_res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "nonexistent_xyz"})
        assert empty_res.ok is True
        assert empty_res.data["results"] == []
        assert empty_res.data["count"] == 0
        state.mode = "normal"

        # Detail found vs unknown not_found
        det_res = await ctx.capabilities.call("book.search.v1", {"action": "detail", "item_id": "101:abc101"})
        assert det_res.ok is True
        assert det_res.data["title"] == "Crafting Interpreters"
        assert set(det_res.data["available_formats"]) == {"epub", "pdf"}

        nf_res = await ctx.capabilities.call("book.search.v1", {"action": "detail", "item_id": "unknown999"})
        assert nf_res.ok is False
        assert nf_res.data["error_code"] == "not_found"

        # Download EPUB, PDF (via JSON downloadLink), MOBI, AZW3, TXT
        for fmt, tok, expected_bytes, dest in (
            ("epub", "/dl/101/abc101", state.epub_bytes, "sub/crafting.epub"),
            ("pdf", "/dl/101/eapi-json", state.pdf_bytes, "sicp.pdf"),
            ("mobi", "/dl/101/mobi", state.mobi_bytes, "book.mobi"),
            ("azw3", "/dl/101/mobi", state.mobi_bytes, "book.azw3"),
            ("txt", "/dl/101/txt", state.txt_bytes, "notes.txt"),
        ):
            dl_res = await ctx.capabilities.call(
                "book.search.v1",
                {"action": "download", "item_id": "101:abc101", "format": fmt, "download_token": tok, "destination": dest},
            )
            assert dl_res.ok is True, f"{fmt} failed: {dl_res}"
            saved = Path(dl_res.data["file_path"])
            assert saved.is_file()
            assert saved.read_bytes() == expected_bytes
            assert dl_res.data["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
        assert list(dl_root.rglob("*.tmp")) == []

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
        assert not ctx.capabilities.has("book.search.v1")
        assert ctx.domains.workspace_tabs("book") == []


@pytest.mark.asyncio
async def test_auth_priority_secret_redaction_and_capability_handoff(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    state = MockZlibState()
    secret_cookie = "remix_userid=42; remix_userkey=vault-cookie-secret-999"
    secret_api_key = "rk-super-secret-api-key-888"
    chrome_cookie = "remix_userid=77; remix_userkey=chrome-live-cookie-777"

    with run_mock_zlib_server(state) as mirror_url:
        ctx = _make_ctx(tmp_path, manifest)
        ctx.set_config({"mirror_url": mirror_url, "download_root": str(tmp_path / "dl")})
        ctx.set_secret("cookie_header", secret_cookie)
        ctx.set_secret("api_key", secret_api_key)
        ctx.set_secret("remix_userid", "42")

        # Secrets never stored in plain config JSON
        cfg_file = tmp_path / "cfg" / "plugins" / "plugin-zlib.json"
        if cfg_file.exists():
            raw_cfg = cfg_file.read_text(encoding="utf-8")
            assert secret_cookie not in raw_cfg
            assert secret_api_key not in raw_cfg

        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # 1) Without web.cookies.v1, falls back to SecretVault + EAPI
        res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "ddia"})
        assert res.ok is True
        assert res.data["results"][0]["item_id"] == "303:eapi303"
        assert state.requests[-1]["cookie"] == secret_cookie
        assert state.requests[-1]["remix_key"] == secret_api_key

        # 2) With web.cookies.v1, Chrome cookie takes priority over SecretVault
        ctx.capabilities.register(
            "web.cookies.v1",
            lambda req: CapabilityResult.ok({"cookie_header": chrome_cookie}),
            plugin_id="plugin-chrome-web",
        )
        await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "ddia"})
        assert state.requests[-1]["cookie"] == chrome_cookie

        # 3) On 401, falls back to web.fetch.v1 if registered; otherwise returns auth_required without leaking secret
        state.mode = "401"
        err_res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "ddia"})
        assert err_res.ok is False
        assert err_res.data["error_code"] == "auth_required"
        assert secret_cookie not in err_res.error
        assert secret_api_key not in err_res.error
        assert chrome_cookie not in caplog.text

        ctx.capabilities.register(
            "web.fetch.v1",
            lambda req: CapabilityResult.ok({"status": 200, "json": {"books": [{"id": 9, "hash": "h9", "title": "Via Chrome", "extension": "pdf", "href": "/book/9/h9"}]}}),
            plugin_id="plugin-chrome-web",
        )
        fetch_res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "chrome"})
        assert fetch_res.ok is True
        assert fetch_res.data["results"][0]["title"] == "Via Chrome"
        state.mode = "normal"

        # 4) Handoff actions degrade with capability_unavailable when absent, and forward when present
        for action in ("open_in_babeldoc", "open_in_ebook", "send_to_nas"):
            miss = await ctx.capabilities.call("book.search.v1", {"action": action, "file_path": str(tmp_path / "dl" / "a.pdf")})
            assert miss.ok is False
            assert miss.data["error_code"] == "capability_unavailable"

        # WorkspaceTab declarative action works
        tab = ctx.domains.workspace_tabs("book")[0]
        assert isinstance(tab, WorkspaceTabSpec)
        assert tab.on_action is not None
        tab_res = await tab.on_action("search", {"query": "interpreters"})
        assert tab_res["ok"] is True

        # Cold download (without prior search/detail) fetches detail and downloads PDF
        cold_dl = await ctx.capabilities.call(
            "book.search.v1",
            {"action": "download", "item_id": "101:abc101", "format": "pdf", "destination": "cold.pdf"},
        )
        assert cold_dl.ok is True
        assert Path(cold_dl.data["file_path"]).read_bytes() == state.pdf_bytes

        forwarded_calls: list[tuple[str, dict[str, Any]]] = []
        for cap_id in ("book.bilingual_pdf.v1", "book.source.v1", "book.sync.v1"):
            def _make_handler(cid: str):
                def _handler(req: dict[str, Any]) -> CapabilityResult:
                    forwarded_calls.append((cid, req))
                    return CapabilityResult.ok({"accepted": True, "cap": cid})
                return _handler
            ctx.capabilities.register(cap_id, _make_handler(cap_id), plugin_id="mock-provider")

        bad_handoff = await ctx.capabilities.call("book.search.v1", {"action": "open_in_babeldoc", "file_path": "../escape.pdf"})
        assert bad_handoff.ok is False and bad_handoff.data["error_code"] == "path_safety"

        for action, expected_cap in (
            ("open_in_babeldoc", "book.bilingual_pdf.v1"),
            ("open_in_ebook", "book.source.v1"),
            ("send_to_nas", "book.sync.v1"),
        ):
            ok_res = await ctx.capabilities.call("book.search.v1", {"action": action, "item_id": "101:abc101", "file_path": str(tmp_path / "dl" / "a.pdf")})
            assert ok_res.ok is True
            assert ok_res.data["forwarded_capability"] == expected_cap
        assert [c[0] for c in forwarded_calls] == ["book.bilingual_pdf.v1", "book.source.v1", "book.sync.v1"]

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_network_offline_and_500_return_service_offline(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    # Reserve an unused loopback port so connection immediately fails offline
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]

    ctx = _make_ctx(tmp_path, manifest)
    ctx.set_config({"mirror_url": f"http://127.0.0.1:{dead_port}", "request_timeout_sec": 1})
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    offline_res = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
    assert offline_res.ok is False
    assert offline_res.data["error_code"] == "service_offline"

    state = MockZlibState()
    state.mode = "500"
    with run_mock_zlib_server(state) as mirror_url:
        ctx.set_config({"mirror_url": mirror_url})
        err_500 = await ctx.capabilities.call("book.search.v1", {"action": "search", "query": "sicp"})
        assert err_500.ok is False
        assert err_500.data["error_code"] == "service_offline"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
