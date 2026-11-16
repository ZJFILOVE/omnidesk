#pragma once

#include <atomic>
#include <borealis.hpp>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/manager.hpp"

namespace omnidesk::host {

/**
 * In-app Plugin Marketplace UI ("插件市场").
 * Provides curated catalog browsing, GitHub/local build & install, live log console,
 * dynamic config_schema editor, Update Radar, rollback/enable/disable/uninstall, and capability inspector.
 */
class PluginStoreView : public brls::Box {
public:
    using OpenCustomTabCallback = std::function<void(const std::string& tab_id)>;
    using OpenDomainCallback = std::function<void(ContentDomain domain)>;
    using OpenAuthCenterCallback = std::function<void(const std::string& plugin_id)>;

    explicit PluginStoreView(OpenCustomTabCallback on_open_custom_tab = nullptr,
                             OpenDomainCallback on_open_domain = nullptr,
                             OpenAuthCenterCallback on_open_auth_center = nullptr);
    ~PluginStoreView() override;

    void refresh();
    void refreshIfNeeded();
    void focusPluginConfig(const std::string& plugin_id);
    void promptSearch();
    void openInstallModal(bool local_mode = false);
    void openScaffoldModal();

private:
    enum class FilterTab {
        All,
        Core,
        Media,
        Text,
        Book,
        Installed,
        Diagnostics,
    };

    void rebuildList();
    void updateSelectedCardStyling(const std::string& prev_id, const std::string& next_id);
    void renderCatalogOrInstalledList();
    void renderDiagnosticsView();
    void renderDetail(const std::string& plugin_id);
    void renderConfigSection(brls::Box* container, const pm::InstalledPluginRecord& rec);
    void scrollToConfigSection(bool focus_first_control);
    bool activateSelectedPluginEntry(const std::string& plugin_id);
    void reloadPluginAfterConfigEdit(const std::string& plugin_id,
                                     const std::string& focus_field_key,
                                     const std::string& toast_msg);
    void openSelectOptionModal(const std::string& plugin_id,
                               const ConfigField& field,
                               const std::string& current_value);
    void appendLog(const std::string& line);
    void startGitInstall(const std::string& source, const std::string& branch);
    void startLocalInstall(const std::string& dir);
    void startUpdate(const std::string& plugin_id);
    void startUpdateRadar();
    void startRebuildPlugin(const std::string& plugin_id);
    void startBatchRebuild(bool only_stale);

    OpenCustomTabCallback on_open_custom_tab_;
    OpenDomainCallback on_open_domain_;
    OpenAuthCenterCallback on_open_auth_center_;
    FilterTab filter_tab_ = FilterTab::All;
    std::string search_query_;
    std::string selected_plugin_id_;
    uint64_t bus_sub_token_ = 0;
    uint64_t auth_sub_token_ = 0;
    bool suppress_bus_refresh_ = false;

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::unordered_map<std::string, pm::UpdateCheckResult> radar_results_;
    std::unordered_map<std::string, pm::AbiStatusEntry> abi_status_cache_;
    struct LogState {
        std::mutex mutex;
        std::string buffer;
    };
    std::shared_ptr<LogState> log_state_ = std::make_shared<LogState>();
    bool busy_ = false;
    bool radar_busy_ = false;
    bool dirty_ = false;
    std::unordered_map<std::string, brls::Box*> list_cards_;
    std::unordered_map<std::string, brls::Button*> config_edit_buttons_;

    brls::Label* summary_label_ = nullptr;
    brls::Box* filter_bar_ = nullptr;
    brls::ScrollingFrame* list_scroll_ = nullptr;
    brls::Box* list_container_ = nullptr;
    brls::ScrollingFrame* detail_scroll_ = nullptr;
    brls::Box* detail_container_ = nullptr;
    brls::Box* config_section_box_ = nullptr;
    brls::Button* first_config_btn_ = nullptr;
    brls::Button* primary_entry_btn_ = nullptr;
    brls::Label* console_label_ = nullptr;
    brls::Button* rebuild_stale_btn_ = nullptr;
};

} // namespace omnidesk::host
