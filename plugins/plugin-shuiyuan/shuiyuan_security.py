"""Security validators, HTTPS enforcement, path containment, and secret redaction."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

DEFAULT_BASE_URL = "https://shuiyuan.sjtu.edu.cn"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_COOKIE_PAIR_RE = re.compile(r"(?i)\b(_t|_forum_session|cf_clearance)\s*=\s*([^\s;,\"']+)")
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|proxy-authorization)\s*([:=])\s*([^\r\n]+)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|api_key|access_token|oauth_token|token|cookie|session)=)[^&\s#]+"
)


class ShuiyuanError(RuntimeError):
    """Structured fail-closed error carrying an OmniDesk machine-readable error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        http_status: int = 0,
        auth_required: bool = False,
        session_expired: bool = False,
        challenge_detected: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.http_status = http_status
        self.auth_required = auth_required
        self.session_expired = session_expired
        self.challenge_detected = challenge_detected
        self.details = dict(details) if details else {}
        super().__init__(message)


def has_control_chars(value: str) -> bool:
    """Return True when value contains ASCII control characters (including NUL, CR, LF)."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def url_origin(url: str) -> str:
    """Return normalized `scheme://host[:port]` origin for an HTTP/HTTPS URL."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def is_same_origin(url_a: str, url_b: str) -> bool:
    """Return True if two URLs share the exact scheme, host, and effective port."""
    try:
        pa = urllib.parse.urlsplit(url_a)
        pb = urllib.parse.urlsplit(url_b)
    except ValueError:
        return False
    if not pa.scheme or not pb.scheme or not pa.hostname or not pb.hostname:
        return False
    return url_origin(url_a) == url_origin(url_b)


def validate_https_url(
    url: Any,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
    field_name: str = "url",
) -> str:
    """Enforce `https://` URLs (allowing loopback `http://` only when explicitly opted in)."""
    if not isinstance(url, str):
        raise ShuiyuanError("invalid_argument", f"{field_name} 必须为非空 HTTPS URL 字符串")
    cleaned = url.strip()
    if not cleaned or any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in cleaned):
        raise ShuiyuanError("invalid_argument", f"{field_name} 包含非法空白或控制字符")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ShuiyuanError("invalid_argument", f"{field_name} 格式无效: {exc}") from None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise ShuiyuanError("invalid_argument", f"{field_name} 主机无效或包含内嵌凭据")
    if port is not None and not (1 <= port <= 65535):
        raise ShuiyuanError("invalid_argument", f"{field_name} 端口无效")
    if scheme == "https":
        return cleaned
    if scheme == "http" and host in LOOPBACK_HOSTS and allow_loopback_http:
        allowed = set(allowed_loopback_origins)
        if not allowed or url_origin(cleaned) in allowed:
            return cleaned
    raise ShuiyuanError("invalid_argument", f"仅允许 https:// 协议（拒绝 {scheme or 'unknown'}://）")


def normalize_base_url(
    raw_url: Any,
    *,
    allow_loopback_http: bool = False,
) -> str:
    """Normalize and validate configured Discourse base_url (defaults to Shuiyuan HTTPS)."""
    cleaned = str(raw_url or "").strip().rstrip("/")
    if not cleaned:
        return DEFAULT_BASE_URL
    validated = validate_https_url(
        cleaned,
        allow_loopback_http=allow_loopback_http,
        field_name="base_url",
    )
    return validated.rstrip("/")


def resolve_url(
    base_url: str,
    relative_url: str,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Resolve a relative or absolute URL against base_url, allowing only safe HTTP(S) targets."""
    rel = str(relative_url or "").strip()
    if not rel or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in rel):
        return ""
    joined = urllib.parse.urljoin(base_url.rstrip("/") + "/", rel)
    try:
        return validate_https_url(
            joined,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
    except ShuiyuanError:
        return ""


def _is_system_symlink_parent(path: Path) -> bool:
    return path in {Path("/tmp"), Path("/var/tmp"), Path("/var/run")}


def validate_safe_file_path(raw_path: Any, *, base_dir: Path | None = None) -> Path:
    """Validate a local file path against NUL, control chars, `..` traversal, and symlinks."""
    if not isinstance(raw_path, (str, Path)):
        raise ShuiyuanError("path_safety", "路径参数必须为字符串或 Path")
    text = os.fspath(raw_path)
    if not text or not text.strip() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ShuiyuanError("path_safety", "路径不能为空或包含控制字符")
    if ".." in text.replace("\\", "/").split("/"):
        raise ShuiyuanError("path_safety", "禁止路径穿越 ('..')")
    expanded = Path(text).expanduser()
    if ".." in expanded.parts:
        raise ShuiyuanError("path_safety", "禁止路径穿越 ('..')")
    if base_dir is not None:
        if base_dir.is_symlink():
            raise ShuiyuanError("path_safety", f"拒绝访问符号链接目录: {base_dir}")
        root = base_dir.resolve()
        candidate = expanded if expanded.is_absolute() else (root / expanded)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ShuiyuanError("path_safety", "路径越界，超出允许目录范围") from None
    else:
        if not expanded.is_absolute():
            raise ShuiyuanError("path_safety", "Cookie 文件路径必须为绝对路径")
        candidate = expanded
    if candidate.is_symlink():
        raise ShuiyuanError("path_safety", f"拒绝访问符号链接文件: {candidate}")
    for parent in candidate.parents:
        if parent == Path(parent.anchor):
            break
        if parent.is_symlink() and not _is_system_symlink_parent(parent):
            raise ShuiyuanError("path_safety", f"拒绝访问位于符号链接父目录下的路径: {parent}")
    return candidate


def safe_child_path(base_dir: Path, filename: str) -> Path:
    """Resolve a direct child filename inside `base_dir` safely without symlinks."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise ShuiyuanError("path_safety", f"非法文件名: {filename!r}")
    return validate_safe_file_path(filename, base_dir=base_dir)


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    """Atomically write a JSON object with restrictive permissions."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ShuiyuanError("path_safety", f"拒绝写入符号链接路径: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    tmp_path: Path | None = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = Path(tmp.name)
        os.fchmod(tmp.fileno(), mode)
        tmp.write(serialized)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            if not tmp.closed:
                tmp.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def redact_sensitive(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub cookies, tokens, auth headers, and sensitive query params from text."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _COOKIE_PAIR_RE.sub(r"\1=[REDACTED]", scrubbed)
    scrubbed = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _HEADER_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", scrubbed)
    return scrubbed
