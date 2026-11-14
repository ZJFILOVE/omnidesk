#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/pm/manager.hpp"
#include "omnidesk/pm/scaffold.hpp"

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
        "  omnidesk-pm rollback <plugin-id>\n"
        "  omnidesk-pm enable <plugin-id>\n"
        "  omnidesk-pm disable <plugin-id>\n"
        "  omnidesk-pm remove <plugin-id>\n"
        "  omnidesk-pm info <plugin-id>\n"
        "  omnidesk-pm config <plugin-id> [show | set <key> <val> | set-secret <key> <val> | get <key>]\n"
        "  omnidesk-pm init <dir> --id <plugin-id> [--name <n>] [--kind provider|workspace|hybrid|extension]\n"
        "                  [--domain core|media|text|book] [--provides a,b] [--consumes c,d] [--owner <github_user>]\n"
        "  omnidesk-pm abi-check <so-path> [--expect-id <id>]\n"
        "  omnidesk-pm verify <plugin-id>\n";
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
            std::cerr << "Usage: omnidesk-pm abi-check <so-path> [--expect-id <id>]\n";
            return 2;
        }
        const std::string so_path = argv[2];
        std::string expect_id;
        for (int i = 3; i + 1 < argc; ++i) {
            if (std::string(argv[i]) == "--expect-id") {
                expect_id = argv[++i];
            }
        }
        std::string err;
        nlohmann::json info;
        if (!omnidesk::pm::PluginInstaller::checkSoAbiInProcess(so_path, expect_id, err, &info)) {
            std::cerr << err << "\n";
            return 1;
        }
        std::cout << info.dump(2) << "\n";
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
    omnidesk::pm::PluginManager manager(installer, caps, bus, domains, config, secrets);

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

    if (cmd == "init" && argc >= 3) {
        omnidesk::pm::ScaffoldOptions opts;
        opts.target_dir = argv[2];
        for (int i = 3; i < argc; ++i) {
            std::string a = argv[i];
            if (a == "--id" && i + 1 < argc) opts.plugin_id = argv[++i];
            else if (a == "--name" && i + 1 < argc) opts.name = argv[++i];
            else if (a == "--author" && i + 1 < argc) opts.author = argv[++i];
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
