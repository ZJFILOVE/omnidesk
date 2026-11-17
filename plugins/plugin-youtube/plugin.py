"""OmniDesk v2 plugin-youtube: MediaSource, yt-dlp stream resolver, subtitles, and subscriptions."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, LyricLine, PluginContext, WorkspaceTabSpec

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if not __package__ and _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

if __package__:
    from .sources import YouTubeMediaSource, split_csv
    from .yt_api import YOUTUBE_API_BASE_URL, YouTubeApiClient
    from .yt_dlp import fetch_video_info, select_stream_formats
    from .yt_http import atomic_write_json
    from .yt_security import (
        YouTubePathSafetyError, default_cache_root, extract_video_id, redact_sensitive,
        resolve_executable, resolve_safe_cache_dir, safe_cache_file, url_origin, validate_channel_id, validate_playlist_id,
    )
    from .yt_subtitles import cues_to_lyric_lines, extract_subtitle_tracks
    from .yt_translate import fetch_subtitles_flow
else:
    from sources import YouTubeMediaSource, split_csv
    from yt_api import YOUTUBE_API_BASE_URL, YouTubeApiClient
    from yt_dlp import fetch_video_info, select_stream_formats
    from yt_http import atomic_write_json
    from yt_security import (
        YouTubePathSafetyError, default_cache_root, extract_video_id, redact_sensitive,
        resolve_executable, resolve_safe_cache_dir, safe_cache_file, url_origin, validate_channel_id, validate_playlist_id,
    )
    from yt_subtitles import cues_to_lyric_lines, extract_subtitle_tracks
    from yt_translate import fetch_subtitles_flow

_SUPPORTED_ACTIONS = (
    "status",
    "categories",
    "fetch_items",
    "search",
    "resolve_stream",
    "list_subtitles",
    "fetch_subtitles",
    "list_subscriptions",
    "sync_subscriptions",
    "fetch_chapters",
)


class YouTubePlugin(BasePlugin):
    """Workspace MediaSource plugin for YouTube feeds, stream resolution, and bilingual subtitles."""

    def __init__(
        self,
        *,
        api_base_url: str = YOUTUBE_API_BASE_URL,
        allow_loopback_http: bool = False,
    ) -> None:
        self.allow_loopback_http = allow_loopback_http
        self.api_client = YouTubeApiClient(api_base_url=api_base_url, allow_loopback_http=allow_loopback_http)
        self.media_source = YouTubeMediaSource(self)
        self._last_action_status = "ready"

    async def on_load(self, ctx: PluginContext) -> None:
        """Register MediaSource, capability handler, and declarative workspace tab (< 50ms)."""
        await super().on_load(ctx)
        ctx.register_media_source(self.media_source)
        ctx.register_capability("media.youtube.v1", self.handle_action)
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        """Clear transient in-memory caches on unload."""
        self.media_source.clear_caches()
        self._last_action_status = "unloaded"

    async def on_health_check(self) -> dict[str, Any]:
        """Return non-blocking readiness diagnostics without leaking secrets."""
        ytdlp = self.ytdlp_bin()
        resolved, err_code = resolve_executable(ytdlp)
        cache_ok = True
        try:
            resolve_safe_cache_dir(self.cache_dir_config(), default_cache_root(self.ctx))
        except YouTubePathSafetyError:
            cache_ok = False
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-youtube"
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "source_id": self.media_source.source_id,
            "ytdlp_configured": bool(ytdlp) and err_code != "path_safety",
            "ytdlp_found": bool(resolved),
            "api_key_configured": bool(self.api_key()),
            "oauth_configured": bool(self.oauth_token()),
            "cache_dir_safe": cache_ok,
        }

    def api_key(self) -> str:
        return "" if self.ctx is None else str(self.ctx.get_secret("api_key", "") or "").strip()

    def oauth_token(self) -> str:
        return "" if self.ctx is None else str(self.ctx.get_secret("oauth_token", "") or "").strip()

    def secret_values(self) -> tuple[str, ...]:
        return tuple(s for s in (self.api_key(), self.oauth_token()) if s)

    def ytdlp_bin(self) -> str:
        return "yt-dlp" if self.ctx is None else str(self.ctx.get_config("ytdlp_bin", "yt-dlp") or "").strip()

    def region(self) -> str:
        return "US" if self.ctx is None else (str(self.ctx.get_config("region", "US") or "US").strip().upper() or "US")

    def quality(self) -> str:
        return "1080p" if self.ctx is None else (str(self.ctx.get_config("quality", "1080p") or "1080p").strip() or "1080p")

    def target_lang(self) -> str:
        return "zh-CN" if self.ctx is None else (str(self.ctx.get_config("target_lang", "zh-CN") or "zh-CN").strip() or "zh-CN")

    def cache_dir_config(self) -> str:
        return "" if self.ctx is None else str(self.ctx.get_config("cache_dir", "") or "").strip()

    def timeout_sec(self) -> float:
        if self.ctx is None:
            return 15.0
        try:
            return max(0.2, float(self.ctx.get_config("request_timeout_sec", 15)))
        except (TypeError, ValueError):
            return 15.0

    def configured_channel_ids(self) -> list[str]:
        return [] if self.ctx is None else split_csv(self.ctx.get_config("channel_ids", ""))

    def configured_playlist_ids(self) -> list[str]:
        return [] if self.ctx is None else split_csv(self.ctx.get_config("playlist_ids", ""))

    def allowed_loopback_origins(self) -> set[str]:
        return {url_origin(self.api_client.api_base_url)} if self.api_client.api_base_url.startswith("http://") else set()

    def _fail(self, error: str, *, error_code: str, data: Mapping[str, Any] | None = None) -> CapabilityResult:
        safe_err = redact_sensitive(error, self.secret_values())
        self._last_action_status = f"error:{error_code}"
        return CapabilityResult.fail(safe_err, data=data, error_code=error_code)

    def _fail_from_dict(self, err: Mapping[str, Any] | None, fallback_msg: str, fallback_code: str = "service_offline") -> CapabilityResult:
        err_copy = dict(err or {})
        code = str(err_copy.pop("error_code", fallback_code))
        msg = str(err_copy.pop("error", fallback_msg))
        return self._fail(msg, error_code=code, data=err_copy)

    def _extract_target_video_id(self, req: Mapping[str, Any]) -> tuple[str | None, CapabilityResult | None]:
        raw = str(req.get("video_id") or req.get("item_id") or req.get("url") or "").strip()
        if not raw:
            return None, self._fail("缺少必填参数 video_id 或 https url", error_code="invalid_argument")
        try:
            vid = extract_video_id(
                raw, allow_loopback_http=self.allow_loopback_http, allowed_loopback_origins=self.allowed_loopback_origins()
            )
            return vid, None
        except ValueError as exc:
            return None, self._fail(str(exc), error_code="invalid_argument")

    async def handle_action(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Dispatch `media.youtube.v1` RPC actions with fail-closed error handling."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "status").strip()
        if action == "status":
            return CapabilityResult.ok(await self.on_health_check())
        if action == "categories":
            return CapabilityResult.ok({"categories": self.media_source.categories()})
        if action in {"fetch_items", "search"}:
            try:
                page = int(payload.get("page") or 1)
            except (TypeError, ValueError):
                return self._fail("page 参数必须是整数", error_code="invalid_argument")
            cat = "search" if action == "search" else str(payload.get("category") or "")
            items = await self.media_source.fetch_items(
                category=cat,
                query=str(payload.get("query") or ""),
                page=page,
                force_refresh=bool(payload.get("force_refresh", False)),
            )
            if not items and self.media_source.last_error:
                err = dict(self.media_source.last_error)
                return self._fail(str(err.pop("error", "获取 YouTube 列表失败")), error_code=str(err.pop("error_code", "service_offline")), data={**err, "items": []})
            return CapabilityResult.ok({"items": [m.model_dump() for m in items], "count": len(items)})
        dispatch = {
            "resolve_stream": self.resolve_stream,
            "list_subtitles": self.list_subtitles,
            "fetch_subtitles": self.fetch_subtitles,
            "list_subscriptions": self.list_subscriptions,
            "sync_subscriptions": self.sync_subscriptions,
            "fetch_chapters": self.fetch_chapters,
        }
        handler = dispatch.get(action)
        if handler is not None:
            return await handler(payload)
        return self._fail(f"不支持的动作: {action!r}", error_code="invalid_argument", data={"supported_actions": list(_SUPPORTED_ACTIONS)})

    async def resolve_stream(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Resolve playable video/audio stream URLs and sanitized headers via yt-dlp -J."""
        if self.ctx is None:
            return self._fail("插件尚未加载", error_code="not_configured")
        vid, bad = self._extract_target_video_id(req)
        if bad is not None or vid is None:
            return bad or self._fail("无效的视频标识", error_code="invalid_argument")

        info, err = await fetch_video_info(
            self.ctx, self.ytdlp_bin(), vid, timeout_sec=self.timeout_sec(), extra_secrets=self.secret_values(),
            allow_loopback_http=self.allow_loopback_http, allowed_loopback_origins=self.allowed_loopback_origins(),
        )
        if err is not None or info is None:
            return self._fail_from_dict(err, "yt-dlp 解析失败")

        quality = str(req.get("quality") or self.quality()).strip() or "1080p"
        selected, sel_err = select_stream_formats(
            info, quality=quality, allow_loopback_http=self.allow_loopback_http, allowed_loopback_origins=self.allowed_loopback_origins()
        )
        if sel_err is not None or selected is None:
            return self._fail_from_dict(sel_err, "未找到可用的媒体流", fallback_code="not_found")
        self._last_action_status = f"resolved:{vid}:{selected['quality']}"
        return CapabilityResult.ok(selected)

    async def _load_video_info_for_subs(self, req: Mapping[str, Any], vid: str) -> tuple[dict[str, Any] | None, CapabilityResult | None]:
        raw_info = req.get("info")
        if isinstance(raw_info, Mapping):
            info = dict(raw_info)
            info.setdefault("id", vid)
            return info, None
        assert self.ctx is not None
        info, err = await fetch_video_info(
            self.ctx, self.ytdlp_bin(), vid, timeout_sec=self.timeout_sec(), extra_secrets=self.secret_values(),
            allow_loopback_http=self.allow_loopback_http, allowed_loopback_origins=self.allowed_loopback_origins(),
        )
        if err is not None or info is None:
            return None, self._fail_from_dict(err, "获取视频字幕列表失败")
        return info, None

    async def list_subtitles(self, req: Mapping[str, Any]) -> CapabilityResult:
        """List official and automatic subtitle tracks for a YouTube video."""
        if self.ctx is None:
            return self._fail("插件尚未加载", error_code="not_configured")
        vid, bad = self._extract_target_video_id(req)
        if bad is not None or vid is None:
            return bad or self._fail("无效的视频标识", error_code="invalid_argument")
        info, err_res = await self._load_video_info_for_subs(req, vid)
        if err_res is not None or info is None:
            return err_res or self._fail("无法读取视频信息", error_code="service_offline")
        tracks = extract_subtitle_tracks(
            info, allow_loopback_http=self.allow_loopback_http, allowed_loopback_origins=self.allowed_loopback_origins()
        )
        return CapabilityResult.ok(
            {"video_id": vid, "tracks": tracks, "count": len(tracks), "has_official": any(not t["is_auto"] for t in tracks), "has_automatic": any(t["is_auto"] for t in tracks)}
        )

    async def fetch_subtitles(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Fetch official/auto VTT/SRT subtitles, optionally pre-translate via llm.translate.v1 and cache."""
        if self.ctx is None:
            return self._fail("插件尚未加载", error_code="not_configured")
        vid, bad = self._extract_target_video_id(req)
        if bad is not None or vid is None:
            return bad or self._fail("无效的视频标识", error_code="invalid_argument")
        return await fetch_subtitles_flow(self, req, vid)

    async def fetch_lyrics_for_item(self, item_id: str) -> list[LyricLine]:
        """Bridge fetch_subtitles into MediaSource.fetch_lyrics."""
        res = await self.fetch_subtitles({"video_id": item_id})
        return cues_to_lyric_lines(res.data.get("cues") or []) if res.ok else []

    def _collect_subscription_inputs(self, req: Mapping[str, Any]) -> tuple[str, list[str], list[str], CapabilityResult | None]:
        oauth = self.oauth_token()
        raw_channels = split_csv(req.get("channel_ids")) if "channel_ids" in req else self.configured_channel_ids()
        raw_playlists = split_csv(req.get("playlist_ids")) if "playlist_ids" in req else self.configured_playlist_ids()
        try:
            channels = [validate_channel_id(c) for c in raw_channels]
            playlists = [validate_playlist_id(p) for p in raw_playlists]
        except ValueError as exc:
            return "", [], [], self._fail(str(exc), error_code="invalid_argument")
        if not oauth and not channels and not playlists:
            return "", [], [], self._fail(
                "尚未配置 YouTube OAuth Token 或频道/播放列表 ID",
                error_code="not_configured",
                data={"subscriptions": [], "channel_ids": [], "playlist_ids": []},
            )
        return oauth, channels, playlists, None

    async def list_subscriptions(self, req: Mapping[str, Any]) -> CapabilityResult:
        """List OAuth subscriptions and/or user-configured channels & playlists."""
        oauth, channels, playlists, bad = self._collect_subscription_inputs(req)
        if bad is not None:
            return bad
        subs: list[dict[str, Any]] = []
        if oauth:
            fetched, err = await self.api_client.fetch_subscriptions(oauth_token=oauth, api_key=self.api_key(), timeout_sec=self.timeout_sec())
            if err is not None:
                return self._fail_from_dict(err, "读取 YouTube 订阅失败")
            subs.extend(fetched)
        seen_ch = {s["channel_id"] for s in subs}
        for cid in channels:
            if cid not in seen_ch:
                subs.append({"channel_id": cid, "title": cid, "description": "", "source": "config"})
                seen_ch.add(cid)
        return CapabilityResult.ok({"subscriptions": subs, "channel_ids": list(seen_ch), "playlist_ids": playlists, "count": len(subs) + len(playlists)})

    async def sync_subscriptions(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Sync recent videos across configured OAuth subscriptions, channels, and playlists."""
        if self.ctx is None:
            return self._fail("插件尚未加载", error_code="not_configured")
        list_res = await self.list_subscriptions(req)
        if not list_res.ok:
            return list_res
        try:
            cache_root = resolve_safe_cache_dir(self.cache_dir_config(), default_cache_root(self.ctx))
            snapshot_path = safe_cache_file(cache_root, "subscriptions.json")
        except YouTubePathSafetyError as exc:
            return self._fail(str(exc), error_code="path_safety")

        channels, playlists = list(list_res.data.get("channel_ids") or []), list(list_res.data.get("playlist_ids") or [])
        items = []
        for cid in channels:
            items.extend(await self.media_source.fetch_from_target("channel", cid, category="subscriptions", per_page=10))
        for pid in playlists:
            items.extend(await self.media_source.fetch_from_target("playlist", pid, category="subscriptions", per_page=10))
        if not items and self.media_source.last_error:
            return self._fail_from_dict(self.media_source.last_error, "同步 YouTube 订阅失败")

        self.media_source.remember_items(items)
        serialized = [m.model_dump() for m in items]
        atomic_write_json(snapshot_path, {"subscriptions": list_res.data.get("subscriptions", []), "playlist_ids": playlists, "items": serialized})
        await self.ctx.emit("youtube.subscriptions.synced", {"count": len(serialized), "channel_count": len(channels)})
        return CapabilityResult.ok(
            {"subscriptions": list_res.data.get("subscriptions", []), "playlist_ids": playlists, "items": serialized, "count": len(serialized), "snapshot_path": str(snapshot_path)}
        )

    async def fetch_chapters(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Fetch real video chapters merged with optional SponsorBlock timeline segments."""
        vid, bad = self._extract_target_video_id(req)
        if bad is not None or vid is None:
            return bad or self._fail("无效的视频标识", error_code="invalid_argument")
        chapters = await self.media_source.fetch_chapters(vid)
        return CapabilityResult.ok({"video_id": vid, "chapters": [c.model_dump() for c in chapters], "count": len(chapters)})

    def _build_ui_schema(self) -> dict[str, Any]:
        return {
            "type": "Section",
            "id": "youtube-workspace-root",
            "title": "YouTube 媒体与双语字幕",
            "description": "订阅同步、关键词搜索、yt-dlp 流解析与双语字幕缓存（media.source.v1）",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "youtube-status",
                    "items": [
                        {"key": "region", "label": "地区", "value": self.region()},
                        {"key": "quality", "label": "首选画质", "value": self.quality()},
                        {"key": "api_key", "label": "Data API Key", "value": "configured" if self.api_key() else "unset"},
                        {"key": "oauth", "label": "OAuth Token", "value": "configured" if self.oauth_token() else "unset"},
                        {"key": "last_status", "label": "最近状态", "value": self._last_action_status},
                    ],
                },
                {
                    "type": "Form",
                    "id": "youtube-action-form",
                    "fields": [
                        {"name": "query", "label": "搜索关键词 / 频道 / 播放列表", "type": "string", "default": ""},
                        {"name": "video_id", "label": "视频 ID 或 HTTPS 链接", "type": "string", "default": ""},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "youtube-actions",
                    "actions": [
                        {"id": "search", "label": "搜索视频", "variant": "primary"},
                        {"id": "resolve_stream", "label": "解析流地址", "variant": "secondary"},
                        {"id": "fetch_subtitles", "label": "拉取双语字幕", "variant": "secondary"},
                        {"id": "sync_subscriptions", "label": "同步订阅", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="youtube", title="YouTube", domain="media", badge="YT",
            ui_schema=self._build_ui_schema(), render=self._build_ui_schema, on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Route declarative workspace tab actions to `handle_action`."""
        payload = {**(dict(state) if state is not None else {}), "action": action_id}
        res = await self.handle_action(payload)
        return {"ok": res.ok, "result": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `YouTubePlugin` instance."""
    return YouTubePlugin()
