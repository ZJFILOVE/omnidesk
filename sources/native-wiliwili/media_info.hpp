#pragma once
#include <borealis.hpp>
#include "view/mpv_core.hpp"
#include "utils/release.hpp"
#ifdef __SDL2__
#include <borealis/platforms/sdl/sdl_video.hpp>
#endif

class NativeMediaInfo : public brls::Box {
    brls::Label* label;
    std::string last;
    int refreshHz = 0;
public:
    NativeMediaInfo(): brls::Box(brls::Axis::COLUMN) {
        setWidth(570); setPadding(24);
        label = new brls::Label(); label->setFontSize(19); addView(label);
#ifdef __SDL2__
        // Query once when opened; do not ask the window server on every draw.
        if (auto* video = dynamic_cast<brls::SDLVideoContext*>(brls::Application::getPlatform()->getVideoContext())) {
            if (auto* window = video->getSDLWindow()) {
                const int display = SDL_GetWindowDisplayIndex(window);
                SDL_DisplayMode mode{};
                if (display >= 0 && SDL_GetCurrentDisplayMode(display, &mode) == 0 && mode.refresh_rate > 0)
                    refreshHz = mode.refresh_rate;
            }
        }
#endif
    }
    void draw(NVGcontext* vg, float x, float y, float width, float height, brls::Style style, brls::FrameContext* ctx) override {
        const auto& player = MPVCore::instance();
        const auto& m = player.mediaDiagnostics;
        const double speed = player.getSpeed(), effectiveFps = m.fps * speed;
        const auto cadence = m.fps > 0 ? fmt::format("{:.2f} fps", effectiveFps) : std::string("暂无数据");
        const auto refresh = refreshHz > 0 ? fmt::format("{} Hz", refreshHz) : std::string("暂无数据");
        const std::string timingHint = refreshHz > 0 && effectiveFps > refreshHz
            ? "\n倍速对应帧率超过屏幕刷新率，部分输出帧会被跳过。" : "";
        auto rate = [](double n){ return n > 0 ? fmt::format("{:.2f} Mbps", n / 1000000.) : std::string("暂无数据"); };
        const auto decoder = m.hardwareDecoder.empty() ? "等待解码" :
                             m.hardwareDecoder == "no" ? "CPU（软件解码）" : m.hardwareDecoder;
        std::string text = fmt::format("实际媒体信息\n\n编码分辨率：{} × {}\n原始帧率：{:.3f} fps\n播放速度：{:.2f}× · 对应 {}\n屏幕刷新率：{}\n视频编码：{}\n解码方式：{}\n丢帧：解码 {} / 输出 {}{}\n音频编码：{}\n音轨：{} · {} Hz\n时长：{:.3f} 秒\n视频码率：{}\n音频码率：{}\n来源：{}\n\n{}\n码率优先来自流元数据；播放器提供实时码率时更新。",
            m.width, m.height, m.fps, speed, cadence, refresh, m.videoCodec.empty() ? "等待解码" : m.videoCodec,
            decoder, m.decoderDroppedFrames, m.droppedFrames, timingHint,
            m.audioCodec.empty() ? "等待音轨" : m.audioCodec, m.audioChannels, m.audioRate, m.duration,
            rate(m.videoBitrate), rate(m.audioBitrate), m.source, NativeRelease::version);
        if (last != text) { last = text; label->setText(text); }
        brls::Box::draw(vg, x, y, width, height, style, ctx);
    }
    static void open() {
        auto* dialog = new brls::Dialog(new NativeMediaInfo());
        dialog->addButton("关闭", []{}); dialog->open();
    }
};
