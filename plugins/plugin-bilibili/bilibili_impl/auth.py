"""QR login flow, SecretVault/`web.cookies.v1` cookie resolution, logout, and status."""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import CapabilityResult

from .client import BilibiliApiError
from .qr import encode_qr_payload
from .security import format_cookie_header, mask_cookie, validate_url

if TYPE_CHECKING:
    from plugin import BilibiliPlugin


async def resolve_cookie(plugin: BilibiliPlugin) -> tuple[str, str]:
    """Resolve Bilibili Cookie strictly from SecretVault or `web.cookies.v1`."""
    if plugin.ctx is None:
        return "", "none"
    vault_cookie = format_cookie_header(plugin.ctx.get_secret("cookie_header"))
    if vault_cookie:
        return vault_cookie, "vault"
    if plugin.ctx.capabilities.has("web.cookies.v1"):
        res = await plugin.ctx.capabilities.call(
            "web.cookies.v1",
            {
                "action": "get_cookies",
                "url": "https://www.bilibili.com/",
                "domain": "bilibili.com",
                "include_subdomains": True,
            },
        )
        if res.ok:
            web_cookie = format_cookie_header(res.data.get("cookie_header") or res.data.get("cookies"))
            if web_cookie:
                return web_cookie, "web.cookies.v1"
    return "", "none"


async def action_status(plugin: BilibiliPlugin) -> CapabilityResult:
    """Check login status without exposing raw cookie secrets."""
    cookie, source = await resolve_cookie(plugin)
    if not cookie:
        return CapabilityResult.fail(
            "尚未配置 Bilibili 登录 Cookie",
            data={"logged_in": False, "cookie_configured": False, "cookie_source": "none", "cookie_masked": ""},
            error_code="not_configured",
        )
    client = plugin._build_client()
    try:
        nav = await client.check_nav(cookie)
    except BilibiliApiError as exc:
        return CapabilityResult.fail(
            exc.message,
            data={"logged_in": False, "cookie_configured": True, "cookie_source": source, "cookie_masked": mask_cookie(cookie)},
            error_code=exc.error_code,
        )
    if not bool(nav.get("isLogin")):
        return CapabilityResult.fail(
            "Bilibili Cookie 未登录或已失效",
            data={"logged_in": False, "cookie_configured": True, "cookie_source": source, "cookie_masked": mask_cookie(cookie)},
            error_code="unauthorized",
        )
    vip_label = nav.get("vip_label") if isinstance(nav.get("vip_label"), Mapping) else {}
    return CapabilityResult.ok(
        {
            "logged_in": True,
            "cookie_configured": True,
            "cookie_source": source,
            "cookie_masked": mask_cookie(cookie),
            "mid": nav.get("mid"),
            "uname": str(nav.get("uname") or ""),
            "vip_status": int(nav.get("vipStatus") or 0),
            "vip_type": int(nav.get("vipType") or 0),
            "vip_label": str(vip_label.get("text") or ""),
        }
    )


async def action_qr_login_start(plugin: BilibiliPlugin) -> CapabilityResult:
    """Generate a Bilibili QR login URL and SVG/PNG QR image payload."""
    client = plugin._build_client()
    data = await client.qr_generate()
    raw_url = str(data.get("url") or "").strip()
    qrcode_key = str(data.get("qrcode_key") or "").strip()
    if not raw_url or not qrcode_key:
        raise BilibiliApiError("二维码生成接口缺少 url 或 qrcode_key", error_code="invalid_response")
    qr_url = validate_url(raw_url, allow_cdn=False, allowed_loopback_origins=client.allowed_loopback_origins)
    plugin._qr_sessions[qrcode_key] = time.monotonic()
    return CapabilityResult.ok(
        {"qrcode_key": qrcode_key, "qr_url": qr_url, "url": qr_url, "expires_in_sec": 180, **encode_qr_payload(qr_url)}
    )


async def action_qr_login_poll(plugin: BilibiliPlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """Poll Bilibili QR login state and save confirmed cookie into 0600 SecretVault."""
    assert plugin.ctx is not None
    qrcode_key = str(req.get("qrcode_key") or "").strip()
    if not qrcode_key and plugin._qr_sessions:
        qrcode_key = list(plugin._qr_sessions.keys())[-1]
    if not qrcode_key:
        return CapabilityResult.fail("缺少 qrcode_key，请先调用 qr_login_start", error_code="invalid_argument")

    client = plugin._build_client()
    data, header_cookies = await client.qr_poll(qrcode_key)
    code = int(data.get("code") if data.get("code") is not None else -1)

    if code == 86101:
        return CapabilityResult.ok({"status": "waiting", "code": 86101, "logged_in": False, "qrcode_key": qrcode_key})
    if code == 86090:
        return CapabilityResult.ok({"status": "scanned", "code": 86090, "logged_in": False, "qrcode_key": qrcode_key})
    if code == 86038:
        plugin._qr_sessions.pop(qrcode_key, None)
        return CapabilityResult.ok({"status": "expired", "code": 86038, "logged_in": False, "qrcode_key": qrcode_key})
    if code != 0:
        return CapabilityResult.fail(
            str(data.get("message") or f"扫码登录失败 ({code})"),
            data={"status": "error", "code": code, "logged_in": False},
            error_code="unauthorized",
        )

    merged: dict[str, str] = dict(header_cookies)
    if raw_redirect_url := str(data.get("url") or ""):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_redirect_url).query)
        for key in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
            if key in qs and qs[key] and qs[key][0]:
                merged.setdefault(key, qs[key][0])
    if isinstance(data.get("cookies"), Mapping):
        for k, v in data["cookies"].items():
            if v:
                merged.setdefault(str(k), str(v))

    cookie_str = format_cookie_header(merged)
    if not cookie_str:
        return CapabilityResult.fail("登录确认后未收到有效 Cookie", error_code="unauthorized")

    plugin.ctx.set_secret("cookie_header", cookie_str)
    refresh_token = str(data.get("refresh_token") or "").strip()
    if refresh_token:
        plugin.ctx.set_secret("refresh_token", refresh_token)
    plugin._qr_sessions.pop(qrcode_key, None)
    return CapabilityResult.ok(
        {
            "status": "confirmed",
            "code": 0,
            "logged_in": True,
            "cookie_configured": True,
            "cookie_masked": mask_cookie(cookie_str),
            "has_refresh_token": bool(refresh_token),
        }
    )


async def action_logout(plugin: BilibiliPlugin) -> CapabilityResult:
    """Clear stored Bilibili credentials from SecretVault and memory."""
    if plugin.ctx is not None:
        plugin.ctx.set_secret("cookie_header", None)
        plugin.ctx.set_secret("refresh_token", None)
    plugin._qr_sessions.clear()
    return CapabilityResult.ok({"status": "logged_out", "logged_in": False, "cookie_configured": False})
