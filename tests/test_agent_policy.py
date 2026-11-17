"""Tests for side-effect classification and agent audit logging."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from omnidesk_core.agent_policy import audit_log_path, side_effect_action, write_audit
from omnidesk_core.paths import OmnideskPaths


def test_side_effect_classification_is_action_specific() -> None:
    assert side_effect_action("book.search.v1", {"action": "download"}) == "download"
    assert side_effect_action("book.search.v1", {"action": "search"}) is None
    assert side_effect_action("ai.curation.v1", {"action": "curate_interactive"}) == "curate_interactive"
    assert side_effect_action("ai.curation.v1", {"action": "status"}) is None
    assert side_effect_action("demo.echo.v1", {"action": "download"}) is None


def test_audit_log_is_private_and_redacts_secrets(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    write_audit(
        paths,
        caller="mcp:test",
        capability_id="book.search.v1",
        action="download",
        approved=True,
        reason="test download",
        result="ok",
        payload={"action": "download", "item_id": "book-1", "download_token": "secret-token"},
    )

    log_path = audit_log_path(paths)
    assert log_path.exists()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["capability"] == "book.search.v1"
    assert record["action"] == "download"
    assert record["approved"] is True
    assert record["payload"]["download_token"] == "[REDACTED]"
    assert "secret-token" not in log_path.read_text(encoding="utf-8")
