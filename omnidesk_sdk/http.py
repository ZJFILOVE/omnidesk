"""Hardened HTTPS-first HTTP client with SSRF, DNS-rebinding, redirect, and secret protections."""

from __future__ import annotations

import asyncio
import base64
import errno
import http.client
import ipaddress
import json as _json
import logging
import os
import re
import socket
import ssl
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .stores import SecretRedactor, redact_secrets

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_TIMEOUT_SEC = 15.0
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_CHUNK_SIZE = 65536
_MAX_URL_LENGTH = 8192
_MAX_ERROR_SNIPPET_BYTES = 2048
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CROSS_ORIGIN_STRIP_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "cookie2",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
        "x-goog-api-key",
    }
)
_SENSITIVE_REQUEST_HEADERS = _CROSS_ORIGIN_STRIP_HEADERS | frozenset({"x-csrf-token"})
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "key",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "secret",
        "cookie",
        "password",
        "passwd",
        "sig",
        "signature",
    }
)
_LOOPBACK_HOSTS = frozenset({"localhost", "ip6-localhost", "ip6-loopback", "127.0.0.1", "::1"})
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".arpa")
_INTERNAL_EXACT_HOSTS = frozenset({"localhost", "ip6-localhost", "ip6-loopback", "metadata.google.internal"})
_METHOD_RE = re.compile(r"^[A-Za-z]{1,16}\Z")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"token|secret|cookie|password|passwd|key|sig|signature)=)([^&\s#]+)"
)
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|cookie2|set-cookie|set-cookie2|"
    r"x-api-key|x-auth-token|x-access-token|x-goog-api-key)\s*([:=])\s*([^\r\n]+)"
)

DnsResolver = Callable[[str, int, int, int], list[tuple[Any, ...]]]


class HttpErrorCode(StrEnum):
    """Canonical error classifications produced by `SafeHttpClient`."""

    TIMEOUT = "timeout"
    TOO_LARGE = "too_large"
    BLOCKED_REDIRECT = "blocked_redirect"
    SSRF = "ssrf"
    UPSTREAM_STATUS = "upstream_status"
    INVALID_ARGUMENT = "invalid_argument"
    SERVICE_OFFLINE = "service_offline"


def scrub_url_secrets(url: str) -> str:
    """Mask userinfo and sensitive query parameters from a URL for safe diagnostics."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "[REDACTED_URL]"
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port_part = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"[REDACTED]@{host}{port_part}"
    if not parsed.query:
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    cleaned = [
        (k, "[REDACTED]" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
        for k, v in pairs
    ]
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, urllib.parse.urlencode(cleaned), ""))


def redact_http_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub explicit secrets, auth headers, and sensitive query parameters from text."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _HEADER_SECRET_RE.sub(r"\1\2 [REDACTED]", scrubbed)
    return redact_secrets(scrubbed, extra_secrets)


class SafeHttpError(RuntimeError):
    """Fail-closed, secret-redacted HTTP client error with canonical classification."""

    default_code: str = HttpErrorCode.SERVICE_OFFLINE

    def __init__(
        self,
        error_code: str | None,
        message: str,
        *,
        status_code: int | None = None,
        url: str = "",
        redirect_blocked: bool = False,
        secrets: Iterable[str] = (),
    ) -> None:
        secret_tuple = tuple(s for s in secrets if isinstance(s, str) and s)
        self.error_code: str = str(error_code or self.default_code)
        self.status_code: int | None = status_code
        self.url: str = scrub_url_secrets(url) if url else ""
        self.redirect_blocked: bool = redirect_blocked
        self.message: str = redact_http_text(message, secret_tuple)
        super().__init__(self.message)

    @property
    def code(self) -> str:
        """Alias for `error_code` matching plugin error conventions."""
        return self.error_code

    @property
    def category(self) -> str:
        """Alias for `error_code` classification."""
        return self.error_code

    @property
    def http_status(self) -> int | None:
        """Alias for `status_code`."""
        return self.status_code

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(error_code={self.error_code!r}, "
            f"status_code={self.status_code!r}, message={self.message!r})"
        )


class HttpTimeoutError(SafeHttpError):
    """Raised when connect, header, or body streaming exceeds the deadline."""

    default_code = HttpErrorCode.TIMEOUT

    def __init__(self, message: str = "HTTP 请求超时", **kwargs: Any) -> None:
        super().__init__(HttpErrorCode.TIMEOUT, message, **kwargs)


class ResponseTooLargeError(SafeHttpError):
    """Raised when response Content-Length or streamed body exceeds `max_bytes`."""

    default_code = HttpErrorCode.TOO_LARGE

    def __init__(self, message: str = "上游响应体超出最大字节限制", **kwargs: Any) -> None:
        super().__init__(HttpErrorCode.TOO_LARGE, message, **kwargs)


class BlockedRedirectError(SafeHttpError):
    """Raised when a redirect is disallowed, malformed, or exceeds `max_redirects`."""

    default_code = HttpErrorCode.BLOCKED_REDIRECT

    def __init__(self, message: str = "HTTP 重定向已被阻止", **kwargs: Any) -> None:
        kwargs.setdefault("redirect_blocked", True)
        super().__init__(HttpErrorCode.BLOCKED_REDIRECT, message, **kwargs)


class SsrfBlockedError(SafeHttpError):
    """Raised when URL scheme, host, or resolved IP violates SSRF / HTTPS policy."""

    default_code = HttpErrorCode.SSRF

    def __init__(self, message: str = "目标地址被安全策略拒绝 (SSRF)", **kwargs: Any) -> None:
        super().__init__(HttpErrorCode.SSRF, message, **kwargs)


class UpstreamStatusError(SafeHttpError):
    """Raised on non-2xx/4xx/5xx upstream status when `raise_for_status=True`."""

    default_code = HttpErrorCode.UPSTREAM_STATUS

    def __init__(self, message: str, *, status_code: int, **kwargs: Any) -> None:
        super().__init__(HttpErrorCode.UPSTREAM_STATUS, message, status_code=status_code, **kwargs)


def _strip_ipv6_brackets(host: str) -> str:
    cleaned = host.strip().lower().rstrip(".")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return cleaned[1:-1]
    return cleaned


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    cleaned = _strip_ipv6_brackets(host)
    if "%" in cleaned:
        return None
    try:
        return ipaddress.ip_address(cleaned)
    except ValueError:
        return None


def _expand_ip_candidates(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip_obj]
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            candidates.append(ip_obj.ipv4_mapped)
        if ip_obj.sixtofour is not None:
            candidates.append(ip_obj.sixtofour)
        if ip_obj.teredo is not None:
            candidates.extend(ip_obj.teredo)
    return candidates


def is_disallowed_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool = False,
    allow_loopback: bool = False,
) -> bool:
    """Return True if IP (or embedded IPv4) is private, loopback, link-local, multicast, or non-global."""
    for addr in _expand_ip_candidates(ip_obj):
        if addr.is_multicast or addr.is_unspecified:
            return True
        if addr.is_loopback:
            if not (allow_private or allow_loopback):
                return True
            continue
        if (
            addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or not addr.is_global
        ):
            if not allow_private:
                return True
    return False


def is_localhost_host(host: str) -> bool:
    """Return True if `host` explicitly names localhost or a loopback IP literal."""
    norm = _strip_ipv6_brackets(host)
    if norm in _LOOPBACK_HOSTS:
        return True
    ip_obj = _parse_ip_literal(norm)
    if ip_obj is None:
        return False
    return all(addr.is_loopback for addr in _expand_ip_candidates(ip_obj))


def url_origin(url: str) -> tuple[str, str, int]:
    """Return normalized `(scheme, host, port)` origin tuple for an HTTP/HTTPS URL."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = _strip_ipv6_brackets(parsed.hostname or "")
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


@dataclass(frozen=True, slots=True)
class _ValidatedTarget:
    url: str
    scheme: str
    host: str
    port: int
    path_and_query: str
    origin: tuple[str, str, int]
    is_localhost: bool


def validate_safe_url(
    raw_url: str,
    *,
    allow_localhost_http: bool = False,
    allow_private: bool = False,
    allow_loopback_target: bool | None = None,
    secrets: Iterable[str] = (),
) -> _ValidatedTarget:
    """Validate URL syntax, scheme, embedded credentials, and literal host SSRF rules."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "URL 不能为空", secrets=secrets)
    cleaned = raw_url.strip()
    if len(cleaned) > _MAX_URL_LENGTH:
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "URL 超出最大长度限制", secrets=secrets)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in cleaned):
        raise SafeHttpError(
            HttpErrorCode.INVALID_ARGUMENT, "URL 包含非法空白或控制字符", url=cleaned, secrets=secrets
        )
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise SafeHttpError(
            HttpErrorCode.INVALID_ARGUMENT, f"URL 格式无效: {exc}", url=cleaned, secrets=secrets
        ) from None

    if parsed.username is not None or parsed.password is not None:
        raise SafeHttpError(
            HttpErrorCode.INVALID_ARGUMENT,
            "URL 禁止内嵌用户名或密码凭据",
            url=cleaned,
            secrets=secrets,
        )

    scheme = parsed.scheme.lower()
    raw_host = parsed.hostname or ""
    if "%" in raw_host:
        raise SafeHttpError(
            HttpErrorCode.INVALID_ARGUMENT, "URL 主机名禁止包含 Zone ID", url=cleaned, secrets=secrets
        )
    host = _strip_ipv6_brackets(raw_host)
    if not host:
        raise SafeHttpError(
            HttpErrorCode.INVALID_ARGUMENT, "URL 缺少有效主机名", url=cleaned, secrets=secrets
        )
    resolved_port = port if port is not None else (443 if scheme == "https" else 80)
    if not (1 <= resolved_port <= 65535):
        raise SafeHttpError(
            HttpErrorCode.INVALID_ARGUMENT, "URL 端口超出范围", url=cleaned, secrets=secrets
        )

    host_is_localhost = is_localhost_host(host)
    if scheme == "https":
        pass
    elif scheme == "http":
        if not (allow_localhost_http and host_is_localhost):
            raise SsrfBlockedError(
                "仅允许 https:// 协议（http:// 仅在显式开启 localhost 测试开关且目标为回环地址时允许）",
                url=cleaned,
                secrets=secrets,
            )
    else:
        raise SsrfBlockedError(
            f"不允许的 URL 协议: {scheme or 'empty'}://",
            url=cleaned,
            secrets=secrets,
        )

    effective_allow_loopback = (
        allow_localhost_http if allow_loopback_target is None else allow_loopback_target
    )
    ip_obj = _parse_ip_literal(host)
    if ip_obj is not None:
        if is_disallowed_ip(
            ip_obj,
            allow_private=allow_private,
            allow_loopback=effective_allow_loopback,
        ):
            raise SsrfBlockedError(
                f"禁止访问私网、回环、链路本地或组播 IP 地址: {host}",
                url=cleaned,
                secrets=secrets,
            )
    else:
        if host_is_localhost:
            if not (allow_private or effective_allow_loopback):
                raise SsrfBlockedError(
                    f"禁止访问本地回环主机: {host}",
                    url=cleaned,
                    secrets=secrets,
                )
        elif not allow_private and (
            host in _INTERNAL_EXACT_HOSTS or host.endswith(_INTERNAL_SUFFIXES)
        ):
            raise SsrfBlockedError(
                f"禁止访问内部保留域名: {host}",
                url=cleaned,
                secrets=secrets,
            )

    normalized_url = urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))
    path = parsed.path or "/"
    path_and_query = f"{path}?{parsed.query}" if parsed.query else path
    return _ValidatedTarget(
        url=normalized_url,
        scheme=scheme,
        host=host,
        port=resolved_port,
        path_and_query=path_and_query,
        origin=(scheme, host, resolved_port),
        is_localhost=host_is_localhost,
    )


def resolve_and_validate_host(
    target: _ValidatedTarget,
    *,
    allow_private: bool,
    allow_loopback: bool,
    resolver: DnsResolver | None = None,
    secrets: Iterable[str] = (),
) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    """Resolve DNS once and validate every returned IP to prevent SSRF and DNS rebinding."""
    ip_obj = _parse_ip_literal(target.host)
    if ip_obj is not None:
        family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
        sockaddr: tuple[Any, ...] = (str(ip_obj), target.port, 0, 0) if family == socket.AF_INET6 else (str(ip_obj), target.port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    resolve_fn: DnsResolver = resolver if resolver is not None else (
        lambda h, p, fam, stype: socket.getaddrinfo(h, p, fam, stype)
    )
    try:
        infos = resolve_fn(target.host, target.port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise SafeHttpError(
            HttpErrorCode.SERVICE_OFFLINE,
            f"域名解析失败 ({target.host}): {exc}",
            url=target.url,
            secrets=secrets,
        ) from None

    if not infos:
        raise SafeHttpError(
            HttpErrorCode.SERVICE_OFFLINE,
            f"域名解析未返回可用地址 ({target.host})",
            url=target.url,
            secrets=secrets,
        )

    validated: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
    for info in infos:
        family, socktype, proto, canonname, sockaddr = info[:5]
        if not sockaddr:
            continue
        raw_ip = str(sockaddr[0])
        if "%" in raw_ip:
            raise SsrfBlockedError(
                f"域名 {target.host} 解析到带 Zone ID 的链路本地地址",
                url=target.url,
                secrets=secrets,
            )
        try:
            resolved_ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise SsrfBlockedError(
                f"域名 {target.host} 解析到无效 IP 地址",
                url=target.url,
                secrets=secrets,
            ) from None

        if target.scheme == "http" and not all(
            addr.is_loopback for addr in _expand_ip_candidates(resolved_ip)
        ):
            raise SsrfBlockedError(
                f"HTTP localhost 测试域名 {target.host} 解析到非回环地址",
                url=target.url,
                secrets=secrets,
            )

        if is_disallowed_ip(
            resolved_ip,
            allow_private=allow_private,
            allow_loopback=allow_loopback,
        ):
            raise SsrfBlockedError(
                f"域名 {target.host} 解析到受限私网/回环/链路本地/组播地址",
                url=target.url,
                secrets=secrets,
            )
        validated.append((int(family), int(socktype), int(proto), str(canonname), tuple(sockaddr)))

    if not validated:
        raise SafeHttpError(
            HttpErrorCode.SERVICE_OFFLINE,
            f"域名解析未返回有效地址 ({target.host})",
            url=target.url,
            secrets=secrets,
        )
    return validated


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection connecting strictly to pre-validated resolved socket addresses."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        pinned_addrinfos: Sequence[tuple[int, int, int, str, tuple[Any, ...]]],
    ) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_addrinfos = list(pinned_addrinfos)

    def _connect_pinned(self) -> None:
        last_exc: OSError | None = None
        for family, socktype, proto, _, sockaddr in self._pinned_addrinfos:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                self.sock = sock
                try:
                    self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError as exc:
                    if exc.errno != errno.ENOPROTOOPT:
                        raise
                return
            except OSError as exc:
                last_exc = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        if last_exc is not None:
            raise last_exc
        raise OSError("No pinned addresses available")

    def connect(self) -> None:
        self._connect_pinned()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection connecting to pinned IP while preserving TLS SNI and cert hostname checks."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
        pinned_addrinfos: Sequence[tuple[int, int, int, str, tuple[Any, ...]]],
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_addrinfos = list(pinned_addrinfos)

    def connect(self) -> None:
        last_exc: OSError | None = None
        for family, socktype, proto, _, sockaddr in self._pinned_addrinfos:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError as exc:
                    if exc.errno != errno.ENOPROTOOPT:
                        raise
                wrapped = self._context.wrap_socket(sock, server_hostname=self.host)
                self.sock = wrapped
                return
            except BaseException as exc:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if isinstance(exc, ssl.SSLError):
                    raise
                if isinstance(exc, OSError):
                    last_exc = exc
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise OSError("No pinned addresses available")


def _connect_pinned_socket(
    pinned_addrinfos: Sequence[tuple[int, int, int, str, tuple[Any, ...]]],
    *,
    timeout: float,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Connect to the first usable pinned address and return the socket."""
    last_exc: OSError | None = None
    for family, socktype, proto, _, sockaddr in pinned_addrinfos:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                if exc.errno != errno.ENOPROTOOPT:
                    raise
            return sock
        except BaseException as exc:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if isinstance(exc, OSError):
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise OSError("No pinned addresses available")


class _PinnedHTTPSProxyConnection(http.client.HTTPSConnection):
    """HTTPS origin reached through an HTTP CONNECT proxy with pinned proxy DNS."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
        proxy_addrinfos: Sequence[tuple[int, int, int, str, tuple[Any, ...]]],
        proxy_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._proxy_addrinfos = list(proxy_addrinfos)
        self._tunnel_host = host
        self._tunnel_port = port
        self._tunnel_headers = dict(proxy_headers or {})

    def connect(self) -> None:
        sock = _connect_pinned_socket(self._proxy_addrinfos, timeout=self.timeout, source_address=self.source_address)
        self.sock = sock
        try:
            self._tunnel()
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host)
        except BaseException:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            raise


class _PinnedHTTPProxyConnection(http.client.HTTPConnection):
    """Plain HTTP origin reached through an HTTP proxy with pinned proxy DNS."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        proxy_addrinfos: Sequence[tuple[int, int, int, str, tuple[Any, ...]]],
    ) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._proxy_addrinfos = list(proxy_addrinfos)

    def connect(self) -> None:
        self.sock = _connect_pinned_socket(
            self._proxy_addrinfos, timeout=self.timeout, source_address=self.source_address
        )


@dataclass(frozen=True, slots=True)
class _ProxyTarget:
    """Parsed explicit or environment HTTP proxy."""

    url: str
    host: str
    port: int
    headers: dict[str, str]
    explicit: bool = False


def _parse_proxy_url(raw: str, *, explicit: bool = False) -> _ProxyTarget:
    value = (raw or "").strip()
    if not value:
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "代理地址不能为空")
    if "://" not in value:
        value = f"http://{value}"
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, f"代理地址格式无效: {exc}") from None
    if parsed.scheme.lower() != "http":
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "当前仅支持 http:// CONNECT 代理")
    host = (parsed.hostname or "").strip()
    if not host:
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "代理地址缺少主机名")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "代理地址不能包含路径、查询或片段")
    resolved_port = port if port is not None else 80
    if not (1 <= resolved_port <= 65535):
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "代理端口超出范围")
    headers: dict[str, str] = {}
    if parsed.username is not None or parsed.password is not None:
        username = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Proxy-Authorization"] = f"Basic {token}"
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return _ProxyTarget(
        url=f"http://{netloc}",
        host=host,
        port=resolved_port,
        headers=headers,
        explicit=explicit,
    )


def _no_proxy_match(host: str, port: int, no_proxy: str) -> bool:
    host_lower = (host or "").lower().rstrip(".")
    if not host_lower:
        return True
    for raw_token in (no_proxy or "").split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token == "*":
            return True
        if "://" in token:
            token = urllib.parse.urlsplit(token).hostname or ""
        if not token:
            continue
        if token.startswith("[") and "]" in token:
            token_host, _, token_port = token[1:].partition("]")
            if token_port.startswith(":"):
                token_port = token_port[1:]
            else:
                token_port = ""
        elif token.count(":") == 1:
            token_host, token_port = token.rsplit(":", 1)
        else:
            token_host, token_port = token, ""
        token_host = token_host.lstrip(".")
        if token_port.isdigit() and int(token_port) != port:
            continue
        if host_lower == token_host or host_lower.endswith(f".{token_host}"):
            return True
    return False


def _env_proxy_url(scheme: str) -> str:
    upper = "HTTPS" if scheme == "https" else "HTTP"
    for key in (f"{upper}_PROXY", f"{upper.lower()}_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _extract_header_secrets(headers: Mapping[str, str]) -> list[str]:
    secrets: list[str] = []
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_REQUEST_HEADERS and v:
            secrets.append(v)
            parts = v.split(None, 1)
            if len(parts) == 2 and len(parts[1]) >= 4:
                secrets.append(parts[1])
    return secrets


def _sanitize_headers(
    default_headers: Mapping[str, str],
    call_headers: Mapping[str, str] | None,
    *,
    secrets: Iterable[str],
) -> dict[str, str]:
    merged: dict[str, str] = {}
    lower_index: dict[str, str] = {}
    for source in (default_headers, call_headers or {}):
        for raw_k, raw_v in source.items():
            key = str(raw_k).strip()
            val = str(raw_v)
            if not key or any(ch in key for ch in ("\r", "\n", "\x00", ":")) or any(
                ch in val for ch in ("\r", "\n", "\x00")
            ):
                raise SafeHttpError(
                    HttpErrorCode.INVALID_ARGUMENT,
                    f"HTTP 请求头包含非法名称或控制字符: {key!r}",
                    secrets=secrets,
                )
            low = key.lower()
            if low in lower_index:
                merged.pop(lower_index[low], None)
            lower_index[low] = key
            merged[key] = val
    return merged


def _strip_headers_case_insensitive(headers: dict[str, str], names: frozenset[str]) -> None:
    for key in [k for k in headers if k.lower() in names]:
        headers.pop(key, None)


def _has_header_case_insensitive(headers: Mapping[str, str], name: str) -> bool:
    low = name.lower()
    return any(k.lower() == low for k in headers)


def _append_query_params(url: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None) -> str:
    if not params:
        return url
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params)
    filtered = [(str(k), str(v)) for k, v in pairs if v is not None]
    if not filtered:
        return url
    parsed = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    combined_query = urllib.parse.urlencode(existing + filtered)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, combined_query, parsed.fragment))


def _prepare_body(
    *,
    content: bytes | str | None,
    data: Mapping[str, Any] | bytes | str | None,
    json_payload: Any,
    headers: dict[str, str],
    secrets: Iterable[str],
) -> bytes | None:
    provided = sum(1 for item in (content, data, json_payload) if item is not None)
    if provided > 1:
        raise SafeHttpError(
            HttpErrorCode.INVALID_ARGUMENT,
            "content、data、json 参数只能指定其中一个",
            secrets=secrets,
        )
    if json_payload is not None:
        try:
            encoded = _json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SafeHttpError(
                HttpErrorCode.INVALID_ARGUMENT, f"JSON 请求体序列化失败: {exc}", secrets=secrets
            ) from None
        if not _has_header_case_insensitive(headers, "content-type"):
            headers["Content-Type"] = "application/json; charset=utf-8"
        return encoded
    if data is not None:
        if isinstance(data, Mapping):
            encoded = urllib.parse.urlencode(
                [(str(k), str(v)) for k, v in data.items() if v is not None]
            ).encode("utf-8")
            if not _has_header_case_insensitive(headers, "content-type"):
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
            return encoded
        if isinstance(data, str):
            return data.encode("utf-8")
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "不支持的 data 请求体类型", secrets=secrets)
    if content is not None:
        if isinstance(content, str):
            return content.encode("utf-8")
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "不支持的 content 请求体类型", secrets=secrets)
    return None


def _parse_content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        val = int(str(raw).strip())
        return val if val >= 0 else None
    except ValueError:
        return None


def _extract_charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        piece = part.strip()
        if piece.lower().startswith("charset="):
            charset = piece.split("=", 1)[1].strip().strip("\"'")
            if charset:
                return charset
    return "utf-8"


def _map_transport_exception(
    exc: BaseException,
    *,
    url: str,
    secrets: tuple[str, ...],
) -> SafeHttpError:
    if isinstance(exc, SafeHttpError):
        return exc
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return HttpTimeoutError("HTTP 请求超时", url=url, secrets=secrets)
    if isinstance(exc, ssl.SSLError):
        return SafeHttpError(
            HttpErrorCode.SERVICE_OFFLINE,
            f"TLS 握手或证书校验失败: {exc}",
            url=url,
            secrets=secrets,
        )
    if isinstance(exc, (http.client.HTTPException, OSError)):
        if "timed out" in str(exc).lower():
            return HttpTimeoutError("HTTP 请求超时", url=url, secrets=secrets)
        return SafeHttpError(
            HttpErrorCode.SERVICE_OFFLINE,
            f"网络请求失败: {exc}",
            url=url,
            secrets=secrets,
        )
    return SafeHttpError(
        HttpErrorCode.SERVICE_OFFLINE,
        f"HTTP 请求异常: {type(exc).__name__}",
        url=url,
        secrets=secrets,
    )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Bounded buffered HTTP response returned by `SafeHttpClient`."""

    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes
    redirect_history: tuple[str, ...] = ()

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def body(self) -> bytes:
        return self.content

    @property
    def text(self) -> str:
        charset = _extract_charset(self.headers.get("content-type", ""))
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def get_header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        return _json.loads(self.text)

    def raise_for_status(self) -> HttpResponse:
        if self.status_code >= 400:
            raise UpstreamStatusError(
                f"上游返回错误状态码 HTTP {self.status_code}",
                status_code=self.status_code,
                url=self.url,
            )
        return self

    def __repr__(self) -> str:
        return (
            f"HttpResponse(status_code={self.status_code}, "
            f"url={scrub_url_secrets(self.url)!r}, bytes={len(self.content)})"
        )

    __str__ = __repr__


class StreamingHttpResponse:
    """Async streaming HTTP response enforcing cumulative max_bytes and deadline."""

    def __init__(
        self,
        *,
        status_code: int,
        url: str,
        headers: dict[str, str],
        redirect_history: tuple[str, ...],
        conn: http.client.HTTPConnection,
        resp: http.client.HTTPResponse,
        deadline: float,
        max_bytes: int,
        secrets: tuple[str, ...],
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = headers
        self.redirect_history = redirect_history
        self._conn = conn
        self._resp = resp
        self._deadline = deadline
        self._max_bytes = max_bytes
        self._secrets = secrets
        self._bytes_read = 0
        self._closed = False
        self._eof = False

    @property
    def status(self) -> int:
        return self.status_code

    def get_header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def _read_next_chunk_sync(self, chunk_size: int) -> bytes:
        if self._closed or self._eof:
            return b""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise HttpTimeoutError("流式读取 HTTP 响应超时", url=self.url, secrets=self._secrets)
        sock = getattr(self._conn, "sock", None)
        if sock is not None:
            sock.settimeout(max(0.001, remaining))
        chunk = self._resp.read1(chunk_size)
        if not chunk:
            self._eof = True
            return b""
        self._bytes_read += len(chunk)
        if self._bytes_read > self._max_bytes:
            raise ResponseTooLargeError(
                f"流式响应体超过 {self._max_bytes} 字节上限",
                url=self.url,
                secrets=self._secrets,
            )
        return chunk

    async def iter_bytes(self, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        try:
            while not self._closed and not self._eof:
                try:
                    chunk = await asyncio.to_thread(self._read_next_chunk_sync, chunk_size)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        await self.aclose()
                        raise
                    await self.aclose()
                    raise _map_transport_exception(exc, url=self.url, secrets=self._secrets) from None
                if not chunk:
                    break
                yield chunk
        finally:
            if self._eof:
                await self.aclose()

    async def iter_lines(self, encoding: str = "utf-8", *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[str]:
        buf = bytearray()
        async for chunk in self.iter_bytes(chunk_size=chunk_size):
            buf.extend(chunk)
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    break
                line = bytes(buf[:idx]).rstrip(b"\r")
                del buf[: idx + 1]
                yield line.decode(encoding, errors="replace")
        if buf:
            yield bytes(buf).rstrip(b"\r").decode(encoding, errors="replace")

    async def read(self) -> bytes:
        chunks: list[bytes] = []
        async for chunk in self.iter_bytes():
            chunks.append(chunk)
        return b"".join(chunks)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._resp.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return (
            f"StreamingHttpResponse(status_code={self.status_code}, "
            f"url={scrub_url_secrets(self.url)!r}, bytes_read={self._bytes_read})"
        )

    __str__ = __repr__


class SafeHttpClient:
    """Async-first HTTPS client enforcing SSRF, DNS-rebinding, proxy, size, and redirect policies."""

    def __init__(
        self,
        *,
        allow_localhost_http: bool = False,
        allow_private: bool = False,
        trust_env: bool = True,
        proxy_url: str | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        timeout: float | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        follow_redirects: bool = True,
        raise_for_status: bool = True,
        default_headers: Mapping[str, str] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        resolver: DnsResolver | None = None,
        secrets: Iterable[str] = (),
        redactor: SecretRedactor | None = None,
    ) -> None:
        effective_timeout = float(timeout if timeout is not None else timeout_sec)
        if effective_timeout <= 0:
            raise ValueError("timeout_sec must be > 0")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        if max_redirects < 0:
            raise ValueError("max_redirects must be >= 0")

        self.trust_env: bool = bool(trust_env)
        self._explicit_proxy: _ProxyTarget | None = (
            _parse_proxy_url(proxy_url, explicit=True) if proxy_url else None
        )
        self._proxy_addrinfos_cache: dict[str, list[tuple[int, int, int, str, tuple[Any, ...]]]] = {}
        self.allow_localhost_http: bool = bool(allow_localhost_http)
        self.allow_private: bool = bool(allow_private)
        self.timeout_sec: float = effective_timeout
        self.max_bytes: int = int(max_bytes)
        self.max_redirects: int = int(max_redirects)
        self.follow_redirects: bool = bool(follow_redirects)
        self.raise_for_status: bool = bool(raise_for_status)
        self._default_headers: dict[str, str] = {"User-Agent": "OmniDesk-SDK/0.2"}
        if default_headers:
            self._default_headers = _sanitize_headers(
                self._default_headers, default_headers, secrets=secrets
            )
        self._ssl_context: ssl.SSLContext | None = ssl_context
        self._resolver: DnsResolver | None = resolver
        self._secrets: tuple[str, ...] = tuple(s for s in secrets if isinstance(s, str) and s)
        self._redactor: SecretRedactor | None = redactor

    def _get_ssl_context(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    def _collect_secrets(self, req_headers: Mapping[str, str], extra: Iterable[str]) -> tuple[str, ...]:
        collected: list[str] = list(self._secrets)
        collected.extend(s for s in extra if isinstance(s, str) and s)
        collected.extend(_extract_header_secrets(req_headers))
        if self._redactor is not None:
            collected.extend(self._redactor.secrets())
        return tuple(sorted({s for s in collected if s}, key=len, reverse=True))

    def _proxy_for(self, target: _ValidatedTarget) -> _ProxyTarget | None:
        if self._explicit_proxy is not None:
            return self._explicit_proxy
        if not self.trust_env:
            return None
        no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        if _no_proxy_match(target.host, target.port, no_proxy):
            return None
        raw = _env_proxy_url(target.scheme)
        if not raw:
            return None
        return _parse_proxy_url(raw, explicit=False)

    def _resolve_proxy(
        self,
        proxy: _ProxyTarget,
        *,
        secrets: tuple[str, ...],
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        cache_key = f"{proxy.host}:{proxy.port}"
        cached = self._proxy_addrinfos_cache.get(cache_key)
        if cached is not None:
            return cached
        host_literal = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
        probe = validate_safe_url(
            f"https://{host_literal}:{proxy.port}/",
            allow_private=True,
            allow_loopback_target=True,
            secrets=secrets,
        )
        resolved = resolve_and_validate_host(
            probe,
            allow_private=True,
            allow_loopback=True,
            resolver=self._resolver,
            secrets=secrets,
        )
        self._proxy_addrinfos_cache[cache_key] = resolved
        return resolved

    def _open_stream_sync(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        deadline: float,
        max_bytes: int,
        max_redirects: int,
        follow_redirects: bool,
        raise_for_status: bool,
        secrets: tuple[str, ...],
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, str, dict[str, str], tuple[str, ...]]:
        current_url = url
        current_method = method
        current_headers = dict(headers)
        current_body = body
        redirect_history: list[str] = []
        hops = 0

        initial_target = validate_safe_url(
            current_url,
            allow_localhost_http=self.allow_localhost_http,
            allow_private=self.allow_private,
            secrets=secrets,
        )
        initial_is_localhost = initial_target.is_localhost

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HttpTimeoutError("HTTP 请求超时", url=current_url, secrets=secrets)

            allow_loopback_hop = self.allow_private or (self.allow_localhost_http and initial_is_localhost)
            target = (
                initial_target
                if hops == 0
                else validate_safe_url(
                    current_url,
                    allow_localhost_http=self.allow_localhost_http and initial_is_localhost,
                    allow_private=self.allow_private,
                    allow_loopback_target=allow_loopback_hop,
                    secrets=secrets,
                )
            )
            proxy = self._proxy_for(target)
            addrinfos = (
                []
                if proxy is not None
                else resolve_and_validate_host(
                    target,
                    allow_private=self.allow_private,
                    allow_loopback=allow_loopback_hop,
                    resolver=self._resolver,
                    secrets=secrets,
                )
            )
            conn_timeout = max(0.001, remaining)
            proxy_addrinfos = self._resolve_proxy(proxy, secrets=secrets) if proxy is not None else []
            if target.scheme == "https" and proxy is not None:
                conn: http.client.HTTPConnection = _PinnedHTTPSProxyConnection(
                    target.host,
                    target.port,
                    timeout=conn_timeout,
                    context=self._get_ssl_context(),
                    proxy_addrinfos=proxy_addrinfos,
                    proxy_headers=proxy.headers,
                )
            elif target.scheme == "https":
                conn = _PinnedHTTPSConnection(
                    target.host,
                    target.port,
                    timeout=conn_timeout,
                    context=self._get_ssl_context(),
                    pinned_addrinfos=addrinfos,
                )
            elif proxy is not None:
                conn = _PinnedHTTPProxyConnection(
                    target.host,
                    target.port,
                    timeout=conn_timeout,
                    proxy_addrinfos=proxy_addrinfos,
                )
            else:
                conn = _PinnedHTTPConnection(
                    target.host,
                    target.port,
                    timeout=conn_timeout,
                    pinned_addrinfos=addrinfos,
                )

            try:
                request_target = (
                    target.url
                    if proxy is not None and target.scheme == "http"
                    else target.path_and_query
                )
                request_headers = dict(current_headers)
                if proxy is not None and target.scheme == "http":
                    request_headers.update(proxy.headers)
                conn.request(current_method, request_target, body=current_body, headers=request_headers)
                remaining_after_send = deadline - time.monotonic()
                if remaining_after_send <= 0:
                    raise HttpTimeoutError("HTTP 请求超时", url=target.url, secrets=secrets)
                if conn.sock is not None:
                    conn.sock.settimeout(max(0.001, remaining_after_send))
                resp = conn.getresponse()
            except BaseException as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                raise _map_transport_exception(exc, url=target.url, secrets=secrets) from None

            status = int(resp.status)
            resp_headers = {str(k).lower(): str(v) for k, v in resp.getheaders()}
            logger.debug(
                "http_response method=%s url=%s status=%d",
                current_method,
                scrub_url_secrets(target.url),
                status,
            )

            if status in _REDIRECT_STATUSES:
                location = (resp_headers.get("location") or "").strip()
                try:
                    resp.close()
                finally:
                    conn.close()

                if not follow_redirects or max_redirects <= 0:
                    raise BlockedRedirectError(
                        f"上游返回重定向 HTTP {status}，已配置禁止跟随重定向",
                        status_code=status,
                        url=target.url,
                        secrets=secrets,
                    )
                if not location:
                    raise BlockedRedirectError(
                        f"上游重定向 HTTP {status} 缺少 Location 响应头",
                        status_code=status,
                        url=target.url,
                        secrets=secrets,
                    )
                if hops >= max_redirects:
                    raise BlockedRedirectError(
                        f"HTTP 重定向次数超过上限 ({max_redirects})",
                        status_code=status,
                        url=target.url,
                        secrets=secrets,
                    )

                next_url = urllib.parse.urljoin(target.url, location)
                try:
                    next_target = validate_safe_url(
                        next_url,
                        allow_localhost_http=self.allow_localhost_http and initial_is_localhost,
                        allow_private=self.allow_private,
                        allow_loopback_target=allow_loopback_hop,
                        secrets=secrets,
                    )
                except SafeHttpError as exc:
                    if exc.error_code == HttpErrorCode.SSRF:
                        raise SsrfBlockedError(
                            exc.message,
                            status_code=status,
                            url=next_url,
                            redirect_blocked=True,
                            secrets=secrets,
                        ) from None
                    raise BlockedRedirectError(
                        exc.message,
                        status_code=status,
                        url=next_url,
                        secrets=secrets,
                    ) from None

                if next_target.origin != target.origin:
                    _strip_headers_case_insensitive(current_headers, _CROSS_ORIGIN_STRIP_HEADERS)

                if status in {301, 302, 303} and current_method not in {"GET", "HEAD"}:
                    current_method = "GET"
                    current_body = None
                    _strip_headers_case_insensitive(
                        current_headers,
                        frozenset({"content-length", "content-type", "transfer-encoding"}),
                    )

                redirect_history.append(target.url)
                current_url = next_target.url
                hops += 1
                continue

            if status >= 400 and raise_for_status:
                snippet = ""
                try:
                    if current_method != "HEAD":
                        sock = getattr(conn, "sock", None)
                        if sock is not None:
                            sock.settimeout(max(0.001, min(1.0, deadline - time.monotonic())))
                        raw_snippet = resp.read1(_MAX_ERROR_SNIPPET_BYTES)
                        snippet = raw_snippet.decode("utf-8", errors="replace").strip()
                except Exception:
                    snippet = ""
                finally:
                    try:
                        resp.close()
                    finally:
                        conn.close()
                detail = f": {snippet[:200]}" if snippet else ""
                raise UpstreamStatusError(
                    f"上游请求失败 (HTTP {status}){detail}",
                    status_code=status,
                    url=target.url,
                    secrets=secrets,
                )

            if current_method != "HEAD":
                content_length = _parse_content_length(resp_headers)
                if content_length is not None and content_length > max_bytes:
                    try:
                        resp.close()
                    finally:
                        conn.close()
                    raise ResponseTooLargeError(
                        f"响应 Content-Length ({content_length} 字节) 超过上限 ({max_bytes} 字节)",
                        status_code=status,
                        url=target.url,
                        secrets=secrets,
                    )

            return conn, resp, target.url, resp_headers, tuple(redirect_history)

    def _prepare_call(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
        content: bytes | str | None,
        data: Mapping[str, Any] | bytes | str | None,
        json_payload: Any,
        timeout_sec: float | None,
        timeout: float | None,
        max_bytes: int | None,
        max_redirects: int | None,
        follow_redirects: bool | None,
        raise_for_status: bool | None,
        secrets: Iterable[str],
    ) -> tuple[str, str, dict[str, str], bytes | None, float, int, int, bool, bool, tuple[str, ...]]:
        raw_secrets = [*self._secrets, *(s for s in secrets if isinstance(s, str) and s)]
        if not isinstance(method, str) or not _METHOD_RE.match(method.strip()):
            raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, f"无效 HTTP 方法: {method!r}", secrets=raw_secrets)
        norm_method = method.strip().upper()
        req_headers = _sanitize_headers(self._default_headers, headers, secrets=raw_secrets)
        all_secrets = self._collect_secrets(req_headers, secrets)

        eff_timeout = float(
            timeout_sec if timeout_sec is not None else (timeout if timeout is not None else self.timeout_sec)
        )
        if eff_timeout <= 0:
            raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "timeout_sec 必须大于 0", secrets=all_secrets)
        eff_max_bytes = int(max_bytes if max_bytes is not None else self.max_bytes)
        if eff_max_bytes <= 0:
            raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "max_bytes 必须大于 0", secrets=all_secrets)
        eff_max_redirects = int(max_redirects if max_redirects is not None else self.max_redirects)
        if eff_max_redirects < 0:
            raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "max_redirects 不能为负数", secrets=all_secrets)
        eff_follow = self.follow_redirects if follow_redirects is None else bool(follow_redirects)
        eff_raise = self.raise_for_status if raise_for_status is None else bool(raise_for_status)

        body = _prepare_body(
            content=content,
            data=data,
            json_payload=json_payload,
            headers=req_headers,
            secrets=all_secrets,
        )
        full_url = _append_query_params(url, params) if isinstance(url, str) and url.strip() else url
        return (
            norm_method,
            full_url,
            req_headers,
            body,
            eff_timeout,
            eff_max_bytes,
            eff_max_redirects,
            eff_follow,
            eff_raise,
            all_secrets,
        )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, Any] | bytes | str | None = None,
        json: Any = None,
        json_body: Any = None,
        timeout_sec: float | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        max_redirects: int | None = None,
        follow_redirects: bool | None = None,
        raise_for_status: bool | None = None,
        secrets: Iterable[str] = (),
    ) -> AsyncIterator[StreamingHttpResponse]:
        """Open a bounded, SSRF-safe streaming HTTP response."""
        if json is not None and json_body is not None:
            raise SafeHttpError(HttpErrorCode.INVALID_ARGUMENT, "json 与 json_body 不能同时传入")
        json_payload = json if json is not None else json_body
        (
            norm_method,
            full_url,
            req_headers,
            body,
            eff_timeout,
            eff_max_bytes,
            eff_max_redirects,
            eff_follow,
            eff_raise,
            all_secrets,
        ) = self._prepare_call(
            method,
            url,
            headers=headers,
            params=params,
            content=content,
            data=data,
            json_payload=json_payload,
            timeout_sec=timeout_sec,
            timeout=timeout,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            follow_redirects=follow_redirects,
            raise_for_status=raise_for_status,
            secrets=secrets,
        )
        deadline = time.monotonic() + eff_timeout
        conn, resp, final_url, resp_headers, redirect_history = await asyncio.to_thread(
            self._open_stream_sync,
            method=norm_method,
            url=full_url,
            headers=req_headers,
            body=body,
            deadline=deadline,
            max_bytes=eff_max_bytes,
            max_redirects=eff_max_redirects,
            follow_redirects=eff_follow,
            raise_for_status=eff_raise,
            secrets=all_secrets,
        )
        stream_resp = StreamingHttpResponse(
            status_code=int(resp.status),
            url=final_url,
            headers=resp_headers,
            redirect_history=redirect_history,
            conn=conn,
            resp=resp,
            deadline=deadline,
            max_bytes=eff_max_bytes,
            secrets=all_secrets,
        )
        try:
            yield stream_resp
        finally:
            await stream_resp.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, Any] | bytes | str | None = None,
        json: Any = None,
        json_body: Any = None,
        timeout_sec: float | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        max_redirects: int | None = None,
        follow_redirects: bool | None = None,
        raise_for_status: bool | None = None,
        secrets: Iterable[str] = (),
    ) -> HttpResponse:
        """Execute a bounded HTTP request and return an `HttpResponse`."""
        async with self.stream(
            method,
            url,
            headers=headers,
            params=params,
            content=content,
            data=data,
            json=json,
            json_body=json_body,
            timeout_sec=timeout_sec,
            timeout=timeout,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            follow_redirects=follow_redirects,
            raise_for_status=raise_for_status,
            secrets=secrets,
        ) as resp:
            body = await resp.read()
            return HttpResponse(
                status_code=resp.status_code,
                url=resp.url,
                headers=resp.headers,
                content=body,
                redirect_history=resp.redirect_history,
            )

    async def get(self, url: str, **kwargs: Any) -> HttpResponse:
        """Execute a hardened HTTP GET request."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> HttpResponse:
        """Execute a hardened HTTP POST request."""
        return await self.request("POST", url, **kwargs)

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Execute a hardened HTTP request and decode a JSON response body."""
        headers = dict(kwargs.pop("headers", None) or {})
        if not _has_header_case_insensitive(headers, "accept"):
            headers["Accept"] = "application/json"
        resp = await self.request(method, url, headers=headers, **kwargs)
        if not resp.content.strip():
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise SafeHttpError(
                HttpErrorCode.UPSTREAM_STATUS,
                f"上游返回无效 JSON 响应: {exc}",
                status_code=resp.status_code,
                url=resp.url,
            ) from None

    async def aclose(self) -> None:
        """Close hook for `async with SafeHttpClient()` ergonomics."""
        return None

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()


__all__ = [
    "BlockedRedirectError",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_TIMEOUT_SEC",
    "DnsResolver",
    "HttpErrorCode",
    "HttpResponse",
    "HttpTimeoutError",
    "ResponseTooLargeError",
    "SafeHttpClient",
    "SafeHttpError",
    "SsrfBlockedError",
    "StreamingHttpResponse",
    "UpstreamStatusError",
    "is_disallowed_ip",
    "is_localhost_host",
    "redact_http_text",
    "resolve_and_validate_host",
    "scrub_url_secrets",
    "url_origin",
    "validate_safe_url",
]
