#pragma once

#include <cstdint>
#include <nlohmann/json.hpp>
#include <omnidesk/types.hpp>
#include <string>
#include <vector>

namespace music_plugin {

struct MusicConfig {
    std::string engine_bin;
    std::string engine_data_dir;
    bool engine_data_dir_explicit = false;
    int engine_port = 20025;
    bool auto_launch_engine = true;
    std::string storefront = "us";
    bool storefront_explicit = false;
    std::string language = "zh-CN";
    bool lossless_enabled = true;
    bool hires_enabled = true;
    bool enable_lrclib = true;
    bool auto_translate_lyrics = true;
    std::string target_language = "zh-CN";
    std::string lyrics_display_mode = "bilingual"; // "bilingual" | "original" | "translation"
    int translation_batch_size = 24;
    std::string media_user_token;
    std::string proxy;
    std::string data_dir;
};

struct DrmStatus {
    bool engine_reachable = false;
    bool available = false;
    std::string backend;
    std::string manager_state = "unknown";
    std::string process_state = "unknown";
    std::string auth_state = "unknown";      // "logged_out" | "logging_in" | "challenging" | "logged_in" | "failed"
    std::string fairplay_state = "unknown";  // "initializing" | "ready" | "failed"
    std::string session_state = "unknown";   // "empty" | "valid" | "expired"
    bool cap_cbcs = false;
    bool cap_alac = false;
    bool cap_atmos = false;
    bool cap_hi_res = false;
    std::string challenge_prompt;
    std::string message;
    std::string error;
};

struct MusicTrack {
    std::string id;            // catalog ID (preferred) or library ID
    std::string library_id;    // optional l. / i. library ID
    std::string kind = "song"; // "song" | "mv"
    std::string title;
    std::string artist;
    std::string album;
    std::string album_id;
    std::string cover_url;
    std::string storefront;
    std::string quality_label; // e.g. "ALAC 24bit/192kHz"
    double duration_sec = 0.0;
    bool has_lyrics = false;
    bool is_hires = false;
    bool is_lossless = false;
    bool is_atmos = false;
    std::string embedded_lyrics;
};

struct ContainerItem {
    std::string id;
    std::string type;          // "albums" | "playlists" | "library-playlists" | "artists"
    std::string name;
    std::string subtitle;
    std::string cover_url;
    std::string storefront;
    int track_count = 0;
};

struct PlaybackSessionInfo {
    bool ok = false;
    std::string session_id;
    std::string asset_id;
    std::string storefront;
    std::string type = "song"; // "song" | "mv"
    std::string codec;
    int sample_rate = 0;
    int bit_depth = 0;
    int bit_rate = 0;
    int channel_count = 0;
    std::string spatial_audio;
    std::string audio_url;
    std::string video_url;
    std::string title;
    std::string artist;
    std::string album;
    std::string cover_url;
    double duration_sec = 0.0;
    std::string error;

    std::string formattedQuality() const;
};

struct LyricLineEx {
    std::string id;
    double time_sec = -1.0;
    bool has_time = false;
    std::string original;
    std::string translation;
};

struct LyricBundle {
    std::string signature;
    MusicTrack track;
    std::vector<LyricLineEx> lines;
    bool synced = false;
    bool instrumental = false;
    bool translated = false;
    bool cache_hit = false;
    std::string source;             // "adam" | "lrclib" | "import" | "cache"
    std::string translation_status; // "loading" | "translating" | "instrumental" | "unavailable" | "existing" | "original_chinese" | "translated" | "disabled" | "error"
    std::string status_text;
    std::string error;
    std::string original_lrc_path;
    std::string zh_lrc_path;
    std::string bilingual_lrc_path;
};

struct PhraseExplanation {
    std::string line_id;
    std::string original;
    std::string translation;
    std::string focus_phrase;
    std::string explanation;
    bool degraded = false;
    std::string provider;
};

struct FeedOutcome {
    bool ok = false;
    std::string feed_id;
    std::vector<ContainerItem> containers;
    std::vector<MusicTrack> tracks;
    std::string error;
};

struct ExportJobInfo {
    std::string job_id;
    std::string asset_id;
    std::string phase; // "queued" | "resolving" | "downloading" | "tagging" | "moving" | "done" | "failed" | "cancelled"
    int percent = 0;
    int queue_index = 0;
    int priority = 0;
    std::string title;
    std::string artist;
    std::string artwork_url;
    int64_t bytes_done = 0;
    int64_t bytes_total = 0;
    std::string output;
    std::string error;
    std::string source; // "cache" | "playback" | "network"
};

struct CuratedTrackItem {
    MusicTrack track;
    std::string item_id;
    std::string mode; // "daily_immutable" | "interactive_ephemeral"
    std::string business_date;
    int position = 0;
    std::string canonical_id;
    std::string why_recommended;
    std::string listening_cue;
    std::string previously_recommended_on;
    std::string revisit_badge;
};

struct CuratedInteractiveSession {
    std::string session_id;
    std::string title;
    std::string user_prompt;
    std::string repeat_policy = "allow_any";
    std::vector<CuratedTrackItem> tracks;
};

struct CurationWorkspaceSnapshot {
    std::vector<std::string> available_dates; // newest-first
    std::string selected_date;
    std::vector<CuratedTrackItem> daily_tracks;
    std::vector<CuratedInteractiveSession> interactive_sessions; // newest-first
    int total_hydrated_tracks = 0;
    int total_dates = 0;

    std::string protected_playlist_id = "p.YJXV7dEIeb3GzZX";
    std::string protected_playlist_name = "ChatGPT Daily 5";
    int protected_synced_dates = 0;
    int protected_cumulative_tracks = 0;

    std::string interactive_playlist_name = "OmniDesk AI 灵感电台";
    std::string interactive_apple_playlist_id;
    int interactive_playlist_tracks = 0;
    bool interactive_cloud_synced = false;
    bool has_live_write_auth = false;
};

struct InteractiveWritebackOutcome {
    bool ok = false;
    std::string session_id;
    std::string title;
    std::string prompt;
    std::string error_code;
    std::string error;
    std::vector<CuratedTrackItem> tracks;
    int revisit_count = 0;
    bool feed_written = false;
    bool playlist_cloud_synced = false;
    bool playlist_local_staged = false;
    std::string target_playlist_name;
    std::string target_apple_playlist_id;
};

struct EngineStatusSnapshot {
    bool initialized = false;
    bool engine_reachable = false;
    bool engine_managed_by_plugin = false;
    DrmStatus drm;
    bool has_dev_token = false;
    bool has_mut = false;
    bool llm_translate_available = false;
    bool llm_chat_available = false;
    size_t library_songs = 0;
    size_t library_playlists = 0;
    size_t cached_lyrics_count = 0;
    std::string active_track_id;
    std::string active_track_title;
    std::string active_quality;
};

} // namespace music_plugin
