#include "ai_curator/plugin.hpp"

#include "ai_curator/domain_sources.hpp"

namespace ai_curator {

bool AiCuratorPlugin::onLoad(omnidesk::PluginContext& ctx) {
    ctx_ = &ctx;
    engine_ = std::make_shared<CurationEngine>(ctx);
    engine_->initializeOnLoad();

    ctx.registerCapability(
        "recommend.feed.v1",
        [eng = engine_](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            return eng->handleRecommendFeed(req);
        },
        {
            {"description", "Dual-repo archive rehydration, daily scheduler, and dual-mode cross-domain curation feed"},
            {"actions", {"bootstrap_and_rehydrate", "list_archive_dates", "search_archive",
                         "lookup_provenance", "get_date_bundle", "publish_daily_bundle",
                         "push_interactive_set", "pin_item", "dismiss_item",
                         "clear_interactive", "status",
                         "daily_status", "configure_daily_scheduler",
                         "run_daily_workflows", "cancel_daily_workflows",
                         "sync_github"}},
        },
        100);

    ctx.registerCapability(
        "ai.curation.v1",
        [eng = engine_](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            return eng->handleAiCuration(req);
        },
        {
            {"description", "Daily scheduled pipeline orchestration and interactive ephemeral curation"},
            {"actions", {"daily_status", "configure_daily_scheduler", "run_daily_pipeline",
                         "run_daily_workflows", "cancel_daily_workflows", "sync_github",
                         "rehydrate_archive", "curate_interactive"}},
        },
        80);

    ctx.registerMediaSource(std::make_shared<CuratorMediaSource>(engine_));
    ctx.registerTextSource(std::make_shared<CuratorTextSource>(engine_));
    ctx.registerBookSource(std::make_shared<CuratorBookSource>(engine_));

    ctx.logInfo("plugin-ai-curator loaded");
    return true;
}

void AiCuratorPlugin::onUnload() {
    if (engine_) {
        engine_->shutdown();
    }
    if (ctx_) {
        const std::string pid = ctx_->pluginId();
        ctx_->logInfo("Unloading plugin-ai-curator");
        ctx_->bus().unsubscribeOwner(pid);
        ctx_->domains().unregisterPlugin(pid);
        ctx_->capabilities().unregisterPlugin(pid);
        if (ctx_->workers()) {
            ctx_->workers()->stopByPlugin(pid);
        }
    }
    engine_.reset();
    ctx_ = nullptr;
}

nlohmann::json AiCuratorPlugin::onHealthCheck() const {
    if (!engine_) {
        return {{"ok", false}, {"error", "not_loaded"}};
    }
    return engine_->statusJson();
}

} // namespace ai_curator
