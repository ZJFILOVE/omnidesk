"""BasePlugin lifecycle contract for pure Python OmniDesk plugins."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import PluginContext


class BasePlugin:
    """Base class for all OmniDesk v2 pure Python plugins.

    All lifecycle hooks (`on_load`, `on_unload`, `on_health_check`) are optional.
    Subclasses may override only the hooks they need.
    """

    ctx: PluginContext | None = None

    async def on_load(self, ctx: PluginContext) -> None:
        """Called once on plugin activation; keep non-blocking (< 50 ms)."""
        self.ctx = ctx

    async def on_unload(self) -> None:
        """Optional cleanup hook called prior to PluginContext resource revocation."""

    async def on_health_check(self) -> dict[str, Any]:
        """Optional health check hook used by verification gates and diagnostics."""
        return {"ok": True}


async def invoke_lifecycle_hook(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke a sync or async plugin lifecycle hook uniformly."""
    outcome = fn(*args, **kwargs)
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome
