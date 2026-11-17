"""Fail-closed daily immutable sync logic (`ChatGPT Daily 5` / `p.YJXV7dEIeb3GzZX`)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .apple_client import (
        AppleMusicClientProtocol,
        AppleMusicCredentials,
        extract_catalog_ids,
    )
    from .curation_gate import scan_repo_anomalies, verify_clean_git_repo, verify_daily_authorization
    from .state_store import (
        PROTECTED_DAILY_PLAYLIST_ID,
        PROTECTED_DAILY_PLAYLIST_NAME,
        GateError,
        acquire_state_lock,
        atomic_write_json,
        ensure_outside_repos,
        is_valid_iso_date,
        normalize_daily_state,
        read_private_json,
        resolve_daily_checkpoint_path,
        resolve_repo_dir,
        utc_now,
    )
else:
    from apple_client import (
        AppleMusicClientProtocol,
        AppleMusicCredentials,
        extract_catalog_ids,
    )
    from curation_gate import scan_repo_anomalies, verify_clean_git_repo, verify_daily_authorization
    from state_store import (
        PROTECTED_DAILY_PLAYLIST_ID,
        PROTECTED_DAILY_PLAYLIST_NAME,
        GateError,
        acquire_state_lock,
        atomic_write_json,
        ensure_outside_repos,
        is_valid_iso_date,
        normalize_daily_state,
        read_private_json,
        resolve_daily_checkpoint_path,
        resolve_repo_dir,
        utc_now,
    )

ClientBuilder = Callable[[PluginContext, bool], tuple[AppleMusicClientProtocol, AppleMusicCredentials]]
CacheTracksFn = Callable[[PluginContext, list[Mapping[str, Any]]], None]


def validate_numeric_ids(raw: Any, field_name: str) -> list[str]:
    """Validate a non-empty list of numeric Apple Music song IDs."""
    if not isinstance(raw, list) or not raw:
        raise GateError("invalid_argument", f"{field_name} 必须是非空数字歌曲 ID 列表")
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip().isdigit():
            raise GateError("invalid_argument", f"{field_name} 包含无效数字 ID: {item!r}")
        ids.append(item.strip())
    return ids


async def execute_sync_daily(
    ctx: PluginContext,
    payload: Mapping[str, Any],
    *,
    creds: AppleMusicCredentials,
    has_custom_factory: bool,
    build_client: ClientBuilder,
    cache_tracks: CacheTracksFn,
    verified_cache: set[str],
) -> CapabilityResult:
    """Execute `sync_daily_authorized` strictly under `mode='daily_immutable'`."""
    if payload.get("mode") != "daily_immutable":
        raise GateError("invalid_argument", "sync_daily_authorized 只接受 mode='daily_immutable'")
    if "storefront" in payload and str(payload["storefront"]).strip().lower() != "cn":
        raise GateError("invalid_argument", "sync_daily_authorized 仅支持 storefront='cn'")
    if payload.get("local_checkpoint_only") is True or (
        "local_checkpoint_only" in payload and not isinstance(payload["local_checkpoint_only"], bool)
    ):
        raise GateError("invalid_argument", "每日歌单禁止 local_checkpoint_only")

    cfg = ctx.get_config()
    default_cum_id = (
        str(cfg.get("daily_playlist_id") or PROTECTED_DAILY_PLAYLIST_ID).strip()
        or PROTECTED_DAILY_PLAYLIST_ID
    )
    cum_id = str(payload.get("cumulative_playlist_id") or default_cum_id).strip()
    if cum_id != PROTECTED_DAILY_PLAYLIST_ID:
        raise GateError("invalid_argument", f"累计歌单必须固定为 {PROTECTED_DAILY_PLAYLIST_ID}")

    sync_all = payload.get("sync_all_rehydrated", False)
    dry_run = payload.get("dry_run", False)
    create_dated = payload.get("create_dated_playlist", False)
    dated_prefix = payload.get("dated_playlist_prefix", "ChatGPT Daily 5 · ")
    if not isinstance(sync_all, bool) or not isinstance(dry_run, bool) or not isinstance(create_dated, bool):
        raise GateError("invalid_argument", "sync_all_rehydrated/dry_run/create_dated_playlist 必须为布尔值")
    if create_dated and (sync_all or dated_prefix != "ChatGPT Daily 5 · "):
        raise GateError("invalid_argument", "单日创建当日歌单必须使用前缀 'ChatGPT Daily 5 · '")

    if payload.get("qa_verified") is not True:
        raise GateError("needs_verified_bundle", "缺少 qa_verified=true 与已提交 PUBLISHED 事务证明")

    business_date = str(payload.get("business_date") or "").strip()
    operation_id = str(payload.get("operation_id") or "").strip()
    if not sync_all:
        if not business_date and operation_id.startswith("music:"):
            business_date = operation_id.removeprefix("music:").strip()
        if not is_valid_iso_date(business_date):
            raise GateError("invalid_argument", "单日同步必须提供合法 business_date (YYYY-MM-DD)")
        if operation_id and operation_id != f"music:{business_date}":
            raise GateError("needs_verified_bundle", f"operation_id 与 business_date ({business_date}) 不匹配")

    req_track_ids = validate_numeric_ids(payload["track_ids"], "track_ids") if "track_ids" in payload else None
    music_repo = resolve_repo_dir(str(cfg.get("music_daily_dir") or ""), ctx.data_dir, "music-daily")
    vault_repo = resolve_repo_dir(str(cfg.get("obsidian_vault_dir") or ""), ctx.data_dir, "obsidian-vault")
    checkpoint_path = resolve_daily_checkpoint_path(str(cfg.get("daily_checkpoint_path") or ""), ctx.data_dir)
    ensure_outside_repos(checkpoint_path, music_repo, vault_repo)

    report = await verify_daily_authorization(
        ctx,
        music_repo=music_repo,
        vault_repo=vault_repo,
        business_date=business_date,
        operation_id=operation_id,
        sync_all_rehydrated=sync_all,
        req_track_ids=req_track_ids,
        publication_evidence=payload.get("publication_evidence"),
    )

    if dry_run:
        existing_state = normalize_daily_state(read_private_json(checkpoint_path))
        confirmed = {str(x) for x in existing_state["cumulative_track_ids"]}
        live_preflight_checked = False
        if creds.has_catalog_auth or has_custom_factory:
            client, _ = build_client(ctx, False)
            pre = await client.preflight_cn_catalog(report.target_song_ids, "cn")
            if not pre["all_playable"]:
                raise GateError("service_offline", "国区预检存在不可播曲目", {
                    "unavailable": pre["unavailable"],
                    "not_found": pre["not_found"],
                    "history_anomalies": report.history_anomalies,
                })
            live_preflight_checked = True
        return CapabilityResult.ok({
            "mode": "daily_immutable",
            "dry_run": True,
            "cloud_synced": False,
            "live_apple_music_synced": False,
            "readback_verified": False,
            "live_preflight_checked": live_preflight_checked,
            "cumulative_playlist_id": PROTECTED_DAILY_PLAYLIST_ID,
            "cumulative_playlist_name": PROTECTED_DAILY_PLAYLIST_NAME,
            "business_date": "" if sync_all else report.days[0].business_date,
            "operation_id": "" if sync_all else report.days[0].operation_id,
            "planned_target_ids": list(report.target_song_ids),
            "planned_missing_ids": [sid for sid in report.target_song_ids if sid not in confirmed],
            "already_confirmed_count": sum(1 for sid in report.target_song_ids if sid in confirmed),
            "added_count": 0,
            "verified_count": 0,
            "history_anomalies": report.history_anomalies,
            "state_file": str(checkpoint_path),
        })

    client, _ = build_client(ctx, True)
    pre = await client.preflight_cn_catalog(report.target_song_ids, "cn")
    if not pre["all_playable"]:
        raise GateError("service_offline", "正式执行前国区预检失败，拒绝写入", {
            "unavailable": pre["unavailable"],
            "not_found": pre["not_found"],
            "history_anomalies": report.history_anomalies,
        })
    verified_cache.update(pre["verified_ids"])
    cache_tracks(ctx, pre.get("tracks", []))

    with acquire_state_lock(checkpoint_path):
        state = normalize_daily_state(read_private_json(checkpoint_path))
        meta = await client.get_playlist_meta(PROTECTED_DAILY_PLAYLIST_ID)
        if meta["name"] != PROTECTED_DAILY_PLAYLIST_NAME:
            raise GateError("service_offline", "远端累计歌单名称与 ChatGPT Daily 5 不符")

        remote_tracks = await client.get_playlist_tracks(PROTECTED_DAILY_PLAYLIST_ID)
        if state["pending_song_ids"]:
            pending = set(state["pending_song_ids"])
            if not (pending <= extract_catalog_ids(remote_tracks)):
                remote_tracks, _, _ = await client.verify_readback(PROTECTED_DAILY_PLAYLIST_ID, pending)
            state["pending_song_ids"] = []
            atomic_write_json(checkpoint_path, state)

        existing_ids = extract_catalog_ids(remote_tracks)
        missing = [sid for sid in report.target_song_ids if sid not in existing_ids]
        added_ids: list[str] = []

        for offset in range(0, len(missing), 25):
            recheck = await verify_daily_authorization(
                ctx,
                music_repo=music_repo,
                vault_repo=vault_repo,
                business_date=business_date,
                operation_id=operation_id,
                sync_all_rehydrated=sync_all,
                req_track_ids=req_track_ids,
                publication_evidence=payload.get("publication_evidence"),
            )
            if recheck.target_song_ids != report.target_song_ids:
                raise GateError("needs_verified_bundle", "同步前双仓授权快照发生变更")

            batch = missing[offset : offset + 25]
            state["pending_song_ids"] = list(batch)
            atomic_write_json(checkpoint_path, state)
            await client.append_playlist_tracks(PROTECTED_DAILY_PLAYLIST_ID, batch)
            remote_tracks, existing_ids, ordered_remote = await client.verify_readback(
                PROTECTED_DAILY_PLAYLIST_ID, set(batch)
            )
            state["pending_song_ids"] = []
            state["cumulative_track_ids"] = ordered_remote
            atomic_write_json(checkpoint_path, state)
            added_ids.extend(batch)

        remote_tracks, final_ids, ordered_final = await client.verify_readback(
            PROTECTED_DAILY_PLAYLIST_ID, set(report.target_song_ids)
        )
        if create_dated and report.days:
            day_obj = report.days[0]
            dated_name = f"{dated_prefix}{day_obj.business_date}"
            dated_entry = state["dated_playlists"].get(day_obj.business_date)
            dated_pid = dated_entry.get("playlist_id") if isinstance(dated_entry, Mapping) else None
            if not dated_pid:
                created = await client.create_library_playlist(
                    dated_name, f"OmniDesk Daily 5 ({day_obj.business_date})", day_obj.song_ids
                )
                dated_pid = created["id"]
            _, _, dated_ordered = await client.verify_readback(str(dated_pid), set(day_obj.song_ids))
            state["dated_playlists"][day_obj.business_date] = {
                "name": dated_name,
                "playlist_id": dated_pid,
                "track_ids": dated_ordered,
                "cloud_synced": True,
                "synced_at": utc_now(),
            }

        synced_dates: list[str] = []
        for day_obj in report.days:
            if set(day_obj.song_ids) <= final_ids:
                synced_dates.append(day_obj.business_date)
                state["synced_dates"][day_obj.business_date] = {
                    "business_date": day_obj.business_date,
                    "operation_id": day_obj.operation_id,
                    "music_head": day_obj.music_head,
                    "track_ids": day_obj.song_ids,
                    "cloud_synced": True,
                    "verified_at": utc_now(),
                }
        state["cumulative_track_ids"] = ordered_final
        state["last_verified_music_head"] = report.days[0].music_head
        state["last_updated_at"] = utc_now()
        atomic_write_json(checkpoint_path, state)

    await ctx.emit("entity.music.playlist_synced", {
        "mode": "daily_immutable",
        "playlist_id": PROTECTED_DAILY_PLAYLIST_ID,
        "playlist_name": PROTECTED_DAILY_PLAYLIST_NAME,
        "business_date": "" if sync_all else report.days[0].business_date,
        "added_count": len(added_ids),
        "verified_count": len(report.target_song_ids),
        "cloud_synced": True,
    })
    return CapabilityResult.ok({
        "mode": "daily_immutable",
        "cumulative_playlist_id": PROTECTED_DAILY_PLAYLIST_ID,
        "cumulative_playlist_name": PROTECTED_DAILY_PLAYLIST_NAME,
        "business_date": "" if sync_all else report.days[0].business_date,
        "dates_synced": len(synced_dates),
        "synced_dates": synced_dates,
        "added_ids": added_ids,
        "added_count": len(added_ids),
        "already_present_count": len(report.target_song_ids) - len(added_ids),
        "verified_count": len(report.target_song_ids),
        "total_cumulative_tracks": len(ordered_final),
        "readback_verified": True,
        "cloud_synced": True,
        "live_apple_music_synced": True,
        "dry_run": False,
        "state_file": str(checkpoint_path),
        "history_anomalies": report.history_anomalies,
    })
