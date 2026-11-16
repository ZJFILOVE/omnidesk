"""Async pub/sub EntityBus with plugin-scoped revocation and fault isolation."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from .errors import InvalidArgumentError, redact_secrets
from .paths import validate_plugin_id

logger = logging.getLogger(__name__)

FAULT_TOPIC = "plugin.fault"


def _normalize_payload(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {"value": payload}


@dataclass(frozen=True, slots=True)
class BusEvent:
    """Immutable event envelope dispatched across EntityBus."""

    topic: str
    payload: dict[str, Any]
    source_plugin_id: str = "core"
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dictionary representation of the event."""
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "payload": dict(self.payload),
            "source_plugin_id": self.source_plugin_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class BusSubscription:
    """Registered EntityBus subscription tagged with its owning plugin_id."""

    subscription_id: str
    topic: str
    plugin_id: str
    handler: Callable[[BusEvent], Any]

    def matches(self, topic: str) -> bool:
        """Return True if this subscription matches the published topic."""
        if "*" in self.topic or "?" in self.topic or "[" in self.topic:
            return fnmatchcase(topic, self.topic)
        return self.topic == topic


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Delivery summary returned by EntityBus.publish."""

    event: BusEvent
    delivered: int
    failed: int
    faults: tuple[dict[str, Any], ...] = ()


class EntityBus:
    """Topic event bus supporting wildcard matching, plugin revocation, and fault isolation."""

    def __init__(
        self,
        *,
        max_history: int = 200,
        secret_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        if max_history < 0:
            raise InvalidArgumentError("max_history must be >= 0")
        self._lock = threading.RLock()
        self._subscriptions: dict[str, BusSubscription] = {}
        self._history: deque[BusEvent] = deque(maxlen=max_history)
        self._max_history = max_history
        self._secret_provider = secret_provider

    def _extra_secrets(self) -> Iterable[str]:
        if self._secret_provider is None:
            return ()
        try:
            return self._secret_provider()
        except Exception:
            return ()

    def subscribe(
        self,
        topic: str,
        handler: Callable[[BusEvent], Any],
        *,
        plugin_id: str = "core",
    ) -> BusSubscription:
        """Register a sync or async subscriber tagged with plugin_id."""
        if not isinstance(topic, str) or not topic.strip():
            raise InvalidArgumentError("Subscription topic must be a non-empty string")
        pid = validate_plugin_id(plugin_id)
        if not callable(handler):
            raise InvalidArgumentError("Subscription handler must be callable")

        sub = BusSubscription(
            subscription_id=uuid.uuid4().hex,
            topic=topic.strip(),
            plugin_id=pid,
            handler=handler,
        )
        with self._lock:
            self._subscriptions[sub.subscription_id] = sub
        return sub

    def unsubscribe(self, subscription: str | BusSubscription) -> bool:
        """Remove a single subscription by ID or BusSubscription handle."""
        sub_id = (
            subscription.subscription_id
            if isinstance(subscription, BusSubscription)
            else subscription
        )
        with self._lock:
            return self._subscriptions.pop(sub_id, None) is not None

    def unsubscribe_plugin(self, plugin_id: str) -> int:
        """Atomically revoke all subscriptions owned by plugin_id."""
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise InvalidArgumentError("plugin_id must be a non-empty string")
        with self._lock:
            to_remove = [
                sub_id
                for sub_id, sub in self._subscriptions.items()
                if sub.plugin_id == plugin_id
            ]
            for sub_id in to_remove:
                self._subscriptions.pop(sub_id, None)
            return len(to_remove)

    revoke_plugin = unsubscribe_plugin

    def list_subscriptions(
        self,
        *,
        plugin_id: str | None = None,
        topic: str | None = None,
    ) -> list[BusSubscription]:
        """List active subscriptions filtered optionally by plugin_id or topic."""
        with self._lock:
            subs = list(self._subscriptions.values())
        if plugin_id is not None:
            subs = [sub for sub in subs if sub.plugin_id == plugin_id]
        if topic is not None:
            subs = [sub for sub in subs if sub.matches(topic)]
        return subs

    def subscription_count(
        self,
        topic: str | None = None,
        *,
        plugin_id: str | None = None,
    ) -> int:
        """Count matching subscriptions."""
        return len(self.list_subscriptions(plugin_id=plugin_id, topic=topic))

    def history(self, topic: str | None = None, *, limit: int | None = None) -> list[BusEvent]:
        """Return recent published events, optionally filtered by topic."""
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise InvalidArgumentError("limit must be an integer >= 0")
        with self._lock:
            events = list(self._history)
        if topic is not None:
            events = [evt for evt in events if evt.topic == topic]
        if limit is not None:
            return events[-limit:] if limit > 0 else []
        return events

    async def publish(
        self,
        topic: str,
        payload: Mapping[str, Any] | Any = None,
        *,
        source_plugin_id: str = "core",
    ) -> PublishResult:
        """Publish an event to matching subscribers with per-subscriber fault isolation."""
        if not isinstance(topic, str) or not topic.strip():
            raise InvalidArgumentError("Publish topic must be a non-empty string")
        clean_topic = topic.strip()
        event = BusEvent(
            topic=clean_topic,
            payload=_normalize_payload(payload),
            source_plugin_id=source_plugin_id,
        )
        with self._lock:
            if self._max_history > 0:
                self._history.append(event)
            # Snapshot matching subscriptions so dynamic subscribe/unsubscribe is safe.
            targets = [sub for sub in self._subscriptions.values() if sub.matches(clean_topic)]
        delivered = 0
        faults: list[dict[str, Any]] = []

        for sub in targets:
            with self._lock:
                if sub.subscription_id not in self._subscriptions:
                    continue
            try:
                outcome = sub.handler(event)
                if inspect.isawaitable(outcome):
                    await outcome
                delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                safe_error = redact_secrets(str(exc) or type(exc).__name__, self._extra_secrets())
                logger.warning(
                    "Isolated EntityBus subscriber fault for plugin=%s topic=%s: %s",
                    sub.plugin_id,
                    clean_topic,
                    safe_error,
                )
                faults.append(
                    {
                        "plugin_id": sub.plugin_id,
                        "component": f"bus:{clean_topic}",
                        "error": safe_error,
                        "subscription_id": sub.subscription_id,
                    }
                )

        # Never recurse when a plugin.fault subscriber itself fails.
        if faults and clean_topic != FAULT_TOPIC:
            for fault in faults:
                await self.publish(
                    FAULT_TOPIC,
                    {
                        "plugin_id": fault["plugin_id"],
                        "component": fault["component"],
                        "error": fault["error"],
                    },
                    source_plugin_id="core",
                )

        return PublishResult(
            event=event,
            delivered=delivered,
            failed=len(faults),
            faults=tuple(faults),
        )

    async def emit_fault(
        self,
        *,
        plugin_id: str,
        component: str,
        error: str | BaseException,
    ) -> PublishResult:
        """Publish a sanitized plugin.fault event onto the bus."""
        raw = error if isinstance(error, str) else (str(error) or type(error).__name__)
        safe_error = redact_secrets(raw or "unknown error", self._extra_secrets())
        return await self.publish(
            FAULT_TOPIC,
            {
                "plugin_id": plugin_id,
                "component": component,
                "error": safe_error,
            },
            source_plugin_id="core",
        )
