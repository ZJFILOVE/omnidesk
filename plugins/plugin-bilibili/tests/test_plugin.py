"""Offline tests for manifest, lifecycle, MediaSource feeds, empty results, and fail-closed errors."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(PLUGIN_DIR), str(REPO_ROOT), str(REPO_ROOT / "core"), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from mock_bili import MockBiliServer, MockBiliState  # noqa: E402
from omnidesk_core.paths import OmnideskPaths  # noqa: E402
from omnidesk_core.plugins import PluginManager  # noqa: E402
from omnidesk_sdk import BasePlugin, MediaItem, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _make_ctx(tmp_path: Path, *, api_base: str = "https://api.bilibili.com") -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"api_base": api_base, "request_timeout_sec": 1, "max_retries": 0})
    return ctx


def test_manifest_matches_v2_schema() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-bilibili"
    assert manifest["kind"] == "workspace"
    assert manifest["domain"] == "media"
    assert manifest["provides"] == ["media.source.v1"]
    assert set(schema["required"]).issubset(manifest.keys())
    assert set(manifest.keys()).issubset(set(schema["properties"].keys()))

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in manifest["provides"])
    assert all(cap_re.match(c) for c in manifest["consumes"])

    fields = {f["key"]: f for f in manifest["config_schema"]}
    assert {"cookie_header", "quality", "api_base", "ytdlp_bin", "proxy"}.issubset(fields.keys())
    assert fields["cookie_header"]["type"] == "secret"
    assert fields["cookie_header"]["default"] == ""
    assert fields["api_base"]["default"] == "https://api.bilibili.com"
    assert fields["quality"]["type"] == "select" and "4k" in fields["quality"]["options"]


@pytest.mark.asyncio
async def test_plugin_manager_load_health_and_unload_cleanup(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    )
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    rec = await mgr.load_plugin(PLUGIN_DIR)
    assert rec.plugin_id == "plugin-bilibili"
    assert len(mgr.domains.list_media_sources(plugin_id="plugin-bilibili")) == 1
    assert mgr.capabilities.has("bilibili.media.v1")
    assert len(mgr.workspace_tabs("media")) == 1

    health = await mgr.health_check("plugin-bilibili")
    assert health["ok"] is True
    assert health["cookie_configured"] is False

    await mgr.unload_plugin("plugin-bilibili")
    assert mgr.domains.list_media_sources(plugin_id="plugin-bilibili") == []
    assert mgr.capabilities.list_capabilities(plugin_id="plugin-bilibili") == []
    assert mgr.workspace_tabs("media") == []


@pytest.mark.asyncio
async def test_media_source_categories_fetch_items_and_empty_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Poison proxy env vars to prove loopback API bases stay direct despite environment proxies
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")

    state = MockBiliState()
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        assert isinstance(plugin, BasePlugin)
        ctx = _make_ctx(tmp_path, api_base=srv.base_url)
        ctx.set_secret("cookie_header", "SESSDATA=valid_sess_123456; DedeUserID=424242")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        src = ctx.domains.media_sources()[0]
        assert src.categories() == ["recommend", "dynamic", "bangumi", "live", "search"]

        rec_items = await src.fetch_items("recommend", page=1)
        assert len(rec_items) == 1
        assert isinstance(rec_items[0], MediaItem)
        assert rec_items[0].id == "BV1xK4y1Z7aa"
        assert rec_items[0].extra["bvid"] == "BV1xK4y1Z7aa"
        assert rec_items[0].extra["aid"] == 10001
        assert rec_items[0].creator == "极客图形社"
        assert rec_items[0].duration_sec == 1120.0
        assert rec_items[0].cover_url == "https://i0.hdslb.com/bfs/archive/cover1.jpg"
        assert rec_items[0].play_url == "https://www.bilibili.com/video/BV1xK4y1Z7aa"
        assert rec_items[0].quality_label == "1080p"

        dyn_items = await src.fetch_items("动态", page=1)
        assert len(dyn_items) == 1 and dyn_items[0].extra["bvid"] == "BV1bB4y1N7kp"
        assert dyn_items[0].duration_sec == 940.0

        bgm_items = await src.fetch_items("追番", page=1)
        assert len(bgm_items) == 1 and bgm_items[0].id == "ss45678"
        assert bgm_items[0].quality_label == "会员专享"

        live_items = await src.fetch_items("直播", page=1)
        assert len(live_items) == 1 and live_items[0].kind == "live"
        assert live_items[0].play_url == "https://live.bilibili.com/9001"

        search_items = await src.fetch_items("search", query="Wayland", page=1)
        assert len(search_items) == 1
        assert search_items[0].title == "Wayland 实战教程"
        assert search_items[0].duration_sec == 754.0

        # Empty search / empty query / empty feed return [] without fabricating videos
        assert await src.fetch_items("search", query="nothing-here") == []
        assert await src.fetch_items("search", query="   ") == []
        state.empty_feeds = True
        assert await src.fetch_items("recommend") == []
        assert await src.fetch_items("unknown-category") == []

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_fail_closed_on_401_412_timeout_and_legacy_fallback(tmp_path: Path) -> None:
    state = MockBiliState(legacy_paths_only=True)
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        ctx = _make_ctx(tmp_path, api_base=srv.base_url)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Unauthenticated dynamic/bangumi fails closed with not_configured
        dyn_fail = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_items", "category": "dynamic"})
        assert dyn_fail.ok is False and dyn_fail.data["error_code"] == "not_configured"

        # Legacy non-WBI fallback still returns real items on 404 from /wbi/
        rec_ok = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_items", "category": "recommend"})
        assert rec_ok.ok is True and rec_ok.data["count"] == 1

        # HTTP 401 -> unauthorized
        state.force_http_status = 401
        r401 = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_items", "category": "recommend"})
        assert r401.ok is False and r401.data["error_code"] == "unauthorized"

        # HTTP 412 -> rate_limited
        state.force_http_status = 412
        r412 = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_items", "category": "search", "query": "test"})
        assert r412.ok is False and r412.data["error_code"] == "rate_limited"

        # Timeout -> timeout (with config timeout=0.1s)
        state.force_http_status = None
        state.delay_sec = 0.4
        ctx.set_config("request_timeout_sec", 0.1)
        r_to = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_items", "category": "recommend"})
        assert r_to.ok is False and r_to.data["error_code"] == "timeout"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
