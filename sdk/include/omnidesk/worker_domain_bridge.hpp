#pragma once

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/domain_registry.hpp>
#include <omnidesk/entity_bus.hpp>
#include <omnidesk/export.h>
#include <omnidesk/worker_supervisor.hpp>

namespace omnidesk {

/// Pure-JSON out-of-process IMediaSource proxy communicating with a supervised worker
/// via WorkerSupervisor (pipe or AF_UNIX socket). Zero C++ domain structs cross processes.
class OMNIDESK_API WorkerMediaSource final : public IMediaSource {
public:
    WorkerMediaSource(WorkerSupervisor& workers,
                      std::string worker_id,
                      std::string plugin_id,
                      std::string source_id,
                      std::string display_name,
                      std::vector<std::string> categories = {});

    std::string sourceId() const override { return source_id_; }
    std::string displayName() const override { return display_name_; }
    std::vector<std::string> categories() const override;
    void seedCategories(std::vector<std::string> categories);
    void invalidateMetadataCache();
    std::vector<MediaItem> fetchItems(const std::string& category,
                                      const std::string& query = "") override;
    std::vector<MediaItem> fetchItemsPage(const std::string& category,
                                          const std::string& query,
                                          int page,
                                          bool force_refresh = false) override;
    std::vector<LyricLine> fetchLyrics(const std::string& item_id) override;
    std::vector<ChapterSegment> fetchChapters(const std::string& item_id) override;

private:
    WorkerSupervisor& workers_;
    std::string worker_id_;
    std::string plugin_id_;
    std::string source_id_;
    std::string display_name_;
    mutable std::mutex metadata_mutex_;
    mutable std::vector<std::string> cached_categories_;
    mutable bool categories_cached_ = false;
    mutable uint64_t cached_registration_gen_ = 0;
    mutable uint64_t metadata_epoch_ = 0;
};

/// Pure-JSON out-of-process ITextSource proxy communicating via WorkerSupervisor.
class OMNIDESK_API WorkerTextSource final : public ITextSource {
public:
    WorkerTextSource(WorkerSupervisor& workers,
                     std::string worker_id,
                     std::string plugin_id,
                     std::string source_id,
                     std::string display_name,
                     std::string category = "blog",
                     std::vector<TextChannel> channels = {});

    std::string sourceId() const override { return source_id_; }
    std::string displayName() const override { return display_name_; }
    std::string category() const override { return category_; }
    std::vector<TextChannel> channels() const override;
    void seedChannels(std::vector<TextChannel> channels);
    void invalidateMetadataCache();
    std::vector<TextFeedItem> fetchFeed(const std::string& query = "") override;
    std::vector<TextFeedItem> fetchFeedInChannel(const std::string& channel_id,
                                                 const std::string& query = "") override;
    std::vector<TextFeedItem> fetchFeedPage(const std::string& channel_id,
                                            const std::string& query,
                                            int page,
                                            bool force_refresh = false) override;
    TextArticle fetchArticle(const std::string& article_id) override;

private:
    WorkerSupervisor& workers_;
    std::string worker_id_;
    std::string plugin_id_;
    std::string source_id_;
    std::string display_name_;
    std::string category_;
    mutable std::mutex metadata_mutex_;
    mutable std::vector<TextChannel> cached_channels_;
    mutable bool channels_cached_ = false;
    mutable uint64_t cached_registration_gen_ = 0;
    mutable uint64_t metadata_epoch_ = 0;
};

/// Pure-JSON out-of-process IBookSource proxy communicating via WorkerSupervisor.
class OMNIDESK_API WorkerBookSource final : public IBookSource {
public:
    WorkerBookSource(WorkerSupervisor& workers,
                     std::string worker_id,
                     std::string plugin_id,
                     std::string source_id,
                     std::string display_name);

    std::string sourceId() const override { return source_id_; }
    std::string displayName() const override { return display_name_; }
    std::vector<BookItem> listBooks(const std::string& format_filter = "",
                                    const std::string& query = "") override;
    BookSpread fetchSpread(const std::string& book_id, int page_number) override;
    std::vector<VocabCard> listVocab() override;

private:
    WorkerSupervisor& workers_;
    std::string worker_id_;
    std::string plugin_id_;
    std::string source_id_;
    std::string display_name_;
};

/// Wraps a CapabilityRegistry handler to forward JSON requests cleanly to a supervised worker.
/// When envelope_op is non-empty (e.g. "cap.invoke"), wraps request as
/// {"op": envelope_op, "capability_id": capability_id, "request": req};
/// when empty, merges optional default_action into req directly for custom sidecar scripts.
OMNIDESK_API CapabilityStreamHandler makeWorkerStreamCapabilityHandler(
    WorkerSupervisor& workers,
    std::string worker_id,
    std::string capability_id,
    std::string envelope_op = "cap.invoke",
    std::string default_action = "");

/// RAII handle representing host-side CapabilityRegistry + EntityBus forwarding
/// attached to a duplex WorkerSupervisor runtime.
class OMNIDESK_API WorkerHostBridge {
public:
    virtual ~WorkerHostBridge() = default;
    virtual void detach() = 0;
};

/// Attaches host-side cross-plugin CapabilityRegistry & EntityBus forwarding
/// to a duplex worker registered in WorkerSupervisor.
OMNIDESK_API std::shared_ptr<WorkerHostBridge> attachWorkerHostBridge(
    WorkerSupervisor& workers,
    const std::string& worker_id,
    const std::string& plugin_id,
    CapabilityRegistry& capabilities,
    EntityBus& bus);

}  // namespace omnidesk
