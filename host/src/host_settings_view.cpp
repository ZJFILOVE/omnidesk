#include "omnidesk/host/host_settings_view.hpp"

#include "omnidesk/host/auth_center_dialog.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/curation_scheduler_dialog.hpp"
#include "omnidesk/host/desktop_ui.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {

HostSettingsView::HostSettingsView(std::function<void()> on_theme_toggled,
                                   std::function<void()> on_open_store,
                                   std::function<void()> on_open_auth_center)
    : brls::Box(brls::Axis::COLUMN),
      on_theme_toggled_(std::move(on_theme_toggled)),
      on_open_store_(std::move(on_open_store)),
      on_open_auth_center_(std::move(on_open_auth_center)) {
    setGrow(1.0f);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    auto* title = ui::makeLabel("宿主与系统设置 · Host Kernel Settings", 24.0f, "brls/text");
    title->setMarginBottom(14);
    addView(title);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setGrow(1.0f);
    scroll->setWidthPercentage(100);
    body_ = new brls::Box(brls::Axis::COLUMN);
    body_->setWidthPercentage(100);
    scroll->setContentView(body_);
    addView(scroll);

    auth_sub_token_ = AuthCenterManager::instance().subscribeChanges([this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire) && getVisibility() == brls::Visibility::VISIBLE) {
                refresh();
            }
        });
    });

    refresh();
}

HostSettingsView::~HostSettingsView() {
    alive_->store(false, std::memory_order_release);
    if (auth_sub_token_ != 0) {
        AuthCenterManager::instance().unsubscribeChanges(auth_sub_token_);
        auth_sub_token_ = 0;
    }
    if (catalog_request_) {
        catalog_request_->cancel();
    }
}

void HostSettingsView::refresh() {
    if (!body_ || !alive_->load(std::memory_order_acquire)) return;
    if (brls::Application::getActivitiesStack().size() > 1) {
        if (refresh_pending_) return;
        refresh_pending_ = true;
        brls::delay(300, [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            refresh_pending_ = false;
            refresh();
        });
        return;
    }
    for (brls::View* cur = brls::Application::getCurrentFocus(); cur; cur = cur->getParent()) {
        if (cur == body_) {
            brls::Application::giveFocus(nullptr);
            break;
        }
    }
    body_->clearViews();
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    // 0. Unified Account Auth & Automatic Browser Cookie Center
    auto* auth_card = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    auth_card->setMarginBottom(14);
    auth_card->addView(ui::makeLabel("统一账号登录授权与浏览器自动 Cookie 中心", 18.5f, "brls/text"));

    const auto auth_reports = AuthCenterManager::instance().inspectAll();
    const auto auth_counts = AuthCenterManager::instance().summarizeCounts(auth_reports);
    std::string auth_summary =
        "需鉴权模块共 " + std::to_string(auth_counts.total_auth_plugins) + " 项  ·  ✓ 已登录授权 " +
        std::to_string(auth_counts.authenticated_count) + " 项";
    if (auth_counts.guest_cookie_warning_count > 0) {
        auth_summary += "  ·  ⚠ 仅匿名访客 Cookie (假登录) " +
                        std::to_string(auth_counts.guest_cookie_warning_count) + " 项";
    }
    if (auth_counts.missing_required_count > 0) {
        auth_summary += "  ·  ✕ 待配置 " + std::to_string(auth_counts.missing_required_count) + " 项";
    }
    auth_summary +=
        "\n集中查看全平台真实登录状态（告别匿名 Cookie 假绿灯），对所有 Cookie 类站点支持一键弹出 Chrome 登录并自动提取保存 Cookie（含 HttpOnly）。";

    auto* auth_desc = ui::makeLabel(
        auth_summary,
        15.0f,
        auth_counts.guest_cookie_warning_count > 0 ? "status/warn_fg" : "font/grey",
        1.55f);
    auth_desc->setMarginTop(8);
    auth_desc->setMarginBottom(12);
    auth_card->addView(auth_desc);

    auto* auth_btn_row = new brls::Box(brls::Axis::ROW);
    auto* open_auth_btn = ui::makeButton(
        "打开统一授权与自动获取 Cookie 中心 ▸",
        [this]() {
            if (on_open_auth_center_) {
                on_open_auth_center_();
            } else {
                AuthCenterDialog::open();
            }
        },
        true, 38.0f);
    open_auth_btn->setMarginRight(10);
    auth_btn_row->addView(open_auth_btn);

    auth_btn_row->addView(ui::makeButton(
        "前往插件市场参数设置",
        [this]() {
            if (on_open_store_) on_open_store_();
        },
        false, 38.0f));
    auth_card->addView(auth_btn_row);
    body_->addView(auth_card);

    // 1. Appearance card
    auto* theme_card = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    theme_card->setMarginBottom(14);
    theme_card->addView(ui::makeLabel("视觉设计系统 (Paper Light / Graphite Dark)", 18.5f, "brls/text"));
    auto* t_desc = ui::makeLabel(
        std::string("当前主题: ") +
            (kernel.isDarkTheme() ? "Graphite 深色石墨 (#181718)" : "Paper 暖调纸感 (#F6F5F3)") +
            " · 按 `t` 瞬时切换主题 · `Ctrl+1..4` 核心台 · `Alt+<字母>` 插件工作区 · `Ctrl+,` 系统设置",
        15.0f, "font/grey", 1.5f);
    t_desc->setMarginTop(8);
    t_desc->setMarginBottom(12);
    theme_card->addView(t_desc);

    auto* btn_row = new brls::Box(brls::Axis::ROW);
    auto* toggle = ui::makeButton(kernel.isDarkTheme() ? "切换至 Paper 亮色" : "切换至 Graphite 暗色",
                                  [this, alive = alive_]() {
                                      if (!alive->load(std::memory_order_acquire)) return;
                                      if (on_theme_toggled_) on_theme_toggled_();
                                      brls::sync([this, alive]() {
                                          if (alive->load(std::memory_order_acquire)) refresh();
                                      });
                                  },
                                  true, 38.0f);
    toggle->setMarginRight(10);
    btn_row->addView(toggle);
    btn_row->addView(ui::makeButton("查看全键盘快捷键 (?)", []() {
        DesktopInputController::showShortcutsHelp();
    }, false, 38.0f));
    theme_card->addView(btn_row);
    body_->addView(theme_card);

    // 2. AI Routing Bridge status
    auto* ai_card = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    ai_card->setMarginBottom(14);
    ai_card->addView(ui::makeLabel("AI Agent 路由与 Magpie 反向控制接口", 18.5f, "brls/text"));
    const bool has_router = kernel.capabilities().has("llm.router.v1");
    auto* ai_desc = ui::makeLabel(
        has_router
            ? "已检测到 `llm.router.v1` 能力提供方，用户选择的 Harness / 模型路由将同步写回底层路由器。"
            : "尚未接入 AI 路由插件。主核已预设 `ai.agent.v1` / `llm.router.v1` / `llm.chat.v1` 标准协议，后续从插件市场安装 `plugin-magpie-agent` 即可无缝接入本地 magpie 网关与路由控制。",
        15.0f, has_router ? "status/ok_fg" : "font/grey", 1.55f);
    ai_desc->setMarginTop(8);
    ai_desc->setMarginBottom(12);
    ai_card->addView(ai_desc);
    ai_card->addView(ui::makeButton("前往插件市场管理 AI 插件", [this]() {
        if (on_open_store_) on_open_store_();
    }, false, 38.0f));
    body_->addView(ai_card);

    // 3. Daily Curation Scheduler & Calendar
    auto* sched_card = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    sched_card->setMarginBottom(14);
    sched_card->addView(ui::makeLabel("每日定时推荐 · 自动检测与后台 Agent 调度", 18.5f, "brls/text"));
    const auto sched_st = CurationSchedulerManager::instance().latestStatus();
    const bool auto_on = sched_st.is_object() ? sched_st.value("auto_run_enabled", true) : true;
    const std::string sched_summary = sched_st.is_object()
        ? sched_st.value(
              "summary_text",
              "自动检测当天（Asia/Shanghai）4 个定时推荐工作流是否已运行，未运行则在 App 内后台调用本地 Agent 补跑。")
        : std::string("自动检测当天（Asia/Shanghai）4 个定时推荐工作流是否已运行，未运行则在 App 内后台调用本地 Agent 补跑。");
    auto* sched_desc = ui::makeLabel(sched_summary, 15.0f, "font/grey", 1.55f);
    sched_desc->setMarginTop(8);
    sched_desc->setMarginBottom(12);
    sched_card->addView(sched_desc);

    auto* sched_row = new brls::Box(brls::Axis::ROW);
    auto* auto_btn = ui::makeButton(
        auto_on ? "自动补跑开关: 已开启 ✓" : "自动补跑开关: 已关闭",
        [this, auto_on]() {
            CurationSchedulerManager::instance().setAutoRunEnabled(!auto_on);
            brls::delay(200, [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire)) refresh();
            });
        },
        auto_on, 38.0f);
    auto_btn->setMarginRight(10);
    sched_row->addView(auto_btn);

    auto* open_sched_btn = ui::makeButton("定时开关与运行面板", []() {
        CurationSchedulerDialog::open([]() { CurationCalendarDialog::open(); });
    }, false, 38.0f);
    open_sched_btn->setMarginRight(10);
    sched_row->addView(open_sched_btn);

    sched_row->addView(ui::makeButton("推荐日历", []() {
        CurationCalendarDialog::open();
    }, false, 38.0f));
    sched_card->addView(sched_row);
    body_->addView(sched_card);

    // 4. Paths & Marketplace Lockfile
    auto* paths_card = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    paths_card->addView(ui::makeLabel("目录与锁定状态 (XDG Paths & SDK)", 18.5f, "brls/text"));
    std::string pinfo =
        "Data Root:      " + kernel.dataRoot() + "\n"
        "Config Root:    " + kernel.configRoot() + "\n"
        "SDK CMake Dir:  " + (kernel.sdkCmakeDir().empty() ? "<unset>" : kernel.sdkCmakeDir()) + "\n"
        "Lockfile:       " + kernel.installer().lockFilePath() + "\n"
        "Catalog Source: " + (kernel.catalogPath().empty() ? "<none>" : kernel.catalogPath());
    auto* pl = ui::makeLabel(pinfo, 14.5f, "font/grey", 1.60f);
    pl->setMarginTop(8);
    paths_card->addView(pl);

    auto* add_cat = ui::makeButton("导入远程第三方插件市场目录 JSON URL", [this]() {
        if (auto* plat = brls::Application::getPlatform()) {
            plat->getImeManager()->openForText(
                [this, alive = alive_](std::string url) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (url.empty()) return;
                    std::string err;
                    if (HostKernel::instance().catalog().mergeRemoteUrl(url, err)) {
                        brls::Application::notify("已合并第三方目录");
                    } else {
                        brls::Application::notify("拉取失败: " + err);
                    }
                    brls::sync([this, alive]() {
                        if (alive->load(std::memory_order_acquire)) refresh();
                    });
                },
                "添加远程 Marketplace Catalog JSON",
                "输入 HTTPS JSON 索引 URL，将动态并入插件市场目录",
                256,
                "https://");
        }
    }, false, 38.0f);
    add_cat->setMarginTop(12);
    paths_card->addView(add_cat);
    body_->addView(paths_card);
}

} // namespace omnidesk::host
