#include "omnidesk/comment_tree_view.hpp"

#include <algorithm>
#include <borealis/core/application.hpp>
#include <borealis/core/thread.hpp>
#include <cctype>
#include <nanovg.h>
#include <thread>

#include "omnidesk/capabilities.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/selection_action_popover.hpp"
#include "omnidesk/types_json.hpp"
#include "omnidesk/ui_task_runner.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk {
namespace {

constexpr float kIndentStepPx = 18.0f;
constexpr int kMaxIndentDepth = 6;

std::string asciiLower(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

const CommentNode* findNodeById(const std::vector<CommentNode>& nodes,
                                const std::string& id,
                                const std::string& path_prefix = "") {
    for (size_t i = 0; i < nodes.size(); ++i) {
        const std::string fallback_id =
            path_prefix.empty() ? ("c:" + std::to_string(i)) : (path_prefix + "." + std::to_string(i));
        const std::string& key = nodes[i].id.empty() ? fallback_id : nodes[i].id;
        if (key == id) return &nodes[i];
        if (const auto* child = findNodeById(nodes[i].replies, id, fallback_id)) {
            return child;
        }
    }
    return nullptr;
}

void collectIds(const std::vector<CommentNode>& nodes,
                std::set<std::string>& out,
                const std::string& path_prefix = "") {
    for (size_t i = 0; i < nodes.size(); ++i) {
        const std::string fallback_id =
            path_prefix.empty() ? ("c:" + std::to_string(i)) : (path_prefix + "." + std::to_string(i));
        const std::string& key = nodes[i].id.empty() ? fallback_id : nodes[i].id;
        if (!nodes[i].replies.empty()) {
            out.insert(key);
            collectIds(nodes[i].replies, out, fallback_id);
        }
    }
}

void seedInitialCollapsed(const std::vector<CommentNode>& nodes,
                          std::set<std::string>& collapsed_ids,
                          const std::string& path_prefix = "") {
    for (size_t i = 0; i < nodes.size(); ++i) {
        const std::string fallback_id =
            path_prefix.empty() ? ("c:" + std::to_string(i)) : (path_prefix + "." + std::to_string(i));
        const std::string& key = nodes[i].id.empty() ? fallback_id : nodes[i].id;
        if (nodes[i].collapsed) {
            collapsed_ids.insert(key);
        }
        seedInitialCollapsed(nodes[i].replies, collapsed_ids, fallback_id);
    }
}

void flattenRecursive(const std::vector<CommentNode>& nodes,
                      int depth,
                      const std::set<std::string>& collapsed_ids,
                      const std::string& op_author,
                      std::vector<FlatCommentRow>& out,
                      const std::string& path_prefix = "") {
    for (size_t i = 0; i < nodes.size(); ++i) {
        const auto& node = nodes[i];
        const std::string fallback_id =
            path_prefix.empty() ? ("c:" + std::to_string(i)) : (path_prefix + "." + std::to_string(i));
        const std::string& key = node.id.empty() ? fallback_id : node.id;
        const bool is_collapsed = (collapsed_ids.count(key) > 0);
        const size_t hidden = is_collapsed ? CommentTreeView::countDescendants(node) : 0;

        FlatCommentRow row;
        row.node_id = key;
        row.depth = depth;
        row.collapsed = is_collapsed;
        row.is_op = CommentTreeView::isNodeOp(node, op_author);
        row.hidden_descendants = hidden;
        row.node = &node;
        out.push_back(std::move(row));

        if (!is_collapsed && !node.replies.empty()) {
            flattenRecursive(node.replies, depth + 1, collapsed_ids, op_author, out, fallback_id);
        }
    }
}

class CommentIndentBox final : public brls::Box {
public:
    explicit CommentIndentBox(int depth)
        : brls::Box(brls::Axis::ROW),
          depth_(std::clamp(depth, 0, kMaxIndentDepth)) {
        setWidthPercentage(100);
        setMarginTop(6);
        if (depth_ > 0) {
            setPaddingLeft(static_cast<float>(depth_) * kIndentStepPx);
        }
    }

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style style,
              brls::FrameContext* ctx) override {
        if (vg && depth_ > 0) {
            const NVGcolor guide = ui::color("desktop/line");
            nvgSave(vg);
            nvgStrokeWidth(vg, 1.5f);
            nvgStrokeColor(vg, guide);
            for (int d = 0; d < depth_; ++d) {
                const float lx = x + static_cast<float>(d) * kIndentStepPx + 8.0f;
                nvgBeginPath(vg);
                nvgMoveTo(vg, lx, y + 2.0f);
                nvgLineTo(vg, lx, y + height - 2.0f);
                nvgStroke(vg);
            }
            nvgRestore(vg);
        }
        brls::Box::draw(vg, x, y, width, height, style, ctx);
    }

private:
    int depth_ = 0;
};

class CommentRecyclerCell final : public brls::RecyclerCell {
public:
    CommentRecyclerCell() {
        setAxis(brls::Axis::COLUMN);
        setWidthPercentage(100);
    }
    void setContent(brls::Box* content) {
        clearViews();
        if (content) addView(content);
    }
};

class CommentTreeDataSource final : public brls::RecyclerDataSource {
public:
    CommentTreeDataSource(std::vector<FlatCommentRow> rows,
                          std::function<brls::Box*(const FlatCommentRow&)> builder)
        : rows_(std::move(rows)), builder_(std::move(builder)) {}

    int numberOfSections(brls::RecyclerFrame*) override { return 1; }

    int numberOfRows(brls::RecyclerFrame*, int) override {
        return static_cast<int>(rows_.size());
    }

    brls::RecyclerCell* cellForRow(brls::RecyclerFrame* recycler, brls::IndexPath index) override {
        auto* cell = dynamic_cast<CommentRecyclerCell*>(recycler->dequeueReusableCell("comment_row"));
        if (!cell) cell = new CommentRecyclerCell();
        if (index.row >= 0 && static_cast<size_t>(index.row) < rows_.size() && builder_) {
            cell->setContent(builder_(rows_[static_cast<size_t>(index.row)]));
        }
        return cell;
    }

private:
    std::vector<FlatCommentRow> rows_;
    std::function<brls::Box*(const FlatCommentRow&)> builder_;
};

} // namespace

size_t CommentTreeView::countDescendants(const CommentNode& node) {
    size_t total = node.replies.size();
    for (const auto& r : node.replies) {
        total += countDescendants(r);
    }
    return total;
}

bool CommentTreeView::isNodeOp(const CommentNode& node, const std::string& op_author) {
    if (!op_author.empty() && !node.author.empty() &&
        asciiLower(node.author) == asciiLower(op_author)) {
        return true;
    }
    const std::string badge = asciiLower(node.role_badge);
    return badge == "op" || badge == "作者" || badge == "楼主" || badge == "up主" ||
           badge.find("op") != std::string::npos;
}

std::vector<FlatCommentRow> CommentTreeView::flattenTree(
    const std::vector<CommentNode>& roots,
    const std::set<std::string>& collapsed_ids,
    const std::string& op_author) {
    std::vector<FlatCommentRow> flat;
    flattenRecursive(roots, 0, collapsed_ids, op_author, flat);
    return flat;
}

CommentTreeView::CommentTreeView() : brls::Box(brls::Axis::COLUMN) {
    setWidthPercentage(100);

    header_bar_ = new brls::Box(brls::Axis::ROW);
    header_bar_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header_bar_->setAlignItems(brls::AlignItems::CENTER);
    header_bar_->setMarginBottom(8);
    addView(header_bar_);

    list_container_ = new brls::Box(brls::Axis::COLUMN);
    list_container_->setWidthPercentage(100);
    addView(list_container_);

    pager_bar_ = new brls::Box(brls::Axis::ROW);
    pager_bar_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    pager_bar_->setAlignItems(brls::AlignItems::CENTER);
    pager_bar_->setMarginTop(10);
    addView(pager_bar_);
}

CommentTreeView::~CommentTreeView() {
    alive_->store(false, std::memory_order_release);
    if (fetch_cancel_) {
        fetch_cancel_->cancel();
    }
    recycler_ = nullptr;
}

void CommentTreeView::setServices(CapabilityRegistry* caps, EntityBus* bus, UiTaskRunner* ui_tasks) {
    caps_ = caps;
    bus_ = bus;
    ui_tasks_ = ui_tasks;
}

void CommentTreeView::setArticleContext(std::string source_plugin_id,
                                        std::string article_id,
                                        std::string article_title,
                                        std::string op_author) {
    const bool changed =
        (source_plugin_id_ != source_plugin_id || article_id_ != article_id);
    source_plugin_id_ = std::move(source_plugin_id);
    article_id_ = std::move(article_id);
    article_title_ = std::move(article_title);
    op_author_ = std::move(op_author);
    if (changed) {
        backend_offset_ = 0;
        backend_cursor_.clear();
        reddit_link_fullname_.clear();
        reddit_more_stubs_ = nlohmann::json::array();
        backend_has_more_ = false;
        backend_status_.clear();
        page_index_ = 0;
        show_all_window_ = false;
    }
}

void CommentTreeView::setComments(std::vector<CommentNode> comments) {
    comments_ = std::move(comments);
    collapsed_ids_.clear();
    seedInitialCollapsed(comments_, collapsed_ids_);
    page_index_ = 0;
    rebuild();
}

void CommentTreeView::setDisplayMode(CommentDisplayMode mode) {
    if (display_mode_ == mode) return;
    display_mode_ = mode;
    rebuild();
}

void CommentTreeView::setPageSize(size_t page_size) {
    page_size_ = std::max<size_t>(4, page_size);
    page_index_ = 0;
    rebuild();
}

void CommentTreeView::setViewportHeight(float height_px) {
    viewport_height_ = height_px;
    rebuild();
}

void CommentTreeView::setSelectionHandler(SelectionHandler handler) {
    selection_handler_ = std::move(handler);
}

void CommentTreeView::toggleCollapse(const std::string& node_id) {
    if (collapsed_ids_.count(node_id) > 0) collapsed_ids_.erase(node_id);
    else collapsed_ids_.insert(node_id);
    rebuild();
}

void CommentTreeView::collapseAll() {
    collapsed_ids_.clear();
    collectIds(comments_, collapsed_ids_);
    page_index_ = 0;
    rebuild();
}

void CommentTreeView::expandAll() {
    collapsed_ids_.clear();
    page_index_ = 0;
    rebuild();
}

std::string CommentTreeView::resolveProviderPluginId() const {
    if (!caps_ || !caps_->has("text.comments.v1")) return "";
    const auto provs = caps_->providersFor("text.comments.v1");
    if (provs.empty()) return "";
    if (!source_plugin_id_.empty()) {
        for (const auto& p : provs) {
            if (p.plugin_id == source_plugin_id_) return p.plugin_id;
        }
        // Handle short sourceId matching plugin-<id>
        const std::string prefixed = "plugin-" + source_plugin_id_;
        for (const auto& p : provs) {
            if (p.plugin_id == prefixed ||
                p.plugin_id.find(source_plugin_id_) != std::string::npos) {
                return p.plugin_id;
            }
        }
        return "";
    }
    return provs.front().plugin_id;
}

bool CommentTreeView::hasCommentsProvider() const {
    return !resolveProviderPluginId().empty() && !article_id_.empty();
}

void CommentTreeView::fetchFromBackend(bool append_more) {
    const std::string provider = resolveProviderPluginId();
    if (provider.empty() || article_id_.empty() || !caps_) return;

    if (fetch_cancel_) fetch_cancel_->cancel();
    auto cancel = CancelToken::create();
    fetch_cancel_ = cancel;
    backend_loading_ = true;
    backend_status_ = append_more ? "正在加载更多评论..." : "正在从插件拉取嵌套评论树...";
    rebuild();

    nlohmann::json req = {
        {"action", "fetch_comments"},
        {"id", article_id_},
        {"article_id", article_id_},
        {"limit", 40},
    };
    if (append_more) {
        if (!reddit_link_fullname_.empty() && reddit_more_stubs_.is_array() && !reddit_more_stubs_.empty()) {
            nlohmann::json children = nlohmann::json::array();
            for (const auto& s : reddit_more_stubs_) {
                if (s.is_string()) children.push_back(s);
                else if (s.is_object() && s.contains("children") && s["children"].is_array()) {
                    for (const auto& cid : s["children"]) {
                        if (cid.is_string()) children.push_back(cid);
                        if (children.size() >= 40) break;
                    }
                }
                if (children.size() >= 40) break;
            }
            if (!children.empty()) {
                req["action"] = "fetch_more_comments";
                req["link_id"] = reddit_link_fullname_;
                req["post_author"] = op_author_;
                req["children"] = children;
                req["comments"] = toJsonArray(comments_);
            }
        } else if (!backend_cursor_.empty()) {
            req["cursor"] = backend_cursor_;
        } else {
            req["offset"] = backend_offset_;
        }
    }

    auto* caps_ptr = caps_;
    const std::string aid = article_id_;
    auto work = [this, alive = alive_, caps_ptr, provider, req, append_more, aid](
                    const std::shared_ptr<CancelToken>& c) {
        CapabilityResult res = caps_ptr->callProviderCancellable(provider, "text.comments.v1", req, c);
        brls::sync([this, alive, c, req, res, append_more, aid]() {
            if (!alive->load(std::memory_order_acquire) || (c && c->isCancelled())) return;
            if (article_id_ != aid) return;
            backend_loading_ = false;
            if (!res.ok) {
                backend_status_ = res.error.empty() ? "评论加载失败" : res.error;
                rebuild();
                return;
            }
            std::vector<CommentNode> fetched;
            if (res.data.is_object() && res.data.contains("comments") && res.data["comments"].is_array()) {
                fetched = fromJsonArray<CommentNode>(res.data["comments"]);
            }
            if (append_more && req.value("action", "") != "fetch_more_comments") {
                comments_.insert(comments_.end(),
                                 std::make_move_iterator(fetched.begin()),
                                 std::make_move_iterator(fetched.end()));
            } else if (!fetched.empty() || !append_more) {
                comments_ = std::move(fetched);
            }

            backend_has_more_ = res.data.value("has_more", false);
            if (res.data.contains("next_offset") && res.data["next_offset"].is_number_integer()) {
                backend_offset_ = res.data["next_offset"].get<int>();
            } else {
                backend_offset_ += static_cast<int>(comments_.size());
            }
            backend_cursor_ = res.data.value("next_cursor", std::string{});
            if (res.data.contains("paging") && res.data["paging"].is_object()) {
                backend_has_more_ = !res.data["paging"].value("is_end", true);
            }
            if (res.data.contains("link_fullname") && res.data["link_fullname"].is_string()) {
                reddit_link_fullname_ = res.data["link_fullname"].get<std::string>();
            }
            if (res.data.contains("more_stubs") && res.data["more_stubs"].is_array()) {
                reddit_more_stubs_ = res.data["more_stubs"];
                if (!reddit_more_stubs_.empty()) backend_has_more_ = true;
            } else if (req.value("action", "") == "fetch_more_comments") {
                reddit_more_stubs_ = nlohmann::json::array();
            }

            backend_status_ = "已加载 " + std::to_string(comments_.size()) + " 组主楼评论";
            rebuild();
        });
    };

    if (ui_tasks_) {
        ui_tasks_->submitInLane("comment_tree.fetch", std::move(work), cancel);
    } else {
        std::thread([work = std::move(work), cancel]() { work(cancel); }).detach();
    }
}

void CommentTreeView::translateComment(const std::string& node_id) {
    if (!caps_ || (!caps_->has("llm.translate.v1") && !caps_->has("llm.chat.v1"))) {
        brls::Application::notify("未装载翻译插件 (请安装 plugin-magpie-agent)");
        return;
    }
    const CommentNode* node = findNodeById(comments_, node_id);
    if (!node || node->body.empty()) return;
    if (translating_ids_.count(node_id) > 0) return;

    translating_ids_.insert(node_id);
    rebuild();

    auto* caps_ptr = caps_;
    const std::string text = node->body;
    const std::string aid = article_id_;
    auto cancel = CancelToken::create();
    auto work = [this, alive = alive_, caps_ptr, node_id, text, aid](
                    const std::shared_ptr<CancelToken>& c) {
        CapabilityResult res;
        if (caps_ptr->has("llm.translate.v1")) {
            res = caps_ptr->callCancellable("llm.translate.v1", {
                {"text", text},
                {"source_lang", "auto"},
                {"target_lang", "zh-CN"},
                {"context", article_title_},
            }, c);
        } else {
            res = caps_ptr->callCancellable("llm.chat.v1", {
                {"domain", "text"},
                {"prompt", "请将这条社区评论精准自然地翻译为简体中文（仅输出译文）：\n" + text},
            }, c);
        }
        brls::sync([this, alive, res, node_id, aid]() {
            if (!alive->load(std::memory_order_acquire)) return;
            translating_ids_.erase(node_id);
            if (article_id_ != aid) return;
            if (res.ok) {
                std::string tr = res.data.value("translated", res.data.value("reply", ""));
                if (!tr.empty()) {
                    translation_cache_[node_id] = std::move(tr);
                }
            } else {
                brls::Application::notify("单条评论翻译失败: " + res.error);
            }
            rebuild();
        });
    };
    if (ui_tasks_) {
        ui_tasks_->submit(std::move(work), cancel);
    } else {
        std::thread([work = std::move(work), cancel]() { work(cancel); }).detach();
    }
}

brls::Box* CommentTreeView::buildRowCard(const FlatCommentRow& row) {
    const auto& node = *row.node;
    auto* wrapper = new CommentIndentBox(row.depth);

    auto* c_card = ui::makeCard("desktop/soft", 14.0f, 10.0f);
    if (row.is_op) {
        c_card->setBorderColor(ui::color("brls/accent"));
    }

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    hdr->setAlignItems(brls::AlignItems::CENTER);

    auto* author_row = new brls::Box(brls::Axis::ROW);
    author_row->setAlignItems(brls::AlignItems::CENTER);
    author_row->addView(ui::makeLabel(node.author.empty() ? "匿名用户" : node.author, 15.0f, "brls/accent"));

    if (row.is_op) {
        auto* op_pill = ui::makePill("OP", "status/ok_bg", "status/ok_fg");
        op_pill->setMarginLeft(6);
        author_row->addView(op_pill);
    }
    if (!node.role_badge.empty() && asciiLower(node.role_badge) != "op") {
        auto* rb = ui::makePill(node.role_badge, "desktop/surface", "font/grey");
        rb->setMarginLeft(6);
        author_row->addView(rb);
    }
    std::string meta_txt = " · ▲ " + std::to_string(node.score);
    if (!node.created_at.empty()) meta_txt = " · " + node.created_at + meta_txt;
    if (row.collapsed && row.hidden_descendants > 0) {
        meta_txt += " · 已折叠 " + std::to_string(row.hidden_descendants) + " 条回复";
    }
    author_row->addView(ui::makeLabel(meta_txt, 13.0f, "font/grey"));
    hdr->addView(author_row);

    auto* act_row = new brls::Box(brls::Axis::ROW);
    act_row->setAlignItems(brls::AlignItems::CENTER);

    const bool is_translating = (translating_ids_.count(row.node_id) > 0);
    std::string cached_tr;
    if (auto it = translation_cache_.find(row.node_id); it != translation_cache_.end()) {
        cached_tr = it->second;
    } else if (!node.translated_body.empty()) {
        cached_tr = node.translated_body;
    }

    if (caps_ && (caps_->has("llm.translate.v1") || caps_->has("llm.chat.v1"))) {
        auto* tr_btn = ui::makeButton(
            is_translating ? "译..." : (cached_tr.empty() ? "译" : "重译"),
            [this, alive = alive_, nid = row.node_id]() {
                if (alive->load(std::memory_order_acquire)) translateComment(nid);
            },
            false, 30.0f);
        tr_btn->setMarginRight(6);
        act_row->addView(tr_btn);
    }

    auto* sel_btn = ui::makeButton(
        "划词",
        [this, alive = alive_, node_copy = node]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (selection_handler_) {
                selection_handler_(node_copy);
                return;
            }
            SelectionContext sctx;
            sctx.context_sentence = node_copy.body;
            sctx.source_title = article_title_;
            sctx.source_id = source_plugin_id_;
            sctx.domain = ContentDomain::Text;
            sctx.capabilities = caps_;
            sctx.bus = bus_;
            sctx.ui_tasks = ui_tasks_;
            SelectionActionPopover::show(std::move(sctx));
        },
        false, 30.0f);
    sel_btn->setMarginRight(6);
    act_row->addView(sel_btn);

    const bool can_collapse = !node.replies.empty() || !row.collapsed;
    if (can_collapse) {
        auto* toggle = ui::makeButton(
            row.collapsed ? ("展开 (" + std::to_string(row.hidden_descendants) + ")") : "折叠",
            [this, alive = alive_, nid = row.node_id]() {
                brls::sync([this, alive, nid]() {
                    if (alive->load(std::memory_order_acquire)) toggleCollapse(nid);
                });
            },
            false, 30.0f);
        act_row->addView(toggle);
    }
    hdr->addView(act_row);
    c_card->addView(hdr);

    if (!row.collapsed) {
        if (display_mode_ != CommentDisplayMode::TranslatedOnly || cached_tr.empty()) {
            auto* b_lbl = ui::makeLabel(node.body, 15.5f, "brls/text", 1.52f);
            b_lbl->setMarginTop(8);
            c_card->addView(b_lbl);
        }
        if (!cached_tr.empty() && display_mode_ != CommentDisplayMode::OriginalOnly) {
            auto* t_lbl = ui::makeLabel(cached_tr, 14.5f, "font/grey", 1.50f);
            t_lbl->setMarginTop(6);
            c_card->addView(t_lbl);
        }
    }

    wrapper->addView(c_card);
    return wrapper;
}

void CommentTreeView::rebuild() {
    if (!header_bar_ || !list_container_ || !pager_bar_) return;
    header_bar_->clearViews();
    list_container_->clearViews();
    pager_bar_->clearViews();
    recycler_ = nullptr;

    const auto flat = flattenTree(comments_, collapsed_ids_, op_author_);

    auto* left = new brls::Box(brls::Axis::ROW);
    left->setAlignItems(brls::AlignItems::CENTER);
    left->addView(ui::makeLabel(
        "嵌套讨论树 · CommentTreeView (" + std::to_string(comments_.size()) +
            " 主楼 / " + std::to_string(flat.size()) + " 可见节点)",
        17.5f, "brls/text"));
    if (!backend_status_.empty()) {
        auto* st_p = ui::makePill(backend_status_, "desktop/soft", "font/grey");
        st_p->setMarginLeft(8);
        left->addView(st_p);
    }
    header_bar_->addView(left);

    auto* tools = new brls::Box(brls::Axis::ROW);
    tools->setAlignItems(brls::AlignItems::CENTER);
    if (hasCommentsProvider()) {
        auto* fetch_btn = ui::makeButton(
            backend_loading_ ? "拉取中..." : "拉取最新评论 (text.comments.v1)",
            [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire) && !backend_loading_) {
                    fetchFromBackend(false);
                }
            },
            false, 32.0f);
        fetch_btn->setMarginRight(6);
        tools->addView(fetch_btn);
    }
    if (!comments_.empty()) {
        auto* fold_btn = ui::makeButton("折叠全部", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) collapseAll();
        }, false, 32.0f);
        fold_btn->setMarginRight(6);
        tools->addView(fold_btn);

        tools->addView(ui::makeButton("展开全部", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) expandAll();
        }, false, 32.0f));
    }
    header_bar_->addView(tools);

    if (flat.empty()) {
        auto* empty_card = ui::makeCard("desktop/soft", 16.0f, 10.0f);
        empty_card->addView(ui::makeLabel(
            hasCommentsProvider()
                ? "当前暂无已缓存评论，点击右上角「拉取最新评论 (text.comments.v1)」实时加载。"
                : "暂无评论。",
            14.5f, "font/grey"));
        list_container_->addView(empty_card);
        return;
    }

    if (viewport_height_ > 0.0f) {
        rebuildRecycler(flat);
    } else {
        rebuildWindowed(flat);
    }

    if (backend_has_more_ && hasCommentsProvider()) {
        auto* more_btn = ui::makeButton(
            backend_loading_ ? "加载中..." : "加载更多后端评论 (text.comments.v1)",
            [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire) && !backend_loading_) {
                    fetchFromBackend(true);
                }
            },
            true, 36.0f);
        pager_bar_->addView(more_btn);
    }
}

void CommentTreeView::rebuildWindowed(const std::vector<FlatCommentRow>& flat) {
    const size_t total = flat.size();
    size_t start = 0;
    size_t end = total;
    if (!show_all_window_ && total > page_size_) {
        const size_t total_pages = (total + page_size_ - 1) / page_size_;
        if (page_index_ >= total_pages) page_index_ = total_pages - 1;
        start = page_index_ * page_size_;
        end = std::min(total, start + page_size_);
    }

    for (size_t i = start; i < end; ++i) {
        list_container_->addView(buildRowCard(flat[i]));
    }

    if (total > page_size_) {
        auto* nav = new brls::Box(brls::Axis::ROW);
        nav->setAlignItems(brls::AlignItems::CENTER);
        const size_t total_pages = (total + page_size_ - 1) / page_size_;
        if (!show_all_window_) {
            auto* prev = ui::makeButton("上一批", [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire) && page_index_ > 0) {
                    --page_index_;
                    rebuild();
                }
            }, false, 34.0f);
            prev->setMarginRight(6);
            nav->addView(prev);

            auto* info = ui::makeLabel(
                "第 " + std::to_string(page_index_ + 1) + "/" + std::to_string(total_pages) +
                    " 批 (" + std::to_string(start + 1) + "-" + std::to_string(end) + "/" +
                    std::to_string(total) + ")",
                13.5f, "font/grey");
            info->setMarginRight(6);
            nav->addView(info);

            auto* next = ui::makeButton("下一批", [this, alive = alive_, total_pages]() {
                if (alive->load(std::memory_order_acquire) && page_index_ + 1 < total_pages) {
                    ++page_index_;
                    rebuild();
                }
            }, false, 34.0f);
            next->setMarginRight(6);
            nav->addView(next);
        }
        nav->addView(ui::makeButton(
            show_all_window_ ? "恢复分页虚拟化" : ("展开全部 " + std::to_string(total) + " 条"),
            [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire)) {
                    show_all_window_ = !show_all_window_;
                    rebuild();
                }
            },
            false, 34.0f));
        pager_bar_->addView(nav);
    }
}

void CommentTreeView::rebuildRecycler(const std::vector<FlatCommentRow>& flat) {
    recycler_ = new brls::RecyclerFrame();
    recycler_->setWidthPercentage(100);
    recycler_->setHeight(viewport_height_);
    recycler_->estimatedRowHeight = 132.0f;
    recycler_->registerCell("comment_row", []() { return new CommentRecyclerCell(); });
    recycler_->setDataSource(
        new CommentTreeDataSource(
            flat, [this](const FlatCommentRow& row) { return buildRowCard(row); }),
        true);
    recycler_->reloadData();
    list_container_->addView(recycler_);
}

} // namespace omnidesk
