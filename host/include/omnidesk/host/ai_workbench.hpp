#pragma once

#include <atomic>
#include <borealis.hpp>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "omnidesk/async.hpp"
#include "omnidesk/host/ai_composer.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::host {

struct AiChatMessage {
    std::string role;          ///< "user", "assistant", "system"
    std::string display_role;  ///< e.g. "AI Agent (claude-sonnet-4)" or "AI 策展大脑"
    std::string content;       ///< Final clean reply markdown (without <think> wrapper)
    std::string reasoning;     ///< DeepSeek R1 / thinking process text
    double thinking_sec = 0.0; ///< Elapsed reasoning duration in seconds
    bool curation = false;
    bool error = false;
    bool thinking_collapsed = false;
};

struct AiChatSession {
    std::string id;
    std::string title = "新对话";
    ContentDomain domain = ContentDomain::Core;
    std::string context_summary;
    bool curation_mode = false;
    bool deep_thinking = false;
    int64_t updated_at = 0;
    std::vector<AiChatMessage> messages;
};

// Shared persistence & UI helpers used by both AiWorkbench and AiCompanionDock
std::string makeAiChatSessionId();
std::string summarizeAiSessionTitle(const std::string& first_prompt);
std::string classifyAiCurationItem(const nlohmann::json& item);
void splitAiThinkAndReply(const std::string& raw,
                          const std::string& explicit_reasoning,
                          std::string* reasoning_out,
                          std::string* reply_out,
                          bool* inside_open_think = nullptr);
std::vector<AiChatSession> loadAiChatSessionsFromDisk();
void saveAiChatSessionsToDisk(const std::vector<AiChatSession>& sessions);
void notifyAiSessionsUpdated(const std::string& origin, const std::string& active_session_id);
void openAiModelPickerDialog(const std::string& current_route_model,
                             std::function<void()> on_open_marketplace,
                             std::function<void(const std::string&)> on_selected = nullptr);

/**
 * Full-stage DeepSeek-inspired AI Chat & Curation Workbench:
 * - Left collapsible multi-session sidebar with persistent local history
 * - Empty session state: centered greeting hero + centered inline DeepSeekComposer + scenario cards
 * - Active session state: multi-turn thread with collapsible DeepSeek reasoning blocks, rich Markdown,
 *   copy/edit/regenerate actions, and bottom-anchored unlimited DeepSeekComposer
 */
class AiWorkbench : public brls::Box {
public:
    explicit AiWorkbench(std::function<void()> on_open_marketplace = nullptr,
                         std::function<void(const std::string& session_id)> on_dock_to_companion = nullptr);
    ~AiWorkbench() override;

    void setOnDockToCompanion(std::function<void(const std::string& session_id)> cb) {
        on_dock_to_companion_ = std::move(cb);
    }

    void refresh();
    void refreshRouteStatus();
    void onViewportLayoutChanged();

    void setExternalContext(ContentDomain domain, const std::string& summary);
    void askWithContext(ContentDomain domain,
                        const std::string& context_summary,
                        const std::string& prompt,
                        bool curation_mode = false);
    void startNewSession(bool curation_mode = false);
    void selectSessionById(const std::string& session_id);
    std::string activeSessionId() const;
    void focusComposer(bool start_editing = true);

    bool hasActiveStream() const { return active_cancel_ != nullptr; }
    bool cancelActiveStream();

    brls::View* getDefaultFocus() override;

private:
    void loadSessionsFromDisk();
    void saveSessionsToDisk() const;
    AiChatSession& currentSession();
    const AiChatSession& currentSession() const;
    void selectSession(size_t index);
    void deleteSession(size_t index);

    void rebuildSessionSidebar();
    void rebuildDomainPills();
    void rebuildThreadViews();
    brls::Box* buildMessageTurnView(size_t msg_idx);
    void appendMessageTurnView(size_t msg_idx);
    void resetLiveTurnRefs();
    void scheduleStreamUiFlush(uint64_t seq, bool curation_only);
    void flushStreamUiNow(uint64_t seq, bool curation_only);
    void updateStageModeLayout();
    void scrollToBottom(bool immediate = false);
    void openModelPickerDialog();
    void triggerArchiveRehydrate();

    void handleComposerSubmit(const DeepSeekComposerSubmission& sub);
    void dispatchChatRequest(const std::string& prompt, bool deep_thinking);
    void dispatchCurationRequest(const std::string& prompt, bool sync_playlist);
    void regenerateFromAssistantIndex(size_t assistant_msg_idx);
    void finalizeActiveStream(const std::string& display_role,
                              const std::string& raw_reply,
                              const std::string& explicit_reasoning,
                              bool is_error);

    std::function<void()> on_open_marketplace_;
    std::function<void(const std::string& session_id)> on_dock_to_companion_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);

    std::vector<AiChatSession> sessions_;
    size_t active_session_idx_ = 0;
    bool sidebar_collapsed_ = false;
    ContentDomain attached_domain_ = ContentDomain::Core;
    std::string attached_summary_;
    std::string current_route_model_ = "Auto Route";
    uint64_t ephemeral_note_sub_ = 0;
    uint64_t sessions_sync_sub_ = 0;
    uint64_t route_req_seq_ = 0;
    std::shared_ptr<CancelToken> route_cancel_;

    // Active streaming state
    std::shared_ptr<CancelToken> active_cancel_;
    uint64_t active_req_seq_ = 0;
    size_t streaming_session_idx_ = 0;
    std::string streaming_session_id_;
    uint64_t stream_start_ms_ = 0;
    uint64_t reasoning_end_ms_ = 0;
    uint64_t last_stream_flush_ms_ = 0;
    std::mutex stream_mu_;
    std::string pending_content_delta_;
    std::string pending_reasoning_delta_;
    uint64_t stream_flush_seq_ = 0;
    std::string live_raw_content_;
    std::string live_reasoning_;

    // Live streaming view refs inside active assistant turn
    brls::Box* live_turn_box_ = nullptr;
    brls::Label* live_role_label_ = nullptr;
    brls::Box* live_think_box_ = nullptr;
    brls::Label* live_think_header_label_ = nullptr;
    brls::Label* live_think_body_label_ = nullptr;
    brls::Box* live_reply_host_ = nullptr;
    brls::Label* live_reply_label_ = nullptr;

    // Layout views
    brls::Box* sidebar_box_ = nullptr;
    class RecyclingGrid* sessions_grid_ = nullptr;
    brls::Box* expand_sidebar_btn_ = nullptr;
    brls::Label* header_title_label_ = nullptr;
    brls::Box* domain_pills_box_ = nullptr;
    brls::Label* route_badge_label_ = nullptr;

    brls::Box* stage_col_ = nullptr;
    brls::Box* hero_top_spacer_ = nullptr;
    brls::Box* hero_box_ = nullptr;
    brls::ScrollingFrame* thread_scroll_ = nullptr;
    brls::Box* thread_inner_ = nullptr;
    brls::Box* messages_box_ = nullptr;
    brls::Box* composer_wrap_ = nullptr;
    DeepSeekComposer* composer_ = nullptr;
    brls::Box* starter_grid_ = nullptr;
    brls::Label* footer_caption_ = nullptr;
    brls::Box* hero_bottom_spacer_ = nullptr;
};

} // namespace omnidesk::host
