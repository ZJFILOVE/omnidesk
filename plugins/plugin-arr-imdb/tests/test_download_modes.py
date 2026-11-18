"""Offline tests for v1 speed/size download modes, IMDb fallback, and unified download status UI."""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from omnidesk_sdk import PluginContext, invoke_lifecycle_hook

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _make_ctx(tmp_path: Path) -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )


def _load_plugin_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location("plugin_arr_imdb_dl_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _ServerFixture:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.posts: list[tuple[str, Any]] = []
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return None

            def _send(self, code: int, payload: Any, headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if outer.mode == "radarr":
                    if self.path.startswith("/api/v3/movie/lookup"):
                        self._send(200, [{"title": "Inception", "year": 2010, "tmdbId": 27205, "imdbId": "tt1375666", "titleSlug": "inception-27205"}])
                        return
                    if self.path == "/api/v3/movie":
                        self._send(200, [])
                        return
                    if self.path == "/api/v3/qualityprofile":
                        self._send(200, [{"id": 1, "name": "Speed"}, {"id": 4, "name": "Remux-4K"}])
                        return
                    if self.path == "/api/v3/rootfolder":
                        self._send(200, [{"id": 1, "path": "/movies"}])
                        return
                    if self.path.startswith("/api/v3/release"):
                        self._send(
                            200,
                            [
                                {"guid": "g-cam", "indexerId": 1, "title": "Inception.2010.HDCAM", "seeders": 999, "size": 1_000_000_000, "quality": {"quality": {"id": 1}}},
                                {"guid": "g-fast", "indexerId": 1, "title": "Inception.2010.1080p.WEB", "seeders": 120, "size": 4_000_000_000, "quality": {"quality": {"id": 4}}},
                                {"guid": "g-big", "indexerId": 2, "title": "Inception.2010.2160p.REMUX", "seeders": 8, "size": 65_000_000_000, "quality": {"quality": {"id": 9}}},
                            ],
                        )
                        return
                    if self.path.startswith("/api/v3/queue"):
                        self._send(
                            200,
                            {
                                "totalRecords": 1,
                                "records": [
                                    {"id": 10, "title": "Inception.2010.1080p.WEB", "status": "downloading", "trackedDownloadState": "downloading", "downloadClient": "qBittorrent", "size": 4_000_000_000, "sizeleft": 1_000_000_000, "timeleft": "00:03:20"}
                                ],
                            },
                        )
                        return
                elif outer.mode == "qb":
                    if self.path.startswith("/api/v2/torrents/info"):
                        self._send(
                            200,
                            [
                                {"hash": "abc123", "name": "Inception.2010.1080p.WEB", "state": "downloading", "progress": 0.75, "dlspeed": 10_485_760, "upspeed": 0, "size": 4_000_000_000, "amount_left": 1_000_000_000, "eta": 95, "num_seeds": 120}
                            ],
                        )
                        return
                self._send(404, {})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
                body = json.loads(raw) if raw.startswith("{") else raw
                outer.posts.append((self.path, body))
                if outer.mode == "radarr":
                    if self.path == "/api/v3/movie":
                        self._send(201, {"id": 501, **body})
                        return
                    if self.path == "/api/v3/release":
                        self._send(200, {"approved": True})
                        return
                elif outer.mode == "qb" and self.path == "/api/v2/auth/login":
                    self._send(200, "Ok.", headers={"Set-Cookie": "SID=test-sid; Path=/"})
                    return
                self._send(404, {})

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> "_ServerFixture":
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.mark.asyncio
async def test_download_speed_and_size_modes_and_status_ui(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    ctx = _make_ctx(tmp_path)

    with _ServerFixture("radarr") as rad, _ServerFixture("qb") as qb:
        ctx.set_config(
            {
                "radarr_url": rad.base_url,
                "radarr_seeders_first_profiles": "1",
                "radarr_default_profile_id": 4,
                "reject_cam_ts": True,
                "qbittorrent_url": qb.base_url,
                "qbittorrent_username": "admin",
                "imdb_language": "zh-CN",
            }
        )
        ctx.set_secret("radarr_api_key", "rad-sec")
        ctx.set_secret("qbittorrent_password", "qb-sec")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Dry-run preview must not POST to Radarr
        preview = await plugin.handle_tab_action("download_preview", {"item_id": "tt1375666", "service": "radarr", "download_mode": "speed"})
        assert preview["ok"] is True
        assert preview["result"]["dry_run"] is True
        assert preview["result"]["download_mode"] == "speed"
        assert rad.posts == []

        # Speed mode skips HDCAM and grabs highest-seeders (120) release
        speed_res = await plugin.handle_tab_action("download_submit", {"item_id": "tt1375666", "service": "radarr", "download_mode": "speed"})
        assert speed_res["ok"] is True
        assert speed_res["result"]["submitted"] is True
        assert speed_res["result"]["started"] is True
        assert speed_res["result"]["grabbed_release"]["title"] == "Inception.2010.1080p.WEB"
        assert speed_res["result"]["grabbed_release"]["seeders"] == 120

        # UI schema download list should be populated with Radarr + qBittorrent progress
        dl_list = next(c for c in speed_res["ui_schema"]["children"] if c.get("id") == "arr-imdb-downloads")
        assert len(dl_list["items"]) == 2
        assert any("qBittorrent" in item["subtitle"] and "75.0%" in item["subtitle"] for item in dl_list["items"])

        # Size mode grabs highest quality / largest (65 GB REMUX) release
        rad.posts.clear()
        size_res = await plugin.handle_tab_action("download_submit", {"item_id": "tt1375666", "service": "radarr", "download_mode": "size"})
        assert size_res["ok"] is True
        assert size_res["result"]["grabbed_release"]["title"] == "Inception.2010.2160p.REMUX"

        # Searching without imdb_api_base falls back cleanly to Radarr lookup instead of not_configured
        search_res = await plugin.handle_tab_action("fetch_items", {"category": "search", "query": "Inception"})
        assert search_res["ok"] is True
        assert search_res["result"]["count"] >= 1


@pytest.mark.asyncio
async def test_imdb_graphql_parsing_poster_optimization_and_cover_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_mod = _load_plugin_module()
    imdb_mod = sys.modules.get("imdb_client")
    assert imdb_mod is not None

    raw_url = "https://m.media-amazon.com/images/M/ABC123._V1_.jpg"
    assert imdb_mod.optimize_poster_url(raw_url) == "https://m.media-amazon.com/images/M/ABC123._V1_QL75_UX320_.jpg"
    assert imdb_mod.optimize_poster_url("https://image.tmdb.org/t/p/w500/x.jpg") == "https://image.tmdb.org/t/p/w500/x.jpg"

    class _FakeHttp:
        async def request_json(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
            assert url == "https://api.graphql.imdb.com/"
            query = str((kwargs.get("json_body") or {}).get("query") or "")
            if "chartTitles" in query:
                return 200, {
                    "data": {
                        "chartTitles": {
                            "edges": [
                                {
                                    "currentRank": 1,
                                    "node": {
                                        "id": "tt0111161",
                                        "titleText": {"text": "The Shawshank Redemption"},
                                        "originalTitleText": {"text": "The Shawshank Redemption"},
                                        "titleType": {"id": "movie"},
                                        "releaseYear": {"year": 1994},
                                        "ratingsSummary": {"aggregateRating": 9.3, "voteCount": 3000000},
                                        "runtime": {"seconds": 8520},
                                        "primaryImage": {"url": raw_url},
                                        "plot": {"plotText": {"plainText": "Two imprisoned men bond."}},
                                        "titleGenres": {"genres": [{"genre": {"text": "Drama"}}]},
                                    },
                                }
                            ]
                        }
                    }
                }
            return 200, {
                "data": {
                    "title": {
                        "id": "tt0111161",
                        "titleText": {"text": "The Shawshank Redemption"},
                        "titleType": {"id": "movie"},
                        "releaseYear": {"year": 1994},
                        "ratingsSummary": {"aggregateRating": 9.3},
                        "runtime": {"seconds": 8520},
                        "primaryImage": {"url": raw_url},
                        "plot": {"plotText": {"plainText": "Two imprisoned men bond."}},
                        "titleGenres": {"genres": [{"genre": {"text": "Drama"}}]},
                        "principalCredits": [
                            {"category": {"id": "director"}, "credits": [{"name": {"nameText": {"text": "Frank Darabont"}}}]},
                            {"category": {"id": "cast"}, "credits": [{"name": {"nameText": {"text": "Tim Robbins"}}}]},
                        ],
                    }
                }
            }

        async def request_bytes(self, method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
            return 200, {"content-type": "image/jpeg"}, b"\xff\xd8\xff\xe0TESTIMG"

    client = imdb_mod.ImdbTmdbClient(_FakeHttp(), public_fallback=True)
    items = await client.fetch_items("top_rated")
    assert len(items) == 1
    assert items[0]["id"] == "tt0111161"
    assert items[0]["poster"] == "https://m.media-amazon.com/images/M/ABC123._V1_QL75_UX320_.jpg"

    detail = await client.fetch_detail("tt0111161")
    assert detail["directors"] == ["Frank Darabont"]
    assert detail["cast"] == ["Tim Robbins"]

    plugin = plugin_mod.ArrImdbPlugin()
    monkeypatch.setattr(plugin, "_http_client", lambda: _FakeHttp())
    cover_res = await plugin.handle_capability({"action": "cover", "url": "https://m.media-amazon.com/images/M/ABC._V1_QL75_UX320_.jpg"})
    assert cover_res.ok is True
    assert cover_res.data["data_url"].startswith("data:image/jpeg;base64,")
