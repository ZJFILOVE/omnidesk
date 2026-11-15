#pragma once

#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "omnidesk/export.h"
#include "omnidesk/playback_coordinator.hpp"

namespace omnidesk {

struct OMNIDESK_API Mpris2MetadataSnapshot {
    std::string playback_status = "Stopped";
    std::string loop_status = "None";
    std::string track_object_path = "/org/mpris/MediaPlayer2/TrackList/NoTrack";
    std::string title;
    std::vector<std::string> artists;
    std::string album;
    std::string art_url;
    int64_t length_us = 0;
    int64_t position_us = 0;
    double volume = 0.85;
    double rate = 1.0;
    bool shuffle = false;
    bool can_go_next = false;
    bool can_go_previous = false;
    bool can_play = false;
    bool can_pause = false;
    bool can_seek = false;
};

/**
 * Linux MPRIS2 D-Bus media player service (org.mpris.MediaPlayer2.omnidesk)
 * powered by systemd sd-bus. Enables Hyprland Waybar MPRIS modules, playerctl,
 * and desktop hardware media keys to control OmniDesk PlaybackCoordinator.
 *
 * Gracefully no-ops in headless environments lacking a D-Bus session bus.
 */
class OMNIDESK_API Mpris2Service {
public:
    using RaiseCallback = std::function<void()>;

    explicit Mpris2Service(PlaybackCoordinator* coordinator = nullptr);
    ~Mpris2Service();

    Mpris2Service(const Mpris2Service&) = delete;
    Mpris2Service& operator=(const Mpris2Service&) = delete;

    void setRaiseCallback(RaiseCallback cb);
    bool start();
    void stop();
    bool isRunning() const;
    std::string busName() const;

    static Mpris2MetadataSnapshot snapshotFromSession(const PlaybackSessionState& session);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace omnidesk
