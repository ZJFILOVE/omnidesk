#pragma once

#include <atomic>
#include <cstddef>
#include <functional>
#include <memory>
#include <string>

#include <borealis/core/box.hpp>
#include "omnidesk/export.h"

namespace omnidesk {

/**
 * Tracks and guards the lifetime of plugin-created brls::View* instances so
 * unloading a plugin .so never calls dlclose() while a view (or Borealis
 * deletionPool entry) referencing that DSO's vtable/callbacks is still alive.
 */
class OMNIDESK_API DsoViewLeaseRegistry {
public:
    struct Impl;

    class OMNIDESK_API LeaseToken {
    public:
        LeaseToken(std::string plugin_id, Impl* impl);
        ~LeaseToken();

        LeaseToken(const LeaseToken&) = delete;
        LeaseToken& operator=(const LeaseToken&) = delete;

        const std::string& pluginId() const noexcept { return plugin_id_; }

    private:
        std::string plugin_id_;
        Impl* impl_ = nullptr;
    };

    static DsoViewLeaseRegistry& instance();

    void markUiThread();
    bool isUiThread() const;

    /// Clears any prior revocation state when loading/reloading a plugin.
    void beginPluginSession(const std::string& plugin_id);

    /// Acquires an RAII lease token for a plugin-backed UI resource.
    /// Returns nullptr if the plugin is currently revoking/unloading.
    std::shared_ptr<LeaseToken> acquireLease(const std::string& plugin_id);

    /// Wraps a plugin-allocated brls::View* inside a host-owned brls::Box
    /// whose vtable and destructor reside in libomnidesk_sdk.so.
    brls::Box* leaseView(const std::string& plugin_id, brls::View* plugin_view);

    /// Marks the plugin revoking, tears down active leased views safely on the UI thread,
    /// and optionally waits up to wait_ms (from non-UI threads) for leases to drain.
    void revokePlugin(const std::string& plugin_id, int wait_ms = 120);

    bool isRevoking(const std::string& plugin_id) const;
    bool hasActiveLeases(const std::string& plugin_id) const;
    size_t activeLeaseCount(const std::string& plugin_id) const;

    /// Registers an optional cleanup callback invoked once active leases for plugin_id drop to 0.
    void onAllLeasesReleased(const std::string& plugin_id, std::function<void()> cb);

private:
    DsoViewLeaseRegistry();
    ~DsoViewLeaseRegistry();

    std::unique_ptr<Impl> impl_;
};

} // namespace omnidesk
