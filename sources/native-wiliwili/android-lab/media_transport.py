#!/usr/bin/env python3
"""Diagnose CDN transport using private URLs and small byte ranges."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.parse

from protobuf_wire import play_reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    parsed = play_reply(args.capture.read_bytes())
    stream = max((s for s in parsed["videos"] if s["has_url"]), key=lambda s: s["quality"])
    for index, url in enumerate([stream["url"], *stream["backup_urls"]]):
        for mode in ("android", "browser", "android-referer", "proxy", "http"):
            target = url.replace("https://", "http://", 1) if mode == "http" else url
            config = "url = " + json.dumps(target) + "\n"
            ua = "Mozilla/5.0" if mode == "browser" else "Bilibili Freedoooooom/MarkII"
            config += "header = " + json.dumps("User-Agent: " + ua) + "\n"
            config += 'range = "0-65535"\n'
            if mode == "android-referer":
                config += 'header = "Referer: https://www.bilibili.com/"\n'
            routing = ["--proxy", "http://127.0.0.1:7897"] if mode == "proxy" else ["--noproxy", "*"]
            p = subprocess.run(["curl", "-sS", "-4", "--max-time", "10", "-K", "-", *routing,
                                "-w", "\nCURLSTATUS:%{http_code}:%{content_type}:%{remote_ip}"],
                               input=config.encode(), capture_output=True, timeout=12)
            body, _, tail = p.stdout.rpartition(b"\nCURLSTATUS:")
            out = {"host": urllib.parse.urlparse(target).hostname, "backup": index,
                   "mode": mode, "curl_exit": p.returncode, "status": tail.decode(errors="replace"),
                   "bytes": len(body), "response_sha256": hashlib.sha256(body).hexdigest(),
                   "mp4_header": b"ftyp" in body[:32]}
            if len(body) < 256 and b"<" not in body and b"http" not in body:
                # Only recognized plain diagnostic messages, never arbitrary response bodies.
                out["https_only_error"] = b"Only HTTPS requests are allowed" in body
            print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
