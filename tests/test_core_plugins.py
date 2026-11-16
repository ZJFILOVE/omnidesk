"""Tests for schema_version=2 manifest validation, importlib plugin lifecycle, hot-reload, and plugin.fault isolation."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from omnidesk_core import (
    FAULT_TOPIC,
    PLUGIN_STATE_CHANGED_TOPIC,
    BusEvent,
    ErrorCode,
    OmnideskPaths,
    PluginFaultError,
    PluginManager,
    PluginManifestError,
    PluginNotFoundError,
    PluginState,
    load_manifest,
    scaffold_plugin,
    validate_manifest,
)


def _write_plugin(
    root: Path,
    plugin_id: str,
    plugin_py: str,
    *,
    manifest_overrides: dict[str, Any] | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "id": plugin_id,
        "name": f"Test {plugin_id}",
        "version": "0.1.0",
        "kind": "hybrid",
        "domain": "core",
        "provides": [],
        "consumes": [],
        "permissions": [],
        "config_schema": [],
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (pdir / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (pdir / "plugin.py").write_text(plugin_py, encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = pdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return pdir


def test_discover_prefers_first_plugin_root(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    installed_root = tmp_path / "installed"
    local_dir = _write_plugin(
        local_root,
        "plugin-duplicate",
        "from omnidesk_sdk import BasePlugin\n\nclass P(BasePlugin):\n    pass\n\ndef create_plugin():\n    return P()\n",
        manifest_overrides={"version": "2.0.0"},
    )
    installed_dir = installed_root / "plugin-duplicate"
    installed_dir.mkdir(parents=True)
    (installed_dir / "plugin.json").write_text(
        json.dumps({"schema_version": 1, "id": "plugin-duplicate", "abi_version": 1}),
        encoding="utf-8",
    )

    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    manager = PluginManager(plugin_dirs=[local_root, installed_root], paths=paths)
    records = manager.list_plugins()

    assert len([rec for rec in records if rec.plugin_id == "plugin-duplicate"]) == 1
    rec = manager.require("plugin-duplicate")
    assert rec.state == PluginState.DISCOVERED
    assert rec.plugin_dir == local_dir.resolve()
    assert rec.manifest is not None and rec.manifest.version == "2.0.0"


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_manifest_schema_v2_validation_accepts_valid_and_rejects_invalid(tmp_path: Path) -> None:
    valid = {
        "schema_version": 2,
        "id": "plugin-magpie-agent",
        "name": "Magpie AI Router",
        "version": "0.2.0-rc.1",
        "kind": "provider",
        "domain": "core",
        "provides": ["ai.agent.v1", "llm.router.v1"],
        "consumes": ["recommend.feed.v1"],
        "permissions": ["network:loopback", "secret:vault"],
        "repository": {"type": "git", "url": "https://github.com/example/plugin.git", "branch": "main"},
        "config_schema": [
            {"key": "strategy", "label": "Strategy", "type": "select", "default": "order", "options": ["order", "smart"]},
            {"key": "gateway_key", "label": "Key", "type": "secret", "default": ""},
        ],
    }
    m = validate_manifest(valid)
    assert m.schema_version == 2
    assert m.entry_module == "plugin"
    assert m.entry_callable == "create_plugin"
    assert m.secret_keys() == {"gateway_key"}
    assert m.default_config() == {"strategy": "order"}

    invalid_cases: list[dict[str, Any]] = [
        {**valid, "schema_version": 1},
        {**valid, "schema_version": "2"},
        {**valid, "schema_version": True},
        {**valid, "id": "../escape"},
        {**valid, "id": ".hidden"},
        {**valid, "name": "   "},
        {**valid, "version": "0.2"},
        {**valid, "kind": "unknown"},
        {**valid, "domain": "audio"},
        {**valid, "entry": "plugin_only"},
        {**valid, "provides": ["llm.chat"]},
        {**valid, "consumes": ["LLM.Chat.v1"]},
        {**valid, "unknown_field": 123},
        {**valid, "config_schema": [{"key": "mode", "label": "Mode", "type": "select", "options": []}]},
        {
            **valid,
            "config_schema": [
                {"key": "dup", "label": "A", "type": "string"},
                {"key": "dup", "label": "B", "type": "bool"},
            ],
        },
    ]
    for bad in invalid_cases:
        with pytest.raises(PluginManifestError):
            validate_manifest(bad)

    real_manifest = tmp_path / "real.json"
    real_manifest.write_text(json.dumps(valid), encoding="utf-8")
    link_dir = tmp_path / "sym-plugin"
    link_dir.mkdir()
    (link_dir / "plugin.json").symlink_to(real_manifest)
    with pytest.raises(PluginManifestError) as sym_exc:
        load_manifest(link_dir)
    assert sym_exc.value.code == ErrorCode.PATH_SAFETY


@pytest.mark.asyncio
async def test_plugin_importlib_load_sdk_interop_and_atomic_revocation(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_root = tmp_path / "plugins"

    alpha_code = """
import asyncio
import sys
from omnidesk_sdk import BasePlugin, CapabilityResult, MediaItem, MediaSource, PluginContext, TextSource, BookSource
import sources

class AlphaMedia(MediaSource):
    source_id = "alpha-media"
    async def fetch_items(self, category="", query="", page=1, *, force_refresh=False):
        return [MediaItem(id="m1", source_id=self.source_id, title=sources.LABEL)]

class AlphaText(TextSource):
    source_id = "alpha-text"
    category = "blog"

class AlphaBook(BookSource):
    source_id = "alpha-book"

class AlphaPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        self.events = []
        self.task_cancelled = False
        self.child_pid = None
        ctx.set_config({"endpoint": "http://127.0.0.1:3425", "gateway_key": "must-strip"})
        ctx.set_secret("gateway_key", "super-vault-secret-123")
        ctx.register_capability("alpha.echo.v1", self.handle_echo)
        ctx.register_streaming_capability("alpha.stream.v1", self.handle_stream)
        ctx.register_streaming_capability("alpha.gen.v1", self.handle_gen)
        ctx.register_media_source(AlphaMedia())
        ctx.register_text_source(AlphaText())
        ctx.register_book_source(AlphaBook())
        ctx.register_workspace_tab({"tab_id": "alpha-tab", "title": "Alpha", "domain": "media"})
        ctx.register_dock_widget({"widget_id": "alpha-dock", "title": "Alpha Dock", "supported_domains": ["media"]})
        ctx.subscribe("entity.*", lambda evt: self.events.append(evt.payload))
        ctx.spawn_task(self._bg())
        ctx.spawn_task(self._subproc(ctx))

    async def _bg(self):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.task_cancelled = True
            raise

    async def _subproc(self, ctx: PluginContext):
        await ctx.exec_subprocess([sys.executable, "-c", "import time; time.sleep(60)"])

    async def handle_echo(self, req: dict) -> CapabilityResult:
        return CapabilityResult.ok({"label": sources.LABEL, "secret": self.ctx.get_secret("gateway_key"), **req})

    async def handle_stream(self, req: dict, stream):
        await stream.emit_delta("part-a")
        await stream.emit_event("stage", {"step": 1})
        return CapabilityResult.ok({"done": True})

    async def handle_gen(self, req: dict):
        yield "g1"
        yield "g2"

    async def on_health_check(self):
        return {"ok": True, "label": sources.LABEL}

def create_plugin():
    return AlphaPlugin()
"""
    beta_code = """
from omnidesk_sdk import BasePlugin, CapabilityResult, MediaSource, PluginContext
import sources

class BetaMedia(MediaSource):
    source_id = "beta-media"

class BetaPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("beta.ping.v1", lambda req: CapabilityResult.ok({"label": sources.LABEL}))
        ctx.register_media_source(BetaMedia())
        ctx.register_workspace_tab({"tab_id": "beta-tab", "title": "Beta", "domain": "text"})

def create_plugin():
    return BetaPlugin()
"""
    _write_plugin(
        plugins_root,
        "plugin-alpha",
        alpha_code,
        manifest_overrides={
            "provides": ["alpha.echo.v1", "alpha.stream.v1", "alpha.gen.v1"],
            "config_schema": [
                {"key": "endpoint", "label": "Endpoint", "type": "string", "default": ""},
                {"key": "gateway_key", "label": "Gateway Key", "type": "secret", "default": ""},
            ],
        },
        extra_files={"sources.py": 'LABEL = "from-alpha"\n'},
    )
    _write_plugin(
        plugins_root,
        "plugin-beta",
        beta_code,
        manifest_overrides={"provides": ["beta.ping.v1"]},
        extra_files={"sources.py": 'LABEL = "from-beta"\n'},
    )

    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    state_events: list[dict[str, Any]] = []
    mgr.bus.subscribe(PLUGIN_STATE_CHANGED_TOPIC, lambda evt: state_events.append(evt.payload), plugin_id="core")

    records = await mgr.load_all()
    assert {r.plugin_id for r in records} == {"plugin-alpha", "plugin-beta"}
    assert mgr.is_loaded("plugin-alpha") and mgr.is_loaded("plugin-beta")
    assert [e["state"] for e in state_events] == ["loaded", "loaded"]

    # Both plugins imported their own sibling sources.py without colliding
    res_a = await mgr.capabilities.call("alpha.echo.v1", {"x": 1})
    res_b = await mgr.capabilities.call("beta.ping.v1", {})
    assert res_a.ok and res_a.data["label"] == "from-alpha"
    assert res_a.data["secret"] == "super-vault-secret-123"
    assert res_a.provider_plugin_id == "plugin-alpha"
    assert res_b.ok and res_b.data["label"] == "from-beta"

    # ConfigStore stripped secret while SecretVault saved it with 0600 permissions
    cfg_file = paths.plugin_config_path("plugin-alpha")
    assert "super-vault-secret-123" not in cfg_file.read_text(encoding="utf-8")
    assert "must-strip" not in cfg_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.vault_path.stat().st_mode) == 0o600

    # Streaming callback and generator capabilities both work through Core CapabilityRegistry
    chunks = [c async for c in mgr.capabilities.stream("alpha.stream.v1", {})]
    assert [c.event for c in chunks] == ["delta", "stage", "result"]
    gen_chunks = [c async for c in mgr.capabilities.stream("alpha.gen.v1", {})]
    assert [c.data for c in gen_chunks] == ["g1", "g2"]

    # DomainRegistry tagged sources with actual plugin_id (NOT "core")
    assert len(mgr.domains.list_sources(plugin_id="plugin-alpha")) == 3
    assert len(mgr.domains.list_sources(plugin_id="plugin-beta")) == 1
    assert [t.tab_id for t in mgr.workspace_tabs()] == ["alpha-tab", "beta-tab"]
    assert [w.widget_id for w in mgr.dock_widgets()] == ["alpha-dock"]

    # Give background subprocess a moment to spawn, then unload plugin-alpha
    await asyncio.sleep(0.1)
    loaded_entry = mgr._loaded["plugin-alpha"]
    procs = list(loaded_entry.ctx.tracked_resources()["subprocesses"])
    assert len(procs) == 1
    child_pid = procs[0].pid

    await mgr.unload_plugin("plugin-alpha")
    assert not mgr.is_loaded("plugin-alpha")
    assert mgr.get("plugin-alpha").state == PluginState.UNLOADED
    assert state_events[-1] == {"plugin_id": "plugin-alpha", "state": "unloaded"}

    # All resources owned by plugin-alpha are atomically revoked
    assert not mgr.capabilities.has("alpha.echo.v1")
    assert not mgr.capabilities.has("alpha.stream.v1")
    assert mgr.domains.list_sources(plugin_id="plugin-alpha") == []
    assert mgr.bus.subscription_count(plugin_id="plugin-alpha") == 0
    assert [t.tab_id for t in mgr.workspace_tabs()] == ["beta-tab"]
    assert mgr.dock_widgets() == []
    assert loaded_entry.instance.task_cancelled is True
    deadline = time.monotonic() + 1.0
    while _is_pid_alive(child_pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not _is_pid_alive(child_pid)

    # plugin-beta stays completely intact
    assert mgr.is_loaded("plugin-beta")
    assert (await mgr.capabilities.call("beta.ping.v1", {})).ok is True
    assert len(mgr.domains.list_sources(plugin_id="plugin-beta")) == 1


@pytest.mark.asyncio
async def test_hot_reload_swaps_code_and_revokes_old_registrations(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data")})
    plugins_root = tmp_path / "plugins"
    pdir = _write_plugin(
        plugins_root,
        "plugin-hot",
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext
import sources

class HotPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        self.unloaded = False
        ctx.register_capability("hot.demo.v1", lambda req: CapabilityResult.ok({"ver": sources.VER}))
        ctx.subscribe("entity.hot", lambda evt: None)

    async def on_unload(self) -> None:
        self.unloaded = True

def create_plugin():
    return HotPlugin()
""",
        manifest_overrides={"provides": ["hot.demo.v1"]},
        extra_files={"sources.py": "VER = 1\n"},
    )

    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    states: list[dict[str, Any]] = []
    mgr.bus.subscribe(PLUGIN_STATE_CHANGED_TOPIC, lambda evt: states.append(evt.payload), plugin_id="core")

    rec1 = await mgr.load_plugin("plugin-hot")
    assert rec1.state == PluginState.LOADED and rec1.reload_count == 0
    assert (await mgr.capabilities.call("hot.demo.v1", {})).data == {"ver": 1}

    # Immediately overwrite sources.py, plugin.py, and manifest (new capability hot.demo.v2 replacing hot.demo.v1)
    (pdir / "sources.py").write_text("VER = 2\n", encoding="utf-8")
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-hot",
                "name": "Test plugin-hot",
                "version": "0.2.0",
                "kind": "provider",
                "domain": "core",
                "provides": ["hot.demo.v2"],
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext
import sources

class HotPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("hot.demo.v2", lambda req: CapabilityResult.ok({"ver": sources.VER}))

def create_plugin():
    return HotPlugin()
""",
        encoding="utf-8",
    )

    rec2 = await mgr.reload_plugin("plugin-hot")
    assert rec2.state == PluginState.LOADED
    assert rec2.reload_count == 1
    assert not mgr.capabilities.has("hot.demo.v1")
    assert mgr.capabilities.has("hot.demo.v2")
    assert mgr.bus.subscription_count(plugin_id="plugin-hot") == 0
    assert (await mgr.capabilities.call("hot.demo.v2", {})).data == {"ver": 2}
    assert states[-1] == {"plugin_id": "plugin-hot", "state": "reloaded"}

    # Failed hot-reload revokes old capability, records FAULTED, and publishes plugin.fault
    faults: list[BusEvent] = []
    mgr.bus.subscribe(FAULT_TOPIC, lambda evt: faults.append(evt), plugin_id="core")
    (pdir / "plugin.py").write_text("def broken(:\n", encoding="utf-8")
    rec3 = await mgr.reload_plugin("plugin-hot", isolate_errors=True)
    assert rec3.state == PluginState.FAULTED
    assert not mgr.capabilities.has("hot.demo.v2")
    assert any(f.payload["plugin_id"] == "plugin-hot" for f in faults)


@pytest.mark.asyncio
async def test_fault_isolation_emits_plugin_fault_with_redacted_secrets(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data")})
    plugins_root = tmp_path / "plugins"
    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    mgr.secret_vault.set("plugin-crash", "api_token", "secret-token-leak-999")

    faults: list[BusEvent] = []
    mgr.bus.subscribe(FAULT_TOPIC, lambda evt: faults.append(evt), plugin_id="core")

    _write_plugin(
        plugins_root,
        "plugin-crash",
        """
from omnidesk_sdk import BasePlugin, PluginContext

class CrashPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("crash.temp.v1", lambda req: {"ok": True})
        ctx.subscribe("entity.x", lambda evt: None)
        raise RuntimeError("boom with secret-token-leak-999 and api_key=rawkey456")

def create_plugin():
    return CrashPlugin()
""",
    )
    _write_plugin(
        plugins_root,
        "plugin-healthy",
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class HealthyPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("healthy.ping.v1", lambda req: CapabilityResult.ok({"ok": True}))

def create_plugin():
    return HealthyPlugin()
""",
    )

    with pytest.raises(PluginFaultError) as exc_info:
        await mgr.load_plugin("plugin-crash", isolate_errors=False)
    assert "secret-token-leak-999" not in str(exc_info.value)
    assert "rawkey456" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    # Partial resources registered before on_load crashed were rolled back
    assert not mgr.capabilities.has("crash.temp.v1")
    assert mgr.bus.subscription_count(plugin_id="plugin-crash") == 0
    assert len(faults) == 1
    assert faults[0].payload["plugin_id"] == "plugin-crash"
    assert "secret-token-leak-999" not in faults[0].payload["error"]

    # load_all isolates faulty plugins and loads healthy ones
    faults.clear()
    all_recs = await mgr.load_all()
    by_id = {r.plugin_id: r for r in all_recs}
    assert by_id["plugin-crash"].state == PluginState.FAULTED
    assert by_id["plugin-healthy"].state == PluginState.LOADED
    assert mgr.is_loaded("plugin-healthy")
    assert len(faults) == 1

    # Unload fault & health_check fault still publish plugin.fault and clean up
    _write_plugin(
        plugins_root,
        "plugin-bad-hooks",
        """
from omnidesk_sdk import BasePlugin, PluginContext

class BadHooks(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("hooks.cap.v1", lambda req: {"ok": True})

    async def on_health_check(self):
        raise ValueError("health check crashed")

    async def on_unload(self) -> None:
        raise RuntimeError("unload crashed")

def create_plugin():
    return BadHooks()
""",
    )
    await mgr.load_plugin("plugin-bad-hooks")
    hc = await mgr.health_check("plugin-bad-hooks")
    assert hc["ok"] is False
    await mgr.unload_plugin("plugin-bad-hooks")
    assert not mgr.capabilities.has("hooks.cap.v1")
    assert any(f.payload["plugin_id"] == "plugin-bad-hooks" and f.payload["component"] == "lifecycle:health_check" for f in faults)
    assert any(f.payload["plugin_id"] == "plugin-bad-hooks" and f.payload["component"] == "lifecycle:unload" for f in faults)

    with pytest.raises(PluginNotFoundError):
        await mgr.unload_plugin("plugin-does-not-exist")


@pytest.mark.asyncio
async def test_verify_plugin_lifecycle_and_isolated_pytest(tmp_path: Path) -> None:
    plugin_dir = scaffold_plugin(
        tmp_path / "plugins" / "plugin-ok",
        plugin_id="plugin-ok",
        kind="provider",
        domain="core",
        provides=["ok.ping.v1"],
    )
    mgr = PluginManager(plugins_dir=tmp_path / "plugins")
    res = await mgr.verify_plugin(plugin_dir)
    assert res.ok is True
    assert res.manifest_ok is True
    assert res.lifecycle_ok is True
    assert res.tests_ok is True
    assert res.tests_skipped is False

    # Failing test inside plugin fails verification
    (plugin_dir / "tests" / "test_plugin.py").write_text("def test_fail():\n    assert False\n", encoding="utf-8")
    bad_res = await mgr.verify_plugin(plugin_dir)
    assert bad_res.ok is False
    assert bad_res.lifecycle_ok is True
    assert bad_res.tests_ok is False
