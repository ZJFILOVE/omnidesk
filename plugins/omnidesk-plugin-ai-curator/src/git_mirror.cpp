#include "ai_curator/git_mirror.hpp"

#include "ai_curator/utils.hpp"
#include <cstdlib>
#include <filesystem>

namespace fs = std::filesystem;

namespace ai_curator {

nlohmann::json RepoSyncStatus::toJson() const {
    return {
        {"path", path},
        {"exists", exists},
        {"is_git_repo", is_git_repo},
        {"cloned", cloned},
        {"pulled", pulled},
        {"dirty_worktree_preserved", dirty_worktree_preserved},
        {"error", error},
        {"status_detail", status_detail},
    };
}

std::string GitMirror::resolveDefaultManagedDataHome() {
    if (const char* env = std::getenv("OMNIDESK_DATA_HOME")) {
        if (*env) return env;
    }
    if (const char* xdg = std::getenv("XDG_DATA_HOME")) {
        if (*xdg) return (fs::path(xdg) / "omnidesk").string();
    }
    const char* home = std::getenv("HOME");
    return (fs::path(home && *home ? home : ".") / ".local" / "share" / "omnidesk").string();
}

std::string GitMirror::resolveDataRoot(const omnidesk::PluginContext* ctx,
                                       const nlohmann::json* req) {
    if (req && req->is_object()) {
        const std::string override_root = req->value("data_root", "");
        if (!override_root.empty()) {
            return override_root;
        }
    }
    if (ctx && !ctx->dataDir().empty()) {
        fs::path p(ctx->dataDir());
        if (p.filename() == "plugin-ai-curator" && p.parent_path().filename() == "plugin-data") {
            return p.parent_path().parent_path().string();
        }
        return p.string();
    }
    return resolveDefaultManagedDataHome();
}

static std::string resolveSingleRepoPath(const std::string& explicit_req,
                                         const std::string& config_val,
                                         const std::string& sibling_path,
                                         const std::string& data_root_repo,
                                         const std::string& managed_home_repo) {
    std::error_code ec;
    if (!explicit_req.empty()) return explicit_req;
    if (!config_val.empty()) return config_val;
    if (fs::exists(fs::path(sibling_path) / ".git", ec)) return sibling_path;
    if (fs::exists(data_root_repo, ec)) return data_root_repo;
    if (fs::exists(managed_home_repo, ec)) return managed_home_repo;
    return data_root_repo;
}

RepoPaths GitMirror::resolveRepoPaths(const omnidesk::PluginContext* ctx,
                                      const nlohmann::json* req) {
    RepoPaths out;
    out.data_root = resolveDataRoot(ctx, req);
    out.curation_dir = (fs::path(out.data_root) / "curation").string();

    std::string req_obs, req_mus;
    if (req && req->is_object()) {
        req_obs = req->value("obsidian_vault_dir", "");
        req_mus = req->value("music_daily_dir", "");
    }
    std::string cfg_obs, cfg_mus;
    if (ctx) {
        cfg_obs = ctx->getConfigString("obsidian_vault_dir", "");
        cfg_mus = ctx->getConfigString("music_daily_dir", "");
    }
    const std::string managed_home = resolveDefaultManagedDataHome();
    out.obsidian_vault_dir = resolveSingleRepoPath(
        req_obs, cfg_obs,
        "/home/zhu/Projects/obsidian-vault",
        (fs::path(out.curation_dir) / "repos" / "obsidian-vault").string(),
        (fs::path(managed_home) / "curation" / "repos" / "obsidian-vault").string());

    out.music_daily_dir = resolveSingleRepoPath(
        req_mus, cfg_mus,
        "/home/zhu/Projects/music-daily",
        (fs::path(out.curation_dir) / "repos" / "music-daily").string(),
        (fs::path(managed_home) / "curation" / "repos" / "music-daily").string());
    return out;
}

RepoSyncStatus GitMirror::syncRepo(const std::string& repo_dir,
                                   const std::string& remote_url,
                                   bool clone_if_missing,
                                   bool pull_remote) {
    RepoSyncStatus st;
    st.path = repo_dir;
    std::error_code ec;

    if (!fs::exists(repo_dir, ec)) {
        st.exists = false;
        if (!clone_if_missing) {
            st.status_detail = "missing_skip_clone";
            return st;
        }
        fs::path parent = fs::path(repo_dir).parent_path();
        if (!parent.empty()) {
            fs::create_directories(parent, ec);
        }
        ProcessResult clone_res = runProcess(
            {"git", "clone", "--branch", "main", remote_url, repo_dir}, "", 120000);
        if (!clone_res.spawn_error.empty() || clone_res.exit_code != 0) {
            st.error = clone_res.spawn_error.empty()
                           ? trim(clone_res.stderr_text + " " + clone_res.stdout_text)
                           : clone_res.spawn_error;
            st.status_detail = "clone_failed";
            return st;
        }
        st.exists = true;
        st.is_git_repo = fs::exists(fs::path(repo_dir) / ".git", ec);
        st.cloned = true;
        st.status_detail = "cloned";
        return st;
    }

    st.exists = true;
    st.is_git_repo = fs::exists(fs::path(repo_dir) / ".git", ec);
    if (!st.is_git_repo || !pull_remote) {
        st.status_detail = st.is_git_repo ? "present_no_pull" : "non_git_directory";
        return st;
    }

    ProcessResult status_res = runProcess({"git", "status", "--porcelain"}, repo_dir, 15000);
    if (status_res.exit_code != 0) {
        st.error = trim(status_res.stderr_text);
        st.status_detail = "git_status_failed";
        return st;
    }
    if (!trim(status_res.stdout_text).empty()) {
        st.dirty_worktree_preserved = true;
        st.status_detail = "dirty_worktree_preserved";
        return st;
    }

    ProcessResult fetch_res = runProcess({"git", "fetch", "origin", "main"}, repo_dir, 60000);
    if (fetch_res.exit_code != 0) {
        st.error = trim(fetch_res.stderr_text + " " + fetch_res.stdout_text);
        st.status_detail = "git_fetch_failed";
        return st;
    }
    ProcessResult merge_res = runProcess({"git", "merge", "--ff-only", "origin/main"}, repo_dir, 30000);
    if (merge_res.exit_code != 0) {
        st.error = trim(merge_res.stderr_text + " " + merge_res.stdout_text);
        st.status_detail = "git_merge_ff_only_failed";
        return st;
    }
    st.pulled = true;
    st.status_detail = "ff_merged";
    return st;
}

} // namespace ai_curator
