#pragma once

#include <filesystem>
#include <functional>
#include <mutex>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <vector>
#include "omnidesk/export.h"
#include "omnidesk/pm/manifest.hpp"

namespace omnidesk::pm {

struct InstalledPluginRecord {
    PluginManifest manifest;
    std::string install_dir;
    std::string so_path;
    std::string source_url;
    std::string branch;
    std::string commit_sha;
    std::string so_sha256;
    std::string installed_at;
    bool enabled = true;
    bool dev_local = false;
    std::string install_source = "source-build";
    std::string release_tag;
    std::string sdk_version;
    std::string sdk_abi_signature;
    std::vector<std::string> backup_versions;
};

struct InstallResult {
    bool ok = false;
    std::string plugin_id;
    std::string version;
    std::string commit_sha;
    std::string so_path;
    std::string install_source;
    bool used_prebuilt = false;
    std::string fallback_reason;
    std::string log;
    std::string error;
};

struct UpdateCheckResult {
    bool ok = false;
    bool update_available = false;
    std::string plugin_id;
    std::string local_commit;
    std::string remote_commit;
    int ahead_commits = 0;
    std::string changelog;
    std::string diff_stat;
    bool prebuilt_available = false;
    ReleaseArtifactSpec prebuilt;
    std::string error;
};

struct AbiStatusEntry {
    std::string plugin_id;
    std::string version;
    std::string so_path;
    std::string plugin_sdk_version;
    std::string plugin_abi_signature;
    std::string host_sdk_version;
    std::string host_abi_signature;
    bool enabled = true;
    bool compatible = false;
    bool needs_rebuild = false;
    bool can_rebuild = false;
    std::string reason;
};

struct BatchRebuildResult {
    bool ok = true;
    int rebuilt_count = 0;
    int skipped_count = 0;
    int failed_count = 0;
    std::vector<InstallResult> results;
    std::string error;
};

using ProgressCallback = std::function<void(const std::string& stage, const std::string& detail)>;

/**
 * Manages plugin installation from prebuilt .tar.zst releases, Git repositories
 * (staged in /tmp/omnidesk-staging to protect /home disk budget), or local directories,
 * with subprocess ABI checks, upstreams.lock.json locking, Update Radar, and 1-click rollback.
 */
class OMNIDESK_API PluginInstaller {
public:
    PluginInstaller(std::string data_root,
                    std::string sdk_cmake_dir,
                    std::string abi_checker_exe = "");

    const std::string& dataRoot() const { return data_root_; }
    std::string pluginsDir() const;
    std::string stagingDir() const;
    std::string backupsDir() const;
    std::string lockFilePath() const;
    std::string stateFilePath() const;

    InstallResult installFromGit(const std::string& git_source,
                                 const std::string& branch = "",
                                 ProgressCallback cb = nullptr,
                                 const std::optional<ReleaseArtifactSpec>& release_hint = std::nullopt,
                                 bool allow_prebuilt = true);

    InstallResult installFromPrebuilt(const ReleaseArtifactSpec& release,
                                      const std::string& source_url = "",
                                      const std::string& branch = "main",
                                      const std::string& commit_sha = "",
                                      ProgressCallback cb = nullptr);

    InstallResult installFromLocal(const std::string& local_dir,
                                   ProgressCallback cb = nullptr);

    UpdateCheckResult checkUpdate(const std::string& plugin_id,
                                  bool include_changelog = true) const;

    std::vector<UpdateCheckResult> checkAllUpdates(bool include_changelog = true) const;

    InstallResult updatePlugin(const std::string& plugin_id,
                               ProgressCallback cb = nullptr);

    std::vector<AbiStatusEntry> checkInstalledAbiStatus(bool verify_binaries = true) const;
    bool hasStaleInstalledPlugins(bool verify_binaries = true) const;

    InstallResult rebuildPlugin(const std::string& plugin_id,
                                ProgressCallback cb = nullptr);

    BatchRebuildResult rebuildPlugins(bool only_stale = true,
                                      ProgressCallback cb = nullptr);

    InstallResult rollbackPlugin(const std::string& plugin_id);
    bool removePlugin(const std::string& plugin_id, std::string& error);

    bool setEnabled(const std::string& plugin_id, bool enabled);
    bool isEnabled(const std::string& plugin_id) const;

    std::vector<InstalledPluginRecord> listInstalled() const;
    std::optional<InstalledPluginRecord> getInstalled(const std::string& plugin_id) const;

    static std::string computeSha256(const std::string& file_path);
    static bool checkSoAbiInProcess(const std::string& so_path,
                                    const std::string& expected_id,
                                    std::string& error_out,
                                    nlohmann::json* info_out = nullptr);

private:
    std::optional<ReleaseArtifactSpec> findPrebuiltRelease(
        const std::string& normalized_git_url,
        const std::string& branch,
        const std::optional<ReleaseArtifactSpec>& hint) const;

    InstallResult activatePluginArtifact(const PluginManifest& manifest,
                                         const std::string& built_so_path,
                                         const std::string& manifest_path,
                                         const std::string& readme_path,
                                         const std::string& source_url,
                                         const std::string& branch,
                                         const std::string& commit_sha,
                                         bool dev_local,
                                         const std::string& install_source,
                                         const std::string& release_tag,
                                         ProgressCallback cb);

    InstallResult stageBuildAndActivate(const std::string& source_dir,
                                        const std::string& source_url,
                                        const std::string& branch,
                                        const std::string& commit_sha,
                                        bool dev_local,
                                        ProgressCallback cb);

    bool verifyViaSubprocess(const std::string& so_path,
                             const std::string& expected_id,
                             std::string& log,
                             std::string& error,
                             nlohmann::json* info_out = nullptr) const;

    nlohmann::json loadLockFile() const;
    bool saveLockFile(const nlohmann::json& j) const;
    nlohmann::json loadStateFile() const;
    bool saveStateFile(const nlohmann::json& j) const;

    std::string data_root_;
    std::string sdk_cmake_dir_;
    std::string abi_checker_exe_;

    mutable std::mutex state_cache_mutex_;
    mutable bool state_cache_valid_ = false;
    mutable std::filesystem::file_time_type state_cache_mtime_{};
    mutable uintmax_t state_cache_size_ = 0;
    mutable nlohmann::json state_cache_json_;
};

} // namespace omnidesk::pm
