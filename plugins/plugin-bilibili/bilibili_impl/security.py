"""Security validators, allowlists, cookie normalization, and secret redaction."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

DEFAULT_API_BASE = "https://api.bilibili.com"
DEFAULT_PASSPORT_BASE = "https://passport.bilibili.com"
DEFAULT_LIVE_BASE = "https://api.live.bilibili.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ALLOWED_PRIMARY_DOMAINS = ("bilibili.com", "b23.tv")
_ALLOWED_CDN_DOMAINS = ("bilibili.com", "b23.tv", "hdslb.com", "bilivideo.com", "bilivideo.cn")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_BILI_SECRET_RE = re.compile(
    r"(?i)\b(SESSDATA|bili_jct|DedeUserID(?:__ckMd5)?|refresh_token|buvid[34]|sid)\s*([=:])\s*([^\s,;'\"]+)"
)
_PROXY_AUTH_RE = re.compile(r"(https?|socks5h?)://([^:/@\s]+):([^/@\s]+)@")


class SecurityValidationError(ValueError):
    """Raised when URL, proxy, path, or XML input violates safety rules."""

    def __init__(self, message: str, *, error_code: str = "invalid_argument") -> None:
        super().__init__(message)
        self.error_code = error_code


def _host_matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    clean = host.rstrip(".").lower()
    return any(clean == dom or clean.endswith(f".{dom}") for dom in domains)


def validate_no_traversal(value: str, *, field_name: str = "input") -> str:
    """Reject null bytes, control chars, backslashes, and `..` traversal segments."""
    if not isinstance(value, str):
        raise SecurityValidationError(f"{field_name} must be a string", error_code="invalid_argument")
    cleaned = value.strip()
    decoded = urllib.parse.unquote(cleaned)
    for candidate in (cleaned, decoded):
        if "\x00" in candidate or "\r" in candidate or "\n" in candidate or "\\" in candidate:
            raise SecurityValidationError(f"Unsafe characters in {field_name}", error_code="path_safety")
        parsed_path = urllib.parse.urlsplit(candidate).path
        segments = [seg for seg in parsed_path.replace("\\", "/").split("/") if seg]
        if ".." in segments:
            raise SecurityValidationError(f"Unsafe path traversal in {field_name}", error_code="path_safety")
    return cleaned


def origin_key(url: str) -> str:
    """Return canonical `scheme://host:port` key for same-origin matching."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme.lower()}://{host}:{port}"


def is_loopback_host(host: str) -> bool:
    """Return True only for literal loopback hostnames/addresses."""
    clean = host.strip("[]").lower()
    if clean in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def validate_url(
    raw_url: str,
    *,
    allow_cdn: bool = False,
    allow_loopback: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Validate URL scheme, traversal safety, and host allowlist (bilibili.com/b23.tv)."""
    cleaned = validate_no_traversal(raw_url, field_name="url")
    if cleaned.startswith("//"):
        cleaned = f"https:{cleaned}"
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise SecurityValidationError(f"Unsupported or invalid URL scheme: {scheme!r}", error_code="invalid_argument")

    host = parsed.hostname.lower()
    domains = _ALLOWED_CDN_DOMAINS if allow_cdn else _ALLOWED_PRIMARY_DOMAINS
    if _host_matches_domain(host, domains):
        if scheme != "https":
            raise SecurityValidationError("Bilibili URLs must use https://", error_code="invalid_argument")
        return urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))

    if is_loopback_host(host):
        origin = origin_key(cleaned)
        if allow_loopback or origin in set(allowed_loopback_origins):
            return urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))
    raise SecurityValidationError(
        f"URL host {host!r} is outside bilibili.com/b23.tv allowlist",
        error_code="invalid_argument",
    )


def validate_api_base(raw_base: str) -> str:
    """Validate configured `api_base`: bilibili.com/b23.tv or explicit loopback mock server."""
    cleaned = (raw_base or DEFAULT_API_BASE).strip()
    if not cleaned:
        return DEFAULT_API_BASE
    return validate_url(cleaned, allow_cdn=False, allow_loopback=True).rstrip("/")


def validate_proxy_url(raw_proxy: str) -> str:
    """Validate optional user-configured proxy URL without leaking credentials."""
    cleaned = (raw_proxy or "").strip()
    if not cleaned:
        return ""
    if any(ch in cleaned for ch in ("\x00", "\r", "\n", " ", "\t")):
        raise SecurityValidationError("Invalid whitespace or control character in proxy URL", error_code="invalid_argument")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise SecurityValidationError("Unsupported proxy URL scheme or missing host", error_code="invalid_argument")
    return cleaned


def validate_ytdlp_bin(raw_bin: str) -> str:
    """Validate `ytdlp_bin` path against path traversal and shell metacharacters."""
    cleaned = (raw_bin or "").strip()
    if not cleaned:
        return ""
    if any(ch in cleaned for ch in ("\x00", "\r", "\n", ";", "&", "|", "`", "$")):
        raise SecurityValidationError("Unsafe character in ytdlp_bin", error_code="path_safety")
    if ".." in Path(cleaned).parts:
        raise SecurityValidationError("Path traversal forbidden in ytdlp_bin", error_code="path_safety")
    return cleaned


def redact_bilibili_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub cookies, tokens, proxy credentials, and vault secrets from diagnostic strings."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _BILI_SECRET_RE.sub(r"\1\2[REDACTED]", scrubbed)
    return _PROXY_AUTH_RE.sub(r"\1://[REDACTED]@", scrubbed)


def mask_cookie(cookie: str) -> str:
    """Return a non-reversible masked representation of a Bilibili cookie string."""
    if not cookie or not cookie.strip():
        return ""
    parts: list[str] = []
    for raw_pair in cookie.split(";"):
        item = raw_pair.strip()
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        masked_v = "***" if len(v) <= 6 else f"{v[:3]}***{v[-2:]}"
        parts.append(f"{k}={masked_v}")
    return "; ".join(parts) if parts else "***"


def format_cookie_header(raw: Any) -> str:
    """Normalize string/dict/list cookies into a single safe Cookie header string."""
    if raw is None:
        return ""
    pairs: list[str] = []
    if isinstance(raw, str):
        for seg in raw.split(";"):
            part = seg.strip()
            if "=" in part and "\r" not in part and "\n" not in part and "\x00" not in part:
                k, v = part.split("=", 1)
                if k.strip() and v.strip():
                    pairs.append(f"{k.strip()}={v.strip()}")
    elif isinstance(raw, Mapping):
        for k, v in raw.items():
            ks, vs = str(k).strip(), str(v).strip()
            if ks and vs and "\r" not in ks + vs and "\n" not in ks + vs and "\x00" not in ks + vs:
                pairs.append(f"{ks}={vs}")
    elif isinstance(raw, Sequence):
        for entry in raw:
            if isinstance(entry, Mapping):
                name = str(entry.get("name", "")).strip()
                val = str(entry.get("value", "")).strip()
                if name and val and "\r" not in name + val and "\n" not in name + val and "\x00" not in name + val:
                    pairs.append(f"{name}={val}")
    return "; ".join(pairs)


def cookie_value(cookie_header: str, name: str) -> str:
    """Extract a named cookie value from a normalized Cookie header string."""
    target = name.lower()
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k.strip().lower() == target:
                return v.strip()
    return ""


def to_netscape_cookie_txt(cookie_header: str) -> str:
    """Convert a Cookie header string to temporary Netscape cookiejar text for yt-dlp."""
    lines = ["# Netscape HTTP Cookie File"]
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or "\t" in k or "\t" in v or "\n" in k + v:
            continue
        lines.append(f".bilibili.com\tTRUE\t/\tTRUE\t0\t{k}\t{v}")
    return "\n".join(lines) + "\n"
