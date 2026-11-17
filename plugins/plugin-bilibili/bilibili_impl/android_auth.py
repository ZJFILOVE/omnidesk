"""Android/TV Passport QR login and Wiliwili session import."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import CapabilityResult

from .client import BilibiliApiError
from .qr import encode_qr_payload
from .security import format_cookie_header, mask_cookie, validate_url

if TYPE_CHECKING:
    from plugin import BilibiliPlugin

TV_APP_KEY = "4409e2ce8ffd12b8"
TV_APP_SECRET = "59b43e04ad6965f34319062b478f83dd"


def sign_app_params(
    params: Mapping[str, Any],
    *,
    appkey: str = TV_APP_KEY,
    appsec: str = TV_APP_SECRET,
    timestamp: int | None = None,
) -> str:
    """Sign TV Passport form parameters with MD5(appkey-secret + sorted query)."""
    signed = {str(key): str(value) for key, value in params.items()}
    signed["appkey"] = appkey
    signed.setdefault("ts", str(timestamp if timestamp is not None else int(time.time())))
    encoded = "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(value, safe='-_.~')}"
        for key, value in sorted(signed.items())
    )
    signature = hashlib.md5((encoded + appsec).encode("utf-8")).hexdigest()
    return f"{encoded}&sign={signature}"


def _post_signed(plugin: BilibiliPlugin, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
    client = plugin._build_client()
    url = f"{client.passport_base.rstrip('/')}/{path.lstrip('/')}"
    safe_url = validate_url(
        url,
        allow_cdn=False,
        allowed_loopback_origins=client.allowed_loopback_origins,
    )
    body = sign_app_params(params).encode("utf-8")
    request = urllib.request.Request(safe_url, data=body, method="POST")
    request.add_header("User-Agent", "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36")
    request.add_header("Referer", "https://passport.bilibili.com/")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = client._build_opener(allow_cdn=False)
    try:
        with opener.open(request, timeout=client.timeout_sec) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise BilibiliApiError(
            f"Android/TV 登录接口 HTTP {exc.code}",
            error_code="unauthorized" if exc.code in (401, 403) else "service_offline",
            http_status=exc.code,
        ) from None
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise BilibiliApiError(f"Android/TV 登录接口不可达: {reason}", error_code="service_offline") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BilibiliApiError(f"Android/TV 登录响应 JSON 无效: {exc}", error_code="invalid_response") from None
    if not isinstance(payload, Mapping):
        raise BilibiliApiError("Android/TV 登录响应必须是对象", error_code="invalid_response")
    return dict(payload)


async def start_android_qr(plugin: BilibiliPlugin) -> CapabilityResult:
    """Request an Android/TV Passport QR code for PlayViewUnite tokens."""
    payload = _post_signed(plugin, "/x/passport-tv-login/qrcode/auth_code", {"local_id": "0"})
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    raw_url = str(data.get("url") or "").strip()
    auth_code = str(data.get("auth_code") or "").strip()
    if int(payload.get("code") or 0) != 0 or not raw_url or not auth_code:
        message = str(payload.get("message") or "Android/TV 二维码获取失败")
        return CapabilityResult.fail(message, error_code="unauthorized")
    client = plugin._build_client()
    qr_url = validate_url(
        raw_url,
        allow_cdn=False,
        allowed_loopback_origins=client.allowed_loopback_origins,
    )
    plugin._qr_sessions[auth_code] = time.monotonic()
    plugin._android_qr_auth_code = auth_code
    result = {
        "mode": "android",
        "auth_code": auth_code,
        "qrcode_key": auth_code,
        "qr_url": qr_url,
        "url": qr_url,
        "expires_in_sec": 180,
        **encode_qr_payload(qr_url),
    }
    plugin._last_qr = dict(result)
    plugin._tab_status = "请用 Bilibili 手机客户端扫码授权 Android/TV 取流"
    return CapabilityResult.ok(result)


async def poll_android_qr(plugin: BilibiliPlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """Poll Android/TV Passport login and persist tokens in SecretVault."""
    assert plugin.ctx is not None
    auth_code = str(req.get("auth_code") or req.get("qrcode_key") or plugin._android_qr_auth_code or "").strip()
    if not auth_code:
        return CapabilityResult.fail("缺少 auth_code，请先生成 TV/Android 二维码", error_code="invalid_argument")
    payload = _post_signed(
        plugin,
        "/x/passport-tv-login/qrcode/poll",
        {"auth_code": auth_code, "local_id": "0"},
    )
    code = int(payload.get("code") if payload.get("code") is not None else -1)
    if code == 86039:
        plugin._tab_status = "Android/TV 扫码：等待手机扫码…"
        return CapabilityResult.ok({"mode": "android", "status": "waiting", "code": code, "logged_in": False})
    if code == 86090:
        plugin._tab_status = "Android/TV 扫码：已扫码，等待手机确认"
        return CapabilityResult.ok({"mode": "android", "status": "scanned", "code": code, "logged_in": False})
    if code == 86038:
        plugin._qr_sessions.pop(auth_code, None)
        plugin._android_qr_auth_code = ""
        plugin._last_qr = None
        plugin._tab_status = "Android/TV 二维码已过期，请重新生成"
        return CapabilityResult.ok({"mode": "android", "status": "expired", "code": code, "logged_in": False})
    if code != 0:
        return CapabilityResult.fail(
            str(payload.get("message") or f"Android/TV 登录失败 ({code})"),
            data={"status": "error", "code": code, "logged_in": False},
            error_code="unauthorized",
        )

    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    token_info = data.get("token_info") if isinstance(data.get("token_info"), Mapping) else data
    token = str(token_info.get("access_token") or "").strip()
    refresh = str(token_info.get("refresh_token") or "").strip()
    mid = str(token_info.get("mid") or "").strip()
    expires_in = _int_value(token_info.get("expires_in"))
    if not token:
        return CapabilityResult.fail("Android/TV 轮询未返回 access_token", error_code="unauthorized")

    plugin.ctx.set_secret("android_access_token", token)
    plugin.ctx.set_secret("android_refresh_token", refresh or None)
    plugin.ctx.set_secret("android_mid", mid or None)
    plugin.ctx.set_secret("android_expires_at", str(int(time.time()) + expires_in) if expires_in > 0 else "")
    plugin._qr_sessions.pop(auth_code, None)
    plugin._android_qr_auth_code = ""
    plugin._last_qr = None
    plugin._tab_status = "Android/TV 授权成功，access_token 已写入 SecretVault"
    return CapabilityResult.ok(
        {
            "mode": "android",
            "status": "confirmed",
            "code": 0,
            "logged_in": True,
            "android_token_configured": True,
            "android_mid": mid,
            "has_refresh_token": bool(refresh),
        }
    )


def import_wiliwili_session(plugin: BilibiliPlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """Import Wiliwili web cookies and Android/TV token files into SecretVault."""
    assert plugin.ctx is not None
    raw_dir = str(
        req.get("wiliwili_dir")
        or req.get("dir")
        or plugin._cfg().get("wiliwili_dir")
        or _default_wiliwili_dir()
    ).strip()
    if not raw_dir:
        return CapabilityResult.fail("无法确定 Wiliwili 配置目录", error_code="not_configured")
    root = Path(raw_dir).expanduser()
    if not root.is_dir():
        return CapabilityResult.fail(f"Wiliwili 配置目录不存在: {root}", error_code="not_found")

    imported_web = False
    imported_android = False
    cookie_header = ""
    web_refresh = ""
    config_path = root / "wiliwili_config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            cookies = config.get("cookie") if isinstance(config, Mapping) else None
            cookie_header = format_cookie_header(cookies)
            web_refresh = str(config.get("refresh") or "") if isinstance(config, Mapping) else ""
            imported_web = bool(cookie_header and "SESSDATA=" in cookie_header)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    access_token = ""
    android_refresh = ""
    android_mid = ""
    expires_at = ""
    session_path = root / "android-session.json"
    if session_path.is_file():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
            if isinstance(session, Mapping):
                access_token = str(session.get("access_token") or "").strip()
                android_refresh = str(session.get("refresh_token") or "").strip()
                android_mid = str(session.get("mid") or "").strip()
                expires_at = str(session.get("expires_at") or "").strip()
            imported_android = bool(access_token)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    if imported_web:
        plugin.ctx.set_secret("cookie_header", cookie_header)
        plugin.ctx.set_secret("refresh_token", web_refresh or None)
    if imported_android:
        plugin.ctx.set_secret("android_access_token", access_token)
        plugin.ctx.set_secret("android_refresh_token", android_refresh or None)
        plugin.ctx.set_secret("android_mid", android_mid or None)
        plugin.ctx.set_secret("android_expires_at", expires_at or None)
    if not imported_web and not imported_android:
        return CapabilityResult.fail(
            "Wiliwili 目录中未找到可导入的 Web Cookie 或 Android/TV token",
            error_code="not_configured",
        )
    plugin._last_qr = None
    plugin._tab_status = (
        f"Wiliwili 会话已导入：Web={'yes' if imported_web else 'no'}，"
        f"Android={'yes' if imported_android else 'no'}"
    )
    return CapabilityResult.ok(
        {
            "status": "imported",
            "imported_web": imported_web,
            "imported_android": imported_android,
            "cookie_masked": mask_cookie(cookie_header) if imported_web else "",
            "android_mid": android_mid if imported_android else "",
            "android_token_configured": imported_android,
            "wiliwili_dir": str(root),
        }
    )


def android_auth_state(plugin: BilibiliPlugin) -> dict[str, Any]:
    """Return non-secret Android/TV session metadata."""
    if plugin.ctx is None:
        return {"android_token_configured": False, "android_mid": "", "android_expires_at": 0}
    token = str(plugin.ctx.get_secret("android_access_token") or "")
    expires_raw = str(plugin.ctx.get_secret("android_expires_at") or "")
    try:
        expires_at = int(expires_raw) if expires_raw else 0
    except ValueError:
        expires_at = 0
    return {
        "android_token_configured": bool(token),
        "android_mid": str(plugin.ctx.get_secret("android_mid") or ""),
        "android_expires_at": expires_at,
        "android_expired": bool(expires_at and expires_at <= int(time.time())),
    }


def _default_wiliwili_dir() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return str(Path(xdg) / "wiliwili")
    home = os.environ.get("HOME", "").strip()
    return str(Path(home) / ".config" / "wiliwili") if home else ""


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
