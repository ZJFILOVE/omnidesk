"""Pure Python plugin loader, hot-reload manager, fault isolator, and verifier."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any

from omnidesk_sdk import (
    BasePlugin,
    CapabilityResult as SdkCapabilityResult,
    DockWidgetSpec,
    PluginContext,
    WorkspaceTabSpec,
    invoke_lifecycle_hook,
)

from .bus import EntityBus
from .config import ConfigStore, DEFAULT_SECRET_KEYS, atomic_write_json
from .domain import DomainKind, DomainRegistry
from .errors import (
    CapabilityNotFoundError,
    ErrorCode,
    InvalidArgumentError,
    InvalidCapabilityIdError,
    PluginFaultError,
    PluginManifestError,
    PluginNotFoundError,
    redact_secrets,
)
from .jobs import JobCenter
from .manifest import PluginManifest, load_manifest, scaffold_plugin
from .search import SearchIndex
from .paths import OmnideskPaths, safe_child_path, validate_plugin_id
from .registry import CapabilityRegistry, CapabilityResult as CoreCapabilityResult
from .vault import SecretVault

PLUGIN_STATE_CHANGED_TOPIC = "plugin.state_changed"
_PKG_NAMESPACE = "_omnidesk_plugins"
_SYS_MOD_LOCK = threading.RLock()
_PLUGIN_SIBLING_DIRS: dict[str, Path] = {}


class PluginState(StrEnum):
    """Lifecycle states tracked by PluginManager."""

    DISCOVERED = "discovered"
    LOADED = "loaded"
    UNLOADED = "unloaded"
    FAULTED = "faulted"
    INVALID = "invalid"


class _StreamBridge:
    """Bridges core CapabilityStream and SDK CapabilityStream expectations."""

    def __init__(self, core_stream: Any) -> None:
        self._stream = core_stream

    @property
    def is_cancelled(self) -> bool:
        """Dynamically proxy the underlying core stream cancellation state."""
        val = getattr(self._stream, "is_cancelled", False)
        return bool(val() if callable(val) else val)

    async def emit_delta(
        self,
        text: str | Mapping[str, Any],
        *,
        event: str = "delta",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if data is not None:
            await self._stream.emit(event, {**dict(data), "text": text})
        elif event != "delta":
            await self._stream.emit(event, text)
        else:
            await self._stream.emit_delta(text)

    async def emit_event(self, event: str, data: Mapping[str, Any] | None = None) -> None:
        await self._stream.emit(event, dict(data) if data is not None else {})

    async def emit(self, event: str, data: Any = None) -> None:
        await self._stream.emit(event, data)

    def emit_sync(self, event: str, data: Any = None) -> None:
        self._stream.emit_sync(event, data)


def _to_core_result(outcome: Any, plugin_id: str) -> Any:
    if isinstance(outcome, SdkCapabilityResult):
        return CoreCapabilityResult(
            ok=outcome.ok,
            data=outcome.data,
            error=outcome.error,
            provider_plugin_id=outcome.provider_plugin_id or plugin_id,
        )
    return outcome


def _to_sdk_result(res: CoreCapabilityResult) -> SdkCapabilityResult:
    return SdkCapabilityResult(
        ok=res.ok,
        data=dict(res.data),
        error=res.error,
        provider_plugin_id=res.provider_plugin_id,
    )


def _takes_no_pos_args(fn: Callable[..., Any]) -> bool:
    try:
        kinds = (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        return not any(p.kind in kinds for p in inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False


def _normalize_capability_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate and normalize capability metadata into a JSON-serializable dict."""
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise InvalidArgumentError("Capability metadata must be a mapping")

    def _convert(value: Any, seen: set[int]) -> Any:
        if isinstance(value, Mapping):
            obj_id = id(value)
            if obj_id in seen:
                raise InvalidArgumentError("Capability metadata must not contain circular references")
            seen.add(obj_id)
            try:
                return {k: _convert(v, seen) for k, v in value.items()}
            finally:
                seen.remove(obj_id)
        if isinstance(value, (list, tuple)):
            obj_id = id(value)
            if obj_id in seen:
                raise InvalidArgumentError("Capability metadata must not contain circular references")
            seen.add(obj_id)
            try:
                converted = [_convert(item, seen) for item in value]
                return tuple(converted) if isinstance(value, tuple) else converted
            finally:
                seen.remove(obj_id)
        return value

    normalized = _convert(metadata, set())
    try:
        json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentError(f"Capability metadata must be JSON-serializable: {exc}") from None
    return normalized


class _CapabilityAdapter:
    """Adapts PluginContext capability APIs onto core CapabilityRegistry."""

    def __init__(self, core: CapabilityRegistry) -> None:
        self._core = core

    def register_capability(
        self,
        cap_id: str,
        handler: Callable[..., Any],
        *,
        plugin_id: str = "core",
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
        description: str = "",
        replace: bool = True,
    ) -> Any:
        return self.register(
            cap_id,
            handler,
            plugin_id=plugin_id,
            streaming=False,
            replace=replace,
            description=description,
            priority=priority,
            metadata=metadata,
        )

    def register_streaming_capability(
        self,
        cap_id: str,
        handler: Callable[..., Any],
        *,
        plugin_id: str = "core",
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
        description: str = "",
        replace: bool = True,
    ) -> Any:
        return self.register(
            cap_id,
            handler,
            plugin_id=plugin_id,
            streaming=True,
            replace=replace,
            description=description,
            priority=priority,
            metadata=metadata,
        )

    def register(
        self,
        cap_id: str,
        handler: Callable[..., Any],
        *,
        plugin_id: str = "core",
        streaming: bool = False,
        replace: bool = True,
        description: str = "",
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        if not callable(handler):
            raise InvalidArgumentError("Capability handler must be callable")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise InvalidArgumentError("priority must be an integer")
        if not isinstance(description, str):
            raise InvalidArgumentError("description must be a string")
        normalized_metadata = _normalize_capability_metadata(metadata)
        if streaming and (inspect.isasyncgenfunction(handler) or inspect.isgeneratorfunction(handler)):
            wrapped: Callable[..., Any] = handler
        elif streaming:
            async def _wrapped_stream(req: dict[str, Any], stream: Any) -> Any:
                out = handler(req, _StreamBridge(stream))
                return _to_core_result(await out if inspect.isawaitable(out) else out, plugin_id)
            wrapped = _wrapped_stream
        else:
            zero_arg = _takes_no_pos_args(handler)

            async def _wrapped(req: dict[str, Any]) -> Any:
                out = handler() if (zero_arg and not req) else handler(req)
                return _to_core_result(await out if inspect.isawaitable(out) else out, plugin_id)

            wrapped = _wrapped
        return self._core.register(
            cap_id,
            wrapped,
            plugin_id=plugin_id,
            streaming=streaming,
            replace=replace,
            description=description,
            priority=priority,
            metadata=normalized_metadata,
        )

    def unregister(self, cap_id: str, *, plugin_id: str | None = None) -> bool:
        return self._core.unregister(cap_id, plugin_id=plugin_id)

    def unregister_plugin(self, plugin_id: str) -> int:
        return self._core.revoke_plugin(plugin_id)

    revoke_plugin = unregister_plugin

    def has(self, cap_id: str) -> bool:
        return self._core.has(cap_id)

    def list_capabilities(self, **kwargs: Any) -> list[Any]:
        return self._core.list_capabilities(**kwargs)

    async def call(
        self, cap_id: str, payload: Mapping[str, Any] | Any = None, *, timeout: float | None = None
    ) -> SdkCapabilityResult:
        try:
            return _to_sdk_result(await self._core.call(cap_id, payload, timeout=timeout, isolate_errors=True))
        except CapabilityNotFoundError as exc:
            return SdkCapabilityResult.fail(str(exc), error_code=ErrorCode.CAPABILITY_NOT_FOUND)
        except InvalidCapabilityIdError as exc:
            return SdkCapabilityResult.fail(str(exc), error_code=ErrorCode.INVALID_ARGUMENT)

    async def call_stream(
        self,
        cap_id: str,
        payload: Mapping[str, Any] | Any = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> SdkCapabilityResult:
        final_res: SdkCapabilityResult | None = None
        try:
            async for chunk in self._core.stream(cap_id, payload, timeout=timeout, isolate_errors=True):
                if chunk.done and chunk.result is not None:
                    final_res = _to_sdk_result(chunk.result)
                elif on_chunk is not None:
                    maybe = on_chunk({"event": chunk.event, "data": chunk.data})
                    if inspect.isawaitable(maybe):
                        await maybe
        except CapabilityNotFoundError as exc:
            return SdkCapabilityResult.fail(str(exc), error_code=ErrorCode.CAPABILITY_NOT_FOUND)
        return final_res if final_res is not None else SdkCapabilityResult.ok()


class _DomainAdapter:
    """Adapts PluginContext domain sources and declarative UI specs onto core DomainRegistry."""

    def __init__(self, core: DomainRegistry) -> None:
        self._core = core
        self._lock = threading.RLock()
        self._anon_seq: dict[str, int] = {}
        self._tabs: list[WorkspaceTabSpec] = []
        self._widgets: list[DockWidgetSpec] = []

    def _reg_src(self, kind: DomainKind, plugin_id: str, source: Any) -> Any:
        raw = getattr(source, "source_id", "")
        if isinstance(raw, str) and raw.strip():
            sid = raw.strip()
        else:
            with self._lock:
                idx = self._anon_seq.get(plugin_id, 0) + 1
                self._anon_seq[plugin_id] = idx
            sid = plugin_id if idx == 1 else f"{plugin_id}:{idx}"
        try:
            priority = int(getattr(source, "priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        title = str(getattr(source, "display_name", "") or sid)
        category = str(getattr(source, "category", "") or "")
        metadata: dict[str, Any] = {}
        categories_fn = getattr(source, "categories", None)
        if callable(categories_fn):
            try:
                categories = categories_fn()
                if isinstance(categories, (list, tuple)):
                    metadata["categories"] = [str(item) for item in categories if str(item)]
            except Exception:  # noqa: BLE001 - source discovery must not break registration
                metadata["categories"] = []
        self._core.register(
            kind,
            sid,
            source,
            plugin_id=plugin_id,
            title=title,
            category=category,
            priority=priority,
            metadata=metadata,
            replace=True,
        )
        return source

    def register_media_source(self, plugin_id: str, source: Any) -> Any:
        return self._reg_src(DomainKind.MEDIA, plugin_id, source)

    def register_text_source(self, plugin_id: str, source: Any) -> Any:
        return self._reg_src(DomainKind.TEXT, plugin_id, source)

    def register_book_source(self, plugin_id: str, source: Any) -> Any:
        return self._reg_src(DomainKind.BOOK, plugin_id, source)

    def media_sources(self) -> list[Any]:
        return [e.source for e in self._core.list_media_sources()]

    def text_sources(self) -> list[Any]:
        return [e.source for e in self._core.list_text_sources()]

    def book_sources(self) -> list[Any]:
        return [e.source for e in self._core.list_book_sources()]

    def register_workspace_tab(self, spec: WorkspaceTabSpec) -> WorkspaceTabSpec:
        with self._lock:
            self._tabs = [t for t in self._tabs if not (t.plugin_id == spec.plugin_id and t.tab_id == spec.tab_id)]
            self._tabs.append(spec)
        return spec

    def register_dock_widget(self, spec: DockWidgetSpec) -> DockWidgetSpec:
        with self._lock:
            self._widgets = [
                w for w in self._widgets if not (w.plugin_id == spec.plugin_id and w.widget_id == spec.widget_id)
            ]
            self._widgets.append(spec)
        return spec

    def workspace_tabs(self, domain: str | None = None) -> list[WorkspaceTabSpec]:
        with self._lock:
            return list(self._tabs) if domain is None else [t for t in self._tabs if t.domain == domain]

    def dock_widgets(self, domain: str | None = None) -> list[DockWidgetSpec]:
        with self._lock:
            return list(self._widgets) if domain is None else [w for w in self._widgets if domain in w.supported_domains]

    def unregister_plugin(self, plugin_id: str) -> int:
        revoked = self._core.revoke_plugin(plugin_id)
        with self._lock:
            before = len(self._tabs) + len(self._widgets)
            self._tabs = [t for t in self._tabs if t.plugin_id != plugin_id]
            self._widgets = [w for w in self._widgets if w.plugin_id != plugin_id]
            self._anon_seq.pop(plugin_id, None)
            return revoked + (before - len(self._tabs) - len(self._widgets))

    revoke_plugin = unregister_plugin


@dataclass
class PluginRecord:
    """Runtime status snapshot for a discovered or loaded plugin."""

    plugin_id: str
    plugin_dir: Path
    state: PluginState = PluginState.DISCOVERED
    manifest: PluginManifest | None = None
    enabled: bool = True
    error: str = ""
    loaded_at: float | None = None
    reload_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        m = self.manifest
        return {
            "plugin_id": self.plugin_id,
            "plugin_dir": str(self.plugin_dir),
            "state": str(self.state),
            "enabled": self.enabled,
            "error": self.error,
            "loaded_at": self.loaded_at,
            "reload_count": self.reload_count,
            "name": m.name if m else self.plugin_id,
            "version": m.version if m else "",
            "kind": m.kind if m else "",
            "domain": m.domain if m else "",
            "provides": list(m.provides) if m else [],
            "consumes": list(m.consumes) if m else [],
        }


@dataclass(frozen=True, slots=True)
class PluginVerifyResult:
    """Verification report produced by PluginManager.verify_plugin."""

    ok: bool
    plugin_id: str
    plugin_dir: Path
    manifest_ok: bool
    lifecycle_ok: bool
    tests_ok: bool
    tests_skipped: bool = False
    health: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plugin_id": self.plugin_id,
            "plugin_dir": str(self.plugin_dir),
            "manifest_ok": self.manifest_ok,
            "lifecycle_ok": self.lifecycle_ok,
            "tests_ok": self.tests_ok,
            "tests_skipped": self.tests_skipped,
            "health": dict(self.health),
            "errors": list(self.errors),
        }


@dataclass
class _LoadedEntry:
    record: PluginRecord
    instance: Any
    ctx: PluginContext
    module_names: set[str]


def _pkg_ident(plugin_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", plugin_id)


def _is_under_dir(path_str: str | None, directory: Path) -> bool:
    if not path_str:
        return False
    try:
        Path(path_str).resolve().relative_to(directory.resolve())
        return True
    except (OSError, ValueError):
        return False


def _purge_plugin_modules(pkg_name: str, plugin_dir: Path, extra_names: Iterable[str] = ()) -> None:
    with _SYS_MOD_LOCK:
        targets = set(extra_names)
        for name, mod in list(sys.modules.items()):
            if name == pkg_name or name.startswith(f"{pkg_name}.") or (
                mod is not None and _is_under_dir(getattr(mod, "__file__", None), plugin_dir)
            ):
                targets.add(name)
        for name in targets:
            sys.modules.pop(name, None)
            _PLUGIN_SIBLING_DIRS.pop(name, None)
        importlib.invalidate_caches()


def _import_plugin_instance(manifest: PluginManifest, plugin_dir: Path) -> tuple[Any, set[str]]:
    parts = manifest.entry_module.split(".")
    entry_file = next(
        (
            cand
            for cand in (
                safe_child_path(plugin_dir, *parts[:-1], f"{parts[-1]}.py"),
                safe_child_path(plugin_dir, *parts, "__init__.py"),
            )
            if cand.is_file() and not cand.is_symlink()
        ),
        None,
    )
    if entry_file is None:
        raise FileNotFoundError(f"Entrypoint module {manifest.entry_module!r} not found in {plugin_dir}")

    pkg_name = f"{_PKG_NAMESPACE}.{_pkg_ident(manifest.id)}"
    resolved_dir = plugin_dir.resolve()
    for cache_dir in resolved_dir.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    _purge_plugin_modules(pkg_name, resolved_dir)

    with _SYS_MOD_LOCK:
        if _PKG_NAMESPACE not in sys.modules:
            ns = ModuleType(_PKG_NAMESPACE)
            ns.__path__ = []
            sys.modules[_PKG_NAMESPACE] = ns

        pkg_mod = ModuleType(pkg_name)
        pkg_mod.__path__ = [str(resolved_dir)]
        pkg_mod.__package__ = pkg_name
        sys.modules[pkg_name] = pkg_mod

        for sibling in resolved_dir.glob("*.py"):
            owner = _PLUGIN_SIBLING_DIRS.get(sibling.stem)
            if owner is not None and owner != resolved_dir:
                sys.modules.pop(sibling.stem, None)

        full_mod = f"{pkg_name}.{manifest.entry_module}"
        locs = [str(entry_file.parent)] if entry_file.name == "__init__.py" else None
        spec = importlib.util.spec_from_file_location(full_mod, entry_file, submodule_search_locations=locs)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {entry_file}")

        before = set(sys.modules)
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_mod] = module
        added = str(resolved_dir)
        sys.path.insert(0, added)
        try:
            spec.loader.exec_module(module)
        except BaseException:
            for name in set(sys.modules) - before:
                sys.modules.pop(name, None)
            sys.modules.pop(pkg_name, None)
            raise
        finally:
            if added in sys.path:
                sys.path.remove(added)

        owned = {pkg_name, full_mod}
        for name in set(sys.modules) - before:
            mod = sys.modules.get(name)
            if mod is not None and _is_under_dir(getattr(mod, "__file__", None), resolved_dir):
                owned.add(name)
                if not name.startswith(f"{pkg_name}."):
                    _PLUGIN_SIBLING_DIRS[name] = resolved_dir

    factory = getattr(module, manifest.entry_callable, None)
    if not callable(factory):
        _purge_plugin_modules(pkg_name, resolved_dir, owned)
        raise AttributeError(f"Entrypoint {manifest.entry!r} is missing or not callable")
    return factory, owned


_VALID_SNAPSHOT_DOMAINS = frozenset({"core", "media", "text", "book"})


def _to_json_safe(value: Any, *, extra_secrets: Iterable[str] = ()) -> Any:
    """Convert domain snapshot payloads to JSON-safe values while scrubbing secrets and callables."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_secrets(value, extra_secrets)
    if isinstance(value, Path):
        return str(value)
    if callable(value):
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        return _to_json_safe(dumped, extra_secrets=extra_secrets)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if callable(v):
                continue
            if key_str.lower() in DEFAULT_SECRET_KEYS and v is not None:
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = _to_json_safe(v, extra_secrets=extra_secrets)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(item, extra_secrets=extra_secrets) for item in value if not callable(item)]
    return redact_secrets(str(value), extra_secrets)


class PluginManager:
    """Manages discovery, importlib loading, hot-reload, fault isolation, and resource revocation."""

    def __init__(
        self,
        *,
        plugin_dirs: Sequence[Path | str] | None = None,
        plugins_dir: Path | str | None = None,
        paths: OmnideskPaths | None = None,
        bus: EntityBus | None = None,
        capabilities: CapabilityRegistry | None = None,
        domains: DomainRegistry | None = None,
        jobs: JobCenter | None = None,
        search: SearchIndex | None = None,
        config_store: ConfigStore | None = None,
        secret_vault: SecretVault | None = None,
        isolate_errors: bool = False,
        state_path: Path | str | None = None,
    ) -> None:
        self.paths = paths or OmnideskPaths.from_env()
        self.secret_vault = secret_vault or SecretVault(paths=self.paths)
        self.bus = bus or EntityBus(secret_provider=self.secret_vault.secret_values)
        self.capabilities = capabilities or CapabilityRegistry(
            bus=self.bus, secret_provider=self.secret_vault.secret_values
        )
        self.domains = domains or DomainRegistry()
        self.jobs = jobs or JobCenter(bus=self.bus, secret_provider=self.secret_vault.secret_values)
        self.search = search
        self.config_store = config_store or ConfigStore(self.paths.config_dir)
        self._cap_adapter = _CapabilityAdapter(self.capabilities)
        self._domain_adapter = _DomainAdapter(self.domains)
        self._isolate_errors = isolate_errors
        self._state_path = Path(state_path).expanduser() if state_path is not None else None
        self._records: dict[str, PluginRecord] = {}
        self._loaded: dict[str, _LoadedEntry] = {}
        self._disabled: set[str] = self._load_disabled_state()

        dirs = ([Path(plugins_dir).expanduser()] if plugins_dir is not None else []) + (
            [Path(d).expanduser() for d in plugin_dirs] if plugin_dirs is not None else []
        )
        self._plugin_dirs: list[Path] = dirs or [self.paths.plugins_dir]
        self._lock = asyncio.Lock()

    def workspace_tabs(self, domain: str | None = None) -> list[WorkspaceTabSpec]:
        return self._domain_adapter.workspace_tabs(domain=domain)

    def dock_widgets(self, domain: str | None = None) -> list[DockWidgetSpec]:
        return self._domain_adapter.dock_widgets(domain=domain)

    async def invoke_ui_action(
        self,
        *,
        kind: str,
        spec_id: str,
        action_id: str,
        plugin_id: str | None = None,
        state: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> CoreCapabilityResult:
        """Invoke a declarative workspace-tab or dock-widget action.

        The snapshot intentionally strips callbacks because they are not JSON
        serializable.  This method is the small, explicit bridge back into the
        owning plugin so declarative UI controls remain interactive.
        """
        if kind not in {"workspace_tab", "dock_widget"}:
            return CoreCapabilityResult.fail(
                f"Unsupported UI action kind {kind!r}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        sid = spec_id.strip() if isinstance(spec_id, str) else ""
        aid = action_id.strip() if isinstance(action_id, str) else ""
        if not sid or not aid:
            return CoreCapabilityResult.fail(
                "UI action requires non-empty spec_id and action_id",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        pid = validate_plugin_id(plugin_id) if plugin_id is not None else None
        state_map = dict(state) if state is not None else {}

        specs: list[WorkspaceTabSpec | DockWidgetSpec]
        id_attr: str
        if kind == "workspace_tab":
            specs = self.workspace_tabs()
            id_attr = "tab_id"
        else:
            specs = self.dock_widgets()
            id_attr = "widget_id"

        target = next(
            (
                spec
                for spec in specs
                if getattr(spec, id_attr) == sid and (pid is None or spec.plugin_id == pid)
            ),
            None,
        )
        if target is None:
            return CoreCapabilityResult.fail(
                f"No {kind} {sid!r} is registered"
                + (f" for plugin {pid!r}" if pid is not None else ""),
                error_code=ErrorCode.NOT_FOUND,
                spec_id=sid,
                kind=kind,
            )

        handler = target.on_action
        if not callable(handler):
            return CoreCapabilityResult.fail(
                f"{kind} {sid!r} does not register an on_action handler",
                error_code=ErrorCode.CAPABILITY_NOT_FOUND,
                provider_plugin_id=target.plugin_id,
            )

        async def _invoke() -> Any:
            try:
                positional = [
                    param
                    for param in inspect.signature(handler).parameters.values()
                    if param.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                ]
            except (TypeError, ValueError):
                positional = [None, None]
            if len(positional) >= 2:
                args: tuple[Any, ...] = (aid, state_map)
            elif len(positional) == 1:
                args = ({**state_map, "action": aid},)
            else:
                args = ()
            if inspect.iscoroutinefunction(handler):
                outcome = handler(*args)
            else:
                outcome = await asyncio.to_thread(handler, *args)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return outcome

        try:
            outcome = await asyncio.wait_for(_invoke(), timeout=max(0.1, float(timeout)))
        except asyncio.TimeoutError:
            return CoreCapabilityResult.fail(
                f"{kind} action {aid!r} timed out after {timeout:g}s",
                error_code=ErrorCode.TIMEOUT,
                provider_plugin_id=target.plugin_id,
            )
        except Exception as exc:  # noqa: BLE001 - plugin faults are isolated
            return CoreCapabilityResult.fail(
                f"{kind} action {aid!r} failed: {type(exc).__name__}: {exc}",
                error_code=ErrorCode.PLUGIN_FAULT,
                provider_plugin_id=target.plugin_id,
            )

        if isinstance(outcome, (CoreCapabilityResult, SdkCapabilityResult)):
            data = dict(outcome.data)
            return CoreCapabilityResult(
                ok=outcome.ok,
                data=data,
                error=outcome.error,
                provider_plugin_id=outcome.provider_plugin_id or target.plugin_id,
            )
        if outcome is None:
            payload: dict[str, Any] = {}
            ok = True
            error = ""
        elif isinstance(outcome, Mapping):
            payload = dict(outcome)
            ok = bool(payload.pop("ok", True))
            error = str(payload.pop("error", "") or "")
        else:
            payload = {"result": outcome}
            ok = True
            error = ""
        return CoreCapabilityResult(
            ok=ok,
            data=payload,
            error=error,
            provider_plugin_id=target.plugin_id,
        )

    def domain_snapshot(
        self,
        *,
        domain: str | None = None,
        plugin_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of DomainRegistry sources, workspace tabs, and dock widgets."""
        norm_domain: str | None = None
        if domain is not None:
            if not isinstance(domain, str):
                raise InvalidArgumentError("domain must be a string")
            norm_domain = domain.strip().lower()
            if norm_domain not in _VALID_SNAPSHOT_DOMAINS:
                raise InvalidArgumentError(
                    f"Unsupported domain {domain!r}; expected one of 'core', 'media', 'text', 'book'"
                )
        pid = validate_plugin_id(plugin_id) if plugin_id is not None else None
        try:
            extra_secrets = self.secret_vault.secret_values()
        except Exception:
            extra_secrets = ()

        media_entries = (
            self.domains.list_media_sources(plugin_id=pid) if norm_domain in (None, "media") else []
        )
        text_entries = (
            self.domains.list_text_sources(plugin_id=pid) if norm_domain in (None, "text") else []
        )
        book_entries = (
            self.domains.list_book_sources(plugin_id=pid) if norm_domain in (None, "book") else []
        )
        tabs = self.workspace_tabs(domain=norm_domain)
        widgets = self.dock_widgets(domain=norm_domain)
        if pid is not None:
            tabs = [t for t in tabs if t.plugin_id == pid]
            widgets = [w for w in widgets if w.plugin_id == pid]

        def _dump_spec(spec: WorkspaceTabSpec | DockWidgetSpec) -> dict[str, Any]:
            raw = spec.model_dump(mode="json")
            raw.pop("render", None)
            raw.pop("on_action", None)
            return dict(_to_json_safe(raw, extra_secrets=extra_secrets))

        return {
            "media_sources": [_to_json_safe(e.to_dict(), extra_secrets=extra_secrets) for e in media_entries],
            "text_sources": [_to_json_safe(e.to_dict(), extra_secrets=extra_secrets) for e in text_entries],
            "book_sources": [_to_json_safe(e.to_dict(), extra_secrets=extra_secrets) for e in book_entries],
            "workspace_tabs": [_dump_spec(t) for t in tabs],
            "dock_widgets": [_dump_spec(w) for w in widgets],
        }

    def _load_disabled_state(self) -> set[str]:
        if self._state_path is None or self._state_path.is_symlink() or not self._state_path.is_file():
            return set()
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return {str(p) for p in data.get("disabled", []) if isinstance(p, str)}
        except (OSError, json.JSONDecodeError):
            return set()

    def save_state(self) -> Path | None:
        """Persist plugin enablement and lifecycle status atomically to `state_path`."""
        if self._state_path is None:
            return None
        payload = {
            "disabled": sorted(self._disabled),
            "plugins": {
                pid: {
                    "enabled": rec.enabled,
                    "state": str(rec.state),
                    "reload_count": rec.reload_count,
                }
                for pid, rec in sorted(self._records.items())
            },
        }
        atomic_write_json(self._state_path, payload, file_mode=0o600)
        return self._state_path

    def _set_enabled(self, plugin_id: str, enabled: bool) -> None:
        pid = validate_plugin_id(plugin_id)
        (self._disabled.discard if enabled else self._disabled.add)(pid)
        if pid in self._records:
            self._records[pid].enabled = enabled
        self.save_state()

    def enable_plugin(self, plugin_id: str) -> None:
        self._set_enabled(plugin_id, True)

    def disable_plugin(self, plugin_id: str) -> None:
        self._set_enabled(plugin_id, False)

    def _redact(self, message: str) -> str:
        try:
            return redact_secrets(message, self.secret_vault.secret_values())
        except Exception:
            return redact_secrets(message)

    async def _emit_fault(self, plugin_id: str, component: str, error: str | BaseException) -> str:
        safe = self._redact(str(error) or type(error).__name__)
        await self.bus.emit_fault(plugin_id=plugin_id, component=component, error=safe)
        return safe

    def discover(self) -> list[PluginRecord]:
        """Scan configured plugin directories for plugin.json manifests."""
        found: list[PluginRecord] = []
        seen: set[str] = set()
        for root in self._plugin_dirs:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir(), key=lambda p: p.name):
                if not entry.is_dir() or entry.name.startswith(".") or not (entry / "plugin.json").exists():
                    continue
                try:
                    manifest = load_manifest(entry)
                    if manifest.id in self._loaded:
                        found.append(self._loaded[manifest.id].record)
                        continue
                    if manifest.id in seen:
                        continue
                    rec = PluginRecord(
                        manifest.id,
                        entry.resolve(),
                        PluginState.DISCOVERED,
                        manifest,
                        manifest.id not in self._disabled,
                    )
                except PluginManifestError as exc:
                    pid = exc.plugin_id or entry.name
                    if pid in self._loaded:
                        found.append(self._loaded[pid].record)
                        continue
                    if pid in seen:
                        continue
                    rec = PluginRecord(
                        pid,
                        entry.resolve(),
                        PluginState.INVALID,
                        None,
                        pid not in self._disabled,
                        self._redact(str(exc)),
                    )
                seen.add(rec.plugin_id)
                self._records[rec.plugin_id] = rec
                found.append(rec)
        return found

    def list_plugins(self) -> list[PluginRecord]:
        self.discover()
        return sorted(self._records.values(), key=lambda r: r.plugin_id)

    def get(self, plugin_id: str) -> PluginRecord | None:
        return self._records.get(plugin_id)

    def is_loaded(self, plugin_id: str) -> bool:
        return plugin_id in self._loaded

    def require(self, plugin_id: str) -> PluginRecord:
        if (rec := self.get(plugin_id)) is None:
            raise PluginNotFoundError(f"Plugin not found: {plugin_id}", plugin_id=plugin_id)
        return rec

    def _resolve_target_dir(self, target: str | Path) -> Path:
        if isinstance(target, Path) or "/" in str(target) or "\\" in str(target):
            return Path(target).expanduser().resolve()
        pid = validate_plugin_id(str(target))
        if (rec := self._records.get(pid)) is not None:
            return rec.plugin_dir
        self.discover()
        if (rec := self._records.get(pid)) is not None:
            return rec.plugin_dir
        for root in self._plugin_dirs:
            if (candidate := root / pid).is_dir():
                return candidate.resolve()
        raise PluginNotFoundError(f"Plugin not found: {pid}", plugin_id=pid)

    async def _revoke_all_for(
        self, plugin_id: str, ctx: PluginContext | None, plugin_dir: Path, owned_mods: Iterable[str]
    ) -> None:
        if ctx is not None:
            await ctx.acleanup(plugin_id)
        self.capabilities.revoke_plugin(plugin_id)
        self._domain_adapter.revoke_plugin(plugin_id)
        self.bus.unsubscribe_plugin(plugin_id)
        await self.jobs.cancel_plugin(plugin_id, reason="plugin unloaded")
        _purge_plugin_modules(f"{_PKG_NAMESPACE}.{_pkg_ident(plugin_id)}", plugin_dir, owned_mods)
        env_site = self._plugin_env_site_packages(plugin_id)
        if env_site is not None:
            try:
                sys.path.remove(str(env_site))
            except ValueError:
                pass

    def _plugin_env_site_packages(self, plugin_id: str) -> Path | None:
        """Resolve an isolated ``python-env`` site-packages directory created by the installer."""
        env_json = self.paths.plugin_data_dir(plugin_id) / "python-env" / "env.json"
        try:
            payload = json.loads(env_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        site = payload.get("site_packages") if isinstance(payload, Mapping) else None
        if not isinstance(site, str) or not site.strip():
            return None
        candidate = Path(site)
        return candidate if candidate.is_dir() else None

    async def _load_unlocked(
        self, plugin_dir: Path, *, is_reload: bool = False, isolate: bool = False, prev_reloads: int = 0
    ) -> PluginRecord:
        try:
            manifest = load_manifest(plugin_dir)
        except PluginManifestError as exc:
            pid = exc.plugin_id or plugin_dir.name
            safe = await self._emit_fault(pid, "manifest", exc)
            rec = PluginRecord(pid, plugin_dir, PluginState.INVALID, None, pid not in self._disabled, safe)
            self._records[pid] = rec
            if isolate:
                return rec
            raise

        pid = manifest.id
        if pid in self._loaded and not is_reload:
            await self._unload_unlocked(pid, emit_event=False)
        ctx = PluginContext(
            pid,
            plugin_dir=plugin_dir,
            data_dir=self.paths.plugin_data_dir(pid),
            config_dir=self.paths.config_dir,
            capabilities=self._cap_adapter,
            bus=self.bus,
            domains=self._domain_adapter,
            jobs=self.jobs,
            search=self.search,
            config_store=self.config_store,
            secret_vault=self.secret_vault,
            config_schema=[f.model_dump() for f in manifest.config_schema],
            secret_keys=manifest.secret_keys(),
        )
        owned_mods: set[str] = set()
        env_site = self._plugin_env_site_packages(pid)
        if env_site is not None and str(env_site) not in sys.path:
            sys.path.insert(0, str(env_site))
        try:
            factory, owned_mods = _import_plugin_instance(manifest, plugin_dir)
            instance = factory()
            if inspect.isawaitable(instance):
                instance = await instance
            if not isinstance(instance, BasePlugin) and not callable(getattr(instance, "on_load", None)):
                raise TypeError(f"Entrypoint {manifest.entry!r} must return a BasePlugin instance")
            await invoke_lifecycle_hook(instance.on_load, ctx)
        except asyncio.CancelledError:
            await self._revoke_all_for(pid, ctx, plugin_dir, owned_mods)
            raise
        except Exception as exc:
            await self._revoke_all_for(pid, ctx, plugin_dir, owned_mods)
            safe = await self._emit_fault(pid, "lifecycle:load", exc)
            rec = PluginRecord(
                pid, plugin_dir, PluginState.FAULTED, manifest, pid not in self._disabled, safe, reload_count=prev_reloads
            )
            self._records[pid] = rec
            if isolate:
                return rec
            raise PluginFaultError(safe, plugin_id=pid, cause=exc) from exc

        rec = PluginRecord(
            pid,
            plugin_dir,
            PluginState.LOADED,
            manifest,
            True,
            "",
            time.time(),
            prev_reloads + (1 if is_reload else 0),
        )
        self._set_enabled(pid, True)
        self._loaded[pid] = _LoadedEntry(rec, instance, ctx, owned_mods)
        self._records[pid] = rec
        await self.bus.publish(
            PLUGIN_STATE_CHANGED_TOPIC,
            {"plugin_id": pid, "state": "reloaded" if is_reload else "loaded"},
            source_plugin_id="core",
        )
        return rec

    async def load_plugin(self, plugin_id_or_path: str | Path, *, isolate_errors: bool | None = None) -> PluginRecord:
        """Load a plugin by ID or directory path via importlib."""
        plugin_dir = self._resolve_target_dir(plugin_id_or_path)
        isolate = self._isolate_errors if isolate_errors is None else isolate_errors
        async with self._lock:
            return await self._load_unlocked(plugin_dir, isolate=isolate)

    async def load_all(self) -> list[PluginRecord]:
        """Discover and fault-isolated load all enabled plugins."""
        discovered, results = self.discover(), []
        async with self._lock:
            for item in discovered:
                if item.state == PluginState.INVALID:
                    await self._emit_fault(item.plugin_id, "manifest", item.error or "invalid manifest")
                    results.append(item)
                elif not item.enabled:
                    results.append(item)
                else:
                    results.append(await self._load_unlocked(item.plugin_dir, isolate=True))
        return results

    async def _unload_unlocked(self, plugin_id: str, *, emit_event: bool = True) -> PluginRecord:
        loaded = self._loaded.pop(plugin_id, None)
        if loaded is None:
            rec = self.require(plugin_id)
            await self._revoke_all_for(plugin_id, None, rec.plugin_dir, ())
            rec.state = PluginState.UNLOADED
            return rec
        try:
            await invoke_lifecycle_hook(loaded.instance.on_unload)
        except Exception as exc:
            await self._emit_fault(plugin_id, "lifecycle:unload", exc)
        finally:
            await self._revoke_all_for(plugin_id, loaded.ctx, loaded.record.plugin_dir, loaded.module_names)
        loaded.record.state = PluginState.UNLOADED
        self._records[plugin_id] = loaded.record
        if emit_event:
            await self.bus.publish(
                PLUGIN_STATE_CHANGED_TOPIC, {"plugin_id": plugin_id, "state": "unloaded"}, source_plugin_id="core"
            )
        return loaded.record

    async def unload_plugin(self, plugin_id: str) -> PluginRecord:
        """Unload a loaded plugin and atomically revoke all resources tagged with its plugin_id."""
        pid = validate_plugin_id(plugin_id)
        async with self._lock:
            return await self._unload_unlocked(pid, emit_event=True)

    async def unload_all(self) -> int:
        async with self._lock:
            ids = list(self._loaded)
            try:
                for pid in ids:
                    await self._unload_unlocked(pid, emit_event=True)
            finally:
                self.save_state()
            return len(ids)

    async def reload_plugin(self, plugin_id: str, *, isolate_errors: bool | None = None) -> PluginRecord:
        """Hot-reload a plugin atomically: validate manifest, revoke old resources, re-import via importlib."""
        pid = validate_plugin_id(plugin_id)
        isolate = self._isolate_errors if isolate_errors is None else isolate_errors
        async with self._lock:
            loaded = self._loaded.get(pid)
            plugin_dir = loaded.record.plugin_dir if loaded is not None else self._resolve_target_dir(pid)
            prev_reloads = (
                loaded.record.reload_count
                if loaded is not None
                else (self._records[pid].reload_count if pid in self._records else 0)
            )
            try:
                load_manifest(plugin_dir)
            except PluginManifestError as exc:
                safe = await self._emit_fault(pid, "manifest", exc)
                if not isolate:
                    raise
                self._records[pid] = PluginRecord(
                    pid, plugin_dir, PluginState.INVALID, None, pid not in self._disabled, safe, reload_count=prev_reloads
                )
                return self._records[pid]
            if loaded is not None:
                await self._unload_unlocked(pid, emit_event=False)
            return await self._load_unlocked(plugin_dir, is_reload=True, isolate=isolate, prev_reloads=prev_reloads)

    async def health_check(self, plugin_id: str) -> dict[str, Any]:
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            raise PluginNotFoundError(f"Plugin {plugin_id!r} is not loaded", plugin_id=plugin_id)
        try:
            res = await invoke_lifecycle_hook(loaded.instance.on_health_check)
            return dict(res) if isinstance(res, Mapping) else {"ok": bool(res)}
        except Exception as exc:
            return {"ok": False, "error": await self._emit_fault(plugin_id, "lifecycle:health_check", exc)}

    async def verify_plugin(
        self, plugin_id_or_path: str | Path, *, run_tests: bool = True, require_tests: bool = False
    ) -> PluginVerifyResult:
        """Verify manifest v2, isolated staged-subprocess lifecycle, and offline tests without touching sys.modules."""
        plugin_dir = self._resolve_target_dir(plugin_id_or_path)
        errors: list[str] = []
        try:
            manifest = load_manifest(plugin_dir)
        except PluginManifestError as exc:
            return PluginVerifyResult(
                False, exc.plugin_id or plugin_dir.name, plugin_dir, False, False, False, errors=(self._redact(str(exc)),)
            )

        lifecycle_ok, health = False, {}
        tests_ok, tests_skipped = True, False
        with tempfile.TemporaryDirectory(prefix="omnidesk-verify-") as tmp:
            tmp_root = Path(tmp)
            staged_dir = tmp_root / "staging" / manifest.id
            try:
                shutil.copytree(
                    plugin_dir,
                    staged_dir,
                    symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
                )
            except (OSError, shutil.Error) as exc:
                errors.append(self._redact(f"Failed to stage plugin for verification: {exc}"))
            else:
                lc_ok, lc_health, lc_errors = await _run_lifecycle_in_subprocess(staged_dir, tmp_root)
                lifecycle_ok = lc_ok
                health = lc_health
                errors.extend(self._redact(e) for e in lc_errors)

            if run_tests:
                if not ((plugin_dir / "tests").is_dir() or any(plugin_dir.glob("test_*.py"))):
                    tests_skipped = True
                    if require_tests:
                        tests_ok = False
                        errors.append("No tests directory or test_*.py files found")
                else:
                    ok, err_msg = await _run_verify_cmd(plugin_dir, manifest.verify_cmd, xdg_root=tmp_root / "test-xdg")
                    if not ok:
                        tests_ok = False
                        errors.append(self._redact(err_msg))

        return PluginVerifyResult(
            not errors and lifecycle_ok and tests_ok,
            manifest.id,
            plugin_dir,
            True,
            lifecycle_ok,
            tests_ok,
            tests_skipped,
            health,
            tuple(errors),
        )


_VERIFY_LIFECYCLE_SCRIPT = """
import asyncio, json, os, sys
from pathlib import Path
from omnidesk_core.errors import redact_secrets
from omnidesk_core.manifest import load_manifest
from omnidesk_core.paths import OmnideskPaths
from omnidesk_core.plugins import PluginManager

async def _main() -> int:
    plugin_dir = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    tmp_root = Path(sys.argv[3])
    paths = OmnideskPaths.from_env({
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_root / "cfg"),
        "XDG_DATA_HOME": str(tmp_root / "data"),
        "XDG_STATE_HOME": str(tmp_root / "state"),
        "XDG_CACHE_HOME": str(tmp_root / "cache"),
        "XDG_RUNTIME_DIR": str(tmp_root / "run"),
    })
    paths.ensure_dirs()
    manifest = load_manifest(plugin_dir)
    sandbox = PluginManager(plugins_dir=plugin_dir.parent, paths=paths, isolate_errors=False)
    faults: list[str] = []
    sandbox.bus.subscribe(
        "plugin.fault",
        lambda evt: faults.append(str(evt.payload.get("error", ""))),
        plugin_id="core",
    )
    errors: list[str] = []
    health: dict = {}
    lifecycle_ok = False
    try:
        await sandbox.load_plugin(plugin_dir)
        health = await sandbox.health_check(manifest.id)
        if not isinstance(health, dict):
            health = {"ok": bool(health)}
        if not health.get("ok", True):
            errors.append(f"Health check returned non-ok: {health}")
        await sandbox.unload_plugin(manifest.id)
        if (
            sandbox.capabilities.list_capabilities(plugin_id=manifest.id)
            or sandbox.domains.list_sources(plugin_id=manifest.id)
            or sandbox.workspace_tabs()
            or sandbox.dock_widgets()
            or sandbox.bus.subscription_count(plugin_id=manifest.id)
        ):
            errors.append("Plugin resources remained registered after unload")
        if faults:
            errors.extend(faults)
        lifecycle_ok = not errors
    except BaseException as exc:
        errors.append(redact_secrets(str(exc) or type(exc).__name__, sandbox.secret_vault.secret_values()))
    out_path.write_text(
        json.dumps({"lifecycle_ok": lifecycle_ok, "health": health, "errors": errors}, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0 if lifecycle_ok else 1

raise SystemExit(asyncio.run(_main()))
"""


def _subprocess_python_env(extra_dirs: Sequence[Path], *, xdg_root: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    repo_root = Path(__file__).resolve().parents[2]
    extra_py = [*(str(p) for p in extra_dirs), str(repo_root / "core"), str(repo_root)]
    env["PYTHONPATH"] = os.pathsep.join([*extra_py, env["PYTHONPATH"]] if env.get("PYTHONPATH") else extra_py)
    if xdg_root is not None:
        for name, sub in (
            ("XDG_CONFIG_HOME", "cfg"),
            ("XDG_DATA_HOME", "data"),
            ("XDG_STATE_HOME", "state"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_RUNTIME_DIR", "run"),
        ):
            target = xdg_root / sub
            target.mkdir(parents=True, exist_ok=True)
            env[name] = str(target)
    return env


async def _run_lifecycle_in_subprocess(
    staged_dir: Path, tmp_root: Path, *, timeout: float = 30.0
) -> tuple[bool, dict[str, Any], list[str]]:
    result_path = tmp_root / "lifecycle_result.json"
    env = _subprocess_python_env((staged_dir,), xdg_root=tmp_root / "lc-xdg")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _VERIFY_LIFECYCLE_SCRIPT,
        str(staged_dir),
        str(result_path),
        str(tmp_root / "lc-xdg"),
        cwd=str(staged_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, {}, [f"Lifecycle verification timed out after {timeout:.0f}s"]

    if result_path.is_file():
        try:
            report = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(report, dict):
                lc_ok = bool(report.get("lifecycle_ok")) and proc.returncode == 0
                health = dict(report.get("health") or {}) if isinstance(report.get("health"), dict) else {}
                errs = [str(e) for e in report.get("errors", []) if str(e)]
                if not lc_ok and not errs:
                    errs.append(f"Lifecycle verification exited with code {proc.returncode}")
                return lc_ok, health, errs
        except (OSError, json.JSONDecodeError) as exc:
            return False, {}, [f"Invalid lifecycle verification report: {exc}"]

    output = (stdout_b.decode("utf-8", errors="replace") + "\n" + stderr_b.decode("utf-8", errors="replace")).strip()
    detail = output[-400:] if output else f"rc={proc.returncode}"
    return False, {}, [f"Lifecycle verification subprocess failed without report ({detail})"]


async def _run_verify_cmd(
    plugin_dir: Path, verify_cmd: str, *, xdg_root: Path | None = None, timeout: float = 60.0
) -> tuple[bool, str]:
    tokens = shlex.split(verify_cmd)
    if not tokens:
        return False, "Empty verify_cmd"
    has_pytest = importlib.util.find_spec("pytest") is not None
    is_pytest = (tokens[:3] == ["uv", "run", "pytest"] or tokens[0] == "pytest") and has_pytest
    if is_pytest:
        extra = tokens[3:] if tokens[:3] == ["uv", "run", "pytest"] else tokens[1:]
        has_own_cfg = (plugin_dir / "pyproject.toml").is_file() or (plugin_dir / "pytest.ini").is_file()
        cfg_args = [] if has_own_cfg else ["-c", os.devnull, "--rootdir", str(plugin_dir)]
        test_target = str(plugin_dir / "tests") if (plugin_dir / "tests").is_dir() else str(plugin_dir)
        argv = [sys.executable, "-m", "pytest", *cfg_args, *extra, test_target]
    else:
        argv = tokens

    env = _subprocess_python_env((plugin_dir,), xdg_root=xdg_root)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(plugin_dir), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"verify_cmd timed out after {timeout:.0f}s"
    if proc.returncode == 0:
        return True, ""
    output = (stdout_b.decode("utf-8", errors="replace") + "\n" + stderr_b.decode("utf-8", errors="replace")).strip()
    return False, f"verify_cmd failed (rc={proc.returncode}): {output[-400:]}"


__all__ = [
    "PLUGIN_STATE_CHANGED_TOPIC",
    "PluginManager",
    "PluginRecord",
    "PluginState",
    "PluginVerifyResult",
    "scaffold_plugin",
]
