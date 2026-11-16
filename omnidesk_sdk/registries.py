"""Standalone in-memory capability and domain registries scoped by `plugin_id`."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .bus import BusEvent, BusSubscription, InMemoryEntityBus, PublishResult
from .models import CapabilityResult, ContentDomain, DockWidgetSpec, WorkspaceTabSpec
from .sources import BookSource, BufferedCapabilityStream, MediaSource, TextSource
from .stores import SecretRedactor, validate_plugin_id


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Metadata describing a registered capability provider."""

    capability_id: str
    plugin_id: str
    priority: int = 100
    streaming: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _CapabilityEntry:
    descriptor: CapabilityDescriptor
    handler: Callable[..., Any]


def takes_no_pos_args(fn: Callable[..., Any]) -> bool:
    try:
        kinds = (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        return not any(p.kind in kinds for p in inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False


def _is_chunk_iterator(value: Any) -> bool:
    return not isinstance(value, (str, bytes, bytearray, Mapping, CapabilityResult)) and isinstance(value, Iterator)


async def _emit_stream_chunk(stream: BufferedCapabilityStream, item: Any) -> None:
    if isinstance(item, Mapping):
        await stream.emit_event("delta", item)
    elif isinstance(item, str):
        await stream.emit_delta(item)
    else:
        await stream.emit_event("delta", {"value": item})


async def _collect_async_stream(aiter: AsyncIterator[Any], stream: BufferedCapabilityStream) -> list[Any]:
    chunks: list[Any] = []
    try:
        async for item in aiter:
            chunks.append(item)
            await _emit_stream_chunk(stream, item)
    finally:
        aclose = getattr(aiter, "aclose", None)
        if callable(aclose):
            await aclose()
    return chunks


async def _collect_sync_stream(iterator: Iterator[Any], stream: BufferedCapabilityStream) -> list[Any]:
    chunks: list[Any] = []
    for item in iterator:
        chunks.append(item)
        await _emit_stream_chunk(stream, item)
    return chunks


class InMemoryCapabilityRegistry:
    """Lightweight capability registry supporting priority routing, streaming, and revocation."""

    def __init__(self, *, secret_provider: Callable[[], Iterable[str]] | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, list[_CapabilityEntry]] = {}
        self._redactor = SecretRedactor(secret_provider)

    def _add_secret_vault(self, vault: Any) -> None:
        self._redactor.add_vault(vault)

    def register(
        self,
        capability_id: str,
        handler: Callable[..., Any],
        *,
        plugin_id: str = "core",
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
        streaming: bool = False,
    ) -> CapabilityDescriptor:
        """Register a regular or streaming capability handler tagged with `plugin_id`."""
        if not isinstance(capability_id, str) or not capability_id.strip() or any(c.isspace() for c in capability_id):
            raise ValueError(f"Invalid capability_id {capability_id!r}")
        if not callable(handler):
            raise TypeError("Capability handler must be callable")
        pid = validate_plugin_id(plugin_id)
        desc = CapabilityDescriptor(
            capability_id=capability_id,
            plugin_id=pid,
            priority=priority,
            streaming=streaming,
            metadata=dict(metadata) if metadata else {},
        )
        entry = _CapabilityEntry(descriptor=desc, handler=handler)
        with self._lock:
            bucket = [
                existing
                for existing in self._entries.get(capability_id, [])
                if existing.descriptor.plugin_id != pid
            ]
            bucket.append(entry)
            bucket.sort(key=lambda item: item.descriptor.priority, reverse=True)
            self._entries[capability_id] = bucket
        return desc

    def has(self, capability_id: str) -> bool:
        with self._lock:
            return bool(self._entries.get(capability_id))

    def providers_for(self, capability_id: str) -> list[CapabilityDescriptor]:
        with self._lock:
            return [entry.descriptor for entry in self._entries.get(capability_id, ())]

    def list_all(self) -> list[CapabilityDescriptor]:
        with self._lock:
            return [entry.descriptor for entries in self._entries.values() for entry in entries]

    def unregister(self, capability_id: str, *, plugin_id: str | None = None) -> int:
        with self._lock:
            entries = self._entries.get(capability_id)
            if not entries:
                return 0
            if plugin_id is None:
                removed = len(entries)
                self._entries.pop(capability_id, None)
                return removed
            kept = [entry for entry in entries if entry.descriptor.plugin_id != plugin_id]
            removed = len(entries) - len(kept)
            if kept:
                self._entries[capability_id] = kept
            else:
                self._entries.pop(capability_id, None)
            return removed

    def unregister_plugin(self, plugin_id: str) -> int:
        removed_total = 0
        with self._lock:
            for cap_id in list(self._entries):
                removed_total += self.unregister(cap_id, plugin_id=plugin_id)
        return removed_total

    revoke_plugin = unregister_plugin

    def _resolve_entry(self, capability_id: str, plugin_id: str | None) -> _CapabilityEntry | None:
        with self._lock:
            entries = self._entries.get(capability_id, ())
            if plugin_id is None:
                return entries[0] if entries else None
            for entry in entries:
                if entry.descriptor.plugin_id == plugin_id:
                    return entry
            return None

    @staticmethod
    def _coerce_result(outcome: Any, plugin_id: str) -> CapabilityResult:
        if isinstance(outcome, CapabilityResult):
            return outcome.with_provider(plugin_id)
        if (
            isinstance(getattr(outcome, "ok", None), bool)
            and isinstance(getattr(outcome, "data", None), Mapping)
            and isinstance(getattr(outcome, "error", None), str)
            and callable(getattr(outcome, "to_dict", None))
        ):
            return CapabilityResult(
                ok=outcome.ok,
                data=dict(outcome.data),
                error=outcome.error,
                provider_plugin_id=str(getattr(outcome, "provider_plugin_id", "") or plugin_id),
            )
        if outcome is None:
            return CapabilityResult.ok({}, provider_plugin_id=plugin_id)
        if isinstance(outcome, Mapping):
            return CapabilityResult.ok(outcome, provider_plugin_id=plugin_id)
        model_dump = getattr(outcome, "model_dump", None)
        if callable(model_dump) and isinstance((dumped := model_dump()), Mapping):
            return CapabilityResult.ok(dumped, provider_plugin_id=plugin_id)
        return CapabilityResult.ok({"value": outcome}, provider_plugin_id=plugin_id)

    async def _invoke(
        self,
        capability_id: str,
        request: Mapping[str, Any] | None,
        *,
        plugin_id: str | None,
        stream: BufferedCapabilityStream | None,
    ) -> CapabilityResult:
        entry = self._resolve_entry(capability_id, plugin_id)
        if entry is None:
            return CapabilityResult.fail(
                f"Capability not found: {capability_id}",
                error_code="capability_not_found",
            )
        req = dict(request) if request is not None else {}
        try:
            if entry.descriptor.streaming:
                active_stream = stream if stream is not None else BufferedCapabilityStream()
                if inspect.isasyncgenfunction(entry.handler):
                    chunks = await _collect_async_stream(entry.handler(req), active_stream)
                    return CapabilityResult.ok({"chunks": chunks}, provider_plugin_id=entry.descriptor.plugin_id)
                if inspect.isgeneratorfunction(entry.handler):
                    chunks = await _collect_sync_stream(entry.handler(req), active_stream)
                    return CapabilityResult.ok({"chunks": chunks}, provider_plugin_id=entry.descriptor.plugin_id)
                outcome = entry.handler(req, active_stream)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                if isinstance(outcome, AsyncIterator):
                    chunks = await _collect_async_stream(outcome, active_stream)
                    return CapabilityResult.ok({"chunks": chunks}, provider_plugin_id=entry.descriptor.plugin_id)
                if _is_chunk_iterator(outcome):
                    chunks = await _collect_sync_stream(outcome, active_stream)
                    return CapabilityResult.ok({"chunks": chunks}, provider_plugin_id=entry.descriptor.plugin_id)
                return self._coerce_result(outcome, entry.descriptor.plugin_id)

            outcome = entry.handler() if (takes_no_pos_args(entry.handler) and not req) else entry.handler(req)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return self._coerce_result(outcome, entry.descriptor.plugin_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_error = self._redactor.redact(str(exc) or type(exc).__name__)
            return CapabilityResult.fail(
                safe_error,
                error_code="plugin_fault",
                provider_plugin_id=entry.descriptor.plugin_id,
            )

    async def call(
        self,
        capability_id: str,
        request: Mapping[str, Any] | None = None,
        *,
        plugin_id: str | None = None,
    ) -> CapabilityResult:
        """Invoke a capability with fault isolation."""
        return await self._invoke(capability_id, request, plugin_id=plugin_id, stream=None)

    async def call_stream(
        self,
        capability_id: str,
        request: Mapping[str, Any] | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        *,
        plugin_id: str | None = None,
    ) -> CapabilityResult:
        """Invoke a streaming capability passing a CapabilityStream writer."""
        return await self._invoke(
            capability_id,
            request,
            plugin_id=plugin_id,
            stream=BufferedCapabilityStream(on_chunk=on_chunk),
        )


@dataclass(frozen=True, slots=True)
class _TaggedEntry:
    plugin_id: str
    item: Any


class InMemoryDomainRegistry:
    """Domain source and declarative UI registry scoped by `plugin_id`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._media_sources: list[_TaggedEntry] = []
        self._text_sources: list[_TaggedEntry] = []
        self._book_sources: list[_TaggedEntry] = []
        self._workspace_tabs: list[WorkspaceTabSpec] = []
        self._dock_widgets: list[DockWidgetSpec] = []

    def register_media_source(self, plugin_id: str, source: MediaSource) -> MediaSource:
        pid = validate_plugin_id(plugin_id)
        if source is None:
            raise ValueError("source must not be None")
        with self._lock:
            self._media_sources.append(_TaggedEntry(pid, source))
        return source

    def register_text_source(self, plugin_id: str, source: TextSource) -> TextSource:
        pid = validate_plugin_id(plugin_id)
        if source is None:
            raise ValueError("source must not be None")
        with self._lock:
            self._text_sources.append(_TaggedEntry(pid, source))
        return source

    def register_book_source(self, plugin_id: str, source: BookSource) -> BookSource:
        pid = validate_plugin_id(plugin_id)
        if source is None:
            raise ValueError("source must not be None")
        with self._lock:
            self._book_sources.append(_TaggedEntry(pid, source))
        return source

    def register_workspace_tab(self, spec: WorkspaceTabSpec) -> WorkspaceTabSpec:
        pid = validate_plugin_id(spec.plugin_id)
        with self._lock:
            self._workspace_tabs = [
                tab for tab in self._workspace_tabs if not (tab.plugin_id == pid and tab.tab_id == spec.tab_id)
            ]
            self._workspace_tabs.append(spec)
        return spec

    def register_dock_widget(self, spec: DockWidgetSpec) -> DockWidgetSpec:
        pid = validate_plugin_id(spec.plugin_id)
        with self._lock:
            self._dock_widgets = [
                w for w in self._dock_widgets if not (w.plugin_id == pid and w.widget_id == spec.widget_id)
            ]
            self._dock_widgets.append(spec)
        return spec

    def media_sources(self) -> list[MediaSource]:
        with self._lock:
            return [entry.item for entry in self._media_sources]

    def text_sources(self) -> list[TextSource]:
        with self._lock:
            return [entry.item for entry in self._text_sources]

    def book_sources(self) -> list[BookSource]:
        with self._lock:
            return [entry.item for entry in self._book_sources]

    def workspace_tabs(self, domain: ContentDomain | None = None) -> list[WorkspaceTabSpec]:
        with self._lock:
            if domain is None:
                return list(self._workspace_tabs)
            return [tab for tab in self._workspace_tabs if tab.domain == domain]

    def dock_widgets(self, domain: ContentDomain | None = None) -> list[DockWidgetSpec]:
        with self._lock:
            if domain is None:
                return list(self._dock_widgets)
            return [w for w in self._dock_widgets if domain in w.supported_domains]

    def unregister_plugin(self, plugin_id: str) -> int:
        with self._lock:
            before = (
                len(self._media_sources)
                + len(self._text_sources)
                + len(self._book_sources)
                + len(self._workspace_tabs)
                + len(self._dock_widgets)
            )
            self._media_sources = [e for e in self._media_sources if e.plugin_id != plugin_id]
            self._text_sources = [e for e in self._text_sources if e.plugin_id != plugin_id]
            self._book_sources = [e for e in self._book_sources if e.plugin_id != plugin_id]
            self._workspace_tabs = [t for t in self._workspace_tabs if t.plugin_id != plugin_id]
            self._dock_widgets = [w for w in self._dock_widgets if w.plugin_id != plugin_id]
            after = (
                len(self._media_sources)
                + len(self._text_sources)
                + len(self._book_sources)
                + len(self._workspace_tabs)
                + len(self._dock_widgets)
            )
            return before - after

    revoke_plugin = unregister_plugin


__all__ = [
    "BusEvent",
    "BusSubscription",
    "CapabilityDescriptor",
    "InMemoryCapabilityRegistry",
    "InMemoryDomainRegistry",
    "InMemoryEntityBus",
    "PublishResult",
]
