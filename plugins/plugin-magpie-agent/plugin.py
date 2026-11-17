"""Stage 2 Magpie AI provider plugin (`llm.router.v1`, `llm.chat.v1`, `llm.translate.v1`, `ai.agent.v1`, `ai.curation.v1`)."""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, SubprocessResult


def _import_sibling(name: str) -> Any:
    if __package__:
        return importlib.import_module(f".{name}", package=__package__)
    plugin_dir = str(Path(__file__).resolve().parent)
    added = plugin_dir not in sys.path
    if added:
        sys.path.insert(0, plugin_dir)
    try:
        return importlib.import_module(name)
    finally:
        if added and plugin_dir in sys.path:
            sys.path.remove(plugin_dir)


_common = _import_sibling("magpie_common")
_router = _import_sibling("magpie_router")
_router_actions = _import_sibling("magpie_router_actions")
_gateway_mod = _import_sibling("magpie_gateway")
_chat_mod = _import_sibling("magpie_chat")
_agent_mod = _import_sibling("magpie_agent_actions")
_runtime_mod = _import_sibling("agent_runtime")
_curation_mod = _import_sibling("magpie_curation")
_context_mod = _import_sibling("magpie_context")
_sessions_mod = _import_sibling("magpie_sessions")

DEFAULT_GATEWAY_URL = _common.DEFAULT_GATEWAY_URL
DEFAULT_GROUP_ID = _common.DEFAULT_GROUP_ID
_strip_plugin_log_lines = _common.strip_plugin_log_lines
_parse_json_output = _common.parse_json_output
_is_loopback_url = _common.is_loopback_url
_redact_secret = _common.redact_secret
_parse_harnesses = _router.parse_harnesses
_parse_models = _router.parse_models
_parse_group_detail = _router.parse_group_detail
_normalize_quotas = _router.normalize_quotas


async def _forward_stream_chunk(stream_sink: Any, chunk: Mapping[str, Any]) -> None:
    if stream_sink is None:
        return
    event = str(chunk.get("event") or "delta")
    data = chunk.get("data")
    if event == "delta":
        if isinstance(data, str):
            await stream_sink.emit_delta(data)
            return
        if isinstance(data, Mapping) and isinstance(data.get("text"), str):
            extra = {k: v for k, v in data.items() if k != "text"}
            await stream_sink.emit_delta(data["text"], data=extra or None)
            return
    await stream_sink.emit_event(event, data if isinstance(data, Mapping) else {"value": data})


class MagpieAgentPlugin(BasePlugin):
    """Production Stage 2 Magpie AI adapter with fail-closed isolation and zero secret leakage."""

    def __init__(self) -> None:
        self._gateway = _gateway_mod.MagpieGatewayClient()
        self._session_seen: dict[str, set[str]] = {}
        self._sessions: Any = None
        self._context_snapshots = _context_mod.ContextSnapshots()
        self._unloaded = False

    async def on_load(self, ctx: PluginContext) -> None:
        """Fast non-blocking registration of all 5 capabilities."""
        await super().on_load(ctx)
        self._unloaded = False
        self._gateway.reset()
        self._session_seen.clear()
        self._sessions = _sessions_mod.AgentSessionStore(ctx.data_dir)
        ctx.register_capability("llm.router.v1", self.handle_router)
        ctx.register_streaming_capability("llm.chat.v1", self.handle_chat_stream)
        ctx.register_capability("llm.translate.v1", self.handle_translate)
        ctx.register_streaming_capability("ai.agent.v1", self.handle_agent_stream)
        ctx.register_capability("ai.curation.v1", self.handle_curation)
        ctx.register_capability("context.collect.v1", self.handle_context_collect)

    async def on_unload(self) -> None:
        """Abort active gateway connections and clear transient session state."""
        self._unloaded = True
        self._gateway.close_all()
        self._session_seen.clear()
        self._sessions = None
        self._context_snapshots.clear()

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight offline readiness diagnostics without network I/O."""
        magpie_bin = self._magpie_bin()
        resolved = self._resolve_bin(magpie_bin) if magpie_bin else None
        gw_url, gw_err = self._configured_gateway_url()
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-magpie-agent"
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "mode": "stage2_production",
            "registered_capabilities": [
                "llm.router.v1",
                "llm.chat.v1",
                "llm.translate.v1",
                "ai.agent.v1",
                "ai.curation.v1",
                "context.collect.v1",
            ],
            "streaming_capabilities": ["llm.chat.v1", "ai.agent.v1"],
            "magpie_bin_configured": bool(magpie_bin),
            "magpie_bin_found": bool(resolved),
            "default_group": f"group/{self._default_group()}",
            "gateway_url": gw_url if not gw_err else "",
            "gateway_url_valid": gw_err is None,
        }

    def _magpie_bin(self) -> str:
        return "magpie" if self.ctx is None else str(self.ctx.get_config("magpie_bin", "magpie") or "").strip()

    def _default_group(self) -> str:
        raw = DEFAULT_GROUP_ID if self.ctx is None else str(self.ctx.get_config("default_group", DEFAULT_GROUP_ID) or DEFAULT_GROUP_ID).strip()
        return raw.removeprefix("group/") or DEFAULT_GROUP_ID

    def _routing_strategy(self) -> str:
        if self.ctx is None:
            return "order"
        return str(self.ctx.get_config("routing_strategy", "order") or "order").strip() or "order"

    def _stays_policy(self) -> str:
        if self.ctx is None:
            return "turn"
        return str(self.ctx.get_config("stays_policy", "turn") or "turn").strip() or "turn"

    def _request_timeout_ms(self) -> int:
        if self.ctx is None:
            return 60000
        raw = self.ctx.get_config("request_timeout_ms", 60000)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            return 60000
        return raw

    def _gateway_key(self) -> str | None:
        return None if self.ctx is None else self.ctx.get_secret("gateway_key")

    def _configured_gateway_url(self) -> tuple[str, str | None]:
        raw = DEFAULT_GATEWAY_URL if self.ctx is None else str(self.ctx.get_config("gateway_url", DEFAULT_GATEWAY_URL) or "").strip()
        if not raw:
            return "", None
        if not _is_loopback_url(raw):
            return "", f"Magpie 网关仅允许 loopback 地址（收到: {raw!r}）"
        return raw.rstrip("/"), None

    async def _resolve_chat_gateway_url(self) -> tuple[str, str | None]:
        gw_url, gw_err = self._configured_gateway_url()
        if gw_err:
            return "", gw_err
        if gw_url and gw_url != DEFAULT_GATEWAY_URL:
            return gw_url, None
        if gw_url == "" or (gw_url == DEFAULT_GATEWAY_URL and self._magpie_bin() not in {"", "magpie"}):
            res, err_code, _ = await self._run_magpie(["status", "--json"], timeout_sec=2.0)
            if res is not None and not err_code:
                try:
                    st = _parse_json_output(res.stdout)
                except (json.JSONDecodeError, ValueError):
                    st = {}
                if isinstance(st, Mapping) and st.get("url"):
                    discovered = str(st["url"]).strip().rstrip("/")
                    if _is_loopback_url(discovered):
                        return discovered, None
        if not gw_url:
            return "", "Magpie 网关地址未配置且无法从本地探测"
        return gw_url, None

    @staticmethod
    def _resolve_bin(magpie_bin: str) -> str | None:
        if "/" in magpie_bin or "\\" in magpie_bin:
            return str(Path(magpie_bin).expanduser()) if Path(magpie_bin).expanduser().is_file() else None
        return shutil.which(magpie_bin)

    async def _run_magpie(self, args: list[str], *, timeout_sec: float = 3.0) -> tuple[SubprocessResult | None, str, str]:
        magpie_bin = self._magpie_bin()
        if not magpie_bin:
            return None, "not_configured", "尚未配置 Magpie CLI 路径（magpie_bin）"
        assert self.ctx is not None
        try:
            res = await self.ctx.exec_subprocess([magpie_bin, *args], timeout_sec=timeout_sec)
        except (FileNotFoundError, PermissionError, OSError):
            return None, "service_offline", "未检测到可用的 Magpie CLI，已进入安全降级模式"
        if res.timed_out or res.cancelled:
            return res, "timeout", "Magpie CLI 响应超时，已安全降级"
        if not res.ok:
            return res, "service_offline", "Magpie CLI 返回异常状态，已安全降级"
        return res, "", ""

    async def _call_chat_via_ctx(self, chat_req: dict[str, Any], stream_sink: Any = None) -> CapabilityResult:
        if self.ctx is not None and self.ctx.capabilities.has("llm.chat.v1"):
            if stream_sink is not None:
                return await self.ctx.capabilities.call_stream(
                    "llm.chat.v1",
                    chat_req,
                    on_chunk=lambda c: _forward_stream_chunk(stream_sink, c),
                )
            return await self.ctx.capabilities.call("llm.chat.v1", chat_req)
        return await self.handle_chat_stream(chat_req, stream_sink)

    async def handle_router(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `llm.router.v1` inspection, quota retrieval, and `set_route`."""
        secret = self._gateway_key()
        if self._unloaded:
            return _common.fail_result("plugin-magpie-agent 已卸载", error_code="unloaded", secret=secret)
        gw_url, gw_err = self._configured_gateway_url()
        return await _router_actions.handle_router_action(
            req,
            ctx=self.ctx,
            run_magpie=self._run_magpie,
            gateway=self._gateway,
            magpie_bin=self._magpie_bin(),
            default_group=self._default_group(),
            default_routing=self._routing_strategy(),
            default_stays=self._stays_policy(),
            configured_gw_url=gw_url,
            gw_url_error=gw_err,
            secret=secret,
        )

    async def handle_chat_stream(self, req: Mapping[str, Any], stream: Any = None) -> CapabilityResult:
        """Handle `llm.chat.v1` with optional SSE streaming and JSON structured output."""
        secret = self._gateway_key()
        if self._unloaded:
            return _common.fail_result("plugin-magpie-agent 已卸载", error_code="unloaded", secret=secret)
        gw_url, gw_err = await self._resolve_chat_gateway_url()
        return await _chat_mod.handle_chat(
            req,
            gateway=self._gateway,
            gateway_url=gw_url,
            gateway_url_error=gw_err,
            default_group=self._default_group(),
            default_timeout_ms=self._request_timeout_ms(),
            secret=secret,
            stream_sink=stream,
        )

    async def handle_translate(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `llm.translate.v1` single or batch translation backed by `llm.chat.v1`."""
        secret = self._gateway_key()
        if self._unloaded:
            return _common.fail_result("plugin-magpie-agent 已卸载", error_code="unloaded", secret=secret)
        return await _chat_mod.handle_translate(req, call_chat=self._call_chat_via_ctx, secret=secret)

    async def handle_agent_stream(self, req: Mapping[str, Any], stream: Any = None) -> CapabilityResult:
        """Handle `ai.agent.v1` domain-contextual actions backed by `llm.chat.v1`."""
        secret = self._gateway_key()
        if self._unloaded:
            return _common.fail_result("plugin-magpie-agent 已卸载", error_code="unloaded", secret=secret)
        if req.get("action") == "list_agents":
            return CapabilityResult.ok({"agents": _runtime_mod.agents(self.ctx)})
        agent_id = str(req.get("agent_id") or "builtin")
        if agent_id not in {"builtin", "codex"}:
            return CapabilityResult.fail("未知 Agent", error_code="invalid_argument")
        async def call_selected(payload: dict[str, Any], sink: Any) -> CapabilityResult:
            if agent_id == "codex":
                return await _runtime_mod.chat(payload, ctx=self.ctx, stream=sink)
            return await self._call_chat_via_ctx(payload, sink)
        return await _agent_mod.handle_agent(
            req,
            call_chat=call_selected,
            secret=secret,
            stream_sink=stream,
            collect_context=self.handle_context_collect,
            session_store=self._sessions,
        )

    async def handle_context_collect(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Collect complete or resumable web/book/subtitle/comment context for `ai.agent.v1` or MCP callers."""
        if self._unloaded or self.ctx is None:
            return _common.fail_result("plugin-magpie-agent 已卸载", error_code="unloaded")
        return await _context_mod.collect_context(req, call_capability=self.ctx.capabilities.call, snapshots=self._context_snapshots)

    async def handle_curation(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `ai.curation.v1` interactive and daily orchestration."""
        secret = self._gateway_key()
        if self._unloaded or self.ctx is None:
            return _common.fail_result("plugin-magpie-agent 已卸载", error_code="unloaded", secret=secret)
        return await _curation_mod.handle_curation(
            req,
            ctx=self.ctx,
            call_chat=lambda r: self._call_chat_via_ctx(r, None),
            session_seen=self._session_seen,
            secret=secret,
        )


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `MagpieAgentPlugin` instance."""
    return MagpieAgentPlugin()
