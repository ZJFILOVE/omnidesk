#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <vector>

namespace omnidesk {

enum class PluginKind {
    Provider,   ///< Headless capability service (e.g. Magpie AI router, ASR, subtitle sync)
    Workspace,  ///< Independent workspace UI or domain source plugin
    Hybrid,     ///< Both workspace UI and shared capability provider (e.g. fnOS NAS, Chrome Web)
    Extension,  ///< Augments existing domain views (e.g. SponsorBlock timeline overlay)
};

enum class ContentDomain {
    Core,   ///< Cross-domain infrastructure (AI routing, translation, downloads)
    Media,  ///< Video & music workbenches (Bilibili, YouTube, NAS, Music)
    Text,   ///< Articles, blogs, Zhihu, Linux.do, Reddit, scientific journals
    Book,   ///< PDF (BabelDOC), EPUB/MOBI/AZW3 companion reading, SM-2 vocabulary
};

enum class ConfigFieldType {
    String,
    Secret,
    Bool,
    Int,
    Select,
};

struct ConfigField {
    std::string key;
    std::string label;
    ConfigFieldType type = ConfigFieldType::String;
    std::string default_value;
    std::string description;
    std::vector<std::string> options;
    bool required = false;
};

struct MediaItem {
    std::string id;
    std::string source_id;
    std::string title;
    std::string creator;
    std::string kind = "video";       ///< "video" | "music" | "live" | "nas"
    std::string source_badge;         ///< e.g. "Bilibili", "飞牛 NAS", "Music"
    std::string cover_url;
    std::string play_url;
    std::string quality_label;        ///< e.g. "4K HEVC", "FLAC 24bit"
    double duration_sec = 0.0;
    double progress_sec = 0.0;
    uint64_t view_count = 0;
    bool has_bilingual_sub = false;
    nlohmann::json extra = nlohmann::json::object();
};

struct LyricLine {
    double time_sec = 0.0;
    std::string original;
    std::string translation;
};

struct ChapterSegment {
    double start_sec = 0.0;
    double end_sec = 0.0;
    std::string label;
    std::string digest;
    std::string kind = "content";     ///< "content" | "ad" | "sponsor"
};

struct TextFeedItem {
    std::string id;
    std::string source_id;
    std::string source_name;          ///< e.g. "Linux.do", "知乎", "Reddit", "arXiv", "Nature", "Blog"
    std::string category = "blog";    ///< "blog" | "zhihu" | "linuxdo" | "shuiyuan" | "v2ex" | "reddit" | "x" | "journal"
    std::string title;
    std::string excerpt;
    std::string author;
    std::string published_at;
    std::string url;
    std::string cover_url;            ///< Optional thumbnail / hero image URL or file:// URI
    std::unordered_map<std::string, std::string> image_headers; ///< Optional HTTP headers (Referer, Cookie) for images
    std::vector<std::string> tags;
    int comment_count = 0;
    int score = 0;
    bool bilingual_cached = false;
};

struct TextChannel {
    std::string id;                   ///< e.g. "hot", "recommend", "following", "blog:lilianweng"
    std::string name;                 ///< Human-readable channel label shown in TextWorkbench
    std::string badge;                ///< Optional count / status badge (e.g. "12", "需登录")
    bool requires_auth = false;
};

struct TextParagraph {
    std::string id;
    std::string kind = "paragraph";   ///< "heading" | "paragraph" | "quote" | "code" | "formula" | "image"
    std::string original;             ///< For kind == "image": image URL or file:// path
    std::string translated;           ///< For kind == "image": alt text or figure caption
};

struct CommentNode {
    std::string id;
    std::string author;
    std::string role_badge;
    std::string body;
    std::string translated_body;
    std::string created_at;
    int score = 0;
    bool collapsed = false;
    std::vector<CommentNode> replies;
};

struct TextArticle {
    std::string id;
    std::string source_name;
    std::string category;
    std::string title;
    std::string subtitle;
    std::string authors;
    std::string published_at;
    std::string url;
    std::string doi;
    std::string cover_url;            ///< Optional hero / og:image URL or file:// URI
    std::unordered_map<std::string, std::string> image_headers; ///< Optional HTTP headers (Referer, Cookie) for images
    std::vector<std::string> tags;
    std::vector<TextParagraph> paragraphs;
    std::vector<CommentNode> comments;
};

struct BookItem {
    std::string id;
    std::string source_id;
    std::string title;
    std::string author;
    std::string format = "epub";      ///< "pdf" | "epub" | "mobi" | "azw3" | "txt"
    std::string file_path;
    std::string cover_path;
    int current_page = 1;
    int total_pages = 1;
    double progress_percent = 0.0;
    bool bilingual_ready = false;
    int vocab_count = 0;
    bool nas_synced = false;
};

struct BookPageBlock {
    std::string id;
    std::string kind = "paragraph";   ///< "heading" | "paragraph" | "caption" | "formula" | "image"
    std::string original;             ///< For kind == "image": image URL or file:// path
    std::string translated;           ///< For kind == "image": alt text or caption
    std::vector<std::string> highlighted_words;
};

struct BookSpread {
    std::string book_id;
    std::string chapter_title;
    int page_number = 1;
    int total_pages = 1;
    std::vector<BookPageBlock> blocks;
};

struct VocabCard {
    std::string word;
    std::string lemma;
    std::string phonetic;
    std::string etymology;
    std::string meaning;
    std::string context_sentence;
    std::string source_book;
    int interval_days = 1;
    int repetitions = 0;
    double ease_factor = 2.5;
};

inline std::string toString(PluginKind k) {
    switch (k) {
        case PluginKind::Provider: return "provider";
        case PluginKind::Workspace: return "workspace";
        case PluginKind::Hybrid: return "hybrid";
        case PluginKind::Extension: return "extension";
    }
    return "provider";
}

inline PluginKind parsePluginKind(const std::string& s) {
    if (s == "workspace") return PluginKind::Workspace;
    if (s == "hybrid") return PluginKind::Hybrid;
    if (s == "extension") return PluginKind::Extension;
    return PluginKind::Provider;
}

inline std::string toString(ContentDomain d) {
    switch (d) {
        case ContentDomain::Core: return "core";
        case ContentDomain::Media: return "media";
        case ContentDomain::Text: return "text";
        case ContentDomain::Book: return "book";
    }
    return "core";
}

inline ContentDomain parseContentDomain(const std::string& s) {
    if (s == "media") return ContentDomain::Media;
    if (s == "text") return ContentDomain::Text;
    if (s == "book") return ContentDomain::Book;
    return ContentDomain::Core;
}

inline std::string toDisplayLabel(ContentDomain d) {
    switch (d) {
        case ContentDomain::Core: return "底座与路由";
        case ContentDomain::Media: return "影视与音乐";
        case ContentDomain::Text: return "文字与期刊";
        case ContentDomain::Book: return "书本与PDF";
    }
    return "底座与路由";
}

inline std::string toDisplayLabel(PluginKind k) {
    switch (k) {
        case PluginKind::Provider: return "能力底座";
        case PluginKind::Workspace: return "独立工作区";
        case PluginKind::Hybrid: return "工作区+能力";
        case PluginKind::Extension: return "视图扩展";
    }
    return "能力底座";
}

inline std::string toString(ConfigFieldType t) {
    switch (t) {
        case ConfigFieldType::String: return "string";
        case ConfigFieldType::Secret: return "secret";
        case ConfigFieldType::Bool: return "bool";
        case ConfigFieldType::Int: return "int";
        case ConfigFieldType::Select: return "select";
    }
    return "string";
}

inline ConfigFieldType parseConfigFieldType(const std::string& s) {
    if (s == "secret") return ConfigFieldType::Secret;
    if (s == "bool") return ConfigFieldType::Bool;
    if (s == "int") return ConfigFieldType::Int;
    if (s == "select") return ConfigFieldType::Select;
    return ConfigFieldType::String;
}

} // namespace omnidesk
