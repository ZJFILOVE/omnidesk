#!/usr/bin/env bash
set -euo pipefail

BUILD_DIR="${1:-/tmp/omnidesk-build}"
PM_BIN="${BUILD_DIR}/omnidesk-pm"
HOST_BIN="${BUILD_DIR}/omnidesk"

if ! command -v ctest >/dev/null 2>&1 && [[ -d "/tmp/omnidesk-toolchain/bin" ]]; then
    export PATH="/tmp/omnidesk-toolchain/bin:${PATH}"
fi

if [[ ! -x "${PM_BIN}" || ! -x "${HOST_BIN}" ]]; then
    echo "Missing built binaries in ${BUILD_DIR}" >&2
    exit 1
fi

TMP_HOME="$(mktemp -d /tmp/omnidesk-e2e.XXXXXX)"
trap 'rm -rf "${TMP_HOME}"' EXIT

export OMNIDESK_DATA_HOME="${TMP_HOME}/data"
export OMNIDESK_CONFIG_HOME="${TMP_HOME}/config"
export OMNIDESK_STAGING_DIR="${TMP_HOME}/staging"
export OMNIDESK_IMAGE_CACHE_DIR="${TMP_HOME}/imgcache"
export OMNIDESK_DISABLE_GITHUB_API=1
export OMNIDESK_SDK_CMAKE_DIR="${BUILD_DIR}/sdk-cmake"
export OMNIDESK_CATALOG_PATH="${BUILD_DIR}/resources/marketplace/catalog.json"

echo "[1/7] Running CTest unit & integration suite..."
ctest --test-dir "${BUILD_DIR}" --output-on-failure

echo "[2/7] Checking omnidesk-pm catalog & GitHub marketplace resolution..."
"${PM_BIN}" catalog --domain core | grep -q "plugin-magpie-agent"
"${PM_BIN}" catalog --domain media | grep -q "plugin-fnos-nas"
"${PM_BIN}" catalog --domain text | grep -q "plugin-linuxdo"
"${PM_BIN}" catalog --domain book | grep -q "plugin-pdf-babeldoc"

echo "[3/7] Installing local out-of-tree sample plugin via CLI & testing config/secrets..."
"${PM_BIN}" install "$(pwd)/tests/test_plugin_sample"
"${PM_BIN}" list | grep -q "plugin-sample-echo"
"${PM_BIN}" config plugin-sample-echo set greeting "HiOmniDesk"
"${PM_BIN}" config plugin-sample-echo set-secret token "secret-token-xyz"
"${PM_BIN}" config plugin-sample-echo show | grep -q "HiOmniDesk"
"${PM_BIN}" verify plugin-sample-echo
"${PM_BIN}" radar >/dev/null

echo "[4/7] Testing scaffolded repo -> local install -> disable -> rollback -> remove..."
"${PM_BIN}" init "${TMP_HOME}/my-plugin" --id plugin-cli-e2e --domain text --kind workspace --provides text.source.v1 --no-git
"${PM_BIN}" install "${TMP_HOME}/my-plugin"
"${PM_BIN}" verify plugin-cli-e2e
"${PM_BIN}" disable plugin-cli-e2e
"${PM_BIN}" enable plugin-cli-e2e
"${PM_BIN}" remove plugin-cli-e2e

echo "[5/7] Testing headless host kernel boot with installed .so plugins loaded..."
HC_OUT="$("${HOST_BIN}" --headless-check)"
echo "${HC_OUT}" | grep -q "installed_plugins: 1"
echo "${HC_OUT}" | grep -Eq "capabilities: ([3-9]|[1-9][0-9]+)"

echo "[6/7] Running live GUI smoke test if graphical display is active..."
probe_sdl_video() {
    python3 -c '
import ctypes
sdl = ctypes.CDLL("libSDL2-2.0.so.0")
rc = sdl.SDL_Init(0x0020)
sdl.SDL_Quit()
raise SystemExit(0 if rc == 0 else 1)
' 2>/dev/null
}
if [[ -n "${WAYLAND_DISPLAY:-}" || -n "${DISPLAY:-}" ]] && probe_sdl_video; then
    "${HOST_BIN}" --smoke-ms 1200 --domain store
    "${HOST_BIN}" --smoke-ms 900 --domain media
    "${HOST_BIN}" --smoke-ms 900 --domain text
    "${HOST_BIN}" --smoke-ms 900 --domain book
    echo "Live GUI smoke windows rendered & exited cleanly across all 4 workbenches!"
elif SDL_VIDEODRIVER=offscreen probe_sdl_video; then
    SDL_VIDEODRIVER=offscreen "${HOST_BIN}" --smoke-ms 900 --domain store
    SDL_VIDEODRIVER=offscreen "${HOST_BIN}" --smoke-ms 700 --domain media
    SDL_VIDEODRIVER=offscreen "${HOST_BIN}" --smoke-ms 700 --domain text
    SDL_VIDEODRIVER=offscreen "${HOST_BIN}" --smoke-ms 700 --domain book
    echo "Offscreen OpenGL GUI smoke windows rendered & exited cleanly across all 4 workbenches (no-UDEV/sandbox fallback verified)!"
else
    echo "No SDL2 video driver available; skipping GUI window test."
fi

echo "[7/7] Optional real-archive curation integration..."
if [[ "${OMNIDESK_RUN_CURATION_INTEGRATION:-0}" == "1" ]]; then
    python3 tests/test_curation_integration.py \
        --host-build "${BUILD_DIR}" \
        --curator-build "${OMNIDESK_CURATOR_BUILD:-/tmp/build-plugin-ai-curator}" \
        --music-build "${OMNIDESK_MUSIC_BUILD:-/tmp/build-plugin-music}" \
        --magpie-build "${OMNIDESK_MAGPIE_BUILD:-/tmp/build-plugin-magpie-agent}" \
        --obsidian-vault "${OMNIDESK_OBSIDIAN_VAULT:-${HOME}/.local/share/omnidesk/curation/repos/obsidian-vault}" \
        --music-daily "${OMNIDESK_MUSIC_DAILY:-${HOME}/.local/share/omnidesk/curation/repos/music-daily}"
else
    echo "Skipping real-archive integration; set OMNIDESK_RUN_CURATION_INTEGRATION=1 to enable."
fi

echo "ALL OMNIDESK E2E CHECKS PASSED!"
