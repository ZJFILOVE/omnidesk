#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "omnidesk/async.hpp"
#include "omnidesk/export.h"
#include "omnidesk/mpv_core.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk {

class CapabilityRegistry;
class EntityBus;
class JobCenter;
class UiTaskRunner;

enum class PlaybackRole {
    None = 0,
    BackgroundMusic,
    ForegroundVideo,
};

struct OMNIDESK_API PlaybackTrack {
    MediaItem item;
    MediaRequestOptions options;
    std::vector<LyricLine> lyrics;
    std::vector<DanmakuCue> danmaku;
    std::vector<ChapterSegment> chapters;
    std::string album;
    bool auto_enrich_timeline = true;
    bool auto_enrich_subtitles = false;
    bool restart_from_beginning = false;
};

struct OMNIDESK_API PlaybackSessionState {
    PlaybackRole active_role = PlaybackRole::None;
    std::string active_owner;
    uint64_t lease_id = 0;
    PlaybackTrack active_track;
    MpvPlaybackState mpv;
    std::vector<PlaybackTrack> music_queue;
    int music_queue_index = -1;
    bool repeat_queue = true;
    bool shuffle = false;
    bool has_suspended_music = false;
    PlaybackTrack suspended_music_track;
    std::vector<PlaybackTrack> suspended_music_queue;
    int suspended_music_index = -1;
    double suspended_music_position_sec = 0.0;
    std::optional<ChapterSegment> active_sponsor;
    bool auto_skip_sponsor = false;
    bool subtitles_attached = false;
    std::string subtitle_status;
    std::string asr_job_id;
};

/**
 * Lease-based Playback Session Arbiter & Coordinator wrapping MPVCore.
 * - Arbitrates between BackgroundMusic queues and ForegroundVideo exclusive stages
 *   (auto-suspending background music with exact breakpoint preservation, and
 *   restoring it on yield/resume).
 * - Persists cross-session resume positions via FileLock-protected JSON ledger.
 * - Automatically wires player.timeline.v1 (SponsorBlock), subtitle.fetch.v1
 *   (SubHunter), and speech.asr.v1 (FunASR) hot-mounting onto live playback.
 */
class OMNIDESK_API PlaybackCoordinator {
public:
    using StateCallback = std::function<void(const PlaybackSessionState&)>;

    static PlaybackCoordinator& instance();

    void configure(std::string state_path,
                   CapabilityRegistry* caps,
                   EntityBus* bus,
                   JobCenter* jobs = nullptr,
                   UiTaskRunner* ui_tasks = nullptr,
                   MPVCore* mpv = nullptr);
    void registerCapabilities(CapabilityRegistry& caps);
    void shutdown();

    uint64_t play(PlaybackRole role, const std::string& owner_id, PlaybackTrack track);
    uint64_t playMusicQueue(const std::string& owner_id,
                            std::vector<PlaybackTrack> queue,
                            int start_index = 0);
    bool releaseLease(uint64_t lease_id, bool resume_suspended = true);
    bool resumeSuspendedMusic();

    void play();
    void pause();
    void playPause();
    void stop(bool resume_suspended = false);
    void nextTrack();
    void prevTrack();
    void seekAbsolute(double seconds);
    void seekRelative(double delta_seconds);
    void setVolume(int volume);
    void setSpeed(double speed);
    void setRepeatQueue(bool repeat);
    void setShuffle(bool shuffle);
    void setAutoSkipSponsor(bool auto_skip);
    bool skipActiveSponsor();

    void attachSubtitlesAsync(bool manual_trigger = true);
    std::string triggerAsrHotMount();

    double getResumePosition(const std::string& item_id) const;
    void saveResumePosition(const std::string& item_id,
                            const std::string& title,
                            double position_sec,
                            double duration_sec);
    void clearResumePosition(const std::string& item_id);

    PlaybackSessionState state() const;
    uint64_t subscribe(StateCallback cb);
    void unsubscribe(uint64_t token);

private:
    PlaybackCoordinator();
    ~PlaybackCoordinator();

    PlaybackCoordinator(const PlaybackCoordinator&) = delete;
    PlaybackCoordinator& operator=(const PlaybackCoordinator&) = delete;

    struct Impl;
    std::unique_ptr<Impl> impl_;
};

OMNIDESK_API const char* playbackRoleLabel(PlaybackRole role);

} // namespace omnidesk
