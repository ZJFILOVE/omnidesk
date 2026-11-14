#pragma once

#include <borealis.hpp>
#include <functional>
#include <string>
#include "omnidesk/export.h"

namespace omnidesk::ui {

/**
 * Apply OmniDesk's Paper (Light) & Graphite (Dark) design tokens inherited
 * from native-wiliwili/design-system.md, plus semantic status/domain palette.
 */
OMNIDESK_API void applyDesignSystemTheme();

OMNIDESK_API NVGcolor color(const std::string& token);

OMNIDESK_API brls::Label* makeLabel(const std::string& text,
                                    float font_size = 18.f,
                                    const std::string& color_token = "brls/text",
                                    float line_height = 1.45f);

OMNIDESK_API brls::Button* makeButton(const std::string& text,
                                      std::function<void()> on_click,
                                      bool primary = false,
                                      float height = 42.f);

OMNIDESK_API brls::Box* makeCard(const std::string& bg_token = "desktop/surface",
                                 float padding = 16.f,
                                 float radius = 10.f);

OMNIDESK_API brls::Box* makePill(const std::string& text,
                                 const std::string& bg_token = "desktop/soft",
                                 const std::string& text_token = "font/grey");

OMNIDESK_API brls::Box* makeDivider(float margin_v = 10.f);

} // namespace omnidesk::ui
