"""Auth material resolution (`web.cookies.v1` -> `SecretVault`) for plugin-annas-archive."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnidesk_sdk import PluginContext

from annas_security import parse_origin

_DEFAULT_USER_AGENT = "OmniDesk-BookSearch/0.2 (AnnasArchive)"


@dataclass(frozen=True, slots=True)
class AuthMaterial:
    """Resolved session credentials for Anna's Archive requests."""

    cookie_header: str = ""
    api_key: str = ""
    cookie_source: str = "none"

    def extra_secrets(self) -> tuple[str, ...]:
        return tuple(v for v in (self.cookie_header, self.api_key) if v)

    def build_headers(self, *, accept: str = "text/html,application/json;q=0.9,*/*;q=0.8") -> dict[str, str]:
        headers = {"User-Agent": _DEFAULT_USER_AGENT, "Accept": accept}
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers


def _format_cookies_from_capability(data: Mapping[str, Any]) -> str:
    raw_header = data.get("cookie_header") or data.get("header")
    if isinstance(raw_header, str) and raw_header.strip():
        return raw_header.strip()
    cookies = data.get("cookies")
    if isinstance(cookies, str) and cookies.strip():
        return cookies.strip()
    if isinstance(cookies, Mapping):
        pairs = [f"{k}={v}" for k, v in cookies.items() if str(k).strip() and v is not None]
        return "; ".join(pairs)
    if isinstance(cookies, list):
        pairs = []
        for item in cookies:
            if isinstance(item, Mapping):
                name = str(item.get("name") or "").strip()
                val = item.get("value")
                if name and val is not None:
                    pairs.append(f"{name}={val}")
        return "; ".join(pairs)
    return ""


async def resolve_auth_material(ctx: PluginContext | None, target_url: str) -> AuthMaterial:
    """Resolve Cookie/API credentials: prefer `web.cookies.v1`, else 0600 `SecretVault`."""
    if ctx is None:
        return AuthMaterial()

    source_pref = str(ctx.get_config("chrome_cookie_source", "auto") or "auto").strip().lower()
    api_key = str(ctx.get_secret("api_key") or "").strip()
    vault_cookie = str(ctx.get_secret("cookie_header") or ctx.get_secret("cookie") or "").strip()

    cookie_header = ""
    cookie_source = "none"

    if source_pref in {"auto", "plugin-chrome-web"} and ctx.capabilities.has("web.cookies.v1"):
        try:
            origin = parse_origin(target_url)
            origin_url = f"{origin.scheme}://{origin.host}:{origin.port}/"
            res = await ctx.capabilities.call(
                "web.cookies.v1",
                {
                    "action": "get_cookies",
                    "url": origin_url,
                    "domain": origin.host,
                    "include_subdomains": True,
                },
            )
            if res.ok and isinstance(res.data, Mapping):
                candidate = _format_cookies_from_capability(res.data)
                if candidate:
                    cookie_header = candidate
                    cookie_source = "web.cookies.v1"
        except Exception:
            pass

    if not cookie_header and source_pref != "disabled" and vault_cookie:
        cookie_header = vault_cookie
        cookie_source = "vault"

    return AuthMaterial(cookie_header=cookie_header, api_key=api_key, cookie_source=cookie_source)
