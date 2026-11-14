#pragma once

#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/export.h"
#include "omnidesk/job_center.hpp"
#include "omnidesk/plugin.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/worker_supervisor.hpp"

namespace omnidesk::pm {

struct RuntimePluginStatus {
    InstalledPluginRecord record;
    bool loaded = false;
    bool healthy = true;
    std::string load_error;
    std::vector<std::string> recent_logs;
};

/**
 * Runtime loader/unloader for C++17 .so plugins.
 * Enforces ABI v1 handshake, isolates PluginContext per plugin, and revokes
 * all capabilities/bus listeners/domain UI bindings/jobs/workers cleanly on unload.
 */
class OMNIDESK_API PluginManager {
public:
    PluginManager(PluginInstaller& installer,
                  CapabilityRegistry& capabilities,
                  EntityBus& bus,
                  DomainRegistry& domains,
                  ConfigStore& config,
                  SecretVault& secrets,
                  JobCenter* jobs = nullptr,
                  WorkerSupervisor* workers = nullptr);
    ~PluginManager();

    void loadAllEnabled();
    void unloadAll();

    bool loadPlugin(const std::string& plugin_id, std::string& error);
    bool unloadPlugin(const std::string& plugin_id);
    bool reloadPlugin(const std::string& plugin_id, std::string& error);

    bool enablePlugin(const std::string& plugin_id, std::string& error);
    bool disablePlugin(const std::string& plugin_id);

    std::vector<RuntimePluginStatus> statuses() const;
    std::optional<RuntimePluginStatus> statusOf(const std::string& plugin_id) const;
    bool isLoaded(const std::string& plugin_id) const;

    brls::View* createCustomSettingsView(const std::string& plugin_id);

private:
    struct LoadedHandle {
        void* dl_handle = nullptr;
        IPlugin* instance = nullptr;
        void (*destroy_fn)(void*) = nullptr;
        std::unique_ptr<PluginContext> context;
    };

    PluginInstaller& installer_;
    CapabilityRegistry& capabilities_;
    EntityBus& bus_;
    DomainRegistry& domains_;
    ConfigStore& config_;
    SecretVault& secrets_;
    JobCenter* jobs_ = nullptr;
    WorkerSupervisor* workers_ = nullptr;

    mutable std::mutex mutex_;
    std::unordered_map<std::string, LoadedHandle> loaded_;
    std::unordered_map<std::string, std::string> last_errors_;
};

} // namespace omnidesk::pm
