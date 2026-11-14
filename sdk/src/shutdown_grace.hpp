#pragma once

#include <algorithm>
#include <cstdlib>

namespace omnidesk::detail {

inline int resolveShutdownGraceMs(int override_ms) noexcept {
    if (override_ms >= 0) {
        return override_ms;
    }
    if (const char* env = std::getenv("OMNIDESK_SHUTDOWN_GRACE_MS")) {
        if (*env) {
            char* end = nullptr;
            long val = std::strtol(env, &end, 10);
            if (end && *end == '\0') {
                return static_cast<int>(std::clamp<long>(val, 0L, 30000L));
            }
        }
    }
    return 1500;
}

}  // namespace omnidesk::detail
