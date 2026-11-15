#pragma once

#include <functional>
#include <map>
#include <nlohmann/json.hpp>
#include <omnidesk/types.hpp>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace ai_curator {

struct HydratedDateStats {
    int music = 0;
    int youtube = 0;
    int news = 0;
    int reddit = 0;
    int journal = 0;
    int english = 0;
    int total() const { return music + youtube + news + reddit + journal + english; }
};

struct EnglishNoteEntry {
    std::string component; // "daily" | "translation" | "vocabulary" | "weekly" | "weekly-tv"
    std::string date;
    std::string title;
    std::string rel_path;
    std::string raw_markdown;
    std::vector<omnidesk::BookPageBlock> blocks;
    std::vector<omnidesk::VocabCard> vocab_cards;
};

struct EnglishDayBundle {
    std::string date;
    std::vector<EnglishNoteEntry> notes;
};

struct HydratedArchive {
    std::vector<omnidesk::MediaItem> media_items;
    std::vector<omnidesk::TextFeedItem> text_items;
    std::unordered_map<std::string, omnidesk::TextArticle> articles;
    std::vector<omnidesk::VocabCard> vocab_cards;
    std::map<std::string, EnglishDayBundle, std::greater<std::string>> english_days;
    std::map<std::string, HydratedDateStats, std::greater<std::string>> date_stats;

    // Read-only provenance & dedup lookup maps (value = earliest/first business_date YYYY-MM-DD)
    std::unordered_map<std::string, std::string> song_id_to_date;
    std::unordered_map<std::string, std::string> canonical_id_to_date;
    std::unordered_map<std::string, std::string> video_id_to_date;
    std::unordered_map<std::string, std::string> post_id_to_date;
    std::unordered_map<std::string, std::string> doi_to_date;
    std::unordered_map<std::string, std::string> url_to_date;
    std::unordered_map<std::string, std::string> norm_title_to_date;

    int dates_indexed = 0;
    int music_tracks_hydrated = 0;
    int youtube_videos_hydrated = 0;
    int news_days_hydrated = 0;
    int news_stories_hydrated = 0;
    int reddit_days_hydrated = 0;
    int reddit_posts_hydrated = 0;
    int journal_days_hydrated = 0;
    int journal_papers_hydrated = 0;
    int english_notes_hydrated = 0;
    int vocab_cards_hydrated = 0;

    void rebuildIndices();
};

class ArchiveRehydrator {
public:
    static bool loadCachedIndex(const std::string& curation_dir, HydratedArchive& out);
    static bool saveCachedIndex(const std::string& curation_dir, const HydratedArchive& archive);

    static bool rehydrate(const std::string& obsidian_vault_dir,
                          const std::string& music_daily_dir,
                          const std::string& curation_dir,
                          bool force_full,
                          HydratedArchive& out,
                          bool& out_cache_hit,
                          std::string& out_error);

    // Exposed for unit testing & direct parsing
    static std::vector<omnidesk::VocabCard> parseVocabularyMarkdown(const std::string& md,
                                                                    const std::string& date);
};

} // namespace ai_curator
