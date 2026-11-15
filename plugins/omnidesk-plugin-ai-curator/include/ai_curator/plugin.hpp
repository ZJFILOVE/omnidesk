#pragma once

#include "ai_curator/curation_engine.hpp"
#include <memory>
#include <omnidesk/plugin.hpp>

namespace ai_curator {

class AiCuratorPlugin : public omnidesk::IPlugin {
public:
    AiCuratorPlugin() = default;
    ~AiCuratorPlugin() override = default;

    bool onLoad(omnidesk::PluginContext& ctx) override;
    void onUnload() override;
    nlohmann::json onHealthCheck() const override;

private:
    omnidesk::PluginContext* ctx_ = nullptr;
    std::shared_ptr<CurationEngine> engine_;
};

} // namespace ai_curator
