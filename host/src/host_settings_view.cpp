#include "omnidesk/host/host_settings_view.hpp"

#include "omnidesk/host/desktop_ui.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {

HostSettingsView::HostSettingsView(std::function<void()> on_theme_toggled,
                                   std::function<void()> on_open_store)
    : brls::Box(brls::Axis::COLUMN),
      on_theme_toggled_(std::move(on_theme_toggled)),
      on_open_store_(std::move(on_open_store)) {
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

    refresh();
}

void HostSettingsView::refresh() {
    if (!body_) return;
    body_->clearViews();
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    // 1. Appearance card
    auto* theme_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    theme_card->setMarginBottom(12);
    theme_card->addView(ui::makeLabel("视觉设计系统 (Paper Light / Graphite Dark)", 18.0f, "brls/text"));
    auto* t_desc = ui::makeLabel(
        std::string("当前主题: ") +
            (kernel.isDarkTheme() ? "Graphite 深色石墨 (#181718)" : "Paper 暖调纸感 (#F6F5F3)") +
            " · 支持随时按键盘 `t` 瞬时切换",
        14.5f, "font/grey");
    t_desc->setMarginTop(6);
    t_desc->setMarginBottom(10);
    theme_card->addView(t_desc);

    auto* btn_row = new brls::Box(brls::Axis::ROW);
    auto* toggle = ui::makeButton(kernel.isDarkTheme() ? "切换至 Paper 亮色" : "切换至 Graphite 暗色",
                                  [this]() {
                                      if (on_theme_toggled_) on_theme_toggled_();
                                      brls::sync([this]() { refresh(); });
                                  },
                                  true, 36.0f);
    toggle->setMarginRight(8);
    btn_row->addView(toggle);
    btn_row->addView(ui::makeButton("查看键盘快捷键 (?)", []() {
        DesktopInputController::showShortcutsHelp();
    }, false, 36.0f));
    theme_card->addView(btn_row);
    body_->addView(theme_card);

    // 2. AI Routing Bridge status
    auto* ai_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    ai_card->setMarginBottom(12);
    ai_card->addView(ui::makeLabel("AI Agent 路由与 Magpie 反向控制接口", 18.0f, "brls/text"));
    const bool has_router = kernel.capabilities().has("llm.router.v1");
    auto* ai_desc = ui::makeLabel(
        has_router
            ? "已检测到 `llm.router.v1` 能力提供方，用户选择的 Harness / 模型路由将同步写回底层路由器。"
            : "尚未接入 AI 路由插件。主核已预设 `ai.agent.v1` / `llm.router.v1` / `llm.chat.v1` 标准协议，后续从插件市场安装 `plugin-magpie-agent` 即可无缝接入本地 magpie 网关与路由控制。",
        14.5f, has_router ? "status/ok_fg" : "font/grey", 1.5f);
    ai_desc->setMarginTop(6);
    ai_desc->setMarginBottom(10);
    ai_card->addView(ai_desc);
    ai_card->addView(ui::makeButton("前往插件市场管理 AI 插件", [this]() {
        if (on_open_store_) on_open_store_();
    }, false, 36.0f));
    body_->addView(ai_card);

    // 3. Paths & Marketplace Lockfile
    auto* paths_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    paths_card->addView(ui::makeLabel("目录与锁定状态 (XDG Paths & SDK)", 18.0f, "brls/text"));
    std::string pinfo =
        "Data Root:      " + kernel.dataRoot() + "\n"
        "Config Root:    " + kernel.configRoot() + "\n"
        "SDK CMake Dir:  " + (kernel.sdkCmakeDir().empty() ? "<unset>" : kernel.sdkCmakeDir()) + "\n"
        "Lockfile:       " + kernel.installer().lockFilePath() + "\n"
        "Catalog Source: " + (kernel.catalogPath().empty() ? "<none>" : kernel.catalogPath());
    auto* pl = ui::makeLabel(pinfo, 14.0f, "font/grey", 1.55f);
    pl->setMarginTop(8);
    paths_card->addView(pl);

    auto* add_cat = ui::makeButton("导入远程第三方插件市场目录 JSON URL", [this]() {
        if (auto* plat = brls::Application::getPlatform()) {
            plat->getImeManager()->openForText(
                [this](std::string url) {
                    if (url.empty()) return;
                    std::string err;
                    if (HostKernel::instance().catalog().mergeRemoteUrl(url, err)) {
                        brls::Application::notify("已合并第三方目录");
                    } else {
                        brls::Application::notify("拉取失败: " + err);
                    }
                    brls::sync([this]() { refresh(); });
                },
                "添加远程 Marketplace Catalog JSON",
                "输入 HTTPS JSON 索引 URL，将动态并入插件市场目录",
                256,
                "https://");
        }
    }, false, 36.0f);
    add_cat->setMarginTop(12);
    paths_card->addView(add_cat);
    body_->addView(paths_card);
}

} // namespace omnidesk::host
