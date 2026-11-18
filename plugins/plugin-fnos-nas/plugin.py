"""OmniDesk v2 plugin-fnos-nas: L1/L2/L3 progressive fnOS NAS media & sync connector."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, WorkspaceTabSpec

if __package__:
    from . import book_sync, lan_http, library, matcher, safety, subtitles
else:
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import book_sync
    import lan_http
    import library
    import matcher
    import safety
    import subtitles


class FnosNasPlugin(BasePlugin):
    """Hybrid Media plugin providing progressive L1/L2/L3 fnOS NAS integration."""

    def __init__(self) -> None:
        self._http_client = lan_http.LanHttpClient(trust_env=False, follow_redirects=False)
        self._snapshot_cache: tuple[tuple[str, str, str], library.LibrarySnapshot] | None = None
        self._media_source = library.FnosMediaSource(self._get_snapshot)
        self._bg_task: asyncio.Task[Any] | None = None

    async def on_load(self, ctx: PluginContext) -> None:
        """Register capabilities, MediaSource, and workspace tab without blocking startup."""
        await super().on_load(ctx)
        ctx.register_capability("nas.connection.v1", self.handle_nas_connection)
        ctx.register_capability("media.source.v1", self.handle_media_source)
        ctx.register_capability("media.local_match.v1", self.handle_local_match)
        ctx.register_capability("media.clip_jump.v1", self.handle_clip_jump)
        ctx.register_capability("book.sync.v1", self.handle_book_sync)
        ctx.register_media_source(self._media_source)
        ctx.register_workspace_tab(
            WorkspaceTabSpec(
                tab_id="fnos-nas",
                title="飞牛 fnOS NAS",
                domain="media",
                badge="NAS",
                ui_schema=self._build_ui_schema(),
                render=self._build_ui_schema,
                on_action=self.handle_tab_action,
            )
        )
        interval = self._int_cfg("refresh_interval_sec", 0)
        if interval > 0 and str(self._cfg().get("mount_root") or "").strip():
            self._bg_task = ctx.spawn_task(self._refresh_loop(max(5, interval)), name="fnos-nas-refresh")

    async def on_unload(self) -> None:
        """Cancel background tasks and release cached snapshots on unload."""
        if self._bg_task is not None and not self._bg_task.done():
            self._bg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bg_task
        self._bg_task = None
        self._snapshot_cache = None

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight local readiness report without external network calls."""
        cfg = self._cfg()
        raw_mount = str(cfg.get("mount_root") or "").strip()
        mount_ok = False
        if raw_mount:
            try:
                safety.validate_mount_root(raw_mount)
                mount_ok = True
            except safety.MountValidationError:
                mount_ok = False
        return {
            "ok": True,
            "plugin_id": self.ctx.plugin_id if self.ctx else "plugin-fnos-nas",
            "configured": bool(raw_mount),
            "tiers": self._tier_summary(cfg, l1_ready=mount_ok),
        }

    async def _refresh_loop(self, interval_sec: int) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            await asyncio.to_thread(self._get_snapshot, True)

    def _cfg(self) -> dict[str, Any]:
        return self.ctx.get_config() if self.ctx is not None else {}

    def _int_cfg(self, key: str, default: int) -> int:
        raw = self._cfg().get(key, default)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def _secret(self, key: str) -> str | None:
        return self.ctx.get_secret(key) if self.ctx is not None else None

    def _tier_summary(self, cfg: Mapping[str, Any], *, l1_ready: bool | None = None) -> dict[str, Any]:
        l1_cfg = bool(str(cfg.get("mount_root") or "").strip())
        l2_media = bool(str(cfg.get("media_service_url") or "").strip())
        l2_sync = bool(str(cfg.get("companion_sync_url") or "").strip())
        l3_radarr = bool(str(cfg.get("radarr_url") or "").strip())
        l3_sonarr = bool(str(cfg.get("sonarr_url") or "").strip())
        return {
            "l1_configured": l1_cfg,
            "l1_ready": l1_cfg if l1_ready is None else l1_ready,
            "l2_media_configured": l2_media,
            "l2_sync_configured": l2_sync,
            "l3_radarr_configured": l3_radarr,
            "l3_sonarr_configured": l3_sonarr,
        }

    def _get_snapshot(self, force_refresh: bool = False) -> library.LibrarySnapshot:
        cfg = self._cfg()
        cache_key = (
            str(cfg.get("mount_root") or "").strip(),
            str(cfg.get("book_subdir") or ""),
            str(cfg.get("music_subdirs") or ""),
        )
        if not force_refresh and self._snapshot_cache is not None and self._snapshot_cache[0] == cache_key:
            return self._snapshot_cache[1]
        snap = library.scan_library(cache_key[0], cfg)
        self._snapshot_cache = (cache_key, snap)
        return snap

    def _validated_mount_or_none(self, cfg: Mapping[str, Any]) -> Path | None:
        raw = str(cfg.get("mount_root") or "").strip()
        if not raw:
            return None
        try:
            return safety.validate_mount_root(raw)
        except safety.MountValidationError:
            return None

    async def handle_nas_connection(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `nas.connection.v1` actions: status, list_roots, test_mount, scan_now, write_subtitle."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "status").strip().lower()
        cfg = self._cfg()

        if action in {"write_subtitle", "safe_write_subtitle"}:
            return self._handle_write_subtitle(payload, cfg)
        if action not in {"status", "list_roots", "list_mounts", "test_mount", "probe", "scan_now"}:
            return CapabilityResult.fail(f"不支持的 nas.connection.v1 动作: {action!r}", error_code="invalid_argument")

        raw_mount = str(payload["mount_root"] if "mount_root" in payload and payload["mount_root"] is not None else cfg.get("mount_root") or "").strip()
        try:
            mount_root = safety.validate_mount_root(raw_mount)
        except safety.MountValidationError as exc:
            return CapabilityResult.fail(
                str(exc),
                data={"configured": bool(raw_mount), "online": False, "readable": False, "roots": [], "tiers": self._tier_summary(cfg, l1_ready=False)},
                error_code=exc.error_code,
            )

        snap = self._get_snapshot(action == "scan_now") if raw_mount == str(cfg.get("mount_root") or "").strip() else library.scan_library(str(mount_root), cfg)
        if snap.error_code:
            return CapabilityResult.fail(snap.error, data={"configured": True, "online": False, "roots": []}, error_code=snap.error_code)

        return CapabilityResult.ok({
            "configured": True,
            "online": True,
            "mount_root": str(mount_root),
            "readable": True,
            "writable": os.access(mount_root, os.W_OK),
            "roots": snap.roots,
            "categories": snap.categories,
            "item_count": len(snap.items),
            "tiers": self._tier_summary(cfg, l1_ready=True),
        })

    def _handle_write_subtitle(self, payload: Mapping[str, Any], cfg: Mapping[str, Any]) -> CapabilityResult:
        raw_mount = str(cfg.get("mount_root") or "").strip()
        try:
            mount_root = safety.validate_mount_root(raw_mount)
        except safety.MountValidationError as exc:
            return CapabilityResult.fail(str(exc), data={"written": False}, error_code=exc.error_code)

        target_path = payload.get("target_path") or payload.get("path") or payload.get("subtitle_path")
        content = payload.get("content")
        if not isinstance(target_path, str) or not target_path.strip() or not isinstance(content, str):
            return CapabilityResult.fail("write_subtitle 需要 target_path 与 content", error_code="invalid_argument")

        try:
            result = safety.safe_write_subtitle(
                mount_root,
                target_path,
                content,
                managed_dirs=str(cfg.get("managed_subtitle_dirs") or ""),
                protect_manual=bool(cfg.get("protect_manual_subtitles", True)),
                allow_overwrite_own=bool(payload.get("allow_overwrite_own", True)),
                overwrite_manual=bool(payload.get("overwrite_manual", False)),
            )
        except safety.MountValidationError as exc:
            return CapabilityResult.fail(str(exc), data={"written": False, **exc.details}, error_code=exc.error_code)

        self._snapshot_cache = None
        return CapabilityResult.ok(result)

    async def handle_media_source(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `media.source.v1` RPC wrapper around FnosMediaSource."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "fetch_items").strip().lower()
        if action == "cover":
            cache_dir = (self.ctx.data_dir / "cover_cache") if getattr(self, "ctx", None) is not None else None
            data = await asyncio.to_thread(
                library.read_cover,
                self._get_snapshot(),
                str(payload.get("item_id") or ""),
                cache_dir=cache_dir,
            )
            return CapabilityResult.ok({"data_url": data}) if data else CapabilityResult.fail("媒体目录暂无封面", error_code="not_found")
        if action == "categories":
            cats = self._media_source.categories()
            return CapabilityResult.ok({"categories": cats})
        if action in {"fetch_items", "list", "scan"}:
            force = action == "scan" or bool(payload.get("force_refresh", False))
            try:
                page = int(payload.get("page") or 1)
                page_size = int(payload.get("page_size") or 50)
            except (TypeError, ValueError):
                return CapabilityResult.fail("page 与 page_size 必须为整数", error_code="invalid_argument")
            items = await self._media_source.fetch_items(
                category=str(payload.get("category") or ""), query=str(payload.get("query") or ""),
                page=page, format=str(payload.get("format") or ""), page_size=page_size, force_refresh=force,
            )
            return CapabilityResult.ok({
                "items": [item.model_dump() for item in items], "total": len(items),
                "page": page, "categories": self._media_source.categories(),
            })
        if action == "fetch_chapters":
            chapters = await self._media_source.fetch_chapters(str(payload.get("item_id") or ""))
            return CapabilityResult.ok({"chapters": [c.model_dump() for c in chapters]})
        if action == "fetch_lyrics":
            lyrics = await self._media_source.fetch_lyrics(str(payload.get("item_id") or ""))
            return CapabilityResult.ok({"lyrics": [ln.model_dump() for ln in lyrics]})
        return CapabilityResult.fail(f"不支持的 media.source.v1 动作: {action!r}", error_code="invalid_argument")

    async def handle_local_match(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `media.local_match.v1` local/Arr matching."""
        payload = dict(req) if req is not None else {}
        cfg = self._cfg()
        snap = self._get_snapshot(bool(payload.get("force_refresh", False)))
        return await matcher.run_local_match(
            payload,
            snap,
            cfg,
            radarr_key=self._secret("radarr_api_key"),
            sonarr_key=self._secret("sonarr_api_key"),
            http_client=self._http_client,
            timeout_sec=float(self._int_cfg("http_timeout_sec", 10)),
        )

    async def handle_clip_jump(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `media.clip_jump.v1` bilingual subtitle search and optional bus jump emit."""
        payload = dict(req) if req is not None else {}
        query = str(payload.get("query") or "").strip()
        if not query:
            return CapabilityResult.fail("缺少字幕检索关键词 (query)", error_code="invalid_argument")

        cfg = self._cfg()
        raw_mount = str(cfg.get("mount_root") or "").strip()
        media_svc_raw = str(cfg.get("media_service_url") or "").strip()
        if not raw_mount and not media_svc_raw:
            return CapabilityResult.fail("尚未配置 NAS 挂载目录或字幕检索服务", data={"clips": [], "count": 0}, error_code="not_configured")

        series_filter = str(payload.get("series_filter") or "").strip()
        limit = max(1, min(int(payload.get("limit") or 10), 100))
        clips: list[dict[str, Any]] = []

        if raw_mount:
            snap = self._get_snapshot(bool(payload.get("force_refresh", False)))
            if snap.error_code and not media_svc_raw:
                return CapabilityResult.fail(snap.error, data={"clips": [], "count": 0}, error_code=snap.error_code)
            if snap.mount_root is not None:
                video_recs = [r for r in snap.item_records if r.get("category") in {"电影", "剧集"}]
                clips.extend(subtitles.search_local_subtitles(snap.mount_root, query, video_items=video_recs, series_filter=series_filter, limit=limit))

        if media_svc_raw:
            try:
                svc_url = lan_http.validate_lan_url(media_svc_raw, label="Media Service")
            except lan_http.LanHttpError as exc:
                if not clips:
                    return CapabilityResult.fail(str(exc), data={"clips": [], "count": 0}, error_code=exc.error_code)
            else:
                token = self._secret("media_service_token")
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                secrets = (token,) if token else ()
                try:
                    remote = await self._http_client.request_json(
                        "GET", f"{svc_url}/subtitles/search", headers=headers,
                        query={"q": query, "series": series_filter, "limit": limit, "target_language": cfg.get("target_language", "zh-CN")},
                        timeout_sec=float(self._int_cfg("http_timeout_sec", 10)), secrets=secrets, service_label="Media Service",
                    )
                    clips.extend(_normalize_remote_clips(remote))
                except lan_http.LanHttpError as exc:
                    if not clips or exc.error_code == "permission_denied":
                        return CapabilityResult.fail(
                            safety.redact_secrets(str(exc), secrets),
                            data={"clips": [], "count": 0, "http_status": exc.http_status},
                            error_code=exc.error_code,
                        )

        clips = clips[:limit]
        bus_published = False
        if bool(payload.get("auto_open_player", False)) and clips and self.ctx is not None:
            first_open = clips[0].get("open_payload")
            if isinstance(first_open, Mapping) and first_open.get("play_url"):
                await self.ctx.emit("entity.media.open", dict(first_open))
                bus_published = True

        return CapabilityResult.ok({
            "query": query,
            "matched": bool(clips),
            "count": len(clips),
            "clips": clips,
            "bus_published": bus_published,
        })

    async def handle_book_sync(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `book.sync.v1` local-first reading progress & vocabulary sync."""
        assert self.ctx is not None
        cfg = self._cfg()
        return await book_sync.run_book_sync(
            dict(req) if req is not None else {},
            data_dir=self.ctx.data_dir,
            mount_root=self._validated_mount_or_none(cfg),
            companion_url_raw=str(cfg.get("companion_sync_url") or ""),
            companion_token=self._secret("companion_sync_token"),
            http_client=self._http_client,
            timeout_sec=float(self._int_cfg("http_timeout_sec", 10)),
        )

    def _build_ui_schema(self) -> dict[str, Any]:
        cfg = self._cfg()
        tiers = self._tier_summary(cfg)
        return {
            "type": "Section", "id": "fnos-nas-root", "title": "飞牛 fnOS NAS 媒体与伴读连接器",
            "description": "L1 本地挂载直通 · L2 伴随台词检索与进度同步 · L3 Radarr/Sonarr 片源核验",
            "children": [
                {
                    "type": "KeyValue", "id": "fnos-nas-status",
                    "items": [
                        {"key": "mount_root", "label": "L1 挂载根目录", "value": str(cfg.get("mount_root") or "(未配置)")},
                        {"key": "l2_media", "label": "L2 字幕检索服务", "value": "已配置" if tiers["l2_media_configured"] else "未配置"},
                        {"key": "l2_sync", "label": "L2 阅读同步服务", "value": "已配置" if tiers["l2_sync_configured"] else "未配置"},
                        {"key": "l3_arr", "label": "L3 Radarr / Sonarr", "value": "已配置" if (tiers["l3_radarr_configured"] or tiers["l3_sonarr_configured"]) else "未配置"},
                    ],
                },
                {
                    "type": "ActionRow", "id": "fnos-nas-actions",
                    "actions": [{"id": "test_mount", "label": "探测挂载目录", "variant": "primary"}, {"id": "scan_now", "label": "立即扫描媒体库", "variant": "secondary"}],
                },
            ],
        }

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if action_id in {"test_mount", "scan_now"}:
            res = await self.handle_nas_connection({"action": action_id})
            return {"ok": res.ok, "result": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}
        return {"ok": False, "error": f"Unsupported action: {action_id}", "ui_schema": self._build_ui_schema()}


def _normalize_remote_clips(remote: Any) -> list[dict[str, Any]]:
    raw_items = remote if isinstance(remote, list) else (remote.get("clips") or remote.get("items") or remote.get("results") or [] if isinstance(remote, Mapping) else [])
    out: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        start_sec = float(item.get("start_sec") or item.get("start") or 0.0)
        end_sec = float(item.get("end_sec") or item.get("end") or start_sec)
        play_url = str(item.get("play_url") or "")
        title = str(item.get("title") or item.get("show") or "Remote Subtitle Match")
        item_id = str(item.get("item_id") or f"remote:{title}:{start_sec}")
        out.append({
            "item_id": item_id, "source_id": "plugin-fnos-nas", "title": title,
            "episode_code": str(item.get("episode_code") or item.get("episode") or ""),
            "dialogue": str(item.get("dialogue") or item.get("text") or ""),
            "text": str(item.get("text") or item.get("dialogue") or ""),
            "en_text": str(item.get("en_text") or ""), "zh_text": str(item.get("zh_text") or ""),
            "start_sec": start_sec, "end_sec": end_sec, "timestamp_label": subtitles.format_timestamp(start_sec),
            "subtitle_path": str(item.get("subtitle_path") or ""), "video_path": str(item.get("video_path") or ""),
            "play_url": play_url, "has_video": bool(play_url), "variant_match": bool(item.get("variant_match", False)),
            "match_source": "remote_media_service",
            "open_payload": {
                "item_id": item_id, "source_id": "plugin-fnos-nas", "title": title, "kind": "nas",
                "play_url": play_url, "start_sec": start_sec, "end_sec": end_sec,
            },
        })
    return out


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `FnosNasPlugin` instance."""
    return FnosNasPlugin()
