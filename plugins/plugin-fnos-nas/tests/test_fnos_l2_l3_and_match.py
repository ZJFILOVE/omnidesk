"""Offline L2/L3 http.server, clip_jump, local_match, book_sync, trust_env=False, and PluginManager tests."""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "core") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "core"))

from omnidesk_core import OmnideskPaths, PluginManager, PluginState  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_fnos_nas_net_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start_mock_server(handler_cls: type[http.server.BaseHTTPRequestHandler]) -> tuple[http.server.ThreadingHTTPServer, str, threading.Thread]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", thread


@pytest.mark.asyncio
async def test_local_match_and_no_forced_fuzzy_mismatch(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    nas = tmp_path / "nas"
    (nas / "Movies").mkdir(parents=True)
    low_br = nas / "Movies" / "Inception.2010.1080p.mkv"
    high_br = nas / "Movies" / "Inception.2010.2160p.REMUX.{tmdb-27205}.mkv"
    low_br.write_bytes(b"L" * 1000)
    high_br.write_bytes(b"H" * 8000)
    high_br.with_suffix(".nfo").write_text("<movie><imdbid>tt1375666</imdbid><tmdbid>27205</tmdbid></movie>", encoding="utf-8")

    (nas / "TV" / "Severance").mkdir(parents=True)
    ep = nas / "TV" / "Severance" / "Severance.S02E03.2160p.mkv"
    ep.write_bytes(b"E" * 4000)

    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
    ctx.set_config("mount_root", str(nas))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Exact IMDb match picks high bitrate REMUX.
    m_imdb = await ctx.capabilities.call("media.local_match.v1", {"imdb_id": "tt1375666"})
    assert m_imdb.ok is True and m_imdb.data["matched"] is True
    assert m_imdb.data["confidence"] == 1.0
    assert m_imdb.data["best_match"]["path"] == str(high_br.resolve())

    # Title match prefers high-bitrate candidate first.
    m_title = await ctx.capabilities.call("media.local_match.v1", {"title": "Inception", "year": 2010})
    assert m_title.ok is True and m_title.data["matched"] is True
    assert len(m_title.data["candidates"]) == 2
    assert m_title.data["best_match"]["path"] == str(high_br.resolve())

    # Episode match works with season + episode.
    m_ep = await ctx.capabilities.call("media.local_match.v1", {"title": "Severance", "season": 2, "episode": 3})
    assert m_ep.ok is True and m_ep.data["matched"] is True
    assert m_ep.data["best_match"]["season"] == 2 and m_ep.data["best_match"]["episode"] == 3

    # Wrong episode, wrong year, unknown ID, or different title must NOT force a match.
    for bad_q in (
        {"title": "Severance", "season": 2, "episode": 9},
        {"title": "Interstellar"},
        {"title": "Dune"},
        {"title": "Inception", "year": 1995},
        {"imdb_id": "tt9999999"},
    ):
        no_hit = await ctx.capabilities.call("media.local_match.v1", bad_q)
        assert no_hit.ok is True
        assert no_hit.data["matched"] is False
        assert no_hit.data["candidates"] == []
        assert no_hit.data["confidence"] == 0.0

    # Conflicting imdb_id rejects the IMDb-tagged high-bitrate file explicitly.
    conflict_hit = await ctx.capabilities.call("media.local_match.v1", {"title": "Inception", "imdb_id": "tt9999999"})
    assert conflict_hit.ok is True
    assert all(c["path"] != str(high_br.resolve()) for c in conflict_hit.data["candidates"])

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_clip_jump_search_bus_open_and_empty_when_no_subtitles(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    empty_nas = tmp_path / "empty_nas"
    empty_nas.mkdir()
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
    ctx.set_config("mount_root", str(empty_nas))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    no_sub_res = await ctx.capabilities.call("media.clip_jump.v1", {"query": "ephemeral"})
    assert no_sub_res.ok is True
    assert no_sub_res.data["matched"] is False
    assert no_sub_res.data["clips"] == []

    nas = tmp_path / "nas"
    (nas / "TV" / "Severance").mkdir(parents=True)
    vid = nas / "TV" / "Severance" / "Severance.S02E03.mkv"
    vid.write_bytes(b"V" * 1024)
    srt = nas / "TV" / "Severance" / "Severance.S02E03.zh-en.srt"
    srt.write_text(
        "\n".join([
            "1",
            "00:14:02,500 --> 00:14:05,800",
            "The arrangement feels deliberately ephemeral, like chalk on wet slate.",
            "这项安排刻意显得转瞬即逝，就像湿石板上的粉笔字。",
            "",
            "2",
            "00:15:10,000 --> 00:15:12,000",
            "She broke the silence.",
            "她打破了沉默。",
        ]),
        encoding="utf-8",
    )
    ctx.set_config("mount_root", str(nas))

    bus_events: list[dict[str, Any]] = []
    ctx.subscribe("entity.media.open", lambda evt: bus_events.append(dict(evt.payload)))

    hit = await ctx.capabilities.call("media.clip_jump.v1", {"query": "ephemeral", "auto_open_player": True})
    assert hit.ok is True and hit.data["matched"] is True and hit.data["count"] == 1
    clip = hit.data["clips"][0]
    assert clip["start_sec"] == 842.5
    assert clip["play_url"] == vid.resolve().as_uri()
    assert clip["timestamp_label"] == "00:14:02"
    assert "ephemeral" in clip["en_text"].lower()
    assert "转瞬即逝" in clip["zh_text"]
    assert hit.data["bus_published"] is True
    assert len(bus_events) == 1
    assert bus_events[0]["play_url"] == vid.resolve().as_uri()
    assert bus_events[0]["start_sec"] == 842.5

    # Phrase match + caller manual bus publish.
    phrase = await ctx.capabilities.call("media.clip_jump.v1", {"query": "wet slate"})
    assert phrase.ok is True and phrase.data["count"] == 1
    await ctx.bus.publish("entity.media.open", phrase.data["clips"][0]["open_payload"], source_plugin_id="plugin-ebook")
    assert len(bus_events) == 2

    # Unmatched word returns empty.
    miss = await ctx.capabilities.call("media.clip_jump.v1", {"query": "quintessential"})
    assert miss.ok is True and miss.data["matched"] is False and miss.data["clips"] == []

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_l2_l3_http_trust_env_false_redirects_401_timeout_and_book_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    # Spy on urllib.request.ProxyHandler and inject hostile proxy env vars to prove trust_env=False.
    proxy_calls: list[Any] = []
    real_proxy_handler = urllib.request.ProxyHandler

    class _SpyProxyHandler(real_proxy_handler):  # type: ignore[misc, valid-type]
        def __init__(self, proxies: Any = None) -> None:
            proxy_calls.append(proxies)
            super().__init__(proxies)

    monkeypatch.setattr(urllib.request, "ProxyHandler", _SpyProxyHandler)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")

    state: dict[str, Any] = {"mode": "ok", "auth_headers": [], "api_keys": [], "redirect_hit": False}

    class _MockHandler(http.server.BaseHTTPRequestHandler):
        def _send_json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/redirect-target"):
                state["redirect_hit"] = True
                self._send_json(200, {"leaked": True})
                return
            if state["mode"] == "redirect":
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
                return
            if state["mode"] == "timeout":
                time.sleep(0.45)
            if state["mode"] == "401":
                self._send_json(401, {"error": "unauthorized"})
                return
            if self.path.startswith("/subtitles/search"):
                state["auth_headers"].append(self.headers.get("Authorization", ""))
                self._send_json(200, {"clips": [{"title": "Remote Show", "dialogue": "ephemeral moment", "start_sec": 12.0, "play_url": "file:///remote/show.mkv"}]})
                return
            if self.path == "/api/v3/movie":
                state["api_keys"].append(self.headers.get("X-Api-Key", ""))
                self._send_json(200, [{"id": 7, "title": "Oppenheimer", "year": 2023, "imdbId": "tt15398776", "tmdbId": 872585, "hasFile": True, "movieFile": {"path": "/media/Movies/Oppenheimer.mkv", "size": 9000, "quality": {"quality": {"name": "Remux-2160p"}}}}])
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if state["mode"] == "401":
                self._send_json(401, {"error": "unauthorized"})
                return
            if state["mode"] == "timeout":
                time.sleep(0.45)
            state["auth_headers"].append(self.headers.get("Authorization", ""))
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            self._send_json(200, {"ok": True, "progress": payload.get("progress", []), "vocab": [{"word": "lucid", "meaning": "clear", "updated_at": 9999999999.0}]})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server, base_url, thread = _start_mock_server(_MockHandler)
    try:
        nas = tmp_path / "nas"
        nas.mkdir()
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
        ctx.set_config({
            "mount_root": str(nas),
            "media_service_url": base_url,
            "companion_sync_url": base_url,
            "radarr_url": base_url,
            "arr_remote_prefix": "/media",
            "http_timeout_sec": 1,
        })
        ctx.set_secret("media_service_token", "sec-media-tok")
        ctx.set_secret("companion_sync_token", "sec-sync-tok")
        ctx.set_secret("radarr_api_key", "sec-radarr-key")
        # Confirm secrets are never stored in plain config.
        raw_cfg = json.dumps(ctx.get_config())
        assert "sec-media-tok" not in raw_cfg
        assert "sec-radarr-key" not in raw_cfg

        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # L3 Radarr match + prefix rewrite + proxy bypass verification.
        arr_res = await ctx.capabilities.call("media.local_match.v1", {"title": "Oppenheimer"})
        assert arr_res.ok is True and arr_res.data["matched"] is True
        assert arr_res.data["best_match"]["path"] == str((nas / "Movies" / "Oppenheimer.mkv").resolve())
        assert "sec-radarr-key" in state["api_keys"]
        assert proxy_calls and all(p == {} for p in proxy_calls)

        # L2 clip_jump remote search.
        cj_res = await ctx.capabilities.call("media.clip_jump.v1", {"query": "ephemeral"})
        assert cj_res.ok is True and cj_res.data["count"] == 1
        assert "Bearer sec-media-tok" in state["auth_headers"]

        # L2 book.sync.v1 push + LWW merge from server.
        bs_res = await ctx.capabilities.call("book.sync.v1", {"action": "sync", "book_id": "book-1", "current_page": 42, "total_pages": 200})
        assert bs_res.ok is True and bs_res.data["remote_synced"] is True
        assert "lucid" in bs_res.data["vocab"]
        assert "Bearer sec-sync-tok" in state["auth_headers"]

        # Redirect must NOT be followed (follow_redirects=False).
        state["mode"] = "redirect"
        redir_res = await ctx.capabilities.call("media.clip_jump.v1", {"query": "ephemeral"})
        assert redir_res.ok is False and state["redirect_hit"] is False

        # HTTP 401 mapped cleanly to permission_denied without leaking secret.
        state["mode"] = "401"
        auth_err = await ctx.capabilities.call("media.local_match.v1", {"title": "Oppenheimer"})
        assert auth_err.ok is False and auth_err.data["error_code"] == "permission_denied"
        assert auth_err.data["http_status"] == 401
        assert "sec-radarr-key" not in auth_err.error

        # Timeout mapped cleanly to error_code='timeout'.
        state["mode"] = "timeout"
        plugin._http_client = mod.lan_http.LanHttpClient(trust_env=False, follow_redirects=False)
        monkeypatch.setattr(plugin, "_int_cfg", lambda k, d=10: 0 if k == "http_timeout_sec" else d)
        to_res = await ctx.capabilities.call("book.sync.v1", {"action": "sync", "book_id": "book-1", "current_page": 43})
        assert to_res.ok is False and to_res.data["error_code"] == "timeout"
        assert to_res.data["local_saved"] is True

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    # Unreachable closed port maps to service_offline.
    ctx_off = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data_off", config_dir=tmp_path / "cfg_off", config_schema=manifest["config_schema"])
    ctx_off.set_config("companion_sync_url", base_url)
    p_off = mod.create_plugin()
    await invoke_lifecycle_hook(p_off.on_load, ctx_off)
    off_res = await ctx_off.capabilities.call("book.sync.v1", {"action": "sync", "book_id": "b2", "current_page": 1})
    assert off_res.ok is False and off_res.data["error_code"] == "service_offline"
    assert off_res.data["local_saved"] is True
    await invoke_lifecycle_hook(p_off.on_unload)
    await ctx_off.acleanup()


@pytest.mark.asyncio
async def test_core_plugin_manager_load_reload_unload_isolation(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env({
        "XDG_CONFIG_HOME": str(tmp_path / "xdg_cfg"),
        "XDG_DATA_HOME": str(tmp_path / "xdg_data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg_run"),
    })
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    rec = await mgr.load_plugin(PLUGIN_DIR)
    assert rec.state == PluginState.LOADED
    assert mgr.capabilities.has("nas.connection.v1")
    assert len(mgr.domains.list_media_sources(plugin_id="plugin-fnos-nas")) == 1

    reloaded = await mgr.reload_plugin("plugin-fnos-nas")
    assert reloaded.state == PluginState.LOADED
    assert reloaded.reload_count == 1

    await mgr.unload_plugin("plugin-fnos-nas")
    assert not mgr.capabilities.has("nas.connection.v1")
    assert mgr.domains.list_media_sources(plugin_id="plugin-fnos-nas") == []
