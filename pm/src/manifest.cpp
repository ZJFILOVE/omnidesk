#include "omnidesk/pm/manifest.hpp"

#include <cctype>
#include <fstream>
#include <set>
#include "omnidesk/plugin_abi.h"

namespace omnidesk::pm {
namespace {

std::string jsonScalarToString(const nlohmann::json& v) {
    if (v.is_string()) {
        return v.get<std::string>();
    }
    if (v.is_boolean()) {
        return v.get<bool>() ? "true" : "false";
    }
    if (v.is_number_integer()) {
        return std::to_string(v.get<long long>());
    }
    if (v.is_number_float()) {
        return std::to_string(v.get<double>());
    }
    return "";
}

bool isValidPluginId(const std::string& id) {
    if (id.empty() || id.size() > 96) {
        return false;
    }
    if (!std::isalnum(static_cast<unsigned char>(id.front()))) {
        return false;
    }
    if (id.find("..") != std::string::npos || id.find('/') != std::string::npos ||
        id.find('\\') != std::string::npos) {
        return false;
    }
    for (unsigned char ch : id) {
        if (!(std::isalnum(ch) || ch == '-' || ch == '_' || ch == '.')) {
            return false;
        }
    }
    return true;
}

void readStringArray(const nlohmann::json& parent,
                     const char* key,
                     std::vector<std::string>& out) {
    out.clear();
    if (!parent.contains(key) || !parent[key].is_array()) {
        return;
    }
    for (const auto& item : parent[key]) {
        if (item.is_string()) {
            out.push_back(item.get<std::string>());
        }
    }
}

} // namespace

bool parseManifestJson(const nlohmann::json& j, PluginManifest& out, std::string& error) {
    if (!j.is_object()) {
        error = "plugin.json root must be a JSON object";
        return false;
    }

    PluginManifest m;
    m.raw = j;
    m.schema_version = j.value("schema_version", 1);
    m.id = j.value("id", "");
    m.name = j.value("name", "");
    m.version = j.value("version", "");
    m.abi_version = j.value("abi_version", OMNIDESK_PLUGIN_ABI_VERSION);
    m.kind = parsePluginKind(j.value("kind", "provider"));
    m.domain = parseContentDomain(j.value("domain", "core"));
    m.author = j.value("author", "");
    m.description = j.value("description", "");
    m.homepage = j.value("homepage", "");

    if (j.contains("repository") && j["repository"].is_object()) {
        const auto& repo = j["repository"];
        m.repo_type = repo.value("type", "git");
        m.repo_url = repo.value("url", "");
        m.repo_branch = repo.value("branch", "main");
    } else {
        m.repo_type = j.value("repo_type", "git");
        m.repo_url = j.value("repo_url", "");
        m.repo_branch = j.value("repo_branch", "main");
    }

    if (j.contains("release") && j["release"].is_object()) {
        const auto& rel = j["release"];
        m.release.archive_url = rel.value("archive_url", rel.value("tar_zst_url", ""));
        m.release.sha256_url = rel.value("sha256_url", "");
        m.release.sha256 = rel.value("sha256", "");
        m.release.tag = rel.value("tag", "");
    }

    if (j.contains("build") && j["build"].is_object()) {
        const auto& build = j["build"];
        m.build_system = build.value("system", "cmake");
        m.entry_target = build.value("entry_target", "");
        m.output_lib = build.value("output_lib", "");
        m.verify_cmd = build.value("verify_cmd", "");
        readStringArray(build, "cmake_args", m.cmake_args);
    } else {
        m.build_system = j.value("build_system", "cmake");
        m.entry_target = j.value("entry_target", "");
        m.output_lib = j.value("output_lib", "");
        m.verify_cmd = j.value("verify_cmd", "");
        readStringArray(j, "cmake_args", m.cmake_args);
    }

    readStringArray(j, "provides", m.provides);
    readStringArray(j, "consumes", m.consumes);
    readStringArray(j, "permissions", m.permissions);

    if (j.contains("config_schema")) {
        if (!j["config_schema"].is_array()) {
            error = "config_schema must be a JSON array";
            return false;
        }
        for (const auto& entry : j["config_schema"]) {
            if (!entry.is_object()) {
                error = "Each config_schema entry must be a JSON object";
                return false;
            }
            ConfigField field;
            field.key = entry.value("key", "");
            field.label = entry.value("label", field.key);
            field.type = parseConfigFieldType(entry.value("type", "string"));
            if (entry.contains("default")) {
                field.default_value = jsonScalarToString(entry["default"]);
            }
            field.description = entry.value("description", "");
            field.required = entry.value("required", false);
            readStringArray(entry, "options", field.options);
            m.config_schema.push_back(std::move(field));
        }
    }

    if (!validateManifest(m, error)) {
        return false;
    }

    out = std::move(m);
    return true;
}

bool parseManifestFile(const std::string& path, PluginManifest& out, std::string& error) {
    std::ifstream ifs(path);
    if (!ifs.is_open()) {
        error = "Cannot open manifest file: " + path;
        return false;
    }
    try {
        nlohmann::json j = nlohmann::json::parse(ifs);
        return parseManifestJson(j, out, error);
    } catch (const std::exception& e) {
        error = std::string("Invalid JSON in ") + path + ": " + e.what();
        return false;
    }
}

bool validateManifest(const PluginManifest& m, std::string& error) {
    if (m.schema_version != 1) {
        error = "Unsupported manifest schema_version: " + std::to_string(m.schema_version);
        return false;
    }
    if (!isValidPluginId(m.id)) {
        error = "Invalid plugin id '" + m.id + "' (must match [a-zA-Z0-9][a-zA-Z0-9._-]*)";
        return false;
    }
    if (m.name.empty()) {
        error = "Manifest field 'name' must not be empty";
        return false;
    }
    if (m.version.empty()) {
        error = "Manifest field 'version' must not be empty";
        return false;
    }
    if (m.abi_version != OMNIDESK_PLUGIN_ABI_VERSION) {
        error = "Incompatible abi_version " + std::to_string(m.abi_version) +
                " (host expects " + std::to_string(OMNIDESK_PLUGIN_ABI_VERSION) + ")";
        return false;
    }
    if (m.build_system != "cmake") {
        error = "Unsupported build_system '" + m.build_system + "' (expected 'cmake')";
        return false;
    }
    if (!m.output_lib.empty() &&
        (m.output_lib.find("..") != std::string::npos || m.output_lib.front() == '/')) {
        error = "build.output_lib must be a safe relative path inside build directory";
        return false;
    }

    std::set<std::string> seen_keys;
    for (const auto& f : m.config_schema) {
        if (f.key.empty()) {
            error = "config_schema entry has empty 'key'";
            return false;
        }
        if (!seen_keys.insert(f.key).second) {
            error = "Duplicate config_schema key: " + f.key;
            return false;
        }
        if (f.type == ConfigFieldType::Select && f.options.empty()) {
            error = "Select config_schema key '" + f.key + "' must provide non-empty 'options'";
            return false;
        }
    }

    error.clear();
    return true;
}

nlohmann::json manifestToJson(const PluginManifest& m) {
    nlohmann::json schema_arr = nlohmann::json::array();
    for (const auto& f : m.config_schema) {
        nlohmann::json item = {
            {"key", f.key},
            {"label", f.label},
            {"type", toString(f.type)},
            {"default", f.default_value},
            {"description", f.description},
            {"required", f.required},
        };
        if (!f.options.empty()) {
            item["options"] = f.options;
        }
        schema_arr.push_back(std::move(item));
    }

    nlohmann::json out{
        {"schema_version", m.schema_version},
        {"id", m.id},
        {"name", m.name},
        {"version", m.version},
        {"abi_version", m.abi_version},
        {"kind", toString(m.kind)},
        {"domain", toString(m.domain)},
        {"author", m.author},
        {"description", m.description},
        {"homepage", m.homepage},
        {"repository",
         {
             {"type", m.repo_type},
             {"url", m.repo_url},
             {"branch", m.repo_branch},
         }},
        {"build",
         {
             {"system", m.build_system},
             {"entry_target", m.entry_target},
             {"output_lib", m.output_lib},
             {"cmake_args", m.cmake_args},
             {"verify_cmd", m.verify_cmd},
         }},
        {"provides", m.provides},
        {"consumes", m.consumes},
        {"permissions", m.permissions},
        {"config_schema", std::move(schema_arr)},
    };
    if (!m.release.empty() || !m.release.sha256.empty() ||
        !m.release.sha256_url.empty() || !m.release.tag.empty()) {
        out["release"] = {
            {"archive_url", m.release.archive_url},
            {"sha256_url", m.release.sha256_url},
            {"sha256", m.release.sha256},
            {"tag", m.release.tag},
        };
    }
    return out;
}

} // namespace omnidesk::pm
