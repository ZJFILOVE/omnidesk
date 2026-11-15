#include "omnidesk/mpris2.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <poll.h>
#include <sys/eventfd.h>
#include <systemd/sd-bus.h>
#include <thread>
#include <unistd.h>

#include <borealis/core/logger.hpp>

namespace omnidesk {
namespace {

std::string sanitizeObjectId(const std::string& raw) {
    if (raw.empty()) return "track_0";
    std::string out;
    out.reserve(raw.size() + 4);
    for (unsigned char ch : raw) {
        if ((ch >= 'A' && ch <= 'Z') ||
            (ch >= 'a' && ch <= 'z') ||
            (ch >= '0' && ch <= '9') ||
            ch == '_') {
            out.push_back(static_cast<char>(ch));
        } else {
            char hex[4];
            std::snprintf(hex, sizeof(hex), "_%02x", ch);
            out += hex;
        }
        if (out.size() >= 96) break;
    }
    if (out.empty()) return "track_0";
    return out;
}

int appendMetadataDict(sd_bus_message* reply, const Mpris2MetadataSnapshot& snap) {
    int r = sd_bus_message_open_container(reply, 'a', "{sv}");
    if (r < 0) return r;

    auto addStr = [&](const char* key, const std::string& val) -> int {
        int rc = sd_bus_message_open_container(reply, 'e', "sv");
        if (rc < 0) return rc;
        rc = sd_bus_message_append_basic(reply, 's', key);
        if (rc < 0) return rc;
        rc = sd_bus_message_open_container(reply, 'v', "s");
        if (rc < 0) return rc;
        rc = sd_bus_message_append_basic(reply, 's', val.c_str());
        if (rc < 0) return rc;
        rc = sd_bus_message_close_container(reply);
        if (rc < 0) return rc;
        return sd_bus_message_close_container(reply);
    };

    {
        r = sd_bus_message_open_container(reply, 'e', "sv");
        if (r < 0) return r;
        r = sd_bus_message_append_basic(reply, 's', "mpris:trackid");
        if (r < 0) return r;
        r = sd_bus_message_open_container(reply, 'v', "o");
        if (r < 0) return r;
        r = sd_bus_message_append_basic(reply, 'o', snap.track_object_path.c_str());
        if (r < 0) return r;
        r = sd_bus_message_close_container(reply);
        if (r < 0) return r;
        r = sd_bus_message_close_container(reply);
        if (r < 0) return r;
    }

    if (snap.length_us > 0) {
        r = sd_bus_message_open_container(reply, 'e', "sv");
        if (r < 0) return r;
        r = sd_bus_message_append_basic(reply, 's', "mpris:length");
        if (r < 0) return r;
        r = sd_bus_message_open_container(reply, 'v', "x");
        if (r < 0) return r;
        r = sd_bus_message_append_basic(reply, 'x', &snap.length_us);
        if (r < 0) return r;
        r = sd_bus_message_close_container(reply);
        if (r < 0) return r;
        r = sd_bus_message_close_container(reply);
        if (r < 0) return r;
    }

    if (!snap.title.empty()) {
        r = addStr("xesam:title", snap.title);
        if (r < 0) return r;
    }
    if (!snap.artists.empty()) {
        r = sd_bus_message_open_container(reply, 'e', "sv");
        if (r < 0) return r;
        r = sd_bus_message_append_basic(reply, 's', "xesam:artist");
        if (r < 0) return r;
        r = sd_bus_message_open_container(reply, 'v', "as");
        if (r < 0) return r;
        r = sd_bus_message_open_container(reply, 'a', "s");
        if (r < 0) return r;
        for (const auto& a : snap.artists) {
            r = sd_bus_message_append_basic(reply, 's', a.c_str());
            if (r < 0) return r;
        }
        r = sd_bus_message_close_container(reply);
        if (r < 0) return r;
        r = sd_bus_message_close_container(reply);
        if (r < 0) return r;
        r = sd_bus_message_close_container(reply);
        if (r < 0) return r;
    }
    if (!snap.album.empty()) {
        r = addStr("xesam:album", snap.album);
        if (r < 0) return r;
    }
    if (!snap.art_url.empty()) {
        r = addStr("mpris:artUrl", snap.art_url);
        if (r < 0) return r;
    }

    return sd_bus_message_close_container(reply);
}

} // namespace

struct Mpris2Service::Impl {
    static const sd_bus_vtable kRootVtable[];
    static const sd_bus_vtable kPlayerVtable[];

    PlaybackCoordinator* coordinator = nullptr;
    std::mutex mutex;
    RaiseCallback raise_cb;
    Mpris2MetadataSnapshot snapshot;
    bool dirty_props = false;

    sd_bus* bus = nullptr;
    sd_bus_slot* root_slot = nullptr;
    sd_bus_slot* player_slot = nullptr;
    int wake_fd = -1;
    std::string bus_name;
    std::atomic<bool> running{false};
    std::atomic<bool> stop_requested{false};
    std::thread thread;
    uint64_t sub_token = 0;

    void signalWakeup() {
        if (wake_fd >= 0) {
            uint64_t one = 1;
            ssize_t ignored = ::write(wake_fd, &one, sizeof(one));
            (void)ignored;
        }
    }

    void drainWakeup() {
        if (wake_fd >= 0) {
            uint64_t val = 0;
            while (::read(wake_fd, &val, sizeof(val)) > 0) {
            }
        }
    }

    Mpris2MetadataSnapshot currentSnapshot() {
        std::lock_guard<std::mutex> lk(mutex);
        return snapshot;
    }

    static int onRaise(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        RaiseCallback cb;
        {
            std::lock_guard<std::mutex> lk(self->mutex);
            cb = self->raise_cb;
        }
        if (cb) cb();
        return sd_bus_reply_method_return(m, "");
    }

    static int onQuit(sd_bus_message* m, void*, sd_bus_error*) {
        return sd_bus_reply_method_return(m, "");
    }

    static int getRootBool(sd_bus*, const char*, const char*, const char* prop,
                           sd_bus_message* reply, void*, sd_bus_error*) {
        int val = 0;
        if (std::strcmp(prop, "CanRaise") == 0) val = 1;
        return sd_bus_message_append_basic(reply, 'b', &val);
    }

    static int getRootString(sd_bus*, const char*, const char*, const char* prop,
                             sd_bus_message* reply, void*, sd_bus_error*) {
        const char* val = (std::strcmp(prop, "Identity") == 0) ? "OmniDesk" : "omnidesk";
        return sd_bus_message_append_basic(reply, 's', val);
    }

    static int getSupportedUriSchemes(sd_bus*, const char*, const char*, const char*,
                                      sd_bus_message* reply, void*, sd_bus_error*) {
        static const char* const schemes[] = {"http", "https", "file", nullptr};
        return sd_bus_message_append_strv(reply, const_cast<char**>(schemes));
    }

    static int getSupportedMimeTypes(sd_bus*, const char*, const char*, const char*,
                                     sd_bus_message* reply, void*, sd_bus_error*) {
        static const char* const mimes[] = {
            "audio/flac", "audio/mpeg", "audio/mp4", "audio/ogg",
            "video/mp4", "video/x-matroska", "video/webm", nullptr
        };
        return sd_bus_message_append_strv(reply, const_cast<char**>(mimes));
    }

    // Player methods
    static int onNext(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        if (self->coordinator) self->coordinator->nextTrack();
        return sd_bus_reply_method_return(m, "");
    }

    static int onPrevious(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        if (self->coordinator) self->coordinator->prevTrack();
        return sd_bus_reply_method_return(m, "");
    }

    static int onPause(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        if (self->coordinator) self->coordinator->pause();
        return sd_bus_reply_method_return(m, "");
    }

    static int onPlayPause(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        if (self->coordinator) self->coordinator->playPause();
        return sd_bus_reply_method_return(m, "");
    }

    static int onStop(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        if (self->coordinator) self->coordinator->stop(false);
        return sd_bus_reply_method_return(m, "");
    }

    static int onPlay(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        if (self->coordinator) self->coordinator->play();
        return sd_bus_reply_method_return(m, "");
    }

    static int onSeek(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        int64_t offset_us = 0;
        int r = sd_bus_message_read(m, "x", &offset_us);
        if (r < 0) return r;
        if (self->coordinator) {
            self->coordinator->seekRelative(static_cast<double>(offset_us) / 1000000.0);
        }
        return sd_bus_reply_method_return(m, "");
    }

    static int onSetPosition(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        const char* track_id = nullptr;
        int64_t pos_us = 0;
        int r = sd_bus_message_read(m, "ox", &track_id, &pos_us);
        if (r < 0) return r;
        (void)track_id;
        if (self->coordinator && pos_us >= 0) {
            self->coordinator->seekAbsolute(static_cast<double>(pos_us) / 1000000.0);
        }
        return sd_bus_reply_method_return(m, "");
    }

    static int onOpenUri(sd_bus_message* m, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        const char* uri = nullptr;
        int r = sd_bus_message_read(m, "s", &uri);
        if (r < 0) return r;
        if (self->coordinator && uri && *uri) {
            PlaybackTrack tr;
            tr.item.id = std::string("uri:") + uri;
            tr.item.title = uri;
            tr.item.play_url = uri;
            tr.options.url = uri;
            self->coordinator->play(PlaybackRole::ForegroundVideo, "mpris2", std::move(tr));
        }
        return sd_bus_reply_method_return(m, "");
    }

    // Player property getters / setters
    static int getPlaybackStatus(sd_bus*, const char*, const char*, const char*,
                                 sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto snap = static_cast<Impl*>(userdata)->currentSnapshot();
        return sd_bus_message_append_basic(reply, 's', snap.playback_status.c_str());
    }

    static int getLoopStatus(sd_bus*, const char*, const char*, const char*,
                             sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto snap = static_cast<Impl*>(userdata)->currentSnapshot();
        return sd_bus_message_append_basic(reply, 's', snap.loop_status.c_str());
    }

    static int setLoopStatus(sd_bus*, const char*, const char*, const char*,
                             sd_bus_message* value, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        const char* s = nullptr;
        int r = sd_bus_message_read(value, "s", &s);
        if (r < 0) return r;
        if (self->coordinator && s) {
            self->coordinator->setRepeatQueue(std::strcmp(s, "None") != 0);
        }
        return 0;
    }

    static int getRate(sd_bus*, const char*, const char*, const char*,
                       sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto snap = static_cast<Impl*>(userdata)->currentSnapshot();
        return sd_bus_message_append_basic(reply, 'd', &snap.rate);
    }

    static int setRate(sd_bus*, const char*, const char*, const char*,
                       sd_bus_message* value, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        double rate = 1.0;
        int r = sd_bus_message_read(value, "d", &rate);
        if (r < 0) return r;
        if (self->coordinator) self->coordinator->setSpeed(rate);
        return 0;
    }

    static int getShuffle(sd_bus*, const char*, const char*, const char*,
                          sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto snap = static_cast<Impl*>(userdata)->currentSnapshot();
        int b = snap.shuffle ? 1 : 0;
        return sd_bus_message_append_basic(reply, 'b', &b);
    }

    static int setShuffle(sd_bus*, const char*, const char*, const char*,
                          sd_bus_message* value, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        int b = 0;
        int r = sd_bus_message_read(value, "b", &b);
        if (r < 0) return r;
        if (self->coordinator) self->coordinator->setShuffle(b != 0);
        return 0;
    }

    static int getMetadata(sd_bus*, const char*, const char*, const char*,
                           sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto snap = static_cast<Impl*>(userdata)->currentSnapshot();
        return appendMetadataDict(reply, snap);
    }

    static int getVolume(sd_bus*, const char*, const char*, const char*,
                         sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto snap = static_cast<Impl*>(userdata)->currentSnapshot();
        return sd_bus_message_append_basic(reply, 'd', &snap.volume);
    }

    static int setVolume(sd_bus*, const char*, const char*, const char*,
                         sd_bus_message* value, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        double vol = 0.85;
        int r = sd_bus_message_read(value, "d", &vol);
        if (r < 0) return r;
        if (self->coordinator) {
            self->coordinator->setVolume(static_cast<int>(std::clamp(vol, 0.0, 1.0) * 100.0 + 0.5));
        }
        return 0;
    }

    static int getPosition(sd_bus*, const char*, const char*, const char*,
                           sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto* self = static_cast<Impl*>(userdata);
        int64_t pos = 0;
        if (self->coordinator) {
            const auto st = self->coordinator->state();
            pos = static_cast<int64_t>(std::max(0.0, st.mpv.position_sec) * 1000000.0);
        }
        return sd_bus_message_append_basic(reply, 'x', &pos);
    }

    static int getRateBound(sd_bus*, const char*, const char*, const char* prop,
                            sd_bus_message* reply, void*, sd_bus_error*) {
        double v = (std::strcmp(prop, "MinimumRate") == 0) ? 0.25 : 4.0;
        return sd_bus_message_append_basic(reply, 'd', &v);
    }

    static int getPlayerCap(sd_bus*, const char*, const char*, const char* prop,
                            sd_bus_message* reply, void* userdata, sd_bus_error*) {
        auto snap = static_cast<Impl*>(userdata)->currentSnapshot();
        int b = 0;
        if (std::strcmp(prop, "CanControl") == 0) b = 1;
        else if (std::strcmp(prop, "CanGoNext") == 0) b = snap.can_go_next ? 1 : 0;
        else if (std::strcmp(prop, "CanGoPrevious") == 0) b = snap.can_go_previous ? 1 : 0;
        else if (std::strcmp(prop, "CanPlay") == 0) b = snap.can_play ? 1 : 0;
        else if (std::strcmp(prop, "CanPause") == 0) b = snap.can_pause ? 1 : 0;
        else if (std::strcmp(prop, "CanSeek") == 0) b = snap.can_seek ? 1 : 0;
        return sd_bus_message_append_basic(reply, 'b', &b);
    }

    void eventLoop() {
        while (!stop_requested.load(std::memory_order_acquire)) {
            bool emit_props = false;
            {
                std::lock_guard<std::mutex> lk(mutex);
                if (dirty_props) {
                    dirty_props = false;
                    emit_props = true;
                }
            }
            if (emit_props && bus) {
                sd_bus_emit_properties_changed(
                    bus,
                    "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player",
                    "PlaybackStatus", "Metadata", "Volume", "Rate",
                    "LoopStatus", "Shuffle", "CanGoNext", "CanGoPrevious",
                    "CanPlay", "CanPause", "CanSeek",
                    nullptr);
            }

            int r = 0;
            do {
                r = sd_bus_process(bus, nullptr);
            } while (r > 0 && !stop_requested.load(std::memory_order_acquire));
            if (r < 0) break;

            const int bus_fd = sd_bus_get_fd(bus);
            const int events = sd_bus_get_events(bus);
            uint64_t timeout_usec = 0;
            sd_bus_get_timeout(bus, &timeout_usec);
            int timeout_ms = 250;
            if (timeout_usec != UINT64_MAX) {
                uint64_t ms = timeout_usec / 1000;
                if (ms < static_cast<uint64_t>(timeout_ms)) {
                    timeout_ms = static_cast<int>(ms);
                }
            }

            struct pollfd pfds[2]{};
            pfds[0].fd = bus_fd;
            pfds[0].events = static_cast<short>(events);
            pfds[1].fd = wake_fd;
            pfds[1].events = POLLIN;
            ::poll(pfds, 2, std::max(10, timeout_ms));
            if ((pfds[1].revents & POLLIN) != 0) {
                drainWakeup();
            }
        }
    }
};

const sd_bus_vtable Mpris2Service::Impl::kRootVtable[] = {
    SD_BUS_VTABLE_START(0),
    SD_BUS_METHOD("Raise", "", "", Mpris2Service::Impl::onRaise, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("Quit", "", "", Mpris2Service::Impl::onQuit, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_PROPERTY("CanQuit", "b", Mpris2Service::Impl::getRootBool, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("CanRaise", "b", Mpris2Service::Impl::getRootBool, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("HasTrackList", "b", Mpris2Service::Impl::getRootBool, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("Identity", "s", Mpris2Service::Impl::getRootString, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("DesktopEntry", "s", Mpris2Service::Impl::getRootString, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("SupportedUriSchemes", "as", Mpris2Service::Impl::getSupportedUriSchemes, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("SupportedMimeTypes", "as", Mpris2Service::Impl::getSupportedMimeTypes, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_VTABLE_END
};

const sd_bus_vtable Mpris2Service::Impl::kPlayerVtable[] = {
    SD_BUS_VTABLE_START(0),
    SD_BUS_METHOD("Next", "", "", Mpris2Service::Impl::onNext, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("Previous", "", "", Mpris2Service::Impl::onPrevious, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("Pause", "", "", Mpris2Service::Impl::onPause, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("PlayPause", "", "", Mpris2Service::Impl::onPlayPause, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("Stop", "", "", Mpris2Service::Impl::onStop, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("Play", "", "", Mpris2Service::Impl::onPlay, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("Seek", "x", "", Mpris2Service::Impl::onSeek, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("SetPosition", "ox", "", Mpris2Service::Impl::onSetPosition, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_METHOD("OpenUri", "s", "", Mpris2Service::Impl::onOpenUri, SD_BUS_VTABLE_UNPRIVILEGED),
    SD_BUS_SIGNAL("Seeked", "x", 0),
    SD_BUS_PROPERTY("PlaybackStatus", "s", Mpris2Service::Impl::getPlaybackStatus, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_WRITABLE_PROPERTY("LoopStatus", "s", Mpris2Service::Impl::getLoopStatus, Mpris2Service::Impl::setLoopStatus, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_WRITABLE_PROPERTY("Rate", "d", Mpris2Service::Impl::getRate, Mpris2Service::Impl::setRate, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_WRITABLE_PROPERTY("Shuffle", "b", Mpris2Service::Impl::getShuffle, Mpris2Service::Impl::setShuffle, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_PROPERTY("Metadata", "a{sv}", Mpris2Service::Impl::getMetadata, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_WRITABLE_PROPERTY("Volume", "d", Mpris2Service::Impl::getVolume, Mpris2Service::Impl::setVolume, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_PROPERTY("Position", "x", Mpris2Service::Impl::getPosition, 0, 0),
    SD_BUS_PROPERTY("MinimumRate", "d", Mpris2Service::Impl::getRateBound, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("MaximumRate", "d", Mpris2Service::Impl::getRateBound, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_PROPERTY("CanGoNext", "b", Mpris2Service::Impl::getPlayerCap, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_PROPERTY("CanGoPrevious", "b", Mpris2Service::Impl::getPlayerCap, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_PROPERTY("CanPlay", "b", Mpris2Service::Impl::getPlayerCap, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_PROPERTY("CanPause", "b", Mpris2Service::Impl::getPlayerCap, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_PROPERTY("CanSeek", "b", Mpris2Service::Impl::getPlayerCap, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
    SD_BUS_PROPERTY("CanControl", "b", Mpris2Service::Impl::getPlayerCap, 0, SD_BUS_VTABLE_PROPERTY_CONST),
    SD_BUS_VTABLE_END
};

Mpris2MetadataSnapshot Mpris2Service::snapshotFromSession(const PlaybackSessionState& session) {
    Mpris2MetadataSnapshot snap;
    if (session.active_role == PlaybackRole::None && session.mpv.stopped) {
        snap.playback_status = "Stopped";
    } else if (session.mpv.paused || session.mpv.stopped) {
        snap.playback_status = "Paused";
    } else {
        snap.playback_status = "Playing";
    }
    snap.loop_status = session.repeat_queue ? "Playlist" : "None";
    snap.shuffle = session.shuffle;
    snap.rate = session.mpv.speed > 0.0 ? session.mpv.speed : 1.0;
    snap.volume = std::clamp(static_cast<double>(session.mpv.volume) / 100.0, 0.0, 1.0);
    snap.position_us = static_cast<int64_t>(std::max(0.0, session.mpv.position_sec) * 1000000.0);
    const double dur = session.mpv.duration_sec > 0.0
                           ? session.mpv.duration_sec
                           : session.active_track.item.duration_sec;
    snap.length_us = static_cast<int64_t>(std::max(0.0, dur) * 1000000.0);

    if (!session.active_track.item.id.empty()) {
        snap.track_object_path =
            "/org/mpris/MediaPlayer2/Track/" + sanitizeObjectId(session.active_track.item.id);
    }
    snap.title = session.active_track.item.title;
    if (!session.active_track.item.creator.empty()) {
        snap.artists.push_back(session.active_track.item.creator);
    }
    snap.album = session.active_track.album.empty()
                     ? session.active_track.item.source_badge
                     : session.active_track.album;
    snap.art_url = session.active_track.item.cover_url;

    const bool has_item = !session.active_track.item.id.empty();
    const bool has_queue = !session.music_queue.empty();
    snap.can_play = has_item || session.has_suspended_music;
    snap.can_pause = has_item && !session.mpv.stopped;
    snap.can_seek = has_item && dur > 0.0;
    snap.can_go_next = has_queue;
    snap.can_go_previous = has_queue;
    return snap;
}

Mpris2Service::Mpris2Service(PlaybackCoordinator* coordinator)
    : impl_(std::make_unique<Impl>()) {
    impl_->coordinator = coordinator ? coordinator : &PlaybackCoordinator::instance();
}

Mpris2Service::~Mpris2Service() {
    stop();
}

void Mpris2Service::setRaiseCallback(RaiseCallback cb) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    impl_->raise_cb = std::move(cb);
}

bool Mpris2Service::start() {
    if (impl_->running.load(std::memory_order_acquire)) {
        return true;
    }
    sd_bus* bus = nullptr;
    int r = sd_bus_open_user(&bus);
    if (r < 0 || !bus) {
        brls::Logger::info("[MPRIS2] User D-Bus unavailable ({}), MPRIS2 disabled", std::strerror(-r));
        return false;
    }

    r = sd_bus_add_object_vtable(
        bus, &impl_->root_slot, "/org/mpris/MediaPlayer2",
        "org.mpris.MediaPlayer2", Mpris2Service::Impl::kRootVtable, impl_.get());
    if (r < 0) {
        sd_bus_unref(bus);
        return false;
    }
    r = sd_bus_add_object_vtable(
        bus, &impl_->player_slot, "/org/mpris/MediaPlayer2",
        "org.mpris.MediaPlayer2.Player", Mpris2Service::Impl::kPlayerVtable, impl_.get());
    if (r < 0) {
        sd_bus_slot_unref(impl_->root_slot);
        impl_->root_slot = nullptr;
        sd_bus_unref(bus);
        return false;
    }

    std::string chosen_name = "org.mpris.MediaPlayer2.omnidesk";
    r = sd_bus_request_name(bus, chosen_name.c_str(), 0);
    if (r < 0) {
        chosen_name = "org.mpris.MediaPlayer2.omnidesk.instance" + std::to_string(::getpid());
        r = sd_bus_request_name(bus, chosen_name.c_str(), 0);
        if (r < 0) {
            sd_bus_slot_unref(impl_->player_slot);
            sd_bus_slot_unref(impl_->root_slot);
            impl_->player_slot = nullptr;
            impl_->root_slot = nullptr;
            sd_bus_unref(bus);
            return false;
        }
    }

    int wfd = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (wfd < 0) {
        sd_bus_slot_unref(impl_->player_slot);
        sd_bus_slot_unref(impl_->root_slot);
        impl_->player_slot = nullptr;
        impl_->root_slot = nullptr;
        sd_bus_unref(bus);
        return false;
    }

    impl_->bus = bus;
    impl_->wake_fd = wfd;
    impl_->bus_name = chosen_name;
    impl_->stop_requested.store(false, std::memory_order_release);
    if (impl_->coordinator) {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->snapshot = snapshotFromSession(impl_->coordinator->state());
        impl_->sub_token = impl_->coordinator->subscribe([this](const PlaybackSessionState& st) {
            Mpris2MetadataSnapshot next = snapshotFromSession(st);
            bool changed = false;
            {
                std::lock_guard<std::mutex> inner_lk(impl_->mutex);
                if (next.playback_status != impl_->snapshot.playback_status ||
                    next.title != impl_->snapshot.title ||
                    next.track_object_path != impl_->snapshot.track_object_path ||
                    std::abs(next.volume - impl_->snapshot.volume) > 0.005 ||
                    std::abs(next.rate - impl_->snapshot.rate) > 0.005 ||
                    next.loop_status != impl_->snapshot.loop_status ||
                    next.shuffle != impl_->snapshot.shuffle) {
                    changed = true;
                }
                impl_->snapshot = std::move(next);
                if (changed) {
                    impl_->dirty_props = true;
                }
            }
            if (changed) {
                impl_->signalWakeup();
            }
        });
    }

    impl_->running.store(true, std::memory_order_release);
    impl_->thread = std::thread([this]() { impl_->eventLoop(); });
    brls::Logger::info("[MPRIS2] Registered D-Bus media player '{}'", chosen_name);
    return true;
}

void Mpris2Service::stop() {
    if (!impl_->running.exchange(false, std::memory_order_acq_rel)) {
        return;
    }
    if (impl_->coordinator && impl_->sub_token != 0) {
        impl_->coordinator->unsubscribe(impl_->sub_token);
        impl_->sub_token = 0;
    }
    impl_->stop_requested.store(true, std::memory_order_release);
    impl_->signalWakeup();
    if (impl_->thread.joinable()) {
        impl_->thread.join();
    }
    if (impl_->wake_fd >= 0) {
        ::close(impl_->wake_fd);
        impl_->wake_fd = -1;
    }
    if (impl_->player_slot) {
        sd_bus_slot_unref(impl_->player_slot);
        impl_->player_slot = nullptr;
    }
    if (impl_->root_slot) {
        sd_bus_slot_unref(impl_->root_slot);
        impl_->root_slot = nullptr;
    }
    if (impl_->bus) {
        if (!impl_->bus_name.empty()) {
            sd_bus_release_name(impl_->bus, impl_->bus_name.c_str());
        }
        sd_bus_unref(impl_->bus);
        impl_->bus = nullptr;
    }
    impl_->bus_name.clear();
}

bool Mpris2Service::isRunning() const {
    return impl_->running.load(std::memory_order_acquire);
}

std::string Mpris2Service::busName() const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    return impl_->bus_name;
}

} // namespace omnidesk
