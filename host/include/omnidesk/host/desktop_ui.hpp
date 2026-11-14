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
    std::function<void()> on_search;               ///< /
    std::function<void()> on_toggle_theme;         ///< t
    std::function<bool()> on_cancel_active;        ///< Esc -> cancel streaming AI / async fetch
    std::function<bool()> on_skip_ad;              ///< F9 -> skip active SponsorBlock segment
};

/**
 * Installs OmniDesk's keyboard-first interaction layer (Ctrl+1..9, Ctrl+Tab, F8,
 * hjkl focus navigation, gg/G/Ctrl+D/U scrolling, f hint labels, /, ? help overlay).
 */
class DesktopInputController {
public:
    static void install(DesktopCallbacks callbacks);
    static void showShortcutsHelp();

private:
    static void renderHintOverlay(NVGcontext* vg);
};

} // namespace omnidesk::host
