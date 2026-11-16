#pragma once

#include <algorithm>
#include <functional>
#include <string>
#include <borealis.hpp>
#include <yoga/Yoga.h>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host::ui_comfort {

inline NVGcolor blendUiColor(NVGcolor a, NVGcolor b, float t) {
    const float k = std::clamp(t, 0.0f, 1.0f);
    return nvgRGBAf(
        a.r + (b.r - a.r) * k,
        a.g + (b.g - a.g) * k,
        a.b + (b.b - a.b) * k,
        a.a + (b.a - a.a) * k);
}

inline bool isDarkUi() {
    return HostKernel::instance().isInitialized() && HostKernel::instance().isDarkTheme();
}

/**
 * Enable multi-line flex wrapping on horizontal filter/tab/action rows so narrow or tiled
 * desktop windows wrap cleanly instead of clipping buttons off-screen.
 */
inline void enableFlexWrap(brls::Box* box, float row_gap = 6.0f) {
    if (!box) {
        return;
    }
    if (YGNodeRef node = box->getYGNode()) {
        YGNodeStyleSetFlexWrap(node, YGWrapWrap);
        YGNodeStyleSetGap(node, YGGutterRow, row_gap);
    }
}

/**
 * Calm domain-tinted filter chip / sub-tab button.
 * Avoids heavy solid-primary CTA fills on filter bars while keeping active state unmistakable.
 *
 * Note: Borealis Button::setStyle() and Button::setTextColor() invoke applyStyle(), which resets
 * custom background/border on BUTTONSTYLE_BORDERLESS. Custom fill/border MUST be applied after them.
 */
inline brls::Button* makeFilterChip(const std::string& text,
                                    std::function<void()> on_click,
                                    bool active,
                                    const std::string& accent_token = "brls/accent",
                                    float height = 31.0f) {
    const bool dark = isDarkUi();
    const NVGcolor surface = ui::color("desktop/surface");
    const NVGcolor soft    = ui::color("desktop/soft");
    const NVGcolor txt     = ui::color("brls/text");
    const NVGcolor muted   = ui::color("font/grey");
    const NVGcolor accent  = ui::color(accent_token);

    const NVGcolor fg = active
                            ? blendUiColor(accent, txt, dark ? 0.18f : 0.10f)
                            : blendUiColor(muted, txt, 0.36f);
    const NVGcolor bg = active
                            ? blendUiColor(surface, accent, dark ? 0.20f : 0.13f)
                            : blendUiColor(surface, soft, dark ? 0.52f : 0.58f);
    NVGcolor bd = active
                      ? blendUiColor(accent, surface, dark ? 0.34f : 0.40f)
                      : ui::color("desktop/line");
    if (active) {
        bd.a = 1.0f;
    }

    const float font_sz = (height <= 29.5f) ? 13.0f : ((height <= 33.5f) ? 13.8f : 14.5f);
    const float pad_v   = (height <= 29.5f) ? 3.5f  : 4.5f;
    const float pad_h   = (height <= 29.5f) ? 10.0f : 12.5f;
    const float radius  = (height <= 29.5f) ? 8.5f  : 10.0f;

    auto* btn = new brls::Button();
    btn->setText(text);
    btn->setFontSize(font_sz);
    btn->setHeight(height);
    btn->setPadding(pad_v, pad_h, pad_v, pad_h);
    btn->setStyle(&brls::BUTTONSTYLE_BORDERLESS);
    btn->setTextColor(fg);

    // Apply custom chip geometry & surface after setStyle/setTextColor
    btn->setCornerRadius(radius);
    btn->setHighlightCornerRadius(radius);
    btn->setHideHighlightBackground(true);
    btn->setHideHighlightBorder(true);
    btn->setBackgroundColor(bg);
    btn->setBorderThickness(1.0f);
    btn->setBorderColor(bd);

    btn->registerClickAction([fn = std::move(on_click)](brls::View*) {
        if (fn) fn();
        return true;
    });
    return btn;
}

/**
 * Apply subtle domain-aware selection tint & border to a list/feed card.
 */
inline void styleSelectableCard(brls::Box* card,
                                bool selected,
                                const std::string& accent_token = "brls/accent") {
    if (!card) {
        return;
    }
    const bool dark = isDarkUi();
    const NVGcolor surface = ui::color("desktop/surface");
    if (!selected) {
        card->setBackgroundColor(surface);
        card->setBorderThickness(1.0f);
        card->setBorderColor(ui::color("desktop/line"));
        return;
    }
    const NVGcolor accent = ui::color(accent_token);
    NVGcolor bd = blendUiColor(accent, surface, dark ? 0.36f : 0.42f);
    bd.a = 1.0f;
    card->setBackgroundColor(blendUiColor(surface, accent, dark ? 0.14f : 0.08f));
    card->setBorderThickness(1.25f);
    card->setBorderColor(bd);
}

/**
 * Compact rounded progress bar for reading/playback progress cards.
 */
inline brls::Box* makeProgressBar(float pct_0_100,
                                  const std::string& fill_token,
                                  float height = 5.0f) {
    auto* track = new brls::Box(brls::Axis::ROW);
    track->setWidthPercentage(100);
    track->setHeight(height);
    track->setCornerRadius(height * 0.5f);
    track->setBackgroundColor(ui::color("desktop/soft"));
    track->setClipsToBounds(true);

    const float clamped = std::clamp(pct_0_100, 0.0f, 100.0f);
    if (clamped > 0.5f) {
        auto* fill = new brls::Box(brls::Axis::ROW);
        fill->setWidthPercentage(std::max(3.0f, clamped));
        fill->setHeight(height);
        fill->setCornerRadius(height * 0.5f);
        fill->setBackgroundColor(ui::color(fill_token));
        track->addView(fill);
    }
    return track;
}

} // namespace omnidesk::host::ui_comfort
