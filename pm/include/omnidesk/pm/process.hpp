#pragma once

#include <functional>
#include <map>
#include <string>
#include <vector>
#include "omnidesk/export.h"

namespace omnidesk::pm {

struct ProcessResult {
    int exit_code = -1;
    bool timed_out = false;
    std::string output;
    std::string error;
};

using OutputCallback = std::function<void(const std::string& chunk)>;

/**
 * Executes a subprocess directly via fork/execvp without invoking a shell,
 * capturing merged stdout+stderr with optional streaming callback and timeout.
 */
OMNIDESK_API ProcessResult runProcess(const std::vector<std::string>& argv,
                                      const std::string& cwd = "",
                                      const std::map<std::string, std::string>& extra_env = {},
                                      int timeout_ms = 300000,
                                      OutputCallback on_output = nullptr);

OMNIDESK_API std::string findExecutableInPath(const std::string& name);

} // namespace omnidesk::pm
