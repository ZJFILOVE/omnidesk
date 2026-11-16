#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/host/book_library_store.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::host {

class RecyclingGrid;

/**
 * Domain 3 Workbench ("书本与 PDF 阅读"):
 * WeRead-inspired unified 2:3 & list library shelf (status, folders, pins, covers),
 * per-book Notebook & Obsidian Markdown exporter, BabelDOC dual-column PDF reader,
 * EPUB sentence/etymology companion with NAS clip-jump hook, and SM-2 vocab review.
 */
class BookWorkbench : public brls::Box {
public:
    struct ShelfDisplayItem {
        BookItem book;
        BookShelfEntry entry;
        BookReadingStatus status = BookReadingStatus::Unread;
        size_t excerpt_count = 0;
    };

    struct NotebookBookGroup {
        BookItem book;
        std::string key;
        size_t excerpt_count = 0;
        size_t vocab_count = 0;
    };

    BookWorkbench(std::function<void(const std::string&)> on_entity_focus,
                  std::function<void()> on_open_marketplace);
    ~BookWorkbench() override;

    void refresh();
    void promptSearch();
    void openExternalBook(const nlohmann::json& payload);
    void onViewportLayoutChanged();
    void applyDateFilter(const std::string& date);

private:
    enum class SubMode {
        Bookshelf,
        Notebook,
        DailyEnglish,
        PdfBabelDoc,
        EpubCompanion,
        VocabReview,
    };

    void setSubMode(SubMode m);
    void rebuildContent();
    void finishRefresh();
    void renderBookshelf();
    void renderDailyEnglishShelf();
    void renderNotebook();
    void renderPdfBabelDoc();
    void renderEpubCompanion();
    void renderVocabReview();
    void openBook(const BookItem& book);
    void openBookDetailModal(const BookItem& book);
    void toggleBatchSelection(const std::string& key);
    std::vector<BookItem> collectBatchSelectedBooks() const;
    void loadSpread(int page_num);
    void ensureLocalPdfPageRendered(int page_num);
    void scheduleMissingPdfCovers();
    void refineCurrentPdfPage();
    void translateCurrentEpubPage();
    void analyzeFocusWord(const std::string& word, const std::string& context_sentence);
    void interpretCurrentSentence(const std::string& sentence);
    void promptJumpPage();
    void promptCustomWord();
    void promptDateFilter();
    void promptImportLocalBook();
    void promptCreateGroup();
    void promptBatchMoveGroup(const std::vector<BookItem>& selected_books);
    void importSelectedLocalPaths(const std::vector<std::string>& paths, bool open_first);
    void triggerClipJump(const std::string& word);
    void addWordToVocab(const std::string& word, const std::string& context_sentence);
    void reviewCurrentVocabCard(int quality, int selected_days);
    void mergeHostVocabFallback();
    void saveHostVocabFallback() const;
    void beginLibraryBusSuppression();
    void endLibraryBusSuppression();

    std::function<void(const std::string&)> on_entity_focus_;
    std::function<void()> on_open_marketplace_;

    SubMode sub_mode_ = SubMode::Bookshelf;
    std::string search_query_;
    std::string format_filter_;
    std::string status_filter_ = "all";       ///< "all" | "reading" | "unread" | "finished"
    std::string group_filter_ = "__all__" ;   ///< "__all__" | "__ungrouped__" | group_id
    ShelfViewMode shelf_view_mode_ = ShelfViewMode::Grid;
    ShelfSortMode shelf_sort_mode_ = ShelfSortMode::RecentRead;
    bool batch_mode_ = false;
    std::unordered_set<std::string> batch_selected_keys_;

    std::string notebook_book_key_;
    int notebook_sub_tab_ = 0;                ///< 0: excerpts/notes, 1: book vocab

    enum class EnglishDateMode { Latest, Selected, All };
    EnglishDateMode english_date_mode_ = EnglishDateMode::Latest;
    std::string english_selected_date_;
    std::string english_latest_date_;
    std::string english_search_query_;

    std::vector<BookItem> preview_books_;
    std::vector<BookItem> books_;
    std::vector<BookExcerpt> cached_excerpts_;
    std::vector<ShelfDisplayItem> visible_shelf_items_;
    std::vector<BookItem> visible_english_items_;
    std::vector<NotebookBookGroup> visible_notebook_groups_;
    std::vector<BookExcerpt> visible_notebook_excerpts_;
    std::vector<VocabCard> visible_notebook_vocab_;
    BookItem active_book_;
    BookSpread active_spread_;
    bool has_active_book_ = false;
    bool pdf_single_page_mode_ = false;
    int pdf_zoom_pct_ = 100;
    std::string rendered_pdf_book_path_;
    int rendered_pdf_page_num_ = 0;
    std::string rendered_pdf_orig_png_;
    std::vector<VocabCard> vocab_cards_;
    uint64_t vocab_mutation_seq_ = 0;
    size_t vocab_idx_ = 0;
    bool vocab_revealed_ = false;
    int selected_word_idx_ = 0;
    std::string custom_focus_word_;
    bool action_busy_ = false;
    std::unordered_map<std::string, std::string> word_analysis_cache_;
    std::unordered_map<std::string, std::string> sentence_note_cache_;
    std::string clip_jump_query_;
    std::vector<nlohmann::json> clip_jump_results_;
    uint64_t download_sub_token_ = 0;
    uint64_t covers_sub_token_ = 0;
    uint64_t recommend_sub_token_ = 0;
    uint64_t books_changed_sub_token_ = 0;
    uint64_t library_sub_token_ = 0;
    std::atomic<bool> suppress_library_bus_{false};
    uint64_t suppress_library_bus_depth_ = 0;
    uint64_t suppress_library_bus_generation_ = 0;
    bool reset_stage_scroll_next_ = false;
    brls::VoidEvent::Subscription resize_sub_;
    int last_bookshelf_cols_ = 0;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> library_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> spread_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> pdf_page_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> action_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> cover_request_ = CancelToken::create();

    brls::Box* sub_tabs_box_ = nullptr;
    brls::Button* date_filter_btn_ = nullptr;
    brls::Label* status_label_ = nullptr;
    brls::Label* batch_count_label_ = nullptr;
    RecyclingGrid* stage_grid_ = nullptr;
    RecyclingGrid* notebook_books_grid_ = nullptr;
    RecyclingGrid* notebook_grid_ = nullptr;
};

} // namespace omnidesk::host
