#include "omnidesk/host/ai_composer.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <vector>

#include <SDL2/SDL.h>
#include <borealis/views/edit_text_dialog.hpp>
#include <nanovg.h>

#include "omnidesk/host/svg_view.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

constexpr float kPadH = 6.0f;
constexpr float kPadV = 6.0f;

uint64_t monotonicMillis() {
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch())
            .count());
}

std::string nvgColorToHex(NVGcolor col) {
    const int r = std::clamp(static_cast<int>( std::lround(col.r * 255.0f)), 0, 255);
    const int g = std::clamp(static_cast<int>( std::lround(col.g * 255.0f)), 0, 255);
    const int b = std::clamp(static_cast<int>( std::lround(col.b * 255.0f)), 0, 255);
    char buf[16];
    std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", r, g, b);
    return std::string(buf);
}

NVGcolor withAlpha(NVGcolor c, float a) {
    c.a = std::clamp(a, 0.0f, 1.0f);
    return c;
}

std::string svgSparkle(const std::string& stroke_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + stroke_hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<path d=\"M12 3l1.9 5.3L19.2 10l-5.3 1.9L12 17.2l-1.9-5.3L4.8 10l5.3-1.7L12 3z\"/>"
           "<path d=\"M19 16l.9 2.1L22 19l-2.1.9L19 22l-.9-2.1L16 19l2.1-.9L19 16z\"/>"
           "</svg>";
}

std::string svgCompass(const std::string& stroke_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + stroke_hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<circle cx=\"12\" cy=\"12\" r=\"9\"/>"
           "<polygon points=\"16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76\"/>"
           "</svg>";
}

std::string svgMusic(const std::string& stroke_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + stroke_hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<path d=\"M9 18V5l12-2v13\"/>"
           "<circle cx=\"6\" cy=\"18\" r=\"3\"/>"
           "<circle cx=\"18\" cy=\"16\" r=\"3\"/>"
           "</svg>";
}

std::string svgModelLayers(const std::string& stroke_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + stroke_hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<polygon points=\"12 2 2 7 12 12 22 7 12 2\"/>"
           "<polyline points=\"2 12 12 17 22 12\"/>"
           "<polyline points=\"2 17 12 22 22 17\"/>"
           "</svg>";
}

std::string svgArrowUp(const std::string& stroke_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"18\" height=\"18\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + stroke_hex +
           "\" stroke-width=\"2.4\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<line x1=\"12\" y1=\"19\" x2=\"12\" y2=\"5\"/>"
           "<polyline points=\"5 12 12 5 19 12\"/>"
           "</svg>";
}

std::string svgStopSquare(const std::string& fill_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"" + fill_hex + "\">"
           "<rect x=\"6\" y=\"6\" width=\"12\" height=\"12\" rx=\"2.5\"/>"
           "</svg>";
}

std::string svgCloseSmall(const std::string& stroke_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + stroke_hex +
           "\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<line x1=\"18\" y1=\"6\" x2=\"6\" y2=\"18\"/>"
           "<line x1=\"6\" y1=\"6\" x2=\"18\" y2=\"18\"/>"
           "</svg>";
}

std::vector<size_t> utf8ByteOffsets(const std::string& s) {
    std::vector<size_t> out;
    out.reserve(s.size() + 1);
    for (size_t i = 0; i < s.size(); ++i) {
        if ((static_cast<unsigned char>(s[i]) & 0xC0u) != 0x80u) {
            out.push_back(i);
        }
    }
    out.push_back(s.size());
    return out;
}

size_t utf8CodepointCount(const std::string& s) {
    size_t n = 0;
    for (unsigned char ch : s) {
        if ((ch & 0xC0u) != 0x80u) {
            ++n;
        }
    }
    return n;
}

size_t byteOffsetToCodepointIndex(const std::vector<size_t>& offsets, size_t byte_pos) {
    if (offsets.empty()) return 0;
    auto it = std::upper_bound(offsets.begin(), offsets.end(), byte_pos);
    if (it == offsets.begin()) return 0;
    --it;
    return static_cast<size_t>(it - offsets.begin());
}

std::string sanitizeLineEndings(const std::string& raw) {
    std::string out;
    out.reserve(raw.size());
    for (size_t i = 0; i < raw.size(); ++i) {
        if (raw[i] == '\r') {
            out.push_back('\n');
            if (i + 1 < raw.size() && raw[i + 1] == '\n') {
                ++i;
            }
        } else if (raw[i] != '\0') {
            out.push_back(raw[i]);
        }
    }
    return out;
}

std::string trimWhitespace(const std::string& s) {
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

bool rectContains(const brls::Rect& r, brls::Point p) {
    return p.x >= r.getMinX() && p.x <= r.getMaxX() &&
           p.y >= r.getMinY() && p.y <= r.getMaxY();
}

brls::Point windowPixelToBorealis(int wx, int wy, uint32_t window_id) {
    SDL_Window* win = window_id != 0 ? SDL_GetWindowFromID(window_id) : SDL_GL_GetCurrentWindow();
    if (!win) {
        win = SDL_GL_GetCurrentWindow();
    }
    int ww = 1280;
    int wh = 720;
    if (win) {
        SDL_GetWindowSize(win, &ww, &wh);
    }
    const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    return {
        ww > 0 ? static_cast<float>(wx) * (cw / static_cast<float>(ww)) : static_cast<float>(wx),
        wh > 0 ? static_cast<float>(wy) * (ch / static_cast<float>(wh)) : static_cast<float>(wy),
    };
}

enum class QueuedKind {
    MouseDown,
    MouseWheel,
    TextInput,
    TextEditing,
    KeyDown,
};

struct QueuedInlineEvent {
    QueuedKind kind = QueuedKind::KeyDown;
    uint64_t seq = 0;
    uint32_t window_id = 0;
    int mouse_x = 0;
    int mouse_y = 0;
    float wheel_dy = 0.0f;
    std::string text;
    int scancode = 0;
    int keycode = 0;
    uint16_t mod = 0;
    bool repeat = false;
};

struct RegistryState {
    bool installed = false;
    std::mutex mu;
    std::atomic<uint64_t> seq_gen{1};
    uint64_t activated_seq = 0;
    std::vector<QueuedInlineEvent> queue;
    std::vector<AiTextEditorView*> editors;
    AiTextEditorView* active = nullptr;
};

RegistryState& regState() {
    static RegistryState s;
    return s;
}

brls::View* topActivityRoot() {
    const auto& stack = brls::Application::getActivitiesStack();
    return stack.empty() ? nullptr : stack.back()->getContentView();
}

bool modalEditDialogOpen() {
    return dynamic_cast<brls::EditTextDialog*>(topActivityRoot()) != nullptr;
}

} // namespace

// ============================================================================
// InlineEditorRegistry
// ============================================================================

void InlineEditorRegistry::ensureInstalled() {
    auto& st = regState();
    if (st.installed) {
        return;
    }
    st.installed = true;

    SDL_AddEventWatch([](void*, SDL_Event* ev) -> int {
        if (!ev) return 0;
        auto& s = regState();
        QueuedInlineEvent q;
        q.seq = s.seq_gen.fetch_add(1, std::memory_order_relaxed);

        switch (ev->type) {
            case SDL_MOUSEBUTTONDOWN:
                if (ev->button.button != SDL_BUTTON_LEFT) return 0;
                q.kind = QueuedKind::MouseDown;
                q.window_id = ev->button.windowID;
                q.mouse_x = ev->button.x;
                q.mouse_y = ev->button.y;
                break;
            case SDL_MOUSEWHEEL: {
                float wy = ev->wheel.preciseY;
                if (wy == 0.0f && ev->wheel.y != 0) {
                    wy = static_cast<float>(ev->wheel.y);
                }
                if (std::fabs(wy) < 0.01f) return 0;
                int mx = 0, my = 0;
                SDL_GetMouseState(&mx, &my);
                q.kind = QueuedKind::MouseWheel;
                q.window_id = ev->wheel.windowID;
                q.mouse_x = mx;
                q.mouse_y = my;
                q.wheel_dy = -wy * 38.0f;
                break;
            }
            case SDL_TEXTINPUT:
                q.kind = QueuedKind::TextInput;
                q.text = ev->text.text;
                break;
            case SDL_TEXTEDITING:
                q.kind = QueuedKind::TextEditing;
                q.text = ev->edit.text;
                break;
            case SDL_KEYDOWN:
                q.kind = QueuedKind::KeyDown;
                q.scancode = static_cast<int>(ev->key.keysym.scancode);
                q.keycode = static_cast<int>(ev->key.keysym.sym);
                q.mod = ev->key.keysym.mod;
                q.repeat = (ev->key.repeat != 0);
                break;
            default:
                return 0;
        }

        std::lock_guard<std::mutex> lk(s.mu);
        if (s.queue.size() < 512) {
            s.queue.push_back(std::move(q));
        }
        return 0;
    }, nullptr);

    brls::Application::getRunLoopEvent()->subscribe([]() {
        auto& s = regState();
        if (s.active && (!s.active->isVisibleInActiveTree() || modalEditDialogOpen())) {
            AiTextEditorView* prev = s.active;
            s.active = nullptr;
            SDL_StopTextInput();
            prev->handleExternalBlur();
        }

        std::vector<QueuedInlineEvent> batch;
        {
            std::lock_guard<std::mutex> lk(s.mu);
            if (s.queue.empty()) return;
            batch.swap(s.queue);
        }

        if (modalEditDialogOpen()) {
            return;
        }

        for (const auto& ev : batch) {
            switch (ev.kind) {
                case QueuedKind::MouseDown: {
                    const brls::Point pt = windowPixelToBorealis(ev.mouse_x, ev.mouse_y, ev.window_id);
                    AiTextEditorView* hit_editor = nullptr;
                    for (AiTextEditorView* ed : s.editors) {
                        if (ed && ed->isVisibleInActiveTree() && rectContains(ed->getFrame(), pt)) {
                            hit_editor = ed;
                            break;
                        }
                    }
                    if (hit_editor) {
                        activateEditor(hit_editor);
                        hit_editor->handleMouseDown(pt);
                    } else if (s.active && ev.seq > s.activated_seq) {
                        deactivateEditor(s.active);
                    }
                    break;
                }
                case QueuedKind::MouseWheel: {
                    const brls::Point pt = windowPixelToBorealis(ev.mouse_x, ev.mouse_y, ev.window_id);
                    for (AiTextEditorView* ed : s.editors) {
                        if (ed && ed->isVisibleInActiveTree() && rectContains(ed->getFrame(), pt)) {
                            ed->handleMouseWheel(pt, ev.wheel_dy);
                            break;
                        }
                    }
                    break;
                }
                case QueuedKind::TextInput:
                    if (s.active && s.active->isVisibleInActiveTree()) {
                        s.active->handleTextInput(ev.text);
                    }
                    break;
                case QueuedKind::TextEditing:
                    if (s.active && s.active->isVisibleInActiveTree()) {
                        s.active->handleTextEditing(ev.text);
                    }
                    break;
                case QueuedKind::KeyDown:
                    if (s.active && s.active->isVisibleInActiveTree()) {
                        s.active->handleKeyDown(ev.scancode, ev.keycode, ev.mod, ev.repeat);
                    }
                    break;
            }
        }
    });
}

void InlineEditorRegistry::registerEditor(AiTextEditorView* editor) {
    if (!editor) return;
    ensureInstalled();
    auto& st = regState();
    if (std::find(st.editors.begin(), st.editors.end(), editor) == st.editors.end()) {
        st.editors.push_back(editor);
    }
}

void InlineEditorRegistry::unregisterEditor(AiTextEditorView* editor) {
    if (!editor) return;
    auto& st = regState();
    if (st.active == editor) {
        st.active = nullptr;
        if (!modalEditDialogOpen()) {
            SDL_StopTextInput();
        }
    }
    st.editors.erase(std::remove(st.editors.begin(), st.editors.end(), editor), st.editors.end());
}

void InlineEditorRegistry::activateEditor(AiTextEditorView* editor) {
    if (!editor) return;
    ensureInstalled();
    auto& st = regState();
    st.activated_seq = st.seq_gen.fetch_add(1, std::memory_order_relaxed);
    if (st.active && st.active != editor) {
        AiTextEditorView* old = st.active;
        st.active = nullptr;
        old->handleExternalBlur();
    }
    st.active = editor;
    SDL_StartTextInput();
}

void InlineEditorRegistry::deactivateEditor(AiTextEditorView* editor) {
    auto& st = regState();
    if (!editor || st.active != editor) return;
    st.active = nullptr;
    if (!modalEditDialogOpen()) {
        SDL_StopTextInput();
    }
    editor->handleExternalBlur();
}

void InlineEditorRegistry::blurActiveEditor() {
    auto& st = regState();
    if (!st.active) return;
    AiTextEditorView* cur = st.active;
    st.active = nullptr;
    if (!modalEditDialogOpen()) {
        SDL_StopTextInput();
    }
    cur->handleExternalBlur();
}

bool InlineEditorRegistry::isActivelyEditing() {
    auto& st = regState();
    if (!st.active) return false;
    if (!st.active->isVisibleInActiveTree() || modalEditDialogOpen()) {
        return false;
    }
    return true;
}

AiTextEditorView* InlineEditorRegistry::activeEditor() {
    return regState().active;
}

// ============================================================================
// AiTextEditorView
// ============================================================================

AiTextEditorView::AiTextEditorView(float min_height, float max_height, float font_size)
    : min_height_(min_height),
      max_height_(max_height),
      applied_height_(min_height),
      font_size_(font_size) {
    setWidthPercentage(100);
    setHeight(min_height_);
    setFocusable(true);
    setHideHighlightBackground(true);
    setHideHighlightBorder(true);
    setHighlightCornerRadius(10.0f);
    blink_origin_usec_ = brls::getCPUTimeUsec();

    InlineEditorRegistry::registerEditor(this);

    // Pressing Enter (BUTTON_A) while focused via keyboard navigation enters active typing mode
    registerClickAction([this](brls::View*) {
        setEditing(true);
        return true;
    });

    addGestureRecognizer(new brls::TapGestureRecognizer(
        [this](brls::TapGestureStatus status, brls::Sound*) {
            if (status.state == brls::GestureState::END) {
                brls::Application::giveFocus(this);
                setEditing(true);
                handleMouseDown(status.position);
            }
        }));
}

AiTextEditorView::~AiTextEditorView() {
    InlineEditorRegistry::unregisterEditor(this);
}

void AiTextEditorView::setPlaceholder(const std::string& placeholder) {
    placeholder_ = placeholder;
    invalidate();
}

void AiTextEditorView::setText(const std::string& text, bool move_cursor_to_end) {
    text_ = sanitizeLineEndings(text);
    composing_.clear();
    is_composing_ = false;
    select_all_ = false;
    const size_t cp_len = utf8CodepointCount(text_);
    cursor_cp_ = move_cursor_to_end ? cp_len : std::min(cursor_cp_, cp_len);
    preferred_caret_x_ = -1.0f;
    auto_scroll_to_caret_ = true;
    resetBlink();
    notifyChanged();
    invalidate();
}

void AiTextEditorView::clear() {
    setText("", true);
    scroll_y_ = 0.0f;
}

bool AiTextEditorView::empty() const {
    return trimWhitespace(text_).empty();
}

bool AiTextEditorView::isVisibleInActiveTree() const {
    brls::View* root = topActivityRoot();
    auto* self = const_cast<AiTextEditorView*>(this);
    if (!root || self->isPtrLocked()) return false;
    brls::View* cur = self;
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

void AiTextEditorView::setEditing(bool active) {
    if (editing_ == active) {
        if (active) {
            InlineEditorRegistry::activateEditor(this);
            resetBlink();
        }
        return;
    }
    editing_ = active;
    resetBlink();
    if (active) {
        if (brls::Application::getCurrentFocus() != this) {
            brls::Application::giveFocus(this);
        }
        InlineEditorRegistry::activateEditor(this);
    } else {
        composing_.clear();
        is_composing_ = false;
        select_all_ = false;
        InlineEditorRegistry::deactivateEditor(this);
    }
    if (on_edit_state_) {
        on_edit_state_(editing_);
    }
    invalidate();
}

void AiTextEditorView::handleExternalBlur() {
    if (!editing_) return;
    editing_ = false;
    composing_.clear();
    is_composing_ = false;
    select_all_ = false;
    if (on_edit_state_) {
        on_edit_state_(false);
    }
    invalidate();
}

void AiTextEditorView::onFocusGained() {
    brls::View::onFocusGained();
    resetBlink();
}

void AiTextEditorView::resetBlink() {
    blink_origin_usec_ = brls::getCPUTimeUsec();
}

void AiTextEditorView::notifyChanged() {
    if (on_change_) {
        on_change_(text_);
    }
}

void AiTextEditorView::insertUtf8AtCursor(const std::string& ins_raw) {
    const std::string ins = sanitizeLineEndings(ins_raw);
    if (ins.empty() && !select_all_) return;
    if (select_all_) {
        text_.clear();
        cursor_cp_ = 0;
        select_all_ = false;
    }
    const auto offsets = utf8ByteOffsets(text_);
    cursor_cp_ = std::min(cursor_cp_, offsets.size() - 1);
    text_.insert(offsets[cursor_cp_], ins);
    cursor_cp_ += utf8CodepointCount(ins);
    preferred_caret_x_ = -1.0f;
    auto_scroll_to_caret_ = true;
    resetBlink();
    notifyChanged();
    invalidate();
}

void AiTextEditorView::handleMouseDown(brls::Point pt) {
    const brls::Rect f = getFrame();
    select_all_ = false;
    placeCursorAtPoint({pt.x - f.getMinX(), pt.y - f.getMinY()});
    resetBlink();
    invalidate();
}

bool AiTextEditorView::handleMouseWheel(brls::Point, float dy) {
    const float max_scroll = std::max(0.0f, content_height_ - getHeight());
    if (max_scroll <= 1.0f) {
        return false;
    }
    scroll_y_ = std::clamp(scroll_y_ + dy, 0.0f, max_scroll);
    return true;
}

void AiTextEditorView::handleTextInput(const std::string& utf8_text) {
    composing_.clear();
    is_composing_ = false;
    last_ime_activity_ms_ = monotonicMillis();
    insertUtf8AtCursor(utf8_text);
}

void AiTextEditorView::handleTextEditing(const std::string& composition) {
    composing_ = composition;
    is_composing_ = !composing_.empty();
    last_ime_activity_ms_ = monotonicMillis();
    auto_scroll_to_caret_ = true;
    resetBlink();
    invalidate();
}

void AiTextEditorView::placeCursorAtPoint(brls::Point local_pt) {
    if (text_.empty() || visual_lines_.empty() || !last_vg_) {
        cursor_cp_ = utf8CodepointCount(text_);
        preferred_caret_x_ = -1.0f;
        return;
    }

    const float line_px = font_size_ * line_height_;
    const float rel_y = std::max(0.0f, local_pt.y - kPadV + scroll_y_);
    const float rel_x = std::max(0.0f, local_pt.x - kPadH);
    const size_t line_idx = std::min(
        visual_lines_.size() - 1,
        static_cast<size_t>(std::floor(rel_y / std::max(1.0f, line_px))));

    const auto& vl = visual_lines_[line_idx];
    const auto offsets = utf8ByteOffsets(text_);
    const size_t cp_start = byteOffsetToCodepointIndex(offsets, std::min(vl.byte_start, text_.size()));
    const size_t cp_end = byteOffsetToCodepointIndex(offsets, std::min(vl.byte_end, text_.size()));

    nvgSave(last_vg_);
    nvgFontFaceId(last_vg_, brls::Application::getDefaultFont());
    nvgFontSize(last_vg_, font_size_);

    size_t best_cp = cp_start;
    float best_dist = std::fabs(rel_x);
    for (size_t cp = cp_start; cp <= cp_end; ++cp) {
        const size_t bpos = std::min(offsets[cp], text_.size());
        const size_t bstart = std::min(vl.byte_start, bpos);
        const float w = (bpos > bstart)
                            ? nvgTextBounds(last_vg_, 0.0f, 0.0f,
                                            text_.data() + bstart,
                                            text_.data() + bpos, nullptr)
                            : 0.0f;
        const float dist = std::fabs(w - rel_x);
        if (dist <= best_dist) {
            best_dist = dist;
            best_cp = cp;
        }
    }
    nvgRestore(last_vg_);

    cursor_cp_ = std::min(best_cp, offsets.size() - 1);
    preferred_caret_x_ = rel_x;
}

void AiTextEditorView::moveCursorVertical(int line_delta) {
    if (visual_lines_.empty() || !last_vg_) {
        cursor_cp_ = (line_delta < 0) ? 0 : utf8CodepointCount(text_);
        return;
    }
    const auto offsets = utf8ByteOffsets(text_);
    cursor_cp_ = std::min(cursor_cp_, offsets.size() - 1);
    const size_t cur_byte = offsets[cursor_cp_];

    size_t cur_line = 0;
    for (size_t i = 0; i < visual_lines_.size(); ++i) {
        if (cur_byte >= visual_lines_[i].byte_start && cur_byte <= visual_lines_[i].byte_end) {
            cur_line = i;
            if (cur_byte < visual_lines_[i].byte_end ||
                i + 1 == visual_lines_.size() ||
                visual_lines_[i + 1].byte_start > cur_byte) {
                break;
            }
        }
    }

    nvgSave(last_vg_);
    nvgFontFaceId(last_vg_, brls::Application::getDefaultFont());
    nvgFontSize(last_vg_, font_size_);

    if (preferred_caret_x_ < 0.0f) {
        const size_t bstart = std::min(visual_lines_[cur_line].byte_start, cur_byte);
        preferred_caret_x_ = (cur_byte > bstart)
                                 ? nvgTextBounds(last_vg_, 0.0f, 0.0f,
                                                 text_.data() + bstart,
                                                 text_.data() + cur_byte, nullptr)
                                 : 0.0f;
    }

    const int next_line = static_cast<int>(cur_line) + line_delta;
    if (next_line < 0) {
        nvgRestore(last_vg_);
        cursor_cp_ = 0;
        auto_scroll_to_caret_ = true;
        return;
    }
    if (static_cast<size_t>(next_line) >= visual_lines_.size()) {
        nvgRestore(last_vg_);
        cursor_cp_ = offsets.size() - 1;
        auto_scroll_to_caret_ = true;
        return;
    }

    const auto& vl = visual_lines_[static_cast<size_t>(next_line)];
    const size_t cp_start = byteOffsetToCodepointIndex(offsets, std::min(vl.byte_start, text_.size()));
    const size_t cp_end = byteOffsetToCodepointIndex(offsets, std::min(vl.byte_end, text_.size()));

    size_t best_cp = cp_start;
    float best_dist = std::fabs(preferred_caret_x_);
    for (size_t cp = cp_start; cp <= cp_end; ++cp) {
        const size_t bpos = std::min(offsets[cp], text_.size());
        const size_t bstart = std::min(vl.byte_start, bpos);
        const float w = (bpos > bstart)
                            ? nvgTextBounds(last_vg_, 0.0f, 0.0f,
                                            text_.data() + bstart,
                                            text_.data() + bpos, nullptr)
                            : 0.0f;
        const float dist = std::fabs(w - preferred_caret_x_);
        if (dist <= best_dist) {
            best_dist = dist;
            best_cp = cp;
        }
    }
    nvgRestore(last_vg_);

    cursor_cp_ = std::min(best_cp, offsets.size() - 1);
    auto_scroll_to_caret_ = true;
}

bool AiTextEditorView::handleKeyDown(int /*scancode*/, int keycode, uint16_t mod, bool repeat) {
    if (is_composing_) {
        return true;
    }

    const bool ctrl = (mod & (KMOD_CTRL | KMOD_GUI)) != 0;
    const bool shift = (mod & KMOD_SHIFT) != 0;
    const bool alt = (mod & KMOD_ALT) != 0;

    if (alt) {
        return false;
    }

    const auto offsets = utf8ByteOffsets(text_);
    const size_t total_cp = offsets.size() - 1;
    cursor_cp_ = std::min(cursor_cp_, total_cp);

    // Clipboard & selection shortcuts
    if (ctrl && !shift) {
        if (keycode == SDLK_a) {
            if (!text_.empty()) {
                select_all_ = true;
                cursor_cp_ = total_cp;
                resetBlink();
                invalidate();
            }
            return true;
        }
        if (keycode == SDLK_c) {
            if (!text_.empty()) {
                SDL_SetClipboardText(text_.c_str());
                brls::Application::notify("已复制输入框内容");
            }
            return true;
        }
        if (keycode == SDLK_x) {
            if (!text_.empty()) {
                SDL_SetClipboardText(text_.c_str());
                clear();
            }
            return true;
        }
        if (keycode == SDLK_v) {
            char* clip = SDL_GetClipboardText();
            if (clip) {
                std::string pasted(clip);
                SDL_free(clip);
                if (!pasted.empty()) {
                    insertUtf8AtCursor(pasted);
                }
            }
            return true;
        }
        if (keycode == SDLK_HOME) {
            select_all_ = false;
            cursor_cp_ = 0;
            preferred_caret_x_ = -1.0f;
            auto_scroll_to_caret_ = true;
            resetBlink();
            invalidate();
            return true;
        }
        if (keycode == SDLK_END) {
            select_all_ = false;
            cursor_cp_ = total_cp;
            preferred_caret_x_ = -1.0f;
            auto_scroll_to_caret_ = true;
            resetBlink();
            invalidate();
            return true;
        }
        if (keycode == SDLK_BACKSPACE) {
            if (select_all_) {
                clear();
                return true;
            }
            if (cursor_cp_ > 0) {
                size_t target = cursor_cp_;
                while (target > 0) {
                    const char ch = text_[offsets[target - 1]];
                    if (ch != ' ' && ch != '\t') break;
                    --target;
                }
                if (target > 0 && text_[offsets[target - 1]] == '\n') {
                    --target;
                } else {
                    while (target > 0) {
                        const char ch = text_[offsets[target - 1]];
                        if (ch == ' ' || ch == '\t' || ch == '\n') break;
                        --target;
                    }
                }
                text_.erase(offsets[target], offsets[cursor_cp_] - offsets[target]);
                cursor_cp_ = target;
                preferred_caret_x_ = -1.0f;
                auto_scroll_to_caret_ = true;
                resetBlink();
                notifyChanged();
                invalidate();
            }
            return true;
        }
    }

    if (keycode == SDLK_RETURN || keycode == SDLK_KP_ENTER) {
        // Guard against Linux IME firing SDL_KEYDOWN(RETURN) right alongside IME candidate commit
        if (monotonicMillis() - last_ime_activity_ms_ < 42) {
            return true;
        }
        if (shift && !ctrl) {
            insertUtf8AtCursor("\n");
            return true;
        }
        if (!repeat && on_submit_) {
            const std::string payload = trimWhitespace(text_);
            if (!payload.empty()) {
                on_submit_(payload);
            }
        }
        return true;
    }

    if (keycode == SDLK_ESCAPE) {
        if (select_all_) {
            select_all_ = false;
            invalidate();
            return true;
        }
        setEditing(false);
        return true;
    }

    if (keycode == SDLK_TAB && !ctrl && !shift) {
        insertUtf8AtCursor("    ");
        return true;
    }

    if (keycode == SDLK_BACKSPACE) {
        if (select_all_) {
            clear();
            return true;
        }
        if (cursor_cp_ > 0) {
            const size_t start_b = offsets[cursor_cp_ - 1];
            const size_t end_b = offsets[cursor_cp_];
            text_.erase(start_b, end_b - start_b);
            --cursor_cp_;
            preferred_caret_x_ = -1.0f;
            auto_scroll_to_caret_ = true;
            resetBlink();
            notifyChanged();
            invalidate();
        }
        return true;
    }

    if (keycode == SDLK_DELETE) {
        if (select_all_) {
            clear();
            return true;
        }
        if (cursor_cp_ < total_cp) {
            const size_t start_b = offsets[cursor_cp_];
            const size_t end_b = offsets[cursor_cp_ + 1];
            text_.erase(start_b, end_b - start_b);
            preferred_caret_x_ = -1.0f;
            auto_scroll_to_caret_ = true;
            resetBlink();
            notifyChanged();
            invalidate();
        }
        return true;
    }

    if (keycode == SDLK_LEFT) {
        if (select_all_) {
            select_all_ = false;
            cursor_cp_ = 0;
        } else if (cursor_cp_ > 0) {
            --cursor_cp_;
        }
        preferred_caret_x_ = -1.0f;
        auto_scroll_to_caret_ = true;
        resetBlink();
        invalidate();
        return true;
    }

    if (keycode == SDLK_RIGHT) {
        if (select_all_) {
            select_all_ = false;
            cursor_cp_ = total_cp;
        } else if (cursor_cp_ < total_cp) {
            ++cursor_cp_;
        }
        preferred_caret_x_ = -1.0f;
        auto_scroll_to_caret_ = true;
        resetBlink();
        invalidate();
        return true;
    }

    if (keycode == SDLK_UP) {
        select_all_ = false;
        moveCursorVertical(-1);
        resetBlink();
        invalidate();
        return true;
    }

    if (keycode == SDLK_DOWN) {
        select_all_ = false;
        moveCursorVertical(1);
        resetBlink();
        invalidate();
        return true;
    }

    if (keycode == SDLK_PAGEUP) {
        select_all_ = false;
        moveCursorVertical(-6);
        resetBlink();
        invalidate();
        return true;
    }

    if (keycode == SDLK_PAGEDOWN) {
        select_all_ = false;
        moveCursorVertical(6);
        resetBlink();
        invalidate();
        return true;
    }

    if (keycode == SDLK_HOME) {
        select_all_ = false;
        size_t target = cursor_cp_;
        while (target > 0 && text_[offsets[target - 1]] != '\n') {
            --target;
        }
        cursor_cp_ = target;
        preferred_caret_x_ = -1.0f;
        auto_scroll_to_caret_ = true;
        resetBlink();
        invalidate();
        return true;
    }

    if (keycode == SDLK_END) {
        select_all_ = false;
        size_t target = cursor_cp_;
        while (target < total_cp && text_[offsets[target]] != '\n') {
            ++target;
        }
        cursor_cp_ = target;
        preferred_caret_x_ = -1.0f;
        auto_scroll_to_caret_ = true;
        resetBlink();
        invalidate();
        return true;
    }

    return true;
}

void AiTextEditorView::rebuildVisualLines(NVGcontext* vg,
                                          float inner_width,
                                          const std::string& display_text) {
    visual_lines_.clear();
    const float line_px = font_size_ * line_height_;
    const float break_w = std::max(40.0f, inner_width);

    if (display_text.empty()) {
        visual_lines_.push_back({0, 0, 0.0f});
        content_height_ = line_px + kPadV * 2.0f;
        return;
    }

    size_t para_start = 0;
    float cur_y = 0.0f;
    NVGtextRow rows[64];

    while (para_start <= display_text.size()) {
        const size_t nl = display_text.find('\n', para_start);
        const size_t para_end = (nl == std::string::npos) ? display_text.size() : nl;

        if (para_end == para_start) {
            visual_lines_.push_back({para_start, para_start, cur_y});
            cur_y += line_px;
        } else {
            const char* ptr = display_text.data() + para_start;
            const char* end = display_text.data() + para_end;
            bool emitted = false;
            while (ptr < end) {
                const int nrows = nvgTextBreakLines(vg, ptr, end, break_w, rows, 64);
                if (nrows <= 0) {
                    break;
                }
                for (int i = 0; i < nrows; ++i) {
                    const size_t bs = static_cast<size_t>(rows[i].start - display_text.data());
                    const size_t be = static_cast<size_t>(rows[i].end - display_text.data());
                    visual_lines_.push_back({bs, be, cur_y});
                    cur_y += line_px;
                    emitted = true;
                }
                ptr = rows[nrows - 1].next;
            }
            if (!emitted) {
                visual_lines_.push_back({para_start, para_end, cur_y});
                cur_y += line_px;
            }
        }

        if (nl == std::string::npos) {
            break;
        }
        para_start = nl + 1;
    }

    if (visual_lines_.empty()) {
        visual_lines_.push_back({0, 0, 0.0f});
        cur_y = line_px;
    }
    content_height_ = cur_y + kPadV * 2.0f;
}

void AiTextEditorView::draw(NVGcontext* vg,
                            float x,
                            float y,
                            float width,
                            float height,
                            brls::Style /*style*/,
                            brls::FrameContext* /*ctx*/) {
    if (!vg || width < 4.0f || height < 4.0f) {
        return;
    }
    last_vg_ = vg;

    const float inner_w = std::max(32.0f, width - kPadH * 2.0f - 6.0f);
    last_inner_width_ = inner_w;
    const float line_px = font_size_ * line_height_;

    nvgSave(vg);
    nvgIntersectScissor(vg, x, y, width, height);
    nvgFontFaceId(vg, brls::Application::getDefaultFont());
    nvgFontSize(vg, font_size_);
    nvgTextLineHeight(vg, line_height_);
    nvgTextAlign(vg, NVG_ALIGN_LEFT | NVG_ALIGN_TOP);

    const auto offsets = utf8ByteOffsets(text_);
    cursor_cp_ = std::min(cursor_cp_, offsets.size() - 1);
    const size_t compose_byte_start = offsets[cursor_cp_];

    std::string display_text = text_;
    if (!composing_.empty()) {
        display_text.insert(compose_byte_start, composing_);
    }
    const size_t compose_byte_end = compose_byte_start + composing_.size();
    const size_t caret_byte = compose_byte_end;

    rebuildVisualLines(vg, inner_w, display_text);

    // Auto-grow height smoothly up to max_height_
    const float desired_h = std::clamp(content_height_, min_height_, max_height_);
    if (std::fabs(desired_h - applied_height_) > 0.75f) {
        applied_height_ = desired_h;
        setHeight(desired_h);
        invalidate();
    }

    // Locate caret visual row & X offset
    size_t caret_line = 0;
    for (size_t i = 0; i < visual_lines_.size(); ++i) {
        if (caret_byte >= visual_lines_[i].byte_start && caret_byte <= visual_lines_[i].byte_end) {
            caret_line = i;
            if (caret_byte < visual_lines_[i].byte_end ||
                i + 1 == visual_lines_.size() ||
                visual_lines_[i + 1].byte_start > caret_byte) {
                break;
            }
        }
    }
    const auto& cl = visual_lines_[caret_line];
    const float caret_x = (caret_byte > cl.byte_start)
                              ? nvgTextBounds(vg, 0.0f, 0.0f,
                                              display_text.data() + cl.byte_start,
                                              display_text.data() + caret_byte, nullptr)
                              : 0.0f;
    const float caret_y = cl.y;

    const float max_scroll = std::max(0.0f, content_height_ - height);
    if (auto_scroll_to_caret_) {
        const float view_inner_h = std::max(line_px, height - kPadV * 2.0f);
        if (caret_y < scroll_y_) {
            scroll_y_ = caret_y;
        } else if (caret_y + line_px > scroll_y_ + view_inner_h) {
            scroll_y_ = caret_y + line_px - view_inner_h;
        }
        auto_scroll_to_caret_ = false;
    }
    scroll_y_ = std::clamp(scroll_y_, 0.0f, max_scroll);

    // Placeholder when completely empty
    if (display_text.empty()) {
        nvgFillColor(vg, a(ui::color("font/grey")));
        nvgTextBox(vg, x + kPadH, y + kPadV, inner_w, placeholder_.c_str(), nullptr);
    } else {
        const NVGcolor text_col = a(ui::color("brls/text"));
        const NVGcolor sel_bg = withAlpha(ui::color("desktop/focus_ring"), 0.24f);
        const NVGcolor comp_line = a(ui::color("desktop/focus_ring"));

        for (const auto& vl : visual_lines_) {
            const float draw_y = y + kPadV + vl.y - scroll_y_;
            if (draw_y + line_px < y - 2.0f) continue;
            if (draw_y > y + height + 2.0f) break;

            if (select_all_ && vl.byte_end > vl.byte_start) {
                const float line_w = nvgTextBounds(
                    vg, 0.0f, 0.0f,
                    display_text.data() + vl.byte_start,
                    display_text.data() + vl.byte_end, nullptr);
                nvgBeginPath(vg);
                nvgRoundedRect(vg, x + kPadH - 2.0f, draw_y + 1.0f, line_w + 4.0f, line_px - 2.0f, 4.0f);
                nvgFillColor(vg, sel_bg);
                nvgFill(vg);
            }

            if (vl.byte_end > vl.byte_start) {
                nvgFillColor(vg, text_col);
                nvgText(vg, x + kPadH, draw_y,
                        display_text.data() + vl.byte_start,
                        display_text.data() + vl.byte_end);
            }

            // IME preedit underline
            if (!composing_.empty() &&
                compose_byte_end > vl.byte_start &&
                compose_byte_start < vl.byte_end) {
                const size_t us = std::max(vl.byte_start, compose_byte_start);
                const size_t ue = std::min(vl.byte_end, compose_byte_end);
                const float ux0 = (us > vl.byte_start)
                                      ? nvgTextBounds(vg, 0.0f, 0.0f,
                                                      display_text.data() + vl.byte_start,
                                                      display_text.data() + us, nullptr)
                                      : 0.0f;
                const float ux1 = nvgTextBounds(vg, 0.0f, 0.0f,
                                                display_text.data() + vl.byte_start,
                                                display_text.data() + ue, nullptr);
                nvgBeginPath(vg);
                nvgMoveTo(vg, x + kPadH + ux0, draw_y + font_size_ + 3.0f);
                nvgLineTo(vg, x + kPadH + ux1, draw_y + font_size_ + 3.0f);
                nvgStrokeWidth(vg, 1.8f);
                nvgStrokeColor(vg, comp_line);
                nvgStroke(vg);
            }
        }
    }

    // Blinking caret + live SDL IME candidate rect
    if (editing_) {
        const float screen_caret_x = x + kPadH + caret_x;
        const float screen_caret_y = y + kPadV + caret_y - scroll_y_;

        if (auto* plat = brls::Application::getPlatform()) {
            if (auto* vc = plat->getVideoContext()) {
                const float scale =
                    brls::Application::windowScale / static_cast<float>(vc->getScaleFactor());
                SDL_Rect ime_rect{
                    static_cast<int>(screen_caret_x * scale),
                    static_cast<int>((screen_caret_y + line_px) * scale),
                    120,
                    24,
                };
                SDL_SetTextInputRect(&ime_rect);
            }
        }

        const uint64_t elapsed_ms = (brls::getCPUTimeUsec() - blink_origin_usec_) / 1000ULL;
        if ((elapsed_ms % 1060ULL) < 580ULL) {
            nvgBeginPath(vg);
            nvgRoundedRect(vg, screen_caret_x, screen_caret_y + 1.5f, 2.2f, font_size_ + 3.0f, 1.1f);
            nvgFillColor(vg, a(ui::color("desktop/focus_ring")));
            nvgFill(vg);
        }
        brls::Application::setActiveEvent(true);
    }

    // Subtle right scrollbar thumb when long multi-line text exceeds max_height_
    if (max_scroll > 2.0f) {
        const float track_h = height - 8.0f;
        const float thumb_h = std::max(18.0f, track_h * (height / content_height_));
        const float thumb_y = y + 4.0f + (track_h - thumb_h) * (scroll_y_ / max_scroll);
        nvgBeginPath(vg);
        nvgRoundedRect(vg, x + width - 4.5f, thumb_y, 3.0f, thumb_h, 1.5f);
        nvgFillColor(vg, withAlpha(ui::color("font/grey"), 0.45f));
        nvgFill(vg);
    }

    nvgRestore(vg);
}

// ============================================================================
// DeepSeekComposer
// ============================================================================

DeepSeekComposer::DeepSeekComposer(DeepSeekComposerOptions opts)
    : brls::Box(brls::Axis::COLUMN),
      opts_(opts) {
    setWidthPercentage(100);
    setShrink(0.0f);
    setCornerRadius(opts_.compact ? 14.0f : 18.0f);
    setPadding(opts_.compact ? 10.0f : 14.0f,
               opts_.compact ? 12.0f : 16.0f,
               opts_.compact ? 10.0f : 12.0f,
               opts_.compact ? 12.0f : 16.0f);
    setBackgroundColor(ui::color("desktop/surface"));
    setBorderThickness(1.4f);
    setBorderColor(ui::color("desktop/line"));

    // Optional top context attachment chip row
    context_row_ = new brls::Box(brls::Axis::ROW);
    context_row_->setAlignItems(brls::AlignItems::CENTER);
    context_row_->setMarginBottom(6.0f);
    context_row_->setVisibility(brls::Visibility::GONE);

    context_chip_ = new brls::Box(brls::Axis::ROW);
    context_chip_->setAlignItems(brls::AlignItems::CENTER);
    context_chip_->setPadding(3.0f, 10.0f, 3.0f, 8.0f);
    context_chip_->setCornerRadius(8.0f);
    context_chip_->setBackgroundColor(ui::color("desktop/soft"));
    context_chip_->setBorderThickness(1.0f);
    context_chip_->setBorderColor(ui::color("desktop/line"));
    context_chip_->setFocusable(true);
    context_chip_->setHighlightCornerRadius(8.0f);

    context_label_ = ui::makeLabel("", 12.5f, "font/grey", 1.2f);
    context_label_->setSingleLine(true);
    context_chip_->addView(context_label_);

    context_clear_icon_ = new SvgIconView();
    context_clear_icon_->setDimensions(13, 13);
    context_clear_icon_->setMarginLeft(6.0f);
    context_clear_icon_->setVisibility(brls::Visibility::GONE);
    context_chip_->addView(context_clear_icon_);

    context_chip_->registerClickAction([this](brls::View*) {
        if (on_clear_context_) {
            on_clear_context_();
        }
        return true;
    });
    context_row_->addView(context_chip_);
    addView(context_row_);

    // Multi-line unlimited inline text editor
    editor_ = new AiTextEditorView(
        opts_.compact ? 44.0f : 56.0f,
        opts_.compact ? 136.0f : 220.0f,
        opts_.compact ? 14.5f : 15.5f);
    editor_->setOnSubmit([this](const std::string&) { triggerSubmit(); });
    editor_->setOnTextChanged([this](const std::string&) { updateVisualState(); });
    editor_->setOnEditStateChanged([this](bool) { updateVisualState(); });
    addView(editor_);

    // Bottom DeepSeek control bar: [深度思考] [灵感策展] [歌单] [模型] ... [Hint] [↑ / ■]
    auto* toolbar = new brls::Box(brls::Axis::ROW);
    toolbar->setWidthPercentage(100);
    toolbar->setMarginTop(opts_.compact ? 6.0f : 8.0f);
    toolbar->setAlignItems(brls::AlignItems::CENTER);
    toolbar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

    auto* left_pills = new brls::Box(brls::Axis::ROW);
    left_pills->setAlignItems(brls::AlignItems::CENTER);
    left_pills->setShrink(1.0f);

    think_pill_ = createTogglePill("深度思考", [this]() {
        deep_thinking_ = !deep_thinking_;
        updateVisualState();
        if (on_state_changed_) on_state_changed_();
        focusInput(true);
    });
    think_pill_.box->setMarginRight(6.0f);
    left_pills->addView(think_pill_.box);

    curate_pill_ = createTogglePill("灵感策展", [this]() {
        curation_mode_ = !curation_mode_;
        updateVisualState();
        if (on_state_changed_) on_state_changed_();
        focusInput(true);
    });
    curate_pill_.box->setMarginRight(6.0f);
    left_pills->addView(curate_pill_.box);

    playlist_pill_ = createTogglePill(opts_.compact ? "歌单" : "同步歌单", [this]() {
        sync_playlist_ = !sync_playlist_;
        updateVisualState();
        if (on_state_changed_) on_state_changed_();
        focusInput(true);
    });
    playlist_pill_.box->setMarginRight(6.0f);
    left_pills->addView(playlist_pill_.box);

    if (opts_.show_model_pill && !opts_.compact) {
        model_pill_ = createTogglePill(model_label_, [this]() {
            if (on_model_click_) {
                on_model_click_();
            }
        });
        left_pills->addView(model_pill_.box);
    }

    toolbar->addView(left_pills);

    auto* right_actions = new brls::Box(brls::Axis::ROW);
    right_actions->setAlignItems(brls::AlignItems::CENTER);
    right_actions->setShrink(0.0f);

    if (!opts_.compact) {
        hint_label_ = ui::makeLabel("Enter 发送 · Shift+Enter 换行", 12.0f, "font/grey", 1.2f);
        hint_label_->setSingleLine(true);
        hint_label_->setMarginRight(10.0f);
        right_actions->addView(hint_label_);
    }

    send_btn_ = new brls::Box(brls::Axis::ROW);
    send_btn_->setDimensions(34.0f, 34.0f);
    send_btn_->setCornerRadius(17.0f);
    send_btn_->setHighlightCornerRadius(17.0f);
    send_btn_->setHideHighlightBackground(true);
    send_btn_->setHideHighlightBorder(true);
    send_btn_->setFocusable(true);
    send_btn_->setAlignItems(brls::AlignItems::CENTER);
    send_btn_->setJustifyContent(brls::JustifyContent::CENTER);
    send_btn_->setShrink(0.0f);

    send_icon_ = new SvgIconView();
    send_icon_->setDimensions(17, 17);
    send_btn_->addView(send_icon_);

    send_btn_->registerClickAction([this](brls::View*) {
        if (streaming_) {
            if (on_cancel_stream_) {
                on_cancel_stream_();
            }
        } else {
            triggerSubmit();
        }
        return true;
    });
    right_actions->addView(send_btn_);

    toolbar->addView(right_actions);
    addView(toolbar);

    updateVisualState();
}

DeepSeekComposer::~DeepSeekComposer() = default;

DeepSeekComposer::PillRefs DeepSeekComposer::createTogglePill(const std::string& text,
                                                              std::function<void()> on_click) {
    PillRefs refs;
    refs.box = new brls::Box(brls::Axis::ROW);
    refs.box->setHeight(opts_.compact ? 28.0f : 30.0f);
    refs.box->setPadding(0.0f, opts_.compact ? 8.0f : 11.0f, 0.0f, opts_.compact ? 8.0f : 11.0f);
    refs.box->setCornerRadius(15.0f);
    refs.box->setHighlightCornerRadius(15.0f);
    refs.box->setHideHighlightBackground(true);
    refs.box->setHideHighlightBorder(true);
    refs.box->setFocusable(true);
    refs.box->setShrink(0.0f);
    refs.box->setAlignItems(brls::AlignItems::CENTER);
    refs.box->setBorderThickness(1.0f);

    refs.icon = new SvgIconView();
    refs.icon->setDimensions(14, 14);
    refs.icon->setShrink(0.0f);
    refs.icon->setMarginRight(5.0f);
    refs.box->addView(refs.icon);

    refs.label = ui::makeLabel(text, opts_.compact ? 12.5f : 13.0f, "brls/text", 1.2f);
    refs.label->setSingleLine(true);
    refs.box->addView(refs.label);

    refs.box->registerClickAction([fn = std::move(on_click)](brls::View*) {
        if (fn) fn();
        return true;
    });
    return refs;
}

void DeepSeekComposer::triggerSubmit() {
    if (!editor_) return;
    const std::string prompt = trimWhitespace(editor_->text());
    if (prompt.empty()) {
        focusInput(true);
        return;
    }
    DeepSeekComposerSubmission sub;
    sub.prompt = prompt;
    sub.curation_mode = curation_mode_;
    sub.deep_thinking = deep_thinking_;
    sub.sync_playlist = sync_playlist_;

    editor_->clear();
    updateVisualState();

    if (on_submit_) {
        on_submit_(sub);
    }
}

void DeepSeekComposer::setStreaming(bool streaming) {
    if (streaming_ == streaming) return;
    streaming_ = streaming;
    updateVisualState();
}

void DeepSeekComposer::setDeepThinking(bool enabled) {
    deep_thinking_ = enabled;
    updateVisualState();
}

void DeepSeekComposer::setCurationMode(bool enabled) {
    curation_mode_ = enabled;
    updateVisualState();
}

void DeepSeekComposer::setSyncPlaylist(bool enabled) {
    sync_playlist_ = enabled;
    updateVisualState();
}

void DeepSeekComposer::setModelLabel(const std::string& label) {
    model_label_ = label.empty() ? "Auto Route" : label;
    if (model_pill_.label) {
        std::string clipped = model_label_;
        if (clipped.size() > 26) {
            clipped = clipped.substr(0, 24) + "…";
        }
        model_pill_.label->setText(clipped);
    }
    updateVisualState();
}

void DeepSeekComposer::setContextBadge(const std::string& badge_text,
                                       std::function<void()> on_clear) {
    context_text_ = badge_text;
    on_clear_context_ = std::move(on_clear);
    if (!context_row_ || !context_label_) return;

    if (context_text_.empty()) {
        context_row_->setVisibility(brls::Visibility::GONE);
    } else {
        context_row_->setVisibility(brls::Visibility::VISIBLE);
        context_label_->setText(context_text_);
        if (context_clear_icon_) {
            context_clear_icon_->setVisibility(
                on_clear_context_ ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        }
    }
    updateVisualState();
}

void DeepSeekComposer::setDraftText(const std::string& text, bool focus_and_edit) {
    if (!editor_) return;
    editor_->setText(text, true);
    if (focus_and_edit) {
        focusInput(true);
    }
    updateVisualState();
}

std::string DeepSeekComposer::draftText() const {
    return editor_ ? editor_->text() : std::string{};
}

void DeepSeekComposer::focusInput(bool start_editing) {
    if (!editor_) return;
    brls::Application::giveFocus(editor_);
    if (start_editing) {
        editor_->setEditing(true);
    }
}

void DeepSeekComposer::refreshThemeStyling() {
    updateVisualState();
}

void DeepSeekComposer::updateVisualState() {
    const bool editing = editor_ && editor_->isEditing();
    const bool has_text = editor_ && !editor_->empty();

    setBackgroundColor(ui::color("desktop/surface"));
    setBorderThickness(editing ? 1.8f : 1.2f);
    if (editing) {
        setBorderColor(ui::color(curation_mode_ ? "brls/accent" : "desktop/focus_ring"));
    } else {
        setBorderColor(ui::color("desktop/line"));
    }

    if (editor_) {
        if (curation_mode_) {
            editor_->setPlaceholder(
                opts_.compact
                    ? "输入心情、主题或场景（如：雨夜写 C++ 来 5 首器乐，推过的也可）..."
                    : "描述心情、主题或场景（如：雨夜写 C++ 来 5 首器乐，以前推过的也可）— 直接生成并反写工作台与歌单...");
        } else {
            editor_->setPlaceholder(
                opts_.compact
                    ? "输入消息，不限字数（Shift+Enter 换行，Ctrl+V 粘贴）..."
                    : "给 OmniDesk AI 发送消息，支持直接粘贴长文 / 代码 / 论文段落（无字数限制）...");
        }
    }

    if (context_chip_) {
        context_chip_->setBackgroundColor(ui::color("desktop/soft"));
        context_chip_->setBorderColor(ui::color("desktop/line"));
        if (context_label_) {
            context_label_->setTextColor(ui::color("font/grey"));
        }
        if (context_clear_icon_) {
            context_clear_icon_->setSvgString(svgCloseSmall(nvgColorToHex(ui::color("font/grey"))));
        }
    }

    // 1. Deep Thinking pill (DeepSeek cool info-blue highlight)
    if (think_pill_.box) {
        const NVGcolor fg = ui::color(deep_thinking_ ? "desktop/focus_ring" : "font/grey");
        think_pill_.box->setBackgroundColor(ui::color(deep_thinking_ ? "status/info_bg" : "desktop/soft"));
        think_pill_.box->setBorderColor(ui::color(deep_thinking_ ? "desktop/focus_ring" : "desktop/line"));
        think_pill_.label->setTextColor(fg);
        think_pill_.icon->setSvgString(svgSparkle(nvgColorToHex(fg)));
    }

    // 2. Curation & Write-Back pill
    if (curate_pill_.box) {
        const NVGcolor fg = ui::color(curation_mode_ ? "brls/accent" : "font/grey");
        curate_pill_.box->setBackgroundColor(ui::color(curation_mode_ ? "desktop/active" : "desktop/soft"));
        curate_pill_.box->setBorderColor(ui::color(curation_mode_ ? "brls/accent" : "desktop/line"));
        curate_pill_.label->setTextColor(fg);
        curate_pill_.icon->setSvgString(svgCompass(nvgColorToHex(fg)));
    }

    // 3. Playlist sync pill (shown when curation mode is active, or dimmed toggle)
    if (playlist_pill_.box) {
        playlist_pill_.box->setVisibility(
            curation_mode_ ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        const NVGcolor fg = ui::color(sync_playlist_ ? "status/ok_fg" : "font/grey");
        playlist_pill_.box->setBackgroundColor(ui::color(sync_playlist_ ? "status/ok_bg" : "desktop/soft"));
        playlist_pill_.box->setBorderColor(ui::color(sync_playlist_ ? "status/ok_fg" : "desktop/line"));
        playlist_pill_.label->setText(
            opts_.compact
                ? (sync_playlist_ ? "歌单:开" : "歌单:关")
                : (sync_playlist_ ? "同步歌单: 开" : "同步歌单: 关"));
        playlist_pill_.label->setTextColor(fg);
        playlist_pill_.icon->setSvgString(svgMusic(nvgColorToHex(fg)));
    }

    // 4. Model / Router pill
    if (model_pill_.box) {
        const NVGcolor fg = ui::color("font/grey");
        model_pill_.box->setBackgroundColor(ui::color("desktop/soft"));
        model_pill_.box->setBorderColor(ui::color("desktop/line"));
        model_pill_.label->setTextColor(fg);
        model_pill_.icon->setSvgString(svgModelLayers(nvgColorToHex(fg)));
    }

    if (hint_label_) {
        hint_label_->setText(
            streaming_ ? "生成中 · Esc 停止" : "Enter 发送 · Shift+Enter 换行");
        hint_label_->setTextColor(ui::color("font/grey"));
    }

    // 5. Circular Send / Stop button
    if (send_btn_ && send_icon_) {
        if (streaming_) {
            send_btn_->setBackgroundColor(ui::color("status/err_bg"));
            send_btn_->setBorderThickness(1.2f);
            send_btn_->setBorderColor(ui::color("status/err_fg"));
            send_icon_->setSvgString(svgStopSquare(nvgColorToHex(ui::color("status/err_fg"))));
        } else if (has_text) {
            const NVGcolor active_fill = ui::color(curation_mode_ ? "brls/accent" : "desktop/focus_ring");
            send_btn_->setBackgroundColor(active_fill);
            send_btn_->setBorderThickness(0.0f);
            send_icon_->setSvgString(svgArrowUp("#FFFFFF"));
        } else {
            send_btn_->setBackgroundColor(ui::color("desktop/soft"));
            send_btn_->setBorderThickness(1.0f);
            send_btn_->setBorderColor(ui::color("desktop/line"));
            send_icon_->setSvgString(svgArrowUp(nvgColorToHex(ui::color("font/grey"))));
        }
    }

    invalidate();
}

} // namespace omnidesk::host
