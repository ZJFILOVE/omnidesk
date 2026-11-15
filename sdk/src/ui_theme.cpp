#include "omnidesk/ui_theme.hpp"

namespace omnidesk::ui {

void applyDesignSystemTheme() {
    brls::Label::FONT_SCALE = 1.0f;
    brls::Application::minimumWindowScale = 0.85f;
    brls::Application::maximumWindowScale = 1.0f;

    for (bool dark : {false, true}) {
        auto& theme = dark ? brls::Theme::getDarkTheme() : brls::Theme::getLightTheme();
        auto surface = dark ? nvgRGB(35, 33, 34) : nvgRGB(252, 251, 249);
        auto canvas  = dark ? nvgRGB(24, 23, 24) : nvgRGB(246, 245, 243);
        auto text    = dark ? nvgRGB(241, 238, 235) : nvgRGB(41, 39, 38);
        auto muted   = dark ? nvgRGB(181, 174, 172) : nvgRGB(108, 103, 98);
        auto line    = dark ? nvgRGB(65, 60, 62) : nvgRGB(224, 221, 216);
        auto accent  = dark ? nvgRGB(255, 147, 180) : nvgRGB(194, 59, 98);
        auto soft    = dark ? nvgRGB(43, 39, 41) : nvgRGB(241, 239, 236);
        auto active  = dark ? nvgRGB(66, 38, 48) : nvgRGB(249, 231, 237);
        auto focus_ring = dark ? nvgRGB(88, 166, 255) : nvgRGB(21, 94, 239);
        auto focus_halo = dark ? nvgRGBA(12, 13, 18, 242) : nvgRGBA(255, 255, 255, 246);
        auto focus_glow = dark ? nvgRGBA(88, 166, 255, 76) : nvgRGBA(21, 94, 239, 56);
        auto focus_fill = dark ? nvgRGBA(88, 166, 255, 26) : nvgRGBA(21, 94, 239, 15);

        theme.addColor("brls/clear", canvas);
        theme.addColor("brls/background", canvas);
        theme.addColor("brls/text", text);
        theme.addColor("brls/text_disabled", muted);
        theme.addColor("brls/backdrop", nvgRGBA(24, 23, 24, 145));
        theme.addColor("brls/accent", accent);
        theme.addColor("color/bilibili", accent);
        theme.addColor("font/grey", muted);
        theme.addColor("color/line", line);
        theme.addColor("color/grey_1", surface);
        theme.addColor("color/grey_2", soft);
        theme.addColor("desktop/surface", surface);
        theme.addColor("desktop/line", line);
        theme.addColor("desktop/soft", soft);
        theme.addColor("desktop/active", active);
        theme.addColor("desktop/focus_ring", focus_ring);
        theme.addColor("desktop/focus_halo", focus_halo);
        theme.addColor("desktop/focus_glow", focus_glow);
        theme.addColor("desktop/focus_fill", focus_fill);

        // Domain & status semantic tokens
        theme.addColor("status/ok_bg", dark ? nvgRGB(26, 54, 39) : nvgRGB(230, 245, 236));
        theme.addColor("status/ok_fg", dark ? nvgRGB(115, 214, 159) : nvgRGB(33, 118, 70));
        theme.addColor("status/warn_bg", dark ? nvgRGB(61, 44, 22) : nvgRGB(253, 243, 224));
        theme.addColor("status/warn_fg", dark ? nvgRGB(245, 186, 98) : nvgRGB(178, 99, 12));
        theme.addColor("status/err_bg", dark ? nvgRGB(66, 31, 36) : nvgRGB(253, 234, 237));
        theme.addColor("status/err_fg", dark ? nvgRGB(248, 134, 150) : nvgRGB(185, 38, 58));
        theme.addColor("status/info_bg", dark ? nvgRGB(29, 46, 66) : nvgRGB(232, 242, 253));
        theme.addColor("status/info_fg", dark ? nvgRGB(130, 186, 248) : nvgRGB(36, 98, 172));

        theme.addColor("domain/media", dark ? nvgRGB(255, 147, 180) : nvgRGB(194, 59, 98));
        theme.addColor("domain/text", dark ? nvgRGB(130, 186, 248) : nvgRGB(36, 98, 172));
        theme.addColor("domain/book", dark ? nvgRGB(115, 214, 159) : nvgRGB(33, 118, 70));
        theme.addColor("domain/core", dark ? nvgRGB(245, 186, 98) : nvgRGB(178, 99, 12));

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
        theme.addColor("brls/button/enabled_border_color", line);
        theme.addColor("brls/button/disabled_border_color", line);
        theme.addColor("brls/button/highlight_enabled_text", focus_ring);
        theme.addColor("brls/button/primary_enabled_background", nvgRGB(194, 59, 98));
        theme.addColor("brls/button/primary_enabled_text", nvgRGB(255, 255, 255));
        theme.addColor("brls/button/primary_disabled_background", soft);
        theme.addColor("brls/button/primary_disabled_text", muted);
        theme.addColor("brls/list/listItem_value_color", accent);
        theme.addColor("brls/slider/line_filled", accent);
    }

    auto style = brls::getStyle();
    style.addMetric("brls/animations/show", 90);
    style.addMetric("brls/animations/show_slide", 80);
    style.addMetric("brls/animations/highlight", 60);
    style.addMetric("brls/animations/highlight_shake", 85);
    style.addMetric("brls/notification/width", 340);
    style.addMetric("brls/label/default_font_size", 16);
    style.addMetric("brls/dialog/fontSize", 17);
    style.addMetric("brls/dropdown/listItemTextSize", 16.5f);
    style.addMetric("brls/dropdown/header_title_font_size", 20);
    style.addMetric("brls/highlight/stroke_width", 0);
    style.addMetric("brls/highlight/shadow_opacity", 0);
    style.addMetric("brls/highlight/shadow_offset", 0);
    style.addMetric("brls/highlight/corner_radius", 10);
    style.addMetric("brls/button/corner_radius", 10);
    style.addMetric("brls/button/border_thickness", 1);
    style.addMetric("brls/button/padding_top_bottom", 6);
    style.addMetric("brls/button/padding_sides", 14);
    style.addMetric("brls/button/text_size", 15);
    style.addMetric("brls/button/primary_highlight_padding", 2);
    style.addMetric("brls/button/shadow_width", 0);
}

NVGcolor color(const std::string& token) {
    return brls::Application::getTheme().getColor(token);
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
