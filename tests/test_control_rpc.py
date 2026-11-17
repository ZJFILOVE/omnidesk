"""Tests for control.sock Unix domain socket (0600) single-request JSON-RPC server & client."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from omnidesk_core import (
    FAULT_TOPIC,
    BusEvent,
    ControlClient,
    ControlRpcError,
    ControlServer,
    ErrorCode,
    JobCenter,
    OmnideskPaths,
    PathSafetyError,
    PluginManager,
    SearchIndex,
    scaffold_plugin,
)


@pytest.mark.asyncio
async def test_control_socket_permissions_and_single_request_roundtrip(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    pdir = scaffold_plugin(
        tmp_path / "plugins" / "plugin-echo",
        plugin_id="plugin-echo",
        provides=["demo.echo.v1"],
    )
    mgr = PluginManager(plugins_dir=tmp_path / "plugins", paths=paths)
    await mgr.load_plugin(pdir)

    sock_path = tmp_path / "run" / "control.sock"
    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr) as srv:
        assert srv.is_running is True
        assert stat.S_IMODE(sock_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600

        client = ControlClient(socket_path=sock_path)
        pong = await client.ping()
        assert pong["ok"] is True
        assert pong["data"]["status"] == "ok"
        assert pong["data"]["plugins"] == 1

        # OmniDesk capability.call envelope
        cap_res = await client.call_capability("demo.echo.v1", {"hello": "omnidesk"})
        assert cap_res.ok is True
        assert cap_res.provider_plugin_id == "plugin-echo"
        assert cap_res.data["echo"] == {"hello": "omnidesk"}

        # Sync client call works cleanly from a worker thread while server runs on main loop
        sync_res = await asyncio.to_thread(client.call_capability_sync, "demo.echo.v1", {"sync": True})
        assert sync_res.ok is True
        assert sync_res.data["echo"] == {"sync": True}

        # Bus publish over control.sock
        seen: list[BusEvent] = []
        mgr.bus.subscribe("entity.text.open", lambda evt: seen.append(evt), plugin_id="core")
        pub_resp = await client.publish("entity.text.open", {"article_id": "art-7"})
        assert pub_resp["ok"] is True
        assert pub_resp["data"]["delivered"] == 1
        assert seen[0].payload == {"article_id": "art-7"}

        # Raw EOF-terminated single-request JSON without trailing newline also works
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps({"id": "eof-1", "op": "capability.list"}).encode("utf-8"))
        writer.write_eof()
        await writer.drain()
        raw_reply = await reader.read()
        writer.close()
        await writer.wait_closed()
        eof_json = json.loads(raw_reply.decode("utf-8"))
        assert eof_json["id"] == "eof-1"
        assert eof_json["ok"] is True
        assert len(eof_json["data"]["capabilities"]) == 1

        # JSON-RPC 2.0 request/response envelope
        rpc_resp = await client.call_jsonrpc(
            "capability.call",
            {"capability": "demo.echo.v1", "payload": {"mode": "jsonrpc"}},
            request_id=99,
        )
        assert rpc_resp["jsonrpc"] == "2.0"
        assert rpc_resp["id"] == 99
        assert rpc_resp["result"]["ok"] is True
        assert rpc_resp["result"]["data"]["echo"] == {"mode": "jsonrpc"}

    assert not sock_path.exists()


@pytest.mark.asyncio
async def test_control_rpc_error_structure_fault_redaction_and_safety(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_root = tmp_path / "plugins"
    pdir = plugins_root / "plugin-faulty"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-faulty",
                "name": "Faulty",
                "version": "0.1.0",
                "kind": "provider",
                "domain": "core",
                "provides": ["faulty.boom.v1"],
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        """
from omnidesk_sdk import BasePlugin, PluginContext

class FaultyPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("faulty.boom.v1", self.boom)

    async def boom(self, req: dict):
        raise RuntimeError("failed with vault-rpc-secret-888 and Bearer token999999")

def create_plugin():
    return FaultyPlugin()
""",
        encoding="utf-8",
    )

    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    mgr.secret_vault.set("plugin-faulty", "api_key", "vault-rpc-secret-888")
    await mgr.load_plugin("plugin-faulty")
    faults: list[BusEvent] = []
    mgr.bus.subscribe(FAULT_TOPIC, lambda evt: faults.append(evt), plugin_id="core")

    sock_path = tmp_path / "run" / "control.sock"
    client = ControlClient(socket_path=sock_path)

    # Offline server error
    with pytest.raises(ControlRpcError) as off_exc:
        await client.ping()
    assert off_exc.value.code == ErrorCode.SERVICE_OFFLINE

    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr, max_payload_bytes=512):
        # Missing capability error structure
        missing = await client.request({"id": "m1", "op": "capability.call", "capability": "missing.cap.v1"})
        assert missing["ok"] is False
        assert missing["id"] == "m1"
        assert missing["error_code"] == ErrorCode.CAPABILITY_NOT_FOUND
        assert missing["data"]["error_code"] == ErrorCode.CAPABILITY_NOT_FOUND
        assert missing["error_detail"]["code"] == ErrorCode.CAPABILITY_NOT_FOUND

        # Faulting capability emits plugin.fault and redacts secrets in RPC error
        boom = await client.call_capability("faulty.boom.v1", {})
        assert boom.ok is False
        assert boom.data["error_code"] == ErrorCode.PLUGIN_FAULT
        assert boom.provider_plugin_id == "plugin-faulty"
        assert "vault-rpc-secret-888" not in boom.error
        assert "token999999" not in boom.error
        assert "[REDACTED]" in boom.error
        assert len(faults) == 1

        # Unknown operation in JSON-RPC 2.0 format
        rpc_unknown = await client.call_jsonrpc("does.not.exist", {}, request_id="r-404")
        assert rpc_unknown["jsonrpc"] == "2.0"
        assert rpc_unknown["id"] == "r-404"
        assert rpc_unknown["error"]["code"] == -32601
        assert rpc_unknown["error"]["data"]["error_code"] == "unknown_operation"

        # Raw malformed JSON & non-object JSON & oversized request
        for raw_payload, expected_code in [
            (b"{broken-json\n", "invalid_json"),
            (b"[1, 2, 3]\n", "invalid_request"),
            (b'{"op": "ping", "pad": "' + b"x" * 700 + b'"}\n', "invalid_request"),
        ]:
            r, w = await asyncio.open_unix_connection(str(sock_path))
            w.write(raw_payload)
            await w.drain()
            line = await r.readline()
            w.close()
            await w.wait_closed()
            parsed = json.loads(line.decode("utf-8"))
            assert parsed["ok"] is False
            assert parsed["error_code"] == expected_code

    # Symlinked control.sock is rejected
    real_file = tmp_path / "real.sock"
    real_file.write_text("", encoding="utf-8")
    sym_sock = tmp_path / "sym.sock"
    sym_sock.symlink_to(real_file)
    with pytest.raises(PathSafetyError):
        await ControlServer(socket_path=sym_sock).start()
    with pytest.raises(PathSafetyError):
        await ControlClient(socket_path=sym_sock).ping()


@pytest.mark.asyncio
async def test_config_rpc_list_get_set_never_echoes_secret_plaintext(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_dir = tmp_path / "plugins"
    pdir = plugins_dir / "plugin-cfg-rpc"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-cfg-rpc",
                "name": "Config RPC",
                "version": "0.1.0",
                "kind": "provider",
                "domain": "core",
                "config_schema": [
                    {"key": "endpoint", "label": "Endpoint", "type": "string", "default": "http://127.0.0.1:3425"},
                    {"key": "gateway_key", "label": "Gateway Key", "type": "secret", "default": ""},
                ],
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        "from omnidesk_sdk import BasePlugin\n\nclass CfgPlugin(BasePlugin):\n    pass\n\ndef create_plugin():\n    return CfgPlugin()\n",
        encoding="utf-8",
    )

    mgr = PluginManager(plugins_dir=plugins_dir, paths=paths)
    await mgr.load_plugin("plugin-cfg-rpc")
    sock_path = paths.control_socket_path

    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr):
        client = ControlClient(socket_path=sock_path, caller="ui-settings")

        # Initial config.list shows schema default and unset secret status
        listed0 = await client.config_list()
        assert listed0["ok"] is True
        entry0 = listed0["data"]["plugins"][0]
        assert entry0["plugin_id"] == "plugin-cfg-rpc"
        assert entry0["config"] == {"endpoint": "http://127.0.0.1:3425"}
        assert entry0["secrets"] == {"gateway_key": {"set": False}}

        # config.set plain + secret -> response must NOT echo secret plaintext
        secret_val = "sk-super-secret-token-98765"
        set_resp = await client.config_set(
            "plugin-cfg-rpc",
            values={"endpoint": "http://127.0.0.1:4444", "gateway_key": secret_val},
        )
        assert set_resp["ok"] is True
        assert secret_val not in json.dumps(set_resp)
        assert set_resp["data"]["updated_keys"] == ["endpoint"]
        assert set_resp["data"]["secret_keys_updated"] == ["gateway_key"]
        assert set_resp["data"]["config"] == {"endpoint": "http://127.0.0.1:4444"}
        assert set_resp["data"]["secrets"] == {"gateway_key": {"set": True}}

        # Verify SecretVault stored it cleanly while plain config JSON did not
        assert mgr.secret_vault.get("plugin-cfg-rpc", "gateway_key") == secret_val
        assert secret_val not in paths.plugin_config_path("plugin-cfg-rpc").read_text(encoding="utf-8")

        # config.list and config.get (full) never expose plaintext secret
        listed1 = await client.config_list(plugin_id="plugin-cfg-rpc")
        assert secret_val not in json.dumps(listed1)
        assert listed1["data"]["plugins"][0]["secrets"] == {"gateway_key": {"set": True}}

        got_all = await client.config_get("plugin-cfg-rpc")
        assert got_all["ok"] is True
        assert secret_val not in json.dumps(got_all)
        assert got_all["data"]["config"]["endpoint"] == "http://127.0.0.1:4444"
        assert got_all["data"]["secrets"]["gateway_key"] == {"set": True}

        # config.get on plain key returns value
        got_plain = await client.config_get("plugin-cfg-rpc", key="endpoint")
        assert got_plain["ok"] is True
        assert got_plain["data"]["value"] == "http://127.0.0.1:4444"
        assert got_plain["data"]["is_secret"] is False

        # config.get on secret key without status fails closed; status=True returns only boolean status
        got_sec_denied = await client.config_get("plugin-cfg-rpc", key="gateway_key")
        assert got_sec_denied["ok"] is False
        assert got_sec_denied["error_code"] == ErrorCode.PERMISSION_DENIED
        assert secret_val not in json.dumps(got_sec_denied)

        got_sec_status = await client.config_get("plugin-cfg-rpc", key="gateway_key", status=True)
        assert got_sec_status["ok"] is True
        assert got_sec_status["data"] == {"plugin_id": "plugin-cfg-rpc", "key": "gateway_key", "is_secret": True, "set": True}
        assert secret_val not in json.dumps(got_sec_status)

        # Clear secret via key=..., value=None
        cleared = await client.config_set("plugin-cfg-rpc", key="gateway_key", value=None, is_secret=True)
        assert cleared["ok"] is True
        assert cleared["data"]["secrets"]["gateway_key"] == {"set": False}
        assert mgr.secret_vault.get("plugin-cfg-rpc", "gateway_key") is None

        # Invalid inputs fail closed
        for bad_req in [
            {"id": "e1", "op": "config.get"},
            {"id": "e2", "op": "config.get", "plugin_id": "../evil"},
            {"id": "e3", "op": "config.set", "plugin_id": "plugin-cfg-rpc"},
            {"id": "e4", "op": "config.set", "plugin_id": "plugin-cfg-rpc", "key": "endpoint", "value": "x", "is_secret": True},
            {"id": "e5", "op": "config.set", "plugin_id": "plugin-cfg-rpc", "key": "gateway_key", "value": 123},
            {"id": "e6", "op": "config.set", "plugin_id": "plugin-cfg-rpc", "values": {"nested": {"api_key": "leak"}}},
        ]:
            resp = await client.request(bad_req)
            assert resp["ok"] is False


@pytest.mark.asyncio
async def test_domain_snapshot_empty_and_populated_over_unix_socket(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_dir = tmp_path / "plugins"
    pdir = plugins_dir / "plugin-dom"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-dom",
                "name": "Domain Plugin",
                "version": "0.1.0",
                "kind": "hybrid",
                "domain": "media",
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        """
from omnidesk_sdk import BasePlugin, BookSource, DockWidgetSpec, MediaSource, PluginContext, TextSource, WorkspaceTabSpec

class DomMedia(MediaSource):
    source_id = "dom-media"
    display_name = "Dom Media"

class DomText(TextSource):
    source_id = "dom-text"
    display_name = "Dom Text"
    category = "blog"

class DomBook(BookSource):
    source_id = "dom-book"
    display_name = "Dom Book"

class DomPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        ctx.register_media_source(DomMedia())
        ctx.register_text_source(DomText())
        ctx.register_book_source(DomBook())
        ctx.register_workspace_tab(
            WorkspaceTabSpec(
                tab_id="dom-tab",
                title="Domain Tab",
                domain="media",
                ui_schema={"type": "Section", "title": "Main"},
                render=lambda: {"type": "Markdown", "text": "live"},
                on_action=lambda action, state: {"ok": True},
            )
        )
        ctx.register_dock_widget(
            DockWidgetSpec(
                widget_id="dom-dock",
                title="Domain Dock",
                supported_domains=["media", "text"],
                ui_schema={"type": "KeyValue", "items": []},
                render=lambda: {"type": "KeyValue"},
                on_action=lambda action, state: {"ok": True},
            )
        )

def create_plugin():
    return DomPlugin()
""",
        encoding="utf-8",
    )

    mgr = PluginManager(plugins_dir=plugins_dir, paths=paths)
    sock_path = paths.control_socket_path

    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr):
        client = ControlClient(socket_path=sock_path)

        # 1. Empty DomainRegistry returns empty arrays, never an error
        empty_snap = await client.domain_snapshot()
        assert empty_snap["ok"] is True
        assert empty_snap["data"] == {
            "media_sources": [],
            "text_sources": [],
            "book_sources": [],
            "workspace_tabs": [],
            "dock_widgets": [],
        }

        # 2. Load plugin -> snapshot exposes sources, tabs, and dock widgets as JSON without render/on_action
        await mgr.load_plugin("plugin-dom")
        snap = await client.domain_snapshot()
        assert snap["ok"] is True
        data = snap["data"]
        assert [s["source_id"] for s in data["media_sources"]] == ["dom-media"]
        assert [s["source_id"] for s in data["text_sources"]] == ["dom-text"]
        assert [s["source_id"] for s in data["book_sources"]] == ["dom-book"]
        assert len(data["workspace_tabs"]) == 1
        assert len(data["dock_widgets"]) == 1
        tab_wire = data["workspace_tabs"][0]
        dock_wire = data["dock_widgets"][0]
        assert tab_wire["tab_id"] == "dom-tab"
        assert tab_wire["ui_schema"] == {"type": "Section", "title": "Main"}
        assert "render" not in tab_wire and "on_action" not in tab_wire
        assert dock_wire["widget_id"] == "dom-dock"
        assert "render" not in dock_wire and "on_action" not in dock_wire
        json.dumps(snap)  # must be strictly JSON-serializable

        # 3. Domain filter & invalid filter fail closed
        book_only = await client.domain_snapshot(domain="book")
        assert book_only["ok"] is True
        assert book_only["data"]["media_sources"] == []
        assert len(book_only["data"]["book_sources"]) == 1
        assert book_only["data"]["workspace_tabs"] == []
        assert book_only["data"]["dock_widgets"] == []

        bad_dom = await client.domain_snapshot(domain="invalid")
        assert bad_dom["ok"] is False
        assert bad_dom["error_code"] == ErrorCode.INVALID_ARGUMENT

        # 4. Unload restores empty snapshot
        await mgr.unload_plugin("plugin-dom")
        after_unload = await client.domain_snapshot()
        assert after_unload["ok"] is True
        assert after_unload["data"] == {
            "media_sources": [],
            "text_sources": [],
            "book_sources": [],
            "workspace_tabs": [],
            "dock_widgets": [],
        }


@pytest.mark.asyncio
async def test_ui_action_dispatches_workspace_tabs_and_dock_widgets(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_dir = tmp_path / "plugins"
    pdir = plugins_dir / "plugin-ui-action"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-ui-action",
                "name": "UI Action Plugin",
                "version": "0.1.0",
                "kind": "hybrid",
                "domain": "media",
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        """
from omnidesk_sdk import BasePlugin, DockWidgetSpec, PluginContext, WorkspaceTabSpec

class UiActionPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        async def tab_action(action, state):
            return {
                "ok": True,
                "action": action,
                "state": state,
                "ui_schema": {"type": "Markdown", "content": f"已执行 {action}"},
            }

        def dock_action(payload):
            return {
                "ok": True,
                "received": payload,
                "ui_schema": {"type": "Markdown", "content": "组件已刷新"},
            }

        ctx.register_workspace_tab(
            WorkspaceTabSpec(
                tab_id="ui-tab",
                title="UI Tab",
                domain="media",
                ui_schema={"type": "ActionRow", "actions": [{"id": "search", "label": "搜索"}]},
                on_action=tab_action,
            )
        )
        ctx.register_workspace_tab(
            WorkspaceTabSpec(
                tab_id="ui-no-action",
                title="No Action",
                domain="media",
                ui_schema={"type": "Markdown", "content": "read only"},
            )
        )
        ctx.register_dock_widget(
            DockWidgetSpec(
                widget_id="ui-dock",
                title="UI Dock",
                supported_domains=["media"],
                ui_schema={"type": "ActionRow", "actions": [{"id": "refresh", "label": "刷新"}]},
                on_action=dock_action,
            )
        )

def create_plugin():
    return UiActionPlugin()
""",
        encoding="utf-8",
    )

    mgr = PluginManager(plugins_dir=plugins_dir, paths=paths)
    await mgr.load_plugin("plugin-ui-action")
    sock_path = paths.control_socket_path

    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr) as srv:
        assert srv.is_running is True
        client = ControlClient(socket_path=sock_path, caller="ui-plugin-tab")

        tab_res = await client.ui_action(
            kind="workspace_tab",
            spec_id="ui-tab",
            action_id="search",
            plugin_id="plugin-ui-action",
            state={"query": "rust"},
        )
        assert tab_res["ok"] is True
        assert tab_res["provider_plugin_id"] == "plugin-ui-action"
        assert tab_res["data"]["action"] == "search"
        assert tab_res["data"]["state"] == {"query": "rust"}
        assert tab_res["data"]["ui_schema"] == {
            "type": "Markdown",
            "content": "已执行 search",
        }
        json.dumps(tab_res)

        # One-argument handlers receive the action merged into state.
        dock_res = await client.ui_action(
            kind="dock_widget",
            spec_id="ui-dock",
            action_id="refresh",
            plugin_id="plugin-ui-action",
            state={"tab": "media"},
        )
        assert dock_res["ok"] is True
        assert dock_res["data"]["received"] == {"tab": "media", "action": "refresh"}
        assert dock_res["data"]["ui_schema"]["content"] == "组件已刷新"

        # Invalid kinds/IDs/states fail closed at the RPC boundary.
        for params, expected in [
            (
                {"kind": "dialog", "spec_id": "ui-tab", "action_id": "search"},
                ErrorCode.INVALID_ARGUMENT,
            ),
            (
                {"kind": "workspace_tab", "spec_id": " ", "action_id": "search"},
                ErrorCode.INVALID_ARGUMENT,
            ),
            (
                {"kind": "workspace_tab", "spec_id": "ui-tab", "action_id": ""},
                ErrorCode.INVALID_ARGUMENT,
            ),
            (
                {"kind": "workspace_tab", "spec_id": "ui-tab", "action_id": "search", "state": []},
                ErrorCode.INVALID_ARGUMENT,
            ),
            (
                {
                    "kind": "workspace_tab",
                    "spec_id": "missing-tab",
                    "action_id": "search",
                },
                ErrorCode.NOT_FOUND,
            ),
            (
                {
                    "kind": "workspace_tab",
                    "spec_id": "ui-tab",
                    "action_id": "search",
                    "plugin_id": "plugin-other",
                },
                ErrorCode.NOT_FOUND,
            ),
            (
                {
                    "kind": "workspace_tab",
                    "spec_id": "ui-no-action",
                    "action_id": "search",
                },
                ErrorCode.CAPABILITY_NOT_FOUND,
            ),
        ]:
            resp = await client.request({"op": "ui.action", "params": params})
            assert resp["ok"] is False
            assert resp["error_code"] == expected


@pytest.mark.asyncio
async def test_domain_invoke_routes_by_source_id_and_validates_arguments(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_dir = tmp_path / "plugins"
    pdir = plugins_dir / "plugin-domrpc"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-domrpc",
                "name": "Domain RPC Plugin",
                "version": "0.1.0",
                "kind": "hybrid",
                "domain": "text",
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        """
from omnidesk_sdk import BasePlugin, PluginContext, TextSource
from omnidesk_sdk.domain_models import TextFeedItem

class AlphaText(TextSource):
    source_id = "alpha-text"
    display_name = "Alpha"
    category = "blog"
    priority = 1

    async def fetch_feed(self, channel_id="", query="", page=1):
        return [TextFeedItem(id="alpha-1", title="Alpha Article", channel_id=channel_id, tags=["alpha"])]

    def channels(self):  # sync handler is offloaded to a worker thread
        return [{"id": "alpha-channel", "name": "Alpha Channel"}]

class BetaText(TextSource):
    source_id = "beta-text"
    display_name = "Beta"
    category = "news"
    priority = 9

    async def fetch_feed(self, channel_id="", query="", page=1):
        return [TextFeedItem(id="beta-1", title="Beta Article", channel_id=channel_id, tags=["beta"])]

class DomRpcPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        ctx.register_text_source(AlphaText())
        ctx.register_text_source(BetaText())

def create_plugin():
    return DomRpcPlugin()
""",
        encoding="utf-8",
    )

    mgr = PluginManager(plugins_dir=plugins_dir, paths=paths)
    sock_path = paths.control_socket_path

    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr):
        client = ControlClient(socket_path=sock_path)
        await mgr.load_plugin("plugin-domrpc")

        # Default routing picks the highest-priority source and serializes pydantic models
        default_call = await client.request(
            {"op": "domain.invoke", "params": {"domain": "text", "method": "fetch_feed", "payload": {"query": "x"}}}
        )
        assert default_call["ok"] is True, default_call
        assert default_call["provider_plugin_id"] == "plugin-domrpc"
        assert default_call["data"]["source_id"] == "beta-text"
        assert default_call["data"]["result"][0]["id"] == "beta-1"
        json.dumps(default_call)

        # Explicit source_id pins the lower-priority source
        pinned = await client.request(
            {
                "op": "domain.invoke",
                "params": {
                    "domain": "text",
                    "source_id": "alpha-text",
                    "method": "fetch_feed",
                    "payload": {"channel_id": "hot"},
                },
            }
        )
        assert pinned["ok"] is True, pinned
        assert pinned["data"]["source_id"] == "alpha-text"
        assert pinned["data"]["result"][0]["channel_id"] == "hot"

        # Sync handlers work too
        sync_call = await client.request(
            {"op": "domain.invoke", "params": {"domain": "text", "source_id": "alpha-text", "method": "channels"}}
        )
        assert sync_call["ok"] is True, sync_call
        assert sync_call["data"]["result"] == [{"id": "alpha-channel", "name": "Alpha Channel"}]

        # Unknown source / method / argument all fail closed
        missing = await client.request(
            {"op": "domain.invoke", "params": {"domain": "text", "source_id": "nope", "method": "fetch_feed"}}
        )
        assert missing["ok"] is False
        assert missing["error_code"] == ErrorCode.NOT_FOUND

        bad_method = await client.request(
            {"op": "domain.invoke", "params": {"domain": "text", "method": "drop_database"}}
        )
        assert bad_method["ok"] is False
        assert bad_method["error_code"] == ErrorCode.INVALID_ARGUMENT

        bad_arg = await client.request(
            {
                "op": "domain.invoke",
                "params": {"domain": "text", "method": "fetch_feed", "payload": {"bogus": 1}},
            }
        )
        assert bad_arg["ok"] is False
        assert bad_arg["error_code"] == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_job_rpc_submit_get_list_cancel_and_search_rpc(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    sock_path = paths.control_socket_path
    jobs = JobCenter()
    search = SearchIndex(":memory:")
    try:
        async with ControlServer(
            socket_path=sock_path,
            paths=paths,
            jobs=jobs,
            search=search,
        ) as srv:
            client = ControlClient(socket_path=sock_path)

            async def handler(payload: dict[str, Any]) -> dict[str, Any]:
                return {"doubled": int(payload.get("n", 0)) * 2}

            srv.register_job_handler("demo.double", handler)

            submitted = await client.request(
                {
                    "op": "job.submit",
                    "params": {"kind": "demo.double", "payload": {"n": 21}, "title": "Double"},
                }
            )
            assert submitted["ok"] is True, submitted
            job_id = submitted["data"]["job"]["job_id"]

            record = None
            for _ in range(100):
                fetched = await client.request({"op": "job.get", "params": {"id": job_id}})
                assert fetched["ok"] is True, fetched
                record = fetched["data"]["job"]
                if record["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            assert record is not None and record["status"] == "completed"
            assert record["result"] == {"doubled": 42}

            listed = await client.request({"op": "job.list", "params": {"kind": "demo.double"}})
            assert listed["ok"] is True
            assert [j["job_id"] for j in listed["data"]["jobs"]] == [job_id]

            missing_kind = await client.request({"op": "job.submit", "params": {"kind": "nope"}})
            assert missing_kind["ok"] is False
            assert missing_kind["error_code"] == ErrorCode.INVALID_ARGUMENT

            upserted = await client.request(
                {
                    "op": "search.upsert",
                    "params": {
                        "documents": [
                            {
                                "id": "doc-1",
                                "title": "Attention Is All You Need",
                                "body": "transformer attention mechanism",
                                "domain": "text",
                                "tags": ["paper", "nlp"],
                            },
                            {"id": "doc-2", "title": "Cooking Rice", "body": "wash and steam"},
                        ]
                    },
                }
            )
            assert upserted["ok"] is True, upserted
            assert upserted["data"]["upserted"] == 2

            hits = await client.request(
                {"op": "search.query", "params": {"query": "attention", "domain": "text"}}
            )
            assert hits["ok"] is True, hits
            assert hits["data"]["hits"][0]["doc_id"] == "doc-1"
            assert hits["data"]["total"] == 1

            removed = await client.request({"op": "search.delete", "params": {"id": "doc-1"}})
            assert removed["ok"] is True and removed["data"]["deleted"] is True
            after = await client.request({"op": "search.query", "params": {"query": "attention"}})
            assert after["ok"] is True and after["data"]["total"] == 0
    finally:
        search.close()
