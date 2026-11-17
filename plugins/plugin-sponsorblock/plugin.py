"""OmniDesk v2 plugin: SponsorBlock K-Anon Timeline (`player.timeline.v1`)."""

from __future__ import annotations

import ssl
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, ChapterSegment, PluginContext
from pydantic import ValidationError

try:
    from . import sb_cache, sb_client
except ImportError:
    import sb_cache  # type: ignore[no-redef]
    import sb_client  # type: ignore[no-redef]

_SUPPORTED_ACTIONS = ("status", "fetch", "merge", "next_segment", "clear_cache")


class SponsorBlockPlugin(BasePlugin):
    """Extension plugin providing K-anonymous SponsorBlock timeline integration."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context

    async def on_load(self, ctx: PluginContext) -> None:
        """Register `player.timeline.v1` without blocking I/O."""
        await super().on_load(ctx)
        ctx.register_capability("player.timeline.v1", self.handle_timeline)

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight configuration and cache status."""
        cfg = self._cfg()
        return {
            "ok": True,
            "plugin_id": self.ctx.plugin_id if self.ctx is not None else "plugin-sponsorblock",
            "capabilities": ["player.timeline.v1"],
            "enabled": bool(cfg.get("enabled", True)),
            "youtube_api_base": str(cfg.get("youtube_api_base") or "https://sponsor.ajay.app"),
            "bilibili_api_base": str(cfg.get("bilibili_api_base") or "https://bsbsb.top"),
            "cache_ttl_sec": self._cache_ttl(cfg),
        }

    def _cfg(self) -> dict[str, Any]:
        return {} if self.ctx is None else dict(self.ctx.get_config())

    def _runtime_data_dir(self) -> Path:
        if self.ctx is None:
            return Path(tempfile.gettempdir()) / "omnidesk" / "plugin-sponsorblock"
        if self.ctx.data_dir != self.ctx.plugin_dir:
            return self.ctx.data_dir
        cfg_root = getattr(getattr(self.ctx, "_config_store", None), "_root_dir", None)
        if isinstance(cfg_root, Path) and cfg_root != self.ctx.plugin_dir:
            return cfg_root / "plugin-data" / self.ctx.plugin_id
        return Path(tempfile.gettempdir()) / "omnidesk" / self.ctx.plugin_id

    @staticmethod
    def _cache_ttl(cfg: Mapping[str, Any]) -> int:
        try:
            return max(0, int(cfg.get("cache_ttl_sec", 3600)))
        except (TypeError, ValueError):
            return 3600

    @staticmethod
    def _timeout_sec(req: Mapping[str, Any], cfg: Mapping[str, Any]) -> float:
        raw = req.get("timeout_sec")
        if raw is None and req.get("timeout_ms") is not None:
            raw = float(req["timeout_ms"]) / 1000.0
        if raw is None:
            raw = cfg.get("request_timeout_sec", 10)
        try:
            return max(0.05, min(float(raw), 60.0))
        except (TypeError, ValueError):
            return 10.0

    async def handle_timeline(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `player.timeline.v1` actions (`status`, `fetch`, `merge`, `next_segment`, `clear_cache`)."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        try:
            if action == "status":
                return self._handle_status()
            if action == "clear_cache":
                removed = sb_cache.clear_cache(self._runtime_data_dir())
                return CapabilityResult.ok({"cleared": removed})
            if action in {"fetch", "get_segments", "query"}:
                return await self._handle_fetch(payload)
            if action == "merge":
                return await self._handle_merge(payload)
            if action == "next_segment":
                return await self._handle_next_segment(payload)
        except (sb_client.SponsorBlockError, sb_cache.CacheSafetyError) as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.error_code)
        except ValidationError as exc:
            return CapabilityResult.fail(f"ChapterSegment 格式校验失败: {exc}", error_code="invalid_argument")

        return CapabilityResult.fail(
            f"不支持的 player.timeline.v1 动作: {action!r}",
            data={"supported_actions": list(_SUPPORTED_ACTIONS)},
            error_code="invalid_argument",
        )

    def _handle_status(self) -> CapabilityResult:
        cfg = self._cfg()
        data_dir = self._runtime_data_dir()
        return CapabilityResult.ok(
            {
                "enabled": bool(cfg.get("enabled", True)),
                "youtube_api_base": str(cfg.get("youtube_api_base") or "https://sponsor.ajay.app"),
                "bilibili_api_base": str(cfg.get("bilibili_api_base") or "https://bsbsb.top"),
                "categories": sb_client.normalize_categories(cfg.get("categories")),
                "cache_ttl_sec": self._cache_ttl(cfg),
                "cache_entries": sb_cache.cache_stats(data_dir),
            }
        )

    async def _fetch_segments_for_video(
        self,
        req: Mapping[str, Any],
        cfg: Mapping[str, Any],
    ) -> tuple[str, str, str, list[ChapterSegment], bool]:
        plat_raw = req.get("platform") or req.get("provider")
        vid_raw = req.get("video_id") or req.get("bvid") or req.get("item_id")
        plat, vid = sb_client.validate_platform_and_id(plat_raw, vid_raw)
        categories = sb_client.normalize_categories(req.get("categories") or cfg.get("categories"))
        prefix = sb_client.k_anon_prefix(vid)

        if not bool(cfg.get("enabled", True)):
            return plat, vid, prefix, [], False

        ttl = self._cache_ttl(cfg)
        data_dir = self._runtime_data_dir()
        base_key = "youtube_api_base" if plat == "youtube" else "bilibili_api_base"
        default_base = "https://sponsor.ajay.app" if plat == "youtube" else "https://bsbsb.top"
        api_base = str(cfg.get(base_key) or default_base).strip()
        cache_key = sb_cache.make_cache_key(plat, vid, categories, api_base)
        force_refresh = bool(req.get("force_refresh") or req.get("bypass_cache"))
        if not force_refresh:
            cached = sb_cache.read_cached_segments(data_dir, cache_key, ttl)
            if cached is not None:
                return plat, vid, prefix, sb_client.coerce_segments(cached), True

        _, fetched = await sb_client.fetch_k_anon_segments(
            platform=plat,
            video_id=vid,
            api_base=api_base,
            categories=categories,
            timeout_sec=self._timeout_sec(req, cfg),
            ssl_context=self._ssl_context,
        )
        sb_cache.write_cached_segments(data_dir, cache_key, [seg.model_dump() for seg in fetched], ttl)
        return plat, vid, prefix, fetched, False

    async def _handle_fetch(self, req: Mapping[str, Any]) -> CapabilityResult:
        cfg = self._cfg()
        plat, vid, prefix, sb_segments, from_cache = await self._fetch_segments_for_video(req, cfg)

        ext_raw = req.get("external_chapters") or req.get("chapters") or []
        if not isinstance(ext_raw, Sequence) or isinstance(ext_raw, (str, bytes)):
            raise sb_client.SponsorBlockError("invalid_argument", "external_chapters 必须为列表")
        ext_segments = sb_client.coerce_segments(ext_raw)
        merged = sb_client.merge_and_sort_segments(ext_segments, sb_segments)
        dumped = [seg.model_dump() for seg in merged]

        data: dict[str, Any] = {
            "platform": plat,
            "video_id": vid,
            "hash_prefix": prefix,
            "enabled": bool(cfg.get("enabled", True)),
            "cached": from_cache,
            "segments": dumped,
            "chapters": dumped,
            "count": len(dumped),
        }
        if "current_sec" in req or "position_sec" in req:
            pos = req.get("current_sec", req.get("position_sec", 0.0))
            nxt, skip_to, active_now = sb_client.find_next_skippable_segment(merged, pos)
            data["next_segment"] = nxt.model_dump() if nxt is not None else None
            data["skip_to_sec"] = skip_to
            data["active_now"] = active_now
        return CapabilityResult.ok(data)

    async def _handle_merge(self, req: Mapping[str, Any]) -> CapabilityResult:
        cfg = self._cfg()
        ext_raw = req.get("external_chapters") if "external_chapters" in req else req.get("chapters", [])
        sb_raw = req.get("sponsor_segments") if "sponsor_segments" in req else req.get("segments", [])
        if not isinstance(ext_raw, Sequence) or isinstance(ext_raw, (str, bytes)):
            raise sb_client.SponsorBlockError("invalid_argument", "external_chapters 必须为数组")
        if not isinstance(sb_raw, Sequence) or isinstance(sb_raw, (str, bytes)):
            raise sb_client.SponsorBlockError("invalid_argument", "sponsor_segments 必须为数组")

        ext_segs = sb_client.coerce_segments(ext_raw)
        sb_segs = sb_client.coerce_segments(sb_raw)
        if not sb_segs and req.get("platform") and req.get("video_id"):
            _, _, _, sb_segs, _ = await self._fetch_segments_for_video(req, cfg)

        merged = sb_client.merge_and_sort_segments(ext_segs, sb_segs)
        dumped = [seg.model_dump() for seg in merged]
        data: dict[str, Any] = {"segments": dumped, "chapters": dumped, "count": len(dumped)}
        if "current_sec" in req or "position_sec" in req:
            pos = req.get("current_sec", req.get("position_sec", 0.0))
            nxt, skip_to, active_now = sb_client.find_next_skippable_segment(merged, pos)
            data["next_segment"] = nxt.model_dump() if nxt is not None else None
            data["skip_to_sec"] = skip_to
            data["active_now"] = active_now
        return CapabilityResult.ok(data)

    async def _handle_next_segment(self, req: Mapping[str, Any]) -> CapabilityResult:
        cfg = self._cfg()
        pos = req.get("current_sec", req.get("position_sec", 0.0))
        raw_segs = req.get("segments") if "segments" in req else req.get("chapters")
        if raw_segs is not None:
            if not isinstance(raw_segs, Sequence) or isinstance(raw_segs, (str, bytes)):
                raise sb_client.SponsorBlockError("invalid_argument", "segments/chapters 必须为数组")
            segments = sb_client.merge_and_sort_segments(sb_client.coerce_segments(raw_segs))
        elif req.get("platform") and req.get("video_id"):
            _, _, _, segments, _ = await self._fetch_segments_for_video(req, cfg)
        else:
            raise sb_client.SponsorBlockError(
                "invalid_argument", "next_segment 需要提供 segments/chapters 或 platform + video_id"
            )

        nxt, skip_to, active_now = sb_client.find_next_skippable_segment(segments, pos)
        seg_dict = nxt.model_dump() if nxt is not None else None
        return CapabilityResult.ok(
            {
                "found": seg_dict is not None,
                "segment": seg_dict,
                "next_segment": seg_dict,
                "skip_to_sec": skip_to,
                "active_now": active_now,
                "current_sec": float(pos),
            }
        )


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `SponsorBlockPlugin` instance."""
    return SponsorBlockPlugin()
