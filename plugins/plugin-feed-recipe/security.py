"""URL containment, HTTPS enforcement, path traversal safety, atomic writes, and secret redaction."""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

_RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".arpa")
_MAX_URL_LENGTH = 4096

_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|api_key|apikey|access_token|auth_token|refresh_token|token|sig|signature|secret|cookie|password)=)[^&\s#]+"
)
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|cookie2|set-cookie|proxy-authorization|x-api-key|x-csrf-token)\s*([:=])\s*([^\r\n,;]+)"
)


class FeedRecipeError(RuntimeError):
    """Fail-closed domain error carrying a canonical OmniDesk error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        http_status: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.http_status = http_status
        self.details = dict(details) if details else {}
        super().__init__(message)


def has_control_chars(value: str) -> bool:
    """Return True if value contains NUL or CR/LF control characters."""
    return any(ch in value for ch in ("\x00", "\r", "\n"))


def is_valid_recipe_id(recipe_id: Any) -> bool:
    """Validate declarative recipe ID matching v1 `[A-Za-z0-9_-]{1,64}`."""
    return isinstance(recipe_id, str) and bool(_RECIPE_ID_RE.match(recipe_id))


def validate_recipe_id(recipe_id: Any, *, field_name: str = "id") -> str:
    """Return validated recipe ID or raise FeedRecipeError."""
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        raise FeedRecipeError("invalid_argument", f"缺少必填参数 {field_name}")
    cleaned = recipe_id.strip()
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned or "\x00" in cleaned:
        raise FeedRecipeError("path_safety", f"非法的 {field_name}（禁止路径穿越字符）")
    if not _RECIPE_ID_RE.match(cleaned):
        raise FeedRecipeError("invalid_argument", f"无效的配方 ID {cleaned!r}（仅允许 [A-Za-z0-9_-]{{1,64}}）")
    return cleaned


def url_origin(url: str) -> str:
    """Return canonical `<scheme>://<host>[:<port>]` origin string."""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host_fmt = f"[{host}]"
    else:
        host_fmt = host
    port = parsed.port
    default_port = 443 if scheme == "https" else 80
    if port is None or port == default_port:
        return f"{scheme}://{host_fmt}"
    return f"{scheme}://{host_fmt}:{port}"


def is_same_origin(url_a: str, url_b: str) -> bool:
    """Return True when two HTTP(S) URLs share scheme, hostname, and effective port."""
    try:
        pa = urllib.parse.urlsplit(url_a.strip())
        pb = urllib.parse.urlsplit(url_b.strip())
    except ValueError:
        return False
    if not pa.hostname or not pb.hostname or not pa.scheme or not pb.scheme:
        return False
    return url_origin(url_a) == url_origin(url_b)


def _is_private_or_internal(host: str) -> bool:
    norm = host.strip().lower().rstrip(".")
    if not norm or norm in _LOOPBACK_HOSTS or norm in {"ip6-localhost", "ip6-loopback", "metadata.google.internal"}:
        return True
    if norm.endswith(_INTERNAL_SUFFIXES):
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
    allowed_loopback_origins: Iterable[str] = (),
    field_name: str = "url",
) -> str:
    """Enforce HTTPS-only URLs (with explicit opt-in only for local loopback test origins)."""
    if not isinstance(raw_url, str):
        raise FeedRecipeError("invalid_argument", f"{field_name} 必须为非空字符串")
    cleaned = raw_url.strip()
    if not cleaned or len(cleaned) > _MAX_URL_LENGTH:
        raise FeedRecipeError("invalid_argument", f"{field_name} 为空或超出长度限制")
    if has_control_chars(cleaned) or any(ch.isspace() for ch in cleaned):
        raise FeedRecipeError("invalid_argument", f"{field_name} 包含非法空白或控制字符")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise FeedRecipeError("invalid_argument", f"{field_name} 格式无效: {exc}") from None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise FeedRecipeError("invalid_argument", f"{field_name} 缺少有效主机名或包含禁止的内嵌凭据")
    if port is not None and not (1 <= port <= 65535):
        raise FeedRecipeError("invalid_argument", f"{field_name} 端口无效")

    origin = url_origin(cleaned)
    allowed_set = set(allowed_loopback_origins)
    is_loopback = host in _LOOPBACK_HOSTS
    if scheme == "https":
        if _is_private_or_internal(host) and not (
            is_loopback and (allow_loopback_http or origin in allowed_set)
        ):
            raise FeedRecipeError("invalid_argument", f"禁止访问内网或受限主机地址: {host}")
        return cleaned
    if scheme == "http" and is_loopback and (allow_loopback_http or origin in allowed_set):
        return cleaned
    raise FeedRecipeError(
        "invalid_argument",
        f"安全限制: 仅允许 https:// 协议（收到 {scheme or 'unknown'}://）",
    )


def is_http_like_url(raw_url: Any) -> bool:
    """Return True if string starts with http:// or https:// and parses cleanly."""
    if not isinstance(raw_url, str):
        return False
    cleaned = raw_url.strip()
    if not cleaned or has_control_chars(cleaned):
        return False
    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def resolve_url(base_url: str, relative_url: str) -> str:
    """Resolve a relative URL safely against base_url (preserving valid absolute URLs)."""
    rel = (relative_url or "").strip()
    if not rel:
        return ""
    if rel.startswith(("http://", "https://")):
        return rel
    if not base_url:
        return rel
    try:
        return urllib.parse.urljoin(base_url, rel)
    except ValueError:
        return rel


def validate_safe_path(
    raw_path: Any,
    *,
    base_dir: Path | None = None,
    require_absolute: bool = False,
    check_symlinks: bool = True,
) -> Path:
    """Reject null bytes, '..' traversal, symlinks, and optional base_dir escapes."""
    if not isinstance(raw_path, (str, Path)):
        raise FeedRecipeError("path_safety", "路径参数必须为字符串或 Path")
    text = os.fspath(raw_path)
    if not text or not text.strip() or "\x00" in text:
        raise FeedRecipeError("path_safety", "路径不能为空或包含空字节")
    if ".." in text.replace("\\", "/").split("/"):
        raise FeedRecipeError("path_safety", "禁止路径穿越 ('..')")
    expanded = Path(text).expanduser()
    if ".." in expanded.parts:
        raise FeedRecipeError("path_safety", "禁止路径穿越 ('..')")
    if base_dir is not None:
        if check_symlinks and base_dir.is_symlink():
            raise FeedRecipeError("path_safety", "拒绝访问符号链接目录")
        root = base_dir.resolve()
        candidate = expanded if expanded.is_absolute() else (root / expanded)
        if check_symlinks:
            _check_relative_symlinks(root, candidate)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise FeedRecipeError("path_safety", "路径越界，超出允许目录范围") from None
        return resolved
    if require_absolute and not expanded.is_absolute():
        raise FeedRecipeError("path_safety", "配置路径必须为绝对路径")
    if check_symlinks:
        if expanded.is_symlink() or expanded.parent.is_symlink():
            raise FeedRecipeError("path_safety", "拒绝访问符号链接路径")
    return expanded


def _check_relative_symlinks(root: Path, candidate: Path) -> None:
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        # Handle non-canonical paths before resolve()
        cur = candidate
        while cur != cur.parent:
            if cur.is_symlink():
                raise FeedRecipeError("path_safety", f"拒绝访问符号链接路径: {cur.name}")
            cur = cur.parent
        return
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise FeedRecipeError("path_safety", f"拒绝访问符号链接路径: {part}")


def redact_sensitive(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub secret strings, Bearer tokens, query secrets, and Cookie/Auth headers."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _HEADER_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", scrubbed)
    return scrubbed


def atomic_write_text(target: Path, content: str, *, file_mode: int = 0o600) -> None:
    """Atomically write UTF-8 text to target with safe permissions, refusing symlinks."""
    if target.is_symlink() or target.parent.is_symlink():
        raise FeedRecipeError("path_safety", f"拒绝写入符号链接路径: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path: Path | None = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = Path(tmp.name)
        os.fchmod(tmp.fileno(), file_mode)
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, target)
        os.chmod(target, file_mode)
    except BaseException:
        try:
            if not tmp.closed:
                tmp.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


__all__ = [
    "FeedRecipeError",
    "atomic_write_text",
    "has_control_chars",
    "is_http_like_url",
    "is_same_origin",
    "is_valid_recipe_id",
    "redact_sensitive",
    "resolve_url",
    "url_origin",
    "validate_https_url",
    "validate_recipe_id",
    "validate_safe_path",
]
