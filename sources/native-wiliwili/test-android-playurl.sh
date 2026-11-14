#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_binary="$(mktemp /tmp/wiliwili-android-playurl-test.XXXXXX)"
trap 'rm -f "$task_binary"' EXIT
c++ -std=c++17 -Wall -Wextra -Werror \
    -I "$task_root/upstream/wiliwili/include" \
    -I "$task_root/upstream/wiliwili/include/api" \
    -I "$task_root/upstream/library/borealis/library/include/borealis/extern" \
    "$task_root/test_android_playurl.cpp" "$task_root/android_playurl.cpp" "$task_root/native_session.cpp" -lcurl -lcrypto -o "$task_binary"
"$task_binary" "$@"
