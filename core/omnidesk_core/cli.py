"""omnidesk-pm CLI for plugin scaffolding, discovery, verification, hot-reload, and control.sock RPC."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .control import ControlClient, ControlRpcError
from .errors import OmniDeskError
from .manifest import scaffold_plugin
from .paths import OmnideskPaths
from .plugins import PluginManager


def _split_csv_flags(values: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for item in values or ():
        for part in item.split(","):
            if cleaned := part.strip():
                out.append(cleaned)
    return out


def _resolve_plugin_dirs(explicit: Sequence[str] | None, paths: OmnideskPaths) -> list[Path]:
    if explicit:
        return [Path(p).expanduser() for p in explicit]
    env_dirs = os.environ.get("OMNIDESK_PLUGINS_DIR", "").strip()
    if env_dirs:
        return [Path(p).expanduser() for p in env_dirs.split(os.pathsep) if p.strip()]
    dirs: list[Path] = []
    cwd_plugins = Path.cwd() / "plugins"
    if cwd_plugins.is_dir():
        dirs.append(cwd_plugins)
    if paths.plugins_dir not in dirs:
        dirs.append(paths.plugins_dir)
    return dirs


def _read_json_arg(raw_payload: str | None, file_path: str | None) -> dict[str, Any]:
    if file_path and raw_payload is not None:
        raise ValueError("Specify either positional JSON payload or --file, not both")
    if file_path:
        text = sys.stdin.read() if file_path == "-" else Path(file_path).expanduser().read_text(encoding="utf-8")
    elif raw_payload is None:
        text = "{}"
    elif raw_payload == "-":
        text = sys.stdin.read()
    else:
        text = raw_payload
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("RPC JSON payload must be a JSON object")
    return parsed


def _emit_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _cmd_list(args: argparse.Namespace, paths: OmnideskPaths) -> int:
    if args.live or args.socket:
        client = ControlClient(socket_path=args.socket, paths=paths)
        resp = client.list_plugins_sync()
        if not resp.get("ok"):
            if args.json:
                _emit_json(resp)
            else:
                print(f"error: {resp.get('error', 'failed to list plugins')}", file=sys.stderr)
            return 1
        items = resp.get("data", {}).get("plugins", [])
    else:
        mgr = PluginManager(plugin_dirs=_resolve_plugin_dirs(args.plugins_dir, paths), paths=paths)
        items = [rec.to_dict() for rec in mgr.list_plugins()]

    if args.json:
        _emit_json({"plugins": items})
        return 0
    if not items:
        print("No plugins found.")
        return 0
    for item in items:
        caps = ",".join(item.get("provides", [])) or "-"
        print(f"{item['plugin_id']}\t{item.get('version') or '-'}\t{item.get('kind') or '-'}\t{item.get('domain') or '-'}\t{item.get('state')}\t{caps}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    created = scaffold_plugin(
        args.target,
        plugin_id=args.id,
        name=args.name,
        version=args.version,
        kind=args.kind,
        domain=args.domain,
        author=args.author,
        description=args.description,
        provides=_split_csv_flags(args.provides),
        consumes=_split_csv_flags(args.consumes),
        permissions=_split_csv_flags(args.permissions),
        force=args.force,
    )
    if args.json:
        _emit_json({"ok": True, "plugin_dir": str(created.resolve()), "plugin_id": args.id or created.name})
    else:
        print(f"Initialized plugin at {created}")
    return 0


def _cmd_verify(args: argparse.Namespace, paths: OmnideskPaths) -> int:
    mgr = PluginManager(plugin_dirs=_resolve_plugin_dirs(args.plugins_dir, paths), paths=paths)
    res = asyncio.run(mgr.verify_plugin(args.target, run_tests=not args.skip_tests, require_tests=args.require_tests))
    if args.json:
        _emit_json(res.to_dict())
    else:
        status = "PASS" if res.ok else "FAIL"
        print(f"{status}: {res.plugin_id} (manifest={res.manifest_ok}, lifecycle={res.lifecycle_ok}, tests={res.tests_ok})")
        for err in res.errors:
            print(f"  - {err}", file=sys.stderr)
    return 0 if res.ok else 1


def _cmd_reload(args: argparse.Namespace, paths: OmnideskPaths) -> int:
    client = ControlClient(socket_path=args.socket, paths=paths)
    resp = client.reload_plugin_sync(args.plugin_id)
    if args.json:
        _emit_json(resp)
    elif resp.get("ok"):
        data = resp.get("data", {})
        print(f"Reloaded {args.plugin_id} (state={data.get('state')}, reload_count={data.get('reload_count')})")
    else:
        print(f"Reload failed for {args.plugin_id}: {resp.get('error', 'unknown error')}", file=sys.stderr)
    return 0 if resp.get("ok") else 1


def _cmd_rpc(args: argparse.Namespace, paths: OmnideskPaths) -> int:
    payload = _read_json_arg(args.payload, args.file)
    client = ControlClient(socket_path=args.socket, paths=paths, timeout=args.timeout, caller=args.caller)
    resp = client.request_sync(
        {
            "id": uuid.uuid4().hex,
            "op": "capability.call",
            "capability": args.capability,
            "caller": args.caller,
            "payload": payload,
        },
        timeout=args.timeout,
    )
    _emit_json(resp)
    return 0 if resp.get("ok") else 1


def _cmd_emit(args: argparse.Namespace, paths: OmnideskPaths) -> int:
    payload = _read_json_arg(args.payload, args.file)
    client = ControlClient(socket_path=args.socket, paths=paths, caller=args.caller)
    resp = client.publish_sync(args.topic, payload)
    _emit_json(resp)
    return 0 if resp.get("ok") else 1


def _cmd_ping(args: argparse.Namespace, paths: OmnideskPaths) -> int:
    client = ControlClient(socket_path=args.socket, paths=paths)
    resp = client.ping_sync()
    if args.json:
        _emit_json(resp)
    else:
        print("pong" if resp.get("ok") else f"error: {resp.get('error')}")
    return 0 if resp.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omnidesk-pm", description="OmniDesk v2 Python plugin manager and RPC CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List discovered or live plugins")
    p_list.add_argument("--plugins-dir", action="append", help="Plugin root directory to scan")
    p_list.add_argument("--socket", help="Query live control.sock instead of scanning local directories")
    p_list.add_argument("--live", action="store_true", help="Query live control.sock")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    p_init = sub.add_parser("init", help="Scaffold a new schema_version=2 pure Python plugin")
    p_init.add_argument("target", help="Target plugin directory path")
    p_init.add_argument("--id", help="Unique plugin ID (defaults to directory name)")
    p_init.add_argument("--name", help="Human-readable plugin name")
    p_init.add_argument("--version", default="0.1.0", help="SemVer version")
    p_init.add_argument("--kind", choices=("provider", "workspace", "hybrid", "extension"), default="provider")
    p_init.add_argument("--domain", choices=("core", "media", "text", "book"), default="core")
    p_init.add_argument("--author", default="OmniDesk")
    p_init.add_argument("--description", default="")
    p_init.add_argument("--provides", action="append", help="Versioned capability ID provided (repeatable)")
    p_init.add_argument("--consumes", action="append", help="Versioned capability ID consumed (repeatable)")
    p_init.add_argument("--permissions", action="append", help="Declarative permission label (repeatable)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing plugin files")
    p_init.add_argument("--json", action="store_true", help="Output JSON")

    p_verify = sub.add_parser("verify", help="Verify plugin manifest, lifecycle, and offline pytest suite")
    p_verify.add_argument("target", help="Plugin ID or directory path")
    p_verify.add_argument("--plugins-dir", action="append", help="Plugin root directory to search")
    p_verify.add_argument("--skip-tests", "--no-tests", dest="skip_tests", action="store_true", help="Skip pytest execution")
    p_verify.add_argument("--require-tests", action="store_true", help="Fail if plugin has no tests")
    p_verify.add_argument("--json", action="store_true", help="Output JSON")

    p_reload = sub.add_parser("reload", help="Hot-reload a plugin via control.sock")
    p_reload.add_argument("plugin_id", help="Plugin ID to hot-reload")
    p_reload.add_argument("--socket", "-s", help="Path to control.sock")
    p_reload.add_argument("--json", action="store_true", help="Output JSON")

    p_rpc = sub.add_parser("rpc", help="Invoke a capability on running OmniDesk via control.sock")
    p_rpc.add_argument("capability", help="Versioned capability ID (e.g. llm.router.v1)")
    p_rpc.add_argument("payload", nargs="?", help="JSON object payload (or '-' for stdin)")
    p_rpc.add_argument("--file", "-f", help="Read JSON object payload from file (or '-')")
    p_rpc.add_argument("--socket", "-s", help="Path to control.sock")
    p_rpc.add_argument("--caller", default="omnidesk-pm", help="Caller identifier")
    p_rpc.add_argument("--timeout", type=float, default=15.0, help="Timeout in seconds")
    p_rpc.add_argument("--json", action="store_true", help="Output JSON (always JSON)")

    p_emit = sub.add_parser("emit", help="Publish an EntityBus event via control.sock")
    p_emit.add_argument("topic", help="EntityBus topic")
    p_emit.add_argument("payload", nargs="?", help="JSON object payload")
    p_emit.add_argument("--file", "-f", help="Read JSON object payload from file")
    p_emit.add_argument("--socket", "-s", help="Path to control.sock")
    p_emit.add_argument("--caller", default="omnidesk-pm", help="Caller identifier")

    p_ping = sub.add_parser("ping", help="Ping running control.sock daemon")
    p_ping.add_argument("--socket", "-s", help="Path to control.sock")
    p_ping.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint for omnidesk-pm CLI."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    paths = OmnideskPaths.from_env()
    try:
        if args.command == "list":
            return _cmd_list(args, paths)
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "verify":
            return _cmd_verify(args, paths)
        if args.command == "reload":
            return _cmd_reload(args, paths)
        if args.command == "rpc":
            return _cmd_rpc(args, paths)
        if args.command == "emit":
            return _cmd_emit(args, paths)
        if args.command == "ping":
            return _cmd_ping(args, paths)
    except (OmniDeskError, ControlRpcError, ValueError, FileExistsError, FileNotFoundError, json.JSONDecodeError) as exc:
        err_code = getattr(exc, "code", "error")
        if getattr(args, "json", False) or args.command in {"rpc", "emit"}:
            _emit_json({"ok": False, "error": str(exc), "error_code": str(err_code)})
        else:
            print(f"error ({err_code}): {exc}", file=sys.stderr)
        return 1
    return 1


__all__ = ["build_parser", "main"]
