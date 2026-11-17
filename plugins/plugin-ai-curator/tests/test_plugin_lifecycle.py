"""Lifecycle, manifest v2, non-blocking registration, and invalid-argument tests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from omnidesk_core.manifest import load_manifest
from omnidesk_core.paths import OmnideskPaths
from omnidesk_core.plugins import PluginManager
from omnidesk_sdk import PluginContext

from conftest import PLUGIN_DIR, REPO_ROOT


def _isolated_xdg(monkeypatch, tmp_path: Path) -> dict[str, str]:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def test_manifest_v2_and_schema_compliance() -> None:
    manifest_path = PLUGIN_DIR / "plugin.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = load_manifest(manifest_path)

    assert manifest.schema_version == 2
    assert manifest.id == "plugin-ai-curator"
    assert manifest.kind == "hybrid"
    assert manifest.domain == "core"
    assert manifest.provides == ["recommend.feed.v1"]
    assert "vocab.sm2.v1" in manifest.consumes
    assert manifest.entry == "plugin:create_plugin"

    assert set(manifest.permissions) == {
        "config:read",
        "config:write",
        "fs:user_mount",
        "process:git",
        "network:wan",
    }

    keys = {entry.key for entry in manifest.config_schema}
    assert {"obsidian_vault_dir", "music_daily_dir", "curation_root", "sync_remote"} <= keys
    for entry in manifest.config_schema:
        lowered = entry.key.lower()
        assert not any(tok in lowered for tok in ("secret", "token", "password", "api_key"))

    schema_path = REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json"
    if schema_path.is_file():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        for req_key in schema.get("required", []):
            assert req_key in raw
        try:
            import jsonschema  # type: ignore[import-untyped]

            jsonschema.validate(instance=raw, schema=schema)
        except ImportError:
            pass


def test_on_load_is_nonblocking_and_registers_domains(monkeypatch, tmp_path: Path) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    from plugin import create_plugin

    plugin = create_plugin()
    ctx = PluginContext(
        "plugin-ai-curator",
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "xdg-data" / "omnidesk" / "plugin-data" / "plugin-ai-curator",
        config_dir=tmp_path / "xdg-config" / "omnidesk" / "plugins" / "plugin-ai-curator",
    )

    # Guard against blocking subprocess/git during on_load
    async def fail_subprocess(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("on_load must never spawn blocking subprocesses")

    monkeypatch.setattr(ctx, "exec_subprocess", fail_subprocess)

    async def scenario() -> None:
        t0 = time.monotonic()
        await plugin.on_load(ctx)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        assert elapsed_ms < 250.0

        assert ctx.capabilities.has("recommend.feed.v1")

        media_srcs = [s for s in ctx.domains.media_sources() if getattr(s, "source_id", "") == "plugin-ai-curator"]
        text_srcs = [s for s in ctx.domains.text_sources() if getattr(s, "source_id", "") == "plugin-ai-curator"]
        book_srcs = [s for s in ctx.domains.book_sources() if getattr(s, "source_id", "") == "plugin-ai-curator"]
        assert len(media_srcs) == 1
        assert len(text_srcs) == 1
        assert len(book_srcs) == 1
        media_src, text_src = media_srcs[0], text_srcs[0]
        assert text_src.category == "multi"
        assert "每日 5 首 · 最新" in media_src.categories()
        assert "YouTube 每日精选" in media_src.categories()

        health = await plugin.on_health_check()
        assert health["ok"] is True
        assert health["plugin_id"] == "plugin-ai-curator"
        assert "recommend.feed.v1" in health["capabilities"]

        await plugin.on_unload()
        after = await plugin.handle_recommend_feed({"action": "status"})
        assert after.ok is False
        assert after.data.get("error_code") == "service_offline"

    asyncio.run(scenario())


def test_plugin_manager_load_reload_unload_revocation(monkeypatch, tmp_path: Path) -> None:
    env = _isolated_xdg(monkeypatch, tmp_path)
    paths = OmnideskPaths.from_env(env=env)
    faults: list[dict] = []

    async def scenario() -> None:
        mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths, isolate_errors=False)
        mgr.bus.subscribe("plugin.fault", lambda ev: faults.append(ev.payload))

        rec = await mgr.load_plugin(PLUGIN_DIR)
        assert rec.manifest.id == "plugin-ai-curator"
        assert mgr.capabilities.has("recommend.feed.v1")
        assert mgr.domains.get("media", "plugin-ai-curator") is not None
        assert mgr.domains.get("text", "plugin-ai-curator") is not None
        assert mgr.domains.get("book", "plugin-ai-curator") is not None

        hc = await mgr.health_check("plugin-ai-curator")
        assert hc["ok"] is True

        st_res = await mgr.capabilities.call("recommend.feed.v1", {"action": "status"})
        assert st_res.ok is True
        bad_res = await mgr.capabilities.call("recommend.feed.v1", {"action": "unknown_rpc_action"})
        assert bad_res.ok is False
        assert bad_res.data.get("error_code") == "invalid_argument"

        await mgr.reload_plugin("plugin-ai-curator")
        assert mgr.capabilities.has("recommend.feed.v1")

        await mgr.unload_plugin("plugin-ai-curator")
        assert not mgr.capabilities.has("recommend.feed.v1")
        assert mgr.domains.get("media", "plugin-ai-curator") is None
        assert mgr.domains.get("text", "plugin-ai-curator") is None
        assert mgr.domains.get("book", "plugin-ai-curator") is None
        assert faults == []

    asyncio.run(scenario())


def test_unknown_and_malformed_actions_return_invalid_argument(monkeypatch, tmp_path: Path) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    from plugin import create_plugin

    plugin = create_plugin()
    ctx = PluginContext(
        "plugin-ai-curator",
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "xdg-data" / "omnidesk" / "plugin-data" / "plugin-ai-curator",
        config_dir=tmp_path / "xdg-config" / "omnidesk" / "plugins" / "plugin-ai-curator",
    )

    async def scenario() -> None:
        await plugin.on_load(ctx)
        for bad_req in ("not-a-dict", {}, {"action": ""}, {"action": 123}, {"action": "unknown_verb"}):
            res = await plugin.handle_recommend_feed(bad_req)
            assert res.ok is False, f"expected failure for {bad_req!r}"
            assert res.data.get("error_code") == "invalid_argument"
        await plugin.on_unload()

    asyncio.run(scenario())
