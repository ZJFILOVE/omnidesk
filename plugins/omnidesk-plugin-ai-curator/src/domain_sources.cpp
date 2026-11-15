#include "ai_curator/domain_sources.hpp"

namespace ai_curator {

CuratorMediaSource::CuratorMediaSource(std::shared_ptr<CurationEngine> engine)
    : engine_(std::move(engine)) {}

std::string CuratorMediaSource::sourceId() const { return "plugin-ai-curator"; }
std::string CuratorMediaSource::displayName() const { return "AI 策展与历史精选"; }

std::vector<std::string> CuratorMediaSource::categories() const {
    return {
        "每日 5 首 · 最新",
        "每日 5 首 · 全量曲库",
        "YouTube 每日精选",
        "AI 灵感推荐",
    };
}

std::vector<omnidesk::MediaItem> CuratorMediaSource::fetchItems(const std::string& category,
                                                                const std::string& query) {
    return engine_ ? engine_->queryMedia(category, query) : std::vector<omnidesk::MediaItem>{};
}

CuratorTextSource::CuratorTextSource(std::shared_ptr<CurationEngine> engine)
    : engine_(std::move(engine)) {}

std::string CuratorTextSource::sourceId() const { return "plugin-ai-curator"; }
std::string CuratorTextSource::displayName() const { return "AI 每日精读与期刊"; }
std::string CuratorTextSource::category() const { return "multi"; }

std::vector<omnidesk::TextChannel> CuratorTextSource::channels() const {
    return engine_ ? engine_->textChannels() : std::vector<omnidesk::TextChannel>{};
}

std::vector<omnidesk::TextFeedItem> CuratorTextSource::fetchFeed(const std::string& query) {
    return engine_ ? engine_->queryTextFeed("", query) : std::vector<omnidesk::TextFeedItem>{};
}

std::vector<omnidesk::TextFeedItem> CuratorTextSource::fetchFeedInChannel(const std::string& channel_id,
                                                                          const std::string& query) {
    return engine_ ? engine_->queryTextFeed(channel_id, query) : std::vector<omnidesk::TextFeedItem>{};
}

omnidesk::TextArticle CuratorTextSource::fetchArticle(const std::string& article_id) {
    return engine_ ? engine_->getTextArticle(article_id) : omnidesk::TextArticle{};
}

CuratorBookSource::CuratorBookSource(std::shared_ptr<CurationEngine> engine)
    : engine_(std::move(engine)) {}

std::string CuratorBookSource::sourceId() const { return "plugin-ai-curator"; }
std::string CuratorBookSource::displayName() const { return "英语精读归档与生词本"; }

std::vector<omnidesk::BookItem> CuratorBookSource::listBooks(const std::string& format_filter,
                                                             const std::string& query) {
    return engine_ ? engine_->listBooks(format_filter, query) : std::vector<omnidesk::BookItem>{};
}

omnidesk::BookSpread CuratorBookSource::fetchSpread(const std::string& book_id, int page_number) {
    return engine_ ? engine_->getBookSpread(book_id, page_number) : omnidesk::BookSpread{};
}

std::vector<omnidesk::VocabCard> CuratorBookSource::listVocab() {
    return engine_ ? engine_->listVocab() : std::vector<omnidesk::VocabCard>{};
}

} // namespace ai_curator
