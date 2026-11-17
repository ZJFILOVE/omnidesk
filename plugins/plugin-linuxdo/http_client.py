"""Hardened HTTPS client with redirect/size limits, credential stripping, and CF/login detection."""

from __future__ import annotations

import asyncio
import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

if __package__:
    from .security import (
        LOOPBACK_HOSTS,
        LinuxdoError,
        is_same_origin,
        redact_text,
        resolve_relative_url,
        url_origin,
        validate_https_url,
    )
else:
    from security import (  # type: ignore[no-redef]
        LOOPBACK_HOSTS,
        LinuxdoError,
        is_same_origin,
        redact_text,
        resolve_relative_url,
        url_origin,
        validate_https_url,
    )

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
_CHUNK_SIZE = 65536
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_AUTH_STRIP_HEADERS = frozenset({"cookie", "authorization", "x-api-key", "proxy-authorization"})


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """Structured outbound HTTP request."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    cookie_header: str = ""
    body: str = ""
    timeout_sec: float = 15.0
    max_redirects: int = MAX_REDIRECTS


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Structured HTTP response with security & challenge telemetry."""

    status_code: int = 0
    effective_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    error_code: str = ""
    transport: str = "urllib"
    redirected_cross_host: bool = False
    sso_redirect_detected: bool = False
    challenge_detected: bool = False

    @property
    def is_success(self) -> bool:
        return not self.error and 200 <= self.status_code < 300


class HttpClientProtocol(Protocol):
    """Protocol for async HTTP clients (real HTTPS or test transport)."""

    async def request(
        self,
        req: HttpRequest,
        *,
        secrets: Iterable[str] = (),
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[tuple[str, str, int]] = (),
    ) -> HttpResponse: ...


def is_login_url(url: str) -> bool:
    """Return True when URL points to Linux.do login or SSO/OAuth endpoints."""
    try:
        scheme, host, _ = url_origin(url)
    except ValueError:
        return False
    if scheme not in {"http", "https"}:
        return False
    if host == "connect.linux.do":
        return True
    path = urllib.parse.urlsplit(url).path.lower()
    return (
        path == "/login"
        or path.startswith("/session/sso")
        or path.startswith("/auth/")
        or path.startswith("/oauth2/authorize")
    )


def detect_challenge(status_code: int, headers: Mapping[str, str], body: str) -> bool:
    """Detect Cloudflare TLS fingerprint block, Turnstile, or JS challenge pages."""
    lower_hdrs = {k.lower(): v for k, v in headers.items()}
    if "challenge" in lower_hdrs.get("cf-mitigated", "").lower():
        return True
    has_cf_header = ("cf-ray" in lower_hdrs) or ("cloudflare" in lower_hdrs.get("server", "").lower())
    low_body = body.lower()

    if status_code in {403, 429, 503}:
        if has_cf_header and '"error_type":"not_logged_in"' not in low_body and '"error_type":"invalid_access"' not in low_body:
            return True
        for marker in (
            "just a moment...",
            "_cf_chl_opt",
            "cf-browser-verification",
            "enable javascript and cookies to continue",
            "attention required!",
            "challenges.cloudflare.com",
            "error code: 1020",
            "access denied",
        ):
            if marker in low_body:
                return True

    if any(m in low_body for m in ("_cf_chl_opt", "cf-browser-verification", "challenges.cloudflare.com/turnstile")):
        return True

    looks_like_discourse_json = any(k in low_body for k in ('"post_stream"', '"topic_list"', '"category_list"'))
    if not looks_like_discourse_json:
        if ("cf-turnstile-response" in low_body and "cf-chl-widget-" in low_body) or any(
            m in low_body
            for m in (
                "/cdn-cgi/challenge-platform/",
                "verification successful. waiting for",
                "verifying you are human",
            )
        ):
            return True
    return False


def detect_login_wall(
    status_code: int,
    effective_url: str,
    headers: Mapping[str, str],
    body: str,
) -> bool:
    """Detect HTTP 401, login/SSO redirect, or Discourse not_logged_in payload."""
    if status_code == 401:
        return True
    if is_login_url(effective_url):
        return True
    lower_hdrs = {k.lower(): v for k, v in headers.items()}
    loc = lower_hdrs.get("location", "")
    if loc and is_login_url(resolve_relative_url(effective_url, loc)):
        return True
    low_body = body.lower()
    if any(
        m in low_body
        for m in (
            '"error_type":"not_logged_in"',
            '"error_type": "not_logged_in"',
            '"error_type":"invalid_access"',
            '"not_logged_in"',
            "you need to be logged in",
        )
    ):
        return True
    is_html = "text/html" in lower_hdrs.get("content-type", "").lower()
    if is_html and not detect_challenge(status_code, lower_hdrs, body):
        if "login-required" in low_body or "connect.linux.do" in low_body:
            return True
    return False


class _RedirectTracker(Exception):
    """Internal control-flow signal when redirect must stop immediately (e.g. SSO login)."""

    def __init__(self, *, status_code: int, effective_url: str, headers: dict[str, str], sso: bool) -> None:
        self.status_code = status_code
        self.effective_url = effective_url
        self.headers = headers
        self.sso = sso
        super().__init__(effective_url)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validates redirect URLs, caps hops, detects SSO walls, and strips cross-origin credentials."""

    def __init__(
        self,
        *,
        initial_url: str,
        max_redirects: int,
        allow_loopback_http: bool,
        allowed_loopback_origins: set[tuple[str, str, int]],
    ) -> None:
        super().__init__()
        self._initial_url = initial_url
        self._max_redirects = max(0, max_redirects)
        self._allow_loopback_http = allow_loopback_http
        self._allowed_origins = allowed_loopback_origins
        self._hops = 0
        self.cross_origin_seen = False

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        resolved = resolve_relative_url(req.full_url, newurl)
        try:
            safe_next = validate_https_url(
                resolved,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self._allowed_origins,
            )
        except LinuxdoError as exc:
            raise urllib.error.URLError(exc) from exc

        if not is_same_origin(self._initial_url, safe_next):
            self.cross_origin_seen = True

        resp_headers = {k.lower(): v for k, v in headers.items()}
        if is_login_url(safe_next):
            raise _RedirectTracker(
                status_code=code,
                effective_url=safe_next,
                headers=resp_headers,
                sso=True,
            )

        self._hops += 1
        if self._hops > self._max_redirects:
            raise urllib.error.URLError(LinuxdoError("service_offline", "Too many redirects"))

        next_req = super().redirect_request(req, fp, code, msg, headers, safe_next)
        if next_req is None:
            return None
        if not is_same_origin(self._initial_url, safe_next):
            for hdr_map in (next_req.headers, next_req.unredirected_hdrs):
                for key in list(hdr_map.keys()):
                    if key.lower() in _AUTH_STRIP_HEADERS:
                        hdr_map.pop(key, None)
        return next_req


def _sync_request(
    req: HttpRequest,
    *,
    secrets: tuple[str, ...],
    allow_loopback_http: bool,
    allowed_loopback_origins: set[tuple[str, str, int]],
) -> HttpResponse:
    try:
        safe_url = validate_https_url(
            req.url,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
    except LinuxdoError as exc:
        return HttpResponse(error=redact_text(str(exc), secrets), error_code=exc.error_code)

    redirect_handler = _SafeRedirectHandler(
        initial_url=safe_url,
        max_redirects=req.max_redirects,
        allow_loopback_http=allow_loopback_http,
        allowed_loopback_origins=allowed_loopback_origins,
    )
    handlers: list[urllib.request.BaseHandler] = [redirect_handler]
    parsed = urllib.parse.urlsplit(safe_url)
    if (parsed.hostname or "").lower() in LOOPBACK_HOSTS:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    ssl_ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx))
    opener = urllib.request.build_opener(*handlers)

    headers: dict[str, str] = {}
    has_ua = False
    has_accept = False
    for k, v in req.headers.items():
        lk = k.lower()
        if lk == "user-agent":
            has_ua = True
        elif lk == "accept":
            has_accept = True
        headers[k] = v
    if not has_ua:
        headers["User-Agent"] = _DEFAULT_UA
    if not has_accept:
        headers["Accept"] = "application/json, text/plain, */*"
    if req.cookie_header:
        headers["Cookie"] = req.cookie_header

    method = (req.method or "GET").upper()
    data = req.body.encode("utf-8") if method == "POST" and req.body else None
    url_req = urllib.request.Request(safe_url, data=data, headers=headers, method=method)
    timeout = max(0.05, float(req.timeout_sec))
    deadline = time.monotonic() + timeout

    def _read_bounded(resp: Any, status_code: int) -> str:
        resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
        cl_raw = resp_hdrs.get("content-length")
        if cl_raw is not None:
            try:
                if int(cl_raw.strip()) > MAX_RESPONSE_BYTES:
                    raise LinuxdoError("invalid_response", "上游响应体积超过 8 MiB 安全上限", http_status=status_code)
            except ValueError:
                pass
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP read timed out")
            chunk = resp.read(_CHUNK_SIZE)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_RESPONSE_BYTES:
                raise LinuxdoError("invalid_response", "上游响应体积超过 8 MiB 安全上限", http_status=status_code)
        return buf.decode("utf-8", errors="replace")

    try:
        with opener.open(url_req, timeout=timeout) as resp:
            status_code = int(resp.status)
            eff_url = str(resp.geturl() or safe_url)
            resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
            body_text = _read_bounded(resp, status_code)
    except _RedirectTracker as redir:
        return HttpResponse(
            status_code=redir.status_code,
            effective_url=redir.effective_url,
            headers=redir.headers,
            redirected_cross_host=redirect_handler.cross_origin_seen,
            sso_redirect_detected=redir.sso,
            challenge_detected=False,
        )
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        eff_url = str(exc.geturl() or safe_url)
        resp_hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        try:
            body_bytes = exc.read(MAX_RESPONSE_BYTES + 1)
            body_text = body_bytes[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
    except (TimeoutError, socket.timeout):
        return HttpResponse(
            error="Linux.do HTTP 请求超时",
            error_code="timeout",
            redirected_cross_host=redirect_handler.cross_origin_seen,
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, LinuxdoError):
            return HttpResponse(
                error=redact_text(str(reason), secrets),
                error_code=reason.error_code,
                redirected_cross_host=redirect_handler.cross_origin_seen,
            )
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            return HttpResponse(
                error="Linux.do HTTP 请求超时",
                error_code="timeout",
                redirected_cross_host=redirect_handler.cross_origin_seen,
            )
        return HttpResponse(
            error=redact_text(f"Linux.do 网络请求失败: {reason}", secrets),
            error_code="service_offline",
            redirected_cross_host=redirect_handler.cross_origin_seen,
        )
    except LinuxdoError as exc:
        return HttpResponse(
            status_code=exc.http_status,
            error=redact_text(str(exc), secrets),
            error_code=exc.error_code,
            redirected_cross_host=redirect_handler.cross_origin_seen,
        )
    except OSError as exc:
        return HttpResponse(
            error=redact_text(f"Linux.do 连接失败: {exc}", secrets),
            error_code="service_offline",
            redirected_cross_host=redirect_handler.cross_origin_seen,
        )

    challenge = detect_challenge(status_code, resp_hdrs, body_text)
    login_wall = detect_login_wall(status_code, eff_url, resp_hdrs, body_text)
    return HttpResponse(
        status_code=status_code,
        effective_url=eff_url,
        headers=resp_hdrs,
        body=body_text,
        redirected_cross_host=redirect_handler.cross_origin_seen,
        sso_redirect_detected=login_wall,
        challenge_detected=challenge,
    )


class SafeHttpsClient:
    """Async wrapper over hardened urllib HTTPS requests."""

    async def request(
        self,
        req: HttpRequest,
        *,
        secrets: Iterable[str] = (),
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[tuple[str, str, int]] = (),
    ) -> HttpResponse:
        secret_tuple = tuple(s for s in secrets if s)
        origin_set = set(allowed_loopback_origins)
        return await asyncio.to_thread(
            _sync_request,
            req,
            secrets=secret_tuple,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=origin_set,
        )
