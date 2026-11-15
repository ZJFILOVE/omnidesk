#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <string>

#include "omnidesk/playback_coordinator.hpp"

namespace omnidesk::host {

/**
 * Persistent global bottom mini player bar attached to WorkspaceShellActivity.
 * Keeps background music / active playback controls accessible while reading
 * articles in TextWorkbench or ebooks/PDFs in BookWorkbench, and surfaces
 * 1-click resume when background music was suspended by a foreground video.
 */
class MiniPlayerBar : public brls::Box {
public:
    explicit MiniPlayerBar(std::function<void(PlaybackRole)> on_open_stage = nullptr);
    ~MiniPlayerBar() override;

    void refresh(const PlaybackSessionState& session);

private:
    void buildStaticUi();
    void updateProgress(const PlaybackSessionState& session);

    std::function<void(PlaybackRole)> on_open_stage_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    uint64_t sub_token_ = 0;

    std::string last_item_key_;
    bool last_visible_ = false;
    bool last_paused_ = true;
    bool last_has_suspended_ = false;
    bool last_has_sponsor_ = false;
    bool last_has_queue_ = false;

    brls::Box* role_pill_box_ = nullptr;
    brls::Label* role_pill_label_ = nullptr;
    brls::Box* thumb_box_ = nullptr;
    brls::Label* title_label_ = nullptr;
    brls::Label* meta_label_ = nullptr;
    brls::Label* time_label_ = nullptr;
    brls::Box* progress_fill_ = nullptr;

    brls::Button* prev_btn_ = nullptr;
    brls::Button* play_btn_ = nullptr;
    brls::Button* next_btn_ = nullptr;
    brls::Button* sponsor_btn_ = nullptr;
    brls::Button* resume_music_btn_ = nullptr;
    brls::Button* open_stage_btn_ = nullptr;
};

} // namespace omnidesk::host
