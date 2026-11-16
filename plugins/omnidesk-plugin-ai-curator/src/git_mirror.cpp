#include "ai_curator/git_mirror.hpp"

#include "ai_curator/utils.hpp"
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <sstream>

namespace fs = std::filesystem;

namespace ai_curator {

namespace {

bool containsControlCharacter(const std::string& value) {
    for (unsigned char c : value) {
        if (c < 0x20 || c == 0x7f) return true;
    }
    return false;
}

bool isSafeGitBranch(const std::string& branch) {
    if (branch.empty() || branch.size() > 255 || branch.front() == '-' ||
        containsControlCharacter(branch)) {
        return false;
    }
    for (char c : branch) {
        if (std::isspace(static_cast<unsigned char>(c))) return false;
    }
    if (branch.front() == '/' || branch.back() == '/' || branch.back() == '.') return false;
    if (branch.size() >= 5 && branch.compare(branch.size() - 5, 5, ".lock") == 0) return false;
    static const char* kForbidden[] = {"..", "@{", "\\", "~", "^", ":", "?", "*", "["};
    for (const char* token : kForbidden) {
        if (branch.find(token) != std::string::npos) return false;
    }
    return true;
}

bool isSafeGitRemote(const std::string& remote) {
    return !remote.empty() && remote.front() != '-' && !containsControlCharacter(remote);
}

bool hasOriginRemote(const std::string& repo_dir) {
    ProcessResult res = runProcess({"git", "remote", "get-url", "origin"}, repo_dir, 5000);
    return res.spawn_error.empty() && res.exit_code == 0 && !trim(res.stdout_text).empty();
}

std::string gitRevParse(const std::string& repo_dir, const std::string& rev) {
    ProcessResult res = runProcess({"git", "rev-parse", "--verify", rev}, repo_dir, 5000);
    if (!res.spawn_error.empty() || res.exit_code != 0) return "";
    return trim(res.stdout_text);
}

bool parseAheadBehind(const std::string& repo_dir,
                      const std::string& remote_ref,
                      int& out_behind,
                      int& out_ahead) {
    out_behind = 0;
    out_ahead = 0;
    ProcessResult res =
        runProcess({"git", "rev-list", "--left-right", "--count", remote_ref + "...HEAD"}, repo_dir, 10000);
    if (!res.spawn_error.empty() || res.exit_code != 0) return false;
    std::istringstream iss(res.stdout_text);
    int behind = 0;
    int ahead = 0;
    if (!(iss >> behind >> ahead)) return false;
    out_behind = behind;
    out_ahead = ahead;
    return true;
}

std::vector<std::string> filterRepoRelativePaths(const std::string& repo_dir,
                                                 const std::vector<std::string>& check_paths) {
    std::vector<std::string> out;
    if (repo_dir.empty() || check_paths.empty()) return out;
    std::error_code ec;
    const fs::path base = fs::weakly_canonical(repo_dir, ec);
    if (ec || base.empty()) return out;
    for (const auto& raw : check_paths) {
        if (raw.empty()) continue;
        const fs::path full = fs::weakly_canonical(raw, ec);
        if (ec || full.empty()) continue;
        const fs::path rel = fs::relative(full, base, ec);
        if (ec || rel.empty()) continue;
        const std::string rel_str = rel.generic_string();
        if (rel_str == "." || startsWith(rel_str, "../") || rel_str == "..") continue;
        out.push_back(rel_str);
    }
    return out;
}

} // namespace

nlohmann::json RepoSyncStatus::toJson() const {
    return {
        {"path", path},
        {"exists", exists},
        {"is_git_repo", is_git_repo},
        {"has_remote", has_remote},
        {"cloned", cloned},
        {"pulled", pulled},
        {"dirty_worktree_preserved", dirty_worktree_preserved},
        {"ahead_count", ahead_count},
        {"behind_count", behind_count},
        {"head_sha", head_sha},
        {"remote_head_sha", remote_head_sha},
        {"error", error},
        {"status_detail", status_detail},
    };
}

nlohmann::json RepoPushStatus::toJson() const {
    return {
        {"path", path},
        {"exists", exists},
        {"is_git_repo", is_git_repo},
        {"has_remote", has_remote},
        {"pushed", pushed},
        {"already_synced", already_synced},
        {"has_uncommitted_changes", has_uncommitted_changes},
        {"has_uncommitted_targets", has_uncommitted_targets},
        {"ahead_count", ahead_count},
        {"behind_count", behind_count},
        {"head_sha", head_sha},
        {"remote_head_sha", remote_head_sha},
        {"ok", ok()},
        {"error", error},
        {"status_detail", status_detail},
    };
}

nlohmann::json BidirectionalSyncStatus::toJson() const {
    return {
        {"ok", ok()},
        {"pull", pull.toJson()},
        {"push", push.toJson()},
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
                                   bool pull_remote,
                                   const std::string& branch) {
    RepoSyncStatus st;
    st.path = repo_dir;
    std::error_code ec;
    const std::string target_branch = branch.empty() ? "main" : branch;
    if (!isSafeGitBranch(target_branch)) {
        st.error = "Invalid Git branch name: " + target_branch;
        st.status_detail = "invalid_branch";
        return st;
    }

    if (!fs::exists(repo_dir, ec)) {
        st.exists = false;
        if (!clone_if_missing) {
            st.status_detail = "missing_skip_clone";
            return st;
        }
        if (!isSafeGitRemote(remote_url)) {
            st.error = "Invalid Git remote URL";
            st.status_detail = "invalid_remote_url";
            return st;
        }
        fs::path parent = fs::path(repo_dir).parent_path();
        if (!parent.empty()) {
            fs::create_directories(parent, ec);
        }
        ProcessResult clone_res = runProcess(
            {"git", "clone", "--branch", target_branch, "--", remote_url, repo_dir}, "", 120000);
        if (!clone_res.spawn_error.empty() || clone_res.exit_code != 0) {
            st.error = clone_res.spawn_error.empty()
                           ? trim(clone_res.stderr_text + " " + clone_res.stdout_text)
                           : clone_res.spawn_error;
            st.status_detail = "clone_failed";
            return st;
        }
        st.exists = true;
        st.is_git_repo = fs::exists(fs::path(repo_dir) / ".git", ec);
        st.has_remote = true;
        st.cloned = true;
        st.head_sha = gitRevParse(repo_dir, "HEAD");
        st.remote_head_sha = gitRevParse(repo_dir, "origin/" + target_branch);
        st.status_detail = "cloned";
        return st;
    }

    st.exists = true;
    st.is_git_repo = fs::exists(fs::path(repo_dir) / ".git", ec);
    if (st.is_git_repo) {
        st.head_sha = gitRevParse(repo_dir, "HEAD");
        st.has_remote = hasOriginRemote(repo_dir);
    }
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

    if (!st.has_remote) {
        st.status_detail = "no_remote_origin";
        return st;
    }

    ProcessResult fetch_res =
        runProcess({"git", "fetch", "--", "origin", target_branch}, repo_dir, 60000);
    if (fetch_res.exit_code != 0) {
        st.error = trim(fetch_res.stderr_text + " " + fetch_res.stdout_text);
        st.status_detail = "git_fetch_failed";
        return st;
    }

    const std::string remote_ref = "origin/" + target_branch;
    st.remote_head_sha = gitRevParse(repo_dir, remote_ref);
    if (parseAheadBehind(repo_dir, remote_ref, st.behind_count, st.ahead_count)) {
        if (st.behind_count == 0) {
            st.pulled = false;
            st.status_detail = (st.ahead_count > 0) ? "local_ahead" : "already_up_to_date";
            return st;
        }
        if (st.ahead_count > 0 && st.behind_count > 0) {
            st.error = "Local branch and " + remote_ref + " have diverged";
            st.status_detail = "diverged_from_origin";
            return st;
        }
    }

    ProcessResult merge_res =
        runProcess({"git", "merge", "--ff-only", "--", remote_ref}, repo_dir, 30000);
    if (merge_res.exit_code != 0) {
        st.error = trim(merge_res.stderr_text + " " + merge_res.stdout_text);
        st.status_detail = "git_merge_ff_only_failed";
        return st;
    }
    st.head_sha = gitRevParse(repo_dir, "HEAD");
    st.behind_count = 0;
    st.pulled = true;
    st.status_detail = "ff_merged";
    return st;
}

RepoPushStatus GitMirror::pushRepo(const std::string& repo_dir,
                                   const std::string& branch,
                                   const std::vector<std::string>& check_paths) {
    RepoPushStatus st;
    st.path = repo_dir;
    std::error_code ec;
    const std::string target_branch = branch.empty() ? "main" : branch;
    if (!isSafeGitBranch(target_branch)) {
        st.error = "Invalid Git branch name: " + target_branch;
        st.status_detail = "invalid_branch";
        return st;
    }

    if (repo_dir.empty() || !fs::exists(repo_dir, ec)) {
        st.exists = false;
        st.status_detail = "missing";
        return st;
    }

    st.exists = true;
    st.is_git_repo = fs::exists(fs::path(repo_dir) / ".git", ec);
    if (!st.is_git_repo) {
        st.status_detail = "non_git_directory";
        return st;
    }

    st.head_sha = gitRevParse(repo_dir, "HEAD");
    st.has_remote = hasOriginRemote(repo_dir);

    ProcessResult status_res = runProcess({"git", "status", "--porcelain"}, repo_dir, 15000);
    if (status_res.exit_code == 0) {
        st.has_uncommitted_changes = !trim(status_res.stdout_text).empty();
    }

    const auto rel_targets = filterRepoRelativePaths(repo_dir, check_paths);
    if (!rel_targets.empty()) {
        std::vector<std::string> cmd = {"git", "status", "--porcelain", "--"};
        cmd.insert(cmd.end(), rel_targets.begin(), rel_targets.end());
        ProcessResult target_res = runProcess(cmd, repo_dir, 10000);
        if (target_res.exit_code == 0) {
            st.has_uncommitted_targets = !trim(target_res.stdout_text).empty();
        }
    }

    if (!st.has_remote) {
        st.already_synced = true;
        st.status_detail = "no_remote_origin";
        return st;
    }

    ProcessResult branch_res = runProcess({"git", "symbolic-ref", "--short", "HEAD"}, repo_dir, 5000);
    const std::string cur_branch = trim(branch_res.stdout_text);
    if (branch_res.exit_code != 0 || cur_branch != target_branch) {
        st.error = "Current branch is '" + (cur_branch.empty() ? "HEAD" : cur_branch) +
                   "', expected '" + target_branch + "'";
        st.status_detail = "not_on_target_branch";
        return st;
    }

    ProcessResult fetch_res =
        runProcess({"git", "fetch", "--", "origin", target_branch}, repo_dir, 60000);
    if (fetch_res.exit_code != 0) {
        st.error = trim(fetch_res.stderr_text + " " + fetch_res.stdout_text);
        st.status_detail = "git_fetch_failed";
        return st;
    }

    const std::string remote_ref = "origin/" + target_branch;
    st.remote_head_sha = gitRevParse(repo_dir, remote_ref);
    if (!parseAheadBehind(repo_dir, remote_ref, st.behind_count, st.ahead_count)) {
        // Remote branch might not exist yet on empty remote; attempt normal push
        st.ahead_count = 1;
        st.behind_count = 0;
    }

    if (st.behind_count > 0) {
        st.error = (st.ahead_count > 0)
                       ? ("Local '" + target_branch + "' and '" + remote_ref + "' have diverged")
                       : ("Local '" + target_branch + "' is behind '" + remote_ref + "'");
        st.status_detail = (st.ahead_count > 0) ? "diverged_from_origin" : "behind_origin";
        return st;
    }

    if (st.ahead_count > 0) {
        ProcessResult push_res =
            runProcess({"git", "push", "--", "origin", target_branch + ":" + target_branch},
                       repo_dir,
                       90000);
        if (!push_res.spawn_error.empty() || push_res.exit_code != 0) {
            st.error = push_res.spawn_error.empty()
                           ? trim(push_res.stderr_text + " " + push_res.stdout_text)
                           : push_res.spawn_error;
            st.status_detail = "git_push_failed";
            return st;
        }
        st.pushed = true;
        st.ahead_count = 0;
        st.head_sha = gitRevParse(repo_dir, "HEAD");
        st.remote_head_sha = gitRevParse(repo_dir, remote_ref);
        if (st.has_uncommitted_targets) {
            st.error = "已推送本地 commit，但今日部分目标文件仍未 commit";
            st.status_detail = "pushed_with_uncommitted_targets";
        } else {
            st.status_detail = "pushed";
        }
        return st;
    }

    if (st.has_uncommitted_targets) {
        st.already_synced = false;
        st.error = "今日目标文件已写入本地工作区但尚未 git commit，无法推送至 GitHub";
        st.status_detail = "uncommitted_targets";
    } else {
        st.already_synced = true;
        st.status_detail = "already_synced";
    }
    return st;
}

BidirectionalSyncStatus GitMirror::syncBidirectional(const std::string& repo_dir,
                                                     const std::string& remote_url,
                                                     bool clone_if_missing,
                                                     const std::string& branch) {
    BidirectionalSyncStatus out;
    out.pull = syncRepo(repo_dir, remote_url, clone_if_missing, true, branch);
    if (out.pull.exists && out.pull.is_git_repo && out.pull.error.empty()) {
        out.push = pushRepo(repo_dir, branch);
    } else {
        out.push.path = repo_dir;
        out.push.exists = out.pull.exists;
        out.push.is_git_repo = out.pull.is_git_repo;
        out.push.has_remote = out.pull.has_remote;
        out.push.status_detail = "skipped_due_to_pull_state";
    }
    return out;
}

} // namespace ai_curator
