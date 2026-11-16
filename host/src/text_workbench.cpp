#include "omnidesk/host/text_workbench.hpp"
#include "omnidesk/host/async_load.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/recycling_grid.hpp"
#include "omnidesk/host/ui_comfort.hpp"

#include "omnidesk/host/kernel.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/markdown_view.hpp"
#include "omnidesk/selection_action_popover.hpp"
#include "omnidesk/ui_theme.hpp"

#include <SDL2/SDL.h>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <functional>
#include <future>
#include <set>
#include <sstream>

namespace omnidesk::host {
namespace {

bool isIsoDateStr(const std::string& s) {
    if (s.size() != 10 || s[4] != '-' || s[7] != '-') return false;
    for (size_t i = 0; i < s.size(); ++i) {
        if (i == 4 || i == 7) continue;
        if (!std::isdigit(static_cast<unsigned char>(s[i]))) return false;
    }
    return true;
}

std::string stripLeadingHashes(const std::string& raw) {
    size_t i = 0;
    while (i < raw.size() && (raw[i] == ' ' || raw[i] == '\t')) ++i;
    size_t hashes = 0;
    while (i + hashes < raw.size() && raw[i + hashes] == '#' && hashes < 6) ++hashes;
    if (hashes > 0 && i + hashes < raw.size() &&
        (raw[i + hashes] == ' ' || raw[i + hashes] == '\t')) {
        size_t start = i + hashes;
        while (start < raw.size() && (raw[start] == ' ' || raw[start] == '\t')) ++start;
        return raw.substr(start);
    }
    return raw;
}

std::string ensureQuoteMarkdown(const std::string& raw) {
    std::istringstream iss(raw);
    std::string line;
    std::string out;
    bool first = true;
    while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        size_t ns = 0;
        while (ns < line.size() && (line[ns] == ' ' || line[ns] == '\t')) ++ns;
        if (!first) out += '\n';
        first = false;
        if (ns < line.size() && line[ns] == '>') {
            out += line;
        } else if (ns == line.size()) {
            out += ">";
        } else {
            out += "> " + line;
        }
    }
    return out;
}

std::string ensureCodeFenceMarkdown(const std::string& raw) {
    size_t i = 0;
    while (i < raw.size() && (raw[i] == ' ' || raw[i] == '\t' || raw[i] == '\n')) ++i;
    if (raw.compare(i, 3, "```") == 0 || raw.compare(i, 3, "~~~") == 0) {
        return raw;
    }
    return "```\n" + raw + (raw.empty() || raw.back() == '\n' ? "" : "\n") + "```";
}

std::string ensureFormulaMarkdown(const std::string& raw) {
    size_t i = 0;
    while (i < raw.size() && (raw[i] == ' ' || raw[i] == '\t' || raw[i] == '\n')) ++i;
    if (raw.compare(i, 2, "$$") == 0) {
        return raw;
    }
    return "$$\n" + raw + (raw.empty() || raw.back() == '\n' ? "" : "\n") + "$$";
}

std::string extractTranslationTag(const std::string& raw) {
    const std::string open_tag = "<translation>";
    const std::string close_tag = "</translation>";
    const size_t start = raw.find(open_tag);
    if (start == std::string::npos) {
        return raw;
    }
    const size_t body_start = start + open_tag.size();
    const size_t end = raw.find(close_tag, body_start);
    if (end == std::string::npos) {
        return raw.substr(body_start);
    }
    return raw.substr(body_start, end - body_start);
}

std::string resolveJournalPdfUrl(const TextArticle& article) {
    const std::string& u = article.url;
    if (u.empty()) return "";
    if (u.find(".pdf") != std::string::npos) return u;
    const size_t pos = u.find("arxiv.org/abs/");
    if (pos != std::string::npos) {
        std::string id = u.substr(pos + std::string("arxiv.org/abs/").size());
        const size_t q = id.find_first_of("?#");
        if (q != std::string::npos) id = id.substr(0, q);
        if (!id.empty()) return "https://arxiv.org/pdf/" + id + ".pdf";
    }
    return "";
}

bool isEphemeralText(const TextFeedItem& item) {
    if (item.category == "interactive") return true;
    return std::any_of(item.tags.begin(), item.tags.end(), [](const std::string& tag) {
        return tag.find("interactive_ephemeral") != std::string::npos;
    });
}

bool itemMatchesCategory(const TextFeedItem& item,
                         const std::string& source_category,
                         const std::string& active_category) {
    if (active_category == "all") return true;
    if (item.category == active_category || source_category == active_category) return true;
    return std::find(item.tags.begin(), item.tags.end(), active_category) != item.tags.end();
}

void applyTranslatedCommentsJson(std::vector<CommentNode>& nodes, const nlohmann::json& arr) {
    if (!arr.is_array()) return;
    const size_t n = std::min(nodes.size(), arr.size());
    for (size_t i = 0; i < n; ++i) {
        if (arr[i].is_object()) {
            const std::string tr = arr[i].value("translated_body", "");
            if (!tr.empty()) nodes[i].translated_body = tr;
            if (arr[i].contains("replies")) {
                applyTranslatedCommentsJson(nodes[i].replies, arr[i]["replies"]);
            }
        }
    }
}

int estimateWrappedLines(const std::string& raw_md, float font_size, float avail_width, int max_lines) {
    const std::string plain = stripMarkdownToPlainText(raw_md, true);
    if (plain.empty()) return 0;
    float est_px = 0.0f;
    size_t i = 0;
    while (i < plain.size()) {
        const unsigned char ch = static_cast<unsigned char>(plain[i]);
        size_t adv = 1;
        if ((ch & 0x80u) == 0) {
            adv = 1;
            est_px += font_size * 0.56f;
        } else {
            if ((ch & 0xE0u) == 0xC0u) adv = 2;
            else if ((ch & 0xF0u) == 0xE0u) adv = 3;
            else if ((ch & 0xF8u) == 0xF0u) adv = 4;
            est_px += font_size * 1.02f;
        }
        i += std::min(adv, plain.size() - i);
    }
    const float w = std::max(140.0f, avail_width);
    const int lines = std::max(1, static_cast<int>(std::ceil(est_px / w)));
    return max_lines > 0 ? std::min(max_lines, lines) : lines;
}

class TextFeedCardCell final : public RecyclingGridCell {
public:
    TextFeedCardCell() : RecyclingGridCell(brls::Axis::COLUMN) {
        setWidthPercentage(100);
        setPadding(16.0f);
        setBackgroundColor(ui::color("desktop/surface"));
        setCornerRadius(12.0f);
        setHighlightCornerRadius(12.0f);
        setHideHighlightBackground(true);
        setHideHighlightBorder(true);
        setBorderThickness(1);
        setBorderColor(ui::color("desktop/line"));
        setFocusable(true);

        top_row_ = new brls::Box(brls::Axis::ROW);
        top_row_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top_row_->setAlignItems(brls::AlignItems::CENTER);
        top_row_->setMarginBottom(7);

        source_pill_box_ = new brls::Box(brls::Axis::ROW);
        source_pill_box_->setPadding(4, 10, 4, 10);
        source_pill_box_->setCornerRadius(7);
        source_pill_box_->setAlignItems(brls::AlignItems::CENTER);
        source_pill_lbl_ = ui::makeLabel("", 13.0f, "domain/text", 1.2f);
        source_pill_lbl_->setSingleLine(true);
        source_pill_box_->addView(source_pill_lbl_);
        top_row_->addView(source_pill_box_);

        auth_pill_box_ = ui::makePill("需登录 · 点击直接登录", "status/warn_bg", "status/warn_fg");
        top_row_->addView(auth_pill_box_);

        stats_lbl_ = ui::makeLabel("", 13.0f, "font/grey");
        stats_lbl_->setSingleLine(true);
        top_row_->addView(stats_lbl_);
        addView(top_row_);

        prov_lbl_ = ui::makeLabel("", 13.0f, "font/grey");
        prov_lbl_->setMarginBottom(4);
        prov_lbl_->setSingleLine(true);
        addView(prov_lbl_);

        title_md_ = ui::makeMarkdownInline("", 16.2f, "brls/text", 1.42f, 3, false);
        addView(title_md_);

        excerpt_md_ = ui::makeMarkdownInline("", 14.2f, "font/grey", 1.48f, 3, false);
        addView(excerpt_md_);

        registerClickAction([this](brls::View*) {
            if (on_activate_) on_activate_();
            return true;
        });

        addGestureRecognizer(new brls::TapGestureRecognizer(
            [this](brls::TapGestureStatus status, brls::Sound* soundToPlay) {
                if (!isFocusable() || getVisibility() != brls::Visibility::VISIBLE) return;
                if (status.state == brls::GestureState::START ||
                    status.state == brls::GestureState::UNSURE) {
                    playClickAnimation(false, false);
                } else if (status.state == brls::GestureState::FAILED ||
                           status.state == brls::GestureState::INTERRUPTED) {
                    playClickAnimation(true);
                } else if (status.state == brls::GestureState::END) {
                    playClickAnimation(true);
                    brls::Application::giveFocus(this);
                    if (soundToPlay) *soundToPlay = brls::SOUND_CLICK;
                    if (on_activate_) on_activate_();
                }
            }));
    }

    void prepareForReuse() override {
        on_activate_ = nullptr;
    }

    void bind(const TextFeedItem& item,
              bool selected,
              bool auth_card,
              const std::string& prov_date,
              std::function<void()> on_activate) {
        on_activate_ = std::move(on_activate);
        ui_comfort::styleSelectableCard(this, selected, "domain/text");

        source_pill_box_->setBackgroundColor(ui::color("status/info_bg"));
        source_pill_lbl_->setTextColor(ui::color("domain/text"));
        source_pill_lbl_->setText(item.source_name);

        if (auth_card) {
            auth_pill_box_->setVisibility(brls::Visibility::VISIBLE);
            stats_lbl_->setVisibility(brls::Visibility::GONE);
        } else {
            auth_pill_box_->setVisibility(brls::Visibility::GONE);
            stats_lbl_->setVisibility(brls::Visibility::VISIBLE);
            stats_lbl_->setTextColor(ui::color("font/grey"));
            stats_lbl_->setText("▲ " + std::to_string(item.score) + " · 评论 " +
                                std::to_string(item.comment_count));
        }

        if (!prov_date.empty()) {
            prov_lbl_->setVisibility(brls::Visibility::VISIBLE);
            prov_lbl_->setTextColor(ui::color("font/grey"));
            prov_lbl_->setText("曾入选每日精选 · " + prov_date);
        } else {
            prov_lbl_->setVisibility(brls::Visibility::GONE);
        }

        const std::string clean_ex = sanitizeMarkdownExcerpt(item.excerpt, 150);
        title_md_->setMarkdown(item.title);
        title_md_->setMarginBottom(clean_ex.empty() ? 0.0f : 6.0f);

        if (!clean_ex.empty()) {
            excerpt_md_->setVisibility(brls::Visibility::VISIBLE);
            excerpt_md_->setMarkdown(clean_ex);
        } else {
            excerpt_md_->setVisibility(brls::Visibility::GONE);
        }
    }

private:
    brls::Box* top_row_ = nullptr;
    brls::Box* source_pill_box_ = nullptr;
    brls::Label* source_pill_lbl_ = nullptr;
    brls::Box* auth_pill_box_ = nullptr;
    brls::Label* stats_lbl_ = nullptr;
    brls::Label* prov_lbl_ = nullptr;
    MarkdownRichTextView* title_md_ = nullptr;
    MarkdownRichTextView* excerpt_md_ = nullptr;
    std::function<void()> on_activate_;
};

} // namespace

TextWorkbench::TextWorkbench(std::function<void(const std::string&)> on_entity_focus,
                             std::function<void()> on_open_marketplace)
    : brls::Box(brls::Axis::COLUMN),
      on_entity_focus_(std::move(on_entity_focus)),
      on_open_marketplace_(std::move(on_open_marketplace)) {
    setGrow(1.0f);
    setShrink(1.0f);
    setMinHeight(0);
    setHeightPercentage(100);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    auto* header = new brls::Box(brls::Axis::ROW);
    header->setId("omnidesk/pane_header");
    header->setShrink(0.0f);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(10);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->setShrink(1.0f);
    left->addView(ui::makeLabel("文字 · 社区 · 科学期刊 (Text Workbench)", 24.0f, "brls/text"));
    status_label_ = ui::makeLabel("个人博客 · 知乎 · Linux.do · Reddit · arXiv / Nature / PubMed", 14.5f, "font/grey");
    status_label_->setMarginTop(3);
    left->addView(status_label_);
    header->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    right->setShrink(0.0f);
    right->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(right, 6.0f);
    date_filter_btn_ = ui::makeButton("日历查找", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptDateFilter();
    }, false, 35.0f);
    date_filter_btn_->setMarginRight(8);
    right->addView(date_filter_btn_);
    auto* r_btn = ui::makeButton("刷新 (R)", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) refresh(true);
    }, true, 35.0f);
    r_btn->setMarginRight(8);
    right->addView(r_btn);
    auto* s_btn = ui::makeButton("检索文章 /", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptSearch();
    }, false, 35.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    right->addView(ui::makeButton("阅读插件管理", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
            on_open_marketplace_();
        }
    }, false, 35.0f));
    header->addView(right);
    addView(header);

    category_bar_ = new brls::Box(brls::Axis::ROW);
    category_bar_->setId("omnidesk/pane_filters");
    category_bar_->setShrink(0.0f);
    category_bar_->setMarginBottom(8);
    ui_comfort::enableFlexWrap(category_bar_, 6.0f);
    addView(category_bar_);

    channel_bar_ = new brls::Box(brls::Axis::ROW);
    channel_bar_->setShrink(0.0f);
    channel_bar_->setMarginBottom(10);
    channel_bar_->setVisibility(brls::Visibility::GONE);
    ui_comfort::enableFlexWrap(channel_bar_, 6.0f);
    addView(channel_bar_);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setGrow(1.0f);
    split->setShrink(1.0f);
    split->setMinHeight(0);
    split->setWidthPercentage(100);
    split->setHeightPercentage(100);

    class TextFeedDataSource final : public RecyclingGridDataSource {
    public:
        explicit TextFeedDataSource(TextWorkbench* owner) : owner_(owner) {}

        size_t getItemCount() override {
            return owner_ ? owner_->feed_items_.size() : 0;
        }

        float heightForRow(RecyclingGrid* recycler, size_t index) override {
            if (!owner_ || index >= owner_->feed_items_.size()) return 148.0f;
            const TextFeedItem& item = owner_->feed_items_[index];
            const float text_w = std::max(180.0f, (recycler ? recycler->cellColumnWidth() : 380.0f) - 34.0f);

            float h = 32.0f + 25.0f + 7.0f;  // vertical padding + top bar
            if (owner_->provenance_dates_.find(item.source_id + ":" + item.id) !=
                owner_->provenance_dates_.end()) {
                h += 22.0f;
            }
            const int title_lines = std::max(1, estimateWrappedLines(item.title, 16.2f, text_w, 3));
            h += static_cast<float>(title_lines) * 23.6f;

            const std::string clean_ex = sanitizeMarkdownExcerpt(item.excerpt, 150);
            if (!clean_ex.empty()) {
                const int ex_lines = std::max(1, estimateWrappedLines(clean_ex, 14.2f, text_w, 3));
                h += 6.0f + static_cast<float>(ex_lines) * 21.6f;
            }
            return h + 6.0f;
        }

        RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) override {
            auto* cell = static_cast<TextFeedCardCell*>(recycler->dequeueReusableCell("text_card"));
            if (!cell || !owner_ || index >= owner_->feed_items_.size()) return cell;

            const TextFeedItem& item = owner_->feed_items_[index];
            const bool selected = (owner_->has_article_ && owner_->active_article_.id == item.id);
            const bool auth_card = (item.id.find("auth_required") != std::string::npos ||
                                    item.id.rfind("auth:login", 0) == 0);
            std::string prov_date;
            if (auto it = owner_->provenance_dates_.find(item.source_id + ":" + item.id);
                it != owner_->provenance_dates_.end()) {
                prov_date = it->second;
            }

            auto* wb = owner_;
            auto alive = owner_->alive_;
            cell->bind(item, selected, auth_card, prov_date, [wb, alive, item, auth_card]() {
                brls::sync([wb, alive, item, auth_card]() {
                    if (alive->load(std::memory_order_acquire)) {
                        wb->openArticle(item);
                        if (auth_card && !item.url.empty()) {
                            wb->openLoginInChrome(item.url, item.title, false);
                        }
                    }
                });
            });
            return cell;
        }

    private:
        TextWorkbench* owner_ = nullptr;
    };

    feed_grid_ = new RecyclingGrid();
    feed_grid_->setId("omnidesk/pane_primary");
    feed_grid_->setWidthPercentage(37);
    feed_grid_->setHeightPercentage(100);
    feed_grid_->setShrink(1.0f);
    feed_grid_->setMinHeight(0);
    feed_grid_->setMarginRight(16);
    feed_grid_->setSpanCount(1);
    feed_grid_->setEstimatedRowHeight(152.0f);
    feed_grid_->setEstimatedRowSpace(10.0f);
    feed_grid_->setPreFetchLine(2);
    feed_grid_->registerCell("text_card", []() { return new TextFeedCardCell(); });

    auto* empty_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    empty_card->addView(ui::makeLabel("当前分类下暂无内容。", 15.0f, "font/grey"));
    feed_grid_->setEmptyView(empty_card);

    feed_grid_->setOnNextPage([this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (getVisibility() != brls::Visibility::VISIBLE) return;
        if (loading_more_ || !has_more_ || active_category_ == "daily_curation" || feed_items_.empty()) return;
        loadMoreFeed();
    });
    feed_grid_->setDataSource(new TextFeedDataSource(this), true);
    split->addView(feed_grid_);

    reader_scroll_ = new brls::ScrollingFrame();
    reader_scroll_->setId("omnidesk/pane_secondary");
    reader_scroll_->setWidthPercentage(63);
    reader_scroll_->setHeightPercentage(100);
    reader_scroll_->setShrink(1.0f);
    reader_scroll_->setMinHeight(0);
    reader_box_ = new brls::Box(brls::Axis::COLUMN);
    reader_box_->setWidthPercentage(100);
    reader_article_box_ = new brls::Box(brls::Axis::COLUMN);
    reader_article_box_->setWidthPercentage(100);
    reader_box_->addView(reader_article_box_);

    auto* cmt_wrap = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    cmt_wrap->setMarginTop(12);
    comment_tree_ = new CommentTreeView();
    cmt_wrap->addView(comment_tree_);
    reader_box_->addView(cmt_wrap);
    reader_scroll_->setContentView(reader_box_);
    split->addView(reader_scroll_);

    addView(split);

    if (HostKernel::instance().isInitialized()) {
        recommend_sub_ = HostKernel::instance().bus().subscribe(
            "entity.recommend.updated",
            "host.text_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh(false);
                    }
                });
            });
    }

    refresh(false);
}

TextWorkbench::~TextWorkbench() {
    alive_->store(false, std::memory_order_release);
    if (recommend_sub_ != 0 && HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().unsubscribe(recommend_sub_);
        recommend_sub_ = 0;
    }
    feed_request_->cancel();
    provenance_request_->cancel();
    load_more_request_->cancel();
    article_request_->cancel();
    action_request_->cancel();
    if (translate_request_) {
        translate_request_->cancel();
        translate_request_.reset();
    }
}

void TextWorkbench::refresh(bool force_remote) {
    feed_cache_.clear();
    loadCurrentFeed(force_remote);
}

void TextWorkbench::loadCurrentFeed(bool force_refresh) {
    feed_request_->cancel();
    feed_request_ = CancelToken::create();
    provenance_request_->cancel();
    provenance_request_ = CancelToken::create();
    load_more_request_->cancel();
    load_more_request_ = CancelToken::create();
    loading_more_ = false;
    current_page_ = 1;
    has_more_ = (active_category_ != "daily_curation");
    if (feed_grid_) {
        feed_grid_->setContentOffsetY(0.0f, false);
    }

    auto& kernel = HostKernel::instance();
    feed_items_.clear();
    provenance_dates_.clear();

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().textSources();
        const bool has_recipe_cap = kernel.capabilities().has("feed.recipe.v1");
        if (!sources.empty() || has_recipe_cap) {
            const std::string cache_key = active_category_ + "|" + active_channel_id_ + "|" + search_query_ + "|" + curation_scope_ + "|" + curation_date_;
            if (!force_refresh) {
                if (auto it = feed_cache_.find(cache_key); it != feed_cache_.end()) {
                    cached_recipe_pills_ = it->second.recipes;
                    feed_items_ = it->second.items;
                    active_channels_ = it->second.channels;
                    current_page_ = it->second.page;
                    has_more_ = it->second.has_more;
                    status_label_->setText("已接入 " + std::to_string(sources.size()) + " 个实时 TextSource 插件 · 共 " +
                                           std::to_string(feed_items_.size()) + " 篇");
                    finishRefresh();
                    loadProvenance();
                    return;
                }
            }

            status_label_->setText(force_refresh ? "正在强制刷新最新文章列表…" : "正在加载文章列表…");
            rebuildCategoryPills();
            rebuildChannelPills();
            struct FeedLoadResult {
                std::vector<TextFeedItem> items;
                std::vector<TextChannel> channels;
                std::vector<std::pair<std::string, std::string>> recipes;
            };
            loadUiInBackground(kernel.uiTasks(), "text.feed", feed_request_,
                [sources, query = search_query_, category = active_category_,
                 channel_id = active_channel_id_, scope = curation_scope_, date = curation_date_, force_refresh](const auto& cancel) {
                    FeedLoadResult out;
                    std::future<std::vector<std::pair<std::string, std::string>>> recipe_fut;
                    const bool fetch_recipes = category != "daily_curation" && !cancel->isCancelled() && HostKernel::instance().isInitialized() &&
                                               HostKernel::instance().capabilities().has("feed.recipe.v1");
                    if (fetch_recipes) {
                        recipe_fut = std::async(std::launch::async, [cancel]() {
                            std::vector<std::pair<std::string, std::string>> recs;
                            const auto res = HostKernel::instance().capabilities().callCancellable(
                                "feed.recipe.v1", {{"action", "list_recipes"}}, cancel);
                            if (res.ok && res.data.is_object() && res.data.contains("recipes") &&
                                res.data["recipes"].is_array()) {
                                for (const auto& r : res.data["recipes"]) {
                                    if (!r.is_object() || !r.value("enabled", true)) continue;
                                    const std::string rid = r.value("id", "");
                                    const std::string rname = r.value("name", rid);
                                    if (!rid.empty()) {
                                        recs.emplace_back("recipe:" + rid, rname);
                                    }
                                }
                            }
                            return recs;
                        });
                    }

                    struct PerSourceResult {
                        std::vector<TextFeedItem> items;
                        std::vector<TextChannel> channels;
                    };
                    std::vector<std::future<PerSourceResult>> src_futs;
                    src_futs.reserve(sources.size());
                    for (const auto& source : sources) {
                        const bool curator = source->sourceId() == "plugin-ai-curator";
                        if ((category == "daily_curation") != curator) continue;
                        const auto source_category = source->category();
                        const bool multi = source_category == "recipe" || source_category == "multi";
                        if (!multi && category != "all" && source_category != category) continue;
                        src_futs.push_back(std::async(std::launch::async,
                            [source, source_category, multi, category, channel_id, query, scope, date, curator, force_refresh, cancel]() {
                                PerSourceResult res;
                                if (cancel->isCancelled()) return res;
                                if (curator) {
                                    res.channels = source->channels();
                                    // Fetch the whole daily archive before selecting its newest business date.
                                    auto items = channel_id == "interactive"
                                        ? source->fetchFeedInChannel("interactive", query)
                                        : source->fetchFeed("");
                                    std::string newest;
                                    for (const auto& item : items) {
                                        if (!isEphemeralText(item) && isIsoDateStr(item.published_at) && item.published_at > newest)
                                            newest = item.published_at;
                                    }
                                    for (auto& item : items) {
                                        const bool ephemeral = isEphemeralText(item);
                                        if (channel_id == "interactive") {
                                            if (scope == "selected" && item.published_at != date) continue;
                                        } else {
                                            if (ephemeral) continue;
                                            if (!channel_id.empty() && item.category != channel_id) continue;
                                            if (scope == "latest" && (newest.empty() || item.published_at != newest)) continue;
                                            if (scope == "selected" && item.published_at != date) continue;
                                        }
                                        if (channel_id != "interactive" && !query.empty()) {
                                            auto lower = [](std::string text) {
                                                std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });
                                                return text;
                                            };
                                            std::string hay = item.title + " " + item.excerpt + " " + item.author + " " + item.source_name;
                                            for (const auto& tag : item.tags) hay += " " + tag;
                                            if (lower(hay).find(lower(query)) == std::string::npos) continue;
                                        }
                                        res.items.push_back(std::move(item));
                                    }
                                    if (channel_id != "interactive") {
                                        std::stable_sort(res.items.begin(), res.items.end(), [](const auto& left, const auto& right) {
                                            if (left.published_at != right.published_at) return left.published_at > right.published_at;
                                            return left.id < right.id;
                                        });
                                    }
                                    return res;
                                }
                                bool owns_selected_channel = false;
                                if (source_category == "multi" || (category != "all" && source_category == category)) {
                                    auto src_channels = source->channels();
                                    for (auto& ch : src_channels) {
                                        if (!channel_id.empty() && ch.id == channel_id) {
                                            owns_selected_channel = true;
                                        }
                                        if (!ch.id.empty()) {
                                            res.channels.push_back(std::move(ch));
                                        }
                                    }
                                }
                                if (cancel->isCancelled()) return res;
                                const bool use_channel =
                                    !channel_id.empty() &&
                                    ((category != "all" && source_category == category) ||
                                     (source_category == "multi" && owns_selected_channel));
                                auto items = force_refresh
                                    ? source->fetchFeedPage(use_channel ? channel_id : "", query, 1, true)
                                    : (use_channel
                                           ? source->fetchFeedInChannel(channel_id, query)
                                           : source->fetchFeed(query));
                                for (auto& item : items) {
                                    if (!multi || itemMatchesCategory(item, source_category, category)) {
                                        res.items.push_back(std::move(item));
                                    }
                                }
                                return res;
                            }));
                    }

                    if (fetch_recipes) {
                        out.recipes = recipe_fut.get();
                    }
                    std::set<std::string> seen_channel_ids;
                    for (auto& f : src_futs) {
                        auto sr = f.get();
                        for (auto& ch : sr.channels) {
                            if (seen_channel_ids.insert(ch.id).second) {
                                out.channels.push_back(std::move(ch));
                            }
                        }
                        for (auto& item : sr.items) {
                            out.items.push_back(std::move(item));
                        }
                    }
                    return out;
                },
                [this, alive = alive_, count = sources.size(), cache_key](FeedLoadResult result) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    cached_recipe_pills_ = result.recipes;
                    current_page_ = 1;
                    has_more_ = (active_category_ != "daily_curation" && !result.items.empty());
                    feed_cache_[cache_key] = {result.items, result.channels, result.recipes, current_page_, has_more_};
                    feed_items_ = std::move(result.items);
                    active_channels_ = std::move(result.channels);
                    if (!active_channel_id_.empty()) {
                        const bool still_exists = std::any_of(
                            active_channels_.begin(), active_channels_.end(),
                            [this](const TextChannel& ch) { return ch.id == active_channel_id_; });
                        if (!still_exists) active_channel_id_.clear();
                    }
                    if (active_category_ == "daily_curation") {
                        const std::string scope = active_channel_id_ == "interactive" ? "AI 灵感阅读 · 可重温"
                            : curation_scope_ == "all" ? "每日精选 · 全部归档"
                            : curation_scope_ == "selected" ? "每日精选 · " + curation_date_
                            : "每日精选 · 最新一期" + (feed_items_.empty() ? std::string{} : " · " + feed_items_.front().published_at);
                        status_label_->setText(scope + " · " + std::to_string(feed_items_.size()) + " 篇");
                    } else {
                        status_label_->setText("已接入 " + std::to_string(count) + " 个实时 TextSource 插件 · 共 " +
                                               std::to_string(feed_items_.size()) + " 篇");
                    }
                    finishRefresh();
                    loadProvenance();
                });
            return;
        }
    }

    cached_recipe_pills_.clear();
    active_channels_.clear();
    status_label_->setText(active_category_ == "daily_curation"
        ? "每日精选插件尚未接入" : "暂无已接入的阅读源，请从插件管理添加订阅");

    finishRefresh();
}

void TextWorkbench::loadMoreFeed() {
    if (loading_more_ || !has_more_ || active_category_ == "daily_curation" || feed_items_.empty()) {
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    const auto sources = kernel.domains().textSources();
    if (sources.empty()) return;

    loading_more_ = true;
    const int next_page = current_page_ + 1;
    const std::string cache_key = active_category_ + "|" + active_channel_id_ + "|" + search_query_ + "|" + curation_scope_ + "|" + curation_date_;
    if (status_label_) {
        status_label_->setText("正在加载第 " + std::to_string(next_page) + " 页文章…");
    }
    rebuildFeedFooter();

    load_more_request_->cancel();
    load_more_request_ = CancelToken::create();
    loadUiInBackground(kernel.uiTasks(), "text.feed.more", load_more_request_,
        [sources, query = search_query_, category = active_category_,
         channel_id = active_channel_id_, next_page](const auto& cancel) {
            std::vector<TextFeedItem> out;
            std::vector<std::future<std::vector<TextFeedItem>>> futs;
            for (const auto& source : sources) {
                if (source->sourceId() == "plugin-ai-curator") continue;
                const auto source_category = source->category();
                const bool multi = source_category == "recipe" || source_category == "multi";
                if (!multi && category != "all" && source_category != category) continue;
                futs.push_back(std::async(std::launch::async,
                    [source, source_category, multi, category, channel_id, query, next_page, cancel]() {
                        std::vector<TextFeedItem> res;
                        if (cancel->isCancelled()) return res;
                        bool owns_selected_channel = false;
                        if (!channel_id.empty() && source_category == "multi") {
                            for (const auto& ch : source->channels()) {
                                if (ch.id == channel_id) {
                                    owns_selected_channel = true;
                                    break;
                                }
                            }
                        }
                        const bool use_channel =
                            !channel_id.empty() &&
                            ((category != "all" && source_category == category) ||
                             (source_category == "multi" && owns_selected_channel));
                        auto items = source->fetchFeedPage(use_channel ? channel_id : "", query, next_page, false);
                        for (auto& item : items) {
                            if (!multi || itemMatchesCategory(item, source_category, category)) {
                                res.push_back(std::move(item));
                            }
                        }
                        return res;
                    }));
            }
            for (auto& f : futs) {
                auto part = f.get();
                for (auto& item : part) {
                    out.push_back(std::move(item));
                }
            }
            return out;
        },
        [this, alive = alive_, next_page, cache_key](std::vector<TextFeedItem> more_items) {
            if (!alive->load(std::memory_order_acquire)) return;
            loading_more_ = false;
            std::set<std::string> existing_keys;
            for (const auto& it : feed_items_) {
                existing_keys.insert(it.source_id + ":" + it.id);
            }
            size_t added = 0;
            for (auto& it : more_items) {
                if (existing_keys.insert(it.source_id + ":" + it.id).second) {
                    feed_items_.push_back(std::move(it));
                    ++added;
                }
            }
            if (added > 0) {
                current_page_ = next_page;
                has_more_ = true;
                status_label_->setText("已加载至第 " + std::to_string(current_page_) + " 页 · 共 " +
                                       std::to_string(feed_items_.size()) + " 篇");
            } else {
                has_more_ = false;
                status_label_->setText("没有更多新内容了 · 共 " + std::to_string(feed_items_.size()) + " 篇");
            }
            if (auto it = feed_cache_.find(cache_key); it != feed_cache_.end()) {
                it->second.items = feed_items_;
                it->second.page = current_page_;
                it->second.has_more = has_more_;
            }
            if (feed_grid_) {
                feed_grid_->notifyDataChanged();
            }
            rebuildFeedFooter();
            if (added > 0) {
                loadProvenance();
            }
        },
        [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            loading_more_ = false;
            rebuildFeedFooter();
        });
}

void TextWorkbench::finishRefresh() {
    rebuildCategoryPills();
    rebuildChannelPills();
    rebuildFeed();
    rebuildReader();
    if (!has_article_ && !feed_items_.empty()) openArticle(feed_items_.front());
}

void TextWorkbench::loadProvenance() {
    auto& kernel = HostKernel::instance();
    if (active_category_ == "daily_curation" || feed_items_.empty() ||
        !kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;
    nlohmann::json items = nlohmann::json::array();
    for (const auto& item : feed_items_) {
        items.push_back({{"id", item.source_id + ":" + item.id}, {"title", item.title}, {"url", item.url},
                         {"source_id", item.source_id}, {"published_at", item.published_at},
                         {"tags", item.tags}});
    }
    provenance_request_->cancel();
    provenance_request_ = CancelToken::create();
    loadUiInBackground(kernel.uiTasks(), "text.provenance", provenance_request_,
        [items = std::move(items)](const auto& cancel) {
            std::unordered_map<std::string, std::string> dates;
            try {
                const auto result = HostKernel::instance().capabilities().callCancellable(
                    "recommend.feed.v1", {{"action", "lookup_provenance"}, {"items", items}}, cancel);
                if (!result.ok || !result.data.is_object() || !result.data.contains("matches") ||
                    !result.data["matches"].is_array()) return dates;
                for (const auto& match : result.data["matches"]) {
                    if (!match.is_object()) continue;
                    const auto id = match.value("id", "");
                    const auto date = match.value("previously_recommended_on", "");
                    if (!id.empty() && !date.empty()) dates[id] = date;
                }
            } catch (...) {
                // Optional historical badges never prevent live reading.
            }
            return dates;
        },
        [this, alive = alive_](std::unordered_map<std::string, std::string> dates) {
            if (!alive->load(std::memory_order_acquire)) return;
            provenance_dates_ = std::move(dates);
            if (feed_grid_) {
                feed_grid_->reloadData(false);
            }
        });
}

void TextWorkbench::rebuildCategoryPills() {
    category_bar_->clearViews();
    std::vector<std::pair<std::string, std::string>> cats = {
        {"all", "全部阅读流"},
        {"daily_curation", "每日精选"},
        {"zhihu", "知乎"},
        {"blog", "个人博客"},
        {"linuxdo", "Linux.do"},
        {"shuiyuan", "交大水源"},
        {"v2ex", "V2EX"},
        {"reddit", "Reddit"},
        {"x", "X"},
        {"journal", "科学期刊 (arXiv/Nature)"},
        {"recipe", "配方源 (20行)"},
    };

    for (const auto& [rid, rname] : cached_recipe_pills_) {
        cats.emplace_back(rid, rname);
    }

    for (const auto& [cid, label] : cats) {
        auto* b = ui_comfort::makeFilterChip(label, [this, alive = alive_, cid]() {
            brls::sync([this, alive, cid]() {
                if (!alive->load(std::memory_order_acquire)) return;
                active_category_ = cid;
                active_channel_id_.clear();
                active_channels_.clear();
                has_article_ = false;
                loadCurrentFeed();
            });
        }, active_category_ == cid, "domain/text", 32.0f);
        b->setMarginRight(7);
        category_bar_->addView(b);
    }
}

void TextWorkbench::rebuildChannelPills() {
    channel_bar_->clearViews();
    const bool has_curator = active_category_ == "daily_curation";
    if (date_filter_btn_) date_filter_btn_->setVisibility(has_curator ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    const bool show_channels = !active_channels_.empty();
    if (!show_channels && !has_curator) {
        channel_bar_->setVisibility(brls::Visibility::GONE);
        return;
    }
    channel_bar_->setVisibility(brls::Visibility::VISIBLE);

    if (show_channels) {
        auto* all_btn = ui_comfort::makeFilterChip("全部栏目", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                active_channel_id_.clear();
                has_article_ = false;
                loadCurrentFeed();
            });
        }, active_channel_id_.empty(), "domain/text", 30.0f);
        all_btn->setMarginRight(6);
        channel_bar_->addView(all_btn);

        for (const auto& ch : active_channels_) {
            std::string label = ch.name.empty() ? ch.id : ch.name;
            if (!ch.badge.empty()) {
                label += " · " + ch.badge;
            } else if (ch.requires_auth) {
                label += " (需登录)";
            }
            auto* btn = ui_comfort::makeFilterChip(label, [this, alive = alive_, cid = ch.id]() {
                brls::sync([this, alive, cid]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    active_channel_id_ = cid;
                    has_article_ = false;
                    loadCurrentFeed();
                });
            }, active_channel_id_ == ch.id, "domain/text", 30.0f);
            btn->setMarginRight(6);
            channel_bar_->addView(btn);
        }
    }

    if (date_filter_btn_) {
        date_filter_btn_->setVisibility(has_curator ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        date_filter_btn_->setText(curation_scope_ == "selected" ? curation_date_ : "日历查找");
    }
    if (has_curator) {
        for (const auto& option : std::vector<std::pair<std::string, std::string>>{
                 {"latest", "最新一期"}, {"all", "全部归档"}}) {
            if (active_channel_id_ == "interactive" && option.first == "latest") continue;
            const auto label = active_channel_id_ == "interactive" ? std::string("全部会话") : option.second;
            const bool scope_active =
                curation_scope_ == option.first ||
                (active_channel_id_ == "interactive" && option.first == "all" && curation_scope_ == "latest");
            auto* button = ui_comfort::makeFilterChip(label, [this, alive = alive_, scope = option.first]() {
                if (!alive->load(std::memory_order_acquire)) return;
                curation_scope_ = scope;
                has_article_ = false;
                loadCurrentFeed();
            }, scope_active, "domain/text", 30.0f);
            button->setMarginRight(6);
            channel_bar_->addView(button);
        }
    }
}

void TextWorkbench::rebuildFeedFooter() {
    if (!feed_grid_) return;
    if (feed_items_.empty()) {
        feed_grid_->setFooterView(nullptr);
        return;
    }

    auto* footer = ui::makeCard("desktop/soft", 12.0f, 10.0f);
    footer->setMarginTop(10);
    footer->setMarginBottom(14);
    auto* f_row = new brls::Box(brls::Axis::ROW);
    f_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    f_row->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(f_row, 6.0f);

    if (active_category_ != "daily_curation") {
        const std::string more_lbl = loading_more_
            ? "正在加载下一页…"
            : (has_more_
                   ? ("加载下一页 (第 " + std::to_string(current_page_ + 1) + " 页)")
                   : "已到底部 · 再试加载");
        auto* more_btn = ui::makeButton(more_lbl, [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire) || loading_more_) return;
                has_more_ = true;
                loadMoreFeed();
            });
        }, has_more_ && !loading_more_, 33.0f);
        more_btn->setGrow(1.0f);
        more_btn->setMarginRight(8);
        f_row->addView(more_btn);
    }

    auto* ref_btn = ui::makeButton("刷新", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                refresh(true);
            }
        });
    }, false, 33.0f);
    ref_btn->setMarginRight(8);
    f_row->addView(ref_btn);

    auto* top_btn = ui::makeButton("顶部", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire) && feed_grid_) {
                feed_grid_->setContentOffsetY(0.0f, false);
            }
        });
    }, false, 33.0f);
    f_row->addView(top_btn);

    footer->addView(f_row);
    feed_grid_->setFooterView(footer);
}

void TextWorkbench::rebuildFeed() {
    rebuildFeedFooter();
    if (feed_grid_) {
        feed_grid_->reloadData(false);
    }
}

void TextWorkbench::openArticle(const TextFeedItem& item) {
    if (translate_request_ && (!has_article_ || active_article_.id != item.id)) {
        translate_request_->cancel();
        translate_request_.reset();
        translating_ = false;
    }
    article_request_->cancel();
    article_request_ = CancelToken::create();
    active_article_source_id_ = item.source_id;
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        for (const auto& src : kernel.domains().textSources()) {
            if (src->sourceId() == item.source_id) {
                status_label_->setText("正在加载文章…");
                loadUiInBackground(kernel.uiTasks(), "text.article", article_request_,
                    [src, article_id = item.id](const auto&) { return src->fetchArticle(article_id); },
                    [this, alive = alive_](TextArticle article) {
                        if (!alive->load(std::memory_order_acquire)) return;
                        status_label_->setText(article.title.empty() ? "文章加载失败，可重试" : "文章已加载");
                        if (!article.id.empty()) showArticle(std::move(article));
                    });
                return;
            }
        }
    }
    status_label_->setText("文章来源不可用，请刷新或打开原文");
}

void TextWorkbench::showArticle(TextArticle article) {
    active_article_ = std::move(article);
    auto& kernel = HostKernel::instance();
    has_article_ = true;
    if (on_entity_focus_) {
        std::string summary = stripMarkdownToPlainText(active_article_.title, true);
        if (!active_article_.source_name.empty()) summary += " · " + active_article_.source_name;
        if (!active_article_.published_at.empty()) summary += " · " + active_article_.published_at;
        on_entity_focus_(summary);
    }
    if (kernel.isInitialized()) {
        kernel.bus().publish("entity.text.open", {
            {"id", active_article_.id},
            {"title", active_article_.title},
            {"origin", "host_workbench"},
        });
        kernel.bus().publish("host.focus_changed", {
            {"domain", "text"},
            {"id", active_article_.id},
            {"title", active_article_.title},
            {"subtitle", active_article_.subtitle},
            {"author", active_article_.authors},
            {"category", active_article_.category},
            {"source_name", active_article_.source_name},
            {"url", active_article_.url},
            {"doi", active_article_.doi},
            {"page_number", 0},
            {"play_url", ""},
            {"extra", nlohmann::json{
                {"tags", active_article_.tags},
                {"published_at", active_article_.published_at},
            }},
        });
    }
    if (comment_tree_) {
        if (kernel.isInitialized()) {
            comment_tree_->setServices(&kernel.capabilities(), &kernel.bus(), &kernel.uiTasks());
        }
        comment_tree_->setArticleContext(
            active_article_source_id_, active_article_.id,
            active_article_.title, active_article_.authors);
        comment_tree_->setComments(active_article_.comments);
        if (active_article_.comments.empty() && kernel.isInitialized() &&
            kernel.capabilities().has("text.comments.v1")) {
            comment_tree_->fetchFromBackend(false);
        }
    }
    rebuildCategoryPills();
    rebuildChannelPills();
    if (feed_grid_) {
        feed_grid_->reloadData(false);
    }
    rebuildReader();
}

void TextWorkbench::rebuildReader() {
    if (!reader_article_box_) return;
    reader_article_box_->clearViews();
    if (!has_article_) return;

    auto* doc = ui::makeCard("desktop/surface", 24.0f, 12.0f);

    // Article meta + display mode pills
    auto* top_row = new brls::Box(brls::Axis::ROW);
    top_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    top_row->setAlignItems(brls::AlignItems::CENTER);
    top_row->setMarginBottom(10);
    ui_comfort::enableFlexWrap(top_row, 6.0f);
    top_row->addView(ui::makePill(active_article_.source_name, "status/info_bg", "domain/text"));

    auto* mode_row = new brls::Box(brls::Axis::ROW);
    mode_row->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(mode_row, 6.0f);
    auto& kernel = HostKernel::instance();
    const bool can_translate = kernel.isInitialized() &&
                               (kernel.capabilities().has("feed.recipe.v1") ||
                                kernel.capabilities().has("llm.translate.v1") ||
                                kernel.capabilities().has("llm.chat.v1"));
    if (can_translate && !active_article_.id.empty()) {
        auto* tr_btn = ui::makeButton(
            translating_ ? "翻译中..." : "一键双语翻译",
            [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire)) translateActiveArticle();
            },
            false, 31.0f);
        tr_btn->setMarginLeft(6);
        mode_row->addView(tr_btn);
    }
    if (kernel.isInitialized() && kernel.capabilities().has("web.embed.v1") && !active_article_.url.empty()) {
        auto* web_btn = ui::makeButton("Chrome 原页", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) openActiveInChrome();
        }, false, 31.0f);
        web_btn->setMarginLeft(6);
        mode_row->addView(web_btn);
    }
    const bool is_journal = (active_article_.category == "journal") ||
                            !active_article_.doi.empty() ||
                            !resolveJournalPdfUrl(active_article_).empty();
    if (kernel.isInitialized() && is_journal && kernel.capabilities().has("book.bilingual_pdf.v1")) {
        auto* pdf_btn = ui::makeButton("BabelDOC 精译 PDF", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) handoffJournalToBabelDoc();
        }, false, 31.0f);
        pdf_btn->setMarginLeft(6);
        mode_row->addView(pdf_btn);
    }

    struct ModeDef {
        BilingualMode m;
        const char* label;
    };
    const ModeDef modes[] = {
        {BilingualMode::Parallel, "双语对照"},
        {BilingualMode::TranslatedOnly, "仅译文"},
        {BilingualMode::OriginalOnly, "仅原文"},
    };
    for (const auto& md : modes) {
        auto* btn = ui_comfort::makeFilterChip(md.label, [this, alive = alive_, m = md.m]() {
            brls::sync([this, alive, m]() {
                if (!alive->load(std::memory_order_acquire)) return;
                bilingual_mode_ = m;
                rebuildReader();
            });
        }, bilingual_mode_ == md.m, "domain/text", 31.0f);
        btn->setMarginLeft(6);
        mode_row->addView(btn);
    }
    top_row->addView(mode_row);
    doc->addView(top_row);

    const bool auth_article = (active_article_.id.find("auth_required") != std::string::npos ||
                               active_article_.id.rfind("auth:login", 0) == 0);
    if (auth_article && !active_article_.url.empty() &&
        kernel.isInitialized() && kernel.capabilities().has("web.embed.v1")) {
        auto* auth_bar = ui::makeCard("desktop/soft", 14.0f, 10.0f);
        auth_bar->setMarginBottom(14);
        auto* auth_row = new brls::Box(brls::Axis::ROW);
        auth_row->setAlignItems(brls::AlignItems::CENTER);
        ui_comfort::enableFlexWrap(auth_row, 6.0f);

        const std::string login_url = active_article_.url;
        const std::string login_title = active_article_.title;

        auto* emb_login_btn = ui::makeButton("立即打开登录界面", [this, alive = alive_, login_url, login_title]() {
            if (alive->load(std::memory_order_acquire)) {
                openLoginInChrome(login_url, login_title, false);
            }
        }, true, 35.0f);
        emb_login_btn->setMarginRight(8);
        auth_row->addView(emb_login_btn);

        auto* ext_login_btn = ui::makeButton("外部 Chrome 窗口登录", [this, alive = alive_, login_url, login_title]() {
            if (alive->load(std::memory_order_acquire)) {
                openLoginInChrome(login_url, login_title, true);
            }
        }, false, 35.0f);
        ext_login_btn->setMarginRight(8);
        auth_row->addView(ext_login_btn);

        auto* sync_btn = ui::makeButton("已完成登录 · 同步刷新", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                syncCookiesAndRefresh();
            }
        }, false, 35.0f);
        auth_row->addView(sync_btn);

        auth_bar->addView(auth_row);
        doc->addView(auth_bar);
    }

    auto linkClickHandler = [this, alive = alive_](const std::string& raw_url, const std::string& label) {
        if (!alive->load(std::memory_order_acquire) || raw_url.empty()) return;
        if (raw_url.rfind("wikilink:", 0) == 0) {
            const std::string target = raw_url.substr(9);
            auto tryMapVaultId = [](const std::string& path) -> std::string {
                const size_t slash = path.find_last_of('/');
                if (slash == std::string::npos || slash + 1 >= path.size()) return "";
                std::string date = path.substr(slash + 1);
                if (date.size() > 3 && date.substr(date.size() - 3) == ".md") {
                    date.resize(date.size() - 3);
                }
                if (!isIsoDateStr(date)) return "";
                if (path.find("English/Weekly/TV/") != std::string::npos) return "english-weekly-tv-" + date;
                if (path.find("English/Weekly/") != std::string::npos) return "english-weekly-" + date;
                if (path.find("English/Daily/") != std::string::npos) return "english-daily-" + date;
                if (path.find("English/Translation/") != std::string::npos) return "english-translation-" + date;
                if (path.find("English/Vocabulary/") != std::string::npos) return "english-vocabulary-" + date;
                if (path.find("News/Daily/") != std::string::npos) return "news-" + date;
                return "";
            };
            const std::string mapped_id = tryMapVaultId(target);
            if (!mapped_id.empty()) {
                openExternalArticle({
                    {"article_id", mapped_id},
                    {"source_id", "plugin-ai-curator"},
                    {"title", label.empty() ? target : label},
                });
                return;
            }
            brls::Application::notify("知识库内链: " + (label.empty() ? target : label));
            return;
        }
        const std::string resolved = ImageCache::normalizeImageUrl(raw_url, active_article_.url);
        if (resolved.empty()) return;
        auto& k = HostKernel::instance();
        if (k.isInitialized() && k.capabilities().has("web.embed.v1")) {
            const std::string link_title = label.empty() ? stripMarkdownToPlainText(active_article_.title, true) : label;
            action_request_->cancel();
            action_request_ = CancelToken::create();
            loadUiInBackground(k.uiTasks(), "text.link", action_request_,
                [resolved, link_title](const auto& cancel) {
                    return HostKernel::instance().capabilities().callCancellable("web.embed.v1", {
                        {"action", "open"},
                        {"url", resolved},
                        {"title", link_title},
                    }, cancel);
                },
                [alive, resolved](CapabilityResult res) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (!res.ok) {
                        SDL_OpenURL(resolved.c_str());
                    }
                });
        } else {
            SDL_OpenURL(resolved.c_str());
        }
    };

    MarkdownStyle h1_style;
    h1_style.font_size = 23.0f;
    h1_style.line_height = 1.36f;
    h1_style.text_color_token = "brls/text";
    h1_style.bold_base = true;
    h1_style.interactive_links = false;
    auto* h1 = new MarkdownRichTextView(stripLeadingHashes(active_article_.title), std::move(h1_style));
    h1->setMarginBottom(6);
    doc->addView(h1);

    std::string meta = active_article_.subtitle;
    if (!active_article_.doi.empty()) {
        meta += (meta.empty() ? "" : " · ") + std::string("DOI: [") + active_article_.doi +
                "](https://doi.org/" + active_article_.doi + ")";
    }
    if (!meta.empty()) {
        MarkdownStyle meta_style;
        meta_style.font_size = 14.5f;
        meta_style.line_height = 1.45f;
        meta_style.text_color_token = "font/grey";
        meta_style.on_link_click = linkClickHandler;
        auto* meta_lbl = new MarkdownRichTextView(meta, std::move(meta_style));
        meta_lbl->setMarginBottom(12);
        doc->addView(meta_lbl);
    }

    doc->addView(ui::makeDivider(8.0f));

    auto openPopoverFor = [this](const std::string& text) {
        auto& k = HostKernel::instance();
        SelectionContext sctx;
        const std::string clean_text = stripMarkdownToPlainText(text);
        sctx.context_sentence = clean_text.empty() ? text : clean_text;
        sctx.source_title = stripMarkdownToPlainText(active_article_.title, true);
        if (!active_article_.source_name.empty()) sctx.source_title += " · " + active_article_.source_name;
        if (!active_article_.published_at.empty()) sctx.source_title += " · " + active_article_.published_at;
        sctx.source_id = active_article_source_id_;
        sctx.domain = ContentDomain::Text;
        if (k.isInitialized()) {
            sctx.capabilities = &k.capabilities();
            sctx.bus = &k.bus();
            sctx.ui_tasks = &k.uiTasks();
        }
        SelectionActionPopover::show(std::move(sctx));
    };

    auto makeBlockStyle = [&](float font_size, const std::string& color_token, float line_height) {
        MarkdownStyle st;
        st.font_size = font_size;
        st.line_height = line_height;
        st.text_color_token = color_token;
        st.block_gap = 7.0f;
        st.base_url = active_article_.url;
        st.image_headers = active_article_.image_headers;
        st.on_link_click = linkClickHandler;
        return st;
    };

    for (const auto& p : active_article_.paragraphs) {
        if (p.kind == "image") {
            const std::string img_url = ImageCache::normalizeImageUrl(p.original, active_article_.url);
            if (!img_url.empty()) {
                auto* fig = ui::makeCard("desktop/soft", 10.0f, 10.0f);
                fig->setAlignItems(brls::AlignItems::CENTER);
                fig->setMarginTop(8);
                fig->setMarginBottom(8);
                auto* img = new CachedImageView();
                img->setWidthPercentage(100);
                img->setHeight(260.0f);
                img->setScalingType(brls::ImageScalingType::FIT);
                img->setCornerRadius(6.0f);
                img->setImageUrl(img_url, active_article_.image_headers);
                fig->addView(img);
                if (!p.translated.empty()) {
                    auto cap_style = makeBlockStyle(13.5f, "font/grey", 1.45f);
                    cap_style.italic_base = true;
                    auto* cap = new MarkdownRichTextView(p.translated, std::move(cap_style));
                    cap->setMarginTop(6);
                    fig->addView(cap);
                }
                doc->addView(fig);
            }
            continue;
        }
        if (p.kind == "heading") {
            if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
                auto h_style = makeBlockStyle(19.5f, "brls/text", 1.42f);
                h_style.bold_base = true;
                auto* hl = new MarkdownRichTextView(stripLeadingHashes(p.original), std::move(h_style));
                hl->setMarginTop(14);
                hl->setMarginBottom(p.translated.empty() ? 6 : 2);
                doc->addView(hl);
            }
            if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
                auto htr_style = makeBlockStyle(17.0f, "font/grey", 1.42f);
                htr_style.bold_base = true;
                auto* htr = new MarkdownRichTextView(stripLeadingHashes(p.translated), std::move(htr_style));
                htr->setMarginTop(2);
                htr->setMarginBottom(6);
                doc->addView(htr);
            }
            continue;
        }
        if (p.kind == "code" || p.kind == "formula") {
            auto* wrap = new brls::Box(brls::Axis::COLUMN);
            wrap->setWidthPercentage(100);
            wrap->setMarginTop(8);
            wrap->setMarginBottom(8);
            if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
                const std::string md_src = (p.kind == "code")
                                               ? ensureCodeFenceMarkdown(p.original)
                                               : ensureFormulaMarkdown(p.original);
                wrap->addView(new MarkdownView(md_src, makeBlockStyle(15.0f, "brls/text", 1.52f)));
            }
            if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
                auto* tr = new MarkdownView(p.translated, makeBlockStyle(15.0f, "font/grey", 1.52f));
                tr->setMarginTop(6);
                wrap->addView(tr);
            }
            doc->addView(wrap);
            continue;
        }
        if (p.kind == "quote") {
            auto* q_wrap = new brls::Box(brls::Axis::COLUMN);
            q_wrap->setWidthPercentage(100);
            q_wrap->setMarginTop(6);
            q_wrap->setMarginBottom(6);
            if (!p.original.empty()) {
                q_wrap->setFocusable(true);
                q_wrap->setHighlightCornerRadius(8.0f);
                q_wrap->registerClickAction([openPopoverFor, text = p.original](brls::View*) {
                    openPopoverFor(text);
                    return true;
                });
            }
            if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
                q_wrap->addView(new MarkdownView(ensureQuoteMarkdown(p.original),
                                                 makeBlockStyle(15.5f, "brls/text", 1.58f)));
            }
            if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
                auto* tr = new MarkdownView(ensureQuoteMarkdown(p.translated),
                                            makeBlockStyle(15.0f, "font/grey", 1.56f));
                tr->setMarginTop(6);
                q_wrap->addView(tr);
            }
            doc->addView(q_wrap);
            continue;
        }

        auto* para_box = new brls::Box(brls::Axis::COLUMN);
        para_box->setWidthPercentage(100);
        para_box->setPadding(6, 10, 6, 10);
        para_box->setCornerRadius(6.0f);
        para_box->setMarginTop(4);
        para_box->setMarginBottom(4);
        if (!p.original.empty()) {
            para_box->setFocusable(true);
            para_box->setHighlightCornerRadius(6.0f);
            para_box->registerClickAction([openPopoverFor, text = p.original](brls::View*) {
                openPopoverFor(text);
                return true;
            });
        }
        if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
            para_box->addView(new MarkdownView(p.original, makeBlockStyle(16.5f, "brls/text", 1.65f)));
        }
        if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
            auto* tr = new MarkdownView(p.translated, makeBlockStyle(15.5f, "font/grey", 1.62f));
            tr->setMarginTop(6);
            para_box->addView(tr);
        }
        doc->addView(para_box);
    }

    reader_article_box_->addView(doc);
    if (comment_tree_) {
        CommentDisplayMode cm_mode = CommentDisplayMode::Parallel;
        if (bilingual_mode_ == BilingualMode::TranslatedOnly) cm_mode = CommentDisplayMode::TranslatedOnly;
        else if (bilingual_mode_ == BilingualMode::OriginalOnly) cm_mode = CommentDisplayMode::OriginalOnly;
        comment_tree_->setDisplayMode(cm_mode);
    }
}

void TextWorkbench::applyDateFilter(const std::string& date, const std::string& domain) {
    if (date.empty()) return;
    active_category_ = "daily_curation";
    if (domain == "news" || domain == "reddit" || domain == "journal" || domain == "english") {
        active_channel_id_ = domain;
    }
    curation_scope_ = "selected";
    curation_date_ = date;
    has_article_ = false;
    refresh();
}

void TextWorkbench::promptDateFilter() {
    std::string init_date = curation_scope_ == "selected" ? curation_date_ : "";
    std::string init_dom = "all";
    if (active_channel_id_ == "news" || active_channel_id_ == "reddit" ||
        active_channel_id_ == "journal" || active_channel_id_ == "english") {
        init_dom = active_channel_id_;
    }
    CurationCalendarDialog::open({
        .initial_date = init_date,
        .initial_domain = init_dom,
        .on_apply_date_filter = [this, alive = alive_](const std::string& d) {
            if (!alive->load(std::memory_order_acquire)) return;
            applyDateFilter(d);
        },
        .on_clear_date_filter = [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            curation_scope_ = "all";
            curation_date_.clear();
            has_article_ = false;
            refresh();
        },
    });
}

void TextWorkbench::translateActiveArticle() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !has_article_) return;

    const bool has_recipe = kernel.capabilities().has("feed.recipe.v1");
    const bool has_tr = kernel.capabilities().has("llm.translate.v1");
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    if (!has_recipe && !has_tr && !has_chat) {
        brls::Application::notify("未装载翻译能力插件（请安装 plugin-magpie-agent）");
        return;
    }

    action_request_->cancel();
    if (translate_request_) {
        translate_request_->cancel();
    }
    auto req = CancelToken::create();
    translate_request_ = req;
    translating_ = true;
    const TextArticle snapshot = active_article_;
    const std::string source_id = active_article_source_id_;
    status_label_->setText("正在后台执行双语翻译: " + snapshot.title + "...");
    rebuildReader();

    loadUiInBackground(kernel.uiTasks(), "text.translate", req,
        [snapshot, source_id, has_recipe, has_tr, has_chat](const auto& cancel) {
            TextArticle out = snapshot;
            bool updated = false;
            std::string err_msg;

            if (has_recipe && (source_id.empty() || source_id.find("recipe") != std::string::npos)) {
                const auto res = HostKernel::instance().capabilities().callCancellable("feed.recipe.v1", {
                    {"action", "translate_article"},
                    {"article_id", out.id},
                }, cancel);
                if (res.ok && res.data.is_object() && res.data.contains("article") &&
                    res.data["article"].is_object()) {
                    const auto& aj = res.data["article"];
                    if (aj.contains("paragraphs") && aj["paragraphs"].is_array()) {
                        const size_t n = std::min(out.paragraphs.size(), aj["paragraphs"].size());
                        for (size_t i = 0; i < n; ++i) {
                            const std::string tr = aj["paragraphs"][i].value("translated", "");
                            if (!tr.empty()) {
                                out.paragraphs[i].translated = tr;
                                updated = true;
                            }
                        }
                    }
                    if (aj.contains("comments")) {
                        applyTranslatedCommentsJson(out.comments, aj["comments"]);
                        updated = true;
                    }
                    if (updated) {
                        return std::tuple<bool, std::string, TextArticle>(true, "", std::move(out));
                    }
                } else if (!res.ok) {
                    err_msg = res.error;
                }
            }

            if (!has_tr && !has_chat) {
                return std::tuple<bool, std::string, TextArticle>(
                    false, err_msg.empty() ? "未找到可用 llm.translate.v1 / llm.chat.v1 提供方" : err_msg,
                    std::move(out));
            }

            auto translateText = [&](const std::string& text, const std::string& ctx_before) -> std::string {
                if (text.empty() || cancel->isCancelled()) return "";
                CapabilityResult r;
                if (has_tr) {
                    r = HostKernel::instance().capabilities().callCancellable("llm.translate.v1", {
                        {"text", text},
                        {"source_lang", "auto"},
                        {"target_lang", "zh-CN"},
                        {"context_before", ctx_before},
                        {"preserve_formatting", true},
                    }, cancel);
                } else {
                    r = HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                        {"domain", "text"},
                        {"context", out.title},
                        {"prompt", "请将以下内容精准译为简体中文，保留代码/公式/引用标记不破坏：\n\n" + text},
                    }, cancel);
                }
                if (!r.ok) {
                    if (err_msg.empty()) err_msg = r.error;
                    return "";
                }
                std::string tr = r.data.value("translation", r.data.value("reply", ""));
                return extractTranslationTag(tr);
            };

            std::string prev_orig;
            for (auto& p : out.paragraphs) {
                if (cancel->isCancelled()) break;
                if (p.kind == "code" || p.kind == "formula" || p.original.empty()) {
                    continue;
                }
                if (p.translated.empty()) {
                    std::string tr = translateText(p.original, prev_orig);
                    if (!tr.empty()) {
                        p.translated = std::move(tr);
                        updated = true;
                    }
                }
                prev_orig = p.original;
            }

            std::function<void(std::vector<CommentNode>&)> translateComments =
                [&](std::vector<CommentNode>& nodes) {
                    for (auto& node : nodes) {
                        if (cancel->isCancelled()) return;
                        if (!node.body.empty() && node.translated_body.empty()) {
                            std::string tr = translateText(node.body, "");
                            if (!tr.empty()) {
                                node.translated_body = std::move(tr);
                                updated = true;
                            }
                        }
                        translateComments(node.replies);
                    }
                };
            translateComments(out.comments);

            return std::tuple<bool, std::string, TextArticle>(updated, std::move(err_msg), std::move(out));
        },
        [this, alive = alive_, req, target_id = snapshot.id](auto tuple_res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (translate_request_ == req) {
                translate_request_.reset();
            }
            translating_ = false;
            const bool ok = std::get<0>(tuple_res);
            const std::string& err = std::get<1>(tuple_res);
            auto& out = std::get<2>(tuple_res);
            if (ok && has_article_ && active_article_.id == target_id) {
                active_article_ = std::move(out);
                bilingual_mode_ = BilingualMode::Parallel;
                status_label_->setText("已完成全文与评论区双语对照翻译");
                brls::Application::notify("双语翻译完成");
            } else {
                status_label_->setText(err.empty() ? "翻译未产生新译文" : ("翻译失败: " + err));
                if (!err.empty()) {
                    brls::Application::notify("翻译失败: " + err);
                }
            }
            rebuildReader();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (translate_request_ && translate_request_ != req) return;
            translate_request_.reset();
            translating_ = false;
            if (status_label_) {
                status_label_->setText("已取消双语翻译");
            }
            rebuildReader();
        });
}

bool TextWorkbench::isShowingAuthPrompt() const {
    if (has_article_ &&
        (active_article_.id.find("auth_required") != std::string::npos ||
         active_article_.id.rfind("auth:login", 0) == 0)) {
        return true;
    }
    if (feed_items_.size() == 1) {
        const std::string& fid = feed_items_.front().id;
        if (fid.find("auth_required") != std::string::npos || fid.rfind("auth:login", 0) == 0) {
            return true;
        }
    }
    return false;
}

void TextWorkbench::openActiveInChrome() {
    if (active_article_.url.empty()) return;
    openLoginInChrome(active_article_.url, active_article_.title, false);
}

void TextWorkbench::openLoginInChrome(const std::string& url,
                                      const std::string& title,
                                      bool external_window) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("web.embed.v1")) {
        brls::Application::notify("未装载 Chrome 网页桥接插件 (plugin-chrome-web)");
        return;
    }
    if (url.empty()) return;
    if (status_label_) {
        status_label_->setText(external_window ? "正在打开外部 Chrome 登录窗口…"
                                               : "正在打开内嵌 Chrome 登录界面…");
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    loadUiInBackground(kernel.uiTasks(), "text.chrome", action_request_,
        [url, title, external_window](const auto& cancel) {
            nlohmann::json req = {
                {"action", "open"},
                {"url", url},
                {"title", title},
                {"auto_translate", false},
            };
            if (external_window) {
                req["window_mode"] = "external";
            }
            return HostKernel::instance().capabilities().callCancellable("web.embed.v1", req, cancel);
        },
        [this, alive = alive_, external_window](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (!res.ok) {
                if (status_label_) status_label_->setText("打开 Chrome 失败: " + res.error);
                brls::Application::notify("打开 Chrome 登录界面失败: " + res.error);
            } else if (external_window) {
                if (status_label_) {
                    status_label_->setText("已弹出 Chrome 登录窗口，登录完成后点击『已完成登录 · 同步刷新』");
                }
                brls::Application::notify("已打开外部 Chrome 登录窗口，完成后点『已完成登录 · 同步刷新』");
            }
        });
}

void TextWorkbench::syncCookiesAndRefresh() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    if (status_label_) {
        status_label_->setText("正在同步浏览器会话 Cookie 并刷新…");
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    loadUiInBackground(kernel.uiTasks(), "text.sync_cookies", action_request_,
        [](const auto& cancel) {
            auto& caps = HostKernel::instance().capabilities();
            if (caps.has("web.embed.v1")) {
                return caps.callCancellable("web.embed.v1", {{"action", "sync_cookies"}}, cancel);
            }
            return CapabilityResult{true, "", nlohmann::json::object()};
        },
        [this, alive = alive_](CapabilityResult) {
            if (!alive->load(std::memory_order_acquire)) return;
            has_article_ = false;
            refresh(true);
            brls::Application::notify("已同步会话并刷新当前订阅源");
        });
}

void TextWorkbench::handoffJournalToBabelDoc() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("book.bilingual_pdf.v1")) {
        brls::Application::notify("未装载 book.bilingual_pdf.v1 插件");
        return;
    }
    const std::string pdf_url = resolveJournalPdfUrl(active_article_);
    if (pdf_url.empty()) {
        brls::Application::notify("当前文章暂无可直达 PDF 链接；可在 Chrome 原页下载 PDF 后送入 BabelDOC");
        return;
    }
    const std::string title = active_article_.title;
    action_request_->cancel();
    action_request_ = CancelToken::create();
    loadInBackground(kernel.jobs(), "提交 BabelDOC PDF 精译: " + title, action_request_,
        [pdf_url, title](const auto& cancel) {
            return HostKernel::instance().capabilities().callCancellable("book.bilingual_pdf.v1", {
                {"action", "import_pdf"},
                {"source", pdf_url},
                {"url", pdf_url},
                {"title", title},
            }, cancel);
        },
        [alive = alive_, title](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (!res.ok) {
                brls::Application::notify("BabelDOC 导入失败: " + res.error);
                return;
            }
            const std::string bid = res.data.value("book_id", "");
            if (!bid.empty() && HostKernel::instance().isInitialized()) {
                HostKernel::instance().bus().publish("entity.book.open", {
                    {"book_id", bid},
                    {"title", title},
                    {"format", "pdf"},
                    {"page", 1},
                });
            } else {
                brls::Application::notify("已送入 BabelDOC 书架: " + title);
            }
        });
}

void TextWorkbench::openExternalArticle(const nlohmann::json& payload) {
    if (!payload.is_object()) return;
    const std::string aid = payload.value("article_id", payload.value("id", ""));
    const std::string sid = payload.value("source_id", "");
    const std::string title = payload.value("title", "");
    const std::string url = payload.value("url", "");
    const std::string doi = payload.value("doi", "");

    for (const auto& item : feed_items_) {
        if (!sid.empty() && item.source_id != sid) continue;
        if ((!aid.empty() && item.id == aid) || (!url.empty() && item.url == url)) {
            openArticle(item);
            return;
        }
    }

    auto& kernel = HostKernel::instance();
    if (!aid.empty() && kernel.isInitialized()) {
        const auto sources = kernel.domains().textSources();
        for (const auto& src : sources) {
            if (!sid.empty() && src->sourceId() != sid) continue;
            TextFeedItem item;
            item.id = aid;
            item.source_id = src->sourceId();
            item.source_name = src->displayName();
            item.category = src->category();
            item.title = title;
            item.url = url;
            openArticle(item);
            return;
        }
    }

    if (!title.empty() || !url.empty()) {
        active_article_source_id_ = sid;
        TextArticle article;
        article.id = !aid.empty() ? aid : ("ext-text:" + url);
        article.source_name = sid.empty() ? "外部阅读" : sid;
        article.category = !doi.empty() ? "journal" : "blog";
        article.title = title.empty() ? url : title;
        article.url = url;
        article.doi = doi;
        article.subtitle = article.source_name + (doi.empty() ? "" : (" · DOI: " + doi));
        showArticle(std::move(article));
    }
}

void TextWorkbench::promptSearch() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string q) {
                brls::sync([this, alive, q = std::move(q)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    search_query_ = q;
                    has_article_ = false;
                    refresh();
                });
            },
            "检索文字与学术期刊",
            "按标题、来源或关键词筛选订阅流",
            128,
            search_query_);
    }
}

} // namespace omnidesk::host
