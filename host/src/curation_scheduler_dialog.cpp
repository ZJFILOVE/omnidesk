#include "omnidesk/host/curation_scheduler_dialog.hpp"

#include <algorithm>
#include <borealis.hpp>
#include <chrono>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {

namespace {

std::string workflowStatusText(const nlohmann::json& wf) {
    const std::string st = wf.value("status", "pending");
    const std::string gs = wf.value("git_sync_status", "");
    if (st == "published") {
        if (gs == "pushed" || gs == "synced" || gs == "pulled") return "今日已完成 · GitHub 已同步";
        if (gs == "push_failed") return "今日已完成 · 待推送 GitHub";
        return "今日已完成";
    }
    if (st == "running") return "后台运行中…";
    if (st == "queued") return "排队等待";
    if (st == "prepared_recovery") return "待恢复事务";
    if (st == "failed") return "执行失败";
    if (st == "cancelled") return "已手动停止";
    if (st == "disabled") return "已关闭";
    return "今日未运行";
}

const char* workflowStatusColorToken(const std::string& st) {
    if (st == "published") return "status/ok_fg";
    if (st == "running" || st == "queued") return "domain/core";
    if (st == "failed") return "status/err_fg";
    if (st == "prepared_recovery" || st == "cancelled") return "status/warn_fg";
    return "font/grey";
}

class SchedulerDialogBox final : public brls::Box {
public:
    explicit SchedulerDialogBox(std::function<void()> on_open_calendar)
        : brls::Box(brls::Axis::COLUMN),
          on_open_calendar_(std::move(on_open_calendar)) {
        setPadding(20, 22, 18, 22);
        setWidth(860);

        auto* hdr = new brls::Box(brls::Axis::ROW);
        hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        hdr->setAlignItems(brls::AlignItems::CENTER);
        hdr->setMarginBottom(8);
        hdr->addView(ui::makeLabel("每日定时推荐 · GitHub 双向同步与后台调度", 19.5f, "brls/text"));
        date_badge_ = ui::makeLabel("Asia/Shanghai · 今日检测", 13.0f, "font/grey");
        hdr->addView(date_badge_);
        addView(hdr);

        summary_label_ = ui::makeLabel("正在读取今日工作流状态…", 14.0f, "domain/core", 1.4f);
        summary_label_->setMarginBottom(10);
        addView(summary_label_);

        // Master controls & runner bar
        controls_box_ = new brls::Box(brls::Axis::COLUMN);
        controls_box_->setMarginBottom(10);
        addView(controls_box_);

        // Workflows list
        auto* scroll = new brls::ScrollingFrame();
        scroll->setWidthPercentage(100);
        scroll->setHeight(330);
        workflows_box_ = new brls::Box(brls::Axis::COLUMN);
        workflows_box_->setWidthPercentage(100);
        scroll->setContentView(workflows_box_);
        addView(scroll);

        sub_token_ = CurationSchedulerManager::instance().subscribeStatus(
            [alive = alive_, this](const nlohmann::json& st) {
                if (!alive->load(std::memory_order_acquire)) return;
                brls::sync([alive, this, st]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    renderStatus(st);
                });
            });

        const auto cached = CurationSchedulerManager::instance().latestStatus();
        if (cached.is_object() && !cached.empty()) {
            renderStatus(cached);
        }
        CurationSchedulerManager::instance().refreshStatus(false, false);
    }

    ~SchedulerDialogBox() override {
        alive_->store(false, std::memory_order_release);
        if (sub_token_ != 0) {
            CurationSchedulerManager::instance().unsubscribeStatus(sub_token_);
            sub_token_ = 0;
        }
    }

    void setDialog(brls::Dialog* dlg) { dialog_ = dlg; }

private:
    void renderStatus(const nlohmann::json& st) {
        if (!st.is_object() || st.empty()) return;

        const std::string today = st.value("business_date", "");
        const std::string wd = st.value("weekday_label", "");
        if (date_badge_) {
            date_badge_->setText("Asia/Shanghai · " + today + (wd.empty() ? "" : (" " + wd)));
        }

        const bool running = st.value("running", false);
        const bool auto_on = st.value("auto_run_enabled", true);
        const bool git_sync_on = st.value("git_sync_enabled", true);
        const std::string summary = st.value("summary_text", "");
        const std::string live_line = st.value("live_output_line", "");
        if (summary_label_) {
            std::string txt = summary;
            if (running && !live_line.empty()) {
                txt += "\n最近输出: " + live_line;
            }
            summary_label_->setText(txt);
            summary_label_->setTextColor(ui::color(running ? "domain/core" : "brls/text"));
        }

        if (controls_box_) {
            controls_box_->clearViews();

            auto* row1 = new brls::Box(brls::Axis::ROW);
            row1->setAlignItems(brls::AlignItems::CENTER);
            row1->setMarginBottom(8);

            auto* auto_btn = ui::makeButton(
                auto_on ? "自动补跑: 已开启 ✓" : "自动补跑: 已关闭",
                [auto_on]() {
                    CurationSchedulerManager::instance().setAutoRunEnabled(!auto_on);
                },
                auto_on, 36.0f);
            auto_btn->setMarginRight(8);
            row1->addView(auto_btn);

            auto* git_btn = ui::makeButton(
                git_sync_on ? "GitHub 同步: 已开启 ✓" : "GitHub 同步: 已关闭",
                [git_sync_on]() {
                    CurationSchedulerManager::instance().setGitSyncEnabled(!git_sync_on);
                },
                git_sync_on, 36.0f);
            git_btn->setMarginRight(8);
            row1->addView(git_btn);

            if (!running) {
                auto* run_all = ui::makeButton(
                    "▶ 同步并补跑今日缺失",
                    []() { CurationSchedulerManager::instance().runMissingToday("manual"); },
                    true, 36.0f);
                run_all->setMarginRight(8);
                row1->addView(run_all);

                auto* sync_now_btn = ui::makeButton(
                    "⇄ 同步 GitHub",
                    []() { CurationSchedulerManager::instance().syncGitHubNow(); },
                    false, 36.0f);
                sync_now_btn->setMarginRight(8);
                row1->addView(sync_now_btn);
            } else {
                auto* stop_btn = ui::makeButton(
                    "■ 停止当前后台运行",
                    []() { CurationSchedulerManager::instance().cancelRun(); },
                    true, 36.0f);
                stop_btn->setMarginRight(8);
                row1->addView(stop_btn);
            }

            if (on_open_calendar_) {
                auto* cal_btn = ui::makeButton(
                    "推荐日历",
                    [this]() {
                        auto cb = on_open_calendar_;
                        if (dialog_) dialog_->dismiss();
                        if (cb) brls::sync([cb]() { cb(); });
                    },
                    false, 36.0f);
                row1->addView(cal_btn);
            }
            controls_box_->addView(row1);

            // Agent runner row
            auto* row2 = new brls::Box(brls::Axis::ROW);
            row2->setAlignItems(brls::AlignItems::CENTER);
            auto* r_lbl = ui::makeLabel("Agent 基座:", 13.5f, "font/grey");
            r_lbl->setMarginRight(8);
            row2->addView(r_lbl);

            std::string selected_runner = st.value("agent_runner", "auto");
            if (selected_runner == "agy") selected_runner = "agy-cli";
            const std::string resolved_runner = st.value("resolved_runner", "");
            std::vector<std::string> avail_ids;
            if (st.contains("available_agents") && st["available_agents"].is_array()) {
                for (const auto& a : st["available_agents"]) {
                    if (a.is_object()) avail_ids.push_back(a.value("id", ""));
                }
            }

            const struct {
                const char* id;
                const char* label;
            } kRunners[] = {
                {"auto", "自动"},
                {"agy-cli", "agy-cli"},
                {"codex", "Codex"},
                {"claude", "Claude"},
                {"opencode", "OpenCode"},
                {"gemini", "Gemini"},
                {"custom", "自定义命令"},
            };
            for (const auto& r : kRunners) {
                const std::string rid = r.id;
                const bool active = (selected_runner == rid);
                const bool installed =
                    (rid == "auto" || rid == "custom" ||
                     std::find(avail_ids.begin(), avail_ids.end(), rid) != avail_ids.end());
                std::string btn_label = r.label;
                if (rid == "auto" && !resolved_runner.empty()) {
                    btn_label += " (" + resolved_runner + ")";
                } else if (!installed) {
                    btn_label += " (未装)";
                }
                auto* b = ui::makeButton(
                    btn_label,
                    [rid, st]() {
                        if (rid == "custom") {
                            if (auto* plat = brls::Application::getPlatform()) {
                                const std::string cur = st.value("custom_agent_command", "");
                                plat->getImeManager()->openForText(
                                    [](std::string cmd) {
                                        if (cmd.empty()) return;
                                        CurationSchedulerManager::instance().setAgentRunner("custom", cmd);
                                    },
                                    "自定义后台 Agent 执行命令",
                                    "环境变量 OMNIDESK_AGENT_PROMPT / OMNIDESK_CURATION_PROJECT / OMNIDESK_BUSINESS_DATE 会自动注入",
                                    512,
                                    cur);
                            }
                        } else {
                            CurationSchedulerManager::instance().setAgentRunner(rid);
                        }
                    },
                    active, 30.0f);
                b->setMarginRight(6);
                row2->addView(b);
            }
            controls_box_->addView(row2);

            if (!st.value("runner_ready", true) && !st.value("runner_error", "").empty()) {
                auto* warn = ui::makeLabel("⚠ " + st.value("runner_error", ""), 13.0f, "status/warn_fg", 1.35f);
                warn->setMarginTop(6);
                controls_box_->addView(warn);
            }
        }

        if (workflows_box_) {
            workflows_box_->clearViews();
            if (st.contains("workflows") && st["workflows"].is_array()) {
                for (const auto& wf : st["workflows"]) {
                    if (!wf.is_object()) continue;
                    const std::string proj = wf.value("id", "");
                    const std::string label = wf.value("label", proj);
                    const std::string hint = wf.value("schedule_hint", "");
                    const std::string domains = wf.value("domains_summary", "");
                    const bool enabled = wf.value("enabled", true);
                    const std::string wf_st = wf.value("status", "pending");
                    const std::string err = wf.value("last_error", "");

                    auto* card = ui::makeCard("desktop/surface", 14.0f, 10.0f);
                    card->setMarginBottom(8);

                    auto* top = new brls::Box(brls::Axis::ROW);
                    top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
                    top->setAlignItems(brls::AlignItems::CENTER);

                    auto* left = new brls::Box(brls::Axis::COLUMN);
                    left->setGrow(1.0f);
                    left->setMarginRight(10);
                    left->addView(ui::makeLabel(
                        label + (hint.empty() ? "" : ("  ·  建议时段 " + hint)),
                        15.5f, "brls/text"));
                    auto* sub = ui::makeLabel(domains, 13.0f, "font/grey");
                    sub->setMarginTop(2);
                    left->addView(sub);
                    top->addView(left);

                    auto* right = new brls::Box(brls::Axis::ROW);
                    right->setAlignItems(brls::AlignItems::CENTER);
                    auto* pill = ui::makeLabel(workflowStatusText(wf), 13.5f,
                                               workflowStatusColorToken(wf_st));
                    pill->setMarginRight(10);
                    right->addView(pill);

                    auto* toggle_btn = ui::makeButton(
                        enabled ? "已启用" : "已关闭",
                        [proj, enabled]() {
                            CurationSchedulerManager::instance().setProjectEnabled(proj, !enabled);
                        },
                        enabled, 30.0f);
                    toggle_btn->setMarginRight(6);
                    right->addView(toggle_btn);

                    if (!running && wf_st != "published") {
                        auto* run_one = ui::makeButton(
                            "单独运行",
                            [proj]() {
                                CurationSchedulerManager::instance().runSingleWorkflow(proj);
                            },
                            false, 30.0f);
                        right->addView(run_one);
                    }

                    top->addView(right);
                    card->addView(top);

                    if (!err.empty()) {
                        auto* err_lbl = ui::makeLabel("提示: " + err, 12.5f, "status/err_fg", 1.35f);
                        err_lbl->setMarginTop(6);
                        card->addView(err_lbl);
                    }
                    workflows_box_->addView(card);
                }
            }
        }
    }

    std::function<void()> on_open_calendar_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    uint64_t sub_token_ = 0;
    brls::Dialog* dialog_ = nullptr;
    brls::Label* date_badge_ = nullptr;
    brls::Label* summary_label_ = nullptr;
    brls::Box* controls_box_ = nullptr;
    brls::Box* workflows_box_ = nullptr;
};

} // namespace

CurationSchedulerManager& CurationSchedulerManager::instance() {
    static CurationSchedulerManager inst;
    return inst;
}

CurationSchedulerManager::~CurationSchedulerManager() {
    stop();
}

void CurationSchedulerManager::start() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (started_) return;
        started_ = true;
        alive_->store(true, std::memory_order_release);
    }

    sched_bus_token_ = kernel.bus().subscribe(
        "entity.curation.scheduler_changed",
        "host.curation_scheduler",
        [this](const std::string&, const nlohmann::json& payload) {
            applyStatusUpdate(payload, true);
        });
    rec_bus_token_ = kernel.bus().subscribe(
        "entity.recommend.updated",
        "host.curation_scheduler",
        [this](const std::string&, const nlohmann::json&) {
            refreshStatus(false, false);
        });

    const auto alive = alive_;
    brls::delay(1800, [this, alive]() {
        if (!alive->load(std::memory_order_acquire)) return;
        refreshStatus(true, true);
    });

    bool schedule_periodic = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!periodic_scheduled_) {
            periodic_scheduled_ = true;
            schedule_periodic = true;
        }
    }
    if (schedule_periodic) {
        schedulePeriodicTick();
    }
}

void CurationSchedulerManager::schedulePeriodicTick() {
    const auto alive = alive_;
    brls::delay(600000, [this, alive]() {
        if (!alive->load(std::memory_order_acquire)) return;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!started_) {
                periodic_scheduled_ = false;
                return;
            }
        }
        refreshStatus(true, true);
        schedulePeriodicTick();
    });
}

void CurationSchedulerManager::stop() {
    alive_->store(false, std::memory_order_release);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        started_ = false;
        periodic_scheduled_ = false;
    }
    {
        std::lock_guard<std::mutex> jlock(job_mutex_);
        job_bridge_active_ = false;
    }
    job_cv_.notify_all();

    if (HostKernel::instance().isInitialized()) {
        if (sched_bus_token_ != 0) {
            HostKernel::instance().bus().unsubscribe(sched_bus_token_);
            sched_bus_token_ = 0;
        }
        if (rec_bus_token_ != 0) {
            HostKernel::instance().bus().unsubscribe(rec_bus_token_);
            rec_bus_token_ = 0;
        }
    }
}

uint64_t CurationSchedulerManager::subscribeStatus(StatusListener listener) {
    nlohmann::json snapshot;
    uint64_t id = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        id = next_listener_id_++;
        listeners_.emplace_back(id, std::move(listener));
        snapshot = latest_status_;
    }
    if (snapshot.is_object() && !snapshot.empty()) {
        std::lock_guard<std::mutex> lock(mutex_);
        for (const auto& [lid, fn] : listeners_) {
            if (lid == id && fn) {
                fn(snapshot);
                break;
            }
        }
    }
    return id;
}

void CurationSchedulerManager::unsubscribeStatus(uint64_t token) {
    std::lock_guard<std::mutex> lock(mutex_);
    listeners_.erase(
        std::remove_if(listeners_.begin(), listeners_.end(),
                       [token](const auto& pair) { return pair.first == token; }),
        listeners_.end());
}

nlohmann::json CurationSchedulerManager::latestStatus() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return latest_status_;
}

void CurationSchedulerManager::refreshStatus(bool trigger_auto_if_needed, bool sync_remote) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "daily_status"}, {"sync_remote", sync_remote}},
        [this, alive, trigger_auto_if_needed](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire) || !res.ok || !res.data.is_object()) return;
            applyStatusUpdate(res.data, false);
            if (trigger_auto_if_needed &&
                res.data.value("auto_run_enabled", false) &&
                res.data.value("needs_auto_run", false) &&
                !res.data.value("running", false)) {
                runMissingToday("auto");
            }
        });
}

void CurationSchedulerManager::runMissingToday(const std::string& trigger) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "run_daily_workflows"}, {"trigger", trigger}, {"wait", false}},
        [this, alive, trigger](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (!res.ok) {
                if (trigger != "auto") {
                    const std::string msg = res.data.value("message", res.error);
                    brls::sync([msg]() { brls::Application::notify("无法启动定时工作流: " + msg); });
                }
                return;
            }
            if (res.data.is_object()) {
                applyStatusUpdate(res.data, false);
                if (res.data.value("started", false)) {
                    const int n = static_cast<int>(res.data.value("scheduled_projects", nlohmann::json::array()).size());
                    const std::string runner = res.data.value("active_runner", "agent");
                    brls::sync([n, runner, trigger]() {
                        brls::Application::notify(
                            (trigger == "auto" ? "已同步 GitHub 并后台调用 " : "已同步 GitHub 并调用 ") +
                            runner + " 补跑 " + std::to_string(n) + " 项每日工作流");
                    });
                } else if (trigger != "auto" && res.data.value("skipped_reason", "") == "nothing_pending_today") {
                    brls::sync([]() { brls::Application::notify("已与 GitHub 同步，今日已启用工作流已全部完成"); });
                }
            }
        });
}

void CurationSchedulerManager::runSingleWorkflow(const std::string& project) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "run_daily_workflows"}, {"trigger", "manual"}, {"project", project}, {"wait", false}},
        [this, alive, project](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (!res.ok) {
                const std::string msg = res.data.value("message", res.error);
                brls::sync([msg]() { brls::Application::notify("启动失败: " + msg); });
                return;
            }
            if (res.data.is_object()) {
                applyStatusUpdate(res.data, false);
                if (res.data.value("started", false)) {
                    brls::sync([project]() {
                        brls::Application::notify("已启动后台工作流: " + project);
                    });
                }
            }
        });
}

void CurationSchedulerManager::cancelRun() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "cancel_daily_workflows"}},
        [this, alive](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok && res.data.value("was_running", false)) {
                brls::sync([]() { brls::Application::notify("已请求停止后台 Agent 工作流"); });
            }
            refreshStatus(false, false);
        });
}

void CurationSchedulerManager::syncGitHubNow() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    brls::Application::notify("正在双向同步 GitHub 双仓 (obsidian-vault & music-daily)…");
    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "sync_github"}, {"push_remote", true}, {"clone_if_missing", true}},
        [this, alive](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.data.is_object() && res.data.contains("scheduler") &&
                res.data["scheduler"].is_object()) {
                applyStatusUpdate(res.data["scheduler"], false);
            } else {
                refreshStatus(false, false);
            }
            const std::string msg = res.data.is_object()
                                        ? res.data.value("summary", res.error)
                                        : res.error;
            brls::sync([ok = res.ok, msg]() {
                brls::Application::notify(ok ? ("GitHub 同步完成: " + msg)
                                             : ("GitHub 同步提示: " + msg));
            });
        });
}

void CurationSchedulerManager::setAutoRunEnabled(bool enabled) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "configure_daily_scheduler"}, {"auto_run_enabled", enabled}},
        [this, alive, enabled](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok && res.data.is_object()) {
                applyStatusUpdate(res.data, false);
                brls::sync([enabled]() {
                    brls::Application::notify(enabled ? "已开启每日定时推荐自动补跑" : "已关闭每日定时推荐自动补跑");
                });
            }
        });
}

void CurationSchedulerManager::setGitSyncEnabled(bool enabled) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "configure_daily_scheduler"}, {"git_sync_enabled", enabled}},
        [this, alive, enabled](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok && res.data.is_object()) {
                applyStatusUpdate(res.data, false);
                brls::sync([enabled]() {
                    brls::Application::notify(enabled ? "已开启每日推荐 GitHub 自动双向同步"
                                                      : "已关闭每日推荐 GitHub 自动双向同步");
                });
            }
        });
}

void CurationSchedulerManager::setProjectEnabled(const std::string& project, bool enabled) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        {{"action", "configure_daily_scheduler"}, {"project", project}, {"enabled", enabled}},
        [this, alive, project, enabled](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok && res.data.is_object()) {
                applyStatusUpdate(res.data, false);
                brls::sync([project, enabled]() {
                    brls::Application::notify((enabled ? "已启用工作流: " : "已关闭工作流: ") + project);
                });
            }
        });
}

void CurationSchedulerManager::setAgentRunner(const std::string& runner, const std::string& custom_cmd) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

    nlohmann::json req = {
        {"action", "configure_daily_scheduler"},
        {"agent_runner", runner},
        {"_omnidesk_local_ui_token", localUiCallToken()},
    };
    if (!custom_cmd.empty()) {
        req["custom_agent_command"] = custom_cmd;
    }

    const auto alive = alive_;
    kernel.capabilities().callAsync(
        "recommend.feed.v1",
        std::move(req),
        [this, alive, runner](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok && res.data.is_object()) {
                applyStatusUpdate(res.data, false);
                brls::sync([runner]() {
                    brls::Application::notify("已切换后台 Agent 执行器: " + runner);
                });
            }
        });
}

void CurationSchedulerManager::applyStatusUpdate(const nlohmann::json& status, bool from_event) {
    if (!status.is_object()) return;

    bool just_finished = false;
    bool now_running = status.value("running", false);
    std::string finish_summary = status.value("last_run_summary", "");

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (was_running_ && !now_running) {
            just_finished = true;
        }
        was_running_ = now_running;
        latest_status_ = status;
    }

    if (now_running) {
        ensureTrackedJob(status);
    }
    job_cv_.notify_all();

    if (just_finished && from_event && !finish_summary.empty()) {
        brls::sync([finish_summary]() {
            brls::Application::notify(finish_summary);
        });
    }

    notifyListeners(status);
}

void CurationSchedulerManager::ensureTrackedJob(const nlohmann::json& status) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    {
        std::lock_guard<std::mutex> jlock(job_mutex_);
        if (job_bridge_active_) return;
        job_bridge_active_ = true;
    }

    const std::string today = status.value("business_date", "");
    JobSpec spec;
    spec.title = "每日定时推荐 · 后台 Agent";
    spec.plugin_id = "plugin-ai-curator";
    spec.kind = "curation.daily_scheduler";
    spec.open_target = {
        "curation",
        "cmd:curation:calendar",
        today,
        "查看今日推荐日历",
        {{"business_date", today}},
    };

    const auto alive = alive_;
    const std::string jid = kernel.jobs().submit(
        std::move(spec),
        [this, alive](JobHandle& job, std::string& error) -> bool {
            while (alive->load(std::memory_order_acquire)) {
                if (job.isCancelled()) {
                    if (HostKernel::instance().isInitialized() &&
                        HostKernel::instance().capabilities().has("recommend.feed.v1")) {
                        HostKernel::instance().capabilities().call(
                            "recommend.feed.v1", {{"action", "cancel_daily_workflows"}});
                    }
                    std::lock_guard<std::mutex> jlock(job_mutex_);
                    job_bridge_active_ = false;
                    return false;
                }

                nlohmann::json st = latestStatus();
                const bool running = st.value("running", false);
                const float pct = std::clamp(st.value("progress", 0.1f), 0.0f, 1.0f);
                std::string stage = st.value("stage_message", "正在执行今日定时推荐…");
                const std::string live = st.value("live_output_line", "");
                if (!live.empty()) {
                    stage += " · " + live;
                }
                job.reportProgress(pct, stage);

                if (!running) {
                    const int failed = st.value("failed_count", 0);
                    const std::string summary = st.value("last_run_summary", "今日定时推荐已完成");
                    const std::string bdate = st.value("business_date", "");
                    {
                        std::lock_guard<std::mutex> jlock(job_mutex_);
                        job_bridge_active_ = false;
                    }
                    if (failed > 0) {
                        error = summary;
                        return false;
                    }
                    job.setOpenTarget({
                        "curation",
                        "cmd:curation:calendar",
                        bdate,
                        "查看今日推荐日历",
                        {{"business_date", bdate}},
                    });
                    job.reportProgress(1.0f, summary);
                    return true;
                }

                std::unique_lock<std::mutex> jlock(job_mutex_);
                job_cv_.wait_for(jlock, std::chrono::milliseconds(500));
            }
            std::lock_guard<std::mutex> jlock(job_mutex_);
            job_bridge_active_ = false;
            return false;
        });

    std::lock_guard<std::mutex> lock(mutex_);
    active_job_id_ = jid;
}

void CurationSchedulerManager::notifyListeners(const nlohmann::json& status) {
    std::vector<StatusListener> copy;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        copy.reserve(listeners_.size());
        for (const auto& [id, fn] : listeners_) {
            (void)id;
            if (fn) copy.push_back(fn);
        }
    }
    for (const auto& fn : copy) {
        fn(status);
    }
}

void CurationSchedulerDialog::open(std::function<void()> on_open_calendar) {
    if (!brls::Application::getPlatform()) {
        return;
    }
    auto* box = new SchedulerDialogBox(std::move(on_open_calendar));
    auto* dialog = new brls::Dialog(box);
    box->setDialog(dialog);
    if (auto* applet = dynamic_cast<brls::Box*>(dialog->getView("brls/dialog/applet"))) {
        applet->setWidth(860.0f);
        applet->setCornerRadius(14.0f);
        applet->setBorderThickness(1.0f);
        applet->setBorderColor(ui::color("desktop/line"));
        applet->setBackgroundColor(ui::color("brls/background"));
        applet->setClipsToBounds(true);
    }
    dialog->addButton("关闭 Esc", []() {});
    if (auto* btn1 = dialog->getView("brls/dialog/button1")) {
        if (auto* btn_row = btn1->getParent()) {
            btn_row->setHeight(52.0f);
        }
    }
    dialog->open();
}

} // namespace omnidesk::host
