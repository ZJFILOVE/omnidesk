"""Domain data source base interfaces and streaming capability writer."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from .models import (
    BookItem,
    BookSpread,
    ChapterSegment,
    LyricLine,
    MediaItem,
    TextArticle,
    TextChannel,
    TextFeedItem,
    VocabCard,
)


@runtime_checkable
class CapabilityStream(Protocol):
    """Streaming sink passed to handlers registered via `register_streaming_capability`."""

    @property
    def is_cancelled(self) -> bool: ...

    async def emit_delta(
        self,
        text: str,
        *,
        event: str = "delta",
        data: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def emit_event(self, event: str, data: Mapping[str, Any] | None = None) -> None: ...


class BufferedCapabilityStream:
    """Default CapabilityStream forwarding chunks to an optional callback while buffering events."""

    def __init__(
        self,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._on_chunk = on_chunk
        self._is_cancelled_cb = is_cancelled
        self.events: list[dict[str, Any]] = []
        self.deltas: list[str] = []

    @property
    def is_cancelled(self) -> bool:
        return bool(self._is_cancelled_cb()) if self._is_cancelled_cb is not None else False

    async def emit_delta(
        self,
        text: str,
        *,
        event: str = "delta",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit an incremental text delta chunk."""
        self.deltas.append(text)
        payload = dict(data) if data is not None else {}
        payload["text"] = text
        await self.emit_event(event, payload)

    async def emit_event(self, event: str, data: Mapping[str, Any] | None = None) -> None:
        """Emit a structured stream event."""
        chunk = {"event": event, "data": dict(data) if data is not None else {}}
        self.events.append(chunk)
        if self._on_chunk is not None:
            outcome = self._on_chunk(chunk)
            if inspect.isawaitable(outcome):
                await outcome


class MediaSource:
    """Domain 1 (Media & Music) data provider base class."""

    source_id: str = ""
    display_name: str = ""

    def categories(self) -> list[str]:
        """Return available media categories/tabs for this source."""
        return []

    async def fetch_items(
        self,
        category: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[MediaItem]:
        """Fetch paginated MediaItem entries."""
        raise NotImplementedError(f"{type(self).__name__}.fetch_items is not implemented")

    async def fetch_chapters(self, item_id: str) -> list[ChapterSegment]:
        """Return optional timeline chapters/segments for a media item."""
        return []

    async def fetch_lyrics(self, item_id: str) -> list[LyricLine]:
        """Return optional bilingual synchronized lyrics for a track."""
        return []


class TextSource:
    """Domain 2 (Text, Communities & Scientific Journals) data provider base class."""

    source_id: str = ""
    display_name: str = ""
    category: str = "blog"

    def channels(self) -> list[TextChannel]:
        """Return dynamic sub-channels exposed by this text source."""
        return []

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        """Fetch paginated feed items for a channel/query."""
        raise NotImplementedError(f"{type(self).__name__}.fetch_feed is not implemented")

    async def fetch_article(self, article_id: str) -> TextArticle:
        """Fetch full structured bilingual article and comments."""
        raise NotImplementedError(f"{type(self).__name__}.fetch_article is not implemented")


class BookSource:
    """Domain 3 (Books, BabelDOC PDF & EPUB Companion) data provider base class."""

    source_id: str = ""
    display_name: str = ""

    async def list_books(
        self,
        format_filter: str = "",
        query: str = "",
    ) -> list[BookItem]:
        """List books matching optional format and search filters."""
        raise NotImplementedError(f"{type(self).__name__}.list_books is not implemented")

    async def fetch_spread(self, book_id: str, page_number: int = 1) -> BookSpread:
        """Fetch bilingual spread for the given book and page number."""
        raise NotImplementedError(f"{type(self).__name__}.fetch_spread is not implemented")

    async def list_vocab(self) -> list[VocabCard]:
        """Return SM-2 vocabulary cards associated with this source."""
        return []
