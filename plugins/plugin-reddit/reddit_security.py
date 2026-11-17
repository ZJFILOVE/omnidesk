"""Security, HTTPS/SSRF enforcement (via SDK), path validation, and secret redaction for plugin-reddit."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk.http import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_REDIRECTS,
    SafeHttpError,
    is_localhost_host,
    redact_http_text,
    scrub_url_secrets,
    url_origin,
    validate_safe_url,
)

DEFAULT_REDDIT_USER_AGENT = "linux:omnidesk.plugin.reddit:v0.1.0 (by /u/omnidesk_reader)"
MAX_RESPONSE_BYTES = DEFAULT_MAX_BYTES
MAX_REDIRECTS = DEFAULT_MAX_REDIRECTS
MAX_URL_LENGTH = 4096

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
SENSITIVE_HEADERS: frozenset[str] = frozenset({"cookie", "cookie2", "authorization", "x-modhash", "proxy-authorization"})
_ALLOWED_SYSTEM_SYMLINK_PARENTS: frozenset[Path] = frozenset({Path("/var/run"), Path("/var/tmp"), Path("/tmp")})
_REDDIT_COOKIE_RE = re.compile(
    r"(?i)\b(reddit_session|token_v2|loid|edgebucket|session_tracker)=([^;\s\"'&]+)"
)
_GENERIC_UA_PREFIXES = ("curl/", "python-requests/", "python-urllib/", "wget/", "urllib/")


class RedditPluginError(RuntimeError):
    """Structured fail-closed exception carrying canonical OmniDesk error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = str(error_code or "service_offline")
        self.status_code = int(status_code)
        self.details = dict(details) if details else {}
        super().__init__(message)


def has_control_chars(value: str) -> bool:
    """Return True if string contains NUL or CR/LF characters."""
    return any(ch in value for ch in ("\x00", "\r", "\n"))


def normalize_user_agent(raw_ua: str | None) -> str:
    """Normalize User-Agent, rejecting empty or generic scraper UAs that trigger Reddit 429."""
    cleaned = str(raw_ua or "").strip()
    if not cleaned or has_control_chars(cleaned):
        return DEFAULT_REDDIT_USER_AGENT
    low = cleaned.lower()
    if low == "mozilla/5.0" or any(low.startswith(prefix) for prefix in _GENERIC_UA_PREFIXES):
        return DEFAULT_REDDIT_USER_AGENT
    return cleaned


def redact_reddit_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub Reddit session cookies, OAuth tokens, headers, and explicit secret strings."""
    if not text:
        return ""
    scrubbed = redact_http_text(text, extra_secrets)
    scrubbed = _REDDIT_COOKIE_RE.sub(r"\1=[REDACTED]", scrubbed)
    return redact_http_text(scrubbed, extra_secrets)


def origin_str(url_or_tuple: str | tuple[str, str, int]) -> str:
    """Format `(scheme, host, port)` or URL into canonical `<scheme>://<host>[:<port>]` string."""
    scheme, host, port = url_origin(url_or_tuple) if isinstance(url_or_tuple, str) else url_or_tuple
    host_fmt = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = 443 if scheme == "https" else 80
    return f"{scheme}://{host_fmt}" if port == default_port else f"{scheme}://{host_fmt}:{port}"


def validate_https_url(
    url: str,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Enforce HTTPS-only URLs (or explicit local loopback test origins) via SDK SSRF validator."""
    if not isinstance(url, str) or not url.strip():
        raise RedditPluginError("invalid_argument", "URL 不能为空")
    cleaned = url.strip()
    if len(cleaned) > MAX_URL_LENGTH:
        raise RedditPluginError("invalid_argument", "URL 超出最大长度限制")
    try:
        target = validate_safe_url(
            cleaned,
            allow_localhost_http=allow_loopback_http,
            allow_private=False,
            allow_loopback_target=allow_loopback_http,
        )
    except SafeHttpError as exc:
        if exc.error_code == "ssrf" and allow_loopback_http:
            raise RedditPluginError("ssrf", exc.message) from None
        code = "ssrf" if exc.error_code == "ssrf" else "invalid_argument"
        raise RedditPluginError(code, exc.message) from None

    allowed_set = set(allowed_loopback_origins)
    if target.scheme == "http":
        cur_origin = origin_str(target.origin)
        if not (is_localhost_host(target.host) and (allow_loopback_http or cur_origin in allowed_set)):
            raise RedditPluginError("ssrf", "仅允许 https:// 协议（http:// 仅限显式本地测试服务器）")
    elif allowed_set and target.is_localhost and origin_str(target.origin) not in allowed_set and not allow_loopback_http:
        raise RedditPluginError("ssrf", f"禁止访问未授权本地端口: {target.host}")
    return target.url


def is_reddit_domain_host(host: str) -> bool:
    """Return True if host is reddit.com, redd.it, or a subdomain thereof."""
    h = host.strip().lower().rstrip(".")
    return h == "reddit.com" or h.endswith(".reddit.com") or h == "redd.it" or h.endswith(".redd.it")


def is_same_or_trusted_reddit_host(url_a: str, url_b: str) -> bool:
    """Return True when two URLs share exact origin or are both trusted HTTPS reddit.com hosts."""
    try:
        oa = url_origin(url_a)
        ob = url_origin(url_b)
    except ValueError:
        return False
    if oa == ob:
        return True
    if oa[0] != ob[0] or oa[0] != "https":
        return False
    ha, hb = oa[1], ob[1]
    return (ha == "reddit.com" or ha.endswith(".reddit.com")) and (hb == "reddit.com" or hb.endswith(".reddit.com"))


def ensure_raw_json_param(url: str) -> str:
    """Ensure Reddit JSON endpoints include `raw_json=1` to avoid double HTML escaping."""
    cleaned = url.strip()
    if not cleaned:
        return cleaned
    parsed = urllib.parse.urlsplit(cleaned)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(k == "raw_json" for k, _ in query_pairs):
        return cleaned
    query_pairs.append(("raw_json", "1"))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query_pairs), parsed.fragment)
    )


def scrub_url_for_log(url: str) -> str:
    """Mask sensitive credentials and query parameters in URLs before diagnostics."""
    return scrub_url_secrets(url)


def validate_safe_file_path(raw_path: str, *, require_regular: bool = False) -> Path:
    """Validate user-configured file paths against null bytes, '..' traversal, and symlinks."""
    if not isinstance(raw_path, str):
        raise RedditPluginError("path_safety", "文件路径必须是字符串")
    text = raw_path.strip()
    if not text or has_control_chars(text):
        raise RedditPluginError("path_safety", "文件路径不能为空或包含控制字符")
    normalized_parts = text.replace("\\", "/").split("/")
    if ".." in normalized_parts or ".." in Path(text).parts:
        raise RedditPluginError("path_safety", "安全限制: 禁止路径穿越 ('..')")
    candidate = Path(text).expanduser()
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise RedditPluginError("path_safety", "安全限制: 拒绝访问符号链接文件")
    for parent in candidate.parents:
        if parent == Path(parent.anchor):
            break
        if parent.is_symlink() and parent not in _ALLOWED_SYSTEM_SYMLINK_PARENTS:
            raise RedditPluginError("path_safety", "安全限制: 拒绝位于符号链接父目录下的路径")
    if require_regular and candidate.exists() and not candidate.is_file():
        raise RedditPluginError("path_safety", "路径不是常规文件")
    return candidate


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, file_mode: int = 0o600) -> None:
    """Atomically write JSON with 0600 permissions, refusing symlinks."""
    if path.is_symlink() or path.parent.is_symlink():
        raise RedditPluginError("path_safety", f"拒绝写入符号链接路径: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    tmp_path: Path | None = None
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = Path(tmp_file.name)
        os.fchmod(tmp_file.fileno(), file_mode)
        tmp_file.write(serialized)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()
        os.replace(tmp_path, path)
        os.chmod(path, file_mode)
    except BaseException:
        try:
            if not tmp_file.closed:
                tmp_file.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise
