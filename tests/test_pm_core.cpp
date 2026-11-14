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

    ::unsetenv("OMNIDESK_STAGING_DIR");
    ::unsetenv("OMNIDESK_DISABLE_GITHUB_API");
    fs::remove_all(tmp_root, ec);
    std::cout << "test_pm_core: ALL PASSED\n";
    return 0;
}
