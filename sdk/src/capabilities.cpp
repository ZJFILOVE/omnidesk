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
        std::unordered_map<std::string, std::unordered_map<uint64_t, std::shared_ptr<CancelToken>>>
            in_flight_tokens_by_plugin;
        std::unordered_map<std::string, size_t> in_flight_by_plugin;
        std::unordered_map<std::string, size_t> cancelling_plugins;
        size_t active_workers = 0;
        uint64_t next_id = 1;
        bool stopping = false;
        std::atomic<bool> owner_alive{true};

        mutable std::shared_mutex registry_mutex;
        std::unordered_map<std::string, std::vector<Entry>> entries;
        std::shared_ptr<RemoteBridge> remote_bridge;
        std::shared_ptr<FaultDispatcher> fault_dispatcher;
    };

    struct WorkerEntry {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> exited;
    };

    struct FlightGuard {
        std::shared_ptr<State> st;
        std::string plugin;
        uint64_t flight_id = 0;

        FlightGuard() = default;
        FlightGuard(std::shared_ptr<State> s,
                    std::string pid,
                    const std::shared_ptr<CancelToken>& tok)
            : st(std::move(s)), plugin(std::move(pid)) {
            if (!st || plugin.empty()) {
                st.reset();
                plugin.clear();
                return;
            }
            bool cancel_now = false;
            {
                std::lock_guard lock(st->mutex);
                flight_id = st->next_id++;
                ++st->in_flight_by_plugin[plugin];
                if (tok) {
                    st->in_flight_tokens_by_plugin[plugin][flight_id] = tok;
                }
                if (st->stopping ||
                    st->cancelling_plugins.find(plugin) != st->cancelling_plugins.end()) {
                    cancel_now = true;
                }
            }
            if (cancel_now && tok) {
                tok->cancel();
            }
        }

        FlightGuard(const FlightGuard&) = delete;
        FlightGuard& operator=(const FlightGuard&) = delete;

        FlightGuard(FlightGuard&& other) noexcept
            : st(std::move(other.st)),
              plugin(std::move(other.plugin)),
              flight_id(other.flight_id) {
            other.flight_id = 0;
            other.plugin.clear();
        }

        FlightGuard& operator=(FlightGuard&& other) noexcept {
            if (this != &other) {
                release();
                st = std::move(other.st);
                plugin = std::move(other.plugin);
                flight_id = other.flight_id;
                other.flight_id = 0;
                other.plugin.clear();
            }
            return *this;
        }

        void release() {
            if (!st || plugin.empty() || flight_id == 0) return;
            {
                std::lock_guard lock(st->mutex);
                auto tok_it = st->in_flight_tokens_by_plugin.find(plugin);
                if (tok_it != st->in_flight_tokens_by_plugin.end()) {
                    tok_it->second.erase(flight_id);
                    if (tok_it->second.empty()) {
                        st->in_flight_tokens_by_plugin.erase(tok_it);
                    }
                }
                auto cnt_it = st->in_flight_by_plugin.find(plugin);
                if (cnt_it != st->in_flight_by_plugin.end() && cnt_it->second > 0) {
                    --cnt_it->second;
                    if (cnt_it->second == 0) {
                        st->in_flight_by_plugin.erase(cnt_it);
                        st->done_cv.notify_all();
                    }
                }
            }
            st.reset();
            plugin.clear();
            flight_id = 0;
        }

        ~FlightGuard() {
            release();
        }
    };

    explicit AsyncExecutor(std::shared_ptr<FaultDispatcher> fault_disp, size_t num_threads = 4) {
        state->fault_dispatcher = std::move(fault_disp);
        std::lock_guard lock(state->mutex);
        state->active_workers = num_threads;
        state->worker_ids.reserve(num_threads);
        workers.reserve(num_threads);
        for (size_t i = 0; i < num_threads; ++i) {
            auto exited = std::make_shared<std::atomic<bool>>(false);
            std::thread t([st = state, exited]() { loop(st, exited); });
            state->worker_ids.push_back(t.get_id());
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
            for (const auto& [_, tok_map] : state->in_flight_tokens_by_plugin) {
                for (const auto& [__, tok] : tok_map) {
                    if (tok) to_cancel.push_back(tok);
                }
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
        std::lock_guard lock(state->mutex);
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
        if (plugin_id.empty()) return;

        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        {
            std::lock_guard lock(state->mutex);
            ++state->cancelling_plugins[plugin_id];
            for (const auto& [id, pid] : state->active_plugins) {
                if (pid == plugin_id) {
                    auto it = state->active_tokens.find(id);
                    if (it != state->active_tokens.end() && it->second) {
                        to_cancel.push_back(it->second);
                    }
                }
            }
            auto f_it = state->in_flight_tokens_by_plugin.find(plugin_id);
            if (f_it != state->in_flight_tokens_by_plugin.end()) {
                for (const auto& [_, tok] : f_it->second) {
                    if (tok) to_cancel.push_back(tok);
                }
            }
        }

        struct CancellingGuard {
            std::shared_ptr<State> st;
            std::string pid;
            ~CancellingGuard() {
                std::lock_guard lock(st->mutex);
                auto it = st->cancelling_plugins.find(pid);
                if (it != st->cancelling_plugins.end()) {
                    if (it->second <= 1) {
                        st->cancelling_plugins.erase(it);
                    } else {
                        --it->second;
                    }
                }
            }
        } cancelling_guard{state, plugin_id};

        for (auto& tok : to_cancel) tok->cancel();
        if (isWorkerThread()) return;

        std::unique_lock lock(state->mutex);
        if (state->stopping) return;
        const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
        if (grace_ms > 0) {
            state->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this, &plugin_id]() {
                auto it = state->in_flight_by_plugin.find(plugin_id);
                return state->stopping || it == state->in_flight_by_plugin.end() || it->second == 0;
            });
        }
    }

    bool hasInFlight(const std::string& plugin_id) const {
        if (plugin_id.empty()) return false;
        std::lock_guard lock(state->mutex);
        auto it = state->in_flight_by_plugin.find(plugin_id);
        return it != state->in_flight_by_plugin.end() && it->second > 0;
    }

    static CapabilityResult invoke(const std::shared_ptr<State>& st,
                                   const std::string& plugin_id_filter,
                                   const std::string& capability_id,
                                   const nlohmann::json& request,
                                   const StreamCallback& on_chunk,
                                   const std::shared_ptr<CancelToken>& cancel,
                                   bool stream_mode) {
        if (!st) {
            return {false, nlohmann::json::object(), "capability registry unavailable", "", false};
        }
        if (cancel && cancel->isCancelled()) {
            return {false, nlohmann::json::object(), "cancelled", plugin_id_filter, true};
        }

        std::shared_ptr<CancelToken> eff_cancel = cancel ? cancel : CancelToken::create();

        CapabilityHandler sync_fn;
        CapabilityStreamHandler stream_fn;
        std::string pid;
        int local_priority = 0;
        std::shared_ptr<RemoteBridge> bridge;
        std::shared_ptr<FaultDispatcher> fault_disp;
        FlightGuard flight_guard;
        {
            std::shared_lock lock(st->registry_mutex);
            bridge = st->remote_bridge;
            fault_disp = st->fault_dispatcher;
            auto it = st->entries.find(capability_id);
            if (it != st->entries.end() && !it->second.empty()) {
                for (const auto& e : it->second) {
                    if (plugin_id_filter.empty() || e.desc.plugin_id == plugin_id_filter) {
                        sync_fn = e.handler;
                        stream_fn = e.stream_handler;
                        pid = e.desc.plugin_id;
                        local_priority = e.desc.priority;
                        flight_guard = FlightGuard(st, pid, eff_cancel);
                        break;
                    }
                }
            }
        }

        if (bridge && bridge->invoke) {
            bool use_remote = (!sync_fn && !stream_fn);
            if (!use_remote && plugin_id_filter.empty() && bridge->providers_for) {
                try {
                    auto remote_providers = bridge->providers_for(capability_id);
                    if (!remote_providers.empty() &&
                        remote_providers[0].plugin_id != pid &&
                        remote_providers[0].priority > local_priority) {
                        use_remote = true;
                    }
                } catch (...) {
                }
            }
            if (use_remote) {
                flight_guard.release();
                try {
                    return bridge->invoke(
                        plugin_id_filter, capability_id, request, on_chunk, eff_cancel, stream_mode);
                } catch (const std::exception& e) {
                    return {false, nlohmann::json::object(),
                            std::string("remote capability bridge threw: ") + e.what(),
                            plugin_id_filter, false};
                } catch (...) {
                    return {false, nlohmann::json::object(),
                            "remote capability bridge threw unknown exception",
                            plugin_id_filter, false};
                }
            }
        }

        if (!sync_fn && !stream_fn) {
            std::string err = plugin_id_filter.empty()
                                  ? ("capability not available: " + capability_id)
                                  : ("provider '" + plugin_id_filter + "' missing capability '" +
                                     capability_id + "'");
            return {false, nlohmann::json::object(), std::move(err), "", false};
        }

        if (eff_cancel->isCancelled()) {
            return {false, nlohmann::json::object(), "cancelled", pid, true};
        }

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
        } else if (stream_fn) {
            std::string accumulated;
            try {
                res = stream_fn(request, eff_cancel, [&](const StreamChunk& chunk) {
                    accumulated += chunk.delta;
                });
                if (!accumulated.empty() && res.data.is_object() &&
                    !res.data.contains("answer") && !res.data.contains("text")) {
                    res.data["text"] = accumulated;
                }
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
      executor_(std::make_unique<AsyncExecutor>(fault_dispatcher_, 4)) {}

CapabilityRegistry::~CapabilityRegistry() {
    shutdown(-1);
}

void CapabilityRegistry::shutdown(int timeout_ms) {
    if (executor_ && executor_->state) {
        std::unique_lock lock(executor_->state->registry_mutex);
        executor_->state->remote_bridge.reset();
    }
    if (fault_dispatcher_) {
        std::lock_guard lock(fault_dispatcher_->mutex);
        fault_dispatcher_->handler = {};
    }
    if (executor_) {
        executor_->shutdown(timeout_ms);
    }
}

void CapabilityRegistry::setRemoteBridge(std::shared_ptr<RemoteBridge> bridge) {
    if (executor_ && executor_->state) {
        std::unique_lock lock(executor_->state->registry_mutex);
        executor_->state->remote_bridge = std::move(bridge);
    }
    notifyChanged();
}

void CapabilityRegistry::notifyRemoteChanged() {
    notifyChanged();
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
        std::unique_lock lock(executor_->state->registry_mutex);
        auto& list = executor_->state->entries[capability_id];
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
        std::unique_lock lock(executor_->state->registry_mutex);
        auto& list = executor_->state->entries[capability_id];
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
    if (plugin_id.empty()) return;
    bool changed = false;
    {
        std::unique_lock lock(executor_->state->registry_mutex);
        auto& entries = executor_->state->entries;
        for (auto it = entries.begin(); it != entries.end();) {
            auto& list = it->second;
            size_t before = list.size();
            list.erase(std::remove_if(list.begin(), list.end(), [&](const Entry& e) {
                return e.desc.plugin_id == plugin_id;
            }), list.end());
            if (list.size() != before) changed = true;
            if (list.empty()) it = entries.erase(it);
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
        std::unique_lock lock(executor_->state->registry_mutex);
        auto& entries = executor_->state->entries;
        auto it = entries.find(capability_id);
        if (it != entries.end()) {
            auto& list = it->second;
            size_t before = list.size();
            list.erase(std::remove_if(list.begin(), list.end(), [&](const Entry& e) {
                return e.desc.plugin_id == plugin_id;
            }), list.end());
            changed = list.size() != before;
            if (list.empty()) entries.erase(it);
        }
    }
    if (changed) notifyChanged();
    return changed;
}

bool CapabilityRegistry::has(const std::string& capability_id) const {
    std::shared_ptr<RemoteBridge> bridge;
    {
        std::shared_lock lock(executor_->state->registry_mutex);
        const auto& entries = executor_->state->entries;
        auto it = entries.find(capability_id);
        if (it != entries.end() && !it->second.empty()) {
            return true;
        }
        bridge = executor_->state->remote_bridge;
    }
    if (bridge && bridge->has) {
        try {
            return bridge->has(capability_id);
        } catch (...) {
            return false;
        }
    }
    return false;
}

std::vector<CapabilityDescriptor> CapabilityRegistry::providersFor(
    const std::string& capability_id) const {
    std::vector<CapabilityDescriptor> out;
    std::shared_ptr<RemoteBridge> bridge;
    {
        std::shared_lock lock(executor_->state->registry_mutex);
        const auto& entries = executor_->state->entries;
        auto it = entries.find(capability_id);
        if (it != entries.end()) {
            for (const auto& e : it->second) out.push_back(e.desc);
        }
        bridge = executor_->state->remote_bridge;
    }
    if (bridge && bridge->providers_for) {
        try {
            auto remote = bridge->providers_for(capability_id);
            for (auto& desc : remote) {
                bool exists = std::any_of(out.begin(), out.end(), [&](const CapabilityDescriptor& local) {
                    return local.capability_id == desc.capability_id && local.plugin_id == desc.plugin_id;
                });
                if (!exists) {
                    out.push_back(std::move(desc));
                }
            }
            std::stable_sort(out.begin(), out.end(), [](const CapabilityDescriptor& a, const CapabilityDescriptor& b) {
                return a.priority > b.priority;
            });
        } catch (...) {
        }
    }
    return out;
}

std::vector<CapabilityDescriptor> CapabilityRegistry::listAll() const {
    std::vector<CapabilityDescriptor> out;
    std::shared_ptr<RemoteBridge> bridge;
    {
        std::shared_lock lock(executor_->state->registry_mutex);
        for (const auto& [_, list] : executor_->state->entries) {
            for (const auto& e : list) out.push_back(e.desc);
        }
        bridge = executor_->state->remote_bridge;
    }
    if (bridge && bridge->list_all) {
        try {
            auto remote = bridge->list_all();
            for (auto& desc : remote) {
                bool exists = std::any_of(out.begin(), out.end(), [&](const CapabilityDescriptor& local) {
                    return local.capability_id == desc.capability_id && local.plugin_id == desc.plugin_id;
                });
                if (!exists) {
                    out.push_back(std::move(desc));
                }
            }
        } catch (...) {
        }
    }
    return out;
}

CapabilityResult CapabilityRegistry::invokeInternal(const std::string& plugin_id_filter,
                                                    const std::string& capability_id,
                                                    const nlohmann::json& request,
                                                    const StreamCallback& on_chunk,
                                                    const std::shared_ptr<CancelToken>& cancel,
                                                    bool stream_mode) const {
    return AsyncExecutor::invoke(
        executor_ ? executor_->state : nullptr,
        plugin_id_filter, capability_id, request, on_chunk, cancel, stream_mode);
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
    auto tok = cancel ? cancel : CancelToken::create();
    return invokeInternal("", capability_id, request, {}, tok, false);
}

CapabilityResult CapabilityRegistry::callProviderCancellable(const std::string& plugin_id,
                                                             const std::string& capability_id,
                                                             const nlohmann::json& request,
                                                             const std::shared_ptr<CancelToken>& cancel) const {
    auto tok = cancel ? cancel : CancelToken::create();
    return invokeInternal(plugin_id, capability_id, request, {}, tok, false);
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
    executor_->submit(plugin_id, cancel, [exec_state, plugin_id, capability_id, request,
                                          on_complete = std::move(on_complete), cancel]() {
        if (!exec_state->owner_alive.load(std::memory_order_acquire)) {
            if (cancel) cancel->cancel();
            return;
        }
        auto res = AsyncExecutor::invoke(exec_state, plugin_id, capability_id, request, {}, cancel, false);
        if (on_complete && exec_state->owner_alive.load(std::memory_order_acquire)) {
            on_complete(res);
        }
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
    StreamCallback guarded_chunk;
    if (on_chunk) {
        guarded_chunk = [exec_state, cb = std::move(on_chunk)](const StreamChunk& chunk) {
            if (exec_state->owner_alive.load(std::memory_order_acquire)) {
                cb(chunk);
            }
        };
    }
    executor_->submit(plugin_id, cancel, [exec_state, plugin_id, capability_id, request,
                                          guarded_chunk = std::move(guarded_chunk),
                                          on_complete = std::move(on_complete), cancel]() {
        if (!exec_state->owner_alive.load(std::memory_order_acquire)) {
            if (cancel) cancel->cancel();
            return;
        }
        auto res = AsyncExecutor::invoke(
            exec_state, plugin_id, capability_id, request, guarded_chunk, cancel, true);
        if (on_complete && exec_state->owner_alive.load(std::memory_order_acquire)) {
            on_complete(res);
        }
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
