"""Canonical errors, secret redaction, URL safety, and path validators for plugin-zhihu."""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
ZHIHU_HOSTS: frozenset[str] = frozenset(
    {
        "zhihu.com",
        "www.zhihu.com",
        "zhuanlan.zhihu.com",
        "api.zhihu.com",
        "m.zhihu.com",
    }
)
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {"cookie", "cookie2", "authorization", "proxy-authorization", "x-xsrftoken", "x-zse-96", "x-api-key"}
)

_ZHIHU_SECRET_RE = re.compile(
    r"(?i)\b(z_c0|d_c0|_xsrf|__zse_ck|cookie_header|x-xsrftoken|x-zse-96|set-cookie|authorization|cookie)\s*([:=])\s*([^\s;\r\n\"']+)"
)
_ZHIHU_JSON_SECRET_RE = re.compile(
    r"""(?i)(["'](?:cookie_header|z_c0|d_c0|_xsrf|__zse_ck|authorization|cookie|x-xsrftoken|x-zse-96)["']\s*:\s*)(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')"""
)
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:token|secret|cookie|z_c0|d_c0|_xsrf|auth|key)=)([^&\s#]+)")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_ALLOWED_SYSTEM_SYMLINK_PARENTS: frozenset[Path] = frozenset({Path("/var/run"), Path("/var/tmp"), Path("/tmp")})


class ZhihuError(LookupError):
    """Fail-closed domain/transport/auth error carrying a canonical OmniDesk error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = str(error_code or "service_offline")
        self.message = str(message)
        self.status_code = status_code
        self.details = dict(details) if details else {}
        super().__init__(self.message)


def has_control_chars(value: str) -> bool:
    """Return True when value contains NUL or CR/LF control characters."""
    return any(ch in value for ch in ("\x00", "\r", "\n"))


def redact_zhihu_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub Zhihu session cookies (z_c0, d_c0, _xsrf, __zse_ck), headers, and secrets."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _ZHIHU_JSON_SECRET_RE.sub(r'\1"[REDACTED]"', scrubbed)
    scrubbed = _ZHIHU_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", scrubbed)
    scrubbed = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", scrubbed)
    return redact_secrets(scrubbed, extra_secrets)


def url_origin(url: str) -> str:
    """Return normalized `<scheme>://<host>[:<port>]` origin for an HTTP/HTTPS URL."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def validate_https_url(
    raw_url: Any,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Enforce HTTPS URLs (with explicit loopback HTTP opt-in solely for local test servers)."""
    if not isinstance(raw_url, str):
        raise ZhihuError("invalid_argument", "URL 必须为字符串")
    cleaned = raw_url.strip()
    if not cleaned or has_control_chars(cleaned) or any(ch.isspace() for ch in cleaned):
        raise ZhihuError("invalid_argument", "URL 不能为空且不能包含空白或控制字符")
    if len(cleaned) > 4096:
        raise ZhihuError("invalid_argument", "URL 超出长度限制")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ZhihuError("invalid_argument", f"URL 格式无效: {exc}") from None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise ZhihuError("invalid_argument", f"URL 缺少有效主机名或包含内嵌凭据: {redact_zhihu_secrets(cleaned)}")
    if port is not None and not (1 <= port <= 65535):
        raise ZhihuError("invalid_argument", "URL 端口无效")
    if scheme == "https":
        return cleaned
    if scheme == "http" and host in LOOPBACK_HOSTS:
        origin = url_origin(cleaned)
        if allow_loopback_http or origin in set(allowed_loopback_origins):
            return cleaned
    raise ZhihuError("invalid_argument", f"仅允许 https:// 协议（收到: {scheme or 'unknown'}://）")


def validate_zhihu_url(
    raw_url: Any,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Validate that a URL is HTTPS (or allowed test loopback) and targets a Zhihu host."""
    safe_url = validate_https_url(
        raw_url,
        allow_loopback_http=allow_loopback_http,
        allowed_loopback_origins=allowed_loopback_origins,
    )
    parsed = urllib.parse.urlsplit(safe_url)
    host = (parsed.hostname or "").lower()
    if host in ZHIHU_HOSTS or host.endswith(".zhihu.com"):
        return safe_url
    if parsed.scheme.lower() == "http" and host in LOOPBACK_HOSTS and (
        allow_loopback_http or url_origin(safe_url) in set(allowed_loopback_origins)
    ):
        return safe_url
    raise ZhihuError("invalid_argument", f"非知乎域名 URL 已被拒绝: {host}")


def validate_safe_file_path(raw_path: Any, *, field_name: str = "cookie_file_path") -> Path:
    """Reject traversal ('..'), control chars, and symlink files/parents for local Cookie files."""
    if not isinstance(raw_path, (str, Path)):
        raise ZhihuError("path_safety", f"{field_name} 必须为路径字符串")
    text = os.fspath(raw_path).strip()
    if not text:
        raise ZhihuError("invalid_argument", f"{field_name} 不能为空")
    if has_control_chars(text):
        raise ZhihuError("path_safety", f"{field_name} 包含非法控制字符")
    raw_parts = text.replace("\\", "/").split("/")
    if ".." in raw_parts:
        raise ZhihuError("path_safety", f"{field_name} 禁止包含路径穿越 '..'")
    candidate = Path(text).expanduser()
    if ".." in candidate.parts:
        raise ZhihuError("path_safety", f"{field_name} 禁止包含路径穿越 '..'")
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise ZhihuError("path_safety", f"拒绝访问符号链接路径: {field_name}")
    for parent in candidate.parents:
        if parent == Path(parent.anchor):
            break
        if parent.is_symlink() and parent not in _ALLOWED_SYSTEM_SYMLINK_PARENTS:
            raise ZhihuError("path_safety", f"拒绝位于符号链接父目录下的路径: {field_name}")
    return candidate


def validate_segment_id(raw_id: Any, *, field: str = "id") -> str:
    """Validate an alphanumeric/underscore/dash identifier for path safety in API URLs."""
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ZhihuError("invalid_argument", f"缺少有效的 {field}")
    cleaned = raw_id.strip()
    if not _SAFE_SEGMENT_RE.match(cleaned):
        raise ZhihuError("invalid_argument", f"{field} 格式非法")
    return cleaned


def coerce_bool(val: Any, default: bool = False) -> bool:
    """Coerce common bool/string representations safely without throwing."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return val != 0
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return default


def coerce_float(val: Any, default: float) -> float:
    """Coerce numeric config values cleanly."""
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip())
        except ValueError:
            return default
    return default


def parse_optional_int(req: Mapping[str, Any], key: str, default: int) -> int:
    """Read an optional strict integer parameter (rejecting booleans and non-integers)."""
    if key not in req or req[key] is None:
        return default
    val = req[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise ZhihuError("invalid_argument", f"{key} 必须为整数")
    return val


def json_str(obj: Mapping[str, Any], key: str, default: str = "") -> str:
    """Read a string value from a mapping safely."""
    val = obj.get(key)
    return val if isinstance(val, str) else default


def json_str_chain(obj: Mapping[str, Any], keys: Iterable[str], default: str = "") -> str:
    """Return the first non-empty string value across `keys`."""
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return default
