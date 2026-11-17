"""Additional offline edge-case tests: list_roots, tmdb_id, Sonarr v3, LWW sync, file size limits, and guards."""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import lan_http  # noqa: E402
import safety  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_fnos_nas_edge_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_connection_status_list_roots_media_rpc_and_size_limits(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    nas = tmp_path / "nas"
    (nas / "Movies").mkdir(parents=True)
    (nas / "TV" / "Severance").mkdir(parents=True)
    m_path = nas / "Movies" / "1917.2019.2160p.{tmdb-530915}.mkv"
    m_path.write_bytes(b"X" * 2048)
    (nas / "TV" / "Severance" / "Severance.S01E01.mkv").write_bytes(b"Y" * 1024)
    # Oversized SRT (> 8 MiB) must be safely skipped by bounded file reading.
    huge_srt = nas / "TV" / "Severance" / "Severance.S01E01.srt"
    huge_srt.write_bytes(b"0" * (safety.MAX_SUBTITLE_BYTES + 64))

    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
    ctx.set_config("mount_root", str(nas))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    status_res = await ctx.capabilities.call("nas.connection.v1", {"action": "status"})
    assert status_res.ok is True
    assert status_res.data["online"] is True
    assert status_res.data["readable"] is True

    roots_res = await ctx.capabilities.call("nas.connection.v1", {"action": "list_roots"})
    assert roots_res.ok is True
    root_cats = {r["id"]: r["category"] for r in roots_res.data["roots"]}
    assert root_cats["Movies"] == "电影"
    assert root_cats["TV"] == "剧集"

    # media.source.v1 RPC wrapper works for categories & fetch_items(format, query, page).
    cats_rpc = await ctx.capabilities.call("media.source.v1", {"action": "categories"})
    assert cats_rpc.ok is True and cats_rpc.data["categories"] == ["电影", "剧集"]

    items_rpc = await ctx.capabilities.call("media.source.v1", {"action": "fetch_items", "category": "电影", "query": "1917", "format": "mkv", "page": 1})
    assert items_rpc.ok is True and items_rpc.data["total"] == 1
    assert items_rpc.data["items"][0]["extra"]["tmdb_id"] == "530915"

    # tmdb_id + year-named movie ("1917") match accurately.
    tmdb_match = await ctx.capabilities.call("media.local_match.v1", {"tmdb_id": "530915"})
    assert tmdb_match.ok is True and tmdb_match.data["matched"] is True and tmdb_match.data["confidence"] == 0.98
    title_1917 = await ctx.capabilities.call("media.local_match.v1", {"title": "1917", "year": 2019})
    assert title_1917.ok is True and title_1917.data["matched"] is True

    # Oversized SRT is safely skipped without error or OOM.
    clip_res = await ctx.capabilities.call("media.clip_jump.v1", {"query": "ephemeral"})
    assert clip_res.ok is True and clip_res.data["clips"] == []
    with pytest.raises(safety.MountValidationError) as exc_info:
        safety.read_text_bounded(huge_srt, safety.MAX_SUBTITLE_BYTES, root_guard=nas)
    assert exc_info.value.error_code == "invalid_argument"

    # Non-subtitle extensions rejected in write_subtitle.
    bad_ext = await ctx.capabilities.call("nas.connection.v1", {"action": "write_subtitle", "target_path": "TV/evil.py", "content": "print(1)"})
    assert bad_ext.ok is False and bad_ext.data["error_code"] == "invalid_argument"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_l3_sonarr_match_lww_local_book_sync_and_client_guards(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        lan_http.LanHttpClient(trust_env=True)
    with pytest.raises(ValueError):
        lan_http.LanHttpClient(follow_redirects=True)

    sonarr_keys: list[str] = []

    class _SonarrHandler(http.server.BaseHTTPRequestHandler):
        def _send(self, obj: Any) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            sonarr_keys.append(self.headers.get("X-Api-Key", ""))
            if self.path == "/api/v3/series":
                self._send([{"id": 11, "title": "Severance", "year": 2022, "imdbId": "tt11280740", "tvdbId": 371980}])
            elif self.path == "/api/v3/episode?seriesId=11":
                self._send([{"id": 101, "seasonNumber": 2, "episodeNumber": 3, "hasFile": True, "episodeFileId": 501}])
            elif self.path == "/api/v3/episodefile?seriesId=11":
                self._send([{"id": 501, "path": "/media/TV/Severance/S02E03.mkv", "size": 7777, "quality": {"quality": {"name": "WEBDL-2160p"}}}])
            else:
                self._send([])

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SonarrHandler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mod = _load_plugin_module()
        plugin = mod.create_plugin()
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        nas = tmp_path / "nas"
        nas.mkdir()
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
        ctx.set_config({"mount_root": str(nas), "sonarr_url": url, "arr_remote_prefix": "/media"})
        ctx.set_secret("sonarr_api_key", "sonarr-secret-xyz")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        sonarr_res = await ctx.capabilities.call("media.local_match.v1", {"title": "Severance", "season": 2, "episode": 3})
        assert sonarr_res.ok is True and sonarr_res.data["matched"] is True
        assert sonarr_res.data["best_match"]["match_source"] == "sonarr"
        assert sonarr_res.data["best_match"]["path"] == str((nas / "TV" / "Severance" / "S02E03.mkv").resolve())
        assert "sonarr-secret-xyz" in sonarr_keys

        # Local-first LWW book sync conflict resolution: newer timestamp wins, older timestamp ignored.
        await ctx.capabilities.call("book.sync.v1", {"action": "save_local", "book_id": "b1", "current_page": 25, "updated_at": 200.0})
        await ctx.capabilities.call("book.sync.v1", {"action": "save_local", "book_id": "b1", "current_page": 10, "updated_at": 100.0})
        got = await ctx.capabilities.call("book.sync.v1", {"action": "get_local", "book_id": "b1"})
        assert got.ok is True and got.data["progress"]["b1"]["current_page"] == 25

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
