#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include <borealis.hpp>
#include <nlohmann/json.hpp>
#include "omnidesk/types.hpp"

namespace omnidesk::host {

enum class BookReadingStatus {
    Unread,
    Reading,
    Finished,
};

enum class ShelfViewMode {
    Grid,
    List,
};

enum class ShelfSortMode {
    RecentRead,
    RecentAdded,
    ProgressDesc,
    TitleAsc,
};

struct BookShelfGroup {
    std::string id;
    std::string name;
    int64_t created_at_ms = 0;
    int sort_order = 0;
};

struct BookShelfEntry {
    std::string key;
    std::string book_id;
    std::string source_id;
    std::string file_path;
    std::string format;
    std::string title_override;
    std::string author_override;
    std::string cover_path;
    std::string status_override = "auto";  ///< "auto" | "unread" | "reading" | "finished"
    std::string group_id;                  ///< "" = ungrouped
    bool pinned = false;
    int64_t pinned_at_ms = 0;
    bool removed_from_shelf = false;
    int64_t added_at_ms = 0;
    int64_t last_read_at_ms = 0;
    std::string last_chapter_title;
    int current_page = 1;
    int total_pages = 1;
    double progress_percent = -1.0;
};

struct BookExcerpt {
    std::string id;
    std::string book_key;
    std::string book_id;
    std::string source_id;
    std::string file_path;
    std::string book_title;
    std::string author;
    std::string chapter_title;
    int page_number = 1;
    std::string quote_text;
    std::string thought_text;
    std::string ai_analysis;
    int64_t created_at_ms = 0;
};

struct LocalCandidateBook {
    std::string file_path;
    std::string title;
    std::string author;
    std::string format;
    int64_t size_bytes = 0;
    int64_t modified_epoch = 0;
};

class BookLibraryStore {
public:
    static BookLibraryStore& instance();

    std::string libraryJsonPath() const;
    std::string coversDirPath() const;

    static std::string computeBookKey(const std::string& file_path,
                                      const std::string& source_id,
                                      const std::string& book_id);
    static std::string computeBookKey(const BookItem& item);

    static void parseCleanFilenameTitleAuthor(const std::string& raw_stem,
                                              const std::string& fallback_author,
                                              std::string& out_title,
                                              std::string& out_author);

    void reload();

    std::vector<BookShelfGroup> listGroups() const;
    BookShelfGroup createGroup(const std::string& name);
    bool renameGroup(const std::string& group_id, const std::string& new_name);
    bool deleteGroup(const std::string& group_id);
    std::string groupName(const std::string& group_id) const;

    std::vector<std::string> watchedDirs() const;
    bool addWatchedDir(const std::string& raw_dir);
    bool removeWatchedDir(const std::string& raw_dir);

    BookShelfEntry entryForBook(const BookItem& book) const;
    void enrichAndMergeLibraryBooks(std::vector<BookItem>& io_books) const;

    static BookReadingStatus effectiveStatus(const BookItem& book, const BookShelfEntry& entry);
    static std::string statusDisplayLabel(BookReadingStatus st);

    void registerImportedFile(const std::string& file_path,
                              const std::string& group_id = "");
    void touchBookRead(const BookItem& book,
                       int page_number,
                       int total_pages,
                       const std::string& chapter_title = "");
    void setBookStatus(const BookItem& book, const std::string& status_override);
    void setBooksStatus(const std::vector<BookItem>& books, const std::string& status_override);
    void setBookGroup(const BookItem& book, const std::string& group_id);
    void setBooksGroup(const std::vector<BookItem>& books, const std::string& group_id);
    void setBookPinned(const BookItem& book, bool pinned);
    void setBooksPinned(const std::vector<BookItem>& books, bool pinned);
    void setBooksRemovedFromShelf(const std::vector<BookItem>& books, bool removed);
    void updateCachedCoverAndPages(const std::string& file_path,
                                   const std::string& cover_path,
                                   int total_pages);

    // Unified Excerpts / Highlights / Sentence Notes (merged with plugin-ebook notes.json)
    BookExcerpt addExcerpt(const BookItem& book,
                           int page_number,
                           const std::string& chapter_title,
                           const std::string& quote_text,
                           const std::string& thought_text,
                           const std::string& ai_analysis = "");
    bool deleteExcerpt(const std::string& excerpt_id);
    std::vector<BookExcerpt> listAllMergedExcerpts() const;
    std::vector<BookExcerpt> listMergedExcerptsForBook(const BookItem& book) const;
    size_t countExcerptsForBook(const BookItem& book,
                                const std::vector<BookExcerpt>& all_excerpts) const;

    // Export WeRead -> Obsidian Markdown
    bool exportBookMarkdown(const BookItem& book,
                            const std::vector<BookExcerpt>& excerpts,
                            const std::vector<VocabCard>& all_vocab,
                            std::string& out_saved_path,
                            std::string& out_error) const;

    // Off-thread PDF cover + page count extraction (safe fork/execvp pdftoppm & pdfinfo)
    static bool extractPdfCoverAndPagesSync(const std::string& pdf_path,
                                            const std::string& covers_dir,
                                            std::string& out_cover_path,
                                            int& out_total_pages);

    // Fast synchronous cache check & off-thread high-DPI single-page PDF rasterization
    static std::string lookupCachedPdfPagePng(const std::string& pdf_path, int page_number);
    static bool renderPdfPagePngSync(const std::string& pdf_path,
                                     int page_number,
                                     std::string& out_png_path,
                                     int& out_total_pages);

    // Scan a single directory for candidate importable books (.epub/.pdf/.mobi/.azw3/.fb2/.txt)
    static std::vector<LocalCandidateBook> scanCandidateBooksInDir(const std::string& dir_path,
                                                                   int max_depth = 1);

private:
    BookLibraryStore();
    void loadUnlocked(nlohmann::json& out_doc) const;
    bool mutateAndPublish(const std::function<bool(nlohmann::json&)>& mutator);
};

// Reusable WeRead-style 2:3 Cover Widget (real CachedImageView cover + typographic bookcloth fallback)
brls::Box* makeWeReadCoverBox(const BookItem& book,
                              const BookShelfEntry& entry,
                              float cover_height = 216.0f,
                              bool compact_thumb = false);

// Interactive Borealis Local Directory & Book Import Browser Dialog
void openWeReadImportDialog(const std::string& current_group_id,
                            std::function<void(const std::vector<std::string>& imported_paths,
                                               bool open_first)> on_imported);

// Interactive WeRead Book Detail, Status & Group Assignment Dialog
void openWeReadBookDetailDialog(const BookItem& book,
                                const std::vector<VocabCard>& all_vocab,
                                std::function<void(const BookItem&, int page)> on_open_reader,
                                std::function<void(const BookItem&)> on_open_notebook,
                                std::function<void()> on_changed);

// Prompt to add a highlight / thought excerpt for a book
void promptAddBookExcerptDialog(const BookItem& book,
                                int page_number,
                                const std::string& chapter_title,
                                const std::string& initial_quote,
                                std::function<void()> on_saved);

}  // namespace omnidesk::host
