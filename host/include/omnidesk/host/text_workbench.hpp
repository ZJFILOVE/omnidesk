#pragma once

#include <borealis.hpp>
#include <functional>
#include <set>
#include <string>
#include <vector>
#include "omnidesk/types.hpp"

namespace omnidesk::host {

/**
 * Domain 2 Workbench ("文字 · 社区 · 科学期刊"):
 * High-readability typographic feed + bilingual reader + collapsible nested CommentTreeView
 * across Blogs, Zhihu, Linux.do, Reddit, and arXiv/Nature/PubMed plugins.
 */
class TextWorkbench : public brls::Box {
public:
    TextWorkbench(std::function<void(const std::string&)> on_entity_focus,
                  std::function<void()> on_open_marketplace);

    void refresh();
    void promptSearch();

private:
    enum class BilingualMode {
        Parallel,
        TranslatedOnly,
        OriginalOnly,
    };

    void rebuildCategoryPills();
    void rebuildFeed();
    void rebuildReader();
    void renderCommentNode(brls::Box* parent, const CommentNode& node, int depth);
    void openArticle(const TextFeedItem& item);

    std::function<void(const std::string&)> on_entity_focus_;
    std::function<void()> on_open_marketplace_;

    std::string active_category_ = "all";
    std::string search_query_;
    BilingualMode bilingual_mode_ = BilingualMode::Parallel;

    std::vector<TextFeedItem> feed_items_;
    TextArticle active_article_;
    bool has_article_ = false;
    std::set<std::string> collapsed_comments_;

    brls::Box* category_bar_ = nullptr;
    brls::Label* status_label_ = nullptr;
    brls::Box* feed_box_ = nullptr;
    brls::Box* reader_box_ = nullptr;
};

} // namespace omnidesk::host
