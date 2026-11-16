"""Safe read-only degradation skeleton for `plugin-magpie-agent` (`llm.router.v1`)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, SubprocessResult

_LOG_LINE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\s+plugin:\s*")
_EFFORTS_RE = re.compile(r"^(?:none|minimal|low|medium|high|xhigh|max|ultra)(?:/(?:none|minimal|low|medium|high|xhigh|max|ultra))*$")
_MODEL_HEAD_RE = re.compile(r"^[A-Za-z0-9._-]+/\S+$")
_MEMBER_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:@/-]+$")
_SUPPORTED_ACTIONS = ("current_route", "list_harnesses", "list_models", "list_quotas")
_DEFAULT_GROUP_ID = "omnidesk-active"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _strip_plugin_log_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not _LOG_LINE_RE.match(line)).strip()


def _parse_json_output(text: str) -> Any:
    cleaned = _strip_plugin_log_lines(text)
    if not cleaned:
        raise ValueError("empty JSON output")
    return json.loads(cleaned)


def _is_loopback_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _redact_secret(text: str, secret: str | None) -> str:
    if not text:
        return ""
    if secret and len(secret) >= 4:
        text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}", r"\1[REDACTED]", text)


def _parse_harnesses(stdout: str) -> list[dict[str, Any]]:
    harnesses: list[dict[str, Any]] = []
    for raw_line in _strip_plugin_log_lines(stdout).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
        name, rest = parts[0], parts[1:]
        config_path = ""
        if rest and (rest[-1].startswith(("~/", "/", "./")) or rest[-1].endswith((".json", ".toml", ".yml", ".yaml"))):
            config_path = rest.pop()
        attrs: dict[str, str] = {}
        status = ""
        for segment in rest:
            for token in (t.strip() for t in segment.split("·") if t.strip()):
                matched = False
                for key, prefix in (("effort", "effort "), ("thinking", "thinking "), ("sign_in", "sign-in "), ("provider", "provider ")):
                    if token.startswith(prefix):
                        attrs[key] = token.removeprefix(prefix).strip()
                        matched = True
                        break
                if not matched and not status:
                    status = token
        harnesses.append({
            "name": name, "harness_id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
            "status": status, "model": status if "/" in status else "",
            "effort": attrs.get("effort", ""), "thinking": attrs.get("thinking", ""),
            "sign_in": attrs.get("sign_in", ""), "provider": attrs.get("provider", ""),
            "config_path": config_path,
            "configured": (status not in {"", "—", "-"}) or (attrs.get("provider", "") not in {"", "—", "-"}),
        })
    return harnesses


def _parse_models(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    models: list[dict[str, Any]] = []
    groups: list[str] = []
    current_group = ""
    for raw_line in _strip_plugin_log_lines(stdout).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
        head, tail = parts[0], parts[1:]
        if not _MODEL_HEAD_RE.match(head) or head.startswith(("http:", "https:")):
            current_group = line
            if current_group not in groups:
                groups.append(current_group)
            continue
        provider, _, slug = head.partition("/")
        efforts = tail.pop().split("/") if tail and _EFFORTS_RE.match(tail[-1]) else []
        models.append({
            "id": head, "provider": provider, "slug": slug,
            "name": " ".join(tail).strip() or slug, "group": current_group,
            "efforts": efforts, "is_group": provider in {"group", "auto"},
        })
    return models, groups


def _parse_group_detail(stdout: str, group_id: str, default_routing: str) -> dict[str, Any]:
    routing, stays, members, fallback_members = default_routing, "turn", [], []
    own_group = f"group/{group_id}"
    in_models = False
    for raw_line in _strip_plugin_log_lines(stdout).splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        for match in re.finditer(r"\b(routing|stays)\s*[=:]\s*([A-Za-z0-9_-]+)", stripped):
            if match.group(1) == "routing":
                routing = match.group(2)
            else:
                stays = match.group(2)
        cols = [p.strip() for p in re.split(r"\s{2,}", stripped) if p.strip()]
        is_key_row = raw_line.startswith("  ") and not raw_line.startswith("    ") and bool(cols)
        if is_key_row:
            key = cols[0]
            if key == "routing" and len(cols) >= 2:
                in_models, routing = False, cols[1].split()[0]
                continue
            if key == "stays" and len(cols) >= 2:
                in_models, stays = False, cols[1].split()[0]
                continue
            if key == "models":
                in_models = True
                stripped = "  ".join(cols[1:])
            elif key in {"rules", "levels", "classifier", "context", "compact", "family", "note"}:
                in_models = False
                continue
        if in_models:
            m_num = re.match(r"^(?:\d+\s+)?([A-Za-z0-9._-]+/[A-Za-z0-9._:@/+_-]+)", stripped)
            if m_num and m_num.group(1) != own_group and m_num.group(1) not in members:
                members.append(m_num.group(1))
        elif len(cols) == 1 and _MEMBER_RE.match(cols[0]) and cols[0] != own_group:
            if not cols[0].startswith(("http:", "https:")) and cols[0] not in fallback_members:
                fallback_members.append(cols[0])
    resolved = members or fallback_members
    primary = resolved[0] if resolved else ""
    return {
        "routing": routing,
        "stays": stays,
        "primary_member": primary,
        "harness_provider": primary.partition("/")[0] if "/" in primary else "",
        "fallbacks": resolved[1:],
    }


def _normalize_quotas(raw: Any) -> list[dict[str, Any]]:
    entries = raw.get("quotas", []) if isinstance(raw, Mapping) else raw
    if not isinstance(entries, list):
        raise ValueError("quotas payload must be a list")
    normalized: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        windows = [
            {
                "name": str(w.get("name", "")), "used_pct": w.get("used"), "remaining_pct": w.get("remaining"),
                "resets_at": str(w.get("resetsAt") or w.get("resets_at") or ""),
                "unlimited": bool(w.get("unlimited", False)), "display": str(w.get("display", "")),
                "amount": w.get("amount"), "limit": w.get("limit"), "unit": str(w.get("unit", "")),
            }
            for w in (item.get("windows") or []) if isinstance(w, Mapping)
        ]
        normalized.append({
            "provider": str(item.get("provider", "")), "name": str(item.get("name", "")),
            "user": str(item.get("user", "")), "plan": str(item.get("plan", "")),
            "kind": str(item.get("kind", "")), "balance": item.get("balance"),
            "error": str(item.get("error", "")), "as_of": str(item.get("asOf") or item.get("readAt") or ""),
            "last_served_at": str(item.get("lastServedAt") or ""), "last": bool(item.get("last", False)),
            "windows": windows,
        })
    return normalized


class MagpieAgentPlugin(BasePlugin):
    """Read-only safe-degradation skeleton for `llm.router.v1`."""

    async def on_load(self, ctx: PluginContext) -> None:
        """Register `llm.router.v1` without performing blocking I/O."""
        await super().on_load(ctx)
        ctx.register_capability("llm.router.v1", self.handle_router)

    async def on_health_check(self) -> dict[str, Any]:
        """Report local configuration readiness without calling external network services."""
        magpie_bin = self._magpie_bin()
        resolved = self._resolve_bin(magpie_bin) if magpie_bin else None
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-magpie-agent"
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "mode": "safe_degraded_skeleton",
            "registered_capabilities": ["llm.router.v1"],
            "magpie_bin_configured": bool(magpie_bin),
            "magpie_bin_found": bool(resolved),
        }

    def _magpie_bin(self) -> str:
        return "magpie" if self.ctx is None else str(self.ctx.get_config("magpie_bin", "magpie") or "").strip()

    def _routing_strategy(self) -> str:
        if self.ctx is None:
            return "order"
        return str(self.ctx.get_config("routing_strategy", "order") or "order").strip() or "order"

    def _gateway_key(self) -> str | None:
        return None if self.ctx is None else self.ctx.get_secret("gateway_key")

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

    async def _fetch_loopback_quotas(self, gateway_url: str, *, timeout_sec: float = 2.0) -> Any | None:
        if not _is_loopback_url(gateway_url):
            return None
        target = gateway_url.rstrip("/") + "/v1/magpie/quotas"
        secret = self._gateway_key()

        def _do_get() -> Any:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(target, method="GET")
            if secret:
                req.add_header("Authorization", f"Bearer {secret}")
            with opener.open(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8")) if resp.status == 200 else None

        try:
            return await asyncio.to_thread(_do_get)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return None

    async def handle_router(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Dispatch `llm.router.v1` read-only inspection actions with fail-closed degradation."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        if action == "current_route":
            return await self._handle_current_route(payload)
        if action == "list_harnesses":
            return await self._handle_list_harnesses()
        if action == "list_models":
            return await self._handle_list_models()
        if action == "list_quotas":
            return await self._handle_list_quotas(payload)
        if action == "set_route":
            return CapabilityResult.fail(
                "当前仅实现只读安全降级骨架，暂不支持 set_route 写回",
                data={"degraded": True, "online": False, "action": "set_route"},
                error_code="not_implemented",
            )
        return CapabilityResult.fail(
            f"不支持的 llm.router.v1 动作: {action!r}",
            data={"supported_actions": list(_SUPPORTED_ACTIONS)},
            error_code="invalid_argument",
        )

    async def _handle_current_route(self, req: Mapping[str, Any]) -> CapabilityResult:
        group_id = str(req.get("group_id") or _DEFAULT_GROUP_ID).strip() or _DEFAULT_GROUP_ID
        routing = self._routing_strategy()
        base: dict[str, Any] = {
            "summary": f"group/{group_id} -> offline (magpie unavailable)",
            "gateway_url": "",
            "gateway_running": False,
            "group_id": group_id,
            "harness_provider": "",
            "primary_member": "",
            "routing": routing,
            "stays": "turn",
            "fallbacks": [],
            "degraded": True,
            "online": False,
            "mode": "safe_degraded_skeleton",
        }
        res, err_code, err_msg = await self._run_magpie(["status", "--json"], timeout_sec=3.0)
        if res is None or err_code:
            return CapabilityResult.fail(err_msg, data=base, error_code=err_code)
        try:
            status_obj = _parse_json_output(res.stdout)
        except (json.JSONDecodeError, ValueError):
            return CapabilityResult.fail("Magpie 状态输出无法解析，已安全降级", data=base, error_code="service_offline")
        if not isinstance(status_obj, Mapping):
            return CapabilityResult.fail("Magpie 状态格式无效，已安全降级", data=base, error_code="service_offline")

        gateway_running = bool(status_obj.get("running", False))
        gateway_url = str(status_obj.get("url") or "")
        group_info: dict[str, Any] = {"routing": routing, "stays": "turn", "primary_member": "", "harness_provider": "", "fallbacks": []}
        if gateway_running:
            grp_res, grp_err, _ = await self._run_magpie(["group", group_id], timeout_sec=3.0)
            if grp_res is not None and not grp_err:
                group_info = _parse_group_detail(grp_res.stdout, group_id, routing)

        primary = group_info["primary_member"]
        summary = (
            f"group/{group_id} -> {primary}"
            if gateway_running and primary
            else (f"group/{group_id} -> unconfigured" if gateway_running else f"group/{group_id} -> offline (gateway stopped)")
        )
        return CapabilityResult.ok({
            **base,
            **group_info,
            "summary": summary,
            "gateway_url": gateway_url,
            "gateway_running": gateway_running,
            "degraded": not (gateway_running and bool(primary)),
            "online": gateway_running,
        })

    async def _handle_list_harnesses(self) -> CapabilityResult:
        base = {"degraded": True, "online": False, "harnesses": [], "count": 0}
        res, err_code, err_msg = await self._run_magpie(["ls"], timeout_sec=3.0)
        if res is None or err_code:
            return CapabilityResult.fail(err_msg, data=base, error_code=err_code)
        harnesses = _parse_harnesses(res.stdout)
        return CapabilityResult.ok({"degraded": False, "online": True, "harnesses": harnesses, "count": len(harnesses)})

    async def _handle_list_models(self) -> CapabilityResult:
        base = {"degraded": True, "online": False, "models": [], "groups": [], "count": 0}
        res, err_code, err_msg = await self._run_magpie(["models"], timeout_sec=3.0)
        if res is None or err_code:
            return CapabilityResult.fail(err_msg, data=base, error_code=err_code)
        models, groups = _parse_models(res.stdout)
        return CapabilityResult.ok({"degraded": False, "online": True, "models": models, "groups": groups, "count": len(models)})

    async def _handle_list_quotas(self, req: Mapping[str, Any]) -> CapabilityResult:
        base = {"degraded": True, "online": False, "quotas": [], "count": 0, "source": "unavailable"}
        status_res, status_err, status_msg = await self._run_magpie(["status", "--json"], timeout_sec=3.0)
        if status_res is None or status_err:
            return CapabilityResult.fail(status_msg, data=base, error_code=status_err)
        try:
            status_obj = _parse_json_output(status_res.stdout)
        except (json.JSONDecodeError, ValueError):
            status_obj = {}

        if isinstance(status_obj, Mapping) and status_obj.get("running"):
            loopback_data = await self._fetch_loopback_quotas(str(status_obj.get("url") or ""), timeout_sec=2.0)
            if loopback_data is not None:
                try:
                    quotas = _normalize_quotas(loopback_data)
                except ValueError:
                    return CapabilityResult.fail("网关配额数据格式无效，已安全降级", data=base, error_code="service_offline")
                return CapabilityResult.ok({"degraded": False, "online": True, "quotas": quotas, "count": len(quotas), "source": "gateway"})

        if req.get("allow_cli_fallback", True) is False:
            return CapabilityResult.fail("Magpie 网关未运行且禁用了 CLI 回退，已安全降级", data=base, error_code="service_offline")

        provider = str(req.get("provider") or "").strip()
        cmd = ["quota", provider, "--json"] if provider else ["quota", "--json"]
        q_res, q_err, q_msg = await self._run_magpie(cmd, timeout_sec=5.0)
        if q_res is None or q_err:
            return CapabilityResult.fail(_redact_secret(q_msg, self._gateway_key()), data=base, error_code=q_err)
        try:
            quotas = _normalize_quotas(_parse_json_output(q_res.stdout))
        except (json.JSONDecodeError, ValueError):
            return CapabilityResult.fail("Magpie 配额输出无法解析，已安全降级", data=base, error_code="service_offline")
        return CapabilityResult.ok({"degraded": False, "online": True, "quotas": quotas, "count": len(quotas), "source": "cli"})


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `MagpieAgentPlugin` instance."""
    return MagpieAgentPlugin()
