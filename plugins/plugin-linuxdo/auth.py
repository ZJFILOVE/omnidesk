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
    from .security import (
        DEFAULT_BASE_URL,
        LinuxdoError,
        has_control_chars,
        json_bool,
        json_bool_chain,
        json_str,
        redact_text,
        validate_safe_file_path,
    )
else:
    from security import (  # type: ignore[no-redef]
        DEFAULT_BASE_URL,
        LinuxdoError,
        has_control_chars,
        json_bool,
        json_bool_chain,
        json_str,
        redact_text,
        validate_safe_file_path,
    )

_AUTO_LOGIN_COOLDOWN_SEC = 120.0
_CHROMIUM_EPOCH_OFFSET_SEC = 11644473600


@dataclass(frozen=True, slots=True)
class CookieEntry:
    """RFC 6265 cookie record parsed from JSON or Netscape cookies.txt."""

    domain: str
    include_subdomains: bool = False
    path: str = "/"
    secure: bool = False
    http_only: bool = False
    expires: int = 0
    name: str = ""
    value: str = ""


@dataclass(frozen=True, slots=True)
class AuthResolution:
    """Resolved cookie header and metadata without leaking raw secret values."""

    cookie_header: str = ""
    source_used: str = "guest"
    has_t_cookie: bool = False
    has_session_cookie: bool = False
    has_cf_clearance: bool = False


def normalize_cookie_input(raw: str) -> str:
    """Normalize pasted Cookie header or bare `_t` token without allowing control characters."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    if has_control_chars(cleaned):
        raise LinuxdoError("invalid_argument", "Cookie Header 包含非法控制字符")
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()
    if cleaned and "=" not in cleaned and ";" not in cleaned:
        return f"_t={cleaned}"
    return cleaned


def extract_cookie_value(cookie_header: str, name: str) -> str:
    """Extract a specific cookie's value from an HTTP Cookie header."""
    if not cookie_header or not name:
        return ""
    for pair in cookie_header.split(";"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.strip() == name and v.strip():
            return v.strip()
    return ""


def contains_cookie_name(cookie_header: str, name: str) -> bool:
    return bool(extract_cookie_value(cookie_header, name))


def merge_cookie_headers(primary: str, secondary: str) -> str:
    """Merge two Cookie headers keeping `primary` precedence on key collisions."""
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for hdr in (primary, secondary):
        if not hdr:
            continue
        for pair in hdr.split(";"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            key, val = k.strip(), v.strip()
            if not key or not val or key in seen:
                continue
            seen.add(key)
            ordered.append((key, val))
    return "; ".join(f"{k}={v}" for k, v in ordered)


def domain_matches(host: str, cookie_domain: str, include_subdomains: bool) -> bool:
    """RFC 6265 host/domain matching."""
    h = host.strip().lower()
    d = cookie_domain.strip().lower().lstrip(".")
    if not h or not d:
        return False
    if h == d:
        return True
    if not include_subdomains or len(h) <= len(d):
        return False
    return h.endswith(f".{d}")


def path_matches(req_path: str, cookie_path: str) -> bool:
    """RFC 6265 path matching."""
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


def parse_json_cookies(raw_items: Any) -> list[CookieEntry]:
    """Parse cookie entries from a JSON string or decoded list/dict structure."""
    data = raw_items
    if isinstance(raw_items, str):
        try:
            data = json.loads(raw_items)
        except json.JSONDecodeError:
            return []
    if isinstance(data, Mapping) and isinstance(data.get("cookies"), list):
        arr = data["cookies"]
    elif isinstance(data, list):
        arr = data
    else:
        return []

    out: list[CookieEntry] = []
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        name = json_str(item, "name", "")
        value = json_str(item, "value", "")
        if not name:
            continue
        raw_dom = json_str(item, "domain", "").strip().lower()
        if raw_dom.startswith("."):
            include_sub = True
            dom = raw_dom.lstrip(".")
        elif isinstance(item.get("hostOnly"), bool):
            include_sub = not item["hostOnly"]
            dom = raw_dom
        else:
            dom = raw_dom
            include_sub = bool(dom)
        out.append(
            CookieEntry(
                domain=dom,
                include_subdomains=include_sub,
                path=json_str(item, "path", "/") or "/",
                secure=json_bool(item, "secure", False),
                http_only=json_bool_chain(item, ("httpOnly", "http_only", "httponly"), False),
                expires=_parse_cookie_expiry(item),
                name=name,
                value=value,
            )
        )
    return out


def parse_netscape_cookies(text: str) -> list[CookieEntry]:
    """Parse Netscape cookies.txt (including `#HttpOnly_` entries)."""
    out: list[CookieEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        http_only = False
        if line.startswith("#"):
            if line.startswith("#HttpOnly_"):
                line = line[10:]
                http_only = True
            else:
                continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        dom = cols[0].strip().lower().lstrip(".")
        include_sub = cols[1].strip().lower() == "true"
        path = cols[2].strip() or "/"
        secure = cols[3].strip().lower() == "true"
        try:
            expires = int(cols[4].strip())
        except ValueError:
            expires = 0
        name, value = cols[5], cols[6]
        if name:
            out.append(
                CookieEntry(
                    domain=dom,
                    include_subdomains=include_sub,
                    path=path,
                    secure=secure,
                    http_only=http_only,
                    expires=expires,
                    name=name,
                    value=value,
                )
            )
    return out


def load_cookies_from_file(path_str: str) -> list[CookieEntry]:
    """Safely load JSON or Netscape cookies from a validated local file path."""
    path = validate_safe_file_path(path_str)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    trimmed = content.strip()
    if not trimmed:
        return []
    if trimmed[0] in {"[", "{"}:
        return parse_json_cookies(trimmed)
    return parse_netscape_cookies(content)


def build_cookie_header(entries: list[CookieEntry], url: str, now_epoch_sec: int | None = None) -> str:
    """Filter cookies against URL scheme, host, path, and expiration, ordered longest-path first."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    scheme = parsed.scheme.lower()
    req_path = parsed.path or "/"
    if not host or scheme not in {"http", "https"}:
        return ""
    now = int(time.time()) if now_epoch_sec is None else now_epoch_sec
    matched: list[tuple[int, str]] = []
    for c in entries:
        if c.secure and scheme != "https":
            continue
        if c.expires > 0 and c.expires <= now:
            continue
        if not domain_matches(host, c.domain, c.include_subdomains):
            continue
        if not path_matches(req_path, c.path):
            continue
        matched.append((len(c.path), f"{c.name}={c.value}"))
    matched.sort(key=lambda item: item[0], reverse=True)
    return "; ".join(pair for _, pair in matched)


def _populate_flags(cookie_header: str, source_used: str) -> AuthResolution:
    return AuthResolution(
        cookie_header=cookie_header,
        source_used=source_used,
        has_t_cookie=contains_cookie_name(cookie_header, "_t"),
        has_session_cookie=contains_cookie_name(cookie_header, "_forum_session"),
        has_cf_clearance=contains_cookie_name(cookie_header, "cf_clearance"),
    )


class AuthManager:
    """Manages multi-source Cookie resolution, Chrome login window, and SecretVault persistence."""

    def __init__(self, ctx: PluginContext | None = None) -> None:
        self._ctx = ctx
        self._lock = threading.RLock()
        self._mode = "auto"
        self._cookie_file_path = ""
        self._base_url = DEFAULT_BASE_URL
        self._auto_login_on_expire = True
        self._last_res = AuthResolution()
        self._session_expired = False
        self._username = ""
        self._last_auth_error = ""
        self._last_login_prompt_ts = 0.0

    def set_context(self, ctx: PluginContext | None) -> None:
        with self._lock:
            self._ctx = ctx

    def configure(
        self,
        *,
        mode: str = "auto",
        cookie_file_path: str = "",
        base_url: str = DEFAULT_BASE_URL,
        auto_login_on_expire: bool = True,
    ) -> None:
        with self._lock:
            norm_mode = (mode or "auto").strip().lower()
            self._mode = norm_mode or "auto"
            self._cookie_file_path = (cookie_file_path or "").strip()
            clean_base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
            self._base_url = clean_base or DEFAULT_BASE_URL
            self._auto_login_on_expire = bool(auto_login_on_expire)

    def _target_host(self, base_url: str) -> str:
        host = urllib.parse.urlsplit(base_url).hostname or "linux.do"
        return host.lower()

    async def _try_chrome_cookies(self, ctx: PluginContext | None, base_url: str) -> str:
        if ctx is None:
            return ""
        target_url = f"{base_url}/"
        domain = self._target_host(base_url)
        for cap_id in ("web.cookies.v1", "web.embed.v1"):
            if not ctx.capabilities.has(cap_id):
                continue
            res = await ctx.capabilities.call(
                cap_id,
                {
                    "action": "get_cookies",
                    "url": target_url,
                    "domain": domain,
                    "include_subdomains": True,
                },
            )
            if not res.ok or not isinstance(res.data, Mapping):
                continue
            header = ""
            for key in ("cookie_header", "header"):
                val = res.data.get(key)
                if isinstance(val, str) and val.strip():
                    try:
                        header = normalize_cookie_input(val)
                    except LinuxdoError:
                        header = ""
                    break
            if not header and isinstance(res.data.get("cookies"), list):
                entries = parse_json_cookies(res.data["cookies"])
                header = build_cookie_header(entries, target_url)
            if header:
                return header
        return ""

    def _read_vault_cookie(self, ctx: PluginContext | None) -> str:
        if ctx is None:
            return ""
        raw = ctx.get_secret("cookie_header")
        if not raw:
            return ""
        try:
            return normalize_cookie_input(raw)
        except LinuxdoError:
            return ""

    def _persist_vault_snapshot(self, ctx: PluginContext | None, cookie_header: str) -> None:
        if ctx is None or not cookie_header:
            return
        prev = ctx.get_secret("cookie_header") or ""
        if prev.strip() != cookie_header:
            ctx.set_secret("cookie_header", cookie_header)

    def _commit_resolution(self, res: AuthResolution) -> AuthResolution:
        with self._lock:
            self._last_res = res
            if res.has_t_cookie:
                self._session_expired = False
                self._last_auth_error = ""
        return res

    def resolve_cookies_sync(self) -> AuthResolution:
        """Non-blocking synchronous cookie check (Vault/File/last Chrome snapshot) for `channels()`."""
        with self._lock:
            ctx, mode, file_path, base_url, cached = (
                self._ctx,
                self._mode,
                self._cookie_file_path,
                self._base_url,
                self._last_res,
            )
        if mode == "disabled":
            return self._commit_resolution(AuthResolution())
        if mode in {"auto", "plugin-chrome-web"} and cached.cookie_header and cached.has_t_cookie:
            return cached
        if mode in {"auto", "vault"}:
            vault_hdr = self._read_vault_cookie(ctx)
            if vault_hdr:
                return self._commit_resolution(_populate_flags(vault_hdr, "vault"))
        if mode in {"auto", "file"} and file_path:
            try:
                entries = load_cookies_from_file(file_path)
            except LinuxdoError:
                entries = []
            file_hdr = build_cookie_header(entries, f"{base_url}/")
            if file_hdr:
                return self._commit_resolution(_populate_flags(file_hdr, "file"))
        return cached if cached.cookie_header else AuthResolution()

    async def resolve_cookies(self, *, persist_chrome_snapshot: bool = True) -> AuthResolution:
        """Resolve session Cookie header across `web.cookies.v1` -> `SecretVault` -> Cookie file."""
        with self._lock:
            ctx, mode, file_path, base_url = (
                self._ctx,
                self._mode,
                self._cookie_file_path,
                self._base_url,
            )
        if mode == "disabled":
            return self._commit_resolution(AuthResolution())

        try_plugin = mode in {"auto", "plugin-chrome-web"}
        try_vault = mode in {"auto", "vault"}
        try_file = mode in {"auto", "file"}

        chosen_hdr = ""
        chosen_src = "guest"
        chrome_fallback = ""

        if try_plugin:
            chrome_hdr = await self._try_chrome_cookies(ctx, base_url)
            if chrome_hdr:
                if contains_cookie_name(chrome_hdr, "_t") or mode == "plugin-chrome-web":
                    chosen_hdr = chrome_hdr
                    chosen_src = "chrome-web:web.cookies.v1"
                    if persist_chrome_snapshot and (
                        contains_cookie_name(chosen_hdr, "_t")
                        or contains_cookie_name(chosen_hdr, "cf_clearance")
                    ):
                        self._persist_vault_snapshot(ctx, chosen_hdr)
                else:
                    chrome_fallback = chrome_hdr

        if not chosen_hdr and try_vault:
            vault_hdr = self._read_vault_cookie(ctx)
            if vault_hdr:
                if chrome_fallback:
                    vault_hdr = merge_cookie_headers(chrome_fallback, vault_hdr)
                chosen_hdr = vault_hdr
                chosen_src = "vault"
                if persist_chrome_snapshot and chrome_fallback and contains_cookie_name(chosen_hdr, "cf_clearance"):
                    self._persist_vault_snapshot(ctx, chosen_hdr)

        if not chosen_hdr and try_file and file_path:
            entries = load_cookies_from_file(file_path)
            file_hdr = build_cookie_header(entries, f"{base_url}/")
            if file_hdr:
                if chrome_fallback:
                    file_hdr = merge_cookie_headers(chrome_fallback, file_hdr)
                chosen_hdr = file_hdr
                chosen_src = "file"

        if not chosen_hdr and chrome_fallback:
            chosen_hdr = chrome_fallback
            chosen_src = "chrome-web:web.cookies.v1"
            if persist_chrome_snapshot and contains_cookie_name(chosen_hdr, "cf_clearance") and ctx is not None:
                if not (ctx.get_secret("cookie_header") or "").strip():
                    self._persist_vault_snapshot(ctx, chosen_hdr)

        return self._commit_resolution(_populate_flags(chosen_hdr, chosen_src))

    async def sync_from_chrome(self) -> tuple[bool, str, AuthResolution]:
        """Force-sync cookies from Chrome bridge and merge with SecretVault `_t` if needed."""
        with self._lock:
            ctx, base_url = self._ctx, self._base_url
        if ctx is None or (not ctx.capabilities.has("web.cookies.v1") and not ctx.capabilities.has("web.embed.v1")):
            msg = "未检测到 plugin-chrome-web (web.cookies.v1)"
            with self._lock:
                self._last_auth_error = msg
            return False, msg, AuthResolution()

        header = await self._try_chrome_cookies(ctx, base_url)
        if not header:
            msg = "Chrome 中暂无 linux.do 会话 Cookie，请先完成 Cloudflare 验证与账号登录"
            with self._lock:
                self._last_auth_error = msg
            return False, msg, AuthResolution()

        if not contains_cookie_name(header, "_t"):
            prev = self._read_vault_cookie(ctx)
            if contains_cookie_name(prev, "_t"):
                header = merge_cookie_headers(header, prev)

        res = _populate_flags(header, "chrome-web:web.cookies.v1")
        with self._lock:
            self._last_res = res
            if res.has_t_cookie:
                self._session_expired = False
                self._last_auth_error = ""
            else:
                self._last_auth_error = "已同步 Cloudflare/站点 Cookie，但缺少 HttpOnly 登录凭据 _t"
        if res.has_t_cookie or res.has_cf_clearance:
            self._persist_vault_snapshot(ctx, res.cookie_header)

        warning = "" if res.has_t_cookie else "已读取到 linux.do Cookie，但未包含 _t 登录凭证，请先在 Chrome 完成 Linux.do 登录"
        return res.has_t_cookie, warning, res

    async def open_login_window(self, *, force: bool = False) -> tuple[bool, str]:
        """Open managed Chrome `--app` window to Linux.do login page with cooldown."""
        with self._lock:
            ctx, base_url, last_ts = self._ctx, self._base_url, self._last_login_prompt_ts
            now = time.monotonic()
            if not force and last_ts > 0 and (now - last_ts) < _AUTO_LOGIN_COOLDOWN_SEC:
                return False, "已打开或刚提示过登录窗口，请在 Chrome 完成 Cloudflare 验证与登录后刷新"
            if ctx is None or not ctx.capabilities.has("web.embed.v1"):
                msg = "未加载 plugin-chrome-web (web.embed.v1)，无法呼起 Chrome 登录窗口"
                self._last_auth_error = msg
                return False, msg
            self._last_login_prompt_ts = now

        res = await ctx.capabilities.call(
            "web.embed.v1",
            {
                "action": "open",
                "url": f"{base_url}/login",
                "title": "Linux.do 登录（完成验证并登录后保持窗口打开或最小化可加速同源代理）",
                "auto_translate": False,
            },
        )
        if not res.ok:
            msg = res.error or "呼起 Chrome 登录窗失败"
            with self._lock:
                self._last_auth_error = msg
            return False, msg
        return True, ""

    def set_manual_cookie_header(self, raw_header: str) -> AuthResolution:
        """Validate, normalize, and store manual Cookie header in 0600 SecretVault."""
        normalized = normalize_cookie_input(raw_header)
        if not normalized:
            raise LinuxdoError("invalid_argument", "cookie_header is required")
        res = _populate_flags(normalized, "vault")
        with self._lock:
            ctx = self._ctx
            self._last_res = res
            self._session_expired = False
            self._last_auth_error = ""
        if ctx is not None:
            ctx.set_secret("cookie_header", normalized)
        return res

    def logout(self) -> None:
        """Clear in-memory cookie state and delete cookie_header from SecretVault."""
        with self._lock:
            ctx = self._ctx
            self._last_res = AuthResolution()
            self._session_expired = False
            self._username = ""
            self._last_auth_error = ""
        if ctx is not None:
            ctx.set_secret("cookie_header", None)

    def mark_session_expired(self, reason: str) -> None:
        with self._lock:
            self._session_expired = True
            self._last_auth_error = reason

    def mark_authenticated(self, username: str = "") -> None:
        with self._lock:
            self._session_expired = False
            self._last_auth_error = ""
            if username:
                self._username = username

    @property
    def is_session_expired(self) -> bool:
        with self._lock:
            return self._session_expired

    @property
    def auto_login_on_expire(self) -> bool:
        with self._lock:
            return self._auto_login_on_expire

    def secrets_for_redaction(self) -> list[str]:
        """Return cookie header and individual cookie values (len >= 4) for redaction."""
        with self._lock:
            hdr = self._last_res.cookie_header
            ctx = self._ctx
        candidates: list[str] = []
        for raw in (hdr, (ctx.get_secret("cookie_header") if ctx is not None else "") or ""):
            if not raw:
                continue
            candidates.append(raw)
            for pair in raw.split(";"):
                if "=" in pair:
                    val = pair.split("=", 1)[1].strip()
                    if len(val) >= 4:
                        candidates.append(val)
        return list(dict.fromkeys(candidates))

    def safe_summary(self) -> dict[str, Any]:
        """Return non-secret auth status summary safe for UI, logs, and RPC."""
        with self._lock:
            r = self._last_res
            return {
                "mode": self._mode,
                "base_url": self._base_url,
                "cookie_source": r.source_used,
                "has_cookies": bool(r.cookie_header),
                "has_t_cookie": r.has_t_cookie,
                "has_session_cookie": r.has_session_cookie,
                "has_cf_clearance": r.has_cf_clearance,
                "authenticated": r.has_t_cookie and not self._session_expired,
                "session_expired": self._session_expired,
                "username": self._username,
                "last_error": redact_text(self._last_auth_error, self.secrets_for_redaction()),
            }
