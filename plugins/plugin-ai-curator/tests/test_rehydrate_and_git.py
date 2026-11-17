"""Offline git mirror, discovery, incremental rehydration, 6-stream parsing, SM-2 idempotency, and fail-closed tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

from conftest import PLUGIN_DIR, commit_all, create_bare_pair, init_git_repo, populate_full_archive_fixtures, run_git


def _setup_plugin(monkeypatch, tmp_path: Path, *, cfg_overrides: dict[str, Any] | None = None):
    xdg_data = tmp_path / "xdg-data"
    xdg_cfg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_cfg))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    from plugin import create_plugin

    plugin = create_plugin()
    ctx = PluginContext(
        "plugin-ai-curator",
        plugin_dir=PLUGIN_DIR,
        data_dir=xdg_data / "omnidesk" / "plugin-data" / "plugin-ai-curator",
        config_dir=xdg_cfg / "omnidesk" / "plugins" / "plugin-ai-curator",
    )
    if cfg_overrides:
        ctx.set_config(cfg_overrides)
    return plugin, ctx, xdg_data


def test_repo_discovery_priority_explicit_then_sibling_then_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    fake_ws = tmp_path / "workspace"
    fake_plugin_dir = fake_ws / "plugins" / "plugin-ai-curator"
    fake_plugin_dir.mkdir(parents=True)
    sibling_obs = fake_ws / "obsidian-vault"
    sibling_mus = fake_ws / "music-daily"
    init_git_repo(sibling_obs)
    init_git_repo(sibling_mus)

    explicit_obs = tmp_path / "explicit-obs"
    explicit_mus = tmp_path / "explicit-mus"

    from archive import resolve_repo_paths

    # 1. Explicit config beats sibling & XDG
    ctx_explicit = PluginContext(
        "plugin-ai-curator",
        plugin_dir=fake_plugin_dir,
        data_dir=tmp_path / "xdg-data" / "omnidesk" / "plugin-data" / "plugin-ai-curator",
        config_dir=tmp_path / "xdg-config" / "cfg1",
    )
    ctx_explicit.set_config({"obsidian_vault_dir": str(explicit_obs), "music_daily_dir": str(explicit_mus)})
    resolved1 = resolve_repo_paths(ctx_explicit)
    assert resolved1.obsidian_vault_dir == explicit_obs
    assert resolved1.music_daily_dir == explicit_mus

    # 2. Workspace sibling (.git) beats XDG fallback when config is empty
    resolved2 = resolve_repo_paths(
        PluginContext(
            "plugin-ai-curator",
            plugin_dir=fake_plugin_dir,
            data_dir=tmp_path / "xdg-data" / "omnidesk" / "plugin-data" / "plugin-ai-curator",
            config_dir=tmp_path / "xdg-config" / "cfg2",
        )
    )
    assert resolved2.obsidian_vault_dir == sibling_obs
    assert resolved2.music_daily_dir == sibling_mus

    # 2b. True workspace sibling (parent dir of workspace repo) also beats XDG fallback
    parent_group = tmp_path / "sibling-group"
    ws_plugin2 = parent_group / "omnidesk" / "plugins" / "plugin-ai-curator"
    ws_plugin2.mkdir(parents=True)
    true_sib_obs = parent_group / "obsidian-vault"
    true_sib_mus = parent_group / "music-daily"
    init_git_repo(true_sib_obs)
    init_git_repo(true_sib_mus)
    resolved2b = resolve_repo_paths(
        PluginContext(
            "plugin-ai-curator",
            plugin_dir=ws_plugin2,
            data_dir=tmp_path / "xdg-data" / "omnidesk" / "plugin-data" / "plugin-ai-curator",
            config_dir=tmp_path / "xdg-config" / "cfg2b",
        )
    )
    assert resolved2b.obsidian_vault_dir == true_sib_obs
    assert resolved2b.music_daily_dir == true_sib_mus

    # 3. XDG fallback when sibling repos are absent
    empty_ws_plugin = tmp_path / "other-ws" / "plugins" / "plugin-ai-curator"
    empty_ws_plugin.mkdir(parents=True)
    resolved3 = resolve_repo_paths(
        PluginContext(
            "plugin-ai-curator",
            plugin_dir=empty_ws_plugin,
            data_dir=tmp_path / "xdg-data" / "omnidesk" / "plugin-data" / "plugin-ai-curator",
            config_dir=tmp_path / "xdg-config" / "cfg3",
        )
    )
    expected_root = tmp_path / "xdg-data" / "omnidesk" / "curation"
    assert resolved3.curation_root == expected_root
    assert resolved3.obsidian_vault_dir == expected_root / "repos" / "obsidian-vault"
    assert resolved3.music_daily_dir == expected_root / "repos" / "music-daily"


def test_git_clone_ff_only_and_dirty_worktree_protection(monkeypatch, tmp_path: Path) -> None:
    obs_bare, obs_seed = create_bare_pair(tmp_path / "remotes", "obsidian-vault")
    mus_bare, mus_seed = create_bare_pair(tmp_path / "remotes", "music-daily")
    populate_full_archive_fixtures(obs_seed, mus_seed)
    commit_all(obs_seed, "initial archive fixtures")
    run_git(["push", "origin", "main"], obs_seed)
    commit_all(mus_seed, "initial music fixtures")
    run_git(["push", "origin", "main"], mus_seed)

    clone_obs = tmp_path / "managed" / "obsidian-vault"
    clone_mus = tmp_path / "managed" / "music-daily"
    curation_root = tmp_path / "curation-root"

    plugin, ctx, _ = _setup_plugin(
        monkeypatch,
        tmp_path,
        cfg_overrides={
            "obsidian_vault_dir": str(clone_obs),
            "music_daily_dir": str(clone_mus),
            "curation_root": str(curation_root),
            "sync_remote": True,
        },
    )

    async def scenario() -> None:
        await plugin.on_load(ctx)

        # 1. Initial clone via `git clone --branch main`
        res1 = await plugin.handle_recommend_feed(
            {
                "action": "bootstrap_and_rehydrate",
                "obsidian_vault_url": str(obs_bare),
                "music_daily_url": str(mus_bare),
                "sync_vocab": False,
            }
        )
        assert res1.ok is True
        assert res1.data["git_sync"]["obsidian_vault"]["cloned"] is True
        assert res1.data["git_sync"]["music_daily"]["cloned"] is True
        assert res1.data["dirty"] is False
        assert (clone_obs / ".git").is_dir()
        assert (clone_mus / ".git").is_dir()

        # 2. Push new clean commit upstream -> ff-only pull succeeds
        new_news = obs_seed / "News" / "Daily" / "2026-11-17.md"
        new_news.write_text(
            "---\ndate: 2026-11-17\ntype: daily-news-daily\n---\n"
            "<!-- AUTO:START -->\n## Executive View\n新进更新。\n"
            "## 1. Fresh Story\n### 事实摘要\n远端新增内容。\n<!-- AUTO:END -->\n",
            encoding="utf-8",
        )
        commit_all(obs_seed, "add 2026-11-17 news")
        run_git(["push", "origin", "main"], obs_seed)

        res2 = await plugin.handle_recommend_feed(
            {"action": "bootstrap_and_rehydrate", "sync_vocab": False}
        )
        assert res2.ok is True
        assert res2.data["git_sync"]["obsidian_vault"]["pulled"] is True
        assert (clone_obs / "News" / "Daily" / "2026-11-17.md").is_file()

        # 3. Make local checkout dirty & push another commit upstream -> dirty worktree MUST NOT pull or lose edits
        local_note = clone_obs / "Music" / "Daily" / "2026-11-13.md"
        edited_text = local_note.read_text(encoding="utf-8") + "\n本地未提交手写感受：吉他泛音极美。\n"
        local_note.write_text(edited_text, encoding="utf-8")

        upstream_conflict_note = obs_seed / "Music" / "Daily" / "2026-11-13.md"
        upstream_conflict_note.write_text(
            upstream_conflict_note.read_text(encoding="utf-8") + "\n远端冲突修改。\n",
            encoding="utf-8",
        )
        upstream_marker = obs_seed / "News" / "Daily" / "2026-11-18.md"
        upstream_marker.write_text(
            "---\ndate: 2026-11-18\n---\n<!-- AUTO:START -->\n## 1. Should Not Pull\n### 事实摘要\nShould stay remote.\n<!-- AUTO:END -->\n",
            encoding="utf-8",
        )
        commit_all(obs_seed, "upstream update while client dirty")
        run_git(["push", "origin", "main"], obs_seed)

        res3 = await plugin.handle_recommend_feed(
            {"action": "bootstrap_and_rehydrate", "sync_vocab": False}
        )
        assert res3.ok is True
        assert res3.data["dirty"] is True
        assert res3.data["dirty_worktree_preserved"] is True
        assert res3.data["obsidian_dirty"] is True
        assert res3.data["git_sync"]["obsidian_vault"]["pulled"] is False
        assert not (clone_obs / "News" / "Daily" / "2026-11-18.md").exists()
        assert local_note.read_text(encoding="utf-8") == edited_text

        # Local dirty edit was still rehydrated into search/media
        search_res = await plugin.handle_recommend_feed({"action": "search_archive", "query": "吉他泛音极美"})
        assert search_res.ok is True
        assert search_res.data["total"] >= 1

        await plugin.on_unload()

    asyncio.run(scenario())


def test_incremental_manifest_and_touch_vs_edit(monkeypatch, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obsidian-vault"
    mus_dir = tmp_path / "music-daily"
    curation_root = tmp_path / "curation-root"
    populate_full_archive_fixtures(obs_dir, mus_dir)

    plugin, ctx, _ = _setup_plugin(
        monkeypatch,
        tmp_path,
        cfg_overrides={
            "obsidian_vault_dir": str(obs_dir),
            "music_daily_dir": str(mus_dir),
            "curation_root": str(curation_root),
            "sync_remote": False,
        },
    )

    async def scenario() -> None:
        await plugin.on_load(ctx)

        res1 = await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})
        assert res1.ok is True
        assert res1.data["cache_hit"] is False
        assert res1.data["reparsed_files"] > 0

        manifest_path = curation_root / "rehydrate_manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(manifest["files"]) > 0
        sample = manifest["files"][0]
        assert {"rel_path", "size", "mtime_ns", "blob_sha"} <= set(sample.keys())
        assert len(sample["blob_sha"]) == 40

        # Repeat without changes -> exact cache hit
        res2 = await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})
        assert res2.ok is True
        assert res2.data["cache_hit"] is True
        assert res2.data["reparsed_files"] == 0

        # Touch mtime without changing bytes -> blob_sha avoids reparsing
        news_file = obs_dir / "News" / "Daily" / "2026-11-13.md"
        st = news_file.stat()
        os.utime(news_file, ns=(st.st_atime_ns + 1_000_000, st.st_mtime_ns + 1_000_000))
        res3 = await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})
        assert res3.ok is True
        assert res3.data["reparsed_files"] == 0

        # Modify content of 1 file -> only 1 file reparsed
        news_file.write_text(news_file.read_text(encoding="utf-8") + "\n<!-- tail -->\n", encoding="utf-8")
        res4 = await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})
        assert res4.ok is True
        assert res4.data["cache_hit"] is False
        assert res4.data["reparsed_files"] == 1

        await plugin.on_unload()

    asyncio.run(scenario())


def test_six_streams_2026_11_06_anomaly_queries_sources_and_sm2_idempotent(monkeypatch, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obsidian-vault"
    mus_dir = tmp_path / "music-daily"
    curation_root = tmp_path / "curation-root"
    populate_full_archive_fixtures(obs_dir, mus_dir)

    plugin, ctx, _ = _setup_plugin(
        monkeypatch,
        tmp_path,
        cfg_overrides={
            "obsidian_vault_dir": str(obs_dir),
            "music_daily_dir": str(mus_dir),
            "curation_root": str(curation_root),
            "sync_remote": False,
        },
    )

    sm2_store: dict[str, dict[str, Any]] = {
        "procrastinate": {
            "word": "procrastinate",
            "interval": 21,
            "ease_factor": 2.6,
            "meaning": "existing user card",
        }
    }

    async def fake_sm2_handler(req: dict[str, Any]) -> CapabilityResult:
        act = req.get("action")
        if act == "list":
            return CapabilityResult.ok({"cards": list(sm2_store.values())})
        if act == "add":
            w = str(req["word"]).strip().lower()
            if w in sm2_store:
                raise AssertionError(f"must never overwrite existing SM-2 word {w!r}")
            sm2_store[w] = {"word": req["word"], "interval": 1, "meaning": req.get("meaning", "")}
            return CapabilityResult.ok({"word": req["word"], "created": True})
        return CapabilityResult.fail("unsupported", error_code="invalid_argument")

    ctx.register_capability("vocab.sm2.v1", fake_sm2_handler)

    async def scenario() -> None:
        await plugin.on_load(ctx)

        res = await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": True})
        assert res.ok is True
        # 5 tracks on 2026-11-13 + 4 tracks on 2026-11-06 = 9 (2026-09-03 ignored!)
        assert res.data["music_tracks_hydrated"] == 9
        assert res.data["youtube_videos_hydrated"] == 2
        assert res.data["news_stories_hydrated"] == 1
        assert res.data["reddit_posts_hydrated"] == 1
        assert res.data["journal_papers_hydrated"] == 1
        assert res.data["english_notes_hydrated"] == 5
        assert res.data["vocab_cards_hydrated"] == 2

        # 2026-11-06 4-track anomaly reported explicitly without inventing a 5th track
        anomalies = res.data["history_anomalies"]
        assert any(
            a["date"] == "2026-11-06"
            and a["kind"] == "incomplete_daily_tracks"
            and a["actual_count"] == 4
            and a["expected_count"] == 5
            and a["positions"] == [1, 2, 4, 5]
            for a in anomalies
        )

        # SM-2 idempotent import: existing interval=21 preserved, new phrase added once
        assert res.data["vocab_sync"]["added"] == 1
        assert res.data["vocab_sync"]["skipped"] == 1
        assert sm2_store["procrastinate"]["interval"] == 21
        assert "over the short / long term" in sm2_store

        # Repeat rehydrate -> 0 newly added vocab cards
        res_again = await plugin.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": True})
        assert res_again.ok is True
        assert res_again.data["vocab_sync"]["added"] == 0

        # list_archive_dates: strictly descending, pre-2026-09-04 absent
        dates_res = await plugin.handle_recommend_feed({"action": "list_archive_dates"})
        assert dates_res.ok is True
        date_rows = dates_res.data["dates"]
        date_strs = [r["date"] for r in date_rows]
        assert date_strs == sorted(date_strs, reverse=True)
        assert "2026-09-03" not in date_strs
        assert "2026-11-06" in date_strs
        nov06 = next(r for r in date_rows if r["date"] == "2026-11-06")
        assert nov06["music"] == 4
        assert "anomalies" in nov06

        # search_archive across keyword, DOI, tag, artist, date filter
        s_artist = await plugin.handle_recommend_feed({"action": "search_archive", "query": "Sampha"})
        assert s_artist.ok and s_artist.data["total"] == 1
        s_doi = await plugin.handle_recommend_feed({"action": "search_archive", "query": "10.1126/science.1259802"})
        assert s_doi.ok and s_doi.data["total"] == 1
        assert s_doi.data["items"][0]["domain"] == "journal"
        s_tag = await plugin.handle_recommend_feed({"action": "search_archive", "query": "wildcard"})
        assert s_tag.ok and s_tag.data["total"] == 1
        s_date = await plugin.handle_recommend_feed({"action": "search_archive", "query": "date:2026-11-06"})
        assert s_date.ok and s_date.data["total"] == 4
        # Multi-term queries fall back to OR matching instead of requiring one exact substring.
        s_terms = await plugin.handle_recommend_feed({"action": "search_archive", "query": "Sampha Muerte", "domain": "music"})
        assert s_terms.ok and s_terms.data["total"] == 2
        # Plural `domains` is honored when callers do not send the singular `domain`.
        s_domains = await plugin.handle_recommend_feed({"action": "search_archive", "query": "Sampha", "domains": ["media"]})
        assert s_domains.ok and s_domains.data["total"] == 1

        # MediaSource, TextSource (category=multi), BookSource (format=english_daily)
        media_src = next(s for s in ctx.domains.media_sources() if s.source_id == "plugin-ai-curator")
        text_src = next(s for s in ctx.domains.text_sources() if s.source_id == "plugin-ai-curator")
        book_src = next(s for s in ctx.domains.book_sources() if s.source_id == "plugin-ai-curator")

        latest_music = await media_src.fetch_items("每日 5 首 · 最新")
        assert len(latest_music) == 5
        assert any("吉他的空气感非常出色" in str(m.extra.get("user_notes", "")) for m in latest_music)
        nov06_music = await media_src.fetch_items("每日 5 首 · 全量曲库", query="date:2026-11-06")
        assert len(nov06_music) == 4

        yt_items = await media_src.fetch_items("YouTube 每日精选")
        assert len(yt_items) == 2

        channels = {ch.id: ch for ch in text_src.channels()}
        assert {"news", "journal", "reddit", "english", "interactive"} <= set(channels.keys())
        reddit_feed = await text_src.fetch_feed("reddit")
        assert len(reddit_feed) == 1
        assert reddit_feed[0].extra.get("original_post_verification") == "not_performed_user_waived"

        journal_feed = await text_src.fetch_feed("journal")
        assert len(journal_feed) == 1
        j_art = await text_src.fetch_article(journal_feed[0].id)
        joined_paras = " ".join(p.original for p in j_art.paragraphs)
        assert "Stark" in joined_paras and "19NT" in joined_paras

        books = await book_src.list_books()
        assert any(b.id == "english-archive-all" and b.format == "english_daily" for b in books)
        assert any(b.id == "english-date-2026-11-16" for b in books)
        spread = await book_src.fetch_spread("english-date-2026-11-16", 1)
        assert len(spread.blocks) >= 1
        vocab_list = await book_src.list_vocab()
        assert len(vocab_list) == 2

        await plugin.on_unload()

    asyncio.run(scenario())


def test_fail_closed_on_truncated_or_corrupt_upstream(monkeypatch, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obsidian-vault"
    mus_dir = tmp_path / "music-daily"
    curation_root = tmp_path / "curation-root"
    populate_full_archive_fixtures(obs_dir, mus_dir)

    # 1. Corrupt music manifest
    (mus_dir / "recommendations" / "2026" / "11" / "2026-11-13.json").write_text('{"tracks": [', encoding="utf-8")
    plugin1, ctx1, _ = _setup_plugin(
        monkeypatch,
        tmp_path / "c1",
        cfg_overrides={"obsidian_vault_dir": str(obs_dir), "music_daily_dir": str(mus_dir), "sync_remote": False},
    )

    async def check(plg, ctx) -> CapabilityResult:
        await plg.on_load(ctx)
        res = await plg.handle_recommend_feed({"action": "bootstrap_and_rehydrate", "sync_vocab": False})
        await plg.on_unload()
        return res

    r1 = asyncio.run(check(plugin1, ctx1))
    assert r1.ok is False
    assert r1.data.get("error_code") == "unreadable_upstream"

    # Restore music manifest and corrupt state/tracks.json
    populate_full_archive_fixtures(obs_dir, mus_dir)
    (mus_dir / "state" / "tracks.json").write_text("{bad json", encoding="utf-8")
    r2 = asyncio.run(check(plugin1, ctx1))
    assert r2.ok is False

    # Restore state/tracks.json and corrupt youtube.jsonl
    populate_full_archive_fixtures(obs_dir, mus_dir)
    (obs_dir / ".recommendation" / "history" / "youtube.jsonl").write_text('{"date":"2026-11-13"\n', encoding="utf-8")
    r3 = asyncio.run(check(plugin1, ctx1))
    assert r3.ok is False

    # Restore youtube.jsonl and corrupt policy.json -> fail closed
    populate_full_archive_fixtures(obs_dir, mus_dir)
    (obs_dir / ".recommendation" / "policy.json").write_text("{broken", encoding="utf-8")
    r4 = asyncio.run(check(plugin1, ctx1))
    assert r4.ok is False

    # Restore policy.json and test escaping symlink -> fail closed
    populate_full_archive_fixtures(obs_dir, mus_dir)
    outside = tmp_path / "outside-secret.md"
    outside.write_text("# secret", encoding="utf-8")
    escape_link = obs_dir / "News" / "Daily" / "2026-11-18.md"
    escape_link.symlink_to(outside)
    r5 = asyncio.run(check(plugin1, ctx1))
    assert r5.ok is False
    escape_link.unlink()

    # Non-UTF8 undecodable bytes -> fail closed
    bad_utf8 = obs_dir / "News" / "Daily" / "2026-11-18.md"
    bad_utf8.write_bytes(b"\xff\xfe\x00\x01")
    r6 = asyncio.run(check(plugin1, ctx1))
    assert r6.ok is False
