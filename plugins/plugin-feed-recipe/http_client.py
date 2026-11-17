"""Safe HTTPS HTTP client with timeout, redirect limit, cross-origin secret stripping, and size cap."""

from __future__ import annotations

import asyncio
import http.client
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

if __package__:
    from .security import FeedRecipeError, is_same_origin, redact_sensitive, url_origin, validate_https_url
else:
    from security import FeedRecipeError, is_same_origin, redact_sensitive, url_origin, validate_https_url

MAX_HTTP_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
_CHUNK_SIZE = 65536
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SENSITIVE_REDIRECT_HEADERS = ("cookie", "authorization", "proxy-authorization", "x-api-key")

_UA_PUBLIC = "OmniDesk-FeedRecipe/0.2 (+https://github.com/omnidesk)"
_UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """Normalized outbound HTTP request."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    cookie_header: str = ""
    body: str = ""
    timeout_sec: float = 15.0
    max_redirects: int = DEFAULT_MAX_REDIRECTS


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Normalized HTTP response envelope."""

    status_code: int = 0
    effective_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    error_code: str = ""
    challenge_detected: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and not self.challenge_detected and 200 <= self.status_code < 400


class HttpTransport(Protocol):
    """Protocol for injectable HTTP transport (testing or custom async client)."""

    async def request(
        self,
        req: HttpRequest,
        *,
        extra_secrets: Iterable[str] = (),
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[str] = (),
    ) -> HttpResponse: ...


def detect_challenge(status_code: int, headers: Mapping[str, str], body: str) -> bool:
    """Detect Cloudflare or JS challenge blocks matching v1 CurlHttpClient::detectChallenge."""
    cf_mitigated = headers.get("cf-mitigated", "")
    if "challenge" in cf_mitigated.lower():
        return True
    if status_code in {403, 429, 503}:
        low = body.lower()
        if any(
            marker in low
            for marker in (
                "just a moment...",
                "_cf_chl_opt",
                "cf-browser-verification",
                "enable javascript and cookies to continue",
            )
        ):
            return True
    return False


def map_http_failure_code(status_code: int, *, challenge_detected: bool = False) -> str:
    """Map HTTP status and challenge state into canonical OmniDesk error codes."""
    if challenge_detected:
        return "cloudflare_challenge"
    if status_code in {401, 403}:
        return "auth_required"
    if status_code in {404, 410}:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500 or status_code == 0:
        return "service_offline"
    return "service_offline"


class _ManualRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent urllib from automatically following redirects so each hop is validated."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _sync_single_hop(
    url: str,
    *,
    method: str,
    headers: Mapping[str, str],
    body: str,
    timeout_sec: float,
    max_bytes: int,
    bypass_proxy: bool,
) -> tuple[int, dict[str, str], bytes]:
    handlers: list[urllib.request.BaseHandler] = [_ManualRedirectHandler()]
    if bypass_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    else:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    opener = urllib.request.build_opener(*handlers)

    body_bytes = body.encode("utf-8") if (method == "POST" and body) else None
    req = urllib.request.Request(url, data=body_bytes, method=method)
    for k, v in headers.items():
        req.add_header(k, v)

    deadline = time.monotonic() + max(0.05, timeout_sec)
    try:
        with opener.open(req, timeout=max(0.05, timeout_sec)) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = _read_bounded(resp, resp_headers, max_bytes=max_bytes, deadline=deadline)
            return int(resp.status), resp_headers, raw
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        try:
            raw = _read_bounded(exc, resp_headers, max_bytes=max_bytes, deadline=deadline)
        except FeedRecipeError:
            raw = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        return int(exc.code), resp_headers, raw


def _read_bounded(fp: Any, headers: Mapping[str, str], *, max_bytes: int, deadline: float) -> bytes:
    cl_raw = headers.get("content-length")
    if cl_raw is not None:
        try:
            declared = int(str(cl_raw).strip())
            if declared > max_bytes:
                raise FeedRecipeError(
                    "validation_error",
                    f"响应体积 ({declared} B) 超出安全上限 ({max_bytes} B)",
                )
        except ValueError:
            pass
    buf = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("HTTP 响应读取超时")
        chunk = fp.read(_CHUNK_SIZE)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise FeedRecipeError(
                "validation_error",
                f"响应体积超出安全上限 ({max_bytes} B)",
            )
    return bytes(buf)


class SafeHttpClient:
    """Async HTTPS client enforcing timeout, redirect bounds, origin header stripping, and size cap."""

    def __init__(self, *, max_bytes: int = MAX_HTTP_BYTES) -> None:
        self._max_bytes = max(1024, int(max_bytes))

    async def request(
        self,
        req: HttpRequest,
        *,
        extra_secrets: Iterable[str] = (),
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[str] = (),
    ) -> HttpResponse:
        secrets = list(extra_secrets)
        if req.cookie_header:
            secrets.append(req.cookie_header)

        try:
            initial_url = validate_https_url(
                req.url,
                allow_loopback_http=allow_loopback_http,
                allowed_loopback_origins=allowed_loopback_origins,
            )
        except FeedRecipeError as exc:
            return HttpResponse(
                status_code=0,
                effective_url="",
                error=redact_sensitive(str(exc), secrets),
                error_code=exc.error_code,
            )

        current_url = initial_url
        current_method = (req.method or "GET").strip().upper()
        if current_method not in {"GET", "POST", "HEAD"}:
            return HttpResponse(
                status_code=0,
                effective_url=current_url,
                error=f"不支持的 HTTP 方法: {current_method!r}",
                error_code="invalid_argument",
            )
        max_redirects = max(0, min(10, int(req.max_redirects)))
        timeout_sec = max(0.05, min(120.0, float(req.timeout_sec or 15.0)))
        allowed_set = set(allowed_loopback_origins)

        for hop in range(max_redirects + 1):
            same_origin = is_same_origin(initial_url, current_url)
            hop_headers: dict[str, str] = {}
            has_ua = False
            for k, v in req.headers.items():
                lk = k.strip().lower()
                if not lk or any(c in k or c in v for c in ("\r", "\n", "\x00")):
                    continue
                if not same_origin and lk in _SENSITIVE_REDIRECT_HEADERS:
                    continue
                if lk == "user-agent":
                    has_ua = True
                hop_headers[k] = v
            if not has_ua:
                hop_headers["User-Agent"] = _UA_BROWSER if req.cookie_header else _UA_PUBLIC
            if same_origin and req.cookie_header:
                hop_headers["Cookie"] = req.cookie_header
            if "accept" not in {k.lower() for k in hop_headers}:
                hop_headers["Accept"] = "application/json, text/html, application/xml, */*;q=0.8"

            parsed_host = (urllib.parse.urlsplit(current_url).hostname or "").lower()
            bypass_proxy = parsed_host in _LOOPBACK_HOSTS
            try:
                status, resp_headers, raw_body = await asyncio.to_thread(
                    _sync_single_hop,
                    current_url,
                    method=current_method,
                    headers=hop_headers,
                    body=req.body if current_method == "POST" else "",
                    timeout_sec=timeout_sec,
                    max_bytes=self._max_bytes,
                    bypass_proxy=bypass_proxy,
                )
            except FeedRecipeError as exc:
                return HttpResponse(
                    status_code=0,
                    effective_url=current_url,
                    error=redact_sensitive(str(exc), secrets),
                    error_code=exc.error_code,
                )
            except (TimeoutError, socket.timeout):
                return HttpResponse(
                    status_code=0,
                    effective_url=current_url,
                    error="HTTP 请求超时",
                    error_code="timeout",
                )
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                    return HttpResponse(
                        status_code=0,
                        effective_url=current_url,
                        error="HTTP 请求超时",
                        error_code="timeout",
                    )
                return HttpResponse(
                    status_code=0,
                    effective_url=current_url,
                    error=redact_sensitive(f"网络请求失败: {reason}", secrets),
                    error_code="service_offline",
                )
            except (OSError, http.client.HTTPException) as exc:
                return HttpResponse(
                    status_code=0,
                    effective_url=current_url,
                    error=redact_sensitive(f"HTTP 连接异常: {exc}", secrets),
                    error_code="service_offline",
                )

            location = resp_headers.get("location", "").strip()
            if status in {301, 302, 303, 307, 308} and location:
                if hop >= max_redirects:
                    return HttpResponse(
                        status_code=status,
                        effective_url=current_url,
                        headers=resp_headers,
                        error="超出最大 HTTP 重定向次数限制",
                        error_code="service_offline",
                    )
                next_raw = urllib.parse.urljoin(current_url, location)
                try:
                    next_url = validate_https_url(
                        next_raw,
                        allow_loopback_http=allow_loopback_http,
                        allowed_loopback_origins=allowed_set,
                        field_name="redirect_url",
                    )
                except FeedRecipeError as exc:
                    return HttpResponse(
                        status_code=status,
                        effective_url=current_url,
                        headers=resp_headers,
                        error=redact_sensitive(f"重定向目标被安全拦截: {exc}", secrets),
                        error_code=exc.error_code,
                    )
                if status == 303 or (current_method == "POST" and status in {301, 302}):
                    current_method = "GET"
                current_url = next_url
                continue

            body_text = raw_body.decode("utf-8", errors="replace")
            challenge = detect_challenge(status, resp_headers, body_text)
            if challenge or status >= 400:
                err_code = map_http_failure_code(status, challenge_detected=challenge)
                msg = f"HTTP {status}" + (" (JS/Cloudflare challenge)" if challenge else "")
                return HttpResponse(
                    status_code=status,
                    effective_url=current_url,
                    headers=resp_headers,
                    body=body_text,
                    error=redact_sensitive(msg, secrets),
                    error_code=err_code,
                    challenge_detected=challenge,
                )

            return HttpResponse(
                status_code=status,
                effective_url=current_url,
                headers=resp_headers,
                body=body_text,
                error="",
                error_code="",
                challenge_detected=False,
            )

        return HttpResponse(
            status_code=0,
            effective_url=current_url,
            error="超出最大 HTTP 重定向次数限制",
            error_code="service_offline",
        )


__all__ = [
    "DEFAULT_MAX_REDIRECTS",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "MAX_HTTP_BYTES",
    "SafeHttpClient",
    "detect_challenge",
    "map_http_failure_code",
]
