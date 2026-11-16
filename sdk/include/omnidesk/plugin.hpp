#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/export.h"
#include "omnidesk/job_center.hpp"
#include "omnidesk/types.hpp"
#include "omnidesk/worker_supervisor.hpp"

namespace brls {
class View;
}

namespace omnidesk {

/**
 * Execution context passed to each plugin's IPlugin::onLoad(PluginContext&).
 * Automatically scopes registrations and persistence to the owning plugin_id
 * so unloading revokes all capabilities, bus listeners, jobs, workers, and UI tabs cleanly.
 */
class OMNIDESK_API PluginContext {
public:
    PluginContext(std::string plugin_id,
                  std::string plugin_dir,
                  std::string data_dir,
                  CapabilityRegistry& capabilities,
                  EntityBus& bus,
                  DomainRegistry& domains,
                  ConfigStore& config,
                  SecretVault& secrets,
                  JobCenter* jobs = nullptr,
                  WorkerSupervisor* workers = nullptr);

    const std::string& pluginId() const { return plugin_id_; }
    const std::string& pluginDir() const { return plugin_dir_; }
    const std::string& dataDir() const { return data_dir_; }

    CapabilityRegistry& capabilities() { return capabilities_; }
    EntityBus& bus() { return bus_; }
    DomainRegistry& domains() { return domains_; }
    ConfigStore& configStore() { return config_; }
    SecretVault& secretVault() { return secrets_; }
    JobCenter* jobs() const { return jobs_; }
    WorkerSupervisor* workers() const { return workers_; }

    // Scoped convenience helpers that auto-tag plugin_id_:
    bool registerCapability(const std::string& capability_id,
                            CapabilityHandler handler,
                            nlohmann::json metadata = nlohmann::json::object(),
                            int priority = 100);
    bool registerStreamingCapability(const std::string& capability_id,
                                     CapabilityStreamHandler handler,
                                     nlohmann::json metadata = nlohmann::json::object(),
                                     int priority = 100);
    uint64_t subscribe(const std::string& topic, EntityEventHandler handler);
    size_t publish(const std::string& topic, const nlohmann::json& payload = nlohmann::json::object()) const;

    std::string submitJob(JobSpec spec, JobCenter::TaskFn task);
    bool registerWorker(WorkerSpec spec);
    bool registerWorkerCapability(const std::string& capability_id,
                                  const std::string& worker_id,
                                  const std::string& default_action = "",
                                  nlohmann::json metadata = nlohmann::json::object(),
                                  int priority = 100);
    bool registerWorkerMediaSource(const std::string& worker_id,
                                   const std::string& source_id,
                                   const std::string& display_name,
                                   std::vector<std::string> categories = {});
    bool registerWorkerTextSource(const std::string& worker_id,
                                  const std::string& source_id,
                                  const std::string& display_name,
                                  const std::string& category = "blog",
                                  std::vector<TextChannel> channels = {});
    bool registerWorkerBookSource(const std::string& worker_id,
                                  const std::string& source_id,
                                  const std::string& display_name);

    void registerMediaSource(std::shared_ptr<IMediaSource> source);
    void registerTextSource(std::shared_ptr<ITextSource> source);
    void registerBookSource(std::shared_ptr<IBookSource> source);
    void registerWorkspaceTab(WorkspaceTabSpec spec);
    void registerDockWidget(DockWidgetSpec spec);

    nlohmann::json loadConfig() const;
    bool saveConfig(const nlohmann::json& cfg) const;
    std::string getConfigString(const std::string& key, const std::string& fallback = "") const;
    std::optional<std::string> getSecret(const std::string& key) const;
    bool setSecret(const std::string& key, const std::string& value);

    void logInfo(const std::string& msg) const;
    void logWarn(const std::string& msg) const;
    void logError(const std::string& msg) const;

    std::vector<std::string> logs() const;

private:
    std::string plugin_id_;
    std::string plugin_dir_;
    std::string data_dir_;
    CapabilityRegistry& capabilities_;
    EntityBus& bus_;
    DomainRegistry& domains_;
    ConfigStore& config_;
    SecretVault& secrets_;
    JobCenter* jobs_ = nullptr;
    WorkerSupervisor* workers_ = nullptr;
    mutable std::vector<std::string> recent_logs_;
};

#ifndef OMNIDESK_PLUGIN_INTERFACE_REVISION
#define OMNIDESK_PLUGIN_INTERFACE_REVISION 2u
#endif

/**
 * Base interface every standalone C++17 .so plugin implements.
 */
class OMNIDESK_API IPlugin {
public:
    virtual ~IPlugin() = default;

    virtual bool onLoad(PluginContext& ctx) = 0;
    virtual void onUnload() = 0;

    virtual nlohmann::json onHealthCheck() const {
        return {{"ok", true}};
    }

    /**
     * Optional custom Borealis settings view. If nullptr is returned,
     * the Plugin Store renders a native form automatically from plugin.json config_schema.
     */
    virtual brls::View* createSettingsView(PluginContext& ctx) {
        (void)ctx;
        return nullptr;
    }
};

OMNIDESK_API std::string sdkVersion();
OMNIDESK_API std::string sdkAbiSignature();

/// Process-local capability token for privileged calls initiated by the host UI.
/// Remote control/MCP clients run in other processes and cannot observe this value.
OMNIDESK_API const std::string& localUiCallToken();

} // namespace omnidesk
