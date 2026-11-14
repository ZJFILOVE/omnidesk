#pragma once

#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <vector>
#include "omnidesk/export.h"
#include "omnidesk/types.hpp"

namespace omnidesk::pm {

struct ReleaseArtifactSpec {
    std::string archive_url;  ///< Direct URL or file:// path to prebuilt .tar.zst package
    std::string sha256_url;   ///< Optional URL/file path to SHA-256 checksum text
    std::string sha256;       ///< Optional expected SHA-256 hex digest
    std::string tag;          ///< Optional release version/tag label

    bool empty() const { return archive_url.empty(); }
};

enum class PluginIsolation {
    InProcess = 0,
    Subprocess = 1,
};

struct PluginManifest {
    int schema_version = 1;
    std::string id;
    std::string name;
    std::string version;
    uint32_t abi_version = 1;
    PluginKind kind = PluginKind::Provider;
    ContentDomain domain = ContentDomain::Core;
    std::string author;
    std::string description;
    std::string homepage;

    // Repository info
    std::string repo_type = "git";
    std::string repo_url;
    std::string repo_branch = "main";
    ReleaseArtifactSpec release;

    // Build & verification spec
    std::string build_system = "cmake";
    std::string entry_target;
    std::string output_lib;          ///< Relative path to built .so (optional; auto-discovered if omitted)
    std::vector<std::string> cmake_args;
    std::string verify_cmd;          ///< Optional command run in plugin dir before activation

    // Capabilities & schema
    std::vector<std::string> provides;
    std::vector<std::string> consumes;
    std::vector<ConfigField> config_schema;
    std::vector<std::string> permissions;

    // Fault isolation & worker transport
    PluginIsolation isolation = PluginIsolation::InProcess;
    std::string worker_transport = "pipe";

    nlohmann::json raw = nlohmann::json::object();
};

inline std::string toString(PluginIsolation iso) {
    return iso == PluginIsolation::Subprocess ? "subprocess" : "in_process";
}

inline bool parsePluginIsolation(const std::string& text, PluginIsolation& out) {
    if (text.empty() || text == "in_process") {
        out = PluginIsolation::InProcess;
        return true;
    }
    if (text == "subprocess") {
        out = PluginIsolation::Subprocess;
        return true;
    }
    return false;
}

OMNIDESK_API bool parseManifestJson(const nlohmann::json& j, PluginManifest& out, std::string& error);
OMNIDESK_API bool parseManifestFile(const std::string& path, PluginManifest& out, std::string& error);
OMNIDESK_API bool validateManifest(const PluginManifest& m, std::string& error);
OMNIDESK_API nlohmann::json manifestToJson(const PluginManifest& m);

} // namespace omnidesk::pm
