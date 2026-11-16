#include "omnidesk/selection_action_popover.hpp"

#include <SDL2/SDL.h>
#include <algorithm>
#include <atomic>
#include <borealis.hpp>
#include <cctype>
#include <cstdio>
#include <memory>
#include <set>
#include <sstream>
#include <thread>

#include "omnidesk/async.hpp"
#include "omnidesk/capabilities.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/ui_task_runner.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk {
namespace {

std::string asciiLower(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

std::string truncatePreview(const std::string& s, size_t max_bytes) {
    if (s.size() <= max_bytes) return s;
    size_t cut = max_bytes > 3 ? max_bytes - 3 : max_bytes;
    while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0) == 0x80) {
        --cut;
    }
    return s.substr(0, cut) + "...";
}

std::string formatClipTime(double sec) {
    const int t = std::max(0, static_cast<int>(sec));
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", t / 60, t % 60);
    return std::string(buf);
}

class PopoverRootBox final : public brls::Box {
public:
    explicit PopoverRootBox(SelectionContext ctx)
        : brls::Box(brls::Axis::COLUMN),
          ctx_(std::move(ctx)) {
        setPadding(22);
        setWidth(640);

        if (ctx_.selected_text.empty()) {
            auto tokens = SelectionActionPopover::extractSelectableTokens(ctx_.context_sentence, 1);
            if (!tokens.empty()) {
                ctx_.selected_text = tokens.front();
            } else {
                ctx_.selected_text = truncatePreview(ctx_.context_sentence, 120);
            }
        }

        auto* hdr = new brls::Box(brls::Axis::ROW);
        hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        hdr->setAlignItems(brls::AlignItems::CENTER);
        hdr->setMarginBottom(12);
        hdr->addView(ui::makeLabel("划词动作菜单 · Selection Actions", 20.0f, "brls/text"));
        hdr->addView(ui::makePill(toDisplayLabel(ctx_.domain), "desktop/soft", "brls/accent"));
        addView(hdr);

        auto* sel_card = ui::makeCard("desktop/soft", 14.0f, 10.0f);
        sel_card->setMarginBottom(12);
        auto* sel_hdr = new brls::Box(brls::Axis::ROW);
        sel_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        sel_hdr->setAlignItems(brls::AlignItems::CENTER);
        sel_hdr->addView(ui::makeLabel("当前选中文本", 13.5f, "font/grey"));
        sel_hdr->addView(ui::makeButton("编辑选词", [this]() { editSelection(); }, false, 30.0f));
        sel_card->addView(sel_hdr);

        selected_label_ = ui::makeLabel(truncatePreview(ctx_.selected_text, 220), 17.0f, "brls/text", 1.45f);
        selected_label_->setMarginTop(8);
        sel_card->addView(selected_label_);

        if (!ctx_.context_sentence.empty() && ctx_.context_sentence != ctx_.selected_text) {
            auto* ctx_lbl = ui::makeLabel(
                "语境: " + truncatePreview(ctx_.context_sentence, 200), 13.5f, "font/grey", 1.42f);
            ctx_lbl->setMarginTop(6);
            sel_card->addView(ctx_lbl);
        }
        addView(sel_card);

        const std::string token_src = !ctx_.context_sentence.empty() ? ctx_.context_sentence : ctx_.selected_text;
        const auto tokens = SelectionActionPopover::extractSelectableTokens(token_src, 10);
        if (!tokens.empty()) {
            auto* chip_lbl = ui::makeLabel("快速点选词条:", 13.5f, "font/grey");
            chip_lbl->setMarginBottom(6);
            addView(chip_lbl);

            chips_box_ = new brls::Box(brls::Axis::COLUMN);
            chips_box_->setMarginBottom(12);
            rebuildChips(tokens);
            addView(chips_box_);
        }

        auto* actions1 = new brls::Box(brls::Axis::ROW);
        actions1->setMarginBottom(8);
        auto* vocab_btn = ui::makeButton("送生词本 (SM-2)", [this]() { sendToVocab(); }, true, 36.0f);
        vocab_btn->setMarginRight(8);
        actions1->addView(vocab_btn);

        auto* ai_btn = ui::makeButton("送 AI 伴读 (F8)", [this]() { sendToAiCompanion(); }, false, 36.0f);
        ai_btn->setMarginRight(8);
        actions1->addView(ai_btn);

        auto* clip_btn = ui::makeButton("搜飞牛台词", [this]() { searchNasClips(); }, false, 36.0f);
        actions1->addView(clip_btn);
        addView(actions1);

        auto* actions2 = new brls::Box(brls::Axis::ROW);
        actions2->setMarginBottom(10);
        auto* tr_btn = ui::makeButton("即刻翻译", [this]() { translateSelection(); }, false, 34.0f);
        tr_btn->setMarginRight(8);
        actions2->addView(tr_btn);

        auto* search_btn = ui::makeButton("全局搜索 (Ctrl+K)", [this]() { triggerGlobalSearch(); }, false, 34.0f);
        search_btn->setMarginRight(8);
        actions2->addView(search_btn);

        auto* copy_btn = ui::makeButton("复制", [this]() {
            if (!ctx_.selected_text.empty()) {
                SDL_SetClipboardText(ctx_.selected_text.c_str());
                brls::Application::notify("已复制选中文本");
            }
        }, false, 34.0f);
        actions2->addView(copy_btn);
        addView(actions2);

        result_box_ = new brls::Box(brls::Axis::COLUMN);
        addView(result_box_);
    }

    ~PopoverRootBox() override {
        alive_->store(false, std::memory_order_release);
        if (pending_cancel_) {
            pending_cancel_->cancel();
        }
    }

    void setDialog(brls::Dialog* dlg) { dialog_ = dlg; }

private:
    void rebuildChips(const std::vector<std::string>& tokens) {
        if (!chips_box_) return;
        chips_box_->clearViews();
        brls::Box* row = nullptr;
        for (size_t i = 0; i < tokens.size(); ++i) {
            if (i % 5 == 0) {
                row = new brls::Box(brls::Axis::ROW);
                if (i > 0) row->setMarginTop(6);
                chips_box_->addView(row);
            }
            const bool active = (asciiLower(tokens[i]) == asciiLower(ctx_.selected_text));
            auto* btn = ui::makeButton(tokens[i], [this, tok = tokens[i], tokens]() {
                ctx_.selected_text = tok;
                if (selected_label_) {
                    selected_label_->setText(truncatePreview(ctx_.selected_text, 220));
                }
                rebuildChips(tokens);
            }, active, 30.0f);
            btn->setMarginRight(6);
            row->addView(btn);
        }
    }

    void editSelection() {
        if (auto* plat = brls::Application::getPlatform()) {
            plat->getImeManager()->openForText(
                [this, alive = alive_](std::string text) {
                    brls::sync([this, alive, text = std::move(text)]() {
                        if (!alive->load(std::memory_order_acquire) || text.empty()) return;
                        ctx_.selected_text = text;
                        if (selected_label_) {
                            selected_label_->setText(truncatePreview(ctx_.selected_text, 220));
                        }
                    });
                },
                "编辑选中词条 / 句子",
                "支持单字、词组或完整长难句",
                256,
                ctx_.selected_text);
        }
    }

    void showStatusCard(const std::string& title, const std::string& body, const char* color_tok) {
        if (!result_box_) return;
        result_box_->clearViews();
        auto* card = ui::makeCard("desktop/soft", 14.0f, 10.0f);
        card->addView(ui::makeLabel(title, 15.0f, color_tok ? color_tok : "brls/accent"));
        if (!body.empty()) {
            auto* lbl = ui::makeLabel(body, 14.5f, "brls/text", 1.50f);
            lbl->setMarginTop(6);
            card->addView(lbl);
        }
        result_box_->addView(card);
    }

    template <typename Fn>
    void runAsyncTask(const std::string& lane, Fn fn) {
        if (pending_cancel_) pending_cancel_->cancel();
        auto cancel = CancelToken::create();
        pending_cancel_ = cancel;
        if (ctx_.ui_tasks) {
            ctx_.ui_tasks->submitInLane(lane, [fn = std::move(fn)](const auto& c) mutable { fn(c); }, cancel);
        } else {
            std::thread([fn = std::move(fn), cancel]() mutable { fn(cancel); }).detach();
        }
    }

    void sendToVocab() {
        if (ctx_.selected_text.empty()) return;
        const std::string word = ctx_.selected_text;
        const std::string sentence = ctx_.context_sentence.empty() ? word : ctx_.context_sentence;
        const std::string source_title = ctx_.source_title;
        const std::string book_id = ctx_.book_id;
        auto* caps = ctx_.capabilities;
        auto* bus = ctx_.bus;

        if (bus) {
            bus->publish("entity.vocab.add", {
                {"word", word},
                {"context_sentence", sentence},
                {"source_title", source_title},
                {"source_book", source_title},
                {"book_id", book_id},
            });
        }

        if (caps && caps->has("vocab.sm2.v1")) {
            showStatusCard("正在写入 SM-2 生词本...", word, "font/grey");
            runAsyncTask("popover.vocab", [this, alive = alive_, caps, word, sentence, source_title, book_id](
                                              const std::shared_ptr<CancelToken>& c) {
                auto res = caps->callCancellable("vocab.sm2.v1", {
                    {"action", "add"},
                    {"word", word},
                    {"context_sentence", sentence},
                    {"source_book", source_title},
                    {"book_id", book_id},
                }, c);
                brls::sync([this, alive, res, word]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (res.ok) {
                        showStatusCard("已加入 SM-2 生词本 (vocab.sm2.v1)",
                                       "词条: " + word, "status/ok_fg");
                    } else {
                        showStatusCard("生词本写入失败", res.error, "status/warn_fg");
                    }
                });
            });
        } else {
            showStatusCard("已提交生词事件 (entity.vocab.add)",
                           "词条 \"" + word + "\" 已入列（安装 plugin-ebook 后自动同步 SM-2 复习卡片）",
                           "status/ok_fg");
        }
    }

    void sendToAiCompanion() {
        if (ctx_.selected_text.empty()) return;
        const std::string prompt =
            "请结合上下文精讲并拆解这段内容（含核心含义、语境用法与关键难点）：\n" +
            ctx_.selected_text;
        const std::string context =
            ctx_.source_title + (ctx_.context_sentence.empty() ? "" : (" | " + ctx_.context_sentence));
        if (ctx_.on_send_to_ai) {
            auto fn = ctx_.on_send_to_ai;
            if (dialog_) dialog_->dismiss();
            fn(prompt, context);
            return;
        }
        if (ctx_.bus) {
            ctx_.bus->publish("entity.ai.companion_ask", {
                {"domain", toString(ctx_.domain)},
                {"prompt", prompt},
                {"context", context},
                {"selected_text", ctx_.selected_text},
            });
            if (dialog_) dialog_->dismiss();
            return;
        }
        showStatusCard("AI 伴读未连接", "请先开启宿主伴读栏或安装 plugin-magpie-agent", "status/warn_fg");
    }

    void searchNasClips() {
        if (ctx_.selected_text.empty()) return;
        auto* caps = ctx_.capabilities;
        auto* bus = ctx_.bus;
        if (!caps || !caps->has("media.clip_jump.v1")) {
            showStatusCard("未检测到飞牛 NAS 台词检索能力",
                           "安装并启用 plugin-fnos-nas (media.clip_jump.v1) 后可一键搜台词跳播原片。",
                           "status/warn_fg");
            return;
        }
        const std::string query = ctx_.selected_text;
        showStatusCard("正在检索飞牛影库台词...", query, "font/grey");
        runAsyncTask("popover.clip_jump", [this, alive = alive_, caps, bus, query](
                                              const std::shared_ptr<CancelToken>& c) {
            auto res = caps->callCancellable("media.clip_jump.v1", {
                {"query", query},
                {"limit", 4},
                {"auto_open_player", false},
            }, c);
            brls::sync([this, alive, bus, query, res]() {
                if (!alive->load(std::memory_order_acquire) || !result_box_) return;
                result_box_->clearViews();
                if (!res.ok) {
                    showStatusCard("飞牛台词检索失败", res.error, "status/err_fg");
                    return;
                }
                const auto clips = res.data.value("clips", nlohmann::json::array());
                if (!clips.is_array() || clips.empty()) {
                    showStatusCard("未找到匹配台词", "关键词: " + query, "font/grey");
                    return;
                }
                auto* wrap = ui::makeCard("desktop/soft", 12.0f, 8.0f);
                wrap->addView(ui::makeLabel(
                    "飞牛影库台词命中 (" + std::to_string(clips.size()) + " 条)",
                    14.5f, "domain/media"));
                for (const auto& clip : clips) {
                    if (!clip.is_object()) continue;
                    const std::string series = clip.value("series", clip.value("title", "NAS 视频"));
                    const std::string ep = clip.value("episode", "");
                    const double start_sec = clip.value("start_sec", 0.0);
                    const double end_sec = clip.value("end_sec", start_sec + 5.0);
                    const std::string line_en = clip.value("line_en", "");
                    const std::string line_zh = clip.value("line_zh", "");
                    const std::string play_url = clip.value("play_url", "");

                    auto* row = ui::makeCard("desktop/surface", 10.0f, 8.0f);
                    row->setMarginTop(8);
                    row->addView(ui::makeLabel(
                        series + (ep.empty() ? "" : (" " + ep)) + " · " + formatClipTime(start_sec),
                        13.5f, "domain/media"));
                    if (!line_en.empty()) row->addView(ui::makeLabel(line_en, 13.5f, "brls/text", 1.35f));
                    if (!line_zh.empty()) row->addView(ui::makeLabel(line_zh, 13.0f, "font/grey", 1.35f));
                    if (!play_url.empty() && bus) {
                        auto* jump = ui::makeButton("▶ 跳播原片片段", [this, bus, series, ep, play_url, start_sec, end_sec]() {
                            bus->publish("entity.media.open", {
                                {"item_id", "clip:" + series + ":" + std::to_string(static_cast<int>(start_sec))},
                                {"source_id", "plugin-fnos-nas"},
                                {"title", series + (ep.empty() ? "" : (" " + ep))},
                                {"kind", "nas"},
                                {"source_badge", "飞牛 NAS · 台词片段"},
                                {"play_url", play_url},
                                {"start_sec", start_sec},
                                {"end_sec", end_sec},
                            });
                            if (dialog_) dialog_->dismiss();
                        }, true, 28.0f);
                        jump->setMarginTop(6);
                        row->addView(jump);
                    }
                    wrap->addView(row);
                }
                result_box_->addView(wrap);
            });
        });
    }

    void translateSelection() {
        if (ctx_.selected_text.empty()) return;
        auto* caps = ctx_.capabilities;
        if (!caps || (!caps->has("llm.translate.v1") && !caps->has("llm.chat.v1"))) {
            showStatusCard("未检测到翻译插件",
                           "请安装 plugin-magpie-agent (llm.translate.v1)", "status/warn_fg");
            return;
        }
        const std::string text = ctx_.selected_text;
        const std::string context = ctx_.context_sentence;
        showStatusCard("正在调用 AI 翻译...", text, "font/grey");
        runAsyncTask("popover.translate", [this, alive = alive_, caps, text, context](
                                              const std::shared_ptr<CancelToken>& c) {
            CapabilityResult res;
            if (caps->has("llm.translate.v1")) {
                res = caps->callCancellable("llm.translate.v1", {
                    {"text", text},
                    {"source_lang", "auto"},
                    {"target_lang", "zh-CN"},
                    {"context", context},
                }, c);
            } else {
                res = caps->callCancellable("llm.chat.v1", {
                    {"domain", "text"},
                    {"prompt", "请将以下内容精准翻译为简体中文（仅输出译文）：\n" + text},
                }, c);
            }
            brls::sync([this, alive, res]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (!res.ok) {
                    showStatusCard("翻译失败", res.error, "status/err_fg");
                    return;
                }
                std::string tr = res.data.value("translated", res.data.value("reply", ""));
                showStatusCard("翻译结果", tr.empty() ? res.data.dump(2) : tr, "status/ok_fg");
            });
        });
    }

    void triggerGlobalSearch() {
        const std::string q = ctx_.selected_text;
        if (ctx_.on_search_global) {
            auto fn = ctx_.on_search_global;
            if (dialog_) dialog_->dismiss();
            fn(q);
            return;
        }
        if (ctx_.bus) {
            if (dialog_) dialog_->dismiss();
            ctx_.bus->publish("entity.search.open", {{"query", q}});
        }
    }

    SelectionContext ctx_;
    brls::Dialog* dialog_ = nullptr;
    brls::Label* selected_label_ = nullptr;
    brls::Box* chips_box_ = nullptr;
    brls::Box* result_box_ = nullptr;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> pending_cancel_;
};

} // namespace

std::vector<std::string> SelectionActionPopover::extractSelectableTokens(const std::string& text,
                                                                         size_t max_tokens) {
    static const std::set<std::string> kStopWords = {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "had", "has", "have", "he", "her", "his", "in", "is", "it", "its",
        "of", "on", "or", "she", "that", "the", "their", "them", "they", "this",
        "to", "was", "we", "were", "with", "you", "your",
    };

    std::vector<std::string> out;
    std::set<std::string> seen;
    std::string cur;
    auto flushToken = [&]() {
        if (cur.size() >= 3) {
            std::string low = asciiLower(cur);
            if (kStopWords.count(low) == 0 && seen.insert(low).second) {
                out.push_back(cur);
            }
        }
        cur.clear();
    };

    for (unsigned char ch : text) {
        if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
            (ch >= '0' && ch <= '9') || ch == '-' || ch == '_' || ch == '\'') {
            if (!cur.empty() || (ch != '-' && ch != '_' && ch != '\'')) {
                cur.push_back(static_cast<char>(ch));
            }
        } else {
            flushToken();
            if (out.size() >= max_tokens) break;
        }
    }
    if (out.size() < max_tokens) {
        flushToken();
    }
    return out;
}

void SelectionActionPopover::show(SelectionContext ctx) {
    if (!brls::Application::getPlatform()) {
        return;
    }
    auto* body = new PopoverRootBox(std::move(ctx));
    auto* dlg = new brls::Dialog(body);
    body->setDialog(dlg);
    dlg->addButton("关闭", [] {});
    dlg->open();
}

} // namespace omnidesk
