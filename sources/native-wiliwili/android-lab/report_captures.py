#!/usr/bin/env python3
"""Convert private Android capture files to shareable, credential-free evidence."""
import argparse
import hashlib
import json
from pathlib import Path

from protobuf_wire import play_reply, public_reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--pattern", default="play-reply-*.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for path in sorted(args.directory.glob(args.pattern)):
        meta = json.loads(path.read_text())
        raw = path.with_suffix(".bin").read_bytes()
        results.append({**{k: meta[k] for k in ("bvid", "trial", "qn", "fnval", "source") if k in meta},
                        "response_sha256": hashlib.sha256(raw).hexdigest(), **public_reply(play_reply(raw))})
    args.output.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"responses": len(results), "highest_quality_with_url":
                      max((v["quality"] for r in results for v in r["videos"] if v["has_url"]), default=0)}))


if __name__ == "__main__":
    main()
