#pragma once

#include "ai_curator/archive_rehydrator.hpp"
#include "ai_curator/daily_scheduler.hpp"
#include "ai_curator/git_mirror.hpp"
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/plugin.hpp>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace ai_curator {

class CurationEngine {
public:
    explicit CurationEngine(omnidesk::PluginContext& ctx);

    void initializeOnLoad();
    void shutdown();

    omnidesk::CapabilityResult handleRecommendFeed(const nlohmann::json& req);
    nlohmann::json statusJson() const;

    // Domain query methods
    std::vector<omnidesk::MediaItem> queryMedia(const std::string& category,
                                                const std::string& query) const;
    std::vector<omnidesk::TextChannel> textChannels() const;
    std::vector<omnidesk::TextFeedItem> queryTextFeed(const std::string& channel_id,
                                                      const std::string& query) const;
    omnidesk::TextArticle getTextArticle(const std::string& article_id) const;
    std::vector<omnidesk::BookItem> listBooks(const std::string& format_filter,
                                              const std::string& query) const;
    omnidesk::BookSpread getBookSpread(const std::string& book_id, int page_number) const;
    std::vector<omnidesk::VocabCard> listVocab() const;

private:
    omnidesk::CapabilityResult actionBootstrapAndRehydrate(const nlohmann::json& req);
    omnidesk::CapabilityResult actionListArchiveDates(const nlohmann::json& req) const;
    omnidesk::CapabilityResult actionSearchArchive(const nlohmann::json& req) const;
    omnidesk::CapabilityResult actionLookupProvenance(const nlohmann::json& req) const;
    omnidesk::CapabilityResult actionGetDateBundle(const nlohmann::json& req) const;
    omnidesk::CapabilityResult actionPublishDailyBundle(const nlohmann::json& req);
    omnidesk::CapabilityResult actionPushInteractiveSet(const nlohmann::json& req);
    omnidesk::CapabilityResult actionPinItem(const nlohmann::json& req);
    omnidesk::CapabilityResult actionDismissItem(const nlohmann::json& req);
    omnidesk::CapabilityResult actionClearInteractive(const nlohmann::json& req);
    omnidesk::CapabilityResult actionStatus(const nlohmann::json& req) const;
    omnidesk::CapabilityResult actionDailyStatus(const nlohmann::json& req);
    omnidesk::CapabilityResult actionConfigureDailyScheduler(const nlohmann::json& req);
    omnidesk::CapabilityResult actionRunDailyWorkflows(const nlohmann::json& req);
    omnidesk::CapabilityResult actionCancelDailyWorkflows(const nlohmann::json& req);
    omnidesk::CapabilityResult actionSyncGitHub(const nlohmann::json& req);

    void rehydrateLocalIncremental(const RepoPaths& paths);

    void loadPersistedLocalStateLocked(const std::string& curation_dir);
    void syncVocabToSm2Idempotent(const std::vector<omnidesk::VocabCard>& cards);
    std::optional<std::string> matchHistoricalProvenanceLocked(const nlohmann::json& item_json,
                                                               const std::string& title,
                                                               const std::string& url,
                                                               size_t min_norm_title_len = 1) const;
    bool validateLedgersReadableLocked(const RepoPaths& paths,
                                       const std::string& operation_id,
                                       const std::vector<omnidesk::MediaItem>& media,
                                       const std::vector<omnidesk::TextFeedItem>& texts,
                                       const std::vector<omnidesk::TextArticle>& articles,
                                       std::string& error_code,
                                       std::string& error_msg) const;

    omnidesk::PluginContext& ctx_;
    mutable std::mutex mutex_;

    RepoPaths last_paths_;
    RepoSyncStatus last_obsidian_sync_;
    RepoSyncStatus last_music_sync_;
    std::string last_rehydrate_time_;
    bool last_cache_hit_ = false;

    HydratedArchive archive_;

    // Live daily bundles (persisted under <data_root>/curation/daily/<date>/<op>.json)
    std::vector<omnidesk::MediaItem> daily_media_;
    std::vector<omnidesk::TextFeedItem> daily_texts_;
    std::unordered_map<std::string, omnidesk::TextArticle> daily_articles_;
    std::vector<omnidesk::BookItem> daily_books_;
    std::vector<omnidesk::VocabCard> daily_vocabs_;

    // Interactive ephemeral sessions (persisted under <data_root>/curation/interactive/sessions/)
    struct InteractiveSession {
        std::string session_id;
        std::string title;
        std::string user_prompt;
        std::string repeat_policy;
        std::vector<omnidesk::MediaItem> media_items;
        std::vector<omnidesk::TextFeedItem> text_items;
        std::unordered_map<std::string, omnidesk::TextArticle> articles;
        std::vector<omnidesk::BookItem> book_items;
        std::unordered_map<std::string, omnidesk::BookSpread> book_spreads;
    };
    InteractiveSession parseInteractiveSession(const nlohmann::json& json,
                                               const std::string& fallback_id) const;
    nlohmann::json interactiveSessionToJson(const InteractiveSession& session) const;
    std::vector<InteractiveSession> interactive_sessions_;
    std::unordered_set<std::string> pinned_ids_;
    std::unordered_set<std::string> dismissed_ids_;

    std::unique_ptr<DailyWorkflowScheduler> scheduler_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
};

} // namespace ai_curator
