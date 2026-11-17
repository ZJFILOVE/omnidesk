"""Security, ID validation, HTTPS enforcement, path containment, and secret redaction."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_RE = re.compile(r"^(?:UC[A-Za-z0-9_-]{22}|@[A-Za-z0-9_.-]{3,30})$")
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
LANG_RE = re.compile(r"^[A-Za-z]{2,4}(?:-[A-Za-z0-9]{2,8}){0,3}$")

YOUTUBE_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
    }
)
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "cookie2",
        "proxy-authorization",
        "set-cookie",
        "set-cookie2",
        "x-api-key",
        "x-goog-api-key",
    }
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|api_key|access_token|oauth_token|refresh_token|token|sig|signature|cookie)=)[^&\s#]+"
)
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-goog-api-key|proxy-authorization)\s*([:=])\s*([^\r\n,;]+)"
)


class YouTubePathSafetyError(ValueError):
    """Raised when a path, cache target, or binary path violates safety constraints."""


def has_control_chars(value: str) -> bool:
    """Return True if value contains NUL or CR/LF control characters."""
    return any(ch in value for ch in ("\x00", "\r", "\n"))


def url_origin(url: str) -> str:
    """Return normalized scheme://host(:port) origin for an HTTP/HTTPS URL."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if port is None or (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80):
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


def validate_https_url(
    url: str,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Enforce HTTPS URLs (with explicit opt-in only for local loopback test origins)."""
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    cleaned = url.strip()
    if not cleaned or has_control_chars(cleaned) or any(ch.isspace() for ch in cleaned):
        raise ValueError("URL must be a non-empty string without whitespace or control characters")
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Unsafe or malformed URL host: {redact_sensitive(cleaned)}")
    if scheme == "https":
        return cleaned
    if scheme == "http" and host in LOOPBACK_HOSTS:
        origin = url_origin(cleaned)
        if allow_loopback_http or origin in set(allowed_loopback_origins):
            return cleaned
    raise ValueError(f"Only https:// URLs are permitted (got {scheme or 'unknown'}://)")


def extract_video_id(
    value: str,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> str:
    """Extract and validate an 11-character YouTube video ID from an ID or HTTPS watch URL."""
    if not isinstance(value, str):
        raise ValueError("video_id or URL must be a string")
    cleaned = value.strip()
    if not cleaned or has_control_chars(cleaned):
        raise ValueError("video_id or URL must be non-empty without control characters")
    if VIDEO_ID_RE.match(cleaned):
        return cleaned
    if "://" not in cleaned:
        raise ValueError("Invalid YouTube video_id: expected 11-character ID or https:// YouTube URL")

    safe_url = validate_https_url(
        cleaned,
        allow_loopback_http=allow_loopback_http,
        allowed_loopback_origins=allowed_loopback_origins,
    )
    parsed = urllib.parse.urlsplit(safe_url)
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS and not (
        parsed.scheme == "http" and host in LOOPBACK_HOSTS and (allow_loopback_http or url_origin(safe_url) in set(allowed_loopback_origins))
    ):
        raise ValueError(f"URL host {host!r} is not a valid YouTube domain")

    candidate = ""
    parts = [p for p in parsed.path.split("/") if p]
    if host == "youtu.be" and parts:
        candidate = parts[0]
    else:
        qs = urllib.parse.parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            candidate = qs["v"][0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
            candidate = parts[1]

    if not VIDEO_ID_RE.match(candidate):
        raise ValueError("Could not extract valid 11-character YouTube video_id")
    return candidate


def canonical_watch_url(video_id: str) -> str:
    """Return canonical HTTPS watch URL for a validated 11-character video_id."""
    vid = extract_video_id(video_id)
    return f"https://www.youtube.com/watch?v={vid}"


def hqdefault_cover_url(video_id: str) -> str:
    """Return canonical HTTPS hqdefault.jpg thumbnail URL for a validated video_id."""
    vid = extract_video_id(video_id)
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


def validate_channel_id(value: str) -> str:
    """Validate a YouTube channel ID (UC...), @handle, or HTTPS YouTube channel URL."""
    if not isinstance(value, str):
        raise ValueError("channel_id must be a string")
    cleaned = value.strip()
    if CHANNEL_ID_RE.match(cleaned):
        return cleaned
    safe_url = validate_https_url(cleaned)
    parsed = urllib.parse.urlsplit(safe_url)
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise ValueError(f"Invalid YouTube channel URL host: {host!r}")
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0].startswith("@") and CHANNEL_ID_RE.match(parts[0]):
        return parts[0]
    if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID_RE.match(parts[1]):
        return parts[1]
    raise ValueError(f"Invalid YouTube channel_id or @handle: {cleaned!r}")


def validate_playlist_id(value: str) -> str:
    """Validate a YouTube playlist ID (e.g. WL, PL...) or HTTPS playlist URL."""
    if not isinstance(value, str):
        raise ValueError("playlist_id must be a string")
    cleaned = value.strip()
    if PLAYLIST_ID_RE.match(cleaned):
        return cleaned
    safe_url = validate_https_url(cleaned)
    parsed = urllib.parse.urlsplit(safe_url)
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise ValueError(f"Invalid YouTube playlist URL host: {host!r}")
    qs = urllib.parse.parse_qs(parsed.query)
    if "list" in qs and qs["list"] and PLAYLIST_ID_RE.match(qs["list"][0]):
        return qs["list"][0]
    raise ValueError(f"Invalid YouTube playlist_id: {cleaned!r}")


def validate_lang(lang: str) -> str:
    """Validate a language tag safe for API parameters and cache filenames."""
    if not isinstance(lang, str):
        raise ValueError("language tag must be a string")
    cleaned = lang.strip()
    if not LANG_RE.match(cleaned):
        raise ValueError(f"Invalid language tag: {cleaned!r}")
    return cleaned


def resolve_executable(bin_spec: str) -> tuple[str | None, str | None]:
    """Validate and resolve a binary path without permitting traversal or control chars."""
    if not isinstance(bin_spec, str) or not bin_spec.strip():
        return None, "not_configured"
    cleaned = bin_spec.strip()
    if has_control_chars(cleaned):
        return None, "path_safety"
    raw_parts = cleaned.replace("\\", "/").split("/")
    if ".." in raw_parts:
        return None, "path_safety"
    if "/" in cleaned or "\\" in cleaned:
        target = Path(cleaned).expanduser()
        if not target.is_file():
            return None, "not_configured"
        return str(target), None
    found = shutil.which(cleaned)
    if not found:
        return None, "not_configured"
    return found, None


def default_cache_root(ctx: Any) -> Path:
    """Return safe fallback cache directory outside plugin source tree when data_dir == plugin_dir."""
    if ctx is not None:
        data_dir = getattr(ctx, "data_dir", None)
        plugin_dir = getattr(ctx, "plugin_dir", None)
        if isinstance(data_dir, Path) and isinstance(plugin_dir, Path) and data_dir.resolve() != plugin_dir.resolve():
            return data_dir / "cache"
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg_cache:
        base = Path(xdg_cache).expanduser()
        if base.is_absolute():
            return base / "omnidesk" / "plugin-youtube"
    return Path(tempfile.gettempdir()) / "omnidesk-cache" / "plugin-youtube"


def resolve_safe_cache_dir(configured: str, fallback_dir: Path) -> Path:
    """Validate configured cache_dir against traversal/symlinks and return resolved directory."""
    raw = configured.strip() if isinstance(configured, str) else ""
    if not raw:
        candidate = fallback_dir
    else:
        if has_control_chars(raw):
            raise YouTubePathSafetyError("cache_dir contains invalid control characters")
        if ".." in raw.replace("\\", "/").split("/"):
            raise YouTubePathSafetyError(f"cache_dir must not contain '..': {raw!r}")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise YouTubePathSafetyError(f"cache_dir must be an absolute path: {raw!r}")
    if candidate.is_symlink():
        raise YouTubePathSafetyError(f"Refusing symlinked cache_dir: {candidate}")
    for parent in candidate.parents:
        if parent == Path(parent.anchor):
            break
        if parent.is_symlink() and not _is_system_symlink_parent(parent):
            raise YouTubePathSafetyError(f"Refusing cache_dir under symlinked parent: {parent}")
    return candidate.resolve()


def _is_system_symlink_parent(path: Path) -> bool:
    return path in {Path("/tmp"), Path("/var/tmp"), Path("/var/run")}


def safe_cache_file(base_dir: Path, *parts: str) -> Path:
    """Resolve a cache file path strictly contained inside base_dir without symlinks."""
    if base_dir.is_symlink():
        raise YouTubePathSafetyError(f"Refusing symlinked cache root: {base_dir}")
    root = base_dir.resolve()
    current = root
    for part in parts:
        if not isinstance(part, str) or not part or has_control_chars(part):
            raise YouTubePathSafetyError(f"Unsafe cache path component: {part!r}")
        if "/" in part or "\\" in part or ".." in part or part in {".", ".."}:
            raise YouTubePathSafetyError(f"Path traversal forbidden in cache component: {part!r}")
        current = current / part
        if current.is_symlink():
            raise YouTubePathSafetyError(f"Refusing symlinked cache path: {current}")
    resolved = current.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise YouTubePathSafetyError(f"Cache path {resolved} escapes {root}") from exc
    return resolved


def redact_sensitive(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub API keys, OAuth tokens, query credentials, and cookie/auth headers from text."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    scrubbed = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _HEADER_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", scrubbed)
    return scrubbed


def sanitize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Keep only safe playback HTTP headers, stripping cookies, auth tokens, and CRLF."""
    if not isinstance(headers, Mapping):
        return {}
    cleaned: dict[str, str] = {}
    for raw_k, raw_v in headers.items():
        if not isinstance(raw_k, str) or raw_v is None:
            continue
        key = raw_k.strip()
        val = str(raw_v).strip()
        if not key or has_control_chars(key) or has_control_chars(val):
            continue
        if key.lower() in _SENSITIVE_HEADERS:
            continue
        cleaned[key] = val
    return cleaned
