"""Read-only recovery of the same-user Apple Music login used by Adam/v1."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_MAX_TOKEN_BYTES = 8192


@dataclass(frozen=True, slots=True)
class LegacyCredentials:
    """Developer/user tokens recovered from local same-user application data."""

    developer_token: str = ""
    user_token: str = ""
    source: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.developer_token and self.user_token)


def _safe_read_text(path: Path) -> str:
    """Read a small regular file only when it belongs to the current user."""
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        stat = path.stat()
        if stat.st_uid != os.geteuid() or stat.st_size > _MAX_TOKEN_BYTES:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _decode_developer_token(path: Path) -> str:
    raw = _safe_read_text(path)
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    token = data.get("token") if isinstance(data, dict) else ""
    return token.strip() if isinstance(token, str) else ""


def load_legacy_credentials(data_dir: Path, home: Path | None = None) -> LegacyCredentials:
    """Recover Adam's existing login without touching SecretVault or network."""
    # The plugin-local dev_token.json is the explicit opt-in marker: tests and
    # fresh installs never have it, so this fallback cannot silently activate.
    home_dir = home or Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home_dir / ".config"))
    developer_token = _decode_developer_token(data_dir.expanduser() / "dev_token.json")
    if not developer_token:
        return LegacyCredentials()
    user_token = _safe_read_text(
        config_home / "adam" / "drm" / "files" / "MUSIC_TOKEN"
    )
    if developer_token and user_token:
        return LegacyCredentials(developer_token, user_token, "adam_local")
    return LegacyCredentials(developer_token=developer_token, source="plugin_dev_token")
