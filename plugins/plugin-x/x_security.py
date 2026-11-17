"""Security validators, HTTPS/origin guards, path containment, and secret redaction for plugin-x."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "cookie2",
        "proxy-authorization",
        "set-cookie",
        "set-cookie2",
        "x-csrf-token",
        "x-twitter-auth-type",
    }
)

_X_SECRET_RE = re.compile(
    r"(?i)\b(auth_token|ct0|kdt|att|twid|cookie_header|x-csrf-token|x-twitter-auth-type|authorization|cookie|set-cookie)\s*([:=])\s*([^\s;\r\n\"']+)"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_LIST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class XPluginError(RuntimeError):
    """Fail-closed domain/network/auth/parse error carrying a canonical OmniDesk error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        http_status: int | None = None,
        data: Mapping[str, Any] | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        safe_msg = redact_x_secrets(message, extra_secrets)
        super().__init__(safe_msg)
        self.error_code = str(error_code or "service_offline")
        self.message = safe_msg
        self.http_status = http_status
        self.data = dict(data) if data else {}


def has_control_chars(value: str) -> bool:
    """Return True if value contains ASCII control characters (including NUL, CR, LF)."""
    return bool(_CONTROL_CHARS_RE.search(value))


def redact_x_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub X session cookies (`auth_token`, `ct0`, `twid`), headers, and Bearer tokens."""
    if not text:
        return ""
    scrubbed = redact_secrets(str(text), extra_secrets)
    scrubbed = _X_SECRET_RE.sub(r"\1\2[REDACTED]", scrubbed)
    return scrubbed


def url_origin(url: str) -> tuple[str, str, int]:
    """Return normalized `(scheme, host, port)` tuple for an HTTP/HTTPS URL."""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


def origin_string(url: str) -> str:
    """Return canonical `scheme://host[:port]` origin string."""
    scheme, host, port = url_origin(url)
    default_port = 443 if scheme == "https" else 80
    host_fmt = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_fmt}" if port == default_port else f"{scheme}://{host_fmt}:{port}"


def is_same_origin(url_a: str, url_b: str) -> bool:
    """Return True when two URLs share exact `(scheme, host, port)`."""
    try:
        return url_origin(url_a) == url_origin(url_b)
    except ValueError:
        return False


def validate_https_url(
    raw_url: Any,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Enforce HTTPS URLs (with optional explicit loopback HTTP opt-in only for tests)."""
    if not isinstance(raw_url, str):
        raise XPluginError("invalid_argument", "URL 必须为字符串")
    cleaned = raw_url.strip()
    if not cleaned or len(cleaned) > 8192 or has_control_chars(cleaned) or any(ch.isspace() for ch in cleaned):
        raise XPluginError("invalid_argument", "URL 为空或包含非法空白/控制字符")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise XPluginError("invalid_argument", f"URL 格式无效: {exc}") from None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise XPluginError("invalid_argument", "URL 缺少有效主机名或包含内嵌凭据")
    if port is not None and not (1 <= port <= 65535):
        raise XPluginError("invalid_argument", "URL 端口超出有效范围")
    if scheme == "https":
        return cleaned
    if scheme == "http" and host in LOOPBACK_HOSTS:
        orig = origin_string(cleaned)
        if allow_loopback_http or orig in set(allowed_loopback_origins):
            return cleaned
    raise XPluginError("invalid_argument", f"仅允许 https:// 协议 (收到 {scheme or 'unknown'}://)")


def validate_safe_file_path(
    raw_path: Any,
    *,
    base_dir: Path | None = None,
    check_symlinks: bool = True,
) -> Path:
    """Validate path against null bytes, '..' traversal, symlinks, and optional base_dir escapes."""
    if not isinstance(raw_path, (str, Path)):
        raise XPluginError("path_safety", "路径必须为字符串或 Path")
    text = os.fspath(raw_path)
    if not text or not text.strip() or "\x00" in text or has_control_chars(text):
        raise XPluginError("path_safety", "路径不能为空或包含控制字符")
    raw_parts = text.replace("\\", "/").split("/")
    if ".." in raw_parts:
        raise XPluginError("path_safety", "禁止路径穿越 ('..')")
    expanded = Path(text).expanduser()
    if ".." in expanded.parts:
        raise XPluginError("path_safety", "禁止路径穿越 ('..')")

    if base_dir is not None:
        root = base_dir.resolve()
        candidate = expanded if expanded.is_absolute() else (root / expanded)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise XPluginError("path_safety", "路径越界，超出允许目录范围") from None
        if check_symlinks:
            cur = root
            for part in candidate.relative_to(root).parts if candidate.is_relative_to(root) else ():
                cur = cur / part
                if cur.is_symlink():
                    raise XPluginError("path_safety", f"拒绝访问符号链接路径: {part}")
        return resolved

    if check_symlinks and (expanded.is_symlink() or expanded.parent.is_symlink()):
        raise XPluginError("path_safety", "拒绝访问符号链接路径")
    return expanded.resolve()


def read_private_file_text(raw_path: str, *, max_bytes: int = MAX_RESPONSE_BYTES) -> str:
    """Safely read a regular non-symlink file up to `max_bytes`."""
    safe_path = validate_safe_file_path(raw_path, check_symlinks=True)
    if not safe_path.exists():
        raise XPluginError("not_found", f"Cookie 文件不存在: {safe_path.name}")
    if not safe_path.is_file():
        raise XPluginError("path_safety", "Cookie 路径不是常规文件")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(safe_path, flags)
    except OSError as exc:
        raise XPluginError("path_safety", f"无法安全打开 Cookie 文件: {type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise XPluginError("path_safety", "Cookie 路径不是常规文件")
        if st.st_size > max_bytes:
            raise XPluginError("validation_error", "Cookie 文件体积超出 8 MiB 安全上限")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > max_bytes:
        raise XPluginError("validation_error", "Cookie 文件体积超出 8 MiB 安全上限")
    return raw.decode("utf-8", errors="replace")


def atomic_write_json_0600(target: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write JSON with 0700 parent directory and 0600 permissions, refusing symlinks."""
    if target.is_symlink() or target.parent.is_symlink():
        raise XPluginError("path_safety", "拒绝写入符号链接路径")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
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
        os.fchmod(tmp.fileno(), 0o600)
        tmp.write(serialized)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            if not tmp.closed:
                tmp.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def safe_data_dir(ctx: Any) -> Path:
    """Resolve safe plugin data directory never overlapping plugin source directory."""
    if ctx is not None:
        data_dir = getattr(ctx, "data_dir", None)
        plugin_dir = getattr(ctx, "plugin_dir", None)
        if isinstance(data_dir, Path) and (not isinstance(plugin_dir, Path) or data_dir.resolve() != plugin_dir.resolve()):
            return data_dir
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        base = Path(xdg_data).expanduser()
        if base.is_absolute():
            return base / "omnidesk" / "plugins" / "plugin-x"
    return Path(tempfile.gettempdir()) / "omnidesk-data" / "plugin-x"


def validate_list_id(raw_id: Any) -> str:
    """Validate an X List ID against path traversal and unsafe characters."""
    if not isinstance(raw_id, str):
        raise XPluginError("invalid_argument", "list_id 必须为字符串")
    cleaned = raw_id.strip()
    if not cleaned or has_control_chars(cleaned) or not _LIST_ID_RE.match(cleaned):
        raise XPluginError("invalid_argument", f"无效的 list_id: {raw_id!r}")
    return cleaned
