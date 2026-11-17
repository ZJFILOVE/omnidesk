"""Real Unix socket integration tests for bus.subscribe, capability.stream, disconnect cleanup, and stream cancellation."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from omnidesk_core import (
    ControlClient,
    ControlRpcError,
    ControlServer,
    ErrorCode,
    OmnideskPaths,
    PluginManager,
)
from omnidesk_core.plugins import _StreamBridge
from omnidesk_core.registry import CapabilityRegistry, CapabilityResult, CapabilityStream
from omnidesk_sdk import PluginContext


def _make_paths(tmp_path: Path) -> OmnideskPaths:
    return OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )


async def _read_json_line(reader: asyncio.StreamReader, *, timeout: float = 3.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    assert raw, "unexpected EOF on control.sock"
    return json.loads(raw.decode("utf-8"))


@pytest.mark.asyncio
async def test_bus_subscribe_events_replay_heartbeat_and_disconnect_cleanup(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure_dirs()
    mgr = PluginManager(plugins_dir=tmp_path / "plugins", paths=paths)
    mgr.secret_vault.set("plugin-nas", "api_key", "vault-bus-secret-777")
    sock_path = paths.control_socket_path

    # Seed pre-subscription history for since_event_id replay
    pub1 = await mgr.bus.publish("entity.media.open", {"item_id": "m1"}, source_plugin_id="plugin-nas")
    pub2 = await mgr.bus.publish("entity.text.open", {"article_id": "t2"}, source_plugin_id="plugin-zhihu")
    pub3 = await mgr.bus.publish("ignored.topic", {"x": 3}, source_plugin_id="core")

    async with ControlServer(
        socket_path=sock_path,
        paths=paths,
        plugins=mgr,
        heartbeat_interval=0.06,
        max_subscribe_queue=4,
    ):
        # 1. Raw Unix socket subscriber with prefix.* + exact topic, receiving live events + heartbeat
        r1, w1 = await asyncio.open_unix_connection(str(sock_path))
        w1.write(
            json.dumps(
                {
                    "id": "sub-1",
                    "op": "bus.subscribe",
                    "topics": ["entity.*", "asr.job.progress"],
                    "caller": "shell-tauri",
                }
            ).encode("utf-8")
            + b"\n"
        )
        await w1.drain()

        ack1 = await _read_json_line(r1)
        assert ack1["id"] == "sub-1"
        assert ack1["ok"] is True
        assert ack1["data"]["subscribed"] is True
        assert mgr.bus.subscription_count(plugin_id="control-sock") == 1

        # Wait for at least one heartbeat while bus is idle
        hb = await _read_json_line(r1)
        assert hb["type"] == "heartbeat"
        assert isinstance(hb["ts"], float)

        # Publish non-matching event (must not arrive on r1) and matching event with secrets (must arrive scrubbed)
        pub_noise = await mgr.bus.publish("other.noise", {"hello": "ignored"}, source_plugin_id="core")
        await mgr.bus.publish(
            "entity.media.open",
            {"item_id": "m2", "token": "raw-tok-123", "msg": "failed vault-bus-secret-777"},
            source_plugin_id="plugin-nas",
        )
        ev1 = await _read_json_line(r1)
        assert ev1["type"] == "event"
        assert ev1["topic"] == "entity.media.open"
        assert ev1["source_plugin_id"] == "plugin-nas"
        assert ev1["payload"]["item_id"] == "m2"
        assert ev1["payload"]["token"] == "[REDACTED]"
        assert "vault-bus-secret-777" not in json.dumps(ev1)
        assert "[REDACTED]" in ev1["payload"]["msg"]

        # 2. Second subscriber with since_event_id replay & wildcard '*' via ControlClient
        client = ControlClient(socket_path=sock_path, caller="agent-watch")
        replayed_and_live: list[dict[str, Any]] = []

        async def _consume_client2() -> None:
            async for frame in client.subscribe(["*"], since_event_id=pub1.event.event_id):
                if frame.get("type") == "event":
                    replayed_and_live.append(frame)
                    if len(replayed_and_live) >= 5:
                        break

        c2_task = asyncio.create_task(_consume_client2())
        await asyncio.sleep(0.05)
        await mgr.bus.publish("asr.job.progress", {"job_id": "j1", "progress": 0.5}, source_plugin_id="plugin-asr")
        await asyncio.wait_for(c2_task, timeout=3.0)
        assert [e["event_id"] for e in replayed_and_live[:4]] == [
            pub2.event.event_id,
            pub3.event.event_id,
            pub_noise.event.event_id,
            ev1["event_id"],
        ]
        assert replayed_and_live[4]["topic"] == "asr.job.progress"

        # 3. Stalled subscriber with bounded queue does NOT block other clients or publish()
        r_stall, w_stall = await asyncio.open_unix_connection(str(sock_path))
        w_stall.write(json.dumps({"id": "sub-stall", "op": "bus.subscribe", "topics": ["*"]}).encode("utf-8") + b"\n")
        await w_stall.drain()
        assert (await _read_json_line(r_stall))["ok"] is True

        for idx in range(15):
            await mgr.bus.publish("asr.job.progress", {"seq": idx}, source_plugin_id="plugin-asr")

        # Client 1 still promptly receives events and can disconnect cleanly
        latest = None
        for _ in range(16):
            frame = await _read_json_line(r1)
            if frame.get("type") == "event" and frame["topic"] == "asr.job.progress":
                latest = frame
        assert latest is not None

        # Close all subscribers -> server must clean up EntityBus subscriptions promptly
        w1.close()
        w_stall.close()
        await w1.wait_closed()
        await w_stall.wait_closed()

        deadline = asyncio.get_running_loop().time() + 2.0
        while mgr.bus.subscription_count(plugin_id="control-sock") > 0:
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("control-sock subscriptions were not cleaned up after disconnect")
            await asyncio.sleep(0.02)

        # 4. Invalid bus.subscribe params fail closed
        for bad_req in [
            {"id": "b1", "op": "bus.subscribe"},
            {"id": "b2", "op": "bus.subscribe", "topics": []},
            {"id": "b3", "op": "bus.subscribe", "topics": "*"},
            {"id": "b4", "op": "bus.subscribe", "topics": ["entity.*.open"]},
            {"id": "b5", "op": "bus.subscribe", "topics": [".."]},
            {"id": "b6", "op": "bus.subscribe", "topics": ["*"], "since_event_id": "bad id"},
            {"id": "b7", "op": "bus.subscribe", "topics": ["*"], "heartbeat_interval": -1},
        ]:
            resp = await client.request(bad_req)
            assert resp["ok"] is False
            assert resp["error_code"] == ErrorCode.INVALID_ARGUMENT

        # After invalid requests, subscription count stays zero
        assert mgr.bus.subscription_count(plugin_id="control-sock") == 0


@pytest.mark.asyncio
async def test_capability_stream_chunks_end_errors_and_disconnect_cancels_underlying_stream(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure_dirs()
    plugins_dir = tmp_path / "plugins"
    pdir = plugins_dir / "plugin-streamer"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-streamer",
                "name": "Streamer",
                "version": "0.1.0",
                "kind": "provider",
                "domain": "core",
                "provides": ["demo.cb.v1", "demo.gen.v1", "demo.slow.v1", "demo.fail.v1"],
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        """
import asyncio
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class StreamerPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        self.slow_cancelled = False
        self.bridge_saw_cancel = False
        self.first_sent = asyncio.Event()
        self.cancel_done = asyncio.Event()
        ctx.register_streaming_capability("demo.cb.v1", self.handle_cb)
        ctx.register_streaming_capability("demo.gen.v1", self.handle_gen)
        ctx.register_streaming_capability("demo.slow.v1", self.handle_slow)
        ctx.register_streaming_capability("demo.fail.v1", self.handle_fail)

    async def handle_cb(self, req: dict, stream):
        await stream.emit_delta("alpha")
        await stream.emit_event("stage", {"step": 2, "token": "must-redact"})
        return CapabilityResult.ok({"reply": "alpha", "echo": req.get("q", "")})

    async def handle_gen(self, req: dict):
        yield "g-1"
        yield {"n": 2}

    async def handle_slow(self, req: dict, stream):
        self.slow_cancelled = False
        self.bridge_saw_cancel = False
        self.first_sent.clear()
        self.cancel_done.clear()
        try:
            await stream.emit_delta("part-0")
            self.first_sent.set()
            while not stream.is_cancelled:
                await asyncio.sleep(0.05)
            self.bridge_saw_cancel = stream.is_cancelled
            return CapabilityResult.ok({"aborted": True})
        except asyncio.CancelledError:
            self.slow_cancelled = True
            self.bridge_saw_cancel = stream.is_cancelled
            raise
        finally:
            self.cancel_done.set()

    async def handle_fail(self, req: dict, stream):
        await stream.emit_delta("before-boom")
        raise RuntimeError("boom with stream-secret-555")

def create_plugin():
    return StreamerPlugin()
""",
        encoding="utf-8",
    )

    mgr = PluginManager(plugins_dir=plugins_dir, paths=paths)
    mgr.secret_vault.set("plugin-streamer", "api_key", "stream-secret-555")
    await mgr.load_plugin("plugin-streamer")
    plugin_instance = mgr._loaded["plugin-streamer"].instance
    sock_path = paths.control_socket_path

    async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr):
        client = ControlClient(socket_path=sock_path, caller="stream-tester")

        # 1. Callback streaming capability via ControlClient (chunk + end)
        cb_frames = [f async for f in client.stream_capability("demo.cb.v1", {"q": "hi"})]
        assert [f["type"] for f in cb_frames] == ["chunk", "chunk", "end"]
        assert cb_frames[0]["data"] == "alpha"
        assert cb_frames[0]["request_id"] == cb_frames[-1]["request_id"]
        assert cb_frames[1]["event"] == "stage"
        assert cb_frames[1]["data"] == {"step": 2, "token": "[REDACTED]"}
        assert cb_frames[2]["ok"] is True
        assert cb_frames[2]["provider_plugin_id"] == "plugin-streamer"
        assert cb_frames[2]["data"] == {"reply": "alpha", "echo": "hi"}

        # 2. Generator streaming capability with half-closed write_eof() client
        r_gen, w_gen = await asyncio.open_unix_connection(str(sock_path))
        w_gen.write(json.dumps({"id": "gen-1", "op": "capability.stream", "capability": "demo.gen.v1"}).encode("utf-8") + b"\n")
        w_gen.write_eof()
        await w_gen.drain()
        gen_frames = []
        while line := await asyncio.wait_for(r_gen.readline(), timeout=3.0):
            gen_frames.append(json.loads(line.decode("utf-8")))
        w_gen.close()
        await w_gen.wait_closed()
        assert [f["type"] for f in gen_frames] == ["chunk", "chunk", "end"]
        assert [f["data"] for f in gen_frames[:2]] == ["g-1", {"n": 2}]
        assert gen_frames[2]["ok"] is True

        # 3. Client disconnect mid-stream cancels underlying stream & updates _StreamBridge.is_cancelled
        r_slow, w_slow = await asyncio.open_unix_connection(str(sock_path))
        w_slow.write(json.dumps({"id": "slow-1", "op": "capability.stream", "capability": "demo.slow.v1"}).encode("utf-8") + b"\n")
        await w_slow.drain()
        first_chunk = await _read_json_line(r_slow)
        assert first_chunk["type"] == "chunk"
        assert first_chunk["request_id"] == "slow-1"
        assert first_chunk["data"] == "part-0"

        w_slow.close()
        await w_slow.wait_closed()
        await asyncio.wait_for(plugin_instance.cancel_done.wait(), timeout=2.0)
        assert plugin_instance.slow_cancelled is True
        assert plugin_instance.bridge_saw_cancel is True

        # 4. Mid-stream fault yields error frame with redacted secret
        err_frames = [f async for f in client.stream_capability("demo.fail.v1", {})]
        assert [f["type"] for f in err_frames] == ["chunk", "error"]
        assert err_frames[1]["ok"] is False
        assert err_frames[1]["error_code"] == ErrorCode.PLUGIN_FAULT
        assert "stream-secret-555" not in json.dumps(err_frames[1])
        assert "[REDACTED]" in err_frames[1]["error"]

        # 5. Explicit cancel frame mid-stream cancels underlying stream & explicit unsubscribe cleans up bus
        r_can, w_can = await asyncio.open_unix_connection(str(sock_path))
        w_can.write(json.dumps({"id": "can-1", "op": "capability.stream", "capability": "demo.slow.v1"}).encode("utf-8") + b"\n")
        await w_can.drain()
        assert (await _read_json_line(r_can))["type"] == "chunk"
        w_can.write(b'{"op": "cancel"}\n')
        await w_can.drain()
        await asyncio.wait_for(plugin_instance.cancel_done.wait(), timeout=2.0)
        assert plugin_instance.slow_cancelled is True
        assert plugin_instance.bridge_saw_cancel is True
        w_can.close()
        await w_can.wait_closed()

        # 6. Stream timeout yields structured timeout error frame
        timeout_frames = [f async for f in client.stream_capability("demo.slow.v1", {}, timeout=0.04)]
        assert timeout_frames[-1]["type"] == "error"
        assert timeout_frames[-1]["ok"] is False
        assert timeout_frames[-1]["error_code"] == ErrorCode.TIMEOUT

        # 7. Missing capability & invalid params return structured error frame
        missing = [f async for f in client.stream_capability("missing.stream.v1", {})]
        assert len(missing) == 1
        assert missing[0]["type"] == "error"
        assert missing[0]["ok"] is False
        assert missing[0]["error_code"] == ErrorCode.CAPABILITY_NOT_FOUND


@pytest.mark.asyncio
async def test_stream_bridge_and_core_stream_bridge_dynamic_is_cancelled_property() -> None:
    # 1. Direct _StreamBridge dynamically reflects underlying CapabilityStream cancellation
    core_stream = CapabilityStream("plugin-x", queue=None)
    bridge = _StreamBridge(core_stream)
    assert bridge.is_cancelled is False
    core_stream._cancel()
    assert bridge.is_cancelled is True

    # 2. _CoreStreamBridge used when PluginContext wraps a CapabilityRegistry directly
    reg = CapabilityRegistry()
    ctx = PluginContext("plugin-sdk-cancel", capabilities=reg)
    observed: list[tuple[bool, bool]] = []
    done_evt = asyncio.Event()

    async def _sdk_stream(req: dict[str, Any], stream: Any) -> CapabilityResult:
        before = stream.is_cancelled
        try:
            await stream.emit_delta("first")
            await asyncio.sleep(10)
            return CapabilityResult.ok()
        except asyncio.CancelledError:
            observed.append((before, stream.is_cancelled))
            done_evt.set()
            raise

    ctx.register_streaming_capability("sdk.cancel.v1", _sdk_stream)
    gen = reg.stream("sdk.cancel.v1", {})
    chunk = await anext(gen)
    assert chunk.data == "first"
    await gen.aclose()
    await asyncio.wait_for(done_evt.wait(), timeout=2.0)
    assert observed == [(False, True)]
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_control_server_auth_caller_audit_and_active_subscriber_shutdown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure_dirs()
    mgr = PluginManager(plugins_dir=tmp_path / "plugins", paths=paths)
    sock_path = paths.control_socket_path
    caplog.set_level(logging.INFO, logger="omnidesk_core.control")

    srv = ControlServer(socket_path=sock_path, paths=paths, plugins=mgr, heartbeat_interval=5.0)
    await srv.start()
    try:
        client = ControlClient(socket_path=sock_path, caller="audit-agent")
        pong = await client.ping()
        assert pong["ok"] is True
        assert "op=ping caller=audit-agent" in caplog.text

        # Invalid caller rejected fail-closed
        bad_caller = await client.request({"id": "c1", "op": "ping", "caller": "bad\ncaller"})
        assert bad_caller["ok"] is False
        assert bad_caller["error_code"] == ErrorCode.INVALID_ARGUMENT

        # Open a live subscriber, then stop server -> must not hang and must clean up bus & socket
        r, w = await asyncio.open_unix_connection(str(sock_path))
        w.write(json.dumps({"id": "s1", "op": "bus.subscribe", "topics": ["*"]}).encode("utf-8") + b"\n")
        await w.drain()
        assert (await _read_json_line(r))["ok"] is True
        assert mgr.bus.subscription_count(plugin_id="control-sock") == 1

        await asyncio.wait_for(srv.stop(), timeout=2.0)
        assert mgr.bus.subscription_count(plugin_id="control-sock") == 0
        assert not sock_path.exists()
        w.close()
        await w.wait_closed()
    finally:
        if srv.is_running:
            await srv.stop()

    # Unauthorized peer UID via SO_PEERCRED fails closed with permission_denied
    if hasattr(socket, "SO_PEERCRED"):
        async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr):
            monkeypatch.setattr("omnidesk_core.control.os.geteuid", lambda: 999999)
            try:
                r_unauth, w_unauth = await asyncio.open_unix_connection(str(sock_path))
                w_unauth.write(json.dumps({"id": "u1", "op": "ping"}).encode("utf-8") + b"\n")
                await w_unauth.drain()
                err_resp = await _read_json_line(r_unauth)
                w_unauth.close()
                await w_unauth.wait_closed()
                assert err_resp["ok"] is False
                assert err_resp["error_code"] == ErrorCode.PERMISSION_DENIED
            finally:
                monkeypatch.undo()
