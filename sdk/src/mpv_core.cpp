#include "omnidesk/mpv_core.hpp"
#include "omnidesk/ui_theme.hpp"

#include <glad/glad.h>
#include <SDL2/SDL.h>
#include <mpv/client.h>
#include <mpv/render.h>
#include <mpv/render_gl.h>

#define NANOVG_GL3 1
#include <nanovg_gl.h>

#include <borealis/core/application.hpp>
#include <borealis/core/logger.hpp>
#include <borealis/core/thread.hpp>
#include <borealis/core/touch/tap_gesture.hpp>
#include <borealis/views/button.hpp>
#include <borealis/views/label.hpp>

#include <algorithm>
#include <atomic>
#include <clocale>
#include <cmath>
#include <cstdio>
#include <map>
#include <mutex>
#include <sstream>

namespace omnidesk {

namespace {

std::string stripNewlines(std::string s) {
    s.erase(std::remove(s.begin(), s.end(), '\r'), s.end());
    s.erase(std::remove(s.begin(), s.end(), '\n'), s.end());
    return s;
}

std::string escapeMpvQuoted(const std::string& raw) {
    std::string clean = stripNewlines(raw);
    std::string out;
    out.reserve(clean.size() + 4);
    for (char c : clean) {
        if (c == '\\' || c == '"') {
            out.push_back('\\');
        }
        out.push_back(c);
    }
    return out;
}

std::string escapeMpvListItem(const std::string& raw) {
    std::string clean = stripNewlines(raw);
    std::string out;
    out.reserve(clean.size() + 4);
    for (char c : clean) {
        if (c == '\\' || c == '"' || c == ',') {
            out.push_back('\\');
        }
        out.push_back(c);
    }
    return out;
}

bool iequals(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); ++i) {
        if (std::tolower(static_cast<unsigned char>(a[i])) !=
            std::tolower(static_cast<unsigned char>(b[i]))) {
            return false;
        }
    }
    return true;
}

bool isSponsorOrAd(const std::string& kind) {
    return kind == "ad" || kind == "sponsor" || kind == "intro" || kind == "outro";
}

std::string formatMmSs(double sec) {
    if (sec < 0 || !std::isfinite(sec)) sec = 0;
    const int64_t total = static_cast<int64_t>(sec);
    const int64_t hrs = total / 3600;
    const int64_t mins = (total % 3600) / 60;
    const int64_t secs = total % 60;
    char buf[32];
    if (hrs > 0) {
        std::snprintf(buf, sizeof(buf), "%lld:%02lld:%02lld",
                      static_cast<long long>(hrs),
                      static_cast<long long>(mins),
                      static_cast<long long>(secs));
    } else {
        std::snprintf(buf, sizeof(buf), "%02lld:%02lld",
                      static_cast<long long>(mins),
                      static_cast<long long>(secs));
    }
    return buf;
}

void* sdlGetProcAddress(void* /*ctx*/, const char* name) {
    if (!SDL_GL_GetCurrentContext()) return nullptr;
    return reinterpret_cast<void*>(SDL_GL_GetProcAddress(name));
}

}  // namespace

// ---- SubtitleOverlay ----

void SubtitleOverlay::setLines(std::vector<LyricLine> lines) {
    std::sort(lines.begin(), lines.end(), [](const LyricLine& a, const LyricLine& b) {
        return a.time_sec < b.time_sec;
    });
    lines_ = std::move(lines);
}

void SubtitleOverlay::clear() {
    lines_.clear();
}

bool SubtitleOverlay::empty() const {
    return lines_.empty();
}

size_t SubtitleOverlay::lineCount() const {
    return lines_.size();
}

std::optional<LyricLine> SubtitleOverlay::activeLine(double playback_sec) const {
    if (lines_.empty()) return std::nullopt;
    for (size_t i = 0; i < lines_.size(); ++i) {
        const auto& line = lines_[i];
        const double end_sec =
            (i + 1 < lines_.size()) ? lines_[i + 1].time_sec : (line.time_sec + 5.0);
        if (playback_sec >= line.time_sec && playback_sec < end_sec) {
            return line;
        }
    }
    return std::nullopt;
}

void SubtitleOverlay::draw(NVGcontext* vg,
                           float x,
                           float y,
                           float width,
                           float height,
                           double playback_sec,
                           float alpha) const {
    if (!vg || !visible_ || lines_.empty() || width <= 40.0f || height <= 40.0f) return;
    auto cue = activeLine(playback_sec);
    if (!cue || (cue->original.empty() && cue->translation.empty())) return;

    const bool has_primary = !cue->original.empty();
    const bool has_trans = !cue->translation.empty();
    const float font_main = std::clamp(height * 0.042f, 14.0f, 24.0f);
    const float font_sub = std::clamp(font_main * 0.85f, 12.0f, 20.0f);
    const float line_gap = (has_primary && has_trans) ? 4.0f : 0.0f;
    const float pad_x = 18.0f;
    const float pad_y = 9.0f;
    const float max_text_w = std::max(80.0f, width - 48.0f);

    nvgSave(vg);
    int font = brls::Application::getDefaultFont();
    if (font >= 0) nvgFontFaceId(vg, font);

    float w1 = 0.0f, w2 = 0.0f;
    float bounds[4] = {0, 0, 0, 0};
    if (has_primary) {
        nvgFontSize(vg, font_main);
        nvgTextBoxBounds(vg, 0, 0, max_text_w, cue->original.c_str(), nullptr, bounds);
        w1 = bounds[2] - bounds[0];
    }
    if (has_trans) {
        nvgFontSize(vg, font_sub);
        nvgTextBoxBounds(vg, 0, 0, max_text_w, cue->translation.c_str(), nullptr, bounds);
        w2 = bounds[2] - bounds[0];
    }

    const float content_w = std::min(max_text_w, std::max(w1, w2));
    const float box_w = content_w + pad_x * 2.0f;
    const float text_h = (has_primary ? font_main * 1.2f : 0.0f) +
                         line_gap +
                         (has_trans ? font_sub * 1.2f : 0.0f);
    const float box_h = text_h + pad_y * 2.0f;
    const float box_x = x + (width - box_w) * 0.5f;
    const float box_y = y + height - box_h - 18.0f;

    nvgBeginPath(vg);
    nvgRoundedRect(vg, box_x, box_y, box_w, box_h, 8.0f);
    nvgFillColor(vg, nvgRGBA(16, 15, 18, static_cast<unsigned char>(188 * alpha)));
    nvgFill(vg);

    nvgTextAlign(vg, NVG_ALIGN_CENTER | NVG_ALIGN_TOP);
    float cur_y = box_y + pad_y;
    if (has_primary) {
        nvgFontSize(vg, font_main);
        nvgFillColor(vg, nvgRGBA(250, 250, 249, static_cast<unsigned char>(250 * alpha)));
        nvgTextBox(vg, box_x + pad_x, cur_y, content_w, cue->original.c_str(), nullptr);
        cur_y += font_main * 1.2f + line_gap;
    }
    if (has_trans) {
        nvgFontSize(vg, font_sub);
        nvgFillColor(vg, nvgRGBA(245, 198, 92, static_cast<unsigned char>(242 * alpha)));
        nvgTextBox(vg, box_x + pad_x, cur_y, content_w, cue->translation.c_str(), nullptr);
    }
    nvgRestore(vg);
}

// ---- MPVCore ----

struct MPVCore::Impl {
    MPVCore* owner = nullptr;
    mpv_handle* mpv = nullptr;
    mpv_render_context* render_ctx = nullptr;

    GLuint fbo = 0;
    GLuint fbo_tex = 0;
    int fbo_w = 0;
    int fbo_h = 0;
    int nvg_image = 0;
    NVGcontext* nvg_ctx = nullptr;
    int flip_y = 0;  // 0 matches NanoVG top-left origin cleanly inside FBO
    mpv_opengl_fbo mpv_fbo{0, 1280, 720, 0};
    mpv_render_param render_params[3] = {
        {MPV_RENDER_PARAM_OPENGL_FBO, nullptr},
        {MPV_RENDER_PARAM_FLIP_Y, nullptr},
        {MPV_RENDER_PARAM_INVALID, nullptr},
    };

    mutable std::mutex mutex;
    MpvPlaybackState state;
    std::vector<ChapterSegment> chapters;
    std::optional<MediaRequestOptions> pending_load;
    std::unordered_map<uint64_t, StateCallback> subscribers;
    uint64_t next_sub_token = 1;
    std::shared_ptr<std::atomic<bool>> alive = std::make_shared<std::atomic<bool>>(true);
    std::atomic<bool> dirty_frame{false};
    int exit_sub = -1;

    static void onMpvWakeup(void* userdata) {
        auto* self = static_cast<Impl*>(userdata);
        if (!self) return;
        auto alive = self->alive;
        brls::sync([self, alive]() {
            if (!alive->load()) return;
            self->drainEvents();
        });
    }

    static void onMpvRenderUpdate(void* userdata) {
        auto* self = static_cast<Impl*>(userdata);
        if (!self) return;
        self->dirty_frame.store(true, std::memory_order_release);
        auto alive = self->alive;
        brls::sync([self, alive]() {
            if (!alive->load()) return;
            self->renderDirtyFbo();
        });
    }

    void commandAsync(const std::vector<std::string>& args) {
        std::lock_guard lock(mutex);
        if (!mpv || args.empty()) return;
        std::vector<const char*> cargs;
        cargs.reserve(args.size() + 1);
        for (const auto& s : args) cargs.push_back(s.c_str());
        cargs.push_back(nullptr);
        mpv_command_async(mpv, 0, cargs.data());
    }

    void emitStateLocked(const MpvPlaybackState& st,
                         const std::vector<StateCallback>& cbs) const {
        for (const auto& cb : cbs) {
            if (cb) cb(st);
        }
    }

    void notifyState() {
        MpvPlaybackState snap;
        std::vector<StateCallback> cbs;
        {
            std::lock_guard lock(mutex);
            snap = state;
            for (const auto& [_, cb] : subscribers) cbs.push_back(cb);
        }
        emitStateLocked(snap, cbs);
    }

    void ensureFboSize(int w, int h, NVGcontext* vg) {
        if (w <= 0 || h <= 0) return;
        if (fbo != 0 && fbo_w == w && fbo_h == h && nvg_ctx == vg) return;

        GLint prev_fbo = 0;
        glGetIntegerv(GL_FRAMEBUFFER_BINDING, &prev_fbo);

        if (nvg_image != 0 && nvg_ctx != nullptr) {
            nvgDeleteImage(nvg_ctx, nvg_image);
            nvg_image = 0;
        }
        if (fbo_tex == 0) {
            glGenTextures(1, &fbo_tex);
            glBindTexture(GL_TEXTURE_2D, fbo_tex);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        } else {
            glBindTexture(GL_TEXTURE_2D, fbo_tex);
        }
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);

        if (fbo == 0) {
            glGenFramebuffers(1, &fbo);
        }
        glBindFramebuffer(GL_FRAMEBUFFER, fbo);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, fbo_tex, 0);

        glBindTexture(GL_TEXTURE_2D, 0);
        glBindFramebuffer(GL_FRAMEBUFFER, static_cast<GLuint>(prev_fbo));

        fbo_w = w;
        fbo_h = h;
        mpv_fbo.fbo = static_cast<int>(fbo);
        mpv_fbo.w = w;
        mpv_fbo.h = h;

        if (vg != nullptr) {
            nvg_ctx = vg;
            nvg_image = nvglCreateImageFromHandleGL3(
                vg, fbo_tex, w, h, NVG_IMAGE_NODELETE);
        }

        if (render_ctx != nullptr) {
            GLint prev_vp[4] = {0, 0, 0, 0};
            glGetIntegerv(GL_VIEWPORT, prev_vp);
            mpv_render_context_render(render_ctx, render_params);
            glBindFramebuffer(GL_FRAMEBUFFER, static_cast<GLuint>(prev_fbo));
            glViewport(prev_vp[0], prev_vp[1], prev_vp[2], prev_vp[3]);
            mpv_render_context_report_swap(render_ctx);
        }
    }

    void renderDirtyFbo() {
        if (!render_ctx || fbo == 0 || fbo_w <= 0 || fbo_h <= 0) return;
        if (!SDL_GL_GetCurrentContext()) return;
        const uint64_t flags = mpv_render_context_update(render_ctx);
        if ((flags & MPV_RENDER_UPDATE_FRAME) == 0 &&
            !dirty_frame.exchange(false, std::memory_order_acq_rel)) {
            return;
        }
        GLint prev_fbo = 0;
        GLint prev_vp[4] = {0, 0, 0, 0};
        glGetIntegerv(GL_FRAMEBUFFER_BINDING, &prev_fbo);
        glGetIntegerv(GL_VIEWPORT, prev_vp);

        mpv_render_context_render(render_ctx, render_params);

        glBindFramebuffer(GL_FRAMEBUFFER, static_cast<GLuint>(prev_fbo));
        glViewport(prev_vp[0], prev_vp[1], prev_vp[2], prev_vp[3]);
        mpv_render_context_report_swap(render_ctx);
    }

    void drainEvents() {
        bool state_changed = false;
        while (true) {
            mpv_event* ev = nullptr;
            {
                std::lock_guard lock(mutex);
                if (!mpv) break;
                ev = mpv_wait_event(mpv, 0);
            }
            if (!ev || ev->event_id == MPV_EVENT_NONE) break;

            switch (ev->event_id) {
                case MPV_EVENT_LOG_MESSAGE: {
                    auto* msg = static_cast<mpv_event_log_message*>(ev->data);
                    if (msg && msg->log_level <= MPV_LOG_LEVEL_ERROR) {
                        brls::Logger::error("[mpv:{}] {}",
                                            msg->prefix ? msg->prefix : "core",
                                            MPVCore::redactSensitiveLog(msg->text ? msg->text : ""));
                    }
                    break;
                }
                case MPV_EVENT_END_FILE: {
                    auto* ef = static_cast<mpv_event_end_file*>(ev->data);
                    std::lock_guard lock(mutex);
                    if (ef && ef->reason == MPV_END_FILE_REASON_ERROR) {
                        state.last_error = mpv_error_string(ef->error);
                    }
                    state.playing = false;
                    state.stopped = true;
                    state_changed = true;
                    break;
                }
                case MPV_EVENT_PLAYBACK_RESTART: {
                    std::lock_guard lock(mutex);
                    state.last_error.clear();
                    state.stopped = false;
                    state.playing = !state.paused;
                    state_changed = true;
                    break;
                }
                case MPV_EVENT_PROPERTY_CHANGE: {
                    auto* prop = static_cast<mpv_event_property*>(ev->data);
                    if (!prop || !prop->data) break;
                    std::lock_guard lock(mutex);
                    state_changed = true;
                    switch (ev->reply_userdata) {
                        case 1: state.position_sec = *static_cast<double*>(prop->data); break;
                        case 2: state.duration_sec = *static_cast<double*>(prop->data); break;
                        case 3:
                            state.paused = (*static_cast<int*>(prop->data) != 0);
                            if (!state.stopped) state.playing = !state.paused;
                            break;
                        case 4:
                            state.stopped = (*static_cast<int*>(prop->data) != 0);
                            if (state.stopped) state.playing = false;
                            break;
                        case 5:
                            state.buffering = (*static_cast<int*>(prop->data) != 0);
                            break;
                        case 6: state.speed = *static_cast<double*>(prop->data); break;
                        case 7: state.volume = static_cast<int>(*static_cast<int64_t*>(prop->data)); break;
                        case 8: state.video_width = static_cast<int>(*static_cast<int64_t*>(prop->data)); break;
                        case 9: state.video_height = static_cast<int>(*static_cast<int64_t*>(prop->data)); break;
                        case 10: {
                            char* s = *static_cast<char**>(prop->data);
                            state.hwdec_current = s ? s : "";
                            break;
                        }
                        default: break;
                    }
                    break;
                }
                default: break;
            }
        }
        if (state_changed) notifyState();
    }

    void destroyLocked() {
        if (render_ctx) {
            mpv_render_context_set_update_callback(render_ctx, nullptr, nullptr);
            mpv_render_context_free(render_ctx);
            render_ctx = nullptr;
        }
        if (mpv) {
            mpv_set_wakeup_callback(mpv, nullptr, nullptr);
            mpv_terminate_destroy(mpv);
            mpv = nullptr;
        }
        if (SDL_GL_GetCurrentContext()) {
            if (nvg_image != 0 && nvg_ctx != nullptr) {
                nvgDeleteImage(nvg_ctx, nvg_image);
            }
            if (fbo != 0) glDeleteFramebuffers(1, &fbo);
            if (fbo_tex != 0) glDeleteTextures(1, &fbo_tex);
        }
        nvg_image = 0;
        nvg_ctx = nullptr;
        fbo = 0;
        fbo_tex = 0;
        fbo_w = 0;
        fbo_h = 0;
        state.initialized = false;
        state.playing = false;
        state.stopped = true;
    }
};

MPVCore& MPVCore::instance() {
    static MPVCore singleton;
    return singleton;
}

MPVCore::MPVCore() : impl_(std::make_unique<Impl>()) {
    impl_->owner = this;
    impl_->render_params[0].data = &impl_->mpv_fbo;
    impl_->render_params[1].data = &impl_->flip_y;
}

MPVCore::~MPVCore() {
    shutdown();
}

bool MPVCore::ensureInitialized() {
    std::optional<MediaRequestOptions> pending;
    {
        std::lock_guard lock(impl_->mutex);
        if (impl_->state.initialized && impl_->mpv && impl_->render_ctx) {
            return true;
        }
        if (!SDL_GL_GetCurrentContext()) {
            return false;
        }

        std::setlocale(LC_NUMERIC, "C");
        mpv_handle* handle = mpv_create();
        if (!handle) {
            impl_->state.last_error = "mpv_create failed";
            return false;
        }

        mpv_set_option_string(handle, "vo", "libmpv");
        mpv_set_option_string(handle, "hwdec", "auto-safe");
        mpv_set_option_string(handle, "idle", "yes");
        mpv_set_option_string(handle, "keep-open", "yes");
        mpv_set_option_string(handle, "hr-seek", "yes");
        mpv_set_option_string(handle, "ytdl", "no");
        mpv_set_option_string(handle, "audio-channels", "stereo");
        mpv_set_option_string(handle, "reset-on-next-file", "speed,pause,http-header-fields,referrer,user-agent");
        mpv_set_option_string(handle, "terminal", "no");
        mpv_request_log_messages(handle, "error");

        if (mpv_initialize(handle) < 0) {
            mpv_terminate_destroy(handle);
            impl_->state.last_error = "mpv_initialize failed";
            return false;
        }

        mpv_observe_property(handle, 1, "playback-time", MPV_FORMAT_DOUBLE);
        mpv_observe_property(handle, 2, "duration", MPV_FORMAT_DOUBLE);
        mpv_observe_property(handle, 3, "pause", MPV_FORMAT_FLAG);
        mpv_observe_property(handle, 4, "playback-abort", MPV_FORMAT_FLAG);
        mpv_observe_property(handle, 5, "paused-for-cache", MPV_FORMAT_FLAG);
        mpv_observe_property(handle, 6, "speed", MPV_FORMAT_DOUBLE);
        mpv_observe_property(handle, 7, "volume", MPV_FORMAT_INT64);
        mpv_observe_property(handle, 8, "video-params/w", MPV_FORMAT_INT64);
        mpv_observe_property(handle, 9, "video-params/h", MPV_FORMAT_INT64);
        mpv_observe_property(handle, 10, "hwdec-current", MPV_FORMAT_STRING);

        int advanced_control = 1;
        mpv_opengl_init_params gl_init{sdlGetProcAddress, nullptr};
        mpv_render_param params[] = {
            {MPV_RENDER_PARAM_API_TYPE, const_cast<char*>(MPV_RENDER_API_TYPE_OPENGL)},
            {MPV_RENDER_PARAM_OPENGL_INIT_PARAMS, &gl_init},
            {MPV_RENDER_PARAM_ADVANCED_CONTROL, &advanced_control},
            {MPV_RENDER_PARAM_INVALID, nullptr},
        };
        mpv_render_context* rctx = nullptr;
        if (mpv_render_context_create(&rctx, handle, params) < 0) {
            mpv_terminate_destroy(handle);
            impl_->state.last_error = "mpv_render_context_create failed";
            return false;
        }

        impl_->mpv = handle;
        impl_->render_ctx = rctx;
        impl_->state.initialized = true;
        impl_->state.last_error.clear();

        mpv_set_wakeup_callback(handle, Impl::onMpvWakeup, impl_.get());
        mpv_render_context_set_update_callback(rctx, Impl::onMpvRenderUpdate, impl_.get());

        int64_t vol = impl_->state.volume;
        mpv_set_option(handle, "volume", MPV_FORMAT_INT64, &vol);

        if (impl_->pending_load) {
            pending = std::move(impl_->pending_load);
            impl_->pending_load.reset();
        }
    }

    if (pending) {
        load(*pending);
    }
    impl_->notifyState();
    return true;
}

bool MPVCore::isInitialized() const {
    std::lock_guard lock(impl_->mutex);
    return impl_->state.initialized;
}

void MPVCore::shutdown() {
    impl_->alive->store(false);
    std::lock_guard lock(impl_->mutex);
    impl_->destroyLocked();
}

std::string MPVCore::buildLoadfileOptions(const MediaRequestOptions& opts) {
    std::string referrer = opts.referrer;
    std::string user_agent = opts.user_agent;
    std::map<std::string, std::string> filtered_headers;

    for (const auto& [k, v] : opts.http_headers) {
        if (k.empty()) continue;
        if (iequals(k, "Referer") || iequals(k, "Referrer")) {
            if (referrer.empty()) referrer = v;
        } else if (iequals(k, "User-Agent")) {
            if (user_agent.empty()) user_agent = v;
        } else {
            filtered_headers[stripNewlines(k)] = stripNewlines(v);
        }
    }

    // Explicitly reset per-stream header fields on every loadfile call so Bilibili web Referer
    // or cookies never leak into Android 4K or NAS streams.
    std::vector<std::string> parts;
    parts.push_back("referrer=\"" + escapeMpvQuoted(referrer) + "\"");
    if (!user_agent.empty()) {
        parts.push_back("user-agent=\"" + escapeMpvQuoted(user_agent) + "\"");
    } else {
        parts.push_back("user-agent=\"OmniDesk/1.0\"");
    }

    if (filtered_headers.empty()) {
        parts.push_back("http-header-fields=\"\"");
    } else {
        std::string joined;
        bool first = true;
        for (const auto& [hk, hv] : filtered_headers) {
            if (!first) joined += ",";
            first = false;
            joined += escapeMpvListItem(hk + ": " + hv);
        }
        parts.push_back("http-header-fields=\"" + escapeMpvQuoted(joined) + "\"");
    }

    parts.push_back("network-timeout=10");
    parts.push_back(std::string("hwdec=") + (opts.hwdec ? "\"auto-safe\"" : "\"no\""));
    if (opts.start_seconds > 0.0) {
        std::ostringstream ss;
        ss << "start=" << opts.start_seconds;
        parts.push_back(ss.str());
    }
    if (opts.end_seconds > 0.0) {
        std::ostringstream ss;
        ss << "end=" << opts.end_seconds;
        parts.push_back(ss.str());
    }
    if (!opts.audio_url.empty()) {
        parts.push_back("audio-file=\"" + escapeMpvQuoted(opts.audio_url) + "\"");
    }
    if (!opts.subtitle_urls.empty()) {
        std::string subs;
        for (size_t i = 0; i < opts.subtitle_urls.size(); ++i) {
            if (i > 0) subs += ",";
            subs += escapeMpvListItem(opts.subtitle_urls[i]);
        }
        parts.push_back("sub-files=\"" + escapeMpvQuoted(subs) + "\"");
    }

    std::string result;
    for (size_t i = 0; i < parts.size(); ++i) {
        if (i > 0) result += ",";
        result += parts[i];
    }
    return result;
}

std::string MPVCore::redactSensitiveLog(const std::string& text) {
    if (text.empty()) return text;
    std::string out = text;
    for (const char* marker : {"Cookie:", "Authorization:", "cookie", "SESSDATA", "auth_key", "token="}) {
        auto pos = out.find(marker);
        if (pos != std::string::npos) {
            return out.substr(0, pos + std::char_traits<char>::length(marker)) + "<redacted>";
        }
    }
    auto scheme = out.find("://");
    if (scheme != std::string::npos) {
        auto q = out.find('?', scheme);
        if (q != std::string::npos) {
            return out.substr(0, q) + "?<redacted>";
        }
    }
    return out;
}

bool MPVCore::load(const MediaRequestOptions& opts) {
    if (opts.url.empty()) return false;
    {
        std::lock_guard lock(impl_->mutex);
        impl_->state.current_url = redactSensitiveLog(opts.url);
        impl_->state.last_error.clear();
        impl_->state.position_sec = opts.start_seconds > 0 ? opts.start_seconds : 0.0;
        impl_->state.duration_sec = 0.0;
        impl_->state.video_width = 0;
        impl_->state.video_height = 0;
        impl_->state.stopped = false;
        impl_->state.paused = false;
        impl_->state.playing = true;
        if (!impl_->state.initialized || !impl_->mpv) {
            impl_->pending_load = opts;
        }
    }
    impl_->notifyState();

    if (!isInitialized() && !ensureInitialized()) {
        // Queued in pending_load_ cleanly; will start as soon as OpenGL surface draws.
        return true;
    }

    const std::string per_stream_opts = buildLoadfileOptions(opts);
    brls::Logger::info("MPVCore loading stream: {}", redactSensitiveLog(opts.url));
    if (mpv_client_api_version() >= MPV_MAKE_VERSION(2, 3)) {
        impl_->commandAsync({"loadfile", opts.url, "replace", "0", per_stream_opts});
    } else {
        impl_->commandAsync({"loadfile", opts.url, "replace", per_stream_opts});
    }
    return true;
}

void MPVCore::play() {
    {
        std::lock_guard lock(impl_->mutex);
        impl_->state.paused = false;
        if (!impl_->state.stopped) impl_->state.playing = true;
    }
    impl_->commandAsync({"set", "pause", "no"});
    impl_->notifyState();
}

void MPVCore::pause() {
    {
        std::lock_guard lock(impl_->mutex);
        impl_->state.paused = true;
        impl_->state.playing = false;
    }
    impl_->commandAsync({"set", "pause", "yes"});
    impl_->notifyState();
}

void MPVCore::togglePause() {
    bool currently_paused = true;
    {
        std::lock_guard lock(impl_->mutex);
        currently_paused = impl_->state.paused;
    }
    if (currently_paused) play();
    else pause();
}

void MPVCore::stop() {
    {
        std::lock_guard lock(impl_->mutex);
        impl_->pending_load.reset();
        impl_->state.playing = false;
        impl_->state.paused = true;
        impl_->state.stopped = true;
        impl_->state.position_sec = 0.0;
    }
    impl_->commandAsync({"stop"});
    impl_->notifyState();
}

void MPVCore::seekAbsolute(double seconds) {
    if (seconds < 0.0) seconds = 0.0;
    {
        std::lock_guard lock(impl_->mutex);
        impl_->state.position_sec = seconds;
    }
    impl_->commandAsync({"seek", std::to_string(seconds), "absolute"});
    impl_->notifyState();
}

void MPVCore::seekRelative(double delta_seconds) {
    impl_->commandAsync({"seek", std::to_string(delta_seconds), "relative"});
}

void MPVCore::setVolume(int volume) {
    volume = std::clamp(volume, 0, 100);
    {
        std::lock_guard lock(impl_->mutex);
        impl_->state.volume = volume;
    }
    impl_->commandAsync({"set", "volume", std::to_string(volume)});
    impl_->notifyState();
}

void MPVCore::setSpeed(double speed) {
    speed = std::clamp(speed, 0.25, 4.0);
    {
        std::lock_guard lock(impl_->mutex);
        impl_->state.speed = speed;
    }
    impl_->commandAsync({"set", "speed", std::to_string(speed)});
    impl_->notifyState();
}

void MPVCore::setHwdec(bool enabled) {
    impl_->commandAsync({"set", "hwdec", enabled ? "auto-safe" : "no"});
}

void MPVCore::setChapters(std::vector<ChapterSegment> chapters) {
    std::sort(chapters.begin(), chapters.end(), [](const ChapterSegment& a, const ChapterSegment& b) {
        return a.start_sec < b.start_sec;
    });
    std::lock_guard lock(impl_->mutex);
    impl_->chapters = std::move(chapters);
}

std::vector<ChapterSegment> MPVCore::chapters() const {
    std::lock_guard lock(impl_->mutex);
    return impl_->chapters;
}

std::optional<ChapterSegment> MPVCore::activeAdSegment(double position_sec) const {
    std::lock_guard lock(impl_->mutex);
    for (const auto& ch : impl_->chapters) {
        if (isSponsorOrAd(ch.kind) &&
            position_sec >= ch.start_sec &&
            position_sec < ch.end_sec) {
            return ch;
        }
    }
    return std::nullopt;
}

bool MPVCore::skipActiveAdSegment() {
    double pos = 0.0;
    {
        std::lock_guard lock(impl_->mutex);
        pos = impl_->state.position_sec;
    }
    auto ad = activeAdSegment(pos);
    if (!ad) return false;
    seekAbsolute(ad->end_sec + 0.05);
    return true;
}

MpvPlaybackState MPVCore::state() const {
    std::lock_guard lock(impl_->mutex);
    return impl_->state;
}

uint64_t MPVCore::subscribeState(StateCallback cb) {
    std::lock_guard lock(impl_->mutex);
    uint64_t tok = impl_->next_sub_token++;
    impl_->subscribers[tok] = std::move(cb);
    return tok;
}

void MPVCore::unsubscribeState(uint64_t token) {
    std::lock_guard lock(impl_->mutex);
    impl_->subscribers.erase(token);
}

void MPVCore::drawVideo(NVGcontext* vg, float x, float y, float width, float height, float alpha) {
    if (!vg || width <= 4.0f || height <= 4.0f) return;

    // Backing matte canvas
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x, y, width, height, 10.0f);
    nvgFillColor(vg, nvgRGBA(12, 12, 14, static_cast<unsigned char>(255 * alpha)));
    nvgFill(vg);

    if (!ensureInitialized()) {
        return;
    }

    const float scale = brls::Application::windowScale > 0 ? brls::Application::windowScale : 1.0f;
    const int draw_w = std::max(1, static_cast<int>(width * scale));
    const int draw_h = std::max(1, static_cast<int>(height * scale));
    impl_->ensureFboSize(draw_w, draw_h, vg);

    if (impl_->nvg_image <= 0) return;

    MpvPlaybackState st = state();
    if (st.stopped) return;

    nvgSave(vg);
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x, y, width, height, 10.0f);
    NVGpaint paint = nvgImagePattern(vg, x, y, width, height, 0.0f, impl_->nvg_image, alpha);
    nvgFillPaint(vg, paint);
    nvgFill(vg);
    nvgRestore(vg);
}

// ---- MpvStageView ----

class MpvStageView::VideoCanvas : public brls::View {
public:
    VideoCanvas(MPVCore* core, SubtitleOverlay* sub) : core_(core), sub_(sub) {
        setFocusable(true);
    }

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style /*style*/,
              brls::FrameContext* /*ctx*/) override {
        const float alpha = getAlpha();
        core_->drawVideo(vg, x, y, width, height, alpha);
        const auto st = core_->state();
        if (st.stopped) {
            nvgSave(vg);
            int font = brls::Application::getDefaultFont();
            if (font >= 0) nvgFontFaceId(vg, font);
            nvgFontSize(vg, 14.0f);
            nvgTextAlign(vg, NVG_ALIGN_CENTER | NVG_ALIGN_MIDDLE);
            nvgFillColor(vg, nvgRGBA(205, 203, 198, 220));
            nvgText(vg, x + width * 0.5f, y + height * 0.5f,
                    "OpenGL libmpv 硬解舞台就绪 · 点击任意媒体条目开始播放", nullptr);
            nvgRestore(vg);
        }
        sub_->draw(vg, x, y, width, height, st.position_sec, alpha);
    }

private:
    MPVCore* core_ = nullptr;
    SubtitleOverlay* sub_ = nullptr;
};

class MpvStageView::ChapterProgressBar : public brls::View {
public:
    explicit ChapterProgressBar(MPVCore* core) : core_(core) {
        setFocusable(false);
        addGestureRecognizer(new brls::TapGestureRecognizer([this](brls::TapGestureStatus status, brls::Sound*) {
            if (status.state != brls::GestureState::END) return;
            const auto st = core_->state();
            if (st.duration_sec <= 0.0) return;
            const float w = getWidth();
            if (w <= 4.0f) return;
            const float rel_x = std::clamp((status.position.x - getX()) / w, 0.0f, 1.0f);
            core_->seekAbsolute(rel_x * st.duration_sec);
        }));
    }

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style /*style*/,
              brls::FrameContext* /*ctx*/) override {
        const auto st = core_->state();
        const float bar_h = 6.0f;
        const float bar_y = y + (height - bar_h) * 0.5f;

        // Track background
        nvgBeginPath(vg);
        nvgRoundedRect(vg, x, bar_y, width, bar_h, 3.0f);
        nvgFillColor(vg, nvgRGBA(72, 70, 76, 180));
        nvgFill(vg);

        const double dur = st.duration_sec > 0.0 ? st.duration_sec : 1.0;

        // SponsorBlock / Ad chapter color blocks on progress bar
        for (const auto& ch : core_->chapters()) {
            const float start_r = static_cast<float>(std::clamp(ch.start_sec / dur, 0.0, 1.0));
            const float end_r = static_cast<float>(std::clamp(ch.end_sec / dur, 0.0, 1.0));
            const float seg_x = x + start_r * width;
            const float seg_w = std::max(3.0f, (end_r - start_r) * width);
            nvgBeginPath(vg);
            nvgRoundedRect(vg, seg_x, bar_y - 1.0f, seg_w, bar_h + 2.0f, 2.0f);
            if (isSponsorOrAd(ch.kind)) {
                nvgFillColor(vg, nvgRGBA(245, 158, 11, 230));  // Sponsor/Ad amber block
            } else {
                nvgFillColor(vg, nvgRGBA(16, 185, 129, 175));  // Normal chapter emerald marker
            }
            nvgFill(vg);
        }

        // Played progress bar
        const float prog = st.duration_sec > 0.0
                               ? static_cast<float>(std::clamp(st.position_sec / st.duration_sec, 0.0, 1.0))
                               : 0.0f;
        if (prog > 0.0f) {
            nvgBeginPath(vg);
            nvgRoundedRect(vg, x, bar_y, std::max(4.0f, width * prog), bar_h, 3.0f);
            nvgFillColor(vg, ui::color("brls/accent"));
            nvgFill(vg);

            nvgBeginPath(vg);
            nvgCircle(vg, x + width * prog, bar_y + bar_h * 0.5f, 5.5f);
            nvgFillColor(vg, nvgRGBA(250, 250, 249, 255));
            nvgFill(vg);
        }
    }

private:
    MPVCore* core_ = nullptr;
};

MpvStageView::MpvStageView(MPVCore* core)
    : brls::Box(brls::Axis::COLUMN),
      core_(core ? core : &MPVCore::instance()),
      alive_(std::make_shared<std::atomic<bool>>(true)) {
    setGrow(1.0f);
    setPadding(0);

    canvas_ = new VideoCanvas(core_, &subtitles_);
    canvas_->setGrow(1.0f);
    canvas_->setHeightPercentage(100);
    canvas_->setMinHeight(240);
    addView(canvas_);

    // SponsorBlock F9 hint pill
    auto* ad_row = ui::makeCard("status/warn_bg", 8.f, 10.f);
    ad_row->setAxis(brls::Axis::ROW);
    ad_row->setAlignItems(brls::AlignItems::CENTER);
    ad_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    ad_row->setPadding(6, 12, 6, 12);
    ad_row->setMarginTop(6);
    ad_row->setVisibility(brls::Visibility::GONE);
    auto* ad_txt = ui::makeLabel("检测到赞助/片头片段 · 点击或按 F9 跳过", 13, "status/warn_fg");
    ad_row->addView(ad_txt);
    auto* ad_btn = ui::makeButton("跳过片段", [this]() { skipSponsorIfActive(); }, false, 30.f);
    ad_row->addView(ad_btn);
    ad_banner_ = ad_row;
    addView(ad_row);

    // Interactive timeline bar with chapter/ad segments
    progress_bar_ = new ChapterProgressBar(core_);
    progress_bar_->setHeight(18);
    progress_bar_->setMarginTop(6);
    addView(progress_bar_);

    // Transport control strip
    auto* controls = ui::makeCard("desktop/soft", 8.f, 10.f);
    controls->setAxis(brls::Axis::ROW);
    controls->setAlignItems(brls::AlignItems::CENTER);
    controls->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    controls->setPadding(6, 12, 6, 12);
    controls->setMarginTop(6);

    auto* left_btns = new brls::Box(brls::Axis::ROW);
    left_btns->setAlignItems(brls::AlignItems::CENTER);

    auto* play_btn = ui::makeButton("播放", [this]() { core_->togglePause(); }, false, 30.f);
    play_btn->setMarginRight(6);
    play_btn_ = play_btn;
    left_btns->addView(play_btn);

    auto* back10 = ui::makeButton("-10s", [this]() { core_->seekRelative(-10.0); }, false, 30.f);
    back10->setMarginRight(6);
    left_btns->addView(back10);

    auto* fwd10 = ui::makeButton("+10s", [this]() { core_->seekRelative(10.0); }, false, 30.f);
    fwd10->setMarginRight(10);
    left_btns->addView(fwd10);

    auto* time_lbl = ui::makeLabel("00:00 / 00:00", 13, "font/grey");
    time_label_ = time_lbl;
    left_btns->addView(time_lbl);

    controls->addView(left_btns);

    auto* right_btns = new brls::Box(brls::Axis::ROW);
    right_btns->setAlignItems(brls::AlignItems::CENTER);

    auto* status_lbl = ui::makeLabel("libmpv FBO", 12, "font/grey");
    status_lbl->setMarginRight(10);
    status_label_ = status_lbl;
    right_btns->addView(status_lbl);

    auto* sub_btn = ui::makeButton("双语字幕: 开", [this]() {
        subtitles_.setVisible(!subtitles_.isVisible());
        refreshHudState(core_->state());
    }, false, 30.f);
    sub_btn->setMarginRight(6);
    sub_btn_ = sub_btn;
    right_btns->addView(sub_btn);

    auto* spd_btn = ui::makeButton("1.0x", [this]() {
        const double cur = core_->state().speed;
        double next = 1.0;
        if (cur < 1.2) next = 1.25;
        else if (cur < 1.45) next = 1.5;
        else if (cur < 1.9) next = 2.0;
        else next = 1.0;
        core_->setSpeed(next);
    }, false, 30.f);
    speed_btn_ = spd_btn;
    right_btns->addView(spd_btn);

    controls->addView(right_btns);
    addView(controls);

    auto alive = alive_;
    state_sub_ = core_->subscribeState([this, alive](const MpvPlaybackState& st) {
        brls::sync([this, alive, st]() {
            if (!alive->load()) return;
            refreshHudState(st);
        });
    });
}

MpvStageView::~MpvStageView() {
    alive_->store(false);
    if (state_sub_ != 0 && core_) {
        core_->unsubscribeState(state_sub_);
    }
}

void MpvStageView::loadMedia(const MediaRequestOptions& opts,
                             std::vector<LyricLine> subtitles,
                             std::vector<ChapterSegment> chapters,
                             const std::string& title) {
    subtitles_.setLines(std::move(subtitles));
    core_->setChapters(std::move(chapters));
    core_->load(opts);
    has_loaded_ = true;
    if (auto* lbl = dynamic_cast<brls::Label*>(status_label_)) {
        lbl->setText(title.empty() ? "libmpv 播放中" : title);
    }
    refreshHudState(core_->state());
}

void MpvStageView::setSubtitles(std::vector<LyricLine> subtitles) {
    subtitles_.setLines(std::move(subtitles));
}

void MpvStageView::setChapters(std::vector<ChapterSegment> chapters) {
    core_->setChapters(std::move(chapters));
    refreshHudState(core_->state());
}

bool MpvStageView::skipSponsorIfActive() {
    return core_->skipActiveAdSegment();
}

void MpvStageView::stopMedia() {
    core_->stop();
}

void MpvStageView::refreshHudState(const MpvPlaybackState& st) {
    if (auto* lbl = dynamic_cast<brls::Label*>(time_label_)) {
        lbl->setText(formatMmSs(st.position_sec) + " / " + formatMmSs(st.duration_sec));
    }
    if (auto* btn = dynamic_cast<brls::Button*>(play_btn_)) {
        btn->setText(st.paused || st.stopped ? "播放" : "暂停");
    }
    if (auto* btn = dynamic_cast<brls::Button*>(sub_btn_)) {
        btn->setText(subtitles_.isVisible() ? "双语字幕: 开" : "双语字幕: 关");
    }
    if (auto* btn = dynamic_cast<brls::Button*>(speed_btn_)) {
        char buf[16];
        std::snprintf(buf, sizeof(buf), "%.2gx", st.speed);
        btn->setText(buf);
    }
    if (auto* lbl = dynamic_cast<brls::Label*>(status_label_)) {
        if (!st.last_error.empty()) {
            lbl->setText("错误: " + st.last_error);
        } else if (st.buffering) {
            lbl->setText("缓冲中...");
        } else if (!st.hwdec_current.empty()) {
            lbl->setText("HW: " + st.hwdec_current);
        }
    }
    if (ad_banner_) {
        const auto active_ad = core_->activeAdSegment(st.position_sec);
        ad_banner_->setVisibility(active_ad ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
}

}  // namespace omnidesk
