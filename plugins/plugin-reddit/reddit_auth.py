"""Multi-source Reddit authentication, Cookie/OAuth resolution, and SecretVault integration."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .reddit_security import (
        DEFAULT_REDDIT_USER_AGENT,
        RedditPluginError,
        has_control_chars,
        normalize_user_agent,
        redact_reddit_secrets,
        validate_safe_file_path,
    )
else:
    from reddit_security import (
        DEFAULT_REDDIT_USER_AGENT,
        RedditPluginError,
        has_control_chars,
        normalize_user_agent,
        redact_reddit_secrets,
        validate_safe_file_path,
    )

REDDIT_LOGIN_URL = "https://www.reddit.com/login"
_COOKIE_KEYS = ("reddit_session", "token_v2", "loid")


@dataclass(slots=True)
class CookieEntry:
    """Normalized RFC 6265 cookie record parsed from Chrome, JSON, or Netscape files."""

    name: str
    value: str
    domain: str
    path: str = "/"
    host_only: bool = False
    secure: bool = False
    http_only: bool = False
    expires: int = 0


@dataclass(slots=True)
class RedditConfig:
    """Validated runtime configuration loaded from ConfigStore."""

    chrome_cookie_source: str = "auto"
    cookie_file_path: str = ""
    prefer_chrome_fetch: bool = False
    auto_translate: bool = False
    target_lang: str = "zh-CN"
    request_timeout_sec: float = 15.0
    subreddits: str = "LocalLLaMA,cpp,rust,programming"
    user_agent: str = DEFAULT_REDDIT_USER_AGENT


@dataclass(slots=True)
class AuthResolution:
    """Resolved credentials and source provenance for an outgoing Reddit request."""

    cookie_header: str = ""
    oauth_bearer_token: str = ""
    source_used: str = "guest"
    has_reddit_session: bool = False
    has_token_v2: bool = False
    has_loid: bool = False

    def is_authenticated(self) -> bool:
        return self.has_reddit_session or self.has_token_v2 or bool(self.oauth_bearer_token)

    def extra_secrets(self) -> tuple[str, ...]:
        secrets: list[str] = []
        if self.cookie_header:
            secrets.append(self.cookie_header)
        if self.oauth_bearer_token:
            secrets.append(self.oauth_bearer_token)
        for key in _COOKIE_KEYS:
            val = extract_cookie_value(self.cookie_header, key)
            if val:
                secrets.append(val)
        return tuple(secrets)


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
    if cp.endswith("/"):
        return True
    return len(rp) > len(cp) and rp[len(cp)] == "/"


def sanitize_cookie_header(raw: str | None) -> str:
    """Strip 'Cookie:' prefix and control characters from a Cookie header string."""
    s = str(raw or "").strip()
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    return s.replace("\r", "").replace("\n", "").replace("\x00", "").strip()


def sanitize_bearer_token(raw: str | None) -> str:
    """Strip 'Authorization: Bearer ' prefix and whitespace from an OAuth token."""
    s = str(raw or "").strip()
    if s.lower().startswith("authorization:"):
        s = s[14:].strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return "".join(ch for ch in s if not ch.isspace() and ch != "\x00")


def extract_cookie_value(cookie_header: str, name: str) -> str:
    """Extract a named cookie value from a Cookie header string."""
    if not cookie_header or not name:
        return ""
    for part in cookie_header.split(";"):
        kv = part.strip()
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        if k.strip() == name:
            val = v.strip()
            if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return val
    return ""


def inspect_key_cookies(cookie_header: str) -> tuple[bool, bool, bool]:
    """Return `(has_reddit_session, has_token_v2, has_loid)` flags."""
    return (
        bool(extract_cookie_value(cookie_header, "reddit_session")),
        bool(extract_cookie_value(cookie_header, "token_v2")),
        bool(extract_cookie_value(cookie_header, "loid")),
    )


def parse_netscape_cookies(text: str) -> list[CookieEntry]:
    """Parse Netscape/curl `cookies.txt` text including `#HttpOnly_` entries."""
    out: list[CookieEntry] = []
    for raw_line in text.splitlines():
        trimmed = raw_line.rstrip("\r\n").strip()
        if not trimmed:
            continue
        http_only = False
        if trimmed.startswith("#HttpOnly_"):
            http_only = True
            trimmed = trimmed[10:]
        elif trimmed.startswith("#"):
            continue
        fields = trimmed.split("\t")
        if len(fields) < 7:
            continue
        raw_dom = fields[0].strip()
        include_sub = fields[1].strip().lower()
        domain = _clean_domain(raw_dom)
        host_only = (not raw_dom.startswith(".")) and (include_sub != "true")
        path = fields[2].strip() or "/"
        secure = fields[3].strip().lower() == "true"
        try:
            expires = int(float(fields[4].strip()))
        except ValueError:
            expires = 0
        name = fields[5].strip()
        value = fields[6].strip()
        if name and domain and not has_control_chars(name) and not has_control_chars(value):
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


def parse_json_cookies(data: str | Mapping[str, Any] | Sequence[Any]) -> list[CookieEntry]:
    """Parse JSON cookie arrays or `{cookies: [...]}` objects from Chrome or `web.cookies.v1`."""
    if isinstance(data, str):
        try:
            root = json.loads(data)
        except json.JSONDecodeError:
            return []
    else:
        root = data
    if isinstance(root, list):
        arr = root
    elif isinstance(root, Mapping) and isinstance(root.get("cookies"), list):
        arr = root["cookies"]
    else:
        return []

    out: list[CookieEntry] = []
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        value = "" if item.get("value") is None else str(item.get("value"))
        raw_dom = str(item.get("domain") or item.get("host_key") or "reddit.com").strip()
        domain = _clean_domain(raw_dom)
        if not name or not domain or has_control_chars(name) or has_control_chars(value):
            continue
        if isinstance(item.get("hostOnly"), bool):
            host_only = item["hostOnly"]
        elif isinstance(item.get("host_only"), bool):
            host_only = item["host_only"]
        else:
            host_only = bool(raw_dom) and not raw_dom.startswith(".")
        path = str(item.get("path") or "/").strip() or "/"
        secure = bool(item.get("secure", item.get("is_secure", False)))
        http_only = bool(item.get("httpOnly", item.get("http_only", item.get("is_httponly", False))))
        expires = 0
        for exp_key in ("expirationDate", "expires", "expiry"):
            raw_exp = item.get(exp_key)
            if isinstance(raw_exp, (int, float)) and not isinstance(raw_exp, bool):
                expires = int(raw_exp)
                break
        if expires == 0:
            raw_utc = item.get("expires_utc")
            if isinstance(raw_utc, (int, float)) and not isinstance(raw_utc, bool) and raw_utc > 11644473600000000:
                expires = int((float(raw_utc) / 1_000_000.0) - 11644473600.0)
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


def build_cookie_header(entries: Sequence[CookieEntry], url: str, now_epoch_sec: int | None = None) -> str:
    """Filter cookies per RFC 6265 domain/path/secure/expiry and format HTTP Cookie header."""
    parsed = urllib.parse.urlsplit(url or "https://www.reddit.com/")
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    is_https = parsed.scheme.lower() == "https"
    req_path = parsed.path or "/"
    now = int(time.time()) if now_epoch_sec is None else int(now_epoch_sec)

    matched: list[CookieEntry] = []
    for c in entries:
        if not c.name:
            continue
        if c.secure and not is_https:
            continue
        if c.expires > 0 and now > 0 and c.expires <= now:
            continue
        if not _domain_matches(host, c.domain, c.host_only):
            continue
        if not _path_matches(req_path, c.path):
            continue
        matched.append(c)

    matched.sort(key=lambda c: len(c.path), reverse=True)
    seen: set[str] = set()
    parts: list[str] = []
    for c in matched:
        if c.name in seen:
            continue
        seen.add(c.name)
        parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


def load_cookies_from_file(raw_path: str) -> list[CookieEntry]:
    """Safely load JSON or Netscape cookies from a validated non-symlink file path."""
    if not str(raw_path or "").strip():
        return []
    safe_path = validate_safe_file_path(raw_path, require_regular=True)
    if not safe_path.is_file():
        return []
    content = safe_path.read_text(encoding="utf-8", errors="replace")
    stripped = content.strip()
    if not stripped:
        return []
    if stripped.startswith(("[", "{")):
        parsed = parse_json_cookies(stripped)
        if parsed:
            return parsed
    return parse_netscape_cookies(content)


def _as_bool(val: Any, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"true", "1", "yes"}:
            return True
        if low in {"false", "0", "no"}:
            return False
    if isinstance(val, int):
        return val != 0
    return default


def _as_timeout(val: Any, default: float = 15.0) -> float:
    if isinstance(val, bool):
        return default
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    return max(0.1, min(120.0, num))


class AuthManager:
    """Manages Reddit config, multi-source cookie resolution, Vault secrets, and Chrome auth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ctx: PluginContext | None = None
        self._config = RedditConfig()
        self._vault_cookie_header = ""
        self._vault_oauth_token = ""
        self._cached_chrome_cookie_header = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        with self._lock:
            self._ctx = ctx

    def configure(
        self,
        cfg: RedditConfig,
        *,
        vault_cookie_header: str = "",
        vault_oauth_token: str = "",
    ) -> None:
        with self._lock:
            cfg.chrome_cookie_source = (cfg.chrome_cookie_source or "auto").strip() or "auto"
            cfg.request_timeout_sec = _as_timeout(cfg.request_timeout_sec, 15.0)
            cfg.user_agent = normalize_user_agent(cfg.user_agent)
            cfg.target_lang = (cfg.target_lang or "zh-CN").strip() or "zh-CN"
            self._config = cfg
            if vault_cookie_header:
                self._vault_cookie_header = sanitize_cookie_header(vault_cookie_header)
            if vault_oauth_token:
                self._vault_oauth_token = sanitize_bearer_token(vault_oauth_token)

    def load_from_context(self, ctx: PluginContext) -> None:
        """Read config from ConfigStore and credentials strictly from 0600 SecretVault."""
        raw_cfg = ctx.get_config()
        subreddits = str(
            raw_cfg.get("subreddits")
            or raw_cfg.get("pinned_subreddits")
            or "LocalLLaMA,cpp,rust,programming"
        ).strip()
        cfg = RedditConfig(
            chrome_cookie_source=str(raw_cfg.get("chrome_cookie_source") or "auto").strip() or "auto",
            cookie_file_path=str(raw_cfg.get("cookie_file_path") or "").strip(),
            prefer_chrome_fetch=_as_bool(raw_cfg.get("prefer_chrome_fetch"), False),
            auto_translate=_as_bool(raw_cfg.get("auto_translate"), False),
            target_lang=str(raw_cfg.get("target_lang") or "zh-CN").strip() or "zh-CN",
            request_timeout_sec=_as_timeout(raw_cfg.get("request_timeout_sec"), 15.0),
            subreddits=subreddits,
            user_agent=normalize_user_agent(str(raw_cfg.get("user_agent") or DEFAULT_REDDIT_USER_AGENT)),
        )
        vault_cookie = ctx.get_secret("cookie_header") or ctx.get_secret("reddit_cookie_header") or ""
        vault_oauth = ctx.get_secret("oauth_token") or ctx.get_secret("access_token") or ""
        with self._lock:
            self._ctx = ctx
            self._config = cfg
            self._vault_cookie_header = sanitize_cookie_header(vault_cookie)
            self._vault_oauth_token = sanitize_bearer_token(vault_oauth)

    def config(self) -> RedditConfig:
        with self._lock:
            c = self._config
            return RedditConfig(
                chrome_cookie_source=c.chrome_cookie_source,
                cookie_file_path=c.cookie_file_path,
                prefer_chrome_fetch=c.prefer_chrome_fetch,
                auto_translate=c.auto_translate,
                target_lang=c.target_lang,
                request_timeout_sec=c.request_timeout_sec,
                subreddits=c.subreddits,
                user_agent=c.user_agent,
            )

    def secret_values(self) -> tuple[str, ...]:
        with self._lock:
            raw = [self._vault_cookie_header, self._vault_oauth_token, self._cached_chrome_cookie_header]
        secrets: list[str] = []
        for item in raw:
            if item:
                secrets.append(item)
                for key in _COOKIE_KEYS:
                    val = extract_cookie_value(item, key)
                    if val:
                        secrets.append(val)
        return tuple(secrets)

    def _finalize(self, hdr: str, src: str, oauth: str) -> AuthResolution:
        clean = sanitize_cookie_header(hdr)
        has_sess, has_t2, has_loid = inspect_key_cookies(clean)
        return AuthResolution(
            cookie_header=clean,
            oauth_bearer_token=oauth,
            source_used=src,
            has_reddit_session=has_sess,
            has_token_v2=has_t2,
            has_loid=has_loid,
        )

    def _resolve_file_or_vault_sync(
        self,
        *,
        mode: str,
        cfg: RedditConfig,
        vault_hdr: str,
        vault_oauth: str,
        cached_chrome: str,
        target_url: str,
    ) -> AuthResolution:
        if mode == "plugin-chrome-web" and cached_chrome:
            return self._finalize(cached_chrome, "plugin-chrome-web:cached", vault_oauth)
        if mode in {"auto", "vault"}:
            if vault_hdr:
                return self._finalize(vault_hdr, "vault", vault_oauth)
            if cached_chrome:
                return self._finalize(cached_chrome, "plugin-chrome-web:cached", vault_oauth)
        if mode in {"auto", "file"} and cfg.cookie_file_path:
            entries = load_cookies_from_file(cfg.cookie_file_path)
            file_hdr = build_cookie_header(entries, target_url)
            if file_hdr:
                return self._finalize(file_hdr, "file", vault_oauth)
        if vault_oauth:
            return AuthResolution(oauth_bearer_token=vault_oauth, source_used="vault:oauth")
        return AuthResolution(source_used="guest")

    def resolve_cached_sync(self, url: str = "https://www.reddit.com/") -> AuthResolution:
        """Non-blocking synchronous resolution for `channels()` and `on_health_check()`."""
        with self._lock:
            cfg = self.config()
            vault_hdr = self._vault_cookie_header
            vault_oauth = self._vault_oauth_token
            cached_chrome = self._cached_chrome_cookie_header
        mode = cfg.chrome_cookie_source.strip().lower()
        if mode in {"disabled", "none", "guest"}:
            return AuthResolution(source_used="guest")
        if mode in {"auto", "plugin-chrome-web", "chrome"} and cached_chrome:
            return self._finalize(cached_chrome, "plugin-chrome-web:cached", vault_oauth)
        try:
            return self._resolve_file_or_vault_sync(
                mode=mode,
                cfg=cfg,
                vault_hdr=vault_hdr,
                vault_oauth=vault_oauth,
                cached_chrome=cached_chrome,
                target_url=url or "https://www.reddit.com/",
            )
        except RedditPluginError:
            return AuthResolution(oauth_bearer_token=vault_oauth, source_used="vault:oauth" if vault_oauth else "guest")

    async def _try_chrome_cookie_cap(self, cap_id: str, url: str) -> str:
        with self._lock:
            ctx = self._ctx
        if ctx is None or not ctx.capabilities.has(cap_id):
            return ""
        res = await ctx.capabilities.call(
            cap_id,
            {
                "action": "get_cookies",
                "url": url or "https://www.reddit.com/",
                "domain": "reddit.com",
                "include_subdomains": True,
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            return ""
        if isinstance(res.data.get("cookies"), list):
            entries = parse_json_cookies(res.data)
            built = build_cookie_header(entries, url or "https://www.reddit.com/")
            if built:
                return built
        raw_hdr = res.data.get("cookie_header")
        if isinstance(raw_hdr, str):
            clean = sanitize_cookie_header(raw_hdr)
            if clean:
                return clean
        return ""

    async def resolve_for_url(self, url: str = "https://www.reddit.com/") -> AuthResolution:
        """Resolve credentials per configured `chrome_cookie_source` priority."""
        with self._lock:
            cfg = self.config()
            vault_hdr = self._vault_cookie_header
            vault_oauth = self._vault_oauth_token
            cached_chrome = self._cached_chrome_cookie_header
            ctx = self._ctx

        mode = cfg.chrome_cookie_source.strip().lower()
        if mode in {"disabled", "none", "guest"}:
            return AuthResolution(source_used="guest")

        target_url = url or "https://www.reddit.com/"
        if mode in {"auto", "plugin-chrome-web", "chrome"}:
            chrome_hdr = await self._try_chrome_cookie_cap("web.cookies.v1", target_url)
            if not chrome_hdr:
                chrome_hdr = await self._try_chrome_cookie_cap("web.embed.v1", target_url)
            if chrome_hdr:
                c_sess, c_t2, _ = inspect_key_cookies(chrome_hdr)
                v_sess, v_t2, _ = inspect_key_cookies(vault_hdr)
                if mode == "auto" and not (c_sess or c_t2) and (v_sess or v_t2):
                    return self._finalize(vault_hdr, "vault", vault_oauth)
                with self._lock:
                    self._cached_chrome_cookie_header = chrome_hdr
                    if not self._vault_cookie_header or c_sess or c_t2:
                        self._vault_cookie_header = chrome_hdr
                if ctx is not None and (c_sess or c_t2 or not vault_hdr):
                    ctx.set_secret("cookie_header", chrome_hdr)
                return self._finalize(chrome_hdr, "plugin-chrome-web", vault_oauth)

        return self._resolve_file_or_vault_sync(
            mode=mode,
            cfg=cfg,
            vault_hdr=vault_hdr,
            vault_oauth=vault_oauth,
            cached_chrome=cached_chrome,
            target_url=target_url,
        )

    async def sync_from_chrome(self) -> tuple[str, AuthResolution]:
        """Synchronize Reddit cookies from `plugin-chrome-web`, failing closed if unauthenticated."""
        src = ""
        hdr = await self._try_chrome_cookie_cap("web.cookies.v1", "https://www.reddit.com/")
        if hdr:
            src = "web.cookies.v1"
        else:
            hdr = await self._try_chrome_cookie_cap("web.embed.v1", "https://www.reddit.com/")
            if hdr:
                src = "web.embed.v1"
        if not hdr:
            raise RedditPluginError(
                "auth_required",
                "未能从 plugin-chrome-web 同步到 reddit.com Cookie，请先完成登录",
            )
        has_sess, has_t2, has_loid = inspect_key_cookies(hdr)
        if not (has_sess or has_t2):
            raise RedditPluginError(
                "auth_required",
                "Chrome 中未检测到已登录的 Reddit 会话（缺少 reddit_session 或 token_v2）",
            )
        self.save_vault_cookie_header(hdr)
        with self._lock:
            self._cached_chrome_cookie_header = hdr
            oauth = self._vault_oauth_token
        return src, AuthResolution(
            cookie_header=hdr,
            oauth_bearer_token=oauth,
            source_used="plugin-chrome-web",
            has_reddit_session=has_sess,
            has_token_v2=has_t2,
            has_loid=has_loid,
        )

    async def open_login_window(self, title: str = "Reddit 登录（完成后关闭窗口即可）") -> CapabilityResult:
        """Open managed Chrome `--app` login window via `web.embed.v1`."""
        with self._lock:
            ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.embed.v1"):
            return CapabilityResult.fail(
                "未检测到 web.embed.v1 提供插件（请安装并启用 plugin-chrome-web）",
                error_code="not_configured",
            )
        res = await ctx.capabilities.call(
            "web.embed.v1",
            {
                "action": "open",
                "url": REDDIT_LOGIN_URL,
                "title": title.strip() or "Reddit 登录（完成后关闭窗口即可）",
                "auto_translate": False,
            },
        )
        if not res.ok:
            err_code = str(res.data.get("error_code") or "service_offline")
            return CapabilityResult.fail(
                redact_reddit_secrets(res.error or "打开 Reddit 登录窗口失败", self.secret_values()),
                data={k: v for k, v in res.data.items() if k != "error_code"},
                error_code=err_code,
            )
        return res

    def save_vault_cookie_header(self, cookie_header: str) -> bool:
        clean = sanitize_cookie_header(cookie_header)
        if not clean:
            raise RedditPluginError("invalid_argument", "cookie_header 不能为空")
        with self._lock:
            self._vault_cookie_header = clean
            ctx = self._ctx
        if ctx is not None:
            ctx.set_secret("cookie_header", clean)
        return True

    def save_vault_oauth_token(self, bearer_token: str) -> bool:
        clean = sanitize_bearer_token(bearer_token)
        if not clean:
            raise RedditPluginError("invalid_argument", "oauth_token 不能为空")
        with self._lock:
            self._vault_oauth_token = clean
            ctx = self._ctx
        if ctx is not None:
            ctx.set_secret("oauth_token", clean)
        return True

    def clear_session(self) -> None:
        with self._lock:
            self._vault_cookie_header = ""
            self._vault_oauth_token = ""
            self._cached_chrome_cookie_header = ""
            ctx = self._ctx
        if ctx is not None:
            ctx.set_secret("cookie_header", None)
            ctx.set_secret("oauth_token", None)

    def safe_status(self, resolved: AuthResolution | None = None) -> dict[str, Any]:
        """Return safe authentication status without any raw cookies or tokens."""
        res = resolved if resolved is not None else self.resolve_cached_sync()
        with self._lock:
            cfg = self.config()
            ctx = self._ctx
        caps = ctx.capabilities if ctx is not None else None
        cookie_file_safe = True
        if cfg.cookie_file_path:
            try:
                validate_safe_file_path(cfg.cookie_file_path)
            except RedditPluginError:
                cookie_file_safe = False
        return {
            "authenticated": res.is_authenticated(),
            "source": res.source_used,
            "chrome_cookie_source": cfg.chrome_cookie_source,
            "prefer_chrome_fetch": cfg.prefer_chrome_fetch,
            "auto_translate": cfg.auto_translate,
            "has_reddit_session": res.has_reddit_session,
            "has_token_v2": res.has_token_v2,
            "has_loid": res.has_loid,
            "has_oauth_token": bool(res.oauth_bearer_token),
            "cookie_file_safe": cookie_file_safe,
            "user_agent": cfg.user_agent,
            "chrome_bridge_available": bool(caps and (caps.has("web.cookies.v1") or caps.has("web.embed.v1"))),
            "chrome_fetch_available": bool(caps and caps.has("web.fetch.v1")),
            "chrome_extract_available": bool(caps and caps.has("web.extract.v1")),
        }
