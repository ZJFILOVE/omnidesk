"""Offline contract, MediaSource, yt-dlp stream resolution, API, and subscription tests."""

from __future__ import annotations

import http.server
import importlib.util
import json
import re
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from omnidesk_sdk import BasePlugin, ChapterSegment, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_youtube_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_ytdlp(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-yt-dlp"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(stat.S_IRWXU)
    return script


class _MockYouTubeServer:
    """Thread-safe local loopback HTTP server simulating YouTube Data API and errors."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mode = "ok"
        self.delay_sec = 0.0
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                outer.requests.append({"path": parsed.path, "query": qs, "headers": dict(self.headers)})
                if outer.delay_sec > 0:
                    time.sleep(outer.delay_sec)
                if outer.mode == "401":
                    self._send_json(401, {"error": {"message": "Invalid credentials"}})
                    return
                if outer.mode == "403":
                    self._send_json(403, {"error": {"errors": [{"reason": "forbidden"}], "message": "Forbidden"}})
                    return
                if outer.mode == "empty":
                    self._send_json(200, {"items": []})
                    return
                if parsed.path.endswith("/search"):
                    self._send_json(
                        200,
                        {
                            "items": [
                                {
                                    "id": {"videoId": "kCc8FmEb1nY"},
                                    "snippet": {
                                        "title": "Sparse Attention Explained",
                                        "channelTitle": "Andrej Karpathy",
                                        "channelId": "UCXUPKJO5MZQN11PqgIvyuvQ",
                                        "publishedAt": "2026-11-10T12:00:00Z",
                                        "description": "00:00 Intro\n03:30 Attention Heads",
                                    },
                                }
                            ]
                        },
                    )
                elif parsed.path.endswith("/videos"):
                    self._send_json(
                        200,
                        {
                            "items": [
                                {
                                    "id": "kCc8FmEb1nY",
                                    "contentDetails": {"duration": "PT32M25S"},
                                    "statistics": {"viewCount": "42000"},
                                }
                            ]
                        },
                    )
                elif parsed.path.endswith("/subscriptions"):
                    if self.headers.get("Authorization") != "Bearer oauth-secret-999":
                        self._send_json(401, {"error": {"message": "Bad token"}})
                        return
                    self._send_json(
                        200,
                        {
                            "items": [
                                {
                                    "snippet": {
                                        "title": "Karpathy",
                                        "resourceId": {"channelId": "UCXUPKJO5MZQN11PqgIvyuvQ"},
                                    }
                                }
                            ]
                        },
                    )
                elif parsed.path.endswith("/playlistItems"):
                    self._send_json(
                        200,
                        {
                            "items": [
                                {
                                    "snippet": {
                                        "title": "Sub Upload Video",
                                        "channelTitle": "Karpathy",
                                        "channelId": "UCXUPKJO5MZQN11PqgIvyuvQ",
                                        "resourceId": {"videoId": "9x5c3pP0M2k"},
                                    },
                                    "contentDetails": {"videoId": "9x5c3pP0M2k"},
                                }
                            ]
                        },
                    )
                else:
                    self._send_json(404, {"error": "not found"})

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except BrokenPipeError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/youtube/v3"

    def __enter__(self) -> _MockYouTubeServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def test_manifest_matches_schema_and_config_rules() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-youtube"
    assert manifest["kind"] == "workspace"
    assert manifest["domain"] == "media"
    assert manifest["entry"] == "plugin:create_plugin"
    assert manifest["provides"] == ["media.source.v1", "media.youtube.v1"]
    assert set(manifest["consumes"]) == {"llm.translate.v1", "speech.asr.v1", "player.timeline.v1", "web.cookies.v1", "web.embed.v1"}
    assert set(manifest.keys()).issubset(set(schema["properties"].keys()))

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in manifest["provides"] + manifest["consumes"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    for required_key in ("api_key", "oauth_token", "ytdlp_bin", "region", "quality", "cache_dir"):
        assert required_key in fields
    assert fields["api_key"]["type"] == "secret" and fields["api_key"]["default"] == ""
    assert fields["oauth_token"]["type"] == "secret" and fields["oauth_token"]["default"] == ""
    assert fields["ytdlp_bin"]["default"] == "yt-dlp"
    assert fields["quality"]["type"] == "select" and fields["quality"]["options"]


@pytest.mark.asyncio
async def test_categories_empty_results_and_api_search_with_errors(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockYouTubeServer() as srv:
        plugin = module.YouTubePlugin(api_base_url=srv.base_url, allow_loopback_http=True)
        assert isinstance(plugin, BasePlugin)
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
        ctx.set_secret("api_key", "AIzaSecretTestKey12345")
        ctx.set_config("request_timeout_sec", 1)
        assert "api_key" not in ctx.get_config()

        await invoke_lifecycle_hook(plugin.on_load, ctx)
        src = ctx.domains.media_sources()[0]
        assert set(["subscriptions", "search", "channels", "watch_later"]).issubset(set(src.categories()))

        assert await src.fetch_items("search", "") == []
        assert await src.fetch_items("subscriptions", "") == []
        assert await src.fetch_items("search", "q", page=0) == []

        items = await src.fetch_items("search", "transformers")
        assert len(items) == 1
        item = items[0]
        assert item.id == "kCc8FmEb1nY"
        assert item.creator == "Andrej Karpathy"
        assert item.cover_url == "https://i.ytimg.com/vi/kCc8FmEb1nY/hqdefault.jpg"
        assert item.play_url == "https://www.youtube.com/watch?v=kCc8FmEb1nY"
        assert item.duration_sec == 1945.0

        srv.mode = "empty"
        assert await src.fetch_items("search", "nothing") == []

        for srv_mode, expected_code in (("401", "permission_denied"), ("403", "permission_denied")):
            srv.mode = srv_mode
            assert await src.fetch_items("search", "fail") == []
            rpc_err = await ctx.capabilities.call("media.youtube.v1", {"action": "search", "query": "fail"})
            assert rpc_err.ok is False
            assert rpc_err.data["error_code"] == expected_code
            assert "AIzaSecretTestKey12345" not in rpc_err.error
            assert "AIzaSecretTestKey12345" not in json.dumps(rpc_err.data)

        srv.mode = "ok"
        srv.delay_sec = 0.6
        ctx.set_config("request_timeout_sec", 0.2)
        timeout_res = await ctx.capabilities.call("media.youtube.v1", {"action": "search", "query": "slow"})
        assert timeout_res.ok is False
        assert timeout_res.data["error_code"] == "timeout"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_ytdlp_search_and_resolve_stream_variants(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    fake_ytdlp = _write_fake_ytdlp(
        tmp_path,
        """
import json, sys, time
args = sys.argv[1:]
target = args[-1]
if target.startswith("ytsearch"):
    print(json.dumps({
        "entries": [
            {"id": "9x5c3pP0M2k", "title": "Live Trio", "channel": "Jazz Archive", "duration": 2680},
            {"id": "bad", "title": "Invalid ID"},
        ]
    }))
elif "VidOk108001" in target:
    print(json.dumps({
        "id": "VidOk108001",
        "title": "Test Video",
        "channel": "Tech Channel",
        "duration": 120,
        "http_headers": {"User-Agent": "OmniDesk-Test/1.0", "Cookie": "SID=should-strip", "Authorization": "Bearer strip"},
        "formats": [
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "url": "https://i.ytimg.com/sb.mhtml"},
            {"format_id": "drm1", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none", "has_drm": True, "url": "https://cdn.example.com/drm.mp4"},
            {"format_id": "http1", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "url": "http://insecure.example.com/v.mp4"},
            {"format_id": "137", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none", "tbr": 2400, "url": "https://rr1.googlevideo.com/v1080.mp4"},
            {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 160, "url": "https://rr1.googlevideo.com/a160.webm"},
        ],
    }))
elif "Vid403Err01" in target:
    print("ERROR: HTTP Error 403: Forbidden", file=sys.stderr)
    sys.exit(1)
elif "VidMembOnly" in target:
    print("ERROR: Join this channel to get access to members-only content.", file=sys.stderr)
    sys.exit(1)
elif "VidDrmOnly1" in target:
    print(json.dumps({"id": "VidDrmOnly1", "has_drm": True, "formats": []}))
elif "VidTimeout1" in target:
    time.sleep(2.0)
else:
    print(json.dumps({"entries": []}))
""",
    )

    plugin = module.create_plugin()
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
    ctx.set_config({"ytdlp_bin": str(fake_ytdlp), "quality": "1080p", "request_timeout_sec": 1})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    src = ctx.domains.media_sources()[0]
    yt_items = await src.fetch_items("search", "jazz")
    assert len(yt_items) == 1
    assert yt_items[0].id == "9x5c3pP0M2k"
    assert yt_items[0].cover_url == "https://i.ytimg.com/vi/9x5c3pP0M2k/hqdefault.jpg"

    ok_res = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "VidOk108001"})
    assert ok_res.ok is True
    assert ok_res.data["play_url"] == "https://rr1.googlevideo.com/v1080.mp4"
    assert ok_res.data["audio_url"] == "https://rr1.googlevideo.com/a160.webm"
    assert ok_res.data["quality"] == "1080p"
    assert ok_res.data["headers"] == {"User-Agent": "OmniDesk-Test/1.0"}

    audio_res = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "VidOk108001", "quality": "audio"})
    assert audio_res.ok is True and audio_res.data["play_url"] == "https://rr1.googlevideo.com/a160.webm"

    err_403 = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "Vid403Err01"})
    assert err_403.ok is False and err_403.data["error_code"] == "permission_denied" and err_403.data["http_status"] == 403

    err_mem = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "VidMembOnly"})
    assert err_mem.ok is False and err_mem.data["error_code"] == "permission_denied" and err_mem.data["reason"] == "members_only"

    err_drm = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "VidDrmOnly1"})
    assert err_drm.ok is False and err_drm.data["error_code"] == "permission_denied" and err_drm.data["reason"] == "drm_protected"

    ctx.set_config("request_timeout_sec", 0.25)
    ctx.set_config("stream_timeout_sec", 0.25)
    err_to = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "VidTimeout1"})
    assert err_to.ok is False and err_to.data["error_code"] == "timeout"

    ctx.set_config("ytdlp_bin", str(tmp_path / "missing-yt-dlp"))
    err_missing = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "VidOk108001"})
    assert err_missing.ok is False and err_missing.data["error_code"] == "not_configured"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_subscriptions_and_sponsorblock_chapters(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockYouTubeServer() as srv:
        plugin = module.YouTubePlugin(api_base_url=srv.base_url, allow_loopback_http=True)
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        unconf = await ctx.capabilities.call("media.youtube.v1", {"action": "list_subscriptions"})
        assert unconf.ok is False and unconf.data["error_code"] == "not_configured"
        unconf_sync = await ctx.capabilities.call("media.youtube.v1", {"action": "sync_subscriptions"})
        assert unconf_sync.ok is False and unconf_sync.data["error_code"] == "not_configured"

        ctx.set_secret("oauth_token", "oauth-secret-999")
        bus_events: list[dict[str, Any]] = []
        ctx.subscribe("youtube.subscriptions.synced", lambda evt: bus_events.append(evt.payload))

        sync_res = await ctx.capabilities.call("media.youtube.v1", {"action": "sync_subscriptions"})
        assert sync_res.ok is True
        assert sync_res.data["count"] == 1
        assert sync_res.data["items"][0]["id"] == "9x5c3pP0M2k"
        assert len(bus_events) == 1

        ctx.set_secret("oauth_token", "wrong-secret-token")
        auth_err = await ctx.capabilities.call("media.youtube.v1", {"action": "list_subscriptions"})
        assert auth_err.ok is False and auth_err.data["error_code"] == "permission_denied"
        assert "wrong-secret-token" not in auth_err.error

        # SponsorBlock optional merge with fetch_chapters
        ctx.set_secret("oauth_token", None)
        ctx.set_secret("api_key", "AIzaTest")
        src = ctx.domains.media_sources()[0]
        await src.fetch_items("search", "attention")  # caches description chapters for kCc8FmEb1nY

        plain_chapters = await src.fetch_chapters("kCc8FmEb1nY")
        assert len(plain_chapters) == 2 and all(isinstance(c, ChapterSegment) for c in plain_chapters)

        ctx.register_capability(
            "player.timeline.v1",
            lambda req: {"chapters": [{"id": "sb-1", "start_sec": 120.0, "end_sec": 180.0, "label": "Sponsor", "kind": "sponsor"}]},
        )
        merged_res = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_chapters", "video_id": "kCc8FmEb1nY"})
        assert merged_res.ok is True
        kinds = [c["kind"] for c in merged_res.data["chapters"]]
        assert kinds == ["content", "sponsor", "content"]

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
