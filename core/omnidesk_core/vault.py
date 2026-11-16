"""0600-protected SecretVault storage with per-plugin isolation and zero-leak logging."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import threading
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import atomic_write_json, ensure_private_dir
from .errors import ErrorCode, InvalidArgumentError, SecretVaultError, redact_secrets
from .paths import OmnideskPaths, validate_plugin_id

logger = logging.getLogger(__name__)

VAULT_SCHEMA_VERSION = 1
_GLOBAL_SCOPE = "__global__"
_UNSET: Any = object()


def _validate_secret_key(key: Any) -> str:
    """Validate that a secret key name is a non-empty string without null bytes."""
    if not isinstance(key, str):
        raise InvalidArgumentError(f"Secret key must be a string, got {type(key).__name__}")
    cleaned = key.strip()
    if not cleaned or "\x00" in key:
        raise InvalidArgumentError("Secret key must be a non-empty string without null bytes")
    if cleaned != key:
        raise InvalidArgumentError("Secret key must not have leading or trailing whitespace")
    return key


def _resolve_vault_path(
    vault_path: Path | str | None = None,
    *,
    paths: OmnideskPaths | None = None,
    config_dir: Path | str | None = None,
) -> Path:
    """Resolve the target vault.json path from explicit arguments or XDG defaults."""
    if vault_path is not None:
        candidate = Path(vault_path).expanduser()
        if candidate.is_symlink():
            return candidate
        return candidate / "vault.json" if candidate.exists() and candidate.is_dir() else candidate
    if config_dir is not None:
        return Path(config_dir).expanduser() / "vault.json"
    return (paths if paths is not None else OmnideskPaths.from_env()).vault_path


def _resolve_scope_and_key(plugin_id: str | None, key: Any) -> tuple[str, str]:
    """Resolve `(plugin_id, key)` or flat `key` / `'plugin_id:key'` arguments."""
    if key is _UNSET or plugin_id is None:
        raw_key = _validate_secret_key(plugin_id if key is _UNSET else key)
        if ":" in raw_key:
            scope_part, key_part = raw_key.split(":", 1)
            return validate_plugin_id(scope_part), _validate_secret_key(key_part)
        return _GLOBAL_SCOPE, raw_key
    return validate_plugin_id(plugin_id), _validate_secret_key(key)


def _vault_err(message: str, *, code: ErrorCode, extra: tuple[str, ...] = ()) -> SecretVaultError:
    return SecretVaultError(redact_secrets(message, extra), code=code)


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs known secret patterns and vaulted values."""

    def __init__(self, vault: SecretVault | None = None, name: str = "") -> None:
        super().__init__(name)
        self._vault = vault

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            extra = self._vault.secret_values() if self._vault is not None else ()
        except Exception:
            extra = ()
        if record.args:
            try:
                rendered = record.getMessage()
            except Exception:
                rendered = f"{record.msg!s} {record.args!r}"
            scrubbed = redact_secrets(rendered, extra)
            if scrubbed != rendered:
                record.msg = scrubbed
                record.args = ()
            elif isinstance(record.msg, str):
                record.msg = redact_secrets(record.msg, extra)
        elif isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg, extra)
        elif record.msg is not None:
            raw_msg = str(record.msg)
            scrubbed_msg = redact_secrets(raw_msg, extra)
            if scrubbed_msg != raw_msg:
                record.msg = scrubbed_msg
        if record.exc_info and not record.exc_text:
            try:
                formatted_exc = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
                scrubbed_exc = redact_secrets(formatted_exc, extra)
                if scrubbed_exc != formatted_exc:
                    record.exc_text = scrubbed_exc
            except Exception:
                pass
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, extra)
        return True


class SecretVault:
    """Manages `0600` isolated secret storage at `<XDG_CONFIG_HOME>/omnidesk/vault.json`."""

    def __init__(
        self,
        vault_path: Path | str | None = None,
        *,
        paths: OmnideskPaths | None = None,
        config_dir: Path | str | None = None,
        strict_permissions: bool = False,
    ) -> None:
        self._vault_path = _resolve_vault_path(vault_path, paths=paths, config_dir=config_dir)
        self._strict_permissions = strict_permissions
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"SecretVault(vault_path={str(self._vault_path)!r})"

    __str__ = __repr__

    @property
    def path(self) -> Path:
        return self._vault_path

    @property
    def vault_path(self) -> Path:
        return self._vault_path

    def _check_paths_unlocked(self) -> Path:
        target = self._vault_path
        if target.parent.is_symlink():
            raise _vault_err(f"Refusing to access secret vault under symlinked directory: {target.parent}", code=ErrorCode.PATH_SAFETY)
        if target.is_symlink():
            raise _vault_err(f"Refusing to access symlinked secret vault: {target}", code=ErrorCode.PATH_SAFETY)
        return target

    def _enforce_stat_unlocked(self, target: Path, st: os.stat_result, *, fd: int | None = None) -> None:
        if not stat.S_ISREG(st.st_mode):
            raise _vault_err(f"Secret vault path is not a regular file: {target}", code=ErrorCode.PATH_SAFETY)
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise _vault_err(f"Secret vault {target} is not owned by the current user", code=ErrorCode.PERMISSION_DENIED)
        mode = stat.S_IMODE(st.st_mode)
        if (mode & 0o077) == 0:
            return
        if self._strict_permissions:
            raise _vault_err(f"Secret vault {target} has insecure permissions {oct(mode)}; expected 0o600", code=ErrorCode.PERMISSION_DENIED)
        try:
            os.fchmod(fd, 0o600) if fd is not None else os.chmod(target, 0o600)
        except OSError as exc:
            raise _vault_err(f"Failed to enforce 0600 permissions on secret vault {target}: {type(exc).__name__}", code=ErrorCode.PERMISSION_DENIED) from None

    def _verify_target_safety_unlocked(self) -> None:
        target = self._check_paths_unlocked()
        if target.exists():
            self._enforce_stat_unlocked(target, target.lstat())

    def _read_buckets_unlocked(self) -> dict[str, dict[str, str]]:
        target = self._check_paths_unlocked()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd: int | None = None
        try:
            fd = os.open(target, flags)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            code = ErrorCode.PATH_SAFETY if exc.errno == errno.ELOOP else ErrorCode.PERMISSION_DENIED
            raise _vault_err(f"Failed to open secret vault {target}: {type(exc).__name__}", code=code) from None

        try:
            self._enforce_stat_unlocked(target, os.fstat(fd), fd=fd)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = None
                raw_text = handle.read()
        except SecretVaultError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise _vault_err(f"Failed to read secret vault {target}: {type(exc).__name__}", code=ErrorCode.PERMISSION_DENIED) from None
        finally:
            if fd is not None:
                os.close(fd)

        if not raw_text.strip():
            return {}
        try:
            loaded = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise _vault_err(
                f"Corrupted secret vault JSON at {target}: line {exc.lineno} col {exc.colno}",
                code=ErrorCode.VALIDATION_ERROR,
            ) from None
        return self._normalize_loaded_payload(loaded)

    def _normalize_loaded_payload(self, loaded: Any) -> dict[str, dict[str, str]]:
        if not isinstance(loaded, dict):
            raise _vault_err("Secret vault root payload must be a JSON object", code=ErrorCode.VALIDATION_ERROR)

        if "secrets" in loaded:
            raw_secrets = loaded["secrets"]
            if not isinstance(raw_secrets, dict):
                raise _vault_err("Secret vault 'secrets' entry must be a JSON object", code=ErrorCode.VALIDATION_ERROR)
        else:
            raw_secrets = {k: v for k, v in loaded.items() if k != "version"}

        buckets: dict[str, dict[str, str]] = {}
        for scope, entry in raw_secrets.items():
            if not isinstance(scope, str) or not scope:
                raise _vault_err("Secret vault scope key must be a non-empty string", code=ErrorCode.VALIDATION_ERROR)
            if isinstance(entry, str):
                buckets.setdefault(_GLOBAL_SCOPE, {})[_validate_secret_key(scope)] = entry
            elif isinstance(entry, dict):
                clean_scope = _GLOBAL_SCOPE if scope == _GLOBAL_SCOPE else validate_plugin_id(scope)
                bucket = buckets.setdefault(clean_scope, {})
                for k, v in entry.items():
                    if not isinstance(v, str):
                        raise _vault_err(
                            f"Secret value for scope {clean_scope!r} must be a string",
                            code=ErrorCode.VALIDATION_ERROR,
                        )
                    bucket[_validate_secret_key(k)] = v
            else:
                raise _vault_err(
                    f"Secret vault entry for {scope!r} must be a mapping or string",
                    code=ErrorCode.VALIDATION_ERROR,
                )
        return buckets

    def _write_buckets_unlocked(self, buckets: dict[str, dict[str, str]]) -> None:
        self._verify_target_safety_unlocked()
        cleaned = {
            scope: dict(sorted(entries.items()))
            for scope, entries in sorted(buckets.items())
            if entries
        }
        payload = {"version": VAULT_SCHEMA_VERSION, "secrets": cleaned}
        try:
            ensure_private_dir(self._vault_path.parent)
            atomic_write_json(self._vault_path, payload, file_mode=0o600)
        except OSError as exc:
            code = ErrorCode.PATH_SAFETY if exc.errno == errno.ELOOP else ErrorCode.PERMISSION_DENIED
            raise _vault_err(
                f"Failed to write secret vault {self._vault_path}: {type(exc).__name__}",
                code=code,
            ) from None

    def set(self, plugin_id: str | None = None, key: str | None = None, value: Any = _UNSET) -> None:
        """Store or overwrite a secret value with atomic 0600 persistence."""
        if value is _UNSET:
            scope, secret_key = _resolve_scope_and_key(plugin_id, _UNSET)
            raw_value = key
        else:
            scope, secret_key = _resolve_scope_and_key(plugin_id, key)
            raw_value = value

        if not isinstance(raw_value, str):
            raise InvalidArgumentError(f"Secret value must be a string, got {type(raw_value).__name__}")

        with self._lock:
            buckets = self._read_buckets_unlocked()
            buckets.setdefault(scope, {})[secret_key] = raw_value
            self._write_buckets_unlocked(buckets)
            logger.debug("SecretVault stored key=%r scope=%r", secret_key, scope)

    def set_many(self, plugin_id: str, secrets: Mapping[str, str]) -> list[str]:
        """Atomically store multiple secret keys for a plugin in one vault write."""
        valid_id = validate_plugin_id(plugin_id)
        if not isinstance(secrets, Mapping):
            raise InvalidArgumentError("secrets must be a mapping")
        validated: dict[str, str] = {}
        for k, v in secrets.items():
            clean_key = _validate_secret_key(k)
            if not isinstance(v, str):
                raise InvalidArgumentError(f"Secret value for {clean_key!r} must be a string, got {type(v).__name__}")
            validated[clean_key] = v
        if not validated:
            return []

        with self._lock:
            buckets = self._read_buckets_unlocked()
            buckets.setdefault(valid_id, {}).update(validated)
            self._write_buckets_unlocked(buckets)
            logger.debug("SecretVault stored %d keys for scope=%r", len(validated), valid_id)
            return sorted(validated)

    def get(
        self,
        plugin_id: str | None = None,
        key: str | None = _UNSET,
        *,
        default: str | None = None,
    ) -> str | None:
        """Retrieve a secret value for `(plugin_id, key)` or return `default`."""
        scope, secret_key = _resolve_scope_and_key(plugin_id, key)
        with self._lock:
            return self._read_buckets_unlocked().get(scope, {}).get(secret_key, default)

    def has(self, plugin_id: str | None = None, key: str | None = _UNSET) -> bool:
        """Return True if a secret key is present in the vault."""
        scope, secret_key = _resolve_scope_and_key(plugin_id, key)
        with self._lock:
            return secret_key in self._read_buckets_unlocked().get(scope, {})

    def delete(self, plugin_id: str | None = None, key: str | None = _UNSET) -> bool:
        """Delete a single secret key, returning True if it existed."""
        scope, secret_key = _resolve_scope_and_key(plugin_id, key)
        with self._lock:
            buckets = self._read_buckets_unlocked()
            bucket = buckets.get(scope)
            if not bucket or secret_key not in bucket:
                return False
            del bucket[secret_key]
            if not bucket:
                buckets.pop(scope, None)
            self._write_buckets_unlocked(buckets)
            logger.debug("SecretVault deleted key=%r scope=%r", secret_key, scope)
            return True

    def delete_plugin(self, plugin_id: str) -> int:
        """Delete all secrets owned by `plugin_id` and return the number removed."""
        valid_id = validate_plugin_id(plugin_id)
        with self._lock:
            buckets = self._read_buckets_unlocked()
            removed = buckets.pop(valid_id, None)
            if not removed:
                return 0
            self._write_buckets_unlocked(buckets)
            logger.debug("SecretVault cleared %d keys for scope=%r", len(removed), valid_id)
            return len(removed)

    def list_keys(self, plugin_id: str | None = None) -> list[str]:
        """List sorted secret key names (never values) for one plugin or the whole vault."""
        with self._lock:
            buckets = self._read_buckets_unlocked()
            if plugin_id is not None:
                return sorted(buckets.get(validate_plugin_id(plugin_id), {}))
            keys: list[str] = []
            for scope, entries in buckets.items():
                if scope == _GLOBAL_SCOPE:
                    keys.extend(entries.keys())
                else:
                    keys.extend(f"{scope}:{k}" for k in entries)
            return sorted(keys)

    def list_plugins(self) -> list[str]:
        """Return sorted plugin IDs that currently have at least one stored secret."""
        with self._lock:
            buckets = self._read_buckets_unlocked()
            return sorted(s for s, entries in buckets.items() if s != _GLOBAL_SCOPE and entries)

    def secret_values(self) -> tuple[str, ...]:
        """Return stored secret strings for log/error redaction providers such as EntityBus."""
        with self._lock:
            buckets = self._read_buckets_unlocked()
            values = {val for entries in buckets.values() for val in entries.values() if val}
            return tuple(sorted(values, key=len, reverse=True))

    def redact(self, text: str) -> str:
        """Redact known credential patterns and all currently vaulted secrets from `text`."""
        return redact_secrets(text, self.secret_values())

    def to_safe_summary(self) -> dict[str, list[str]]:
        """Return a zero-value diagnostic mapping of scopes to sorted key names."""
        with self._lock:
            buckets = self._read_buckets_unlocked()
            return {scope: sorted(entries) for scope, entries in sorted(buckets.items()) if entries}
