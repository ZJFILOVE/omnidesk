#!/usr/bin/env bash
# Reproducible command-line build; Android Studio does not need to be open.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${ANDROID_JAVA_HOME:-}" ]]; then
    export JAVA_HOME="$ANDROID_JAVA_HOME"
else
    for candidate in "$project_dir"/runtime/android/jdk-21* /usr/lib/jvm/java-21-openjdk*; do
        if [[ -x "$candidate/bin/java" ]]; then
            export JAVA_HOME="$candidate"
            break
        fi
    done
fi
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
if [[ ! -d "$ANDROID_HOME/platforms/android-36" ]]; then
    echo 'Install Android SDK platform 36, build-tools 35.0.0, and platform-tools first.' >&2
    exit 1
fi
cd "$project_dir/android"
if [[ $# -eq 0 ]]; then
    set -- :app:assembleDebug :app:testDebugUnitTest
fi
exec ./gradlew "$@"
