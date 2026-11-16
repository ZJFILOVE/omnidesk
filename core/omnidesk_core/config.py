"""Plugin configuration storage with XDG paths, 0700/0600 permissions, atomic writes, and secret stripping."""

from __future__ import annotations

import copy
import errno
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

PLUGIN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")

DEFAULT_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "secret",
        "secrets",
        "password",
        "passwd",
        "passphrase",
        "private_key",
        "client_secret",
        "api_key",
        "api_secret",
        "api_token",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
        "gateway_key",
        "cookie_header",
        "radarr_api_key",
        "sonarr_api_key",
        "session_secret",
    }
)


class ConfigError(ValueError):
    """Raised when plugin configuration ID, path, or JSON payload is invalid."""


def resolve_xdg_app_dir(env_var: str, fallback_relative: str, app_name: str = "omnidesk") -> Path:
    """Resolve an XDG directory for OmniDesk without creating it."""
    raw = os.environ.get(env_var, "").strip()
    fallback = Path.home() / fallback_relative
    if not raw:
        return fallback / app_name
    expanded = Path(raw).expanduser()
    base = expanded if expanded.is_absolute() else fallback
    return base / app_name


def default_xdg_config_dir() -> Path:
    """Return `<XDG_CONFIG_HOME>/omnidesk` (defaults to `~/.config/omnidesk`)."""
    return resolve_xdg_app_dir("XDG_CONFIG_HOME", ".config")


def default_xdg_data_dir() -> Path:
    """Return `<XDG_DATA_HOME>/omnidesk` (defaults to `~/.local/share/omnidesk`)."""
    return resolve_xdg_app_dir("XDG_DATA_HOME", ".local/share")


def validate_plugin_id(plugin_id: str) -> str:
    """Validate a plugin ID per PLUGIN_SPEC.md (`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`, no traversal)."""
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ConfigError("plugin_id must be a non-empty string")
    if "/" in plugin_id or "\\" in plugin_id or "\x00" in plugin_id or ".." in plugin_id:
        raise ConfigError(f"Invalid plugin_id {plugin_id!r}: path traversal characters are forbidden")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise ConfigError(f"Invalid plugin_id {plugin_id!r}: must match {PLUGIN_ID_RE.pattern}")
    return plugin_id


def _is_shared_system_root(path: Path) -> bool:
    """Return True for shared roots whose permissions must never be altered."""
    resolved = path.resolve()
    protected = {Path("/"), Path("/tmp"), Path("/var/tmp"), Path.home().resolve()}
    for env_name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            protected.add(Path(raw).expanduser().resolve())
    return resolved in protected


def ensure_private_dir(path: Path) -> Path:
    """Create a directory and enforce 0700 permissions when safely owned by the current user."""
    if _is_shared_system_root(path):
        path.mkdir(parents=True, exist_ok=True)
        return path
    if path.is_symlink():
        raise OSError(errno.ELOOP, f"Refusing to use symlinked private directory: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, f"Private directory path is not a directory: {path}")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise PermissionError(f"Private directory {path} is not owned by the current user")
    os.chmod(path, 0o700)
    return path


def _fsync_dir_best_effort(directory: Path) -> None:
    """Flush directory entry metadata to disk when supported by the OS/filesystem."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dir_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def atomic_write_json(target: Path, payload: Mapping[str, Any], *, file_mode: int = 0o600) -> None:
    """Atomically serialize JSON to a temporary file (0600), fsync, and os.replace onto target."""
    if not isinstance(payload, Mapping):
        raise TypeError("JSON payload must be a mapping")
    if target.is_symlink():
        raise OSError(errno.ELOOP, f"Refusing to overwrite symlinked target: {target}")
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ensure_private_dir(target.parent)

    tmp_path: Path | None = None
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
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
        os.replace(tmp_path, target)
        _fsync_dir_best_effort(target.parent)
    except BaseException:
        try:
            if not tmp_file.closed:
                tmp_file.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def _schema_field_key_and_type(field: Any) -> tuple[str | None, str | None, Any]:
    if isinstance(field, Mapping):
        return field.get("key"), field.get("type"), field.get("default")
    return getattr(field, "key", None), getattr(field, "type", None), getattr(field, "default", None)


def extract_secret_keys(
    config_schema: Iterable[Any] | None = None,
    secret_keys: Iterable[str] | None = None,
) -> set[str]:
    """Collect field keys marked `type == 'secret'` in a config schema plus explicit keys."""
    keys: set[str] = set()
    if config_schema is not None:
        for field in config_schema:
            key, ftype, _ = _schema_field_key_and_type(field)
            if ftype == "secret" and isinstance(key, str) and key:
                keys.add(key)
    if secret_keys is not None:
        for key in secret_keys:
            if isinstance(key, str) and key:
                keys.add(key)
    return keys


def _effective_secret_rules(
    config_schema: Iterable[Any] | None = None,
    secret_keys: Iterable[str] | None = None,
    *,
    registered_plain: Iterable[str] = (),
    registered_secrets: Iterable[str] = (),
    strip_known_secrets: bool = True,
) -> tuple[set[str], set[str]]:
    schema_fields = tuple(config_schema) if config_schema is not None else None
    exact_keys = extract_secret_keys(schema_fields, secret_keys)
    exact_keys.update(k for k in registered_secrets if isinstance(k, str) and k)

    plain_keys = set(registered_plain)
    if schema_fields is not None:
        for field in schema_fields:
            key, ftype, _ = _schema_field_key_and_type(field)
            if isinstance(key, str) and key and ftype != "secret":
                plain_keys.add(key)

    known_lower = set(DEFAULT_SECRET_KEYS) - {k.lower() for k in plain_keys} if strip_known_secrets else set()
    return exact_keys, known_lower


def _effective_secret_keys(
    config_schema: Iterable[Any] | None = None,
    secret_keys: Iterable[str] | None = None,
    *,
    registered_plain: Iterable[str] = (),
    registered_secrets: Iterable[str] = (),
    strip_known_secrets: bool = True,
) -> set[str]:
    exact_keys, known_lower = _effective_secret_rules(
        config_schema,
        secret_keys,
        registered_plain=registered_plain,
        registered_secrets=registered_secrets,
        strip_known_secrets=strip_known_secrets,
    )
    return exact_keys | known_lower


def _is_secret_key(key: Any, exact_keys: set[str], known_lower: set[str]) -> bool:
    return isinstance(key, str) and (key in exact_keys or key.lower() in known_lower)


def _strip_value(
    value: Any,
    exact_keys: set[str],
    known_lower: set[str] | None = None,
    *,
    recursive: bool,
) -> Any:
    lower_set = set() if known_lower is None else known_lower
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k, exact_keys, lower_set):
                continue
            cleaned[k] = _strip_value(v, exact_keys, lower_set, recursive=recursive) if recursive else copy.deepcopy(v)
        return cleaned
    if recursive and isinstance(value, list):
        return [_strip_value(item, exact_keys, lower_set, recursive=True) for item in value]
    if recursive and isinstance(value, tuple):
        return tuple(_strip_value(item, exact_keys, lower_set, recursive=True) for item in value)
    return copy.deepcopy(value)


def strip_secrets(
    data: Mapping[str, Any],
    *,
    config_schema: Iterable[Any] | None = None,
    secret_keys: Iterable[str] | None = None,
    strip_known_secrets: bool = True,
    recursive: bool = True,
) -> dict[str, Any]:
    """Return a deep copy of `data` with all secret fields removed."""
    if not isinstance(data, Mapping):
        raise TypeError("data must be a mapping")
    exact_keys, known_lower = _effective_secret_rules(
        config_schema,
        secret_keys,
        strip_known_secrets=strip_known_secrets,
    )
    return _strip_value(data, exact_keys, known_lower, recursive=recursive)


def split_secrets(
    data: Mapping[str, Any],
    *,
    config_schema: Iterable[Any] | None = None,
    secret_keys: Iterable[str] | None = None,
    strip_known_secrets: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Split a flat config payload into `(plain_config, secret_values)`."""
    if not isinstance(data, Mapping):
        raise TypeError("data must be a mapping")
    exact_keys, known_lower = _effective_secret_rules(
        config_schema,
        secret_keys,
        strip_known_secrets=strip_known_secrets,
    )
    secrets: dict[str, str] = {}
    for key, val in data.items():
        if _is_secret_key(key, exact_keys, known_lower) and val is not None:
            secrets[key] = str(val)
    plain = _strip_value(data, exact_keys, known_lower, recursive=True)
    return plain, secrets


class ConfigStore:
    """Manages `0600` per-plugin JSON configs under `<XDG_CONFIG_HOME>/omnidesk/plugins/<id>.json`."""

    def __init__(self, config_dir: Path | str | None = None) -> None:
        self._root_dir = Path(config_dir).expanduser() if config_dir is not None else default_xdg_config_dir()
        self._plugins_dir = self._root_dir / "plugins"
        self._lock = threading.RLock()
        self._registered_secrets: dict[str, set[str]] = {}
        self._registered_plain: dict[str, set[str]] = {}

    @property
    def config_dir(self) -> Path:
        return self._root_dir

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    def _check_dirs_safe(self) -> None:
        if self._root_dir.is_symlink():
            raise ConfigError(f"Refusing symlinked config directory: {self._root_dir}")
        if self._plugins_dir.is_symlink():
            raise ConfigError(f"Refusing symlinked plugins config directory: {self._plugins_dir}")

    def path_for(self, plugin_id: str) -> Path:
        """Return the validated config file path for `plugin_id`."""
        self._check_dirs_safe()
        valid_id = validate_plugin_id(plugin_id)
        plugins_resolved = self._plugins_dir.resolve()
        candidate = self._plugins_dir / f"{valid_id}.json"
        if (plugins_resolved / f"{valid_id}.json").parent != plugins_resolved:
            raise ConfigError(f"Config path escapes plugins directory for {plugin_id!r}")
        return candidate

    def register_schema(self, plugin_id: str, config_schema: Iterable[Any]) -> None:
        """Remember secret and plain keys from a plugin manifest `config_schema`."""
        valid_id = validate_plugin_id(plugin_id)
        schema_fields = tuple(config_schema)
        secrets = extract_secret_keys(schema_fields)
        plain = {
            k
            for field in schema_fields
            for k, ftype, _ in [_schema_field_key_and_type(field)]
            if isinstance(k, str) and k and ftype != "secret"
        }
        with self._lock:
            self._registered_secrets.setdefault(valid_id, set()).update(secrets)
            plain_bucket = self._registered_plain.setdefault(valid_id, set())
            plain_bucket.update(plain)
            plain_bucket.difference_update(secrets)

    def register_secret_keys(self, plugin_id: str, secret_keys: Iterable[str]) -> None:
        """Remember explicit secret key names for `plugin_id`."""
        valid_id = validate_plugin_id(plugin_id)
        with self._lock:
            bucket = self._registered_secrets.setdefault(valid_id, set())
            plain_bucket = self._registered_plain.setdefault(valid_id, set())
            for key in secret_keys:
                if isinstance(key, str) and key:
                    bucket.add(key)
                    plain_bucket.discard(key)

    def _effective_rules_for(
        self,
        plugin_id: str,
        *,
        config_schema: Iterable[Any] | None = None,
        secret_keys: Iterable[str] | None = None,
        strip_known_secrets: bool = True,
    ) -> tuple[set[str], set[str]]:
        return _effective_secret_rules(
            config_schema,
            secret_keys,
            registered_plain=self._registered_plain.get(plugin_id, ()),
            registered_secrets=self._registered_secrets.get(plugin_id, ()),
            strip_known_secrets=strip_known_secrets,
        )

    def exists(self, plugin_id: str) -> bool:
        target = self.path_for(plugin_id)
        return target.is_file() and not target.is_symlink()

    def list_plugins(self) -> list[str]:
        """List sorted plugin IDs that currently have saved config files."""
        with self._lock:
            self._check_dirs_safe()
            if not self._plugins_dir.is_dir():
                return []
            ids: list[str] = []
            for entry in self._plugins_dir.iterdir():
                if entry.suffix == ".json" and entry.is_file() and not entry.is_symlink():
                    try:
                        ids.append(validate_plugin_id(entry.stem))
                    except ConfigError:
                        continue
            return sorted(ids)

    def read(
        self,
        plugin_id: str,
        *,
        config_schema: Iterable[Any] | None = None,
        secret_keys: Iterable[str] | None = None,
        strip_known_secrets: bool = True,
        apply_defaults: bool = False,
    ) -> dict[str, Any]:
        """Read plugin configuration, stripping any secret fields."""
        schema_fields = tuple(config_schema) if config_schema is not None else None
        with self._lock:
            target = self.path_for(plugin_id)
            data: dict[str, Any] = {}
            if target.is_symlink():
                raise ConfigError(f"Refusing to read symlinked config file: {target}")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd: int | None = None
            try:
                fd = os.open(target, flags)
            except FileNotFoundError:
                fd = None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ConfigError(f"Refusing to read symlinked config file: {target}") from None
                raise ConfigError(f"Failed to read config for {plugin_id!r}: {exc}") from None

            if fd is not None:
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        raise ConfigError(f"Config path is not a regular file: {target}")
                    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                        raise ConfigError(f"Config file {target} is not owned by the current user")
                    with os.fdopen(fd, "r", encoding="utf-8") as handle:
                        fd = None
                        loaded = json.loads(handle.read())
                except ConfigError:
                    raise
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConfigError(f"Failed to read config for {plugin_id!r}: {exc}") from None
                finally:
                    if fd is not None:
                        os.close(fd)
                if not isinstance(loaded, dict):
                    raise ConfigError(f"Config for {plugin_id!r} must be a JSON object")
                data = loaded

            exact_keys, known_lower = self._effective_rules_for(
                plugin_id,
                config_schema=schema_fields,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )
            cleaned = _strip_value(data, exact_keys, known_lower, recursive=True)

            if apply_defaults and schema_fields is not None:
                for field in schema_fields:
                    key, ftype, default_val = _schema_field_key_and_type(field)
                    if (
                        isinstance(key, str)
                        and key
                        and ftype != "secret"
                        and not _is_secret_key(key, exact_keys, known_lower)
                        and key not in cleaned
                        and default_val is not None
                    ):
                        cleaned[key] = _strip_value(default_val, exact_keys, known_lower, recursive=True)
            return cleaned

    def write(
        self,
        plugin_id: str,
        data: Mapping[str, Any],
        *,
        config_schema: Iterable[Any] | None = None,
        secret_keys: Iterable[str] | None = None,
        strip_known_secrets: bool = True,
    ) -> dict[str, Any]:
        """Atomically write plain config for `plugin_id` with `0700` dir / `0600` file permissions."""
        if not isinstance(data, Mapping):
            raise TypeError("Config data must be a mapping")
        schema_fields = tuple(config_schema) if config_schema is not None else None
        with self._lock:
            target = self.path_for(plugin_id)
            if target.is_symlink():
                raise ConfigError(f"Refusing to write symlinked config file: {target}")
            if target.exists() and not target.is_file():
                raise ConfigError(f"Config path is not a regular file: {target}")
            exact_keys, known_lower = self._effective_rules_for(
                plugin_id,
                config_schema=schema_fields,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )
            cleaned = _strip_value(data, exact_keys, known_lower, recursive=True)
            try:
                ensure_private_dir(self._root_dir)
                ensure_private_dir(self._plugins_dir)
                atomic_write_json(target, cleaned, file_mode=0o600)
            except OSError as exc:
                raise ConfigError(f"Failed to write config for {plugin_id!r}: {exc}") from None
            return cleaned

    def update(
        self,
        plugin_id: str,
        patch: Mapping[str, Any],
        *,
        config_schema: Iterable[Any] | None = None,
        secret_keys: Iterable[str] | None = None,
        strip_known_secrets: bool = True,
    ) -> dict[str, Any]:
        """Merge `patch` into existing config, strip secret fields, and atomically write."""
        if not isinstance(patch, Mapping):
            raise TypeError("Config patch must be a mapping")
        schema_fields = tuple(config_schema) if config_schema is not None else None
        with self._lock:
            current = self.read(
                plugin_id,
                config_schema=schema_fields,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )
            merged = {**current, **dict(patch)}
            return self.write(
                plugin_id,
                merged,
                config_schema=schema_fields,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )

    def delete(self, plugin_id: str) -> bool:
        """Delete the saved config file for `plugin_id`, returning True if it existed."""
        with self._lock:
            target = self.path_for(plugin_id)
            if not target.exists() and not target.is_symlink():
                return False
            target.unlink(missing_ok=True)
            return True
