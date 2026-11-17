"""Versioned capability registry with sync, async, streaming dispatch, and plugin revocation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .bus import EntityBus
from .errors import (
    CapabilityConflictError,
    CapabilityError,
    CapabilityInvocationError,
    CapabilityNotFoundError,
    ErrorCode,
    InvalidArgumentError,
    InvalidCapabilityIdError,
    OmniDeskError,
    redact_secrets,
)
from .paths import validate_plugin_id

try:
    from omnidesk_sdk.models import CapabilityResult as _SdkCapabilityResult
except ImportError:  # pragma: no cover - optional fallback if SDK isn't on sys.path
    _SdkCapabilityResult = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)
CAPABILITY_ID_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+\.v[0-9]+\Z")


@dataclass(frozen=True, slots=True)
class ParsedCapabilityId:
    """Decomposed versioned capability identifier (`<ns>.<name>.v<n>`)."""

    raw: str
    namespace: str
    name: str
    base_id: str
    version: int


def parse_capability_id(capability_id: str) -> ParsedCapabilityId:
    """Validate and decompose `<ns>.<name>.v<n>` capability IDs."""
    if not isinstance(capability_id, str) or not CAPABILITY_ID_RE.match(capability_id):
        raise InvalidCapabilityIdError(
            capability_id if isinstance(capability_id, str) else repr(capability_id),
            "must match ^[a-z0-9_]+(\\.[a-z0-9_]+)+\\.v[0-9]+$",
        )
    parts = capability_id.split(".")
    base = parts[:-1]
    return ParsedCapabilityId(capability_id, base[0], ".".join(base[1:]), ".".join(base), int(parts[-1][1:]))


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump) and isinstance((dumped := model_dump()), Mapping):
        return dict(dumped)
    return None


def _coerce_payload_mapping(data: Any, extra_data: Mapping[str, Any]) -> dict[str, Any]:
    if data is None:
        payload: dict[str, Any] = {}
    else:
        mapped = _as_mapping(data)
        if mapped is None:
            raise TypeError("CapabilityResult payload must be a mapping, BaseModel, or None")
        payload = mapped
    if extra_data:
        payload.update(extra_data)
    return payload


def _normalize_request(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    if payload is None:
        return {}
    mapped = _as_mapping(payload)
    return mapped if mapped is not None else {"value": payload}


class _OkAccessor:
    """Descriptor exposing `res.ok` on instances and `CapabilityResult.ok(...)` on the class."""

    def __get__(self, instance: CapabilityResult | None, owner: type[CapabilityResult]) -> Any:
        return owner._ok_factory if instance is None else instance._ok


class CapabilityResult:
    """Immutable cross-plugin RPC response envelope."""

    __slots__ = ("_ok", "data", "error", "provider_plugin_id")
    ok = _OkAccessor()

    def __init__(
        self, ok: bool = True, data: Mapping[str, Any] | Any = None, error: str = "", provider_plugin_id: str = ""
    ) -> None:
        object.__setattr__(self, "_ok", bool(ok))
        object.__setattr__(self, "data", _coerce_payload_mapping(data, {}))
        object.__setattr__(self, "error", str(error))
        object.__setattr__(self, "provider_plugin_id", str(provider_plugin_id))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("CapabilityResult is immutable")

    @classmethod
    def _ok_factory(
        cls,
        data: Mapping[str, Any] | Any = None,
        *,
        provider_plugin_id: str = "",
        error: str = "",
        **extra_data: Any,
    ) -> CapabilityResult:
        return cls(ok=True, data=_coerce_payload_mapping(data, extra_data), error=error, provider_plugin_id=provider_plugin_id)

    @classmethod
    def fail(
        cls,
        error: str,
        data: Mapping[str, Any] | Any = None,
        *,
        error_code: str | ErrorCode = "",
        provider_plugin_id: str = "",
        **extra_data: Any,
    ) -> CapabilityResult:
        """Build a failed CapabilityResult carrying an optional machine `error_code`."""
        payload = _coerce_payload_mapping(data, extra_data)
        if error_code:
            payload["error_code"] = str(error_code)
        return cls(ok=False, data=payload, error=error, provider_plugin_id=provider_plugin_id)

    def with_provider(self, plugin_id: str) -> CapabilityResult:
        return self if self.provider_plugin_id else CapabilityResult(self._ok, self.data, self.error, plugin_id)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self._ok, "data": dict(self.data), "error": self.error, "provider_plugin_id": self.provider_plugin_id}

    model_dump = to_dict

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CapabilityResult) and (
            self._ok, self.data, self.error, self.provider_plugin_id
        ) == (other._ok, other.data, other.error, other.provider_plugin_id)

    def __repr__(self) -> str:
        return f"CapabilityResult(ok={self._ok!r}, data={self.data!r}, error={self.error!r}, provider_plugin_id={self.provider_plugin_id!r})"


@dataclass(frozen=True, slots=True)
class CapabilityStreamChunk:
    """Single item yielded by a streaming capability invocation."""

    event: str
    data: Any = None
    provider_plugin_id: str = ""
    done: bool = False
    result: CapabilityResult | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"event": self.event, "data": self.data, "provider_plugin_id": self.provider_plugin_id, "done": self.done}
        if self.result is not None:
            out["result"] = self.result.to_dict()
        return out


class CapabilityStream:
    """Producer-side handle passed to callback-style streaming capability handlers."""

    def __init__(
        self,
        provider_plugin_id: str,
        queue: asyncio.Queue[CapabilityStreamChunk | None] | None,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._provider_plugin_id = provider_plugin_id
        self._queue = queue
        self._loop = loop
        self._lock = threading.Lock()
        self._chunks: list[CapabilityStreamChunk] = []
        self._pending: deque[CapabilityStreamChunk] = deque()
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    @property
    def chunks(self) -> tuple[CapabilityStreamChunk, ...]:
        with self._lock:
            return tuple(self._chunks)

    def _flush_pending(self) -> None:
        if self._queue is None:
            return
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        for item in pending:
            self._queue.put_nowait(item)

    def emit_sync(self, event: str, data: Any = None) -> None:
        if not isinstance(event, str) or not event.strip():
            raise InvalidArgumentError("Stream event name must be a non-empty string")
        chunk = CapabilityStreamChunk(event=event.strip(), data=data, provider_plugin_id=self._provider_plugin_id)
        if self._queue is None:
            with self._lock:
                self._chunks.append(chunk)
            return
        if self._loop is not None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not self._loop:
                with self._lock:
                    self._chunks.append(chunk)
                    self._pending.append(chunk)
                if not self._loop.is_closed():
                    try:
                        self._loop.call_soon_threadsafe(self._flush_pending)
                    except RuntimeError:
                        pass
                return
        with self._lock:
            self._chunks.append(chunk)
        self._flush_pending()
        self._queue.put_nowait(chunk)

    async def emit(self, event: str, data: Any = None) -> None:
        self.emit_sync(event, data)

    async def emit_event(self, event: str, data: Mapping[str, Any] | None = None) -> None:
        await self.emit(event, dict(data) if data is not None else {})

    async def emit_delta(
        self,
        delta: str | Mapping[str, Any],
        *,
        event: str = "delta",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if data is not None:
            await self.emit(event, {**dict(data), "text": delta})
        else:
            await self.emit(event, delta)


@dataclass(frozen=True, slots=True)
class CapabilityRegistration:
    """Registered capability metadata and handler binding."""

    capability_id: str
    base_id: str
    namespace: str
    name: str
    version: int
    plugin_id: str
    handler: Callable[..., Any]
    streaming: bool = False
    description: str = ""
    registered_at: float = field(default_factory=time.time)
    priority: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id, "base_id": self.base_id, "namespace": self.namespace,
            "name": self.name, "version": self.version, "plugin_id": self.plugin_id,
            "streaming": self.streaming, "description": self.description, "registered_at": self.registered_at,
            "priority": self.priority, "metadata": dict(self.metadata),
        }


def _normalize_result(value: Any, plugin_id: str) -> CapabilityResult:
    if isinstance(value, CapabilityResult):
        return value.with_provider(plugin_id)
    if _SdkCapabilityResult is not None and isinstance(value, _SdkCapabilityResult):
        return CapabilityResult(
            ok=value.ok,
            data=value.data,
            error=value.error,
            provider_plugin_id=value.provider_plugin_id or plugin_id,
        )
    if value is None:
        return CapabilityResult(ok=True, data={}, provider_plugin_id=plugin_id)
    mapped = _as_mapping(value)
    return CapabilityResult(ok=True, data=mapped if mapped is not None else {"value": value}, provider_plugin_id=plugin_id)


def _is_chunk_iterator(value: Any) -> bool:
    return not isinstance(value, (str, bytes, bytearray, Mapping, CapabilityResult)) and isinstance(value, Iterator)


def _unwrap_callable(handler: Callable[..., Any]) -> Callable[..., Any]:
    if not isinstance(handler, type) and not inspect.isroutine(handler) and not isinstance(handler, functools.partial):
        call_attr = getattr(handler, "__call__", None)
        if callable(call_attr):
            return call_attr
    return handler


def _is_coroutine_handler(handler: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(_unwrap_callable(handler))


def _call_mode(handler: Callable[..., Any], *, streaming: bool) -> str:
    target = _unwrap_callable(handler)
    if inspect.isasyncgenfunction(handler) or inspect.isasyncgenfunction(target):
        return "async_gen"
    if inspect.isgeneratorfunction(handler) or inspect.isgeneratorfunction(target):
        return "sync_gen"
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return "unary"
    pos = [p for p in sig.parameters.values() if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    stream_param = sig.parameters.get("stream")
    if streaming and stream_param is not None and stream_param.kind == inspect.Parameter.KEYWORD_ONLY:
        return "stream_cb_kw"
    if streaming and (stream_param is not None or len(pos) >= 2 or varargs):
        return "stream_cb"
    return "zero_arg" if (not pos and not varargs) else "unary"


def _validate_timeout(timeout: float | int | None) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidArgumentError(f"timeout must be a finite number > 0, got {timeout!r}")
    val = float(timeout)
    if not math.isfinite(val) or val <= 0.0:
        raise InvalidArgumentError(f"timeout must be a finite number > 0, got {timeout!r}")
    return val


async def _close_async_iter(iterator: Any) -> None:
    aclose = getattr(iterator, "aclose", None)
    if callable(aclose):
        outcome = aclose()
        if inspect.isawaitable(outcome):
            await outcome


def _close_sync_iter(iterator: Any) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        close()


class CapabilityRegistry:
    """Versioned capability RPC registry with sync/async/streaming dispatch and plugin revocation."""

    def __init__(
        self, *, bus: EntityBus | None = None, isolate_errors: bool = False, secret_provider: Callable[[], Iterable[str]] | None = None
    ) -> None:
        self._bus, self._isolate_errors, self._secret_provider = bus, isolate_errors, secret_provider
        self._lock = threading.RLock()
        self._entries: dict[str, list[CapabilityRegistration]] = {}

    def _extra_secrets(self) -> Iterable[str]:
        if self._secret_provider is None:
            return ()
        try:
            return self._secret_provider()
        except Exception:
            return ()

    def register(
        self,
        capability_id: str,
        handler: Callable[..., Any],
        *,
        plugin_id: str = "core",
        streaming: bool = False,
        replace: bool = False,
        description: str = "",
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityRegistration:
        """Register a versioned capability handler owned by `plugin_id`."""
        parsed = parse_capability_id(capability_id)
        pid = validate_plugin_id(plugin_id)
        if not callable(handler):
            raise InvalidArgumentError("Capability handler must be callable")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise InvalidArgumentError("priority must be an integer")
        entry = CapabilityRegistration(
            parsed.raw, parsed.base_id, parsed.namespace, parsed.name, parsed.version,
            pid, handler, bool(streaming), description,
            priority=priority, metadata=dict(metadata) if metadata else {},
        )
        with self._lock:
            bucket = self._entries.get(parsed.raw, [])
            for existing in bucket:
                if existing.plugin_id == pid:
                    if not replace:
                        raise CapabilityConflictError(
                            parsed.raw, plugin_id=pid, existing_plugin_id=existing.plugin_id
                        )
                    break
            new_bucket = [e for e in bucket if e.plugin_id != pid]
            new_bucket.append(entry)
            new_bucket.sort(key=lambda item: item.priority, reverse=True)
            self._entries[parsed.raw] = new_bucket
        return entry

    def register_streaming(
        self,
        capability_id: str,
        handler: Callable[..., Any],
        *,
        plugin_id: str = "core",
        replace: bool = False,
        description: str = "",
        priority: int = 100,
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityRegistration:
        """Register a streaming capability handler owned by `plugin_id`."""
        return self.register(
            capability_id,
            handler,
            plugin_id=plugin_id,
            streaming=True,
            replace=replace,
            description=description,
            priority=priority,
            metadata=metadata,
        )

    def _remove_plugin_entries(self, capability_id: str, plugin_id: str) -> int:
        entries = self._entries.get(capability_id)
        if not entries:
            return 0
        kept = [entry for entry in entries if entry.plugin_id != plugin_id]
        removed = len(entries) - len(kept)
        if not removed:
            return 0
        if kept:
            self._entries[capability_id] = kept
        else:
            self._entries.pop(capability_id, None)
        return removed

    def unregister(self, capability_id: str, *, plugin_id: str | None = None) -> bool:
        """Remove registrations for `capability_id`, scoped to `plugin_id` when supplied."""
        with self._lock:
            if plugin_id is None:
                return bool(self._entries.pop(capability_id, None))
            return self._remove_plugin_entries(capability_id, plugin_id) > 0

    def revoke_plugin(self, plugin_id: str) -> int:
        """Atomically revoke all capabilities owned by `plugin_id`."""
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise InvalidArgumentError("plugin_id must be a non-empty string")
        with self._lock:
            return sum(self._remove_plugin_entries(cid, plugin_id) for cid in list(self._entries))

    unregister_plugin = revoke_plugin

    def has(self, capability_id: str) -> bool:
        with self._lock:
            return bool(self._entries.get(capability_id))

    def __contains__(self, capability_id: object) -> bool:
        return isinstance(capability_id, str) and self.has(capability_id)

    def get(self, capability_id: str) -> CapabilityRegistration | None:
        with self._lock:
            entries = self._entries.get(capability_id)
            return entries[0] if entries else None

    def resolve(self, capability_id: str, *, plugin_id: str | None = None) -> CapabilityRegistration:
        """Validate versioned ID and return a provider registration or raise CapabilityNotFoundError."""
        parsed = parse_capability_id(capability_id)
        if plugin_id:
            with self._lock:
                entries = list(self._entries.get(parsed.raw, ()))
            if (entry := next((e for e in entries if e.plugin_id == plugin_id), None)) is None:
                raise CapabilityNotFoundError(
                    parsed.raw,
                    details={"provider_plugin_id": plugin_id, "providers": [e.plugin_id for e in entries]},
                )
            return entry
        if (entry := self.get(parsed.raw)) is None:
            raise CapabilityNotFoundError(parsed.raw)
        return entry

    def providers_for(self, capability_id: str) -> list[CapabilityRegistration]:
        """Return all registered providers for `capability_id` in priority resolution order."""
        with self._lock:
            return list(self._entries.get(capability_id, ()))

    def list_capabilities(
        self, *, plugin_id: str | None = None, prefix: str | None = None, streaming: bool | None = None
    ) -> list[CapabilityRegistration]:
        with self._lock:
            entries = [entry for bucket in self._entries.values() for entry in bucket]
        if plugin_id is not None:
            entries = [e for e in entries if e.plugin_id == plugin_id]
        if prefix is not None:
            entries = [e for e in entries if e.capability_id.startswith(prefix)]
        if streaming is not None:
            entries = [e for e in entries if e.streaming is streaming]
        return entries

    def list_versions(self, base_id: str) -> list[CapabilityRegistration]:
        with self._lock:
            entries = [bucket[0] for bucket in self._entries.values() if bucket and bucket[0].base_id == base_id]
        return sorted(entries, key=lambda e: e.version)

    def latest_version(self, base_id: str) -> CapabilityRegistration | None:
        versions = self.list_versions(base_id)
        return versions[-1] if versions else None

    def _prepare(
        self,
        capability_id: str,
        payload: Mapping[str, Any] | Any,
        timeout: float | None,
        isolate_errors: bool | None,
        *,
        plugin_id: str | None = None,
    ) -> tuple[CapabilityRegistration, dict[str, Any], bool, str, float | None]:
        effective_timeout = _validate_timeout(timeout)
        entry = self.resolve(capability_id, plugin_id=plugin_id)
        isolate = self._isolate_errors if isolate_errors is None else isolate_errors
        mode = _call_mode(entry.handler, streaming=entry.streaming)
        return entry, _normalize_request(payload), isolate, mode, effective_timeout

    def _safe_error(self, exc: BaseException) -> tuple[str, str]:
        if isinstance(exc, TimeoutError):
            return redact_secrets(str(exc) or "capability timed out", self._extra_secrets()), str(ErrorCode.TIMEOUT)
        if isinstance(exc, OmniDeskError):
            return redact_secrets(exc.message or str(exc) or type(exc).__name__, self._extra_secrets()), exc.code
        return redact_secrets(str(exc) or type(exc).__name__, self._extra_secrets()), str(ErrorCode.PLUGIN_FAULT)

    async def _emit_fault_async(self, entry: CapabilityRegistration, safe_error: str) -> None:
        if self._bus is not None:
            await self._bus.emit_fault(plugin_id=entry.plugin_id, component=f"capability:{entry.capability_id}", error=safe_error)

    def _build_fault_outcome(self, entry: CapabilityRegistration, exc: BaseException, *, isolate: bool) -> CapabilityResult:
        safe_error, code = self._safe_error(exc)
        logger.warning("Capability %s faulted (plugin=%s): %s", entry.capability_id, entry.plugin_id, safe_error)
        if isolate:
            return CapabilityResult.fail(safe_error, error_code=code, provider_plugin_id=entry.plugin_id)
        raise CapabilityInvocationError(entry.capability_id, entry.plugin_id, safe_error, code=code, cause=exc) from exc

    async def _finalize_raw_outcome(self, raw: Any, entry: CapabilityRegistration) -> CapabilityResult:
        if inspect.isawaitable(raw):
            raw = await raw
        if isinstance(raw, AsyncIterator):
            try:
                chunks = [item async for item in raw]
            finally:
                await _close_async_iter(raw)
            return CapabilityResult.ok({"chunks": chunks}, provider_plugin_id=entry.plugin_id)
        if _is_chunk_iterator(raw):
            try:
                chunks = list(raw)
            finally:
                _close_sync_iter(raw)
            return CapabilityResult.ok({"chunks": chunks}, provider_plugin_id=entry.plugin_id)
        return _normalize_result(raw, entry.plugin_id)

    async def _call_entry(
        self,
        entry: CapabilityRegistration,
        req: dict[str, Any],
        *,
        timeout: float | None,
        isolate: bool,
        mode: str,
    ) -> CapabilityResult:
        stream: CapabilityStream | None = None
        try:
            async with asyncio.timeout(timeout):
                if mode in ("stream_cb", "stream_cb_kw"):
                    stream = CapabilityStream(entry.plugin_id, queue=None, loop=asyncio.get_running_loop())
                    raw = entry.handler(req, stream=stream) if mode == "stream_cb_kw" else entry.handler(req, stream)
                    res = _normalize_result(await raw if inspect.isawaitable(raw) else raw, entry.plugin_id)
                    if stream.chunks and "chunks" not in res.data:
                        return CapabilityResult(res.ok, {**res.data, "chunks": [c.data for c in stream.chunks]}, res.error, res.provider_plugin_id)
                    return res
                raw = entry.handler() if mode == "zero_arg" and not req else entry.handler(req)
                return await self._finalize_raw_outcome(raw, entry)
        except (asyncio.CancelledError, CapabilityError):
            if stream is not None:
                stream._cancel()
            raise
        except Exception as exc:
            if stream is not None:
                stream._cancel()
            await self._emit_fault_async(entry, self._safe_error(exc)[0])
            return self._build_fault_outcome(entry, exc, isolate=isolate)

    async def call(
        self,
        capability_id: str,
        payload: Mapping[str, Any] | Any = None,
        *,
        timeout: float | None = None,
        isolate_errors: bool | None = None,
        plugin_id: str | None = None,
    ) -> CapabilityResult:
        """Invoke a sync or async capability asynchronously."""
        entry, req, isolate, mode, effective_timeout = self._prepare(
            capability_id, payload, timeout, isolate_errors, plugin_id=plugin_id
        )
        return await self._call_entry(entry, req, timeout=effective_timeout, isolate=isolate, mode=mode)

    def call_sync(
        self,
        capability_id: str,
        payload: Mapping[str, Any] | Any = None,
        *,
        timeout: float | None = None,
        isolate_errors: bool | None = None,
        plugin_id: str | None = None,
    ) -> CapabilityResult:
        """Invoke a capability synchronously, bridging awaitables when required."""
        entry, req, isolate, mode, effective_timeout = self._prepare(
            capability_id, payload, timeout, isolate_errors, plugin_id=plugin_id
        )
        if mode in ("async_gen", "stream_cb", "stream_cb_kw") or _is_coroutine_handler(entry.handler):
            return self._run_coro_sync(self._call_entry(entry, req, timeout=effective_timeout, isolate=isolate, mode=mode))
        try:
            raw = entry.handler() if mode == "zero_arg" and not req else entry.handler(req)
            if inspect.isawaitable(raw) or isinstance(raw, AsyncIterator):
                async def _await_raw() -> CapabilityResult:
                    try:
                        async with asyncio.timeout(effective_timeout):
                            return await self._finalize_raw_outcome(raw, entry)
                    except (asyncio.CancelledError, CapabilityError):
                        raise
                    except Exception as exc:
                        await self._emit_fault_async(entry, self._safe_error(exc)[0])
                        return self._build_fault_outcome(entry, exc, isolate=isolate)
                return self._run_coro_sync(_await_raw())
            if _is_chunk_iterator(raw):
                try:
                    chunks = list(raw)
                finally:
                    _close_sync_iter(raw)
                return CapabilityResult.ok({"chunks": chunks}, provider_plugin_id=entry.plugin_id)
            return _normalize_result(raw, entry.plugin_id)
        except CapabilityError:
            raise
        except Exception as exc:
            if self._bus is not None:
                safe_error = self._safe_error(exc)[0]
                self._run_coro_sync(self._emit_fault_async(entry, safe_error))
            return self._build_fault_outcome(entry, exc, isolate=isolate)

    @staticmethod
    def _run_coro_sync(coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                fut = pool.submit(asyncio.run, coro)
            except BaseException:
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
                raise
            return fut.result()

    async def stream(
        self,
        capability_id: str,
        payload: Mapping[str, Any] | Any = None,
        *,
        timeout: float | None = None,
        isolate_errors: bool | None = None,
        plugin_id: str | None = None,
    ) -> AsyncIterator[CapabilityStreamChunk]:
        """Invoke a capability and yield live CapabilityStreamChunk items."""
        entry, req, isolate, mode, effective_timeout = self._prepare(
            capability_id, payload, timeout, isolate_errors, plugin_id=plugin_id
        )
        if mode in ("stream_cb", "stream_cb_kw"):
            inner = self._stream_callback(
                entry, req, timeout=effective_timeout, isolate=isolate, kw_stream=(mode == "stream_cb_kw")
            )
            try:
                async for chunk in inner:
                    yield chunk
            finally:
                await inner.aclose()
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + effective_timeout if effective_timeout is not None else None
        raw: Any = None
        try:
            if deadline is not None and loop.time() >= deadline:
                raise TimeoutError("capability timed out")
            async with asyncio.timeout_at(deadline):
                raw = entry.handler() if mode == "zero_arg" and not req else entry.handler(req)
                if inspect.isawaitable(raw):
                    raw = await raw
            if isinstance(raw, AsyncIterator):
                it = aiter(raw)
                try:
                    while True:
                        if deadline is not None and loop.time() >= deadline:
                            raise TimeoutError("capability timed out")
                        try:
                            async with asyncio.timeout_at(deadline):
                                item = await anext(it)
                        except StopAsyncIteration:
                            break
                        yield self._wrap_chunk(item, entry.plugin_id)
                finally:
                    await _close_async_iter(raw)
                return
            if _is_chunk_iterator(raw):
                try:
                    for item in raw:
                        if deadline is not None and loop.time() >= deadline:
                            raise TimeoutError("capability timed out")
                        yield self._wrap_chunk(item, entry.plugin_id)
                finally:
                    _close_sync_iter(raw)
                return
            res = _normalize_result(raw, entry.plugin_id)
            yield CapabilityStreamChunk("result", res.data, res.provider_plugin_id, done=True, result=res)
        except (asyncio.CancelledError, CapabilityError):
            raise
        except Exception as exc:
            yield await self._emit_and_fail_chunk(entry, exc, isolate=isolate)

    async def call_stream(
        self,
        capability_id: str,
        payload: Mapping[str, Any] | Any = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        *,
        timeout: float | None = None,
        isolate_errors: bool | None = None,
    ) -> CapabilityResult:
        """Invoke a capability via `stream()`, forwarding chunks to `on_chunk` and returning CapabilityResult."""
        final_res: CapabilityResult | None = None
        chunks: list[Any] = []
        provider_id = ""
        async for chunk in self.stream(capability_id, payload, timeout=timeout, isolate_errors=isolate_errors):
            if not provider_id and chunk.provider_plugin_id:
                provider_id = chunk.provider_plugin_id
            if chunk.done and chunk.result is not None:
                final_res = chunk.result
            else:
                chunks.append(chunk.data)
                if on_chunk is not None:
                    outcome = on_chunk({"event": chunk.event, "data": chunk.data})
                    if inspect.isawaitable(outcome):
                        await outcome
        if final_res is not None:
            return final_res
        return CapabilityResult.ok({"chunks": chunks} if chunks else {}, provider_plugin_id=provider_id)

    async def _emit_and_fail_chunk(self, entry: CapabilityRegistration, exc: BaseException, *, isolate: bool) -> CapabilityStreamChunk:
        safe_error, code = self._safe_error(exc)
        await self._emit_fault_async(entry, safe_error)
        if not isolate:
            self._build_fault_outcome(entry, exc, isolate=False)
        fail_res = CapabilityResult.fail(safe_error, error_code=code, provider_plugin_id=entry.plugin_id)
        return CapabilityStreamChunk("error", fail_res.data, entry.plugin_id, done=True, result=fail_res)

    async def _stream_callback(
        self,
        entry: CapabilityRegistration,
        req: dict[str, Any],
        *,
        timeout: float | None,
        isolate: bool,
        kw_stream: bool = False,
    ) -> AsyncIterator[CapabilityStreamChunk]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        queue: asyncio.Queue[CapabilityStreamChunk | None] = asyncio.Queue()
        stream = CapabilityStream(entry.plugin_id, queue=queue, loop=loop)
        final_result: CapabilityResult | None = None
        failure: BaseException | None = None

        async def _producer() -> None:
            nonlocal final_result, failure
            try:
                raw = entry.handler(req, stream=stream) if kw_stream else entry.handler(req, stream)
                final_result = _normalize_result(await raw if inspect.isawaitable(raw) else raw, entry.plugin_id)
            except BaseException as exc:
                failure = exc
            finally:
                stream._flush_pending()
                await queue.put(None)

        task = asyncio.create_task(_producer())
        try:
            while True:
                if deadline is not None and loop.time() >= deadline:
                    raise TimeoutError("capability timed out")
                async with asyncio.timeout_at(deadline):
                    item = await queue.get()
                if item is None:
                    break
                yield item
            if deadline is not None and not task.done():
                if loop.time() >= deadline:
                    raise TimeoutError("capability timed out")
                async with asyncio.timeout_at(deadline):
                    await task
            else:
                await task
        except TimeoutError as exc:
            stream._cancel()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            yield await self._emit_and_fail_chunk(entry, exc, isolate=isolate)
            return
        except BaseException:
            stream._cancel()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

        if failure is not None:
            if not isinstance(failure, Exception) or isinstance(failure, CapabilityError):
                raise failure
            yield await self._emit_and_fail_chunk(entry, failure, isolate=isolate)
            return
        assert final_result is not None
        yield CapabilityStreamChunk("result", final_result.data, final_result.provider_plugin_id, done=True, result=final_result)

    @staticmethod
    def _wrap_chunk(item: Any, plugin_id: str) -> CapabilityStreamChunk:
        if isinstance(item, CapabilityStreamChunk):
            return item if item.provider_plugin_id else CapabilityStreamChunk(
                item.event, item.data, plugin_id, item.done, item.result
            )
        return CapabilityStreamChunk(event="delta", data=item, provider_plugin_id=plugin_id)
