#!/usr/bin/env python3
"""Download returned media privately and report its real raster and full extent."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from protobuf_wire import play_reply, public_reply


def inspect_media(stream, expected_ms, destination):
    errors = []
    for url in [stream["url"], *stream["backup_urls"]]:
        # Pass signed links via stdin, never the command line or a public log.
        config = "url = " + json.dumps(url) + "\n"
        # Android-signed CDN links reject the web player's Referer with HTTP 403.
        config += 'header = "User-Agent: Bilibili Freedoooooom/MarkII"\n'
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            process = subprocess.run(["curl", "-f", "-sS", "-L", "-4", "--noproxy", "*",
                                      "--max-time", "120", "-K", "-"],
                                     input=config.encode(), stdout=output, stderr=subprocess.PIPE,
                                     timeout=130)
        if process.returncode == 0:
            break
        errors.append(process.returncode)
    else:
        return {"download_failed": True, "curl_exits": errors}
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_packets", "-show_entries",
                            "packet=pts_time,duration_time:stream=codec_name,width,height,avg_frame_rate,duration,bit_rate:format=duration,size,bit_rate",
                            "-of", "json", str(destination)], capture_output=True, text=True, timeout=120)
    if probe.returncode:
        return {"ffprobe_failed": True, "exit": probe.returncode}
    result = json.loads(probe.stdout)
    packets = result.pop("packets", [])
    end = max((float(p.get("pts_time", 0)) + float(p.get("duration_time", 0)) for p in packets), default=0)
    video = next((s for s in result.get("streams", []) if "width" in s), {})
    width, height = video.get("width", 0), video.get("height", 0)
    duration = float((result.get("format") or {}).get("duration", video.get("duration", 0)))
    expected = expected_ms / 1000
    result.update({"packet_count": len(packets), "last_packet_end_seconds": end,
                   "expected_duration_seconds": expected,
                   "duration_matches": expected > 0 and abs(duration - expected) <= 2,
                   "packets_reach_end": expected > 0 and abs(end - expected) <= 2,
                   "raster_4k": max(width, height) >= 3840 and min(width, height) >= 1600,
                   "media_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
    result["verified_full_4k"] = result["raster_4k"] and result["duration_matches"] and result["packets_reach_end"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="Sanitized evidence file")
    parser.add_argument("--pattern", default="play-reply-*.json", help="Private capture filename pattern")
    args = parser.parse_args()
    if args.capture_dir.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise SystemExit("Private media and raw captures must remain outside the repository")
    results = []
    for capture in sorted(args.capture_dir.glob(args.pattern)):
        meta = json.loads(capture.read_text())
        raw = capture.with_suffix(".bin").read_bytes()
        parsed = play_reply(raw)
        entry = {**{k: meta[k] for k in ("bvid", "trial", "qn", "fnval", "source") if k in meta},
                 "response_sha256": hashlib.sha256(raw).hexdigest(), **public_reply(parsed)}
        available = [s for s in parsed["videos"] if s["has_url"]]
        best = max(available, key=lambda s: s["quality"], default=None)
        if best:
            media = capture.with_suffix(".m4s")
            entry["media"] = {"requested_quality_id": best["quality"],
                              **inspect_media(best, parsed["timelength"], media)}
        else:
            entry["media"] = {"no_playable_stream": True}
        results.append(entry)
        print(json.dumps({"bvid": entry.get("bvid"), "trial": entry.get("trial"), "media": entry["media"]}), flush=True)
    args.output.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
