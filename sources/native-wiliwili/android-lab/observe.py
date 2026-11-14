#!/usr/bin/env python3
"""Observe only the isolated local Android experiment; keep raw data private."""
import argparse
import json
import os
from pathlib import Path
import time
import threading

import frida


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", type=Path)
    parser.add_argument("--prelude", type=Path, action="append", default=[], help="Additional hook loaded before the probe")
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--state-dir", type=Path, default=Path("/tmp/wiliwili-android-lab"))
    parser.add_argument("--rpc", help="Call an RPC exported by the script")
    parser.add_argument("--rpc-input", type=Path, help="Private JSON argument passed over the local RPC channel")
    parser.add_argument("--pid", type=int, help="Attach directly to a previously inspected test PID")
    args = parser.parse_args()
    if args.state_dir.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise SystemExit("Raw Android captures must remain outside the repository")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    device = frida.get_device_manager().add_remote_device("127.0.0.1:17042")
    pid = args.pid
    if not pid:
        apps = [a for a in device.enumerate_applications() if a.identifier == "tv.danmaku.bili" and a.pid]
        if not apps:
            raise SystemExit("The isolated Bilibili application is not running")
        pid = apps[0].pid
    session = device.attach(pid)
    source = "\n".join(path.read_text() for path in [*args.prelude, args.script])
    if int(frida.__version__.split(".")[0]) >= 17:
        import frida_tools
        bridge = Path(frida_tools.__path__[0]) / "bridges" / "java.js"
        source = bridge.read_text() + "\nglobalThis.Java = bridge;\n" + source
    script = session.create_script(source)
    count = 0

    def on_message(message, data):
        nonlocal count
        if message.get("type") == "send":
            payload = message.get("payload") or {}
            if payload.get("private"):
                count += 1
                label = payload.get("label", "capture")
                if not label.replace("-", "").replace("_", "").isalnum():
                    label = "capture"
                dest = args.state_dir / f"{label}-{count:04d}"
                private_write(dest.with_suffix(".json"), json.dumps(payload).encode())
                if data is not None:
                    private_write(dest.with_suffix(".bin"), bytes(data))
                print(json.dumps({"captured": label, "bytes": len(data or b"")}), flush=True)
            else:
                print(json.dumps(payload, ensure_ascii=False), flush=True)
        elif message.get("type") == "error":
            # Stack traces contain source locations, never dump request object values.
            print(json.dumps({"script_error": message.get("description", "unknown"),
                              "stack": message.get("stack", "").splitlines()[:8]}), flush=True)

    script.on("message", on_message)
    script.load()
    if args.rpc:
        cancel = frida.Cancellable()
        timer = threading.Timer(50, cancel.cancel)
        timer.start()
        try:
            with cancel:
                method = getattr(script.exports_sync, args.rpc)
                result = method(json.loads(args.rpc_input.read_text())) if args.rpc_input else method()
            print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            timer.cancel()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    session.detach()


if __name__ == "__main__":
    main()
