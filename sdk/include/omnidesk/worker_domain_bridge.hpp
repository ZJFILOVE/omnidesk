#pragma once

#include <memory>
#include <string>
#include <vector>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/domain_registry.hpp>
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
    std::vector<MediaItem> fetchItems(const std::string& category,
                                      const std::string& query = "") override;
    std::vector<LyricLine> fetchLyrics(const std::string& item_id) override;
    std::vector<ChapterSegment> fetchChapters(const std::string& item_id) override;

private:
    WorkerSupervisor& workers_;
    std::string worker_id_;
    std::string plugin_id_;
    std::string source_id_;
    std::string display_name_;
    std::vector<std::string> cached_categories_;
};

/// Pure-JSON out-of-process ITextSource proxy communicating via WorkerSupervisor.
class OMNIDESK_API WorkerTextSource final : public ITextSource {
public:
    WorkerTextSource(WorkerSupervisor& workers,
                     std::string worker_id,
                     std::string plugin_id,
                     std::string source_id,
                     std::string display_name,
                     std::string category = "blog");

    std::string sourceId() const override { return source_id_; }
    std::string displayName() const override { return display_name_; }
    std::string category() const override { return category_; }
    std::vector<TextFeedItem> fetchFeed(const std::string& query = "") override;
    TextArticle fetchArticle(const std::string& article_id) override;

private:
    WorkerSupervisor& workers_;
    std::string worker_id_;
    std::string plugin_id_;
    std::string source_id_;
    std::string display_name_;
    std::string category_;
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

}  // namespace omnidesk
