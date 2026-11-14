#include "omnidesk/pm/manager.hpp"

#include <dlfcn.h>
#include <filesystem>
#include "omnidesk/plugin_abi.h"

namespace fs = std::filesystem;

namespace omnidesk::pm {

PluginManager::PluginManager(PluginInstaller& installer,
                             CapabilityRegistry& capabilities,
                             EntityBus& bus,
                             DomainRegistry& domains,
                             ConfigStore& config,
                             SecretVault& secrets,
                             JobCenter* jobs,
                             WorkerSupervisor* workers)
    : installer_(installer),
      capabilities_(capabilities),
      bus_(bus),
      domains_(domains),
      config_(config),
      secrets_(secrets),
      jobs_(jobs),
      workers_(workers) {}

PluginManager::~PluginManager() {
    unloadAll();
}

void PluginManager::loadAllEnabled() {
    for (const auto& rec : installer_.listInstalled()) {
        if (rec.enabled) {
            std::string err;
            loadPlugin(rec.manifest.id, err);
        }
    }
}

void PluginManager::unloadAll() {
    std::vector<std::string> ids;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        for (const auto& [id, _] : loaded_) {
            ids.push_back(id);
        }
    }
    for (const auto& id : ids) {
        unloadPlugin(id);
    }
}

bool PluginManager::loadPlugin(const std::string& plugin_id, std::string& error) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (loaded_.find(plugin_id) != loaded_.end()) {
            error.clear();
            return true;
        }
    }

    auto rec = installer_.getInstalled(plugin_id);
    if (!rec.has_value()) {
        error = "Plugin not installed: " + plugin_id;
        return false;
    }
    if (!fs::exists(rec->so_path)) {
        error = "Plugin binary missing: " + rec->so_path;
        std::lock_guard<std::mutex> lock(mutex_);
        last_errors_[plugin_id] = error;
        return false;
    }

    void* handle = dlopen(rec->so_path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        const char* dlerr = dlerror();
        error = std::string("dlopen failed: ") + (dlerr ? dlerr : "unknown");
        std::lock_guard<std::mutex> lock(mutex_);
        last_errors_[plugin_id] = error;
        return false;
    }

    auto fail_with_dlclose = [&](const std::string& msg) {
        dlclose(handle);
        error = msg;
        std::lock_guard<std::mutex> lock(mutex_);
        last_errors_[plugin_id] = msg;
        return false;
    };

    auto abi_fn = reinterpret_cast<OmniDeskAbiInfoFn>(dlsym(handle, "omnidesk_plugin_abi_info"));
    auto create_fn = reinterpret_cast<OmniDeskPluginCreateFn>(dlsym(handle, "omnidesk_plugin_create"));
    auto destroy_fn = reinterpret_cast<OmniDeskPluginDestroyFn>(dlsym(handle, "omnidesk_plugin_destroy"));

    if (!abi_fn || !create_fn || !destroy_fn) {
        return fail_with_dlclose("Plugin missing mandatory ABI entrypoints");
    }

    const OmniDeskPluginInfo* info = abi_fn();
    if (!info || info->abi_version != OMNIDESK_PLUGIN_ABI_VERSION ||
        info->struct_size < sizeof(OmniDeskPluginInfo) || !info->id ||
        plugin_id != info->id) {
        return fail_with_dlclose("Plugin ABI handshake failed");
    }

    IPlugin* instance = static_cast<IPlugin*>(create_fn());
    if (!instance) {
        return fail_with_dlclose("omnidesk_plugin_create() returned nullptr");
    }

    const std::string plugin_data_dir =
        (fs::path(installer_.dataRoot()) / "plugin-data" / plugin_id).string();
    std::error_code ec;
    fs::create_directories(plugin_data_dir, ec);

    auto ctx = std::make_unique<PluginContext>(
        plugin_id, rec->install_dir, plugin_data_dir, capabilities_, bus_, domains_, config_, secrets_,
        jobs_, workers_);

    // Seed defaults into plugin config if missing
    nlohmann::json cfg = config_.loadPluginConfig(plugin_id);
    bool cfg_changed = false;
    for (const auto& field : rec->manifest.config_schema) {
        if (field.type != ConfigFieldType::Secret && !cfg.contains(field.key) &&
            !field.default_value.empty()) {
            if (field.type == ConfigFieldType::Bool) {
                cfg[field.key] = (field.default_value == "true");
            } else if (field.type == ConfigFieldType::Int) {
                try {
                    cfg[field.key] = std::stoll(field.default_value);
                } catch (...) {
                    cfg[field.key] = field.default_value;
                }
            } else {
                cfg[field.key] = field.default_value;
            }
            cfg_changed = true;
        }
    }
    if (cfg_changed) {
        config_.savePluginConfig(plugin_id, cfg);
    }

    if (!instance->onLoad(*ctx)) {
        capabilities_.unregisterPlugin(plugin_id);
        if (jobs_) jobs_->cancelByPlugin(plugin_id);
        if (workers_) workers_->stopByPlugin(plugin_id);
        bus_.unsubscribeOwner(plugin_id);
        domains_.unregisterPlugin(plugin_id);
        destroy_fn(instance);
        return fail_with_dlclose("IPlugin::onLoad() returned false");
    }

    LoadedHandle lh;
    lh.dl_handle = handle;
    lh.instance = instance;
    lh.destroy_fn = destroy_fn;
    lh.context = std::move(ctx);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        loaded_[plugin_id] = std::move(lh);
        last_errors_.erase(plugin_id);
    }

    bus_.publish("plugin.state_changed", {{"plugin_id", plugin_id}, {"loaded", true}});
    error.clear();
    return true;
}

bool PluginManager::unloadPlugin(const std::string& plugin_id) {
    LoadedHandle lh;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = loaded_.find(plugin_id);
        if (it == loaded_.end()) {
            return false;
        }
        lh = std::move(it->second);
        loaded_.erase(it);
    }

    if (lh.instance) {
        lh.instance->onUnload();
    }
    capabilities_.unregisterPlugin(plugin_id);
    if (jobs_) jobs_->cancelByPlugin(plugin_id);
    if (workers_) workers_->stopByPlugin(plugin_id);
    bus_.unsubscribeOwner(plugin_id);
    domains_.unregisterPlugin(plugin_id);

    if (lh.instance && lh.destroy_fn) {
        lh.destroy_fn(lh.instance);
    }
    lh.context.reset();
    if (lh.dl_handle) {
        dlclose(lh.dl_handle);
    }

    bus_.publish("plugin.state_changed", {{"plugin_id", plugin_id}, {"loaded", false}});
    return true;
}

bool PluginManager::reloadPlugin(const std::string& plugin_id, std::string& error) {
    unloadPlugin(plugin_id);
    return loadPlugin(plugin_id, error);
}

bool PluginManager::enablePlugin(const std::string& plugin_id, std::string& error) {
    if (!installer_.setEnabled(plugin_id, true)) {
        error = "Plugin not installed: " + plugin_id;
        return false;
    }
    return loadPlugin(plugin_id, error);
}

bool PluginManager::disablePlugin(const std::string& plugin_id) {
    unloadPlugin(plugin_id);
    return installer_.setEnabled(plugin_id, false);
}

std::vector<RuntimePluginStatus> PluginManager::statuses() const {
    auto installed = installer_.listInstalled();
    std::vector<RuntimePluginStatus> out;
    out.reserve(installed.size());

    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& rec : installed) {
        RuntimePluginStatus st;
        st.record = rec;
        auto lit = loaded_.find(rec.manifest.id);
        if (lit != loaded_.end()) {
            st.loaded = true;
            nlohmann::json hc = lit->second.instance->onHealthCheck();
            st.healthy = hc.value("ok", true);
            st.recent_logs = lit->second.context->logs();
        } else {
            st.loaded = false;
            auto eit = last_errors_.find(rec.manifest.id);
            if (eit != last_errors_.end()) {
                st.load_error = eit->second;
                st.healthy = false;
            }
        }
        out.push_back(std::move(st));
    }
    return out;
}

std::optional<RuntimePluginStatus> PluginManager::statusOf(const std::string& plugin_id) const {
    auto rec = installer_.getInstalled(plugin_id);
    if (!rec.has_value()) {
        return std::nullopt;
    }
    RuntimePluginStatus st;
    st.record = *rec;
    std::lock_guard<std::mutex> lock(mutex_);
    auto lit = loaded_.find(plugin_id);
    if (lit != loaded_.end()) {
        st.loaded = true;
        nlohmann::json hc = lit->second.instance->onHealthCheck();
        st.healthy = hc.value("ok", true);
        st.recent_logs = lit->second.context->logs();
    } else {
        auto eit = last_errors_.find(plugin_id);
        if (eit != last_errors_.end()) {
            st.load_error = eit->second;
            st.healthy = false;
        }
    }
    return st;
}

bool PluginManager::isLoaded(const std::string& plugin_id) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return loaded_.find(plugin_id) != loaded_.end();
}

brls::View* PluginManager::createCustomSettingsView(const std::string& plugin_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = loaded_.find(plugin_id);
    if (it == loaded_.end() || !it->second.instance || !it->second.context) {
        return nullptr;
    }
    return it->second.instance->createSettingsView(*it->second.context);
}

} // namespace omnidesk::pm
