#!/usr/bin/env python3
"""Play real public 4K streams in isolated native processes and seek near the end."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent
VIDEOS = [("BV1heam6TExz", 3840, 2160, 1018.389), ("BV1fK4y1t7hj", 4096, 1860, 295.124)]


def run(installed, session, output, codec, verify_container_stopped):
    app = Path.home() / ".local/opt/wiliwili-ai" if installed else ROOT / "build"
    binary = app / "bin/wiliwili" if installed else app / "wiliwili"
    previous = json.loads(subprocess.check_output(["hyprctl", "activeworkspace", "-j"]))["id"]
    used = {w["id"] for w in json.loads(subprocess.check_output(["hyprctl", "workspaces", "-j"]))}
    workspace = next(i for i in range(9900, 10000) if i not in used)
    results = []

    def dispatch(expression):
        assert subprocess.check_output(["hyprctl", "dispatch", expression], text=True).strip() == "ok"

    with tempfile.TemporaryDirectory(prefix="wiliwili-real-4k-") as temporary:
        private = Path(temporary)
        os.chmod(private, 0o700)
        for bvid, width, height, duration in VIDEOS:
            state_path, log_path = private / "state.json", private / "client.log"
            state_path.unlink(missing_ok=True)
            env = os.environ.copy()
            env.pop("WILI_AI_TOKEN", None)
            env["XDG_CONFIG_HOME"] = str(private / "config")
            env["WILI_AI_DIAGNOSTICS"] = str(state_path)
            config = private / "config/wiliwili/wiliwili_config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            private_session = config.parent/'android-session.json'
            shutil.copyfile(session,private_session); private_session.chmod(0o600)
            env["WILI_ANDROID_SESSION"] = str(private_session)
            settings={"setting": {"app_ui_scale": "1080p", "search_tv_mode": False,
                                  "video_quality": 0, "video_codec": codec,"history_report":False}}
            # Reuse the same account's two daily sessions in the isolated client.
            original=Path.home()/'.config/wiliwili/wiliwili_config.json'
            settings['cookie']=json.loads(original.read_text()).get('cookie',{}) if original.exists() else {}
            config.write_text(json.dumps(settings));config.chmod(0o600)
            def state():
                try:
                    return json.loads(state_path.read_text())
                except (ValueError, OSError):
                    return {}
            def wait(predicate, timeout=60):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    data = state()
                    if predicate(data):
                        return data
                    if process.poll() is not None:
                        raise AssertionError("Native client exited")
                    time.sleep(0.2)
                raise AssertionError("Native playback verification timed out")
            with log_path.open("w") as log:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(["stdbuf", "-oL", "-eL", str(binary), "--bv", bvid],
                                           cwd=app, env=env, stdout=log, stderr=log)
                selector = "pid:" + str(process.pid)
                entry = {"bvid": bvid, "source": "native_cpp_android_trial", "installed": installed, "preferred_codec": codec}
                try:
                    if verify_container_stopped:
                        running = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", "wiliwili-android-lab"], text=True).strip()
                        assert running == "false", "Android experiment container must be stopped"
                        entry["android_container_running"] = False
                    until = time.monotonic() + 10
                    while time.monotonic() < until:
                        if any(c["pid"] == process.pid for c in json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))):
                            break
                        time.sleep(0.1)
                    dispatch(f'hl.dsp.window.move({{window="{selector}",workspace={workspace}}})')
                    dispatch(f'hl.dsp.focus({{workspace={workspace}}})')
                    dispatch(f'hl.dsp.focus({{window="{selector}"}})')
                    start = wait(lambda d: not d.get("stopped", True) and d.get("position", 0) > 2
                                 and d.get("media", {}).get("width") == width
                                 and d.get("media", {}).get("height") == height)
                    assert abs(start["media"]["duration"] - duration) < 2
                    assert start["media"]["audio_codec"]
                    selected = re.findall(r"Native quality: preference=(\d+), selected=(\d+), codec=(\d+)", log_path.read_text())
                    assert selected[-1][0] == "0" and selected[-1][1] == "120"
                    dispatch(f'hl.dsp.window.move({{window="{selector}",workspace={workspace}}})')
                    dispatch(f'hl.dsp.focus({{workspace={workspace}}})')
                    dispatch(f'hl.dsp.focus({{window="{selector}"}})')
                    # Native shortcut 9 seeks to 90%; playback must continue
                    # there with the same decoded raster and working audio.
                    for action in ("down", "up"):
                        dispatch(f'hl.dsp.send_key_state({{window="{selector}",mods="",key="9",state="{action}"}})')
                    end = wait(lambda d: d.get("position", 0) > duration * 0.9 + 2
                               and not d.get("stopped", True) and not d.get("paused", True))
                    assert end["media"]["width"] == width and end["media"]["height"] == height
                    entry.update({"result": "pass", "media": end["media"], "selected_quality": 120,
                                  "position_after_seek": end["position"], "full_duration": duration,
                                  "real_video_and_audio_decoded": True})
                    window = next(c for c in json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
                                  if c["pid"] == process.pid)
                    x, y = window["at"]; w, h = window["size"]
                    screenshot = ROOT / ("validation/quality-native-4k-" + bvid + ".png")
                    subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", str(screenshot)], check=True)
                except Exception as error:
                    entry.update({"result": "fail", "error": str(error), "media": state().get("media", {})})
                    failure = Path("/tmp/wiliwili-android-lab/native-failure.log")
                    shutil.copyfile(log_path, failure)
                    os.chmod(failure, 0o600)
                    raise
                finally:
                    results.append(entry)
                    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
                    print(json.dumps(entry, ensure_ascii=False), flush=True)
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(10)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(5)
                    dispatch(f'hl.dsp.focus({{workspace={previous}}})')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--verify-container-stopped", action="store_true")
    parser.add_argument("--codec", type=int, choices=[7, 12, 13], default=12)
    parser.add_argument("--session", type=Path, default=Path.home() / ".config/wiliwili/android-session.json")
    parser.add_argument("--output", type=Path, default=ROOT / "validation/quality-native-4k.json")
    args = parser.parse_args()
    run(args.installed, args.session, args.output, args.codec, args.verify_container_stopped)
