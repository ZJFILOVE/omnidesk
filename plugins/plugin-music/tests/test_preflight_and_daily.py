"""Offline tests for fail-closed validation, CN preflight, and daily immutable sync."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from conftest import (
    FakeAppleMusicState,
    build_plugin_context,
    git_commit_all,
    make_tracks,
    run_async,
    write_daily_fixture,
)
from state_store import PROTECTED_DAILY_PLAYLIST_ID, resolve_daily_checkpoint_path


def _err(res: Any) -> str:
    return str(res.data.get("error_code") or "")


def test_fail_closed_validation_and_missing_credentials(tmp_path: Path) -> None:
    _, ctx = build_plugin_context(tmp_path, dev_token="", user_token="", cookie="")

    res_bad_act = run_async(ctx.capabilities.call("media.playlist.v1", {"action": "unknown_op"}))
    assert not res_bad_act.ok
    assert _err(res_bad_act) == "invalid_argument"

    res_bad_param = run_async(
        ctx.capabilities.call("media.playlist.v1", {"action": "status", "unexpected": 1})
    )
    assert not res_bad_param.ok
    assert _err(res_bad_param) == "invalid_argument"

    res_no_cred = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {"action": "preflight_cn_catalog", "storefront": "cn", "track_ids": ["1001"]},
        )
    )
    assert not res_no_cred.ok
    assert _err(res_no_cred) == "not_configured"


def test_preflight_partial_unplayable_and_upstream_offline(
    tmp_path: Path, fake_apple_server: tuple[FakeAppleMusicState, str]
) -> None:
    state, url = fake_apple_server
    state.seed_playable("1001", playable=True)
    state.seed_playable("1002", playable=False)
    _, ctx = build_plugin_context(tmp_path, api_base=url)

    no_fallback = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "preflight_cn_catalog",
                "storefront": "cn",
                "track_ids": ["1001"],
                "allow_offline_catalog_fallback": True,
            },
        )
    )
    assert not no_fallback.ok
    assert _err(no_fallback) == "invalid_argument"

    partial = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "preflight_cn_catalog",
                "storefront": "cn",
                "track_ids": ["1001", "1002", "9999"],
            },
        )
    )
    assert partial.ok
    assert partial.data["verified_ids"] == ["1001"]
    assert partial.data["unavailable"] == ["1002"]
    assert partial.data["not_found"] == ["9999"]
    assert partial.data["all_playable"] is False
    assert not partial.data["error"]

    state.force_catalog_status = 503
    offline = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {"action": "preflight_cn_catalog", "storefront": "cn", "track_ids": ["1001"]},
        )
    )
    assert not offline.ok
    assert _err(offline) == "service_offline"
    assert offline.data["verified_ids"] == []
    assert offline.data["error"]

    # Real unreachable port never assumes playable.
    _, dead_ctx = build_plugin_context(tmp_path / "dead", api_base="http://127.0.0.1:1")
    dead_res = run_async(
        dead_ctx.capabilities.call(
            "media.playlist.v1",
            {"action": "preflight_cn_catalog", "storefront": "cn", "track_ids": ["1001"]},
        )
    )
    assert not dead_res.ok
    assert _err(dead_res) == "service_offline"


def test_daily_sync_requires_committed_published_bundle(
    tmp_path: Path, fake_apple_server: tuple[FakeAppleMusicState, str]
) -> None:
    state, url = fake_apple_server
    ids = ["2001", "2002", "2003", "2004", "2005"]
    for sid in ids:
        state.seed_playable(sid)
    music_repo = tmp_path / "music-daily"
    vault_repo = tmp_path / "obsidian-vault"
    music_repo.mkdir()
    vault_repo.mkdir()

    # Case 1: PREPARED transaction + qa_verified=True alone -> needs_verified_bundle.
    write_daily_fixture(
        music_repo,
        vault_repo,
        day="2026-11-13",
        tracks=make_tracks(ids),
        tx_status="PREPARED",
        include_qa_run=False,
    )
    _, ctx = build_plugin_context(tmp_path, music_repo=music_repo, vault_repo=vault_repo, api_base=url)
    res_prepared = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "sync_daily_authorized",
                "mode": "daily_immutable",
                "business_date": "2026-11-13",
                "operation_id": "music:2026-11-13",
                "qa_verified": True,
            },
        )
    )
    assert not res_prepared.ok
    assert _err(res_prepared) == "needs_verified_bundle"

    # Case 2: Valid PUBLISHED bundle, then uncommitted dirty file in obsidian-vault -> needs_verified_bundle.
    write_daily_fixture(
        music_repo,
        vault_repo,
        day="2026-11-13",
        tracks=make_tracks(ids),
        tx_status="real_published",
        include_qa_run=True,
    )
    (vault_repo / "dirty.tmp").write_text("uncommitted", encoding="utf-8")
    res_dirty = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "sync_daily_authorized",
                "mode": "daily_immutable",
                "business_date": "2026-11-13",
                "operation_id": "music:2026-11-13",
                "qa_verified": True,
            },
        )
    )
    assert not res_dirty.ok
    assert _err(res_dirty) == "needs_verified_bundle"


def test_daily_sync_dry_run_missing_only_append_0600_and_readback_failure(
    tmp_path: Path, fake_apple_server: tuple[FakeAppleMusicState, str]
) -> None:
    state, url = fake_apple_server
    ids = ["3001", "3002", "3003", "3004", "3005"]
    for sid in ["3000", *ids]:
        state.seed_playable(sid)
    # Seed pre-existing track on remote protected playlist to prove missing-only append without delete/reorder.
    state.playlists[PROTECTED_DAILY_PLAYLIST_ID]["tracks"] = ["3000", "3001"]

    music_repo = tmp_path / "music-daily"
    vault_repo = tmp_path / "obsidian-vault"
    music_repo.mkdir()
    vault_repo.mkdir()
    write_daily_fixture(
        music_repo,
        vault_repo,
        day="2026-11-13",
        tracks=make_tracks(ids),
        tx_status="PUBLISHED",
        include_qa_run=True,
    )
    _, ctx = build_plugin_context(tmp_path, music_repo=music_repo, vault_repo=vault_repo, api_base=url)
    ckpt = resolve_daily_checkpoint_path("", ctx.data_dir)

    dry = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "sync_daily_authorized",
                "mode": "daily_immutable",
                "business_date": "2026-11-13",
                "operation_id": "music:2026-11-13",
                "qa_verified": True,
                "dry_run": True,
            },
        )
    )
    assert dry.ok
    assert dry.data["dry_run"] is True
    assert dry.data["cloud_synced"] is False
    assert not ckpt.exists()
    assert not [r for r in state.requests if r[0] == "POST"]

    synced = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "sync_daily_authorized",
                "mode": "daily_immutable",
                "business_date": "2026-11-13",
                "operation_id": "music:2026-11-13",
                "qa_verified": True,
            },
        )
    )
    assert synced.ok
    assert synced.data["added_ids"] == ["3002", "3003", "3004", "3005"]
    assert state.playlists[PROTECTED_DAILY_PLAYLIST_ID]["tracks"] == ["3000", "3001", "3002", "3003", "3004", "3005"]
    assert synced.data["cloud_synced"] is True
    assert stat.S_IMODE(ckpt.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(f"{ckpt}.lock").stat().st_mode) == 0o600

    # Idempotent repeat does zero POSTs.
    post_count_before = len([r for r in state.requests if r[0] == "POST"])
    repeat = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "sync_daily_authorized",
                "mode": "daily_immutable",
                "business_date": "2026-11-13",
                "operation_id": "music:2026-11-13",
                "qa_verified": True,
            },
        )
    )
    assert repeat.ok
    assert repeat.data["added_count"] == 0
    assert len([r for r in state.requests if r[0] == "POST"]) == post_count_before

    # Second date with readback failure must fail closed and NOT mark synced.
    ids2 = ["4001", "4002", "4003", "4004", "4005"]
    for sid in ids2:
        state.seed_playable(sid)
    write_daily_fixture(
        music_repo,
        vault_repo,
        day="2026-11-14",
        tracks=make_tracks(ids2),
        tx_status="PUBLISHED",
        include_qa_run=True,
    )
    state.fail_append_readback = True
    failed_rb = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "sync_daily_authorized",
                "mode": "daily_immutable",
                "business_date": "2026-11-14",
                "operation_id": "music:2026-11-14",
                "qa_verified": True,
            },
        )
    )
    assert not failed_rb.ok
    assert _err(failed_rb) == "readback_verification_failed"
    stored = json.loads(ckpt.read_text(encoding="utf-8"))
    assert "2026-11-14" not in stored["synced_dates"]
    assert stored["pending_song_ids"] == ids2


def test_2026_11_06_four_track_anomaly_reported_without_fabrication(
    tmp_path: Path, fake_apple_server: tuple[FakeAppleMusicState, str]
) -> None:
    state, url = fake_apple_server
    ids_4 = ["6101", "6102", "6104", "6105"]
    for sid in ids_4:
        state.seed_playable(sid)
    music_repo = tmp_path / "music-daily"
    vault_repo = tmp_path / "obsidian-vault"
    music_repo.mkdir()
    vault_repo.mkdir()
    write_daily_fixture(
        music_repo,
        vault_repo,
        day="2026-11-06",
        tracks=make_tracks(ids_4, positions=[1, 2, 4, 5]),
        tx_status="real_published",
        include_qa_run=True,
    )
    git_commit_all(vault_repo, "ensure clean")

    _, ctx = build_plugin_context(tmp_path, music_repo=music_repo, vault_repo=vault_repo, api_base=url)
    res = run_async(
        ctx.capabilities.call(
            "media.playlist.v1",
            {
                "action": "sync_daily_authorized",
                "mode": "daily_immutable",
                "business_date": "2026-11-06",
                "operation_id": "music:2026-11-06",
                "qa_verified": True,
            },
        )
    )
    assert res.ok
    assert res.data["added_ids"] == ids_4
    assert res.data["verified_count"] == 4
    assert len(state.playlists[PROTECTED_DAILY_PLAYLIST_ID]["tracks"]) == 4
    anomalies = res.data["history_anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["date"] == "2026-11-06"
    assert anomalies[0]["actual_count"] == 4
    assert anomalies[0]["expected_count"] == 5
    assert anomalies[0]["song_ids"] == ids_4
