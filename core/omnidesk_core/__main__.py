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


async def _serve_runtime(plugin_dirs: list[Path], socket_path: str | None) -> int:
    paths = OmnideskPaths.from_env()
    paths.ensure_dirs()
    manager = PluginManager(plugin_dirs=plugin_dirs, paths=paths, isolate_errors=True)
    await manager.load_all()
    server = ControlServer(socket_path=socket_path, paths=paths, plugins=manager)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    async with server:
        await stop_event.wait()
    await manager.unload_all()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if args_list and args_list[0] == "serve":
        parser = argparse.ArgumentParser(prog="python -m omnidesk_core serve")
        parser.add_argument("--plugins-dir", action="append", default=[])
        parser.add_argument("--socket", default=None)
        ns = parser.parse_args(args_list[1:])
        paths = OmnideskPaths.from_env()
        dirs = [Path(p).expanduser() for p in ns.plugins_dir] or (
            [Path.cwd() / "plugins"] if (Path.cwd() / "plugins").is_dir() else [paths.plugins_dir]
        )
        return asyncio.run(_serve_runtime(dirs, ns.socket))
    if args_list and args_list[0] == "pm":
        return pm_main(args_list[1:])
    return pm_main(args_list)


if __name__ == "__main__":
    raise SystemExit(main())
