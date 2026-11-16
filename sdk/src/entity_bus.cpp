#include "omnidesk/entity_bus.hpp"
#include <algorithm>
#include <chrono>
#include <unordered_set>

namespace omnidesk {

void EntityBus::setRemoteBridge(std::shared_ptr<RemoteBridge> bridge) {
    std::vector<std::string> existing_topics;
    {
        std::lock_guard lock(mutex_);
        remote_bridge_ = bridge;
        if (bridge && bridge->on_subscribe) {
            for (const auto& [topic, cnt] : topic_refcounts_) {
                if (cnt > 0) existing_topics.push_back(topic);
            }
        }
    }
    if (bridge && bridge->on_subscribe) {
        for (const auto& topic : existing_topics) {
            try {
                bridge->on_subscribe(topic);
            } catch (...) {
            }
        }
    }
}

uint64_t EntityBus::subscribe(const std::string& topic,
                              const std::string& owner_id,
                              EntityEventHandler handler) {
    if (topic.empty() || owner_id.empty() || !handler) return 0;
    uint64_t token = 0;
    std::shared_ptr<RemoteBridge> bridge;
    bool first_for_topic = false;
    {
        std::lock_guard lock(mutex_);
        token = next_token_++;
        subs_.push_back({token, topic, owner_id, std::move(handler)});
        size_t& cnt = topic_refcounts_[topic];
        if (cnt == 0) {
            first_for_topic = true;
        }
        ++cnt;
        bridge = remote_bridge_;
    }
    if (first_for_topic && bridge && bridge->on_subscribe) {
        try {
            bridge->on_subscribe(topic);
        } catch (...) {
        }
    }
    return token;
}

void EntityBus::unsubscribe(uint64_t token) {
    std::vector<std::string> removed_topics;
    std::shared_ptr<RemoteBridge> bridge;
    {
        std::lock_guard lock(mutex_);
        bridge = remote_bridge_;
        for (auto it = subs_.begin(); it != subs_.end();) {
            if (it->token == token) {
                auto rit = topic_refcounts_.find(it->topic);
                if (rit != topic_refcounts_.end()) {
                    if (rit->second <= 1) {
                        removed_topics.push_back(it->topic);
                        topic_refcounts_.erase(rit);
                    } else {
                        --rit->second;
                    }
                }
                it = subs_.erase(it);
            } else {
                ++it;
            }
        }
    }
    if (bridge && bridge->on_unsubscribe) {
        for (const auto& topic : removed_topics) {
            try {
                bridge->on_unsubscribe(topic);
            } catch (...) {
            }
        }
    }
}

void EntityBus::unsubscribeOwner(const std::string& owner_id) {
    std::vector<std::string> removed_topics;
    std::shared_ptr<RemoteBridge> bridge;
    {
        std::lock_guard lock(mutex_);
        bridge = remote_bridge_;
        for (auto it = subs_.begin(); it != subs_.end();) {
            if (it->owner_id == owner_id) {
                auto rit = topic_refcounts_.find(it->topic);
                if (rit != topic_refcounts_.end()) {
                    if (rit->second <= 1) {
                        removed_topics.push_back(it->topic);
                        topic_refcounts_.erase(rit);
                    } else {
                        --rit->second;
                    }
                }
                it = subs_.erase(it);
            } else {
                ++it;
            }
        }
    }
    if (bridge && bridge->on_unsubscribe) {
        for (const auto& topic : removed_topics) {
            try {
                bridge->on_unsubscribe(topic);
            } catch (...) {
            }
        }
    }
}

void EntityBus::setFaultHandler(FaultHandler handler) {
    std::lock_guard lock(fault_mutex_);
    fault_handler_ = std::move(handler);
}

size_t EntityBus::publishExcludingOwner(const std::string& topic,
                                        const nlohmann::json& payload,
                                        const std::string& exclude_owner_id) const {
    struct Target {
        std::string owner_id;
        EntityEventHandler fn;
    };
    std::vector<Target> targets;
    {
        std::lock_guard lock(mutex_);
        for (const auto& sub : subs_) {
            if (!exclude_owner_id.empty() && sub.owner_id == exclude_owner_id) {
                continue;
            }
            if (sub.topic == topic || sub.topic == "*") {
                targets.push_back({sub.owner_id, sub.handler});
                ++in_flight_by_owner_[sub.owner_id];
            }
        }
    }
    FaultHandler fault_cb;
    if (topic != "plugin.fault" && topic != "worker.fault") {
        std::lock_guard lock(fault_mutex_);
        fault_cb = fault_handler_;
    }
    size_t delivered = 0;
    for (const auto& target : targets) {
        try {
            target.fn(topic, payload);
            ++delivered;
        } catch (const std::exception& e) {
            if (fault_cb) {
                try {
                    fault_cb(target.owner_id, topic, e.what());
                } catch (...) {
                }
            }
        } catch (...) {
            if (fault_cb) {
                try {
                    fault_cb(target.owner_id, topic, "unknown subscriber exception");
                } catch (...) {
                }
            }
        }
        {
            std::lock_guard lock(mutex_);
            auto it = in_flight_by_owner_.find(target.owner_id);
            if (it != in_flight_by_owner_.end()) {
                if (it->second <= 1) {
                    in_flight_by_owner_.erase(it);
                    owner_idle_cv_.notify_all();
                } else {
                    --it->second;
                }
            }
        }
    }
    return delivered;
}

size_t EntityBus::publishLocalOnly(const std::string& topic, const nlohmann::json& payload) const {
    return publishExcludingOwner(topic, payload, "");
}

size_t EntityBus::publish(const std::string& topic, const nlohmann::json& payload) const {
    size_t delivered = publishExcludingOwner(topic, payload, "");
    std::shared_ptr<RemoteBridge> bridge;
    {
        std::lock_guard lock(mutex_);
        bridge = remote_bridge_;
    }
    if (bridge && bridge->on_publish) {
        try {
            delivered += bridge->on_publish(topic, payload);
        } catch (...) {
        }
    }
    return delivered;
}

std::vector<std::string> EntityBus::activeTopics() const {
    std::lock_guard lock(mutex_);
    std::unordered_set<std::string> unique;
    for (const auto& sub : subs_) unique.insert(sub.topic);
    std::vector<std::string> out(unique.begin(), unique.end());
    std::sort(out.begin(), out.end());
    return out;
}

bool EntityBus::hasInFlightOwner(const std::string& owner_id) const {
    std::lock_guard lock(mutex_);
    auto it = in_flight_by_owner_.find(owner_id);
    return it != in_flight_by_owner_.end() && it->second > 0;
}

bool EntityBus::waitForOwnerIdle(const std::string& owner_id, int timeout_ms) const {
    std::unique_lock lock(mutex_);
    auto idle = [&]() {
        auto it = in_flight_by_owner_.find(owner_id);
        return it == in_flight_by_owner_.end() || it->second == 0;
    };
    if (timeout_ms <= 0) {
        return idle();
    }
    return owner_idle_cv_.wait_for(
        lock, std::chrono::milliseconds(timeout_ms), idle);
}

} // namespace omnidesk
