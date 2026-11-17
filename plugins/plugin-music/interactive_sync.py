"""Fail-closed interactive ephemeral playlist & playback queue logic."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .apple_client import AppleMusicClientProtocol, AppleMusicCredentials, extract_catalog_ids
    from .daily_sync import validate_numeric_ids
    from .state_store import (
        DEFAULT_INTERACTIVE_PLAYLIST,
        PROTECTED_DAILY_PLAYLIST_ID,
        GateError,
        acquire_state_lock,
        atomic_write_json,
        collect_protected_ids,
        is_protected_playlist,
        normalize_daily_state,
        read_private_json,
        resolve_daily_checkpoint_path,
        resolve_interactive_store_path,
        resolve_playback_queue_path,
        utc_now,
    )
else:
    from apple_client import AppleMusicClientProtocol, AppleMusicCredentials, extract_catalog_ids
    from daily_sync import validate_numeric_ids
    from state_store import (
        DEFAULT_INTERACTIVE_PLAYLIST,
        PROTECTED_DAILY_PLAYLIST_ID,
        GateError,
        acquire_state_lock,
        atomic_write_json,
        collect_protected_ids,
        is_protected_playlist,
        normalize_daily_state,
        read_private_json,
        resolve_daily_checkpoint_path,
        resolve_interactive_store_path,
        resolve_playback_queue_path,
        utc_now,
    )

ClientBuilder = Callable[[PluginContext, bool], tuple[AppleMusicClientProtocol, AppleMusicCredentials]]
CacheTracksFn = Callable[[PluginContext, list[Mapping[str, Any]]], None]


def _extract_interactive_tracks(payload: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    raw_ids = payload.get("track_ids")
    raw_tracks = payload.get("tracks")
    ids: list[str] = []
    tracks: list[dict[str, Any]] = []
    if raw_ids is not None:
        ids = validate_numeric_ids(raw_ids, "track_ids")
        tracks = [{"id": sid, "title": "", "artist": "", "album": "", "storefront": "cn"} for sid in ids]
    elif raw_tracks is not None:
        if not isinstance(raw_tracks, list) or not raw_tracks:
            raise GateError("invalid_argument", "tracks 必须是非空对象列表")
        for item in raw_tracks:
            if not isinstance(item, Mapping):
                raise GateError("invalid_argument", "tracks 条目必须是对象")
            sid = str(item.get("song_id") or item.get("id") or "").strip()
            if not sid.isdigit():
                raise GateError("invalid_argument", f"无效的 song_id: {sid!r}")
            ids.append(sid)
            tracks.append({
                "id": sid,
                "title": str(item.get("title") or ""),
                "artist": str(item.get("artist") or ""),
                "album": str(item.get("album") or ""),
                "storefront": str(item.get("storefront") or "cn").lower(),
            })
    else:
        raise GateError("invalid_argument", "必须提供非空 track_ids 列表")
    return ids, tracks


async def execute_interactive(
    ctx: PluginContext,
    action: str,
    payload: Mapping[str, Any],
    *,
    build_client: ClientBuilder,
    cache_tracks: CacheTracksFn,
    verified_cache: set[str],
    unplayable_cache: set[str],
) -> CapabilityResult:
    """Execute `write_interactive_playlist` or `enqueue_playback` under strict isolation."""
    checkpoint_path = resolve_daily_checkpoint_path(str(ctx.get_config("daily_checkpoint_path") or ""), ctx.data_dir)
    daily_state = normalize_daily_state(read_private_json(checkpoint_path))
    protected_ids = collect_protected_ids(daily_state)

    req_pid = str(payload.get("playlist_id") or "").strip()
    req_cum = str(payload.get("cumulative_playlist_id") or "").strip()
    req_name = str(payload.get("playlist_name") or "").strip()
    playlist_name = req_name or DEFAULT_INTERACTIVE_PLAYLIST
    if req_cum or is_protected_playlist(req_pid, req_name, protected_ids) or is_protected_playlist("", playlist_name, protected_ids):
        raise GateError(
            "protected_daily_playlist",
            "交互模式禁止修改受保护的每日歌单 p.YJXV7dEIeb3GzZX (ChatGPT Daily 5)",
            {"protected_playlist_id": PROTECTED_DAILY_PLAYLIST_ID},
        )
    if payload.get("mode") != "interactive_ephemeral":
        raise GateError("invalid_argument", f"{action} 只接受 mode='interactive_ephemeral'")
    if "storefront" in payload and str(payload["storefront"]).strip().lower() != "cn":
        raise GateError("invalid_argument", "交互模式仅支持 storefront='cn'")
    if "record_permanent_history" in payload and payload["record_permanent_history"] is not False:
        raise GateError("invalid_argument", "交互模式禁止写入永久历史账本 (record_permanent_history 必须为 false)")

    for bool_key in ("enqueue_now", "dry_run", "local_checkpoint_only", "allow_duplicates"):
        if bool_key in payload and not isinstance(payload[bool_key], bool):
            raise GateError("invalid_argument", f"{bool_key} 必须为布尔值")

    requested_ids, tracks = _extract_interactive_tracks(payload)
    raw_verified = payload.get("verified_ids")
    if raw_verified is None:
        raise GateError("invalid_argument", "交互模式写回必须传入 preflight_cn_catalog 返回的 verified_ids")
    verified_ids = validate_numeric_ids(raw_verified, "verified_ids")
    if not set(requested_ids).issubset(set(verified_ids)):
        missing_v = sorted(set(requested_ids) - set(verified_ids))
        raise GateError("invalid_argument", f"存在未通过 preflight verified_ids 校验的曲目: {', '.join(missing_v)}")
    bad_cached = sorted(set(requested_ids) & unplayable_cache)
    if bad_cached:
        raise GateError("invalid_argument", f"拒绝写入已确认国区不可播曲目: {', '.join(bad_cached)}")

    strategy = str(payload.get("write_strategy") or "append").strip()
    if strategy not in {"append", "replace_ephemeral", "replace"}:
        raise GateError("invalid_argument", "write_strategy 仅支持 append / replace_ephemeral")
    dry_run = bool(payload.get("dry_run", False))
    local_only = bool(payload.get("local_checkpoint_only", False))
    enqueue_now = (not dry_run) and (bool(payload.get("enqueue_now", False)) or action == "enqueue_playback")
    wants_playlist = action == "write_interactive_playlist"
    wants_cloud = wants_playlist and not dry_run and not local_only
    if wants_cloud and strategy != "append":
        raise GateError("invalid_argument", "云端交互歌单仅支持 append，替换请使用 local_checkpoint_only")

    local_id = req_pid if (req_pid and not req_pid.startswith("p.")) else (
        "ix-pl-" + hashlib.sha256(playlist_name.encode("utf-8")).hexdigest()[:12]
    )
    if dry_run:
        return CapabilityResult.ok({
            "mode": "interactive_ephemeral",
            "dry_run": True,
            "playlist_id": local_id,
            "playlist_name": playlist_name,
            "planned_track_ids": requested_ids,
            "cloud_synced": False,
            "live_apple_music_synced": False,
            "local_staged": False,
            "enqueued": False,
            "record_permanent_history": False,
        })

    if not local_only and (wants_cloud or enqueue_now):
        client, _ = build_client(ctx, wants_cloud)
        pre = await client.preflight_cn_catalog(requested_ids, "cn")
        if not pre["all_playable"]:
            for bad in (*pre["unavailable"], *pre["not_found"]):
                unplayable_cache.add(bad)
            raise GateError("service_offline", "交互曲目实时国区预检失败", {
                "unavailable": pre["unavailable"],
                "not_found": pre["not_found"],
            })
        verified_cache.update(pre["verified_ids"])
        cache_tracks(ctx, pre.get("tracks", []))
        tracks = pre["tracks"]
    else:
        named_tracks = [tr for tr in tracks if tr.get("title")]
        if named_tracks:
            cache_tracks(ctx, named_tracks)

    interactive_file = resolve_interactive_store_path(ctx.data_dir)
    cloud_synced = False
    local_staged = False
    apple_pid = req_pid if req_pid.startswith("p.") else ""
    final_ids = list(requested_ids)

    if wants_playlist:
        with acquire_state_lock(interactive_file):
            store = read_private_json(interactive_file)
            playlists = list(store.get("playlists")) if isinstance(store.get("playlists"), list) else []
            entry = next((p for p in playlists if isinstance(p, dict) and p.get("id") == local_id), None)
            if entry is None:
                entry = {
                    "id": local_id, "name": playlist_name, "mode": "interactive_ephemeral",
                    "allow_historical_repeats": True, "cloud_synced": False,
                    "track_ids": [], "staged_track_ids": [],
                }
                playlists.append(entry)
            if not apple_pid:
                apple_pid = str(entry.get("apple_music_playlist_id") or "")
            if is_protected_playlist(apple_pid, playlist_name, protected_ids):
                raise GateError("protected_daily_playlist", "目标歌单受 Daily 5 保护")

            if wants_cloud:
                client, _ = build_client(ctx, True)
                if not apple_pid:
                    matches = [
                        p for p in await client.list_library_playlists()
                        if str(p.get("attributes", {}).get("name") or "") == playlist_name
                    ]
                    if len(matches) > 1:
                        raise GateError("conflict", "云端存在多个同名交互歌单")
                    apple_pid = str(matches[0].get("id") or "") if matches else ""
                if apple_pid:
                    meta = await client.get_playlist_meta(apple_pid)
                    if is_protected_playlist(meta["id"], meta["name"], protected_ids):
                        raise GateError("protected_daily_playlist", "远端歌单实为受保护的 ChatGPT Daily 5")
                    remote_now = await client.get_playlist_tracks(apple_pid)
                    missing = [sid for sid in requested_ids if sid not in extract_catalog_ids(remote_now)]
                    if missing:
                        await client.append_playlist_tracks(apple_pid, missing)
                    _, _, final_ids = await client.verify_readback(apple_pid, set(requested_ids))
                else:
                    created = await client.create_library_playlist(
                        playlist_name, DEFAULT_INTERACTIVE_PLAYLIST, requested_ids
                    )
                    apple_pid = created["id"]
                    _, _, final_ids = await client.verify_readback(apple_pid, set(requested_ids))
                entry.update(
                    apple_music_playlist_id=apple_pid,
                    cloud_synced=True,
                    track_ids=final_ids,
                    staged_track_ids=final_ids,
                    updated_at=utc_now(),
                )
                cloud_synced = True
            else:
                staged: list[str] = []
                seen: set[str] = set()
                if strategy == "append":
                    for prev in entry.get("staged_track_ids", []):
                        if isinstance(prev, str) and prev not in seen:
                            seen.add(prev)
                            staged.append(prev)
                allow_dup = bool(payload.get("allow_duplicates", False))
                for sid in requested_ids:
                    if allow_dup or sid not in seen:
                        seen.add(sid)
                        staged.append(sid)
                entry.update(cloud_synced=False, staged_track_ids=staged, track_ids=staged, updated_at=utc_now())
                final_ids = staged
                local_staged = True

            atomic_write_json(interactive_file, {"schema_version": 2, "playlists": playlists})

    queue_file = resolve_playback_queue_path(ctx.data_dir)
    if enqueue_now:
        queue_items = [
            {
                "id": str(tr.get("id") or ""),
                "title": str(tr.get("title") or str(tr.get("id") or "")),
                "artist": str(tr.get("artist") or ""),
                "album": str(tr.get("album") or ""),
                "play_url": str(tr.get("url") or f"https://music.apple.com/cn/song/{tr.get('id')}"),
                "storefront": "cn",
                "added_at": utc_now(),
            }
            for tr in tracks
        ]
        with acquire_state_lock(queue_file):
            atomic_write_json(queue_file, {"version": 1, "backend": "libmpv", "items": queue_items})
        if not local_only and queue_items:
            await ctx.emit("entity.media.open", {
                "item_id": f"ix-am-{queue_items[0]['id']}",
                "source_id": "plugin-music",
                "title": queue_items[0]["title"],
                "kind": "music",
                "play_url": queue_items[0]["play_url"],
            })

    if cloud_synced:
        await ctx.emit("entity.music.playlist_synced", {
            "mode": "interactive_ephemeral",
            "playlist_id": apple_pid or local_id,
            "playlist_name": playlist_name,
            "added_count": len(requested_ids),
            "verified_count": len(requested_ids),
            "cloud_synced": True,
        })

    return CapabilityResult.ok({
        "mode": "interactive_ephemeral",
        "playlist_id": local_id,
        "apple_music_playlist_id": apple_pid if cloud_synced else "",
        "playlist_name": playlist_name,
        "write_strategy": strategy,
        "record_permanent_history": False,
        "track_ids": final_ids,
        "verified_count": len(requested_ids) if cloud_synced else 0,
        "readback_verified": cloud_synced,
        "cloud_synced": cloud_synced,
        "live_apple_music_synced": cloud_synced,
        "local_staged": local_staged,
        "enqueued": enqueue_now,
        "playlists_file": str(interactive_file),
        "queue_file": str(queue_file) if enqueue_now else "",
    })
