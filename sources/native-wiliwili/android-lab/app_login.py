#!/usr/bin/env python3
"""Obtain an actual Bilibili app session through the account's QR approval."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request

APPKEY = "783bbb7264451d82"
APPSECRET = "2653583c8873dea268ab9386918b1d65"  # Public app signing constant, not an account credential.
UA = "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/130.0.0.0 Mobile Safari/537.36"


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output)


def post(path, params, cookie=None, signed=False):
    headers = {"User-Agent": UA, "Referer": "https://passport.bilibili.com/",
               "Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        params = {**params, "appkey": APPKEY, "local_id": "0", "ts": str(int(time.time()))}
        data = urllib.parse.urlencode(sorted(params.items()))
        data += "&sign=" + hashlib.md5((data + APPSECRET).encode()).hexdigest()
    else:
        data = urllib.parse.urlencode(params)
    if cookie:
        headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookie.items())
    config = "".join("header = " + json.dumps(key + ": " + value) + "\n"
                     for key, value in headers.items())
    config += "data = " + json.dumps(data) + "\n"
    proc = subprocess.run(["curl", "-sS", "--max-time", "10", "--retry", "3", "--retry-all-errors", "--retry-delay", "1",
                           "-4", "--noproxy", "*", "-K", "-",
                           "https://passport.bilibili.com" + path],
                          input=config, capture_output=True, text=True, timeout=50)
    if proc.returncode:
        raise SystemExit("Official login request failed (curl exit " + str(proc.returncode) + "): " + proc.stderr.strip())
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise SystemExit("Official login returned a non-JSON response") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "reuse", "poll", "install"])
    parser.add_argument("--state-dir", type=Path, default=Path("/tmp/wiliwili-android-lab"))
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    if args.state_dir.resolve().is_relative_to(repository):
        raise SystemExit("Login state must be stored outside the repository")
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.state_dir, 0o700)
    if args.action == "install":
        session = json.loads((args.state_dir / "app-session.json").read_text())
        if not session.get("access_token"):
            raise SystemExit("No approved app session exists")
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        target = config_root / "wiliwili" / "android-session.json"
        if target.resolve().is_relative_to(repository):
            raise SystemExit("Credentials must not be installed inside the repository")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_json(target, session)
        print(json.dumps({"installed_private_session": str(target), "mode": "0600"}), flush=True)
        return
    qr_path = args.state_dir / "app-qr.json"
    if args.action == "prepare":
        reply = post("/x/passport-tv-login/qrcode/auth_code", {}, signed=True)
        print(json.dumps({"step": "prepare", "code": reply.get("code")}), flush=True)
        data = reply.get("data") or {}
        if not data.get("url") or not data.get("auth_code"):
            raise SystemExit("QR preparation failed")
        private_json(qr_path, {**data, "created": time.time()})
        import qrcode
        dest = args.state_dir / "app-login-qr.png"
        qrcode.make(data["url"]).save(dest)
        os.chmod(dest, 0o600)
        print(json.dumps({"qr_image": str(dest), "expires_in": data.get("expires_in", 180)}), flush=True)
        return
    data = json.loads(qr_path.read_text())
    if args.action == "reuse":
        config = Path.home() / ".config/wiliwili/wiliwili_config.json"
        cookie = json.loads(config.read_text()).get("cookie") or {}
        reply = post("/x/passport-tv-login/h5/qrcode/confirm",
                     {"auth_code": data["auth_code"], "build": 9130500,
                      "csrf": cookie.get("bili_jct", "")}, cookie=cookie)
        print(json.dumps({"step": "reuse_web", "code": reply.get("code")}), flush=True)
        return
    deadline = data["created"] + 180
    previous = None
    while time.time() < deadline:
        try:
            reply = post("/x/passport-tv-login/qrcode/poll", {"auth_code": data["auth_code"]}, signed=True)
        except (SystemExit, subprocess.TimeoutExpired):
            print(json.dumps({"step": "poll", "transient_network_error": True}), flush=True)
            time.sleep(3)
            continue
        code = reply.get("code")
        if code != previous:
            print(json.dumps({"step": "poll", "code": code}), flush=True)
            previous = code
        session = reply.get("data") or {}
        if code == 0 and session.get("access_token"):
            private_json(args.state_dir / "app-session.json", {**session, "created": time.time()})
            print(json.dumps({"authenticated": True, "credential_saved_privately": True}), flush=True)
            return
        if code not in (86039, 86038, 86090, 0):
            raise SystemExit("QR poll rejected; prepare a new QR")
        if code == 86038:
            raise SystemExit("QR expired; prepare a new QR")
        time.sleep(3)
    raise SystemExit("QR expired; prepare a new QR")


if __name__ == "__main__":
    main()
