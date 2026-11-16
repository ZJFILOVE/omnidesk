"""In-memory EntityBus mirroring `omnidesk_core.bus.EntityBus` semantics."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from .stores import SecretRedactor, validate_plugin_id


@dataclass(frozen=True, slots=True)
class BusEvent:
    """Event envelope delivered by InMemoryEntityBus."""

    topic: str
    payload: dict[str, Any]
    source_plugin_id: str = "core"
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "payload": dict(self.payload),
            "source_plugin_id": self.source_plugin_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class BusSubscription:
    """Subscription handle tagged with owning plugin_id."""

    subscription_id: str
    topic: str
    plugin_id: str
    handler: Callable[[BusEvent], Any]

    def matches(self, topic: str) -> bool:
        if "*" in self.topic or "?" in self.topic or "[" in self.topic:
            return fnmatchcase(topic, self.topic)
        return self.topic == topic


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Delivery summary returned by InMemoryEntityBus.publish."""

    event: BusEvent
    delivered: int
    failed: int
    faults: tuple[dict[str, Any], ...] = ()


class InMemoryEntityBus:
    """Standalone EntityBus supporting wildcard matching, plugin revocation, and fault isolation."""

    def __init__(
        self,
        *,
        max_history: int = 200,
        secret_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        if max_history < 0:
            raise ValueError("max_history must be >= 0")
        self._subscriptions: dict[str, BusSubscription] = {}
        self._history: deque[BusEvent] = deque(maxlen=max_history)
        self._max_history = max_history
        self._redactor = SecretRedactor(secret_provider)

    def _add_secret_vault(self, vault: Any) -> None:
        self._redactor.add_vault(vault)

    def subscribe(
        self,
        topic: str,
        handler: Callable[[BusEvent], Any],
        *,
        plugin_id: str = "core",
    ) -> BusSubscription:
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("topic must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        pid = validate_plugin_id(plugin_id)
        sub = BusSubscription(
            subscription_id=uuid.uuid4().hex,
            topic=topic.strip(),
            plugin_id=pid,
            handler=handler,
        )
        self._subscriptions[sub.subscription_id] = sub
        return sub

    def unsubscribe(self, subscription: str | BusSubscription) -> bool:
        sub_id = subscription.subscription_id if isinstance(subscription, BusSubscription) else subscription
        return self._subscriptions.pop(sub_id, None) is not None

    def unsubscribe_plugin(self, plugin_id: str) -> int:
        to_remove = [sub_id for sub_id, sub in self._subscriptions.items() if sub.plugin_id == plugin_id]
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
        return len(self.list_subscriptions(plugin_id=plugin_id, topic=topic))

    def history(self, topic: str | None = None, *, limit: int | None = None) -> list[BusEvent]:
        events = list(self._history)
        if topic is not None:
            events = [evt for evt in events if evt.topic == topic]
        if limit is not None and limit >= 0:
            return events[-limit:] if limit > 0 else []
        return events

    async def publish(
        self,
        topic: str,
        payload: Mapping[str, Any] | Any = None,
        *,
        source_plugin_id: str = "core",
    ) -> PublishResult:
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("topic must be a non-empty string")
        clean_topic = topic.strip()
        model_dump = getattr(payload, "model_dump", None)
        if payload is None:
            norm_payload: dict[str, Any] = {}
        elif isinstance(payload, Mapping):
            norm_payload = dict(payload)
        elif callable(model_dump) and isinstance((dumped := model_dump()), Mapping):
            norm_payload = dict(dumped)
        else:
            norm_payload = {"value": payload}
        event = BusEvent(topic=clean_topic, payload=norm_payload, source_plugin_id=source_plugin_id)
        if self._max_history > 0:
            self._history.append(event)

        delivered = 0
        faults: list[dict[str, Any]] = []
        for sub in [s for s in self._subscriptions.values() if s.matches(clean_topic)]:
            try:
                outcome = sub.handler(event)
                if inspect.isawaitable(outcome):
                    await outcome
                delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                safe_error = self._redactor.redact(str(exc) or type(exc).__name__)
                faults.append(
                    {
                        "plugin_id": sub.plugin_id,
                        "component": f"bus:{clean_topic}",
                        "error": safe_error,
                        "subscription_id": sub.subscription_id,
                    }
                )
        return PublishResult(event=event, delivered=delivered, failed=len(faults), faults=tuple(faults))
