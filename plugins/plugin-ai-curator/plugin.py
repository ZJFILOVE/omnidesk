"""OmniDesk v2 entry module for `plugin-ai-curator` (`recommend.feed.v1`)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

if __package__:
    from .engine import CurationEngine
    from .models import CuratorError
    from .sources import CuratorBookSource, CuratorMediaSource, CuratorTextSource
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from engine import CurationEngine  # type: ignore[no-redef]
    from models import CuratorError  # type: ignore[no-redef]
    from sources import CuratorBookSource, CuratorMediaSource, CuratorTextSource  # type: ignore[no-redef]


class AiCuratorPlugin(BasePlugin):
    """Hybrid core plugin for dual-repo rehydration and dual-mode curation feeds."""

    def __init__(self) -> None:
        self._engine: CurationEngine | None = None

    async def on_load(self, ctx: PluginContext) -> None:
        """Non-blocking registration of `recommend.feed.v1` and three domain sources."""
        await super().on_load(ctx)
        self._engine = CurationEngine(ctx)
        ctx.register_capability("recommend.feed.v1", self.handle_recommend_feed)
        ctx.register_media_source(CuratorMediaSource(self._engine))
        ctx.register_text_source(CuratorTextSource(self._engine))
        ctx.register_book_source(CuratorBookSource(self._engine))

    async def on_unload(self) -> None:
        """Release in-memory references cleanly prior to PluginContext revocation."""
        self._engine = None

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight local readiness diagnostics without blocking I/O."""
        if self._engine is None:
            return {"ok": False, "error": "not_loaded"}
        snap = self._engine.status_snapshot()
        snap["plugin_id"] = self.ctx.plugin_id if self.ctx is not None else "plugin-ai-curator"
        snap["capabilities"] = ["recommend.feed.v1"]
        snap["sources"] = {"media": "plugin-ai-curator", "text": "plugin-ai-curator", "book": "plugin-ai-curator"}
        return snap

    async def handle_recommend_feed(self, req: Any) -> CapabilityResult:
        """Dispatch `recommend.feed.v1` RPC actions with fail-closed error envelopes."""
        if self._engine is None:
            return CapabilityResult.fail("Plugin is not loaded", error_code="service_offline")
        if not isinstance(req, Mapping):
            return CapabilityResult.fail("Request payload must be a JSON object", error_code="invalid_argument")
        try:
            data = await self._engine.dispatch_action(req)
            return CapabilityResult.ok(data)
        except CuratorError as exc:
            return CapabilityResult.fail(exc.message, error_code=exc.error_code)


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning an `AiCuratorPlugin` instance."""
    return AiCuratorPlugin()
