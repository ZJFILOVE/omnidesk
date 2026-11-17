"""Offline tests for `ai.curation.v1` fail-closed guarantees, partial_success, and dedup."""

from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_sdk import CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_magpie_agent_curation_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_skill_fixture(base: Path) -> Path:
    sdir = base / "omnidesk-curation-writeback"
    (sdir / "references").mkdir(parents=True, exist_ok=True)
    for rel in (
        "SKILL.md",
        "references/archive-rehydration.md",
        "references/daily-immutable-rules.md",
        "references/interactive-repeatable-rules.md",
        "references/writeback-rpc-and-apple-music.md",
    ):
        (sdir / rel).write_text(f"# {rel}\nrule content\n", encoding="utf-8")
    return sdir


def _closed_loopback_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _start_curation_llm_server(response_obj: Any) -> tuple[http.server.ThreadingHTTPServer, str, threading.Thread]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            _ = self.rfile.read(length)
            body = json.dumps({
                "model": "group/omnidesk-active",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(response_obj)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", thread


@pytest.mark.asyncio
async def test_curate_interactive_skill_missing_and_no_verified_candidates(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("gateway_url", _closed_loopback_url())
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # 1. Missing skill -> skill_unavailable
    ctx.set_config("skill_dir", str(tmp_path / "missing-skill"))
    sk_res = await ctx.capabilities.call("ai.curation.v1", {"action": "curate_interactive", "prompt": "钢琴曲"})
    assert sk_res.ok is False
    assert sk_res.data["error_code"] == "skill_unavailable"

    # Check default repo skill auto-discovery works via status
    ctx.set_config("skill_dir", "")
    st_res = await ctx.capabilities.call("ai.curation.v1", {"action": "status"})
    assert st_res.ok is True and st_res.data["skill_available"] is True

    # 2. Valid skill, offline gateway, empty archive -> no_verified_candidates
    ctx.set_config("skill_dir", str(_create_skill_fixture(tmp_path)))
    ctx.capabilities.register(
        "recommend.feed.v1",
        lambda req: CapabilityResult.ok({"media_items": [], "text_items": [], "items": []}),
        plugin_id="plugin-ai-curator",
    )
    empty_res = await ctx.capabilities.call("ai.curation.v1", {"action": "curate_interactive", "prompt": "冷门内容"})
    assert empty_res.ok is False
    assert empty_res.data["error_code"] == "no_verified_candidates"

    # 3. LLM fabricates fake song & fake DOI -> dropped by preflight/archive gate -> no_verified_candidates
    server, url, thread = _start_curation_llm_server({
        "media_items": [{"id": "fake-1", "kind": "music", "apple_music_song_id": "999999", "title": "Fake Song"}],
        "text_items": [{"id": "fake-doi", "title": "Fake Paper", "doi": "10.9999/fake"}],
    })
    try:
        ctx.set_config("gateway_url", url)
        ctx.capabilities.register(
            "media.playlist.v1",
            lambda req: CapabilityResult.ok({"verified_ids": []}) if req.get("action") == "preflight_cn_catalog" else CapabilityResult.ok({}),
            plugin_id="plugin-music",
        )
        fake_res = await ctx.capabilities.call("ai.curation.v1", {"action": "curate_interactive", "prompt": "推荐"})
        assert fake_res.ok is False
        assert fake_res.data["error_code"] == "no_verified_candidates"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_curate_interactive_order_partial_success_and_session_dedup(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("skill_dir", str(_create_skill_fixture(tmp_path)))
    ctx.set_config("gateway_url", _closed_loopback_url())
    ctx.set_secret("gateway_key", "sk-curation-secret-555")

    call_log: list[tuple[str, str]] = []
    playlist_should_fail = {"flag": False}

    def handle_feed(req: dict[str, Any]) -> CapabilityResult:
        act = req.get("action")
        call_log.append(("recommend.feed.v1", str(act)))
        if act == "search_archive":
            return CapabilityResult.ok({
                "media_items": [
                    {
                        "id": "am-cn-1001",
                        "title": "Track One",
                        "creator": "Artist A",
                        "kind": "music",
                        "play_url": "https://music.apple.com/cn/song/1001",
                        "extra": {"apple_music_song_id": "1001"},
                    },
                    {
                        "id": "am-cn-1002",
                        "title": "Track Two",
                        "creator": "Artist B",
                        "kind": "music",
                        "play_url": "https://music.apple.com/cn/song/1002",
                        "extra": {"apple_music_song_id": "1002"},
                    },
                ],
                "text_items": [
                    {"id": "paper-1", "title": "Active Learning Study", "doi": "10.1038/s41586-026-00001-1", "url": "https://doi.org/10.1038/s41586-026-00001-1"}
                ],
            })
        if act == "lookup_provenance":
            return CapabilityResult.ok({
                "read_only": True,
                "matches": [{"id": "am-cn-1001", "source_id": "plugin-ai-curator", "previously_recommended_on": "2026-10-12"}],
            })
        if act == "push_interactive_set":
            assert req["mode"] == "interactive_ephemeral"
            assert req["record_permanent_history"] is False
            return CapabilityResult.ok({"pushed": True, "session_id": req["session_id"]})
        return CapabilityResult.fail("unsupported", error_code="invalid_argument")

    def handle_playlist(req: dict[str, Any]) -> CapabilityResult:
        act = req.get("action")
        call_log.append(("media.playlist.v1", str(act)))
        if act == "preflight_cn_catalog":
            return CapabilityResult.ok({"verified_ids": ["1001", "1002", "1004"]})
        if act in {"write_interactive_playlist", "enqueue_playback"}:
            if playlist_should_fail["flag"]:
                return CapabilityResult.fail("Apple Music token sk-curation-secret-555 expired", error_code="unauthorized")
            return CapabilityResult.ok({"synced": True, "playlist_name": req.get("playlist_name", "queue")})
        return CapabilityResult.fail("unexpected", error_code="invalid_argument")

    ctx.capabilities.register("recommend.feed.v1", handle_feed, plugin_id="plugin-ai-curator")
    ctx.capabilities.register("media.playlist.v1", handle_playlist, plugin_id="plugin-music")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # 1. Protected daily playlist guard
    prot = await ctx.capabilities.call(
        "ai.curation.v1",
        {"action": "curate_interactive", "prompt": "music", "playlist_id": "p.YJXV7dEIeb3GzZX"},
    )
    assert prot.ok is False and prot.data["error_code"] == "protected_daily_playlist"

    # 2. Happy path: search_archive -> preflight -> lookup_provenance -> push_interactive_set -> write_interactive_playlist
    ok_res = await ctx.capabilities.call(
        "ai.curation.v1",
        {"action": "curate_interactive", "prompt": "静心音乐", "session_id": "sess-1", "repeat_policy": "session_dedup_only"},
    )
    assert ok_res.ok is True
    assert ok_res.data["feed_written"] is True
    assert ok_res.data["playlist_synced"] is True
    assert ok_res.data["media_items"][0]["extra"]["previously_recommended_on"] == "2026-10-12"
    push_idx = call_log.index(("recommend.feed.v1", "push_interactive_set"))
    write_idx = call_log.index(("media.playlist.v1", "write_interactive_playlist"))
    assert push_idx < write_idx

    # 3. session_dedup_only skips previous identities (1001, 1002, paper-1) and keeps new 1004, calling enqueue_playback
    dup_res = await ctx.capabilities.call(
        "ai.curation.v1",
        {
            "action": "curate_interactive",
            "prompt": "再来一首",
            "session_id": "sess-1",
            "repeat_policy": "session_dedup_only",
            "sync_apple_music": "queue",
            "media_items": [{"id": "am-cn-1004", "title": "Track Four", "kind": "music", "apple_music_song_id": "1004"}],
        },
    )
    assert dup_res.ok is True
    assert dup_res.data["skipped_session_duplicates"] == 3
    assert [m["extra"]["apple_music_song_id"] for m in dup_res.data["media_items"]] == ["1004"]
    assert ("media.playlist.v1", "enqueue_playback") in call_log

    # 4. Partial failure when playlist fails after feed write succeeds
    playlist_should_fail["flag"] = True
    part_res = await ctx.capabilities.call(
        "ai.curation.v1",
        {"action": "curate_interactive", "prompt": "再来", "session_id": "sess-2", "repeat_policy": "allow_any"},
    )
    assert part_res.ok is False
    assert part_res.data["partial_success"] is True
    assert part_res.data["feed_written"] is True
    assert part_res.data["playlist_synced"] is False
    assert "sk-curation-secret-555" not in part_res.data["playlist_error"]
    assert "[REDACTED]" in part_res.data["playlist_error"]

    # 5. Graceful degradation when media.playlist.v1 is absent and only text items requested
    ctx.capabilities.unregister("media.playlist.v1", plugin_id="plugin-music")
    text_only = await ctx.capabilities.call(
        "ai.curation.v1",
        {"action": "curate_interactive", "prompt": "论文", "domains": ["text"], "session_id": "sess-3"},
    )
    assert text_only.ok is True
    assert text_only.data["feed_written"] is True
    assert text_only.data["playlist_synced"] is False

    await invoke_lifecycle_hook(plugin.on_unload)
    assert plugin._session_seen == {}
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_curate_interactive_derives_focused_archive_query(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("skill_dir", str(_create_skill_fixture(tmp_path)))
    ctx.set_config("gateway_url", _closed_loopback_url())

    search_reqs: list[dict[str, Any]] = []

    def handle_feed(req: dict[str, Any]) -> CapabilityResult:
        act = req.get("action")
        if act == "search_archive":
            search_reqs.append(dict(req))
            return CapabilityResult.ok(
                {
                    "media_items": [
                        {
                            "id": f"am-cn-{2000 + index}",
                            "title": f"Jazz Track {index}",
                            "creator": "Bill Evans",
                            "kind": "music",
                            "play_url": f"https://music.apple.com/cn/song/{2000 + index}",
                            "extra": {"apple_music_song_id": str(2000 + index)},
                        }
                        for index in range(1, 7)
                    ]
                }
            )
        if act == "lookup_provenance":
            return CapabilityResult.ok({"read_only": True, "matches": []})
        if act == "push_interactive_set":
            return CapabilityResult.ok({"pushed": True})
        return CapabilityResult.fail("unexpected", error_code="invalid_argument")

    def handle_playlist(req: dict[str, Any]) -> CapabilityResult:
        if req.get("action") == "preflight_cn_catalog":
            return CapabilityResult.ok({"verified_ids": [str(2000 + index) for index in range(1, 7)]})
        return CapabilityResult.ok({})

    ctx.capabilities.register("recommend.feed.v1", handle_feed, plugin_id="plugin-ai-curator")
    ctx.capabilities.register("media.playlist.v1", handle_playlist, plugin_id="plugin-music")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    prompt = "从往期每日 5 首历史库里重温 3 首最适合深夜编码的冷静器乐爵士，写到灵感电台。"
    res = await ctx.capabilities.call(
        "ai.curation.v1",
        {
            "action": "curate_interactive",
            "prompt": prompt,
            "domains": ["media"],
            "session_id": "ix-focused-query",
            "sync_apple_music": "none",
        },
    )

    assert res.ok is True
    assert res.data["feed_written"] is True
    assert res.data["requested_count"] == 3
    assert len(res.data["media_items"]) == 3
    assert res.data["llm_selection"]["used"] is False
    assert search_reqs
    assert search_reqs[0]["query"] == "爵士"
    assert search_reqs[0]["domain"] == "music"
    assert search_reqs[0]["query"] != prompt

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_curate_interactive_recovers_markdown_selected_ids(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("skill_dir", str(_create_skill_fixture(tmp_path)))
    server, url, thread = _start_curation_llm_server("我推荐 am-cn-1002，适合深夜单曲循环。")
    try:
        ctx.set_config("gateway_url", url)

        def handle_feed(req: dict[str, Any]) -> CapabilityResult:
            act = req.get("action")
            if act == "search_archive":
                return CapabilityResult.ok(
                    {
                        "media_items": [
                            {
                                "id": f"am-cn-{sid}",
                                "title": f"Track {sid}",
                                "kind": "music",
                                "play_url": f"https://music.apple.com/cn/song/{sid}",
                                "extra": {"apple_music_song_id": sid},
                            }
                            for sid in ("1001", "1002")
                        ]
                    }
                )
            if act == "lookup_provenance":
                return CapabilityResult.ok({"read_only": True, "matches": []})
            if act == "push_interactive_set":
                return CapabilityResult.ok({"pushed": True})
            return CapabilityResult.fail("unexpected", error_code="invalid_argument")

        def handle_playlist(req: dict[str, Any]) -> CapabilityResult:
            if req.get("action") == "preflight_cn_catalog":
                return CapabilityResult.ok({"verified_ids": ["1001", "1002"]})
            return CapabilityResult.ok({})

        ctx.capabilities.register("recommend.feed.v1", handle_feed, plugin_id="plugin-ai-curator")
        ctx.capabilities.register("media.playlist.v1", handle_playlist, plugin_id="plugin-music")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        res = await ctx.capabilities.call(
            "ai.curation.v1",
            {
                "action": "curate_interactive",
                "prompt": "推荐 1 首适合深夜的曲子",
                "domains": ["media"],
                "session_id": "ix-markdown-fallback",
                "sync_apple_music": "none",
            },
        )

        assert res.ok is True
        assert [item["id"] for item in res.data["media_items"]] == ["am-cn-1002"]
        assert res.data["llm_selection"]["used"] is True
        assert "markdown" in res.data["llm_selection"]["reason"]

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_curate_interactive_searches_video_and_text_for_mixed_prompts(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("skill_dir", str(_create_skill_fixture(tmp_path)))
    ctx.set_config("gateway_url", _closed_loopback_url())

    searches: list[dict[str, Any]] = []

    def handle_feed(req: dict[str, Any]) -> CapabilityResult:
        act = req.get("action")
        if act == "search_archive":
            searches.append(dict(req))
            if req.get("domain") == "video":
                return CapabilityResult.ok(
                    {
                        "media_items": [
                            {
                                "id": "yt-kv-cache-001",
                                "title": "KV Cache eviction explained",
                                "kind": "video",
                                "play_url": "https://www.youtube.com/watch?v=abcdefghijk",
                                "extra": {"youtube_video_id": "abcdefghijk"},
                            }
                        ]
                    }
                )
            return CapabilityResult.ok(
                {
                    "text_items": [
                        {
                            "id": "paper-kv-cache-001",
                            "title": "KV Cache Eviction Boundaries",
                            "doi": "10.1000/kv-cache",
                            "url": "https://doi.org/10.1000/kv-cache",
                        }
                    ]
                }
            )
        if act == "lookup_provenance":
            return CapabilityResult.ok({"read_only": True, "matches": []})
        if act == "push_interactive_set":
            return CapabilityResult.ok({"pushed": True})
        return CapabilityResult.fail("unexpected", error_code="invalid_argument")

    ctx.capabilities.register("recommend.feed.v1", handle_feed, plugin_id="plugin-ai-curator")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    res = await ctx.capabilities.call(
        "ai.curation.v1",
        {
            "action": "curate_interactive",
            "prompt": "结合 KV Cache 淘汰边界论文，推荐 2 篇同主题实证工作和对应 YouTube 讲解。",
            "domains": ["media", "text"],
            "session_id": "ix-mixed",
            "sync_apple_music": "none",
        },
    )

    assert res.ok is True
    assert [(s.get("domain"), s.get("query")) for s in searches] == [
        ("video", "KV Cache"),
        ("text", "KV Cache"),
    ]
    assert [item["id"] for item in res.data["media_items"]] == ["yt-kv-cache-001"]
    assert [item["id"] for item in res.data["text_items"]] == ["paper-kv-cache-001"]

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_run_daily_pipeline_guards_dry_run_and_partial_success(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("skill_dir", str(_create_skill_fixture(tmp_path)))

    calls: list[tuple[str, Any]] = []
    dry_run_fail = {"flag": False}
    sync_fail = {"flag": False}

    def handle_feed(req: dict[str, Any]) -> CapabilityResult:
        calls.append(("feed", req.get("action")))
        return CapabilityResult.ok({"published": True})

    def handle_playlist(req: dict[str, Any]) -> CapabilityResult:
        calls.append(("playlist", req.get("dry_run")))
        if req.get("dry_run"):
            if dry_run_fail["flag"]:
                return CapabilityResult.fail("transaction not committed", error_code="needs_verified_bundle")
            return CapabilityResult.ok({"dry_run": True, "verified": True})
        if sync_fail["flag"]:
            return CapabilityResult.fail("Apple Music cloud timeout", error_code="timeout")
        return CapabilityResult.ok({"synced": True, "added_count": 5})

    ctx.capabilities.register("recommend.feed.v1", handle_feed, plugin_id="plugin-ai-curator")
    ctx.capabilities.register("media.playlist.v1", handle_playlist, plugin_id="plugin-music")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    evidence = {
        "status": "PUBLISHED",
        "publication_verified": True,
        "operation_id": "music:2026-11-17",
        "project": "music",
        "business_date": "2026-11-17",
        "receipt": {"path": ".recommendation/transactions/music/2026-11-17.json", "bundle_sha256": "abc123"},
    }

    # 1. Reject non-daily_immutable mode
    bad_mode = await ctx.capabilities.call(
        "ai.curation.v1",
        {"action": "run_daily_pipeline", "mode": "interactive_ephemeral", "project": "music"},
    )
    assert bad_mode.ok is False and bad_mode.data["error_code"] == "invalid_argument"

    # 2. qa_verified alone MUST return needs_verified_bundle
    qa_only = await ctx.capabilities.call(
        "ai.curation.v1",
        {"action": "run_daily_pipeline", "mode": "daily_immutable", "project": "music", "business_date": "2026-11-17", "qa_verified": True},
    )
    assert qa_only.ok is False and qa_only.data["error_code"] == "needs_verified_bundle"
    assert calls == []

    # 3. dry_run failure blocks publish_daily_bundle
    dry_run_fail["flag"] = True
    dry_fail_res = await ctx.capabilities.call(
        "ai.curation.v1",
        {
            "action": "run_daily_pipeline",
            "mode": "daily_immutable",
            "project": "music",
            "business_date": "2026-11-17",
            "qa_verified": True,
            "publication_evidence": evidence,
            "track_ids": ["1", "2", "3", "4", "5"],
        },
    )
    assert dry_fail_res.ok is False
    assert dry_fail_res.data["feed_published"] is False
    assert ("feed", "publish_daily_bundle") not in calls

    # 4. Feed published + playlist failure -> ok=False AND feed_published=True AND partial_success=True
    calls.clear()
    dry_run_fail["flag"] = False
    sync_fail["flag"] = True
    part = await ctx.capabilities.call(
        "ai.curation.v1",
        {
            "action": "run_daily_pipeline",
            "mode": "daily_immutable",
            "project": "music",
            "business_date": "2026-11-17",
            "qa_verified": True,
            "publication_evidence": evidence,
            "track_ids": ["1", "2", "3", "4", "5"],
        },
    )
    assert part.ok is False
    assert part.data["feed_published"] is True
    assert part.data["partial_success"] is True
    assert part.data["playlist_synced"] is False
    assert "Apple Music cloud timeout" in part.data["playlist_error"]
    assert calls == [("playlist", True), ("feed", "publish_daily_bundle"), ("playlist", False)]

    # 5. Full success
    calls.clear()
    sync_fail["flag"] = False
    ok_res = await ctx.capabilities.call(
        "ai.curation.v1",
        {
            "action": "run_daily_pipeline",
            "mode": "daily_immutable",
            "project": "music",
            "business_date": "2026-11-17",
            "qa_verified": True,
            "publication_evidence": evidence,
            "track_ids": ["1", "2", "3", "4", "5"],
        },
    )
    assert ok_res.ok is True
    assert ok_res.data["feed_published"] is True
    assert ok_res.data["partial_success"] is False
    assert ok_res.data["playlist_synced"] is True
    assert calls == [("playlist", True), ("feed", "publish_daily_bundle"), ("playlist", False)]

    # 6. English daily pipeline passes ephemeral_chat_note transiently without playlist sync
    eng_ev = {**evidence, "operation_id": "english:2026-11-17", "project": "english"}
    eng_res = await ctx.capabilities.call(
        "ai.curation.v1",
        {
            "action": "run_daily_pipeline",
            "mode": "daily_immutable",
            "project": "english",
            "business_date": "2026-11-17",
            "qa_verified": True,
            "publication_evidence": eng_ev,
            "ephemeral_chat_note": "今日饮食：清蒸鲈鱼",
        },
    )
    assert eng_res.ok is True and eng_res.data["feed_published"] is True
    assert eng_res.data["ephemeral_chat_note"] == "今日饮食：清蒸鲈鱼"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_curate_interactive_chart_prompt_uses_live_chart_without_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("skill_dir", str(_create_skill_fixture(tmp_path)))
    ctx.set_config("gateway_url", _closed_loopback_url())
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    chart_requests: list[dict[str, Any]] = []
    feed_requests: list[dict[str, Any]] = []

    def handle_charts(req: dict[str, Any]) -> CapabilityResult:
        chart_requests.append(dict(req))
        return CapabilityResult.ok({
            "storefront": "cn",
            "chart": "most-played",
            "items": [
                {
                    "id": f"am-cn-{sid}",
                    "title": f"Chart Track {sid}",
                    "creator": f"Chart Artist {sid}",
                    "kind": "music",
                    "subtitle": f"Chart Artist {sid} · 国区实时榜 #{rank}",
                    "source_badge": f"Apple Music CN 实时榜 #{rank}",
                    "play_url": f"https://music.apple.com/cn/song/{sid}",
                    "extra": {"apple_music_song_id": sid, "song_id": sid, "chart_rank": rank},
                }
                for rank, sid in enumerate(("9001", "9002", "9003"), start=1)
            ],
        })

    def handle_playlist(req: dict[str, Any]) -> CapabilityResult:
        if req.get("action") == "preflight_cn_catalog":
            ids = [str(x) for x in req.get("track_ids") or []]
            return CapabilityResult.ok({"verified_ids": ids, "songs": []})
        return CapabilityResult.ok({})

    def handle_feed(req: dict[str, Any]) -> CapabilityResult:
        feed_requests.append(dict(req))
        if req.get("action") == "lookup_provenance":
            return CapabilityResult.ok({"matches": []})
        if req.get("action") == "push_interactive_set":
            return CapabilityResult.ok({"session_id": req.get("session_id"), "accepted": True})
        raise AssertionError(f"chart path must not call {req.get('action')}")

    async def fail_llm(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("chart fast path must not call the LLM selector")

    ctx.capabilities.register("media.charts.v1", handle_charts, plugin_id="plugin-music")
    ctx.capabilities.register("media.playlist.v1", handle_playlist, plugin_id="plugin-music")
    ctx.capabilities.register("recommend.feed.v1", handle_feed, plugin_id="plugin-ai-curator")
    monkeypatch.setattr(module._curation_mod, "llm_select_interactive", fail_llm)

    try:
        res = await ctx.capabilities.call(
            "ai.curation.v1",
            {
                "action": "curate_interactive",
                "mode": "interactive_ephemeral",
                "session_id": "ix-chart-test",
                "prompt": "写三首目前最火的歌到歌单",
                "domains": ["media"],
                "sync_apple_music": "none",
            },
        )
        assert res.ok
        assert chart_requests and chart_requests[0]["limit"] >= 6
        assert [item["extra"]["chart_rank"] for item in res.data["media_items"]] == [1, 2, 3]
        assert [item["extra"]["apple_music_song_id"] for item in res.data["media_items"]] == ["9001", "9002", "9003"]
        assert res.data["feed_written"] is True
        assert res.data["playlist_synced"] is False
        assert [r["action"] for r in feed_requests] == ["lookup_provenance", "push_interactive_set"]
        assert feed_requests[1]["title"] == "Apple Music 国区实时热门 · 3 首"
    finally:
        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
