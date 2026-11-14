#pragma once

#include <borealis.hpp>
#include <functional>
#include <string>
#include <vector>
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

    void refresh();
    void promptSearch();

private:
    enum class SubMode {
        Bookshelf,
        PdfBabelDoc,
        EpubCompanion,
        VocabReview,
    };

    void setSubMode(SubMode m);
    void rebuildContent();
    void renderBookshelf();
    void renderPdfBabelDoc();
    void renderEpubCompanion();
    void renderVocabReview();
    void openBook(const BookItem& book);
    void loadSpread(int page_num);
    void triggerClipJump(const std::string& word);

    std::function<void(const std::string&)> on_entity_focus_;
    std::function<void()> on_open_marketplace_;

    SubMode sub_mode_ = SubMode::Bookshelf;
    std::string search_query_;
    std::vector<BookItem> books_;
    BookItem active_book_;
    BookSpread active_spread_;
    bool has_active_book_ = false;
    std::vector<VocabCard> vocab_cards_;
    size_t vocab_idx_ = 0;
    bool vocab_revealed_ = false;
    int selected_word_idx_ = 0;

    brls::Box* sub_tabs_box_ = nullptr;
    brls::Label* status_label_ = nullptr;
    brls::Box* stage_box_ = nullptr;
};

} // namespace omnidesk::host
