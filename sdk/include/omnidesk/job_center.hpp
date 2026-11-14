#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#include <omnidesk/async.hpp>
#include <omnidesk/export.h>

namespace omnidesk {

class EntityBus;

enum class JobState {
    Queued = 0,
    Running,
    Completed,
    Failed,
    Cancelled,
};

struct OMNIDESK_API JobTarget {
    std::string domain;       ///< e.g. "books", "media", "text"
    std::string action_topic; ///< EntityBus topic to publish when user clicks Open (e.g. "entity.book.open")
    std::string entity_id;    ///< Path, item id, or entity identifier
    std::string label;        ///< UI CTA text (e.g. "打开双栏译本")
    nlohmann::json payload = nlohmann::json::object();
};

struct OMNIDESK_API JobSpec {
    std::string title;
    std::string plugin_id;
    std::string kind;
    JobTarget open_target;
};

struct OMNIDESK_API JobInfo {
    std::string id;
    std::string title;
    std::string plugin_id;
    std::string kind;
    JobState state = JobState::Queued;
    float progress = 0.0f;  ///< [0.0f, 1.0f]
    std::string stage_message;
    std::string error;
    JobTarget open_target;
    int64_t created_ms = 0;
    int64_t updated_ms = 0;
};

class JobCenter;

/// Execution context handed to each background JobCenter task.
class OMNIDESK_API JobHandle {
public:
    const std::string& jobId() const noexcept { return job_id_; }
    bool isCancelled() const noexcept { return cancel_->isCancelled(); }
    const std::shared_ptr<CancelToken>& cancelToken() const noexcept { return cancel_; }

    void reportProgress(float progress, const std::string& stage_message = "");
    void setOpenTarget(const JobTarget& target);

private:
    friend class JobCenter;
    JobHandle(JobCenter& center, std::string job_id, std::shared_ptr<CancelToken> cancel);

    JobCenter& center_;
    std::string job_id_;
    std::shared_ptr<CancelToken> cancel_;
};

/// Global background long-running task queue (e.g. BabelDOC PDF layout,
/// FunASR offline transcription, NAS batch metadata scraping). Persists across
/// workspace switches and emits EntityBus events for status bars & 1-click open.
class OMNIDESK_API JobCenter {
public:
    using TaskFn = std::function<bool(JobHandle& job, std::string& error)>;
    using Listener = std::function<void()>;

    explicit JobCenter(EntityBus* bus = nullptr, size_t worker_count = 2);
    ~JobCenter();

    JobCenter(const JobCenter&) = delete;
    JobCenter& operator=(const JobCenter&) = delete;

    void setBus(EntityBus* bus);
    void shutdown();

    std::string submit(const JobSpec& spec, TaskFn task);
    bool cancel(const std::string& job_id);
    void cancelByPlugin(const std::string& plugin_id);
    void dismissFinished(const std::string& job_id);

    std::optional<JobInfo> getJob(const std::string& job_id) const;
    std::vector<JobInfo> listJobs(bool include_finished = true) const;

    /// Publishes open_target action on EntityBus so the shell opens the result in one click.
    bool triggerOpen(const std::string& job_id) const;

    uint64_t subscribe(Listener cb);
    void unsubscribe(uint64_t token);

private:
    friend class JobHandle;

    struct QueuedTask {
        std::string job_id;
        TaskFn fn;
    };

    void workerLoop();
    void updateProgress(const std::string& job_id, float progress, const std::string& stage_message);
    void updateOpenTarget(const std::string& job_id, const JobTarget& target);
    void publishJobEvent(const std::string& topic, const JobInfo& info);
    void notifyListeners();

    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::condition_variable done_cv_;
    std::unordered_map<std::string, JobInfo> jobs_;
    std::unordered_map<std::string, std::shared_ptr<CancelToken>> tokens_;
    std::unordered_map<std::string, size_t> active_running_by_plugin_;
    std::vector<std::string> insertion_order_;
    std::queue<QueuedTask> queue_;
    std::vector<std::thread> workers_;
    bool stopping_ = false;
    uint64_t next_id_ = 1;

    EntityBus* bus_ = nullptr;
    mutable std::mutex listeners_mutex_;
    std::unordered_map<uint64_t, Listener> listeners_;
    uint64_t next_listener_token_ = 1;
};

OMNIDESK_API const char* jobStateLabel(JobState state);

}  // namespace omnidesk
