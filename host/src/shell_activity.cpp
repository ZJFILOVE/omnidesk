#include "omnidesk/host/shell_activity.hpp"

#include <algorithm>
#include <unordered_set>
#include "omnidesk/host/ai_composer.hpp"
#include "omnidesk/host/ai_workbench.hpp"
#include "omnidesk/host/auth_center_dialog.hpp"
#include "omnidesk/host/book_workbench.hpp"
#include "omnidesk/host/companion_dock.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/curation_scheduler_dialog.hpp"
#include "omnidesk/host/desktop_ui.hpp"
#include "omnidesk/host/host_settings_view.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/media_workbench.hpp"
#include "omnidesk/host/mini_player_bar.hpp"
#include "omnidesk/host/nav_shortcuts.hpp"
#include "omnidesk/host/omni_command_palette.hpp"
#include "omnidesk/host/plugin_store_view.hpp"
#include "omnidesk/host/svg_view.hpp"
#include "omnidesk/host/text_workbench.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/mpv_core.hpp"
#include "omnidesk/playback_coordinator.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

bool focusIsInside(brls::View* ancestor) {
    if (!ancestor) return false;
    brls::View* focus = brls::Application::getCurrentFocus();
    while (focus) {
        if (focus == ancestor) return true;
        focus = focus->getParent();
    }
    return false;
}

brls::Box* makeKeycapBadge(const std::string& text,
                           const NVGcolor& bg,
                           const NVGcolor& border,
                           const NVGcolor& fg,
                           brls::Label** out_lbl = nullptr) {
    auto* kbd = new brls::Box(brls::Axis::ROW);
    kbd->setHeight(20.0f);
    kbd->setPadding(0, 6, 0, 6);
    kbd->setCornerRadius(5.0f);
    kbd->setShrink(0.0f);
    kbd->setAlignItems(brls::AlignItems::CENTER);
    kbd->setJustifyContent(brls::JustifyContent::CENTER);
    kbd->setBackgroundColor(bg);
    kbd->setBorderThickness(1.0f);
    kbd->setBorderColor(border);
    auto* lbl = ui::makeLabel(text, 11.2f, "font/grey", 1.0f);
    lbl->setSingleLine(true);
    lbl->setTextColor(fg);
    kbd->addView(lbl);
    if (out_lbl) *out_lbl = lbl;
    return kbd;
}

} // namespace

WorkspaceShellActivity::WorkspaceShellActivity(WorkspaceId initial_workspace)
    : active_workspace_(initial_workspace) {}

WorkspaceShellActivity::~WorkspaceShellActivity() {
    alive_->store(false, std::memory_order_release);
    if (auth_ui_sub_token_ != 0) {
        AuthCenterManager::instance().unsubscribeChanges(auth_ui_sub_token_);
        auth_ui_sub_token_ = 0;
    }
    AuthCenterManager::instance().stop();
    if (sched_ui_sub_token_ != 0) {
        CurationSchedulerManager::instance().unsubscribeStatus(sched_ui_sub_token_);
        sched_ui_sub_token_ = 0;
    }
    CurationSchedulerManager::instance().stop();
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        if (dom_sub_token_ != 0) kernel.domains().unsubscribeChanges(dom_sub_token_);
        if (bus_sub_token_ != 0) kernel.bus().unsubscribe(bus_sub_token_);
        if (job_sub_token_ != 0) kernel.jobs().unsubscribe(job_sub_token_);
        for (uint64_t tok : job_open_sub_tokens_) {
            if (tok != 0) kernel.bus().unsubscribe(tok);
        }
        for (uint64_t tok : fault_sub_tokens_) {
            if (tok != 0) kernel.bus().unsubscribe(tok);
        }
    }
    job_open_sub_tokens_.clear();
    fault_sub_tokens_.clear();
    brls::Application::getWindowSizeChangedEvent()->unsubscribe(resize_sub_);
}

brls::View* WorkspaceShellActivity::createContentView() {
    root_box_ = new brls::Box(brls::Axis::ROW);
    root_box_->setWidthPercentage(100);
    root_box_->setHeightPercentage(100);
    populateRootBox();
    brls::sync([]() {
        MPVCore::instance().ensureInitialized();
    });
    return root_box_;
}

void WorkspaceShellActivity::populateRootBox() {
    root_box_->setBackgroundColor(ui::color("brls/background"));

    // Left Navigation Rail
    rail_box_ = new brls::Box(brls::Axis::COLUMN);
    rail_box_->setId("omnidesk/pane_rail");
    rail_box_->setWidth(258);
    rail_box_->setShrink(0.0f);
    rail_box_->setHeightPercentage(100);
    rail_box_->setPadding(14, 10, 12, 10);
    rail_box_->setBackgroundColor(ui::color("desktop/surface"));
    rail_box_->setBorderThickness(1);
    rail_box_->setBorderColor(ui::color("desktop/line"));

    // Brand Header (pinned at top)
    auto* brand = new brls::Box(brls::Axis::ROW);
    brand->setShrink(0.0f);
    brand->setPadding(0, 4, 0, 4);
    brand->setAlignItems(brls::AlignItems::CENTER);
    brand->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    brand->setMarginBottom(8);

    auto* brand_left = new brls::Box(brls::Axis::ROW);
    brand_left->setAlignItems(brls::AlignItems::CENTER);
    auto* logo = new SvgIconView(HostKernel::instance().assetPath("svg/omnidesk_logo.svg"));
    logo->setDimensions(26, 26);
    logo->setMarginRight(9);
    brand_left->addView(logo);
    brand_left->addView(ui::makeLabel("OmniDesk", 18.0f, "brls/text"));
    brand->addView(brand_left);

    auto* ver_lbl = ui::makeLabel("Workbench", 11.5f, "font/grey");
    brand->addView(ver_lbl);
    rail_box_->addView(brand);

    // Quick Search & Command Trigger Bar (Ctrl+K)
    auto* search_bar = new brls::Box(brls::Axis::ROW);
    search_bar->setShrink(0.0f);
    search_bar->setWidthPercentage(100);
    search_bar->setHeight(33.0f);
    search_bar->setPadding(0, 8, 0, 10);
    search_bar->setMarginBottom(4);
    search_bar->setCornerRadius(8.0f);
    search_bar->setHighlightCornerRadius(8.0f);
    search_bar->setBackgroundColor(ui::color("desktop/soft"));
    search_bar->setBorderThickness(1.0f);
    search_bar->setBorderColor(ui::color("desktop/line"));
    search_bar->setFocusable(true);
    search_bar->setAlignItems(brls::AlignItems::CENTER);
    search_bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

    auto* sb_left = new brls::Box(brls::Axis::ROW);
    sb_left->setAlignItems(brls::AlignItems::CENTER);
    sb_left->setShrink(1.0f);
    auto* sb_icon = new SvgIconView();
    sb_icon->setSvgString(buildNavIconSvg("search", nvgColorToHex(ui::color("font/grey"))));
    sb_icon->setDimensions(14, 14);
    sb_icon->setShrink(0.0f);
    sb_icon->setMarginRight(8);
    sb_left->addView(sb_icon);
    auto* sb_lbl = ui::makeLabel("全局检索与命令...", 12.8f, "font/grey");
    sb_lbl->setSingleLine(true);
    sb_lbl->setShrink(1.0f);
    sb_left->addView(sb_lbl);
    search_bar->addView(sb_left);

    search_bar->addView(makeKeycapBadge(
        "Ctrl+K", ui::color("desktop/surface"), ui::color("desktop/line"), ui::color("font/grey")));
    search_bar->registerClickAction([this, alive = alive_](brls::View*) {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                openCommandPalette();
            }
        });
        return true;
    });
    rail_box_->addView(search_bar);

    auto* top_div = ui::makeDivider(5.0f);
    top_div->setShrink(0.0f);
    rail_box_->addView(top_div);

    // Scrollable workspace navigation (prevents overlap in tiled Hyprland windows)
    auto* nav_scroll = new brls::ScrollingFrame();
    nav_scroll->setGrow(1.0f);
    nav_scroll->setShrink(1.0f);
    nav_scroll->setWidthPercentage(100);
    nav_scroll->setMarginTop(2);
    nav_scroll->setMarginBottom(4);

    auto* nav_scroll_content = new brls::Box(brls::Axis::COLUMN);
    nav_scroll_content->setWidthPercentage(100);

    built_in_nav_box_ = new brls::Box(brls::Axis::COLUMN);
    built_in_nav_box_->setShrink(0.0f);
    built_in_nav_box_->setWidthPercentage(100);
    nav_scroll_content->addView(built_in_nav_box_);

    custom_nav_box_ = new brls::Box(brls::Axis::COLUMN);
    custom_nav_box_->setShrink(0.0f);
    custom_nav_box_->setWidthPercentage(100);
    custom_nav_box_->setMarginTop(4);
    nav_scroll_content->addView(custom_nav_box_);

    nav_scroll->setContentView(nav_scroll_content);
    rail_box_->addView(nav_scroll);

    // Bottom Compact Status & Utilities (pinned at bottom, ultra-clean footprint)
    auto* bottom_group = new brls::Box(brls::Axis::COLUMN);
    bottom_group->setShrink(0.0f);
    bottom_group->setWidthPercentage(100);
    auto* btm_div = ui::makeDivider(5.0f);
    btm_div->setShrink(0.0f);
    bottom_group->addView(btm_div);

    auto make_status_row = [](brls::Label** out_lbl, const char* init_txt, std::function<void()> on_tap) {
        auto* row = new brls::Box(brls::Axis::ROW);
        row->setShrink(0.0f);
        row->setWidthPercentage(100);
        row->setHeight(26.0f);
        row->setPadding(0, 9, 0, 9);
        row->setMarginBottom(4);
        row->setCornerRadius(6.5f);
        row->setHighlightCornerRadius(6.5f);
        row->setBackgroundColor(ui::color("desktop/soft"));
        row->setBorderThickness(1.0f);
        row->setBorderColor(ui::color("desktop/line"));
        row->setAlignItems(brls::AlignItems::CENTER);
        row->setFocusable(true);
        auto* lbl = ui::makeLabel(init_txt, 12.0f, "font/grey");
        lbl->setSingleLine(true);
        lbl->setShrink(1.0f);
        row->addView(lbl);
        if (out_lbl) *out_lbl = lbl;
        row->registerClickAction([fn = std::move(on_tap)](brls::View*) {
            if (fn) fn();
            return true;
        });
        return row;
    };

    bottom_group->addView(make_status_row(&ai_route_pill_, "AI 路由: 待接入插件", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                selectWorkspace(WorkspaceId::AiChat);
            }
        });
    }));

    bottom_group->addView(make_status_row(&auth_status_pill_, "账号授权: 检测中…", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                openAuthCenter();
            }
        });
    }));

    bottom_group->addView(make_status_row(&curation_status_pill_, "定时推荐: 检测中…", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            CurationSchedulerDialog::open([this, alive]() {
                if (alive->load(std::memory_order_acquire)) {
                    CurationCalendarDialog::open();
                }
            });
        });
    }));

    auto* util_row1 = new brls::Box(brls::Axis::ROW);
    util_row1->setShrink(0.0f);
    util_row1->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    util_row1->setMarginBottom(4);

    auto* cal_btn = ui::makeButton("推荐日历", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            CurationCalendarDialog::open({
                .on_open_scheduler = [this, alive]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    CurationSchedulerDialog::open([this, alive]() {
                        if (alive->load(std::memory_order_acquire)) {
                            CurationCalendarDialog::open();
                        }
                    });
                },
            });
        });
    }, false, 30.0f);
    cal_btn->setGrow(1.0f);
    cal_btn->setMarginRight(5);
    util_row1->addView(cal_btn);

    auto* dock_btn = ui::makeButton("F8 伴读", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            toggleCompanionDock();
        }
    }, false, 30.0f);
    dock_btn->setGrow(1.0f);
    util_row1->addView(dock_btn);
    bottom_group->addView(util_row1);

    auto* util_row2 = new brls::Box(brls::Axis::ROW);
    util_row2->setShrink(0.0f);
    util_row2->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

    auto* theme_btn = ui::makeButton("主题 · t", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            toggleTheme();
        }
    }, false, 30.0f);
    theme_btn->setGrow(1.0f);
    theme_btn->setMarginRight(5);
    util_row2->addView(theme_btn);

    auto* settings_btn = ui::makeButton("设置", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                selectWorkspace(WorkspaceId::Settings);
            }
        });
    }, false, 30.0f);
    settings_btn->setGrow(1.0f);
    settings_btn->setMarginRight(5);
    util_row2->addView(settings_btn);

    auto* help_btn = ui::makeButton("?", [alive = alive_]() {
        brls::sync([alive]() {
            if (alive->load(std::memory_order_acquire)) {
                DesktopInputController::showShortcutsHelp();
            }
        });
    }, false, 30.0f);
    help_btn->setShrink(0.0f);
    util_row2->addView(help_btn);
    bottom_group->addView(util_row2);

    rail_box_->addView(bottom_group);

    root_box_->addView(rail_box_);

    // Main Split (Workspace Stage + Responsive F8 Companion Dock)
    main_split_ = new brls::Box(brls::Axis::ROW);
    main_split_->setGrow(1.0f);
    main_split_->setShrink(1.0f);
    main_split_->setMinWidth(0);
    main_split_->setMinHeight(0);
    main_split_->setHeightPercentage(100);

    stage_container_ = new brls::Box(brls::Axis::COLUMN);
    stage_container_->setGrow(1.0f);
    stage_container_->setShrink(1.0f);
    stage_container_->setMinWidth(0);
    stage_container_->setMinHeight(0);
    stage_container_->setHeightPercentage(100);

    views_host_ = new brls::Box(brls::Axis::COLUMN);
    views_host_->setId("omnidesk/views_host");
    views_host_->setGrow(1.0f);
    views_host_->setShrink(1.0f);
    views_host_->setMinWidth(0);
    views_host_->setMinHeight(0);
    views_host_->setWidthPercentage(100);
    views_host_->setClipsToBounds(true);

    job_bar_box_ = new brls::Box(brls::Axis::ROW);
    job_bar_box_->setWidthPercentage(100);
    job_bar_box_->setPadding(8, 18, 8, 18);
    job_bar_box_->setBackgroundColor(ui::color("desktop/surface"));
    job_bar_box_->setAlignItems(brls::AlignItems::CENTER);
    job_bar_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    job_bar_box_->setVisibility(brls::Visibility::GONE);

    fault_banner_box_ = new brls::Box(brls::Axis::ROW);
    fault_banner_box_->setId("omnidesk/pane_banner");
    fault_banner_box_->setBackgroundColor(ui::color("status/warn_bg"));
    fault_banner_box_->setCornerRadius(10.0f);
    fault_banner_box_->setPadding(6.0f, 12.0f, 6.0f, 12.0f);
    fault_banner_box_->setMargins(4.0f, 12.0f, 4.0f, 12.0f);
    fault_banner_box_->setAlignItems(brls::AlignItems::CENTER);
    fault_banner_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    fault_banner_box_->setVisibility(brls::Visibility::GONE);

    fault_banner_label_ = ui::makeLabel("", 14.5f, "status/warn_fg", 1.25f);
    fault_banner_label_->setSingleLine(true);
    fault_banner_label_->setGrow(1.0f);
    fault_banner_label_->setMarginRight(10.0f);
    fault_banner_box_->addView(fault_banner_label_);

    auto* banner_actions = new brls::Box(brls::Axis::ROW);
    banner_actions->setAlignItems(brls::AlignItems::CENTER);
    auto* go_store_btn = ui::makeButton("前往插件市场", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            selectWorkspace(WorkspaceId::Store);
        }
    }, false, 32.0f);
    go_store_btn->setMarginRight(6.0f);
    banner_actions->addView(go_store_btn);
    banner_actions->addView(ui::makeButton("×", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            dismissWarningBanner();
        }
    }, false, 32.0f));
    fault_banner_box_->addView(banner_actions);

    stage_container_->addView(views_host_);

    chrome_bottom_box_ = new brls::Box(brls::Axis::COLUMN);
    chrome_bottom_box_->setShrink(0.0f);
    chrome_bottom_box_->setWidthPercentage(100);
    chrome_bottom_box_->addView(fault_banner_box_);

    mini_player_bar_ = new MiniPlayerBar([this, alive = alive_](PlaybackRole role) {
        brls::sync([this, alive, role]() {
            if (!alive->load(std::memory_order_acquire)) return;
            selectWorkspace(WorkspaceId::Media);
            if (media_view_) {
                media_view_->focusPlaybackStage(role);
            }
        });
    });
    mini_player_bar_->setId("omnidesk/pane_player");
    chrome_bottom_box_->addView(mini_player_bar_);
    chrome_bottom_box_->addView(job_bar_box_);
    stage_container_->addView(chrome_bottom_box_);

    main_split_->addView(stage_container_);

    root_box_->addView(main_split_);
    refreshJobBar();
}

void WorkspaceShellActivity::setCompanionDomainContext(ContentDomain domain, const std::string& summary) {
    pending_dock_domain_ = domain;
    pending_dock_summary_ = summary;
    if (dock_view_) {
        dock_view_->setDomainContext(domain, summary);
    }
}

void WorkspaceShellActivity::setExternalAiContext(ContentDomain domain, const std::string& summary) {
    pending_ai_ext_domain_ = domain;
    pending_ai_ext_summary_ = summary;
    has_pending_ai_ext_ = true;
    if (ai_view_) {
        ai_view_->setExternalContext(domain, summary);
    }
}

brls::View* WorkspaceShellActivity::ensureWorkspaceCreated(WorkspaceId id) {
    if (!views_host_) return nullptr;
    switch (id) {
        case WorkspaceId::Store: {
            if (!store_view_) {
                store_view_ = new PluginStoreView(
                    [this, alive = alive_](const std::string& tab_id) {
                        brls::sync([this, alive, tab_id]() {
                            if (!alive->load(std::memory_order_acquire) || tab_id.empty()) return;
                            return_workspace_ = WorkspaceId::Store;
                            return_custom_tab_id_.clear();
                            has_return_workspace_ = true;
                            selectWorkspace(WorkspaceId::CustomPluginTab, tab_id);
                        });
                    },
                    [this, alive = alive_](ContentDomain domain) {
                        brls::sync([this, alive, domain]() {
                            if (!alive->load(std::memory_order_acquire)) return;
                            switch (domain) {
                                case ContentDomain::Media:
                                    selectWorkspace(WorkspaceId::Media);
                                    break;
                                case ContentDomain::Text:
                                    selectWorkspace(WorkspaceId::Text);
                                    break;
                                case ContentDomain::Book:
                                    selectWorkspace(WorkspaceId::Book);
                                    break;
                                case ContentDomain::Core:
                                    selectWorkspace(WorkspaceId::AiChat);
                                    break;
                            }
                        });
                    },
                    [this, alive = alive_](const std::string& plugin_id) {
                        brls::sync([this, alive, plugin_id]() {
                            if (alive->load(std::memory_order_acquire)) {
                                openAuthCenter(plugin_id);
                            }
                        });
                    });
                store_dirty_ = false;
                views_host_->addView(store_view_);
                DesktopInputController::markDirty(store_view_);
            }
            return store_view_;
        }
        case WorkspaceId::Media: {
            if (!media_view_) {
                media_view_ = new MediaWorkbench(
                    [this](const std::string& summary) {
                        setCompanionDomainContext(ContentDomain::Media, summary);
                        setExternalAiContext(ContentDomain::Media, summary);
                    },
                    [this]() { selectWorkspace(WorkspaceId::Store); });
                media_view_->setFullscreenToggleCallback([this, alive = alive_]() {
                    brls::sync([this, alive]() {
                        if (alive->load(std::memory_order_acquire)) {
                            toggleImmersiveFullscreen();
                        }
                    });
                });
                media_dirty_ = false;
                views_host_->addView(media_view_);
                if (!pending_all_date_filter_.empty()) {
                    media_view_->applyDateFilter(pending_all_date_filter_, "all");
                }
                DesktopInputController::markDirty(media_view_);
            }
            return media_view_;
        }
        case WorkspaceId::Text: {
            if (!text_view_) {
                text_view_ = new TextWorkbench(
                    [this](const std::string& summary) {
                        setCompanionDomainContext(ContentDomain::Text, summary);
                        setExternalAiContext(ContentDomain::Text, summary);
                    },
                    [this]() { selectWorkspace(WorkspaceId::Store); });
                text_dirty_ = false;
                views_host_->addView(text_view_);
                if (!pending_all_date_filter_.empty()) {
                    text_view_->applyDateFilter(pending_all_date_filter_, "all");
                }
                DesktopInputController::markDirty(text_view_);
            }
            return text_view_;
        }
        case WorkspaceId::Book: {
            if (!book_view_) {
                book_view_ = new BookWorkbench(
                    [this](const std::string& summary) {
                        setCompanionDomainContext(ContentDomain::Book, summary);
                        setExternalAiContext(ContentDomain::Book, summary);
                    },
                    [this]() { selectWorkspace(WorkspaceId::Store); });
                book_dirty_ = false;
                views_host_->addView(book_view_);
                if (!pending_all_date_filter_.empty()) {
                    book_view_->applyDateFilter(pending_all_date_filter_);
                }
                DesktopInputController::markDirty(book_view_);
            }
            return book_view_;
        }
        case WorkspaceId::AiChat: {
            if (!ai_view_) {
                ai_view_ = new AiWorkbench(
                    [this]() { selectWorkspace(WorkspaceId::Store); },
                    [this](const std::string& session_id) {
                        const WorkspaceId target_ws =
                            has_last_companion_workspace_ ? last_companion_workspace_ : WorkspaceId::Media;
                        const std::string target_cid =
                            has_last_companion_workspace_ ? last_companion_custom_tab_id_ : std::string{};
                        selectWorkspace(target_ws, target_cid);
                        if (auto* dock = ensureCompanionDockCreated()) {
                            if (!dock->isDockedOpen()) {
                                dock->toggleVisible();
                            }
                            dock->switchTab(AiCompanionDock::DockTab::Chat);
                            if (!session_id.empty()) {
                                dock->selectSessionById(session_id);
                            }
                            updateResponsiveDock();
                            if (media_view_) media_view_->onViewportLayoutChanged();
                            if (book_view_) book_view_->onViewportLayoutChanged();
                            dock->focusComposer(true);
                            DesktopInputController::markDirty(main_split_);
                        }
                    });
                ai_dirty_ = false;
                views_host_->addView(ai_view_);
                if (has_pending_ai_ext_) {
                    ai_view_->setExternalContext(pending_ai_ext_domain_, pending_ai_ext_summary_);
                }
                DesktopInputController::markDirty(ai_view_);
            }
            return ai_view_;
        }
        case WorkspaceId::Settings: {
            if (!settings_view_) {
                settings_view_ = new HostSettingsView(
                    [this]() { toggleTheme(); },
                    [this]() { selectWorkspace(WorkspaceId::Store); },
                    [this]() { openAuthCenter(); });
                settings_dirty_ = false;
                views_host_->addView(settings_view_);
                DesktopInputController::markDirty(settings_view_);
            }
            return settings_view_;
        }
        case WorkspaceId::CustomPluginTab:
            return active_custom_view_;
    }
    return nullptr;
}

AiCompanionDock* WorkspaceShellActivity::ensureCompanionDockCreated() {
    if (dock_view_ || !main_split_) {
        return dock_view_;
    }
    dock_view_ = new AiCompanionDock(
        [this]() { selectWorkspace(WorkspaceId::Store); },
        [this]() { selectWorkspace(WorkspaceId::AiChat); },
        [this](const std::string& session_id) {
            if (dock_view_ && dock_view_->isDockedOpen()) {
                dock_view_->toggleVisible();
                updateResponsiveDock();
            }
            selectWorkspace(WorkspaceId::AiChat);
            if (ai_view_ && !session_id.empty()) {
                ai_view_->selectSessionById(session_id);
                ai_view_->focusComposer(true);
            }
            DesktopInputController::markDirty(main_split_);
        });
    dock_view_->setId("omnidesk/pane_dock");
    dock_view_->setDomainContext(pending_dock_domain_, pending_dock_summary_);
    main_split_->addView(dock_view_);
    updateResponsiveDock();
    DesktopInputController::markDirty(dock_view_);
    return dock_view_;
}

void WorkspaceShellActivity::onContentAvailable() {
    rebuildRail();
    selectWorkspace(active_workspace_);
    refreshAiPill();
    refreshAuthPill();
    updateResponsiveDock();
    refreshJobBar();

    if (auth_ui_sub_token_ == 0) {
        auth_ui_sub_token_ = AuthCenterManager::instance().subscribeChanges([this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (alive->load(std::memory_order_acquire)) {
                    refreshAuthPill();
                }
            });
        });
    }
    AuthCenterManager::instance().triggerAsyncLiveVerification();

    DesktopInputController::install({
        .on_select_workspace = [this](int idx) { selectWorkspaceByIndex(idx); },
        .on_select_plugin_shortcut = [this](char key) { return selectPluginByShortcut(key); },
        .on_open_settings = [this]() { selectWorkspace(WorkspaceId::Settings); },
        .on_cycle_workspace = [this](int delta) { cycleWorkspace(delta); },
        .on_toggle_dock = [this]() { toggleCompanionDock(); },
        .on_toggle_fullscreen = [this]() { toggleWindowFullscreen(); },
        .on_search = [this]() { triggerSearch(); },
        .on_command_palette = [this]() { openCommandPalette(); },
        .on_toggle_theme = [this]() { toggleTheme(); },
        .on_cancel_active = [this]() { return cancelActiveWork(); },
        .on_skip_ad = [this]() { return skipSponsorSegment(); },
        .is_busy = [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return false;
            return (ai_view_ && ai_view_->hasActiveStream()) ||
                   (dock_view_ && dock_view_->hasActiveStream());
        },
    });

    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        dom_sub_token_ = kernel.domains().subscribeChanges([this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                rebuildPluginTabs();
                if (media_view_) {
                    if (active_workspace_ == WorkspaceId::Media) {
                        media_view_->refresh();
                        media_dirty_ = false;
                    } else {
                        media_dirty_ = true;
                    }
                }
                if (text_view_) {
                    if (active_workspace_ == WorkspaceId::Text) {
                        text_view_->refresh();
                        text_dirty_ = false;
                    } else {
                        text_dirty_ = true;
                    }
                }
                if (book_view_) {
                    if (active_workspace_ == WorkspaceId::Book) {
                        book_view_->refresh();
                        book_dirty_ = false;
                    } else {
                        book_dirty_ = true;
                    }
                }
                if (ai_view_) {
                    if (active_workspace_ == WorkspaceId::AiChat) {
                        ai_view_->refresh();
                        ai_dirty_ = false;
                    } else {
                        ai_dirty_ = true;
                    }
                }
                if (store_view_ && active_workspace_ != WorkspaceId::Store) {
                    store_dirty_ = true;
                }
                if (settings_view_) {
                    if (active_workspace_ == WorkspaceId::Settings) {
                        settings_view_->refresh();
                        settings_dirty_ = false;
                    } else {
                        settings_dirty_ = true;
                    }
                }
                if (dock_view_) dock_view_->onDomainsChanged();
                refreshAiPill();
                refreshAuthPill();
            });
        });
        bus_sub_token_ = kernel.bus().subscribe(
            "entity.ai.route_changed", "host.shell",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    refreshAiPill();
                    if (dock_view_) dock_view_->refreshAiRouteStatus();
                    if (ai_view_) ai_view_->refreshRouteStatus();
                });
            });
        job_sub_token_ = kernel.jobs().subscribe([this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (alive->load(std::memory_order_acquire)) {
                    refreshJobBar();
                }
            });
        });
        for (const char* topic :
             {"entity.book.open", "entity.text.open", "entity.media.open",
              "entity.web.open", "entity.workspace.open", "entity.workspace.return",
              "job.open_requested", "cmd:curation:calendar", "cmd:curation:scheduler",
              "cmd:auth:center", "entity.curation.filter_date"}) {
            uint64_t tok = kernel.bus().subscribe(
                topic, "host.shell.job_open",
                [this, alive = alive_](const std::string& ev_topic, const nlohmann::json& payload) {
                    if (!payload.is_object()) {
                        return;
                    }
                    const std::string origin = payload.value("origin", "");
                    if (origin == "host_workbench" || origin == "plugin_music") {
                        return;
                    }
                    if (ev_topic == "job.open_requested" && !payload.contains("job_id")) {
                        return;
                    }
                    brls::sync([this, alive, ev_topic, payload]() {
                        if (alive->load(std::memory_order_acquire)) {
                            handleJobOpenEvent(ev_topic, payload);
                        }
                    });
                });
            job_open_sub_tokens_.push_back(tok);
        }
        job_open_sub_tokens_.push_back(kernel.bus().subscribe(
            "entity.ai.companion_ask", "host.shell.companion",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                if (!payload.is_object()) return;
                const std::string dom_s = payload.value("domain", "core");
                const std::string ctx_s = payload.value("context", "");
                const std::string prompt = payload.value("prompt", "");
                const bool full_stage = payload.value("full_stage", false);
                const bool curate = payload.value("curation", false);
                brls::sync([this, alive, dom_s, ctx_s, prompt, full_stage, curate]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    const ContentDomain dom = parseContentDomain(dom_s);
                    if (full_stage || curate || active_workspace_ == WorkspaceId::AiChat) {
                        selectWorkspace(WorkspaceId::AiChat);
                        if (ai_view_) {
                            ai_view_->askWithContext(dom, ctx_s, prompt, curate);
                        }
                    } else if (auto* dock = ensureCompanionDockCreated()) {
                        setExternalAiContext(dom, ctx_s);
                        setCompanionDomainContext(dom, ctx_s);
                        dock->askWithContext(dom, ctx_s, prompt);
                        updateResponsiveDock();
                        DesktopInputController::markDirty(dock);
                    }
                });
            }));
        job_open_sub_tokens_.push_back(kernel.bus().subscribe(
            "entity.search.open", "host.shell.search",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                const std::string q = payload.is_object() ? payload.value("query", "") : std::string{};
                brls::sync([this, alive, q]() {
                    if (alive->load(std::memory_order_acquire)) {
                        openCommandPalette(q);
                    }
                });
            }));

        CurationSchedulerManager::instance().start();
        sched_ui_sub_token_ = CurationSchedulerManager::instance().subscribeStatus(
            [this, alive = alive_](const nlohmann::json& st) {
                brls::sync([this, alive, st]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refreshCurationPill(st);
                    }
                });
            });

        auto join_names = [](const std::vector<std::string>& names) {
            std::string joined;
            for (size_t i = 0; i < names.size() && i < 3; ++i) {
                if (!joined.empty()) joined += ", ";
                joined += names[i];
            }
            if (names.size() > 3) joined += " 等";
            return joined;
        };

        const uint64_t wf_tok = kernel.bus().subscribe(
            "worker.fault", "host.shell.fault",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                const std::string pid = payload.is_object()
                    ? payload.value("plugin_id", payload.value("worker_id", "unknown-plugin"))
                    : std::string("unknown-plugin");
                const std::string sig = payload.is_object() ? payload.value("signal_name", "") : "";
                const std::string err = payload.is_object() ? payload.value("error", "") : "";
                const bool quarantined = payload.is_object() && payload.value("quarantined", false);
                const bool restarted = payload.is_object() && payload.value("restart_succeeded", false);
                const std::string detail = !sig.empty() ? sig : err;
                std::string msg;
                if (quarantined) {
                    msg = "⚠ 隔离插件 " + pid + " 连续崩溃已熔断停用 (" + detail + ")";
                } else if (restarted) {
                    msg = "⚠ 隔离插件 " + pid + " 异常退出 (" + detail + ")，已自动拉起，宿主未受影响";
                } else {
                    msg = "⚠ 隔离插件 " + pid + " 异常 (" + detail + ")，宿主运行未受影响";
                }
                brls::sync([this, alive, msg = std::move(msg)]() {
                    if (alive->load(std::memory_order_acquire)) {
                        showWarningBanner(msg, "worker.fault");
                    }
                });
            });
        if (wf_tok != 0) fault_sub_tokens_.push_back(wf_tok);

        const uint64_t pf_tok = kernel.bus().subscribe(
            "plugin.fault", "host.shell.fault",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                const std::string pid = payload.is_object()
                    ? payload.value("plugin_id", "unknown-plugin")
                    : std::string("unknown-plugin");
                const std::string comp = payload.is_object() ? payload.value("component", "") : "";
                const std::string err = payload.is_object() ? payload.value("error", "") : "";
                std::string msg = "⚠ 插件 " + pid;
                if (!comp.empty()) msg += " [" + comp + "]";
                msg += " 异常已被宿主隔离拦截: " + err;
                brls::sync([this, alive, msg = std::move(msg)]() {
                    if (alive->load(std::memory_order_acquire)) {
                        showWarningBanner(msg, "plugin.fault");
                    }
                });
            });
        if (pf_tok != 0) fault_sub_tokens_.push_back(pf_tok);

        const uint64_t abi_tok = kernel.bus().subscribe(
            "plugin.abi_drift", "host.shell.fault",
            [this, alive = alive_, join_names](const std::string&, const nlohmann::json& payload) {
                std::vector<std::string> names;
                if (payload.is_object() && payload.contains("stale_plugins") &&
                    payload["stale_plugins"].is_array()) {
                    for (const auto& item : payload["stale_plugins"]) {
                        if (item.is_string()) {
                            names.push_back(item.get<std::string>());
                        }
                    }
                }
                if (names.empty()) return;
                std::string msg = "⚠ 检测到 " + std::to_string(names.size()) +
                                  " 个插件 SDK ABI 已过期 (" + join_names(names) +
                                  ")，请前往插件市场一键重编";
                brls::sync([this, alive, msg = std::move(msg)]() {
                    if (alive->load(std::memory_order_acquire)) {
                        showWarningBanner(msg, "plugin.abi_drift");
                    }
                });
            });
        if (abi_tok != 0) fault_sub_tokens_.push_back(abi_tok);

        const uint64_t st_tok = kernel.bus().subscribe(
            "plugin.state_changed", "host.shell.fault",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (!alive->load(std::memory_order_acquire) ||
                        !fault_banner_box_ ||
                        fault_banner_box_->getVisibility() != brls::Visibility::VISIBLE) {
                        return;
                    }
                    if (active_warning_topic_ != "plugin.abi_drift" &&
                        active_warning_topic_ != "plugin.load_error") {
                        return;
                    }
                    auto& k = HostKernel::instance();
                    if (!k.isInitialized()) return;
                    if (!k.hasStaleAbiPlugins(false)) {
                        bool any_failed = false;
                        for (const auto& st : k.plugins().statuses()) {
                            if (st.record.enabled && !st.load_error.empty()) {
                                any_failed = true;
                                break;
                            }
                        }
                        if (!any_failed) {
                            dismissWarningBanner();
                        }
                    }
                });
            });
        if (st_tok != 0) fault_sub_tokens_.push_back(st_tok);

        // Check boot-time ABI drift or load errors emitted before UI subscribed
        std::vector<std::string> stale_names;
        for (const auto& entry : kernel.installedAbiStatuses(false)) {
            if (entry.needs_rebuild) {
                stale_names.push_back(entry.plugin_id);
            }
        }
        if (!stale_names.empty()) {
            showWarningBanner("⚠ 检测到 " + std::to_string(stale_names.size()) +
                                  " 个已安装插件 SDK ABI 已过期 (" + join_names(stale_names) +
                                  ")，请前往插件市场一键重编",
                              "plugin.abi_drift");
        } else {
            std::vector<std::string> failed_names;
            for (const auto& st : kernel.plugins().statuses()) {
                if (st.record.enabled && !st.load_error.empty()) {
                    failed_names.push_back(st.record.manifest.id);
                }
            }
            if (!failed_names.empty()) {
                showWarningBanner("⚠ 部分已启用插件加载异常 (" + join_names(failed_names) +
                                      ")，请前往插件市场诊断或重编",
                                  "plugin.load_error");
            }
        }
    }

    resize_sub_ = brls::Application::getWindowSizeChangedEvent()->subscribe([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            updateResponsiveDock();
            if (ai_view_) ai_view_->onViewportLayoutChanged();
        }
    });
}

brls::Box* WorkspaceShellActivity::makeRailButton(const std::string& label,
                                                  const std::string& icon_kind,
                                                  const std::string& domain_token,
                                                  const std::string& shortcut_hint,
                                                  bool active,
                                                  bool compact,
                                                  std::function<void()> on_click,
                                                  NavEntry* out_refs) {
    const bool dark = HostKernel::instance().isInitialized() ? HostKernel::instance().isDarkTheme() : true;
    const NVGcolor dom_col = ui::color(domain_token);
    const NVGcolor surf_col = ui::color("desktop/surface");
    const NVGcolor soft_col = ui::color("desktop/soft");
    const NVGcolor line_col = ui::color("desktop/line");
    const NVGcolor text_col = ui::color("brls/text");
    const NVGcolor muted_col = ui::color("font/grey");

    auto* item = new brls::Box(brls::Axis::ROW);
    item->setWidthPercentage(100);
    item->setHeight(compact ? 34.0f : 38.0f);
    item->setShrink(0.0f);
    item->setPadding(0, 8, 0, 0);
    item->setMarginBottom(compact ? 2.0f : 3.0f);
    item->setCornerRadius(8.0f);
    item->setFocusable(true);
    item->setHighlightCornerRadius(8.0f);
    item->setAlignItems(brls::AlignItems::CENTER);
    item->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    item->setBorderThickness(0.0f);
    item->setBackgroundColor(
        active ? blendColor(surf_col, dom_col, dark ? 0.16f : 0.10f) : nvgRGBA(0, 0, 0, 0));

    auto* left = new brls::Box(brls::Axis::ROW);
    left->setAlignItems(brls::AlignItems::CENTER);
    left->setShrink(1.0f);
    left->setMinWidth(0);

    // Soft left semantic accent bar (replaces harsh full-width pink border)
    auto* bar = new brls::Box(brls::Axis::COLUMN);
    bar->setWidth(3.0f);
    bar->setHeight(compact ? 16.0f : 18.0f);
    bar->setCornerRadius(1.5f);
    bar->setShrink(0.0f);
    bar->setMarginLeft(2.5f);
    bar->setMarginRight(7.5f);
    bar->setBackgroundColor(active ? dom_col : nvgRGBA(0, 0, 0, 0));
    left->addView(bar);

    SvgIconView* icon = nullptr;
    if (!icon_kind.empty()) {
        icon = new SvgIconView();
        const NVGcolor icon_col = active ? dom_col : blendColor(muted_col, dom_col, 0.28f);
        icon->setNavIcon(icon_kind, icon_col);
        const float sz = compact ? 16.0f : 18.0f;
        icon->setDimensions(sz, sz);
        icon->setShrink(0.0f);
        icon->setMarginRight(8.5f);
        left->addView(icon);
    }

    auto* lbl = ui::makeLabel(label, compact ? 13.8f : 14.8f, "brls/text");
    lbl->setSingleLine(true);
    lbl->setShrink(1.0f);
    lbl->setTextColor(active ? blendColor(text_col, dom_col, 0.28f)
                             : blendColor(text_col, muted_col, compact ? 0.14f : 0.03f));
    left->addView(lbl);
    item->addView(left);

    brls::Box* kbd_box = nullptr;
    brls::Label* kbd_lbl = nullptr;
    if (!shortcut_hint.empty()) {
        const NVGcolor k_bg = active ? blendColor(surf_col, dom_col, dark ? 0.24f : 0.15f) : soft_col;
        const NVGcolor k_bd = active ? blendColor(line_col, dom_col, 0.65f) : line_col;
        const NVGcolor k_fg = active ? dom_col : muted_col;
        kbd_box = makeKeycapBadge(shortcut_hint, k_bg, k_bd, k_fg, &kbd_lbl);
        kbd_box->setMarginLeft(6.0f);
        item->addView(kbd_box);
    }

    if (out_refs) {
        out_refs->button = item;
        out_refs->accent_bar = bar;
        out_refs->icon_view = icon;
        out_refs->title_label = lbl;
        out_refs->keycap_box = kbd_box;
        out_refs->keycap_label = kbd_lbl;
        out_refs->styled_state = (active ? 1 : 0) | (dark ? 2 : 0);
    }

    item->registerClickAction([alive = alive_, fn = std::move(on_click)](brls::View*) {
        brls::sync([alive, fn]() {
            if (alive->load(std::memory_order_acquire) && fn) {
                fn();
            }
        });
        return true;
    });
    return item;
}

void WorkspaceShellActivity::rebuildRail() {
    if (focusIsInside(rail_box_)) {
        brls::Application::giveFocus(nullptr);
    }
    built_in_nav_box_->clearViews();
    nav_entries_.clear();
    group_headers_.clear();
    alt_shortcut_map_.clear();

    struct BaseNav {
        WorkspaceId id;
        const char* label;
        const char* icon_kind;
        const char* shortcut;
        ContentDomain domain;
        const char* domain_token;
    };
    const BaseNav items[] = {
        {WorkspaceId::Store, "插件市场", "store", "Ctrl+1", ContentDomain::Core, "domain/core"},
        {WorkspaceId::Media, "影视与音乐", "media", "Ctrl+2", ContentDomain::Media, "domain/media"},
        {WorkspaceId::Text, "文字与期刊", "text", "Ctrl+3", ContentDomain::Text, "domain/text"},
        {WorkspaceId::Book, "书本与 PDF", "book", "Ctrl+4", ContentDomain::Book, "domain/book"},
        {WorkspaceId::AiChat, "AI 对话", "ai", "Ctrl+5", ContentDomain::Core, "domain/core"},
    };

    for (const auto& it : items) {
        const bool active = (active_workspace_ == it.id);
        NavEntry entry{};
        entry.id = it.id;
        entry.label = it.label;
        entry.domain = it.domain;
        entry.domain_token = it.domain_token;
        entry.icon_kind = it.icon_kind;
        entry.shortcut_label = it.shortcut;
        entry.group_idx = -1;
        auto* btn = makeRailButton(
            it.label, it.icon_kind, it.domain_token, it.shortcut, active, false,
            [this, id = it.id]() { selectWorkspace(id); }, &entry);
        built_in_nav_box_->addView(btn);
        nav_entries_.push_back(std::move(entry));
    }

    rebuildPluginTabs();
}

void WorkspaceShellActivity::rebuildPluginTabs(int focus_group_idx) {
    const bool restore_focus = (focus_group_idx < 0) && focusIsInside(custom_nav_box_);
    if (restore_focus || focus_group_idx >= 0) {
        brls::Application::giveFocus(nullptr);
    }
    custom_nav_box_->clearViews();
    group_headers_.clear();
    alt_shortcut_map_.clear();
    nav_entries_.resize(std::min<size_t>(nav_entries_.size(), 5));

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        if (restore_focus) focusActiveWorkspace();
        return;
    }

    const auto tabs = kernel.domains().workspaceTabs();
    std::unordered_set<std::string> live_tab_ids;
    for (const auto& t : tabs) {
        live_tab_ids.insert(t.tab_id);
    }
    for (auto it = custom_tab_views_.begin(); it != custom_tab_views_.end();) {
        if (live_tab_ids.find(it->first) == live_tab_ids.end()) {
            if (it->second == active_custom_view_) {
                active_custom_view_ = nullptr;
            }
            if (views_host_ && it->second) {
                if (focusIsInside(it->second)) {
                    brls::Application::giveFocus(nullptr);
                }
                views_host_->removeView(it->second, true);
            }
            it = custom_tab_views_.erase(it);
        } else {
            ++it;
        }
    }

    if (tabs.empty()) {
        if (restore_focus) focusActiveWorkspace();
        return;
    }

    const auto assigned = assignPluginNavTabs(tabs);
    std::vector<AssignedPluginTab> buckets[3];
    for (const auto& item : assigned) {
        if (item.alt_key != '\0') {
            alt_shortcut_map_[item.alt_key] = item.spec.tab_id;
        }
        const int gi = std::clamp(static_cast<int>(item.group), 0, 2);
        buckets[gi].push_back(item);
    }

    brls::View* target_group_header = nullptr;
    const NavGroupId groups[] = {NavGroupId::Media, NavGroupId::CommunityWeb, NavGroupId::Book};

    for (NavGroupId g : groups) {
        const int gi = static_cast<int>(g);
        const auto& bucket = buckets[gi];
        if (bucket.empty()) continue;

        const bool collapsed = collapsed_groups_[gi];
        const char* dom_tok = navGroupDomainToken(g);
        const NVGcolor dom_col = ui::color(dom_tok);
        const NVGcolor muted_col = ui::color("font/grey");

        // Collapsible domain group header
        auto* hdr_row = new brls::Box(brls::Axis::ROW);
        hdr_row->setShrink(0.0f);
        hdr_row->setWidthPercentage(100);
        hdr_row->setHeight(25.0f);
        hdr_row->setPadding(0, 6, 0, 6);
        hdr_row->setMarginTop(6.0f);
        hdr_row->setMarginBottom(2.0f);
        hdr_row->setCornerRadius(6.0f);
        hdr_row->setHighlightCornerRadius(6.0f);
        hdr_row->setFocusable(true);
        hdr_row->setAlignItems(brls::AlignItems::CENTER);
        hdr_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

        auto* hdr_left = new brls::Box(brls::Axis::ROW);
        hdr_left->setAlignItems(brls::AlignItems::CENTER);
        auto* dot = new brls::Box(brls::Axis::ROW);
        dot->setWidth(6.0f);
        dot->setHeight(6.0f);
        dot->setCornerRadius(3.0f);
        dot->setBackgroundColor(dom_col);
        dot->setMarginRight(7.0f);
        hdr_left->addView(dot);

        auto* grp_lbl = ui::makeLabel(
            std::string(navGroupTitle(g)) + " · " + std::to_string(bucket.size()),
            11.8f, "font/grey");
        grp_lbl->setSingleLine(true);
        hdr_left->addView(grp_lbl);
        hdr_row->addView(hdr_left);

        auto* hdr_right = new brls::Box(brls::Axis::ROW);
        hdr_right->setAlignItems(brls::AlignItems::CENTER);

        brls::Label* collapsed_active_lbl = nullptr;
        auto* collapsed_active_badge = makeKeycapBadge(
            "", ui::color("desktop/soft"), blendColor(ui::color("desktop/line"), dom_col, 0.55f),
            dom_col, &collapsed_active_lbl);
        collapsed_active_badge->setMarginRight(5.0f);
        collapsed_active_badge->setVisibility(brls::Visibility::GONE);
        hdr_right->addView(collapsed_active_badge);

        auto* chev = new SvgIconView();
        chev->setNavIcon(collapsed ? "chevron_right" : "chevron_down", muted_col);
        chev->setDimensions(12.0f, 12.0f);
        chev->setShrink(0.0f);
        hdr_right->addView(chev);
        hdr_row->addView(hdr_right);

        hdr_row->registerClickAction([this, alive = alive_, gi](brls::View*) {
            brls::sync([this, alive, gi]() {
                if (!alive->load(std::memory_order_acquire)) return;
                collapsed_groups_[gi] = !collapsed_groups_[gi];
                rebuildPluginTabs(gi);
            });
            return true;
        });

        custom_nav_box_->addView(hdr_row);
        group_headers_.push_back({gi, dom_tok, hdr_row, collapsed_active_badge, collapsed_active_lbl});
        if (focus_group_idx == gi) {
            target_group_header = hdr_row;
        }

        for (const auto& item : bucket) {
            const bool active =
                (active_workspace_ == WorkspaceId::CustomPluginTab &&
                 active_custom_tab_id_ == item.spec.tab_id);
            NavEntry entry{};
            entry.id = WorkspaceId::CustomPluginTab;
            entry.custom_tab_id = item.spec.tab_id;
            entry.label = item.spec.title;
            entry.domain = item.spec.domain;
            entry.domain_token = item.domain_token;
            entry.icon_kind = item.icon_kind;
            entry.alt_key = item.alt_key;
            entry.shortcut_label = item.shortcut_label;
            entry.group_idx = gi;

            if (!collapsed) {
                auto* btn = makeRailButton(
                    item.spec.title, item.icon_kind, item.domain_token, item.shortcut_label,
                    active, true,
                    [this, tid = item.spec.tab_id]() {
                        selectWorkspace(WorkspaceId::CustomPluginTab, tid);
                    },
                    &entry);
                custom_nav_box_->addView(btn);
            }
            nav_entries_.push_back(std::move(entry));
        }
    }

    updateActiveNavStyling();
    DesktopInputController::markDirty(rail_box_);

    if (target_group_header) {
        brls::Application::giveFocus(target_group_header);
    } else if (restore_focus) {
        focusActiveWorkspace();
    }
}

void WorkspaceShellActivity::updateActiveNavStyling() {
    const bool dark = HostKernel::instance().isInitialized() ? HostKernel::instance().isDarkTheme() : true;
    const NVGcolor surf_col = ui::color("desktop/surface");
    const NVGcolor soft_col = ui::color("desktop/soft");
    const NVGcolor line_col = ui::color("desktop/line");
    const NVGcolor text_col = ui::color("brls/text");
    const NVGcolor muted_col = ui::color("font/grey");

    int active_group_idx = -1;
    std::string active_group_shortcut;

    for (auto& entry : nav_entries_) {
        const bool active =
            (entry.id == active_workspace_) &&
            (active_workspace_ != WorkspaceId::CustomPluginTab ||
             entry.custom_tab_id == active_custom_tab_id_);
        if (active && entry.group_idx >= 0) {
            active_group_idx = entry.group_idx;
            active_group_shortcut = entry.shortcut_label;
        }
        if (!entry.button) continue;

        const int desired_state = (active ? 1 : 0) | (dark ? 2 : 0);
        if (entry.styled_state == desired_state) {
            continue;
        }
        entry.styled_state = desired_state;

        const bool compact = (entry.group_idx >= 0);
        const NVGcolor dom_col = ui::color(entry.domain_token);

        entry.button->setBackgroundColor(
            active ? blendColor(surf_col, dom_col, dark ? 0.16f : 0.10f) : nvgRGBA(0, 0, 0, 0));
        entry.button->setBorderThickness(0.0f);

        if (entry.accent_bar) {
            entry.accent_bar->setBackgroundColor(active ? dom_col : nvgRGBA(0, 0, 0, 0));
        }
        if (entry.icon_view && !entry.icon_kind.empty()) {
            const NVGcolor icon_col = active ? dom_col : blendColor(muted_col, dom_col, 0.28f);
            entry.icon_view->setNavIcon(entry.icon_kind, icon_col);
        }
        if (entry.title_label) {
            entry.title_label->setTextColor(
                active ? blendColor(text_col, dom_col, 0.28f)
                       : blendColor(text_col, muted_col, compact ? 0.14f : 0.03f));
        }
        if (entry.keycap_box && entry.keycap_label) {
            entry.keycap_box->setBackgroundColor(
                active ? blendColor(surf_col, dom_col, dark ? 0.24f : 0.15f) : soft_col);
            entry.keycap_box->setBorderColor(
                active ? blendColor(line_col, dom_col, 0.65f) : line_col);
            entry.keycap_label->setTextColor(active ? dom_col : muted_col);
        }
    }

    // If a collapsed section owns the currently active plugin tab, surface its keycap on the header
    for (const auto& gh : group_headers_) {
        if (!gh.active_badge_box || !gh.active_badge_label) continue;
        const bool show = collapsed_groups_[gh.group_idx] &&
                          (gh.group_idx == active_group_idx) &&
                          !active_group_shortcut.empty();
        if (show) {
            gh.active_badge_label->setText(active_group_shortcut);
            gh.active_badge_box->setVisibility(brls::Visibility::VISIBLE);
        } else {
            gh.active_badge_box->setVisibility(brls::Visibility::GONE);
        }
    }
}

void WorkspaceShellActivity::selectWorkspace(WorkspaceId id, const std::string& custom_tab_id) {
    InlineEditorRegistry::blurActiveEditor();
    if (immersive_fullscreen_ && id != WorkspaceId::Media) {
        setImmersiveFullscreen(false);
    }
    active_workspace_ = id;
    active_custom_tab_id_ = custom_tab_id;
    if (id != WorkspaceId::AiChat) {
        last_companion_workspace_ = id;
        last_companion_custom_tab_id_ = custom_tab_id;
        has_last_companion_workspace_ = true;
    }

    if (active_custom_view_) {
        if (focusIsInside(active_custom_view_)) {
            brls::Application::giveFocus(nullptr);
        }
        active_custom_view_->setVisibility(brls::Visibility::GONE);
        active_custom_view_ = nullptr;
    }

    ContentDomain dock_dom = ContentDomain::Core;
    if (id == WorkspaceId::Media) dock_dom = ContentDomain::Media;
    else if (id == WorkspaceId::Text) dock_dom = ContentDomain::Text;
    else if (id == WorkspaceId::Book) dock_dom = ContentDomain::Book;

    if (id != WorkspaceId::CustomPluginTab) {
        setCompanionDomainContext(dock_dom);
        ensureWorkspaceCreated(id);
    }

    if (store_view_) {
        store_view_->setVisibility(id == WorkspaceId::Store ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (media_view_) {
        media_view_->setVisibility(id == WorkspaceId::Media ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (text_view_) {
        text_view_->setVisibility(id == WorkspaceId::Text ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (book_view_) {
        book_view_->setVisibility(id == WorkspaceId::Book ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (ai_view_) {
        ai_view_->setVisibility(id == WorkspaceId::AiChat ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (settings_view_) {
        settings_view_->setVisibility(id == WorkspaceId::Settings ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }

    if (id == WorkspaceId::CustomPluginTab) {
        auto cached_it = custom_tab_views_.find(custom_tab_id);
        for (const auto& tab : HostKernel::instance().domains().workspaceTabs()) {
            if (tab.tab_id == custom_tab_id) {
                dock_dom = tab.domain;
                if (cached_it != custom_tab_views_.end() && cached_it->second) {
                    active_custom_view_ = cached_it->second;
                    active_custom_view_->setVisibility(brls::Visibility::VISIBLE);
                } else if (tab.create_view) {
                    active_custom_view_ = tab.create_view();
                    if (active_custom_view_ && views_host_) {
                        views_host_->addView(active_custom_view_);
                        custom_tab_views_[custom_tab_id] = active_custom_view_;
                    }
                }
                break;
            }
        }
        setCompanionDomainContext(dock_dom);
    } else if (id == WorkspaceId::Store && store_view_) {
        if (store_dirty_) {
            store_view_->refresh();
            store_dirty_ = false;
        } else {
            store_view_->refreshIfNeeded();
        }
    } else if (id == WorkspaceId::Media && media_view_ && media_dirty_) {
        media_view_->refresh();
        media_dirty_ = false;
    } else if (id == WorkspaceId::Text && text_view_ && text_dirty_) {
        text_view_->refresh();
        text_dirty_ = false;
    } else if (id == WorkspaceId::Book && book_view_ && book_dirty_) {
        book_view_->refresh();
        book_dirty_ = false;
    } else if (id == WorkspaceId::AiChat && ai_view_ && ai_dirty_) {
        ai_view_->refresh();
        ai_dirty_ = false;
    } else if (id == WorkspaceId::Settings && settings_view_ && settings_dirty_) {
        settings_view_->refresh();
        settings_dirty_ = false;
    }

    updateActiveNavStyling();
    focusActiveWorkspace();
    if (id == WorkspaceId::AiChat && ai_view_) {
        ai_view_->focusComposer(true);
    }
    DesktopInputController::markDirty(views_host_);
}

void WorkspaceShellActivity::focusActiveWorkspace() {
    brls::View* target = nullptr;
    switch (active_workspace_) {
        case WorkspaceId::Store: target = store_view_; break;
        case WorkspaceId::Media: target = media_view_; break;
        case WorkspaceId::Text: target = text_view_; break;
        case WorkspaceId::Book: target = book_view_; break;
        case WorkspaceId::AiChat: target = ai_view_; break;
        case WorkspaceId::Settings: target = settings_view_; break;
        case WorkspaceId::CustomPluginTab: target = active_custom_view_; break;
    }
    if (target) {
        target = target->getDefaultFocus();
    }
    if (!target) {
        for (const auto& entry : nav_entries_) {
            const bool same =
                entry.id == active_workspace_ &&
                (active_workspace_ != WorkspaceId::CustomPluginTab ||
                 entry.custom_tab_id == active_custom_tab_id_);
            if (same && entry.button) {
                target = entry.button->getDefaultFocus();
                break;
            }
        }
    }
    if (!target && root_box_) {
        target = root_box_->getDefaultFocus();
    }
    if (target) {
        brls::Application::giveFocus(target);
    }
}

void WorkspaceShellActivity::selectWorkspaceByIndex(int zero_based_index) {
    if (zero_based_index < 0 || static_cast<size_t>(zero_based_index) >= nav_entries_.size()) {
        return;
    }
    const auto& target = nav_entries_[static_cast<size_t>(zero_based_index)];
    selectWorkspace(target.id, target.custom_tab_id);
}

bool WorkspaceShellActivity::selectPluginByShortcut(char alt_key) {
    const auto it = alt_shortcut_map_.find(alt_key);
    if (it == alt_shortcut_map_.end() || it->second.empty()) {
        return false;
    }
    selectWorkspace(WorkspaceId::CustomPluginTab, it->second);
    return true;
}

void WorkspaceShellActivity::cycleWorkspace(int delta) {
    if (nav_entries_.empty()) return;
    int cur = 0;
    for (size_t i = 0; i < nav_entries_.size(); ++i) {
        if (nav_entries_[i].id == active_workspace_ &&
            (active_workspace_ != WorkspaceId::CustomPluginTab ||
             nav_entries_[i].custom_tab_id == active_custom_tab_id_)) {
            cur = static_cast<int>(i);
            break;
        }
    }
    int n = static_cast<int>(nav_entries_.size());
    int next = ((cur + delta) % n + n) % n;
    selectWorkspaceByIndex(next);
}

void WorkspaceShellActivity::toggleCompanionDock() {
    auto* dock = ensureCompanionDockCreated();
    if (!dock) return;
    if (immersive_fullscreen_) {
        setImmersiveFullscreen(false);
    }
    dock->toggleVisible();
    updateResponsiveDock();
    if (media_view_) media_view_->onViewportLayoutChanged();
    if (book_view_) book_view_->onViewportLayoutChanged();
    if (ai_view_) ai_view_->onViewportLayoutChanged();
    DesktopInputController::markDirty(main_split_);
}

void WorkspaceShellActivity::toggleWindowFullscreen() {
    if (immersive_fullscreen_) {
        setImmersiveFullscreen(false);
        return;
    }
    if (auto* plat = brls::Application::getPlatform()) {
        if (auto* vc = plat->getVideoContext()) {
            const bool next_fs = !::VideoContext::FULLSCREEN;
            vc->fullScreen(next_fs);
            brls::Application::notify(next_fs ? "已切换全屏显示 (F11)" : "已退出全屏显示");
        }
    }
}

void WorkspaceShellActivity::toggleImmersiveFullscreen() {
    setImmersiveFullscreen(!immersive_fullscreen_);
}

void WorkspaceShellActivity::setImmersiveFullscreen(bool active) {
    if (immersive_fullscreen_ == active) {
        return;
    }
    if (active && active_workspace_ != WorkspaceId::Media) {
        selectWorkspace(WorkspaceId::Media);
    }
    immersive_fullscreen_ = active;

    if (active) {
        immersive_entered_from_window_fs_ = ::VideoContext::FULLSCREEN;
        if (focusIsInside(rail_box_) || focusIsInside(dock_view_) || focusIsInside(chrome_bottom_box_)) {
            brls::Application::giveFocus(nullptr);
        }
        if (rail_box_) rail_box_->setVisibility(brls::Visibility::GONE);
        if (chrome_bottom_box_) chrome_bottom_box_->setVisibility(brls::Visibility::GONE);
        if (dock_view_) dock_view_->setVisibility(brls::Visibility::GONE);
        if (!immersive_entered_from_window_fs_) {
            if (auto* plat = brls::Application::getPlatform()) {
                if (auto* vc = plat->getVideoContext()) {
                    vc->fullScreen(true);
                }
            }
        }
        if (media_view_) {
            media_view_->setImmersiveFullscreen(true);
        }
        focusActiveWorkspace();
        brls::Application::notify("已进入沉浸全屏播放（按 Esc 或 F11 退出）");
    } else {
        if (rail_box_) rail_box_->setVisibility(brls::Visibility::VISIBLE);
        if (chrome_bottom_box_) chrome_bottom_box_->setVisibility(brls::Visibility::VISIBLE);
        if (dock_view_) {
            dock_view_->setVisibility(
                dock_view_->isDockedOpen() ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        }
        if (!immersive_entered_from_window_fs_) {
            if (auto* plat = brls::Application::getPlatform()) {
                if (auto* vc = plat->getVideoContext()) {
                    vc->fullScreen(false);
                }
            }
        }
        updateResponsiveDock();
        if (media_view_) {
            media_view_->setImmersiveFullscreen(false);
        }
        if (book_view_) {
            book_view_->onViewportLayoutChanged();
        }
        if (ai_view_) {
            ai_view_->onViewportLayoutChanged();
        }
        focusActiveWorkspace();
    }
    DesktopInputController::markDirty(root_box_);
}

void WorkspaceShellActivity::triggerSearch() {
    switch (active_workspace_) {
        case WorkspaceId::Store: if (store_view_) store_view_->promptSearch(); break;
        case WorkspaceId::Media: if (media_view_) media_view_->promptSearch(); break;
        case WorkspaceId::Text: if (text_view_) text_view_->promptSearch(); break;
        case WorkspaceId::Book: if (book_view_) book_view_->promptSearch(); break;
        case WorkspaceId::AiChat: if (ai_view_) ai_view_->focusComposer(true); break;
        default: break;
    }
}

void WorkspaceShellActivity::openCommandPalette(const std::string& initial_query) {
    CommandPaletteCallbacks cbs;
    cbs.on_execute_command = [this, alive = alive_](const std::string& cmd, const nlohmann::json&) {
        if (!alive->load(std::memory_order_acquire)) return;
        if (cmd == "workspace:store") selectWorkspace(WorkspaceId::Store);
        else if (cmd == "workspace:media") selectWorkspace(WorkspaceId::Media);
        else if (cmd == "workspace:text") selectWorkspace(WorkspaceId::Text);
        else if (cmd == "workspace:book") selectWorkspace(WorkspaceId::Book);
        else if (cmd == "workspace:ai") selectWorkspace(WorkspaceId::AiChat);
        else if (cmd == "auth:center") openAuthCenter();
        else if (cmd == "ai:curate") {
            selectWorkspace(WorkspaceId::AiChat);
            if (ai_view_) {
                ai_view_->startNewSession(true);
                ai_view_->focusComposer(true);
            }
        }
        else if (cmd == "workspace:settings") selectWorkspace(WorkspaceId::Settings);
        else if (cmd == "toggle_dock") toggleCompanionDock();
        else if (cmd == "toggle_fullscreen") toggleWindowFullscreen();
        else if (cmd == "toggle_theme") toggleTheme();
        else if (cmd == "show_shortcuts") DesktopInputController::showShortcutsHelp();
        else if (cmd == "playback:play_pause") PlaybackCoordinator::instance().playPause();
        else if (cmd == "playback:resume_music") PlaybackCoordinator::instance().resumeSuspendedMusic();
        else if (cmd == "curation:calendar") CurationCalendarDialog::open();
        else if (cmd == "curation:scheduler") CurationSchedulerDialog::open();
    };
    cbs.on_open_workspace_tab = [this, alive = alive_](const std::string& tab_id) {
        if (!alive->load(std::memory_order_acquire) || tab_id.empty()) return;
        selectWorkspace(WorkspaceId::CustomPluginTab, tab_id);
    };
    cbs.on_open_vocab = [this, alive = alive_](const std::string& word) {
        if (!alive->load(std::memory_order_acquire)) return;
        selectWorkspace(WorkspaceId::Book);
        if (book_view_) {
            book_view_->openExternalBook({{"sub_mode", "vocab"}, {"word", word}});
        }
    };
    OmniCommandPalette::open(initial_query, std::move(cbs));
}

void WorkspaceShellActivity::openPluginConfigInStore(const std::string& plugin_id) {
    selectWorkspace(WorkspaceId::Store);
    if (store_view_ && !plugin_id.empty()) {
        store_view_->focusPluginConfig(plugin_id);
    }
}

void WorkspaceShellActivity::openAuthCenter(const std::string& focus_plugin_id) {
    AuthCenterCallbacks cbs;
    cbs.on_open_plugin_config = [this, alive = alive_](const std::string& plugin_id) {
        if (alive->load(std::memory_order_acquire)) {
            openPluginConfigInStore(plugin_id);
        }
    };
    cbs.on_open_custom_tab = [this, alive = alive_](const std::string& tab_id) {
        if (alive->load(std::memory_order_acquire) && !tab_id.empty()) {
            selectWorkspace(WorkspaceId::CustomPluginTab, tab_id);
        }
    };
    cbs.on_open_ai_workbench = [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            selectWorkspace(WorkspaceId::AiChat);
        }
    };
    AuthCenterDialog::open(std::move(cbs), focus_plugin_id);
}

void WorkspaceShellActivity::toggleTheme() {
    if (immersive_fullscreen_) {
        setImmersiveFullscreen(false);
    }
    InlineEditorRegistry::blurActiveEditor();
    auto& kernel = HostKernel::instance();
    kernel.toggleTheme();
    brls::Application::notify(kernel.isDarkTheme() ? "已切换 Graphite 暗色主题" : "已切换 Paper 亮色主题");
    brls::sync([this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire) || !root_box_) return;
        brls::Application::giveFocus(nullptr);
        const WorkspaceId cur = active_workspace_;
        const std::string cid = active_custom_tab_id_;
        const bool was_dock_open = dock_view_ && dock_view_->isDockedOpen();
        const ContentDomain saved_dock_domain =
            dock_view_ ? dock_view_->domainContext() : pending_dock_domain_;
        const std::string saved_dock_summary =
            dock_view_ ? dock_view_->entitySummary() : pending_dock_summary_;

        active_custom_view_ = nullptr;
        custom_tab_views_.clear();
        store_dirty_ = false;
        media_dirty_ = false;
        text_dirty_ = false;
        book_dirty_ = false;
        ai_dirty_ = false;
        settings_dirty_ = false;
        nav_entries_.clear();
        group_headers_.clear();
        alt_shortcut_map_.clear();
        root_box_->clearViews();
        store_view_ = nullptr;
        media_view_ = nullptr;
        text_view_ = nullptr;
        book_view_ = nullptr;
        ai_view_ = nullptr;
        settings_view_ = nullptr;
        dock_view_ = nullptr;
        chrome_bottom_box_ = nullptr;
        mini_player_bar_ = nullptr;
        job_bar_box_ = nullptr;
        fault_banner_box_ = nullptr;
        fault_banner_label_ = nullptr;
        ai_route_pill_ = nullptr;
        auth_status_pill_ = nullptr;
        curation_status_pill_ = nullptr;

        populateRootBox();
        rebuildRail();
        selectWorkspace(cur, cid);
        setCompanionDomainContext(saved_dock_domain, saved_dock_summary);
        if (was_dock_open) {
            if (auto* dock = ensureCompanionDockCreated()) {
                if (!dock->isDockedOpen()) {
                    dock->toggleVisible();
                }
            }
        }
        refreshAiPill();
        refreshAuthPill();
        refreshCurationPill();
        updateResponsiveDock();
        DesktopInputController::markDirty(root_box_);
    });
}

void WorkspaceShellActivity::refreshAiPill() {
    if (!ai_route_pill_) return;
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    auto provs = caps.providersFor("ai.agent.v1");
    if (provs.empty()) {
        provs = caps.providersFor("llm.chat.v1");
    }
    if (!provs.empty()) {
        ai_route_pill_->setText("AI 已就绪: " + provs.front().plugin_id);
        ai_route_pill_->setTextColor(ui::color("status/ok_fg"));
    } else {
        ai_route_pill_->setText("AI 路由: 待接入插件");
        ai_route_pill_->setTextColor(ui::color("font/grey"));
    }
}

void WorkspaceShellActivity::refreshAuthPill() {
    if (!auth_status_pill_) return;
    auto& mgr = AuthCenterManager::instance();
    std::string active_pid;
    if (mgr.isBrowserLoginRunning(&active_pid, nullptr)) {
        auth_status_pill_->setText("账号授权: 浏览器登录中…");
        auth_status_pill_->setTextColor(ui::color("brls/accent"));
        return;
    }
    const auto counts = mgr.summarizeCounts(mgr.inspectAll());
    std::string txt = "账号授权: " + std::to_string(counts.authenticated_count) + "/" +
                      std::to_string(counts.total_auth_plugins) + " 已就绪";
    if (counts.guest_cookie_warning_count > 0) {
        txt += " · ⚠" + std::to_string(counts.guest_cookie_warning_count);
        auth_status_pill_->setText(txt);
        auth_status_pill_->setTextColor(ui::color("status/warn_fg"));
    } else if (counts.missing_required_count > 0) {
        txt += " · 待配" + std::to_string(counts.missing_required_count);
        auth_status_pill_->setText(txt);
        auth_status_pill_->setTextColor(ui::color("status/info_fg"));
    } else {
        auth_status_pill_->setText(txt);
        auth_status_pill_->setTextColor(ui::color("status/ok_fg"));
    }
}

void WorkspaceShellActivity::updateResponsiveDock() {
    if (!main_split_ || !dock_view_ || immersive_fullscreen_) return;
    const float w = brls::Application::contentWidth > 0
                        ? brls::Application::contentWidth
                        : brls::Application::windowWidth;
    const float h = brls::Application::contentHeight > 0
                        ? brls::Application::contentHeight
                        : brls::Application::windowHeight;
    if (w <= 1.0f || h <= 1.0f) return;
    const bool narrow = (w / h) < 1.45f;
    main_split_->setAxis(narrow ? brls::Axis::COLUMN : brls::Axis::ROW);
    dock_view_->applyResponsiveMode(w, h);
}

bool WorkspaceShellActivity::cancelActiveWork() {
    if (immersive_fullscreen_) {
        setImmersiveFullscreen(false);
        return true;
    }
    if (ai_view_ && ai_view_->cancelActiveStream()) {
        brls::Application::notify("已中断 AI 流式回答");
        return true;
    }
    if (dock_view_ && dock_view_->cancelActiveStream()) {
        brls::Application::notify("已中断 AI 流式回答");
        return true;
    }
    if (active_workspace_ == WorkspaceId::Media && media_view_ && media_view_->cancelActiveFetch()) {
        brls::Application::notify("已取消媒体异步加载");
        return true;
    }
    return false;
}

bool WorkspaceShellActivity::skipSponsorSegment() {
    if (media_view_ && media_view_->skipSponsorIfActive()) {
        brls::Application::notify("已跳过当前 SponsorBlock / 广告片段");
        return true;
    }
    if (MPVCore::instance().skipActiveAdSegment()) {
        brls::Application::notify("已跳过当前广告片段");
        return true;
    }
    return false;
}

void WorkspaceShellActivity::refreshCurationPill(const nlohmann::json& st_in) {
    if (!curation_status_pill_) return;
    nlohmann::json st = st_in.is_object() && !st_in.empty()
                            ? st_in
                            : CurationSchedulerManager::instance().latestStatus();
    if (!st.is_object() || st.empty()) {
        curation_status_pill_->setText("定时推荐: 点击配置");
        curation_status_pill_->setTextColor(ui::color("font/grey"));
        return;
    }
    const bool running = st.value("running", false);
    const bool auto_on = st.value("auto_run_enabled", true);
    const int done = st.value("published_count", 0);
    const int total = st.value("total_workflows", 4);
    const int missing = st.value("missing_enabled_count", 0);
    const int failed = st.value("failed_count", 0);
    if (running) {
        const std::string proj = st.value("active_project", "");
        curation_status_pill_->setText("定时推荐: 运行中 (" + proj + ")");
        curation_status_pill_->setTextColor(ui::color("domain/core"));
    } else if (failed > 0) {
        curation_status_pill_->setText("定时推荐: " + std::to_string(done) + "/" +
                                       std::to_string(total) + " · 有失败");
        curation_status_pill_->setTextColor(ui::color("status/err_fg"));
    } else if (missing == 0) {
        curation_status_pill_->setText("定时推荐: 今日 " + std::to_string(done) + "/" +
                                       std::to_string(total) + " ✓");
        curation_status_pill_->setTextColor(ui::color("status/ok_fg"));
    } else if (!auto_on) {
        curation_status_pill_->setText("定时推荐: 自动关 · 待跑 " + std::to_string(missing));
        curation_status_pill_->setTextColor(ui::color("status/warn_fg"));
    } else {
        curation_status_pill_->setText("定时推荐: 待补跑 " + std::to_string(missing) + " 项");
        curation_status_pill_->setTextColor(ui::color("status/warn_fg"));
    }
}

void WorkspaceShellActivity::refreshJobBar() {
    if (!job_bar_box_) {
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        job_bar_box_->setVisibility(brls::Visibility::GONE);
        return;
    }

    auto jobs = kernel.jobs().listJobs(true);
    job_bar_box_->clearViews();
    if (jobs.empty()) {
        job_bar_box_->setVisibility(brls::Visibility::GONE);
        job_bar_box_->invalidate();
        return;
    }

    // Prioritize an active (Running/Queued) job, otherwise show the latest finished one.
    JobInfo featured = jobs.back();
    for (auto it = jobs.rbegin(); it != jobs.rend(); ++it) {
        if (it->state == JobState::Running || it->state == JobState::Queued) {
            featured = *it;
            break;
        }
    }

    auto* left = new brls::Box(brls::Axis::ROW);
    left->setAlignItems(brls::AlignItems::CENTER);
    left->setGrow(1.0f);

    const char* bg_tok = "status/info_bg";
    const char* fg_tok = "status/info_fg";
    std::string state_txt = "排队中";
    const int pct = std::clamp(static_cast<int>(featured.progress * 100.0f + 0.5f), 0, 100);

    switch (featured.state) {
        case JobState::Queued:
            state_txt = "排队中";
            break;
        case JobState::Running:
            state_txt = "进行中 " + std::to_string(pct) + "%";
            break;
        case JobState::Completed:
            bg_tok = "status/ok_bg";
            fg_tok = "status/ok_fg";
            state_txt = "已完成";
            break;
        case JobState::Failed:
            bg_tok = "status/err_bg";
            fg_tok = "status/err_fg";
            state_txt = "失败";
            break;
        case JobState::Cancelled:
            bg_tok = "status/warn_bg";
            fg_tok = "status/warn_fg";
            state_txt = "已取消";
            break;
    }

    auto* pill = ui::makePill(state_txt, bg_tok, fg_tok);
    pill->setMarginRight(10);
    left->addView(pill);

    std::string summary = featured.title;
    if (!featured.stage_message.empty()) {
        summary += " · " + featured.stage_message;
    } else if (featured.state == JobState::Failed && !featured.error.empty()) {
        summary += " · " + featured.error;
    }
    auto* msg_lbl = ui::makeLabel(summary, 14.5f, "brls/text");
    msg_lbl->setGrow(1.0f);
    left->addView(msg_lbl);

    if (jobs.size() > 1) {
        auto* more_pill = ui::makePill("共 " + std::to_string(jobs.size()) + " 个后台任务",
                                       "desktop/soft", "font/grey");
        more_pill->setMarginLeft(10);
        more_pill->setMarginRight(10);
        left->addView(more_pill);
    }

    auto* track = new brls::Box(brls::Axis::ROW);
    track->setWidth(96);
    track->setHeight(6);
    track->setCornerRadius(3.0f);
    track->setBackgroundColor(ui::color("desktop/soft"));
    track->setMarginLeft(10);
    track->setMarginRight(12);
    auto* fill = new brls::Box(brls::Axis::ROW);
    fill->setHeight(6);
    fill->setCornerRadius(3.0f);
    fill->setWidthPercentage(featured.state == JobState::Completed ? 100.0f : static_cast<float>(pct));
    fill->setBackgroundColor(ui::color(fg_tok));
    track->addView(fill);
    left->addView(track);

    job_bar_box_->addView(left);

    auto* actions = new brls::Box(brls::Axis::ROW);
    actions->setAlignItems(brls::AlignItems::CENTER);
    const std::string jid = featured.id;
    if (featured.state == JobState::Queued || featured.state == JobState::Running) {
        actions->addView(ui::makeButton("取消任务", [jid]() {
            if (HostKernel::instance().isInitialized()) {
                HostKernel::instance().jobs().cancel(jid);
            }
        }, false, 34.0f));
    } else {
        if (featured.state == JobState::Completed) {
            const std::string open_lbl = featured.open_target.label.empty()
                                             ? "一键打开"
                                             : featured.open_target.label;
            auto* open_btn = ui::makeButton(open_lbl, [jid]() {
                if (HostKernel::instance().isInitialized()) {
                    HostKernel::instance().jobs().triggerOpen(jid);
                    HostKernel::instance().jobs().dismissFinished(jid);
                }
            }, true, 34.0f);
            open_btn->setMarginRight(8);
            actions->addView(open_btn);
        }
        actions->addView(ui::makeButton("关闭", [jid]() {
            if (HostKernel::instance().isInitialized()) {
                HostKernel::instance().jobs().dismissFinished(jid);
            }
        }, false, 34.0f));
    }
    job_bar_box_->addView(actions);
    job_bar_box_->setVisibility(brls::Visibility::VISIBLE);
    job_bar_box_->invalidate();
    DesktopInputController::markDirty(job_bar_box_);
}

void WorkspaceShellActivity::handleJobOpenEvent(const std::string& topic, const nlohmann::json& payload) {
    std::string domain;
    if (payload.is_object() && payload.contains("domain") && payload["domain"].is_string()) {
        domain = payload["domain"].get<std::string>();
    }
    const std::string title =
        (payload.is_object() && payload.contains("title") && payload["title"].is_string())
            ? payload["title"].get<std::string>()
            : std::string{};
    const bool from_job = payload.is_object() && payload.contains("job_id");

    if (topic == "cmd:curation:calendar") {
        const std::string bdate =
            (payload.is_object() && payload.contains("business_date") && payload["business_date"].is_string())
                ? payload["business_date"].get<std::string>()
                : std::string{};
        CurationCalendarDialog::open({.initial_date = bdate});
        return;
    }
    if (topic == "cmd:curation:scheduler") {
        CurationSchedulerDialog::open();
        return;
    }
    if (topic == "cmd:auth:center") {
        const std::string pid =
            (payload.is_object() && payload.contains("plugin_id") && payload["plugin_id"].is_string())
                ? payload["plugin_id"].get<std::string>()
                : std::string{};
        openAuthCenter(pid);
        return;
    }
    if (topic == "entity.curation.filter_date") {
        const std::string bdate =
            (payload.is_object() && payload.contains("business_date") && payload["business_date"].is_string())
                ? payload["business_date"].get<std::string>()
                : std::string{};
        if (bdate.empty()) return;
        if (domain == "music" || domain == "youtube" || domain == "video") {
            selectWorkspace(WorkspaceId::Media);
            if (media_view_) media_view_->applyDateFilter(bdate, domain);
        } else if (domain == "english") {
            selectWorkspace(WorkspaceId::Book);
            if (book_view_) book_view_->applyDateFilter(bdate);
        } else if (domain == "news" || domain == "reddit" || domain == "journal") {
            selectWorkspace(WorkspaceId::Text);
            if (text_view_) text_view_->applyDateFilter(bdate, domain);
        } else {
            pending_all_date_filter_ = bdate;
            if (media_view_) media_view_->applyDateFilter(bdate, "all");
            if (text_view_) text_view_->applyDateFilter(bdate, "all");
            if (book_view_) book_view_->applyDateFilter(bdate);
            if (active_workspace_ != WorkspaceId::Media &&
                active_workspace_ != WorkspaceId::Text &&
                active_workspace_ != WorkspaceId::Book) {
                selectWorkspace(WorkspaceId::Text);
            }
        }
        brls::Application::notify("已按 " + bdate + " 筛选每日推荐工作台");
        return;
    }

    if (topic == "entity.workspace.return") {
        WorkspaceId landed_ws = WorkspaceId::Text;
        if (has_return_workspace_) {
            const WorkspaceId target_ws = return_workspace_;
            const std::string target_cid = return_custom_tab_id_;
            has_return_workspace_ = false;
            landed_ws = target_ws;
            selectWorkspace(target_ws, target_cid);
        } else {
            selectWorkspace(WorkspaceId::Text);
        }
        if (landed_ws == WorkspaceId::Text && text_view_ && text_view_->isShowingAuthPrompt()) {
            text_view_->refresh(true);
        }
        return;
    }

    if (topic == "entity.workspace.open") {
        const std::string tab_id =
            payload.is_object() ? payload.value("tab_id", "") : std::string{};
        if (!tab_id.empty()) {
            return_workspace_ = active_workspace_;
            return_custom_tab_id_ = active_custom_tab_id_;
            has_return_workspace_ = true;
            selectWorkspace(WorkspaceId::CustomPluginTab, tab_id);
        }
        return;
    }

    if (topic == "entity.web.open" || domain == "web") {
        if (payload.is_object() && payload.value("background", false)) {
            return;
        }
        const std::string tab_id =
            payload.is_object() ? payload.value("tab_id", "chrome_web_reader")
                                : std::string("chrome_web_reader");
        const bool already_in_browser =
            (active_workspace_ == WorkspaceId::CustomPluginTab &&
             active_custom_tab_id_ == tab_id);
        if (!already_in_browser) {
            return_workspace_ = active_workspace_;
            return_custom_tab_id_ = active_custom_tab_id_;
            has_return_workspace_ = true;
            selectWorkspace(WorkspaceId::CustomPluginTab, tab_id);
        }
        if (!title.empty()) {
            setCompanionDomainContext(ContentDomain::Text, title);
            brls::Application::notify("已在内嵌 Chrome 打开: " + title);
        }
        return;
    }

    if (topic == "entity.book.open" || domain == "book" || domain == "books") {
        selectWorkspace(WorkspaceId::Book);
        if (book_view_) {
            book_view_->openExternalBook(payload);
        }
        if (!title.empty()) {
            setCompanionDomainContext(ContentDomain::Book, title);
        }
    } else if (payload.contains("sub_mode") && payload.value("sub_mode", "") == "vocab") {
        selectWorkspace(WorkspaceId::Book);
        if (book_view_) {
            book_view_->openExternalBook(payload);
        }
    } else if (topic == "entity.text.open" || domain == "text" || domain == "journal" || domain == "journals") {
        selectWorkspace(WorkspaceId::Text);
        if (text_view_) {
            text_view_->openExternalArticle(payload);
        }
        if (!title.empty()) {
            setCompanionDomainContext(ContentDomain::Text, title);
        }
    } else if (topic == "entity.media.open" || domain == "media" || domain == "video" ||
               domain == "music" || domain == "nas") {
        selectWorkspace(WorkspaceId::Media);
        if (media_view_) {
            media_view_->openExternalMedia(payload);
        }
        if (!title.empty()) {
            setCompanionDomainContext(ContentDomain::Media, title);
        }
    }
    if (!title.empty()) {
        brls::Application::notify(
            (from_job ? "后台任务完成，已定位结果: " : "已跳转至: ") + title);
    }
}

void WorkspaceShellActivity::showWarningBanner(const std::string& message,
                                               const std::string& topic) {
    if (!fault_banner_box_ || !fault_banner_label_ || message.empty()) {
        return;
    }
    std::string clean;
    clean.reserve(message.size());
    for (char ch : message) {
        clean.push_back((ch == '\r' || ch == '\n') ? ' ' : ch);
    }
    if (clean.size() > 150) {
        size_t cut = 146;
        while (cut > 0 && (static_cast<unsigned char>(clean[cut]) & 0xC0) == 0x80) {
            --cut;
        }
        clean = clean.substr(0, cut) + "...";
    }
    active_warning_topic_ = topic;
    fault_banner_label_->setText(clean);
    fault_banner_box_->setVisibility(brls::Visibility::VISIBLE);
    fault_banner_box_->invalidate();
    DesktopInputController::markDirty(fault_banner_box_);
}

void WorkspaceShellActivity::dismissWarningBanner() {
    if (!fault_banner_box_) {
        return;
    }
    active_warning_topic_.clear();
    fault_banner_box_->setVisibility(brls::Visibility::GONE);
    fault_banner_box_->invalidate();
    DesktopInputController::markDirty(chrome_bottom_box_);
}

} // namespace omnidesk::host
