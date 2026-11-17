"""Security, URL/target validation, path safety, secret redaction, and atomic writes for plugin-v2ex."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
DEFAULT_V2EX_BASE_URL = "https://www.v2ex.com"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
V2EX_HOSTS: frozenset[str] = frozenset({"v2ex.com", "www.v2ex.com", "m.v2ex.com", "fast.v2ex.com", "s.v2ex.com"})

_TOPIC_PATH_RE = re.compile(r"(?:^|/)(?:t|api/v2/topics)/(\d+)(?:[/?#]|$)")
_PURE_DIGITS_RE = re.compile(r"^(\d+)$")
_SLUG_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$")
_SENSITIVE_COOKIE_RE = re.compile(
    r"(?i)\b(A2|V2EX_TAB|PB3_SESSION|api_token|cookie_header|Authorization|Cookie)\s*([:=])\s*"
    r"""(?:"([^"\r\n]*)"|'([^'\r\n]*)'|([^\s;\r\n"']+))"""
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api_token|token|access_token|key|secret|cookie|auth)=)[^&\s#]+"
)
_SYSTEM_SYMLINK_PARENTS = frozenset({Path("/tmp"), Path("/var/tmp"), Path("/var/run")})


class V2exError(RuntimeError):
    """Fail-closed domain error carrying a canonical OmniDesk error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = str(error_code or "internal_error")
        self.message = str(message)
        self.details: dict[str, Any] = dict(details) if details else {}
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ParsedV2exTarget:
    """Validated V2EX topic target ID and canonical URLs."""

    topic_id: str
    canonical_id: str
    web_url: str
    api_v2_url: str


def has_control_chars(value: str) -> bool:
    """Return True if value contains NUL, CR, or LF."""
    return any(ch in value for ch in ("\x00", "\r", "\n"))


def url_origin(url: str) -> str:
    """Return normalized scheme://host(:port) origin for an HTTP/HTTPS URL."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if port is None or (parsed.scheme.lower() == "https" and port == 443) or (parsed.scheme.lower() == "http" and port == 80):
        return f"{parsed.scheme.lower()}://{host}"
    return f"{parsed.scheme.lower()}://{host}:{port}"


def extract_host(url: str) -> str:
    """Extract lowercase hostname from an HTTP/HTTPS URL."""
    try:
        return (urllib.parse.urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def is_same_origin_or_host(url_a: str, url_b: str) -> bool:
    """Return True if both URLs share the same scheme, host, and effective port."""
    if not url_a or not url_b:
        return False
    try:
        return url_origin(url_a) == url_origin(url_b)
    except ValueError:
        return False


def validate_https_url(
    url: str,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Enforce HTTPS-only URLs (allowing loopback HTTP only when explicitly opted in for tests)."""
    if not isinstance(url, str):
        raise V2exError("invalid_argument", "URL 必须是字符串")
    cleaned = url.strip()
    if not cleaned or has_control_chars(cleaned) or any(ch.isspace() for ch in cleaned):
        raise V2exError("invalid_argument", "URL 不能为空且不得包含空白或控制字符")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except ValueError as exc:
        raise V2exError("invalid_argument", f"URL 格式无效: {exc}") from exc

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise V2exError("invalid_argument", "URL 主机无效或包含内嵌凭据")
    if scheme == "https":
        return cleaned
    if scheme == "http" and host in LOOPBACK_HOSTS:
        origin = url_origin(cleaned)
        if allow_loopback_http or origin in set(allowed_loopback_origins):
            return cleaned
    raise V2exError("invalid_argument", f"仅允许 https:// 协议（拒绝 {scheme or 'unknown'}://）")


def _redact_cookie_kv(match: re.Match[str]) -> str:
    key, sep, dq, sq, _ = match.groups()
    if dq is not None:
        return f'{key}{sep}"[REDACTED]"'
    if sq is not None:
        return f"{key}{sep}'[REDACTED]'"
    return f"{key}{sep}[REDACTED]"


def redact_sensitive(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub V2EX session cookies, Bearer tokens, and explicit secrets from text."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _SENSITIVE_COOKIE_RE.sub(_redact_cookie_kv, scrubbed)
    scrubbed = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", scrubbed)
    return redact_secrets(scrubbed, extra_secrets)


def validate_safe_file_path(raw_path: str, *, must_exist: bool = False) -> Path:
    """Validate an explicit local file path against traversal and symlinks."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise V2exError("invalid_argument", "文件路径不能为空")
    cleaned = raw_path.strip()
    if has_control_chars(cleaned):
        raise V2exError("path_safety", "文件路径包含非法控制字符")
    if ".." in cleaned.replace("\\", "/").split("/"):
        raise V2exError("path_safety", "文件路径禁止包含 '..' 路径穿越")
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        raise V2exError("path_safety", "文件路径必须为绝对路径")
    if candidate.is_symlink():
        raise V2exError("path_safety", f"拒绝读取符号链接文件: {candidate.name}")
    for parent in candidate.parents:
        if parent == Path(parent.anchor):
            break
        if parent.is_symlink() and parent not in _SYSTEM_SYMLINK_PARENTS:
            raise V2exError("path_safety", f"拒绝访问包含符号链接的父目录: {parent.name}")
    if must_exist:
        if not candidate.exists():
            raise V2exError("not_found", f"文件不存在: {candidate.name}")
        if not candidate.is_file():
            raise V2exError("path_safety", f"目标路径不是普通文件: {candidate.name}")
    return candidate.resolve()


def sanitize_node_slug(raw_slug: str) -> str:
    """Normalize and validate a V2EX node slug (rejecting traversal/invalid syntax)."""
    if not isinstance(raw_slug, str):
        return ""
    work = raw_slug.strip()
    if not work or has_control_chars(work):
        return ""
    if ".." in work or "\\" in work:
        return ""
    if work.startswith("node:"):
        work = work[5:].strip()
    elif work.startswith("/go/"):
        work = work[4:].strip()
    elif work.startswith("go/"):
        work = work[3:].strip()
    if not work or "/" in work or not _SLUG_ALLOWED_RE.match(work):
        return ""
    return work.lower()


def _is_allowed_v2ex_host(host: str, *, base_url: str, allow_loopback_http: bool, allowed_loopback_origins: Iterable[str]) -> bool:
    if host in V2EX_HOSTS or host.endswith(".v2ex.com"):
        return True
    base_host = extract_host(base_url)
    if base_host and host == base_host:
        return True
    if allow_loopback_http and host in LOOPBACK_HOSTS:
        return True
    return any(extract_host(orig) == host for orig in allowed_loopback_origins)


def parse_topic_id_or_url(
    id_or_url: str,
    *,
    base_url: str = DEFAULT_V2EX_BASE_URL,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> ParsedV2exTarget:
    """Extract and validate a V2EX topic ID from numeric ID, v2ex:<id>, /t/<id>, or HTTPS URL."""
    if not isinstance(id_or_url, str):
        raise V2exError("invalid_argument", "topic id 或 url 必须是字符串")
    work = id_or_url.strip()
    if not work or has_control_chars(work):
        raise V2exError("invalid_argument", "topic id 或 url 不能为空且不得包含控制字符")
    if ".." in work or "\\" in work:
        raise V2exError("invalid_argument", "无效的 topic id 或 url")

    if work.startswith("v2ex:"):
        work = work[5:].strip()

    topic_id = ""
    if "://" in work:
        safe_url = validate_https_url(
            work,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        parsed = urllib.parse.urlsplit(safe_url)
        host = (parsed.hostname or "").lower()
        if not _is_allowed_v2ex_host(
            host,
            base_url=base_url,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        ):
            raise V2exError("invalid_argument", f"URL 主机 {host!r} 不是有效的 V2EX 域名")
        m = _TOPIC_PATH_RE.search(parsed.path)
        if m:
            topic_id = m.group(1)
    elif m_path := _TOPIC_PATH_RE.search(work):
        topic_id = m_path.group(1)
    elif m_digits := _PURE_DIGITS_RE.match(work):
        topic_id = m_digits.group(1)

    if not topic_id:
        raise V2exError("invalid_argument", f"无效的 V2EX topic ID 或 URL: {work!r}")

    clean_base = base_url.rstrip("/")
    return ParsedV2exTarget(
        topic_id=topic_id,
        canonical_id=topic_id,
        web_url=f"{clean_base}/t/{topic_id}",
        api_v2_url=f"{clean_base}/api/v2/topics/{topic_id}",
    )


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, file_mode: int = 0o600) -> Path:
    """Atomically write JSON with 0600 permissions, refusing symlinks."""
    if path.is_symlink() or path.parent.is_symlink():
        raise V2exError("path_safety", f"拒绝写入符号链接路径: {path.name}")
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
        os.fchmod(tmp.fileno(), file_mode)
        tmp.write(serialized)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, path)
        os.chmod(path, file_mode)
    except BaseException:
        if not tmp.closed:
            tmp.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    return path
