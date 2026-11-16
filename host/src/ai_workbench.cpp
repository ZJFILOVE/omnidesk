#include "omnidesk/host/ai_workbench.hpp"

#include <SDL2/SDL.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>

#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/curation_scheduler_dialog.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/recycling_grid.hpp"
#include "omnidesk/host/svg_view.hpp"
#include "omnidesk/markdown_view.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

uint64_t nowSteadyMs() {
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch())
            .count());
}

int64_t nowUnixSec() {
    return static_cast<int64_t>(std::time(nullptr));
}

std::string nvgHex(NVGcolor col) {
    const int r = std::clamp(static_cast<int>(std::lround(col.r * 255.0f)), 0, 255);
    const int g = std::clamp(static_cast<int>(std::lround(col.g * 255.0f)), 0, 255);
    const int b = std::clamp(static_cast<int>(std::lround(col.b * 255.0f)), 0, 255);
    char buf[16];
    std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", r, g, b);
    return std::string(buf);
}

std::string svgPlus(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<line x1=\"12\" y1=\"5\" x2=\"12\" y2=\"19\"/>"
           "<line x1=\"5\" y1=\"12\" x2=\"19\" y2=\"12\"/>"
           "</svg>";
}

std::string svgSidebarToggle(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"1.9\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<rect x=\"3\" y=\"3\" width=\"18\" height=\"18\" rx=\"3\"/>"
           "<line x1=\"9\" y1=\"3\" x2=\"9\" y2=\"21\"/>"
           "</svg>";
}

std::string svgAiEmblem(const std::string& primary_hex, const std::string& secondary_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"42\" height=\"42\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<path d=\"M12 2.5l2.3 6.2L20.5 11l-6.2 2.3L12 19.5l-2.3-6.2L3.5 11l6.2-2.3L12 2.5z\" "
           "stroke=\"" + primary_hex + "\" stroke-width=\"1.8\"/>"
           "<path d=\"M19 15.5l.9 2.2 2.1.8-2.1.9-.9 2.1-.8-2.1-2.2-.9 2.2-.8.8-2.2z\" "
           "stroke=\"" + secondary_hex + "\" stroke-width=\"1.7\"/>"
           "</svg>";
}

std::string svgDeleteX(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"13\" height=\"13\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<line x1=\"18\" y1=\"6\" x2=\"6\" y2=\"18\"/>"
           "<line x1=\"6\" y1=\"6\" x2=\"18\" y2=\"18\"/>"
           "</svg>";
}

std::string trimSpaces(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && (s[b] == ' ' || s[b] == '\t' || s[b] == '\r' || s[b] == '\n')) {
        ++b;
    }
    size_t e = s.size();
    while (e > b && (s[e - 1] == ' ' || s[e - 1] == '\t' || s[e - 1] == '\r' || s[e - 1] == '\n')) {
        --e;
    }
    return s.substr(b, e - b);
}

bool extractCurationPrefix(const std::string& prompt, std::string* query_out) {
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
            std::string rest = trimSpaces(prompt.substr(p.size()));
            *query_out = rest.empty() ? prompt : rest;
            return true;
        }
    }
    return false;
}

std::string svgDockRight(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"15\" height=\"15\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"1.9\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<rect x=\"3\" y=\"3\" width=\"18\" height=\"18\" rx=\"3\"/>"
           "<line x1=\"15\" y1=\"3\" x2=\"15\" y2=\"21\"/>"
           "</svg>";
}

std::string summarizeSessionTitle(const std::string& first_prompt) {
    return summarizeAiSessionTitle(first_prompt);
}

void splitThinkAndReply(const std::string& raw,
                        const std::string& explicit_reasoning,
                        std::string* reasoning_out,
                        std::string* reply_out,
                        bool* inside_open_think) {
    splitAiThinkAndReply(raw, explicit_reasoning, reasoning_out, reply_out, inside_open_think);
}

std::string formatDurationSeconds(double sec) {
    if (sec <= 0.05) return "";
    std::ostringstream oss;
    oss << std::fixed << std::setprecision(1) << sec;
    return oss.str();
}

std::string makeSessionId() {
    return makeAiChatSessionId();
}

std::filesystem::path sessionsFilePath() {
    if (HostKernel::instance().isInitialized() && !HostKernel::instance().dataRoot().empty()) {
        return std::filesystem::path(HostKernel::instance().dataRoot()) / "ai_chat_sessions.json";
    }
    const char* home = std::getenv("HOME");
    if (home && *home) {
        return std::filesystem::path(home) / ".local/share/omnidesk/ai_chat_sessions.json";
    }
    return std::filesystem::temp_directory_path() / "omnidesk_ai_chat_sessions.json";
}

class AiSessionRowCell final : public RecyclingGridCell {
public:
    AiSessionRowCell() : RecyclingGridCell(brls::Axis::ROW) {
        setWidthPercentage(100);
        setHeight(38.0f);
        setPadding(0, 8, 0, 10);
        setCornerRadius(9.0f);
        setHighlightCornerRadius(9.0f);
        setFocusable(true);
        setAlignItems(brls::AlignItems::CENTER);
        setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

        auto* left = new brls::Box(brls::Axis::ROW);
        left->setAlignItems(brls::AlignItems::CENTER);
        left->setShrink(1.0f);
        left->setGrow(1.0f);

        curation_tag_ = ui::makePill("策展", "desktop/active", "brls/accent");
        curation_tag_->setMarginRight(6.0f);
        curation_tag_->setShrink(0.0f);
        left->addView(curation_tag_);

        title_label_ = ui::makeLabel("新对话", 13.8f, "font/grey");
        title_label_->setSingleLine(true);
        title_label_->setShrink(1.0f);
        left->addView(title_label_);
        addView(left);

        del_btn_ = new brls::Box(brls::Axis::ROW);
        del_btn_->setDimensions(24.0f, 24.0f);
        del_btn_->setMarginLeft(4.0f);
        del_btn_->setCornerRadius(6.0f);
        del_btn_->setHighlightCornerRadius(6.0f);
        del_btn_->setFocusable(true);
        del_btn_->setShrink(0.0f);
        del_btn_->setAlignItems(brls::AlignItems::CENTER);
        del_btn_->setJustifyContent(brls::JustifyContent::CENTER);
        del_icon_ = new SvgIconView();
        del_icon_->setDimensions(12, 12);
        del_btn_->addView(del_icon_);
        addView(del_btn_);

        registerClickAction([this](brls::View*) {
            if (on_select_) on_select_();
            return true;
        });
        del_btn_->registerClickAction([this](brls::View*) {
            if (on_delete_) on_delete_();
            return true;
        });
    }

    void bind(const AiChatSession& s,
              bool active,
              bool show_delete,
              std::function<void()> on_select,
              std::function<void()> on_delete) {
        on_select_ = std::move(on_select);
        on_delete_ = std::move(on_delete);

        setBackgroundColor(ui::color(active ? "desktop/soft" : "desktop/surface"));
        setBorderThickness(active ? 1.0f : 0.0f);
        if (active) {
            setBorderColor(ui::color("desktop/line"));
        }

        curation_tag_->setVisibility(s.curation_mode ? brls::Visibility::VISIBLE
                                                     : brls::Visibility::GONE);
        title_label_->setText(s.title.empty() ? "新对话" : s.title);
        title_label_->setTextColor(ui::color(active ? "brls/text" : "font/grey"));

        del_btn_->setVisibility(show_delete ? brls::Visibility::VISIBLE
                                            : brls::Visibility::GONE);
        if (show_delete) {
            del_icon_->setSvgString(svgDeleteX(nvgHex(ui::color("font/grey"))));
        }
    }

private:
    brls::Box* curation_tag_ = nullptr;
    brls::Label* title_label_ = nullptr;
    brls::Box* del_btn_ = nullptr;
    SvgIconView* del_icon_ = nullptr;
    std::function<void()> on_select_;
    std::function<void()> on_delete_;
};

class AiSessionsDataSource final : public RecyclingGridDataSource {
public:
    using SelectFn = std::function<void(size_t)>;
    using DeleteFn = std::function<void(size_t)>;

    AiSessionsDataSource(const std::vector<AiChatSession>* sessions,
                         const size_t* active_idx,
                         SelectFn on_select,
                         DeleteFn on_delete)
        : sessions_(sessions),
          active_idx_(active_idx),
          on_select_(std::move(on_select)),
          on_delete_(std::move(on_delete)) {}

    size_t getItemCount() override {
        return sessions_ ? sessions_->size() : 0;
    }

    RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) override {
        auto* cell = static_cast<AiSessionRowCell*>(recycler->dequeueReusableCell("session_row"));
        if (!cell) cell = new AiSessionRowCell();
        if (!sessions_ || index >= sessions_->size()) return cell;

        const auto& s = (*sessions_)[index];
        const bool active = active_idx_ && (*active_idx_ == index);
        const bool show_del = (sessions_->size() > 1 || !s.messages.empty());
        cell->bind(
            s,
            active,
            show_del,
            [cb = on_select_, index]() {
                if (cb) cb(index);
            },
            [cb = on_delete_, index]() {
                if (cb) cb(index);
            });
        return cell;
    }

private:
    const std::vector<AiChatSession>* sessions_ = nullptr;
    const size_t* active_idx_ = nullptr;
    SelectFn on_select_;
    DeleteFn on_delete_;
};

constexpr uint64_t kStreamUiFlushIntervalMs = 32;

} // namespace

std::string makeAiChatSessionId() {
    static std::atomic<uint64_t> counter{1};
    return "sess-" + std::to_string(nowUnixSec()) + "-" +
           std::to_string(counter.fetch_add(1, std::memory_order_relaxed));
}

std::string summarizeAiSessionTitle(const std::string& first_prompt) {
    std::string single;
    single.reserve(first_prompt.size());
    for (char c : first_prompt) {
        if (c == '\n' || c == '\r' || c == '\t') {
            if (!single.empty() && single.back() != ' ') single.push_back(' ');
        } else {
            single.push_back(c);
        }
    }
    single = trimSpaces(single);
    if (single.empty()) return "新对话";

    size_t cp = 0;
    size_t i = 0;
    while (i < single.size() && cp < 22) {
        const unsigned char ch = static_cast<unsigned char>(single[i]);
        size_t adv = 1;
        if ((ch & 0x80u) == 0) adv = 1;
        else if ((ch & 0xE0u) == 0xC0u) adv = 2;
        else if ((ch & 0xF0u) == 0xE0u) adv = 3;
        else if ((ch & 0xF8u) == 0xF0u) adv = 4;
        if (i + adv > single.size()) break;
        i += adv;
        ++cp;
    }
    if (i < single.size()) {
        return single.substr(0, i) + "…";
    }
    return single;
}

void splitAiThinkAndReply(const std::string& raw,
                          const std::string& explicit_reasoning,
                          std::string* reasoning_out,
                          std::string* reply_out,
                          bool* inside_open_think) {
    std::string thought = explicit_reasoning;
    std::string rest = raw;
    bool open_think = false;

    while (true) {
        const size_t open_pos = rest.find("<think>");
        if (open_pos == std::string::npos) {
            break;
        }
        const size_t after_open = open_pos + 7;
        const size_t close_pos = rest.find("</think>", after_open);
        if (close_pos == std::string::npos) {
            const std::string inner = rest.substr(after_open);
            if (!thought.empty() && !inner.empty()) thought += "\n";
            thought += inner;
            rest = rest.substr(0, open_pos);
            open_think = true;
            break;
        }
        const std::string inner = rest.substr(after_open, close_pos - after_open);
        if (!thought.empty() && !inner.empty()) thought += "\n";
        thought += inner;
        rest = rest.substr(0, open_pos) + rest.substr(close_pos + 8);
    }

    if (reasoning_out) *reasoning_out = trimSpaces(thought);
    if (reply_out) *reply_out = trimSpaces(rest);
    if (inside_open_think) {
        *inside_open_think = open_think;
    }
}

std::vector<AiChatSession> loadAiChatSessionsFromDisk() {
    std::vector<AiChatSession> sessions;
    const auto path = sessionsFilePath();
    std::error_code ec;
    if (std::filesystem::exists(path, ec)) {
        std::ifstream ifs(path);
        if (ifs) {
            nlohmann::json j = nlohmann::json::parse(ifs, nullptr, false);
            if (j.is_object() && j.contains("sessions") && j["sessions"].is_array()) {
                for (const auto& sj : j["sessions"]) {
                    if (!sj.is_object()) continue;
                    AiChatSession s;
                    s.id = sj.value("id", makeAiChatSessionId());
                    s.title = sj.value("title", "新对话");
                    s.domain = parseContentDomain(sj.value("domain", "core"));
                    s.context_summary = sj.value("context_summary", "");
                    s.curation_mode = sj.value("curation_mode", false);
                    s.deep_thinking = sj.value("deep_thinking", false);
                    s.updated_at = sj.value("updated_at", nowUnixSec());
                    if (sj.contains("messages") && sj["messages"].is_array()) {
                        for (const auto& mj : sj["messages"]) {
                            if (!mj.is_object()) continue;
                            AiChatMessage m;
                            m.role = mj.value("role", "assistant");
                            m.display_role = mj.value("display_role", "AI");
                            m.content = mj.value("content", "");
                            m.reasoning = mj.value("reasoning", "");
                            m.thinking_sec = mj.value("thinking_sec", 0.0);
                            m.curation = mj.value("curation", false);
                            m.error = mj.value("error", false);
                            m.thinking_collapsed = !m.reasoning.empty();
                            s.messages.push_back(std::move(m));
                        }
                    }
                    sessions.push_back(std::move(s));
                }
            }
        }
    }

    if (sessions.empty()) {
        AiChatSession fresh;
        fresh.id = makeAiChatSessionId();
        fresh.title = "新对话";
        fresh.updated_at = nowUnixSec();
        sessions.push_back(std::move(fresh));
    }
    return sessions;
}

void saveAiChatSessionsToDisk(const std::vector<AiChatSession>& sessions) {
    const auto path = sessionsFilePath();
    std::error_code ec;
    std::filesystem::create_directories(path.parent_path(), ec);

    nlohmann::json arr = nlohmann::json::array();
    size_t persisted = 0;
    for (const auto& s : sessions) {
        if (s.messages.empty() && persisted > 0) {
            continue;
        }
        nlohmann::json msgs = nlohmann::json::array();
        for (const auto& m : s.messages) {
            msgs.push_back({
                {"role", m.role},
                {"display_role", m.display_role},
                {"content", m.content},
                {"reasoning", m.reasoning},
                {"thinking_sec", m.thinking_sec},
                {"curation", m.curation},
                {"error", m.error},
            });
        }
        arr.push_back({
            {"id", s.id},
            {"title", s.title},
            {"domain", toString(s.domain)},
            {"context_summary", s.context_summary},
            {"curation_mode", s.curation_mode},
            {"deep_thinking", s.deep_thinking},
            {"updated_at", s.updated_at},
            {"messages", std::move(msgs)},
        });
        if (++persisted >= 60) {
            break;
        }
    }

    const nlohmann::json root = {{"version", 1}, {"sessions", std::move(arr)}};
    const auto tmp = path.string() + ".tmp";
    {
        std::ofstream ofs(tmp, std::ios::trunc);
        if (!ofs) return;
        ofs << root.dump(2);
    }
    std::filesystem::rename(tmp, path, ec);
}

void notifyAiSessionsUpdated(const std::string& origin, const std::string& active_session_id) {
    if (!HostKernel::instance().isInitialized()) return;
    HostKernel::instance().bus().publish("entity.ai.sessions_updated", {
        {"origin", origin},
        {"active_session_id", active_session_id},
    });
}

void openAiModelPickerDialog(const std::string& current_route_model,
                             std::function<void()> on_open_marketplace,
                             std::function<void(const std::string&)> on_selected) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("llm.router.v1")) {
        if (on_open_marketplace) {
            on_open_marketplace();
        }
        return;
    }

    kernel.capabilities().callAsync(
        "llm.router.v1",
        {{"action", "list_models"}},
        [current_route_model, on_selected = std::move(on_selected)](const CapabilityResult& res) {
            brls::sync([current_route_model, on_selected, res]() {
                auto* box = new brls::Box(brls::Axis::COLUMN);
                box->setPadding(22);
                box->setWidth(620);

                box->addView(ui::makeLabel("选择 AI 基座与模型 (支持 agy-cli 直连免 Magpie)", 19.0f, "brls/text"));
                auto* sub = ui::makeLabel(
                    "agy-cli 基座直连本机官方 Antigravity CLI（自动剥离 Magpie 端口）；Magpie 基座仅作用于专用路由组",
                    13.2f,
                    "font/grey");
                sub->setMarginTop(4.0f);
                sub->setMarginBottom(10.0f);
                box->addView(sub);

                auto* tabs_row = new brls::Box(brls::Axis::ROW);
                tabs_row->setAlignItems(brls::AlignItems::CENTER);
                tabs_row->setMarginBottom(10.0f);
                box->addView(tabs_row);

                auto* scroll = new brls::ScrollingFrame();
                scroll->setWidthPercentage(100);
                scroll->setHeight(330);
                auto* list = new brls::Box(brls::Axis::COLUMN);
                list->setWidthPercentage(100);
                scroll->setContentView(list);
                box->addView(scroll);

                auto* dlg = new brls::Dialog(box);

                struct PickerState {
                    std::string tab = "agy-cli"; // "agy-cli" | "magpie"
                    nlohmann::json agy_models = nlohmann::json::array();
                    nlohmann::json magpie_models = nlohmann::json::array();
                    bool agy_available = false;
                };
                auto st = std::make_shared<PickerState>();
                if (res.data.is_object()) {
                    st->agy_available = res.data.value("agy_available", false);
                    if (res.data.contains("agy_models") && res.data["agy_models"].is_array()) {
                        st->agy_models = res.data["agy_models"];
                    }
                    if (res.data.contains("models") && res.data["models"].is_array()) {
                        st->magpie_models = res.data["models"];
                    }
                }
                const bool currently_agy =
                    (current_route_model.rfind("agy-cli/", 0) == 0) ||
                    (res.data.is_object() && res.data.value("active_backend", "") == "agy-cli") ||
                    (st->magpie_models.empty() && !st->agy_models.empty());
                st->tab = currently_agy ? "agy-cli" : "magpie";

                auto rebuild = std::make_shared<std::function<void()>>();
                *rebuild = [st, tabs_row, list, current_route_model, on_selected, dlg, rebuild]() {
                    tabs_row->clearViews();
                    const bool on_agy = (st->tab == "agy-cli");

                    auto* agy_tab_btn = ui::makeButton(
                        on_agy ? "✓ 基座: agy-cli 直连 (免 Magpie)" : "基座: agy-cli 直连 (免 Magpie)",
                        [st, rebuild]() {
                            st->tab = "agy-cli";
                            (*rebuild)();
                        },
                        on_agy,
                        32.0f);
                    agy_tab_btn->setMarginRight(8.0f);
                    tabs_row->addView(agy_tab_btn);

                    auto* magpie_tab_btn = ui::makeButton(
                        !on_agy ? "✓ 基座: Magpie Gateway" : "基座: Magpie Gateway",
                        [st, rebuild]() {
                            st->tab = "magpie";
                            (*rebuild)();
                        },
                        !on_agy,
                        32.0f);
                    tabs_row->addView(magpie_tab_btn);

                    list->clearViews();
                    const auto& arr = on_agy ? st->agy_models : st->magpie_models;
                    size_t count = 0;
                    if (arr.is_array()) {
                        for (const auto& item : arr) {
                            std::string mid = item.is_object()
                                                  ? item.value("id", item.value("name", ""))
                                                  : (item.is_string() ? item.get<std::string>() : "");
                            if (mid.empty()) continue;
                            std::string disp = item.is_object() ? item.value("display_name", "") : "";
                            std::string label = mid;
                            if (on_agy && !disp.empty() && disp != mid) {
                                label = mid + "  ·  " + disp;
                            }
                            const bool active = (mid == current_route_model);
                            const std::string chosen_backend = on_agy ? "agy-cli" : "magpie";
                            auto* btn = ui::makeButton(
                                active ? ("✓ " + label) : label,
                                [mid, chosen_backend, on_selected, dlg]() {
                                    dlg->close();
                                    if (!HostKernel::instance().isInitialized()) return;
                                    nlohmann::json req = {
                                        {"action", "set_route"},
                                        {"backend", chosen_backend},
                                        {"model", mid},
                                    };
                                    if (chosen_backend == "magpie") {
                                        req["routing"] = "order";
                                        req["stays"] = "session";
                                    }
                                    HostKernel::instance().capabilities().callAsync(
                                        "llm.router.v1",
                                        req,
                                        [mid, chosen_backend, on_selected](const CapabilityResult& set_res) {
                                            brls::sync([mid, chosen_backend, on_selected, set_res]() {
                                                if (set_res.ok) {
                                                    if (on_selected) on_selected(mid);
                                                    brls::Application::notify(
                                                        "已切换基座 [" + chosen_backend + "] · " + mid);
                                                } else {
                                                    brls::Application::notify(
                                                        "切换失败: " + set_res.error);
                                                }
                                            });
                                        });
                                },
                                active,
                                36.0f);
                            btn->setMarginBottom(6.0f);
                            list->addView(btn);
                            ++count;
                        }
                    }

                    if (count == 0) {
                        list->addView(ui::makeLabel(
                            on_agy
                                ? "未检测到可用 agy-cli 模型，请确认 ~/.local/bin/agy 已安装并登录。"
                                : "暂未读取到 Magpie 模型列表，可直接点击上方切换至「agy-cli 直连」基座。",
                            14.0f,
                            "status/warn_fg"));
                    }
                };

                (*rebuild)();

                dlg->addButton("关闭", [] {});
                dlg->open();
            });
        });
}

AiWorkbench::AiWorkbench(std::function<void()> on_open_marketplace,
                         std::function<void(const std::string& session_id)> on_dock_to_companion)
    : brls::Box(brls::Axis::ROW),
      on_open_marketplace_(std::move(on_open_marketplace)),
      on_dock_to_companion_(std::move(on_dock_to_companion)) {
    setWidthPercentage(100);
    setHeightPercentage(100);
    setBackgroundColor(ui::color("brls/background"));

    // ========================================================================
    // 1. Left Collapsible DeepSeek Conversation History Sidebar
    // ========================================================================
    sidebar_box_ = new brls::Box(brls::Axis::COLUMN);
    sidebar_box_->setId("omnidesk/ai_sidebar");
    sidebar_box_->setWidth(248);
    sidebar_box_->setShrink(0.0f);
    sidebar_box_->setHeightPercentage(100);
    sidebar_box_->setPadding(16, 12, 14, 12);
    sidebar_box_->setBackgroundColor(ui::color("desktop/surface"));
    sidebar_box_->setBorderThickness(1.0f);
    sidebar_box_->setBorderColor(ui::color("desktop/line"));

    auto* sb_top = new brls::Box(brls::Axis::ROW);
    sb_top->setShrink(0.0f);
    sb_top->setAlignItems(brls::AlignItems::CENTER);
    sb_top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    sb_top->setMarginBottom(12);

    auto* new_chat_btn = new brls::Box(brls::Axis::ROW);
    new_chat_btn->setGrow(1.0f);
    new_chat_btn->setHeight(38.0f);
    new_chat_btn->setPadding(0, 12, 0, 12);
    new_chat_btn->setCornerRadius(10.0f);
    new_chat_btn->setHighlightCornerRadius(10.0f);
    new_chat_btn->setFocusable(true);
    new_chat_btn->setAlignItems(brls::AlignItems::CENTER);
    new_chat_btn->setJustifyContent(brls::JustifyContent::CENTER);
    new_chat_btn->setBackgroundColor(ui::color("desktop/active"));
    new_chat_btn->setBorderThickness(1.0f);
    new_chat_btn->setBorderColor(ui::color("brls/accent"));

    auto* plus_icon = new SvgIconView();
    plus_icon->setDimensions(15, 15);
    plus_icon->setMarginRight(6.0f);
    plus_icon->setSvgString(svgPlus(nvgHex(ui::color("brls/accent"))));
    new_chat_btn->addView(plus_icon);
    new_chat_btn->addView(ui::makeLabel("开启新对话", 14.5f, "brls/accent", 1.2f));
    new_chat_btn->registerClickAction([this, alive = alive_](brls::View*) {
        if (alive->load(std::memory_order_acquire)) {
            startNewSession(false);
            focusComposer(true);
        }
        return true;
    });
    sb_top->addView(new_chat_btn);

    auto* collapse_btn = new brls::Box(brls::Axis::ROW);
    collapse_btn->setDimensions(36.0f, 38.0f);
    collapse_btn->setMarginLeft(8.0f);
    collapse_btn->setCornerRadius(9.0f);
    collapse_btn->setHighlightCornerRadius(9.0f);
    collapse_btn->setFocusable(true);
    collapse_btn->setAlignItems(brls::AlignItems::CENTER);
    collapse_btn->setJustifyContent(brls::JustifyContent::CENTER);
    collapse_btn->setBackgroundColor(ui::color("desktop/soft"));
    collapse_btn->setBorderThickness(1.0f);
    collapse_btn->setBorderColor(ui::color("desktop/line"));
    auto* collapse_icon = new SvgIconView();
    collapse_icon->setDimensions(15, 15);
    collapse_icon->setSvgString(svgSidebarToggle(nvgHex(ui::color("font/grey"))));
    collapse_btn->addView(collapse_icon);
    collapse_btn->registerClickAction([this, alive = alive_](brls::View*) {
        if (!alive->load(std::memory_order_acquire)) return true;
        sidebar_collapsed_ = true;
        sidebar_box_->setVisibility(brls::Visibility::GONE);
        if (expand_sidebar_btn_) expand_sidebar_btn_->setVisibility(brls::Visibility::VISIBLE);
        onViewportLayoutChanged();
        focusComposer(false);
        return true;
    });
    sb_top->addView(collapse_btn);
    sidebar_box_->addView(sb_top);

    auto* hist_hdr = ui::makeLabel("对话与策展记录", 12.5f, "font/grey");
    hist_hdr->setShrink(0.0f);
    hist_hdr->setMarginBottom(6.0f);
    hist_hdr->setMarginLeft(4.0f);
    sidebar_box_->addView(hist_hdr);

    sessions_grid_ = new RecyclingGrid();
    sessions_grid_->setGrow(1.0f);
    sessions_grid_->setShrink(1.0f);
    sessions_grid_->setWidthPercentage(100);
    sessions_grid_->setSpanCount(1);
    sessions_grid_->setEstimatedRowHeight(38.0f);
    sessions_grid_->setEstimatedRowSpace(4.0f);
    sessions_grid_->setPreFetchLine(3);
    sessions_grid_->registerCell("session_row", []() { return new AiSessionRowCell(); });
    sessions_grid_->setDataSource(
        new AiSessionsDataSource(
            &sessions_,
            &active_session_idx_,
            [this, alive = alive_](size_t idx) {
                brls::sync([this, alive, idx]() {
                    if (alive->load(std::memory_order_acquire)) selectSession(idx);
                });
            },
            [this, alive = alive_](size_t idx) {
                brls::sync([this, alive, idx]() {
                    if (alive->load(std::memory_order_acquire)) deleteSession(idx);
                });
            }),
        true);
    sidebar_box_->addView(sessions_grid_);

    // Bottom curation & utility shortcuts inside AI sidebar
    auto* sb_bottom = new brls::Box(brls::Axis::COLUMN);
    sb_bottom->setShrink(0.0f);
    sb_bottom->setWidthPercentage(100);
    sb_bottom->addView(ui::makeDivider(8.0f));

    auto* row1 = new brls::Box(brls::Axis::ROW);
    row1->setMarginBottom(6.0f);
    auto* rehydrate_btn = ui::makeButton("回灌历史推荐", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) triggerArchiveRehydrate();
    }, false, 32.0f);
    rehydrate_btn->setGrow(1.0f);
    rehydrate_btn->setMarginRight(6.0f);
    row1->addView(rehydrate_btn);

    auto* cal_btn = ui::makeButton("推荐日历", [alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) CurationCalendarDialog::open();
    }, false, 32.0f);
    cal_btn->setGrow(1.0f);
    row1->addView(cal_btn);
    sb_bottom->addView(row1);

    auto* row2 = new brls::Box(brls::Axis::ROW);
    auto* sched_btn = ui::makeButton("定时策展", [alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            CurationSchedulerDialog::open([]() { CurationCalendarDialog::open(); });
        }
    }, false, 32.0f);
    sched_btn->setGrow(1.0f);
    sched_btn->setMarginRight(6.0f);
    row2->addView(sched_btn);

    auto* mkt_btn = ui::makeButton("AI 插件", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
            on_open_marketplace_();
        }
    }, false, 32.0f);
    mkt_btn->setGrow(1.0f);
    row2->addView(mkt_btn);
    sb_bottom->addView(row2);

    sidebar_box_->addView(sb_bottom);
    addView(sidebar_box_);

    // ========================================================================
    // 2. Main DeepSeek Conversation Stage
    // ========================================================================
    stage_col_ = new brls::Box(brls::Axis::COLUMN);
    stage_col_->setGrow(1.0f);
    stage_col_->setShrink(1.0f);
    stage_col_->setMinWidth(0);
    stage_col_->setMinHeight(0);
    stage_col_->setHeightPercentage(100);
    stage_col_->setAlignItems(brls::AlignItems::CENTER);

    // Top minimal header bar
    auto* top_bar = new brls::Box(brls::Axis::ROW);
    top_bar->setWidthPercentage(100);
    top_bar->setShrink(0.0f);
    top_bar->setPadding(10, 20, 10, 20);
    top_bar->setAlignItems(brls::AlignItems::CENTER);
    top_bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    top_bar->setBorderThickness(0.0f);

    auto* top_left = new brls::Box(brls::Axis::ROW);
    top_left->setAlignItems(brls::AlignItems::CENTER);
    top_left->setShrink(1.0f);

    expand_sidebar_btn_ = new brls::Box(brls::Axis::ROW);
    expand_sidebar_btn_->setDimensions(34.0f, 32.0f);
    expand_sidebar_btn_->setMarginRight(10.0f);
    expand_sidebar_btn_->setCornerRadius(8.0f);
    expand_sidebar_btn_->setHighlightCornerRadius(8.0f);
    expand_sidebar_btn_->setFocusable(true);
    expand_sidebar_btn_->setAlignItems(brls::AlignItems::CENTER);
    expand_sidebar_btn_->setJustifyContent(brls::JustifyContent::CENTER);
    expand_sidebar_btn_->setBackgroundColor(ui::color("desktop/surface"));
    expand_sidebar_btn_->setBorderThickness(1.0f);
    expand_sidebar_btn_->setBorderColor(ui::color("desktop/line"));
    expand_sidebar_btn_->setVisibility(brls::Visibility::GONE);
    auto* expand_icon = new SvgIconView();
    expand_icon->setDimensions(15, 15);
    expand_icon->setSvgString(svgSidebarToggle(nvgHex(ui::color("font/grey"))));
    expand_sidebar_btn_->addView(expand_icon);
    expand_sidebar_btn_->registerClickAction([this, alive = alive_](brls::View*) {
        if (!alive->load(std::memory_order_acquire)) return true;
        sidebar_collapsed_ = false;
        sidebar_box_->setVisibility(brls::Visibility::VISIBLE);
        expand_sidebar_btn_->setVisibility(brls::Visibility::GONE);
        onViewportLayoutChanged();
        return true;
    });
    top_left->addView(expand_sidebar_btn_);

    header_title_label_ = ui::makeLabel("新对话", 16.5f, "brls/text");
    header_title_label_->setSingleLine(true);
    header_title_label_->setShrink(1.0f);
    top_left->addView(header_title_label_);
    top_bar->addView(top_left);

    auto* top_right = new brls::Box(brls::Axis::ROW);
    top_right->setAlignItems(brls::AlignItems::CENTER);
    top_right->setShrink(0.0f);

    domain_pills_box_ = new brls::Box(brls::Axis::ROW);
    domain_pills_box_->setAlignItems(brls::AlignItems::CENTER);
    domain_pills_box_->setMarginRight(10.0f);
    top_right->addView(domain_pills_box_);

    auto* route_chip = new brls::Box(brls::Axis::ROW);
    route_chip->setHeight(28.0f);
    route_chip->setPadding(0, 10, 0, 10);
    route_chip->setCornerRadius(14.0f);
    route_chip->setHighlightCornerRadius(14.0f);
    route_chip->setFocusable(true);
    route_chip->setAlignItems(brls::AlignItems::CENTER);
    route_chip->setBackgroundColor(ui::color("desktop/surface"));
    route_chip->setBorderThickness(1.0f);
    route_chip->setBorderColor(ui::color("desktop/line"));
    route_badge_label_ = ui::makeLabel("路由检测中...", 12.5f, "font/grey", 1.2f);
    route_badge_label_->setSingleLine(true);
    route_chip->addView(route_badge_label_);
    route_chip->registerClickAction([this, alive = alive_](brls::View*) {
        if (alive->load(std::memory_order_acquire)) {
            openModelPickerDialog();
        }
        return true;
    });
    top_right->addView(route_chip);

    auto* dock_mode_btn = new brls::Box(brls::Axis::ROW);
    dock_mode_btn->setHeight(28.0f);
    dock_mode_btn->setPadding(0, 10, 0, 10);
    dock_mode_btn->setMarginLeft(8.0f);
    dock_mode_btn->setCornerRadius(14.0f);
    dock_mode_btn->setHighlightCornerRadius(14.0f);
    dock_mode_btn->setFocusable(true);
    dock_mode_btn->setAlignItems(brls::AlignItems::CENTER);
    dock_mode_btn->setBackgroundColor(ui::color("desktop/surface"));
    dock_mode_btn->setBorderThickness(1.0f);
    dock_mode_btn->setBorderColor(ui::color("desktop/line"));
    auto* dm_icon = new SvgIconView();
    dm_icon->setDimensions(14, 14);
    dm_icon->setMarginRight(5.0f);
    dm_icon->setSvgString(svgDockRight(nvgHex(ui::color("font/grey"))));
    dock_mode_btn->addView(dm_icon);
    auto* dm_lbl = ui::makeLabel("伴读模式", 12.5f, "brls/text", 1.2f);
    dm_lbl->setSingleLine(true);
    dock_mode_btn->addView(dm_lbl);
    dock_mode_btn->registerClickAction([this, alive = alive_](brls::View*) {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire) || !on_dock_to_companion_) return;
            if (active_cancel_) {
                cancelActiveStream();
            } else {
                saveSessionsToDisk();
            }
            on_dock_to_companion_(activeSessionId());
        });
        return true;
    });
    top_right->addView(dock_mode_btn);
    top_bar->addView(top_right);
    stage_col_->addView(top_bar);

    // Empty-session top centering spacer
    hero_top_spacer_ = new brls::Box(brls::Axis::COLUMN);
    hero_top_spacer_->setGrow(0.85f);
    hero_top_spacer_->setShrink(1.0f);
    stage_col_->addView(hero_top_spacer_);

    // Empty-session DeepSeek Hero Greeting
    hero_box_ = new brls::Box(brls::Axis::COLUMN);
    hero_box_->setShrink(0.0f);
    hero_box_->setAlignItems(brls::AlignItems::CENTER);
    hero_box_->setMarginBottom(20.0f);

    auto* hero_title_row = new brls::Box(brls::Axis::ROW);
    hero_title_row->setAlignItems(brls::AlignItems::CENTER);
    hero_title_row->setMarginBottom(8.0f);

    auto* emblem = new SvgIconView();
    emblem->setDimensions(36, 36);
    emblem->setMarginRight(12.0f);
    emblem->setSvgString(svgAiEmblem(nvgHex(ui::color("desktop/focus_ring")),
                                     nvgHex(ui::color("brls/accent"))));
    hero_title_row->addView(emblem);
    hero_title_row->addView(ui::makeLabel("今天想聊点什么，或者策展什么灵感？", 24.0f, "brls/text"));
    hero_box_->addView(hero_title_row);

    auto* hero_sub = ui::makeLabel(
        "内嵌多行输入 · 无字数上限 · 支持深度思考推理与一键反写影视音乐 / 文字期刊 / 书架与歌单",
        14.0f,
        "font/grey");
    hero_box_->addView(hero_sub);
    stage_col_->addView(hero_box_);

    // Active-session scrollable conversation thread
    thread_scroll_ = new brls::ScrollingFrame();
    thread_scroll_->setGrow(1.0f);
    thread_scroll_->setShrink(1.0f);
    thread_scroll_->setWidthPercentage(100);
    thread_scroll_->setVisibility(brls::Visibility::GONE);

    thread_inner_ = new brls::Box(brls::Axis::COLUMN);
    thread_inner_->setWidthPercentage(100);
    thread_inner_->setAlignItems(brls::AlignItems::CENTER);
    thread_inner_->setPadding(8, 24, 16, 24);

    messages_box_ = new brls::Box(brls::Axis::COLUMN);
    messages_box_->setWidth(790);
    thread_inner_->addView(messages_box_);
    thread_scroll_->setContentView(thread_inner_);
    stage_col_->addView(thread_scroll_);

    // Single persistent DeepSeekComposer (glides smoothly from Hero Center -> Bottom Docked)
    composer_wrap_ = new brls::Box(brls::Axis::COLUMN);
    composer_wrap_->setShrink(0.0f);
    composer_wrap_->setWidth(790);
    composer_wrap_->setMargins(4, 24, 6, 24);

    composer_ = new DeepSeekComposer({.compact = false, .show_model_pill = true});
    composer_->setOnSubmit([this, alive = alive_](const DeepSeekComposerSubmission& sub) {
        if (alive->load(std::memory_order_acquire)) {
            handleComposerSubmit(sub);
        }
    });
    composer_->setOnCancelStream([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            cancelActiveStream();
        }
    });
    composer_->setOnModelPillClick([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            openModelPickerDialog();
        }
    });
    composer_->setOnStateChanged([this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire) || !composer_) return;
        auto& sess = currentSession();
        sess.curation_mode = composer_->isCurationMode();
        sess.deep_thinking = composer_->isDeepThinking();
    });
    composer_wrap_->addView(composer_);
    stage_col_->addView(composer_wrap_);

    // Empty-session starter scenario cards (2x2 clean grid right under centered composer)
    starter_grid_ = new brls::Box(brls::Axis::COLUMN);
    starter_grid_->setShrink(0.0f);
    starter_grid_->setWidth(790);
    starter_grid_->setMarginTop(12.0f);

    struct StarterSpec {
        const char* title;
        const char* subtitle;
        const char* draft_or_prompt;
        bool curation;
        bool deep_think;
        bool immediate_send;
    };
    const StarterSpec starters[4] = {
        {
            "AI 灵感策展 (反写工作台 + 歌单)",
            "例如：雨夜写 C++ 来 5 首耐听器乐，以前推过的也可",
            "雨夜写 C++ 来 5 首耐听器乐，以前推过的也可",
            true,
            false,
            false,
        },
        {
            "检索并下载书籍到书架",
            "直接在输入框补充书名 / 作者 / ISBN，自动回退 Z-Library 与 Anna's Archive",
            "请帮我检索并下载书籍到书架（优先 EPUB/PDF，下载完成后刷新书架）：",
            false,
            false,
            false,
        },
        {
            "无限长文 / 代码粘贴与深度思考",
            "开启深度思考模式，支持一次性粘贴整篇论文或数千行代码进行推理剖析",
            "请深入分析以下内容的核心架构、关键细节与潜在边界问题：\n\n",
            false,
            true,
            false,
        },
        {
            "诊断本机 Magpie 模型路由与额度",
            "即时读取可用订阅窗口余量、激活分组与已装载插件能力拓扑",
            "读取本机 Magpie 可用路由与余量，并汇总当前已装载的 OmniDesk 能力插件状态",
            false,
            false,
            true,
        },
    };

    for (int r = 0; r < 2; ++r) {
        auto* row = new brls::Box(brls::Axis::ROW);
        row->setWidthPercentage(100);
        if (r == 0) row->setMarginBottom(8.0f);
        for (int c = 0; c < 2; ++c) {
            const auto& sp = starters[r * 2 + c];
            auto* card = ui::makeCard("desktop/surface", 12.0f, 12.0f);
            card->setGrow(1.0f);
            card->setShrink(1.0f);
            card->setFocusable(true);
            if (c == 0) card->setMarginRight(8.0f);

            auto* t = ui::makeLabel(sp.title, 14.0f, sp.curation ? "brls/accent" : "brls/text");
            t->setSingleLine(true);
            t->setMarginBottom(3.0f);
            card->addView(t);

            auto* s = ui::makeLabel(sp.subtitle, 12.5f, "font/grey", 1.35f);
            card->addView(s);

            card->registerClickAction([this, alive = alive_, sp](brls::View*) {
                if (!alive->load(std::memory_order_acquire) || !composer_) return true;
                composer_->setCurationMode(sp.curation);
                composer_->setDeepThinking(sp.deep_think);
                currentSession().curation_mode = sp.curation;
                currentSession().deep_thinking = sp.deep_think;
                if (sp.immediate_send) {
                    DeepSeekComposerSubmission sub;
                    sub.prompt = sp.draft_or_prompt;
                    sub.curation_mode = sp.curation;
                    sub.deep_thinking = sp.deep_think;
                    sub.sync_playlist = composer_->isSyncPlaylist();
                    handleComposerSubmit(sub);
                } else {
                    composer_->setDraftText(sp.draft_or_prompt, true);
                }
                return true;
            });
            row->addView(card);
        }
        starter_grid_->addView(row);
    }
    stage_col_->addView(starter_grid_);

    footer_caption_ = ui::makeLabel(
        "无字数上限 · 点击输入框直接打字 · Enter 发送 · Shift+Enter 换行 · Esc 退出输入态或中断流式生成",
        12.0f,
        "font/grey");
    footer_caption_->setShrink(0.0f);
    footer_caption_->setMarginTop(4.0f);
    footer_caption_->setMarginBottom(10.0f);
    stage_col_->addView(footer_caption_);

    hero_bottom_spacer_ = new brls::Box(brls::Axis::COLUMN);
    hero_bottom_spacer_->setGrow(1.1f);
    hero_bottom_spacer_->setShrink(1.0f);
    stage_col_->addView(hero_bottom_spacer_);

    addView(stage_col_);

    loadSessionsFromDisk();
    rebuildSessionSidebar();
    rebuildDomainPills();
    rebuildThreadViews();
    updateStageModeLayout();
    refreshRouteStatus();

    if (HostKernel::instance().isInitialized()) {
        ephemeral_note_sub_ = HostKernel::instance().bus().subscribe(
            "entity.recommend.ephemeral_note",
            "host.ai_workbench",
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
                    if (!note.empty() && !sessions_.empty()) {
                        auto& sess = currentSession();
                        AiChatMessage m;
                        m.role = "system";
                        m.display_role = "瞬时伴读提示 (不入库)";
                        m.content = note;
                        sess.messages.push_back(std::move(m));
                        if (!active_cancel_) {
                            appendMessageTurnView(sess.messages.size() - 1);
                            updateStageModeLayout();
                            scrollToBottom(false);
                        }
                    }
                });
            });

        sessions_sync_sub_ = HostKernel::instance().bus().subscribe(
            "entity.ai.sessions_updated",
            "host.ai_workbench.sync",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                if (!payload.is_object() || payload.value("origin", "") == "workbench") {
                    return;
                }
                const std::string target_id = payload.value("active_session_id", "");
                brls::sync([this, alive, target_id]() {
                    if (!alive->load(std::memory_order_acquire) || active_cancel_) return;
                    const std::string keep_id = target_id.empty() ? activeSessionId() : target_id;
                    sessions_ = loadAiChatSessionsFromDisk();
                    active_session_idx_ = 0;
                    for (size_t i = 0; i < sessions_.size(); ++i) {
                        if (sessions_[i].id == keep_id) {
                            active_session_idx_ = i;
                            break;
                        }
                    }
                    if (composer_) {
                        composer_->setCurationMode(currentSession().curation_mode);
                        composer_->setDeepThinking(currentSession().deep_thinking);
                    }
                    rebuildSessionSidebar();
                    rebuildDomainPills();
                    rebuildThreadViews();
                    updateStageModeLayout();
                    scrollToBottom(true);
                });
            });
    }
}

AiWorkbench::~AiWorkbench() {
    alive_->store(false, std::memory_order_release);
    if (HostKernel::instance().isInitialized()) {
        if (ephemeral_note_sub_ != 0) {
            HostKernel::instance().bus().unsubscribe(ephemeral_note_sub_);
            ephemeral_note_sub_ = 0;
        }
        if (sessions_sync_sub_ != 0) {
            HostKernel::instance().bus().unsubscribe(sessions_sync_sub_);
            sessions_sync_sub_ = 0;
        }
    }
    if (route_cancel_) {
        route_cancel_->cancel();
        route_cancel_.reset();
    }
    if (active_cancel_) {
        active_cancel_->cancel();
        active_cancel_.reset();
    }
}

brls::View* AiWorkbench::getDefaultFocus() {
    if (composer_ && composer_->editor()) {
        return composer_->editor();
    }
    return brls::Box::getDefaultFocus();
}

void AiWorkbench::focusComposer(bool start_editing) {
    if (composer_) {
        composer_->focusInput(start_editing);
    }
}

void AiWorkbench::refresh() {
    refreshRouteStatus();
    if (composer_) {
        composer_->refreshThemeStyling();
    }
    onViewportLayoutChanged();
}

void AiWorkbench::onViewportLayoutChanged() {
    const float total_w =
        brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float rail_w = 260.0f;
    const float sb_w = sidebar_collapsed_ ? 0.0f : 248.0f;
    const float avail_stage = std::max(380.0f, total_w - rail_w - sb_w - 56.0f);
    const float col_w = std::min(820.0f, avail_stage);

    if (messages_box_) messages_box_->setWidth(col_w);
    if (composer_wrap_) composer_wrap_->setWidth(col_w);
    if (starter_grid_) starter_grid_->setWidth(std::min(790.0f, avail_stage));
    invalidate();
}

void AiWorkbench::loadSessionsFromDisk() {
    sessions_ = loadAiChatSessionsFromDisk();
    active_session_idx_ = 0;
}

void AiWorkbench::saveSessionsToDisk() const {
    saveAiChatSessionsToDisk(sessions_);
    notifyAiSessionsUpdated("workbench", activeSessionId());
}

AiChatSession& AiWorkbench::currentSession() {
    if (sessions_.empty()) {
        AiChatSession fresh;
        fresh.id = makeSessionId();
        fresh.updated_at = nowUnixSec();
        sessions_.push_back(std::move(fresh));
        active_session_idx_ = 0;
    }
    if (active_session_idx_ >= sessions_.size()) {
        active_session_idx_ = 0;
    }
    return sessions_[active_session_idx_];
}

const AiChatSession& AiWorkbench::currentSession() const {
    return sessions_[std::min(active_session_idx_, sessions_.size() - 1)];
}

std::string AiWorkbench::activeSessionId() const {
    if (sessions_.empty()) return "";
    return currentSession().id;
}

void AiWorkbench::selectSessionById(const std::string& session_id) {
    if (session_id.empty()) return;
    if (!active_cancel_) {
        sessions_ = loadAiChatSessionsFromDisk();
    }
    for (size_t i = 0; i < sessions_.size(); ++i) {
        if (sessions_[i].id == session_id) {
            selectSession(i);
            return;
        }
    }
}

void AiWorkbench::startNewSession(bool curation_mode) {
    if (!sessions_.empty() && sessions_.front().messages.empty()) {
        active_session_idx_ = 0;
        sessions_.front().curation_mode = curation_mode;
        sessions_.front().domain = attached_domain_;
        sessions_.front().context_summary = attached_summary_;
    } else {
        AiChatSession fresh;
        fresh.id = makeSessionId();
        fresh.title = curation_mode ? "新灵感策展" : "新对话";
        fresh.domain = attached_domain_;
        fresh.context_summary = attached_summary_;
        fresh.curation_mode = curation_mode;
        fresh.updated_at = nowUnixSec();
        sessions_.insert(sessions_.begin(), std::move(fresh));
        active_session_idx_ = 0;
    }

    if (composer_) {
        composer_->setCurationMode(currentSession().curation_mode);
        composer_->setDeepThinking(currentSession().deep_thinking);
    }
    saveSessionsToDisk();
    rebuildSessionSidebar();
    rebuildDomainPills();
    rebuildThreadViews();
    updateStageModeLayout();
}

void AiWorkbench::selectSession(size_t index) {
    if (index >= sessions_.size()) return;
    if (active_cancel_ && streaming_session_idx_ != index) {
        cancelActiveStream();
    }
    active_session_idx_ = index;
    const auto& sess = currentSession();
    if (composer_) {
        composer_->setCurationMode(sess.curation_mode);
        composer_->setDeepThinking(sess.deep_thinking);
    }
    rebuildSessionSidebar();
    rebuildDomainPills();
    rebuildThreadViews();
    updateStageModeLayout();
    scrollToBottom(true);
}

void AiWorkbench::deleteSession(size_t index) {
    if (index >= sessions_.size()) return;
    if (active_cancel_ && streaming_session_idx_ == index) {
        cancelActiveStream();
    } else if (active_cancel_ && index < streaming_session_idx_) {
        --streaming_session_idx_;
    }
    sessions_.erase(sessions_.begin() + static_cast<std::ptrdiff_t>(index));
    if (sessions_.empty()) {
        AiChatSession fresh;
        fresh.id = makeSessionId();
        fresh.updated_at = nowUnixSec();
        sessions_.push_back(std::move(fresh));
        active_session_idx_ = 0;
    } else if (active_session_idx_ >= sessions_.size()) {
        active_session_idx_ = sessions_.size() - 1;
    } else if (index < active_session_idx_) {
        --active_session_idx_;
    }
    saveSessionsToDisk();
    rebuildSessionSidebar();
    rebuildDomainPills();
    rebuildThreadViews();
    updateStageModeLayout();
}

void AiWorkbench::setExternalContext(ContentDomain domain, const std::string& summary) {
    attached_domain_ = domain;
    attached_summary_ = summary;
    if (!sessions_.empty() && currentSession().messages.empty()) {
        currentSession().domain = domain;
        currentSession().context_summary = summary;
    }
    rebuildDomainPills();
    updateStageModeLayout();
}

void AiWorkbench::askWithContext(ContentDomain domain,
                                 const std::string& context_summary,
                                 const std::string& prompt,
                                 bool curation_mode) {
    attached_domain_ = domain;
    attached_summary_ = context_summary;
    auto& sess = currentSession();
    sess.domain = domain;
    sess.context_summary = context_summary;
    sess.curation_mode = curation_mode;
    if (composer_) {
        composer_->setCurationMode(curation_mode);
    }
    rebuildDomainPills();
    updateStageModeLayout();

    if (!prompt.empty()) {
        DeepSeekComposerSubmission sub;
        sub.prompt = prompt;
        sub.curation_mode = curation_mode;
        sub.deep_thinking = composer_ ? composer_->isDeepThinking() : false;
        sub.sync_playlist = composer_ ? composer_->isSyncPlaylist() : true;
        handleComposerSubmit(sub);
    } else {
        focusComposer(true);
    }
}

void AiWorkbench::rebuildSessionSidebar() {
    if (!sessions_grid_) return;
    sessions_grid_->reloadData(false);
}

void AiWorkbench::rebuildDomainPills() {
    if (!domain_pills_box_) return;
    domain_pills_box_->clearViews();

    const auto& sess = currentSession();
    struct DomOpt {
        ContentDomain dom;
        const char* label;
    };
    const DomOpt opts[] = {
        {ContentDomain::Core, "全域"},
        {ContentDomain::Media, "影视音乐"},
        {ContentDomain::Text, "文字期刊"},
        {ContentDomain::Book, "书本PDF"},
    };
    for (const auto& o : opts) {
        const bool active = (sess.domain == o.dom);
        auto* btn = ui::makeButton(o.label, [this, alive = alive_, dom = o.dom]() {
            if (!alive->load(std::memory_order_acquire)) return;
            currentSession().domain = dom;
            if (dom == attached_domain_ && !attached_summary_.empty()) {
                currentSession().context_summary = attached_summary_;
            }
            rebuildDomainPills();
            updateStageModeLayout();
        }, active, 28.0f);
        btn->setMarginRight(5.0f);
        domain_pills_box_->addView(btn);
    }
}

void AiWorkbench::updateStageModeLayout() {
    const auto& sess = currentSession();
    const bool is_empty = sess.messages.empty();

    if (header_title_label_) {
        header_title_label_->setText(sess.title.empty() ? "新对话" : sess.title);
    }

    if (hero_top_spacer_) {
        hero_top_spacer_->setVisibility(is_empty ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (hero_box_) {
        hero_box_->setVisibility(is_empty ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (thread_scroll_) {
        thread_scroll_->setVisibility(is_empty ? brls::Visibility::GONE : brls::Visibility::VISIBLE);
    }
    if (starter_grid_) {
        starter_grid_->setVisibility(is_empty ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (hero_bottom_spacer_) {
        hero_bottom_spacer_->setVisibility(is_empty ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }

    if (composer_) {
        if (!sess.context_summary.empty()) {
            composer_->setContextBadge(
                "已关联上下文 · " + toDisplayLabel(sess.domain) + ": " + sess.context_summary,
                [this, alive = alive_]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    currentSession().context_summary.clear();
                    attached_summary_.clear();
                    updateStageModeLayout();
                });
        } else {
            composer_->setContextBadge("");
        }
    }

    onViewportLayoutChanged();
}

void AiWorkbench::resetLiveTurnRefs() {
    live_turn_box_ = nullptr;
    live_role_label_ = nullptr;
    live_think_box_ = nullptr;
    live_think_header_label_ = nullptr;
    live_think_body_label_ = nullptr;
    live_reply_host_ = nullptr;
    live_reply_label_ = nullptr;
}

brls::Box* AiWorkbench::buildMessageTurnView(size_t msg_idx) {
    auto& sess = currentSession();
    if (msg_idx >= sess.messages.size()) return nullptr;
    const auto& m = sess.messages[msg_idx];

    if (m.role == "user") {
        // Right-aligned DeepSeek User Message Bubble
        auto* outer = new brls::Box(brls::Axis::COLUMN);
        outer->setWidthPercentage(100);
        outer->setAlignItems(brls::AlignItems::FLEX_END);
        outer->setMarginBottom(18.0f);

        auto* bubble = new brls::Box(brls::Axis::COLUMN);
        bubble->setMaxWidthPercentage(82);
        bubble->setPadding(12, 16, 12, 16);
        bubble->setCornerRadius(16.0f);
        bubble->setBackgroundColor(ui::color(m.curation ? "desktop/active" : "desktop/soft"));
        bubble->setBorderThickness(1.0f);
        bubble->setBorderColor(ui::color(m.curation ? "brls/accent" : "desktop/line"));

        if (m.curation) {
            auto* badge = ui::makeLabel("灵感策展指令", 12.0f, "brls/accent", 1.2f);
            badge->setMarginBottom(4.0f);
            bubble->addView(badge);
        }

        MarkdownStyle u_style;
        u_style.font_size = 15.5f;
        u_style.line_height = 1.55f;
        u_style.text_color_token = "brls/text";
        u_style.block_gap = 6.0f;
        bubble->addView(new MarkdownView(m.content, std::move(u_style)));
        outer->addView(bubble);

        // Subtle user message actions: 复制 | 重新编辑
        auto* act_row = new brls::Box(brls::Axis::ROW);
        act_row->setMarginTop(4.0f);
        auto* copy_btn = ui::makeButton("复制", [txt = m.content]() {
            SDL_SetClipboardText(txt.c_str());
            brls::Application::notify("已复制提问内容");
        }, false, 26.0f);
        copy_btn->setMarginRight(6.0f);
        act_row->addView(copy_btn);

        auto* edit_btn = ui::makeButton("重新编辑", [this, alive = alive_, txt = m.content, cur = m.curation]() {
            if (!alive->load(std::memory_order_acquire) || !composer_) return;
            composer_->setCurationMode(cur);
            composer_->setDraftText(txt, true);
        }, false, 26.0f);
        act_row->addView(edit_btn);
        outer->addView(act_row);
        return outer;
    }

    // Left-aligned open DeepSeek Assistant / System Message Turn
    auto* turn = new brls::Box(brls::Axis::COLUMN);
    turn->setWidthPercentage(100);
    turn->setMarginBottom(22.0f);

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setMarginBottom(8.0f);

    auto* avatar = new brls::Box(brls::Axis::ROW);
    avatar->setDimensions(26.0f, 26.0f);
    avatar->setCornerRadius(13.0f);
    avatar->setMarginRight(8.0f);
    avatar->setAlignItems(brls::AlignItems::CENTER);
    avatar->setJustifyContent(brls::JustifyContent::CENTER);
    avatar->setBackgroundColor(ui::color(m.curation ? "desktop/active" : "status/info_bg"));
    auto* av_icon = new SvgIconView();
    av_icon->setDimensions(15, 15);
    av_icon->setSvgString(svgAiEmblem(
        nvgHex(ui::color(m.curation ? "brls/accent" : "desktop/focus_ring")),
        nvgHex(ui::color("brls/accent"))));
    avatar->addView(av_icon);
    hdr->addView(avatar);

    const char* role_col = m.error ? "status/err_fg"
                                   : (m.curation ? "brls/accent" : "brls/text");
    hdr->addView(ui::makeLabel(m.display_role.empty() ? "AI" : m.display_role, 14.0f, role_col));
    turn->addView(hdr);

    // Collapsible DeepSeek Reasoning / Thought Block
    if (!m.reasoning.empty()) {
        auto* think_wrap = new brls::Box(brls::Axis::COLUMN);
        think_wrap->setWidthPercentage(100);
        think_wrap->setMarginBottom(10.0f);
        think_wrap->setMarginLeft(34.0f);

        const std::string dur = formatDurationSeconds(m.thinking_sec);
        const std::string base_hdr =
            dur.empty() ? "已深度思考" : ("已深度思考 (用时 " + dur + " 秒)");

        auto* toggle_btn = new brls::Box(brls::Axis::ROW);
        toggle_btn->setPadding(5, 12, 5, 12);
        toggle_btn->setCornerRadius(8.0f);
        toggle_btn->setHighlightCornerRadius(8.0f);
        toggle_btn->setFocusable(true);
        toggle_btn->setAlignItems(brls::AlignItems::CENTER);
        toggle_btn->setBackgroundColor(ui::color("desktop/soft"));
        toggle_btn->setBorderThickness(1.0f);
        toggle_btn->setBorderColor(ui::color("desktop/line"));

        auto* toggle_lbl = ui::makeLabel(
            (m.thinking_collapsed ? "▸ " : "▾ ") + base_hdr,
            13.0f,
            "font/grey",
            1.2f);
        toggle_btn->addView(toggle_lbl);

        auto* quote_row = new brls::Box(brls::Axis::ROW);
        quote_row->setWidthPercentage(100);
        quote_row->setMarginTop(8.0f);
        quote_row->setVisibility(
            m.thinking_collapsed ? brls::Visibility::GONE : brls::Visibility::VISIBLE);

        auto* quote_bar = new brls::Box(brls::Axis::COLUMN);
        quote_bar->setWidth(2.5f);
        quote_bar->setShrink(0.0f);
        quote_bar->setCornerRadius(1.2f);
        quote_bar->setBackgroundColor(ui::color("desktop/line"));
        quote_bar->setMarginRight(12.0f);
        quote_row->addView(quote_bar);

        auto* thought_body = ui::makeLabel(m.reasoning, 13.8f, "font/grey", 1.52f);
        thought_body->setGrow(1.0f);
        thought_body->setShrink(1.0f);
        quote_row->addView(thought_body);

        toggle_btn->registerClickAction([this, alive = alive_, msg_idx, quote_row, toggle_lbl, base_hdr](brls::View*) {
            if (!alive->load(std::memory_order_acquire)) return true;
            auto& cur_s = currentSession();
            if (msg_idx >= cur_s.messages.size()) return true;
            bool& collapsed = cur_s.messages[msg_idx].thinking_collapsed;
            collapsed = !collapsed;
            quote_row->setVisibility(collapsed ? brls::Visibility::GONE : brls::Visibility::VISIBLE);
            toggle_lbl->setText((collapsed ? "▸ " : "▾ ") + base_hdr);
            if (messages_box_) messages_box_->invalidate();
            return true;
        });

        auto* btn_holder = new brls::Box(brls::Axis::ROW);
        btn_holder->addView(toggle_btn);
        think_wrap->addView(btn_holder);
        think_wrap->addView(quote_row);
        turn->addView(think_wrap);
    }

    // Markdown reply body
    auto* body_wrap = new brls::Box(brls::Axis::COLUMN);
    body_wrap->setWidthPercentage(100);
    body_wrap->setPadding(0, 0, 0, 34);

    MarkdownStyle md_style;
    md_style.font_size = 15.5f;
    md_style.line_height = 1.62f;
    md_style.text_color_token = m.error ? "status/err_fg" : "brls/text";
    md_style.block_gap = 8.0f;
    body_wrap->addView(new MarkdownView(m.content, std::move(md_style)));

    if (m.role == "assistant") {
        auto* actions = new brls::Box(brls::Axis::ROW);
        actions->setMarginTop(8.0f);
        auto* cp_btn = ui::makeButton("复制回复", [txt = m.content]() {
            SDL_SetClipboardText(txt.c_str());
            brls::Application::notify("已复制 AI 回复全文");
        }, false, 28.0f);
        cp_btn->setMarginRight(8.0f);
        actions->addView(cp_btn);

        const size_t assistant_idx = msg_idx;
        auto* regen_btn = ui::makeButton("重新生成", [this, alive = alive_, assistant_idx]() {
            if (alive->load(std::memory_order_acquire)) {
                regenerateFromAssistantIndex(assistant_idx);
            }
        }, false, 28.0f);
        actions->addView(regen_btn);
        body_wrap->addView(actions);
    }

    turn->addView(body_wrap);
    return turn;
}

void AiWorkbench::appendMessageTurnView(size_t msg_idx) {
    if (!messages_box_) return;
    if (brls::Box* turn = buildMessageTurnView(msg_idx)) {
        messages_box_->addView(turn);
        messages_box_->invalidate();
    }
}

void AiWorkbench::rebuildThreadViews() {
    resetLiveTurnRefs();

    if (!messages_box_) return;
    messages_box_->clearViews();

    auto& sess = currentSession();
    for (size_t i = 0; i < sess.messages.size(); ++i) {
        if (brls::Box* turn = buildMessageTurnView(i)) {
            messages_box_->addView(turn);
        }
    }
    messages_box_->invalidate();
}

void AiWorkbench::scheduleStreamUiFlush(uint64_t seq, bool curation_only) {
    uint64_t delay_ms = 0;
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        if (flush_scheduled_) {
            return;
        }
        flush_scheduled_ = true;
        const uint64_t now_ms = nowSteadyMs();
        if (last_stream_flush_ms_ > 0 && now_ms > last_stream_flush_ms_ &&
            (now_ms - last_stream_flush_ms_) < kStreamUiFlushIntervalMs) {
            delay_ms = kStreamUiFlushIntervalMs - (now_ms - last_stream_flush_ms_);
        }
    }

    auto alive = alive_;
    if (delay_ms == 0) {
        brls::sync([this, alive, seq, curation_only]() {
            if (alive->load(std::memory_order_acquire)) {
                flushStreamUiNow(seq, curation_only);
            }
        });
    } else {
        brls::Threading::delay(static_cast<long>(delay_ms), [this, alive, seq, curation_only]() {
            if (alive->load(std::memory_order_acquire)) {
                flushStreamUiNow(seq, curation_only);
            }
        });
    }
}

void AiWorkbench::flushStreamUiNow(uint64_t seq, bool curation_only) {
    if (seq != active_req_seq_) {
        return;
    }

    std::string c_delta;
    std::string r_delta;
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        c_delta.swap(pending_content_delta_);
        r_delta.swap(pending_reasoning_delta_);
        flush_scheduled_ = false;
        last_stream_flush_ms_ = nowSteadyMs();
    }

    if (curation_only) {
        if (c_delta.empty()) return;
        live_raw_content_ += c_delta;
        if (live_reply_label_) {
            live_reply_label_->setText(live_raw_content_ + "▍");
        }
        if (messages_box_) messages_box_->invalidate();
        scrollToBottom(true);
        return;
    }

    if (!r_delta.empty()) {
        live_reasoning_ += r_delta;
        reasoning_end_ms_ = nowSteadyMs();
    }
    if (!c_delta.empty()) {
        if (reasoning_end_ms_ == 0 && !live_reasoning_.empty()) {
            reasoning_end_ms_ = nowSteadyMs();
        }
        live_raw_content_ += c_delta;
    }

    std::string parsed_think;
    std::string parsed_reply;
    bool in_think = false;
    splitThinkAndReply(live_raw_content_, live_reasoning_, &parsed_think, &parsed_reply, &in_think);
    if (in_think) {
        reasoning_end_ms_ = nowSteadyMs();
    }

    if (!parsed_think.empty() && live_think_box_ && live_think_body_label_) {
        live_think_box_->setVisibility(brls::Visibility::VISIBLE);
        const bool still_thinking = in_think || parsed_reply.empty();
        if (live_think_header_label_) {
            live_think_header_label_->setText(
                still_thinking ? "▾ 正在深度思考..." : "▾ 思考完成，正在输出回答...");
        }
        live_think_body_label_->setText(parsed_think + (still_thinking ? "▍" : ""));
    }

    if (live_reply_label_) {
        if (!parsed_reply.empty() || parsed_think.empty()) {
            live_reply_label_->setText(parsed_reply + "▍");
        } else {
            live_reply_label_->setText("");
        }
    }
    if (messages_box_) messages_box_->invalidate();
    scrollToBottom(true);
}

void AiWorkbench::scrollToBottom(bool immediate) {
    if (!thread_scroll_ || !thread_inner_) return;
    brls::sync([this, alive = alive_, immediate]() {
        if (!alive->load(std::memory_order_acquire) || !thread_scroll_ || !thread_inner_) return;
        const float max_y = std::max(0.0f, thread_inner_->getHeight() - thread_scroll_->getHeight());
        thread_scroll_->setContentOffsetY(max_y, !immediate);
    });
}

void AiWorkbench::refreshRouteStatus() {
    if (!route_badge_label_) return;
    if (route_cancel_) {
        route_cancel_->cancel();
        route_cancel_.reset();
    }
    const uint64_t seq = ++route_req_seq_;

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    auto providers = caps.providersFor("ai.agent.v1");
    if (providers.empty()) {
        providers = caps.providersFor("llm.chat.v1");
    }
    if (providers.empty()) {
        route_badge_label_->setText("未接入 AI 插件");
        route_badge_label_->setTextColor(ui::color("status/warn_fg"));
        if (composer_) composer_->setModelLabel("未安装 AI 插件");
        return;
    }

    route_badge_label_->setText("已接入: " + providers.front().plugin_id);
    route_badge_label_->setTextColor(ui::color("status/ok_fg"));

    if (caps.has("llm.router.v1")) {
        auto cancel = CancelToken::create();
        route_cancel_ = cancel;
        caps.callAsync(
            "llm.router.v1",
            {{"action", "current_route"}},
            [this, alive = alive_, seq](const CapabilityResult& r) {
                brls::sync([this, alive, seq, r]() {
                    if (!alive->load(std::memory_order_acquire) || seq != route_req_seq_ ||
                        !route_badge_label_) {
                        return;
                    }
                    route_cancel_.reset();
                    if (r.ok && r.data.is_object()) {
                        std::string model = r.data.value("model", "");
                        std::string summary = r.data.value("summary", "");
                        std::string backend = r.data.value("backend", "");
                        current_route_model_ = !model.empty() ? model : (!summary.empty() ? summary : "Auto Route");
                        if (backend == "agy-cli") {
                            route_badge_label_->setText("基座: " + current_route_model_ + " (直连)");
                        } else {
                            route_badge_label_->setText("基座: " + current_route_model_);
                        }
                        route_badge_label_->setTextColor(ui::color("status/ok_fg"));
                        if (composer_) {
                            composer_->setModelLabel(current_route_model_);
                        }
                    }
                });
            },
            cancel);
    }
}

void AiWorkbench::openModelPickerDialog() {
    openAiModelPickerDialog(
        current_route_model_,
        on_open_marketplace_,
        [this, alive = alive_](const std::string& mid) {
            if (!alive->load(std::memory_order_acquire)) return;
            current_route_model_ = mid;
            if (composer_) composer_->setModelLabel(mid);
            refreshRouteStatus();
        });
}

void AiWorkbench::handleComposerSubmit(const DeepSeekComposerSubmission& sub) {
    std::string prompt = trimSpaces(sub.prompt);
    if (prompt.empty()) return;

    std::string extracted_curation;
    const bool force_curation = extractCurationPrefix(prompt, &extracted_curation);
    if (force_curation) {
        prompt = extracted_curation;
    }

    const bool use_curation = sub.curation_mode || force_curation;
    if (use_curation) {
        dispatchCurationRequest(prompt, sub.sync_playlist);
    } else {
        dispatchChatRequest(prompt, sub.deep_thinking);
    }
}

void AiWorkbench::regenerateFromAssistantIndex(size_t assistant_msg_idx) {
    cancelActiveStream();

    auto& sess = currentSession();
    if (assistant_msg_idx == 0 || assistant_msg_idx >= sess.messages.size()) return;

    // Find preceding user message
    size_t user_idx = assistant_msg_idx;
    while (user_idx > 0) {
        --user_idx;
        if (sess.messages[user_idx].role == "user") {
            break;
        }
    }
    if (sess.messages[user_idx].role != "user") return;

    const std::string prompt = sess.messages[user_idx].content;
    const bool cur = sess.messages[user_idx].curation;
    sess.messages.resize(user_idx);

    if (messages_box_ && messages_box_->getChildren().size() >= user_idx) {
        resetLiveTurnRefs();
        while (messages_box_->getChildren().size() > user_idx) {
            messages_box_->removeView(messages_box_->getChildren().back(), true);
        }
        messages_box_->invalidate();
    } else {
        rebuildThreadViews();
    }
    updateStageModeLayout();

    if (cur) {
        dispatchCurationRequest(prompt, composer_ ? composer_->isSyncPlaylist() : true);
    } else {
        dispatchChatRequest(prompt, composer_ ? composer_->isDeepThinking() : false);
    }
}

void AiWorkbench::dispatchChatRequest(const std::string& prompt, bool deep_thinking) {
    cancelActiveStream();

    auto& sess = currentSession();
    const bool first_msg = sess.messages.empty();

    // Build multi-turn context history before appending current prompt
    nlohmann::json history = nlohmann::json::array();
    for (const auto& prev : sess.messages) {
        if ((prev.role == "user" || prev.role == "assistant") && !prev.error && !prev.content.empty()) {
            history.push_back({{"role", prev.role}, {"content", prev.content}});
        }
    }

    AiChatMessage user_msg;
    user_msg.role = "user";
    user_msg.display_role = "你";
    user_msg.content = prompt;
    user_msg.curation = false;
    sess.messages.push_back(std::move(user_msg));
    if (first_msg || sess.title == "新对话" || sess.title == "新灵感策展") {
        sess.title = summarizeSessionTitle(prompt);
    }
    sess.curation_mode = false;
    sess.deep_thinking = deep_thinking;
    sess.updated_at = nowUnixSec();

    rebuildSessionSidebar();
    appendMessageTurnView(sess.messages.size() - 1);
    updateStageModeLayout();

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    std::string cap_id;
    std::string role_prefix;
    nlohmann::json req = nlohmann::json::object();

    if (caps.has("ai.agent.v1")) {
        cap_id = "ai.agent.v1";
        role_prefix = "AI Agent";
        req = {
            {"action", "chat"},
            {"domain", toString(sess.domain)},
            {"context", sess.context_summary},
            {"messages", history},
            {"prompt", prompt},
            {"stream", true},
        };
    } else if (caps.has("llm.chat.v1")) {
        cap_id = "llm.chat.v1";
        role_prefix = "LLM";
        history.push_back({{"role", "user"}, {"content", prompt}});
        req = {
            {"domain", toString(sess.domain)},
            {"context", sess.context_summary},
            {"messages", history},
            {"stream", true},
        };
    } else {
        AiChatMessage err_msg;
        err_msg.role = "assistant";
        err_msg.display_role = "未安装 AI 能力插件";
        err_msg.content =
            "尚未安装提供 `ai.agent.v1` 或 `llm.chat.v1` 的插件（如 `plugin-magpie-agent`）。\n"
            "请点击左侧边栏「插件市场」安装后即可开启无限制多轮对话。";
        err_msg.error = true;
        sess.messages.push_back(std::move(err_msg));
        saveSessionsToDisk();
        appendMessageTurnView(sess.messages.size() - 1);
        scrollToBottom(false);
        return;
    }

    req["deep_thinking"] = deep_thinking;
    if (deep_thinking) {
        req["reasoning_effort"] = "high";
    }

    // Create live streaming Assistant turn UI right inside messages_box_
    auto* turn = new brls::Box(brls::Axis::COLUMN);
    turn->setWidthPercentage(100);
    turn->setMarginBottom(22.0f);
    live_turn_box_ = turn;

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setMarginBottom(8.0f);
    auto* avatar = new brls::Box(brls::Axis::ROW);
    avatar->setDimensions(26.0f, 26.0f);
    avatar->setCornerRadius(13.0f);
    avatar->setMarginRight(8.0f);
    avatar->setAlignItems(brls::AlignItems::CENTER);
    avatar->setJustifyContent(brls::JustifyContent::CENTER);
    avatar->setBackgroundColor(ui::color("status/info_bg"));
    auto* av_icon = new SvgIconView();
    av_icon->setDimensions(15, 15);
    av_icon->setSvgString(svgAiEmblem(nvgHex(ui::color("desktop/focus_ring")),
                                      nvgHex(ui::color("brls/accent"))));
    avatar->addView(av_icon);
    hdr->addView(avatar);

    live_role_label_ = ui::makeLabel(
        role_prefix + (deep_thinking ? " · 深度思考中..." : " · 正在生成..."),
        14.0f,
        "desktop/focus_ring");
    hdr->addView(live_role_label_);
    turn->addView(hdr);

    // Live collapsible reasoning container (revealed automatically if thinking tokens arrive)
    live_think_box_ = new brls::Box(brls::Axis::COLUMN);
    live_think_box_->setWidthPercentage(100);
    live_think_box_->setMarginBottom(10.0f);
    live_think_box_->setMarginLeft(34.0f);
    live_think_box_->setVisibility(deep_thinking ? brls::Visibility::VISIBLE : brls::Visibility::GONE);

    live_think_header_label_ = ui::makeLabel("▾ 正在深度思考...", 13.0f, "font/grey", 1.2f);
    live_think_header_label_->setMarginBottom(6.0f);
    live_think_box_->addView(live_think_header_label_);

    auto* quote_row = new brls::Box(brls::Axis::ROW);
    quote_row->setWidthPercentage(100);
    auto* quote_bar = new brls::Box(brls::Axis::COLUMN);
    quote_bar->setWidth(2.5f);
    quote_bar->setShrink(0.0f);
    quote_bar->setCornerRadius(1.2f);
    quote_bar->setBackgroundColor(ui::color("desktop/line"));
    quote_bar->setMarginRight(12.0f);
    quote_row->addView(quote_bar);

    live_think_body_label_ = ui::makeLabel(deep_thinking ? "▍" : "", 13.8f, "font/grey", 1.52f);
    live_think_body_label_->setGrow(1.0f);
    live_think_body_label_->setShrink(1.0f);
    quote_row->addView(live_think_body_label_);
    live_think_box_->addView(quote_row);
    turn->addView(live_think_box_);

    live_reply_host_ = new brls::Box(brls::Axis::COLUMN);
    live_reply_host_->setWidthPercentage(100);
    live_reply_host_->setPadding(0, 0, 0, 34);
    live_reply_label_ = ui::makeLabel(deep_thinking ? "" : "▍", 15.5f, "brls/text", 1.58f);
    live_reply_host_->addView(live_reply_label_);
    turn->addView(live_reply_host_);

    if (messages_box_) {
        messages_box_->addView(turn);
        messages_box_->invalidate();
    }
    scrollToBottom(false);

    const uint64_t seq = ++active_req_seq_;
    auto cancel = CancelToken::create();
    active_cancel_ = cancel;
    streaming_session_idx_ = active_session_idx_;
    stream_start_ms_ = nowSteadyMs();
    reasoning_end_ms_ = 0;
    live_raw_content_.clear();
    live_reasoning_.clear();
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        pending_content_delta_.clear();
        pending_reasoning_delta_.clear();
        flush_scheduled_ = false;
        last_stream_flush_ms_ = 0;
    }
    if (composer_) {
        composer_->setStreaming(true);
    }

    caps.callStreamAsync(
        cap_id,
        req,
        [this, alive = alive_, cancel, seq](const StreamChunk& chunk) {
            if (!alive->load(std::memory_order_acquire) || cancel->isCancelled() || chunk.delta.empty()) {
                return;
            }
            {
                std::lock_guard<std::mutex> lk(stream_mu_);
                if (chunk.event == "reasoning") {
                    pending_reasoning_delta_ += chunk.delta;
                } else {
                    pending_content_delta_ += chunk.delta;
                }
            }
            scheduleStreamUiFlush(seq, false);
        },
        [this, alive = alive_, cancel, seq, role_prefix](const CapabilityResult& res) {
            brls::sync([this, alive, cancel, seq, role_prefix, res]() {
                if (!alive->load(std::memory_order_acquire) || seq != active_req_seq_) {
                    return;
                }
                std::string c_delta;
                std::string r_delta;
                {
                    std::lock_guard<std::mutex> lk(stream_mu_);
                    c_delta.swap(pending_content_delta_);
                    r_delta.swap(pending_reasoning_delta_);
                    flush_scheduled_ = false;
                }
                live_reasoning_ += r_delta;
                live_raw_content_ += c_delta;

                std::string model_tag;
                if (res.data.is_object() && res.data.contains("model") && res.data["model"].is_string()) {
                    model_tag = res.data["model"].get<std::string>();
                }
                const std::string provider = !model_tag.empty()
                                                 ? (role_prefix + " · " + model_tag)
                                                 : (!res.provider_plugin_id.empty()
                                                        ? (role_prefix + " (" + res.provider_plugin_id + ")")
                                                        : role_prefix);

                if (res.cancelled || cancel->isCancelled()) {
                    const std::string body = live_raw_content_.empty()
                                                 ? "[已中断本次生成]"
                                                 : (live_raw_content_ + "\n\n*[已中断]*");
                    finalizeActiveStream(provider + " · 已中断", body, live_reasoning_, false);
                } else if (res.ok) {
                    std::string final_raw = live_raw_content_;
                    if (final_raw.empty() && res.data.is_object() && res.data.contains("reply") &&
                        res.data["reply"].is_string()) {
                        final_raw = res.data["reply"].get<std::string>();
                    }
                    if (final_raw.empty()) {
                        final_raw = res.data.empty() ? "(空回复)" : res.data.dump(2);
                    }
                    std::string final_reason = live_reasoning_;
                    if (final_reason.empty() && res.data.is_object() && res.data.contains("reasoning") &&
                        res.data["reasoning"].is_string()) {
                        final_reason = res.data["reasoning"].get<std::string>();
                    }
                    if (res.data.is_object() && res.data.contains("tool_calls_executed") &&
                        res.data["tool_calls_executed"].is_array() &&
                        HostKernel::instance().isInitialized()) {
                        for (const auto& tc : res.data["tool_calls_executed"]) {
                            if (!tc.is_object() || !tc.value("ok", false)) continue;
                            const std::string tname = tc.value("name", "");
                            if (tname == "omnidesk_curation_writeback" ||
                                tname == "omnidesk_curation_rehydrate_archive") {
                                HostKernel::instance().bus().publish("entity.recommend.updated", {
                                    {"mode", "interactive_ephemeral"},
                                    {"source", tname},
                                });
                                break;
                            }
                        }
                    }
                    finalizeActiveStream(provider, final_raw, final_reason, false);
                } else {
                    finalizeActiveStream("请求失败 · " + provider,
                                         res.error.empty() ? "AI 能力调用失败" : res.error,
                                         live_reasoning_,
                                         true);
                }
            });
        },
        cancel);
}

void AiWorkbench::dispatchCurationRequest(const std::string& prompt, bool sync_playlist) {
    cancelActiveStream();

    auto& sess = currentSession();
    const bool first_msg = sess.messages.empty();

    AiChatMessage user_msg;
    user_msg.role = "user";
    user_msg.display_role = "你 · 灵感策展";
    user_msg.content = prompt;
    user_msg.curation = true;
    sess.messages.push_back(std::move(user_msg));
    if (first_msg || sess.title == "新对话" || sess.title == "新灵感策展") {
        sess.title = summarizeSessionTitle(prompt);
    }
    sess.curation_mode = true;
    sess.updated_at = nowUnixSec();

    rebuildSessionSidebar();
    appendMessageTurnView(sess.messages.size() - 1);
    updateStageModeLayout();

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    const bool has_curation = caps.has("ai.curation.v1");
    const bool has_feed = caps.has("recommend.feed.v1");

    if (!has_curation && !has_feed) {
        AiChatMessage err_msg;
        err_msg.role = "assistant";
        err_msg.display_role = "未安装策展插件";
        err_msg.content =
            "尚未安装 `plugin-magpie-agent` (`ai.curation.v1`) 或 `plugin-ai-curator` (`recommend.feed.v1`)。\n"
            "请在插件市场安装后即可直接策展并反写三大工作台与 Apple Music 灵感歌单。";
        err_msg.curation = true;
        err_msg.error = true;
        sess.messages.push_back(std::move(err_msg));
        saveSessionsToDisk();
        appendMessageTurnView(sess.messages.size() - 1);
        scrollToBottom(false);
        return;
    }

    auto* turn = new brls::Box(brls::Axis::COLUMN);
    turn->setWidthPercentage(100);
    turn->setMarginBottom(22.0f);
    live_turn_box_ = turn;

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setMarginBottom(8.0f);
    auto* avatar = new brls::Box(brls::Axis::ROW);
    avatar->setDimensions(26.0f, 26.0f);
    avatar->setCornerRadius(13.0f);
    avatar->setMarginRight(8.0f);
    avatar->setAlignItems(brls::AlignItems::CENTER);
    avatar->setJustifyContent(brls::JustifyContent::CENTER);
    avatar->setBackgroundColor(ui::color("desktop/active"));
    auto* av_icon = new SvgIconView();
    av_icon->setDimensions(15, 15);
    av_icon->setSvgString(svgAiEmblem(nvgHex(ui::color("brls/accent")),
                                      nvgHex(ui::color("desktop/focus_ring"))));
    avatar->addView(av_icon);
    hdr->addView(avatar);

    live_role_label_ = ui::makeLabel(
        has_curation ? "AI 策展大脑 · 正在编排并反写工作台..." : "历史归档策展 · 检索中...",
        14.0f,
        "brls/accent");
    hdr->addView(live_role_label_);
    turn->addView(hdr);

    live_reply_host_ = new brls::Box(brls::Axis::COLUMN);
    live_reply_host_->setWidthPercentage(100);
    live_reply_host_->setPadding(0, 0, 0, 34);
    live_reply_label_ = ui::makeLabel("▍", 15.5f, "brls/text", 1.58f);
    live_reply_host_->addView(live_reply_label_);
    turn->addView(live_reply_host_);

    if (messages_box_) {
        messages_box_->addView(turn);
        messages_box_->invalidate();
    }
    scrollToBottom(false);

    const uint64_t seq = ++active_req_seq_;
    auto cancel = CancelToken::create();
    active_cancel_ = cancel;
    streaming_session_idx_ = active_session_idx_;
    stream_start_ms_ = nowSteadyMs();
    reasoning_end_ms_ = 0;
    live_raw_content_.clear();
    live_reasoning_.clear();
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        pending_content_delta_.clear();
        pending_reasoning_delta_.clear();
        flush_scheduled_ = false;
        last_stream_flush_ms_ = 0;
    }
    if (composer_) {
        composer_->setStreaming(true);
    }

    if (has_curation) {
        nlohmann::json req = {
            {"action", "curate_interactive"},
            {"prompt", prompt},
            {"domain", toString(sess.domain)},
            {"context", sess.context_summary},
            {"repeat_policy", "allow_any"},
            {"sync_playlist", sync_playlist},
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
                {
                    std::lock_guard<std::mutex> lk(stream_mu_);
                    pending_content_delta_ += chunk.delta;
                }
                scheduleStreamUiFlush(seq, true);
            },
            [this, alive = alive_, cancel, seq](const CapabilityResult& res) {
                brls::sync([this, alive, cancel, seq, res]() {
                    if (!alive->load(std::memory_order_acquire) || seq != active_req_seq_) return;
                    std::string delta;
                    {
                        std::lock_guard<std::mutex> lk(stream_mu_);
                        delta.swap(pending_content_delta_);
                        flush_scheduled_ = false;
                    }
                    live_raw_content_ += delta;

                    const std::string provider = res.provider_plugin_id.empty()
                                                     ? "AI 策展大脑"
                                                     : ("AI 策展 (" + res.provider_plugin_id + ")");
                    if (res.cancelled || cancel->isCancelled()) {
                        const std::string body = live_raw_content_.empty()
                                                     ? "[已中断策展]"
                                                     : (live_raw_content_ + "\n\n*[已中断]*");
                        finalizeActiveStream(provider + " · 已中断", body, "", false);
                    } else if (res.ok) {
                        std::string final_text;
                        if (res.data.is_object() && res.data.contains("reply") &&
                            res.data["reply"].is_string() &&
                            !res.data["reply"].get_ref<const std::string&>().empty()) {
                            final_text = res.data["reply"].get<std::string>();
                        } else if (!live_raw_content_.empty()) {
                            final_text = live_raw_content_;
                        } else {
                            final_text = res.data.empty() ? "(策展完成)" : res.data.dump(2);
                        }
                        finalizeActiveStream(provider, final_text, "", false);
                    } else {
                        finalizeActiveStream("策展失败 · " + provider,
                                             res.error.empty() ? "ai.curation.v1 调用失败" : res.error,
                                             "",
                                             true);
                    }
                });
            },
            cancel);
        return;
    }

    // Archive fallback when only plugin-ai-curator is loaded
    caps.callAsync(
        "recommend.feed.v1",
        {{"action", "search_archive"}, {"query", prompt}, {"limit", 8}},
        [this, alive = alive_, cancel, seq, prompt](const CapabilityResult& res) {
            brls::sync([this, alive, cancel, seq, prompt, res]() {
                if (!alive->load(std::memory_order_acquire) || seq != active_req_seq_) return;
                if (res.cancelled || cancel->isCancelled()) {
                    finalizeActiveStream("历史归档策展 · 已中断", "[已取消]", "", false);
                    return;
                }
                if (!res.ok) {
                    finalizeActiveStream("策展检索失败",
                                         res.error.empty() ? "recommend.feed.v1 调用失败" : res.error,
                                         "",
                                         true);
                    return;
                }
                nlohmann::json media_items = nlohmann::json::array();
                nlohmann::json text_items = nlohmann::json::array();
                nlohmann::json book_items = nlohmann::json::array();
                std::string summary = "从已回灌历史归档中找到匹配推荐（未装载 `plugin-magpie-agent`，使用归档回退检索）：\n\n";
                size_t count = 0;
                for (const char* k : {"items", "results", "matches"}) {
                    if (res.data.contains(k) && res.data[k].is_array()) {
                        for (const auto& it : res.data[k]) {
                            if (!it.is_object()) continue;
                            ++count;
                            summary += std::to_string(count) + ". **" + it.value("title", "未命名") + "**";
                            std::string dt = it.value("business_date", it.value("date", ""));
                            if (!dt.empty()) summary += " `[" + dt + "]`";
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
                finalizeActiveStream("历史归档策展 (plugin-ai-curator)", summary, "", false);
            });
        },
        cancel);
}

bool AiWorkbench::cancelActiveStream() {
    if (!active_cancel_) {
        return false;
    }
    auto cancel = active_cancel_;
    cancel->cancel();
    ++active_req_seq_;

    std::string c_delta;
    std::string r_delta;
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        c_delta.swap(pending_content_delta_);
        r_delta.swap(pending_reasoning_delta_);
        flush_scheduled_ = false;
    }
    live_reasoning_ += r_delta;
    live_raw_content_ += c_delta;

    const std::string body = live_raw_content_.empty()
                                 ? "[已取消 AI 请求]"
                                 : (live_raw_content_ + "\n\n*[已按 Esc / 停止按钮中断]*");
    finalizeActiveStream("AI · 已中断", body, live_reasoning_, false);
    return true;
}

void AiWorkbench::finalizeActiveStream(const std::string& display_role,
                                       const std::string& raw_reply,
                                       const std::string& explicit_reasoning,
                                       bool is_error) {
    std::string parsed_think;
    std::string parsed_reply;
    splitThinkAndReply(raw_reply, explicit_reasoning, &parsed_think, &parsed_reply, nullptr);
    if (parsed_reply.empty() && !raw_reply.empty() && parsed_think.empty()) {
        parsed_reply = raw_reply;
    }

    const uint64_t end_ms = reasoning_end_ms_ > stream_start_ms_ ? reasoning_end_ms_ : nowSteadyMs();
    const double think_sec =
        (!parsed_think.empty() && end_ms > stream_start_ms_)
            ? static_cast<double>(end_ms - stream_start_ms_) / 1000.0
            : 0.0;

    const size_t target_idx = std::min(streaming_session_idx_, sessions_.size() - 1);
    auto& sess = sessions_[target_idx];

    AiChatMessage msg;
    msg.role = "assistant";
    msg.display_role = display_role;
    msg.content = parsed_reply.empty() ? "(思考已完成)" : parsed_reply;
    msg.reasoning = parsed_think;
    msg.thinking_sec = think_sec;
    msg.curation = sess.curation_mode;
    msg.error = is_error;
    msg.thinking_collapsed = false;
    sess.messages.push_back(std::move(msg));
    sess.updated_at = nowUnixSec();

    active_cancel_.reset();
    live_raw_content_.clear();
    live_reasoning_.clear();
    if (composer_) {
        composer_->setStreaming(false);
    }

    saveSessionsToDisk();
    rebuildSessionSidebar();

    if (target_idx == active_session_idx_) {
        if (messages_box_ && live_turn_box_) {
            brls::Box* stale_live = live_turn_box_;
            resetLiveTurnRefs();
            messages_box_->removeView(stale_live, true);
            appendMessageTurnView(sess.messages.size() - 1);
        } else {
            rebuildThreadViews();
        }
        updateStageModeLayout();
        scrollToBottom(false);
    } else {
        resetLiveTurnRefs();
    }
}

void AiWorkbench::triggerArchiveRehydrate() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    auto& caps = kernel.capabilities();
    if (!caps.has("recommend.feed.v1") && !caps.has("ai.curation.v1")) {
        brls::Application::notify("尚未安装 plugin-ai-curator，无法回灌历史推荐");
        return;
    }

    brls::Application::notify("正在后台同步 obsidian-vault 与 music-daily 历史归档...");
    const std::string cap = caps.has("recommend.feed.v1") ? "recommend.feed.v1" : "ai.curation.v1";
    const nlohmann::json req =
        caps.has("recommend.feed.v1")
            ? nlohmann::json{{"action", "bootstrap_and_rehydrate"}, {"pull_remote", true}}
            : nlohmann::json{{"action", "rehydrate_archive"}, {"pull_remote", true}};
    caps.callAsync(cap, req, [this, alive = alive_](const CapabilityResult& res) {
        brls::sync([this, alive, res]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok) {
                std::string msg = "双仓历史推荐回灌完成";
                if (res.data.is_object() && res.data.contains("dates_indexed")) {
                    msg += "（已索引 " + std::to_string(res.data.value("dates_indexed", 0)) + " 天归档）";
                }
                brls::Application::notify(msg);
            } else {
                brls::Application::notify("历史回灌失败: " + res.error);
            }
        });
    });
}

} // namespace omnidesk::host
