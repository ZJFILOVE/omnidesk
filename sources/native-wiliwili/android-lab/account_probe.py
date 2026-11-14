#!/usr/bin/env python3
"""Check an approved app token and compare genuine server play replies.

Credentials, headers, response bodies and signed media URLs remain outside git.
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import time
import urllib.parse

from app_login import private_json
from protobuf_wire import message, play_reply, public_reply

UA = "Dalvik/2.1.0 (Linux; U; Android 12) 9.13.0 os/android model/redroid mobi_app/android build/9130500 channel/master network/2 grpc-java-cronet/1.36.1"
VIDEOS = [("BV1heam6TExz", 117370098027243, 42391570547),
          ("BV1fK4y1t7hj", 883362563, 196018899)]


def b64(fields):
    return base64.b64encode(message(fields)).decode()


def curl(config, *args):
    return subprocess.run(["curl", "-sS", "-4", "--noproxy", "*", "--max-time", "25", "-K", "-", *args],
                          input=config.encode(), capture_output=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/tmp/wiliwili-android-lab"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bvid-only", action="store_true", help="Check play requests without an aid")
    parser.add_argument("--qn", type=int, default=120)
    args = parser.parse_args()
    if args.state_dir.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise SystemExit("Account state and raw replies must remain outside the repository")
    token = json.loads((args.state_dir / "app-session.json").read_text())["access_token"]
    folder = args.state_dir / ("account-bvid" if args.bvid_only else "account-direct")
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)

    query = urllib.parse.urlencode({"access_key": token, "mobi_app": "android", "build": 9130500})
    # The token is supplied via stdin, never argv, stdout or repository evidence.
    config = "url = " + json.dumps("https://api.bilibili.com/x/v2/account/myinfo?" + query) + "\n"
    config += "header = " + json.dumps("User-Agent: " + UA) + "\n"
    check = curl(config)
    account = {"curl_exit": check.returncode}
    if not check.returncode:
        try:
            reply = json.loads(check.stdout)
            data = reply.get("data") or {}
            account.update({"code": reply.get("code"), "accepted": reply.get("code") == 0,
                            "vip_type": (data.get("vip") or {}).get("type"),
                            "vip_status": (data.get("vip") or {}).get("status")})
        except ValueError:
            account["non_json_reply"] = True
    print(json.dumps({"account_check": account}), flush=True)

    headers = {"content-type": "application/grpc", "te": "trailers", "user-agent": UA,
               "grpc-encoding": "gzip", "grpc-accept-encoding": "identity,gzip",
               "authorization": "identify_v1 " + token,
               "x-bili-metadata-bin": b64([(1, token), (2, "android"), (4, 9130500), (5, "master"), (7, "android")]),
               "x-bili-device-bin": b64([(1, 1), (2, 9130500), (4, "android"), (5, "android"), (7, "master"),
                                         (8, "redroid"), (9, "redroid"), (10, "12")]),
               "x-bili-network-bin": b64([(1, 1), (2, "46007")]),
               "x-bili-fawkes-req-bin": b64([(1, "android64"), (2, "prod"), (3, "quality-research")])}
    config = "".join("header = " + json.dumps(k + ": " + v) + "\n" for k, v in headers.items())
    results = []
    for bvid, aid, cid in VIDEOS:
        for trial in (False, True):
            vod = message([(1, 0 if args.bvid_only else aid), (2, cid), (3, args.qn), (5, 4048), (7, 2), (8, 1), (9, 2), (11, int(trial))])
            body = message([(1, vod), (2, "united.player-video-detail.0.0"), (3, "main.my-history.0.0"), (5, bvid)])
            compressed = gzip.compress(body)
            request = folder / "request.grpc"
            fd = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(b"\1" + struct.pack(">I", len(compressed)) + compressed)
            dest = folder / (bvid + ("-trial" if trial else "-ordinary"))
            response = curl(config, "--http2", "-D", str(dest.with_suffix(".headers")),
                            "--data-binary", "@" + str(request),
                            "https://grpc.biliapi.net/bilibili.app.playerunite.v1.Player/PlayViewUnite")
            entry = {"bvid": bvid, "trial": trial, "source": "direct_android_grpc",
                     "authenticated_token_supplied": True, "curl_exit": response.returncode,
                     "qn": args.qn, "fnval": 4048, "bvid_only": args.bvid_only}
            header_file = dest.with_suffix(".headers")
            os.chmod(header_file, 0o600)
            lines = header_file.read_text().splitlines()
            entry["status"] = [line for line in lines if line.startswith(("HTTP/", "grpc-status:"))]
            if not response.returncode:
                try:
                    raw = response.stdout
                    if len(raw) < 5 or len(raw) < 5 + int.from_bytes(raw[1:5], "big"):
                        raise ValueError("missing or truncated grpc message")
                    raw = raw[5:5 + int.from_bytes(raw[1:5], "big")]
                    if response.stdout[0] == 1:
                        raw = gzip.decompress(raw)
                    fd = os.open(dest.with_suffix(".bin"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                    entry.update(public_reply(play_reply(raw)))
                    entry["response_sha256"] = hashlib.sha256(raw).hexdigest()
                    private_json(dest.with_suffix(".json"), {"private": True, **entry})
                except (ValueError, OSError):
                    entry["invalid_grpc_message"] = True
            results.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            time.sleep(1)
    args.output.write_text(json.dumps({"account": account, "results": results}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
