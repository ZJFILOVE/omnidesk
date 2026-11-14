#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_binary="$(mktemp /tmp/wiliwili-session-test.XXXXXX)"
trap 'rm -f "$task_binary"' EXIT
c++ -std=c++17 -Wall -Wextra -Werror \
    -I "$task_root/upstream/wiliwili/include" \
    -I "$task_root/upstream/library/borealis/library/include/borealis/extern" \
    "$task_root/test_native_session.cpp" "$task_root/native_session.cpp" -lcurl -lcrypto -o "$task_binary"
"$task_binary" "$@"
