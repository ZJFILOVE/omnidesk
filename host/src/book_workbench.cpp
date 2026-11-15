#include "omnidesk/host/book_workbench.hpp"
#include "omnidesk/host/async_load.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <sstream>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/selection_action_popover.hpp"
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
    if (std::isnan(sec) || std::isinf(sec) || sec <= 0.0) {
        return "00:00";
    }
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
        {"book-epub-1", "plugin-ebook", "The Hitchhiker's Guide to the Galaxy", "Douglas Adams",
         "epub", "/nas/books/h2g2.epub", "", 68, 224, 30.3, true, 34, true},
        {"book-azw3-1", "plugin-ebook", "Dune (Annotated Bilingual Companion)", "Frank Herbert",
         "azw3", "/nas/books/dune.azw3", "", 112, 688, 16.2, true, 41, false},
    };
}

BookSpread buildShowcaseSpread(const BookItem& book, int page_num) {
    BookSpread s;
    s.book_id = book.id;
    s.page_number = std::max(1, page_num);
    s.total_pages = book.total_pages;
    s.chapter_title = (book.format == "pdf") ? "BabelDOC 版面保真对照页" : "电子书伴读双语精析页";
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

bool focusIsInsideBookView(brls::View* ancestor) {
    if (!ancestor) return false;
    brls::View* focus = brls::Application::getCurrentFocus();
    while (focus) {
        if (focus == ancestor) return true;
        focus = focus->getParent();
    }
    return false;
}

float estimateBookStageWidth(const brls::View* self) {
    const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    float avail = cw - 260.0f - 48.0f;
    auto* v = const_cast<brls::View*>(self);
    if (v && v->getParent() && v->getParent()->getParent()) {
        if (auto* split = dynamic_cast<brls::Box*>(v->getParent()->getParent()->getParent())) {
            if (split->getAxis() == brls::Axis::ROW) {
                for (auto* ch : split->getChildren()) {
                    if (ch != v->getParent()->getParent() &&
                        ch->getVisibility() == brls::Visibility::VISIBLE) {
                        avail -= 388.0f;
                    }
                }
            }
        }
    }
    return std::max(320.0f, avail);
}

int computeBookshelfColumns(float stage_w) {
    if (stage_w < 680.0f) return 2;
    if (stage_w < 960.0f) return 3;
    if (stage_w < 1420.0f) return 4;
    if (stage_w < 1950.0f) return 5;
    return 6;
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
    header->setId("omnidesk/pane_header");
    header->setShrink(0.0f);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(12);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->setShrink(1.0f);
    left->addView(ui::makeLabel("书本与 PDF 阅读 · Book Workbench", 24.0f, "brls/text"));
    status_label_ = ui::makeLabel("2:3 立体书架 · BabelDOC PDF 双栏对照 · EPUB 词根伴读与影视切片跳转 · SM-2 生词本",
                                   14.5f, "font/grey");
    status_label_->setMarginTop(3);
    left->addView(status_label_);
    header->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    right->setShrink(0.0f);
    date_filter_btn_ = ui::makeButton("📅 日历查找", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptDateFilter();
    }, false, 36.0f);
    date_filter_btn_->setMarginRight(8);
    right->addView(date_filter_btn_);
    auto* s_btn = ui::makeButton("检索书库 /", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptSearch();
    }, false, 36.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    right->addView(ui::makeButton("阅读插件管理", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) on_open_marketplace_();
    }, false, 36.0f));
    header->addView(right);
    addView(header);

    sub_tabs_box_ = new brls::Box(brls::Axis::ROW);
    sub_tabs_box_->setId("omnidesk/pane_filters");
    sub_tabs_box_->setShrink(0.0f);
    sub_tabs_box_->setMarginBottom(14);
    addView(sub_tabs_box_);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setId("omnidesk/pane_primary");
    scroll->setGrow(1.0f);
    scroll->setWidthPercentage(100);
    stage_box_ = new brls::Box(brls::Axis::COLUMN);
    stage_box_->setWidthPercentage(100);
    scroll->setContentView(stage_box_);
    addView(scroll);

    resize_sub_ = brls::Application::getWindowSizeChangedEvent()->subscribe([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            onViewportLayoutChanged();
        }
    });

    refresh();

    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        download_sub_token_ = k.bus().subscribe("entity.book.downloaded", "host.book_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
        recommend_sub_token_ = k.bus().subscribe("entity.recommend.updated", "host.book_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
    }
}

BookWorkbench::~BookWorkbench() {
    brls::Application::getWindowSizeChangedEvent()->unsubscribe(resize_sub_);
    alive_->store(false, std::memory_order_release);
    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        if (download_sub_token_ != 0) k.bus().unsubscribe(download_sub_token_);
        if (recommend_sub_token_ != 0) k.bus().unsubscribe(recommend_sub_token_);
    }
    download_sub_token_ = 0;
    recommend_sub_token_ = 0;
    library_request_->cancel();
    spread_request_->cancel();
    action_request_->cancel();
}

void BookWorkbench::onViewportLayoutChanged() {
    if (getVisibility() != brls::Visibility::VISIBLE || (sub_mode_ != SubMode::Bookshelf && sub_mode_ != SubMode::DailyEnglish)) {
        return;
    }
    const int cols = computeBookshelfColumns(estimateBookStageWidth(this));
    if (cols != last_bookshelf_cols_) {
        rebuildContent();
    }
}

void BookWorkbench::refresh() {
    library_request_->cancel();
    library_request_ = CancelToken::create();
    auto& kernel = HostKernel::instance();
    books_.clear();
    preview_books_.clear();
    vocab_cards_.clear();

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().bookSources();
        if (!sources.empty()) {
            status_label_->setText("正在加载书库…");
            finishRefresh();
            loadUiInBackground(kernel.uiTasks(), "book.library", library_request_,
                [sources](const auto& cancel) {
                    std::pair<std::vector<BookItem>, std::vector<VocabCard>> library;
                    for (const auto& source : sources) {
                        if (cancel->isCancelled()) break;
                        auto books = source->listBooks("", "");
                        for (auto& book : books) {
                            if (book.source_id.empty()) book.source_id = source->sourceId();
                        }
                        library.first.insert(library.first.end(), books.begin(), books.end());
                        if (cancel->isCancelled()) break;
                        auto vocab = source->listVocab();
                        library.second.insert(library.second.end(), vocab.begin(), vocab.end());
                    }
                    return library;
                },
                [this, alive = alive_, sources](auto library) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    books_ = std::move(library.first);
                    vocab_cards_ = std::move(library.second);
                    mergeHostVocabFallback();
                    const bool has_library_source = std::any_of(sources.begin(), sources.end(), [](const auto& source) {
                        return source->sourceId() != "plugin-ai-curator";
                    });
                    if (!has_library_source) {
                        preview_books_ = buildShowcaseBooks();
                        books_.insert(books_.end(), preview_books_.begin(), preview_books_.end());
                    }
                    status_label_->setText("已接入 " + std::to_string(sources.size()) + " 个实时 BookSource 插件" +
                        (has_library_source ? "" : " · 普通书架为布局预览"));
                    finishRefresh();
                });
            return;
        }
    }

    {
        preview_books_ = buildShowcaseBooks();
        books_ = preview_books_;
        vocab_cards_ = buildShowcaseVocab();
        mergeHostVocabFallback();
        status_label_->setText("深度阅读布局预览（安装 plugin-pdf-babeldoc / plugin-ebook 后无缝接管书架与生词库）");
    }

    finishRefresh();
}

void BookWorkbench::finishRefresh() {
    english_latest_date_.clear();
    for (const auto& book : books_) {
        // Curator exposes business dates through these stable IDs (BookItem has no extra metadata).
        if (book.source_id == "plugin-ai-curator" && book.id.rfind("english-date-", 0) == 0 && book.id.size() == 23) {
            english_latest_date_ = std::max(english_latest_date_, book.id.substr(13));
        }
    }
    if (date_filter_btn_) {
        date_filter_btn_->setVisibility(sub_mode_ == SubMode::DailyEnglish ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        date_filter_btn_->setText(english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : "英语日历");
    }
    sub_tabs_box_->clearViews();
    struct Tab {
        SubMode m;
        const char* label;
    };
    const Tab tabs[] = {
        {SubMode::Bookshelf, "2:3 书架"},
        {SubMode::DailyEnglish, "每日英语"},
        {SubMode::PdfBabelDoc, "PDF / BabelDOC 双栏对照"},
        {SubMode::EpubCompanion, "电子书伴读 & NAS 片段跳转"},
        {SubMode::VocabReview, "SM-2 生词记忆"},
    };
    for (const auto& t : tabs) {
        std::string txt = (t.m == SubMode::VocabReview)
                              ? ("SM-2 生词记忆 (" + std::to_string(vocab_cards_.size()) + ")")
                              : std::string(t.label);
        auto* btn = ui::makeButton(txt, [this, alive = alive_, m = t.m]() {
            brls::sync([this, alive, m]() {
                if (alive->load(std::memory_order_acquire)) setSubMode(m);
            });
        }, sub_mode_ == t.m, 34.0f);
        btn->setMarginRight(8);
        sub_tabs_box_->addView(btn);
    }

    rebuildContent();
}

void BookWorkbench::setSubMode(SubMode m) {
    if (sub_mode_ == m) return;
    sub_mode_ = m;
    finishRefresh();
}

void BookWorkbench::rebuildContent() {
    const bool had_focus = focusIsInsideBookView(stage_box_);
    if (had_focus) {
        brls::Application::giveFocus(nullptr);
    }
    stage_box_->clearViews();
if ((sub_mode_ == SubMode::PdfBabelDoc || sub_mode_ == SubMode::EpubCompanion) &&
    (!has_active_book_ || active_spread_.blocks.empty())) {
    stage_box_->addView(ui::makeLabel(has_active_book_ ? "书页尚未加载，可重试或返回书架选择其他内容。" : "请先从书架、每日英语或搜索结果打开阅读内容。", 16.0f, "font/grey"));
    if (has_active_book_) {
        stage_box_->addView(ui::makeButton("重试加载书页", [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            loadSpread(active_spread_.page_number);
            rebuildContent();
        }, false, 36.0f));
    }
    if (had_focus) {
        if (auto* df = stage_box_->getDefaultFocus()) brls::Application::giveFocus(df);
    }
    return;
}
switch (sub_mode_) {
        case SubMode::Bookshelf: renderBookshelf(); break;
        case SubMode::DailyEnglish: renderBookshelf(); break;
        case SubMode::PdfBabelDoc: renderPdfBabelDoc(); break;
        case SubMode::EpubCompanion: renderEpubCompanion(); break;
        case SubMode::VocabReview: renderVocabReview(); break;
    }
    if (had_focus) {
        if (auto* df = stage_box_->getDefaultFocus()) {
            brls::Application::giveFocus(df);
        }
    }
}

void BookWorkbench::renderBookshelf() {
    const bool daily = sub_mode_ == SubMode::DailyEnglish;
    if (daily) {
        auto* dates = new brls::Box(brls::Axis::ROW);
        dates->setMarginBottom(12);
        auto* latest = ui::makeButton("最新一期" + (english_latest_date_.empty() ? std::string{} : " · " + english_latest_date_), [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            english_date_mode_ = EnglishDateMode::Latest;
            finishRefresh();
        }, english_date_mode_ == EnglishDateMode::Latest, 36.0f);
        latest->setMarginRight(8);
        dates->addView(latest);
        auto* selected = ui::makeButton(english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : "选择日期", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) promptDateFilter();
        }, english_date_mode_ == EnglishDateMode::Selected, 36.0f);
        selected->setMarginRight(8);
        dates->addView(selected);
        dates->addView(ui::makeButton("全部归档", [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            english_date_mode_ = EnglishDateMode::All;
            finishRefresh();
        }, english_date_mode_ == EnglishDateMode::All, 36.0f));
        stage_box_->addView(dates);
    }
    if (!daily) {
        auto* filter_bar = new brls::Box(brls::Axis::ROW);
        filter_bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        filter_bar->setAlignItems(brls::AlignItems::CENTER);
        filter_bar->setMarginBottom(12);

        auto* chips = new brls::Box(brls::Axis::ROW);
        const struct { const char* fmt; const char* label; } filters[] = {
            {"", "全部"},
            {"epub", "EPUB"},
            {"mobi", "MOBI"},
            {"azw3", "AZW3"},
            {"fb2", "FB2"},
            {"txt", "TXT"},
            {"pdf", "PDF"},
        };
        for (const auto& f : filters) {
            auto* btn = ui::makeButton(f.label, [this, alive = alive_, fmt = std::string(f.fmt)]() {
                brls::sync([this, alive, fmt]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    format_filter_ = fmt;
                    rebuildContent();
                });
            }, format_filter_ == f.fmt, 32.0f);
            btn->setMarginRight(6);
            chips->addView(btn);
        }
        filter_bar->addView(chips);
        filter_bar->addView(ui::makeButton("+ 导入本地电子书", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) promptImportLocalBook();
        }, false, 32.0f));
        stage_box_->addView(filter_bar);

    }

    const float stage_w = estimateBookStageWidth(this);
    const int cols = computeBookshelfColumns(stage_w);
    last_bookshelf_cols_ = cols;
    constexpr float kGap = 14.0f;

    auto* grid = new brls::Box(brls::Axis::COLUMN);
    grid->setWidthPercentage(100);
    brls::Box* row = nullptr;
    int idx = 0;
    const std::string q_low = toLowerCopy(daily ? english_search_query_ : search_query_);
    const std::string date = english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : english_latest_date_;
    for (const auto& book : books_) {
        const bool curator = book.source_id == "plugin-ai-curator";
        if (curator != daily) continue;
        if (daily && english_date_mode_ != EnglishDateMode::All &&
            (date.empty() || book.id != "english-date-" + date)) continue;
        if (!daily && !format_filter_.empty() && toLowerCopy(book.format) != format_filter_) continue;
        if (!q_low.empty() &&
            toLowerCopy(book.title).find(q_low) == std::string::npos &&
            toLowerCopy(book.author).find(q_low) == std::string::npos &&
            toLowerCopy(book.format).find(q_low) == std::string::npos) continue;
        if (idx % cols == 0) {
            row = new brls::Box(brls::Axis::ROW);
            row->setWidthPercentage(100);
            row->setMarginBottom(14);
            grid->addView(row);
        }

        auto* card = ui::makeCard("desktop/surface", 14.0f, 12.0f);
        card->setWidth(0);
        card->setGrow(1.0f);
        card->setShrink(1.0f);
        if (idx % cols != cols - 1) card->setMarginRight(kGap);
        card->setFocusable(true);

        // Upright 2:3 Book Cover
        auto* cover = ui::makeCard("desktop/soft", 14.0f, 8.0f);
        cover->setHeight(224);
        cover->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        cover->setMarginBottom(10);

        auto* top_pills = new brls::Box(brls::Axis::ROW);
        top_pills->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        std::string fmt = book.format;
        for (char& ch : fmt) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
        if (!fmt.empty()) {
            top_pills->addView(ui::makePill(fmt, "status/ok_bg", "domain/book"));
        }
        if (book.nas_synced) {
            top_pills->addView(ui::makePill("NAS", "desktop/surface", "font/grey"));
        }
        cover->addView(top_pills);

        cover->addView(ui::makeLabel(book.title, 16.5f, "brls/text", 1.38f));

        std::string prog = "P." + std::to_string(book.current_page) + "/" +
                           std::to_string(book.total_pages) + " · 生词 " + std::to_string(book.vocab_count);
        cover->addView(ui::makeLabel(prog, 13.5f, "font/grey"));
        card->addView(cover);

        card->addView(ui::makeLabel(book.author, 15.0f, "font/grey"));
        auto* pct = ui::makeLabel("阅读进度: " + std::to_string(static_cast<int>(book.progress_percent)) + "%",
                                  14.0f, "domain/book");
        pct->setMarginTop(4);
        card->addView(pct);

        card->registerClickAction([this, alive = alive_, book](brls::View*) {
            brls::sync([this, alive, book]() {
                if (alive->load(std::memory_order_acquire)) openBook(book);
            });
            return true;
        });
        row->addView(card);
        ++idx;
    }

    if (row && idx > 0 && (idx % cols) != 0) {
        for (int s = idx % cols; s < cols; ++s) {
            auto* spacer = new brls::Box(brls::Axis::COLUMN);
            spacer->setWidth(0);
            spacer->setGrow(1.0f);
            spacer->setShrink(1.0f);
            spacer->setFocusable(false);
            if (s != cols - 1) spacer->setMarginRight(kGap);
            row->addView(spacer);
        }
    }

if (idx == 0) {
    stage_box_->addView(ui::makeLabel(daily ? "此日期暂无英语课程，可选择其他日期或查看全部归档。" : "没有匹配的书籍，可调整检索条件或导入本地文件。", 15.0f, "font/grey"));
}
stage_box_->addView(grid);
}

void BookWorkbench::openBook(const BookItem& book) {
    active_book_ = book;
    has_active_book_ = true;
    clip_jump_results_.clear();
    clip_jump_query_.clear();
    selected_word_idx_ = 0;
    custom_focus_word_.clear();
    loadSpread(book.current_page);
    sub_mode_ = (book.format == "pdf") ? SubMode::PdfBabelDoc : SubMode::EpubCompanion;
    if (on_entity_focus_) {
        on_entity_focus_(book.title + " · P." + std::to_string(active_spread_.page_number) +
                         "/" + std::to_string(std::max(active_spread_.total_pages, book.total_pages)));
    }
    if (HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().publish("entity.book.open", {
                                                                     {"id", book.id},
                                                                     {"title", book.title},
                                                                     {"page", book.current_page},
                                                                     {"origin", "host_workbench"},
                                                                 });
        HostKernel::instance().bus().publish("host.focus_changed", {
                                                                       {"domain", "book"},
                                                                       {"id", book.id},
                                                                       {"title", book.title},
                                                                       {"subtitle", book.author},
                                                                       {"author", book.author},
                                                                       {"format", book.format},
                                                                       {"source_id", book.source_id},
                                                                       {"page_number", book.current_page},
                                                                       {"total_pages", book.total_pages},
                                                                       {"play_url", book.file_path},
                                                                       {"extra", nlohmann::json{
                                                                           {"vocab_count", book.vocab_count},
                                                                           {"progress_percent", book.progress_percent},
                                                                       }},
                                                                   });
    }
    finishRefresh();
}

void BookWorkbench::loadSpread(int page_num) {
    spread_request_->cancel();
    spread_request_ = CancelToken::create();
    active_spread_ = {};
    active_spread_.page_number = std::max(1, page_num);
    active_spread_.total_pages = std::max(1, active_book_.total_pages);
    const bool preview = std::any_of(preview_books_.begin(), preview_books_.end(), [this](const auto& book) {
        return book.id == active_book_.id && book.source_id == active_book_.source_id;
    });
    if (preview) {
        active_spread_ = buildShowcaseSpread(active_book_, page_num);
        active_book_.current_page = active_spread_.page_number;
        status_label_->setText("布局预览 · 示例内容");
        return;
    }
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().bookSources();
        std::shared_ptr<IBookSource> matched_src;
        for (const auto& src : sources) {
            if (!active_book_.source_id.empty() && src->sourceId() == active_book_.source_id) {
                matched_src = src;
                break;
            }
        }
        if (!matched_src && active_book_.source_id.empty()) {
            const std::string preferred = (active_book_.format == "pdf") ? "plugin-pdf-babeldoc" : "plugin-ebook";
            for (const auto& src : sources) {
                if (src->sourceId() == preferred) {
                    matched_src = src;
                    break;
                }
            }
        }
        if (matched_src) {
            status_label_->setText("正在加载书页…");
            loadUiInBackground(kernel.uiTasks(), "book.spread", spread_request_,
                [src = matched_src, book_id = active_book_.id, page_num](const auto&) {
                    return src->fetchSpread(book_id, page_num);
                },
                [this, alive = alive_](BookSpread spread) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    status_label_->setText(spread.book_id.empty() || spread.blocks.empty() ? "书页加载失败，可重试" : "书页已加载");
                    if (!spread.book_id.empty() && !spread.blocks.empty()) {
                        active_book_.current_page = spread.page_number;
                        active_book_.total_pages = std::max(active_book_.total_pages, spread.total_pages);
                        active_spread_ = std::move(spread);
                        if (HostKernel::instance().isInitialized()) {
                            HostKernel::instance().bus().publish("host.focus_changed", {
                                {"domain", "book"},
                                {"id", active_book_.id},
                                {"title", active_book_.title},
                                {"subtitle", active_book_.author},
                                {"author", active_book_.author},
                                {"format", active_book_.format},
                                {"source_id", active_book_.source_id},
                                {"page_number", active_book_.current_page},
                                {"total_pages", active_book_.total_pages},
                                {"play_url", active_book_.file_path},
                                {"extra", nlohmann::json{
                                    {"chapter_title", active_spread_.chapter_title},
                                    {"vocab_count", active_book_.vocab_count},
                                }},
                            });
                        }
                    }
                    rebuildContent();
                });
            return;
        }
    }
    status_label_->setText("无法加载书页：对应阅读来源未连接");
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
    auto* prev = ui::makeButton("上一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (active_spread_.page_number > 1) loadSpread(active_spread_.page_number - 1);
            rebuildContent();
        });
    }, false, 34.0f);
    prev->setMarginRight(8);
    nav->addView(prev);

    auto* next = ui::makeButton("下一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (active_spread_.page_number < std::max(1, active_spread_.total_pages)) {
                loadSpread(active_spread_.page_number + 1);
                rebuildContent();
            }
        });
    }, false, 34.0f);
    next->setMarginRight(8);
    nav->addView(next);

    nav->addView(ui::makeButton(
        action_busy_ ? "精校中..." : "BabelDOC 精校此页",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) refineCurrentPdfPage();
        },
        true, 34.0f));
    bar->addView(nav);
    toolbar->addView(bar);
    stage_box_->addView(toolbar);

    // Side-by-side 50/50 PDF Original vs BabelDOC Chinese Layout
    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    auto* orig_col = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    orig_col->setWidthPercentage(49);
    orig_col->setMarginRight(12);
    orig_col->addView(ui::makePill("Original PDF Layout Block", "desktop/soft", "font/grey"));
    for (const auto& blk : active_spread_.blocks) {
        auto* p_box = new brls::Box(brls::Axis::COLUMN);
        p_box->setWidthPercentage(100);
        p_box->setPadding(6, 10, 6, 10);
        p_box->setCornerRadius(6.0f);
        p_box->setMarginTop(8);
        if (!blk.original.empty()) {
            p_box->setFocusable(true);
            p_box->setHighlightCornerRadius(6.0f);
            p_box->registerClickAction([this, alive = alive_, text = blk.original](brls::View*) {
                if (!alive->load(std::memory_order_acquire)) return true;
                auto& k = HostKernel::instance();
SelectionContext sctx;
sctx.context_sentence = text;
sctx.source_title = active_book_.title + " · P." +
                    std::to_string(active_spread_.page_number) + "/" +
                    std::to_string(std::max(active_spread_.total_pages, active_book_.total_pages));
                sctx.source_id = active_book_.source_id;
                sctx.book_id = active_book_.id;
                sctx.domain = ContentDomain::Book;
                if (k.isInitialized()) {
                    sctx.capabilities = &k.capabilities();
                    sctx.bus = &k.bus();
                    sctx.ui_tasks = &k.uiTasks();
                }
                SelectionActionPopover::show(std::move(sctx));
                return true;
            });
        }
        p_box->addView(ui::makeLabel(blk.original, blk.kind == "heading" ? 19.0f : 16.5f, "brls/text", 1.62f));
        orig_col->addView(p_box);
    }
    split->addView(orig_col);

    auto* trans_col = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    trans_col->setWidthPercentage(49);
    trans_col->addView(ui::makePill("BabelDOC 保真中文版面区块", "status/ok_bg", "domain/book"));
    for (const auto& blk : active_spread_.blocks) {
        auto* p = ui::makeLabel(blk.translated, blk.kind == "heading" ? 19.0f : 16.5f, "brls/text", 1.62f);
        p->setMarginTop(12);
        trans_col->addView(p);
    }
    split->addView(trans_col);

    stage_box_->addView(split);
}

void BookWorkbench::renderEpubCompanion() {
    auto* toolbar = ui::makeCard("desktop/surface", 12.0f, 10.0f);
    toolbar->setMarginBottom(12);
    auto* bar = new brls::Box(brls::Axis::ROW);
    bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    bar->setAlignItems(brls::AlignItems::CENTER);

    std::string hdr_txt = active_book_.title;
    if (!active_spread_.chapter_title.empty()) hdr_txt += " · " + active_spread_.chapter_title;
    hdr_txt += " · 第 " + std::to_string(std::max(1, active_spread_.page_number)) +
               " / " + std::to_string(std::max(1, active_spread_.total_pages)) + " 页";
    bar->addView(ui::makeLabel(hdr_txt, 15.5f, "brls/text"));

    auto* nav = new brls::Box(brls::Axis::ROW);
    auto* prev = ui::makeButton("上一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (active_spread_.page_number > 1) {
                selected_word_idx_ = 0;
                custom_focus_word_.clear();
                clip_jump_query_.clear();
                clip_jump_results_.clear();
                loadSpread(active_spread_.page_number - 1);
            }
            rebuildContent();
        });
    }, false, 34.0f);
    prev->setMarginRight(8);
    nav->addView(prev);

    auto* next = ui::makeButton("下一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (active_spread_.page_number < std::max(1, active_spread_.total_pages)) {
                selected_word_idx_ = 0;
                custom_focus_word_.clear();
                clip_jump_query_.clear();
                clip_jump_results_.clear();
                loadSpread(active_spread_.page_number + 1);
                rebuildContent();
            }
        });
    }, false, 34.0f);
    next->setMarginRight(8);
    nav->addView(next);

    auto* jump_p = ui::makeButton("跳页", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptJumpPage();
    }, false, 34.0f);
    jump_p->setMarginRight(8);
    nav->addView(jump_p);

    nav->addView(ui::makeButton(
        action_busy_ ? "翻译中..." : "AI 译本页",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) translateCurrentEpubPage();
        },
        true, 34.0f));
    bar->addView(nav);
    toolbar->addView(bar);
    stage_box_->addView(toolbar);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    // Left: Calm serif-like reader pane
    auto* reader = ui::makeCard("desktop/surface", 24.0f, 12.0f);
    reader->setWidthPercentage(58);
    reader->setMarginRight(14);
    std::string fmt_upper = active_book_.format.empty() ? "EBOOK" : active_book_.format;
    for (char& ch : fmt_upper) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    reader->addView(ui::makeLabel(active_book_.title + " · " + fmt_upper + " 伴读正文", 19.5f, "brls/text"));

    std::vector<std::string> highlight_words;
    for (const auto& blk : active_spread_.blocks) {
        auto* p_box = new brls::Box(brls::Axis::COLUMN);
        p_box->setWidthPercentage(100);
        p_box->setPadding(6, 10, 6, 10);
        p_box->setCornerRadius(6.0f);
        p_box->setMarginTop(8);
        if (!blk.original.empty()) {
            p_box->setFocusable(true);
            p_box->setHighlightCornerRadius(6.0f);
            p_box->registerClickAction([this, alive = alive_, text = blk.original](brls::View*) {
                if (!alive->load(std::memory_order_acquire)) return true;
                auto& k = HostKernel::instance();
SelectionContext sctx;
sctx.context_sentence = text;
sctx.source_title = active_book_.title + " · P." +
                    std::to_string(active_spread_.page_number) + "/" +
                    std::to_string(std::max(active_spread_.total_pages, active_book_.total_pages));
                sctx.source_id = active_book_.source_id;
                sctx.book_id = active_book_.id;
                sctx.domain = ContentDomain::Book;
                if (k.isInitialized()) {
                    sctx.capabilities = &k.capabilities();
                    sctx.bus = &k.bus();
                    sctx.ui_tasks = &k.uiTasks();
                }
                SelectionActionPopover::show(std::move(sctx));
                return true;
            });
        }
        p_box->addView(ui::makeLabel(blk.original, blk.kind == "heading" ? 19.0f : 17.0f, "brls/text", 1.68f));
        if (!blk.translated.empty()) {
            auto* t = ui::makeLabel(blk.translated, 15.5f, "font/grey", 1.65f);
            t->setMarginTop(4);
            p_box->addView(t);
        }
        reader->addView(p_box);
        for (const auto& hw : blk.highlighted_words) {
            if (std::find(highlight_words.begin(), highlight_words.end(), hw) == highlight_words.end()) {
                highlight_words.push_back(hw);
            }
        }
    }
    split->addView(reader);

    // Right: Sentence & Etymology Companion + NAS Clip Jump
    auto* comp = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    comp->setWidthPercentage(40);
    comp->addView(ui::makeLabel("句子精析 · 词根记忆 & 影视联动", 18.0f, "brls/text"));

    const size_t word_cap = std::min<size_t>(highlight_words.size(), 8);
    brls::Box* word_row = nullptr;
    for (size_t i = 0; i < word_cap; ++i) {
        if (i % 4 == 0) {
            word_row = new brls::Box(brls::Axis::ROW);
            word_row->setMarginTop(i == 0 ? 10 : 6);
            comp->addView(word_row);
        }
        auto* b = ui::makeButton(highlight_words[i], [this, alive = alive_, i]() {
            brls::sync([this, alive, i]() {
                if (!alive->load(std::memory_order_acquire)) return;
                custom_focus_word_.clear();
                selected_word_idx_ = static_cast<int>(i);
                rebuildContent();
            });
        }, custom_focus_word_.empty() && selected_word_idx_ == static_cast<int>(i), 32.0f);
        b->setMarginRight(6);
        word_row->addView(b);
    }
    auto* lookup_row = new brls::Box(brls::Axis::ROW);
    lookup_row->setMarginTop(8);
    lookup_row->setMarginBottom(10);
    lookup_row->addView(ui::makeButton("查任意词 / 词组", [this, alive = alive_]() {
                                           if (alive->load(std::memory_order_acquire)) promptCustomWord();
                                       },
                                       !custom_focus_word_.empty(), 32.0f));
    comp->addView(lookup_row);

    const std::string focus_word =
        !custom_focus_word_.empty()
            ? custom_focus_word_
            : ((!highlight_words.empty() && selected_word_idx_ < static_cast<int>(word_cap))
            ? highlight_words[selected_word_idx_]
            : "degradation");
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

    auto* ety_card = ui::makeCard("desktop/soft", 14.0f, 10.0f);
    ety_card->setMarginBottom(12);
    ety_card->addView(ui::makeLabel(
        "选中词条: " + focus_word + (known_phonetic.empty() ? "" : ("  " + known_phonetic)),
        17.0f, "domain/book"));
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
    ety_card->addView(ui::makeLabel(ety_oss.str(), 14.5f, "font/grey", 1.48f));
    comp->addView(ety_card);

    auto* ai_word_btn = ui::makeButton(
        action_busy_ ? "正在调用 AI 解析..." : "AI 拆解词根与句法 (llm.chat.v1)",
        [this, alive = alive_, focus_word, context_sentence]() {
            if (alive->load(std::memory_order_acquire)) analyzeFocusWord(focus_word, context_sentence);
        },
        false, 36.0f);
    ai_word_btn->setMarginBottom(8);
    comp->addView(ai_word_btn);

    if (!context_sentence.empty()) {
        const std::string skey = active_book_.id + "|" + std::to_string(active_spread_.page_number);
        if (auto sit = sentence_note_cache_.find(skey); sit != sentence_note_cache_.end() && !sit->second.empty()) {
            auto* s_card = ui::makeCard("desktop/soft", 10.0f, 8.0f);
            s_card->setMarginBottom(8);
            s_card->addView(ui::makeLabel("上下文长难句精读笔记:", 13.5f, "domain/book"));
            s_card->addView(ui::makeLabel(sit->second, 13.0f, "font/grey", 1.4f));
            comp->addView(s_card);
        }
        auto* interp_btn = ui::makeButton(
            action_busy_ ? "精读解析中..." : "语境长难句精读 (doc.open.ebook.v1)",
            [this, alive = alive_, context_sentence]() {
                if (alive->load(std::memory_order_acquire)) interpretCurrentSentence(context_sentence);
            },
            false, 36.0f);
        interp_btn->setMarginBottom(8);
        comp->addView(interp_btn);
    }

    auto* jump_btn = ui::makeButton("查找关联影视 / NAS 对话原声片段 (clip_jump.v1)",
                                    [this, alive = alive_, focus_word]() {
                                        if (alive->load(std::memory_order_acquire)) triggerClipJump(focus_word);
                                    }, true, 38.0f);
    comp->addView(jump_btn);

    auto* add_btn = ui::makeButton("加入 SM-2 生词复习队列", [this, alive = alive_, focus_word, context_sentence]() {
        if (alive->load(std::memory_order_acquire)) addWordToVocab(focus_word, context_sentence);
    }, false, 36.0f);
    add_btn->setMarginTop(8);
    comp->addView(add_btn);

    auto* pop_btn = ui::makeButton("划词动作浮层 (vocab / AI / 台词)", [this, alive = alive_, focus_word, context_sentence]() {
        if (!alive->load(std::memory_order_acquire)) return;
        auto& k = HostKernel::instance();
        SelectionContext sctx;
        sctx.selected_text = focus_word;
sctx.context_sentence = context_sentence;
sctx.source_title = active_book_.title + " · P." +
                    std::to_string(active_spread_.page_number) + "/" +
                    std::to_string(std::max(active_spread_.total_pages, active_book_.total_pages));
        sctx.source_id = active_book_.source_id;
        sctx.book_id = active_book_.id;
        sctx.domain = ContentDomain::Book;
        if (k.isInitialized()) {
            sctx.capabilities = &k.capabilities();
            sctx.bus = &k.bus();
            sctx.ui_tasks = &k.uiTasks();
        }
        SelectionActionPopover::show(std::move(sctx));
    }, false, 34.0f);
    pop_btn->setMarginTop(8);
    comp->addView(pop_btn);

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
                        const int start_sec_i = std::isfinite(start_sec) ? std::max(0, static_cast<int>(start_sec)) : 0;
                        HostKernel::instance().bus().publish("entity.media.open", {
                            {"item_id", "clip:" + series + ":" + std::to_string(start_sec_i)},
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
    auto req = action_request_;
    action_busy_ = true;
    status_label_->setText("正在检索 NAS 美剧台词片段: " + word + "...");
    loadUiInBackground(kernel.uiTasks(), "book.clip_jump", req,
        [word, book_title = active_book_.title](const auto&) {
            return HostKernel::instance().capabilities().call("media.clip_jump.v1", {
                {"query", word},
                {"book", book_title},
                {"limit", 5},
                {"auto_open_player", false},
            });
        },
        [this, alive = alive_, req, word](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
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
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消台词片段检索");
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

    auto* w_lbl = ui::makeLabel(card.word + "   " + card.phonetic, 28.0f, "brls/text");
    w_lbl->setMarginTop(14);
    deck->addView(w_lbl);

    auto* ety = ui::makeLabel("词源提示: " + card.etymology, 16.0f, "font/grey");
    ety->setMarginTop(6);
    deck->addView(ety);

    if (!vocab_revealed_) {
        auto* rev = ui::makeButton("显示完整释义与书内原句", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                vocab_revealed_ = true;
                rebuildContent();
            });
        }, true, 40.0f);
        rev->setMarginTop(16);
        deck->addView(rev);
    } else {
        auto* ans = ui::makeCard("desktop/soft", 16.0f, 10.0f);
        ans->setMarginTop(14);
        ans->addView(ui::makeLabel("释义: " + card.meaning, 18.0f, "brls/text"));
        auto* ctx = ui::makeLabel("原句 (" + card.source_book + "): " + card.context_sentence, 15.5f, "font/grey");
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
            auto* b = ui::makeButton(opt.label, [this, alive = alive_, q = opt.quality, days = opt.days]() {
                brls::sync([this, alive, q, days]() {
                    if (!alive->load(std::memory_order_acquire)) return;
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
    auto req = action_request_;
    action_busy_ = true;
    const BookItem target_book = active_book_;
    const BookSpread current_spread = active_spread_;
    const int target_page = std::max(1, current_spread.page_number);
    status_label_->setText("正在精校第 " + std::to_string(target_page) + " 页...");
    rebuildContent();

    if (has_pdf_cap) {
        const auto sources = kernel.domains().bookSources();
        loadInBackground(kernel.jobs(), "BabelDOC 精校第 " + std::to_string(target_page) + " 页", req,
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
            [this, alive = alive_, req, target_book, target_page](auto result_pair) {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
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
            },
            [this, alive = alive_, req]() {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
                action_busy_ = false;
                status_label_->setText("已取消 BabelDOC 精校");
                rebuildContent();
            });
        return;
    }

    loadInBackground(kernel.jobs(), "LLM 精译第 " + std::to_string(target_page) + " 页", req,
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
        [this, alive = alive_, req, target_book, target_page](auto tuple_res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
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
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消 AI 精译");
            rebuildContent();
        });
}

void BookWorkbench::analyzeFocusWord(const std::string& word, const std::string& context_sentence) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    const bool has_vocab = kernel.capabilities().has("vocab.sm2.v1");
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    const bool has_agent = kernel.capabilities().has("ai.agent.v1");
    if (!has_chat && !has_agent) {
        brls::Application::notify("未装载 llm.chat.v1 / ai.agent.v1 插件，请先安装 plugin-magpie-agent");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    const std::string key = active_book_.id + "|" + toLowerCopy(word);
    const std::string title = active_book_.title;
    status_label_->setText("正在解析词条 \"" + word + "\" 的词根与语境句法...");
    rebuildContent();

    loadUiInBackground(kernel.uiTasks(), "book.word_analysis", req,
        [word, context_sentence, title, has_vocab, has_chat](const auto& cancel) {
            if (has_vocab) {
                auto vres = HostKernel::instance().capabilities().callCancellable("vocab.sm2.v1", {
                    {"action", "etymology"},
                    {"word", word},
                    {"book_title", title},
                    {"context_sentence", context_sentence},
                }, cancel);
                if (vres.ok && !vres.data.value("etymology", std::string{}).empty()) {
                    CapabilityResult wrapped = vres;
                    wrapped.data["reply"] = vres.data.value("etymology", "");
                    return wrapped;
                }
            }
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
        [this, alive = alive_, req, key, word](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
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
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消词根解析");
            rebuildContent();
        });
}

void BookWorkbench::translateCurrentEpubPage() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    const bool has_ebook = kernel.capabilities().has("doc.open.ebook.v1");
    const bool has_tr = kernel.capabilities().has("llm.translate.v1");
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    if (!has_ebook && !has_tr && !has_chat) {
        brls::Application::notify("未装载翻译或 eBook 插件，请先在插件市场安装");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    const BookItem target_book = active_book_;
    const BookSpread current_spread = active_spread_;
    const int target_page = std::max(1, current_spread.page_number);
    status_label_->setText("正在翻译第 " + std::to_string(target_page) + " 页段落...");
    rebuildContent();

    if (has_ebook) {
        loadInBackground(kernel.jobs(), "eBook 双语翻译第 " + std::to_string(target_page) + " 页", req,
            [target_book, target_page](const auto& cancel) {
                return HostKernel::instance().capabilities().callCancellable("doc.open.ebook.v1", {
                    {"action", "translate_page"},
                    {"book_id", target_book.id},
                    {"page", target_page},
                }, cancel);
            },
            [this, alive = alive_, req, target_book, target_page](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
                action_busy_ = false;
                if (res.ok && res.data.contains("spread") && res.data["spread"].is_object() &&
                    active_book_.id == target_book.id) {
                    const auto& sj = res.data["spread"];
                    BookSpread sp;
                    sp.book_id = sj.value("book_id", target_book.id);
                    sp.chapter_title = sj.value("chapter_title", "");
                    sp.page_number = sj.value("page_number", target_page);
                    sp.total_pages = sj.value("total_pages", target_book.total_pages);
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
                                    if (hw.is_string()) blk.highlighted_words.push_back(hw.get<std::string>());
                                }
                            }
                            sp.blocks.push_back(std::move(blk));
                        }
                    }
                    active_spread_ = std::move(sp);
                    status_label_->setText("已完成第 " + std::to_string(target_page) + " 页双语对照");
                    brls::Application::notify("本页双语对照翻译完成");
                } else {
                    status_label_->setText("本页翻译失败: " + res.error);
                    brls::Application::notify("本页翻译失败: " + res.error);
                }
                rebuildContent();
            },
            [this, alive = alive_, req]() {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
                action_busy_ = false;
                status_label_->setText("已取消本页翻译");
                rebuildContent();
            });
        return;
    }

    loadInBackground(kernel.jobs(), "LLM 翻译第 " + std::to_string(target_page) + " 页", req,
        [current_spread, has_tr](const auto& cancel) {
            BookSpread out = current_spread;
            std::string last_err;
            bool any_ok = false;
            for (auto& blk : out.blocks) {
                if (cancel->isCancelled()) break;
                if (blk.original.empty()) continue;
                CapabilityResult r = has_tr
                    ? HostKernel::instance().capabilities().callCancellable("llm.translate.v1", {
                          {"text", blk.original}, {"source_lang", "en"}, {"target_lang", "zh-CN"},
                      }, cancel)
                    : HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                          {"domain", "book"},
                          {"prompt", "请将以下英文原版书段落译为自然流畅的简体中文：\n\n" + blk.original},
                      }, cancel);
                if (r.ok) {
                    std::string tr = extractTranslationText(r.data.value("translation", r.data.value("reply", "")));
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
        [this, alive = alive_, req, target_book, target_page](auto tup) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            if (std::get<0>(tup) && active_book_.id == target_book.id) {
                active_spread_ = std::move(std::get<2>(tup));
                status_label_->setText("已完成第 " + std::to_string(target_page) + " 页 AI 翻译");
            } else {
                status_label_->setText("翻译失败: " + std::get<1>(tup));
            }
            rebuildContent();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消 AI 翻译");
            rebuildContent();
        });
}

void BookWorkbench::interpretCurrentSentence(const std::string& sentence) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || sentence.empty()) return;
    const bool has_ebook = kernel.capabilities().has("doc.open.ebook.v1");
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    if (!has_ebook && !has_chat) {
        brls::Application::notify("未装载 doc.open.ebook.v1 或 llm.chat.v1");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    const std::string book_id = active_book_.id;
    const std::string skey = book_id + "|" + std::to_string(active_spread_.page_number);
    status_label_->setText("正在结合原书前后段上下文解析长难句...");
    rebuildContent();

    loadUiInBackground(kernel.uiTasks(), "book.sentence_interp", req,
        [sentence, book_id, has_ebook](const auto& cancel) {
            if (has_ebook) {
                return HostKernel::instance().capabilities().callCancellable("doc.open.ebook.v1", {
                    {"action", "interpret_sentence"},
                    {"sentence", sentence},
                    {"book_id", book_id},
                    {"presets", {"context", "syntax", "expressions"}},
                }, cancel);
            }
            return HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                {"domain", "book"},
                {"prompt", "请拆解以下英文长难句的主干结构、地道表达并给出贴合语境的中文精译：\n\n" + sentence},
            }, cancel);
        },
        [this, alive = alive_, req, skey](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            if (res.ok) {
                std::string interp;
                if (res.data.contains("note") && res.data["note"].is_object()) {
                    interp = res.data["note"].value("ai_interpretation", "");
                } else {
                    interp = res.data.value("reply", res.data.value("text", ""));
                }
                if (!interp.empty()) {
                    sentence_note_cache_[skey] = interp;
                    status_label_->setText("已完成长难句上下文精读并收入笔记");
                    brls::Application::notify("长难句精读已保存至笔记");
                }
            } else {
                status_label_->setText("长难句精读失败: " + res.error);
                brls::Application::notify("精读失败: " + res.error);
            }
            rebuildContent();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消长难句精读");
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
            loadUiInBackground(k.uiTasks(), "book.vocab_add", CancelToken::create(),
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
            loadUiInBackground(k.uiTasks(), "book.vocab_review", CancelToken::create(),
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
    if (payload.value("sub_mode", "") == "vocab") {
        const std::string target_word = toLowerCopy(payload.value("word", ""));
        if (!target_word.empty()) {
            for (size_t i = 0; i < vocab_cards_.size(); ++i) {
                if (toLowerCopy(vocab_cards_[i].word) == target_word) {
                    vocab_idx_ = i;
                    break;
                }
            }
        }
        sub_mode_ = SubMode::VocabReview;
        vocab_revealed_ = !target_word.empty();
        finishRefresh();
        return;
    }
    const std::string bid = payload.value("book_id", payload.value("id", ""));
    const std::string file_path = payload.value("file_path", payload.value("source", ""));
    const std::string title = payload.value("title", "");
    const std::string fmt = toLowerCopy(payload.value("format", ""));
    const int page = std::max(1, payload.value("page", 1));
    const std::string source_id = payload.value("source_id", "");

    BookItem matched;
    bool found = false;
    for (const auto& b : books_) {
        if (!source_id.empty() && b.source_id != source_id) continue;
        if ((!bid.empty() && b.id == bid) ||
            (!file_path.empty() && b.file_path == file_path)) {
            matched = b;
            found = true;
            break;
        }
    }
    const bool curator_request = source_id == "plugin-ai-curator" ||
        (found && matched.source_id == "plugin-ai-curator") ||
        (source_id.empty() && (bid.rfind("english-date-", 0) == 0 || bid == "english-archive-all"));
    if (!found) {
        matched.id = !bid.empty() ? bid : ("ext-book:" + file_path);
        matched.source_id = curator_request ? "plugin-ai-curator" : source_id;
        matched.file_path = file_path;
        if (!fmt.empty()) {
            matched.format = fmt;
        } else {
            std::string ext = toLowerCopy(std::filesystem::path(file_path).extension().string());
            if (!ext.empty() && ext.front() == '.') ext.erase(ext.begin());
            if (ext == "azw" || ext == "prc") ext = "mobi";
            if (ext == "md") ext = "txt";
            matched.format = ext.empty() ? "epub" : ext;
        }
        matched.title = !title.empty()
                            ? title
                            : (file_path.empty() ? "外部电子书" : std::filesystem::path(file_path).filename().string());
        matched.current_page = page;
        matched.total_pages = std::max(page, payload.value("total_pages", 1));
    } else {
        matched.current_page = page;
    }

    auto& kernel = HostKernel::instance();
    if (!found && !curator_request && !file_path.empty() && matched.format == "pdf" &&
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
                if (on_entity_focus_) {
                    on_entity_focus_(active_book_.title + " · P." +
                                     std::to_string(active_spread_.page_number) + "/" +
                                     std::to_string(std::max(active_spread_.total_pages,
                                                             active_book_.total_pages)));
                }
                finishRefresh();
            });
        return;
    }

    if (!found && !curator_request && !file_path.empty() && matched.format != "pdf" &&
        kernel.isInitialized() && kernel.capabilities().has("doc.open.ebook.v1")) {
        action_request_->cancel();
        action_request_ = CancelToken::create();
        loadInBackground(kernel.jobs(), "导入电子书到书架", action_request_,
            [file_path, page](const auto& cancel) {
                return HostKernel::instance().capabilities().callCancellable("doc.open.ebook.v1", {
                    {"action", "import_ebook"},
                    {"file_path", file_path},
                    {"page", page},
                }, cancel);
            },
            [this, alive = alive_, matched, page](CapabilityResult res) mutable {
                if (!alive->load(std::memory_order_acquire)) return;
                if (res.ok && !res.data.value("book_id", std::string{}).empty()) {
                    matched.id = res.data.value("book_id", matched.id);
                    matched.source_id = "plugin-ebook";
                    matched.total_pages = std::max(matched.total_pages, res.data.value("total_pages", matched.total_pages));
                }
                active_book_ = matched;
                has_active_book_ = true;
                sub_mode_ = SubMode::EpubCompanion;
                if (res.ok && res.data.contains("spread") && res.data["spread"].is_object()) {
                    const auto& sj = res.data["spread"];
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
                            if (bj.contains("highlighted_words") && bj["highlighted_words"].is_array()) {
                                for (const auto& hw : bj["highlighted_words"]) {
                                    if (hw.is_string()) blk.highlighted_words.push_back(hw.get<std::string>());
                                }
                            }
                            sp.blocks.push_back(std::move(blk));
                        }
                    }
                    active_spread_ = std::move(sp);
                } else {
                    loadSpread(page);
                }
                if (on_entity_focus_) {
                    on_entity_focus_(active_book_.title + " · P." +
                                     std::to_string(active_spread_.page_number) + "/" +
                                     std::to_string(std::max(active_spread_.total_pages,
                                                             active_book_.total_pages)));
                }
                finishRefresh();
            });
        return;
    }

    active_book_ = matched;
    has_active_book_ = true;
    sub_mode_ = (matched.format == "pdf") ? SubMode::PdfBabelDoc : SubMode::EpubCompanion;
    if (on_entity_focus_) {
        on_entity_focus_(matched.title + " · P." + std::to_string(page) + "/" +
                         std::to_string(matched.total_pages));
    }
    loadSpread(page);
    finishRefresh();
}

void BookWorkbench::promptSearch() {
    const bool daily = sub_mode_ == SubMode::DailyEnglish;
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_, daily](std::string q) {
                brls::sync([this, alive, daily, q = std::move(q)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (daily) english_search_query_ = q;
                    else search_query_ = q;
                    refresh();
                });
            },
            daily ? "检索每日英语" : "检索本地与 NAS 书库",
            daily ? "按课程名称或日期过滤" : "支持按书名、作者或格式过滤",
            128,
            daily ? english_search_query_ : search_query_);
    }
}

void BookWorkbench::promptJumpPage() {
    if (auto* plat = brls::Application::getPlatform()) {
        const int max_p = std::max(1, active_spread_.total_pages);
        plat->getImeManager()->openForText(
            [this, alive = alive_, max_p](std::string val) {
                brls::sync([this, alive, val = std::move(val), max_p]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    try {
                        int target = std::clamp(std::stoi(val), 1, max_p);
                        selected_word_idx_ = 0;
                        custom_focus_word_.clear();
                        clip_jump_query_.clear();
                        clip_jump_results_.clear();
                        loadSpread(target);
                        rebuildContent();
                    } catch (...) {
                        brls::Application::notify("请输入有效页码 (1-" + std::to_string(max_p) + ")");
                    }
                });
            },
            "跳转页码 (1-" + std::to_string(max_p) + ")",
            "输入目标页码",
            16,
            std::to_string(std::max(1, active_spread_.page_number)));
    }
}

void BookWorkbench::promptCustomWord() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string w) {
                brls::sync([this, alive, w = std::move(w)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (!w.empty()) {
                        custom_focus_word_ = w;
                        rebuildContent();
                    }
                });
            },
            "查词与句法/词源拆解",
            "输入任意英文单词或短语",
            64,
            custom_focus_word_);
    }
}

void BookWorkbench::promptDateFilter() {
    const std::string init_date = english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : english_latest_date_;
    CurationCalendarDialog::open({
        .initial_date = init_date,
        .initial_domain = "english",
        .on_apply_date_filter = [this, alive = alive_](const std::string& d) {
            if (!alive->load(std::memory_order_acquire)) return;
            english_selected_date_ = d;
            english_date_mode_ = EnglishDateMode::Selected;
            sub_mode_ = SubMode::DailyEnglish;
            refresh();
        },
        .on_clear_date_filter = [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            english_date_mode_ = EnglishDateMode::All;
            sub_mode_ = SubMode::DailyEnglish;
            refresh();
        },
    });
}

void BookWorkbench::promptImportLocalBook() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string path) {
                brls::sync([this, alive, path = std::move(path)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (!path.empty()) {
                        openExternalBook({{"file_path", path}, {"page", 1}});
                    }
                });
            },
            "导入本地电子书 / PDF",
            "输入 .epub / .mobi / .azw3 / .fb2 / .txt / .pdf 路径",
            256,
            "");
    }
}

} // namespace omnidesk::host
