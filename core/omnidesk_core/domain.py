"""Domain data source registry for Media, Text, and Book workbenches."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .errors import (
    DomainSourceConflictError,
    DomainSourceNotFoundError,
    InvalidArgumentError,
)
from .paths import validate_plugin_id


class DomainKind(StrEnum):
    """Canonical workbench content domains supported by DomainRegistry."""

    MEDIA = "media"
    TEXT = "text"
    BOOK = "book"


def normalize_domain(domain: DomainKind | str) -> DomainKind:
    """Validate and coerce a domain name into a DomainKind enum member."""
    if isinstance(domain, DomainKind):
        return domain
    if isinstance(domain, str):
        cleaned = domain.strip().lower()
        try:
            return DomainKind(cleaned)
        except ValueError:
            pass
    raise InvalidArgumentError(
        f"Unsupported domain {domain!r}; expected one of 'media', 'text', 'book'"
    )


def _validate_source_id(source_id: str) -> str:
    if not isinstance(source_id, str) or not source_id.strip():
        raise InvalidArgumentError("source_id must be a non-empty string")
    cleaned = source_id.strip()
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned or "\x00" in cleaned:
        raise InvalidArgumentError(f"Unsafe source_id {source_id!r}")
    return cleaned


@runtime_checkable
class MediaSource(Protocol):
    """Structural contract for MediaWorkbench sources (`media.source.v1`)."""

    def categories(self) -> Any: ...
    def fetch_items(self, category: str = "", query: str = "", page: int = 1) -> Any: ...
    def fetch_chapters(self, item_id: str) -> Any: ...
    def fetch_lyrics(self, item_id: str) -> Any: ...


@runtime_checkable
class TextSource(Protocol):
    """Structural contract for TextWorkbench sources (`text.source.v1`)."""

    category: str

    def channels(self) -> Any: ...
    def fetch_feed(self, channel_id: str = "", query: str = "", page: int = 1) -> Any: ...
    def fetch_article(self, article_id: str) -> Any: ...


@runtime_checkable
class BookSource(Protocol):
    """Structural contract for BookWorkbench sources (`book.source.v1`)."""

    def list_books(self, format_filter: str = "", query: str = "") -> Any: ...
    def fetch_spread(self, book_id: str, page_number: int = 1) -> Any: ...
    def list_vocab(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class DomainSourceRegistration:
    """Registered domain source entry tagged with owning `plugin_id`."""

    domain: DomainKind
    source_id: str
    plugin_id: str
    source: Any
    title: str = ""
    category: str = ""
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    registered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize registration metadata without exposing the live source object."""
        return {
            "domain": str(self.domain),
            "source_id": self.source_id,
            "plugin_id": self.plugin_id,
            "title": self.title,
            "category": self.category,
            "priority": self.priority,
            "metadata": dict(self.metadata),
            "registered_at": self.registered_at,
        }


class DomainRegistry:
    """Thread-safe registry of media, text, and book sources with plugin-scoped revocation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[DomainKind, str], tuple[int, DomainSourceRegistration]] = {}
        self._seq = 0

    def register(
        self,
        domain: DomainKind | str,
        source_id: str,
        source: Any,
        *,
        plugin_id: str = "core",
        title: str = "",
        category: str = "",
        priority: int = 0,
        metadata: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> DomainSourceRegistration:
        """Register a domain source keyed by `(domain, source_id)` and tagged with `plugin_id`."""
        kind = normalize_domain(domain)
        sid = _validate_source_id(source_id)
        pid = validate_plugin_id(plugin_id)
        if source is None:
            raise InvalidArgumentError("Domain source instance must not be None")

        resolved_title = title or str(
            getattr(source, "title", "") or getattr(source, "display_name", "") or getattr(source, "name", "") or sid
        )
        resolved_category = category or str(getattr(source, "category", "") or "")
        entry = DomainSourceRegistration(
            domain=kind,
            source_id=sid,
            plugin_id=pid,
            source=source,
            title=resolved_title,
            category=resolved_category,
            priority=int(priority),
            metadata=dict(metadata) if metadata else {},
        )
        key = (kind, sid)
        with self._lock:
            existing_pair = self._entries.get(key)
            if existing_pair is not None:
                _, existing = existing_pair
                if not replace or existing.plugin_id != pid:
                    raise DomainSourceConflictError(
                        f"Domain source {sid!r} in domain {str(kind)!r} is already registered "
                        f"by plugin {existing.plugin_id!r}",
                        domain=str(kind),
                        source_id=sid,
                        details={"plugin_id": pid, "existing_plugin_id": existing.plugin_id},
                    )
            self._seq += 1
            self._entries[key] = (self._seq, entry)
        return entry

    register_source = register

    def register_media_source(
        self,
        source_id: str,
        source: MediaSource | Any,
        *,
        plugin_id: str = "core",
        title: str = "",
        category: str = "",
        priority: int = 0,
        metadata: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> DomainSourceRegistration:
        """Register a MediaWorkbench source under domain `'media'`."""
        return self.register(
            DomainKind.MEDIA,
            source_id,
            source,
            plugin_id=plugin_id,
            title=title,
            category=category,
            priority=priority,
            metadata=metadata,
            replace=replace,
        )

    def register_text_source(
        self,
        source_id: str,
        source: TextSource | Any,
        *,
        plugin_id: str = "core",
        title: str = "",
        category: str = "",
        priority: int = 0,
        metadata: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> DomainSourceRegistration:
        """Register a TextWorkbench source under domain `'text'`."""
        return self.register(
            DomainKind.TEXT,
            source_id,
            source,
            plugin_id=plugin_id,
            title=title,
            category=category,
            priority=priority,
            metadata=metadata,
            replace=replace,
        )

    def register_book_source(
        self,
        source_id: str,
        source: BookSource | Any,
        *,
        plugin_id: str = "core",
        title: str = "",
        category: str = "",
        priority: int = 0,
        metadata: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> DomainSourceRegistration:
        """Register a BookWorkbench source under domain `'book'`."""
        return self.register(
            DomainKind.BOOK,
            source_id,
            source,
            plugin_id=plugin_id,
            title=title,
            category=category,
            priority=priority,
            metadata=metadata,
            replace=replace,
        )

    def unregister(
        self,
        domain: DomainKind | str,
        source_id: str,
        *,
        plugin_id: str | None = None,
    ) -> bool:
        """Unregister a single `(domain, source_id)` source, checking ownership if `plugin_id` is given."""
        kind = normalize_domain(domain)
        sid = _validate_source_id(source_id)
        key = (kind, sid)
        with self._lock:
            pair = self._entries.get(key)
            if pair is None:
                return False
            _, existing = pair
            if plugin_id is not None and existing.plugin_id != plugin_id:
                raise DomainSourceConflictError(
                    f"Cannot unregister domain source {sid!r} ({str(kind)}) owned by {existing.plugin_id!r}",
                    domain=str(kind),
                    source_id=sid,
                    details={"plugin_id": plugin_id, "existing_plugin_id": existing.plugin_id},
                )
            self._entries.pop(key, None)
            return True

    unregister_source = unregister

    def revoke_plugin(self, plugin_id: str) -> int:
        """Atomically revoke all media, text, and book sources owned by `plugin_id`."""
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise InvalidArgumentError("plugin_id must be a non-empty string")
        with self._lock:
            targets = [key for key, (_, entry) in self._entries.items() if entry.plugin_id == plugin_id]
            for key in targets:
                self._entries.pop(key, None)
            return len(targets)

    unregister_plugin = revoke_plugin

    def has(self, domain: DomainKind | str, source_id: str) -> bool:
        """Return True if `(domain, source_id)` is registered."""
        kind = normalize_domain(domain)
        sid = source_id.strip() if isinstance(source_id, str) else source_id
        with self._lock:
            return (kind, sid) in self._entries

    def get(self, domain: DomainKind | str, source_id: str) -> DomainSourceRegistration | None:
        """Return registration metadata for `(domain, source_id)` or None."""
        kind = normalize_domain(domain)
        sid = source_id.strip() if isinstance(source_id, str) else source_id
        with self._lock:
            pair = self._entries.get((kind, sid))
            return pair[1] if pair is not None else None

    def resolve(self, domain: DomainKind | str, source_id: str) -> DomainSourceRegistration:
        """Return registration for `(domain, source_id)` or raise DomainSourceNotFoundError."""
        kind = normalize_domain(domain)
        entry = self.get(kind, source_id)
        if entry is None:
            raise DomainSourceNotFoundError(
                f"Domain source not found: {str(kind)}/{source_id}",
                domain=str(kind),
                source_id=source_id,
            )
        return entry

    def get_source(self, domain: DomainKind | str, source_id: str) -> Any:
        """Return the underlying source instance for `(domain, source_id)` or raise DomainSourceNotFoundError."""
        return self.resolve(domain, source_id).source

    def list_sources(
        self,
        domain: DomainKind | str | None = None,
        *,
        plugin_id: str | None = None,
        category: str | None = None,
    ) -> list[DomainSourceRegistration]:
        """List registered domain sources ordered by descending priority then registration order."""
        kind = normalize_domain(domain) if domain is not None else None
        with self._lock:
            pairs = list(self._entries.values())
        if kind is not None:
            pairs = [p for p in pairs if p[1].domain == kind]
        if plugin_id is not None:
            pairs = [p for p in pairs if p[1].plugin_id == plugin_id]
        if category is not None:
            pairs = [p for p in pairs if p[1].category == category]
        pairs.sort(key=lambda item: (-item[1].priority, item[0]))
        return [entry for _, entry in pairs]

    def list_media_sources(self, *, plugin_id: str | None = None) -> list[DomainSourceRegistration]:
        """List all registered MediaWorkbench sources."""
        return self.list_sources(DomainKind.MEDIA, plugin_id=plugin_id)

    def list_text_sources(
        self, *, plugin_id: str | None = None, category: str | None = None
    ) -> list[DomainSourceRegistration]:
        """List all registered TextWorkbench sources."""
        return self.list_sources(DomainKind.TEXT, plugin_id=plugin_id, category=category)

    def list_book_sources(self, *, plugin_id: str | None = None) -> list[DomainSourceRegistration]:
        """List all registered BookWorkbench sources."""
        return self.list_sources(DomainKind.BOOK, plugin_id=plugin_id)

    def counts(self) -> dict[str, int]:
        """Return source counts per domain."""
        summary = {str(kind): 0 for kind in DomainKind}
        with self._lock:
            for _, entry in self._entries.values():
                summary[str(entry.domain)] += 1
        return summary
