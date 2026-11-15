#include "ai_curator/curation_engine.hpp"

#include "ai_curator/utils.hpp"
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <omnidesk/types_json.hpp>
#include <sstream>

namespace fs = std::filesystem;

namespace ai_curator {
namespace {

omnidesk::CapabilityResult makeError(const std::string& code, const std::string& message) {
    omnidesk::CapabilityResult res;
    res.ok = false;
    res.error = message;
    res.data = {
        {"error_code", code},
        {"message", message},
    };
    return res;
}

void parseQueryFilter(const std::string& raw_query, std::string& out_date, std::string& out_kw) {
    out_date.clear();
    out_kw.clear();
    std::istringstream iss(raw_query);
    std::string tok;
    while (iss >> tok) {
        if (startsWith(tok, "date:") && tok.size() == 15 && isValidIsoDate(tok.substr(5))) {
            out_date = tok.substr(5);
        } else {
            if (!out_kw.empty()) out_kw += " ";
            out_kw += tok;
        }
    }
    out_kw = toLowerAscii(trim(out_kw));
}

bool mediaMatchesFilter(const omnidesk::MediaItem& item,
                        const std::string& date_filter,
                        const std::string& kw_lower) {
    if (!date_filter.empty()) {
        std::string item_date = item.extra.value("business_date", "");
        if (item_date != date_filter) return false;
    }
    if (kw_lower.empty()) return true;
    std::string hay = toLowerAscii(
        item.title + " " + item.creator + " " + item.source_badge + " " +
        item.extra.value("artist", "") + " " + item.extra.value("album", "") + " " +
        item.extra.value("topic", "") + " " + item.extra.value("why_recommended", "") + " " +
        item.extra.value("listening_or_reading_cue", "") + " " +
        item.extra.value("business_date", ""));
    return hay.find(kw_lower) != std::string::npos;
}

bool textMatchesFilter(const omnidesk::TextFeedItem& item,
                       const std::string& date_filter,
                       const std::string& kw_lower) {
    if (!date_filter.empty() && item.published_at != date_filter) return false;
    if (kw_lower.empty()) return true;
    std::string hay = toLowerAscii(item.title + " " + item.excerpt + " " + item.author + " " +
                                   item.source_name + " " + item.published_at + " " + item.category);
    for (const auto& t : item.tags) {
        hay += " " + toLowerAscii(t);
    }
    return hay.find(kw_lower) != std::string::npos;
}

omnidesk::MediaItem parseIncomingMediaItem(const nlohmann::json& j, const std::string& default_mode) {
    omnidesk::MediaItem item;
    omnidesk::fromJson(j, item);
    if (item.source_id.empty()) item.source_id = "plugin-ai-curator";
    if (item.creator.empty() && j.contains("subtitle") && j["subtitle"].is_string()) {
        item.creator = j["subtitle"].get<std::string>();
    }
    if (!item.extra.is_object()) item.extra = nlohmann::json::object();
    if (!item.extra.contains("curation_mode")) {
        item.extra["curation_mode"] = default_mode;
    }
    if (item.extra.value("apple_music_song_id", "").empty() && !item.play_url.empty()) {
        std::string sid = extractAppleMusicSongId(item.play_url);
        if (!sid.empty()) item.extra["apple_music_song_id"] = sid;
    }
    if (item.extra.value("video_id", "").empty() && !item.play_url.empty()) {
        std::string vid = extractYouTubeVideoId(item.play_url);
        if (!vid.empty()) item.extra["video_id"] = vid;
    }
    return item;
}

void parseIncomingTextItem(const nlohmann::json& j,
                           const std::string& default_category,
                           omnidesk::TextFeedItem& out_feed,
                           omnidesk::TextArticle& out_art) {
    omnidesk::fromJson(j, out_feed);
    if (out_feed.source_id.empty()) out_feed.source_id = "plugin-ai-curator";
    if (out_feed.category.empty()) out_feed.category = default_category;
    if (out_feed.source_name.empty()) {
        out_feed.source_name = (out_feed.category == "interactive") ? "AI 灵感阅读" : "AI 每日精选";
    }
    if (out_feed.published_at.empty() && j.contains("extra") && j["extra"].is_object()) {
        out_feed.published_at = j["extra"].value("business_date", "");
    }
    if (out_feed.id.empty()) {
        out_feed.id = "txt-" + sanitizeFilenameComponent(out_feed.title);
    }

    std::string why;
    std::string cue;
    if (j.contains("extra") && j["extra"].is_object()) {
        const auto& ex = j["extra"];
        why = ex.value("why_recommended", "");
        cue = ex.value("listening_or_reading_cue", "");
    }
    if (!why.empty() && out_feed.excerpt == why) {
        out_feed.excerpt.clear();
    }

    if (j.contains("article") && j["article"].is_object()) {
        omnidesk::fromJson(j["article"], out_art);
    } else {
        out_art.id = out_feed.id;
        out_art.source_name = out_feed.source_name;
        out_art.category = out_feed.category;
        out_art.title = out_feed.title;
        out_art.subtitle = j.value("subtitle", out_feed.source_name + " · " + out_feed.published_at);
        out_art.authors = j.value("authors", out_feed.author);
        out_art.published_at = out_feed.published_at;
        out_art.url = out_feed.url;
        out_art.doi = j.value("doi", "");
        if (out_art.doi.empty() && j.contains("extra") && j["extra"].is_object()) {
            out_art.doi = j["extra"].value("doi", "");
        }
        out_art.tags = out_feed.tags;
        if (j.contains("paragraphs") && j["paragraphs"].is_array()) {
            out_art.paragraphs = omnidesk::fromJsonArray<omnidesk::TextParagraph>(j["paragraphs"]);
        } else {
            int pidx = 1;
            if (!out_feed.excerpt.empty()) {
                out_art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", out_feed.excerpt, ""});
            }
            if (!why.empty()) {
                out_art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Why Recommended", ""});
                out_art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", why, ""});
            }
            if (!cue.empty()) {
                out_art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Reading Cue", ""});
                out_art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote", cue, ""});
            }
        }
    }
    if (out_art.id.empty()) out_art.id = out_feed.id;
}

bool validatePublicationEvidence(const nlohmann::json& req,
                                 const std::string& operation_id,
                                 const std::string& business_date,
                                 nlohmann::json& evidence_out,
                                 std::string& error) {
    nlohmann::json evidence = nlohmann::json::object();
    if (req.contains("publication_evidence") && req["publication_evidence"].is_object()) {
        evidence = req["publication_evidence"];
    } else {
        for (const char* key : {"publication_verified", "receipt", "status", "operation_id",
                                "project", "business_date"}) {
            if (req.contains(key)) evidence[key] = req[key];
        }
    }
    if (!evidence.is_object() || evidence.empty()) {
        error = "publication_evidence is required; qa_verified alone is not publication proof";
        return false;
    }
    if (evidence.value("status", "") != "PUBLISHED") {
        error = "publication_evidence.status must be PUBLISHED";
        return false;
    }
    if (!evidence.contains("publication_verified") ||
        !evidence["publication_verified"].is_boolean() ||
        !evidence["publication_verified"].get<bool>()) {
        error = "publication_evidence.publication_verified must be true";
        return false;
    }
    if (evidence.value("operation_id", "") != operation_id) {
        error = "publication_evidence.operation_id does not match " + operation_id;
        return false;
    }
    if (evidence.value("business_date", "") != business_date) {
        error = "publication_evidence.business_date does not match " + business_date;
        return false;
    }
    const std::string project = evidence.value("project", "");
    if (project.empty() || operation_id.rfind(project + ":", 0) != 0) {
        error = "publication_evidence.project does not match operation_id";
        return false;
    }
    if (!evidence.contains("receipt")) {
        error = "publication_evidence.receipt is required";
        return false;
    }
    const auto& receipt = evidence["receipt"];
    bool receipt_present = false;
    if (receipt.is_string()) {
        receipt_present = !trim(receipt.get<std::string>()).empty();
    } else if (receipt.is_object()) {
        for (const char* key : {"id", "path", "bundle_sha256", "manifest_blob", "receipt_id"}) {
            if (receipt.contains(key) && receipt[key].is_string() &&
                !trim(receipt[key].get<std::string>()).empty()) {
                receipt_present = true;
                break;
            }
        }
    }
    if (!receipt_present) {
        error = "publication_evidence.receipt must identify the committed PUBLISHED artifact";
        return false;
    }
    evidence_out = std::move(evidence);
    return true;
}

std::string mediaSessionKey(const omnidesk::MediaItem& item) {
    const std::string song = item.extra.value("apple_music_song_id", "");
    if (!song.empty()) return "am:" + song;
    const std::string video = item.extra.value("video_id", "");
    if (!video.empty()) return "video:" + video;
    if (!item.play_url.empty()) return "url:" + item.play_url;
    return "title:" + toLowerAscii(trim(item.title)) + "|" + toLowerAscii(trim(item.creator));
}

std::string textSessionKey(const omnidesk::TextFeedItem& item,
                           const omnidesk::TextArticle& article) {
    if (!article.doi.empty()) return "doi:" + toLowerAscii(trim(article.doi));
    if (!item.url.empty()) return "url:" + item.url;
    return "title:" + toLowerAscii(trim(item.title));
}

std::string bookSessionKey(const omnidesk::BookItem& item) {
    if (!item.id.empty()) return "id:" + item.id;
    return "title:" + toLowerAscii(trim(item.title)) + "|" + toLowerAscii(trim(item.author));
}

} // namespace

CurationEngine::CurationEngine(omnidesk::PluginContext& ctx) : ctx_(ctx) {
    scheduler_ = std::make_unique<DailyWorkflowScheduler>(
        ctx_,
        [this](const std::string&, const std::string&, const RepoPaths& paths) {
            rehydrateLocalIncremental(paths);
        });
}

void CurationEngine::initializeOnLoad() {
    std::vector<omnidesk::VocabCard> loaded_vocabs;
    bool should_bg_pull = false;
    RepoPaths init_paths;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        last_paths_ = GitMirror::resolveRepoPaths(&ctx_);
        init_paths = last_paths_;
        std::error_code ec;
        fs::create_directories(last_paths_.curation_dir, ec);

        auto cfg = ctx_.loadConfig();
        const bool auto_rehydrate = cfg.value("auto_rehydrate_on_load", true);
        const bool auto_pull = cfg.value("auto_pull_remote", false);

        ArchiveRehydrator::loadCachedIndex(last_paths_.curation_dir, archive_);
        loadPersistedLocalStateLocked(last_paths_.curation_dir);

        if (auto_rehydrate) {
            if (auto_pull && ctx_.jobs()) {
                should_bg_pull = true;
            } else if (fs::exists(last_paths_.obsidian_vault_dir, ec) ||
                       fs::exists(last_paths_.music_daily_dir, ec)) {
                std::string err;
                bool cache_hit = false;
                ArchiveRehydrator::rehydrate(last_paths_.obsidian_vault_dir,
                                             last_paths_.music_daily_dir,
                                             last_paths_.curation_dir,
                                             false,
                                             archive_,
                                             cache_hit,
                                             err);
                last_cache_hit_ = cache_hit;
                last_rehydrate_time_ = currentShanghaiDate();
            }
        }
        loaded_vocabs = archive_.vocab_cards;
    }

    if (scheduler_) {
        scheduler_->initialize(init_paths);
    }

    if (should_bg_pull && ctx_.jobs()) {
        omnidesk::JobSpec spec;
        spec.title = "同步并回灌 AI 策展历史归档";
        spec.plugin_id = "plugin-ai-curator";
        spec.kind = "curation.rehydrate";
        ctx_.submitJob(std::move(spec),
                       [this](omnidesk::JobHandle& job, std::string& error) -> bool {
                           if (job.isCancelled()) return false;
                           job.reportProgress(0.2f, "正在同步双仓并回灌历史归档…");
                           auto res = actionBootstrapAndRehydrate({
                               {"pull_remote", true},
                               {"clone_if_missing", false},
                           });
                           if (!res.ok) {
                               error = res.error;
                               return false;
                           }
                           job.reportProgress(1.0f, "历史归档回灌完成");
                           return true;
                       });
    }
    (void)loaded_vocabs;
}

void CurationEngine::shutdown() {
    if (scheduler_) {
        scheduler_->shutdown();
    }
}

void CurationEngine::rehydrateLocalIncremental(const RepoPaths& paths) {
    HydratedArchive hydrated;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (last_paths_.obsidian_vault_dir == paths.obsidian_vault_dir &&
            last_paths_.music_daily_dir == paths.music_daily_dir &&
            !archive_.date_stats.empty()) {
            hydrated = archive_;
        }
    }
    bool cache_hit = false;
    std::string err;
    ArchiveRehydrator::rehydrate(paths.obsidian_vault_dir,
                                 paths.music_daily_dir,
                                 paths.curation_dir,
                                 false,
                                 hydrated,
                                 cache_hit,
                                 err);
    std::vector<omnidesk::VocabCard> vocabs_to_sync;
    int dates_indexed = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        last_paths_ = paths;
        last_cache_hit_ = cache_hit;
        last_rehydrate_time_ = currentShanghaiDate();
        archive_ = std::move(hydrated);
        loadPersistedLocalStateLocked(paths.curation_dir);
        vocabs_to_sync = archive_.vocab_cards;
        dates_indexed = archive_.dates_indexed;
    }
    syncVocabToSm2Idempotent(vocabs_to_sync);
    ctx_.publish("entity.recommend.updated", {
        {"source", "daily_scheduler"},
        {"dates_indexed", dates_indexed},
    });
}

CurationEngine::InteractiveSession CurationEngine::parseInteractiveSession(
    const nlohmann::json& json,
    const std::string& fallback_id) const {
    InteractiveSession session;
    session.session_id = json.value("session_id", fallback_id);
    session.title = json.value("title", "");
    session.user_prompt = json.value("user_prompt", "");
    session.repeat_policy = json.value("repeat_policy", "allow_any");
    if (json.contains("media_items") && json["media_items"].is_array()) {
        for (const auto& item : json["media_items"]) {
            if (item.is_object()) {
                session.media_items.push_back(parseIncomingMediaItem(item, "interactive_ephemeral"));
            }
        }
    }
    if (json.contains("text_items") && json["text_items"].is_array()) {
        for (const auto& item : json["text_items"]) {
            if (!item.is_object()) continue;
            omnidesk::TextFeedItem feed;
            omnidesk::TextArticle article;
            parseIncomingTextItem(item, "interactive", feed, article);
            session.text_items.push_back(std::move(feed));
            if (!article.id.empty()) session.articles[article.id] = std::move(article);
        }
    }
    if (json.contains("articles") && json["articles"].is_object()) {
        for (auto it = json["articles"].begin(); it != json["articles"].end(); ++it) {
            if (!it.value().is_object()) continue;
            omnidesk::TextArticle article;
            omnidesk::fromJson(it.value(), article);
            if (article.id.empty()) article.id = it.key();
            session.articles[article.id] = std::move(article);
        }
    }
    if (json.contains("book_items") && json["book_items"].is_array()) {
        session.book_items = omnidesk::fromJsonArray<omnidesk::BookItem>(json["book_items"]);
    }
    if (json.contains("book_spreads") && json["book_spreads"].is_object()) {
        for (auto it = json["book_spreads"].begin(); it != json["book_spreads"].end(); ++it) {
            if (!it.value().is_object()) continue;
            omnidesk::BookSpread spread;
            omnidesk::fromJson(it.value(), spread);
            if (spread.book_id.empty()) spread.book_id = it.key();
            session.book_spreads[spread.book_id] = std::move(spread);
        }
    }
    return session;
}

nlohmann::json CurationEngine::interactiveSessionToJson(const InteractiveSession& session) const {
    nlohmann::json articles = nlohmann::json::object();
    for (const auto& [id, article] : session.articles) {
        articles[id] = omnidesk::toJson(article);
    }
    nlohmann::json spreads = nlohmann::json::object();
    for (const auto& [id, spread] : session.book_spreads) {
        spreads[id] = omnidesk::toJson(spread);
    }
    return {
        {"schema_version", 2},
        {"mode", "interactive_ephemeral"},
        {"session_id", session.session_id},
        {"title", session.title},
        {"user_prompt", session.user_prompt},
        {"repeat_policy", session.repeat_policy},
        {"record_permanent_history", false},
        {"media_items", omnidesk::toJsonArray(session.media_items)},
        {"text_items", omnidesk::toJsonArray(session.text_items)},
        {"articles", std::move(articles)},
        {"book_items", omnidesk::toJsonArray(session.book_items)},
        {"book_spreads", std::move(spreads)},
    };
}

void CurationEngine::loadPersistedLocalStateLocked(const std::string& curation_dir) {
    daily_media_.clear();
    daily_texts_.clear();
    daily_articles_.clear();
    daily_books_.clear();
    daily_vocabs_.clear();
    interactive_sessions_.clear();
    pinned_ids_.clear();
    dismissed_ids_.clear();

    std::error_code ec;
    const fs::path daily_dir = fs::path(curation_dir) / "daily";
    if (fs::exists(daily_dir, ec)) {
        std::vector<fs::path> files;
        for (const auto& entry : fs::recursive_directory_iterator(daily_dir, ec)) {
            if (entry.is_regular_file() && entry.path().extension() == ".json") {
                files.push_back(entry.path());
            }
        }
        std::sort(files.begin(), files.end());
        for (const auto& fp : files) {
            auto jopt = readJsonFile(fp.string());
            if (!jopt.has_value() || !jopt->is_object()) continue;
            const auto& j = *jopt;
            if (j.contains("media_items") && j["media_items"].is_array()) {
                for (const auto& mj : j["media_items"]) {
                    daily_media_.push_back(parseIncomingMediaItem(mj, "daily_immutable"));
                }
            }
            if (j.contains("text_items") && j["text_items"].is_array()) {
                for (const auto& tj : j["text_items"]) {
                    omnidesk::TextFeedItem fi;
                    omnidesk::TextArticle art;
                    parseIncomingTextItem(tj, "news", fi, art);
                    daily_texts_.push_back(fi);
                    daily_articles_[art.id] = std::move(art);
                }
            }
            if (j.contains("book_items") && j["book_items"].is_array()) {
                auto books = omnidesk::fromJsonArray<omnidesk::BookItem>(j["book_items"]);
                daily_books_.insert(daily_books_.end(), books.begin(), books.end());
            }
            if (j.contains("vocab_cards") && j["vocab_cards"].is_array()) {
                auto vcs = omnidesk::fromJsonArray<omnidesk::VocabCard>(j["vocab_cards"]);
                daily_vocabs_.insert(daily_vocabs_.end(), vcs.begin(), vcs.end());
            }
        }
    }

    const fs::path ix_dir = fs::path(curation_dir) / "interactive" / "sessions";
    if (fs::exists(ix_dir, ec)) {
        std::vector<fs::path> files;
        for (const auto& entry : fs::directory_iterator(ix_dir, ec)) {
            if (entry.is_regular_file() && entry.path().extension() == ".json") {
                files.push_back(entry.path());
            }
        }
        std::sort(files.begin(), files.end());
        for (const auto& fp : files) {
            auto jopt = readJsonFile(fp.string());
            if (!jopt.has_value() || !jopt->is_object()) continue;
            interactive_sessions_.push_back(parseInteractiveSession(*jopt, fp.stem().string()));
        }
    }

    if (auto pjson = readJsonFile((fs::path(curation_dir) / "interactive" / "pinned.json").string())) {
        if (pjson->contains("ids") && (*pjson)["ids"].is_array()) {
            for (const auto& id : (*pjson)["ids"]) {
                if (id.is_string()) pinned_ids_.insert(id.get<std::string>());
            }
        }
    }
    if (auto djson = readJsonFile((fs::path(curation_dir) / "interactive" / "dismissed.json").string())) {
        if (djson->contains("ids") && (*djson)["ids"].is_array()) {
            for (const auto& id : (*djson)["ids"]) {
                if (id.is_string()) dismissed_ids_.insert(id.get<std::string>());
            }
        }
    }
}

void CurationEngine::syncVocabToSm2Idempotent(const std::vector<omnidesk::VocabCard>& cards) {
    if (cards.empty()) return;
    if (!ctx_.capabilities().has("vocab.sm2.v1")) return;

    std::unordered_set<std::string> existing_words;
    auto list_res = ctx_.capabilities().call("vocab.sm2.v1", {{"action", "list"}, {"limit", 10000}});
    if (list_res.ok && list_res.data.is_object()) {
        const nlohmann::json* arr = nullptr;
        if (list_res.data.contains("cards") && list_res.data["cards"].is_array()) arr = &list_res.data["cards"];
        else if (list_res.data.contains("items") && list_res.data["items"].is_array()) arr = &list_res.data["items"];
        else if (list_res.data.contains("vocab") && list_res.data["vocab"].is_array()) arr = &list_res.data["vocab"];
        if (arr) {
            for (const auto& item : *arr) {
                if (item.is_object()) {
                    std::string w = toLowerAscii(trim(item.value("word", "")));
                    if (!w.empty()) existing_words.insert(w);
                }
            }
        }
    }

    // Also track locally pushed words so even if vocab.sm2.v1 lacks "list", we stay idempotent
    std::string pushed_path;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        pushed_path = (fs::path(last_paths_.curation_dir) / "vocab_pushed.json").string();
    }
    if (auto pjson = readJsonFile(pushed_path)) {
        if (pjson->contains("words") && (*pjson)["words"].is_array()) {
            for (const auto& w : (*pjson)["words"]) {
                if (w.is_string()) existing_words.insert(toLowerAscii(w.get<std::string>()));
            }
        }
    }

    bool pushed_any = false;
    for (const auto& c : cards) {
        std::string lw = toLowerAscii(trim(c.word));
        if (lw.empty() || existing_words.count(lw) > 0) continue;
        auto add_res = ctx_.capabilities().call("vocab.sm2.v1", {
            {"action", "add"},
            {"word", c.word},
            {"lemma", c.lemma},
            {"phonetic", c.phonetic},
            {"etymology", c.etymology},
            {"meaning", c.meaning},
            {"context_sentence", c.context_sentence},
            {"source_book", c.source_book},
        });
        if (add_res.ok) {
            existing_words.insert(lw);
            pushed_any = true;
        }
    }
    if (pushed_any && !pushed_path.empty()) {
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& w : existing_words) arr.push_back(w);
        writeJsonFileAtomic(pushed_path, {{"schema_version", 1}, {"words", std::move(arr)}}, -1);
    }
}

omnidesk::CapabilityResult CurationEngine::handleRecommendFeed(const nlohmann::json& req) {
    if (!req.is_object()) {
        return makeError("invalid_request", "Request payload must be a JSON object.");
    }
    const std::string action = req.value("action", "status");
    if (action == "bootstrap_and_rehydrate") return actionBootstrapAndRehydrate(req);
    if (action == "list_archive_dates") return actionListArchiveDates(req);
    if (action == "search_archive") return actionSearchArchive(req);
    if (action == "lookup_provenance") return actionLookupProvenance(req);
    if (action == "get_date_bundle") return actionGetDateBundle(req);
    if (action == "publish_daily_bundle") return actionPublishDailyBundle(req);
    if (action == "push_interactive_set") return actionPushInteractiveSet(req);
    if (action == "pin_item") return actionPinItem(req);
    if (action == "dismiss_item") return actionDismissItem(req);
    if (action == "clear_interactive") return actionClearInteractive(req);
    if (action == "status") return actionStatus(req);
    if (action == "daily_status") return actionDailyStatus(req);
    if (action == "configure_daily_scheduler") return actionConfigureDailyScheduler(req);
    if (action == "run_daily_workflows" || action == "start_daily_workflows") {
        return actionRunDailyWorkflows(req);
    }
    if (action == "cancel_daily_workflows") return actionCancelDailyWorkflows(req);
    return makeError("unknown_action", "Unsupported recommend.feed.v1 action: " + action);
}

omnidesk::CapabilityResult CurationEngine::handleAiCuration(const nlohmann::json& req) {
    if (!req.is_object()) {
        return makeError("invalid_request", "Request payload must be a JSON object.");
    }
    const std::string action = req.value("action", "daily_status");
    if (action == "daily_status") return actionDailyStatus(req);
    if (action == "configure_daily_scheduler") return actionConfigureDailyScheduler(req);
    if (action == "run_daily_pipeline" || action == "run_daily_workflows" ||
        action == "start_daily_workflows") {
        return actionRunDailyWorkflows(req);
    }
    if (action == "cancel_daily_workflows") return actionCancelDailyWorkflows(req);
    if (action == "rehydrate_archive") return actionBootstrapAndRehydrate(req);
    if (action == "curate_interactive") {
        const std::string prompt = trim(req.value("user_prompt", req.value("prompt", "")));
        if (prompt.empty()) {
            return makeError("invalid_request", "user_prompt is required for curate_interactive.");
        }
        auto search_res = actionSearchArchive({
            {"query", prompt},
            {"domain", req.value("domain", "all")},
            {"limit", req.value("limit", 12)},
        });
        if (!search_res.ok) return search_res;
        nlohmann::json push_req = {
            {"action", "push_interactive_set"},
            {"mode", "interactive_ephemeral"},
            {"record_permanent_history", false},
            {"title", req.value("title", "AI 灵感推荐 · " + utf8Truncate(prompt, 32))},
            {"user_prompt", prompt},
            {"media_items", search_res.data.value("media_items", nlohmann::json::array())},
            {"text_items", search_res.data.value("text_items", nlohmann::json::array())},
        };
        return actionPushInteractiveSet(push_req);
    }
    return makeError("unknown_action", "Unsupported ai.curation.v1 action: " + action);
}

omnidesk::CapabilityResult CurationEngine::actionBootstrapAndRehydrate(const nlohmann::json& req) {
    const auto t0 = std::chrono::steady_clock::now();
    const RepoPaths paths = GitMirror::resolveRepoPaths(&ctx_, &req);
    auto cfg = ctx_.loadConfig();
    const bool pull_remote = req.contains("pull_remote")
                                 ? req.value("pull_remote", false)
                                 : cfg.value("auto_pull_remote", false);
    const bool clone_if_missing = req.value("clone_if_missing", false);
    const bool force_full = req.value("force_full", false);
    const bool sync_vocab = req.value("sync_vocab", true);

    RepoSyncStatus obs_sync = GitMirror::syncRepo(
        paths.obsidian_vault_dir, kObsidianVaultRemoteUrl, clone_if_missing, pull_remote);
    RepoSyncStatus mus_sync = GitMirror::syncRepo(
        paths.music_daily_dir, kMusicDailyRemoteUrl, clone_if_missing, pull_remote);

    HydratedArchive hydrated;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!force_full &&
            last_paths_.obsidian_vault_dir == paths.obsidian_vault_dir &&
            last_paths_.music_daily_dir == paths.music_daily_dir &&
            !archive_.date_stats.empty()) {
            // Pass already-hydrated archive so an exact manifest match avoids re-parsing JSON
            hydrated = archive_;
        }
    }
    bool cache_hit = false;
    std::string err;
    ArchiveRehydrator::rehydrate(paths.obsidian_vault_dir,
                                 paths.music_daily_dir,
                                 paths.curation_dir,
                                 force_full,
                                 hydrated,
                                 cache_hit,
                                 err);

    std::vector<omnidesk::VocabCard> vocabs_to_sync;
    nlohmann::json result_data;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        last_paths_ = paths;
        last_obsidian_sync_ = obs_sync;
        last_music_sync_ = mus_sync;
        last_cache_hit_ = cache_hit;
        last_rehydrate_time_ = currentShanghaiDate();
        archive_ = std::move(hydrated);
        loadPersistedLocalStateLocked(paths.curation_dir);
        vocabs_to_sync = archive_.vocab_cards;

        const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                    std::chrono::steady_clock::now() - t0)
                                    .count();
        const bool dirty_preserved = obs_sync.dirty_worktree_preserved || mus_sync.dirty_worktree_preserved;
        result_data = {
            {"obsidian_vault_dir", paths.obsidian_vault_dir},
            {"music_daily_dir", paths.music_daily_dir},
            {"curation_dir", paths.curation_dir},
            {"dirty_worktree_preserved", dirty_preserved},
            {"obsidian_dirty", obs_sync.dirty_worktree_preserved},
            {"music_daily_dirty", mus_sync.dirty_worktree_preserved},
            {"git_sync", {
                {"obsidian_vault", obs_sync.toJson()},
                {"music_daily", mus_sync.toJson()},
            }},
            {"dates_indexed", archive_.dates_indexed},
            {"music_tracks_hydrated", archive_.music_tracks_hydrated},
            {"youtube_videos_hydrated", archive_.youtube_videos_hydrated},
            {"news_days_hydrated", archive_.news_days_hydrated},
            {"news_stories_hydrated", archive_.news_stories_hydrated},
            {"reddit_days_hydrated", archive_.reddit_days_hydrated},
            {"reddit_posts_hydrated", archive_.reddit_posts_hydrated},
            {"journal_days_hydrated", archive_.journal_days_hydrated},
            {"journal_papers_hydrated", archive_.journal_papers_hydrated},
            {"english_notes_hydrated", archive_.english_notes_hydrated},
            {"vocab_cards_hydrated", archive_.vocab_cards_hydrated},
            {"cache_hit", cache_hit},
            {"elapsed_ms", elapsed_ms},
        };
    }

    if (sync_vocab) {
        syncVocabToSm2Idempotent(vocabs_to_sync);
    }

    ctx_.publish("entity.recommend.updated", {
        {"source", "bootstrap_and_rehydrate"},
        {"dates_indexed", result_data["dates_indexed"]},
    });

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = std::move(result_data);
    return res;
}

omnidesk::CapabilityResult CurationEngine::actionListArchiveDates(const nlohmann::json& req) const {
    std::lock_guard<std::mutex> lock(mutex_);
    const int limit = req.value("limit", 0);
    const std::string domain = req.value("domain", "");

    // Merge archived date_stats with live daily_* bundles (deduplicated by item ID)
    auto merged_stats = archive_.date_stats;
    std::unordered_set<std::string> seen_media_ids;
    std::unordered_set<std::string> seen_text_ids;
    for (const auto& m : archive_.media_items) seen_media_ids.insert(m.id);
    for (const auto& t : archive_.text_items) seen_text_ids.insert(t.id);

    for (const auto& m : daily_media_) {
        if (dismissed_ids_.count(m.id) || !seen_media_ids.insert(m.id).second) continue;
        const std::string d = m.extra.value("business_date", "");
        if (!isValidIsoDate(d)) continue;
        auto& st = merged_stats[d];
        const std::string dom = toLowerAscii(m.extra.value("domain", m.kind));
        if (dom == "music" || m.kind == "music") ++st.music;
        else ++st.youtube;
    }
    for (const auto& t : daily_texts_) {
        if (dismissed_ids_.count(t.id) || !seen_text_ids.insert(t.id).second) continue;
        if (!isValidIsoDate(t.published_at)) continue;
        auto& st = merged_stats[t.published_at];
        if (t.category == "news") ++st.news;
        else if (t.category == "reddit") ++st.reddit;
        else if (t.category == "journal") ++st.journal;
        else if (t.category == "english") ++st.english;
    }

    nlohmann::json dates = nlohmann::json::array();
    for (const auto& [date, st] : merged_stats) {
        if (!domain.empty()) {
            if (domain == "music" && st.music == 0) continue;
            if ((domain == "youtube" || domain == "video") && st.youtube == 0) continue;
            if (domain == "news" && st.news == 0) continue;
            if (domain == "reddit" && st.reddit == 0) continue;
            if (domain == "journal" && st.journal == 0) continue;
            if (domain == "english" && st.english == 0) continue;
        }
        dates.push_back({
            {"date", date},
            {"music", st.music},
            {"youtube", st.youtube},
            {"news", st.news},
            {"reddit", st.reddit},
            {"journal", st.journal},
            {"english", st.english},
            {"total", st.total()},
        });
        if (limit > 0 && static_cast<int>(dates.size()) >= limit) break;
    }
    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = {
        {"total_dates", static_cast<int>(merged_stats.size())},
        {"dates", std::move(dates)},
    };
    return res;
}

omnidesk::CapabilityResult CurationEngine::actionLookupProvenance(const nlohmann::json& req) const {
    if (!req.contains("items") || !req["items"].is_array()) {
        return makeError("invalid_items", "lookup_provenance requires an items array.");
    }
    if (req["items"].size() > 4096) {
        return makeError("too_many_items", "lookup_provenance accepts at most 4096 items per batch.");
    }
    std::lock_guard<std::mutex> lock(mutex_);
    // Pure lookup: never refresh repositories, persist state, or publish events.
    auto stringField = [](const nlohmann::json& obj, const char* key) -> std::string {
        auto it = obj.find(key);
        return it != obj.end() && it->is_string() ? it->get<std::string>() : "";
    };
    auto identityKeys = [&](const nlohmann::json& obj) {
        std::vector<std::string> keys;
        auto add = [&](const std::string& prefix, const std::string& value) {
            if (!value.empty()) keys.push_back(prefix + value);
        };
        const auto ex = obj.contains("extra") && obj["extra"].is_object()
                            ? obj["extra"] : nlohmann::json::object();
        for (const auto& fields : {obj, ex}) {
            add("song:", stringField(fields, "apple_music_song_id"));
            add("song:", stringField(fields, "song_id"));
            add("video:", stringField(fields, "video_id"));
            add("video:", stringField(fields, "youtube_id"));
            add("post:", stringField(fields, "post_id"));
            add("canonical:", normalizeForDedup(stringField(fields, "canonical_id")));
            std::string doi = toLowerAscii(stringField(fields, "doi"));
            for (const std::string prefix : {"https://doi.org/", "http://doi.org/", "doi:"}) {
                if (startsWith(doi, prefix)) doi.erase(0, prefix.size());
            }
            add("doi:", doi);
        }
        for (const char* field : {"url", "play_url"}) {
            auto url = stringField(obj, field);
            add("song:", extractAppleMusicSongId(url));
            add("video:", extractYouTubeVideoId(url));
            add("post:", extractRedditPostId(url));
            if (startsWith(url, "https://doi.org/")) add("doi:", toLowerAscii(url.substr(16)));
            auto hash = url.find('#');
            if (hash != std::string::npos) url.resize(hash);
            while (!url.empty() && url.back() == '/') url.pop_back();
            add("url:", url);
        }
        const std::string title = normalizeForDedup(stringField(obj, "title"));
        const std::string creator = normalizeForDedup(stringField(obj, "creator"));
        if (!title.empty() && !creator.empty()) add("work:", creator + "::" + title);
        // Short titles and songs are ambiguous without a creator or stable identity.
        if (stringField(obj, "kind") != "music" && title.size() >= 16) add("title:", title);
        return keys;
    };
    std::unordered_map<std::string, std::string> dates;
    auto remember = [&](const std::string& key, const std::string& date) {
        if (key.empty() || !isValidIsoDate(date)) return;
        auto it = dates.find(key);
        if (it == dates.end() || date < it->second) dates[key] = date;
    };
    auto addIndex = [&](const auto& index, const std::string& prefix) {
        for (const auto& [key, date] : index) remember(prefix + key, date);
    };
    addIndex(archive_.song_id_to_date, "song:");
    addIndex(archive_.video_id_to_date, "video:");
    addIndex(archive_.post_id_to_date, "post:");
    addIndex(archive_.doi_to_date, "doi:");
    for (const auto& [key, date] : archive_.canonical_id_to_date)
        remember("canonical:" + normalizeForDedup(key), date);
    auto addItem = [&](const nlohmann::json& item, const std::string& date) {
        for (const auto& key : identityKeys(item)) remember(key, date);
    };
    for (const auto& item : archive_.media_items)
        addItem(omnidesk::toJson(item), item.extra.value("business_date", ""));
    for (const auto& item : daily_media_)
        addItem(omnidesk::toJson(item), item.extra.value("business_date", ""));
    auto addTexts = [&](const auto& items, const auto& articles) {
        for (const auto& item : items) {
            auto json = omnidesk::toJson(item);
            auto article = articles.find(item.id);
            if (article != articles.end()) json["doi"] = article->second.doi;
            addItem(json, item.published_at);
        }
    };
    addTexts(archive_.text_items, archive_.articles);
    addTexts(daily_texts_, daily_articles_);
    nlohmann::json matches = nlohmann::json::array();
    for (const auto& item : req["items"]) {
        if (!item.is_object()) continue;
        std::string date;
        for (const auto& key : identityKeys(item)) {
            auto it = dates.find(key);
            if (it != dates.end() && (date.empty() || it->second < date)) date = it->second;
        }
        if (!date.empty()) matches.push_back({
            {"id", stringField(item, "id")}, {"source_id", stringField(item, "source_id")},
            {"previously_recommended_on", date},
        });
    }
    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = {{"matches", std::move(matches)}, {"read_only", true}};
    return res;
}

omnidesk::CapabilityResult CurationEngine::actionSearchArchive(const nlohmann::json& req) const {
    std::lock_guard<std::mutex> lock(mutex_);
    const std::string raw_query = req.value("query", "");
    std::string date_filter = req.value("date", "");
    std::string kw_lower;
    {
        std::string qd;
        parseQueryFilter(raw_query, qd, kw_lower);
        if (date_filter.empty()) date_filter = qd;
    }
    std::string domain = toLowerAscii(req.value("domain", req.value("kind", "all")));
    if (domain.empty()) domain = "all";
    const int limit = std::max(1, req.value("limit", 50));

    nlohmann::json media_out = nlohmann::json::array();
    nlohmann::json text_out = nlohmann::json::array();
    nlohmann::json combined = nlohmann::json::array();

    if (domain == "all" || domain == "music" || domain == "video" || domain == "youtube") {
        std::unordered_set<std::string> seen_media;
        auto push_media = [&](const omnidesk::MediaItem& m) {
            if (dismissed_ids_.count(m.id)) return;
            if (domain == "music" && m.kind != "music") return;
            if ((domain == "video" || domain == "youtube") && m.kind != "video") return;
            if (!mediaMatchesFilter(m, date_filter, kw_lower)) return;
            if (!seen_media.insert(m.id).second) return;
            if (static_cast<int>(combined.size()) >= limit) return;
            auto mj = omnidesk::toJson(m);
            media_out.push_back(mj);
            combined.push_back({
                {"domain", m.extra.value("domain", m.kind)},
                {"id", m.id},
                {"title", m.title},
                {"subtitle", m.creator},
                {"business_date", m.extra.value("business_date", "")},
                {"url", m.play_url},
                {"item", std::move(mj)},
            });
        };
        for (const auto& m : daily_media_) push_media(m);
        for (const auto& m : archive_.media_items) push_media(m);
    }

    if (domain == "all" || domain == "news" || domain == "reddit" ||
        domain == "journal" || domain == "english" || domain == "text") {
        std::unordered_set<std::string> seen_text;
        auto push_text = [&](const omnidesk::TextFeedItem& t,
                              const std::unordered_map<std::string, omnidesk::TextArticle>& art_map) {
            if (dismissed_ids_.count(t.id)) return;
            if (domain != "all" && domain != "text" && t.category != domain) return;
            if (!textMatchesFilter(t, date_filter, kw_lower)) return;
            if (!seen_text.insert(t.id).second) return;
            if (static_cast<int>(combined.size()) >= limit) return;
            auto tj = omnidesk::toJson(t);
            tj["business_date"] = t.published_at;
            auto ait = art_map.find(t.id);
            if (ait != art_map.end() && !ait->second.doi.empty()) {
                tj["doi"] = ait->second.doi;
            }
            text_out.push_back(tj);
            combined.push_back({
                {"domain", t.category},
                {"id", t.id},
                {"title", t.title},
                {"subtitle", t.source_name + " · " + t.published_at},
                {"business_date", t.published_at},
                {"url", t.url},
                {"item", std::move(tj)},
            });
        };
        for (const auto& t : daily_texts_) push_text(t, daily_articles_);
        for (const auto& t : archive_.text_items) push_text(t, archive_.articles);
    }

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = {
        {"query", raw_query},
        {"total", static_cast<int>(combined.size())},
        {"media_items", std::move(media_out)},
        {"text_items", std::move(text_out)},
        {"items", std::move(combined)},
    };
    return res;
}

omnidesk::CapabilityResult CurationEngine::actionGetDateBundle(const nlohmann::json& req) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::string date = req.value("business_date", req.value("date", ""));
    if (!isValidIsoDate(date)) {
        return makeError("invalid_date", "Valid YYYY-MM-DD business_date is required.");
    }
    nlohmann::json media_arr = nlohmann::json::array();
    std::unordered_set<std::string> seen_media;
    for (const auto& m : daily_media_) {
        if (!dismissed_ids_.count(m.id) && m.extra.value("business_date", "") == date &&
            seen_media.insert(m.id).second) {
            media_arr.push_back(omnidesk::toJson(m));
        }
    }
    for (const auto& m : archive_.media_items) {
        if (!dismissed_ids_.count(m.id) && m.extra.value("business_date", "") == date &&
            seen_media.insert(m.id).second) {
            media_arr.push_back(omnidesk::toJson(m));
        }
    }

    nlohmann::json text_arr = nlohmann::json::array();
    std::unordered_set<std::string> seen_text;
    for (const auto& t : daily_texts_) {
        if (!dismissed_ids_.count(t.id) && t.published_at == date &&
            seen_text.insert(t.id).second) {
            text_arr.push_back(omnidesk::toJson(t));
        }
    }
    for (const auto& t : archive_.text_items) {
        if (!dismissed_ids_.count(t.id) && t.published_at == date &&
            seen_text.insert(t.id).second) {
            text_arr.push_back(omnidesk::toJson(t));
        }
    }

    nlohmann::json book_arr = nlohmann::json::array();
    auto eit = archive_.english_days.find(date);
    if (eit != archive_.english_days.end()) {
        omnidesk::BookItem b;
        b.id = "english-date-" + date;
        b.source_id = "plugin-ai-curator";
        b.title = "英语伴读 · " + date;
        b.author = "AI Curation";
        b.format = "markdown";
        b.total_pages = static_cast<int>(eit->second.notes.size());
        b.bilingual_ready = true;
        int vc = 0;
        for (const auto& n : eit->second.notes) vc += static_cast<int>(n.vocab_cards.size());
        b.vocab_count = vc;
        book_arr.push_back(omnidesk::toJson(b));
    }

    nlohmann::json vocab_arr = nlohmann::json::array();
    if (eit != archive_.english_days.end()) {
        for (const auto& n : eit->second.notes) {
            for (const auto& v : n.vocab_cards) vocab_arr.push_back(omnidesk::toJson(v));
        }
    }

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = {
        {"business_date", date},
        {"media_items", std::move(media_arr)},
        {"text_items", std::move(text_arr)},
        {"book_items", std::move(book_arr)},
        {"vocab_cards", std::move(vocab_arr)},
    };
    return res;
}

bool CurationEngine::validateLedgersReadableLocked(const RepoPaths& paths,
                                                 const std::string& operation_id,
                                                 const std::vector<omnidesk::MediaItem>& media,
                                                 const std::vector<omnidesk::TextFeedItem>& texts,
                                                 const std::vector<omnidesk::TextArticle>& articles,
                                                 std::string& error_code,
                                                 std::string& error_msg) const {
    std::error_code ec;
    std::unordered_set<std::string> prepared_ids;
    std::unordered_set<std::string> history_keys;

    // 1. Validate obsidian-vault ledgers if obsidian_vault_dir exists
    if (fs::exists(paths.obsidian_vault_dir, ec)) {
        const fs::path rec_dir = fs::path(paths.obsidian_vault_dir) / ".recommendation";
        if (fs::exists(rec_dir, ec)) {
            const fs::path policy_path = rec_dir / "policy.json";
            if (fs::exists(policy_path, ec)) {
                std::string jerr;
                if (!readJsonFile(policy_path.string(), &jerr).has_value()) {
                    error_code = "unreadable_ledger";
                    error_msg = "Unreadable or corrupted policy.json: " + jerr;
                    return false;
                }
            }

            const char* hist_files[] = {"youtube.jsonl", "reddit.jsonl", "english.jsonl", "journal.jsonl"};
            for (const char* hf : hist_files) {
                fs::path hp = rec_dir / "history" / hf;
                if (!fs::exists(hp, ec)) continue;
                auto txt = readFileText(hp.string());
                if (!txt.has_value()) {
                    error_code = "unreadable_ledger";
                    error_msg = "Cannot read permanent history ledger: " + hp.string();
                    return false;
                }
                std::istringstream iss(*txt);
                std::string line;
                size_t lnum = 0;
                while (std::getline(iss, line)) {
                    ++lnum;
                    std::string t = trim(line);
                    if (t.empty()) continue;
                    try {
                        auto j = nlohmann::json::parse(t);
                        if (!j.is_object()) throw std::runtime_error("not an object");
                        if (j.contains("video_id") && j["video_id"].is_string()) {
                            history_keys.insert("vid:" + j["video_id"].get<std::string>());
                        }
                        if (j.contains("post_id") && j["post_id"].is_string()) {
                            history_keys.insert("pid:" + j["post_id"].get<std::string>());
                        }
                        if (j.contains("doi") && j["doi"].is_string()) {
                            history_keys.insert("doi:" + toLowerAscii(j["doi"].get<std::string>()));
                        }
                        if (j.contains("normalized_doi") && j["normalized_doi"].is_string()) {
                            history_keys.insert("doi:" + toLowerAscii(j["normalized_doi"].get<std::string>()));
                        }
                        if (j.contains("source_url") && j["source_url"].is_string()) {
                            history_keys.insert("url:" + j["source_url"].get<std::string>());
                        }
                    } catch (const std::exception& e) {
                        error_code = "unreadable_ledger";
                        error_msg = "Corrupted JSONL line " + std::to_string(lnum) + " in " + hp.string() + ": " + e.what();
                        return false;
                    }
                }
            }

            const fs::path tx_dir = rec_dir / "transactions";
            if (fs::exists(tx_dir, ec)) {
                for (const auto& entry : fs::recursive_directory_iterator(tx_dir, ec)) {
                    if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
                    std::string jerr;
                    auto txj = readJsonFile(entry.path().string(), &jerr);
                    if (!txj.has_value() || !txj->is_object()) {
                        error_code = "unreadable_ledger";
                        error_msg = "Unreadable transaction bundle: " + entry.path().string();
                        return false;
                    }
                    if (!operation_id.empty() && txj->contains("plan") && (*txj)["plan"].is_object()) {
                        const auto& plan = (*txj)["plan"];
                        if (plan.value("operation_id", "") == operation_id &&
                            plan.contains("exposed_resource_ids") &&
                            plan["exposed_resource_ids"].is_array()) {
                            for (const auto& rid : plan["exposed_resource_ids"]) {
                                if (rid.is_string()) prepared_ids.insert(rid.get<std::string>());
                            }
                        }
                    }
                }
            }
        }
    }

    // 2. Validate music-daily/state/tracks.json if music_daily_dir exists
    if (fs::exists(paths.music_daily_dir, ec)) {
        const fs::path tracks_path = fs::path(paths.music_daily_dir) / "state" / "tracks.json";
        if (fs::exists(tracks_path, ec)) {
            std::string jerr;
            auto tj = readJsonFile(tracks_path.string(), &jerr);
            if (!tj.has_value() || !tj->is_object()) {
                error_code = "unreadable_ledger";
                error_msg = "Unreadable or corrupted music-daily state/tracks.json: " + jerr;
                return false;
            }
            if (tj->contains("recommended") && (*tj)["recommended"].is_object()) {
                for (auto it = (*tj)["recommended"].begin(); it != (*tj)["recommended"].end(); ++it) {
                    history_keys.insert("cid:" + it.key());
                }
            }
        }
    }

    // 3. Check duplicates against permanent history (unless recovering PREPARED op)
    auto is_prepared = [&](const std::string& k) -> bool {
        return prepared_ids.count(k) > 0;
    };

    for (const auto& m : media) {
        std::string sid = m.extra.value("apple_music_song_id", "");
        std::string cid = m.extra.value("canonical_id", "");
        std::string vid = m.extra.value("video_id", "");
        if (!sid.empty() && !is_prepared(sid) && !is_prepared("song:" + sid) &&
            archive_.song_id_to_date.count(sid) > 0) {
            error_code = "duplicate_in_permanent_history";
            error_msg = "Duplicate Apple Music song_id in permanent history (" +
                        archive_.song_id_to_date.at(sid) + "): " + sid;
            return false;
        }
        if (!cid.empty() && !is_prepared(cid) &&
            (history_keys.count("cid:" + cid) > 0 || archive_.canonical_id_to_date.count(cid) > 0)) {
            error_code = "duplicate_in_permanent_history";
            error_msg = "Duplicate canonical_id in permanent history: " + cid;
            return false;
        }
        if (!vid.empty() && !is_prepared(vid) && !is_prepared("youtube:" + vid) &&
            (history_keys.count("vid:" + vid) > 0 || archive_.video_id_to_date.count(vid) > 0)) {
            error_code = "duplicate_in_permanent_history";
            error_msg = "Duplicate YouTube video_id in permanent history: " + vid;
            return false;
        }
    }

    for (size_t i = 0; i < texts.size(); ++i) {
        const auto& t = texts[i];
        const auto& a = articles[i];
        std::string doi = toLowerAscii(a.doi);
        if (!doi.empty() && !is_prepared("doi:" + doi) && !is_prepared(doi) &&
            (history_keys.count("doi:" + doi) > 0 || archive_.doi_to_date.count(doi) > 0)) {
            error_code = "duplicate_in_permanent_history";
            error_msg = "Duplicate DOI in permanent history: " + doi;
            return false;
        }
        if (t.category == "reddit" && !t.url.empty()) {
            std::string pid = extractRedditPostId(t.url);
            if (!pid.empty() && !is_prepared(pid) &&
                (history_keys.count("pid:" + pid) > 0 || archive_.post_id_to_date.count(pid) > 0)) {
                error_code = "duplicate_in_permanent_history";
                error_msg = "Duplicate Reddit post_id in permanent history: " + pid;
                return false;
            }
        }
        if (!t.url.empty() && !is_prepared(t.url) &&
            (history_keys.count("url:" + t.url) > 0 || archive_.url_to_date.count(t.url) > 0)) {
            error_code = "duplicate_in_permanent_history";
            error_msg = "Duplicate source URL in permanent history: " + t.url;
            return false;
        }
    }
    return true;
}

omnidesk::CapabilityResult CurationEngine::actionPublishDailyBundle(const nlohmann::json& req) {
    if (req.value("mode", "") != "daily_immutable") {
        return makeError("invalid_curation_mode", "publish_daily_bundle requires mode == \"daily_immutable\".");
    }
    if (!req.value("qa_verified", false)) {
        return makeError("qa_not_verified", "publish_daily_bundle requires qa_verified == true.");
    }

    const std::string today = currentShanghaiDate();
    std::string business_date = req.value("business_date", today);
    if (!isValidIsoDate(business_date)) {
        return makeError("invalid_business_date", "business_date must be valid YYYY-MM-DD.");
    }
    if (business_date < today && !req.value("allow_historical_recovery", false)) {
        return makeError("historical_backfill_forbidden",
                         "Silent backfill of past business_date (" + business_date +
                             ") is forbidden without allow_historical_recovery=true.");
    }

    const std::string operation_id = req.value("operation_id", "daily:" + business_date);
    nlohmann::json publication_evidence;
    std::string evidence_error;
    if (!validatePublicationEvidence(req, operation_id, business_date,
                                     publication_evidence, evidence_error)) {
        return makeError("needs_verified_bundle", evidence_error);
    }

    std::vector<omnidesk::MediaItem> incoming_media;
    if (req.contains("media_items") && req["media_items"].is_array()) {
        for (const auto& mj : req["media_items"]) {
            if (!mj.is_object()) continue;
            auto m = parseIncomingMediaItem(mj, "daily_immutable");
            if (m.source_badge.empty()) m.source_badge = "每日精选 · " + business_date;
            if (m.extra.value("business_date", "").empty()) m.extra["business_date"] = business_date;
            incoming_media.push_back(std::move(m));
        }
    }

    std::vector<omnidesk::TextFeedItem> incoming_texts;
    std::vector<omnidesk::TextArticle> incoming_articles;
    if (req.contains("text_items") && req["text_items"].is_array()) {
        for (const auto& tj : req["text_items"]) {
            if (!tj.is_object()) continue;
            omnidesk::TextFeedItem fi;
            omnidesk::TextArticle art;
            parseIncomingTextItem(tj, "news", fi, art);
            if (fi.published_at.empty()) fi.published_at = business_date;
            if (art.published_at.empty()) art.published_at = business_date;
            incoming_texts.push_back(std::move(fi));
            incoming_articles.push_back(std::move(art));
        }
    }

    std::vector<omnidesk::BookItem> incoming_books;
    if (req.contains("book_items") && req["book_items"].is_array()) {
        incoming_books = omnidesk::fromJsonArray<omnidesk::BookItem>(req["book_items"]);
        for (auto& b : incoming_books) {
            if (b.source_id.empty()) b.source_id = "plugin-ai-curator";
        }
    }

    std::vector<omnidesk::VocabCard> incoming_vocabs;
    if (req.contains("vocab_cards") && req["vocab_cards"].is_array()) {
        incoming_vocabs = omnidesk::fromJsonArray<omnidesk::VocabCard>(req["vocab_cards"]);
    }

    const std::string ephemeral_note = req.value("ephemeral_chat_note", "");
    const RepoPaths paths = GitMirror::resolveRepoPaths(&ctx_, &req);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        std::string err_code, err_msg;
        if (!validateLedgersReadableLocked(paths, operation_id,
                                           incoming_media, incoming_texts, incoming_articles,
                                           err_code, err_msg)) {
            return makeError(err_code, err_msg);
        }

        // Optional markdown updates strictly preserving ## Notes outside AUTO:START/END
        if (req.contains("markdown_updates") && req["markdown_updates"].is_array()) {
            for (const auto& upd : req["markdown_updates"]) {
                if (!upd.is_object()) continue;
                std::string mpath = upd.value("path", "");
                std::string mauto = upd.value("auto_block", "");
                if (!mpath.empty() && !mauto.empty()) {
                    std::string uerr;
                    if (!updateMarkdownPreservingNotes(mpath, mauto, &uerr)) {
                        return makeError("markdown_update_failed", uerr);
                    }
                }
            }
        }

        // Persist bundle WITHOUT ephemeral_chat_note so 今日饮食 NEVER touches disk
        nlohmann::json persisted_bundle = {
            {"schema_version", 1},
            {"mode", "daily_immutable"},
            {"operation_id", operation_id},
            {"business_date", business_date},
            {"qa_verified", true},
            {"publication_evidence", publication_evidence},
            {"media_items", omnidesk::toJsonArray(incoming_media)},
            {"text_items", omnidesk::toJsonArray(incoming_texts)},
            {"book_items", omnidesk::toJsonArray(incoming_books)},
            {"vocab_cards", omnidesk::toJsonArray(incoming_vocabs)},
        };
        const fs::path bundle_path = fs::path(paths.curation_dir) / "daily" / business_date /
                                     (sanitizeFilenameComponent(operation_id) + ".json");
        std::string werr;
        if (!writeJsonFileAtomic(bundle_path.string(), persisted_bundle, 2, &werr)) {
            return makeError("persist_failed", werr);
        }

        // Merge into live in-memory feeds (replacing items with matching id)
        for (auto& m : incoming_media) {
            auto it = std::find_if(daily_media_.begin(), daily_media_.end(),
                                   [&](const auto& x) { return x.id == m.id; });
            if (it != daily_media_.end()) *it = m;
            else daily_media_.push_back(m);
        }
        for (size_t i = 0; i < incoming_texts.size(); ++i) {
            const auto& fi = incoming_texts[i];
            const auto& art = incoming_articles[i];
            auto it = std::find_if(daily_texts_.begin(), daily_texts_.end(),
                                   [&](const auto& x) { return x.id == fi.id; });
            if (it != daily_texts_.end()) *it = fi;
            else daily_texts_.push_back(fi);
            daily_articles_[art.id] = art;
        }
        for (const auto& b : incoming_books) daily_books_.push_back(b);
        for (const auto& v : incoming_vocabs) daily_vocabs_.push_back(v);
        last_paths_ = paths;
    }

    if (!incoming_vocabs.empty()) {
        syncVocabToSm2Idempotent(incoming_vocabs);
    }

    if (!ephemeral_note.empty()) {
        ctx_.publish("entity.recommend.ephemeral_note", {
            {"business_date", business_date},
            {"operation_id", operation_id},
            {"note", ephemeral_note},
        });
    }

    ctx_.publish("entity.recommend.updated", {
        {"mode", "daily_immutable"},
        {"operation_id", operation_id},
        {"business_date", business_date},
        {"media_count", incoming_media.size()},
        {"text_count", incoming_texts.size()},
        {"vocab_count", incoming_vocabs.size()},
    });

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = {
        {"mode", "daily_immutable"},
        {"operation_id", operation_id},
        {"business_date", business_date},
        {"publication_evidence", publication_evidence},
        {"media_count", incoming_media.size()},
        {"text_count", incoming_texts.size()},
        {"book_count", incoming_books.size()},
        {"vocab_count", incoming_vocabs.size()},
        {"ephemeral_note_emitted", !ephemeral_note.empty()},
    };
    return res;
}

std::optional<std::string> CurationEngine::matchHistoricalProvenanceLocked(
    const nlohmann::json& item_json,
    const std::string& title,
    const std::string& url,
    size_t min_norm_title_len) const {
    if (item_json.contains("extra") && item_json["extra"].is_object()) {
        const auto& ex = item_json["extra"];
        std::string sid = ex.value("apple_music_song_id", ex.value("song_id", ""));
        if (!sid.empty()) {
            auto it = archive_.song_id_to_date.find(sid);
            if (it != archive_.song_id_to_date.end()) return it->second;
        }
        std::string cid = ex.value("canonical_id", "");
        if (!cid.empty()) {
            auto it = archive_.canonical_id_to_date.find(cid);
            if (it != archive_.canonical_id_to_date.end()) return it->second;
        }
        std::string vid = ex.value("video_id", "");
        if (!vid.empty()) {
            auto it = archive_.video_id_to_date.find(vid);
            if (it != archive_.video_id_to_date.end()) return it->second;
        }
        std::string doi = toLowerAscii(ex.value("doi", ""));
        if (!doi.empty()) {
            auto it = archive_.doi_to_date.find(doi);
            if (it != archive_.doi_to_date.end()) return it->second;
        }
    }
    std::string doi = toLowerAscii(item_json.value("doi", ""));
    if (!doi.empty()) {
        auto it = archive_.doi_to_date.find(doi);
        if (it != archive_.doi_to_date.end()) return it->second;
    }
    if (!url.empty()) {
        std::string am_sid = extractAppleMusicSongId(url);
        if (!am_sid.empty()) {
            auto it = archive_.song_id_to_date.find(am_sid);
            if (it != archive_.song_id_to_date.end()) return it->second;
        }
        std::string yt_vid = extractYouTubeVideoId(url);
        if (!yt_vid.empty()) {
            auto it = archive_.video_id_to_date.find(yt_vid);
            if (it != archive_.video_id_to_date.end()) return it->second;
        }
        std::string r_pid = extractRedditPostId(url);
        if (!r_pid.empty()) {
            auto it = archive_.post_id_to_date.find(r_pid);
            if (it != archive_.post_id_to_date.end()) return it->second;
        }
        auto uit = archive_.url_to_date.find(url);
        if (uit != archive_.url_to_date.end()) return uit->second;
    }
    std::string nt = normalizeForDedup(title);
    if (!nt.empty() && nt.size() >= min_norm_title_len) {
        auto tit = archive_.norm_title_to_date.find(nt);
        if (tit != archive_.norm_title_to_date.end()) return tit->second;
    }
    return std::nullopt;
}

omnidesk::CapabilityResult CurationEngine::actionPushInteractiveSet(const nlohmann::json& req) {
    if (req.value("mode", "") != "interactive_ephemeral") {
        return makeError("invalid_curation_mode",
                         "push_interactive_set requires mode == \"interactive_ephemeral\".");
    }
    if (!req.contains("record_permanent_history") ||
        !req["record_permanent_history"].is_boolean() ||
        req["record_permanent_history"].get<bool>()) {
        return makeError("ledger_pollution_forbidden",
                         "Interactive requests must explicitly set record_permanent_history=false.");
    }

    const std::string session_id = req.value("session_id", "");
    if (!safeIdentifier(session_id)) {
        return makeError("invalid_session_id",
                         "session_id must be 1-96 ASCII letters, digits, '-' or '_'.");
    }
    const std::string title = req.value("title", "");
    const std::string user_prompt = req.value("user_prompt", "");
    const std::string repeat_policy = req.value("repeat_policy", "allow_any");
    if (repeat_policy != "allow_any" && repeat_policy != "prefer_familiar" &&
        repeat_policy != "session_dedup_only") {
        return makeError("invalid_repeat_policy", "Unsupported repeat_policy: " + repeat_policy);
    }
    const bool replace_session = req.value("replace_session_items", false);
    const RepoPaths paths = GitMirror::resolveRepoPaths(&ctx_, &req);

    InteractiveSession incoming;
    incoming.session_id = session_id;
    incoming.title = title.empty() ? "AI 灵感推荐" : title;
    incoming.user_prompt = user_prompt;
    incoming.repeat_policy = repeat_policy;
    int revisit_matches = 0;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (req.contains("media_items") && req["media_items"].is_array()) {
            for (const auto& mj : req["media_items"]) {
                if (!mj.is_object()) continue;
                omnidesk::MediaItem item = parseIncomingMediaItem(mj, "interactive_ephemeral");
                if (item.id.empty()) {
                    return makeError("invalid_item", "Every interactive media item requires a non-empty id.");
                }
                item.extra["curation_mode"] = "interactive_ephemeral";
                if (item.source_badge.empty()) item.source_badge = "AI 灵感推荐 · 可重温";

                if (auto prev = matchHistoricalProvenanceLocked(mj, item.title, item.play_url)) {
                    item.extra["previously_recommended_on"] = *prev;
                    item.extra["revisit_badge"] = "经典重温 · " + *prev;
                    revisit_matches++;
                }
                incoming.media_items.push_back(std::move(item));
            }
        }
        if (req.contains("text_items") && req["text_items"].is_array()) {
            for (const auto& tj : req["text_items"]) {
                if (!tj.is_object()) continue;
                omnidesk::TextFeedItem fi;
                omnidesk::TextArticle art;
                parseIncomingTextItem(tj, "interactive", fi, art);
                fi.category = "interactive";
                if (auto prev = matchHistoricalProvenanceLocked(tj, fi.title, fi.url)) {
                    fi.tags.push_back("经典重温 · " + *prev);
                    revisit_matches++;
                }
                incoming.text_items.push_back(std::move(fi));
                incoming.articles[art.id] = std::move(art);
            }
        }
        if (req.contains("book_items") && req["book_items"].is_array()) {
            incoming.book_items = omnidesk::fromJsonArray<omnidesk::BookItem>(req["book_items"]);
        }
        if (req.contains("book_spreads") && req["book_spreads"].is_object()) {
            for (auto it = req["book_spreads"].begin(); it != req["book_spreads"].end(); ++it) {
                if (!it.value().is_object()) continue;
                omnidesk::BookSpread spread;
                omnidesk::fromJson(it.value(), spread);
                if (spread.book_id.empty()) spread.book_id = it.key();
                incoming.book_spreads[spread.book_id] = std::move(spread);
            }
        }
    }

    nlohmann::json added_media_json = nlohmann::json::array();
    nlohmann::json added_text_json = nlohmann::json::array();
    nlohmann::json added_book_json = nlohmann::json::array();
    int skipped_session_duplicates = 0;
    const fs::path sess_path = fs::path(paths.curation_dir) / "interactive" / "sessions" /
                               (sanitizeFilenameComponent(session_id) + ".json");
    InteractiveSession merged;
    try {
        CacheLock session_lock(sess_path.string() + ".lock");
        std::string read_error;
        auto existing_json = readJsonFile(sess_path.string(), &read_error);
        if (existing_json.has_value()) {
            merged = parseInteractiveSession(*existing_json, session_id);
        } else if (fs::exists(sess_path)) {
            return makeError("persist_failed", "Unreadable interactive session: " + read_error);
        } else {
            merged.session_id = session_id;
        }

        if (replace_session) {
            InteractiveSession metadata = merged;
            merged = incoming;
            if (!req.contains("title")) merged.title = metadata.title;
            if (!req.contains("user_prompt")) merged.user_prompt = metadata.user_prompt;
            if (!req.contains("repeat_policy")) merged.repeat_policy = metadata.repeat_policy;
            added_media_json = omnidesk::toJsonArray(merged.media_items);
            added_text_json = omnidesk::toJsonArray(merged.text_items);
            added_book_json = omnidesk::toJsonArray(merged.book_items);
        } else {
            std::unordered_set<std::string> media_ids;
            std::unordered_set<std::string> media_identities;
            for (const auto& item : merged.media_items) {
                media_ids.insert(item.id);
                media_identities.insert(mediaSessionKey(item));
            }
            for (auto& item : incoming.media_items) {
                const std::string identity = mediaSessionKey(item);
                const bool duplicate = media_ids.count(item.id) > 0 ||
                                       (repeat_policy == "session_dedup_only" &&
                                        media_identities.count(identity) > 0);
                if (duplicate) {
                    if (repeat_policy == "session_dedup_only") ++skipped_session_duplicates;
                    continue;
                }
                media_ids.insert(item.id);
                media_identities.insert(identity);
                merged.media_items.push_back(std::move(item));
                added_media_json.push_back(omnidesk::toJson(merged.media_items.back()));
            }
            std::unordered_set<std::string> text_ids;
            std::unordered_set<std::string> text_identities;
            for (const auto& item : merged.text_items) {
                text_ids.insert(item.id);
                auto article_it = merged.articles.find(item.id);
                text_identities.insert(
                    article_it == merged.articles.end()
                        ? textSessionKey(item, omnidesk::TextArticle{})
                        : textSessionKey(item, article_it->second));
            }
            for (std::size_t i = 0; i < incoming.text_items.size(); ++i) {
                auto& item = incoming.text_items[i];
                auto article_it = incoming.articles.find(item.id);
                const omnidesk::TextArticle article =
                    article_it == incoming.articles.end() ? omnidesk::TextArticle{} : article_it->second;
                const std::string identity = textSessionKey(item, article);
                const bool duplicate = text_ids.count(item.id) > 0 ||
                                       (repeat_policy == "session_dedup_only" &&
                                        text_identities.count(identity) > 0);
                if (duplicate) {
                    if (repeat_policy == "session_dedup_only") ++skipped_session_duplicates;
                    continue;
                }
                text_ids.insert(item.id);
                text_identities.insert(identity);
                merged.text_items.push_back(std::move(item));
                added_text_json.push_back(omnidesk::toJson(merged.text_items.back()));
                if (article_it != incoming.articles.end()) {
                    merged.articles[article_it->first] = article_it->second;
                }
            }
            std::unordered_set<std::string> book_ids;
            std::unordered_set<std::string> book_identities;
            for (const auto& item : merged.book_items) {
                book_ids.insert(item.id);
                book_identities.insert(bookSessionKey(item));
            }
            for (auto& item : incoming.book_items) {
                const std::string identity = bookSessionKey(item);
                const bool duplicate = book_ids.count(item.id) > 0 ||
                                       (repeat_policy == "session_dedup_only" &&
                                        book_identities.count(identity) > 0);
                if (duplicate) {
                    if (repeat_policy == "session_dedup_only") ++skipped_session_duplicates;
                    continue;
                }
                book_ids.insert(item.id);
                book_identities.insert(identity);
                merged.book_items.push_back(std::move(item));
                added_book_json.push_back(omnidesk::toJson(merged.book_items.back()));
            }
            for (auto& [id, spread] : incoming.book_spreads) {
                merged.book_spreads[id] = std::move(spread);
            }
        }

        merged.session_id = session_id;
        if (req.contains("title")) merged.title = incoming.title;
        if (req.contains("user_prompt")) merged.user_prompt = incoming.user_prompt;
        if (req.contains("repeat_policy")) merged.repeat_policy = incoming.repeat_policy;
        if (merged.title.empty()) merged.title = "AI 灵感推荐";

        std::string write_error;
        if (!writeJsonFileAtomicLocked(sess_path.string(), interactiveSessionToJson(merged), 2, &write_error)) {
            return makeError("persist_failed", write_error);
        }

        std::lock_guard<std::mutex> lock(mutex_);
        auto it = std::find_if(interactive_sessions_.begin(), interactive_sessions_.end(),
                               [&](const auto& session) { return session.session_id == session_id; });
        if (it == interactive_sessions_.end()) {
            interactive_sessions_.push_back(std::move(merged));
        } else {
            *it = std::move(merged);
        }
        last_paths_ = paths;
    } catch (const std::exception& exception) {
        return makeError("persist_failed", exception.what());
    }

    ctx_.publish("entity.recommend.updated", {
        {"mode", "interactive_ephemeral"},
        {"session_id", session_id},
        {"media_count", added_media_json.size()},
        {"text_count", added_text_json.size()},
        {"skipped_session_duplicates", skipped_session_duplicates},
        {"revisit_matches", revisit_matches},
    });

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = {
        {"mode", "interactive_ephemeral"},
        {"session_id", session_id},
        {"media_count", added_media_json.size()},
        {"text_count", added_text_json.size()},
        {"added_media_count", added_media_json.size()},
        {"added_text_count", added_text_json.size()},
        {"added_book_count", added_book_json.size()},
        {"skipped_session_duplicates", skipped_session_duplicates},
        {"revisit_matches", revisit_matches},
        {"media_items", added_media_json},
        {"text_items", added_text_json},
        {"book_items", added_book_json},
    };
    return res;
}

omnidesk::CapabilityResult CurationEngine::actionPinItem(const nlohmann::json& req) {
    const std::string item_id = req.value("item_id", req.value("id", ""));
    if (item_id.empty()) return makeError("invalid_request", "item_id is required.");
    std::string curation_dir;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        pinned_ids_.insert(item_id);
        dismissed_ids_.erase(item_id);
        curation_dir = last_paths_.curation_dir.empty()
                           ? GitMirror::resolveRepoPaths(&ctx_, &req).curation_dir
                           : last_paths_.curation_dir;
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& id : pinned_ids_) arr.push_back(id);
        writeJsonFileAtomic((fs::path(curation_dir) / "interactive" / "pinned.json").string(),
                            {{"schema_version", 1}, {"ids", std::move(arr)}});
    }
    ctx_.publish("entity.recommend.updated", {{"action", "pin_item"}, {"item_id", item_id}});
    return {true, {{"pinned", true}, {"item_id", item_id}}, "", "", false};
}

omnidesk::CapabilityResult CurationEngine::actionDismissItem(const nlohmann::json& req) {
    const std::string item_id = req.value("item_id", req.value("id", ""));
    if (item_id.empty()) return makeError("invalid_request", "item_id is required.");
    {
        std::lock_guard<std::mutex> lock(mutex_);
        dismissed_ids_.insert(item_id);
        pinned_ids_.erase(item_id);
        std::string curation_dir = last_paths_.curation_dir.empty()
                                       ? GitMirror::resolveRepoPaths(&ctx_, &req).curation_dir
                                       : last_paths_.curation_dir;
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& id : dismissed_ids_) arr.push_back(id);
        writeJsonFileAtomic((fs::path(curation_dir) / "interactive" / "dismissed.json").string(),
                            {{"schema_version", 1}, {"ids", std::move(arr)}});
    }
    ctx_.publish("entity.recommend.updated", {{"action", "dismiss_item"}, {"item_id", item_id}});
    return {true, {{"dismissed", true}, {"item_id", item_id}}, "", "", false};
}

omnidesk::CapabilityResult CurationEngine::actionClearInteractive(const nlohmann::json& req) {
    const std::string session_id = req.value("session_id", "");
    {
        std::lock_guard<std::mutex> lock(mutex_);
        std::string curation_dir = last_paths_.curation_dir.empty()
                                       ? GitMirror::resolveRepoPaths(&ctx_, &req).curation_dir
                                       : last_paths_.curation_dir;
        std::error_code ec;
        const fs::path ix_dir = fs::path(curation_dir) / "interactive" / "sessions";
        if (session_id.empty()) {
            interactive_sessions_.clear();
            if (fs::exists(ix_dir, ec)) {
                for (const auto& e : fs::directory_iterator(ix_dir, ec)) {
                    if (e.is_regular_file()) fs::remove(e.path(), ec);
                }
            }
        } else {
            interactive_sessions_.erase(
                std::remove_if(interactive_sessions_.begin(), interactive_sessions_.end(),
                               [&](const auto& s) { return s.session_id == session_id; }),
                interactive_sessions_.end());
            fs::remove(ix_dir / (sanitizeFilenameComponent(session_id) + ".json"), ec);
        }
    }
    ctx_.publish("entity.recommend.updated", {{"action", "clear_interactive"}});
    return {true, {{"cleared", true}}, "", "", false};
}

nlohmann::json CurationEngine::statusJson() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return {
        {"ok", true},
        {"obsidian_vault_dir", last_paths_.obsidian_vault_dir},
        {"music_daily_dir", last_paths_.music_daily_dir},
        {"curation_dir", last_paths_.curation_dir},
        {"dirty_worktree_preserved",
         last_obsidian_sync_.dirty_worktree_preserved || last_music_sync_.dirty_worktree_preserved},
        {"git_sync", {
            {"obsidian_vault", last_obsidian_sync_.toJson()},
            {"music_daily", last_music_sync_.toJson()},
        }},
        {"dates_indexed", archive_.dates_indexed},
        {"music_tracks_hydrated", archive_.music_tracks_hydrated},
        {"youtube_videos_hydrated", archive_.youtube_videos_hydrated},
        {"news_days_hydrated", archive_.news_days_hydrated},
        {"news_stories_hydrated", archive_.news_stories_hydrated},
        {"reddit_days_hydrated", archive_.reddit_days_hydrated},
        {"reddit_posts_hydrated", archive_.reddit_posts_hydrated},
        {"journal_days_hydrated", archive_.journal_days_hydrated},
        {"journal_papers_hydrated", archive_.journal_papers_hydrated},
        {"english_notes_hydrated", archive_.english_notes_hydrated},
        {"vocab_cards_hydrated", archive_.vocab_cards_hydrated},
        {"daily_media_count", daily_media_.size()},
        {"daily_text_count", daily_texts_.size()},
        {"interactive_sessions_count", interactive_sessions_.size()},
        {"pinned_count", pinned_ids_.size()},
        {"dismissed_count", dismissed_ids_.size()},
        {"cache_hit", last_cache_hit_},
        {"last_rehydrate_date", last_rehydrate_time_},
    };
}

omnidesk::CapabilityResult CurationEngine::actionStatus(const nlohmann::json&) const {
    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = statusJson();
    return res;
}

omnidesk::CapabilityResult CurationEngine::actionDailyStatus(const nlohmann::json& req) {
    const RepoPaths paths = GitMirror::resolveRepoPaths(&ctx_, &req);
    if (!scheduler_) {
        return makeError("scheduler_unavailable", "DailyWorkflowScheduler is not initialized.");
    }
    nlohmann::json st = scheduler_->inspectStatus(paths);

    // If today's repo targets already exist but archive_ doesn't index today yet, refresh incrementally
    const std::string today = st.value("business_date", "");
    const bool any_repo = st.value("any_repo_targets_exist", false);
    if (any_repo && !today.empty()) {
        bool need_rehydrate = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (archive_.date_stats.find(today) == archive_.date_stats.end()) {
                need_rehydrate = true;
            }
        }
        if (need_rehydrate) {
            rehydrateLocalIncremental(paths);
            st = scheduler_->inspectStatus(paths);
        }
    }

    omnidesk::CapabilityResult res;
    res.ok = true;
    res.data = std::move(st);
    return res;
}

omnidesk::CapabilityResult CurationEngine::actionConfigureDailyScheduler(const nlohmann::json& req) {
    const RepoPaths paths = GitMirror::resolveRepoPaths(&ctx_, &req);
    if (!scheduler_) {
        return makeError("scheduler_unavailable", "DailyWorkflowScheduler is not initialized.");
    }
    return scheduler_->configure(paths, req);
}

omnidesk::CapabilityResult CurationEngine::actionRunDailyWorkflows(const nlohmann::json& req) {
    const RepoPaths paths = GitMirror::resolveRepoPaths(&ctx_, &req);
    if (!scheduler_) {
        return makeError("scheduler_unavailable", "DailyWorkflowScheduler is not initialized.");
    }
    return scheduler_->startWorkflows(paths, req, req.value("wait", false));
}

omnidesk::CapabilityResult CurationEngine::actionCancelDailyWorkflows(const nlohmann::json&) {
    if (!scheduler_) {
        return makeError("scheduler_unavailable", "DailyWorkflowScheduler is not initialized.");
    }
    return scheduler_->cancelWorkflows();
}

// --- Domain query methods ---

std::vector<omnidesk::MediaItem> CurationEngine::queryMedia(const std::string& category,
                                                            const std::string& query) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::string date_filter, kw_lower;
    parseQueryFilter(query, date_filter, kw_lower);

    std::vector<omnidesk::MediaItem> out;
    if (category == "AI 灵感推荐") {
        for (auto it = interactive_sessions_.rbegin(); it != interactive_sessions_.rend(); ++it) {
            for (const auto& m : it->media_items) {
                if (dismissed_ids_.count(m.id)) continue;
                if (mediaMatchesFilter(m, date_filter, kw_lower)) out.push_back(m);
            }
        }
        return out;
    }

    if (category == "每日 5 首 · 最新") {
        std::string latest_date;
        for (const auto& m : daily_media_) {
            if (m.kind == "music") {
                std::string d = m.extra.value("business_date", "");
                if (d > latest_date) latest_date = d;
            }
        }
        for (const auto& m : archive_.media_items) {
            if (m.kind == "music") {
                std::string d = m.extra.value("business_date", "");
                if (d > latest_date) latest_date = d;
            }
        }
        std::string target_date = date_filter.empty() ? latest_date : date_filter;
        for (const auto& m : daily_media_) {
            if (m.kind == "music" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, target_date, kw_lower)) {
                out.push_back(m);
            }
        }
        for (const auto& m : archive_.media_items) {
            if (m.kind == "music" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, target_date, kw_lower)) {
                out.push_back(m);
            }
        }
        return out;
    }

    if (category == "每日 5 首 · 全量曲库") {
        for (const auto& m : daily_media_) {
            if (m.kind == "music" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, date_filter, kw_lower)) {
                out.push_back(m);
            }
        }
        for (const auto& m : archive_.media_items) {
            if (m.kind == "music" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, date_filter, kw_lower)) {
                out.push_back(m);
            }
        }
        return out;
    }

    if (category == "YouTube 每日精选" || category == "video") {
        std::unordered_set<std::string> seen;
        for (const auto& m : daily_media_) {
            if (m.kind == "video" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, date_filter, kw_lower) && seen.insert(m.id).second) {
                out.push_back(m);
            }
        }
        for (const auto& m : archive_.media_items) {
            if (m.kind == "video" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, date_filter, kw_lower) && seen.insert(m.id).second) {
                out.push_back(m);
            }
        }
        return out;
    }

    if (category == "music") {
        std::unordered_set<std::string> seen;
        for (const auto& m : daily_media_) {
            if (m.kind == "music" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, date_filter, kw_lower) && seen.insert(m.id).second) {
                out.push_back(m);
            }
        }
        for (const auto& m : archive_.media_items) {
            if (m.kind == "music" && !dismissed_ids_.count(m.id) &&
                mediaMatchesFilter(m, date_filter, kw_lower) && seen.insert(m.id).second) {
                out.push_back(m);
            }
        }
        return out;
    }

    if (category == "nas") {
        return {};
    }

    // Combined / fallback (deduplicated)
    std::unordered_set<std::string> seen;
    for (const auto& s : interactive_sessions_) {
        for (const auto& m : s.media_items) {
            if (!dismissed_ids_.count(m.id) && mediaMatchesFilter(m, date_filter, kw_lower) &&
                seen.insert(m.id).second) {
                out.push_back(m);
            }
        }
    }
    for (const auto& m : daily_media_) {
        if (!dismissed_ids_.count(m.id) && mediaMatchesFilter(m, date_filter, kw_lower) &&
            seen.insert(m.id).second) {
            out.push_back(m);
        }
    }
    for (const auto& m : archive_.media_items) {
        if (!dismissed_ids_.count(m.id) && mediaMatchesFilter(m, date_filter, kw_lower) &&
            seen.insert(m.id).second) {
            out.push_back(m);
        }
    }
    return out;
}

std::vector<omnidesk::TextChannel> CurationEngine::textChannels() const {
    std::lock_guard<std::mutex> lock(mutex_);
    int news_c = 0, journal_c = 0, reddit_c = 0, english_c = 0, ix_c = 0;
    auto count_item = [&](const omnidesk::TextFeedItem& t) {
        if (dismissed_ids_.count(t.id)) return;
        if (t.category == "news") ++news_c;
        else if (t.category == "journal") ++journal_c;
        else if (t.category == "reddit") ++reddit_c;
        else if (t.category == "english") ++english_c;
    };
    for (const auto& t : daily_texts_) count_item(t);
    for (const auto& t : archive_.text_items) count_item(t);
    for (const auto& s : interactive_sessions_) {
        for (const auto& t : s.text_items) {
            if (!dismissed_ids_.count(t.id)) ++ix_c;
        }
    }
    return {
        {"news", "每日早报 (News)", std::to_string(news_c), false},
        {"journal", "科学期刊精读 (Journal)", std::to_string(journal_c), false},
        {"reddit", "Reddit 每日精选", std::to_string(reddit_c), false},
        {"english", "英语每日伴读 (English)", std::to_string(english_c), false},
        {"interactive", "AI 灵感阅读 (可重温)", std::to_string(ix_c), false},
    };
}

std::vector<omnidesk::TextFeedItem> CurationEngine::queryTextFeed(const std::string& channel_id,
                                                                 const std::string& query) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::string date_filter, kw_lower;
    parseQueryFilter(query, date_filter, kw_lower);

    std::vector<omnidesk::TextFeedItem> out;
    if (channel_id.empty() || channel_id == "interactive") {
        for (auto it = interactive_sessions_.rbegin(); it != interactive_sessions_.rend(); ++it) {
            for (const auto& t : it->text_items) {
                if (!dismissed_ids_.count(t.id) && textMatchesFilter(t, date_filter, kw_lower)) {
                    out.push_back(t);
                }
            }
        }
    }
    if (channel_id != "interactive") {
        for (const auto& t : daily_texts_) {
            if (dismissed_ids_.count(t.id)) continue;
            if (!channel_id.empty() && t.category != channel_id) continue;
            if (textMatchesFilter(t, date_filter, kw_lower)) out.push_back(t);
        }
        for (const auto& t : archive_.text_items) {
            if (dismissed_ids_.count(t.id)) continue;
            if (!channel_id.empty() && t.category != channel_id) continue;
            if (textMatchesFilter(t, date_filter, kw_lower)) out.push_back(t);
        }
    }
    return out;
}

omnidesk::TextArticle CurationEngine::getTextArticle(const std::string& article_id) const {
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& s : interactive_sessions_) {
        auto it = s.articles.find(article_id);
        if (it != s.articles.end()) return it->second;
    }
    auto dit = daily_articles_.find(article_id);
    if (dit != daily_articles_.end()) return dit->second;
    auto ait = archive_.articles.find(article_id);
    if (ait != archive_.articles.end()) return ait->second;
    return {};
}

std::vector<omnidesk::BookItem> CurationEngine::listBooks(const std::string& format_filter,
                                                          const std::string& query) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::string fmt = toLowerAscii(trim(format_filter));
    std::string date_filter, kw_lower;
    parseQueryFilter(query, date_filter, kw_lower);

    std::vector<omnidesk::BookItem> out;
    if ((fmt.empty() || fmt == "all" || fmt == "markdown") && !archive_.english_days.empty()) {
        if (date_filter.empty()) {
            omnidesk::BookItem master;
            master.id = "english-archive-all";
            master.source_id = "plugin-ai-curator";
            master.title = "英语每日精读与翻译归档册";
            master.author = "AI Curation · 2026-09-04 至今";
            master.format = "markdown";
            master.current_page = 1;
            master.total_pages = std::max(1, static_cast<int>(archive_.english_days.size()));
            master.bilingual_ready = true;
            master.vocab_count = static_cast<int>(archive_.vocab_cards.size());
            if (kw_lower.empty() || toLowerAscii(master.title + " " + master.author).find(kw_lower) != std::string::npos) {
                out.push_back(std::move(master));
            }
        }

        for (const auto& [date, bundle] : archive_.english_days) {
            if (!date_filter.empty() && date != date_filter) continue;
            omnidesk::BookItem b;
            b.id = "english-date-" + date;
            b.source_id = "plugin-ai-curator";
            b.title = "英语伴读 · " + date;
            b.author = "AI Curation";
            b.format = "markdown";
            b.current_page = 1;
            b.total_pages = std::max(1, static_cast<int>(bundle.notes.size()));
            b.bilingual_ready = true;
            int vc = 0;
            std::string titles;
            for (const auto& n : bundle.notes) {
                vc += static_cast<int>(n.vocab_cards.size());
                titles += " " + n.title;
            }
            b.vocab_count = vc;
            if (kw_lower.empty() || toLowerAscii(b.title + " " + titles).find(kw_lower) != std::string::npos) {
                out.push_back(std::move(b));
            }
        }
    }

    for (const auto& b : daily_books_) {
        if (!fmt.empty() && fmt != "all" && toLowerAscii(b.format) != fmt) continue;
        if (!date_filter.empty() &&
            b.id.find(date_filter) == std::string::npos &&
            b.title.find(date_filter) == std::string::npos) {
            continue;
        }
        if (!kw_lower.empty() && toLowerAscii(b.title + " " + b.author).find(kw_lower) == std::string::npos) continue;
        out.push_back(b);
    }
    for (const auto& session : interactive_sessions_) {
        for (const auto& b : session.book_items) {
            if (!fmt.empty() && fmt != "all" && toLowerAscii(b.format) != fmt) continue;
            if (!date_filter.empty() &&
                b.id.find(date_filter) == std::string::npos &&
                b.title.find(date_filter) == std::string::npos) {
                continue;
            }
            if (!kw_lower.empty() &&
                toLowerAscii(b.title + " " + b.author).find(kw_lower) == std::string::npos) {
                continue;
            }
            out.push_back(b);
        }
    }
    return out;
}

omnidesk::BookSpread CurationEngine::getBookSpread(const std::string& book_id, int page_number) const {
    std::lock_guard<std::mutex> lock(mutex_);
    omnidesk::BookSpread spread;
    spread.book_id = book_id;

    if (book_id == "english-archive-all" && !archive_.english_days.empty()) {
        int total = static_cast<int>(archive_.english_days.size());
        int idx = std::clamp(page_number, 1, total) - 1;
        auto it = archive_.english_days.begin();
        std::advance(it, idx);
        spread.page_number = idx + 1;
        spread.total_pages = total;
        spread.chapter_title = "英语每日归档 · " + it->first;
        int bidx = 1;
        for (const auto& note : it->second.notes) {
            spread.blocks.push_back({"blk-" + std::to_string(bidx++), "heading", note.title, "", {}});
            for (const auto& blk : note.blocks) {
                auto copy = blk;
                copy.id = "blk-" + std::to_string(bidx++);
                spread.blocks.push_back(std::move(copy));
            }
        }
        return spread;
    }

    if (startsWith(book_id, "english-date-")) {
        std::string date = book_id.substr(std::char_traits<char>::length("english-date-"));
        auto it = archive_.english_days.find(date);
        if (it != archive_.english_days.end() && !it->second.notes.empty()) {
            int total = static_cast<int>(it->second.notes.size());
            int idx = std::clamp(page_number, 1, total) - 1;
            const auto& note = it->second.notes[static_cast<size_t>(idx)];
            spread.page_number = idx + 1;
            spread.total_pages = total;
            spread.chapter_title = note.title;
            spread.blocks = note.blocks;
            return spread;
        }
    }
    for (const auto& session : interactive_sessions_) {
        auto it = session.book_spreads.find(book_id);
        if (it != session.book_spreads.end()) {
            return it->second;
        }
    }
    return spread;
}

std::vector<omnidesk::VocabCard> CurationEngine::listVocab() const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<omnidesk::VocabCard> out = archive_.vocab_cards;
    std::unordered_set<std::string> seen;
    for (const auto& v : out) seen.insert(toLowerAscii(trim(v.word)));
    for (const auto& v : daily_vocabs_) {
        std::string k = toLowerAscii(trim(v.word));
        if (!k.empty() && seen.insert(k).second) out.push_back(v);
    }
    return out;
}

} // namespace ai_curator
