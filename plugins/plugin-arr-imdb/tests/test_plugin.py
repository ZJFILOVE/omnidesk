"""Offline unit, security, fail-closed, and lifecycle tests for plugin-arr-imdb."""

from __future__ import annotations

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
for candidate in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR / "tests")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from mock_servers import MockServerState, RunningMockServer  # noqa: E402
from omnidesk_core import OmnideskPaths, PluginManager, PluginState  # noqa: E402
from omnidesk_sdk import BasePlugin, CapabilityResult, MediaItem, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_arr_imdb_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_ctx(tmp_path: Path) -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )


def test_manifest_matches_v2_schema_and_has_no_hardcoded_secrets_or_ips() -> None:
    manifest_text = (PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-arr-imdb"
    assert manifest["kind"] == "workspace"
    assert manifest["domain"] == "media"
    assert manifest["provides"] == ["media.source.v1"]
    assert manifest["consumes"] == ["media.local_match.v1"]
    assert set(manifest.keys()).issubset(set(schema["properties"].keys()))
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in manifest["provides"] + manifest["consumes"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    for key in ("imdb_api_base", "tmdb_api_base", "radarr_url", "sonarr_url", "imdb_api_key", "tmdb_api_key", "radarr_api_key", "sonarr_api_key", "quality_profile", "dry_run"):
        assert key in fields
    for secret_key in ("imdb_api_key", "tmdb_api_key", "radarr_api_key", "sonarr_api_key"):
        assert fields[secret_key]["type"] == "secret"
        assert fields[secret_key]["default"] == ""
    assert fields["dry_run"]["type"] == "bool" and fields["dry_run"]["default"] is True
    assert fields["radarr_url"]["default"] == ""
    assert fields["sonarr_url"]["default"] == ""
    assert not re.search(r"192\.168\.|10\.\d+\.|172\.(?:1[6-9]|2\d|3[0-1])\.", manifest_text)


@pytest.mark.asyncio
async def test_mediasource_categories_fetch_items_empty_and_detail_not_found(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    assert isinstance(plugin, BasePlugin)
    ctx = _make_ctx(tmp_path)
    imdb_state = MockServerState(expected_api_key="imdb-secret-token")

    with RunningMockServer("imdb", imdb_state) as imdb_srv:
        ctx.set_config({"imdb_api_base": imdb_srv.base_url, "imdb_provider": "imdb"})
        ctx.set_secret("imdb_api_key", "imdb-secret-token")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        sources = ctx.domains.media_sources()
        assert len(sources) == 1
        src = sources[0]
        cats = src.categories()
        assert {"popular", "top_rated", "upcoming", "search"}.issubset(set(cats))

        items = await src.fetch_items("popular")
        assert len(items) == 1
        item = items[0]
        assert isinstance(item, MediaItem)
        assert item.id == "tt1375666"
        assert item.title == "Inception"
        assert item.extra["imdb_id"] == "tt1375666"
        assert item.extra["tmdb_id"] == 27205
        assert item.extra["year"] == 2010
        assert item.extra["rating"] == 8.8
        assert item.extra["genres"] == ["Action", "Sci-Fi"]
        assert item.cover_url == "https://posters.example/inception.jpg"

        # Empty search result returns []
        empty_items = await src.fetch_items("search", query="nonexistent")
        assert empty_items == []
        assert await src.fetch_items("search", query="") == []

        imdb_state.mode = "empty"
        assert await src.fetch_items("top_rated") == []
        imdb_state.mode = "ok"

        # Detail found vs unknown ID not_found
        detail_res = await ctx.capabilities.call("media.source.v1", {"action": "fetch_detail", "item_id": "tt1375666"})
        assert detail_res.ok is True
        assert detail_res.data["directors"] == ["Christopher Nolan"]
        assert detail_res.data["cast"] == ["Leonardo DiCaprio", "Joseph Gordon-Levitt"]
        assert detail_res.data["runtime_minutes"] == 148
        assert detail_res.data["genres"] == ["Action", "Sci-Fi", "Thriller"]
        assert detail_res.data["rating"] == 8.8
        assert "dream-sharing" in detail_res.data["overview"]

        missing_res = await ctx.capabilities.call("media.source.v1", {"action": "fetch_detail", "item_id": "tt0000000"})
        assert missing_res.ok is False
        assert missing_res.data["error_code"] == "not_found"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_tmdb_mode_and_local_status_present_missing_unavailable(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)
    tmdb_state = MockServerState(expected_api_key="tmdb-v3-secret-key")

    with RunningMockServer("tmdb", tmdb_state) as tmdb_srv:
        ctx.set_config({"tmdb_api_base": tmdb_srv.base_url, "imdb_provider": "tmdb"})
        ctx.set_secret("tmdb_api_key", "tmdb-v3-secret-key")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        list_res = await ctx.capabilities.call("media.source.v1", {"action": "fetch_items", "category": "popular"})
        assert list_res.ok is True
        assert list_res.data["items"][0]["title"] == "Interstellar"
        assert list_res.data["items"][0]["extra"]["tmdb_id"] == 157336
        assert list_res.data["items"][0]["extra"]["genres"] == ["Adventure", "Drama", "Science Fiction"]

        det_res = await ctx.capabilities.call("media.source.v1", {"action": "fetch_detail", "item_id": "tt0816692"})
        assert det_res.ok is True
        assert det_res.data["directors"] == ["Christopher Nolan"]
        assert det_res.data["cast"] == ["Matthew McConaughey", "Anne Hathaway"]
        assert det_res.data["runtime_minutes"] == 169

        nf_res = await ctx.capabilities.call("media.source.v1", {"action": "fetch_detail", "item_id": "tt9999999"})
        assert nf_res.ok is False
        assert nf_res.data["error_code"] == "not_found"

        # 1. local_match missing capability -> capability_unavailable (never assumes exists)
        unavail = await ctx.capabilities.call("media.source.v1", {"action": "local_status", "imdb_id": "tt0816692", "title": "Interstellar"})
        assert unavail.ok is False
        assert unavail.data["error_code"] == "capability_unavailable"
        assert unavail.data["exists"] is False

        # 2. local_match present vs missing
        received_queries: list[dict[str, Any]] = []

        async def _fake_local_match(req: dict[str, Any]) -> CapabilityResult:
            received_queries.append(dict(req))
            if req.get("imdb_id") == "tt0903747" and req.get("season") == 1 and req.get("episode") == 2:
                return CapabilityResult.ok({"matched": True, "files": ["/nas/Breaking.Bad.S01E02.mkv"]})
            return CapabilityResult.ok({"available": True, "matched": False, "matches": []})

        ctx.capabilities.register("media.local_match.v1", _fake_local_match, plugin_id="plugin-fnos-nas")

        present = await ctx.capabilities.call(
            "media.source.v1",
            {"action": "local_status", "imdb_id": "tt0903747", "title": "Breaking Bad", "season": 1, "episode": 2},
        )
        assert present.ok is True
        assert present.data["status"] == "present"
        assert present.data["exists"] is True
        assert received_queries[-1]["season"] == 1 and received_queries[-1]["episode"] == 2

        missing = await ctx.capabilities.call("media.source.v1", {"action": "local_status", "imdb_id": "tt0816692", "title": "Interstellar"})
        assert missing.ok is True
        assert missing.data["status"] == "missing"
        assert missing.data["exists"] is False

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_radarr_and_sonarr_dry_run_confirm_duplicate_queue_and_errors(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)
    radarr_state = MockServerState(expected_api_key="radarr-secret-xyz")
    sonarr_state = MockServerState(expected_api_key="sonarr-secret-abc")

    with RunningMockServer("radarr", radarr_state) as rad_srv, RunningMockServer("sonarr", sonarr_state) as son_srv:
        ctx.set_config({"radarr_url": rad_srv.base_url, "sonarr_url": son_srv.base_url, "quality_profile": "HD-1080p", "dry_run": True})
        ctx.set_secret("radarr_api_key", "radarr-secret-xyz")
        ctx.set_secret("sonarr_api_key", "sonarr-secret-abc")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Lookup
        rlookup = await ctx.capabilities.call("media.source.v1", {"action": "radarr_lookup", "imdb_id": "tt1375666"})
        assert rlookup.ok is True and rlookup.data["results"][0]["tmdb_id"] == 27205
        slookup = await ctx.capabilities.call("media.source.v1", {"action": "sonarr_lookup", "tvdb_id": 81189})
        assert slookup.ok is True and slookup.data["results"][0]["tvdb_id"] == 81189

        # Dry-run default & dry_run=False without confirm=True MUST NOT POST
        rdry = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "imdb_id": "tt1375666"})
        assert rdry.ok is True and rdry.data["dry_run"] is True and rdry.data["submitted"] is False
        rdry2 = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "imdb_id": "tt1375666", "dry_run": False})
        assert rdry2.ok is True and rdry2.data["dry_run"] is True and rdry2.data["submitted"] is False
        sdry = await ctx.capabilities.call("media.source.v1", {"action": "sonarr_add", "tvdb_id": 81189, "confirm": False})
        assert sdry.ok is True and sdry.data["dry_run"] is True and sdry.data["submitted"] is False
        assert not any(m == "POST" for m, *_ in radarr_state.requests)
        assert not any(m == "POST" for m, *_ in sonarr_state.requests)

        # Explicit confirm=True submits POST
        radd = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "imdb_id": "tt1375666", "confirm": True})
        assert radd.ok is True and radd.data["submitted"] is True and radd.data["item"]["id"] == 101
        sadd = await ctx.capabilities.call("media.source.v1", {"action": "sonarr_add", "tvdb_id": 81189, "confirm": True})
        assert sadd.ok is True and sadd.data["submitted"] is True and sadd.data["item"]["id"] == 202

        # Duplicate detection (both library match and 409 / 400 validator)
        rdup = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "imdb_id": "tt1375666", "confirm": True})
        assert rdup.ok is False and rdup.data["error_code"] == "conflict" and rdup.data["duplicate"] is True

        radarr_state.existing_movies.clear()
        radarr_state.mode = "conflict_post"
        r409 = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "tmdb_id": 27205, "title": "Inception", "confirm": True})
        assert r409.ok is False and r409.data["error_code"] == "conflict"
        radarr_state.mode = "conflict_400"
        r400 = await ctx.capabilities.call("media.source.v1", {"action": "radarr_add", "tmdb_id": 27205, "title": "Inception", "confirm": True})
        assert r400.ok is False and r400.data["error_code"] == "conflict"
        radarr_state.mode = "ok"

        # Queue parsing
        rq = await ctx.capabilities.call("media.source.v1", {"action": "radarr_queue"})
        assert rq.ok is True and rq.data["total_records"] == 1
        assert rq.data["records"][0]["progress_pct"] == 75.0
        assert rq.data["records"][0]["movie_title"] == "Inception"
        sq = await ctx.capabilities.call("media.source.v1", {"action": "sonarr_queue"})
        assert sq.ok is True and sq.data["records"][0]["progress_pct"] == 80.0
        assert sq.data["records"][0]["season_number"] == 1 and sq.data["records"][0]["episode_number"] == 1

        # 401 redacts secret
        radarr_state.mode = "unauthorized"
        unauth = await ctx.capabilities.call("media.source.v1", {"action": "radarr_queue"})
        assert unauth.ok is False and unauth.data["error_code"] == "unauthorized"
        assert "radarr-secret-xyz" not in unauth.error
        assert "radarr-secret-xyz" not in json.dumps(unauth.data)
        radarr_state.mode = "ok"

        # Timeout
        ctx.set_config("request_timeout_sec", 0.2)
        sonarr_state.delay_sec = 0.6
        tout = await ctx.capabilities.call("media.source.v1", {"action": "sonarr_queue"})
        assert tout.ok is False and tout.data["error_code"] == "timeout"
        sonarr_state.delay_sec = 0.0

        # Unreachable
        ctx.set_config("radarr_url", f"http://127.0.0.1:{_unused_local_port()}")
        offline = await ctx.capabilities.call("media.source.v1", {"action": "radarr_queue"})
        assert offline.ok is False and offline.data["error_code"] == "service_offline"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_security_proxy_bypass_no_redirect_path_traversal_and_secret_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)
    radarr_state = MockServerState(expected_api_key="vault-only-key-999")
    dead_proxy = f"http://127.0.0.1:{_unused_local_port()}"
    monkeypatch.setenv("HTTP_PROXY", dead_proxy)
    monkeypatch.setenv("HTTPS_PROXY", dead_proxy)
    monkeypatch.setenv("ALL_PROXY", dead_proxy)

    with RunningMockServer("radarr", radarr_state) as rad_srv:
        ctx.set_config({"radarr_url": rad_srv.base_url, "imdb_api_base": rad_srv.base_url})
        ctx.set_secret("radarr_api_key", "vault-only-key-999")
        assert "radarr_api_key" not in ctx.get_config()
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # trust_env=False: succeeds despite dead HTTP_PROXY env var
        q_ok = await ctx.capabilities.call("media.source.v1", {"action": "radarr_queue"})
        assert q_ok.ok is True

        # follow_redirects=False: 302 redirect is NOT followed
        radarr_state.mode = "redirect"
        radarr_state.redirect_target = f"{rad_srv.base_url}/redirect-trap"
        redir = await ctx.capabilities.call("media.source.v1", {"action": "radarr_queue"})
        assert redir.ok is False and redir.data["error_code"] == "redirect_blocked"
        assert radarr_state.trap_hits == 0
        radarr_state.mode = "ok"

        # Non-http/https URLs rejected
        ctx.set_config("radarr_url", "file:///etc/passwd")
        bad_scheme = await ctx.capabilities.call("media.source.v1", {"action": "radarr_queue"})
        assert bad_scheme.ok is False and bad_scheme.data["error_code"] == "invalid_argument"

        ctx.set_config("radarr_url", rad_srv.base_url)
        # Path traversal in item_id and root_folder_path rejected without hitting upstream
        before_reqs = len(radarr_state.requests)
        trav_id = await ctx.capabilities.call("media.source.v1", {"action": "fetch_detail", "item_id": "../../etc/passwd"})
        assert trav_id.ok is False and trav_id.data["error_code"] == "path_safety"
        trav_path = await ctx.capabilities.call(
            "media.source.v1",
            {"action": "radarr_add", "tmdb_id": 27205, "title": "Inception", "root_folder_path": "/movies/../etc", "confirm": True},
        )
        assert trav_path.ok is False and trav_path.data["error_code"] == "path_safety"
        assert len(radarr_state.requests) == before_reqs

        # Workspace tab action + secret redaction
        tab = ctx.domains.workspace_tabs("media")[0]
        assert tab.on_action is not None
        tab_res = await tab.on_action("radarr_queue", {})
        assert tab_res["ok"] is True
        assert "vault-only-key-999" not in json.dumps(tab_res)
        assert "vault-only-key-999" not in json.dumps(await invoke_lifecycle_hook(plugin.on_health_check))

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_plugin_manager_load_reload_unload_cleanup_and_conflict_fallback(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    rec = await mgr.load_plugin("plugin-arr-imdb")
    assert rec.state == PluginState.LOADED
    assert (await mgr.health_check("plugin-arr-imdb"))["ok"] is True
    assert mgr.domains.has("media", "arr-imdb")
    assert len(mgr.workspace_tabs("media")) == 1
    assert mgr.capabilities.has("media.source.v1")

    reloaded = await mgr.reload_plugin("plugin-arr-imdb")
    assert reloaded.state == PluginState.LOADED
    assert reloaded.reload_count == 1

    unloaded = await mgr.unload_plugin("plugin-arr-imdb")
    assert unloaded.state == PluginState.UNLOADED
    assert not mgr.domains.has("media", "arr-imdb")
    assert mgr.workspace_tabs("media") == []
    assert not mgr.capabilities.has("media.source.v1")

    # Even if another plugin already owns capability media.source.v1 in core CapabilityRegistry,
    # plugin-arr-imdb still loads its MediaSource without faulting.
    mgr.capabilities.register("media.source.v1", lambda req: CapabilityResult.ok({}), plugin_id="plugin-other")
    rec2 = await mgr.load_plugin("plugin-arr-imdb")
    assert rec2.state == PluginState.LOADED
    assert mgr.domains.has("media", "arr-imdb")
    await mgr.unload_all()
