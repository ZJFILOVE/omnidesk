"""Regression tests for multi-provider Core CapabilityRegistry semantics and SDK parity."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_CORE_DIR = str(Path(__file__).resolve().parents[1] / "core")
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from omnidesk_core.bus import FAULT_TOPIC, BusEvent, EntityBus
from omnidesk_core.errors import CapabilityConflictError, CapabilityInvocationError, ErrorCode
from omnidesk_core.paths import OmnideskPaths
from omnidesk_core.plugins import PluginManager, PluginState
from omnidesk_core.registry import CapabilityRegistry, CapabilityResult, CapabilityStream


def _write_test_plugin(root: Path, plugin_id: str, provides: list[str], body: str) -> Path:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": plugin_id,
                "name": f"Test {plugin_id}",
                "version": "0.1.0",
                "kind": "provider",
                "domain": "media",
                "provides": provides,
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(body, encoding="utf-8")
    return pdir


def test_multi_provider_priority_tie_break_and_non_preempting_replace() -> None:
    reg = CapabilityRegistry()

    reg.register("book.source.v1", lambda req: {"from": "ebook"}, plugin_id="plugin-ebook", priority=100)
    reg.register("book.source.v1", lambda req: {"from": "babeldoc"}, plugin_id="plugin-pdf-babeldoc", priority=100)
    reg.register("book.source.v1", lambda req: {"from": "low"}, plugin_id="plugin-low", priority=-5)

    # Equal priority keeps first-registered winner; all providers appear in list_capabilities & providers_for
    assert reg.resolve("book.source.v1").plugin_id == "plugin-ebook"
    assert reg.get("book.source.v1").plugin_id == "plugin-ebook"
    assert [e.plugin_id for e in reg.providers_for("book.source.v1")] == [
        "plugin-ebook",
        "plugin-pdf-babeldoc",
        "plugin-low",
    ]
    assert [e.plugin_id for e in reg.list_capabilities()] == [
        "plugin-ebook",
        "plugin-pdf-babeldoc",
        "plugin-low",
    ]

    # replace=True for a new plugin adds alongside existing providers without preempting them
    reg.register(
        "book.source.v1",
        lambda req: {"from": "vip"},
        plugin_id="plugin-vip",
        priority=250,
        replace=True,
    )
    assert reg.resolve("book.source.v1").plugin_id == "plugin-vip"
    assert len(reg.list_capabilities(prefix="book.")) == 4

    # Same plugin replace=False conflicts; replace=True updates only itself with no duplicates
    with pytest.raises(CapabilityConflictError) as exc_info:
        reg.register("book.source.v1", lambda req: {"from": "ebook-2"}, plugin_id="plugin-ebook")
    assert exc_info.value.plugin_id == "plugin-ebook"
    assert exc_info.value.existing_plugin_id == "plugin-ebook"

    reg.register(
        "book.source.v1",
        lambda req: {"from": "ebook-2"},
        plugin_id="plugin-ebook",
        priority=300,
        replace=True,
    )
    assert len(reg.list_capabilities(plugin_id="plugin-ebook")) == 1
    assert len(reg.list_capabilities()) == 4
    assert reg.resolve("book.source.v1").plugin_id == "plugin-ebook"
    assert reg.call_sync("book.source.v1").data == {"from": "ebook-2"}


def test_list_versions_and_latest_version_select_winning_provider_per_version() -> None:
    reg = CapabilityRegistry()

    reg.register("book.search.v1", lambda req: {"v": 1, "p": "a"}, plugin_id="plugin-a", priority=50)
    reg.register("book.search.v1", lambda req: {"v": 1, "p": "b"}, plugin_id="plugin-b", priority=200)
    reg.register("book.search.v2", lambda req: {"v": 2, "p": "b"}, plugin_id="plugin-b", priority=200)
    reg.register("book.search.v2", lambda req: {"v": 2, "p": "a"}, plugin_id="plugin-a", priority=50)

    versions = reg.list_versions("book.search")
    assert [(v.version, v.plugin_id) for v in versions] == [(1, "plugin-b"), (2, "plugin-b")]
    latest = reg.latest_version("book.search")
    assert latest is not None and latest.capability_id == "book.search.v2" and latest.plugin_id == "plugin-b"
    assert len(reg.list_capabilities(prefix="book.search.")) == 4


@pytest.mark.asyncio
async def test_multi_provider_async_streaming_fault_isolation_and_redaction() -> None:
    bus = EntityBus()
    faults: list[BusEvent] = []
    bus.subscribe(FAULT_TOPIC, lambda evt: faults.append(evt), plugin_id="core")

    reg = CapabilityRegistry(bus=bus, secret_provider=lambda: ["multi-provider-secret-999"])

    # Standby healthy provider at lower priority
    async def fallback_stream(req: dict[str, Any]):
        yield f"ok-{req.get('tag', 'x')}"

    # Primary faulty streaming provider at higher priority
    async def broken_stream(req: dict[str, Any], stream: CapabilityStream) -> CapabilityResult:
        await stream.emit_delta("partial")
        raise RuntimeError("failed with multi-provider-secret-999 and api_key=leak123")

    reg.register_streaming("media.search.v1", fallback_stream, plugin_id="plugin-standby", priority=100)
    reg.register_streaming("media.search.v1", broken_stream, plugin_id="plugin-primary", priority=200)

    assert len(reg.list_capabilities(streaming=True)) == 2
    assert len(reg.list_capabilities(streaming=False)) == 0

    # Fault isolation attributes the active failing provider and redacts secrets
    forwarded: list[dict[str, Any]] = []
    res = await reg.call_stream(
        "media.search.v1",
        {"tag": "test"},
        on_chunk=forwarded.append,
        isolate_errors=True,
    )
    assert res.ok is False
    assert res.provider_plugin_id == "plugin-primary"
    assert res.data["error_code"] == ErrorCode.PLUGIN_FAULT
    assert "multi-provider-secret-999" not in res.error
    assert "leak123" not in res.error
    assert "[REDACTED]" in res.error
    assert [f["data"] for f in forwarded] == ["partial"]
    assert len(faults) == 1 and faults[0].payload["plugin_id"] == "plugin-primary"

    # Timeout and non-isolated error also attribute active high-priority provider
    async def slow_primary(req: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"p": "primary"}

    reg.register("media.fetch.v1", lambda req: {"p": "standby"}, plugin_id="plugin-standby", priority=50)
    reg.register("media.fetch.v1", slow_primary, plugin_id="plugin-primary", priority=200)
    with pytest.raises(CapabilityInvocationError) as timeout_exc:
        await reg.call("media.fetch.v1", {}, timeout=0.01, isolate_errors=False)
    assert timeout_exc.value.code == ErrorCode.TIMEOUT
    assert timeout_exc.value.plugin_id == "plugin-primary"

    # Revoking primary cleanly falls back to standby across async, sync, and streaming
    assert reg.revoke_plugin("plugin-primary") == 2
    assert (await reg.call("media.fetch.v1")).provider_plugin_id == "plugin-standby"
    assert reg.call_sync("media.fetch.v1").data == {"p": "standby"}

    standby_chunks: list[dict[str, Any]] = []
    stream_res = await reg.call_stream("media.search.v1", {"tag": "live"}, on_chunk=standby_chunks.append)
    assert stream_res.ok is True
    assert stream_res.provider_plugin_id == "plugin-standby"
    assert stream_res.data == {"chunks": ["ok-live"]}
    assert [c["data"] for c in standby_chunks] == ["ok-live"]


def test_multi_provider_concurrent_snapshot_safety() -> None:
    reg = CapabilityRegistry()
    shared_caps = ["media.source.v1", "book.source.v1", "book.search.v1"]

    def writer(idx: int) -> None:
        pid = f"plugin-w{idx}"
        for round_i in range(25):
            for cap_idx, cap in enumerate(shared_caps):
                reg.register(
                    cap,
                    lambda req, p=pid, r=round_i: {"plugin": p, "round": r},
                    plugin_id=pid,
                    priority=(idx + round_i + cap_idx) % 7,
                    replace=True,
                )
            _ = reg.unregister("book.search.v1", plugin_id=pid)
        assert reg.revoke_plugin(pid) == 2

    def reader() -> None:
        for _ in range(120):
            caps = reg.list_capabilities()
            assert all(c.capability_id in shared_caps for c in caps)
            for cap in shared_caps:
                _ = reg.has(cap)
                _ = reg.get(cap)
                _ = reg.providers_for(cap)
                _ = reg.list_versions(cap.rsplit(".", 1)[0])
                _ = reg.latest_version(cap.rsplit(".", 1)[0])
                try:
                    res = reg.call_sync(cap)
                    assert res.ok is True
                except Exception as exc:  # CapabilityNotFoundError when all writers momentarily revoked
                    assert exc.code == ErrorCode.CAPABILITY_NOT_FOUND

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(writer, i) for i in range(6)] + [pool.submit(reader) for _ in range(4)]
        for fut in futures:
            fut.result()

    assert reg.list_capabilities() == []


@pytest.mark.asyncio
async def test_plugin_manager_load_all_and_reload_with_shared_capabilities(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data")}
    )
    plugins_root = tmp_path / "plugins"

    _write_test_plugin(
        plugins_root,
        "plugin-fnos-nas",
        ["media.source.v1"],
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class NasPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("media.source.v1", lambda req: CapabilityResult.ok({"provider": "nas"}))

def create_plugin():
    return NasPlugin()
""",
    )
    music_dir = _write_test_plugin(
        plugins_root,
        "plugin-music",
        ["media.source.v1"],
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class MusicPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("media.source.v1", lambda req: CapabilityResult.ok({"provider": "music-v1"}))

def create_plugin():
    return MusicPlugin()
""",
    )
    _write_test_plugin(
        plugins_root,
        "plugin-zlib",
        ["book.search.v1"],
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class ZlibPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("book.search.v1", lambda req: CapabilityResult.ok({"provider": "zlib"}))

def create_plugin():
    return ZlibPlugin()
""",
    )
    _write_test_plugin(
        plugins_root,
        "plugin-annas-archive",
        ["book.search.v1"],
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class AnnasPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("book.search.v1", lambda req: CapabilityResult.ok({"provider": "annas"}))

def create_plugin():
    return AnnasPlugin()
""",
    )

    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    records = await mgr.load_all()
    assert all(r.state == PluginState.LOADED for r in records)
    assert len(mgr.capabilities.list_capabilities(prefix="media.source.v1")) == 2
    assert len(mgr.capabilities.list_capabilities(prefix="book.search.v1")) == 2

    # Hot-reload plugin-music without losing plugin-fnos-nas
    (music_dir / "plugin.py").write_text(
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class MusicPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("media.source.v1", lambda req: CapabilityResult.ok({"provider": "music-v2"}))

def create_plugin():
    return MusicPlugin()
""",
        encoding="utf-8",
    )
    reloaded = await mgr.reload_plugin("plugin-music")
    assert reloaded.state == PluginState.LOADED
    assert {c.plugin_id for c in mgr.capabilities.list_capabilities(prefix="media.source.v1")} == {
        "plugin-fnos-nas",
        "plugin-music",
    }

    await mgr.unload_plugin("plugin-fnos-nas")
    assert [c.plugin_id for c in mgr.capabilities.list_capabilities(prefix="media.source.v1")] == ["plugin-music"]
    res = await mgr.capabilities.call("media.source.v1", {})
    assert res.ok is True and res.data == {"provider": "music-v2"}
    await mgr.unload_all()
