"""Side-effect classification and audit trail for agent-initiated capability calls."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ensure_private_dir
from .paths import OmnideskPaths

#: Capability actions that mutate local files, playlists, feeds, or external services.
SIDE_EFFECT_ACTIONS: dict[str, frozenset[str]] = {
    "book.search.v1": frozenset({"download", "open_in_babeldoc", "open_in_ebook", "send_to_nas"}),
    "book.bilingual_pdf.v1": frozenset({"translate", "enqueue", "run"}),
    "book.sync.v1": frozenset({"sync"}),
    "subtitle.fetch.v1": frozenset({"download"}),
    "subtitle.sync.v1": frozenset({"sync", "shift"}),
    "vocab.sm2.v1": frozenset({"add", "review", "import", "sync", "export"}),
    "media.playlist.v1": frozenset(
        {"enqueue_playback", "write_interactive_playlist", "sync_daily_authorized", "push_interactive_set"}
    ),
    "ai.curation.v1": frozenset({"curate_interactive", "run_daily_pipeline"}),
}

_SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "gateway_key",
)


def side_effect_action(capability_id: str, payload: Mapping[str, Any] | None) -> str | None:
    """Return the mutating action name when the call needs explicit confirmation."""
    actions = SIDE_EFFECT_ACTIONS.get(capability_id)
    if not actions:
        return None
    action = str((payload or {}).get("action") or "").strip()
    return action if action in actions else None


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SENSITIVE_KEY_PARTS):
                out[key_text] = "[REDACTED]"
                continue
            out[key_text] = _redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:20]]
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:500] + "...[truncated]"
    return value


def audit_log_path(paths: OmnideskPaths) -> Path:
    """Return the private JSONL audit log used for agent side effects."""
    return paths.data_dir / "audit" / "agent-actions.jsonl"


def write_audit(
    paths: OmnideskPaths,
    *,
    caller: str,
    capability_id: str,
    action: str,
    approved: bool,
    reason: str,
    result: str,
    payload: Mapping[str, Any] | None,
) -> None:
    """Append one bounded, redacted audit record with 0600 permissions."""
    path = audit_log_path(paths)
    ensure_private_dir(path.parent)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "caller": caller,
        "capability": capability_id,
        "action": action,
        "approved": bool(approved),
        "reason": reason[:500],
        "result": result[:500],
        "payload": _redact(payload or {}),
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
