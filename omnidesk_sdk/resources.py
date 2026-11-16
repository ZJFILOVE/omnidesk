"""Per-plugin cleanable resource tracking container."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .sources import BookSource, MediaSource, TextSource


@dataclass(slots=True)
class TrackedResources:
    """Cleanable resources recorded per `plugin_id` inside PluginContext."""

    capabilities: list[str] = field(default_factory=list)
    streaming_capabilities: list[str] = field(default_factory=list)
    media_sources: list[MediaSource] = field(default_factory=list)
    text_sources: list[TextSource] = field(default_factory=list)
    book_sources: list[BookSource] = field(default_factory=list)
    workspace_tabs: list[str] = field(default_factory=list)
    dock_widgets: list[str] = field(default_factory=list)
    subscriptions: list[Any] = field(default_factory=list)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    subprocesses: set[asyncio.subprocess.Process] = field(default_factory=set)

    def snapshot(self) -> dict[str, list[Any]]:
        """Return a shallow inspection snapshot of tracked resources."""
        return {
            "capabilities": list(self.capabilities),
            "streaming_capabilities": list(self.streaming_capabilities),
            "media_sources": list(self.media_sources),
            "text_sources": list(self.text_sources),
            "book_sources": list(self.book_sources),
            "workspace_tabs": list(self.workspace_tabs),
            "dock_widgets": list(self.dock_widgets),
            "subscriptions": list(self.subscriptions),
            "tasks": list(self.tasks),
            "subprocesses": list(self.subprocesses),
        }

    def counts(self) -> dict[str, int]:
        """Return counts per tracked resource category."""
        return {k: len(v) for k, v in self.snapshot().items()}
