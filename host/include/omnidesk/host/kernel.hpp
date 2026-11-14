#pragma once

#include <memory>
#include <string>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/pm/manager.hpp"
#include "omnidesk/worker_supervisor.hpp"

namespace omnidesk::host {

/**
 * Central OmniDesk microkernel container owning SDK registries, config/vault storage,
 * background JobCenter, WorkerSupervisor sidecars, and the Plugin Marketplace installer/loader.
 */
class HostKernel {
public:
    static HostKernel& instance();

    bool init(const char* argv0);
    void shutdown();

    bool isInitialized() const { return initialized_; }

    const std::string& exePath() const { return exe_path_; }
    const std::string& dataRoot() const { return data_root_; }
    const std::string& configRoot() const { return config_root_; }
    const std::string& sdkCmakeDir() const { return sdk_cmake_dir_; }
    const std::string& resourcesDir() const { return resources_dir_; }
    const std::string& catalogPath() const { return catalog_path_; }

    ConfigStore& config() { return *config_; }
    SecretVault& secrets() { return *secrets_; }
    CapabilityRegistry& capabilities() { return *capabilities_; }
    EntityBus& bus() { return *bus_; }
    DomainRegistry& domains() { return *domains_; }
    JobCenter& jobs() { return *jobs_; }
    WorkerSupervisor& workers() { return *workers_; }
    pm::MarketplaceCatalog& catalog() { return *catalog_; }
    pm::PluginInstaller& installer() { return *installer_; }
    pm::PluginManager& plugins() { return *plugins_; }

    bool isDarkTheme() const;
    void setDarkTheme(bool dark);
    void toggleTheme();

    std::string assetPath(const std::string& rel) const;

private:
    HostKernel() = default;
    ~HostKernel() = default;

    bool initialized_ = false;
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
    std::unique_ptr<WorkerSupervisor> workers_;
    std::unique_ptr<pm::MarketplaceCatalog> catalog_;
    std::unique_ptr<pm::PluginInstaller> installer_;
    std::unique_ptr<pm::PluginManager> plugins_;
};

} // namespace omnidesk::host
