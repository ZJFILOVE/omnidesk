"""Async-safe Bilibili HTTP client honoring environment proxies for WAN requests."""

from __future__ import annotations

import asyncio
import hashlib
import http.cookies
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any

from .security import (
    DEFAULT_API_BASE,
    DEFAULT_LIVE_BASE,
    DEFAULT_PASSPORT_BASE,
    DEFAULT_USER_AGENT,
    SecurityValidationError,
    cookie_value,
    is_loopback_host,
    origin_key,
    redact_bilibili_secrets,
    validate_api_base,
    validate_proxy_url,
    validate_url,
)

_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
_TRANSIENT_HTTP = frozenset({429, 502, 503, 504})
_WBI_FILTER_TRANS = str.maketrans("", "", "!'()*")


class BilibiliApiError(RuntimeError):
    """Fail-closed Bilibili API/transport error with scrubbed message and machine error_code."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "service_offline",
        http_status: int | None = None,
        bili_code: int | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        safe_msg = redact_bilibili_secrets(message, extra_secrets)
        super().__init__(safe_msg)
        self.message = safe_msg
        self.error_code = error_code
        self.http_status = http_status
        self.bili_code = bili_code


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Blocks redirects outside allowlist and strips Cookie/Authorization cross-host."""

    def __init__(self, *, allow_cdn: bool, allowed_loopback_origins: frozenset[str]) -> None:
        self._allow_cdn = allow_cdn
        self._allowed_origins = allowed_loopback_origins

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        try:
            safe_url = validate_url(
                resolved, allow_cdn=self._allow_cdn, allowed_loopback_origins=self._allowed_origins
            )
        except SecurityValidationError as exc:
            raise urllib.error.HTTPError(resolved, 403, f"Redirect blocked: {exc}", headers, None) from None

        new_req = super().redirect_request(req, fp, code, msg, headers, safe_url)
        if new_req is None:
            return None
        old_host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
        new_host = (urllib.parse.urlsplit(safe_url).hostname or "").lower()
        if old_host != new_host:
            for hdr in ("Cookie", "cookie", "Authorization", "authorization"):
                new_req.headers.pop(hdr, None)
                new_req.unredirected_hdrs.pop(hdr, None)
        return new_req


def _map_bili_code(code: int, msg: str, secrets: Iterable[str]) -> BilibiliApiError:
    mapping: dict[int, tuple[str, str]] = {
        -101: ("unauthorized", "Bilibili 登录态无效或已过期 (-101)"),
        -412: ("rate_limited", "触发 Bilibili 风控或限流 (-412)"),
        -352: ("rate_limited", "触发 Bilibili 风控校验 (-352)"),
        -509: ("rate_limited", "请求过于频繁 (-509)"),
        -799: ("rate_limited", "请求过于频繁 (-799)"),
        -10403: ("quality_unavailable", "当前账号无权播放该清晰度或地区受限 (-10403)"),
        -404: ("not_found", "目标视频或资源不存在 (-404)"),
        62002: ("not_found", "稿件不可见或已删除 (62002)"),
        62004: ("not_found", "视频审核中或不存在 (62004)"),
        -403: ("unauthorized", "无访问权限 (-403)"),
        -400: ("invalid_argument", "请求参数无效 (-400)"),
    }
    err_code, default_msg = mapping.get(code, ("upstream_error", f"Bilibili 接口返回错误 ({code})"))
    return BilibiliApiError(msg or default_msg, error_code=err_code, bili_code=code, extra_secrets=secrets)


class BilibiliClient:
    """Thread-safe / asyncio-friendly Bilibili API client; WAN requests honor environment proxies."""

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_API_BASE,
        proxy: str = "",
        timeout_sec: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self.api_base = validate_api_base(api_base)
        self.proxy = validate_proxy_url(proxy)
        self.timeout_sec = max(0.05, min(60.0, float(timeout_sec)))
        self.max_retries = max(0, min(3, int(max_retries)))
        self._wbi_keys: tuple[str, str, float] | None = None

    @property
    def is_custom_base(self) -> bool:
        return self.api_base != DEFAULT_API_BASE

    @property
    def passport_base(self) -> str:
        return self.api_base if self.is_custom_base else DEFAULT_PASSPORT_BASE

    @property
    def live_base(self) -> str:
        return self.api_base if self.is_custom_base else DEFAULT_LIVE_BASE

    @property
    def allowed_loopback_origins(self) -> frozenset[str]:
        host = urllib.parse.urlsplit(self.api_base).hostname or ""
        return frozenset({origin_key(self.api_base)}) if is_loopback_host(host) else frozenset()

    def clear_cache(self) -> None:
        self._wbi_keys = None

    def _build_opener(self, *, allow_cdn: bool) -> urllib.request.OpenerDirector:
        if not self.proxy:
            api_host = urllib.parse.urlsplit(self.api_base).hostname or ""
            proxy_handler = (
                urllib.request.ProxyHandler({})
                if is_loopback_host(api_host)
                else urllib.request.ProxyHandler()
            )
        else:
            scheme = urllib.parse.urlsplit(self.proxy).scheme.lower()
            if scheme not in {"http", "https"}:
                raise BilibiliApiError(
                    f"HTTP client only supports http/https proxy directly (got {scheme})",
                    error_code="invalid_argument",
                )
            proxy_handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
        redirect_handler = _SafeRedirectHandler(
            allow_cdn=allow_cdn, allowed_loopback_origins=self.allowed_loopback_origins
        )
        return urllib.request.build_opener(proxy_handler, redirect_handler)

    def _request_sync(
        self, url: str, *, cookie: str = "", referer: str = "https://www.bilibili.com/",
        allow_cdn: bool = False, secrets: tuple[str, ...] = ()
    ) -> tuple[bytes, list[str], str]:
        safe_url = validate_url(url, allow_cdn=allow_cdn, allowed_loopback_origins=self.allowed_loopback_origins)
        opener = self._build_opener(allow_cdn=allow_cdn)
        last_exc: BilibiliApiError | None = None

        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(safe_url, method="GET")
            req.add_header("User-Agent", DEFAULT_USER_AGENT)
            req.add_header("Referer", referer)
            req.add_header("Accept", "application/json, text/plain, */*")
            if cookie:
                req.add_header("Cookie", cookie)
            try:
                with opener.open(req, timeout=self.timeout_sec) as resp:
                    return resp.read(), resp.headers.get_all("Set-Cookie") or [], resp.geturl()
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if status == 401:
                    raise BilibiliApiError("未授权访问 Bilibili 接口 (HTTP 401)", error_code="unauthorized", http_status=401, extra_secrets=secrets) from None
                if status == 412:
                    raise BilibiliApiError("触发 Bilibili 风控拦截 (HTTP 412)", error_code="rate_limited", http_status=412, extra_secrets=secrets) from None
                if status == 403:
                    raise BilibiliApiError("访问被拒绝 (HTTP 403)", error_code="unauthorized", http_status=403, extra_secrets=secrets) from None
                if status == 404:
                    raise BilibiliApiError("目标资源不存在 (HTTP 404)", error_code="not_found", http_status=404, extra_secrets=secrets) from None
                code = "rate_limited" if status == 429 else "service_offline"
                last_exc = BilibiliApiError(f"Bilibili HTTP {status}", error_code=code, http_status=status, extra_secrets=secrets)
                if status not in _TRANSIENT_HTTP or attempt >= self.max_retries:
                    raise last_exc from None
            except (TimeoutError, socket.timeout):
                last_exc = BilibiliApiError("Bilibili 请求超时", error_code="timeout", extra_secrets=secrets)
                if attempt >= self.max_retries:
                    raise last_exc from None
            except urllib.error.URLError as exc:
                is_to = isinstance(exc.reason, (TimeoutError, socket.timeout))
                last_exc = BilibiliApiError(
                    "Bilibili 请求超时" if is_to else f"Bilibili 网络不可达: {exc.reason}",
                    error_code="timeout" if is_to else "service_offline",
                    extra_secrets=secrets,
                )
                if attempt >= self.max_retries:
                    raise last_exc from None
            except OSError as exc:
                last_exc = BilibiliApiError(f"Bilibili 连接异常: {type(exc).__name__}", error_code="service_offline", extra_secrets=secrets)
                if attempt >= self.max_retries:
                    raise last_exc from None
            time.sleep(min(0.05 * (2**attempt), 0.25))

        assert last_exc is not None
        raise last_exc

    async def get_bytes(
        self, path_or_url: str, *, params: Mapping[str, Any] | None = None, cookie: str = "",
        base_override: str | None = None, allow_cdn: bool = False, referer: str = "https://www.bilibili.com/"
    ) -> tuple[bytes, list[str], str]:
        """Execute an async GET returning `(raw_bytes, set_cookie_headers, final_url)`."""
        base = (base_override or self.api_base).rstrip("/")
        url = path_or_url if path_or_url.startswith(("http://", "https://", "//")) else f"{base}/{path_or_url.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}{'&' if '?' in url else '?'}{query}" if query else url
        secrets = (cookie,) if cookie else ()
        try:
            return await asyncio.to_thread(
                self._request_sync, url, cookie=cookie, referer=referer, allow_cdn=allow_cdn, secrets=secrets
            )
        except SecurityValidationError as exc:
            raise BilibiliApiError(str(exc), error_code=exc.error_code, extra_secrets=secrets) from None

    async def get_json(
        self, path_or_url: str, *, params: Mapping[str, Any] | None = None, cookie: str = "",
        base_override: str | None = None, allow_cdn: bool = False, check_bili_code: bool = True
    ) -> Any:
        """Fetch JSON and fail closed on invalid JSON or non-zero Bilibili status `code`."""
        raw, _, _ = await self.get_bytes(
            path_or_url, params=params, cookie=cookie, base_override=base_override, allow_cdn=allow_cdn
        )
        secrets = (cookie,) if cookie else ()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BilibiliApiError(f"Bilibili 返回非法的 JSON 响应: {exc}", error_code="invalid_response", extra_secrets=secrets) from None
        if not isinstance(payload, Mapping):
            raise BilibiliApiError("Bilibili JSON 响应必须是对象", error_code="invalid_response", extra_secrets=secrets)
        if check_bili_code and isinstance(payload.get("code"), int) and payload["code"] != 0:
            raise _map_bili_code(int(payload["code"]), str(payload.get("message") or payload.get("msg") or ""), secrets)
        return payload

    async def _sign_wbi(self, params: Mapping[str, Any], cookie: str = "") -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        now = time.monotonic()
        if self._wbi_keys is None or (now - self._wbi_keys[2]) >= 300:
            try:
                nav = await self.get_json("/x/web-interface/nav", cookie=cookie, check_bili_code=False)
                wbi = (nav.get("data") or {}).get("wbi_img") if isinstance(nav.get("data"), Mapping) else None
                if isinstance(wbi, Mapping):
                    img_k = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
                    sub_k = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
                    if img_k and sub_k:
                        self._wbi_keys = (img_k, sub_k, now)
            except BilibiliApiError:
                pass
        if self._wbi_keys is None or len(self._wbi_keys[0] + self._wbi_keys[1]) < 64:
            return clean
        mixin = "".join((self._wbi_keys[0] + self._wbi_keys[1])[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
        signed = {**clean, "wts": int(time.time())}
        query = urllib.parse.urlencode({k: str(v).translate(_WBI_FILTER_TRANS) for k, v in sorted(signed.items())})
        signed["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return signed

    async def _get_wbi_with_fallback(
        self, wbi_path: str, legacy_path: str, params: Mapping[str, Any], *, cookie: str = ""
    ) -> Any:
        signed = await self._sign_wbi(params, cookie=cookie)
        try:
            return await self.get_json(wbi_path, params=signed, cookie=cookie)
        except BilibiliApiError as exc:
            if exc.http_status == 404:
                return await self.get_json(legacy_path, params=params, cookie=cookie)
            raise

    async def check_nav(self, cookie: str) -> dict[str, Any]:
        payload = await self.get_json("/x/web-interface/nav", cookie=cookie, check_bili_code=True)
        return dict(payload.get("data") or {})

    async def fetch_recommend(self, page: int = 1, *, cookie: str = "") -> list[dict[str, Any]]:
        payload = await self._get_wbi_with_fallback(
            "/x/web-interface/wbi/index/top/feed/rcmd", "/x/web-interface/index/top/feed/rcmd",
            {"ps": 20, "fresh_idx": page, "fresh_type": 4, "feed_version": "V8"}, cookie=cookie,
        )
        data = payload.get("data") or {}
        items = data.get("item") if isinstance(data.get("item"), list) else data.get("list")
        return [dict(it) for it in items if isinstance(it, Mapping)] if isinstance(items, list) else []

    async def fetch_dynamic(self, page: int = 1, *, cookie: str = "") -> list[dict[str, Any]]:
        if not cookie:
            raise BilibiliApiError("获取 Bilibili 动态流需要登录 Cookie", error_code="not_configured")
        payload = await self.get_json("/x/polymer/web-dynamic/v1/feed/all", params={"type": "video", "page": page}, cookie=cookie)
        items = (payload.get("data") or {}).get("items")
        return [dict(it) for it in items if isinstance(it, Mapping)] if isinstance(items, list) else []

    async def fetch_bangumi(self, page: int = 1, *, cookie: str = "") -> list[dict[str, Any]]:
        if not cookie:
            raise BilibiliApiError("获取 Bilibili 追番列表需要登录 Cookie", error_code="not_configured")
        vmid = cookie_value(cookie, "DedeUserID") or str((await self.check_nav(cookie)).get("mid") or "")
        if not vmid:
            raise BilibiliApiError("无法识别追番用户 MID，请重新登录", error_code="unauthorized")
        payload = await self.get_json("/x/space/bangumi/follow/list", params={"type": 1, "pn": page, "ps": 20, "vmid": vmid}, cookie=cookie)
        items = (payload.get("data") or {}).get("list")
        return [dict(it) for it in items if isinstance(it, Mapping)] if isinstance(items, list) else []

    async def fetch_live(self, page: int = 1, *, cookie: str = "") -> list[dict[str, Any]]:
        payload = await self.get_json(
            "/xlive/web-interface/v1/second/getListByArea",
            params={"platform": "web", "parent_area_id": 0, "area_id": 0, "sort_type": "online", "page": page, "page_size": 20},
            cookie=cookie, base_override=self.live_base,
        )
        items = (payload.get("data") or {}).get("list")
        return [dict(it) for it in items if isinstance(it, Mapping)] if isinstance(items, list) else []

    async def fetch_search(self, query: str, page: int = 1, *, cookie: str = "") -> list[dict[str, Any]]:
        if not query.strip():
            return []
        payload = await self._get_wbi_with_fallback(
            "/x/web-interface/wbi/search/type", "/x/web-interface/search/type",
            {"search_type": "video", "keyword": query, "page": page, "page_size": 20}, cookie=cookie,
        )
        items = (payload.get("data") or {}).get("result")
        return [dict(it) for it in items if isinstance(it, Mapping)] if isinstance(items, list) else []

    async def fetch_view(self, *, bvid: str = "", aid: int | None = None, cookie: str = "") -> dict[str, Any]:
        payload = await self.get_json("/x/web-interface/view", params={"bvid": bvid} if bvid else {"aid": aid}, cookie=cookie)
        return dict(payload.get("data") or {})

    async def fetch_pagelist(self, *, bvid: str = "", aid: int | None = None, cookie: str = "") -> list[dict[str, Any]]:
        payload = await self.get_json("/x/player/pagelist", params={"bvid": bvid} if bvid else {"aid": aid}, cookie=cookie)
        data = payload.get("data")
        return [dict(it) for it in data if isinstance(it, Mapping)] if isinstance(data, list) else []

    async def fetch_player_v2(self, *, bvid: str = "", aid: int | None = None, cid: int, cookie: str = "") -> dict[str, Any]:
        params = {"cid": cid, **({"bvid": bvid} if bvid else {"aid": aid})}
        payload = await self._get_wbi_with_fallback("/x/player/wbi/v2", "/x/player/v2", params, cookie=cookie)
        return dict(payload.get("data") or {})

    async def fetch_playurl(self, *, bvid: str = "", aid: int | None = None, cid: int, qn: int = 80, cookie: str = "") -> dict[str, Any]:
        params = {"cid": cid, "qn": qn, "fnval": 4048, "fnver": 0, "fourk": 1, **({"bvid": bvid} if bvid else {"aid": aid})}
        payload = await self._get_wbi_with_fallback("/x/player/wbi/playurl", "/x/player/playurl", params, cookie=cookie)
        return dict(payload.get("data") or {})

    async def fetch_danmaku_raw(self, cid: int, *, cookie: str = "") -> bytes:
        raw, _, _ = await self.get_bytes("/x/v1/dm/list.so", params={"oid": cid}, cookie=cookie)
        return raw

    async def fetch_subtitle_json(self, subtitle_url: str, *, cookie: str = "") -> dict[str, Any]:
        payload = await self.get_json(subtitle_url, cookie=cookie, allow_cdn=True, check_bili_code=False)
        return dict(payload) if isinstance(payload, Mapping) else {}

    async def qr_generate(self) -> dict[str, Any]:
        payload = await self.get_json("/x/passport-login/web/qrcode/generate", base_override=self.passport_base)
        if not isinstance(payload.get("data"), Mapping):
            raise BilibiliApiError("二维码生成接口返回无效数据结构", error_code="invalid_response")
        return dict(payload["data"])

    async def qr_poll(self, qrcode_key: str) -> tuple[dict[str, Any], dict[str, str]]:
        raw, set_cookies, _ = await self.get_bytes("/x/passport-login/web/qrcode/poll", params={"qrcode_key": qrcode_key}, base_override=self.passport_base)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BilibiliApiError(f"扫码轮询响应 JSON 无效: {exc}", error_code="invalid_response") from None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Mapping):
            raise BilibiliApiError("扫码轮询响应格式无效", error_code="invalid_response")
        if isinstance(payload.get("code"), int) and payload["code"] != 0:
            raise _map_bili_code(int(payload["code"]), str(payload.get("message") or ""), ())
        parsed_cookies: dict[str, str] = {}
        for header_val in set_cookies:
            jar = http.cookies.SimpleCookie()
            jar.load(header_val)
            for name, morsel in jar.items():
                if morsel.value:
                    parsed_cookies[name] = morsel.value
        return dict(payload["data"]), parsed_cookies
