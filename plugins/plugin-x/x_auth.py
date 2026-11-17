"""Session credential resolution, SecretVault persistence, and web.cookies/web.embed bridge."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .x_security import (
        XPluginError,
        has_control_chars,
        read_private_file_text,
        redact_x_secrets,
    )
else:
    from x_security import (  # type: ignore[no-redef]
        XPluginError,
        has_control_chars,
        read_private_file_text,
        redact_x_secrets,
    )

K_LOGIN_URL = "https://x.com/i/flow/login"
K_HOME_URL = "https://x.com/home"
K_ROOT_DOMAIN = "x.com"
_VALID_COOKIE_SOURCES = frozenset({"auto", "plugin-chrome-web", "vault", "file", "disabled"})


@dataclass(slots=True)
class CookieEntry:
    """Single parsed HTTP cookie entry from JSON or Netscape cookies.txt."""

    name: str
    value: str
    domain: str
    path: str = "/"
    host_only: bool = False
    secure: bool = True
    http_only: bool = False
    expires: int = 0


@dataclass(slots=True)
class XConfig:
    """Typed runtime configuration snapshot mapped from plugin.json config_schema."""

    chrome_cookie_source: str = "auto"
    cookie_file_path: str = ""
    prefer_chrome_fetch: bool = True
    auto_translate: bool = True
    target_lang: str = "zh-CN"
    default_list_ids: str = ""
    request_timeout_sec: float = 15.0


@dataclass(slots=True)
class AuthResolution:
    """Resolved X session tokens (`auth_token` + `ct0`) and provenance."""

    cookie_header: str = ""
    auth_token: str = ""
    ct0: str = ""
    twid: str = ""
    user_id: str = ""
    source_used: str = "guest"
    has_auth_token: bool = False
    has_ct0: bool = False
    has_twid: bool = False
    diagnostics: list[str] = field(default_factory=list)

    @property
    def is_authenticated(self) -> bool:
        return self.has_auth_token and self.has_ct0

    def extra_secrets(self) -> tuple[str, ...]:
        return tuple(v for v in (self.cookie_header, self.auth_token, self.ct0, self.twid) if v)


def _strip_quotes(value: str) -> str:
    s = value.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def _clean_domain(domain: str) -> str:
    return domain.strip().lower().lstrip(".")


def _is_x_family_domain(domain: str) -> bool:
    cd = _clean_domain(domain)
    return cd == "x.com" or cd.endswith(".x.com") or cd == "twitter.com" or cd.endswith(".twitter.com")


def _domain_matches(host: str, cookie_domain: str, host_only: bool) -> bool:
    h = _clean_domain(host)
    d = _clean_domain(cookie_domain)
    if not h or not d:
        return False
    if h == d:
        return True
    if host_only:
        return (h == "x.com" and d == "twitter.com") or (h == "twitter.com" and d == "x.com")
    if h.endswith(f".{d}"):
        return True
    if (h == "x.com" or h.endswith(".x.com")) and d == "twitter.com":
        return True
    return False


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


def _read_bool(raw: Any, fallback: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off"}:
            return False
    if isinstance(raw, int):
        return raw != 0
    return fallback


def _read_timeout(raw: Any, fallback: float = 15.0) -> float:
    if isinstance(raw, bool):
        return fallback
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return fallback
    return max(0.1, min(120.0, val))


def extract_cookie_value(cookie_header: str, name: str) -> str:
    """Extract a single cookie value by name from an HTTP Cookie header string."""
    if not cookie_header or not name:
        return ""
    for part in cookie_header.split(";"):
        t = part.strip()
        if "=" not in t:
            continue
        k, v = t.split("=", 1)
        if k.strip() == name:
            return _strip_quotes(v)
    return ""


def extract_user_id_from_twid(twid: str) -> str:
    """Extract numeric X user ID from `twid` cookie (`u%3D12345` or `u=12345`)."""
    if not twid:
        return ""
    s = _strip_quotes(twid)
    low = s.lower()
    if "u%3d" in low:
        idx = low.find("u%3d") + 4
    elif "u=" in s:
        idx = s.find("u=") + 2
    else:
        idx = 0
    digits: list[str] = []
    while idx < len(s) and s[idx].isdigit():
        digits.append(s[idx])
        idx += 1
    return "".join(digits)


def inspect_key_cookies(cookie_header: str) -> tuple[str, str, str, str]:
    """Return `(auth_token, ct0, twid, user_id)` extracted from a Cookie header."""
    auth_token = _strip_quotes(extract_cookie_value(cookie_header, "auth_token"))
    ct0 = _strip_quotes(extract_cookie_value(cookie_header, "ct0"))
    twid = _strip_quotes(extract_cookie_value(cookie_header, "twid"))
    user_id = extract_user_id_from_twid(twid)
    return auth_token, ct0, twid, user_id


def merge_cookie_headers(primary: str, secondary: str) -> str:
    """Merge two Cookie headers, preserving order with `primary` taking precedence."""
    order: list[str] = []
    values: dict[str, str] = {}
    for hdr in (primary, secondary):
        if not hdr:
            continue
        for part in hdr.split(";"):
            t = part.strip()
            if "=" not in t:
                continue
            k, v = t.split("=", 1)
            key = k.strip()
            val = _strip_quotes(v)
            if not key or not val:
                continue
            if key not in values:
                order.append(key)
                values[key] = val
    return "; ".join(f"{k}={values[k]}" for k in order)


def parse_netscape_cookies(text: str) -> list[CookieEntry]:
    """Parse Netscape cookies.txt content, preserving `#HttpOnly_` entries."""
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
        path = cols[2].strip() or "/"
        secure = cols[3].strip().lower() == "true"
        try:
            expires = int(float(cols[4].strip()))
        except ValueError:
            expires = 0
        name = cols[5].strip()
        value = _strip_quotes(cols[6])
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
    """Parse Chrome-exported JSON array or `{"cookies": [...]}` object."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(parsed, list):
        arr = parsed
        fallback_domain = ""
    elif isinstance(parsed, Mapping) and isinstance(parsed.get("cookies"), list):
        arr = parsed["cookies"]
        raw_dom = str(parsed.get("domain") or "")
        fallback_domain = _clean_domain(raw_dom) if _is_x_family_domain(raw_dom) else ""
    else:
        return []

    out: list[CookieEntry] = []
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        value = _strip_quotes(str(item.get("value") or ""))
        raw_dom = str(item.get("domain") or "")
        if raw_dom.startswith("."):
            host_only = False
        elif isinstance(item.get("hostOnly"), bool):
            host_only = bool(item["hostOnly"])
        else:
            host_only = False
        domain = _clean_domain(raw_dom) or fallback_domain
        path = str(item.get("path") or "/") or "/"
        secure = bool(item.get("secure", False))
        http_only = bool(item.get("httpOnly", False))
        expires = 0
        for exp_key in ("expirationDate", "expires"):
            val = item.get(exp_key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                expires = int(val)
                break
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


def build_cookie_header(entries: list[CookieEntry], url: str, now_epoch_sec: int | None = None) -> str:
    """Filter cookies by RFC 6265 domain, path, secure, and expiration for `url`."""
    now = int(time.time()) if now_epoch_sec is None else now_epoch_sec
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

    matched.sort(key=lambda c: (-len(c.path), 0 if _clean_domain(c.domain) == "x.com" else 1))
    seen: set[str] = set()
    parts: list[str] = []
    for c in matched:
        if c.name in seen:
            continue
        seen.add(c.name)
        parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


class AuthManager:
    """Manages X session credentials across web.cookies.v1, SecretVault, and cookie files."""

    def __init__(self, ctx: PluginContext | None = None) -> None:
        self._ctx = ctx
        self._config = XConfig()
        self._vault_cookie_header = ""
        self._vault_auth_token = ""
        self._vault_ct0 = ""
        self._cached_chrome_cookie_header = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    @property
    def config(self) -> XConfig:
        return self._config

    def configure(
        self,
        cfg: XConfig,
        *,
        vault_cookie_header: str = "",
        vault_auth_token: str = "",
        vault_ct0: str = "",
    ) -> None:
        self._config = cfg
        if vault_cookie_header:
            self._vault_cookie_header = vault_cookie_header.strip()
        if vault_auth_token:
            self._vault_auth_token = vault_auth_token.strip()
        if vault_ct0:
            self._vault_ct0 = vault_ct0.strip()

    def load_from_context(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        cfg_map = ctx.get_config()
        raw_src = str(cfg_map.get("chrome_cookie_source") or "auto").strip().lower()
        cookie_src = raw_src if raw_src in _VALID_COOKIE_SOURCES else "auto"
        target_lang = str(cfg_map.get("target_lang") or "zh-CN").strip() or "zh-CN"
        self._config = XConfig(
            chrome_cookie_source=cookie_src,
            cookie_file_path=str(cfg_map.get("cookie_file_path") or "").strip(),
            prefer_chrome_fetch=_read_bool(cfg_map.get("prefer_chrome_fetch"), True),
            auto_translate=_read_bool(cfg_map.get("auto_translate"), True),
            target_lang=target_lang,
            default_list_ids=str(cfg_map.get("default_list_ids") or "").strip(),
            request_timeout_sec=_read_timeout(cfg_map.get("request_timeout_sec"), 15.0),
        )
        self._vault_cookie_header = str(ctx.get_secret("cookie_header", "") or "").strip()
        self._vault_auth_token = str(ctx.get_secret("auth_token", "") or "").strip()
        self._vault_ct0 = str(ctx.get_secret("ct0", "") or "").strip()

    def extra_secrets(self) -> tuple[str, ...]:
        res = self.resolve_cached_sync()
        items = {
            self._vault_cookie_header,
            self._vault_auth_token,
            self._vault_ct0,
            self._cached_chrome_cookie_header,
            *res.extra_secrets(),
        }
        return tuple(s for s in items if s)

    def _populate_resolution(self, header: str, source: str) -> AuthResolution:
        clean = header.strip()
        auth_tok, ct0_tok, twid_tok, uid = inspect_key_cookies(clean)
        return AuthResolution(
            cookie_header=clean,
            auth_token=auth_tok,
            ct0=ct0_tok,
            twid=twid_tok,
            user_id=uid,
            source_used=source,
            has_auth_token=bool(auth_tok),
            has_ct0=bool(ct0_tok),
            has_twid=bool(twid_tok),
        )

    def _build_vault_header(self) -> str:
        pieces: list[str] = []
        if self._vault_auth_token:
            pieces.append(f"auth_token={self._vault_auth_token}")
        if self._vault_ct0:
            pieces.append(f"ct0={self._vault_ct0}")
        return merge_cookie_headers(self._vault_cookie_header, "; ".join(pieces))

    def _try_vault(self) -> AuthResolution:
        combined = self._build_vault_header()
        if not combined:
            return AuthResolution()
        return self._populate_resolution(combined, "vault")

    def _try_file(self, url: str) -> AuthResolution:
        path = self._config.cookie_file_path
        if not path:
            return AuthResolution()
        content = read_private_file_text(path)
        entries = parse_json_cookies(content) or parse_netscape_cookies(content)
        if not entries:
            return AuthResolution()
        hdr = build_cookie_header(entries, url or "https://x.com/")
        if not hdr:
            return AuthResolution()
        return self._populate_resolution(hdr, "file")

    async def _try_chrome_capability(self, url: str, domain: str) -> str:
        ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.cookies.v1"):
            return ""
        res = await ctx.capabilities.call(
            "web.cookies.v1",
            {
                "action": "get_cookies",
                "url": url,
                "domain": domain,
                "include_subdomains": True,
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            return ""
        header = str(res.data.get("cookie_header") or "").strip()
        if isinstance(res.data.get("cookies"), list):
            entries = parse_json_cookies(json.dumps(dict(res.data)))
            built = build_cookie_header(entries, url)
            header = merge_cookie_headers(built, header)
        return header

    async def _try_chrome(self, url: str) -> AuthResolution:
        target_url = url or "https://x.com/"
        hdr_x = await self._try_chrome_capability(target_url, K_ROOT_DOMAIN)
        cap_used = "plugin-chrome-web:web.cookies.v1" if hdr_x else ""
        auth_tok, ct0_tok, _, _ = inspect_key_cookies(hdr_x)
        if not auth_tok or not ct0_tok:
            hdr_tw = await self._try_chrome_capability("https://twitter.com/", "twitter.com")
            if hdr_tw:
                hdr_x = merge_cookie_headers(hdr_x, hdr_tw)
                if not cap_used:
                    cap_used = "plugin-chrome-web:web.cookies.v1"
        if not hdr_x and self._cached_chrome_cookie_header:
            hdr_x = self._cached_chrome_cookie_header
            cap_used = "plugin-chrome-web:cached"
        if not hdr_x:
            return AuthResolution()
        self._cached_chrome_cookie_header = hdr_x
        return self._populate_resolution(hdr_x, cap_used)

    def resolve_cached_sync(self, url: str = "https://x.com/") -> AuthResolution:
        """Non-blocking synchronous resolution for `TextSource.channels()` and diagnostics."""
        mode = self._config.chrome_cookie_source
        if mode == "disabled":
            return AuthResolution(source_used="disabled")
        if mode in {"auto", "plugin-chrome-web"} and self._cached_chrome_cookie_header:
            res = self._populate_resolution(self._cached_chrome_cookie_header, "plugin-chrome-web:cached")
            if res.is_authenticated or mode == "plugin-chrome-web":
                return res
        if mode in {"auto", "vault"}:
            vres = self._try_vault()
            if vres.is_authenticated or (mode == "vault" and vres.cookie_header):
                return vres
        if mode in {"auto", "file"} and self._config.cookie_file_path:
            try:
                fres = self._try_file(url)
                if fres.is_authenticated or (mode == "file" and fres.cookie_header):
                    return fres
            except XPluginError:
                pass
        return AuthResolution(source_used="guest")

    async def resolve_for_url(self, url: str = "https://x.com/") -> AuthResolution:
        """Resolve X session cookies asynchronously following `chrome_cookie_source` priority."""
        mode = self._config.chrome_cookie_source
        if mode == "disabled":
            return AuthResolution(source_used="disabled")
        if mode == "plugin-chrome-web":
            res = await self._try_chrome(url)
            return res if res.cookie_header else AuthResolution(source_used="guest")
        if mode == "vault":
            res = self._try_vault()
            return res if res.cookie_header else AuthResolution(source_used="guest")
        if mode == "file":
            res = self._try_file(url)
            return res if res.cookie_header else AuthResolution(source_used="guest")

        chrome_res = await self._try_chrome(url)
        if chrome_res.is_authenticated:
            return chrome_res
        vault_res = self._try_vault()
        if vault_res.is_authenticated:
            if chrome_res.cookie_header:
                return self._populate_resolution(
                    merge_cookie_headers(chrome_res.cookie_header, vault_res.cookie_header),
                    f"{chrome_res.source_used}+vault",
                )
            return vault_res
        file_res = self._try_file(url)
        if file_res.is_authenticated:
            return file_res

        merged = merge_cookie_headers(chrome_res.cookie_header, vault_res.cookie_header)
        merged = merge_cookie_headers(merged, file_res.cookie_header)
        if merged:
            src = (
                chrome_res.source_used
                if chrome_res.cookie_header
                else ("vault" if vault_res.cookie_header else "file")
            )
            return self._populate_resolution(merged, src)
        return AuthResolution(source_used="guest")

    async def sync_from_chrome(self) -> tuple[bool, str, str, str]:
        """Pull X session cookies via `web.cookies.v1` and persist into `0600` SecretVault."""
        ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.cookies.v1"):
            return False, "", "service_offline", "未加载 plugin-chrome-web (web.cookies.v1)"

        hdr_x = await self._try_chrome_capability("https://x.com/", K_ROOT_DOMAIN)
        hdr_tw = await self._try_chrome_capability("https://twitter.com/", "twitter.com")
        merged = merge_cookie_headers(hdr_x, hdr_tw)
        if not merged:
            return False, "", "auth_required", "未能从 plugin-chrome-web 同步到 x.com 会话 Cookie，请先登录 X"

        auth_token, ct0, _, _ = inspect_key_cookies(merged)
        self._cached_chrome_cookie_header = merged
        self.save_vault_cookie_header(merged)
        if auth_token or ct0:
            self.save_vault_tokens(auth_token, ct0)

        source = "plugin-chrome-web:web.cookies.v1"
        if not auth_token or not ct0:
            return False, source, "auth_required", "已同步 x.com Cookie，但尚未检测到完整 auth_token / ct0 登录凭证"
        return True, source, "", ""

    async def open_login_window(self, title: str = "") -> CapabilityResult:
        """Open managed Chrome `--app` login window via `web.embed.v1`."""
        ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.embed.v1"):
            msg = "未加载 plugin-chrome-web (web.embed.v1)"
            return CapabilityResult.fail(msg, error_code="service_offline", ok=False, error=msg)
        clean_title = (title or "").strip() or "X 登录（完成后关闭窗口即可）"
        res = await ctx.capabilities.call(
            "web.embed.v1",
            {
                "action": "open",
                "url": K_LOGIN_URL,
                "title": clean_title,
                "auto_translate": False,
            },
        )
        if not res.ok:
            safe_err = redact_x_secrets(res.error or "打开 Chrome 登录窗口失败", self.extra_secrets())
            code = str(res.data.get("error_code") or "service_offline")
            return CapabilityResult.fail(safe_err, data=dict(res.data), error_code=code)
        return CapabilityResult.ok({**res.data, "ok": True})

    def save_vault_cookie_header(self, cookie_header: str) -> bool:
        """Persist raw Cookie header and extracted key tokens into `0600` SecretVault."""
        if not isinstance(cookie_header, str) or has_control_chars(cookie_header):
            raise XPluginError("invalid_argument", "cookie_header 包含非法控制字符")
        trimmed = cookie_header.strip()
        auth_tok, ct0_tok, _, _ = inspect_key_cookies(trimmed)
        self._vault_cookie_header = trimmed
        if auth_tok:
            self._vault_auth_token = auth_tok
        if ct0_tok:
            self._vault_ct0 = ct0_tok
        if self._ctx is not None:
            self._ctx.set_secret("cookie_header", trimmed)
            if auth_tok:
                self._ctx.set_secret("auth_token", auth_tok)
            if ct0_tok:
                self._ctx.set_secret("ct0", ct0_tok)
        return True

    def save_vault_tokens(self, auth_token: str, ct0: str) -> bool:
        """Persist `auth_token` and `ct0` tokens into `0600` SecretVault."""
        for label, val in (("auth_token", auth_token), ("ct0", ct0)):
            if not isinstance(val, str) or has_control_chars(val):
                raise XPluginError("invalid_argument", f"{label} 包含非法控制字符")
        clean_auth = auth_token.strip()
        clean_ct0 = ct0.strip()
        if clean_auth:
            self._vault_auth_token = clean_auth
        if clean_ct0:
            self._vault_ct0 = clean_ct0
        parts: list[str] = []
        if self._vault_auth_token:
            parts.append(f"auth_token={self._vault_auth_token}")
        if self._vault_ct0:
            parts.append(f"ct0={self._vault_ct0}")
        self._vault_cookie_header = merge_cookie_headers("; ".join(parts), self._vault_cookie_header)
        if self._ctx is not None:
            if clean_auth:
                self._ctx.set_secret("auth_token", clean_auth)
            if clean_ct0:
                self._ctx.set_secret("ct0", clean_ct0)
            if self._vault_cookie_header:
                self._ctx.set_secret("cookie_header", self._vault_cookie_header)
        return True

    def safe_status(self, resolved: AuthResolution | None = None) -> dict[str, Any]:
        """Return diagnostic status without exposing secret tokens or raw cookie headers."""
        r = resolved if resolved is not None else self.resolve_cached_sync("https://x.com/")
        cfg = self._config
        caps = self._ctx.capabilities if self._ctx is not None else None
        return {
            "authenticated": r.is_authenticated,
            "cookie_source": r.source_used,
            "chrome_cookie_source": cfg.chrome_cookie_source,
            "prefer_chrome_fetch": cfg.prefer_chrome_fetch,
            "auto_translate": cfg.auto_translate,
            "target_lang": cfg.target_lang,
            "login_url": K_LOGIN_URL,
            "has_auth_token": r.has_auth_token,
            "has_ct0": r.has_ct0,
            "has_twid": r.has_twid,
            "user_id": r.user_id,
            "has_cookie_file": bool(cfg.cookie_file_path),
            "chrome_bridge_available": bool(
                caps and (caps.has("web.cookies.v1") or caps.has("web.embed.v1"))
            ),
            "chrome_fetch_available": bool(caps and caps.has("web.fetch.v1")),
            "chrome_extract_available": bool(caps and caps.has("web.extract.v1")),
        }

    async def safe_status_async(self) -> dict[str, Any]:
        """Async version of `safe_status` resolving live `web.cookies.v1` when active."""
        try:
            res = await self.resolve_for_url("https://x.com/")
        except XPluginError:
            res = self.resolve_cached_sync("https://x.com/")
        return self.safe_status(res)
