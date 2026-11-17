"""Hardened HTTPS client wrapping SDK SafeHttpClient with Cloudflare/antispam detection and Chrome proxy fallback."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from omnidesk_sdk import PluginContext
from omnidesk_sdk.http import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_REDIRECTS,
    SafeHttpClient,
    SafeHttpError,
)

if __package__:
    from .zhihu_auth import AuthManager, extract_cookie_value
    from .zhihu_errors import (
        SENSITIVE_HEADERS,
        ZhihuError,
        redact_zhihu_secrets,
        url_origin,
        validate_https_url,
    )
else:
    from zhihu_auth import AuthManager, extract_cookie_value  # type: ignore[no-redef]
    from zhihu_errors import (  # type: ignore[no-redef]
        SENSITIVE_HEADERS,
        ZhihuError,
        redact_zhihu_secrets,
        url_origin,
        validate_https_url,
    )

MAX_RESPONSE_BYTES = DEFAULT_MAX_BYTES
MAX_REDIRECTS = DEFAULT_MAX_REDIRECTS
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_CLOUDFLARE_MARKERS = (
    "just a moment...",
    "_cf_chl_opt",
    "cf-browser-verification",
    "enable javascript and cookies to continue",
    "attention required!",
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/",
    "error code: 1020",
    "verifying you are human",
)


@dataclass
class ZhihuHttpRequest:
    """Structured outbound HTTP request."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout_sec: float = 15.0
    follow_redirects: bool = True
    max_redirects: int = MAX_REDIRECTS


@dataclass
class ZhihuHttpResponse:
    """Structured HTTP response carrying fail-closed classification telemetry."""

    ok: bool = False
    status_code: int = 0
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    error_code: str = ""
    reason: str = ""
    transport: str = "safe_http"
    fallback_used: bool = False
    auth_required: bool = False
    challenge_detected: bool = False
    cloudflare_blocked: bool = False


def _header_ci(headers: Mapping[str, str], name: str) -> str:
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return ""


def _strip_sensitive_headers(headers: dict[str, str]) -> None:
    for key in [k for k in headers if k.lower() in SENSITIVE_HEADERS]:
        headers.pop(key, None)


def _is_cloudflare_blocked(status_code: int, headers: Mapping[str, str], body: str) -> bool:
    cf_mitigated = _header_ci(headers, "cf-mitigated").lower()
    if "challenge" in cf_mitigated:
        return True
    head = body[:16384].lower()
    has_cf_hdr = bool(_header_ci(headers, "cf-ray")) or ("cloudflare" in _header_ci(headers, "server").lower())
    if status_code in {403, 429, 503}:
        if has_cf_hdr:
            return True
        if any(m in head for m in _CLOUDFLARE_MARKERS):
            return True
    if any(m in head for m in ("_cf_chl_opt", "cf-browser-verification", "challenges.cloudflare.com/turnstile", "/cdn-cgi/challenge-platform/")):
        return True
    return False


def _is_zhihu_antispam(final_url: str, body: str, expect_json: bool) -> bool:
    low_url = (final_url or "").lower()
    if "/account/unhuman" in low_url:
        return True
    if (
        '"code":40362' in body
        or '"code": 40362' in body
        or '"code":10003' in body
        or '"code": 10003' in body
        or '"zse-96"' in body
        or "x-zse-96" in body
    ):
        return True
    if not expect_json and "js-initialdata" not in body.lower():
        if "zse_ck" in body or "安全验证" in body or "请求存在异常" in body:
            return True
    return False


def _is_login_wall(status_code: int, final_url: str, body: str, expect_json: bool) -> bool:
    if status_code == 401:
        return True
    low_url = (final_url or "").lower()
    if "/signin" in low_url or "/login" in low_url:
        return True
    if expect_json:
        if '"code":401' in body or '"code": 401' in body or '"ERR_LOGIN_REQUIRED"' in body:
            return True
    else:
        head = body[:8192].lower()
        if "js-initialdata" not in head and (
            "zhihu.com/signin" in head or ("登录知乎" in body[:8192] and "<form" in head)
        ):
            return True
    return False


def classify_zhihu_response(
    resp: ZhihuHttpResponse,
    *,
    expect_json: bool = False,
    is_authenticated: bool = False,
    secrets: tuple[str, ...] = (),
) -> ZhihuHttpResponse:
    """Classify response failures fail-closed with canonical error codes and secret redaction."""
    if resp.error_code:
        resp.ok = False
        resp.error = redact_zhihu_secrets(resp.error or f"请求失败 ({resp.error_code})", secrets)
        return resp

    status = resp.status_code
    if _is_cloudflare_blocked(status, resp.headers, resp.body):
        resp.ok = False
        resp.challenge_detected = True
        resp.cloudflare_blocked = True
        resp.error_code = "permission_denied"
        resp.reason = "cloudflare_blocked"
        resp.error = f"请求被 Cloudflare 验证拦截 (HTTP {status or 403})，请通过 Chrome 登录或验证后重试"
        return resp

    if _is_zhihu_antispam(resp.final_url, resp.body, expect_json):
        resp.ok = False
        resp.challenge_detected = True
        resp.error_code = "permission_denied"
        resp.reason = "zhihu_antispam"
        resp.error = f"知乎风控或签名校验拦截 (HTTP {status or 403})，请刷新 Cookie 或启用 Chrome 代理"
        return resp

    if _is_login_wall(status, resp.final_url, resp.body, expect_json):
        resp.ok = False
        resp.auth_required = True
        resp.error_code = "permission_denied"
        resp.reason = "auth_required"
        resp.error = "需要登录知乎以继续访问 (HTTP 401 / 登录墙)"
        return resp

    if status == 429:
        resp.ok = False
        resp.error_code = "rate_limited"
        resp.reason = "rate_limited"
        resp.error = "知乎上游触发限流 (HTTP 429)"
        return resp

    if status in {404, 410}:
        resp.ok = False
        resp.error_code = "not_found"
        resp.reason = "not_found"
        resp.error = f"知乎目标内容不存在或已删除 (HTTP {status})"
        return resp

    if status == 403:
        resp.ok = False
        resp.auth_required = not is_authenticated
        resp.error_code = "permission_denied"
        resp.reason = "forbidden" if is_authenticated else "auth_required"
        resp.error = (
            "知乎拒绝访问该资源 (HTTP 403 Forbidden)"
            if is_authenticated
            else "知乎访问受限，请先完成登录 (HTTP 403)"
        )
        return resp

    if status >= 500:
        resp.ok = False
        resp.error_code = "service_offline"
        resp.reason = "upstream_error"
        resp.error = f"知乎上游服务异常 (HTTP {status})"
        return resp

    if not resp.ok or (status != 0 and status >= 400):
        resp.ok = False
        resp.error_code = "service_offline"
        resp.reason = "http_error"
        resp.error = redact_zhihu_secrets(resp.error or f"知乎上游 HTTP 错误 ({status})", secrets)
        return resp

    trimmed = resp.body.strip()
    if not trimmed:
        resp.ok = False
        resp.error_code = "validation_error"
        resp.reason = "parse_error"
        resp.error = "知乎上游返回空响应体"
        return resp

    if expect_json:
        try:
            parsed = json.loads(trimmed)
        except json.JSONDecodeError as exc:
            resp.ok = False
            resp.error_code = "validation_error"
            resp.reason = "parse_error"
            resp.error = f"知乎 API 返回无效 JSON 响应: {exc}"
            return resp
        if not isinstance(parsed, (dict, list)):
            resp.ok = False
            resp.error_code = "validation_error"
            resp.reason = "parse_error"
            resp.error = "知乎 API 返回非 JSON 对象"
            return resp
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            err_obj = parsed["error"]
            err_code_num = err_obj.get("code")
            err_msg = redact_zhihu_secrets(str(err_obj.get("message") or ""), secrets)
            if err_code_num in {40362, 10003}:
                resp.ok = False
                resp.challenge_detected = True
                resp.error_code = "permission_denied"
                resp.reason = "zhihu_antispam"
                resp.error = err_msg or "知乎签名校验拦截"
                return resp
            if err_code_num == 401:
                resp.ok = False
                resp.auth_required = True
                resp.error_code = "permission_denied"
                resp.reason = "auth_required"
                resp.error = err_msg or "知乎未登录 (401)"
                return resp

    resp.ok = True
    return resp


class ZhihuHttpClient:
    """Async Zhihu HTTP client backed by SDK `SafeHttpClient` and Chrome bridge fallback."""

    def __init__(
        self,
        *,
        allow_loopback_http: bool = False,
        allowed_loopback_origins: set[str] | None = None,
    ) -> None:
        self._ctx: PluginContext | None = None
        self._allow_loopback_http = bool(allow_loopback_http)
        self._allowed_loopback_origins = set(allowed_loopback_origins or ())
        self._transport_override: Callable[[ZhihuHttpRequest], ZhihuHttpResponse] | None = None
        self._default_timeout_sec: float = 15.0
        self.last_transport: str = "safe_http"
        self.fallback_count: int = 0

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def set_transport_override(
        self, fn: Callable[[ZhihuHttpRequest], ZhihuHttpResponse] | None
    ) -> None:
        self._transport_override = fn

    def set_default_timeout_sec(self, sec: float) -> None:
        self._default_timeout_sec = max(0.1, min(120.0, float(sec)))

    def allow_loopback_origin(self, origin: str) -> None:
        self._allowed_loopback_origins.add(origin)

    def _has_cap(self, cap_id: str) -> bool:
        return self._ctx is not None and self._ctx.capabilities.has(cap_id)

    async def perform_direct(
        self, req: ZhihuHttpRequest, *, secrets: tuple[str, ...] = ()
    ) -> ZhihuHttpResponse:
        """Execute direct request using `SafeHttpClient` (or test `transport_override`)."""
        try:
            safe_url = validate_https_url(
                req.url,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self._allowed_loopback_origins,
            )
        except ZhihuError as exc:
            return ZhihuHttpResponse(
                ok=False,
                error=redact_zhihu_secrets(exc.message, secrets),
                error_code=exc.error_code,
                transport="safe_http",
            )

        timeout_sec = max(0.1, min(120.0, float(req.timeout_sec or self._default_timeout_sec)))
        max_redirects = max(0, req.max_redirects) if req.follow_redirects else 0

        if self._transport_override is not None:
            return self._perform_via_override(
                ZhihuHttpRequest(
                    url=safe_url,
                    method=req.method,
                    headers=dict(req.headers),
                    body=req.body,
                    timeout_sec=timeout_sec,
                    follow_redirects=req.follow_redirects,
                    max_redirects=max_redirects,
                ),
                secrets=secrets,
            )

        client = SafeHttpClient(
            allow_localhost_http=self._allow_loopback_http,
            timeout_sec=timeout_sec,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=max_redirects,
            follow_redirects=req.follow_redirects,
            raise_for_status=False,
            secrets=secrets,
        )
        try:
            async with client:
                sdk_resp = await client.request(
                    req.method,
                    safe_url,
                    headers=req.headers,
                    content=req.body if req.method.upper() == "POST" and req.body else None,
                    secrets=secrets,
                )
        except SafeHttpError as exc:
            code = exc.error_code or "service_offline"
            return ZhihuHttpResponse(
                ok=False,
                status_code=exc.status_code or 0,
                final_url=exc.url or safe_url,
                error=redact_zhihu_secrets(exc.message, secrets),
                error_code=code,
                transport="safe_http",
            )

        return ZhihuHttpResponse(
            ok=200 <= sdk_resp.status_code < 400,
            status_code=sdk_resp.status_code,
            final_url=sdk_resp.url or safe_url,
            headers=dict(sdk_resp.headers),
            body=sdk_resp.text,
            transport="safe_http",
        )

    def _perform_via_override(
        self, req: ZhihuHttpRequest, *, secrets: tuple[str, ...]
    ) -> ZhihuHttpResponse:
        cur = req
        max_hops = cur.max_redirects if cur.follow_redirects else 0
        for hop in range(max_hops + 1):
            resp = self._transport_override(cur)  # type: ignore[misc]
            if not resp.transport:
                resp.transport = "mock"
            if not resp.final_url:
                resp.final_url = cur.url
            if cur.follow_redirects and resp.status_code in _REDIRECT_STATUSES:
                if hop >= max_hops:
                    return ZhihuHttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=cur.url,
                        error=f"HTTP 重定向次数超过上限 ({max_hops})",
                        error_code="blocked_redirect",
                        transport=resp.transport,
                    )
                loc = _header_ci(resp.headers, "location").strip()
                if not loc:
                    return resp
                next_url = urllib.parse.urljoin(cur.url, loc)
                try:
                    safe_next = validate_https_url(
                        next_url,
                        allow_loopback_http=self._allow_loopback_http,
                        allowed_loopback_origins=self._allowed_loopback_origins,
                    )
                except ZhihuError as exc:
                    return ZhihuHttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=cur.url,
                        error=redact_zhihu_secrets(exc.message, secrets),
                        error_code=exc.error_code,
                        transport=resp.transport,
                    )
                next_headers = dict(cur.headers)
                if url_origin(cur.url) != url_origin(safe_next):
                    _strip_sensitive_headers(next_headers)
                next_method = cur.method
                next_body = cur.body
                if resp.status_code == 303 or (resp.status_code in {301, 302} and cur.method == "POST"):
                    next_method = "GET"
                    next_body = ""
                cur = ZhihuHttpRequest(
                    url=safe_next,
                    method=next_method,
                    headers=next_headers,
                    body=next_body,
                    timeout_sec=cur.timeout_sec,
                    follow_redirects=cur.follow_redirects,
                    max_redirects=cur.max_redirects,
                )
                continue
            return resp
        return ZhihuHttpResponse(
            ok=False,
            error="Too many redirects",
            error_code="blocked_redirect",
            transport="mock",
        )

    async def fetch_via_chrome_web_fetch(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_ms: int,
        *,
        secrets: tuple[str, ...] = (),
    ) -> ZhihuHttpResponse:
        """Proxy same-origin `fetch()` via `plugin-chrome-web` (`web.fetch.v1`), never passing Cookie headers."""
        try:
            safe_url = validate_https_url(url, allow_loopback_http=False)
        except ZhihuError as exc:
            return ZhihuHttpResponse(
                ok=False,
                error=redact_zhihu_secrets(exc.message, secrets),
                error_code=exc.error_code,
                transport="web.fetch.v1",
            )
        if self._ctx is None or not self._has_cap("web.fetch.v1"):
            return ZhihuHttpResponse(
                ok=False,
                error="web.fetch.v1 unavailable",
                error_code="service_offline",
                transport="web.fetch.v1",
            )

        safe_headers = {
            k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS
        }
        res = await self._ctx.capabilities.call(
            "web.fetch.v1",
            {
                "action": "fetch",
                "url": safe_url,
                "method": "GET",
                "headers": safe_headers,
                "body": "",
                "credentials": "include",
                "timeout_ms": max(100, int(timeout_ms)),
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            err_code = str((res.data or {}).get("error_code") or "service_offline")
            return ZhihuHttpResponse(
                ok=False,
                error=redact_zhihu_secrets(res.error or "web.fetch.v1 failed", secrets),
                error_code=err_code,
                transport="web.fetch.v1",
            )

        status = res.data.get("status", 200)
        if isinstance(status, bool) or not isinstance(status, int):
            status = 200
        final_url = str(res.data.get("url") or safe_url)
        body = str(res.data.get("body") or "")
        if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return ZhihuHttpResponse(
                ok=False,
                status_code=status,
                final_url=final_url,
                error="web.fetch.v1 response exceeded 8 MiB limit",
                error_code="too_large",
                transport="web.fetch.v1",
            )
        raw_hdrs = res.data.get("headers")
        hdrs = (
            {str(k).lower(): str(v) for k, v in raw_hdrs.items() if isinstance(v, str)}
            if isinstance(raw_hdrs, Mapping)
            else {}
        )
        return ZhihuHttpResponse(
            ok=(200 <= status < 400 and bool(body.strip())),
            status_code=status,
            final_url=final_url,
            headers=hdrs,
            body=body,
            transport="web.fetch.v1",
        )

    async def fetch_via_chrome_extract(
        self, url: str, timeout_ms: int, *, secrets: tuple[str, ...] = ()
    ) -> ZhihuHttpResponse:
        """Extract rendered DOM via `plugin-chrome-web` (`web.extract.v1`)."""
        try:
            safe_url = validate_https_url(url, allow_loopback_http=False)
        except ZhihuError as exc:
            return ZhihuHttpResponse(
                ok=False,
                error=redact_zhihu_secrets(exc.message, secrets),
                error_code=exc.error_code,
                transport="web.extract.v1",
            )
        if self._ctx is None or not self._has_cap("web.extract.v1"):
            return ZhihuHttpResponse(
                ok=False,
                error="web.extract.v1 unavailable",
                error_code="service_offline",
                transport="web.extract.v1",
            )
        res = await self._ctx.capabilities.call(
            "web.extract.v1",
            {
                "action": "extract",
                "url": safe_url,
                "mode": "auto",
                "use_cookies": True,
                "render_js": True,
                "timeout_ms": max(int(timeout_ms), 15000),
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            err_code = str((res.data or {}).get("error_code") or "service_offline")
            return ZhihuHttpResponse(
                ok=False,
                error=redact_zhihu_secrets(res.error or "web.extract.v1 failed", secrets),
                error_code=err_code,
                transport="web.extract.v1",
            )
        status = res.data.get("status", 200)
        if isinstance(status, bool) or not isinstance(status, int):
            status = 200
        final_url = str(res.data.get("url") or safe_url)
        body = str(res.data.get("html") or res.data.get("text") or "")
        if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return ZhihuHttpResponse(
                ok=False,
                status_code=status,
                final_url=final_url,
                error="web.extract.v1 response exceeded 8 MiB limit",
                error_code="too_large",
                transport="web.extract.v1",
            )
        return ZhihuHttpResponse(
            ok=(200 <= status < 400 and bool(body.strip())),
            status_code=status,
            final_url=final_url,
            body=body,
            transport="web.extract.v1",
        )

    async def fetch_with_fallback(
        self,
        url: str,
        auth: AuthManager,
        *,
        prefer_chrome_fetch: bool | None = None,
        expect_json: bool = False,
        force_chrome_on_signature: bool = False,
    ) -> ZhihuHttpResponse:
        """Fetch URL via fast direct path with automatic Chrome proxy fallback on blocks/signatures."""
        cfg = auth.config()
        resolved = await auth.resolve_for_url(url)
        secrets = auth.secrets_for_redaction()
        timeout_sec = cfg.request_timeout_sec if cfg.request_timeout_sec > 0 else self._default_timeout_sec
        timeout_ms = int(timeout_sec * 1000)
        use_pref_chrome = cfg.prefer_chrome_fetch if prefer_chrome_fetch is None else bool(prefer_chrome_fetch)

        headers: dict[str, str] = {
            "User-Agent": _DEFAULT_USER_AGENT,
            "Accept": (
                "application/json, text/plain, */*"
                if expect_json
                else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.zhihu.com/",
        }
        if resolved.cookie_header:
            headers["Cookie"] = resolved.cookie_header
            xsrf = extract_cookie_value(resolved.cookie_header, "_xsrf")
            if xsrf:
                headers["x-xsrftoken"] = xsrf

        is_https = url.strip().lower().startswith("https://")
        use_chrome_first = (
            (use_pref_chrome or force_chrome_on_signature)
            and is_https
            and self._has_cap("web.fetch.v1")
        )

        if use_chrome_first:
            chrome_resp = await self.fetch_via_chrome_web_fetch(url, headers, timeout_ms, secrets=secrets)
            classified = classify_zhihu_response(
                chrome_resp,
                expect_json=expect_json,
                is_authenticated=resolved.is_authenticated,
                secrets=secrets,
            )
            if classified.ok:
                classified.fallback_used = True
                self.last_transport = classified.transport
                self.fallback_count += 1
                return classified

        direct = await self.perform_direct(
            ZhihuHttpRequest(url=url, headers=headers, timeout_sec=timeout_sec),
            secrets=secrets,
        )
        direct_classified = classify_zhihu_response(
            direct,
            expect_json=expect_json,
            is_authenticated=resolved.is_authenticated,
            secrets=secrets,
        )
        if direct_classified.ok:
            self.last_transport = direct_classified.transport
            return direct_classified

        if not use_chrome_first and is_https and self._has_cap("web.fetch.v1"):
            chrome_resp = await self.fetch_via_chrome_web_fetch(url, headers, timeout_ms, secrets=secrets)
            chrome_classified = classify_zhihu_response(
                chrome_resp,
                expect_json=expect_json,
                is_authenticated=resolved.is_authenticated,
                secrets=secrets,
            )
            if chrome_classified.ok:
                chrome_classified.fallback_used = True
                self.last_transport = chrome_classified.transport
                self.fallback_count += 1
                return chrome_classified
            if chrome_classified.body.strip():
                chrome_classified.fallback_used = True
                self.last_transport = chrome_classified.transport
                return chrome_classified

        if not expect_json and is_https and self._has_cap("web.extract.v1"):
            ext_resp = await self.fetch_via_chrome_extract(url, timeout_ms, secrets=secrets)
            ext_classified = classify_zhihu_response(
                ext_resp,
                expect_json=False,
                is_authenticated=resolved.is_authenticated,
                secrets=secrets,
            )
            if ext_classified.ok:
                ext_classified.fallback_used = True
                self.last_transport = ext_classified.transport
                self.fallback_count += 1
                return ext_classified

        self.last_transport = direct_classified.transport
        return direct_classified
