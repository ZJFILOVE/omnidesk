"""Offline tests for `llm.router.v1` `set_route` write-back, read-back verification, and unload cleanup."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from omnidesk_core import OmnideskPaths, PluginManager  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_magpie_agent_route_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_magpie(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-magpie"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(stat.S_IRWXU)
    return script


@pytest.mark.asyncio
async def test_set_route_add_set_pick_readback_cycle_and_loopback_guards(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    state_file = tmp_path / "magpie_state.json"
    state_file.write_text(json.dumps({"groups": {}, "log": [], "tamper": False}), encoding="utf-8")

    fake_magpie = _write_fake_magpie(
        tmp_path,
        f"""
import json, pathlib, sys
sf = pathlib.Path({str(state_file)!r})
st = json.loads(sf.read_text(encoding="utf-8"))
args = sys.argv[1:]
st["log"].append(args)
if args[:2] == ["status", "--json"]:
    sf.write_text(json.dumps(st), encoding="utf-8")
    print(json.dumps({{"running": True, "url": "http://127.0.0.1:3425"}}))
elif args == ["groups"]:
    print("  Cycle Target  group/cycle-a  order  group/omnidesk-active")
elif len(args) == 2 and args[0] == "group":
    gid = args[1]
    g = st["groups"].get(gid)
    sf.write_text(json.dumps(st), encoding="utf-8")
    if g is None:
        print(f'magpie: no group "{{gid}}"', file=sys.stderr)
        sys.exit(1)
    if st.get("tamper"):
        print(f"  OmniDesk Active  group/{{gid}}")
        print("  routing   order")
        print("  stays     turn")
        print("  models    1 wrong/model-999")
    else:
        print(f"  OmniDesk Active  group/{{gid}}")
        print(f"  routing   {{g['routing']}}")
        print(f"  stays     {{g['stays']}}")
        for idx, m in enumerate(g["models"], start=1):
            mark = "● " if g.get("picked") == m else ""
            pfx = "  models    " if idx == 1 else "            "
            print(f"{{pfx}}{{idx}} {{mark}}{{m}}")
elif len(args) >= 4 and args[0] == "group" and args[1] in {{"add", "set"}}:
    gid = args[2]
    g = st["groups"].setdefault(gid, {{"models": [], "routing": "order", "stays": "turn", "picked": ""}})
    for kv in args[3:]:
        k, _, v = kv.partition("=")
        if k == "models":
            g["models"] = [x for x in v.split(",") if x]
        elif k in {{"routing", "stays"}}:
            g[k] = v
    sf.write_text(json.dumps(st), encoding="utf-8")
elif len(args) == 4 and args[:2] == ["group", "pick"]:
    gid, target = args[2], args[3]
    st["groups"][gid]["picked"] = target
    sf.write_text(json.dumps(st), encoding="utf-8")
else:
    sf.write_text(json.dumps(st), encoding="utf-8")
    sys.exit(2)
""",
    )

    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
    ctx.set_config("magpie_bin", str(fake_magpie))
    ctx.set_secret("gateway_key", "sk-route-secret-999")
    route_events: list[dict[str, Any]] = []
    ctx.subscribe("entity.ai.route_changed", lambda evt: route_events.append(dict(evt.payload)))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # 1. Create group/omnidesk-active (`group add`) + readback verification
    r1 = await ctx.capabilities.call(
        "llm.router.v1",
        {
            "action": "set_route",
            "model_id": "antigravity/gemini-4-carbon",
            "effort": "high",
            "routing": "order",
            "stays": "session",
            "fallbacks": ["codex/gpt-6-astra:medium"],
        },
    )
    assert r1.ok is True
    assert r1.data["verified"] is True
    assert r1.data["primary_member"] == "antigravity/gemini-4-carbon:high"
    assert r1.data["fallbacks"] == ["codex/gpt-6-astra:medium"]
    assert r1.data["summary"] == "group/omnidesk-active -> antigravity/gemini-4-carbon:high"
    assert len(route_events) == 1
    assert route_events[0]["primary_member"] == "antigravity/gemini-4-carbon:high"

    # 2. Update existing group with routing=manual (`group set` + `group pick`) + readback
    r2 = await ctx.capabilities.call(
        "llm.router.v1",
        {"action": "set_route", "model_id": "codex/gpt-6-astra", "routing": "manual", "stays": "turn"},
    )
    assert r2.ok is True
    assert r2.data["routing"] == "manual"
    assert r2.data["primary_member"] == "codex/gpt-6-astra"
    log_cmds = json.loads(state_file.read_text(encoding="utf-8"))["log"]
    assert any(cmd[:3] == ["group", "add", "omnidesk-active"] for cmd in log_cmds)
    assert any(cmd[:3] == ["group", "set", "omnidesk-active"] for cmd in log_cmds)
    assert any(cmd[:4] == ["group", "pick", "omnidesk-active", "codex/gpt-6-astra"] for cmd in log_cmds)

    # 3. Forbid harness/non-dedicated groups & cycle references
    for bad in (
        {"action": "set_route", "group_id": "codex", "model_id": "a/b"},
        {"action": "set_route", "group_id": "smart-split", "model_id": "a/b"},
        {"action": "set_route", "model_id": "group/omnidesk-active"},
        {"action": "set_route", "model_id": "group/cycle-a"},
    ):
        bad_res = await ctx.capabilities.call("llm.router.v1", bad)
        assert bad_res.ok is False
        assert bad_res.data["error_code"] == "invalid_argument"

    # 4. Readback verification mismatch fails closed
    st_obj = json.loads(state_file.read_text(encoding="utf-8"))
    st_obj["tamper"] = True
    state_file.write_text(json.dumps(st_obj), encoding="utf-8")
    tamper_res = await ctx.capabilities.call("llm.router.v1", {"action": "set_route", "model_id": "codex/gpt-6-astra"})
    assert tamper_res.ok is False
    assert tamper_res.data["error_code"] == "route_verification_failed"

    # 5. Non-loopback gateway_url rejected fail-closed
    ctx.set_config("gateway_url", "https://remote.example.com:3425")
    bad_gw = await ctx.capabilities.call("llm.chat.v1", {"prompt": "hello"})
    assert bad_gw.ok is False
    assert bad_gw.data["error_code"] == "invalid_argument"

    # 6. dry_run returns planned route without running CLI
    dry_res = await ctx.capabilities.call(
        "llm.router.v1",
        {"action": "set_route", "model_id": "deepseek/deepseek-flash", "dry_run": True},
    )
    assert dry_res.ok is True and dry_res.data["dry_run"] is True
    assert dry_res.data["primary_member"] == "deepseek/deepseek-flash"

    await invoke_lifecycle_hook(plugin.on_unload)
    assert plugin._unloaded is True
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_plugin_manager_lifecycle_and_unload_cleanup(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env({
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    })
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    mgr.secret_vault.set("plugin-magpie-agent", "gateway_key", "vault-mgr-secret-xyz")
    rec = await mgr.load_plugin(PLUGIN_DIR)
    assert rec.plugin_id == "plugin-magpie-agent"
    for cap in ("llm.router.v1", "llm.chat.v1", "llm.translate.v1", "ai.agent.v1", "ai.curation.v1"):
        assert mgr.capabilities.has(cap)
    hc = await mgr.health_check("plugin-magpie-agent")
    assert hc["ok"] is True

    await mgr.unload_plugin("plugin-magpie-agent")
    for cap in ("llm.router.v1", "llm.chat.v1", "llm.translate.v1", "ai.agent.v1", "ai.curation.v1"):
        assert not mgr.capabilities.has(cap)
