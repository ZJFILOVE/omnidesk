#include <omnidesk/sdk.hpp>

class StarterPlugin final : public omnidesk::IPlugin {
public:
    bool onLoad(omnidesk::PluginContext& ctx) override {
        ctx_ = &ctx;
        ctx.logInfo("StarterPlugin initialized");
        ctx.registerCapability(
            "starter.echo.v1",
            [this](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                omnidesk::CapabilityResult res;
                res.ok = true;
                res.data = {
                    {"plugin_id", ctx_->pluginId()},
                    {"endpoint", ctx_->getConfigString("endpoint", "http://127.0.0.1:8080")},
                    {"echo", req},
                };
                return res;
            },
            {{"name", "Starter Echo"}, {"version", "0.1.0"}});
        return true;
    }

    void onUnload() override {
        if (ctx_) {
            ctx_->logInfo("StarterPlugin unloaded cleanly");
            ctx_ = nullptr;
        }
    }

    nlohmann::json onHealthCheck() const override {
        return {{"ok", true}, {"plugin_id", "plugin-starter"}};
    }

private:
    omnidesk::PluginContext* ctx_ = nullptr;
};

OMNIDESK_DECLARE_PLUGIN(StarterPlugin,
                        "plugin-starter",
                        "OmniDesk Starter Plugin",
                        "0.1.0",
                        "provider",
                        "core")
