#include "music/plugin.hpp"
#include "music/ui_views.hpp"

#include <algorithm>
#include <cctype>
#include <omnidesk/domain_registry.hpp>

namespace music_plugin {
namespace {

bool isDigitsOnly(const std::string& s) {
    if (s.empty()) return false;
    return std::all_of(s.begin(), s.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
}

std::string safeEventStr(const nlohmann::json& j, const char* key, const std::string& def = "") {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    return def;
}

std::string extractStorefrontFromUrl(const std::string& url) {
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

std::string extractAppleMusicIdFromUrl(const std::string& url) {
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
                std::string candidate = kv.substr(2);
                if (isDigitsOnly(candidate)) return candidate;
            }
            if (amp == std::string::npos) break;
            cur = amp + 1;
        }
    }
    for (const char* marker : {"/song/", "/music-video/"}) {
        size_t song_pos = url.find(marker);
        if (song_pos != std::string::npos) {
            size_t p = song_pos + std::char_traits<char>::length(marker);
            // Handle either /song/12345 or /song/slug/12345
            size_t q = url.find_first_of("?#", p);
            std::string tail = (q == std::string::npos) ? url.substr(p) : url.substr(p, q - p);
            while (!tail.empty() && tail.back() == '/') tail.pop_back();
            size_t slash = tail.rfind('/');
            std::string candidate = (slash == std::string::npos) ? tail : tail.substr(slash + 1);
            if (isDigitsOnly(candidate)) return candidate;
        }
    }
    return "";
}

std::string extractNumericSongIdFromPrefixedId(const std::string& raw_id, std::string* sf_out = nullptr) {
    if (raw_id.rfind("am-", 0) == 0 && raw_id.size() > 6 && raw_id[5] == '-' &&
        std::isalpha(static_cast<unsigned char>(raw_id[3])) &&
        std::isalpha(static_cast<unsigned char>(raw_id[4]))) {
        if (sf_out && sf_out->empty()) {
            std::string sf = raw_id.substr(3, 2);
            sf[0] = static_cast<char>(std::tolower(static_cast<unsigned char>(sf[0])));
            sf[1] = static_cast<char>(std::tolower(static_cast<unsigned char>(sf[1])));
            *sf_out = sf;
        }
        std::string rest = raw_id.substr(6);
        if (isDigitsOnly(rest)) return rest;
        size_t dash = rest.rfind('-');
        if (dash != std::string::npos && isDigitsOnly(rest.substr(dash + 1))) {
            return rest.substr(dash + 1);
        }
    }
    if (raw_id.rfind("ix-am-", 0) == 0) {
        std::string rest = raw_id.substr(6);
        if (isDigitsOnly(rest)) return rest;
        size_t dash = rest.rfind('-');
        if (dash != std::string::npos && isDigitsOnly(rest.substr(dash + 1))) {
            return rest.substr(dash + 1);
        }
    }
    return "";
}

class AppleMusicMediaSource final : public omnidesk::IMediaSource {
public:
    explicit AppleMusicMediaSource(std::shared_ptr<MusicEngine> engine)
        : engine_(std::move(engine)) {}

    std::string sourceId() const override { return "plugin-music"; }
    std::string displayName() const override { return "Apple Music (FairPlay ALAC)"; }

    std::vector<std::string> categories() const override {
        return {"music", "recommendations", "双语歌词"};
    }

    std::vector<omnidesk::MediaItem> fetchItems(const std::string& category,
                                                const std::string& query = "") override {
        if (!engine_ || !engine_->isAlive()) return {};
        return engine_->fetchSdkMediaItems(category, query);
    }

    std::vector<omnidesk::LyricLine> fetchLyrics(const std::string& item_id) override {
        if (!engine_ || !engine_->isAlive()) return {};
        return engine_->fetchSdkLyrics(item_id);
    }

private:
    std::shared_ptr<MusicEngine> engine_;
};

} // namespace

bool MusicPlugin::onLoad(omnidesk::PluginContext& ctx) {
    ctx_ = &ctx;
    engine_ = std::make_shared<MusicEngine>(ctx);
    engine_->initializeFast();

    ctx.registerCapability(
        "media.source.v1",
        [eng = engine_](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            return eng->handleMediaSourceRpc(req);
        },
        {
            {"name", "Apple Music (Adam FairPlay ALAC)"},
            {"version", "0.1.0"},
            {"actions", {"categories", "feed", "search", "metadata", "playurl", "lyrics", "drm_status"}},
        },
        95);

    ctx.registerCapability(
        "media.lyrics.v1",
        [eng = engine_](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            return eng->handleLyricsRpc(req);
        },
        {
            {"name", "Apple Music & LRCLIB Bilingual Lyrics"},
            {"version", "0.1.0"},
            {"actions", {"status", "probe", "get", "fetch", "parse", "compose", "export", "import", "translate", "explain", "analyze_line"}},
        },
        100);

    ctx.registerStreamingCapability(
        "media.playlist.v1",
        [eng = engine_](const nlohmann::json& req,
                       const std::shared_ptr<omnidesk::CancelToken>& cancel,
                       const omnidesk::StreamCallback&) -> omnidesk::CapabilityResult {
            return eng->handlePlaylistRpc(req, cancel);
        },
        {
            {"name", "Apple Music Playlist Sync & Guardrail"},
            {"version", "0.1.0"},
            {"actions", {"preflight_cn_catalog", "sync_daily_authorized", "write_interactive_playlist", "enqueue_playback", "status", "list_playlists"}},
        },
        100);

    ctx.registerMediaSource(std::make_shared<AppleMusicMediaSource>(engine_));

    omnidesk::WorkspaceTabSpec tab;
    tab.tab_id = "apple_music.workspace";
    tab.plugin_id = ctx.pluginId();
    tab.title = "Apple Music";
    tab.domain = omnidesk::ContentDomain::Media;
    tab.badge = "ALAC";
    tab.create_view = [weak = std::weak_ptr<MusicEngine>(engine_)]() -> brls::View* {
        auto eng = weak.lock();
        if (!eng || !eng->isAlive()) return nullptr;
        return new MusicWorkspaceView(eng);
    };
    ctx.registerWorkspaceTab(std::move(tab));

    omnidesk::DockWidgetSpec dock;
    dock.widget_id = "apple_music.companion";
    dock.plugin_id = ctx.pluginId();
    dock.title = "Apple Music 双语精讲";
    dock.supported_domains = {omnidesk::ContentDomain::Media, omnidesk::ContentDomain::Core};
    dock.create_view = [weak = std::weak_ptr<MusicEngine>(engine_)]() -> brls::View* {
        auto eng = weak.lock();
        if (!eng || !eng->isAlive()) return nullptr;
        return new MusicCompanionDockView(eng);
    };
    ctx.registerDockWidget(std::move(dock));

    ctx.subscribe("entity.media.open", [weak = std::weak_ptr<MusicEngine>(engine_)](
                                           const std::string& /*topic*/,
                                           const nlohmann::json& payload) {
        auto eng = weak.lock();
        if (!eng || !eng->isAlive() || !payload.is_object()) return;
        const std::string origin = safeEventStr(payload, "origin");
        if (origin == "plugin_music") return;

        const std::string source_id = safeEventStr(payload, "source_id");
        std::string raw_id = safeEventStr(payload, "id");
        if (raw_id.empty()) raw_id = safeEventStr(payload, "item_id");
        const std::string kind = safeEventStr(payload, "kind");
        if (raw_id.rfind("cache:", 0) == 0 || raw_id.rfind("demo-", 0) == 0) return;

        std::string extra_song_id = safeEventStr(payload, "apple_music_song_id");
        std::string storefront;
        if (payload.contains("extra") && payload["extra"].is_object()) {
            const auto& extra = payload["extra"];
            if (extra_song_id.empty()) {
                extra_song_id = safeEventStr(extra, "apple_music_song_id");
            }
            storefront = safeEventStr(extra, "storefront");
        }
        std::string pref_sf;
        std::string prefixed_song_id = extractNumericSongIdFromPrefixedId(raw_id, &pref_sf);
        std::string url_str = safeEventStr(payload, "play_url");
        if (url_str.empty()) url_str = safeEventStr(payload, "url");
        std::string url_song_id = extractAppleMusicIdFromUrl(url_str);
        std::string url_sf = extractStorefrontFromUrl(url_str);

        const bool is_curated_am = !extra_song_id.empty() || !prefixed_song_id.empty() || !url_song_id.empty();
        if (source_id != "plugin-music" && source_id != "plugin-ai-curator" && !is_curated_am) {
            return;
        }
        if (source_id == "plugin-ai-curator" && !is_curated_am &&
            (kind == "video" || kind == "youtube" || kind == "nas_clip")) {
            return;
        }

        std::string resolved_id;
        if (isDigitsOnly(extra_song_id)) {
            resolved_id = extra_song_id;
        } else if (!prefixed_song_id.empty()) {
            resolved_id = prefixed_song_id;
        } else if (!url_song_id.empty()) {
            resolved_id = url_song_id;
        } else if (source_id == "plugin-music") {
            resolved_id = raw_id;
        } else if (source_id == "plugin-ai-curator" && kind == "music" && isDigitsOnly(raw_id)) {
            resolved_id = raw_id;
        }
        if (resolved_id.empty()) return;

        MusicTrack tr;
        tr.id = resolved_id;
        tr.title = safeEventStr(payload, "title", resolved_id);
        tr.artist = safeEventStr(payload, "artist");
        if (tr.artist.empty()) tr.artist = safeEventStr(payload, "creator");
        tr.cover_url = safeEventStr(payload, "cover_url");
        if (!storefront.empty()) {
            tr.storefront = storefront;
        } else if (!url_sf.empty()) {
            tr.storefront = url_sf;
        } else if (!pref_sf.empty()) {
            tr.storefront = pref_sf;
        } else if (source_id == "plugin-ai-curator") {
            tr.storefront = "cn";
        }
        if (origin == "host_workbench") {
            eng->syncExternalTrack(tr);
        } else {
            eng->playTrack(tr);
        }
    });

    engine_->startBackgroundTasks();
    ctx.logInfo("Loaded plugin-music (Apple Music Adam FairPlay ALAC + Bilingual Lyrics)");
    return true;
}

void MusicPlugin::onUnload() {
    if (engine_) {
        engine_->shutdown();
    }
    if (ctx_) {
        const std::string pid = ctx_->pluginId();
        ctx_->logInfo("Unloading plugin-music");
        ctx_->bus().unsubscribeOwner(pid);
        ctx_->domains().unregisterPlugin(pid);
        ctx_->capabilities().unregisterPlugin(pid);
        if (ctx_->workers()) {
            ctx_->workers()->stopByPlugin(pid);
        }
    }
    engine_.reset();
    ctx_ = nullptr;
}

nlohmann::json MusicPlugin::onHealthCheck() const {
    if (!engine_) {
        return {{"ok", false}, {"plugin_id", "plugin-music"}, {"loaded", false}};
    }
    EngineStatusSnapshot st = engine_->statusSnapshot();
    return {
        {"ok", true},
        {"plugin_id", "plugin-music"},
        {"engine_reachable", st.engine_reachable},
        {"engine_managed_by_plugin", st.engine_managed_by_plugin},
        {"auth_state", st.drm.auth_state},
        {"fairplay_state", st.drm.fairplay_state},
        {"cap_alac", st.drm.cap_alac},
        {"cap_hi_res", st.drm.cap_hi_res},
        {"has_dev_token", st.has_dev_token},
        {"has_mut", st.has_mut},
        {"llm_translate_available", st.llm_translate_available},
        {"llm_chat_available", st.llm_chat_available},
        {"cached_lyrics_count", st.cached_lyrics_count},
        {"active_track_id", st.active_track_id},
        {"active_track_title", st.active_track_title},
        {"active_quality", st.active_quality},
    };
}

brls::View* MusicPlugin::createSettingsView(omnidesk::PluginContext& /*ctx*/) {
    return nullptr;
}

} // namespace music_plugin
