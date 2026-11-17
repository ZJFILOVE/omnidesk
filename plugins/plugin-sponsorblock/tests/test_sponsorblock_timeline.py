"""Offline K-anonymity SHA-256 prefix, merge/dedup, next_segment, TTL cache, and error tests."""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import stat
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_sdk import ChapterSegment, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_sb_mod(submodule: str) -> Any:
    pkg_name = "_omnidesk_test_sponsorblock"
    if pkg_name not in sys.modules:
        pkg = ModuleType(pkg_name)
        pkg.__path__ = [str(PLUGIN_DIR)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
    full_name = f"{pkg_name}.{submodule}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PLUGIN_DIR / f"{submodule}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _start_mock_server(responder: Any) -> tuple[str, http.server.ThreadingHTTPServer, threading.Thread, list[dict[str, Any]]]:
    recorded: list[dict[str, Any]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            recorded.append({"path": self.path, "headers": dict(self.headers)})
            responder(self)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", server, thread, recorded


async def _make_ctx(tmp_path: Path, cfg: dict[str, Any]) -> PluginContext:
    plugin_mod = _load_sb_mod("plugin")
    plugin = plugin_mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config(cfg)
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    return ctx


@pytest.mark.asyncio
async def test_k_anonymity_4char_sha256_prefix_and_bucket_filtering(tmp_path: Path) -> None:
    yt_id = "kCc8FmEb1nY"
    bili_id = "BV1xK4y1Z7aa"
    yt_full = hashlib.sha256(yt_id.encode("utf-8")).hexdigest()
    bili_full = hashlib.sha256(bili_id.encode("utf-8")).hexdigest()

    def _responder(handler: Any) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        prefix = parsed.path.rsplit("/", 1)[-1]
        if prefix == yt_full[:4]:
            payload = [
                {
                    "videoID": "OTHER_COLLIDING_ID",
                    "segments": [{"UUID": "ignore-me", "segment": [1.0, 9.0], "category": "sponsor", "actionType": "skip"}],
                },
                {
                    "videoID": yt_id,
                    "segments": [
                        {"UUID": "yt-sp-1", "segment": [210.0, 295.0], "category": "sponsor", "actionType": "skip"},
                        {"UUID": "yt-ad-1", "segment": [10.0, 25.0], "category": "selfpromo", "actionType": "skip"},
                    ],
                },
            ]
            body = json.dumps(payload).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        if prefix == bili_full[:4]:
            payload = [
                {
                    "videoID": bili_id,
                    "segments": [{"UUID": "bili-sp-1", "segment": [480.0, 540.0], "category": "sponsor", "actionType": "skip"}],
                }
            ]
            body = json.dumps(payload).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        handler.send_response(404)
        handler.end_headers()

    base_url, server, thread, recorded = _start_mock_server(_responder)
    try:
        ctx = await _make_ctx(tmp_path, {"youtube_api_base": base_url, "bilibili_api_base": base_url})

        yt_res = await ctx.capabilities.call(
            "player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": yt_id}
        )
        assert yt_res.ok is True
        assert yt_res.data["hash_prefix"] == yt_full[:4]
        assert len(yt_res.data["hash_prefix"]) == 4
        req_yt = recorded[-1]
        assert urllib.parse.urlsplit(req_yt["path"]).path == f"/api/skipSegments/{yt_full[:4]}"
        assert yt_id not in req_yt["path"]
        assert yt_full not in req_yt["path"]
        assert yt_id not in json.dumps(req_yt["headers"])

        segs = [ChapterSegment.model_validate(s) for s in yt_res.data["segments"]]
        assert [s.id for s in segs] == ["yt-ad-1", "yt-sp-1"]
        assert [s.kind for s in segs] == ["ad", "sponsor"]

        bili_res = await ctx.capabilities.call(
            "player.timeline.v1", {"action": "fetch", "platform": "bilibili", "video_id": bili_id}
        )
        assert bili_res.ok is True
        assert bili_res.data["segments"][0]["id"] == "bili-sp-1"
        req_bili = recorded[-1]
        assert urllib.parse.urlsplit(req_bili["path"]).path == f"/api/skipSegments/{bili_full[:4]}"
        assert bili_id not in req_bili["path"]
        assert bili_full not in req_bili["path"]

        # Empty 404 -> []
        empty_res = await ctx.capabilities.call(
            "player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": "NoSegmentsVideo123"}
        )
        assert empty_res.ok is True
        assert empty_res.data["segments"] == []
        assert empty_res.data["count"] == 0

        # Sibling plugin compatibility aliases: get_segments (plugin-bilibili) & query (plugin-youtube)
        bili_alias = await ctx.capabilities.call(
            "player.timeline.v1", {"action": "get_segments", "provider": "bilibili", "video_id": bili_id}
        )
        assert bili_alias.ok is True
        assert bili_alias.data["segments"][0]["id"] == "bili-sp-1"

        yt_alias = await ctx.capabilities.call(
            "player.timeline.v1",
            {
                "action": "query",
                "platform": "youtube",
                "video_id": yt_id,
                "chapters": [{"id": "ch0", "start_sec": 0.0, "end_sec": 210.0, "label": "Intro", "kind": "content"}],
            },
        )
        assert yt_alias.ok is True
        assert [s["kind"] for s in yt_alias.data["segments"]] == ["content", "ad", "sponsor"]
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_ttl_cache_atomic_write_and_network_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sb_cache = _load_sb_mod("sb_cache")
    vid = "CacheTestVideo99"
    state = {"mode": "ok", "calls": 0}

    def _responder(handler: Any) -> None:
        state["calls"] += 1
        if state["mode"] == "error500":
            handler.send_response(500)
            handler.end_headers()
            return
        if state["mode"] == "slow":
            time.sleep(0.35)
        body = json.dumps(
            [{"videoID": vid, "segments": [{"UUID": f"seg-{state['calls']}", "segment": [30.0, 45.0], "category": "sponsor"}]}]
        ).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    base_url, server, thread, _ = _start_mock_server(_responder)
    try:
        clock = {"now": 1000.0}
        monkeypatch.setattr(sb_cache.time, "time", lambda: clock["now"])

        ctx = await _make_ctx(tmp_path, {"youtube_api_base": base_url, "cache_ttl_sec": 60})
        r1 = await ctx.capabilities.call("player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": vid})
        assert r1.ok is True
        assert r1.data["cached"] is False
        assert state["calls"] == 1

        cache_file = tmp_path / "data" / "timeline_cache.json"
        assert cache_file.is_file()
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
        assert not list((tmp_path / "data").glob("*.tmp"))

        clock["now"] = 1030.0
        r2 = await ctx.capabilities.call("player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": vid})
        assert r2.ok is True
        assert r2.data["cached"] is True
        assert state["calls"] == 1

        clock["now"] = 1065.0
        r3 = await ctx.capabilities.call("player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": vid})
        assert r3.ok is True
        assert r3.data["cached"] is False
        assert state["calls"] == 2
        assert r3.data["segments"][0]["id"] == "seg-2"

        state["mode"] = "error500"
        r_err = await ctx.capabilities.call(
            "player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": "AnotherId", "force_refresh": True}
        )
        assert r_err.ok is False
        assert r_err.data["error_code"] == "service_offline"

        state["mode"] = "slow"
        r_to = await ctx.capabilities.call(
            "player.timeline.v1",
            {"action": "fetch", "platform": "youtube", "video_id": "SlowVid", "timeout_sec": 0.1},
        )
        assert r_to.ok is False
        assert r_to.data["error_code"] == "timeout"

        ctx.set_config("bilibili_api_base", "http://127.0.0.1:1")
        r_off = await ctx.capabilities.call(
            "player.timeline.v1", {"action": "fetch", "platform": "bilibili", "video_id": "BV1xx411c7mD"}
        )
        assert r_off.ok is False
        assert r_off.data["error_code"] == "service_offline"
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_merge_dedup_and_f9_next_segment(tmp_path: Path) -> None:
    ctx = await _make_ctx(tmp_path, {})

    external_chapters = [
        {"id": "ch-1", "startSec": 0, "endSec": 120, "label": "00:00 Intro", "kind": "content"},
        {"id": "ch-1-dup", "start_sec": 0.1, "end_sec": 120.2, "label": "00:00 Intro", "kind": "content"},
        {"id": "ch-2", "start_sec": 120, "end_sec": 400, "label": "02:00 Deep Dive", "kind": "content"},
    ]
    sponsor_segments = [
        {"id": "sb-1", "start_sec": 80, "end_sec": 110, "label": "Sponsor A", "kind": "sponsor"},
        {"id": "sb-1-overlap", "start_sec": 105, "end_sec": 115, "label": "Sponsor A Extended", "kind": "sponsor"},
        {"id": "sb-1", "start_sec": 80, "end_sec": 110, "label": "Sponsor A", "kind": "sponsor"},
        {"id": "sb-2", "start_sec": 250, "end_sec": 270, "label": "Self Promo", "kind": "ad"},
    ]

    merged = await ctx.capabilities.call(
        "player.timeline.v1",
        {"action": "merge", "external_chapters": external_chapters, "sponsor_segments": sponsor_segments},
    )
    assert merged.ok is True
    segs = merged.data["segments"]
    assert len(segs) == 4
    assert [(s["kind"], s["start_sec"], s["end_sec"]) for s in segs] == [
        ("content", 0.0, 120.0),
        ("sponsor", 80.0, 115.0),
        ("content", 120.0, 400.0),
        ("ad", 250.0, 270.0),
    ]

    n1 = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "next_segment", "segments": segs, "current_sec": 40.0}
    )
    assert n1.ok is True
    assert n1.data["found"] is True
    assert n1.data["skip_to_sec"] == 115.0
    assert n1.data["active_now"] is False

    n2 = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "next_segment", "segments": segs, "current_sec": 95.0}
    )
    assert n2.ok is True
    assert n2.data["found"] is True
    assert n2.data["skip_to_sec"] == 115.0
    assert n2.data["active_now"] is True

    n3 = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "next_segment", "segments": segs, "current_sec": 130.0}
    )
    assert n3.ok is True
    assert n3.data["found"] is True
    assert n3.data["segment"]["kind"] == "ad"
    assert n3.data["skip_to_sec"] == 270.0

    n4 = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "next_segment", "segments": segs, "current_sec": 300.0}
    )
    assert n4.ok is True
    assert n4.data["found"] is False
    assert n4.data["segment"] is None
    assert n4.data["skip_to_sec"] is None
    await ctx.acleanup()
