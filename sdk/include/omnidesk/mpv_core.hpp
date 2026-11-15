#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include <borealis/core/box.hpp>
#include <nanovg.h>

#include "omnidesk/export.h"
#include "omnidesk/types.hpp"

namespace omnidesk {

/// Per-stream playback request with isolated HTTP headers, DASH audio, and external subtitles.
struct OMNIDESK_API MediaRequestOptions {
    std::string url;
    std::string audio_url;
    std::vector<std::string> subtitle_urls;
    std::unordered_map<std::string, std::string> http_headers;
    std::string referrer;
    std::string user_agent;
    double start_seconds = 0.0;
    double end_seconds = -1.0;
    bool hwdec = true;
};

struct OMNIDESK_API MpvPlaybackState {
    bool initialized = false;
    bool playing = false;
    bool paused = true;
    bool stopped = true;
    bool buffering = false;
    double position_sec = 0.0;
    double duration_sec = 0.0;
    double speed = 1.0;
    int volume = 85;
    int video_width = 0;
    int video_height = 0;
    double fps = 0.0;
    double video_bitrate = 0.0;
    int64_t decoder_drop_frames = 0;
    int64_t output_drop_frames = 0;
    std::string video_codec;
    std::string audio_codec;
    int audio_samplerate = 0;
    int audio_channels = 0;
    double cache_duration_sec = 0.0;
    std::string hwdec_current;
    std::string last_error;
    std::string current_url;
};

struct OMNIDESK_API DanmakuCue {
    double time_sec = 0.0;
    int mode = 1;           ///< 1..3: scroll, 4: bottom, 5: top
    uint32_t color = 0xFFFFFF;
    std::string text;
};

/// Real-time NanoVG danmaku overlay rendered synchronously onto the video stage.
class OMNIDESK_API DanmakuOverlay {
public:
    void setCues(std::vector<DanmakuCue> cues);
    void clear();
    bool empty() const;
    size_t cueCount() const;

    void setVisible(bool visible) { visible_ = visible; }
    bool isVisible() const { return visible_; }
    void setOpacity(float opacity) { opacity_ = opacity; }
    float opacity() const { return opacity_; }

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              double playback_sec,
              float alpha = 1.0f) const;

private:
    std::vector<DanmakuCue> cues_;
    bool visible_ = true;
    float opacity_ = 0.86f;
};

/// Bilingual / dual-line subtitle renderer overlaid onto the OpenGL video stage via NanoVG.
class OMNIDESK_API SubtitleOverlay {
public:
    void setLines(std::vector<LyricLine> lines);
    void clear();
    bool empty() const;
    size_t lineCount() const;

    void setVisible(bool visible) { visible_ = visible; }
    bool isVisible() const { return visible_; }

    /// Finds the subtitle cue covering playback_sec (using time_ms/end_ms or next cue heuristic).
    std::optional<LyricLine> activeLine(double playback_sec) const;

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              double playback_sec,
              float alpha = 1.0f) const;

private:
    std::vector<LyricLine> lines_;
    bool visible_ = true;
};

/// Thread-safe, lazy-initialized libmpv + OpenGL FBO hardware-decode playback engine.
/// Headless safe: construction does not touch GL or audio until ensureInitialized() runs inside an active GL context.
class OMNIDESK_API MPVCore {
public:
    using StateCallback = std::function<void(const MpvPlaybackState& state)>;

    static MPVCore& instance();

    MPVCore();
    ~MPVCore();

    MPVCore(const MPVCore&) = delete;
    MPVCore& operator=(const MPVCore&) = delete;

    /// Initializes libmpv and OpenGL FBO/render context if an OpenGL context is current.
    bool ensureInitialized();
    bool isInitialized() const;
    void shutdown();

    /// Loads media using isolated per-file loadfile options (no cross-stream header leakage).
    bool load(const MediaRequestOptions& opts);
    void play();
    void pause();
    void togglePause();
    void stop();
    void seekAbsolute(double seconds);
    void seekRelative(double delta_seconds);
    void setVolume(int volume);
    void setSpeed(double speed);
    void setHwdec(bool enabled);

    void setChapters(std::vector<ChapterSegment> chapters);
    std::vector<ChapterSegment> chapters() const;
    bool addSubtitleFile(const std::string& path);
    std::optional<ChapterSegment> activeAdSegment(double position_sec) const;
    bool skipActiveAdSegment();

    MpvPlaybackState state() const;
    uint64_t subscribeState(StateCallback cb);
    void unsubscribeState(uint64_t token);

    /// Renders decoded video FBO cleanly through NanoVG at the given layout rect.
    void drawVideo(NVGcontext* vg, float x, float y, float width, float height, float alpha = 1.0f);

    /// Pure utility helpers exposed for verification and plugin diagnostics.
    static std::string buildLoadfileOptions(const MediaRequestOptions& opts);
    static std::string redactSensitiveLog(const std::string& text);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

/// Drop-in Borealis widget combining OpenGL FBO video stage, bilingual SubtitleOverlay,
/// SponsorBlock/chapter progress bar, F9 ad-skip banner, and transport controls.
class OMNIDESK_API MpvStageView : public brls::Box {
public:
    explicit MpvStageView(MPVCore* core = nullptr);
    ~MpvStageView() override;

    void loadMedia(const MediaRequestOptions& opts,
                   std::vector<LyricLine> subtitles = {},
                   std::vector<ChapterSegment> chapters = {},
                   const std::string& title = "");
    void setSubtitles(std::vector<LyricLine> subtitles);
    void setDanmaku(std::vector<DanmakuCue> cues);
    void setChapters(std::vector<ChapterSegment> chapters);
    void setTitle(const std::string& title);
    void setStageMinHeight(float height);
    void setFullscreenCallback(std::function<void()> on_fullscreen);
    void setFullscreenLabel(bool fullscreen_active);
    void showMediaStatsDialog();
    bool skipSponsorIfActive();
    void stopMedia();

    MPVCore& core() const { return *core_; }
    SubtitleOverlay& subtitles() { return subtitles_; }
    DanmakuOverlay& danmaku() { return danmaku_; }

private:
    class VideoCanvas;
    class ChapterProgressBar;

    void refreshHudState(const MpvPlaybackState& st);

    MPVCore* core_ = nullptr;
    SubtitleOverlay subtitles_;
    DanmakuOverlay danmaku_;
    uint64_t state_sub_ = 0;
    std::shared_ptr<std::atomic<bool>> alive_;

    VideoCanvas* canvas_ = nullptr;
    ChapterProgressBar* progress_bar_ = nullptr;
    brls::View* ad_banner_ = nullptr;
    brls::View* status_label_ = nullptr;
    brls::View* time_label_ = nullptr;
    brls::View* play_btn_ = nullptr;
    brls::View* sub_btn_ = nullptr;
    brls::View* dm_btn_ = nullptr;
    brls::View* speed_btn_ = nullptr;
    brls::View* fs_btn_ = nullptr;
    std::function<void()> on_fullscreen_;
    bool has_loaded_ = false;
};

}  // namespace omnidesk
