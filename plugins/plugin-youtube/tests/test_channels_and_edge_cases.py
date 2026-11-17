"""Additional offline coverage: fake yt-dlp subtitle pipeline, channels/watch_later, tabs, and edge cases."""

from __future__ import annotations

import http.server
import importlib.util
import json
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from omnidesk_sdk import CapabilityResult, LyricLine, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_youtube_edge", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_ytdlp(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-yt-dlp-edge"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(stat.S_IRWXU)
    return script


class _EdgeSubServer:
    def __init__(self) -> None:
        self.mode = "ok"
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if outer.mode == "403":
                    self._send(403, b"forbidden")
                    return
                if outer.mode == "timeout":
                    time.sleep(0.5)
                if self.path == "/subs.vtt":
                    self._send(200, b"WEBVTT\n\n00:01.000 --> 00:03.000\nReal caption cue\n")
                else:
                    self._send(404, b"missing")

            def _send(self, code: int, body: bytes) -> None:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "text/vtt")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _EdgeSubServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_channels_watch_later_user_ids_and_tab_actions(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    fake_ytdlp = _write_fake_ytdlp(
        tmp_path,
        """
import json, sys
target = sys.argv[-1]
if "/@karpathy/" in target:
    print(json.dumps({"entries": [{"id": "kCc8FmEb1nY", "title": "LLM Deep Dive", "channel": "Karpathy", "duration": 900}]}))
elif "list=PLdemo123" in target:
    print(json.dumps({"entries": [{"id": "9x5c3pP0M2k", "title": "Watch Later Jazz", "uploader": "Jazz", "duration": 600}]}))
elif "Insecure001" in target:
    print(json.dumps({"id": "Insecure001", "formats": [{"format_id": "18", "ext": "mp4", "height": 360, "url": "http://cdn.example.com/v.mp4"}]}))
elif "ChapVid0001" in target:
    print(json.dumps({
        "id": "ChapVid0001",
        "title": "Chapters Video",
        "duration": 300,
        "chapters": [{"start_time": 0, "end_time": 100, "title": "Intro"}, {"start_time": 100, "end_time": 300, "title": "Main"}],
    }))
else:
    print(json.dumps({"entries": []}))
""",
    )

    plugin = module.create_plugin()
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
    ctx.set_config({"ytdlp_bin": str(fake_ytdlp), "channel_ids": "@karpathy", "playlist_ids": "PLdemo123"})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    src = ctx.domains.media_sources()[0]
    assert "playlists" in src.categories()

    ch_items = await src.fetch_items("频道")
    assert len(ch_items) == 1 and ch_items[0].id == "kCc8FmEb1nY"

    wl_items = await src.fetch_items("稍后观看", "https://www.youtube.com/playlist?list=PLdemo123")
    assert len(wl_items) == 1 and wl_items[0].id == "9x5c3pP0M2k"

    # Subscriptions work without OAuth when user provides channel_ids / playlist_ids
    sub_list = await ctx.capabilities.call("media.youtube.v1", {"action": "list_subscriptions"})
    assert sub_list.ok is True and sub_list.data["channel_ids"] == ["@karpathy"] and sub_list.data["playlist_ids"] == ["PLdemo123"]
    sub_sync = await ctx.capabilities.call("media.youtube.v1", {"action": "sync_subscriptions"})
    assert sub_sync.ok is True and sub_sync.data["count"] == 2

    # Insecure-only format URL must fail closed
    insecure_res = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "Insecure001"})
    assert insecure_res.ok is False and insecure_res.data["error_code"] == "invalid_argument"

    # yt-dlp chapters + SponsorBlock `segments` payload merge
    ctx.register_capability("player.timeline.v1", lambda req: {"segments": [{"start": 40, "end": 65, "category": "sponsor"}]})
    chaps = await src.fetch_chapters("https://youtu.be/ChapVid0001")
    assert [c.kind for c in chaps] == ["content", "sponsor", "content"]

    # WorkspaceTabSpec on_action and invalid page argument
    tab = ctx.domains.workspace_tabs(domain="media")[0]
    tab_out = await tab.on_action("categories", {})
    assert tab_out["ok"] is True and "search" in tab_out["data"]["categories"]
    bad_page = await ctx.capabilities.call("media.youtube.v1", {"action": "search", "query": "x", "page": "not-an-int"})
    assert bad_page.ok is False and bad_page.data["error_code"] == "invalid_argument"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_ytdlp_end_to_end_subtitles_download_errors_and_translate_failure(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _EdgeSubServer() as srv:
        fake_ytdlp = _write_fake_ytdlp(
            tmp_path,
            f"""
import json, sys
print(json.dumps({{
    "id": "SubPipe0001",
    "title": "Pipeline Video",
    "subtitles": {{"en": [{{"ext": "vtt", "url": "{srv.origin}/subs.vtt"}}]}},
}}))
""",
        )
        plugin = module.YouTubePlugin(allow_loopback_http=True)
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
        ctx.set_config({"ytdlp_bin": str(fake_ytdlp), "cache_dir": str(tmp_path / "cache"), "request_timeout_sec": 0.25})
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Register failing llm.translate.v1 -> must gracefully degrade and still return parsed VTT cues
        ctx.register_capability("llm.translate.v1", lambda req: CapabilityResult.fail("gateway down", error_code="service_offline"))
        res = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "SubPipe0001"})
        assert res.ok is True and res.data["bilingual"] is False and res.data["translation_degraded"] is True
        assert res.data["cues"][0]["original"] == "Real caption cue"

        # MediaSource.fetch_lyrics bridges into LyricLine
        lyrics = await ctx.domains.media_sources()[0].fetch_lyrics("SubPipe0001")
        assert len(lyrics) == 1 and isinstance(lyrics[0], LyricLine) and lyrics[0].original == "Real caption cue"

        # Subtitle HTTP 403 & timeout fail closed
        srv.mode = "403"
        err_403 = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "SubPipe0001", "force_refresh": True})
        assert err_403.ok is False and err_403.data["error_code"] == "permission_denied"

        srv.mode = "timeout"
        err_to = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "SubPipe0001", "force_refresh": True})
        assert err_to.ok is False and err_to.data["error_code"] == "timeout"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
