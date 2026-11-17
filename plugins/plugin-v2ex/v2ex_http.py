"""Hardened HTTPS client and web.fetch.v1 / web.extract.v1 fallback for plugin-v2ex."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from omnidesk_sdk import PluginContext
from omnidesk_sdk.http import SafeHttpClient, SafeHttpError

if __package__:
    from .v2ex_auth import AuthManager
    from .v2ex_security import (
        DEFAULT_V2EX_BASE_URL,
        LOOPBACK_HOSTS,
        MAX_REDIRECTS,
        MAX_RESPONSE_BYTES,
        V2exError,
        is_same_origin_or_host,
        redact_sensitive,
        url_origin,
        validate_https_url,
    )
else:
    from v2ex_auth import AuthManager
    from v2ex_security import (
        DEFAULT_V2EX_BASE_URL,
        LOOPBACK_HOSTS,
        MAX_REDIRECTS,
        MAX_RESPONSE_BYTES,
        V2exError,
        is_same_origin_or_host,
        redact_sensitive,
        url_origin,
        validate_https_url,
    )

_CHUNK_SIZE = 65536
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_CLOUDFLARE_MARKERS = (
    "cf-turnstile",
    "__cf_chl_",
    "just a moment...",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
)


def _decode_bounded_response_body(raw_bytes: bytes, headers: Mapping[str, str], *, max_bytes: int) -> str:
    """Decode raw HTTP bytes with bounded gzip/deflate decompression."""
    enc = str(headers.get("content-encoding") or "").strip().lower()
    payload = raw_bytes
    if enc in {"gzip", "x-gzip", "deflate"} and raw_bytes:
        wbits = 16 + zlib.MAX_WBITS if "gzip" in enc else zlib.MAX_WBITS
        decomp = zlib.decompressobj(wbits)
        out = bytearray()
        for idx in range(0, len(raw_bytes), _CHUNK_SIZE):
            chunk = decomp.decompress(raw_bytes[idx : idx + _CHUNK_SIZE], max_length=max_bytes + 1 - len(out))
            out.extend(chunk)
            if len(out) > max_bytes or decomp.unconsumed_tail:
                raise V2exError(
                    "too_large",
                    f"解压后响应体积超出 {max_bytes // (1024 * 1024)} MiB 上限",
                    details={"reason": "response_too_large"},
                )
        tail = decomp.flush()
        if len(out) + len(tail) > max_bytes:
            raise V2exError(
                "too_large",
                f"解压后响应体积超出 {max_bytes // (1024 * 1024)} MiB 上限",
                details={"reason": "response_too_large"},
            )
        out.extend(tail)
        payload = bytes(out)
    elif len(payload) > max_bytes:
        raise V2exError(
            "too_large",
            f"响应体积超出 {max_bytes // (1024 * 1024)} MiB 上限",
            details={"reason": "response_too_large"},
        )
    return payload.decode("utf-8", errors="replace")


@dataclass(slots=True)
class HttpRequest:
    """Outbound HTTP request parameters."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout_sec: float = 15.0
    follow_redirects: bool = True
    max_redirects: int = MAX_REDIRECTS


@dataclass(slots=True)
class HttpResponse:
    """Normalized HTTP response envelope."""

    ok: bool = False
    status_code: int = 0
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    error_code: str = ""
    transport: str = "urllib"


def is_v2ex_login_required(resp: HttpResponse) -> bool:
    """Return True when response indicates V2EX login wall or OAuth signin redirect."""
    final_low = (resp.final_url or "").lower()
    if "/signin" in final_low or "accounts.google.com" in final_low:
        return True
    body = resp.body or ""
    if (
        "你要查看的页面需要先登录" in body
        or "需要先登录" in body
        or "请先登录" in body
        or ('action="/signin"' in body and "topic_content" not in body and "item_title" not in body)
    ):
        return True
    return False


def is_v2ex_cloudflare_blocked(resp: HttpResponse) -> bool:
    """Return True when response carries Cloudflare Turnstile or JS challenge markers."""
    low = (resp.body or "").lower()
    if any(marker in low for marker in _CLOUDFLARE_MARKERS):
        return True
    return False


def is_v2ex_blocked_response(resp: HttpResponse, expect_json: bool = False) -> bool:
    """Return True when response is blocked by auth, Cloudflare, rate limit, or non-JSON shell."""
    if not resp.ok or resp.status_code in {401, 403, 429, 503}:
        return True
    if is_v2ex_login_required(resp) or is_v2ex_cloudflare_blocked(resp):
        return True
    if expect_json:
        stripped = (resp.body or "").lstrip()
        if not stripped or stripped[0] not in "{[":
            return True
    return False


def classify_http_error(resp: HttpResponse, *, expect_json: bool = False) -> V2exError:
    """Map an unsuccessful or blocked HttpResponse into a fail-closed V2exError."""
    if is_v2ex_cloudflare_blocked(resp):
        return V2exError(
            "cloudflare_blocked",
            f"V2EX 触发 Cloudflare 人机验证 (HTTP {resp.status_code or 403})，请使用 Chrome 桥接或稍后重试",
            details={"http_status": resp.status_code or 403, "reason": "cloudflare"},
        )
    if is_v2ex_login_required(resp) or resp.status_code == 401:
        return V2exError(
            "auth_required",
            f"需要登录 V2EX（请同步 A2 Cookie 或配置 API v2 Token，HTTP {resp.status_code or 401}）",
            details={"http_status": resp.status_code or 401, "reason": "login_required"},
        )
    if resp.error_code:
        return V2exError(
            resp.error_code,
            resp.error or f"V2EX 请求失败 ({resp.error_code})",
            details={"http_status": resp.status_code} if resp.status_code else {},
        )
    code = resp.status_code
    if code == 403:
        return V2exError("permission_denied", "V2EX 拒绝访问 (HTTP 403 Forbidden)", details={"http_status": 403})
    if code == 404:
        return V2exError("not_found", "请求的 V2EX 主题或节点不存在 (HTTP 404)", details={"http_status": 404})
    if code == 429:
        return V2exError("rate_limited", "V2EX 请求触发限流 (HTTP 429)", details={"http_status": 429})
    if code >= 500:
        return V2exError("service_offline", f"V2EX 上游服务异常 (HTTP {code})", details={"http_status": code})
    if expect_json and resp.ok:
        return V2exError("parse_error", "V2EX 接口返回了非 JSON 响应")
    if code >= 400:
        return V2exError("service_offline", f"V2EX HTTP 请求失败 ({code})", details={"http_status": code})
    return V2exError("service_offline", resp.error or "V2EX 网络请求失败")


def _strip_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in {"cookie", "authorization"}}


class HttpClient:
    """Hardened HTTPS-only client with redirect/size limits and Chrome fallback."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_V2EX_BASE_URL,
        allow_loopback_http: bool = False,
        default_timeout_sec: float = 15.0,
    ) -> None:
        self._ctx: PluginContext | None = None
        self.base_url = base_url.rstrip("/")
        self.allow_loopback_http = allow_loopback_http
        self._default_timeout_sec = max(0.05, min(120.0, float(default_timeout_sec)))
        self._transport_override: Callable[[HttpRequest], HttpResponse] | None = None

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def set_default_timeout_sec(self, sec: float) -> None:
        self._default_timeout_sec = max(0.05, min(120.0, float(sec)))

    def set_transport_override(self, fn: Callable[[HttpRequest], HttpResponse] | None) -> None:
        """Override direct transport in unit tests."""
        self._transport_override = fn

    def allowed_loopback_origins(self) -> set[str]:
        if self.base_url.startswith("http://"):
            host = (urllib.parse.urlsplit(self.base_url).hostname or "").lower()
            if host in LOOPBACK_HOSTS:
                return {url_origin(self.base_url)}
        return set()

    def _validate_target(self, url: str) -> str:
        return validate_https_url(
            url,
            allow_loopback_http=self.allow_loopback_http,
            allowed_loopback_origins=self.allowed_loopback_origins(),
        )

    async def perform_direct(self, req: HttpRequest, *, extra_secrets: Iterable[str] = ()) -> HttpResponse:
        """Execute a hardened direct HTTPS request using `SafeHttpClient` with redirect auditing."""
        secrets = tuple(extra_secrets)
        try:
            safe_url = self._validate_target(req.url)
        except V2exError as exc:
            return HttpResponse(ok=False, error=redact_sensitive(exc.message, secrets), error_code=exc.error_code)

        max_redirects = min(MAX_REDIRECTS, max(0, int(req.max_redirects)))
        timeout_sec = max(0.05, float(req.timeout_sec if req.timeout_sec > 0 else self._default_timeout_sec))

        if self._transport_override is None:
            client = SafeHttpClient(
                allow_localhost_http=self.allow_loopback_http,
                timeout_sec=timeout_sec,
                max_bytes=MAX_RESPONSE_BYTES,
                max_redirects=max_redirects,
                follow_redirects=req.follow_redirects,
                raise_for_status=False,
                secrets=secrets,
            )
            req_headers = dict(req.headers)
            if not any(k.lower() == "accept-encoding" for k in req_headers):
                req_headers["Accept-Encoding"] = "identity"
            body_str = req.body if req.body and req.method == "POST" else None
            try:
                async with client:
                    raw_resp = await client.request(
                        req.method,
                        safe_url,
                        headers=req_headers,
                        content=body_str,
                        timeout_sec=timeout_sec,
                        max_bytes=MAX_RESPONSE_BYTES,
                        max_redirects=max_redirects,
                        follow_redirects=req.follow_redirects,
                        raise_for_status=False,
                        secrets=secrets,
                    )
                text = _decode_bounded_response_body(raw_resp.content, raw_resp.headers, max_bytes=MAX_RESPONSE_BYTES)
                status = int(raw_resp.status_code)
                return HttpResponse(
                    ok=200 <= status < 400,
                    status_code=status,
                    final_url=raw_resp.url or safe_url,
                    headers=dict(raw_resp.headers),
                    body=text,
                    error=f"HTTP {status}" if status >= 400 else "",
                    transport="safe_http",
                )
            except SafeHttpError as exc:
                if "accounts.google.com" in (exc.url or "").lower():
                    return HttpResponse(
                        ok=False,
                        status_code=int(exc.status_code or 302),
                        final_url=exc.url or safe_url,
                        error="V2EX 重定向至 Google OAuth 登录页，请先完成登录",
                        error_code="auth_required",
                        transport="safe_http",
                    )
                return HttpResponse(
                    ok=False,
                    status_code=int(exc.status_code or 0),
                    final_url=exc.url or safe_url,
                    error=redact_sensitive(exc.message, secrets),
                    error_code=exc.error_code,
                    transport="safe_http",
                )
            except V2exError as exc:
                return HttpResponse(
                    ok=False,
                    final_url=safe_url,
                    error=redact_sensitive(exc.message, secrets),
                    error_code=exc.error_code,
                    transport="safe_http",
                )

        cur = HttpRequest(
            url=safe_url,
            method=req.method,
            headers=dict(req.headers),
            body=req.body,
            timeout_sec=timeout_sec,
            follow_redirects=req.follow_redirects,
            max_redirects=max_redirects,
        )
        max_hops = cur.max_redirects if cur.follow_redirects else 0

        for hop in range(max_hops + 1):
            resp = await asyncio.to_thread(self._transport_override, cur)
            if not resp.transport:
                resp.transport = "mock"
            if not resp.final_url:
                resp.final_url = cur.url
            if cur.follow_redirects and resp.status_code in _REDIRECT_STATUSES:
                if hop >= max_hops:
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=cur.url,
                        error=f"HTTP 重定向次数超过上限 ({max_hops})",
                        error_code="blocked_redirect",
                        transport=resp.transport,
                    )
                location = next((v for k, v in resp.headers.items() if k.lower() == "location"), "").strip()
                if not location:
                    return resp
                next_url = urllib.parse.urljoin(cur.url, location)
                try:
                    next_url = self._validate_target(next_url)
                except V2exError as exc:
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=cur.url,
                        error=redact_sensitive(exc.message, secrets),
                        error_code=exc.error_code,
                        transport=resp.transport,
                    )
                next_host = (urllib.parse.urlsplit(next_url).hostname or "").lower()
                if next_host == "accounts.google.com" or next_host.endswith(".accounts.google.com"):
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=next_url,
                        headers=resp.headers,
                        error="V2EX 重定向至 Google OAuth 登录页，请先完成登录",
                        error_code="auth_required",
                        transport=resp.transport,
                    )
                if not is_same_origin_or_host(cur.url, next_url):
                    cur.headers = _strip_sensitive_headers(cur.headers)
                if resp.status_code == 303 or (resp.status_code in {301, 302} and cur.method == "POST"):
                    cur.method = "GET"
                    cur.body = ""
                cur.url = next_url
                continue
            return resp

        return HttpResponse(ok=False, error="HTTP 重定向次数过多", error_code="blocked_redirect")

    async def fetch_api_v2(self, url: str, auth: AuthManager) -> HttpResponse:
        """Execute a V2EX API v2 JSON request authenticated with Personal Access Token."""
        resolved = await auth.resolve_for_url(url)
        if not resolved.has_pat:
            return HttpResponse(
                ok=False,
                error="未配置 V2EX API v2 Personal Access Token",
                error_code="not_configured",
            )
        timeout_sec = auth.config.request_timeout_sec or self._default_timeout_sec
        req = HttpRequest(
            url=url,
            timeout_sec=timeout_sec,
            headers={
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept": "application/json",
                "Authorization": f"Bearer {resolved.api_token}",
            },
        )
        secrets = (*auth.secret_values(), resolved.api_token)
        resp = await self.perform_direct(req, extra_secrets=secrets)
        if not resp.ok:
            if not resp.error_code:
                resp.error_code = classify_http_error(resp, expect_json=True).error_code
            if not resp.error:
                resp.error = f"API v2 HTTP {resp.status_code}"
            resp.error = redact_sensitive(resp.error, secrets)
            return resp

        try:
            parsed = json.loads(resp.body)
        except json.JSONDecodeError:
            resp.ok = False
            resp.error_code = "parse_error"
            resp.error = "API v2 返回了无效 JSON"
            return resp
        if not isinstance(parsed, Mapping):
            resp.ok = False
            resp.error_code = "parse_error"
            resp.error = "API v2 返回了非对象 JSON 响应"
            return resp
        if parsed.get("success") is False:
            resp.ok = False
            msg = str(parsed.get("message") or "API v2 请求被拒绝")
            resp.error = redact_sensitive(msg, secrets)
            low = msg.lower()
            if "token" in low or "unauthorized" in low or "auth" in low:
                resp.error_code = "auth_required"
            elif "not found" in low:
                resp.error_code = "not_found"
            else:
                resp.error_code = "permission_denied"
            return resp
        return resp

    async def _fetch_via_chrome_fetch(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_ms: int,
        secrets: tuple[str, ...],
    ) -> HttpResponse:
        out = HttpResponse(transport="web.fetch.v1")
        try:
            safe_url = self._validate_target(url)
        except V2exError as exc:
            out.error = redact_sensitive(exc.message, secrets)
            out.error_code = exc.error_code
            return out
        if self._ctx is None or not self._ctx.capabilities.has("web.fetch.v1"):
            out.error = "web.fetch.v1 不可用"
            out.error_code = "capability_not_found"
            return out

        safe_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() not in {"cookie", "authorization", "host", "connection", "content-length"}
        }
        parsed = urllib.parse.urlsplit(safe_url)
        request_payload: dict[str, Any] = {
            "action": "fetch",
            "url": safe_url,
            "method": "GET",
            "headers": safe_headers,
            "body": "",
            "credentials": "include",
            "timeout_ms": max(100, int(timeout_ms)),
        }
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            request_payload["open_tab_if_missing"] = True
            request_payload["tab_url"] = f"{parsed.scheme}://{parsed.netloc}/"
        res = await self._ctx.capabilities.call(
            "web.fetch.v1",
            request_payload,
        )
        if not res.ok or not isinstance(res.data, Mapping):
            out.error = redact_sensitive(res.error or "web.fetch.v1 请求失败", secrets)
            out.error_code = str(res.data.get("error_code") or "service_offline")
            return out
        out.status_code = int(res.data.get("status") or 200)
        out.final_url = str(res.data.get("url") or safe_url)
        out.body = str(res.data.get("body") or "")
        if len(out.body.encode("utf-8")) > MAX_RESPONSE_BYTES:
            out.ok = False
            out.body = ""
            out.error = "web.fetch.v1 响应超过 8 MiB 上限"
            out.error_code = "validation_error"
            return out
        raw_hdrs = res.data.get("headers")
        if isinstance(raw_hdrs, Mapping):
            out.headers = {str(k).lower(): str(v) for k, v in raw_hdrs.items() if isinstance(v, str)}
        out.ok = 200 <= out.status_code < 400 and bool(out.body)
        return out

    async def _fetch_via_chrome_extract(
        self,
        url: str,
        timeout_ms: int,
        secrets: tuple[str, ...],
    ) -> HttpResponse:
        out = HttpResponse(transport="web.extract.v1")
        try:
            safe_url = self._validate_target(url)
        except V2exError as exc:
            out.error = redact_sensitive(exc.message, secrets)
            out.error_code = exc.error_code
            return out
        if self._ctx is None or not self._ctx.capabilities.has("web.extract.v1"):
            out.error = "web.extract.v1 不可用"
            out.error_code = "capability_not_found"
            return out
        res = await self._ctx.capabilities.call(
            "web.extract.v1",
            {
                "action": "extract",
                "url": safe_url,
                "mode": "auto",
                "use_cookies": True,
                "render_js": True,
                "timeout_ms": max(timeout_ms, 15000),
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            out.error = redact_sensitive(res.error or "web.extract.v1 请求失败", secrets)
            out.error_code = str(res.data.get("error_code") or "service_offline")
            return out
        out.status_code = int(res.data.get("status") or 200)
        out.final_url = str(res.data.get("url") or safe_url)
        out.body = str(res.data.get("html") or res.data.get("text") or "")
        if len(out.body.encode("utf-8")) > MAX_RESPONSE_BYTES:
            out.ok = False
            out.body = ""
            out.error = "web.extract.v1 响应超过 8 MiB 上限"
            out.error_code = "validation_error"
            return out
        out.ok = 200 <= out.status_code < 400 and bool(out.body)
        return out

    async def fetch_with_fallback(
        self,
        url: str,
        auth: AuthManager,
        *,
        prefer_chrome_fetch: bool = False,
        expect_json: bool = False,
    ) -> HttpResponse:
        """Fetch URL via direct HTTPS + Cookie with web.fetch.v1 / web.extract.v1 fallback."""
        resolved = await auth.resolve_for_url(url)
        timeout_sec = auth.config.request_timeout_sec or self._default_timeout_sec
        timeout_ms = max(100, int(timeout_sec * 1000))
        secrets = (*auth.secret_values(), resolved.cookie_header, resolved.api_token)

        headers: dict[str, str] = {
            "User-Agent": _DEFAULT_USER_AGENT,
            "Accept": (
                "application/json, text/plain, */*"
                if expect_json
                else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{self.base_url}/",
        }
        if resolved.cookie_header:
            headers["Cookie"] = resolved.cookie_header

        has_fetch = self._ctx is not None and self._ctx.capabilities.has("web.fetch.v1")
        has_extract = self._ctx is not None and self._ctx.capabilities.has("web.extract.v1")
        use_chrome_first = prefer_chrome_fetch and has_fetch

        if use_chrome_first:
            chrome_resp = await self._fetch_via_chrome_fetch(url, headers, timeout_ms, secrets)
            if chrome_resp.ok and not is_v2ex_blocked_response(chrome_resp, expect_json):
                return chrome_resp

        direct = await self.perform_direct(
            HttpRequest(url=url, headers=headers, timeout_sec=timeout_sec),
            extra_secrets=secrets,
        )
        if direct.ok and not is_v2ex_blocked_response(direct, expect_json):
            return direct

        # Only trigger browser proxy fallback on anti-bot / auth block or when chrome was preferred.
        should_try_chrome = (
            direct.status_code in {401, 403, 429, 503}
            or is_v2ex_cloudflare_blocked(direct)
            or is_v2ex_login_required(direct)
            or (expect_json and direct.ok)
        )
        if not use_chrome_first and has_fetch and should_try_chrome:
            chrome_resp = await self._fetch_via_chrome_fetch(url, headers, timeout_ms, secrets)
            if chrome_resp.ok and not is_v2ex_blocked_response(chrome_resp, expect_json):
                return chrome_resp
            if is_v2ex_cloudflare_blocked(chrome_resp) or is_v2ex_login_required(chrome_resp):
                direct = chrome_resp

        if not expect_json and has_extract and should_try_chrome:
            ext_resp = await self._fetch_via_chrome_extract(url, timeout_ms, secrets)
            if ext_resp.ok and not is_v2ex_blocked_response(ext_resp, False):
                return ext_resp
            if is_v2ex_cloudflare_blocked(ext_resp) or is_v2ex_login_required(ext_resp):
                direct = ext_resp

        if not direct.error_code:
            err = classify_http_error(direct, expect_json=expect_json)
            direct.error_code = err.error_code
            direct.error = redact_sensitive(err.message, secrets)
        else:
            direct.error = redact_sensitive(direct.error, secrets)
        direct.ok = False
        return direct
