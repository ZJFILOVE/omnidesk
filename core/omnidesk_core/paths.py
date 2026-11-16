"""XDG Base Directory path resolution and safe workspace path helpers."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import PathSafetyError

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


def validate_plugin_id(plugin_id: str) -> str:
    """Validate a plugin ID against manifest safety rules (no traversal or separators)."""
    if not isinstance(plugin_id, str) or not plugin_id:
        raise PathSafetyError("Plugin ID must be a non-empty string")
    if "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id or "\x00" in plugin_id:
        raise PathSafetyError(f"Unsafe plugin ID {plugin_id!r}: path traversal characters forbidden")
    if not _PLUGIN_ID_RE.match(plugin_id):
        raise PathSafetyError(
            f"Invalid plugin ID {plugin_id!r}: must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,95}}$"
        )
    return plugin_id


def safe_child_path(base_dir: Path, *parts: str) -> Path:
    """Resolve child path under base_dir and reject any escape outside base_dir."""
    root = base_dir.resolve()
    candidate = root
    for part in parts:
        if not isinstance(part, str) or not part or part in {".", ".."} or "\x00" in part:
            raise PathSafetyError(f"Unsafe path component {part!r}")
        p = Path(part)
        if p.is_absolute():
            raise PathSafetyError(f"Absolute path component {part!r} not allowed")
        candidate = candidate / p
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"Path {resolved} escapes base directory {root}") from exc
    return resolved


def _resolve_xdg_base(
    env: Mapping[str, str],
    var_name: str,
    fallback: Path,
) -> Path:
    """Read an XDG environment variable per spec, ignoring empty or relative values."""
    raw = env.get(var_name, "").strip()
    if not raw:
        return fallback
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return fallback
    return expanded


@dataclass(frozen=True, slots=True)
class OmnideskPaths:
    """Resolved XDG directory layout for an OmniDesk runtime instance."""

    app_name: str
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path
    staging_dir: Path

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        app_name: str = "omnidesk",
        home: Path | None = None,
    ) -> OmnideskPaths:
        """Construct XDG paths from environment variables with standard fallbacks."""
        if (
            not isinstance(app_name, str)
            or not app_name
            or app_name in {".", ".."}
            or any(ch in app_name for ch in ("/", "\\", "\x00", "\n", "\r"))
            or ".." in app_name
        ):
            raise PathSafetyError(f"Unsafe app_name {app_name!r}")
        environ = os.environ if env is None else env
        if home is not None:
            home_dir = home.expanduser()
        elif environ.get("HOME", "").strip():
            home_dir = Path(environ["HOME"]).expanduser()
        else:
            home_dir = Path.home()

        xdg_config = _resolve_xdg_base(environ, "XDG_CONFIG_HOME", home_dir / ".config")
        xdg_data = _resolve_xdg_base(environ, "XDG_DATA_HOME", home_dir / ".local" / "share")
        xdg_state = _resolve_xdg_base(environ, "XDG_STATE_HOME", home_dir / ".local" / "state")
        xdg_cache = _resolve_xdg_base(environ, "XDG_CACHE_HOME", home_dir / ".cache")

        config_dir = xdg_config / app_name
        data_dir = xdg_data / app_name
        state_dir = xdg_state / app_name
        cache_dir = xdg_cache / app_name

        runtime_raw = environ.get("XDG_RUNTIME_DIR", "").strip()
        if runtime_raw:
            runtime_base = Path(runtime_raw).expanduser()
            runtime_dir = (
                runtime_base / app_name if runtime_base.is_absolute() else data_dir / "run"
            )
        else:
            runtime_dir = data_dir / "run"

        staging_dir = Path(tempfile.gettempdir()) / f"{app_name}-staging"
        return cls(
            app_name=app_name,
            config_dir=config_dir,
            data_dir=data_dir,
            state_dir=state_dir,
            cache_dir=cache_dir,
            runtime_dir=runtime_dir,
            staging_dir=staging_dir,
        )

    @property
    def plugins_config_dir(self) -> Path:
        return self.config_dir / "plugins"

    @property
    def vault_path(self) -> Path:
        return self.config_dir / "vault.json"

    @property
    def plugins_dir(self) -> Path:
        return self.data_dir / "plugins"

    @property
    def plugin_data_root(self) -> Path:
        return self.data_dir / "plugin-data"

    @property
    def upstreams_lock_path(self) -> Path:
        return self.data_dir / "upstreams.lock.json"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def curation_dir(self) -> Path:
        return self.data_dir / "curation"

    @property
    def curation_repos_dir(self) -> Path:
        return self.curation_dir / "repos"

    @property
    def curation_daily_dir(self) -> Path:
        return self.curation_dir / "daily"

    @property
    def curation_interactive_dir(self) -> Path:
        return self.curation_dir / "interactive"

    @property
    def chrome_profile_dir(self) -> Path:
        return self.data_dir / "chrome-profile"

    @property
    def search_db_path(self) -> Path:
        return self.data_dir / "search.db"

    @property
    def control_socket_path(self) -> Path:
        return self.runtime_dir / "control.sock"

    def plugin_config_path(self, plugin_id: str) -> Path:
        pid = validate_plugin_id(plugin_id)
        return self.plugins_config_dir / f"{pid}.json"

    def plugin_data_dir(self, plugin_id: str) -> Path:
        pid = validate_plugin_id(plugin_id)
        return self.plugin_data_root / pid

    def plugin_backup_dir(self, plugin_id: str) -> Path:
        pid = validate_plugin_id(plugin_id)
        return self.backups_dir / pid

    def ensure_dirs(self, *, mode: int = 0o700) -> OmnideskPaths:
        """Create core XDG directories with private directory permissions."""
        for directory in (
            self.config_dir,
            self.plugins_config_dir,
            self.data_dir,
            self.plugins_dir,
            self.plugin_data_root,
            self.backups_dir,
            self.curation_dir,
            self.curation_repos_dir,
            self.curation_daily_dir,
            self.curation_interactive_dir,
            self.state_dir,
            self.cache_dir,
            self.runtime_dir,
        ):
            if directory.is_symlink():
                raise PathSafetyError(f"Refusing to use symlinked XDG directory: {directory}")
            directory.mkdir(parents=True, exist_ok=True, mode=mode)
            st = directory.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise PathSafetyError(f"XDG path is not a directory: {directory}")
            if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                raise PathSafetyError(f"XDG directory {directory} is not owned by the current user")
            os.chmod(directory, mode)
        return self


def get_paths(
    env: Mapping[str, str] | None = None,
    *,
    app_name: str = "omnidesk",
    home: Path | None = None,
) -> OmnideskPaths:
    """Convenience wrapper returning resolved Omnidesk XDG paths."""
    return OmnideskPaths.from_env(env, app_name=app_name, home=home)
