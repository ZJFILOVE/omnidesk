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
    OmnideskPaths,
    PathSafetyError,
    PluginManager,
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
