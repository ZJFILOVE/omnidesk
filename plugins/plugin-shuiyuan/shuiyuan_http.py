"""Hardened HTTPS-only HTTP client with bounded redirects, payload size cap, and SSO/Cloudflare detection."""

from __future__ import annotations

import asyncio
import http.client
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

if __package__:
    from .shuiyuan_security import (
        LOOPBACK_HOSTS,
        ShuiyuanError,
        is_same_origin,
        redact_sensitive,
        resolve_url,
        validate_https_url,
    )
else:
    from shuiyuan_security import (
        LOOPBACK_HOSTS,
        ShuiyuanError,
        is_same_origin,
        redact_sensitive,
        resolve_url,
        validate_https_url,
    )

MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
_CHUNK_SIZE = 65536
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class HttpRequest:
    """Normalized HTTP request envelope."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    cookie_header: str = ""
    timeout_sec: float = 15.0
    max_redirects: int = MAX_REDIRECTS
    max_bytes: int = MAX_HTTP_BYTES
    binary: bool = False


@dataclass(slots=True)
class HttpResponse:
    """Bounded HTTP response with SSO and anti-bot challenge flags."""

    status_code: int = 0
    effective_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    body_bytes: bytes = b""
    error: str = ""
    error_code: str = ""
    redirected_cross_host: bool = False
    sso_redirect_detected: bool = False
    challenge_detected: bool = False

    @property
    def is_success(self) -> bool:
        return not self.error and 200 <= self.status_code < 300


class HttpClientProtocol(Protocol):
    """Protocol implemented by SafeHttpClient and offline test mocks."""

    async def request(self, req: HttpRequest, *, extra_secrets: Iterable[str] = ()) -> HttpResponse: ...


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses directly so our loop enforces HTTPS, origin stripping, and SSO checks."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def is_sso_or_login_url(url: str) -> bool:
    """Return True if URL points to SJTU jAccount SSO or Discourse login wall."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host == "jaccount.sjtu.edu.cn" or "jaccount." in host:
        return True
    path = (parsed.path or "").lower()
    return (
        path == "/login"
        or path.startswith("/login?")
        or path.startswith("/session/sso")
        or path.startswith("/oauth2/authorize")
    )


def detect_challenge(status_code: int, headers: Mapping[str, str], body: str) -> bool:
    """Detect Cloudflare or anti-bot interstitial challenges."""
    cf_mitigated = str(headers.get("cf-mitigated") or "").lower()
    if "challenge" in cf_mitigated:
        return True
    if status_code in {200, 403, 429, 503}:
        low = body.lower()
        markers = (
            "just a moment...",
            "_cf_chl_opt",
            "cf-browser-verification",
            "cf-turnstile",
            "enable javascript and cookies to continue",
            "checking your browser before accessing",
        )
        if any(marker in low for marker in markers):
            return True
    return False


def detect_sso_or_login_wall(
    status_code: int,
    effective_url: str,
    headers: Mapping[str, str],
    body: str,
) -> bool:
    """Detect 401/403, jAccount SSO redirects, HTML login walls, or Discourse not_logged_in JSON."""
    if status_code in {401, 403}:
        return True
    if is_sso_or_login_url(effective_url):
        return True
    loc = str(headers.get("location") or "")
    if loc and is_sso_or_login_url(loc):
        return True
    ct = str(headers.get("content-type") or "").lower()
    low = body.lower()
    if "text/html" in ct and any(
        marker in low for marker in ("jaccount.sjtu.edu.cn", "oauth2/authorize", "login-required")
    ):
        return True
    if any(
        marker in low
        for marker in ('"error_type":"not_logged_in"', '"error_type": "not_logged_in"', '"not_logged_in"', "you need to be logged in")
    ):
        return True
    return False


def _read_bounded_body(stream: Any, *, max_bytes: int, deadline: float, binary: bool = False) -> str | bytes:
    declared = getattr(stream, "headers", {}).get("Content-Length") if hasattr(stream, "headers") else None
    if declared is not None:
        try:
            if int(str(declared).strip()) > max_bytes:
                raise ShuiyuanError("validation_error", f"响应体积超出 {max_bytes} 字节安全上限")
        except ValueError:
            pass
    buf = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("HTTP 读取超时")
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ShuiyuanError("validation_error", f"响应体积超出 {max_bytes} 字节安全上限")
    return bytes(buf) if binary else bytes(buf).decode("utf-8", errors="replace")


class SafeHttpClient:
    """Hardened stdlib HTTPS client executing bounded requests in a worker thread."""

    def __init__(
        self,
        *,
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[str] = (),
    ) -> None:
        self._allow_loopback_http = allow_loopback_http
        self._allowed_loopback_origins = tuple(allowed_loopback_origins)

    def _allowed_origins(self, initial_url: str) -> set[str]:
        origins = set(self._allowed_loopback_origins)
        if self._allow_loopback_http and initial_url.startswith("http://"):
            parsed = urllib.parse.urlsplit(initial_url)
            if (parsed.hostname or "").lower() in LOOPBACK_HOSTS:
                port = f":{parsed.port}" if parsed.port else ""
                origins.add(f"http://{(parsed.hostname or '').lower()}{port}")
        return origins

    def _sync_request(self, req: HttpRequest, secrets: tuple[str, ...]) -> HttpResponse:
        try:
            initial_url = validate_https_url(
                req.url,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self._allowed_origins(req.url),
            )
        except ShuiyuanError as exc:
            return HttpResponse(error=redact_sensitive(str(exc), secrets), error_code=exc.error_code)

        allowed_origins = self._allowed_origins(initial_url)
        current_url = initial_url
        current_method = (req.method or "GET").upper()
        max_hops = max(0, min(int(req.max_redirects), MAX_REDIRECTS))
        timeout_sec = max(0.1, min(float(req.timeout_sec), 120.0))
        deadline = time.monotonic() + timeout_sec
        redirected_cross_host = False

        for hop in range(max_hops + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return HttpResponse(effective_url=current_url, error="HTTP 请求超时", error_code="timeout")

            same_origin = is_same_origin(initial_url, current_url)
            if not same_origin:
                redirected_cross_host = True

            parsed = urllib.parse.urlsplit(current_url)
            is_loopback = (parsed.hostname or "").lower() in LOOPBACK_HOSTS
            handlers: list[urllib.request.BaseHandler] = [_NoAutoRedirect()]
            if is_loopback:
                handlers.append(urllib.request.ProxyHandler({}))
            opener = urllib.request.build_opener(*handlers)

            u_req = urllib.request.Request(current_url, method=current_method)
            has_ua = False
            has_accept = False
            for k, v in req.headers.items():
                lk = k.lower()
                if not same_origin and lk in {"cookie", "authorization"}:
                    continue
                if lk == "user-agent":
                    has_ua = True
                elif lk == "accept":
                    has_accept = True
                u_req.add_header(k, v)
            if not has_ua:
                u_req.add_header("User-Agent", _DEFAULT_UA)
            if not has_accept:
                u_req.add_header("Accept", "application/json, text/plain, */*")
            if same_origin and req.cookie_header:
                u_req.add_header("Cookie", req.cookie_header)

            status_code = 0
            resp_headers: dict[str, str] = {}
            body = ""
            try:
                with opener.open(u_req, timeout=remaining) as resp:
                    status_code = int(getattr(resp, "status", 200))
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    body = _read_bounded_body(resp, max_bytes=req.max_bytes, deadline=deadline, binary=req.binary)
            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
                resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
                try:
                    body = _read_bounded_body(exc, max_bytes=req.max_bytes, deadline=deadline)
                except (TimeoutError, socket.timeout):
                    return HttpResponse(
                        status_code=status_code,
                        effective_url=current_url,
                        error="HTTP 请求超时",
                        error_code="timeout",
                    )
                except ShuiyuanError as size_exc:
                    return HttpResponse(
                        status_code=status_code,
                        effective_url=current_url,
                        error=redact_sensitive(str(size_exc), secrets),
                        error_code=size_exc.error_code,
                    )
                except Exception:
                    body = ""
            except (TimeoutError, socket.timeout):
                return HttpResponse(effective_url=current_url, error="HTTP 请求超时", error_code="timeout")
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                    return HttpResponse(effective_url=current_url, error="HTTP 请求超时", error_code="timeout")
                return HttpResponse(
                    effective_url=current_url,
                    error=redact_sensitive(f"网络请求失败: {reason}", secrets),
                    error_code="service_offline",
                )
            except ShuiyuanError as exc:
                return HttpResponse(
                    effective_url=current_url,
                    error=redact_sensitive(str(exc), secrets),
                    error_code=exc.error_code,
                )
            except (http.client.HTTPException, OSError) as exc:
                return HttpResponse(
                    effective_url=current_url,
                    error=redact_sensitive(f"HTTP 连接异常: {exc}", secrets),
                    error_code="service_offline",
                )

            if status_code in {301, 302, 303, 307, 308} and "location" in resp_headers:
                loc_raw = resp_headers["location"]
                next_url = resolve_url(
                    current_url,
                    loc_raw,
                    allow_loopback_http=self._allow_loopback_http,
                    allowed_loopback_origins=allowed_origins,
                )
                if not next_url:
                    return HttpResponse(
                        status_code=status_code,
                        effective_url=current_url,
                        error="重定向目标使用了不受支持的非 HTTPS 协议",
                        error_code="invalid_argument",
                    )
                if not is_same_origin(initial_url, next_url):
                    redirected_cross_host = True
                if is_sso_or_login_url(next_url):
                    return HttpResponse(
                        status_code=status_code,
                        effective_url=next_url,
                        headers=resp_headers,
                        redirected_cross_host=redirected_cross_host,
                        sso_redirect_detected=True,
                    )
                if hop < max_hops:
                    if status_code == 303 or (current_method == "POST" and status_code in {301, 302}):
                        current_method = "GET"
                    current_url = next_url
                    continue
                return HttpResponse(
                    status_code=status_code,
                    effective_url=current_url,
                    headers=resp_headers,
                    error="HTTP 重定向次数过多",
                    error_code="service_offline",
                    redirected_cross_host=redirected_cross_host,
                )

            body_bytes = body if isinstance(body, bytes) else b""
            text_body = body.decode('utf-8', errors='replace') if isinstance(body, bytes) else body
            challenge = detect_challenge(status_code, resp_headers, text_body)
            sso = detect_sso_or_login_wall(status_code, current_url, resp_headers, text_body)
            return HttpResponse(
                status_code=status_code,
                effective_url=current_url,
                headers=resp_headers,
                body="" if req.binary else text_body,
                body_bytes=body_bytes,
                redirected_cross_host=redirected_cross_host,
                sso_redirect_detected=sso,
                challenge_detected=challenge,
            )

        return HttpResponse(
            effective_url=current_url,
            error="HTTP 重定向次数过多",
            error_code="service_offline",
            redirected_cross_host=redirected_cross_host,
        )

    async def request(self, req: HttpRequest, *, extra_secrets: Iterable[str] = ()) -> HttpResponse:
        """Execute a bounded HTTPS request asynchronously without blocking the event loop."""
        secrets = tuple(s for s in extra_secrets if isinstance(s, str) and s)
        if req.cookie_header:
            secrets = (*secrets, req.cookie_header)
        return await asyncio.to_thread(self._sync_request, req, secrets)
