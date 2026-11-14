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
    PluginStoreView();
    ~PluginStoreView() override;

    void refresh();
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
    void renderCatalogOrInstalledList();
    void renderDiagnosticsView();
    void renderDetail(const std::string& plugin_id);
    void renderConfigSection(brls::Box* container, const pm::InstalledPluginRecord& rec);
    void appendLog(const std::string& line);
    void startGitInstall(const std::string& source, const std::string& branch);
    void startLocalInstall(const std::string& dir);
    void startUpdate(const std::string& plugin_id);
    void startUpdateRadar();

    FilterTab filter_tab_ = FilterTab::All;
    std::string search_query_;
    std::string selected_plugin_id_;
    uint64_t bus_sub_token_ = 0;

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::unordered_map<std::string, pm::UpdateCheckResult> radar_results_;
    std::mutex log_mutex_;
    std::string log_buffer_;
    bool busy_ = false;
    bool radar_busy_ = false;

    brls::Label* summary_label_ = nullptr;
    brls::Box* filter_bar_ = nullptr;
    brls::Box* list_container_ = nullptr;
    brls::Box* detail_container_ = nullptr;
    brls::Label* console_label_ = nullptr;
};

} // namespace omnidesk::host
