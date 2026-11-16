"""Minimal OmniDesk Protocol v2 demo plugin providing `demo.echo.v1` and a workspace tab."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, WorkspaceTabSpec

_MAX_HISTORY = 10


class DemoPlugin(BasePlugin):
    """Reference pure-Python plugin demonstrating capability RPC and declarative workspace tabs."""

    def __init__(self) -> None:
        self._echo_count = 0
        self._last_message = ""
        self._last_echo = ""
        self._history: list[dict[str, Any]] = []

    async def on_load(self, ctx: PluginContext) -> None:
        """Register `demo.echo.v1` and the declarative `demo-echo` workspace tab."""
        await super().on_load(ctx)
        ctx.register_capability("demo.echo.v1", self.handle_echo)
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        """Clear transient in-memory state when the plugin unloads."""
        self._echo_count = 0
        self._last_message = ""
        self._last_echo = ""
        self._history.clear()

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight offline health diagnostics."""
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-demo"
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "capabilities": ["demo.echo.v1"],
            "workspace_tabs": ["demo-echo"],
            "echo_count": self._echo_count,
        }

    def _read_options(self) -> tuple[str, bool]:
        if self.ctx is None:
            return "echo", False
        cfg = self.ctx.get_config()
        prefix = str(cfg.get("prefix", "echo"))
        uppercase = bool(cfg.get("uppercase", False))
        return prefix, uppercase

    def _format_echo(self, message: str) -> str:
        prefix, uppercase = self._read_options()
        formatted = message.upper() if uppercase else message
        if not formatted:
            return ""
        return f"{prefix}: {formatted}" if prefix else formatted

    async def handle_echo(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Echo request text/payload using plugin config options."""
        payload = dict(req) if req is not None else {}
        raw_value: Any = ""
        for key in ("message", "text", "value"):
            if key in payload:
                raw_value = payload[key]
                break
        message = "" if raw_value is None else str(raw_value)
        echoed = self._format_echo(message)

        self._echo_count += 1
        self._last_message = message
        self._last_echo = echoed
        self._history.append({"count": self._echo_count, "message": message, "echo": echoed})
        if len(self._history) > _MAX_HISTORY:
            del self._history[: len(self._history) - _MAX_HISTORY]

        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-demo"
        return CapabilityResult.ok(
            {
                "echo": echoed,
                "message": message,
                "payload": payload,
                "count": self._echo_count,
                "plugin_id": plugin_id,
            }
        )

    def _build_ui_schema(self) -> dict[str, Any]:
        prefix, uppercase = self._read_options()
        return {
            "type": "Section",
            "id": "demo-echo-root",
            "title": "Demo Echo Workspace",
            "description": "OmniDesk Protocol v2 声明式工作台页签示例（demo.echo.v1）",
            "children": [
                {
                    "type": "Markdown",
                    "id": "demo-intro",
                    "content": "调用 `demo.echo.v1` 或使用下方表单触发回显并查看状态更新。",
                },
                {
                    "type": "KeyValue",
                    "id": "demo-status",
                    "items": [
                        {"key": "capability", "label": "能力 ID", "value": "demo.echo.v1"},
                        {"key": "prefix", "label": "回显前缀", "value": prefix},
                        {"key": "uppercase", "label": "大写模式", "value": str(uppercase).lower()},
                        {"key": "echo_count", "label": "累计回显次数", "value": str(self._echo_count)},
                        {"key": "last_echo", "label": "最近回显", "value": self._last_echo or "(none)"},
                    ],
                },
                {
                    "type": "Form",
                    "id": "demo-echo-form",
                    "fields": [
                        {
                            "name": "message",
                            "label": "测试消息",
                            "type": "string",
                            "default": self._last_message,
                            "placeholder": "输入要回显的文本",
                        }
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "demo-actions",
                    "actions": [
                        {"id": "echo", "label": "发送 Echo", "variant": "primary"},
                        {"id": "reset", "label": "清空记录", "variant": "secondary"},
                    ],
                },
                {
                    "type": "List",
                    "id": "demo-history",
                    "title": "最近回显记录",
                    "items": [
                        {
                            "id": f"echo-{entry['count']}",
                            "title": entry["echo"] or "(empty)",
                            "subtitle": f"原始输入: {entry['message'] or '(empty)'}",
                        }
                        for entry in reversed(self._history)
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="demo-echo",
            title="Demo Echo",
            domain="core",
            badge="DEMO",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Handle declarative workspace tab actions (`echo`, `reset`)."""
        state_map = dict(state) if state is not None else {}
        if action_id == "echo":
            res = await self.handle_echo({"message": state_map.get("message", "")})
            return {"ok": res.ok, "result": res.data, "ui_schema": self._build_ui_schema()}
        if action_id == "reset":
            self._echo_count = 0
            self._last_message = ""
            self._last_echo = ""
            self._history.clear()
            return {"ok": True, "ui_schema": self._build_ui_schema()}
        return {
            "ok": False,
            "error": f"Unsupported workspace tab action: {action_id}",
            "ui_schema": self._build_ui_schema(),
        }


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `DemoPlugin` instance."""
    return DemoPlugin()
