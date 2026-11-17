"""Module entrypoint for `python -m omnidesk_core`."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from .cli import main as pm_main
from .control import ControlServer
from .paths import OmnideskPaths
from .plugins import PluginManager


async def _serve_runtime(
    plugin_dirs: list[Path],
    socket_path: str | None,
    *,
    state_path: Path | None = None,
    stop_event: asyncio.Event | None = None,
) -> int:
    paths = OmnideskPaths.from_env()
    paths.ensure_dirs()
    resolved_state_path = state_path if state_path is not None else (paths.state_dir / "plugins.json")
    manager = PluginManager(
        plugin_dirs=plugin_dirs,
        paths=paths,
        isolate_errors=True,
        state_path=resolved_state_path,
    )
    stop_evt = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_evt.set)
        except NotImplementedError:
            pass
    try:
        await manager.load_all()
        server = ControlServer(socket_path=socket_path, paths=paths, plugins=manager)
        async with server:
            await stop_evt.wait()
    finally:
        try:
            await manager.unload_all()
        finally:
            manager.save_state()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if args_list and args_list[0] == "serve":
        parser = argparse.ArgumentParser(prog="python -m omnidesk_core serve")
        parser.add_argument("--plugins-dir", action="append", default=[])
        parser.add_argument("--socket", default=None)
        parser.add_argument("--state-path", default=None)
        ns = parser.parse_args(args_list[1:])
        paths = OmnideskPaths.from_env()
        dirs = [Path(p).expanduser() for p in ns.plugins_dir] or (
            [Path.cwd() / "plugins"] if (Path.cwd() / "plugins").is_dir() else [paths.plugins_dir]
        )
        explicit_state = Path(ns.state_path).expanduser() if ns.state_path else None
        try:
            return asyncio.run(_serve_runtime(dirs, ns.socket, state_path=explicit_state))
        except KeyboardInterrupt:
            return 0
    if args_list and args_list[0] == "pm":
        return pm_main(args_list[1:])
    return pm_main(args_list)


if __name__ == "__main__":
    raise SystemExit(main())
