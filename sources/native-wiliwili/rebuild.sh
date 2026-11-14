#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_toolchain="$(readlink -m "$task_root/toolchain")"
task_build="$(readlink -m "$task_root/build")"
# A desktop with limited disk space can keep these generated directories in /tmp.
# Recreate their targets if the temporary directory was cleared between boots.
mkdir -p "$task_build"
task_cmake="$task_toolchain/bin/cmake"
if [[ ! -x "$task_cmake" ]]; then
    uv venv "$task_toolchain"
    uv pip install --python "$task_toolchain/bin/python" cmake==4.4.3 ninja==1.13.2
fi
for task_file in ai_helper.hpp desktop_ui.hpp ai_panel.hpp quality_policy.hpp android_playurl.hpp native_session.hpp session_ui.hpp release.hpp media_info.hpp; do
    task_target="$task_root/upstream/wiliwili/include/utils/$task_file"
    if ! cmp -s "$task_root/$task_file" "$task_target"; then cp "$task_root/$task_file" "$task_target"; fi
done
if ! cmp -s "$task_root/ad_filter.hpp" "$task_root/upstream/wiliwili/include/api/bilibili/util/ad_filter.hpp"; then
    cp "$task_root/ad_filter.hpp" "$task_root/upstream/wiliwili/include/api/bilibili/util/ad_filter.hpp"
fi
for task_file in ai_helper.cpp desktop_ui.cpp android_playurl.cpp native_session.cpp session_ui.cpp; do
    task_target="$task_root/upstream/wiliwili/source/utils/$task_file"
    if ! cmp -s "$task_root/$task_file" "$task_target"; then cp "$task_root/$task_file" "$task_target"; fi
done
"$task_cmake" -S "$task_root/upstream" -B "$task_root/build" -G Ninja \
    -DPLATFORM_DESKTOP=ON -DUSE_SDL2=ON -DUSE_SYSTEM_SDL2=ON \
    -DUSE_SYSTEM_CURL=ON -DUSE_SYSTEM_FMT=OFF -DUSE_SYSTEM_TINYXML2=OFF \
    -DDISABLE_OPENCC=ON -DANALYTICS=OFF -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_MAKE_PROGRAM="$task_toolchain/bin/ninja"
"$task_cmake" --build "$task_root/build" -j 6
