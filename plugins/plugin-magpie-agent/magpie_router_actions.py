"""Async RPC handlers for `llm.router.v1` (inspection, quotas, and `set_route` with read-back verification)."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext, SubprocessResult

try:
    from .magpie_common import (
        DEFAULT_GATEWAY_URL,
        DEFAULT_GROUP_ID,
        fail_result,
        parse_json_output,
        strip_plugin_log_lines,
    )
    from .magpie_router import (
        SUPPORTED_ROUTER_ACTIONS,
        VALID_ROUTING_STRATEGIES,
        VALID_STAYS_POLICIES,
        build_member_spec,
        detect_group_cycle,
        normalize_quotas,
        parse_fallbacks,
        parse_group_detail,
        parse_groups_list,
        parse_harnesses,
        parse_models,
        validate_dedicated_group_id,
    )
except ImportError:
    from magpie_common import (  # type: ignore[no-redef]
        DEFAULT_GATEWAY_URL,
        DEFAULT_GROUP_ID,
        fail_result,
        parse_json_output,
        strip_plugin_log_lines,
    )
    from magpie_router import (  # type: ignore[no-redef]
        SUPPORTED_ROUTER_ACTIONS,
        VALID_ROUTING_STRATEGIES,
        VALID_STAYS_POLICIES,
        build_member_spec,
        detect_group_cycle,
        normalize_quotas,
        parse_fallbacks,
        parse_group_detail,
        parse_groups_list,
        parse_harnesses,
        parse_models,
        validate_dedicated_group_id,
    )

RunMagpieFn = Callable[..., Coroutine[Any, Any, tuple[SubprocessResult | None, str, str]]]


async def handle_router_action(
    req: Mapping[str, Any],
    *,
    ctx: PluginContext | None,
    run_magpie: RunMagpieFn,
    gateway: Any,
    magpie_bin: str,
    default_group: str,
    default_routing: str,
    default_stays: str,
    configured_gw_url: str,
    gw_url_error: str | None,
    secret: str | None,
) -> CapabilityResult:
    """Dispatch `llm.router.v1` actions with fail-closed degradation and read-back verification."""
    payload = dict(req) if req is not None else {}
    action = str(payload.get("action") or "").strip()
    if action in {"current_route", "gateway_status"}:
        return await _handle_current_route(
            payload, run_magpie=run_magpie, default_group=default_group,
            default_routing=default_routing, default_stays=default_stays, secret=secret,
        )
    if action == "list_harnesses":
        res, err_code, err_msg = await run_magpie(["ls"], timeout_sec=3.0)
        if res is None or err_code:
            return fail_result(err_msg, error_code=err_code, secret=secret, data={"degraded": True, "online": False, "harnesses": [], "count": 0})
        harnesses = parse_harnesses(res.stdout)
        return CapabilityResult.ok({"degraded": False, "online": True, "harnesses": harnesses, "count": len(harnesses)})
    if action == "list_models":
        res, err_code, err_msg = await run_magpie(["models"], timeout_sec=3.0)
        if res is None or err_code:
            return fail_result(err_msg, error_code=err_code, secret=secret, data={"degraded": True, "online": False, "models": [], "groups": [], "count": 0})
        models, groups = parse_models(res.stdout)
        return CapabilityResult.ok({"degraded": False, "online": True, "models": models, "groups": groups, "count": len(models)})
    if action == "list_groups":
        res, err_code, err_msg = await run_magpie(["groups"], timeout_sec=3.0)
        if res is None or err_code:
            return fail_result(err_msg, error_code=err_code, secret=secret, data={"groups": [], "count": 0})
        groups = parse_groups_list(res.stdout)
        return CapabilityResult.ok({"groups": groups, "count": len(groups)})
    if action == "list_quotas":
        return await _handle_list_quotas(
            payload, run_magpie=run_magpie, gateway=gateway,
            configured_gw_url=configured_gw_url, gw_url_error=gw_url_error, secret=secret,
        )
    if action == "set_route":
        return await _handle_set_route(
            payload, ctx=ctx, run_magpie=run_magpie, magpie_bin=magpie_bin,
            default_group=default_group, default_routing=default_routing,
            default_stays=default_stays, secret=secret,
        )
    return CapabilityResult.fail(
        f"不支持的 llm.router.v1 动作: {action!r}",
        data={"supported_actions": list(SUPPORTED_ROUTER_ACTIONS)},
        error_code="invalid_argument",
    )


async def _handle_current_route(
    req: Mapping[str, Any],
    *,
    run_magpie: RunMagpieFn,
    default_group: str,
    default_routing: str,
    default_stays: str,
    secret: str | None,
) -> CapabilityResult:
    group_id = str(req.get("group_id") or default_group).strip().removeprefix("group/") or DEFAULT_GROUP_ID
    base: dict[str, Any] = {
        "summary": f"group/{group_id} -> offline (magpie unavailable)",
        "gateway_url": "",
        "gateway_running": False,
        "group_id": group_id,
        "harness_provider": "",
        "primary_member": "",
        "routing": default_routing,
        "stays": default_stays,
        "fallbacks": [],
        "degraded": True,
        "online": False,
        "mode": "stage2_production",
    }
    res, err_code, err_msg = await run_magpie(["status", "--json"], timeout_sec=3.0)
    if res is None or err_code:
        return fail_result(err_msg, error_code=err_code, secret=secret, data=base)
    try:
        status_obj = parse_json_output(res.stdout)
    except (json.JSONDecodeError, ValueError):
        return CapabilityResult.fail("Magpie 状态输出无法解析，已安全降级", data=base, error_code="service_offline")
    if not isinstance(status_obj, Mapping):
        return CapabilityResult.fail("Magpie 状态格式无效，已安全降级", data=base, error_code="service_offline")

    gateway_running = bool(status_obj.get("running", False))
    gateway_url = str(status_obj.get("url") or "")
    group_info: dict[str, Any] = {
        "routing": default_routing,
        "stays": default_stays,
        "primary_member": "",
        "harness_provider": "",
        "fallbacks": [],
    }
    if gateway_running:
        grp_res, grp_err, _ = await run_magpie(["group", group_id], timeout_sec=3.0)
        if grp_res is not None and not grp_err:
            parsed = parse_group_detail(grp_res.stdout, group_id, default_routing)
            group_info = {k: parsed[k] for k in ("routing", "stays", "primary_member", "harness_provider", "fallbacks")}

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


async def _handle_list_quotas(
    req: Mapping[str, Any],
    *,
    run_magpie: RunMagpieFn,
    gateway: Any,
    configured_gw_url: str,
    gw_url_error: str | None,
    secret: str | None,
) -> CapabilityResult:
    base = {"degraded": True, "online": False, "quotas": [], "count": 0, "source": "unavailable"}
    status_res, status_err, status_msg = await run_magpie(["status", "--json"], timeout_sec=3.0)
    if status_res is None or status_err:
        return fail_result(status_msg, error_code=status_err, secret=secret, data=base)
    try:
        status_obj = parse_json_output(status_res.stdout)
    except (json.JSONDecodeError, ValueError):
        status_obj = {}

    if isinstance(status_obj, Mapping) and status_obj.get("running"):
        target_gw = str(status_obj.get("url") or "") if configured_gw_url in {"", DEFAULT_GATEWAY_URL} else configured_gw_url
        if not gw_url_error and target_gw:
            loopback_data = await gateway.fetch_quotas(target_gw, secret=secret, timeout_sec=2.0)
            if loopback_data is not None:
                try:
                    quotas = normalize_quotas(loopback_data)
                except ValueError:
                    return CapabilityResult.fail("网关配额数据格式无效，已安全降级", data=base, error_code="service_offline")
                return CapabilityResult.ok({"degraded": False, "online": True, "quotas": quotas, "count": len(quotas), "source": "gateway"})

    if req.get("allow_cli_fallback", True) is False:
        return CapabilityResult.fail("Magpie 网关未运行且禁用了 CLI 回退，已安全降级", data=base, error_code="service_offline")

    provider = str(req.get("provider") or "").strip()
    cmd = ["quota", provider, "--json"] if provider else ["quota", "--json"]
    q_res, q_err, q_msg = await run_magpie(cmd, timeout_sec=5.0)
    if q_res is None or q_err:
        return fail_result(q_msg, error_code=q_err, secret=secret, data=base)
    try:
        quotas = normalize_quotas(parse_json_output(q_res.stdout))
    except (json.JSONDecodeError, ValueError):
        return CapabilityResult.fail("Magpie 配额输出无法解析，已安全降级", data=base, error_code="service_offline")
    return CapabilityResult.ok({"degraded": False, "online": True, "quotas": quotas, "count": len(quotas), "source": "cli"})


async def _handle_set_route(
    req: Mapping[str, Any],
    *,
    ctx: PluginContext | None,
    run_magpie: RunMagpieFn,
    magpie_bin: str,
    default_group: str,
    default_routing: str,
    default_stays: str,
    secret: str | None,
) -> CapabilityResult:
    group_id, grp_err = validate_dedicated_group_id(req.get("group_id") or req.get("route_group_id"), default_group)
    if grp_err:
        return fail_result(grp_err, error_code="invalid_argument", secret=secret)

    raw_model = str(req.get("model_id") or req.get("model") or req.get("primary_model") or req.get("active_model") or "").strip()
    primary_member, mem_err = build_member_spec(
        raw_model,
        effort=str(req.get("effort") or "").strip(),
        fast=bool(req.get("fast", False)),
        account=str(req.get("account") or "").strip(),
    )
    if mem_err:
        return fail_result(mem_err, error_code="invalid_argument", secret=secret)

    routing = str(req.get("routing") or default_routing).strip() or default_routing
    if routing not in VALID_ROUTING_STRATEGIES:
        return fail_result(f"不支持的路由策略 routing={routing!r}", error_code="invalid_argument", secret=secret)
    stays = str(req.get("stays") or default_stays).strip() or default_stays
    if stays not in VALID_STAYS_POLICIES:
        return fail_result(f"不支持的粘性策略 stays={stays!r}", error_code="invalid_argument", secret=secret)

    fallbacks, fb_err = parse_fallbacks(req.get("fallbacks"))
    if fb_err:
        return fail_result(fb_err, error_code="invalid_argument", secret=secret)
    members = [primary_member, *[fb for fb in fallbacks if fb != primary_member]]
    normalized_fallbacks = members[1:]

    existing_groups: list[dict[str, Any]] = []
    if any(m.startswith(("group/", "auto/")) for m in members):
        g_res, g_err, _ = await run_magpie(["groups"], timeout_sec=3.0)
        if g_res is not None and not g_err:
            existing_groups = parse_groups_list(g_res.stdout)
    if cycle := detect_group_cycle(group_id, members, existing_groups):
        return fail_result(f"路由组存在自指或循环嵌套: {cycle}", error_code="invalid_argument", secret=secret)

    if req.get("dry_run") is True:
        return CapabilityResult.ok({
            "dry_run": True, "group_id": group_id, "group_ref": f"group/{group_id}",
            "primary_member": primary_member, "fallbacks": normalized_fallbacks,
            "members": members, "routing": routing, "stays": stays,
        })

    if not magpie_bin:
        return fail_result("尚未配置 Magpie CLI 路径（magpie_bin）", error_code="not_configured", secret=secret)
    assert ctx is not None
    try:
        probe = await ctx.exec_subprocess([magpie_bin, "group", group_id], timeout_sec=3.0)
    except (FileNotFoundError, PermissionError, OSError):
        return fail_result("未检测到可用的 Magpie CLI", error_code="service_offline", secret=secret)
    if probe.timed_out or probe.cancelled:
        return fail_result("探测 Magpie 路由组超时", error_code="timeout", secret=secret)

    subcmd = "set" if probe.ok else "add"
    w_res, w_err, w_msg = await run_magpie(
        ["group", subcmd, group_id, f"models={','.join(members)}", f"routing={routing}", f"stays={stays}"],
        timeout_sec=5.0,
    )
    if w_res is None or w_err:
        detail = strip_plugin_log_lines((w_res.stderr or w_res.stdout) if w_res else "")
        return fail_result(detail or w_msg, error_code=w_err or "service_offline", secret=secret)

    if routing == "manual" and not primary_member.startswith(("group/", "auto/")):
        p_res, p_err, p_msg = await run_magpie(["group", "pick", group_id, primary_member], timeout_sec=3.0)
        if p_res is None or p_err:
            detail = strip_plugin_log_lines((p_res.stderr or p_res.stdout) if p_res else "")
            return fail_result(detail or p_msg, error_code=p_err or "service_offline", secret=secret)

    rb_res, rb_err, rb_msg = await run_magpie(["group", group_id], timeout_sec=3.0)
    if rb_res is None or rb_err:
        return fail_result(f"路由写入后读回失败: {rb_msg}", error_code=rb_err or "service_offline", secret=secret)
    parsed = parse_group_detail(rb_res.stdout, group_id, routing)
    mismatch = (
        parsed["primary_member"] != primary_member
        or any(m not in parsed["members"] for m in members)
        or (parsed["explicit_routing"] and parsed["routing"] != routing)
        or (parsed["explicit_stays"] and parsed["stays"] != stays)
    )
    if mismatch:
        return fail_result(
            f"路由写入后读回校验不一致: 期望 {members} ({routing}/{stays})，实际 {parsed['members']} ({parsed['routing']}/{parsed['stays']})",
            error_code="route_verification_failed",
            secret=secret,
            data={"readback": parsed},
        )

    summary = f"group/{group_id} -> {primary_member}"
    patch: dict[str, Any] = {
        "default_group": group_id,
        "stays_policy": stays,
        "active_route": {
            "group_id": group_id, "primary_member": primary_member, "fallbacks": normalized_fallbacks,
            "members": members, "routing": routing, "stays": stays, "summary": summary,
        },
    }
    if routing in {"manual", "order", "smart", "usage", "pace"}:
        patch["routing_strategy"] = routing
    ctx.set_config(patch)

    event_payload = {
        "summary": summary,
        "group_id": group_id,
        "primary_member": primary_member,
        "harness_provider": primary_member.partition("/")[0] if "/" in primary_member else "",
        "routing": routing,
        "stays": stays,
        "fallbacks": normalized_fallbacks,
    }
    await ctx.emit("entity.ai.route_changed", event_payload)
    return CapabilityResult.ok({
        **event_payload,
        "group_ref": f"group/{group_id}",
        "members": members,
        "verified": True,
        "readback": parsed,
        "degraded": False,
        "online": True,
    })
