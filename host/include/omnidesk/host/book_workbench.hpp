#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::host {

/**
 * Domain 3 Workbench ("书本与 PDF 阅读"):
 * 2:3 vertical bookshelf, BabelDOC dual-column layout PDF reader,
 * EPUB sentence/etymology companion with NAS clip-jump hook, and SM-2 vocab review.
 */
class BookWorkbench : public brls::Box {
public:
    BookWorkbench(std::function<void(const std::string&)> on_entity_focus,
                  std::function<void()> on_open_marketplace);
    ~BookWorkbench() override;

    void refresh();
    void promptSearch();
    void openExternalBook(const nlohmann::json& payload);
    void onViewportLayoutChanged();

private:
    enum class SubMode {
        Bookshelf,
        DailyEnglish,
        PdfBabelDoc,
        EpubCompanion,
        VocabReview,
    };

    void setSubMode(SubMode m);
    void rebuildContent();
    void finishRefresh();
    void renderBookshelf();
    void renderPdfBabelDoc();
    void renderEpubCompanion();
    void renderVocabReview();
    void openBook(const BookItem& book);
    void loadSpread(int page_num);
    void refineCurrentPdfPage();
    void translateCurrentEpubPage();
    void analyzeFocusWord(const std::string& word, const std::string& context_sentence);
    void interpretCurrentSentence(const std::string& sentence);
    void promptJumpPage();
    void promptCustomWord();
    void promptDateFilter();
    void promptImportLocalBook();
    void triggerClipJump(const std::string& word);
    void addWordToVocab(const std::string& word, const std::string& context_sentence);
    void reviewCurrentVocabCard(int quality, int selected_days);
    void mergeHostVocabFallback();
    void saveHostVocabFallback() const;

    std::function<void(const std::string&)> on_entity_focus_;
    std::function<void()> on_open_marketplace_;

    SubMode sub_mode_ = SubMode::Bookshelf;
    std::string search_query_;
std::string format_filter_;
enum class EnglishDateMode { Latest, Selected, All };
EnglishDateMode english_date_mode_ = EnglishDateMode::Latest;
std::string english_selected_date_;
std::string english_latest_date_;
std::string english_search_query_;
std::vector<BookItem> preview_books_;
    std::vector<BookItem> books_;
    BookItem active_book_;
    BookSpread active_spread_;
    bool has_active_book_ = false;
    std::vector<VocabCard> vocab_cards_;
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
    uint64_t recommend_sub_token_ = 0;
    brls::VoidEvent::Subscription resize_sub_;
    int last_bookshelf_cols_ = 0;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> library_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> spread_request_ = CancelToken::create();
    std::shared_ptr<CancelToken> action_request_ = CancelToken::create();

    brls::Box* sub_tabs_box_ = nullptr;
    brls::Button* date_filter_btn_ = nullptr;
    brls::Label* status_label_ = nullptr;
    brls::Box* stage_box_ = nullptr;
};

} // namespace omnidesk::host
