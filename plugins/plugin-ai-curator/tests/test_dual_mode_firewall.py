"""Dual-mode firewall, PUBLISHED evidence/receipt verification, lookup_provenance read-only, and session action tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

from conftest import PLUGIN_DIR, populate_full_archive_fixtures


def _setup_curator(monkeypatch, tmp_path: Path, obs_dir: Path, mus_dir: Path, curation_root: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    from plugin import create_plugin

    plugin = create_plugin()
    ctx = PluginContext(
        "plugin-ai-curator",
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "xdg-data" / "omnidesk" / "plugin-data" / "plugin-ai-curator",
        config_dir=tmp_path / "xdg-config" / "omnidesk" / "plugins" / "plugin-ai-curator",
    )
    ctx.set_config(
        {
            "obsidian_vault_dir": str(obs_dir),
            "music_daily_dir": str(mus_dir),
            "curation_root": str(curation_root),
            "sync_remote": False,
        }
    )
    return plugin, ctx


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    if not root.exists():
        return {}
    snap: dict[str, tuple[int, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            snap[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return snap


def test_publish_daily_bundle_gates_receipt_firewall_and_notes_preservation(monkeypatch, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obsidian-vault"
    mus_dir = tmp_path / "music-daily"
    curation_root = tmp_path / "curation-root"
    populate_full_archive_fixtures(obs_dir, mus_dir)
    plugin, ctx = _setup_curator(monkeypatch, tmp_path, obs_dir, mus_dir, curation_root)

    from models import current_shanghai_date

    today = current_shanghai_date()
    events: list[tuple[str, dict[str, Any]]] = []
    ctx.bus.subscribe("entity.recommend.*", lambda ev: events.append((ev.topic, dict(ev.payload))))

    sm2_calls: list[dict[str, Any]] = []

    async def fake_sm2(req: dict[str, Any]) -> CapabilityResult:
        sm2_calls.append(dict(req))
        if req.get("action") == "list":
            return CapabilityResult.ok({"cards": [{"word": "procrastinate", "interval": 14}]})
        return CapabilityResult.ok({"word": req.get("word"), "created": True})

    ctx.register_capability("vocab.sm2.v1", fake_sm2)

    valid_bundle_items = {
        "media_items": [
            {
                "id": "track-today-1",
                "kind": "music",
                "title": "Brand New Track",
                "creator": "Brand New Artist",
                "play_url": "https://music.apple.com/cn/song/999888777",
                "extra": {"canonical_id": "Brand New Artist::Brand New Track"},
            }
        ],
        "text_items": [
            {
                "id": "news-today-1",
                "category": "news",
                "title": "Brand New Policy Brief",
                "url": "https://example.org/news/today-brief",
                "extra": {"why_recommended": "Clean structural analysis.", "listening_or_reading_cue": "Check Section 2."},
            }
        ],
        "vocab_cards": [
            {"word": "procrastinate", "meaning": "delay", "source_book": f"English · {today}", "source_date": today},
            {"word": "epistemic", "meaning": "relating to knowledge", "source_book": f"English · {today}", "source_date": today},
        ],
    }

    async def scenario() -> None:
        await plugin.on_load(ctx)
        await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})

        # 1. Wrong mode rejected
        r_mode = await plugin.handle_recommend_feed(
            {"action": "publish_daily_bundle", "mode": "interactive_ephemeral", "qa_verified": True}
        )
        assert r_mode.ok is False and r_mode.data["error_code"] == "invalid_curation_mode"

        # 2. qa_verified=False rejected
        r_qa = await plugin.handle_recommend_feed(
            {"action": "publish_daily_bundle", "mode": "daily_immutable", "qa_verified": False}
        )
        assert r_qa.ok is False and r_qa.data["error_code"] == "qa_not_verified"

        # 3. qa_verified=True alone without publication_evidence -> needs_verified_bundle
        r_no_ev = await plugin.handle_recommend_feed(
            {
                "action": "publish_daily_bundle",
                "mode": "daily_immutable",
                "qa_verified": True,
                "business_date": today,
                "project": "daily",
                "operation_id": f"daily:{today}",
            }
        )
        assert r_no_ev.ok is False and r_no_ev.data["error_code"] == "needs_verified_bundle"

        # 4. PREPARED status or missing receipt or mismatched operation_id -> needs_verified_bundle
        for bad_ev in (
            {"status": "PREPARED", "publication_verified": True, "operation_id": f"daily:{today}", "business_date": today, "project": "daily", "receipt": {"id": "r1"}},
            {"status": "PUBLISHED", "publication_verified": True, "operation_id": f"daily:{today}", "business_date": today, "project": "daily", "receipt": {}},
            {"status": "PUBLISHED", "publication_verified": True, "operation_id": "daily:1999-01-01", "business_date": today, "project": "daily", "receipt": "sha256:abc"},
        ):
            r_bad_ev = await plugin.handle_recommend_feed(
                {
                    "action": "publish_daily_bundle",
                    "mode": "daily_immutable",
                    "qa_verified": True,
                    "business_date": today,
                    "project": "daily",
                    "operation_id": f"daily:{today}",
                    "publication_evidence": bad_ev,
                }
            )
            assert r_bad_ev.ok is False and r_bad_ev.data["error_code"] == "needs_verified_bundle"

        # 5. Historical silent backfill forbidden
        r_past = await plugin.handle_recommend_feed(
            {
                "action": "publish_daily_bundle",
                "mode": "daily_immutable",
                "qa_verified": True,
                "business_date": "2026-11-10",
                "project": "daily",
                "operation_id": "daily:2026-11-10",
                "publication_evidence": {
                    "status": "PUBLISHED",
                    "publication_verified": True,
                    "operation_id": "daily:2026-11-10",
                    "business_date": "2026-11-10",
                    "project": "daily",
                    "receipt": {"id": "rcpt-past"},
                },
            }
        )
        assert r_past.ok is False and r_past.data["error_code"] == "historical_backfill_forbidden"

        # 6. On-disk PREPARED transaction blocks publish unless published run receipt exists
        tx_file = obs_dir / ".recommendation" / "transactions" / "daily" / f"{today}.json"
        tx_file.parent.mkdir(parents=True, exist_ok=True)
        tx_file.write_text(
            json.dumps({"status": "PREPARED", "plan": {"operation_id": f"daily:{today}"}}),
            encoding="utf-8",
        )
        r_prepared = await plugin.handle_recommend_feed(
            {
                "action": "publish_daily_bundle",
                "mode": "daily_immutable",
                "qa_verified": True,
                "business_date": today,
                "project": "daily",
                "operation_id": f"daily:{today}",
                "publication_evidence": {
                    "status": "PUBLISHED",
                    "publication_verified": True,
                    "operation_id": f"daily:{today}",
                    "business_date": today,
                    "project": "daily",
                    "receipt": {"id": "rcpt-1"},
                },
                **valid_bundle_items,
            }
        )
        assert r_prepared.ok is False and r_prepared.data["error_code"] == "needs_verified_bundle"
        tx_file.unlink()

        # 7. Duplicate in permanent history rejected fail-closed
        r_dup = await plugin.handle_recommend_feed(
            {
                "action": "publish_daily_bundle",
                "mode": "daily_immutable",
                "qa_verified": True,
                "business_date": today,
                "project": "daily",
                "operation_id": f"daily:{today}",
                "publication_evidence": {
                    "status": "PUBLISHED",
                    "publication_verified": True,
                    "operation_id": f"daily:{today}",
                    "business_date": today,
                    "project": "daily",
                    "receipt": {"id": "rcpt-dup"},
                },
                "media_items": [
                    {
                        "id": "dup-muerte",
                        "kind": "music",
                        "title": "Muerte",
                        "creator": "Natalia Lafourcade",
                        "play_url": "https://music.apple.com/cn/song/1643009587",
                    }
                ],
            }
        )
        assert r_dup.ok is False and r_dup.data["error_code"] == "duplicate_in_permanent_history"

        # 8. Valid PUBLISHED bundle succeeds, strips ephemeral_chat_note from disk, preserves ## Notes
        note_target = obs_dir / "Music" / "Daily" / "2026-11-13.md"
        events.clear()
        r_ok = await plugin.handle_recommend_feed(
            {
                "action": "publish_daily_bundle",
                "mode": "daily_immutable",
                "qa_verified": True,
                "business_date": today,
                "project": "daily",
                "operation_id": f"daily:{today}",
                "publication_evidence": {
                    "status": "PUBLISHED",
                    "publication_verified": True,
                    "operation_id": f"daily:{today}",
                    "business_date": today,
                    "project": "daily",
                    "receipt": {"id": f"tx-{today}", "commit_sha": "deadbeef"},
                },
                "ephemeral_chat_note": "今日饮食：清蒸鲈鱼配糙米，绝不落盘。",
                "markdown_updates": [
                    {"path": "Music/Daily/2026-11-13.md", "auto_block": "# Refreshed Header Only"},
                ],
                **valid_bundle_items,
            }
        )
        assert r_ok.ok is True
        bundle_path = Path(r_ok.data["bundle_path"])
        assert bundle_path.is_file()
        assert bundle_path == curation_root / "daily" / today / "daily.json"

        # ephemeral_chat_note emitted via event bus but NEVER persisted anywhere on disk
        bundle_text = bundle_path.read_text(encoding="utf-8")
        assert "清蒸鲈鱼" not in bundle_text
        topics = [t for t, _ in events]
        assert "entity.recommend.ephemeral_note" in topics
        assert "entity.recommend.updated" in topics

        # ## Notes preserved outside AUTO:START/END
        updated_md = note_target.read_text(encoding="utf-8")
        assert "# Refreshed Header Only" in updated_md
        assert "深夜耳机试听第 1 首吉他的空气感非常出色。" in updated_md

        # SM-2 skipped procrastinate, added epistemic
        assert r_ok.data["vocab_sync"]["added"] == 1
        assert r_ok.data["vocab_sync"]["skipped"] == 1
        assert any(c.get("action") == "add" and c.get("word") == "epistemic" for c in sm2_calls)

        await plugin.on_unload()

    asyncio.run(scenario())


def test_push_interactive_isolation_revisit_and_session_dedup(monkeypatch, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obsidian-vault"
    mus_dir = tmp_path / "music-daily"
    curation_root = tmp_path / "curation-root"
    populate_full_archive_fixtures(obs_dir, mus_dir)
    plugin, ctx = _setup_curator(monkeypatch, tmp_path, obs_dir, mus_dir, curation_root)

    async def scenario() -> None:
        await plugin.on_load(ctx)
        await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})

        # Take pre-push snapshots of ledgers & daily directory
        rec_before = _snapshot_tree(obs_dir / ".recommendation")
        state_before = _snapshot_tree(mus_dir / "state")
        daily_before = _snapshot_tree(curation_root / "daily")

        # Reject daily_immutable mode on push_interactive_set
        bad_mode = await plugin.handle_recommend_feed(
            {"action": "push_interactive_set", "mode": "daily_immutable", "record_permanent_history": False, "session_id": "s1"}
        )
        assert bad_mode.ok is False and bad_mode.data["error_code"] == "invalid_curation_mode"

        # Reject missing or True record_permanent_history
        for bad_val in (None, True):
            payload: dict[str, Any] = {"action": "push_interactive_set", "mode": "interactive_ephemeral", "session_id": "s1"}
            if bad_val is not None:
                payload["record_permanent_history"] = bad_val
            bad_rec = await plugin.handle_recommend_feed(payload)
            assert bad_rec.ok is False and bad_rec.data["error_code"] == "ledger_pollution_forbidden"

        # Reject traversal session_id
        bad_sid = await plugin.handle_recommend_feed(
            {"action": "push_interactive_set", "mode": "interactive_ephemeral", "record_permanent_history": False, "session_id": "../evil"}
        )
        assert bad_sid.ok is False and bad_sid.data["error_code"] == "invalid_session_id"

        # Push valid interactive set revisiting archived items + testing session_dedup_only
        res1 = await plugin.handle_recommend_feed(
            {
                "action": "push_interactive_set",
                "mode": "interactive_ephemeral",
                "record_permanent_history": False,
                "session_id": "jazz-session-1",
                "repeat_policy": "session_dedup_only",
                "title": "深夜吉他灵感",
                "media_items": [
                    {
                        "id": "ix-revisit-muerte",
                        "kind": "music",
                        "title": "Muerte",
                        "creator": "Natalia Lafourcade",
                        "play_url": "https://music.apple.com/cn/song/1643009587",
                    },
                    {
                        "id": "ix-brand-new",
                        "kind": "music",
                        "title": "Midnight Acoustic",
                        "creator": "Indie Duo",
                        "play_url": "https://music.apple.com/cn/song/777666555",
                    },
                ],
                "text_items": [
                    {
                        "id": "ix-revisit-ksi",
                        "category": "journal",
                        "title": "Extreme electric fields power catalysis in ketosteroid isomerase",
                        "doi": "10.1126/science.1259802",
                    }
                ],
            }
        )
        assert res1.ok is True
        assert res1.data["record_permanent_history"] is False
        assert res1.data["revisit_matches"] == 2
        assert res1.data["media_items"][0]["previously_recommended_on"] == "2026-11-13"
        assert res1.data["media_items"][0]["extra"]["revisit_badge"] == "经典重温 · 2026-11-13"
        assert res1.data["text_items"][0]["previously_recommended_on"] == "2026-11-16"
        assert "经典重温 · 2026-11-16" in res1.data["text_items"][0]["tags"]

        # Duplicate inside SAME session with session_dedup_only is skipped
        res2 = await plugin.handle_recommend_feed(
            {
                "action": "push_interactive_set",
                "mode": "interactive_ephemeral",
                "record_permanent_history": False,
                "session_id": "jazz-session-1",
                "repeat_policy": "session_dedup_only",
                "media_items": [
                    {
                        "id": "ix-revisit-muerte-dup",
                        "kind": "music",
                        "title": "Muerte",
                        "creator": "Natalia Lafourcade",
                        "play_url": "https://music.apple.com/cn/song/1643009587",
                    }
                ],
            }
        )
        assert res2.ok is True
        assert res2.data["skipped_session_duplicates"] == 1
        assert res2.data["media_count"] == 0

        # Zero pollution on .recommendation/, music-daily/state/, or daily/
        assert _snapshot_tree(obs_dir / ".recommendation") == rec_before
        assert _snapshot_tree(mus_dir / "state") == state_before
        assert _snapshot_tree(curation_root / "daily") == daily_before
        assert (curation_root / "interactive" / "sessions" / "jazz-session-1.json").is_file()

        await plugin.on_unload()

    asyncio.run(scenario())


def test_lookup_provenance_strict_read_only_limit_and_matching_rules(monkeypatch, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obsidian-vault"
    mus_dir = tmp_path / "music-daily"
    curation_root = tmp_path / "curation-root"
    populate_full_archive_fixtures(obs_dir, mus_dir)
    plugin, ctx = _setup_curator(monkeypatch, tmp_path, obs_dir, mus_dir, curation_root)

    async def scenario() -> None:
        await plugin.on_load(ctx)
        await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})

        # Push an interactive-only song; lookup_provenance MUST NOT match it
        await plugin.handle_recommend_feed(
            {
                "action": "push_interactive_set",
                "mode": "interactive_ephemeral",
                "record_permanent_history": False,
                "session_id": "ephemeral-only",
                "media_items": [
                    {
                        "id": "ix-temp",
                        "kind": "music",
                        "title": "Temporary Jam",
                        "creator": "Session Artist",
                        "play_url": "https://music.apple.com/cn/song/555444333",
                    }
                ],
            }
        )

        # Fail on > 4096 items
        over_limit = await plugin.handle_recommend_feed(
            {"action": "lookup_provenance", "items": [{"id": f"i-{i}"} for i in range(4097)]}
        )
        assert over_limit.ok is False and over_limit.data["error_code"] == "too_many_items"

        # Guard against subprocesses, bus emissions, and filesystem mutations during lookup_provenance
        sub_calls: list[Any] = []
        bus_calls: list[str] = []

        async def trap_subprocess(*args, **kwargs):
            sub_calls.append(args)
            raise AssertionError("lookup_provenance must not execute git or subprocesses")

        monkeypatch.setattr(ctx, "exec_subprocess", trap_subprocess)
        ctx.bus.subscribe("entity.*", lambda ev: bus_calls.append(ev.topic))
        curation_before = _snapshot_tree(curation_root)
        obs_before = _snapshot_tree(obs_dir)

        res = await plugin.handle_recommend_feed(
            {
                "action": "lookup_provenance",
                "items": [
                    {"id": "by-song-id", "kind": "music", "play_url": "https://music.apple.com/cn/song/1643009587"},
                    {"id": "by-bare-music-title-must-miss", "kind": "music", "title": "Muerte", "creator": "Different Artist"},
                    {"id": "by-video", "kind": "video", "url": "https://youtu.be/1ZUIrDTN6wo"},
                    {"id": "by-doi", "kind": "journal", "doi": "HTTPS://DOI.ORG/10.1126/SCIENCE.1259802"},
                    {"id": "by-long-title", "kind": "journal", "title": "Extreme electric fields power catalysis in ketosteroid isomerase"},
                    {"id": "by-reddit", "kind": "reddit", "url": "https://www.reddit.com/r/instrumentation/comments/1n0o4w0/extra"},
                    {"id": "interactive-must-miss", "kind": "music", "play_url": "https://music.apple.com/cn/song/555444333"},
                ],
            }
        )
        assert res.ok is True
        assert res.data["read_only"] is True
        matched = {m["id"]: m["previously_recommended_on"] for m in res.data["matches"]}
        assert matched["by-song-id"] == "2026-11-13"
        assert matched["by-video"] == "2026-11-13"
        assert matched["by-doi"] == "2026-11-16"
        assert matched["by-long-title"] == "2026-11-16"
        assert matched["by-reddit"] == "2026-11-13"
        assert "by-bare-music-title-must-miss" not in matched
        assert "interactive-must-miss" not in matched
        assert sub_calls == []
        assert bus_calls == []
        assert _snapshot_tree(curation_root) == curation_before
        assert _snapshot_tree(obs_dir) == obs_before

        await plugin.on_unload()

    asyncio.run(scenario())


def test_pin_dismiss_clear_interactive_and_status(monkeypatch, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obsidian-vault"
    mus_dir = tmp_path / "music-daily"
    curation_root = tmp_path / "curation-root"
    populate_full_archive_fixtures(obs_dir, mus_dir)
    plugin, ctx = _setup_curator(monkeypatch, tmp_path, obs_dir, mus_dir, curation_root)

    async def scenario() -> None:
        await plugin.on_load(ctx)
        await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})
        await plugin.handle_recommend_feed(
            {
                "action": "push_interactive_set",
                "mode": "interactive_ephemeral",
                "record_permanent_history": False,
                "session_id": "pin-test",
                "media_items": [
                    {"id": "m-a", "kind": "music", "title": "Alpha Song", "creator": "Artist A"},
                    {"id": "m-b", "kind": "music", "title": "Beta Song", "creator": "Artist B"},
                ],
            }
        )

        pin_res = await plugin.handle_recommend_feed({"action": "pin_item", "item_id": "m-b"})
        assert pin_res.ok and pin_res.data["pinned"] is True
        media_src = next(s for s in ctx.domains.media_sources() if s.source_id == "plugin-ai-curator")
        ix_items = await media_src.fetch_items("AI 灵感推荐")
        assert [m.id for m in ix_items] == ["m-b", "m-a"]

        dis_res = await plugin.handle_recommend_feed({"action": "dismiss_item", "item_id": "m-a"})
        assert dis_res.ok and dis_res.data["dismissed"] is True
        ix_after = await media_src.fetch_items("AI 灵感推荐")
        assert [m.id for m in ix_after] == ["m-b"]

        sets_res = await plugin.handle_recommend_feed({"action": "list_sets"})
        assert sets_res.ok and len(sets_res.data["sets"]) == 1
        get_res = await plugin.handle_recommend_feed({"action": "get_set", "session_id": "pin-test"})
        assert get_res.ok and get_res.data["session_id"] == "pin-test"

        st_res = await plugin.handle_recommend_feed({"action": "status"})
        assert st_res.ok and st_res.data["pinned_count"] == 1 and st_res.data["dismissed_count"] == 1

        clr_res = await plugin.handle_recommend_feed({"action": "clear_interactive", "session_id": "pin-test"})
        assert clr_res.ok and clr_res.data["removed_sessions"] == 1
        assert not (curation_root / "interactive" / "sessions" / "pin-test.json").exists()

        await plugin.on_unload()

    asyncio.run(scenario())
