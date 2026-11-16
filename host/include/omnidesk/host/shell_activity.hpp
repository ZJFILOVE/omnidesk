#pragma once

#include <atomic>
#include <borealis.hpp>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>
#include "omnidesk/types.hpp"

namespace omnidesk::host {

class PluginStoreView;
class MediaWorkbench;
class TextWorkbench;
class BookWorkbench;
class AiWorkbench;
class HostSettingsView;
class AiCompanionDock;
class MiniPlayerBar;
class SvgIconView;

enum class WorkspaceId {
    Store,
    Media,
    Text,
    Book,
    AiChat,
    Settings,
    CustomPluginTab,
};

/**
 * Root desktop workbench Activity modeled on native-wiliwili ergonomics:
 * left compact navigation rail + active domain workspace stage + bottom JobCenter bar + F8 companion dock.
 */
class WorkspaceShellActivity : public brls::Activity {
public:
    explicit WorkspaceShellActivity(WorkspaceId initial_workspace = WorkspaceId::Store);
    ~WorkspaceShellActivity() override;

    brls::View* createContentView() override;
    void onContentAvailable() override;

    void selectWorkspace(WorkspaceId id, const std::string& custom_tab_id = "");
    void selectWorkspaceByIndex(int zero_based_index);
    bool selectPluginByShortcut(char alt_key);
    void cycleWorkspace(int delta);
    void toggleCompanionDock();
    void toggleWindowFullscreen();
    void toggleImmersiveFullscreen();
    void setImmersiveFullscreen(bool active);
    void triggerSearch();
    void openCommandPalette(const std::string& initial_query = "");
    void openAuthCenter(const std::string& focus_plugin_id = "");
    void openPluginConfigInStore(const std::string& plugin_id);
    void toggleTheme();
    bool cancelActiveWork();
    bool skipSponsorSegment();

private:
    struct NavEntry {
        WorkspaceId id;
        std::string custom_tab_id;
        std::string label;
        ContentDomain domain = ContentDomain::Core;
        std::string domain_token = "domain/core";
        std::string icon_kind = "extension";
        char alt_key = '\0';
        std::string shortcut_label;
        int group_idx = -1;
        brls::Box* button = nullptr;
        brls::Box* accent_bar = nullptr;
        SvgIconView* icon_view = nullptr;
        brls::Label* title_label = nullptr;
        brls::Box* keycap_box = nullptr;
        brls::Label* keycap_label = nullptr;
        int styled_state = -1;
    };

    struct GroupHeaderEntry {
        int group_idx = 0;
        std::string domain_token = "domain/text";
        brls::Box* header_box = nullptr;
        brls::Box* active_badge_box = nullptr;
        brls::Label* active_badge_label = nullptr;
    };

    void populateRootBox();
    brls::View* ensureWorkspaceCreated(WorkspaceId id);
    AiCompanionDock* ensureCompanionDockCreated();
    void setCompanionDomainContext(ContentDomain domain, const std::string& summary = "");
    void setExternalAiContext(ContentDomain domain, const std::string& summary);
    void rebuildRail();
    void rebuildPluginTabs(int focus_group_idx = -1);
    void focusActiveWorkspace();
    void updateActiveNavStyling();
    void updateResponsiveDock();
    void refreshAiPill();
    void refreshAuthPill();
    void refreshCurationPill(const nlohmann::json& st = {});
    void refreshJobBar();
    void showWarningBanner(const std::string& message, const std::string& topic = "");
    void dismissWarningBanner();
    void handleJobOpenEvent(const std::string& topic, const nlohmann::json& payload);
    brls::Box* makeRailButton(const std::string& label,
                              const std::string& icon_kind,
                              const std::string& domain_token,
                              const std::string& shortcut_hint,
                              bool active,
                              bool compact,
                              std::function<void()> on_click,
                              NavEntry* out_refs = nullptr);

    WorkspaceId active_workspace_;
    std::string active_custom_tab_id_;
    WorkspaceId last_companion_workspace_ = WorkspaceId::Media;
    std::string last_companion_custom_tab_id_;
    bool has_last_companion_workspace_ = false;
    bool has_return_workspace_ = false;
    WorkspaceId return_workspace_ = WorkspaceId::Text;
    std::string return_custom_tab_id_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    uint64_t dom_sub_token_ = 0;
    uint64_t bus_sub_token_ = 0;
    uint64_t sched_ui_sub_token_ = 0;
    uint64_t auth_ui_sub_token_ = 0;
    uint64_t job_sub_token_ = 0;
    std::vector<uint64_t> job_open_sub_tokens_;
    brls::VoidEvent::Subscription resize_sub_;
    bool immersive_fullscreen_ = false;
    bool immersive_entered_from_window_fs_ = false;

    brls::Box* root_box_ = nullptr;
    brls::Box* rail_box_ = nullptr;
    brls::Box* built_in_nav_box_ = nullptr;
    brls::Box* custom_nav_box_ = nullptr;
    brls::Box* main_split_ = nullptr;
    brls::Box* stage_container_ = nullptr;
    brls::Box* views_host_ = nullptr;
    brls::Box* chrome_bottom_box_ = nullptr;
    brls::Box* fault_banner_box_ = nullptr;
    brls::Label* fault_banner_label_ = nullptr;
    brls::Box* job_bar_box_ = nullptr;
    brls::Label* ai_route_pill_ = nullptr;
    brls::Label* auth_status_pill_ = nullptr;
    brls::Label* curation_status_pill_ = nullptr;
    brls::Label* theme_btn_label_ = nullptr;
    std::vector<uint64_t> fault_sub_tokens_;
    std::string active_warning_topic_;

    PluginStoreView* store_view_ = nullptr;
    MediaWorkbench* media_view_ = nullptr;
    TextWorkbench* text_view_ = nullptr;
    BookWorkbench* book_view_ = nullptr;
    AiWorkbench* ai_view_ = nullptr;
    HostSettingsView* settings_view_ = nullptr;
    AiCompanionDock* dock_view_ = nullptr;
    MiniPlayerBar* mini_player_bar_ = nullptr;
    brls::View* active_custom_view_ = nullptr;
    std::unordered_map<std::string, brls::View*> custom_tab_views_;
    bool store_dirty_ = false;
    bool media_dirty_ = false;
    bool text_dirty_ = false;
    bool book_dirty_ = false;
    bool ai_dirty_ = false;
    bool settings_dirty_ = false;
    ContentDomain pending_dock_domain_ = ContentDomain::Core;
    std::string pending_dock_summary_;
    ContentDomain pending_ai_ext_domain_ = ContentDomain::Core;
    std::string pending_ai_ext_summary_;
    bool has_pending_ai_ext_ = false;
    std::string pending_all_date_filter_;

    bool collapsed_groups_[3] = {false, false, false};
    std::vector<NavEntry> nav_entries_;
    std::vector<GroupHeaderEntry> group_headers_;
    std::unordered_map<char, std::string> alt_shortcut_map_;
};

} // namespace omnidesk::host
