"""Hardened HTTP fetch, redirect-safe opener, and streaming temp-file client for plugin-annas-archive."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext

from annas_auth import AuthMaterial
from annas_security import (
    BookSearchSecurityError,
    Origin,
    is_private_or_internal_host,
    parse_origin,
    redact_text,
    scrub_url_secrets,
    validate_redirect_target,
    validate_url,
)

_CHUNK_SIZE = 65536
_MAX_REDIRECTS = 5
_AUTH_STRIP_HEADERS = ("cookie", "authorization", "x-api-key")


@dataclass(frozen=True, slots=True)
class HttpResponseData:
    """Bounded HTTP response payload."""

    status_code: int
    url: str
    headers: dict[str, str]
    body: bytes
    via: str = "http"

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


class HttpFetchError(RuntimeError):
    """Normalized HTTP/network failure without secret leakage."""

    def __init__(self, error_code: str, message: str, *, status_code: int = 0) -> None:
        self.error_code = error_code
        self.status_code = status_code
        super().__init__(message)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validates redirects against SSRF rules and strips credentials on cross-origin hops."""

    def __init__(self, configured_origins: frozenset[Origin], resolve_dns: bool) -> None:
        super().__init__()
        self._configured_origins = configured_origins
        self._resolve_dns = resolve_dns
        self._hops = 0

    def http_error_302(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
    ) -> Any:
        location = headers.get("location") or headers.get("uri") or ""
        try:
            validate_redirect_target(
                req.full_url,
                location,
                configured_origins=self._configured_origins,
                resolve_dns=self._resolve_dns,
            )
        except BookSearchSecurityError as exc:
            raise urllib.error.URLError(exc) from exc
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._hops += 1
        if self._hops > _MAX_REDIRECTS:
            raise urllib.error.URLError(BookSearchSecurityError("unsafe_url", "HTTP 重定向次数过多"))
        try:
            safe_url, cross_origin = validate_redirect_target(
                req.full_url,
                newurl,
                configured_origins=self._configured_origins,
                resolve_dns=self._resolve_dns,
            )
        except BookSearchSecurityError as exc:
            raise urllib.error.URLError(exc) from exc

        new_req = super().redirect_request(req, fp, code, msg, headers, safe_url)
        if new_req is None:
            return None
        if cross_origin:
            for header_dict in (new_req.headers, new_req.unredirected_hdrs):
                for key in list(header_dict.keys()):
                    if key.lower() in _AUTH_STRIP_HEADERS:
                        header_dict.pop(key, None)
        return new_req


def _build_opener(url: str, configured_origins: frozenset[Origin], *, resolve_dns: bool) -> urllib.request.OpenerDirector:
    origin = parse_origin(url)
    handlers: list[urllib.request.BaseHandler] = [_SafeRedirectHandler(configured_origins, resolve_dns)]
    if origin in configured_origins and is_private_or_internal_host(origin.host):
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _map_http_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "auth_required"
    if status_code in {404, 410}:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "service_offline"
    return "http_error"


def _parse_content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        val = int(str(raw).strip())
        return val if val >= 0 else None
    except ValueError:
        return None


def _map_transport_exc(exc: BaseException, secrets: tuple[str, ...]) -> HttpFetchError:
    if isinstance(exc, HttpFetchError):
        return exc
    if isinstance(exc, BookSearchSecurityError):
        return HttpFetchError(exc.error_code, redact_text(str(exc), secrets))
    if isinstance(exc, urllib.error.HTTPError):
        return HttpFetchError(_map_http_status(int(exc.code)), f"站点返回 HTTP {exc.code}", status_code=int(exc.code))
    if isinstance(exc, TimeoutError):
        return HttpFetchError("timeout", "请求超时")
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BookSearchSecurityError):
            return HttpFetchError(reason.error_code, redact_text(str(reason), secrets))
        if isinstance(reason, TimeoutError):
            return HttpFetchError("timeout", "请求超时")
        return HttpFetchError("service_offline", redact_text(f"站点连接失败: {reason}", secrets))
    return HttpFetchError("service_offline", redact_text(f"网络请求失败: {type(exc).__name__}", secrets))


def _sync_fetch_bytes(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout_sec: float,
    max_bytes: int,
    configured_origins: frozenset[Origin],
    secrets: tuple[str, ...],
) -> HttpResponseData:
    safe_url = validate_url(url, configured_origins=configured_origins, resolve_dns=True)
    opener = _build_opener(safe_url, configured_origins, resolve_dns=True)
    req = urllib.request.Request(safe_url, headers=dict(headers), method="GET")
    deadline = time.monotonic() + max(0.05, timeout_sec)
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            declared_len = _parse_content_length(resp_headers)
            if declared_len is not None and declared_len > max_bytes:
                raise HttpFetchError("file_too_large", f"响应体积 ({declared_len} B) 超出上限 ({max_bytes} B)", status_code=resp.status)
            buf = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("HTTP 读取超时")
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise HttpFetchError("file_too_large", f"响应体积超出上限 ({max_bytes} B)", status_code=resp.status)
            final_url = scrub_url_secrets(str(resp.geturl() or safe_url))
            return HttpResponseData(status_code=int(resp.status), url=final_url, headers=resp_headers, body=bytes(buf))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise _map_transport_exc(exc, secrets) from exc


def _sync_stream_to_temp(
    url: str,
    target_path: Path,
    *,
    headers: Mapping[str, str],
    timeout_sec: float,
    max_bytes: int,
    configured_origins: frozenset[Origin],
    secrets: tuple[str, ...],
) -> tuple[Path, str, int, str, str]:
    safe_url = validate_url(url, configured_origins=configured_origins, resolve_dns=True)
    opener = _build_opener(safe_url, configured_origins, resolve_dns=True)
    req = urllib.request.Request(safe_url, headers=dict(headers), method="GET")
    deadline = time.monotonic() + max(0.05, timeout_sec)
    tmp_file = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=target_path.parent,
        prefix=f".{target_path.name}.",
        suffix=".omnidesk.tmp",
        delete=False,
    )
    tmp_path = Path(tmp_file.name)
    hasher = hashlib.sha256()
    total = 0
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            declared_len = _parse_content_length(resp_headers)
            if declared_len is not None and declared_len > max_bytes:
                raise HttpFetchError("file_too_large", f"文件大小 ({declared_len} B) 超过限制 ({max_bytes} B)", status_code=resp.status)
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("下载流读取超时")
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HttpFetchError("file_too_large", f"下载流体积超过限制 ({max_bytes} B)", status_code=resp.status)
                tmp_file.write(chunk)
                hasher.update(chunk)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
            tmp_file.close()
            final_url = scrub_url_secrets(str(resp.geturl() or safe_url))
            return tmp_path, hasher.hexdigest(), total, final_url, resp_headers.get("content-type", "")
    except BaseException as exc:
        try:
            if not tmp_file.closed:
                tmp_file.close()
        finally:
            tmp_path.unlink(missing_ok=True)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise _map_transport_exc(exc, secrets) from exc


async def fetch_with_chrome_fallback(
    ctx: PluginContext | None,
    url: str,
    *,
    auth: AuthMaterial,
    timeout_sec: float,
    max_bytes: int,
    configured_origins: frozenset[Origin],
    accept: str = "text/html,application/json;q=0.9,*/*;q=0.8",
    prefer_chrome_fetch: bool = False,
) -> HttpResponseData:
    """Fetch URL with SSRF safety; optionally route or fallback via `web.fetch.v1`."""
    safe_url = validate_url(url, configured_origins=configured_origins, resolve_dns=False)
    has_web_fetch = ctx is not None and ctx.capabilities.has("web.fetch.v1")

    async def _via_chrome() -> HttpResponseData:
        assert ctx is not None
        res = await ctx.capabilities.call(
            "web.fetch.v1",
            {
                "url": safe_url,
                "method": "GET",
                "headers": {"Accept": accept},
                "body": "",
                "credentials": "include",
                "timeout_ms": int(max(1.0, timeout_sec) * 1000),
            },
        )
        if not res.ok:
            code = str(res.data.get("error_code") or "service_offline")
            raise HttpFetchError(code, redact_text(res.error or "web.fetch.v1 调用失败", auth.extra_secrets()))
        data = res.data
        status = int(data.get("status") or data.get("status_code") or 200)
        if status >= 400:
            raise HttpFetchError(_map_http_status(status), f"站点返回 HTTP {status}", status_code=status)
        if isinstance(data.get("content_base64"), str):
            raw_body = base64.b64decode(data["content_base64"])
            hdrs = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
        elif "json" in data and data["json"] is not None:
            raw_body = json.dumps(data["json"], ensure_ascii=False).encode("utf-8")
            hdrs = {"content-type": "application/json; charset=utf-8"}
        else:
            text_val = data.get("body") if data.get("body") is not None else data.get("text", data.get("html", ""))
            raw_body = text_val if isinstance(text_val, bytes) else str(text_val).encode("utf-8")
            hdrs = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
        if len(raw_body) > max_bytes:
            raise HttpFetchError("file_too_large", f"响应体积 ({len(raw_body)} B) 超出上限 ({max_bytes} B)", status_code=status)
        return HttpResponseData(status_code=status, url=scrub_url_secrets(safe_url), headers=hdrs, body=raw_body, via="web.fetch.v1")

    if prefer_chrome_fetch and has_web_fetch:
        return await _via_chrome()

    try:
        return await asyncio.to_thread(
            _sync_fetch_bytes,
            safe_url,
            headers=auth.build_headers(accept=accept),
            timeout_sec=timeout_sec,
            max_bytes=max_bytes,
            configured_origins=configured_origins,
            secrets=auth.extra_secrets(),
        )
    except HttpFetchError as exc:
        if exc.error_code == "auth_required" and has_web_fetch:
            return await _via_chrome()
        raise


async def download_to_target(
    ctx: PluginContext | None,
    url: str,
    target_path: Path,
    *,
    auth: AuthMaterial,
    timeout_sec: float,
    max_bytes: int,
    configured_origins: frozenset[Origin],
) -> tuple[Path, str, int, str, str]:
    """Stream download URL into a temporary file under `target_path.parent` (no rename yet)."""
    safe_url = validate_url(url, configured_origins=configured_origins, resolve_dns=False)
    try:
        return await asyncio.to_thread(
            _sync_stream_to_temp,
            safe_url,
            target_path,
            headers=auth.build_headers(accept="*/*"),
            timeout_sec=timeout_sec,
            max_bytes=max_bytes,
            configured_origins=configured_origins,
            secrets=auth.extra_secrets(),
        )
    except HttpFetchError as exc:
        if exc.error_code == "auth_required" and ctx is not None and ctx.capabilities.has("web.fetch.v1"):
            resp = await fetch_with_chrome_fallback(
                ctx,
                safe_url,
                auth=auth,
                timeout_sec=timeout_sec,
                max_bytes=max_bytes,
                configured_origins=configured_origins,
                accept="*/*",
                prefer_chrome_fetch=True,
            )
            tmp_file = tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target_path.parent,
                prefix=f".{target_path.name}.",
                suffix=".omnidesk.tmp",
                delete=False,
            )
            tmp_path = Path(tmp_file.name)
            try:
                tmp_file.write(resp.body)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
                tmp_file.close()
            except BaseException:
                if not tmp_file.closed:
                    tmp_file.close()
                tmp_path.unlink(missing_ok=True)
                raise
            digest = hashlib.sha256(resp.body).hexdigest()
            return tmp_path, digest, len(resp.body), resp.url, resp.content_type
        raise
