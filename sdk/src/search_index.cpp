#include "omnidesk/search_index.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <sqlite3.h>
#include <sstream>

#include "omnidesk/capabilities.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/types_json.hpp"

namespace fs = std::filesystem;

namespace omnidesk {
namespace {

int64_t nowUnixSec() {
    return std::chrono::duration_cast<std::chrono::seconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

size_t countUtf8Codepoints(const std::string& s) {
    size_t count = 0;
    for (unsigned char c : s) {
        if ((c & 0xC0u) != 0x80u) ++count;
    }
    return count;
}

std::vector<std::string> splitWhitespace(const std::string& s) {
    std::vector<std::string> out;
    std::istringstream iss(s);
    std::string tok;
    while (iss >> tok) {
        if (!tok.empty()) out.push_back(std::move(tok));
    }
    return out;
}

std::string escapeFtsPhrase(const std::string& token) {
    std::string out = "\"";
    for (char c : token) {
        if (c == '"') out += "\"\"";
        else out.push_back(c);
    }
    out += "\"";
    return out;
}

std::string makeSnippet(const std::string& title,
                        const std::string& subtitle,
                        const std::string& body,
                        const std::string& query) {
    auto truncateUtf8 = [](const std::string& src, size_t max_bytes) -> std::string {
        if (src.size() <= max_bytes) return src;
        size_t cut = max_bytes > 3 ? max_bytes - 3 : max_bytes;
        while (cut > 0 && (static_cast<unsigned char>(src[cut]) & 0xC0) == 0x80) --cut;
        return src.substr(0, cut) + "...";
    };
    if (!body.empty()) {
        if (!query.empty()) {
            std::string body_low = body;
            std::string q_low = query;
            for (char& ch : body_low) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
            for (char& ch : q_low) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
            const auto first_tok = splitWhitespace(q_low);
            if (!first_tok.empty()) {
                size_t pos = body_low.find(first_tok.front());
                if (pos != std::string::npos) {
                    size_t start = pos > 36 ? pos - 36 : 0;
                    while (start > 0 && (static_cast<unsigned char>(body[start]) & 0xC0) == 0x80) --start;
                    std::string slice = body.substr(start, 140);
                    if (start > 0) slice = "..." + slice;
                    return truncateUtf8(slice, 144);
                }
            }
        }
        return truncateUtf8(body, 140);
    }
    if (!subtitle.empty()) return truncateUtf8(subtitle, 140);
    return truncateUtf8(title, 140);
}

std::string colText(sqlite3_stmt* stmt, int col) {
    const unsigned char* txt = sqlite3_column_text(stmt, col);
    return txt ? reinterpret_cast<const char*>(txt) : std::string{};
}

nlohmann::json readJsonFileQuiet(const fs::path& p) {
    std::ifstream ifs(p);
    if (!ifs.is_open()) return nlohmann::json();
    try {
        nlohmann::json j;
        ifs >> j;
        return j;
    } catch (...) {
        return nlohmann::json();
    }
}

std::string readTextHead(const fs::path& p, size_t max_bytes = 6144) {
    std::ifstream ifs(p, std::ios::binary);
    if (!ifs.is_open()) return "";
    std::string buf(max_bytes, '\0');
    ifs.read(buf.data(), static_cast<std::streamsize>(max_bytes));
    buf.resize(static_cast<size_t>(ifs.gcount()));
    return buf;
}

} // namespace

struct SearchIndexService::Impl {
    mutable std::mutex mutex;
    sqlite3* db = nullptr;
    std::string db_path;

    bool execSql(const char* sql) {
        if (!db) return false;
        char* err = nullptr;
        const int rc = sqlite3_exec(db, sql, nullptr, nullptr, &err);
        if (err) sqlite3_free(err);
        return rc == SQLITE_OK;
    }

    bool upsertDocLocked(const SearchDocument& doc) {
        if (!db || doc.doc_id.empty()) return false;
        const int64_t ts = doc.updated_at > 0 ? doc.updated_at : nowUnixSec();
        const std::string payload_str = doc.payload.dump();

        {
            sqlite3_stmt* stmt = nullptr;
            const char* sql =
                "INSERT OR REPLACE INTO docs "
                "(doc_id, kind, domain, source_id, title, subtitle, body, tags, payload_json, updated_at) "
                "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10);";
            if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) return false;
            sqlite3_bind_text(stmt, 1, doc.doc_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 2, doc.kind.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 3, doc.domain.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 4, doc.source_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 5, doc.title.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 6, doc.subtitle.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 7, doc.body.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 8, doc.tags.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 9, payload_str.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_int64(stmt, 10, ts);
            const int rc = sqlite3_step(stmt);
            sqlite3_finalize(stmt);
            if (rc != SQLITE_DONE) return false;
        }
        {
            sqlite3_stmt* del_stmt = nullptr;
            if (sqlite3_prepare_v2(db, "DELETE FROM docs_fts WHERE doc_id = ?1;", -1, &del_stmt, nullptr) != SQLITE_OK) {
                return false;
            }
            sqlite3_bind_text(del_stmt, 1, doc.doc_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_step(del_stmt);
            sqlite3_finalize(del_stmt);
        }
        {
            sqlite3_stmt* fts_stmt = nullptr;
            const char* fts_sql =
                "INSERT INTO docs_fts (doc_id, kind, domain, title, subtitle, body, tags) "
                "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7);";
            if (sqlite3_prepare_v2(db, fts_sql, -1, &fts_stmt, nullptr) != SQLITE_OK) return false;
            sqlite3_bind_text(fts_stmt, 1, doc.doc_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(fts_stmt, 2, doc.kind.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(fts_stmt, 3, doc.domain.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(fts_stmt, 4, doc.title.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(fts_stmt, 5, doc.subtitle.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(fts_stmt, 6, doc.body.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(fts_stmt, 7, doc.tags.c_str(), -1, SQLITE_TRANSIENT);
            const int rc = sqlite3_step(fts_stmt);
            sqlite3_finalize(fts_stmt);
            if (rc != SQLITE_DONE) return false;
        }
        return true;
    }
};

std::string SearchIndexService::kindDisplayLabel(const std::string& kind) {
    if (kind == "nas_clip") return "飞牛台词";
    if (kind == "book") return "书架";
    if (kind == "obsidian") return "往期精选";
    if (kind == "music_review") return "乐评";
    if (kind == "vocab") return "生词卡";
    if (kind == "command") return "命令";
    if (kind == "media") return "影视音乐";
    if (kind == "article") return "文章";
    return kind.empty() ? "条目" : kind;
}

SearchIndexService::SearchIndexService() : impl_(std::make_unique<Impl>()) {}

SearchIndexService::~SearchIndexService() {
    close();
}

bool SearchIndexService::open(const std::string& db_path) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    if (impl_->db) {
        sqlite3_close_v2(impl_->db);
        impl_->db = nullptr;
    }
    if (db_path != ":memory:") {
        std::error_code ec;
        fs::path p(db_path);
        if (p.has_parent_path()) {
            fs::create_directories(p.parent_path(), ec);
            if (ec) return false;
        }
    }

    sqlite3* db = nullptr;
    const int rc = sqlite3_open_v2(
        db_path.c_str(),
        &db,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
        nullptr);
    if (rc != SQLITE_OK || !db) {
        if (db) sqlite3_close_v2(db);
        return false;
    }

    impl_->db = db;
    impl_->db_path = db_path;

    if (db_path != ":memory:") {
        impl_->execSql("PRAGMA journal_mode=WAL;");
        impl_->execSql("PRAGMA synchronous=NORMAL;");
    }
    impl_->execSql("PRAGMA busy_timeout=3000;");

    const bool ok_tables =
        impl_->execSql(
            "CREATE TABLE IF NOT EXISTS docs ("
            "  doc_id TEXT PRIMARY KEY,"
            "  kind TEXT NOT NULL,"
            "  domain TEXT NOT NULL,"
            "  source_id TEXT NOT NULL,"
            "  title TEXT NOT NULL,"
            "  subtitle TEXT NOT NULL DEFAULT '',"
            "  body TEXT NOT NULL DEFAULT '',"
            "  tags TEXT NOT NULL DEFAULT '',"
            "  payload_json TEXT NOT NULL DEFAULT '{}',"
            "  updated_at INTEGER NOT NULL DEFAULT 0"
            ");"
            "CREATE INDEX IF NOT EXISTS idx_docs_kind_domain ON docs(kind, domain, updated_at DESC);") &&
        impl_->execSql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
            "  doc_id UNINDEXED,"
            "  kind UNINDEXED,"
            "  domain UNINDEXED,"
            "  title,"
            "  subtitle,"
            "  body,"
            "  tags,"
            "  tokenize='trigram'"
            ");");

    if (!ok_tables) {
        sqlite3_close_v2(impl_->db);
        impl_->db = nullptr;
        return false;
    }
    return true;
}

void SearchIndexService::close() {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    if (impl_->db) {
        sqlite3_close_v2(impl_->db);
        impl_->db = nullptr;
    }
    impl_->db_path.clear();
}

bool SearchIndexService::isOpen() const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    return impl_->db != nullptr;
}

bool SearchIndexService::upsert(const SearchDocument& doc) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    return impl_->upsertDocLocked(doc);
}

size_t SearchIndexService::upsertBatch(const std::vector<SearchDocument>& docs) {
    if (docs.empty()) return 0;
    std::lock_guard<std::mutex> lk(impl_->mutex);
    if (!impl_->db) return 0;
    if (!impl_->execSql("BEGIN IMMEDIATE;")) return 0;
    size_t count = 0;
    for (const auto& doc : docs) {
        if (impl_->upsertDocLocked(doc)) {
            ++count;
        }
    }
    if (!impl_->execSql("COMMIT;")) {
        impl_->execSql("ROLLBACK;");
        return 0;
    }
    return count;
}

bool SearchIndexService::remove(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    if (!impl_->db || doc_id.empty()) return false;
    {
        sqlite3_stmt* s = nullptr;
        if (sqlite3_prepare_v2(impl_->db, "DELETE FROM docs WHERE doc_id = ?1;", -1, &s, nullptr) != SQLITE_OK) {
            return false;
        }
        sqlite3_bind_text(s, 1, doc_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_step(s);
        sqlite3_finalize(s);
    }
    {
        sqlite3_stmt* s = nullptr;
        if (sqlite3_prepare_v2(impl_->db, "DELETE FROM docs_fts WHERE doc_id = ?1;", -1, &s, nullptr) == SQLITE_OK) {
            sqlite3_bind_text(s, 1, doc_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_step(s);
            sqlite3_finalize(s);
        }
    }
    return true;
}

bool SearchIndexService::clearSource(const std::string& source_id) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    if (!impl_->db || source_id.empty()) return false;
    if (!impl_->execSql("BEGIN IMMEDIATE;")) return false;
    {
        sqlite3_stmt* s = nullptr;
        if (sqlite3_prepare_v2(
                impl_->db,
                "DELETE FROM docs_fts WHERE doc_id IN (SELECT doc_id FROM docs WHERE source_id = ?1);",
                -1, &s, nullptr) == SQLITE_OK) {
            sqlite3_bind_text(s, 1, source_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_step(s);
            sqlite3_finalize(s);
        }
    }
    {
        sqlite3_stmt* s = nullptr;
        if (sqlite3_prepare_v2(impl_->db, "DELETE FROM docs WHERE source_id = ?1;", -1, &s, nullptr) == SQLITE_OK) {
            sqlite3_bind_text(s, 1, source_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_step(s);
            sqlite3_finalize(s);
        }
    }
    impl_->execSql("COMMIT;");
    return true;
}

bool SearchIndexService::clearKind(const std::string& kind) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    if (!impl_->db || kind.empty()) return false;
    if (!impl_->execSql("BEGIN IMMEDIATE;")) return false;
    {
        sqlite3_stmt* s = nullptr;
        if (sqlite3_prepare_v2(
                impl_->db,
                "DELETE FROM docs_fts WHERE kind = ?1;",
                -1, &s, nullptr) == SQLITE_OK) {
            sqlite3_bind_text(s, 1, kind.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_step(s);
            sqlite3_finalize(s);
        }
    }
    {
        sqlite3_stmt* s = nullptr;
        if (sqlite3_prepare_v2(impl_->db, "DELETE FROM docs WHERE kind = ?1;", -1, &s, nullptr) == SQLITE_OK) {
            sqlite3_bind_text(s, 1, kind.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_step(s);
            sqlite3_finalize(s);
        }
    }
    impl_->execSql("COMMIT;");
    return true;
}

std::vector<SearchHit> SearchIndexService::search(const SearchQueryOptions& opts) const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    std::vector<SearchHit> hits;
    if (!impl_->db) return hits;

    const size_t limit = std::clamp<size_t>(opts.limit, 1, 200);
    const auto raw_tokens = splitWhitespace(opts.query);
    std::vector<std::string> fts_tokens;
    std::vector<std::string> short_tokens;
    for (const auto& t : raw_tokens) {
        if (countUtf8Codepoints(t) >= 3) fts_tokens.push_back(t);
        else short_tokens.push_back(t);
    }

    std::ostringstream sql;
    std::vector<std::string> binds;
    auto addBind = [&](const std::string& v) -> int {
        binds.push_back(v);
        return static_cast<int>(binds.size());
    };

    if (!fts_tokens.empty()) {
        std::string match_expr;
        for (size_t i = 0; i < fts_tokens.size(); ++i) {
            if (i > 0) match_expr += " AND ";
            match_expr += escapeFtsPhrase(fts_tokens[i]);
        }
        const int m_idx = addBind(match_expr);
        sql << "SELECT d.doc_id, d.kind, d.domain, d.source_id, d.title, d.subtitle, d.body, d.payload_json, "
               "-bm25(docs_fts, 10.0, 5.0, 2.0, 4.0) AS rank_score "
               "FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.doc_id "
               "WHERE docs_fts MATCH ?" << m_idx;
    } else {
        sql << "SELECT d.doc_id, d.kind, d.domain, d.source_id, d.title, d.subtitle, d.body, d.payload_json, "
               "CASE WHEN d.kind = 'command' THEN 10.0 ELSE 1.0 END AS rank_score "
               "FROM docs d WHERE 1=1";
    }

    if (!opts.kind.empty()) {
        const int k_idx = addBind(opts.kind);
        sql << " AND d.kind = ?" << k_idx;
    }
    if (!opts.domain.empty()) {
        const int d_idx = addBind(opts.domain);
        sql << " AND d.domain = ?" << d_idx;
    }
    for (const auto& st : short_tokens) {
        const int s_idx = addBind(st);
        sql << " AND (INSTR(lower(d.title), lower(?" << s_idx << ")) > 0 "
               "OR INSTR(lower(d.subtitle), lower(?" << s_idx << ")) > 0 "
               "OR INSTR(lower(d.body), lower(?" << s_idx << ")) > 0 "
               "OR INSTR(lower(d.tags), lower(?" << s_idx << ")) > 0)";
    }

    if (!fts_tokens.empty()) {
        sql << " ORDER BY rank_score DESC, d.updated_at DESC";
    } else {
        sql << " ORDER BY CASE WHEN d.kind = 'command' THEN 0 ELSE 1 END, d.updated_at DESC";
    }
    sql << " LIMIT " << limit << ";";

    sqlite3_stmt* stmt = nullptr;
    const std::string sql_str = sql.str();
    if (sqlite3_prepare_v2(impl_->db, sql_str.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        return hits;
    }
    for (size_t i = 0; i < binds.size(); ++i) {
        sqlite3_bind_text(stmt, static_cast<int>(i + 1), binds[i].c_str(), -1, SQLITE_TRANSIENT);
    }

    while (sqlite3_step(stmt) == SQLITE_ROW) {
        SearchHit hit;
        hit.doc_id = colText(stmt, 0);
        hit.kind = colText(stmt, 1);
        hit.domain = colText(stmt, 2);
        hit.source_id = colText(stmt, 3);
        hit.title = colText(stmt, 4);
        hit.subtitle = colText(stmt, 5);
        const std::string body = colText(stmt, 6);
        const std::string payload_raw = colText(stmt, 7);
        hit.score = sqlite3_column_double(stmt, 8);
        hit.snippet = makeSnippet(hit.title, hit.subtitle, body, opts.query);
        try {
            auto pj = nlohmann::json::parse(payload_raw);
            if (pj.is_object()) hit.payload = std::move(pj);
        } catch (...) {
        }
        hits.push_back(std::move(hit));
    }
    sqlite3_finalize(stmt);
    return hits;
}

SearchIndexStats SearchIndexService::stats() const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    SearchIndexStats st;
    st.db_path = impl_->db_path;
    if (!impl_->db) return st;

    sqlite3_stmt* stmt = nullptr;
    if (sqlite3_prepare_v2(impl_->db, "SELECT kind, COUNT(*) FROM docs GROUP BY kind;", -1, &stmt, nullptr) == SQLITE_OK) {
        while (sqlite3_step(stmt) == SQLITE_ROW) {
            const std::string k = colText(stmt, 0);
            const size_t c = static_cast<size_t>(sqlite3_column_int64(stmt, 1));
            st.by_kind[k] = c;
            st.total_docs += c;
        }
        sqlite3_finalize(stmt);
    }
    return st;
}

size_t SearchIndexService::hydrateEcosystem(DomainRegistry* domains,
                                            CapabilityRegistry* caps,
                                            const std::string& data_root,
                                            const std::string& config_root) {
    if (!isOpen()) return 0;
    std::vector<SearchDocument> batch;
    const int64_t now = nowUnixSec();

    // 1. Built-in host quick commands
    const struct {
        const char* id;
        const char* title;
        const char* sub;
        const char* action;
        const char* domain;
    } builtin_cmds[] = {
        {"cmd:ws:store", "切换至「插件市场」工作台", "Ctrl+1 · 安装/更新/配置插件", "workspace:store", "core"},
        {"cmd:ws:media", "切换至「影视与音乐」工作台", "Ctrl+2 · 16:9 影视 / 双语歌词 / NAS / MPV", "workspace:media", "media"},
        {"cmd:ws:text", "切换至「文字与期刊」工作台", "Ctrl+3 · 博客 / 知乎 / Reddit / 科学期刊", "workspace:text", "text"},
        {"cmd:ws:book", "切换至「书本与 PDF」工作台", "Ctrl+4 · 2:3 书架 / BabelDOC / SM-2", "workspace:book", "book"},
        {"cmd:ws:settings", "打开宿主系统设置与快捷键速查", "主题 / 路径 / 目录同步", "workspace:settings", "core"},
        {"cmd:toggle_dock", "开关 F8 AI 伴读协作栏", "F8 · Magpie 路由 & 伴读对话", "toggle_dock", "core"},
        {"cmd:toggle_theme", "切换 Paper 亮色 / Graphite 暗色主题", "t · 即时切换全局视觉主题", "toggle_theme", "core"},
        {"cmd:shortcuts", "查看 OmniDesk 全键盘快捷键帮助", "? · Vim 导引与全局按键", "show_shortcuts", "core"},
        {"cmd:reindex", "立即重建跨三域 SQLite FTS5 全局索引", "刷新台词 / 书架 / 往期精选 / 乐评 / 生词", "reindex_search", "core"},
        {"cmd:media:play_pause", "播放 / 暂停当前媒体会话", "PlaybackCoordinator · MPRIS2", "playback:play_pause", "media"},
        {"cmd:media:resume_music", "恢复被视频打断的后台音乐队列", "断点恢复 PlaybackCoordinator", "playback:resume_music", "media"},
    };
    for (const auto& c : builtin_cmds) {
        batch.push_back(SearchDocument{
            c.id, "command", c.domain, "host.commands",
            c.title, c.sub, c.sub, "command 快捷命令",
            {{"target", "command"}, {"command", c.action}},
            now + 1000,
        });
    }

    // 2. DomainRegistry sources & plugin workspace tabs
    if (domains) {
        for (const auto& tab : domains->workspaceTabs()) {
            batch.push_back(SearchDocument{
                "cmd:tab:" + tab.tab_id,
                "command",
                toString(tab.domain),
                tab.plugin_id,
                "打开插件工作区: " + tab.title,
                tab.plugin_id + (tab.badge.empty() ? "" : (" · " + tab.badge)),
                tab.title,
                "plugin command 工作区",
                {{"target", "workspace_tab"}, {"tab_id", tab.tab_id}, {"plugin_id", tab.plugin_id}},
                now + 900,
            });
        }

        for (const auto& bsrc : domains->bookSources()) {
            try {
                const auto books = bsrc->listBooks("", "");
                for (const auto& b : books) {
                    batch.push_back(SearchDocument{
                        "book:" + b.source_id + ":" + b.id,
                        b.source_id == "plugin-ai-curator" ? "obsidian" : "book",
                        "book",
                        b.source_id,
                        b.title,
                        b.author + " · " + b.format + " · P." + std::to_string(b.current_page),
                        b.title + " " + b.author + " " + b.file_path,
                        b.format + " 书架 book",
                        {
                            {"target", "book"},
                            {"book_id", b.id},
                            {"source_id", b.source_id},
                            {"title", b.title},
                            {"file_path", b.file_path},
                            {"format", b.format},
                            {"page", std::max(1, b.current_page)},
                        },
                        now,
                    });
                }
                const auto vocab = bsrc->listVocab();
                for (const auto& vc : vocab) {
                    batch.push_back(SearchDocument{
                        "vocab:" + vc.word,
                        "vocab",
                        "book",
                        bsrc->sourceId(),
                        vc.word + (vc.phonetic.empty() ? "" : ("  " + vc.phonetic)),
                        vc.meaning,
                        vc.etymology + "\n" + vc.context_sentence,
                        "vocab sm2 生词 " + vc.source_book,
                        {
                            {"target", "vocab"},
                            {"word", vc.word},
                            {"source_book", vc.source_book},
                            {"context_sentence", vc.context_sentence},
                        },
                        now,
                    });
                }
            } catch (...) {
            }
        }

        for (const auto& tsrc : domains->textSources()) {
            try {
                const auto items = tsrc->fetchFeed("");
                const bool is_obsidian_source =
                    (tsrc->sourceId().find("curator") != std::string::npos ||
                     tsrc->sourceId().find("obsidian") != std::string::npos);
                for (const auto& item : items) {
                    std::string kind = is_obsidian_source ? "obsidian" : "article";
                    std::string tags_str = item.source_name + " " + item.category;
                    for (const auto& t : item.tags) {
                        tags_str += " " + t;
                        if (t.find("obsidian") != std::string::npos ||
                            t.find("往期") != std::string::npos ||
                            t.find("精选") != std::string::npos) {
                            kind = "obsidian";
                        }
                    }
                    batch.push_back(SearchDocument{
                        "text:" + item.source_id + ":" + item.id,
                        kind,
                        "text",
                        item.source_id,
                        item.title,
                        item.source_name + " · " + item.author + " · " + item.published_at,
                        item.excerpt,
                        tags_str,
                        {
                            {"target", "text"},
                            {"article_id", item.id},
                            {"source_id", item.source_id},
                            {"title", item.title},
                            {"url", item.url},
                            {"published_at", item.published_at},
                            {"category", item.category},
                            {"tags", item.tags},
                        },
                        now,
                    });
                }
            } catch (...) {
            }
        }

        for (const auto& msrc : domains->mediaSources()) {
            try {
                const auto items = msrc->fetchItems("all", "");
                for (const auto& item : items) {
                    const bool is_music =
                        (item.kind == "music" ||
                         item.source_id.find("music") != std::string::npos);
                    std::string review_body = item.creator + " " + item.quality_label;
                    if (item.extra.is_object()) {
                        if (item.extra.contains("analysis_summary") && item.extra["analysis_summary"].is_string()) {
                            review_body += "\n" + item.extra["analysis_summary"].get<std::string>();
                        }
                        if (item.extra.contains("listening_cue") && item.extra["listening_cue"].is_string()) {
                            review_body += "\n" + item.extra["listening_cue"].get<std::string>();
                        }
                    }
                    batch.push_back(SearchDocument{
                        "media:" + item.source_id + ":" + item.id,
                        is_music ? "music_review" : (item.source_id == "plugin-ai-curator" ? "obsidian" : "media"),
                        "media",
                        item.source_id,
                        item.title,
                        item.source_badge + " · " + item.creator,
                        review_body,
                        item.kind + " " + item.source_badge,
                        {
                            {"target", "media"},
                            {"item_id", item.id},
                            {"source_id", item.source_id},
                            {"title", item.title},
                            {"kind", item.kind},
                            {"play_url", item.play_url},
                            {"cover_url", item.cover_url},
                            {"creator", item.creator},
                            {"source_badge", item.source_badge},
                            {"quality_label", item.quality_label},
                            {"extra", item.extra},
                        },
                        now,
                    });
                }
            } catch (...) {
            }
        }
    }

    // 3. Host vocab fallback JSON
    if (!config_root.empty()) {
        std::error_code ec;
        for (const fs::path& vpath : {
                 fs::path(config_root) / "host_vocab_fallback.json",
                 fs::path(config_root) / "plugins" / "host-book-vocab.json",
             }) {
            if (!fs::exists(vpath, ec)) continue;
            auto vj = readJsonFileQuiet(vpath);
            const nlohmann::json* arr = nullptr;
            if (vj.is_array()) arr = &vj;
            else if (vj.is_object() && vj.contains("cards") && vj["cards"].is_array()) arr = &vj["cards"];
            if (arr) {
                for (const auto& c : *arr) {
                    if (!c.is_object()) continue;
                    const std::string word = c.value("word", "");
                    if (word.empty()) continue;
                    batch.push_back(SearchDocument{
                        "vocab:" + word,
                        "vocab",
                        "book",
                        "host.vocab_fallback",
                        word + (c.value("phonetic", "").empty() ? "" : ("  " + c.value("phonetic", ""))),
                        c.value("meaning", ""),
                        c.value("etymology", "") + "\n" + c.value("context_sentence", ""),
                        "vocab sm2 生词 " + c.value("source_book", ""),
                        {
                            {"target", "vocab"},
                            {"word", word},
                            {"source_book", c.value("source_book", "")},
                            {"context_sentence", c.value("context_sentence", "")},
                        },
                        now,
                    });
                }
            }
        }
    }

    // 4. Local Obsidian / Music-Daily curation archives under data_root
    if (!data_root.empty()) {
        std::error_code ec;
        const fs::path curation_root = fs::path(data_root) / "curation";
        const fs::path music_daily_dirs[] = {
            curation_root / "repos" / "music-daily" / "recommendations",
            curation_root / "repos" / "obsidian-vault" / "Music" / "Daily" / "manifests",
        };
        size_t music_files_scanned = 0;
        for (const auto& dir : music_daily_dirs) {
            if (!fs::exists(dir, ec)) continue;
            for (const auto& entry : fs::recursive_directory_iterator(dir, ec)) {
                if (music_files_scanned >= 200) break;
                if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
                ++music_files_scanned;
                auto mj = readJsonFileQuiet(entry.path());
                if (!mj.is_object() || !mj.contains("tracks") || !mj["tracks"].is_array()) continue;
                const std::string date_str = mj.value("date", entry.path().stem().string());
                for (const auto& tr : mj["tracks"]) {
                    if (!tr.is_object()) continue;
                    const std::string title = tr.value("title", "");
                    const std::string artist = tr.value("artist", "");
                    if (title.empty()) continue;
                    const std::string cid = tr.value("canonical_id", date_str + ":" + title);
                    std::string body = tr.value("analysis_summary", "");
                    if (tr.contains("listening_cue") && tr["listening_cue"].is_string()) {
                        body += "\n" + tr["listening_cue"].get<std::string>();
                    }
                    std::string apple_url;
                    if (tr.contains("apple_music") && tr["apple_music"].is_object()) {
                        apple_url = tr["apple_music"].value("url", "");
                    }
                    batch.push_back(SearchDocument{
                        "music_review:" + date_str + ":" + cid,
                        "music_review",
                        "media",
                        "music-daily",
                        title + (artist.empty() ? "" : (" - " + artist)),
                        "每日乐评 · " + date_str + " · " + tr.value("album", ""),
                        body,
                        "music daily 乐评 " + date_str + " " + artist,
                        {
                            {"target", "media"},
                            {"item_id", cid},
                            {"source_id", "plugin-music"},
                            {"title", title},
                            {"creator", artist},
                            {"kind", "music"},
                            {"url", apple_url},
                            {"date", date_str},
                        },
                        now,
                    });
                }
            }
        }

        const fs::path obsidian_dirs[] = {
            curation_root / "repos" / "obsidian-vault" / "News" / "Daily",
            curation_root / "repos" / "obsidian-vault" / "Resources" / "Reddit" / "Daily",
            curation_root / "repos" / "obsidian-vault" / "Resources" / "YouTube" / "Daily",
            curation_root / "repos" / "obsidian-vault" / "00-inbox" / "Journal",
            curation_root / "repos" / "obsidian-vault" / "50-courses" / "English",
        };
        size_t obs_scanned = 0;
        for (const auto& dir : obsidian_dirs) {
            if (!fs::exists(dir, ec)) continue;
            for (const auto& entry : fs::recursive_directory_iterator(dir, ec)) {
                if (obs_scanned >= 300) break;
                if (!entry.is_regular_file() || entry.path().extension() != ".md") continue;
                ++obs_scanned;
                const std::string rel = fs::relative(entry.path(), curation_root / "repos" / "obsidian-vault", ec).string();
                const std::string text = readTextHead(entry.path(), 4096);
                const std::string title = entry.path().stem().string() + " · " + entry.path().parent_path().filename().string();
                batch.push_back(SearchDocument{
                    "obsidian:" + rel,
                    "obsidian",
                    "text",
                    "obsidian-vault",
                    title,
                    rel,
                    text,
                    "obsidian 往期精选 " + rel,
                    {
                        {"target", "text"},
                        {"article_id", "obsidian:" + rel},
                        {"source_id", "plugin-ai-curator"},
                        {"title", title},
                        {"file_path", entry.path().string()},
                    },
                    now,
                });
            }
        }
    }

    // 5. Optional live vocab.sm2.v1 deck if no BookSource provided vocab
    if (caps && caps->has("vocab.sm2.v1")) {
        try {
            auto vres = caps->call("vocab.sm2.v1", {{"action", "list"}, {"limit", 300}});
            if (vres.ok && vres.data.is_object()) {
                const nlohmann::json* arr = nullptr;
                if (vres.data.contains("cards") && vres.data["cards"].is_array()) arr = &vres.data["cards"];
                else if (vres.data.contains("words") && vres.data["words"].is_array()) arr = &vres.data["words"];
                if (arr) {
                    for (const auto& c : *arr) {
                        if (!c.is_object()) continue;
                        const std::string word = c.value("word", "");
                        if (word.empty()) continue;
                        batch.push_back(SearchDocument{
                            "vocab:" + word,
                            "vocab",
                            "book",
                            vres.provider_plugin_id.empty() ? "plugin-ebook" : vres.provider_plugin_id,
                            word + (c.value("phonetic", "").empty() ? "" : ("  " + c.value("phonetic", ""))),
                            c.value("meaning", ""),
                            c.value("etymology", "") + "\n" + c.value("context_sentence", ""),
                            "vocab sm2 生词 " + c.value("book_title", c.value("source_book", "")),
                            {
                                {"target", "vocab"},
                                {"word", word},
                                {"source_book", c.value("book_title", c.value("source_book", ""))},
                                {"context_sentence", c.value("context_sentence", "")},
                            },
                            now,
                        });
                    }
                }
            }
        } catch (...) {
        }
    }

    return upsertBatch(batch);
}

void SearchIndexService::registerCapability(CapabilityRegistry& caps,
                                            DomainRegistry* domains,
                                            std::string data_root,
                                            std::string config_root) {
    caps.registerCapability(
        "search.index.v1",
        "host.search",
        [this, &caps, domains, data_root = std::move(data_root), config_root = std::move(config_root)](
            const nlohmann::json& req) -> CapabilityResult {
            if (!req.is_object()) {
                return {false, {{"error_code", "invalid_argument"}}, "request must be an object", "host.search", false};
            }
            const std::string action = req.value("action", "query");
            if (action == "query" || action == "search") {
                SearchQueryOptions opts;
                opts.query = req.value("q", req.value("query", ""));
                opts.kind = req.value("kind", "");
                opts.domain = req.value("domain", "");
                opts.limit = static_cast<size_t>(std::clamp(req.value("limit", 30), 1, 200));

                const auto t0 = std::chrono::steady_clock::now();
                auto hits = search(opts);

                // Live NAS subtitle clip augmentation when query >= 2 chars & nas_clip requested/all
                if (!opts.query.empty() &&
                    (opts.kind.empty() || opts.kind == "nas_clip") &&
                    caps.has("media.clip_jump.v1")) {
                    auto clip_res = caps.call("media.clip_jump.v1", {
                        {"query", opts.query},
                        {"limit", 6},
                        {"auto_open_player", false},
                    });
                    if (clip_res.ok && clip_res.data.is_object() &&
                        clip_res.data.contains("clips") && clip_res.data["clips"].is_array()) {
                        std::vector<SearchDocument> clip_docs;
                        for (const auto& clip : clip_res.data["clips"]) {
                            if (!clip.is_object()) continue;
                            const std::string series = clip.value("series", clip.value("title", "NAS 视频"));
                            const std::string ep = clip.value("episode", "");
                            const double start_sec = clip.value("start_sec", 0.0);
                            const double end_sec = clip.value("end_sec", start_sec + 5.0);
                            const std::string line_en = clip.value("line_en", "");
                            const std::string line_zh = clip.value("line_zh", "");
                            const std::string play_url = clip.value("play_url", "");
                            const std::string did = "nas_clip:" + series + ":" +
                                                    std::to_string(static_cast<int>(start_sec * 10));
                            SearchDocument doc;
                            doc.doc_id = did;
                            doc.kind = "nas_clip";
                            doc.domain = "media";
                            doc.source_id = "plugin-fnos-nas";
                            doc.title = series + (ep.empty() ? "" : (" " + ep)) + " · " +
                                        std::to_string(static_cast<int>(start_sec)) + "s";
                            doc.subtitle = line_en;
                            doc.body = line_en + "\n" + line_zh;
                            doc.tags = "nas clip 台词 " + series;
                            doc.payload = {
                                {"target", "nas_clip"},
                                {"item_id", did},
                                {"source_id", "plugin-fnos-nas"},
                                {"title", series + (ep.empty() ? "" : (" " + ep))},
                                {"kind", "nas"},
                                {"play_url", play_url},
                                {"start_sec", start_sec},
                                {"end_sec", end_sec},
                            };
                            clip_docs.push_back(doc);

                            bool dup = false;
                            for (const auto& existing : hits) {
                                if (existing.doc_id == did) {
                                    dup = true;
                                    break;
                                }
                            }
                            if (!dup && hits.size() < opts.limit) {
                                hits.push_back(SearchHit{
                                    doc.doc_id, doc.kind, doc.domain, doc.source_id,
                                    doc.title, doc.subtitle,
                                    line_zh.empty() ? line_en : (line_en + " / " + line_zh),
                                    5.0, doc.payload
                                });
                            }
                        }
                        if (!clip_docs.empty()) {
                            upsertBatch(clip_docs);
                        }
                    }
                }

                const double ms = std::chrono::duration<double, std::milli>(
                                      std::chrono::steady_clock::now() - t0)
                                      .count();
                nlohmann::json arr = nlohmann::json::array();
                for (const auto& h : hits) {
                    arr.push_back({
                        {"doc_id", h.doc_id},
                        {"kind", h.kind},
                        {"kind_label", kindDisplayLabel(h.kind)},
                        {"domain", h.domain},
                        {"source_id", h.source_id},
                        {"title", h.title},
                        {"subtitle", h.subtitle},
                        {"snippet", h.snippet},
                        {"score", h.score},
                        {"payload", h.payload},
                    });
                }
                return {true, {
                    {"hits", std::move(arr)},
                    {"total", hits.size()},
                    {"elapsed_ms", ms},
                }, "", "host.search", false};
            }

            if (action == "upsert") {
                std::vector<SearchDocument> docs;
                auto parseDoc = [](const nlohmann::json& j) -> SearchDocument {
                    SearchDocument d;
                    d.doc_id = j.value("doc_id", "");
                    d.kind = j.value("kind", "article");
                    d.domain = j.value("domain", "text");
                    d.source_id = j.value("source_id", "");
                    d.title = j.value("title", "");
                    d.subtitle = j.value("subtitle", "");
                    d.body = j.value("body", "");
                    d.tags = j.value("tags", "");
                    if (j.contains("payload") && j["payload"].is_object()) {
                        d.payload = j["payload"];
                    }
                    d.updated_at = j.value("updated_at", int64_t{0});
                    return d;
                };
                if (req.contains("docs") && req["docs"].is_array()) {
                    for (const auto& j : req["docs"]) {
                        if (j.is_object()) docs.push_back(parseDoc(j));
                    }
                } else if (req.contains("doc") && req["doc"].is_object()) {
                    docs.push_back(parseDoc(req["doc"]));
                } else if (req.contains("doc_id")) {
                    docs.push_back(parseDoc(req));
                }
                const size_t n = upsertBatch(docs);
                return {true, {{"upserted", n}}, "", "host.search", false};
            }

            if (action == "remove") {
                const std::string doc_id = req.value("doc_id", "");
                if (doc_id.empty()) {
                    return {false, {{"error_code", "invalid_argument"}}, "doc_id required", "host.search", false};
                }
                bool ok = remove(doc_id);
                return {ok, {{"removed", ok}}, "", "host.search", false};
            }

            if (action == "clear_source") {
                const std::string sid = req.value("source_id", "");
                if (sid.empty()) {
                    return {false, {{"error_code", "invalid_argument"}}, "source_id required", "host.search", false};
                }
                bool ok = clearSource(sid);
                return {ok, {{"cleared", ok}}, "", "host.search", false};
            }

            if (action == "reindex") {
                const size_t n = hydrateEcosystem(domains, &caps, data_root, config_root);
                auto st = stats();
                return {true, {
                    {"indexed", n},
                    {"total_docs", st.total_docs},
                    {"by_kind", st.by_kind},
                }, "", "host.search", false};
            }

            if (action == "stats") {
                auto st = stats();
                return {true, {
                    {"total_docs", st.total_docs},
                    {"by_kind", st.by_kind},
                    {"db_path", st.db_path},
                }, "", "host.search", false};
            }

            return {false, {{"error_code", "invalid_argument"}}, "Unsupported search.index.v1 action: " + action, "host.search", false};
        },
        {{"name", "跨三域 SQLite FTS5 全局检索服务"}, {"version", "1.0.0"}},
        100);
}

} // namespace omnidesk
