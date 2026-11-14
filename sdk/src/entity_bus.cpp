#include "omnidesk/entity_bus.hpp"
#include <algorithm>
#include <unordered_set>

namespace omnidesk {

uint64_t EntityBus::subscribe(const std::string& topic,
                              const std::string& owner_id,
                              EntityEventHandler handler) {
    if (topic.empty() || owner_id.empty() || !handler) return 0;
    std::lock_guard lock(mutex_);
    uint64_t token = next_token_++;
    subs_.push_back({token, topic, owner_id, std::move(handler)});
    return token;
}

void EntityBus::unsubscribe(uint64_t token) {
    std::lock_guard lock(mutex_);
    subs_.erase(std::remove_if(subs_.begin(), subs_.end(), [&](const Subscription& s) {
        return s.token == token;
    }), subs_.end());
}

void EntityBus::unsubscribeOwner(const std::string& owner_id) {
    std::lock_guard lock(mutex_);
    subs_.erase(std::remove_if(subs_.begin(), subs_.end(), [&](const Subscription& s) {
        return s.owner_id == owner_id;
    }), subs_.end());
}

size_t EntityBus::publish(const std::string& topic, const nlohmann::json& payload) const {
    std::vector<EntityEventHandler> targets;
    {
        std::lock_guard lock(mutex_);
        for (const auto& sub : subs_) {
            if (sub.topic == topic || sub.topic == "*") {
                targets.push_back(sub.handler);
            }
        }
    }
    for (const auto& fn : targets) {
        fn(topic, payload);
    }
    return targets.size();
}

std::vector<std::string> EntityBus::activeTopics() const {
    std::lock_guard lock(mutex_);
    std::unordered_set<std::string> unique;
    for (const auto& sub : subs_) unique.insert(sub.topic);
    std::vector<std::string> out(unique.begin(), unique.end());
    std::sort(out.begin(), out.end());
    return out;
}

} // namespace omnidesk
