"""Tests for the omnidesk-pm CLI (list, verify, reload, rpc, init) and console script."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from omnidesk_core import ControlServer, OmnideskPaths, PluginManager
from omnidesk_core.cli import main as pm_main


def _run_server_in_thread(
    plugins_dir: Path, paths: OmnideskPaths, sock_path: Path
) -> tuple[threading.Thread, asyncio.AbstractEventLoop, asyncio.Event, PluginManager]:
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def _worker() -> None:
        async def _main() -> None:
            mgr = PluginManager(plugins_dir=plugins_dir, paths=paths)
            await mgr.load_all()
            stop_evt = asyncio.Event()
            holder["loop"] = asyncio.get_running_loop()
            holder["stop"] = stop_evt
            holder["mgr"] = mgr
            async with ControlServer(socket_path=sock_path, paths=paths, plugins=mgr):
                ready.set()
                await stop_evt.wait()
            await mgr.unload_all()

        asyncio.run(_main())

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    assert ready.wait(timeout=5.0)
    return thread, holder["loop"], holder["stop"], holder["mgr"]


def test_cli_init_verify_and_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugins_dir = tmp_path / "plugins"
    target = plugins_dir / "plugin-demo-cli"

    # 1. init
    rc = pm_main(
        [
            "init",
            str(target),
            "--id",
            "plugin-demo-cli",
            "--name",
            "Demo CLI Plugin",
            "--kind",
            "workspace",
            "--domain",
            "text",
            "--provides",
            "text.source.v1",
            "--consumes",
            "llm.chat.v1",
            "--json",
        ]
    )
    assert rc == 0
    init_out = json.loads(capsys.readouterr().out)
    assert init_out["ok"] is True
    assert (target / "plugin.json").is_file()
    assert (target / "plugin.py").is_file()
    assert (target / "tests" / "test_plugin.py").is_file()

    # Re-init without --force fails
    assert pm_main(["init", str(target), "--json"]) == 1
    _ = capsys.readouterr()

    # 2. verify
    rc = pm_main(["verify", str(target), "--json"])
    assert rc == 0
    verify_out = json.loads(capsys.readouterr().out)
    assert verify_out["ok"] is True
    assert verify_out["manifest_ok"] is True
    assert verify_out["lifecycle_ok"] is True
    assert verify_out["tests_ok"] is True

    # Invalid plugin fails verify
    bad_dir = plugins_dir / "plugin-invalid"
    bad_dir.mkdir(parents=True)
    (bad_dir / "plugin.json").write_text('{"schema_version": 1, "id": "plugin-invalid"}', encoding="utf-8")
    assert pm_main(["verify", str(bad_dir), "--json"]) == 1
    bad_verify = json.loads(capsys.readouterr().out)
    assert bad_verify["ok"] is False
    assert bad_verify["manifest_ok"] is False

    # 3. list
    rc = pm_main(["list", "--plugins-dir", str(plugins_dir), "--json"])
    assert rc == 0
    listed = json.loads(capsys.readouterr().out)["plugins"]
    by_id = {p["plugin_id"]: p for p in listed}
    assert by_id["plugin-demo-cli"]["state"] == "discovered"
    assert by_id["plugin-demo-cli"]["provides"] == ["text.source.v1"]
    assert by_id["plugin-invalid"]["state"] == "invalid"


def test_cli_rpc_and_reload_against_live_control_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    plugins_dir = tmp_path / "plugins"
    pdir = plugins_dir / "plugin-live"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "plugin-live",
                "name": "Live Plugin",
                "version": "0.1.0",
                "kind": "provider",
                "domain": "core",
                "provides": ["live.info.v1"],
            }
        ),
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class LivePlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("live.info.v1", lambda req: CapabilityResult.ok({"rev": 1, **req}))

def create_plugin():
    return LivePlugin()
""",
        encoding="utf-8",
    )

    sock_path = tmp_path / "run" / "control.sock"
    thread, loop, stop_evt, _ = _run_server_in_thread(plugins_dir, paths, sock_path)
    try:
        # 1. rpc inline JSON
        rc = pm_main(["rpc", "live.info.v1", '{"tag":"alpha"}', "--socket", str(sock_path)])
        assert rc == 0
        rpc_out = json.loads(capsys.readouterr().out)
        assert rpc_out["ok"] is True
        assert rpc_out["data"] == {"rev": 1, "tag": "alpha"}

        # 2. rpc from --file
        payload_file = tmp_path / "payload.json"
        payload_file.write_text('{"tag":"from-file"}', encoding="utf-8")
        assert pm_main(["rpc", "live.info.v1", "--file", str(payload_file), "--socket", str(sock_path)]) == 0
        file_out = json.loads(capsys.readouterr().out)
        assert file_out["data"] == {"rev": 1, "tag": "from-file"}

        # 3. list --socket
        assert pm_main(["list", "--socket", str(sock_path), "--json"]) == 0
        live_list = json.loads(capsys.readouterr().out)
        assert live_list["plugins"][0]["state"] == "loaded"

        # 4. edit plugin.py -> omnidesk-pm reload -> verify rpc returns rev=2
        (pdir / "plugin.py").write_text(
            """
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

class LivePlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("live.info.v1", lambda req: CapabilityResult.ok({"rev": 2, **req}))

def create_plugin():
    return LivePlugin()
""",
            encoding="utf-8",
        )
        assert pm_main(["reload", "plugin-live", "--socket", str(sock_path), "--json"]) == 0
        reload_out = json.loads(capsys.readouterr().out)
        assert reload_out["ok"] is True
        assert reload_out["data"]["reload_count"] == 1

        assert pm_main(["rpc", "live.info.v1", "{}", "--socket", str(sock_path)]) == 0
        v2_out = json.loads(capsys.readouterr().out)
        assert v2_out["data"] == {"rev": 2}

        # 5. Missing capability returns exit code 1 with structured error JSON
        assert pm_main(["rpc", "missing.cap.v1", "{}", "--socket", str(sock_path)]) == 1
        err_out = json.loads(capsys.readouterr().out)
        assert err_out["ok"] is False
        assert err_out["error_code"] == "capability_not_found"
    finally:
        loop.call_soon_threadsafe(stop_evt.set)
        thread.join(timeout=5.0)

    # Reload against offline socket exits non-zero
    assert pm_main(["reload", "plugin-live", "--socket", str(sock_path), "--json"]) == 1
    offline_out = json.loads(capsys.readouterr().out)
    assert offline_out["ok"] is False


def test_console_script_and_module_entrypoint_help() -> None:
    console_bin = Path(sys.executable).parent / "omnidesk-pm"
    if console_bin.is_file():
        res = subprocess.run([str(console_bin), "--help"], capture_output=True, text=True, check=False)
        assert res.returncode == 0
        assert "omnidesk-pm" in res.stdout

    mod_res = subprocess.run([sys.executable, "-m", "omnidesk_core", "--help"], capture_output=True, text=True, check=False)
    assert mod_res.returncode == 0
    assert "omnidesk-pm" in mod_res.stdout
