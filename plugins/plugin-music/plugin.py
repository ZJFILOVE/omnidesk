"""OmniDesk Stage 2 Apple Music plugin (`plugin-music`, hybrid, domain=media)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import (
    BasePlugin,
    CapabilityResult,
    DockWidgetSpec,
    PluginContext,
    WorkspaceTabSpec,
)

if __package__:
    from .apple_client import AppleMusicClientProtocol, AppleMusicCredentials
    from .charts import ChartFetchError, fetch_apple_music_chart
    from .lyrics_translate import translate_lyric_lines
    from .playlist_handlers import PlaylistService
    from .qwen_bridge import QwenModules, load_qwen_modules
    from .sources import AppleMusicMediaSource
else:
    _PLUGIN_ROOT = str(Path(__file__).resolve().parent)
    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    from apple_client import AppleMusicClientProtocol, AppleMusicCredentials
    from charts import ChartFetchError, fetch_apple_music_chart
    from lyrics_translate import translate_lyric_lines
    from playlist_handlers import PlaylistService
    from qwen_bridge import QwenModules, load_qwen_modules
    from sources import AppleMusicMediaSource

_MEDIA_SOURCE_ALLOWED: dict[str, frozenset[str]] = {
    "categories": frozenset({"action"}),
    "fetch_items": frozenset({"action", "category", "query", "page", "force_refresh"}),
    "feed": frozenset({"action", "category", "query", "page", "force_refresh"}),
    "fetch_lyrics": frozenset({"action", "item_id", "song_id"}),
    "lyrics": frozenset({"action", "item_id", "song_id"}),
    "status": frozenset({"action"}),
}

_CHARTS_ALLOWED: dict[str, frozenset[str]] = {
    "top_songs": frozenset({"action", "storefront", "limit", "force_refresh"}),
}
_CHARTS_TTL_SEC = 300.0


class MusicPlugin(BasePlugin):
    """Hybrid Apple Music & Daily 5 curation plugin reusing qwen-translator modules."""

    def __init__(
        self,
        *,
        client_factory: Callable[[AppleMusicCredentials, dict[str, Any]], AppleMusicClientProtocol] | None = None,
        qwen_loader: Callable[[str], QwenModules] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._qwen_loader = qwen_loader or load_qwen_modules
        self._ctx: PluginContext | None = None
        self._qwen: QwenModules | None = None
        self._playlist_service: PlaylistService | None = None
        self._media_source: AppleMusicMediaSource | None = None
        self._last_event: dict[str, Any] = {}
        self._charts_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}

    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._qwen = self._qwen_loader(str(ctx.get_config("qwen_translator_dir") or ""))
        self._playlist_service = PlaylistService(
            lambda: self._ctx,
            client_factory=self._client_factory,
            qwen_loader=self._qwen_loader,
        )

        async def _translate_lines(texts: Any, title: str) -> tuple[list[str] | None, str]:
            return await translate_lyric_lines(ctx, texts, title=title)

        async def _fetch_metadata(song_ids: list[str]) -> list[dict[str, Any]]:
            client, _ = self._playlist_service._build_client(ctx, require_write=False)
            response = await client.preflight_cn_catalog(song_ids)
            tracks = response.get("tracks", [])
            self._playlist_service._cache_verified_tracks(ctx, tracks)
            return tracks

        self._media_source = AppleMusicMediaSource(
            get_config=lambda: self._ctx.get_config() if self._ctx is not None else {},
            get_data_dir=lambda: self._ctx.data_dir if self._ctx is not None else Path("/tmp"),
            get_qwen=lambda: self._qwen or self._qwen_loader(
                str(self._ctx.get_config("qwen_translator_dir") or "") if self._ctx else ""
            ),
            translate_lines=_translate_lines,
            fetch_metadata=_fetch_metadata,
        )

        ctx.register_capability("media.playlist.v1", self._playlist_service.handle)
        ctx.register_capability("media.source.v1", self._handle_media_source)
        ctx.register_capability("media.charts.v1", self._handle_charts)
        ctx.register_media_source(self._media_source)

        ctx.register_workspace_tab(
            WorkspaceTabSpec(
                tab_id="music.daily5",
                title="Apple Music · Daily 5",
                domain="media",
                badge="CN",
                ui_schema={"kind": "music_daily5_view", "storefront": "cn"},
                render=self._render_daily_tab,
                on_action=self._handle_ui_action,
            )
        )
        ctx.register_dock_widget(
            DockWidgetSpec(
                widget_id="music.now_playing",
                title="歌词 & 播放队列",
                supported_domains=["media", "core"],
                ui_schema={"kind": "lyrics_dock", "bilingual": True},
                render=self._render_lyrics_dock,
                on_action=self._handle_ui_action,
            )
        )

        ctx.subscribe("entity.music.curation_published", self._on_bus_event)
        ctx.subscribe("entity.media.open", self._on_bus_event)

    async def on_unload(self) -> None:
        self._ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        if self._playlist_service is None or self._ctx is None:
            return {"status": "offline", "degraded": True, "reason": "not_loaded"}
        res = await self._playlist_service.handle({"action": "status"})
        if not res.ok:
            return {"status": "degraded", "degraded": True, "error": res.error}
        data = res.data or {}
        qwen = self._qwen.status_dict() if self._qwen is not None else {"available": False}
        return {
            "status": "degraded" if data.get("degraded") else "healthy",
            "degraded": bool(data.get("degraded")),
            "qwen_translator": qwen,
            "archive_dates_count": data.get("catalog", {}).get("archive_dates_count", 0),
        }

    async def _on_bus_event(self, payload: Any) -> None:
        if isinstance(payload, Mapping):
            self._last_event = dict(payload)

    async def _handle_media_source(self, req: Any) -> CapabilityResult:
        if not isinstance(req, Mapping):
            return CapabilityResult.fail("media.source.v1 请求必须是 JSON 对象", error_code="invalid_argument")
        if self._media_source is None or self._playlist_service is None:
            return CapabilityResult.fail("plugin-music 尚未完成初始化", error_code="service_offline")

        payload = dict(req)
        action = str(payload.get("action") or "fetch_items").strip()
        if action not in _MEDIA_SOURCE_ALLOWED:
            return CapabilityResult.fail(f"不支持的 media.source.v1 动作: {action!r}", error_code="invalid_argument")
        unknown = sorted(set(payload) - _MEDIA_SOURCE_ALLOWED[action])
        if unknown:
            return CapabilityResult.fail(f"未知参数不被允许: {', '.join(unknown)}", error_code="invalid_argument")

        if action == "categories":
            return CapabilityResult.ok({"categories": self._media_source.categories()})

        if action == "status":
            items = await self._media_source.fetch_items(category="每日 5 首·全量", page=1)
            return CapabilityResult.ok({
                "source_id": self._media_source.source_id,
                "categories": self._media_source.categories(),
                "item_count": len(items),
                "qwen_translator": self._qwen.status_dict() if self._qwen else {"available": False},
            })

        if action in {"fetch_items", "feed"}:
            category = payload.get("category", "")
            query = payload.get("query", "")
            page = payload.get("page", 1)
            force_refresh = payload.get("force_refresh", False)
            if not isinstance(category, str) or not isinstance(query, str):
                return CapabilityResult.fail("category 与 query 必须为字符串", error_code="invalid_argument")
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                return CapabilityResult.fail("page 必须是 >= 1 的整数", error_code="invalid_argument")
            if not isinstance(force_refresh, bool):
                return CapabilityResult.fail("force_refresh 必须为布尔值", error_code="invalid_argument")
            items = await self._media_source.fetch_items(
                category=category, query=query, page=page, force_refresh=force_refresh
            )
            return CapabilityResult.ok({
                "source_id": self._media_source.source_id,
                "category": category or "每日 5 首·最新",
                "page": page,
                "count": len(items),
                "items": [item.model_dump(mode="json") for item in items],
            })

        raw_item = payload.get("item_id") if "item_id" in payload else payload.get("song_id")
        if not isinstance(raw_item, str) or not raw_item.strip():
            return CapabilityResult.fail("fetch_lyrics 必须提供非空 item_id 或 song_id", error_code="invalid_argument")
        lines = await self._media_source.fetch_lyrics(raw_item.strip())
        return CapabilityResult.ok({
            "item_id": raw_item.strip(),
            "count": len(lines),
            "reason": "" if lines else "no_lyrics_available",
            "lines": [line.model_dump(mode="json") for line in lines],
        })

    async def _handle_charts(self, req: Any) -> CapabilityResult:
        if not isinstance(req, Mapping):
            return CapabilityResult.fail("media.charts.v1 请求必须是 JSON 对象", error_code="invalid_argument")
        if self._ctx is None:
            return CapabilityResult.fail("plugin-music 尚未完成初始化", error_code="service_offline")
        payload = dict(req)
        action = str(payload.get("action") or "top_songs").strip()
        if action not in _CHARTS_ALLOWED:
            return CapabilityResult.fail(f"不支持的 media.charts.v1 动作: {action!r}", error_code="invalid_argument")
        unknown = sorted(set(payload) - _CHARTS_ALLOWED[action])
        if unknown:
            return CapabilityResult.fail(f"未知参数不被允许: {', '.join(unknown)}", error_code="invalid_argument")

        storefront = str(payload.get("storefront") or "cn").strip().lower()
        raw_limit = payload.get("limit", 10)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= 25:
            return CapabilityResult.fail("limit 必须是 1..25 的整数", error_code="invalid_argument")
        force_refresh = payload.get("force_refresh", False)
        if not isinstance(force_refresh, bool):
            return CapabilityResult.fail("force_refresh 必须为布尔值", error_code="invalid_argument")

        key = (storefront, raw_limit)
        cached = self._charts_cache.get(key)
        if cached is not None and not force_refresh and time.monotonic() - cached[0] <= _CHARTS_TTL_SEC:
            return CapabilityResult.ok({
                "storefront": storefront,
                "chart": "most-played",
                "count": len(cached[1]),
                "cached": True,
                "items": [dict(item) for item in cached[1]],
            })

        cfg = self._ctx.get_config()
        try:
            items = await fetch_apple_music_chart(
                storefront=storefront,
                limit=raw_limit,
                proxy=str(cfg.get("proxy") or ""),
            )
        except ChartFetchError as exc:
            return CapabilityResult.fail(exc.message, error_code=exc.error_code, data={"storefront": storefront})
        if not items:
            return CapabilityResult.fail("Apple Music 榜单未返回有效曲目", error_code="service_offline")
        self._charts_cache[key] = (time.monotonic(), items)
        return CapabilityResult.ok({
            "storefront": storefront,
            "chart": "most-played",
            "count": len(items),
            "cached": False,
            "items": [dict(item) for item in items],
        })

    async def _render_daily_tab(self) -> dict[str, Any]:
        if self._media_source is None or self._playlist_service is None:
            return {"status": "offline", "items": []}
        items = await self._media_source.fetch_items(category="每日 5 首·最新", page=1)
        status = await self._playlist_service.handle({"action": "status"})
        return {
            "source_id": self._media_source.source_id,
            "categories": self._media_source.categories(),
            "latest_items": [item.model_dump(mode="json") for item in items],
            "health": status.data or {},
        }

    async def _render_lyrics_dock(self) -> dict[str, Any]:
        song_id = str(self._last_event.get("item_id") or self._last_event.get("song_id") or "")
        lines = await self._media_source.fetch_lyrics(song_id) if (self._media_source and song_id) else []
        return {
            "active_item_id": song_id,
            "lines": [line.model_dump(mode="json") for line in lines],
            "adam_available": bool(self._qwen and self._qwen.available),
        }

    async def _handle_ui_action(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        act = str(payload.get("action") or "status")
        if act in {"status", "list_playlists"} and self._playlist_service is not None:
            res = await self._playlist_service.handle({"action": act})
            return {"ok": res.ok, "data": res.data, "error": res.error, "error_code": res.data.get("error_code", "")}
        if act == "refresh":
            return await self._render_daily_tab()
        return {"ok": False, "error": f"unsupported UI action: {act}", "error_code": "invalid_argument"}


def create_plugin() -> MusicPlugin:
    """Entry point declared in plugin.json (`plugin:create_plugin`)."""
    return MusicPlugin()
