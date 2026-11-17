"""Shared URL, CDP loopback, path-safety, and redaction validators for plugin-chrome-web."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_MAX_URL_LENGTH = 4096
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}")
_COOKIE_KV_RE = re.compile(r"(?i)\b(cookie|set-cookie|authorization|x-csrf-token)\s*([=:])\s*([^\r\n]+)")


class ValidationProblem(ValueError):
    """Structured validation error carrying a canonical CapabilityResult error_code."""

    def __init__(self, message: str, *, error_code: str = "invalid_argument") -> None:
        self.error_code = error_code
        super().__init__(message)


def validate_http_url(raw_url: Any, *, field_name: str = "url") -> str:
    """Strictly allow http/https URLs with a valid hostname and no control chars."""
    if not isinstance(raw_url, str):
        raise ValidationProblem(f"{field_name} 必须为非空 http/https URL 字符串")
    url = raw_url.strip()
    if not url or len(url) > _MAX_URL_LENGTH:
        raise ValidationProblem(f"{field_name} 为空或超出长度限制")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in url):
        raise ValidationProblem(f"{field_name} 包含非法空白或控制字符")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValidationProblem(f"{field_name} 格式无效: {exc}") from None
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValidationProblem(
            f"仅允许 http/https 协议，已拒绝危险协议: {scheme or '(empty)'}"
        )
    if not parsed.netloc or not parsed.hostname:
        raise ValidationProblem(f"{field_name} 缺少有效主机名")
    if port is not None and not (1 <= port <= 65535):
        raise ValidationProblem(f"{field_name} 端口无效")
    return url


def url_origin(url: str) -> tuple[str, str, int]:
    """Return normalized `(scheme, host, port)` origin tuple for an http/https URL."""
    parsed = urlsplit(validate_http_url(url))
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


def origin_string(url: str) -> str:
    """Return canonical `<scheme>://<host>[:<port>]` origin string."""
    scheme, host, port = url_origin(url)
    default_port = 443 if scheme == "https" else 80
    host_fmt = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_fmt}" if port == default_port else f"{scheme}://{host_fmt}:{port}"


def same_origin(url_a: str, url_b: str) -> bool:
    """Return True if two http/https URLs share the exact same `(scheme, host, port)`."""
    return url_origin(url_a) == url_origin(url_b)


def validate_cdp_port(raw_port: Any, *, allow_zero: bool = False) -> int:
    """Validate loopback CDP TCP port."""
    if isinstance(raw_port, bool):
        raise ValidationProblem("cdp_port 不能为布尔值")
    if isinstance(raw_port, int):
        port = raw_port
    elif isinstance(raw_port, str) and raw_port.strip().isdigit():
        port = int(raw_port.strip())
    else:
        raise ValidationProblem("cdp_port 必须为整数端口")
    min_port = 0 if allow_zero else 1
    if not (min_port <= port <= 65535):
        raise ValidationProblem(f"cdp_port 超出范围 ({min_port}..65535)")
    return port


def validate_cdp_url(raw_cdp_url: Any) -> tuple[str, int]:
    """Validate an explicit CDP HTTP/WebSocket base URL strictly bound to 127.0.0.1."""
    if not isinstance(raw_cdp_url, str) or not raw_cdp_url.strip():
        raise ValidationProblem("cdp_url 必须为非空 URL")
    cleaned = raw_cdp_url.strip()
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in cleaned):
        raise ValidationProblem("cdp_url 包含非法字符")
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ValidationProblem(f"cdp_url 无效: {exc}") from None
    if parsed.scheme.lower() not in {"http", "ws"}:
        raise ValidationProblem("cdp_url 仅支持 http:// 或 ws://")
    if (parsed.hostname or "") != "127.0.0.1":
        raise ValidationProblem("安全限制: CDP 仅允许连接 127.0.0.1", error_code="permission_denied")
    if port is None or not (1 <= port <= 65535):
        raise ValidationProblem("cdp_url 必须包含有效端口号")
    return "127.0.0.1", port


def validate_cdp_ws_url(ws_url: Any) -> tuple[str, int, str]:
    """Validate a target `webSocketDebuggerUrl` returned by CDP `/json/list`."""
    if not isinstance(ws_url, str) or not ws_url.strip():
        raise ValidationProblem("缺少 webSocketDebuggerUrl", error_code="service_offline")
    cleaned = ws_url.strip()
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ValidationProblem(f"webSocketDebuggerUrl 无效: {exc}", error_code="service_offline") from None
    if parsed.scheme.lower() != "ws":
        raise ValidationProblem("CDP WebSocket 仅允许 ws:// 协议", error_code="permission_denied")
    if (parsed.hostname or "") != "127.0.0.1":
        raise ValidationProblem("安全限制: CDP WebSocket 仅允许连接 127.0.0.1", error_code="permission_denied")
    if port is None or not (1 <= port <= 65535):
        raise ValidationProblem("CDP WebSocket 端口无效", error_code="service_offline")
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return "127.0.0.1", port, target


def validate_safe_path(
    raw_path: Any,
    *,
    base_dir: Path | None = None,
    require_absolute: bool = False,
    check_symlinks: bool = True,
) -> Path:
    """Reject null bytes, '..' traversal, symlinks, and optional base_dir escapes."""
    if not isinstance(raw_path, (str, Path)):
        raise ValidationProblem("路径参数必须为字符串或 Path", error_code="path_safety")
    text = os.fspath(raw_path)
    if not text or not text.strip() or "\x00" in text:
        raise ValidationProblem("路径不能为空或包含空字节", error_code="path_safety")
    expanded = Path(text).expanduser()
    if ".." in expanded.parts:
        raise ValidationProblem("禁止路径穿越 ('..')", error_code="path_safety")
    if base_dir is not None:
        root = base_dir.resolve()
        candidate = expanded if expanded.is_absolute() else (root / expanded)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValidationProblem("路径越界，超出允许目录范围", error_code="path_safety") from None
    else:
        if require_absolute and not expanded.is_absolute():
            raise ValidationProblem("配置路径必须为绝对路径", error_code="path_safety")
        candidate = expanded
    if check_symlinks:
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise ValidationProblem("拒绝访问符号链接路径", error_code="path_safety")
    return candidate


def resolve_profile_dir(configured_profile_dir: str = "") -> Path:
    """Resolve Chrome `--user-data-dir`, defaulting to `<XDG_DATA_HOME>/omnidesk/chrome-profile`."""
    cleaned = str(configured_profile_dir or "").strip()
    if cleaned:
        return validate_safe_path(cleaned, require_absolute=True, check_symlinks=True)
    raw_xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw_xdg:
        xdg_path = Path(raw_xdg).expanduser()
        base = xdg_path if xdg_path.is_absolute() else (Path.home() / ".local" / "share")
    else:
        base = Path.home() / ".local" / "share"
    app_dir = base / "omnidesk"
    profile_dir = app_dir / "chrome-profile"
    if app_dir.is_symlink() or profile_dir.is_symlink():
        raise ValidationProblem("受管 Chrome profile 路径不得为符号链接", error_code="path_safety")
    return profile_dir


def validate_secret_key_name(key: Any) -> str:
    """Validate a SecretVault key name (non-empty, no null bytes or outer whitespace)."""
    if not isinstance(key, str):
        raise ValidationProblem("secret_key 必须为非空字符串")
    cleaned = key.strip()
    if not cleaned or "\x00" in key or cleaned != key:
        raise ValidationProblem("secret_key 不能包含空字节或首尾空白")
    return key


def redact_sensitive(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub secret strings, Bearer tokens, and Cookie headers from diagnostics."""
    if not text:
        return ""
    scrubbed = text
    for secret in sorted({s for s in extra_secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    scrubbed = _BEARER_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _COOKIE_KV_RE.sub(r"\1\2[REDACTED]", scrubbed)
    return scrubbed
