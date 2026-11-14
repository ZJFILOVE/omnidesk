#include "omnidesk/host/kernel.hpp"

#include <borealis.hpp>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <unistd.h>
#include "omnidesk/image_cache.hpp"
#include "omnidesk/mpv_core.hpp"
#include "omnidesk/pm/process.hpp"
#include "omnidesk/ui_theme.hpp"

namespace fs = std::filesystem;

namespace omnidesk::host {
namespace {

std::string resolveSelfExe(const char* argv0) {
    std::error_code ec;
    fs::path p = fs::read_symlink("/proc/self/exe", ec);
    if (!ec && fs::exists(p)) {
        return p.string();
    }
    if (argv0 && *argv0) {
        fs::path c = fs::weakly_canonical(argv0, ec);
        if (!ec && fs::exists(c)) {
            return c.string();
        }
    }
    return "";
}

std::string resolveDataRoot() {
    if (const char* env = std::getenv("OMNIDESK_DATA_HOME")) {
        if (*env) return env;
    }
    if (const char* xdg = std::getenv("XDG_DATA_HOME")) {
        if (*xdg) return (fs::path(xdg) / "omnidesk").string();
    }
    const char* home = std::getenv("HOME");
    return (fs::path(home ? home : ".") / ".local" / "share" / "omnidesk").string();
}

std::string resolveConfigRoot() {
    if (const char* env = std::getenv("OMNIDESK_CONFIG_HOME")) {
        if (*env) return env;
    }
    if (const char* xdg = std::getenv("XDG_CONFIG_HOME")) {
        if (*xdg) return (fs::path(xdg) / "omnidesk").string();
    }
    const char* home = std::getenv("HOME");
    return (fs::path(home ? home : ".") / ".config" / "omnidesk").string();
}

} // namespace

HostKernel& HostKernel::instance() {
    static HostKernel inst;
    return inst;
}

bool HostKernel::init(const char* argv0) {
    if (initialized_) {
        return true;
    }

    exe_path_ = resolveSelfExe(argv0);
    data_root_ = resolveDataRoot();
    config_root_ = resolveConfigRoot();

    std::error_code ec;
    fs::path bin_dir = exe_path_.empty() ? fs::current_path() : fs::path(exe_path_).parent_path();

    // Locate resources directory
    for (const fs::path& cand : {
             fs::path(BRLS_RESOURCES),
             bin_dir / "resources",
             bin_dir / ".." / "share" / "omnidesk",
             fs::current_path() / "host" / "resources",
         }) {
        if (fs::exists(cand / "marketplace" / "catalog.json", ec) ||
            fs::exists(cand / "font" / "switch_font.ttf", ec)) {
            resources_dir_ = fs::weakly_canonical(cand, ec).string();
            break;
        }
    }

    // Locate OmniDeskSDKConfig.cmake
    if (const char* env = std::getenv("OMNIDESK_SDK_CMAKE_DIR")) {
        if (*env && fs::exists(env, ec)) {
            sdk_cmake_dir_ = env;
        }
    }
    if (sdk_cmake_dir_.empty()) {
        for (const fs::path& cand : {
                 bin_dir / "sdk-cmake",
                 bin_dir / ".." / "lib" / "cmake" / "OmniDeskSDK",
             }) {
            if (fs::exists(cand / "OmniDeskSDKConfig.cmake", ec)) {
                sdk_cmake_dir_ = fs::weakly_canonical(cand, ec).string();
                break;
            }
        }
    }
#ifdef OMNIDESK_DEFAULT_SDK_CMAKE_DIR
    if (sdk_cmake_dir_.empty() && fs::exists(OMNIDESK_DEFAULT_SDK_CMAKE_DIR, ec)) {
        sdk_cmake_dir_ = OMNIDESK_DEFAULT_SDK_CMAKE_DIR;
    }
#endif

    // Locate omnidesk-pm helper executable for isolated child-process ABI verification
    std::string pm_exe;
    if (!bin_dir.empty() && fs::exists(bin_dir / "omnidesk-pm", ec)) {
        pm_exe = fs::weakly_canonical(bin_dir / "omnidesk-pm", ec).string();
    } else {
        pm_exe = pm::findExecutableInPath("omnidesk-pm");
    }

    // Locate omnidesk-plugin-worker executable for subprocess-isolated plugins
    std::string worker_exe;
    if (!bin_dir.empty() && fs::exists(bin_dir / "omnidesk-plugin-worker", ec)) {
        worker_exe = fs::weakly_canonical(bin_dir / "omnidesk-plugin-worker", ec).string();
    } else {
        worker_exe = pm::findExecutableInPath("omnidesk-plugin-worker");
    }

    // Locate curated marketplace catalog
    if (const char* env = std::getenv("OMNIDESK_CATALOG_PATH")) {
        if (*env && fs::exists(env, ec)) {
            catalog_path_ = env;
        }
    }
    if (catalog_path_.empty()) {
        for (const fs::path& cand : {
                 fs::path(resources_dir_) / "marketplace" / "catalog.json",
                 bin_dir / "resources" / "marketplace" / "catalog.json",
                 fs::current_path() / "host" / "resources" / "marketplace" / "catalog.json",
             }) {
            if (!cand.empty() && fs::exists(cand, ec)) {
                catalog_path_ = fs::weakly_canonical(cand, ec).string();
                break;
            }
        }
    }

    config_ = std::make_unique<ConfigStore>(config_root_);
    secrets_ = std::make_unique<SecretVault>(config_root_);
    capabilities_ = std::make_unique<CapabilityRegistry>();
    bus_ = std::make_unique<EntityBus>();
    domains_ = std::make_unique<DomainRegistry>();
    jobs_ = std::make_unique<JobCenter>(bus_.get(), 2);
    workers_ = std::make_unique<WorkerSupervisor>();
    catalog_ = std::make_unique<pm::MarketplaceCatalog>();
    installer_ = std::make_unique<pm::PluginInstaller>(data_root_, sdk_cmake_dir_, pm_exe);
    plugins_ = std::make_unique<pm::PluginManager>(
        *installer_, *capabilities_, *bus_, *domains_, *config_, *secrets_,
        jobs_.get(), workers_.get());
    if (!worker_exe.empty()) {
        plugins_->setWorkerExecutable(worker_exe);
    }

    workers_->setFaultCallback([this](const WorkerFaultEvent& ev) {
        brls::Logger::warning(
            "[HostKernel] Worker fault '{}' (plugin '{}'): {} [crash={}, restart={}, quarantined={}]",
            ev.worker_id, ev.plugin_id, ev.error, ev.crash_count, ev.restart_count, ev.quarantined);
        if (bus_) {
            nlohmann::json payload = {
                {"worker_id", ev.worker_id},
                {"plugin_id", ev.plugin_id},
                {"exit_code", ev.exit_code},
                {"signal_number", ev.signal_number},
                {"signal_name", ev.signal_name},
                {"crash_count", ev.crash_count},
                {"restart_count", ev.restart_count},
                {"auto_restarted", ev.auto_restarted},
                {"restart_succeeded", ev.restart_succeeded},
                {"quarantined", ev.quarantined},
                {"error", ev.error},
            };
            if (!ev.recent_stderr.empty()) {
                payload["recent_stderr"] = ev.recent_stderr;
            }
            bus_->publish("worker.fault", std::move(payload));
        }
    });

    capabilities_->setFaultHandler([this](const std::string& plugin_id,
                                          const std::string& capability,
                                          const std::string& err) {
        brls::Logger::warning("[HostKernel] Plugin capability fault '{}' in '{}': {}",
                              capability, plugin_id, err);
        if (bus_) {
            bus_->publish("plugin.fault", {
                {"plugin_id", plugin_id},
                {"component", "capability:" + capability},
                {"error", err},
            });
        }
    });

    domains_->setFaultHandler([this](const std::string& plugin_id,
                                     const std::string& domain,
                                     const std::string& err) {
        brls::Logger::warning("[HostKernel] Plugin domain fault '{}' in '{}': {}",
                              domain, plugin_id, err);
        if (bus_) {
            bus_->publish("plugin.fault", {
                {"plugin_id", plugin_id},
                {"component", "domain:" + domain},
                {"error", err},
            });
        }
    });

    bus_->setFaultHandler([this](const std::string& owner,
                                 const std::string& topic,
                                 const std::string& err) {
        brls::Logger::warning("[HostKernel] EventBus subscriber fault '{}' on '{}': {}",
                              owner, topic, err);
        thread_local bool in_bus_fault = false;
        if (in_bus_fault || !bus_) {
            return;
        }
        in_bus_fault = true;
        try {
            bus_->publish("plugin.fault", {
                {"plugin_id", owner},
                {"component", "event_bus:" + topic},
                {"error", err},
            });
        } catch (...) {
        }
        in_bus_fault = false;
    });

    ImageCacheConfig ic_cfg;
    if (const char* ic_env = std::getenv("OMNIDESK_IMAGE_CACHE_DIR")) {
        if (*ic_env) ic_cfg.cache_dir = ic_env;
    } else {
        ic_cfg.cache_dir = "/tmp/omnidesk-imgcache";
    }
    ImageCache::instance().configure(ic_cfg);

    if (!catalog_path_.empty()) {
        catalog_->loadFromFile(catalog_path_);
    }

    // Load previously installed & enabled .so plugins
    plugins_->loadAllEnabled();

    // Detect SDK ABI drift across all installed plugins and emit diagnostic event
    const auto abi_statuses = installer_->checkInstalledAbiStatus(false);
    nlohmann::json stale_ids = nlohmann::json::array();
    nlohmann::json stale_entries = nlohmann::json::array();
    for (const auto& entry : abi_statuses) {
        if (entry.needs_rebuild) {
            stale_ids.push_back(entry.plugin_id);
            stale_entries.push_back({
                {"plugin_id", entry.plugin_id},
                {"version", entry.version},
                {"enabled", entry.enabled},
                {"can_rebuild", entry.can_rebuild},
                {"reason", entry.reason},
            });
        }
    }
    if (!stale_ids.empty()) {
        brls::Logger::warning("[HostKernel] Detected {} installed plugin(s) with SDK ABI drift",
                              stale_ids.size());
        bus_->publish("plugin.abi_drift", {
            {"count", stale_ids.size()},
            {"stale_plugins", std::move(stale_ids)},
            {"entries", std::move(stale_entries)},
        });
    }

    initialized_ = true;
    return true;
}

void HostKernel::shutdown() {
    if (!initialized_) {
        return;
    }
    MPVCore::instance().shutdown();
    ImageCache::instance().shutdown();
    if (workers_) {
        workers_->stopAll();
    }
    const int grace_ms = JobCenter::defaultShutdownGraceMs();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(grace_ms);
    auto remainingMs = [&deadline]() -> int {
        const auto rem = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        return rem > 0 ? static_cast<int>(rem) : 0;
    };
    if (jobs_) {
        jobs_->shutdown(remainingMs());
    }
    if (capabilities_) {
        capabilities_->shutdown(remainingMs());
    }
    if (domains_) {
        domains_->shutdown(remainingMs());
    }
    if (plugins_) {
        plugins_->unloadAll();
    }
    if (workers_) {
        workers_->stopAll();
    }
    plugins_.reset();
    installer_.reset();
    catalog_.reset();
    workers_.reset();
    jobs_.reset();
    domains_.reset();
    bus_.reset();
    capabilities_.reset();
    secrets_.reset();
    config_.reset();
    initialized_ = false;
}

bool HostKernel::isDarkTheme() const {
    return brls::Application::getThemeVariant() == brls::ThemeVariant::DARK;
}

void HostKernel::setDarkTheme(bool dark) {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->setThemeVariant(dark ? brls::ThemeVariant::DARK : brls::ThemeVariant::LIGHT);
    }
    ui::applyDesignSystemTheme();
    if (config_) {
        auto host_cfg = config_->loadHostConfig();
        host_cfg["theme"] = dark ? "dark" : "light";
        config_->saveHostConfig(host_cfg);
    }
    if (bus_) {
        bus_->publish("host.theme_changed", {{"dark", dark}});
    }
}

void HostKernel::toggleTheme() {
    setDarkTheme(!isDarkTheme());
}

std::string HostKernel::assetPath(const std::string& rel) const {
    std::error_code ec;
    if (!resources_dir_.empty()) {
        fs::path p = fs::path(resources_dir_) / rel;
        if (fs::exists(p, ec)) {
            return p.string();
        }
    }
    fs::path fallback = fs::path(BRLS_RESOURCES) / rel;
    if (fs::exists(fallback, ec)) {
        return fallback.string();
    }
    return rel;
}

} // namespace omnidesk::host
