#pragma once

#include <nlohmann/json.hpp>
#include <omnidesk/async.hpp>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/export.h>
#include <omnidesk/job_center.hpp>
#include <omnidesk/types.hpp>
#include <omnidesk/worker_supervisor.hpp>

namespace omnidesk {

/// Monotonic schema version for pure-JSON domain & capability boundary envelopes.
inline constexpr int kDomainJsonSchemaVersion = 1;

OMNIDESK_API nlohmann::json toJson(const MediaItem& item);
OMNIDESK_API void fromJson(const nlohmann::json& j, MediaItem& out);

OMNIDESK_API nlohmann::json toJson(const LyricLine& line);
OMNIDESK_API void fromJson(const nlohmann::json& j, LyricLine& out);

OMNIDESK_API nlohmann::json toJson(const ChapterSegment& seg);
OMNIDESK_API void fromJson(const nlohmann::json& j, ChapterSegment& out);

OMNIDESK_API nlohmann::json toJson(const TextFeedItem& item);
OMNIDESK_API void fromJson(const nlohmann::json& j, TextFeedItem& out);

OMNIDESK_API nlohmann::json toJson(const TextChannel& channel);
OMNIDESK_API void fromJson(const nlohmann::json& j, TextChannel& out);

OMNIDESK_API nlohmann::json toJson(const TextParagraph& para);
OMNIDESK_API void fromJson(const nlohmann::json& j, TextParagraph& out);

OMNIDESK_API nlohmann::json toJson(const CommentNode& node);
OMNIDESK_API void fromJson(const nlohmann::json& j, CommentNode& out);

OMNIDESK_API nlohmann::json toJson(const TextArticle& article);
OMNIDESK_API void fromJson(const nlohmann::json& j, TextArticle& out);

OMNIDESK_API nlohmann::json toJson(const BookItem& item);
OMNIDESK_API void fromJson(const nlohmann::json& j, BookItem& out);

OMNIDESK_API nlohmann::json toJson(const BookPageBlock& block);
OMNIDESK_API void fromJson(const nlohmann::json& j, BookPageBlock& out);

OMNIDESK_API nlohmann::json toJson(const BookSpread& spread);
OMNIDESK_API void fromJson(const nlohmann::json& j, BookSpread& out);

OMNIDESK_API nlohmann::json toJson(const VocabCard& card);
OMNIDESK_API void fromJson(const nlohmann::json& j, VocabCard& out);

OMNIDESK_API nlohmann::json toJson(const CapabilityResult& res);
OMNIDESK_API void fromJson(const nlohmann::json& j, CapabilityResult& out);

OMNIDESK_API nlohmann::json toJson(const StreamChunk& chunk);
OMNIDESK_API void fromJson(const nlohmann::json& j, StreamChunk& out);

OMNIDESK_API nlohmann::json toJson(const JobTarget& target);
OMNIDESK_API void fromJson(const nlohmann::json& j, JobTarget& out);

OMNIDESK_API nlohmann::json toJson(const WorkerStatus& status);

template <typename T>
inline nlohmann::json toJsonArray(const std::vector<T>& items) {
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& item : items) {
        arr.push_back(toJson(item));
    }
    return arr;
}

template <typename T>
inline std::vector<T> fromJsonArray(const nlohmann::json& arr) {
    std::vector<T> out;
    if (!arr.is_array()) {
        return out;
    }
    out.reserve(arr.size());
    for (const auto& elem : arr) {
        if (!elem.is_object()) {
            continue;
        }
        T item;
        fromJson(elem, item);
        out.push_back(std::move(item));
    }
    return out;
}

}  // namespace omnidesk
