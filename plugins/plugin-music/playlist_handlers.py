"""Fail-closed `media.playlist.v1` router, CN preflight, and safe status reporting."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext
from omnidesk_sdk.stores import redact_secrets

if __package__:
    from .apple_client import (
        AppleMusicApiError,
        AppleMusicClient,
        AppleMusicClientProtocol,
        AppleMusicCredentials,
        extract_catalog_ids,
    )
    from .curation_gate import scan_repo_anomalies, verify_clean_git_repo
    from .daily_sync import execute_sync_daily, validate_numeric_ids
    from .interactive_sync import execute_interactive
    from .legacy_credentials import load_legacy_credentials
    from .qwen_bridge import QwenModules, load_qwen_modules, probe_adam_listener
    from .state_store import (
        PROTECTED_DAILY_PLAYLIST_ID,
        PROTECTED_DAILY_PLAYLIST_NAME,
        GateError,
        atomic_write_json,
        is_valid_iso_date,
        normalize_daily_state,
        read_private_json,
        resolve_daily_checkpoint_path,
        resolve_interactive_store_path,
        resolve_playback_queue_path,
        resolve_repo_dir,
        utc_now,
    )
else:
    from apple_client import (
        AppleMusicApiError,
        AppleMusicClient,
        AppleMusicClientProtocol,
        AppleMusicCredentials,
        extract_catalog_ids,
    )
    from curation_gate import scan_repo_anomalies, verify_clean_git_repo
    from daily_sync import execute_sync_daily, validate_numeric_ids
    from interactive_sync import execute_interactive
    from legacy_credentials import load_legacy_credentials
    from qwen_bridge import QwenModules, load_qwen_modules, probe_adam_listener
    from state_store import (
        PROTECTED_DAILY_PLAYLIST_ID,
        PROTECTED_DAILY_PLAYLIST_NAME,
        GateError,
        atomic_write_json,
        is_valid_iso_date,
        normalize_daily_state,
        read_private_json,
        resolve_daily_checkpoint_path,
        resolve_interactive_store_path,
        resolve_playback_queue_path,
        resolve_repo_dir,
        utc_now,
    )

_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "status": frozenset({"action", "include_remote"}),
    "list_playlists": frozenset({"action", "include_remote"}),
    "preflight_cn_catalog": frozenset({
        "action", "storefront", "track_ids", "allow_offline_catalog_fallback",
    }),
    "sync_daily_authorized": frozenset({
        "action", "mode", "storefront", "business_date", "operation_id", "qa_verified",
        "publication_evidence", "dry_run", "local_checkpoint_only", "sync_all_rehydrated",
        "cumulative_playlist_id", "create_dated_playlist", "dated_playlist_prefix", "track_ids",
    }),
    "write_interactive_playlist": frozenset({
        "action", "mode", "storefront", "playlist_id", "cumulative_playlist_id", "playlist_name",
        "write_strategy", "enqueue_now", "dry_run", "local_checkpoint_only",
        "record_permanent_history", "track_ids", "tracks", "verified_ids", "allow_duplicates",
    }),
    "enqueue_playback": frozenset({
        "action", "mode", "storefront", "playlist_id", "cumulative_playlist_id", "playlist_name",
        "write_strategy", "enqueue_now", "dry_run", "local_checkpoint_only",
        "record_permanent_history", "track_ids", "tracks", "verified_ids", "allow_duplicates",
    }),
}


def _fail(code: str, message: str, *, secrets: tuple[str, ...] = (), **extra: Any) -> CapabilityResult:
    safe_msg = redact_secrets(message, secrets)
    return CapabilityResult.fail(
        safe_msg,
        data={"cloud_synced": False, "live_apple_music_synced": False, **extra},
        error_code=code,
    )


class PlaylistService:
    """Stateful handler implementing `media.playlist.v1` actions for `plugin-music`."""

    def __init__(
        self,
        ctx_getter: Callable[[], PluginContext | None],
        *,
        client_factory: Callable[[AppleMusicCredentials, dict[str, Any]], AppleMusicClientProtocol] | None = None,
        qwen_loader: Callable[[str], QwenModules] | None = None,
    ) -> None:
        self._get_ctx = ctx_getter
        self._client_factory = client_factory
        self._qwen_loader = qwen_loader or load_qwen_modules
        self._verified_ids: set[str] = set()
        self._unplayable_ids: set[str] = set()
        self._credential_source = "none"

    def _ctx(self) -> PluginContext:
        ctx = self._get_ctx()
        if ctx is None:
            raise GateError("service_offline", "plugin-music 尚未完成初始化")
        return ctx

    def _credentials(self, ctx: PluginContext) -> AppleMusicCredentials:
        dev = str(ctx.get_secret("apple_music_developer_token") or "").strip()
        user = str(ctx.get_secret("apple_music_user_token") or "").strip()
        cookie = str(ctx.get_secret("apple_music_cookie") or "").strip()
        source = "secret_vault" if dev or user or cookie else "none"
        if not dev or not user:
            legacy = load_legacy_credentials(ctx.data_dir)
            if legacy.usable:
                dev = dev or legacy.developer_token
                user = user or legacy.user_token
                source = "secret_vault+adam_local" if source == "secret_vault" else legacy.source
        self._credential_source = source
        return AppleMusicCredentials(developer_token=dev, user_token=user, cookie=cookie)

    def _build_client(
        self, ctx: PluginContext, require_write: bool
    ) -> tuple[AppleMusicClientProtocol, AppleMusicCredentials]:
        creds = self._credentials(ctx)
        if not creds.has_catalog_auth:
            raise GateError("not_configured", "尚未在 SecretVault 配置 apple_music_developer_token")
        if require_write and not creds.has_write_auth:
            raise GateError("not_configured", "尚未在 SecretVault 配置 apple_music_user_token")
        cfg = ctx.get_config()
        if self._client_factory is not None:
            return self._client_factory(creds, cfg), creds
        client = AppleMusicClient(
            creds,
            base_url=str(cfg.get("apple_music_api_base") or "https://amp-api.music.apple.com"),
            proxy=str(cfg.get("proxy") or ""),
        )
        return client, creds

    def _cache_verified_tracks(self, ctx: PluginContext, tracks: list[Mapping[str, Any]]) -> None:
        if not tracks:
            return
        cache_path = ctx.data_dir.expanduser().resolve() / "catalog_cache.json"
        try:
            existing = read_private_json(cache_path)
        except GateError:
            existing = {}
        stored = dict(existing.get("tracks")) if isinstance(existing.get("tracks"), Mapping) else {}
        for tr in tracks:
            sid = str(tr.get("id") or "").strip()
            if sid.isdigit():
                stored[sid] = dict(tr)
        atomic_write_json(cache_path, {"version": 1, "updated_at": utc_now(), "tracks": stored})

    async def handle(self, req: Any) -> CapabilityResult:
        """Route and validate a `media.playlist.v1` request failing closed on invalid input."""
        if not isinstance(req, Mapping):
            return _fail("invalid_argument", "media.playlist.v1 请求必须是 JSON 对象")
        payload = dict(req)
        action = str(payload.get("action") or "").strip()
        if action not in _ALLOWED_KEYS:
            return _fail("invalid_argument", f"不支持的 media.playlist.v1 动作: {action!r}")
        unknown = sorted(set(payload) - _ALLOWED_KEYS[action])
        if unknown:
            return _fail("invalid_argument", f"未知参数不被允许: {', '.join(unknown)}")

        try:
            ctx = self._ctx()
            creds = self._credentials(ctx)
            secrets = creds.secret_values()
        except GateError as exc:
            return _fail(exc.error_code, exc.message)

        try:
            if action in {"status", "list_playlists"}:
                return await self._handle_status(ctx, payload, creds)
            if action == "preflight_cn_catalog":
                return await self._handle_preflight(ctx, payload)
            if action == "sync_daily_authorized":
                return await execute_sync_daily(
                    ctx,
                    payload,
                    creds=creds,
                    has_custom_factory=self._client_factory is not None,
                    build_client=self._build_client,
                    cache_tracks=self._cache_verified_tracks,
                    verified_cache=self._verified_ids,
                )
            return await execute_interactive(
                ctx,
                action,
                payload,
                build_client=self._build_client,
                cache_tracks=self._cache_verified_tracks,
                verified_cache=self._verified_ids,
                unplayable_cache=self._unplayable_ids,
            )
        except GateError as exc:
            return _fail(exc.error_code, exc.message, secrets=secrets, **exc.extra)
        except AppleMusicApiError as exc:
            return _fail(exc.error_code, exc.message, secrets=secrets, **exc.details)

    async def _handle_preflight(self, ctx: PluginContext, payload: Mapping[str, Any]) -> CapabilityResult:
        if "allow_offline_catalog_fallback" in payload:
            val = payload["allow_offline_catalog_fallback"]
            if not isinstance(val, bool) or val:
                raise GateError("invalid_argument", "禁止使用离线/历史文件代替实时国区可播性预检")
        sf = str(payload.get("storefront", "cn") or "cn").strip().lower()
        if sf != "cn":
            raise GateError("invalid_argument", "preflight_cn_catalog 仅支持 storefront='cn'")
        ids = validate_numeric_ids(payload.get("track_ids"), "track_ids")
        client, creds = self._build_client(ctx, require_write=False)
        try:
            report = await client.preflight_cn_catalog(ids, sf)
        except AppleMusicApiError as exc:
            return _fail(
                exc.error_code,
                exc.message,
                secrets=creds.secret_values(),
                storefront=sf,
                requested_count=len(list(dict.fromkeys(ids))),
                verified_count=0,
                all_playable=False,
                verified_ids=[],
                unavailable=[],
                not_found=[],
                unplayable_ids=list(dict.fromkeys(ids)),
                tracks=[],
                error=exc.message,
            )

        self._verified_ids.update(str(x) for x in report.get("verified_ids", []))
        for bad in (*report.get("unavailable", []), *report.get("not_found", [])):
            self._verified_ids.discard(str(bad))
            self._unplayable_ids.add(str(bad))
        self._cache_verified_tracks(ctx, report.get("tracks", []))
        return CapabilityResult.ok(report)

    async def _handle_status(
        self, ctx: PluginContext, payload: Mapping[str, Any], creds: AppleMusicCredentials
    ) -> CapabilityResult:
        if "include_remote" in payload and not isinstance(payload["include_remote"], bool):
            raise GateError("invalid_argument", "include_remote 必须为布尔值")
        include_remote = bool(payload.get("include_remote", False))

        cfg = ctx.get_config()
        qwen = self._qwen_loader(str(cfg.get("qwen_translator_dir") or ""))
        music_repo = resolve_repo_dir(str(cfg.get("music_daily_dir") or ""), ctx.data_dir, "music-daily")
        vault_repo = resolve_repo_dir(str(cfg.get("obsidian_vault_dir") or ""), ctx.data_dir, "obsidian-vault")
        checkpoint_path = resolve_daily_checkpoint_path(str(cfg.get("daily_checkpoint_path") or ""), ctx.data_dir)
        interactive_file = resolve_interactive_store_path(ctx.data_dir)
        queue_file = resolve_playback_queue_path(ctx.data_dir)

        issues: list[str] = []
        try:
            daily_state = normalize_daily_state(read_private_json(checkpoint_path))
        except GateError:
            daily_state = normalize_daily_state({})
            issues.append("corrupt_daily_checkpoint")
        try:
            ix_store = read_private_json(interactive_file)
        except GateError:
            ix_store = {}
            issues.append("corrupt_interactive_store")
        try:
            queue_data = read_private_json(queue_file)
        except GateError:
            queue_data = {}

        dates: list[str] = []
        anomalies: list[dict[str, Any]] = []
        if music_repo is not None and (music_repo / ".git").exists():
            try:
                head = await verify_clean_git_repo(ctx, music_repo, "music-daily")
                dates, anomalies = await scan_repo_anomalies(ctx, music_repo, head)
            except GateError:
                issues.append("music_repo_unverified_or_dirty")
        elif music_repo is not None and (music_repo / "recommendations").is_dir():
            dates = sorted(p.stem for p in (music_repo / "recommendations").glob("*/*/*.json") if is_valid_iso_date(p.stem))
        else:
            issues.append("missing_curation_repos")

        if not creds.has_write_auth:
            issues.append("missing_apple_music_credentials")
        if daily_state["pending_song_ids"]:
            issues.append("pending_daily_write_unconfirmed")
        if anomalies:
            issues.append("history_anomaly_2026_11_06")

        remote_playlists: list[dict[str, Any]] = []
        remote_coverage: dict[str, Any] | None = None
        remote_reachable: bool | None = None
        if include_remote:
            if not creds.has_write_auth and self._client_factory is None:
                remote_reachable = False
                issues.append("remote_apple_music_unconfigured")
            else:
                try:
                    client, _ = self._build_client(ctx, require_write=True)
                    raw_remote = await client.list_library_playlists()
                    remote_playlists = [
                        {
                            "id": str(item.get("id") or ""),
                            "name": str(item.get("attributes", {}).get("name") or ""),
                        }
                        for item in raw_remote
                    ]
                    remote_tracks = await client.get_playlist_tracks(PROTECTED_DAILY_PLAYLIST_ID)
                    remote_ids = extract_catalog_ids(remote_tracks)
                    manifests: list[tuple[str, list[str]]] = []
                    if music_repo is not None and (music_repo / "recommendations").is_dir():
                        for manifest_path in sorted((music_repo / "recommendations").glob("*/*/*.json")):
                            try:
                                doc = json.loads(manifest_path.read_text(encoding="utf-8"))
                            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                                continue
                            raw_tracks = doc.get("tracks") if isinstance(doc, Mapping) else None
                            tracks = raw_tracks if isinstance(raw_tracks, list) else []
                            ids = [
                                str((track.get("apple_music") or {}).get("song_id") or "")
                                for track in tracks
                                if isinstance(track, Mapping)
                            ]
                            if ids:
                                manifests.append((manifest_path.stem, ids))
                    partial = [
                        {
                            "date": day,
                            "present": sum(1 for sid in ids if sid in remote_ids),
                            "expected": len(ids),
                        }
                        for day, ids in manifests
                        if 0 < sum(1 for sid in ids if sid in remote_ids) < len(ids)
                    ]
                    missing = [
                        day
                        for day, ids in manifests
                        if not any(sid in remote_ids for sid in ids)
                    ]
                    remote_coverage = {
                        "total_dates": len(manifests),
                        "fully_synced_dates": len(manifests) - len(partial) - len(missing),
                        "partial_dates": partial,
                        "missing_dates": missing,
                        "remote_track_count": len(remote_ids),
                    }
                    remote_reachable = True
                except (GateError, AppleMusicApiError):
                    remote_reachable = False
                    issues.append("remote_apple_music_unreachable")

        ix_playlists = list(ix_store.get("playlists")) if isinstance(ix_store.get("playlists"), list) else []
        queue_items = list(queue_data.get("items")) if isinstance(queue_data.get("items"), list) else []

        return CapabilityResult.ok({
            "degraded": bool(issues),
            "auth": {
                "has_developer_token": creds.has_catalog_auth,
                "has_user_token": bool(creds.user_token.strip()),
                "has_cookie": bool(creds.cookie.strip()),
                "has_write_auth": creds.has_write_auth,
                "credential_source": self._credential_source,
                "adam_listener_ready": probe_adam_listener(qwen),
                "qwen_translator": qwen.status_dict(),
            },
            "catalog": {
                "storefront": str(cfg.get("storefront") or "cn").lower(),
                "repos_configured": bool(music_repo and vault_repo),
                "music_daily_dir": str(music_repo) if music_repo else "",
                "obsidian_vault_dir": str(vault_repo) if vault_repo else "",
                "archive_dates_count": len(dates),
                "latest_business_date": dates[-1] if dates else None,
                "history_anomalies": anomalies,
            },
            "writeback": {
                "daily_playlist_id": PROTECTED_DAILY_PLAYLIST_ID,
                "daily_checkpoint_path": str(checkpoint_path),
                "daily_checkpoint_exists": checkpoint_path.is_file(),
                "synced_dates_count": len(daily_state["synced_dates"]),
                "cumulative_track_count": len(daily_state["cumulative_track_ids"]),
                "pending_song_ids": daily_state["pending_song_ids"],
                "interactive_playlists_file": str(interactive_file),
                "interactive_playlists_count": len(ix_playlists),
                "playback_queue_file": str(queue_file),
                "playback_queue_count": len(queue_items),
                "remote_reachable": remote_reachable,
            },
            "playlist_health": {
                "healthy": not any(
                    i in {"corrupt_daily_checkpoint", "corrupt_interactive_store", "pending_daily_write_unconfirmed"}
                    for i in issues
                ),
                "issues": issues,
                "protected_daily_playlist": {
                    "playlist_id": daily_state["cumulative_playlist_id"],
                    "name": PROTECTED_DAILY_PLAYLIST_NAME,
                    "synced_dates_count": len(daily_state["synced_dates"]),
                    "cumulative_track_count": len(daily_state["cumulative_track_ids"]),
                    "pending_song_ids": daily_state["pending_song_ids"],
                    "state_file": str(checkpoint_path),
                },
                "interactive_playlists": ix_playlists,
                "remote_playlists": remote_playlists,
                "remote_coverage": remote_coverage,
            },
        })
