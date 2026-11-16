#include "omnidesk/host/desktop_ui.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <mutex>
#include <unordered_set>
#include <SDL2/SDL.h>
#include <borealis/core/time.hpp>
#include <borealis/views/dialog.hpp>
#include <borealis/views/edit_text_dialog.hpp>
#include <borealis/views/rectangle.hpp>
#include <nanovg.h>
#include <yoga/Yoga.h>
#include <yoga/event/event.h>
#include "omnidesk/host/ai_composer.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/nav_shortcuts.hpp"
#include "omnidesk/mpv_core.hpp"
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

enum class CursorKind {
    Arrow = 0,
    Hand,
    IBeam,
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
    bool sdl_wheel_watch_registered = false;
    bool yoga_event_subscribed = false;
    bool internal_passthrough = false;
    float pending_fallback_wheel_y = 0.0f;

    // Event-driven view normalization & cached ScrollingFrame / rail state
    std::mutex alloc_mutex;
    std::unordered_set<const YGNode*> allocated_ygnodes;
    std::vector<brls::View*> dirty_subtrees;
    bool dirty_root = true;
    bool in_normalize_pass = false;
    size_t last_activity_depth = 0;
    brls::View* last_observed_root = nullptr;
    int sync_scroll_widths_frames = 2;

    bool scroll_cache_dirty = true;
    brls::View* scroll_cache_root = nullptr;
    brls::View* cached_nav_rail = nullptr;
    std::vector<brls::ScrollingFrame*> cached_scroll_frames;

    // 2D column-preserving navigation & focus continuity across UI rebuilds
    float preferred_x = -1.0f;
    bool preserving_x = false;
    brls::View* last_root = nullptr;
    brls::View* last_focused_view = nullptr;
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

    // Desktop pointer hover & system cursor state
    bool hover_dirty = true;
    bool mouse_in_window = false;
    brls::Point mouse_pt{-1000.0f, -1000.0f};
    brls::View* hovered_view = nullptr;
    brls::Rect hovered_clip{0.0f, 0.0f, 0.0f, 0.0f};
    CursorKind active_cursor = CursorKind::Arrow;
    SDL_Cursor* cursor_arrow = nullptr;
    SDL_Cursor* cursor_hand = nullptr;
    SDL_Cursor* cursor_ibeam = nullptr;
};

State& state() {
    static State s;
    return s;
}

void forgetCachedView(brls::View* view) {
    if (!view) return;
    auto& st = state();
    if (st.hint_root == view) {
        st.hint_mode = false;
        st.hint_root = nullptr;
        st.hint_prefix.clear();
        st.hints.clear();
    }
    st.hints.erase(
        std::remove_if(st.hints.begin(), st.hints.end(),
                       [view](const HintTarget& hint) { return hint.view == view; }),
        st.hints.end());
    if (st.last_observed_root == view) st.last_observed_root = nullptr;
    if (st.scroll_cache_root == view) {
        st.scroll_cache_root = nullptr;
        st.scroll_cache_dirty = true;
    }
    if (st.cached_nav_rail == view) st.cached_nav_rail = nullptr;
    if (st.last_root == view) st.last_root = nullptr;
    if (st.last_focused_view == view) st.last_focused_view = nullptr;
    if (st.last_pane_box == view) st.last_pane_box = nullptr;
    if (st.pre_focus_scroll_sf == view) {
        st.pre_focus_scroll_sf = nullptr;
        st.adjust_scroll_after_focus = false;
    }
    if (st.hovered_view == view) {
        st.hovered_view = nullptr;
        st.hover_dirty = true;
    }
    st.dirty_subtrees.erase(
        std::remove(st.dirty_subtrees.begin(), st.dirty_subtrees.end(), view),
        st.dirty_subtrees.end());
    st.cached_scroll_frames.erase(
        std::remove(st.cached_scroll_frames.begin(), st.cached_scroll_frames.end(), view),
        st.cached_scroll_frames.end());
}

brls::View* currentRootView() {
    const auto& stack = brls::Application::getActivitiesStack();
    return stack.empty() ? nullptr : stack.back()->getContentView();
}

bool isModalEditDialog() {
    return dynamic_cast<brls::EditTextDialog*>(currentRootView()) != nullptr;
}

bool isEditingText() {
    return isModalEditDialog() || InlineEditorRegistry::isActivelyEditing();
}

bool isEmbeddedBrowserViewport(brls::View* v) {
    if (!v) return false;
    return v->getView("omnidesk_embedded_browser_capture") == v ||
           v->getView("omnidesk_embedded_browser_viewport") == v;
}

bool isEmbeddedBrowserCaptured() {
    brls::View* cur = brls::Application::getCurrentFocus();
    return cur != nullptr && cur->getView("omnidesk_embedded_browser_capture") == cur;
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

bool isPointInEmbeddedBrowserViewport(const brls::Point& pt) {
    brls::View* root = currentRootView();
    if (!root) return false;
    brls::View* vp = root->getView("omnidesk_embedded_browser_capture");
    if (!vp) {
        vp = root->getView("omnidesk_embedded_browser_viewport");
    }
    return vp && isViewInActiveTree(vp, root) && vp->getFrame().pointInside(pt);
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

void normalizeDialogIfNeeded(brls::Dialog* dlg) {
    if (!dlg) {
        return;
    }
    auto* applet = dynamic_cast<brls::Box*>(dlg->getView("brls/dialog/applet"));
    auto* container = dynamic_cast<brls::Box*>(dlg->getView("brls/dialog/container"));
    if (!applet || !container) {
        return;
    }

    const float viewport_w =
        brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float max_w = std::max(420.0f, viewport_w - 48.0f);

    if (!container->getChildren().empty() && container->getChildren().front()) {
        brls::View* content = container->getChildren().front();
        const YGValue yg_w = YGNodeStyleGetWidth(content->getYGNode());
        float desired_w = 0.0f;
        if (yg_w.unit == YGUnitPoint && yg_w.value > 100.0f) {
            desired_w = yg_w.value;
        }
        const float target_w =
            (desired_w > 0.0f) ? std::min(desired_w, max_w) : std::min(720.0f, max_w);
        if (desired_w > max_w && std::fabs(yg_w.value - target_w) > 0.5f) {
            content->setWidth(target_w);
        }
        const YGValue app_w = YGNodeStyleGetWidth(applet->getYGNode());
        if (app_w.unit != YGUnitPoint || std::fabs(app_w.value - target_w) > 0.5f) {
            applet->setWidth(target_w);
        }
    }

    if (std::fabs(applet->getCornerRadius() - 14.0f) > 0.1f) {
        applet->setCornerRadius(14.0f);
        applet->setBorderThickness(1.0f);
        applet->setClipsToBounds(true);
    }
    applet->setBorderColor(ui::color("desktop/line"));
    applet->setBackgroundColor(ui::color("brls/background"));

    if (auto* btn1 = dlg->getView("brls/dialog/button1")) {
        if (auto* btn_row = btn1->getParent()) {
            const YGValue row_h = YGNodeStyleGetHeight(btn_row->getYGNode());
            if (row_h.unit == YGUnitPoint && std::fabs(row_h.value - 52.0f) > 0.5f) {
                btn_row->setHeight(52.0f);
            }
        }
    }
}

brls::View* scrollContentView(brls::ScrollingFrame* sf) {
    if (!sf) {
        return nullptr;
    }
    const auto& ch = sf->getChildren();
    for (auto it = ch.rbegin(); it != ch.rend(); ++it) {
        if (*it && dynamic_cast<brls::Rectangle*>(*it) == nullptr) {
            return *it;
        }
    }
    return ch.size() >= 2 ? ch.back() : nullptr;
}

// Normalizes a single view node (Dialog, ScrollingFrame, or focusable Box) without recursing.
void normalizeSingleView(brls::View* node) {
    if (!node || node->isPtrLocked()) {
        return;
    }
    if (auto* dlg = dynamic_cast<brls::Dialog*>(node)) {
        normalizeDialogIfNeeded(dlg);
    }
    if (auto* sf = dynamic_cast<brls::ScrollingFrame*>(node)) {
        auto& st = state();
        st.scroll_cache_dirty = true;
        st.sync_scroll_widths_frames = std::max(st.sync_scroll_widths_frames, 2);
        const bool first_visit = sf->isFocusable();
        sf->setScrollingBehavior(brls::ScrollingBehavior::CENTERED);
        sf->setFocusable(false);
        if (first_visit) {
            sf->setClipsToBounds(true);
            sf->setShrink(1.0f);
            sf->setMinHeight(0.0f);
            sf->setMinWidth(0.0f);
            if (auto* parent_box = dynamic_cast<brls::Box*>(sf->getParent())) {
                if (parent_box->getAxis() == brls::Axis::ROW) {
                    const YGValue h = YGNodeStyleGetHeight(sf->getYGNode());
                    if (h.unit == YGUnitAuto || h.unit == YGUnitUndefined) {
                        sf->setHeightPercentage(100.0f);
                    }
                } else {
                    const YGValue w = YGNodeStyleGetWidth(sf->getYGNode());
                    if (w.unit == YGUnitAuto || w.unit == YGUnitUndefined) {
                        sf->setWidthPercentage(100.0f);
                    }
                }
            }
            brls::View* root = currentRootView();
            brls::View* views_host = root ? root->getView("omnidesk/views_host") : nullptr;
            for (brls::View* anc = sf->getParent(); anc != nullptr; anc = anc->getParent()) {
                if (auto* anc_box = dynamic_cast<brls::Box*>(anc)) {
                    if (YGNodeStyleGetFlexGrow(anc_box->getYGNode()) > 0.0f) {
                        anc_box->setShrink(1.0f);
                        anc_box->setMinHeight(0.0f);
                        anc_box->setMinWidth(0.0f);
                        if (auto* gp = dynamic_cast<brls::Box*>(anc_box->getParent())) {
                            if (gp->getAxis() == brls::Axis::ROW) {
                                const YGValue ah = YGNodeStyleGetHeight(anc_box->getYGNode());
                                if (ah.unit == YGUnitAuto || ah.unit == YGUnitUndefined) {
                                    anc_box->setHeightPercentage(100.0f);
                                }
                            }
                        }
                    }
                    if (views_host && anc_box == views_host) {
                        break;
                    }
                }
            }
        }
        if (brls::View* content = scrollContentView(sf)) {
            const float sf_w = sf->getWidth();
            const float cur_w = content->getWidth();
            if (!std::isnan(sf_w) && sf_w > 2.0f &&
                (std::isnan(cur_w) || std::fabs(cur_w - sf_w) > 1.0f)) {
                content->setWidth(sf_w);
                content->invalidate();
            }
        }
    }
    if (auto* box = dynamic_cast<brls::Box*>(node)) {
        if (box->isFocusable()) {
            box->setHideHighlightBackground(true);
            box->setHideHighlightBorder(true);
            attachChildAwareTapIfNeeded(box);
        }
    }
}

// Normalizes ScrollingFrames (prevents NATURAL focus traps/stealing and Yoga flex overflow),
// Dialog frames, and ensures focusable Boxes respond to mouse taps across Host, SDK, and Plugins.
void normalizeViewTree(brls::View* node) {
    if (!node || node->isPtrLocked() || node->getVisibility() != brls::Visibility::VISIBLE) {
        return;
    }
    normalizeSingleView(node);
    if (auto* box = dynamic_cast<brls::Box*>(node)) {
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
    if (!sf || !target) {
        return;
    }
    brls::View* content = scrollContentView(sf);
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

void recoverFocusIfNeeded(bool tree_changed = false) {
    if (isEditingText()) {
        return;
    }
    brls::View* root = currentRootView();
    if (!root) {
        return;
    }

    auto& st = state();
    brls::View* cur = brls::Application::getCurrentFocus();

    // Fast O(1) steady-state check: when focus is unchanged and valid on a clean tree,
    // skip full-tree pane resolution and RTTI traversal.
    if (!tree_changed &&
        cur != nullptr &&
        cur == st.last_focused_view &&
        root == st.last_root &&
        !cur->isPtrLocked() &&
        cur->getVisibility() == brls::Visibility::VISIBLE &&
        !cur->isHidden()) {
        const brls::Rect f = cur->getFrame();
        if (f.getWidth() >= 4.0f && f.getHeight() >= 4.0f) {
            st.last_focus_rect = f;
            st.has_last_focus_rect = true;
            if (st.preferred_x < 0.0f) {
                st.preferred_x = f.getMidX();
            }
            return;
        }
    }

    if (isUsableFocusTarget(cur, root)) {
        const bool focus_switched = (cur != st.last_focused_view || root != st.last_root);
        st.last_root = root;
        st.last_focused_view = cur;
        st.last_focus_rect = cur->getFrame();
        st.has_last_focus_rect = true;
        if (focus_switched || !st.last_pane_box || !isViewInActiveTree(st.last_pane_box, root)) {
            st.last_pane_box = findEnclosingPane(cur, root);
        }
        if (st.preferred_x < 0.0f) {
            st.preferred_x = st.last_focus_rect.getMidX();
        }
        return;
    }

    st.last_focused_view = nullptr;

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
            st.last_focused_view = brls::Application::getCurrentFocus();
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
        st.last_focused_view = brls::Application::getCurrentFocus();
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

    // Borealis ScrollingFrame::getNextFocus returns `this` when at boundary with remaining scroll;
    // calling getDefaultFocus() on it wraps all the way back to item #0 at the top!
    if (dynamic_cast<brls::ScrollingFrame*>(next) != nullptr) {
        next = nullptr;
    } else if (next) {
        next = next->getDefaultFocus();
    }

    // Reject structural navigation candidates that move backwards against `dir`.
    if (next && next != cur) {
        const brls::Rect cf = cur->getFrame();
        const brls::Rect nf = next->getFrame();
        if ((dir == brls::FocusDirection::DOWN && nf.getMidY() < cf.getMidY() - 2.0f) ||
            (dir == brls::FocusDirection::UP && nf.getMidY() > cf.getMidY() + 2.0f) ||
            (dir == brls::FocusDirection::RIGHT && nf.getMidX() < cf.getMidX() - 2.0f) ||
            (dir == brls::FocusDirection::LEFT && nf.getMidX() > cf.getMidX() + 2.0f)) {
            next = nullptr;
        }
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

    // If at the bottom/top focusable item inside a ScrollingFrame that still has un-scrolled overflow,
    // advance scroll smoothly instead of getting stuck before the footer.
    if (vertical) {
        if (auto* sf = findAncestorScroll(cur)) {
            if (brls::View* content = scrollContentView(sf)) {
                const float max_scroll = std::max(0.0f, content->getHeight() - sf->getHeight());
                const float cur_y = sf->getContentOffsetY();
                if (dir == brls::FocusDirection::DOWN && cur_y < max_scroll - 2.0f) {
                    sf->setContentOffsetY(std::min(max_scroll, cur_y + 160.0f), !repeating);
                    return true;
                }
                if (dir == brls::FocusDirection::UP && cur_y > 2.0f) {
                    sf->setContentOffsetY(std::max(0.0f, cur_y - 160.0f), !repeating);
                    return true;
                }
            }
        }
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

brls::ScrollingFrame* findHoveredScrollTarget(brls::Point pt) {
    brls::View* root = currentRootView();
    if (!root) return nullptr;
    brls::ScrollingFrame* found = nullptr;
    auto walk = [&](auto& self, brls::View* node) -> void {
        if (!node || node->getVisibility() != brls::Visibility::VISIBLE) return;
        if (auto* sf = dynamic_cast<brls::ScrollingFrame*>(node)) {
            if (sf->getFrame().pointInside(pt)) {
                found = sf;
            }
        }
        if (auto* box = dynamic_cast<brls::Box*>(node)) {
            for (brls::View* ch : box->getChildren()) {
                self(self, ch);
            }
        }
    };
    walk(walk, root);
    return found;
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
    if (!sf) return;
    brls::View* content = scrollContentView(sf);
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
    if (!sf) {
        return false;
    }
    brls::View* content = scrollContentView(sf);
    const float max_scroll = content ? std::max(0.0f, content->getHeight() - sf->getHeight()) : 99999.0f;
    const float target_y = std::clamp(sf->getContentOffsetY() + delta_y, 0.0f, max_scroll);
    syncFocusIntoScrolledViewport(sf, target_y, 0);
    sf->setContentOffsetY(target_y, !immediate);
    return true;
}

bool scrollToEdge(bool bottom) {
    ensureGamepadMode();
    auto* sf = findBestScrollTarget();
    if (!sf) {
        return false;
    }
    brls::View* content = scrollContentView(sf);
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
    if (isEditingText() || isEmbeddedBrowserCaptured() || state().hint_mode ||
        brls::Application::isInputBlocks()) {
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

bool queryMouseContentPoint(brls::Point& out_pt) {
    SDL_Window* win = SDL_GetMouseFocus();
    if (!win) {
        return false;
    }
    int mx = 0, my = 0;
    SDL_GetMouseState(&mx, &my);
    int ww = 1280, wh = 720;
    SDL_GetWindowSize(win, &ww, &wh);
    const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    out_pt = {
        ww > 0 ? static_cast<float>(mx) * (cw / static_cast<float>(ww)) : static_cast<float>(mx),
        wh > 0 ? static_cast<float>(my) * (ch / static_cast<float>(wh)) : static_cast<float>(my),
    };
    return true;
}

void findDeepestInteractiveHover(brls::View* v,
                                 brls::Rect clip,
                                 const brls::Point& pt,
                                 brls::View*& best_view,
                                 brls::Rect& best_clip) {
    if (!v || v->isPtrLocked() || v->getVisibility() != brls::Visibility::VISIBLE ||
        v->isHidden() || v->getAlpha() < 0.08f) {
        return;
    }
    brls::Rect visible = intersectRect(v->getFrame(), clip);
    if (visible.pointInside(pt) && visible.getWidth() >= 4.0f && visible.getHeight() >= 4.0f) {
        const bool is_editor = dynamic_cast<AiTextEditorView*>(v) != nullptr;
        const bool is_focusable =
            v->isFocusable() && dynamic_cast<brls::ScrollingFrame*>(v) == nullptr;
        if (is_editor || is_focusable) {
            best_view = v;
            best_clip = clip;
        }
    }
    if (auto* box = dynamic_cast<brls::Box*>(v)) {
        brls::Rect child_clip = clip;
        if (dynamic_cast<brls::ScrollingFrame*>(v) != nullptr || v->getClipsToBounds()) {
            child_clip = visible;
            if (!child_clip.pointInside(pt)) {
                return;
            }
        }
        for (brls::View* child : box->getChildren()) {
            findDeepestInteractiveHover(child, child_clip, pt, best_view, best_clip);
        }
    }
}

void applySystemCursor(CursorKind kind) {
    auto& st = state();
    if (kind == st.active_cursor) {
        return;
    }
    if (!st.cursor_arrow) st.cursor_arrow = SDL_CreateSystemCursor(SDL_SYSTEM_CURSOR_ARROW);
    if (!st.cursor_hand)  st.cursor_hand  = SDL_CreateSystemCursor(SDL_SYSTEM_CURSOR_HAND);
    if (!st.cursor_ibeam) st.cursor_ibeam = SDL_CreateSystemCursor(SDL_SYSTEM_CURSOR_IBEAM);

    SDL_Cursor* target = st.cursor_arrow;
    if (kind == CursorKind::Hand && st.cursor_hand) {
        target = st.cursor_hand;
    } else if (kind == CursorKind::IBeam && st.cursor_ibeam) {
        target = st.cursor_ibeam;
    }
    if (target) {
        SDL_SetCursor(target);
        st.active_cursor = kind;
    }
}

void updatePointerHoverState() {
    auto& st = state();
    brls::Point pt{};
    if (!queryMouseContentPoint(pt)) {
        if (st.mouse_in_window || st.hovered_view != nullptr) {
            st.mouse_in_window = false;
            st.hovered_view = nullptr;
            applySystemCursor(CursorKind::Arrow);
        }
        return;
    }

    if (isEmbeddedBrowserCaptured() && isPointInEmbeddedBrowserViewport(pt)) {
        st.mouse_in_window = true;
        st.mouse_pt = pt;
        st.hovered_view = nullptr;
        applySystemCursor(CursorKind::Arrow);
        return;
    }

    brls::View* root = currentRootView();
    const bool pointer_moved =
        !st.mouse_in_window ||
        std::fabs(pt.x - st.mouse_pt.x) > 0.25f ||
        std::fabs(pt.y - st.mouse_pt.y) > 0.25f;
    const bool animations_running = !brls::Ticking::runningTickings.empty();
    const bool hovered_stale =
        st.hovered_view != nullptr && !isViewInActiveTree(st.hovered_view, root);

    st.mouse_in_window = true;
    st.mouse_pt = pt;

    // Skip full-tree RTTI hover scan when pointer is stationary and view tree / scroll state is clean.
    if (!pointer_moved && !st.hover_dirty && !animations_running && !hovered_stale) {
        return;
    }
    st.hover_dirty = false;
    st.hovered_view = nullptr;

    if (root) {
        const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
        const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
        findDeepestInteractiveHover(root, {0.0f, 0.0f, cw, ch}, pt, st.hovered_view, st.hovered_clip);
    }

    CursorKind desired = CursorKind::Arrow;
    if (dynamic_cast<AiTextEditorView*>(st.hovered_view) != nullptr) {
        desired = CursorKind::IBeam;
    } else if (st.hovered_view != nullptr && !isEmbeddedBrowserViewport(st.hovered_view)) {
        desired = CursorKind::Hand;
    }
    applySystemCursor(desired);
}

void renderHoverHighlight(NVGcontext* vg) {
    if (!vg || isModalEditDialog()) {
        return;
    }
    const auto& st = state();
    brls::View* root = currentRootView();
    if (!st.mouse_in_window || !st.hovered_view || !isViewInActiveTree(st.hovered_view, root)) {
        return;
    }
    if (dynamic_cast<AiTextEditorView*>(st.hovered_view) != nullptr ||
        isEmbeddedBrowserViewport(st.hovered_view)) {
        return;
    }
    if (st.hovered_view == brls::Application::getCurrentFocus() && !isEditingText()) {
        return;
    }

    const brls::Rect frame = st.hovered_view->getFrame();
    const float x = frame.getMinX();
    const float y = frame.getMinY();
    const float w = frame.getWidth();
    const float h = frame.getHeight();
    const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    if (w < 6.0f || h < 6.0f || (w > cw * 0.86f && h > ch * 0.70f)) {
        return;
    }
    if (st.hovered_clip.getWidth() < 2.0f || st.hovered_clip.getHeight() < 2.0f) {
        return;
    }

    float r = st.hovered_view->getCornerRadius();
    if (r <= 0.5f) {
        r = 8.0f;
    }
    r = std::min(r, std::min(w, h) * 0.5f);

    nvgSave(vg);
    nvgScissor(vg,
               st.hovered_clip.getMinX(),
               st.hovered_clip.getMinY(),
               st.hovered_clip.getWidth(),
               st.hovered_clip.getHeight());
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x, y, w, h, r);
    nvgFillColor(vg, ui::color("desktop/hover_fill"));
    nvgFill(vg);
    nvgStrokeWidth(vg, 1.0f);
    nvgStrokeColor(vg, ui::color("desktop/hover_border"));
    nvgStroke(vg);
    nvgRestore(vg);
}

void collectScrollFramePointers(brls::View* v,
                                brls::View* nav_rail,
                                std::vector<brls::ScrollingFrame*>& out) {
    if (!v || v->isPtrLocked() || v->getVisibility() != brls::Visibility::VISIBLE || v->isHidden()) {
        return;
    }
    if (auto* sf = dynamic_cast<brls::ScrollingFrame*>(v)) {
        if (sf != nav_rail) {
            out.push_back(sf);
        }
    }
    if (auto* box = dynamic_cast<brls::Box*>(v)) {
        for (brls::View* child : box->getChildren()) {
            collectScrollFramePointers(child, nav_rail, out);
        }
    }
}

void ensureScrollCacheFresh(brls::View* root) {
    auto& st = state();
    if (!root) {
        st.scroll_cache_root = nullptr;
        st.cached_nav_rail = nullptr;
        st.cached_scroll_frames.clear();
        st.scroll_cache_dirty = false;
        return;
    }
    if (!st.scroll_cache_dirty && st.scroll_cache_root == root) {
        return;
    }
    st.scroll_cache_root = root;
    st.cached_nav_rail = root->getView("omnidesk/pane_rail");
    st.cached_scroll_frames.clear();
    collectScrollFramePointers(root, st.cached_nav_rail, st.cached_scroll_frames);
    st.scroll_cache_dirty = false;
}

bool computeVisibleScrollClip(brls::ScrollingFrame* sf,
                              brls::View* root,
                              const brls::Rect& viewport,
                              brls::Rect& out_vis) {
    if (!sf || !root || sf->isPtrLocked()) {
        return false;
    }
    brls::Rect clip = viewport;
    bool reached_root = false;
    for (brls::View* p = sf; p != nullptr; p = p->getParent()) {
        if (p->isPtrLocked() || p->getVisibility() != brls::Visibility::VISIBLE ||
            p->isHidden() || p->getAlpha() < 0.08f) {
            return false;
        }
        if (p == sf || p->getClipsToBounds() || dynamic_cast<brls::ScrollingFrame*>(p) != nullptr) {
            clip = intersectRect(clip, p->getFrame());
        }
        if (p == root) {
            reached_root = true;
            break;
        }
    }
    if (!reached_root || clip.getWidth() < 180.0f || clip.getHeight() < 80.0f) {
        return false;
    }
    out_vis = clip;
    return true;
}

void syncCachedScrollWidths(brls::View* root) {
    ensureScrollCacheFresh(root);
    auto& st = state();
    for (brls::ScrollingFrame* sf : st.cached_scroll_frames) {
        if (!sf || !isViewInActiveTree(sf, root)) continue;
        if (brls::View* content = scrollContentView(sf)) {
            const float sf_w = sf->getWidth();
            const float cur_w = content->getWidth();
            if (!std::isnan(sf_w) && sf_w > 2.0f &&
                (std::isnan(cur_w) || std::fabs(cur_w - sf_w) > 1.0f)) {
                content->setWidth(sf_w);
                content->invalidate();
            }
        }
    }
}

void renderScrollbars(NVGcontext* vg) {
    if (!vg || isEmbeddedBrowserCaptured()) {
        return;
    }
    brls::View* root = currentRootView();
    if (!root) {
        return;
    }
    ensureScrollCacheFresh(root);
    const auto& st = state();
    if (st.cached_scroll_frames.empty()) {
        return;
    }

    const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    const brls::Rect viewport{0.0f, 0.0f, cw, ch};
    const NVGcolor thumb_base = ui::color("desktop/scrollbar_thumb");
    const NVGcolor track_col  = ui::color("desktop/scrollbar_track");

    for (brls::ScrollingFrame* sf : st.cached_scroll_frames) {
        brls::Rect vis{};
        if (!computeVisibleScrollClip(sf, root, viewport, vis)) {
            continue;
        }
        brls::View* content = scrollContentView(sf);
        if (!content) {
            continue;
        }
        const float view_h = sf->getHeight();
        const float content_h = content->getHeight();
        if (content_h <= view_h + 14.0f || view_h <= 32.0f) {
            continue;
        }
        const float max_scroll = content_h - view_h;
        const float track_pad = 6.0f;
        const float track_h = std::max(24.0f, view_h - track_pad * 2.0f);
        const float thumb_h = std::clamp(track_h * (view_h / content_h), 26.0f, track_h * 0.92f);
        const float progress = std::clamp(sf->getContentOffsetY() / std::max(1.0f, max_scroll), 0.0f, 1.0f);
        const float track_y = sf->getFrame().getMinY() + track_pad;
        const float thumb_y = track_y + (track_h - thumb_h) * progress;

        brls::Rect vis_rect = vis;
        const bool pointer_inside = st.mouse_in_window && vis_rect.pointInside(st.mouse_pt);
        const float thumb_w = pointer_inside ? 5.0f : 3.5f;
        const float thumb_x = sf->getFrame().getMaxX() - thumb_w - 3.5f;

        nvgSave(vg);
        nvgScissor(vg, vis.getMinX(), vis.getMinY(), vis.getWidth(), vis.getHeight());
        if (pointer_inside) {
            nvgBeginPath(vg);
            nvgRoundedRect(vg, thumb_x, track_y, thumb_w, track_h, thumb_w * 0.5f);
            nvgFillColor(vg, track_col);
            nvgFill(vg);
        }
        nvgBeginPath(vg);
        nvgRoundedRect(vg, thumb_x, thumb_y, thumb_w, thumb_h, thumb_w * 0.5f);
        nvgFillColor(vg, scaleAlpha(thumb_base, pointer_inside ? 1.25f : 0.62f));
        nvgFill(vg);
        nvgRestore(vg);
    }
}

void renderFocusRing(NVGcontext* vg) {
    if (!vg || isEditingText() || isEmbeddedBrowserCaptured()) {
        return;
    }
    brls::View* root = currentRootView();
    brls::View* cur = brls::Application::getCurrentFocus();
    if (!isUsableFocusTarget(cur, root) || isEmbeddedBrowserViewport(cur)) {
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

    ensureScrollCacheFresh(root);
    brls::View* nav_rail = state().cached_nav_rail;
    const float viewport_w =
        brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float viewport_h =
        brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    brls::Rect clip{0.0f, 0.0f, viewport_w, viewport_h};
    bool in_nav_rail = false;
    for (brls::View* p = cur->getParent(); p; p = p->getParent()) {
        if (nav_rail && p == nav_rail) {
            in_nav_rail = true;
        }
        if (dynamic_cast<brls::ScrollingFrame*>(p) != nullptr || p->getClipsToBounds()) {
            clip = intersectRect(clip, p->getFrame());
        }
        if (p == root) break;
    }
    if (clip.getWidth() < 2.0f || clip.getHeight() < 2.0f) {
        return;
    }

    const brls::Rect expanded{x - 5.0f, y - 5.0f, w + 10.0f, h + 10.0f};
    const brls::Rect visible_test = intersectRect(expanded, clip);
    if (visible_test.getWidth() < 1.0f || visible_test.getHeight() < 1.0f) {
        return;
    }

    const bool kbd_mode = (brls::Application::getInputType() == brls::InputType::GAMEPAD);
    const float mode_alpha = kbd_mode ? 0.94f : 0.38f;
    const NVGcolor ring_col = scaleAlpha(ui::color("desktop/focus_ring"), mode_alpha);
    const NVGcolor halo_col = scaleAlpha(ui::color("desktop/focus_halo"), kbd_mode ? 0.72f : 0.28f);
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

    // 2. Soft outer ambient glow (keyboard navigation mode only)
    if (kbd_mode) {
        nvgBeginPath(vg);
        nvgRoundedRect(vg, x - 2.0f, y - 2.0f, w + 4.0f, h + 4.0f, r + 2.0f);
        nvgStrokeWidth(vg, 2.4f);
        nvgStrokeColor(vg, glow_col);
        nvgStroke(vg);
    }

    // 3. Thin separation halo
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x - 1.0f, y - 1.0f, w + 2.0f, h + 2.0f, r + 1.0f);
    nvgStrokeWidth(vg, 2.5f);
    nvgStrokeColor(vg, halo_col);
    nvgStroke(vg);

    // 4. Crisp semantic focus ring
    nvgBeginPath(vg);
    nvgRoundedRect(vg, x - 1.0f, y - 1.0f, w + 2.0f, h + 2.0f, r + 1.0f);
    nvgStrokeWidth(vg, kbd_mode ? 1.75f : 1.35f);
    nvgStrokeColor(vg, ring_col);
    nvgStroke(vg);

    // 5. Left vertical anchor bar on content cards/paragraphs in keyboard mode (skipped in nav rail)
    if (kbd_mode && !is_button && !in_nav_rail && w >= 140.0f && h >= 36.0f) {
        const float bar_h = std::max(14.0f, h - 16.0f);
        const float bar_y = y + (h - bar_h) * 0.5f;
        nvgBeginPath(vg);
        nvgRoundedRect(vg, x + 2.0f, bar_y, 3.0f, bar_h, 1.5f);
        nvgFillColor(vg, ring_col);
        nvgFill(vg);
    }

    nvgRestore(vg);
}

} // namespace

void DesktopInputController::markDirty(brls::View* subtree) {
    auto& st = state();
    st.hover_dirty = true;
    st.scroll_cache_dirty = true;
    st.sync_scroll_widths_frames = std::max(st.sync_scroll_widths_frames, 2);
    if (!subtree) {
        st.dirty_root = true;
        st.dirty_subtrees.clear();
        return;
    }
    if (st.dirty_root) {
        return;
    }
    if (std::find(st.dirty_subtrees.begin(), st.dirty_subtrees.end(), subtree) ==
        st.dirty_subtrees.end()) {
        if (st.dirty_subtrees.size() >= 16) {
            st.dirty_root = true;
            st.dirty_subtrees.clear();
        } else {
            st.dirty_subtrees.push_back(subtree);
        }
    }
}

void DesktopInputController::install(DesktopCallbacks callbacks) {
    state().callbacks = std::move(callbacks);

    // Adaptive Idle Frame Pacing: step down to 8 FPS via SDL_WaitEventTimeout when idle,
    // waking immediately (0ms) on keyboard/mouse input, animations, AI streaming, or MPV video.
    brls::Application::setAutomaticDeactivation(true);
    brls::Application::setDeactivatedTime(300);
    brls::Application::setDeactivatedFPS(8);
    brls::Application::keyboardOverlayRenderer = renderOverlay;

    if (!state().yoga_event_subscribed) {
        state().yoga_event_subscribed = true;
        facebook::yoga::Event::subscribe([](const YGNode& node,
                                            facebook::yoga::Event::Type eventType,
                                            facebook::yoga::Event::Data) {
            auto& st = state();
            if (eventType == facebook::yoga::Event::NodeAllocation) {
                std::lock_guard<std::mutex> lk(st.alloc_mutex);
                if (!st.in_normalize_pass) {
                    if (st.allocated_ygnodes.size() < 4096) {
                        st.allocated_ygnodes.insert(&node);
                    } else {
                        st.dirty_root = true;
                    }
                    st.hover_dirty = true;
                }
            } else if (eventType == facebook::yoga::Event::NodeDeallocation) {
                std::lock_guard<std::mutex> lk(st.alloc_mutex);
                forgetCachedView(static_cast<brls::View*>(
                    YGNodeGetContext(const_cast<YGNode*>(&node))));
                st.allocated_ygnodes.erase(&node);
                st.scroll_cache_dirty = true;
                st.hover_dirty = true;
            }
        });
    }

    if (!state().lifecycle_hooks_registered) {
        state().lifecycle_hooks_registered = true;

        brls::Application::getWindowSizeChangedEvent()->subscribe([]() {
            DesktopInputController::markDirty(nullptr);
        });

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

        if (!state().sdl_wheel_watch_registered) {
            state().sdl_wheel_watch_registered = true;
            SDL_AddEventWatch([](void*, SDL_Event* ev) -> int {
                if (!ev || ev->type != SDL_MOUSEWHEEL) {
                    return 0;
                }
                float wy = ev->wheel.preciseY;
                if (wy == 0.0f && ev->wheel.y != 0) {
                    wy = static_cast<float>(ev->wheel.y);
                }
                if (wy == 0.0f) {
                    return 0;
                }
                state().hover_dirty = true;
                int mx = 0, my = 0;
                SDL_GetMouseState(&mx, &my);
                SDL_Window* win = SDL_GetWindowFromID(ev->wheel.windowID);
                int ww = 1280, wh = 720;
                if (win) {
                    SDL_GetWindowSize(win, &ww, &wh);
                }
                const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
                const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
                const brls::Point pt{
                    ww > 0 ? static_cast<float>(mx) * (cw / static_cast<float>(ww)) : static_cast<float>(mx),
                    wh > 0 ? static_cast<float>(my) * (ch / static_cast<float>(wh)) : static_cast<float>(my),
                };
                if (auto* ed = InlineEditorRegistry::activeEditor();
                    ed && ed->isVisibleInActiveTree() && ed->getFrame().pointInside(pt)) {
                    return 0;
                }
                if (isPointInEmbeddedBrowserViewport(pt)) {
                    return 0;
                }
                brls::ScrollingFrame* hovered_sf = findHoveredScrollTarget(pt);
                // Borealis drops fractional touchpad deltas where |preciseY * 30| < 1.0, and ignores
                // wheel events when the cursor hovers over header/filter/stage chrome outside a ScrollingFrame.
                if (!hovered_sf || std::fabs(wy * 30.0f) < 1.0f) {
                    state().pending_fallback_wheel_y += (-wy * 46.0f);
                }
                return 0;
            }, nullptr);
        }

        // Event-driven view normalization, O(1) steady-state focus/hover checks, 26 Hz repeat,
        // and adaptive idle frame pacing.
        brls::Application::getRunLoopEvent()->subscribe([]() {
            auto& st = state();
            brls::View* root = currentRootView();
            const size_t stack_depth = brls::Application::getActivitiesStack().size();
            if (root != st.last_observed_root || stack_depth != st.last_activity_depth) {
                st.last_observed_root = root;
                st.last_activity_depth = stack_depth;
                st.dirty_root = true;
                st.scroll_cache_dirty = true;
                st.hover_dirty = true;
                st.sync_scroll_widths_frames = 2;
            }

            std::unordered_set<const YGNode*> newly_allocated;
            {
                std::lock_guard<std::mutex> lk(st.alloc_mutex);
                newly_allocated.swap(st.allocated_ygnodes);
            }
            const bool had_dirty_root = st.dirty_root;
            std::vector<brls::View*> subtrees;
            subtrees.swap(st.dirty_subtrees);
            st.dirty_root = false;

            const bool any_tree_change =
                had_dirty_root || !subtrees.empty() || !newly_allocated.empty();

            if (root && any_tree_change) {
                st.in_normalize_pass = true;
                if (had_dirty_root) {
                    normalizeViewTree(root);
                    st.scroll_cache_dirty = true;
                    st.sync_scroll_widths_frames = std::max(st.sync_scroll_widths_frames, 2);
                } else {
                    for (brls::View* sub : subtrees) {
                        if (sub && isViewInActiveTree(sub, root)) {
                            normalizeViewTree(sub);
                            st.scroll_cache_dirty = true;
                            st.sync_scroll_widths_frames = std::max(st.sync_scroll_widths_frames, 2);
                        }
                    }
                    if (!newly_allocated.empty()) {
                        st.scroll_cache_dirty = true;
                        st.sync_scroll_widths_frames = std::max(st.sync_scroll_widths_frames, 3);
                    }
                    for (const YGNode* yn : newly_allocated) {
                        if (!yn) continue;
                        auto* v = static_cast<brls::View*>(YGNodeGetContext(const_cast<YGNode*>(yn)));
                        if (v && isViewInActiveTree(v, root)) {
                            normalizeSingleView(v);
                        }
                    }
                }
                st.in_normalize_pass = false;
            }

            if (root && st.sync_scroll_widths_frames > 0) {
                --st.sync_scroll_widths_frames;
                syncCachedScrollWidths(root);
            }

            if (std::fabs(st.pending_fallback_wheel_y) >= 1.0f) {
                int mx = 0, my = 0;
                SDL_GetMouseState(&mx, &my);
                SDL_Window* win = SDL_GL_GetCurrentWindow();
                int ww = 1280, wh = 720;
                if (win) {
                    SDL_GetWindowSize(win, &ww, &wh);
                }
                const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
                const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
                const brls::Point pt{
                    ww > 0 ? static_cast<float>(mx) * (cw / static_cast<float>(ww)) : static_cast<float>(mx),
                    wh > 0 ? static_cast<float>(my) * (ch / static_cast<float>(wh)) : static_cast<float>(my),
                };
                brls::ScrollingFrame* sf = findHoveredScrollTarget(pt);
                if (!sf) {
                    sf = findBestScrollTarget();
                }
                if (sf) {
                    if (brls::View* content = scrollContentView(sf)) {
                        const float max_scroll = std::max(0.0f, content->getHeight() - sf->getHeight());
                        const float next_y = std::clamp(
                            sf->getContentOffsetY() + st.pending_fallback_wheel_y, 0.0f, max_scroll);
                        sf->setContentOffsetY(next_y, false);
                    }
                }
                st.pending_fallback_wheel_y = 0.0f;
                st.hover_dirty = true;
                brls::Application::setActiveEvent(true);
            }

            recoverFocusIfNeeded(any_tree_change);
            updatePointerHoverState();
            tickDesktopRepeat();

            // Keep full 60 FPS whenever animations/scroll inertia, key-repeat, AI streaming,
            // or MPV video playback are active; otherwise allow automatic idle step-down.
            const bool keep_full_fps =
                any_tree_change ||
                st.sync_scroll_widths_frames > 0 ||
                !brls::Ticking::runningTickings.empty() ||
                st.repeat_op != RepeatOp::None ||
                omnidesk::MPVCore::instance().isVideoPlaying() ||
                (st.callbacks.is_busy && st.callbacks.is_busy());
            if (keep_full_fps) {
                brls::Application::setActiveEvent(true);
            }
        });
    }

    brls::Application::controllerShortcutHandler = [](brls::ControllerButton button,
                                                      bool repeating) -> bool {
        auto& st = state();
        if (st.internal_passthrough) {
            return false;
        }
        if (isModalEditDialog()) {
            return false;
        }
        if (InlineEditorRegistry::isActivelyEditing()) {
            return true;
        }
        if (isEmbeddedBrowserCaptured()) {
            return true;
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
        if (isModalEditDialog()) {
            cancelHints();
            return fallback;
        }
        const bool inline_editing = InlineEditorRegistry::isActivelyEditing();
        if (inline_editing) {
            cancelHints();
        }
        if (st.hint_mode && st.hint_root != currentRootView()) {
            cancelHints();
        }
        const bool in_modal_activity = brls::Application::getActivitiesStack().size() > 1;

        const int key = combo.code;
        const int mods = combo.mod;
        const bool ctrl = (mods & brls::BRLS_KBD_MODIFIER_CTRL) != 0;
        const bool shift = (mods & brls::BRLS_KBD_MODIFIER_SHIFT) != 0;
        const bool alt = (mods & brls::BRLS_KBD_MODIFIER_ALT) != 0;
        const bool meta = (mods & brls::BRLS_KBD_MODIFIER_META) != 0;
        const bool plain = !ctrl && !shift && !alt && !meta;

        if (!fallback) {
            // Global shortcuts handled before focused view actions:
            if (!repeating && !ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_F8) {
                if (!in_modal_activity && st.callbacks.on_toggle_dock) {
                    ensureGamepadMode();
                    st.callbacks.on_toggle_dock();
                }
                return true;
            }

            if (!repeating && !ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_F11) {
                if (st.callbacks.on_toggle_fullscreen) {
                    st.callbacks.on_toggle_fullscreen();
                }
                return true;
            }

            if (!inline_editing && !repeating && !ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_F9) {
                if (!in_modal_activity && st.callbacks.on_skip_ad && st.callbacks.on_skip_ad()) {
                    return true;
                }
            }

            if (!repeating && ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_TAB) {
                if (!in_modal_activity && st.callbacks.on_cycle_workspace) {
                    if (inline_editing) InlineEditorRegistry::blurActiveEditor();
                    ensureGamepadMode();
                    st.callbacks.on_cycle_workspace(shift ? -1 : 1);
                    return true;
                }
            }

            if (!repeating && ctrl && !shift && !alt && !meta && key == brls::BRLS_KBD_KEY_K) {
                if (!in_modal_activity && st.callbacks.on_command_palette) {
                    if (inline_editing) InlineEditorRegistry::blurActiveEditor();
                    ensureGamepadMode();
                    st.callbacks.on_command_palette();
                    return true;
                }
            }

            if (!repeating && ctrl && !shift && !alt && !meta && key == brls::BRLS_KBD_KEY_COMMA) {
                if (!in_modal_activity && st.callbacks.on_open_settings) {
                    if (inline_editing) InlineEditorRegistry::blurActiveEditor();
                    ensureGamepadMode();
                    st.callbacks.on_open_settings();
                    return true;
                }
            }

            if (!repeating && ctrl && !shift && !alt && !meta &&
                key >= brls::BRLS_KBD_KEY_1 && key <= brls::BRLS_KBD_KEY_9) {
                if (!in_modal_activity && st.callbacks.on_select_workspace) {
                    if (inline_editing) InlineEditorRegistry::blurActiveEditor();
                    ensureGamepadMode();
                    st.callbacks.on_select_workspace(key - brls::BRLS_KBD_KEY_1);
                    return true;
                }
            }

            if (!in_modal_activity && isEmbeddedBrowserCaptured()) {
                cancelHints();
                return true;
            }

            // Semantic Alt+<Letter/Digit> direct jump to any plugin workspace extension
            if (!repeating && alt && !ctrl && !shift && !meta && !in_modal_activity &&
                st.callbacks.on_select_plugin_shortcut) {
                char target_ch = '\0';
                if (key >= brls::BRLS_KBD_KEY_A && key <= brls::BRLS_KBD_KEY_Z) {
                    target_ch = static_cast<char>('A' + (key - brls::BRLS_KBD_KEY_A));
                } else if (key >= brls::BRLS_KBD_KEY_0 && key <= brls::BRLS_KBD_KEY_9) {
                    target_ch = static_cast<char>('0' + (key - brls::BRLS_KBD_KEY_0));
                }
                if (target_ch != '\0') {
                    if (inline_editing) InlineEditorRegistry::blurActiveEditor();
                    ensureGamepadMode();
                    if (st.callbacks.on_select_plugin_shortcut(target_ch)) {
                        return true;
                    }
                }
            }

            if (inline_editing) {
                return true;
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
                if (!repeating && !ctrl && !alt && !meta &&
                    key >= brls::BRLS_KBD_KEY_A && key <= brls::BRLS_KBD_KEY_Z) {
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
            if (!ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_TAB) {
                if (!repeating) {
                    cyclePaneOrModalFocus(shift);
                }
                return true;
            }

            // Letter hints 'f' work everywhere, including Ctrl+K palette & popovers.
            if (!repeating && plain && key == brls::BRLS_KBD_KEY_F) {
                ensureGamepadMode();
                buildHints();
                st.hint_mode = !st.hints.empty();
                return true;
            }

            if (!in_modal_activity && !repeating && !ctrl && !alt && !meta) {
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

        if (inline_editing) {
            return true;
        }

        if (!in_modal_activity && isEmbeddedBrowserCaptured()) {
            return true;
        }

        if (brls::Application::isInputBlocks()) {
            return false;
        }

        // Escape: close modal dialog if open, otherwise cancel active stream/fetch on root.
        if (!repeating && plain && key == brls::BRLS_KBD_KEY_ESCAPE) {
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
        if (!repeating && plain && key == brls::BRLS_KBD_KEY_SPACE) {
            return triggerFocusedClick();
        }

        // Ctrl+D / Ctrl+U / PageDown / PageUp / Home / End / gg / G scrolling (works in modals too)
        if (ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_D) {
            return repeating ? true : scrollCurrentBy(280.0f, false);
        }
        if (ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_U) {
            return repeating ? true : scrollCurrentBy(-280.0f, false);
        }
        if (plain && key == brls::BRLS_KBD_KEY_PAGE_DOWN) {
            return repeating ? true : scrollCurrentBy(460.0f, false);
        }
        if (plain && key == brls::BRLS_KBD_KEY_PAGE_UP) {
            return repeating ? true : scrollCurrentBy(-460.0f, false);
        }
        if (!repeating && plain && key == brls::BRLS_KBD_KEY_HOME) {
            return scrollToEdge(false);
        }
        if (!repeating && plain && key == brls::BRLS_KBD_KEY_END) {
            return scrollToEdge(true);
        }

        if (!repeating && !ctrl && !alt && !meta && key == brls::BRLS_KBD_KEY_G) {
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
        if (plain) {
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
            {brls::BRLS_KBD_KEY_COMMA, brls::BRLS_KBD_MODIFIER_CTRL},
            {brls::BRLS_KBD_KEY_SLASH, 0},
            {brls::BRLS_KBD_KEY_SLASH, brls::BRLS_KBD_MODIFIER_SHIFT},
            {brls::BRLS_KBD_KEY_G, brls::BRLS_KBD_MODIFIER_SHIFT},
        };
        for (const auto& c : combos) {
            brls::Application::addToWatchedKeys(c);
        }
        for (int k = brls::BRLS_KBD_KEY_A; k <= brls::BRLS_KBD_KEY_Z; ++k) {
            const auto sc = static_cast<brls::BrlsKeyboardScancode>(k);
            brls::Application::addToWatchedKeys({sc, 0});
            brls::Application::addToWatchedKeys({sc, brls::BRLS_KBD_MODIFIER_ALT});
        }
        for (int k = brls::BRLS_KBD_KEY_0; k <= brls::BRLS_KBD_KEY_9; ++k) {
            const auto sc = static_cast<brls::BrlsKeyboardScancode>(k);
            brls::Application::addToWatchedKeys({sc, 0});
            brls::Application::addToWatchedKeys({sc, brls::BRLS_KBD_MODIFIER_ALT});
            if (k >= brls::BRLS_KBD_KEY_1) {
                brls::Application::addToWatchedKeys({sc, brls::BRLS_KBD_MODIFIER_CTRL});
            }
        }
    }

    ensureGamepadMode();
    recoverFocusIfNeeded();
}

void DesktopInputController::renderOverlay(NVGcontext* vg) {
    renderScrollbars(vg);
    renderHoverHighlight(vg);
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
    auto* outer = new brls::Box(brls::Axis::COLUMN);
    outer->setPadding(22, 24, 16, 24);
    outer->setWidth(640);

    auto* title = ui::makeLabel("OmniDesk 桌面快捷键速查", 21.0f, "brls/text");
    title->setMarginBottom(4);
    outer->addView(title);
    auto* sub = ui::makeLabel("Ctrl+1..5 切换核心主台 · Alt+字母 一键直达全部扩展工作区", 13.5f, "font/grey");
    sub->setMarginBottom(12);
    outer->addView(sub);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setWidthPercentage(100);
    scroll->setHeight(430);
    auto* box = new brls::Box(brls::Axis::COLUMN);
    box->setWidthPercentage(100);

    struct Row {
        const char* keys;
        const char* desc;
    };
    const Row rows[] = {
        {"Ctrl + 1..5", "切换五大主工作台（1 插件市场 · 2 影视音乐 · 3 文字期刊 · 4 书本 PDF · 5 AI 对话）"},
        {"AI 内联输入框", "Enter 发送 · Shift+Enter 换行 · Ctrl+V 无限字数粘贴 · Esc 退出编辑"},
        {"Alt + 字母", "语义助记直达任意插件扩展（无数量上限，顺序固定，见下方实时列表）"},
        {"Ctrl + ,", "打开宿主与系统设置面板"},
        {"Ctrl + Tab / Shift+Tab", "按侧栏分组顺序正向 / 反向循环切换工作区"},
        {"Ctrl + K", "打开跨三域全局检索与命令面板 (SQLite FTS5)"},
        {"方向键 / h j k l", "跨网格列保持与跨面板焦点导航（长按 26Hz 高速连发）"},
        {"Tab / Shift+Tab", "在左侧栏 / 顶栏 / 主列表 / 阅读详情 / 伴读栏之间循环切换"},
        {"Enter / Space", "激活当前焦点框所在的卡片、按钮、段落或分组折叠头"},
        {"f", "激活全局字母直达角标（主界面与弹窗均支持）"},
        {"Ctrl+D / Ctrl+U / PgUp / PgDn", "半页 / 整页平滑滚动并同步保持视口焦点"},
        {"gg / G / Home / End", "滚动并定位至当前滚动区顶部 / 底部"},
        {"F8 / F11 / F9", "切换 AI 伴读栏 / 切换全屏 / 跳过广告片段"},
        {"/ · t · ? · Esc", "当前工作台检索 · 切换亮暗主题 · 快捷键速查 · 关闭/中断任务"},
    };

    for (const auto& r : rows) {
        auto* row = new brls::Box(brls::Axis::ROW);
        row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        row->setAlignItems(brls::AlignItems::CENTER);
        row->setMarginBottom(7);

        row->addView(ui::makePill(r.keys, "desktop/soft", "desktop/focus_ring"));
        auto* desc = ui::makeLabel(r.desc, 14.0f, "font/grey");
        desc->setGrow(1.0f);
        desc->setMarginLeft(12);
        row->addView(desc);
        box->addView(row);
    }

    if (HostKernel::instance().isInitialized()) {
        const auto assigned = assignPluginNavTabs(HostKernel::instance().domains().workspaceTabs());
        if (!assigned.empty()) {
            box->addView(ui::makeDivider(10.0f));
            auto* ext_hdr = ui::makeLabel("已装载扩展工作区直达键 (Alt + 语义键)", 15.0f, "brls/text");
            ext_hdr->setMarginBottom(8);
            box->addView(ext_hdr);

            for (size_t i = 0; i < assigned.size(); i += 2) {
                auto* pair_row = new brls::Box(brls::Axis::ROW);
                pair_row->setWidthPercentage(100);
                pair_row->setMarginBottom(6);
                for (size_t j = 0; j < 2 && (i + j) < assigned.size(); ++j) {
                    const auto& item = assigned[i + j];
                    auto* cell = new brls::Box(brls::Axis::ROW);
                    cell->setGrow(1.0f);
                    cell->setWidthPercentage(48);
                    cell->setPadding(5, 10, 5, 10);
                    cell->setCornerRadius(8.0f);
                    cell->setBackgroundColor(ui::color("desktop/soft"));
                    cell->setAlignItems(brls::AlignItems::CENTER);
                    cell->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
                    if (j == 0) cell->setMarginRight(8);

                    auto* name_lbl = ui::makeLabel(item.spec.title, 13.5f, "brls/text");
                    name_lbl->setSingleLine(true);
                    name_lbl->setShrink(1.0f);
                    cell->addView(name_lbl);

                    if (!item.shortcut_label.empty()) {
                        auto* kbd = ui::makePill(item.shortcut_label, "desktop/surface", item.domain_token);
                        kbd->setMarginLeft(8);
                        kbd->setShrink(0.0f);
                        cell->addView(kbd);
                    }
                    pair_row->addView(cell);
                }
                box->addView(pair_row);
            }
        }
    }

    scroll->setContentView(box);
    outer->addView(scroll);

    auto* dialog = new brls::Dialog(outer);
    dialog->addButton("关闭", [] {});
    dialog->open();
}

} // namespace omnidesk::host
