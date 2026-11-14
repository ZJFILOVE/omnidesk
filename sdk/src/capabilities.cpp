#include "omnidesk/capabilities.hpp"
#include "shutdown_grace.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
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

struct CapabilityRegistry::FaultDispatcher {
    std::mutex mutex;
    FaultHandler handler;

    void report(const std::string& plugin_id,
                const std::string& component,
                const std::string& error) const {
        FaultHandler cb;
        {
            std::lock_guard lock(const_cast<std::mutex&>(mutex));
            cb = handler;
        }
        if (cb) {
            try {
                cb(plugin_id, component, error);
            } catch (...) {
            }
        }
    }
};

struct CapabilityRegistry::AsyncExecutor {
    struct TaskRecord {
        uint64_t id = 0;
        std::string plugin_hint;
        std::shared_ptr<CancelToken> cancel;
        std::function<void()> fn;
    };

    struct State {
        std::mutex mutex;
        std::condition_variable cv;
        std::condition_variable done_cv;
        std::queue<TaskRecord> queue;
        std::vector<std::thread::id> worker_ids;
        std::unordered_map<uint64_t, std::shared_ptr<CancelToken>> active_tokens;
        std::unordered_map<uint64_t, std::string> active_plugins;
        std::unordered_map<std::string, size_t> in_flight_by_plugin;
        size_t active_workers = 0;
        uint64_t next_id = 1;
        bool stopping = false;
        std::atomic<bool> owner_alive{true};
    };

    struct WorkerEntry {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> exited;
    };

    explicit AsyncExecutor(size_t num_threads = 4) {
        state->active_workers = num_threads;
        workers.reserve(num_threads);
        for (size_t i = 0; i < num_threads; ++i) {
            auto exited = std::make_shared<std::atomic<bool>>(false);
            std::thread t([st = state, exited]() { loop(st, exited); });
            {
                std::lock_guard lock(state->mutex);
                state->worker_ids.push_back(t.get_id());
            }
            workers.push_back(WorkerEntry{std::move(t), std::move(exited)});
        }
    }

    ~AsyncExecutor() {
        shutdown(-1);
    }

    void shutdown(int timeout_ms) {
        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        std::vector<TaskRecord> dropped;
        {
            std::lock_guard lock(state->mutex);
            if (state->stopping) return;
            state->stopping = true;
            state->owner_alive.store(false, std::memory_order_release);
            for (const auto& [_, tok] : state->active_tokens) {
                if (tok) to_cancel.push_back(tok);
            }
            while (!state->queue.empty()) {
                dropped.push_back(std::move(state->queue.front()));
                state->queue.pop();
            }
        }
        dropped.clear();
        for (auto& tok : to_cancel) tok->cancel();
        state->cv.notify_all();

        const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
        const auto self_id = std::this_thread::get_id();
        {
            std::unique_lock lock(state->mutex);
            if (grace_ms > 0) {
                state->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this]() {
                    return state->active_workers == 0;
                });
            }
            if (state->active_workers > 0) {
                std::fprintf(stderr,
                             "[CapabilityRegistry] shutdown grace (%d ms) expired with %zu active worker(s); detaching stuck worker(s)\n",
                             grace_ms, state->active_workers);
            }
        }
        for (auto& w : workers) {
            if (!w.thread.joinable()) continue;
            if (w.thread.get_id() == self_id || !w.exited->load(std::memory_order_acquire)) {
                w.thread.detach();
            } else {
                w.thread.join();
            }
        }
        workers.clear();
    }

    bool isWorkerThread() const {
        const auto self_id = std::this_thread::get_id();
        for (const auto& wid : state->worker_ids) {
            if (wid == self_id) return true;
        }
        return false;
    }

    void submit(const std::string& plugin_hint,
                std::shared_ptr<CancelToken> cancel,
                std::function<void()> fn) {
        std::lock_guard lock(state->mutex);
        if (state->stopping) {
            if (cancel) cancel->cancel();
            return;
        }
        const uint64_t id = state->next_id++;
        state->active_tokens[id] = cancel;
        state->active_plugins[id] = plugin_hint;
        state->queue.push(TaskRecord{id, plugin_hint, std::move(cancel), std::move(fn)});
        state->cv.notify_one();
    }

    void cancelAndWaitPlugin(const std::string& plugin_id, int timeout_ms = -1) {
        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        {
            std::lock_guard lock(state->mutex);
            for (const auto& [id, pid] : state->active_plugins) {
                if (pid.empty() || pid == plugin_id) {
                    auto it = state->active_tokens.find(id);
                    if (it != state->active_tokens.end() && it->second) {
                        to_cancel.push_back(it->second);
                    }
                }
            }
        }
        for (auto& tok : to_cancel) tok->cancel();
        if (isWorkerThread()) return;

        std::unique_lock lock(state->mutex);
        if (state->stopping) return;
        const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
        if (grace_ms > 0) {
            state->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this, &plugin_id]() {
                auto it = state->in_flight_by_plugin.find(plugin_id);
                return it == state->in_flight_by_plugin.end() || it->second == 0;
            });
        }
    }

    bool hasInFlight(const std::string& plugin_id) const {
        if (plugin_id.empty()) return false;
        std::lock_guard lock(state->mutex);
        auto it = state->in_flight_by_plugin.find(plugin_id);
        return it != state->in_flight_by_plugin.end() && it->second > 0;
    }

    static void trackPluginStart(const std::shared_ptr<State>& st, const std::string& plugin_id) {
        if (!st || plugin_id.empty()) return;
        std::lock_guard lock(st->mutex);
        ++st->in_flight_by_plugin[plugin_id];
    }

    static void trackPluginEnd(const std::shared_ptr<State>& st, const std::string& plugin_id) {
        if (!st || plugin_id.empty()) return;
        std::lock_guard lock(st->mutex);
        auto it = st->in_flight_by_plugin.find(plugin_id);
        if (it != st->in_flight_by_plugin.end() && it->second > 0) {
            --it->second;
            if (it->second == 0) {
                st->in_flight_by_plugin.erase(it);
                st->done_cv.notify_all();
            }
        }
    }

    static void loop(std::shared_ptr<State> st, std::shared_ptr<std::atomic<bool>> exited) {
        struct ExitGuard {
            std::shared_ptr<State> state_ptr;
            std::shared_ptr<std::atomic<bool>> exit_flag;
            ~ExitGuard() {
                {
                    std::lock_guard lock(state_ptr->mutex);
                    if (state_ptr->active_workers > 0) --state_ptr->active_workers;
                    state_ptr->done_cv.notify_all();
                }
                exit_flag->store(true, std::memory_order_release);
            }
        } exit_guard{st, exited};

        while (true) {
            TaskRecord task;
            {
                std::unique_lock lock(st->mutex);
                st->cv.wait(lock, [&st]() { return st->stopping || !st->queue.empty(); });
                if (st->stopping && st->queue.empty()) return;
                task = std::move(st->queue.front());
                st->queue.pop();
            }
            try {
                if (task.fn) task.fn();
            } catch (...) {
            }
            task.fn = {};
            {
                std::lock_guard lock(st->mutex);
                st->active_tokens.erase(task.id);
                st->active_plugins.erase(task.id);
            }
        }
    }

    std::shared_ptr<State> state = std::make_shared<State>();
    std::vector<WorkerEntry> workers;
};

CapabilityRegistry::CapabilityRegistry()
    : fault_dispatcher_(std::make_shared<FaultDispatcher>()),
      executor_(std::make_unique<AsyncExecutor>(4)) {}

CapabilityRegistry::~CapabilityRegistry() {
    shutdown(-1);
}

void CapabilityRegistry::shutdown(int timeout_ms) {
    if (fault_dispatcher_) {
        std::lock_guard lock(fault_dispatcher_->mutex);
        fault_dispatcher_->handler = {};
    }
    if (executor_) {
        executor_->shutdown(timeout_ms);
    }
}

bool CapabilityRegistry::hasInFlight(const std::string& plugin_id) const {
    return executor_ ? executor_->hasInFlight(plugin_id) : false;
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

    auto exec_state = executor_ ? executor_->state : nullptr;
    auto fault_disp = fault_dispatcher_;
    AsyncExecutor::trackPluginStart(exec_state, pid);
    struct FlightGuard {
        std::shared_ptr<AsyncExecutor::State> st;
        std::string plugin;
        ~FlightGuard() {
            AsyncExecutor::trackPluginEnd(st, plugin);
        }
    } guard{exec_state, pid};

    std::shared_ptr<CancelToken> eff_cancel = cancel ? cancel : CancelToken::create();
    CapabilityResult res;
    auto safeChunk = [&](const StreamChunk& chunk) {
        if (!on_chunk) return;
        try {
            on_chunk(chunk);
        } catch (...) {
        }
    };

    if (stream_fn && (stream_mode || cancel != nullptr)) {
        bool saw_done = false;
        StreamCallback wrapped = [&](const StreamChunk& chunk) {
            if (chunk.done) saw_done = true;
            if (on_chunk && (!eff_cancel->isCancelled() || chunk.done)) {
                safeChunk(chunk);
            }
        };
        try {
            res = stream_fn(request, eff_cancel, stream_mode ? wrapped : StreamCallback{});
        } catch (const std::exception& e) {
            res.ok = false;
            res.error = std::string("plugin capability threw exception: ") + e.what();
            if (fault_disp) fault_disp->report(pid, capability_id, res.error);
        } catch (...) {
            res.ok = false;
            res.error = "plugin capability threw unknown exception";
            if (fault_disp) fault_disp->report(pid, capability_id, res.error);
        }
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
            safeChunk(terminal);
        }
    } else {
        try {
            res = sync_fn(request);
        } catch (const std::exception& e) {
            res.ok = false;
            res.error = std::string("plugin capability threw exception: ") + e.what();
            if (fault_disp) fault_disp->report(pid, capability_id, res.error);
        } catch (...) {
            res.ok = false;
            res.error = "plugin capability threw unknown exception";
            if (fault_disp) fault_disp->report(pid, capability_id, res.error);
        }
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
                safeChunk(c);
            }
            StreamChunk terminal;
            terminal.done = true;
            terminal.data = res.data;
            terminal.event = res.ok ? "done" : "error";
            safeChunk(terminal);
        }
    }

    if (res.provider_plugin_id.empty()) res.provider_plugin_id = pid;
    return res;
}

void CapabilityRegistry::setFaultHandler(FaultHandler handler) {
    std::lock_guard lock(fault_dispatcher_->mutex);
    fault_dispatcher_->handler = std::move(handler);
}

void CapabilityRegistry::reportFault(const std::string& plugin_id,
                                     const std::string& component,
                                     const std::string& error) const {
    if (fault_dispatcher_) fault_dispatcher_->report(plugin_id, component, error);
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
    auto exec_state = executor_->state;
    executor_->submit(plugin_id, cancel, [this, exec_state, plugin_id, capability_id, request,
                                          on_complete = std::move(on_complete), cancel]() {
        auto res = invokeInternal(plugin_id, capability_id, request, {}, cancel, false);
        if (on_complete && exec_state->owner_alive.load(std::memory_order_acquire)) on_complete(res);
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
    auto exec_state = executor_->state;
    executor_->submit(plugin_id, cancel, [this, exec_state, plugin_id, capability_id, request,
                                          on_chunk = std::move(on_chunk),
                                          on_complete = std::move(on_complete), cancel]() {
        auto res = invokeInternal(plugin_id, capability_id, request, on_chunk, cancel, true);
        if (on_complete && exec_state->owner_alive.load(std::memory_order_acquire)) on_complete(res);
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
    for (const auto& cb : snapshot) {
        try {
            cb();
        } catch (...) {
        }
    }
}

} // namespace omnidesk
