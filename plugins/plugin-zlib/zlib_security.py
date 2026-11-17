"""URL/SSRF containment, download_root path safety, and secret redaction for plugin-zlib."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from omnidesk_sdk.stores import redact_secrets

from zlib_magic import SUPPORTED_BOOK_FORMATS, BookValidationError, finalize_downloaded_file, validate_downloaded_file

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset(
    {"key", "api_key", "apikey", "token", "access_token", "remix_key", "secret", "cookie", "password"}
)
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")

BookSearchSecurityError = BookValidationError


@dataclass(frozen=True, slots=True)
class Origin:
    """Normalized `(scheme, host, port)` origin tuple."""

    scheme: str
    host: str
    port: int


def default_port_for_scheme(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def parse_origin(url: str) -> Origin:
    """Parse and validate an http/https URL into its canonical Origin."""
    if not isinstance(url, str) or not url.strip():
        raise BookSearchSecurityError("invalid_argument", "URL 不能为空")
    cleaned = url.strip()
    if any(ch in cleaned for ch in ("\x00", "\r", "\n")):
        raise BookSearchSecurityError("unsafe_url", "URL 包含非法控制字符")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except ValueError as exc:
        raise BookSearchSecurityError("unsafe_url", f"URL 格式无效: {exc}") from exc
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise BookSearchSecurityError("unsafe_url", f"仅允许 http/https 协议，拒绝 {scheme or 'unknown'}://")
    if parsed.username is not None or parsed.password is not None:
        raise BookSearchSecurityError("unsafe_url", "URL 不允许内嵌用户名或密码凭据")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BookSearchSecurityError("unsafe_url", "URL 缺少有效主机名")
    try:
        port = parsed.port if parsed.port is not None else default_port_for_scheme(scheme)
    except ValueError as exc:
        raise BookSearchSecurityError("unsafe_url", f"URL 端口无效: {exc}") from exc
    if not (1 <= port <= 65535):
        raise BookSearchSecurityError("unsafe_url", f"URL 端口超出范围: {port}")
    return Origin(scheme=scheme, host=host, port=port)


def is_private_ip_value(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if IP (or IPv4-mapped IPv6) is loopback, private, link-local, or reserved."""
    candidates = [ip_obj]
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        candidates.append(ip_obj.ipv4_mapped)
    return any(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        for addr in candidates
    )


def is_private_or_internal_host(host: str) -> bool:
    """Return True for localhost, internal suffixes, or non-public IP literals."""
    norm = host.strip().lower().rstrip(".")
    if not norm or norm in {"localhost", "ip6-localhost", "ip6-loopback", "metadata.google.internal"}:
        return True
    if norm.endswith((".localhost", ".local", ".internal", ".arpa")):
        return True
    try:
        ip_obj = ipaddress.ip_address(norm)
    except ValueError:
        return False
    return is_private_ip_value(ip_obj)


def _verify_dns_public(host: str, port: int) -> None:
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if is_private_ip_value(ip_obj):
            raise BookSearchSecurityError("unsafe_url", f"域名 {host} 解析到受限内网地址")


def validate_url(
    url: str,
    *,
    configured_origins: frozenset[Origin],
    resolve_dns: bool = False,
) -> str:
    """Validate URL scheme (`http`/`https`) and block private/internal targets unless explicitly configured."""
    origin = parse_origin(url)
    if origin not in configured_origins:
        if is_private_or_internal_host(origin.host):
            raise BookSearchSecurityError(
                "unsafe_url",
                f"禁止访问未显式配置的内网或本地地址: {origin.host}:{origin.port}",
            )
        if resolve_dns:
            _verify_dns_public(origin.host, origin.port)
    return url.strip()


def validate_redirect_target(
    current_url: str,
    location: str,
    *,
    configured_origins: frozenset[Origin],
    resolve_dns: bool = False,
) -> tuple[str, bool]:
    """Validate an HTTP redirect target and indicate whether origin changed (strip credentials)."""
    if not isinstance(location, str) or not location.strip():
        raise BookSearchSecurityError("unsafe_url", "重定向 Location 为空")
    resolved = urllib.parse.urljoin(current_url, location.strip())
    validate_url(resolved, configured_origins=configured_origins, resolve_dns=resolve_dns)
    cross_origin = parse_origin(current_url) != parse_origin(resolved)
    return resolved, cross_origin


def scrub_url_secrets(url: str) -> str:
    """Mask sensitive query parameters from a URL before returning or logging diagnostics."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "[REDACTED_URL]"
    if not parsed.query:
        return url
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    cleaned = [
        (k, "[REDACTED]" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
        for k, v in pairs
    ]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(cleaned), "")
    )


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Redact vaulted secrets, headers, and sensitive query strings from diagnostic text."""
    return redact_secrets(text, extra_secrets)


def sanitize_filename(name: str, *, default_stem: str = "book", ext: str = "") -> str:
    """Normalize a book title or ID into a safe single-component filename."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", (name or "").strip()).strip("._-")
    stem = (cleaned[:96] or default_stem).strip("._-") or default_stem
    if ext:
        norm_ext = ext.lower().lstrip(".")
        if not stem.lower().endswith(f".{norm_ext}"):
            return f"{stem}.{norm_ext}"
    return stem


def _reject_symlink_ancestors(root: Path, candidate: Path) -> None:
    rel = candidate.relative_to(root)
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise BookSearchSecurityError("path_safety", f"路径包含受禁止的符号链接: {part}")


def resolve_download_target(
    download_root_raw: str,
    destination: str | None,
    *,
    default_filename: str,
) -> Path:
    """Validate explicit `download_root` and contain `destination` strictly inside it."""
    root_text = (download_root_raw or "").strip()
    if not root_text:
        raise BookSearchSecurityError("not_configured", "尚未配置 download_root，无法保存下载文件")
    if "\x00" in root_text or ".." in Path(root_text).parts:
        raise BookSearchSecurityError("path_safety", "download_root 包含非法路径字符")
    root_path = Path(root_text).expanduser()
    if not root_path.is_absolute():
        raise BookSearchSecurityError("path_safety", "download_root 必须为绝对路径")
    if root_path.is_symlink():
        raise BookSearchSecurityError("path_safety", "download_root 不能是符号链接")
    if root_path.exists() and not root_path.is_dir():
        raise BookSearchSecurityError("path_safety", "download_root 必须是目录")
    root_path.mkdir(parents=True, exist_ok=True)
    root_resolved = root_path.resolve()

    if destination is None or not str(destination).strip():
        candidate = root_resolved / sanitize_filename(default_filename)
    else:
        raw_dest = str(destination).strip()
        if "\x00" in raw_dest or "\\" in raw_dest:
            raise BookSearchSecurityError("path_safety", "destination 包含非法字符")
        raw_parts = [p for p in raw_dest.split("/") if p]
        if any(p == ".." for p in raw_parts) or ".." in Path(raw_dest).parts:
            raise BookSearchSecurityError("path_safety", "destination 禁止使用 '..' 路径穿越")
        dest_obj = Path(raw_dest).expanduser()
        if dest_obj.is_absolute():
            try:
                rel = dest_obj.relative_to(root_path)
            except ValueError:
                try:
                    rel = dest_obj.relative_to(root_resolved)
                except ValueError as exc:
                    raise BookSearchSecurityError("path_safety", "destination 超出 download_root 边界") from exc
            if not rel.parts:
                raise BookSearchSecurityError("path_safety", "destination 必须指向具体文件，不能是根目录")
            candidate = root_resolved / rel
        else:
            if raw_dest in {".", "./"}:
                candidate = root_resolved / sanitize_filename(default_filename)
            elif raw_dest.endswith("/"):
                candidate = root_resolved / dest_obj / sanitize_filename(default_filename)
            else:
                candidate = root_resolved / dest_obj

    _reject_symlink_ancestors(root_resolved, candidate)
    resolved = candidate.resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise BookSearchSecurityError("path_safety", "destination 逃逸出 download_root")
    if candidate.exists() and candidate.is_dir():
        raise BookSearchSecurityError("path_safety", "destination 不能指向已存在的目录")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(root_resolved, candidate)
    return candidate


__all__ = [
    "BookSearchSecurityError",
    "Origin",
    "SUPPORTED_BOOK_FORMATS",
    "finalize_downloaded_file",
    "is_private_or_internal_host",
    "parse_origin",
    "redact_text",
    "resolve_download_target",
    "sanitize_filename",
    "scrub_url_secrets",
    "validate_downloaded_file",
    "validate_redirect_target",
    "validate_url",
]
