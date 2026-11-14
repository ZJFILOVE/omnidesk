#include "omnidesk/host/svg_view.hpp"

#include <cmath>
#include <cstring>

namespace omnidesk::host {

SvgIconView::SvgIconView(const std::string& file_path) {
    if (!file_path.empty()) {
        setSvgFile(file_path);
    }
}

SvgIconView::~SvgIconView() {
    freeTexture();
}

void SvgIconView::freeTexture() {
    if (nvg_tex_ != 0 && owner_vg_ != nullptr) {
        nvgDeleteImage(owner_vg_, nvg_tex_);
    }
    nvg_tex_ = 0;
    rendered_w_ = 0;
    rendered_h_ = 0;
}

void SvgIconView::setSvgFile(const std::string& file_path) {
    doc_ = lunasvg::Document::loadFromFile(file_path);
    dirty_ = true;
    invalidate();
}

void SvgIconView::setSvgString(const std::string& xml) {
    doc_ = lunasvg::Document::loadFromData(xml);
    dirty_ = true;
    invalidate();
}

void SvgIconView::rebuildTexture(NVGcontext* vg, int pw, int ph) {
    freeTexture();
    if (!doc_ || pw <= 0 || ph <= 0) {
        dirty_ = false;
        return;
    }

    auto bitmap = doc_->renderToBitmap(pw, ph);
    if (!bitmap.valid()) {
        dirty_ = false;
        return;
    }
    bitmap.convertToRGBA();

    owner_vg_ = vg;
    nvg_tex_ = nvgCreateImageRGBA(vg, pw, ph, 0, bitmap.data());
    rendered_w_ = pw;
    rendered_h_ = ph;
    dirty_ = false;
}

void SvgIconView::draw(NVGcontext* vg,
                       float x,
                       float y,
                       float width,
                       float height,
                       brls::Style style,
                       brls::FrameContext* ctx) {
    (void)style;
    (void)ctx;
    if (!doc_ || width <= 1.0f || height <= 1.0f) {
        return;
    }

    const float scale = brls::Application::windowScale > 0.5f ? brls::Application::windowScale : 1.0f;
    const int pw = std::max(1, static_cast<int>(std::round(width * scale * 2.0f)));
    const int ph = std::max(1, static_cast<int>(std::round(height * scale * 2.0f)));

    if (dirty_ || nvg_tex_ == 0 || pw != rendered_w_ || ph != rendered_h_) {
        rebuildTexture(vg, pw, ph);
    }

    if (nvg_tex_ == 0) {
        return;
    }

    NVGpaint paint = nvgImagePattern(vg, x, y, width, height, 0.0f, nvg_tex_, this->getAlpha());
    nvgBeginPath(vg);
    nvgRect(vg, x, y, width, height);
    nvgFillPaint(vg, paint);
    nvgFill(vg);
}

} // namespace omnidesk::host
