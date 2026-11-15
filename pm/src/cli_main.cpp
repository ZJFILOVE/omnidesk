#include <algorithm>
#include <array>
#include <borealis/core/logger.hpp>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/plugin.hpp"
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/control_bridge.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/pm/manager.hpp"
#include "omnidesk/pm/scaffold.hpp"
#include "omnidesk/worker_supervisor.hpp"

namespace fs = std::filesystem;

namespace {

std::string defaultDataRoot() {
    if (const char* env = std::getenv("OMNIDESK_DATA_HOME")) {
        if (*env) return env;
    }
    if (const char* xdg = std::getenv("XDG_DATA_HOME")) {
        if (*xdg) return (fs::path(xdg) / "omnidesk").string();
    }
    const char* home = std::getenv("HOME");
    return (fs::path(home ? home : ".") / ".local" / "share" / "omnidesk").string();
}

std::string defaultConfigRoot() {
    if (const char* env = std::getenv("OMNIDESK_CONFIG_HOME")) {
        if (*env) return env;
    }
    if (const char* xdg = std::getenv("XDG_CONFIG_HOME")) {
        if (*xdg) return (fs::path(xdg) / "omnidesk").string();
    }
    const char* home = std::getenv("HOME");
    return (fs::path(home ? home : ".") / ".config" / "omnidesk").string();
}

std::string resolveExePath(const char* argv0) {
    std::error_code ec;
    fs::path self = fs::read_symlink("/proc/self/exe", ec);
    if (!ec && fs::exists(self)) {
        return self.string();
    }
    if (argv0 && *argv0) {
        fs::path p = fs::weakly_canonical(argv0, ec);
        if (!ec && fs::exists(p)) {
            return p.string();
        }
    }
    return "";
}

std::string resolveSdkCmakeDir(const std::string& exe_path) {
    if (const char* env = std::getenv("OMNIDESK_SDK_CMAKE_DIR")) {
        if (*env && fs::exists(env)) {
            return env;
        }
    }
#ifdef OMNIDESK_DEFAULT_SDK_CMAKE_DIR
    if (fs::exists(OMNIDESK_DEFAULT_SDK_CMAKE_DIR)) {
        return OMNIDESK_DEFAULT_SDK_CMAKE_DIR;
    }
#endif
    if (!exe_path.empty()) {
        fs::path bin_dir = fs::path(exe_path).parent_path();
        for (const fs::path& cand : {
                 bin_dir / "sdk-cmake",
                 bin_dir / ".." / "lib" / "cmake" / "OmniDeskSDK",
                 bin_dir / ".." / "sdk",
             }) {
            std::error_code ec;
            if (fs::exists(cand / "OmniDeskSDKConfig.cmake", ec)) {
                return fs::weakly_canonical(cand, ec).string();
            }
        }
    }
    return "";
}

std::string resolveCatalogPath(const std::string& exe_path) {
    if (const char* env = std::getenv("OMNIDESK_CATALOG_PATH")) {
        if (*env && fs::exists(env)) {
            return env;
        }
    }
    if (!exe_path.empty()) {
        fs::path bin_dir = fs::path(exe_path).parent_path();
        for (const fs::path& cand : {
                 bin_dir / "resources" / "marketplace" / "catalog.json",
                 bin_dir / ".." / "share" / "omnidesk" / "marketplace" / "catalog.json",
                 fs::current_path() / "host" / "resources" / "marketplace" / "catalog.json",
             }) {
            std::error_code ec;
            if (fs::exists(cand, ec)) {
                return fs::weakly_canonical(cand, ec).string();
            }
        }
    }
    return "";
}

std::vector<std::string> splitCsv(const std::string& s) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string tok;
    while (std::getline(ss, tok, ',')) {
        if (!tok.empty()) {
            out.push_back(tok);
        }
    }
    return out;
}

void printUsage() {
    std::cout <<
        "omnidesk-pm - OmniDesk C++17 Plugin Marketplace & SDK Manager\n\n"
        "Usage:\n"
        "  omnidesk-pm list\n"
        "  omnidesk-pm catalog [--domain core|media|text|book] [--query <q>] [--file <path>]\n"
        "  omnidesk-pm search <query>\n"
        "  omnidesk-pm install <github-owner/repo | git-url | local-dir | catalog-id> [--branch <br>] [--no-verify-load]\n"
        "  omnidesk-pm radar [--no-changelog]\n"
        "  omnidesk-pm update [<plugin-id> | --all]\n"
        "  omnidesk-pm abi-status [--json] [--strict]\n"
        "  omnidesk-pm rebuild [<plugin-id> | --stale | --all]\n"
        "  omnidesk-pm rollback <plugin-id>\n"
        "  omnidesk-pm enable <plugin-id>\n"
        "  omnidesk-pm disable <plugin-id>\n"
        "  omnidesk-pm remove <plugin-id>\n"
        "  omnidesk-pm info <plugin-id>\n"
        "  omnidesk-pm config <plugin-id> [show | set <key> <val> | set-secret <key> <val> | get <key>]\n"
        "  omnidesk-pm init <dir> --id <plugin-id> [--name <n>] [--kind provider|workspace|hybrid|extension]\n"
        "                  [--domain core|media|text|book] [--provides a,b] [--consumes c,d] [--owner <github_user>]\n"
        "  omnidesk-pm abi-check <so-path> [--expect-id <id>] [--json]\n"
        "  omnidesk-pm verify <plugin-id>\n"
        "  omnidesk-pm rpc <capability_id> [<json_payload> | --file <path>] [--plugin <id>] [--caller <name>]\n"
        "  omnidesk-pm emit <topic> [<json_payload> | --file <path>]\n"
        "  omnidesk-pm context [--json]\n"
        "  omnidesk-pm mcp-serve\n";
}

bool readTextFileOrStdin(const std::string& path, std::string& out, std::string& err) {
    if (path == "-") {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        out = ss.str();
        return true;
    }
    std::ifstream ifs(path);
    if (!ifs.is_open()) {
        err = "Failed opening JSON file: " + path;
        return false;
    }
    std::ostringstream ss;
    ss << ifs.rdbuf();
    out = ss.str();
    return true;
}

bool parsePayloadArgs(int argc,
                      char** argv,
                      int start_idx,
                      bool allow_rpc_flags,
                      nlohmann::json& out_payload,
                      std::string& out_plugin_id,
                      std::string& out_caller,
                      int& out_timeout_ms,
                      std::string& out_err) {
    std::string raw_json;
    bool has_raw = false;

    for (int i = start_idx; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--file") {
            if (i + 1 >= argc) {
                out_err = "--file requires a path argument (or '-')";
                return false;
            }
            if (has_raw) {
                out_err = "Cannot specify both inline JSON payload and --file";
                return false;
            }
            if (!readTextFileOrStdin(argv[++i], raw_json, out_err)) {
                return false;
            }
            has_raw = true;
        } else if (allow_rpc_flags && a == "--plugin") {
            if (i + 1 >= argc) {
                out_err = "--plugin requires a plugin ID";
                return false;
            }
            out_plugin_id = argv[++i];
        } else if (allow_rpc_flags && a == "--caller") {
            if (i + 1 >= argc) {
                out_err = "--caller requires a caller name";
                return false;
            }
            out_caller = argv[++i];
        } else if (allow_rpc_flags && a == "--timeout") {
            if (i + 1 >= argc) {
                out_err = "--timeout requires milliseconds";
                return false;
            }
            try {
                out_timeout_ms = std::stoi(argv[++i]);
            } catch (...) {
                out_err = "Invalid --timeout integer";
                return false;
            }
        } else if (!a.empty() && a[0] == '-') {
            out_err = "Unknown option: " + a;
            return false;
        } else {
            if (has_raw) {
                out_err = "Unexpected extra positional argument: " + a;
                return false;
            }
            raw_json = a;
            has_raw = true;
        }
    }

    if (!has_raw || raw_json.empty()) {
        out_payload = nlohmann::json::object();
        return true;
    }
    try {
        out_payload = nlohmann::json::parse(raw_json);
    } catch (const std::exception& ex) {
        out_err = std::string("Invalid JSON payload: ") + ex.what();
        return false;
    }
    if (!out_payload.is_object()) {
        out_err = "JSON payload must be a JSON object";
        return false;
    }
    return true;
}

nlohmann::json buildCapabilitiesArray(const omnidesk::CapabilityRegistry& caps) {
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& d : caps.listAll()) {
        arr.push_back({
            {"capability_id", d.capability_id},
            {"plugin_id", d.plugin_id},
            {"priority", d.priority},
            {"metadata", d.metadata},
        });
    }
    return arr;
}

nlohmann::json buildPluginsArray(const omnidesk::pm::PluginManager& manager) {
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& st : manager.statuses()) {
        arr.push_back({
            {"id", st.record.manifest.id},
            {"name", st.record.manifest.name},
            {"version", st.record.manifest.version},
            {"domain", omnidesk::toString(st.record.manifest.domain)},
            {"kind", omnidesk::toString(st.record.manifest.kind)},
            {"enabled", st.record.enabled},
            {"loaded", st.loaded},
            {"healthy", st.healthy},
        });
    }
    return arr;
}

nlohmann::json buildOfflineContextSnapshot(const omnidesk::CapabilityRegistry& caps,
                                           const omnidesk::pm::PluginManager& manager) {
    return {
        {"online", false},
        {"offline_fallback", true},
        {"socket_path", omnidesk::pm::resolveControlSocketPath()},
        {"active_domain", ""},
        {"active_media", nlohmann::json::object()},
        {"active_text", nlohmann::json::object()},
        {"active_book", nlohmann::json::object()},
        {"last_recommend_update", nlohmann::json::object()},
        {"focus", nlohmann::json::object()},
        {"selected_word", ""},
        {"capabilities", buildCapabilitiesArray(caps)},
        {"plugins", buildPluginsArray(manager)},
    };
}

nlohmann::json buildOfflineCapabilitiesFromManifests(const omnidesk::pm::PluginInstaller& installer) {
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& rec : installer.listInstalled()) {
        if (!rec.enabled) continue;
        for (const auto& cap : rec.manifest.provides) {
            arr.push_back({
                {"capability_id", cap},
                {"plugin_id", rec.manifest.id},
                {"priority", 100},
                {"metadata", {
                    {"offline_manifest", true},
                    {"isolation", omnidesk::pm::toString(rec.manifest.isolation)},
                }},
            });
        }
    }
    return arr;
}

nlohmann::json buildOfflinePluginsFromManifests(const omnidesk::pm::PluginInstaller& installer) {
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& rec : installer.listInstalled()) {
        arr.push_back({
            {"id", rec.manifest.id},
            {"name", rec.manifest.name},
            {"version", rec.manifest.version},
            {"domain", omnidesk::toString(rec.manifest.domain)},
            {"kind", omnidesk::toString(rec.manifest.kind)},
            {"isolation", omnidesk::pm::toString(rec.manifest.isolation)},
            {"enabled", rec.enabled},
            {"loaded", false},
            {"provides", rec.manifest.provides},
        });
    }
    return arr;
}

nlohmann::json buildOfflineMetadataContext(const omnidesk::pm::PluginInstaller& installer) {
    return {
        {"online", false},
        {"offline_fallback", true},
        {"read_only_metadata", true},
        {"socket_path", omnidesk::pm::resolveControlSocketPath()},
        {"active_domain", ""},
        {"active_media", nlohmann::json::object()},
        {"active_text", nlohmann::json::object()},
        {"active_book", nlohmann::json::object()},
        {"last_recommend_update", nlohmann::json::object()},
        {"focus", nlohmann::json::object()},
        {"selected_word", ""},
        {"capabilities", buildOfflineCapabilitiesFromManifests(installer)},
        {"plugins", buildOfflinePluginsFromManifests(installer)},
    };
}

bool selectOfflinePluginsForCapability(const omnidesk::pm::PluginInstaller& installer,
                                       const std::string& capability_id,
                                       const std::string& explicit_plugin_id,
                                       std::vector<std::string>& out_plugin_ids,
                                       std::string& out_error) {
    out_plugin_ids.clear();
    out_error.clear();

    const auto installed = installer.listInstalled();
    std::map<std::string, omnidesk::pm::InstalledPluginRecord> by_id;
    std::map<std::string, std::vector<std::string>> cap_providers;
    for (const auto& rec : installed) {
        by_id[rec.manifest.id] = rec;
        if (rec.enabled) {
            for (const auto& cap : rec.manifest.provides) {
                cap_providers[cap].push_back(rec.manifest.id);
            }
        }
    }

    std::vector<std::string> seeds;
    if (!explicit_plugin_id.empty()) {
        auto it = by_id.find(explicit_plugin_id);
        if (it == by_id.end()) {
            out_error = "target provider plugin not installed: " + explicit_plugin_id;
            return false;
        }
        if (!it->second.enabled) {
            out_error = "target provider plugin is disabled: " + explicit_plugin_id;
            return false;
        }
        seeds.push_back(explicit_plugin_id);
    } else {
        auto cit = cap_providers.find(capability_id);
        if (cit == cap_providers.end() || cit->second.empty()) {
            out_error = "no enabled offline provider found for capability: " + capability_id;
            return false;
        }
        seeds = cit->second;
    }

    std::set<std::string> visited;
    std::function<void(const std::string&)> visit = [&](const std::string& pid) {
        if (visited.count(pid)) return;
        visited.insert(pid);
        auto it = by_id.find(pid);
        if (it == by_id.end() || !it->second.enabled) return;
        for (const auto& dep_cap : it->second.manifest.consumes) {
            auto pit = cap_providers.find(dep_cap);
            if (pit != cap_providers.end()) {
                for (const auto& dep_pid : pit->second) {
                    visit(dep_pid);
                }
            }
        }
        out_plugin_ids.push_back(pid);
    };

    for (const auto& seed : seeds) {
        visit(seed);
    }
    return !out_plugin_ids.empty();
}

bool readBoundedStdinLine(std::string& pending,
                          std::string& out_line,
                          bool& out_too_large,
                          size_t max_bytes) {
    out_line.clear();
    out_too_large = false;
    bool draining_oversize = false;

    while (true) {
        const size_t nl = pending.find('\n');
        if (nl != std::string::npos) {
            if (draining_oversize || nl + 1 > max_bytes) {
                pending.erase(0, nl + 1);
                out_too_large = true;
                return true;
            }
            out_line = pending.substr(0, nl);
            pending.erase(0, nl + 1);
            if (!out_line.empty() && out_line.back() == '\r') {
                out_line.pop_back();
            }
            return true;
        }

        if (pending.size() >= max_bytes) {
            draining_oversize = true;
            pending.clear();
        }

        char buf[8192];
        const ssize_t n = ::read(STDIN_FILENO, buf, sizeof(buf));
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) {
            if (draining_oversize) {
                out_too_large = true;
                return true;
            }
            if (pending.empty()) {
                return false;
            }
            out_line = std::move(pending);
            pending.clear();
            if (!out_line.empty() && out_line.back() == '\r') {
                out_line.pop_back();
            }
            return true;
        }
        if (!draining_oversize) {
            if (pending.size() + static_cast<size_t>(n) > max_bytes) {
                const size_t avail = max_bytes - pending.size();
                const char* nl_ptr = static_cast<const char*>(
                    std::memchr(buf, '\n', static_cast<size_t>(n)));
                if (nl_ptr && static_cast<size_t>(nl_ptr - buf) < avail) {
                    pending.append(buf, static_cast<size_t>(n));
                } else if (nl_ptr) {
                    pending.clear();
                    pending.append(nl_ptr + 1, static_cast<size_t>(buf + n - (nl_ptr + 1)));
                    out_too_large = true;
                    return true;
                } else {
                    draining_oversize = true;
                    pending.clear();
                }
            } else {
                pending.append(buf, static_cast<size_t>(n));
            }
        } else {
            const char* nl_ptr = static_cast<const char*>(
                std::memchr(buf, '\n', static_cast<size_t>(n)));
            if (nl_ptr) {
                pending.assign(nl_ptr + 1, static_cast<size_t>(buf + n - (nl_ptr + 1)));
                out_too_large = true;
                return true;
            }
        }
    }
}

bool isSupportedMcpProtocolVersion(const std::string& ver) {
    return ver == "2024-11-05" || ver == "2025-03-26" ||
           ver == "2025-06-18" || ver == "2025-11-25";
}

bool hasOnlyKeys(const nlohmann::json& obj, const std::set<std::string>& allowed) {
    for (auto it = obj.begin(); it != obj.end(); ++it) {
        if (allowed.find(it.key()) == allowed.end()) {
            return false;
        }
    }
    return true;
}

nlohmann::json mergeMcpToolPayload(const nlohmann::json& args) {
    if (!args.is_object()) {
        return nlohmann::json::object();
    }
    if (args.contains("payload") && args["payload"].is_object()) {
        nlohmann::json p = args["payload"];
        if (args.contains("action") && args["action"].is_string() && !p.contains("action")) {
            p["action"] = args["action"];
        }
        return p;
    }
    return args;
}

std::string toLowerAscii(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

std::string trimAscii(std::string s) {
    while (!s.empty() && (s.front() == ' ' || s.front() == '\t' || s.front() == '\r' || s.front() == '\n')) {
        s.erase(s.begin());
    }
    while (!s.empty() && (s.back() == ' ' || s.back() == '\t' || s.back() == '\r' || s.back() == '\n')) {
        s.pop_back();
    }
    return s;
}

bool isHex32Ascii(const std::string& s) {
    if (s.size() != 32) return false;
    for (char ch : s) {
        if (!std::isxdigit(static_cast<unsigned char>(ch))) return false;
    }
    return true;
}

std::string normalizeBookProvKey(const std::string& raw) {
    std::string low = toLowerAscii(trimAscii(raw));
    if (low.empty() || low == "auto" || low == "default") return "auto";
    if (low == "all" || low == "both" || low == "*") return "all";
    if (low == "zlib" || low == "z-lib" || low == "zlibrary" || low == "z-library" || low == "plugin-zlib") {
        return "zlib";
    }
    if (low == "annas" || low == "anna" || low == "annas-archive" || low == "annas_archive" ||
        low == "plugin-annas-archive") {
        return "annas-archive";
    }
    return low;
}

std::string stripBookIdPrefix(const std::string& raw_id, std::string* provider_hint) {
    std::string s = trimAscii(raw_id);
    std::string low = toLowerAscii(s);
    if (low.rfind("zlib:", 0) == 0) {
        if (provider_hint) *provider_hint = "zlib";
        return trimAscii(s.substr(5));
    }
    if (low.rfind("annas-archive:", 0) == 0) {
        if (provider_hint) *provider_hint = "annas-archive";
        return trimAscii(s.substr(14));
    }
    if (low.rfind("annas:", 0) == 0) {
        if (provider_hint) *provider_hint = "annas-archive";
        return trimAscii(s.substr(6));
    }
    if (isHex32Ascii(s)) {
        if (provider_hint) *provider_hint = "annas-archive";
        return toLowerAscii(s);
    }
    if (s.find(':') != std::string::npos) {
        if (provider_hint) *provider_hint = "zlib";
    }
    return s;
}

struct BookCapProvider {
    std::string plugin_id;
    std::string provider_key;
    int priority = 0;
};

std::vector<BookCapProvider> extractBookProviders(const nlohmann::json& caps_data,
                                                  const std::string& requested_provider,
                                                  const std::string& id_hint) {
    std::vector<BookCapProvider> all;
    if (caps_data.is_object() && caps_data.contains("capabilities") && caps_data["capabilities"].is_array()) {
        for (const auto& c : caps_data["capabilities"]) {
            if (!c.is_object() || c.value("capability_id", "") != "book.search.v1") continue;
            BookCapProvider bp;
            bp.plugin_id = c.value("plugin_id", "");
            bp.priority = c.value("priority", 0);
            if (c.contains("metadata") && c["metadata"].is_object() &&
                c["metadata"].contains("provider") && c["metadata"]["provider"].is_string()) {
                bp.provider_key = normalizeBookProvKey(c["metadata"]["provider"].get<std::string>());
            }
            if (bp.provider_key.empty() || bp.provider_key == "auto" || bp.provider_key == "all") {
                bp.provider_key = (bp.plugin_id == "plugin-zlib")
                                      ? "zlib"
                                      : (bp.plugin_id == "plugin-annas-archive" ? "annas-archive" : bp.plugin_id);
            }
            if (!bp.plugin_id.empty()) {
                all.push_back(std::move(bp));
            }
        }
    }
    std::stable_sort(all.begin(), all.end(), [](const BookCapProvider& a, const BookCapProvider& b) {
        return a.priority > b.priority;
    });

    const std::string req = normalizeBookProvKey(requested_provider);
    if (req != "auto" && req != "all") {
        std::vector<BookCapProvider> exact;
        for (const auto& p : all) {
            if (p.provider_key == req || toLowerAscii(p.plugin_id) == req) {
                exact.push_back(p);
            }
        }
        return exact;
    }
    if (!id_hint.empty()) {
        std::vector<BookCapProvider> ordered;
        for (const auto& p : all) {
            if (p.provider_key == id_hint) ordered.push_back(p);
        }
        for (const auto& p : all) {
            if (p.provider_key != id_hint) ordered.push_back(p);
        }
        if (!ordered.empty()) return ordered;
    }
    return all;
}

nlohmann::json buildMcpToolsList() {
    return nlohmann::json::array({
        {
            {"name", "omnidesk_get_active_context"},
            {"description", "Read live desktop workbench context (currently playing video/song, active paper/page, selected vocabulary word, loaded capabilities)."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", nlohmann::json::object()},
                {"additionalProperties", false},
            }},
        },
        {
            {"name", "omnidesk_book_search"},
            {"description", "Search books across installed OmniDesk book.search.v1 plugins (Z-Library and Anna's Archive) with automatic provider selection and fallback, or inspect book source status / book details."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"action", {{"type", "string"}, {"enum", {"search", "detail", "status"}}, {"description", "Operation: 'search' (default), 'detail', or 'status'"}}},
                    {"query", {{"type", "string"}, {"description", "Search query (title, author, ISBN, keywords) for action='search'"}}},
                    {"id", {{"type", "string"}, {"description", "Book ID ('raw_id:hash' or 32-hex MD5) for action='detail'"}}},
                    {"provider", {{"type", "string"}, {"enum", {"auto", "all", "zlib", "annas-archive"}}, {"description", "Provider routing: 'auto' (default, fallback enabled), 'all', 'zlib', or 'annas-archive'"}}},
                    {"format", {{"type", "string"}, {"description", "Optional format filter such as 'epub' or 'pdf'"}}},
                    {"language", {{"type", "string"}, {"description", "Optional language filter such as 'en' or 'zh'"}}},
                    {"limit", {{"type", "integer"}, {"description", "Maximum results per provider (1-25)"}}},
                    {"page", {{"type", "integer"}, {"description", "1-based page number"}}},
                }},
                {"additionalProperties", true},
            }},
        },
        {
            {"name", "omnidesk_book_download"},
            {"description", "Download a book into OmniDesk's local bookshelf via plugin-zlib or plugin-annas-archive (automatically inferring provider from ID format when provider='auto'), and optionally open it in BookWorkbench."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"id", {{"type", "string"}, {"description", "Book ID from omnidesk_book_search (Z-Library 'raw_id:hash' or Anna's Archive 32-hex MD5)"}}},
                    {"md5", {{"type", "string"}, {"description", "Alias for Anna's Archive 32-hex MD5"}}},
                    {"provider", {{"type", "string"}, {"enum", {"auto", "zlib", "annas-archive"}}, {"description", "Provider ('auto' infers from ID format)"}}},
                    {"dir", {{"type", "string"}, {"description", "Optional target download directory"}}},
                    {"babeldoc", {{"type", "boolean"}, {"description", "Whether to trigger BabelDOC bilingual PDF translation after download"}}},
                    {"prefer_fast", {{"type", "boolean"}, {"description", "For Anna's Archive, prefer fast download API when configured (default true)"}}},
                    {"open_in_workbench", {{"type", "boolean"}, {"description", "Emit entity.book.open to open the downloaded book in BookWorkbench"}}},
                }},
                {"additionalProperties", true},
            }},
        },
        {
            {"name", "omnidesk_recommend_feed"},
            {"description", "Call recommend.feed.v1 (rehydrate archives, publish daily immutable bundle, push interactive repeatable recommendations, search archive, list dates)."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"action", {{"type", "string"}, {"description", "Action for recommend.feed.v1 (bootstrap_and_rehydrate, list_archive_dates, search_archive, publish_daily_bundle, push_interactive_set, pin_item, dismiss_item, clear_interactive, status)"}}},
                    {"payload", {{"type", "object"}, {"description", "Optional nested payload object merged with action"}}},
                }},
                {"additionalProperties", true},
            }},
        },
        {
            {"name", "omnidesk_media_playlist"},
            {"description", "Call media.playlist.v1 (preflight CN catalog, sync daily authorized p.YJXV7dEIeb3GzZX, write interactive playlist / enqueue playback)."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"action", {{"type", "string"}, {"description", "Action for media.playlist.v1 (preflight_cn_catalog, sync_daily_authorized, write_interactive_playlist, enqueue_playback)"}}},
                    {"payload", {{"type", "object"}, {"description", "Optional nested payload object merged with action"}}},
                }},
                {"additionalProperties", true},
            }},
        },
        {
            {"name", "omnidesk_ai_curation"},
            {"description", "Call ai.curation.v1 (trigger in-app curation or daily pipeline)."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"action", {{"type", "string"}, {"description", "Action for ai.curation.v1 (curate_interactive, run_daily_pipeline)"}}},
                    {"payload", {{"type", "object"}, {"description", "Optional nested payload object merged with action"}}},
                }},
                {"additionalProperties", true},
            }},
        },
        {
            {"name", "omnidesk_call_capability"},
            {"description", "Invoke any OmniDesk capability (capability_id, payload, optional plugin_id)."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"capability_id", {{"type", "string"}, {"description", "Target capability identifier (e.g. sample.echo.v1)"}}},
                    {"payload", {{"type", "object"}, {"description", "JSON object request payload"}}},
                    {"plugin_id", {{"type", "string"}, {"description", "Optional provider plugin ID"}}},
                }},
                {"required", nlohmann::json::array({"capability_id"})},
                {"additionalProperties", false},
            }},
        },
        {
            {"name", "omnidesk_emit_event"},
            {"description", "Publish an event to the host EntityBus (topic, payload)."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"topic", {{"type", "string"}, {"description", "EntityBus topic (e.g. entity.recommend.updated, host.focus_changed)"}}},
                    {"payload", {{"type", "object"}, {"description", "JSON event payload"}}},
                }},
                {"required", nlohmann::json::array({"topic"})},
                {"additionalProperties", false},
            }},
        },
    });
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        printUsage();
        return 1;
    }

    const std::string cmd = argv[1];
    if (cmd == "-h" || cmd == "--help" || cmd == "help") {
        printUsage();
        return 0;
    }

    // Fast path for isolated subprocess ABI verification
    if (cmd == "abi-check") {
        if (argc < 3) {
            std::cerr << "Usage: omnidesk-pm abi-check <so-path> [--expect-id <id>] [--json]\n";
            return 2;
        }
        const std::string so_path = argv[2];
        std::string expect_id;
        bool json_out = false;
        for (int i = 3; i < argc; ++i) {
            std::string a = argv[i];
            if (a == "--expect-id" && i + 1 < argc) {
                expect_id = argv[++i];
            } else if (a == "--json") {
                json_out = true;
            }
        }
        std::string err;
        nlohmann::json info;
        if (!omnidesk::pm::PluginInstaller::checkSoAbiInProcess(so_path, expect_id, err, &info)) {
            if (json_out) {
                if (!info.is_object()) info = nlohmann::json::object();
                info["compatible"] = false;
                info["error"] = err;
                std::cout << "\n" << info.dump() << "\n";
            } else {
                std::cerr << err << "\n";
            }
            return 1;
        }
        std::cout << "\n" << (json_out ? info.dump() : info.dump(2)) << "\n";
        return 0;
    }

    const std::string exe_path = resolveExePath(argv[0]);
    const std::string sdk_cmake_dir = resolveSdkCmakeDir(exe_path);
    const std::string catalog_path = resolveCatalogPath(exe_path);

    omnidesk::pm::PluginInstaller installer(defaultDataRoot(), sdk_cmake_dir, exe_path);
    omnidesk::CapabilityRegistry caps;
    omnidesk::EntityBus bus;
    omnidesk::DomainRegistry domains;
    omnidesk::ConfigStore config(defaultConfigRoot());
    omnidesk::SecretVault secrets(defaultConfigRoot());
    omnidesk::JobCenter jobs(&bus, 2);
    omnidesk::WorkerSupervisor workers;
    omnidesk::pm::PluginManager manager(installer, caps, bus, domains, config, secrets, &jobs, &workers);

    omnidesk::pm::MarketplaceCatalog catalog;
    if (!catalog_path.empty()) {
        catalog.loadFromFile(catalog_path);
    }

    if (cmd == "list") {
        auto recs = installer.listInstalled();
        if (recs.empty()) {
            std::cout << "No plugins installed in " << installer.pluginsDir() << "\n";
            return 0;
        }
        for (const auto& r : recs) {
            std::cout << r.manifest.id << "\t"
                      << r.manifest.version << "\t"
                      << omnidesk::toString(r.manifest.domain) << "\t"
                      << omnidesk::toString(r.manifest.kind) << "\t"
                      << (r.enabled ? "enabled" : "disabled") << "\t"
                      << (r.dev_local ? "local" : r.commit_sha.substr(0, 8)) << "\n";
        }
        return 0;
    }

    if (cmd == "catalog" || cmd == "search") {
        std::optional<omnidesk::ContentDomain> domain_filter;
        std::string query;
        std::string custom_file;

        int start = 2;
        if (cmd == "search" && argc >= 3 && argv[2][0] != '-') {
            query = argv[2];
            start = 3;
        }
        for (int i = start; i < argc; ++i) {
            std::string a = argv[i];
            if (a == "--domain" && i + 1 < argc) {
                domain_filter = omnidesk::parseContentDomain(argv[++i]);
            } else if (a == "--query" && i + 1 < argc) {
                query = argv[++i];
            } else if (a == "--file" && i + 1 < argc) {
                custom_file = argv[++i];
            }
        }
        if (!custom_file.empty()) {
            catalog.loadFromFile(custom_file);
        }
        auto items = catalog.filter(domain_filter, query);
        if (items.empty()) {
            std::cout << "No matching marketplace plugins found.\n";
            return 0;
        }
        for (const auto& item : items) {
            const bool installed = installer.getInstalled(item.id).has_value();
            std::cout << item.id << "\tv" << item.version << "\t["
                      << omnidesk::toString(item.domain) << "/" << omnidesk::toString(item.kind) << "]\t"
                      << (installed ? "[installed]" : "[available]") << "\t"
                      << item.name << " - " << item.description << "\n";
        }
        return 0;
    }

    if (cmd == "install") {
        if (argc < 3) {
            std::cerr << "Usage: omnidesk-pm install <source|id> [--branch <br>] [--no-verify-load]\n";
            return 2;
        }
        std::string target = argv[2];
        std::string branch;
        bool verify_load = true;
        for (int i = 3; i < argc; ++i) {
            std::string a = argv[i];
            if (a == "--branch" && i + 1 < argc) {
                branch = argv[++i];
            } else if (a == "--no-verify-load") {
                verify_load = false;
            }
        }

        if (sdk_cmake_dir.empty()) {
            std::cerr << "Error: Cannot locate OmniDeskSDKConfig.cmake (set OMNIDESK_SDK_CMAKE_DIR)\n";
            return 1;
        }

        auto progress = [](const std::string& stage, const std::string& detail) {
            std::cout << "[" << stage << "] " << detail;
            if (detail.empty() || detail.back() != '\n') {
                std::cout << "\n";
            }
        };

        omnidesk::pm::InstallResult res;
        std::error_code ec;
        std::optional<omnidesk::pm::ReleaseArtifactSpec> release_hint;
        if (fs::exists(target, ec) && fs::is_directory(target, ec)) {
            res = installer.installFromLocal(target, progress);
        } else {
            if (auto cat = catalog.findById(target)) {
                target = cat->repo_url;
                if (branch.empty()) {
                    branch = cat->repo_branch;
                }
                if (!cat->release.empty()) {
                    release_hint = cat->release;
                }
            }
            res = installer.installFromGit(target, branch, progress, release_hint);
        }

        if (!res.ok) {
            std::cerr << "Install failed: " << res.error << "\n";
            return 1;
        }

        if (verify_load) {
            std::string load_err;
            if (!manager.loadPlugin(res.plugin_id, load_err)) {
                std::cerr << "Installed binary but runtime onLoad() check failed: " << load_err << "\n";
                return 1;
            }
            manager.unloadPlugin(res.plugin_id);
        }

        std::cout << "Successfully installed " << res.plugin_id << " v" << res.version
                  << " (" << res.so_path << ", source="
                  << (res.install_source.empty() ? std::string("source-build") : res.install_source)
                  << ")\n";
        return 0;
    }

    if (cmd == "radar") {
        bool include_changelog = true;
        for (int i = 2; i < argc; ++i) {
            if (std::string(argv[i]) == "--no-changelog") include_changelog = false;
        }
        auto checks = installer.checkAllUpdates(include_changelog);
        if (checks.empty()) {
            std::cout << "No installed plugins to scan.\n";
            return 0;
        }
        int updates = 0;
        for (const auto& c : checks) {
            if (!c.ok) {
                std::cout << c.plugin_id << "\t[error] " << c.error << "\n";
                continue;
            }
            if (!c.update_available) {
                std::cout << c.plugin_id << "\t[up-to-date] " << c.local_commit.substr(0, 8) << "\n";
                continue;
            }
            ++updates;
            std::cout << c.plugin_id << "\t[UPDATE " << c.local_commit.substr(0, 8) << " -> "
                      << c.remote_commit.substr(0, 8) << ", +" << c.ahead_commits << " commits]"
                      << (c.prebuilt_available ? "\t[prebuilt .tar.zst]" : "\t[source build in /tmp]")
                      << "\n";
            if (!c.changelog.empty()) {
                std::cout << c.changelog;
                if (c.changelog.back() != '\n') std::cout << "\n";
            }
            if (!c.diff_stat.empty()) {
                std::cout << c.diff_stat;
                if (c.diff_stat.back() != '\n') std::cout << "\n";
            }
        }
        std::cout << "Update Radar: " << updates << "/" << checks.size() << " updates available.\n";
        return 0;
    }

    if (cmd == "update") {
        std::vector<std::string> targets;
        if (argc >= 3 && std::string(argv[2]) != "--all") {
            targets.push_back(argv[2]);
        } else {
            for (const auto& rec : installer.listInstalled()) {
                targets.push_back(rec.manifest.id);
            }
        }
        int failures = 0;
        for (const auto& id : targets) {
            auto chk = installer.checkUpdate(id);
            if (chk.ok && !chk.update_available && !chk.remote_commit.empty()) {
                std::cout << id << ": already up to date (" << chk.local_commit.substr(0, 8) << ")\n";
                continue;
            }
            if (chk.ok && chk.update_available && !chk.changelog.empty()) {
                std::cout << id << " changelog (" << chk.ahead_commits << " commits):\n"
                          << chk.changelog;
                if (chk.changelog.back() != '\n') std::cout << "\n";
            }
            auto res = installer.updatePlugin(id, [](const std::string& s, const std::string& d) {
                std::cout << "[" << s << "] " << d << (d.empty() || d.back() != '\n' ? "\n" : "");
            });
            if (!res.ok) {
                std::cerr << id << ": update failed: " << res.error << "\n";
                ++failures;
            } else {
                std::cout << id << ": updated to v" << res.version
                          << " (" << (res.used_prebuilt ? "prebuilt .tar.zst" : "source build")
                          << ")\n";
            }
        }
        return failures == 0 ? 0 : 1;
    }

    if (cmd == "abi-status") {
        bool json_out = false;
        bool strict = false;
        for (int i = 2; i < argc; ++i) {
            std::string a = argv[i];
            if (a == "--json") json_out = true;
            else if (a == "--strict") strict = true;
        }
        auto statuses = installer.checkInstalledAbiStatus();
        int stale_count = 0;
        for (const auto& st : statuses) {
            if (st.needs_rebuild) ++stale_count;
        }
        if (json_out) {
            nlohmann::json arr = nlohmann::json::array();
            for (const auto& st : statuses) {
                arr.push_back({
                    {"plugin_id", st.plugin_id},
                    {"version", st.version},
                    {"so_path", st.so_path},
                    {"enabled", st.enabled},
                    {"compatible", st.compatible},
                    {"needs_rebuild", st.needs_rebuild},
                    {"can_rebuild", st.can_rebuild},
                    {"plugin_sdk_version", st.plugin_sdk_version},
                    {"plugin_abi_signature", st.plugin_abi_signature},
                    {"host_sdk_version", st.host_sdk_version},
                    {"host_abi_signature", st.host_abi_signature},
                    {"reason", st.reason},
                });
            }
            nlohmann::json root = {
                {"host_sdk_version", omnidesk::sdkVersion()},
                {"host_abi_signature", omnidesk::sdkAbiSignature()},
                {"stale_count", stale_count},
                {"plugins", std::move(arr)},
            };
            std::cout << root.dump(2) << "\n";
        } else {
            std::cout << "Host SDK: " << omnidesk::sdkVersion()
                      << " (ABI signature: " << omnidesk::sdkAbiSignature() << ")\n";
            if (statuses.empty()) {
                std::cout << "No plugins installed.\n";
            }
            for (const auto& st : statuses) {
                std::cout << st.plugin_id << "\tv" << st.version << "\t"
                          << (st.compatible ? "[OK]" : "[STALE - REBUILD REQUIRED]");
                if (!st.plugin_abi_signature.empty()) {
                    std::cout << "\tsig=" << st.plugin_abi_signature;
                }
                if (!st.compatible && !st.reason.empty()) {
                    std::cout << "\t" << st.reason;
                }
                std::cout << "\n";
            }
            std::cout << "ABI Status: " << stale_count << "/" << statuses.size()
                      << " installed plugin(s) need rebuild.\n";
        }
        return (strict && stale_count > 0) ? 1 : 0;
    }

    if (cmd == "rebuild") {
        if (sdk_cmake_dir.empty()) {
            std::cerr << "Error: Cannot locate OmniDeskSDKConfig.cmake (set OMNIDESK_SDK_CMAKE_DIR)\n";
            return 1;
        }
        auto progress = [](const std::string& stage, const std::string& detail) {
            std::cout << "[" << stage << "] " << detail;
            if (detail.empty() || detail.back() != '\n') {
                std::cout << "\n";
            }
        };

        std::string target = (argc >= 3) ? argv[2] : "--stale";
        if (target == "--stale" || target == "--all") {
            const bool only_stale = (target == "--stale");
            auto batch = installer.rebuildPlugins(only_stale, progress);
            std::cout << "Batch rebuild complete: rebuilt=" << batch.rebuilt_count
                      << ", skipped=" << batch.skipped_count
                      << ", failed=" << batch.failed_count << "\n";
            if (!batch.ok) {
                std::cerr << "Batch rebuild errors: " << batch.error << "\n";
                return 1;
            }
            return 0;
        }

        auto res = installer.rebuildPlugin(target, progress);
        if (!res.ok) {
            std::cerr << "Rebuild failed (" << target << "): " << res.error << "\n";
            return 1;
        }
        std::cout << "Rebuilt " << res.plugin_id << " v" << res.version
                  << " (" << res.so_path << ")\n";
        return 0;
    }

    if (cmd == "rollback" && argc >= 3) {
        auto res = installer.rollbackPlugin(argv[2]);
        if (!res.ok) {
            std::cerr << "Rollback failed: " << res.error << "\n";
            return 1;
        }
        std::cout << res.log;
        return 0;
    }

    if (cmd == "enable" && argc >= 3) {
        if (!installer.setEnabled(argv[2], true)) {
            std::cerr << "Plugin not found: " << argv[2] << "\n";
            return 1;
        }
        std::cout << "Enabled " << argv[2] << "\n";
        return 0;
    }

    if (cmd == "disable" && argc >= 3) {
        if (!installer.setEnabled(argv[2], false)) {
            std::cerr << "Plugin not found: " << argv[2] << "\n";
            return 1;
        }
        std::cout << "Disabled " << argv[2] << "\n";
        return 0;
    }

    if (cmd == "remove" && argc >= 3) {
        std::string err;
        if (!installer.removePlugin(argv[2], err)) {
            std::cerr << "Remove failed: " << err << "\n";
            return 1;
        }
        std::cout << "Removed " << argv[2] << "\n";
        return 0;
    }

    if (cmd == "info" && argc >= 3) {
        auto rec = installer.getInstalled(argv[2]);
        if (!rec.has_value()) {
            std::cerr << "Plugin not installed: " << argv[2] << "\n";
            return 1;
        }
        std::cout << omnidesk::pm::manifestToJson(rec->manifest).dump(2) << "\n"
                  << "so_path: " << rec->so_path << "\n"
                  << "so_sha256: " << rec->so_sha256 << "\n"
                  << "commit_sha: " << rec->commit_sha << "\n"
                  << "enabled: " << (rec->enabled ? "true" : "false") << "\n";
        return 0;
    }

    if (cmd == "config" && argc >= 3) {
        const std::string plugin_id = argv[2];
        const std::string sub = argc >= 4 ? argv[3] : "show";
        if (sub == "show") {
            std::cout << config.loadPluginConfig(plugin_id).dump(2) << "\n";
            auto secret_keys = secrets.listKeys(plugin_id);
            if (!secret_keys.empty()) {
                std::cout << "Configured secrets (keys only):\n";
                for (const auto& k : secret_keys) {
                    std::cout << "  - " << k << " = <redacted>\n";
                }
            }
            return 0;
        }
        if (sub == "get" && argc >= 5) {
            std::cout << config.getPluginValue(plugin_id, argv[4]) << "\n";
            return 0;
        }
        if (sub == "set" && argc >= 6) {
            const std::string val = argv[5];
            nlohmann::json jval = val;
            if (val == "true") jval = true;
            else if (val == "false") jval = false;
            if (!config.setPluginValue(plugin_id, argv[4], jval)) {
                std::cerr << "Failed saving config value\n";
                return 1;
            }
            std::cout << "Updated " << plugin_id << "." << argv[4] << "\n";
            return 0;
        }
        if (sub == "set-secret" && argc >= 6) {
            if (!secrets.setSecret(plugin_id, argv[4], argv[5])) {
                std::cerr << "Failed storing secret\n";
                return 1;
            }
            std::cout << "Stored secret " << plugin_id << "." << argv[4] << " in 0600 SecretVault\n";
            return 0;
        }
        std::cerr << "Unknown config subcommand\n";
        return 2;
    }

    if (cmd == "verify" && argc >= 3) {
        const std::string id = argv[2];
        std::string err;
        if (!manager.loadPlugin(id, err)) {
            std::cerr << "Verification failed: " << err << "\n";
            return 1;
        }
        auto st = manager.statusOf(id);
        manager.unloadPlugin(id);
        std::cout << "Verified runtime lifecycle (dlopen -> onLoad -> health -> onUnload): " << id
                  << " healthy=" << (st && st->healthy ? "true" : "false") << "\n";
        return 0;
    }

    if (cmd == "rpc") {
        brls::Logger::setLogOutput(stderr);
        if (argc < 3) {
            std::cerr << "Usage: omnidesk-pm rpc <capability_id> [<json_payload> | --file <path>] [--plugin <id>] [--caller <name>]\n";
            return 2;
        }
        FILE* json_out = stdout;
        int saved_stdout = ::dup(STDOUT_FILENO);
        if (saved_stdout >= 0) {
            if (::dup2(STDERR_FILENO, STDOUT_FILENO) >= 0) {
                if (FILE* f = ::fdopen(saved_stdout, "w")) {
                    json_out = f;
                } else {
                    ::close(saved_stdout);
                }
            } else {
                ::close(saved_stdout);
            }
        }
        auto writeOut = [&](const nlohmann::json& j) {
            const std::string s = j.dump(2) + "\n";
            std::fwrite(s.data(), 1, s.size(), json_out);
            std::fflush(json_out);
            if (json_out != stdout) std::fclose(json_out);
        };

        const std::string capability_id = argv[2];
        nlohmann::json payload;
        std::string plugin_id;
        std::string caller = "omnidesk-pm";
        int timeout_ms = 30000;
        std::string parse_err;
        if (!parsePayloadArgs(argc, argv, 3, true, payload, plugin_id, caller, timeout_ms, parse_err)) {
            std::cerr << parse_err << "\n";
            if (json_out != stdout) std::fclose(json_out);
            return 2;
        }

        nlohmann::json req = {
            {"id", "cli-rpc-1"},
            {"op", "capability.call"},
            {"capability", capability_id},
            {"payload", payload},
            {"caller", caller},
        };
        if (!plugin_id.empty()) {
            req["plugin_id"] = plugin_id;
        }

        auto resp = omnidesk::pm::sendControlRequest(req, timeout_ms);
        if (resp.transport_ok) {
            nlohmann::json out = {
                {"ok", resp.ok},
                {"provider_plugin_id", resp.provider_plugin_id},
                {"data", resp.data},
                {"error", resp.error},
                {"transport", "control_socket"},
            };
            writeOut(out);
            return resp.ok ? 0 : 1;
        }
        if (resp.server_unreachable) {
            manager.loadAllEnabled();
            const omnidesk::CapabilityResult res = plugin_id.empty()
                                                       ? caps.call(capability_id, payload)
                                                       : caps.callProvider(plugin_id, capability_id, payload);
            nlohmann::json out = {
                {"ok", res.ok},
                {"provider_plugin_id", res.provider_plugin_id},
                {"data", res.data},
                {"error", res.error},
                {"offline_fallback", true},
                {"transport", "offline_inprocess"},
            };
            manager.unloadAll();
            jobs.shutdown(1000);
            workers.stopAll();
            caps.shutdown(1000);
            domains.shutdown(1000);
            writeOut(out);
            return res.ok ? 0 : 1;
        }

        nlohmann::json out = {
            {"ok", false},
            {"error", resp.error},
            {"transport", "control_socket"},
        };
        writeOut(out);
        return 1;
    }

    if (cmd == "emit") {
        brls::Logger::setLogOutput(stderr);
        if (argc < 3) {
            std::cerr << "Usage: omnidesk-pm emit <topic> [<json_payload> | --file <path>]\n";
            return 2;
        }
        const std::string topic = argv[2];
        nlohmann::json payload;
        std::string unused_plugin;
        std::string unused_caller;
        int timeout_ms = 15000;
        std::string parse_err;
        if (!parsePayloadArgs(argc, argv, 3, false, payload, unused_plugin, unused_caller, timeout_ms, parse_err)) {
            std::cerr << parse_err << "\n";
            return 2;
        }

        nlohmann::json req = {
            {"id", "cli-emit-1"},
            {"op", "bus.emit"},
            {"topic", topic},
            {"payload", payload},
        };
        auto resp = omnidesk::pm::sendControlRequest(req, timeout_ms);
        if (resp.transport_ok) {
            nlohmann::json out = {
                {"ok", resp.ok},
                {"data", resp.data},
                {"error", resp.error},
                {"transport", "control_socket"},
            };
            std::cout << out.dump(2) << "\n";
            return resp.ok ? 0 : 1;
        }
        if (resp.server_unreachable) {
            const size_t delivered = bus.publish(topic, payload);
            nlohmann::json out = {
                {"ok", true},
                {"data", {{"topic", topic}, {"delivered", delivered}}},
                {"offline_fallback", true},
                {"transport", "offline_inprocess"},
            };
            std::cout << out.dump(2) << "\n";
            return 0;
        }

        nlohmann::json out = {
            {"ok", false},
            {"error", resp.error},
            {"transport", "control_socket"},
        };
        std::cout << out.dump(2) << "\n";
        return 1;
    }

    if (cmd == "context") {
        brls::Logger::setLogOutput(stderr);
        bool json_out = false;
        for (int i = 2; i < argc; ++i) {
            if (std::string(argv[i]) == "--json") {
                json_out = true;
            }
        }
        FILE* out_stream = stdout;
        int saved_stdout = ::dup(STDOUT_FILENO);
        if (saved_stdout >= 0) {
            if (::dup2(STDERR_FILENO, STDOUT_FILENO) >= 0) {
                if (FILE* f = ::fdopen(saved_stdout, "w")) {
                    out_stream = f;
                } else {
                    ::close(saved_stdout);
                }
            } else {
                ::close(saved_stdout);
            }
        }
        auto writeText = [&](const std::string& text) {
            std::fwrite(text.data(), 1, text.size(), out_stream);
            std::fflush(out_stream);
            if (out_stream != stdout) std::fclose(out_stream);
        };

        auto resp = omnidesk::pm::sendControlRequest({{"id", "cli-context-1"}, {"op", "context.get"}}, 10000);
        nlohmann::json snap;
        bool ok = true;
        if (resp.transport_ok) {
            snap = resp.data;
            ok = resp.ok;
        } else if (resp.server_unreachable) {
            manager.loadAllEnabled();
            snap = buildOfflineContextSnapshot(caps, manager);
            manager.unloadAll();
            jobs.shutdown(1000);
            workers.stopAll();
            caps.shutdown(1000);
            domains.shutdown(1000);
        } else {
            if (json_out) {
                writeText(nlohmann::json({{"ok", false}, {"error", resp.error}}).dump(2) + "\n");
            } else {
                std::cerr << "Context query failed: " << resp.error << "\n";
                if (out_stream != stdout) std::fclose(out_stream);
            }
            return 1;
        }

        if (json_out) {
            writeText(snap.dump(2) + "\n");
        } else {
            std::ostringstream ss;
            ss << "OmniDesk Context (" << (snap.value("online", false) ? "online" : "offline") << ")\n"
               << "  socket_path: " << snap.value("socket_path", "") << "\n"
               << "  active_domain: " << snap.value("active_domain", "") << "\n"
               << "  selected_word: " << snap.value("selected_word", "") << "\n"
               << "  plugins: " << (snap.contains("plugins") ? snap["plugins"].size() : 0) << "\n"
               << "  capabilities: " << (snap.contains("capabilities") ? snap["capabilities"].size() : 0) << "\n"
               << snap.dump(2) << "\n";
            writeText(ss.str());
        }
        return ok ? 0 : 1;
    }

    if (cmd == "mcp-serve") {
        brls::Logger::setLogOutput(stderr);
        FILE* mcp_out = stdout;
        int saved_stdout = ::dup(STDOUT_FILENO);
        if (saved_stdout >= 0) {
            if (::dup2(STDERR_FILENO, STDOUT_FILENO) >= 0) {
                FILE* f = ::fdopen(saved_stdout, "w");
                if (f) {
                    mcp_out = f;
                } else {
                    ::close(saved_stdout);
                }
            } else {
                ::close(saved_stdout);
            }
        }
        std::setvbuf(mcp_out, nullptr, _IONBF, 0);

        auto sendMcp = [&](const nlohmann::json& msg) {
            const std::string line = msg.dump() + "\n";
            std::fwrite(line.data(), 1, line.size(), mcp_out);
            std::fflush(mcp_out);
        };

        bool offline_plugins_loaded = false;
        auto ensureOfflinePlugins = [&]() {
            if (!offline_plugins_loaded) {
                manager.loadAllEnabled();
                offline_plugins_loaded = true;
            }
        };

        auto getContextData = [&]() -> nlohmann::json {
            auto resp = omnidesk::pm::sendControlRequest({{"id", "mcp-ctx"}, {"op", "context.get"}}, 10000);
            if (resp.transport_ok && resp.ok) {
                return resp.data;
            }
            if (resp.server_unreachable) {
                ensureOfflinePlugins();
                return buildOfflineContextSnapshot(caps, manager);
            }
            return {{"online", false}, {"error", resp.error}};
        };

        auto getCapabilitiesData = [&]() -> nlohmann::json {
            auto resp = omnidesk::pm::sendControlRequest({{"id", "mcp-caps"}, {"op", "capability.list"}}, 10000);
            if (resp.transport_ok && resp.ok) {
                return resp.data;
            }
            if (resp.server_unreachable) {
                ensureOfflinePlugins();
                return {
                    {"capabilities", buildCapabilitiesArray(caps)},
                    {"offline_fallback", true},
                };
            }
            return {{"capabilities", nlohmann::json::array()}, {"error", resp.error}};
        };

        auto invokeCapability = [&](const std::string& cap_id,
                                    const nlohmann::json& payload,
                                    const std::string& plugin_id,
                                    int timeout_ms = 30000) -> nlohmann::json {
            nlohmann::json req = {
                {"id", "mcp-rpc"},
                {"op", "capability.call"},
                {"capability", cap_id},
                {"payload", payload},
                {"caller", "mcp:omnidesk-mcp"},
                {"timeout_ms", timeout_ms},
            };
            if (!plugin_id.empty()) {
                req["plugin_id"] = plugin_id;
            }
            auto resp = omnidesk::pm::sendControlRequest(req, timeout_ms);
            if (resp.transport_ok) {
                return {
                    {"ok", resp.ok},
                    {"provider_plugin_id", resp.provider_plugin_id},
                    {"data", resp.data},
                    {"error", resp.error},
                    {"transport", "control_socket"},
                };
            }
            if (resp.server_unreachable) {
                ensureOfflinePlugins();
                const omnidesk::CapabilityResult res = plugin_id.empty()
                                                           ? caps.call(cap_id, payload)
                                                           : caps.callProvider(plugin_id, cap_id, payload);
                return {
                    {"ok", res.ok},
                    {"provider_plugin_id", res.provider_plugin_id},
                    {"data", res.data},
                    {"error", res.error},
                    {"offline_fallback", true},
                    {"transport", "offline_inprocess"},
                };
            }
            return {
                {"ok", false},
                {"error", resp.error},
                {"transport", "control_socket"},
            };
        };

        auto emitBusEvent = [&](const std::string& topic, const nlohmann::json& payload) -> nlohmann::json {
            nlohmann::json req = {
                {"id", "mcp-emit"},
                {"op", "bus.emit"},
                {"topic", topic},
                {"payload", payload},
            };
            auto resp = omnidesk::pm::sendControlRequest(req, 10000);
            if (resp.transport_ok) {
                return {
                    {"ok", resp.ok},
                    {"data", resp.data},
                    {"error", resp.error},
                    {"transport", "control_socket"},
                };
            }
            if (resp.server_unreachable) {
                ensureOfflinePlugins();
                const size_t delivered = bus.publish(topic, payload);
                return {
                    {"ok", true},
                    {"data", {{"topic", topic}, {"delivered", delivered}}},
                    {"offline_fallback", true},
                    {"transport", "offline_inprocess"},
                };
            }
            return {
                {"ok", false},
                {"error", resp.error},
                {"transport", "control_socket"},
            };
        };

        std::string line;
        while (std::getline(std::cin, line)) {
            if (!line.empty() && line.back() == '\r') {
                line.pop_back();
            }
            if (line.empty()) {
                continue;
            }

            nlohmann::json req;
            try {
                req = nlohmann::json::parse(line);
            } catch (const std::exception& ex) {
                sendMcp({
                    {"jsonrpc", "2.0"},
                    {"id", nullptr},
                    {"error", {{"code", -32700}, {"message", std::string("Parse error: ") + ex.what()}}},
                });
                continue;
            }
            if (!req.is_object()) {
                sendMcp({
                    {"jsonrpc", "2.0"},
                    {"id", nullptr},
                    {"error", {{"code", -32600}, {"message", "Invalid Request"}}},
                });
                continue;
            }

            const bool has_id = req.contains("id") && !req["id"].is_null();
            const nlohmann::json id = has_id ? req["id"] : nlohmann::json(nullptr);
            const std::string method = req.value("method", "");
            const nlohmann::json params = (req.contains("params") && req["params"].is_object())
                                              ? req["params"]
                                              : nlohmann::json::object();

            if (!req.contains("id") || method == "notifications/initialized" ||
                method.rfind("notifications/", 0) == 0) {
                continue;
            }

            auto sendResult = [&](const nlohmann::json& result) {
                sendMcp({
                    {"jsonrpc", "2.0"},
                    {"id", id},
                    {"result", result},
                });
            };
            auto sendError = [&](int code, const std::string& message) {
                sendMcp({
                    {"jsonrpc", "2.0"},
                    {"id", id},
                    {"error", {{"code", code}, {"message", message}}},
                });
            };

            if (method == "initialize") {
                sendResult({
                    {"protocolVersion", "2024-11-05"},
                    {"capabilities", {{"tools", nlohmann::json::object()}, {"resources", nlohmann::json::object()}}},
                    {"serverInfo", {{"name", "omnidesk-mcp"}, {"version", "0.1.0"}}},
                });
            } else if (method == "ping") {
                sendResult(nlohmann::json::object());
            } else if (method == "resources/list") {
                sendResult({
                    {"resources", nlohmann::json::array({
                        {
                            {"uri", "omnidesk://context/active"},
                            {"name", "Active OmniDesk Workbench Context"},
                            {"description", "Live desktop workbench focus context including active media, article, book, selected word, plugins, and capabilities."},
                            {"mimeType", "application/json"},
                        },
                        {
                            {"uri", "omnidesk://capabilities/list"},
                            {"name", "Registered OmniDesk Capabilities"},
                            {"description", "All capabilities registered in OmniDesk (online host or offline enabled plugins)."},
                            {"mimeType", "application/json"},
                        },
                    })},
                });
            } else if (method == "resources/read") {
                const std::string uri = params.value("uri", "");
                if (uri == "omnidesk://context/active") {
                    sendResult({
                        {"contents", nlohmann::json::array({
                            {
                                {"uri", uri},
                                {"mimeType", "application/json"},
                                {"text", getContextData().dump(2)},
                            },
                        })},
                    });
                } else if (uri == "omnidesk://capabilities/list") {
                    sendResult({
                        {"contents", nlohmann::json::array({
                            {
                                {"uri", uri},
                                {"mimeType", "application/json"},
                                {"text", getCapabilitiesData().dump(2)},
                            },
                        })},
                    });
                } else {
                    sendError(-32602, "Unknown resource URI: " + uri);
                }
            } else if (method == "tools/list") {
                sendResult({{"tools", buildMcpToolsList()}});
            } else if (method == "tools/call") {
                const std::string tool_name = params.value("name", "");
                const nlohmann::json args = (params.contains("arguments") && params["arguments"].is_object())
                                                ? params["arguments"]
                                                : nlohmann::json::object();

                auto sendToolJson = [&](const nlohmann::json& result_json, bool is_error) {
                    sendResult({
                        {"content", nlohmann::json::array({
                            {
                                {"type", "text"},
                                {"text", result_json.dump(2)},
                            },
                        })},
                        {"isError", is_error},
                    });
                };

                if (tool_name == "omnidesk_get_active_context") {
                    nlohmann::json ctx = getContextData();
                    const bool is_err = ctx.contains("error") && ctx["error"].is_string() &&
                                        !ctx["error"].get<std::string>().empty();
                    sendToolJson(ctx, is_err);
                } else if (tool_name == "omnidesk_book_search") {
                    const nlohmann::json merged = mergeMcpToolPayload(args);
                    const std::string action = merged.value("action", "search");
                    const std::string req_prov = normalizeBookProvKey(merged.value("provider", "auto"));
                    std::string id_hint;
                    const std::string clean_id = stripBookIdPrefix(
                        merged.value("id", merged.value("md5", "")), &id_hint);
                    const auto providers = extractBookProviders(getCapabilitiesData(), req_prov, id_hint);
                    if (providers.empty()) {
                        sendToolJson({
                            {"ok", false},
                            {"error", "No matching book.search.v1 provider plugins available"},
                            {"requested_provider", req_prov},
                        }, true);
                    } else if (action == "status") {
                        nlohmann::json sources = nlohmann::json::array();
                        for (const auto& p : providers) {
                            auto r = invokeCapability("book.search.v1", {{"action", "status"}}, p.plugin_id, 15000);
                            sources.push_back({
                                {"plugin_id", p.plugin_id},
                                {"provider", p.provider_key},
                                {"priority", p.priority},
                                {"ok", r.value("ok", false)},
                                {"data", r.value("data", nlohmann::json::object())},
                                {"error", r.value("error", "")},
                            });
                        }
                        sendToolJson({{"ok", true}, {"action", "status"}, {"sources", std::move(sources)}}, false);
                    } else if (action == "detail") {
                        if (clean_id.empty()) {
                            sendToolJson({{"ok", false}, {"error", "omnidesk_book_search action='detail' requires 'id' or 'md5'"}}, true);
                        } else {
                            nlohmann::json attempts = nlohmann::json::array();
                            nlohmann::json winner;
                            bool found = false;
                            for (const auto& p : providers) {
                                nlohmann::json payload = {
                                    {"action", "detail"},
                                    {"provider", p.provider_key},
                                    {"id", clean_id},
                                };
                                if (p.provider_key == "annas-archive" || isHex32Ascii(clean_id)) {
                                    payload["md5"] = clean_id;
                                }
                                auto r = invokeCapability("book.search.v1", payload, p.plugin_id, 30000);
                                if (r.value("ok", false)) {
                                    winner = std::move(r);
                                    winner["provider"] = p.provider_key;
                                    found = true;
                                    break;
                                }
                                attempts.push_back({{"plugin_id", p.plugin_id}, {"provider", p.provider_key}, {"ok", false}, {"error", r.value("error", "")}});
                            }
                            if (found) {
                                sendToolJson(winner, false);
                            } else {
                                sendToolJson({{"ok", false}, {"error", "Book detail lookup failed across providers"}, {"attempts", std::move(attempts)}}, true);
                            }
                        }
                    } else {
                        const std::string query = trimAscii(merged.value("query", merged.value("q", "")));
                        if (query.empty()) {
                            sendToolJson({{"ok", false}, {"error", "omnidesk_book_search requires non-empty 'query'"}}, true);
                        } else {
                            nlohmann::json attempts = nlohmann::json::array();
                            nlohmann::json merged_books = nlohmann::json::array();
                            std::string served_provider;
                            std::string served_plugin;
                            std::string transport;
                            bool any_ok = false;
                            std::string last_err;

                            const int limit = merged.contains("limit") && merged["limit"].is_number_integer()
                                                  ? std::clamp(merged["limit"].get<int>(), 1, 25)
                                                  : 10;
                            const int page = merged.contains("page") && merged["page"].is_number_integer()
                                                 ? std::max(1, merged["page"].get<int>())
                                                 : 1;
                            std::string ext = toLowerAscii(trimAscii(merged.value("format", merged.value("ext", ""))));
                            if (!ext.empty() && ext.front() == '.') ext.erase(ext.begin());
                            const std::string lang = toLowerAscii(trimAscii(merged.value("language", merged.value("lang", ""))));

                            for (const auto& p : providers) {
                                nlohmann::json payload = {
                                    {"action", "search"},
                                    {"provider", p.provider_key},
                                    {"query", query},
                                    {"page", page},
                                    {"limit", limit},
                                };
                                if (p.provider_key == "zlib") {
                                    if (!ext.empty()) payload["extensions"] = nlohmann::json::array({ext});
                                    else if (merged.contains("extensions")) payload["extensions"] = merged["extensions"];
                                    if (!lang.empty()) payload["languages"] = nlohmann::json::array({lang});
                                    else if (merged.contains("languages")) payload["languages"] = merged["languages"];
                                } else {
                                    if (!ext.empty()) payload["ext"] = ext;
                                    if (!lang.empty()) payload["lang"] = lang;
                                }

                                auto r = invokeCapability("book.search.v1", payload, p.plugin_id, 30000);
                                const bool r_ok = r.value("ok", false);
                                const nlohmann::json r_data = r.value("data", nlohmann::json::object());
                                size_t count = (r_ok && r_data.is_object() && r_data.contains("books") && r_data["books"].is_array())
                                                   ? r_data["books"].size()
                                                   : 0;
                                attempts.push_back({
                                    {"plugin_id", p.plugin_id},
                                    {"provider", p.provider_key},
                                    {"ok", r_ok},
                                    {"books_returned", count},
                                    {"error", r.value("error", "")},
                                });
                                if (r_ok) {
                                    any_ok = true;
                                    if (served_plugin.empty()) {
                                        served_plugin = p.plugin_id;
                                        served_provider = p.provider_key;
                                        transport = r.value("transport", "");
                                    }
                                    if (r_data.is_object() && r_data.contains("books") && r_data["books"].is_array()) {
                                        for (auto item : r_data["books"]) {
                                            if (!item.is_object()) continue;
                                            item["provider"] = p.provider_key;
                                            item["provider_plugin_id"] = p.plugin_id;
                                            std::string dl_id = (p.provider_key == "annas-archive")
                                                                    ? item.value("md5", item.value("id", ""))
                                                                    : item.value("id", item.value("md5", ""));
                                            if (!dl_id.empty()) item["download_id"] = dl_id;
                                            merged_books.push_back(std::move(item));
                                            if (req_prov != "all" && static_cast<int>(merged_books.size()) >= limit) {
                                                break;
                                            }
                                        }
                                    }
                                    if (req_prov != "all" && !merged_books.empty()) {
                                        served_plugin = p.plugin_id;
                                        served_provider = p.provider_key;
                                        break;
                                    }
                                } else {
                                    last_err = p.plugin_id + ": " + r.value("error", "");
                                }
                            }
                            if (any_ok) {
                                sendToolJson({
                                    {"ok", true},
                                    {"provider", req_prov == "all" ? std::string("all") : served_provider},
                                    {"provider_plugin_id", served_plugin},
                                    {"transport", transport},
                                    {"data", {
                                        {"query", query},
                                        {"count", merged_books.size()},
                                        {"books", std::move(merged_books)},
                                        {"attempts", std::move(attempts)},
                                    }},
                                }, false);
                            } else {
                                sendToolJson({
                                    {"ok", false},
                                    {"error", last_err.empty() ? "All book providers failed" : last_err},
                                    {"data", {{"query", query}, {"attempts", std::move(attempts)}}},
                                }, true);
                            }
                        }
                    }
                } else if (tool_name == "omnidesk_book_download") {
                    const nlohmann::json merged = mergeMcpToolPayload(args);
                    std::string id_hint;
                    const std::string clean_id = stripBookIdPrefix(
                        merged.value("id", merged.value("download_id", merged.value("md5", ""))), &id_hint);
                    if (clean_id.empty()) {
                        sendToolJson({{"ok", false}, {"error", "omnidesk_book_download requires non-empty 'id' or 'md5'"}}, true);
                    } else {
                        const std::string req_prov = normalizeBookProvKey(merged.value("provider", "auto"));
                        const auto providers = extractBookProviders(getCapabilitiesData(), req_prov, id_hint);
                        if (providers.empty()) {
                            sendToolJson({{"ok", false}, {"error", "No matching book.search.v1 provider available"}}, true);
                        } else {
                            nlohmann::json attempts = nlohmann::json::array();
                            nlohmann::json winner;
                            bool downloaded = false;
                            std::string last_err;
                            for (const auto& p : providers) {
                                if (req_prov == "auto" && !id_hint.empty() && p.provider_key != id_hint) {
                                    if ((id_hint == "annas-archive" && p.provider_key == "zlib" && clean_id.find(':') == std::string::npos) ||
                                        (id_hint == "zlib" && p.provider_key == "annas-archive" && !isHex32Ascii(clean_id))) {
                                        continue;
                                    }
                                }
                                nlohmann::json payload = {
                                    {"action", "download"},
                                    {"provider", p.provider_key},
                                    {"id", clean_id},
                                    {"async", false},
                                    {"babeldoc", merged.value("babeldoc", false)},
                                    {"prefer_fast", merged.value("prefer_fast", true)},
                                };
                                if (p.provider_key == "annas-archive" || isHex32Ascii(clean_id)) {
                                    payload["md5"] = clean_id;
                                }
                                if (merged.contains("dir") && merged["dir"].is_string() && !merged["dir"].get<std::string>().empty()) {
                                    payload["dir"] = merged["dir"];
                                }
                                auto r = invokeCapability("book.search.v1", payload, p.plugin_id, 120000);
                                if (r.value("ok", false)) {
                                    winner = std::move(r);
                                    winner["provider"] = p.provider_key;
                                    if (merged.value("open_in_workbench", false) &&
                                        winner.contains("data") && winner["data"].is_object()) {
                                        const auto& d = winner["data"];
                                        const std::string fp = d.value("file_path", "");
                                        if (!fp.empty()) {
                                            nlohmann::json open_ev = {
                                                {"book_id", d.value("shelf_book_id", clean_id)},
                                                {"title", d.value("title", "")},
                                                {"file_path", fp},
                                                {"format", d.value("format", "")},
                                                {"source_id", p.plugin_id},
                                                {"page", 1},
                                            };
                                            emitBusEvent("entity.book.open", open_ev);
                                            winner["data"]["opened_in_workbench"] = true;
                                        }
                                    }
                                    downloaded = true;
                                    break;
                                }
                                last_err = p.plugin_id + ": " + r.value("error", "");
                                attempts.push_back({{"plugin_id", p.plugin_id}, {"provider", p.provider_key}, {"ok", false}, {"error", r.value("error", "")}});
                            }
                            if (downloaded) {
                                sendToolJson(winner, false);
                            } else {
                                sendToolJson({
                                    {"ok", false},
                                    {"error", last_err.empty() ? "Book download failed" : last_err},
                                    {"attempts", std::move(attempts)},
                                }, true);
                            }
                        }
                    }
                } else if (tool_name == "omnidesk_recommend_feed") {
                    nlohmann::json res = invokeCapability("recommend.feed.v1", mergeMcpToolPayload(args), "");
                    sendToolJson(res, !res.value("ok", false));
                } else if (tool_name == "omnidesk_media_playlist") {
                    nlohmann::json res = invokeCapability("media.playlist.v1", mergeMcpToolPayload(args), "");
                    sendToolJson(res, !res.value("ok", false));
                } else if (tool_name == "omnidesk_ai_curation") {
                    nlohmann::json res = invokeCapability("ai.curation.v1", mergeMcpToolPayload(args), "");
                    sendToolJson(res, !res.value("ok", false));
                } else if (tool_name == "omnidesk_call_capability") {
                    const std::string cap_id = args.value("capability_id", "");
                    if (cap_id.empty()) {
                        sendToolJson({{"ok", false}, {"error", "missing required 'capability_id'"}}, true);
                    } else {
                        const nlohmann::json payload = (args.contains("payload") && args["payload"].is_object())
                                                           ? args["payload"]
                                                           : nlohmann::json::object();
                        const std::string plugin_id = args.value("plugin_id", "");
                        nlohmann::json res = invokeCapability(cap_id, payload, plugin_id);
                        sendToolJson(res, !res.value("ok", false));
                    }
                } else if (tool_name == "omnidesk_emit_event") {
                    const std::string topic = args.value("topic", "");
                    if (topic.empty()) {
                        sendToolJson({{"ok", false}, {"error", "missing required 'topic'"}}, true);
                    } else {
                        const nlohmann::json payload = (args.contains("payload") && args["payload"].is_object())
                                                           ? args["payload"]
                                                           : nlohmann::json::object();
                        nlohmann::json res = emitBusEvent(topic, payload);
                        sendToolJson(res, !res.value("ok", false));
                    }
                } else {
                    sendError(-32602, "Unknown tool: " + tool_name);
                }
            } else {
                sendError(-32601, "Method not found: " + method);
            }
        }

        if (offline_plugins_loaded) {
            manager.unloadAll();
        }
        jobs.shutdown(1000);
        workers.stopAll();
        caps.shutdown(1000);
        domains.shutdown(1000);
        if (mcp_out && mcp_out != stdout) {
            std::fclose(mcp_out);
        }
        return 0;
    }

    if (cmd == "init" && argc >= 3) {
        omnidesk::pm::ScaffoldOptions opts;
        opts.target_dir = argv[2];
        for (int i = 3; i < argc; ++i) {
            std::string a = argv[i];
            if (a == "--id" && i + 1 < argc) opts.plugin_id = argv[++i];
            else if (a == "--name" && i + 1 < argc) opts.name = argv[++i];
            else if (a == "--author" && i + 1 < argc) opts.author = argv[++i];
            else if (a == "--version" && i + 1 < argc) opts.version = argv[++i];
            else if (a == "--description" && i + 1 < argc) opts.description = argv[++i];
            else if (a == "--owner" && i + 1 < argc) opts.github_owner = argv[++i];
            else if (a == "--kind" && i + 1 < argc) opts.kind = omnidesk::parsePluginKind(argv[++i]);
            else if (a == "--domain" && i + 1 < argc) opts.domain = omnidesk::parseContentDomain(argv[++i]);
            else if (a == "--provides" && i + 1 < argc) opts.provides = splitCsv(argv[++i]);
            else if (a == "--consumes" && i + 1 < argc) opts.consumes = splitCsv(argv[++i]);
            else if (a == "--no-git") opts.init_git = false;
        }
        if (opts.plugin_id.empty()) {
            opts.plugin_id = fs::path(opts.target_dir).filename().string();
        }
        auto res = omnidesk::pm::scaffoldPluginRepo(opts);
        if (!res.ok) {
            std::cerr << "Scaffold failed: " << res.error << "\n";
            return 1;
        }
        std::cout << "Created standalone plugin repository at " << res.dir_path << "\n";
        for (const auto& f : res.created_files) {
            std::cout << "  - " << f << "\n";
        }
        return 0;
    }

    printUsage();
    return 1;
}
