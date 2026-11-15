#include "omnidesk/host/omni_command_palette.hpp"

#include <atomic>
#include <borealis.hpp>
#include <memory>
#include <string>
#include <vector>

#include "omnidesk/async.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/search_index.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

const char* kindColorToken(const std::string& kind) {
    if (kind == "nas_clip" || kind == "music_review" || kind == "media") return "domain/media";
    if (kind == "book" || kind == "vocab") return "domain/book";
    if (kind == "obsidian" || kind == "article") return "domain/text";
    return "domain/core";
}

class PaletteBox final : public brls::Box {
public:
    PaletteBox(std::string initial_query, CommandPaletteCallbacks callbacks)
        : brls::Box(brls::Axis::COLUMN),
          query_(std::move(initial_query)),
          callbacks_(std::move(callbacks)) {
        setPadding(22);
        setWidth(820);

        auto* hdr = new brls::Box(brls::Axis::ROW);
        hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        hdr->setAlignItems(brls::AlignItems::CENTER);
        hdr->setMarginBottom(12);
        hdr->addView(ui::makeLabel("OmniCommandPalette · Ctrl+K 跨三域全局检索与命令面板", 20.0f, "brls/text"));
        status_label_ = ui::makeLabel("SQLite FTS5", 13.5f, "font/grey");
        hdr->addView(status_label_);
        addView(hdr);

        // Search input row
        auto* search_row = new brls::Box(brls::Axis::ROW);
        search_row->setAlignItems(brls::AlignItems::CENTER);
        search_row->setMarginBottom(10);

        query_btn_ = ui::makeButton(
            query_.empty() ? "输入搜索关键词（台词 / 书名 / 往期精选 / 乐评 / 生词 / 命令）..." : ("关键词: " + query_),
            [this]() { openSearchIme(); },
            false, 38.0f);
        query_btn_->setGrow(1.0f);
        query_btn_->setMarginRight(8);
        search_row->addView(query_btn_);

        auto* clr_btn = ui::makeButton("清空", [this]() {
            query_.clear();
            if (query_btn_) {
                query_btn_->setText("输入搜索关键词（台词 / 书名 / 往期精选 / 乐评 / 生词 / 命令）...");
            }
            runQuery();
        }, false, 38.0f);
        clr_btn->setMarginRight(8);
        search_row->addView(clr_btn);

        search_row->addView(ui::makeButton("重建索引", [this]() { triggerReindex(); }, false, 38.0f));
        addView(search_row);

        // Kind filter pills
        filter_bar_ = new brls::Box(brls::Axis::ROW);
        filter_bar_->setMarginBottom(10);
        rebuildFilters();
        addView(filter_bar_);

        // Scrollable results list
        auto* scroll = new brls::ScrollingFrame();
        scroll->setWidthPercentage(100);
        scroll->setHeight(390);
        results_box_ = new brls::Box(brls::Axis::COLUMN);
        results_box_->setWidthPercentage(100);
        scroll->setContentView(results_box_);
        addView(scroll);

        runQuery();
    }

    ~PaletteBox() override {
        alive_->store(false, std::memory_order_release);
        if (pending_cancel_) {
            pending_cancel_->cancel();
        }
    }

    void setDialog(brls::Dialog* dlg) { dialog_ = dlg; }

private:
    void rebuildFilters() {
        if (!filter_bar_) return;
        filter_bar_->clearViews();
        const struct {
            const char* kind;
            const char* label;
        } filters[] = {
            {"", "全部"},
            {"nas_clip", "飞牛台词"},
            {"book", "书架"},
            {"obsidian", "往期精选"},
            {"music_review", "乐评"},
            {"vocab", "生词卡"},
            {"command", "快捷命令"},
        };
        for (const auto& f : filters) {
            auto* btn = ui::makeButton(f.label, [this, k = std::string(f.kind)]() {
                active_kind_ = k;
                rebuildFilters();
                runQuery();
            }, active_kind_ == f.kind, 34.0f);
            btn->setMarginRight(6);
            filter_bar_->addView(btn);
        }
    }

    void openSearchIme() {
        if (auto* plat = brls::Application::getPlatform()) {
            plat->getImeManager()->openForText(
                [this, alive = alive_](std::string text) {
                    brls::sync([this, alive, text = std::move(text)]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        query_ = text;
                        if (query_btn_) {
                            query_btn_->setText(
                                query_.empty()
                                    ? "输入搜索关键词（台词 / 书名 / 往期精选 / 乐评 / 生词 / 命令）..."
                                    : ("关键词: " + query_));
                        }
                        runQuery();
                    });
                },
                "Ctrl+K 跨三域全局检索",
                "一步检索飞牛台词、书架、Obsidian 往期精选、Apple Music 乐评、SM-2 生词与命令",
                128,
                query_);
        }
    }

    void triggerReindex() {
        auto& kernel = HostKernel::instance();
        if (!kernel.isInitialized()) return;
        if (pending_cancel_) pending_cancel_->cancel();
        auto cancel = CancelToken::create();
        pending_cancel_ = cancel;
        if (status_label_) status_label_->setText("正在重建跨三域 FTS5 索引...");

        kernel.uiTasks().submitInLane(
            "palette.search",
            [this, alive = alive_](const std::shared_ptr<CancelToken>& c) {
                if (c && c->isCancelled()) return;
                auto res = HostKernel::instance().capabilities().callCancellable(
                    "search.index.v1", {{"action", "reindex"}}, c);
                brls::sync([this, alive, c, res]() {
                    if (!alive->load(std::memory_order_acquire) || (c && c->isCancelled())) return;
                    if (res.ok && status_label_) {
                        status_label_->setText(
                            "已索引 " + std::to_string(res.data.value("total_docs", size_t{0})) + " 条记录");
                    }
                    runQuery();
                });
            },
            cancel);
    }

    void runQuery() {
        auto& kernel = HostKernel::instance();
        if (!kernel.isInitialized()) return;
        if (pending_cancel_) pending_cancel_->cancel();
        auto cancel = CancelToken::create();
        pending_cancel_ = cancel;
        const std::string q = query_;
        const std::string k = active_kind_;

        kernel.uiTasks().submitInLane(
            "palette.search",
            [this, alive = alive_, q, k](const std::shared_ptr<CancelToken>& c) {
                if (c && c->isCancelled()) return;
                auto res = HostKernel::instance().capabilities().callCancellable(
                    "search.index.v1",
                    {{"action", "query"}, {"q", q}, {"kind", k}, {"limit", 36}},
                    c);
                brls::sync([this, alive, c, res]() {
                    if (!alive->load(std::memory_order_acquire) || (c && c->isCancelled())) return;
                    renderResults(res);
                });
            },
            cancel);
    }

    void renderResults(const CapabilityResult& res) {
        if (!results_box_) return;
        results_box_->clearViews();
        if (!res.ok || !res.data.is_object()) {
            results_box_->addView(ui::makeLabel(
                res.error.empty() ? "检索服务未就绪" : res.error, 14.0f, "status/err_fg"));
            return;
        }
        if (active_kind_.empty() || active_kind_ == "command") {
            auto add_quick_cmd = [&](const std::string& label, const std::string& sub,
                                     const std::string& cmd) {
                if (!query_.empty() &&
                    label.find(query_) == std::string::npos &&
                    sub.find(query_) == std::string::npos &&
                    cmd.find(query_) == std::string::npos) {
                    return;
                }
                auto* card = ui::makeCard("desktop/soft", 12.0f, 10.0f);
                card->setMarginBottom(8);
                card->setFocusable(true);
                card->addView(ui::makeLabel(label, 15.5f, "brls/text"));
                auto* sub_lbl = ui::makeLabel(sub, 13.5f, "font/grey");
                sub_lbl->setMarginTop(4);
                card->addView(sub_lbl);
                card->registerClickAction([this, cmd](brls::View*) {
                    auto cb = callbacks_.on_execute_command;
                    if (dialog_) dialog_->dismiss();
                    if (cb) brls::sync([cb, cmd]() { cb(cmd, nlohmann::json::object()); });
                    return true;
                });
                results_box_->addView(card);
            };
            add_quick_cmd("📅 打开推荐日历与每日清单 (Calendar)",
                          "按日期/领域浏览全部历史与今日 AI 推荐", "curation:calendar");
            add_quick_cmd("⚙ 每日定时推荐开关与后台 Agent 调度",
                          "查看今日 4 个定时工作流状态、手动开关或立即补跑", "curation:scheduler");
        }

        const auto hits = res.data.value("hits", nlohmann::json::array());
        const double elapsed_ms = res.data.value("elapsed_ms", 0.0);
        if (status_label_) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "命中 %zu 条 · %.1f ms", hits.size(), elapsed_ms);
            status_label_->setText(buf);
        }
        if (!hits.is_array() || hits.empty()) {
            auto* empty_card = ui::makeCard("desktop/soft", 16.0f, 8.0f);
            empty_card->addView(ui::makeLabel(
                "未命中相关条目。可点击「重建索引」同步最新插件数据，或换一个关键词重试。",
                14.5f, "font/grey"));
            results_box_->addView(empty_card);
            return;
        }

        for (const auto& hit : hits) {
            if (!hit.is_object()) continue;
            const std::string kind = hit.value("kind", "article");
            const std::string kind_label =
                hit.value("kind_label", SearchIndexService::kindDisplayLabel(kind));
            const std::string title = hit.value("title", "");
            const std::string subtitle = hit.value("subtitle", "");
            const std::string snippet = hit.value("snippet", "");
            const nlohmann::json payload =
                hit.contains("payload") && hit["payload"].is_object()
                    ? hit["payload"]
                    : nlohmann::json::object();

            auto* card = ui::makeCard("desktop/soft", 12.0f, 10.0f);
            card->setMarginBottom(8);
            card->setFocusable(true);

            auto* top = new brls::Box(brls::Axis::ROW);
            top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            top->setAlignItems(brls::AlignItems::CENTER);

            auto* left_row = new brls::Box(brls::Axis::ROW);
            left_row->setAlignItems(brls::AlignItems::CENTER);
            left_row->setGrow(1.0f);
            auto* pill = ui::makePill(kind_label, "desktop/surface", kindColorToken(kind));
            pill->setMarginRight(8);
            left_row->addView(pill);

            auto* ttl = ui::makeLabel(title, 15.5f, "brls/text", 1.3f);
            ttl->setSingleLine(true);
            ttl->setGrow(1.0f);
            left_row->addView(ttl);
            top->addView(left_row);

            if (!subtitle.empty()) {
                auto* sub_lbl = ui::makeLabel(subtitle, 13.0f, "font/grey");
                sub_lbl->setSingleLine(true);
                sub_lbl->setMarginLeft(10);
                top->addView(sub_lbl);
            }
            card->addView(top);

            if (!snippet.empty() && snippet != subtitle) {
                auto* sn = ui::makeLabel(snippet, 13.5f, "font/grey", 1.4f);
                sn->setMarginTop(6);
                card->addView(sn);
            }

            card->registerClickAction([this, kind, payload](brls::View*) {
                activateHit(kind, payload);
                return true;
            });
            results_box_->addView(card);
        }
    }

    void activateHit(const std::string& kind, const nlohmann::json& payload) {
        auto& kernel = HostKernel::instance();
        if (!kernel.isInitialized()) return;

        const std::string target = payload.value("target", kind);
        if (target == "command") {
            const std::string cmd = payload.value("command", "");
            if (cmd == "reindex_search") {
                triggerReindex();
                return;
            }
            auto cb = callbacks_.on_execute_command;
            if (dialog_) dialog_->dismiss();
            if (cb) cb(cmd, payload);
            return;
        }
        if (target == "workspace_tab") {
            const std::string tab_id = payload.value("tab_id", "");
            auto cb = callbacks_.on_open_workspace_tab;
            if (dialog_) dialog_->dismiss();
            if (cb && !tab_id.empty()) cb(tab_id);
            return;
        }
        if (target == "vocab") {
            const std::string word = payload.value("word", "");
            auto cb = callbacks_.on_open_vocab;
            if (dialog_) dialog_->dismiss();
            if (cb) {
                cb(word);
            } else {
                kernel.bus().publish("entity.book.open", {
                    {"sub_mode", "vocab"},
                    {"word", word},
                    {"title", "SM-2 生词卡 · " + word},
                });
            }
            return;
        }
        if (target == "book" || kind == "book") {
            if (dialog_) dialog_->dismiss();
            kernel.bus().publish("entity.book.open", payload);
            return;
        }
        if (target == "nas_clip" || target == "media" ||
            kind == "nas_clip" || kind == "media" || kind == "music_review") {
            if (dialog_) dialog_->dismiss();
            kernel.bus().publish("entity.media.open", payload);
            return;
        }
        if (target == "text" || kind == "obsidian" || kind == "article") {
            if (dialog_) dialog_->dismiss();
            kernel.bus().publish("entity.text.open", payload);
            return;
        }
    }

    std::string query_;
    std::string active_kind_;
    CommandPaletteCallbacks callbacks_;
    brls::Dialog* dialog_ = nullptr;
    brls::Button* query_btn_ = nullptr;
    brls::Label* status_label_ = nullptr;
    brls::Box* filter_bar_ = nullptr;
    brls::Box* results_box_ = nullptr;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> pending_cancel_;
};

} // namespace

void OmniCommandPalette::open(const std::string& initial_query,
                              CommandPaletteCallbacks callbacks) {
    if (!brls::Application::getPlatform()) {
        return;
    }
    auto* box = new PaletteBox(initial_query, std::move(callbacks));
    auto* dlg = new brls::Dialog(box);
    box->setDialog(dlg);
    dlg->addButton("关闭 Esc", [] {});
    dlg->open();
}

} // namespace omnidesk::host
