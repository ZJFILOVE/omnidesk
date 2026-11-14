#include "omnidesk/host/plugin_store_view.hpp"

#include <algorithm>
#include <thread>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/pm/scaffold.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

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
                        const std::string& prefix = "") {
    auto* row = new brls::Box(brls::Axis::ROW);
    row->setAlignItems(brls::AlignItems::CENTER);
    for (size_t i = 0; i < items.size() && i < 4; ++i) {
        auto* pill = ui::makePill(prefix + items[i], bg_token, fg_token);
        pill->setMarginRight(6);
        row->addView(pill);
    }
    if (items.size() > 4) {
        auto* more = ui::makePill("+" + std::to_string(items.size() - 4), bg_token, fg_token);
        row->addView(more);
    }
    return row;
}

} // namespace

PluginStoreView::PluginStoreView() : brls::Box(brls::Axis::COLUMN) {
    setGrow(1.0f);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    // Top Header Row
    auto* header = new brls::Box(brls::Axis::ROW);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(12);

    auto* title_col = new brls::Box(brls::Axis::COLUMN);
    auto* title = ui::makeLabel("插件市场 · Plugin Marketplace", 24.0f, "brls/text");
    title->setMarginBottom(3);
    summary_label_ = ui::makeLabel("独立目录 & GitHub 仓库构建 · ABI v1 热插拔底座", 14.5f, "font/grey");
    title_col->addView(title);
    title_col->addView(summary_label_);
    header->addView(title_col);

    auto* actions = new brls::Box(brls::Axis::ROW);
    actions->setAlignItems(brls::AlignItems::CENTER);

    auto* search_btn = ui::makeButton("搜索 /", [this]() { promptSearch(); }, false, 38.0f);
    search_btn->setMarginRight(8);
    actions->addView(search_btn);

    auto* radar_btn = ui::makeButton("更新雷达", [this]() { startUpdateRadar(); }, false, 38.0f);
    radar_btn->setMarginRight(8);
    actions->addView(radar_btn);

    auto* gh_btn = ui::makeButton("+ GitHub 安装", [this]() { openInstallModal(false); }, true, 38.0f);
    gh_btn->setMarginRight(8);
    actions->addView(gh_btn);

    auto* local_btn = ui::makeButton("+ 本地源码安装", [this]() { openInstallModal(true); }, false, 38.0f);
    local_btn->setMarginRight(8);
    actions->addView(local_btn);

    auto* new_btn = ui::makeButton("+ 脚手架", [this]() { openScaffoldModal(); }, false, 38.0f);
    actions->addView(new_btn);

    header->addView(actions);
    addView(header);

    // Filter Bar
    filter_bar_ = new brls::Box(brls::Axis::ROW);
    filter_bar_->setAlignItems(brls::AlignItems::CENTER);
    filter_bar_->setMarginBottom(14);
    addView(filter_bar_);

    // Master-Detail Split Body
    auto* split = new brls::Box(brls::Axis::ROW);
    split->setGrow(1.0f);
    split->setWidthPercentage(100);

    auto* list_scroll = new brls::ScrollingFrame();
    list_scroll->setWidthPercentage(54);
    list_scroll->setHeightPercentage(100);
    list_scroll->setMarginRight(14);
    list_container_ = new brls::Box(brls::Axis::COLUMN);
    list_container_->setWidthPercentage(100);
    list_scroll->setContentView(list_container_);
    split->addView(list_scroll);

    auto* right_col = new brls::Box(brls::Axis::COLUMN);
    right_col->setWidthPercentage(46);
    right_col->setHeightPercentage(100);

    auto* detail_scroll = new brls::ScrollingFrame();
    detail_scroll->setGrow(1.0f);
    detail_scroll->setWidthPercentage(100);
    detail_container_ = new brls::Box(brls::Axis::COLUMN);
    detail_container_->setWidthPercentage(100);
    detail_scroll->setContentView(detail_container_);
    right_col->addView(detail_scroll);

    // Live Build & Verify Log Console
    auto* console_card = ui::makeCard("desktop/soft", 12.0f, 10.0f);
    console_card->setHeight(155);
    console_card->setMarginTop(10);

    auto* console_hdr = new brls::Box(brls::Axis::ROW);
    console_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    console_hdr->setMarginBottom(6);
    console_hdr->addView(ui::makeLabel("构建与验证日志 (CMake / ABI / Load)", 13.5f, "font/grey"));
    auto* clr_btn = ui::makeButton("清空", [this]() {
        std::lock_guard<std::mutex> lock(log_mutex_);
        log_buffer_.clear();
        if (console_label_) {
            console_label_->setText("就绪。点击插件「安装」或「更新」查看完整构建流水线输出。");
        }
    }, false, 26.0f);
    console_hdr->addView(clr_btn);
    console_card->addView(console_hdr);

    auto* console_scroll = new brls::ScrollingFrame();
    console_scroll->setGrow(1.0f);
    console_scroll->setWidthPercentage(100);
    console_label_ = ui::makeLabel("就绪。点击插件「安装」或「更新」查看完整构建流水线输出。", 13.0f, "brls/text", 1.35f);
    console_scroll->setContentView(console_label_);
    console_card->addView(console_scroll);

    right_col->addView(console_card);
    split->addView(right_col);
    addView(split);

    bus_sub_token_ = HostKernel::instance().bus().subscribe(
        "plugin.state_changed", "host.plugin_store",
        [this, alive = alive_](const std::string&, const nlohmann::json&) {
            brls::sync([this, alive]() {
                if (alive->load(std::memory_order_acquire)) {
                    refresh();
                }
            });
        });

    refresh();
}

PluginStoreView::~PluginStoreView() {
    alive_->store(false, std::memory_order_release);
    if (HostKernel::instance().isInitialized() && bus_sub_token_ != 0) {
        HostKernel::instance().bus().unsubscribe(bus_sub_token_);
    }
}

void PluginStoreView::appendLog(const std::string& line) {
    {
        std::lock_guard<std::mutex> lock(log_mutex_);
        if (log_buffer_.size() > 6000) {
            log_buffer_.erase(0, log_buffer_.size() - 4000);
        }
        log_buffer_ += line;
        if (!line.empty() && line.back() != '\n') {
            log_buffer_ += '\n';
        }
    }
    brls::sync([this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire) || !console_label_) return;
        std::lock_guard<std::mutex> lock(log_mutex_);
        console_label_->setText(log_buffer_);
    });
}

void PluginStoreView::refresh() {
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

    std::string summary = "目录 " + std::to_string(cat_items.size()) + " 个 · 已安装 " +
                          std::to_string(installed.size()) + " 个 · 运行中 " +
                          std::to_string(loaded_count) + " 个";
    if (!search_query_.empty()) {
        summary += " · 筛选: \"" + search_query_ + "\"";
    }
    if (summary_label_) {
        summary_label_->setText(summary);
    }

    // Filter tabs
    filter_bar_->clearViews();
    struct TabDef {
        FilterTab tab;
        const char* label;
    };
    const TabDef tabs[] = {
        {FilterTab::All, "全部"},
        {FilterTab::Core, "底座与AI"},
        {FilterTab::Media, "影视音乐"},
        {FilterTab::Text, "文字期刊"},
        {FilterTab::Book, "书本PDF"},
        {FilterTab::Installed, "已安装"},
        {FilterTab::Diagnostics, "能力诊断"},
    };

    for (const auto& t : tabs) {
        const bool active = (filter_tab_ == t.tab);
        auto* btn = ui::makeButton(t.label, [this, tab = t.tab]() {
            brls::sync([this, tab]() {
                filter_tab_ = tab;
                refresh();
            });
        }, active, 34.0f);
        btn->setMarginRight(8);
        filter_bar_->addView(btn);
    }

    if (!search_query_.empty()) {
        auto* clr = ui::makeButton("清除搜索", [this]() {
            brls::sync([this]() {
                search_query_.clear();
                refresh();
            });
        }, false, 34.0f);
        filter_bar_->addView(clr);
    }

    rebuildList();
}

void PluginStoreView::rebuildList() {
    list_container_->clearViews();
    if (filter_tab_ == FilterTab::Diagnostics) {
        renderDiagnosticsView();
        return;
    }
    renderCatalogOrInstalledList();
}

void PluginStoreView::renderCatalogOrInstalledList() {
    auto& kernel = HostKernel::instance();
    const auto installed = kernel.installer().listInstalled();

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
            auto rec = kernel.installer().getInstalled(item.id);
            auto st = kernel.plugins().statusOf(item.id);
            rows.push_back({
                item.id, item.name, rec ? rec->manifest.version : item.version,
                item.kind, item.domain, item.description, item.provides, item.consumes,
                rec.has_value(), st && st->loaded, rec ? rec->enabled : false,
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
        detail_container_->clearViews();
        return;
    }

    if (selected_plugin_id_.empty() ||
        std::none_of(rows.begin(), rows.end(), [&](const Entry& e) { return e.id == selected_plugin_id_; })) {
        selected_plugin_id_ = rows.front().id;
    }

    for (const auto& e : rows) {
        const bool selected = (e.id == selected_plugin_id_);
        auto* card = ui::makeCard(selected ? "desktop/active" : "desktop/surface", 14.0f, 10.0f);
        card->setMarginBottom(10);
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
        name_row->addView(ui::makeLabel(e.name, 17.5f, "brls/text"));
        auto* ver = ui::makePill("v" + e.version, "desktop/soft", "font/grey");
        ver->setMarginLeft(8);
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
        top->addView(ui::makePill(st_text, st_bg, st_fg));
        card->addView(top);

        auto* desc = ui::makeLabel(e.description, 14.5f, "font/grey", 1.45f);
        desc->setMarginBottom(8);
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
            meta->addView(makeBadgeRow(e.provides, "status/info_bg", "status/info_fg", "⊕ "));
        }
        card->addView(meta);

        card->registerClickAction([this, id = e.id](brls::View*) {
            brls::sync([this, id]() {
                selected_plugin_id_ = id;
                rebuildList();
            });
            return true;
        });
        list_container_->addView(card);
    }

    renderDetail(selected_plugin_id_);
}

void PluginStoreView::renderDiagnosticsView() {
    auto& kernel = HostKernel::instance();
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
            row->setMarginTop(8);
            row->addView(ui::makePill(c.capability_id, "status/info_bg", "status/info_fg"));
            row->addView(ui::makeLabel(c.plugin_id + " (priority=" + std::to_string(c.priority) + ")", 14.5f, "brls/text"));
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

    detail_container_->clearViews();
    auto* help = ui::makeCard("desktop/surface", 16.0f, 10.0f);
    help->addView(ui::makeLabel("插件解耦架构说明", 18.0f, "brls/text"));
    help->addView(ui::makeLabel(
        "• 每个插件拥有独立目录与 GitHub 仓库，依赖 OmniDesk::SDK (libomnidesk_sdk.so)。\n"
        "• 安装流程: git clone -> staging CMake 编译 -> ctest 验证 -> 子进程 dlopen ABI 握手 -> SHA-256 锁定 -> 备份快照并激活。\n"
        "• 配置与密钥保存在 0600 权限的 plugins/<id>.json 与 SecretVault，卸载前可安全保留或回滚。",
        14.5f, "font/grey", 1.55f));
    detail_container_->addView(help);
}

void PluginStoreView::renderDetail(const std::string& plugin_id) {
    detail_container_->clearViews();
    auto& kernel = HostKernel::instance();

    auto rec = kernel.installer().getInstalled(plugin_id);
    auto cat = kernel.catalog().findById(plugin_id);
    auto st = kernel.plugins().statusOf(plugin_id);

    if (!rec.has_value() && !cat.has_value()) {
        return;
    }

    auto* card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    const std::string name = rec ? rec->manifest.name : cat->name;
    const std::string ver = rec ? rec->manifest.version : cat->version;
    const std::string desc = rec ? rec->manifest.description : cat->description;
    const std::string repo = rec ? rec->source_url : cat->repo_url;
    const std::string branch = rec ? rec->branch : cat->repo_branch;
    const auto provides = rec ? rec->manifest.provides : cat->provides;
    const auto consumes = rec ? rec->manifest.consumes : cat->consumes;

    card->addView(ui::makeLabel(name, 21.0f, "brls/text"));
    auto* sub = ui::makeLabel(plugin_id + " · v" + ver + " · " + (rec ? rec->manifest.author : cat->author),
                              14.0f, "font/grey");
    sub->setMarginTop(4);
    sub->setMarginBottom(10);
    card->addView(sub);

    auto* d_lbl = ui::makeLabel(desc, 15.0f, "brls/text", 1.5f);
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
        auto* l_lbl = ui::makeLabel(lock_txt, 13.0f, "font/grey");
        l_lbl->setMarginBottom(10);
        card->addView(l_lbl);

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

    if (st && !st->load_error.empty()) {
        auto* err_card = ui::makeCard("status/err_bg", 10.0f, 8.0f);
        err_card->setMarginBottom(12);
        err_card->addView(ui::makeLabel("加载失败: " + st->load_error, 14.0f, "status/err_fg"));
        card->addView(err_card);
    }

    // Action Bar
    auto* act = new brls::Box(brls::Axis::ROW);
    act->setMarginTop(4);
    if (!rec.has_value()) {
        auto* inst_btn = ui::makeButton(
            busy_ ? "正在安装..." : "安装插件 (优先 .tar.zst)",
            [this, repo, branch]() {
                if (!busy_) {
                    startGitInstall(repo, branch);
                }
            },
            true, 40.0f);
        act->addView(inst_btn);
    } else {
        const bool is_loaded = st && st->loaded;
        auto* toggle_btn = ui::makeButton(
            rec->enabled ? (is_loaded ? "停用" : "启用并加载") : "启用",
            [this, plugin_id, enabled = rec->enabled, is_loaded]() {
                brls::sync([this, plugin_id, enabled, is_loaded]() {
                    auto& k = HostKernel::instance();
                    std::string err;
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
                    refresh();
                });
            },
            !is_loaded, 38.0f);
        toggle_btn->setMarginRight(8);
        act->addView(toggle_btn);

        auto rit = radar_results_.find(plugin_id);
        const bool radar_has_update = (rit != radar_results_.end() && rit->second.ok && rit->second.update_available);
        std::string upd_label = "更新/重编译";
        if (radar_has_update) {
            upd_label = "立即更新 (" + std::to_string(rit->second.ahead_commits) + " commits)";
        }
        auto* upd_btn = ui::makeButton(
            upd_label,
            [this, plugin_id]() {
                if (!busy_) startUpdate(plugin_id);
            },
            radar_has_update, 38.0f);
        upd_btn->setMarginRight(8);
        act->addView(upd_btn);

        if (!rec->backup_versions.empty()) {
            auto* rb_btn = ui::makeButton("回滚", [this, plugin_id]() {
                brls::sync([this, plugin_id]() {
                    auto& k = HostKernel::instance();
                    k.plugins().unloadPlugin(plugin_id);
                    auto r = k.installer().rollbackPlugin(plugin_id);
                    if (r.ok) {
                        std::string err;
                        k.plugins().loadPlugin(plugin_id, err);
                        appendLog("Rolled back " + plugin_id + " -> v" + r.version);
                    } else {
                        appendLog("Rollback failed: " + r.error);
                    }
                    refresh();
                });
            }, false, 38.0f);
            rb_btn->setMarginRight(8);
            act->addView(rb_btn);
        }

        auto* rm_btn = ui::makeButton("卸载", [this, plugin_id]() {
            brls::sync([this, plugin_id]() {
                auto& k = HostKernel::instance();
                k.plugins().unloadPlugin(plugin_id);
                std::string err;
                if (k.installer().removePlugin(plugin_id, err)) {
                    appendLog("Removed " + plugin_id);
                } else {
                    appendLog("Remove failed: " + err);
                }
                refresh();
            });
        }, false, 38.0f);
        act->addView(rm_btn);
    }
    card->addView(act);

    if (rec.has_value()) {
        card->addView(ui::makeDivider(14.0f));
        renderConfigSection(card, *rec);
    }

    detail_container_->addView(card);
}

void PluginStoreView::renderConfigSection(brls::Box* container, const pm::InstalledPluginRecord& rec) {
    auto& kernel = HostKernel::instance();
    container->addView(ui::makeLabel("插件配置与凭据 (config_schema)", 17.0f, "brls/text"));

    // Custom settings view override from plugin if provided
    if (auto* custom = kernel.plugins().createCustomSettingsView(rec.manifest.id)) {
        container->addView(custom);
        return;
    }

    if (rec.manifest.config_schema.empty()) {
        auto* none = ui::makeLabel("此插件未声明可配置参数。", 14.0f, "font/grey");
        none->setMarginTop(6);
        container->addView(none);
        return;
    }

    const nlohmann::json cfg = kernel.config().loadPluginConfig(rec.manifest.id);

    for (const auto& field : rec.manifest.config_schema) {
        auto* row = ui::makeCard("desktop/soft", 10.0f, 8.0f);
        row->setMarginTop(8);

        auto* top = new brls::Box(brls::Axis::ROW);
        top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top->setAlignItems(brls::AlignItems::CENTER);

        auto* label_col = new brls::Box(brls::Axis::COLUMN);
        label_col->setGrow(1.0f);
        label_col->addView(ui::makeLabel(field.label + " (" + field.key + ")", 15.0f, "brls/text"));
        if (!field.description.empty()) {
            label_col->addView(ui::makeLabel(field.description, 13.0f, "font/grey"));
        }
        top->addView(label_col);

        std::string cur_val = field.default_value;
        if (field.type == ConfigFieldType::Secret) {
            cur_val = kernel.secrets().hasSecret(rec.manifest.id, field.key) ? "•••••• (已加密存储)" : "未设置";
        } else if (cfg.contains(field.key)) {
            if (cfg[field.key].is_string()) cur_val = cfg[field.key].get<std::string>();
            else if (cfg[field.key].is_boolean()) cur_val = cfg[field.key].get<bool>() ? "true" : "false";
            else if (cfg[field.key].is_number()) cur_val = cfg[field.key].dump();
        }

        auto* edit_btn = ui::makeButton(cur_val.empty() ? "设置..." : cur_val, [this, id = rec.manifest.id, field, cur_val]() {
            auto& k = HostKernel::instance();
            if (field.type == ConfigFieldType::Bool) {
                bool next = (cur_val != "true");
                k.config().setPluginValue(id, field.key, next);
                std::string err;
                if (k.plugins().isLoaded(id)) k.plugins().reloadPlugin(id, err);
                brls::sync([this]() { refresh(); });
                return;
            }
            if (field.type == ConfigFieldType::Select && !field.options.empty()) {
                size_t idx = 0;
                for (size_t i = 0; i < field.options.size(); ++i) {
                    if (field.options[i] == cur_val) {
                        idx = (i + 1) % field.options.size();
                        break;
                    }
                }
                k.config().setPluginValue(id, field.key, field.options[idx]);
                std::string err;
                if (k.plugins().isLoaded(id)) k.plugins().reloadPlugin(id, err);
                brls::sync([this]() { refresh(); });
                return;
            }
            if (auto* plat = brls::Application::getPlatform()) {
                plat->getImeManager()->openForText(
                    [this, id, field](std::string val) {
                        auto& hk = HostKernel::instance();
                        if (field.type == ConfigFieldType::Secret) {
                            hk.secrets().setSecret(id, field.key, val);
                        } else if (field.type == ConfigFieldType::Int) {
                            try {
                                hk.config().setPluginValue(id, field.key, std::stoll(val));
                            } catch (...) {
                                hk.config().setPluginValue(id, field.key, val);
                            }
                        } else {
                            hk.config().setPluginValue(id, field.key, val);
                        }
                        std::string err;
                        if (hk.plugins().isLoaded(id)) hk.plugins().reloadPlugin(id, err);
                        brls::sync([this]() { refresh(); });
                    },
                    "配置 " + field.label,
                    field.description,
                    256,
                    field.type == ConfigFieldType::Secret ? "" : cur_val);
            }
        }, false, 34.0f);
        edit_btn->setMarginLeft(10);
        top->addView(edit_btn);
        row->addView(top);
        container->addView(row);
    }
}

void PluginStoreView::promptSearch() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this](std::string q) {
                brls::sync([this, q]() {
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
            [this, local_mode](std::string input) {
                if (input.empty()) return;
                if (local_mode) {
                    startLocalInstall(input);
                } else {
                    startGitInstall(input, "");
                }
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
            [this](std::string dir_and_id) {
                if (dir_and_id.empty()) return;
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
                std::string err;
                k.plugins().reloadPlugin(res.plugin_id, err);
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
                std::string err;
                k.plugins().reloadPlugin(res.plugin_id, err);
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
                std::string err;
                k.plugins().reloadPlugin(plugin_id, err);
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

} // namespace omnidesk::host
