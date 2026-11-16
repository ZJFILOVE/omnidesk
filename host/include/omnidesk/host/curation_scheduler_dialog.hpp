#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace omnidesk::host {

/**
 * Coordinates automatic & manual daily workflow detection, background CLI agent
 * execution via `recommend.feed.v1`, JobCenter progress bridging, and toast prompts.
 */
class CurationSchedulerManager {
public:
    using StatusListener = std::function<void(const nlohmann::json&)>;

    static CurationSchedulerManager& instance();

    void start();
    void stop();

    uint64_t subscribeStatus(StatusListener listener);
    void unsubscribeStatus(uint64_t token);

    nlohmann::json latestStatus() const;
    void refreshStatus(bool trigger_auto_if_needed = false, bool sync_remote = false);

    void runMissingToday(const std::string& trigger = "manual");
    void runSingleWorkflow(const std::string& project);
    void cancelRun();
    void syncGitHubNow();

    void setAutoRunEnabled(bool enabled);
    void setGitSyncEnabled(bool enabled);
    void setProjectEnabled(const std::string& project, bool enabled);
    void setAgentRunner(const std::string& runner, const std::string& custom_cmd = "");

private:
    CurationSchedulerManager() = default;
    ~CurationSchedulerManager();

    void schedulePeriodicTick();
    void applyStatusUpdate(const nlohmann::json& status, bool from_event);
    void ensureTrackedJob(const nlohmann::json& status);
    void notifyListeners(const nlohmann::json& status);

    mutable std::mutex mutex_;
    bool started_ = false;
    bool was_running_ = false;
    bool periodic_scheduled_ = false;
    std::string active_job_id_;
    nlohmann::json latest_status_;

    std::condition_variable job_cv_;
    std::mutex job_mutex_;
    bool job_bridge_active_ = false;

    uint64_t sched_bus_token_ = 0;
    uint64_t rec_bus_token_ = 0;
    uint64_t next_listener_id_ = 1;
    std::vector<std::pair<uint64_t, StatusListener>> listeners_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
};

class CurationSchedulerDialog {
public:
    static void open(std::function<void()> on_open_calendar = {});
};

} // namespace omnidesk::host
