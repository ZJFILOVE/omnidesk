#pragma once

#include <atomic>
#include <cstddef>
#include <functional>
#include <memory>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include <borealis/core/box.hpp>
#include <borealis/views/recycler.hpp>

#include "omnidesk/async.hpp"
#include "omnidesk/export.h"
#include "omnidesk/types.hpp"

namespace omnidesk {

class CapabilityRegistry;
class EntityBus;
class UiTaskRunner;

enum class CommentDisplayMode {
    Parallel,
    TranslatedOnly,
    OriginalOnly,
};

struct OMNIDESK_API FlatCommentRow {
    std::string node_id;
    int depth = 0;
    bool collapsed = false;
    bool is_op = false;
    size_t hidden_descendants = 0;
    const CommentNode* node = nullptr;
};

/**
 * Reusable virtualized & collapsible nested CommentTreeView UI component.
 * Supports:
 * - Multi-level indentation with NanoVG vertical thread guide lines
 * - Subtree collapse/expand with hidden descendant counts
 * - OP / role badges
 * - On-demand single-comment translation via llm.translate.v1 (fallback llm.chat.v1)
 * - SelectionActionPopover integration (vocab.sm2.v1 / AI / clip_jump.v1)
 * - Normalized pagination & fetch via text.comments.v1 across zhihu, reddit,
 *   linuxdo, shuiyuan, v2ex, x, bilibili, youtube, and feed-recipe plugins.
 */
class OMNIDESK_API CommentTreeView : public brls::Box {
public:
    using SelectionHandler = std::function<void(const CommentNode& node)>;

    CommentTreeView();
    ~CommentTreeView() override;

    void setServices(CapabilityRegistry* caps, EntityBus* bus, UiTaskRunner* ui_tasks);
    void setArticleContext(std::string source_plugin_id,
                           std::string article_id,
                           std::string article_title = "",
                           std::string op_author = "");

    void setComments(std::vector<CommentNode> comments);
    const std::vector<CommentNode>& comments() const noexcept { return comments_; }

    void setDisplayMode(CommentDisplayMode mode);
    void setPageSize(size_t page_size);
    void setViewportHeight(float height_px); ///< >0 activates bounded RecyclerFrame virtualization
    void setSelectionHandler(SelectionHandler handler);

    void toggleCollapse(const std::string& node_id);
    void collapseAll();
    void expandAll();

    void fetchFromBackend(bool append_more = false);
    void translateComment(const std::string& node_id);

    /// Pure helper exposed for verification: flattens visible tree nodes respecting collapsed state.
    static std::vector<FlatCommentRow> flattenTree(
        const std::vector<CommentNode>& roots,
        const std::set<std::string>& collapsed_ids,
        const std::string& op_author);

    static size_t countDescendants(const CommentNode& node);
    static bool isNodeOp(const CommentNode& node, const std::string& op_author);

private:
    void rebuild();
    void rebuildWindowed(const std::vector<FlatCommentRow>& flat);
    void rebuildRecycler(const std::vector<FlatCommentRow>& flat);
    brls::Box* buildRowCard(const FlatCommentRow& row);
    bool hasCommentsProvider() const;
    std::string resolveProviderPluginId() const;

    CapabilityRegistry* caps_ = nullptr;
    EntityBus* bus_ = nullptr;
    UiTaskRunner* ui_tasks_ = nullptr;

    std::string source_plugin_id_;
    std::string article_id_;
    std::string article_title_;
    std::string op_author_;

    std::vector<CommentNode> comments_;
    std::set<std::string> collapsed_ids_;
    std::set<std::string> translating_ids_;
    std::unordered_map<std::string, std::string> translation_cache_;

    CommentDisplayMode display_mode_ = CommentDisplayMode::Parallel;
    size_t page_size_ = 24;
    size_t page_index_ = 0;
    bool show_all_window_ = false;
    float viewport_height_ = 0.0f;

    int backend_offset_ = 0;
    std::string backend_cursor_;
    std::string reddit_link_fullname_;
    nlohmann::json reddit_more_stubs_ = nlohmann::json::array();
    bool backend_has_more_ = false;
    bool backend_loading_ = false;
    std::string backend_status_;

    SelectionHandler selection_handler_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> fetch_cancel_;

    brls::Box* header_bar_ = nullptr;
    brls::Box* list_container_ = nullptr;
    brls::Box* pager_bar_ = nullptr;
    brls::RecyclerFrame* recycler_ = nullptr;
};

} // namespace omnidesk
