#pragma once

#include <string>
#include <vector>
#include "omnidesk/export.h"
#include "omnidesk/types.hpp"

namespace omnidesk::pm {

struct ScaffoldOptions {
    std::string target_dir;
    std::string plugin_id;
    std::string name;
    std::string author = "OmniDesk Developer";
    std::string version = "0.1.0";
    std::string github_owner = "";
    PluginKind kind = PluginKind::Provider;
    ContentDomain domain = ContentDomain::Core;
    std::vector<std::string> provides;
    std::vector<std::string> consumes;
    bool init_git = true;
};

struct ScaffoldResult {
    bool ok = false;
    std::string dir_path;
    std::string error;
    std::vector<std::string> created_files;
};

/**
 * Generates a standalone, CMake-buildable C++17 .so plugin repository
 * ready for local testing or pushing to an independent GitHub repository.
 */
OMNIDESK_API ScaffoldResult scaffoldPluginRepo(const ScaffoldOptions& opts);

} // namespace omnidesk::pm
