#include "omnidesk/host/desktop_ui.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <nanovg.h>
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

struct HintTarget {
    std::string label;
    brls::View* view = nullptr;
    brls::Rect frame;
};

struct State {
    DesktopCallbacks callbacks;
    bool hint_mode = false;
    std::string hint_prefix;
    std::vector<HintTarget> hints;
    bool pending_g = false;
    std::chrono::steady_clock::time_point pending_g_time;
};

State& state() {
    static State s;
    return s;
}

brls::ScrollingFrame* findAncestorScroll(brls::View* v) {
    while (v) {
        if (auto* sf = dynamic_cast<brls::ScrollingFrame*>(v)) {
            return sf;
        }
        v = v->getParent();
    }
    return nullptr;
}

void collectHintTargets(brls::View* root, std::vector<brls::View*>& out) {
    if (!root || root->getVisibility() != brls::Visibility::VISIBLE) {
        return;
    }
    if (root->isFocusable()) {
        brls::Rect r = root->getFrame();
        if (r.size.width > 4.0f && r.size.height > 4.0f) {
            out.push_back(root);
        }
    }
    if (auto* box = dynamic_cast<brls::Box*>(root)) {
        for (brls::View* child : box->getChildren()) {
            collectHintTargets(child, out);
        }
    }
}

void buildHints() {
    auto& st = state();
    st.hints.clear();
    st.hint_prefix.clear();

    const auto& stack = brls::Application::getActivitiesStack();
    if (stack.empty()) {
        return;
    }
    brls::View* content = stack.back()->getContentView();
    std::vector<brls::View*> views;
    collectHintTargets(content, views);

    static constexpr const char kAlphabet[] = "ASDFGHJKLQWERTYUIOPZXCVBNM";
    const size_t alpha_n = 26;
    const size_t count = std::min(views.size(), alpha_n * 2);
    for (size_t i = 0; i < count; ++i) {
        HintTarget ht;
        ht.view = views[i];
        ht.frame = views[i]->getFrame();
        if (i < alpha_n) {
            ht.label = std::string(1, kAlphabet[i]);
        } else {
            ht.label = std::string("G") + kAlphabet[i - alpha_n];
        }
        st.hints.push_back(std::move(ht));
    }
}

bool scrollCurrentBy(float delta_y) {
    brls::View* cur = brls::Application::getCurrentFocus();
    if (!cur) {
        return false;
    }
    if (auto* sf = findAncestorScroll(cur)) {
        sf->setContentOffsetY(std::max(0.0f, sf->getContentOffsetY() + delta_y), true);
        return true;
    }
    return false;
}

bool scrollToEdge(bool bottom) {
    brls::View* cur = brls::Application::getCurrentFocus();
    if (!cur) {
        return false;
    }
    if (auto* sf = findAncestorScroll(cur)) {
        sf->setContentOffsetY(bottom ? 99999.0f : 0.0f, true);
        return true;
    }
    return false;
}

} // namespace

void DesktopInputController::install(DesktopCallbacks callbacks) {
    state().callbacks = std::move(callbacks);

    brls::Application::keyboardOverlayRenderer = renderHintOverlay;

    brls::Application::keyboardShortcutHandler = [](brls::BrlsKeyCombination combo,
                                                    bool repeating,
                                                    bool pressed) -> bool {
        if (!pressed) {
            return false;
        }
        auto& st = state();
        const int key = combo.code;
        const int mods = combo.mod;
        const bool ctrl = (mods & brls::BRLS_KBD_MODIFIER_CTRL) != 0;
        const bool shift = (mods & brls::BRLS_KBD_MODIFIER_SHIFT) != 0;

        // Global shortcuts even while focused anywhere:
        if (!repeating && key == brls::BRLS_KBD_KEY_F8) {
            if (st.callbacks.on_toggle_dock) {
                st.callbacks.on_toggle_dock();
            }
            return true;
        }

        if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_F9) {
            if (st.callbacks.on_skip_ad && st.callbacks.on_skip_ad()) {
                return true;
            }
        }

        if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_ESCAPE && !st.hint_mode &&
            brls::Application::getActivitiesStack().size() <= 1 &&
            !brls::Application::isInputBlocks()) {
            if (st.callbacks.on_cancel_active && st.callbacks.on_cancel_active()) {
                return true;
            }
        }

        if (!repeating && ctrl && key == brls::BRLS_KBD_KEY_TAB) {
            if (st.callbacks.on_cycle_workspace) {
                st.callbacks.on_cycle_workspace(shift ? -1 : 1);
            }
            return true;
        }

        if (!repeating && ctrl && key >= brls::BRLS_KBD_KEY_1 && key <= brls::BRLS_KBD_KEY_9) {
            if (st.callbacks.on_select_workspace) {
                st.callbacks.on_select_workspace(key - brls::BRLS_KBD_KEY_1);
            }
            return true;
        }

        // Vim hint overlay mode
        if (st.hint_mode) {
            if (key == brls::BRLS_KBD_KEY_ESCAPE) {
                st.hint_mode = false;
                st.hints.clear();
                return true;
            }
            if (key >= brls::BRLS_KBD_KEY_A && key <= brls::BRLS_KBD_KEY_Z) {
                const char ch = static_cast<char>('A' + (key - brls::BRLS_KBD_KEY_A));
                st.hint_prefix += ch;
                std::vector<HintTarget> matches;
                for (const auto& h : st.hints) {
                    if (h.label.rfind(st.hint_prefix, 0) == 0) {
                        matches.push_back(h);
                    }
                }
                if (matches.empty()) {
                    st.hint_mode = false;
                    st.hints.clear();
                } else if (matches.size() == 1 && matches[0].label == st.hint_prefix) {
                    brls::View* target = matches[0].view;
                    st.hint_mode = false;
                    st.hints.clear();
                    if (target) {
                        brls::Application::giveFocus(target);
                        for (const auto& act : target->getActions()) {
                            if (act && act->getButton() == brls::BUTTON_A && act->isAvailable()) {
                                auto listener = act->getActionListener();
                                if (listener) {
                                    target->playClickAnimation();
                                    listener(target);
                                }
                                break;
                            }
                        }
                    }
                }
                return true;
            }
            st.hint_mode = false;
            st.hints.clear();
            return true;
        }

        // Don't hijack typing if inputs are blocked
        if (brls::Application::isInputBlocks()) {
            return false;
        }

        // Ctrl+D / Ctrl+U half-page scroll
        if (ctrl && key == brls::BRLS_KBD_KEY_D) {
            return scrollCurrentBy(280.0f);
        }
        if (ctrl && key == brls::BRLS_KBD_KEY_U) {
            return scrollCurrentBy(-280.0f);
        }

        if (!repeating && !ctrl) {
            if (key == brls::BRLS_KBD_KEY_F) {
                buildHints();
                st.hint_mode = !st.hints.empty();
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_SLASH && !shift) {
                if (st.callbacks.on_search) {
                    st.callbacks.on_search();
                    return true;
                }
            }
            if (key == brls::BRLS_KBD_KEY_SLASH && shift) { // '?'
                showShortcutsHelp();
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_T) {
                if (st.callbacks.on_toggle_theme) {
                    st.callbacks.on_toggle_theme();
                    return true;
                }
            }
            if (key == brls::BRLS_KBD_KEY_G) {
                if (shift) {
                    st.pending_g = false;
                    return scrollToEdge(true);
                }
                auto now = std::chrono::steady_clock::now();
                if (st.pending_g &&
                    std::chrono::duration_cast<std::chrono::milliseconds>(now - st.pending_g_time).count() < 550) {
                    st.pending_g = false;
                    return scrollToEdge(false);
                }
                st.pending_g = true;
                st.pending_g_time = now;
                return true;
            }
        }

        // hjkl directional focus movement
        if (!ctrl && !shift) {
            if (key == brls::BRLS_KBD_KEY_H) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_LEFT, repeating);
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_J) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_DOWN, repeating);
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_K) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_UP, repeating);
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_L) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_RIGHT, repeating);
                return true;
            }
        }

        return false;
    };
}

void DesktopInputController::renderHintOverlay(NVGcontext* vg) {
    const auto& st = state();
    if (!st.hint_mode || st.hints.empty()) {
        return;
    }

    const NVGcolor accent = ui::color("brls/accent");
    const NVGcolor bg = ui::color("desktop/surface");
    const NVGcolor text_col = ui::color("brls/text");

    nvgSave(vg);
    nvgFontFace(vg, "regular");
    nvgFontSize(vg, 14.0f);
    nvgTextAlign(vg, NVG_ALIGN_CENTER | NVG_ALIGN_MIDDLE);

    for (const auto& h : st.hints) {
        if (!st.hint_prefix.empty() && h.label.rfind(st.hint_prefix, 0) != 0) {
            continue;
        }
        const float badge_w = h.label.size() > 1 ? 30.0f : 22.0f;
        const float badge_h = 20.0f;
        const float bx = std::max(4.0f, h.frame.origin.x + 2.0f);
        const float by = std::max(4.0f, h.frame.origin.y + 2.0f);

        nvgBeginPath(vg);
        nvgRoundedRect(vg, bx, by, badge_w, badge_h, 5.0f);
        nvgFillColor(vg, bg);
        nvgFill(vg);
        nvgStrokeWidth(vg, 1.5f);
        nvgStrokeColor(vg, accent);
        nvgStroke(vg);

        nvgFillColor(vg, text_col);
        nvgText(vg, bx + badge_w * 0.5f, by + badge_h * 0.5f, h.label.c_str(), nullptr);
    }

    nvgRestore(vg);
}

void DesktopInputController::showShortcutsHelp() {
    auto* box = new brls::Box(brls::Axis::COLUMN);
    box->setPadding(22);
    box->setWidth(520);

    auto* title = ui::makeLabel("OmniDesk 桌面快捷键", 22.0f, "brls/text");
    title->setMarginBottom(12);
    box->addView(title);

    struct Row {
        const char* keys;
        const char* desc;
    };
    const Row rows[] = {
        {"Ctrl + 1..9", "切换工作台（插件市场 / 影视音乐 / 文字期刊 / 书本PDF / 插件扩展）"},
        {"Ctrl + Tab / Shift+Tab", "顺序或反向切换左侧工作区标签"},
        {"F8", "显示 / 隐藏右侧或底部 AI 伴读协作栏"},
        {"F9", "跳过当前 SponsorBlock / 广告片段"},
        {"Esc", "中断当前 AI 流式推理或媒体异步拉取"},
        {"h / j / k / l", "方向焦点导航（与方向键等效）"},
        {"f", "激活全局字母直达角标（输入字母即可点击对应控件）"},
        {"gg / G / Ctrl+D / Ctrl+U", "滚至顶部 / 底部 / 半页下翻 / 半页上翻"},
        {"/", "聚焦当前工作台搜索 / 过滤入口"},
        {"t", "切换 Paper 亮色纸感 / Graphite 暗色石墨主题"},
        {"?", "打开当前快捷键速查表"},
    };

    for (const auto& r : rows) {
        auto* row = new brls::Box(brls::Axis::ROW);
        row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        row->setAlignItems(brls::AlignItems::CENTER);
        row->setMarginBottom(8);

        row->addView(ui::makePill(r.keys, "desktop/soft", "brls/accent"));
        auto* desc = ui::makeLabel(r.desc, 15.0f, "font/grey");
        desc->setGrow(1.0f);
        desc->setMarginLeft(12);
        row->addView(desc);
        box->addView(row);
    }

    auto* dialog = new brls::Dialog(box);
    dialog->addButton("关闭", [] {});
    dialog->open();
}

} // namespace omnidesk::host
