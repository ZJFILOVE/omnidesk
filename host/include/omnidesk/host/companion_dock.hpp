#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include "omnidesk/async.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::host {

/**
 * Responsive F8 AI & Plugin Companion Dock.
 * Switches between a 350px right-hand rail on widescreen (>=1.45 aspect)
 * and a 280px bottom drawer on narrow/split desktop viewports.
 */
class AiCompanionDock : public brls::Box {
public:
    explicit AiCompanionDock(std::function<void()> on_open_marketplace = nullptr);
    ~AiCompanionDock() override;

    void setDomainContext(ContentDomain domain, const std::string& entity_summary = "");
    ContentDomain domainContext() const { return domain_; }
    const std::string& entitySummary() const { return entity_summary_; }
    void toggleVisible();
    bool isDockedOpen() const { return open_; }
    void applyResponsiveMode(float window_width, float window_height);
    void refreshAiRouteStatus();
    void askWithContext(ContentDomain domain,
                        const std::string& context_summary,
                        const std::string& prompt);

    bool hasActiveStream() const { return active_cancel_ != nullptr; }
    bool cancelActiveStream();
    void submitCurationPrompt(const std::string& prompt);

private:
    void rebuildQuickPresets();
    void submitPrompt(const std::string& prompt);
    void triggerArchiveRehydrate();
    brls::Label* appendMessage(const std::string& role,
                               const std::string& text,
                               brls::Label** role_label_out = nullptr);
    void finalizeStreamUi(const std::string& role_text,
                          const std::string& body_text,
                          const char* color_key);

    std::function<void()> on_open_marketplace_;
    ContentDomain domain_ = ContentDomain::Core;
    std::string entity_summary_;
    bool open_ = false;
    bool bottom_mode_ = false;
    bool sync_playlist_enabled_ = true;
    bool route_dirty_ = true;
    bool presets_dirty_ = true;
    bool has_built_presets_ = false;
    uint64_t ephemeral_note_sub_ = 0;

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> active_cancel_;
    std::shared_ptr<CancelToken> route_cancel_;
    uint64_t active_request_seq_ = 0;
    uint64_t route_req_seq_ = 0;
    std::mutex stream_mutex_;
    std::string stream_pending_delta_;
    bool stream_flush_scheduled_ = false;
    std::string active_stream_text_;
    brls::Label* active_role_label_ = nullptr;
    brls::Label* active_body_label_ = nullptr;

    brls::Label* domain_badge_ = nullptr;
    brls::Label* route_status_label_ = nullptr;
    brls::Box* quick_actions_box_ = nullptr;
    brls::Box* widget_slot_box_ = nullptr;
    brls::Box* messages_box_ = nullptr;
    brls::Button* stop_btn_ = nullptr;
    brls::Button* playlist_toggle_btn_ = nullptr;
};

} // namespace omnidesk::host
