#include "omnidesk/host/shell_activity.hpp"

#include <algorithm>
#include "omnidesk/host/book_workbench.hpp"
#include "omnidesk/host/companion_dock.hpp"
#include "omnidesk/host/desktop_ui.hpp"
#include "omnidesk/host/host_settings_view.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/media_workbench.hpp"
#include "omnidesk/host/plugin_store_view.hpp"
#include "omnidesk/host/svg_view.hpp"
#include "omnidesk/host/text_workbench.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/mpv_core.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {

WorkspaceShellActivity::WorkspaceShellActivity(WorkspaceId initial_workspace)
    : active_workspace_(initial_workspace) {}

WorkspaceShellActivity::~WorkspaceShellActivity() {
    alive_->store(false, std::memory_order_release);
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
    return root_box_;
}

void WorkspaceShellActivity::populateRootBox() {
    root_box_->setBackgroundColor(ui::color("brls/background"));

    // Left Navigation Rail
    rail_box_ = new brls::Box(brls::Axis::COLUMN);
    rail_box_->setWidth(236);
    rail_box_->setHeightPercentage(100);
    rail_box_->setPadding(18, 14, 16, 14);
    rail_box_->setBackgroundColor(ui::color("desktop/surface"));
    rail_box_->setBorderThickness(1);
    rail_box_->setBorderColor(ui::color("desktop/line"));
    rail_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

    auto* top_group = new brls::Box(brls::Axis::COLUMN);

    // Brand Header
    auto* brand = new brls::Box(brls::Axis::ROW);
    brand->setAlignItems(brls::AlignItems::CENTER);
    brand->setMarginBottom(14);
    auto* logo = new SvgIconView(HostKernel::instance().assetPath("svg/omnidesk_logo.svg"));
    logo->setDimensions(28, 28);
    logo->setMarginRight(10);
    brand->addView(logo);

    auto* brand_text = new brls::Box(brls::Axis::COLUMN);
    brand_text->addView(ui::makeLabel("OmniDesk", 19.0f, "brls/text"));
    brand_text->addView(ui::makeLabel("Plugin Workbench", 12.5f, "font/grey"));
    brand->addView(brand_text);
    top_group->addView(brand);

    top_group->addView(ui::makeDivider(6.0f));

    built_in_nav_box_ = new brls::Box(brls::Axis::COLUMN);
    top_group->addView(built_in_nav_box_);

    custom_nav_box_ = new brls::Box(brls::Axis::COLUMN);
    custom_nav_box_->setMarginTop(8);
    top_group->addView(custom_nav_box_);
    rail_box_->addView(top_group);

    // Bottom Status & Utilities
    auto* bottom_group = new brls::Box(brls::Axis::COLUMN);
    bottom_group->addView(ui::makeDivider(8.0f));

    auto* ai_card = ui::makeCard("desktop/soft", 10.0f, 8.0f);
    ai_card->setMarginBottom(8);
    ai_card->setFocusable(true);
    ai_route_pill_ = ui::makeLabel("AI 路由: 待接入插件", 13.0f, "font/grey");
    ai_card->addView(ai_route_pill_);
    ai_card->registerClickAction([this](brls::View*) {
        brls::sync([this]() { toggleCompanionDock(); });
        return true;
    });
    bottom_group->addView(ai_card);

    auto* util_row = new brls::Box(brls::Axis::ROW);
    util_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    util_row->setMarginBottom(8);
    auto* dock_btn = ui::makeButton("F8 协作", [this]() { toggleCompanionDock(); }, false, 34.0f);
    dock_btn->setGrow(1.0f);
    dock_btn->setMarginRight(6);
    util_row->addView(dock_btn);

    auto* theme_btn = ui::makeButton("主题 (t)", [this]() { toggleTheme(); }, false, 34.0f);
    theme_btn->setGrow(1.0f);
    util_row->addView(theme_btn);
    bottom_group->addView(util_row);

    auto* settings_btn = ui::makeButton("宿主设置 & 快捷键 (?)", [this]() {
        brls::sync([this]() { selectWorkspace(WorkspaceId::Settings); });
    }, false, 34.0f);
    bottom_group->addView(settings_btn);
    rail_box_->addView(bottom_group);

    root_box_->addView(rail_box_);

    // Main Split (Workspace Stage + Responsive F8 Companion Dock)
    main_split_ = new brls::Box(brls::Axis::ROW);
    main_split_->setGrow(1.0f);
    main_split_->setHeightPercentage(100);

    stage_container_ = new brls::Box(brls::Axis::COLUMN);
    stage_container_->setGrow(1.0f);
    stage_container_->setHeightPercentage(100);

    views_host_ = new brls::Box(brls::Axis::COLUMN);
    views_host_->setGrow(1.0f);
    views_host_->setWidthPercentage(100);

    store_view_ = new PluginStoreView();
    media_view_ = new MediaWorkbench(
        [this](const std::string& summary) {
            if (dock_view_) dock_view_->setDomainContext(ContentDomain::Media, summary);
        },
        [this]() { selectWorkspace(WorkspaceId::Store); });
    text_view_ = new TextWorkbench(
        [this](const std::string& summary) {
            if (dock_view_) dock_view_->setDomainContext(ContentDomain::Text, summary);
        },
        [this]() { selectWorkspace(WorkspaceId::Store); });
    book_view_ = new BookWorkbench(
        [this](const std::string& summary) {
            if (dock_view_) dock_view_->setDomainContext(ContentDomain::Book, summary);
        },
        [this]() { selectWorkspace(WorkspaceId::Store); });
    settings_view_ = new HostSettingsView(
        [this]() { toggleTheme(); },
        [this]() { selectWorkspace(WorkspaceId::Store); });

    views_host_->addView(store_view_);
    views_host_->addView(media_view_);
    views_host_->addView(text_view_);
    views_host_->addView(book_view_);
    views_host_->addView(settings_view_);

    job_bar_box_ = new brls::Box(brls::Axis::ROW);
    job_bar_box_->setWidthPercentage(100);
    job_bar_box_->setPadding(8, 18, 8, 18);
    job_bar_box_->setBackgroundColor(ui::color("desktop/surface"));
    job_bar_box_->setAlignItems(brls::AlignItems::CENTER);
    job_bar_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    job_bar_box_->setVisibility(brls::Visibility::GONE);

    fault_banner_box_ = new brls::Box(brls::Axis::ROW);
    fault_banner_box_->setBackgroundColor(ui::color("status/warn_bg"));
    fault_banner_box_->setCornerRadius(10.0f);
    fault_banner_box_->setPadding(6.0f, 12.0f, 6.0f, 12.0f);
    fault_banner_box_->setMargins(4.0f, 12.0f, 4.0f, 12.0f);
    fault_banner_box_->setAlignItems(brls::AlignItems::CENTER);
    fault_banner_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    fault_banner_box_->setVisibility(brls::Visibility::GONE);

    fault_banner_label_ = ui::makeLabel("", 13.5f, "status/warn_fg", 1.2f);
    fault_banner_label_->setSingleLine(true);
    fault_banner_label_->setGrow(1.0f);
    fault_banner_label_->setMarginRight(10.0f);
    fault_banner_box_->addView(fault_banner_label_);

    auto* banner_actions = new brls::Box(brls::Axis::ROW);
    banner_actions->setAlignItems(brls::AlignItems::CENTER);
    auto* go_store_btn = ui::makeButton("前往插件市场", [this]() {
        selectWorkspace(WorkspaceId::Store);
    }, false, 28.0f);
    go_store_btn->setMarginRight(6.0f);
    banner_actions->addView(go_store_btn);
    banner_actions->addView(ui::makeButton("✕", [this]() {
        dismissWarningBanner();
    }, false, 28.0f));
    fault_banner_box_->addView(banner_actions);

    stage_container_->addView(views_host_);
    stage_container_->addView(fault_banner_box_);
    stage_container_->addView(job_bar_box_);

    main_split_->addView(stage_container_);

    dock_view_ = new AiCompanionDock([this]() { selectWorkspace(WorkspaceId::Store); });
    main_split_->addView(dock_view_);

    root_box_->addView(main_split_);
    refreshJobBar();
}

void WorkspaceShellActivity::onContentAvailable() {
    rebuildRail();
    selectWorkspace(active_workspace_);
    refreshAiPill();
    updateResponsiveDock();
    refreshJobBar();

    DesktopInputController::install({
        .on_select_workspace = [this](int idx) { selectWorkspaceByIndex(idx); },
        .on_cycle_workspace = [this](int delta) { cycleWorkspace(delta); },
        .on_toggle_dock = [this]() { toggleCompanionDock(); },
        .on_search = [this]() { triggerSearch(); },
        .on_toggle_theme = [this]() { toggleTheme(); },
        .on_cancel_active = [this]() { return cancelActiveWork(); },
        .on_skip_ad = [this]() { return skipSponsorSegment(); },
    });

    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        dom_sub_token_ = kernel.domains().subscribeChanges([this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                rebuildPluginTabs();
                if (media_view_) media_view_->refresh();
                if (text_view_) text_view_->refresh();
                if (book_view_) book_view_->refresh();
                if (dock_view_) dock_view_->refreshAiRouteStatus();
                refreshAiPill();
            });
        });
        bus_sub_token_ = kernel.bus().subscribe(
            "entity.ai.route_changed", "host.shell",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    refreshAiPill();
                    if (dock_view_) dock_view_->refreshAiRouteStatus();
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
             {"entity.book.open", "entity.text.open", "entity.media.open", "job.open_requested"}) {
            uint64_t tok = kernel.bus().subscribe(
                topic, "host.shell.job_open",
                [this, alive = alive_](const std::string& ev_topic, const nlohmann::json& payload) {
                    if (!payload.is_object()) {
                        return;
                    }
                    if (payload.value("origin", "") == "host_workbench") {
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
                    if (!k.installer().hasStaleInstalledPlugins(false)) {
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
        for (const auto& entry : kernel.installer().checkInstalledAbiStatus(false)) {
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

    resize_sub_ = brls::Application::getWindowSizeChangedEvent()->subscribe([this]() {
        updateResponsiveDock();
    });
}

brls::Box* WorkspaceShellActivity::makeRailButton(const std::string& label,
                                                  const std::string& svg_rel,
                                                  const std::string& shortcut_hint,
                                                  bool active,
                                                  std::function<void()> on_click) {
    auto* item = new brls::Box(brls::Axis::ROW);
    item->setWidthPercentage(100);
    item->setHeight(44);
    item->setPadding(0, 12, 0, 12);
    item->setMarginBottom(6);
    item->setCornerRadius(10);
    item->setFocusable(true);
    item->setHighlightCornerRadius(10);
    item->setAlignItems(brls::AlignItems::CENTER);
    item->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    item->setBackgroundColor(ui::color(active ? "desktop/active" : "desktop/surface"));
    if (active) {
        item->setBorderThickness(1);
        item->setBorderColor(ui::color("brls/accent"));
    }

    auto* left = new brls::Box(brls::Axis::ROW);
    left->setAlignItems(brls::AlignItems::CENTER);
    if (!svg_rel.empty()) {
        auto* icon = new SvgIconView(HostKernel::instance().assetPath(svg_rel));
        icon->setDimensions(19, 19);
        icon->setMarginRight(10);
        left->addView(icon);
    }
    left->addView(ui::makeLabel(label, 16.0f, active ? "brls/accent" : "brls/text"));
    item->addView(left);

    if (!shortcut_hint.empty()) {
        item->addView(ui::makeLabel(shortcut_hint, 12.0f, "font/grey"));
    }

    item->registerClickAction([fn = std::move(on_click)](brls::View*) {
        brls::sync(fn);
        return true;
    });
    return item;
}

void WorkspaceShellActivity::rebuildRail() {
    built_in_nav_box_->clearViews();
    nav_entries_.clear();

    struct BaseNav {
        WorkspaceId id;
        const char* label;
        const char* svg;
        const char* shortcut;
        ContentDomain domain;
    };
    const BaseNav items[] = {
        {WorkspaceId::Store, "插件市场", "svg/nav_store.svg", "Ctrl+1", ContentDomain::Core},
        {WorkspaceId::Media, "影视与音乐", "svg/nav_media.svg", "Ctrl+2", ContentDomain::Media},
        {WorkspaceId::Text, "文字与期刊", "svg/nav_text.svg", "Ctrl+3", ContentDomain::Text},
        {WorkspaceId::Book, "书本与 PDF", "svg/nav_book.svg", "Ctrl+4", ContentDomain::Book},
    };

    for (const auto& it : items) {
        const bool active = (active_workspace_ == it.id);
        auto* btn = makeRailButton(it.label, it.svg, it.shortcut, active, [this, id = it.id]() {
            selectWorkspace(id);
        });
        built_in_nav_box_->addView(btn);
        nav_entries_.push_back({it.id, "", it.label, it.domain, btn});
    }

    rebuildPluginTabs();
}

void WorkspaceShellActivity::rebuildPluginTabs() {
    custom_nav_box_->clearViews();
    nav_entries_.resize(std::min<size_t>(nav_entries_.size(), 4));

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    const auto tabs = kernel.domains().workspaceTabs();
    if (tabs.empty()) return;

    auto* hdr = ui::makeLabel("插件工作区扩展", 12.5f, "font/grey");
    hdr->setMarginBottom(6);
    hdr->setMarginLeft(4);
    custom_nav_box_->addView(hdr);

    int slot = 5;
    for (const auto& t : tabs) {
        const bool active =
            (active_workspace_ == WorkspaceId::CustomPluginTab && active_custom_tab_id_ == t.tab_id);
        std::string hint = slot <= 9 ? ("Ctrl+" + std::to_string(slot)) : "";
        auto* btn = makeRailButton(t.title, "", hint, active, [this, tid = t.tab_id]() {
            selectWorkspace(WorkspaceId::CustomPluginTab, tid);
        });
        custom_nav_box_->addView(btn);
        nav_entries_.push_back({WorkspaceId::CustomPluginTab, t.tab_id, t.title, t.domain, btn});
        ++slot;
    }
}

void WorkspaceShellActivity::selectWorkspace(WorkspaceId id, const std::string& custom_tab_id) {
    active_workspace_ = id;
    active_custom_tab_id_ = custom_tab_id;

    if (active_custom_view_ && views_host_) {
        views_host_->removeView(active_custom_view_, true);
        active_custom_view_ = nullptr;
    }

    store_view_->setVisibility(id == WorkspaceId::Store ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    media_view_->setVisibility(id == WorkspaceId::Media ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    text_view_->setVisibility(id == WorkspaceId::Text ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    book_view_->setVisibility(id == WorkspaceId::Book ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    settings_view_->setVisibility(id == WorkspaceId::Settings ? brls::Visibility::VISIBLE : brls::Visibility::GONE);

    ContentDomain dock_dom = ContentDomain::Core;
    if (id == WorkspaceId::Media) dock_dom = ContentDomain::Media;
    else if (id == WorkspaceId::Text) dock_dom = ContentDomain::Text;
    else if (id == WorkspaceId::Book) dock_dom = ContentDomain::Book;

    if (id == WorkspaceId::CustomPluginTab) {
        for (const auto& tab : HostKernel::instance().domains().workspaceTabs()) {
            if (tab.tab_id == custom_tab_id && tab.create_view) {
                active_custom_view_ = tab.create_view();
                if (active_custom_view_ && views_host_) {
                    views_host_->addView(active_custom_view_);
                }
                dock_dom = tab.domain;
                break;
            }
        }
    } else if (id == WorkspaceId::Store) {
        store_view_->refresh();
    } else if (id == WorkspaceId::Media) {
        media_view_->refresh();
    } else if (id == WorkspaceId::Text) {
        text_view_->refresh();
    } else if (id == WorkspaceId::Book) {
        book_view_->refresh();
    } else if (id == WorkspaceId::Settings) {
        settings_view_->refresh();
    }

    if (dock_view_) {
        dock_view_->setDomainContext(dock_dom);
    }
    rebuildRail();
}

void WorkspaceShellActivity::selectWorkspaceByIndex(int zero_based_index) {
    if (zero_based_index < 0 || static_cast<size_t>(zero_based_index) >= nav_entries_.size()) {
        return;
    }
    const auto& target = nav_entries_[static_cast<size_t>(zero_based_index)];
    selectWorkspace(target.id, target.custom_tab_id);
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
    if (!dock_view_) return;
    dock_view_->toggleVisible();
    updateResponsiveDock();
}

void WorkspaceShellActivity::triggerSearch() {
    switch (active_workspace_) {
        case WorkspaceId::Store: if (store_view_) store_view_->promptSearch(); break;
        case WorkspaceId::Media: if (media_view_) media_view_->promptSearch(); break;
        case WorkspaceId::Text: if (text_view_) text_view_->promptSearch(); break;
        case WorkspaceId::Book: if (book_view_) book_view_->promptSearch(); break;
        default: break;
    }
}

void WorkspaceShellActivity::toggleTheme() {
    auto& kernel = HostKernel::instance();
    kernel.toggleTheme();
    brls::Application::notify(kernel.isDarkTheme() ? "已切换 Graphite 暗色主题" : "已切换 Paper 亮色主题");
    const WorkspaceId cur = active_workspace_;
    const std::string cid = active_custom_tab_id_;
    brls::sync([this, cur, cid]() {
        if (!root_box_) return;
        active_custom_view_ = nullptr;
        nav_entries_.clear();
        root_box_->clearViews();
        populateRootBox();
        selectWorkspace(cur, cid);
        refreshAiPill();
        updateResponsiveDock();
    });
}

void WorkspaceShellActivity::refreshAiPill() {
    if (!ai_route_pill_) return;
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    if (caps.has("ai.agent.v1") || caps.has("llm.chat.v1")) {
        auto provs = caps.providersFor(caps.has("ai.agent.v1") ? "ai.agent.v1" : "llm.chat.v1");
        ai_route_pill_->setText("AI 已就绪: " + provs.front().plugin_id);
        ai_route_pill_->setTextColor(ui::color("status/ok_fg"));
    } else {
        ai_route_pill_->setText("AI 路由: 待接入插件");
        ai_route_pill_->setTextColor(ui::color("font/grey"));
    }
}

void WorkspaceShellActivity::updateResponsiveDock() {
    if (!main_split_ || !dock_view_) return;
    const float w = brls::Application::windowWidth;
    const float h = brls::Application::windowHeight;
    if (w <= 1.0f || h <= 1.0f) return;
    const bool narrow = (w / h) < 1.45f;
    main_split_->setAxis(narrow ? brls::Axis::COLUMN : brls::Axis::ROW);
    dock_view_->applyResponsiveMode(w, h);
}

bool WorkspaceShellActivity::cancelActiveWork() {
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
    auto* msg_lbl = ui::makeLabel(summary, 14.0f, "brls/text");
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
        }, false, 30.0f));
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
            }, true, 30.0f);
            open_btn->setMarginRight(8);
            actions->addView(open_btn);
        }
        actions->addView(ui::makeButton("关闭", [jid]() {
            if (HostKernel::instance().isInitialized()) {
                HostKernel::instance().jobs().dismissFinished(jid);
            }
        }, false, 30.0f));
    }
    job_bar_box_->addView(actions);
    job_bar_box_->setVisibility(brls::Visibility::VISIBLE);
    job_bar_box_->invalidate();
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

    if (topic == "entity.book.open" || domain == "book" || domain == "books") {
        selectWorkspace(WorkspaceId::Book);
        if (book_view_) {
            book_view_->openExternalBook(payload);
        }
        if (dock_view_ && !title.empty()) {
            dock_view_->setDomainContext(ContentDomain::Book, title);
        }
    } else if (topic == "entity.text.open" || domain == "text" || domain == "journal" || domain == "journals") {
        selectWorkspace(WorkspaceId::Text);
        if (text_view_) {
            text_view_->openExternalArticle(payload);
        }
        if (dock_view_ && !title.empty()) {
            dock_view_->setDomainContext(ContentDomain::Text, title);
        }
    } else if (topic == "entity.media.open" || domain == "media" || domain == "video" ||
               domain == "music" || domain == "nas") {
        selectWorkspace(WorkspaceId::Media);
        if (media_view_) {
            media_view_->openExternalMedia(payload);
        }
        if (dock_view_ && !title.empty()) {
            dock_view_->setDomainContext(ContentDomain::Media, title);
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
}

void WorkspaceShellActivity::dismissWarningBanner() {
    if (!fault_banner_box_) {
        return;
    }
    active_warning_topic_.clear();
    fault_banner_box_->setVisibility(brls::Visibility::GONE);
    fault_banner_box_->invalidate();
}

} // namespace omnidesk::host
