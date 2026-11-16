#include "omnidesk/playback_coordinator.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <random>
#include <thread>
#include <unordered_map>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "omnidesk/capabilities.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/file_lock.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/types_json.hpp"
#include "omnidesk/ui_task_runner.hpp"

namespace fs = std::filesystem;

namespace omnidesk {
namespace {

std::string resolveLocalPath(const MediaItem& item, const MediaRequestOptions& opts) {
    if (item.extra.is_object()) {
        for (const char* key : {"abs_path", "local_path"}) {
            if (item.extra.contains(key) && item.extra[key].is_string()) {
                const std::string val = item.extra[key].get<std::string>();
                if (!val.empty()) return val;
            }
        }
    }
    const std::string url = !opts.url.empty() ? opts.url : item.play_url;
    constexpr const char kFileScheme[] = "file://";
    if (url.rfind(kFileScheme, 0) == 0) {
        return url.substr(sizeof(kFileScheme) - 1);
    }
    for (const char* remote : {"http://", "https://", "rtsp://", "rtmp://", "smb://"}) {
        if (url.rfind(remote, 0) == 0) return "";
    }
    return url;
}

void populateOptionsFromItem(PlaybackTrack& track) {
    if (track.options.url.empty()) {
        track.options.url = track.item.play_url;
    }
    if (track.item.extra.is_object()) {
        const auto& ex = track.item.extra;
        if (track.options.audio_url.empty() && ex.contains("audio_url") && ex["audio_url"].is_string()) {
            track.options.audio_url = ex["audio_url"].get<std::string>();
        }
        if (track.options.referrer.empty() && ex.contains("referrer") && ex["referrer"].is_string()) {
            track.options.referrer = ex["referrer"].get<std::string>();
        }
        if (track.options.user_agent.empty() && ex.contains("user_agent") && ex["user_agent"].is_string()) {
            track.options.user_agent = ex["user_agent"].get<std::string>();
        }
        if (track.options.http_headers.empty() && ex.contains("http_headers") && ex["http_headers"].is_object()) {
            for (const auto& kv : ex["http_headers"].items()) {
                if (kv.value().is_string()) {
                    track.options.http_headers[kv.key()] = kv.value().get<std::string>();
                }
            }
        }
        if (track.options.subtitle_urls.empty() && ex.contains("subtitle_urls") && ex["subtitle_urls"].is_array()) {
            for (const auto& s : ex["subtitle_urls"]) {
                if (s.is_string() && !s.get_ref<const std::string&>().empty()) {
                    track.options.subtitle_urls.push_back(s.get<std::string>());
                }
            }
        }
        if (track.album.empty() && ex.contains("album") && ex["album"].is_string()) {
            track.album = ex["album"].get<std::string>();
        }
    }
}

bool writeFileAtomic0600(const std::string& path, const std::string& content) {
    if (path.empty()) return false;
    std::error_code ec;
    fs::path p(path);
    if (p.has_parent_path()) {
        fs::create_directories(p.parent_path(), ec);
        if (ec) return false;
    }
    const std::string tmp = path + ".tmp." + std::to_string(::getpid());
    int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;
    size_t off = 0;
    while (off < content.size()) {
        ssize_t n = ::write(fd, content.data() + off, content.size() - off);
        if (n <= 0) {
            ::close(fd);
            ::unlink(tmp.c_str());
            return false;
        }
        off += static_cast<size_t>(n);
    }
    ::fsync(fd);
    ::close(fd);
    if (::rename(tmp.c_str(), path.c_str()) != 0) {
        ::unlink(tmp.c_str());
        return false;
    }
    return true;
}

void appendCuesToLyrics(const nlohmann::json& cues, std::vector<LyricLine>& out) {
    if (!cues.is_array()) return;
    for (const auto& cue : cues) {
        if (!cue.is_object()) continue;
        const double start_sec = cue.value("start_sec", 0.0);
        const std::string text = cue.value("text", std::string{});
        if (!text.empty()) {
            out.push_back(LyricLine{start_sec, text, ""});
        }
    }
}

bool isDigitsOnly(const std::string& s) {
    if (s.empty()) return false;
    return std::all_of(s.begin(), s.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
}

bool isAppleMusicCatalogUrl(const std::string& url) {
    return url.find("music.apple.com/") != std::string::npos;
}

bool isYouTubeCatalogUrl(const std::string& url) {
    return url.find("youtube.com/watch") != std::string::npos ||
           url.find("youtu.be/") != std::string::npos ||
           url.find("youtube.com/shorts/") != std::string::npos;
}

bool isBilibiliCatalogUrl(const std::string& url) {
    return url.find("bilibili.com/video/") != std::string::npos ||
           url.find("b23.tv/") != std::string::npos;
}

bool isCatalogWebPageUrl(const std::string& url) {
    return isAppleMusicCatalogUrl(url) || isYouTubeCatalogUrl(url) || isBilibiliCatalogUrl(url);
}

std::string extractAppleStorefrontFromUrl(const std::string& url) {
    size_t host_pos = url.find("music.apple.com/");
    if (host_pos == std::string::npos) return "";
    size_t p = host_pos + std::char_traits<char>::length("music.apple.com/");
    size_t slash = url.find('/', p);
    if (slash == std::string::npos || slash <= p) return "";
    std::string seg = url.substr(p, slash - p);
    if (seg.size() == 2 &&
        std::isalpha(static_cast<unsigned char>(seg[0])) &&
        std::isalpha(static_cast<unsigned char>(seg[1]))) {
        seg[0] = static_cast<char>(std::tolower(static_cast<unsigned char>(seg[0])));
        seg[1] = static_cast<char>(std::tolower(static_cast<unsigned char>(seg[1])));
        return seg;
    }
    return "";
}

std::string extractAppleSongIdFromUrl(const std::string& url) {
    if (!isAppleMusicCatalogUrl(url)) return "";
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
                if (isDigitsOnly(cand)) return cand;
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
            if (isDigitsOnly(cand)) return cand;
        }
    }
    return "";
}

std::string extractYouTubeIdFromUrl(const std::string& url) {
    size_t v_pos = url.find("v=");
    if (v_pos != std::string::npos && url.find("youtube.com/") != std::string::npos) {
        size_t p = v_pos + 2;
        size_t q = url.find_first_of("&#", p);
        return (q == std::string::npos) ? url.substr(p) : url.substr(p, q - p);
    }
    for (const char* marker : {"youtu.be/", "youtube.com/shorts/"}) {
        size_t pos = url.find(marker);
        if (pos != std::string::npos) {
            size_t p = pos + std::char_traits<char>::length(marker);
            size_t q = url.find_first_of("?&#/", p);
            return (q == std::string::npos) ? url.substr(p) : url.substr(p, q - p);
        }
    }
    return "";
}

struct DeferredStreamTarget {
    bool needed = false;
    std::string target_plugin;
    nlohmann::json playurl_req;
    bool is_apple_music = false;
    std::string apple_song_id;
    std::string apple_storefront;
    std::string apple_url;
};

DeferredStreamTarget analyzeDeferredStreamTarget(const PlaybackTrack& track) {
    DeferredStreamTarget out;
    const auto& item = track.item;
    if (item.id.rfind("demo-", 0) == 0 || item.id.rfind("cache:", 0) == 0) {
        return out;
    }

    const std::string candidate_url = !track.options.url.empty() ? track.options.url : item.play_url;
    const bool url_empty = candidate_url.empty();
    const bool url_is_web = isCatalogWebPageUrl(candidate_url);
    if (!url_empty && !url_is_web) {
        return out;
    }

    // 1. Apple Music (personal library or AI-curated daily/interactive tracks)
    std::string extra_song_id;
    std::string storefront;
    if (item.extra.is_object()) {
        for (const char* key : {"apple_music_song_id", "asset_id", "song_id"}) {
            if (item.extra.contains(key) && item.extra[key].is_string()) {
                extra_song_id = item.extra[key].get<std::string>();
                if (!extra_song_id.empty()) break;
            }
        }
        if (item.extra.contains("storefront") && item.extra["storefront"].is_string()) {
            storefront = item.extra["storefront"].get<std::string>();
        }
    }
    std::string pref_song_id;
    std::string pref_sf;
    if (item.id.rfind("am-", 0) == 0 && item.id.size() > 6 && item.id[5] == '-' &&
        std::isalpha(static_cast<unsigned char>(item.id[3])) &&
        std::isalpha(static_cast<unsigned char>(item.id[4]))) {
        pref_sf = item.id.substr(3, 2);
        pref_sf[0] = static_cast<char>(std::tolower(static_cast<unsigned char>(pref_sf[0])));
        pref_sf[1] = static_cast<char>(std::tolower(static_cast<unsigned char>(pref_sf[1])));
        std::string rest = item.id.substr(6);
        if (isDigitsOnly(rest)) pref_song_id = rest;
        else {
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && isDigitsOnly(rest.substr(dash + 1))) {
                pref_song_id = rest.substr(dash + 1);
            }
        }
    } else if (item.id.rfind("ix-am-", 0) == 0) {
        std::string rest = item.id.substr(6);
        if (isDigitsOnly(rest)) pref_song_id = rest;
        else {
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && isDigitsOnly(rest.substr(dash + 1))) {
                pref_song_id = rest.substr(dash + 1);
            }
        }
    }
    const std::string url_song_id = extractAppleSongIdFromUrl(candidate_url);
    const std::string url_sf = extractAppleStorefrontFromUrl(candidate_url);

    std::string resolved_am_id;
    if (isDigitsOnly(extra_song_id)) {
        resolved_am_id = extra_song_id;
    } else if (!pref_song_id.empty()) {
        resolved_am_id = pref_song_id;
    } else if (!url_song_id.empty()) {
        resolved_am_id = url_song_id;
    } else if (item.source_id == "plugin-music" && !item.id.empty()) {
        resolved_am_id = item.id;
    } else if (item.source_id == "plugin-ai-curator" && item.kind == "music" && isDigitsOnly(item.id)) {
        resolved_am_id = item.id;
    }

    if (!resolved_am_id.empty()) {
        std::string sf = storefront;
        if (sf.empty()) sf = url_sf;
        if (sf.empty()) sf = pref_sf;
        if (sf.empty() && item.source_id == "plugin-ai-curator") sf = "cn";
        out.needed = true;
        out.is_apple_music = true;
        out.target_plugin = "plugin-music";
        out.apple_song_id = resolved_am_id;
        out.apple_storefront = sf;
        out.apple_url = candidate_url;
        out.playurl_req = {
            {"action", "playurl"},
            {"id", resolved_am_id},
            {"video", item.kind == "mv"},
        };
        if (!sf.empty()) out.playurl_req["storefront"] = sf;
        if (!candidate_url.empty()) out.playurl_req["url"] = candidate_url;
        return out;
    }

    // 2. YouTube / Bilibili catalog page URLs or empty URLs
    if (item.source_id == "youtube" || item.source_id == "plugin-youtube" || isYouTubeCatalogUrl(candidate_url)) {
        std::string yt_id;
        if (item.extra.is_object()) {
            for (const char* k : {"youtube_id", "video_id"}) {
                if (item.extra.contains(k) && item.extra[k].is_string() && !item.extra[k].get_ref<const std::string&>().empty()) {
                    yt_id = item.extra[k].get<std::string>();
                    break;
                }
            }
        }
        if (yt_id.empty()) yt_id = extractYouTubeIdFromUrl(candidate_url);
        if (yt_id.empty() && (item.source_id == "youtube" || item.source_id == "plugin-youtube")) yt_id = item.id;
        if (!yt_id.empty()) {
            out.needed = true;
            out.target_plugin = "plugin-youtube";
            out.playurl_req = {{"action", "playurl"}, {"id", yt_id}};
            return out;
        }
    }

    if (item.source_id == "bilibili" || item.source_id == "plugin-bilibili" || isBilibiliCatalogUrl(candidate_url)) {
        std::string bvid;
        if (item.extra.is_object() && item.extra.contains("bvid") && item.extra["bvid"].is_string()) {
            bvid = item.extra["bvid"].get<std::string>();
        }
        if (bvid.empty() && (item.source_id == "bilibili" || item.source_id == "plugin-bilibili")) {
            bvid = item.id;
        }
        if (!bvid.empty()) {
            out.needed = true;
            out.target_plugin = "plugin-bilibili";
            out.playurl_req = {{"action", "playurl"}, {"id", bvid}};
            return out;
        }
    }

    return out;
}

bool hasMediaSourceProvider(const CapabilityRegistry& caps, const std::string& plugin_id) {
    if (plugin_id.empty()) return false;
    const auto providers = caps.providersFor("media.source.v1");
    return std::any_of(providers.begin(), providers.end(), [&](const CapabilityDescriptor& provider) {
        return provider.plugin_id == plugin_id;
    });
}

} // namespace

const char* playbackRoleLabel(PlaybackRole role) {
    switch (role) {
        case PlaybackRole::None: return "none";
        case PlaybackRole::BackgroundMusic: return "background_music";
        case PlaybackRole::ForegroundVideo: return "foreground_video";
    }
    return "none";
}

struct PlaybackCoordinator::Impl {
    mutable std::mutex mutex;
    std::string state_path;
    CapabilityRegistry* caps = nullptr;
    EntityBus* bus = nullptr;
    JobCenter* jobs = nullptr;
    UiTaskRunner* ui_tasks = nullptr;
    MPVCore* mpv = nullptr;
    uint64_t mpv_sub_token = 0;

    PlaybackSessionState session;
    uint64_t next_lease_id = 1;
    uint64_t generation = 0;
    bool user_stopped = true;
    bool was_stopped = true;
    bool stream_resolving = false;
    std::string last_auto_skipped_sponsor_key;
    double last_persisted_pos = -1.0;
    std::string last_persisted_item_id;

    nlohmann::json resume_ledger = nlohmann::json::object();
    std::shared_ptr<CancelToken> stream_resolve_cancel;
    std::shared_ptr<CancelToken> enrich_cancel;
    std::shared_ptr<CancelToken> sub_cancel;
    std::vector<std::thread> detached_helpers;

    std::unordered_map<uint64_t, StateCallback> subscribers;
    uint64_t next_sub_token = 1;
    std::shared_ptr<std::atomic<bool>> alive = std::make_shared<std::atomic<bool>>(true);

    void loadLedger() {
        if (state_path.empty()) return;
        ScopedFileLock flock(state_path, FileLock::Mode::Shared);
        std::ifstream ifs(state_path);
        if (!ifs.is_open()) return;
        try {
            nlohmann::json j;
            ifs >> j;
            if (j.is_object()) {
                resume_ledger = std::move(j);
            }
        } catch (...) {
        }
    }

    void persistLedger(const nlohmann::json& snapshot) {
        if (state_path.empty()) return;
        ScopedFileLock flock(state_path, FileLock::Mode::Exclusive);
        nlohmann::json merged = resume_ledger;
        std::ifstream ifs(state_path);
        if (ifs.is_open()) {
            try {
                nlohmann::json disk;
                ifs >> disk;
                if (disk.is_object()) {
                    for (auto it = disk.begin(); it != disk.end(); ++it) {
                        if (!snapshot.contains(it.key())) {
                            merged[it.key()] = it.value();
                        }
                    }
                }
            } catch (...) {
            }
        }
        for (auto it = snapshot.begin(); it != snapshot.end(); ++it) {
            merged[it.key()] = it.value();
        }
        // Cap ledger size to most recent 500 entries
        if (merged.size() > 500) {
            std::vector<std::pair<int64_t, std::string>> entries;
            for (auto it = merged.begin(); it != merged.end(); ++it) {
                int64_t ts = it.value().is_object() ? it.value().value("updated_ms", int64_t{0}) : 0;
                entries.emplace_back(ts, it.key());
            }
            std::sort(entries.begin(), entries.end());
            size_t to_remove = merged.size() - 500;
            for (size_t i = 0; i < to_remove && i < entries.size(); ++i) {
                merged.erase(entries[i].second);
            }
        }
        writeFileAtomic0600(state_path, merged.dump(2) + "\n");
    }

    void notifyListeners(const PlaybackSessionState& snap, bool publish_bus = false,
                         const std::string& bus_topic = "playback.state_changed") {
        std::vector<StateCallback> cbs;
        EntityBus* bus_ptr = nullptr;
        {
            std::lock_guard<std::mutex> lk(mutex);
            for (const auto& [_, cb] : subscribers) cbs.push_back(cb);
            bus_ptr = bus;
        }
        for (const auto& cb : cbs) {
            if (cb) {
                try {
                    cb(snap);
                } catch (...) {
                }
            }
        }
        if (publish_bus && bus_ptr) {
            bus_ptr->publish(bus_topic, {
                {"role", playbackRoleLabel(snap.active_role)},
                {"owner", snap.active_owner},
                {"lease_id", snap.lease_id},
                {"item_id", snap.active_track.item.id},
                {"title", snap.active_track.item.title},
                {"paused", snap.mpv.paused},
                {"stopped", snap.mpv.stopped},
                {"position_sec", snap.mpv.position_sec},
                {"duration_sec", snap.mpv.duration_sec},
                {"has_suspended_music", snap.has_suspended_music},
            });
        }
    }

    void saveResumePointLocked(const std::string& item_id,
                               const std::string& title,
                               double pos_sec,
                               double dur_sec,
                               bool force_write) {
        if (item_id.empty() || item_id.rfind("demo-", 0) == 0) return;
        if (pos_sec < 3.0) return;
        if (dur_sec > 10.0 && pos_sec >= dur_sec - 4.0) {
            resume_ledger.erase(item_id);
            last_persisted_pos = -1.0;
            return;
        }
        if (!force_write && last_persisted_item_id == item_id &&
            std::abs(pos_sec - last_persisted_pos) < 3.0) {
            return;
        }
        const int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                   std::chrono::system_clock::now().time_since_epoch())
                                   .count();
        resume_ledger[item_id] = {
            {"item_id", item_id},
            {"title", title},
            {"position_sec", pos_sec},
            {"duration_sec", dur_sec},
            {"updated_ms", now_ms},
        };
        last_persisted_pos = pos_sec;
        last_persisted_item_id = item_id;
    }

    double lookupResumePointLocked(const std::string& item_id) const {
        if (item_id.empty() || !resume_ledger.contains(item_id)) return 0.0;
        const auto& rec = resume_ledger[item_id];
        if (!rec.is_object()) return 0.0;
        return std::max(0.0, rec.value("position_sec", 0.0));
    }

    void startStreamResolution(uint64_t gen, PlaybackTrack track, DeferredStreamTarget target) {
        CapabilityRegistry* caps_ptr = nullptr;
        UiTaskRunner* runner = nullptr;
        std::shared_ptr<CancelToken> cancel;
        std::shared_ptr<std::atomic<bool>> alive_flag = alive;
        {
            std::lock_guard<std::mutex> lk(mutex);
            if (stream_resolve_cancel) stream_resolve_cancel->cancel();
            cancel = CancelToken::create();
            stream_resolve_cancel = cancel;
            caps_ptr = caps;
            runner = ui_tasks;
        }
        if (!caps_ptr || !target.needed) {
            std::lock_guard<std::mutex> lk(mutex);
            if (generation == gen) {
                stream_resolving = false;
            }
            return;
        }
        if (!hasMediaSourceProvider(*caps_ptr, target.target_plugin)) {
            PlaybackSessionState err_snap;
            bool notify_err = false;
            {
                std::lock_guard<std::mutex> lk(mutex);
                if (generation == gen) {
                    stream_resolving = false;
                    session.mpv.last_error =
                        "未安装 " + target.target_plugin + "（media.source.v1），无法解析播放地址";
                    err_snap = session;
                    notify_err = true;
                }
            }
            if (notify_err) {
                notifyListeners(err_snap, true, "playback.state_changed");
            }
            return;
        }

        auto run_work = [this, alive_flag, caps_ptr, gen, track = std::move(track),
                         target = std::move(target)](const std::shared_ptr<CancelToken>& c) mutable {
            if (!alive_flag->load(std::memory_order_acquire) || (c && c->isCancelled())) return;

            CapabilityResult res =
                caps_ptr->callProvider(target.target_plugin, "media.source.v1", target.playurl_req);
            if (!alive_flag->load(std::memory_order_acquire) || (c && c->isCancelled())) return;

            if (!res.ok || !res.data.is_object()) {
                PlaybackSessionState err_snap;
                bool notify_err = false;
                {
                    std::lock_guard<std::mutex> lk(mutex);
                    if (generation == gen && alive_flag->load(std::memory_order_acquire)) {
                        stream_resolving = false;
                        session.mpv.last_error = res.error.empty() ? "无法解析流媒体播放地址" : res.error;
                        err_snap = session;
                        notify_err = true;
                    }
                }
                if (notify_err) {
                    notifyListeners(err_snap, true, "playback.state_changed");
                }
                return;
            }

            const std::string audio_url = res.data.value("audio_url", std::string{});
            const std::string video_url = res.data.value("video_url", std::string{});
            std::string primary_url;
            std::string separate_audio_url;
            if (target.is_apple_music) {
                const bool is_mv = (track.item.kind == "mv" && !video_url.empty());
                primary_url = is_mv ? video_url : (!audio_url.empty() ? audio_url : video_url);
                if (is_mv && !audio_url.empty() && audio_url != video_url) {
                    separate_audio_url = audio_url;
                }
            } else {
                primary_url = !video_url.empty() ? video_url : audio_url;
                if (!video_url.empty() && !audio_url.empty() && audio_url != video_url) {
                    separate_audio_url = audio_url;
                }
            }

            if (primary_url.empty()) {
                std::lock_guard<std::mutex> lk(mutex);
                if (generation == gen) stream_resolving = false;
                return;
            }

            MediaRequestOptions resolved_opts;
            MPVCore* mpv_ptr = nullptr;
            PlaybackSessionState snap;
            bool should_load = false;
            bool need_lyrics = false;
            {
                std::lock_guard<std::mutex> lk(mutex);
                if (generation == gen && alive_flag->load(std::memory_order_acquire) && !user_stopped) {
                    auto& active = session.active_track;
                    active.options.url = primary_url;
                    if (!separate_audio_url.empty()) {
                        active.options.audio_url = separate_audio_url;
                    }
                    if (res.data.contains("http_headers") && res.data["http_headers"].is_object()) {
                        for (const auto& kv : res.data["http_headers"].items()) {
                            if (kv.value().is_string()) {
                                active.options.http_headers[kv.key()] = kv.value().get<std::string>();
                            }
                        }
                    }
                    if (target.target_plugin == "plugin-bilibili") {
                        if (res.data.value("android_media", false)) {
                            active.options.referrer.clear();
                            active.options.user_agent = "Bilibili Freedoooooom/MarkII";
                        } else if (active.options.referrer.empty()) {
                            active.options.referrer = "https://www.bilibili.com";
                            active.options.user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36";
                        }
                    } else if (target.target_plugin == "plugin-youtube") {
                        active.options.referrer.clear();
                        if (active.options.user_agent.empty()) {
                            active.options.user_agent =
                                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36";
                        }
                    }

                    const std::string q_lbl = res.data.value("quality_label", std::string{});
                    const std::string cov = res.data.value("cover_url", std::string{});
                    const std::string alb = res.data.value("album", std::string{});
                    const std::string ttl = res.data.value("title", std::string{});
                    const std::string art = res.data.value("artist", std::string{});
                    const double dur = res.data.value("duration_sec", 0.0);

                    if (!q_lbl.empty()) active.item.quality_label = q_lbl;
                    if (active.item.cover_url.empty() && !cov.empty()) active.item.cover_url = cov;
                    if (active.album.empty() && !alb.empty()) active.album = alb;
                    if (active.item.title.empty() && !ttl.empty()) active.item.title = ttl;
                    if (active.item.creator.empty() && !art.empty()) active.item.creator = art;
                    if (active.item.duration_sec <= 0.0 && dur > 0.0) active.item.duration_sec = dur;

                    if (session.music_queue_index >= 0 &&
                        static_cast<size_t>(session.music_queue_index) < session.music_queue.size() &&
                        session.music_queue[static_cast<size_t>(session.music_queue_index)].item.id == active.item.id) {
                        auto& q_item = session.music_queue[static_cast<size_t>(session.music_queue_index)];
                        if (!q_lbl.empty()) q_item.item.quality_label = q_lbl;
                        if (q_item.item.cover_url.empty() && !cov.empty()) q_item.item.cover_url = cov;
                        if (q_item.album.empty() && !alb.empty()) q_item.album = alb;
                        if (q_item.item.duration_sec <= 0.0 && dur > 0.0) q_item.item.duration_sec = dur;
                    }

                    resolved_opts = active.options;
                    need_lyrics = target.is_apple_music && active.lyrics.empty();
                    stream_resolving = false;
                    was_stopped = false;
                    mpv_ptr = mpv;
                    snap = session;
                    should_load = true;
                } else if (generation == gen) {
                    stream_resolving = false;
                }
            }

            if (should_load) {
                if (mpv_ptr) {
                    mpv_ptr->load(resolved_opts);
                    std::lock_guard<std::mutex> lk(mutex);
                    if (generation == gen) {
                        session.mpv = mpv_ptr->state();
                        if (session.mpv.duration_sec <= 0.0 &&
                            session.active_track.item.duration_sec > 0.0) {
                            session.mpv.duration_sec = session.active_track.item.duration_sec;
                        }
                        snap = session;
                    }
                }
                notifyListeners(snap, true, "playback.track_changed");
            }

            if (should_load && need_lyrics && alive_flag->load(std::memory_order_acquire) &&
                !(c && c->isCancelled())) {
                nlohmann::json lreq = {
                    {"action", "lyrics"},
                    {"id", target.apple_song_id},
                };
                if (!target.apple_storefront.empty()) lreq["storefront"] = target.apple_storefront;
                if (!target.apple_url.empty()) lreq["url"] = target.apple_url;
                CapabilityResult lres = caps_ptr->callProvider("plugin-music", "media.source.v1", lreq);
                if (lres.ok && lres.data.is_object() &&
                    alive_flag->load(std::memory_order_acquire) && !(c && c->isCancelled())) {
                    const nlohmann::json* larr = nullptr;
                    if (lres.data.contains("lyrics") && lres.data["lyrics"].is_array()) {
                        larr = &lres.data["lyrics"];
                    } else if (lres.data.contains("lines") && lres.data["lines"].is_array()) {
                        larr = &lres.data["lines"];
                    }
                    std::vector<LyricLine> fetched;
                    if (larr) {
                        for (const auto& row : *larr) {
                            if (!row.is_object()) continue;
                            fetched.push_back(LyricLine{
                                row.value("time_sec", 0.0),
                                row.value("original", std::string{}),
                                row.value("translation", std::string{}),
                            });
                        }
                    }
                    if (!fetched.empty()) {
                        PlaybackSessionState lsnap;
                        bool notify_lyrics = false;
                        {
                            std::lock_guard<std::mutex> lk(mutex);
                            if (generation == gen && alive_flag->load(std::memory_order_acquire) &&
                                session.active_track.lyrics.empty()) {
                                session.active_track.lyrics = std::move(fetched);
                                lsnap = session;
                                notify_lyrics = true;
                            }
                        }
                        if (notify_lyrics) {
                            notifyListeners(lsnap, true, "playback.lyrics_updated");
                        }
                    }
                }
            }
        };

        if (runner) {
            runner->submitInLane("playback.resolve", std::move(run_work), cancel);
        } else {
            std::thread([work = std::move(run_work), cancel]() mutable { work(cancel); }).detach();
        }
    }

    void startEnrichment(uint64_t gen, const PlaybackTrack& track) {
        CapabilityRegistry* caps_ptr = nullptr;
        UiTaskRunner* runner = nullptr;
        std::shared_ptr<CancelToken> cancel;
        {
            std::lock_guard<std::mutex> lk(mutex);
            if (enrich_cancel) enrich_cancel->cancel();
            cancel = CancelToken::create();
            enrich_cancel = cancel;
            caps_ptr = caps;
            runner = ui_tasks;
        }
        if (!caps_ptr) return;

        const bool want_timeline =
            track.auto_enrich_timeline &&
            caps_ptr->has("player.timeline.v1") &&
            track.item.id.rfind("demo-", 0) != 0;

        const bool want_subtitles =
            track.auto_enrich_subtitles &&
            track.options.subtitle_urls.empty() &&
            track.lyrics.empty() &&
            caps_ptr->has("subtitle.fetch.v1") &&
            track.item.id.rfind("demo-", 0) != 0;

        if (!want_timeline && !want_subtitles) return;

        auto run_work = [this, caps_ptr, gen, track, want_timeline, want_subtitles](
                            const std::shared_ptr<CancelToken>& c) {
            if (c && c->isCancelled()) return;

            if (want_timeline) {
                std::string timeline_vid = track.item.id;
                uint64_t cid = 0;
                if (track.item.extra.is_object()) {
                    for (const char* key : {"bvid", "youtube_id", "video_id"}) {
                        if (track.item.extra.contains(key) && track.item.extra[key].is_string() &&
                            !track.item.extra[key].get_ref<const std::string&>().empty()) {
                            timeline_vid = track.item.extra[key].get<std::string>();
                            break;
                        }
                    }
                    if (track.item.extra.contains("cid") &&
                        (track.item.extra["cid"].is_number_integer() ||
                         track.item.extra["cid"].is_number_unsigned())) {
                        cid = track.item.extra["cid"].get<uint64_t>();
                    }
                }
                if (!timeline_vid.empty()) {
                    nlohmann::json req = {
                        {"action", "fetch_segments"},
                        {"video_id", timeline_vid},
                        {"platform", track.item.source_id.empty() ? "auto" : track.item.source_id},
                        {"duration_sec", track.item.duration_sec},
                        {"existing_chapters", toJsonArray(track.chapters)},
                    };
                    if (cid > 0) req["cid"] = cid;
                    auto res = caps_ptr->callCancellable("player.timeline.v1", req, c);
                    if (res.ok && !(c && c->isCancelled()) &&
                        res.data.is_object() && res.data.contains("chapters") &&
                        res.data["chapters"].is_array()) {
                        auto merged = fromJsonArray<ChapterSegment>(res.data["chapters"]);
                        PlaybackSessionState snap;
                        MPVCore* mpv_ptr = nullptr;
                        bool still_current = false;
                        {
                            std::lock_guard<std::mutex> lk(mutex);
                            if (generation == gen && alive->load(std::memory_order_acquire)) {
                                session.active_track.chapters = merged;
                                snap = session;
                                mpv_ptr = mpv;
                                still_current = true;
                            }
                        }
                        if (still_current) {
                            if (mpv_ptr) mpv_ptr->setChapters(std::move(merged));
                            notifyListeners(snap, true, "playback.timeline_updated");
                        }
                    }
                }
            }

            if (want_subtitles && !(c && c->isCancelled())) {
                const std::string local_path = resolveLocalPath(track.item, track.options);
                nlohmann::json sreq = {{"action", "search"}, {"provider", "all"}};
                if (!local_path.empty()) sreq["filename"] = local_path;
                else sreq["query"] = track.item.title;
                auto sres = caps_ptr->callCancellable("subtitle.fetch.v1", sreq, c);
                if (sres.ok && sres.data.is_object() &&
                    sres.data.contains("candidates") && sres.data["candidates"].is_array() &&
                    !sres.data["candidates"].empty() && !(c && c->isCancelled())) {
                    const auto& cand = sres.data["candidates"][0];
                    if (cand.is_object()) {
                        auto dres = caps_ptr->callCancellable(
                            "subtitle.fetch.v1",
                            {
                                {"action", "download"},
                                {"provider", cand.value("provider", std::string{})},
                                {"id", cand.value("id", std::string{})},
                            },
                            c);
                        if (dres.ok && dres.data.contains("files") &&
                            dres.data["files"].is_array() && !dres.data["files"].empty() &&
                            !(c && c->isCancelled())) {
                            std::string sub_path = dres.data["files"][0].value("path", std::string{});
                            if (!local_path.empty() && !sub_path.empty() &&
                                caps_ptr->has("subtitle.sync.v1")) {
                                auto sync_res = caps_ptr->callCancellable(
                                    "subtitle.sync.v1",
                                    {
                                        {"action", "sync"},
                                        {"subtitle_path", sub_path},
                                        {"reference_path", local_path},
                                        {"mode", "auto"},
                                    },
                                    c);
                                if (sync_res.ok && !sync_res.data.value("output_path", std::string{}).empty()) {
                                    sub_path = sync_res.data["output_path"].get<std::string>();
                                }
                            }
                            if (!sub_path.empty()) {
                                PlaybackSessionState snap;
                                MPVCore* mpv_ptr = nullptr;
                                bool still_current = false;
                                {
                                    std::lock_guard<std::mutex> lk(mutex);
                                    if (generation == gen && alive->load(std::memory_order_acquire)) {
                                        session.active_track.options.subtitle_urls.push_back(sub_path);
                                        session.subtitles_attached = true;
                                        session.subtitle_status =
                                            "已挂载外挂字幕: " + fs::path(sub_path).filename().string();
                                        snap = session;
                                        mpv_ptr = mpv;
                                        still_current = true;
                                    }
                                }
                                if (still_current) {
                                    if (mpv_ptr) mpv_ptr->addSubtitleFile(sub_path);
                                    notifyListeners(snap, true, "playback.subtitles_attached");
                                }
                            }
                        }
                    }
                }
            }
        };

        if (runner) {
            runner->submitInLane("playback.enrich", std::move(run_work), cancel);
        } else {
            std::thread([work = std::move(run_work), cancel]() { work(cancel); }).detach();
        }
    }

    void onMpvStateChanged(const MpvPlaybackState& st) {
        PlaybackSessionState snap;
        std::optional<double> auto_skip_target;
        bool should_advance_queue = false;
        nlohmann::json ledger_to_persist;
        bool need_persist = false;
        bool notify_bus = false;
        std::string bus_topic = "playback.state_changed";

        {
            std::lock_guard<std::mutex> lk(mutex);
            const bool prev_paused = session.mpv.paused;
            const bool prev_stopped = was_stopped;
            session.mpv = st;
            if (session.mpv.duration_sec <= 0.0 &&
                session.active_track.item.duration_sec > 0.0) {
                session.mpv.duration_sec = session.active_track.item.duration_sec;
            }

            if (session.active_role != PlaybackRole::None && !session.active_track.item.id.empty()) {
                const double before = last_persisted_pos;
                saveResumePointLocked(session.active_track.item.id,
                                      session.active_track.item.title,
                                      st.position_sec,
                                      session.mpv.duration_sec,
                                      false);
                if (last_persisted_pos != before) {
                    ledger_to_persist = resume_ledger;
                    need_persist = true;
                }
            }

            // Check SponsorBlock active segment
            std::optional<ChapterSegment> hit;
            for (const auto& ch : session.active_track.chapters) {
                if ((ch.kind == "sponsor" || ch.kind == "ad" ||
                     ch.kind == "intro" || ch.kind == "outro") &&
                    st.position_sec >= ch.start_sec &&
                    st.position_sec < ch.end_sec) {
                    hit = ch;
                    break;
                }
            }
            session.active_sponsor = hit;
            if (hit && session.auto_skip_sponsor) {
                const std::string key = session.active_track.item.id + ":" +
                                        std::to_string(static_cast<int>(hit->start_sec * 10));
                if (last_auto_skipped_sponsor_key != key) {
                    last_auto_skipped_sponsor_key = key;
                    auto_skip_target = hit->end_sec + 0.05;
                }
            }

            // Detect natural EOF on BackgroundMusic queue (never skip-loop on stream errors)
            if (!prev_stopped && st.stopped && st.last_error.empty() &&
                !user_stopped && !stream_resolving &&
                session.active_role == PlaybackRole::BackgroundMusic &&
                !session.music_queue.empty()) {
                should_advance_queue = true;
            }
            was_stopped = st.stopped;
            if (prev_paused != st.paused || prev_stopped != st.stopped) {
                notify_bus = true;
            }
            snap = session;
        }

        if (need_persist) {
            persistLedger(ledger_to_persist);
        }
        if (auto_skip_target) {
            MPVCore* m = nullptr;
            {
                std::lock_guard<std::mutex> lk(mutex);
                m = mpv;
            }
            if (m) m->seekAbsolute(*auto_skip_target);
            if (bus) {
                bus->publish("playback.sponsor_skipped", {
                    {"item_id", snap.active_track.item.id},
                    {"seek_to", *auto_skip_target},
                });
            }
        }
        notifyListeners(snap, notify_bus, bus_topic);
        if (should_advance_queue) {
            owner_ptr->nextTrack();
        }
    }

    PlaybackCoordinator* owner_ptr = nullptr;
};

PlaybackCoordinator& PlaybackCoordinator::instance() {
    static PlaybackCoordinator inst;
    return inst;
}

PlaybackCoordinator::PlaybackCoordinator() : impl_(std::make_unique<Impl>()) {
    impl_->owner_ptr = this;
    impl_->mpv = &MPVCore::instance();
}

PlaybackCoordinator::~PlaybackCoordinator() {
    shutdown();
}

void PlaybackCoordinator::configure(std::string state_path,
                                    CapabilityRegistry* caps,
                                    EntityBus* bus,
                                    JobCenter* jobs,
                                    UiTaskRunner* ui_tasks,
                                    MPVCore* mpv) {
    MPVCore* target_mpv = mpv ? mpv : &MPVCore::instance();
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (impl_->mpv && impl_->mpv_sub_token != 0) {
            impl_->mpv->unsubscribeState(impl_->mpv_sub_token);
            impl_->mpv_sub_token = 0;
        }
        impl_->state_path = std::move(state_path);
        impl_->caps = caps;
        impl_->bus = bus;
        impl_->jobs = jobs;
        impl_->ui_tasks = ui_tasks;
        impl_->mpv = target_mpv;
        impl_->alive->store(true, std::memory_order_release);
        impl_->loadLedger();
    }
    auto alive = impl_->alive;
    uint64_t tok = target_mpv->subscribeState([this, alive](const MpvPlaybackState& st) {
        if (!alive->load(std::memory_order_acquire)) return;
        impl_->onMpvStateChanged(st);
    });
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->mpv_sub_token = tok;
        impl_->session.mpv = target_mpv->state();
    }
}

void PlaybackCoordinator::registerCapabilities(CapabilityRegistry& caps) {
    caps.registerCapability(
        "playback.session.v1",
        "host.playback",
        [this](const nlohmann::json& req) -> CapabilityResult {
            if (!req.is_object()) {
                return {false, {{"error_code", "invalid_argument"}}, "request must be an object", "host.playback", false};
            }
            const std::string action = req.value("action", "status");
            if (action == "status") {
                const auto st = state();
                return {true, {
                    {"role", playbackRoleLabel(st.active_role)},
                    {"owner", st.active_owner},
                    {"lease_id", st.lease_id},
                    {"item", toJson(st.active_track.item)},
                    {"paused", st.mpv.paused},
                    {"stopped", st.mpv.stopped},
                    {"playing", st.mpv.playing},
                    {"position_sec", st.mpv.position_sec},
                    {"duration_sec", st.mpv.duration_sec},
                    {"volume", st.mpv.volume},
                    {"speed", st.mpv.speed},
                    {"has_suspended_music", st.has_suspended_music},
                    {"suspended_music_position_sec", st.suspended_music_position_sec},
                    {"queue_size", st.music_queue.size()},
                    {"queue_index", st.music_queue_index},
                    {"subtitles_attached", st.subtitles_attached},
                    {"subtitle_status", st.subtitle_status},
                }, "", "host.playback", false};
            }
            if (action == "play") { play(); return {true, {{"ok", true}}, "", "host.playback", false}; }
            if (action == "pause") { pause(); return {true, {{"ok", true}}, "", "host.playback", false}; }
            if (action == "play_pause") { playPause(); return {true, {{"ok", true}}, "", "host.playback", false}; }
            if (action == "stop") {
                stop(req.value("resume_suspended", false));
                return {true, {{"ok", true}}, "", "host.playback", false};
            }
            if (action == "next") { nextTrack(); return {true, {{"ok", true}}, "", "host.playback", false}; }
            if (action == "previous") { prevTrack(); return {true, {{"ok", true}}, "", "host.playback", false}; }
            if (action == "resume_music") {
                bool resumed = resumeSuspendedMusic();
                return {resumed, {{"resumed", resumed}}, resumed ? "" : "no suspended music session", "host.playback", false};
            }
            if (action == "skip_sponsor") {
                bool skipped = skipActiveSponsor();
                return {skipped, {{"skipped", skipped}}, skipped ? "" : "no active sponsor segment", "host.playback", false};
            }
            if (action == "seek") {
                if (req.contains("position_sec") && req["position_sec"].is_number()) {
                    seekAbsolute(req["position_sec"].get<double>());
                    return {true, {{"ok", true}}, "", "host.playback", false};
                }
                if (req.contains("delta_sec") && req["delta_sec"].is_number()) {
                    seekRelative(req["delta_sec"].get<double>());
                    return {true, {{"ok", true}}, "", "host.playback", false};
                }
                return {false, {{"error_code", "invalid_argument"}}, "position_sec or delta_sec required", "host.playback", false};
            }
            if (action == "set_volume" && req.contains("volume") && req["volume"].is_number_integer()) {
                setVolume(req["volume"].get<int>());
                return {true, {{"ok", true}}, "", "host.playback", false};
            }
            if (action == "set_speed" && req.contains("speed") && req["speed"].is_number()) {
                setSpeed(req["speed"].get<double>());
                return {true, {{"ok", true}}, "", "host.playback", false};
            }
            if (action == "attach_subtitles") {
                attachSubtitlesAsync(true);
                return {true, {{"started", true}}, "", "host.playback", false};
            }
            if (action == "generate_asr_subtitles") {
                const std::string jid = triggerAsrHotMount();
                return {!jid.empty(), {{"job_id", jid}}, jid.empty() ? "ASR unavailable or not local media" : "", "host.playback", false};
            }
            return {false, {{"error_code", "invalid_argument"}}, "Unsupported playback.session.v1 action: " + action, "host.playback", false};
        },
        {{"name", "统一播放会话仲裁器"}, {"version", "1.0.0"}},
        100);
}

void PlaybackCoordinator::shutdown() {
    nlohmann::json ledger_to_persist;
    MPVCore* mpv_ptr = nullptr;
    uint64_t sub_tok = 0;
    std::shared_ptr<CancelToken> rc;
    std::shared_ptr<CancelToken> ec;
    std::shared_ptr<CancelToken> sc;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->alive->store(false, std::memory_order_release);
        impl_->stream_resolving = false;
        if (impl_->session.active_role != PlaybackRole::None &&
            !impl_->session.active_track.item.id.empty()) {
            impl_->saveResumePointLocked(impl_->session.active_track.item.id,
                                         impl_->session.active_track.item.title,
                                         impl_->session.mpv.position_sec,
                                         impl_->session.mpv.duration_sec,
                                         true);
        }
        ledger_to_persist = impl_->resume_ledger;
        mpv_ptr = impl_->mpv;
        sub_tok = impl_->mpv_sub_token;
        impl_->mpv_sub_token = 0;
        rc = std::move(impl_->stream_resolve_cancel);
        ec = std::move(impl_->enrich_cancel);
        sc = std::move(impl_->sub_cancel);
        impl_->caps = nullptr;
        impl_->bus = nullptr;
        impl_->jobs = nullptr;
        impl_->ui_tasks = nullptr;
        impl_->subscribers.clear();
    }
    if (rc) rc->cancel();
    if (ec) ec->cancel();
    if (sc) sc->cancel();
    if (mpv_ptr && sub_tok != 0) {
        mpv_ptr->unsubscribeState(sub_tok);
    }
    impl_->persistLedger(ledger_to_persist);
}

uint64_t PlaybackCoordinator::play(PlaybackRole role,
                                   const std::string& owner_id,
                                   PlaybackTrack track) {
    if (role == PlaybackRole::None) return 0;
    populateOptionsFromItem(track);
    if (track.item.id.empty()) {
        track.item.id = !track.options.url.empty() ? ("url:" + track.options.url) : ("title:" + track.item.title);
    }

    DeferredStreamTarget deferred = analyzeDeferredStreamTarget(track);
    const bool url_is_catalog_page = isCatalogWebPageUrl(track.options.url);
    const bool has_direct_url = !track.options.url.empty() && !url_is_catalog_page;

    uint64_t lease_id = 0;
    uint64_t gen = 0;
    PlaybackSessionState snap;
    MPVCore* mpv_ptr = nullptr;
    nlohmann::json ledger_to_persist;
    bool need_persist = false;
    bool should_resolve_async = false;
    std::shared_ptr<CancelToken> prev_resolve_cancel;

    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        prev_resolve_cancel = std::move(impl_->stream_resolve_cancel);

        // Save resume position of previous item before switching
        if (impl_->session.active_role != PlaybackRole::None &&
            !impl_->session.active_track.item.id.empty() &&
            impl_->session.active_track.item.id != track.item.id) {
            impl_->saveResumePointLocked(impl_->session.active_track.item.id,
                                         impl_->session.active_track.item.title,
                                         impl_->session.mpv.position_sec,
                                         impl_->session.mpv.duration_sec,
                                         true);
            ledger_to_persist = impl_->resume_ledger;
            need_persist = true;
        }

        // Suspend active BackgroundMusic when ForegroundVideo takes stage
        if (role == PlaybackRole::ForegroundVideo &&
            impl_->session.active_role == PlaybackRole::BackgroundMusic &&
            (!impl_->session.mpv.stopped || impl_->stream_resolving)) {
            impl_->session.has_suspended_music = true;
            impl_->session.suspended_music_track = impl_->session.active_track;
            impl_->session.suspended_music_queue = impl_->session.music_queue;
            impl_->session.suspended_music_index = impl_->session.music_queue_index;
            impl_->session.suspended_music_position_sec = impl_->session.mpv.position_sec;
        } else if (role == PlaybackRole::BackgroundMusic) {
            impl_->session.has_suspended_music = false;
        }

        if (role == PlaybackRole::ForegroundVideo &&
            !track.restart_from_beginning && track.options.start_seconds <= 0.0) {
            const double saved = impl_->lookupResumePointLocked(track.item.id);
            if (saved >= 3.0) {
                track.options.start_seconds = saved;
            }
        }

        should_resolve_async = !has_direct_url && deferred.needed &&
                               impl_->caps != nullptr && impl_->caps->has("media.source.v1");

        lease_id = impl_->next_lease_id++;
        gen = ++impl_->generation;
        impl_->user_stopped = false;
        impl_->stream_resolving = should_resolve_async;
        impl_->was_stopped = !has_direct_url;
        impl_->last_auto_skipped_sponsor_key.clear();
        impl_->session.active_role = role;
        impl_->session.active_owner = owner_id;
        impl_->session.lease_id = lease_id;
        impl_->session.active_track = track;
        impl_->session.subtitles_attached = !track.options.subtitle_urls.empty();
        impl_->session.subtitle_status.clear();
        impl_->session.active_sponsor.reset();
        mpv_ptr = impl_->mpv;
        snap = impl_->session;
    }

    if (prev_resolve_cancel) {
        prev_resolve_cancel->cancel();
    }
    if (need_persist) {
        impl_->persistLedger(ledger_to_persist);
    }
    if (mpv_ptr) {
        mpv_ptr->setChapters(track.chapters);
        if (has_direct_url) {
            mpv_ptr->load(track.options);
            snap.mpv = mpv_ptr->state();
        } else {
            mpv_ptr->pause();
            snap.mpv = mpv_ptr->state();
        }
    }

    impl_->notifyListeners(snap, true, "playback.track_changed");
    if (should_resolve_async) {
        impl_->startStreamResolution(gen, track, std::move(deferred));
    }
    impl_->startEnrichment(gen, track);
    return lease_id;
}

uint64_t PlaybackCoordinator::playMusicQueue(const std::string& owner_id,
                                             std::vector<PlaybackTrack> queue,
                                             int start_index) {
    if (queue.empty()) return 0;
    const int idx = std::clamp(start_index, 0, static_cast<int>(queue.size()) - 1);
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->session.music_queue = queue;
        impl_->session.music_queue_index = idx;
    }
    return play(PlaybackRole::BackgroundMusic, owner_id, queue[static_cast<size_t>(idx)]);
}

bool PlaybackCoordinator::releaseLease(uint64_t lease_id, bool resume_suspended) {
    bool matched = false;
    bool can_resume = false;
    MPVCore* mpv_ptr = nullptr;
    PlaybackSessionState snap;
    nlohmann::json ledger_to_persist;
    std::shared_ptr<CancelToken> rc;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (lease_id == 0 || impl_->session.lease_id != lease_id) {
            return false;
        }
        matched = true;
        rc = std::move(impl_->stream_resolve_cancel);
        impl_->stream_resolving = false;
        if (!impl_->session.active_track.item.id.empty()) {
            impl_->saveResumePointLocked(impl_->session.active_track.item.id,
                                         impl_->session.active_track.item.title,
                                         impl_->session.mpv.position_sec,
                                         impl_->session.mpv.duration_sec,
                                         true);
            ledger_to_persist = impl_->resume_ledger;
        }
        impl_->user_stopped = true;
        impl_->session.active_role = PlaybackRole::None;
        impl_->session.active_owner.clear();
        impl_->session.lease_id = 0;
        impl_->session.active_sponsor.reset();
        can_resume = resume_suspended && impl_->session.has_suspended_music;
        mpv_ptr = impl_->mpv;
        snap = impl_->session;
    }
    if (rc) rc->cancel();
    if (!matched) return false;
    impl_->persistLedger(ledger_to_persist);
    if (can_resume) {
        return resumeSuspendedMusic();
    }
    if (mpv_ptr) mpv_ptr->stop();
    impl_->notifyListeners(snap, true, "playback.state_changed");
    return true;
}

bool PlaybackCoordinator::resumeSuspendedMusic() {
    PlaybackTrack track;
    std::vector<PlaybackTrack> queue;
    int idx = -1;
    double pos = 0.0;
    bool can_re_resolve = false;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (!impl_->session.has_suspended_music) {
            return false;
        }
        track = impl_->session.suspended_music_track;
        queue = impl_->session.suspended_music_queue;
        idx = impl_->session.suspended_music_index;
        pos = impl_->session.suspended_music_position_sec;
        can_re_resolve = impl_->caps != nullptr && impl_->caps->has("media.source.v1");
        impl_->session.has_suspended_music = false;
        impl_->session.music_queue = std::move(queue);
        impl_->session.music_queue_index = idx;
    }
    if (can_re_resolve && track.options.url.find("/api/v1/playback/") != std::string::npos) {
        track.options.url = isCatalogWebPageUrl(track.item.play_url) ? track.item.play_url : std::string{};
    }
    if (pos > 0.0) {
        track.options.start_seconds = pos;
    }
    return play(PlaybackRole::BackgroundMusic, "music.resumed", std::move(track)) != 0;
}

void PlaybackCoordinator::play() {
    MPVCore* m = nullptr;
    PlaybackSessionState snap;
    bool retry_resolve = false;
    uint64_t gen = 0;
    PlaybackTrack retry_track;
    DeferredStreamTarget retry_target;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->user_stopped = false;
        m = impl_->mpv;
        if (impl_->session.active_role != PlaybackRole::None &&
            !impl_->stream_resolving &&
            (impl_->session.active_track.options.url.empty() ||
             isCatalogWebPageUrl(impl_->session.active_track.options.url))) {
            retry_target = analyzeDeferredStreamTarget(impl_->session.active_track);
            if (retry_target.needed && impl_->caps != nullptr && impl_->caps->has("media.source.v1")) {
                impl_->stream_resolving = true;
                gen = impl_->generation;
                retry_track = impl_->session.active_track;
                retry_resolve = true;
            }
        }
    }
    if (retry_resolve) {
        impl_->startStreamResolution(gen, std::move(retry_track), std::move(retry_target));
        return;
    }
    if (m) m->play();
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (m) impl_->session.mpv = m->state();
        snap = impl_->session;
    }
    impl_->notifyListeners(snap, true, "playback.state_changed");
}

void PlaybackCoordinator::pause() {
    MPVCore* m = nullptr;
    PlaybackSessionState snap;
    nlohmann::json ledger;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        m = impl_->mpv;
        if (impl_->session.active_role != PlaybackRole::None &&
            !impl_->session.active_track.item.id.empty()) {
            impl_->saveResumePointLocked(impl_->session.active_track.item.id,
                                         impl_->session.active_track.item.title,
                                         impl_->session.mpv.position_sec,
                                         impl_->session.mpv.duration_sec,
                                         true);
            ledger = impl_->resume_ledger;
        }
    }
    impl_->persistLedger(ledger);
    if (m) m->pause();
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (m) impl_->session.mpv = m->state();
        snap = impl_->session;
    }
    impl_->notifyListeners(snap, true, "playback.state_changed");
}

void PlaybackCoordinator::playPause() {
    bool is_paused = true;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        is_paused = impl_->session.mpv.paused || impl_->session.mpv.stopped;
    }
    if (is_paused) play();
    else pause();
}

void PlaybackCoordinator::stop(bool resume_suspended) {
    uint64_t cur_lease = 0;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        cur_lease = impl_->session.lease_id;
        impl_->user_stopped = true;
    }
    if (cur_lease != 0) {
        releaseLease(cur_lease, resume_suspended);
        return;
    }
    MPVCore* m = nullptr;
    PlaybackSessionState snap;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        m = impl_->mpv;
        impl_->session.active_role = PlaybackRole::None;
        impl_->session.active_owner.clear();
    }
    if (m) m->stop();
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (m) impl_->session.mpv = m->state();
        snap = impl_->session;
    }
    impl_->notifyListeners(snap, true, "playback.state_changed");
}

void PlaybackCoordinator::nextTrack() {
    PlaybackTrack next;
    std::string owner;
    bool has_next = false;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (impl_->session.music_queue.empty()) return;
        const int n = static_cast<int>(impl_->session.music_queue.size());
        int idx = impl_->session.music_queue_index;
        if (impl_->session.shuffle && n > 1) {
            static thread_local std::mt19937 rng{std::random_device{}()};
            std::uniform_int_distribution<int> dist(0, n - 1);
            int cand = dist(rng);
            if (cand == idx) cand = (idx + 1) % n;
            idx = cand;
        } else {
            idx = idx + 1;
            if (idx >= n) {
                if (!impl_->session.repeat_queue) return;
                idx = 0;
            }
        }
        impl_->session.music_queue_index = idx;
        next = impl_->session.music_queue[static_cast<size_t>(idx)];
        next.restart_from_beginning = true;
        owner = impl_->session.active_owner.empty() ? "music.queue" : impl_->session.active_owner;
        has_next = true;
    }
    if (has_next) {
        play(PlaybackRole::BackgroundMusic, owner, std::move(next));
    }
}

void PlaybackCoordinator::prevTrack() {
    PlaybackTrack prev;
    std::string owner;
    bool has_prev = false;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (impl_->session.music_queue.empty()) return;
        const int n = static_cast<int>(impl_->session.music_queue.size());
        int idx = impl_->session.music_queue_index - 1;
        if (idx < 0) {
            idx = impl_->session.repeat_queue ? (n - 1) : 0;
        }
        impl_->session.music_queue_index = idx;
        prev = impl_->session.music_queue[static_cast<size_t>(idx)];
        prev.restart_from_beginning = true;
        owner = impl_->session.active_owner.empty() ? "music.queue" : impl_->session.active_owner;
        has_prev = true;
    }
    if (has_prev) {
        play(PlaybackRole::BackgroundMusic, owner, std::move(prev));
    }
}

void PlaybackCoordinator::seekAbsolute(double seconds) {
    MPVCore* m = nullptr;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        m = impl_->mpv;
    }
    if (m) m->seekAbsolute(seconds);
}

void PlaybackCoordinator::seekRelative(double delta_seconds) {
    MPVCore* m = nullptr;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        m = impl_->mpv;
    }
    if (m) m->seekRelative(delta_seconds);
}

void PlaybackCoordinator::setVolume(int volume) {
    MPVCore* m = nullptr;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        m = impl_->mpv;
    }
    if (m) m->setVolume(volume);
}

void PlaybackCoordinator::setSpeed(double speed) {
    MPVCore* m = nullptr;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        m = impl_->mpv;
    }
    if (m) m->setSpeed(speed);
}

void PlaybackCoordinator::setRepeatQueue(bool repeat) {
    PlaybackSessionState snap;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->session.repeat_queue = repeat;
        snap = impl_->session;
    }
    impl_->notifyListeners(snap, false);
}

void PlaybackCoordinator::setShuffle(bool shuffle) {
    PlaybackSessionState snap;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->session.shuffle = shuffle;
        snap = impl_->session;
    }
    impl_->notifyListeners(snap, false);
}

void PlaybackCoordinator::setAutoSkipSponsor(bool auto_skip) {
    PlaybackSessionState snap;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->session.auto_skip_sponsor = auto_skip;
        snap = impl_->session;
    }
    impl_->notifyListeners(snap, false);
}

bool PlaybackCoordinator::skipActiveSponsor() {
    MPVCore* m = nullptr;
    std::optional<ChapterSegment> hit;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        m = impl_->mpv;
        hit = impl_->session.active_sponsor;
    }
    if (m && m->skipActiveAdSegment()) return true;
    if (hit && hit->end_sec > hit->start_sec) {
        seekAbsolute(hit->end_sec + 0.05);
        return true;
    }
    return false;
}

void PlaybackCoordinator::attachSubtitlesAsync(bool manual_trigger) {
    CapabilityRegistry* caps_ptr = nullptr;
    UiTaskRunner* runner = nullptr;
    PlaybackTrack track;
    uint64_t gen = 0;
    std::shared_ptr<CancelToken> cancel;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        caps_ptr = impl_->caps;
        runner = impl_->ui_tasks;
        track = impl_->session.active_track;
        gen = impl_->generation;
        if (!caps_ptr || !caps_ptr->has("subtitle.fetch.v1") || track.item.id.empty()) {
            return;
        }
        if (impl_->sub_cancel) impl_->sub_cancel->cancel();
        cancel = CancelToken::create();
        impl_->sub_cancel = cancel;
        if (manual_trigger) {
            impl_->session.subtitle_status = "正在匹配 SubHunter 字幕...";
        }
    }
    impl_->notifyListeners(state(), false);

    auto work = [this, caps_ptr, gen, track](const std::shared_ptr<CancelToken>& c) {
        const std::string local_path = resolveLocalPath(track.item, track.options);
        nlohmann::json sreq = {{"action", "search"}, {"provider", "all"}};
        if (!local_path.empty()) sreq["filename"] = local_path;
        else sreq["query"] = track.item.title;
        auto sres = caps_ptr->callCancellable("subtitle.fetch.v1", sreq, c);
        std::string status_msg = "未找到匹配字幕";
        std::string sub_path;
        if (sres.ok && sres.data.is_object() &&
            sres.data.contains("candidates") && sres.data["candidates"].is_array() &&
            !sres.data["candidates"].empty() && !(c && c->isCancelled())) {
            const auto& cand = sres.data["candidates"][0];
            if (cand.is_object()) {
                auto dres = caps_ptr->callCancellable(
                    "subtitle.fetch.v1",
                    {
                        {"action", "download"},
                        {"provider", cand.value("provider", std::string{})},
                        {"id", cand.value("id", std::string{})},
                    },
                    c);
                if (dres.ok && dres.data.contains("files") &&
                    dres.data["files"].is_array() && !dres.data["files"].empty() &&
                    !(c && c->isCancelled())) {
                    sub_path = dres.data["files"][0].value("path", std::string{});
                    if (!local_path.empty() && !sub_path.empty() &&
                        caps_ptr->has("subtitle.sync.v1")) {
                        auto sync_res = caps_ptr->callCancellable(
                            "subtitle.sync.v1",
                            {
                                {"action", "sync"},
                                {"subtitle_path", sub_path},
                                {"reference_path", local_path},
                                {"mode", "auto"},
                            },
                            c);
                        if (sync_res.ok && !sync_res.data.value("output_path", std::string{}).empty()) {
                            sub_path = sync_res.data["output_path"].get<std::string>();
                        }
                    }
                    if (!sub_path.empty()) {
                        status_msg = "已挂载字幕: " + fs::path(sub_path).filename().string();
                    }
                }
            }
        } else if (!sres.ok && !sres.error.empty()) {
            status_msg = "字幕检索失败: " + sres.error;
        }

        PlaybackSessionState snap;
        MPVCore* mpv_ptr = nullptr;
        bool current = false;
        {
            std::lock_guard<std::mutex> lk(impl_->mutex);
            if (impl_->generation == gen && impl_->alive->load(std::memory_order_acquire)) {
                impl_->session.subtitle_status = status_msg;
                if (!sub_path.empty()) {
                    impl_->session.active_track.options.subtitle_urls.push_back(sub_path);
                    impl_->session.subtitles_attached = true;
                }
                snap = impl_->session;
                mpv_ptr = impl_->mpv;
                current = true;
            }
        }
        if (current) {
            if (!sub_path.empty() && mpv_ptr) {
                mpv_ptr->addSubtitleFile(sub_path);
            }
            impl_->notifyListeners(snap, !sub_path.empty(), "playback.subtitles_attached");
        }
    };
    if (runner) {
        runner->submitInLane("playback.subtitle", std::move(work), cancel);
    } else {
        std::thread([work = std::move(work), cancel]() { work(cancel); }).detach();
    }
}

std::string PlaybackCoordinator::triggerAsrHotMount() {
    CapabilityRegistry* caps_ptr = nullptr;
    JobCenter* jobs_ptr = nullptr;
    PlaybackTrack track;
    uint64_t gen = 0;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        caps_ptr = impl_->caps;
        jobs_ptr = impl_->jobs;
        track = impl_->session.active_track;
        gen = impl_->generation;
    }
    if (!caps_ptr || !jobs_ptr || !caps_ptr->has("speech.asr.v1")) return "";
    const std::string local_path = resolveLocalPath(track.item, track.options);
    if (local_path.empty()) return "";

    JobSpec spec;
    spec.title = "FunASR 热挂载字幕 · " + (track.item.title.empty() ? "本地媒体" : track.item.title);
    spec.plugin_id = "plugin-asr-funasr";
    spec.kind = "asr";
    spec.open_target = JobTarget{
        "media",
        "entity.media.open",
        track.item.id,
        "查看媒体",
        {{"id", track.item.id}, {"title", track.item.title}},
    };

    const std::string job_id = jobs_ptr->submit(
        spec,
        [this, caps_ptr, gen, local_path](JobHandle& job, std::string& error) -> bool {
            std::vector<LyricLine> live_lyrics;
            const nlohmann::json req = {
                {"action", "transcribe_media"},
                {"media_path", local_path},
                {"job_id", job.jobId()},
            };
            CapabilityResult res = caps_ptr->callStream(
                "speech.asr.v1",
                req,
                [&](const StreamChunk& ch) {
                    if (ch.done) return;
                    if (ch.event == "resumed" || ch.event == "chunk") {
                        const float prog = static_cast<float>(ch.data.value("progress", 0.0));
                        const int done_n = ch.data.value("completed_chunks", 0);
                        const int total_n = ch.data.value("total_chunks", 0);
                        job.reportProgress(prog,
                                           (ch.event == "resumed" ? "恢复断点 " : "转写分块 ") +
                                               std::to_string(done_n) + "/" + std::to_string(total_n));
                        if (ch.data.contains("cues") && ch.data["cues"].is_array()) {
                            appendCuesToLyrics(ch.data["cues"], live_lyrics);
                            PlaybackSessionState snap;
                            bool current = false;
                            {
                                std::lock_guard<std::mutex> lk(impl_->mutex);
                                if (impl_->generation == gen &&
                                    impl_->alive->load(std::memory_order_acquire)) {
                                    impl_->session.active_track.lyrics = live_lyrics;
                                    impl_->session.subtitles_attached = !live_lyrics.empty();
                                    impl_->session.subtitle_status =
                                        "FunASR 字幕热挂载中 (" + std::to_string(live_lyrics.size()) + " 条)";
                                    snap = impl_->session;
                                    current = true;
                                }
                            }
                            if (current) {
                                impl_->notifyListeners(snap, false);
                            }
                        }
                    } else if (ch.event == "stage") {
                        job.reportProgress(static_cast<float>(ch.data.value("progress", 0.0)),
                                           ch.data.value("message", std::string{}));
                    }
                },
                job.cancelToken());
            if (!res.ok) {
                error = res.error.empty() ? "FunASR 字幕生成失败" : res.error;
                return false;
            }
            std::vector<LyricLine> final_lyrics = std::move(live_lyrics);
            if (res.data.contains("cues") && res.data["cues"].is_array()) {
                final_lyrics.clear();
                appendCuesToLyrics(res.data["cues"], final_lyrics);
            }
            const std::string srt_path = res.data.value("output_srt_path", std::string{});
            PlaybackSessionState snap;
            MPVCore* mpv_ptr = nullptr;
            bool current = false;
            {
                std::lock_guard<std::mutex> lk(impl_->mutex);
                if (impl_->generation == gen && impl_->alive->load(std::memory_order_acquire)) {
                    impl_->session.active_track.lyrics = final_lyrics;
                    impl_->session.subtitles_attached = true;
                    impl_->session.subtitle_status =
                        "FunASR 字幕已挂载 (" + std::to_string(final_lyrics.size()) + " 条)";
                    if (!srt_path.empty()) {
                        impl_->session.active_track.options.subtitle_urls.push_back(srt_path);
                    }
                    snap = impl_->session;
                    mpv_ptr = impl_->mpv;
                    current = true;
                }
            }
            if (current) {
                if (!srt_path.empty() && mpv_ptr) {
                    mpv_ptr->addSubtitleFile(srt_path);
                }
                impl_->notifyListeners(snap, true, "playback.subtitles_attached");
            }
            job.reportProgress(1.0f, "FunASR 字幕热挂载完成");
            return true;
        });
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->session.asr_job_id = job_id;
    }
    return job_id;
}

double PlaybackCoordinator::getResumePosition(const std::string& item_id) const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    return impl_->lookupResumePointLocked(item_id);
}

void PlaybackCoordinator::saveResumePosition(const std::string& item_id,
                                             const std::string& title,
                                             double position_sec,
                                             double duration_sec) {
    nlohmann::json ledger;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->saveResumePointLocked(item_id, title, position_sec, duration_sec, true);
        ledger = impl_->resume_ledger;
    }
    impl_->persistLedger(ledger);
}

void PlaybackCoordinator::clearResumePosition(const std::string& item_id) {
    nlohmann::json ledger;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->resume_ledger.erase(item_id);
        ledger = impl_->resume_ledger;
    }
    impl_->persistLedger(ledger);
}

PlaybackSessionState PlaybackCoordinator::state() const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    return impl_->session;
}

uint64_t PlaybackCoordinator::subscribe(StateCallback cb) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    const uint64_t tok = impl_->next_sub_token++;
    impl_->subscribers[tok] = std::move(cb);
    return tok;
}

void PlaybackCoordinator::unsubscribe(uint64_t token) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    impl_->subscribers.erase(token);
}

} // namespace omnidesk
