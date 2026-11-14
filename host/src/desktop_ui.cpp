#include "omnidesk/host/desktop_ui.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <borealis/views/edit_text_dialog.hpp>
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
    brls::View* hint_root = nullptr;
    std::string hint_prefix;
    std::vector<HintTarget> hints;
    bool pending_g = false;
    std::chrono::steady_clock::time_point pending_g_time;
    bool watched_keys_registered = false;
};

State& state() {
    static State s;
    return s;
}

brls::View* currentRootView() {
    const auto& stack = brls::Application::getActivitiesStack();
    return stack.empty() ? nullptr : stack.back()->getContentView();
}

bool isEditingText() {
    return dynamic_cast<brls::EditTextDialog*>(currentRootView()) != nullptr;
}

void cancelHints() {
    auto& st = state();
    st.hint_mode = false;
    st.hint_root = nullptr;
    st.hint_prefix.clear();
    st.hints.clear();
}

brls::Rect intersectRect(const brls::Rect& a, const brls::Rect& b) {
    const float x = std::max(a.getMinX(), b.getMinX());
    const float y = std::max(a.getMinY(), b.getMinY());
    const float w = std::max(0.0f, std::min(a.getMaxX(), b.getMaxX()) - x);
    const float h = std::max(0.0f, std::min(a.getMaxY(), b.getMaxY()) - y);
    return {x, y, w, h};
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

void collectHintTargets(brls::View* root,
                        brls::Rect clip,
                        std::vector<std::pair<brls::View*, brls::Rect>>& out) {
    if (!root || root->getVisibility() != brls::Visibility::VISIBLE || root->getAlpha() < 0.1f) {
        return;
    }
    const brls::Rect frame = intersectRect(root->getFrame(), clip);
    if (frame.getWidth() < 8.0f || frame.getHeight() < 8.0f) {
        return;
    }
    if (root->isFocusable()) {
        out.emplace_back(root, frame);
    }
    if (auto* box = dynamic_cast<brls::Box*>(root)) {
        if (dynamic_cast<brls::ScrollingFrame*>(root)) {
            clip = frame;
        }
        for (brls::View* child : box->getChildren()) {
            collectHintTargets(child, clip, out);
        }
    }
}

void buildHints() {
    auto& st = state();
    st.hints.clear();
    st.hint_prefix.clear();

    brls::View* content = currentRootView();
    if (!content) {
        return;
    }
    const float viewport_w =
        brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float viewport_h =
        brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    std::vector<std::pair<brls::View*, brls::Rect>> views;
    collectHintTargets(content, {0.0f, 0.0f, viewport_w, viewport_h}, views);

    static constexpr const char kAlphabet[] = "ASDFGHJKLQWERTYUIOPZXCVBNM";
    const size_t alpha_n = 26;
    const size_t count = std::min(views.size(), alpha_n * 2);
    for (size_t i = 0; i < count; ++i) {
        HintTarget ht;
        ht.view = views[i].first;
        ht.frame = views[i].second;
        if (i < alpha_n) {
            ht.label = std::string(1, kAlphabet[i]);
        } else {
            ht.label = std::string("G") + kAlphabet[i - alpha_n];
        }
        st.hints.push_back(std::move(ht));
    }
    st.hint_root = content;
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
    brls::Application::controllerShortcutHandler = [](brls::ControllerButton button, bool) {
        auto& st = state();
        if (!st.hint_mode) {
            return false;
        }
        if (button == brls::BUTTON_B) {
            cancelHints();
            return true;
        }
        if (button == brls::BUTTON_A) {
            return true;
        }
        return false;
    };

    brls::Application::keyboardShortcutHandler = [](brls::BrlsKeyCombination combo,
                                                    bool repeating,
                                                    bool fallback) -> bool {
        auto& st = state();
        if (isEditingText()) {
            cancelHints();
            return fallback;
        }
        if (st.hint_mode && st.hint_root != currentRootView()) {
            cancelHints();
        }
        const bool in_modal_activity = brls::Application::getActivitiesStack().size() > 1;

        const int key = combo.code;
        const int mods = combo.mod;
        const bool ctrl = (mods & brls::BRLS_KBD_MODIFIER_CTRL) != 0;
        const bool shift = (mods & brls::BRLS_KBD_MODIFIER_SHIFT) != 0;

        if (!fallback) {
            // Global shortcuts handled before focused view actions:
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
                    cancelHints();
                    return true;
                }
                if (key == brls::BRLS_KBD_KEY_BACKSPACE) {
                    if (!st.hint_prefix.empty()) {
                        st.hint_prefix.pop_back();
                    } else {
                        cancelHints();
                    }
                    return true;
                }
                if (!repeating && !ctrl && key >= brls::BRLS_KBD_KEY_A && key <= brls::BRLS_KBD_KEY_Z) {
                    const char ch = static_cast<char>('A' + (key - brls::BRLS_KBD_KEY_A));
                    st.hint_prefix += ch;
                    std::vector<HintTarget> matches;
                    for (const auto& h : st.hints) {
                        if (h.label.rfind(st.hint_prefix, 0) == 0) {
                            matches.push_back(h);
                        }
                    }
                    if (matches.empty()) {
                        cancelHints();
                    } else if (matches.size() == 1 && matches[0].label == st.hint_prefix) {
                        brls::View* target = matches[0].view;
                        cancelHints();
                        if (target && target->getVisibility() == brls::Visibility::VISIBLE) {
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
                cancelHints();
                return true;
            }

            if (brls::Application::isInputBlocks()) {
                return false;
            }
            if (in_modal_activity) {
                return false;
            }

            if (!repeating && !ctrl) {
                if (key == brls::BRLS_KBD_KEY_F && !shift) {
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
                if (key == brls::BRLS_KBD_KEY_T && !shift) {
                    if (st.callbacks.on_toggle_theme) {
                        st.callbacks.on_toggle_theme();
                        return true;
                    }
                }
            }
            return false;
        }

        // Don't hijack typing if inputs are blocked
        if (brls::Application::isInputBlocks() || in_modal_activity) {
            return false;
        }

        if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_ESCAPE &&
            brls::Application::getActivitiesStack().size() <= 1) {
            if (st.callbacks.on_cancel_active && st.callbacks.on_cancel_active()) {
                return true;
            }
        }

        // Ctrl+D / Ctrl+U half-page scroll
        if (ctrl && key == brls::BRLS_KBD_KEY_D) {
            return scrollCurrentBy(280.0f);
        }
        if (ctrl && key == brls::BRLS_KBD_KEY_U) {
            return scrollCurrentBy(-280.0f);
        }

        if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_G) {
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
        st.pending_g = false;

        // hjkl directional focus movement
        if (!ctrl && !shift) {
            if (key == brls::BRLS_KBD_KEY_H) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_NAV_LEFT, repeating);
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_J) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_NAV_DOWN, repeating);
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_K) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_NAV_UP, repeating);
                return true;
            }
            if (key == brls::BRLS_KBD_KEY_L) {
                brls::Application::onControllerButtonPressed(brls::BUTTON_NAV_RIGHT, repeating);
                return true;
            }
        }

        return false;
    };

    if (!state().watched_keys_registered) {
        state().watched_keys_registered = true;
        const brls::BrlsKeyCombination combos[] = {
            {brls::BRLS_KBD_KEY_F8, 0},
            {brls::BRLS_KBD_KEY_F9, 0},
            {brls::BRLS_KBD_KEY_ESCAPE, 0},
            {brls::BRLS_KBD_KEY_BACKSPACE, 0},
            {brls::BRLS_KBD_KEY_TAB, 0},
            {brls::BRLS_KBD_KEY_TAB, brls::BRLS_KBD_MODIFIER_SHIFT},
            {brls::BRLS_KBD_KEY_TAB, brls::BRLS_KBD_MODIFIER_CTRL},
            {brls::BRLS_KBD_KEY_TAB,
             static_cast<short>(brls::BRLS_KBD_MODIFIER_CTRL | brls::BRLS_KBD_MODIFIER_SHIFT)},
            {brls::BRLS_KBD_KEY_D, brls::BRLS_KBD_MODIFIER_CTRL},
            {brls::BRLS_KBD_KEY_U, brls::BRLS_KBD_MODIFIER_CTRL},
            {brls::BRLS_KBD_KEY_SLASH, 0},
            {brls::BRLS_KBD_KEY_SLASH, brls::BRLS_KBD_MODIFIER_SHIFT},
            {brls::BRLS_KBD_KEY_G, brls::BRLS_KBD_MODIFIER_SHIFT},
        };
        for (const auto& c : combos) {
            brls::Application::addToWatchedKeys(c);
        }
        for (int k = brls::BRLS_KBD_KEY_A; k <= brls::BRLS_KBD_KEY_Z; ++k) {
            brls::Application::addToWatchedKeys(
                {static_cast<brls::BrlsKeyboardScancode>(k), 0});
        }
        for (int k = brls::BRLS_KBD_KEY_0; k <= brls::BRLS_KBD_KEY_9; ++k) {
            brls::Application::addToWatchedKeys(
                {static_cast<brls::BrlsKeyboardScancode>(k), 0});
            if (k >= brls::BRLS_KBD_KEY_1) {
                brls::Application::addToWatchedKeys(
                    {static_cast<brls::BrlsKeyboardScancode>(k), brls::BRLS_KBD_MODIFIER_CTRL});
            }
        }
    }
}

void DesktopInputController::renderHintOverlay(NVGcontext* vg) {
    const auto& st = state();
    if (!st.hint_mode || st.hints.empty()) {
        return;
    }
    if (st.hint_root != currentRootView() || isEditingText()) {
        cancelHints();
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
