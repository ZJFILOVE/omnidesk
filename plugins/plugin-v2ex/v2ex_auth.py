"""Multi-source authentication & SecretVault credential manager for plugin-v2ex."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .v2ex_security import (
        DEFAULT_V2EX_BASE_URL,
        LOOPBACK_HOSTS,
        V2exError,
        extract_host,
        redact_sensitive,
        validate_safe_file_path,
    )
else:
    from v2ex_security import (
        DEFAULT_V2EX_BASE_URL,
        LOOPBACK_HOSTS,
        V2exError,
        extract_host,
        redact_sensitive,
        validate_safe_file_path,
    )

VALID_COOKIE_SOURCES = frozenset({"auto", "plugin-chrome-web", "vault", "file", "disabled"})
_DEFAULT_SUBSCRIBED_NODES = "programmer,python,linux,macos"


def _strip_bearer_prefix(token: str) -> str:
    cleaned = token.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned


def _clean_domain(domain: str) -> str:
    return domain.strip().lower().lstrip(".")


def _domain_matches(host: str, cookie_domain: str, host_only: bool) -> bool:
    h = _clean_domain(host)
    d = _clean_domain(cookie_domain)
    if not h or not d:
        return False
    if h == d:
        return True
    if host_only:
        return False
    return len(h) > len(d) and h.endswith(f".{d}")


def _path_matches(req_path: str, cookie_path: str) -> bool:
    rp = req_path or "/"
    cp = cookie_path or "/"
    if rp == cp:
        return True
    if not rp.startswith(cp):
        return False
    if cp.endswith("/"):
        return True
    return len(rp) > len(cp) and rp[len(cp)] == "/"


def _read_bool(val: Any, fallback: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off"}:
            return False
    return fallback


def _read_timeout(val: Any, fallback: float = 15.0) -> float:
    if isinstance(val, bool):
        return fallback
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        return fallback
    if parsed <= 0:
        return fallback
    return max(0.05, min(120.0, parsed))


@dataclass(frozen=True, slots=True)
class CookieEntry:
    """Single parsed RFC 6265 cookie record."""

    name: str
    value: str
    domain: str
    path: str = "/"
    host_only: bool = False
    secure: bool = False
    http_only: bool = False
    expires: int = 0


@dataclass(slots=True)
class V2exConfig:
    """Normalized non-secret configuration for plugin-v2ex."""

    chrome_cookie_source: str = "auto"
    cookie_file_path: str = ""
    subscribed_nodes: str = _DEFAULT_SUBSCRIBED_NODES
    prefer_chrome_fetch: bool = False
    request_timeout_sec: float = 15.0
    auto_translate: bool = False
    target_lang: str = "zh-CN"


@dataclass(slots=True)
class AuthResolution:
    """Resolved credentials for a specific V2EX request."""

    api_token: str = ""
    cookie_header: str = ""
    cookie_source_used: str = "guest"
    has_api_token: bool = False
    has_a2: bool = False
    has_v2ex_tab: bool = False
    has_pb3_session: bool = False

    @property
    def has_pat(self) -> bool:
        return self.has_api_token and bool(self.api_token)

    @property
    def is_cookie_authenticated(self) -> bool:
        return self.has_a2

    @property
    def is_authenticated(self) -> bool:
        return self.has_pat or self.is_cookie_authenticated

    def active_mode_label(self) -> str:
        if self.has_pat and self.is_cookie_authenticated:
            return "pat+cookie"
        if self.has_pat:
            return "api_v2_pat"
        if self.is_cookie_authenticated:
            return f"cookie:{self.cookie_source_used}"
        return "guest"


def parse_netscape_cookies(text: str) -> list[CookieEntry]:
    """Parse Netscape cookies.txt format (supporting #HttpOnly_ lines)."""
    out: list[CookieEntry] = []
    for raw_line in text.splitlines():
        trimmed = raw_line.strip()
        if not trimmed:
            continue
        http_only = False
        work = trimmed
        if work.startswith("#HttpOnly_"):
            http_only = True
            work = work[10:]
        elif work.startswith("#"):
            continue
        cols = work.split("\t")
        if len(cols) < 7:
            continue
        domain = _clean_domain(cols[0])
        host_only = cols[1].strip().lower() != "true"
        path = cols[2] or "/"
        secure = cols[3].strip().lower() == "true"
        try:
            expires = int(cols[4].strip())
        except ValueError:
            expires = 0
        name = cols[5].strip()
        value = cols[6].strip()
        if name and domain:
            out.append(
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
    return out


def parse_json_cookies(text: str) -> list[CookieEntry]:
    """Parse JSON cookie export (list or {'cookies': [...]}) into CookieEntry items."""
    out: list[CookieEntry] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return out
    arr = parsed if isinstance(parsed, list) else (parsed.get("cookies") if isinstance(parsed, Mapping) else None)
    if not isinstance(arr, list):
        return out

    for item in arr:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        raw_dom = str(item.get("domain") or "")
        if raw_dom.startswith("."):
            host_only = False
        elif isinstance(item.get("hostOnly"), bool):
            host_only = bool(item["hostOnly"])
        else:
            host_only = False
        domain = _clean_domain(raw_dom)
        path = str(item.get("path") or "/") or "/"
        secure = bool(item.get("secure", False))
        http_only = bool(item.get("httpOnly", False))
        expires = 0
        if isinstance(item.get("expirationDate"), (int, float)) and not isinstance(item.get("expirationDate"), bool):
            expires = int(item["expirationDate"])
        elif isinstance(item.get("expires"), (int, float)) and not isinstance(item.get("expires"), bool):
            expires = int(item["expires"])
        elif isinstance(item.get("expires_utc"), int) and item["expires_utc"] > 0:
            expires = int((item["expires_utc"] / 1_000_000.0) - 11644473600)
        if name and domain:
            out.append(
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
    return out


def load_cookies_from_file(path_str: str) -> list[CookieEntry]:
    """Load JSON or Netscape cookies from an explicitly validated local file path."""
    cleaned = path_str.strip()
    if not cleaned:
        return []
    safe_path = validate_safe_file_path(cleaned, must_exist=False)
    if not safe_path.exists():
        return []
    if not safe_path.is_file():
        raise V2exError("path_safety", f"Cookie 路径不是普通文件: {safe_path.name}")
    try:
        content = safe_path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries = parse_json_cookies(content)
    return entries if entries else parse_netscape_cookies(content)


def build_cookie_header(
    entries: Sequence[CookieEntry],
    url: str,
    now_epoch_sec: int | None = None,
) -> str:
    """Filter cookies by RFC 6265 domain/path/secure/expiry rules and format Cookie header."""
    now = int(time.time()) if now_epoch_sec is None else int(now_epoch_sec)
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    is_https = parsed.scheme.lower() == "https"
    req_path = parsed.path or "/"

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

    matched.sort(key=lambda item: len(item.path), reverse=True)
    seen: set[str] = set()
    parts: list[str] = []
    for c in matched:
        if c.name in seen:
            continue
        seen.add(c.name)
        parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


def extract_cookie_value(cookie_header: str, name: str) -> str:
    """Extract a single cookie value by exact name from a Cookie header string."""
    if not cookie_header or not name:
        return ""
    for part in cookie_header.split(";"):
        t = part.strip()
        if "=" not in t:
            continue
        k, v = t.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return ""


def inspect_key_cookies(cookie_header: str) -> tuple[bool, bool, bool]:
    """Return `(has_a2, has_v2ex_tab, has_pb3_session)` from a Cookie header."""
    return (
        bool(extract_cookie_value(cookie_header, "A2")),
        bool(extract_cookie_value(cookie_header, "V2EX_TAB")),
        bool(extract_cookie_value(cookie_header, "PB3_SESSION")),
    )


class AuthManager:
    """Manages V2EX PAT + Cookie priority resolution across web.cookies.v1, SecretVault, and file."""

    def __init__(self, *, allow_loopback_http: bool = False) -> None:
        self._ctx: PluginContext | None = None
        self._allow_loopback_http = allow_loopback_http
        self._config = V2exConfig()
        self._vault_cookie_header = ""
        self._vault_api_token = ""
        self._cached_chrome_cookie_header = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    @property
    def config(self) -> V2exConfig:
        return self._config

    def secret_values(self) -> tuple[str, ...]:
        """Return currently known secret values for log/error redaction."""
        vals: list[str] = []
        for s in (self._vault_api_token, self._vault_cookie_header, self._cached_chrome_cookie_header):
            if s:
                vals.append(s)
        if self._ctx is not None:
            for key in ("api_token", "cookie_header"):
                v = self._ctx.get_secret(key, "") or ""
                if v:
                    vals.append(v)
        return tuple(dict.fromkeys(vals))

    def configure(
        self,
        cfg: V2exConfig,
        *,
        vault_cookie_header: str = "",
        vault_api_token: str = "",
    ) -> None:
        src = (cfg.chrome_cookie_source or "auto").strip().lower()
        if src not in VALID_COOKIE_SOURCES:
            src = "auto"
        self._config = V2exConfig(
            chrome_cookie_source=src,
            cookie_file_path=(cfg.cookie_file_path or "").strip(),
            subscribed_nodes=(cfg.subscribed_nodes or _DEFAULT_SUBSCRIBED_NODES).strip(),
            prefer_chrome_fetch=bool(cfg.prefer_chrome_fetch),
            request_timeout_sec=_read_timeout(cfg.request_timeout_sec, 15.0),
            auto_translate=bool(cfg.auto_translate),
            target_lang=(cfg.target_lang or "zh-CN").strip() or "zh-CN",
        )
        self._vault_cookie_header = vault_cookie_header.strip()
        self._vault_api_token = _strip_bearer_prefix(vault_api_token)

    def load_from_context(self, ctx: PluginContext | None = None) -> None:
        if ctx is not None:
            self._ctx = ctx
        active = self._ctx
        if active is None:
            return
        raw = active.get_config()
        cfg = V2exConfig(
            chrome_cookie_source=str(raw.get("chrome_cookie_source") or "auto"),
            cookie_file_path=str(raw.get("cookie_file_path") or ""),
            subscribed_nodes=str(raw.get("subscribed_nodes") or _DEFAULT_SUBSCRIBED_NODES),
            prefer_chrome_fetch=_read_bool(raw.get("prefer_chrome_fetch"), False),
            request_timeout_sec=_read_timeout(raw.get("request_timeout_sec"), 15.0),
            auto_translate=_read_bool(raw.get("auto_translate"), False),
            target_lang=str(raw.get("target_lang") or "zh-CN"),
        )
        vault_cookie = str(active.get_secret("cookie_header", "") or "")
        vault_token = str(active.get_secret("api_token", "") or "")
        self.configure(cfg, vault_cookie_header=vault_cookie, vault_api_token=vault_token)

    def _cookie_match_url(self, url: str) -> str:
        target = (url or f"{DEFAULT_V2EX_BASE_URL}/").strip()
        if self._allow_loopback_http and extract_host(target) in LOOPBACK_HOSTS:
            parts = urllib.parse.urlsplit(target)
            path = parts.path or "/"
            return f"{DEFAULT_V2EX_BASE_URL}{path}"
        return target

    def _parse_capability_cookies(self, data: Mapping[str, Any], target_url: str) -> str:
        raw_hdr = data.get("cookie_header")
        if isinstance(raw_hdr, str) and raw_hdr.strip():
            return raw_hdr.strip()
        raw_list = data.get("cookies")
        if isinstance(raw_list, list):
            normalized: list[dict[str, Any]] = []
            for item in raw_list:
                if isinstance(item, Mapping):
                    copy_item = dict(item)
                    if not str(copy_item.get("domain") or "").strip():
                        copy_item["domain"] = "v2ex.com"
                    normalized.append(copy_item)
            entries = parse_json_cookies(json.dumps(normalized))
            hdr = build_cookie_header(entries, self._cookie_match_url(target_url))
            if hdr:
                return hdr
        return ""

    async def _try_chrome_cookies(self, target_url: str) -> tuple[str, str, CapabilityResult | None]:
        ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.cookies.v1"):
            return "", "", None
        lookup_url = self._cookie_match_url(target_url)
        res = await ctx.capabilities.call(
            "web.cookies.v1",
            {
                "action": "get_cookies",
                "url": lookup_url,
                "domain": "v2ex.com",
                "include_subdomains": True,
            },
        )
        if res.ok and isinstance(res.data, Mapping):
            hdr = self._parse_capability_cookies(res.data, lookup_url)
            if hdr:
                return hdr, "chrome:web.cookies.v1", res
        return "", "", res

    def _finalize_cookie_resolution(self, res: AuthResolution, header: str, source: str) -> AuthResolution:
        has_a2, has_tab, has_pb3 = inspect_key_cookies(header)
        res.cookie_header = header
        res.cookie_source_used = source
        res.has_a2 = has_a2
        res.has_v2ex_tab = has_tab
        res.has_pb3_session = has_pb3
        return res

    def resolve_sync_snapshot(self, url: str = f"{DEFAULT_V2EX_BASE_URL}/") -> AuthResolution:
        """Non-blocking snapshot resolution using vault, cached Chrome cookies, and local file."""
        if self._ctx is not None:
            self.load_from_context(self._ctx)
        res = AuthResolution()
        pat = self._vault_api_token
        if not pat and self._ctx is not None:
            pat = _strip_bearer_prefix(str(self._ctx.get_secret("api_token", "") or ""))
        if pat:
            res.api_token = pat
            res.has_api_token = True

        src = self._config.chrome_cookie_source
        if src == "disabled":
            return res

        if src in {"auto", "plugin-chrome-web"} and self._cached_chrome_cookie_header:
            return self._finalize_cookie_resolution(res, self._cached_chrome_cookie_header, "chrome:cached")

        if src in {"auto", "vault"}:
            vault_hdr = self._vault_cookie_header
            if not vault_hdr and self._ctx is not None:
                vault_hdr = str(self._ctx.get_secret("cookie_header", "") or "").strip()
            if vault_hdr:
                return self._finalize_cookie_resolution(res, vault_hdr, "vault")

        if src in {"auto", "file"} and self._config.cookie_file_path:
            entries = load_cookies_from_file(self._config.cookie_file_path)
            hdr = build_cookie_header(entries, self._cookie_match_url(url))
            if hdr:
                return self._finalize_cookie_resolution(res, hdr, "file")
        return res

    async def resolve_for_url(self, url: str = f"{DEFAULT_V2EX_BASE_URL}/") -> AuthResolution:
        """Resolve PAT + Cookie header according to `chrome_cookie_source` priority."""
        if self._ctx is not None:
            self.load_from_context(self._ctx)
        res = AuthResolution()
        pat = self._vault_api_token
        if not pat and self._ctx is not None:
            pat = _strip_bearer_prefix(str(self._ctx.get_secret("api_token", "") or ""))
        if pat:
            res.api_token = pat
            res.has_api_token = True

        src = self._config.chrome_cookie_source
        if src == "disabled":
            return res

        if src in {"auto", "plugin-chrome-web"}:
            hdr, used_src, _ = await self._try_chrome_cookies(url)
            if hdr:
                has_a2, _, _ = inspect_key_cookies(hdr)
                if has_a2 or src == "plugin-chrome-web":
                    self._cached_chrome_cookie_header = hdr
                    return self._finalize_cookie_resolution(res, hdr, used_src)
                vault_hdr = self._vault_cookie_header
                if not vault_hdr and self._ctx is not None:
                    vault_hdr = str(self._ctx.get_secret("cookie_header", "") or "").strip()
                if vault_hdr and inspect_key_cookies(vault_hdr)[0]:
                    return self._finalize_cookie_resolution(res, vault_hdr, "vault")
                self._cached_chrome_cookie_header = hdr
                return self._finalize_cookie_resolution(res, hdr, used_src)
            if self._cached_chrome_cookie_header:
                return self._finalize_cookie_resolution(res, self._cached_chrome_cookie_header, "chrome:cached")

        if src in {"auto", "vault"}:
            vault_hdr = self._vault_cookie_header
            if not vault_hdr and self._ctx is not None:
                vault_hdr = str(self._ctx.get_secret("cookie_header", "") or "").strip()
            if vault_hdr:
                return self._finalize_cookie_resolution(res, vault_hdr, "vault")

        if src in {"auto", "file"} and self._config.cookie_file_path:
            entries = load_cookies_from_file(self._config.cookie_file_path)
            hdr = build_cookie_header(entries, self._cookie_match_url(url))
            if hdr:
                return self._finalize_cookie_resolution(res, hdr, "file")

        return res

    async def sync_from_chrome(self) -> tuple[bool, str, str, str]:
        """Sync V2EX cookies from `web.cookies.v1`, fail-closed if A2 or bridge missing."""
        if self._ctx is None or not self._ctx.capabilities.has("web.cookies.v1"):
            return False, "", "capability_not_found", "未检测到 plugin-chrome-web (web.cookies.v1)"
        hdr, source, raw_res = await self._try_chrome_cookies(f"{DEFAULT_V2EX_BASE_URL}/")
        if not hdr:
            err_code = "auth_required"
            err_msg = "Chrome CookieStore 暂无 v2ex.com 登录凭证 (A2)，请先完成登录"
            if raw_res is not None and not raw_res.ok:
                cap_code = str(raw_res.data.get("error_code") or "")
                if cap_code == "cookie_decryption_unavailable":
                    err_code = cap_code
                    err_msg = redact_sensitive(raw_res.error or err_msg, self.secret_values())
                elif cap_code == "path_safety":
                    err_code = cap_code
                    err_msg = redact_sensitive(raw_res.error or err_msg, self.secret_values())
            return False, "", err_code, err_msg

        has_a2, _, _ = inspect_key_cookies(hdr)
        if not has_a2:
            return False, "", "auth_required", "Chrome CookieStore 暂无 v2ex.com 登录凭证 (A2)，请先完成登录"

        self._cached_chrome_cookie_header = hdr
        self.save_vault_cookie_header(hdr)
        return True, source, "", ""

    async def open_login_window(self, title: str = "V2EX 登录（完成后关闭窗口即可）") -> CapabilityResult:
        """Launch managed Chrome login window via `web.embed.v1`, failing closed if absent."""
        if self._ctx is None or not self._ctx.capabilities.has("web.embed.v1"):
            return CapabilityResult.fail(
                "未检测到 plugin-chrome-web (web.embed.v1)，可在插件设置填写 API v2 Token 或 cookie_header",
                error_code="capability_not_found",
            )
        return await self._ctx.capabilities.call(
            "web.embed.v1",
            {
                "action": "open",
                "url": f"{DEFAULT_V2EX_BASE_URL}/signin",
                "title": title,
                "auto_translate": False,
            },
        )

    def save_vault_cookie_header(self, cookie_header: str | None) -> bool:
        trimmed = (cookie_header or "").strip()
        self._vault_cookie_header = trimmed
        if self._ctx is not None:
            self._ctx.set_secret("cookie_header", trimmed or None)
        return True

    def save_vault_api_token(self, api_token: str | None) -> bool:
        cleaned = _strip_bearer_prefix(api_token or "")
        self._vault_api_token = cleaned
        if self._ctx is not None:
            self._ctx.set_secret("api_token", cleaned or None)
        return True

    def clear_credentials(self) -> None:
        """Clear stored V2EX cookies, API token, and cached Chrome cookies."""
        self._cached_chrome_cookie_header = ""
        self._vault_cookie_header = ""
        self._vault_api_token = ""
        if self._ctx is not None:
            self._ctx.set_secret("cookie_header", None)
            self._ctx.set_secret("api_token", None)

    def _status_from_resolution(self, r: AuthResolution) -> dict[str, Any]:
        has_bridge = bool(
            self._ctx is not None
            and (self._ctx.capabilities.has("web.cookies.v1") or self._ctx.capabilities.has("web.embed.v1"))
        )
        has_fetch = bool(
            self._ctx is not None
            and (self._ctx.capabilities.has("web.fetch.v1") or self._ctx.capabilities.has("web.extract.v1"))
        )
        return {
            "authenticated": r.is_authenticated,
            "active_mode": r.active_mode_label(),
            "has_api_token": r.has_pat,
            "cookie_authenticated": r.is_cookie_authenticated,
            "cookie_source": r.cookie_source_used,
            "chrome_cookie_source": self._config.chrome_cookie_source,
            "prefer_chrome_fetch": self._config.prefer_chrome_fetch,
            "auto_translate": self._config.auto_translate,
            "target_lang": self._config.target_lang,
            "request_timeout_sec": self._config.request_timeout_sec,
            "has_a2": r.has_a2,
            "has_v2ex_tab": r.has_v2ex_tab,
            "has_pb3_session": r.has_pb3_session,
            "has_cookie_file": bool(self._config.cookie_file_path),
            "chrome_bridge_available": has_bridge,
            "chrome_fetch_available": has_fetch,
        }

    async def safe_status(self) -> dict[str, Any]:
        """Return secret-free auth & capability availability snapshot."""
        cookie_file_safe = True
        try:
            r = await self.resolve_for_url(f"{DEFAULT_V2EX_BASE_URL}/")
        except V2exError as exc:
            cookie_file_safe = exc.error_code != "path_safety"
            r = AuthResolution(api_token=self._vault_api_token, has_api_token=bool(self._vault_api_token))
        out = self._status_from_resolution(r)
        out["cookie_file_safe"] = cookie_file_safe
        return out

    def safe_status_sync(self) -> dict[str, Any]:
        """Synchronous secret-free status snapshot."""
        cookie_file_safe = True
        try:
            r = self.resolve_sync_snapshot(f"{DEFAULT_V2EX_BASE_URL}/")
        except V2exError as exc:
            cookie_file_safe = exc.error_code != "path_safety"
            r = AuthResolution(api_token=self._vault_api_token, has_api_token=bool(self._vault_api_token))
        out = self._status_from_resolution(r)
        out["cookie_file_safe"] = cookie_file_safe
        return out
