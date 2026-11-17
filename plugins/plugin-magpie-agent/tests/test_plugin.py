"""Offline contract, routing write-back, safety, and lifecycle tests for plugin-magpie-agent."""

from __future__ import annotations

import http.server
import importlib.util
import json
import re
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
from omnidesk_sdk import BasePlugin, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_magpie_agent_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_magpie(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-magpie"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(stat.S_IRWXU)
    return script


def test_manifest_matches_spec_and_schema() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-magpie-agent"
    assert manifest["kind"] == "provider"
    assert manifest["domain"] == "core"
    assert manifest["entry"] == "plugin:create_plugin"
    assert set(manifest["provides"]) == {
        "llm.router.v1",
        "ai.agent.v1",
        "ai.curation.v1",
        "llm.chat.v1",
        "llm.translate.v1",
        "context.collect.v1",
    }
    assert set(manifest["consumes"]) == {
        "recommend.feed.v1",
        "media.charts.v1",
        "media.playlist.v1",
        "web.extract.v1",
        "web.fetch.v1",
        "book.source.v1",
        "subtitle.fetch.v1",
        "subtitle.sync.v1",
        "speech.asr.v1",
        "text.source.v1",
        "media.source.v1",
        "book.bilingual_pdf.v1",
        "text.comments.v1",
        "media.youtube.v1",
        "media.bilibili.v1",
    }
    assert set(manifest["permissions"]) == {"process:codex", "process:magpie", "network:loopback", "config:read", "config:write", "secret:vault"}

    allowed_top = set(schema["properties"].keys())
    assert set(manifest.keys()).issubset(allowed_top)
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(cap) for cap in manifest["provides"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    assert set(fields.keys()) == {
        "magpie_bin",
        "gateway_url",
        "default_group",
        "routing_strategy",
        "stays_policy",
        "request_timeout_ms",
        "gateway_key",
        "codex_bin",
        "codex_model",
    }
    assert fields["magpie_bin"]["type"] == "string"
    assert fields["magpie_bin"]["default"] == "magpie"
    assert fields["gateway_url"]["default"] == "http://127.0.0.1:3425"
    assert fields["default_group"]["default"] == "omnidesk-active"
    assert fields["routing_strategy"]["type"] == "select"
    assert fields["routing_strategy"]["default"] == "order"
    assert fields["routing_strategy"]["options"] == ["manual", "order", "smart", "usage", "pace"]
    assert fields["gateway_key"]["type"] == "secret"
    assert fields["gateway_key"]["default"] == ""


@pytest.mark.asyncio
async def test_safe_degradation_when_magpie_missing_or_unconfigured(tmp_path: Path) -> None:
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
    ctx.set_config("magpie_bin", str(tmp_path / "missing-magpie"))
    ctx.set_secret("gateway_key", "sk-vault-only-secret-12345")
    assert "gateway_key" not in ctx.get_config()

    await invoke_lifecycle_hook(plugin.on_load, ctx)
    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert health["magpie_bin_found"] is False
    for cap in manifest["provides"]:
        assert ctx.capabilities.has(cap)

    for action, list_key in (
        ("current_route", None),
        ("list_harnesses", "harnesses"),
        ("list_models", "models"),
        ("list_quotas", "quotas"),
    ):
        res = await ctx.capabilities.call("llm.router.v1", {"action": action})
        assert res.ok is False
        assert res.data["error_code"] == "service_offline"
        assert res.data["degraded"] is True
        assert res.data["online"] is False
        assert "sk-vault-only-secret-12345" not in res.error
        assert "sk-vault-only-secret-12345" not in json.dumps(res.data)
        if list_key is None:
            assert res.data["gateway_running"] is False
            assert res.data["primary_member"] == ""
            assert res.data["gateway_url"] == ""
        else:
            assert res.data[list_key] == []
            assert res.data["count"] == 0

    set_res = await ctx.capabilities.call("llm.router.v1", {"action": "set_route", "model_id": "a/b"})
    assert set_res.ok is False
    assert set_res.data["error_code"] == "service_offline"

    bad_res = await ctx.capabilities.call("llm.router.v1", {"action": "unknown"})
    assert bad_res.ok is False
    assert bad_res.data["error_code"] == "invalid_argument"

    ctx.set_config("magpie_bin", "")
    unconf_res = await ctx.capabilities.call("llm.router.v1", {"action": "current_route"})
    assert unconf_res.ok is False
    assert unconf_res.data["error_code"] == "not_configured"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
    assert not ctx.capabilities.has("llm.router.v1")


@pytest.mark.asyncio
async def test_offline_cli_parsing_with_log_prefix_and_stopped_gateway(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    fake_magpie = _write_fake_magpie(
        tmp_path,
        """
import json, sys
args = sys.argv[1:]
print("2026/11/16 12:00:00 plugin: host starts with HTTP_PROXY=http://127.0.0.1:7897")
if args[:2] == ["status", "--json"]:
    print(json.dumps({"running": False, "url": "http://127.0.0.1:3425"}))
elif args == ["ls"]:
    print("  Codex             provider-a/model-1  ·  effort high  ·  sign-in ok  ~/.codex/config.toml")
    print("  Antigravity CLI   —  ·  effort —                                       ~/.gemini/settings.json")
elif args == ["models"]:
    print("  Routing groups")
    print("  group/omnidesk-active  OmniDesk Route  low/medium/high")
    print("  ProviderA")
    print("  provider-a/model-1     Model One       low/high/max")
elif args == ["quota", "--json"]:
    print(json.dumps([
        {
            "provider": "provider-a",
            "name": "Provider A",
            "kind": "subscription",
            "last": True,
            "windows": [{"name": "5 hours", "used": 20, "remaining": 80, "resetsAt": "2026-11-16T15:00:00Z"}],
        }
    ]))
else:
    sys.exit(2)
""",
    )

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )
    ctx.set_config("magpie_bin", str(fake_magpie))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    route_res = await ctx.capabilities.call("llm.router.v1", {"action": "current_route"})
    assert route_res.ok is True
    assert route_res.data["gateway_running"] is False
    assert route_res.data["degraded"] is True
    assert route_res.data["primary_member"] == ""
    assert route_res.data["gateway_url"] == "http://127.0.0.1:3425"

    ls_res = await ctx.capabilities.call("llm.router.v1", {"action": "list_harnesses"})
    assert ls_res.ok is True
    assert ls_res.data["count"] == 2
    assert ls_res.data["harnesses"][0]["harness_id"] == "codex"
    assert ls_res.data["harnesses"][0]["configured"] is True
    assert ls_res.data["harnesses"][0]["effort"] == "high"
    assert ls_res.data["harnesses"][1]["configured"] is False

    models_res = await ctx.capabilities.call("llm.router.v1", {"action": "list_models"})
    assert models_res.ok is True
    assert models_res.data["groups"] == ["Routing groups", "ProviderA"]
    assert models_res.data["models"][0]["id"] == "group/omnidesk-active"
    assert models_res.data["models"][0]["is_group"] is True
    assert models_res.data["models"][1]["efforts"] == ["low", "high", "max"]

    quotas_res = await ctx.capabilities.call("llm.router.v1", {"action": "list_quotas"})
    assert quotas_res.ok is True
    assert quotas_res.data["source"] == "cli"
    assert quotas_res.data["quotas"][0]["provider"] == "provider-a"
    assert quotas_res.data["quotas"][0]["windows"][0]["remaining_pct"] == 80

    quotas_no_cli = await ctx.capabilities.call(
        "llm.router.v1",
        {"action": "list_quotas", "allow_cli_fallback": False},
    )
    assert quotas_no_cli.ok is False
    assert quotas_no_cli.data["error_code"] == "service_offline"
    assert quotas_no_cli.data["quotas"] == []

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_loopback_gateway_quotas_and_running_group_resolution(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    captured_auth: list[str] = []

    class _QuotaHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/v1/magpie/quotas":
                self.send_response(404)
                self.end_headers()
                return
            captured_auth.append(self.headers.get("Authorization", ""))
            body = json.dumps([{"provider": "local-p", "name": "Local", "kind": "plan", "windows": []}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _QuotaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fake_magpie = _write_fake_magpie(
            tmp_path,
            f"""
import json, sys
args = sys.argv[1:]
if args[:2] == ["status", "--json"]:
    print(json.dumps({{"running": True, "url": "http://127.0.0.1:{port}"}}))
elif args == ["group", "omnidesk-active"]:
    print("group/omnidesk-active  routing=smart  stays=session")
    print("  provider-x/model-main:high")
    print("  provider-y/model-backup")
else:
    sys.exit(1)
""",
        )
        ctx = PluginContext(
            manifest["id"],
            plugin_dir=PLUGIN_DIR,
            config_dir=tmp_path,
            config_schema=manifest["config_schema"],
        )
        ctx.set_config("magpie_bin", str(fake_magpie))
        ctx.set_secret("gateway_key", "gw-loopback-token")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        route_res = await ctx.capabilities.call("llm.router.v1", {"action": "current_route"})
        assert route_res.ok is True
        assert route_res.data["gateway_running"] is True
        assert route_res.data["degraded"] is False
        assert route_res.data["primary_member"] == "provider-x/model-main:high"
        assert route_res.data["fallbacks"] == ["provider-y/model-backup"]
        assert route_res.data["routing"] == "smart"
        assert route_res.data["stays"] == "session"

        col_output = "\n".join(
            [
                "2026/11/16 23:53:30 plugin: log line ignored",
                "  OmniDesk Active  group/omnidesk-active",
                "  routing   order",
                "  stays     turn",
                "  models    1 provider-a/model-one:high  extra notes",
                "            2 provider-b/model-two",
                "  rules     1 images -> provider-z/should-ignore at high",
                "  classifier group/classifier-ignore  tells intent",
            ]
        )
        parsed = module._parse_group_detail(col_output, "omnidesk-active", "smart")
        assert parsed["routing"] == "order"
        assert parsed["stays"] == "turn"
        assert parsed["primary_member"] == "provider-a/model-one:high"
        assert parsed["fallbacks"] == ["provider-b/model-two"]

        q_res = await ctx.capabilities.call("llm.router.v1", {"action": "list_quotas"})
        assert q_res.ok is True
        assert q_res.data["source"] == "gateway"
        assert q_res.data["quotas"][0]["provider"] == "local-p"
        assert captured_auth == ["Bearer gw-loopback-token"]

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
