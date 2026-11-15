#include <omnidesk/types_json.hpp>

namespace omnidesk {
namespace {

constexpr int kMaxCommentDepth = 64;

void assignString(const nlohmann::json& j, const char* key, std::string& out) {
    auto it = j.find(key);
    if (it != j.end() && it->is_string()) {
        out = it->get<std::string>();
    }
}

void assignBool(const nlohmann::json& j, const char* key, bool& out) {
    auto it = j.find(key);
    if (it != j.end() && it->is_boolean()) {
        out = it->get<bool>();
    }
}

void assignInt(const nlohmann::json& j, const char* key, int& out) {
    auto it = j.find(key);
    if (it != j.end() && it->is_number_integer()) {
        out = it->get<int>();
    }
}

void assignUInt64(const nlohmann::json& j, const char* key, uint64_t& out) {
    auto it = j.find(key);
    if (it != j.end() && it->is_number_unsigned()) {
        out = it->get<uint64_t>();
    } else if (it != j.end() && it->is_number_integer()) {
        auto v = it->get<int64_t>();
        if (v >= 0) out = static_cast<uint64_t>(v);
    }
}

void assignDouble(const nlohmann::json& j, const char* key, double& out) {
    auto it = j.find(key);
    if (it != j.end() && it->is_number()) {
        out = it->get<double>();
    }
}

void assignStringVec(const nlohmann::json& j, const char* key, std::vector<std::string>& out) {
    out.clear();
    auto it = j.find(key);
    if (it == j.end() || !it->is_array()) {
        return;
    }
    for (const auto& elem : *it) {
        if (elem.is_string()) {
            out.push_back(elem.get<std::string>());
        }
    }
}

void assignStringMap(const nlohmann::json& j,
                     const char* key,
                     std::unordered_map<std::string, std::string>& out) {
    out.clear();
    auto it = j.find(key);
    if (it == j.end() || !it->is_object()) {
        return;
    }
    for (auto mit = it->begin(); mit != it->end(); ++mit) {
        if (mit.value().is_string()) {
            out[mit.key()] = mit.value().get<std::string>();
        }
    }
}

nlohmann::json commentToJsonDepth(const CommentNode& node, int depth) {
    nlohmann::json replies = nlohmann::json::array();
    if (depth < kMaxCommentDepth) {
        for (const auto& r : node.replies) {
            replies.push_back(commentToJsonDepth(r, depth + 1));
        }
    }
    return {
        {"id", node.id},
        {"author", node.author},
        {"role_badge", node.role_badge},
        {"body", node.body},
        {"translated_body", node.translated_body},
        {"created_at", node.created_at},
        {"score", node.score},
        {"collapsed", node.collapsed},
        {"replies", std::move(replies)},
    };
}

void commentFromJsonDepth(const nlohmann::json& j, CommentNode& out, int depth) {
    out = CommentNode{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "author", out.author);
    assignString(j, "role_badge", out.role_badge);
    assignString(j, "body", out.body);
    assignString(j, "translated_body", out.translated_body);
    assignString(j, "created_at", out.created_at);
    assignInt(j, "score", out.score);
    assignBool(j, "collapsed", out.collapsed);
    if (depth < kMaxCommentDepth) {
        auto it = j.find("replies");
        if (it != j.end() && it->is_array()) {
            for (const auto& r : *it) {
                if (!r.is_object()) continue;
                CommentNode child;
                commentFromJsonDepth(r, child, depth + 1);
                out.replies.push_back(std::move(child));
            }
        }
    }
}

}  // namespace

nlohmann::json toJson(const MediaItem& item) {
    return {
        {"id", item.id},
        {"source_id", item.source_id},
        {"title", item.title},
        {"creator", item.creator},
        {"kind", item.kind},
        {"source_badge", item.source_badge},
        {"cover_url", item.cover_url},
        {"play_url", item.play_url},
        {"quality_label", item.quality_label},
        {"duration_sec", item.duration_sec},
        {"progress_sec", item.progress_sec},
        {"view_count", item.view_count},
        {"has_bilingual_sub", item.has_bilingual_sub},
        {"extra", item.extra.is_object() ? item.extra : nlohmann::json::object()},
    };
}

void fromJson(const nlohmann::json& j, MediaItem& out) {
    out = MediaItem{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "source_id", out.source_id);
    assignString(j, "title", out.title);
    assignString(j, "creator", out.creator);
    assignString(j, "kind", out.kind);
    assignString(j, "source_badge", out.source_badge);
    assignString(j, "cover_url", out.cover_url);
    assignString(j, "play_url", out.play_url);
    assignString(j, "quality_label", out.quality_label);
    assignDouble(j, "duration_sec", out.duration_sec);
    assignDouble(j, "progress_sec", out.progress_sec);
    assignUInt64(j, "view_count", out.view_count);
    assignBool(j, "has_bilingual_sub", out.has_bilingual_sub);
    auto it = j.find("extra");
    if (it != j.end() && it->is_object()) {
        out.extra = *it;
    }
}

nlohmann::json toJson(const LyricLine& line) {
    return {
        {"time_sec", line.time_sec},
        {"original", line.original},
        {"translation", line.translation},
    };
}

void fromJson(const nlohmann::json& j, LyricLine& out) {
    out = LyricLine{};
    if (!j.is_object()) return;
    assignDouble(j, "time_sec", out.time_sec);
    assignString(j, "original", out.original);
    assignString(j, "translation", out.translation);
}

nlohmann::json toJson(const ChapterSegment& seg) {
    return {
        {"start_sec", seg.start_sec},
        {"end_sec", seg.end_sec},
        {"label", seg.label},
        {"digest", seg.digest},
        {"kind", seg.kind},
    };
}

void fromJson(const nlohmann::json& j, ChapterSegment& out) {
    out = ChapterSegment{};
    if (!j.is_object()) return;
    assignDouble(j, "start_sec", out.start_sec);
    assignDouble(j, "end_sec", out.end_sec);
    assignString(j, "label", out.label);
    assignString(j, "digest", out.digest);
    assignString(j, "kind", out.kind);
}

nlohmann::json toJson(const TextFeedItem& item) {
    nlohmann::json j = {
        {"id", item.id},
        {"source_id", item.source_id},
        {"source_name", item.source_name},
        {"category", item.category},
        {"title", item.title},
        {"excerpt", item.excerpt},
        {"author", item.author},
        {"published_at", item.published_at},
        {"url", item.url},
        {"tags", item.tags},
        {"comment_count", item.comment_count},
        {"score", item.score},
        {"bilingual_cached", item.bilingual_cached},
    };
    if (!item.cover_url.empty()) j["cover_url"] = item.cover_url;
    if (!item.image_headers.empty()) j["image_headers"] = item.image_headers;
    return j;
}

void fromJson(const nlohmann::json& j, TextFeedItem& out) {
    out = TextFeedItem{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "source_id", out.source_id);
    assignString(j, "source_name", out.source_name);
    assignString(j, "category", out.category);
    assignString(j, "title", out.title);
    assignString(j, "excerpt", out.excerpt);
    assignString(j, "author", out.author);
    assignString(j, "published_at", out.published_at);
    assignString(j, "url", out.url);
    assignString(j, "cover_url", out.cover_url);
    assignStringMap(j, "image_headers", out.image_headers);
    assignStringVec(j, "tags", out.tags);
    assignInt(j, "comment_count", out.comment_count);
    assignInt(j, "score", out.score);
    assignBool(j, "bilingual_cached", out.bilingual_cached);
}

nlohmann::json toJson(const TextChannel& channel) {
    return {
        {"id", channel.id},
        {"name", channel.name},
        {"badge", channel.badge},
        {"requires_auth", channel.requires_auth},
    };
}

void fromJson(const nlohmann::json& j, TextChannel& out) {
    out = TextChannel{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "name", out.name);
    assignString(j, "badge", out.badge);
    assignBool(j, "requires_auth", out.requires_auth);
}

nlohmann::json toJson(const TextParagraph& para) {
    return {
        {"id", para.id},
        {"kind", para.kind},
        {"original", para.original},
        {"translated", para.translated},
    };
}

void fromJson(const nlohmann::json& j, TextParagraph& out) {
    out = TextParagraph{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "kind", out.kind);
    assignString(j, "original", out.original);
    assignString(j, "translated", out.translated);
}

nlohmann::json toJson(const CommentNode& node) {
    return commentToJsonDepth(node, 0);
}

void fromJson(const nlohmann::json& j, CommentNode& out) {
    commentFromJsonDepth(j, out, 0);
}

nlohmann::json toJson(const TextArticle& article) {
    nlohmann::json j = {
        {"id", article.id},
        {"source_name", article.source_name},
        {"category", article.category},
        {"title", article.title},
        {"subtitle", article.subtitle},
        {"authors", article.authors},
        {"published_at", article.published_at},
        {"url", article.url},
        {"doi", article.doi},
        {"tags", article.tags},
        {"paragraphs", toJsonArray(article.paragraphs)},
        {"comments", toJsonArray(article.comments)},
    };
    if (!article.cover_url.empty()) j["cover_url"] = article.cover_url;
    if (!article.image_headers.empty()) j["image_headers"] = article.image_headers;
    return j;
}

void fromJson(const nlohmann::json& j, TextArticle& out) {
    out = TextArticle{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "source_name", out.source_name);
    assignString(j, "category", out.category);
    assignString(j, "title", out.title);
    assignString(j, "subtitle", out.subtitle);
    assignString(j, "authors", out.authors);
    assignString(j, "published_at", out.published_at);
    assignString(j, "url", out.url);
    assignString(j, "doi", out.doi);
    assignString(j, "cover_url", out.cover_url);
    assignStringMap(j, "image_headers", out.image_headers);
    assignStringVec(j, "tags", out.tags);
    if (j.contains("paragraphs")) {
        out.paragraphs = fromJsonArray<TextParagraph>(j["paragraphs"]);
    }
    if (j.contains("comments")) {
        out.comments = fromJsonArray<CommentNode>(j["comments"]);
    }
}

nlohmann::json toJson(const BookItem& item) {
    return {
        {"id", item.id},
        {"source_id", item.source_id},
        {"title", item.title},
        {"author", item.author},
        {"format", item.format},
        {"file_path", item.file_path},
        {"cover_path", item.cover_path},
        {"current_page", item.current_page},
        {"total_pages", item.total_pages},
        {"progress_percent", item.progress_percent},
        {"bilingual_ready", item.bilingual_ready},
        {"vocab_count", item.vocab_count},
        {"nas_synced", item.nas_synced},
    };
}

void fromJson(const nlohmann::json& j, BookItem& out) {
    out = BookItem{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "source_id", out.source_id);
    assignString(j, "title", out.title);
    assignString(j, "author", out.author);
    assignString(j, "format", out.format);
    assignString(j, "file_path", out.file_path);
    assignString(j, "cover_path", out.cover_path);
    assignInt(j, "current_page", out.current_page);
    assignInt(j, "total_pages", out.total_pages);
    assignDouble(j, "progress_percent", out.progress_percent);
    assignBool(j, "bilingual_ready", out.bilingual_ready);
    assignInt(j, "vocab_count", out.vocab_count);
    assignBool(j, "nas_synced", out.nas_synced);
}

nlohmann::json toJson(const BookPageBlock& block) {
    return {
        {"id", block.id},
        {"kind", block.kind},
        {"original", block.original},
        {"translated", block.translated},
        {"highlighted_words", block.highlighted_words},
    };
}

void fromJson(const nlohmann::json& j, BookPageBlock& out) {
    out = BookPageBlock{};
    if (!j.is_object()) return;
    assignString(j, "id", out.id);
    assignString(j, "kind", out.kind);
    assignString(j, "original", out.original);
    assignString(j, "translated", out.translated);
    assignStringVec(j, "highlighted_words", out.highlighted_words);
}

nlohmann::json toJson(const BookSpread& spread) {
    return {
        {"book_id", spread.book_id},
        {"chapter_title", spread.chapter_title},
        {"page_number", spread.page_number},
        {"total_pages", spread.total_pages},
        {"blocks", toJsonArray(spread.blocks)},
    };
}

void fromJson(const nlohmann::json& j, BookSpread& out) {
    out = BookSpread{};
    if (!j.is_object()) return;
    assignString(j, "book_id", out.book_id);
    assignString(j, "chapter_title", out.chapter_title);
    assignInt(j, "page_number", out.page_number);
    assignInt(j, "total_pages", out.total_pages);
    if (j.contains("blocks")) {
        out.blocks = fromJsonArray<BookPageBlock>(j["blocks"]);
    }
}

nlohmann::json toJson(const VocabCard& card) {
    return {
        {"word", card.word},
        {"lemma", card.lemma},
        {"phonetic", card.phonetic},
        {"etymology", card.etymology},
        {"meaning", card.meaning},
        {"context_sentence", card.context_sentence},
        {"source_book", card.source_book},
        {"interval_days", card.interval_days},
        {"repetitions", card.repetitions},
        {"ease_factor", card.ease_factor},
    };
}

void fromJson(const nlohmann::json& j, VocabCard& out) {
    out = VocabCard{};
    if (!j.is_object()) return;
    assignString(j, "word", out.word);
    assignString(j, "lemma", out.lemma);
    assignString(j, "phonetic", out.phonetic);
    assignString(j, "etymology", out.etymology);
    assignString(j, "meaning", out.meaning);
    assignString(j, "context_sentence", out.context_sentence);
    assignString(j, "source_book", out.source_book);
    assignInt(j, "interval_days", out.interval_days);
    assignInt(j, "repetitions", out.repetitions);
    assignDouble(j, "ease_factor", out.ease_factor);
}

nlohmann::json toJson(const CapabilityResult& res) {
    return {
        {"ok", res.ok},
        {"data", res.data},
        {"error", res.error},
        {"provider_plugin_id", res.provider_plugin_id},
        {"cancelled", res.cancelled},
    };
}

void fromJson(const nlohmann::json& j, CapabilityResult& out) {
    out = CapabilityResult{};
    if (!j.is_object()) return;
    assignBool(j, "ok", out.ok);
    if (j.contains("data")) {
        out.data = j["data"];
    }
    assignString(j, "error", out.error);
    assignString(j, "provider_plugin_id", out.provider_plugin_id);
    assignBool(j, "cancelled", out.cancelled);
}

nlohmann::json toJson(const StreamChunk& chunk) {
    return {
        {"delta", chunk.delta},
        {"data", chunk.data},
        {"event", chunk.event},
        {"done", chunk.done},
    };
}

void fromJson(const nlohmann::json& j, StreamChunk& out) {
    out = StreamChunk{};
    if (!j.is_object()) return;
    assignString(j, "delta", out.delta);
    if (j.contains("data")) {
        out.data = j["data"];
    }
    assignString(j, "event", out.event);
    assignBool(j, "done", out.done);
}

nlohmann::json toJson(const JobTarget& target) {
    return {
        {"domain", target.domain},
        {"action_topic", target.action_topic},
        {"entity_id", target.entity_id},
        {"label", target.label},
        {"payload", target.payload.is_object() ? target.payload : nlohmann::json::object()},
    };
}

void fromJson(const nlohmann::json& j, JobTarget& out) {
    out = JobTarget{};
    if (!j.is_object()) return;
    assignString(j, "domain", out.domain);
    assignString(j, "action_topic", out.action_topic);
    assignString(j, "entity_id", out.entity_id);
    assignString(j, "label", out.label);
    auto it = j.find("payload");
    if (it != j.end() && it->is_object()) {
        out.payload = *it;
    }
}

nlohmann::json toJson(const WorkerStatus& status) {
    return {
        {"worker_id", status.worker_id},
        {"plugin_id", status.plugin_id},
        {"registered", status.registered},
        {"running", status.running},
        {"pid", status.pid},
        {"requests_served", status.requests_served},
        {"restart_count", status.restart_count},
        {"last_active_ms", status.last_active_ms},
        {"idle_timeout_ms", status.idle_timeout_ms},
        {"last_exit_code", status.last_exit_code},
        {"recent_stderr", status.recent_stderr},
        {"transport", status.transport == WorkerTransport::UnixSocket ? "unix_socket" : "pipe"},
        {"crash_count", status.crash_count},
        {"last_signal", status.last_signal},
        {"last_signal_name", status.last_signal_name},
        {"last_crash_ms", status.last_crash_ms},
        {"last_exited_unexpectedly", status.last_exited_unexpectedly},
        {"quarantined", status.quarantined},
        {"last_error", status.last_error},
    };
}

}  // namespace omnidesk
