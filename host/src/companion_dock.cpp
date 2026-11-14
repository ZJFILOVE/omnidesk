#include "omnidesk/host/companion_dock.hpp"

#include <vector>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {

AiCompanionDock::AiCompanionDock(std::function<void()> on_open_marketplace)
    : brls::Box(brls::Axis::COLUMN),
      on_open_marketplace_(std::move(on_open_marketplace)) {
    setVisibility(brls::Visibility::GONE);
    setBackgroundColor(ui::color("desktop/surface"));
    setBorderThickness(1);
    setBorderColor(ui::color("desktop/line"));
    setPadding(16);
    setWidth(350);

    // Header
    auto* header = new brls::Box(brls::Axis::ROW);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(8);

    auto* title = ui::makeLabel("F8 伴读协作栏", 19.0f, "brls/text");
    header->addView(title);

    auto* close_btn = ui::makeButton("收起", [this]() { toggleVisible(); }, false, 32.0f);
    header->addView(close_btn);
    addView(header);

    // Domain context + AI Router badge
    auto* meta_card = ui::makeCard("desktop/soft", 10.0f, 8.0f);
    meta_card->setMarginBottom(10);

    domain_badge_ = ui::makeLabel("当前域: 底座与插件", 14.5f, "brls/accent");
    domain_badge_->setMarginBottom(4);
    meta_card->addView(domain_badge_);

    route_status_label_ = ui::makeLabel("AI 路由: 检测中...", 14.0f, "font/grey");
    meta_card->addView(route_status_label_);
    addView(meta_card);

    // Quick domain presets
    auto* preset_title = ui::makeLabel("场景快捷指令", 14.0f, "font/grey");
    preset_title->setMarginBottom(6);
    addView(preset_title);

    quick_actions_box_ = new brls::Box(brls::Axis::COLUMN);
    quick_actions_box_->setMarginBottom(10);
    addView(quick_actions_box_);

    // Custom plugin dock slot
    widget_slot_box_ = new brls::Box(brls::Axis::COLUMN);
    widget_slot_box_->setMarginBottom(8);
    addView(widget_slot_box_);

    // Scrollable response thread
    auto* scroll = new brls::ScrollingFrame();
    scroll->setGrow(1.0f);
    scroll->setWidthPercentage(100);
    messages_box_ = new brls::Box(brls::Axis::COLUMN);
    messages_box_->setWidthPercentage(100);
    scroll->setContentView(messages_box_);
    addView(scroll);

    // Input action bar
    auto* input_row = new brls::Box(brls::Axis::ROW);
    input_row->setMarginTop(10);
    input_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

    auto* ask_btn = ui::makeButton(
        "输入提问...",
        [this]() {
            if (auto* plat = brls::Application::getPlatform()) {
                plat->getImeManager()->openForText(
                    [this](std::string text) {
                        if (!text.empty()) {
                            submitPrompt(text);
                        }
                    },
                    "向 OmniDesk AI Companion 提问",
                    "将优先路由至已注册的 ai.agent.v1 / llm.chat.v1 插件",
                    512,
                    "");
            }
        },
        true,
        38.0f);
    ask_btn->setGrow(1.0f);
    input_row->addView(ask_btn);

    stop_btn_ = ui::makeButton(
        "停止 Esc",
        [this]() { cancelActiveStream(); },
        false,
        38.0f);
    stop_btn_->setMarginLeft(8);
    stop_btn_->setVisibility(brls::Visibility::GONE);
    input_row->addView(stop_btn_);

    auto* store_btn = ui::makeButton(
        "插件",
        [this]() {
            if (on_open_marketplace_) {
                on_open_marketplace_();
            }
        },
        false,
        38.0f);
    store_btn->setMarginLeft(8);
    input_row->addView(store_btn);
    addView(input_row);

    refreshAiRouteStatus();
    rebuildQuickPresets();
    appendMessage("系统", "F8 侧边栏已就绪。安装 plugin-magpie-agent 后，将自动读取本机 magpie 的 Harness / 分组 / 配额并实时双向路由模型选择。");
}

AiCompanionDock::~AiCompanionDock() {
    alive_->store(false, std::memory_order_release);
    cancelActiveStream();
}

void AiCompanionDock::toggleVisible() {
    open_ = !open_;
    setVisibility(open_ ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    if (open_) {
        refreshAiRouteStatus();
        rebuildQuickPresets();
    }
    invalidate();
}

void AiCompanionDock::applyResponsiveMode(float window_width, float window_height) {
    if (window_height <= 1.0f) {
        return;
    }
    const bool narrow = (window_width / window_height) < 1.45f;
    if (narrow == bottom_mode_) {
        return;
    }
    bottom_mode_ = narrow;
    if (bottom_mode_) {
        setWidthPercentage(100);
        setHeight(280);
    } else {
        setHeightPercentage(100);
        setWidth(350);
    }
}

void AiCompanionDock::setDomainContext(ContentDomain domain, const std::string& entity_summary) {
    if (domain != domain_ || entity_summary != entity_summary_) {
        cancelActiveStream();
    }
    domain_ = domain;
    entity_summary_ = entity_summary;
    std::string text = "当前域: " + toDisplayLabel(domain);
    if (!entity_summary.empty()) {
        text += " · " + entity_summary;
    }
    if (domain_badge_) {
        domain_badge_->setText(text);
    }
    refreshAiRouteStatus();
    rebuildQuickPresets();
}

void AiCompanionDock::refreshAiRouteStatus() {
    if (!route_status_label_) {
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        return;
    }
    auto& caps = kernel.capabilities();
    if (caps.has("ai.agent.v1") || caps.has("llm.chat.v1")) {
        auto providers = caps.providersFor(caps.has("ai.agent.v1") ? "ai.agent.v1" : "llm.chat.v1");
        std::string info = "已接入: " + providers.front().plugin_id;
        if (caps.has("llm.router.v1")) {
            auto r = caps.call("llm.router.v1", {{"action", "current_route"}});
            if (r.ok && r.data.contains("summary") && r.data["summary"].is_string()) {
                info += " (" + r.data["summary"].get<std::string>() + ")";
            }
        }
        route_status_label_->setText(info);
        route_status_label_->setTextColor(ui::color("status/ok_fg"));
    } else {
        route_status_label_->setText("未装载 AI 插件 · 请在插件市场接入 plugin-magpie-agent");
        route_status_label_->setTextColor(ui::color("status/warn_fg"));
    }
}

void AiCompanionDock::rebuildQuickPresets() {
    if (!quick_actions_box_) {
        return;
    }
    quick_actions_box_->clearViews();

    std::vector<std::string> prompts;
    switch (domain_) {
        case ContentDomain::Media:
            prompts = {"生成视频时间轴章节摘要", "分析当前歌词语法与双语意译", "匹配 NAS 本地关联片源"};
            break;
        case ContentDomain::Text:
            prompts = {"提炼技术长文结论与代码要点", "汇总评论区核心共识与争议", "生成期刊摘要与研究方法卡片"};
            break;
        case ContentDomain::Book:
            prompts = {"对当前 PDF 段落执行 BabelDOC 精译", "拆解当前长难句并给出词根记忆", "提取生词并推入 SM-2 复习卡片"};
            break;
        case ContentDomain::Core:
            prompts = {"诊断已装载能力与依赖拓扑", "读取本机 Magpie 可用路由与余量"};
            break;
    }

    for (const auto& p : prompts) {
        auto* btn = ui::makeButton(p, [this, p]() { submitPrompt(p); }, false, 34.0f);
        btn->setMarginBottom(6);
        quick_actions_box_->addView(btn);
    }

    if (widget_slot_box_) {
        widget_slot_box_->clearViews();
        auto& kernel = HostKernel::instance();
        if (kernel.isInitialized()) {
            for (const auto& w : kernel.domains().dockWidgets(domain_)) {
                if (w.create_view) {
                    if (auto* v = w.create_view()) {
                        widget_slot_box_->addView(v);
                    }
                }
            }
        }
    }
}

void AiCompanionDock::submitPrompt(const std::string& prompt) {
    cancelActiveStream();
    appendMessage("你", prompt);

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        return;
    }

    auto& caps = kernel.capabilities();
    std::string cap_id;
    std::string role_prefix;
    nlohmann::json req = nlohmann::json::object();

    if (caps.has("ai.agent.v1")) {
        cap_id = "ai.agent.v1";
        role_prefix = "AI Agent";
        req = {
            {"action", "chat"},
            {"domain", toString(domain_)},
            {"context", entity_summary_},
            {"prompt", prompt},
            {"stream", true},
        };
    } else if (caps.has("llm.chat.v1")) {
        cap_id = "llm.chat.v1";
        role_prefix = "LLM";
        req = {
            {"domain", toString(domain_)},
            {"context", entity_summary_},
            {"prompt", prompt},
            {"stream", true},
        };
    } else {
        appendMessage("未安装能力插件",
                      "尚未安装提供 `ai.agent.v1` 或 `llm.chat.v1` 的独立插件（如 `plugin-magpie-agent`）。\n"
                      "你可以通过左侧「插件市场」从 GitHub 或本地仓库一键编译安装，无需重新编译主程序。");
        return;
    }

    const uint64_t seq = ++active_request_seq_;
    auto cancel = CancelToken::create();
    active_cancel_ = cancel;
    active_stream_text_.clear();
    {
        std::lock_guard<std::mutex> lk(stream_mutex_);
        stream_pending_delta_.clear();
        stream_flush_scheduled_ = false;
    }

    brls::Label* role_lbl = nullptr;
    brls::Label* body_lbl = appendMessage(role_prefix + " · 思考中...", "▍", &role_lbl);
    active_role_label_ = role_lbl;
    active_body_label_ = body_lbl;
    if (stop_btn_) {
        stop_btn_->setVisibility(brls::Visibility::VISIBLE);
    }

    caps.callStreamAsync(
        cap_id,
        req,
        [this, alive = alive_, cancel, seq](const StreamChunk& chunk) {
            if (!alive->load(std::memory_order_acquire) || cancel->isCancelled() || chunk.delta.empty()) {
                return;
            }
            bool need_schedule = false;
            {
                std::lock_guard<std::mutex> lk(stream_mutex_);
                stream_pending_delta_ += chunk.delta;
                if (!stream_flush_scheduled_) {
                    stream_flush_scheduled_ = true;
                    need_schedule = true;
                }
            }
            if (need_schedule) {
                brls::sync([this, alive, seq]() {
                    if (!alive->load(std::memory_order_acquire) || seq != active_request_seq_) {
                        return;
                    }
                    std::string delta;
                    {
                        std::lock_guard<std::mutex> lk(stream_mutex_);
                        delta.swap(stream_pending_delta_);
                        stream_flush_scheduled_ = false;
                    }
                    if (!delta.empty()) {
                        active_stream_text_ += delta;
                    }
                    if (active_body_label_) {
                        active_body_label_->setText(active_stream_text_ + "▍");
                    }
                    if (messages_box_) {
                        messages_box_->invalidate();
                    }
                });
            }
        },
        [this, alive = alive_, cancel, seq, role_prefix](const CapabilityResult& res) {
            brls::sync([this, alive, cancel, seq, role_prefix, res]() {
                if (!alive->load(std::memory_order_acquire) || seq != active_request_seq_) {
                    return;
                }
                std::string delta;
                {
                    std::lock_guard<std::mutex> lk(stream_mutex_);
                    delta.swap(stream_pending_delta_);
                    stream_flush_scheduled_ = false;
                }
                if (!delta.empty()) {
                    active_stream_text_ += delta;
                }

                const std::string provider = res.provider_plugin_id.empty()
                                                 ? role_prefix
                                                 : (role_prefix + " (" + res.provider_plugin_id + ")");
                if (res.cancelled || cancel->isCancelled()) {
                    const std::string body = active_stream_text_.empty()
                                                 ? "[已中断本次对话生成]"
                                                 : (active_stream_text_ + "\n\n[已中断]");
                    finalizeStreamUi(provider + " · 已中断", body, "font/grey");
                } else if (res.ok) {
                    std::string final_text = active_stream_text_;
                    if (final_text.empty() && res.data.contains("reply") && res.data["reply"].is_string()) {
                        final_text = res.data["reply"].get<std::string>();
                    }
                    if (final_text.empty()) {
                        final_text = res.data.empty() ? "(空回复)" : res.data.dump(2);
                    }
                    finalizeStreamUi(provider, final_text, "brls/text");
                } else {
                    finalizeStreamUi("错误 · " + provider,
                                     res.error.empty() ? "AI 能力调用失败" : res.error,
                                     "status/err_fg");
                }
            });
        },
        cancel);
}

bool AiCompanionDock::cancelActiveStream() {
    if (!active_cancel_) {
        return false;
    }
    auto cancel = active_cancel_;
    cancel->cancel();
    ++active_request_seq_;

    std::string delta;
    {
        std::lock_guard<std::mutex> lk(stream_mutex_);
        delta.swap(stream_pending_delta_);
        stream_flush_scheduled_ = false;
    }
    if (!delta.empty()) {
        active_stream_text_ += delta;
    }
    const std::string body = active_stream_text_.empty()
                                 ? "[已取消 AI 请求]"
                                 : (active_stream_text_ + "\n\n[已按 Esc / 停止键中断]");
    finalizeStreamUi("AI · 已中断", body, "font/grey");
    return true;
}

void AiCompanionDock::finalizeStreamUi(const std::string& role_text,
                                       const std::string& body_text,
                                       const char* color_key) {
    if (active_role_label_) {
        active_role_label_->setText(role_text);
    }
    if (active_body_label_) {
        active_body_label_->setText(body_text);
        if (color_key) {
            active_body_label_->setTextColor(ui::color(color_key));
        }
    }
    if (stop_btn_) {
        stop_btn_->setVisibility(brls::Visibility::GONE);
    }
    active_cancel_.reset();
    active_role_label_ = nullptr;
    active_body_label_ = nullptr;
    active_stream_text_.clear();
    if (messages_box_) {
        messages_box_->invalidate();
    }
}

brls::Label* AiCompanionDock::appendMessage(const std::string& role,
                                            const std::string& text,
                                            brls::Label** role_label_out) {
    if (!messages_box_) {
        return nullptr;
    }
    auto* card = ui::makeCard("desktop/soft", 10.0f, 8.0f);
    card->setMarginBottom(8);

    auto* r_lbl = ui::makeLabel(role, 13.5f, "brls/accent");
    r_lbl->setMarginBottom(4);
    card->addView(r_lbl);
    if (role_label_out) {
        *role_label_out = r_lbl;
    }

    auto* t_lbl = ui::makeLabel(text, 15.0f, "brls/text");
    card->addView(t_lbl);

    messages_box_->addView(card);
    return t_lbl;
}

} // namespace omnidesk::host
