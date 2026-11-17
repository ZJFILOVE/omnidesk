"""URL validation, path safety, secret redaction, and structured errors for plugin-linuxdo."""

from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

DEFAULT_BASE_URL = "https://linux.do"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_COOKIE_HEADER_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|x-csrf-token|proxy-authorization)\s*([:=])\s*([^\r\n]+)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|api_key|token|access_token|secret|cookie)=)[^&\s#]+"
)


class LinuxdoError(RuntimeError):
    """Fail-closed domain error carrying a canonical OmniDesk error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        http_status: int = 0,
        auth_required: bool = False,
        challenge_detected: bool = False,
        session_expired: bool = False,
        transport: str = "",
        fallback_used: bool = False,
    ) -> None:
        self.error_code = error_code
        self.http_status = http_status
        self.auth_required = auth_required
        self.challenge_detected = challenge_detected
        self.session_expired = session_expired
        self.transport = transport
        self.fallback_used = fallback_used
        super().__init__(message)


def has_control_chars(value: str) -> bool:
    """Return True if value contains NUL or CR/LF characters."""
    return any(ch in value for ch in ("\x00", "\r", "\n"))


def url_origin(url: str) -> tuple[str, str, int]:
    """Return normalized `(scheme, host, port)` origin tuple for an HTTP/HTTPS URL."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


def is_same_origin(url_a: str, url_b: str) -> bool:
    """Return True if two URLs share exact `(scheme, host, port)` origin."""
    try:
        return url_origin(url_a) == url_origin(url_b)
    except ValueError:
        return False


def _is_private_or_internal_host(host: str) -> bool:
    norm = host.strip().lower().rstrip(".")
    if not norm or norm in {"localhost", "ip6-localhost", "ip6-loopback", "metadata.google.internal"}:
        return True
    if norm.endswith((".localhost", ".local", ".internal", ".arpa")):
        return True
    try:
        ip_obj = ipaddress.ip_address(norm)
    except ValueError:
        return False
    candidates = [ip_obj]
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        candidates.append(ip_obj.ipv4_mapped)
    return any(
        a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified
        for a in candidates
    )


def validate_https_url(
    raw_url: Any,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[tuple[str, str, int]] = (),
    field_name: str = "url",
) -> str:
    """Enforce strict HTTPS URLs (with explicit opt-in only for local test loopback origins)."""
    if not isinstance(raw_url, str):
        raise LinuxdoError("invalid_argument", f"{field_name} 必须为非空 https:// URL 字符串")
    cleaned = raw_url.strip()
    if not cleaned or len(cleaned) > 4096:
        raise LinuxdoError("invalid_argument", f"{field_name} 为空或超出长度限制")
    if has_control_chars(cleaned) or any(ch.isspace() for ch in cleaned):
        raise LinuxdoError("invalid_argument", f"{field_name} 包含非法空白或控制字符")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise LinuxdoError("invalid_argument", f"{field_name} 格式无效: {exc}") from exc

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or parsed.username is not None or parsed.password is not None:
        raise LinuxdoError("invalid_argument", f"{field_name} 缺少有效主机名或包含内嵌凭据")
    if port is not None and not (1 <= port <= 65535):
        raise LinuxdoError("invalid_argument", f"{field_name} 端口无效")

    origin = (scheme, host, port if port is not None else (443 if scheme == "https" else 80))
    allowed_set = set(allowed_loopback_origins)
    if scheme == "https":
        if _is_private_or_internal_host(host) and origin not in allowed_set:
            raise LinuxdoError("invalid_argument", f"禁止访问内网或本地地址: {host}")
        return cleaned
    if scheme == "http" and host in LOOPBACK_HOSTS and (allow_loopback_http or origin in allowed_set):
        return cleaned
    raise LinuxdoError("invalid_argument", f"仅允许 https:// 协议 (收到 {scheme or 'unknown'}://)")


def resolve_relative_url(base_url: str, relative_url: str) -> str:
    """Resolve a relative URL against `base_url` safely."""
    rel = (relative_url or "").strip()
    if not rel:
        return ""
    return urllib.parse.urljoin(base_url, rel)


def validate_safe_file_path(raw_path: Any, *, base_dir: Path | None = None) -> Path:
    """Reject empty paths, NUL bytes, '..' traversal, symlinks, and optional base_dir escapes."""
    if not isinstance(raw_path, (str, Path)):
        raise LinuxdoError("path_safety", "路径参数必须为字符串")
    text = os.fspath(raw_path).strip()
    if not text or has_control_chars(text):
        raise LinuxdoError("path_safety", "路径不能为空或包含控制字符")
    expanded = Path(text).expanduser()
    if ".." in expanded.parts or ".." in text.replace("\\", "/").split("/"):
        raise LinuxdoError("path_safety", "禁止路径穿越 ('..')")
    if base_dir is not None:
        root = base_dir.resolve()
        candidate = expanded if expanded.is_absolute() else (root / expanded)
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise LinuxdoError("path_safety", "拒绝访问符号链接路径")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise LinuxdoError("path_safety", "路径超出允许的数据目录范围") from exc
        return resolved
    if expanded.is_symlink():
        raise LinuxdoError("path_safety", "拒绝访问符号链接文件")
    for parent in expanded.parents:
        if parent == Path(parent.anchor):
            break
        if parent.is_symlink() and parent not in {Path("/tmp"), Path("/var/tmp"), Path("/var/run")}:
            raise LinuxdoError("path_safety", f"拒绝位于符号链接父目录下的路径: {parent}")
    return expanded


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub secret tokens, Cookie headers, and query credentials from diagnostic strings."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", scrubbed)
    scrubbed = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", scrubbed)
    return scrubbed


def json_str(obj: Mapping[str, Any], key: str, fallback: str = "") -> str:
    """Strictly read a string field from a Discourse/RPC mapping (treating null as fallback)."""
    val = obj.get(key)
    return val if isinstance(val, str) else fallback


def json_str_chain(obj: Mapping[str, Any], keys: Iterable[str], fallback: str = "") -> str:
    """Return the first non-empty string field among `keys`."""
    for key in keys:
        val = json_str(obj, key, "")
        if val:
            return val
    return fallback


def json_int(obj: Mapping[str, Any], key: str, fallback: int = 0) -> int:
    """Strictly read an integer field (ignoring bools and null)."""
    val = obj.get(key)
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return fallback


def json_bool(obj: Mapping[str, Any], key: str, fallback: bool = False) -> bool:
    """Strictly read a boolean field."""
    val = obj.get(key)
    return val if isinstance(val, bool) else fallback


def json_bool_chain(obj: Mapping[str, Any], keys: Iterable[str], fallback: bool = False) -> bool:
    """Return the first boolean field found among `keys`."""
    for key in keys:
        val = obj.get(key)
        if isinstance(val, bool):
            return val
    return fallback


def extract_topic_id(article_id: Any) -> int:
    """Extract numeric Discourse topic ID from `topic:123`, `linuxdo:topic:123`, `123`, or `/t/.../123` URLs."""
    if isinstance(article_id, int) and not isinstance(article_id, bool):
        return article_id if article_id > 0 else 0
    if not isinstance(article_id, str):
        return 0
    s = article_id.strip()
    if not s:
        return 0
    if s.startswith("linuxdo:topic:"):
        s = s[14:]
    elif s.startswith("topic:"):
        s = s[6:]
    if s.lower().endswith(".json"):
        s = s[:-5]
    if s.isdigit():
        val = int(s)
        return val if val > 0 else 0
    tpos = s.find("/t/")
    if tpos != -1:
        rest = s[tpos + 3 :]
        for sep in ("?", "#"):
            if sep in rest:
                rest = rest.split(sep, 1)[0]
        segs = [
            seg[:-5] if seg.lower().endswith(".json") else seg
            for raw_seg in rest.split("/")
            if (seg := raw_seg.strip())
        ]
        if len(segs) >= 2 and segs[1].isdigit():
            val = int(segs[1])
            return val if val > 0 else 0
        if segs and segs[0].isdigit():
            val = int(segs[0])
            return val if val > 0 else 0
    return 0

