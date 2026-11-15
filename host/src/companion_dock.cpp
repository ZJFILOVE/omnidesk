#include "omnidesk/host/companion_dock.hpp"

#include <algorithm>
#include <vector>
#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/curation_scheduler_dialog.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

std::string trimLeadingSpaces(std::string s) {
    while (!s.empty() && (s.front() == ' ' || s.front() == '\t')) {
        s.erase(s.begin());
    }
    return s;
}

bool extractCurationCommand(const std::string& prompt, std::string* query_out) {
    static const char* kPrefixes[] = {
        "/curate ",
        "策展：",
        "策展:",
        "推荐：",
        "推荐:",
    };
    for (const char* prefix : kPrefixes) {
        const std::string p(prefix);
        if (prompt.rfind(p, 0) == 0) {
            std::string rest = trimLeadingSpaces(prompt.substr(p.size()));
            *query_out = rest.empty() ? prompt : rest;
            return true;
        }
    }
    return false;
}

} // namespace

AiCompanionDock::AiCompanionDock(std::function<void()> on_open_marketplace)
    : brls::Box(brls::Axis::COLUMN),
      on_open_marketplace_(std::move(on_open_marketplace)) {
    setVisibility(brls::Visibility::GONE);
    setBackgroundColor(ui::color("desktop/surface"));
    setBorderThickness(1);
    setBorderColor(ui::color("desktop/line"));
    setPadding(18);
    setWidth(388);

    // Header
    auto* header = new brls::Box(brls::Axis::ROW);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(8);

    auto* title = ui::makeLabel("F8 伴读协作栏", 19.0f, "brls/text");
    header->addView(title);

    auto* close_btn = ui::makeButton("收起", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            toggleVisible();
        }
    }, false, 32.0f);
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

    // AI Curation & Write-Back actions
    auto* curate_box = new brls::Box(brls::Axis::COLUMN);
    curate_box->setMarginBottom(10);
    auto* curate_btn = ui::makeButton(
        "AI 灵感策展 (反写工作台+歌单)",
        [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (auto* plat = brls::Application::getPlatform()) {
                plat->getImeManager()->openForText(
                    [this, alive](std::string text) {
                        brls::sync([this, alive, text = std::move(text)]() {
                            if (!alive->load(std::memory_order_acquire)) return;
                            if (!text.empty()) {
                                submitCurationPrompt(text);
                            }
                        });
                    },
                    "AI 灵感策展 (反写工作台 + 歌单)",
                    "输入心情、主题或场景（如：雨夜写 C++ 来 5 首器乐，以前推过的也可）",
                    512,
                    "");
            }
        },
        true,
        36.0f);
    curate_btn->setMarginBottom(6);
    curate_box->addView(curate_btn);

    auto* curate_row = new brls::Box(brls::Axis::ROW);
    curate_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    playlist_toggle_btn_ = ui::makeButton(
        "同步灵感歌单: 开",
        [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            sync_playlist_enabled_ = !sync_playlist_enabled_;
            if (playlist_toggle_btn_) {
                playlist_toggle_btn_->setText(
                    sync_playlist_enabled_ ? "同步灵感歌单: 开" : "同步灵感歌单: 关");
            }
        },
        false,
        32.0f);
    playlist_toggle_btn_->setGrow(1.0f);
    curate_row->addView(playlist_toggle_btn_);

    auto* rehydrate_btn = ui::makeButton(
        "回灌历史推荐",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                triggerArchiveRehydrate();
            }
        },
        false,
        32.0f);
    rehydrate_btn->setMarginLeft(6);
    curate_row->addView(rehydrate_btn);
    curate_box->addView(curate_row);

    auto* util_curate_row = new brls::Box(brls::Axis::ROW);
    util_curate_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    util_curate_row->setMarginTop(6);
    auto* cal_quick = ui::makeButton(
        "📅 推荐日历",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) CurationCalendarDialog::open();
        },
        false, 30.0f);
    cal_quick->setGrow(1.0f);
    util_curate_row->addView(cal_quick);

    auto* sched_quick = ui::makeButton(
        "⚙ 定时开关",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                CurationSchedulerDialog::open([]() { CurationCalendarDialog::open(); });
            }
        },
        false, 30.0f);
    sched_quick->setGrow(1.0f);
    sched_quick->setMarginLeft(6);
    util_curate_row->addView(sched_quick);
    curate_box->addView(util_curate_row);
    addView(curate_box);

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
        [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (auto* plat = brls::Application::getPlatform()) {
                plat->getImeManager()->openForText(
                    [this, alive](std::string text) {
                        brls::sync([this, alive, text = std::move(text)]() {
                            if (!alive->load(std::memory_order_acquire)) return;
                            if (!text.empty()) {
                                submitPrompt(text);
                            }
                        });
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
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                cancelActiveStream();
            }
        },
        false,
        38.0f);
    stop_btn_->setMarginLeft(8);
    stop_btn_->setVisibility(brls::Visibility::GONE);
    input_row->addView(stop_btn_);

    auto* store_btn = ui::makeButton(
        "插件",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
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

    if (HostKernel::instance().isInitialized()) {
        ephemeral_note_sub_ = HostKernel::instance().bus().subscribe(
            "entity.recommend.ephemeral_note",
            "host.companion_dock",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                brls::sync([this, alive, payload]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    std::string note;
                    if (payload.is_object()) {
                        note = payload.value(
                            "note",
                            payload.value("ephemeral_chat_note",
                                          payload.value("message", payload.value("text", ""))));
                    } else if (payload.is_string()) {
                        note = payload.get<std::string>();
                    }
                    if (!note.empty()) {
                        appendMessage("瞬时伴读提示 (不入库)", note);
                        if (messages_box_) {
                            messages_box_->invalidate();
                        }
                    }
                });
            });
    }
}

AiCompanionDock::~AiCompanionDock() {
    alive_->store(false, std::memory_order_release);
    if (ephemeral_note_sub_ != 0 && HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().unsubscribe(ephemeral_note_sub_);
        ephemeral_note_sub_ = 0;
    }
    if (route_cancel_) {
        route_cancel_->cancel();
        route_cancel_.reset();
    }
    cancelActiveStream();
}

void AiCompanionDock::toggleVisible() {
    open_ = !open_;
    setVisibility(open_ ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    if (open_) {
        if (route_dirty_) {
            refreshAiRouteStatus();
        }
        if (presets_dirty_ || !has_built_presets_) {
            rebuildQuickPresets();
        }
    }
    invalidate();
}

void AiCompanionDock::askWithContext(ContentDomain domain,
                                     const std::string& context_summary,
                                     const std::string& prompt) {
    if (!open_) {
        toggleVisible();
    }
    setDomainContext(domain, context_summary);
    if (!prompt.empty()) {
        submitPrompt(prompt);
    }
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
        setHeight(310);
    } else {
        setHeightPercentage(100);
        setWidth(388);
    }
}

void AiCompanionDock::setDomainContext(ContentDomain domain, const std::string& entity_summary) {
    const bool domain_changed = (domain != domain_);
    if (domain_changed || entity_summary != entity_summary_) {
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
    if (!open_) {
        if (domain_changed || !has_built_presets_) {
            presets_dirty_ = true;
        }
        return;
    }
    if (route_dirty_) {
        refreshAiRouteStatus();
    }
    if (domain_changed || presets_dirty_ || !has_built_presets_) {
        rebuildQuickPresets();
    }
}

void AiCompanionDock::refreshAiRouteStatus() {
    if (!open_) {
        route_dirty_ = true;
        return;
    }
    route_dirty_ = false;
    if (!route_status_label_) {
        return;
    }
    if (route_cancel_) {
        route_cancel_->cancel();
        route_cancel_.reset();
    }
    const uint64_t seq = ++route_req_seq_;

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        return;
    }
    auto& caps = kernel.capabilities();
    auto providers = caps.providersFor("ai.agent.v1");
    if (providers.empty()) {
        providers = caps.providersFor("llm.chat.v1");
    }
    if (!providers.empty()) {
        const std::string base_info = "已接入: " + providers.front().plugin_id;
        route_status_label_->setText(base_info);
        route_status_label_->setTextColor(ui::color("status/ok_fg"));
        if (caps.has("llm.router.v1")) {
            auto cancel = CancelToken::create();
            route_cancel_ = cancel;
            caps.callAsync(
                "llm.router.v1",
                {{"action", "current_route"}},
                [this, alive = alive_, seq, base_info](const CapabilityResult& r) {
                    brls::sync([this, alive, seq, base_info, r]() {
                        if (!alive->load(std::memory_order_acquire) || seq != route_req_seq_ ||
                            !route_status_label_) {
                            return;
                        }
                        route_cancel_.reset();
                        std::string info = base_info;
                        if (r.ok && r.data.is_object() && r.data.contains("summary") &&
                            r.data["summary"].is_string()) {
                            info += " (" + r.data["summary"].get<std::string>() + ")";
                        }
                        route_status_label_->setText(info);
                        route_status_label_->setTextColor(ui::color("status/ok_fg"));
                    });
                },
                cancel);
        }
    } else {
        route_status_label_->setText("未装载 AI 插件 · 请在插件市场接入 plugin-magpie-agent");
        route_status_label_->setTextColor(ui::color("status/warn_fg"));
    }
}

void AiCompanionDock::rebuildQuickPresets() {
    presets_dirty_ = false;
    has_built_presets_ = true;
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
            prompts = {"检索并下载书籍到书架", "对当前 PDF 段落执行 BabelDOC 精译", "拆解当前长难句并给出词根记忆", "提取生词并推入 SM-2 复习卡片"};
            break;
        case ContentDomain::Core:
            prompts = {"诊断已装载能力与依赖拓扑", "读取本机 Magpie 可用路由与余量"};
            break;
    }

    for (const auto& p : prompts) {
        auto* btn = ui::makeButton(p, [this, alive = alive_, p]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (p == "检索并下载书籍到书架") {
                if (auto* plat = brls::Application::getPlatform()) {
                    plat->getImeManager()->openForText(
                        [this, alive](std::string text) {
                            brls::sync([this, alive, text = std::move(text)]() {
                                if (!alive->load(std::memory_order_acquire) || text.empty()) return;
                                submitPrompt("请帮我检索并下载书籍到书架（优先 EPUB/PDF，下载完成后刷新书架）：" + text);
                            });
                        },
                        "AI 检索并下载书籍",
                        "输入书名、作者或 ISBN（自动在 Z-Library / Anna's Archive 间回退检索并下载）",
                        256,
                        "");
                }
                return;
            }
            submitPrompt(p);
        }, false, 36.0f);
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
    std::string curation_query;
    if (extractCurationCommand(prompt, &curation_query)) {
        submitCurationPrompt(curation_query);
        return;
    }
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

void AiCompanionDock::submitCurationPrompt(const std::string& prompt) {
    if (prompt.empty()) {
        return;
    }
    cancelActiveStream();
    appendMessage("你 · 灵感策展", prompt);

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        return;
    }

    auto& caps = kernel.capabilities();
    const bool has_curation = caps.has("ai.curation.v1");
    const bool has_feed = caps.has("recommend.feed.v1");

    if (!has_curation && !has_feed) {
        appendMessage("未安装策展插件",
                      "尚未安装 `plugin-magpie-agent` (`ai.curation.v1`) 或 `plugin-ai-curator` (`recommend.feed.v1`)。\n"
                      "请在插件市场安装后即可直接策展并反写三大工作台与 Apple Music 灵感歌单。");
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
    brls::Label* body_lbl = appendMessage(
        has_curation ? "AI 策展大脑 · 编排中..." : "历史归档策展 · 检索中...",
        "▍",
        &role_lbl);
    active_role_label_ = role_lbl;
    active_body_label_ = body_lbl;
    if (stop_btn_) {
        stop_btn_->setVisibility(brls::Visibility::VISIBLE);
    }

    if (has_curation) {
        nlohmann::json req = {
            {"action", "curate_interactive"},
            {"prompt", prompt},
            {"domain", toString(domain_)},
            {"context", entity_summary_},
            {"repeat_policy", "allow_any"},
            {"sync_playlist", sync_playlist_enabled_},
            {"playlist_name", "OmniDesk AI 灵感电台"},
            {"stream", true},
        };
        caps.callStreamAsync(
            "ai.curation.v1",
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
            [this, alive = alive_, cancel, seq](const CapabilityResult& res) {
                brls::sync([this, alive, cancel, seq, res]() {
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
                                                     ? "AI 策展"
                                                     : ("AI 策展 (" + res.provider_plugin_id + ")");
                    if (res.cancelled || cancel->isCancelled()) {
                        const std::string body = active_stream_text_.empty()
                                                     ? "[已中断策展]"
                                                     : (active_stream_text_ + "\n\n[已中断]");
                        finalizeStreamUi(provider + " · 已中断", body, "font/grey");
                    } else if (res.ok) {
                        std::string final_text;
                        if (res.data.is_object() && res.data.contains("reply") &&
                            res.data["reply"].is_string() &&
                            !res.data["reply"].get_ref<const std::string&>().empty()) {
                            final_text = res.data["reply"].get<std::string>();
                        } else if (!active_stream_text_.empty()) {
                            final_text = active_stream_text_;
                        } else {
                            final_text = res.data.empty() ? "(策展完成)" : res.data.dump(2);
                        }
                        finalizeStreamUi(provider, final_text, "brls/text");
                        if (res.data.is_object() && res.data.contains("ephemeral_chat_note") &&
                            res.data["ephemeral_chat_note"].is_string() &&
                            !res.data["ephemeral_chat_note"].get_ref<const std::string&>().empty()) {
                            appendMessage("瞬时伴读提示 (不入库)",
                                          res.data["ephemeral_chat_note"].get<std::string>());
                        }
                    } else {
                        finalizeStreamUi("策展失败 · " + provider,
                                         res.error.empty() ? "ai.curation.v1 调用失败" : res.error,
                                         "status/err_fg");
                    }
                });
            },
            cancel);
        return;
    }

    // Fallback when only plugin-ai-curator (recommend.feed.v1) is loaded
    caps.callAsync(
        "recommend.feed.v1",
        {{"action", "search_archive"}, {"query", prompt}, {"limit", 8}},
        [this, alive = alive_, cancel, seq, prompt](const CapabilityResult& res) {
            brls::sync([this, alive, cancel, seq, prompt, res]() {
                if (!alive->load(std::memory_order_acquire) || seq != active_request_seq_) {
                    return;
                }
                if (res.cancelled || cancel->isCancelled()) {
                    finalizeStreamUi("历史归档策展 · 已中断", "[已取消]", "font/grey");
                    return;
                }
                if (!res.ok) {
                    finalizeStreamUi("策展检索失败",
                                     res.error.empty() ? "recommend.feed.v1 调用失败" : res.error,
                                     "status/err_fg");
                    return;
                }
                nlohmann::json media_items = nlohmann::json::array();
                nlohmann::json text_items = nlohmann::json::array();
                nlohmann::json book_items = nlohmann::json::array();
                std::string summary = "从已回灌历史归档中找到匹配推荐（未装载 plugin-magpie-agent，仅回退检索）：\n";
                size_t count = 0;
                for (const char* k : {"items", "results", "matches"}) {
                    if (res.data.contains(k) && res.data[k].is_array()) {
                        for (const auto& it : res.data[k]) {
                            if (!it.is_object()) continue;
                            ++count;
                            summary += std::to_string(count) + ". " + it.value("title", "未命名");
                            std::string dt = it.value("business_date", it.value("date", ""));
                            if (!dt.empty()) summary += " [" + dt + "]";
                            summary += "\n";
                            std::string dom = it.value("domain", "");
                            if (dom == "text" || it.contains("url")) text_items.push_back(it);
                            else if (dom == "book" || it.contains("format")) book_items.push_back(it);
                            else media_items.push_back(it);
                        }
                        break;
                    }
                }
                if (count == 0) {
                    summary = "未在本地归档中匹配到与「" + prompt + "」直接相关的历史条目。";
                } else {
                    HostKernel::instance().capabilities().callAsync(
                        "recommend.feed.v1",
                        {
                            {"action", "push_interactive_set"},
                            {"mode", "interactive_ephemeral"},
                            {"title", "归档匹配 · " + prompt},
                            {"user_prompt", prompt},
                            {"repeat_policy", "prefer_familiar"},
                            {"record_permanent_history", false},
                            {"auto_focus", true},
                            {"media_items", media_items},
                            {"text_items", text_items},
                            {"book_items", book_items},
                        },
                        [](const CapabilityResult&) {},
                        cancel);
                }
                finalizeStreamUi("历史归档策展 (plugin-ai-curator)", summary, "brls/text");
            });
        },
        cancel);
}

void AiCompanionDock::triggerArchiveRehydrate() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        return;
    }
    auto& caps = kernel.capabilities();
    if (!caps.has("recommend.feed.v1") && !caps.has("ai.curation.v1")) {
        appendMessage("未安装策展插件",
                      "尚未安装 `plugin-ai-curator` (`recommend.feed.v1`)，无法回灌双仓历史推荐。");
        return;
    }
    appendMessage("历史回灌", "正在同步 obsidian-vault 与 music-daily 双仓并回灌全部历史推荐...");
    const std::string cap = caps.has("recommend.feed.v1") ? "recommend.feed.v1" : "ai.curation.v1";
    const nlohmann::json req = caps.has("recommend.feed.v1")
                                   ? nlohmann::json{{"action", "bootstrap_and_rehydrate"}, {"pull_remote", true}}
                                   : nlohmann::json{{"action", "rehydrate_archive"}, {"pull_remote", true}};
    caps.callAsync(cap, req, [this, alive = alive_](const CapabilityResult& res) {
        brls::sync([this, alive, res]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok) {
                std::string msg = "双仓历史推荐回灌完成。";
                if (res.data.is_object() && res.data.contains("dates_indexed")) {
                    msg += " 已索引 " + std::to_string(res.data.value("dates_indexed", 0)) + " 天归档";
                    if (res.data.contains("music_tracks_hydrated")) {
                        msg += "（曲目 " + std::to_string(res.data.value("music_tracks_hydrated", 0)) + " 首";
                        msg += " / 视频 " + std::to_string(res.data.value("youtube_videos_hydrated", 0)) + " 条）";
                    }
                    msg += "。";
                }
                appendMessage("历史回灌完成", msg);
            } else {
                appendMessage("历史回灌失败", res.error.empty() ? "回灌调用失败" : res.error);
            }
            if (messages_box_) {
                messages_box_->invalidate();
            }
        });
    });
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
    auto* card = ui::makeCard("desktop/soft", 12.0f, 10.0f);
    card->setMarginBottom(10);

    auto* r_lbl = ui::makeLabel(role, 13.5f, "brls/accent");
    r_lbl->setMarginBottom(4);
    card->addView(r_lbl);
    if (role_label_out) {
        *role_label_out = r_lbl;
    }

    auto* t_lbl = ui::makeLabel(text, 15.5f, "brls/text", 1.5f);
    card->addView(t_lbl);

    messages_box_->addView(card);
    return t_lbl;
}

} // namespace omnidesk::host
