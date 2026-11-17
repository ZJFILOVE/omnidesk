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
from bilibili_impl.android_auth import (  # noqa: E402
    import_wiliwili_session,
    poll_android_qr,
    start_android_qr,
)
from bilibili_impl.client import BilibiliApiError, BilibiliClient  # noqa: E402
from bilibili_impl.extras import fetch_chapters_impl, fetch_danmaku_action, fetch_subtitles_action  # noqa: E402
from bilibili_impl.parsers import normalize_quality, parse_bilibili_target  # noqa: E402
from bilibili_impl.security import (  # noqa: E402
    DEFAULT_API_BASE,
    DEFAULT_GRPC_ENDPOINT,
    SecurityValidationError,
    redact_bilibili_secrets,
    validate_api_base,
    validate_curl_bin,
    validate_grpc_endpoint,
    validate_proxy_url,
    validate_ytdlp_bin,
)
from bilibili_impl.streams import (  # noqa: E402
    default_stream_headers,
    resolve_via_playurl,
    resolve_via_ytdlp,
    resolve_ytdlp_mode,
)
from bilibili_impl.workspace_ui import build_ui_schema  # noqa: E402
from bilibili_impl.browse import browse  # noqa: E402
from sources import BilibiliMediaSource  # noqa: E402

_SUPPORTED_ACTIONS = (
    "status",
    "qr_login_start",
    "qr_login_poll",
    "android_qr_login_start",
    "android_qr_login_poll",
    "import_wiliwili_session",
    "set_settings",
    "ui_schema",
    "logout",
    "resolve_stream",
    "open_media",
    "fetch_danmaku",
    "fetch_subtitles",
    "fetch_chapters",
    "categories",
    "fetch_items",
    "followings", "dynamics", "space",
)


class BilibiliPlugin(BasePlugin):
    """Workspace media plugin for Bilibili (`media.source.v1`)."""

    def __init__(self) -> None:
        self._media_source: BilibiliMediaSource | None = None
        self._qr_sessions: dict[str, float] = {}
        self._android_qr_auth_code = ""
        self._last_qr: dict[str, Any] | None = None
        self._tab_status = ""
        self._browse_offsets: dict[Any, Any] = {}

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
        self._android_qr_auth_code = ""
        self._last_qr = None
        self._tab_status = ""
        self._media_source = None
        self._browse_offsets.clear()

    async def on_health_check(self) -> dict[str, Any]:
        """Lightweight offline readiness report without calling external network services."""
        cfg = self._cfg()
        has_vault_cookie = bool(self.ctx and self.ctx.get_secret("cookie_header"))
        has_android_token = bool(self.ctx and self.ctx.get_secret("android_access_token"))
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
            "android_token_configured": has_android_token,
            "unlock_hd": self._unlock_hd(),
            "optional_capabilities": {
                cap: bool(self.ctx and self.ctx.capabilities.has(cap))
                for cap in ("web.cookies.v1", "speech.asr.v1", "llm.translate.v1", "player.timeline.v1")
            },
        }

    def _cfg(self) -> dict[str, Any]:
        return self.ctx.get_config() if self.ctx is not None else {}

    def _default_quality_label(self) -> str:
        return str(self._cfg().get("quality") or "1080p").strip() or "1080p"

    def _unlock_hd(self) -> bool:
        value = self._cfg().get("unlock_hd", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _preferred_codec_id(self) -> int:
        raw = str(self._cfg().get("preferred_codec") or "hevc").strip().lower()
        return {"avc": 7, "hevc": 12, "av1": 13, "7": 7, "12": 12, "13": 13}.get(raw, 12)

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
            if action in {"followings", "dynamics", "space"}:
                return await browse(self, payload)
            if action == "status":
                return await action_status(self)
            if action == "qr_login_start":
                return await action_qr_login_start(self)
            if action == "qr_login_poll":
                return await action_qr_login_poll(self, payload)
            if action == "android_qr_login_start":
                return await start_android_qr(self)
            if action == "android_qr_login_poll":
                return await poll_android_qr(self, payload)
            if action == "import_wiliwili_session":
                return import_wiliwili_session(self, payload)
            if action == "set_settings":
                return self.action_set_settings(payload)
            if action == "ui_schema":
                return CapabilityResult.ok({"ui_schema": build_ui_schema(self)})
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
        if method not in {"auto", "playurl", "ytdlp"}:
            return CapabilityResult.fail("未知取流方式", error_code="invalid_argument")
        cookie, cookie_src = await self.resolve_cookie()
        assert self.ctx is not None
        android_token = str(self.ctx.get_secret("android_access_token") or "")
        unlock_hd = self._unlock_hd()
        preferred_codec = self._preferred_codec_id()

        mode, bin_path = resolve_ytdlp_mode(client, method=method, ytdlp_bin=ytdlp_bin)
        async def native_stream() -> dict[str, Any]:
            grpc_endpoint = validate_grpc_endpoint(
                str(cfg.get("grpc_endpoint") or DEFAULT_GRPC_ENDPOINT),
                allowed_loopback_origins=client.allowed_loopback_origins,
            )
            curl_bin = validate_curl_bin(str(cfg.get("curl_bin") or "curl"))
            return await resolve_via_playurl(
                client,
                target,
                cookie=cookie,
                req_label=req_label,
                req_qn=req_qn,
                android_token=android_token,
                unlock_hd=unlock_hd,
                preferred_codec=preferred_codec,
                proxy=proxy,
                grpc_endpoint=grpc_endpoint,
                curl_bin=curl_bin,
                plugin=self,
            )

        async def ytdlp_stream() -> dict[str, Any]:
            return await resolve_via_ytdlp(
                self, bin_path, target["url"], cookie=cookie, proxy=proxy,
                timeout_sec=client.timeout_sec, req_label=req_label, req_qn=req_qn,
                allowed_origins=client.allowed_loopback_origins,
            )

        # An authorized App session must participate in normal playback even
        # when yt-dlp happens to be installed. Explicit methods stay explicit.
        if method == "ytdlp":
            resolved = await ytdlp_stream()
        elif method == "playurl" or mode != "ytdlp":
            resolved = await native_stream()
        else:
            try:
                resolved = await native_stream()
            except BilibiliApiError as native_error:
                if native_error.error_code in {"drm_protected", "not_found", "cancelled"}:
                    raise
                try:
                    resolved = await ytdlp_stream()
                except BilibiliApiError as extractor_error:
                    raise BilibiliApiError(
                        f"原生取流：{native_error.message}；yt-dlp：{extractor_error.message}",
                        error_code=extractor_error.error_code,
                    ) from None

        android_media = bool(resolved.get("android_media"))
        headers = default_stream_headers(
            target.get("bvid", ""), target.get("aid"), android_media=android_media
        )
        item_id = target.get("bvid") or (f"av{target['aid']}" if target.get("aid") else target["url"])
        out = {
            **resolved,
            "bvid": target.get("bvid", ""),
            "aid": target.get("aid"),
            "item_id": item_id,
            "requested_quality": req_label,
            "cookie_source": cookie_src,
            "headers": headers,
            "android_media": android_media,
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
                    "audio_url": out.get("audio_url", ""),
                    "http_headers": headers,
                    "android_media": android_media,
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
            "audio_url": str(stream_res.data.get("audio_url") or ""),
            "http_headers": dict(stream_res.data.get("headers") or {}),
            "android_media": bool(stream_res.data.get("android_media")),
            "start_sec": float(req.get("start_sec") or 0.0),
        }
        await self.ctx.emit("entity.media.open", payload)
        return CapabilityResult.ok({**stream_res.data, "chapters": [c.model_dump() for c in chapters], "opened": True})

    def action_set_settings(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Persist quality, codec preference, and Android fallback toggle."""
        if self.ctx is None:
            return CapabilityResult.fail("插件尚未加载", error_code="not_configured")
        patch: dict[str, Any] = {}
        if "quality" in req:
            label, _ = normalize_quality(req.get("quality"))
            patch["quality"] = label
        if "preferred_codec" in req:
            codec = str(req.get("preferred_codec") or "hevc").strip().lower()
            if codec not in {"hevc", "avc", "av1"}:
                return CapabilityResult.fail("preferred_codec 必须是 hevc/avc/av1", error_code="invalid_argument")
            patch["preferred_codec"] = codec
        if "unlock_hd" in req:
            value = req.get("unlock_hd")
            patch["unlock_hd"] = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
        if "wiliwili_dir" in req:
            patch["wiliwili_dir"] = str(req.get("wiliwili_dir") or "").strip()
        self.ctx.set_config(patch)
        self._tab_status = "播放与登录设置已保存"
        return CapabilityResult.ok({"settings": {**self._cfg(), **patch}, "ui_schema": build_ui_schema(self)})

    def _build_ui_schema(self) -> dict[str, Any]:
        return build_ui_schema(self)

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
        if action_id not in {"qr_login_poll", "android_qr_login_poll"}:
            self._tab_status = res.error if not res.ok else str(res.data.get("status") or "操作完成")
        return {"ok": res.ok, "data": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `BilibiliPlugin` instance."""
    return BilibiliPlugin()
