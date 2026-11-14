#pragma once

#include <borealis.hpp>
#include <lunasvg.h>
#include <memory>
#include <string>

namespace omnidesk::host {

/**
 * Lightweight NanoVG + lunasvg icon view without coupling to wiliwili globals.
 */
class SvgIconView : public brls::View {
public:
    explicit SvgIconView(const std::string& file_path = "");
    ~SvgIconView() override;

    void setSvgFile(const std::string& file_path);
    void setSvgString(const std::string& xml);

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style style,
              brls::FrameContext* ctx) override;

private:
    void rebuildTexture(NVGcontext* vg, int pw, int ph);
    void freeTexture();

    std::unique_ptr<lunasvg::Document> doc_;
    int nvg_tex_ = 0;
    NVGcontext* owner_vg_ = nullptr;
    int rendered_w_ = 0;
    int rendered_h_ = 0;
    bool dirty_ = false;
};

} // namespace omnidesk::host
