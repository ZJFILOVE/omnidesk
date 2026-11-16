#include "music/music_engine.hpp"
#include "music/config.hpp"

#include <algorithm>
#include <cctype>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <regex>
#include <set>
#include <sstream>
#include <unordered_map>
#include <unordered_set>

namespace fs = std::filesystem;

namespace music_plugin {
namespace {

std::string cbTrim(std::string s) {
    size_t b = 0;
    while (b < s.size() && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    size_t e = s.size();
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

std::string cbLower(std::string s) {
    for (char& ch : s) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return s;
}

std::string cbStr(const nlohmann::json& j, const char* key, const std::string& def = "") {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    return def;
}

int cbInt(const nlohmann::json& j, const char* key, int def = 0) {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_number_integer()) return v.get<int>();
    if (v.is_number_float()) return static_cast<int>(v.get<double>());
    return def;
}

double cbDouble(const nlohmann::json& j, const char* key, double def = 0.0) {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_number()) return v.get<double>();
    return def;
}

bool cbBool(const nlohmann::json& j, const char* key, bool def = false) {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number_integer()) return v.get<int>() != 0;
    return def;
}

bool cbIsDigits(const std::string& s) {
    return !s.empty() && std::all_of(s.begin(), s.end(), [](unsigned char c) {
        return std::isdigit(c) != 0;
    });
}

bool cbIsProtectedPlaylistNameOrId(const std::string& id, const std::string& name) {
    const std::string cid = cbTrim(id);
    if (cid == MusicEngine::kProtectedDailyPlaylistId) return true;
    std::string folded;
    for (unsigned char ch : cbLower(cbTrim(name))) {
        if (std::isalnum(ch)) folded.push_back(static_cast<char>(ch));
    }
    return folded.find("chatgptdaily5") != std::string::npos ||
           cbLower(cbTrim(name)).rfind("chatgpt daily 5", 0) == 0;
}

fs::path cbResolveDataRoot(const std::string& plugin_data_dir) {
    if (plugin_data_dir.empty()) {
        return fs::path(expandUserPath("~/.local/share/omnidesk"));
    }
    fs::path p(plugin_data_dir);
    if (p.has_parent_path() && p.parent_path().filename() == "plugin-data") {
        return p.parent_path().parent_path();
    }
    return p;
}

std::string cbExtractSongIdFromUrlOrId(const std::string& raw_id, const std::string& url) {
    if (cbIsDigits(raw_id)) return raw_id;
    auto fromPrefix = [](const std::string& s) -> std::string {
        if (s.rfind("am-", 0) == 0 && s.size() > 6 && s[5] == '-') {
            std::string rest = s.substr(6);
            if (cbIsDigits(rest)) return rest;
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && cbIsDigits(rest.substr(dash + 1))) {
                return rest.substr(dash + 1);
            }
        }
        if (s.rfind("ix-am-", 0) == 0) {
            std::string rest = s.substr(6);
            if (cbIsDigits(rest)) return rest;
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && cbIsDigits(rest.substr(dash + 1))) {
                return rest.substr(dash + 1);
            }
        }
        return "";
    };
    if (std::string p = fromPrefix(raw_id); !p.empty()) return p;

    size_t song_pos = url.find("/song/");
    if (song_pos != std::string::npos) {
        size_t p = song_pos + 6;
        size_t q = url.find_first_of("?#", p);
        std::string tail = (q == std::string::npos) ? url.substr(p) : url.substr(p, q - p);
        while (!tail.empty() && tail.back() == '/') tail.pop_back();
        size_t slash = tail.rfind('/');
        std::string cand = (slash == std::string::npos) ? tail : tail.substr(slash + 1);
        if (cbIsDigits(cand)) return cand;
    }
    return "";
}

bool cbParseCuratedMediaItem(const nlohmann::json& j,
                             const std::string& default_mode,
                             CuratedTrackItem& out) {
    if (!j.is_object()) return false;
    const std::string kind = cbStr(j, "kind", "music");
    const auto extra = (j.contains("extra") && j["extra"].is_object())
                           ? j["extra"]
                           : nlohmann::json::object();
    const std::string dom = cbLower(cbStr(extra, "domain", kind));
    if (kind != "music" && dom != "music") return false;

    const std::string item_id = cbStr(j, "id");
    const std::string play_url = cbStr(j, "play_url", cbStr(j, "url"));
    std::string song_id = cbStr(extra, "apple_music_song_id", cbStr(extra, "song_id"));
    if (!cbIsDigits(song_id)) {
        song_id = cbExtractSongIdFromUrlOrId(item_id, play_url);
    }
    if (!cbIsDigits(song_id)) return false;

    out = CuratedTrackItem{};
    out.item_id = item_id.empty() ? ("am-cn-" + song_id) : item_id;
    out.mode = cbStr(extra, "curation_mode", default_mode);
    out.business_date = cbStr(extra, "business_date");
    out.position = cbInt(extra, "position", 0);
    out.canonical_id = cbStr(extra, "canonical_id");
    out.why_recommended = cbStr(extra, "why_recommended");
    out.listening_cue = cbStr(extra, "listening_or_reading_cue", cbStr(extra, "listening_cue"));
    out.previously_recommended_on = cbStr(extra, "previously_recommended_on");
    out.revisit_badge = cbStr(extra, "revisit_badge");
    if (out.revisit_badge.empty() && !out.previously_recommended_on.empty()) {
        out.revisit_badge = "经典重温 · " + out.previously_recommended_on;
    }

    out.track.id = song_id;
    out.track.kind = "song";
    out.track.title = cbStr(j, "title", song_id);
    out.track.artist = cbStr(extra, "artist", cbStr(j, "creator"));
    out.track.album = cbStr(extra, "album");
    out.track.cover_url = cbStr(j, "cover_url");
    out.track.storefront = cbLower(cbStr(extra, "storefront", "cn"));
    if (out.track.storefront.empty()) out.track.storefront = "cn";
    out.track.quality_label = cbStr(j, "quality_label", "Apple Music CN · ALAC");
    out.track.duration_sec = cbDouble(j, "duration_sec", 0.0);
    out.track.has_lyrics = cbBool(j, "has_bilingual_sub", false);
    return true;
}

std::string makeSessionTimestampId() {
    std::time_t now = std::time(nullptr) + 8 * 3600;
    std::tm tm{};
    gmtime_r(&now, &tm);
    char buf[32];
    std::strftime(buf, sizeof(buf), "ix-music-%Y%m%d-%H%M%S", &tm);
    return std::string(buf);
}

std::string utf8Clip(const std::string& s, size_t max_bytes) {
    if (s.size() <= max_bytes) return s;
    size_t cut = max_bytes;
    while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0) == 0x80) --cut;
    return s.substr(0, cut);
}

std::vector<std::string> expandMoodSearchTerms(const std::string& prompt) {
    std::vector<std::string> terms;
    const std::string trimmed = cbTrim(prompt);
    if (!trimmed.empty()) terms.push_back(trimmed);

    const std::string low = cbLower(trimmed);
    auto add = [&](const char* t) {
        if (std::find(terms.begin(), terms.end(), t) == terms.end()) {
            terms.emplace_back(t);
        }
    };

    if (trimmed.find("雨") != std::string::npos || trimmed.find("器乐") != std::string::npos ||
        low.find("ambient") != std::string::npos || low.find("piano") != std::string::npos) {
        add("Nils Frahm");
        add("Ólafur Arnalds");
        add("Ryuichi Sakamoto");
    }
    if (trimmed.find("爵士") != std::string::npos || low.find("jazz") != std::string::npos ||
        trimmed.find("深夜") != std::string::npos) {
        add("Bill Evans");
        add("Chet Baker");
        add("Norah Jones");
    }
    if (trimmed.find("电子") != std::string::npos || trimmed.find("编码") != std::string::npos ||
        trimmed.find("专注") != std::string::npos || low.find("synth") != std::string::npos) {
        add("Tycho");
        add("Bonobo");
        add("Four Tet");
    }
    if (trimmed.find("民谣") != std::string::npos || trimmed.find("清晨") != std::string::npos ||
        trimmed.find("原声") != std::string::npos || low.find("folk") != std::string::npos) {
        add("Sufjan Stevens");
        add("Kings of Convenience");
        add("Nick Drake");
    }
    if (trimmed.find("摇滚") != std::string::npos || trimmed.find("独立") != std::string::npos ||
        low.find("indie") != std::string::npos) {
        add("Radiohead");
        add("Yo La Tengo");
        add("Beach House");
    }
    if (terms.size() == 1) {
        add("Khruangbin");
        add(" Ryuichi Sakamoto");
    }
    return terms;
}

nlohmann::json extractJsonObjectOrArray(const std::string& text) {
    auto direct = nlohmann::json::parse(text, nullptr, false);
    if (!direct.is_discarded()) return direct;

    size_t f_obj = text.find('{');
    size_t l_obj = text.rfind('}');
    if (f_obj != std::string::npos && l_obj != std::string::npos && l_obj > f_obj) {
        auto parsed = nlohmann::json::parse(text.substr(f_obj, l_obj - f_obj + 1), nullptr, false);
        if (!parsed.is_discarded()) return parsed;
    }
    size_t f_arr = text.find('[');
    size_t l_arr = text.rfind(']');
    if (f_arr != std::string::npos && l_arr != std::string::npos && l_arr > f_arr) {
        auto parsed = nlohmann::json::parse(text.substr(f_arr, l_arr - f_arr + 1), nullptr, false);
        if (!parsed.is_discarded()) return parsed;
    }
    return nlohmann::json();
}

} // namespace

CurationWorkspaceSnapshot MusicEngine::loadCurationSnapshot(const std::string& requested_date,
                                                            bool enrich_artwork) {
    CurationWorkspaceSnapshot snap;
    const fs::path data_root = cbResolveDataRoot(currentConfig().data_dir);
    const fs::path curation_dir = data_root / "curation";

    std::set<std::string, std::greater<std::string>> date_set;
    std::unordered_map<std::string, std::vector<CuratedTrackItem>> archive_by_date;
    int total_hydrated = 0;

    // 1. Load local hydrated_index.json & daily bundles as fast baseline
    std::error_code ec;
    const fs::path hydrated_path = curation_dir / "hydrated_index.json";
    if (fs::exists(hydrated_path, ec)) {
        std::ifstream ifs(hydrated_path);
        auto hj = nlohmann::json::parse(ifs, nullptr, false);
        if (hj.is_object() && hj.contains("media_items") && hj["media_items"].is_array()) {
            for (const auto& mj : hj["media_items"]) {
                CuratedTrackItem item;
                if (!cbParseCuratedMediaItem(mj, "daily_immutable", item)) continue;
                if (!item.business_date.empty()) {
                    date_set.insert(item.business_date);
                    archive_by_date[item.business_date].push_back(std::move(item));
                    ++total_hydrated;
                }
            }
        }
    }

    const fs::path daily_dir = curation_dir / "daily";
    if (fs::exists(daily_dir, ec)) {
        for (const auto& entry : fs::recursive_directory_iterator(daily_dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
            std::ifstream ifs(entry.path());
            auto dj = nlohmann::json::parse(ifs, nullptr, false);
            if (!dj.is_object() || !dj.contains("media_items") || !dj["media_items"].is_array()) continue;
            const std::string fallback_date = cbStr(dj, "business_date");
            for (const auto& mj : dj["media_items"]) {
                CuratedTrackItem item;
                if (!cbParseCuratedMediaItem(mj, "daily_immutable", item)) continue;
                if (item.business_date.empty()) item.business_date = fallback_date;
                if (item.business_date.empty()) continue;
                date_set.insert(item.business_date);
                auto& vec = archive_by_date[item.business_date];
                bool exists = std::any_of(vec.begin(), vec.end(), [&](const CuratedTrackItem& x) {
                    return x.track.id == item.track.id;
                });
                if (!exists) {
                    vec.push_back(std::move(item));
                    ++total_hydrated;
                }
            }
        }
    }

    // 2. Also query live recommend.feed.v1 capability if available so in-memory updates are reflected immediately
    if (ctx_.capabilities().has("recommend.feed.v1")) {
        auto dates_res = ctx_.capabilities().call("recommend.feed.v1", {
            {"action", "list_archive_dates"},
            {"domain", "music"},
        });
        if (dates_res.ok && dates_res.data.is_object() &&
            dates_res.data.contains("dates") && dates_res.data["dates"].is_array()) {
            for (const auto& row : dates_res.data["dates"]) {
                std::string d = cbStr(row, "date");
                if (!d.empty() && cbInt(row, "music", 0) > 0) date_set.insert(d);
            }
        }
    }

    snap.available_dates.assign(date_set.begin(), date_set.end());
    snap.total_dates = static_cast<int>(snap.available_dates.size());
    snap.total_hydrated_tracks = total_hydrated;

    if (!requested_date.empty() && date_set.count(requested_date) > 0) {
        snap.selected_date = requested_date;
    } else if (!snap.available_dates.empty()) {
        snap.selected_date = snap.available_dates.front();
    }

    if (!snap.selected_date.empty()) {
        bool loaded_from_rpc = false;
        if (ctx_.capabilities().has("recommend.feed.v1")) {
            auto b_res = ctx_.capabilities().call("recommend.feed.v1", {
                {"action", "get_date_bundle"},
                {"business_date", snap.selected_date},
            });
            if (b_res.ok && b_res.data.is_object() &&
                b_res.data.contains("media_items") && b_res.data["media_items"].is_array()) {
                for (const auto& mj : b_res.data["media_items"]) {
                    CuratedTrackItem item;
                    if (cbParseCuratedMediaItem(mj, "daily_immutable", item)) {
                        snap.daily_tracks.push_back(std::move(item));
                    }
                }
                loaded_from_rpc = !snap.daily_tracks.empty();
            }
        }
        if (!loaded_from_rpc) {
            auto it = archive_by_date.find(snap.selected_date);
            if (it != archive_by_date.end()) {
                snap.daily_tracks = it->second;
            }
        }
        std::sort(snap.daily_tracks.begin(), snap.daily_tracks.end(),
                  [](const CuratedTrackItem& a, const CuratedTrackItem& b) {
                      if (a.position != b.position && a.position > 0 && b.position > 0) {
                          return a.position < b.position;
                      }
                      return a.item_id < b.item_id;
                  });
    }

    // 3. Load interactive ephemeral sessions (newest-first)
    const fs::path ix_dir = curation_dir / "interactive" / "sessions";
    if (fs::exists(ix_dir, ec)) {
        std::vector<fs::path> files;
        for (const auto& entry : fs::directory_iterator(ix_dir, ec)) {
            if (entry.is_regular_file() && entry.path().extension() == ".json") {
                files.push_back(entry.path());
            }
        }
        std::sort(files.begin(), files.end(), [&](const fs::path& a, const fs::path& b) {
            std::error_code ec1, ec2;
            auto t1 = fs::last_write_time(a, ec1);
            auto t2 = fs::last_write_time(b, ec2);
            if (!ec1 && !ec2 && t1 != t2) return t1 > t2;
            return a.filename().string() > b.filename().string();
        });
        for (const auto& fp : files) {
            std::ifstream ifs(fp);
            auto sj = nlohmann::json::parse(ifs, nullptr, false);
            if (!sj.is_object()) continue;
            CuratedInteractiveSession sess;
            sess.session_id = cbStr(sj, "session_id", fp.stem().string());
            sess.title = cbStr(sj, "title", "AI 灵感电台");
            sess.user_prompt = cbStr(sj, "user_prompt");
            sess.repeat_policy = cbStr(sj, "repeat_policy", "allow_any");
            if (sj.contains("media_items") && sj["media_items"].is_array()) {
                for (const auto& mj : sj["media_items"]) {
                    CuratedTrackItem item;
                    if (cbParseCuratedMediaItem(mj, "interactive_ephemeral", item)) {
                        sess.tracks.push_back(std::move(item));
                    }
                }
            }
            if (!sess.tracks.empty()) {
                snap.interactive_sessions.push_back(std::move(sess));
            }
            if (snap.interactive_sessions.size() >= 12) break;
        }
    }

    // 4. Load Apple Music playlist sync & guardrail state
    auto pl_res = handlePlaylistRpc({{"action", "status"}});
    if (pl_res.ok && pl_res.data.is_object()) {
        snap.has_live_write_auth = cbBool(pl_res.data, "has_live_apple_music_auth", false);
        if (pl_res.data.contains("protected_daily_playlist") &&
            pl_res.data["protected_daily_playlist"].is_object()) {
            const auto& pd = pl_res.data["protected_daily_playlist"];
            snap.protected_playlist_id = cbStr(pd, "playlist_id", kProtectedDailyPlaylistId);
            snap.protected_playlist_name = cbStr(pd, "name", kProtectedDailyPlaylistName);
            snap.protected_synced_dates = cbInt(pd, "synced_dates_count", 0);
            snap.protected_cumulative_tracks = cbInt(pd, "cumulative_track_count", 0);
        }
        if (pl_res.data.contains("interactive_playlists") &&
            pl_res.data["interactive_playlists"].is_array()) {
            for (const auto& ip : pl_res.data["interactive_playlists"]) {
                if (!ip.is_object()) continue;
                const std::string name = cbStr(ip, "name");
                if (name == kDefaultInteractivePlaylistName || snap.interactive_apple_playlist_id.empty()) {
                    snap.interactive_playlist_name = name.empty() ? kDefaultInteractivePlaylistName : name;
                    snap.interactive_apple_playlist_id = cbStr(ip, "apple_music_playlist_id");
                    snap.interactive_cloud_synced = cbBool(ip, "cloud_synced", false);
                    if (ip.contains("track_ids") && ip["track_ids"].is_array()) {
                        snap.interactive_playlist_tracks = static_cast<int>(ip["track_ids"].size());
                    }
                    if (name == kDefaultInteractivePlaylistName) break;
                }
            }
        }
    }

    // 5. Enrich cover artwork & album metadata for visible daily + interactive tracks
    auto applyCachedMeta = [&](std::vector<CuratedTrackItem>& list,
                               std::vector<std::pair<std::string, std::string>>& missing_out) {
        std::lock_guard<std::mutex> lk(curation_mutex_);
        for (auto& item : list) {
            auto it = curated_meta_cache_.find(item.track.id);
            if (it != curated_meta_cache_.end()) {
                if (item.track.cover_url.empty()) item.track.cover_url = it->second.cover_url;
                if (item.track.album.empty()) item.track.album = it->second.album;
                if (item.track.duration_sec <= 0.0) item.track.duration_sec = it->second.duration_sec;
                if (!it->second.quality_label.empty()) item.track.quality_label = it->second.quality_label;
            } else if (item.track.cover_url.empty() && missing_out.size() < 40) {
                missing_out.emplace_back(item.track.id, item.track.storefront);
            }
        }
    };

    std::vector<std::pair<std::string, std::string>> missing_meta;
    applyCachedMeta(snap.daily_tracks, missing_meta);
    for (auto& sess : snap.interactive_sessions) {
        applyCachedMeta(sess.tracks, missing_meta);
    }

    if (enrich_artwork && !missing_meta.empty() &&
        (adam_.allowLocalAdamDrmFallback() || !adam_.cachedOrEnvDeveloperToken().empty() || adam_.isPortReachable())) {
        std::unordered_map<std::string, MusicTrack> resolved;
        std::string dummy_err;
        if (adam_.fetchBatchMetadata(missing_meta, resolved, dummy_err) && !resolved.empty()) {
            {
                std::lock_guard<std::mutex> lk(curation_mutex_);
                for (const auto& [id, meta] : resolved) {
                    curated_meta_cache_[id] = meta;
                }
            }
            std::vector<std::pair<std::string, std::string>> unused;
            applyCachedMeta(snap.daily_tracks, unused);
            for (auto& sess : snap.interactive_sessions) {
                applyCachedMeta(sess.tracks, unused);
            }
        }
    }

    return snap;
}

InteractiveWritebackOutcome MusicEngine::curateAndWriteBackInteractive(
    const std::string& user_prompt,
    const std::string& target_playlist_name,
    bool sync_playlist,
    bool enqueue_now) {
    InteractiveWritebackOutcome out;
    out.prompt = cbTrim(user_prompt);
    out.target_playlist_name = cbTrim(target_playlist_name).empty()
                                   ? std::string(kDefaultInteractivePlaylistName)
                                   : cbTrim(target_playlist_name);

    if (out.prompt.empty()) {
        out.error_code = "invalid_argument";
        out.error = "请输入听歌场景、风格或艺人提示词";
        return out;
    }

    // Strict guardrail: interactive mode NEVER touches ChatGPT Daily 5 (p.YJXV7dEIeb3GzZX)
    if (cbIsProtectedPlaylistNameOrId("", out.target_playlist_name)) {
        out.error_code = "protected_daily_playlist";
        out.error = "交互式 AI 反写严禁修改受保护的每日精选歌单 ChatGPT Daily 5 (p.YJXV7dEIeb3GzZX)";
        return out;
    }

    const fs::path data_root = cbResolveDataRoot(currentConfig().data_dir);
    const fs::path hydrated_path = data_root / "curation" / "hydrated_index.json";

    // Gather candidate pool from:
    //   (a) Historical curated archive (repeats explicitly allowed in interactive_ephemeral!)
    //   (b) Live Apple Music CN catalog search
    std::vector<CuratedTrackItem> pool;
    std::unordered_set<std::string> seen_ids;
    std::unordered_map<std::string, CuratedTrackItem> archive_by_song_id;

    std::error_code ec;
    if (fs::exists(hydrated_path, ec)) {
        std::ifstream ifs(hydrated_path);
        auto hj = nlohmann::json::parse(ifs, nullptr, false);
        if (hj.is_object() && hj.contains("media_items") && hj["media_items"].is_array()) {
            for (const auto& mj : hj["media_items"]) {
                CuratedTrackItem item;
                if (cbParseCuratedMediaItem(mj, "interactive_ephemeral", item)) {
                    item.previously_recommended_on = item.business_date;
                    if (!item.business_date.empty()) {
                        item.revisit_badge = "经典重温 · " + item.business_date;
                    }
                    archive_by_song_id[item.track.id] = item;
                }
            }
        }
    }

    if (ctx_.capabilities().has("recommend.feed.v1")) {
        auto arch = ctx_.capabilities().call("recommend.feed.v1", {
            {"action", "search_archive"},
            {"query", out.prompt},
            {"domain", "music"},
            {"limit", 8},
        });
        if (arch.ok && arch.data.is_object() &&
            arch.data.contains("media_items") && arch.data["media_items"].is_array()) {
            for (const auto& mj : arch.data["media_items"]) {
                CuratedTrackItem item;
                if (cbParseCuratedMediaItem(mj, "interactive_ephemeral", item) &&
                    seen_ids.insert(item.track.id).second) {
                    item.previously_recommended_on = item.business_date;
                    if (!item.business_date.empty()) {
                        item.revisit_badge = "经典重温 · " + item.business_date;
                    }
                    pool.push_back(std::move(item));
                }
            }
        }
    }

    const bool wants_revisit =
        out.prompt.find("重温") != std::string::npos ||
        out.prompt.find("遗珠") != std::string::npos ||
        out.prompt.find("经典") != std::string::npos ||
        out.prompt.find("归档") != std::string::npos;

    // Live Apple Music CN catalog discovery across expanded mood/query terms
    const auto search_terms = expandMoodSearchTerms(out.prompt);
    for (const auto& term : search_terms) {
        if (pool.size() >= 18) break;
        FeedOutcome cat = adam_.searchCatalog(term, "cn", "songs");
        if (!cat.ok) continue;
        for (const auto& tr : cat.tracks) {
            if (!cbIsDigits(tr.id) || !seen_ids.insert(tr.id).second) continue;
            CuratedTrackItem item;
            auto ait = archive_by_song_id.find(tr.id);
            if (ait != archive_by_song_id.end()) {
                item = ait->second;
                if (!tr.cover_url.empty()) item.track.cover_url = tr.cover_url;
                if (!tr.album.empty()) item.track.album = tr.album;
                if (tr.duration_sec > 0.0) item.track.duration_sec = tr.duration_sec;
            } else {
                item.track = tr;
                item.track.storefront = "cn";
                if (item.track.quality_label.empty()) item.track.quality_label = "Apple Music CN · ALAC";
                item.mode = "interactive_ephemeral";
                item.canonical_id = tr.artist + "::" + tr.title;
            }
            pool.push_back(std::move(item));
            if (pool.size() >= 20) break;
        }
    }

    // Token-based fallback over verified archive when user asks for archive revisit or catalog is offline
    if (pool.size() < 6 && !archive_by_song_id.empty()) {
        std::vector<CuratedTrackItem> archive_list;
        archive_list.reserve(archive_by_song_id.size());
        for (const auto& [sid, it] : archive_by_song_id) archive_list.push_back(it);
        std::sort(archive_list.begin(), archive_list.end(),
                  [](const CuratedTrackItem& a, const CuratedTrackItem& b) {
                      return a.business_date > b.business_date;
                  });
        for (const auto& item : archive_list) {
            if (pool.size() >= 10) break;
            const std::string hay = cbLower(item.track.title + " " + item.track.artist + " " +
                                            item.why_recommended + " " + item.listening_cue);
            bool match = wants_revisit;
            for (const auto& term : search_terms) {
                if (!term.empty() && hay.find(cbLower(term)) != std::string::npos) {
                    match = true;
                    break;
                }
            }
            if (match && seen_ids.insert(item.track.id).second) {
                pool.push_back(item);
            }
        }
        if (pool.empty()) {
            for (const auto& item : archive_list) {
                if (pool.size() >= 6) break;
                if (seen_ids.insert(item.track.id).second) pool.push_back(item);
            }
        }
    }

    // Optional LLM selection & tailored Chinese editorial generation via llm.chat.v1
    std::vector<CuratedTrackItem> chosen;
    if (!pool.empty() && ctx_.capabilities().has("llm.chat.v1")) {
        std::ostringstream prompt_ss;
        prompt_ss << "你是 OmniDesk 的资深音乐策展编辑。用户提出了按需点歌需求：「" << out.prompt << "」。\n"
                  << "请从以下已验证 Apple Music 中国区候选曲目中挑选最契合的 5 首，并为每首撰写具体、真诚的中文乐评（严禁空泛套话）：\n";
        const size_t max_cand = std::min<size_t>(pool.size(), 15);
        for (size_t i = 0; i < max_cand; ++i) {
            prompt_ss << (i + 1) << ". song_id=" << pool[i].track.id
                      << " | " << pool[i].track.artist << " - " << pool[i].track.title;
            if (!pool[i].track.album.empty()) prompt_ss << " (" << pool[i].track.album << ")";
            prompt_ss << "\n";
        }
        prompt_ss << "\n请仅输出严格 JSON 对象，格式如下：\n"
                  << "{\"title\":\"歌单短标题\",\"picks\":[{\"song_id\":\"...\",\"why_recommended\":\"60-110字中文推荐理由，点明编曲配器、音色层次与契合用户场景之处\",\"listening_cue\":\"35-65字收听切入点或细节提示\"}]}";

        auto llm_res = ctx_.capabilities().call("llm.chat.v1", {
            {"action", "complete"},
            {"messages", nlohmann::json::array({
                {{"role", "user"}, {"content", prompt_ss.str()}},
            })},
            {"temperature", 0.4},
        });
        if (llm_res.ok && llm_res.data.is_object()) {
            std::string reply = cbStr(llm_res.data, "reply", cbStr(llm_res.data, "content", cbStr(llm_res.data, "text")));
            auto parsed = extractJsonObjectOrArray(reply);
            if (parsed.is_object()) {
                std::string custom_title = cbTrim(cbStr(parsed, "title"));
                if (!custom_title.empty()) out.title = custom_title;
                if (parsed.contains("picks") && parsed["picks"].is_array()) {
                    std::unordered_set<std::string> picked_ids;
                    for (const auto& pk : parsed["picks"]) {
                        std::string sid = cbTrim(cbStr(pk, "song_id"));
                        auto pit = std::find_if(pool.begin(), pool.end(), [&](const CuratedTrackItem& x) {
                            return x.track.id == sid;
                        });
                        if (pit != pool.end() && picked_ids.insert(sid).second) {
                            CuratedTrackItem item = *pit;
                            std::string why = cbTrim(cbStr(pk, "why_recommended"));
                            std::string cue = cbTrim(cbStr(pk, "listening_cue", cbStr(pk, "listening_or_reading_cue")));
                            if (!why.empty()) item.why_recommended = why;
                            if (!cue.empty()) item.listening_cue = cue;
                            chosen.push_back(std::move(item));
                            if (chosen.size() >= 6) break;
                        }
                    }
                }
            }
        }
    }

    if (chosen.empty()) {
        for (size_t i = 0; i < pool.size() && chosen.size() < 5; ++i) {
            chosen.push_back(pool[i]);
        }
    }

    // Ensure every candidate has non-empty, track-specific Chinese editorial notes
    for (auto& item : chosen) {
        if (item.why_recommended.empty()) {
            item.why_recommended =
                "围绕「" + utf8Clip(out.prompt, 36) + "」甄选：" + item.track.artist + " 在《" + item.track.title +
                "》" + (item.track.album.empty() ? "" : ("（收录于专辑《" + item.track.album + "》）")) +
                "中以细腻克制的配器铺陈空间层次，律动舒展而不喧宾夺主，适合沉浸聆听。";
        }
        if (item.listening_cue.empty()) {
            item.listening_cue =
                "建议开启 FairPlay ALAC 无损模式，留意《" + item.track.title +
                "》前奏进入时的器乐结像、低频呼吸感与中段情绪推进。";
        }
    }

    // Mandatory CN Catalog Preflight Verification (fail-closed on unverified candidates!)
    std::vector<std::string> cand_ids;
    for (const auto& c : chosen) {
        if (cbIsDigits(c.track.id)) cand_ids.push_back(c.track.id);
    }
    if (cand_ids.empty()) {
        out.error_code = "no_verified_candidates";
        out.error = "未找到可验证的 Apple Music 中国区曲目，已按 fail-closed 策略中止反写";
        return out;
    }

    std::vector<MusicTrack> verified_tracks;
    std::vector<std::string> unplayable_ids;
    std::string preflight_err;
    bool live_preflight_ok = adam_.preflightCatalogSongs(cand_ids, "cn", verified_tracks, unplayable_ids, preflight_err);
    if (!live_preflight_ok && adam_.cachedOrEnvDeveloperToken().empty()) {
        // Attempt fetching public Apple web JWT once for live CN catalog preflight
        adam_.getDeveloperToken(false);
        if (!adam_.cachedOrEnvDeveloperToken().empty()) {
            live_preflight_ok = adam_.preflightCatalogSongs(cand_ids, "cn", verified_tracks, unplayable_ids, preflight_err);
        }
    }

    std::vector<CuratedTrackItem> final_items;
    if (live_preflight_ok) {
        std::unordered_map<std::string, MusicTrack> ver_map;
        for (const auto& vt : verified_tracks) ver_map[vt.id] = vt;
        for (auto& item : chosen) {
            auto vit = ver_map.find(item.track.id);
            if (vit == ver_map.end()) continue;
            if (!vit->second.title.empty()) item.track.title = vit->second.title;
            if (!vit->second.artist.empty()) item.track.artist = vit->second.artist;
            if (!vit->second.album.empty()) item.track.album = vit->second.album;
            if (!vit->second.cover_url.empty()) item.track.cover_url = vit->second.cover_url;
            if (vit->second.duration_sec > 0.0) item.track.duration_sec = vit->second.duration_sec;
            if (!vit->second.quality_label.empty()) item.track.quality_label = vit->second.quality_label;
            item.track.storefront = "cn";
            {
                std::lock_guard<std::mutex> lk(curation_mutex_);
                curated_meta_cache_[item.track.id] = item.track;
            }
            final_items.push_back(std::move(item));
        }
    } else {
        // Offline / test fallback ONLY for tracks already verified in committed CN archive manifests
        for (auto& item : chosen) {
            if (archive_by_song_id.count(item.track.id) > 0) {
                final_items.push_back(std::move(item));
            }
        }
    }

    if (final_items.empty()) {
        out.error_code = "no_verified_candidates";
        out.error = preflight_err.empty()
                        ? "候选曲目未通过 Apple Music 中国区可播性预检 (no_verified_candidates)"
                        : ("中国区曲库预检未通过: " + preflight_err);
        return out;
    }

    // Stamp read-only historical provenance via recommend.feed.v1 lookup_provenance
    if (ctx_.capabilities().has("recommend.feed.v1")) {
        nlohmann::json lookup_arr = nlohmann::json::array();
        for (const auto& item : final_items) {
            lookup_arr.push_back({
                {"id", "ix-am-cn-" + item.track.id},
                {"source_id", "plugin-ai-curator"},
                {"kind", "music"},
                {"title", item.track.title},
                {"creator", item.track.artist},
                {"play_url", "https://music.apple.com/cn/song/" + item.track.id},
                {"extra", {{"apple_music_song_id", item.track.id}}},
            });
        }
        auto prov = ctx_.capabilities().call("recommend.feed.v1", {
            {"action", "lookup_provenance"},
            {"items", std::move(lookup_arr)},
        });
        if (prov.ok && prov.data.is_object() &&
            prov.data.contains("matches") && prov.data["matches"].is_array()) {
            std::unordered_map<std::string, std::string> id_to_date;
            for (const auto& m : prov.data["matches"]) {
                std::string mid = cbStr(m, "id");
                std::string d = cbStr(m, "previously_recommended_on");
                if (!mid.empty() && !d.empty()) id_to_date[mid] = d;
            }
            for (auto& item : final_items) {
                auto it = id_to_date.find("ix-am-cn-" + item.track.id);
                if (it != id_to_date.end()) {
                    item.previously_recommended_on = it->second;
                    item.revisit_badge = "经典重温 · " + it->second;
                }
            }
        }
    }

    out.session_id = makeSessionTimestampId();
    if (out.title.empty()) {
        out.title = "AI 灵感电台 · " + utf8Clip(out.prompt, 36);
    }

    nlohmann::json media_items_json = nlohmann::json::array();
    int pos = 1;
    for (auto& item : final_items) {
        item.position = pos++;
        item.item_id = "ix-am-cn-" + item.track.id;
        item.mode = "interactive_ephemeral";
        if (!item.previously_recommended_on.empty()) ++out.revisit_count;

        nlohmann::json extra = {
            {"curation_mode", "interactive_ephemeral"},
            {"domain", "music"},
            {"apple_music_song_id", item.track.id},
            {"storefront", "cn"},
            {"artist", item.track.artist},
            {"album", item.track.album},
            {"canonical_id", item.track.artist + "::" + item.track.title},
            {"position", item.position},
            {"why_recommended", item.why_recommended},
            {"listening_or_reading_cue", item.listening_cue},
        };
        if (!item.previously_recommended_on.empty()) {
            extra["previously_recommended_on"] = item.previously_recommended_on;
            extra["revisit_badge"] = item.revisit_badge;
        }
        media_items_json.push_back({
            {"id", item.item_id},
            {"source_id", "plugin-ai-curator"},
            {"title", item.track.title},
            {"creator", item.track.artist},
            {"kind", "music"},
            {"source_badge", item.revisit_badge.empty() ? "AI 灵感推荐 · 可重温" : item.revisit_badge},
            {"play_url", "https://music.apple.com/cn/song/" + item.track.id},
            {"cover_url", item.track.cover_url},
            {"quality_label", item.track.quality_label.empty() ? "Apple Music CN · ALAC" : item.track.quality_label},
            {"duration_sec", item.track.duration_sec},
            {"has_bilingual_sub", true},
            {"extra", std::move(extra)},
        });
    }

    // Reverse-write 1: OmniDesk workbench feed (recommend.feed.v1 -> push_interactive_set)
    nlohmann::json push_payload = {
        {"action", "push_interactive_set"},
        {"mode", "interactive_ephemeral"},
        {"record_permanent_history", false},
        {"session_id", out.session_id},
        {"title", out.title},
        {"user_prompt", out.prompt},
        {"repeat_policy", "allow_any"},
        {"replace_session_items", true},
        {"media_items", media_items_json},
    };
    if (ctx_.capabilities().has("recommend.feed.v1")) {
        auto feed_res = ctx_.capabilities().call("recommend.feed.v1", push_payload);
        out.feed_written = feed_res.ok;
        if (!feed_res.ok && out.error.empty()) {
            out.error = feed_res.error;
        }
    } else {
        // Standalone fallback: write ephemeral session file directly without touching permanent ledgers
        const fs::path sess_dir = data_root / "curation" / "interactive" / "sessions";
        fs::create_directories(sess_dir, ec);
        const fs::path sess_file = sess_dir / (out.session_id + ".json");
        std::ofstream ofs(sess_file, std::ios::trunc);
        if (ofs.is_open()) {
            nlohmann::json doc = {
                {"schema_version", 2},
                {"mode", "interactive_ephemeral"},
                {"record_permanent_history", false},
                {"session_id", out.session_id},
                {"title", out.title},
                {"user_prompt", out.prompt},
                {"repeat_policy", "allow_any"},
                {"media_items", media_items_json},
                {"text_items", nlohmann::json::array()},
                {"articles", nlohmann::json::object()},
                {"book_items", nlohmann::json::array()},
                {"book_spreads", nlohmann::json::object()},
            };
            ofs << doc.dump(2) << "\n";
            out.feed_written = true;
        }
    }

    // Reverse-write 2: Apple Music interactive playlist ("OmniDesk AI 灵感电台")
    if (sync_playlist) {
        nlohmann::json pl_data;
        std::string pl_err;
        if (syncInteractiveTracksToPlaylist(final_items, out.target_playlist_name, enqueue_now, pl_data, pl_err)) {
            out.playlist_cloud_synced = cbBool(pl_data, "cloud_synced", false);
            out.playlist_local_staged = cbBool(pl_data, "local_staged", false);
            out.target_apple_playlist_id = cbStr(pl_data, "apple_music_playlist_id");
        } else if (enqueue_now) {
            std::vector<MusicTrack> q;
            for (const auto& item : final_items) q.push_back(item.track);
            setQueueAndPlay(std::move(q), 0);
        }
    } else if (enqueue_now) {
        std::vector<MusicTrack> q;
        for (const auto& item : final_items) q.push_back(item.track);
        setQueueAndPlay(std::move(q), 0);
    }

    out.tracks = std::move(final_items);
    out.ok = out.feed_written || out.playlist_cloud_synced || out.playlist_local_staged;
    notifyObservers();
    return out;
}

bool MusicEngine::syncInteractiveTracksToPlaylist(
    const std::vector<CuratedTrackItem>& items,
    const std::string& playlist_name,
    bool enqueue_now,
    nlohmann::json& result_out,
    std::string& error) {
    result_out = nlohmann::json::object();
    error.clear();
    const std::string eff_name = cbTrim(playlist_name).empty()
                                     ? std::string(kDefaultInteractivePlaylistName)
                                     : cbTrim(playlist_name);
    if (cbIsProtectedPlaylistNameOrId("", eff_name)) {
        error = "严禁向受保护的每日歌单 ChatGPT Daily 5 (p.YJXV7dEIeb3GzZX) 写入交互推荐";
        return false;
    }

    nlohmann::json tracks_json = nlohmann::json::array();
    nlohmann::json track_ids = nlohmann::json::array();
    for (const auto& item : items) {
        if (!cbIsDigits(item.track.id)) continue;
        track_ids.push_back(item.track.id);
        tracks_json.push_back({
            {"song_id", item.track.id},
            {"title", item.track.title},
            {"artist", item.track.artist},
            {"album", item.track.album},
            {"storefront", item.track.storefront.empty() ? "cn" : item.track.storefront},
        });
    }
    if (track_ids.empty()) {
        error = "没有可写回歌单的有效数字曲目 ID";
        return false;
    }

    const bool has_cloud_auth = adam_.hasUsableAuthForWrite();
    nlohmann::json req = {
        {"action", "write_interactive_playlist"},
        {"mode", "interactive_ephemeral"},
        {"record_permanent_history", false},
        {"playlist_name", eff_name},
        {"write_strategy", "append"},
        {"track_ids", std::move(track_ids)},
        {"tracks", std::move(tracks_json)},
        {"local_checkpoint_only", !has_cloud_auth},
        {"enqueue_now", enqueue_now},
    };
    auto res = handlePlaylistRpc(req);
    if (!res.ok && has_cloud_auth) {
        // Graceful fallback to local staging if cloud token expired mid-session
        req["local_checkpoint_only"] = true;
        auto local_res = handlePlaylistRpc(req);
        if (local_res.ok) {
            result_out = local_res.data;
            result_out["cloud_fallback_reason"] = res.error;
            notifyObservers();
            return true;
        }
    }
    if (!res.ok) {
        error = res.error;
        return false;
    }
    result_out = res.data;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        feed_cache_.erase("library-playlists|");
    }
    notifyObservers();
    return true;
}

bool MusicEngine::clearInteractiveSessions(const std::string& session_id, std::string& error) {
    error.clear();
    if (ctx_.capabilities().has("recommend.feed.v1")) {
        nlohmann::json req = {{"action", "clear_interactive"}};
        if (!session_id.empty()) req["session_id"] = session_id;
        auto res = ctx_.capabilities().call("recommend.feed.v1", req);
        if (!res.ok) {
            error = res.error;
            return false;
        }
        notifyObservers();
        return true;
    }

    const fs::path data_root = cbResolveDataRoot(currentConfig().data_dir);
    const fs::path ix_dir = data_root / "curation" / "interactive" / "sessions";
    std::error_code ec;
    if (fs::exists(ix_dir, ec)) {
        for (const auto& entry : fs::directory_iterator(ix_dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
            if (session_id.empty() || entry.path().stem().string() == session_id) {
                fs::remove(entry.path(), ec);
            }
        }
    }
    notifyObservers();
    return true;
}

bool MusicEngine::checkDailyPlaylistSyncDryRun(const std::string& date,
                                               nlohmann::json& result_out,
                                               std::string& error) {
    nlohmann::json req = {
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"dry_run", true},
        {"cumulative_playlist_id", kProtectedDailyPlaylistId},
    };
    if (date.empty()) {
        req["sync_all_rehydrated"] = true;
    } else {
        req["business_date"] = date;
    }
    auto res = handlePlaylistRpc(req);
    if (!res.ok) {
        error = res.error;
        return false;
    }
    result_out = res.data;
    return true;
}

void MusicEngine::onExternalCurationUpdated() {
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        feed_cache_.erase("library-playlists|");
    }
    notifyObservers();
}

} // namespace music_plugin
