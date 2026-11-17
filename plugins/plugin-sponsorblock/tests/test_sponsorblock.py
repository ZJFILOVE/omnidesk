"""Manifest, lifecycle, validation, and disabled-mode tests for plugin-sponsorblock."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_sdk import BasePlugin, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_sponsorblock_module(submodule: str = "plugin") -> Any:
    pkg_name = "_omnidesk_test_sponsorblock"
    if pkg_name not in sys.modules:
        pkg = ModuleType(pkg_name)
        pkg.__path__ = [str(PLUGIN_DIR)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
    full_name = f"{pkg_name}.{submodule}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PLUGIN_DIR / f"{submodule}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_matches_v2_schema_and_requirements() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-sponsorblock"
    assert manifest["kind"] == "extension"
    assert manifest["domain"] == "media"
    assert manifest["entry"] == "plugin:create_plugin"
    assert manifest["provides"] == ["player.timeline.v1"]

    allowed_top = set(schema["properties"].keys())
    assert set(manifest.keys()).issubset(allowed_top)
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(cap) for cap in manifest["provides"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    required_keys = {"youtube_api_base", "bilibili_api_base", "categories", "cache_ttl_sec", "enabled"}
    assert required_keys.issubset(fields.keys())
    assert fields["youtube_api_base"]["default"] == "https://sponsor.ajay.app"
    assert fields["bilibili_api_base"]["default"] == "https://bsbsb.top"
    assert fields["cache_ttl_sec"]["type"] == "int"
    assert fields["enabled"]["type"] == "bool"


@pytest.mark.asyncio
async def test_lifecycle_validation_unknown_platform_and_disabled(tmp_path: Path) -> None:
    module = _load_sponsorblock_module("plugin")
    plugin = module.create_plugin()
    assert isinstance(plugin, BasePlugin)

    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert ctx.capabilities.has("player.timeline.v1")

    st = await ctx.capabilities.call("player.timeline.v1", {"action": "status"})
    assert st.ok is True
    assert st.data["enabled"] is True

    bad_plat = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "fetch", "platform": "vimeo", "video_id": "12345"}
    )
    assert bad_plat.ok is False
    assert bad_plat.data["error_code"] == "invalid_argument"

    bad_vid = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": "../evil"}
    )
    assert bad_vid.ok is False
    assert bad_vid.data["error_code"] == "invalid_argument"

    ctx.set_config("youtube_api_base", "http://sponsor.ajay.app")
    bad_http = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": "dQw4w9WgXcQ"}
    )
    assert bad_http.ok is False
    assert bad_http.data["error_code"] == "invalid_argument"

    bad_act = await ctx.capabilities.call("player.timeline.v1", {"action": "nope"})
    assert bad_act.ok is False
    assert bad_act.data["error_code"] == "invalid_argument"

    ctx.set_config({"enabled": False, "youtube_api_base": "https://127.0.0.1:1"})
    dis = await ctx.capabilities.call(
        "player.timeline.v1", {"action": "fetch", "platform": "youtube", "video_id": "dQw4w9WgXcQ"}
    )
    assert dis.ok is True
    assert dis.data["enabled"] is False
    assert dis.data["segments"] == []
    assert dis.data["chapters"] == []

    await invoke_lifecycle_hook(plugin.on_unload)
    summary = await ctx.acleanup()
    assert summary["capabilities"] == 1
    assert not ctx.capabilities.has("player.timeline.v1")
