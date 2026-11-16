#include "omnidesk/host/svg_view.hpp"
#include "omnidesk/host/nav_shortcuts.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <list>
#include <lunasvg.h>
#include <unordered_map>

namespace omnidesk::host {
namespace {

struct TextureCacheKey {
    std::string source_key;
    int pw = 0;
    int ph = 0;

    bool operator==(const TextureCacheKey& o) const {
        return pw == o.pw && ph == o.ph && source_key == o.source_key;
    }
};

struct TextureCacheKeyHash {
    size_t operator()(const TextureCacheKey& k) const noexcept {
        size_t h = std::hash<std::string>{}(k.source_key);
        h ^= static_cast<size_t>(k.pw) + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
        h ^= static_cast<size_t>(k.ph) + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
        return h;
    }
};

class SvgTexturePool {
public:
    static constexpr size_t kMaxCachedTextures = 384;

    static SvgTexturePool& instance() {
        static SvgTexturePool pool;
        return pool;
    }

    int find(NVGcontext* vg, const TextureCacheKey& key, bool& out_known_invalid) {
        out_known_invalid = false;
        if (!vg) return 0;
        ensureContext(vg);
        auto it = entries_.find(key);
        if (it == entries_.end()) {
            return 0;
        }
        lru_.splice(lru_.begin(), lru_, it->second.lru_it);
        if (it->second.tex_id < 0) {
            out_known_invalid = true;
            return 0;
        }
        return it->second.tex_id;
    }

    void store(NVGcontext* vg, const TextureCacheKey& key, int tex_id) {
        if (!vg) return;
        ensureContext(vg);
        auto it = entries_.find(key);
        if (it != entries_.end()) {
            if (it->second.tex_id > 0 && it->second.tex_id != tex_id) {
                nvgDeleteImage(vg, it->second.tex_id);
            }
            it->second.tex_id = tex_id > 0 ? tex_id : -1;
            lru_.splice(lru_.begin(), lru_, it->second.lru_it);
            return;
        }

        while (entries_.size() >= kMaxCachedTextures && !lru_.empty()) {
            const TextureCacheKey& victim_key = lru_.back();
            auto vit = entries_.find(victim_key);
            if (vit != entries_.end()) {
                if (vit->second.tex_id > 0) {
                    nvgDeleteImage(vg, vit->second.tex_id);
                }
                entries_.erase(vit);
            }
            lru_.pop_back();
        }

        lru_.push_front(key);
        entries_.emplace(key, Entry{tex_id > 0 ? tex_id : -1, lru_.begin()});
    }

private:
    struct Entry {
        int tex_id = -1;
        std::list<TextureCacheKey>::iterator lru_it;
    };

    void ensureContext(NVGcontext* vg) {
        if (vg_ != vg) {
            // Previous NVGcontext was destroyed or recreated; drop stale handles without calling nvgDeleteImage.
            entries_.clear();
            lru_.clear();
            vg_ = vg;
        }
    }

    NVGcontext* vg_ = nullptr;
    std::list<TextureCacheKey> lru_;
    std::unordered_map<TextureCacheKey, Entry, TextureCacheKeyHash> entries_;
};

std::string colorToHexRgb(NVGcolor c) {
    const int r = std::clamp(static_cast<int>(std::round(c.r * 255.0f)), 0, 255);
    const int g = std::clamp(static_cast<int>(std::round(c.g * 255.0f)), 0, 255);
    const int b = std::clamp(static_cast<int>(std::round(c.b * 255.0f)), 0, 255);
    char buf[16];
    std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", r, g, b);
    return std::string(buf);
}

} // namespace

SvgIconView::SvgIconView(const std::string& file_path) {
    if (!file_path.empty()) {
        setSvgFile(file_path);
    }
}

void SvgIconView::setSvgFile(const std::string& file_path) {
    const std::string next_key = "file:" + file_path;
    if (source_kind_ == SourceKind::File && source_key_ == next_key) {
        return;
    }
    source_kind_ = file_path.empty() ? SourceKind::None : SourceKind::File;
    source_key_ = next_key;
    raw_payload_ = file_path;
    nav_opacity_ = 1.0f;
    dirty_ = true;
}

void SvgIconView::setSvgString(const std::string& xml) {
    const std::string next_key = "xml:" + xml;
    if (source_kind_ == SourceKind::Xml && source_key_ == next_key) {
        return;
    }
    source_kind_ = xml.empty() ? SourceKind::None : SourceKind::Xml;
    source_key_ = next_key;
    raw_payload_ = xml;
    nav_opacity_ = 1.0f;
    dirty_ = true;
}

void SvgIconView::setNavIcon(const std::string& icon_kind, NVGcolor color) {
    if (icon_kind.empty()) {
        source_kind_ = SourceKind::None;
        source_key_.clear();
        raw_payload_.clear();
        nvg_tex_ = 0;
        dirty_ = false;
        return;
    }
    const std::string hex = colorToHexRgb(color);
    const float alpha = std::clamp(color.a, 0.0f, 1.0f);
    const std::string next_key = "nav:" + icon_kind + ":" + hex;
    nav_opacity_ = alpha;
    if (source_kind_ == SourceKind::NavIcon && source_key_ == next_key) {
        return;
    }
    source_kind_ = SourceKind::NavIcon;
    source_key_ = next_key;
    raw_payload_ = icon_kind;
    nav_hex_color_ = hex;
    dirty_ = true;
}

void SvgIconView::resolveTexture(NVGcontext* vg, int pw, int ph) {
    owner_vg_ = vg;
    rendered_w_ = pw;
    rendered_h_ = ph;
    dirty_ = false;

    if (source_kind_ == SourceKind::None || source_key_.empty() || pw <= 0 || ph <= 0 || !vg) {
        nvg_tex_ = 0;
        return;
    }

    const TextureCacheKey cache_key{source_key_, pw, ph};
    bool known_invalid = false;
    int cached = SvgTexturePool::instance().find(vg, cache_key, known_invalid);
    if (cached > 0) {
        nvg_tex_ = cached;
        return;
    }
    if (known_invalid) {
        nvg_tex_ = 0;
        return;
    }

    std::unique_ptr<lunasvg::Document> doc;
    if (source_kind_ == SourceKind::File) {
        doc = lunasvg::Document::loadFromFile(raw_payload_);
    } else if (source_kind_ == SourceKind::Xml) {
        doc = lunasvg::Document::loadFromData(raw_payload_);
    } else if (source_kind_ == SourceKind::NavIcon) {
        const std::string xml = buildNavIconSvg(raw_payload_, nav_hex_color_);
        doc = lunasvg::Document::loadFromData(xml);
    }

    if (!doc) {
        nvg_tex_ = 0;
        SvgTexturePool::instance().store(vg, cache_key, -1);
        return;
    }

    auto bitmap = doc->renderToBitmap(pw, ph);
    if (!bitmap.valid()) {
        nvg_tex_ = 0;
        SvgTexturePool::instance().store(vg, cache_key, -1);
        return;
    }
    bitmap.convertToRGBA();

    nvg_tex_ = nvgCreateImageRGBA(vg, pw, ph, 0, bitmap.data());
    SvgTexturePool::instance().store(vg, cache_key, nvg_tex_);
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
    if (source_kind_ == SourceKind::None || width <= 1.0f || height <= 1.0f || !vg) {
        return;
    }

    const float scale = brls::Application::windowScale > 0.5f ? brls::Application::windowScale : 1.0f;
    const int pw = std::max(1, static_cast<int>(std::round(width * scale * 2.0f)));
    const int ph = std::max(1, static_cast<int>(std::round(height * scale * 2.0f)));

    if (dirty_ || owner_vg_ != vg || nvg_tex_ == 0 || pw != rendered_w_ || ph != rendered_h_) {
        resolveTexture(vg, pw, ph);
    }

    if (nvg_tex_ <= 0) {
        return;
    }

    const float alpha = this->getAlpha() * nav_opacity_;
    NVGpaint paint = nvgImagePattern(vg, x, y, width, height, 0.0f, nvg_tex_, alpha);
    nvgBeginPath(vg);
    nvgRect(vg, x, y, width, height);
    nvgFillPaint(vg, paint);
    nvgFill(vg);
}

} // namespace omnidesk::host
