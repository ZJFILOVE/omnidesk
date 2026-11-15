#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def tree_digest(root):
    digest = hashlib.sha256()
    for directory, folders, filenames in os.walk(root):
        folders[:] = sorted(folder for folder in folders if folder != ".git")
        for filename in sorted(filenames):
            path = Path(directory) / filename
            digest.update(str(path.relative_to(root)).encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            else:
                digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-build", type=Path, default=Path("/tmp/omnidesk-build"))
    parser.add_argument("--curator-build", type=Path, default=Path("/tmp/build-plugin-ai-curator"))
    parser.add_argument("--music-build", type=Path, default=Path("/tmp/build-plugin-music"))
    parser.add_argument("--magpie-build", type=Path, default=Path("/tmp/build-plugin-magpie-agent"))
    parser.add_argument("--obsidian-vault", type=Path, required=True)
    parser.add_argument("--music-daily", type=Path, required=True)
    options = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    runtime = Path(tempfile.mkdtemp(prefix="omnidesk-curation-integration-"))
    data_root = runtime / "data"
    config_root = runtime / "config"
    pm_binary = options.host_build / "omnidesk-pm"
    environment = dict(os.environ)
    environment.pop("LD_PRELOAD", None)
    environment.update({
        "OMNIDESK_DATA_HOME": str(data_root),
        "OMNIDESK_CONFIG_HOME": str(config_root),
        "OMNIDESK_CONTROL_SOCK": str(runtime / "control.sock"),
        "OMNIDESK_SDK_CMAKE_DIR": str(options.host_build / "sdk-cmake"),
        "OMNIDESK_CATALOG_PATH": str(source / "host/resources/marketplace/catalog.json"),
        "OMNIDESK_IMAGE_CACHE_DIR": str(runtime / "images"),
        "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
        "XDG_DATA_HOME": str(runtime / "xdg-data"),
        "XDG_STATE_HOME": str(runtime / "xdg-state"),
        "XDG_CACHE_HOME": str(runtime / "xdg-cache"),
    })
    environment["LD_LIBRARY_PATH"] = str(options.host_build)
    reports = []

    def run_pm(arguments, expected_code=0):
        result = subprocess.run(
            [str(pm_binary), *arguments], env=environment,
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != expected_code:
            raise AssertionError(
                f"{arguments}: exit {result.returncode}\n{result.stdout}\n{result.stderr}"
            )
        return json.loads(result.stdout)

    def rpc(capability, payload, expected_ok=True):
        result = run_pm(
            ["rpc", capability, json.dumps(payload, ensure_ascii=False)],
            0 if expected_ok else 1,
        )
        assert result["ok"] is expected_ok, result
        return result.get("data", {})

    before = {
        str(root): tree_digest(root)
        for root in (options.obsidian_vault, options.music_daily)
    }
    plugins = {}
    for plugin_id, build_dir, library in (
        ("plugin-ai-curator", options.curator_build, "libplugin_ai_curator.so"),
        ("plugin-music", options.music_build, "libplugin_music.so"),
        ("plugin-magpie-agent", options.magpie_build, "libplugin_magpie_agent.so"),
    ):
        plugin_source = source / "plugins" / ("omnidesk-" + plugin_id)
        manifest = json.loads((plugin_source / "plugin.json").read_text())
        installed = data_root / "plugins" / plugin_id
        target_library = installed / "lib" / library
        target_library.parent.mkdir(parents=True)
        shutil.copy2(build_dir / library, target_library)
        write_json(installed / "plugin.json", manifest)
        abi = run_pm(["abi-check", str(target_library), "--expect-id", plugin_id, "--json"])
        assert abi.get("compatible", True), abi
        record = {
            "manifest": manifest,
            "install_dir": str(installed),
            "so_path": str(target_library),
            "so_sha256": hashlib.sha256(target_library.read_bytes()).hexdigest(),
            "source_url": str(plugin_source),
            "enabled": True,
            "dev_local": True,
            "install_source": "local-test-fixture",
        }
        write_json(installed / "record.json", record)
        plugins[plugin_id] = record
    write_json(data_root / "state.json", {"plugins": plugins})
    write_json(config_root / "plugins/plugin-ai-curator.json", {
        "obsidian_vault_dir": str(options.obsidian_vault),
        "music_daily_dir": str(options.music_daily),
        "auto_rehydrate_on_load": False,
        "auto_pull_remote": False,
    })
    write_json(config_root / "plugins/plugin-music.json", {
        "auto_launch_engine": False,
        "auto_translate_lyrics": False,
        "enable_lrclib": False,
        "engine_data_dir": str(runtime / "adam"),
    })
    write_json(config_root / "plugins/plugin-magpie-agent.json", {
        "curation_skill_dir": str(source / ".agents/skills/omnidesk-curation-writeback"),
        "gateway_url": "http://127.0.0.1:1",
    })

    hydrated = rpc("recommend.feed.v1", {
        "action": "bootstrap_and_rehydrate",
        "pull_remote": False,
        "clone_if_missing": False,
        "sync_vocab": False,
    })
    assert hydrated["dates_indexed"] > 0, hydrated
    assert hydrated["music_tracks_hydrated"] > 0, hydrated
    reports.append({"rehydration": hydrated})
    dates = rpc("recommend.feed.v1", {"action": "list_archive_dates"})
    assert dates["dates"], dates
    searched = rpc("recommend.feed.v1", {
        "action": "search_archive", "domain": "music", "query": "", "limit": 2,
    })
    items = searched["media_items"]
    assert len(items) == 2, searched
    local_state_before_lookup = tree_digest(data_root / "curation")
    provenance = rpc("recommend.feed.v1", {"action": "lookup_provenance", "items": items})
    assert provenance["read_only"] is True, provenance
    assert len(provenance["matches"]) == 2, provenance
    assert tree_digest(data_root / "curation") == local_state_before_lookup
    reports.append({"provenance_lookup": "passed", "local_state_unchanged": True})
    for item in items:
        assert item["extra"]["business_date"]
        item["id"] = "integration-" + item["id"]
        rpc("recommend.feed.v1", {
            "action": "push_interactive_set",
            "mode": "interactive_ephemeral",
            "session_id": "integration-repeat",
            "record_permanent_history": False,
            "replace_session_items": False,
            "media_items": [item],
        })
    session_path = data_root / "curation/interactive/sessions/integration-repeat.json"
    session = json.loads(session_path.read_text())
    assert len(session["media_items"]) == 2, session
    assert all(
        item["extra"].get("previously_recommended_on")
        for item in session["media_items"]
    ), session
    reports.append({"session_roundtrip": {"items": 2, "historical_badges": True}})

    rejected = rpc("recommend.feed.v1", {
        "action": "push_interactive_set",
        "mode": "interactive_ephemeral",
        "session_id": "invalid",
        "record_permanent_history": True,
        "media_items": [],
    }, expected_ok=False)
    assert rejected["error_code"] == "ledger_pollution_forbidden", rejected
    rejected = rpc("media.playlist.v1", {
        "action": "write_interactive_playlist",
        "mode": "interactive_ephemeral",
        "playlist_id": "p.YJXV7dEIeb3GzZX",
        "track_ids": ["1463501518"],
    }, expected_ok=False)
    assert rejected["error_code"] == "protected_daily_playlist", rejected
    skill_info = rpc("ai.curation.v1", {"action": "skill_info"})
    assert skill_info["skill_loaded"], skill_info
    reports.append({"firewalls": "passed", "skill_loaded": True})

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "curation-integration", "version": "1"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "omnidesk_recommend_feed",
            "arguments": {"action": "list_archive_dates"},
        }},
    ]
    mcp = subprocess.run(
        [str(pm_binary), "mcp-serve"], env=environment,
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=90,
    )
    assert mcp.returncode == 0, mcp.stderr
    responses = [json.loads(line) for line in mcp.stdout.splitlines() if line.strip()]
    by_id = {response["id"]: response for response in responses}
    assert set(by_id) == {1, 2, 3, 4}, responses
    assert all("result" in response for response in responses), responses
    assert len(by_id[2]["result"]["tools"]) >= 6
    assert not by_id[4]["result"].get("isError", False), by_id[4]
    content = by_id[4]["result"]["content"]
    mcp_result = json.loads(next(part["text"] for part in content if part["type"] == "text"))
    assert mcp_result["data"]["dates"], mcp_result
    reports.append({"mcp_stdio": "passed", "response_count": len(responses)})

    after = {str(root): tree_digest(root) for root in (
        options.obsidian_vault, options.music_daily,
    )}
    assert before == after, "Archive repositories changed during read-only integration"
    reports.append({"repositories_unchanged": True, "sha256": after})
    write_json(runtime / "report.json", {"ok": True, "checks": reports})
    print(json.dumps({
        "ok": True, "runtime": str(runtime), "report": str(runtime / "report.json"),
        "dates": hydrated["dates_indexed"], "checks": len(reports),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
