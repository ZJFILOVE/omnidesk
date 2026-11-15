#pragma once

#include "ai_curator/curation_engine.hpp"
#include <memory>
#include <omnidesk/domain_registry.hpp>

namespace ai_curator {

class CuratorMediaSource : public omnidesk::IMediaSource {
public:
    explicit CuratorMediaSource(std::shared_ptr<CurationEngine> engine);
    std::string sourceId() const override;
    std::string displayName() const override;
    std::vector<std::string> categories() const override;
    std::vector<omnidesk::MediaItem> fetchItems(const std::string& category,
                                                const std::string& query = "") override;

private:
    std::shared_ptr<CurationEngine> engine_;
};

class CuratorTextSource : public omnidesk::ITextSource {
public:
    explicit CuratorTextSource(std::shared_ptr<CurationEngine> engine);
    std::string sourceId() const override;
    std::string displayName() const override;
    std::string category() const override;
    std::vector<omnidesk::TextChannel> channels() const override;
    std::vector<omnidesk::TextFeedItem> fetchFeed(const std::string& query = "") override;
    std::vector<omnidesk::TextFeedItem> fetchFeedInChannel(const std::string& channel_id,
                                                           const std::string& query = "") override;
    omnidesk::TextArticle fetchArticle(const std::string& article_id) override;

private:
    std::shared_ptr<CurationEngine> engine_;
};

class CuratorBookSource : public omnidesk::IBookSource {
public:
    explicit CuratorBookSource(std::shared_ptr<CurationEngine> engine);
    std::string sourceId() const override;
    std::string displayName() const override;
    std::vector<omnidesk::BookItem> listBooks(const std::string& format_filter = "",
                                              const std::string& query = "") override;
    omnidesk::BookSpread fetchSpread(const std::string& book_id, int page_number) override;
    std::vector<omnidesk::VocabCard> listVocab() override;

private:
    std::shared_ptr<CurationEngine> engine_;
};

} // namespace ai_curator
