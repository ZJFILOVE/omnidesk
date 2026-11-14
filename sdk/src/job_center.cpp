#include <omnidesk/job_center.hpp>
#include <omnidesk/entity_bus.hpp>

#include <algorithm>
#include <chrono>

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

JobHandle::JobHandle(JobCenter& center, std::string job_id, std::shared_ptr<CancelToken> cancel)
    : center_(center), job_id_(std::move(job_id)), cancel_(std::move(cancel)) {}

void JobHandle::reportProgress(float progress, const std::string& stage_message) {
    center_.updateProgress(job_id_, progress, stage_message);
}

void JobHandle::setOpenTarget(const JobTarget& target) {
    center_.updateOpenTarget(job_id_, target);
}

JobCenter::JobCenter(EntityBus* bus, size_t worker_count) : bus_(bus) {
    const size_t count = std::max<size_t>(1, worker_count);
    workers_.reserve(count);
    for (size_t i = 0; i < count; ++i) {
        workers_.emplace_back([this]() { workerLoop(); });
    }
}

JobCenter::~JobCenter() {
    shutdown();
}

void JobCenter::setBus(EntityBus* bus) {
    std::lock_guard lock(mutex_);
    bus_ = bus;
}

void JobCenter::shutdown() {
    std::vector<std::shared_ptr<CancelToken>> active_tokens;
    {
        std::lock_guard lock(mutex_);
        if (stopping_) return;
        stopping_ = true;
        for (const auto& [_, tok] : tokens_) {
            if (tok) active_tokens.push_back(tok);
        }
    }
    for (auto& tok : active_tokens) {
        tok->cancel();
    }
    cv_.notify_all();
    for (auto& w : workers_) {
        if (w.joinable()) w.join();
    }
    workers_.clear();
}

std::string JobCenter::submit(const JobSpec& spec, TaskFn task) {
    if (!task) return "";

    JobInfo info;
    std::shared_ptr<CancelToken> tok = CancelToken::create();
    {
        std::lock_guard lock(mutex_);
        if (stopping_) return "";
        info.id = "job-" + std::to_string(next_id_++);
        info.title = spec.title.empty() ? info.id : spec.title;
        info.plugin_id = spec.plugin_id;
        info.kind = spec.kind;
        info.state = JobState::Queued;
        info.progress = 0.0f;
        info.stage_message = "排队中";
        info.open_target = spec.open_target;
        info.created_ms = nowMs();
        info.updated_ms = info.created_ms;

        jobs_[info.id] = info;
        tokens_[info.id] = tok;
        insertion_order_.push_back(info.id);
        queue_.push(QueuedTask{info.id, std::move(task)});
    }
    cv_.notify_one();
    publishJobEvent("job.updated", info);
    notifyListeners();
    return info.id;
}

bool JobCenter::cancel(const std::string& job_id) {
    std::shared_ptr<CancelToken> tok;
    std::optional<JobInfo> updated;
    {
        std::lock_guard lock(mutex_);
        auto tit = tokens_.find(job_id);
        if (tit != tokens_.end()) tok = tit->second;
        auto jit = jobs_.find(job_id);
        if (jit == jobs_.end()) return false;
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
        publishJobEvent(updated->state == JobState::Cancelled ? "job.cancelled" : "job.updated", *updated);
        notifyListeners();
    }
    return true;
}

void JobCenter::cancelByPlugin(const std::string& plugin_id) {
    if (plugin_id.empty()) return;
    std::vector<std::string> ids;
    {
        std::lock_guard lock(mutex_);
        for (const auto& [id, info] : jobs_) {
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
    {
        std::unique_lock lock(mutex_);
        std::queue<QueuedTask> filtered;
        while (!queue_.empty()) {
            QueuedTask qt = std::move(queue_.front());
            queue_.pop();
            auto jit = jobs_.find(qt.job_id);
            if (jit != jobs_.end() && jit->second.plugin_id == plugin_id) {
                dropped.push_back(std::move(qt.fn));
            } else {
                filtered.push(std::move(qt));
            }
        }
        queue_ = std::move(filtered);
        done_cv_.wait(lock, [this, &plugin_id]() {
            auto it = active_running_by_plugin_.find(plugin_id);
            return it == active_running_by_plugin_.end() || it->second == 0;
        });
    }
    dropped.clear();
}

void JobCenter::dismissFinished(const std::string& job_id) {
    bool removed = false;
    {
        std::lock_guard lock(mutex_);
        auto it = jobs_.find(job_id);
        if (it != jobs_.end() &&
            it->second.state != JobState::Queued &&
            it->second.state != JobState::Running) {
            jobs_.erase(it);
            tokens_.erase(job_id);
            insertion_order_.erase(
                std::remove(insertion_order_.begin(), insertion_order_.end(), job_id),
                insertion_order_.end());
            removed = true;
        }
    }
    if (removed) notifyListeners();
}

std::optional<JobInfo> JobCenter::getJob(const std::string& job_id) const {
    std::lock_guard lock(mutex_);
    auto it = jobs_.find(job_id);
    if (it == jobs_.end()) return std::nullopt;
    return it->second;
}

std::vector<JobInfo> JobCenter::listJobs(bool include_finished) const {
    std::lock_guard lock(mutex_);
    std::vector<JobInfo> out;
    for (const auto& id : insertion_order_) {
        auto it = jobs_.find(id);
        if (it == jobs_.end()) continue;
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
        std::lock_guard lock(mutex_);
        auto it = jobs_.find(job_id);
        if (it == jobs_.end()) return false;
        snapshot = it->second;
        bus = bus_;
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
    std::lock_guard lock(listeners_mutex_);
    uint64_t tok = next_listener_token_++;
    listeners_[tok] = std::move(cb);
    return tok;
}

void JobCenter::unsubscribe(uint64_t token) {
    std::lock_guard lock(listeners_mutex_);
    listeners_.erase(token);
}

void JobCenter::workerLoop() {
    while (true) {
        QueuedTask task;
        std::shared_ptr<CancelToken> tok;
        std::optional<JobInfo> started_info;
        {
            std::unique_lock lock(mutex_);
            cv_.wait(lock, [this]() { return stopping_ || !queue_.empty(); });
            if (stopping_ && queue_.empty()) return;

            task = std::move(queue_.front());
            queue_.pop();

            auto jit = jobs_.find(task.job_id);
            auto tit = tokens_.find(task.job_id);
            if (jit == jobs_.end()) continue;
            if (tit != tokens_.end()) tok = tit->second;
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
                active_running_by_plugin_[plugin_id] += 1;
            }
        }

        if (started_info) {
            publishJobEvent("job.updated", *started_info);
            notifyListeners();
        }

        if (!tok) tok = CancelToken::create();
        JobHandle handle(*this, task.job_id, tok);
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
            std::lock_guard lock(mutex_);
            auto jit = jobs_.find(task.job_id);
            if (jit != jobs_.end()) {
                if (!jit->second.plugin_id.empty()) {
                    auto ait = active_running_by_plugin_.find(jit->second.plugin_id);
                    if (ait != active_running_by_plugin_.end() && ait->second > 0) {
                        ait->second -= 1;
                        if (ait->second == 0) active_running_by_plugin_.erase(ait);
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
                finished_info = jit->second;
            }
            done_cv_.notify_all();
        }
        if (finished_info) {
            publishJobEvent(topic, *finished_info);
            notifyListeners();
        }
    }
}

void JobCenter::updateProgress(const std::string& job_id, float progress, const std::string& stage_message) {
    std::optional<JobInfo> info;
    {
        std::lock_guard lock(mutex_);
        auto it = jobs_.find(job_id);
        if (it == jobs_.end()) return;
        if (it->second.state != JobState::Running && it->second.state != JobState::Queued) return;
        it->second.progress = std::clamp(progress, 0.0f, 1.0f);
        if (!stage_message.empty()) {
            it->second.stage_message = stage_message;
        }
        it->second.updated_ms = nowMs();
        info = it->second;
    }
    if (info) {
        publishJobEvent("job.updated", *info);
        notifyListeners();
    }
}

void JobCenter::updateOpenTarget(const std::string& job_id, const JobTarget& target) {
    std::lock_guard lock(mutex_);
    auto it = jobs_.find(job_id);
    if (it == jobs_.end()) return;
    it->second.open_target = target;
    it->second.updated_ms = nowMs();
}

void JobCenter::publishJobEvent(const std::string& topic, const JobInfo& info) {
    EntityBus* bus = nullptr;
    {
        std::lock_guard lock(mutex_);
        bus = bus_;
    }
    if (bus) {
        bus->publish(topic, jobToJson(info));
    }
}

void JobCenter::notifyListeners() {
    std::vector<Listener> snapshot;
    {
        std::lock_guard lock(listeners_mutex_);
        for (const auto& [_, cb] : listeners_) {
            if (cb) snapshot.push_back(cb);
        }
    }
    for (const auto& cb : snapshot) {
        cb();
    }
}

}  // namespace omnidesk
