"""Hardened LAN HTTP client enforcing trust_env=False, follow_redirects=False, and bounded reads."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

if __package__:
    from .safety import redact_secrets
else:
    from safety import redact_secrets

DEFAULT_HTTP_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_HTTP_TIMEOUT_SEC = 10.0


class LanHttpError(Exception):
    """Structured LAN HTTP failure mapped to OmniDesk error_code."""

    def __init__(
        self,
        message: str,
        error_code: str,
        *,
        http_status: int | None = None,
        redirect_blocked: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status
        self.redirect_blocked = redirect_blocked


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block all HTTP 3xx redirects (`follow_redirects=False`) so tokens never leak cross-origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"Redirect blocked ({code})",
            headers,
            fp,
        )


def validate_lan_url(raw_url: str | None, *, label: str = "LAN 服务") -> str:
    """Validate an optional http/https LAN URL and strip trailing slashes."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise LanHttpError(f"{label} URL 尚未配置", "not_configured")
    cleaned = raw_url.strip()
    if any(ch in cleaned for ch in ("\x00", "\r", "\n", " ")):
        raise LanHttpError(f"{label} URL 包含非法字符", "invalid_argument")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise LanHttpError(f"{label} URL 必须为合法的 http/https 地址", "invalid_argument")
    if parsed.username is not None or parsed.password is not None:
        raise LanHttpError(f"{label} URL 禁止内嵌明文凭据，请使用 SecretVault", "invalid_argument")
    return cleaned.rstrip("/")


@dataclass(frozen=True, slots=True)
class LanResponse:
    """Bounded HTTP response returned by LanHttpClient."""

    status: int
    body_bytes: bytes
    headers: dict[str, str]


class LanHttpClient:
    """Async wrapper over urllib enforcing trust_env=False and follow_redirects=False."""

    def __init__(self, *, trust_env: bool = False, follow_redirects: bool = False) -> None:
        if trust_env:
            raise ValueError("LAN HTTP client strictly enforces trust_env=False")
        if follow_redirects:
            raise ValueError("LAN HTTP client strictly enforces follow_redirects=False")
        self.trust_env = False
        self.follow_redirects = False

    def _build_opener(self) -> urllib.request.OpenerDirector:
        # Empty ProxyHandler({}) bypasses HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (`trust_env=False`).
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, Any] | None = None,
        json_body: Any = None,
        timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
        max_bytes: int = DEFAULT_HTTP_MAX_BYTES,
        secrets: Iterable[str] = (),
        service_label: str = "局域网服务",
    ) -> Any:
        """Execute a bounded JSON HTTP request with strict timeout/401/offline error mapping."""
        if self.trust_env or self.follow_redirects:
            raise ValueError("trust_env and follow_redirects must remain False")

        target_url = url
        if query:
            pairs = [(str(k), str(v)) for k, v in query.items() if v is not None and str(v) != ""]
            if pairs:
                sep = "&" if "?" in target_url else "?"
                target_url = f"{target_url}{sep}{urllib.parse.urlencode(pairs)}"

        req_headers = {"Accept": "application/json", "User-Agent": "OmniDesk-fnOS-NAS/0.2"}
        if headers:
            for k, v in headers.items():
                if v:
                    req_headers[str(k)] = str(v)

        data_bytes: bytes | None = None
        if json_body is not None:
            data_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json; charset=utf-8")

        secret_tuple = tuple(s for s in secrets if isinstance(s, str) and s)
        timeout = max(0.05, float(timeout_sec))

        def _perform() -> LanResponse:
            opener = self._build_opener()
            req = urllib.request.Request(target_url, data=data_bytes, headers=req_headers, method=method.upper())
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise LanHttpError(f"{service_label}响应超过 {max_bytes} 字节限制", "invalid_argument")
                return LanResponse(
                    status=int(getattr(resp, "status", 200)),
                    body_bytes=raw,
                    headers={str(k).lower(): str(v) for k, v in resp.headers.items()},
                )

        try:
            res = await asyncio.to_thread(_perform)
        except LanHttpError:
            raise
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc.code, exc.reason, service_label, secret_tuple) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                raise LanHttpError(f"{service_label}请求超时", "timeout") from None
            safe_reason = redact_secrets(str(reason or exc), secret_tuple)
            raise LanHttpError(f"{service_label}不可达: {safe_reason}", "service_offline") from None
        except (TimeoutError, socket.timeout):
            raise LanHttpError(f"{service_label}请求超时", "timeout") from None
        except (http.client.HTTPException, OSError) as exc:
            if "timed out" in str(exc).lower():
                raise LanHttpError(f"{service_label}请求超时", "timeout") from None
            safe_msg = redact_secrets(str(exc), secret_tuple)
            raise LanHttpError(f"{service_label}连接失败: {safe_msg}", "service_offline") from None

        if not res.body_bytes:
            return {}
        try:
            return json.loads(res.body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LanHttpError(f"{service_label}返回了无效的 JSON 响应", "service_offline") from None


def _map_http_error(
    code: int,
    reason: Any,
    service_label: str,
    secrets: tuple[str, ...],
) -> LanHttpError:
    if code in {301, 302, 303, 307, 308}:
        return LanHttpError(
            f"{service_label}返回重定向 (HTTP {code})，已禁止跟随以防凭据泄露",
            "service_offline",
            http_status=code,
            redirect_blocked=True,
        )
    if code in {401, 403}:
        return LanHttpError(
            f"{service_label}鉴权失败 (HTTP {code})，请检查 SecretVault 凭据",
            "permission_denied",
            http_status=code,
        )
    if code == 404:
        return LanHttpError(
            f"{service_label}接口或资源不存在 (HTTP 404)",
            "not_found",
            http_status=404,
        )
    if code == 429:
        return LanHttpError(
            f"{service_label}触发限流 (HTTP 429)",
            "rate_limited",
            http_status=429,
        )
    if 500 <= code <= 599:
        return LanHttpError(
            f"{service_label}服务端错误 (HTTP {code})",
            "service_offline",
            http_status=code,
        )
    safe_reason = redact_secrets(str(reason or ""), secrets)
    return LanHttpError(
        f"{service_label}请求失败 (HTTP {code}): {safe_reason}",
        "service_offline",
        http_status=code,
    )
