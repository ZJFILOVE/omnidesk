#include "omnidesk/plugin.hpp"
#include "omnidesk/plugin_abi.h"
#include "omnidesk/worker_domain_bridge.hpp"
#include <borealis/core/logger.hpp>
#include <mutex>

namespace omnidesk {
namespace {

std::mutex& pluginLogMutex() {
    static std::mutex m;
    return m;
}

}  // namespace

PluginContext::PluginContext(std::string plugin_id,
                             std::string plugin_dir,
                             std::string data_dir,
                             CapabilityRegistry& capabilities,
                             EntityBus& bus,
                             DomainRegistry& domains,
                             ConfigStore& config,
                             SecretVault& secrets,
                             JobCenter* jobs,
                             WorkerSupervisor* workers)
    : plugin_id_(std::move(plugin_id))
    , plugin_dir_(std::move(plugin_dir))
    , data_dir_(std::move(data_dir))
    , capabilities_(capabilities)
    , bus_(bus)
    , domains_(domains)
    , config_(config)
    , secrets_(secrets)
    , jobs_(jobs)
    , workers_(workers) {}

bool PluginContext::registerCapability(const std::string& capability_id,
                                       CapabilityHandler handler,
                                       nlohmann::json metadata,
                                       int priority) {
    return capabilities_.registerCapability(
        capability_id, plugin_id_, std::move(handler), std::move(metadata), priority);
}

bool PluginContext::registerStreamingCapability(const std::string& capability_id,
                                                CapabilityStreamHandler handler,
                                                nlohmann::json metadata,
                                                int priority) {
    return capabilities_.registerStreamingCapability(
        capability_id, plugin_id_, std::move(handler), std::move(metadata), priority);
}

uint64_t PluginContext::subscribe(const std::string& topic, EntityEventHandler handler) {
    return bus_.subscribe(topic, plugin_id_, std::move(handler));
}

size_t PluginContext::publish(const std::string& topic, const nlohmann::json& payload) const {
    return bus_.publish(topic, payload);
}

std::string PluginContext::submitJob(JobSpec spec, JobCenter::TaskFn task) {
    if (!jobs_) return "";
    spec.plugin_id = plugin_id_;
    return jobs_->submit(spec, std::move(task));
}

bool PluginContext::registerWorker(WorkerSpec spec) {
    if (!workers_) return false;
    spec.plugin_id = plugin_id_;
    return workers_->registerWorker(spec);
}

bool PluginContext::registerWorkerCapability(const std::string& capability_id,
                                             const std::string& worker_id,
                                             const std::string& default_action,
                                             nlohmann::json metadata,
                                             int priority) {
    if (!workers_ || capability_id.empty() || worker_id.empty()) return false;
    auto handler = makeWorkerStreamCapabilityHandler(
        *workers_, worker_id, capability_id, "", default_action);
    return capabilities_.registerStreamingCapability(
        capability_id, plugin_id_, std::move(handler), std::move(metadata), priority);
}

bool PluginContext::registerWorkerMediaSource(const std::string& worker_id,
                                              const std::string& source_id,
                                              const std::string& display_name,
                                              std::vector<std::string> categories) {
    if (!workers_ || worker_id.empty() || source_id.empty()) return false;
    auto src = std::make_shared<WorkerMediaSource>(
        *workers_, worker_id, plugin_id_, source_id, display_name, std::move(categories));
    domains_.registerMediaSource(plugin_id_, std::move(src));
    return true;
}

bool PluginContext::registerWorkerTextSource(const std::string& worker_id,
                                             const std::string& source_id,
                                             const std::string& display_name,
                                             const std::string& category) {
    if (!workers_ || worker_id.empty() || source_id.empty()) return false;
    auto src = std::make_shared<WorkerTextSource>(
        *workers_, worker_id, plugin_id_, source_id, display_name, category);
    domains_.registerTextSource(plugin_id_, std::move(src));
    return true;
}

bool PluginContext::registerWorkerBookSource(const std::string& worker_id,
                                             const std::string& source_id,
                                             const std::string& display_name) {
    if (!workers_ || worker_id.empty() || source_id.empty()) return false;
    auto src = std::make_shared<WorkerBookSource>(
        *workers_, worker_id, plugin_id_, source_id, display_name);
    domains_.registerBookSource(plugin_id_, std::move(src));
    return true;
}

void PluginContext::registerMediaSource(std::shared_ptr<IMediaSource> source) {
    domains_.registerMediaSource(plugin_id_, std::move(source));
}

void PluginContext::registerTextSource(std::shared_ptr<ITextSource> source) {
    domains_.registerTextSource(plugin_id_, std::move(source));
}

void PluginContext::registerBookSource(std::shared_ptr<IBookSource> source) {
    domains_.registerBookSource(plugin_id_, std::move(source));
}

void PluginContext::registerWorkspaceTab(WorkspaceTabSpec spec) {
    spec.plugin_id = plugin_id_;
    domains_.registerWorkspaceTab(std::move(spec));
}

void PluginContext::registerDockWidget(DockWidgetSpec spec) {
    spec.plugin_id = plugin_id_;
    domains_.registerDockWidget(std::move(spec));
}

nlohmann::json PluginContext::loadConfig() const {
    return config_.loadPluginConfig(plugin_id_);
}

bool PluginContext::saveConfig(const nlohmann::json& cfg) const {
    return config_.savePluginConfig(plugin_id_, cfg);
}

std::string PluginContext::getConfigString(const std::string& key,
                                           const std::string& fallback) const {
    return config_.getPluginValue(plugin_id_, key, fallback);
}

std::optional<std::string> PluginContext::getSecret(const std::string& key) const {
    return secrets_.getSecret(plugin_id_, key);
}

bool PluginContext::setSecret(const std::string& key, const std::string& value) {
    return secrets_.setSecret(plugin_id_, key, value);
}

void PluginContext::logInfo(const std::string& msg) const {
    {
        std::lock_guard<std::mutex> lk(pluginLogMutex());
        recent_logs_.push_back("[INFO] " + msg);
        if (recent_logs_.size() > 64) recent_logs_.erase(recent_logs_.begin());
    }
    brls::Logger::info("[{}] {}", plugin_id_, msg);
}

void PluginContext::logWarn(const std::string& msg) const {
    {
        std::lock_guard<std::mutex> lk(pluginLogMutex());
        recent_logs_.push_back("[WARN] " + msg);
        if (recent_logs_.size() > 64) recent_logs_.erase(recent_logs_.begin());
    }
    brls::Logger::warning("[{}] {}", plugin_id_, msg);
}

void PluginContext::logError(const std::string& msg) const {
    {
        std::lock_guard<std::mutex> lk(pluginLogMutex());
        recent_logs_.push_back("[ERROR] " + msg);
        if (recent_logs_.size() > 64) recent_logs_.erase(recent_logs_.begin());
    }
    brls::Logger::error("[{}] {}", plugin_id_, msg);
}

std::string sdkVersion() {
    return OMNIDESK_SDK_VERSION_STRING;
}

std::string sdkAbiSignature() {
    return omnidesk_sdk_abi_signature();
}

} // namespace omnidesk

extern "C" OMNIDESK_API const char* omnidesk_sdk_abi_signature(void) {
    static constexpr auto kHostSig = ::omnidesk::detail::computeHeaderSdkAbiSignature();
    return kHostSig.c_str();
}
