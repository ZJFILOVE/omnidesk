#pragma once

#include <borealis.hpp>
#include <functional>
#include <string>
#include <vector>

namespace omnidesk::host {

struct DesktopCallbacks {
    std::function<void(int)> on_select_workspace;  ///< Ctrl+1..9 -> 0-based index
    std::function<void(int)> on_cycle_workspace;   ///< Ctrl+Tab / Ctrl+Shift+Tab -> delta +/-1
    std::function<void()> on_toggle_dock;          ///< F8
    std::function<void()> on_toggle_fullscreen;    ///< F11 -> toggle window / exit immersive fullscreen
    std::function<void()> on_search;               ///< /
    std::function<void()> on_command_palette;      ///< Ctrl+K
    std::function<void()> on_toggle_theme;         ///< t
    std::function<bool()> on_cancel_active;        ///< Esc -> exit immersive fs / cancel streaming AI / async fetch
    std::function<bool()> on_skip_ad;              ///< F9 -> skip active SponsorBlock segment
};

/**
 * Installs OmniDesk's desktop focus & keyboard interaction engine:
 * - High-contrast scissor-clipped focus ring & card accent indicator
 * - Zero-dropped-keypress mouse-to-keyboard mode handoff & 26 Hz key repeat
 * - ScrollingFrame normalization, minimal-travel viewport scroll, and focus recovery
 * - 2D column-preserving grid navigation, Tab/Shift+Tab pane cycling, and modal support
 */
class DesktopInputController {
public:
    static void install(DesktopCallbacks callbacks);
    static void showShortcutsHelp();

private:
    static void renderOverlay(NVGcontext* vg);
};

} // namespace omnidesk::host
