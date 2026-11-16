#pragma once

#include <atomic>
#include <memory>
#include <string>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/mpris2.hpp"
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/control_bridge.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/pm/manager.hpp"
#include "omnidesk/search_index.hpp"
#include "omnidesk/ui_task_runner.hpp"
#include "omnidesk/worker_supervisor.hpp"

#include <mutex>
#include <unordered_set>

namespace omnidesk::host {

/**
 * Central OmniDesk microkernel container owning SDK registries, config/vault storage,
 * background JobCenter, WorkerSupervisor sidecars, and the Plugin Marketplace installer/loader.
 */
class HostKernel {
public:
    static HostKernel& instance();

    bool init(const char* argv0);
    void startDesktopServices();
    void stopBackgroundServices();
    void shutdown();

    bool isInitialized() const { return initialized_.load(std::memory_order_acquire); }

    const std::string& exePath() const { return exe_path_; }
    const std::string& dataRoot() const { return data_root_; }
    const std::string& configRoot() const { return config_root_; }
    const std::string& sdkCmakeDir() const { return sdk_cmake_dir_; }
    const std::string& resourcesDir() const { return resources_dir_; }
    const std::string& catalogPath() const { return catalog_path_; }
    std::string controlSocketPath() const;

    ConfigStore& config() { return *config_; }
    SecretVault& secrets() { return *secrets_; }
    CapabilityRegistry& capabilities() { return *capabilities_; }
    EntityBus& bus() { return *bus_; }
    DomainRegistry& domains() { return *domains_; }
    JobCenter& jobs() { return *jobs_; }
    UiTaskRunner& uiTasks() { return *ui_tasks_; }
    SearchIndexService& searchIndex() { return *search_index_; }
    Mpris2Service& mpris() { return *mpris_; }
    WorkerSupervisor& workers() { return *workers_; }
    pm::MarketplaceCatalog& catalog() { return *catalog_; }
    pm::PluginInstaller& installer() { return *installer_; }
    pm::PluginManager& plugins() { return *plugins_; }

    std::vector<pm::AbiStatusEntry> installedAbiStatuses(bool force_refresh = false);
    bool hasStaleAbiPlugins(bool force_refresh = false);

    bool isDarkTheme() const;
    void setDarkTheme(bool dark);
    void toggleTheme();

    std::string assetPath(const std::string& rel) const;

private:
    HostKernel() = default;
    ~HostKernel() = default;

    void scheduleSearchHydrate(uint32_t scopes,
                               const std::string& source_id = "",
                               bool all_domain_sources = false,
                               int debounce_ms = 320);

    std::atomic<bool> initialized_{false};
    bool background_services_stopped_ = false;
    std::string exe_path_;
    std::string data_root_;
    std::string config_root_;
    std::string sdk_cmake_dir_;
    std::string resources_dir_;
    std::string catalog_path_;

    std::unique_ptr<ConfigStore> config_;
    std::unique_ptr<SecretVault> secrets_;
    std::unique_ptr<CapabilityRegistry> capabilities_;
    std::unique_ptr<EntityBus> bus_;
    std::unique_ptr<DomainRegistry> domains_;
    std::unique_ptr<JobCenter> jobs_;
    std::unique_ptr<UiTaskRunner> ui_tasks_;
    std::unique_ptr<UiTaskRunner> search_tasks_;
    std::unique_ptr<SearchIndexService> search_index_;
    std::unique_ptr<Mpris2Service> mpris_;
    std::unique_ptr<WorkerSupervisor> workers_;
    std::unique_ptr<pm::MarketplaceCatalog> catalog_;
    std::unique_ptr<pm::PluginInstaller> installer_;
    std::unique_ptr<pm::PluginManager> plugins_;
    std::unique_ptr<pm::ControlServer> control_server_;
    uint64_t search_plugin_sub_ = 0;
    uint64_t search_recommend_sub_ = 0;
    uint64_t search_vocab_sub_ = 0;
    uint64_t abi_plugin_sub_ = 0;
    std::mutex abi_cache_mu_;
    bool abi_cache_valid_ = false;
    std::vector<pm::AbiStatusEntry> cached_abi_statuses_;
    std::mutex search_hydrate_mu_;
    uint32_t pending_hydrate_scopes_ = 0;
    bool pending_hydrate_all_sources_ = false;
    std::unordered_set<std::string> pending_hydrate_sources_;
};

} // namespace omnidesk::host
