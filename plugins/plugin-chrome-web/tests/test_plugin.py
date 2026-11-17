"""Offline tests for plugin-chrome-web manifest, MV3 skeleton, web.embed.v1, and lifecycle."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for path_entry in (str(REPO_ROOT / "core"), str(REPO_ROOT)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from omnidesk_core import OmnideskPaths, PluginManager  # noqa: E402
from omnidesk_sdk import BasePlugin, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_chrome_web_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_chrome(
    tmp_path: Path, args_out: Path, *, stay_alive: bool = True, exit_code: int = 0, name: str = "fake-chrome"
) -> Path:
    script = tmp_path / name
    loop_code = (
        "import signal, time\n"
        "running = True\n"
        "def _stop(signum, frame):\n"
        "    global running\n"
        "    running = False\n"
        "signal.signal(signal.SIGTERM, _stop)\n"
        "while running:\n"
        "    time.sleep(0.05)\n"
        if stay_alive
        else f"sys.exit({exit_code})\n"
    )
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(args_out)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        f"{loop_code}",
        encoding="utf-8",
    )
    script.chmod(stat.S_IRWXU)
    return script


async def _wait_for_file(path: Path, timeout_sec: float = 2.0) -> list[str]:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                return list(json.loads(raw))
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for fake chrome args file: {path}")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_background_requests_reuse_headless_and_explicit_login_switches_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    module = _load_plugin_module()
    plugin = module.create_plugin()
    args_file = tmp_path / "background-args.json"
    binary = _write_fake_chrome(tmp_path, args_file)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data",
                        config_dir=tmp_path / "config", config_schema=manifest["config_schema"])
    ctx.set_config({"chrome_bin": str(binary), "profile_dir": str(tmp_path / "profile"), "cdp_port": 9444})

    class Transport:
        evaluations = 0
        external = False
        ready_checks = 0
        close_calls = 0

        async def close_browser(self, ws_url, **kwargs):
            self.close_calls += 1
            plugin._sessions.latest_session().process.terminate()

        async def get_cookies(self, ws_url, url, **kwargs):
            assert url == 'https://www.reddit.com/'
            return [{'name': 'session', 'value': 'live-test', 'domain': '.reddit.com', 'httpOnly': True}]

        async def list_targets(self, host, port, **kwargs):
            if not plugin._sessions.active_sessions() and not self.external:
                raise module.CdpError("offline")
            return [{"id": "page", "type": "page", "url": "https://www.reddit.com/",
                     "webSocketDebuggerUrl": "ws://127.0.0.1:9444/devtools/page/page"}]

        async def evaluate(self, ws_url, expression, **kwargs):
            if expression.startswith("window.location.origin ==="):
                self.ready_checks += 1
                return {"result": {"result": {"value": self.ready_checks > 1}}}
            self.evaluations += 1
            value = ({"ok": True, "status": 200, "url": "https://www.reddit.com/r/rust.json", "body": "{}", "headers": {}}
                     if kwargs.get("await_promise") else "<html><body><p>Background article</p></body></html>")
            return {"result": {"result": {"value": value}}}

    transport = Transport()
    plugin.set_cdp_transport(transport)
    await plugin.on_load(ctx)
    request = {"url": "https://www.reddit.com/r/rust.json", "headless": True, "open_tab_if_missing": True}
    try:
        # A locked profile must never forward a URL to a pre-existing GUI Chrome.
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("other-host-12345")
        blocked = await plugin.handle_fetch(request)
        assert not blocked.ok and not args_file.exists()
        (profile / "SingletonLock").unlink()
        transport.external = True
        occupied = await plugin.handle_fetch(request)
        assert not occupied.ok and not args_file.exists()
        transport.external = False
        # A stale lock left by an already-exited local Chrome is recoverable.
        (profile / "SingletonLock").symlink_to(f"{socket.gethostname()}-2147483647")

        first, second = await asyncio.gather(plugin.handle_fetch(request), plugin.handle_fetch(request))
        assert first.ok and second.ok
        assert len(plugin._sessions.active_sessions()) == 1
        background = plugin._sessions.latest_session()
        assert background.headless and background.cdp_port == 9444
        recorded = await _wait_for_file(args_file)
        assert "--headless=new" in recorded
        assert not any(arg.startswith("--app=") for arg in recorded)
        assert transport.evaluations == 2

        login = await plugin.handle_embed({"action": "open", "url": "https://www.reddit.com/login/"})
        assert login.ok and not login.data["headless"]
        assert transport.close_calls == 1
        assert not background.is_running
        assert "--app=https://www.reddit.com/login/" in login.data["args"]
        assert "--headless=new" not in login.data["args"]
        cookies = await plugin.handle_cookies({'action': 'get_cookies', 'url': 'https://www.reddit.com/'})
        assert cookies.ok and cookies.data['cookie_header'] == 'session=live-test'
        assert cookies.data['cookie_source'] == 'chrome-live'
        blocked = await plugin.handle_fetch(request)
        assert not blocked.ok and transport.evaluations == 2
        reused = await plugin.handle_fetch({**request, "reuse_visible_session": True})
        assert reused.ok and transport.evaluations == 3
        assert plugin._sessions.latest_session().session_id == login.data['session_id']
        unrelated = await plugin.handle_fetch({**request, "url": "https://unrelated.example/", "reuse_visible_session": True})
        assert not unrelated.ok and transport.evaluations == 3
        await plugin.handle_embed({"action": "close"})
        extracted = await plugin.handle_extract({"url": "https://www.reddit.com/r/rust/", "headless": True})
        assert extracted.ok and "Background article" in extracted.data["text"]
        assert plugin._sessions.latest_session().headless
        assert plugin._sessions.latest_session().profile_dir == background.profile_dir
    finally:
        await plugin.on_unload()
        await ctx.acleanup()
    assert not plugin._sessions.active_sessions()
    assert transport.close_calls == 2


def test_manifest_matches_v2_schema_and_no_hardcoded_topology() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-chrome-web"
    assert manifest["kind"] == "hybrid"
    assert manifest["domain"] == "text"
    assert manifest["entry"] == "plugin:create_plugin"
    assert manifest["provides"] == [
        "web.embed.v1",
        "web.cookies.v1",
        "web.extract.v1",
        "web.fetch.v1",
    ]
    assert manifest["consumes"] == ["llm.translate.v1"]
    assert {"process:chrome", "network:wan", "secret:vault", "config:read", "config:write"}.issubset(
        set(manifest["permissions"])
    )

    allowed_top = set(schema["properties"].keys())
    assert set(manifest.keys()).issubset(allowed_top)
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(cap) for cap in manifest["provides"])
    assert all(cap_re.match(cap) for cap in manifest["consumes"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    assert {"chrome_bin", "profile_dir", "extension_enabled", "cdp_port"}.issubset(fields.keys())
    assert fields["chrome_bin"]["type"] == "string"
    assert fields["extension_enabled"]["type"] == "bool"
    assert fields["cdp_port"]["type"] == "int"
    assert fields["cookie_import_target"]["type"] == "select"
    assert fields["cookie_import_target"]["options"] == ["vault", "file", "both"]

    for src in PLUGIN_DIR.glob("*.py"):
        content = src.read_text(encoding="utf-8")
        assert "/home/zhu" not in content
        assert len(content.splitlines()) <= 400


def test_mv3_extension_skeleton_has_no_remote_code() -> None:
    ext_dir = PLUGIN_DIR / "resources" / "mv3-extension"
    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    sw = ext_dir / manifest["background"]["service_worker"]
    assert sw.is_file()
    js_files = [sw]
    for entry in manifest["content_scripts"]:
        for rel in entry["js"]:
            p = ext_dir / rel
            assert p.is_file()
            js_files.append(p)

    for js_file in js_files:
        text = js_file.read_text(encoding="utf-8")
        assert "eval(" not in text
        assert "new Function" not in text
        assert "importScripts(" not in text
        assert "http://" not in text and "https://" not in text


@pytest.mark.asyncio
async def test_embed_rejects_dangerous_schemes_and_missing_chrome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    module = _load_plugin_module()
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
    ctx.set_config("chrome_bin", str(tmp_path / "nonexistent-chrome"))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    for bad_url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "ftp://example.com/a",
        "chrome://settings",
        "",
        "https://example.com/\ninjected",
    ):
        res = await ctx.capabilities.call("web.embed.v1", {"action": "open", "url": bad_url})
        assert res.ok is False
        assert res.data["error_code"] == "invalid_argument"

    missing_res = await ctx.capabilities.call("web.embed.v1", {"action": "open", "url": "https://example.com/login"})
    assert missing_res.ok is False
    assert missing_res.data["error_code"] == "not_configured"
    assert missing_res.data["launched"] is False

    ctx.set_config("chrome_bin", "")
    empty_res = await ctx.capabilities.call("web.embed.v1", {"action": "open", "url": "https://example.com/login"})
    assert empty_res.ok is False
    assert empty_res.data["error_code"] == "not_configured"

    bad_action = await ctx.capabilities.call("web.embed.v1", {"action": "unknown"})
    assert bad_action.ok is False
    assert bad_action.data["error_code"] == "invalid_argument"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_embed_open_status_close_and_unload_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_data = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    args_file = tmp_path / "chrome-args.json"
    fake_chrome = _write_fake_chrome(tmp_path, args_file, stay_alive=True)

    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "plugin-data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"chrome_bin": str(fake_chrome), "extension_enabled": True, "cdp_port": 9333})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    open_res = await ctx.capabilities.call(
        "web.embed.v1",
        {"action": "open", "url": "https://example.com/reader"},
    )
    assert open_res.ok is True
    expected_profile = xdg_data / "omnidesk" / "chrome-profile"
    expected_ext = PLUGIN_DIR / "resources" / "mv3-extension"
    assert open_res.data["launched"] is True
    assert open_res.data["running"] is True
    assert isinstance(open_res.data["pid"], int) and open_res.data["pid"] > 0
    assert open_res.data["url"] == "https://example.com/reader"
    assert open_res.data["profile_dir"] == str(expected_profile)
    pid1 = open_res.data["pid"]

    recorded_args = await _wait_for_file(args_file)
    assert f"--user-data-dir={expected_profile}" in recorded_args
    assert "--app=https://example.com/reader" in recorded_args
    assert "--class=omnidesk-reader" in recorded_args
    assert f"--load-extension={expected_ext}" in recorded_args
    assert "--remote-debugging-port=9333" in recorded_args
    assert "--remote-debugging-address=127.0.0.1" in recorded_args

    status_res = await ctx.capabilities.call("web.embed.v1", {"action": "status"})
    assert status_res.ok is True
    assert status_res.data["running"] is True
    assert status_res.data["pid"] == pid1

    close_res = await ctx.capabilities.call("web.embed.v1", {"action": "close"})
    assert close_res.ok is True
    assert close_res.data["closed"] is True
    assert pid1 in close_res.data["closed_pids"]
    assert close_res.data["running"] is False
    assert not _pid_exists(pid1)

    # Launch a second session without extension and verify on_unload + acleanup tears it down.
    args_file.unlink()
    open_res2 = await ctx.capabilities.call(
        "web.embed.v1",
        {"action": "open", "url": "https://example.com/noext", "extension_enabled": False},
    )
    assert open_res2.ok is True
    pid2 = open_res2.data["pid"]
    recorded_args2 = await _wait_for_file(args_file)
    assert all(not arg.startswith("--load-extension=") for arg in recorded_args2)
    assert _pid_exists(pid2)

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
    assert not _pid_exists(pid2)
    assert not ctx.capabilities.has("web.embed.v1")

    # Verify ctx.acleanup() alone kills subprocess even if on_unload is skipped, and exit_code!=0 fails.
    ctx2 = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "plugin-data-2",
        config_dir=tmp_path / "cfg-2",
        config_schema=manifest["config_schema"],
    )
    plugin2 = module.create_plugin()
    ctx2.set_config({"chrome_bin": str(fake_chrome), "extension_enabled": False, "cdp_port": 0})
    await invoke_lifecycle_hook(plugin2.on_load, ctx2)
    args_file.unlink(missing_ok=True)
    res3 = await ctx2.capabilities.call("web.embed.v1", {"action": "open", "url": "https://example.com/solo"})
    assert res3.ok is True
    pid3 = res3.data["pid"]
    recorded3 = await _wait_for_file(args_file)
    assert all(not arg.startswith("--remote-debugging-port=") for arg in recorded3)
    await ctx2.acleanup()
    assert not _pid_exists(pid3)

    failing_chrome = _write_fake_chrome(
        tmp_path, tmp_path / "fail-args.json", stay_alive=False, exit_code=2, name="failing-chrome"
    )
    ctx3 = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "plugin-data-3",
        config_dir=tmp_path / "cfg-3",
        config_schema=manifest["config_schema"],
    )
    plugin3 = module.create_plugin()
    ctx3.set_config({"chrome_bin": str(failing_chrome)})
    await invoke_lifecycle_hook(plugin3.on_load, ctx3)
    fail_res = await ctx3.capabilities.call("web.embed.v1", {"action": "open", "url": "https://example.com/fail"})
    assert fail_res.ok is False
    assert fail_res.data["error_code"] == "service_offline"
    await ctx3.acleanup()


@pytest.mark.asyncio
async def test_path_safety_and_plugin_manager_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-run"))

    paths = OmnideskPaths.from_env()
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    rec = await mgr.load_plugin(PLUGIN_DIR)
    assert rec.plugin_id == "plugin-chrome-web"
    assert mgr.capabilities.has("web.embed.v1")
    assert mgr.capabilities.has("web.cookies.v1")
    assert mgr.capabilities.has("web.extract.v1")
    assert mgr.capabilities.has("web.fetch.v1")

    health = await mgr.health_check("plugin-chrome-web")
    assert health["ok"] is True
    assert health["extension_ready"] is True

    # Reject path traversal and symlink profile_dir
    mgr.config_store.update("plugin-chrome-web", {"profile_dir": "/tmp/../escape"})
    trav_res = await mgr.capabilities.call("web.embed.v1", {"action": "status"})
    assert trav_res.ok is False
    assert trav_res.data["error_code"] == "path_safety"

    symlink_profile = tmp_path / "symlink-profile"
    real_dir = tmp_path / "real-profile"
    real_dir.mkdir()
    symlink_profile.symlink_to(real_dir)
    mgr.config_store.update("plugin-chrome-web", {"profile_dir": str(symlink_profile)})
    sym_res = await mgr.capabilities.call("web.embed.v1", {"action": "status"})
    assert sym_res.ok is False
    assert sym_res.data["error_code"] == "path_safety"

    await mgr.unload_plugin("plugin-chrome-web")
    assert not mgr.capabilities.has("web.embed.v1")
