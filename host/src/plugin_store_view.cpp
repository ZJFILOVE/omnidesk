#include "omnidesk/host/plugin_store_view.hpp"

#include <algorithm>
#include <thread>
#include "omnidesk/host/auth_center_dialog.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/ui_comfort.hpp"
#include "omnidesk/pm/scaffold.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

bool focusIsInsideView(brls::View* ancestor) {
    if (!ancestor) return false;
    brls::View* cur = brls::Application::getCurrentFocus();
    while (cur) {
        if (cur == ancestor) return true;
        cur = cur->getParent();
    }
    return false;
}

void clearBoxViewsSafely(brls::Box* box) {
    if (!box) return;
    if (focusIsInsideView(box)) {
        brls::Application::giveFocus(nullptr);
    }
    box->clearViews();
}

std::string trimAscii(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && (s[b] == ' ' || s[b] == '\t' || s[b] == '\r' || s[b] == '\n')) {
        ++b;
    }
    size_t e = s.size();
    while (e > b && (s[e - 1] == ' ' || s[e - 1] == '\t' || s[e - 1] == '\r' || s[e - 1] == '\n')) {
        --e;
    }
    return s.substr(b, e - b);
}

std::string truncateUtf8Display(const std::string& s, size_t max_codepoints) {
    if (s.empty() || max_codepoints == 0) return s;
    size_t cp = 0;
    size_t i = 0;
    while (i < s.size()) {
        if (cp >= max_codepoints) {
            return s.substr(0, i) + "…";
        }
        const unsigned char c = static_cast<unsigned char>(s[i]);
        size_t adv = 1;
        if ((c & 0x80u) == 0) adv = 1;
        else if ((c & 0xE0u) == 0xC0u) adv = 2;
        else if ((c & 0xF0u) == 0xE0u) adv = 3;
        else if ((c & 0xF8u) == 0xF0u) adv = 4;
        if (i + adv > s.size()) break;
        i += adv;
        ++cp;
    }
    return s;
}

std::optional<WorkspaceTabSpec> findCustomTabForPlugin(const std::string& plugin_id) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return std::nullopt;
    for (const auto& tab : kernel.domains().workspaceTabs()) {
        if (tab.plugin_id == plugin_id && !tab.tab_id.empty()) {
            return tab;
        }
    }
    return std::nullopt;
}

std::string domainWorkbenchButtonTitle(ContentDomain d) {
    switch (d) {
        case ContentDomain::Media: return "打开影视与音乐工作台 ▸";
        case ContentDomain::Text: return "打开文字与期刊工作台 ▸";
        case ContentDomain::Book: return "打开书本与 PDF 工作台 ▸";
        case ContentDomain::Core: return "打开 AI 协作栏 (F8) ▸";
    }
    return "打开工作台 ▸";
}

std::string joinStrings(const std::vector<std::string>& v, const std::string& sep) {
    std::string out;
    for (size_t i = 0; i < v.size(); ++i) {
        if (i > 0) out += sep;
        out += v[i];
    }
    return out;
}

std::string domainColorToken(ContentDomain d) {
    switch (d) {
        case ContentDomain::Core: return "domain/core";
        case ContentDomain::Media: return "domain/media";
        case ContentDomain::Text: return "domain/text";
        case ContentDomain::Book: return "domain/book";
    }
    return "domain/core";
}

brls::Box* makeBadgeRow(const std::vector<std::string>& items,
                        const std::string& bg_token,
                        const std::string& fg_token,
                        const std::string& prefix = "",
                        size_t max_items = 4) {
    auto* row = new brls::Box(brls::Axis::ROW);
    row->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(row, 4.0f);
    for (size_t i = 0; i < items.size() && i < max_items; ++i) {
        auto* pill = ui::makePill(prefix + items[i], bg_token, fg_token);
        pill->setMarginRight(6);
        row->addView(pill);
    }
    if (items.size() > max_items) {
        auto* more = ui::makePill("+" + std::to_string(items.size() - max_items), bg_token, fg_token);
        row->addView(more);
    }
    return row;
}

} // namespace

PluginStoreView::PluginStoreView(OpenCustomTabCallback on_open_custom_tab,
                                 OpenDomainCallback on_open_domain,
                                 OpenAuthCenterCallback on_open_auth_center)
    : brls::Box(brls::Axis::COLUMN),
      on_open_custom_tab_(std::move(on_open_custom_tab)),
      on_open_domain_(std::move(on_open_domain)),
      on_open_auth_center_(std::move(on_open_auth_center)) {
    setGrow(1.0f);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    // Top Header Row
    auto* header = new brls::Box(brls::Axis::ROW);
    header->setId("omnidesk/pane_header");
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(12);

    auto* title_col = new brls::Box(brls::Axis::COLUMN);
    title_col->setShrink(1.0f);
    auto* title = ui::makeLabel("插件市场 · Plugin Marketplace", 24.0f, "brls/text");
    title->setMarginBottom(3);
    summary_label_ = ui::makeLabel("独立目录 & GitHub 仓库构建 · ABI v1 热插拔底座", 14.5f, "font/grey");
    title_col->addView(title);
    title_col->addView(summary_label_);
    header->addView(title_col);

    auto* actions = new brls::Box(brls::Axis::ROW);
    actions->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(actions, 6.0f);

    auto* auth_hub_btn = ui::makeButton("账号授权中心", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (on_open_auth_center_) {
            on_open_auth_center_(selected_plugin_id_);
        } else {
            AuthCenterDialog::open({}, selected_plugin_id_);
        }
    }, true, 35.0f);
    auth_hub_btn->setMarginRight(6);
    actions->addView(auth_hub_btn);

    auto* search_btn = ui::makeButton("搜索 /", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptSearch();
    }, false, 35.0f);
    search_btn->setMarginRight(6);
    actions->addView(search_btn);

    auto* radar_btn = ui::makeButton("更新雷达", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) startUpdateRadar();
    }, false, 35.0f);
    radar_btn->setMarginRight(6);
    actions->addView(radar_btn);

    rebuild_stale_btn_ = ui::makeButton("一键重编过期", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && !busy_) {
            startBatchRebuild(true);
        }
    }, false, 35.0f);
    rebuild_stale_btn_->setMarginRight(6);
    rebuild_stale_btn_->setVisibility(brls::Visibility::GONE);
    actions->addView(rebuild_stale_btn_);

    auto* gh_btn = ui::makeButton("+ GitHub 安装", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) openInstallModal(false);
    }, false, 35.0f);
    gh_btn->setMarginRight(6);
    actions->addView(gh_btn);

    auto* local_btn = ui::makeButton("+ 本地源码安装", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) openInstallModal(true);
    }, false, 35.0f);
    local_btn->setMarginRight(6);
    actions->addView(local_btn);

    auto* new_btn = ui::makeButton("+ 脚手架", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) openScaffoldModal();
    }, false, 35.0f);
    actions->addView(new_btn);

    header->setId("omnidesk/pane_header");
    header->addView(actions);
    addView(header);

    // Filter Bar
    filter_bar_ = new brls::Box(brls::Axis::ROW);
    filter_bar_->setId("omnidesk/pane_filters");
    filter_bar_->setAlignItems(brls::AlignItems::CENTER);
    filter_bar_->setMarginBottom(12);
    ui_comfort::enableFlexWrap(filter_bar_, 6.0f);
    addView(filter_bar_);

    // Master-Detail Split Body
    auto* split = new brls::Box(brls::Axis::ROW);
    split->setGrow(1.0f);
    split->setShrink(1.0f);
    split->setMinHeight(0);
    split->setWidthPercentage(100);

    list_scroll_ = new brls::ScrollingFrame();
    list_scroll_->setId("omnidesk/pane_primary");
    list_scroll_->setWidthPercentage(52);
    list_scroll_->setHeightPercentage(100);
    list_scroll_->setMarginRight(16);
    list_container_ = new brls::Box(brls::Axis::COLUMN);
    list_container_->setWidthPercentage(100);
    list_scroll_->setContentView(list_container_);
    split->addView(list_scroll_);

    auto* right_col = new brls::Box(brls::Axis::COLUMN);
    right_col->setId("omnidesk/pane_secondary");
    right_col->setWidthPercentage(48);
    right_col->setHeightPercentage(100);
    right_col->setMinWidth(0);

    detail_scroll_ = new brls::ScrollingFrame();
    detail_scroll_->setGrow(1.0f);
    detail_scroll_->setShrink(1.0f);
    detail_scroll_->setMinHeight(0);
    detail_scroll_->setWidthPercentage(100);
    detail_container_ = new brls::Box(brls::Axis::COLUMN);
    detail_container_->setWidthPercentage(100);
    detail_scroll_->setContentView(detail_container_);
    right_col->addView(detail_scroll_);

    // Live Build & Verify Log Console
    auto* console_card = ui::makeCard("desktop/soft", 14.0f, 10.0f);
    console_card->setHeight(172);
    console_card->setShrink(0.0f);
    console_card->setMarginTop(12);

    auto* console_hdr = new brls::Box(brls::Axis::ROW);
    console_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    console_hdr->setMarginBottom(6);
    console_hdr->addView(ui::makeLabel("构建与验证日志 (CMake / ABI / Load)", 14.0f, "font/grey"));
    auto* clr_btn = ui::makeButton("清空", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        std::lock_guard<std::mutex> lock(log_state_->mutex);
        log_state_->buffer.clear();
        if (console_label_) {
            console_label_->setText("就绪。点击插件「安装」或「更新」查看完整构建流水线输出。");
        }
    }, false, 30.0f);
    console_hdr->addView(clr_btn);
    console_card->addView(console_hdr);

    auto* console_scroll = new brls::ScrollingFrame();
    console_scroll->setGrow(1.0f);
    console_scroll->setWidthPercentage(100);
    console_label_ = ui::makeLabel("就绪。点击插件「安装」或「更新」查看完整构建流水线输出。", 13.5f, "brls/text", 1.38f);
    console_scroll->setContentView(console_label_);
    console_card->addView(console_scroll);

    right_col->addView(console_card);
    split->addView(right_col);
    addView(split);

    bus_sub_token_ = HostKernel::instance().bus().subscribe(
        "plugin.state_changed", "host.plugin_store",
        [this, alive = alive_](const std::string&, const nlohmann::json&) {
            if (suppress_bus_refresh_) {
                return;
            }
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (getVisibility() == brls::Visibility::VISIBLE) {
                    refresh();
                } else {
                    dirty_ = true;
                }
            });
        });

    auth_sub_token_ = AuthCenterManager::instance().subscribeChanges([this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (getVisibility() == brls::Visibility::VISIBLE) {
                refresh();
            } else {
                dirty_ = true;
            }
        });
    });

    refresh();
}

PluginStoreView::~PluginStoreView() {
    alive_->store(false, std::memory_order_release);
    if (auth_sub_token_ != 0) {
        AuthCenterManager::instance().unsubscribeChanges(auth_sub_token_);
        auth_sub_token_ = 0;
    }
    if (HostKernel::instance().isInitialized() && bus_sub_token_ != 0) {
        HostKernel::instance().bus().unsubscribe(bus_sub_token_);
    }
}

void PluginStoreView::focusPluginConfig(const std::string& plugin_id) {
    if (plugin_id.empty()) return;
    filter_tab_ = FilterTab::All;
    search_query_.clear();
    selected_plugin_id_ = plugin_id;
    refresh();
    brls::sync([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            scrollToConfigSection(true);
        }
    });
}

void PluginStoreView::appendLog(const std::string& line) {
    auto log_state = log_state_;
    if (!log_state) return;
    {
        std::lock_guard<std::mutex> lock(log_state->mutex);
        if (log_state->buffer.size() > 6000) {
            log_state->buffer.erase(0, log_state->buffer.size() - 4000);
        }
        log_state->buffer += line;
        if (!line.empty() && line.back() != '\n') {
            log_state->buffer += '\n';
        }
    }
    brls::sync([this, alive = alive_, log_state]() {
        if (!alive->load(std::memory_order_acquire) || !console_label_) return;
        std::lock_guard<std::mutex> lock(log_state->mutex);
        console_label_->setText(log_state->buffer);
    });
}

void PluginStoreView::refreshIfNeeded() {
    if (dirty_) {
        refresh();
    }
}

void PluginStoreView::refresh() {
    if (!alive_->load(std::memory_order_acquire)) return;
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
        if (cur == this) {
            brls::Application::giveFocus(nullptr);
            break;
        }
    }
    dirty_ = false;
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    const auto cat_items = kernel.catalog().items();
    const auto installed = kernel.installer().listInstalled();
    size_t loaded_count = 0;
    for (const auto& rec : installed) {
        if (kernel.plugins().isLoaded(rec.manifest.id)) {
            ++loaded_count;
        }
    }

    abi_status_cache_.clear();
    size_t stale_count = 0;
    for (const auto& entry : kernel.installedAbiStatuses(false)) {
        if (entry.needs_rebuild) {
            ++stale_count;
        }
        abi_status_cache_[entry.plugin_id] = entry;
    }
    if (rebuild_stale_btn_) {
        rebuild_stale_btn_->setText(
            busy_ ? "正在重编..."
                  : ("一键重编过期 (" + std::to_string(stale_count) + ")"));
        rebuild_stale_btn_->setVisibility(
            stale_count > 0 ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }

    std::string summary = "目录 " + std::to_string(cat_items.size()) + " 个 · 已安装 " +
                          std::to_string(installed.size()) + " 个 · 运行中 " +
                          std::to_string(loaded_count) + " 个";
    if (stale_count > 0) {
        summary += " · ABI过期 " + std::to_string(stale_count) + " 个";
    }
    if (!search_query_.empty()) {
        summary += " · 筛选: \"" + search_query_ + "\"";
    }
    if (summary_label_) {
        summary_label_->setText(summary);
    }

    // Filter tabs
    clearBoxViewsSafely(filter_bar_);
    struct TabDef {
        FilterTab tab;
        const char* label;
        const char* accent;
    };
    const TabDef tabs[] = {
        {FilterTab::All, "全部", "brls/accent"},
        {FilterTab::Core, "底座与AI", "domain/core"},
        {FilterTab::Media, "影视音乐", "domain/media"},
        {FilterTab::Text, "文字期刊", "domain/text"},
        {FilterTab::Book, "书本PDF", "domain/book"},
        {FilterTab::Installed, "已安装", "status/ok_fg"},
        {FilterTab::Diagnostics, "能力诊断", "domain/core"},
    };

    for (const auto& t : tabs) {
        const bool active = (filter_tab_ == t.tab);
        auto* btn = ui_comfort::makeFilterChip(t.label, [this, alive = alive_, tab = t.tab]() {
            brls::sync([this, alive, tab]() {
                if (!alive->load(std::memory_order_acquire)) return;
                filter_tab_ = tab;
                if (list_scroll_) list_scroll_->setContentOffsetY(0.0f, false);
                if (detail_scroll_) detail_scroll_->setContentOffsetY(0.0f, false);
                refresh();
            });
        }, active, t.accent, 32.0f);
        btn->setMarginRight(7);
        filter_bar_->addView(btn);
    }

    if (!search_query_.empty()) {
        auto* clr = ui::makeButton("清除搜索", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                search_query_.clear();
                if (list_scroll_) list_scroll_->setContentOffsetY(0.0f, false);
                refresh();
            });
        }, false, 32.0f);
        filter_bar_->addView(clr);
    }

    rebuildList();
}

void PluginStoreView::rebuildList() {
    list_cards_.clear();
    clearBoxViewsSafely(list_container_);
    if (filter_tab_ == FilterTab::Diagnostics) {
        renderDiagnosticsView();
        return;
    }
    renderCatalogOrInstalledList();
}

void PluginStoreView::updateSelectedCardStyling(const std::string& prev_id, const std::string& next_id) {
    if (auto it = list_cards_.find(prev_id); it != list_cards_.end() && it->second) {
        ui_comfort::styleSelectableCard(it->second, false, "brls/accent");
    }
    if (auto it = list_cards_.find(next_id); it != list_cards_.end() && it->second) {
        ui_comfort::styleSelectableCard(it->second, true, "brls/accent");
    }
}

void PluginStoreView::renderCatalogOrInstalledList() {
    auto& kernel = HostKernel::instance();
    const auto installed = kernel.installer().listInstalled();
    std::unordered_map<std::string, pm::InstalledPluginRecord> installed_map;
    installed_map.reserve(installed.size());
    for (const auto& r : installed) {
        installed_map.emplace(r.manifest.id, r);
    }

    struct Entry {
        std::string id;
        std::string name;
        std::string version;
        PluginKind kind = PluginKind::Provider;
        ContentDomain domain = ContentDomain::Core;
        std::string description;
        std::vector<std::string> provides;
        std::vector<std::string> consumes;
        bool is_installed = false;
        bool is_loaded = false;
        bool is_enabled = false;
        std::string load_error;
    };

    std::vector<Entry> rows;
    if (filter_tab_ == FilterTab::Installed) {
        for (const auto& r : installed) {
            if (!search_query_.empty() &&
                r.manifest.name.find(search_query_) == std::string::npos &&
                r.manifest.id.find(search_query_) == std::string::npos) {
                continue;
            }
            auto st = kernel.plugins().statusOf(r.manifest.id);
            rows.push_back({
                r.manifest.id, r.manifest.name, r.manifest.version, r.manifest.kind,
                r.manifest.domain, r.manifest.description, r.manifest.provides,
                r.manifest.consumes, true, st && st->loaded, r.enabled,
                st ? st->load_error : ""
            });
        }
    } else {
        std::optional<ContentDomain> dom;
        if (filter_tab_ == FilterTab::Core) dom = ContentDomain::Core;
        else if (filter_tab_ == FilterTab::Media) dom = ContentDomain::Media;
        else if (filter_tab_ == FilterTab::Text) dom = ContentDomain::Text;
        else if (filter_tab_ == FilterTab::Book) dom = ContentDomain::Book;

        for (const auto& item : kernel.catalog().filter(dom, search_query_)) {
            auto inst_it = installed_map.find(item.id);
            const pm::InstalledPluginRecord* rec =
                (inst_it != installed_map.end()) ? &inst_it->second : nullptr;
            auto st = kernel.plugins().statusOf(item.id);
            rows.push_back({
                item.id, item.name, rec ? rec->manifest.version : item.version,
                item.kind, item.domain, item.description, item.provides, item.consumes,
                rec != nullptr, st && st->loaded, rec ? rec->enabled : false,
                st ? st->load_error : ""
            });
        }

        // Also append custom locally/GitHub-installed plugins not in official catalog
        for (const auto& r : installed) {
            if (kernel.catalog().findById(r.manifest.id).has_value()) continue;
            if (dom.has_value() && r.manifest.domain != *dom) continue;
            auto st = kernel.plugins().statusOf(r.manifest.id);
            rows.push_back({
                r.manifest.id, r.manifest.name, r.manifest.version, r.manifest.kind,
                r.manifest.domain, r.manifest.description, r.manifest.provides,
                r.manifest.consumes, true, st && st->loaded, r.enabled,
                st ? st->load_error : ""
            });
        }
    }

    if (rows.empty()) {
        auto* empty = ui::makeCard("desktop/surface", 20.0f, 10.0f);
        empty->addView(ui::makeLabel("当前筛选条件下暂无插件。", 16.0f, "font/grey"));
        list_container_->addView(empty);
        clearBoxViewsSafely(detail_container_);
        config_section_box_ = nullptr;
        first_config_btn_ = nullptr;
        primary_entry_btn_ = nullptr;
        config_edit_buttons_.clear();
        return;
    }

    if (selected_plugin_id_.empty() ||
        std::none_of(rows.begin(), rows.end(), [&](const Entry& e) { return e.id == selected_plugin_id_; })) {
        selected_plugin_id_ = rows.front().id;
    }

    for (const auto& e : rows) {
        const bool selected = (e.id == selected_plugin_id_);
        auto* card = ui::makeCard(selected ? "desktop/active" : "desktop/surface", 16.0f, 12.0f);
        card->setMarginBottom(12);
        card->setFocusable(true);
        if (selected) {
            card->setBorderColor(ui::color("brls/accent"));
        }

        auto* top = new brls::Box(brls::Axis::ROW);
        top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top->setAlignItems(brls::AlignItems::CENTER);
        top->setMarginBottom(6);

        auto* name_row = new brls::Box(brls::Axis::ROW);
        name_row->setAlignItems(brls::AlignItems::CENTER);
        name_row->setShrink(1.0f);
        name_row->setMinWidth(0);
        name_row->setMarginRight(8);
        auto* name_lbl = ui::makeLabel(e.name, 18.0f, "brls/text");
        name_lbl->setSingleLine(true);
        name_lbl->setShrink(1.0f);
        name_row->addView(name_lbl);
        auto* ver = ui::makePill("v" + e.version, "desktop/soft", "font/grey");
        ver->setMarginLeft(8);
        ver->setShrink(0.0f);
        name_row->addView(ver);
        top->addView(name_row);

        std::string st_text = "未安装";
        std::string st_bg = "desktop/soft";
        std::string st_fg = "font/grey";
        if (e.is_installed) {
            if (!e.load_error.empty()) {
                st_text = "加载异常";
                st_bg = "status/err_bg";
                st_fg = "status/err_fg";
            } else if (e.is_loaded) {
                st_text = "运行中";
                st_bg = "status/ok_bg";
                st_fg = "status/ok_fg";
            } else if (!e.is_enabled) {
                st_text = "已停用";
                st_bg = "status/warn_bg";
                st_fg = "status/warn_fg";
            } else {
                st_text = "已安装";
                st_bg = "status/info_bg";
                st_fg = "status/info_fg";
            }
        }
        auto* badges = new brls::Box(brls::Axis::ROW);
        badges->setAlignItems(brls::AlignItems::CENTER);
        badges->setShrink(0.0f);
        if (e.is_installed) {
            auto abi_it = abi_status_cache_.find(e.id);
            if (abi_it != abi_status_cache_.end() && abi_it->second.needs_rebuild) {
                auto* abi_pill = ui::makePill("ABI过期·需重编", "status/warn_bg", "status/warn_fg");
                abi_pill->setMarginRight(6);
                badges->addView(abi_pill);
            }
            if (auto w = HostKernel::instance().workers().status("plugin:" + e.id)) {
                auto* iso_pill = ui::makePill(
                    w->quarantined ? "工作器熔断" : "隔离进程",
                    w->quarantined ? "status/err_bg" : "desktop/soft",
                    w->quarantined ? "status/err_fg" : "status/info_fg");
                iso_pill->setMarginRight(6);
                badges->addView(iso_pill);
            }
            const auto auth_rep = AuthCenterManager::instance().inspectPlugin(e.id);
            if (auth_rep.method != AuthMethodKind::NoAuth) {
                auto* auth_pill = ui::makePill(
                    auth_rep.status_badge_text, auth_rep.status_bg_token, auth_rep.status_fg_token);
                auth_pill->setMarginRight(6);
                badges->addView(auth_pill);
            }
        }
        badges->addView(ui::makePill(st_text, st_bg, st_fg));
        top->addView(badges);
        card->addView(top);

        auto* desc = ui::makeLabel(e.description, 15.0f, "font/grey", 1.48f);
        desc->setMarginBottom(10);
        card->addView(desc);

        auto* meta = new brls::Box(brls::Axis::ROW);
        meta->setAlignItems(brls::AlignItems::CENTER);
        auto* dom_pill = ui::makePill(toDisplayLabel(e.domain), "desktop/soft", domainColorToken(e.domain));
        dom_pill->setMarginRight(6);
        meta->addView(dom_pill);
        auto* kind_pill = ui::makePill(toDisplayLabel(e.kind), "desktop/soft", "font/grey");
        kind_pill->setMarginRight(8);
        meta->addView(kind_pill);
        if (!e.provides.empty()) {
            meta->addView(makeBadgeRow(e.provides, "status/info_bg", "status/info_fg", "⊕ ", 3));
        }
        card->addView(meta);

        list_cards_[e.id] = card;
        card->registerClickAction([this, alive = alive_, id = e.id](brls::View*) {
            brls::sync([this, alive, id]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (selected_plugin_id_ == id) {
                    activateSelectedPluginEntry(id);
                    return;
                }
                const std::string prev = selected_plugin_id_;
                selected_plugin_id_ = id;
                updateSelectedCardStyling(prev, selected_plugin_id_);
                if (detail_scroll_) {
                    detail_scroll_->setContentOffsetY(0.0f, false);
                }
                renderDetail(selected_plugin_id_);
            });
            return true;
        });
        list_container_->addView(card);
    }

    renderDetail(selected_plugin_id_);
}

void PluginStoreView::renderDiagnosticsView() {
    auto& kernel = HostKernel::instance();

    // 1. SDK ABI Drift & Batch Rebuild Card
    auto* abi_card = ui::makeCard("desktop/surface", 16.0f, 10.0f);
    abi_card->setMarginBottom(12);
    abi_card->addView(ui::makeLabel("SDK ABI 签名一致性与热重编诊断", 18.0f, "brls/text"));
    auto* host_sig_lbl = ui::makeLabel(
        "宿主 SDK v" + omnidesk::sdkVersion() + " · 布局指纹: " +
            omnidesk::sdkAbiSignature(),
        13.5f, "font/grey");
    host_sig_lbl->setMarginTop(4);
    abi_card->addView(host_sig_lbl);

    const auto abi_entries = kernel.installedAbiStatuses(false);
    size_t stale_count = 0;
    if (abi_entries.empty()) {
        auto* none = ui::makeLabel("当前尚未安装本地 .so 插件。", 14.5f, "font/grey");
        none->setMarginTop(8);
        abi_card->addView(none);
    } else {
        for (const auto& ae : abi_entries) {
            if (ae.needs_rebuild) {
                ++stale_count;
            }
            auto* row = new brls::Box(brls::Axis::ROW);
            row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            row->setAlignItems(brls::AlignItems::CENTER);
            row->setMarginTop(8);

            auto* left = new brls::Box(brls::Axis::COLUMN);
            left->setWidth(0);
            left->setGrow(1.0f);
            left->setShrink(1.0f);
            left->setMarginRight(10);
            left->addView(ui::makeLabel(ae.plugin_id + " (v" + ae.version + ")", 14.5f, "brls/text"));
            const std::string sub =
                ae.compatible ? ("签名: " + ae.plugin_abi_signature) : ae.reason;
            left->addView(ui::makeLabel(sub, 12.5f, ae.compatible ? "font/grey" : "status/warn_fg"));
            row->addView(left);

            auto* pill = ui::makePill(
                ae.needs_rebuild ? "ABI过期·需重编" : (ae.compatible ? "兼容" : "不兼容"),
                ae.needs_rebuild ? "status/warn_bg"
                                 : (ae.compatible ? "status/ok_bg" : "status/err_bg"),
                ae.needs_rebuild ? "status/warn_fg"
                                 : (ae.compatible ? "status/ok_fg" : "status/err_fg"));
            pill->setShrink(0.0f);
            row->addView(pill);
            abi_card->addView(row);
        }
    }

    auto* abi_actions = new brls::Box(brls::Axis::ROW);
    abi_actions->setMarginTop(12);
    auto* rebuild_stale = ui::makeButton(
        busy_ ? "正在重编..."
              : ("一键重编全部过期插件 (" + std::to_string(stale_count) + ")"),
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire) && !busy_) startBatchRebuild(true);
        },
        stale_count > 0, 36.0f);
    rebuild_stale->setMarginRight(8);
    abi_actions->addView(rebuild_stale);
    abi_actions->addView(ui::makeButton(
        "全量按当前SDK重编",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire) && !busy_) startBatchRebuild(false);
        },
        false, 36.0f));
    abi_card->addView(abi_actions);
    list_container_->addView(abi_card);

    // 2. WorkerSupervisor Isolation & Crash Guard Card
    auto* worker_card = ui::makeCard("desktop/surface", 16.0f, 10.0f);
    worker_card->setMarginBottom(12);
    worker_card->addView(ui::makeLabel("WorkerSupervisor 进程隔离与崩溃保护", 18.0f, "brls/text"));
    const auto workers = kernel.workers().listWorkers();
    if (workers.empty()) {
        auto* hint = ui::makeLabel(
            "当前无独立子进程托管工作器（在 plugin.json 设置 \"isolation\": \"subprocess\" 即可启用进程崩溃隔离）。",
            14.0f, "font/grey");
        hint->setMarginTop(8);
        worker_card->addView(hint);
    } else {
        for (const auto& w : workers) {
            auto* row = new brls::Box(brls::Axis::ROW);
            row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            row->setAlignItems(brls::AlignItems::CENTER);
            row->setMarginTop(8);

            auto* left = new brls::Box(brls::Axis::COLUMN);
            left->setWidth(0);
            left->setGrow(1.0f);
            left->setShrink(1.0f);
            left->setMarginRight(10);
            const std::string tr =
                (w.transport == WorkerTransport::UnixSocket) ? "unix_socket" : "pipe";
            left->addView(ui::makeLabel(w.worker_id + " [" + tr + "]", 14.5f, "brls/text"));
            std::string stat_txt = "PID=" + std::to_string(w.pid) +
                                   " · requests=" + std::to_string(w.requests_served) +
                                   " · crashes=" + std::to_string(w.crash_count) +
                                   " · restarts=" + std::to_string(w.restart_count);
            if (!w.last_error.empty()) {
                stat_txt += " · " + w.last_error;
            }
            left->addView(ui::makeLabel(stat_txt, 12.5f, "font/grey"));
            row->addView(left);

            const std::string pill_txt =
                w.quarantined ? "已熔断" : (w.running ? "运行中" : "已停止");
            const std::string pill_bg =
                w.quarantined ? "status/err_bg" : (w.running ? "status/ok_bg" : "status/warn_bg");
            const std::string pill_fg =
                w.quarantined ? "status/err_fg" : (w.running ? "status/ok_fg" : "status/warn_fg");
            auto* pill = ui::makePill(pill_txt, pill_bg, pill_fg);
            pill->setShrink(0.0f);
            row->addView(pill);
            worker_card->addView(row);
        }
    }
    list_container_->addView(worker_card);

    // 3. CapabilityRegistry Card
    auto* cap_card = ui::makeCard("desktop/surface", 16.0f, 10.0f);
    cap_card->setMarginBottom(12);
    cap_card->addView(ui::makeLabel("CapabilityRegistry 已注册能力", 18.0f, "brls/text"));

    const auto caps = kernel.capabilities().listAll();
    if (caps.empty()) {
        auto* hint = ui::makeLabel("当前尚无插件向主核注册能力。安装 plugin-magpie-agent / plugin-fnos-nas 后将在此实时展示优先级与元数据。", 14.5f, "font/grey");
        hint->setMarginTop(8);
        cap_card->addView(hint);
    } else {
        for (const auto& c : caps) {
            auto* row = new brls::Box(brls::Axis::ROW);
            row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            row->setAlignItems(brls::AlignItems::CENTER);
            row->setMarginTop(8);
            auto* pill = ui::makePill(c.capability_id, "status/info_bg", "status/info_fg");
            pill->setShrink(0.0f);
            pill->setMarginRight(8);
            row->addView(pill);
            auto* r_lbl = ui::makeLabel(c.plugin_id + " (priority=" + std::to_string(c.priority) + ")", 14.5f, "brls/text");
            r_lbl->setShrink(1.0f);
            row->addView(r_lbl);
            cap_card->addView(row);
        }
    }
    list_container_->addView(cap_card);

    auto* dom_card = ui::makeCard("desktop/surface", 16.0f, 10.0f);
    dom_card->addView(ui::makeLabel("DomainRegistry 数据源与工作区扩展", 18.0f, "brls/text"));
    std::string stat = "MediaSources: " + std::to_string(kernel.domains().mediaSources().size()) +
                       " · TextSources: " + std::to_string(kernel.domains().textSources().size()) +
                       " · BookSources: " + std::to_string(kernel.domains().bookSources().size()) +
                       " · CustomTabs: " + std::to_string(kernel.domains().workspaceTabs().size());
    auto* stat_lbl = ui::makeLabel(stat, 15.0f, "font/grey");
    stat_lbl->setMarginTop(8);
    dom_card->addView(stat_lbl);
    list_container_->addView(dom_card);

    clearBoxViewsSafely(detail_container_);
    config_section_box_ = nullptr;
    first_config_btn_ = nullptr;
    primary_entry_btn_ = nullptr;
    config_edit_buttons_.clear();

    auto* help = ui::makeCard("desktop/surface", 16.0f, 10.0f);
    help->addView(ui::makeLabel("插件解耦与防崩溃架构说明", 18.0f, "brls/text"));
    help->addView(ui::makeLabel(
        "• C ABI + 结构体布局指纹校验：dlopen 后先在 omnidesk_plugin_create() 之前校验 struct_size 与 sdk_abi_signature，老旧 .so 被安全拒绝。\n"
        "• 子进程崩溃隔离：设置 \"isolation\": \"subprocess\"（支持 pipe / unix_socket）后，.so 仅在 omnidesk-plugin-worker 子进程中加载并通过 JSON RPC 通信。\n"
        "• 异常隔离与自动拉起：能力调用、域接口、EntityBus 与崩溃进程均有隔离屏障与自动重启/熔断，异常仅在主舞台顶部提示一行黄字而不影响宿主运行。\n"
        "• 一键热重编：当宿主 SDK 升级时，可直接在诊断页或插件详情按当前 SDK 从源码重编并恢复。",
        14.5f, "font/grey", 1.55f));
    detail_container_->addView(help);
}

void PluginStoreView::renderDetail(const std::string& plugin_id) {
    clearBoxViewsSafely(detail_container_);
    config_section_box_ = nullptr;
    first_config_btn_ = nullptr;
    primary_entry_btn_ = nullptr;
    config_edit_buttons_.clear();

    auto& kernel = HostKernel::instance();

    auto rec = kernel.installer().getInstalled(plugin_id);
    auto cat = kernel.catalog().findById(plugin_id);
    auto st = kernel.plugins().statusOf(plugin_id);

    if (!rec.has_value() && !cat.has_value()) {
        return;
    }

    auto* card = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    const std::string name = rec ? rec->manifest.name : cat->name;
    const std::string ver = rec ? rec->manifest.version : cat->version;
    const std::string desc = rec ? rec->manifest.description : cat->description;
    const std::string repo = rec ? rec->source_url : cat->repo_url;
    const std::string branch = rec ? rec->branch : cat->repo_branch;
    const ContentDomain domain = rec ? rec->manifest.domain : cat->domain;
    const auto provides = rec ? rec->manifest.provides : cat->provides;
    const auto consumes = rec ? rec->manifest.consumes : cat->consumes;

    card->addView(ui::makeLabel(name, 22.0f, "brls/text"));
    auto* sub = ui::makeLabel(plugin_id + " · v" + ver + " · " + (rec ? rec->manifest.author : cat->author),
                              14.5f, "font/grey");
    sub->setMarginTop(4);
    sub->setMarginBottom(10);
    card->addView(sub);

    auto* d_lbl = ui::makeLabel(desc, 15.5f, "brls/text", 1.52f);
    d_lbl->setMarginBottom(12);
    card->addView(d_lbl);

    // Capabilities & missing dependency warnings
    if (!provides.empty()) {
        auto* p_lbl = ui::makeLabel("提供能力 (provides):", 13.5f, "font/grey");
        p_lbl->setMarginBottom(4);
        card->addView(p_lbl);
        auto* p_row = makeBadgeRow(provides, "status/info_bg", "status/info_fg");
        p_row->setMarginBottom(10);
        card->addView(p_row);
    }

    if (!consumes.empty()) {
        auto* c_lbl = ui::makeLabel("依赖能力 (consumes):", 13.5f, "font/grey");
        c_lbl->setMarginBottom(4);
        card->addView(c_lbl);
        auto* c_row = new brls::Box(brls::Axis::ROW);
        c_row->setMarginBottom(10);
        for (const auto& dep : consumes) {
            const bool satisfied = kernel.capabilities().has(dep);
            auto* p = ui::makePill(
                (satisfied ? "✓ " : "⚠ ") + dep,
                satisfied ? "status/ok_bg" : "status/warn_bg",
                satisfied ? "status/ok_fg" : "status/warn_fg");
            p->setMarginRight(6);
            c_row->addView(p);
        }
        card->addView(c_row);
    }

    auto* repo_lbl = ui::makeLabel("仓库源: " + repo + (branch.empty() ? "" : " [" + branch + "]"),
                                 13.5f, "font/grey");
    repo_lbl->setMarginBottom(6);
    card->addView(repo_lbl);

    if (rec.has_value()) {
        std::string lock_txt = "来源: " + (rec->install_source.empty() ? "source-build" : rec->install_source) +
                               (rec->release_tag.empty() ? "" : " [" + rec->release_tag + "]") +
                               " · SHA-256: " + rec->so_sha256.substr(0, 16) + "... · Commit: " +
                               (rec->commit_sha.size() > 10 ? rec->commit_sha.substr(0, 10) : rec->commit_sha);
        auto* l_lbl = ui::makeLabel(lock_txt, 13.5f, "font/grey");
        l_lbl->setMarginBottom(4);
        card->addView(l_lbl);

        const std::string iso_txt =
            (rec->manifest.isolation == pm::PluginIsolation::Subprocess)
                ? ("subprocess (" + rec->manifest.worker_transport + ")")
                : "in_process";
        std::string runtime_txt = "隔离模式: " + iso_txt;
        auto abi_it = abi_status_cache_.find(plugin_id);
        if (abi_it != abi_status_cache_.end()) {
            runtime_txt += abi_it->second.compatible ? " · ABI: 兼容 (" : " · ABI: 过期/不兼容 (";
            runtime_txt += (abi_it->second.plugin_abi_signature.empty()
                                ? "legacy-no-sig"
                                : abi_it->second.plugin_abi_signature) +
                           ")";
        }
        auto* iso_lbl = ui::makeLabel(runtime_txt, 13.5f, "font/grey");
        iso_lbl->setMarginBottom(4);
        card->addView(iso_lbl);

        if (auto w = kernel.workers().status("plugin:" + plugin_id)) {
            std::string w_txt = "工作进程 PID=" + std::to_string(w->pid) +
                                (w->running ? " [运行中]" : " [未运行]") +
                                " · requests=" + std::to_string(w->requests_served) +
                                " · crashes=" + std::to_string(w->crash_count) +
                                " · restarts=" + std::to_string(w->restart_count);
            if (!w->last_signal_name.empty()) {
                w_txt += " · last_sig=" + w->last_signal_name;
            }
            if (w->quarantined) {
                w_txt += " · [已熔断隔离]";
            }
            auto* w_lbl = ui::makeLabel(
                w_txt, 13.5f, w->quarantined ? "status/err_fg" : "status/info_fg");
            w_lbl->setMarginBottom(8);
            card->addView(w_lbl);
        } else {
            iso_lbl->setMarginBottom(10);
        }

        auto rit = radar_results_.find(plugin_id);
        if (rit != radar_results_.end()) {
            const auto& ur = rit->second;
            if (!ur.ok) {
                auto* rc = ui::makeCard("status/warn_bg", 10.0f, 8.0f);
                rc->setMarginBottom(12);
                rc->addView(ui::makeLabel("更新雷达检查失败: " + ur.error, 13.5f, "status/warn_fg"));
                card->addView(rc);
            } else if (ur.update_available) {
                auto* rc = ui::makeCard("status/info_bg", 10.0f, 8.0f);
                rc->setMarginBottom(12);
                std::string hdr = "更新雷达发现上游更新 · 领先 " + std::to_string(ur.ahead_commits) + " commits";
                if (ur.prebuilt_available) {
                    hdr += " · [prebuilt .tar.zst";
                    if (!ur.prebuilt.tag.empty()) hdr += " " + ur.prebuilt.tag;
                    hdr += "]";
                } else {
                    hdr += " · [/tmp 源码编译]";
                }
                rc->addView(ui::makeLabel(hdr, 14.0f, "status/info_fg"));
                if (!ur.changelog.empty()) {
                    auto* ch = ui::makeLabel(ur.changelog, 12.5f, "brls/text", 1.35f);
                    ch->setMarginTop(4);
                    rc->addView(ch);
                }
                if (!ur.diff_stat.empty()) {
                    auto* ds = ui::makeLabel(ur.diff_stat, 12.0f, "font/grey", 1.3f);
                    ds->setMarginTop(4);
                    rc->addView(ds);
                }
                card->addView(rc);
            } else {
                auto* rc = ui::makeCard("status/ok_bg", 10.0f, 8.0f);
                rc->setMarginBottom(12);
                std::string short_sha = ur.local_commit.size() > 10 ? ur.local_commit.substr(0, 10) : ur.local_commit;
                rc->addView(ui::makeLabel("更新雷达: 已同步上游最新 (" + short_sha + ")",
                                          13.5f, "status/ok_fg"));
                card->addView(rc);
            }
        }
    }

    bool abi_needs_rebuild = false;
    if (rec.has_value()) {
        auto abi_it = abi_status_cache_.find(plugin_id);
        if (abi_it != abi_status_cache_.end() && abi_it->second.needs_rebuild) {
            abi_needs_rebuild = true;
            auto* abi_card = ui::makeCard("status/warn_bg", 10.0f, 8.0f);
            abi_card->setMarginBottom(12);
            std::string abi_msg = "SDK ABI 已过期 · 需重编: " + abi_it->second.reason +
                                  "\n宿主签名: " + abi_it->second.host_abi_signature;
            if (!abi_it->second.plugin_abi_signature.empty()) {
                abi_msg += "\n插件签名: " + abi_it->second.plugin_abi_signature;
            }
            abi_card->addView(ui::makeLabel(abi_msg, 13.5f, "status/warn_fg", 1.35f));
            card->addView(abi_card);
        }
    }

    if (st && !st->load_error.empty()) {
        auto* err_card = ui::makeCard("status/err_bg", 10.0f, 8.0f);
        err_card->setMarginBottom(12);
        err_card->addView(ui::makeLabel("加载失败: " + st->load_error, 14.0f, "status/err_fg"));
        card->addView(err_card);
    }

    // Action Bar (two clean rows when installed so controls never clip horizontally)
    if (!rec.has_value()) {
        auto* act = new brls::Box(brls::Axis::ROW);
        act->setMarginTop(4);
        auto* inst_btn = ui::makeButton(
            busy_ ? "正在安装..." : "安装插件 (优先 .tar.zst)",
            [this, alive = alive_, repo, branch]() {
                if (alive->load(std::memory_order_acquire) && !busy_) {
                    startGitInstall(repo, branch);
                }
            },
            true, 40.0f);
        primary_entry_btn_ = inst_btn;
        act->addView(inst_btn);
        card->addView(act);
    } else {
        const bool is_loaded = st && st->loaded;

        // Row 1: Primary Workspace/Workbench Entry + Quick Jump to Settings + Enable/Disable
        auto* entry_row = new brls::Box(brls::Axis::ROW);
        entry_row->setAlignItems(brls::AlignItems::CENTER);
        entry_row->setMarginTop(4);
        entry_row->setMarginBottom(8);

        if (is_loaded) {
            if (auto custom_tab = findCustomTabForPlugin(plugin_id)) {
                auto* open_tab_btn = ui::makeButton(
                    "进入: " + custom_tab->title + " ▸",
                    [this, alive = alive_, tid = custom_tab->tab_id]() {
                        if (alive->load(std::memory_order_acquire) && on_open_custom_tab_) {
                            on_open_custom_tab_(tid);
                        }
                    },
                    true, 38.0f);
                open_tab_btn->setMarginRight(8);
                open_tab_btn->setShrink(0.0f);
                primary_entry_btn_ = open_tab_btn;
                entry_row->addView(open_tab_btn);
            } else if (on_open_domain_) {
                auto* open_dom_btn = ui::makeButton(
                    domainWorkbenchButtonTitle(domain),
                    [this, alive = alive_, domain]() {
                        if (alive->load(std::memory_order_acquire) && on_open_domain_) {
                            on_open_domain_(domain);
                        }
                    },
                    domain != ContentDomain::Core, 38.0f);
                open_dom_btn->setMarginRight(8);
                open_dom_btn->setShrink(0.0f);
                primary_entry_btn_ = open_dom_btn;
                entry_row->addView(open_dom_btn);
            }
        }

        auto* cfg_jump_btn = ui::makeButton(
            "⚙ 参数设置",
            [this, alive = alive_]() {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        scrollToConfigSection(true);
                    }
                });
            },
            false, 38.0f);
        cfg_jump_btn->setMarginRight(8);
        cfg_jump_btn->setShrink(0.0f);
        entry_row->addView(cfg_jump_btn);

        auto* toggle_btn = ui::makeButton(
            rec->enabled ? (is_loaded ? "停用" : "启用并加载") : "启用",
            [this, alive = alive_, plugin_id, enabled = rec->enabled, is_loaded]() {
                brls::sync([this, alive, plugin_id, enabled, is_loaded]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    auto& k = HostKernel::instance();
                    clearBoxViewsSafely(detail_container_);
                    config_section_box_ = nullptr;
                    first_config_btn_ = nullptr;
                    primary_entry_btn_ = nullptr;
                    config_edit_buttons_.clear();
                    std::string err;
                    suppress_bus_refresh_ = true;
                    if (enabled && is_loaded) {
                        k.plugins().disablePlugin(plugin_id);
                        appendLog("Disabled plugin " + plugin_id);
                    } else {
                        if (!k.plugins().enablePlugin(plugin_id, err)) {
                            appendLog("Enable failed: " + err);
                            brls::Application::notify("启用失败: " + err);
                        } else {
                            appendLog("Loaded plugin " + plugin_id);
                        }
                    }
                    suppress_bus_refresh_ = false;
                    refresh();
                });
            },
            !is_loaded && !abi_needs_rebuild, 38.0f);
        toggle_btn->setShrink(0.0f);
        if (!primary_entry_btn_) {
            primary_entry_btn_ = toggle_btn;
        }
        entry_row->addView(toggle_btn);
        card->addView(entry_row);

        // Row 2: Lifecycle Maintenance (Update / Rebuild / Rollback / Uninstall)
        auto* maint_row = new brls::Box(brls::Axis::ROW);
        maint_row->setAlignItems(brls::AlignItems::CENTER);

        auto rit = radar_results_.find(plugin_id);
        const bool radar_has_update = (rit != radar_results_.end() && rit->second.ok && rit->second.update_available);
        std::string upd_label = "更新/重编译";
        if (radar_has_update) {
            upd_label = "立即更新 (" + std::to_string(rit->second.ahead_commits) + " commits)";
        }
        auto* upd_btn = ui::makeButton(
            upd_label,
            [this, alive = alive_, plugin_id]() {
                if (alive->load(std::memory_order_acquire) && !busy_) startUpdate(plugin_id);
            },
            radar_has_update, 36.0f);
        upd_btn->setMarginRight(8);
        upd_btn->setShrink(0.0f);
        maint_row->addView(upd_btn);

        auto* rebuild_btn = ui::makeButton(
            busy_ ? "重编中..." : "按当前SDK重编",
            [this, alive = alive_, plugin_id]() {
                if (alive->load(std::memory_order_acquire) && !busy_) startRebuildPlugin(plugin_id);
            },
            abi_needs_rebuild && !radar_has_update, 36.0f);
        rebuild_btn->setMarginRight(8);
        rebuild_btn->setShrink(0.0f);
        maint_row->addView(rebuild_btn);

        if (!rec->backup_versions.empty()) {
            auto* rb_btn = ui::makeButton("回滚", [this, alive = alive_, plugin_id]() {
                brls::sync([this, alive, plugin_id]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    auto& k = HostKernel::instance();
                    clearBoxViewsSafely(detail_container_);
                    config_section_box_ = nullptr;
                    first_config_btn_ = nullptr;
                    primary_entry_btn_ = nullptr;
                    config_edit_buttons_.clear();
                    suppress_bus_refresh_ = true;
                    k.plugins().unloadPlugin(plugin_id);
                    auto r = k.installer().rollbackPlugin(plugin_id);
                    if (r.ok) {
                        std::string err;
                        k.plugins().loadPlugin(plugin_id, err);
                        appendLog("Rolled back " + plugin_id + " -> v" + r.version);
                    } else {
                        appendLog("Rollback failed: " + r.error);
                    }
                    suppress_bus_refresh_ = false;
                    refresh();
                });
            }, false, 36.0f);
            rb_btn->setMarginRight(8);
            rb_btn->setShrink(0.0f);
            maint_row->addView(rb_btn);
        }

        auto* rm_btn = ui::makeButton("卸载", [this, alive = alive_, plugin_id]() {
            brls::sync([this, alive, plugin_id]() {
                if (!alive->load(std::memory_order_acquire)) return;
                auto& k = HostKernel::instance();
                clearBoxViewsSafely(detail_container_);
                config_section_box_ = nullptr;
                first_config_btn_ = nullptr;
                primary_entry_btn_ = nullptr;
                config_edit_buttons_.clear();
                suppress_bus_refresh_ = true;
                k.plugins().unloadPlugin(plugin_id);
                std::string err;
                if (k.installer().removePlugin(plugin_id, err)) {
                    appendLog("Removed " + plugin_id);
                } else {
                    appendLog("Remove failed: " + err);
                }
                suppress_bus_refresh_ = false;
                refresh();
            });
        }, false, 36.0f);
        rm_btn->setShrink(0.0f);
        maint_row->addView(rm_btn);
        card->addView(maint_row);
    }

    if (rec.has_value()) {
        card->addView(ui::makeDivider(14.0f));
        renderConfigSection(card, *rec);
    }

    detail_container_->addView(card);
}

void PluginStoreView::renderConfigSection(brls::Box* container, const pm::InstalledPluginRecord& rec) {
    auto& kernel = HostKernel::instance();
    config_section_box_ = new brls::Box(brls::Axis::COLUMN);
    config_section_box_->setWidthPercentage(100);
    container->addView(config_section_box_);

    auto* hdr_row = new brls::Box(brls::Axis::ROW);
    hdr_row->setWidthPercentage(100);
    hdr_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    hdr_row->setAlignItems(brls::AlignItems::CENTER);
    hdr_row->addView(ui::makeLabel("插件配置与凭据 (参数设置)", 17.5f, "brls/text"));
    if (!rec.manifest.config_schema.empty()) {
        auto* cnt_pill = ui::makePill(
            std::to_string(rec.manifest.config_schema.size()) + " 项可调参数",
            "desktop/soft", "font/grey");
        cnt_pill->setShrink(0.0f);
        hdr_row->addView(cnt_pill);
    }
    config_section_box_->addView(hdr_row);

    // Host-unified Auth Status & One-Click Browser Cookie Harvester card
    const auto auth_rep = AuthCenterManager::instance().inspectPlugin(rec.manifest.id);
    if (auth_rep.method != AuthMethodKind::NoAuth) {
        auto* auth_box = ui::makeCard(
            auth_rep.status == AuthStatusKind::GuestCookieOnly ? "status/warn_bg" : "desktop/soft",
            12.0f, 10.0f);
        auth_box->setMarginTop(8);
        auth_box->setMarginBottom(4);

        auto* ab_top = new brls::Box(brls::Axis::ROW);
        ab_top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        ab_top->setAlignItems(brls::AlignItems::CENTER);
        ab_top->addView(ui::makeLabel("登录授权诊断与一键自动取 Cookie", 14.5f, "brls/text"));
        ab_top->addView(ui::makePill(
            auth_rep.status_badge_text, auth_rep.status_bg_token, auth_rep.status_fg_token));
        auth_box->addView(ab_top);

        auto* ab_desc = ui::makeLabel(
            auth_rep.detail_summary,
            13.0f,
            auth_rep.status == AuthStatusKind::GuestCookieOnly ? "status/warn_fg" : "font/grey",
            1.40f);
        ab_desc->setMarginTop(4);
        auth_box->addView(ab_desc);

        auto* ab_btns = new brls::Box(brls::Axis::ROW);
        ab_btns->setAlignItems(brls::AlignItems::CENTER);
        ab_btns->setMarginTop(8);
        ui_comfort::enableFlexWrap(ab_btns, 6.0f);

        if (auth_rep.supports_browser_cookie_login) {
            const std::string login_lbl =
                auth_rep.allows_custom_login_url
                    ? "🌐 输入网址登录自动获取 Cookie"
                    : (auth_rep.status == AuthStatusKind::Authenticated
                           ? "🌐 重新浏览器登录更新 Cookie"
                           : "🌐 浏览器登录并自动获取 Cookie");
            auto* b_login = ui::makeButton(
                login_lbl,
                [alive = alive_, id = rec.manifest.id,
                 allow_custom = auth_rep.allows_custom_login_url,
                 def_url = auth_rep.default_login_url]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (allow_custom) {
                        if (auto* plat = brls::Application::getPlatform()) {
                            plat->getImeManager()->openForText(
                                [alive, id](std::string url) {
                                    brls::sync([alive, id, url = std::move(url)]() {
                                        if (!alive->load(std::memory_order_acquire)) return;
                                        const std::string clean = trimAscii(url);
                                        if (clean.empty()) return;
                                        AuthCenterManager::instance().startBrowserCookieLogin(id, clean);
                                    });
                                },
                                "输入目标网站登录地址",
                                "将弹出独立 Chrome 登录窗口，完成登录或验证后关闭窗口即可自动提取并保存该站 Cookie",
                                512,
                                def_url);
                        }
                    } else {
                        AuthCenterManager::instance().startBrowserCookieLogin(id);
                    }
                },
                auth_rep.status != AuthStatusKind::Authenticated, 34.0f);
            b_login->setMarginRight(8);
            ab_btns->addView(b_login);
            if (!first_config_btn_) first_config_btn_ = b_login;

            if (!auth_rep.allows_custom_login_url) {
                auto* b_sync = ui::makeButton(
                    "🔄 同步现有会话",
                    [alive = alive_, id = rec.manifest.id]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        std::string msg;
                        AuthCenterManager::instance().syncCookiesFromExistingStores(id, msg);
                        if (!msg.empty()) brls::Application::notify(msg);
                    },
                    false, 34.0f);
                b_sync->setMarginRight(8);
                ab_btns->addView(b_sync);
            }
        }

        auto* b_hub = ui::makeButton(
            "打开统一授权中心 ▸",
            [this, alive = alive_, id = rec.manifest.id]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (on_open_auth_center_) {
                    on_open_auth_center_(id);
                } else {
                    AuthCenterDialog::open({}, id);
                }
            },
            false, 34.0f);
        ab_btns->addView(b_hub);
        if (!first_config_btn_) first_config_btn_ = b_hub;

        auth_box->addView(ab_btns);
        config_section_box_->addView(auth_box);
    }

    // Render plugin-provided status/diagnostic settings panel if available,
    // AND continue rendering declarative config_schema fields underneath.
    brls::View* custom = kernel.plugins().createCustomSettingsView(rec.manifest.id);
    if (custom) {
        custom->setWidthPercentage(100);
        custom->setMarginTop(8);
        custom->setMarginBottom(4);
        config_section_box_->addView(custom);
    }

    if (rec.manifest.config_schema.empty()) {
        if (!custom && auth_rep.method == AuthMethodKind::NoAuth) {
            auto* none = ui::makeLabel("此插件未声明可配置参数。", 14.0f, "font/grey");
            none->setMarginTop(8);
            config_section_box_->addView(none);
        }
        return;
    }

    const nlohmann::json cfg = kernel.config().loadPluginConfig(rec.manifest.id);

    for (const auto& field : rec.manifest.config_schema) {
        auto* row = ui::makeCard("desktop/soft", 12.0f, 10.0f);
        row->setMarginTop(8);

        auto* top = new brls::Box(brls::Axis::ROW);
        top->setWidthPercentage(100);
        top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top->setAlignItems(brls::AlignItems::CENTER);

        auto* label_col = new brls::Box(brls::Axis::COLUMN);
        label_col->setWidth(0);
        label_col->setGrow(1.0f);
        label_col->setShrink(1.0f);
        label_col->setMarginRight(12);

        auto* title_line = new brls::Box(brls::Axis::ROW);
        title_line->setAlignItems(brls::AlignItems::CENTER);
        title_line->setWidthPercentage(100);
        auto* f_lbl = ui::makeLabel(field.label, 15.0f, "brls/text");
        f_lbl->setShrink(1.0f);
        title_line->addView(f_lbl);
        auto* k_lbl = ui::makeLabel(" (" + field.key + ")", 12.5f, "font/grey");
        k_lbl->setShrink(0.0f);
        title_line->addView(k_lbl);
        label_col->addView(title_line);

        if (!field.description.empty()) {
            auto* desc_lbl = ui::makeLabel(field.description, 13.0f, "font/grey", 1.42f);
            desc_lbl->setMarginTop(3);
            label_col->addView(desc_lbl);
        }

        const bool has_override = cfg.contains(field.key);
        const bool has_secret =
            (field.type == ConfigFieldType::Secret) &&
            kernel.secrets().hasSecret(rec.manifest.id, field.key);

        std::string cur_val = field.default_value;
        if (field.type != ConfigFieldType::Secret && has_override) {
            if (cfg[field.key].is_string()) cur_val = cfg[field.key].get<std::string>();
            else if (cfg[field.key].is_boolean()) cur_val = cfg[field.key].get<bool>() ? "true" : "false";
            else if (cfg[field.key].is_number()) cur_val = cfg[field.key].dump();
        }

        std::string val_summary;
        std::string val_color = "font/grey";
        if (field.type == ConfigFieldType::Secret) {
            if (!AuthCenterManager::instance().diagnoseCookieSecretField(
                    rec.manifest.id, field.key, val_summary, val_color)) {
                val_summary = has_secret ? "当前状态: •••••• (已加密保存)" : "当前状态: 未设置密钥";
                val_color = has_secret ? "status/ok_fg" : "font/grey";
            }
        } else if (field.type == ConfigFieldType::Bool) {
            const bool is_on = (cur_val == "true" || cur_val == "1");
            val_summary = std::string("当前状态: ") + (is_on ? "已开启" : "已关闭") +
                          (has_override ? " · [已自定义]" : " · [默认]");
            val_color = is_on ? "status/ok_fg" : "font/grey";
        } else {
            const std::string shown = cur_val.empty() ? "<空>" : truncateUtf8Display(cur_val, 54);
            val_summary = "当前值: " + shown + (has_override ? " · [已自定义]" : " · [默认]");
            val_color = has_override ? "status/info_fg" : "font/grey";
        }
        auto* val_lbl = ui::makeLabel(val_summary, 12.5f, val_color, 1.35f);
        val_lbl->setMarginTop(4);
        label_col->addView(val_lbl);

        top->addView(label_col);

        auto* btn_group = new brls::Box(brls::Axis::ROW);
        btn_group->setAlignItems(brls::AlignItems::CENTER);
        btn_group->setShrink(0.0f);

        std::string btn_label = "修改...";
        bool btn_primary = false;
        if (field.type == ConfigFieldType::Bool) {
            const bool is_on = (cur_val == "true" || cur_val == "1");
            btn_label = is_on ? "已开启 ✓" : "已关闭";
            btn_primary = is_on;
        } else if (field.type == ConfigFieldType::Select) {
            btn_label = "选择 ▾";
        } else if (field.type == ConfigFieldType::Secret) {
            btn_label = has_secret ? "更新密钥..." : "设置密钥...";
        }

        auto* edit_btn = ui::makeButton(
            btn_label,
            [this, alive = alive_, id = rec.manifest.id, field, cur_val, has_secret]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (field.type == ConfigFieldType::Bool) {
                    const bool next = !(cur_val == "true" || cur_val == "1");
                    brls::sync([this, alive, id, field, next]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        auto& hk = HostKernel::instance();
                        if (!hk.isInitialized()) return;
                        hk.config().setPluginValue(id, field.key, next);
                        reloadPluginAfterConfigEdit(
                            id, field.key,
                            field.label + (next ? ": 已开启" : ": 已关闭"));
                    });
                    return;
                }
                if (field.type == ConfigFieldType::Select && !field.options.empty()) {
                    brls::sync([this, alive, id, field, cur_val]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        openSelectOptionModal(id, field, cur_val);
                    });
                    return;
                }
                if (auto* plat = brls::Application::getPlatform()) {
                    std::string hint = field.description;
                    if (field.type == ConfigFieldType::Secret && has_secret) {
                        hint += "（直接留空确认将保持现有密钥不变；如需删除请点右侧「清除」）";
                    }
                    plat->getImeManager()->openForText(
                        [this, alive, id, field, has_secret](std::string val) {
                            brls::sync([this, alive, id, field, has_secret, val = std::move(val)]() {
                                if (!alive->load(std::memory_order_acquire)) return;
                                auto& hk = HostKernel::instance();
                                if (!hk.isInitialized()) return;
                                if (field.type == ConfigFieldType::Secret) {
                                    if (val.empty()) {
                                        if (has_secret) {
                                            brls::Application::notify("输入为空，已保留原有密钥不变");
                                        }
                                        return;
                                    }
                                    hk.secrets().setSecret(id, field.key, val);
                                    reloadPluginAfterConfigEdit(
                                        id, field.key, "已加密保存凭据: " + field.label);
                                    return;
                                }
                                if (field.type == ConfigFieldType::Int) {
                                    const std::string trimmed = trimAscii(val);
                                    if (trimmed.empty()) {
                                        brls::Application::notify("整数参数不能为空");
                                        return;
                                    }
                                    try {
                                        size_t parsed_len = 0;
                                        const long long iv = std::stoll(trimmed, &parsed_len, 10);
                                        if (parsed_len != trimmed.size()) {
                                            brls::Application::notify("请输入有效整数: " + field.label);
                                            return;
                                        }
                                        hk.config().setPluginValue(id, field.key, iv);
                                    } catch (...) {
                                        brls::Application::notify("请输入有效整数: " + field.label);
                                        return;
                                    }
                                    reloadPluginAfterConfigEdit(
                                        id, field.key,
                                        "已保存 " + field.label + " = " + trimmed);
                                    return;
                                }
                                hk.config().setPluginValue(id, field.key, val);
                                reloadPluginAfterConfigEdit(
                                    id, field.key, "已保存参数: " + field.label);
                            });
                        },
                        "配置 " + field.label,
                        hint,
                        4096,
                        field.type == ConfigFieldType::Secret ? "" : cur_val);
                }
            },
            btn_primary, 34.0f);
        edit_btn->setShrink(0.0f);
        btn_group->addView(edit_btn);
        config_edit_buttons_[field.key] = edit_btn;
        if (!first_config_btn_) {
            first_config_btn_ = edit_btn;
        }

        if (field.type == ConfigFieldType::Secret && has_secret) {
            auto* clr_secret_btn = ui::makeButton(
                "清除",
                [this, alive = alive_, id = rec.manifest.id, field]() {
                    brls::sync([this, alive, id, field]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        auto& hk = HostKernel::instance();
                        if (!hk.isInitialized()) return;
                        hk.secrets().deleteSecret(id, field.key);
                        reloadPluginAfterConfigEdit(
                            id, field.key, "已清除凭据: " + field.label);
                    });
                },
                false, 34.0f);
            clr_secret_btn->setMarginLeft(6);
            clr_secret_btn->setShrink(0.0f);
            btn_group->addView(clr_secret_btn);
        } else if (field.type != ConfigFieldType::Secret &&
                   field.type != ConfigFieldType::Bool &&
                   has_override) {
            auto* reset_btn = ui::makeButton(
                "重置",
                [this, alive = alive_, id = rec.manifest.id, field]() {
                    brls::sync([this, alive, id, field]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        auto& hk = HostKernel::instance();
                        if (!hk.isInitialized()) return;
                        nlohmann::json next_cfg = hk.config().loadPluginConfig(id);
                        next_cfg.erase(field.key);
                        hk.config().savePluginConfig(id, next_cfg);
                        reloadPluginAfterConfigEdit(
                            id, field.key, "已恢复默认值: " + field.label);
                    });
                },
                false, 34.0f);
            reset_btn->setMarginLeft(6);
            reset_btn->setShrink(0.0f);
            btn_group->addView(reset_btn);
        }

        top->addView(btn_group);
        row->addView(top);
        config_section_box_->addView(row);
    }
}

void PluginStoreView::scrollToConfigSection(bool focus_first_control) {
    if (!detail_scroll_ || !detail_container_) return;
    if (config_section_box_) {
        float local_y = 0.0f;
        brls::View* cur = config_section_box_;
        while (cur && cur != detail_container_) {
            local_y += cur->getLocalY();
            cur = cur->getParent();
        }
        const float max_scroll =
            std::max(0.0f, detail_container_->getHeight() - detail_scroll_->getHeight());
        const float target_y = std::clamp(local_y - 12.0f, 0.0f, max_scroll);
        detail_scroll_->setContentOffsetY(target_y, false);
    }
    if (focus_first_control) {
        if (first_config_btn_) {
            brls::Application::giveFocus(first_config_btn_);
        } else if (config_section_box_) {
            if (auto* f = config_section_box_->getDefaultFocus()) {
                brls::Application::giveFocus(f);
            }
        }
    }
}

bool PluginStoreView::activateSelectedPluginEntry(const std::string& plugin_id) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return false;

    const auto st = kernel.plugins().statusOf(plugin_id);
    const bool is_loaded = st && st->loaded;

    if (is_loaded) {
        if (auto custom_tab = findCustomTabForPlugin(plugin_id)) {
            if (on_open_custom_tab_) {
                on_open_custom_tab_(custom_tab->tab_id);
                return true;
            }
        }
        const auto rec = kernel.installer().getInstalled(plugin_id);
        if (rec.has_value() && rec->manifest.domain != ContentDomain::Core && on_open_domain_) {
            on_open_domain_(rec->manifest.domain);
            return true;
        }
    }

    if (config_section_box_) {
        scrollToConfigSection(true);
        return true;
    }
    if (primary_entry_btn_) {
        brls::Application::giveFocus(primary_entry_btn_);
        return true;
    }
    return false;
}

void PluginStoreView::reloadPluginAfterConfigEdit(const std::string& plugin_id,
                                                  const std::string& focus_field_key,
                                                  const std::string& toast_msg) {
    auto& hk = HostKernel::instance();
    if (!hk.isInitialized()) return;

    const float saved_list_y = list_scroll_ ? list_scroll_->getContentOffsetY() : 0.0f;
    const float saved_detail_y = detail_scroll_ ? detail_scroll_->getContentOffsetY() : 0.0f;

    clearBoxViewsSafely(detail_container_);
    config_section_box_ = nullptr;
    first_config_btn_ = nullptr;
    primary_entry_btn_ = nullptr;
    config_edit_buttons_.clear();

    std::string err;
    suppress_bus_refresh_ = true;
    if (hk.plugins().isLoaded(plugin_id)) {
        hk.plugins().reloadPlugin(plugin_id, err);
    }
    suppress_bus_refresh_ = false;

    refresh();

    if (list_scroll_) {
        list_scroll_->setContentOffsetY(saved_list_y, false);
    }
    if (detail_scroll_) {
        detail_scroll_->setContentOffsetY(saved_detail_y, false);
    }
    if (!focus_field_key.empty()) {
        auto it = config_edit_buttons_.find(focus_field_key);
        if (it != config_edit_buttons_.end() && it->second) {
            brls::Application::giveFocus(it->second);
        }
    }

    if (!err.empty()) {
        appendLog("[config-reload-error] " + plugin_id + ": " + err);
        brls::Application::notify("配置已保存，但重载异常: " + err);
    } else if (!toast_msg.empty()) {
        brls::Application::notify(toast_msg);
    }
}

void PluginStoreView::openSelectOptionModal(const std::string& plugin_id,
                                            const ConfigField& field,
                                            const std::string& current_value) {
    if (!brls::Application::getPlatform() || field.options.empty()) {
        return;
    }

    auto* modal_box = new brls::Box(brls::Axis::COLUMN);
    modal_box->setWidthPercentage(100);
    modal_box->setPadding(20, 22, 14, 22);

    modal_box->addView(ui::makeLabel("选择参数: " + field.label, 18.5f, "brls/text"));
    if (!field.description.empty()) {
        auto* desc_lbl = ui::makeLabel(field.description, 13.5f, "font/grey", 1.42f);
        desc_lbl->setMarginTop(4);
        modal_box->addView(desc_lbl);
    }

    const float list_h = std::min<float>(
        340.0f, std::max<float>(110.0f, static_cast<float>(field.options.size()) * 48.0f + 8.0f));
    auto* opt_scroll = new brls::ScrollingFrame();
    opt_scroll->setWidthPercentage(100);
    opt_scroll->setHeight(list_h);
    opt_scroll->setMarginTop(12);

    auto* opt_list = new brls::Box(brls::Axis::COLUMN);
    opt_list->setWidthPercentage(100);
    opt_scroll->setContentView(opt_list);
    modal_box->addView(opt_scroll);

    auto* dialog = new brls::Dialog(modal_box);
    if (auto* applet = dynamic_cast<brls::Box*>(dialog->getView("brls/dialog/applet"))) {
        applet->setWidth(520.0f);
        applet->setCornerRadius(14.0f);
        applet->setBorderThickness(1.0f);
        applet->setBorderColor(ui::color("desktop/line"));
        applet->setBackgroundColor(ui::color("brls/background"));
        applet->setClipsToBounds(true);
    }

    for (const auto& opt : field.options) {
        const bool is_cur = (opt == current_value);
        const std::string label = is_cur ? ("✓  " + opt + "  (当前)") : opt;
        auto* btn = ui::makeButton(
            label,
            [this, alive = alive_, dialog, plugin_id, field, opt]() {
                dialog->close([this, alive, plugin_id, field, opt]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    auto& hk = HostKernel::instance();
                    if (!hk.isInitialized()) return;
                    hk.config().setPluginValue(plugin_id, field.key, opt);
                    reloadPluginAfterConfigEdit(
                        plugin_id, field.key,
                        "已切换 " + field.label + " = " + opt);
                });
            },
            is_cur, 40.0f);
        btn->setWidthPercentage(100);
        btn->setMarginBottom(8);
        opt_list->addView(btn);
    }

    dialog->addButton("取消 Esc", []() {});
    if (auto* btn1 = dialog->getView("brls/dialog/button1")) {
        if (auto* btn_row = btn1->getParent()) {
            btn_row->setHeight(50.0f);
        }
    }
    dialog->open();
}

void PluginStoreView::promptSearch() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string q) {
                brls::sync([this, alive, q = std::move(q)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    search_query_ = q;
                    refresh();
                });
            },
            "搜索插件市场",
            "支持按名称、ID、领域或 capability（如 ai.agent.v1 / nas.connection.v1）检索",
            128,
            search_query_);
    }
}

void PluginStoreView::openInstallModal(bool local_mode) {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_, local_mode](std::string input) {
                brls::sync([this, alive, local_mode, input = std::move(input)]() {
                    if (!alive->load(std::memory_order_acquire) || input.empty()) return;
                    if (local_mode) {
                        startLocalInstall(input);
                    } else {
                        startGitInstall(input, "");
                    }
                });
            },
            local_mode ? "从本地目录编译安装插件" : "从 GitHub 仓库编译安装插件",
            local_mode ? "输入含 plugin.json + CMakeLists.txt 的本地路径"
                       : "输入 owner/repo 简写（如 ZJFILOVE/omnidesk-plugin-magpie-agent）或完整 Git URL",
            256,
            local_mode ? "" : "ZJFILOVE/");
    }
}

void PluginStoreView::openScaffoldModal() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string dir_and_id) {
                brls::sync([this, alive, dir_and_id = std::move(dir_and_id)]() {
                    if (!alive->load(std::memory_order_acquire) || dir_and_id.empty()) return;
                    pm::ScaffoldOptions opts;
                    opts.target_dir = dir_and_id;
                    const auto pos = dir_and_id.find_last_of('/');
                    opts.plugin_id = (pos == std::string::npos) ? dir_and_id : dir_and_id.substr(pos + 1);
                    opts.name = opts.plugin_id;
                    opts.github_owner = "ZJFILOVE";
                    auto res = pm::scaffoldPluginRepo(opts);
                    if (res.ok) {
                        appendLog("Scaffolded standalone plugin repo at " + res.dir_path);
                        brls::Application::notify("已生成独立插件仓库: " + res.dir_path);
                    } else {
                        appendLog("Scaffold failed: " + res.error);
                        brls::Application::notify("生成失败: " + res.error);
                    }
                });
            },
            "生成独立 C++17 .so 插件仓库骨架",
            "输入目标本地目录路径（目录名将作为初始插件 ID，自动初始化 git 仓库）",
            256,
            "/home/zhu/Projects/");
    }
}

void PluginStoreView::startGitInstall(const std::string& source, const std::string& branch) {
    busy_ = true;
    appendLog("=== 开始安装插件 (优先 prebuilt .tar.zst): " + source + " ===");
    refresh();
    std::thread([this, alive = alive_, source, branch]() {
        auto& k = HostKernel::instance();
        std::optional<pm::ReleaseArtifactSpec> release_hint;
        const std::string norm_src = pm::MarketplaceCatalog::normalizeGitSource(source);
        for (const auto& item : k.catalog().items()) {
            if ((item.repo_url == source ||
                 pm::MarketplaceCatalog::normalizeGitSource(item.repo_url) == norm_src) &&
                !item.release.archive_url.empty()) {
                release_hint = item.release;
                break;
            }
        }
        auto res = k.installer().installFromGit(
            source,
            branch,
            [this, alive](const std::string& stage, const std::string& detail) {
                if (alive->load(std::memory_order_acquire)) {
                    appendLog("[" + stage + "] " + detail);
                }
            },
            release_hint);
        brls::sync([this, alive, res]() {
            if (!alive->load(std::memory_order_acquire)) {
                return;
            }
            busy_ = false;
            auto& k = HostKernel::instance();
            if (res.ok) {
                selected_plugin_id_ = res.plugin_id;
                radar_results_.erase(res.plugin_id);
                clearBoxViewsSafely(detail_container_);
                config_section_box_ = nullptr;
                first_config_btn_ = nullptr;
                primary_entry_btn_ = nullptr;
                config_edit_buttons_.clear();
                std::string err;
                suppress_bus_refresh_ = true;
                k.plugins().reloadPlugin(res.plugin_id, err);
                suppress_bus_refresh_ = false;
                k.installedAbiStatuses(true);
                if (!err.empty()) {
                    appendLog("[load-error] " + err);
                }
                const std::string mode = res.used_prebuilt ? " [prebuilt .tar.zst]" : " [/tmp 源码构建]";
                appendLog("[installed] " + res.plugin_id + " v" + res.version + mode);
                brls::Application::notify("插件安装成功: " + res.plugin_id + mode);
            } else {
                appendLog("[ERROR] " + res.error);
                brls::Application::notify("安装失败: " + res.error);
            }
            refresh();
        });
    }).detach();
}

void PluginStoreView::startLocalInstall(const std::string& dir) {
    busy_ = true;
    appendLog("=== 开始本地开发编译安装 (/tmp staging): " + dir + " ===");
    refresh();
    std::thread([this, alive = alive_, dir]() {
        auto& k = HostKernel::instance();
        auto res = k.installer().installFromLocal(dir, [this, alive](const std::string& stage, const std::string& detail) {
            if (alive->load(std::memory_order_acquire)) {
                appendLog("[" + stage + "] " + detail);
            }
        });
        brls::sync([this, alive, res]() {
            if (!alive->load(std::memory_order_acquire)) {
                return;
            }
            busy_ = false;
            auto& k = HostKernel::instance();
            if (res.ok) {
                selected_plugin_id_ = res.plugin_id;
                radar_results_.erase(res.plugin_id);
                clearBoxViewsSafely(detail_container_);
                config_section_box_ = nullptr;
                first_config_btn_ = nullptr;
                primary_entry_btn_ = nullptr;
                config_edit_buttons_.clear();
                std::string err;
                suppress_bus_refresh_ = true;
                k.plugins().reloadPlugin(res.plugin_id, err);
                suppress_bus_refresh_ = false;
                k.installedAbiStatuses(true);
                if (!err.empty()) appendLog("[load-error] " + err);
                brls::Application::notify("本地插件已安装并激活: " + res.plugin_id);
            } else {
                appendLog("[ERROR] " + res.error);
                brls::Application::notify("本地安装失败: " + res.error);
            }
            refresh();
        });
    }).detach();
}

void PluginStoreView::startUpdate(const std::string& plugin_id) {
    busy_ = true;
    appendLog("=== 开始更新: " + plugin_id + " ===");
    refresh();
    std::thread([this, alive = alive_, plugin_id]() {
        auto& k = HostKernel::instance();
        auto res = k.installer().updatePlugin(plugin_id, [this, alive](const std::string& stage, const std::string& detail) {
            if (alive->load(std::memory_order_acquire)) {
                appendLog("[" + stage + "] " + detail);
            }
        });
        brls::sync([this, alive, plugin_id, res]() {
            if (!alive->load(std::memory_order_acquire)) {
                return;
            }
            busy_ = false;
            auto& k = HostKernel::instance();
            if (res.ok) {
                radar_results_.erase(plugin_id);
                clearBoxViewsSafely(detail_container_);
                config_section_box_ = nullptr;
                first_config_btn_ = nullptr;
                primary_entry_btn_ = nullptr;
                config_edit_buttons_.clear();
                std::string err;
                suppress_bus_refresh_ = true;
                k.plugins().reloadPlugin(plugin_id, err);
                suppress_bus_refresh_ = false;
                k.installedAbiStatuses(true);
                const std::string mode = res.used_prebuilt ? " [prebuilt]" : " [source-build]";
                brls::Application::notify("插件已更新至 v" + res.version + mode);
            } else {
                appendLog("[ERROR] " + res.error);
                brls::Application::notify("更新失败: " + res.error);
            }
            refresh();
        });
    }).detach();
}

void PluginStoreView::startUpdateRadar() {
    if (radar_busy_) {
        return;
    }
    radar_busy_ = true;
    appendLog("=== 更新雷达：扫描已安装插件上游 commit diff 与预编译 Release ===");
    refresh();
    std::thread([this, alive = alive_]() {
        auto& k = HostKernel::instance();
        auto results = k.installer().checkAllUpdates(true);
        brls::sync([this, alive, results]() {
            if (!alive->load(std::memory_order_acquire)) {
                return;
            }
            radar_busy_ = false;
            radar_results_.clear();
            int updates = 0;
            for (const auto& ur : results) {
                radar_results_[ur.plugin_id] = ur;
                if (!ur.ok) {
                    appendLog("[radar] " + ur.plugin_id + ": 检查失败 (" + ur.error + ")");
                } else if (ur.update_available) {
                    ++updates;
                    appendLog("[radar] " + ur.plugin_id + ": 领先 " + std::to_string(ur.ahead_commits) +
                              " commits" + (ur.prebuilt_available ? " [prebuilt .tar.zst]" : " [source]"));
                    if (!ur.changelog.empty()) {
                        appendLog(ur.changelog);
                    }
                    if (!ur.diff_stat.empty()) {
                        appendLog(ur.diff_stat);
                    }
                } else {
                    appendLog("[radar] " + ur.plugin_id + ": 已是最新 (" +
                              (ur.local_commit.size() > 10 ? ur.local_commit.substr(0, 10) : ur.local_commit) + ")");
                }
            }
            if (results.empty()) {
                brls::Application::notify("更新雷达: 当前尚未安装插件");
            } else {
                brls::Application::notify("更新雷达完成: " + std::to_string(results.size()) + " 个已扫，发现 " +
                                          std::to_string(updates) + " 个上游更新");
            }
            refresh();
        });
    }).detach();
}

void PluginStoreView::startRebuildPlugin(const std::string& plugin_id) {
    busy_ = true;
    appendLog("=== 开始按当前宿主 SDK 重编: " + plugin_id + " ===");
    refresh();
    std::thread([this, alive = alive_, plugin_id]() {
        auto& k = HostKernel::instance();
        auto res = k.installer().rebuildPlugin(
            plugin_id, [this, alive](const std::string& stage, const std::string& detail) {
                if (alive->load(std::memory_order_acquire)) {
                    appendLog("[" + stage + "] " + detail);
                }
            });
        brls::sync([this, alive, plugin_id, res]() {
            if (!alive->load(std::memory_order_acquire)) {
                return;
            }
            busy_ = false;
            auto& k = HostKernel::instance();
            if (res.ok) {
                if (k.installer().isEnabled(plugin_id)) {
                    clearBoxViewsSafely(detail_container_);
                    config_section_box_ = nullptr;
                    first_config_btn_ = nullptr;
                    primary_entry_btn_ = nullptr;
                    config_edit_buttons_.clear();
                    std::string err;
                    suppress_bus_refresh_ = true;
                    k.plugins().reloadPlugin(plugin_id, err);
                    suppress_bus_refresh_ = false;
                    if (!err.empty()) {
                        appendLog("[reload-error] " + err);
                    }
                }
                k.installedAbiStatuses(true);
                appendLog("[rebuilt] " + plugin_id + " -> SDK v" +
                          omnidesk::sdkVersion());
                brls::Application::notify("插件已按当前 SDK 重编完成: " + plugin_id);
            } else {
                appendLog("[ERROR] " + res.error);
                brls::Application::notify("重编失败: " + res.error);
            }
            refresh();
        });
    }).detach();
}

void PluginStoreView::startBatchRebuild(bool only_stale) {
    busy_ = true;
    appendLog(only_stale ? "=== 一键重编所有 SDK ABI 过期插件 ==="
                         : "=== 全量按当前宿主 SDK 重编已安装插件 ===");
    refresh();
    std::thread([this, alive = alive_, only_stale]() {
        auto& k = HostKernel::instance();
        auto batch = k.installer().rebuildPlugins(
            only_stale, [this, alive](const std::string& stage, const std::string& detail) {
                if (alive->load(std::memory_order_acquire)) {
                    appendLog("[" + stage + "] " + detail);
                }
            });
        brls::sync([this, alive, batch]() {
            if (!alive->load(std::memory_order_acquire)) {
                return;
            }
            busy_ = false;
            auto& k = HostKernel::instance();
            clearBoxViewsSafely(detail_container_);
            config_section_box_ = nullptr;
            first_config_btn_ = nullptr;
            primary_entry_btn_ = nullptr;
            config_edit_buttons_.clear();
            suppress_bus_refresh_ = true;
            for (const auto& r : batch.results) {
                if (r.ok && k.installer().isEnabled(r.plugin_id)) {
                    std::string err;
                    k.plugins().reloadPlugin(r.plugin_id, err);
                    if (!err.empty()) {
                        appendLog("[reload-error] " + r.plugin_id + ": " + err);
                    }
                } else if (!r.ok) {
                    appendLog("[ERROR] " + r.plugin_id + ": " + r.error);
                }
            }
            suppress_bus_refresh_ = false;
            k.installedAbiStatuses(true);
            std::string summary = "批量重编完成: 成功 " + std::to_string(batch.rebuilt_count) +
                                  " · 跳过 " + std::to_string(batch.skipped_count) +
                                  " · 失败 " + std::to_string(batch.failed_count);
            appendLog("[batch-rebuild] " + summary);
            brls::Application::notify(summary);
            refresh();
        });
    }).detach();
}

} // namespace omnidesk::host
