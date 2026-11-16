#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/mpv_core.hpp"
#include "omnidesk/playback_coordinator.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::host {

class RecyclingGrid;

/**
 * Domain 1 Workbench ("影视与音乐"):
 * Dedicated widescreen 16:9 video card grid, Music & Bilingual Lyrics split view,
 * Feiniu (fnOS) NAS library tab, and MPV player stage bound to IMediaSource plugins.
 */
class MediaWorkbench : public brls::Box {
public:
    MediaWorkbench(std::function<void(const std::string&)> on_entity_focus,
                   std::function<void()> on_open_marketplace);
    ~MediaWorkbench() override;

    void refresh(bool force_remote = true);
    void promptSearch();
    bool cancelActiveFetch();
    bool skipSponsorIfActive();
    void openExternalMedia(const nlohmann::json& payload);
    void focusPlaybackStage(PlaybackRole role);
    void setFullscreenToggleCallback(std::function<void()> on_toggle_fullscreen);
    void setImmersiveFullscreen(bool active);
    bool isImmersiveFullscreen() const { return immersive_fullscreen_; }
    void onViewportLayoutChanged();
    void applyDateFilter(const std::string& date, const std::string& domain = "");

private:
    enum class SubMode {
        Videos,
        DailyCuration,
        MusicLyrics,
        NasLibrary,
        ImdbDiscover,
        PlayerStage,
    };

    void setSubMode(SubMode m);
    void fetchCurrentSubMode(bool preserve_detail = false, bool force_refresh = false);
    void loadMoreMedia();
    void rebuildTabs();
    void rebuildSubContent();
    void renderDailyCurationView();
    brls::Box* buildScopeControlsBox(bool music);
    void rebuildVideosFooter(size_t total_count);
    void refreshMediaGridInPlace();
    std::vector<MediaItem> visibleMusicItems() const;
    bool matchesCurationScope(const MediaItem& item, bool music) const;
    void lookupProvenance();
    void enrichMissingMusicCovers();
    void enrichMissingNasCovers();
    void playVisibleMusic(double start_sec = -1.0);
    void renderVideosView(brls::Box* header_prefix = nullptr);
    void renderMusicLyricsView();
    void renderNasView();
    void renderImdbView();
    void renderPlayerStageView();
    void promptRehydrateOrDateFilter();
    void triggerArchiveRehydrate();
    void activateMediaItem(const MediaItem& item);
    void showSeasonEpisodesDialog(const MediaItem& season_item,
                                  const std::vector<MediaItem>& episodes);
    void selectItem(const MediaItem& item, bool switch_to_player = false);
    void ensureVisibleMusicSelection();
    void selectAdjacentMusicTrack(int delta);
    void updateActiveLyricHighlight(double position_sec);
    void loadNasStatus(bool probe_remote);
    void loadNasDirectory(const std::string& path);
    void loadImdbDiscover(const std::string& chart_id, const std::string& query);
    void triggerLocalAsrGeneration();
    void triggerSubHunterMatch();
    void showVideoCommentsDialog();

    std::function<void(const std::string&)> on_entity_focus_;
    std::function<void()> on_open_marketplace_;
    std::function<void()> on_toggle_fullscreen_;
    SubMode sub_mode_ = SubMode::Videos;
    std::string search_query_;
    enum class MusicScope { Personal, Daily, Interactive };
    MusicScope music_scope_ = MusicScope::Personal;
    // Empty means latest available business date; archive_all_ explicitly disables the date filter.
    std::string curation_date_;
    bool archive_all_ = false;
    int curation_filter_ = 0; // Daily music, daily video, interactive recommendations.
    std::shared_ptr<CancelToken> provenance_request_;
    std::shared_ptr<CancelToken> cover_enrich_request_;
    std::shared_ptr<CancelToken> nas_cover_enrich_request_;
    std::unordered_map<std::string, std::string> nas_imdb_cover_cache_;
    bool immersive_fullscreen_ = false;
    int last_stage_w_ = 0;
    int last_stage_h_ = 0;

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::vector<std::shared_ptr<CancelToken>> active_fetches_;
    std::vector<std::shared_ptr<CancelToken>> more_fetches_;
    std::shared_ptr<CancelToken> detail_request_;
    std::shared_ptr<CancelToken> nas_status_request_;
    std::shared_ptr<CancelToken> nas_dir_request_;
    std::shared_ptr<CancelToken> imdb_request_;
    std::shared_ptr<CancelToken> imdb_action_request_;
    uint64_t fetch_seq_ = 0;
    uint64_t mpv_state_sub_ = 0;
    uint64_t coord_state_sub_ = 0;
    uint64_t recommend_sub_ = 0;
    brls::VoidEvent::Subscription resize_sub_;
    std::shared_ptr<CancelToken> rehydrate_request_;

    std::vector<MediaItem> current_items_;
    std::vector<MediaItem> visible_grid_items_;
    std::vector<MediaItem> visible_music_items_;
    std::vector<nlohmann::json> visible_imdb_items_;
    float active_video_cover_h_ = 160.0f;
    std::unordered_map<std::string, std::vector<MediaItem>> cached_feeds_;
    int media_page_ = 1;
    bool media_loading_more_ = false;
    bool media_has_more_ = true;
    MediaItem active_item_;
    std::vector<LyricLine> active_lyrics_;
    std::vector<DanmakuCue> active_danmaku_;
    std::vector<ChapterSegment> active_chapters_;
    bool has_active_item_ = false;
    std::string last_loaded_stream_key_;
    std::string last_coord_track_id_;
    std::string active_asr_job_id_;
    std::string active_sub_job_id_;

    nlohmann::json nas_status_json_;
    bool nas_status_loaded_ = false;
    bool nas_status_loading_ = false;
    std::string nas_browse_dir_;
    nlohmann::json nas_dir_entries_ = nlohmann::json::array();
    std::string nas_dir_error_;

    std::string imdb_chart_ = "top250";
    std::string imdb_query_;
    nlohmann::json imdb_items_ = nlohmann::json::array();
    std::string imdb_error_;
    bool imdb_loaded_ = false;
    bool imdb_loading_ = false;

    std::vector<brls::Box*> lyric_cards_;
    std::vector<brls::Label*> lyric_orig_labels_;
    std::vector<double> lyric_times_;
    int active_lyric_idx_ = -1;

    brls::Box* header_box_ = nullptr;
    brls::Box* sub_tabs_box_ = nullptr;
    brls::Label* source_status_label_ = nullptr;
    brls::Button* date_filter_btn_ = nullptr;
    RecyclingGrid* stage_grid_ = nullptr;
    RecyclingGrid* music_queue_grid_ = nullptr;
    MpvStageView* mpv_stage_ = nullptr;
};

} // namespace omnidesk::host
