#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include "omnidesk/async.hpp"

namespace omnidesk::host {

/**
 * Host kernel & marketplace settings panel.
 */
class HostSettingsView : public brls::Box {
public:
    HostSettingsView(std::function<void()> on_theme_toggled,
                     std::function<void()> on_open_store);
    ~HostSettingsView() override;

    void refresh();

private:
    std::function<void()> on_theme_toggled_;
    std::function<void()> on_open_store_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> catalog_request_;
    bool syncing_catalog_ = false;
    brls::Box* body_ = nullptr;
};

} // namespace omnidesk::host
