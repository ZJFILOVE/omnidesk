"""Safe HTTPS HTTP client and atomic JSON persistence helpers for plugin-youtube."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import ssl
import tempfile
import time
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
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ignore_eof = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    if ignore_eof:
        ctx.options |= ignore_eof
    return ctx


def _is_transient_tls_or_eof(exc: BaseException) -> bool:
    if isinstance(exc, (ssl.SSLEOFError, ConnectionResetError, BrokenPipeError)):
        return True
    low = str(exc).lower()
    return any(
        token in low
        for token in (
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "connection reset by peer",
            "remote end closed connection",
        )
    )


def _validate_proxy(proxy: str) -> str:
    clean = str(proxy or "").strip()
    if not clean:
        return ""
    if any(ch in clean for ch in "\r\n\t "):
        raise ValueError("YouTube 代理地址无效")
    parsed = urllib.parse.urlsplit(clean)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("YouTube 代理地址无效")
    return clean


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


async def _http_get_bytes_via_curl(
    safe_url: str,
    *,
    proxy: str,
    headers: Mapping[str, str] | None,
    timeout_sec: float,
    extra_secrets: Iterable[str],
    allow_loopback_http: bool,
) -> tuple[bytes | None, dict[str, Any] | None]:
    curl_bin = shutil.which("curl")
    if not curl_bin:
        return None, {"error_code": "service_offline", "error": "当前环境不支持 socks5 代理（缺少 curl），请改用 http/https 代理"}
    protos = "http,https" if allow_loopback_http else "https"
    cmd = [
        curl_bin,
        "-sS",
        "-L",
        "--max-redirs",
        "5",
        "--proto",
        f"={protos}",
        "--proto-redir",
        f"={protos}",
        "--max-filesize",
        str(MAX_HTTP_BYTES),
        "--connect-timeout",
        f"{max(0.1, float(timeout_sec)):.2f}",
        "--max-time",
        f"{max(0.1, float(timeout_sec)):.2f}",
        "-H",
        "Accept: application/json, text/vtt, text/plain;q=0.9, */*;q=0.1",
        "-w",
        "\n%{http_code}",
    ]
    if proxy:
        cmd.extend(["--proxy", proxy])
    has_ua = any(k.lower() == "user-agent" for k in (headers or {}))
    if not has_ua:
        cmd.extend(["-A", _DEFAULT_USER_AGENT])
    for k, v in (headers or {}).items():
        if any(ch in k or ch in v for ch in "\r\n\0"):
            return None, {"error_code": "invalid_argument", "error": "请求头包含非法换行字符"}
        cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(safe_url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(0.2, float(timeout_sec)) + 1.0)
    except (TimeoutError, asyncio.TimeoutError):
        return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}
    except OSError as exc:
        return None, {"error_code": "service_offline", "error": f"YouTube 连接失败: {redact_sensitive(str(exc), extra_secrets)}"}

    if proc.returncode == 28:
        return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}
    if proc.returncode != 0:
        err_msg = redact_sensitive(stderr.decode("utf-8", errors="replace").strip(), extra_secrets)
        if "timed out" in err_msg.lower():
            return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}
        return None, {"error_code": "service_offline", "error": f"YouTube 网络请求失败: {err_msg or f'curl exit {proc.returncode}'}"}

    body, _, status_raw = stdout.rpartition(b"\n")
    try:
        status = int(status_raw.decode("ascii", errors="ignore").strip() or "0")
    except ValueError:
        status = 0
    if status >= 400:
        code, msg, extra = map_http_error(status, body[:65536].decode("utf-8", errors="replace"), extra_secrets)
        return None, {"error_code": code, "error": msg, **extra}
    if len(body) > MAX_HTTP_BYTES:
        return None, {"error_code": "invalid_argument", "error": "Response payload exceeds 8 MiB safety limit"}
    return body, None


async def http_get_bytes(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_sec: float = 15.0,
    extra_secrets: Iterable[str] = (),
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
    proxy: str = "",
) -> tuple[bytes | None, dict[str, Any] | None]:
    """Execute a safe HTTPS-only GET request (loopback HTTP allowed only when opted in)."""
    try:
        clean_proxy = _validate_proxy(proxy)
    except ValueError as exc:
        return None, {"error_code": "invalid_argument", "error": redact_sensitive(str(exc), (*extra_secrets, proxy))}

    merged_secrets = (*extra_secrets, clean_proxy) if clean_proxy else tuple(extra_secrets)

    try:
        safe_url = validate_https_url(
            url, allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins
        )
    except ValueError as exc:
        return None, {"error_code": "invalid_argument", "error": redact_sensitive(str(exc), merged_secrets)}

    parsed = urllib.parse.urlsplit(safe_url)
    is_loopback = (parsed.hostname or "").lower() in LOOPBACK_HOSTS
    effective_proxy = "" if is_loopback else clean_proxy

    if effective_proxy and urllib.parse.urlsplit(effective_proxy).scheme.lower() in {"socks5", "socks5h"}:
        return await _http_get_bytes_via_curl(
            safe_url,
            proxy=effective_proxy,
            headers=headers,
            timeout_sec=timeout_sec,
            extra_secrets=merged_secrets,
            allow_loopback_http=allow_loopback_http,
        )

    def _do_request(attempt_timeout: float) -> bytes:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=_build_ssl_context()),
            _SafeRedirectHandler(allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins),
        ]
        if is_loopback:
            handlers.append(urllib.request.ProxyHandler({}))
        elif effective_proxy:
            handlers.append(urllib.request.ProxyHandler({"http": effective_proxy, "https": effective_proxy}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(safe_url, method="GET")
        req.add_header("Accept", "application/json, text/vtt, text/plain;q=0.9, */*;q=0.1")
        if not any(k.lower() == "user-agent" for k in (headers or {})):
            req.add_header("User-Agent", _DEFAULT_USER_AGENT)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with opener.open(req, timeout=max(0.1, float(attempt_timeout))) as resp:
            data = resp.read(MAX_HTTP_BYTES + 1)
            if len(data) > MAX_HTTP_BYTES:
                raise ValueError("Response payload exceeds 8 MiB safety limit")
            return data

    total_budget = max(0.1, float(timeout_sec))
    attempt_timeouts = (
        [max(2.0, total_budget * 0.55)] if total_budget >= 4.0 else [total_budget]
    )
    started_at = time.monotonic()

    for idx in range(2):
        cur_timeout = attempt_timeouts[0] if idx == 0 else max(1.0, total_budget - (time.monotonic() - started_at))
        try:
            return await asyncio.to_thread(_do_request, cur_timeout), None
        except urllib.error.HTTPError as exc:
            body_bytes = b""
            try:
                body_bytes = exc.read(65536)
            except Exception:
                pass
            code, msg, extra = map_http_error(exc.code, body_bytes.decode("utf-8", errors="replace"), merged_secrets)
            return None, {"error_code": code, "error": msg, **extra}
        except (TimeoutError, socket.timeout):
            if idx == 0 and total_budget >= 4.0 and (total_budget - (time.monotonic() - started_at)) >= 1.5:
                continue
            return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc.reason).lower():
                if idx == 0 and total_budget >= 4.0 and (total_budget - (time.monotonic() - started_at)) >= 1.5:
                    continue
                return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}
            is_eof = (isinstance(exc.reason, BaseException) and _is_transient_tls_or_eof(exc.reason)) or _is_transient_tls_or_eof(exc)
            if is_eof:
                if idx == 0 and total_budget >= 4.0 and (total_budget - (time.monotonic() - started_at)) >= 1.5:
                    continue
                if not is_loopback and shutil.which("curl"):
                    rem = max(2.0, total_budget - (time.monotonic() - started_at))
                    return await _http_get_bytes_via_curl(
                        safe_url,
                        proxy=effective_proxy,
                        headers=headers,
                        timeout_sec=rem,
                        extra_secrets=merged_secrets,
                        allow_loopback_http=allow_loopback_http,
                    )
            return None, {"error_code": "service_offline", "error": f"YouTube 网络请求失败: {redact_sensitive(str(exc.reason), merged_secrets)}"}
        except ValueError as exc:
            return None, {"error_code": "invalid_argument", "error": redact_sensitive(str(exc), merged_secrets)}
        except OSError as exc:
            if _is_transient_tls_or_eof(exc):
                if idx == 0 and total_budget >= 4.0 and (total_budget - (time.monotonic() - started_at)) >= 1.5:
                    continue
                if not is_loopback and shutil.which("curl"):
                    rem = max(2.0, total_budget - (time.monotonic() - started_at))
                    return await _http_get_bytes_via_curl(
                        safe_url,
                        proxy=effective_proxy,
                        headers=headers,
                        timeout_sec=rem,
                        extra_secrets=merged_secrets,
                        allow_loopback_http=allow_loopback_http,
                    )
            return None, {"error_code": "service_offline", "error": f"YouTube 连接失败: {redact_sensitive(str(exc), merged_secrets)}"}
    return None, {"error_code": "timeout", "error": "YouTube HTTP 请求超时", "reason": "timeout"}


async def http_get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_sec: float = 15.0,
    extra_secrets: Iterable[str] = (),
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
    proxy: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch and parse a JSON object from a validated HTTPS/loopback endpoint."""
    raw, err = await http_get_bytes(
        url,
        headers=headers,
        timeout_sec=timeout_sec,
        extra_secrets=extra_secrets,
        allow_loopback_http=allow_loopback_http,
        allowed_loopback_origins=allowed_loopback_origins,
        proxy=proxy,
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
