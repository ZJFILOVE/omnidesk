#pragma once

#include <borealis.hpp>
#include <functional>

namespace omnidesk::host {

/**
 * Host kernel & marketplace settings panel.
 */
class HostSettingsView : public brls::Box {
public:
    HostSettingsView(std::function<void()> on_theme_toggled,
                     std::function<void()> on_open_store);

    void refresh();

private:
    std::function<void()> on_theme_toggled_;
    std::function<void()> on_open_store_;
    brls::Box* body_ = nullptr;
};

} // namespace omnidesk::host
