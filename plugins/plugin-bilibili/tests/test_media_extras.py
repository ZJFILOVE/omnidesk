"""Offline tests for danmaku XML, subtitles JSON, optional capabilities, graceful degradation, and EntityBus."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(PLUGIN_DIR), str(REPO_ROOT), str(REPO_ROOT / "core"), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from bilibili_impl.parsers import SecurityValidationError, parse_danmaku_xml  # noqa: E402
from mock_bili import MockBiliServer, MockBiliState  # noqa: E402
from omnidesk_sdk import CapabilityResult, ChapterSegment, LyricLine, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _make_ctx(tmp_path: Path, api_base: str) -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"api_base": api_base, "request_timeout_sec": 2, "max_retries": 0})
    return ctx


@pytest.mark.asyncio
async def test_danmaku_xml_plain_deflate_and_xxe_blocking(tmp_path: Path) -> None:
    state = MockBiliState(deflate_danmaku=False)
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        ctx = _make_ctx(tmp_path, srv.base_url)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        res1 = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_danmaku", "bvid": "BV1xK4y1Z7aa"})
        assert res1.ok is True
        assert res1.data["count"] == 2
        assert [d["time_sec"] for d in res1.data["danmaku"]] == [3.2, 12.5]
        assert res1.data["danmaku"][0]["mode_label"] == "top"
        assert res1.data["danmaku"][0]["color_hex"] == "#FF0000"
        assert res1.data["danmaku"][1]["text"] == "后发先至"

        state.deflate_danmaku = True
        res2 = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_danmaku", "cid": 88001})
        assert res2.ok is True and res2.data["count"] == 2

        with pytest.raises(SecurityValidationError) as exc:
            parse_danmaku_xml('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><i><d p="1,1,25,0,1">x</d></i>')
        assert exc.value.error_code == "path_safety"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_subtitles_asr_translation_and_graceful_degradation(tmp_path: Path) -> None:
    state = MockBiliState(has_subtitles=True)
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        ctx = _make_ctx(tmp_path, srv.base_url)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # 1) Bilibili JSON subtitles present, llm.translate.v1 missing -> graceful degradation
        sub1 = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_subtitles", "bvid": "BV1xK4y1Z7aa"})
        assert sub1.ok is True
        assert sub1.data["source"] == "bilibili"
        assert sub1.data["count"] == 2
        assert sub1.data["subtitles"][0]["original"] == "Hello Wayland"
        assert sub1.data["subtitles"][0]["translation"] == ""
        assert "llm.translate.v1" in sub1.data["degraded_capabilities"]

        # 2) No Bilibili subtitles, speech.asr.v1 missing -> graceful degradation
        state.has_subtitles = False
        sub2 = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_subtitles", "bvid": "BV1xK4y1Z7aa"})
        assert sub2.ok is True
        assert sub2.data["count"] == 0
        assert sub2.data["source"] == "none"
        assert "speech.asr.v1" in sub2.data["degraded_capabilities"]

        src = ctx.domains.media_sources()[0]
        assert await src.fetch_lyrics("BV1xK4y1Z7aa") == []

        # 3) Register optional speech.asr.v1 + llm.translate.v1 -> ASR + bilingual translation
        ctx.register_capability(
            "speech.asr.v1",
            lambda req: CapabilityResult.ok(
                {"cues": [{"start_ms": 500, "end_ms": 2000, "text": "Hardware decode ready"}]}
            ),
        )
        ctx.register_capability(
            "llm.translate.v1",
            lambda req: CapabilityResult.ok({"translations": [f"译:{t}" for t in req["texts"]]}),
        )
        sub3 = await ctx.capabilities.call("bilibili.media.v1", {"action": "fetch_subtitles", "bvid": "BV1xK4y1Z7aa"})
        assert sub3.ok is True
        assert sub3.data["source"] == "asr"
        assert sub3.data["degraded"] is False
        assert sub3.data["subtitles"][0]["original"] == "Hardware decode ready"
        assert sub3.data["subtitles"][0]["translation"] == "译:Hardware decode ready"

        # 4) MediaSource.fetch_lyrics returns bilingual LyricLine list when Bilibili subtitles exist
        state.has_subtitles = True
        lyrics = await src.fetch_lyrics("BV1xK4y1Z7aa")
        assert len(lyrics) == 2
        assert isinstance(lyrics[0], LyricLine)
        assert lyrics[0].translation == "译:Hello Wayland"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_chapters_sponsorblock_workspace_tab_and_entity_media_open(tmp_path: Path) -> None:
    state = MockBiliState(multi_part=True)
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        ctx = _make_ctx(tmp_path, srv.base_url)
        await invoke_lifecycle_hook(plugin.on_load, ctx)
        src = ctx.domains.media_sources()[0]

        # Multi-P chapters without player.timeline.v1
        chs1 = await src.fetch_chapters("BV1xK4y1Z7aa")
        assert len(chs1) == 2
        assert isinstance(chs1[0], ChapterSegment)
        assert chs1[0].label == "P1 导论" and chs1[1].start_sec == 300.0

        # Single-P without view_points and without SponsorBlock -> []
        state.multi_part = False
        assert await src.fetch_chapters("BV1xK4y1Z7aa") == []

        # With failing player.timeline.v1 -> gracefully degrades to real Bilibili chapters
        state.multi_part = True
        ctx.register_capability(
            "player.timeline.v1",
            lambda req: CapabilityResult.fail("bsbsb.top offline", error_code="service_offline"),
        )
        chs_deg = await src.fetch_chapters("BV1xK4y1Z7aa")
        assert [c.label for c in chs_deg] == ["P1 导论", "P2 进阶实现"]

        # Register working player.timeline.v1 -> SponsorBlock segments merged & sorted
        ctx.register_capability(
            "player.timeline.v1",
            lambda req: CapabilityResult.ok(
                {"segments": [{"id": "sb-1", "start_sec": 120.0, "end_sec": 165.0, "label": "恰饭推广", "kind": "ad"}]}
            ),
        )
        chs2 = await src.fetch_chapters("BV1xK4y1Z7aa")
        assert [c.label for c in chs2] == ["P1 导论", "恰饭推广", "P2 进阶实现"]
        assert chs2[1].kind == "ad"

        # Publish entity.media.open on ctx.bus via open_media and WorkspaceTab action
        bus_events: list[dict] = []
        ctx.subscribe("entity.media.open", lambda evt: bus_events.append(evt.payload))
        opened = await ctx.capabilities.call("bilibili.media.v1", {"action": "open_media", "bvid": "BV1xK4y1Z7aa", "quality": "1080p"})
        assert opened.ok is True and opened.data["opened"] is True
        assert len(bus_events) == 1
        assert bus_events[0]["item_id"] == "BV1xK4y1Z7aa"
        assert bus_events[0]["source_id"] == "plugin-bilibili"
        assert bus_events[0]["play_url"].endswith("/cdn/1080.m4s")

        tab = ctx.domains.workspace_tabs("media")[0]
        assert tab.on_action is not None
        tab_res = await tab.on_action("status", {})
        assert tab_res["ok"] is False and tab_res["data"]["logged_in"] is False

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
