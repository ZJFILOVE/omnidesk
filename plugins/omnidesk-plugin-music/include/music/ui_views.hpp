#pragma once

#include "music/music_engine.hpp"
#include <atomic>
#include <borealis.hpp>
#include <memory>
#include <omnidesk/image_cache.hpp>
#include <omnidesk/mpv_core.hpp>
#include <string>
#include <vector>

namespace music_plugin {

class MusicWorkspaceView : public brls::Box {
public:
    explicit MusicWorkspaceView(std::shared_ptr<MusicEngine> engine);
    ~MusicWorkspaceView() override;

private:
    enum class SubTab {
        ForYou,
        AiCuration,
        Recent,
        Library,
        Search,
        NowPlaying,
    };

    void buildUi();
    void setSubTab(SubTab tab);
    void refreshHeader();
    void refreshIslandBar();
    void syncStageLayout();
    void renderActiveTab();

    void triggerFeedLoad(const std::string& feed_id, const std::string& query = "", bool force = false);
    void triggerCurationLoad(const std::string& requested_date = "", bool silent = false);
    void triggerExportsRefresh();
    void triggerInteractiveWriteback(const std::string& prompt);
    void triggerTrackExport(const MusicTrack& track, bool convert_to_flac);
    void triggerContainerExport(const ContainerItem& container, bool convert_to_flac);

    void openContainerView(const ContainerItem& container);
    void renderForYouHeroCard();
    void renderAiCurationBody();
    void renderRecentBody();
    void renderFeedBody(const FeedOutcome& outcome);
    void renderLibraryBody();
    void renderNowPlayingBody();
    void renderDownloadsPanel();
    void addTrackRowCard(brls::Box* parent,
                         const std::vector<MusicTrack>& queue_context,
                         size_t index,
                         const std::string& fallback_cover = "");
    void updateActiveLyricHighlight(double position_sec);

    void promptAppleIdLogin();
    void promptTwoFactorCode();
    void promptSearch();
    void promptAiWriteback();
    void promptTargetPlaylistName();
    void promptSelectArchiveDate();
    void promptImportLyrics();
    void promptMediaUserToken();
    void explainLyricAtIndex(size_t index, const std::string& focus_phrase = "");

    std::shared_ptr<MusicEngine> engine_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    uint64_t engine_sub_ = 0;
    uint64_t mpv_sub_ = 0;
    uint64_t feed_req_seq_ = 0;
    uint64_t curation_req_seq_ = 0;

    SubTab active_tab_ = SubTab::ForYou;
    std::string search_query_;
    std::string search_filter_ = "all"; // "all" | "songs" | "containers" | "mv"
    FeedOutcome current_feed_;
    bool feed_loading_ = false;

    CurationWorkspaceSnapshot curation_snap_;
    bool curation_loading_ = false;
    bool ai_curating_ = false;
    std::string selected_archive_date_;
    std::string ai_prompt_draft_;
    std::string ai_target_playlist_ = "OmniDesk AI 灵感电台";
    bool ai_sync_playlist_ = true;
    bool ai_autoplay_ = true;
    std::string last_ai_banner_;
    bool last_ai_banner_ok_ = true;

    bool showing_container_detail_ = false;
    ContainerItem active_container_;
    std::vector<MusicTrack> container_tracks_;
    std::vector<ContainerItem> library_playlists_;

    bool show_auth_panel_ = false;
    bool show_downloads_panel_ = false;
    std::vector<ExportJobInfo> export_jobs_;
    std::string pending_email_;

    int active_lyric_idx_ = -1;

    PhraseExplanation last_explanation_;
    bool has_explanation_ = false;

    brls::Label* status_label_ = nullptr;
    brls::Box* drm_pill_box_ = nullptr;
    brls::Label* drm_pill_label_ = nullptr;
    brls::Box* island_bar_box_ = nullptr;
    brls::Label* island_title_label_ = nullptr;
    brls::Label* island_lyric_label_ = nullptr;
    brls::Box* sub_tabs_box_ = nullptr;
    brls::Box* auth_panel_box_ = nullptr;
    brls::Box* downloads_panel_box_ = nullptr;
    brls::ScrollingFrame* scroll_frame_ = nullptr;
    brls::Box* stage_box_ = nullptr;
    omnidesk::MpvStageView* mv_stage_ = nullptr;

    std::vector<brls::Box*> lyric_cards_;
    std::vector<brls::Label*> lyric_orig_labels_;
    std::vector<double> lyric_times_;
};

class MusicCompanionDockView : public brls::Box {
public:
    explicit MusicCompanionDockView(std::shared_ptr<MusicEngine> engine);
    ~MusicCompanionDockView() override;

private:
    void refreshSummary();

    std::shared_ptr<MusicEngine> engine_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    uint64_t engine_sub_ = 0;
    uint64_t mpv_sub_ = 0;

    omnidesk::CachedImageView* cover_view_ = nullptr;
    brls::Label* track_label_ = nullptr;
    brls::Label* lyric_orig_label_ = nullptr;
    brls::Label* lyric_trans_label_ = nullptr;
    brls::Label* note_label_ = nullptr;
};

} // namespace music_plugin
