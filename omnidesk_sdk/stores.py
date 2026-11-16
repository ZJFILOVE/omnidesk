"""ConfigStore and SecretVault standalone helpers with secret stripping and 0600 atomic writes."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import tempfile
import threading
import weakref
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}")
_KEY_VAL_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|cookie|authorization)\s*([=:])\s*([^\s,;'\"]+)"
)
_KNOWN_PREFIX_RE = re.compile(r"\b(?:sk|ghp|gho|glpat|xox[baprs])-[A-Za-z0-9_-]{8,}\b")
_VAULT_SCHEMA_VERSION = 1
_GLOBAL_SCOPE = "__global__"

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


def validate_plugin_id(plugin_id: str) -> str:
    """Validate a plugin ID per PLUGIN_SPEC.md (safe characters, no path traversal)."""
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ValueError("plugin_id must be a non-empty string")
    if "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id or "\x00" in plugin_id:
        raise ValueError(f"Invalid plugin_id {plugin_id!r}: path traversal characters forbidden")
    if not _PLUGIN_ID_RE.match(plugin_id):
        raise ValueError(f"Invalid plugin_id {plugin_id!r}: must match {_PLUGIN_ID_RE.pattern}")
    return plugin_id


def validate_secret_key(key: Any) -> str:
    """Validate that a secret key is a non-empty string without null bytes or surrounding whitespace."""
    if not isinstance(key, str):
        raise ValueError("secret key must be a non-empty string")
    cleaned = key.strip()
    if not cleaned or "\x00" in key or cleaned != key:
        raise ValueError("secret key must be a non-empty string without null bytes or surrounding whitespace")
    return key


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub known credential patterns and explicit secret strings from diagnostic text."""
    if not text:
        return ""
    scrubbed = text
    for secret in sorted({s for s in extra_secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    scrubbed = _BEARER_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _KEY_VAL_RE.sub(r"\1\2[REDACTED]", scrubbed)
    scrubbed = _KNOWN_PREFIX_RE.sub("[REDACTED]", scrubbed)
    return scrubbed


def _is_shared_system_root(path: Path) -> bool:
    """Return True for shared roots whose permissions must never be altered."""
    resolved = path.resolve()
    protected = {Path("/"), Path("/tmp"), Path("/var/tmp"), Path.home().resolve()}
    for env_name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            protected.add(Path(raw).expanduser().resolve())
    return resolved in protected


def _atomic_write_json(target: Path, payload: Mapping[str, Any], *, file_mode: int = 0o600) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _is_shared_system_root(target.parent):
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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
        os.chmod(target, file_mode)
    except BaseException:
        try:
            if not tmp_file.closed:
                tmp_file.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def _strip_secret_fields(value: Any, secret_keys: set[str]) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k in secret_keys:
                continue
            cleaned[k] = _strip_secret_fields(v, secret_keys)
        return cleaned
    if isinstance(value, list):
        return [_strip_secret_fields(item, secret_keys) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_secret_fields(item, secret_keys) for item in value)
    return copy.deepcopy(value)


def _schema_field_parts(field_spec: Any) -> tuple[Any, Any, Any]:
    if isinstance(field_spec, Mapping):
        return field_spec.get("key"), field_spec.get("type"), field_spec.get("default")
    return (
        getattr(field_spec, "key", None),
        getattr(field_spec, "type", None),
        getattr(field_spec, "default", None),
    )


def _extract_schema_keys(config_schema: Iterable[Any] | None) -> tuple[set[str], set[str]]:
    secrets: set[str] = set()
    plain: set[str] = set()
    if config_schema is None:
        return secrets, plain
    for field_spec in config_schema:
        key, ftype, _ = _schema_field_parts(field_spec)
        if isinstance(key, str) and key:
            (secrets if ftype == "secret" else plain).add(key)
    return secrets, plain


class LocalConfigStore:
    """In-memory or 0600 JSON-backed plain config store that strips secret keys."""

    def __init__(self, config_dir: Path | str | None = None) -> None:
        self._root_dir = Path(config_dir).expanduser() if config_dir is not None else None
        self._lock = threading.RLock()
        self._memory: dict[str, dict[str, Any]] = {}
        self._registered_secrets: dict[str, set[str]] = {}
        self._registered_plain: dict[str, set[str]] = {}

    def register_schema(self, plugin_id: str, config_schema: Iterable[Any]) -> None:
        pid = validate_plugin_id(plugin_id)
        secrets, plain = _extract_schema_keys(config_schema)
        with self._lock:
            self._registered_secrets.setdefault(pid, set()).update(secrets)
            self._registered_plain.setdefault(pid, set()).update(plain)

    def register_secret_keys(self, plugin_id: str, secret_keys: Iterable[str]) -> None:
        pid = validate_plugin_id(plugin_id)
        with self._lock:
            bucket = self._registered_secrets.setdefault(pid, set())
            for key in secret_keys:
                if isinstance(key, str) and key:
                    bucket.add(key)

    def _effective_secret_keys(
        self,
        plugin_id: str,
        *,
        config_schema: Iterable[Any] | None = None,
        secret_keys: Iterable[str] | None = None,
        strip_known_secrets: bool = True,
    ) -> set[str]:
        schema_secrets, schema_plain = _extract_schema_keys(config_schema)
        keys = set(schema_secrets)
        keys.update(self._registered_secrets.get(plugin_id, ()))
        if secret_keys is not None:
            keys.update(k for k in secret_keys if isinstance(k, str) and k)
        plain_keys = set(self._registered_plain.get(plugin_id, ()))
        plain_keys.update(schema_plain)
        if strip_known_secrets:
            keys.update(DEFAULT_SECRET_KEYS - plain_keys)
        return keys

    def _file_path(self, plugin_id: str) -> Path | None:
        if self._root_dir is None:
            return None
        pid = validate_plugin_id(plugin_id)
        return self._root_dir / "plugins" / f"{pid}.json"

    def read(
        self,
        plugin_id: str,
        *,
        config_schema: Iterable[Any] | None = None,
        secret_keys: Iterable[str] | None = None,
        strip_known_secrets: bool = True,
        apply_defaults: bool = False,
    ) -> dict[str, Any]:
        pid = validate_plugin_id(plugin_id)
        with self._lock:
            path = self._file_path(pid)
            if path is not None and (path.exists() or path.is_symlink()):
                if path.is_symlink():
                    raise ValueError(f"Refusing to read symlinked config file: {path}")
                if not path.is_file():
                    raise ValueError(f"Config path is not a regular file: {path}")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    raw = json.loads(handle.read())
                if not isinstance(raw, Mapping):
                    raise ValueError(f"Config for {pid!r} must be a JSON object")
                data = dict(raw)
            else:
                data = copy.deepcopy(self._memory.get(pid, {}))
            strip = self._effective_secret_keys(
                pid,
                config_schema=config_schema,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )
            cleaned = _strip_secret_fields(data, strip)
            if apply_defaults and config_schema is not None:
                for field_spec in config_schema:
                    key, ftype, default_val = _schema_field_parts(field_spec)
                    if (
                        isinstance(key, str)
                        and key
                        and ftype != "secret"
                        and key not in strip
                        and key not in cleaned
                        and default_val is not None
                    ):
                        cleaned[key] = copy.deepcopy(default_val)
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
        pid = validate_plugin_id(plugin_id)
        if not isinstance(data, Mapping):
            raise TypeError("Config data must be a mapping")
        with self._lock:
            strip = self._effective_secret_keys(
                pid,
                config_schema=config_schema,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )
            cleaned = _strip_secret_fields(data, strip)
            path = self._file_path(pid)
            if path is not None:
                if path.is_symlink():
                    raise ValueError(f"Refusing to write symlinked config file: {path}")
                _atomic_write_json(path, cleaned, file_mode=0o600)
            else:
                self._memory[pid] = copy.deepcopy(cleaned)
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
        if not isinstance(patch, Mapping):
            raise TypeError("Config patch must be a mapping")
        with self._lock:
            current = self.read(
                plugin_id,
                config_schema=config_schema,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )
            merged = {**current, **dict(patch)}
            return self.write(
                plugin_id,
                merged,
                config_schema=config_schema,
                secret_keys=secret_keys,
                strip_known_secrets=strip_known_secrets,
            )


class LocalSecretVault:
    """In-memory or 0600 JSON-backed per-plugin secret vault compatible with `omnidesk_core.SecretVault`."""

    def __init__(self, config_dir: Path | str | None = None) -> None:
        self._vault_path = (Path(config_dir).expanduser() / "vault.json") if config_dir is not None else None
        self._lock = threading.RLock()
        self._secrets: dict[str, dict[str, str]] = {}

    def __repr__(self) -> str:
        return f"LocalSecretVault(vault_path={str(self._vault_path)!r})"

    __str__ = __repr__

    def _verify_target_safety(self) -> None:
        if self._vault_path is None:
            return
        target = self._vault_path
        if target.is_symlink():
            raise ValueError(f"Refusing symlinked secret vault: {target}")
        if not target.exists():
            return
        if not target.is_file():
            raise ValueError(f"Secret vault path is not a regular file: {target}")
        st = target.lstat()
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise PermissionError(f"Secret vault {target} is not owned by the current user")
        if (stat.S_IMODE(st.st_mode) & 0o077) != 0:
            os.chmod(target, 0o600)

    @staticmethod
    def _normalize_loaded(raw: Any) -> dict[str, dict[str, str]]:
        if not isinstance(raw, Mapping):
            raise ValueError("Secret vault root payload must be a JSON object")
        source = raw["secrets"] if "secrets" in raw else {k: v for k, v in raw.items() if k != "version"}
        if not isinstance(source, Mapping):
            raise ValueError("Secret vault 'secrets' entry must be a JSON object")
        buckets: dict[str, dict[str, str]] = {}
        for scope, entry in source.items():
            if not isinstance(scope, str) or not scope:
                continue
            if isinstance(entry, str):
                buckets.setdefault(_GLOBAL_SCOPE, {})[validate_secret_key(scope)] = entry
            elif isinstance(entry, Mapping):
                clean_scope = _GLOBAL_SCOPE if scope == _GLOBAL_SCOPE else validate_plugin_id(scope)
                buckets[clean_scope] = {validate_secret_key(str(k)): str(v) for k, v in entry.items()}
        return buckets

    def _load(self) -> dict[str, dict[str, str]]:
        if self._vault_path is None:
            return self._secrets
        if not self._vault_path.exists() and not self._vault_path.is_symlink():
            return {}
        self._verify_target_safety()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._vault_path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
        if not raw_text.strip():
            return {}
        try:
            loaded = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Corrupted secret vault JSON at {self._vault_path}: line {exc.lineno} col {exc.colno}"
            ) from None
        return self._normalize_loaded(loaded)

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        cleaned = {
            scope: dict(sorted(entries.items()))
            for scope, entries in sorted(data.items())
            if entries
        }
        if self._vault_path is not None:
            self._verify_target_safety()
            payload = {"version": _VAULT_SCHEMA_VERSION, "secrets": cleaned}
            _atomic_write_json(self._vault_path, payload, file_mode=0o600)
        else:
            self._secrets = cleaned

    def get_secret(self, plugin_id: str, key: str, default: str | None = None) -> str | None:
        pid = validate_plugin_id(plugin_id)
        secret_key = validate_secret_key(key)
        with self._lock:
            return self._load().get(pid, {}).get(secret_key, default)

    def set_secret(self, plugin_id: str, key: str, value: str | None) -> None:
        pid = validate_plugin_id(plugin_id)
        secret_key = validate_secret_key(key)
        if value is None:
            self.delete_secret(pid, secret_key)
            return
        with self._lock:
            data = self._load()
            bucket = dict(data.get(pid, {}))
            bucket[secret_key] = str(value)
            data[pid] = bucket
            self._save(data)

    def delete_secret(self, plugin_id: str, key: str) -> bool:
        pid = validate_plugin_id(plugin_id)
        secret_key = validate_secret_key(key)
        with self._lock:
            data = self._load()
            bucket = dict(data.get(pid, {}))
            existed = secret_key in bucket
            bucket.pop(secret_key, None)
            if bucket:
                data[pid] = bucket
            else:
                data.pop(pid, None)
            if existed:
                self._save(data)
            return existed

    def delete_plugin(self, plugin_id: str) -> int:
        pid = validate_plugin_id(plugin_id)
        with self._lock:
            data = self._load()
            removed = data.pop(pid, None)
            if not removed:
                return 0
            self._save(data)
            return len(removed)

    def secret_values(self) -> tuple[str, ...]:
        """Return stored secret strings (longest first) for log/error redaction."""
        with self._lock:
            data = self._load()
            values = {val for entries in data.values() for val in entries.values() if val}
            return tuple(sorted(values, key=len, reverse=True))


class SecretRedactor:
    """Collects secret providers and weak-referenced vaults to scrub diagnostic text."""

    def __init__(self, secret_provider: Callable[[], Iterable[str]] | None = None) -> None:
        self._provider = secret_provider
        self._vault_refs: list[weakref.ref[Any]] = []

    def add_vault(self, vault: Any) -> None:
        if vault is None or not callable(getattr(vault, "secret_values", None)):
            return
        try:
            ref = weakref.ref(vault)
        except TypeError:
            return
        self._vault_refs = [r for r in self._vault_refs if r() is not None]
        if all(r() is not vault for r in self._vault_refs):
            self._vault_refs.append(ref)

    def secrets(self) -> list[str]:
        collected: list[str] = []
        if self._provider is not None:
            try:
                collected.extend(self._provider())
            except Exception:
                pass
        alive: list[weakref.ref[Any]] = []
        for ref in self._vault_refs:
            vault = ref()
            if vault is not None:
                alive.append(ref)
                try:
                    collected.extend(vault.secret_values())
                except Exception:
                    pass
        self._vault_refs = alive
        return collected

    def redact(self, text: str) -> str:
        return redact_secrets(text, self.secrets())
