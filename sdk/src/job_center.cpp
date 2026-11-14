#include <omnidesk/job_center.hpp>
#include <omnidesk/entity_bus.hpp>
#include "shutdown_grace.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <queue>
#include <unordered_map>

namespace omnidesk {

namespace {

int64_t nowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

nlohmann::json jobToJson(const JobInfo& info) {
    return {
        {"id", info.id},
        {"title", info.title},
        {"plugin_id", info.plugin_id},
        {"kind", info.kind},
        {"state", jobStateLabel(info.state)},
        {"progress", info.progress},
        {"stage_message", info.stage_message},
        {"error", info.error},
        {"target", {
            {"domain", info.open_target.domain},
            {"action_topic", info.open_target.action_topic},
            {"entity_id", info.open_target.entity_id},
            {"label", info.open_target.label},
            {"payload", info.open_target.payload},
        }},
        {"created_ms", info.created_ms},
        {"updated_ms", info.updated_ms},
    };
}

}  // namespace

struct JobHandle::State {
    struct QueuedTask {
        std::string job_id;
        JobCenter::TaskFn fn;
    };

    mutable std::mutex mutex;
    std::condition_variable cv;
    std::condition_variable done_cv;
    std::unordered_map<std::string, JobInfo> jobs;
    std::unordered_map<std::string, std::shared_ptr<CancelToken>> tokens;
    std::unordered_map<std::string, size_t> active_running_by_plugin;
    size_t active_running_total = 0;
    size_t active_workers = 0;
    std::vector<std::thread::id> worker_ids;
    std::vector<std::string> insertion_order;
    std::queue<QueuedTask> queue;
    bool stopping = false;
    bool owner_alive = true;
    uint64_t next_id = 1;

    EntityBus* bus = nullptr;
    mutable std::mutex listeners_mutex;
    std::unordered_map<uint64_t, JobCenter::Listener> listeners;
    uint64_t next_listener_token = 1;
};

namespace {

void publishJobEventOnState(const JobHandle::State& state,
                            const std::string& topic,
                            const JobInfo& info) {
    EntityBus* bus = nullptr;
    {
        std::lock_guard lock(state.mutex);
        if (!state.owner_alive || state.stopping) return;
        bus = state.bus;
    }
    if (bus) {
        bus->publish(topic, jobToJson(info));
    }
}

void notifyListenersOnState(JobHandle::State& state) {
    {
        std::lock_guard lock(state.mutex);
        if (!state.owner_alive || state.stopping) return;
    }
    std::vector<JobCenter::Listener> snapshot;
    {
        std::lock_guard lock(state.listeners_mutex);
        for (const auto& [_, cb] : state.listeners) {
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

}  // namespace

void JobCenter::workerLoop(std::shared_ptr<State> state,
                           std::shared_ptr<std::atomic<bool>> exited) {
    struct ExitGuard {
        std::shared_ptr<JobHandle::State> st;
        std::shared_ptr<std::atomic<bool>> ex;
        ~ExitGuard() {
            {
                std::lock_guard lock(st->mutex);
                if (st->active_workers > 0) --st->active_workers;
                st->done_cv.notify_all();
            }
            ex->store(true, std::memory_order_release);
        }
    } exit_guard{state, exited};

    while (true) {
        JobHandle::State::QueuedTask task;
        std::shared_ptr<CancelToken> tok;
        std::optional<JobInfo> started_info;
        {
            std::unique_lock lock(state->mutex);
            state->cv.wait(lock, [&state]() { return state->stopping || !state->queue.empty(); });
            if (state->stopping && state->queue.empty()) return;

            task = std::move(state->queue.front());
            state->queue.pop();

            auto jit = state->jobs.find(task.job_id);
            auto tit = state->tokens.find(task.job_id);
            if (jit == state->jobs.end()) continue;
            if (tit != state->tokens.end()) tok = tit->second;
            if (jit->second.state == JobState::Cancelled || (tok && tok->isCancelled())) {
                jit->second.state = JobState::Cancelled;
                jit->second.stage_message = "已取消";
                jit->second.updated_ms = nowMs();
                continue;
            }
            const std::string plugin_id = jit->second.plugin_id;
            jit->second.state = JobState::Running;
            if (jit->second.stage_message == "排队中") {
                jit->second.stage_message = "执行中";
            }
            jit->second.updated_ms = nowMs();
            started_info = jit->second;
            if (!plugin_id.empty()) {
                state->active_running_by_plugin[plugin_id] += 1;
            }
            state->active_running_total += 1;
        }

        if (started_info) {
            publishJobEventOnState(*state, "job.updated", *started_info);
            notifyListenersOnState(*state);
        }

        if (!tok) tok = CancelToken::create();
        JobHandle handle(state, task.job_id, tok);
        std::string err;
        bool ok = false;
        try {
            ok = task.fn(handle, err);
        } catch (const std::exception& ex) {
            ok = false;
            err = ex.what();
        } catch (...) {
            ok = false;
            err = "unknown worker exception";
        }
        task.fn = {};

        std::optional<JobInfo> finished_info;
        std::string topic = "job.completed";
        {
            std::lock_guard lock(state->mutex);
            if (state->active_running_total > 0) --state->active_running_total;
            auto jit = state->jobs.find(task.job_id);
            if (jit != state->jobs.end()) {
                if (!jit->second.plugin_id.empty()) {
                    auto ait = state->active_running_by_plugin.find(jit->second.plugin_id);
                    if (ait != state->active_running_by_plugin.end() && ait->second > 0) {
                        ait->second -= 1;
                        if (ait->second == 0) state->active_running_by_plugin.erase(ait);
                    }
                }
                if (tok->isCancelled()) {
                    jit->second.state = JobState::Cancelled;
                    jit->second.stage_message = "已取消";
                    jit->second.error = err.empty() ? "cancelled" : err;
                    topic = "job.cancelled";
                } else if (ok) {
                    jit->second.state = JobState::Completed;
                    jit->second.progress = 1.0f;
                    if (jit->second.stage_message.empty() || jit->second.stage_message == "执行中") {
                        jit->second.stage_message = "已完成";
                    }
                    topic = "job.completed";
                } else {
                    jit->second.state = JobState::Failed;
                    jit->second.error = err.empty() ? "task failed" : err;
                    jit->second.stage_message = "失败";
                    topic = "job.failed";
                }
                jit->second.updated_ms = nowMs();
                if (state->owner_alive && !state->stopping) {
                    finished_info = jit->second;
                }
            }
            state->done_cv.notify_all();
        }
        if (finished_info) {
            publishJobEventOnState(*state, topic, *finished_info);
            notifyListenersOnState(*state);
        }
    }
}

const char* jobStateLabel(JobState state) {
    switch (state) {
        case JobState::Queued: return "queued";
        case JobState::Running: return "running";
        case JobState::Completed: return "completed";
        case JobState::Failed: return "failed";
        case JobState::Cancelled: return "cancelled";
    }
    return "unknown";
}

JobHandle::JobHandle(std::shared_ptr<State> state, std::string job_id, std::shared_ptr<CancelToken> cancel)
    : state_(std::move(state)), job_id_(std::move(job_id)), cancel_(std::move(cancel)) {}

void JobHandle::reportProgress(float progress, const std::string& stage_message) {
    if (!state_) return;
    std::optional<JobInfo> info;
    {
        std::lock_guard lock(state_->mutex);
        if (!state_->owner_alive || state_->stopping) return;
        auto it = state_->jobs.find(job_id_);
        if (it == state_->jobs.end()) return;
        if (it->second.state != JobState::Running && it->second.state != JobState::Queued) return;
        it->second.progress = std::clamp(progress, 0.0f, 1.0f);
        if (!stage_message.empty()) {
            it->second.stage_message = stage_message;
        }
        it->second.updated_ms = nowMs();
        info = it->second;
    }
    if (info) {
        publishJobEventOnState(*state_, "job.updated", *info);
        notifyListenersOnState(*state_);
    }
}

void JobHandle::setOpenTarget(const JobTarget& target) {
    if (!state_) return;
    std::lock_guard lock(state_->mutex);
    if (!state_->owner_alive || state_->stopping) return;
    auto it = state_->jobs.find(job_id_);
    if (it == state_->jobs.end()) return;
    it->second.open_target = target;
    it->second.updated_ms = nowMs();
}

int JobCenter::defaultShutdownGraceMs() noexcept {
    return detail::resolveShutdownGraceMs(-1);
}

JobCenter::JobCenter(EntityBus* bus, size_t worker_count) : state_(std::make_shared<State>()) {
    state_->bus = bus;
    const size_t count = std::max<size_t>(1, worker_count);
    state_->active_workers = count;
    workers_.reserve(count);
    for (size_t i = 0; i < count; ++i) {
        auto exited = std::make_shared<std::atomic<bool>>(false);
        std::thread t([st = state_, exited]() { JobCenter::workerLoop(st, exited); });
        {
            std::lock_guard lock(state_->mutex);
            state_->worker_ids.push_back(t.get_id());
        }
        workers_.push_back(WorkerEntry{std::move(t), std::move(exited)});
    }
}

JobCenter::~JobCenter() {
    shutdown();
}

void JobCenter::setBus(EntityBus* bus) {
    std::lock_guard lock(state_->mutex);
    state_->bus = bus;
}

void JobCenter::shutdown(int timeout_ms) {
    std::vector<std::shared_ptr<CancelToken>> active_tokens;
    std::vector<State::QueuedTask> dropped;
    {
        std::lock_guard lock(state_->mutex);
        if (state_->stopping) return;
        state_->stopping = true;
        state_->owner_alive = false;
        state_->bus = nullptr;
        for (const auto& [_, tok] : state_->tokens) {
            if (tok) active_tokens.push_back(tok);
        }
        while (!state_->queue.empty()) {
            State::QueuedTask qt = std::move(state_->queue.front());
            state_->queue.pop();
            auto jit = state_->jobs.find(qt.job_id);
            if (jit != state_->jobs.end() && jit->second.state == JobState::Queued) {
                jit->second.state = JobState::Cancelled;
                jit->second.stage_message = "已取消";
                jit->second.updated_ms = nowMs();
            }
            dropped.push_back(std::move(qt));
        }
    }
    {
        std::lock_guard lock(state_->listeners_mutex);
        state_->listeners.clear();
    }
    dropped.clear();
    for (auto& tok : active_tokens) {
        tok->cancel();
    }
    state_->cv.notify_all();

    const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
    const auto self_id = std::this_thread::get_id();
    {
        std::unique_lock lock(state_->mutex);
        if (grace_ms > 0) {
            state_->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this]() {
                return state_->active_workers == 0;
            });
        }
        if (state_->active_workers > 0) {
            std::fprintf(stderr,
                         "[JobCenter] shutdown grace (%d ms) expired with %zu active worker(s); detaching stuck worker(s)\n",
                         grace_ms, state_->active_workers);
        }
    }
    for (auto& w : workers_) {
        if (!w.thread.joinable()) continue;
        if (w.thread.get_id() == self_id || !w.exited->load(std::memory_order_acquire)) {
            w.thread.detach();
        } else {
            w.thread.join();
        }
    }
    workers_.clear();
}

std::string JobCenter::submit(const JobSpec& spec, TaskFn task) {
    if (!task) return "";

    JobInfo info;
    std::shared_ptr<CancelToken> tok = CancelToken::create();
    {
        std::lock_guard lock(state_->mutex);
        if (state_->stopping) return "";
        info.id = "job-" + std::to_string(state_->next_id++);
        info.title = spec.title.empty() ? info.id : spec.title;
        info.plugin_id = spec.plugin_id;
        info.kind = spec.kind;
        info.state = JobState::Queued;
        info.progress = 0.0f;
        info.stage_message = "排队中";
        info.open_target = spec.open_target;
        info.created_ms = nowMs();
        info.updated_ms = info.created_ms;

        state_->jobs[info.id] = info;
        state_->tokens[info.id] = tok;
        state_->insertion_order.push_back(info.id);
        state_->queue.push(State::QueuedTask{info.id, std::move(task)});
    }
    state_->cv.notify_one();
    publishJobEventOnState(*state_, "job.updated", info);
    notifyListenersOnState(*state_);
    return info.id;
}

bool JobCenter::cancel(const std::string& job_id) {
    std::shared_ptr<CancelToken> tok;
    std::optional<JobInfo> updated;
    {
        std::lock_guard lock(state_->mutex);
        auto tit = state_->tokens.find(job_id);
        if (tit != state_->tokens.end()) tok = tit->second;
        auto jit = state_->jobs.find(job_id);
        if (jit == state_->jobs.end()) return false;
        if (jit->second.state == JobState::Completed ||
            jit->second.state == JobState::Failed ||
            jit->second.state == JobState::Cancelled) {
            return false;
        }
        if (jit->second.state == JobState::Queued) {
            jit->second.state = JobState::Cancelled;
            jit->second.stage_message = "已取消";
            jit->second.updated_ms = nowMs();
            updated = jit->second;
        } else {
            jit->second.stage_message = "正在取消...";
            jit->second.updated_ms = nowMs();
            updated = jit->second;
        }
    }
    if (tok) tok->cancel();
    if (updated) {
        publishJobEventOnState(*state_, updated->state == JobState::Cancelled ? "job.cancelled" : "job.updated", *updated);
        notifyListenersOnState(*state_);
    }
    return true;
}

bool JobCenter::cancelByPlugin(const std::string& plugin_id, bool wait, int timeout_ms) {
    if (plugin_id.empty()) return true;
    std::vector<std::string> ids;
    {
        std::lock_guard lock(state_->mutex);
        for (const auto& [id, info] : state_->jobs) {
            if (info.plugin_id == plugin_id &&
                (info.state == JobState::Queued || info.state == JobState::Running)) {
                ids.push_back(id);
            }
        }
    }
    for (const auto& id : ids) {
        cancel(id);
    }
    std::vector<TaskFn> dropped;
    bool idle = true;
    {
        std::unique_lock lock(state_->mutex);
        std::queue<State::QueuedTask> filtered;
        while (!state_->queue.empty()) {
            State::QueuedTask qt = std::move(state_->queue.front());
            state_->queue.pop();
            auto jit = state_->jobs.find(qt.job_id);
            if (jit != state_->jobs.end() && jit->second.plugin_id == plugin_id) {
                dropped.push_back(std::move(qt.fn));
            } else {
                filtered.push(std::move(qt));
            }
        }
        state_->queue = std::move(filtered);
        auto is_idle = [this, &plugin_id]() {
            auto it = state_->active_running_by_plugin.find(plugin_id);
            return it == state_->active_running_by_plugin.end() || it->second == 0;
        };
        if (wait && !state_->stopping) {
            const auto self_id = std::this_thread::get_id();
            bool on_worker = false;
            for (const auto& wid : state_->worker_ids) {
                if (wid == self_id) {
                    on_worker = true;
                    break;
                }
            }
            if (!on_worker) {
                const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
                if (grace_ms > 0) {
                    state_->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), is_idle);
                }
            }
        }
        idle = is_idle();
    }
    dropped.clear();
    return idle;
}

bool JobCenter::hasActiveJobs(const std::string& plugin_id) const {
    std::lock_guard lock(state_->mutex);
    if (plugin_id.empty()) {
        return state_->active_running_total > 0;
    }
    auto it = state_->active_running_by_plugin.find(plugin_id);
    return it != state_->active_running_by_plugin.end() && it->second > 0;
}

void JobCenter::dismissFinished(const std::string& job_id) {
    bool removed = false;
    {
        std::lock_guard lock(state_->mutex);
        auto it = state_->jobs.find(job_id);
        if (it != state_->jobs.end() &&
            it->second.state != JobState::Queued &&
            it->second.state != JobState::Running) {
            state_->jobs.erase(it);
            state_->tokens.erase(job_id);
            state_->insertion_order.erase(
                std::remove(state_->insertion_order.begin(), state_->insertion_order.end(), job_id),
                state_->insertion_order.end());
            removed = true;
        }
    }
    if (removed) notifyListenersOnState(*state_);
}

std::optional<JobInfo> JobCenter::getJob(const std::string& job_id) const {
    std::lock_guard lock(state_->mutex);
    auto it = state_->jobs.find(job_id);
    if (it == state_->jobs.end()) return std::nullopt;
    return it->second;
}

std::vector<JobInfo> JobCenter::listJobs(bool include_finished) const {
    std::lock_guard lock(state_->mutex);
    std::vector<JobInfo> out;
    for (const auto& id : state_->insertion_order) {
        auto it = state_->jobs.find(id);
        if (it == state_->jobs.end()) continue;
        if (!include_finished &&
            it->second.state != JobState::Queued &&
            it->second.state != JobState::Running) {
            continue;
        }
        out.push_back(it->second);
    }
    return out;
}

bool JobCenter::triggerOpen(const std::string& job_id) const {
    std::optional<JobInfo> snapshot;
    EntityBus* bus = nullptr;
    {
        std::lock_guard lock(state_->mutex);
        auto it = state_->jobs.find(job_id);
        if (it == state_->jobs.end()) return false;
        snapshot = it->second;
        bus = state_->bus;
    }
    if (!bus || !snapshot) return false;

    std::string topic = snapshot->open_target.action_topic;
    if (topic.empty() && !snapshot->open_target.domain.empty()) {
        topic = "entity." + snapshot->open_target.domain + ".open";
    }
    if (topic.empty()) topic = "job.open_requested";

    nlohmann::json payload = snapshot->open_target.payload;
    if (!payload.is_object()) payload = nlohmann::json::object();
    if (!snapshot->open_target.entity_id.empty()) {
        payload["id"] = snapshot->open_target.entity_id;
    }
    if (!snapshot->open_target.domain.empty()) {
        payload["domain"] = snapshot->open_target.domain;
    }
    payload["job_id"] = snapshot->id;
    payload["title"] = snapshot->title;
    bus->publish(topic, payload);
    return true;
}

uint64_t JobCenter::subscribe(Listener cb) {
    std::lock_guard lock(state_->listeners_mutex);
    uint64_t tok = state_->next_listener_token++;
    state_->listeners[tok] = std::move(cb);
    return tok;
}

void JobCenter::unsubscribe(uint64_t token) {
    std::lock_guard lock(state_->listeners_mutex);
    state_->listeners.erase(token);
}

}  // namespace omnidesk
