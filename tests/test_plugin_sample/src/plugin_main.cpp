#include <memory>
#include <omnidesk/sdk.hpp>
#include <omnidesk/ui_theme.hpp>

namespace {

class SampleMediaSource final : public omnidesk::IMediaSource {
public:
    std::string sourceId() const override { return "plugin-sample-echo"; }
    std::string displayName() const override { return "Sample Echo Source"; }
    std::vector<std::string> categories() const override { return {"video"}; }
    std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
        omnidesk::MediaItem m;
        m.id = "sample-1";
        m.source_id = "plugin-sample-echo";
        m.title = "Sample Plugin Media Card";
        m.creator = "Plugin CI";
        m.source_badge = "Sample .so";
        m.quality_label = "4K";
        m.duration_sec = 95;
        return {m};
    }
};

class SampleEchoPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx_ = &ctx;
        ctx.logInfo("SampleEchoPlugin onLoad");

        ctx.registerCapability(
            "sample.echo.v1",
            [this](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                omnidesk::CapabilityResult r;
                r.ok = true;
                r.data = {
                    {"greeting", ctx_->getConfigString("greeting", "Hello")},
                    {"echo", req},
                };
                return r;
            },
            {{"name", "Sample Echo"}, {"version", "0.1.0"}});

        ctx.registerMediaSource(std::make_shared<SampleMediaSource>());

        ctx.registerWorkspaceTab({
            "sample-echo-tab",
            ctx.pluginId(),
            "示例扩展页",
            omnidesk::ContentDomain::Media,
            "Sample",
            []() -> brls::View* {
                auto* card = omnidesk::ui::makeCard("desktop/surface", 24.0f, 12.0f);
                card->addView(omnidesk::ui::makeLabel("来自外部 .so 插件的动态 WorkspaceTab", 22.0f, "brls/accent"));
                return card;
            },
        });

        ctx.subscribe("entity.media.open", [this](const std::string&, const nlohmann::json& p) {
            if (ctx_) {
                ctx_->logInfo("Observed media open: " + p.dump());
            }
        });
        return true;
    }

    void onUnload() override {
        ctx_ = nullptr;
    }

private:
    omnidesk::PluginContext* ctx_ = nullptr;
};

} // namespace

OMNIDESK_DECLARE_PLUGIN(SampleEchoPlugin,
                        "plugin-sample-echo",
                        "Sample Out-of-Tree Test Plugin",
                        "0.1.0",
                        "hybrid",
                        "media")
