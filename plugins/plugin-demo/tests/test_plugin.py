"""Offline contract and lifecycle tests for plugin-demo."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_sdk import BasePlugin, PluginContext, WorkspaceTabSpec, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_demo_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_matches_v2_schema() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-demo"
    assert manifest["runtime"] == "python"
    assert manifest["entry"] == "plugin:create_plugin"
    assert manifest["kind"] in schema["properties"]["kind"]["enum"]
    assert manifest["domain"] in schema["properties"]["domain"]["enum"]
    assert manifest["provides"] == ["demo.echo.v1"]

    allowed_top = set(schema["properties"].keys())
    assert set(manifest.keys()).issubset(allowed_top)
    for req_key in schema["required"]:
        assert req_key in manifest

    id_re = re.compile(schema["properties"]["id"]["pattern"])
    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert id_re.match(manifest["id"])
    assert all(cap_re.match(cap) for cap in manifest["provides"])

    field_types = schema["properties"]["config_schema"]["items"]["properties"]["type"]["enum"]
    for field in manifest["config_schema"]:
        assert field["type"] in field_types
        assert {"key", "label", "type"}.issubset(field.keys())


@pytest.mark.asyncio
async def test_demo_echo_capability_and_workspace_tab(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    assert isinstance(plugin, BasePlugin)

    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )

    await invoke_lifecycle_hook(plugin.on_load, ctx)
    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert ctx.capabilities.has("demo.echo.v1")

    res = await ctx.capabilities.call("demo.echo.v1", {"message": "hello omnidesk"})
    assert res.ok is True
    assert res.provider_plugin_id == "plugin-demo"
    assert res.data["echo"] == "echo: hello omnidesk"
    assert res.data["message"] == "hello omnidesk"
    assert res.data["count"] == 1

    ctx.set_config({"prefix": "reply", "uppercase": True})
    res2 = await ctx.capabilities.call("demo.echo.v1", {"text": "world"})
    assert res2.ok is True
    assert res2.data["echo"] == "reply: WORLD"
    assert res2.data["count"] == 2

    tabs = ctx.domains.workspace_tabs("core")
    assert len(tabs) == 1
    tab = tabs[0]
    assert isinstance(tab, WorkspaceTabSpec)
    assert tab.tab_id == "demo-echo"
    assert tab.plugin_id == "plugin-demo"
    assert tab.ui_schema["type"] == "Section"
    child_types = [c["type"] for c in tab.ui_schema["children"]]
    assert {"Markdown", "KeyValue", "Form", "ActionRow", "List"}.issubset(set(child_types))

    assert tab.render is not None
    rendered = tab.render()
    kv_block = next(c for c in rendered["children"] if c["type"] == "KeyValue")
    kv_map = {item["key"]: item["value"] for item in kv_block["items"]}
    assert kv_map["echo_count"] == "2"
    assert kv_map["last_echo"] == "reply: WORLD"

    assert tab.on_action is not None
    action_res = await tab.on_action("echo", {"message": "from tab"})
    assert action_res["ok"] is True
    assert action_res["result"]["echo"] == "reply: FROM TAB"

    reset_res = await tab.on_action("reset", {})
    assert reset_res["ok"] is True
    reset_kv = next(c for c in reset_res["ui_schema"]["children"] if c["type"] == "KeyValue")
    assert next(item["value"] for item in reset_kv["items"] if item["key"] == "echo_count") == "0"

    await invoke_lifecycle_hook(plugin.on_unload)
    summary = await ctx.acleanup()
    assert summary["capabilities"] == 1
    assert summary["workspace_tabs"] == 1
    assert not ctx.capabilities.has("demo.echo.v1")
    assert ctx.domains.workspace_tabs("core") == []
