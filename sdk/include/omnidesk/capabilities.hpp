#pragma once

#include <functional>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <shared_mutex>
#include <string>
#include <unordered_map>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/export.h"

namespace omnidesk {

struct CapabilityResult {
    bool ok = false;
    nlohmann::json data = nlohmann::json::object();
    std::string error;
    std::string provider_plugin_id;
    bool cancelled = false;
};

using CapabilityHandler = std::function<CapabilityResult(const nlohmann::json& request)>;
using CapabilityStreamHandler = std::function<CapabilityResult(
    const nlohmann::json& request,
    const std::shared_ptr<CancelToken>& cancel,
    const StreamCallback& on_chunk)>;
using CapabilityAsyncCallback = std::function<void(const CapabilityResult& result)>;

struct CapabilityDescriptor {
    std::string capability_id;     ///< e.g. "ai.agent.v1", "llm.router.v1", "media.clip_jump.v1"
    std::string plugin_id;         ///< Owning plugin ID
    int priority = 100;            ///< Higher priority wins default call()
    nlohmann::json metadata = nlohmann::json::object();
};

#define OMNIDESK_CAPABILITY_INTERFACE_REVISION 2u

/**
 * Central service registry enabling decoupled cross-plugin collaboration.
 * Supports synchronous calls, cancellable async calls, and SSE-style token streaming.
 */
class OMNIDESK_API CapabilityRegistry {
public:
    using ChangeCallback = std::function<void()>;
    using FaultHandler = std::function<void(const std::string& plugin_id,
                                            const std::string& component,
                                            const std::string& error)>;

    CapabilityRegistry();
    ~CapabilityRegistry();

    CapabilityRegistry(const CapabilityRegistry&) = delete;
    CapabilityRegistry& operator=(const CapabilityRegistry&) = delete;

    void shutdown(int timeout_ms = -1);
    bool hasInFlight(const std::string& plugin_id) const;

    void setFaultHandler(FaultHandler handler);

    bool registerCapability(const std::string& capability_id,
                            const std::string& plugin_id,
                            CapabilityHandler handler,
                            nlohmann::json metadata = nlohmann::json::object(),
                            int priority = 100);

    bool registerStreamingCapability(const std::string& capability_id,
                                     const std::string& plugin_id,
                                     CapabilityStreamHandler handler,
                                     nlohmann::json metadata = nlohmann::json::object(),
                                     int priority = 100);

    void unregisterPlugin(const std::string& plugin_id);
    bool unregisterCapability(const std::string& capability_id, const std::string& plugin_id);

    bool has(const std::string& capability_id) const;
    std::vector<CapabilityDescriptor> providersFor(const std::string& capability_id) const;
    std::vector<CapabilityDescriptor> listAll() const;

    CapabilityResult call(const std::string& capability_id,
                          const nlohmann::json& request = nlohmann::json::object()) const;
    CapabilityResult callProvider(const std::string& plugin_id,
                                  const std::string& capability_id,
                                  const nlohmann::json& request = nlohmann::json::object()) const;

    CapabilityResult callCancellable(const std::string& capability_id,
                                     const nlohmann::json& request,
                                     const std::shared_ptr<CancelToken>& cancel) const;

    CapabilityResult callStream(const std::string& capability_id,
                                const nlohmann::json& request,
                                const StreamCallback& on_chunk,
                                const std::shared_ptr<CancelToken>& cancel = nullptr) const;

    CapabilityResult callProviderStream(const std::string& plugin_id,
                                        const std::string& capability_id,
                                        const nlohmann::json& request,
                                        const StreamCallback& on_chunk,
                                        const std::shared_ptr<CancelToken>& cancel = nullptr) const;

    std::shared_ptr<CancelToken> callAsync(const std::string& capability_id,
                                           const nlohmann::json& request,
                                           CapabilityAsyncCallback on_complete,
                                           std::shared_ptr<CancelToken> cancel = nullptr) const;

    std::shared_ptr<CancelToken> callProviderAsync(const std::string& plugin_id,
                                                   const std::string& capability_id,
                                                   const nlohmann::json& request,
                                                   CapabilityAsyncCallback on_complete,
                                                   std::shared_ptr<CancelToken> cancel = nullptr) const;

    std::shared_ptr<CancelToken> callStreamAsync(const std::string& capability_id,
                                                 const nlohmann::json& request,
                                                 StreamCallback on_chunk,
                                                 CapabilityAsyncCallback on_complete,
                                                 std::shared_ptr<CancelToken> cancel = nullptr) const;

    std::shared_ptr<CancelToken> callProviderStreamAsync(const std::string& plugin_id,
                                                         const std::string& capability_id,
                                                         const nlohmann::json& request,
                                                         StreamCallback on_chunk,
                                                         CapabilityAsyncCallback on_complete,
                                                         std::shared_ptr<CancelToken> cancel = nullptr) const;

    uint64_t subscribeChanges(ChangeCallback cb);
    void unsubscribeChanges(uint64_t token);

private:
    struct Entry {
        CapabilityDescriptor desc;
        CapabilityHandler handler;
        CapabilityStreamHandler stream_handler;
    };

    struct FaultDispatcher;
    struct AsyncExecutor;

    CapabilityResult invokeInternal(const std::string& plugin_id_filter,
                                    const std::string& capability_id,
                                    const nlohmann::json& request,
                                    const StreamCallback& on_chunk,
                                    const std::shared_ptr<CancelToken>& cancel,
                                    bool stream_mode) const;
    void reportFault(const std::string& plugin_id,
                     const std::string& component,
                     const std::string& error) const;
    void notifyChanged();

    mutable std::shared_mutex mutex_;
    std::unordered_map<std::string, std::vector<Entry>> entries_;
    std::mutex listeners_mutex_;
    std::unordered_map<uint64_t, ChangeCallback> listeners_;
    std::shared_ptr<FaultDispatcher> fault_dispatcher_;
    uint64_t next_token_ = 1;
    std::unique_ptr<AsyncExecutor> executor_;
};

} // namespace omnidesk
