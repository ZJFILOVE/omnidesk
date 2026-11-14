#include "omnidesk/capabilities.hpp"

#include <algorithm>
#include <condition_variable>
#include <queue>
#include <thread>
#include <unordered_set>

namespace omnidesk {

namespace {

std::string extractTextFallback(const nlohmann::json& data) {
    if (!data.is_object()) return {};
    for (const char* key : {"answer", "text", "reply", "content", "delta", "message"}) {
        if (data.contains(key) && data[key].is_string()) {
            return data[key].get<std::string>();
        }
    }
    return {};
}

}  // namespace

struct CapabilityRegistry::AsyncExecutor {
    struct TaskRecord {
        uint64_t id = 0;
        std::string plugin_hint;
        std::shared_ptr<CancelToken> cancel;
        std::function<void()> fn;
    };

    explicit AsyncExecutor(size_t num_threads = 4) {
        workers.reserve(num_threads);
        for (size_t i = 0; i < num_threads; ++i) {
            workers.emplace_back([this]() { loop(); });
        }
    }

    ~AsyncExecutor() {
        shutdown();
    }

    void shutdown() {
        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        {
            std::lock_guard lock(mutex);
            if (stopping) return;
            stopping = true;
            for (const auto& [_, tok] : active_tokens) {
                if (tok) to_cancel.push_back(tok);
            }
        }
        for (auto& tok : to_cancel) tok->cancel();
        cv.notify_all();
        const auto self_id = std::this_thread::get_id();
        for (auto& t : workers) {
            if (t.joinable()) {
                if (t.get_id() == self_id) t.detach();
                else t.join();
            }
        }
        workers.clear();
    }

    bool isWorkerThread() const {
        const auto self_id = std::this_thread::get_id();
        for (const auto& t : workers) {
            if (t.get_id() == self_id) return true;
        }
        return false;
    }

    void submit(const std::string& plugin_hint,
                std::shared_ptr<CancelToken> cancel,
                std::function<void()> fn) {
        std::lock_guard lock(mutex);
        if (stopping) {
            if (cancel) cancel->cancel();
            return;
        }
        const uint64_t id = next_id++;
        active_tokens[id] = cancel;
        active_plugins[id] = plugin_hint;
        queue.push(TaskRecord{id, plugin_hint, std::move(cancel), std::move(fn)});
        cv.notify_one();
    }

    void cancelAndWaitPlugin(const std::string& plugin_id) {
        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        {
            std::lock_guard lock(mutex);
            for (const auto& [id, pid] : active_plugins) {
                if (pid.empty() || pid == plugin_id) {
                    auto it = active_tokens.find(id);
                    if (it != active_tokens.end() && it->second) {
                        to_cancel.push_back(it->second);
                    }
                }
            }
        }
        for (auto& tok : to_cancel) tok->cancel();
        if (isWorkerThread()) return;

        std::unique_lock lock(mutex);
        done_cv.wait(lock, [this, &plugin_id]() {
            return in_flight_by_plugin[plugin_id] == 0;
        });
    }

    void trackPluginStart(const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::lock_guard lock(mutex);
        ++in_flight_by_plugin[plugin_id];
    }

    void trackPluginEnd(const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::lock_guard lock(mutex);
        auto it = in_flight_by_plugin.find(plugin_id);
        if (it != in_flight_by_plugin.end() && it->second > 0) {
            --it->second;
            if (it->second == 0) done_cv.notify_all();
        }
    }

    void loop() {
        while (true) {
            TaskRecord task;
            {
                std::unique_lock lock(mutex);
                cv.wait(lock, [this]() { return stopping || !queue.empty(); });
                if (stopping && queue.empty()) return;
                task = std::move(queue.front());
                queue.pop();
            }
            try {
                if (task.fn) task.fn();
            } catch (...) {
            }
            {
                std::lock_guard lock(mutex);
                active_tokens.erase(task.id);
                active_plugins.erase(task.id);
            }
        }
    }

    std::mutex mutex;
    std::condition_variable cv;
    std::condition_variable done_cv;
    std::queue<TaskRecord> queue;
    std::vector<std::thread> workers;
    std::unordered_map<uint64_t, std::shared_ptr<CancelToken>> active_tokens;
    std::unordered_map<uint64_t, std::string> active_plugins;
    std::unordered_map<std::string, size_t> in_flight_by_plugin;
    uint64_t next_id = 1;
    bool stopping = false;
};

CapabilityRegistry::CapabilityRegistry() : executor_(std::make_unique<AsyncExecutor>(4)) {}

CapabilityRegistry::~CapabilityRegistry() {
    if (executor_) executor_->shutdown();
}

bool CapabilityRegistry::registerCapability(const std::string& capability_id,
                                            const std::string& plugin_id,
                                            CapabilityHandler handler,
                                            nlohmann::json metadata,
                                            int priority) {
    if (capability_id.empty() || plugin_id.empty() || !handler) {
        return false;
    }
    {
        std::unique_lock lock(mutex_);
        auto& list = entries_[capability_id];
        list.erase(std::remove_if(list.begin(), list.end(), [&](const Entry& e) {
            return e.desc.plugin_id == plugin_id;
        }), list.end());
        Entry entry;
        entry.desc.capability_id = capability_id;
        entry.desc.plugin_id = plugin_id;
        entry.desc.priority = priority;
        entry.desc.metadata = std::move(metadata);
        entry.handler = std::move(handler);
        list.push_back(std::move(entry));
        std::stable_sort(list.begin(), list.end(), [](const Entry& a, const Entry& b) {
            return a.desc.priority > b.desc.priority;
        });
    }
    notifyChanged();
    return true;
}

bool CapabilityRegistry::registerStreamingCapability(const std::string& capability_id,
                                                     const std::string& plugin_id,
                                                     CapabilityStreamHandler handler,
                                                     nlohmann::json metadata,
                                                     int priority) {
    if (capability_id.empty() || plugin_id.empty() || !handler) {
        return false;
    }
    if (!metadata.is_object()) metadata = nlohmann::json::object();
    if (!metadata.contains("streaming")) metadata["streaming"] = true;

    CapabilityHandler sync_bridge = [handler](const nlohmann::json& req) -> CapabilityResult {
        auto tok = CancelToken::create();
        std::string accumulated;
        auto res = handler(req, tok, [&](const StreamChunk& chunk) {
            accumulated += chunk.delta;
        });
        if (!accumulated.empty() && res.data.is_object() &&
            !res.data.contains("answer") && !res.data.contains("text")) {
            res.data["text"] = accumulated;
        }
        return res;
    };

    {
        std::unique_lock lock(mutex_);
        auto& list = entries_[capability_id];
        list.erase(std::remove_if(list.begin(), list.end(), [&](const Entry& e) {
            return e.desc.plugin_id == plugin_id;
        }), list.end());
        Entry entry;
        entry.desc.capability_id = capability_id;
        entry.desc.plugin_id = plugin_id;
        entry.desc.priority = priority;
        entry.desc.metadata = std::move(metadata);
        entry.handler = std::move(sync_bridge);
        entry.stream_handler = std::move(handler);
        list.push_back(std::move(entry));
        std::stable_sort(list.begin(), list.end(), [](const Entry& a, const Entry& b) {
            return a.desc.priority > b.desc.priority;
        });
    }
    notifyChanged();
    return true;
}

void CapabilityRegistry::unregisterPlugin(const std::string& plugin_id) {
    bool changed = false;
    {
        std::unique_lock lock(mutex_);
        for (auto it = entries_.begin(); it != entries_.end();) {
            auto& list = it->second;
            size_t before = list.size();
            list.erase(std::remove_if(list.begin(), list.end(), [&](const Entry& e) {
                return e.desc.plugin_id == plugin_id;
            }), list.end());
            if (list.size() != before) changed = true;
            if (list.empty()) it = entries_.erase(it);
            else ++it;
        }
    }
    if (executor_) {
        executor_->cancelAndWaitPlugin(plugin_id);
    }
    if (changed) notifyChanged();
}

bool CapabilityRegistry::unregisterCapability(const std::string& capability_id,
                                              const std::string& plugin_id) {
    bool changed = false;
    {
        std::unique_lock lock(mutex_);
        auto it = entries_.find(capability_id);
        if (it != entries_.end()) {
            auto& list = it->second;
            size_t before = list.size();
            list.erase(std::remove_if(list.begin(), list.end(), [&](const Entry& e) {
                return e.desc.plugin_id == plugin_id;
            }), list.end());
            changed = list.size() != before;
            if (list.empty()) entries_.erase(it);
        }
    }
    if (changed) notifyChanged();
    return changed;
}

bool CapabilityRegistry::has(const std::string& capability_id) const {
    std::shared_lock lock(mutex_);
    auto it = entries_.find(capability_id);
    return it != entries_.end() && !it->second.empty();
}

std::vector<CapabilityDescriptor> CapabilityRegistry::providersFor(
    const std::string& capability_id) const {
    std::shared_lock lock(mutex_);
    std::vector<CapabilityDescriptor> out;
    auto it = entries_.find(capability_id);
    if (it != entries_.end()) {
        for (const auto& e : it->second) out.push_back(e.desc);
    }
    return out;
}

std::vector<CapabilityDescriptor> CapabilityRegistry::listAll() const {
    std::shared_lock lock(mutex_);
    std::vector<CapabilityDescriptor> out;
    for (const auto& [_, list] : entries_) {
        for (const auto& e : list) out.push_back(e.desc);
    }
    return out;
}

CapabilityResult CapabilityRegistry::invokeInternal(const std::string& plugin_id_filter,
                                                    const std::string& capability_id,
                                                    const nlohmann::json& request,
                                                    const StreamCallback& on_chunk,
                                                    const std::shared_ptr<CancelToken>& cancel,
                                                    bool stream_mode) const {
    if (cancel && cancel->isCancelled()) {
        return {false, nlohmann::json::object(), "cancelled", plugin_id_filter, true};
    }

    CapabilityHandler sync_fn;
    CapabilityStreamHandler stream_fn;
    std::string pid;
    {
        std::shared_lock lock(mutex_);
        auto it = entries_.find(capability_id);
        if (it != entries_.end() && !it->second.empty()) {
            for (const auto& e : it->second) {
                if (plugin_id_filter.empty() || e.desc.plugin_id == plugin_id_filter) {
                    sync_fn = e.handler;
                    stream_fn = e.stream_handler;
                    pid = e.desc.plugin_id;
                    break;
                }
            }
        }
    }

    if (!sync_fn && !stream_fn) {
        std::string err = plugin_id_filter.empty()
                              ? ("capability not available: " + capability_id)
                              : ("provider '" + plugin_id_filter + "' missing capability '" + capability_id + "'");
        return {false, nlohmann::json::object(), std::move(err), "", false};
    }

    if (executor_) executor_->trackPluginStart(pid);
    struct FlightGuard {
        AsyncExecutor* exec;
        std::string plugin;
        ~FlightGuard() {
            if (exec) exec->trackPluginEnd(plugin);
        }
    } guard{executor_.get(), pid};

    std::shared_ptr<CancelToken> eff_cancel = cancel ? cancel : CancelToken::create();
    CapabilityResult res;
    if (stream_fn && (stream_mode || cancel != nullptr)) {
        bool saw_done = false;
        StreamCallback wrapped = [&](const StreamChunk& chunk) {
            if (chunk.done) saw_done = true;
            if (on_chunk && (!eff_cancel->isCancelled() || chunk.done)) {
                on_chunk(chunk);
            }
        };
        res = stream_fn(request, eff_cancel, stream_mode ? wrapped : StreamCallback{});
        if (eff_cancel->isCancelled()) {
            res.cancelled = true;
            res.ok = false;
            if (res.error.empty()) res.error = "cancelled";
        }
        if (stream_mode && !saw_done && on_chunk) {
            StreamChunk terminal;
            terminal.done = true;
            terminal.data = res.data;
            terminal.event = res.cancelled ? "cancelled" : (res.ok ? "done" : "error");
            on_chunk(terminal);
        }
    } else {
        res = sync_fn(request);
        if (eff_cancel->isCancelled()) {
            res.cancelled = true;
            res.ok = false;
            if (res.error.empty()) res.error = "cancelled";
        } else if (stream_mode && on_chunk) {
            std::string txt = extractTextFallback(res.data);
            if (!txt.empty()) {
                StreamChunk c;
                c.delta = std::move(txt);
                c.data = res.data;
                c.event = "delta";
                c.done = false;
                on_chunk(c);
            }
            StreamChunk terminal;
            terminal.done = true;
            terminal.data = res.data;
            terminal.event = res.ok ? "done" : "error";
            on_chunk(terminal);
        }
    }

    if (res.provider_plugin_id.empty()) res.provider_plugin_id = pid;
    return res;
}

CapabilityResult CapabilityRegistry::call(const std::string& capability_id,
                                          const nlohmann::json& request) const {
    return invokeInternal("", capability_id, request, {}, nullptr, false);
}

CapabilityResult CapabilityRegistry::callProvider(const std::string& plugin_id,
                                                  const std::string& capability_id,
                                                  const nlohmann::json& request) const {
    return invokeInternal(plugin_id, capability_id, request, {}, nullptr, false);
}

CapabilityResult CapabilityRegistry::callCancellable(const std::string& capability_id,
                                                     const nlohmann::json& request,
                                                     const std::shared_ptr<CancelToken>& cancel) const {
    return invokeInternal("", capability_id, request, {}, cancel, false);
}

CapabilityResult CapabilityRegistry::callStream(const std::string& capability_id,
                                                const nlohmann::json& request,
                                                const StreamCallback& on_chunk,
                                                const std::shared_ptr<CancelToken>& cancel) const {
    return invokeInternal("", capability_id, request, on_chunk, cancel, true);
}

CapabilityResult CapabilityRegistry::callProviderStream(const std::string& plugin_id,
                                                        const std::string& capability_id,
                                                        const nlohmann::json& request,
                                                        const StreamCallback& on_chunk,
                                                        const std::shared_ptr<CancelToken>& cancel) const {
    return invokeInternal(plugin_id, capability_id, request, on_chunk, cancel, true);
}

std::shared_ptr<CancelToken> CapabilityRegistry::callAsync(const std::string& capability_id,
                                                           const nlohmann::json& request,
                                                           CapabilityAsyncCallback on_complete,
                                                           std::shared_ptr<CancelToken> cancel) const {
    return callProviderAsync("", capability_id, request, std::move(on_complete), std::move(cancel));
}

std::shared_ptr<CancelToken> CapabilityRegistry::callProviderAsync(const std::string& plugin_id,
                                                                   const std::string& capability_id,
                                                                   const nlohmann::json& request,
                                                                   CapabilityAsyncCallback on_complete,
                                                                   std::shared_ptr<CancelToken> cancel) const {
    if (!cancel) cancel = CancelToken::create();
    executor_->submit(plugin_id, cancel, [this, plugin_id, capability_id, request,
                                          on_complete = std::move(on_complete), cancel]() {
        auto res = invokeInternal(plugin_id, capability_id, request, {}, cancel, false);
        if (on_complete) on_complete(res);
    });
    return cancel;
}

std::shared_ptr<CancelToken> CapabilityRegistry::callStreamAsync(const std::string& capability_id,
                                                                 const nlohmann::json& request,
                                                                 StreamCallback on_chunk,
                                                                 CapabilityAsyncCallback on_complete,
                                                                 std::shared_ptr<CancelToken> cancel) const {
    return callProviderStreamAsync("", capability_id, request,
                                   std::move(on_chunk), std::move(on_complete), std::move(cancel));
}

std::shared_ptr<CancelToken> CapabilityRegistry::callProviderStreamAsync(
    const std::string& plugin_id,
    const std::string& capability_id,
    const nlohmann::json& request,
    StreamCallback on_chunk,
    CapabilityAsyncCallback on_complete,
    std::shared_ptr<CancelToken> cancel) const {
    if (!cancel) cancel = CancelToken::create();
    executor_->submit(plugin_id, cancel, [this, plugin_id, capability_id, request,
                                          on_chunk = std::move(on_chunk),
                                          on_complete = std::move(on_complete), cancel]() {
        auto res = invokeInternal(plugin_id, capability_id, request, on_chunk, cancel, true);
        if (on_complete) on_complete(res);
    });
    return cancel;
}

uint64_t CapabilityRegistry::subscribeChanges(ChangeCallback cb) {
    std::lock_guard lock(listeners_mutex_);
    uint64_t token = next_token_++;
    listeners_[token] = std::move(cb);
    return token;
}

void CapabilityRegistry::unsubscribeChanges(uint64_t token) {
    std::lock_guard lock(listeners_mutex_);
    listeners_.erase(token);
}

void CapabilityRegistry::notifyChanged() {
    std::vector<ChangeCallback> snapshot;
    {
        std::lock_guard lock(listeners_mutex_);
        for (const auto& [_, cb] : listeners_) {
            if (cb) snapshot.push_back(cb);
        }
    }
    for (const auto& cb : snapshot) cb();
}

} // namespace omnidesk
