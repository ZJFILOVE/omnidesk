#include "omnidesk/host/book_workbench.hpp"

#include <sstream>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

std::vector<BookItem> buildShowcaseBooks() {
    return {
        {"book-pdf-1", "plugin-pdf-babeldoc", "Designing Data-Intensive Applications (PDF)", "Martin Kleppmann",
         "pdf", "/nas/books/ddia.pdf", "", 142, 560, 25.4, true, 18, true},
        {"book-pdf-2", "plugin-pdf-babeldoc", "Attention Is All You Need · 论文精读", "Vaswani et al.",
         "pdf", "/nas/papers/transformer.pdf", "", 4, 15, 26.6, true, 9, true},
        {"book-epub-1", "plugin-epub-companion", "The Hitchhiker's Guide to the Galaxy", "Douglas Adams",
         "epub", "/nas/books/h2g2.epub", "", 68, 224, 30.3, true, 34, true},
        {"book-azw3-1", "plugin-epub-companion", "Dune (Annotated Bilingual Companion)", "Frank Herbert",
         "azw3", "/nas/books/dune.azw3", "", 112, 688, 16.2, true, 41, false},
    };
}

BookSpread buildShowcaseSpread(const BookItem& book, int page_num) {
    BookSpread s;
    s.book_id = book.id;
    s.page_number = std::max(1, page_num);
    s.total_pages = book.total_pages;
    s.chapter_title = (book.format == "pdf") ? "BabelDOC 版面保真对照页" : "EPUB 伴读双语精析页";
    s.blocks = {
        {"b1", "heading",
         "Section " + std::to_string(s.page_number) + " · Structured Systems & Memory Models",
         "第 " + std::to_string(s.page_number) + " 节 · 结构化系统与记忆模型"},
        {"b2", "paragraph",
         "Reliable systems continue to work correctly even when individual components experience partial degradation, network partitions, or transient latency spikes.",
         "即使个别组件遭遇局部降级、网络分区或瞬时延迟抖动，可靠的系统依然能够保持整体行为的正确性与数据一致性。",
         {"degradation", "transient", "partition"}},
        {"b3", "formula",
         "P(recall | t, I) = exp(-t / (I * ease_factor))",
         "间隔重复回忆概率公式：由 SM-2 记忆引擎根据反馈动态调整难度系数。",
         {"ease_factor"}},
    };
    return s;
}

std::vector<VocabCard> buildShowcaseVocab() {
    return {
        {"ephemeral", "ephemeral", "/ɪˈfɛmərəl/", "epi- (upon) + hemera (day) -> 朝生暮死的、短暂的",
         "短暂的；瞬息的；临时性的",
         "Containers spawn ephemeral staging directories inside tmpfs.",
         "The Hitchhiker's Guide to the Galaxy", 4, 2, 2.5},
        {"degradation", "degrade", "/ˌdɛɡrəˈdeɪʃən/", "de- (down) + gradus (step) -> 降级、退化",
         "降级；性能退化；磨损",
         "Graceful degradation preserves core reading continuity.",
         "Designing Data-Intensive Applications", 7, 3, 2.6},
    };
}

} // namespace

BookWorkbench::BookWorkbench(std::function<void(const std::string&)> on_entity_focus,
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
    header->setMarginBottom(12);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->addView(ui::makeLabel("书本与 PDF 阅读 · Book Workbench", 24.0f, "brls/text"));
    status_label_ = ui::makeLabel("2:3 立体书架 · BabelDOC PDF 双栏对照 · EPUB 词根伴读与影视切片跳转 · SM-2 生词本",
                                   14.5f, "font/grey");
    status_label_->setMarginTop(3);
    left->addView(status_label_);
    header->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    auto* s_btn = ui::makeButton("检索书库 /", [this]() { promptSearch(); }, false, 36.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    right->addView(ui::makeButton("阅读插件管理", [this]() {
        if (on_open_marketplace_) on_open_marketplace_();
    }, false, 36.0f));
    header->addView(right);
    addView(header);

    sub_tabs_box_ = new brls::Box(brls::Axis::ROW);
    sub_tabs_box_->setMarginBottom(14);
    addView(sub_tabs_box_);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setGrow(1.0f);
    scroll->setWidthPercentage(100);
    stage_box_ = new brls::Box(brls::Axis::COLUMN);
    stage_box_->setWidthPercentage(100);
    scroll->setContentView(stage_box_);
    addView(scroll);

    refresh();
}

void BookWorkbench::refresh() {
    auto& kernel = HostKernel::instance();
    books_.clear();
    vocab_cards_.clear();
    bool live = false;

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().bookSources();
        if (!sources.empty()) {
            live = true;
            for (const auto& src : sources) {
                auto b = src->listBooks("", search_query_);
                books_.insert(books_.end(), b.begin(), b.end());
                auto v = src->listVocab();
                vocab_cards_.insert(vocab_cards_.end(), v.begin(), v.end());
            }
            status_label_->setText("已接入 " + std::to_string(sources.size()) + " 个实时 BookSource 插件");
        }
    }

    if (!live) {
        books_ = buildShowcaseBooks();
        vocab_cards_ = buildShowcaseVocab();
        status_label_->setText("深度阅读布局预览（安装 plugin-pdf-babeldoc / plugin-epub-companion 后无缝接管书架与生词库）");
    }

    if (!has_active_book_ && !books_.empty()) {
        active_book_ = books_.front();
        has_active_book_ = true;
        active_spread_ = buildShowcaseSpread(active_book_, active_book_.current_page);
    }

    sub_tabs_box_->clearViews();
    struct Tab {
        SubMode m;
        const char* label;
    };
    const Tab tabs[] = {
        {SubMode::Bookshelf, "2:3 书架"},
        {SubMode::PdfBabelDoc, "PDF / BabelDOC 双栏对照"},
        {SubMode::EpubCompanion, "EPUB 伴读 & NAS 片段跳转"},
        {SubMode::VocabReview, "SM-2 生词记忆"},
    };
    for (const auto& t : tabs) {
        std::string txt = (t.m == SubMode::VocabReview)
                              ? ("SM-2 生词记忆 (" + std::to_string(vocab_cards_.size()) + ")")
                              : std::string(t.label);
        auto* btn = ui::makeButton(txt, [this, m = t.m]() {
            brls::sync([this, m]() { setSubMode(m); });
        }, sub_mode_ == t.m, 34.0f);
        btn->setMarginRight(8);
        sub_tabs_box_->addView(btn);
    }

    rebuildContent();
}

void BookWorkbench::setSubMode(SubMode m) {
    sub_mode_ = m;
    refresh();
}

void BookWorkbench::rebuildContent() {
    stage_box_->clearViews();
    switch (sub_mode_) {
        case SubMode::Bookshelf: renderBookshelf(); break;
        case SubMode::PdfBabelDoc: renderPdfBabelDoc(); break;
        case SubMode::EpubCompanion: renderEpubCompanion(); break;
        case SubMode::VocabReview: renderVocabReview(); break;
    }
}

void BookWorkbench::renderBookshelf() {
    auto* grid = new brls::Box(brls::Axis::COLUMN);
    brls::Box* row = nullptr;
    int idx = 0;

    for (const auto& book : books_) {
        if (!search_query_.empty() && book.title.find(search_query_) == std::string::npos) continue;
        if (idx % 4 == 0) {
            row = new brls::Box(brls::Axis::ROW);
            row->setWidthPercentage(100);
            row->setMarginBottom(14);
            grid->addView(row);
        }

        auto* card = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        card->setWidthPercentage(23.5f);
        if (idx % 4 != 3) card->setMarginRight(14);
        card->setFocusable(true);

        // Upright 2:3 Book Cover
        auto* cover = ui::makeCard("desktop/soft", 12.0f, 8.0f);
        cover->setHeight(210);
        cover->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        cover->setMarginBottom(10);

        auto* top_pills = new brls::Box(brls::Axis::ROW);
        top_pills->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        std::string fmt = book.format;
        for (char& ch : fmt) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
        top_pills->addView(ui::makePill(fmt, "status/ok_bg", "domain/book"));
        if (book.nas_synced) {
            top_pills->addView(ui::makePill("NAS", "desktop/surface", "font/grey"));
        }
        cover->addView(top_pills);

        cover->addView(ui::makeLabel(book.title, 16.0f, "brls/text", 1.35f));

        std::string prog = "P." + std::to_string(book.current_page) + "/" +
                           std::to_string(book.total_pages) + " · 生词 " + std::to_string(book.vocab_count);
        cover->addView(ui::makeLabel(prog, 13.0f, "font/grey"));
        card->addView(cover);

        card->addView(ui::makeLabel(book.author, 14.5f, "font/grey"));
        auto* pct = ui::makeLabel("阅读进度: " + std::to_string(static_cast<int>(book.progress_percent)) + "%",
                                  13.5f, "domain/book");
        pct->setMarginTop(4);
        card->addView(pct);

        card->registerClickAction([this, book](brls::View*) {
            brls::sync([this, book]() { openBook(book); });
            return true;
        });
        row->addView(card);
        ++idx;
    }

    stage_box_->addView(grid);
}

void BookWorkbench::openBook(const BookItem& book) {
    active_book_ = book;
    has_active_book_ = true;
    loadSpread(book.current_page);
    sub_mode_ = (book.format == "pdf") ? SubMode::PdfBabelDoc : SubMode::EpubCompanion;
    if (on_entity_focus_) {
        on_entity_focus_(book.title);
    }
    if (HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().publish("entity.book.open", {
                                                                     {"id", book.id},
                                                                     {"title", book.title},
                                                                     {"page", book.current_page},
                                                                 });
    }
    refresh();
}

void BookWorkbench::loadSpread(int page_num) {
    auto& kernel = HostKernel::instance();
    bool loaded = false;
    if (kernel.isInitialized()) {
        for (const auto& src : kernel.domains().bookSources()) {
            if (src->sourceId() == active_book_.source_id) {
                active_spread_ = src->fetchSpread(active_book_.id, page_num);
                loaded = true;
                break;
            }
        }
    }
    if (!loaded) {
        active_spread_ = buildShowcaseSpread(active_book_, page_num);
    }
}

void BookWorkbench::renderPdfBabelDoc() {
    auto* toolbar = ui::makeCard("desktop/surface", 12.0f, 10.0f);
    toolbar->setMarginBottom(12);
    auto* bar = new brls::Box(brls::Axis::ROW);
    bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    bar->setAlignItems(brls::AlignItems::CENTER);

    bar->addView(ui::makeLabel(active_book_.title + " · 第 " + std::to_string(active_spread_.page_number) +
                                   " / " + std::to_string(active_spread_.total_pages) + " 页",
                               16.5f, "brls/text"));

    auto* nav = new brls::Box(brls::Axis::ROW);
    auto* prev = ui::makeButton("上一页", [this]() {
        brls::sync([this]() {
            if (active_spread_.page_number > 1) loadSpread(active_spread_.page_number - 1);
            rebuildContent();
        });
    }, false, 34.0f);
    prev->setMarginRight(8);
    nav->addView(prev);

    auto* next = ui::makeButton("下一页", [this]() {
        brls::sync([this]() {
            loadSpread(active_spread_.page_number + 1);
            rebuildContent();
        });
    }, false, 34.0f);
    next->setMarginRight(8);
    nav->addView(next);

    nav->addView(ui::makeButton("BabelDOC 精校此页", [this]() {
        brls::Application::notify("已请求 book.bilingual_pdf.v1 / llm.chat.v1 重排对照第 " +
                                  std::to_string(active_spread_.page_number) + " 页");
    }, true, 34.0f));
    bar->addView(nav);
    toolbar->addView(bar);
    stage_box_->addView(toolbar);

    // Side-by-side 50/50 PDF Original vs BabelDOC Chinese Layout
    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    auto* orig_col = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    orig_col->setWidthPercentage(49);
    orig_col->setMarginRight(12);
    orig_col->addView(ui::makePill("Original PDF Layout Block", "desktop/soft", "font/grey"));
    for (const auto& blk : active_spread_.blocks) {
        auto* p = ui::makeLabel(blk.original, blk.kind == "heading" ? 18.0f : 15.5f, "brls/text", 1.55f);
        p->setMarginTop(12);
        orig_col->addView(p);
    }
    split->addView(orig_col);

    auto* trans_col = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    trans_col->setWidthPercentage(49);
    trans_col->addView(ui::makePill("BabelDOC 保真中文版面区块", "status/ok_bg", "domain/book"));
    for (const auto& blk : active_spread_.blocks) {
        auto* p = ui::makeLabel(blk.translated, blk.kind == "heading" ? 18.0f : 15.5f, "brls/text", 1.55f);
        p->setMarginTop(12);
        trans_col->addView(p);
    }
    split->addView(trans_col);

    stage_box_->addView(split);
}

void BookWorkbench::renderEpubCompanion() {
    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    // Left: Calm serif-like reader pane
    auto* reader = ui::makeCard("desktop/surface", 22.0f, 12.0f);
    reader->setWidthPercentage(58);
    reader->setMarginRight(14);
    reader->addView(ui::makeLabel(active_book_.title + " · EPUB 伴读正文", 19.0f, "brls/text"));

    std::vector<std::string> highlight_words;
    for (const auto& blk : active_spread_.blocks) {
        auto* o = ui::makeLabel(blk.original, 16.5f, "brls/text", 1.68f);
        o->setMarginTop(12);
        reader->addView(o);
        auto* t = ui::makeLabel(blk.translated, 15.0f, "font/grey", 1.6f);
        t->setMarginTop(4);
        reader->addView(t);
        highlight_words.insert(highlight_words.end(), blk.highlighted_words.begin(), blk.highlighted_words.end());
    }
    split->addView(reader);

    // Right: Sentence & Etymology Companion + NAS Clip Jump
    auto* comp = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    comp->setWidthPercentage(40);
    comp->addView(ui::makeLabel("句子精析 · 词根记忆 & 影视联动", 18.0f, "brls/text"));

    auto* word_row = new brls::Box(brls::Axis::ROW);
    word_row->setMarginTop(10);
    word_row->setMarginBottom(10);
    for (size_t i = 0; i < highlight_words.size(); ++i) {
        auto* b = ui::makeButton(highlight_words[i], [this, i]() {
            brls::sync([this, i]() {
                selected_word_idx_ = static_cast<int>(i);
                rebuildContent();
            });
        }, selected_word_idx_ == static_cast<int>(i), 32.0f);
        b->setMarginRight(6);
        word_row->addView(b);
    }
    comp->addView(word_row);

    const std::string focus_word =
        (!highlight_words.empty() && selected_word_idx_ < static_cast<int>(highlight_words.size()))
            ? highlight_words[selected_word_idx_]
            : "degradation";

    auto* ety_card = ui::makeCard("desktop/soft", 12.0f, 8.0f);
    ety_card->setMarginBottom(12);
    ety_card->addView(ui::makeLabel("选中词条: " + focus_word, 16.5f, "domain/book"));
    ety_card->addView(ui::makeLabel("• 词根拆解: 可调用 llm.chat.v1 生成语境句法解析与词根联想\n"
                                    "• 语境句: Reliable systems continue to work correctly even under " + focus_word,
                                    14.0f, "font/grey", 1.45f));
    comp->addView(ety_card);

    auto* jump_btn = ui::makeButton("查找关联影视 / NAS 对话原声片段 (clip_jump.v1)",
                                    [this, focus_word]() { triggerClipJump(focus_word); }, true, 38.0f);
    comp->addView(jump_btn);

    auto* add_btn = ui::makeButton("加入 SM-2 生词复习队列", [this, focus_word]() {
        if (HostKernel::instance().isInitialized()) {
            HostKernel::instance().bus().publish("entity.vocab.add", {{"word", focus_word}, {"book", active_book_.title}});
        }
        brls::Application::notify("已添加至 SM-2 生词本: " + focus_word);
    }, false, 36.0f);
    add_btn->setMarginTop(8);
    comp->addView(add_btn);

    split->addView(comp);
    stage_box_->addView(split);
}

void BookWorkbench::triggerClipJump(const std::string& word) {
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized() && kernel.capabilities().has("media.clip_jump.v1")) {
        auto res = kernel.capabilities().call("media.clip_jump.v1", {{"query", word}, {"book", active_book_.title}});
        if (res.ok) {
            brls::Application::notify("匹配到影视片段: " + res.data.value("summary", "即将打开播放器"));
            return;
        }
    }
    brls::Application::notify("未装载 media.clip_jump.v1 · 安装 plugin-fnos-nas 后可直达 NAS 本地字幕视频片段");
}

void BookWorkbench::renderVocabReview() {
    auto* deck = ui::makeCard("desktop/surface", 24.0f, 12.0f);
    if (vocab_cards_.empty()) {
        deck->addView(ui::makeLabel("生词本为空。", 18.0f, "font/grey"));
        stage_box_->addView(deck);
        return;
    }

    const VocabCard& card = vocab_cards_[vocab_idx_ % vocab_cards_.size()];
    deck->addView(ui::makePill("SM-2 间隔重复队列 · " + std::to_string((vocab_idx_ % vocab_cards_.size()) + 1) +
                                   " / " + std::to_string(vocab_cards_.size()),
                               "status/ok_bg", "domain/book"));

    auto* w_lbl = ui::makeLabel(card.word + "   " + card.phonetic, 26.0f, "brls/text");
    w_lbl->setMarginTop(14);
    deck->addView(w_lbl);

    auto* ety = ui::makeLabel("词源提示: " + card.etymology, 15.5f, "font/grey");
    ety->setMarginTop(6);
    deck->addView(ety);

    if (!vocab_revealed_) {
        auto* rev = ui::makeButton("显示完整释义与书内原句", [this]() {
            brls::sync([this]() {
                vocab_revealed_ = true;
                rebuildContent();
            });
        }, true, 40.0f);
        rev->setMarginTop(16);
        deck->addView(rev);
    } else {
        auto* ans = ui::makeCard("desktop/soft", 14.0f, 8.0f);
        ans->setMarginTop(14);
        ans->addView(ui::makeLabel("释义: " + card.meaning, 17.0f, "brls/text"));
        auto* ctx = ui::makeLabel("原句 (" + card.source_book + "): " + card.context_sentence, 15.0f, "font/grey");
        ctx->setMarginTop(6);
        ans->addView(ctx);
        deck->addView(ans);

        auto* rating = new brls::Box(brls::Axis::ROW);
        rating->setMarginTop(14);
        for (const auto& [label, days] : {std::pair<const char*, int>{"生疏 (1天)", 1},
                                          {"困难 (3天)", 3},
                                          {"掌握 (7天)", 7},
                                          {"秒记 (14天)", 14}}) {
            auto* b = ui::makeButton(label, [this, days]() {
                brls::sync([this, days]() {
                    vocab_cards_[vocab_idx_ % vocab_cards_.size()].interval_days = days;
                    vocab_idx_ = (vocab_idx_ + 1) % vocab_cards_.size();
                    vocab_revealed_ = false;
                    rebuildContent();
                });
            }, days == 7, 36.0f);
            b->setMarginRight(8);
            rating->addView(b);
        }
        deck->addView(rating);
    }

    stage_box_->addView(deck);
}

void BookWorkbench::promptSearch() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this](std::string q) {
                brls::sync([this, q]() {
                    search_query_ = q;
                    refresh();
                });
            },
            "检索本地与 NAS 书库",
            "支持按书名、作者或格式过滤",
            128,
            search_query_);
    }
}

} // namespace omnidesk::host
