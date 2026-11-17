"""Hardened HTTPS client reusing omnidesk_sdk.http.SafeHttpClient, with Chrome fallbacks."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from omnidesk_sdk import PluginContext
from omnidesk_sdk.http import (
    HttpTimeoutError,
    ResponseTooLargeError,
    SafeHttpClient,
    SafeHttpError,
    is_localhost_host,
    url_origin,
)

if __package__:
    from .reddit_auth import AuthManager, AuthResolution
    from .reddit_security import (
        MAX_REDIRECTS,
        MAX_RESPONSE_BYTES,
        SENSITIVE_HEADERS,
        RedditPluginError,
        ensure_raw_json_param,
        is_same_or_trusted_reddit_host,
        normalize_user_agent,
        origin_str,
        redact_reddit_secrets,
        scrub_url_for_log,
        validate_https_url,
    )
else:
    from reddit_auth import AuthManager, AuthResolution
    from reddit_security import (
        MAX_REDIRECTS,
        MAX_RESPONSE_BYTES,
        SENSITIVE_HEADERS,
        RedditPluginError,
        ensure_raw_json_param,
        is_same_or_trusted_reddit_host,
        normalize_user_agent,
        origin_str,
        redact_reddit_secrets,
        scrub_url_for_log,
        validate_https_url,
    )

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_FALLBACK_ELIGIBLE_CODES = frozenset(
    {"auth_required", "cloudflare_blocked", "permission_denied", "rate_limited", "service_offline", "parse_error", "not_found"}
)
_CLOUDFLARE_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "cf-challenge",
    "/cdn-cgi/challenge-platform",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "you've been blocked by network security",
)
_LOGIN_MARKERS = (
    "log in to reddit",
    "sign in to continue",
    "reddit.com/login",
    "/account/login",
)


@dataclass(slots=True)
class HttpRequestSpec:
    """Outgoing HTTP request specification."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout_sec: float = 15.0
    follow_redirects: bool = True
    max_redirects: int = MAX_REDIRECTS


@dataclass(slots=True)
class HttpResponse:
    """Bounded HTTP response or structured failure."""

    ok: bool = False
    status_code: int = 0
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    error_code: str = ""
    transport: str = "sdk_http"


def _find_header_ci(headers: Mapping[str, str], name: str) -> str:
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return ""


def _strip_sensitive_headers(headers: dict[str, str], *, keep_cookie: bool = False) -> None:
    for k in list(headers):
        low = k.lower()
        if low in SENSITIVE_HEADERS:
            if keep_cookie and low in {"cookie", "cookie2"}:
                continue
            headers.pop(k, None)


def _is_cloudflare_response(status_code: int, headers: Mapping[str, str], body: str) -> bool:
    cf_mitigated = _find_header_ci(headers, "cf-mitigated").lower()
    if "challenge" in cf_mitigated:
        return True
    head = body[:8192].lower()
    if any(marker in head for marker in _CLOUDFLARE_MARKERS):
        return True
    if status_code in {403, 503} and "cloudflare" in head and ("ray id" in head or "cf-ray" in headers):
        return True
    return False


def _is_login_wall(final_url: str, body: str) -> bool:
    low_url = (final_url or "").lower()
    if "/login" in low_url or "/account/login" in low_url:
        return True
    head = body[:4096].lower()
    return ("<html" in head or "<!doctype html" in head) and any(m in head for m in _LOGIN_MARKERS)


def classify_http_failure(
    resp: HttpResponse,
    *,
    expect_json: bool = True,
    is_authenticated: bool = False,
) -> tuple[str, str]:
    """Return `(error_code, human_message)` if `resp` is blocked, error status, or invalid; else `('', '')`."""
    if resp.error_code:
        return resp.error_code, resp.error or f"Reddit 请求失败 ({resp.error_code})"

    status = resp.status_code
    if _is_cloudflare_response(status, resp.headers, resp.body):
        return "cloudflare_blocked", f"请求被 Cloudflare / 反爬验证拦截 (HTTP {status or 403})"

    if status == 401 or (status < 400 and _is_login_wall(resp.final_url, resp.body)):
        return "auth_required", f"Reddit 需要有效登录会话 (HTTP {status or 401})"

    if status == 429:
        return "rate_limited", "Reddit API 请求触发限流 (HTTP 429)"

    if status == 403:
        code = "permission_denied" if is_authenticated else "auth_required"
        msg = (
            "Reddit 拒绝访问此内容或私有版块 (HTTP 403)"
            if is_authenticated
            else "Reddit 访问受限，请先完成登录或使用 Chrome 代理 (HTTP 403)"
        )
        return code, msg

    if status in {404, 410}:
        return "not_found", f"请求的 Reddit 资源不存在 (HTTP {status})"

    if status >= 500:
        return "service_offline", f"Reddit 上游服务异常 (HTTP {status})"

    if not resp.ok or (status != 0 and status >= 400):
        return "service_offline", resp.error or f"Reddit HTTP 错误 ({status})"

    trimmed = resp.body.strip()
    if not trimmed:
        return "parse_error", "Reddit 返回空响应体"

    if expect_json:
        head = trimmed[:512].lower()
        if trimmed[0] not in "{[":
            if _is_cloudflare_response(status, resp.headers, resp.body):
                return "cloudflare_blocked", "Reddit 返回了 Cloudflare 验证页而非 JSON"
            return "parse_error", "Reddit 返回了非 JSON 响应（可能是风控或畸形 HTML）"
        if '"error": 429' in head or '"error":429' in head or "whoa there, pardner" in head:
            return "rate_limited", "Reddit API 返回 429 限流"
        if '"error": 403' in head or '"error":403' in head:
            return ("permission_denied" if is_authenticated else "auth_required"), "Reddit API 返回 403 拒绝访问"
        if '"error": 401' in head or '"error":401' in head:
            return "auth_required", "Reddit API 返回 401 未登录"
    return "", ""


def is_reddit_blocked_or_rate_limited(resp: HttpResponse, *, expect_json: bool = True) -> bool:
    """Return True if response indicates auth wall, Cloudflare, rate limit, or server error."""
    if resp.status_code in {401, 403, 429, 503} or resp.status_code >= 500:
        return True
    if not resp.ok:
        return False
    code, _ = classify_http_failure(resp, expect_json=expect_json)
    return bool(code)


def _extract_path_and_query(path_or_url: str) -> str:
    work = path_or_url.strip()
    low = work.lower()
    if low.startswith(("https://", "http://")):
        parsed = urllib.parse.urlsplit(work)
        pq = parsed.path or "/"
        if parsed.query:
            pq += f"?{parsed.query}"
        return pq
    return work if work.startswith("/") else f"/{work}"


def _to_html_extract_url(url: str) -> str:
    """Convert a `.json?...` endpoint URL to its corresponding Reddit HTML URL for `web.extract.v1`."""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    if path.endswith("/.json"):
        path = path[:-6] + "/"
    elif path.endswith(".json"):
        path = path[:-5]
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != "raw_json"]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path or "/", urllib.parse.urlencode(pairs), ""))


def _extract_charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        piece = part.strip()
        if piece.lower().startswith("charset="):
            cs = piece.split("=", 1)[1].strip().strip("\"'")
            if cs:
                return cs
    return "utf-8"


def _decode_bytes(raw: bytes, headers: Mapping[str, str]) -> str:
    charset = _extract_charset(headers.get("content-type", ""))
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class RedditHttpClient:
    """Async Reddit HTTP client backed by `omnidesk_sdk.http.SafeHttpClient` and Chrome fallbacks."""

    def __init__(
        self,
        *,
        api_base_url: str = "",
        allow_loopback_http: bool = False,
    ) -> None:
        self._ctx: PluginContext | None = None
        self._api_base_url = api_base_url.rstrip("/") if api_base_url else ""
        self._allow_loopback_http = bool(allow_loopback_http)
        self._transport_override: Callable[[HttpRequestSpec], HttpResponse] | None = None
        self._default_timeout_sec: float = 15.0
        self._user_agent: str = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def set_transport_override(self, fn: Callable[[HttpRequestSpec], HttpResponse] | None) -> None:
        self._transport_override = fn

    def set_default_timeout_sec(self, sec: float) -> None:
        self._default_timeout_sec = max(0.1, min(120.0, float(sec)))

    def set_user_agent(self, ua: str) -> None:
        self._user_agent = normalize_user_agent(ua)

    def user_agent(self) -> str:
        return self._user_agent or normalize_user_agent("")

    def allowed_loopback_origins(self) -> frozenset[str]:
        if self._api_base_url and self._api_base_url.startswith(("http://", "https://")):
            try:
                _, host, _ = url_origin(self._api_base_url)
                if is_localhost_host(host):
                    return frozenset({origin_str(self._api_base_url)})
            except ValueError:
                pass
        return frozenset()

    def _has_cap(self, cap_id: str) -> bool:
        return self._ctx is not None and self._ctx.capabilities.has(cap_id)

    def _prepare_request(self, spec: HttpRequestSpec) -> tuple[str, dict[str, str], float, int]:
        allowed_origins = self.allowed_loopback_origins()
        safe_url = validate_https_url(
            spec.url,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=allowed_origins,
        )
        effective_ua = _find_header_ci(spec.headers, "user-agent")
        effective_ua = normalize_user_agent(effective_ua) if effective_ua else self.user_agent()
        req_headers = dict(spec.headers)
        req_headers["User-Agent"] = effective_ua
        if not _find_header_ci(req_headers, "accept"):
            req_headers["Accept"] = "application/json, text/plain;q=0.8, */*;q=0.5"
        timeout = max(0.1, float(spec.timeout_sec or self._default_timeout_sec))
        max_hops = max(0, spec.max_redirects) if spec.follow_redirects else 0
        return safe_url, req_headers, timeout, max_hops

    def _apply_redirect_hop(
        self,
        *,
        current_url: str,
        current_method: str,
        current_body: str,
        headers: dict[str, str],
        status_code: int,
        location: str,
        initial_is_localhost: bool,
        allowed_origins: frozenset[str],
    ) -> tuple[str, str, str]:
        if not location:
            raise RedditPluginError("blocked_redirect", f"HTTP {status_code} 重定向缺少 Location 头")
        next_raw = urllib.parse.urljoin(current_url, location)
        # Never allow a non-localhost request to redirect onto a loopback test endpoint.
        allow_lb = self._allow_loopback_http and initial_is_localhost
        safe_next = validate_https_url(
            next_raw,
            allow_loopback_http=allow_lb,
            allowed_loopback_origins=allowed_origins if initial_is_localhost else (),
        )
        same_origin = url_origin(current_url) == url_origin(safe_next)
        if not same_origin:
            trusted_reddit = is_same_or_trusted_reddit_host(current_url, safe_next)
            _strip_sensitive_headers(headers, keep_cookie=trusted_reddit)
        if status_code == 303 or (status_code in {301, 302} and current_method in {"POST", "PUT", "DELETE"}):
            return safe_next, "GET", ""
        return safe_next, current_method, current_body

    async def perform_direct(self, spec: HttpRequestSpec, *, extra_secrets: tuple[str, ...] = ()) -> HttpResponse:
        """Perform bounded HTTPS-only direct request via SDK SafeHttpClient with redirect hygiene."""
        allowed_origins = self.allowed_loopback_origins()
        try:
            current_url, req_headers, timeout, max_hops = self._prepare_request(spec)
        except RedditPluginError as exc:
            return HttpResponse(
                ok=False,
                error=redact_reddit_secrets(str(exc), extra_secrets),
                error_code=exc.error_code,
            )

        initial_is_localhost = is_localhost_host(url_origin(current_url)[1])
        current_method = (spec.method or "GET").strip().upper()
        current_body = spec.body or ""
        deadline = time.monotonic() + timeout

        for hop in range(max_hops + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return HttpResponse(ok=False, error="Reddit HTTP 请求超时", error_code="timeout")

            if self._transport_override is not None:
                cur_spec = HttpRequestSpec(
                    url=current_url,
                    method=current_method,
                    headers=dict(req_headers),
                    body=current_body,
                    timeout_sec=remaining,
                    follow_redirects=spec.follow_redirects,
                    max_redirects=spec.max_redirects,
                )
                resp = self._transport_override(cur_spec)
                if not resp.transport:
                    resp.transport = "mock"
                if not resp.final_url:
                    resp.final_url = current_url
            else:
                client = SafeHttpClient(
                    allow_localhost_http=self._allow_loopback_http and initial_is_localhost,
                    allow_private=False,
                    timeout_sec=max(0.05, remaining),
                    max_bytes=MAX_RESPONSE_BYTES,
                    follow_redirects=spec.follow_redirects,
                    max_redirects=spec.max_redirects,
                    raise_for_status=False,
                    secrets=extra_secrets,
                )
                try:
                    sdk_resp = await client.request(
                        current_method,
                        current_url,
                        headers=req_headers,
                        content=current_body if (current_method == "POST" and current_body) else None,
                    )
                except HttpTimeoutError as exc:
                    return HttpResponse(
                        ok=False,
                        error=redact_reddit_secrets(exc.message or "Reddit HTTP 请求超时", extra_secrets),
                        error_code="timeout",
                        transport="sdk_http",
                    )
                except ResponseTooLargeError as exc:
                    return HttpResponse(
                        ok=False,
                        status_code=exc.status_code or 0,
                        error=redact_reddit_secrets(exc.message, extra_secrets),
                        error_code="too_large",
                        transport="sdk_http",
                    )
                except SafeHttpError as exc:
                    return HttpResponse(
                        ok=False,
                        status_code=exc.status_code or 0,
                        error=redact_reddit_secrets(exc.message, extra_secrets),
                        error_code=exc.error_code,
                        transport="sdk_http",
                    )

                status = int(sdk_resp.status_code)
                resp = HttpResponse(
                    ok=(200 <= status < 400),
                    status_code=status,
                    final_url=scrub_url_for_log(sdk_resp.url or current_url),
                    headers=dict(sdk_resp.headers),
                    body=_decode_bytes(sdk_resp.content, sdk_resp.headers),
                    error="" if (200 <= status < 400) else f"HTTP {status}",
                    transport="sdk_http",
                )

            if spec.follow_redirects and resp.status_code in _REDIRECT_STATUSES:
                if hop >= max_hops:
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=resp.final_url,
                        error=f"Too many redirects (exceeded {max_hops})",
                        error_code="blocked_redirect",
                        transport=resp.transport,
                    )
                location = _find_header_ci(resp.headers, "location")
                try:
                    current_url, current_method, current_body = self._apply_redirect_hop(
                        current_url=current_url,
                        current_method=current_method,
                        current_body=current_body,
                        headers=req_headers,
                        status_code=resp.status_code,
                        location=location,
                        initial_is_localhost=initial_is_localhost,
                        allowed_origins=allowed_origins,
                    )
                except RedditPluginError as exc:
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=resp.final_url,
                        error=redact_reddit_secrets(str(exc), extra_secrets),
                        error_code=exc.error_code,
                        transport=resp.transport,
                    )
                continue

            return resp

        return HttpResponse(
            ok=False,
            error="Too many redirects",
            error_code="blocked_redirect",
            transport="sdk_http",
        )

    async def _fetch_via_chrome_web_fetch(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_ms: int,
        extra_secrets: tuple[str, ...],
    ) -> HttpResponse:
        if self._ctx is None or not self._ctx.capabilities.has("web.fetch.v1"):
            return HttpResponse(
                ok=False, error="web.fetch.v1 unavailable", error_code="capability_unavailable", transport="web.fetch.v1"
            )
        try:
            safe_url = validate_https_url(
                url,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self.allowed_loopback_origins(),
            )
        except RedditPluginError as exc:
            return HttpResponse(ok=False, error=str(exc), error_code=exc.error_code, transport="web.fetch.v1")

        safe_headers = {
            k: v for k, v in headers.items() if k.lower() not in {"cookie", "cookie2", "authorization", "user-agent"}
        }
        safe_headers["Accept"] = "application/json"
        parsed = urllib.parse.urlsplit(safe_url)
        tab_url = f"{parsed.scheme}://{parsed.netloc}/"

        res = await self._ctx.capabilities.call(
            "web.fetch.v1",
            {
                "action": "fetch",
                "url": safe_url,
                "method": "GET",
                "headers": safe_headers,
                "body": "",
                "credentials": "include",
                "open_tab_if_missing": True,
                "tab_url": tab_url,
                "timeout_ms": timeout_ms,
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            err_code = str((res.data or {}).get("error_code") or "service_offline")
            return HttpResponse(
                ok=False,
                error=redact_reddit_secrets(res.error or "web.fetch.v1 failed", extra_secrets),
                error_code=err_code,
                transport="web.fetch.v1",
            )
        status = int(res.data.get("status") or 200)
        raw_body = res.data.get("body")
        if raw_body is None and "json" in res.data and res.data["json"] is not None:
            raw_body = json.dumps(res.data["json"], ensure_ascii=False)
        body = "" if raw_body is None else str(raw_body)
        if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return HttpResponse(
                ok=False,
                status_code=status,
                error="Response exceeded 8 MiB limit",
                error_code="too_large",
                transport="web.fetch.v1",
            )
        resp_headers = (
            {str(k).lower(): str(v) for k, v in (res.data.get("headers") or {}).items()}
            if isinstance(res.data.get("headers"), Mapping)
            else {}
        )
        final_url = scrub_url_for_log(str(res.data.get("url") or safe_url))
        return HttpResponse(
            ok=(200 <= status < 400 and bool(body)),
            status_code=status,
            final_url=final_url,
            headers=resp_headers,
            body=body,
            error="" if (200 <= status < 400 and body) else f"web.fetch.v1 HTTP {status}",
            transport="web.fetch.v1",
        )

    async def _fetch_via_chrome_extract(
        self,
        url: str,
        timeout_ms: int,
        extra_secrets: tuple[str, ...],
    ) -> HttpResponse:
        if self._ctx is None or not self._ctx.capabilities.has("web.extract.v1"):
            return HttpResponse(
                ok=False,
                error="web.extract.v1 unavailable",
                error_code="capability_unavailable",
                transport="web.extract.v1",
            )
        extract_url = _to_html_extract_url(url)
        try:
            safe_url = validate_https_url(
                extract_url,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self.allowed_loopback_origins(),
            )
        except RedditPluginError as exc:
            return HttpResponse(ok=False, error=str(exc), error_code=exc.error_code, transport="web.extract.v1")

        res = await self._ctx.capabilities.call(
            "web.extract.v1",
            {
                "action": "extract",
                "url": safe_url,
                "mode": "auto",
                "use_cookies": True,
                "render_js": True,
                "timeout_ms": timeout_ms,
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            err_code = str((res.data or {}).get("error_code") or "service_offline")
            return HttpResponse(
                ok=False,
                error=redact_reddit_secrets(res.error or "web.extract.v1 failed", extra_secrets),
                error_code=err_code,
                transport="web.extract.v1",
            )
        final_url = scrub_url_for_log(str(res.data.get("url") or safe_url))
        html_body = str(res.data.get("html") or "")
        text_body = str(res.data.get("text") or res.data.get("body") or "")
        trimmed_text = text_body.strip()
        if trimmed_text.startswith(("{", "[")):
            return HttpResponse(
                ok=True,
                status_code=200,
                final_url=final_url,
                headers={"content-type": "application/json"},
                body=trimmed_text,
                transport="web.extract.v1:json",
            )
        if not html_body.strip():
            return HttpResponse(
                ok=False,
                final_url=final_url,
                error="web.extract.v1 未返回有效页面 HTML",
                error_code="parse_error",
                transport="web.extract.v1",
            )
        if len(html_body.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return HttpResponse(
                ok=False,
                final_url=final_url,
                error="Response exceeded 8 MiB limit",
                error_code="too_large",
                transport="web.extract.v1",
            )
        if _is_cloudflare_response(200, {}, html_body):
            return HttpResponse(
                ok=False,
                status_code=403,
                final_url=final_url,
                body=html_body[:2048],
                error="web.extract.v1 页面仍处于 Cloudflare 验证拦截状态",
                error_code="cloudflare_blocked",
                transport="web.extract.v1",
            )
        if _is_login_wall(final_url, html_body):
            return HttpResponse(
                ok=False,
                status_code=401,
                final_url=final_url,
                body=html_body[:2048],
                error="Reddit 页面需要登录会话",
                error_code="auth_required",
                transport="web.extract.v1",
            )
        return HttpResponse(
            ok=True,
            status_code=200,
            final_url=final_url,
            headers={"content-type": "text/html; charset=utf-8"},
            body=html_body,
            transport="web.extract.v1:html",
        )

    async def fetch_with_fallback(
        self,
        path_or_url: str,
        auth: AuthManager,
        *,
        prefer_chrome_fetch: bool | None = None,
        allow_html_extract: bool = True,
    ) -> HttpResponse:
        """Fetch Reddit JSON (with `web.fetch.v1`, `web.extract.v1`, and `www.reddit.com` fallbacks)."""
        cfg = auth.config()
        timeout_sec = cfg.request_timeout_sec if cfg.request_timeout_sec > 0 else self._default_timeout_sec
        timeout_ms = max(100, int(timeout_sec * 1000))
        path_query = ensure_raw_json_param(_extract_path_and_query(path_or_url))
        auth_res: AuthResolution = await auth.resolve_for_url(f"https://www.reddit.com{path_query}")
        extra_secrets = auth.secret_values() + auth_res.extra_secrets()
        is_auth = auth_res.is_authenticated()

        if path_or_url.strip().lower().startswith(("https://", "http://")):
            primary_url = ensure_raw_json_param(path_or_url.strip())
        elif self._api_base_url:
            primary_url = f"{self._api_base_url}{path_query}"
        elif auth_res.oauth_bearer_token:
            primary_url = f"https://oauth.reddit.com{path_query}"
        else:
            primary_url = f"https://old.reddit.com{path_query}"

        chrome_url = primary_url if self._api_base_url else f"https://www.reddit.com{path_query}"
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "User-Agent": normalize_user_agent(cfg.user_agent),
        }
        if auth_res.cookie_header:
            headers["Cookie"] = auth_res.cookie_header
        try:
            primary_host = url_origin(primary_url)[1]
        except ValueError:
            primary_host = ""
        if auth_res.oauth_bearer_token and (primary_host == "oauth.reddit.com" or self._api_base_url):
            headers["Authorization"] = f"Bearer {auth_res.oauth_bearer_token}"

        use_chrome_first = cfg.prefer_chrome_fetch if prefer_chrome_fetch is None else prefer_chrome_fetch
        if use_chrome_first and self._has_cap("web.fetch.v1"):
            chrome_resp = await self._fetch_via_chrome_web_fetch(chrome_url, headers, timeout_ms, extra_secrets)
            code, _ = classify_http_failure(chrome_resp, expect_json=True, is_authenticated=is_auth)
            if chrome_resp.ok and not code:
                return chrome_resp

        direct = await self.perform_direct(
            HttpRequestSpec(url=primary_url, method="GET", headers=headers, timeout_sec=timeout_sec),
            extra_secrets=extra_secrets,
        )
        direct_code, direct_msg = classify_http_failure(direct, expect_json=True, is_authenticated=is_auth)
        if direct.ok and not direct_code:
            return direct

        if direct_code in _FALLBACK_ELIGIBLE_CODES:
            if self._has_cap("web.fetch.v1"):
                chrome_resp = await self._fetch_via_chrome_web_fetch(chrome_url, headers, timeout_ms, extra_secrets)
                c_code, _ = classify_http_failure(chrome_resp, expect_json=True, is_authenticated=is_auth)
                if chrome_resp.ok and not c_code:
                    return chrome_resp

            if allow_html_extract and self._has_cap("web.extract.v1"):
                ext_resp = await self._fetch_via_chrome_extract(chrome_url, timeout_ms, extra_secrets)
                if ext_resp.ok:
                    return ext_resp
                if ext_resp.error_code in {"cloudflare_blocked", "auth_required"}:
                    return ext_resp

            if self._transport_override is None and not self._api_base_url and primary_host == "old.reddit.com":
                alt_resp = await self.perform_direct(
                    HttpRequestSpec(url=chrome_url, method="GET", headers=headers, timeout_sec=timeout_sec),
                    extra_secrets=extra_secrets,
                )
                a_code, _ = classify_http_failure(alt_resp, expect_json=True, is_authenticated=is_auth)
                if alt_resp.ok and not a_code:
                    return alt_resp

        direct.ok = False
        if not direct.error_code:
            direct.error_code = direct_code or "service_offline"
        if not direct.error or direct.error.startswith("HTTP "):
            direct.error = redact_reddit_secrets(direct_msg or direct.error or "Reddit 请求失败", extra_secrets)
        else:
            direct.error = redact_reddit_secrets(direct.error, extra_secrets)
        return direct
