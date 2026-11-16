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
#include "omnidesk/host/ai_workbench.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::host {

class SvgIconView;

/**
 * Unified F8 AI Assistant & Scene Companion Dock.
 * Shares persistent sessions, DeepSeek message turn visual language, collapsible reasoning blocks,
 * model router picker, and curation capabilities with full-stage AiWorkbench, while isolating
 * domain plugin widgets inside a dedicated scrollable, visually harmonized Tools view.
 */
class AiCompanionDock : public brls::Box {
public:
    enum class DockTab : uint8_t {
        Chat = 0,
        Tools = 1,
    };

    explicit AiCompanionDock(
        std::function<void()> on_open_marketplace = nullptr,
        std::function<void()> on_open_ai_workspace = nullptr,
        std::function<void(const std::string& session_id)> on_expand_session = nullptr);
    ~AiCompanionDock() override;

    void setOnOpenAiWorkspace(std::function<void()> cb) { on_open_ai_workspace_ = std::move(cb); }
    void setOnExpandSession(std::function<void(const std::string& session_id)> cb) {
        on_expand_session_ = std::move(cb);
    }

    void setDomainContext(ContentDomain domain, const std::string& entity_summary = "");
    ContentDomain domainContext() const { return domain_; }
    const std::string& entitySummary() const { return entity_summary_; }

    void toggleVisible();
    bool isDockedOpen() const { return open_; }
    void applyResponsiveMode(float window_width, float window_height);

    void refreshAiRouteStatus();
    void onDomainsChanged();
    void switchTab(DockTab tab);
    DockTab activeTab() const { return active_tab_; }

    void startNewSession(bool curation_mode = false);
    void selectSessionById(const std::string& session_id);
    std::string activeSessionId() const;
    void clearConversation();
    void focusComposer(bool start_editing = true);

    void askWithContext(ContentDomain domain,
                        const std::string& context_summary,
                        const std::string& prompt);

    bool hasActiveStream() const { return active_cancel_ != nullptr; }
    bool cancelActiveStream();
    void submitCurationPrompt(const std::string& prompt);

    brls::View* getDefaultFocus() override;

private:
    void loadSessionsFromDisk();
    void saveSessionsToDisk() const;
    AiChatSession& currentSession();
    const AiChatSession& currentSession() const;
    void selectSession(size_t index);
    void deleteSession(size_t index);

    void updateHeaderAndTabsStyling();
    void updateStageVisibility();
    void rebuildHeroAndStarters();
    void rebuildQuickPromptChips();
    void rebuildPluginToolsView();
    void rebuildThreadViews();
    brls::Box* buildMessageTurnView(size_t msg_idx);
    void appendMessageTurnView(size_t msg_idx);
    void resetLiveTurnRefs();

    void openSessionAndUtilityDialog();
    void openModelPickerDialog();
    void triggerArchiveRehydrate();
    void expandToFullAiWorkspace();

    void handleComposerSubmit(const DeepSeekComposerSubmission& sub);
    void submitPrompt(const std::string& prompt, bool deep_thinking = false);
    void dispatchCurationRequest(const std::string& prompt, bool sync_playlist);
    void regenerateFromAssistantIndex(size_t assistant_msg_idx);

    void scheduleStreamUiFlush(uint64_t seq, bool curation_only);
    void flushStreamUiNow(uint64_t seq, bool curation_only);
    void finalizeActiveStream(const std::string& display_role,
                              const std::string& raw_reply,
                              const std::string& explicit_reasoning,
                              bool is_error);
    void scrollToBottom(bool immediate = false);

    std::function<void()> on_open_marketplace_;
    std::function<void()> on_open_ai_workspace_;
    std::function<void(const std::string& session_id)> on_expand_session_;

    ContentDomain domain_ = ContentDomain::Core;
    std::string entity_summary_;
    DockTab active_tab_ = DockTab::Chat;
    bool open_ = false;
    bool bottom_mode_ = false;
    bool sync_playlist_enabled_ = true;
    bool other_tools_expanded_ = false;
    bool route_dirty_ = true;
    bool tools_dirty_ = true;
    bool starters_dirty_ = true;
    size_t primary_tool_count_ = 0;
    size_t total_tool_count_ = 0;

    std::vector<AiChatSession> sessions_;
    size_t active_session_idx_ = 0;
    std::string current_route_model_ = "Auto Route";
    bool route_ready_ = false;

    uint64_t ephemeral_note_sub_ = 0;
    uint64_t sessions_sync_sub_ = 0;

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> active_cancel_;
    std::shared_ptr<CancelToken> route_cancel_;
    uint64_t active_req_seq_ = 0;
    uint64_t route_req_seq_ = 0;
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

    // Live streaming refs inside active turn
    brls::Box* live_turn_box_ = nullptr;
    brls::Label* live_role_label_ = nullptr;
    brls::Box* live_think_box_ = nullptr;
    brls::Label* live_think_header_label_ = nullptr;
    brls::Label* live_think_body_label_ = nullptr;
    brls::Box* live_reply_host_ = nullptr;
    brls::Label* live_reply_label_ = nullptr;

    // Header & Subbar views
    brls::Box* session_selector_btn_ = nullptr;
    SvgIconView* header_emblem_ = nullptr;
    brls::Label* session_title_label_ = nullptr;
    SvgIconView* session_chevron_ = nullptr;

    brls::Box* tab_chat_btn_ = nullptr;
    SvgIconView* tab_chat_icon_ = nullptr;
    brls::Label* tab_chat_label_ = nullptr;

    brls::Box* tab_tools_btn_ = nullptr;
    SvgIconView* tab_tools_icon_ = nullptr;
    brls::Label* tab_tools_label_ = nullptr;

    brls::Box* route_chip_ = nullptr;
    brls::Box* route_dot_ = nullptr;
    brls::Label* route_badge_label_ = nullptr;

    // Stage views
    brls::ScrollingFrame* empty_scroll_ = nullptr;
    brls::Box* empty_inner_ = nullptr;
    brls::Box* hero_domain_dot_ = nullptr;
    brls::Label* hero_domain_label_ = nullptr;
    brls::Box* starter_cards_box_ = nullptr;

    brls::ScrollingFrame* thread_scroll_ = nullptr;
    brls::Box* messages_box_ = nullptr;
    brls::Box* quick_chips_row_ = nullptr;

    brls::ScrollingFrame* tools_scroll_ = nullptr;
    brls::Box* tools_inner_ = nullptr;

    DeepSeekComposer* composer_ = nullptr;
};

} // namespace omnidesk::host
