"""Offline tests for interactive isolation, MediaSource, bilingual lyrics, and secrets."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from conftest import (
    FakeAppleMusicState,
    PLUGIN_ROOT,
    REPO_ROOT,
    build_plugin_context,
    make_tracks,
    run_async,
    write_daily_fixture,
)
from omnidesk_core import PluginManager
from qwen_bridge import load_qwen_modules
from state_store import (
    DEFAULT_INTERACTIVE_PLAYLIST,
    PROTECTED_DAILY_PLAYLIST_ID,
    resolve_daily_checkpoint_path,
    resolve_playback_queue_path,
)


def _err(res: Any) -> str:
    return str(res.data.get("error_code") or "")


def test_interactive_blocks_protected_daily_playlist_and_unverified_tracks(
    tmp_path: Path, fake_apple_server: tuple[FakeAppleMusicState, str]
) -> None:
    state, url = fake_apple_server
    state.seed_playable("7001")
    _, ctx = build_plugin_context(tmp_path, api_base=url)

    for bad_target in (
        {"playlist_id": PROTECTED_DAILY_PLAYLIST_ID},
        {"playlist_name": "ChatGPT Daily 5"},
        {"playlist_name": "ChatGPT Daily 5 · 2026-11-13"},
        {"cumulative_playlist_id": PROTECTED_DAILY_PLAYLIST_ID},
    ):
        res = run_async(
            ctx.capabilities.call(
                "media.playlist.v1",
                {
                    "action": "write_interactive_playlist",
                    "mode": "interactive_ephemeral",
                    "storefront": "cn",
                    "track_ids": ["7001"],
                    "verified_ids": ["7001"],
                    **bad_target,
                },
            )
        )
        assert not res.ok
        assert _err(res) == "protected_daily_playlist"

    enq_blocked = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "enqueue_playback",
                "mode": "interactive_ephemeral",
                "playlist_id": PROTECTED_DAILY_PLAYLIST_ID,
                "track_ids": ["7001"],
                "verified_ids": ["7001"],
            },
        )
    )
    assert not enq_blocked.ok
    assert _err(enq_blocked) == "protected_daily_playlist"

    no_verified = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "write_interactive_playlist",
                "mode": "interactive_ephemeral",
                "track_ids": ["7001"],
            },
        )
    )
    assert not no_verified.ok
    assert _err(no_verified) == "invalid_argument"

    partial_verified = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "write_interactive_playlist",
                "mode": "interactive_ephemeral",
                "track_ids": ["7001", "7002"],
                "verified_ids": ["7001"],
            },
        )
    )
    assert not partial_verified.ok
    assert _err(partial_verified) == "invalid_argument"

    forbid_history = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "write_interactive_playlist",
                "mode": "interactive_ephemeral",
                "track_ids": ["7001"],
                "verified_ids": ["7001"],
                "record_permanent_history": True,
            },
        )
    )
    assert not forbid_history.ok
    assert _err(forbid_history) == "invalid_argument"


def test_interactive_local_staging_cloud_sync_repeats_and_enqueue_playback(
    tmp_path: Path, fake_apple_server: tuple[FakeAppleMusicState, str]
) -> None:
    state, url = fake_apple_server
    state.seed_playable("8001", title="Daily Repeat Track", artist="Nils Frahm")
    state.seed_playable("8002", title="Evening Glow", artist="Ólafur Arnalds")
    _, ctx = build_plugin_context(tmp_path, api_base=url)
    ckpt = resolve_daily_checkpoint_path("", ctx.data_dir)

    pre = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {"action": "preflight_cn_catalog", "storefront": "cn", "track_ids": ["8001", "8002"]},
        )
    )
    assert pre.ok
    verified = pre.data["verified_ids"]

    local_only = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "write_interactive_playlist",
                "mode": "interactive_ephemeral",
                "track_ids": ["8001"],
                "verified_ids": verified,
                "local_checkpoint_only": True,
            },
        )
    )
    assert local_only.ok
    assert local_only.data["local_staged"] is True
    assert local_only.data["cloud_synced"] is False
    assert local_only.data["live_apple_music_synced"] is False
    assert local_only.data["playlist_name"] == DEFAULT_INTERACTIVE_PLAYLIST
    assert not [r for r in state.requests if r[0] == "POST"]

    synced = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "write_interactive_playlist",
                "mode": "interactive_ephemeral",
                "track_ids": ["8001", "8002"],
                "verified_ids": verified,
                "enqueue_now": True,
            },
        )
    )
    assert synced.ok
    assert synced.data["cloud_synced"] is True
    assert synced.data["record_permanent_history"] is False
    assert synced.data["enqueued"] is True
    assert not ckpt.exists()
    assert state.playlists[PROTECTED_DAILY_PLAYLIST_ID]["tracks"] == []

    qfile = resolve_playback_queue_path(ctx.data_dir)
    assert stat.S_IMODE(qfile.stat().st_mode) == 0o600
    assert json.loads(qfile.read_text(encoding="utf-8"))["backend"] == "libmpv"
    assert [ev.topic for ev in ctx.bus.history("entity.media.open")] == ["entity.media.open"]


def test_media_source_empty_never_fabricates_and_reads_real_archive_and_lyrics(tmp_path: Path) -> None:
    # 1. Empty archive returns [] without fabricating any tracks.
    _, empty_ctx = build_plugin_context(tmp_path / "empty")
    empty_res = run_async(
        empty_ctx.capabilities.call("media.source.v1", {"action": "fetch_items", "category": "每日 5 首·全量"})
    )
    assert empty_res.ok
    assert empty_res.data["items"] == []

    bad_src = run_async(empty_ctx.capabilities.call("media.source.v1", {"action": "fetch_items", "extra_arg": 1}))
    assert not bad_src.ok
    assert _err(bad_src) == "invalid_argument"

    # 2. Populated dual repos + bilingual cached lyrics parsed via qwen-translator/music_lyrics.py.
    music_repo = tmp_path / "music-daily"
    vault_repo = tmp_path / "obsidian-vault"
    music_repo.mkdir()
    vault_repo.mkdir()
    write_daily_fixture(music_repo, vault_repo, day="2026-11-06", tracks=make_tracks(["11", "12", "14", "15"], [1, 2, 4, 5]))
    write_daily_fixture(music_repo, vault_repo, day="2026-11-13", tracks=make_tracks(["21", "22", "23", "24", "25"]))

    qwen_dir = Path("/home/zhu/Projects/qwen-translator")
    qwen_probe = load_qwen_modules(str(qwen_dir))
    assert qwen_probe.available is True

    _, ctx = build_plugin_context(tmp_path / "rich", music_repo=music_repo, vault_repo=vault_repo)
    lyrics_dir = ctx.data_dir / "lyrics"
    lyrics_dir.mkdir(parents=True, exist_ok=True)
    (lyrics_dir / "21.bilingual.lrc").write_text(
        "[00:01.50]Silent morning light\n[00:01.50]清晨微光\n[00:05.00]Drifting through the trees\n",
        encoding="utf-8",
    )

    source = next(s for s in ctx.domains.media_sources() if s.source_id == "plugin-music")
    assert set(source.categories()) >= {"每日 5 首·最新", "每日 5 首·往期", "每日 5 首·全量"}

    latest = run_async(source.fetch_items("每日 5 首·最新"))
    past = run_async(source.fetch_items("每日 5 首·往期"))
    all_items = run_async(source.fetch_items("每日 5 首·全量"))
    assert len(latest) == 5
    assert len(past) == 4
    assert len(all_items) == 9
    first = latest[0]
    assert first.extra["canonical_id"] == "Artist 1::Song 21"
    assert first.extra["apple_music_song_id"] == "21"
    assert first.extra["business_date"] == "2026-11-13"
    assert first.extra["curation_mode"] == "daily_immutable"
    assert first.extra["listening_cue"] == "聆听提示 21"
    assert first.extra["analysis_summary"] == "解析摘要 21"

    lyrics = run_async(source.fetch_lyrics(first.id))
    assert len(lyrics) == 2
    assert lyrics[0].original == "Silent morning light"
    assert lyrics[0].translation == "清晨微光"


def test_secrets_never_leak_and_plugin_manager_verifies_manifest_and_lifecycle(tmp_path: Path) -> None:
    secret_dev = "SECRET_DEV_SHOULD_NEVER_LEAK"
    secret_user = "SECRET_USER_SHOULD_NEVER_LEAK"
    secret_ck = "SECRET_CK_SHOULD_NEVER_LEAK"
    plugin, ctx = build_plugin_context(
        tmp_path,
        dev_token=secret_dev,
        user_token=secret_user,
        cookie=secret_ck,
    )
    status = run_async(ctx.capabilities.call("media.playlist.v1", {"action": "status"}))
    health = run_async(plugin.on_health_check())
    blob = json.dumps({"status": status.data, "health": health}, ensure_ascii=False)
    for token in (secret_dev, secret_user, secret_ck):
        assert token not in blob

    mgr = PluginManager(plugins_dir=REPO_ROOT / "plugins")
    discovered = {r.plugin_id: r for r in mgr.discover()}
    assert "plugin-music" in discovered
    manifest_rec = discovered["plugin-music"].manifest
    assert manifest_rec is not None
    assert manifest_rec.schema_version == 2
    assert {"media.playlist.v1", "media.source.v1"} <= set(manifest_rec.provides)
    manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    secret_fields = [f for f in manifest["config_schema"] if f["type"] == "secret"]
    assert {f["key"] for f in secret_fields} == {
        "apple_music_developer_token",
        "apple_music_user_token",
        "apple_music_cookie",
    }


def test_finalize_lyrics_returns_original_lines_when_translation_times_out(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio
    from omnidesk_sdk import LyricLine, MediaItem
    from sources import AppleMusicMediaSource

    async def slow_translate(texts: list[str], title: str) -> tuple[list[str] | None, str]:
        await asyncio.sleep(5.0)
        return ["翻译"], ""

    async def fake_adam_feed(cat: str, query: str, page: int) -> list[MediaItem]:
        return [
            MediaItem(
                id="adam:songs:1440873510",
                source_id="plugin-music",
                kind="music",
                title="State of Grace",
                creator="Taylor Swift",
                subtitle="Red",
                duration_sec=295.0,
                extra={"apple_music_song_id": "1440873510"},
            )
        ]

    source = AppleMusicMediaSource(
        get_config=lambda: {"auto_translate_lyrics": True},
        get_data_dir=lambda: tmp_path,
        translate_lines=slow_translate,
        adam_feed=fake_adam_feed,
    )

    items = run_async(source.fetch_items("search", "State of Grace", 1))
    assert len(items) == 1
    assert source._resolve_track_metadata("1440873510") == ("State of Grace", "Taylor Swift", "Red", 295.0)

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout=None):
        return await real_wait_for(coro, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
    lines = [LyricLine(time_sec=1.0, original="I'm walking fast through the traffic lights", translation="")]
    result = run_async(source._finalize_lyrics("1440873510", lines, "State of Grace"))
    assert len(result) == 1
    assert result[0].original == "I'm walking fast through the traffic lights"
    assert source.last_translation_error == "translation_timeout"
