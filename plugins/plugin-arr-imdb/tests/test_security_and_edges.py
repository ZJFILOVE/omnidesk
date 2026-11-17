"""Edge-case, fail-closed, URL validation, and ControlServer RPC tests for plugin-arr-imdb."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for candidate in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR), str(PLUGIN_DIR / "tests")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from mock_servers import MockServerState, RunningMockServer  # noqa: E402
from omnidesk_core import ControlClient, ControlServer, OmnideskPaths, PluginManager  # noqa: E402
from omnidesk_sdk import CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_arr_imdb_edges_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_http_client_module() -> Any:
    spec = importlib.util.spec_from_file_location("http_client_edges_test", PLUGIN_DIR / "http_client.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_ctx(tmp_path: Path) -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )


def test_url_validation_allows_explicit_lan_and_rejects_unsafe_schemes() -> None:
    hc = _load_http_client_module()
    assert hc.validate_http_url("http://192.168.50.10:7878/", label="radarr_url") == "http://192.168.50.10:7878"
    assert hc.validate_http_url("https://arr.local/radarr", label="radarr_url") == "https://arr.local/radarr"
    for bad in ("file:///etc/passwd", "ftp://192.168.1.2/movies", "javascript:alert(1)", "http://user:pass@127.0.0.1:7878", "http://127.0.0.1\nX: y"):
        with pytest.raises(hc.UpstreamClientError) as exc:
            hc.validate_http_url(bad, label="radarr_url")
        assert exc.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_mediasource_fail_closed_and_category_aliases(tmp_path: Path) -> None:
    module = _load_plugin_module()
    err_cls = module.UpstreamClientError
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    src = ctx.domains.media_sources()[0]

    # Unconfigured MediaSource fails closed (never fakes charts)
    with pytest.raises(err_cls) as unconf_exc:
        await src.fetch_items("popular")
    assert unconf_exc.value.code == "not_configured"

    imdb_state = MockServerState(expected_api_key="sec-imdb-key")
    with RunningMockServer("imdb", imdb_state) as srv:
        ctx.set_config({"imdb_api_base": srv.base_url, "imdb_provider": "imdb", "request_timeout_sec": 0.2})
        ctx.set_secret("imdb_api_key", "sec-imdb-key")

        # Chinese category aliases
        for alias in ("热门", "高分", "即将上映"):
            items = await src.fetch_items(alias)
            assert len(items) == 1 and items[0].id == "tt1375666"
        search_items = await src.fetch_items("搜索", query="Breaking")
        assert len(search_items) == 1 and search_items[0].id == "tt0903747"

        # Invalid category fails closed
        with pytest.raises(err_cls) as cat_exc:
            await src.fetch_items("nonexistent-category")
        assert cat_exc.value.code == "invalid_argument"

        # 401, 502, and timeout fail closed on both fetch_items and fetch_detail
        imdb_state.mode = "unauthorized"
        with pytest.raises(err_cls) as auth_exc:
            await src.fetch_items("popular")
        assert auth_exc.value.code == "unauthorized"
        det_auth = await ctx.capabilities.call("media.source.v1", {"action": "fetch_detail", "item_id": "tt1375666"})
        assert det_auth.ok is False and det_auth.data["error_code"] == "unauthorized"
        assert "sec-imdb-key" not in det_auth.error

        imdb_state.mode = "server_error"
        with pytest.raises(err_cls) as srv_exc:
            await src.fetch_items("top_rated")
        assert srv_exc.value.code == "upstream_error"

        imdb_state.mode = "ok"
        imdb_state.delay_sec = 0.6
        with pytest.raises(err_cls) as to_exc:
            await src.fetch_items("upcoming")
        assert to_exc.value.code == "timeout"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_local_status_propagates_failure_and_arr_confirm_validation(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)
    radarr_state = MockServerState(expected_api_key="radarr-k-1")

    with RunningMockServer("radarr", radarr_state) as rad_srv:
        ctx.set_config({"radarr_url": rad_srv.base_url, "quality_profile": "HD-1080p"})
        ctx.set_secret("radarr_api_key", "radarr-k-1")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # local_match fnos-nas candidate shape passes through matches/best_match/confidence
        ctx.capabilities.register(
            "media.local_match.v1",
            lambda req: CapabilityResult.ok({
                "matched": True,
                "best_match": {"item_id": "nas-1", "path": "/nas/Inception.mkv", "confidence": 1.0},
                "candidates": [{"item_id": "nas-1", "path": "/nas/Inception.mkv", "confidence": 1.0}],
                "confidence": 1.0,
            }),
            plugin_id="plugin-fnos-nas",
        )
        fnos_hit = await ctx.capabilities.call(
            "media.source.v1",
            {"action": "local_status", "imdb_id": "tt1375666", "title": "Inception", "year": 2010},
        )
        assert fnos_hit.ok is True and fnos_hit.data["exists"] is True
        assert fnos_hit.data["best_match"]["item_id"] == "nas-1"
        assert len(fnos_hit.data["matches"]) == 1
        assert fnos_hit.data["query"]["year"] == 2010

        # local_match failure propagates fail-closed (does not claim missing or present)
        ctx.capabilities.register(
            "media.local_match.v1",
            lambda req: CapabilityResult.fail("NAS disconnected", error_code="service_offline"),
            plugin_id="plugin-fnos-nas",
        )
        local_err = await ctx.capabilities.call("media.source.v1", {"action": "local_status", "imdb_id": "tt1375666"})
        assert local_err.ok is False and local_err.data["error_code"] == "service_offline"
        assert local_err.data["exists"] is False

        bad_season = await ctx.capabilities.call("media.source.v1", {"action": "local_status", "title": "Lost", "season": -1})
        assert bad_season.ok is False and bad_season.data["error_code"] == "invalid_argument"

        # String "confirm": "true" rejected fail-closed; confirm=True + dry_run=True never POSTs
        bad_confirm = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "imdb_id": "tt1375666", "confirm": "true"})
        assert bad_confirm.ok is False and bad_confirm.data["error_code"] == "invalid_argument"
        dry_override = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "imdb_id": "tt1375666", "confirm": True, "dry_run": True})
        assert dry_override.ok is True and dry_override.data["submitted"] is False
        assert not any(m == "POST" for m, *_ in radarr_state.requests)

        # Missing profile name fails closed instead of guessing
        bad_prof = await ctx.capabilities.call(
            "media.source.v1",
            {"action": "radarr_add", "imdb_id": "tt1375666", "quality_profile": "DoesNotExist", "confirm": True},
        )
        assert bad_prof.ok is False and bad_prof.data["error_code"] == "not_found"

        # Valid confirm=True resolves HD-1080p -> qualityProfileId == 4 and rootFolderPath == "/movies"
        ok_add = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "imdb_id": "tt1375666", "confirm": True})
        assert ok_add.ok is True and ok_add.data["submitted"] is True
        post_reqs = [body for m, _, _, body in radarr_state.requests if m == "POST"]
        assert len(post_reqs) == 1
        assert post_reqs[0]["qualityProfileId"] == 4
        assert post_reqs[0]["rootFolderPath"] == "/movies"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_control_socket_rpc_end_to_end(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    await mgr.load_plugin("plugin-arr-imdb")
    sock_path = tmp_path / "run" / "control.sock"

    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr):
        client = ControlClient(socket_path=sock_path, paths=paths)
        cats = await client.call_capability("media.source.v1", {"action": "categories"})
        assert cats.ok is True
        assert "popular" in cats.data["categories"]

        unavail = await client.call_capability("media.source.v1", {"action": "local_status", "imdb_id": "tt1375666"})
        assert unavail.ok is False
        assert unavail.data["error_code"] == "capability_unavailable"

    await mgr.unload_all()
