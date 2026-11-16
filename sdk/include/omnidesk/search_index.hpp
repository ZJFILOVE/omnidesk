#pragma once

#include <cstdint>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "omnidesk/export.h"

namespace omnidesk {

class CapabilityRegistry;
class DomainRegistry;
class EntityBus;

struct OMNIDESK_API SearchDocument {
    std::string doc_id;
    std::string kind;       ///< "nas_clip" | "book" | "obsidian" | "music_review" | "vocab" | "article" | "media" | "command"
    std::string domain;     ///< "media" | "text" | "book" | "core"
    std::string source_id;
    std::string title;
    std::string subtitle;
    std::string body;
    std::string tags;
    nlohmann::json payload = nlohmann::json::object();
    int64_t updated_at = 0;
};

struct OMNIDESK_API SearchHit {
    std::string doc_id;
    std::string kind;
    std::string domain;
    std::string source_id;
    std::string title;
    std::string subtitle;
    std::string snippet;
    double score = 0.0;
    nlohmann::json payload = nlohmann::json::object();
};

struct OMNIDESK_API SearchQueryOptions {
    std::string query;
    std::string kind;
    std::string domain;
    size_t limit = 30;
};

struct OMNIDESK_API SearchIndexStats {
    size_t total_docs = 0;
    std::unordered_map<std::string, size_t> by_kind;
    std::string db_path;
};

enum SearchHydrateScope : uint32_t {
    kHydrateCommands = 1u << 0,
    kHydrateDomains  = 1u << 1,
    kHydrateVocab    = 1u << 2,
    kHydrateArchives = 1u << 3,
    kHydrateAll      = kHydrateCommands | kHydrateDomains | kHydrateVocab | kHydrateArchives,
};

struct OMNIDESK_API SearchHydrateOptions {
    uint32_t scopes = kHydrateAll;
    /// Optional filter when `kHydrateDomains` is set: if non-empty, only queries sources matching these IDs.
    std::unordered_set<std::string> source_ids;
    /// When true, bypasses file mtime cache to force re-reading local archive files.
    bool force = false;
};

/**
 * Lightweight SQLite FTS5 (trigram + short-token fallback) cross-domain search
 * service backing `search.index.v1` and the Ctrl+K OmniCommandPalette.
 */
class OMNIDESK_API SearchIndexService {
public:
    SearchIndexService();
    ~SearchIndexService();

    SearchIndexService(const SearchIndexService&) = delete;
    SearchIndexService& operator=(const SearchIndexService&) = delete;

    bool open(const std::string& db_path);
    void close();
    bool isOpen() const;

    bool upsert(const SearchDocument& doc);
    size_t upsertBatch(const std::vector<SearchDocument>& docs);
    bool remove(const std::string& doc_id);
    bool clearSource(const std::string& source_id);
    bool clearKind(const std::string& kind);

    std::vector<SearchHit> search(const SearchQueryOptions& opts) const;
    SearchIndexStats stats() const;

    /// Number of actual SQLite row mutations performed in the most recent upsert/hydrate call.
    size_t lastWriteCount() const;
    /// Total `sqlite3_prepare_v2` compilations executed on the currently open database handle.
    size_t preparedCompileCount() const;

    /// Hydrates FTS5 index from DomainRegistry, CapabilityRegistry, data_root archives,
    /// config_root vocab fallback, and built-in/plugin commands.
    size_t hydrateEcosystem(DomainRegistry* domains,
                            CapabilityRegistry* caps,
                            const std::string& data_root,
                            const std::string& config_root);

    /// Incremental / scoped hydration supporting source_id filters and file mtime skip.
    size_t hydrateIncremental(DomainRegistry* domains,
                              CapabilityRegistry* caps,
                              const std::string& data_root,
                              const std::string& config_root,
                              const SearchHydrateOptions& opts);

    /// Registers `search.index.v1` on CapabilityRegistry.
    void registerCapability(CapabilityRegistry& caps,
                            DomainRegistry* domains,
                            std::string data_root,
                            std::string config_root);

    static std::string kindDisplayLabel(const std::string& kind);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace omnidesk
