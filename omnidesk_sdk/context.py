"""PluginContext with plugin_id-scoped resource tracking, subprocess isolation, and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
import sys
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .bus import InMemoryEntityBus
from .models import CapabilityResult, DockWidgetSpec, SubprocessResult, WorkspaceTabSpec
from .registries import InMemoryCapabilityRegistry, InMemoryDomainRegistry, takes_no_pos_args
from .resources import TrackedResources
from .sources import BookSource, MediaSource, TextSource
from .stores import (
    DEFAULT_SECRET_KEYS,
    LocalConfigStore,
    LocalSecretVault,
    _extract_schema_keys,
    _strip_secret_fields,
    validate_plugin_id,
    validate_secret_key,
)
from .subprocess_exec import run_isolated_subprocess, signal_process_group, terminate_process_group

_MISSING = object()


def _filter_supported_kwargs(fn: Callable[..., Any], candidates: Mapping[str, Any]) -> dict[str, Any]:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(candidates)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(candidates)
    return {
        k: v
        for k, v in candidates.items()
        if k in params and params[k].kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


class _CoreStreamBridge:
    """Bridges core `CapabilityStream` onto SDK `CapabilityStream` expectations."""

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


def _wrap_handler_for_foreign_registry(
    handler: Callable[..., Any],
    plugin_id: str,
    core_result_cls: type[Any],
    *,
    streaming: bool,
) -> Callable[..., Any]:
    def _convert(outcome: Any) -> Any:
        if isinstance(outcome, CapabilityResult):
            return core_result_cls(
                ok=outcome.ok,
                data=outcome.data,
                error=outcome.error,
                provider_plugin_id=outcome.provider_plugin_id or plugin_id,
            )
        return outcome

    if streaming and (inspect.isasyncgenfunction(handler) or inspect.isgeneratorfunction(handler)):
        return handler
    if streaming:

        async def _wrapped_stream(req: dict[str, Any], stream: Any) -> Any:
            out = handler(req, _CoreStreamBridge(stream))
            return _convert(await out if inspect.isawaitable(out) else out)

        return _wrapped_stream
    zero_arg = takes_no_pos_args(handler)

    async def _wrapped(req: dict[str, Any]) -> Any:
        out = handler() if (zero_arg and not req) else handler(req)
        return _convert(await out if inspect.isawaitable(out) else out)

    return _wrapped


def _default_plugin_data_dir(plugin_id: str, paths: Any = None) -> Path:
    """Resolve default `paths.data_dir / 'plugins' / plugin_id` without falling back to plugin_dir."""
    if paths is not None and getattr(paths, "data_dir", None) is not None:
        return Path(paths.data_dir).expanduser() / "plugins" / plugin_id
    try:
        from omnidesk_core.paths import OmnideskPaths
    except ImportError:
        raw = os.environ.get("XDG_DATA_HOME", "").strip()
        home_raw = os.environ.get("HOME", "").strip()
        home_dir = Path(home_raw).expanduser() if home_raw else Path.home()
        expanded = Path(raw).expanduser() if raw else None
        base = expanded if (expanded is not None and expanded.is_absolute()) else home_dir / ".local" / "share"
        return base / "omnidesk" / "plugins" / plugin_id
    return OmnideskPaths.from_env().data_dir / "plugins" / plugin_id


class PluginContext:
    """Execution context scoped to a single `plugin_id` with atomic cleanup tracking."""

    def __init__(
        self,
        plugin_id: str,
        *,
        plugin_dir: Path | str | None = None,
        data_dir: Path | str | None = None,
        config_dir: Path | str | None = None,
        paths: Any = None,
        capabilities: Any = None,
        bus: Any = None,
        domains: Any = None,
        jobs: Any = None,
        search: Any = None,
        config_store: Any = None,
        secret_vault: Any = None,
        config_schema: Iterable[Any] | None = None,
        secret_keys: Iterable[str] | None = None,
    ) -> None:
        self._plugin_id = validate_plugin_id(plugin_id)
        self._plugin_dir = Path(plugin_dir).expanduser() if plugin_dir is not None else Path.cwd()
        self._data_dir = (
            Path(data_dir).expanduser()
            if data_dir is not None
            else _default_plugin_data_dir(self._plugin_id, paths)
        )
        self._capabilities = capabilities if capabilities is not None else InMemoryCapabilityRegistry()
        self._bus = bus if bus is not None else InMemoryEntityBus()
        self._domains = domains if domains is not None else InMemoryDomainRegistry()
        self._jobs = jobs
        self._search = search
        self._config_store = config_store if config_store is not None else LocalConfigStore(config_dir)
        self._secret_vault = secret_vault if secret_vault is not None else LocalSecretVault(config_dir)
        self._config_schema = list(config_schema) if config_schema is not None else None
        self._secret_keys: set[str] = {k for k in (secret_keys or ()) if isinstance(k, str) and k}
        self._resources: dict[str, TrackedResources] = {}

        if self._config_schema is not None:
            reg_schema = getattr(self._config_store, "register_schema", None)
            if callable(reg_schema):
                reg_schema(self._plugin_id, self._config_schema)
        if self._secret_keys:
            self._register_secret_keys_on_store(self._secret_keys)
        for registry in (self._capabilities, self._bus):
            add_vault = getattr(registry, "_add_secret_vault", None)
            if callable(add_vault):
                add_vault(self._secret_vault)

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def plugin_dir(self) -> Path:
        return self._plugin_dir

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def capabilities(self) -> Any:
        return self._capabilities

    @property
    def bus(self) -> Any:
        return self._bus

    @property
    def jobs(self) -> Any:
        """Background job center injected by the core runtime (``None`` in bare unit tests)."""
        return self._jobs

    @property
    def search(self) -> Any:
        """Full-text search index injected by the core runtime (``None`` in bare unit tests)."""
        return self._search

    @property
    def domains(self) -> Any:
        return self._domains

    def _bucket(self, plugin_id: str | None = None) -> TrackedResources:
        pid = validate_plugin_id(plugin_id) if plugin_id is not None else self._plugin_id
        return self._resources.setdefault(pid, TrackedResources())

    def _register_secret_keys_on_store(self, keys: Iterable[str]) -> None:
        reg = getattr(self._config_store, "register_secret_keys", None)
        if callable(reg):
            reg(self._plugin_id, keys)

    def _register_cap_dispatch(
        self,
        capability_id: str,
        handler: Callable[..., Any],
        *,
        priority: int,
        metadata: Mapping[str, Any] | None,
        streaming: bool,
    ) -> Any:
        specific_name = "register_streaming_capability" if streaming else "register_capability"
        specific = getattr(self._capabilities, specific_name, None)
        if callable(specific):
            kwargs = _filter_supported_kwargs(
                specific,
                {"plugin_id": self._plugin_id, "priority": priority, "metadata": metadata},
            )
            return specific(capability_id, handler, **kwargs)
        reg = getattr(self._capabilities, "register", None)
        if not callable(reg):
            raise AttributeError("capabilities registry does not expose capability registration")
        reg_mod = sys.modules.get(type(self._capabilities).__module__)
        foreign_result_cls = getattr(reg_mod, "CapabilityResult", None) if reg_mod is not None else None
        target_handler = (
            _wrap_handler_for_foreign_registry(handler, self._plugin_id, foreign_result_cls, streaming=streaming)
            if isinstance(foreign_result_cls, type) and foreign_result_cls is not CapabilityResult
            else handler
        )
        kwargs = _filter_supported_kwargs(
            reg,
            {
                "plugin_id": self._plugin_id,
                "priority": priority,
                "metadata": metadata,
                "streaming": streaming,
            },
        )
        return reg(capability_id, target_handler, **kwargs)

    def register_capability(
        self,
        capability_id: str,
        handler: Callable[..., Any],
        *,
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """Register a capability handler scoped to this plugin_id."""
        handle = self._register_cap_dispatch(
            capability_id, handler, priority=priority, metadata=metadata, streaming=False
        )
        resources = self._bucket()
        if capability_id in resources.streaming_capabilities:
            resources.streaming_capabilities.remove(capability_id)
        if capability_id not in resources.capabilities:
            resources.capabilities.append(capability_id)
        return handle

    def register_streaming_capability(
        self,
        capability_id: str,
        handler: Callable[..., Any],
        *,
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """Register a streaming capability handler scoped to this plugin_id."""
        handle = self._register_cap_dispatch(
            capability_id, handler, priority=priority, metadata=metadata, streaming=True
        )
        resources = self._bucket()
        if capability_id in resources.capabilities:
            resources.capabilities.remove(capability_id)
        if capability_id not in resources.streaming_capabilities:
            resources.streaming_capabilities.append(capability_id)
        return handle

    def _register_domain_source(self, method_name: str, source: Any, bucket_list: list[Any]) -> Any:
        reg_fn = getattr(self._domains, method_name)
        try:
            params = inspect.signature(reg_fn).parameters
        except (TypeError, ValueError):
            params = {}
        pid_param = params.get("plugin_id")
        if pid_param is not None and pid_param.kind == inspect.Parameter.KEYWORD_ONLY:
            raw_sid = getattr(source, "source_id", "")
            if isinstance(raw_sid, str) and raw_sid.strip():
                sid = raw_sid.strip()
            else:
                idx = len(bucket_list) + 1
                sid = self._plugin_id if idx == 1 else f"{self._plugin_id}:{idx}"
            kwargs = _filter_supported_kwargs(reg_fn, {"plugin_id": self._plugin_id, "replace": True})
            reg_fn(sid, source, **kwargs)
        else:
            reg_fn(self._plugin_id, source)
        bucket_list.append(source)
        return source

    def register_media_source(self, source: MediaSource) -> MediaSource:
        """Register a Domain 1 MediaSource tagged with this plugin_id."""
        return self._register_domain_source("register_media_source", source, self._bucket().media_sources)

    def register_text_source(self, source: TextSource) -> TextSource:
        """Register a Domain 2 TextSource tagged with this plugin_id."""
        return self._register_domain_source("register_text_source", source, self._bucket().text_sources)

    def register_book_source(self, source: BookSource) -> BookSource:
        """Register a Domain 3 BookSource tagged with this plugin_id."""
        return self._register_domain_source("register_book_source", source, self._bucket().book_sources)

    def register_workspace_tab(
        self,
        spec: WorkspaceTabSpec | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkspaceTabSpec:
        """Register a declarative WorkspaceTabSpec tagged with this plugin_id."""
        scoped = WorkspaceTabSpec.model_validate(spec if spec is not None else kwargs).model_copy(
            update={"plugin_id": self._plugin_id}
        )
        self._domains.register_workspace_tab(scoped)
        tabs = self._bucket().workspace_tabs
        if scoped.tab_id not in tabs:
            tabs.append(scoped.tab_id)
        return scoped

    def register_dock_widget(
        self,
        spec: DockWidgetSpec | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> DockWidgetSpec:
        """Register a declarative F8 DockWidgetSpec tagged with this plugin_id."""
        scoped = DockWidgetSpec.model_validate(spec if spec is not None else kwargs).model_copy(
            update={"plugin_id": self._plugin_id}
        )
        self._domains.register_dock_widget(scoped)
        widgets = self._bucket().dock_widgets
        if scoped.widget_id not in widgets:
            widgets.append(scoped.widget_id)
        return scoped

    def subscribe(self, topic: str, handler: Callable[[Any], Any]) -> Any:
        """Subscribe to an EntityBus topic tagged with this plugin_id."""
        sub = self._bus.subscribe(topic, handler, plugin_id=self._plugin_id)
        self._bucket().subscriptions.append(sub)
        return sub

    async def emit(self, topic: str, payload: Mapping[str, Any] | Any = None) -> Any:
        """Publish an EntityBus event tagged with `source_plugin_id=self.plugin_id`."""
        fn = getattr(self._bus, "publish", None) or getattr(self._bus, "emit", None)
        if not callable(fn):
            raise AttributeError("bus does not expose publish or emit")
        outcome = fn(topic, payload, source_plugin_id=self._plugin_id)
        return await outcome if inspect.isawaitable(outcome) else outcome

    def spawn_task(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        """Spawn a background asyncio.Task tracked for automatic cancellation on cleanup."""
        task = asyncio.create_task(coro, name=name or f"{self._plugin_id}:task")
        bucket = self._bucket()
        bucket.tasks.add(task)
        task.add_done_callback(bucket.tasks.discard)
        return task

    async def exec_subprocess(
        self,
        args: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin_data: bytes | str | None = None,
        timeout_sec: float | None = None,
        timeout: float | None = None,
        cancel_token: Any = None,
        grace_sec: float = 2.0,
    ) -> SubprocessResult:
        """Execute an external binary with arg-list only (`shell=False`) and process-group cleanup."""
        effective_timeout = timeout_sec if timeout_sec is not None else timeout
        return await run_isolated_subprocess(
            args,
            tracked_set=self._bucket().subprocesses,
            cwd=cwd,
            env=env,
            stdin_data=stdin_data,
            timeout_sec=effective_timeout,
            cancel_token=cancel_token,
            grace_sec=grace_sec,
        )

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Read a secret for this plugin_id from SecretVault."""
        secret_key = validate_secret_key(key)
        getter = getattr(self._secret_vault, "get_secret", None) or getattr(self._secret_vault, "get", None)
        if not callable(getter):
            raise AttributeError("secret_vault does not expose get_secret or get")
        val = getter(self._plugin_id, secret_key)
        return default if val is None else str(val)

    def set_secret(self, key: str, value: str | None) -> None:
        """Store or clear a secret for this plugin_id in SecretVault."""
        secret_key = validate_secret_key(key)
        self._secret_keys.add(secret_key)
        self._register_secret_keys_on_store((secret_key,))
        if value is None:
            deleter = getattr(self._secret_vault, "delete_secret", None) or getattr(
                self._secret_vault, "delete", None
            )
            if callable(deleter):
                deleter(self._plugin_id, secret_key)
                return
        setter = getattr(self._secret_vault, "set_secret", None) or getattr(self._secret_vault, "set", None)
        if not callable(setter):
            raise AttributeError("secret_vault does not expose set_secret or set")
        setter(self._plugin_id, secret_key, value)

    def _effective_secret_keys(self) -> set[str]:
        schema_secrets, schema_plain = _extract_schema_keys(self._config_schema)
        return set(schema_secrets) | self._secret_keys | (DEFAULT_SECRET_KEYS - schema_plain)

    def _call_config_store(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._config_store, method_name, None)
        if not callable(fn):
            raise AttributeError(f"config_store does not expose {method_name}")
        accepted = _filter_supported_kwargs(
            fn,
            {
                "config_schema": self._config_schema,
                "secret_keys": self._secret_keys,
                **kwargs,
            },
        )
        call_args = args
        if args and "secret_keys" not in accepted and "config_schema" not in accepted:
            call_args = tuple(
                _strip_secret_fields(arg, self._effective_secret_keys()) if isinstance(arg, Mapping) else arg
                for arg in args
            )
        result = fn(self._plugin_id, *call_args, **accepted)
        if method_name == "read" and isinstance(result, Mapping) and "secret_keys" not in accepted:
            return _strip_secret_fields(result, self._effective_secret_keys())
        return result

    def get_config(self, key: str | None = None, default: Any = None) -> Any:
        """Read plain configuration (full dict or single key) with secrets stripped."""
        cfg = self._call_config_store("read", apply_defaults=True)
        return dict(cfg) if key is None else cfg.get(key, default)

    def set_config(self, key_or_data: str | Mapping[str, Any], value: Any = _MISSING) -> dict[str, Any]:
        """Update plain configuration by key/value or mapping patch (secrets are stripped)."""
        if isinstance(key_or_data, Mapping):
            if value is not _MISSING:
                raise TypeError("set_config(mapping) does not accept a second positional value")
            patch = dict(key_or_data)
        elif isinstance(key_or_data, str):
            if not key_or_data.strip():
                raise ValueError("config key must be a non-empty string")
            if value is _MISSING:
                raise TypeError("set_config(key, value) requires a value")
            patch = {key_or_data: value}
        else:
            raise TypeError("set_config expects a config key string or mapping")

        if callable(getattr(self._config_store, "update", None)):
            return dict(self._call_config_store("update", patch))
        merged = {**self.get_config(), **patch}
        return dict(self._call_config_store("write", merged))

    def tracked_resources(self, plugin_id: str | None = None) -> dict[str, list[Any]]:
        """Inspect cleanable resources currently recorded for `plugin_id`."""
        pid = validate_plugin_id(plugin_id) if plugin_id is not None else self._plugin_id
        bucket = self._resources.get(pid)
        return bucket.snapshot() if bucket is not None else TrackedResources().snapshot()

    def resource_counts(self, plugin_id: str | None = None) -> dict[str, int]:
        """Return counts of tracked resources for `plugin_id`."""
        return {k: len(v) for k, v in self.tracked_resources(plugin_id).items()}

    def _revoke_registry_resources(self, pid: str, bucket: TrackedResources) -> None:
        cap_unreg = getattr(self._capabilities, "unregister_plugin", None) or getattr(
            self._capabilities, "revoke_plugin", None
        )
        if callable(cap_unreg):
            cap_unreg(pid)
        else:
            single_unreg = getattr(self._capabilities, "unregister", None)
            if callable(single_unreg):
                for cap_id in [*bucket.capabilities, *bucket.streaming_capabilities]:
                    single_unreg(cap_id, plugin_id=pid)

        dom_unreg = getattr(self._domains, "unregister_plugin", None) or getattr(
            self._domains, "revoke_plugin", None
        )
        if callable(dom_unreg):
            dom_unreg(pid)

        bus_unreg = getattr(self._bus, "unsubscribe_plugin", None) or getattr(
            self._bus, "revoke_plugin", None
        )
        if callable(bus_unreg):
            bus_unreg(pid)
        else:
            unsub = getattr(self._bus, "unsubscribe", None)
            if callable(unsub):
                for sub in bucket.subscriptions:
                    unsub(sub)

    def cleanup(self, plugin_id: str | None = None) -> dict[str, int]:
        """Synchronously revoke all tracked resources, cancel tasks, and signal subprocesses for `plugin_id`."""
        pid = validate_plugin_id(plugin_id) if plugin_id is not None else self._plugin_id
        bucket = self._resources.pop(pid, None) or TrackedResources()
        summary = bucket.counts()
        self._revoke_registry_resources(pid, bucket)
        for task in list(bucket.tasks):
            if not task.done():
                task.cancel()
        for proc in list(bucket.subprocesses):
            signal_process_group(proc, signal.SIGTERM)
        return summary

    revoke_all = cleanup

    async def acleanup(self, plugin_id: str | None = None, *, grace_sec: float = 2.0) -> dict[str, int]:
        """Async cleanup awaiting cancelled tasks and graceful SIGTERM->SIGKILL process-group teardown."""
        pid = validate_plugin_id(plugin_id) if plugin_id is not None else self._plugin_id
        bucket = self._resources.pop(pid, None) or TrackedResources()
        summary = bucket.counts()
        self._revoke_registry_resources(pid, bucket)

        for proc in list(bucket.subprocesses):
            with contextlib.suppress(AttributeError):
                prev = getattr(proc, "_omnidesk_cleanup_grace", grace_sec)
                setattr(proc, "_omnidesk_cleanup_grace", min(grace_sec, prev))

        current_task = asyncio.current_task()
        pending_tasks = [t for t in bucket.tasks if not t.done() and t is not current_task]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        remaining_procs = list(bucket.subprocesses)
        if remaining_procs:
            await asyncio.gather(
                *(terminate_process_group(proc, grace_sec=grace_sec) for proc in remaining_procs),
                return_exceptions=True,
            )
        return summary

    aclose = acleanup

    async def __aenter__(self) -> PluginContext:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.acleanup()
