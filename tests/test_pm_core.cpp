#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <dlfcn.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <unistd.h>
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/pm/manager.hpp"
#include "omnidesk/pm/manifest.hpp"
#include "omnidesk/pm/process.hpp"
#include "omnidesk/pm/scaffold.hpp"
#include "omnidesk/sdk.hpp"

#define TEST_CHECK(cond)                                                               \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            std::cerr << "TEST_CHECK failed at " << __FILE__ << ":" << __LINE__        \
                      << " (" << #cond << ")\n";                                       \
            std::exit(1);                                                              \
        }                                                                              \
    } while (false)

namespace fs = std::filesystem;

namespace {

int runShell(const std::string& cmd) {
    return std::system(cmd.c_str());
}

std::string sha256File(const fs::path& p) {
    const std::string cmd = "sha256sum '" + p.string() + "'";
    FILE* pipe = ::popen(cmd.c_str(), "r");
    if (!pipe) return "";
    char buf[256] = {};
    std::string out;
    if (std::fgets(buf, sizeof(buf), pipe)) {
        out = buf;
    }
    ::pclose(pipe);
    auto sp = out.find(' ');
    return sp == std::string::npos ? "" : out.substr(0, sp);
}

} // namespace

int main() {
    const fs::path tmp_root = fs::temp_directory_path() / ("omnidesk_pm_test_" + std::to_string(getpid()));
    std::error_code ec;
    fs::remove_all(tmp_root, ec);
    fs::create_directories(tmp_root, ec);

    ::setenv("OMNIDESK_DISABLE_GITHUB_API", "1", 1);

    {
        const auto started = std::chrono::steady_clock::now();
        auto blocked = omnidesk::pm::runProcess(
            {"python3", "-c", "import os,time; os.close(1); os.close(2); time.sleep(30)"},
            "", {}, 150);
        TEST_CHECK(blocked.timed_out);
        TEST_CHECK(std::chrono::steady_clock::now() - started < std::chrono::seconds(3));
    }

    // 1. Manifest validation checks
    {
        omnidesk::pm::PluginManifest bad;
        bad.id = "../escape";
        bad.name = "Bad";
        bad.version = "0.1.0";
        std::string err;
        TEST_CHECK(!omnidesk::pm::validateManifest(bad, err));

        bad.id = "plugin-good";
        bad.abi_version = 999;
        TEST_CHECK(!omnidesk::pm::validateManifest(bad, err));
    }

    // 2. Catalog load, filter & GitHub normalization
    omnidesk::pm::MarketplaceCatalog catalog;
    TEST_CHECK(catalog.loadFromFile(OMNIDESK_TEST_CATALOG));
    TEST_CHECK(catalog.findById("plugin-magpie-agent").has_value());
    TEST_CHECK(catalog.findById("plugin-fnos-nas").has_value());
    TEST_CHECK(!catalog.filter(omnidesk::ContentDomain::Text, "Linux.do").empty());
    TEST_CHECK(omnidesk::pm::MarketplaceCatalog::normalizeGitSource("ZJFILOVE/omnidesk-plugin-magpie-agent") ==
           "https://github.com/ZJFILOVE/omnidesk-plugin-magpie-agent.git");

    // 3. Staging directory placement (/tmp default + env override) & local build
    const std::string data_dir = (tmp_root / "data").string();
    const std::string cfg_dir = (tmp_root / "config").string();
    omnidesk::pm::PluginInstaller installer(data_dir, OMNIDESK_TEST_SDK_CMAKE_DIR, OMNIDESK_TEST_PM_EXE);

    ::unsetenv("OMNIDESK_STAGING_DIR");
    TEST_CHECK(installer.stagingDir().rfind("/tmp/", 0) == 0);
    const std::string custom_staging = (tmp_root / "custom-staging").string();
    ::setenv("OMNIDESK_STAGING_DIR", custom_staging.c_str(), 1);
    TEST_CHECK(installer.stagingDir() == custom_staging);

    auto res1 = installer.installFromLocal(OMNIDESK_TEST_SAMPLE_PLUGIN);
    if (!res1.ok) {
        std::cerr << "First installFromLocal failed: " << res1.error << "\nLOG:\n" << res1.log << "\n";
        return 1;
    }
    TEST_CHECK(res1.plugin_id == "plugin-sample-echo");
    TEST_CHECK(fs::exists(res1.so_path));
    TEST_CHECK(fs::exists(installer.lockFilePath()));

    // Keep a copy of the compiled .so to package into a real .tar.zst release artifact below
    const fs::path pkg_stage = tmp_root / "pkg-src";
    fs::create_directories(pkg_stage / "workers", ec);
    fs::copy_file(fs::path(OMNIDESK_TEST_SAMPLE_PLUGIN) / "plugin.json",
                  pkg_stage / "plugin.json", fs::copy_options::overwrite_existing);
    fs::copy_file(res1.so_path, pkg_stage / fs::path(res1.so_path).filename(),
                  fs::copy_options::overwrite_existing);
    {
        std::ofstream wofs(pkg_stage / "workers" / "helper.py");
        wofs << "print('helper')\n";
    }
    const fs::path tar_zst_path = tmp_root / "plugin-sample-echo-v1.0.0.tar.zst";
    TEST_CHECK(runShell("tar --zstd -cf '" + tar_zst_path.string() + "' -C '" + pkg_stage.string() + "' .") == 0);
    const std::string tar_sha = sha256File(tar_zst_path);
    TEST_CHECK(tar_sha.size() == 64);

    // Runtime dlopen + onLoad + CapabilityRegistry + DomainRegistry + EntityBus
    omnidesk::CapabilityRegistry caps;
    omnidesk::EntityBus bus;
    omnidesk::DomainRegistry domains;
    omnidesk::ConfigStore cfg(cfg_dir);
    omnidesk::SecretVault secrets(cfg_dir);
    omnidesk::pm::PluginManager mgr(installer, caps, bus, domains, cfg, secrets);

    std::string load_err;
    TEST_CHECK(mgr.loadPlugin("plugin-sample-echo", load_err));
    TEST_CHECK(mgr.isLoaded("plugin-sample-echo"));
    TEST_CHECK(caps.has("sample.echo.v1"));
    TEST_CHECK(domains.mediaSources().size() == 1);
    TEST_CHECK(domains.workspaceTabs().size() == 1);

    auto call_res = caps.call("sample.echo.v1", {{"hello", "world"}});
    TEST_CHECK(call_res.ok);
    TEST_CHECK(call_res.data["echo"]["hello"] == "world");
    TEST_CHECK(call_res.provider_plugin_id == "plugin-sample-echo");

    TEST_CHECK(mgr.disablePlugin("plugin-sample-echo"));
    TEST_CHECK(!mgr.isLoaded("plugin-sample-echo"));
    TEST_CHECK(!caps.has("sample.echo.v1"));
    TEST_CHECK(domains.mediaSources().empty());
    TEST_CHECK(domains.workspaceTabs().empty());

    TEST_CHECK(mgr.enablePlugin("plugin-sample-echo", load_err));
    TEST_CHECK(caps.has("sample.echo.v1"));
    mgr.unloadAll();

    // Install second time -> backup snapshot + rollback
    auto res2 = installer.installFromLocal(OMNIDESK_TEST_SAMPLE_PLUGIN);
    TEST_CHECK(res2.ok);
    auto rec_after = installer.getInstalled("plugin-sample-echo");
    TEST_CHECK(rec_after.has_value() && !rec_after->backup_versions.empty());
    auto rb = installer.rollbackPlugin("plugin-sample-echo");
    TEST_CHECK(rb.ok);

    // 4. Prebuilt .tar.zst SHA-256 rejection + installFromGit prebuilt vs fallback + Update Radar
    {
        std::string rm_err;
        TEST_CHECK(installer.removePlugin("plugin-sample-echo", rm_err));

        omnidesk::pm::ReleaseArtifactSpec bad_rel;
        bad_rel.archive_url = "file://" + tar_zst_path.string();
        bad_rel.sha256 = std::string(64, '0');
        bad_rel.tag = "v1.0.0";
        auto bad_pre = installer.installFromPrebuilt(bad_rel);
        TEST_CHECK(!bad_pre.ok);
        TEST_CHECK(bad_pre.error.find("SHA-256 mismatch") != std::string::npos);

        // Set up local git repo + bare upstream remote
        const fs::path work_repo = tmp_root / "work-repo";
        const fs::path bare_repo = tmp_root / "upstream.git";
        fs::create_directories(work_repo, ec);
        fs::copy(OMNIDESK_TEST_SAMPLE_PLUGIN, work_repo, fs::copy_options::recursive | fs::copy_options::overwrite_existing);
        TEST_CHECK(runShell("git init -b main '" + work_repo.string() + "' >/dev/null 2>&1") == 0);
        TEST_CHECK(runShell("git -C '" + work_repo.string() + "' config user.name 'Test' && git -C '" + work_repo.string() + "' config user.email 't@example.com'") == 0);
        TEST_CHECK(runShell("git -C '" + work_repo.string() + "' add -A && git -C '" + work_repo.string() + "' commit -m 'initial v0.1.0' >/dev/null 2>&1") == 0);
        TEST_CHECK(runShell("git clone --bare '" + work_repo.string() + "' '" + bare_repo.string() + "' >/dev/null 2>&1") == 0);

        const std::string bare_url = "file://" + bare_repo.string();

        // Install from prebuilt .tar.zst with valid SHA-256 via release_hint
        omnidesk::pm::ReleaseArtifactSpec good_rel = bad_rel;
        good_rel.sha256 = tar_sha;
        auto pre_git = installer.installFromGit(bare_url, "main", nullptr, good_rel);
        if (!pre_git.ok) {
            std::cerr << "Prebuilt installFromGit failed: " << pre_git.error << "\nLOG:\n" << pre_git.log << "\n";
            return 1;
        }
        TEST_CHECK(pre_git.used_prebuilt);
        TEST_CHECK(pre_git.install_source == "prebuilt-tar-zst");
        auto rec_pre = installer.getInstalled("plugin-sample-echo");
        TEST_CHECK(rec_pre && rec_pre->install_source == "prebuilt-tar-zst");
        TEST_CHECK(fs::exists(fs::path(rec_pre->install_dir) / "workers" / "helper.py"));

        // Install from git with broken prebuilt hint -> falls back to /tmp CMake source build
        auto fb_git = installer.installFromGit(bare_url, "main", nullptr, bad_rel);
        if (!fb_git.ok) {
            std::cerr << "Fallback installFromGit failed: " << fb_git.error << "\nLOG:\n" << fb_git.log << "\n";
            return 1;
        }
        TEST_CHECK(!fb_git.used_prebuilt);
        TEST_CHECK(!fb_git.fallback_reason.empty());

        auto up0 = installer.checkUpdate("plugin-sample-echo", true);
        TEST_CHECK(up0.ok && !up0.update_available);

        // Push 2 upstream commits bumping version to 0.2.0
        {
            std::ifstream ifs(work_repo / "plugin.json");
            nlohmann::json mj;
            ifs >> mj;
            mj["version"] = "0.2.0";
            std::ofstream ofs(work_repo / "plugin.json");
            ofs << mj.dump(2) << "\n";
        }
        TEST_CHECK(runShell("git -C '" + work_repo.string() + "' add plugin.json && git -C '" + work_repo.string() + "' commit -m 'chore: bump to 0.2.0' >/dev/null 2>&1") == 0);
        {
            std::ofstream cl(work_repo / "CHANGELOG.md");
            cl << "# Changelog\n- Add v0.2.0 update\n";
        }
        TEST_CHECK(runShell("git -C '" + work_repo.string() + "' add CHANGELOG.md && git -C '" + work_repo.string() + "' commit -m 'docs: add CHANGELOG' >/dev/null 2>&1") == 0);
        TEST_CHECK(runShell("git -C '" + work_repo.string() + "' push '" + bare_repo.string() + "' main >/dev/null 2>&1") == 0);

        auto up1 = installer.checkUpdate("plugin-sample-echo", true);
        TEST_CHECK(up1.ok && up1.update_available && up1.ahead_commits == 2);
        TEST_CHECK(!up1.changelog.empty() && up1.changelog.find("CHANGELOG") != std::string::npos);
        TEST_CHECK(!up1.diff_stat.empty() && up1.diff_stat.find("plugin.json") != std::string::npos);

        auto all_up = installer.checkAllUpdates(true);
        TEST_CHECK(!all_up.empty());

        auto upd_res = installer.updatePlugin("plugin-sample-echo");
        TEST_CHECK(upd_res.ok && upd_res.version == "0.2.0");

        // 5. PluginManager unload cancels plugin JobCenter jobs & stops plugin WorkerSupervisor processes
        omnidesk::JobCenter jobs(&bus, 2);
        omnidesk::WorkerSupervisor workers;
        omnidesk::pm::PluginManager tied_mgr(installer, caps, bus, domains, cfg, secrets, &jobs, &workers);
        TEST_CHECK(tied_mgr.loadPlugin("plugin-sample-echo", load_err));

        const fs::path py_worker = tmp_root / "plugin_worker.py";
        {
            std::ofstream wofs(py_worker);
            wofs << "import json, sys\n"
                    "for line in sys.stdin:\n"
                    "    if not line.strip(): continue\n"
                    "    print(json.dumps({'ok':True,'data':{'pong':True}}), flush=True)\n";
        }
        omnidesk::WorkerSpec ws;
        ws.worker_id = "sample-sidecar";
        ws.plugin_id = "plugin-sample-echo";
        ws.executable = "python3";
        ws.args = {"-u", py_worker.string()};
        TEST_CHECK(workers.registerWorker(ws));
        TEST_CHECK(workers.request("sample-sidecar", {{"ping", 1}}).ok);
        const int wpid = workers.status("sample-sidecar")->pid;
        TEST_CHECK(wpid > 0);

        std::atomic<bool> job_exited{false};
        omnidesk::JobSpec js{"Plugin BG Job", "plugin-sample-echo", "bg", {}};
        jobs.submit(js, [&](omnidesk::JobHandle& h, std::string&) {
            while (!h.isCancelled()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            job_exited.store(true);
            return false;
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(25));

        tied_mgr.unloadPlugin("plugin-sample-echo");
        TEST_CHECK(job_exited.load());
        auto wst = workers.status("sample-sidecar");
        TEST_CHECK(!wst || !wst->running);
        TEST_CHECK(::kill(wpid, 0) == -1 && errno == ESRCH);

        workers.stopAll();
        jobs.shutdown();
    }

    // 6. Verify scaffolded repo builds and installs cleanly too
    const fs::path scaffold_dir = tmp_root / "scaffolded-repo";
    omnidesk::pm::ScaffoldOptions sopts;
    sopts.target_dir = scaffold_dir.string();
    sopts.plugin_id = "plugin-scaffold-smoke";
    sopts.name = "Scaffold Smoke";
    sopts.provides = {"smoke.ping.v1"};
    sopts.init_git = false;
    auto sres = omnidesk::pm::scaffoldPluginRepo(sopts);
    TEST_CHECK(sres.ok);

    auto res3 = installer.installFromLocal(sres.dir_path);
    if (!res3.ok) {
        std::cerr << "Scaffold installFromLocal failed: " << res3.error << "\nLOG:\n" << res3.log << "\n";
        return 1;
    }
    TEST_CHECK(mgr.loadPlugin("plugin-scaffold-smoke", load_err));
    TEST_CHECK(caps.has("smoke.ping.v1"));

    // Verify DsoViewLease prevents unsafe dlclose() when a plugin view lease is still alive
    {
        bool saw_pinned_event = false;
        uint64_t ev_tok = bus.subscribe("plugin.state_changed", "test", [&](const std::string&, const nlohmann::json& p) {
            if (p.value("plugin_id", "") == "plugin-scaffold-smoke" &&
                p.value("pinned_in_flight", false)) {
                saw_pinned_event = true;
            }
        });
        auto view_lease = omnidesk::DsoViewLeaseRegistry::instance().acquireLease("plugin-scaffold-smoke");
        TEST_CHECK(view_lease != nullptr);
        TEST_CHECK(mgr.unloadPlugin("plugin-scaffold-smoke"));
        TEST_CHECK(saw_pinned_event);
        TEST_CHECK(omnidesk::DsoViewLeaseRegistry::instance().hasActiveLeases("plugin-scaffold-smoke"));
        view_lease.reset();
        TEST_CHECK(!omnidesk::DsoViewLeaseRegistry::instance().hasActiveLeases("plugin-scaffold-smoke"));
        bus.unsubscribe(ev_tok);
    }
    mgr.unloadAll();

    std::string rm_err;
    TEST_CHECK(installer.removePlugin("plugin-sample-echo", rm_err));
    TEST_CHECK(installer.removePlugin("plugin-scaffold-smoke", rm_err));
    TEST_CHECK(!installer.getInstalled("plugin-sample-echo").has_value());

    // 7. C++ ABI drift detection (legacy 56-byte & signature mismatch rejected BEFORE create()) + batch rebuild recovery
    {
        const fs::path stale_src = tmp_root / "stale-plugin-src";
        omnidesk::pm::ScaffoldOptions sopts_stale;
        sopts_stale.target_dir = stale_src.string();
        sopts_stale.plugin_id = "plugin-stale-test";
        sopts_stale.name = "Stale ABI Test Plugin";
        sopts_stale.provides = {"stale.ping.v1"};
        sopts_stale.init_git = false;
        auto s_res = omnidesk::pm::scaffoldPluginRepo(sopts_stale);
        TEST_CHECK(s_res.ok);

        auto inst_stale = installer.installFromLocal(stale_src.string());
        TEST_CHECK(inst_stale.ok);
        auto rec_stale = installer.getInstalled("plugin-stale-test");
        TEST_CHECK(rec_stale.has_value());
        TEST_CHECK(!installer.hasStaleInstalledPlugins());

        const fs::path shadow_include = stale_src / "sdk-shadow" / "omnidesk";
        fs::create_directories(shadow_include, ec);
        {
            std::ofstream header(shadow_include / "abi_build_signature.h");
            header << "#pragma once\n#define OMNIDESK_SDK_HEADER_SIGNATURE \"previous-sdk-headers\"\n";
        }
        const fs::path stale_cmake = stale_src / "CMakeLists.txt";
        std::ifstream cmake_input(stale_cmake);
        const std::string original_cmake((std::istreambuf_iterator<char>(cmake_input)), {});
        cmake_input.close();
        {
            std::ofstream cmake_output(stale_cmake, std::ios::app);
            cmake_output << "\ntarget_include_directories(plugin_stale_test BEFORE PRIVATE "
                            "\"${CMAKE_CURRENT_SOURCE_DIR}/sdk-shadow\")\n";
        }
        auto stale_headers = installer.installFromLocal(stale_src.string());
        TEST_CHECK(!stale_headers.ok && stale_headers.error.find("signature") != std::string::npos);
        {
            std::ofstream cmake_output(stale_cmake);
            cmake_output << original_cmake;
        }
        TEST_CHECK(!installer.hasStaleInstalledPlugins());

        // Compile 2 poisoned .so variants:
        // (1) Legacy 56-byte OmniDeskPluginInfo (no sdk_abi_signature field)
        // (2) Full-size struct with stale sdk_abi_signature
        // Both fail cleanly BEFORE omnidesk_plugin_create() is ever invoked.
        const fs::path marker_legacy = tmp_root / "legacy56_create_called.marker";
        const fs::path marker_badsig = tmp_root / "badsig_create_called.marker";
        const fs::path legacy_cpp = tmp_root / "legacy56.cpp";
        const fs::path legacy_so = tmp_root / "liblegacy56.so";
        const fs::path badsig_cpp = tmp_root / "badsig.cpp";
        const fs::path badsig_so = tmp_root / "libbadsig.so";

        {
            std::ofstream ofs(legacy_cpp);
            ofs << "#include <cstdint>\n#include <cstdlib>\n#include <fstream>\n"
                   "struct LegacyInfo56 {\n"
                   "  uint32_t abi_version;\n  uint32_t struct_size;\n"
                   "  const char* id;\n  const char* name;\n  const char* version;\n"
                   "  const char* kind;\n  const char* domain;\n  const char* sdk_version;\n"
                   "};\n"
                   "extern \"C\" __attribute__((visibility(\"default\"))) const LegacyInfo56* omnidesk_plugin_abi_info() {\n"
                   "  static const LegacyInfo56 k = {1u, static_cast<uint32_t>(sizeof(LegacyInfo56)),\n"
                   "    \"plugin-stale-test\", \"Stale\", \"0.1.0\", \"provider\", \"core\", \"0.0.9\"};\n"
                   "  return &k;\n"
                   "}\n"
                   "extern \"C\" __attribute__((visibility(\"default\"))) void* omnidesk_plugin_create() {\n"
                   "  std::ofstream m(\"" << marker_legacy.string() << "\"); m << \"called\\n\"; std::abort();\n"
                   "}\n"
                   "extern \"C\" __attribute__((visibility(\"default\"))) void omnidesk_plugin_destroy(void*) {}\n";
        }
        {
            std::ofstream ofs(badsig_cpp);
            ofs << "#include <cstdint>\n#include <cstdlib>\n#include <fstream>\n"
                   "struct FullInfo {\n"
                   "  uint32_t abi_version;\n  uint32_t struct_size;\n"
                   "  const char* id;\n  const char* name;\n  const char* version;\n"
                   "  const char* kind;\n  const char* domain;\n  const char* sdk_version;\n"
                   "  const char* sdk_abi_signature;\n"
                   "};\n"
                   "extern \"C\" __attribute__((visibility(\"default\"))) const FullInfo* omnidesk_plugin_abi_info() {\n"
                   "  static const FullInfo k = {1u, static_cast<uint32_t>(sizeof(FullInfo)),\n"
                   "    \"plugin-stale-test\", \"Stale\", \"0.1.0\", \"provider\", \"core\", \"0.1.0\", \"abi1-v0.0.0-deadbeefcafebabe\"};\n"
                   "  return &k;\n"
                   "}\n"
                   "extern \"C\" __attribute__((visibility(\"default\"))) void* omnidesk_plugin_create() {\n"
                   "  std::ofstream m(\"" << marker_badsig.string() << "\"); m << \"called\\n\"; std::abort();\n"
                   "}\n"
                   "extern \"C\" __attribute__((visibility(\"default\"))) void omnidesk_plugin_destroy(void*) {}\n";
        }
        TEST_CHECK(runShell("c++ -shared -fPIC -std=c++17 '" + legacy_cpp.string() + "' -o '" + legacy_so.string() + "'") == 0);
        TEST_CHECK(runShell("c++ -shared -fPIC -std=c++17 '" + badsig_cpp.string() + "' -o '" + badsig_so.string() + "'") == 0);

        std::string abi_err;
        nlohmann::json abi_info;
        TEST_CHECK(!omnidesk::pm::PluginInstaller::checkSoAbiInProcess(
            legacy_so.string(), "plugin-stale-test", abi_err, &abi_info));
        TEST_CHECK(!fs::exists(marker_legacy));
        TEST_CHECK(abi_err.find("older SDK") != std::string::npos ||
                   abi_err.find("struct_size") != std::string::npos);

        TEST_CHECK(!omnidesk::pm::PluginInstaller::checkSoAbiInProcess(
            badsig_so.string(), "plugin-stale-test", abi_err, &abi_info));
        TEST_CHECK(!fs::exists(marker_badsig));
        TEST_CHECK(abi_err.find("sdk_abi_signature") != std::string::npos ||
                   abi_err.find("mismatch") != std::string::npos);

        // Overwrite installed .so with legacy 56-byte binary -> installer + PluginManager + CLI detect drift cleanly
        fs::copy_file(legacy_so, rec_stale->so_path, fs::copy_options::overwrite_existing);
        TEST_CHECK(installer.hasStaleInstalledPlugins());
        auto abi_list = installer.checkInstalledAbiStatus();
        TEST_CHECK(abi_list.size() == 1 && !abi_list[0].compatible && abi_list[0].needs_rebuild);

        std::vector<nlohmann::json> drift_events;
        const auto drift_tok = bus.subscribe("plugin.abi_drift", "test",
            [&](const std::string&, const nlohmann::json& p) { drift_events.push_back(p); });
        std::string stale_load_err;
        TEST_CHECK(!mgr.loadPlugin("plugin-stale-test", stale_load_err));
        TEST_CHECK(!stale_load_err.empty());
        TEST_CHECK(!fs::exists(marker_legacy));
        TEST_CHECK(!drift_events.empty());
        bus.unsubscribe(drift_tok);

        const std::string cli_env =
            "OMNIDESK_DATA_HOME='" + data_dir + "' "
            "OMNIDESK_CONFIG_HOME='" + cfg_dir + "' "
            "OMNIDESK_SDK_CMAKE_DIR='" + std::string(OMNIDESK_TEST_SDK_CMAKE_DIR) + "' ";
        TEST_CHECK(runShell(cli_env + "'" + OMNIDESK_TEST_PM_EXE + "' abi-status --strict >/dev/null 2>&1") != 0);

        // Recover via API rebuildPlugins(true)
        auto batch_res = installer.rebuildPlugins(true);
        TEST_CHECK(batch_res.ok && batch_res.rebuilt_count == 1 && batch_res.failed_count == 0);
        TEST_CHECK(!installer.hasStaleInstalledPlugins());
        TEST_CHECK(mgr.loadPlugin("plugin-stale-test", load_err));
        TEST_CHECK(caps.has("stale.ping.v1"));
        mgr.unloadAll();

        // Also poison with bad_sig and recover via CLI `omnidesk-pm rebuild --stale`
        fs::copy_file(badsig_so, rec_stale->so_path, fs::copy_options::overwrite_existing);
        TEST_CHECK(installer.hasStaleInstalledPlugins());
        TEST_CHECK(runShell(cli_env + "'" + OMNIDESK_TEST_PM_EXE + "' rebuild --stale >/dev/null 2>&1") == 0);
        TEST_CHECK(runShell(cli_env + "'" + OMNIDESK_TEST_PM_EXE + "' abi-status --strict >/dev/null 2>&1") == 0);
        TEST_CHECK(!installer.hasStaleInstalledPlugins());
        TEST_CHECK(mgr.loadPlugin("plugin-stale-test", load_err));
        TEST_CHECK(!fs::exists(marker_badsig));
        mgr.unloadAll();
        TEST_CHECK(installer.removePlugin("plugin-stale-test", rm_err));

        const fs::path crash_cpp = tmp_root / "crash-probe.cpp";
        const fs::path crash_so = tmp_root / "libcrash-probe.so";
        {
            std::ofstream source(crash_cpp);
            source << "#include <csignal>\n"
                      "__attribute__((constructor)) static void crash() { std::raise(SIGSEGV); }\n";
        }
        TEST_CHECK(runShell("c++ -shared -fPIC '" + crash_cpp.string() + "' -o '" + crash_so.string() + "'") == 0);
        TEST_CHECK(installer.installFromLocal(stale_src.string()).ok);
        auto probe_rec = installer.getInstalled("plugin-stale-test");
        TEST_CHECK(probe_rec.has_value());
        fs::copy_file(crash_so, probe_rec->so_path, fs::copy_options::overwrite_existing);
        TEST_CHECK(installer.setEnabled("plugin-stale-test", false));
        auto probe_statuses = installer.checkInstalledAbiStatus();
        TEST_CHECK(probe_statuses.size() == 1 && probe_statuses[0].needs_rebuild);
        TEST_CHECK(probe_statuses[0].reason.find("signal") != std::string::npos);
        TEST_CHECK(!probe_statuses[0].enabled);
        auto repaired_probe = installer.rebuildPlugins(true);
        TEST_CHECK(repaired_probe.ok && repaired_probe.rebuilt_count == 1);
        TEST_CHECK(!installer.getInstalled("plugin-stale-test")->enabled);
        TEST_CHECK(installer.removePlugin("plugin-stale-test", rm_err));
    }

    // 8. Subprocess isolation ("isolation": "subprocess") over pipe & unix_socket + Domain proxies + SIGSEGV survival & auto-restart
    {
        const fs::path iso_src = tmp_root / "isolated-plugin-src";
        fs::create_directories(iso_src / "src", ec);
        auto writeIsoManifest = [&](const std::string& transport) {
            nlohmann::json mj = {
                {"schema_version", 1},
                {"id", "plugin-isolated-test"},
                {"name", "Isolated Subprocess Test"},
                {"version", "0.1.0"},
                {"abi_version", 1},
                {"kind", "hybrid"},
                {"domain", "media"},
                {"isolation", "subprocess"},
                {"worker_transport", transport},
                {"repository", {{"type", "git"}, {"url", "https://example.invalid/iso.git"}, {"branch", "main"}}},
                {"build", {{"system", "cmake"}, {"entry_target", "plugin_isolated_test"}, {"output_lib", "libplugin_isolated_test.so"}}},
                {"provides", {"isolated.echo.v1", "media.source.v1", "text.source.v1", "book.source.v1"}},
                {"consumes", nlohmann::json::array()},
                {"permissions", {"config:read"}},
            };
            std::ofstream ofs(iso_src / "plugin.json");
            ofs << mj.dump(2) << "\n";
        };
        writeIsoManifest("pipe");
        {
            std::ofstream ofs(iso_src / "CMakeLists.txt");
            ofs << "cmake_minimum_required(VERSION 3.20)\n"
                   "project(plugin_isolated_test LANGUAGES CXX)\n"
                   "set(CMAKE_CXX_STANDARD 17)\n"
                   "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
                   "set(CMAKE_POSITION_INDEPENDENT_CODE ON)\n"
                   "find_package(OmniDeskSDK REQUIRED)\n"
                   "add_library(plugin_isolated_test SHARED src/plugin_main.cpp)\n"
                   "target_link_libraries(plugin_isolated_test PRIVATE OmniDesk::SDK)\n"
                   "set_target_properties(plugin_isolated_test PROPERTIES OUTPUT_NAME \"plugin_isolated_test\" PREFIX \"lib\")\n";
        }
        {
            std::ofstream ofs(iso_src / "src" / "plugin_main.cpp");
            ofs << R"CPP(
#include <csignal>
#include <filesystem>
#include <fstream>
#include <chrono>
#include <thread>
#include <unistd.h>
#include "omnidesk/sdk.hpp"

class IsoMedia final : public omnidesk::IMediaSource {
public:
    std::string sourceId() const override { return "iso-media"; }
    std::string displayName() const override { return "Isolated Media"; }
    std::vector<std::string> categories() const override { return {"all", "sci-fi"}; }
    std::vector<omnidesk::MediaItem> fetchItems(const std::string& category, const std::string& query) override {
        omnidesk::MediaItem item;
        item.id = "m1";
        item.source_id = "iso-media";
        item.title = category + ":" + query;
        item.kind = "video";
        item.duration_sec = 90.0;
        item.has_bilingual_sub = true;
        return {item};
    }
    std::vector<omnidesk::LyricLine> fetchLyrics(const std::string&) override {
        return {{1.5, "line-orig", "line-trans"}};
    }
    std::vector<omnidesk::ChapterSegment> fetchChapters(const std::string&) override {
        return {{0.0, 15.0, "Ch1", "Intro", "content"}};
    }
};

class IsoText final : public omnidesk::ITextSource {
public:
    std::string sourceId() const override { return "iso-text"; }
    std::string displayName() const override { return "Isolated Text"; }
    std::string category() const override { return "journal"; }
    std::vector<omnidesk::TextFeedItem> fetchFeed(const std::string& query) override {
        if (std::getenv("OMNIDESK_TEST_SLOW_DOMAIN")) {
            std::this_thread::sleep_for(std::chrono::seconds(30));
        }
        omnidesk::TextFeedItem t;
        t.id = "t1";
        t.source_id = "iso-text";
        t.source_name = "IsoJournal";
        t.title = "Feed:" + query;
        return {t};
    }
    omnidesk::TextArticle fetchArticle(const std::string& article_id) override {
        omnidesk::TextArticle a;
        a.id = article_id;
        a.title = "Article:" + article_id;
        a.paragraphs.push_back({"p1", "paragraph", "En", "Zh"});
        return a;
    }
};

class IsoBook final : public omnidesk::IBookSource {
public:
    std::string sourceId() const override { return "iso-book"; }
    std::string displayName() const override { return "Isolated Book"; }
    std::vector<omnidesk::BookItem> listBooks(const std::string&, const std::string& query) override {
        omnidesk::BookItem b;
        b.id = "b1";
        b.source_id = "iso-book";
        b.title = "Book:" + query;
        b.total_pages = 12;
        return {b};
    }
    omnidesk::BookSpread fetchSpread(const std::string& book_id, int page_number) override {
        omnidesk::BookSpread s;
        s.book_id = book_id;
        s.page_number = page_number;
        s.total_pages = 12;
        s.blocks.push_back({"blk", "paragraph", "Original", "Translated", {"word"}});
        return s;
    }
    std::vector<omnidesk::VocabCard> listVocab() override {
        omnidesk::VocabCard v;
        v.word = "isolated";
        v.meaning = "隔离的";
        return {v};
    }
};

class IsolatedTestPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx_ = &ctx;
        ctx.registerStreamingCapability(
            "isolated.echo.v1",
            [this](const nlohmann::json& req,
                   const std::shared_ptr<omnidesk::CancelToken>&,
                   const omnidesk::StreamCallback& on_chunk) -> omnidesk::CapabilityResult {
                if (req.value("crash", false)) {
                    const std::string marker = req.value("marker", "");
                    if (!marker.empty() && !std::filesystem::exists(marker)) {
                        std::ofstream ofs(marker);
                        ofs << "1\n";
                        ofs.flush();
                        std::raise(SIGSEGV);
                    }
                }
                if (req.value("stream", false) && on_chunk) {
                    on_chunk({"iso-", nlohmann::json::object(), "delta", false});
                    on_chunk({"stream", nlohmann::json::object(), "delta", false});
                }
                return {true,
                        {{"worker_pid", static_cast<int>(::getpid())},
                         {"recovered", true},
                         {"echo", req}},
                        "", ""};
            });
        ctx.registerMediaSource(std::make_shared<IsoMedia>());
        ctx.registerTextSource(std::make_shared<IsoText>());
        ctx.registerBookSource(std::make_shared<IsoBook>());
        return true;
    }
    void onUnload() override { ctx_ = nullptr; }
private:
    omnidesk::PluginContext* ctx_ = nullptr;
};

OMNIDESK_DECLARE_PLUGIN(
    IsolatedTestPlugin,
    "plugin-isolated-test",
    "Isolated Subprocess Test",
    "0.1.0",
    "hybrid",
    "media")
)CPP";
        }

        auto iso_inst = installer.installFromLocal(iso_src.string());
        if (!iso_inst.ok) {
            std::cerr << "Isolated install failed: " << iso_inst.error << "\n" << iso_inst.log << "\n";
            return 1;
        }
        auto iso_rec = installer.getInstalled("plugin-isolated-test");
        TEST_CHECK(iso_rec.has_value());
        TEST_CHECK(iso_rec->manifest.isolation == omnidesk::pm::PluginIsolation::Subprocess);
        TEST_CHECK(iso_rec->manifest.worker_transport == "pipe");

        omnidesk::JobCenter iso_jobs(&bus, 2);
        omnidesk::WorkerSupervisor iso_workers;
        std::vector<omnidesk::WorkerFaultEvent> wf_events;
        std::mutex wf_mu;
        iso_workers.setFaultCallback([&](const omnidesk::WorkerFaultEvent& ev) {
            {
                std::lock_guard<std::mutex> lk(wf_mu);
                wf_events.push_back(ev);
            }
            bus.publish("worker.fault", {{"worker_id", ev.worker_id}, {"signal_name", ev.signal_name}});
        });
        std::atomic<int> bus_wf_count{0};
        const auto wf_tok = bus.subscribe("worker.fault", "test",
            [&](const std::string&, const nlohmann::json&) { bus_wf_count.fetch_add(1); });

        omnidesk::pm::PluginManager iso_mgr(
            installer, caps, bus, domains, cfg, secrets, &iso_jobs, &iso_workers);
        iso_mgr.setWorkerExecutable(OMNIDESK_TEST_WORKER_EXE);

        std::string iso_err;
        if (!iso_mgr.loadPlugin("plugin-isolated-test", iso_err)) {
            std::cerr << "Isolated loadPlugin failed: " << iso_err << "\n";
            return 1;
        }

        // Verify .so is NOT loaded into the host process address space!
        TEST_CHECK(::dlopen(iso_rec->so_path.c_str(), RTLD_NOLOAD | RTLD_LOCAL) == nullptr);

        // Capability RPC + streaming + all 3 domain proxies over pipe IPC
        auto r_pipe = caps.call("isolated.echo.v1", {{"hello", "pipe"}});
        TEST_CHECK(r_pipe.ok);
        const int worker_pid_1 = r_pipe.data["worker_pid"].get<int>();
        TEST_CHECK(worker_pid_1 > 0 && worker_pid_1 != static_cast<int>(::getpid()));

        std::string stream_out;
        auto s_pipe = caps.callStream("isolated.echo.v1", {{"stream", true}},
            [&](const omnidesk::StreamChunk& ch) { stream_out += ch.delta; });
        TEST_CHECK(s_pipe.ok && stream_out == "iso-stream");

        auto m_srcs = domains.mediaSources();
        TEST_CHECK(m_srcs.size() == 1 && m_srcs[0]->sourceId() == "iso-media");
        auto m_items = m_srcs[0]->fetchItems("sci-fi", "dune");
        TEST_CHECK(m_items.size() == 1 && m_items[0].title == "sci-fi:dune");
        TEST_CHECK(m_srcs[0]->fetchLyrics("m1").size() == 1);
        TEST_CHECK(m_srcs[0]->fetchChapters("m1").size() == 1);

        auto t_srcs = domains.textSources();
        TEST_CHECK(t_srcs.size() == 1 && t_srcs[0]->fetchFeed("abi").size() == 1);
        TEST_CHECK(t_srcs[0]->fetchArticle("a42").title == "Article:a42");

        auto b_srcs = domains.bookSources();
        TEST_CHECK(b_srcs.size() == 1 && b_srcs[0]->listBooks("", "rust").size() == 1);
        TEST_CHECK(b_srcs[0]->fetchSpread("b1", 5).blocks.size() == 1);
        TEST_CHECK(b_srcs[0]->listVocab().size() == 1);

        // Trigger SIGSEGV inside worker .so -> host survives, WorkerFaultEvent emitted, worker auto-restarts!
        const fs::path segv_marker = tmp_root / "iso_segv.marker";
        auto crash_res = caps.call(
            "isolated.echo.v1", {{"crash", true}, {"marker", segv_marker.string()}});
        TEST_CHECK(fs::exists(segv_marker));
        TEST_CHECK(!crash_res.ok);
        auto recovered_res = caps.call("isolated.echo.v1", {{"hello", "after-crash"}});
        TEST_CHECK(recovered_res.ok);
        TEST_CHECK(recovered_res.data.value("recovered", false));
        const int worker_pid_2 = recovered_res.data["worker_pid"].get<int>();
        TEST_CHECK(worker_pid_2 > 0 && worker_pid_2 != worker_pid_1);
        TEST_CHECK(bus_wf_count.load() >= 1);
        {
            std::lock_guard<std::mutex> lk(wf_mu);
            bool saw_segv = false;
            for (const auto& ev : wf_events) {
                if (ev.plugin_id == "plugin-isolated-test" && ev.signal_number == SIGSEGV &&
                    ev.auto_restarted && ev.restart_succeeded) {
                    saw_segv = true;
                }
            }
            TEST_CHECK(saw_segv);
        }

        // Now switch worker_transport to "unix_socket", reinstall & reload, and verify AF_UNIX IPC
        writeIsoManifest("unix_socket");
        TEST_CHECK(installer.installFromLocal(iso_src.string()).ok);
        TEST_CHECK(iso_mgr.reloadPlugin("plugin-isolated-test", iso_err));
        auto wst_u = iso_workers.status("plugin:plugin-isolated-test");
        TEST_CHECK(wst_u.has_value() && wst_u->transport == omnidesk::WorkerTransport::UnixSocket);
        auto r_unix = caps.call("isolated.echo.v1", {{"hello", "unix"}});
        TEST_CHECK(r_unix.ok && r_unix.data["echo"]["hello"] == "unix");
        TEST_CHECK(domains.mediaSources()[0]->fetchItems("all", "ux").size() == 1);

        if (std::getenv("OMNIDESK_TEST_GUI")) {
            auto gui = omnidesk::pm::runProcess(
                {OMNIDESK_TEST_HOST_EXE, "--smoke-ms", "700", "--domain", "text"}, "",
                {{"OMNIDESK_DATA_HOME", data_dir}, {"OMNIDESK_CONFIG_HOME", cfg_dir},
                 {"OMNIDESK_TEST_SLOW_DOMAIN", "1"},
                 {"OMNIDESK_SDK_CMAKE_DIR", OMNIDESK_TEST_SDK_CMAKE_DIR}},
                8000);
            if (gui.exit_code != 0) std::cerr << gui.output << "\n" << gui.error << "\n";
            TEST_CHECK(gui.exit_code == 0 && !gui.timed_out);
        }

        iso_mgr.unloadAll();
        bus.unsubscribe(wf_tok);
        iso_workers.stopAll();
        iso_jobs.shutdown();
        TEST_CHECK(installer.removePlugin("plugin-isolated-test", rm_err));
    }

    // 9. In-process throwing onLoad & throwing capability contained with "plugin.fault" events
    {
        const fs::path throw_src = tmp_root / "throwing-inproc-src";
        fs::create_directories(throw_src / "src", ec);
        {
            nlohmann::json mj = {
                {"schema_version", 1},
                {"id", "plugin-throwing-inproc"},
                {"name", "Throwing InProcess Plugin"},
                {"version", "0.1.0"},
                {"abi_version", 1},
                {"kind", "provider"},
                {"domain", "core"},
                {"isolation", "in_process"},
                {"repository", {{"type", "git"}, {"url", "https://example.invalid/throw.git"}, {"branch", "main"}}},
                {"build", {{"system", "cmake"}, {"entry_target", "plugin_throwing_inproc"}, {"output_lib", "libplugin_throwing_inproc.so"}}},
                {"provides", {"throw.cap.v1"}},
                {"consumes", nlohmann::json::array()},
            };
            std::ofstream ofs(throw_src / "plugin.json");
            ofs << mj.dump(2) << "\n";
        }
        {
            std::ofstream ofs(throw_src / "CMakeLists.txt");
            ofs << "cmake_minimum_required(VERSION 3.20)\n"
                   "project(plugin_throwing_inproc LANGUAGES CXX)\n"
                   "set(CMAKE_CXX_STANDARD 17)\n"
                   "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
                   "set(CMAKE_POSITION_INDEPENDENT_CODE ON)\n"
                   "find_package(OmniDeskSDK REQUIRED)\n"
                   "add_library(plugin_throwing_inproc SHARED src/plugin_main.cpp)\n"
                   "target_link_libraries(plugin_throwing_inproc PRIVATE OmniDesk::SDK)\n"
                   "set_target_properties(plugin_throwing_inproc PROPERTIES OUTPUT_NAME \"plugin_throwing_inproc\" PREFIX \"lib\")\n";
        }
        {
            std::ofstream ofs(throw_src / "src" / "plugin_main.cpp");
            ofs << R"CPP(
#include <stdexcept>
#include "omnidesk/sdk.hpp"

class ThrowingInprocPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx.registerCapability("throw.canary.v1", [](const nlohmann::json&) -> omnidesk::CapabilityResult {
            return {true, {}, "", ""};
        });
        if (ctx.loadConfig().value("throw_onload", true)) {
            throw std::runtime_error("boom in onLoad");
        }
        ctx.registerCapability("throw.cap.v1", [](const nlohmann::json&) -> omnidesk::CapabilityResult {
            throw std::runtime_error("boom in cap");
        });
        return true;
    }
    void onUnload() override {}
};

OMNIDESK_DECLARE_PLUGIN(
    ThrowingInprocPlugin,
    "plugin-throwing-inproc",
    "Throwing InProcess Plugin",
    "0.1.0",
    "provider",
    "core")
)CPP";
        }

        auto throwing_install = installer.installFromLocal(throw_src.string());
        if (!throwing_install.ok) {
            std::cerr << throwing_install.error << "\n" << throwing_install.log << "\n";
        }
        TEST_CHECK(throwing_install.ok);
        std::vector<nlohmann::json> plugin_faults;
        const auto pf_tok = bus.subscribe("plugin.fault", "test",
            [&](const std::string&, const nlohmann::json& p) { plugin_faults.push_back(p); });
        caps.setFaultHandler([&](const std::string& p, const std::string& c, const std::string& e) {
            bus.publish("plugin.fault", {{"plugin_id", p}, {"component", "capability:" + c}, {"error", e}});
        });

        std::string th_err;
        TEST_CHECK(!mgr.loadPlugin("plugin-throwing-inproc", th_err));
        TEST_CHECK(th_err.find("boom in onLoad") != std::string::npos);
        TEST_CHECK(!caps.has("throw.canary.v1"));
        TEST_CHECK(!plugin_faults.empty());

        cfg.setPluginValue("plugin-throwing-inproc", "throw_onload", false);
        TEST_CHECK(mgr.loadPlugin("plugin-throwing-inproc", th_err));
        auto bad_cap = caps.call("throw.cap.v1", {});
        TEST_CHECK(!bad_cap.ok && bad_cap.error.find("boom in cap") != std::string::npos);
        TEST_CHECK(plugin_faults.size() >= 2);

        mgr.unloadAll();
        bus.unsubscribe(pf_tok);
        TEST_CHECK(installer.removePlugin("plugin-throwing-inproc", rm_err));
    }

    // 10. Uncooperative slow in_process plugin: bounded shutdown + DSO pinning (zero hang & zero SIGSEGV)
    {
        const fs::path slow_src = tmp_root / "slow-inproc-src";
        fs::create_directories(slow_src / "src", ec);
        {
            nlohmann::json mj = {
                {"schema_version", 1},
                {"id", "plugin-slow-inproc"},
                {"name", "Slow InProcess Plugin"},
                {"version", "0.1.0"},
                {"abi_version", 1},
                {"kind", "source"},
                {"domain", "text"},
                {"isolation", "in_process"},
                {"repository", {{"type", "git"}, {"url", "https://example.invalid/slow.git"}, {"branch", "main"}}},
                {"build", {{"system", "cmake"}, {"entry_target", "plugin_slow_inproc"}, {"output_lib", "libplugin_slow_inproc.so"}}},
                {"provides", {"text.source.v1"}},
                {"consumes", nlohmann::json::array()},
            };
            std::ofstream ofs(slow_src / "plugin.json");
            ofs << mj.dump(2) << "\n";
        }
        {
            std::ofstream ofs(slow_src / "CMakeLists.txt");
            ofs << "cmake_minimum_required(VERSION 3.20)\n"
                   "project(plugin_slow_inproc LANGUAGES CXX)\n"
                   "set(CMAKE_CXX_STANDARD 17)\n"
                   "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
                   "set(CMAKE_POSITION_INDEPENDENT_CODE ON)\n"
                   "find_package(OmniDeskSDK REQUIRED)\n"
                   "add_library(plugin_slow_inproc SHARED src/plugin_main.cpp)\n"
                   "target_link_libraries(plugin_slow_inproc PRIVATE OmniDesk::SDK)\n"
                   "set_target_properties(plugin_slow_inproc PROPERTIES OUTPUT_NAME \"plugin_slow_inproc\" PREFIX \"lib\")\n";
        }
        {
            std::ofstream ofs(slow_src / "src" / "plugin_main.cpp");
            ofs << R"CPP(
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <string>
#include <thread>
#include "omnidesk/sdk.hpp"

class SlowTextSource final : public omnidesk::ITextSource {
public:
    std::string sourceId() const override { return "slow-inproc-text"; }
    std::string displayName() const override { return "Slow InProcess Text"; }
    std::string category() const override { return "journal"; }
    std::vector<omnidesk::TextFeedItem> fetchFeed(const std::string&) override {
        if (const char* marker = std::getenv("OMNIDESK_TEST_SLOW_STARTED_FILE")) {
            std::ofstream ofs(marker);
            ofs << "started\n";
        }
        int ms = 300;
        if (const char* env = std::getenv("OMNIDESK_TEST_SLOW_MS")) {
            ms = std::stoi(env);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(ms));
        if (const char* marker = std::getenv("OMNIDESK_TEST_SLOW_DONE_FILE")) {
            std::ofstream ofs(marker);
            ofs << "done\n";
        }
        return {};
    }
    omnidesk::TextArticle fetchArticle(const std::string& id) override {
        omnidesk::TextArticle a;
        a.id = id;
        return a;
    }
};

class SlowInprocPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx.registerTextSource(std::make_shared<SlowTextSource>());
        return true;
    }
    void onUnload() override {}
};

OMNIDESK_DECLARE_PLUGIN(
    SlowInprocPlugin,
    "plugin-slow-inproc",
    "Slow InProcess Plugin",
    "0.1.0",
    "source",
    "text")
)CPP";
        }

        auto slow_install = installer.installFromLocal(slow_src.string());
        if (!slow_install.ok) {
            std::cerr << slow_install.error << "\n" << slow_install.log << "\n";
        }
        TEST_CHECK(slow_install.ok);

        const fs::path started_marker = tmp_root / "slow_inproc.started";
        const fs::path done_marker = tmp_root / "slow_inproc.done";
        ::setenv("OMNIDESK_TEST_SLOW_STARTED_FILE", started_marker.c_str(), 1);
        ::setenv("OMNIDESK_TEST_SLOW_DONE_FILE", done_marker.c_str(), 1);
        ::setenv("OMNIDESK_TEST_SLOW_MS", "350", 1);
        ::setenv("OMNIDESK_SHUTDOWN_GRACE_MS", "80", 1);

        {
            omnidesk::JobCenter slow_jobs(&bus, 1);
            omnidesk::pm::PluginManager slow_mgr(
                installer, caps, bus, domains, cfg, secrets, &slow_jobs, nullptr);
            std::string sl_err;
            TEST_CHECK(slow_mgr.loadPlugin("plugin-slow-inproc", sl_err));
            auto t_srcs = domains.textSources();
            TEST_CHECK(t_srcs.size() == 1);
            slow_jobs.submit({"host-slow-read", "host.reader", "plugin-read", {}},
                [src = t_srcs[0]](omnidesk::JobHandle&, std::string&) {
                    src->fetchFeed("");
                    return true;
                });
            for (int i = 0; i < 200 && !fs::exists(started_marker); ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            TEST_CHECK(fs::exists(started_marker));

            const auto t0 = std::chrono::steady_clock::now();
            slow_jobs.shutdown(80);
            domains.shutdown(0);
            slow_mgr.unloadAll();
            const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t0).count();
            TEST_CHECK(elapsed_ms < 320);
        }
        for (int i = 0; i < 200 && !fs::exists(done_marker); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(fs::exists(done_marker));

        if (std::getenv("OMNIDESK_TEST_GUI")) {
            auto gui = omnidesk::pm::runProcess(
                {OMNIDESK_TEST_HOST_EXE, "--smoke-ms", "400", "--domain", "text"}, "",
                {{"OMNIDESK_DATA_HOME", data_dir},
                 {"OMNIDESK_CONFIG_HOME", cfg_dir},
                 {"OMNIDESK_TEST_SLOW_MS", "30000"},
                 {"OMNIDESK_SHUTDOWN_GRACE_MS", "300"},
                 {"OMNIDESK_SDK_CMAKE_DIR", OMNIDESK_TEST_SDK_CMAKE_DIR}},
                5000);
            if (gui.exit_code != 0) std::cerr << gui.output << "\n" << gui.error << "\n";
            TEST_CHECK(gui.exit_code == 0 && !gui.timed_out);
        }

        ::unsetenv("OMNIDESK_TEST_SLOW_STARTED_FILE");
        ::unsetenv("OMNIDESK_TEST_SLOW_DONE_FILE");
        ::unsetenv("OMNIDESK_TEST_SLOW_MS");
        ::unsetenv("OMNIDESK_SHUTDOWN_GRACE_MS");
        TEST_CHECK(installer.removePlugin("plugin-slow-inproc", rm_err));
    }

    // 10. Subprocess cross-plugin CapabilityRegistry & EntityBus forwarding (pipe & unix_socket,
    //     subprocess<->host<->subprocess, streaming/cancel, re-entrancy, and crash recovery)
    {
        auto buildSubprocessPlugin = [&](const std::string& id,
                                         const std::string& target_name,
                                         const std::string& transport,
                                         const std::string& cpp_src,
                                         const std::vector<std::string>& provides) {
            const fs::path pdir = tmp_root / (id + "-src");
            fs::create_directories(pdir / "src", ec);
            nlohmann::json mj = {
                {"schema_version", 1},
                {"id", id},
                {"name", id},
                {"version", "0.1.0"},
                {"abi_version", 1},
                {"kind", "hybrid"},
                {"domain", "core"},
                {"isolation", "subprocess"},
                {"worker_transport", transport},
                {"repository", {{"type", "git"}, {"url", "https://example.invalid/" + id + ".git"}, {"branch", "main"}}},
                {"build", {{"system", "cmake"}, {"entry_target", target_name}, {"output_lib", "lib" + target_name + ".so"}}},
                {"provides", provides},
                {"consumes", {"host.llm.v1", "beta.echo.v1", "alpha.leaf.v1"}},
            };
            {
                std::ofstream ofs(pdir / "plugin.json");
                ofs << mj.dump(2) << "\n";
            }
            {
                std::ofstream ofs(pdir / "CMakeLists.txt");
                ofs << "cmake_minimum_required(VERSION 3.20)\n"
                       "project(" << target_name << " LANGUAGES CXX)\n"
                       "set(CMAKE_CXX_STANDARD 17)\n"
                       "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
                       "set(CMAKE_POSITION_INDEPENDENT_CODE ON)\n"
                       "find_package(OmniDeskSDK REQUIRED)\n"
                       "add_library(" << target_name << " SHARED src/plugin_main.cpp)\n"
                       "target_link_libraries(" << target_name << " PRIVATE OmniDesk::SDK)\n"
                       "set_target_properties(" << target_name << " PROPERTIES OUTPUT_NAME \"" << target_name << "\" PREFIX \"lib\")\n";
            }
            {
                std::ofstream ofs(pdir / "src" / "plugin_main.cpp");
                ofs << cpp_src;
            }
            auto ir = installer.installFromLocal(pdir.string());
            if (!ir.ok) {
                std::cerr << "Install " << id << " failed: " << ir.error << "\n" << ir.log << "\n";
                std::exit(1);
            }
        };

        const std::string alpha_cpp = R"CPP(
#include <atomic>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <thread>
#include <unistd.h>
#include "omnidesk/sdk.hpp"

class AlphaPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx_ = &ctx;
        // Verify host capability query & call works right inside onLoad()!
        onload_saw_host_llm_ = ctx.capabilities().has("host.llm.v1");
        if (onload_saw_host_llm_) {
            auto r = ctx.capabilities().call("host.llm.v1", {{"prompt", "onload-probe"}});
            if (r.ok) onload_reply_ = r.data.value("reply", "");
        }

        ctx.subscribe("entity.alpha.ping", [this](const std::string& topic, const nlohmann::json& p) {
            std::lock_guard<std::mutex> lk(mu_);
            pings_.push_back({{"topic", topic}, {"seq", p.value("seq", 0)}});
        });
        ctx.subscribe("*", [this](const std::string& topic, const nlohmann::json& p) {
            if (topic == "entity.alpha.ping" || topic == "entity.alpha.out") {
                std::lock_guard<std::mutex> lk(mu_);
                wildcard_events_.push_back({{"topic", topic}, {"from", p.value("from", "")}});
            }
        });

        ctx.registerCapability("alpha.work.v1", [this](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            const std::string action = req.value("action", "");
            if (action == "status") {
                std::lock_guard<std::mutex> lk(mu_);
                return {true,
                        {{"pid", static_cast<int>(::getpid())},
                         {"onload_saw_host_llm", onload_saw_host_llm_},
                         {"onload_reply", onload_reply_},
                         {"pings", pings_},
                         {"wildcard_events", wildcard_events_}},
                        "", ""};
            }
            if (action == "call_host_and_beta") {
                auto llm = ctx_->capabilities().call("host.llm.v1", {{"prompt", "from-alpha"}});
                auto beta = ctx_->capabilities().call("beta.echo.v1", {{"msg", "alpha->beta"}});
                auto beta_explicit = ctx_->capabilities().callProvider(
                    "plugin-iso-beta", "beta.echo.v1", {{"msg", "explicit"}});
                auto provs = ctx_->capabilities().providersFor("host.llm.v1");
                auto all = ctx_->capabilities().listAll();
                return {llm.ok && beta.ok && beta_explicit.ok,
                        {{"llm_reply", llm.data.value("reply", "")},
                         {"llm_provider", llm.provider_plugin_id},
                         {"beta_echo", beta.data.value("echo", "")},
                         {"beta_provider", beta.provider_plugin_id},
                         {"beta_explicit", beta_explicit.data.value("echo", "")},
                         {"host_llm_providers", provs.size()},
                         {"total_caps", all.size()}},
                        llm.ok ? beta.error : llm.error, ""};
            }
            if (action == "stream_cancel_host") {
                auto cancel = omnidesk::CancelToken::create();
                std::string got;
                int chunks = 0;
                auto sres = ctx_->capabilities().callStream(
                    "host.llm.v1",
                    {{"stream", true}},
                    [&](const omnidesk::StreamChunk& ch) {
                        if (!ch.delta.empty()) {
                            got += ch.delta;
                            if (++chunks >= 2) cancel->cancel();
                        }
                    },
                    cancel);
                return {true,
                        {{"cancelled", sres.cancelled}, {"got", got}, {"chunks", chunks}},
                        "", ""};
            }
            if (action == "reenter") {
                return ctx_->capabilities().call("host.reenter.v1", {{"val", req.value("val", 0)}});
            }
            if (action == "publish_out") {
                size_t delivered = ctx_->publish("entity.alpha.out", {{"from", "alpha"}, {"n", req.value("n", 1)}});
                return {true, {{"delivered", delivered}}, "", ""};
            }
            if (action == "crash_once") {
                const std::string marker = req.value("marker", "");
                if (!marker.empty() && !std::filesystem::exists(marker)) {
                    std::ofstream ofs(marker);
                    ofs << "crashed\n";
                    ofs.flush();
                    std::raise(SIGSEGV);
                }
                return {true, {{"recovered", true}, {"pid", static_cast<int>(::getpid())}}, "", ""};
            }
            return {false, {}, "unknown action", ""};
        });

        ctx.registerCapability("alpha.leaf.v1", [](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            return {true, {{"leaf", req.value("val", 0) + 100}}, "", ""};
        });
        return true;
    }

    void onUnload() override { ctx_ = nullptr; }

private:
    omnidesk::PluginContext* ctx_ = nullptr;
    bool onload_saw_host_llm_ = false;
    std::string onload_reply_;
    std::mutex mu_;
    nlohmann::json pings_ = nlohmann::json::array();
    nlohmann::json wildcard_events_ = nlohmann::json::array();
};

OMNIDESK_DECLARE_PLUGIN(AlphaPlugin, "plugin-iso-alpha", "Alpha Iso", "0.1.0", "hybrid", "core")
)CPP";

        const std::string beta_cpp = R"CPP(
#include <mutex>
#include <unistd.h>
#include "omnidesk/sdk.hpp"

class BetaPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx_ = &ctx;
        ctx.subscribe("entity.alpha.out", [this](const std::string&, const nlohmann::json& p) {
            std::lock_guard<std::mutex> lk(mu_);
            alpha_outs_.push_back(p);
        });
        ctx.registerCapability("beta.echo.v1", [this](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            std::lock_guard<std::mutex> lk(mu_);
            return {true,
                    {{"echo", req.value("msg", "")},
                     {"pid", static_cast<int>(::getpid())},
                     {"alpha_outs", alpha_outs_}},
                    "", ""};
        });
        ctx.registerCapability("beta.call_alpha_leaf.v1", [this](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            return ctx_->capabilities().call("alpha.leaf.v1", req);
        });
        return true;
    }
    void onUnload() override { ctx_ = nullptr; }
private:
    omnidesk::PluginContext* ctx_ = nullptr;
    std::mutex mu_;
    nlohmann::json alpha_outs_ = nlohmann::json::array();
};

OMNIDESK_DECLARE_PLUGIN(BetaPlugin, "plugin-iso-beta", "Beta Iso", "0.1.0", "hybrid", "core")
)CPP";

        buildSubprocessPlugin("plugin-iso-alpha", "plugin_iso_alpha", "pipe", alpha_cpp,
                              {"alpha.work.v1", "alpha.leaf.v1"});
        buildSubprocessPlugin("plugin-iso-beta", "plugin_iso_beta", "unix_socket", beta_cpp,
                              {"beta.echo.v1", "beta.call_alpha_leaf.v1"});

        omnidesk::CapabilityRegistry fwd_caps;
        omnidesk::EntityBus fwd_bus;
        omnidesk::DomainRegistry fwd_domains;
        omnidesk::JobCenter fwd_jobs(&fwd_bus, 2);
        omnidesk::WorkerSupervisor fwd_workers;

        fwd_caps.registerStreamingCapability("host.llm.v1", "host.llm",
            [](const nlohmann::json& req,
               const std::shared_ptr<omnidesk::CancelToken>& cancel,
               const omnidesk::StreamCallback& on_chunk) -> omnidesk::CapabilityResult {
                if (req.value("stream", false)) {
                    for (int i = 0; i < 30; ++i) {
                        if (cancel && cancel->isCancelled()) {
                            return {false, {}, "cancelled", "host.llm", true};
                        }
                        if (on_chunk) on_chunk({"c" + std::to_string(i), nlohmann::json::object(), "delta", false});
                        std::this_thread::sleep_for(std::chrono::milliseconds(15));
                    }
                    return {true, {{"reply", "full-stream"}}, "", "host.llm"};
                }
                return {true, {{"reply", "ok:" + req.value("prompt", "")}}, "", "host.llm"};
            });
        fwd_caps.registerCapability("host.reenter.v1", "host.reenter",
            [&](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                return fwd_caps.call("alpha.leaf.v1", req);
            });

        omnidesk::pm::PluginManager fwd_mgr(
            installer, fwd_caps, fwd_bus, fwd_domains, cfg, secrets, &fwd_jobs, &fwd_workers);
        fwd_mgr.setWorkerExecutable(OMNIDESK_TEST_WORKER_EXE);

        std::string fwd_err;
        if (!fwd_mgr.loadPlugin("plugin-iso-beta", fwd_err)) {
            std::cerr << "Load beta failed: " << fwd_err << "\n";
            return 1;
        }
        if (!fwd_mgr.loadPlugin("plugin-iso-alpha", fwd_err)) {
            std::cerr << "Load alpha failed: " << fwd_err << "\n";
            return 1;
        }

        // Verify onLoad() forwarding + capability calls (Alpha->Host, Alpha->Beta, Beta->Alpha)
        auto st_init = fwd_caps.call("alpha.work.v1", {{"action", "status"}});
        TEST_CHECK(st_init.ok);
        TEST_CHECK(st_init.data.value("onload_saw_host_llm", false));
        TEST_CHECK(st_init.data.value("onload_reply", "") == "ok:onload-probe");
        const int alpha_pid_1 = st_init.data.value("pid", 0);
        TEST_CHECK(alpha_pid_1 > 0 && alpha_pid_1 != static_cast<int>(::getpid()));

        auto cross = fwd_caps.call("alpha.work.v1", {{"action", "call_host_and_beta"}});
        TEST_CHECK(cross.ok);
        TEST_CHECK(cross.data["llm_reply"] == "ok:from-alpha");
        TEST_CHECK(cross.data["llm_provider"] == "host.llm");
        TEST_CHECK(cross.data["beta_echo"] == "alpha->beta");
        TEST_CHECK(cross.data["beta_provider"] == "plugin-iso-beta");
        TEST_CHECK(cross.data["beta_explicit"] == "explicit");
        TEST_CHECK(cross.data["host_llm_providers"].get<int>() == 1);
        TEST_CHECK(cross.data["total_caps"].get<int>() >= 6);

        auto b_to_a = fwd_caps.call("beta.call_alpha_leaf.v1", {{"val", 23}});
        TEST_CHECK(b_to_a.ok && b_to_a.data["leaf"] == 123);

        // Streaming + mid-stream cancellation from subprocess plugin to host capability
        auto sc = fwd_caps.call("alpha.work.v1", {{"action", "stream_cancel_host"}});
        TEST_CHECK(sc.ok && sc.data["cancelled"] == true);
        TEST_CHECK(sc.data["chunks"].get<int>() >= 2 && sc.data["chunks"].get<int>() < 30);

        // Re-entrant call: Host -> Alpha("alpha.work.v1") -> Host("host.reenter.v1") -> Alpha("alpha.leaf.v1")
        auto re = fwd_caps.call("alpha.work.v1", {{"action", "reenter"}, {"val", 7}});
        TEST_CHECK(re.ok && re.data["leaf"] == 107);

        // EntityBus forwarding: Host -> Alpha (exact + wildcard -> 1 delivery each, no duplicates)
        // and Alpha -> Host & Beta (no echo back to Alpha wildcard!)
        int host_saw_alpha_out = 0;
        auto h_out_tok = fwd_bus.subscribe("entity.alpha.out", "host.observer",
            [&](const std::string&, const nlohmann::json& p) {
                if (p.value("n", 0) == 99) ++host_saw_alpha_out;
            });
        fwd_bus.publish("entity.alpha.ping", {{"seq", 1}});
        auto pub_res = fwd_caps.call("alpha.work.v1", {{"action", "publish_out"}, {"n", 99}});
        TEST_CHECK(pub_res.ok && pub_res.data["delivered"].get<int>() >= 2);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));

        auto st_bus = fwd_caps.call("alpha.work.v1", {{"action", "status"}});
        TEST_CHECK(st_bus.ok);
        TEST_CHECK(st_bus.data["pings"].size() == 1 && st_bus.data["pings"][0]["seq"] == 1);
        // Alpha's wildcard saw entity.alpha.ping (from Host) and entity.alpha.out (delivered locally once, NOT echoed from Host)
        TEST_CHECK(st_bus.data["wildcard_events"].size() == 2);
        TEST_CHECK(host_saw_alpha_out == 1);

        auto beta_st = fwd_caps.call("beta.echo.v1", {{"msg", "check"}});
        TEST_CHECK(beta_st.ok && beta_st.data["alpha_outs"].size() == 1);
        TEST_CHECK(beta_st.data["alpha_outs"][0]["n"] == 99);

        // Crash Alpha with SIGSEGV -> auto-restarts -> re-subscribes on EntityBus cleanly -> no duplicate events
        const fs::path alpha_segv = tmp_root / "alpha_segv.marker";
        auto crashed = fwd_caps.call("alpha.work.v1", {{"action", "crash_once"}, {"marker", alpha_segv.string()}});
        TEST_CHECK(!crashed.ok && fs::exists(alpha_segv));
        auto recov = fwd_caps.call("alpha.work.v1", {{"action", "crash_once"}, {"marker", alpha_segv.string()}});
        TEST_CHECK(recov.ok && recov.data.value("recovered", false));
        const int alpha_pid_2 = recov.data.value("pid", 0);
        TEST_CHECK(alpha_pid_2 > 0 && alpha_pid_2 != alpha_pid_1);

        // Host event still reaches restarted Alpha exactly once, and Alpha still calls Host & Beta
        fwd_bus.publish("entity.alpha.ping", {{"seq", 2}});
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        auto st_after_crash = fwd_caps.call("alpha.work.v1", {{"action", "status"}});
        TEST_CHECK(st_after_crash.ok);
        TEST_CHECK(st_after_crash.data["pings"].size() == 1 && st_after_crash.data["pings"][0]["seq"] == 2);
        auto cross_after = fwd_caps.call("alpha.work.v1", {{"action", "call_host_and_beta"}});
        TEST_CHECK(cross_after.ok && cross_after.data["beta_echo"] == "alpha->beta");

        fwd_bus.unsubscribe(h_out_tok);
        fwd_mgr.unloadAll();
        fwd_workers.stopAll();
        fwd_jobs.shutdown();
        TEST_CHECK(!fwd_caps.has("alpha.work.v1"));
        TEST_CHECK(!fwd_caps.has("beta.echo.v1"));
        TEST_CHECK(installer.removePlugin("plugin-iso-alpha", rm_err));
        TEST_CHECK(installer.removePlugin("plugin-iso-beta", rm_err));
    }

    // 11. Prebuilt .tar.zst symlink & hardlink escape rejection
    {
        const fs::path secret_vault = tmp_root / "secret_vault.json";
        {
            std::ofstream ofs(secret_vault);
            ofs << "{\"api_key\":\"SUPER_SECRET_DO_NOT_EXFILTRATE\"}\n";
        }

        // 11a. Symlink README.md -> secret_vault.json inside .tar.zst
        const fs::path sym_stage = tmp_root / "pkg-symlink-stage";
        fs::create_directories(sym_stage, ec);
        fs::copy_file(pkg_stage / "plugin.json", sym_stage / "plugin.json",
                      fs::copy_options::overwrite_existing);
        for (const auto& entry : fs::directory_iterator(pkg_stage)) {
            if (entry.path().extension() == ".so") {
                fs::copy_file(entry.path(), sym_stage / entry.path().filename(),
                              fs::copy_options::overwrite_existing);
            }
        }
        fs::create_symlink(secret_vault, sym_stage / "README.md", ec);
        TEST_CHECK(!ec);
        const fs::path sym_tar = tmp_root / "malicious-symlink.tar.zst";
        TEST_CHECK(runShell("tar --zstd -cf '" + sym_tar.string() + "' -C '" + sym_stage.string() + "' .") == 0);

        omnidesk::pm::ReleaseArtifactSpec sym_rel;
        sym_rel.archive_url = "file://" + sym_tar.string();
        sym_rel.sha256 = sha256File(sym_tar);
        sym_rel.tag = "v1.0.0";
        auto sym_res = installer.installFromPrebuilt(sym_rel);
        TEST_CHECK(!sym_res.ok);
        TEST_CHECK(sym_res.error.find("Symlink") != std::string::npos ||
                   sym_res.error.find("symlink") != std::string::npos);
        TEST_CHECK(!installer.getInstalled("plugin-sample-echo").has_value());
        TEST_CHECK(!fs::exists(fs::path(installer.pluginsDir()) / "plugin-sample-echo"));

        // 11b. Directory symlink workers -> outside directory inside .tar.zst
        const fs::path dirsym_stage = tmp_root / "pkg-dirsym-stage";
        fs::create_directories(dirsym_stage, ec);
        fs::copy_file(pkg_stage / "plugin.json", dirsym_stage / "plugin.json",
                      fs::copy_options::overwrite_existing);
        for (const auto& entry : fs::directory_iterator(pkg_stage)) {
            if (entry.path().extension() == ".so") {
                fs::copy_file(entry.path(), dirsym_stage / entry.path().filename(),
                              fs::copy_options::overwrite_existing);
            }
        }
        fs::create_directory_symlink(tmp_root, dirsym_stage / "workers", ec);
        TEST_CHECK(!ec);
        const fs::path dirsym_tar = tmp_root / "malicious-dirsym.tar.zst";
        TEST_CHECK(runShell("tar --zstd -cf '" + dirsym_tar.string() + "' -C '" + dirsym_stage.string() + "' .") == 0);

        omnidesk::pm::ReleaseArtifactSpec dirsym_rel;
        dirsym_rel.archive_url = "file://" + dirsym_tar.string();
        dirsym_rel.sha256 = sha256File(dirsym_tar);
        dirsym_rel.tag = "v1.0.0";
        auto dirsym_res = installer.installFromPrebuilt(dirsym_rel);
        TEST_CHECK(!dirsym_res.ok);
        TEST_CHECK(!installer.getInstalled("plugin-sample-echo").has_value());

        // 11c. Hardlink entry inside .tar.zst
        const fs::path raw_hard_tar = tmp_root / "malicious-hardlink.tar";
        const fs::path hard_tar_zst = tmp_root / "malicious-hardlink.tar.zst";
        const std::string py_hardlink =
            "python3 -c \"import os, tarfile; "
            "tf = tarfile.open('" + raw_hard_tar.string() + "', 'w'); "
            "tf.add('" + (pkg_stage / "plugin.json").string() + "', arcname='plugin.json'); "
            "so_files = [f for f in os.listdir('" + pkg_stage.string() + "') if f.endswith('.so')]; "
            "tf.add(os.path.join('" + pkg_stage.string() + "', so_files[0]), arcname=so_files[0]); "
            "ti = tarfile.TarInfo('README.md'); ti.type = tarfile.LNKTYPE; "
            "ti.linkname = '" + secret_vault.string() + "'; tf.addfile(ti); tf.close()\" && "
            "zstd -q -f '" + raw_hard_tar.string() + "' -o '" + hard_tar_zst.string() + "'";
        TEST_CHECK(runShell(py_hardlink) == 0);

        omnidesk::pm::ReleaseArtifactSpec hard_rel;
        hard_rel.archive_url = "file://" + hard_tar_zst.string();
        hard_rel.sha256 = sha256File(hard_tar_zst);
        hard_rel.tag = "v1.0.0";
        auto hard_res = installer.installFromPrebuilt(hard_rel);
        TEST_CHECK(!hard_res.ok);
        TEST_CHECK(hard_res.error.find("hardlink") != std::string::npos ||
                   hard_res.error.find("Hardlink") != std::string::npos ||
                   hard_res.error.find("Unsafe") != std::string::npos);
        TEST_CHECK(!installer.getInstalled("plugin-sample-echo").has_value());
    }

    // 12. scaffoldPluginRepo C++ string literal escaping + out-of-lock onHealthCheck / unload safety
    {
        const fs::path esc_dir = tmp_root / "scaffold-escape-repo";
        omnidesk::pm::ScaffoldOptions esc_opts;
        esc_opts.target_dir = esc_dir.string();
        esc_opts.plugin_id = "plugin-escape-smoke";
        esc_opts.name = "Name \"Quoted\" \\Slash\\ \nLine2\tTab\rCR";
        esc_opts.description = "Desc \"with quotes\", C:\\dir\\file, and\nsecond line\tend";
        esc_opts.author = "Dev \"Team\" \\ Core\nUnit";
        esc_opts.version = "0.1.0-rc\"1";
        esc_opts.provides = {"escape.ping.v1"};
        esc_opts.init_git = false;

        auto esc_sres = omnidesk::pm::scaffoldPluginRepo(esc_opts);
        TEST_CHECK(esc_sres.ok);

        std::ifstream cpp_ifs(esc_dir / "src" / "plugin_main.cpp");
        const std::string gen_cpp((std::istreambuf_iterator<char>(cpp_ifs)), {});
        TEST_CHECK(gen_cpp.find("Name \\\"Quoted\\\" \\\\Slash\\\\ \\nLine2\\tTab\\rCR") != std::string::npos);
        TEST_CHECK(gen_cpp.find("Desc \\\"with quotes\\\", C:\\\\dir\\\\file, and\\nsecond line\\tend") !=
                   std::string::npos);

        auto esc_inst = installer.installFromLocal(esc_sres.dir_path);
        if (!esc_inst.ok) {
            std::cerr << "Escaped scaffold installFromLocal failed: " << esc_inst.error
                      << "\nLOG:\n" << esc_inst.log << "\n";
            return 1;
        }
        TEST_CHECK(mgr.loadPlugin("plugin-escape-smoke", load_err));
        auto esc_provs = caps.providersFor("escape.ping.v1");
        TEST_CHECK(esc_provs.size() == 1);
        TEST_CHECK(esc_provs[0].metadata.value("name", "") == esc_opts.name);
        TEST_CHECK(esc_provs[0].metadata.value("description", "") == esc_opts.description);
        TEST_CHECK(esc_provs[0].metadata.value("author", "") == esc_opts.author);
        TEST_CHECK(esc_provs[0].metadata.value("version", "") == esc_opts.version);
        mgr.unloadAll();
        TEST_CHECK(installer.removePlugin("plugin-escape-smoke", rm_err));

        // Verify PluginManager::statusOf / statuses runs onHealthCheck() outside mutex_ (no re-entrant deadlock)
        // and waits for active health check before unloading.
        const fs::path hc_dir = tmp_root / "health-reentrant-src";
        omnidesk::pm::ScaffoldOptions hc_opts;
        hc_opts.target_dir = hc_dir.string();
        hc_opts.plugin_id = "plugin-health-reentrant";
        hc_opts.name = "Health Reentrant";
        hc_opts.provides = {"health.reentrant.v1"};
        hc_opts.init_git = false;
        TEST_CHECK(omnidesk::pm::scaffoldPluginRepo(hc_opts).ok);
        {
            std::ofstream ofs(hc_dir / "src" / "plugin_main.cpp", std::ios::trunc);
            ofs << R"CPP(
#include <atomic>
#include <chrono>
#include <thread>
#include <omnidesk/sdk.hpp>

class HealthReentrantPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx_ = &ctx;
        unloaded_.store(false);
        ctx.logInfo("health-reentrant loaded");
        return true;
    }
    void onUnload() override {
        unloaded_.store(true);
        ctx_ = nullptr;
    }
    nlohmann::json onHealthCheck() const override {
        if (ctx_) {
            ctx_->publish("test.health.entered", {{"plugin_id", "plugin-health-reentrant"}});
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(60));
        const bool still_alive = !unloaded_.load();
        return {{"ok", still_alive}};
    }
private:
    omnidesk::PluginContext* ctx_ = nullptr;
    mutable std::atomic<bool> unloaded_{false};
};

OMNIDESK_DECLARE_PLUGIN(HealthReentrantPlugin, "plugin-health-reentrant", "Health Reentrant", "0.1.0", "provider", "core")
)CPP";
        }
        TEST_CHECK(installer.installFromLocal(hc_dir.string()).ok);
        TEST_CHECK(mgr.loadPlugin("plugin-health-reentrant", load_err));

        std::atomic<bool> entered_hc{false};
        std::atomic<bool> reentrant_is_loaded{false};
        const auto hc_tok = bus.subscribe("test.health.entered", "test.reentrant",
            [&](const std::string&, const nlohmann::json&) {
                reentrant_is_loaded.store(mgr.isLoaded("plugin-health-reentrant"));
                entered_hc.store(true);
            });

        std::thread unloader([&]() {
            while (!entered_hc.load()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            mgr.unloadPlugin("plugin-health-reentrant");
        });
        auto hc_st = mgr.statusOf("plugin-health-reentrant");
        unloader.join();
        bus.unsubscribe(hc_tok);

        TEST_CHECK(reentrant_is_loaded.load());
        TEST_CHECK(hc_st.has_value() && hc_st->healthy);
        TEST_CHECK(!mgr.isLoaded("plugin-health-reentrant"));
        TEST_CHECK(installer.removePlugin("plugin-health-reentrant", rm_err));
    }

    // 13. Subprocess worker duplex unregistered cap.invoke fast-fail (no bounce) & graceful SIGTERM onUnload()
    {
        const fs::path guard_src = tmp_root / "plugin-iso-guard-src";
        fs::create_directories(guard_src / "src", ec);
        {
            nlohmann::json mj = {
                {"schema_version", 1},
                {"id", "plugin-iso-guard"},
                {"name", "Iso Guard"},
                {"version", "0.1.0"},
                {"abi_version", 1},
                {"kind", "provider"},
                {"domain", "core"},
                {"isolation", "subprocess"},
                {"worker_transport", "pipe"},
                {"repository", {{"type", "git"}, {"url", "https://example.invalid/guard.git"}, {"branch", "main"}}},
                {"build", {{"system", "cmake"}, {"entry_target", "plugin_iso_guard"}, {"output_lib", "libplugin_iso_guard.so"}}},
                {"provides", {"guard.info.v1"}},
                {"consumes", nlohmann::json::array()},
            };
            std::ofstream ofs(guard_src / "plugin.json");
            ofs << mj.dump(2) << "\n";
        }
        {
            std::ofstream ofs(guard_src / "CMakeLists.txt");
            ofs << "cmake_minimum_required(VERSION 3.20)\n"
                   "project(plugin_iso_guard LANGUAGES CXX)\n"
                   "set(CMAKE_CXX_STANDARD 17)\n"
                   "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
                   "set(CMAKE_POSITION_INDEPENDENT_CODE ON)\n"
                   "find_package(OmniDeskSDK REQUIRED)\n"
                   "add_library(plugin_iso_guard SHARED src/plugin_main.cpp)\n"
                   "target_link_libraries(plugin_iso_guard PRIVATE OmniDesk::SDK)\n"
                   "set_target_properties(plugin_iso_guard PROPERTIES OUTPUT_NAME \"plugin_iso_guard\" PREFIX \"lib\")\n";
        }
        {
            std::ofstream ofs(guard_src / "src" / "plugin_main.cpp");
            ofs << R"CPP(
#include <filesystem>
#include <fstream>
#include <unistd.h>
#include "omnidesk/sdk.hpp"

class IsoGuardPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        data_dir_ = ctx.dataDir();
        ctx.registerCapability("guard.info.v1", [this](const nlohmann::json&) -> omnidesk::CapabilityResult {
            return {true,
                    {{"pid", static_cast<int>(::getpid())},
                     {"data_dir", data_dir_}},
                    "", ""};
        });
        return true;
    }
    void onUnload() override {
        if (!data_dir_.empty()) {
            std::error_code ec;
            std::filesystem::create_directories(data_dir_, ec);
            const auto tmp_p = std::filesystem::path(data_dir_) / "unloaded.marker.tmp";
            const auto final_p = std::filesystem::path(data_dir_) / "unloaded.marker";
            {
                std::ofstream ofs(tmp_p, std::ios::trunc);
                ofs << static_cast<int>(::getpid()) << "\n";
            }
            std::filesystem::rename(tmp_p, final_p, ec);
        }
    }
private:
    std::string data_dir_;
};

OMNIDESK_DECLARE_PLUGIN(IsoGuardPlugin, "plugin-iso-guard", "Iso Guard", "0.1.0", "provider", "core")
)CPP";
        }

        auto guard_inst = installer.installFromLocal(guard_src.string());
        if (!guard_inst.ok) {
            std::cerr << "Install plugin-iso-guard failed: " << guard_inst.error << "\n" << guard_inst.log << "\n";
            return 1;
        }

        omnidesk::CapabilityRegistry guard_caps;
        omnidesk::EntityBus guard_bus;
        omnidesk::DomainRegistry guard_domains;
        omnidesk::JobCenter guard_jobs(&guard_bus, 2);
        omnidesk::WorkerSupervisor guard_workers;
        omnidesk::pm::PluginManager guard_mgr(
            installer, guard_caps, guard_bus, guard_domains, cfg, secrets, &guard_jobs, &guard_workers);
        guard_mgr.setWorkerExecutable(OMNIDESK_TEST_WORKER_EXE);

        std::string guard_err;
        TEST_CHECK(guard_mgr.loadPlugin("plugin-iso-guard", guard_err));

        // Register a ghost capability on the host routing into plugin:plugin-iso-guard that the worker did NOT register.
        // Without the bounce guard in cap_bridge->invoke, this would loop worker->host->worker until pool deadlock/timeout.
        guard_caps.registerStreamingCapability(
            "guard.ghost.v1",
            "plugin-iso-guard",
            omnidesk::makeWorkerStreamCapabilityHandler(
                guard_workers, "plugin:plugin-iso-guard", "guard.ghost.v1", "cap.invoke"));

        const auto t0 = std::chrono::steady_clock::now();
        auto ghost_sync = guard_caps.call("guard.ghost.v1", {{"probe", 1}});
        const auto elapsed_sync = std::chrono::steady_clock::now() - t0;
        TEST_CHECK(!ghost_sync.ok);
        TEST_CHECK(ghost_sync.error.find("missing capability") != std::string::npos);
        TEST_CHECK(elapsed_sync < std::chrono::seconds(2));

        auto ghost_stream = guard_caps.callStream(
            "guard.ghost.v1", {{"probe", 2}}, [](const omnidesk::StreamChunk&) {});
        TEST_CHECK(!ghost_stream.ok);
        TEST_CHECK(ghost_stream.error.find("missing capability") != std::string::npos);

        // Malformed non-req envelope containing "op": "shutdown" (with kind="resp", missing "id") must be ignored
        auto info1 = guard_caps.call("guard.info.v1");
        TEST_CHECK(info1.ok);
        const int guard_pid_1 = info1.data.value("pid", 0);
        const fs::path unload_marker = fs::path(info1.data.value("data_dir", "")) / "unloaded.marker";
        TEST_CHECK(guard_pid_1 > 0);

        guard_workers.sendEvent("plugin:plugin-iso-guard", {{"kind", "resp"}, {"op", "shutdown"}});
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        auto info_still_up = guard_caps.call("guard.info.v1");
        TEST_CHECK(info_still_up.ok && info_still_up.data.value("pid", 0) == guard_pid_1);

        // Graceful onUnload() when WorkerSupervisor stops the worker via SIGTERM
        fs::remove(unload_marker, ec);
        TEST_CHECK(!fs::exists(unload_marker));
        TEST_CHECK(guard_workers.stopWorker("plugin:plugin-iso-guard"));
        TEST_CHECK(fs::exists(unload_marker));

        // Graceful onUnload() on direct SIGTERM while duplex stdio pipe remains open
        fs::remove(unload_marker, ec);
        auto info2 = guard_caps.call("guard.info.v1");
        TEST_CHECK(info2.ok);
        const int guard_pid_2 = info2.data.value("pid", 0);
        TEST_CHECK(guard_pid_2 > 0 && guard_pid_2 != guard_pid_1);
        TEST_CHECK(::kill(guard_pid_2, SIGTERM) == 0);
        const auto term_deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(2000);
        while (!fs::exists(unload_marker) && std::chrono::steady_clock::now() < term_deadline) {
            std::this_thread::sleep_for(std::chrono::milliseconds(15));
        }
        TEST_CHECK(fs::exists(unload_marker));

        guard_mgr.unloadAll();
        guard_workers.stopAll();
        guard_jobs.shutdown();
        TEST_CHECK(installer.removePlugin("plugin-iso-guard", rm_err));
    }
    ::unsetenv("OMNIDESK_STAGING_DIR");
    ::unsetenv("OMNIDESK_DISABLE_GITHUB_API");
    fs::remove_all(tmp_root, ec);
    std::cout << "test_pm_core: ALL PASSED\n";
    return 0;
}
