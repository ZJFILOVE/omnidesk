#include "omnidesk/host/desktop_ui.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <borealis/views/dialog.hpp>
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

enum class RepeatOp {
    None = 0,
    NavUp,
    NavDown,
    NavLeft,
    NavRight,
    HalfPageDown,
    HalfPageUp,
    FullPageDown,
    FullPageUp,
};

struct State {
    DesktopCallbacks callbacks;
    bool hint_mode = false;
    brls::View* hint_root = nullptr;
    std::string hint_prefix;
    std::vector<HintTarget> hints;

    bool pending_g = false;
    std::chrono::steady_clock::time_point pending_g_time{};

    bool watched_keys_registered = false;
    bool lifecycle_hooks_registered = false;
    bool internal_passthrough = false;

    // 2D column-preserving navigation & focus continuity across UI rebuilds
    float preferred_x = -1.0f;
    bool preserving_x = false;
    brls::View* last_root = nullptr;
    brls::Rect last_focus_rect{0.0f, 0.0f, 0.0f, 0.0f};
    bool has_last_focus_rect = false;
    brls::View* last_pane_box = nullptr;

    // Scroll stabilization bracket around View::onFocusGained()
    brls::ScrollingFrame* pre_focus_scroll_sf = nullptr;
    float pre_focus_scroll_y = 0.0f;
    bool adjust_scroll_after_focus = false;
    bool immediate_scroll = false;
    bool suppress_focus_scroll = false;

    // Responsive 26 Hz desktop key-repeat state
    RepeatOp repeat_op = RepeatOp::None;
    std::chrono::steady_clock::time_point next_repeat_time{};
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

bool isViewInActiveTree(brls::View* v, brls::View* root) {
    if (!v || !root || v->isPtrLocked()) {
        return false;
    }
    brls::View* cur = v;
    while (cur) {
        if (cur->getVisibility() != brls::Visibility::VISIBLE || cur->isHidden()) {
            return false;
        }
        if (cur == root) {
            return true;
        }
        cur = cur->getParent();
    }
    return false;
}

bool isUsableFocusTarget(brls::View* v, brls::View* root) {
    if (!v || !v->isFocusable()) {
        return false;
    }
    if (dynamic_cast<brls::ScrollingFrame*>(v) != nullptr) {
        return false;
    }
    if (!isViewInActiveTree(v, root)) {
        return false;
    }
    const brls::Rect f = v->getFrame();
    return f.getWidth() >= 4.0f && f.getHeight() >= 4.0f;
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

bool isDescendantOf(brls::View* v, brls::View* ancestor) {
    if (!v || !ancestor) return false;
    while (v) {
        if (v == ancestor) return true;
        v = v->getParent();
    }
    return false;
}

bool hasGamepadAction(brls::View* start, brls::ControllerButton button) {
    brls::View* cur = start ? start : currentRootView();
    while (cur) {
        for (const auto& action : cur->getActions()) {
            if (action &&
                action->getType() == brls::ACTION_GAMEPAD &&
                action->getButton() == button &&
                action->isAvailable()) {
                return true;
            }
        }
        cur = cur->getParent();
    }
    return false;
}

// Flips Borealis out of InputType::TOUCH without dropping the user's first keypress.
void ensureGamepadMode() {
    if (brls::Application::getInputType() == brls::InputType::GAMEPAD) {
        return;
    }
    auto& st = state();
    const brls::ControllerButton candidates[] = {
        brls::BUTTON_NAV_UP,
        brls::BUTTON_NAV_DOWN,
        brls::BUTTON_NAV_LEFT,
        brls::BUTTON_NAV_RIGHT,
    };
    brls::ControllerButton safe_btn = brls::BUTTON_NAV_UP;
    brls::View* cur = brls::Application::getCurrentFocus();
    for (auto b : candidates) {
        if (!hasGamepadAction(cur, b)) {
            safe_btn = b;
            break;
        }
    }
    st.internal_passthrough = true;
    brls::Application::onControllerButtonPressed(safe_btn, false);
    st.internal_passthrough = false;
}

void attachChildAwareTapIfNeeded(brls::Box* box) {
    if (!box || !box->isFocusable() || !box->getGestureRecognizers().empty()) {
        return;
    }
    box->addGestureRecognizer(new brls::TapGestureRecognizer(
        [box](brls::TapGestureStatus status, brls::Sound* soundToPlay) {
            if (!box->isFocusable() || box->getVisibility() != brls::Visibility::VISIBLE) {
                return;
            }
            for (brls::View* hit = box->hitTest(status.position);
                 hit && hit != box;
                 hit = hit->getParent()) {
                if (hit->isFocusable()) {
                    return;
                }
            }
            if (status.state == brls::GestureState::START ||
                status.state == brls::GestureState::UNSURE) {
                box->playClickAnimation(false, false);
            } else if (status.state == brls::GestureState::FAILED ||
                       status.state == brls::GestureState::INTERRUPTED) {
                box->playClickAnimation(true);
            } else if (status.state == brls::GestureState::END) {
                box->playClickAnimation(true);
                brls::Application::giveFocus(box);
                for (const auto& act : box->getActions()) {
                    if (act && act->getButton() == brls::BUTTON_A && act->isAvailable()) {
                        if (soundToPlay) *soundToPlay = act->getSound();
                        auto listener = act->getActionListener();
                        if (listener) listener(box);
                        break;
                    }
                }
            }
        }));
}

// Normalizes ScrollingFrames (prevents NATURAL focus traps/stealing) and ensures focusable Boxes
// respond to mouse taps and clean highlight rendering across Host, SDK, and Plugins.
void normalizeViewTree(brls::View* node) {
    if (!node || node->getVisibility() != brls::Visibility::VISIBLE) {
        return;
    }
    if (auto* sf = dynamic_cast<brls::ScrollingFrame*>(node)) {
        sf->setScrollingBehavior(brls::ScrollingBehavior::CENTERED);
        sf->setFocusable(false);
    }
    if (auto* box = dynamic_cast<brls::Box*>(node)) {
        if (box->isFocusable()) {
            box->setHideHighlightBackground(true);
            box->setHideHighlightBorder(true);
            attachChildAwareTapIfNeeded(box);
        }
        for (brls::View* child : box->getChildren()) {
            normalizeViewTree(child);
        }
    }
}

void collectFocusableViews(brls::View* node, std::vector<brls::View*>& out) {
    if (!node || node->getVisibility() != brls::Visibility::VISIBLE || node->isHidden()) {
        return;
    }
    if (node->isFocusable() && dynamic_cast<brls::ScrollingFrame*>(node) == nullptr) {
        const brls::Rect f = node->getFrame();
        if (f.getWidth() >= 4.0f && f.getHeight() >= 4.0f) {
            out.push_back(node);
        }
    }
    if (auto* box = dynamic_cast<brls::Box*>(node)) {
        for (brls::View* child : box->getChildren()) {
            collectFocusableViews(child, out);
        }
    }
}

std::vector<brls::View*> resolveOrderedPanes(brls::View* root) {
    std::vector<brls::View*> panes;
    if (!root) return panes;

    auto pushIfFocusable = [&](brls::View* v) {
        if (!v || v->getVisibility() != brls::Visibility::VISIBLE) return;
        if (std::find(panes.begin(), panes.end(), v) != panes.end()) return;
        std::vector<brls::View*> items;
        collectFocusableViews(v, items);
        if (!items.empty()) {
            panes.push_back(v);
        }
    };

    pushIfFocusable(root->getView("omnidesk/pane_rail"));
    pushIfFocusable(root->getView("omnidesk/pane_banner"));

    if (auto* host_box = dynamic_cast<brls::Box*>(root->getView("omnidesk/views_host"))) {
        for (brls::View* ws : host_box->getChildren()) {
            if (!ws || ws->getVisibility() != brls::Visibility::VISIBLE) continue;
            const size_t before = panes.size();
            pushIfFocusable(ws->getView("omnidesk/pane_header"));
            pushIfFocusable(ws->getView("omnidesk/pane_filters"));
            pushIfFocusable(ws->getView("omnidesk/pane_primary"));
            pushIfFocusable(ws->getView("omnidesk/pane_secondary"));
            if (panes.size() == before) {
                pushIfFocusable(ws);
            }
        }
    }

    pushIfFocusable(root->getView("omnidesk/pane_player"));
    pushIfFocusable(root->getView("omnidesk/pane_dock"));
    return panes;
}

brls::View* findEnclosingPane(brls::View* v, brls::View* root) {
    for (brls::View* pane : resolveOrderedPanes(root)) {
        if (isDescendantOf(v, pane)) {
            return pane;
        }
    }
    return nullptr;
}

void applyComfortScroll(brls::ScrollingFrame* sf,
                        brls::View* target,
                        float pre_offset_y,
                        bool immediate) {
    if (!sf || !target || sf->getChildren().empty()) {
        return;
    }
    brls::View* content = sf->getChildren().front();
    if (!content) {
        return;
    }
    const float view_h = sf->getHeight();
    const float content_h = content->getHeight();
    if (view_h <= 1.0f || content_h <= view_h + 1.0f) {
        sf->setContentOffsetY(0.0f, false);
        return;
    }

    const float max_scroll = std::max(0.0f, content_h - view_h);
    const float clamped_pre = std::clamp(pre_offset_y, 0.0f, max_scroll);
    const float rel_top = target->getY() - content->getY();
    const float item_h = target->getHeight();
    const float rel_bottom = rel_top + item_h;
    const float pad = std::min(32.0f, view_h * 0.12f);

    const float vis_top = rel_top - clamped_pre;
    const float vis_bottom = rel_bottom - clamped_pre;

    float desired = clamped_pre;
    if (item_h + 2.0f * pad >= view_h) {
        // Tall item (e.g. long article card or comment block): keep stable if already spanning
        // the viewport, otherwise align its top cleanly inside the comfort margin.
        if (!(vis_top <= pad && vis_bottom >= view_h - pad)) {
            desired = std::clamp(rel_top - pad, 0.0f, max_scroll);
        }
    } else {
        if (vis_top < pad) {
            desired = std::clamp(rel_top - pad, 0.0f, max_scroll);
        } else if (vis_bottom > view_h - pad) {
            desired = std::clamp(rel_bottom - (view_h - pad), 0.0f, max_scroll);
        }
    }

    if (std::fabs(desired - clamped_pre) <= 1.5f) {
        // Cancel Borealis's aggressive mid-screen recentering when item is already visible.
        sf->setContentOffsetY(clamped_pre, false);
    } else {
        sf->setContentOffsetY(desired, !immediate);
    }
}

void giveFocusClean(brls::View* target, bool immediate_scroll, bool preserve_x = false) {
    if (!target) return;
    auto& st = state();
    const bool prev_preserve = st.preserving_x;
    const bool prev_imm = st.immediate_scroll;
    st.preserving_x = preserve_x;
    st.immediate_scroll = immediate_scroll;
    brls::Application::giveFocus(target);
    st.preserving_x = prev_preserve;
    st.immediate_scroll = prev_imm;
}

void recoverFocusIfNeeded() {
    if (isEditingText()) {
        return;
    }
    brls::View* root = currentRootView();
    if (!root) {
        return;
    }

    auto& st = state();
    brls::View* cur = brls::Application::getCurrentFocus();
    if (isUsableFocusTarget(cur, root)) {
        st.last_root = root;
        st.last_focus_rect = cur->getFrame();
        st.has_last_focus_rect = true;
        st.last_pane_box = findEnclosingPane(cur, root);
        if (st.preferred_x < 0.0f) {
            st.preferred_x = st.last_focus_rect.getMidX();
        }
        return;
    }

    // Focus was lost (nullptr, hidden workspace, or destroyed by clearViews()/refresh()).
    std::vector<brls::View*> candidates;
    if (st.last_root == root && st.has_last_focus_rect) {
        if (st.last_pane_box && isViewInActiveTree(st.last_pane_box, root)) {
            collectFocusableViews(st.last_pane_box, candidates);
        }
        if (candidates.empty()) {
            collectFocusableViews(root, candidates);
        }
        brls::View* best = nullptr;
        float best_score = std::numeric_limits<float>::max();
        const float tx = st.last_focus_rect.getMidX();
        const float ty = st.last_focus_rect.getMidY();
        for (brls::View* c : candidates) {
            const brls::Rect f = c->getFrame();
            const float score = std::fabs(f.getMidX() - tx) * 1.15f + std::fabs(f.getMidY() - ty);
            if (score < best_score) {
                best_score = score;
                best = c;
            }
        }
        if (best) {
            st.suppress_focus_scroll = true;
            giveFocusClean(best, true, true);
            st.suppress_focus_scroll = false;
            return;
        }
    }

    // Fresh root activity (initial launch or newly opened modal): focus primary stage or default.
    brls::View* initial = nullptr;
    if (auto* host_box = dynamic_cast<brls::Box*>(root->getView("omnidesk/views_host"))) {
        for (brls::View* ws : host_box->getChildren()) {
            if (ws && ws->getVisibility() == brls::Visibility::VISIBLE) {
                if (brls::View* primary = ws->getView("omnidesk/pane_primary")) {
                    initial = primary->getDefaultFocus();
                }
                if (!initial) {
                    initial = ws->getDefaultFocus();
                }
                break;
            }
        }
    }
    if (!initial) {
        initial = root->getDefaultFocus();
    }
    if (!initial) {
        collectFocusableViews(root, candidates);
        if (!candidates.empty()) {
            initial = candidates.front();
        }
    }
    if (initial) {
        st.suppress_focus_scroll = true;
        giveFocusClean(initial, true, false);
        st.suppress_focus_scroll = false;
    }
}

float horizontalDistanceToX(const brls::Rect& r, float x) {
    if (x >= r.getMinX() && x <= r.getMaxX()) {
        return std::fabs(r.getMidX() - x) * 0.15f;
    }
    return std::min(std::fabs(r.getMinX() - x), std::fabs(r.getMaxX() - x));
}

brls::View* matchColumnInRow(brls::View* row_sample, brls::View* scope_root, float target_x) {
    if (!row_sample || !scope_root) {
        return row_sample;
    }
    const brls::Rect sf_rect = row_sample->getFrame();
    const float row_mid_y = sf_rect.getMidY();
    const float y_tol = std::max(16.0f, sf_rect.getHeight() * 0.55f);

    // Restrict column matching to the same pane / ScrollingFrame so adjacent panes don't mix.
    brls::View* container = findAncestorScroll(row_sample);
    if (!container) {
        container = findEnclosingPane(row_sample, scope_root);
    }
    if (!container) {
        container = row_sample->getParent();
    }
    if (!container) {
        return row_sample;
    }

    std::vector<brls::View*> peers;
    collectFocusableViews(container, peers);

    brls::View* best = row_sample;
    float best_dist = std::numeric_limits<float>::max();
    for (brls::View* p : peers) {
        const brls::Rect pf = p->getFrame();
        if (std::fabs(pf.getMidY() - row_mid_y) <= y_tol) {
            const float d = horizontalDistanceToX(pf, target_x);
            if (d < best_dist) {
                best_dist = d;
                best = p;
            }
        }
    }
    return best;
}

brls::View* findSpatialNeighbor(brls::View* cur,
                                brls::View* root,
                                brls::FocusDirection dir,
                                float preferred_x) {
    if (!cur || !root) return nullptr;
    std::vector<brls::View*> all;
    collectFocusableViews(root, all);

    const brls::Rect cf = cur->getFrame();
    const float cx = cf.getMidX();
    const float cy = cf.getMidY();
    const float ref_x = (dir == brls::FocusDirection::UP || dir == brls::FocusDirection::DOWN)
                            ? preferred_x
                            : cx;

    brls::View* cur_pane = findEnclosingPane(cur, root);

    brls::View* best_same_col = nullptr;
    float best_same_score = std::numeric_limits<float>::max();
    brls::View* best_any = nullptr;
    float best_any_score = std::numeric_limits<float>::max();

    for (brls::View* cand : all) {
        if (cand == cur) continue;
        const brls::Rect f = cand->getFrame();
        const float mx = f.getMidX();
        const float my = f.getMidY();

        float primary = 0.0f;
        float ortho = 0.0f;
        bool valid = false;

        switch (dir) {
            case brls::FocusDirection::UP:
                if (my < cy - 6.0f && f.getMaxY() <= cy + 2.0f) {
                    valid = true;
                    primary = cy - f.getMaxY();
                    ortho = horizontalDistanceToX(f, ref_x);
                }
                break;
            case brls::FocusDirection::DOWN:
                if (my > cy + 6.0f && f.getMinY() >= cy - 2.0f) {
                    valid = true;
                    primary = f.getMinY() - cy;
                    ortho = horizontalDistanceToX(f, ref_x);
                }
                break;
            case brls::FocusDirection::LEFT:
                if (mx < cx - 6.0f && f.getMaxX() <= cx + 2.0f) {
                    valid = true;
                    primary = cx - f.getMaxX();
                    ortho = (cy >= f.getMinY() && cy <= f.getMaxY())
                                ? std::fabs(my - cy) * 0.2f
                                : std::min(std::fabs(f.getMinY() - cy), std::fabs(f.getMaxY() - cy));
                }
                break;
            case brls::FocusDirection::RIGHT:
                if (mx > cx + 6.0f && f.getMinX() >= cx - 2.0f) {
                    valid = true;
                    primary = f.getMinX() - cx;
                    ortho = (cy >= f.getMinY() && cy <= f.getMaxY())
                                ? std::fabs(my - cy) * 0.2f
                                : std::min(std::fabs(f.getMinY() - cy), std::fabs(f.getMaxY() - cy));
                }
                break;
        }
        if (!valid) continue;

        const float score = std::max(0.0f, primary) + ortho * 2.2f;
        if (dir == brls::FocusDirection::UP || dir == brls::FocusDirection::DOWN) {
            const bool same_band = (f.getMaxX() >= cf.getMinX() - 48.0f &&
                                    f.getMinX() <= cf.getMaxX() + 48.0f) ||
                                   (cur_pane && findEnclosingPane(cand, root) == cur_pane);
            if (same_band && score < best_same_score) {
                best_same_score = score;
                best_same_col = cand;
            }
        }
        if (score < best_any_score) {
            best_any_score = score;
            best_any = cand;
        }
    }

    if ((dir == brls::FocusDirection::UP || dir == brls::FocusDirection::DOWN) && best_same_col) {
        return best_same_col;
    }
    return best_any;
}

bool navigateDirection(brls::FocusDirection dir, bool repeating) {
    if (isEditingText() || brls::Application::isInputBlocks()) {
        return false;
    }
    ensureGamepadMode();
    recoverFocusIfNeeded();

    brls::View* root = currentRootView();
    brls::View* cur = brls::Application::getCurrentFocus();
    if (!cur || !root) {
        return false;
    }

    auto& st = state();
    const bool vertical = (dir == brls::FocusDirection::UP || dir == brls::FocusDirection::DOWN);
    if (st.preferred_x < 0.0f || !vertical) {
        st.preferred_x = cur->getFrame().getMidX();
    }

    brls::View* next = nullptr;
    if (cur->hasCustomNavigationRouteByPtr(dir)) {
        next = cur->getCustomNavigationRoutePtr(dir);
    } else if (cur->hasCustomNavigationRouteById(dir)) {
        next = cur->getNearestView(cur->getCustomNavigationRouteId(dir));
    } else if (cur->hasParent()) {
        next = cur->getParent()->getNextFocus(dir, cur);
    }

    if (next) {
        next = next->getDefaultFocus();
    }

    // Preserve horizontal column across multi-column rows, chip grids, and button bars.
    if (next && vertical && isUsableFocusTarget(next, root)) {
        next = matchColumnInRow(next, root, st.preferred_x);
    }

    // Spatial fallback when structural Box traversal hits a container boundary.
    if (!next || next == cur || !isUsableFocusTarget(next, root)) {
        next = findSpatialNeighbor(cur, root, dir, st.preferred_x);
        if (next && vertical) {
            next = matchColumnInRow(next, root, st.preferred_x);
        }
    }

    if (next && next != cur && isUsableFocusTarget(next, root)) {
        if (auto* audio = brls::Application::getAudioPlayer()) {
            audio->play(next->getFocusSound());
        }
        giveFocusClean(next, repeating, vertical);
        if (!vertical) {
            st.preferred_x = next->getFrame().getMidX();
        }
        return true;
    }

    if (!repeating) {
        cur->shakeHighlight(dir);
    }
    return true;
}

bool triggerFocusedClick() {
    ensureGamepadMode();
    recoverFocusIfNeeded();
    brls::View* cur = brls::Application::getCurrentFocus();
    brls::View* root = currentRootView();
    if (!isUsableFocusTarget(cur, root)) {
        return false;
    }
    for (const auto& act : cur->getActions()) {
        if (act && act->getButton() == brls::BUTTON_A && act->isAvailable()) {
            auto listener = act->getActionListener();
            if (listener && listener(cur)) {
                cur->playClickAnimation();
                if (auto* audio = brls::Application::getAudioPlayer()) {
                    audio->play(act->getSound());
                }
                return true;
            }
        }
    }
    return false;
}

bool cyclePaneOrModalFocus(bool reverse) {
    ensureGamepadMode();
    recoverFocusIfNeeded();
    brls::View* root = currentRootView();
    if (!root) return false;

    const bool in_modal = brls::Application::getActivitiesStack().size() > 1;
    brls::View* cur = brls::Application::getCurrentFocus();

    if (!in_modal) {
        std::vector<brls::View*> panes = resolveOrderedPanes(root);
        if (panes.size() >= 2) {
            int cur_idx = -1;
            for (size_t i = 0; i < panes.size(); ++i) {
                if (isDescendantOf(cur, panes[i])) {
                    cur_idx = static_cast<int>(i);
                    break;
                }
            }
            const int n = static_cast<int>(panes.size());
            const int next_idx = (cur_idx < 0)
                                     ? (reverse ? n - 1 : 0)
                                     : ((cur_idx + (reverse ? n - 1 : 1)) % n);
            brls::View* target = panes[next_idx]->getDefaultFocus();
            if (!isUsableFocusTarget(target, root)) {
                std::vector<brls::View*> items;
                collectFocusableViews(panes[next_idx], items);
                if (!items.empty()) target = items.front();
            }
            if (isUsableFocusTarget(target, root)) {
                giveFocusClean(target, false, false);
                state().preferred_x = target->getFrame().getMidX();
                return true;
            }
        }
    }

    // Modal dialog (or single-pane view): cycle focusable controls in visual reading order.
    std::vector<brls::View*> items;
    collectFocusableViews(root, items);
    if (items.empty()) return false;

    int idx = -1;
    for (size_t i = 0; i < items.size(); ++i) {
        if (items[i] == cur) {
            idx = static_cast<int>(i);
            break;
        }
    }
    const int n = static_cast<int>(items.size());
    const int next_idx = (idx < 0)
                             ? (reverse ? n - 1 : 0)
                             : ((idx + (reverse ? n - 1 : 1)) % n);
    brls::View* target = items[next_idx];
    if (target && target != cur) {
        giveFocusClean(target, false, false);
        state().preferred_x = target->getFrame().getMidX();
    }
    return true;
}

brls::ScrollingFrame* findBestScrollTarget() {
    brls::View* cur = brls::Application::getCurrentFocus();
    if (auto* sf = findAncestorScroll(cur)) {
        return sf;
    }
    brls::View* root = currentRootView();
    if (!root) return nullptr;

    // Fallback: locate the largest visible ScrollingFrame in the active workspace/modal.
    brls::ScrollingFrame* best = nullptr;
    float best_area = 0.0f;
    auto walk = [&](auto& self, brls::View* node) -> void {
        if (!node || node->getVisibility() != brls::Visibility::VISIBLE) return;
        if (auto* sf = dynamic_cast<brls::ScrollingFrame*>(node)) {
            const float area = sf->getWidth() * sf->getHeight();
            if (area > best_area) {
                best_area = area;
                best = sf;
            }
        }
        if (auto* box = dynamic_cast<brls::Box*>(node)) {
            for (brls::View* ch : box->getChildren()) {
                self(self, ch);
            }
        }
    };
    walk(walk, root);
    return best;
}

void syncFocusIntoScrolledViewport(brls::ScrollingFrame* sf, float new_offset_y, int edge_bias) {
    if (!sf || sf->getChildren().empty()) return;
    brls::View* content = sf->getChildren().front();
    if (!content) return;

    const float view_h = sf->getHeight();
    if (view_h <= 1.0f) return;

    brls::View* cur = brls::Application::getCurrentFocus();
    if (cur && isDescendantOf(cur, sf)) {
        const float rel_top = cur->getY() - content->getY();
        const float rel_bot = rel_top + cur->getHeight();
        if (rel_bot > new_offset_y + 12.0f && rel_top < new_offset_y + view_h - 12.0f) {
            return; // Current focus remains visible at the new scroll offset.
        }
    }

    std::vector<brls::View*> items;
    collectFocusableViews(sf, items);
    if (items.empty()) return;

    const float target_rel_y = (edge_bias < 0)
                                   ? (new_offset_y + 24.0f)
                                   : (edge_bias > 0)
                                         ? (new_offset_y + view_h - 24.0f)
                                         : (new_offset_y + view_h * 0.35f);
    const float pref_x = state().preferred_x >= 0.0f ? state().preferred_x : sf->getFrame().getMidX();

    brls::View* best = nullptr;
    float best_score = std::numeric_limits<float>::max();
    for (brls::View* item : items) {
        const float rel_mid_y = (item->getY() - content->getY()) + item->getHeight() * 0.5f;
        const float dy = std::fabs(rel_mid_y - target_rel_y);
        const float dx = horizontalDistanceToX(item->getFrame(), pref_x) * 0.25f;
        if (dy + dx < best_score) {
            best_score = dy + dx;
            best = item;
        }
    }
    if (best && best != cur) {
        auto& st = state();
        st.suppress_focus_scroll = true;
        giveFocusClean(best, true, true);
        st.suppress_focus_scroll = false;
    }
}

bool scrollCurrentBy(float delta_y, bool immediate = false) {
    ensureGamepadMode();
    auto* sf = findBestScrollTarget();
    if (!sf || sf->getChildren().empty()) {
        return false;
    }
    brls::View* content = sf->getChildren().front();
    const float max_scroll = content ? std::max(0.0f, content->getHeight() - sf->getHeight()) : 99999.0f;
    const float target_y = std::clamp(sf->getContentOffsetY() + delta_y, 0.0f, max_scroll);
    syncFocusIntoScrolledViewport(sf, target_y, 0);
    sf->setContentOffsetY(target_y, !immediate);
    return true;
}

bool scrollToEdge(bool bottom) {
    ensureGamepadMode();
    auto* sf = findBestScrollTarget();
    if (!sf || sf->getChildren().empty()) {
        return false;
    }
    brls::View* content = sf->getChildren().front();
    const float max_scroll = content ? std::max(0.0f, content->getHeight() - sf->getHeight()) : 0.0f;
    const float target_y = bottom ? max_scroll : 0.0f;
    syncFocusIntoScrolledViewport(sf, target_y, bottom ? 1 : -1);
    sf->setContentOffsetY(target_y, true);
    return true;
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
    if (root->isFocusable() && dynamic_cast<brls::ScrollingFrame*>(root) == nullptr) {
        out.emplace_back(root, frame);
    }
    if (auto* box = dynamic_cast<brls::Box*>(root)) {
        if (dynamic_cast<brls::ScrollingFrame*>(root) != nullptr || root->getClipsToBounds()) {
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

RepeatOp detectHeldRepeatOp() {
    auto* plat = brls::Application::getPlatform();
    if (!plat || !plat->getInputManager()) {
        return RepeatOp::None;
    }
    if (isEditingText() || state().hint_mode || brls::Application::isInputBlocks()) {
        return RepeatOp::None;
    }
    auto* im = plat->getInputManager();
    const bool ctrl = im->getKeyboardKeyState(brls::BRLS_KBD_KEY_LEFT_CONTROL) ||
                      im->getKeyboardKeyState(brls::BRLS_KBD_KEY_RIGHT_CONTROL);
    const bool alt = im->getKeyboardKeyState(brls::BRLS_KBD_KEY_LEFT_ALT) ||
                     im->getKeyboardKeyState(brls::BRLS_KBD_KEY_RIGHT_ALT);
    const bool shift = im->getKeyboardKeyState(brls::BRLS_KBD_KEY_LEFT_SHIFT) ||
                       im->getKeyboardKeyState(brls::BRLS_KBD_KEY_RIGHT_SHIFT);
    const bool meta = im->getKeyboardKeyState(brls::BRLS_KBD_KEY_LEFT_SUPER) ||
                      im->getKeyboardKeyState(brls::BRLS_KBD_KEY_RIGHT_SUPER);
    if (alt || meta) {
        return RepeatOp::None;
    }
    if (ctrl && !shift) {
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_D)) return RepeatOp::HalfPageDown;
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_U)) return RepeatOp::HalfPageUp;
        return RepeatOp::None;
    }
    if (!ctrl && !shift) {
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_DOWN) ||
            im->getKeyboardKeyState(brls::BRLS_KBD_KEY_J)) {
            return RepeatOp::NavDown;
        }
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_UP) ||
            im->getKeyboardKeyState(brls::BRLS_KBD_KEY_K)) {
            return RepeatOp::NavUp;
        }
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_LEFT) ||
            im->getKeyboardKeyState(brls::BRLS_KBD_KEY_H)) {
            return RepeatOp::NavLeft;
        }
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_RIGHT) ||
            im->getKeyboardKeyState(brls::BRLS_KBD_KEY_L)) {
            return RepeatOp::NavRight;
        }
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_PAGE_DOWN)) {
            return RepeatOp::FullPageDown;
        }
        if (im->getKeyboardKeyState(brls::BRLS_KBD_KEY_PAGE_UP)) {
            return RepeatOp::FullPageUp;
        }
    }
    return RepeatOp::None;
}

void tickDesktopRepeat() {
    auto& st = state();
    const RepeatOp op = detectHeldRepeatOp();
    if (op == RepeatOp::None) {
        st.repeat_op = RepeatOp::None;
        return;
    }
    const auto now = std::chrono::steady_clock::now();
    if (op != st.repeat_op) {
        st.repeat_op = op;
        st.next_repeat_time = now + std::chrono::milliseconds(180);
        return;
    }
    if (now < st.next_repeat_time) {
        return;
    }
    st.next_repeat_time = now + std::chrono::milliseconds(38);

    switch (op) {
        case RepeatOp::NavUp:
            if (!hasGamepadAction(brls::Application::getCurrentFocus(), brls::BUTTON_NAV_UP)) {
                navigateDirection(brls::FocusDirection::UP, true);
            }
            break;
        case RepeatOp::NavDown:
            if (!hasGamepadAction(brls::Application::getCurrentFocus(), brls::BUTTON_NAV_DOWN)) {
                navigateDirection(brls::FocusDirection::DOWN, true);
            }
            break;
        case RepeatOp::NavLeft:
            if (!hasGamepadAction(brls::Application::getCurrentFocus(), brls::BUTTON_NAV_LEFT)) {
                navigateDirection(brls::FocusDirection::LEFT, true);
            }
            break;
        case RepeatOp::NavRight:
            if (!hasGamepadAction(brls::Application::getCurrentFocus(), brls::BUTTON_NAV_RIGHT)) {
                navigateDirection(brls::FocusDirection::RIGHT, true);
            }
            break;
        case RepeatOp::HalfPageDown:
            scrollCurrentBy(260.0f, true);
            break;
        case RepeatOp::HalfPageUp:
            scrollCurrentBy(-260.0f, true);
            break;
        case RepeatOp::FullPageDown:
            scrollCurrentBy(440.0f, true);
            break;
        case RepeatOp::FullPageUp:
            scrollCurrentBy(-440.0f, true);
            break;
        case RepeatOp::None:
            break;
    }
}

NVGcolor scaleAlpha(NVGcolor c, float factor) {
    c.a = std::clamp(c.a * factor, 0.0f, 1.0f);
    return c;
}

void renderFocusRing(NVGcontext* vg) {
    if (!vg || isEditingText()) {
        return;
    }
    brls::View* root = currentRootView();
    brls::View* cur = brls::Application::getCurrentFocus();
    if (!isUsableFocusTarget(cur, root)) {
        return;
    }

    const brls::Rect frame = cur->getFrame();
    const float x = frame.getMinX();
    const float y = frame.getMinY();
    const float w = frame.getWidth();
    const float h = frame.getHeight();
    if (w < 4.0f || h < 4.0f) {
        return;
    }

    const float viewport_w =
        brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float viewport_h =
        brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    brls::Rect clip{0.0f, 0.0f, viewport_w, viewport_h};
    for (brls::View* p = cur->getParent(); p; p = p->getParent()) {
        if (dynamic_cast<brls::ScrollingFrame*>(p) != nullptr || p->getClipsToBounds()) {
            clip = intersectRect(clip, p->getFrame());
        }
        if (p == root) break;
    }
    if (clip.getWidth() < 2.0f || clip.getHeight() < 2.0f) {
        return;
    }

    const brls::Rect expanded{x - 6.0f, y - 6.0f, w + 12.0f, h + 12.0f};
    const brls::Rect visible_test = intersectRect(expanded, clip);
    if (visible_test.getWidth() < 1.0f || visible_test.getHeight() < 1.0f) {
        return;
    }

    const float mode_alpha =
        (brls::Application::getInputType() == brls::InputType::GAMEPAD) ? 1.0f : 0.86f;
    const NVGcolor ring_col = scaleAlpha(ui::color("desktop/focus_ring"), mode_alpha);
    const NVGcolor halo_col = scaleAlpha(ui::color("desktop/focus_halo"), mode_alpha);
    const NVGcolor glow_col = scaleAlpha(ui::color("desktop/focus_glow"), mode_alpha);
    const NVGcolor fill_col = scaleAlpha(ui::color("desktop/focus_fill"), mode_alpha);

    float r = cur->getCornerRadius();
    if (r <= 0.5f) {
        r = 8.0f;
    }
    r = std::min(r, std::min(w, h) * 0.5f);

    const bool is_button = (dynamic_cast<brls::Button*>(cur) != nullptr);

    nvgSave(vg);
    nvgScissor(vg, clip.getMinX(), clip.getMinY(), clip.getWidth(), clip.getHeight());

    // 1. Subtle interior tint wash on cards, rows, and reader paragraphs
    if (!is_button) {
        nvgBeginPath(vg);
        nvgRoundedRect(vg, x, y, w, h, r);
        nvgFillColor(vg, fill_col);
        nvgFill(vg);
    }

    // 2. Soft outer ambient glow
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x - 3.0f, y - 3.0f, w + 6.0f, h + 6.0f, r + 3.0f);
    nvgStrokeWidth(vg, 3.6f);
    nvgStrokeColor(vg, glow_col);
    nvgStroke(vg);

    // 3. High-contrast separation halo (white on Paper Light, obsidian on Graphite Dark)
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x - 1.5f, y - 1.5f, w + 3.0f, h + 3.0f, r + 1.5f);
    nvgStrokeWidth(vg, 4.4f);
    nvgStrokeColor(vg, halo_col);
    nvgStroke(vg);

    // 4. Crisp semantic focus ring (#155EEF Cobalt / #58A6FF Sky Blue)
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x - 1.5f, y - 1.5f, w + 3.0f, h + 3.0f, r + 1.5f);
    nvgStrokeWidth(vg, 2.4f);
    nvgStrokeColor(vg, ring_col);
    nvgStroke(vg);

    // 5. Left vertical anchor bar on cards, rail entries, and paragraphs for instant eye tracking
    if (!is_button && w >= 120.0f && h >= 32.0f) {
        const float bar_h = std::max(14.0f, h - 14.0f);
        const float bar_y = y + (h - bar_h) * 0.5f;
        nvgBeginPath(vg);
        nvgRoundedRect(vg, x + 2.0f, bar_y, 3.6f, bar_h, 1.8f);
        nvgFillColor(vg, ring_col);
        nvgFill(vg);
    }

    nvgRestore(vg);
}

} // namespace

void DesktopInputController::install(DesktopCallbacks callbacks) {
    state().callbacks = std::move(callbacks);

    brls::Application::setAutomaticDeactivation(false);
    brls::Application::setDeactivatedFPS(60);
    brls::Application::keyboardOverlayRenderer = renderOverlay;

    if (!state().lifecycle_hooks_registered) {
        state().lifecycle_hooks_registered = true;

        // Capture ScrollingFrame offset right BEFORE View::onFocusGained() runs.
        brls::Application::getGlobalFocusChangeEvent()->subscribe([](brls::View* newFocus) {
            auto& st = state();
            st.pre_focus_scroll_sf = nullptr;
            st.adjust_scroll_after_focus = false;
            if (!newFocus) {
                return;
            }
            if (!st.preserving_x && newFocus->getFrame().getWidth() >= 4.0f) {
                st.preferred_x = newFocus->getFrame().getMidX();
            }
            if (auto* sf = findAncestorScroll(newFocus)) {
                st.pre_focus_scroll_sf = sf;
                st.pre_focus_scroll_y = sf->getContentOffsetY();
                st.adjust_scroll_after_focus = true;
            }
        });

        // Fires immediately AFTER View::onFocusGained() inside Application::giveFocus().
        brls::Application::getGlobalHintsUpdateEvent()->subscribe([]() {
            auto& st = state();
            if (!st.adjust_scroll_after_focus || !st.pre_focus_scroll_sf) {
                return;
            }
            brls::ScrollingFrame* sf = st.pre_focus_scroll_sf;
            const float pre_y = st.pre_focus_scroll_y;
            st.adjust_scroll_after_focus = false;
            st.pre_focus_scroll_sf = nullptr;

            if (st.suppress_focus_scroll ||
                brls::Application::getInputType() == brls::InputType::TOUCH) {
                // Mouse click or programmatic focus recovery: never jerk scroll position!
                sf->setContentOffsetY(pre_y, false);
                return;
            }
            applyComfortScroll(sf, brls::Application::getCurrentFocus(), pre_y, st.immediate_scroll);
        });

        // Per-frame view normalization, focus continuity recovery, and 26 Hz desktop repeat.
        brls::Application::getRunLoopEvent()->subscribe([]() {
            if (brls::View* root = currentRootView()) {
                normalizeViewTree(root);
            }
            recoverFocusIfNeeded();
            tickDesktopRepeat();
        });
    }

    brls::Application::controllerShortcutHandler = [](brls::ControllerButton button,
                                                      bool repeating) -> bool {
        auto& st = state();
        if (st.internal_passthrough) {
            return false;
        }
        if (isEditingText()) {
            return false;
        }

        if (st.hint_mode) {
            if (button == brls::BUTTON_B) {
                cancelHints();
                return true;
            }
            if (button == brls::BUTTON_A) {
                return true;
            }
            return false;
        }

        // Never drop the first Enter / A click after moving the mouse.
        if (button == brls::BUTTON_A) {
            ensureGamepadMode();
            recoverFocusIfNeeded();
            return false;
        }

        auto handleNavBtn = [&](brls::FocusDirection dir) -> bool {
            if (hasGamepadAction(brls::Application::getCurrentFocus(), button)) {
                ensureGamepadMode();
                return false; // Let sliders / custom controls handle their own directional action.
            }
            if (repeating) {
                // If keyboard arrow/hjkl is held, tickDesktopRepeat() drives smooth 26 Hz repeat;
                // if a physical gamepad is held, forward repeat through navigateDirection.
                if (detectHeldRepeatOp() != RepeatOp::None) {
                    return true;
                }
                return navigateDirection(dir, true);
            }
            return navigateDirection(dir, false);
        };

        switch (button) {
            case brls::BUTTON_NAV_UP:
                return handleNavBtn(brls::FocusDirection::UP);
            case brls::BUTTON_NAV_DOWN:
                return handleNavBtn(brls::FocusDirection::DOWN);
            case brls::BUTTON_NAV_LEFT:
                return handleNavBtn(brls::FocusDirection::LEFT);
            case brls::BUTTON_NAV_RIGHT:
                return handleNavBtn(brls::FocusDirection::RIGHT);
            default:
                break;
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
                if (!in_modal_activity && st.callbacks.on_toggle_dock) {
                    ensureGamepadMode();
                    st.callbacks.on_toggle_dock();
                }
                return true;
            }

            if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_F11) {
                if (st.callbacks.on_toggle_fullscreen) {
                    st.callbacks.on_toggle_fullscreen();
                }
                return true;
            }

            if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_F9) {
                if (!in_modal_activity && st.callbacks.on_skip_ad && st.callbacks.on_skip_ad()) {
                    return true;
                }
            }

            if (!repeating && ctrl && key == brls::BRLS_KBD_KEY_TAB) {
                if (!in_modal_activity && st.callbacks.on_cycle_workspace) {
                    ensureGamepadMode();
                    st.callbacks.on_cycle_workspace(shift ? -1 : 1);
                    return true;
                }
            }

            if (!repeating && ctrl && !shift && key == brls::BRLS_KBD_KEY_K) {
                if (!in_modal_activity && st.callbacks.on_command_palette) {
                    ensureGamepadMode();
                    st.callbacks.on_command_palette();
                    return true;
                }
            }

            if (!repeating && ctrl && key >= brls::BRLS_KBD_KEY_1 && key <= brls::BRLS_KBD_KEY_9) {
                if (!in_modal_activity && st.callbacks.on_select_workspace) {
                    ensureGamepadMode();
                    st.callbacks.on_select_workspace(key - brls::BRLS_KBD_KEY_1);
                    return true;
                }
            }

            // Vim hint overlay mode (works in both main workbenches and modal dialogs)
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
                        ensureGamepadMode();
                        if (target && target->getVisibility() == brls::Visibility::VISIBLE) {
                            giveFocusClean(target, false, false);
                            st.preferred_x = target->getFrame().getMidX();
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

            // Tab / Shift+Tab cycles major panes in workbenches and controls in modals.
            if (!ctrl && key == brls::BRLS_KBD_KEY_TAB) {
                if (!repeating) {
                    cyclePaneOrModalFocus(shift);
                }
                return true;
            }

            // Letter hints 'f' work everywhere, including Ctrl+K palette & popovers.
            if (!repeating && !ctrl && !shift && key == brls::BRLS_KBD_KEY_F) {
                ensureGamepadMode();
                buildHints();
                st.hint_mode = !st.hints.empty();
                return true;
            }

            if (!in_modal_activity && !repeating && !ctrl) {
                if (key == brls::BRLS_KBD_KEY_SLASH && !shift) {
                    if (st.callbacks.on_search) {
                        ensureGamepadMode();
                        st.callbacks.on_search();
                        return true;
                    }
                }
                if (key == brls::BRLS_KBD_KEY_SLASH && shift) { // '?'
                    ensureGamepadMode();
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

        if (brls::Application::isInputBlocks()) {
            return false;
        }

        // Escape: close modal dialog if open, otherwise cancel active stream/fetch on root.
        if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_ESCAPE) {
            if (in_modal_activity) {
                if (auto* dlg = dynamic_cast<brls::Dialog*>(currentRootView())) {
                    dlg->close();
                } else {
                    brls::Application::popActivity();
                }
                return true;
            }
            if (st.callbacks.on_cancel_active && st.callbacks.on_cancel_active()) {
                return true;
            }
        }

        // Space activates the focused button/card when not consumed by a media view.
        if (!repeating && !ctrl && !shift && key == brls::BRLS_KBD_KEY_SPACE) {
            return triggerFocusedClick();
        }

        // Ctrl+D / Ctrl+U / PageDown / PageUp / Home / End / gg / G scrolling (works in modals too)
        if (ctrl && key == brls::BRLS_KBD_KEY_D) {
            return repeating ? true : scrollCurrentBy(280.0f, false);
        }
        if (ctrl && key == brls::BRLS_KBD_KEY_U) {
            return repeating ? true : scrollCurrentBy(-280.0f, false);
        }
        if (!ctrl && key == brls::BRLS_KBD_KEY_PAGE_DOWN) {
            return repeating ? true : scrollCurrentBy(460.0f, false);
        }
        if (!ctrl && key == brls::BRLS_KBD_KEY_PAGE_UP) {
            return repeating ? true : scrollCurrentBy(-460.0f, false);
        }
        if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_HOME) {
            return scrollToEdge(false);
        }
        if (!repeating && !ctrl && key == brls::BRLS_KBD_KEY_END) {
            return scrollToEdge(true);
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

        // hjkl directional focus movement (initial press here; hold driven at 26 Hz by tickDesktopRepeat)
        if (!ctrl && !shift) {
            if (key == brls::BRLS_KBD_KEY_H) {
                return repeating ? true : navigateDirection(brls::FocusDirection::LEFT, false);
            }
            if (key == brls::BRLS_KBD_KEY_J) {
                return repeating ? true : navigateDirection(brls::FocusDirection::DOWN, false);
            }
            if (key == brls::BRLS_KBD_KEY_K) {
                return repeating ? true : navigateDirection(brls::FocusDirection::UP, false);
            }
            if (key == brls::BRLS_KBD_KEY_L) {
                return repeating ? true : navigateDirection(brls::FocusDirection::RIGHT, false);
            }
        }

        return false;
    };

    if (!state().watched_keys_registered) {
        state().watched_keys_registered = true;
        const brls::BrlsKeyCombination combos[] = {
            {brls::BRLS_KBD_KEY_F8, 0},
            {brls::BRLS_KBD_KEY_F9, 0},
            {brls::BRLS_KBD_KEY_F11, 0},
            {brls::BRLS_KBD_KEY_ESCAPE, 0},
            {brls::BRLS_KBD_KEY_BACKSPACE, 0},
            {brls::BRLS_KBD_KEY_SPACE, 0},
            {brls::BRLS_KBD_KEY_PAGE_UP, 0},
            {brls::BRLS_KBD_KEY_PAGE_DOWN, 0},
            {brls::BRLS_KBD_KEY_HOME, 0},
            {brls::BRLS_KBD_KEY_END, 0},
            {brls::BRLS_KBD_KEY_TAB, 0},
            {brls::BRLS_KBD_KEY_TAB, brls::BRLS_KBD_MODIFIER_SHIFT},
            {brls::BRLS_KBD_KEY_TAB, brls::BRLS_KBD_MODIFIER_CTRL},
            {brls::BRLS_KBD_KEY_TAB,
             static_cast<short>(brls::BRLS_KBD_MODIFIER_CTRL | brls::BRLS_KBD_MODIFIER_SHIFT)},
            {brls::BRLS_KBD_KEY_D, brls::BRLS_KBD_MODIFIER_CTRL},
            {brls::BRLS_KBD_KEY_U, brls::BRLS_KBD_MODIFIER_CTRL},
            {brls::BRLS_KBD_KEY_K, brls::BRLS_KBD_MODIFIER_CTRL},
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

    ensureGamepadMode();
    recoverFocusIfNeeded();
}

void DesktopInputController::renderOverlay(NVGcontext* vg) {
    renderFocusRing(vg);

    const auto& st = state();
    if (!st.hint_mode || st.hints.empty()) {
        return;
    }
    if (st.hint_root != currentRootView() || isEditingText()) {
        cancelHints();
        return;
    }

    const NVGcolor ring = ui::color("desktop/focus_ring");
    const NVGcolor halo = ui::color("desktop/focus_halo");
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
        const float bx = std::max(4.0f, h.frame.origin.x + 3.0f);
        const float by = std::max(4.0f, h.frame.origin.y + 3.0f);

        nvgBeginPath(vg);
        nvgRoundedRect(vg, bx - 1.0f, by - 1.0f, badge_w + 2.0f, badge_h + 2.0f, 6.0f);
        nvgStrokeWidth(vg, 2.5f);
        nvgStrokeColor(vg, halo);
        nvgStroke(vg);

        nvgBeginPath(vg);
        nvgRoundedRect(vg, bx, by, badge_w, badge_h, 5.0f);
        nvgFillColor(vg, bg);
        nvgFill(vg);
        nvgStrokeWidth(vg, 1.8f);
        nvgStrokeColor(vg, ring);
        nvgStroke(vg);

        nvgFillColor(vg, text_col);
        nvgText(vg, bx + badge_w * 0.5f, by + badge_h * 0.5f, h.label.c_str(), nullptr);
    }

    nvgRestore(vg);
}

void DesktopInputController::showShortcutsHelp() {
    auto* box = new brls::Box(brls::Axis::COLUMN);
    box->setPadding(22);
    box->setWidth(560);

    auto* title = ui::makeLabel("OmniDesk 桌面快捷键", 22.0f, "brls/text");
    title->setMarginBottom(12);
    box->addView(title);

    struct Row {
        const char* keys;
        const char* desc;
    };
    const Row rows[] = {
        {"方向键 / h j k l", "跨网格列保持与跨面板焦点导航（长按 26Hz 高速连发）"},
        {"Tab / Shift+Tab", "在左侧栏 / 顶栏 / 主列表 / 阅读详情 / 伴读栏之间循环切换"},
        {"Enter / Space", "激活当前高亮蓝色焦点框所在的卡片、按钮或段落"},
        {"f", "激活全局字母直达角标（主界面与弹窗均支持）"},
        {"Ctrl+D / Ctrl+U / PgUp / PgDn", "半页 / 整页平滑滚动并同步保持视口焦点"},
        {"gg / G / Home / End", "滚动并定位至当前滚动区顶部 / 底部"},
        {"Ctrl + 1..9", "切换工作台（插件市场 / 影视音乐 / 文字期刊 / 书本PDF / 扩展）"},
        {"Ctrl + Tab / Ctrl+Shift+Tab", "顺序或反向切换左侧工作区标签"},
        {"Ctrl + K", "打开跨三域全局检索与命令面板 (SQLite FTS5)"},
        {"F8 / F11 / F9", "切换 AI 伴读栏 / 切换全屏 / 跳过广告片段"},
        {"/ · t · ?", "当前工作台检索 · 切换亮暗主题 · 打开快捷键速查表"},
        {"Esc", "关闭当前弹窗，或中断活跃 AI 流式推理 / 异步拉取"},
    };

    for (const auto& r : rows) {
        auto* row = new brls::Box(brls::Axis::ROW);
        row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        row->setAlignItems(brls::AlignItems::CENTER);
        row->setMarginBottom(8);

        row->addView(ui::makePill(r.keys, "desktop/soft", "desktop/focus_ring"));
        auto* desc = ui::makeLabel(r.desc, 14.5f, "font/grey");
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
