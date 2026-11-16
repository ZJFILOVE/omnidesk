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
                     std::function<void()> on_open_store,
                     std::function<void()> on_open_auth_center = nullptr);
    ~HostSettingsView() override;

    void refresh();

private:
    std::function<void()> on_theme_toggled_;
    std::function<void()> on_open_store_;
    std::function<void()> on_open_auth_center_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> catalog_request_;
    uint64_t auth_sub_token_ = 0;
    bool syncing_catalog_ = false;
    bool refresh_pending_ = false;
    brls::Box* body_ = nullptr;
};

} // namespace omnidesk::host
