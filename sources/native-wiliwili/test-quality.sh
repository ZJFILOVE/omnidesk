#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_binary="$(mktemp /tmp/wiliwili-quality-test.XXXXXX)"
trap 'rm -f "$task_binary"' EXIT
c++ -std=c++17 -Wall -Wextra -Werror -I "$task_root" \
    -I "$task_root/upstream/wiliwili/include/api" \
    -I "$task_root/upstream/library/borealis/library/include/borealis/extern" \
    "$task_root/test_quality.cpp" -o "$task_binary"
"$task_binary"
