#include "omnidesk/host/text_workbench.hpp"

#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

std::vector<TextFeedItem> buildShowcaseFeed() {
    return {
        {"arxiv-1", "plugin-journals", "arXiv / Nature CS", "journal",
         "Microkernel Plugin Architectures for Heterogeneous AI Desktop Companions",
         "We analyze ABI-stable dynamic plugin isolation paired with bidirectional local LLM router control.",
         "Lin et al. · MIT / SJTU", "2026-11-12", "https://arxiv.org/abs/2611.00421",
         {"Systems", "AI Agent", "Peer-Reviewed"}, 18, 420, true},
        {"linuxdo-1", "plugin-linuxdo", "Linux.do", "linuxdo",
         "【实战】如何把 magpie 网关做成可热插拔桌面路由，把模型选择反向写回 CLI",
         "讨论跨 Harness 配额读取、本地回环网关路由与 C++ 桌面前端的事件解耦实践。",
         "zhu_dev", "2 小时前", "https://linux.do",
         {"Linux.do", "Magpie", "开发"}, 64, 219, true},
        {"zhihu-1", "plugin-zhihu", "知乎专栏", "zhihu",
         "为什么学术期刊、社区论坛和电子书必须彻底拆开做排版，而不能复用视频瀑布流？",
         "信息密度、公式/代码块渲染与嵌套评论树的阅读 ergonomics 完全不同。",
         "排版工程笔记", "昨天", "https://zhihu.com",
         {"UX 设计", "双语阅读", "排版"}, 41, 892, true},
        {"reddit-1", "plugin-reddit", "r/LocalLLaMA", "reddit",
         "Benchmarking Hybrid Prompt Caching Across Coding Harnesses & Local Gateways",
         "Detailed throughput and latency traces when switching models dynamically mid-session.",
         "u/systems_bench", "5h ago", "https://reddit.com/r/LocalLLaMA",
         {"Reddit", "Benchmarks"}, 93, 610, true},
        {"blog-1", "plugin-journals", "Engineering Blog", "blog",
         "Calm Surfaces: Designing Low-Fatigue Dual-Language Reading Interfaces",
         "Notes on paper canvas #F6F5F3, 0.82 typographic scaling, and keyboard hint navigation.",
         "Minimalist Ink", "2026-11-08", "https://example.org/calm-surfaces",
         {"Blog", "Typography"}, 7, 155, true},
    };
}

TextArticle buildShowcaseArticle(const TextFeedItem& feed) {
    TextArticle a;
    a.id = feed.id;
    a.source_name = feed.source_name;
    a.category = feed.category;
    a.title = feed.title;
    a.subtitle = feed.source_name + " · " + feed.author + " · " + feed.published_at;
    a.authors = feed.author;
    a.published_at = feed.published_at;
    a.url = feed.url;
    a.doi = (feed.category == "journal") ? "10.48550/arXiv.2611.00421" : "";
    a.tags = feed.tags;

    a.paragraphs = {
        {"p1", "heading", "1. Core Architectural Separation / 核心架构解耦设计", ""},
        {"p2", "paragraph",
         "Unlike media workbenches optimized for visual thumbnails, text-heavy ecosystems (personal blogs, Zhihu Q&A, Linux.do threads, Reddit, and peer-reviewed journals) require distraction-free reading measures and typographic hierarchy.",
         "与偏重视觉封面的影视工作台不同，高密度文字生态（个人博客、知乎深度问答、Linux.do 讨论贴、Reddit 以及同行评审科学期刊）需要无干扰的行宽控制、清晰的字体层级以及稳定可控的双语对齐体验。"},
        {"p3", "quote",
         "Decoupling data acquisition into standalone C++17 .so plugins ensures that site-specific DOM scrapers, Discourse/OAuth bridges, or DOI resolvers evolve at their own GitHub release cadence.",
         "将数据采集解耦为独立的 C++17 .so 插件，可让各站点的解析器、浏览器桥接和 DOI 查询以各自独立的 GitHub 仓库节奏快速演进，主工作台排版体验保持零回归。"},
        {"p4", "code",
         "// Capability handshake example for any TextWorkbench plugin\n"
         "ctx.registerCapability(\"text.summarize.v1\", [&](const json& req) { ... });",
         ""},
    };

    CommentNode c1{"c1", "kernel_reader", "研究员", "Collapsible nested comment trees make Linux.do and Reddit threads dramatically faster to scan than flat infinite scrolling.",
                   "可折叠的多层级嵌套评论树让 Linux.do 和 Reddit 长贴的阅读效率远高于传统扁平无限滚动。", "1小时前", 48, false, {}};
    CommentNode c1_1{"c1-1", "typography_fan", "回复", "Agreed — especially when pairing AI translation inline right beneath each technical reply.",
                     "赞同，尤其当 AI 译文能够紧贴每条技术回复下方内联展开时体验极佳。", "42分钟前", 19, false, {}};
    c1.replies.push_back(c1_1);
    CommentNode c2{"c2", "arxiv_bot", "期刊助手", "Citation graph & BibTeX export hook directly into plugin-pdf-babeldoc.",
                   "文献引用网络与 BibTeX 导出可直接流转至书本/PDF BabelDOC 双栏精读工作台。", "3小时前", 27, false, {}};
    a.comments = {c1, c2};
    return a;
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
    auto* s_btn = ui::makeButton("检索文章 /", [this]() { promptSearch(); }, false, 36.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    right->addView(ui::makeButton("阅读插件管理", [this]() {
        if (on_open_marketplace_) on_open_marketplace_();
    }, false, 36.0f));
    header->addView(right);
    addView(header);

    category_bar_ = new brls::Box(brls::Axis::ROW);
    category_bar_->setMarginBottom(12);
    addView(category_bar_);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setGrow(1.0f);
    split->setWidthPercentage(100);

    auto* feed_scroll = new brls::ScrollingFrame();
    feed_scroll->setWidthPercentage(38);
    feed_scroll->setHeightPercentage(100);
    feed_scroll->setMarginRight(14);
    feed_box_ = new brls::Box(brls::Axis::COLUMN);
    feed_box_->setWidthPercentage(100);
    feed_scroll->setContentView(feed_box_);
    split->addView(feed_scroll);

    auto* reader_scroll = new brls::ScrollingFrame();
    reader_scroll->setWidthPercentage(62);
    reader_scroll->setHeightPercentage(100);
    reader_box_ = new brls::Box(brls::Axis::COLUMN);
    reader_box_->setWidthPercentage(100);
    reader_scroll->setContentView(reader_box_);
    split->addView(reader_scroll);

    addView(split);
    refresh();
}

void TextWorkbench::refresh() {
    auto& kernel = HostKernel::instance();
    feed_items_.clear();
    bool live = false;

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().textSources();
        if (!sources.empty()) {
            live = true;
            for (const auto& src : sources) {
                if (active_category_ != "all" && src->category() != active_category_) continue;
                auto items = src->fetchFeed(search_query_);
                feed_items_.insert(feed_items_.end(), items.begin(), items.end());
            }
            status_label_->setText("已接入 " + std::to_string(sources.size()) + " 个实时 TextSource 插件");
        }
    }

    if (!live) {
        for (const auto& item : buildShowcaseFeed()) {
            if (active_category_ != "all" && item.category != active_category_) continue;
            if (!search_query_.empty() &&
                item.title.find(search_query_) == std::string::npos &&
                item.source_name.find(search_query_) == std::string::npos) {
                continue;
            }
            feed_items_.push_back(item);
        }
        status_label_->setText("高密度阅读排版预览（安装 知乎 / Linux.do / Reddit / 科学期刊 / Chrome 桥接插件后实时同步）");
    }

    if (!has_article_ && !feed_items_.empty()) {
        openArticle(feed_items_.front());
        return;
    }

    rebuildCategoryPills();
    rebuildFeed();
    rebuildReader();
}

void TextWorkbench::rebuildCategoryPills() {
    category_bar_->clearViews();
    struct Cat {
        const char* id;
        const char* label;
    };
    const Cat cats[] = {
        {"all", "全部阅读流"},
        {"blog", "个人博客"},
        {"zhihu", "知乎"},
        {"linuxdo", "Linux.do"},
        {"reddit", "Reddit"},
        {"journal", "科学期刊 (arXiv/Nature)"},
    };
    for (const auto& c : cats) {
        auto* b = ui::makeButton(c.label, [this, cid = std::string(c.id)]() {
            brls::sync([this, cid]() {
                active_category_ = cid;
                has_article_ = false;
                refresh();
            });
        }, active_category_ == c.id, 34.0f);
        b->setMarginRight(8);
        category_bar_->addView(b);
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
        auto* card = ui::makeCard(selected ? "desktop/active" : "desktop/surface", 14.0f, 10.0f);
        card->setMarginBottom(8);
        card->setFocusable(true);
        if (selected) card->setBorderColor(ui::color("domain/text"));

        auto* top = new brls::Box(brls::Axis::ROW);
        top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top->setMarginBottom(6);
        top->addView(ui::makePill(item.source_name, "status/info_bg", "domain/text"));
        top->addView(ui::makeLabel("▲ " + std::to_string(item.score) + " · 💬 " + std::to_string(item.comment_count),
                                   13.0f, "font/grey"));
        card->addView(top);

        auto* title = ui::makeLabel(item.title, 16.5f, "brls/text", 1.4f);
        title->setMarginBottom(5);
        card->addView(title);

        auto* ex = ui::makeLabel(item.excerpt, 14.0f, "font/grey", 1.4f);
        card->addView(ex);

        card->registerClickAction([this, item](brls::View*) {
            brls::sync([this, item]() { openArticle(item); });
            return true;
        });
        feed_box_->addView(card);
    }
}

void TextWorkbench::openArticle(const TextFeedItem& item) {
    bool fetched = false;
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        for (const auto& src : kernel.domains().textSources()) {
            if (src->sourceId() == item.source_id) {
                active_article_ = src->fetchArticle(item.id);
                fetched = true;
                break;
            }
        }
    }
    if (!fetched) {
        active_article_ = buildShowcaseArticle(item);
    }
    has_article_ = true;
    if (on_entity_focus_) {
        on_entity_focus_(active_article_.title);
    }
    if (kernel.isInitialized()) {
        kernel.bus().publish("entity.text.open", {{"id", active_article_.id}, {"title", active_article_.title}});
    }
    rebuildCategoryPills();
    rebuildFeed();
    rebuildReader();
}

void TextWorkbench::rebuildReader() {
    reader_box_->clearViews();
    if (!has_article_) return;

    auto* doc = ui::makeCard("desktop/surface", 22.0f, 12.0f);

    // Article meta + display mode pills
    auto* top_row = new brls::Box(brls::Axis::ROW);
    top_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    top_row->setAlignItems(brls::AlignItems::CENTER);
    top_row->setMarginBottom(10);
    top_row->addView(ui::makePill(active_article_.source_name, "status/info_bg", "domain/text"));

    auto* mode_row = new brls::Box(brls::Axis::ROW);
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
        auto* btn = ui::makeButton(md.label, [this, m = md.m]() {
            brls::sync([this, m]() {
                bilingual_mode_ = m;
                rebuildReader();
            });
        }, bilingual_mode_ == md.m, 30.0f);
        btn->setMarginLeft(6);
        mode_row->addView(btn);
    }
    top_row->addView(mode_row);
    doc->addView(top_row);

    auto* h1 = ui::makeLabel(active_article_.title, 22.0f, "brls/text", 1.35f);
    h1->setMarginBottom(6);
    doc->addView(h1);

    std::string meta = active_article_.subtitle;
    if (!active_article_.doi.empty()) meta += " · DOI: " + active_article_.doi;
    auto* meta_lbl = ui::makeLabel(meta, 14.0f, "font/grey");
    meta_lbl->setMarginBottom(12);
    doc->addView(meta_lbl);

    doc->addView(ui::makeDivider(8.0f));

    for (const auto& p : active_article_.paragraphs) {
        if (p.kind == "heading") {
            auto* hl = ui::makeLabel(p.original, 18.0f, "brls/text");
            hl->setMarginTop(12);
            hl->setMarginBottom(6);
            doc->addView(hl);
            continue;
        }
        if (p.kind == "code" || p.kind == "quote") {
            auto* blk = ui::makeCard("desktop/soft", 12.0f, 8.0f);
            blk->setMarginTop(8);
            blk->setMarginBottom(8);
            if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
                blk->addView(ui::makeLabel(p.original, 14.5f, "brls/text", 1.45f));
            }
            if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
                auto* tr = ui::makeLabel(p.translated, 14.5f, "font/grey", 1.45f);
                tr->setMarginTop(6);
                blk->addView(tr);
            }
            doc->addView(blk);
            continue;
        }
        if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
            auto* orig = ui::makeLabel(p.original, 16.0f, "brls/text", 1.6f);
            orig->setMarginTop(6);
            doc->addView(orig);
        }
        if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
            auto* tr = ui::makeLabel(p.translated, 15.0f, "font/grey", 1.6f);
            tr->setMarginTop(4);
            tr->setMarginBottom(8);
            doc->addView(tr);
        }
    }

    doc->addView(ui::makeDivider(14.0f));
    auto* cm_hdr = ui::makeLabel("嵌套讨论树 · CommentTree (" + std::to_string(active_article_.comments.size()) + " 组主楼)",
                                 17.5f, "brls/text");
    cm_hdr->setMarginBottom(10);
    doc->addView(cm_hdr);

    for (const auto& node : active_article_.comments) {
        renderCommentNode(doc, node, 0);
    }

    reader_box_->addView(doc);
}

void TextWorkbench::renderCommentNode(brls::Box* parent, const CommentNode& node, int depth) {
    const bool collapsed = (collapsed_comments_.count(node.id) > 0);
    auto* c_card = ui::makeCard("desktop/soft", 12.0f, 8.0f);
    c_card->setMarginTop(8);
    if (depth > 0) {
        c_card->setMarginLeft(static_cast<float>(std::min(depth, 4) * 20));
    }

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    hdr->setAlignItems(brls::AlignItems::CENTER);

    auto* author_row = new brls::Box(brls::Axis::ROW);
    author_row->setAlignItems(brls::AlignItems::CENTER);
    author_row->addView(ui::makeLabel(node.author, 14.5f, "brls/accent"));
    if (!node.role_badge.empty()) {
        auto* rb = ui::makePill(node.role_badge, "desktop/surface", "font/grey");
        rb->setMarginLeft(6);
        author_row->addView(rb);
    }
    auto* tm = ui::makeLabel(" · " + node.created_at + " · ▲ " + std::to_string(node.score), 13.0f, "font/grey");
    author_row->addView(tm);
    hdr->addView(author_row);

    auto* toggle = ui::makeButton(
        collapsed ? "展开" : "折叠",
        [this, nid = node.id]() {
            brls::sync([this, nid]() {
                if (collapsed_comments_.count(nid)) collapsed_comments_.erase(nid);
                else collapsed_comments_.insert(nid);
                rebuildReader();
            });
        },
        false, 26.0f);
    hdr->addView(toggle);
    c_card->addView(hdr);

    if (!collapsed) {
        auto* b_lbl = ui::makeLabel(node.body, 14.5f, "brls/text", 1.45f);
        b_lbl->setMarginTop(6);
        c_card->addView(b_lbl);
        if (!node.translated_body.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
            auto* t_lbl = ui::makeLabel(node.translated_body, 14.0f, "font/grey", 1.45f);
            t_lbl->setMarginTop(4);
            c_card->addView(t_lbl);
        }
    }

    parent->addView(c_card);
    if (!collapsed) {
        for (const auto& reply : node.replies) {
            renderCommentNode(parent, reply, depth + 1);
        }
    }
}

void TextWorkbench::promptSearch() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this](std::string q) {
                brls::sync([this, q]() {
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
