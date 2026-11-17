"""Multi-source Cookie resolution (web.cookies.v1 -> SecretVault -> Netscape/JSON file) and login flow."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnidesk_sdk import PluginContext

if __package__:
    from .zhihu_errors import (
        ZhihuError,
        coerce_bool,
        coerce_float,
        has_control_chars,
        json_str,
        redact_zhihu_secrets,
        validate_https_url,
        validate_safe_file_path,
    )
else:
    from zhihu_errors import (  # type: ignore[no-redef]
        ZhihuError,
        coerce_bool,
        coerce_float,
        has_control_chars,
        json_str,
        redact_zhihu_secrets,
        validate_https_url,
        validate_safe_file_path,
    )

VALID_COOKIE_SOURCES: frozenset[str] = frozenset(
    {"auto", "plugin-chrome-web", "vault", "file", "disabled"}
)
_CHROMIUM_EPOCH_OFFSET_SEC = 11644473600
_ZHIHU_SIGNIN_URL = "https://www.zhihu.com/signin"


@dataclass(frozen=True, slots=True)
class CookieEntry:
    """RFC 6265 cookie record parsed from JSON or Netscape cookies.txt."""

    name: str
    value: str
    domain: str
    path: str = "/"
    host_only: bool = False
    secure: bool = False
    http_only: bool = False
    expires: int = 0


@dataclass
class ZhihuConfig:
    """Validated runtime configuration aligned with plugin.json config_schema."""

    chrome_cookie_source: str = "auto"
    cookie_file_path: str = ""
    prefer_chrome_fetch: bool = False
    request_timeout_sec: float = 15.0
    pinned_channels: str = ""
    auto_translate: bool = False


@dataclass(frozen=True, slots=True)
class AuthResolution:
    """Resolved cookie header and key-cookie presence flags without leaking secrets."""

    cookie_header: str = ""
    source_used: str = "guest"
    has_z_c0: bool = False
    has_d_c0: bool = False
    has_xsrf: bool = False
    has_zse_ck: bool = False
    session_expired: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.has_z_c0 and not self.session_expired


def normalize_cookie_header_input(raw: Any) -> str:
    """Normalize a manual Cookie header string and reject control characters."""
    if not isinstance(raw, str):
        raise ZhihuError("invalid_argument", "cookie_header 必须为字符串")
    cleaned = raw.strip()
    if not cleaned:
        return ""
    if has_control_chars(cleaned):
        raise ZhihuError("invalid_argument", "cookie_header 包含非法控制字符")
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()
    return cleaned


def extract_cookie_value(cookie_header: str, name: str) -> str:
    """Extract a named cookie value from an HTTP Cookie header string."""
    if not cookie_header or not name:
        return ""
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return ""


def merge_cookie_headers(primary: str, secondary: str) -> str:
    """Merge two Cookie header strings giving `primary` precedence per cookie name."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for hdr in (primary, secondary):
        if not hdr:
            continue
        for part in hdr.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            key, val = k.strip(), v.strip()
            if not key or not val or key in seen:
                continue
            seen.add(key)
            pairs.append((key, val))
    return "; ".join(f"{k}={v}" for k, v in pairs)


def inspect_key_cookies(cookie_header: str) -> tuple[bool, bool, bool, bool]:
    """Return `(has_z_c0, has_d_c0, has_xsrf, has_zse_ck)` presence flags."""
    return (
        bool(extract_cookie_value(cookie_header, "z_c0")),
        bool(extract_cookie_value(cookie_header, "d_c0")),
        bool(extract_cookie_value(cookie_header, "_xsrf")),
        bool(extract_cookie_value(cookie_header, "__zse_ck")),
    )


def _clean_domain(domain: str) -> str:
    return domain.strip().lower().lstrip(".")


def _domain_matches(host: str, cookie_domain: str, host_only: bool) -> bool:
    h = _clean_domain(host)
    d = _clean_domain(cookie_domain)
    if not h or not d:
        return False
    if h == d:
        return True
    if host_only or len(h) <= len(d):
        return False
    return h.endswith(f".{d}")


def _path_matches(req_path: str, cookie_path: str) -> bool:
    rp = req_path or "/"
    cp = cookie_path or "/"
    if rp == cp:
        return True
    if not rp.startswith(cp):
        return False
    return cp.endswith("/") or rp[len(cp)] == "/"


def _parse_cookie_expiry(item: Mapping[str, Any]) -> int:
    for key in ("expirationDate", "expires"):
        val = item.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return int(val)
    utc_val = item.get("expires_utc")
    if isinstance(utc_val, (int, float)) and not isinstance(utc_val, bool) and utc_val > 0:
        if utc_val > 10_000_000_000_000:
            return max(0, int((utc_val / 1_000_000.0) - _CHROMIUM_EPOCH_OFFSET_SEC))
        return int(utc_val)
    return 0


def parse_netscape_cookies(text: str) -> list[CookieEntry]:
    """Parse Netscape `cookies.txt` including `#HttpOnly_` entries."""
    entries: list[CookieEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r").strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[10:]
        elif line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        domain = _clean_domain(cols[0])
        host_only = cols[1].strip().lower() != "true"
        path = cols[2].strip() or "/"
        secure = cols[3].strip().lower() == "true"
        try:
            expires = int(cols[4].strip())
        except ValueError:
            expires = 0
        name = cols[5].strip()
        value = cols[6].strip()
        if name and domain:
            entries.append(
                CookieEntry(
                    name=name,
                    value=value,
                    domain=domain,
                    path=path,
                    host_only=host_only,
                    secure=secure,
                    http_only=http_only,
                    expires=expires,
                )
            )
    return entries


def parse_json_cookies(raw_items: Any) -> list[CookieEntry]:
    """Parse JSON cookie array or `{"cookies": [...]}` object."""
    data = raw_items
    if isinstance(raw_items, str):
        try:
            data = json.loads(raw_items)
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        arr = data
    elif isinstance(data, Mapping) and isinstance(data.get("cookies"), list):
        arr = data["cookies"]
    else:
        return []

    entries: list[CookieEntry] = []
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        name = json_str(item, "name", "").strip()
        value = json_str(item, "value", "").strip()
        raw_dom = json_str(item, "domain", "").strip()
        if raw_dom.startswith("."):
            host_only = False
        elif isinstance(item.get("hostOnly"), bool):
            host_only = bool(item["hostOnly"])
        else:
            host_only = False
        domain = _clean_domain(raw_dom)
        path = json_str(item, "path", "/").strip() or "/"
        secure = bool(item.get("secure", False))
        http_only = bool(item.get("httpOnly", item.get("http_only", False)))
        expires = _parse_cookie_expiry(item)
        if name and domain:
            entries.append(
                CookieEntry(
                    name=name,
                    value=value,
                    domain=domain,
                    path=path,
                    host_only=host_only,
                    secure=secure,
                    http_only=http_only,
                    expires=expires,
                )
            )
    return entries


def load_cookies_from_file(path_str: str) -> list[CookieEntry]:
    """Validate path safety (rejecting '..' and symlinks) and parse JSON or Netscape cookies."""
    if not path_str.strip():
        return []
    safe_path = validate_safe_file_path(path_str, field_name="cookie_file_path")
    if not safe_path.is_file():
        return []
    try:
        content = safe_path.read_text(encoding="utf-8")
    except OSError:
        return []
    stripped = content.strip()
    if not stripped:
        return []
    if stripped[0] in {"[", "{"}:
        entries = parse_json_cookies(stripped)
        if entries:
            return entries
    return parse_netscape_cookies(content)


def build_cookie_header(
    entries: list[CookieEntry],
    url: str,
    now_epoch_sec: int | None = None,
) -> str:
    """Filter cookies by RFC 6265 domain, path, secure flag, and expiry, longest-path first."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    req_path = parsed.path or "/"
    if scheme not in {"http", "https"} or not host:
        return ""

    now = int(time.time()) if now_epoch_sec is None else int(now_epoch_sec)
    is_https = scheme == "https"
    matched: list[CookieEntry] = []
    for c in entries:
        if c.expires > 0 and c.expires <= now:
            continue
        if c.secure and not is_https:
            continue
        if not _domain_matches(host, c.domain, c.host_only):
            continue
        if not _path_matches(req_path, c.path):
            continue
        matched.append(c)

    matched.sort(key=lambda c: len(c.path), reverse=True)
    ordered: list[str] = []
    seen: set[str] = set()
    for c in matched:
        if c.name in seen:
            continue
        seen.add(c.name)
        ordered.append(f"{c.name}={c.value}")
    return "; ".join(ordered)


class AuthManager:
    """Coordinates Chrome cookie sync, SecretVault storage, and Cookie file fallback."""

    def __init__(self, ctx: PluginContext | None = None) -> None:
        self._lock = threading.RLock()
        self._ctx: PluginContext | None = ctx
        self._config = ZhihuConfig()
        self._vault_cookie_header: str = ""
        self._cached_chrome_cookie_header: str = ""
        self._session_expired: bool = False
        self._last_error: str = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        with self._lock:
            self._ctx = ctx

    def configure(self, cfg: ZhihuConfig, vault_cookie_header: str = "") -> None:
        norm_source = (cfg.chrome_cookie_source or "auto").strip().lower() or "auto"
        if norm_source not in VALID_COOKIE_SOURCES:
            raise ZhihuError("invalid_argument", f"不支持的 chrome_cookie_source: {cfg.chrome_cookie_source!r}")
        file_path = (cfg.cookie_file_path or "").strip()
        if file_path:
            validate_safe_file_path(file_path, field_name="cookie_file_path")
        timeout = max(0.1, min(120.0, coerce_float(cfg.request_timeout_sec, 15.0)))
        normalized_vault = normalize_cookie_header_input(vault_cookie_header) if vault_cookie_header else ""

        with self._lock:
            self._config = ZhihuConfig(
                chrome_cookie_source=norm_source,
                cookie_file_path=file_path,
                prefer_chrome_fetch=bool(cfg.prefer_chrome_fetch),
                request_timeout_sec=timeout,
                pinned_channels=str(cfg.pinned_channels or "").strip(),
                auto_translate=bool(cfg.auto_translate),
            )
            self._vault_cookie_header = normalized_vault

    def load_from_context(self, ctx: PluginContext | None = None) -> ZhihuConfig:
        target_ctx = ctx if ctx is not None else self._ctx
        if target_ctx is None:
            return self.config()
        if ctx is not None:
            self.set_context(ctx)
        raw = target_ctx.get_config()
        secret = target_ctx.get_secret("cookie_header", "") or ""
        cfg = ZhihuConfig(
            chrome_cookie_source=str(raw.get("chrome_cookie_source") or "auto"),
            cookie_file_path=str(raw.get("cookie_file_path") or ""),
            prefer_chrome_fetch=coerce_bool(raw.get("prefer_chrome_fetch"), False),
            request_timeout_sec=coerce_float(raw.get("request_timeout_sec"), 15.0),
            pinned_channels=str(raw.get("pinned_channels") or ""),
            auto_translate=coerce_bool(raw.get("auto_translate"), False),
        )
        self.configure(cfg, secret)
        return self.config()

    def config(self) -> ZhihuConfig:
        with self._lock:
            return ZhihuConfig(
                chrome_cookie_source=self._config.chrome_cookie_source,
                cookie_file_path=self._config.cookie_file_path,
                prefer_chrome_fetch=self._config.prefer_chrome_fetch,
                request_timeout_sec=self._config.request_timeout_sec,
                pinned_channels=self._config.pinned_channels,
                auto_translate=self._config.auto_translate,
            )

    def _read_vault_header(self, ctx: PluginContext | None) -> str:
        with self._lock:
            hdr = self._vault_cookie_header
        if hdr:
            return hdr
        if ctx is not None:
            raw = ctx.get_secret("cookie_header", "") or ""
            if raw.strip():
                try:
                    return normalize_cookie_header_input(raw)
                except ZhihuError:
                    return ""
        return ""

    def _build_resolution(self, cookie_header: str, source_used: str) -> AuthResolution:
        z_c0, d_c0, xsrf, zse_ck = inspect_key_cookies(cookie_header)
        with self._lock:
            expired = self._session_expired if z_c0 else False
        return AuthResolution(
            cookie_header=cookie_header,
            source_used=source_used,
            has_z_c0=z_c0,
            has_d_c0=d_c0,
            has_xsrf=xsrf,
            has_zse_ck=zse_ck,
            session_expired=expired,
        )

    async def _try_chrome_capability(
        self, ctx: PluginContext | None, cap_id: str, url: str
    ) -> str:
        if ctx is None or not ctx.capabilities.has(cap_id):
            return ""
        target_url = url or "https://www.zhihu.com/"
        res = await ctx.capabilities.call(
            cap_id,
            {
                "action": "get_cookies",
                "url": target_url,
                "domain": "zhihu.com",
                "include_subdomains": True,
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            return ""
        raw_hdr = res.data.get("cookie_header")
        if isinstance(raw_hdr, str) and raw_hdr.strip():
            try:
                return normalize_cookie_header_input(raw_hdr)
            except ZhihuError:
                return ""
        raw_cookies = res.data.get("cookies")
        if isinstance(raw_cookies, list):
            entries = parse_json_cookies(raw_cookies)
            return build_cookie_header(entries, target_url)
        return ""

    async def resolve_for_url(self, url: str = "https://www.zhihu.com/") -> AuthResolution:
        """Resolve session cookies asynchronously across Chrome -> Vault -> File."""
        with self._lock:
            ctx = self._ctx
            cfg = self.config()

        if cfg.chrome_cookie_source == "disabled":
            return AuthResolution()

        target_url = url or "https://www.zhihu.com/"
        mode = cfg.chrome_cookie_source
        try_plugin = mode in {"auto", "plugin-chrome-web"}
        try_vault = mode in {"auto", "vault"}
        try_file = mode in {"auto", "file"}

        chrome_partial = ""
        chrome_source = ""
        if try_plugin:
            for cap_id in ("web.cookies.v1", "web.embed.v1"):
                hdr = await self._try_chrome_capability(ctx, cap_id, target_url)
                if hdr:
                    with self._lock:
                        self._cached_chrome_cookie_header = hdr
                    z_c0, _, _, _ = inspect_key_cookies(hdr)
                    if z_c0 or mode == "plugin-chrome-web":
                        return self._build_resolution(hdr, f"chrome:{cap_id}")
                    chrome_partial = hdr
                    chrome_source = f"chrome:{cap_id}"
                    break

        if try_vault:
            vault_hdr = self._read_vault_header(ctx)
            if vault_hdr:
                merged = merge_cookie_headers(chrome_partial, vault_hdr) if chrome_partial else vault_hdr
                return self._build_resolution(merged, "vault")

        if try_file and cfg.cookie_file_path:
            entries = load_cookies_from_file(cfg.cookie_file_path)
            file_hdr = build_cookie_header(entries, target_url)
            if file_hdr:
                merged = merge_cookie_headers(chrome_partial, file_hdr) if chrome_partial else file_hdr
                return self._build_resolution(merged, "file")

        if chrome_partial:
            return self._build_resolution(chrome_partial, chrome_source)
        return AuthResolution()

    def resolve_for_url_sync(self, url: str = "https://www.zhihu.com/") -> AuthResolution:
        """Non-blocking synchronous resolution using cached Chrome snapshot, Vault, and File."""
        with self._lock:
            ctx = self._ctx
            cfg = self.config()
            cached_chrome = self._cached_chrome_cookie_header

        if cfg.chrome_cookie_source == "disabled":
            return AuthResolution()

        target_url = url or "https://www.zhihu.com/"
        mode = cfg.chrome_cookie_source
        if mode in {"auto", "plugin-chrome-web"} and cached_chrome:
            z_c0, _, _, _ = inspect_key_cookies(cached_chrome)
            if z_c0 or mode == "plugin-chrome-web":
                return self._build_resolution(cached_chrome, "chrome:web.cookies.v1")

        if mode in {"auto", "vault"}:
            vault_hdr = self._read_vault_header(ctx)
            if vault_hdr:
                return self._build_resolution(vault_hdr, "vault")

        if mode in {"auto", "file"} and cfg.cookie_file_path:
            try:
                entries = load_cookies_from_file(cfg.cookie_file_path)
            except ZhihuError:
                entries = []
            file_hdr = build_cookie_header(entries, target_url)
            if file_hdr:
                return self._build_resolution(file_hdr, "file")

        if cached_chrome:
            return self._build_resolution(cached_chrome, "chrome:web.cookies.v1")
        return AuthResolution()

    async def sync_from_chrome(self) -> tuple[bool, str, str, AuthResolution]:
        """Pull zhihu.com cookies from `plugin-chrome-web` and persist valid sessions to SecretVault."""
        with self._lock:
            ctx = self._ctx

        if ctx is None or (not ctx.capabilities.has("web.cookies.v1") and not ctx.capabilities.has("web.embed.v1")):
            msg = "未检测到 plugin-chrome-web (web.cookies.v1)"
            with self._lock:
                self._last_error = msg
            return False, "", msg, AuthResolution()

        hdr = ""
        source = ""
        for cap_id in ("web.cookies.v1", "web.embed.v1"):
            hdr = await self._try_chrome_capability(ctx, cap_id, "https://www.zhihu.com/")
            if hdr:
                source = f"chrome:{cap_id}"
                break

        if not hdr:
            msg = "Chrome CookieStore 暂无 zhihu.com 登录凭证，请先完成扫码/账号登录"
            with self._lock:
                self._last_error = msg
            return False, "", msg, AuthResolution()

        res = self._build_resolution(hdr, source)
        if not res.has_z_c0:
            msg = "已检测到 zhihu.com 访客 Cookie，但缺少登录凭证 z_c0，请在 Chrome 完成账号登录"
            with self._lock:
                self._cached_chrome_cookie_header = hdr
                self._last_error = msg
            return False, source, msg, res

        with self._lock:
            self._cached_chrome_cookie_header = hdr
            self._session_expired = False
            self._last_error = ""
        self.save_vault_cookie_header(hdr)
        return True, source, "", self._build_resolution(hdr, source)

    async def open_login_window(
        self, title: str = "知乎登录（完成后关闭窗口即可）"
    ) -> tuple[bool, dict[str, Any], str, str]:
        """Launch managed Chrome `--app` login window via `web.embed.v1`."""
        with self._lock:
            ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.embed.v1"):
            return (
                False,
                {},
                "service_offline",
                "未检测到 plugin-chrome-web (web.embed.v1)，可在插件设置手动填写 SecretVault cookie_header",
            )
        signin_url = validate_https_url(_ZHIHU_SIGNIN_URL)
        res = await ctx.capabilities.call(
            "web.embed.v1",
            {
                "action": "open",
                "url": signin_url,
                "title": title,
                "auto_translate": False,
            },
        )
        if not res.ok:
            code = str((res.data or {}).get("error_code") or "service_offline")
            msg = redact_zhihu_secrets(res.error or "呼起知乎登录窗失败", self.secrets_for_redaction())
            with self._lock:
                self._last_error = msg
            return False, {}, code, msg
        return True, dict(res.data or {}), "", ""

    def save_vault_cookie_header(self, cookie_header: str) -> AuthResolution:
        """Validate and persist a Cookie header into 0600 SecretVault."""
        cleaned = normalize_cookie_header_input(cookie_header)
        if not cleaned:
            raise ZhihuError("invalid_argument", "cookie_header 不能为空")
        with self._lock:
            self._vault_cookie_header = cleaned
            self._session_expired = False
            self._last_error = ""
            ctx = self._ctx
        if ctx is not None:
            ctx.set_secret("cookie_header", cleaned)
        return self._build_resolution(cleaned, "vault")

    def logout(self) -> None:
        """Clear cached and persisted Zhihu session cookies."""
        with self._lock:
            ctx = self._ctx
            self._vault_cookie_header = ""
            self._cached_chrome_cookie_header = ""
            self._session_expired = False
            self._last_error = ""
        if ctx is not None:
            ctx.set_secret("cookie_header", None)

    def mark_session_expired(self, reason: str) -> None:
        with self._lock:
            self._session_expired = True
            self._last_error = redact_zhihu_secrets(reason, self.secrets_for_redaction())

    def mark_authenticated(self) -> None:
        with self._lock:
            self._session_expired = False
            self._last_error = ""

    def secrets_for_redaction(self) -> tuple[str, ...]:
        """Collect stored cookie headers and individual cookie values (len >= 4) for scrubbing."""
        with self._lock:
            vault = self._vault_cookie_header
            cached = self._cached_chrome_cookie_header
            ctx = self._ctx
        if not vault and ctx is not None:
            vault = ctx.get_secret("cookie_header", "") or ""
        items: list[str] = []
        for raw in (vault, cached):
            if not raw:
                continue
            items.append(raw)
            for pair in raw.split(";"):
                if "=" in pair:
                    val = pair.split("=", 1)[1].strip()
                    if len(val) >= 4:
                        items.append(val)
        return tuple(dict.fromkeys(items))

    def safe_status(self, resolved: AuthResolution | None = None) -> dict[str, Any]:
        """Return non-secret auth status snapshot safe for UI, RPC, and diagnostics."""
        r = resolved if resolved is not None else self.resolve_for_url_sync("https://www.zhihu.com/")
        cfg = self.config()
        with self._lock:
            ctx = self._ctx
            last_err = self._last_error
        pinned = [p.strip() for p in cfg.pinned_channels.split(",") if p.strip()]
        return {
            "authenticated": r.is_authenticated,
            "cookie_source": r.source_used,
            "chrome_cookie_source": cfg.chrome_cookie_source,
            "prefer_chrome_fetch": cfg.prefer_chrome_fetch,
            "auto_translate": cfg.auto_translate,
            "pinned_channels": pinned,
            "has_z_c0": r.has_z_c0,
            "has_d_c0": r.has_d_c0,
            "has_xsrf": r.has_xsrf,
            "has_zse_ck": r.has_zse_ck,
            "session_expired": r.session_expired,
            "has_cookie_file": bool(cfg.cookie_file_path),
            "chrome_bridge_available": bool(
                ctx is not None and (ctx.capabilities.has("web.cookies.v1") or ctx.capabilities.has("web.embed.v1"))
            ),
            "chrome_fetch_available": bool(
                ctx is not None and (ctx.capabilities.has("web.fetch.v1") or ctx.capabilities.has("web.extract.v1"))
            ),
            "last_error": redact_zhihu_secrets(last_err, self.secrets_for_redaction()),
        }
