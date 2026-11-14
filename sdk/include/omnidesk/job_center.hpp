#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <thread>
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
    struct State;
    const std::string& jobId() const noexcept { return job_id_; }
    bool isCancelled() const noexcept { return cancel_->isCancelled(); }
    const std::shared_ptr<CancelToken>& cancelToken() const noexcept { return cancel_; }

    void reportProgress(float progress, const std::string& stage_message = "");
    void setOpenTarget(const JobTarget& target);

private:
    friend class JobCenter;
    JobHandle(std::shared_ptr<State> state, std::string job_id, std::shared_ptr<CancelToken> cancel);

    std::shared_ptr<State> state_;
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

    static int defaultShutdownGraceMs() noexcept;

    void setBus(EntityBus* bus);
    void shutdown(int timeout_ms = -1);

    std::string submit(const JobSpec& spec, TaskFn task);
    bool cancel(const std::string& job_id);
    bool cancelByPlugin(const std::string& plugin_id, bool wait = true, int timeout_ms = -1);
    bool hasActiveJobs(const std::string& plugin_id = "") const;
    void dismissFinished(const std::string& job_id);

    std::optional<JobInfo> getJob(const std::string& job_id) const;
    std::vector<JobInfo> listJobs(bool include_finished = true) const;

    /// Publishes open_target action on EntityBus so the shell opens the result in one click.
    bool triggerOpen(const std::string& job_id) const;

    uint64_t subscribe(Listener cb);
    void unsubscribe(uint64_t token);

private:
    friend class JobHandle;

    struct WorkerEntry {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> exited;
    };

    using State = JobHandle::State;
    static void workerLoop(std::shared_ptr<State> state, std::shared_ptr<std::atomic<bool>> exited);
    std::shared_ptr<State> state_;
    std::vector<WorkerEntry> workers_;
};

OMNIDESK_API const char* jobStateLabel(JobState state);

}  // namespace omnidesk
