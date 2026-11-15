#pragma once

#include "ai_curator/git_mirror.hpp"
#include <atomic>
#include <functional>
#include <mutex>
#include <nlohmann/json.hpp>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/plugin.hpp>
#include <string>
#include <sys/types.h>
#include <thread>
#include <unordered_map>
#include <vector>

namespace ai_curator {

struct WorkflowDefinition {
    std::string id;
    std::string label;
    std::string schedule_hint;
    std::string domains_summary;
};

struct SchedulerConfig {
    bool auto_run_enabled = true;
    std::string agent_runner = "auto"; // auto | codex | claude | opencode | gemini | custom
    std::string custom_agent_command;
    std::unordered_map<std::string, bool> project_enabled = {
        {"news-youtube-reddit", true},
        {"music", true},
        {"english", true},
        {"journal", true},
    };
};

struct ProjectAttemptRecord {
    std::string business_date;
    bool auto_attempted = false;
    int attempt_count = 0;
    std::string last_status; // published | failed | cancelled | running
    std::string last_error;
    std::string last_started_at;
    std::string last_finished_at;
    std::string last_runner;
    std::string log_path;
};

class DailyWorkflowScheduler {
public:
    using PipelineFinishedCallback =
        std::function<void(const std::string& project, const std::string& business_date, const RepoPaths& paths)>;

    explicit DailyWorkflowScheduler(omnidesk::PluginContext& ctx,
                                    PipelineFinishedCallback on_pipeline_finished = {});
    ~DailyWorkflowScheduler();

    DailyWorkflowScheduler(const DailyWorkflowScheduler&) = delete;
    DailyWorkflowScheduler& operator=(const DailyWorkflowScheduler&) = delete;

    static const std::vector<WorkflowDefinition>& workflowDefinitions();
    static bool isKnownWorkflow(const std::string& project);
    static int isoDateWeekdayMondayZero(const std::string& iso_date);
    static std::vector<std::string> canonicalRepoTargets(const std::string& project,
                                                         const std::string& day,
                                                         const RepoPaths& paths);

    void initialize(const RepoPaths& paths);
    void shutdown();

    nlohmann::json inspectStatus(const RepoPaths& paths);
    omnidesk::CapabilityResult configure(const RepoPaths& paths, const nlohmann::json& req);
    omnidesk::CapabilityResult startWorkflows(const RepoPaths& paths,
                                              const nlohmann::json& req,
                                              bool wait_for_completion = false);
    omnidesk::CapabilityResult cancelWorkflows();

private:
    struct AgentBinary {
        std::string id;
        std::string label;
        std::string binary_path;
    };

    struct CodexFlagSupport {
        bool probed = false;
        bool bypass_approvals_and_sandbox = false;
        bool full_auto = false;
        bool skip_git_repo_check = false;
        bool cd_flag = false;
    };

    void loadConfigAndStateLocked(const std::string& curation_dir);
    void saveConfigLocked(const std::string& curation_dir) const;
    void saveStateLocked(const std::string& curation_dir) const;

    std::vector<AgentBinary> detectAvailableAgentsLocked() const;
    bool resolveSelectedRunnerLocked(const std::vector<AgentBinary>& available,
                                     std::string& out_runner_id,
                                     std::string& out_binary_or_cmd,
                                     std::string& out_error) const;
    CodexFlagSupport probeCodexFlags(const std::string& codex_bin);

    std::string inspectSingleProjectArtifactState(const std::string& project,
                                                  const std::string& today,
                                                  const RepoPaths& paths,
                                                  bool& out_repo_targets_exist,
                                                  bool& out_live_bundle_exists,
                                                  bool& out_receipt_exists,
                                                  bool& out_prepared_exists,
                                                  std::vector<std::string>& out_existing_targets) const;

    std::string buildWorkflowPrompt(const std::string& project,
                                    const std::string& today,
                                    const RepoPaths& paths,
                                    bool has_prepared_bundle) const;
    std::string resolveOmnideskPmBinary() const;
    std::string resolveSkillMarkdownPath() const;

    void executeRunLoop(std::string run_id,
                        std::string today,
                        std::string trigger_source,
                        RepoPaths paths,
                        std::vector<std::string> projects,
                        std::string runner_id,
                        std::string runner_bin_or_cmd,
                        int timeout_sec);

    int runAgentSubprocess(const std::string& project,
                           const std::string& today,
                           const RepoPaths& paths,
                           const std::string& runner_id,
                           const std::string& runner_bin_or_cmd,
                           const std::string& prompt,
                           const std::string& log_path,
                           int timeout_sec,
                           bool& out_cancelled,
                           bool& out_timed_out,
                           std::string& out_error);

    void publishSchedulerChanged(const std::string& event_reason, const RepoPaths& paths);

    omnidesk::PluginContext& ctx_;
    PipelineFinishedCallback on_pipeline_finished_;

    mutable std::mutex mutex_;
    std::string loaded_curation_dir_;
    SchedulerConfig config_;
    std::unordered_map<std::string, ProjectAttemptRecord> today_attempts_;
    std::string attempts_date_;

    CodexFlagSupport codex_flags_;

    bool running_ = false;
    std::atomic<bool> cancel_requested_{false};
    pid_t active_child_pgid_ = -1;
    std::string active_run_id_;
    std::string active_trigger_ = "idle";
    std::string active_runner_id_;
    std::string active_project_;
    std::vector<std::string> queued_projects_;
    int completed_projects_in_run_ = 0;
    int total_projects_in_run_ = 0;
    float active_progress_ = 0.0f;
    std::string active_stage_message_;
    std::string active_live_line_;
    std::string last_run_summary_;

    std::mutex thread_lifecycle_mutex_;
    std::thread worker_thread_;
};

} // namespace ai_curator
