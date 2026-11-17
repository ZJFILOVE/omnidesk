"""Tests for the omnidesk-pm CLI (list, verify, reload, rpc, init) and console script."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from omnidesk_core import ControlServer, ErrorCode, OmnideskPaths, PluginManager
from omnidesk_core.cli import main as pm_main
from omnidesk_core.vault import SecretVault


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


def _isolate_xdg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OmnideskPaths:
    env_map = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "OMNIDESK_STAGING_DIR": str(tmp_path / "staging"),
    }
    for k, v in env_map.items():
        monkeypatch.setenv(k, v)
    return OmnideskPaths.from_env(env_map)


def _write_cli_plugin(
    root: Path,
    *,
    plugin_id: str,
    version: str = "0.1.0",
    schema_version: Any = 2,
    cap_id: str = "cli.sample.v1",
    test_ok: bool = True,
    config_schema: list[dict[str, Any]] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    mf: dict[str, Any] = {
        "schema_version": schema_version,
        "id": plugin_id,
        "name": f"CLI {plugin_id}",
        "version": version,
        "runtime": "python",
        "entry": "plugin:create_plugin",
        "kind": "provider",
        "domain": "core",
        "provides": [cap_id],
        "verify_cmd": "uv run pytest -q",
        "config_schema": config_schema or [],
    }
    (root / "plugin.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")
    (root / "plugin.py").write_text(
        "from __future__ import annotations\n"
        "from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext\n\n"
        "class CliPlugin(BasePlugin):\n"
        "    async def on_load(self, ctx: PluginContext) -> None:\n"
        f'        ctx.register_capability("{cap_id}", lambda req: CapabilityResult.ok({{"ver": "{version}", "req": req}}))\n\n'
        "def create_plugin() -> BasePlugin:\n"
        "    return CliPlugin()\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_plugin.py").write_text(
        "def test_ok():\n    assert " + ("True" if test_ok else "False") + "\n",
        encoding="utf-8",
    )
    return root


def _git_cmd(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return res.stdout.strip()


def test_cli_install_update_rollback_and_error_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _isolate_xdg_env(tmp_path, monkeypatch)
    repo = tmp_path / "git-src"
    _write_cli_plugin(repo, plugin_id="plugin-cli-git", version="0.1.0")
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git_cmd(repo, "config", "user.email", "cli@omnidesk.local")
    _git_cmd(repo, "config", "user.name", "CLI Tester")
    _git_cmd(repo, "add", ".")
    _git_cmd(repo, "commit", "-m", "v0.1.0")
    _git_cmd(repo, "tag", "v0.1.0")
    sha1 = _git_cmd(repo, "rev-parse", "HEAD")

    # 1. install from git URL + --ref + --id
    rc = pm_main(["install", f"file://{repo}", "--id", "plugin-cli-git", "--ref", "v0.1.0", "--json"])
    assert rc == 0
    inst_out = json.loads(capsys.readouterr().out)
    assert inst_out["ok"] is True
    assert inst_out["exit_code"] == 0
    assert inst_out["action"] == "install"
    assert inst_out["commit"] == sha1
    assert list(paths.staging_dir.iterdir()) == []
    assert (paths.plugins_dir / "plugin-cli-git" / "plugin.json").is_file()
    lock_json = json.loads(paths.upstreams_lock_path.read_text(encoding="utf-8"))
    assert lock_json["locks"]["plugin-cli-git"]["commit"] == sha1
    assert lock_json["locks"]["plugin-cli-git"]["ref"] == "v0.1.0"

    # 2. duplicate id rejected with conflict + exit code 1
    dup_src = _write_cli_plugin(tmp_path / "dup-src", plugin_id="plugin-cli-git", version="0.1.0")
    assert pm_main(["install", str(dup_src), "--json"]) == 1
    dup_out = json.loads(capsys.readouterr().out)
    assert dup_out["ok"] is False
    assert dup_out["error_code"] == ErrorCode.CONFLICT
    assert dup_out["exit_code"] == 1

    # 3. verification / schema failure preserves diagnostic and cleans staging
    bad_src = _write_cli_plugin(tmp_path / "bad-src", plugin_id="plugin-bad", schema_version=1)
    assert pm_main(["install", str(bad_src), "--json"]) == 1
    bad_out = json.loads(capsys.readouterr().out)
    assert bad_out["ok"] is False
    assert bad_out["error_code"] == ErrorCode.VALIDATION_ERROR
    assert bad_out["exit_code"] == 1
    assert Path(bad_out["details"]["diagnostic_path"]).is_file()
    assert list(paths.staging_dir.iterdir()) == []

    # 4. path traversal & symlink rejected with path_safety
    assert pm_main(["install", str(dup_src), "--id", "../evil", "--json"]) == 1
    trav_out = json.loads(capsys.readouterr().out)
    assert trav_out["ok"] is False
    assert trav_out["error_code"] == ErrorCode.PATH_SAFETY
    assert trav_out["exit_code"] == 1

    sym_src = tmp_path / "sym-src"
    sym_src.symlink_to(dup_src)
    assert pm_main(["install", str(sym_src), "--json"]) == 1
    sym_out = json.loads(capsys.readouterr().out)
    assert sym_out["ok"] is False
    assert sym_out["error_code"] == ErrorCode.PATH_SAFETY
    assert sym_out["exit_code"] == 1

    # 5. CLI syntax error with --json exits 2 consistently
    assert pm_main(["install", "--json"]) == 2
    syntax_out = json.loads(capsys.readouterr().out)
    assert syntax_out["ok"] is False
    assert syntax_out["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert syntax_out["exit_code"] == 2

    # 6. update across commits + list snapshots + rollback (default & explicit)
    for ver in ("0.2.0", "0.3.0"):
        _write_cli_plugin(repo, plugin_id="plugin-cli-git", version=ver)
        _git_cmd(repo, "add", ".")
        _git_cmd(repo, "commit", "-m", f"v{ver}")
        assert pm_main(["update", "plugin-cli-git", "--ref", "main", "--json"]) == 0
        upd_out = json.loads(capsys.readouterr().out)
        assert upd_out["ok"] is True
        assert upd_out["version"] == ver

    assert pm_main(["rollback", "plugin-cli-git", "--list", "--json"]) == 0
    snaps_out = json.loads(capsys.readouterr().out)
    assert [s["version"] for s in snaps_out["snapshots"]] == ["0.1.0", "0.2.0"]
    snap_v1 = snaps_out["snapshots"][0]["snapshot_id"]

    # Default rollback restores latest snapshot (0.2.0)
    assert pm_main(["rollback", "plugin-cli-git", "--json"]) == 0
    rb1 = json.loads(capsys.readouterr().out)
    assert rb1["ok"] is True
    assert rb1["version"] == "0.2.0"

    # Explicit --snapshot restores 0.1.0
    assert pm_main(["rollback", "plugin-cli-git", "--snapshot", snap_v1, "--json"]) == 0
    rb2 = json.loads(capsys.readouterr().out)
    assert rb2["ok"] is True
    assert rb2["version"] == "0.1.0"

    # Path traversal in --snapshot fails closed
    assert pm_main(["rollback", "plugin-cli-git", "--snapshot", "../../evil", "--json"]) == 1
    rb_bad = json.loads(capsys.readouterr().out)
    assert rb_bad["error_code"] == ErrorCode.PATH_SAFETY


@pytest.mark.asyncio
async def test_cli_enable_disable_persists_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _isolate_xdg_env(tmp_path, monkeypatch)
    src = _write_cli_plugin(tmp_path / "src-toggle", plugin_id="plugin-toggle", cap_id="toggle.ping.v1")
    assert pm_main(["install", str(src), "--json"]) == 0
    _ = capsys.readouterr()

    # Disable via CLI
    assert pm_main(["disable", "plugin-toggle", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    dis_out = json.loads(capsys.readouterr().out)
    assert dis_out["ok"] is True
    assert dis_out["enabled"] is False
    assert stat.S_IMODE(paths.plugins_state_path.stat().st_mode) == 0o600

    # list reflects disabled status
    assert pm_main(["list", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["plugins"]
    assert listed[0]["enabled"] is False

    # Restart simulation: fresh PluginManager respects disabled state
    mgr1 = PluginManager(plugins_dir=paths.plugins_dir, paths=paths, state_path=paths.plugins_state_path)
    recs1 = await mgr1.load_all()
    assert recs1[0].enabled is False
    assert recs1[0].state == "discovered"
    assert not mgr1.capabilities.has("toggle.ping.v1")

    # Enable via CLI -> fresh PluginManager loads it on restart
    assert pm_main(["enable", "plugin-toggle", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    en_out = json.loads(capsys.readouterr().out)
    assert en_out["ok"] is True
    assert en_out["enabled"] is True

    mgr2 = PluginManager(plugins_dir=paths.plugins_dir, paths=paths, state_path=paths.plugins_state_path)
    recs2 = await mgr2.load_all()
    assert recs2[0].enabled is True
    assert recs2[0].state == "loaded"
    assert mgr2.capabilities.has("toggle.ping.v1")
    await mgr2.unload_all()

    # Unknown plugin fails cleanly with not_found
    assert pm_main(["enable", "plugin-missing", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 1
    miss_out = json.loads(capsys.readouterr().out)
    assert miss_out["error_code"] == ErrorCode.NOT_FOUND
    assert miss_out["exit_code"] == 1


def test_cli_config_list_get_set_and_secret_no_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _isolate_xdg_env(tmp_path, monkeypatch)
    src = _write_cli_plugin(
        tmp_path / "src-cfg",
        plugin_id="plugin-cfg-cli",
        config_schema=[
            {"key": "endpoint", "label": "Endpoint", "type": "string", "default": "http://127.0.0.1:3425"},
            {"key": "retries", "label": "Retries", "type": "int", "default": 2},
            {"key": "strict_mode", "label": "Strict", "type": "bool", "default": False},
            {"key": "api_token", "label": "API Token", "type": "secret", "default": ""},
        ],
    )
    assert pm_main(["install", str(src), "--json"]) == 0
    _ = capsys.readouterr()

    # 1. Set plain keys (subcommand-first and plugin-first syntax)
    assert pm_main(["config", "set", "plugin-cfg-cli", "retries", "7", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    set_int = json.loads(capsys.readouterr().out)
    assert set_int["ok"] is True
    assert set_int["config"]["retries"] == 7

    assert pm_main(["config", "plugin-cfg-cli", "set", "strict_mode", "true", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    set_bool = json.loads(capsys.readouterr().out)
    assert set_bool["config"]["strict_mode"] is True

    # 2. Set secret -> NEVER echoed in stdout JSON/text or plain config JSON
    secret_val = "sk-cli-ultra-secret-424242"
    assert pm_main(["config", "set", "plugin-cfg-cli", "api_token", secret_val, "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    raw_set_out = capsys.readouterr().out
    assert secret_val not in raw_set_out
    set_sec = json.loads(raw_set_out)
    assert set_sec["ok"] is True
    assert set_sec["is_secret"] is True
    assert set_sec["secrets"]["api_token"] == {"set": True}
    assert SecretVault(paths=paths).get("plugin-cfg-cli", "api_token") == secret_val
    assert stat.S_IMODE(paths.vault_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.plugin_config_path("plugin-cfg-cli").stat().st_mode) == 0o600
    assert secret_val not in paths.plugin_config_path("plugin-cfg-cli").read_text(encoding="utf-8")

    # Also check non-JSON text mode never echoes secret
    assert pm_main(["config", "plugin-cfg-cli", "set-secret", "custom_secret", "another-secret-999", "--plugins-dir", str(paths.plugins_dir)]) == 0
    text_out = capsys.readouterr()
    assert "another-secret-999" not in text_out.out
    assert "another-secret-999" not in text_out.err

    # 3. Reading secret plaintext via config get is strictly forbidden
    assert pm_main(["config", "get", "plugin-cfg-cli", "api_token", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 1
    denied_raw = capsys.readouterr().out
    assert secret_val not in denied_raw
    denied = json.loads(denied_raw)
    assert denied["ok"] is False
    assert denied["error_code"] == ErrorCode.PERMISSION_DENIED
    assert denied["exit_code"] == 1

    # 4. Status query returns only boolean status without plaintext
    assert pm_main(["config", "get", "plugin-cfg-cli", "api_token", "--status", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    st_raw = capsys.readouterr().out
    assert secret_val not in st_raw
    assert json.loads(st_raw)["set"] is True

    assert pm_main(["config", "status", "plugin-cfg-cli", "api_token", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["set"] is True

    # 5. list and full get show plain values + boolean secret statuses, never secret plaintext
    assert pm_main(["config", "list", "plugin-cfg-cli", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    list_raw = capsys.readouterr().out
    assert secret_val not in list_raw
    assert "another-secret-999" not in list_raw
    list_json = json.loads(list_raw)
    assert list_json["plugins"][0]["config"]["retries"] == 7

    assert pm_main(["config", "get", "plugin-cfg-cli", "retries", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["value"] == 7

    # 6. Unset secret + path traversal check
    assert pm_main(["config", "set", "plugin-cfg-cli", "api_token", "--unset", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0
    unset_json = json.loads(capsys.readouterr().out)
    assert unset_json["secrets"]["api_token"] == {"set": False}

    assert pm_main(["config", "get", "../evil", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == ErrorCode.PATH_SAFETY


def test_cli_mcp_serve_stdio_list_call_and_no_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _isolate_xdg_env(tmp_path, monkeypatch)
    src_on = _write_cli_plugin(tmp_path / "src-mcp-on", plugin_id="plugin-mcp-on", cap_id="mcp.echo.v1")
    src_off = _write_cli_plugin(tmp_path / "src-mcp-off", plugin_id="plugin-mcp-off", cap_id="mcp.hidden.v1")
    assert pm_main(["install", str(src_on), "--json"]) == 0
    assert pm_main(["install", str(src_off), "--json"]) == 0
    assert pm_main(["disable", "plugin-mcp-off", "--plugins-dir", str(paths.plugins_dir), "--json"]) == 0

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "omnidesk_list_plugins", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "omnidesk_list_capabilities", "arguments": {}}},
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "omnidesk_call_capability", "arguments": {"capability_id": "mcp.echo.v1", "payload": {"msg": "hi"}}},
        },
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "id"}},
        },
        {"jsonrpc": "2.0", "id": 8, "method": "unknown/method"},
    ]
    stdin_text = "\n".join(json.dumps(r) for r in requests) + "\n{broken-json\n"

    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(paths.config_dir.parent),
        "XDG_DATA_HOME": str(paths.data_dir.parent),
        "XDG_STATE_HOME": str(paths.state_dir.parent),
        "XDG_CACHE_HOME": str(paths.cache_dir.parent),
        "XDG_RUNTIME_DIR": str(paths.runtime_dir.parent),
        "OMNIDESK_STAGING_DIR": str(paths.staging_dir),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "omnidesk_core", "mcp-serve", "--plugins-dir", str(paths.plugins_dir)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=15.0,
        check=False,
    )
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    by_id = {msg.get("id"): msg for msg in lines}

    assert by_id[1]["result"]["serverInfo"]["name"] == "omnidesk-mcp"
    assert by_id[2]["result"] == {}

    tool_names = {t["name"] for t in by_id[3]["result"]["tools"]}
    assert {"omnidesk_list_plugins", "omnidesk_list_capabilities", "omnidesk_call_capability", "omnidesk_emit_event"} <= tool_names
    assert not any(bad in tool_names for bad in ("shell", "bash", "exec", "run_command"))

    plugs_payload = json.loads(by_id[4]["result"]["content"][0]["text"])
    assert {p["plugin_id"]: p["enabled"] for p in plugs_payload["plugins"]} == {
        "plugin-mcp-on": True,
        "plugin-mcp-off": False,
    }

    caps_payload = json.loads(by_id[5]["result"]["content"][0]["text"])
    cap_ids = {c["capability_id"] for c in caps_payload["capabilities"]}
    assert "mcp.echo.v1" in cap_ids
    assert "mcp.hidden.v1" not in cap_ids

    call_payload = json.loads(by_id[6]["result"]["content"][0]["text"])
    assert by_id[6]["result"]["isError"] is False
    assert call_payload["ok"] is True
    assert call_payload["data"] == {"ver": "0.1.0", "req": {"msg": "hi"}}

    # Shell execution tool does not exist and is rejected cleanly
    assert by_id[7]["error"]["code"] == -32602
    assert by_id[8]["error"]["code"] == -32601
    parse_err = next(msg for msg in lines if msg.get("id") is None)
    assert parse_err["error"]["code"] == -32700


def test_cli_mcp_side_effect_requires_confirmation_and_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _isolate_xdg_env(tmp_path, monkeypatch)
    src = _write_cli_plugin(tmp_path / "src-mcp-book", plugin_id="plugin-mcp-book", cap_id="book.search.v1")
    assert pm_main(["install", str(src), "--json"]) == 0

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "omnidesk_download_book",
                "arguments": {"item_id": "book-1", "format": "pdf"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "omnidesk_download_book",
                "arguments": {"item_id": "book-1", "format": "pdf", "confirm": True, "reason": "test download"},
            },
        },
    ]
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(paths.config_dir.parent),
        "XDG_DATA_HOME": str(paths.data_dir.parent),
        "XDG_STATE_HOME": str(paths.state_dir.parent),
        "XDG_CACHE_HOME": str(paths.cache_dir.parent),
        "XDG_RUNTIME_DIR": str(paths.runtime_dir.parent),
        "OMNIDESK_STAGING_DIR": str(paths.staging_dir),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "omnidesk_core", "mcp-serve", "--plugins-dir", str(paths.plugins_dir)],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=15.0,
        check=False,
    )
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    by_id = {msg.get("id"): msg for msg in lines}

    denied = json.loads(by_id[2]["result"]["content"][0]["text"])
    assert by_id[2]["result"]["isError"] is True
    assert denied["error_code"] == "approval_required"

    approved = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert by_id[3]["result"]["isError"] is False
    assert approved["ok"] is True

    audit_path = paths.data_dir / "audit" / "agent-actions.jsonl"
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
    assert records[0]["approved"] is False
    assert records[0]["result"] == "denied:approval_required"
    assert any(record["approved"] is True and record["result"] == "ok" for record in records)
