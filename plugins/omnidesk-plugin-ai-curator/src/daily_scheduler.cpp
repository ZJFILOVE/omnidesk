#include "ai_curator/daily_scheduler.hpp"

#include "ai_curator/utils.hpp"
#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <poll.h>
#include <sstream>
#include <sys/wait.h>
#include <unistd.h>

namespace fs = std::filesystem;

namespace ai_curator {

namespace {

std::string currentShanghaiTimestamp() {
    using namespace std::chrono;
    const auto now = system_clock::now() + hours(8);
    const std::time_t tt = system_clock::to_time_t(now);
    std::tm tm_val{};
    gmtime_r(&tt, &tm_val);
    char buf[32]{};
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S+08:00", &tm_val);
    return std::string(buf);
}

bool nonEmptyRegularFile(const fs::path& p) {
    std::error_code ec;
    return fs::is_regular_file(p, ec) && fs::file_size(p, ec) > 0 && !ec;
}

bool isExecutableFile(const fs::path& p) {
    std::error_code ec;
    if (!fs::is_regular_file(p, ec)) return false;
    return ::access(p.c_str(), X_OK) == 0;
}

std::vector<fs::path> candidateBinSearchDirs() {
    std::vector<fs::path> dirs;
    auto add_unique = [&](const fs::path& p) {
        if (p.empty()) return;
        for (const auto& existing : dirs) {
            if (existing == p) return;
        }
        dirs.push_back(p);
    };

    if (const char* path_env = std::getenv("PATH")) {
        std::stringstream ss(path_env);
        std::string item;
        while (std::getline(ss, item, ':')) {
            if (!item.empty()) add_unique(fs::path(item));
        }
    }
    if (const char* home = std::getenv("HOME")) {
        const fs::path h(home);
        add_unique(h / ".local" / "bin");
        add_unique(h / ".local" / "npm" / "bin");
        add_unique(h / ".npm-global" / "bin");
        add_unique(h / ".local" / "share" / "pnpm");
        add_unique(h / ".volta" / "bin");
        add_unique(h / ".bun" / "bin");
        add_unique(h / ".cargo" / "bin");
        add_unique(h / "bin");
    }
    add_unique("/usr/local/bin");
    add_unique("/usr/bin");
    add_unique("/bin");
    return dirs;
}

std::string buildEnhancedPathEnv() {
    std::string out;
    for (const auto& d : candidateBinSearchDirs()) {
        if (!out.empty()) out.push_back(':');
        out += d.string();
    }
    return out;
}

std::string sanitizeLiveLogLine(const std::string& raw) {
    std::string clean;
    clean.reserve(raw.size());
    bool in_escape = false;
    for (unsigned char c : raw) {
        if (c == 0x1B) {
            in_escape = true;
            continue;
        }
        if (in_escape) {
            if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')) {
                in_escape = false;
            }
            continue;
        }
        if (c == '\r' || c == '\n' || c == '\t') {
            clean.push_back(' ');
        } else if (c >= 0x20) {
            clean.push_back(static_cast<char>(c));
        }
    }
    return utf8Truncate(trim(clean), 96);
}

omnidesk::CapabilityResult makeSchedError(const std::string& code,
                                          const std::string& message,
                                          nlohmann::json extra = nlohmann::json::object()) {
    omnidesk::CapabilityResult res;
    res.ok = false;
    res.error = code + ": " + message;
    extra["error_code"] = code;
    extra["message"] = message;
    res.data = std::move(extra);
    return res;
}

} // namespace

DailyWorkflowScheduler::DailyWorkflowScheduler(omnidesk::PluginContext& ctx,
                                               PipelineFinishedCallback on_pipeline_finished)
    : ctx_(ctx), on_pipeline_finished_(std::move(on_pipeline_finished)) {}

DailyWorkflowScheduler::~DailyWorkflowScheduler() {
    shutdown();
}

const std::vector<WorkflowDefinition>& DailyWorkflowScheduler::workflowDefinitions() {
    static const std::vector<WorkflowDefinition> kDefs = {
        {"news-youtube-reddit", "早报 · YouTube · Reddit", "08:00", "每日新闻早报 + YouTube 视频 + Reddit 热帖"},
        {"english", "英语精读 · 翻译 · 词汇", "08:30", "工作日精读/翻译/词汇 · 周六影视 · 周日周刊"},
        {"music", "每日 5 首音乐", "09:00", "双仓音乐清单 + Apple Music 每日歌单"},
        {"journal", "科学期刊精读", "09:30", "顶刊论文精读与 DOI 归档"},
    };
    return kDefs;
}

bool DailyWorkflowScheduler::isKnownWorkflow(const std::string& project) {
    for (const auto& def : workflowDefinitions()) {
        if (def.id == project) return true;
    }
    return false;
}

int DailyWorkflowScheduler::isoDateWeekdayMondayZero(const std::string& iso_date) {
    if (!isValidIsoDate(iso_date)) return 0;
    int y = std::stoi(iso_date.substr(0, 4));
    int m = std::stoi(iso_date.substr(5, 2));
    int d = std::stoi(iso_date.substr(8, 2));
    y -= (m <= 2) ? 1 : 0;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = (153u * static_cast<unsigned>(m + (m > 2 ? -3 : 9)) + 2u) / 5u +
                         static_cast<unsigned>(d) - 1u;
    const unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    const int z = era * 146097 + static_cast<int>(doe) - 719468; // days since 1970-01-01 (Thu = 3)
    return ((z + 3) % 7 + 7) % 7;
}

std::vector<std::string> DailyWorkflowScheduler::canonicalRepoTargets(const std::string& project,
                                                                      const std::string& day,
                                                                      const RepoPaths& paths) {
    std::vector<std::string> out;
    if (!isValidIsoDate(day)) return out;

    const fs::path vault(paths.obsidian_vault_dir);
    const fs::path music(paths.music_daily_dir);

    if (project == "journal") {
        if (!paths.obsidian_vault_dir.empty()) {
            out.push_back((vault / "00-inbox" / "Journal" / (day + ".md")).string());
        }
    } else if (project == "music") {
        const std::string year = day.substr(0, 4);
        const std::string month = day.substr(5, 2);
        if (!paths.music_daily_dir.empty()) {
            out.push_back((music / "recommendations" / year / month / (day + ".json")).string());
        }
        if (!paths.obsidian_vault_dir.empty()) {
            out.push_back((vault / "Music" / "Daily" / "manifests" / (day + ".json")).string());
        }
    } else if (project == "english") {
        if (!paths.obsidian_vault_dir.empty()) {
            const int wd = isoDateWeekdayMondayZero(day);
            if (wd < 5) {
                out.push_back((vault / "50-courses" / "English" / "Daily" / (day + ".md")).string());
                out.push_back((vault / "50-courses" / "English" / "Translation" / (day + ".md")).string());
                out.push_back((vault / "50-courses" / "English" / "Vocabulary" / (day + ".md")).string());
            } else if (wd == 5) {
                out.push_back((vault / "50-courses" / "English" / "Weekly" / "TV" / (day + ".md")).string());
            } else {
                out.push_back((vault / "50-courses" / "English" / "Weekly" / (day + ".md")).string());
            }
        }
    } else if (project == "news-youtube-reddit") {
        if (!paths.obsidian_vault_dir.empty()) {
            out.push_back((vault / "News" / "Daily" / (day + ".md")).string());
            out.push_back((vault / "Resources" / "YouTube" / "Daily" / (day + ".md")).string());
            out.push_back((vault / "Resources" / "Reddit" / "Daily" / (day + ".md")).string());
        }
    }
    return out;
}

void DailyWorkflowScheduler::initialize(const RepoPaths& paths) {
    std::lock_guard<std::mutex> lock(mutex_);
    loadConfigAndStateLocked(paths.curation_dir);
}

void DailyWorkflowScheduler::shutdown() {
    shutting_down_.store(true);
    cancel_requested_.store(true);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (active_child_pgid_ > 0) {
            ::kill(-active_child_pgid_, SIGTERM);
        }
    }
    std::lock_guard<std::mutex> tlock(thread_lifecycle_mutex_);
    if (worker_thread_.joinable()) {
        worker_thread_.join();
    }
}

void DailyWorkflowScheduler::loadConfigAndStateLocked(const std::string& curation_dir) {
    if (curation_dir.empty()) return;
    const std::string today = currentShanghaiDate();

    if (loaded_curation_dir_ != curation_dir) {
        loaded_curation_dir_ = curation_dir;
        config_ = SchedulerConfig{};
        today_attempts_.clear();
        attempts_date_.clear();

        const fs::path sched_dir = fs::path(curation_dir) / "scheduler";
        std::error_code ec;
        fs::create_directories(sched_dir / "logs", ec);

        if (auto cj = readJsonFile((sched_dir / "config.json").string())) {
            if (cj->is_object()) {
                config_.auto_run_enabled = cj->value("auto_run_enabled", true);
                config_.git_sync_enabled = cj->value("git_sync_enabled", true);
                config_.agent_runner = cj->value("agent_runner", std::string("auto"));
                config_.custom_agent_command = cj->value("custom_agent_command", std::string(""));
                if (cj->contains("project_enabled") && (*cj)["project_enabled"].is_object()) {
                    for (const auto& def : workflowDefinitions()) {
                        if ((*cj)["project_enabled"].contains(def.id) &&
                            (*cj)["project_enabled"][def.id].is_boolean()) {
                            config_.project_enabled[def.id] =
                                (*cj)["project_enabled"][def.id].get<bool>();
                        }
                    }
                }
            }
        }

        if (auto sj = readJsonFile((sched_dir / "state.json").string())) {
            if (sj->is_object()) {
                last_git_sync_at_ = sj->value("last_git_sync_at", "");
                last_git_sync_summary_ = sj->value("last_git_sync_summary", "");
            }
            if (sj->is_object() && sj->value("business_date", "") == today &&
                sj->contains("attempts") && (*sj)["attempts"].is_object()) {
                attempts_date_ = today;
                for (const auto& def : workflowDefinitions()) {
                    if (!(*sj)["attempts"].contains(def.id)) continue;
                    const auto& aj = (*sj)["attempts"][def.id];
                    if (!aj.is_object()) continue;
                    ProjectAttemptRecord rec;
                    rec.business_date = today;
                    rec.auto_attempted = aj.value("auto_attempted", false);
                    rec.attempt_count = aj.value("attempt_count", 0);
                    rec.last_status = aj.value("last_status", "");
                    rec.last_error = aj.value("last_error", "");
                    rec.last_started_at = aj.value("last_started_at", "");
                    rec.last_finished_at = aj.value("last_finished_at", "");
                    rec.last_runner = aj.value("last_runner", "");
                    rec.log_path = aj.value("log_path", "");
                    rec.git_sync_status = aj.value("git_sync_status", "");
                    rec.git_sync_detail = aj.value("git_sync_detail", "");
                    today_attempts_[def.id] = std::move(rec);
                }
            }
        }
    }

    if (attempts_date_ != today) {
        today_attempts_.clear();
        attempts_date_ = today;
    }
}

void DailyWorkflowScheduler::saveConfigLocked(const std::string& curation_dir) const {
    if (curation_dir.empty()) return;
    nlohmann::json pe = nlohmann::json::object();
    for (const auto& def : workflowDefinitions()) {
        auto it = config_.project_enabled.find(def.id);
        pe[def.id] = (it != config_.project_enabled.end()) ? it->second : true;
    }
    nlohmann::json j = {
        {"schema_version", 1},
        {"auto_run_enabled", config_.auto_run_enabled},
        {"git_sync_enabled", config_.git_sync_enabled},
        {"agent_runner", config_.agent_runner},
        {"custom_agent_command", config_.custom_agent_command},
        {"project_enabled", std::move(pe)},
        {"updated_at", currentShanghaiTimestamp()},
    };
    const fs::path path = fs::path(curation_dir) / "scheduler" / "config.json";
    writeJsonFileAtomic(path.string(), j, 2);
}

void DailyWorkflowScheduler::saveStateLocked(const std::string& curation_dir) const {
    if (curation_dir.empty()) return;
    nlohmann::json attempts = nlohmann::json::object();
    for (const auto& [proj, rec] : today_attempts_) {
        attempts[proj] = {
            {"business_date", rec.business_date},
            {"auto_attempted", rec.auto_attempted},
            {"attempt_count", rec.attempt_count},
            {"last_status", rec.last_status},
            {"last_error", rec.last_error},
            {"last_started_at", rec.last_started_at},
            {"last_finished_at", rec.last_finished_at},
            {"last_runner", rec.last_runner},
            {"log_path", rec.log_path},
            {"git_sync_status", rec.git_sync_status},
            {"git_sync_detail", rec.git_sync_detail},
        };
    }
    nlohmann::json j = {
        {"schema_version", 1},
        {"business_date", attempts_date_.empty() ? currentShanghaiDate() : attempts_date_},
        {"last_run_summary", last_run_summary_},
        {"last_git_sync_at", last_git_sync_at_},
        {"last_git_sync_summary", last_git_sync_summary_},
        {"attempts", std::move(attempts)},
        {"updated_at", currentShanghaiTimestamp()},
    };
    const fs::path path = fs::path(curation_dir) / "scheduler" / "state.json";
    writeJsonFileAtomic(path.string(), j, 2);
}

std::vector<DailyWorkflowScheduler::AgentBinary>
DailyWorkflowScheduler::detectAvailableAgentsLocked() const {
    struct KnownAgentSpec {
        const char* id;
        const char* bin_name;
        const char* alt_bin;
        const char* label;
    };
    static const KnownAgentSpec kKnownAgents[] = {
        {"agy-cli", "agy", "agy-cli", "Antigravity CLI (直连免 Magpie)"},
        {"codex", "codex", nullptr, "Codex CLI"},
        {"claude", "claude", nullptr, "Claude Code"},
        {"opencode", "opencode", nullptr, "OpenCode"},
        {"gemini", "gemini", nullptr, "Gemini CLI"},
    };
    const auto search_dirs = candidateBinSearchDirs();
    std::vector<AgentBinary> found;
    for (const auto& spec : kKnownAgents) {
        bool matched = false;
        for (const auto& dir : search_dirs) {
            const fs::path candidate = dir / spec.bin_name;
            if (isExecutableFile(candidate)) {
                found.push_back({spec.id, spec.label, candidate.string()});
                matched = true;
                break;
            }
            if (spec.alt_bin != nullptr) {
                const fs::path alt_cand = dir / spec.alt_bin;
                if (isExecutableFile(alt_cand)) {
                    found.push_back({spec.id, spec.label, alt_cand.string()});
                    matched = true;
                    break;
                }
            }
        }
        (void)matched;
    }
    return found;
}

bool DailyWorkflowScheduler::resolveSelectedRunnerLocked(
    const std::vector<AgentBinary>& available,
    std::string& out_runner_id,
    std::string& out_binary_or_cmd,
    std::string& out_error) const {
    std::string pref = trim(config_.agent_runner.empty() ? "auto" : config_.agent_runner);
    if (pref == "agy") {
        pref = "agy-cli";
    }
    if (pref == "custom") {
        if (trim(config_.custom_agent_command).empty()) {
            out_error = "已选择自定义 Agent 命令，但尚未配置 custom_agent_command。";
            return false;
        }
        out_runner_id = "custom";
        out_binary_or_cmd = config_.custom_agent_command;
        return true;
    }

    if (pref == "auto") {
        if (!available.empty()) {
            out_runner_id = available.front().id;
            out_binary_or_cmd = available.front().binary_path;
            return true;
        }
        if (!trim(config_.custom_agent_command).empty()) {
            out_runner_id = "custom";
            out_binary_or_cmd = config_.custom_agent_command;
            return true;
        }
        out_error = "未在系统 PATH / ~/.local/bin 中检测到可用的 CLI Agent（agy-cli / codex / claude / opencode / gemini）。";
        return false;
    }

    for (const auto& ag : available) {
        if (ag.id == pref) {
            out_runner_id = ag.id;
            out_binary_or_cmd = ag.binary_path;
            return true;
        }
    }
    out_error = "指定的 CLI Agent '" + pref + "' 未安装或不在可执行路径中。";
    return false;
}

DailyWorkflowScheduler::CodexFlagSupport
DailyWorkflowScheduler::probeCodexFlags(const std::string& codex_bin) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (codex_flags_.probed) return codex_flags_;
    }

    CodexFlagSupport detected;
    detected.probed = true;
    const ProcessResult help_res = runProcess({codex_bin, "exec", "--help"}, "", 4000);
    const std::string text = help_res.stdout_text + "\n" + help_res.stderr_text;
    detected.bypass_approvals_and_sandbox =
        text.find("--dangerously-bypass-approvals-and-sandbox") != std::string::npos;
    detected.full_auto = text.find("--full-auto") != std::string::npos;
    detected.skip_git_repo_check = text.find("--skip-git-repo-check") != std::string::npos;
    detected.cd_flag = (text.find("-C,") != std::string::npos ||
                        text.find(" -C ") != std::string::npos ||
                        text.find("--cd") != std::string::npos);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        codex_flags_ = detected;
    }
    return detected;
}

std::string DailyWorkflowScheduler::inspectSingleProjectArtifactState(
    const std::string& project,
    const std::string& today,
    const RepoPaths& paths,
    bool& out_repo_targets_exist,
    bool& out_live_bundle_exists,
    bool& out_receipt_exists,
    bool& out_prepared_exists,
    std::vector<std::string>& out_existing_targets) const {
    out_repo_targets_exist = false;
    out_live_bundle_exists = false;
    out_receipt_exists = false;
    out_prepared_exists = false;
    out_existing_targets.clear();

    std::error_code ec;
    if (!paths.obsidian_vault_dir.empty()) {
        const fs::path receipt =
            fs::path(paths.obsidian_vault_dir) / ".recommendation" / "runs" / (today + "-" + project + ".json");
        if (nonEmptyRegularFile(receipt)) {
            if (auto rj = readJsonFile(receipt.string())) {
                if (rj->is_object()) {
                    const std::string st = toLowerAscii(rj->value("status", "published"));
                    if (st != "failed" && st != "error" && st != "aborted") {
                        out_receipt_exists = true;
                    }
                }
            }
        }

        const fs::path prepared =
            fs::path(paths.obsidian_vault_dir) / ".recommendation" / "transactions" / project / (today + ".json");
        if (nonEmptyRegularFile(prepared)) {
            out_prepared_exists = true;
        }
    }

    if (!paths.curation_dir.empty()) {
        const fs::path daily_today = fs::path(paths.curation_dir) / "daily" / today;
        if (fs::is_directory(daily_today, ec)) {
            const std::string expected_op = project + ":" + today;
            const std::string sanitized_op = sanitizeFilenameComponent(expected_op) + ".json";
            for (const auto& entry : fs::directory_iterator(daily_today, ec)) {
                if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
                if (entry.path().filename().string() == sanitized_op) {
                    out_live_bundle_exists = true;
                    break;
                }
                if (auto bj = readJsonFile(entry.path().string())) {
                    if (bj->is_object()) {
                        if (bj->value("operation_id", "") == expected_op ||
                            bj->value("pipeline", "") == project) {
                            out_live_bundle_exists = true;
                            break;
                        }
                    }
                }
            }
        }
    }

    const auto targets = canonicalRepoTargets(project, today, paths);
    if (!targets.empty()) {
        bool all_exist = true;
        for (const auto& t : targets) {
            if (nonEmptyRegularFile(fs::path(t))) {
                out_existing_targets.push_back(t);
            } else {
                all_exist = false;
            }
        }
        if (all_exist) {
            out_repo_targets_exist = true;
        } else if (project == "music" && !paths.music_daily_dir.empty() && !paths.obsidian_vault_dir.empty()) {
            const std::string year = today.substr(0, 4);
            const std::string month = today.substr(5, 2);
            const fs::path rec_json =
                fs::path(paths.music_daily_dir) / "recommendations" / year / month / (today + ".json");
            const fs::path md_note =
                fs::path(paths.obsidian_vault_dir) / "Music" / "Daily" / (today + ".md");
            if (nonEmptyRegularFile(rec_json) && nonEmptyRegularFile(md_note)) {
                out_repo_targets_exist = true;
                out_existing_targets = {rec_json.string(), md_note.string()};
            }
        }
    }

    if (out_receipt_exists || out_live_bundle_exists || out_repo_targets_exist) {
        return "published";
    }
    if (out_prepared_exists) {
        return "prepared_recovery";
    }
    return "pending";
}

nlohmann::json DailyWorkflowScheduler::syncGitHubRepos(const RepoPaths& paths,
                                                       bool push_ahead,
                                                       bool force) {
    std::lock_guard<std::mutex> sync_lock(git_sync_mutex_);

    const auto now_tp = std::chrono::steady_clock::now();
    {
        std::lock_guard<std::mutex> lock(mutex_);
        loadConfigAndStateLocked(paths.curation_dir);
        if (!force && !push_ahead && last_remote_pull_tp_.time_since_epoch().count() > 0) {
            const auto sec = std::chrono::duration_cast<std::chrono::seconds>(
                                 now_tp - last_remote_pull_tp_)
                                 .count();
            if (sec < 25) {
                return {
                    {"ok", true},
                    {"skipped_recent", true},
                    {"pulled_any", false},
                    {"pushed_any", false},
                    {"last_git_sync_at", last_git_sync_at_},
                    {"summary", last_git_sync_summary_},
                };
            }
        }
    }

    BidirectionalSyncStatus obs_bi;
    BidirectionalSyncStatus mus_bi;
    if (push_ahead) {
        obs_bi = GitMirror::syncBidirectional(paths.obsidian_vault_dir, kObsidianVaultRemoteUrl, false);
        mus_bi = GitMirror::syncBidirectional(paths.music_daily_dir, kMusicDailyRemoteUrl, false);
    } else {
        obs_bi.pull = GitMirror::syncRepo(paths.obsidian_vault_dir, kObsidianVaultRemoteUrl, false, true);
        mus_bi.pull = GitMirror::syncRepo(paths.music_daily_dir, kMusicDailyRemoteUrl, false, true);
    }

    const bool pulled_any = obs_bi.pull.pulled || mus_bi.pull.pulled;
    const bool pushed_any = obs_bi.push.pushed || mus_bi.push.pushed;
    const bool has_any_remote = obs_bi.pull.has_remote || mus_bi.pull.has_remote;
    const bool ok = obs_bi.pull.error.empty() && mus_bi.pull.error.empty() &&
                    (!push_ahead || (obs_bi.push.error.empty() && mus_bi.push.error.empty()));

    std::string summary;
    if (!has_any_remote) {
        summary = "本地仓库未配置 GitHub origin 远端";
    } else if (!ok) {
        std::string err = !obs_bi.pull.error.empty() ? obs_bi.pull.error
                          : (!mus_bi.pull.error.empty() ? mus_bi.pull.error
                          : (!obs_bi.push.error.empty() ? obs_bi.push.error : mus_bi.push.error));
        summary = "GitHub 同步异常: " + utf8Truncate(err, 96);
    } else if (pulled_any && pushed_any) {
        summary = "已双向同步 GitHub（拉取远端更新并推送本地提交）";
    } else if (pulled_any) {
        summary = "已从 GitHub 拉取最新每日推荐并刷新本地归档";
    } else if (pushed_any) {
        summary = "已将本地每日推荐提交推送至 GitHub";
    } else if (obs_bi.pull.dirty_worktree_preserved || mus_bi.pull.dirty_worktree_preserved) {
        summary = "检测到本地未提交手写修改，已安全保留现场";
    } else {
        summary = "已与 GitHub origin/main 保持最新同步";
    }

    const std::string ts = currentShanghaiTimestamp();
    {
        std::lock_guard<std::mutex> lock(mutex_);
        last_remote_pull_tp_ = std::chrono::steady_clock::now();
        if (has_any_remote) {
            last_git_sync_at_ = ts;
            last_git_sync_summary_ = summary;
            saveStateLocked(paths.curation_dir);
        }
    }

    if (pulled_any && on_pipeline_finished_) {
        try {
            on_pipeline_finished_("git_sync", currentShanghaiDate(), paths);
        } catch (...) {
        }
    }

    return {
        {"ok", ok},
        {"has_remote", has_any_remote},
        {"pulled_any", pulled_any},
        {"pushed_any", pushed_any},
        {"synced_at", ts},
        {"summary", summary},
        {"obsidian_vault", push_ahead ? obs_bi.toJson() : obs_bi.pull.toJson()},
        {"music_daily", push_ahead ? mus_bi.toJson() : mus_bi.pull.toJson()},
    };
}

nlohmann::json DailyWorkflowScheduler::inspectStatus(const RepoPaths& paths) {
    std::lock_guard<std::mutex> lock(mutex_);
    loadConfigAndStateLocked(paths.curation_dir);

    const std::string today = currentShanghaiDate();
    const int weekday = isoDateWeekdayMondayZero(today);
    static const char* kWeekdayNames[] = {"周一", "周二", "周三", "周四", "周五", "周六", "周日"};

    const auto available = detectAvailableAgentsLocked();
    std::string resolved_runner_id;
    std::string resolved_runner_bin;
    std::string runner_error;
    const bool runner_ready =
        resolveSelectedRunnerLocked(available, resolved_runner_id, resolved_runner_bin, runner_error);

    nlohmann::json agents_arr = nlohmann::json::array();
    for (const auto& ag : available) {
        agents_arr.push_back({
            {"id", ag.id},
            {"label", ag.label},
            {"binary_path", ag.binary_path},
        });
    }

    int published_count = 0;
    int enabled_count = 0;
    int missing_enabled_count = 0;
    int failed_count = 0;
    bool needs_auto_run = false;
    bool any_repo_targets_exist = false;

    nlohmann::json workflows_arr = nlohmann::json::array();
    for (const auto& def : workflowDefinitions()) {
        const auto en_it = config_.project_enabled.find(def.id);
        const bool enabled = (en_it != config_.project_enabled.end()) ? en_it->second : true;
        if (enabled) ++enabled_count;

        bool repo_targets_exist = false;
        bool live_bundle_exists = false;
        bool receipt_exists = false;
        bool prepared_exists = false;
        std::vector<std::string> existing_targets;
        std::string art_state = inspectSingleProjectArtifactState(
            def.id, today, paths, repo_targets_exist, live_bundle_exists,
            receipt_exists, prepared_exists, existing_targets);
        if (repo_targets_exist) any_repo_targets_exist = true;

        const auto att_it = today_attempts_.find(def.id);
        const bool has_attempt = (att_it != today_attempts_.end());

        std::string status = art_state;
        if (running_ && active_project_ == def.id) {
            status = "running";
        } else if (running_ &&
                   std::find(queued_projects_.begin(), queued_projects_.end(), def.id) !=
                       queued_projects_.end()) {
            status = "queued";
        } else if (art_state == "published") {
            status = "published";
            ++published_count;
        } else if (!enabled) {
            status = "disabled";
        } else if (art_state == "prepared_recovery") {
            status = "prepared_recovery";
            ++missing_enabled_count;
            if (!has_attempt || !att_it->second.auto_attempted) {
                needs_auto_run = true;
            }
        } else if (has_attempt && att_it->second.last_status == "failed") {
            status = "failed";
            ++failed_count;
            ++missing_enabled_count;
        } else if (has_attempt && att_it->second.last_status == "cancelled") {
            status = "cancelled";
            ++missing_enabled_count;
        } else {
            status = "pending";
            ++missing_enabled_count;
            if (!has_attempt || !att_it->second.auto_attempted) {
                needs_auto_run = true;
            }
        }

        nlohmann::json wf = {
            {"id", def.id},
            {"label", def.label},
            {"schedule_hint", def.schedule_hint},
            {"domains_summary", def.domains_summary},
            {"enabled", enabled},
            {"status", status},
            {"published_today", art_state == "published"},
            {"repo_targets_exist", repo_targets_exist},
            {"live_bundle_exists", live_bundle_exists},
            {"receipt_exists", receipt_exists},
            {"prepared_bundle_exists", prepared_exists},
            {"existing_targets", existing_targets},
            {"canonical_targets", canonicalRepoTargets(def.id, today, paths)},
            {"auto_attempted_today", has_attempt ? att_it->second.auto_attempted : false},
            {"attempt_count", has_attempt ? att_it->second.attempt_count : 0},
            {"last_error", has_attempt ? att_it->second.last_error : ""},
            {"last_started_at", has_attempt ? att_it->second.last_started_at : ""},
            {"last_finished_at", has_attempt ? att_it->second.last_finished_at : ""},
            {"last_runner", has_attempt ? att_it->second.last_runner : ""},
            {"git_sync_status", has_attempt ? att_it->second.git_sync_status : ""},
            {"git_sync_detail", has_attempt ? att_it->second.git_sync_detail : ""},
            {"log_path", has_attempt ? att_it->second.log_path
                                     : (paths.curation_dir.empty()
                                            ? std::string("")
                                            : (fs::path(paths.curation_dir) / "scheduler" / "logs" /
                                               (today + "-" + def.id + ".log"))
                                                  .string())},
        };
        workflows_arr.push_back(std::move(wf));
    }

    const bool effective_needs_auto_run =
        config_.auto_run_enabled && !running_ && runner_ready && needs_auto_run;

    std::string summary_text;
    if (running_) {
        summary_text = active_stage_message_.empty()
                           ? ("后台运行中：" + active_project_)
                           : active_stage_message_;
    } else if (enabled_count == 0) {
        summary_text = "全部定时工作流已手动关闭";
    } else if (missing_enabled_count == 0) {
        summary_text = "今日已全部完成 (" + std::to_string(published_count) + "/" +
                       std::to_string(static_cast<int>(workflowDefinitions().size())) + ")";
    } else if (failed_count > 0) {
        summary_text = "今日已完成 " + std::to_string(published_count) + " 项 · " +
                       std::to_string(failed_count) + " 项待重试";
    } else if (!config_.auto_run_enabled) {
        summary_text = "自动补跑已关闭 · 今日待运行 " + std::to_string(missing_enabled_count) + " 项";
    } else if (!runner_ready) {
        summary_text = "待运行 " + std::to_string(missing_enabled_count) + " 项（未检测到 CLI Agent）";
    } else {
        summary_text = "今日待补跑 " + std::to_string(missing_enabled_count) + " 项（已完成 " +
                       std::to_string(published_count) + " 项）";
    }
    if (!running_ && !last_git_sync_summary_.empty()) {
        summary_text += " · " + last_git_sync_summary_;
    }

    nlohmann::json pe = nlohmann::json::object();
    for (const auto& def : workflowDefinitions()) {
        auto it = config_.project_enabled.find(def.id);
        pe[def.id] = (it != config_.project_enabled.end()) ? it->second : true;
    }

    return {
        {"business_date", today},
        {"timezone", "Asia/Shanghai"},
        {"weekday_index", weekday},
        {"weekday_label", kWeekdayNames[std::clamp(weekday, 0, 6)]},
        {"auto_run_enabled", config_.auto_run_enabled},
        {"git_sync_enabled", config_.git_sync_enabled},
        {"agent_runner", config_.agent_runner},
        {"custom_agent_command", config_.custom_agent_command},
        {"project_enabled", std::move(pe)},
        {"runner_ready", runner_ready},
        {"resolved_runner", resolved_runner_id},
        {"resolved_runner_binary", resolved_runner_bin},
        {"runner_error", runner_error},
        {"available_agents", std::move(agents_arr)},
        {"running", running_},
        {"run_id", active_run_id_},
        {"active_trigger", active_trigger_},
        {"active_runner", active_runner_id_},
        {"active_project", active_project_},
        {"queued_projects", queued_projects_},
        {"completed_in_run", completed_projects_in_run_},
        {"total_in_run", total_projects_in_run_},
        {"progress", active_progress_},
        {"stage_message", active_stage_message_},
        {"live_output_line", active_live_line_},
        {"last_run_summary", last_run_summary_},
        {"last_git_sync_at", last_git_sync_at_},
        {"last_git_sync_summary", last_git_sync_summary_},
        {"published_count", published_count},
        {"enabled_count", enabled_count},
        {"missing_enabled_count", missing_enabled_count},
        {"failed_count", failed_count},
        {"total_workflows", static_cast<int>(workflowDefinitions().size())},
        {"needs_auto_run", effective_needs_auto_run},
        {"any_repo_targets_exist", any_repo_targets_exist},
        {"summary_text", summary_text},
        {"workflows", std::move(workflows_arr)},
    };
}

omnidesk::CapabilityResult DailyWorkflowScheduler::configure(const RepoPaths& paths,
                                                             const nlohmann::json& req) {
    if (req.contains("custom_agent_command") &&
        req.value("_omnidesk_local_ui_token", std::string("")) != omnidesk::localUiCallToken()) {
        return makeSchedError("untrusted_custom_agent",
                              "custom_agent_command can only be changed by the local OmniDesk UI");
    }
    {
        std::lock_guard<std::mutex> lock(mutex_);
        loadConfigAndStateLocked(paths.curation_dir);

        if (req.contains("auto_run_enabled") && req["auto_run_enabled"].is_boolean()) {
            config_.auto_run_enabled = req["auto_run_enabled"].get<bool>();
        }
        if (req.contains("git_sync_enabled") && req["git_sync_enabled"].is_boolean()) {
            config_.git_sync_enabled = req["git_sync_enabled"].get<bool>();
        }
        if (req.contains("agent_runner") && req["agent_runner"].is_string()) {
            std::string runner = toLowerAscii(trim(req["agent_runner"].get<std::string>()));
            if (runner == "agy") {
                runner = "agy-cli";
            }
            if (runner != "auto" && runner != "agy-cli" && runner != "codex" &&
                runner != "claude" && runner != "opencode" && runner != "gemini" &&
                runner != "custom") {
                return makeSchedError("invalid_agent_runner",
                                      "Unsupported agent_runner: " + runner);
            }
            config_.agent_runner = runner;
        }
        if (req.contains("custom_agent_command") && req["custom_agent_command"].is_string()) {
            config_.custom_agent_command = trim(req["custom_agent_command"].get<std::string>());
        }
        if (req.contains("project_enabled") && req["project_enabled"].is_object()) {
            for (auto it = req["project_enabled"].begin(); it != req["project_enabled"].end(); ++it) {
                if (!isKnownWorkflow(it.key())) {
                    return makeSchedError("unknown_workflow",
                                          "Unknown daily workflow: " + it.key());
                }
                if (it.value().is_boolean()) {
                    config_.project_enabled[it.key()] = it.value().get<bool>();
                }
            }
        }
        if (req.contains("project") && req["project"].is_string() &&
            req.contains("enabled") && req["enabled"].is_boolean()) {
            const std::string proj = req["project"].get<std::string>();
            if (!isKnownWorkflow(proj)) {
                return makeSchedError("unknown_workflow", "Unknown daily workflow: " + proj);
            }
            config_.project_enabled[proj] = req["enabled"].get<bool>();
        }
        if (req.value("reset_today_attempts", false)) {
            today_attempts_.clear();
            saveStateLocked(paths.curation_dir);
        }

        saveConfigLocked(paths.curation_dir);
    }

    publishSchedulerChanged("configured", paths);
    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = inspectStatus(paths);
    return res;
}

std::string DailyWorkflowScheduler::resolveOmnideskPmBinary() const {
    if (const char* env_pm = std::getenv("OMNIDESK_PM_BIN")) {
        if (*env_pm && isExecutableFile(fs::path(env_pm))) return std::string(env_pm);
    }
    char exe_buf[4096]{};
    const ssize_t n = ::readlink("/proc/self/exe", exe_buf, sizeof(exe_buf) - 1);
    if (n > 0) {
        exe_buf[n] = '\0';
        const fs::path exe_dir = fs::path(exe_buf).parent_path();
        if (isExecutableFile(exe_dir / "omnidesk-pm")) {
            return (exe_dir / "omnidesk-pm").string();
        }
    }
    const fs::path ws_pm("/home/zhu/Projects/omnidesk/build/omnidesk-pm");
    if (isExecutableFile(ws_pm)) return ws_pm.string();

    for (const auto& dir : candidateBinSearchDirs()) {
        if (isExecutableFile(dir / "omnidesk-pm")) {
            return (dir / "omnidesk-pm").string();
        }
    }
    return "omnidesk-pm";
}

std::string DailyWorkflowScheduler::resolveSkillMarkdownPath() const {
    if (const char* env_sk = std::getenv("OMNIDESK_CURATION_SKILL_PATH")) {
        if (*env_sk && nonEmptyRegularFile(fs::path(env_sk))) return std::string(env_sk);
    }
    const fs::path ws_skill(
        "/home/zhu/Projects/omnidesk/.agents/skills/omnidesk-curation-writeback/SKILL.md");
    if (nonEmptyRegularFile(ws_skill)) return ws_skill.string();

    char exe_buf[4096]{};
    const ssize_t n = ::readlink("/proc/self/exe", exe_buf, sizeof(exe_buf) - 1);
    if (n > 0) {
        exe_buf[n] = '\0';
        fs::path cur = fs::path(exe_buf).parent_path();
        for (int i = 0; i < 4 && !cur.empty(); ++i) {
            const fs::path cand =
                cur / ".agents" / "skills" / "omnidesk-curation-writeback" / "SKILL.md";
            if (nonEmptyRegularFile(cand)) return cand.string();
            cur = cur.parent_path();
        }
    }
    return ws_skill.string();
}

std::string DailyWorkflowScheduler::buildWorkflowPrompt(const std::string& project,
                                                        const std::string& today,
                                                        const RepoPaths& paths,
                                                        bool has_prepared_bundle) const {
    const std::string skill_path = resolveSkillMarkdownPath();
    const std::string pm_bin = resolveOmnideskPmBinary();
    const auto targets = canonicalRepoTargets(project, today, paths);

    std::ostringstream oss;
    oss << "You are invoked inside OmniDesk's background Daily Curation Scheduler in non-interactive mode.\n"
        << "Execute ONLY today's scheduled daily_immutable workflow for project `" << project
        << "` on Asia/Shanghai business_date `" << today << "`.\n\n"
        << "Mandatory Context & Paths:\n"
        << "- Skill specification (MUST read and follow strictly): " << skill_path << "\n"
        << "- Obsidian Vault repo: " << paths.obsidian_vault_dir << "\n"
        << "- Music Daily repo: " << paths.music_daily_dir << "\n"
        << "- OmniDesk PM CLI: " << pm_bin << "\n"
        << "- Canonical target files for `" << project << "` on `" << today << "`:\n";
    for (const auto& t : targets) {
        oss << "  * " << t << "\n";
    }
    if (has_prepared_bundle) {
        oss << "\nCRITICAL RECOVERY NOTICE: An unfinished PREPARED bundle exists at `"
            << (fs::path(paths.obsidian_vault_dir) / ".recommendation" / "transactions" / project /
                (today + ".json"))
                   .string()
            << "`. Per `.recommendation/PUBLISHING.md` and `policy.json`, you MUST resume and recover "
               "this existing bundle (`resume_existing_bundle`) and NEVER re-select or overwrite it!\n";
    }
    oss << "\nHard Guardrails (Non-Negotiable):\n"
        << "1. Read `" << skill_path << "`, `" << paths.obsidian_vault_dir
        << "/.recommendation/PUBLISHING.md`, and `" << paths.obsidian_vault_dir
        << "/.recommendation/policy.json` before acting.\n"
        << "2. Strictly target ONLY business_date `" << today
        << "` (Asia/Shanghai). NEVER backfill or modify any past historical date.\n"
        << "3. Enforce strict permanent non-repeat dedup against `.recommendation/history/*.jsonl`, "
           "`music-daily/recommendations/**/*.json`, and historical notes. Fail closed if any ledger is unreadable.\n"
        << "4. Preserve any user handwritten content outside `<!-- AUTO:START -->` ... `<!-- AUTO:END -->` (`## Notes`).\n"
        << "5. For `english`, keep any `今日饮食` suggestion strictly ephemeral in stdout/summary only (`ephemeral_chat_note`); NEVER write it to Vault files.\n"
        << "6. For `music`, ONLY sync Apple Music playlist `p.YJXV7dEIeb3GzZX` (`ChatGPT Daily 5`) AFTER verified dual-repo publication succeeds.\n"
        << "7. Commit all verified stage payloads, history ledgers, and run receipts to the local `main` branch per `.recommendation/PUBLISHING.md` (for `music`, in BOTH `music-daily` and `obsidian-vault`). When `git remote get-url origin` is configured, push `main` to GitHub (`git push origin main`) and verify `HEAD == origin/main` with no uncommitted target files.\n"
        << "8. Publish the curated bundle back into OmniDesk live frontend via:\n"
        << "   `" << pm_bin << " rpc recommend.feed.v1 '{\"action\":\"publish_daily_bundle\",\"mode\":\"daily_immutable\",\"pipeline\":\""
        << project << "\",\"business_date\":\"" << today << "\",\"operation_id\":\"" << project << ":"
        << today << "\", ...}'`\n"
        << "9. Do not ask interactive follow-up questions; complete the workflow end-to-end and exit 0 only when `"
        << project << "` for `" << today << "` is genuinely published and synced.\n";
    return oss.str();
}

omnidesk::CapabilityResult DailyWorkflowScheduler::startWorkflows(const RepoPaths& paths,
                                                                  const nlohmann::json& req,
                                                                  bool wait_for_completion) {
    const std::string today = currentShanghaiDate();
    if (req.contains("business_date") || req.contains("date")) {
        const std::string requested_date =
            trim(req.value("business_date", req.value("date", "")));
        if (!requested_date.empty()) {
            if (!isValidIsoDate(requested_date)) {
                return makeSchedError("invalid_date",
                                      "Invalid business_date: " + requested_date);
            }
            if (requested_date < today) {
                return makeSchedError(
                    "historical_backfill_forbidden",
                    "Scheduled daily workflows strictly target today (" + today +
                        ", Asia/Shanghai) and never backfill missed past dates (" +
                        requested_date + ").");
            }
            if (requested_date > today) {
                return makeSchedError("future_date_forbidden",
                                      "Cannot run daily workflow for future date " + requested_date);
            }
        }
    }

    const std::string trigger = toLowerAscii(trim(req.value("trigger", "manual")));
    const bool is_auto = (trigger == "auto");
    const int timeout_sec = std::clamp(req.value("timeout_sec", 1800), 5, 7200);
    const bool wait_flag = wait_for_completion || req.value("wait", false);

    std::vector<std::string> explicit_projects;
    if (req.contains("project") && req["project"].is_string()) {
        const std::string p = trim(req["project"].get<std::string>());
        if (!p.empty()) explicit_projects.push_back(p);
    }
    if (req.contains("projects") && req["projects"].is_array()) {
        for (const auto& item : req["projects"]) {
            if (item.is_string()) {
                const std::string p = trim(item.get<std::string>());
                if (!p.empty()) explicit_projects.push_back(p);
            }
        }
    }
    for (const auto& p : explicit_projects) {
        if (!isKnownWorkflow(p)) {
            return makeSchedError("unknown_workflow", "Unknown daily workflow: " + p);
        }
    }

    struct StartReservation {
        std::atomic<bool>& flag;
        bool held = false;

        explicit StartReservation(std::atomic<bool>& value) : flag(value) {}
        ~StartReservation() {
            if (held) flag.store(false);
        }

        void hold() {
            flag.store(true);
            held = true;
        }

        void clear() {
            flag.store(false);
            held = false;
        }
    } reservation(start_in_progress_);

    bool already_running = false;
    bool auto_run_disabled = false;
    bool do_pre_pull = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        loadConfigAndStateLocked(paths.curation_dir);

        already_running = running_ || start_in_progress_.load() || shutting_down_.load();
        if (!already_running) {
            if (is_auto && !config_.auto_run_enabled) {
                auto_run_disabled = true;
            } else {
                reservation.hold();
                do_pre_pull = config_.git_sync_enabled && req.value("sync_remote", true);
            }
        }
    }

    if (already_running) {
        omnidesk::CapabilityResult res;
        res.ok = true;
        res.data = inspectStatus(paths);
        res.data["started"] = false;
        res.data["already_running"] = true;
        return res;
    }
    if (auto_run_disabled) {
        omnidesk::CapabilityResult res;
        res.ok = true;
        res.data = inspectStatus(paths);
        res.data["started"] = false;
        res.data["skipped_reason"] = "auto_run_disabled";
        return res;
    }

    nlohmann::json pre_sync_result = nlohmann::json::object();
    if (do_pre_pull) {
        pre_sync_result = syncGitHubRepos(paths, false, true);
    }

    if (shutting_down_.load()) {
        reservation.clear();
        return makeSchedError("scheduler_stopping", "Daily workflow scheduler is shutting down");
    }

    std::unique_lock<std::mutex> tlock(thread_lifecycle_mutex_);
    if (shutting_down_.load()) {
        reservation.clear();
        tlock.unlock();
        return makeSchedError("scheduler_stopping", "Daily workflow scheduler is shutting down");
    }

    std::string run_id;
    std::vector<std::string> to_run;
    std::string runner_id;
    std::string runner_bin_or_cmd;
    bool nothing_pending = false;
    bool no_agent_runner = false;
    std::string runner_err;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        loadConfigAndStateLocked(paths.curation_dir);

        std::vector<std::string> candidates;
        if (!explicit_projects.empty()) {
            candidates = explicit_projects;
        } else {
            for (const auto& def : workflowDefinitions()) {
                const auto it = config_.project_enabled.find(def.id);
                const bool enabled = (it != config_.project_enabled.end()) ? it->second : true;
                if (enabled) candidates.push_back(def.id);
            }
        }

        for (const auto& proj : candidates) {
            bool repo_ok = false, live_ok = false, receipt_ok = false, prep_ok = false;
            std::vector<std::string> existing;
            const std::string st = inspectSingleProjectArtifactState(
                proj, today, paths, repo_ok, live_ok, receipt_ok, prep_ok, existing);
            if (st == "published") {
                continue; // Never overwrite already-published today's daily_immutable bundle
            }
            if (is_auto) {
                const auto att_it = today_attempts_.find(proj);
                if (att_it != today_attempts_.end() && att_it->second.auto_attempted) {
                    continue;
                }
            }
            if (std::find(to_run.begin(), to_run.end(), proj) == to_run.end()) {
                to_run.push_back(proj);
            }
        }

        if (to_run.empty()) {
            nothing_pending = true;
        }

        if (!nothing_pending) {
            const auto available = detectAvailableAgentsLocked();
            if (!resolveSelectedRunnerLocked(available, runner_id, runner_bin_or_cmd, runner_err)) {
                no_agent_runner = true;
            }
        }
    }

    if (nothing_pending) {
        reservation.clear();
        tlock.unlock();
        omnidesk::CapabilityResult res;
        res.ok = true;
        res.data = inspectStatus(paths);
        res.data["started"] = false;
        res.data["skipped_reason"] = "nothing_pending_today";
        if (!pre_sync_result.empty()) {
            res.data["git_sync"] = std::move(pre_sync_result);
        }
        return res;
    }

    if (no_agent_runner) {
        reservation.clear();
        tlock.unlock();
        if (is_auto) {
            omnidesk::CapabilityResult res;
            res.ok = true;
            res.data = inspectStatus(paths);
            res.data["started"] = false;
            res.data["skipped_reason"] = "no_agent_runner";
            return res;
        }
        return makeSchedError("no_agent_runner", runner_err);
    }

    if (worker_thread_.joinable()) {
        worker_thread_.join();
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (shutting_down_.load()) {
            reservation.clear();
            tlock.unlock();
            return makeSchedError("scheduler_stopping",
                                  "Daily workflow scheduler is shutting down");
        }
        cancel_requested_.store(false);
        running_ = true;
        reservation.clear();
        active_child_pgid_ = -1;
        run_id = "sched-" + today + "-" +
                 std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(
                                    std::chrono::system_clock::now().time_since_epoch())
                                    .count());
        active_run_id_ = run_id;
        active_trigger_ = is_auto ? "auto" : "manual";
        active_runner_id_ = runner_id;
        active_project_ = to_run.front();
        queued_projects_.assign(to_run.begin() + 1, to_run.end());
        completed_projects_in_run_ = 0;
        total_projects_in_run_ = static_cast<int>(to_run.size());
        active_progress_ = 0.02f;
        active_stage_message_ = "正在启动 " + runner_id + " 执行今日工作流 (" + to_run.front() + ")…";
        active_live_line_.clear();

        if (is_auto) {
            for (const auto& proj : to_run) {
                auto& rec = today_attempts_[proj];
                rec.business_date = today;
                rec.auto_attempted = true;
            }
            saveStateLocked(paths.curation_dir);
        }

        worker_thread_ = std::thread(&DailyWorkflowScheduler::executeRunLoop,
                                     this,
                                     run_id,
                                     today,
                                     active_trigger_,
                                     paths,
                                     to_run,
                                     runner_id,
                                     runner_bin_or_cmd,
                                     timeout_sec);
    }

    publishSchedulerChanged("started", paths);

    if (wait_flag) {
        if (worker_thread_.joinable()) {
            worker_thread_.join();
        }
    }
    tlock.unlock();

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = inspectStatus(paths);
    res.data["started"] = true;
    res.data["run_id"] = run_id;
    res.data["scheduled_projects"] = to_run;
    if (!pre_sync_result.empty()) {
        res.data["git_sync"] = std::move(pre_sync_result);
    }
    return res;
}

omnidesk::CapabilityResult DailyWorkflowScheduler::cancelWorkflows() {
    bool was_running = false;
    RepoPaths empty_paths;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        was_running = running_;
        if (running_) {
            cancel_requested_.store(true);
            active_stage_message_ = "正在终止后台 Agent 工作流…";
            if (active_child_pgid_ > 0) {
                ::kill(-active_child_pgid_, SIGTERM);
            }
        }
        empty_paths.curation_dir = loaded_curation_dir_;
    }

    if (was_running) {
        publishSchedulerChanged("cancelling", empty_paths);
    }

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = {
        {"cancelling", was_running},
        {"was_running", was_running},
    };
    return res;
}

void DailyWorkflowScheduler::executeRunLoop(std::string run_id,
                                            std::string today,
                                            std::string trigger_source,
                                            RepoPaths paths,
                                            std::vector<std::string> projects,
                                            std::string runner_id,
                                            std::string runner_bin_or_cmd,
                                            int timeout_sec) {
    (void)run_id;
    int succeeded = 0;
    int failed = 0;
    int cancelled_count = 0;
    int github_synced_count = 0;
    int github_push_warn_count = 0;
    const int total = static_cast<int>(projects.size());

    for (int idx = 0; idx < total; ++idx) {
        const std::string& project = projects[static_cast<size_t>(idx)];
        if (cancel_requested_.load()) {
            ++cancelled_count;
            std::lock_guard<std::mutex> lock(mutex_);
            auto& rec = today_attempts_[project];
            rec.business_date = today;
            rec.last_status = "cancelled";
            rec.last_finished_at = currentShanghaiTimestamp();
            saveStateLocked(paths.curation_dir);
            continue;
        }

        std::string wf_label = project;
        for (const auto& def : workflowDefinitions()) {
            if (def.id == project) {
                wf_label = def.label;
                break;
            }
        }

        bool do_git_sync = true;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            do_git_sync = config_.git_sync_enabled;
        }
        if (do_git_sync && idx > 0) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                active_stage_message_ = "[" + std::to_string(idx + 1) + "/" + std::to_string(total) +
                                        "] 正在同步 GitHub 双仓最新提交 (" + wf_label + ")…";
            }
            publishSchedulerChanged("git_pulling", paths);
            (void)syncGitHubRepos(paths, false, true);
        }

        bool repo_ok = false, live_ok = false, receipt_ok = false, prep_ok = false;
        std::vector<std::string> existing_targets;
        const std::string pre_state = inspectSingleProjectArtifactState(
            project, today, paths, repo_ok, live_ok, receipt_ok, prep_ok, existing_targets);

        if (pre_state == "published") {
            ++succeeded;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                auto& rec = today_attempts_[project];
                rec.business_date = today;
                rec.last_status = "published";
                if (do_git_sync) {
                    rec.git_sync_status = "pulled";
                    rec.git_sync_detail = "已从 GitHub 同步今日已完成内容";
                }
                completed_projects_in_run_ = idx + 1;
                active_progress_ = static_cast<float>(idx + 1) / static_cast<float>(std::max(1, total));
                saveStateLocked(paths.curation_dir);
            }
            continue;
        }

        const std::string log_path =
            (fs::path(paths.curation_dir) / "scheduler" / "logs" / (today + "-" + project + ".log"))
                .string();

        {
            std::lock_guard<std::mutex> lock(mutex_);
            active_project_ = project;
            queued_projects_.clear();
            for (int q = idx + 1; q < total; ++q) {
                queued_projects_.push_back(projects[static_cast<size_t>(q)]);
            }
            completed_projects_in_run_ = idx;
            active_progress_ =
                (static_cast<float>(idx) + 0.1f) / static_cast<float>(std::max(1, total));
            active_stage_message_ = "[" + std::to_string(idx + 1) + "/" + std::to_string(total) +
                                    "] 正在运行 " + wf_label + " (" + runner_id + ")…";
            active_live_line_.clear();

            auto& rec = today_attempts_[project];
            rec.business_date = today;
            if (trigger_source == "auto") rec.auto_attempted = true;
            rec.attempt_count += 1;
            rec.last_status = "running";
            rec.last_error.clear();
            rec.git_sync_status.clear();
            rec.git_sync_detail.clear();
            rec.last_started_at = currentShanghaiTimestamp();
            rec.last_runner = runner_id;
            rec.log_path = log_path;
            saveStateLocked(paths.curation_dir);
        }
        publishSchedulerChanged("project_started", paths);

        const std::string prompt = buildWorkflowPrompt(project, today, paths, prep_ok);
        bool was_cancelled = false;
        bool timed_out = false;
        std::string run_err;
        const int exit_code = runAgentSubprocess(project,
                                                 today,
                                                 paths,
                                                 runner_id,
                                                 runner_bin_or_cmd,
                                                 prompt,
                                                 log_path,
                                                 timeout_sec,
                                                 was_cancelled,
                                                 timed_out,
                                                 run_err);

        if (on_pipeline_finished_) {
            try {
                on_pipeline_finished_(project, today, paths);
            } catch (...) {
            }
        }

        bool post_repo = false, post_live = false, post_receipt = false, post_prep = false;
        std::vector<std::string> post_existing;
        const std::string post_state = inspectSingleProjectArtifactState(
            project, today, paths, post_repo, post_live, post_receipt, post_prep, post_existing);

        std::string git_sync_status;
        std::string git_sync_detail;
        std::string git_push_warning;
        if (!was_cancelled && post_state == "published" && do_git_sync) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                active_stage_message_ = "[" + std::to_string(idx + 1) + "/" + std::to_string(total) +
                                        "] 正在同步推送今日更新至 GitHub (" + wf_label + ")…";
            }
            publishSchedulerChanged("git_pushing", paths);

            auto check_paths = canonicalRepoTargets(project, today, paths);
            if (project == "music" && !paths.obsidian_vault_dir.empty()) {
                check_paths.push_back(
                    (fs::path(paths.obsidian_vault_dir) / "Music" / "Daily" / (today + ".md")).string());
            }

            RepoPushStatus obs_push;
            RepoPushStatus mus_push;
            {
                std::lock_guard<std::mutex> sync_lock(git_sync_mutex_);
                obs_push = GitMirror::pushRepo(paths.obsidian_vault_dir, "main", check_paths);
                if (project == "music") {
                    mus_push = GitMirror::pushRepo(paths.music_daily_dir, "main", check_paths);
                }
            }

            std::ofstream log_append(log_path, std::ios::out | std::ios::app);
            if (log_append.is_open()) {
                log_append << "\n=== GitHub Sync Verification ===\n"
                           << "obsidian_vault: " << obs_push.toJson().dump() << "\n";
                if (project == "music") {
                    log_append << "music_daily: " << mus_push.toJson().dump() << "\n";
                }
                log_append.flush();
            }

            const bool any_remote = obs_push.has_remote || (project == "music" && mus_push.has_remote);
            const bool both_ok = obs_push.ok() && (project != "music" || mus_push.ok());
            const bool any_pushed = obs_push.pushed || (project == "music" && mus_push.pushed);

            if (!any_remote) {
                git_sync_status = "local_only";
                git_sync_detail = "本地仓库未配置 origin 远端";
            } else if (both_ok) {
                git_sync_status = any_pushed ? "pushed" : "synced";
                git_sync_detail = any_pushed ? "已推送至 GitHub origin/main" : "已与 GitHub origin/main 同步";
                ++github_synced_count;
            } else {
                git_sync_status = "push_failed";
                git_push_warning = !obs_push.error.empty() ? obs_push.error : mus_push.error;
                git_sync_detail = git_push_warning;
                ++github_push_warn_count;
            }
        }

        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto& rec = today_attempts_[project];
            rec.last_finished_at = currentShanghaiTimestamp();
            rec.git_sync_status = git_sync_status;
            rec.git_sync_detail = git_sync_detail;

            if (git_sync_status == "pushed" || git_sync_status == "synced") {
                last_git_sync_at_ = rec.last_finished_at;
                last_git_sync_summary_ = git_sync_detail;
            } else if (git_sync_status == "push_failed" && !git_push_warning.empty()) {
                last_git_sync_at_ = rec.last_finished_at;
                last_git_sync_summary_ = "GitHub 待同步: " + utf8Truncate(git_push_warning, 72);
            }

            if (was_cancelled) {
                ++cancelled_count;
                rec.last_status = "cancelled";
                rec.last_error = "用户已手动停止运行";
            } else if (post_state == "published") {
                ++succeeded;
                rec.last_status = "published";
                if (!git_push_warning.empty()) {
                    rec.last_error = "本地已发布，但同步 GitHub 待处理: " + git_push_warning;
                } else {
                    rec.last_error.clear();
                }
            } else {
                ++failed;
                rec.last_status = "failed";
                if (timed_out) {
                    rec.last_error = "Agent 执行超时 (" + std::to_string(timeout_sec) + "s)";
                } else if (!run_err.empty()) {
                    rec.last_error = run_err;
                } else if (exit_code != 0) {
                    rec.last_error = "Agent 退出码 " + std::to_string(exit_code) +
                                     (active_live_line_.empty() ? "" : (" · " + active_live_line_));
                } else {
                    rec.last_error = "Agent 已退出，但未检测到今日已发布推荐产物（请检查日志）";
                }
            }

            completed_projects_in_run_ = idx + 1;
            active_progress_ =
                static_cast<float>(idx + 1) / static_cast<float>(std::max(1, total));
            saveStateLocked(paths.curation_dir);
        }
        publishSchedulerChanged("project_finished", paths);
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        running_ = false;
        active_child_pgid_ = -1;
        active_project_.clear();
        queued_projects_.clear();
        active_progress_ = 1.0f;

        if (cancelled_count > 0) {
            last_run_summary_ = "已停止后台运行（完成 " + std::to_string(succeeded) + "/" +
                                std::to_string(total) + " 项）";
        } else if (failed == 0) {
            last_run_summary_ = "今日定时推荐已完成（本次成功运行 " + std::to_string(succeeded) + " 项" +
                                (github_synced_count > 0 ? " · 已同步 GitHub" : "") +
                                (github_push_warn_count > 0 ? " · GitHub 待推送" : "") + "）";
        } else {
            last_run_summary_ = "后台运行结束：成功 " + std::to_string(succeeded) + " 项，失败 " +
                                std::to_string(failed) + " 项";
        }
        active_stage_message_ = last_run_summary_;
        saveStateLocked(paths.curation_dir);
    }

    publishSchedulerChanged("finished", paths);
}

int DailyWorkflowScheduler::runAgentSubprocess(const std::string& project,
                                               const std::string& today,
                                               const RepoPaths& paths,
                                               const std::string& runner_id,
                                               const std::string& runner_bin_or_cmd,
                                               const std::string& prompt,
                                               const std::string& log_path,
                                               int timeout_sec,
                                               bool& out_cancelled,
                                               bool& out_timed_out,
                                               std::string& out_error) {
    out_cancelled = false;
    out_timed_out = false;
    out_error.clear();

    std::error_code ec;
    fs::create_directories(fs::path(log_path).parent_path(), ec);

    std::string workdir = paths.obsidian_vault_dir;
    if (workdir.empty() || !fs::is_directory(workdir, ec)) {
        workdir = paths.curation_dir;
    }

    std::vector<std::string> argv_strings;
    if (runner_id == "custom") {
        argv_strings = {"/bin/sh", "-c", runner_bin_or_cmd};
    } else if (runner_id == "agy-cli" || runner_id == "agy") {
        argv_strings = {
            runner_bin_or_cmd,
            "-p",
            prompt,
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
        };
        if (!paths.obsidian_vault_dir.empty() && fs::is_directory(paths.obsidian_vault_dir, ec)) {
            argv_strings.push_back("--add-dir");
            argv_strings.push_back(paths.obsidian_vault_dir);
        }
        if (!paths.music_daily_dir.empty() && fs::is_directory(paths.music_daily_dir, ec)) {
            argv_strings.push_back("--add-dir");
            argv_strings.push_back(paths.music_daily_dir);
        }
        if (!paths.curation_dir.empty() && fs::is_directory(paths.curation_dir, ec)) {
            argv_strings.push_back("--add-dir");
            argv_strings.push_back(paths.curation_dir);
        }
    } else if (runner_id == "codex") {
        const CodexFlagSupport flags = probeCodexFlags(runner_bin_or_cmd);
        argv_strings.push_back(runner_bin_or_cmd);
        argv_strings.push_back("exec");
        if (flags.bypass_approvals_and_sandbox) {
            argv_strings.push_back("--dangerously-bypass-approvals-and-sandbox");
        } else if (flags.full_auto) {
            argv_strings.push_back("--full-auto");
        }
        if (flags.skip_git_repo_check) {
            argv_strings.push_back("--skip-git-repo-check");
        }
        if (flags.cd_flag && !workdir.empty()) {
            argv_strings.push_back("-C");
            argv_strings.push_back(workdir);
        }
        argv_strings.push_back(prompt);
    } else if (runner_id == "claude") {
        argv_strings = {runner_bin_or_cmd, "-p", "--dangerously-skip-permissions", prompt};
    } else if (runner_id == "opencode") {
        argv_strings = {runner_bin_or_cmd, "run", prompt};
    } else if (runner_id == "gemini") {
        argv_strings = {runner_bin_or_cmd, "-p", prompt, "--yolo"};
    } else {
        out_error = "未知 Agent 执行器: " + runner_id;
        return -1;
    }

    int out_pipe[2] = {-1, -1};
    if (::pipe(out_pipe) != 0) {
        out_error = std::string("pipe() failed: ") + std::strerror(errno);
        return -1;
    }

    const std::string skill_path = resolveSkillMarkdownPath();
    const std::string pm_bin = resolveOmnideskPmBinary();
    const std::string enhanced_path = buildEnhancedPathEnv();

    const pid_t pid = ::fork();
    if (pid < 0) {
        ::close(out_pipe[0]);
        ::close(out_pipe[1]);
        out_error = std::string("fork() failed: ") + std::strerror(errno);
        return -1;
    }

    if (pid == 0) {
        ::setpgid(0, 0);
        ::close(out_pipe[0]);
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::dup2(out_pipe[1], STDERR_FILENO);
        ::close(out_pipe[1]);

        const int devnull = ::open("/dev/null", O_RDONLY);
        if (devnull >= 0) {
            ::dup2(devnull, STDIN_FILENO);
            if (devnull > 2) ::close(devnull);
        }

        if (!workdir.empty()) {
            if (::chdir(workdir.c_str()) != 0) {
                // Keep current dir if chdir fails
            }
        }

        if (runner_id == "agy-cli" || runner_id == "agy") {
            // Never route Antigravity CLI through Magpie's blocked proxy port
            ::unsetenv("GEMINI_API_KEY");
            ::unsetenv("GOOGLE_GEMINI_BASE_URL");
            ::unsetenv("GOOGLE_GENAI_USE_VERTEXAI");
        }

        ::setenv("TZ", "Asia/Shanghai", 1);
        ::setenv("PATH", enhanced_path.c_str(), 1);
        ::setenv("OMNIDESK_AGENT_PROMPT", prompt.c_str(), 1);
        ::setenv("OMNIDESK_CURATION_PROJECT", project.c_str(), 1);
        ::setenv("OMNIDESK_BUSINESS_DATE", today.c_str(), 1);
        ::setenv("OMNIDESK_SKILL_PATH", skill_path.c_str(), 1);
        ::setenv("OMNIDESK_OBSIDIAN_VAULT_DIR", paths.obsidian_vault_dir.c_str(), 1);
        ::setenv("OMNIDESK_MUSIC_DAILY_DIR", paths.music_daily_dir.c_str(), 1);
        ::setenv("OMNIDESK_CURATION_DIR", paths.curation_dir.c_str(), 1);
        ::setenv("OMNIDESK_PM_BIN", pm_bin.c_str(), 1);
        ::setenv("CI", "1", 1);

        std::vector<char*> c_args;
        c_args.reserve(argv_strings.size() + 1);
        for (auto& s : argv_strings) {
            c_args.push_back(s.data());
        }
        c_args.push_back(nullptr);

        ::execv(c_args[0], c_args.data());
        const char* msg = "execv failed for CLI agent\n";
        (void)::write(STDERR_FILENO, msg, std::strlen(msg));
        ::_exit(127);
    }

    ::setpgid(pid, pid);
    ::close(out_pipe[1]);
    const int flags = ::fcntl(out_pipe[0], F_GETFL, 0);
    if (flags >= 0) ::fcntl(out_pipe[0], F_SETFL, flags | O_NONBLOCK);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        active_child_pgid_ = pid;
    }

    std::ofstream log_file(log_path, std::ios::out | std::ios::trunc);
    if (log_file.is_open()) {
        log_file << "=== OmniDesk Daily Workflow Log ===\n"
                 << "started_at: " << currentShanghaiTimestamp() << "\n"
                 << "project: " << project << "\n"
                 << "business_date: " << today << "\n"
                 << "runner: " << runner_id << " (" << runner_bin_or_cmd << ")\n"
                 << "workdir: " << workdir << "\n"
                 << "===================================\n\n";
        log_file.flush();
    }

    const auto start_tp = std::chrono::steady_clock::now();
    auto last_event_tp = start_tp;
    std::string line_buf;
    bool pipe_open = true;
    int status = 0;
    bool child_reaped = false;

    auto flush_line_update = [&](const std::string& raw_line) {
        const std::string clean = sanitizeLiveLogLine(raw_line);
        if (clean.empty()) return;
        bool should_emit = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            active_live_line_ = clean;
            const auto now_tp = std::chrono::steady_clock::now();
            if (std::chrono::duration_cast<std::chrono::milliseconds>(now_tp - last_event_tp).count() >= 900) {
                last_event_tp = now_tp;
                should_emit = true;
            }
        }
        if (should_emit) {
            publishSchedulerChanged("progress", paths);
        }
    };

    while (pipe_open || !child_reaped) {
        if (!child_reaped) {
            const pid_t w = ::waitpid(pid, &status, WNOHANG);
            if (w == pid) {
                child_reaped = true;
            }
        }

        const auto elapsed_sec = std::chrono::duration_cast<std::chrono::seconds>(
                                     std::chrono::steady_clock::now() - start_tp)
                                     .count();
        const bool cancelled_now = cancel_requested_.load();
        const bool timed_out_now = (!cancelled_now && elapsed_sec >= timeout_sec);

        if ((cancelled_now || timed_out_now) && !child_reaped) {
            out_cancelled = cancelled_now;
            out_timed_out = timed_out_now;
            ::kill(-pid, SIGTERM);
            for (int i = 0; i < 15 && !child_reaped; ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                if (::waitpid(pid, &status, WNOHANG) == pid) {
                    child_reaped = true;
                    break;
                }
            }
            if (!child_reaped) {
                ::kill(-pid, SIGKILL);
                (void)::waitpid(pid, &status, 0);
                child_reaped = true;
            }
        }

        if (pipe_open) {
            struct pollfd pfd {};
            pfd.fd = out_pipe[0];
            pfd.events = POLLIN | POLLHUP;
            const int pr = ::poll(&pfd, 1, child_reaped ? 0 : 120);
            if (pr > 0 && (pfd.revents & (POLLIN | POLLHUP))) {
                char buf[4096];
                for (;;) {
                    const ssize_t nr = ::read(out_pipe[0], buf, sizeof(buf));
                    if (nr > 0) {
                        if (log_file.is_open()) {
                            log_file.write(buf, nr);
                            log_file.flush();
                        }
                        line_buf.append(buf, static_cast<size_t>(nr));
                        size_t pos = 0;
                        while ((pos = line_buf.find('\n')) != std::string::npos) {
                            std::string one_line = line_buf.substr(0, pos);
                            line_buf.erase(0, pos + 1);
                            flush_line_update(one_line);
                        }
                    } else if (nr == 0) {
                        pipe_open = false;
                        break;
                    } else {
                        if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                        pipe_open = false;
                        break;
                    }
                }
            } else if (child_reaped) {
                pipe_open = false;
            }
        } else if (!child_reaped) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }

    if (!line_buf.empty()) {
        flush_line_update(line_buf);
    }
    ::close(out_pipe[0]);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (active_child_pgid_ == pid) {
            active_child_pgid_ = -1;
        }
    }

    int exit_code = -1;
    if (WIFEXITED(status)) {
        exit_code = WEXITSTATUS(status);
    } else if (WIFSIGNALED(status)) {
        exit_code = 128 + WTERMSIG(status);
    }

    if (log_file.is_open()) {
        log_file << "\n=== Finished at " << currentShanghaiTimestamp()
                 << " (exit_code=" << exit_code
                 << ", cancelled=" << (out_cancelled ? "true" : "false")
                 << ", timed_out=" << (out_timed_out ? "true" : "false") << ") ===\n";
        log_file.flush();
    }

    return exit_code;
}

void DailyWorkflowScheduler::publishSchedulerChanged(const std::string& event_reason,
                                                     const RepoPaths& paths) {
    nlohmann::json payload = inspectStatus(paths);
    payload["event"] = event_reason;
    ctx_.publish("entity.curation.scheduler_changed", std::move(payload));
}

} // namespace ai_curator
