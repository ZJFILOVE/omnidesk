#pragma once

#include <functional>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <vector>
#include "omnidesk/export.h"

namespace omnidesk {

using EntityEventHandler = std::function<void(const std::string& topic, const nlohmann::json& payload)>;

/**
 * Cross-plugin asynchronous/synchronous entity routing bus.
 * Standard topics:
 *  - "entity.media.open"
 *  - "entity.text.open"
 *  - "entity.book.open"
 *  - "entity.vocab.add"
 *  - "entity.ai.route_changed"
 *  - "plugin.state_changed"
 */
class OMNIDESK_API EntityBus {
public:
    using FaultHandler = std::function<void(const std::string& owner_id,
                                            const std::string& topic,
                                            const std::string& error)>;

    struct RemoteBridge {
        std::function<void(const std::string& topic)> on_subscribe;
        std::function<void(const std::string& topic)> on_unsubscribe;
        std::function<size_t(const std::string& topic, const nlohmann::json& payload)> on_publish;
    };

    void setFaultHandler(FaultHandler handler);
    void setRemoteBridge(std::shared_ptr<RemoteBridge> bridge);

    uint64_t subscribe(const std::string& topic,
                       const std::string& owner_id,
                       EntityEventHandler handler);

    void unsubscribe(uint64_t token);
    void unsubscribeOwner(const std::string& owner_id);

    size_t publish(const std::string& topic,
                   const nlohmann::json& payload = nlohmann::json::object()) const;
    size_t publishLocalOnly(const std::string& topic,
                            const nlohmann::json& payload = nlohmann::json::object()) const;
    size_t publishExcludingOwner(const std::string& topic,
                                 const nlohmann::json& payload,
                                 const std::string& exclude_owner_id) const;

    std::vector<std::string> activeTopics() const;

private:
    struct Subscription {
        uint64_t token = 0;
        std::string topic;
        std::string owner_id;
        EntityEventHandler handler;
    };

    mutable std::mutex mutex_;
    std::vector<Subscription> subs_;
    std::unordered_map<std::string, size_t> topic_refcounts_;
    std::shared_ptr<RemoteBridge> remote_bridge_;
    mutable std::mutex fault_mutex_;
    FaultHandler fault_handler_;
    uint64_t next_token_ = 1;
};

} // namespace omnidesk
