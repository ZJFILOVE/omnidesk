#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <string>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk {
class MpvStageView;
}

namespace omnidesk::host {

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

    void refresh();
    void promptSearch();
    bool cancelActiveFetch();
    bool skipSponsorIfActive();

private:
    enum class SubMode {
        Videos,
        MusicLyrics,
        NasLibrary,
        PlayerStage,
    };

    void setSubMode(SubMode m);
    void rebuildTabs();
    void rebuildSubContent();
    void renderVideosView();
    void renderMusicLyricsView();
    void renderNasView();
    void renderPlayerStageView();
    void selectItem(const MediaItem& item, bool switch_to_player = false);

    std::function<void(const std::string&)> on_entity_focus_;
    std::function<void()> on_open_marketplace_;
    SubMode sub_mode_ = SubMode::Videos;
    std::string search_query_;

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::vector<std::shared_ptr<CancelToken>> active_fetches_;
    uint64_t fetch_seq_ = 0;

    std::vector<MediaItem> current_items_;
    MediaItem active_item_;
    std::vector<LyricLine> active_lyrics_;
    std::vector<ChapterSegment> active_chapters_;
    bool has_active_item_ = false;
    std::string last_loaded_stream_key_;

    brls::Box* sub_tabs_box_ = nullptr;
    brls::Label* source_status_label_ = nullptr;
    brls::Box* stage_box_ = nullptr;
    MpvStageView* mpv_stage_ = nullptr;
};

} // namespace omnidesk::host
