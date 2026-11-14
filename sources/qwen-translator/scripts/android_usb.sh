#!/usr/bin/env bash
# Keep this terminal alive during USB testing. Ctrl-C closes only this relay.
set -euo pipefail
nas_host="${1:-100.90.158.48}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
adb reverse tcp:18765 tcp:18765
# Explicit USB proxy points to the computer, not the NAS loopback interface.
adb reverse tcp:17897 tcp:7897
echo 'USB test: NAS http://127.0.0.1:18765 ; download proxy http://127.0.0.1:17897'
exec python3 "$script_dir/android_usb_relay.py" --host "$nas_host"
