"""Manifest, lifecycle, secret isolation, and web.cookies.v1 tests for plugin-sub-hunter."""

from __future__ import annotations

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

from conftest import load_sub_hunter_module  # noqa: E402
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402


def test_manifest_matches_v2_schema_and_requirements() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-sub-hunter"
    assert manifest["kind"] == "provider"
    assert manifest["domain"] == "media"
    assert manifest["entry"] == "plugin:create_plugin"
    assert set(manifest["provides"]) == {"subtitle.fetch.v1", "subtitle.sync.v1"}

    allowed_top = set(schema["properties"].keys())
    assert set(manifest.keys()).issubset(allowed_top)
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(cap) for cap in manifest["provides"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    required_keys = {
        "provider",
        "assrt_token",
        "download_root",
        "max_download_mb",
        "ffsubsync_bin",
        "overwrite_policy",
    }
    assert required_keys.issubset(fields.keys())
    assert fields["assrt_token"]["type"] == "secret"
    assert fields["max_download_mb"]["type"] == "int"
    assert fields["overwrite_policy"]["default"] == "protect_manual"
    assert "protect_manual" in fields["overwrite_policy"]["options"]


@pytest.mark.asyncio
async def test_lifecycle_secret_isolation_and_status(tmp_path: Path) -> None:
    module = load_sub_hunter_module("plugin")
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
    secret_tok = "assrt-secret-token-xyz-999"
    secret_cookie = "session_id=topsecret-cookie-abc"
    ctx.set_secret("assrt_token", secret_tok)
    ctx.set_secret("cookie_header", secret_cookie)
    ctx.set_config({"provider": "assrt", "assrt_token": "should-be-stripped", "cookie_header": "strip-too"})
    plain_cfg = ctx.get_config()
    assert "assrt_token" not in plain_cfg
    assert "cookie_header" not in plain_cfg

    await invoke_lifecycle_hook(plugin.on_load, ctx)
    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert health["assrt_token_configured"] is True
    assert secret_tok not in json.dumps(health)

    assert ctx.capabilities.has("subtitle.fetch.v1")
    assert ctx.capabilities.has("subtitle.sync.v1")

    st = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "status"})
    assert st.ok is True
    assert st.data["assrt_token_configured"] is True
    assert secret_tok not in json.dumps(st.data)
    assert secret_cookie not in json.dumps(st.data)

    derived = await ctx.capabilities.call(
        "subtitle.fetch.v1",
        {"action": "derive_query", "filename": "Severance.S02E03.2160p.WEB-DL.mkv", "language": "zh"},
    )
    assert derived.ok is True
    assert derived.data["query"] == "Severance S02E03"
    assert derived.data["season"] == 2
    assert derived.data["episode"] == 3

    bad = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "unknown_op"})
    assert bad.ok is False
    assert bad.data["error_code"] == "invalid_argument"

    await invoke_lifecycle_hook(plugin.on_unload)
    summary = await ctx.acleanup()
    assert summary["capabilities"] == 2
    assert not ctx.capabilities.has("subtitle.fetch.v1")
    assert not ctx.capabilities.has("subtitle.sync.v1")


@pytest.mark.asyncio
async def test_web_cookies_capability_used_over_vault_fallback(tmp_path: Path, make_https_server) -> None:
    module = load_sub_hunter_module("plugin")

    def _responder(handler: Any) -> None:
        body = b"<html><body><a href='/a/sub123'>Test Movie \xe7\xae\x80\xe4\xbd\x93</a></body></html>"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    srv = make_https_server(_responder)
    plugin = module.SubHunterPlugin(ssl_context=srv.ssl_context)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"provider": "subhd", "subhd_base_url": srv.base_url})
    ctx.set_secret("cookie_header", "vault=fallback")

    async def _fake_web_cookies(req: dict[str, Any]) -> CapabilityResult:
        assert req["action"] == "get_cookies"
        return CapabilityResult.ok({"cookies": [{"name": "sid", "value": "live-chrome-cookie"}]})

    ctx.capabilities.register("web.cookies.v1", _fake_web_cookies, plugin_id="plugin-chrome-web")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    res = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "search", "title": "Test Movie"})
    assert res.ok is True
    assert res.data["count"] == 1
    assert res.data["results"][0]["id"] == "sub123"
    assert srv.requests[0]["headers"]["cookie"] == "sid=live-chrome-cookie"

    await ctx.acleanup()
