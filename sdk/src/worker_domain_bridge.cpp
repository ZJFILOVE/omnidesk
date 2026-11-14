#include <omnidesk/worker_domain_bridge.hpp>

#include <omnidesk/types_json.hpp>

namespace omnidesk {

WorkerMediaSource::WorkerMediaSource(WorkerSupervisor& workers,
                                     std::string worker_id,
                                     std::string plugin_id,
                                     std::string source_id,
                                     std::string display_name,
                                     std::vector<std::string> categories)
    : workers_(workers),
      worker_id_(std::move(worker_id)),
      plugin_id_(std::move(plugin_id)),
      source_id_(std::move(source_id)),
      display_name_(std::move(display_name)),
      cached_categories_(std::move(categories)) {}

std::vector<std::string> WorkerMediaSource::categories() const {
    if (!cached_categories_.empty()) {
        return cached_categories_;
    }
    CapabilityResult res = workers_.request(
        worker_id_,
        {{"op", "media.categories"}, {"source_id", source_id_}},
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("categories") || !res.data["categories"].is_array()) {
        return {};
    }
    std::vector<std::string> out;
    for (const auto& c : res.data["categories"]) {
        if (c.is_string()) out.push_back(c.get<std::string>());
    }
    return out;
}

std::vector<MediaItem> WorkerMediaSource::fetchItems(const std::string& category,
                                                     const std::string& query) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "media.fetch_items"},
            {"source_id", source_id_},
            {"category", category},
            {"query", query},
        },
        nullptr,
        30000);
    if (!res.ok || !res.data.contains("items")) {
        return {};
    }
    return fromJsonArray<MediaItem>(res.data["items"]);
}

std::vector<LyricLine> WorkerMediaSource::fetchLyrics(const std::string& item_id) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "media.fetch_lyrics"},
            {"source_id", source_id_},
            {"item_id", item_id},
        },
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("lyrics")) {
        return {};
    }
    return fromJsonArray<LyricLine>(res.data["lyrics"]);
}

std::vector<ChapterSegment> WorkerMediaSource::fetchChapters(const std::string& item_id) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "media.fetch_chapters"},
            {"source_id", source_id_},
            {"item_id", item_id},
        },
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("chapters")) {
        return {};
    }
    return fromJsonArray<ChapterSegment>(res.data["chapters"]);
}

WorkerTextSource::WorkerTextSource(WorkerSupervisor& workers,
                                   std::string worker_id,
                                   std::string plugin_id,
                                   std::string source_id,
                                   std::string display_name,
                                   std::string category)
    : workers_(workers),
      worker_id_(std::move(worker_id)),
      plugin_id_(std::move(plugin_id)),
      source_id_(std::move(source_id)),
      display_name_(std::move(display_name)),
      category_(std::move(category)) {}

std::vector<TextFeedItem> WorkerTextSource::fetchFeed(const std::string& query) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "text.fetch_feed"},
            {"source_id", source_id_},
            {"query", query},
        },
        nullptr,
        30000);
    if (!res.ok || !res.data.contains("items")) {
        return {};
    }
    return fromJsonArray<TextFeedItem>(res.data["items"]);
}

TextArticle WorkerTextSource::fetchArticle(const std::string& article_id) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "text.fetch_article"},
            {"source_id", source_id_},
            {"article_id", article_id},
        },
        nullptr,
        30000);
    TextArticle out;
    if (!res.ok || !res.data.contains("article")) {
        return out;
    }
    fromJson(res.data["article"], out);
    return out;
}

WorkerBookSource::WorkerBookSource(WorkerSupervisor& workers,
                                   std::string worker_id,
                                   std::string plugin_id,
                                   std::string source_id,
                                   std::string display_name)
    : workers_(workers),
      worker_id_(std::move(worker_id)),
      plugin_id_(std::move(plugin_id)),
      source_id_(std::move(source_id)),
      display_name_(std::move(display_name)) {}

std::vector<BookItem> WorkerBookSource::listBooks(const std::string& format_filter,
                                                  const std::string& query) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "book.list_books"},
            {"source_id", source_id_},
            {"format_filter", format_filter},
            {"query", query},
        },
        nullptr,
        30000);
    if (!res.ok || !res.data.contains("items")) {
        return {};
    }
    return fromJsonArray<BookItem>(res.data["items"]);
}

BookSpread WorkerBookSource::fetchSpread(const std::string& book_id, int page_number) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "book.fetch_spread"},
            {"source_id", source_id_},
            {"book_id", book_id},
            {"page_number", page_number},
        },
        nullptr,
        30000);
    BookSpread out;
    if (!res.ok || !res.data.contains("spread")) {
        return out;
    }
    fromJson(res.data["spread"], out);
    return out;
}

std::vector<VocabCard> WorkerBookSource::listVocab() {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "book.list_vocab"},
            {"source_id", source_id_},
        },
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("vocab")) {
        return {};
    }
    return fromJsonArray<VocabCard>(res.data["vocab"]);
}

CapabilityStreamHandler makeWorkerStreamCapabilityHandler(
    WorkerSupervisor& workers,
    std::string worker_id,
    std::string capability_id,
    std::string envelope_op,
    std::string default_action) {
    return [&workers,
            worker_id = std::move(worker_id),
            capability_id = std::move(capability_id),
            envelope_op = std::move(envelope_op),
            default_action = std::move(default_action)](
               const nlohmann::json& request,
               const std::shared_ptr<CancelToken>& cancel,
               const StreamCallback& on_chunk) -> CapabilityResult {
        nlohmann::json payload;
        if (!envelope_op.empty()) {
            payload = {
                {"op", envelope_op},
                {"capability_id", capability_id},
                {"stream", static_cast<bool>(on_chunk)},
                {"request", request.is_object() ? request : nlohmann::json::object()},
            };
        } else {
            payload = request.is_object() ? request : nlohmann::json::object();
            if (!default_action.empty() && !payload.contains("action")) {
                payload["action"] = default_action;
            }
        }
        return workers.streamRequest(worker_id, payload, on_chunk, cancel, 300000);
    };
}

}  // namespace omnidesk
