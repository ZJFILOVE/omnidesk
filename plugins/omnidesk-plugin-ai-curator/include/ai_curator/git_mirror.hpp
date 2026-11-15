#pragma once

#include <nlohmann/json.hpp>
#include <omnidesk/plugin.hpp>
#include <string>

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
    bool cloned = false;
    bool pulled = false;
    bool dirty_worktree_preserved = false;
    std::string error;
    std::string status_detail;

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
                                   bool pull_remote);
};

} // namespace ai_curator
