#pragma once

#include "music/lyrics_cache.hpp"
#include "music/types.hpp"
#include <atomic>
#include <mutex>
#include <omnidesk/async.hpp>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace music_plugin {

struct HttpResponse {
    bool ok = false;
    bool cancelled = false;
    int status = 0;
    std::string content_type;
    std::string body;
    std::string error;
};

class AdamClient {
public:
    explicit AdamClient(LyricsCacheStore* cache = nullptr);
    ~AdamClient();

    void configure(const MusicConfig& cfg);
    void setCacheStore(LyricsCacheStore* cache) { cache_ = cache; }

    // Headless Go engine process lifecycle
    bool ensureEngineRunning(std::string& error);
    void stopManagedEngine();
    bool isManagedByUs() const { return managed_pid_ > 0; }
    bool isPortReachable() const;

    // Developer JWT & Music-User-Token
    std::string getDeveloperToken(bool force_refresh = false);
    std::string cachedOrEnvDeveloperToken() const;
    void setMediaUserToken(const std::string& mut);
    std::string mediaUserToken() const;
    std::string resolveMediaUserToken() const;
    bool hasDeveloperToken() const;
    bool hasUsableAuthForWrite() const;
    static bool localWrapperAuthEnabled();
    std::string ampApiBaseUrl() const;
    std::string configuredProxy() const;
    bool pushTokensToEngine(std::string& error);
    void setDeveloperTokenForTesting(const std::string& token);
    void setAmpApiBaseUrlForTesting(const std::string& base_url);

    // Native Apple ID + 2FA DRM authentication
    DrmStatus getDrmStatus();
    bool authenticateDrm(const std::string& email, const std::string& password, DrmStatus& status_out, std::string& error);
    bool submitDrmChallenge(const std::string& code, DrmStatus& status_out, std::string& error);
    bool resendDrmChallenge(DrmStatus& status_out, std::string& error);
    bool logoutDrm(std::string& error);

    // Playback session (ALAC / Hi-Res / AAC / MV)
    PlaybackSessionInfo createPlaybackSession(const std::string& asset_id,
                                              const std::string& storefront,
                                              bool lossless,
                                              bool hires,
                                              bool video,
                                              bool atmos);
    bool deletePlaybackSession(const std::string& session_id);

    // Metadata & native lyrics
    bool fetchMetadata(const std::string& asset_id, const std::string& storefront, MusicTrack& out, std::string& error);
    bool fetchBatchMetadata(const std::vector<std::pair<std::string, std::string>>& id_and_sf,
                            std::unordered_map<std::string, MusicTrack>& out,
                            std::string& error);
    bool fetchNativeLyrics(const std::string& asset_id, const std::string& storefront, std::string& lrc_out, std::string& error);

    std::string effectiveStorefront(const std::string& override_sf = "") const;
    bool allowLocalAdamDrmFallback() const;

    // Library (local SQLite playlists & tracks)
    bool fetchLibraryStatus(nlohmann::json& out, std::string& error);
    std::vector<ContainerItem> fetchLibraryPlaylists(std::string& error);
    std::vector<MusicTrack> fetchLibraryPlaylistTracks(const std::string& playlist_id, std::string& error);
    std::vector<MusicTrack> fetchLibraryAlbumTracks(const std::string& album_id, std::string& error);
    bool syncLibraryFromApple(nlohmann::json& stats_out, std::string& error);

    // Catalog & Personalised feeds
    FeedOutcome fetchRecommendations(const std::string& kind, int limit = 20);
    bool fetchCatalogAlbum(const std::string& album_id, const std::string& sf,
                           ContainerItem& album_out, std::vector<MusicTrack>& tracks_out, std::string& error);
    bool fetchCatalogPlaylist(const std::string& playlist_id, const std::string& sf,
                              ContainerItem& pl_out, std::vector<MusicTrack>& tracks_out, std::string& error);
    bool fetchCatalogArtist(const std::string& artist_id, const std::string& sf,
                            ContainerItem& artist_out, std::vector<ContainerItem>& albums_out,
                            std::vector<MusicTrack>& top_songs_out, std::string& error);
    FeedOutcome searchCatalog(const std::string& query, const std::string& sf = "",
                              const std::string& types = "songs,albums,playlists,artists,music-videos");

    // Apple Music AMP API & Catalog Preflight / Library Playlist Write-Back
    HttpResponse ampApiGet(const std::string& path_and_query, bool require_mut = false,
                           const std::shared_ptr<omnidesk::CancelToken>& cancel = nullptr);
    HttpResponse ampApiPost(const std::string& path_and_query, const std::string& json_body, bool require_mut = true,
                            const std::shared_ptr<omnidesk::CancelToken>& cancel = nullptr);

    bool preflightCatalogSongs(const std::vector<std::string>& track_ids,
                               const std::string& storefront,
                               std::vector<MusicTrack>& verified_tracks_out,
                               std::vector<std::string>& unplayable_ids_out,
                               std::string& error,
                               bool allow_offline_catalog_fallback = false,
                               const std::string& music_daily_dir = "",
                               const std::shared_ptr<omnidesk::CancelToken>& cancel = nullptr);

    bool appendLibraryPlaylistTracks(const std::string& playlist_id,
                                      const std::vector<std::string>& song_ids,
                                      std::string& error,
                                      const std::shared_ptr<omnidesk::CancelToken>& cancel = nullptr);
    bool createLibraryPlaylist(const std::string& name,
                               const std::string& description,
                               const std::vector<std::string>& song_ids,
                               std::string& playlist_id_out,
                               std::string& error,
                               const std::shared_ptr<omnidesk::CancelToken>& cancel = nullptr);
    bool fetchLibraryPlaylistCatalogIdsFromAmp(const std::string& playlist_id,
                                               std::vector<std::string>& catalog_ids_out,
                                               std::string& error,
                                               const std::shared_ptr<omnidesk::CancelToken>& cancel = nullptr);
    bool fetchLibraryPlaylistFromAmp(const std::string& playlist_id, ContainerItem& playlist_out,
                                      std::string& error,
                                      const std::shared_ptr<omnidesk::CancelToken>& cancel = nullptr);

    // Lossless / MV Export & Cache Stats (Adam Go Engine /api/v1/export & /api/v1/cache/stats)
    bool enqueueExportJob(const std::string& asset_id,
                          const std::string& storefront,
                          bool is_video,
                          bool is_playlist,
                          bool is_library_playlist,
                          bool convert_to_flac,
                          const std::string& output_dir,
                          const std::string& hint_title,
                          const std::string& hint_artist,
                          const std::string& hint_artwork,
                          ExportJobInfo& job_out,
                          std::string& error);
    bool listExportJobs(std::vector<ExportJobInfo>& jobs_out, std::string& error);
    bool cancelExportJob(const std::string& job_id, std::string& error);
    bool retryExportJob(const std::string& job_id, ExportJobInfo& job_out, std::string& error);
    bool fetchCacheStats(nlohmann::json& out, std::string& error);

    // Helpers exposed for testing
    static bool isLoopbackPortOwnedByUid(uint16_t port, uint32_t expected_uid);
    static std::string decodeChunkedBody(const std::string& raw);
    static void parseResourceNode(const nlohmann::json& node,
                                  const std::string& default_sf,
                                  std::vector<ContainerItem>& containers,
                                  std::vector<MusicTrack>& tracks);

private:
    HttpResponse engineRequest(const std::string& method,
                               const std::string& path,
                               const std::string& body = "",
                               const std::string& content_type = "application/json",
                               const std::string& accept = "application/json",
                               int timeout_ms = 8000);
    HttpResponse ampApiRequest(const std::string& method,
                               const std::string& path_and_query,
                               const std::string& json_body,
                               bool require_mut,
                               const std::shared_ptr<omnidesk::CancelToken>& cancel);
    std::string resolveMediaUserTokenWithFallback() const;
    std::string discoverLocalAdamMusicToken() const;
    std::string discoverLocalAdamStorefront() const;
    std::string fetchDeveloperJwtFromApple();

    LyricsCacheStore* cache_ = nullptr;
    mutable std::mutex cfg_mutex_;
    MusicConfig cfg_;

    mutable std::mutex token_mutex_;
    std::string dev_token_;
    std::string media_user_token_;
    std::string api_token_env_;
    std::string amp_api_base_url_ = "https://amp-api.music.apple.com";

    std::mutex proc_mutex_;
    pid_t managed_pid_ = -1;
};

} // namespace music_plugin
