#pragma once

#include <nlohmann/json.hpp>
#include <omnidesk/plugin.hpp>
#include <string>
#include <vector>

namespace ai_curator {

inline constexpr const char* kObsidianVaultRemoteUrl = "https://github.com/ZJFILOVE/obsidian-vault.git";
inline constexpr const char* kMusicDailyRemoteUrl = "https://github.com/ZJFILOVE/music-daily.git";

struct RepoPaths {
    std::string data_root;
    std::string curation_dir;
    std::string obsidian_vault_dir;
    std::string music_daily_dir;
};

struct RepoSyncStatus {
    std::string path;
    bool exists = false;
    bool is_git_repo = false;
    bool has_remote = false;
    bool cloned = false;
    bool pulled = false;
    bool dirty_worktree_preserved = false;
    int ahead_count = 0;
    int behind_count = 0;
    std::string head_sha;
    std::string remote_head_sha;
    std::string error;
    std::string status_detail;

    nlohmann::json toJson() const;
};

struct RepoPushStatus {
    std::string path;
    bool exists = false;
    bool is_git_repo = false;
    bool has_remote = false;
    bool pushed = false;
    bool already_synced = false;
    bool has_uncommitted_changes = false;
    bool has_uncommitted_targets = false;
    int ahead_count = 0;
    int behind_count = 0;
    std::string head_sha;
    std::string remote_head_sha;
    std::string error;
    std::string status_detail;

    bool ok() const {
        return error.empty() && (!has_remote || (!has_uncommitted_targets && (pushed || already_synced)));
    }
    nlohmann::json toJson() const;
};

struct BidirectionalSyncStatus {
    RepoSyncStatus pull;
    RepoPushStatus push;

    bool ok() const { return pull.error.empty() && push.error.empty(); }
    nlohmann::json toJson() const;
};

class GitMirror {
public:
    static std::string resolveDefaultManagedDataHome();
    static std::string resolveDataRoot(const omnidesk::PluginContext* ctx,
                                       const nlohmann::json* req = nullptr);
    static RepoPaths resolveRepoPaths(const omnidesk::PluginContext* ctx,
                                      const nlohmann::json* req = nullptr);

    static RepoSyncStatus syncRepo(const std::string& repo_dir,
                                   const std::string& remote_url,
                                   bool clone_if_missing,
                                   bool pull_remote,
                                   const std::string& branch = "main");

    static RepoPushStatus pushRepo(const std::string& repo_dir,
                                   const std::string& branch = "main",
                                   const std::vector<std::string>& check_paths = {});

    static BidirectionalSyncStatus syncBidirectional(const std::string& repo_dir,
                                                     const std::string& remote_url,
                                                     bool clone_if_missing = false,
                                                     const std::string& branch = "main");
};

} // namespace ai_curator
