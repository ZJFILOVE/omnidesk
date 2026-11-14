#include "omnidesk/pm/manager.hpp"

#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <dlfcn.h>
#include <filesystem>
#include <unistd.h>
#include "omnidesk/plugin.hpp"
#include "omnidesk/plugin_abi.h"
#include "omnidesk/pm/process.hpp"
#include "omnidesk/worker_domain_bridge.hpp"

namespace fs = std::filesystem;

namespace omnidesk::pm {

namespace {

struct PinnedPluginSlot {
    void* dl_handle = nullptr;
    IPlugin* instance = nullptr;
    PluginContext* context = nullptr;
};

void pinInFlightPlugin(void* dl_handle, IPlugin* instance, std::unique_ptr<PluginContext> context) {
    static std::mutex pin_mutex;
    static auto* pinned = new std::vector<PinnedPluginSlot>();
    std::lock_guard<std::mutex> lock(pin_mutex);
    pinned->push_back(PinnedPluginSlot{dl_handle, instance, context.release()});
}

void seedPluginConfigDefaults(const InstalledPluginRecord& rec, ConfigStore& config) {
    nlohmann::json cfg = config.loadPluginConfig(rec.manifest.id);
    bool cfg_changed = false;
    for (const auto& field : rec.manifest.config_schema) {
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
        config.savePluginConfig(rec.manifest.id, cfg);
    }
}

}  // namespace

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

void PluginManager::setWorkerExecutable(std::string path) {
    std::lock_guard<std::mutex> lock(mutex_);
    worker_executable_ = std::move(path);
}

std::string PluginManager::resolveWorkerExecutable() const {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!worker_executable_.empty() && fs::exists(worker_executable_)) {
            return worker_executable_;
        }
    }
    if (const char* env = std::getenv("OMNIDESK_WORKER_EXE")) {
        if (*env && fs::exists(env)) {
            return env;
        }
    }
    std::error_code ec;
    fs::path self = fs::read_symlink("/proc/self/exe", ec);
    if (!ec && !self.empty()) {
        fs::path sibling = self.parent_path() / "omnidesk-plugin-worker";
        if (fs::exists(sibling, ec)) {
            return sibling.string();
        }
    }
#ifdef OMNIDESK_DEFAULT_WORKER_EXE
    if (fs::exists(OMNIDESK_DEFAULT_WORKER_EXE, ec)) {
        return OMNIDESK_DEFAULT_WORKER_EXE;
    }
#endif
    return findExecutableInPath("omnidesk-plugin-worker");
}

void PluginManager::emitFault(const std::string& plugin_id,
                              const std::string& component,
                              const std::string& error) {
    bus_.publish("plugin.fault", {
        {"plugin_id", plugin_id},
        {"component", component},
        {"error", error},
    });
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

bool PluginManager::loadSubprocessPlugin(const InstalledPluginRecord& rec, std::string& error) {
    const std::string& plugin_id = rec.manifest.id;
    auto fail_record = [&](const std::string& msg, bool abi_drift = false) {
        error = msg;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            last_errors_[plugin_id] = msg;
        }
        if (abi_drift) {
            bus_.publish("plugin.abi_drift", {{"plugin_id", plugin_id}, {"reason", msg}});
        } else {
            emitFault(plugin_id, "subprocess_load", msg);
        }
        return false;
    };

    if (!workers_) {
        return fail_record("WorkerSupervisor is null; cannot launch subprocess plugin " + plugin_id);
    }
    const std::string worker_exe = resolveWorkerExecutable();
    std::error_code ec;
    if (worker_exe.empty() || !fs::exists(worker_exe, ec)) {
        return fail_record("omnidesk-plugin-worker executable not found");
    }

    const std::string plugin_data_dir =
        (fs::path(installer_.dataRoot()) / "plugin-data" / plugin_id).string();
    fs::create_directories(plugin_data_dir, ec);

    WorkerSpec spec;
    spec.worker_id = "plugin:" + plugin_id;
    spec.plugin_id = plugin_id;
    spec.executable = worker_exe;
    spec.args = {
        "--plugin-id", plugin_id,
        "--so", rec.so_path,
        "--plugin-dir", rec.install_dir,
        "--data-dir", plugin_data_dir,
        "--config-dir", config_.configRoot(),
    };
    spec.transport = (rec.manifest.worker_transport == "unix_socket")
                         ? WorkerTransport::UnixSocket
                         : WorkerTransport::StdioPipe;
    spec.idle_timeout_ms = 0;
    spec.stop_grace_ms = 1200;
    spec.auto_restart = true;
    spec.retry_on_crash = false;

    workers_->unregisterWorker(spec.worker_id);
    if (!workers_->registerWorker(spec)) {
        return fail_record("Failed registering worker runtime for " + plugin_id);
    }

    CapabilityResult desc = workers_->request(spec.worker_id, {{"op", "describe"}}, nullptr, 15000);
    if (!desc.ok) {
        std::string msg = desc.error;
        std::string stderr_detail;
        if (auto wst = workers_->status(spec.worker_id)) {
            for (const auto& l : wst->recent_stderr) {
                if (!stderr_detail.empty()) stderr_detail += " | ";
                stderr_detail += l;
            }
            if (msg.empty() && !wst->last_error.empty()) msg = wst->last_error;
        }
        if (!stderr_detail.empty()) {
            msg += " (" + stderr_detail + ")";
        }
        workers_->unregisterWorker(spec.worker_id);
        const bool is_abi = (msg.find("ABI") != std::string::npos ||
                             msg.find("signature") != std::string::npos);
        return fail_record(msg.empty() ? "Subprocess plugin handshake failed" : msg, is_abi);
    }

    const std::string worker_sig = desc.data.value("sdk_abi_signature", "");
    if (worker_sig != omnidesk::sdkAbiSignature()) {
        workers_->unregisterWorker(spec.worker_id);
        return fail_record("Worker SDK ABI signature mismatch (host rebuild mismatch)", true);
    }

    if (desc.data.contains("capabilities") && desc.data["capabilities"].is_array()) {
        for (const auto& c : desc.data["capabilities"]) {
            const std::string cap_id = c.value("capability_id", "");
            if (cap_id.empty()) continue;
            int priority = c.value("priority", 100);
            nlohmann::json meta = c.contains("metadata") && c["metadata"].is_object()
                                      ? c["metadata"]
                                      : nlohmann::json::object();
            meta["isolation"] = "subprocess";
            capabilities_.registerStreamingCapability(
                cap_id, plugin_id,
                makeWorkerStreamCapabilityHandler(*workers_, spec.worker_id, cap_id, "cap.invoke"),
                std::move(meta), priority);
        }
    }

    if (desc.data.contains("media_sources") && desc.data["media_sources"].is_array()) {
        for (const auto& s : desc.data["media_sources"]) {
            const std::string sid = s.value("source_id", "");
            if (sid.empty()) continue;
            const std::string dname = s.value("display_name", sid);
            std::vector<std::string> cats;
            if (s.contains("categories") && s["categories"].is_array()) {
                for (const auto& cat : s["categories"]) {
                    if (cat.is_string()) cats.push_back(cat.get<std::string>());
                }
            }
            domains_.registerMediaSource(
                plugin_id,
                std::make_shared<WorkerMediaSource>(
                    *workers_, spec.worker_id, plugin_id, sid, dname, std::move(cats)));
        }
    }

    if (desc.data.contains("text_sources") && desc.data["text_sources"].is_array()) {
        for (const auto& s : desc.data["text_sources"]) {
            const std::string sid = s.value("source_id", "");
            if (sid.empty()) continue;
            domains_.registerTextSource(
                plugin_id,
                std::make_shared<WorkerTextSource>(
                    *workers_, spec.worker_id, plugin_id, sid,
                    s.value("display_name", sid), s.value("category", "blog")));
        }
    }

    if (desc.data.contains("book_sources") && desc.data["book_sources"].is_array()) {
        for (const auto& s : desc.data["book_sources"]) {
            const std::string sid = s.value("source_id", "");
            if (sid.empty()) continue;
            domains_.registerBookSource(
                plugin_id,
                std::make_shared<WorkerBookSource>(
                    *workers_, spec.worker_id, plugin_id, sid, s.value("display_name", sid)));
        }
    }

    LoadedHandle lh;
    lh.is_subprocess = true;
    lh.worker_id = spec.worker_id;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        loaded_[plugin_id] = std::move(lh);
        last_errors_.erase(plugin_id);
    }

    bus_.publish("plugin.state_changed", {
        {"plugin_id", plugin_id},
        {"loaded", true},
        {"isolation", "subprocess"},
    });
    error.clear();
    return true;
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

    seedPluginConfigDefaults(*rec, config_);

    if (rec->manifest.isolation == PluginIsolation::Subprocess) {
        return loadSubprocessPlugin(*rec, error);
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

    const OmniDeskPluginInfo* info = nullptr;
    std::string entry_error;
    try {
        info = abi_fn();
    } catch (const std::exception& exception) {
        entry_error = std::string("omnidesk_plugin_abi_info threw: ") + exception.what();
    } catch (...) {
        entry_error = "omnidesk_plugin_abi_info threw unknown C++ exception";
    }
    if (!entry_error.empty()) {
        emitFault(plugin_id, "omnidesk_plugin_abi_info", entry_error);
        return fail_with_dlclose(entry_error);
    }
    constexpr size_t kLegacyBaseInfoSize = offsetof(OmniDeskPluginInfo, sdk_abi_signature);
    if (!info || info->abi_version != OMNIDESK_PLUGIN_ABI_VERSION ||
        info->struct_size < kLegacyBaseInfoSize || !info->id ||
        plugin_id != info->id) {
        return fail_with_dlclose("Plugin base ABI handshake failed");
    }
    if (info->struct_size < sizeof(OmniDeskPluginInfo) || !info->sdk_abi_signature ||
        !*info->sdk_abi_signature) {
        const std::string msg =
            "Plugin compiled with legacy OmniDeskPluginInfo layout (struct_size=" +
            std::to_string(info->struct_size) + "); rebuild required";
        bus_.publish("plugin.abi_drift", {{"plugin_id", plugin_id}, {"reason", msg}});
        return fail_with_dlclose(msg);
    }
    if (std::string(info->sdk_abi_signature) != omnidesk::sdkAbiSignature()) {
        const std::string msg =
            std::string("Plugin SDK ABI signature mismatch (rebuild plugin against SDK ") +
            omnidesk::sdkVersion() + "): plugin='" + info->sdk_abi_signature +
            "' host='" + omnidesk::sdkAbiSignature() + "'";
        bus_.publish("plugin.abi_drift", {
            {"plugin_id", plugin_id},
            {"plugin_sig", info->sdk_abi_signature},
            {"host_sig", omnidesk::sdkAbiSignature()},
            {"reason", msg},
        });
        return fail_with_dlclose(msg);
    }

    IPlugin* instance = nullptr;
    try {
        instance = static_cast<IPlugin*>(create_fn());
    } catch (const std::exception& e) {
        entry_error = std::string("omnidesk_plugin_create threw: ") + e.what();
    } catch (...) {
        entry_error = "omnidesk_plugin_create threw unknown C++ exception";
    }
    if (!entry_error.empty()) {
        emitFault(plugin_id, "omnidesk_plugin_create", entry_error);
        return fail_with_dlclose(entry_error);
    }
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

    bool loaded_ok = false;
    std::string load_exc;
    try {
        loaded_ok = instance->onLoad(*ctx);
    } catch (const std::exception& e) {
        load_exc = std::string("IPlugin::onLoad() threw std::exception: ") + e.what();
    } catch (...) {
        load_exc = "IPlugin::onLoad() threw unknown C++ exception";
    }

    if (!loaded_ok) {
        capabilities_.unregisterPlugin(plugin_id);
        if (jobs_) jobs_->cancelByPlugin(plugin_id);
        if (workers_) workers_->stopByPlugin(plugin_id);
        bus_.unsubscribeOwner(plugin_id);
        domains_.unregisterPlugin(plugin_id);
        try {
            destroy_fn(instance);
        } catch (...) {
        }
        const std::string msg = load_exc.empty() ? "IPlugin::onLoad() returned false" : load_exc;
        if (!load_exc.empty()) {
            emitFault(plugin_id, "onLoad", msg);
        }
        return fail_with_dlclose(msg);
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

    bus_.publish("plugin.state_changed", {
        {"plugin_id", plugin_id},
        {"loaded", true},
        {"isolation", "in_process"},
    });
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

    if (lh.is_subprocess) {
        if (workers_) {
            auto wst = workers_->status(lh.worker_id);
            if (wst && wst->running) {
                workers_->request(lh.worker_id, {{"op", "shutdown"}}, nullptr, 500);
            }
            workers_->stopByPlugin(plugin_id);
            workers_->unregisterWorker(lh.worker_id);
        }
        if (jobs_) jobs_->cancelByPlugin("host.reader", true);
        capabilities_.unregisterPlugin(plugin_id);
        if (jobs_) jobs_->cancelByPlugin(plugin_id);
        bus_.unsubscribeOwner(plugin_id);
        domains_.unregisterPlugin(plugin_id);
        bus_.publish("plugin.state_changed", {{"plugin_id", plugin_id}, {"loaded", false}});
        return true;
    }

    if (jobs_) jobs_->cancelByPlugin("host.reader", true);
    capabilities_.unregisterPlugin(plugin_id);
    if (jobs_) jobs_->cancelByPlugin(plugin_id, true);
    if (workers_) workers_->stopByPlugin(plugin_id);
    bus_.unsubscribeOwner(plugin_id);
    domains_.unregisterPlugin(plugin_id);

    const bool still_busy =
        capabilities_.hasInFlight(plugin_id) ||
        domains_.hasInFlight(plugin_id) ||
        (jobs_ && (jobs_->hasActiveJobs(plugin_id) || jobs_->hasActiveJobs("host.reader")));

    if (still_busy) {
        std::fprintf(stderr,
                     "[PluginManager] Plugin '%s' still has active in-flight calls after grace timeout; "
                     "pinning DSO handle instead of dlclose() to prevent SIGSEGV\n",
                     plugin_id.c_str());
        pinInFlightPlugin(lh.dl_handle, lh.instance, std::move(lh.context));
        bus_.publish("plugin.state_changed", {
            {"plugin_id", plugin_id},
            {"loaded", false},
            {"pinned_in_flight", true},
        });
        return true;
    }

    if (lh.instance) {
        try {
            lh.instance->onUnload();
        } catch (const std::exception& e) {
            emitFault(plugin_id, "onUnload", e.what());
        } catch (...) {
            emitFault(plugin_id, "onUnload", "unknown C++ exception in onUnload()");
        }
    }

    if (lh.instance && lh.destroy_fn) {
        try {
            lh.destroy_fn(lh.instance);
        } catch (const std::exception& e) {
            emitFault(plugin_id, "omnidesk_plugin_destroy", e.what());
        } catch (...) {
            emitFault(plugin_id, "omnidesk_plugin_destroy", "unknown C++ exception in destroy()");
        }
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

RuntimePluginStatus PluginManager::buildStatusLocked(const InstalledPluginRecord& rec) const {
    RuntimePluginStatus st;
    st.record = rec;
    auto lit = loaded_.find(rec.manifest.id);
    if (lit != loaded_.end()) {
        st.loaded = true;
        if (lit->second.is_subprocess) {
            if (workers_) {
                if (auto wst = workers_->status(lit->second.worker_id)) {
                    st.healthy = !wst->quarantined;
                    st.recent_logs = wst->recent_stderr;
                    if (wst->quarantined && !wst->last_error.empty()) {
                        st.load_error = wst->last_error;
                    }
                } else {
                    st.healthy = false;
                }
            } else {
                st.healthy = false;
            }
        } else {
            try {
                nlohmann::json hc = lit->second.instance->onHealthCheck();
                st.healthy = hc.value("ok", true);
            } catch (const std::exception& e) {
                st.healthy = false;
                st.load_error = std::string("onHealthCheck threw: ") + e.what();
            } catch (...) {
                st.healthy = false;
                st.load_error = "onHealthCheck threw unknown C++ exception";
            }
            if (lit->second.context) {
                st.recent_logs = lit->second.context->logs();
            }
        }
    } else {
        st.loaded = false;
        auto eit = last_errors_.find(rec.manifest.id);
        if (eit != last_errors_.end()) {
            st.load_error = eit->second;
            st.healthy = false;
        }
    }
    return st;
}

std::vector<RuntimePluginStatus> PluginManager::statuses() const {
    auto installed = installer_.listInstalled();
    std::vector<RuntimePluginStatus> out;
    out.reserve(installed.size());

    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& rec : installed) {
        out.push_back(buildStatusLocked(rec));
    }
    return out;
}

std::optional<RuntimePluginStatus> PluginManager::statusOf(const std::string& plugin_id) const {
    auto rec = installer_.getInstalled(plugin_id);
    if (!rec.has_value()) {
        return std::nullopt;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    return buildStatusLocked(*rec);
}

bool PluginManager::isLoaded(const std::string& plugin_id) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return loaded_.find(plugin_id) != loaded_.end();
}

brls::View* PluginManager::createCustomSettingsView(const std::string& plugin_id) {
    IPlugin* inst = nullptr;
    PluginContext* ctx_ptr = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = loaded_.find(plugin_id);
        if (it == loaded_.end() || it->second.is_subprocess || !it->second.instance ||
            !it->second.context) {
            return nullptr;
        }
        inst = it->second.instance;
        ctx_ptr = it->second.context.get();
    }
    try {
        return inst->createSettingsView(*ctx_ptr);
    } catch (const std::exception& e) {
        emitFault(plugin_id, "createSettingsView", e.what());
        return nullptr;
    } catch (...) {
        emitFault(plugin_id, "createSettingsView", "unknown C++ exception in createSettingsView()");
        return nullptr;
    }
}

} // namespace omnidesk::pm
