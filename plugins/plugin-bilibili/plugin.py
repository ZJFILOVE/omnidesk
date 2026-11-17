"""OmniDesk v2 plugin-bilibili: Bilibili MediaSource, stream resolver, QR auth, and danmaku/subtitles."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, WorkspaceTabSpec

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from bilibili_impl.auth import (  # noqa: E402
    action_logout,
    action_qr_login_poll,
    action_qr_login_start,
    action_status,
    resolve_cookie,
)
from bilibili_impl.client import BilibiliApiError, BilibiliClient  # noqa: E402
from bilibili_impl.extras import fetch_chapters_impl, fetch_danmaku_action, fetch_subtitles_action  # noqa: E402
from bilibili_impl.parsers import normalize_quality, parse_bilibili_target  # noqa: E402
from bilibili_impl.security import (  # noqa: E402
    DEFAULT_API_BASE,
    SecurityValidationError,
    redact_bilibili_secrets,
    validate_api_base,
    validate_proxy_url,
    validate_ytdlp_bin,
)
from bilibili_impl.streams import (  # noqa: E402
    default_stream_headers,
    resolve_via_playurl,
    resolve_via_ytdlp,
    resolve_ytdlp_mode,
)
from sources import BilibiliMediaSource  # noqa: E402

_SUPPORTED_ACTIONS = (
    "status",
    "qr_login_start",
    "qr_login_poll",
    "logout",
    "resolve_stream",
    "open_media",
    "fetch_danmaku",
    "fetch_subtitles",
    "fetch_chapters",
    "categories",
    "fetch_items",
)


class BilibiliPlugin(BasePlugin):
    """Workspace media plugin for Bilibili (`media.source.v1`)."""

    def __init__(self) -> None:
        self._media_source: BilibiliMediaSource | None = None
        self._qr_sessions: dict[str, float] = {}

    async def on_load(self, ctx: PluginContext) -> None:
        """Non-blocking registration of MediaSource, action capability, and workspace tab."""
        await super().on_load(ctx)
        self._media_source = BilibiliMediaSource(self)
        ctx.register_media_source(self._media_source)
        ctx.register_capability("bilibili.media.v1", self.handle_capability)
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        """Clear transient QR sessions and references."""
        self._qr_sessions.clear()
        self._media_source = None

    async def on_health_check(self) -> dict[str, Any]:
        """Lightweight offline readiness report without calling external network services."""
        cfg = self._cfg()
        has_vault_cookie = bool(self.ctx and self.ctx.get_secret("cookie_header"))
        api_base = str(cfg.get("api_base") or DEFAULT_API_BASE)
        api_valid = True
        try:
            validate_api_base(api_base)
        except SecurityValidationError:
            api_valid = False
        return {
            "ok": api_valid,
            "plugin_id": self.ctx.plugin_id if self.ctx else "plugin-bilibili",
            "api_base_valid": api_valid,
            "cookie_configured": has_vault_cookie,
            "optional_capabilities": {
                cap: bool(self.ctx and self.ctx.capabilities.has(cap))
                for cap in ("web.cookies.v1", "speech.asr.v1", "llm.translate.v1", "player.timeline.v1")
            },
        }

    def _cfg(self) -> dict[str, Any]:
        return self.ctx.get_config() if self.ctx is not None else {}

    def _default_quality_label(self) -> str:
        return str(self._cfg().get("quality") or "1080p").strip() or "1080p"

    def _build_client(self) -> BilibiliClient:
        cfg = self._cfg()
        return BilibiliClient(
            api_base=str(cfg.get("api_base") or DEFAULT_API_BASE),
            proxy=str(cfg.get("proxy") or ""),
            timeout_sec=float(cfg.get("request_timeout_sec") or 10),
            max_retries=int(cfg.get("max_retries") if cfg.get("max_retries") is not None else 2),
        )

    async def resolve_cookie(self) -> tuple[str, str]:
        return await resolve_cookie(self)

    async def fetch_chapters_for_item(self, item_id: str) -> list[Any]:
        return await fetch_chapters_impl(self, item_id)

    async def action_fetch_subtitles(self, req: Mapping[str, Any]) -> CapabilityResult:
        return await fetch_subtitles_action(self, req)

    async def handle_capability(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Dispatch `bilibili.media.v1` actions with fail-closed error handling."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        try:
            if action == "status":
                return await action_status(self)
            if action == "qr_login_start":
                return await action_qr_login_start(self)
            if action == "qr_login_poll":
                return await action_qr_login_poll(self, payload)
            if action == "logout":
                return await action_logout(self)
            if action == "resolve_stream":
                return await self.action_resolve_stream(payload)
            if action == "open_media":
                return await self.action_open_media(payload)
            if action == "fetch_danmaku":
                return await fetch_danmaku_action(self, payload)
            if action == "fetch_subtitles":
                return await fetch_subtitles_action(self, payload)
            if action == "fetch_chapters":
                item_id = str(payload.get("item_id") or payload.get("bvid") or payload.get("url") or "")
                chapters = [c.model_dump() for c in await fetch_chapters_impl(self, item_id)]
                return CapabilityResult.ok({"count": len(chapters), "chapters": chapters})
            if action == "categories":
                cats = self._media_source.categories() if self._media_source else []
                return CapabilityResult.ok({"categories": cats})
            if action == "fetch_items":
                if self._media_source is None:
                    return CapabilityResult.fail("MediaSource 尚未加载", error_code="service_offline")
                items = await self._media_source.fetch_items(
                    category=str(payload.get("category") or ""),
                    query=str(payload.get("query") or ""),
                    page=int(payload.get("page") or 1),
                )
                return CapabilityResult.ok({"count": len(items), "items": [i.model_dump() for i in items]})
            return CapabilityResult.fail(
                f"不支持的动作: {action!r}",
                data={"supported_actions": list(_SUPPORTED_ACTIONS)},
                error_code="invalid_argument",
            )
        except SecurityValidationError as exc:
            return CapabilityResult.fail(redact_bilibili_secrets(str(exc)), error_code=exc.error_code)
        except BilibiliApiError as exc:
            return CapabilityResult.fail(exc.message, error_code=exc.error_code)

    async def action_resolve_stream(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Resolve real Bilibili streams via yt-dlp or playurl API without faking unavailable quality."""
        assert self.ctx is not None
        client = self._build_client()
        cfg = self._cfg()
        target = parse_bilibili_target(req, allowed_loopback_origins=client.allowed_loopback_origins)
        req_label, req_qn = normalize_quality(req.get("quality") or cfg.get("quality") or "1080p")
        proxy = validate_proxy_url(str(req.get("proxy") if "proxy" in req else cfg.get("proxy") or ""))
        ytdlp_bin = validate_ytdlp_bin(str(req.get("ytdlp_bin") if "ytdlp_bin" in req else cfg.get("ytdlp_bin") or ""))
        method = str(req.get("method") or "auto").strip().lower()
        cookie, cookie_src = await self.resolve_cookie()

        mode, bin_path = resolve_ytdlp_mode(client, method=method, ytdlp_bin=ytdlp_bin)
        if mode == "ytdlp":
            resolved = await resolve_via_ytdlp(
                self, bin_path, target["url"], cookie=cookie, proxy=proxy,
                timeout_sec=client.timeout_sec, req_label=req_label, req_qn=req_qn,
                allowed_origins=client.allowed_loopback_origins,
            )
        else:
            resolved = await resolve_via_playurl(
                client, target, cookie=cookie, req_label=req_label, req_qn=req_qn
            )

        headers = default_stream_headers(target.get("bvid", ""), target.get("aid"))
        item_id = target.get("bvid") or (f"av{target['aid']}" if target.get("aid") else target["url"])
        out = {
            **resolved,
            "bvid": target.get("bvid", ""),
            "aid": target.get("aid"),
            "item_id": item_id,
            "requested_quality": req_label,
            "cookie_source": cookie_src,
            "headers": headers,
        }
        if bool(req.get("emit_open", True)) and out.get("play_url"):
            await self.ctx.emit(
                "entity.media.open",
                {
                    "item_id": item_id,
                    "source_id": "plugin-bilibili",
                    "title": str(out.get("title") or item_id),
                    "kind": "live" if target.get("room_id") else "video",
                    "play_url": out["play_url"],
                    "start_sec": float(req.get("start_sec") or 0.0),
                },
            )
        return CapabilityResult.ok(out)

    async def action_open_media(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Resolve stream and optional SponsorBlock chapters, then publish `entity.media.open`."""
        assert self.ctx is not None
        stream_res = await self.action_resolve_stream({**req, "emit_open": False})
        if not stream_res.ok:
            return stream_res
        item_id = str(stream_res.data.get("item_id") or "")
        chapters = await fetch_chapters_impl(self, item_id) if item_id else []
        payload = {
            "item_id": item_id,
            "source_id": "plugin-bilibili",
            "title": str(req.get("title") or stream_res.data.get("title") or item_id),
            "kind": str(req.get("kind") or "video"),
            "play_url": str(stream_res.data.get("play_url") or ""),
            "start_sec": float(req.get("start_sec") or 0.0),
        }
        await self.ctx.emit("entity.media.open", payload)
        return CapabilityResult.ok({**stream_res.data, "chapters": [c.model_dump() for c in chapters], "opened": True})

    def _build_ui_schema(self) -> dict[str, Any]:
        has_cookie = bool(self.ctx and self.ctx.get_secret("cookie_header"))
        return {
            "type": "Section",
            "id": "bilibili-workspace-root",
            "title": "Bilibili Media Workspace",
            "description": "推荐 / 动态 / 追番 / 直播 / 搜索、扫码鉴权与 4K/弹幕/双语字幕解析",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "bilibili-status",
                    "items": [
                        {"key": "cookie_configured", "label": "Cookie 状态", "value": "configured" if has_cookie else "not_configured"},
                        {"key": "quality", "label": "默认清晰度", "value": self._default_quality_label()},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "bilibili-actions",
                    "actions": [
                        {"id": "status", "label": "检查登录态", "variant": "secondary"},
                        {"id": "qr_login_start", "label": "生成登录二维码", "variant": "primary"},
                        {"id": "logout", "label": "退出登录", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="bilibili",
            title="Bilibili",
            domain="media",
            badge="BILI",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Forward declarative workspace tab actions to the unified capability dispatcher."""
        req = dict(state) if state is not None else {}
        req["action"] = action_id
        res = await self.handle_capability(req)
        return {"ok": res.ok, "data": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `BilibiliPlugin` instance."""
    return BilibiliPlugin()
