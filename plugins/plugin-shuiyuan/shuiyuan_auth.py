"""Multi-source cookie resolution, Chrome SSO login, and SecretVault persistence for plugin-shuiyuan."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnidesk_sdk import PluginContext

if __package__:
    from .shuiyuan_security import (
        DEFAULT_BASE_URL,
        ShuiyuanError,
        has_control_chars,
        redact_sensitive,
        validate_safe_file_path,
    )
else:
    from shuiyuan_security import (
        DEFAULT_BASE_URL,
        ShuiyuanError,
        has_control_chars,
        redact_sensitive,
        validate_safe_file_path,
    )

AUTO_LOGIN_COOLDOWN_SEC = 120.0


@dataclass(slots=True)
class CookieEntry:
    """Normalized cookie entry parsed from JSON or Netscape cookies.txt."""

    domain: str
    include_subdomains: bool = False
    path: str = "/"
    secure: bool = False
    http_only: bool = False
    expires: int = 0
    name: str = ""
    value: str = ""


@dataclass(slots=True)
class AuthResolution:
    """Resolved cookie header and authentication metadata."""

    cookie_header: str = ""
    source_used: str = "guest"
    has_t_cookie: bool = False
    has_session_cookie: bool = False


class AuthManager:
    """Resolves Discourse session cookies across plugin-chrome-web, SecretVault, and local cookie files."""

    def __init__(self, ctx: PluginContext | None = None) -> None:
        self._ctx = ctx
        self._mode = "auto"
        self._cookie_file_path = ""
        self._base_url = DEFAULT_BASE_URL
        self._auto_login_on_expire = True
        self._last_cookie_header = ""
        self._last_source = "guest"
        self._last_has_t = False
        self._last_has_session = False
        self._session_expired = False
        self._username = ""
        self._last_auth_error = ""
        self._last_login_prompt_monotonic = 0.0

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def configure(
        self,
        mode: str = "auto",
        cookie_file_path: str = "",
        base_url: str = DEFAULT_BASE_URL,
        auto_login_on_expire: bool = True,
    ) -> None:
        clean_mode = str(mode or "auto").strip().lower()
        self._mode = clean_mode or "auto"
        self._cookie_file_path = str(cookie_file_path or "").strip()
        clean_base = str(base_url or "").strip().rstrip("/")
        self._base_url = clean_base or DEFAULT_BASE_URL
        self._auto_login_on_expire = bool(auto_login_on_expire)

    @property
    def auto_login_on_expire(self) -> bool:
        return self._auto_login_on_expire

    @property
    def is_session_expired(self) -> bool:
        return self._session_expired

    def is_authenticated_hint(self) -> bool:
        """Non-blocking hint for sync TextSource.channels() without invoking IPC."""
        if self._session_expired:
            return False
        if self._last_has_t:
            return True
        if self._mode in {"auto", "vault"} and self._ctx is not None:
            raw = self.normalize_cookie_input(self._ctx.get_secret("cookie_header") or "")
            if self.contains_cookie_name(raw, "_t"):
                return True
        return False

    @staticmethod
    def normalize_cookie_input(raw: str) -> str:
        """Normalize pasted Cookie header or bare `_t` token without control characters."""
        cleaned = str(raw or "").strip()
        if not cleaned:
            return ""
        if has_control_chars(cleaned):
            raise ShuiyuanError("invalid_argument", "Cookie header 包含非法控制字符")
        if cleaned.lower().startswith("cookie:"):
            cleaned = cleaned[7:].strip()
        if not cleaned:
            return ""
        if "=" not in cleaned and ";" not in cleaned:
            return f"_t={cleaned}"
        return cleaned

    @staticmethod
    def contains_cookie_name(cookie_header: str, name: str) -> bool:
        if not cookie_header or not name:
            return False
        for pair in cookie_header.split(";"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            if k.strip() == name and v.strip():
                return True
        return False

    @staticmethod
    def domain_matches(host: str, cookie_domain: str, include_subdomains: bool) -> bool:
        h = host.strip().lower()
        d = cookie_domain.strip().lower().lstrip(".")
        if not h or not d:
            return False
        if h == d:
            return True
        if not include_subdomains:
            return False
        return h.endswith(f".{d}")

    @staticmethod
    def path_matches(req_path: str, cookie_path: str) -> bool:
        rp = req_path or "/"
        cp = cookie_path or "/"
        if rp == cp:
            return True
        if not rp.startswith(cp):
            return False
        return cp.endswith("/") or rp[len(cp)] == "/"

    @classmethod
    def parse_json_cookies(cls, json_text: str) -> list[CookieEntry]:
        """Parse Chrome-exported JSON cookies array or `{cookies: [...]}` object."""
        try:
            doc = json.loads(json_text)
        except json.JSONDecodeError:
            return []
        if isinstance(doc, list):
            items = doc
        elif isinstance(doc, Mapping) and isinstance(doc.get("cookies"), list):
            items = doc["cookies"]
        else:
            return []

        out: list[CookieEntry] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name:
                continue
            raw_dom = str(item.get("domain") or "").strip().lower()
            if raw_dom.startswith("."):
                include_sub = True
                dom = raw_dom.lstrip(".")
            elif isinstance(item.get("hostOnly"), bool):
                include_sub = not item["hostOnly"]
                dom = raw_dom
            else:
                include_sub = bool(raw_dom)
                dom = raw_dom
            path = str(item.get("path") or "/") or "/"
            secure = bool(item.get("secure", False))
            http_only = bool(item.get("httpOnly") if isinstance(item.get("httpOnly"), bool) else item.get("http_only", False))
            raw_exp = item.get("expirationDate") if item.get("expirationDate") is not None else item.get("expires")
            expires = 0
            if isinstance(raw_exp, (int, float)) and not isinstance(raw_exp, bool):
                expires = int(raw_exp)
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

    @classmethod
    def parse_netscape_cookies(cls, text: str) -> list[CookieEntry]:
        """Parse Netscape cookies.txt (including `#HttpOnly_` entries)."""
        out: list[CookieEntry] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                continue
            http_only = False
            if line.startswith("#"):
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_") :]
                    http_only = True
                else:
                    continue
            cols = line.split("\t")
            if len(cols) < 7:
                continue
            dom = cols[0].strip().lower().lstrip(".")
            include_sub = cols[1].strip().lower() == "true"
            path = cols[2] or "/"
            secure = cols[3].strip().lower() == "true"
            try:
                expires = int(cols[4])
            except ValueError:
                expires = 0
            name = cols[5]
            value = cols[6]
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

    @classmethod
    def load_cookies_from_file(cls, raw_path: str) -> list[CookieEntry]:
        """Load cookies from a validated local JSON or Netscape file (rejects traversal/symlinks)."""
        safe_path = validate_safe_file_path(raw_path)
        if not safe_path.is_file():
            return []
        content = safe_path.read_text(encoding="utf-8", errors="replace")
        trimmed = content.strip()
        if not trimmed:
            return []
        if trimmed[0] in {"[", "{"}:
            return cls.parse_json_cookies(trimmed)
        return cls.parse_netscape_cookies(content)

    @classmethod
    def build_cookie_header(cls, entries: list[CookieEntry], url: str, now_epoch_sec: int | None = None) -> str:
        """Build an RFC 6265 Cookie header matching domain, path, secure flag, and expiry."""
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        scheme = parsed.scheme.lower()
        req_path = parsed.path or "/"
        if not host or scheme not in {"http", "https"}:
            return ""
        now = int(time.time()) if now_epoch_sec is None else int(now_epoch_sec)
        matched: list[tuple[int, str]] = []
        for c in entries:
            if c.secure and scheme != "https":
                continue
            if c.expires > 0 and c.expires <= now:
                continue
            if not cls.domain_matches(host, c.domain, c.include_subdomains):
                continue
            if not cls.path_matches(req_path, c.path):
                continue
            matched.append((len(c.path), f"{c.name}={c.value}"))
        matched.sort(key=lambda pair: pair[0], reverse=True)
        return "; ".join(pair[1] for pair in matched)

    def _has_cap(self, cap_id: str) -> bool:
        return self._ctx is not None and bool(self._ctx.capabilities.has(cap_id))

    async def _try_chrome_cookies(self) -> str:
        if self._ctx is None:
            return ""
        parsed = urllib.parse.urlsplit(self._base_url)
        domain = (parsed.hostname or "shuiyuan.sjtu.edu.cn").lower()
        target_url = f"{self._base_url}/"
        for cap_id in ("web.cookies.v1", "web.embed.v1"):
            if not self._has_cap(cap_id):
                continue
            try:
                res = await self._ctx.capabilities.call(
                    cap_id,
                    {
                        "action": "get_cookies",
                        "url": target_url,
                        "domain": domain,
                        "include_subdomains": True,
                    },
                )
            except Exception:
                continue
            if not res.ok or not isinstance(res.data, Mapping):
                continue
            raw_hdr = res.data.get("cookie_header") or res.data.get("header")
            if isinstance(raw_hdr, str) and raw_hdr.strip():
                try:
                    return self.normalize_cookie_input(raw_hdr)
                except ShuiyuanError:
                    continue
            raw_cookies = res.data.get("cookies")
            if isinstance(raw_cookies, list):
                entries = self.parse_json_cookies(json.dumps(raw_cookies))
                header = self.build_cookie_header(entries, target_url)
                if header:
                    return header
        return ""

    async def resolve_cookies(self, persist_chrome_snapshot: bool = True) -> AuthResolution:
        """Resolve session cookies using configured priority (`auto`, `plugin-chrome-web`, `vault`, `file`, `disabled`)."""
        out = AuthResolution()
        if self._mode == "disabled":
            self._last_cookie_header = ""
            self._last_source = "guest"
            self._last_has_t = False
            self._last_has_session = False
            return out

        try_plugin = self._mode in {"auto", "plugin-chrome-web"}
        try_vault = self._mode in {"auto", "vault"}
        try_file = self._mode in {"auto", "file"}
        chrome_fallback = ""

        if try_plugin:
            chrome_hdr = await self._try_chrome_cookies()
            if chrome_hdr:
                has_t = self.contains_cookie_name(chrome_hdr, "_t")
                if has_t or self._mode == "plugin-chrome-web":
                    out.cookie_header = chrome_hdr
                    out.source_used = "chrome-web:web.cookies.v1"
                    out.has_t_cookie = has_t
                    out.has_session_cookie = self.contains_cookie_name(chrome_hdr, "_forum_session")
                    if persist_chrome_snapshot and has_t and self._ctx is not None:
                        prev = (self._ctx.get_secret("cookie_header") or "").strip()
                        if prev != chrome_hdr:
                            self._ctx.set_secret("cookie_header", chrome_hdr)
                else:
                    chrome_fallback = chrome_hdr

        if not out.cookie_header and try_vault and self._ctx is not None:
            raw_sec = self._ctx.get_secret("cookie_header") or ""
            if raw_sec.strip():
                try:
                    normalized = self.normalize_cookie_input(raw_sec)
                except ShuiyuanError:
                    normalized = ""
                if normalized:
                    out.cookie_header = normalized
                    out.source_used = "vault"
                    out.has_t_cookie = self.contains_cookie_name(normalized, "_t")
                    out.has_session_cookie = self.contains_cookie_name(normalized, "_forum_session")

        if not out.cookie_header and try_file and self._cookie_file_path:
            entries = self.load_cookies_from_file(self._cookie_file_path)
            file_hdr = self.build_cookie_header(entries, f"{self._base_url}/")
            if file_hdr:
                out.cookie_header = file_hdr
                out.source_used = "file"
                out.has_t_cookie = self.contains_cookie_name(file_hdr, "_t")
                out.has_session_cookie = self.contains_cookie_name(file_hdr, "_forum_session")

        if not out.cookie_header and chrome_fallback:
            out.cookie_header = chrome_fallback
            out.source_used = "chrome-web:web.cookies.v1"
            out.has_t_cookie = False
            out.has_session_cookie = self.contains_cookie_name(chrome_fallback, "_forum_session")

        self._last_cookie_header = out.cookie_header
        self._last_source = out.source_used
        self._last_has_t = out.has_t_cookie
        self._last_has_session = out.has_session_cookie
        if out.has_t_cookie:
            self._session_expired = False
            self._last_auth_error = ""
        return out

    async def sync_from_chrome(self) -> tuple[bool, str, str, AuthResolution]:
        """Synchronize Shuiyuan cookies from plugin-chrome-web fail-closed (requires `_t`)."""
        if not self._has_cap("web.cookies.v1") and not self._has_cap("web.embed.v1"):
            msg = "未检测到 plugin-chrome-web (web.cookies.v1)"
            self._last_auth_error = msg
            return False, "not_configured", msg, AuthResolution()

        header = await self._try_chrome_cookies()
        if not header:
            msg = "Chrome 中暂无 shuiyuan.sjtu.edu.cn 登录 Cookie，请先完成 jAccount SSO 登录"
            self._last_auth_error = msg
            return False, "not_configured", msg, AuthResolution()

        has_t = self.contains_cookie_name(header, "_t")
        has_sess = self.contains_cookie_name(header, "_forum_session")
        res = AuthResolution(
            cookie_header=header,
            source_used="chrome-web:web.cookies.v1",
            has_t_cookie=has_t,
            has_session_cookie=has_sess,
        )
        self._last_cookie_header = header
        self._last_source = res.source_used
        self._last_has_t = has_t
        self._last_has_session = has_sess

        if has_t:
            self._session_expired = False
            self._last_auth_error = ""
            if self._ctx is not None:
                self._ctx.set_secret("cookie_header", header)
            return True, "", "", res

        msg = "已读取到站点 Cookie，但未包含 _t 登录凭证，请先在浏览器完成 jAccount 登录"
        self._last_auth_error = "已同步会话 Cookie，但缺少 HttpOnly 登录凭据 _t"
        return False, "not_configured", msg, res

    async def open_login_window(self, *, force: bool = False) -> tuple[bool, str, str]:
        """Open managed Chrome jAccount SSO login window via `web.embed.v1` with cooldown."""
        if not self._has_cap("web.embed.v1"):
            msg = "未加载 plugin-chrome-web (web.embed.v1)，无法打开 jAccount 登录窗口"
            self._last_auth_error = msg
            return False, "service_offline", msg

        now = time.monotonic()
        if not force and self._last_login_prompt_monotonic > 0.0:
            if (now - self._last_login_prompt_monotonic) < AUTO_LOGIN_COOLDOWN_SEC:
                return False, "conflict", "已打开或刚提示过登录窗口，请在 Chrome 完成 jAccount 登录后刷新"
        self._last_login_prompt_monotonic = now

        assert self._ctx is not None
        res = await self._ctx.capabilities.call(
            "web.embed.v1",
            {
                "action": "open",
                "url": f"{self._base_url}/login",
                "title": "交大水源社区 jAccount 登录（完成后关闭窗口即可）",
                "auto_translate": False,
            },
        )
        if not res.ok:
            msg = redact_sensitive(res.error or "呼起 Chrome 登录窗失败", self.secrets_for_redaction())
            self._last_auth_error = msg
            code = str(res.data.get("error_code") or "service_offline")
            return False, code, msg
        return True, "", ""

    def set_manual_cookie_header(self, raw_header: str) -> None:
        """Validate and persist a manual Cookie header into SecretVault (0600)."""
        normalized = self.normalize_cookie_input(raw_header)
        if not normalized:
            raise ShuiyuanError("invalid_argument", "cookie_header 不能为空")
        self._last_cookie_header = normalized
        self._last_source = "vault"
        self._last_has_t = self.contains_cookie_name(normalized, "_t")
        self._last_has_session = self.contains_cookie_name(normalized, "_forum_session")
        self._session_expired = False
        self._last_auth_error = ""
        if self._ctx is not None:
            self._ctx.set_secret("cookie_header", normalized)

    def logout(self) -> None:
        """Clear in-memory auth state and remove `cookie_header` from SecretVault."""
        self._last_cookie_header = ""
        self._last_source = "guest"
        self._last_has_t = False
        self._last_has_session = False
        self._session_expired = False
        self._username = ""
        self._last_auth_error = ""
        if self._ctx is not None:
            self._ctx.set_secret("cookie_header", None)

    def mark_session_expired(self, reason: str) -> None:
        self._session_expired = True
        self._last_auth_error = redact_sensitive(reason, self.secrets_for_redaction())

    def mark_authenticated(self, username: str = "") -> None:
        self._session_expired = False
        self._last_auth_error = ""
        if username:
            self._username = username

    def secrets_for_redaction(self) -> tuple[str, ...]:
        """Return secret cookie values for downstream redaction."""
        out: list[str] = []
        if self._last_cookie_header:
            out.append(self._last_cookie_header)
            for pair in self._last_cookie_header.split(";"):
                if "=" in pair:
                    val = pair.split("=", 1)[1].strip()
                    if len(val) >= 6:
                        out.append(val)
        if self._ctx is not None:
            vault_val = self._ctx.get_secret("cookie_header") or ""
            if vault_val and vault_val not in out:
                out.append(vault_val)
        return tuple(out)

    def safe_summary(self) -> dict[str, Any]:
        """Return non-secret authentication state summary."""
        return {
            "mode": self._mode,
            "base_url": self._base_url,
            "cookie_source": self._last_source,
            "has_cookies": bool(self._last_cookie_header),
            "has_t_cookie": self._last_has_t,
            "has_session_cookie": self._last_has_session,
            "authenticated": self._last_has_t and not self._session_expired,
            "session_expired": self._session_expired,
            "username": self._username,
            "last_error": redact_sensitive(self._last_auth_error, self.secrets_for_redaction()),
        }
