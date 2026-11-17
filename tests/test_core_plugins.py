"""Tests for schema_version=2 manifest validation, importlib plugin lifecycle, hot-reload, and plugin.fault isolation."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from omnidesk_core import (
    FAULT_TOPIC,
    PLUGIN_STATE_CHANGED_TOPIC,
    BusEvent,
    CapabilityConflictError,
    CapabilityRegistration,
    CapabilityRegistry,
    ErrorCode,
    InvalidArgumentError,
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
from omnidesk_core.plugins import _CapabilityAdapter


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


@pytest.mark.asyncio
async def test_verify_plugin_isolated_staging_preserves_live_sys_modules_and_fails_honestly(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_root = tmp_path / "plugins"
    live_dir = _write_plugin(
        plugins_root,
        "plugin-live-keep",
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext
import helper_mod

SENTINEL = object()

class LiveKeepPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("keep.ping.v1", lambda req: CapabilityResult.ok({"tag": helper_mod.TAG}))

def create_plugin():
    return LiveKeepPlugin()
""",
        manifest_overrides={"provides": ["keep.ping.v1"]},
        extra_files={
            "helper_mod.py": 'TAG = "live-ok"\n',
            "tests/test_plugin.py": "def test_ok():\n    assert True\n",
        },
    )

    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    await mgr.load_plugin(live_dir)
    mod_key = "_omnidesk_plugins.plugin_live_keep.plugin"
    assert mod_key in sys.modules
    live_mod_before = sys.modules[mod_key]
    sentinel_before = live_mod_before.SENTINEL
    helper_before = sys.modules.get("helper_mod")

    # Verifying an already-loaded plugin MUST NOT purge its sys.modules or break the live instance
    res = await mgr.verify_plugin("plugin-live-keep")
    assert res.ok is True
    assert res.lifecycle_ok is True
    assert res.tests_ok is True
    assert sys.modules.get(mod_key) is live_mod_before
    assert sys.modules[mod_key].SENTINEL is sentinel_before
    if helper_before is not None:
        assert sys.modules.get("helper_mod") is helper_before
    assert (await mgr.capabilities.call("keep.ping.v1", {})).data == {"tag": "live-ok"}

    # Verification must fail honestly (never fake PASS) on bad health, crash, or sys.exit(0)
    bad_health_dir = _write_plugin(
        plugins_root,
        "plugin-bad-health",
        """
from omnidesk_sdk import BasePlugin

class BadHealth(BasePlugin):
    async def on_health_check(self):
        return {"ok": False, "reason": "broken"}

def create_plugin():
    return BadHealth()
""",
    )
    bad_health_res = await mgr.verify_plugin(bad_health_dir, run_tests=False)
    assert bad_health_res.ok is False
    assert bad_health_res.lifecycle_ok is False

    early_exit_dir = _write_plugin(
        plugins_root,
        "plugin-early-exit",
        """
import sys
from omnidesk_sdk import BasePlugin

class ExitPlugin(BasePlugin):
    async def on_load(self, ctx):
        sys.exit(0)

def create_plugin():
    return ExitPlugin()
""",
    )
    early_exit_res = await mgr.verify_plugin(early_exit_dir, run_tests=False)
    assert early_exit_res.ok is False
    assert early_exit_res.lifecycle_ok is False

    await mgr.unload_all()


def test_plugin_context_default_data_dir_uses_paths_never_plugin_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnidesk_sdk import PluginContext

    xdg_data = tmp_path / "xdg-data-root"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    plugin_src = tmp_path / "src" / "plugin-example"
    plugin_src.mkdir(parents=True)

    ctx = PluginContext("plugin-example", plugin_dir=plugin_src)
    assert ctx.data_dir == xdg_data / "omnidesk" / "plugins" / "plugin-example"
    assert ctx.data_dir != ctx.plugin_dir

    explicit = tmp_path / "explicit-data"
    ctx_explicit = PluginContext("plugin-example", plugin_dir=plugin_src, data_dir=explicit)
    assert ctx_explicit.data_dir == explicit


@pytest.mark.asyncio
async def test_main_serve_passes_state_path_and_persists_state_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnidesk_core.__main__ import _serve_runtime
    from omnidesk_core.control import ControlClient

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    plugins_root = tmp_path / "plugins"
    _write_plugin(
        plugins_root,
        "plugin-srv-state",
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class StatePlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("state.ping.v1", lambda req: CapabilityResult.ok({"alive": True}))

def create_plugin():
    return StatePlugin()
""",
        manifest_overrides={"provides": ["state.ping.v1"]},
    )

    paths = OmnideskPaths.from_env()
    stop_evt = asyncio.Event()
    serve_task = asyncio.create_task(_serve_runtime([plugins_root], None, stop_event=stop_evt))

    deadline = asyncio.get_running_loop().time() + 5.0
    while not paths.control_socket_path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            stop_evt.set()
            await serve_task
            pytest.fail("control.sock did not come up")
        await asyncio.sleep(0.02)

    client = ControlClient(paths=paths)
    pong = await client.ping()
    assert pong["ok"] is True
    assert pong["data"]["plugins"] == 1

    stop_evt.set()
    rc = await asyncio.wait_for(serve_task, timeout=5.0)
    assert rc == 0

    state_file = paths.state_dir / "plugins.json"
    assert state_file.is_file()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["disabled"] == []
    assert "plugin-srv-state" in saved["plugins"]

    # Disabled plugin recorded in state_path stays skipped on next serve startup and persists on exit
    state_file.write_text(json.dumps({"disabled": ["plugin-srv-state"]}), encoding="utf-8")
    stop_evt2 = asyncio.Event()
    serve_task2 = asyncio.create_task(_serve_runtime([plugins_root], None, stop_event=stop_evt2))
    deadline2 = asyncio.get_running_loop().time() + 5.0
    while not paths.control_socket_path.exists():
        if asyncio.get_running_loop().time() >= deadline2:
            stop_evt2.set()
            await serve_task2
            pytest.fail("control.sock did not come up on second serve")
        await asyncio.sleep(0.02)
    pong2 = await client.ping()
    assert pong2["ok"] is True
    listed2 = await client.list_plugins()
    assert listed2["data"]["plugins"][0]["enabled"] is False
    assert listed2["data"]["plugins"][0]["state"] == "discovered"
    stop_evt2.set()
    assert await asyncio.wait_for(serve_task2, timeout=5.0) == 0
    saved2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved2["disabled"] == ["plugin-srv-state"]


@pytest.mark.asyncio
async def test_plugin_manager_load_all_preserves_priority_metadata_and_streaming_resolution(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_root = tmp_path / "plugins"

    # plugin-alpha-first loads before plugin-zeta-second alphabetically, but has lower priority
    _write_plugin(
        plugins_root,
        "plugin-alpha-first",
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class AlphaFirstPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability(
            "shared.search.v1",
            lambda req: CapabilityResult.ok({"provider": "alpha", **req}),
            priority=50,
            metadata={"tier": "fallback", "weights": [1, 2]},
        )
        ctx.register_streaming_capability(
            "shared.stream.v1",
            self.stream_alpha,
            priority=50,
            metadata={"mode": "slow"},
        )
        ctx.register_capability(
            "shared.explicit.v1",
            lambda req: CapabilityResult.ok({"provider": "alpha-explicit"}),
            priority=175,
        )

    async def stream_alpha(self, req: dict, stream):
        await stream.emit_delta("alpha-chunk")
        return CapabilityResult.ok({"provider": "alpha-stream"})

def create_plugin():
    return AlphaFirstPlugin()
""",
        manifest_overrides={"provides": ["shared.search.v1", "shared.stream.v1", "shared.explicit.v1"]},
    )

    _write_plugin(
        plugins_root,
        "plugin-zeta-second",
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class ZetaSecondPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability(
            "shared.search.v1",
            lambda req: CapabilityResult.ok({"provider": "zeta", **req}),
            priority=200,
            metadata={"tier": "primary", "nested": {"ok": True}},
        )
        ctx.register_streaming_capability(
            "shared.stream.v1",
            self.stream_zeta,
            priority=200,
            metadata={"mode": "fast"},
        )

    async def stream_zeta(self, req: dict, stream):
        await stream.emit_delta("zeta-chunk")
        return CapabilityResult.ok({"provider": "zeta-stream"})

def create_plugin():
    return ZetaSecondPlugin()
""",
        manifest_overrides={"provides": ["shared.search.v1", "shared.stream.v1"]},
    )

    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    records = await mgr.load_all()
    assert {r.plugin_id: r.state for r in records} == {
        "plugin-alpha-first": PluginState.LOADED,
        "plugin-zeta-second": PluginState.LOADED,
    }

    # Priority 200 provider wins resolve & invocation even though it loaded second
    winning_unary = mgr.capabilities.resolve("shared.search.v1")
    assert isinstance(winning_unary, CapabilityRegistration)
    assert winning_unary.plugin_id == "plugin-zeta-second"
    assert winning_unary.priority == 200
    assert winning_unary.metadata == {"tier": "primary", "nested": {"ok": True}}

    providers = mgr.capabilities.providers_for("shared.search.v1")
    assert [(p.plugin_id, p.priority, p.metadata) for p in providers] == [
        ("plugin-zeta-second", 200, {"tier": "primary", "nested": {"ok": True}}),
        ("plugin-alpha-first", 50, {"tier": "fallback", "weights": [1, 2]}),
    ]

    unary_res = await mgr.capabilities.call("shared.search.v1", {"q": "hello"})
    assert unary_res.ok is True
    assert unary_res.provider_plugin_id == "plugin-zeta-second"
    assert unary_res.data == {"provider": "zeta", "q": "hello"}

    # Streaming provider also forwards priority & metadata and resolves to priority 200
    winning_stream = mgr.capabilities.resolve("shared.stream.v1")
    assert winning_stream.plugin_id == "plugin-zeta-second"
    assert winning_stream.streaming is True
    assert winning_stream.priority == 200
    assert winning_stream.metadata == {"mode": "fast"}

    stream_chunks = [c async for c in mgr.capabilities.stream("shared.stream.v1", {})]
    assert [c.event for c in stream_chunks] == ["delta", "result"]
    assert stream_chunks[0].data == "zeta-chunk"
    assert stream_chunks[-1].result is not None
    assert stream_chunks[-1].result.provider_plugin_id == "plugin-zeta-second"
    assert stream_chunks[-1].result.data == {"provider": "zeta-stream"}

    # Explicit non-default priority from load_all() is preserved
    explicit_reg = mgr.capabilities.resolve("shared.explicit.v1")
    assert explicit_reg.priority == 175

    # Unloading priority 200 plugin falls back cleanly to priority 50 provider
    await mgr.unload_plugin("plugin-zeta-second")
    assert mgr.capabilities.resolve("shared.search.v1").plugin_id == "plugin-alpha-first"
    assert mgr.capabilities.resolve("shared.stream.v1").plugin_id == "plugin-alpha-first"
    fallback_res = await mgr.capabilities.call("shared.search.v1", {})
    assert fallback_res.provider_plugin_id == "plugin-alpha-first"
    fallback_chunks = [c async for c in mgr.capabilities.stream("shared.stream.v1", {})]
    assert fallback_chunks[0].data == "alpha-chunk"

    await mgr.unload_all()


@pytest.mark.asyncio
async def test_capability_adapter_forwards_description_metadata_replace_and_fails_closed() -> None:
    reg = CapabilityRegistry()
    adapter = _CapabilityAdapter(reg)

    async def stream_cb(req: dict[str, Any], stream: Any) -> dict[str, Any]:
        await stream.emit_delta("ok")
        return {"ok": True}

    reg_a = adapter.register_capability(
        "demo.meta.v1",
        lambda req: {"v": 1, **req},
        plugin_id="plugin-a",
        priority=150,
        metadata=MappingProxyType({"source": "proxy", "nested": MappingProxyType({"k": 1})}),
        description="Alpha capability",
    )
    assert isinstance(reg_a, CapabilityRegistration)
    assert reg_a.priority == 150
    assert reg_a.description == "Alpha capability"
    assert reg_a.metadata == {"source": "proxy", "nested": {"k": 1}}
    assert reg_a.streaming is False

    reg_s = adapter.register_streaming_capability(
        "demo.stream.v1",
        stream_cb,
        plugin_id="plugin-a",
        priority=220,
        metadata={"channel": "live"},
        description="Live stream",
    )
    assert reg_s.priority == 220
    assert reg_s.description == "Live stream"
    assert reg_s.metadata == {"channel": "live"}
    assert reg_s.streaming is True

    # Adding another provider keeps both, and same-plugin replace=True only replaces itself
    adapter.register_capability(
        "demo.meta.v1",
        lambda req: {"v": 99},
        plugin_id="plugin-b",
        priority=100,
        description="Beta capability",
    )
    adapter.register_capability(
        "demo.meta.v1",
        lambda req: {"v": 2, **req},
        plugin_id="plugin-a",
        priority=300,
        metadata={"source": "updated"},
        description="Updated alpha",
    )
    providers = reg.providers_for("demo.meta.v1")
    assert [(p.plugin_id, p.priority, p.description, p.metadata) for p in providers] == [
        ("plugin-a", 300, "Updated alpha", {"source": "updated"}),
        ("plugin-b", 100, "Beta capability", {}),
    ]
    assert (await reg.call("demo.meta.v1", {"x": 7})).data == {"v": 2, "x": 7}

    # Explicit replace=False conflicts on duplicate registration from the same plugin
    with pytest.raises(CapabilityConflictError):
        adapter.register_capability(
            "demo.meta.v1",
            lambda req: {"v": 3},
            plugin_id="plugin-a",
            replace=False,
        )

    # Unknown kwargs are rejected rather than silently swallowed
    for fn in (adapter.register_capability, adapter.register_streaming_capability, adapter.register):
        with pytest.raises(TypeError):
            fn("demo.badkw.v1", lambda req: {}, plugin_id="plugin-a", unknown_kw=123)  # type: ignore[call-arg]

    # Invalid priority (bool, float, str, None) fails closed without registering default 100
    for bad_priority in (True, False, 1.5, "200", None):
        with pytest.raises(InvalidArgumentError):
            adapter.register_capability(
                "demo.invalid.v1",
                lambda req: {},
                plugin_id="plugin-a",
                priority=bad_priority,  # type: ignore[arg-type]
            )
        with pytest.raises(InvalidArgumentError):
            adapter.register_streaming_capability(
                "demo.invalid.v1",
                stream_cb,
                plugin_id="plugin-a",
                priority=bad_priority,  # type: ignore[arg-type]
            )

    # Invalid metadata (non-Mapping, non-JSON-serializable, NaN/Inf, circular) fails closed
    circular: dict[str, Any] = {}
    circular["self"] = circular
    for bad_metadata in (
        "not-a-mapping",
        [1, 2],
        123,
        {"obj": object()},
        {"set": {1, 2}},
        {"nan": float("nan")},
        {"inf": float("inf")},
        circular,
    ):
        with pytest.raises(InvalidArgumentError):
            adapter.register_capability(
                "demo.invalid.v1",
                lambda req: {},
                plugin_id="plugin-a",
                metadata=bad_metadata,  # type: ignore[arg-type]
            )

    # Invalid description or non-callable handler fails closed
    with pytest.raises(InvalidArgumentError):
        adapter.register_capability("demo.invalid.v1", lambda req: {}, plugin_id="plugin-a", description=None)  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentError):
        adapter.register_capability("demo.invalid.v1", "not-callable", plugin_id="plugin-a")  # type: ignore[arg-type]

    assert not reg.has("demo.invalid.v1")
    assert not reg.has("demo.badkw.v1")


@pytest.mark.asyncio
async def test_plugin_manager_fails_closed_on_invalid_priority_or_metadata(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data")})
    plugins_root = tmp_path / "plugins"

    _write_plugin(
        plugins_root,
        "plugin-bad-priority",
        """
from omnidesk_sdk import BasePlugin, PluginContext

class BadPriorityPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("ok.before.v1", lambda req: {"ok": True})
        ctx.register_capability("bad.prio.v1", lambda req: {"ok": True}, priority=True)

def create_plugin():
    return BadPriorityPlugin()
""",
    )
    _write_plugin(
        plugins_root,
        "plugin-bad-metadata",
        """
from omnidesk_sdk import BasePlugin, PluginContext

class BadMetaPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("bad.meta.v1", lambda req: {"ok": True}, metadata={"bad": object()})

def create_plugin():
    return BadMetaPlugin()
""",
    )

    mgr = PluginManager(plugins_dir=plugins_root, paths=paths)
    with pytest.raises(PluginFaultError) as prio_exc:
        await mgr.load_plugin("plugin-bad-priority", isolate_errors=False)
    assert isinstance(prio_exc.value.cause, InvalidArgumentError)
    assert not mgr.capabilities.has("ok.before.v1")
    assert not mgr.capabilities.has("bad.prio.v1")

    records = await mgr.load_all()
    by_id = {r.plugin_id: r for r in records}
    assert by_id["plugin-bad-priority"].state == PluginState.FAULTED
    assert by_id["plugin-bad-metadata"].state == PluginState.FAULTED
    assert not mgr.capabilities.has("bad.prio.v1")
    assert not mgr.capabilities.has("bad.meta.v1")
