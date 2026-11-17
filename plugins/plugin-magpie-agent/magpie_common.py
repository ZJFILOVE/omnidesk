"""Shared security, loopback validation, log stripping, and secret redaction helpers."""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult

DEFAULT_GATEWAY_URL = "http://127.0.0.1:3425"
DEFAULT_GROUP_ID = "omnidesk-active"
PROTECTED_DAILY_PLAYLIST_ID = "p.YJXV7dEIeb3GzZX"

_LOG_LINE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\s+plugin(?:\s+\[[^\]]+\])?:\s*")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|passphrase|gateway[_-]?key|authorization)\s*([=:])\s*([^\s,;'\"]+)"
)
_KNOWN_SECRET_PREFIX_RE = re.compile(r"\b(?:(?:sk|gh[pousr]|glpat|xox[baprs])-|github_pat_)[A-Za-z0-9_-]{8,}\b")


def strip_plugin_log_lines(text: str) -> str:
    """Remove Magpie `YYYY/MM/DD HH:MM:SS plugin: ...` preamble lines."""
    return "\n".join(line for line in text.splitlines() if not _LOG_LINE_RE.match(line)).strip()


def parse_json_output(text: str) -> Any:
    """Parse JSON from CLI stdout after stripping Magpie plugin log lines."""
    cleaned = strip_plugin_log_lines(text)
    if not cleaned:
        raise ValueError("empty JSON output")
    return json.loads(cleaned)


def is_loopback_url(url: str) -> bool:
    """Return True iff `url` is a valid HTTP(S) loopback URL without embedded credentials."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def redact_secret(text: str, secret: str | None) -> str:
    """Redact explicit secret values, Bearer tokens, and credential patterns."""
    if not text:
        return ""
    scrubbed = str(text)
    if secret:
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    scrubbed = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{4,}", r"\1[REDACTED]", scrubbed)
    scrubbed = _SECRET_KV_RE.sub(r"\1\2[REDACTED]", scrubbed)
    scrubbed = _KNOWN_SECRET_PREFIX_RE.sub("[REDACTED]", scrubbed)
    return scrubbed


def redact_data(value: Any, secret: str | None) -> Any:
    """Recursively redact sensitive strings inside structured data payloads."""
    if isinstance(value, str):
        return redact_secret(value, secret)
    if isinstance(value, list):
        return [redact_data(v, secret) for v in value]
    if isinstance(value, Mapping):
        return {k: redact_data(v, secret) for k, v in value.items()}
    return value


def fail_result(
    msg: str,
    *,
    error_code: str,
    secret: str | None,
    data: Mapping[str, Any] | None = None,
) -> CapabilityResult:
    """Build a secret-redacted failed CapabilityResult."""
    safe_msg = redact_secret(msg, secret)
    safe_data = redact_data(dict(data or {}), secret)
    return CapabilityResult.fail(safe_msg, data=safe_data, error_code=error_code)
