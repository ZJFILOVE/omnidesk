#pragma once

#include <atomic>
#include <borealis.hpp>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "omnidesk/types.hpp"

namespace omnidesk::host {

class SvgIconView;
class AiTextEditorView;

/**
 * Global coordinator for inline (non-popup) multi-line text editors so DesktopInputController
 * yields keyboard & IME events cleanly while actively typing, and resumes desktop navigation on blur.
 */
class InlineEditorRegistry {
public:
    static void ensureInstalled();
    static void registerEditor(AiTextEditorView* editor);
    static void unregisterEditor(AiTextEditorView* editor);
    static void activateEditor(AiTextEditorView* editor);
    static void deactivateEditor(AiTextEditorView* editor);
    static void blurActiveEditor();
    static bool isActivelyEditing();
    static AiTextEditorView* activeEditor();
};

/**
 * Native NanoVG + SDL2 inline multi-line UTF-8 text editor with:
 * - Zero popup dialog (renders directly inside the DeepSeek composer card)
 * - Zero character limit (supports arbitrary-length code/article pastes & multi-line prompts)
 * - Full CJK IME preedit composition + live SDL_SetTextInputRect candidate positioning
 * - Shift+Enter newline, Enter / Ctrl+Enter submit, Ctrl+A/C/X/V, arrow/line navigation, mouse click caret
 */
class AiTextEditorView : public brls::View {
public:
    AiTextEditorView(float min_height = 54.0f, float max_height = 210.0f, float font_size = 15.5f);
    ~AiTextEditorView() override;

    void setPlaceholder(const std::string& placeholder);
    const std::string& placeholder() const { return placeholder_; }

    void setText(const std::string& text, bool move_cursor_to_end = true);
    const std::string& text() const { return text_; }
    void clear();
    bool empty() const;

    void setEditing(bool active);
    bool isEditing() const { return editing_; }
    bool isVisibleInActiveTree() const;

    void setOnSubmit(std::function<void(const std::string&)> cb) { on_submit_ = std::move(cb); }
    void setOnTextChanged(std::function<void(const std::string&)> cb) { on_change_ = std::move(cb); }
    void setOnEditStateChanged(std::function<void(bool)> cb) { on_edit_state_ = std::move(cb); }

    // Callbacks invoked on main UI thread by InlineEditorRegistry
    void handleMouseDown(brls::Point pt);
    bool handleMouseWheel(brls::Point pt, float dy);
    void handleTextInput(const std::string& utf8_text);
    void handleTextEditing(const std::string& composition);
    bool handleKeyDown(int scancode, int keycode, uint16_t mod, bool repeat);
    void handleExternalBlur();

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style style,
              brls::FrameContext* ctx) override;
    void onFocusGained() override;

private:
    struct VisualLine {
        size_t byte_start = 0;
        size_t byte_end = 0;
        float y = 0.0f;
    };

    void insertUtf8AtCursor(const std::string& ins);
    void notifyChanged();
    void rebuildVisualLines(NVGcontext* vg, float inner_width, const std::string& display_text);
    void moveCursorVertical(int line_delta);
    void placeCursorAtPoint(brls::Point local_pt);
    void resetBlink();

    std::string text_;
    std::string composing_;
    std::string placeholder_ = "给 OmniDesk AI 发送消息...";
    size_t cursor_cp_ = 0;  ///< UTF-8 codepoint index in text_
    bool editing_ = false;
    bool is_composing_ = false;
    bool select_all_ = false;
    bool auto_scroll_to_caret_ = false;
    uint64_t last_ime_activity_ms_ = 0;
    uint64_t blink_origin_usec_ = 0;

    float min_height_ = 54.0f;
    float max_height_ = 210.0f;
    float applied_height_ = 0.0f;
    float font_size_ = 15.5f;
    float line_height_ = 1.52f;
    float scroll_y_ = 0.0f;
    float content_height_ = 0.0f;
    float last_inner_width_ = 0.0f;
    float preferred_caret_x_ = -1.0f;
    NVGcontext* last_vg_ = nullptr;
    std::vector<VisualLine> visual_lines_;

    std::function<void(const std::string&)> on_submit_;
    std::function<void(const std::string&)> on_change_;
    std::function<void(bool)> on_edit_state_;
};

struct DeepSeekComposerSubmission {
    std::string prompt;
    bool curation_mode = false;
    bool deep_thinking = false;
    bool sync_playlist = true;
};

struct DeepSeekComposerOptions {
    bool compact = false;  ///< true for F8 Companion Dock, false for full-stage DeepSeek AI Workbench
    bool show_model_pill = true;
};

/**
 * DeepSeek-inspired inline rounded AI composer card:
 * - Optional top context pill (active domain / document / media attachment with quick detach)
 * - Unlimited multi-line auto-expanding text area (zero popup, zero character cap)
 * - Bottom pill bar: [深度思考] [灵感策展] [同步歌单] [模型路由] + Send (↑) / Stop (■) button
 */
class DeepSeekComposer : public brls::Box {
public:
    explicit DeepSeekComposer(DeepSeekComposerOptions opts = {});
    ~DeepSeekComposer() override;

    void setOnSubmit(std::function<void(const DeepSeekComposerSubmission&)> cb) {
        on_submit_ = std::move(cb);
    }
    void setOnCancelStream(std::function<void()> cb) {
        on_cancel_stream_ = std::move(cb);
    }
    void setOnModelPillClick(std::function<void()> cb) {
        on_model_click_ = std::move(cb);
    }
    void setOnStateChanged(std::function<void()> cb) {
        on_state_changed_ = std::move(cb);
    }

    void setStreaming(bool streaming);
    bool isStreaming() const { return streaming_; }

    void setDeepThinking(bool enabled);
    bool isDeepThinking() const { return deep_thinking_; }

    void setCurationMode(bool enabled);
    bool isCurationMode() const { return curation_mode_; }

    void setSyncPlaylist(bool enabled);
    bool isSyncPlaylist() const { return sync_playlist_; }

    void setModelLabel(const std::string& label);
    void setContextBadge(const std::string& badge_text, std::function<void()> on_clear = nullptr);

    void setDraftText(const std::string& text, bool focus_and_edit = true);
    std::string draftText() const;
    void focusInput(bool start_editing = true);
    void refreshThemeStyling();

    AiTextEditorView* editor() const { return editor_; }

private:
    struct PillRefs {
        brls::Box* box = nullptr;
        SvgIconView* icon = nullptr;
        brls::Label* label = nullptr;
    };

    PillRefs createTogglePill(const std::string& text, std::function<void()> on_click);
    void updateVisualState();
    void triggerSubmit();

    DeepSeekComposerOptions opts_;
    bool streaming_ = false;
    bool deep_thinking_ = false;
    bool curation_mode_ = false;
    bool sync_playlist_ = true;
    std::string model_label_ = "Auto Route";
    std::string context_text_;
    std::function<void()> on_clear_context_;

    std::function<void(const DeepSeekComposerSubmission&)> on_submit_;
    std::function<void()> on_cancel_stream_;
    std::function<void()> on_model_click_;
    std::function<void()> on_state_changed_;

    brls::Box* context_row_ = nullptr;
    brls::Box* context_chip_ = nullptr;
    brls::Label* context_label_ = nullptr;
    SvgIconView* context_clear_icon_ = nullptr;

    AiTextEditorView* editor_ = nullptr;
    PillRefs think_pill_;
    PillRefs curate_pill_;
    PillRefs playlist_pill_;
    PillRefs model_pill_;
    brls::Label* hint_label_ = nullptr;
    brls::Box* send_btn_ = nullptr;
    SvgIconView* send_icon_ = nullptr;
};

} // namespace omnidesk::host
