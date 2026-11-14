#include "omnidesk/host/text_workbench.hpp"
#include "omnidesk/host/async_load.hpp"

#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

#include <algorithm>
#include <functional>

namespace omnidesk::host {
namespace {

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

TextWorkbench::~TextWorkbench() {
    alive_->store(false, std::memory_order_release);
    feed_request_->cancel();
    article_request_->cancel();
    action_request_->cancel();
}

void TextWorkbench::refresh() {
    feed_request_->cancel();
    feed_request_ = CancelToken::create();
    auto& kernel = HostKernel::instance();
    feed_items_.clear();

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().textSources();
        if (!sources.empty()) {
            status_label_->setText("正在加载文章列表…");
            rebuildCategoryPills();
            loadInBackground(kernel.jobs(), "加载文章列表", feed_request_,
                [sources, query = search_query_, category = active_category_](const auto& cancel) {
                    std::vector<TextFeedItem> feed;
                    for (const auto& source : sources) {
                        if (cancel->isCancelled()) break;
                        const auto source_category = source->category();
                        const bool multi = source_category == "recipe" || source_category == "multi";
                        if (!multi && category != "all" && source_category != category) continue;
                        auto items = source->fetchFeed(query);
                        for (auto& item : items) {
                            if (!multi || itemMatchesCategory(item, source_category, category)) {
                                feed.push_back(std::move(item));
                            }
                        }
                    }
                    return feed;
                },
                [this, alive = alive_, count = sources.size()](std::vector<TextFeedItem> items) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    feed_items_ = std::move(items);
                    status_label_->setText("已接入 " + std::to_string(count) + " 个实时 TextSource 插件");
                    finishRefresh();
                });
            return;
        }
    }

    {
        for (const auto& item : buildShowcaseFeed()) {
            if (active_category_ != "all" && item.category != active_category_) continue;
            if (!search_query_.empty() &&
                item.title.find(search_query_) == std::string::npos &&
                item.source_name.find(search_query_) == std::string::npos) {
                continue;
            }
            feed_items_.push_back(item);
        }
        status_label_->setText("高密度阅读排版预览（安装 知乎 / Linux.do / Reddit / 科学期刊 / 声明式配方引擎 后实时同步）");
    }

    finishRefresh();
}

void TextWorkbench::finishRefresh() {
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
    std::vector<std::pair<std::string, std::string>> cats = {
        {"all", "全部阅读流"},
        {"recipe", "配方源 (20行)"},
        {"blog", "个人博客"},
        {"zhihu", "知乎"},
        {"linuxdo", "Linux.do"},
        {"reddit", "Reddit"},
        {"journal", "科学期刊 (arXiv/Nature)"},
    };

    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized() && kernel.capabilities().has("feed.recipe.v1")) {
        const auto res = kernel.capabilities().call("feed.recipe.v1", {{"action", "list_recipes"}});
        if (res.ok && res.data.is_object() && res.data.contains("recipes") && res.data["recipes"].is_array()) {
            for (const auto& r : res.data["recipes"]) {
                if (!r.is_object() || !r.value("enabled", true)) continue;
                const std::string rid = r.value("id", "");
                const std::string rname = r.value("name", rid);
                if (!rid.empty()) {
                    cats.emplace_back("recipe:" + rid, rname);
                }
            }
        }
    }

    for (const auto& [cid, label] : cats) {
        auto* b = ui::makeButton(label, [this, cid]() {
            brls::sync([this, cid]() {
                active_category_ = cid;
                has_article_ = false;
                refresh();
            });
        }, active_category_ == cid, 34.0f);
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
    article_request_->cancel();
    article_request_ = CancelToken::create();
    active_article_source_id_ = item.source_id;
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        for (const auto& src : kernel.domains().textSources()) {
            if (src->sourceId() == item.source_id) {
                status_label_->setText("正在加载文章…");
                loadInBackground(kernel.jobs(), "加载文章", article_request_,
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
    showArticle(buildShowcaseArticle(item));
}

void TextWorkbench::showArticle(TextArticle article) {
    active_article_ = std::move(article);
    auto& kernel = HostKernel::instance();
    has_article_ = true;
    if (on_entity_focus_) {
        on_entity_focus_(active_article_.title);
    }
    if (kernel.isInitialized()) {
        kernel.bus().publish("entity.text.open", {
            {"id", active_article_.id},
            {"title", active_article_.title},
            {"origin", "host_workbench"},
        });
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
    auto& kernel = HostKernel::instance();
    const bool can_translate = kernel.isInitialized() &&
                               (kernel.capabilities().has("feed.recipe.v1") ||
                                kernel.capabilities().has("llm.translate.v1") ||
                                kernel.capabilities().has("llm.chat.v1"));
    if (can_translate && !active_article_.id.empty()) {
        auto* tr_btn = ui::makeButton(
            translating_ ? "翻译中..." : "一键双语翻译",
            [this]() { translateActiveArticle(); },
            false, 30.0f);
        tr_btn->setMarginLeft(6);
        mode_row->addView(tr_btn);
    }
    if (kernel.isInitialized() && kernel.capabilities().has("web.embed.v1") && !active_article_.url.empty()) {
        auto* web_btn = ui::makeButton("Chrome 原页", [this]() {
            openActiveInChrome();
        }, false, 30.0f);
        web_btn->setMarginLeft(6);
        mode_row->addView(web_btn);
    }
    const bool is_journal = (active_article_.category == "journal") ||
                            !active_article_.doi.empty() ||
                            !resolveJournalPdfUrl(active_article_).empty();
    if (kernel.isInitialized() && is_journal && kernel.capabilities().has("book.bilingual_pdf.v1")) {
        auto* pdf_btn = ui::makeButton("BabelDOC 精译 PDF", [this]() {
            handoffJournalToBabelDoc();
        }, false, 30.0f);
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
            if (bilingual_mode_ != BilingualMode::TranslatedOnly || p.translated.empty()) {
                auto* hl = ui::makeLabel(p.original, 18.0f, "brls/text");
                hl->setMarginTop(12);
                hl->setMarginBottom(p.translated.empty() ? 6 : 2);
                doc->addView(hl);
            }
            if (!p.translated.empty() && bilingual_mode_ != BilingualMode::OriginalOnly) {
                auto* htr = ui::makeLabel(p.translated, 16.5f, "font/grey");
                htr->setMarginTop(2);
                htr->setMarginBottom(6);
                doc->addView(htr);
            }
            continue;
        }
        if (p.kind == "code" || p.kind == "quote" || p.kind == "formula") {
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
    action_request_ = CancelToken::create();
    translating_ = true;
    const TextArticle snapshot = active_article_;
    const std::string source_id = active_article_source_id_;
    status_label_->setText("正在后台执行双语翻译: " + snapshot.title + "...");
    rebuildReader();

    loadInBackground(kernel.jobs(), "双语翻译: " + snapshot.title, action_request_,
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
        [this, alive = alive_, target_id = snapshot.id](auto tuple_res) {
            if (!alive->load(std::memory_order_acquire)) return;
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
    loadInBackground(kernel.jobs(), "在 Chrome 打开: " + title, action_request_,
        [url](const auto& cancel) {
            return HostKernel::instance().capabilities().callCancellable("web.embed.v1", {
                {"action", "open"},
                {"url", url},
            }, cancel);
        },
        [alive = alive_](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (!res.ok) {
                brls::Application::notify("打开托管 Chrome 失败: " + res.error);
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
