#pragma once

#include <borealis.hpp>
#include <cstdint>
#include <string>

namespace omnidesk::host {

/**
 * Lightweight NanoVG + lunasvg icon view with per-NVGcontext RGBA texture caching.
 * Repeated workspace switches or rail rebuilds reuse existing GL textures with zero
 * XML re-parse and zero texture recreation.
 */
class SvgIconView : public brls::View {
public:
    explicit SvgIconView(const std::string& file_path = "");
    ~SvgIconView() override = default;

    void setSvgFile(const std::string& file_path);
    void setSvgString(const std::string& xml);
    void setNavIcon(const std::string& icon_kind, NVGcolor color);

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style style,
              brls::FrameContext* ctx) override;

private:
    enum class SourceKind : uint8_t {
        None = 0,
        File,
        Xml,
        NavIcon,
    };

    void resolveTexture(NVGcontext* vg, int pw, int ph);

    SourceKind source_kind_ = SourceKind::None;
    std::string source_key_;
    std::string raw_payload_;
    std::string nav_hex_color_;
    float nav_opacity_ = 1.0f;

    int nvg_tex_ = 0;
    NVGcontext* owner_vg_ = nullptr;
    int rendered_w_ = 0;
    int rendered_h_ = 0;
    bool dirty_ = false;
};

} // namespace omnidesk::host
