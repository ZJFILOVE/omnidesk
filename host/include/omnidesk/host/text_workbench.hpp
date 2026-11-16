#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/comment_tree_view.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::host {

class RecyclingGrid;

/**
 * Domain 2 Workbench ("文字 · 社区 · 科学期刊"):
 * High-readability typographic feed + bilingual reader + collapsible nested CommentTreeView
 * across Blogs, Zhihu, Linux.do, Reddit, and arXiv/Nature/PubMed plugins.
 */
class TextWorkbench : public brls::Box {
public:
    TextWorkbench(std::function<void(const std::string&)> on_entity_focus,
                  std::function<void()> on_open_marketplace);
    ~TextWorkbench() override;

    void refresh(bool force_remote = true);
    void promptSearch();
    void openExternalArticle(const nlohmann::json& payload);
    void applyDateFilter(const std::string& date, const std::string& domain = "");
    bool isShowingAuthPrompt() const;

private:
    enum class BilingualMode {
        Parallel,
        TranslatedOnly,
        OriginalOnly,
    };

    void rebuildCategoryPills();
    void rebuildChannelPills();
    void rebuildFeed();
    void rebuildFeedFooter();
    void rebuildReader();
    void renderCommentNode(brls::Box* parent, const CommentNode& node, int depth);
    void openArticle(const TextFeedItem& item);
    void showArticle(TextArticle article);
    void translateActiveArticle();
    void openActiveInChrome();
    void openLoginInChrome(const std::string& url, const std::string& title, bool external_window = false);
    void syncCookiesAndRefresh();
    void handoffJournalToBabelDoc();
    void promptDateFilter();
    void loadCurrentFeed(bool force_refresh = false);
    void loadMoreFeed();
    void finishRefresh();
    void loadProvenance();

    std::function<void(const std::string&)> on_entity_focus_;
    std::function<void()> on_open_marketplace_;

    std::string active_category_ = "all";
    std::string active_channel_id_;
    std::string search_query_;
    // latest | selected | all; dates apply only to the curator archive.
    std::string curation_scope_ = "latest";
    std::string curation_date_;
    std::unordered_map<std::string, std::string> provenance_dates_;
    BilingualMode bilingual_mode_ = BilingualMode::Parallel;

    std::vector<TextFeedItem> feed_items_;
    std::vector<TextChannel> active_channels_;
    std::vector<std::pair<std::string, std::string>> cached_recipe_pills_;
    struct CachedTextFeed {
        std::vector<TextFeedItem> items;
        std::vector<TextChannel> channels;
        std::vector<std::pair<std::string, std::string>> recipes;
        int page = 1;
        bool has_more = true;
    };
    std::unordered_map<std::string, CachedTextFeed> feed_cache_;
    int current_page_ = 1;
    bool loading_more_ = false;
    bool has_more_ = true;
    TextArticle active_article_;
    std::string active_article_source_id_;
    bool has_article_ = false;
    bool translating_ = false;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> feed_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> provenance_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> load_more_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> article_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> action_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> translate_request_;
    uint64_t recommend_sub_ = 0;

    brls::Box* category_bar_ = nullptr;
    brls::Box* channel_bar_ = nullptr;
    brls::Button* date_filter_btn_ = nullptr;
    brls::Label* status_label_ = nullptr;
    RecyclingGrid* feed_grid_ = nullptr;
    brls::ScrollingFrame* reader_scroll_ = nullptr;
    brls::Box* reader_box_ = nullptr;
    brls::Box* reader_article_box_ = nullptr;
    CommentTreeView* comment_tree_ = nullptr;
};

} // namespace omnidesk::host
