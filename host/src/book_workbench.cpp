#include "omnidesk/host/book_workbench.hpp"
#include "omnidesk/host/async_load.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <sstream>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

std::string toLowerCopy(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

std::string extractTranslationText(const std::string& raw) {
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

std::string findContextSentenceForWord(const BookSpread& spread, const std::string& word) {
    const std::string needle = toLowerCopy(word);
    std::string fallback;
    for (const auto& blk : spread.blocks) {
        if (blk.original.empty()) continue;
        if (fallback.empty() && blk.kind == "paragraph") {
            fallback = blk.original;
        }
        if (!needle.empty() && toLowerCopy(blk.original).find(needle) != std::string::npos) {
            return blk.original;
        }
    }
    if (!fallback.empty()) return fallback;
    return spread.blocks.empty() ? std::string{} : spread.blocks.front().original;
}

std::string formatClipSeconds(double sec) {
    const int total = std::max(0, static_cast<int>(sec));
    const int m = total / 60;
    const int s = total % 60;
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", m, s);
    return std::string(buf);
}

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

BookWorkbench::~BookWorkbench() {
    alive_->store(false, std::memory_order_release);
    library_request_->cancel();
    spread_request_->cancel();
    action_request_->cancel();
}

void BookWorkbench::refresh() {
    library_request_->cancel();
    library_request_ = CancelToken::create();
    auto& kernel = HostKernel::instance();
    books_.clear();
    vocab_cards_.clear();

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().bookSources();
        if (!sources.empty()) {
            status_label_->setText("正在加载书库…");
            finishRefresh();
            loadInBackground(kernel.jobs(), "加载书库", library_request_,
                [sources, query = search_query_](const auto& cancel) {
                    std::pair<std::vector<BookItem>, std::vector<VocabCard>> library;
                    for (const auto& source : sources) {
                        if (cancel->isCancelled()) break;
                        auto books = source->listBooks("", query);
                        library.first.insert(library.first.end(), books.begin(), books.end());
                        if (cancel->isCancelled()) break;
                        auto vocab = source->listVocab();
                        library.second.insert(library.second.end(), vocab.begin(), vocab.end());
                    }
                    return library;
                },
                [this, alive = alive_, count = sources.size()](auto library) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    books_ = std::move(library.first);
                    vocab_cards_ = std::move(library.second);
                    mergeHostVocabFallback();
                    status_label_->setText("已接入 " + std::to_string(count) + " 个实时 BookSource 插件");
                    if (!has_active_book_ && !books_.empty()) {
                        active_book_ = books_.front();
                        has_active_book_ = true;
                        loadSpread(active_book_.current_page);
                    }
                    finishRefresh();
                });
            return;
        }
    }

    {
        books_ = buildShowcaseBooks();
        vocab_cards_ = buildShowcaseVocab();
        mergeHostVocabFallback();
        status_label_->setText("深度阅读布局预览（安装 plugin-pdf-babeldoc / plugin-epub-companion 后无缝接管书架与生词库）");
    }

    finishRefresh();
}

void BookWorkbench::finishRefresh() {
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
    clip_jump_results_.clear();
    clip_jump_query_.clear();
    selected_word_idx_ = 0;
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
                                                                     {"origin", "host_workbench"},
                                                                 });
    }
    refresh();
}

void BookWorkbench::loadSpread(int page_num) {
    spread_request_->cancel();
    spread_request_ = CancelToken::create();
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        for (const auto& src : kernel.domains().bookSources()) {
            if (src->sourceId() == active_book_.source_id) {
                status_label_->setText("正在加载书页…");
                loadInBackground(kernel.jobs(), "加载书页", spread_request_,
                    [src, book_id = active_book_.id, page_num](const auto&) {
                        return src->fetchSpread(book_id, page_num);
                    },
                    [this, alive = alive_](BookSpread spread) {
                        if (!alive->load(std::memory_order_acquire)) return;
                        status_label_->setText(spread.book_id.empty() ? "书页加载失败，可重试" : "书页已加载");
                        if (!spread.book_id.empty()) active_spread_ = std::move(spread);
                        rebuildContent();
                    });
                return;
            }
        }
    }
    active_spread_ = buildShowcaseSpread(active_book_, page_num);
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

    nav->addView(ui::makeButton(
        action_busy_ ? "精校中..." : "BabelDOC 精校此页",
        [this]() { refineCurrentPdfPage(); },
        true, 34.0f));
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
    const std::string context_sentence = findContextSentenceForWord(active_spread_, focus_word);
    const std::string analysis_key = active_book_.id + "|" + toLowerCopy(focus_word);

    std::string known_phonetic;
    std::string known_etymology;
    std::string known_meaning;
    for (const auto& vc : vocab_cards_) {
        if (toLowerCopy(vc.word) == toLowerCopy(focus_word)) {
            known_phonetic = vc.phonetic;
            known_etymology = vc.etymology;
            known_meaning = vc.meaning;
            break;
        }
    }

    auto* ety_card = ui::makeCard("desktop/soft", 12.0f, 8.0f);
    ety_card->setMarginBottom(12);
    ety_card->addView(ui::makeLabel(
        "选中词条: " + focus_word + (known_phonetic.empty() ? "" : ("  " + known_phonetic)),
        16.5f, "domain/book"));
    std::ostringstream ety_oss;
    if (!known_meaning.empty()) {
        ety_oss << "• 词库释义: " << known_meaning << "\n";
    }
    if (!known_etymology.empty()) {
        ety_oss << "• 词根词源: " << known_etymology << "\n";
    }
    if (auto it = word_analysis_cache_.find(analysis_key); it != word_analysis_cache_.end() && !it->second.empty()) {
        ety_oss << "• AI 语境拆解:\n" << it->second << "\n";
    } else if (known_etymology.empty()) {
        ety_oss << "• 词根拆解: 点击下方「AI 拆解词根与句法」即可结合当前段落语境生成词源与句法分析\n";
    }
    if (!context_sentence.empty()) {
        ety_oss << "• 语境原句: " << context_sentence;
    }
    ety_card->addView(ui::makeLabel(ety_oss.str(), 14.0f, "font/grey", 1.45f));
    comp->addView(ety_card);

    auto* ai_word_btn = ui::makeButton(
        action_busy_ ? "正在调用 AI 解析..." : "AI 拆解词根与句法 (llm.chat.v1)",
        [this, focus_word, context_sentence]() {
            analyzeFocusWord(focus_word, context_sentence);
        },
        false, 36.0f);
    ai_word_btn->setMarginBottom(8);
    comp->addView(ai_word_btn);

    auto* jump_btn = ui::makeButton("查找关联影视 / NAS 对话原声片段 (clip_jump.v1)",
                                    [this, focus_word]() { triggerClipJump(focus_word); }, true, 38.0f);
    comp->addView(jump_btn);

    auto* add_btn = ui::makeButton("加入 SM-2 生词复习队列", [this, focus_word, context_sentence]() {
        addWordToVocab(focus_word, context_sentence);
    }, false, 36.0f);
    add_btn->setMarginTop(8);
    comp->addView(add_btn);

    if (!clip_jump_query_.empty()) {
        auto* clip_box = ui::makeCard("desktop/soft", 12.0f, 8.0f);
        clip_box->setMarginTop(12);
        clip_box->addView(ui::makeLabel(
            "NAS 台词匹配: \"" + clip_jump_query_ + "\" (" +
                std::to_string(clip_jump_results_.size()) + " 条)",
            15.0f, "brls/text"));
        if (clip_jump_results_.empty()) {
            clip_box->addView(ui::makeLabel("未在已索引字幕中找到匹配对白。", 13.5f, "font/grey"));
        } else {
            for (const auto& clip : clip_jump_results_) {
                if (!clip.is_object()) continue;
                const std::string series = clip.value("series", clip.value("title", "NAS 视频"));
                const std::string ep = clip.value("episode", "");
                const double start_sec = clip.value("start_sec", 0.0);
                const double end_sec = clip.value("end_sec", start_sec + 4.0);
                const std::string line_en = clip.value("line_en", "");
                const std::string line_zh = clip.value("line_zh", "");
                const std::string play_url = clip.value("play_url", "");

                auto* c_row = ui::makeCard("desktop/surface", 10.0f, 8.0f);
                c_row->setMarginTop(8);
                c_row->addView(ui::makeLabel(
                    series + (ep.empty() ? "" : (" " + ep)) + " · " + formatClipSeconds(start_sec),
                    13.5f, "domain/media"));
                if (!line_en.empty()) {
                    c_row->addView(ui::makeLabel(line_en, 13.5f, "brls/text", 1.35f));
                }
                if (!line_zh.empty()) {
                    c_row->addView(ui::makeLabel(line_zh, 13.0f, "font/grey", 1.35f));
                }
                if (!play_url.empty()) {
                    auto* open_clip = ui::makeButton("▶ 跳播此原片片段", [series, ep, play_url, start_sec, end_sec]() {
                        if (!HostKernel::instance().isInitialized()) return;
                        HostKernel::instance().bus().publish("entity.media.open", {
                            {"item_id", "clip:" + series + ":" + std::to_string(static_cast<int>(start_sec))},
                            {"source_id", "plugin-fnos-nas"},
                            {"title", series + (ep.empty() ? "" : (" " + ep))},
                            {"kind", "nas"},
                            {"source_badge", "飞牛 NAS · 台词片段"},
                            {"play_url", play_url},
                            {"start_sec", start_sec},
                            {"end_sec", end_sec},
                        });
                    }, true, 30.0f);
                    open_clip->setMarginTop(6);
                    c_row->addView(open_clip);
                }
                clip_box->addView(c_row);
            }
        }
        comp->addView(clip_box);
    }

    split->addView(comp);
    stage_box_->addView(split);
}

void BookWorkbench::triggerClipJump(const std::string& word) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("media.clip_jump.v1")) {
        brls::Application::notify("未装载 media.clip_jump.v1 · 安装 plugin-fnos-nas 后可直达 NAS 本地字幕视频片段");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    action_busy_ = true;
    status_label_->setText("正在检索 NAS 美剧台词片段: " + word + "...");
    loadInBackground(kernel.jobs(), "台词片段检索: " + word, action_request_,
        [word, book_title = active_book_.title](const auto&) {
            return HostKernel::instance().capabilities().call("media.clip_jump.v1", {
                {"query", word},
                {"book", book_title},
                {"limit", 5},
                {"auto_open_player", false},
            });
        },
        [this, alive = alive_, word](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            action_busy_ = false;
            clip_jump_query_ = word;
            clip_jump_results_.clear();
            if (res.ok) {
                if (res.data.contains("clips") && res.data["clips"].is_array()) {
                    for (const auto& c : res.data["clips"]) {
                        clip_jump_results_.push_back(c);
                    }
                }
                const std::string summary = res.data.value("summary", "");
                status_label_->setText(summary.empty() ? "台词片段检索完成" : summary);
                brls::Application::notify(
                    clip_jump_results_.empty()
                        ? ("未找到 \"" + word + "\" 的影视台词片段")
                        : ("匹配到 " + std::to_string(clip_jump_results_.size()) + " 条影视台词，可点击跳播"));
            } else {
                status_label_->setText("台词检索失败: " + res.error);
                brls::Application::notify("台词检索失败: " + res.error);
            }
            rebuildContent();
        });
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
        struct RatingOption {
            const char* label;
            int quality;
            int days;
        };
        const RatingOption opts[] = {
            {"生疏 (1天)", 1, 1},
            {"困难 (3天)", 3, 3},
            {"掌握 (7天)", 4, 7},
            {"秒记 (14天)", 5, 14},
        };
        for (const auto& opt : opts) {
            auto* b = ui::makeButton(opt.label, [this, q = opt.quality, days = opt.days]() {
                brls::sync([this, q, days]() {
                    reviewCurrentVocabCard(q, days);
                });
            }, opt.days == 7, 36.0f);
            b->setMarginRight(8);
            rating->addView(b);
        }
        deck->addView(rating);
    }

    stage_box_->addView(deck);
}

void BookWorkbench::refineCurrentPdfPage() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    const bool has_pdf_cap = kernel.capabilities().has("book.bilingual_pdf.v1");
    const bool has_tr_cap = kernel.capabilities().has("llm.translate.v1");
    const bool has_chat_cap = kernel.capabilities().has("llm.chat.v1");
    if (!has_pdf_cap && !has_tr_cap && !has_chat_cap) {
        brls::Application::notify("未装载 book.bilingual_pdf.v1 或 llm 翻译插件，请先在插件市场安装");
        return;
    }

    action_request_->cancel();
    action_request_ = CancelToken::create();
    action_busy_ = true;
    const BookItem target_book = active_book_;
    const BookSpread current_spread = active_spread_;
    const int target_page = std::max(1, current_spread.page_number);
    status_label_->setText("正在精校第 " + std::to_string(target_page) + " 页...");
    rebuildContent();

    if (has_pdf_cap) {
        const auto sources = kernel.domains().bookSources();
        loadInBackground(kernel.jobs(), "BabelDOC 精校第 " + std::to_string(target_page) + " 页", action_request_,
            [target_book, target_page, sources](const auto& cancel) {
                nlohmann::json req = {
                    {"action", "translate_page"},
                    {"book_id", target_book.id},
                    {"id", target_book.id},
                    {"source", target_book.file_path},
                    {"page", target_page},
                    {"page_range", std::to_string(target_page)},
                };
                CapabilityResult res = HostKernel::instance().capabilities().callCancellable(
                    "book.bilingual_pdf.v1", req, cancel);
                BookSpread updated;
                if (res.ok) {
                    for (const auto& src : sources) {
                        if (cancel->isCancelled()) break;
                        if (!target_book.source_id.empty() && src->sourceId() != target_book.source_id) continue;
                        updated = src->fetchSpread(target_book.id, target_page);
                        if (!updated.book_id.empty()) break;
                    }
                    if (updated.book_id.empty()) {
                        auto sp_res = HostKernel::instance().capabilities().callCancellable(
                            "book.bilingual_pdf.v1",
                            {{"action", "fetch_spread"}, {"book_id", target_book.id}, {"page", target_page}},
                            cancel);
                        if (sp_res.ok && sp_res.data.contains("spread") && sp_res.data["spread"].is_object()) {
                            const auto& sj = sp_res.data["spread"];
                            updated.book_id = sj.value("book_id", target_book.id);
                            updated.chapter_title = sj.value("chapter_title", "");
                            updated.page_number = sj.value("page_number", target_page);
                            updated.total_pages = sj.value("total_pages", target_book.total_pages);
                            if (sj.contains("blocks") && sj["blocks"].is_array()) {
                                for (const auto& bj : sj["blocks"]) {
                                    if (!bj.is_object()) continue;
                                    BookPageBlock blk;
                                    blk.id = bj.value("id", "");
                                    blk.kind = bj.value("kind", "paragraph");
                                    blk.original = bj.value("original", "");
                                    blk.translated = bj.value("translated", "");
                                    if (bj.contains("highlighted_words") && bj["highlighted_words"].is_array()) {
                                        for (const auto& hw : bj["highlighted_words"]) {
                                            if (hw.is_string()) blk.highlighted_words.push_back(hw.template get<std::string>());
                                        }
                                    }
                                    updated.blocks.push_back(std::move(blk));
                                }
                            }
                        }
                    }
                }
                return std::pair<CapabilityResult, BookSpread>(std::move(res), std::move(updated));
            },
            [this, alive = alive_, target_book, target_page](auto result_pair) {
                if (!alive->load(std::memory_order_acquire)) return;
                action_busy_ = false;
                const auto& res = result_pair.first;
                auto& updated = result_pair.second;
                if (res.ok) {
                    if (!updated.book_id.empty() && active_book_.id == target_book.id) {
                        active_spread_ = std::move(updated);
                    }
                    status_label_->setText("BabelDOC 已完成第 " + std::to_string(target_page) + " 页精校对照");
                    brls::Application::notify("BabelDOC 第 " + std::to_string(target_page) + " 页精校完成");
                } else {
                    status_label_->setText("BabelDOC 精校失败: " + res.error);
                    brls::Application::notify("BabelDOC 精校失败: " + res.error);
                }
                rebuildContent();
            });
        return;
    }

    loadInBackground(kernel.jobs(), "LLM 精译第 " + std::to_string(target_page) + " 页", action_request_,
        [current_spread, has_tr_cap](const auto& cancel) {
            BookSpread out = current_spread;
            std::string last_err;
            bool any_ok = false;
            for (auto& blk : out.blocks) {
                if (cancel->isCancelled()) break;
                if (blk.original.empty()) continue;
                CapabilityResult r;
                if (has_tr_cap) {
                    r = HostKernel::instance().capabilities().callCancellable("llm.translate.v1", {
                        {"text", blk.original},
                        {"source_lang", "auto"},
                        {"target_lang", "zh-CN"},
                    }, cancel);
                } else {
                    r = HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                        {"domain", "book"},
                        {"prompt", "请将以下学术段落精准译为简体中文，保留术语一致性：\n\n" + blk.original},
                    }, cancel);
                }
                if (r.ok) {
                    std::string tr = r.data.value("translation", r.data.value("reply", ""));
                    tr = extractTranslationText(tr);
                    if (!tr.empty()) {
                        blk.translated = std::move(tr);
                        any_ok = true;
                    }
                } else {
                    last_err = r.error;
                }
            }
            return std::tuple<bool, std::string, BookSpread>(any_ok, std::move(last_err), std::move(out));
        },
        [this, alive = alive_, target_book, target_page](auto tuple_res) {
            if (!alive->load(std::memory_order_acquire)) return;
            action_busy_ = false;
            const bool ok = std::get<0>(tuple_res);
            const std::string& err = std::get<1>(tuple_res);
            auto& out = std::get<2>(tuple_res);
            if (ok && active_book_.id == target_book.id) {
                active_spread_ = std::move(out);
                status_label_->setText("已完成第 " + std::to_string(target_page) + " 页 AI 精译对照");
                brls::Application::notify("第 " + std::to_string(target_page) + " 页精译完成");
            } else {
                status_label_->setText("精译失败: " + (err.empty() ? "未返回有效译文" : err));
                brls::Application::notify("精译失败: " + (err.empty() ? "未返回有效译文" : err));
            }
            rebuildContent();
        });
}

void BookWorkbench::analyzeFocusWord(const std::string& word, const std::string& context_sentence) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    const bool has_agent = kernel.capabilities().has("ai.agent.v1");
    if (!has_chat && !has_agent) {
        brls::Application::notify("未装载 llm.chat.v1 / ai.agent.v1 插件，请先安装 plugin-magpie-agent");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    action_busy_ = true;
    const std::string key = active_book_.id + "|" + toLowerCopy(word);
    const std::string title = active_book_.title;
    status_label_->setText("正在解析词条 \"" + word + "\" 的词根与语境句法...");
    rebuildContent();

    loadInBackground(kernel.jobs(), "词根与句法拆解: " + word, action_request_,
        [word, context_sentence, title, has_chat](const auto& cancel) {
            std::ostringstream prompt;
            prompt << "请结合以下英文书籍语境，简洁解析单词 \"" << word << "\"：\n"
                   << "1. 音标与语境释义\n"
                   << "2. 词根词缀拆解与记忆线索\n"
                   << "3. 在原句中的语法功能与搭配\n\n"
                   << "书名: " << title << "\n"
                   << "原句: " << context_sentence;
            if (has_chat) {
                return HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                    {"domain", "book"},
                    {"context", title},
                    {"prompt", prompt.str()},
                }, cancel);
            }
            return HostKernel::instance().capabilities().callCancellable("ai.agent.v1", {
                {"action", "chat"},
                {"domain", "book"},
                {"context", title},
                {"prompt", prompt.str()},
            }, cancel);
        },
        [this, alive = alive_, key, word](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            action_busy_ = false;
            if (res.ok) {
                const std::string reply = res.data.value("reply", res.data.value("text", ""));
                if (!reply.empty()) {
                    word_analysis_cache_[key] = reply;
                    status_label_->setText("已完成词条 \"" + word + "\" 的语境词根解析");
                } else {
                    status_label_->setText("AI 未返回词根解析内容");
                }
            } else {
                status_label_->setText("词根解析失败: " + res.error);
                brls::Application::notify("词根解析失败: " + res.error);
            }
            rebuildContent();
        });
}

void BookWorkbench::addWordToVocab(const std::string& word, const std::string& context_sentence) {
    if (word.empty()) return;
    const std::string lower_w = toLowerCopy(word);
    const std::string analysis_key = active_book_.id + "|" + lower_w;
    std::string cached_ety;
    if (auto it = word_analysis_cache_.find(analysis_key); it != word_analysis_cache_.end()) {
        cached_ety = it->second;
    }

    bool exists = false;
    for (auto& vc : vocab_cards_) {
        if (toLowerCopy(vc.word) == lower_w) {
            if (vc.context_sentence.empty() && !context_sentence.empty()) {
                vc.context_sentence = context_sentence;
            }
            if (vc.etymology.empty() && !cached_ety.empty()) {
                vc.etymology = cached_ety;
            }
            exists = true;
            break;
        }
    }
    if (!exists) {
        VocabCard card;
        card.word = word;
        card.lemma = lower_w;
        card.etymology = cached_ety.empty() ? "待 AI 拆解词根词源" : cached_ety;
        card.meaning = "源自《" + active_book_.title + "》";
        card.context_sentence = context_sentence;
        card.source_book = active_book_.title;
        vocab_cards_.push_back(std::move(card));
    }
    saveHostVocabFallback();

    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        k.bus().publish("entity.vocab.add", {
            {"word", word},
            {"book", active_book_.title},
            {"context_sentence", context_sentence},
        });
        if (k.capabilities().has("vocab.sm2.v1")) {
            loadInBackground(k.jobs(), "同步生词至 SM-2 插件: " + word, action_request_,
                [word, title = active_book_.title, context_sentence, cached_ety](const auto&) {
                    return HostKernel::instance().capabilities().call("vocab.sm2.v1", {
                        {"action", "add"},
                        {"word", word},
                        {"source_book", title},
                        {"context_sentence", context_sentence},
                        {"etymology", cached_ety},
                    });
                },
                [alive = alive_](CapabilityResult) {});
        }
    }

    brls::Application::notify(exists ? ("生词已在复习队列中: " + word) : ("已添加至 SM-2 生词本: " + word));
    finishRefresh();
}

void BookWorkbench::reviewCurrentVocabCard(int quality, int selected_days) {
    if (vocab_cards_.empty()) return;
    VocabCard& card = vocab_cards_[vocab_idx_ % vocab_cards_.size()];

    if (quality < 3) {
        card.repetitions = 0;
        card.interval_days = 1;
    } else {
        card.repetitions += 1;
        card.interval_days = selected_days;
    }
    const double qd = static_cast<double>(quality);
    const double new_ef = card.ease_factor + (0.1 - (5.0 - qd) * (0.08 + (5.0 - qd) * 0.02));
    card.ease_factor = std::max(1.3, std::round(new_ef * 100.0) / 100.0);

    const std::string reviewed_word = card.word;
    const int final_interval = card.interval_days;
    const int final_reps = card.repetitions;
    const double final_ef = card.ease_factor;

    saveHostVocabFallback();

    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        if (k.capabilities().has("vocab.sm2.v1")) {
            loadInBackground(k.jobs(), "提交 SM-2 复习记录", action_request_,
                [reviewed_word, quality, final_interval, final_reps, final_ef](const auto&) {
                    return HostKernel::instance().capabilities().call("vocab.sm2.v1", {
                        {"action", "review"},
                        {"word", reviewed_word},
                        {"quality", quality},
                        {"interval_days", final_interval},
                        {"repetitions", final_reps},
                        {"ease_factor", final_ef},
                    });
                },
                [alive = alive_](CapabilityResult) {});
        }
    }

    vocab_idx_ = (vocab_idx_ + 1) % vocab_cards_.size();
    vocab_revealed_ = false;
    rebuildContent();
}

void BookWorkbench::mergeHostVocabFallback() {
    auto& k = HostKernel::instance();
    if (!k.isInitialized()) return;
    const nlohmann::json saved = k.config().loadPluginConfig("host-book-vocab");
    if (!saved.is_object() || !saved.contains("cards") || !saved["cards"].is_array()) {
        return;
    }
    for (const auto& j : saved["cards"]) {
        if (!j.is_object()) continue;
        const std::string w = j.value("word", "");
        if (w.empty()) continue;
        const std::string lw = toLowerCopy(w);
        bool updated = false;
        for (auto& existing : vocab_cards_) {
            if (toLowerCopy(existing.word) == lw) {
                existing.interval_days = j.value("interval_days", existing.interval_days);
                existing.repetitions = j.value("repetitions", existing.repetitions);
                existing.ease_factor = j.value("ease_factor", existing.ease_factor);
                updated = true;
                break;
            }
        }
        if (!updated) {
            VocabCard c;
            c.word = w;
            c.lemma = j.value("lemma", lw);
            c.phonetic = j.value("phonetic", "");
            c.etymology = j.value("etymology", "");
            c.meaning = j.value("meaning", "");
            c.context_sentence = j.value("context_sentence", "");
            c.source_book = j.value("source_book", "");
            c.interval_days = j.value("interval_days", 1);
            c.repetitions = j.value("repetitions", 0);
            c.ease_factor = j.value("ease_factor", 2.5);
            vocab_cards_.push_back(std::move(c));
        }
    }
}

void BookWorkbench::saveHostVocabFallback() const {
    auto& k = HostKernel::instance();
    if (!k.isInitialized()) return;
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& c : vocab_cards_) {
        arr.push_back({
            {"word", c.word},
            {"lemma", c.lemma},
            {"phonetic", c.phonetic},
            {"etymology", c.etymology},
            {"meaning", c.meaning},
            {"context_sentence", c.context_sentence},
            {"source_book", c.source_book},
            {"interval_days", c.interval_days},
            {"repetitions", c.repetitions},
            {"ease_factor", c.ease_factor},
        });
    }
    k.config().savePluginConfig("host-book-vocab", {{"cards", std::move(arr)}});
}

void BookWorkbench::openExternalBook(const nlohmann::json& payload) {
    if (!payload.is_object()) return;
    const std::string bid = payload.value("book_id", payload.value("id", ""));
    const std::string file_path = payload.value("file_path", payload.value("source", ""));
    const std::string title = payload.value("title", "");
    const std::string fmt = toLowerCopy(payload.value("format", ""));
    const int page = std::max(1, payload.value("page", 1));

    BookItem matched;
    bool found = false;
    for (const auto& b : books_) {
        if ((!bid.empty() && b.id == bid) ||
            (!file_path.empty() && b.file_path == file_path)) {
            matched = b;
            found = true;
            break;
        }
    }
    if (!found) {
        matched.id = !bid.empty() ? bid : ("ext-book:" + file_path);
        matched.file_path = file_path;
        matched.format = !fmt.empty() ? fmt
                                      : (toLowerCopy(std::filesystem::path(file_path).extension().string()) == ".pdf"
                                             ? "pdf"
                                             : "epub");
        matched.title = !title.empty()
                            ? title
                            : (file_path.empty() ? "外部电子书" : std::filesystem::path(file_path).filename().string());
        matched.current_page = page;
        matched.total_pages = std::max(page, payload.value("total_pages", 1));
    } else {
        matched.current_page = page;
    }

    auto& kernel = HostKernel::instance();
    if (!found && !file_path.empty() && matched.format == "pdf" &&
        kernel.isInitialized() && kernel.capabilities().has("book.bilingual_pdf.v1")) {
        action_request_->cancel();
        action_request_ = CancelToken::create();
        loadInBackground(kernel.jobs(), "导入 PDF 到书架", action_request_,
            [file_path, title, page](const auto& cancel) {
                auto res = HostKernel::instance().capabilities().callCancellable("book.bilingual_pdf.v1", {
                    {"action", "import_pdf"},
                    {"source", file_path},
                    {"title", title},
                }, cancel);
                nlohmann::json sp_data;
                std::string resolved_id = res.data.value("book_id", "");
                if (res.ok && !resolved_id.empty()) {
                    auto sp = HostKernel::instance().capabilities().callCancellable("book.bilingual_pdf.v1", {
                        {"action", "fetch_spread"},
                        {"book_id", resolved_id},
                        {"page", page},
                    }, cancel);
                    if (sp.ok) sp_data = std::move(sp.data);
                }
                return std::pair<CapabilityResult, nlohmann::json>(std::move(res), std::move(sp_data));
            },
            [this, alive = alive_, matched, page](auto rp) mutable {
                if (!alive->load(std::memory_order_acquire)) return;
                const auto& res = rp.first;
                const auto& sp_data = rp.second;
                if (res.ok && !res.data.value("book_id", std::string{}).empty()) {
                    matched.id = res.data.value("book_id", matched.id);
                    matched.source_id = "plugin-pdf-babeldoc";
                    matched.total_pages = std::max(matched.total_pages, res.data.value("total_pages", matched.total_pages));
                }
                active_book_ = matched;
                has_active_book_ = true;
                sub_mode_ = SubMode::PdfBabelDoc;
                if (sp_data.contains("spread") && sp_data["spread"].is_object()) {
                    const auto& sj = sp_data["spread"];
                    BookSpread sp;
                    sp.book_id = sj.value("book_id", matched.id);
                    sp.chapter_title = sj.value("chapter_title", "");
                    sp.page_number = sj.value("page_number", page);
                    sp.total_pages = sj.value("total_pages", matched.total_pages);
                    if (sj.contains("blocks") && sj["blocks"].is_array()) {
                        for (const auto& bj : sj["blocks"]) {
                            if (!bj.is_object()) continue;
                            BookPageBlock blk;
                            blk.id = bj.value("id", "");
                            blk.kind = bj.value("kind", "paragraph");
                            blk.original = bj.value("original", "");
                            blk.translated = bj.value("translated", "");
                            sp.blocks.push_back(std::move(blk));
                        }
                    }
                    active_spread_ = std::move(sp);
                } else {
                    loadSpread(page);
                }
                if (on_entity_focus_) on_entity_focus_(active_book_.title);
                finishRefresh();
            });
        return;
    }

    active_book_ = matched;
    has_active_book_ = true;
    sub_mode_ = (matched.format == "pdf") ? SubMode::PdfBabelDoc : SubMode::EpubCompanion;
    if (on_entity_focus_) {
        on_entity_focus_(matched.title);
    }
    loadSpread(page);
    finishRefresh();
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
