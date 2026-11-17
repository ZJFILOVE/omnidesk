"""HTTPS-only network client, SecretVault/web.cookies.v1 auth bridge, and SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

try:
    from .sub_archive import HARD_MAX_DOWNLOAD_BYTES
except ImportError:
    from sub_archive import HARD_MAX_DOWNLOAD_BYTES

_CHUNK_SIZE = 64 * 1024
_MAX_REDIRECTS = 5
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_AUTH_HEADERS = ("authorization", "cookie", "proxy-authorization", "x-api-key")


class SubNetError(RuntimeError):
    """Classified network/protocol error with secret-redacted message."""

    def __init__(self, error_code: str, message: str, *, http_status: int | None = None) -> None:
        self.error_code = error_code
        self.http_status = http_status
        super().__init__(message)


def is_loopback_host(host: str) -> bool:
    """Return True if `host` refers to a loopback address."""
    low = host.strip().lower().strip("[]")
    if low in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(low).is_loopback
    except ValueError:
        return False


def validate_https_url(url: str, *, allow_loopback: bool = True) -> urllib.parse.SplitResult:
    """Strictly enforce https:// and reject private/link-local/credentialed URLs."""
    if not isinstance(url, str) or not url.strip():
        raise SubNetError("invalid_argument", "URL 不能为空")
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        raise SubNetError("invalid_argument", f"URL 格式无效: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise SubNetError("invalid_argument", f"安全限制：仅允许 https 协议，已拒绝 {parts.scheme or 'empty'}://")
    if not parts.hostname:
        raise SubNetError("invalid_argument", "URL 缺少有效主机名")
    if parts.username or parts.password:
        raise SubNetError("invalid_argument", "URL 禁止携带明文用户名或密码")

    host = parts.hostname.strip().lower()
    if is_loopback_host(host):
        if not allow_loopback:
            raise SubNetError("path_safety", "安全限制：禁止重定向或引用本地回环地址")
        return parts

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise SubNetError("invalid_argument", f"URL 主机名无效: {host}")
    else:
        if not addr.is_global:
            raise SubNetError("path_safety", f"安全限制：禁止访问私网或保留 IP ({host})")
    return parts


def _origin_of(url: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or 443)


class _HttpsSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforce HTTPS-only redirects, strip credentials cross-origin, and block SSRF."""

    def __init__(self, initial_is_loopback: bool) -> None:
        super().__init__()
        self._initial_is_loopback = initial_is_loopback
        self._hops = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._hops += 1
        if self._hops > _MAX_REDIRECTS:
            raise SubNetError("service_offline", "HTTP 重定向次数过多")
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        validate_https_url(resolved, allow_loopback=self._initial_is_loopback)
        next_req = super().redirect_request(req, fp, code, msg, headers, resolved)
        if next_req is None:
            return None
        if _origin_of(req.full_url) != _origin_of(resolved):
            for hdr in list(next_req.headers):
                if hdr.lower() in _AUTH_HEADERS:
                    next_req.remove_header(hdr)
            for hdr in list(next_req.unredirected_hdrs):
                if hdr.lower() in _AUTH_HEADERS:
                    next_req.unredirected_hdrs.pop(hdr, None)
        return next_req


def collect_secrets(ctx: Any, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Collect active secret values from context vault and callers for redaction."""
    vals: list[str] = [s for s in extra if isinstance(s, str) and len(s) >= 4]
    vault = getattr(ctx, "_secret_vault", None)
    if vault is not None and callable(getattr(vault, "secret_values", None)):
        try:
            vals.extend(vault.secret_values())
        except Exception:
            pass
    return tuple(sorted(set(vals), key=len, reverse=True))


async def resolve_cookie_header(ctx: Any, target_url: str) -> str:
    """Resolve cookies exclusively via web.cookies.v1 or SecretVault."""
    host = (urllib.parse.urlsplit(target_url).hostname or "").lower()
    if ctx is not None and ctx.capabilities is not None and ctx.capabilities.has("web.cookies.v1"):
        try:
            res = await ctx.capabilities.call(
                "web.cookies.v1",
                {
                    "action": "get_cookies",
                    "url": target_url,
                    "domain": host,
                    "include_subdomains": True,
                },
            )
            if res.ok and isinstance(res.data, Mapping):
                direct = res.data.get("cookie_header") or res.data.get("header")
                if isinstance(direct, str) and direct.strip():
                    return direct.strip()
                cookies = res.data.get("cookies")
                if isinstance(cookies, list):
                    pairs = [
                        f"{c['name']}={c['value']}"
                        for c in cookies
                        if isinstance(c, Mapping) and c.get("name") and c.get("value") is not None
                    ]
                    if pairs:
                        return "; ".join(pairs)
        except Exception:
            pass
    if ctx is not None:
        vault_cookie = ctx.get_secret("cookie_header")
        if vault_cookie and vault_cookie.strip():
            return vault_cookie.strip()
    return ""


@dataclass(frozen=True, slots=True)
class HttpBytesResponse:
    """Downloaded payload with final URL and extracted metadata."""

    url: str
    status: int
    body: bytes
    content_type: str
    filename: str
    charset: str


def _parse_content_disposition(header_val: str) -> str:
    if not header_val:
        return ""
    m_star = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')?([^;\r\n]+)", header_val)
    if m_star:
        return urllib.parse.unquote(m_star.group(1).strip().strip('"'))
    m_plain = re.search(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\r\n]+)', header_val)
    if m_plain:
        return (m_plain.group(1) or m_plain.group(2) or "").strip()
    return ""


def _parse_charset(content_type: str) -> str:
    m = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", content_type or "", re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _fetch_https_sync(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_sec: float = 15.0,
    max_bytes: int = HARD_MAX_DOWNLOAD_BYTES,
    allow_loopback: bool = True,
    ssl_context: ssl.SSLContext | None = None,
    secrets: tuple[str, ...] = (),
) -> HttpBytesResponse:
    parts = validate_https_url(url, allow_loopback=allow_loopback)
    is_loopback = is_loopback_host(parts.hostname or "")
    handlers: list[Any] = [_HttpsSafeRedirectHandler(is_loopback)]
    if is_loopback:
        handlers.append(urllib.request.ProxyHandler({}))
    ctx_tls = ssl_context if ssl_context is not None else ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx_tls))
    opener = urllib.request.build_opener(*handlers)

    req_headers = {"User-Agent": "OmniDesk-SubHunter/0.2", "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")

    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            cl_raw = resp.headers.get("Content-Length")
            if cl_raw is not None:
                try:
                    if int(cl_raw) > max_bytes:
                        raise SubNetError("validation_error", f"响应体积 ({cl_raw} bytes) 超过 {max_bytes} bytes 上限")
                except ValueError:
                    pass

            buf = bytearray()
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if len(buf) + len(chunk) > max_bytes:
                    raise SubNetError("validation_error", f"下载体积超过上限 ({max_bytes} bytes)，已中止")
                buf.extend(chunk)

            ctype = str(resp.headers.get("Content-Type") or "")
            cdisp = str(resp.headers.get("Content-Disposition") or "")
            fname = _parse_content_disposition(cdisp) or Path(urllib.parse.urlsplit(resp.geturl()).path).name
            return HttpBytesResponse(
                url=resp.geturl(),
                status=int(getattr(resp, "status", 200)),
                body=bytes(buf),
                content_type=ctype,
                filename=fname,
                charset=_parse_charset(ctype),
            )
    except SubNetError:
        raise
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if code in (401, 403):
            raise SubNetError("permission_denied", f"字幕源鉴权失败 (HTTP {code})", http_status=code) from None
        if code == 429:
            raise SubNetError("rate_limited", "字幕源触发限流 (HTTP 429)", http_status=429) from None
        if code == 404:
            raise SubNetError("not_found", "字幕条目不存在 (HTTP 404)", http_status=404) from None
        raise SubNetError("service_offline", f"字幕源返回 HTTP {code}", http_status=code) from None
    except (TimeoutError, socket.timeout):
        raise SubNetError("timeout", "字幕源请求超时") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise SubNetError("timeout", "字幕源请求超时") from None
        safe = redact_secrets(str(exc.reason), secrets)
        raise SubNetError("service_offline", f"字幕源不可达: {safe}") from None
    except (OSError, ssl.SSLError) as exc:
        safe = redact_secrets(str(exc), secrets)
        raise SubNetError("service_offline", f"字幕源连接失败: {safe}") from None


async def fetch_https_bytes(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_sec: float = 15.0,
    max_bytes: int = HARD_MAX_DOWNLOAD_BYTES,
    allow_loopback: bool = True,
    ssl_context: ssl.SSLContext | None = None,
    secrets: tuple[str, ...] = (),
) -> HttpBytesResponse:
    """Execute bounded HTTPS GET off the event loop with safe redirect & timeout handling."""
    return await asyncio.to_thread(
        _fetch_https_sync,
        url,
        headers=headers,
        timeout_sec=timeout_sec,
        max_bytes=max_bytes,
        allow_loopback=allow_loopback,
        ssl_context=ssl_context,
        secrets=secrets,
    )
