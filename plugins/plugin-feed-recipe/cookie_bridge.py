"""Multi-source Cookie resolver (`web.cookies.v1` / `web.embed.v1`, Netscape/JSON files, SecretVault)."""

from __future__ import annotations

import inspect
import json
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .security import FeedRecipeError, is_http_like_url, validate_safe_path
else:
    from security import FeedRecipeError, is_http_like_url, validate_safe_path

CapHasFn = Callable[[str], bool]
CapCallFn = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class CookieEntry:
    """RFC 6265 cookie record parsed from JSON or Netscape cookies.txt."""

    domain: str
    include_subdomains: bool = False
    path: str = "/"
    secure: bool = False
    expires: int = 0
    name: str = ""
    value: str = ""


@dataclass(frozen=True, slots=True)
class CookieResolution:
    """Resolved cookie header and provenance tag."""

    header: str = ""
    source_used: str = "none"


def domain_matches(host: str, cookie_domain: str, include_subdomains: bool) -> bool:
    """RFC 6265 domain matching check."""
    h = (host or "").strip().lower()
    d = (cookie_domain or "").strip().lower().lstrip(".")
    if not h or not d:
        return False
    if h == d:
        return True
    if not include_subdomains:
        return False
    return h.endswith(f".{d}")


def path_matches(req_path: str, cookie_path: str) -> bool:
    """RFC 6265 path prefix matching check."""
    rp = req_path or "/"
    cp = cookie_path or "/"
    if rp == cp:
        return True
    if not rp.startswith(cp):
        return False
    return cp.endswith("/") or rp[len(cp)] == "/"


def build_cookie_header(entries: Sequence[CookieEntry], url: str, now_epoch_sec: int | None = None) -> str:
    """Filter cookies by scheme, expiry, domain, path (longest path first) and format Cookie header."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    scheme = parsed.scheme.lower()
    req_path = parsed.path or "/"
    now_sec = int(time.time()) if now_epoch_sec is None else int(now_epoch_sec)

    matched: list[tuple[int, str]] = []
    for entry in entries:
        if not entry.name:
            continue
        if entry.secure and scheme != "https":
            continue
        if entry.expires > 0 and entry.expires <= now_sec:
            continue
        if not domain_matches(host, entry.domain, entry.include_subdomains):
            continue
        if not path_matches(req_path, entry.path):
            continue
        matched.append((len(entry.path), f"{entry.name}={entry.value}"))

    matched.sort(key=lambda pair: pair[0], reverse=True)
    return "; ".join(kv for _, kv in matched)


def parse_json_cookies(json_text: str) -> list[CookieEntry]:
    """Parse Chrome-exported JSON array or `{cookies: [...]}` object."""
    try:
        raw = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(raw, list):
        arr = raw
    elif isinstance(raw, Mapping) and isinstance(raw.get("cookies"), list):
        arr = raw["cookies"]
    else:
        return []

    out: list[CookieEntry] = []
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        value = str(item.get("value") or "")
        dom = str(item.get("domain") or "").strip().lower()
        if dom.startswith("."):
            inc_sub = True
            dom = dom.lstrip(".")
        elif isinstance(item.get("hostOnly"), bool):
            inc_sub = not item["hostOnly"]
        else:
            inc_sub = bool(dom)
        path = str(item.get("path") or "/") or "/"
        secure = bool(item.get("secure", False))
        expires = 0
        if isinstance(item.get("expirationDate"), (int, float)) and not isinstance(item.get("expirationDate"), bool):
            expires = int(item["expirationDate"])
        elif isinstance(item.get("expires"), (int, float)) and not isinstance(item.get("expires"), bool):
            expires = int(item["expires"])
        out.append(
            CookieEntry(
                domain=dom,
                include_subdomains=inc_sub,
                path=path,
                secure=secure,
                expires=expires,
                name=name,
                value=value,
            )
        )
    return out


def parse_netscape_cookies(text: str) -> list[CookieEntry]:
    """Parse Netscape `cookies.txt` including `#HttpOnly_` prefixed rows."""
    out: list[CookieEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("#HttpOnly_"):
                line = line[10:]
            else:
                continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        dom = cols[0].strip().lower().lstrip(".")
        inc_sub = cols[1].strip().lower() == "true"
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
                    include_subdomains=inc_sub,
                    path=path,
                    secure=secure,
                    expires=expires,
                    name=name,
                    value=value,
                )
            )
    return out


def load_cookies_from_file(path_str: str) -> list[CookieEntry]:
    """Safely read JSON or Netscape cookie file (rejecting symlinks and path traversal)."""
    cleaned = (path_str or "").strip()
    if not cleaned:
        return []
    try:
        path = validate_safe_path(cleaned, check_symlinks=True)
    except FeedRecipeError:
        return []
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    trimmed = content.strip()
    if not trimmed:
        return []
    if trimmed.startswith(("[", "{")):
        return parse_json_cookies(trimmed)
    return parse_netscape_cookies(content)


def _format_capability_cookies(data: Mapping[str, Any], url: str) -> str:
    raw_hdr = data.get("cookie_header") or data.get("header")
    if isinstance(raw_hdr, str) and raw_hdr.strip():
        return raw_hdr.strip()
    cookies = data.get("cookies")
    if isinstance(cookies, str) and cookies.strip():
        return cookies.strip()
    if isinstance(cookies, Mapping):
        pairs = [f"{k}={v}" for k, v in cookies.items() if str(k).strip() and v is not None]
        return "; ".join(pairs)
    if isinstance(cookies, list):
        entries = parse_json_cookies(json.dumps(cookies))
        if entries:
            # Some capabilities (like plugin-chrome-web get_cookies) omit domain when already filtered
            if any(e.domain for e in entries):
                hdr = build_cookie_header(entries, url)
                if hdr:
                    return hdr
            return "; ".join(f"{e.name}={e.value}" for e in entries if e.name)
    return ""


class CookieBridge:
    """Resolves per-URL session cookies across Chrome Web Bridge, local file, and SecretVault."""

    def __init__(self, ctx: PluginContext | None = None) -> None:
        self._ctx = ctx
        self._custom_has: CapHasFn | None = None
        self._custom_call: CapCallFn | None = None
        self.mode: str = "auto"
        self.cookie_file_path: str = ""
        self.vault_cookie_header: str = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def set_custom_transport(self, has_cap: CapHasFn | None, call_cap: CapCallFn | None) -> None:
        self._custom_has = has_cap
        self._custom_call = call_cap

    def configure(self, mode: str, cookie_file_path: str, vault_cookie_header: str) -> None:
        self.mode = (mode or "auto").strip().lower() or "auto"
        self.cookie_file_path = (cookie_file_path or "").strip()
        self.vault_cookie_header = (vault_cookie_header or "").strip()

    def _has_cap(self, cap_id: str) -> bool:
        if self._custom_has is not None:
            return bool(self._custom_has(cap_id))
        if self._ctx is not None:
            return bool(self._ctx.capabilities.has(cap_id))
        return False

    async def _call_cap(self, cap_id: str, request: dict[str, Any]) -> CapabilityResult:
        if self._custom_call is not None:
            out = self._custom_call(cap_id, request)
            return await out if inspect.isawaitable(out) else out
        if self._ctx is not None:
            return await self._ctx.capabilities.call(cap_id, request)
        return CapabilityResult.fail("No context", error_code="service_offline")

    async def _try_chrome_capability(self, cap_id: str, url: str, domain: str) -> str:
        if not self._has_cap(cap_id):
            return ""
        res = await self._call_cap(
            cap_id,
            {
                "action": "get_cookies",
                "url": url,
                "domain": domain,
                "include_subdomains": True,
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            return ""
        return _format_capability_cookies(res.data, url)

    async def resolve_for_url(
        self,
        url: str,
        recipe_id: str,
        use_chrome_cookies: bool,
        override_domain: str = "",
    ) -> CookieResolution:
        """Resolve Cookie header for target URL according to `chrome_cookie_source` priority."""
        if not use_chrome_cookies or self.mode == "disabled" or not is_http_like_url(url):
            return CookieResolution()

        host = (urllib.parse.urlsplit(url.strip()).hostname or "").lower()
        target_domain = (override_domain or host).strip().lower()

        try_plugin = self.mode in {"auto", "plugin-chrome-web"}
        try_file = self.mode in {"auto", "file"}
        try_vault = self.mode in {"auto", "vault"}

        if try_plugin:
            hdr = await self._try_chrome_capability("web.cookies.v1", url, target_domain)
            if hdr:
                return CookieResolution(header=hdr, source_used="chrome-web:web.cookies.v1")
            hdr = await self._try_chrome_capability("web.embed.v1", url, target_domain)
            if hdr:
                return CookieResolution(header=hdr, source_used="chrome-web:web.embed.v1")

        if try_file and self.cookie_file_path:
            entries = load_cookies_from_file(self.cookie_file_path)
            hdr = build_cookie_header(entries, url)
            if hdr:
                return CookieResolution(header=hdr, source_used="file")

        if try_vault:
            if self._ctx is not None and recipe_id:
                per_recipe = self._ctx.get_secret(f"cookie:{recipe_id}")
                if per_recipe and per_recipe.strip():
                    return CookieResolution(header=per_recipe.strip(), source_used="vault:recipe")
            if self.vault_cookie_header:
                return CookieResolution(header=self.vault_cookie_header, source_used="vault:default")
            if self._ctx is not None:
                for key in ("cookie_header", "default_cookie_header"):
                    val = self._ctx.get_secret(key)
                    if val and val.strip():
                        return CookieResolution(header=val.strip(), source_used="vault:default")

        return CookieResolution()


__all__ = [
    "CookieBridge",
    "CookieEntry",
    "CookieResolution",
    "build_cookie_header",
    "domain_matches",
    "load_cookies_from_file",
    "parse_json_cookies",
    "parse_netscape_cookies",
    "path_matches",
]
