"""Offline subtitle parsing, translation degradation, security, and PluginManager lifecycle tests."""

from __future__ import annotations

import http.server
import importlib.util
import json
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from omnidesk_core import OmnideskPaths, PluginManager, PluginState  # noqa: E402
from omnidesk_sdk import CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_youtube_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _MockSubServer:
    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(self.path)
                if self.path == "/official.vtt":
                    body = (
                        "WEBVTT\nKind: captions\nLanguage: en\n\n"
                        "NOTE skip comment\n\n"
                        "00:00:01.000 --> 00:00:03.500 align:start\n"
                        "<c>Hello</c> <00:00:02.000>world &amp; friends\n\n"
                        "00:04.000 --> 00:06.000\nSecond line\n"
                    ).encode("utf-8")
                    self._send(200, "text/vtt", body)
                elif self.path == "/auto.srt":
                    body = (
                        "\ufeff1\n00:00:01,000 --> 00:00:03,000\n{\\an8}Auto <b>caption</b>\n\n"
                        "2\n00:00:03,500 --> 00:00:05,000\nSecond auto\n"
                    ).encode("utf-8")
                    self._send(200, "text/plain", body)
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _MockSubServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_vtt_srt_official_priority_translation_cache_and_degradation(tmp_path: Path) -> None:
    module = _load_plugin_module()
    sub_mod = importlib.import_module("yt_subtitles")
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    # Direct SRT parser check
    srt_cues = sub_mod.parse_srt("\ufeff1\n00:00:01,000 --> 00:00:02,500\n{\\an8}Line &gt; 1\n")
    assert len(srt_cues) == 1
    assert srt_cues[0]["original"] == "Line > 1"
    assert srt_cues[0]["start_ms"] == 1000 and srt_cues[0]["end_ms"] == 2500

    with _MockSubServer() as srv:
        plugin = module.YouTubePlugin(allow_loopback_http=True)
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
        ctx.set_config("cache_dir", str(tmp_path / "sub-cache"))
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        info_both = {
            "id": "SubTestVd01",
            "title": "Subtitle Demo",
            "subtitles": {"en": [{"ext": "vtt", "url": f"{srv.origin}/official.vtt"}]},
            "automatic_captions": {"en": [{"ext": "srt", "url": f"{srv.origin}/auto.srt"}]},
        }
        listed = await ctx.capabilities.call("media.youtube.v1", {"action": "list_subtitles", "video_id": "SubTestVd01", "info": info_both})
        assert listed.ok is True and listed.data["has_official"] is True and listed.data["tracks"][0]["source_type"] == "official"

        # Missing llm.translate.v1 -> graceful degradation (real VTT cues, no fabricated translation)
        degraded = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "SubTestVd01", "info": info_both})
        assert degraded.ok is True
        assert degraded.data["source_type"] == "official"
        assert degraded.data["format"] == "vtt"
        assert degraded.data["bilingual"] is False
        assert degraded.data["translation_degraded"] is True
        assert degraded.data["cues"][0]["original"] == "Hello world & friends"
        assert degraded.data["cues"][0]["translation"] == ""
        assert degraded.data["cues"][1]["start_ms"] == 4000

        # Auto-only SRT fallback when official missing
        info_auto_only = {"id": "SubTestVd02", "automatic_captions": {"en": [{"ext": "srt", "url": f"{srv.origin}/auto.srt"}]}}
        auto_res = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "SubTestVd02", "info": info_auto_only})
        assert auto_res.ok is True and auto_res.data["source_type"] == "automatic" and auto_res.data["cues"][0]["original"] == "Auto caption"

        # Register llm.translate.v1 -> bilingual translation & atomic disk cache
        ctx.register_capability(
            "llm.translate.v1",
            lambda req: CapabilityResult.ok({"translations": [f"译:{t}" for t in req["texts"]]}),
        )
        bilingual = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "SubTestVd01", "info": info_both})
        assert bilingual.ok is True and bilingual.data["bilingual"] is True and bilingual.data["cached"] is False
        assert bilingual.data["cues"][0]["translation"] == "译:Hello world & friends"
        cache_file = Path(bilingual.data["cache_path"])
        assert cache_file.is_file() and stat.S_IMODE(cache_file.stat().st_mode) == 0o600

        # Second fetch hits cache without hitting HTTP server again
        req_count_before = len(srv.requests)
        cached_res = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "SubTestVd01"})
        assert cached_res.ok is True and cached_res.data["cached"] is True and len(srv.requests) == req_count_before

        # No subtitles & no ASR -> fail closed (no fake subtitles)
        no_sub = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "NoSubsVd001", "info": {"id": "NoSubsVd001"}})
        assert no_sub.ok is False and no_sub.data["error_code"] == "not_found" and no_sub.data["cues"] == []

        # Optional speech.asr.v1 fallback when no subtitles exist
        ctx.register_capability(
            "speech.asr.v1",
            lambda req: CapabilityResult.ok({"cues": [{"start_ms": 0, "end_ms": 1500, "text": "ASR spoken line"}]}),
        )
        asr_res = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "NoSubsVd001", "info": {"id": "NoSubsVd001"}})
        assert asr_res.ok is True and asr_res.data["source_type"] == "asr" and asr_res.data["cues"][0]["translation"] == "译:ASR spoken line"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_https_only_and_path_traversal_security(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    plugin = module.create_plugin()
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg", config_schema=manifest["config_schema"])
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    for bad_url in (
        "http://www.youtube.com/watch?v=kCc8FmEb1nY",
        "http://127.0.0.1:8080/watch?v=kCc8FmEb1nY",
        "file:///etc/passwd",
        "https://evil.example.com/watch?v=kCc8FmEb1nY",
        "../escape",
    ):
        res = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "url": bad_url})
        assert res.ok is False and res.data["error_code"] == "invalid_argument"

    # Reject path traversal in cache_dir & symlinked cache_dir
    ctx.set_config("cache_dir", str(tmp_path / "a" / ".." / "escape"))
    trav = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "kCc8FmEb1nY"})
    assert trav.ok is False and trav.data["error_code"] == "path_safety"

    real_dir = tmp_path / "real-cache"
    real_dir.mkdir()
    sym_dir = tmp_path / "sym-cache"
    sym_dir.symlink_to(real_dir)
    ctx.set_config("cache_dir", str(sym_dir))
    sym_res = await ctx.capabilities.call("media.youtube.v1", {"action": "fetch_subtitles", "video_id": "kCc8FmEb1nY"})
    assert sym_res.ok is False and sym_res.data["error_code"] == "path_safety"

    # Reject path traversal in ytdlp_bin
    ctx.set_config({"cache_dir": str(real_dir), "ytdlp_bin": "/usr/bin/../bin/yt-dlp"})
    bin_trav = await ctx.capabilities.call("media.youtube.v1", {"action": "resolve_stream", "video_id": "kCc8FmEb1nY"})
    assert bin_trav.ok is False and bin_trav.data["error_code"] == "path_safety"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_core_plugin_manager_coexistence_and_unload_cleanup(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    )
    sibling_dir = tmp_path / "plugins" / "plugin-bilibili-peer"
    sibling_dir.mkdir(parents=True)
    (sibling_dir / "plugin.json").write_text(
        json.dumps({"schema_version": 2, "id": "plugin-bilibili-peer", "name": "Bili Peer", "version": "0.1.0", "kind": "workspace", "domain": "media", "provides": ["media.source.v1"]}),
        encoding="utf-8",
    )
    (sibling_dir / "plugin.py").write_text(
        "from omnidesk_sdk import BasePlugin, MediaSource\nclass Bili(MediaSource):\n    source_id = 'plugin-bilibili-peer'\nclass P(BasePlugin):\n    async def on_load(self, ctx):\n        await super().on_load(ctx)\n        ctx.register_media_source(Bili())\ndef create_plugin():\n    return P()\n",
        encoding="utf-8",
    )

    mgr = PluginManager(plugin_dirs=[PLUGIN_DIR.parent, sibling_dir.parent], paths=paths)
    rec_yt = await mgr.load_plugin(PLUGIN_DIR)
    rec_bili = await mgr.load_plugin(sibling_dir)
    assert rec_yt.state == PluginState.LOADED and rec_bili.state == PluginState.LOADED
    assert {s.source_id for s in mgr.domains.list_media_sources()} >= {"plugin-youtube", "plugin-bilibili-peer"}
    assert mgr.capabilities.has("media.youtube.v1")
    assert [t.tab_id for t in mgr.workspace_tabs(domain="media")] == ["youtube"]

    health = await mgr.health_check("plugin-youtube")
    assert health["ok"] is True

    await mgr.unload_plugin("plugin-youtube")
    assert not mgr.is_loaded("plugin-youtube")
    assert mgr.capabilities.list_capabilities(plugin_id="plugin-youtube") == []
    assert mgr.domains.list_sources(plugin_id="plugin-youtube") == []
    assert mgr.workspace_tabs(domain="media") == []
    assert mgr.bus.subscription_count(plugin_id="plugin-youtube") == 0
