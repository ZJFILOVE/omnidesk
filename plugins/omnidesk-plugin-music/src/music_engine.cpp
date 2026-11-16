#include "music/music_engine.hpp"
#include "music/config.hpp"
#include "music/lrc_parser.hpp"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <ctime>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <sys/file.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <unordered_set>
#include <omnidesk/mpv_core.hpp>

namespace fs = std::filesystem;

namespace music_plugin {
namespace {

std::string toLowerCopy(std::string s) {
    for (char& ch : s) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return s;
}

std::string statusLabelFor(const std::string& st) {
    if (st == "loading") return "正在读取 Apple Music 原生歌词与缓存…";
    if (st == "translating") return "正在生成双语中文歌词，原文可先查看…";
    if (st == "instrumental") return "纯音乐";
    if (st == "unavailable") return "暂未找到歌词";
    if (st == "existing") return "已接入已有双语歌词";
    if (st == "original_chinese") return "原歌词已是中文";
    if (st == "translated") return "双语中文歌词已翻译并保存";
    if (st == "disabled") return "自动翻译已关闭 · 点击「翻译中文」";
    if (st == "error") return "歌词原文已保存，中文翻译可重试";
    return "";
}

bool isDigitsOnlyStr(const std::string& s) {
    if (s.empty()) return false;
    return std::all_of(s.begin(), s.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
}

std::string extractStorefrontFromAppleUrl(const std::string& url) {
    size_t host_pos = url.find("music.apple.com/");
    if (host_pos == std::string::npos) return "";
    size_t p = host_pos + std::char_traits<char>::length("music.apple.com/");
    size_t slash = url.find('/', p);
    if (slash == std::string::npos || slash <= p) return "";
    std::string seg = toLowerCopy(url.substr(p, slash - p));
    if (seg.size() == 2 &&
        std::isalpha(static_cast<unsigned char>(seg[0])) &&
        std::isalpha(static_cast<unsigned char>(seg[1]))) {
        return seg;
    }
    return "";
}

std::string extractSongIdFromAppleUrl(const std::string& url) {
    if (url.find("music.apple.com/") == std::string::npos) return "";
    size_t q_pos = url.find('?');
    if (q_pos != std::string::npos) {
        std::string query = url.substr(q_pos + 1);
        size_t hash = query.find('#');
        if (hash != std::string::npos) query.resize(hash);
        size_t cur = 0;
        while (cur < query.size()) {
            size_t amp = query.find('&', cur);
            std::string kv = (amp == std::string::npos) ? query.substr(cur) : query.substr(cur, amp - cur);
            if (kv.rfind("i=", 0) == 0) {
                std::string cand = kv.substr(2);
                if (isDigitsOnlyStr(cand)) return cand;
            }
            if (amp == std::string::npos) break;
            cur = amp + 1;
        }
    }
    for (const char* marker : {"/song/", "/music-video/"}) {
        size_t pos = url.find(marker);
        if (pos != std::string::npos) {
            size_t p = pos + std::char_traits<char>::length(marker);
            size_t q = url.find_first_of("?#", p);
            std::string tail = (q == std::string::npos) ? url.substr(p) : url.substr(p, q - p);
            while (!tail.empty() && tail.back() == '/') tail.pop_back();
            size_t slash = tail.rfind('/');
            std::string cand = (slash == std::string::npos) ? tail : tail.substr(slash + 1);
            if (isDigitsOnlyStr(cand)) return cand;
        }
    }
    return "";
}

struct NormalizedAppleAsset {
    std::string id;
    std::string storefront;
};

NormalizedAppleAsset normalizeAppleMusicAsset(const std::string& raw_id,
                                              const std::string& raw_url,
                                              const std::string& explicit_sf,
                                              const std::string& fallback_sf) {
    NormalizedAppleAsset out;
    out.storefront = toLowerCopy(explicit_sf);

    auto parsePrefixed = [&](const std::string& s) -> std::string {
        if (s.rfind("am-", 0) == 0 && s.size() > 6 && s[5] == '-' &&
            std::isalpha(static_cast<unsigned char>(s[3])) &&
            std::isalpha(static_cast<unsigned char>(s[4]))) {
            if (out.storefront.empty()) out.storefront = toLowerCopy(s.substr(3, 2));
            std::string rest = s.substr(6);
            if (isDigitsOnlyStr(rest)) return rest;
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && isDigitsOnlyStr(rest.substr(dash + 1))) {
                return rest.substr(dash + 1);
            }
        }
        if (s.rfind("ix-am-", 0) == 0) {
            std::string rest = s.substr(6);
            if (isDigitsOnlyStr(rest)) return rest;
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && isDigitsOnlyStr(rest.substr(dash + 1))) {
                return rest.substr(dash + 1);
            }
        }
        return "";
    };

    if (!raw_url.empty() && out.storefront.empty()) {
        out.storefront = extractStorefrontFromAppleUrl(raw_url);
    }
    if (!raw_id.empty() && out.storefront.empty()) {
        out.storefront = extractStorefrontFromAppleUrl(raw_id);
    }

    if (isDigitsOnlyStr(raw_id)) {
        out.id = raw_id;
    } else if (std::string pref = parsePrefixed(raw_id); !pref.empty()) {
        out.id = pref;
    } else if (std::string from_id_url = extractSongIdFromAppleUrl(raw_id); !from_id_url.empty()) {
        out.id = from_id_url;
    } else if (std::string from_url = extractSongIdFromAppleUrl(raw_url); !from_url.empty()) {
        out.id = from_url;
    } else {
        out.id = raw_id;
    }

    if (out.storefront.empty()) {
        out.storefront = fallback_sf;
    }
    return out;
}

} // namespace

MusicEngine::MusicEngine(omnidesk::PluginContext& ctx)
    : ctx_(ctx),
      adam_(&cache_),
      llm_(ctx.capabilities()) {}

MusicEngine::~MusicEngine() {
    shutdown();
}

void MusicEngine::initializeFast() {
    reloadConfig();
    if (!cfg_.data_dir.empty()) {
        std::error_code ec;
        fs::create_directories(fs::path(cfg_.data_dir) / "lyrics", ec);
    }
}

void MusicEngine::reloadConfig() {
    MusicConfig next = loadConfigFromContext(ctx_);
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        cfg_ = next;
    }
    cache_.setBaseDir(next.data_dir);
    adam_.configure(next);
}

MusicConfig MusicEngine::currentConfig() const {
    std::lock_guard<std::mutex> lk(cfg_mutex_);
    return cfg_;
}

void MusicEngine::startBackgroundTasks() {
    worker_thread_ = std::thread([this]() { workerLoop(); });

    mpv_sub_token_ = omnidesk::MPVCore::instance().subscribeState(
        [this](const omnidesk::MpvPlaybackState& st) {
            if (!alive_.load(std::memory_order_acquire)) return;
            bool trigger_next = false;
            {
                std::lock_guard<std::mutex> lk(state_mutex_);
                bool is_ours = !active_session_.session_id.empty() &&
                               st.current_url.find(active_session_.session_id) != std::string::npos;
                if (is_ours) {
                    if (st.playing && !st.stopped) {
                        was_playing_in_mpv_ = true;
                        auto_advancing_ = false;
                    } else if (st.stopped && was_playing_in_mpv_ && !auto_advancing_ &&
                               st.last_error.empty() && queue_.size() > 1 &&
                               queue_index_ + 1 < queue_.size()) {
                        was_playing_in_mpv_ = false;
                        auto_advancing_ = true;
                        trigger_next = true;
                    }
                }
            }
            if (trigger_next) {
                enqueueTask([this]() { playAdjacentTrack(1); });
            }
        });

    enqueueTask([this]() {
        std::string err;
        adam_.ensureEngineRunning(err);
        refreshDrmStatus(false);
        std::string tok_err;
        adam_.pushTokensToEngine(tok_err);
    });
}

void MusicEngine::shutdown() {
    if (shutting_down_.exchange(true, std::memory_order_acq_rel)) {
        return;
    }
    alive_.store(false, std::memory_order_release);

    if (mpv_sub_token_ != 0) {
        omnidesk::MPVCore::instance().unsubscribeState(mpv_sub_token_);
        mpv_sub_token_ = 0;
    }

    {
        std::lock_guard<std::mutex> lk(queue_mutex_);
        worker_tasks_.clear();
        queue_cv_.notify_all();
    }
    if (worker_thread_.joinable()) {
        worker_thread_.join();
    }

    std::string sid;
    std::string rpc_sid;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        sid = active_session_.session_id;
        active_session_.session_id.clear();
        rpc_sid = last_rpc_session_id_;
        last_rpc_session_id_.clear();
    }
    if (!sid.empty()) {
        adam_.deletePlaybackSession(sid);
    }
    if (!rpc_sid.empty() && rpc_sid != sid) {
        adam_.deletePlaybackSession(rpc_sid);
    }
    adam_.stopManagedEngine();

    std::lock_guard<std::mutex> lk(obs_mutex_);
    observers_.clear();
}

void MusicEngine::enqueueTask(std::function<void()> task) {
    if (!alive_.load(std::memory_order_acquire)) return;
    {
        std::lock_guard<std::mutex> lk(queue_mutex_);
        worker_tasks_.push_back(std::move(task));
    }
    queue_cv_.notify_one();
}

void MusicEngine::workerLoop() {
    while (alive_.load(std::memory_order_acquire)) {
        std::function<void()> task;
        {
            std::unique_lock<std::mutex> lk(queue_mutex_);
            queue_cv_.wait(lk, [this]() {
                return !alive_.load(std::memory_order_acquire) || !worker_tasks_.empty();
            });
            if (!alive_.load(std::memory_order_acquire)) break;
            task = std::move(worker_tasks_.front());
            worker_tasks_.erase(worker_tasks_.begin());
        }
        if (task) {
            try {
                task();
            } catch (...) {
            }
        }
    }
}

uint64_t MusicEngine::subscribeState(StateObserver cb) {
    std::lock_guard<std::mutex> lk(obs_mutex_);
    uint64_t tok = next_obs_token_++;
    observers_[tok] = std::move(cb);
    return tok;
}

void MusicEngine::unsubscribeState(uint64_t token) {
    std::lock_guard<std::mutex> lk(obs_mutex_);
    observers_.erase(token);
}

void MusicEngine::notifyObservers() {
    std::vector<StateObserver> cbs;
    {
        std::lock_guard<std::mutex> lk(obs_mutex_);
        cbs.reserve(observers_.size());
        for (const auto& kv : observers_) cbs.push_back(kv.second);
    }
    for (const auto& cb : cbs) {
        if (cb && alive_.load(std::memory_order_acquire)) cb();
    }
}

DrmStatus MusicEngine::refreshDrmStatus(bool ensure_started) {
    if (ensure_started) {
        std::string err;
        adam_.ensureEngineRunning(err);
    }
    DrmStatus st = adam_.getDrmStatus();
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        last_drm_ = st;
    }
    notifyObservers();
    return st;
}

DrmStatus MusicEngine::cachedDrmStatus() const {
    std::lock_guard<std::mutex> lk(state_mutex_);
    return last_drm_;
}

bool MusicEngine::authenticateAppleId(const std::string& email,
                                      const std::string& password,
                                      DrmStatus& out,
                                      std::string& error) {
    if (!adam_.ensureEngineRunning(error)) return false;
    if (!adam_.authenticateDrm(email, password, out, error)) return false;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        last_drm_ = out;
    }
    notifyObservers();
    return true;
}

bool MusicEngine::submitTwoFactorCode(const std::string& code, DrmStatus& out, std::string& error) {
    if (!adam_.ensureEngineRunning(error)) return false;
    if (!adam_.submitDrmChallenge(code, out, error)) return false;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        last_drm_ = out;
    }
    notifyObservers();
    return true;
}

bool MusicEngine::resendTwoFactorCode(DrmStatus& out, std::string& error) {
    if (!adam_.ensureEngineRunning(error)) return false;
    if (!adam_.resendDrmChallenge(out, error)) return false;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        last_drm_ = out;
    }
    notifyObservers();
    return true;
}

bool MusicEngine::logoutAppleId(std::string& error) {
    if (!adam_.logoutDrm(error)) return false;
    refreshDrmStatus(false);
    return true;
}

bool MusicEngine::importMediaUserToken(const std::string& mut, std::string& error) {
    if (mut.empty()) {
        error = "media-user-token 不能为空";
        return false;
    }
    adam_.setMediaUserToken(mut);
    ctx_.setSecret("media_user_token", mut);
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        cfg_.media_user_token = mut;
    }
    std::string push_err;
    adam_.pushTokensToEngine(push_err);
    notifyObservers();
    return true;
}

bool MusicEngine::tryImportMutFromChromeCookies(std::string& error) {
    if (!ctx_.capabilities().has("web.cookies.v1")) {
        error = "未检测到 plugin-chrome-web (web.cookies.v1) 插件";
        return false;
    }
    auto res = ctx_.capabilities().call("web.cookies.v1", {
        {"action", "get"},
        {"domain", "music.apple.com"},
        {"url", "https://music.apple.com"},
    });
    if (!res.ok || !res.data.is_object()) {
        error = res.error.empty() ? "无法从 Chrome 读取 music.apple.com Cookie" : res.error;
        return false;
    }

    std::string found_mut;
    if (res.data.contains("cookies") && res.data["cookies"].is_array()) {
        for (const auto& c : res.data["cookies"]) {
            if (!c.is_object()) continue;
            if (c.value("name", "") == "media-user-token" && !c.value("value", "").empty()) {
                found_mut = c.value("value", "");
                break;
            }
        }
    }
    if (found_mut.empty() && res.data.contains("cookie_header") && res.data["cookie_header"].is_string()) {
        std::string hdr = res.data["cookie_header"].get<std::string>();
        size_t pos = hdr.find("media-user-token=");
        if (pos != std::string::npos) {
            pos += 17;
            size_t end = hdr.find(';', pos);
            found_mut = (end == std::string::npos) ? hdr.substr(pos) : hdr.substr(pos, end - pos);
        }
    }
    if (found_mut.empty()) {
        error = "Chrome 中未找到有效的 media-user-token Cookie（请先在 Chrome 登录 music.apple.com）";
        return false;
    }
    return importMediaUserToken(found_mut, error);
}

FeedOutcome MusicEngine::loadFeed(const std::string& feed_id, const std::string& query, bool force_refresh) {
    std::string cache_key = feed_id + "|" + query;
    if (!force_refresh) {
        std::lock_guard<std::mutex> lk(state_mutex_);
        auto it = feed_cache_.find(cache_key);
        if (it != feed_cache_.end() && it->second.ok) {
            return it->second;
        }
    }

    FeedOutcome out;
    out.feed_id = feed_id;
    std::string eng_err;
    const bool can_use_amp_fallback =
        !adam_.resolveMediaUserToken().empty() || adam_.allowLocalAdamDrmFallback();
    if (feed_id != "search" && feed_id != "recently-played-tracks") {
        if (!adam_.ensureEngineRunning(eng_err) && !can_use_amp_fallback) {
            out.error = eng_err;
            return out;
        }
    }

    auto appendUniqueTracks = [](std::vector<MusicTrack>& dst, const std::vector<MusicTrack>& src, size_t max_total) {
        std::unordered_set<std::string> seen;
        for (const auto& t : dst) seen.insert(t.id);
        for (const auto& t : src) {
            if (dst.size() >= max_total) break;
            if (!t.id.empty() && seen.insert(t.id).second) {
                dst.push_back(t);
            }
        }
    };

    if (feed_id == "recently-played-tracks") {
        out = adam_.fetchRecommendations("recently-played-tracks", 20);
    } else if (feed_id == "heavy-rotation" || feed_id == "recently-played") {
        out = adam_.fetchRecommendations(feed_id, 10);
        if (feed_id == "heavy-rotation" && out.containers.size() < 6) {
            FeedOutcome rp = adam_.fetchRecommendations("recently-played", 10);
            if (rp.ok) {
                if (!out.ok) {
                    out = std::move(rp);
                    out.feed_id = feed_id;
                } else {
                    std::unordered_set<std::string> seen_c;
                    for (const auto& c : out.containers) seen_c.insert(c.type + ":" + c.id);
                    for (auto& c : rp.containers) {
                        if (seen_c.insert(c.type + ":" + c.id).second) {
                            out.containers.push_back(std::move(c));
                        }
                    }
                    appendUniqueTracks(out.tracks, rp.tracks, 20);
                }
            }
        }
        if (adam_.allowLocalAdamDrmFallback() && !adam_.resolveMediaUserToken().empty()) {
            FeedOutcome rpt = adam_.fetchRecommendations("recently-played-tracks", 20);
            if (rpt.ok && !rpt.tracks.empty()) {
                appendUniqueTracks(out.tracks, rpt.tracks, 20);
                out.ok = true;
                out.error.clear();
            }
        }
    } else if (feed_id == "recommendations") {
        out = adam_.fetchRecommendations("recommendations", 12);
        if (out.ok && out.tracks.empty() && adam_.allowLocalAdamDrmFallback() &&
            !adam_.resolveMediaUserToken().empty()) {
            FeedOutcome rpt = adam_.fetchRecommendations("recently-played-tracks", 15);
            if (rpt.ok && !rpt.tracks.empty()) {
                appendUniqueTracks(out.tracks, rpt.tracks, 15);
            }
            if (out.tracks.empty()) {
                for (const auto& c : out.containers) {
                    if (c.type == "playlists" || c.type == "albums") {
                        std::vector<MusicTrack> c_tracks;
                        std::string dummy_err;
                        if (openContainer(c, c_tracks, dummy_err) && !c_tracks.empty()) {
                            appendUniqueTracks(out.tracks, c_tracks, 15);
                            break;
                        }
                    }
                }
            }
        }
    } else if (feed_id == "library-playlists") {
        std::string err;
        out.containers = adam_.fetchLibraryPlaylists(err);
        out.ok = err.empty();
        out.error = err;
    } else if (feed_id == "search") {
        out = adam_.searchCatalog(query, adam_.effectiveStorefront());
    } else {
        out.error = "未知栏目: " + feed_id;
    }

    if (out.ok) {
        std::lock_guard<std::mutex> lk(state_mutex_);
        feed_cache_[cache_key] = out;
    }
    return out;
}

bool MusicEngine::openContainer(const ContainerItem& container,
                                std::vector<MusicTrack>& tracks_out,
                                std::string& error) {
    tracks_out.clear();
    std::string eng_err;
    if (!adam_.ensureEngineRunning(eng_err) &&
        !adam_.allowLocalAdamDrmFallback() &&
        adam_.resolveMediaUserToken().empty()) {
        error = eng_err;
        return false;
    }

    if (container.type == "library-playlists") {
        tracks_out = adam_.fetchLibraryPlaylistTracks(container.id, error);
        return error.empty();
    }
    if (container.type == "library-albums") {
        tracks_out = adam_.fetchLibraryAlbumTracks(container.id, error);
        return error.empty();
    }
    if (container.type == "albums") {
        ContainerItem dummy;
        return adam_.fetchCatalogAlbum(container.id, container.storefront, dummy, tracks_out, error);
    }
    if (container.type == "playlists") {
        ContainerItem dummy;
        return adam_.fetchCatalogPlaylist(container.id, container.storefront, dummy, tracks_out, error);
    }
    if (container.type == "artists") {
        ContainerItem dummy;
        std::vector<ContainerItem> albums;
        return adam_.fetchCatalogArtist(container.id, container.storefront, dummy, albums, tracks_out, error);
    }
    error = "不支持的容器类型: " + container.type;
    return false;
}

bool MusicEngine::syncLibrary(nlohmann::json& stats_out, std::string& error) {
    if (!adam_.ensureEngineRunning(error)) return false;
    if (!adam_.syncLibraryFromApple(stats_out, error)) return false;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        feed_cache_.erase("library-playlists|");
    }
    notifyObservers();
    return true;
}

std::string MusicEngine::makeMpvUrlHeaders(std::unordered_map<std::string, std::string>& headers_out) const {
    headers_out.clear();
    if (const char* tok = std::getenv("ADAM_API_TOKEN"); tok && *tok) {
        headers_out["X-Api-Token"] = tok;
        return tok;
    }
    return "";
}

void MusicEngine::releasePlaybackSessionAsync(const std::string& session_id) {
    if (session_id.empty()) return;
    enqueueTask([this, session_id]() {
        adam_.deletePlaybackSession(session_id);
    });
}

void MusicEngine::setQueueAndPlay(std::vector<MusicTrack> queue, size_t start_index) {
    if (queue.empty()) return;
    if (start_index >= queue.size()) start_index = 0;
    MusicTrack target = queue[start_index];
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        queue_ = std::move(queue);
        queue_index_ = start_index;
    }
    playTrack(target);
}

void MusicEngine::playAdjacentTrack(int delta) {
    MusicTrack target;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        if (queue_.empty()) return;
        int n = static_cast<int>(queue_.size());
        int next_i = ((static_cast<int>(queue_index_) + delta) % n + n) % n;
        queue_index_ = static_cast<size_t>(next_i);
        target = queue_[queue_index_];
    }
    playTrack(target);
}

void MusicEngine::playTrack(const MusicTrack& track_in) {
    MusicTrack track = track_in;
    std::string prev_sid;
    uint64_t gen = 0;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        prev_sid = active_session_.session_id;
        active_session_ = PlaybackSessionInfo{};
        active_track_ = track;
        has_active_track_ = true;
        was_playing_in_mpv_ = false;
        auto_advancing_ = false;
        gen = ++lyric_generation_;
        active_lyrics_ = LyricBundle{};
        active_lyrics_.track = track;
        active_lyrics_.signature = computeTrackSignature(track);
        active_lyrics_.translation_status = "loading";
        active_lyrics_.status_text = statusLabelFor("loading");
    }

    if (!prev_sid.empty()) {
        releasePlaybackSessionAsync(prev_sid);
    }
    notifyObservers();

    // Start async lyrics loading
    enqueueTask([this, track, gen]() {
        LyricBundle b = getLyricsForTrack(track, false, false, gen);
        bool notify = false;
        {
            std::lock_guard<std::mutex> lk(state_mutex_);
            if (gen == lyric_generation_ && alive_.load(std::memory_order_acquire)) {
                active_lyrics_ = std::move(b);
                notify = true;
            }
        }
        if (notify) notifyObservers();
    });

    // Start async FairPlay playback session + MPVCore load
    enqueueTask([this, track, gen]() {
        std::string eng_err;
        if (!adam_.ensureEngineRunning(eng_err)) {
            std::lock_guard<std::mutex> lk(state_mutex_);
            if (gen == lyric_generation_) {
                active_session_.ok = false;
                active_session_.error = eng_err;
            }
            notifyObservers();
            return;
        }

        MusicConfig cfg = currentConfig();
        bool is_mv = (track.kind == "mv");
        PlaybackSessionInfo sess = adam_.createPlaybackSession(
            track.id,
            track.storefront.empty() ? cfg.storefront : track.storefront,
            cfg.lossless_enabled,
            cfg.hires_enabled,
            is_mv,
            false);

        if (!alive_.load(std::memory_order_acquire)) {
            if (!sess.session_id.empty()) adam_.deletePlaybackSession(sess.session_id);
            return;
        }

        {
            std::lock_guard<std::mutex> lk(state_mutex_);
            if (gen != lyric_generation_) {
                if (!sess.session_id.empty()) adam_.deletePlaybackSession(sess.session_id);
                return;
            }
            active_session_ = sess;
            if (sess.ok) {
                if (active_track_.title.empty() && !sess.title.empty()) active_track_.title = sess.title;
                if (active_track_.artist.empty() && !sess.artist.empty()) active_track_.artist = sess.artist;
                if (active_track_.album.empty() && !sess.album.empty()) active_track_.album = sess.album;
                if (active_track_.cover_url.empty() && !sess.cover_url.empty()) active_track_.cover_url = sess.cover_url;
                if (active_track_.duration_sec <= 0.0 && sess.duration_sec > 0.0) active_track_.duration_sec = sess.duration_sec;
                active_track_.quality_label = sess.formattedQuality();
            }
        }

        if (sess.ok) {
            omnidesk::MediaRequestOptions opts;
            opts.url = (sess.type == "mv" && !sess.video_url.empty()) ? sess.video_url : sess.audio_url;
            if (sess.type == "mv" && !sess.audio_url.empty() && sess.video_url != sess.audio_url) {
                opts.audio_url = sess.audio_url;
            }
            makeMpvUrlHeaders(opts.http_headers);
            opts.hwdec = (sess.type == "mv");
            omnidesk::MPVCore::instance().load(opts);

            ctx_.publish("entity.media.open", {
                {"id", track.id},
                {"title", track.title},
                {"artist", track.artist},
                {"source_id", "plugin-music"},
                {"kind", is_mv ? "video" : "music"},
                {"origin", "plugin_music"},
            });
        }
        notifyObservers();
    });
}

void MusicEngine::syncExternalTrack(const MusicTrack& track_in) {
    MusicTrack track = track_in;
    uint64_t gen = 0;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        if (has_active_track_ && active_track_.id == track.id) {
            if (track.title.empty()) track.title = active_track_.title;
            if (track.artist.empty()) track.artist = active_track_.artist;
            if (track.album.empty()) track.album = active_track_.album;
            if (track.cover_url.empty()) track.cover_url = active_track_.cover_url;
            if (track.duration_sec <= 0.0) track.duration_sec = active_track_.duration_sec;
            if (track.quality_label.empty()) track.quality_label = active_track_.quality_label;
            if (!active_lyrics_.lines.empty() &&
                active_lyrics_.translation_status != "loading") {
                active_track_ = track;
                notifyObservers();
                return;
            }
        }
        active_track_ = track;
        has_active_track_ = true;
        gen = ++lyric_generation_;
        active_lyrics_ = LyricBundle{};
        active_lyrics_.track = track;
        active_lyrics_.signature = computeTrackSignature(track);
        active_lyrics_.translation_status = "loading";
        active_lyrics_.status_text = statusLabelFor("loading");
    }
    notifyObservers();

    enqueueTask([this, track, gen]() mutable {
        if (!alive_.load(std::memory_order_acquire)) return;
        MusicConfig cfg = currentConfig();
        if (adam_.isPortReachable() &&
            (track.cover_url.empty() || track.album.empty() || track.artist.empty())) {
            MusicTrack meta;
            std::string meta_err;
            const std::string sf = track.storefront.empty() ? cfg.storefront : track.storefront;
            if (adam_.fetchMetadata(track.id, sf, meta, meta_err)) {
                if (track.title.empty() && !meta.title.empty()) track.title = meta.title;
                if (track.artist.empty() && !meta.artist.empty()) track.artist = meta.artist;
                if (track.album.empty() && !meta.album.empty()) track.album = meta.album;
                if (track.cover_url.empty() && !meta.cover_url.empty()) track.cover_url = meta.cover_url;
                if (track.duration_sec <= 0.0 && meta.duration_sec > 0.0) track.duration_sec = meta.duration_sec;
                if (track.quality_label.empty() && !meta.quality_label.empty()) track.quality_label = meta.quality_label;
                std::lock_guard<std::mutex> lk(state_mutex_);
                if (gen == lyric_generation_ && alive_.load(std::memory_order_acquire)) {
                    active_track_ = track;
                }
            }
        }
        LyricBundle b = getLyricsForTrack(track, false, false, gen);
        bool notify = false;
        {
            std::lock_guard<std::mutex> lk(state_mutex_);
            if (gen == lyric_generation_ && alive_.load(std::memory_order_acquire)) {
                active_lyrics_ = std::move(b);
                notify = true;
            }
        }
        if (notify) notifyObservers();
    });
}

void MusicEngine::togglePlayPause() {
    omnidesk::MPVCore::instance().togglePause();
}

void MusicEngine::seekSeconds(double sec) {
    omnidesk::MPVCore::instance().seekAbsolute(std::max(0.0, sec));
}

void MusicEngine::setVolumePercent(int vol) {
    omnidesk::MPVCore::instance().setVolume(std::clamp(vol, 0, 100));
}

void MusicEngine::setLosslessEnabled(bool enabled) {
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        cfg_.lossless_enabled = enabled;
    }
    notifyObservers();
}

void MusicEngine::setDisplayMode(const std::string& mode) {
    if (mode != "bilingual" && mode != "original" && mode != "translation") return;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        cfg_.lyrics_display_mode = mode;
    }
    notifyObservers();
}

std::string MusicEngine::displayMode() const {
    return currentConfig().lyrics_display_mode;
}

LyricBundle MusicEngine::getLyricsForTrack(const MusicTrack& track_in,
                                           bool force_refresh,
                                           bool force_translate,
                                           uint64_t expected_gen) {
    MusicConfig cfg = currentConfig();
    MusicTrack track = track_in;
    LyricBundle bundle;
    bundle.track = track;
    bundle.signature = computeTrackSignature(track);

    auto isStale = [&]() -> bool {
        if (!alive_.load(std::memory_order_acquire)) return true;
        if (expected_gen == 0) return false;
        std::lock_guard<std::mutex> lk(state_mutex_);
        return expected_gen != lyric_generation_;
    };

    std::vector<LyricLineEx> lines;
    std::string source;
    bool instrumental = false;
    std::string error;

    // 1. Explicit imported lyrics
    if (!track.embedded_lyrics.empty()) {
        lines = parseLyricsText(track.embedded_lyrics);
        if (!lines.empty()) source = "import";
    }

    // 2. Native Adam Go engine lyrics (/api/v1/lyrics/{id})
    if (lines.empty() && !track.id.empty() && adam_.isPortReachable()) {
        std::string native_lrc;
        std::string adam_err;
        if (adam_.fetchNativeLyrics(track.id,
                                    track.storefront.empty() ? cfg.storefront : track.storefront,
                                    native_lrc, adam_err)) {
            lines = parseLyricsText(native_lrc);
            if (!lines.empty()) source = "adam";
        }
    }

    if (isStale()) return bundle;

    // 3. Local disk cache
    auto cached = cache_.loadLyrics(bundle.signature);
    if (lines.empty() && cached.has_value() && !force_refresh) {
        lines = cached->lines;
        source = cached->source.empty() ? "cache" : cached->source;
        instrumental = cached->instrumental;
        bundle.cache_hit = true;
    }

    // 4. LRCLIB exact signature fallback
    if (lines.empty() && !instrumental && (force_refresh || !cached.has_value()) && cfg.enable_lrclib) {
        LrclibLookupResult lrc_res = lrclib_.fetchExact(track, cfg.proxy);
        if (!lrc_res.ok) {
            error = lrc_res.error;
        } else if (lrc_res.found) {
            instrumental = lrc_res.instrumental;
            std::string raw_lrc = !lrc_res.synced_lyrics.empty() ? lrc_res.synced_lyrics : lrc_res.plain_lyrics;
            lines = parseLyricsText(raw_lrc);
            if (!lines.empty() || instrumental) {
                source = "lrclib";
                bundle.cache_hit = false;
            }
        }
    }

    if (isStale()) return bundle;

    // Preserve cached Chinese translations for matching lines
    if (cached.has_value() && !lines.empty()) {
        std::unordered_map<std::string, std::string> old_tr;
        for (const auto& cl : cached->lines) {
            if (!cl.translation.empty()) old_tr[cl.id] = cl.translation;
        }
        for (auto& l : lines) {
            if (l.translation.empty()) {
                auto it = old_tr.find(l.id);
                if (it != old_tr.end()) l.translation = it->second;
            }
        }
    }

    bundle.lines = std::move(lines);
    bundle.source = source;
    bundle.instrumental = instrumental;
    bundle.error = error;
    bundle.synced = !bundle.lines.empty() &&
                    std::all_of(bundle.lines.begin(), bundle.lines.end(),
                                [](const LyricLineEx& l) { return l.has_time; });
    bundle.translated = std::any_of(bundle.lines.begin(), bundle.lines.end(),
                                    [](const LyricLineEx& l) { return !l.translation.empty(); });

    bool target_is_zh = (cfg.target_language.rfind("zh", 0) == 0);
    bool has_kana = std::any_of(bundle.lines.begin(), bundle.lines.end(), [](const LyricLineEx& l) {
        return containsKanaOrHangul(l.original);
    });
    bool has_missing = std::any_of(bundle.lines.begin(), bundle.lines.end(), [&](const LyricLineEx& l) {
        if (!hasAlphabeticText(l.original) || !l.translation.empty()) return false;
        if (target_is_zh && !has_kana && containsChineseOnlyScript(l.original)) return false;
        return true;
    });

    if (bundle.instrumental) bundle.translation_status = "instrumental";
    else if (bundle.lines.empty()) bundle.translation_status = "unavailable";
    else if (bundle.translated && !has_missing) bundle.translation_status = "existing";
    else if (!has_missing) bundle.translation_status = "original_chinese";

    if (!bundle.lines.empty() || bundle.instrumental) {
        cache_.saveLyrics(bundle);
    }

    if (has_missing) {
        bool should_translate = cfg.auto_translate_lyrics || force_translate;
        if (!should_translate) {
            bundle.translation_status = "disabled";
        } else {
            if (expected_gen != 0) {
                {
                    std::lock_guard<std::mutex> lk(state_mutex_);
                    if (expected_gen == lyric_generation_) {
                        active_lyrics_ = bundle;
                        active_lyrics_.translation_status = "translating";
                        active_lyrics_.status_text = statusLabelFor("translating");
                    }
                }
                notifyObservers();
            }

            std::string tr_err;
            bool degraded = false;
            bool ok = llm_.translateMissingLines(track, bundle.lines, cfg.target_language,
                                                 cfg.translation_batch_size, tr_err, &degraded);
            if (isStale()) return bundle;
            if (ok) {
                bundle.translated = std::any_of(bundle.lines.begin(), bundle.lines.end(),
                                                [](const LyricLineEx& l) { return !l.translation.empty(); });
                bundle.translation_status = "translated";
                cache_.saveLyrics(bundle);
            } else {
                bundle.translation_status = degraded ? "disabled" : "error";
                bundle.error = tr_err;
            }
        }
    }

    bundle.status_text = statusLabelFor(bundle.translation_status);
    return bundle;
}

void MusicEngine::triggerAsyncLyricsRefresh(bool force_refresh, bool force_translate) {
    MusicTrack tr;
    uint64_t gen = 0;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        if (!has_active_track_) return;
        gen = ++lyric_generation_;
        tr = active_track_;
        if (force_translate && !active_lyrics_.lines.empty()) {
            tr.embedded_lyrics = composeLrc(active_lyrics_.lines, "bilingual");
            active_lyrics_.translation_status = "translating";
            active_lyrics_.status_text = statusLabelFor("translating");
        } else {
            tr.embedded_lyrics.clear();
            active_lyrics_ = LyricBundle{};
            active_lyrics_.track = tr;
            active_lyrics_.signature = computeTrackSignature(tr);
            active_lyrics_.translation_status = "loading";
            active_lyrics_.status_text = "正在重新读取歌词…";
        }
    }
    notifyObservers();

    enqueueTask([this, tr, force_refresh, force_translate, gen]() {
        LyricBundle b = getLyricsForTrack(tr, force_refresh, force_translate, gen);
        bool notify = false;
        {
            std::lock_guard<std::mutex> lk(state_mutex_);
            if (gen == lyric_generation_ && alive_.load(std::memory_order_acquire)) {
                active_lyrics_ = std::move(b);
                notify = true;
            }
        }
        if (notify) notifyObservers();
    });
}

LyricBundle MusicEngine::importLyricsForActiveOrTrack(const MusicTrack* explicit_track,
                                                      const std::string& raw_lrc_or_text,
                                                      std::string& error) {
    LyricBundle bundle;
    if (raw_lrc_or_text.empty()) {
        error = "请提供有效的 LRC 或文本歌词";
        return bundle;
    }

    MusicTrack tr;
    uint64_t gen = 0;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        if (explicit_track) tr = *explicit_track;
        else if (has_active_track_) tr = active_track_;
        else {
            error = "当前没有选中的曲目";
            return bundle;
        }
        gen = ++lyric_generation_;
    }

    tr.embedded_lyrics = raw_lrc_or_text;
    bundle = getLyricsForTrack(tr, false, false, 0);
    bundle.source = "import";
    cache_.saveLyrics(bundle);

    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        if (gen == lyric_generation_ && has_active_track_ &&
            computeTrackSignature(active_track_) == bundle.signature) {
            active_lyrics_ = bundle;
        }
    }
    notifyObservers();
    return bundle;
}

PhraseExplanation MusicEngine::explainLine(size_t line_index, const std::string& focus_phrase) {
    MusicTrack tr;
    LyricBundle bundle;
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        tr = active_track_;
        bundle = active_lyrics_;
    }

    if (line_index >= bundle.lines.size()) {
        PhraseExplanation empty;
        empty.degraded = true;
        empty.explanation = "当前无可精讲的歌词行。";
        return empty;
    }

    const LyricLineEx& target = bundle.lines[line_index];
    if (focus_phrase.empty()) {
        if (auto cached = cache_.loadExplanation(bundle.signature, target.id)) {
            return *cached;
        }
    }

    std::string prev = (line_index > 0) ? bundle.lines[line_index - 1].original : "";
    std::string next = (line_index + 1 < bundle.lines.size()) ? bundle.lines[line_index + 1].original : "";
    PhraseExplanation note = llm_.explainLyricLine(
        tr, target, prev, next, focus_phrase, currentConfig().target_language);

    if (!note.degraded && focus_phrase.empty()) {
        cache_.saveExplanation(bundle.signature, note);
    }
    return note;
}

std::string MusicEngine::exportActiveLrc(const std::string& variant, std::string& error) const {
    std::lock_guard<std::mutex> lk(state_mutex_);
    if (active_lyrics_.lines.empty()) {
        error = "当前曲目暂无可导出的歌词";
        return "";
    }
    if (variant != "original" && variant != "translation" && variant != "bilingual") {
        error = "导出格式必须是 original / translation / bilingual";
        return "";
    }
    return composeLrc(active_lyrics_.lines, variant);
}

bool MusicEngine::getActivePlaybackState(MusicTrack& track_out,
                                         PlaybackSessionInfo& session_out,
                                         LyricBundle& lyrics_out,
                                         std::vector<MusicTrack>& queue_out,
                                         size_t& queue_index_out) const {
    std::lock_guard<std::mutex> lk(state_mutex_);
    track_out = active_track_;
    session_out = active_session_;
    lyrics_out = active_lyrics_;
    queue_out = queue_;
    queue_index_out = queue_index_;
    return has_active_track_;
}

EngineStatusSnapshot MusicEngine::statusSnapshot() const {
    EngineStatusSnapshot st;
    st.initialized = true;
    st.engine_reachable = adam_.isPortReachable();
    st.engine_managed_by_plugin = adam_.isManagedByUs();
    st.has_dev_token = adam_.hasDeveloperToken();
    st.has_mut = !adam_.resolveMediaUserToken().empty();
    st.llm_translate_available = llm_.hasTranslator();
    st.llm_chat_available = llm_.hasChat();
    st.cached_lyrics_count = cache_.countCachedTracks();
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        st.drm = last_drm_;
        st.active_track_id = active_track_.id;
        st.active_track_title = active_track_.title;
        st.active_quality = active_session_.ok ? active_session_.formattedQuality() : active_track_.quality_label;
    }
    return st;
}

std::vector<omnidesk::MediaItem> MusicEngine::fetchSdkMediaItems(const std::string& category,
                                                                 const std::string& query) {
    std::vector<omnidesk::MediaItem> out;
    std::string cat_low = toLowerCopy(category);
    if (!cat_low.empty() && cat_low != "all" && cat_low != "music" &&
        cat_low != "recommendations" && cat_low != "双语歌词") {
        return out;
    }

    const std::string default_sf = adam_.effectiveStorefront();
    auto isNumericId = [](const std::string& s) {
        return !s.empty() &&
               std::all_of(s.begin(), s.end(), [](unsigned char ch) { return std::isdigit(ch) != 0; });
    };

    auto toItem = [&](const MusicTrack& tr, const std::string& badge) {
        omnidesk::MediaItem item;
        item.id = tr.id;
        item.source_id = "plugin-music";
        item.title = tr.title;
        item.creator = tr.artist + (tr.album.empty() ? "" : (" · " + tr.album));
        item.kind = (tr.kind == "mv") ? "video" : "music";
        item.source_badge = badge;
        item.cover_url = tr.cover_url;
        item.quality_label = tr.quality_label.empty() ? "ALAC Lossless" : tr.quality_label;
        item.duration_sec = tr.duration_sec;
        item.has_bilingual_sub = tr.has_lyrics;
        const std::string sf = tr.storefront.empty() ? (default_sf.empty() ? "us" : default_sf) : tr.storefront;
        if (isNumericId(tr.id)) {
            item.play_url = "https://music.apple.com/" + sf +
                            (tr.kind == "mv" ? "/music-video/" : "/song/") + tr.id;
        }
        item.extra = {
            {"asset_id", tr.id},
            {"storefront", sf},
            {"kind", tr.kind},
        };
        return item;
    };

    if (!query.empty()) {
        FeedOutcome sr = loadFeed("search", query, false);
        for (const auto& tr : sr.tracks) {
            out.push_back(toItem(tr, "Apple Music"));
        }
        return out;
    }

    auto appendUniqueTrackItem = [&](const MusicTrack& tr, const std::string& badge) {
        if (tr.id.empty() || tr.title.empty()) return;
        bool dup = std::any_of(out.begin(), out.end(), [&](const omnidesk::MediaItem& x) {
            return x.id == tr.id;
        });
        if (!dup) {
            out.push_back(toItem(tr, badge));
        }
    };

    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        if (has_active_track_) {
            auto it = toItem(active_track_, "Apple Music · 正在播放");
            if (active_session_.ok && !active_session_.audio_url.empty()) {
                it.play_url = active_session_.audio_url;
                it.quality_label = active_session_.formattedQuality();
                if (it.cover_url.empty() && !active_session_.cover_url.empty()) {
                    it.cover_url = active_session_.cover_url;
                }
            }
            it.has_bilingual_sub = active_lyrics_.translated || active_track_.has_lyrics;
            out.push_back(std::move(it));
        }
        for (const auto& tr : queue_) {
            if (has_active_track_ && tr.id == active_track_.id) continue;
            appendUniqueTrackItem(tr, "Apple Music · 队列");
            if (out.size() >= 15) break;
        }
    }

    const bool has_user_auth = !adam_.resolveMediaUserToken().empty();
    if (out.size() < 20 && has_user_auth &&
        (adam_.allowLocalAdamDrmFallback() || !adam_.cachedOrEnvDeveloperToken().empty())) {
        FeedOutcome rpt = loadFeed("recently-played-tracks", "", false);
        for (const auto& tr : rpt.tracks) {
            appendUniqueTrackItem(tr, "Apple Music · 最近常听");
            if (out.size() >= 24) break;
        }
    }

    if (out.size() < 12 && (has_user_auth || adam_.isPortReachable())) {
        FeedOutcome hr = loadFeed("heavy-rotation", "", false);
        for (const auto& tr : hr.tracks) {
            appendUniqueTrackItem(tr, "Apple Music · 最近常听");
            if (out.size() >= 24) break;
        }
    }

    if (out.size() < 12 && (has_user_auth || adam_.isPortReachable())) {
        FeedOutcome pls = loadFeed("library-playlists", "", false);
        if (pls.ok && !pls.containers.empty()) {
            const auto& first_pl = pls.containers.front();
            const std::string ckey = "container-tracks|" + first_pl.id;
            std::vector<MusicTrack> pl_tracks;
            bool hit = false;
            {
                std::lock_guard<std::mutex> lk(state_mutex_);
                auto it = feed_cache_.find(ckey);
                if (it != feed_cache_.end() && it->second.ok) {
                    pl_tracks = it->second.tracks;
                    hit = true;
                }
            }
            if (!hit) {
                std::string dummy_err;
                if (openContainer(first_pl, pl_tracks, dummy_err) && !pl_tracks.empty()) {
                    FeedOutcome cached_pl;
                    cached_pl.feed_id = ckey;
                    cached_pl.ok = true;
                    cached_pl.tracks = pl_tracks;
                    std::lock_guard<std::mutex> lk(state_mutex_);
                    feed_cache_[ckey] = std::move(cached_pl);
                }
            }
            const std::string pl_badge = "Apple Music · " + first_pl.name;
            for (const auto& tr : pl_tracks) {
                appendUniqueTrackItem(tr, pl_badge);
                if (out.size() >= 24) break;
            }
        }
    }

    if (out.size() < 12 && (has_user_auth || adam_.isPortReachable())) {
        FeedOutcome rec = loadFeed("recommendations", "", false);
        for (const auto& tr : rec.tracks) {
            appendUniqueTrackItem(tr, "Apple Music · 为你推荐");
            if (out.size() >= 24) break;
        }
    }

    for (const auto& cb : cache_.listCachedBundles(16)) {
        bool dup = std::any_of(out.begin(), out.end(), [&](const omnidesk::MediaItem& x) {
            return x.id == cb.track.id || (!cb.track.title.empty() && x.title == cb.track.title);
        });
        if (dup) continue;
        MusicTrack tr = cb.track;
        if (tr.id.empty()) {
            if (!out.empty()) continue;
            tr.id = "cache:" + cb.signature;
        }
        tr.has_lyrics = cb.translated;
        out.push_back(toItem(tr, cb.translated ? "Apple Music · 双语歌词" : "Apple Music · 歌词缓存"));
    }

    // Backfill any missing covers for numeric catalog IDs (e.g. library playlist or cached lyric bundles)
    if (adam_.allowLocalAdamDrmFallback() || !adam_.cachedOrEnvDeveloperToken().empty() ||
        adam_.isPortReachable()) {
        std::vector<std::pair<std::string, std::string>> missing_cov;
        for (const auto& it : out) {
            if (it.cover_url.empty() && isNumericId(it.id)) {
                std::string sf = it.extra.is_object() ? it.extra.value("storefront", default_sf) : default_sf;
                missing_cov.emplace_back(it.id, sf);
            }
        }
        if (!missing_cov.empty()) {
            std::unordered_map<std::string, MusicTrack> resolved;
            std::string dummy_err;
            if (adam_.fetchBatchMetadata(missing_cov, resolved, dummy_err) && !resolved.empty()) {
                for (auto& it : out) {
                    auto mit = resolved.find(it.id);
                    if (mit != resolved.end()) {
                        if (it.cover_url.empty() && !mit->second.cover_url.empty()) {
                            it.cover_url = mit->second.cover_url;
                        }
                        if (it.duration_sec <= 0.0 && mit->second.duration_sec > 0.0) {
                            it.duration_sec = mit->second.duration_sec;
                        }
                    }
                }
            }
        }
    }

    return out;
}

std::vector<omnidesk::LyricLine> MusicEngine::fetchSdkLyrics(const std::string& item_id) {
    MusicConfig cfg = currentConfig();
    const auto norm = normalizeAppleMusicAsset(item_id, "", "", cfg.storefront);
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        if (has_active_track_ &&
            (active_track_.id == item_id || active_track_.id == norm.id ||
             item_id == active_lyrics_.signature) &&
            !active_lyrics_.lines.empty()) {
            return toSdkLyricLines(active_lyrics_.lines);
        }
    }

    auto maybeScheduleBgTranslate = [&](const LyricBundle& b) {
        if (b.translated || b.instrumental || b.lines.empty() || !cfg.auto_translate_lyrics) {
            return;
        }
        if (!llm_.hasChat() && !llm_.hasTranslator() && !llm_.hasAgent()) {
            return;
        }
        MusicTrack bg_tr = b.track;
        if (bg_tr.id.empty()) bg_tr.id = norm.id;
        if (bg_tr.storefront.empty()) bg_tr.storefront = norm.storefront;
        enqueueTask([this, bg_tr]() {
            if (!alive_.load(std::memory_order_acquire)) return;
            LyricBundle updated = getLyricsForTrack(bg_tr, false, true, 0);
            if (!updated.translated || updated.lines.empty()) return;
            {
                std::lock_guard<std::mutex> lk(state_mutex_);
                if (has_active_track_ && active_track_.id == bg_tr.id) {
                    active_lyrics_ = updated;
                }
            }
            notifyObservers();
            nlohmann::json larr = nlohmann::json::array();
            for (const auto& l : updated.lines) {
                larr.push_back({
                    {"time_sec", l.has_time ? l.time_sec : 0.0},
                    {"original", l.original},
                    {"translation", l.translation},
                });
            }
            ctx_.publish("entity.lyrics.updated", {
                {"id", bg_tr.id},
                {"title", bg_tr.title},
                {"translated", true},
                {"lines", std::move(larr)},
            });
        });
    };

    std::string sig = item_id;
    if (sig.rfind("cache:", 0) == 0) sig = sig.substr(6);
    if (auto b = cache_.loadLyrics(sig)) {
        maybeScheduleBgTranslate(*b);
        return toSdkLyricLines(b->lines);
    }
    if (!norm.id.empty() && item_id.rfind("cache:", 0) != 0) {
        for (const auto& cb : cache_.listCachedBundles(64)) {
            if (!cb.track.id.empty() && cb.track.id == norm.id && !cb.lines.empty()) {
                maybeScheduleBgTranslate(cb);
                return toSdkLyricLines(cb.lines);
            }
        }
        MusicTrack tr;
        tr.id = norm.id;
        tr.storefront = norm.storefront;
        std::string err;
        adam_.fetchMetadata(norm.id, norm.storefront, tr, err);
        LyricBundle b = getLyricsForTrack(tr, false, false, 0);
        return toSdkLyricLines(b.lines);
    }
    return {};
}

omnidesk::CapabilityResult MusicEngine::handleMediaSourceRpc(const nlohmann::json& req) {
    omnidesk::CapabilityResult res;
    res.provider_plugin_id = "plugin-music";
    std::string action = req.is_object() ? req.value("action", "feed") : "feed";

    if (action == "categories") {
        res.ok = true;
        res.data = {{"categories", {"recommendations", "heavy-rotation", "recently-played", "recently-played-tracks", "library-playlists", "search"}}};
        return res;
    }

    if (action == "drm_status") {
        DrmStatus st = refreshDrmStatus(req.value("ensure_started", false));
        res.ok = true;
        res.data = {
            {"engine_reachable", st.engine_reachable},
            {"available", st.available},
            {"auth_state", st.auth_state},
            {"fairplay_state", st.fairplay_state},
            {"session_state", st.session_state},
            {"cap_alac", st.cap_alac},
            {"cap_hi_res", st.cap_hi_res},
            {"cap_atmos", st.cap_atmos},
            {"error", st.error},
        };
        return res;
    }

    if (action == "metadata") {
        MusicConfig cfg = currentConfig();
        std::string raw_id = req.value("id", req.value("asset_id", ""));
        std::string raw_url = req.value("url", req.value("play_url", ""));
        auto norm = normalizeAppleMusicAsset(raw_id, raw_url, req.value("storefront", ""), adam_.effectiveStorefront());
        if (norm.id.empty()) {
            res.ok = false;
            res.error = "missing asset id";
            return res;
        }
        std::string eng_err;
        adam_.ensureEngineRunning(eng_err);
        std::string err;
        MusicTrack tr;
        if (!adam_.fetchMetadata(norm.id, norm.storefront, tr, err)) {
            res.ok = false;
            res.error = err.empty() ? eng_err : err;
            return res;
        }
        res.ok = true;
        res.data = {
            {"id", tr.id},
            {"storefront", tr.storefront.empty() ? norm.storefront : tr.storefront},
            {"kind", tr.kind},
            {"title", tr.title},
            {"artist", tr.artist},
            {"album", tr.album},
            {"cover_url", tr.cover_url},
            {"duration_sec", tr.duration_sec},
            {"quality_label", tr.quality_label},
            {"has_lyrics", tr.has_lyrics},
        };
        return res;
    }

    if (action == "batch_metadata") {
        const std::string def_sf = adam_.effectiveStorefront();
        std::vector<std::pair<std::string, std::string>> pairs;
        if (req.contains("items") && req["items"].is_array()) {
            for (const auto& row : req["items"]) {
                if (!row.is_object()) continue;
                auto norm = normalizeAppleMusicAsset(
                    row.value("id", row.value("asset_id", "")),
                    row.value("url", row.value("play_url", "")),
                    row.value("storefront", ""),
                    def_sf);
                if (!norm.id.empty()) {
                    pairs.emplace_back(norm.id, norm.storefront);
                }
            }
        }
        std::unordered_map<std::string, MusicTrack> resolved;
        std::string err;
        bool ok = adam_.fetchBatchMetadata(pairs, resolved, err);
        nlohmann::json map_json = nlohmann::json::object();
        for (const auto& [id, tr] : resolved) {
            map_json[id] = {
                {"id", tr.id},
                {"storefront", tr.storefront},
                {"kind", tr.kind},
                {"title", tr.title},
                {"artist", tr.artist},
                {"album", tr.album},
                {"cover_url", tr.cover_url},
                {"duration_sec", tr.duration_sec},
                {"quality_label", tr.quality_label},
                {"has_lyrics", tr.has_lyrics},
            };
        }
        res.ok = ok;
        res.error = err;
        res.data = {{"tracks", std::move(map_json)}, {"count", resolved.size()}};
        return res;
    }

    if (action == "playurl") {
        MusicConfig cfg = currentConfig();
        std::string raw_id = req.value("id", req.value("asset_id", ""));
        std::string raw_url = req.value("url", req.value("play_url", ""));
        auto norm = normalizeAppleMusicAsset(raw_id, raw_url, req.value("storefront", ""), adam_.effectiveStorefront());
        if (norm.id.empty()) {
            res.ok = false;
            res.error = "missing asset id";
            return res;
        }
        std::string err;
        if (!adam_.ensureEngineRunning(err)) {
            res.ok = false;
            res.error = err;
            return res;
        }
        PlaybackSessionInfo sess = adam_.createPlaybackSession(
            norm.id,
            norm.storefront,
            req.value("lossless", cfg.lossless_enabled),
            req.value("hires", cfg.hires_enabled),
            req.value("video", false),
            req.value("atmos", false));
        if (!sess.ok) {
            res.ok = false;
            res.error = sess.error;
            return res;
        }

        std::string prev_rpc_sid;
        bool notify_obs = false;
        {
            std::lock_guard<std::mutex> lk(state_mutex_);
            if (!last_rpc_session_id_.empty() &&
                last_rpc_session_id_ != sess.session_id &&
                last_rpc_session_id_ != active_session_.session_id) {
                prev_rpc_sid = last_rpc_session_id_;
            }
            last_rpc_session_id_ = sess.session_id;
            if (has_active_track_ && active_track_.id == sess.asset_id) {
                if (active_track_.title.empty() && !sess.title.empty()) active_track_.title = sess.title;
                if (active_track_.artist.empty() && !sess.artist.empty()) active_track_.artist = sess.artist;
                if (active_track_.album.empty() && !sess.album.empty()) active_track_.album = sess.album;
                if (active_track_.cover_url.empty() && !sess.cover_url.empty()) active_track_.cover_url = sess.cover_url;
                if (active_track_.duration_sec <= 0.0 && sess.duration_sec > 0.0) active_track_.duration_sec = sess.duration_sec;
                active_track_.quality_label = sess.formattedQuality();
                notify_obs = true;
            }
        }
        if (!prev_rpc_sid.empty()) {
            releasePlaybackSessionAsync(prev_rpc_sid);
        }
        if (notify_obs) {
            notifyObservers();
        }

        std::unordered_map<std::string, std::string> hdr_map;
        makeMpvUrlHeaders(hdr_map);
        nlohmann::json hdr_json = nlohmann::json::object();
        for (const auto& [hk, hv] : hdr_map) {
            hdr_json[hk] = hv;
        }

        const std::string resolved_id = sess.asset_id.empty() ? norm.id : sess.asset_id;
        res.ok = true;
        res.data = {
            {"id", resolved_id},
            {"session_id", sess.session_id},
            {"asset_id", resolved_id},
            {"storefront", sess.storefront.empty() ? norm.storefront : sess.storefront},
            {"codec", sess.codec},
            {"sample_rate", sess.sample_rate},
            {"bit_depth", sess.bit_depth},
            {"quality_label", sess.formattedQuality()},
            {"audio_url", sess.audio_url},
            {"video_url", sess.video_url},
            {"title", sess.title},
            {"artist", sess.artist},
            {"album", sess.album},
            {"cover_url", sess.cover_url},
            {"duration_sec", sess.duration_sec},
            {"http_headers", std::move(hdr_json)},
        };
        return res;
    }

    if (action == "search") {
        std::string q = req.value("query", "");
        FeedOutcome sr = loadFeed("search", q, req.value("force", false));
        nlohmann::json tracks_arr = nlohmann::json::array();
        for (const auto& t : sr.tracks) {
            tracks_arr.push_back({
                {"id", t.id}, {"kind", t.kind}, {"title", t.title},
                {"artist", t.artist}, {"album", t.album},
                {"cover_url", t.cover_url}, {"duration_sec", t.duration_sec},
                {"quality_label", t.quality_label},
            });
        }
        nlohmann::json cont_arr = nlohmann::json::array();
        for (const auto& c : sr.containers) {
            cont_arr.push_back({
                {"id", c.id}, {"type", c.type}, {"name", c.name},
                {"subtitle", c.subtitle}, {"cover_url", c.cover_url},
            });
        }
        res.ok = sr.ok;
        res.error = sr.error;
        res.data = {{"tracks", std::move(tracks_arr)}, {"containers", std::move(cont_arr)}};
        return res;
    }

    if (action == "lyrics") {
        MusicConfig cfg = currentConfig();
        std::string raw_id = req.value("item_id", req.value("id", ""));
        std::string raw_url = req.value("url", req.value("play_url", ""));
        auto norm = normalizeAppleMusicAsset(raw_id, raw_url, req.value("storefront", ""), adam_.effectiveStorefront());
        const std::string lookup = !raw_id.empty() && raw_id.rfind("cache:", 0) == 0 ? raw_id
                                   : (!norm.id.empty() ? ("am-" + norm.storefront + "-" + norm.id) : raw_id);
        auto lines = fetchSdkLyrics(lookup);
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& l : lines) {
            arr.push_back({{"time_sec", l.time_sec}, {"original", l.original}, {"translation", l.translation}});
        }
        res.ok = true;
        res.data = {{"item_id", !norm.id.empty() ? norm.id : raw_id}, {"lyrics", std::move(arr)}};
        return res;
    }

    std::string cat = req.value("category", "music");
    std::string q = req.value("query", "");
    auto items = fetchSdkMediaItems(cat, q);
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& it : items) {
        arr.push_back({
            {"id", it.id}, {"source_id", it.source_id}, {"title", it.title},
            {"creator", it.creator}, {"kind", it.kind}, {"source_badge", it.source_badge},
            {"play_url", it.play_url},
            {"cover_url", it.cover_url}, {"quality_label", it.quality_label},
            {"duration_sec", it.duration_sec}, {"has_bilingual_sub", it.has_bilingual_sub},
            {"extra", it.extra},
        });
    }
    res.ok = true;
    res.data = {{"items", std::move(arr)}, {"count", items.size()}};
    return res;
}

omnidesk::CapabilityResult MusicEngine::handleLyricsRpc(const nlohmann::json& req) {
    omnidesk::CapabilityResult res;
    res.provider_plugin_id = "plugin-music";
    if (!req.is_object()) {
        res.ok = false;
        res.error = "media.lyrics.v1 request must be a JSON object";
        res.data = {{"error_code", "invalid_argument"}};
        return res;
    }

    std::string action = req.value("action", "get");
    if (action == "status" || action == "probe") {
        if (action == "probe") refreshDrmStatus(false);
        EngineStatusSnapshot st = statusSnapshot();
        res.ok = true;
        res.data = {
            {"initialized", st.initialized},
            {"engine_reachable", st.engine_reachable},
            {"auth_state", st.drm.auth_state},
            {"fairplay_state", st.drm.fairplay_state},
            {"cap_alac", st.drm.cap_alac},
            {"cap_hi_res", st.drm.cap_hi_res},
            {"llm_translate_available", st.llm_translate_available},
            {"llm_chat_available", st.llm_chat_available},
            {"cached_lyrics_count", st.cached_lyrics_count},
            {"active_track_id", st.active_track_id},
            {"active_track_title", st.active_track_title},
            {"active_quality", st.active_quality},
        };
        return res;
    }

    if (action == "parse") {
        auto lines = parseLyricsText(req.value("text", req.value("lrc", "")));
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& l : lines) {
            arr.push_back({
                {"id", l.id},
                {"time_sec", l.has_time ? nlohmann::json(l.time_sec) : nlohmann::json(nullptr)},
                {"original", l.original},
                {"translation", l.translation},
            });
        }
        res.ok = true;
        res.data = {{"lines", std::move(arr)}, {"count", lines.size()}};
        return res;
    }

    if (action == "compose" || action == "export") {
        std::string variant = req.value("variant", "bilingual");
        if (req.contains("lines") && req["lines"].is_array()) {
            std::vector<LyricLineEx> lines;
            for (const auto& row : req["lines"]) {
                if (!row.is_object()) continue;
                LyricLineEx l;
                if (row.contains("time_sec") && row["time_sec"].is_number()) {
                    l.time_sec = row["time_sec"].get<double>();
                    l.has_time = (l.time_sec >= 0.0);
                }
                l.original = row.value("original", row.value("text", ""));
                l.translation = row.value("translation", "");
                lines.push_back(std::move(l));
            }
            res.ok = true;
            res.data = {{"variant", variant}, {"lrc", composeLrc(lines, variant)}};
            return res;
        }
        std::string err;
        std::string out_lrc = exportActiveLrc(variant, err);
        if (out_lrc.empty()) {
            res.ok = false;
            res.error = err;
            return res;
        }
        res.ok = true;
        res.data = {{"variant", variant}, {"lrc", out_lrc}};
        return res;
    }

    if (action == "import") {
        std::string raw = req.value("lyrics", req.value("lrc", ""));
        MusicTrack tr;
        const MusicTrack* tr_ptr = nullptr;
        if (req.contains("title") || req.contains("id")) {
            tr.id = req.value("id", "");
            tr.title = req.value("title", "");
            tr.artist = req.value("artist", "");
            tr.album = req.value("album", "");
            tr.duration_sec = req.value("duration_sec", 0.0);
            tr_ptr = &tr;
        }
        std::string err;
        LyricBundle b = importLyricsForActiveOrTrack(tr_ptr, raw, err);
        if (!err.empty()) {
            res.ok = false;
            res.error = err;
            return res;
        }
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& l : b.lines) {
            arr.push_back({
                {"id", l.id},
                {"time_sec", l.has_time ? nlohmann::json(l.time_sec) : nlohmann::json(nullptr)},
                {"original", l.original},
                {"translation", l.translation},
            });
        }
        res.ok = true;
        res.data = {{"signature", b.signature}, {"source", b.source}, {"translated", b.translated}, {"lines", std::move(arr)}};
        return res;
    }

    if (action == "translate") {
        std::string target_lang = req.value("target_language", currentConfig().target_language);
        MusicTrack tr;
        tr.id = req.value("id", "");
        tr.title = req.value("title", "");
        tr.artist = req.value("artist", "");
        std::vector<LyricLineEx> lines;
        if (req.contains("lines") && req["lines"].is_array()) {
            for (const auto& row : req["lines"]) {
                if (row.is_string()) {
                    lines.push_back(LyricLineEx{"", -1.0, false, row.get<std::string>(), ""});
                } else if (row.is_object()) {
                    LyricLineEx l;
                    if (row.contains("time_sec") && row["time_sec"].is_number()) {
                        l.time_sec = row["time_sec"].get<double>();
                        l.has_time = (l.time_sec >= 0.0);
                    }
                    l.original = row.value("original", row.value("text", ""));
                    l.translation = row.value("translation", "");
                    lines.push_back(std::move(l));
                }
            }
            assignStableLineIds(lines);
        } else {
            std::lock_guard<std::mutex> lk(state_mutex_);
            tr = active_track_;
            lines = active_lyrics_.lines;
        }
        if (lines.empty()) {
            res.ok = false;
            res.error = "没有可翻译的歌词行";
            res.data = {{"error_code", "invalid_argument"}};
            return res;
        }
        std::string err;
        bool degraded = false;
        bool ok = llm_.translateMissingLines(tr, lines, target_lang,
                                             currentConfig().translation_batch_size, err, &degraded);
        if (ok && (!tr.id.empty() || !tr.title.empty())) {
            LyricBundle b;
            b.track = tr;
            b.signature = computeTrackSignature(tr);
            b.lines = lines;
            b.source = "cache";
            b.synced = std::all_of(lines.begin(), lines.end(), [](const LyricLineEx& l) { return l.has_time; });
            b.translated = std::any_of(lines.begin(), lines.end(), [](const LyricLineEx& l) { return !l.translation.empty(); });
            b.translation_status = b.translated ? "translated" : "existing";
            b.status_text = statusLabelFor(b.translation_status);
            cache_.saveLyrics(b);
            {
                std::lock_guard<std::mutex> lk(state_mutex_);
                if (has_active_track_ &&
                    ((!tr.id.empty() && active_track_.id == tr.id) ||
                     computeTrackSignature(active_track_) == b.signature)) {
                    active_lyrics_ = b;
                }
            }
            notifyObservers();
        }
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& l : lines) {
            arr.push_back({
                {"id", l.id},
                {"time_sec", l.has_time ? nlohmann::json(l.time_sec) : nlohmann::json(nullptr)},
                {"original", l.original},
                {"translation", l.translation},
            });
        }
        if (ok && !tr.id.empty()) {
            ctx_.publish("entity.lyrics.updated", {
                {"id", tr.id},
                {"title", tr.title},
                {"translated", true},
                {"lines", arr},
            });
        }
        res.ok = ok;
        res.error = err;
        res.data = {{"degraded", degraded}, {"target_language", target_lang}, {"lines", std::move(arr)}};
        return res;
    }

    if (action == "explain" || action == "analyze_line") {
        std::string orig = req.value("original", req.value("line", ""));
        if (!orig.empty()) {
            MusicTrack tr;
            tr.title = req.value("title", "");
            tr.artist = req.value("artist", "");
            LyricLineEx line;
            line.original = orig;
            line.translation = req.value("translation", "");
            line.id = sha256Hex(orig).substr(0, 16);
            PhraseExplanation note = llm_.explainLyricLine(
                tr, line, req.value("prev_line", ""), req.value("next_line", ""),
                req.value("focus_phrase", ""), req.value("target_language", currentConfig().target_language));
            res.ok = true;
            res.data = {
                {"original", note.original},
                {"translation", note.translation},
                {"focus_phrase", note.focus_phrase},
                {"explanation", note.explanation},
                {"degraded", note.degraded},
                {"provider", note.provider},
            };
            return res;
        }
        size_t idx = req.value("line_index", size_t{0});
        PhraseExplanation note = explainLine(idx, req.value("focus_phrase", ""));
        res.ok = true;
        res.data = {
            {"line_id", note.line_id},
            {"original", note.original},
            {"translation", note.translation},
            {"explanation", note.explanation},
            {"degraded", note.degraded},
            {"provider", note.provider},
        };
        return res;
    }

    if (action == "get" || action == "fetch") {
        MusicTrack tr;
        if (req.contains("title") || req.contains("id")) {
            tr.id = req.value("id", "");
            tr.title = req.value("title", "");
            tr.artist = req.value("artist", "");
            tr.album = req.value("album", "");
            tr.storefront = req.value("storefront", "");
            tr.duration_sec = req.value("duration_sec", 0.0);
            tr.embedded_lyrics = req.value("lyrics", "");
        } else {
            std::lock_guard<std::mutex> lk(state_mutex_);
            if (!has_active_track_) {
                res.ok = false;
                res.error = "当前无活动曲目且未传入 id/title";
                res.data = {{"error_code", "invalid_argument"}};
                return res;
            }
            tr = active_track_;
        }
        LyricBundle b = getLyricsForTrack(tr, req.value("force", false),
                                          req.value("bilingual", req.value("translate", false)), 0);
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& l : b.lines) {
            arr.push_back({
                {"id", l.id},
                {"time_sec", l.has_time ? nlohmann::json(l.time_sec) : nlohmann::json(nullptr)},
                {"original", l.original},
                {"translation", l.translation},
            });
        }
        res.ok = true;
        res.data = {
            {"signature", b.signature},
            {"source", b.source},
            {"synced", b.synced},
            {"instrumental", b.instrumental},
            {"translated", b.translated},
            {"translation_status", b.translation_status},
            {"status_text", b.status_text},
            {"error", b.error},
            {"paths", {
                {"original", b.original_lrc_path},
                {"translation", b.zh_lrc_path},
                {"bilingual", b.bilingual_lrc_path},
            }},
            {"lines", std::move(arr)},
        };
        return res;
    }

    res.ok = false;
    res.error = "unsupported media.lyrics.v1 action: " + action;
    res.data = {{"error_code", "invalid_argument"}};
    return res;
}

} // namespace music_plugin
