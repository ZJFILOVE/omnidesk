"""HTTPS-only X GraphQL, Syndication, and Chrome `web.fetch.v1` / `web.extract.v1` client."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from omnidesk_sdk import PluginContext

if __package__:
    from .x_auth import AuthManager, AuthResolution
    from .x_security import (
        MAX_RESPONSE_BYTES,
        SENSITIVE_HEADERS,
        XPluginError,
        is_same_origin,
        origin_string,
        redact_x_secrets,
        validate_https_url,
    )
else:
    from x_auth import AuthManager, AuthResolution  # type: ignore[no-redef]
    from x_security import (  # type: ignore[no-redef]
        MAX_RESPONSE_BYTES,
        SENSITIVE_HEADERS,
        XPluginError,
        is_same_origin,
        origin_string,
        redact_x_secrets,
        validate_https_url,
    )

PUBLIC_WEB_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_QUERY_IDS: dict[str, str] = {
    "HomeLatestTimeline": "DiTkXJgLqBBxCs7ZAYsbtA",
    "HomeTimeline": "HJFjzBgCs16TqxewQOeLNg",
    "ListLatestTweetsTimeline": "Pa45JvqZuKcW1plybfgBlQ",
    "ListsManagementPageTimeline": "nhYp4n09Hi5n2hQW0Q2v6g",
    "Bookmarks": "qToeLeMs43Q8cr7tRYXmaQ",
    "TweetDetail": "QuBlQ6SxNAQCt6-kBiCXCQ",
    "TweetResultByRestId": "V3vfsYzNEyD9tsf4xoFRgw",
    "SearchTimeline": "UN1i3zUiCWa-6r-Uaho4fw",
    "UserTweets": "E3opETHurmVJflFsUBVuUQ",
}
_CHROME_FORBIDDEN_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "cookie",
        "cookie2",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "te",
        "trailer",
        "origin",
        "referer",
        "user-agent",
    }
)
_QID_RE_1 = re.compile(r'queryId\s*:\s*"([A-Za-z0-9_-]{12,40})"\s*,\s*operationName\s*:\s*"([A-Za-z0-9_]+)"')
_QID_RE_2 = re.compile(r'operationName\s*:\s*"([A-Za-z0-9_]+)"\s*,\s*queryId\s*:\s*"([A-Za-z0-9_-]{12,40})"')
_MAIN_JS_RE = re.compile(r"https://abs\.twimg\.com/responsive-web/client-web/main\.[A-Za-z0-9._-]+\.js")
_CF_BODY_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "challenges.cloudflare.com",
    "just a moment...",
    "attention required! | cloudflare",
    "cf-challenge-running",
    "enable javascript and cookies to continue",
)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


@dataclass(slots=True)
class HttpRequest:
    """Outbound HTTPS request descriptor."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout_sec: float = 15.0
    follow_redirects: bool = True
    max_redirects: int = 5


@dataclass(slots=True)
class HttpResponse:
    """Bounded HTTP response with normalized error_code and transport metadata."""

    ok: bool = False
    status_code: int = 0
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    error_code: str = ""
    transport: str = "web.fetch.v1"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses directly so our manual redirect loop controls origin/header stripping."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def default_graphql_features() -> dict[str, bool]:
    """Standard X Web GraphQL feature flags aligned with v1."""
    return {
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    }


def default_article_field_toggles() -> dict[str, bool]:
    """Standard X Article / NoteTweet field toggles aligned with v1."""
    return {
        "withArticleRichContentState": True,
        "withArticlePlainText": True,
        "withGrokAnalyze": False,
        "withDisallowedReplyControls": False,
    }


def extract_query_ids_from_js(text: str) -> dict[str, str]:
    """Extract GraphQL `{queryId, operationName}` pairs from X Web JS bundle text."""
    if not text:
        return {}
    out: dict[str, str] = {}
    for m in _QID_RE_1.finditer(text):
        out[m.group(2)] = m.group(1)
    for m in _QID_RE_2.finditer(text):
        out[m.group(1)] = m.group(2)
    return out


def is_cloudflare_challenge(status_code: int, headers: Mapping[str, str], body: str) -> bool:
    """Detect Cloudflare Turnstile / JS challenge responses."""
    cf_mitigated = str(headers.get("cf-mitigated") or "").lower()
    if "challenge" in cf_mitigated:
        return True
    low_body = (body or "").lower()
    if any(marker in low_body for marker in _CF_BODY_MARKERS):
        return True
    server = str(headers.get("server") or "").lower()
    if status_code in {403, 503} and "cloudflare" in server and ("challenge" in low_body or "captcha" in low_body):
        return True
    return False


def is_login_redirect_url(url: str) -> bool:
    """Return True if URL points to X login / authentication wall."""
    low = (url or "").lower()
    return "/i/flow/login" in low or low.rstrip("/").endswith("/login")


def _is_chrome_forbidden_header(lower_name: str) -> bool:
    if lower_name.startswith(("proxy-", "sec-", "x-omnidesk-")):
        return True
    return lower_name in _CHROME_FORBIDDEN_HEADERS


def _strip_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}


def _classify_http_failure(
    status_code: int,
    headers: Mapping[str, str],
    body: str,
    final_url: str = "",
) -> tuple[str, str]:
    if is_cloudflare_challenge(status_code, headers, body):
        return "cloudflare_blocked", "触发 Cloudflare 人机验证，请通过 Chrome 完成校验"
    if status_code == 401 or is_login_redirect_url(final_url):
        return "auth_required", "X 会话未登录或已过期 (HTTP 401)，请重新登录"
    if status_code == 403:
        return "auth_required", "X 拒绝访问 (HTTP 403)，会话凭证或 CSRF 可能已失效"
    if status_code == 404:
        return "not_found", "请求的 X 资源不存在或接口已轮换 (HTTP 404)"
    if status_code == 429:
        return "rate_limited", "X 接口请求触发限流 (HTTP 429)"
    if status_code in {400, 422}:
        return "upstream_error", f"X 接口参数或 queryId 无效 (HTTP {status_code})"
    if status_code >= 500:
        return "service_offline", f"X 上游服务异常 (HTTP {status_code})"
    return "service_offline", f"X 上游返回 HTTP {status_code}"


def _inspect_graphql_errors(body: str) -> tuple[bool, bool, str, str]:
    """Inspect GraphQL JSON body for errors; return `(has_fatal_err, stale_qid, error_code, message)`."""
    if not body.strip():
        return True, False, "parse_error", "X 接口返回空响应"
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return True, False, "parse_error", "X 接口返回非法的 JSON 响应"
    if not isinstance(parsed, Mapping):
        return True, False, "parse_error", "X 接口 JSON 响应格式无效"
    errors = parsed.get("errors")
    has_data = isinstance(parsed.get("data"), Mapping)
    if isinstance(errors, list) and errors and not has_data:
        stale_qid = False
        err_code = "upstream_error"
        messages: list[str] = []
        for err in errors:
            if not isinstance(err, Mapping):
                continue
            code = err.get("code")
            msg = str(err.get("message") or "").strip()
            if msg:
                messages.append(msg)
            low_msg = msg.lower()
            if code in {32, 89, 239, 353} or any(
                w in low_msg for w in ("csrf", "authenticate", "unauthorized", "login", "expired")
            ):
                err_code = "auth_required"
            elif code == 88 or "rate limit" in low_msg:
                err_code = "rate_limited"
            elif "query" in low_msg or "operation" in low_msg:
                stale_qid = True
        return True, stale_qid, err_code, "; ".join(messages) or "X GraphQL 接口返回错误"
    return False, False, "", ""


class XClient:
    """Async X client prioritizing `web.fetch.v1` with safe HTTPS fallback and queryId refresh."""

    def __init__(
        self,
        ctx: PluginContext | None = None,
        *,
        base_url: str = "https://x.com",
        syndication_base_url: str = "https://cdn.syndication.twimg.com",
        allow_loopback_http: bool = False,
        transport_override: Callable[[HttpRequest], Any] | None = None,
    ) -> None:
        self._ctx = ctx
        self._base_url = base_url.rstrip("/")
        self._syndication_base_url = syndication_base_url.rstrip("/")
        self._allow_loopback_http = allow_loopback_http
        self._transport_override = transport_override
        self._default_timeout_sec = 15.0
        self._query_ids: dict[str, str] = dict(_DEFAULT_QUERY_IDS)

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def set_transport_override(self, fn: Callable[[HttpRequest], Any] | None) -> None:
        self._transport_override = fn

    def set_default_timeout_sec(self, sec: float) -> None:
        self._default_timeout_sec = max(0.1, min(120.0, float(sec)))

    def set_query_id(self, operation: str, query_id: str) -> None:
        if operation and query_id:
            self._query_ids[operation] = query_id

    def query_id(self, operation: str) -> str:
        return self._query_ids.get(operation, "")

    def query_ids_snapshot(self) -> dict[str, str]:
        return dict(self._query_ids)

    @property
    def allowed_loopback_origins(self) -> frozenset[str]:
        origins: set[str] = set()
        for u in (self._base_url, self._syndication_base_url):
            if u.startswith("http://"):
                origins.add(origin_string(u))
        return frozenset(origins)

    def _has_cap(self, cap_id: str) -> bool:
        return self._ctx is not None and bool(self._ctx.capabilities.has(cap_id))

    def build_graphql_url(
        self,
        operation: str,
        variables: Mapping[str, Any],
        features: Mapping[str, Any] | None = None,
        field_toggles: Mapping[str, Any] | None = None,
    ) -> str:
        qid = self.query_id(operation) or operation
        feat = dict(features) if features else default_graphql_features()
        params = [
            ("variables", json.dumps(dict(variables), separators=(",", ":"), ensure_ascii=False)),
            ("features", json.dumps(feat, separators=(",", ":"), ensure_ascii=False)),
        ]
        if field_toggles:
            params.append(
                ("fieldToggles", json.dumps(dict(field_toggles), separators=(",", ":"), ensure_ascii=False))
            )
        return f"{self._base_url}/i/api/graphql/{qid}/{operation}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def build_x_graphql_headers(auth_res: AuthResolution, *, for_chrome_fetch: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {PUBLIC_WEB_BEARER_TOKEN}",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        }
        if auth_res.ct0:
            headers["x-csrf-token"] = auth_res.ct0
        if auth_res.is_authenticated:
            headers["x-twitter-auth-type"] = "OAuth2Session"
        if not for_chrome_fetch and auth_res.cookie_header:
            headers["Cookie"] = auth_res.cookie_header
            headers["Origin"] = "https://x.com"
            headers["Referer"] = "https://x.com/home"
        return headers

    @staticmethod
    def is_blocked_or_stale_response(resp: HttpResponse) -> tuple[bool, bool]:
        """Return `(is_blocked_or_error, is_stale_query_id)`."""
        if resp.status_code in {400, 404, 422}:
            return True, True
        if not resp.ok or resp.status_code in {401, 403, 429} or not resp.body:
            return True, False
        has_err, stale_qid, _, _ = _inspect_graphql_errors(resp.body)
        return has_err, stale_qid

    def _send_single_sync(self, req: HttpRequest, extra_secrets: tuple[str, ...]) -> HttpResponse:
        safe_url = validate_https_url(
            req.url,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=self.allowed_loopback_origins,
        )
        parsed = urllib.parse.urlsplit(safe_url)
        handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
        if parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)

        body_bytes = req.body.encode("utf-8") if req.body and req.method not in {"GET", "HEAD"} else None
        url_req = urllib.request.Request(safe_url, data=body_bytes, method=req.method.upper())
        has_ua = False
        for k, v in req.headers.items():
            if k.lower() == "user-agent":
                has_ua = True
            url_req.add_header(k, v)
        if not has_ua:
            url_req.add_header("User-Agent", _DEFAULT_USER_AGENT)

        timeout_sec = max(0.05, float(req.timeout_sec or self._default_timeout_sec))
        deadline = time.monotonic() + timeout_sec

        def _read_bounded(stream: Any, status: int, hdrs: dict[str, str], final_u: str) -> HttpResponse:
            cl_raw = hdrs.get("content-length")
            if cl_raw is not None:
                try:
                    if int(cl_raw.strip()) > MAX_RESPONSE_BYTES:
                        return HttpResponse(
                            ok=False,
                            status_code=status,
                            final_url=final_u,
                            headers=hdrs,
                            error="响应体超出 8 MiB 安全上限",
                            error_code="response_too_large",
                            transport="https",
                        )
                except ValueError:
                    pass
            buf = bytearray()
            while True:
                if time.monotonic() > deadline:
                    return HttpResponse(
                        ok=False,
                        status_code=0,
                        final_url=final_u,
                        headers=hdrs,
                        error="X HTTP 请求超时",
                        error_code="timeout",
                        transport="https",
                    )
                chunk = stream.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_RESPONSE_BYTES:
                    return HttpResponse(
                        ok=False,
                        status_code=status,
                        final_url=final_u,
                        headers=hdrs,
                        error="响应体超出 8 MiB 安全上限",
                        error_code="response_too_large",
                        transport="https",
                    )
            text = bytes(buf).decode("utf-8", errors="replace")
            ok = 200 <= status < 400
            err_code = ""
            err_msg = ""
            if not ok and status not in _REDIRECT_CODES:
                err_code, err_msg = _classify_http_failure(status, hdrs, text, final_u)
            elif ok and is_cloudflare_challenge(status, hdrs, text):
                ok = False
                err_code, err_msg = "cloudflare_blocked", "触发 Cloudflare 人机验证，请通过 Chrome 完成校验"
            return HttpResponse(
                ok=ok,
                status_code=status,
                final_url=final_u,
                headers=hdrs,
                body=text,
                error=err_msg,
                error_code=err_code,
                transport="https",
            )

        try:
            with opener.open(url_req, timeout=timeout_sec) as resp:
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return _read_bounded(resp, int(resp.status), hdrs, str(resp.geturl() or safe_url))
        except urllib.error.HTTPError as exc:
            hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            status = int(exc.code)
            try:
                return _read_bounded(exc, status, hdrs, safe_url)
            finally:
                exc.close()
        except (TimeoutError, socket.timeout):
            return HttpResponse(
                ok=False, final_url=safe_url, error="X HTTP 请求超时", error_code="timeout", transport="https"
            )
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                return HttpResponse(
                    ok=False, final_url=safe_url, error="X HTTP 请求超时", error_code="timeout", transport="https"
                )
            return HttpResponse(
                ok=False,
                final_url=safe_url,
                error=redact_x_secrets(f"X 网络请求失败: {reason}", extra_secrets),
                error_code="service_offline",
                transport="https",
            )
        except OSError as exc:
            return HttpResponse(
                ok=False,
                final_url=safe_url,
                error=redact_x_secrets(f"X 连接异常: {type(exc).__name__}", extra_secrets),
                error_code="service_offline",
                transport="https",
            )

    async def perform_direct(
        self,
        req: HttpRequest,
        *,
        extra_secrets: tuple[str, ...] = (),
    ) -> HttpResponse:
        """Execute HTTPS request with bounded redirects and cross-origin credential stripping."""
        try:
            start_url = validate_https_url(
                req.url,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self.allowed_loopback_origins,
            )
        except XPluginError as exc:
            return HttpResponse(ok=False, error=exc.message, error_code=exc.error_code, transport="https")

        cur = HttpRequest(
            url=start_url,
            method=req.method.upper() or "GET",
            headers=dict(req.headers),
            body=req.body,
            timeout_sec=req.timeout_sec if req.timeout_sec > 0 else self._default_timeout_sec,
            follow_redirects=req.follow_redirects,
            max_redirects=max(0, min(10, int(req.max_redirects))),
        )

        for hop in range(cur.max_redirects + 1):
            if self._transport_override is not None:
                raw_resp = self._transport_override(cur)
                resp = await raw_resp if inspect.isawaitable(raw_resp) else raw_resp
                if not resp.final_url:
                    resp.final_url = cur.url
                if not resp.transport:
                    resp.transport = "override"
                if len(resp.body.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=resp.final_url,
                        headers=resp.headers,
                        error="响应体超出 8 MiB 安全上限",
                        error_code="response_too_large",
                        transport=resp.transport,
                    )
                if not resp.ok and resp.status_code not in _REDIRECT_CODES and not resp.error_code:
                    resp.error_code, resp.error = _classify_http_failure(
                        resp.status_code, resp.headers, resp.body, resp.final_url
                    )
                elif resp.ok and is_cloudflare_challenge(resp.status_code, resp.headers, resp.body):
                    resp.ok = False
                    resp.error_code = "cloudflare_blocked"
                    resp.error = "触发 Cloudflare 人机验证，请通过 Chrome 完成校验"
            else:
                resp = await asyncio.to_thread(self._send_single_sync, cur, extra_secrets)

            if cur.follow_redirects and resp.status_code in _REDIRECT_CODES:
                if hop >= cur.max_redirects:
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=cur.url,
                        error="HTTP 重定向次数超出上限",
                        error_code="service_offline",
                        transport=resp.transport,
                    )
                location = next(
                    (v for k, v in resp.headers.items() if k.lower() == "location"),
                    "",
                )
                if not location:
                    return resp
                next_url = urllib.parse.urljoin(cur.url, location.strip())
                try:
                    next_url = validate_https_url(
                        next_url,
                        allow_loopback_http=self._allow_loopback_http,
                        allowed_loopback_origins=self.allowed_loopback_origins,
                    )
                except XPluginError as exc:
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=cur.url,
                        error=exc.message,
                        error_code=exc.error_code,
                        transport=resp.transport,
                    )
                if is_login_redirect_url(next_url):
                    return HttpResponse(
                        ok=False,
                        status_code=resp.status_code,
                        final_url=next_url,
                        error="X 重定向至登录页，需要重新登录会话",
                        error_code="auth_required",
                        transport=resp.transport,
                    )
                if not is_same_origin(cur.url, next_url):
                    cur.headers = _strip_sensitive_headers(cur.headers)
                if resp.status_code == 303 or (resp.status_code in {301, 302} and cur.method == "POST"):
                    cur.method = "GET"
                    cur.body = ""
                cur.url = next_url
                continue
            return resp

        return HttpResponse(ok=False, error="HTTP 重定向次数超出上限", error_code="service_offline", transport="https")

    async def fetch_via_chrome_web_fetch(
        self,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: str = "",
        timeout_ms: int = 15000,
        tab_url: str = "https://x.com/home",
        *,
        extra_secrets: tuple[str, ...] = (),
    ) -> HttpResponse:
        """Proxy same-origin `fetch()` through `plugin-chrome-web` (`web.fetch.v1`)."""
        try:
            safe_url = validate_https_url(
                url,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self.allowed_loopback_origins,
            )
        except XPluginError as exc:
            return HttpResponse(ok=False, error=exc.message, error_code=exc.error_code, transport="web.fetch.v1")

        if not self._has_cap("web.fetch.v1"):
            return HttpResponse(
                ok=False, error="web.fetch.v1 不可用", error_code="service_offline", transport="web.fetch.v1"
            )

        safe_headers = {
            k: v for k, v in (headers or {}).items() if not _is_chrome_forbidden_header(k.lower())
        }
        req_payload: dict[str, Any] = {
            "action": "fetch",
            "url": safe_url,
            "method": (method or "GET").upper(),
            "headers": safe_headers,
            "body": "" if (method or "GET").upper() in {"GET", "HEAD"} else body,
            "credentials": "include",
            "open_tab_if_missing": True,
            "timeout_ms": max(100, min(60000, int(timeout_ms))),
        }
        if tab_url and is_same_origin(safe_url, tab_url):
            req_payload["tab_url"] = tab_url

        assert self._ctx is not None
        res = await self._ctx.capabilities.call("web.fetch.v1", req_payload)
        if not res.ok or not isinstance(res.data, Mapping):
            err_code = str(res.data.get("error_code") or "service_offline") if isinstance(res.data, Mapping) else "service_offline"
            if err_code == "permission_denied":
                err_code = "auth_required"
            return HttpResponse(
                ok=False,
                error=redact_x_secrets(res.error or "web.fetch.v1 调用失败", extra_secrets),
                error_code=err_code,
                transport="web.fetch.v1",
            )

        data = res.data
        status = int(data.get("status") or 200)
        final_url = str(data.get("url") or safe_url)
        raw_body = data.get("body")
        resp_body = "" if raw_body is None else str(raw_body)
        if len(resp_body.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
            return HttpResponse(
                ok=False,
                status_code=status,
                final_url=final_url,
                error="web.fetch.v1 响应超出 8 MiB 上限",
                error_code="response_too_large",
                transport="web.fetch.v1",
            )
        resp_hdrs = (
            {str(k).lower(): str(v) for k, v in data["headers"].items()}
            if isinstance(data.get("headers"), Mapping)
            else {}
        )
        if is_cloudflare_challenge(status, resp_hdrs, resp_body):
            return HttpResponse(
                ok=False,
                status_code=status,
                final_url=final_url,
                headers=resp_hdrs,
                body=resp_body,
                error="触发 Cloudflare 人机验证，请通过 Chrome 完成校验",
                error_code="cloudflare_blocked",
                transport="web.fetch.v1",
            )
        if is_login_redirect_url(final_url):
            return HttpResponse(
                ok=False,
                status_code=status,
                final_url=final_url,
                headers=resp_hdrs,
                body=resp_body,
                error="X 会话未登录或已跳转至登录页",
                error_code="auth_required",
                transport="web.fetch.v1",
            )
        ok = 200 <= status < 400 and bool(resp_body)
        err_code = ""
        err_msg = ""
        if not ok:
            if 200 <= status < 400 and not resp_body:
                err_code, err_msg = "parse_error", "X 接口返回空响应"
            else:
                err_code, err_msg = _classify_http_failure(status, resp_hdrs, resp_body, final_url)
        return HttpResponse(
            ok=ok,
            status_code=status,
            final_url=final_url,
            headers=resp_hdrs,
            body=resp_body,
            error=err_msg,
            error_code=err_code,
            transport="web.fetch.v1",
        )

    async def fetch_via_chrome_extract(
        self,
        url: str,
        timeout_ms: int = 15000,
        *,
        extra_secrets: tuple[str, ...] = (),
    ) -> HttpResponse:
        """Extract rendered DOM HTML via `plugin-chrome-web` (`web.extract.v1`)."""
        try:
            safe_url = validate_https_url(
                url,
                allow_loopback_http=self._allow_loopback_http,
                allowed_loopback_origins=self.allowed_loopback_origins,
            )
        except XPluginError as exc:
            return HttpResponse(ok=False, error=exc.message, error_code=exc.error_code, transport="web.extract.v1")

        if not self._has_cap("web.extract.v1"):
            return HttpResponse(
                ok=False, error="web.extract.v1 不可用", error_code="service_offline", transport="web.extract.v1"
            )

        assert self._ctx is not None
        res = await self._ctx.capabilities.call(
            "web.extract.v1",
            {
                "action": "extract",
                "url": safe_url,
                "mode": "auto",
                "use_cookies": True,
                "render_js": True,
                "timeout_ms": max(100, min(60000, int(timeout_ms))),
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            err_code = str(res.data.get("error_code") or "service_offline") if isinstance(res.data, Mapping) else "service_offline"
            return HttpResponse(
                ok=False,
                error=redact_x_secrets(res.error or "web.extract.v1 调用失败", extra_secrets),
                error_code=err_code,
                transport="web.extract.v1",
            )
        data = res.data
        status = int(data.get("status") or 200)
        final_url = str(data.get("url") or safe_url)
        html_body = str(data.get("html") or data.get("text") or "")
        if len(html_body.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
            return HttpResponse(
                ok=False,
                status_code=status,
                final_url=final_url,
                error="web.extract.v1 响应超出 8 MiB 上限",
                error_code="response_too_large",
                transport="web.extract.v1",
            )
        if is_cloudflare_challenge(status, {}, html_body):
            return HttpResponse(
                ok=False,
                status_code=status,
                final_url=final_url,
                body=html_body,
                error="触发 Cloudflare 人机验证，请通过 Chrome 完成校验",
                error_code="cloudflare_blocked",
                transport="web.extract.v1",
            )
        ok = 200 <= status < 400 and bool(html_body.strip())
        err_code = "" if ok else ("parse_error" if 200 <= status < 400 else _classify_http_failure(status, {}, html_body, final_url)[0])
        err_msg = "" if ok else ("页面渲染提取结果为空" if 200 <= status < 400 else f"HTTP {status}")
        return HttpResponse(
            ok=ok,
            status_code=status,
            final_url=final_url,
            body=html_body,
            error=err_msg,
            error_code=err_code,
            transport="web.extract.v1",
        )

    async def refresh_query_ids_from_bundle(self, auth: AuthManager) -> bool:
        """Fetch X home HTML and JS bundle to refresh rotated GraphQL queryIds."""
        timeout_sec = self._default_timeout_sec
        timeout_ms = int(timeout_sec * 1000)
        home_url = f"{self._base_url}/home"
        if self._has_cap("web.fetch.v1"):
            html_resp = await self.fetch_via_chrome_web_fetch(
                home_url,
                "GET",
                {"Accept": "text/html"},
                "",
                timeout_ms,
                home_url,
                extra_secrets=auth.extra_secrets(),
            )
        else:
            auth_res = await auth.resolve_for_url(home_url)
            hdrs = {"Cookie": auth_res.cookie_header} if auth_res.cookie_header else {}
            html_resp = await self.perform_direct(
                HttpRequest(url=home_url, method="GET", headers=hdrs, timeout_sec=timeout_sec),
                extra_secrets=auth.extra_secrets(),
            )
        if not html_resp.ok or not html_resp.body:
            return False

        discovered = extract_query_ids_from_js(html_resp.body)
        if not discovered:
            if m := _MAIN_JS_RE.search(html_resp.body):
                bundle_url = m.group(0)
                if self._has_cap("web.fetch.v1"):
                    js_resp = await self.fetch_via_chrome_web_fetch(
                        bundle_url, "GET", {"Accept": "*/*"}, "", timeout_ms, ""
                    )
                else:
                    js_resp = await self.perform_direct(
                        HttpRequest(url=bundle_url, method="GET", timeout_sec=timeout_sec)
                    )
                if js_resp.ok and js_resp.body:
                    discovered = extract_query_ids_from_js(js_resp.body)
        if not discovered:
            return False
        for op, qid in discovered.items():
            if op and qid:
                self._query_ids[op] = qid
        return True

    async def fetch_graphql(
        self,
        operation: str,
        variables: Mapping[str, Any],
        auth: AuthManager,
        *,
        prefer_chrome_fetch: bool = True,
        tab_url: str = "",
        features_override: Mapping[str, Any] | None = None,
        field_toggles_override: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        """Execute an X GraphQL operation via Chrome `web.fetch.v1` or direct HTTPS."""
        timeout_sec = auth.config.request_timeout_sec or self._default_timeout_sec
        timeout_ms = int(timeout_sec * 1000)
        auth_res = await auth.resolve_for_url(f"{self._base_url}/")
        url = self.build_graphql_url(operation, variables, features_override, field_toggles_override)
        effective_tab_url = tab_url or f"{self._base_url}/home"
        can_chrome_fetch = self._has_cap("web.fetch.v1")
        secrets = auth.extra_secrets()

        if prefer_chrome_fetch and can_chrome_fetch:
            headers = self.build_x_graphql_headers(auth_res, for_chrome_fetch=True)
            resp = await self.fetch_via_chrome_web_fetch(
                url, "GET", headers, "", timeout_ms, effective_tab_url, extra_secrets=secrets
            )
            if resp.status_code in {401, 403} and resp.error_code != "cloudflare_blocked":
                synced, _, _, _ = await auth.sync_from_chrome()
                if synced:
                    auth_res = await auth.resolve_for_url(f"{self._base_url}/")
                    headers = self.build_x_graphql_headers(auth_res, for_chrome_fetch=True)
                    resp = await self.fetch_via_chrome_web_fetch(
                        url, "GET", headers, "", timeout_ms, effective_tab_url, extra_secrets=secrets
                    )
            blocked, stale_qid = self.is_blocked_or_stale_response(resp)
            if blocked and stale_qid and await self.refresh_query_ids_from_bundle(auth):
                url = self.build_graphql_url(operation, variables, features_override, field_toggles_override)
                resp = await self.fetch_via_chrome_web_fetch(
                    url, "GET", headers, "", timeout_ms, effective_tab_url, extra_secrets=secrets
                )
            if resp.ok or self._transport_override is None:
                return resp

        direct_req = HttpRequest(
            url=url,
            method="GET",
            headers=self.build_x_graphql_headers(auth_res, for_chrome_fetch=False),
            timeout_sec=timeout_sec,
        )
        direct_resp = await self.perform_direct(direct_req, extra_secrets=secrets)
        blocked, stale_qid = self.is_blocked_or_stale_response(direct_resp)
        if not blocked:
            return direct_resp

        if stale_qid and await self.refresh_query_ids_from_bundle(auth):
            direct_req.url = self.build_graphql_url(operation, variables, features_override, field_toggles_override)
            direct_resp = await self.perform_direct(direct_req, extra_secrets=secrets)
            blocked2, _ = self.is_blocked_or_stale_response(direct_resp)
            if not blocked2:
                return direct_resp

        if not prefer_chrome_fetch and can_chrome_fetch:
            headers = self.build_x_graphql_headers(auth_res, for_chrome_fetch=True)
            chrome_resp = await self.fetch_via_chrome_web_fetch(
                direct_req.url, "GET", headers, "", timeout_ms, effective_tab_url, extra_secrets=secrets
            )
            if chrome_resp.ok:
                return chrome_resp
        return direct_resp

    async def fetch_graphql_json(
        self,
        operation: str,
        variables: Mapping[str, Any],
        auth: AuthManager,
        *,
        tab_url: str = "",
        features_override: Mapping[str, Any] | None = None,
        field_toggles_override: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute GraphQL query and parse JSON fail-closed, raising `XPluginError` on error."""
        resp = await self.fetch_graphql(
            operation,
            variables,
            auth,
            prefer_chrome_fetch=auth.config.prefer_chrome_fetch,
            tab_url=tab_url,
            features_override=features_override,
            field_toggles_override=field_toggles_override,
        )
        if not resp.ok:
            raise XPluginError(
                resp.error_code or "service_offline",
                resp.error or f"X GraphQL {operation} 请求失败",
                http_status=resp.status_code or None,
                extra_secrets=auth.extra_secrets(),
            )
        has_err, _, err_code, err_msg = _inspect_graphql_errors(resp.body)
        if has_err:
            raise XPluginError(
                err_code,
                err_msg,
                http_status=resp.status_code or None,
                extra_secrets=auth.extra_secrets(),
            )
        parsed = json.loads(resp.body)
        return dict(parsed)

    async def fetch_home_latest_timeline(self, auth: AuthManager, count: int = 20, cursor: str = "") -> dict[str, Any]:
        vars_obj: dict[str, Any] = {
            "count": max(1, min(100, count)),
            "includePromotedContent": False,
            "latestControlAvailable": True,
            "requestContext": "launch",
            "seenTweetIds": [],
        }
        if cursor:
            vars_obj["cursor"] = cursor
        return await self.fetch_graphql_json("HomeLatestTimeline", vars_obj, auth, tab_url=f"{self._base_url}/home")

    async def fetch_home_timeline(self, auth: AuthManager, count: int = 20, cursor: str = "") -> dict[str, Any]:
        vars_obj: dict[str, Any] = {
            "count": max(1, min(100, count)),
            "includePromotedContent": False,
            "latestControlAvailable": True,
            "requestContext": "launch",
            "withCommunity": True,
            "seenTweetIds": [],
        }
        if cursor:
            vars_obj["cursor"] = cursor
        return await self.fetch_graphql_json("HomeTimeline", vars_obj, auth, tab_url=f"{self._base_url}/home")

    async def fetch_list_latest_tweets_timeline(
        self, auth: AuthManager, list_id: str, count: int = 20, cursor: str = ""
    ) -> dict[str, Any]:
        vars_obj: dict[str, Any] = {"listId": list_id, "count": max(1, min(100, count))}
        if cursor:
            vars_obj["cursor"] = cursor
        tab_url = f"{self._base_url}/i/lists/{list_id}" if list_id else f"{self._base_url}/home"
        return await self.fetch_graphql_json("ListLatestTweetsTimeline", vars_obj, auth, tab_url=tab_url)

    async def fetch_user_lists(self, auth: AuthManager, count: int = 50) -> dict[str, Any]:
        return await self.fetch_graphql_json(
            "ListsManagementPageTimeline",
            {"count": max(1, min(100, count))},
            auth,
            tab_url=f"{self._base_url}/home",
        )

    async def fetch_bookmarks_timeline(self, auth: AuthManager, count: int = 20, cursor: str = "") -> dict[str, Any]:
        vars_obj: dict[str, Any] = {"count": max(1, min(100, count)), "includePromotedContent": False}
        if cursor:
            vars_obj["cursor"] = cursor
        return await self.fetch_graphql_json(
            "Bookmarks", vars_obj, auth, tab_url=f"{self._base_url}/i/bookmarks"
        )

    async def fetch_tweet_detail(self, auth: AuthManager, focal_tweet_id: str, cursor: str = "") -> dict[str, Any]:
        vars_obj: dict[str, Any] = {
            "focalTweetId": focal_tweet_id,
            "with_rux_injections": False,
            "rankingMode": "Relevance",
            "includePromotedContent": False,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
        }
        if cursor:
            vars_obj["cursor"] = cursor
            vars_obj["referrer"] = "tweet"
        return await self.fetch_graphql_json(
            "TweetDetail",
            vars_obj,
            auth,
            tab_url=f"{self._base_url}/home",
            features_override=default_graphql_features(),
            field_toggles_override=default_article_field_toggles(),
        )

    async def fetch_tweet_result_by_rest_id(self, auth: AuthManager, tweet_id: str) -> dict[str, Any]:
        vars_obj: dict[str, Any] = {
            "tweetId": tweet_id,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        }
        return await self.fetch_graphql_json(
            "TweetResultByRestId",
            vars_obj,
            auth,
            tab_url=f"{self._base_url}/home",
            features_override=default_graphql_features(),
            field_toggles_override=default_article_field_toggles(),
        )

    async def fetch_search_timeline(
        self,
        auth: AuthManager,
        raw_query: str,
        count: int = 20,
        product: str = "Latest",
        cursor: str = "",
    ) -> dict[str, Any]:
        vars_obj: dict[str, Any] = {
            "rawQuery": raw_query,
            "count": max(1, min(100, count)),
            "querySource": "typed_query",
            "product": product or "Latest",
        }
        if cursor:
            vars_obj["cursor"] = cursor
        return await self.fetch_graphql_json("SearchTimeline", vars_obj, auth, tab_url=f"{self._base_url}/explore")

    async def fetch_tweet_syndication(self, tweet_id: str) -> HttpResponse:
        """Fetch public Syndication JSON endpoint for single public tweets."""
        url = (
            f"{self._syndication_base_url}/tweet-result?id={urllib.parse.quote(tweet_id, safe='')}"
            "&lang=en&token=0"
        )
        timeout_sec = self._default_timeout_sec
        if self._has_cap("web.fetch.v1") and self._transport_override is None:
            r = await self.fetch_via_chrome_web_fetch(
                url, "GET", {"Accept": "application/json"}, "", int(timeout_sec * 1000), ""
            )
            if r.ok:
                return r
        return await self.perform_direct(
            HttpRequest(
                url=url,
                method="GET",
                headers={"Accept": "application/json"},
                timeout_sec=timeout_sec,
            )
        )
