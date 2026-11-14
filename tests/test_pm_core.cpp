#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <thread>
#include <unistd.h>
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/pm/manager.hpp"
#include "omnidesk/pm/manifest.hpp"
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

    ::unsetenv("OMNIDESK_STAGING_DIR");
    ::unsetenv("OMNIDESK_DISABLE_GITHUB_API");
    fs::remove_all(tmp_root, ec);
    std::cout << "test_pm_core: ALL PASSED\n";
    return 0;
}
