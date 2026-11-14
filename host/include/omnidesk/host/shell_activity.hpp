#pragma once

#include <atomic>
#include <borealis.hpp>
#include <memory>
#include <string>
#include <vector>
#include "omnidesk/types.hpp"

namespace omnidesk::host {

class PluginStoreView;
class MediaWorkbench;
class TextWorkbench;
class BookWorkbench;
class HostSettingsView;
class AiCompanionDock;

enum class WorkspaceId {
    Store,
    Media,
    Text,
    Book,
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
    void cycleWorkspace(int delta);
    void toggleCompanionDock();
    void triggerSearch();
    void toggleTheme();
    bool cancelActiveWork();
    bool skipSponsorSegment();

private:
    struct NavEntry {
        WorkspaceId id;
        std::string custom_tab_id;
        std::string label;
        ContentDomain domain = ContentDomain::Core;
        brls::Box* button = nullptr;
    };

    void populateRootBox();
    void rebuildRail();
    void rebuildPluginTabs();
    void updateActiveNavStyling();
    void updateResponsiveDock();
    void refreshAiPill();
    void refreshJobBar();
    void handleJobOpenEvent(const std::string& topic, const nlohmann::json& payload);
    brls::Box* makeRailButton(const std::string& label,
                              const std::string& svg_rel,
                              const std::string& shortcut_hint,
                              bool active,
                              std::function<void()> on_click);

    WorkspaceId active_workspace_;
    std::string active_custom_tab_id_;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    uint64_t dom_sub_token_ = 0;
    uint64_t bus_sub_token_ = 0;
    uint64_t job_sub_token_ = 0;
    std::vector<uint64_t> job_open_sub_tokens_;
    brls::VoidEvent::Subscription resize_sub_;

    brls::Box* root_box_ = nullptr;
    brls::Box* rail_box_ = nullptr;
    brls::Box* built_in_nav_box_ = nullptr;
    brls::Box* custom_nav_box_ = nullptr;
    brls::Box* main_split_ = nullptr;
    brls::Box* stage_container_ = nullptr;
    brls::Box* views_host_ = nullptr;
    brls::Box* job_bar_box_ = nullptr;
    brls::Label* ai_route_pill_ = nullptr;
    brls::Label* theme_btn_label_ = nullptr;

    PluginStoreView* store_view_ = nullptr;
    MediaWorkbench* media_view_ = nullptr;
    TextWorkbench* text_view_ = nullptr;
    BookWorkbench* book_view_ = nullptr;
    HostSettingsView* settings_view_ = nullptr;
    AiCompanionDock* dock_view_ = nullptr;
    brls::View* active_custom_view_ = nullptr;

    std::vector<NavEntry> nav_entries_;
};

} // namespace omnidesk::host
