#include "omnidesk/ui_theme.hpp"

namespace omnidesk::ui {

void applyDesignSystemTheme() {
    brls::Label::FONT_SCALE = 1.0f;
    brls::Application::minimumWindowScale = 0.85f;
    brls::Application::maximumWindowScale = 1.0f;

    for (bool dark : {false, true}) {
        auto& theme = dark ? brls::Theme::getDarkTheme() : brls::Theme::getLightTheme();
        auto canvas  = dark ? nvgRGB(20, 21, 25)    : nvgRGB(245, 244, 240);
        auto surface = dark ? nvgRGB(28, 30, 36)    : nvgRGB(252, 251, 249);
        auto soft    = dark ? nvgRGB(37, 40, 48)    : nvgRGB(238, 236, 231);
        auto text    = dark ? nvgRGB(236, 238, 242) : nvgRGB(34, 33, 32);
        auto muted   = dark ? nvgRGB(158, 164, 176) : nvgRGB(102, 98, 93);
        auto line    = dark ? nvgRGBA(255, 255, 255, 23) : nvgRGBA(36, 33, 29, 28);
        auto btn_bd  = dark ? nvgRGBA(255, 255, 255, 32) : nvgRGBA(36, 33, 29, 36);
        auto accent  = dark ? nvgRGB(244, 134, 168) : nvgRGB(186, 52, 90);
        auto active  = dark ? nvgRGB(46, 36, 45)    : nvgRGB(247, 233, 238);

        auto focus_ring = dark ? nvgRGB(96, 165, 250)  : nvgRGB(37, 99, 235);
        auto focus_halo = dark ? nvgRGBA(15, 17, 21, 210) : nvgRGBA(255, 255, 255, 224);
        auto focus_glow = dark ? nvgRGBA(96, 165, 250, 52) : nvgRGBA(37, 99, 235, 38);
        auto focus_fill = dark ? nvgRGBA(96, 165, 250, 18) : nvgRGBA(37, 99, 235, 11);

        auto hover_fill   = dark ? nvgRGBA(255, 255, 255, 11) : nvgRGBA(28, 26, 23, 9);
        auto hover_border = dark ? nvgRGBA(255, 255, 255, 42) : nvgRGBA(36, 33, 29, 46);
        auto scroll_thumb = dark ? nvgRGBA(186, 194, 210, 88) : nvgRGBA(92, 88, 82, 82);
        auto scroll_track = dark ? nvgRGBA(255, 255, 255, 8)  : nvgRGBA(36, 33, 29, 8);

        theme.addColor("brls/clear", canvas);
        theme.addColor("brls/background", canvas);
        theme.addColor("brls/text", text);
        theme.addColor("brls/text_muted", muted);
        theme.addColor("brls/text_disabled", muted);
        theme.addColor("brls/backdrop", dark ? nvgRGBA(9, 10, 14, 206) : nvgRGBA(24, 23, 22, 148));
        theme.addColor("brls/accent", accent);
        theme.addColor("color/bilibili", accent);
        theme.addColor("font/grey", muted);
        theme.addColor("color/line", line);
        theme.addColor("color/grey_1", surface);
        theme.addColor("color/grey_2", soft);
        theme.addColor("desktop/surface", surface);
        theme.addColor("desktop/line", line);
        theme.addColor("desktop/border", line);
        theme.addColor("desktop/soft", soft);
        theme.addColor("desktop/panel", soft);
        theme.addColor("desktop/active", active);
        theme.addColor("desktop/focus_ring", focus_ring);
        theme.addColor("desktop/focus_halo", focus_halo);
        theme.addColor("desktop/focus_glow", focus_glow);
        theme.addColor("desktop/focus_fill", focus_fill);
        theme.addColor("desktop/hover_fill", hover_fill);
        theme.addColor("desktop/hover_border", hover_border);
        theme.addColor("desktop/scrollbar_thumb", scroll_thumb);
        theme.addColor("desktop/scrollbar_track", scroll_track);

        // Domain & status semantic tokens
        theme.addColor("status/ok_bg", dark ? nvgRGB(24, 48, 37) : nvgRGB(231, 245, 237));
        theme.addColor("status/ok_fg", dark ? nvgRGB(110, 214, 156) : nvgRGB(30, 114, 68));
        theme.addColor("status/warn_bg", dark ? nvgRGB(54, 41, 22) : nvgRGB(252, 242, 223));
        theme.addColor("status/warn_fg", dark ? nvgRGB(245, 186, 98) : nvgRGB(172, 95, 12));
        theme.addColor("status/err_bg", dark ? nvgRGB(58, 29, 35) : nvgRGB(252, 234, 237));
        theme.addColor("status/err_fg", dark ? nvgRGB(248, 134, 150) : nvgRGB(182, 38, 58));
        theme.addColor("status/info_bg", dark ? nvgRGB(27, 42, 62) : nvgRGB(232, 242, 253));
        theme.addColor("status/info_fg", dark ? nvgRGB(130, 186, 248) : nvgRGB(34, 96, 170));
        theme.addColor("status/ok", dark ? nvgRGB(110, 214, 156) : nvgRGB(30, 114, 68));
        theme.addColor("status/warn", dark ? nvgRGB(245, 186, 98) : nvgRGB(172, 95, 12));
        theme.addColor("status/err", dark ? nvgRGB(248, 134, 150) : nvgRGB(182, 38, 58));
        theme.addColor("status/error", dark ? nvgRGB(248, 134, 150) : nvgRGB(182, 38, 58));
        theme.addColor("status/info", dark ? nvgRGB(130, 186, 248) : nvgRGB(34, 96, 170));

        theme.addColor("domain/media", dark ? nvgRGB(244, 134, 168) : nvgRGB(186, 52, 90));
        theme.addColor("domain/text", dark ? nvgRGB(125, 184, 250) : nvgRGB(34, 96, 170));
        theme.addColor("domain/book", dark ? nvgRGB(110, 214, 156) : nvgRGB(30, 114, 68));
        theme.addColor("domain/core", dark ? nvgRGB(242, 184, 98) : nvgRGB(172, 95, 12));

        theme.addColor("brls/highlight/background", focus_fill);
        theme.addColor("brls/highlight/color1", focus_ring);
        theme.addColor("brls/highlight/color2", focus_ring);
        theme.addColor("brls/sidebar/background", canvas);
        theme.addColor("brls/sidebar/separator", line);
        theme.addColor("brls/sidebar/active_item", accent);
        theme.addColor("brls/button/default_enabled_background", surface);
        theme.addColor("brls/button/default_enabled_text", text);
        theme.addColor("brls/button/default_disabled_background", canvas);
        theme.addColor("brls/button/default_disabled_text", muted);
        theme.addColor("brls/button/enabled_border_color", btn_bd);
        theme.addColor("brls/button/disabled_border_color", line);
        theme.addColor("brls/button/highlight_enabled_text", focus_ring);
        theme.addColor("brls/button/primary_enabled_background",
                       dark ? nvgRGB(204, 66, 106) : nvgRGB(186, 52, 90));
        theme.addColor("brls/button/primary_enabled_text", nvgRGB(255, 255, 255));
        theme.addColor("brls/button/primary_disabled_background", soft);
        theme.addColor("brls/button/primary_disabled_text", muted);
        theme.addColor("brls/list/listItem_value_color", accent);
        theme.addColor("brls/slider/line_filled", accent);
    }

    auto style = brls::getStyle();
    style.addMetric("brls/animations/show", 105);
    style.addMetric("brls/animations/show_slide", 95);
    style.addMetric("brls/animations/highlight", 75);
    style.addMetric("brls/animations/highlight_shake", 85);
    style.addMetric("brls/notification/width", 356);
    style.addMetric("brls/label/default_font_size", 16);
    style.addMetric("brls/dialog/fontSize", 16.5f);
    style.addMetric("brls/dropdown/listItemTextSize", 16.0f);
    style.addMetric("brls/dropdown/header_title_font_size", 19.5f);
    style.addMetric("brls/highlight/stroke_width", 0);
    style.addMetric("brls/highlight/shadow_opacity", 0);
    style.addMetric("brls/highlight/shadow_offset", 0);
    style.addMetric("brls/highlight/corner_radius", 10);
    style.addMetric("brls/button/corner_radius", 9);
    style.addMetric("brls/button/border_thickness", 1);
    style.addMetric("brls/button/padding_top_bottom", 6);
    style.addMetric("brls/button/padding_sides", 14);
    style.addMetric("brls/button/text_size", 14.8f);
    style.addMetric("brls/button/primary_highlight_padding", 2);
    style.addMetric("brls/button/shadow_width", 0);
}

NVGcolor color(const std::string& token) {
    auto theme = brls::Application::getTheme();
    try {
        return theme.getColor(token);
    } catch (...) {
        try {
            return theme.getColor("brls/text");
        } catch (...) {
            return nvgRGB(41, 39, 38);
        }
    }
}

brls::Label* makeLabel(const std::string& text,
                       float font_size,
                       const std::string& color_token,
                       float line_height) {
    auto* lbl = new brls::Label();
    lbl->setText(text);
    lbl->setFontSize(font_size);
    lbl->setLineHeight(line_height);
    lbl->setTextColor(color(color_token));
    return lbl;
}

brls::Button* makeButton(const std::string& text,
                         std::function<void()> on_click,
                         bool primary,
                         float height) {
    auto* btn = new brls::Button();
    btn->setText(text);
    const float font_sz = (height <= 30.5f) ? 13.5f : ((height <= 35.5f) ? 14.5f : 15.5f);
    const float pad_v   = (height <= 30.5f) ? 4.0f  : ((height <= 35.5f) ? 5.0f  : 6.0f);
    const float pad_h   = (height <= 30.5f) ? 10.0f : ((height <= 35.5f) ? 12.0f : 15.0f);
    const float radius  = (height <= 30.5f) ? 8.0f  : 10.0f;
    btn->setFontSize(font_sz);
    btn->setHeight(height);
    btn->setPadding(pad_v, pad_h, pad_v, pad_h);
    btn->setCornerRadius(radius);
    btn->setHighlightCornerRadius(radius);
    btn->setHideHighlightBackground(true);
    btn->setHideHighlightBorder(true);
    btn->setStyle(primary ? &brls::BUTTONSTYLE_PRIMARY : &brls::BUTTONSTYLE_BORDERED);
    if (!primary) {
        btn->setBackgroundColor(color("desktop/surface"));
    }
    btn->registerClickAction([fn = std::move(on_click)](brls::View*) {
        if (fn) fn();
        return true;
    });
    return btn;
}

brls::Box* makeCard(const std::string& bg_token, float padding, float radius) {
    auto* box = new brls::Box(brls::Axis::COLUMN);
    box->setWidthPercentage(100);
    box->setPadding(padding);
    box->setBackgroundColor(color(bg_token));
    box->setCornerRadius(radius);
    box->setHighlightCornerRadius(radius);
    box->setHideHighlightBackground(true);
    box->setHideHighlightBorder(true);
    box->setBorderThickness(1);
    box->setBorderColor(color("desktop/line"));
    box->addGestureRecognizer(new brls::TapGestureRecognizer(
        [box](brls::TapGestureStatus status, brls::Sound* soundToPlay) {
            if (!box->isFocusable() || box->getVisibility() != brls::Visibility::VISIBLE) {
                return;
            }
            // Let nested focusable child buttons handle taps inside their own bounds
            for (brls::View* hit = box->hitTest(status.position);
                 hit && hit != box;
                 hit = hit->getParent()) {
                if (hit->isFocusable()) {
                    return;
                }
            }
            if (status.state == brls::GestureState::START) {
                box->playClickAnimation(false, false);
            } else if (status.state == brls::GestureState::UNSURE) {
                box->playClickAnimation(false, false);
            } else if (status.state == brls::GestureState::FAILED ||
                       status.state == brls::GestureState::INTERRUPTED) {
                box->playClickAnimation(true);
            } else if (status.state == brls::GestureState::END) {
                box->playClickAnimation(true);
                brls::Application::giveFocus(box);
                for (const auto& act : box->getActions()) {
                    if (act && act->getButton() == brls::BUTTON_A && act->isAvailable()) {
                        if (soundToPlay) *soundToPlay = act->getSound();
                        auto listener = act->getActionListener();
                        if (listener) listener(box);
                        break;
                    }
                }
            }
        }));
    return box;
}

brls::Box* makePill(const std::string& text,
                    const std::string& bg_token,
                    const std::string& text_token) {
    auto* box = new brls::Box(brls::Axis::ROW);
    box->setPadding(4, 10, 4, 10);
    box->setCornerRadius(7);
    box->setBackgroundColor(color(bg_token));
    box->setAlignItems(brls::AlignItems::CENTER);
    auto* lbl = makeLabel(text, 13.0f, text_token, 1.2f);
    lbl->setSingleLine(true);
    box->addView(lbl);
    return box;
}

brls::Box* makeDivider(float margin_v) {
    auto* line = new brls::Box(brls::Axis::ROW);
    line->setWidthPercentage(100);
    line->setHeight(1);
    line->setMarginTop(margin_v);
    line->setMarginBottom(margin_v);
    line->setBackgroundColor(color("desktop/line"));
    return line;
}

} // namespace omnidesk::ui
