#include "omnidesk/host/text_workbench.hpp"
#include "omnidesk/host/async_load.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"

#include "omnidesk/host/kernel.hpp"
#include "omnidesk/selection_action_popover.hpp"
#include "omnidesk/ui_theme.hpp"

#include <algorithm>
#include <cctype>
#include <functional>
#include <future>
#include <set>

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

std::string truncateUtf8Text(const std::string& text, size_t max_chars) {
    if (text.empty()) return "";
    size_t chars = 0;
    size_t i = 0;
    while (i < text.size()) {
        const unsigned char c = static_cast<unsigned char>(text[i]);
        size_t adv = 1;
        if ((c & 0x80) == 0) adv = 1;
        else if ((c & 0xE0) == 0xC0) adv = 2;
        else if ((c & 0xF0) == 0xE0) adv = 3;
        else if ((c & 0xF8) == 0xF0) adv = 4;
        if (i + adv > text.size()) break;
        i += adv;
        if (++chars >= max_chars) {
            return i < text.size() ? (text.substr(0, i) + "…") : text;
        }
    }
    return text;
}

} // namespace

TextWorkbench::TextWorkbench(std::function<void(const std::string&)> on_entity_focus,
                             std::function<void()> on_open_marketplace)
    : brls::Box(brls::Axis::COLUMN),
      on_entity_focus_(std::move(on_entity_focus)),
      on_open_marketplace_(std::move(on_open_marketplace)) {
    setGrow(1.0f);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    auto* header = new brls::Box(brls::Axis::ROW);
    header->setId("omnidesk/pane_header");
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(10);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->addView(ui::makeLabel("文字 · 社区 · 科学期刊 (Text Workbench)", 24.0f, "brls/text"));
    status_label_ = ui::makeLabel("个人博客 · 知乎 · Linux.do · Reddit · arXiv / Nature / PubMed", 14.5f, "font/grey");
    status_label_->setMarginTop(3);
    left->addView(status_label_);
    header->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    date_filter_btn_ = ui::makeButton("📅 日历查找", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptDateFilter();
    }, false, 36.0f);
    date_filter_btn_->setMarginRight(8);
    right->addView(date_filter_btn_);
    auto* s_btn = ui::makeButton("检索文章 /", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptSearch();
    }, false, 36.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    right->addView(ui::makeButton("阅读插件管理", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
            on_open_marketplace_();
        }
    }, false, 36.0f));
    header->addView(right);
    addView(header);

    category_bar_ = new brls::Box(brls::Axis::ROW);
    category_bar_->setId("omnidesk/pane_filters");
    category_bar_->setMarginBottom(8);
    addView(category_bar_);

    channel_bar_ = new brls::Box(brls::Axis::ROW);
    channel_bar_->setMarginBottom(10);
    channel_bar_->setVisibility(brls::Visibility::GONE);
    addView(channel_bar_);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setGrow(1.0f);
    split->setWidthPercentage(100);

    auto* feed_scroll = new brls::ScrollingFrame();
    feed_scroll->setId("omnidesk/pane_primary");
    feed_scroll->setWidthPercentage(37);
    feed_scroll->setHeightPercentage(100);
    feed_scroll->setMarginRight(16);
    feed_box_ = new brls::Box(brls::Axis::COLUMN);
    feed_box_->setWidthPercentage(100);
    feed_scroll->setContentView(feed_box_);
    split->addView(feed_scroll);

    auto* reader_scroll = new brls::ScrollingFrame();
    reader_scroll->setId("omnidesk/pane_secondary");
    reader_scroll->setWidthPercentage(63);
    reader_scroll->setHeightPercentage(100);
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
    reader_scroll->setContentView(reader_box_);
    split->addView(reader_scroll);

    addView(split);

    if (HostKernel::instance().isInitialized()) {
        recommend_sub_ = HostKernel::instance().bus().subscribe(
            "entity.recommend.updated",
            "host.text_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
    }

    refresh();
}

TextWorkbench::~TextWorkbench() {
    alive_->store(false, std::memory_order_release);
    if (recommend_sub_ != 0 && HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().unsubscribe(recommend_sub_);
        recommend_sub_ = 0;
    }
    feed_request_->cancel();
    article_request_->cancel();
    action_request_->cancel();
    if (translate_request_) {
        translate_request_->cancel();
        translate_request_.reset();
    }
}

void TextWorkbench::refresh() {
    feed_cache_.clear();
    loadCurrentFeed();
}

void TextWorkbench::loadCurrentFeed() {
    feed_request_->cancel();
    feed_request_ = CancelToken::create();
    auto& kernel = HostKernel::instance();
    feed_items_.clear();
    provenance_dates_.clear();

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().textSources();
        const bool has_recipe_cap = kernel.capabilities().has("feed.recipe.v1");
        if (!sources.empty() || has_recipe_cap) {
            const std::string cache_key = active_category_ + "|" + active_channel_id_ + "|" + search_query_ + "|" + curation_scope_ + "|" + curation_date_;
            if (auto it = feed_cache_.find(cache_key); it != feed_cache_.end()) {
                cached_recipe_pills_ = it->second.recipes;
                feed_items_ = it->second.items;
                active_channels_ = it->second.channels;
                status_label_->setText("已接入 " + std::to_string(sources.size()) + " 个实时 TextSource 插件");
                finishRefresh();
                loadProvenance();
                return;
            }

            status_label_->setText("正在加载文章列表…");
            rebuildCategoryPills();
            rebuildChannelPills();
            struct FeedLoadResult {
                std::vector<TextFeedItem> items;
                std::vector<TextChannel> channels;
                std::vector<std::pair<std::string, std::string>> recipes;
            };
            loadUiInBackground(kernel.uiTasks(), "text.feed", feed_request_,
                [sources, query = search_query_, category = active_category_,
                 channel_id = active_channel_id_, scope = curation_scope_, date = curation_date_](const auto& cancel) {
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
                            [source, source_category, multi, category, channel_id, query, scope, date, curator, cancel]() {
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
                                auto items = use_channel
                                                 ? source->fetchFeedInChannel(channel_id, query)
                                                 : source->fetchFeed(query);
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
                    feed_cache_[cache_key] = {result.items, result.channels, result.recipes};
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
                        status_label_->setText("已接入 " + std::to_string(count) + " 个实时 TextSource 插件");
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
    loadUiInBackground(kernel.uiTasks(), "text.provenance", feed_request_,
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
            rebuildFeed();
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
        auto* b = ui::makeButton(label, [this, alive = alive_, cid]() {
            brls::sync([this, alive, cid]() {
                if (!alive->load(std::memory_order_acquire)) return;
                active_category_ = cid;
                active_channel_id_.clear();
                active_channels_.clear();
                has_article_ = false;
                loadCurrentFeed();
            });
        }, active_category_ == cid, 34.0f);
        b->setMarginRight(8);
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
        auto* all_btn = ui::makeButton("全部栏目", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                active_channel_id_.clear();
                has_article_ = false;
                loadCurrentFeed();
            });
        }, active_channel_id_.empty(), 32.0f);
        all_btn->setMarginRight(6);
        channel_bar_->addView(all_btn);

        for (const auto& ch : active_channels_) {
            std::string label = ch.name.empty() ? ch.id : ch.name;
            if (!ch.badge.empty()) {
                label += " · " + ch.badge;
            } else if (ch.requires_auth) {
                label += " (需登录)";
            }
            auto* btn = ui::makeButton(label, [this, alive = alive_, cid = ch.id]() {
                brls::sync([this, alive, cid]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    active_channel_id_ = cid;
                    has_article_ = false;
                    loadCurrentFeed();
                });
            }, active_channel_id_ == ch.id, 32.0f);
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
            auto* button = ui::makeButton(label, [this, alive = alive_, scope = option.first]() {
                if (!alive->load(std::memory_order_acquire)) return;
                curation_scope_ = scope;
                has_article_ = false;
                loadCurrentFeed();
            }, curation_scope_ == option.first || (active_channel_id_ == "interactive" &&
                option.first == "all" && curation_scope_ == "latest"), 32.0f);
            button->setMarginRight(6);
            channel_bar_->addView(button);
        }
    }
}

void TextWorkbench::rebuildFeed() {
    feed_box_->clearViews();
    if (feed_items_.empty()) {
        auto* card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
        card->addView(ui::makeLabel("当前分类下暂无内容。", 15.0f, "font/grey"));
        feed_box_->addView(card);
        return;
    }

    for (const auto& item : feed_items_) {
        const bool selected = (has_article_ && active_article_.id == item.id);
        auto* card = ui::makeCard(selected ? "desktop/active" : "desktop/surface", 16.0f, 12.0f);
        card->setMarginBottom(10);
        card->setFocusable(true);
        if (selected) card->setBorderColor(ui::color("domain/text"));

        auto* top = new brls::Box(brls::Axis::ROW);
        top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top->setMarginBottom(6);
        top->addView(ui::makePill(item.source_name, "status/info_bg", "domain/text"));
        top->addView(ui::makeLabel("▲ " + std::to_string(item.score) + " · 💬 " + std::to_string(item.comment_count),
                                   13.5f, "font/grey"));
        card->addView(top);
        if (auto it = provenance_dates_.find(item.source_id + ":" + item.id); it != provenance_dates_.end()) {
            card->addView(ui::makeLabel("曾入选每日精选 · " + it->second, 13.5f, "font/grey"));
        }

        auto* title = ui::makeLabel(item.title, 16.5f, "brls/text", 1.4f);
        if (!item.excerpt.empty()) {
            title->setMarginBottom(6);
        }
        card->addView(title);

        if (!item.excerpt.empty()) {
            auto* ex = ui::makeLabel(truncateUtf8Text(item.excerpt, 140), 14.5f, "font/grey", 1.45f);
            card->addView(ex);
        }

        card->registerClickAction([this, alive = alive_, item](brls::View*) {
            brls::sync([this, alive, item]() {
                if (alive->load(std::memory_order_acquire)) {
                    openArticle(item);
                }
            });
            return true;
        });
        feed_box_->addView(card);
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
        std::string summary = active_article_.title;
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
    rebuildFeed();
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
    top_row->addView(ui::makePill(active_article_.source_name, "status/info_bg", "domain/text"));

    auto* mode_row = new brls::Box(brls::Axis::ROW);
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
            false, 32.0f);
        tr_btn->setMarginLeft(6);
        mode_row->addView(tr_btn);
    }
    if (kernel.isInitialized() && kernel.capabilities().has("web.embed.v1") && !active_article_.url.empty()) {
        auto* web_btn = ui::makeButton("Chrome 原页", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) openActiveInChrome();
        }, false, 32.0f);
        web_btn->setMarginLeft(6);
        mode_row->addView(web_btn);
    }
    const bool is_journal = (active_article_.category == "journal") ||
                            !active_article_.doi.empty() ||
                            !resolveJournalPdfUrl(active_article_).empty();
    if (kernel.isInitialized() && is_journal && kernel.capabilities().has("book.bilingual_pdf.v1")) {
        auto* pdf_btn = ui::makeButton("BabelDOC 精译 PDF", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) handoffJournalToBabelDoc();
        }, false, 32.0f);
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
        auto* btn = ui::makeButton(md.label, [this, alive = alive_, m = md.m]() {
            brls::sync([this, alive, m]() {
                if (!alive->load(std::memory_order_acquire)) return;
                bilingual_mode_ = m;
                rebuildReader();
            });
        }, bilingual_mode_ == md.m, 32.0f);
        btn->setMarginLeft(6);
        mode_row->addView(btn);
    }
    top_row->addView(mode_row);
    doc->addView(top_row);

    auto* h1 = ui::makeLabel(active_article_.title, 23.0f, "brls/text", 1.35f);
    h1->setMarginBottom(6);
    doc->addView(h1);

    std::string meta = active_article_.subtitle;
    if (!active_article_.doi.empty()) meta += " · DOI: " + active_article_.doi;
    auto* meta_lbl = ui::makeLabel(meta, 14.5f, "font/grey");
    meta_lbl->setMarginBottom(12);
    doc->addView(meta_lbl);

    doc->addView(ui::makeDivider(8.0f));

    auto openPopoverFor = [this](const std::string& text) {
        auto& k = HostKernel::instance();
SelectionContext sctx;
sctx.context_sentence = text;
sctx.source_title = active_article_.title;
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

    for (const auto& p : active_article_.paragraphs) {
        if (p.kind == "heading") {
            if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
                auto* hl = ui::makeLabel(p.original, 19.0f, "brls/text");
                hl->setMarginTop(12);
                hl->setMarginBottom(p.translated.empty() ? 6 : 2);
                doc->addView(hl);
            }
            if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
                auto* htr = ui::makeLabel(p.translated, 17.0f, "font/grey");
                htr->setMarginTop(2);
                htr->setMarginBottom(6);
                doc->addView(htr);
            }
            continue;
        }
        if (p.kind == "code" || p.kind == "quote" || p.kind == "formula") {
            auto* blk = ui::makeCard("desktop/soft", 14.0f, 8.0f);
            blk->setMarginTop(8);
            blk->setMarginBottom(8);
            if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
                blk->addView(ui::makeLabel(p.original, 15.0f, "brls/text", 1.48f));
            }
            if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
                auto* tr = ui::makeLabel(p.translated, 15.0f, "font/grey", 1.48f);
                tr->setMarginTop(6);
                blk->addView(tr);
            }
            if (p.kind == "quote" && !p.original.empty()) {
                blk->setFocusable(true);
                blk->registerClickAction([openPopoverFor, text = p.original](brls::View*) {
                    openPopoverFor(text);
                    return true;
                });
            }
            doc->addView(blk);
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
            para_box->addView(ui::makeLabel(p.original, 16.5f, "brls/text", 1.65f));
        }
        if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
            auto* tr = ui::makeLabel(p.translated, 15.5f, "font/grey", 1.65f);
            tr->setMarginTop(4);
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
            active_category_ = "daily_curation";
            curation_scope_ = "selected";
            curation_date_ = d;
            has_article_ = false;
            refresh();
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
    action_request_ = req;
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

void TextWorkbench::openActiveInChrome() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("web.embed.v1") || active_article_.url.empty()) {
        return;
    }
    const std::string url = active_article_.url;
    const std::string title = active_article_.title;
    action_request_->cancel();
    action_request_ = CancelToken::create();
    loadUiInBackground(kernel.uiTasks(), "text.chrome", action_request_,
        [url, title](const auto& cancel) {
            return HostKernel::instance().capabilities().callCancellable("web.embed.v1", {
                {"action", "open"},
                {"url", url},
                {"title", title},
            }, cancel);
        },
        [alive = alive_](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (!res.ok) {
                brls::Application::notify("打开内嵌 Chrome 失败: " + res.error);
            }
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
