"""Safe HTTPS HTTP client and atomic JSON persistence helpers for plugin-youtube."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

if __package__:
    from .yt_security import LOOPBACK_HOSTS, redact_sensitive, url_origin, validate_https_url
else:
    from yt_security import LOOPBACK_HOSTS, redact_sensitive, url_origin, validate_https_url

MAX_HTTP_BYTES = 8 * 1024 * 1024


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject insecure redirects and strip sensitive headers across origins."""

    def __init__(self, *, allow_loopback_http: bool = False, allowed_loopback_origins: Iterable[str] = ()) -> None:
        self._allow_loopback_http = allow_loopback_http
        self._allowed_origins = set(allowed_loopback_origins)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        safe_target = validate_https_url(
            newurl,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=self._allowed_origins,
        )
        redirected = super().redirect_request(req, fp, code, msg, headers, safe_target)
        if redirected is None:
            return None
        if url_origin(req.full_url) != url_origin(safe_target):
            for hdr in ("Authorization", "Cookie", "X-Goog-Api-Key", "X-Api-Key"):
                redirected.remove_header(hdr)
        return redirected


def map_http_error(status: int, body_text: str, extra_secrets: Iterable[str]) -> tuple[str, str, dict[str, Any]]:
    """Map HTTP status codes into canonical OmniDesk error codes without leaking secrets."""
    safe_body = redact_sensitive(body_text, extra_secrets)
    low = safe_body.lower()
    if status == 401:
        return "permission_denied", "YouTube API 鉴权失败 (HTTP 401 Unauthorized)", {"http_status": 401, "reason": "unauthorized"}
    if status == 403:
        if "quotaexceeded" in low or "dailylimitexceeded" in low:
            return "quota_exceeded", "YouTube Data API 配额已耗尽 (HTTP 403)", {"http_status": 403, "reason": "quota_exceeded"}
        return "permission_denied", "YouTube 请求被拒绝 (HTTP 403 Forbidden)", {"http_status": 403, "reason": "forbidden"}
    if status == 404:
        return "not_found", "请求的 YouTube 资源不存在 (HTTP 404)", {"http_status": 404, "reason": "not_found"}
    if status == 429:
        return "rate_limited", "YouTube 请求触发限流 (HTTP 429)", {"http_status": 429, "reason": "rate_limited"}
    return "service_offline", f"YouTube 上游 HTTP 错误 ({status})", {"http_status": status, "reason": "http_error"}


async def http_get_bytes(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_sec: float = 15.0,
    extra_secrets: Iterable[str] = (),
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> tuple[bytes | None, dict[str, Any] | None]:
    """Execute a safe HTTPS-only GET request (loopback HTTP allowed only when opted in)."""
    try:
        safe_url = validate_https_url(
            url, allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins
        )
    except ValueError as exc:
        return None, {"error_code": "invalid_argument", "error": redact_sensitive(str(exc), extra_secrets)}

    parsed = urllib.parse.urlsplit(safe_url)
    is_loopback = (parsed.hostname or "").lower() in LOOPBACK_HOSTS

    def _do_request() -> bytes:
        handlers: list[urllib.request.BaseHandler] = [
            _SafeRedirectHandler(allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins)
        ]
        if is_loopback:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(safe_url, method="GET")
        req.add_header("Accept", "application/json, text/vtt, text/plain;q=0.9, */*;q=0.1")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with opener.open(req, timeout=max(0.1, float(timeout_sec))) as resp:
            data = resp.read(MAX_HTTP_BYTES + 1)
            if len(data) > MAX_HTTP_BYTES:
                raise ValueError("Response payload exceeds 8 MiB safety limit")
            return data

    try:
        return await asyncio.to_thread(_do_request), None
    except urllib.error.HTTPError as exc:
        body_bytes = b""
        try:
            body_bytes = exc.read(65536)
        except Exception:
            pass
        code, msg, extra = map_http_error(exc.code, body_bytes.decode("utf-8", errors="replace"), extra_secrets)
        return None, {"error_code": code, "error": msg, **extra}
    except (TimeoutError, socket.timeout):
        return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc.reason).lower():
            return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}
        return None, {"error_code": "service_offline", "error": f"YouTube 网络请求失败: {redact_sensitive(str(exc.reason), extra_secrets)}"}
    except ValueError as exc:
        return None, {"error_code": "invalid_argument", "error": redact_sensitive(str(exc), extra_secrets)}
    except OSError as exc:
        return None, {"error_code": "service_offline", "error": f"YouTube 连接失败: {redact_sensitive(str(exc), extra_secrets)}"}


async def http_get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_sec: float = 15.0,
    extra_secrets: Iterable[str] = (),
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch and parse a JSON object from a validated HTTPS/loopback endpoint."""
    raw, err = await http_get_bytes(
        url,
        headers=headers,
        timeout_sec=timeout_sec,
        extra_secrets=extra_secrets,
        allow_loopback_http=allow_loopback_http,
        allowed_loopback_origins=allowed_loopback_origins,
    )
    if err is not None or raw is None:
        return None, err
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None, {"error_code": "service_offline", "error": "YouTube API 返回了无效 JSON"}
    if not isinstance(parsed, Mapping):
        return None, {"error_code": "service_offline", "error": "YouTube API 响应格式无效"}
    return dict(parsed), None


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write JSON with 0600 permissions inside cache directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    tmp_path: Path | None = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        tmp_path = Path(tmp.name)
        os.fchmod(tmp.fileno(), 0o600)
        tmp.write(serialized)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if not tmp.closed:
                tmp.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise
