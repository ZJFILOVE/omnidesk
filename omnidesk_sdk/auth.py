"""Cookie/session resolution shared by plugins.

Priority: `web.cookies.v1` capability -> SecretVault -> Netscape/JSON cookie file.
The resolver never logs or returns cookie values in error messages.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CookieResolutionError(RuntimeError):
    """Fail-closed cookie resolution error carrying a canonical error code."""

    def __init__(self, message: str, *, code: str = "auth_required") -> None:
        self.error_code = code
        super().__init__(message)

    @property
    def code(self) -> str:
        return self.error_code


@dataclass(frozen=True)
class SessionCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    expires_at: float = 0.0
    http_only: bool = False


def _chromium_expiry_to_unix(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value > 10**12:
        return value / 1_000_000.0 - 11_644_473_600.0
    return value


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "secure"}


def parse_netscape_cookies(text: str) -> list[SessionCookie]:
    """Parse Netscape `cookies.txt` content (including `#HttpOnly_` rows)."""
    cookies: list[SessionCookie] = []
    for raw_line in text.splitlines():
        line = raw_line.strip("\r\n")
        if not line:
            continue
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _include_subdomains, path, secure, expiry, name, value = fields
        if not name:
            continue
        cookies.append(
            SessionCookie(
                name=name,
                value=value,
                domain=domain.strip(),
                path=path or "/",
                secure=_coerce_bool(secure),
                expires_at=_chromium_expiry_to_unix(expiry),
                http_only=http_only,
            )
        )
    return cookies


def _cookie_from_mapping(item: Mapping[str, Any]) -> SessionCookie | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    domain = str(item.get("domain") or item.get("host_key") or "").strip()
    expires = item.get("expires_at")
    if expires is None:
        expires = item.get("expirationDate")
    if expires is None:
        expires = item.get("expires")
    if expires is None:
        expires = item.get("expires_utc")
    return SessionCookie(
        name=name,
        value=str(item.get("value") or ""),
        domain=domain,
        path=str(item.get("path") or "/"),
        secure=_coerce_bool(item.get("secure", item.get("is_secure", False))),
        expires_at=_chromium_expiry_to_unix(expires),
        http_only=_coerce_bool(item.get("httpOnly", item.get("is_httponly", False))),
    )


def parse_json_cookies(payload: str) -> list[SessionCookie]:
    """Parse JSON cookie exports (`[...]` or `{"cookies": [...]}`)."""
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CookieResolutionError("Cookie JSON 格式无效", code="invalid_response") from exc
    if isinstance(parsed, Mapping):
        parsed = parsed.get("cookies")
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes)):
        raise CookieResolutionError("Cookie JSON 缺少 cookies 数组", code="invalid_response")
    cookies: list[SessionCookie] = []
    for item in parsed:
        if isinstance(item, Mapping):
            cookie = _cookie_from_mapping(item)
            if cookie is not None:
                cookies.append(cookie)
    return cookies


def parse_cookie_header(header: str, *, domain: str, path: str = "/", secure: bool = False) -> list[SessionCookie]:
    """Parse a `Cookie` header into cookies scoped to an explicit domain."""
    cookies: list[SessionCookie] = []
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name.strip():
            continue
        cookies.append(
            SessionCookie(
                name=name.strip(),
                value=value.strip(),
                domain=domain,
                path=path or "/",
                secure=secure,
            )
        )
    return cookies


def _domain_matches(cookie_domain: str, host: str) -> bool:
    domain = cookie_domain.strip().lower().lstrip(".")
    if not domain:
        return False
    return host == domain or host.endswith(f".{domain}")


def _path_matches(cookie_path: str, request_path: str) -> bool:
    path = cookie_path or "/"
    if path == "/":
        return True
    request_path = request_path or "/"
    if request_path == path:
        return True
    return request_path.startswith(path.rstrip("/") + "/")


def filter_cookies(cookies: Iterable[SessionCookie], url: str, *, now: float | None = None) -> list[SessionCookie]:
    """Return cookies applicable to `url`, ignoring expired and secure-on-http rows."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    request_path = parsed.path or "/"
    secure_request = parsed.scheme.lower() == "https"
    current = time.time() if now is None else now
    matched = [
        cookie
        for cookie in cookies
        if _domain_matches(cookie.domain, host)
        and _path_matches(cookie.path, request_path)
        and (secure_request or not cookie.secure)
        and (cookie.expires_at <= 0 or cookie.expires_at > current)
    ]
    matched.sort(key=lambda item: len(item.path), reverse=True)
    return matched


def _read_secret(secret_vault: Any, key: str) -> str | None:
    for method_name in ("get_secret", "get"):
        method = getattr(secret_vault, method_name, None)
        if callable(method):
            value = method(key)
            return None if value is None else str(value)
    if isinstance(secret_vault, Mapping):
        value = secret_vault.get(key)
        return None if value is None else str(value)
    return None


async def _call_capability(capability: Any, request: Mapping[str, Any]) -> Any:
    if hasattr(capability, "call"):
        return await capability.call("web.cookies.v1", request)
    if callable(capability):
        result = capability("web.cookies.v1", request)
        if isinstance(result, Awaitable):
            return await result
        return result
    raise CookieResolutionError("web.cookies.v1 capability 不可用", code="not_configured")


def _capability_cookies(result: Any) -> tuple[list[SessionCookie], str]:
    if result is None:
        return [], "not_configured"
    ok = getattr(result, "ok", None)
    data = getattr(result, "data", None)
    if ok is False:
        code = ""
        if isinstance(data, Mapping):
            code = str(data.get("error_code") or "")
        return [], code or "auth_required"
    if data is None and isinstance(result, Mapping):
        ok = result.get("ok", True)
        data = result.get("data", result)
    if ok is False:
        return [], str((data or {}).get("error_code") or "auth_required") if isinstance(data, Mapping) else "auth_required"
    if not isinstance(data, Mapping):
        return [], "invalid_response"
    raw_cookies = data.get("cookies")
    if raw_cookies is None and data.get("cookie_header"):
        return [], "invalid_response"
    if not isinstance(raw_cookies, Sequence) or isinstance(raw_cookies, (str, bytes)):
        return [], "invalid_response"
    cookies = [cookie for item in raw_cookies if isinstance(item, Mapping) if (cookie := _cookie_from_mapping(item))]
    return cookies, ""


class CookieSessionResolver:
    """Resolve request cookies from capability, SecretVault, or cookie files."""

    def __init__(
        self,
        *,
        capability: Any = None,
        secret_vault: Any = None,
        cookie_file: str | Path | None = None,
        secret_key: str = "cookies",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.capability = capability
        self.secret_vault = secret_vault
        self.cookie_file = Path(cookie_file).expanduser() if cookie_file else None
        self.secret_key = secret_key
        self.clock = clock or time.time

    async def resolve(
        self,
        url: str,
        *,
        capability: Any = None,
        secret_vault: Any = None,
        cookie_file: str | Path | None = None,
        secret_key: str | None = None,
        header_name: str | None = None,
    ) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise CookieResolutionError("Cookie 解析需要有效的 http(s) URL", code="invalid_argument")
        host = parsed.hostname.lower()
        first_error = ""
        collected: list[SessionCookie] = []

        capability_obj = self.capability if capability is None else capability
        if capability_obj is not None:
            try:
                result = await _call_capability(capability_obj, {"action": "get", "url": url})
                cookies, error_code = _capability_cookies(result)
            except CookieResolutionError as exc:
                cookies, error_code = [], exc.error_code
            except Exception:
                cookies, error_code = [], "service_offline"
            collected.extend(cookies)
            first_error = first_error or error_code

        vault = self.secret_vault if secret_vault is None else secret_vault
        if vault is not None:
            key = secret_key or self.secret_key
            raw = _read_secret(vault, key)
            if raw:
                try:
                    if raw.lstrip().startswith(("{", "[")):
                        collected.extend(parse_json_cookies(raw))
                    else:
                        collected.extend(
                            parse_cookie_header(
                                raw,
                                domain=host,
                                path=parsed.path or "/",
                                secure=parsed.scheme.lower() == "https",
                            )
                        )
                except CookieResolutionError as exc:
                    first_error = first_error or exc.error_code

        path = Path(cookie_file).expanduser() if cookie_file else self.cookie_file
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                first_error = first_error or "auth_required"
            else:
                try:
                    if text.lstrip().startswith(("{", "[")):
                        collected.extend(parse_json_cookies(text))
                    else:
                        collected.extend(parse_netscape_cookies(text))
                except CookieResolutionError as exc:
                    first_error = first_error or exc.error_code

        matched = filter_cookies(collected, url, now=self.clock())
        if not matched:
            code = first_error or "auth_required"
            raise CookieResolutionError(f"未找到域 {host} 的可用 Cookie", code=code)
        resolved: dict[str, str] = {}
        for cookie in matched:
            resolved.setdefault(cookie.name, cookie.value)
        return resolved


__all__ = [
    "CookieResolutionError",
    "CookieSessionResolver",
    "SessionCookie",
    "filter_cookies",
    "parse_cookie_header",
    "parse_json_cookies",
    "parse_netscape_cookies",
]
