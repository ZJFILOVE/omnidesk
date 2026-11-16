#include "music/music_engine.hpp"
#include "music/config.hpp"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <ctime>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <poll.h>
#include <regex>
#include <set>
#include <signal.h>
#include <sstream>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <unordered_map>
#include <unordered_set>

namespace fs = std::filesystem;

namespace music_plugin {
namespace {

constexpr size_t kMaxFileBytes = 8 * 1024 * 1024;
constexpr const char* kDefaultInteractivePlaylist = "OmniDesk AI 灵感电台";

std::string trim(std::string value) {
    size_t begin = 0;
    while (begin < value.size() && std::isspace(static_cast<unsigned char>(value[begin]))) ++begin;
    size_t end = value.size();
    while (end > begin && std::isspace(static_cast<unsigned char>(value[end - 1]))) --end;
    return value.substr(begin, end - begin);
}

std::string lower(std::string value) {
    for (char& ch : value) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return value;
}

bool isNumericId(const std::string& value) {
    return !value.empty() && std::all_of(value.begin(), value.end(), [](unsigned char ch) {
        return std::isdigit(ch) != 0;
    });
}

bool isSafeId(const std::string& value) {
    return !value.empty() && std::all_of(value.begin(), value.end(), [](unsigned char ch) {
        return std::isalnum(ch) || ch == '.' || ch == '_' || ch == '-';
    });
}

bool isIsoDate(const std::string& date) {
    if (!std::regex_match(date, std::regex(R"(20[0-9]{2}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01]))"))) {
        return false;
    }
    std::tm tm{};
    tm.tm_year = std::stoi(date.substr(0, 4)) - 1900;
    tm.tm_mon = std::stoi(date.substr(5, 2)) - 1;
    tm.tm_mday = std::stoi(date.substr(8, 2));
    tm.tm_hour = 12;
    auto copy = tm;
    return timegm(&copy) != -1 && copy.tm_year == tm.tm_year &&
           copy.tm_mon == tm.tm_mon && copy.tm_mday == tm.tm_mday;
}

std::string shanghaiToday() {
    std::time_t shanghai = std::time(nullptr) + 8 * 3600;
    std::tm tm{};
    gmtime_r(&shanghai, &tm);
    char buffer[16];
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%d", &tm);
    return buffer;
}

std::string utcNow() {
    std::time_t now = std::time(nullptr);
    std::tm tm{};
    gmtime_r(&now, &tm);
    char buffer[32];
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &tm);
    return buffer;
}

bool isProtectedPlaylist(const std::string& id, const std::string& name, const nlohmann::json& daily_state) {
    const std::string clean_id = trim(id);
    if (clean_id == MusicEngine::kProtectedDailyPlaylistId ||
        clean_id == daily_state.value("cumulative_playlist_id", std::string(MusicEngine::kProtectedDailyPlaylistId))) {
        return true;
    }
    if (daily_state.contains("dated_playlists") && daily_state["dated_playlists"].is_object()) {
        for (auto it = daily_state["dated_playlists"].begin(); it != daily_state["dated_playlists"].end(); ++it) {
            if (!it.value().is_object()) continue;
            if (!clean_id.empty() &&
                (clean_id == it.value().value("playlist_id", "") ||
                 clean_id == it.value().value("apple_music_playlist_id", ""))) {
                return true;
            }
        }
    }
    std::string folded;
    for (unsigned char ch : lower(trim(name))) {
        if (std::isalnum(ch)) folded.push_back(static_cast<char>(ch));
    }
    return folded.find("chatgptdaily5") != std::string::npos ||
           lower(trim(name)).rfind("chatgpt daily 5", 0) == 0;
}

fs::path resolveDataRoot(const std::string& data_dir) {
    if (data_dir.empty()) return fs::path(expandUserPath("~/.local/share/omnidesk"));
    fs::path path(data_dir);
    return (path.has_parent_path() && path.parent_path().filename() == "plugin-data")
               ? path.parent_path().parent_path()
               : path;
}

fs::path resolveRepoDir(const std::string& configured_dir,
                        const fs::path& data_root,
                        const char* name) {
    std::error_code ec;
    if (!configured_dir.empty()) {
        return fs::path(expandUserPath(configured_dir));
    }
    for (const fs::path& candidate : {
             data_root / "curation" / "repos" / name,
             fs::path("/home/zhu/Projects") / name,
             fs::path(expandUserPath("~/.local/share/omnidesk/curation/repos")) / name,
         }) {
        if (fs::exists(candidate, ec) && fs::is_directory(candidate, ec)) return candidate;
    }
    return {};
}

class ScopedFlock {
public:
    explicit ScopedFlock(const fs::path& path) {
        std::error_code ec;
        fs::create_directories(path.parent_path(), ec);
        ::chmod(path.parent_path().c_str(), 0700);
        fd_ = ::open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd_ < 0) return;
        struct stat st {};
        if (::fstat(fd_, &st) != 0 || !S_ISREG(st.st_mode) || st.st_uid != ::geteuid()) return;
        ::fchmod(fd_, 0600);
        locked_ = ::flock(fd_, LOCK_EX | LOCK_NB) == 0;
    }
    ~ScopedFlock() {
        if (fd_ >= 0) {
            if (locked_) ::flock(fd_, LOCK_UN);
            ::close(fd_);
        }
    }
    bool locked() const { return locked_; }

private:
    int fd_ = -1;
    bool locked_ = false;
};

bool readPrivateBytes(const fs::path& path, std::string& out, bool require_mode_0600, std::string& error) {
    out.clear();
    int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        error = std::strerror(errno);
        return false;
    }
    struct stat st {};
    if (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_uid != ::geteuid() ||
        st.st_size < 0 || static_cast<size_t>(st.st_size) > kMaxFileBytes ||
        (require_mode_0600 && ((st.st_mode & 0777) != 0600)) ||
        (!require_mode_0600 && ((st.st_mode & 0022) != 0))) {
        ::close(fd);
        error = "Unsafe or oversized file: " + path.string();
        return false;
    }
    out.resize(static_cast<size_t>(st.st_size));
    size_t total = 0;
    while (total < out.size()) {
        ssize_t count = ::read(fd, out.data() + total, out.size() - total);
        if (count <= 0) {
            if (count < 0 && errno == EINTR) continue;
            ::close(fd);
            error = "Short read: " + path.string();
            return false;
        }
        total += static_cast<size_t>(count);
    }
    ::close(fd);
    return true;
}

bool readStateObject(const fs::path& path, nlohmann::json& out, std::string& error) {
    std::error_code ec;
    if (!fs::exists(path, ec)) {
        out = nlohmann::json::object();
        return true;
    }
    std::string raw;
    if (!readPrivateBytes(path, raw, true, error)) return false;
    out = nlohmann::json::parse(raw, nullptr, false);
    if (!out.is_object()) {
        error = "Corrupt JSON state file: " + path.string();
        return false;
    }
    return true;
}

bool writeStateObject(const fs::path& path, const nlohmann::json& state, std::string& error) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    ::chmod(path.parent_path().c_str(), 0700);
    fs::path tmp = path;
    tmp += ".tmp." + std::to_string(::getpid());
    ::unlink(tmp.c_str());
    int fd = ::open(tmp.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        error = "Cannot create temporary state file";
        return false;
    }
    ::fchmod(fd, 0600);
    const std::string data = state.dump(2) + "\n";
    size_t written = 0;
    while (written < data.size()) {
        ssize_t count = ::write(fd, data.data() + written, data.size() - written);
        if (count <= 0) {
            if (count < 0 && errno == EINTR) continue;
            ::close(fd);
            ::unlink(tmp.c_str());
            error = "Failed writing state file";
            return false;
        }
        written += static_cast<size_t>(count);
    }
    if (::fsync(fd) != 0) {
        ::close(fd);
        ::unlink(tmp.c_str());
        error = "Failed syncing state file";
        return false;
    }
    ::close(fd);
    fs::rename(tmp, path, ec);
    if (ec) {
        ::unlink(tmp.c_str());
        error = ec.message();
        return false;
    }
    ::chmod(path.c_str(), 0600);
    int dir_fd = ::open(path.parent_path().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (dir_fd >= 0) {
        ::fsync(dir_fd);
        ::close(dir_fd);
    }
    return true;
}

std::string gitBlobSha(const std::string& content) {
    return sha1Hex("blob " + std::to_string(content.size()) + std::string("\0", 1) + content);
}

std::string sanitizeRuntimeMessage(std::string msg, const std::vector<std::string>& secrets) {
    for (const auto& s : secrets) {
        if (s.size() >= 4) {
            size_t pos = 0;
            while ((pos = msg.find(s, pos)) != std::string::npos) {
                msg.replace(pos, s.size(), "[REDACTED]");
                pos += 10;
            }
        }
    }
    msg = std::regex_replace(msg, std::regex(R"(Bearer\s+[A-Za-z0-9._\-+/=]+)", std::regex::icase), "Bearer [REDACTED]");
    msg = std::regex_replace(msg, std::regex(R"(media-user-token=[^;\s"']+)"), "media-user-token=[REDACTED]");
    if (msg.size() > 512) msg.resize(512);
    return trim(msg);
}

fs::path resolveWritebackWorkerScript(const omnidesk::PluginContext& ctx) {
    std::error_code ec;
    if (const char* override_path = std::getenv("OMNIDESK_MUSIC_WRITEBACK_WORKER"); override_path && *override_path) {
        fs::path p(override_path);
        if (fs::exists(p, ec) && fs::is_regular_file(p, ec)) return p;
    }
    if (!ctx.pluginDir().empty()) {
        fs::path p = fs::path(ctx.pluginDir()) / "workers" / "apple_music_writeback.py";
        if (fs::exists(p, ec) && fs::is_regular_file(p, ec)) return p;
    }
#ifdef OMNIDESK_MUSIC_SOURCE_DIR
    {
        fs::path p = fs::path(OMNIDESK_MUSIC_SOURCE_DIR) / "workers" / "apple_music_writeback.py";
        if (fs::exists(p, ec) && fs::is_regular_file(p, ec)) return p;
    }
#endif
    return {};
}

bool invokePythonWritebackWorker(const omnidesk::PluginContext& ctx,
                                 const fs::path& cwd,
                                 const nlohmann::json& payload,
                                 const std::vector<std::string>& secrets,
                                 const std::shared_ptr<omnidesk::CancelToken>& cancel,
                                 nlohmann::json& data_out,
                                 std::string& error_code_out,
                                 std::string& error_msg_out,
                                 nlohmann::json& extra_out,
                                 int timeout_ms = 60000) {
    data_out = nlohmann::json::object();
    extra_out = nlohmann::json::object();
    error_code_out.clear();
    error_msg_out.clear();

    if (cancel && cancel->isCancelled()) {
        error_code_out = "cancelled";
        error_msg_out = "Operation cancelled";
        return false;
    }
    const fs::path script = resolveWritebackWorkerScript(ctx);
    if (script.empty() || !fs::exists("/usr/bin/python3")) {
        error_code_out = "worker_unavailable";
        error_msg_out = "Apple Music Python write-back worker is unavailable";
        return false;
    }

    int in_pipe[2]{-1, -1};
    int out_pipe[2]{-1, -1};
    int err_pipe[2]{-1, -1};
    if (::pipe(in_pipe) != 0 || ::pipe(out_pipe) != 0 || ::pipe(err_pipe) != 0) {
        for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) {
            if (fd >= 0) ::close(fd);
        }
        error_code_out = "worker_spawn_failed";
        error_msg_out = "Failed creating IPC pipes for Python write-back worker";
        return false;
    }

    pid_t pid = ::fork();
    if (pid < 0) {
        for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) {
            if (fd >= 0) ::close(fd);
        }
        error_code_out = "worker_spawn_failed";
        error_msg_out = "Failed forking Python write-back worker";
        return false;
    }
    if (pid == 0) {
        ::setpgid(0, 0);
        if (!cwd.empty()) {
            std::error_code ec;
            if (fs::exists(cwd, ec) && ::chdir(cwd.c_str()) != 0) ::_exit(126);
        }
        ::dup2(in_pipe[0], STDIN_FILENO);
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::dup2(err_pipe[1], STDERR_FILENO);
        for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) {
            if (fd >= 0) ::close(fd);
        }
        std::string py = "/usr/bin/python3";
        std::string sc = script.string();
        std::string flag = "--stdin-rpc";
        char* const argv[] = {py.data(), sc.data(), flag.data(), nullptr};
        ::execv(py.c_str(), argv);
        ::_exit(127);
    }

    ::close(in_pipe[0]);
    ::close(out_pipe[1]);
    ::close(err_pipe[1]);

    for (int fd : {in_pipe[1], out_pipe[0], err_pipe[0]}) {
        int fl = ::fcntl(fd, F_GETFL, 0);
        if (fl >= 0) ::fcntl(fd, F_SETFL, fl | O_NONBLOCK);
    }

    std::string wire = payload.dump() + "\n";
    size_t in_off = 0;
    std::string stdout_buf;
    std::string stderr_buf;
    auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    bool child_done = false;
    int status = 0;

    auto kill_child = [&]() {
        if (!child_done && pid > 0) {
            ::kill(-pid, SIGKILL);
            ::kill(pid, SIGKILL);
            ::waitpid(pid, &status, 0);
            child_done = true;
        }
        if (in_pipe[1] >= 0) { ::close(in_pipe[1]); in_pipe[1] = -1; }
        if (out_pipe[0] >= 0) { ::close(out_pipe[0]); out_pipe[0] = -1; }
        if (err_pipe[0] >= 0) { ::close(err_pipe[0]); err_pipe[0] = -1; }
    };

    while (in_pipe[1] >= 0 || out_pipe[0] >= 0 || err_pipe[0] >= 0 || !child_done) {
        if (cancel && cancel->isCancelled()) {
            kill_child();
            error_code_out = "cancelled";
            error_msg_out = "Operation cancelled";
            return false;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            kill_child();
            error_code_out = "worker_timeout";
            error_msg_out = "Apple Music Python write-back worker timed out";
            return false;
        }

        if (!child_done) {
            pid_t w = ::waitpid(pid, &status, WNOHANG);
            if (w == pid) child_done = true;
        }

        struct pollfd pfds[3]{};
        nfds_t nfds = 0;
        int in_idx = -1, out_idx = -1, err_idx = -1;
        if (in_pipe[1] >= 0) {
            in_idx = static_cast<int>(nfds);
            pfds[nfds++] = {in_pipe[1], POLLOUT, 0};
        }
        if (out_pipe[0] >= 0) {
            out_idx = static_cast<int>(nfds);
            pfds[nfds++] = {out_pipe[0], POLLIN, 0};
        }
        if (err_pipe[0] >= 0) {
            err_idx = static_cast<int>(nfds);
            pfds[nfds++] = {err_pipe[0], POLLIN, 0};
        }

        if (nfds > 0) {
            ::poll(pfds, nfds, 40);
        } else if (!child_done) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }

        if (in_idx >= 0 && (pfds[in_idx].revents & (POLLOUT | POLLERR | POLLHUP))) {
            ssize_t n = ::write(in_pipe[1], wire.data() + in_off, wire.size() - in_off);
            if (n > 0) {
                in_off += static_cast<size_t>(n);
                if (in_off >= wire.size()) {
                    ::close(in_pipe[1]);
                    in_pipe[1] = -1;
                }
            } else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                ::close(in_pipe[1]);
                in_pipe[1] = -1;
            }
        }

        auto drain_nb = [](int& fd, std::string& dst) {
            char buf[4096];
            while (fd >= 0 && dst.size() < kMaxFileBytes) {
                ssize_t n = ::read(fd, buf, sizeof(buf));
                if (n > 0) {
                    dst.append(buf, static_cast<size_t>(n));
                } else if (n == 0) {
                    ::close(fd);
                    fd = -1;
                    break;
                } else {
                    if (errno == EINTR) continue;
                    if (errno != EAGAIN && errno != EWOULDBLOCK) {
                        ::close(fd);
                        fd = -1;
                    }
                    break;
                }
            }
        };
        if (out_idx >= 0 && (pfds[out_idx].revents & (POLLIN | POLLHUP | POLLERR))) drain_nb(out_pipe[0], stdout_buf);
        if (err_idx >= 0 && (pfds[err_idx].revents & (POLLIN | POLLHUP | POLLERR))) drain_nb(err_pipe[0], stderr_buf);

        if (child_done && out_pipe[0] < 0 && err_pipe[0] < 0) {
            if (in_pipe[1] >= 0) { ::close(in_pipe[1]); in_pipe[1] = -1; }
            break;
        }
    }
    std::fill(wire.begin(), wire.end(), '\0');

    auto resp = nlohmann::json::parse(stdout_buf, nullptr, false);
    if (resp.is_object() && resp.contains("ok") && resp["ok"].is_boolean()) {
        if (resp["ok"].get<bool>()) {
            data_out = resp.value("data", nlohmann::json::object());
            return true;
        }
        error_code_out = resp.value("error_code", "python_writeback_failed");
        error_msg_out = sanitizeRuntimeMessage(resp.value("error", "Apple Music Python write-back failed"), secrets);
        if (resp.contains("extra") && resp["extra"].is_object()) {
            extra_out = resp["extra"];
        }
        return false;
    }

    error_code_out = "python_writeback_failed";
    std::string raw_err = trim(stderr_buf).empty() ? trim(stdout_buf) : trim(stderr_buf);
    error_msg_out = sanitizeRuntimeMessage(
        raw_err.empty() ? "Apple Music Python write-back worker exited unexpectedly" : raw_err, secrets);
    return false;
}

bool runProcess(const fs::path& cwd, const std::vector<std::string>& args, std::string& stdout_out, std::string& error) {
    stdout_out.clear();
    int out_pipe[2]{-1, -1};
    int err_pipe[2]{-1, -1};
    if (::pipe(out_pipe) != 0 || ::pipe(err_pipe) != 0) {
        if (out_pipe[0] >= 0) { ::close(out_pipe[0]); ::close(out_pipe[1]); }
        error = "pipe failed";
        return false;
    }
    pid_t pid = ::fork();
    if (pid < 0) {
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        error = "fork failed";
        return false;
    }
    if (pid == 0) {
        if (!cwd.empty() && ::chdir(cwd.c_str()) != 0) ::_exit(126);
        int null_fd = ::open("/dev/null", O_RDONLY);
        if (null_fd >= 0) { ::dup2(null_fd, STDIN_FILENO); ::close(null_fd); }
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::dup2(err_pipe[1], STDERR_FILENO);
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        std::vector<char*> argv;
        for (const auto& arg : args) argv.push_back(const_cast<char*>(arg.c_str()));
        argv.push_back(nullptr);
        ::execv(argv[0], argv.data());
        ::_exit(127);
    }
    ::close(out_pipe[1]);
    ::close(err_pipe[1]);
    auto drain = [](int fd, std::string& target) {
        char buffer[4096];
        while (target.size() < kMaxFileBytes) {
            ssize_t count = ::read(fd, buffer, sizeof(buffer));
            if (count <= 0) break;
            target.append(buffer, static_cast<size_t>(count));
        }
        ::close(fd);
    };
    std::string stderr_out;
    drain(out_pipe[0], stdout_out);
    drain(err_pipe[0], stderr_out);
    int status = 0;
    ::waitpid(pid, &status, 0);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        error = trim(stderr_out).empty() ? trim(stdout_out) : trim(stderr_out);
        return false;
    }
    return true;
}

struct ManifestDay {
    std::string date;
    std::string rel_path;
    std::string raw;
    std::vector<std::string> song_ids;
    std::vector<std::string> canonical_ids;
    std::vector<MusicTrack> tracks;
    int declared_track_count = 0;
    bool complete_five = true;
    std::string anomaly;
};

bool parseManifestDay(const std::string& raw, const std::string& expected_date,
                      const std::string& rel_path, ManifestDay& out, std::string& error) {
    if (!isIsoDate(expected_date) || expected_date > shanghaiToday()) {
        error = "Invalid, withdrawn, or future recommendation date: " + expected_date;
        return false;
    }
    auto manifest = nlohmann::json::parse(raw, nullptr, false);
    const std::size_t actual_count =
        manifest.is_object() && manifest.contains("tracks") && manifest["tracks"].is_array()
            ? manifest["tracks"].size()
            : 0;
    const bool known_partial = expected_date == "2026-11-06" && actual_count == 4;
    if (!manifest.is_object() || manifest.value("schema_version", 0) != 1 ||
        manifest.value("provider", "") != "apple_music" ||
        manifest.value("date", "") != expected_date ||
        !manifest.contains("tracks") || !manifest["tracks"].is_array() ||
        (actual_count != 5 && !known_partial)) {
        error = "Invalid five-track manifest for " + expected_date +
                " (committed track count=" + std::to_string(actual_count) + ")";
        return false;
    }
    out = {expected_date, rel_path, raw, {}, {}, {},
           static_cast<int>(actual_count), actual_count == 5,
           known_partial
               ? "known historical anomaly: the committed manifest contains 4 tracks instead of 5"
               : ""};
    std::unordered_set<std::string> seen_songs;
    std::unordered_set<std::string> seen_canonicals;
    for (int index = 0; index < static_cast<int>(actual_count); ++index) {
        const auto& item = manifest["tracks"][index];
        if (!item.is_object() || !item.contains("position") || !item["position"].is_number_integer() ||
            item["position"].get<int>() != index + 1 ||
            !item.contains("apple_music") || !item["apple_music"].is_object()) {
            error = "Manifest tracks must be ordered 1..N for " + expected_date;
            return false;
        }
        const auto& am = item["apple_music"];
        const std::string song_id = am.value("song_id", "");
        const std::string sf = am.value("storefront", "");
        const std::string url = am.value("url", "");
        const std::string title = item.value("title", "");
        const std::string artist = item.value("artist", "");
        const std::string canonical = item.value("canonical_id", "");
        if (!isNumericId(song_id) || sf != "cn" ||
            url != ("https://music.apple.com/cn/song/" + song_id) ||
            title.empty() || artist.empty() || canonical != (artist + "::" + title) ||
            item.value("analysis_summary", "").empty() || item.value("listening_cue", "").empty() ||
            !seen_songs.insert(song_id).second || !seen_canonicals.insert(canonical).second) {
            error = "Invalid recording metadata in manifest for " + expected_date;
            return false;
        }
        MusicTrack track;
        track.id = song_id;
        track.title = title;
        track.artist = artist;
        if (item.contains("album") && item["album"].is_string()) track.album = item["album"].get<std::string>();
        track.storefront = "cn";
        track.quality_label = "Apple Music CN · ALAC";
        out.song_ids.push_back(song_id);
        out.canonical_ids.push_back(canonical);
        out.tracks.push_back(std::move(track));
    }
    return true;
}

bool verifyCleanGitRepo(const fs::path& repo, std::string& head_out, std::string& error) {
    std::string out;
    if (!runProcess(repo, {"/usr/bin/git", "rev-parse", "HEAD"}, out, error)) return false;
    head_out = trim(out);
    if (!std::regex_match(head_out, std::regex(R"([0-9a-f]{40})"))) {
        error = "Invalid Git HEAD for " + repo.string();
        return false;
    }
    if (!runProcess(repo, {"/usr/bin/git", "rev-parse", "--is-shallow-repository"}, out, error) ||
        trim(out) != "false") {
        error = "Shallow Git checkout cannot authorize sync: " + repo.string();
        return false;
    }
    if (!runProcess(repo, {"/usr/bin/git", "status", "--porcelain", "--untracked-files=all"}, out, error) ||
        !trim(out).empty()) {
        error = "Dirty Git checkout cannot authorize sync: " + repo.string();
        return false;
    }
    return true;
}

bool readCommittedFile(const fs::path& repo, const std::string& head,
                       const std::string& rel_path, std::string& raw_out, std::string& error) {
    std::string disk;
    if (!readPrivateBytes(repo / rel_path, disk, false, error)) return false;
    if (!runProcess(repo, {"/usr/bin/git", "show", head + ":" + rel_path}, raw_out, error) ||
        raw_out != disk) {
        error = "Committed artifact mismatch for " + rel_path;
        return false;
    }
    return true;
}

bool verifyVaultCurrentPublication(const fs::path& music_repo, const std::string& music_head,
                                   const fs::path& vault_repo, const ManifestDay& day,
                                   const std::string& music_state_raw, std::string& error,
                                   bool execute_gate) {
    std::string vault_head;
    if (vault_repo.empty() || !verifyCleanGitRepo(vault_repo, vault_head, error)) {
        error = "Clean obsidian-vault checkout required when local_sync_gate.py is unavailable";
        return false;
    }
    std::string vault_manifest_raw;
    std::string vault_state_raw;
    std::string note_raw;
    if (!readCommittedFile(vault_repo, vault_head, "Music/Daily/manifests/" + day.date + ".json", vault_manifest_raw, error) ||
        !readCommittedFile(vault_repo, vault_head, "Music/Daily/state/tracks.json", vault_state_raw, error) ||
        !readCommittedFile(vault_repo, vault_head, "Music/Daily/" + day.date + ".md", note_raw, error)) {
        return false;
    }
    if (vault_manifest_raw != day.raw || vault_state_raw != music_state_raw) {
        error = "Obsidian Vault mirror does not match music-daily";
        return false;
    }
    for (const auto& song_id : day.song_ids) {
        if (note_raw.find("https://music.apple.com/cn/song/" + song_id) == std::string::npos) {
            error = "Daily Markdown note is missing song URL for " + song_id;
            return false;
        }
    }
    const std::string bundle_rel = ".recommendation/transactions/music/" + day.date + ".json";
    std::string bundle_raw;
    std::string qa_raw;
    if (!readCommittedFile(vault_repo, vault_head, bundle_rel, bundle_raw, error) ||
        !readCommittedFile(vault_repo, vault_head, ".recommendation/runs/" + day.date + "-music.json", qa_raw, error)) {
        return false;
    }
    auto bundle = nlohmann::json::parse(bundle_raw, nullptr, false);
    auto qa = nlohmann::json::parse(qa_raw, nullptr, false);
    if (!bundle.is_object() || !qa.is_object() ||
        bundle.value("status", "") != "PUBLISHED" ||
        bundle.value("operation_id", "") != ("music:" + day.date) ||
        qa.value("status", "") != "passed_current_publication" ||
        qa.value("project", "") != "music" ||
        qa.value("date", "") != day.date ||
        qa.value("operation_id", "") != ("music:" + day.date) ||
        !qa.contains("bundle") || !qa["bundle"].is_object() ||
        qa["bundle"].value("path", "") != bundle_rel ||
        qa["bundle"].value("bundle_sha256", "") != bundle.value("bundle_sha256", "") ||
        qa["bundle"].value("plan_sha256", "") != bundle.value("plan_sha256", "")) {
        error = "Missing or mismatched PUBLISHED transaction bundle / QA receipt for " + day.date;
        return false;
    }
    if (execute_gate && fs::exists(music_repo / "scripts" / "publication_gate.py") &&
        fs::exists("/usr/bin/python3")) {
        std::string gate_out;
        return runProcess(music_repo, {
            "/usr/bin/python3", (music_repo / "scripts" / "publication_gate.py").string(),
            "--repo", music_repo.string(), "--vault-root", vault_repo.string(),
            "--date", day.date, "--music-head", music_head, "--vault-head", vault_head,
            "--bundle-digest", bundle.value("bundle_sha256", ""),
        }, gate_out, error);
    }
    const std::set<std::string> expected_keys = {
        "ZJFILOVE/music-daily:" + day.rel_path,
        "ZJFILOVE/music-daily:state/tracks.json",
        "ZJFILOVE/obsidian-vault:Music/Daily/" + day.date + ".md",
        "ZJFILOVE/obsidian-vault:Music/Daily/manifests/" + day.date + ".json",
        "ZJFILOVE/obsidian-vault:Music/Daily/state/tracks.json",
    };
    if (!bundle.contains("payloads") || !bundle["payloads"].is_object() ||
        !qa.contains("readbacks") || !qa["readbacks"].is_object()) {
        error = "Incomplete bundle payloads or QA readbacks";
        return false;
    }
    std::set<std::string> actual_keys;
    for (auto it = bundle["payloads"].begin(); it != bundle["payloads"].end(); ++it) actual_keys.insert(it.key());
    if (actual_keys != expected_keys) {
        error = "Unexpected transaction write scope";
        return false;
    }
    const std::unordered_map<std::string, std::string> raw_by_key = {
        {"ZJFILOVE/music-daily:" + day.rel_path, day.raw},
        {"ZJFILOVE/music-daily:state/tracks.json", music_state_raw},
        {"ZJFILOVE/obsidian-vault:Music/Daily/" + day.date + ".md", note_raw},
        {"ZJFILOVE/obsidian-vault:Music/Daily/manifests/" + day.date + ".json", vault_manifest_raw},
        {"ZJFILOVE/obsidian-vault:Music/Daily/state/tracks.json", vault_state_raw},
    };
    for (const auto& [key, expected_raw] : raw_by_key) {
        if (bundle["payloads"].value(key, "") != expected_raw ||
            !qa["readbacks"].contains(key) ||
            qa["readbacks"][key].value("sha", "") != gitBlobSha(expected_raw) ||
            qa["readbacks"][key].value("bytes", -1) != static_cast<int>(expected_raw.size())) {
            error = "Committed artifact hash does not match frozen bundle / QA readback: " + key;
            return false;
        }
    }
    return true;
}

bool authorizeDailyRepository(const nlohmann::json& req, const fs::path& music_repo,
                              const fs::path& vault_repo, bool sync_all,
                              std::vector<ManifestDay>& days_out,
                              std::vector<std::string>& targets_out,
                              std::string& music_head_out,
                              std::vector<std::string>& anomalies_out,
                              std::string& error, bool execute_gate = true) {
    days_out.clear();
    targets_out.clear();
    anomalies_out.clear();
    if (music_repo.empty() || !verifyCleanGitRepo(music_repo, music_head_out, error)) return false;
    std::string state_raw;
    if (!readCommittedFile(music_repo, music_head_out, "state/tracks.json", state_raw, error)) return false;
    auto state_json = nlohmann::json::parse(state_raw, nullptr, false);
    if (!state_json.is_object() || !state_json.contains("recommended") || !state_json["recommended"].is_object()) {
        error = "Invalid committed state/tracks.json";
        return false;
    }
    const auto& recommended = state_json["recommended"];
    std::string ls_out;
    if (!runProcess(music_repo, {"/usr/bin/git", "ls-tree", "-r", "--name-only", music_head_out, "recommendations"}, ls_out, error)) {
        return false;
    }
    std::vector<std::string> rel_paths;
    std::istringstream stream(ls_out);
    for (std::string line; std::getline(stream, line);) {
        line = trim(line);
        if (!line.empty()) rel_paths.push_back(line);
    }
    std::sort(rel_paths.begin(), rel_paths.end());
    std::unordered_map<std::string, ManifestDay> by_date;
    for (const auto& rel : rel_paths) {
        if (!std::regex_match(rel, std::regex(R"(recommendations/(20[0-9]{2})/([0-9]{2})/\1-\2-[0-9]{2}\.json)"))) {
            error = "Unexpected file under recommendations/: " + rel;
            return false;
        }
        const std::string date = fs::path(rel).stem().string();
        std::string raw;
        ManifestDay day;
        if (!readCommittedFile(music_repo, music_head_out, rel, raw, error) ||
            !parseManifestDay(raw, date, rel, day, error)) {
            return false;
        }
        if (!day.complete_five) {
            anomalies_out.push_back(day.date + ": " + day.anomaly);
        }
        for (const auto& canonical : day.canonical_ids) {
            if (!recommended.contains(canonical) || !recommended[canonical].is_number_integer() ||
                recommended[canonical].get<int>() < 1) {
                error = "state/tracks.json is missing canonical_id: " + canonical;
                return false;
            }
        }
        by_date.emplace(date, std::move(day));
    }
    if (by_date.empty()) {
        error = "No valid committed manifests in music-daily";
        return false;
    }
    if (!sync_all) {
        const std::string date = trim(req.value("business_date", ""));
        const std::string op = trim(req.value("operation_id", ""));
        if (!isIsoDate(date) || (!op.empty() && op != ("music:" + date))) {
            error = "Valid business_date and matching operation_id are required";
            return false;
        }
        auto found = by_date.find(date);
        if (found == by_date.end()) {
            error = "No committed manifest found for " + date;
            return false;
        }
        if (!req.contains("track_ids") || !req["track_ids"].is_array()) {
            error = "sync_daily_authorized requires explicit 5-song track_ids matching the committed manifest";
            return false;
        }
        std::vector<std::string> req_ids;
        for (const auto& item : req["track_ids"]) {
            if (!item.is_string()) {
                error = "track_ids must contain string song IDs";
                return false;
            }
            req_ids.push_back(trim(item.get<std::string>()));
        }
        if (req_ids != found->second.song_ids) {
            error = "Requested track_ids do not exactly match committed manifest for " + date;
            return false;
        }
        const fs::path gate = music_repo / "scripts" / "local_sync_gate.py";
        if (execute_gate && fs::exists(gate) && fs::exists("/usr/bin/python3")) {
            std::string gate_out;
            if (!runProcess(music_repo, {
                    "/usr/bin/python3", gate.string(), "--repo", music_repo.string(),
                    "--date", date, "--music-head", music_head_out,
                }, gate_out, error)) {
                return false;
            }
            auto auth = nlohmann::json::parse(gate_out, nullptr, false);
            if (!auth.is_object() || auth.value("date", "") != date ||
                auth.value("music_head", "") != music_head_out ||
                auth.value("manifest_blob", "") != gitBlobSha(found->second.raw) ||
                !auth.contains("song_ids") || auth["song_ids"] != nlohmann::json(req_ids)) {
                error = "local_sync_gate.py output did not authorize " + date;
                return false;
            }
        }
        if (!verifyVaultCurrentPublication(music_repo, music_head_out, vault_repo, found->second,
                                           state_raw, error, execute_gate)) {
            return false;
        }
        days_out.push_back(found->second);
        targets_out = found->second.song_ids;
        return true;
    }
    std::vector<std::string> dates;
    for (const auto& [date, _] : by_date) dates.push_back(date);
    std::sort(dates.rbegin(), dates.rend());
    const fs::path gate = music_repo / "scripts" / "local_sync_gate.py";
    if (execute_gate && fs::exists(gate) && fs::exists("/usr/bin/python3")) {
        std::string gate_out;
        if (!runProcess(music_repo, {
                "/usr/bin/python3", gate.string(), "--repo", music_repo.string(),
                "--date", dates.front(), "--music-head", music_head_out,
            }, gate_out, error)) {
            return false;
        }
    }
    if (!verifyVaultCurrentPublication(music_repo, music_head_out, vault_repo,
                                       by_date.at(dates.front()), state_raw, error, execute_gate)) {
        return false;
    }
    std::unordered_set<std::string> seen;
    for (const auto& date : dates) {
        const auto& day = by_date.at(date);
        days_out.push_back(day);
        for (const auto& sid : day.song_ids) {
            if (seen.insert(sid).second) targets_out.push_back(sid);
        }
    }
    return true;
}

bool verifyLivePlaylistContains(AdamClient& adam, const std::string& playlist_id,
                                const std::vector<std::string>& expected,
                                std::vector<std::string>& remote_out, std::string& error,
                                const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    for (int attempt = 0; attempt < 3; ++attempt) {
        if (cancel && cancel->isCancelled()) {
            error = "cancelled";
            return false;
        }
        if (adam.fetchLibraryPlaylistCatalogIdsFromAmp(playlist_id, remote_out, error, cancel)) {
            std::unordered_set<std::string> remote_set(remote_out.begin(), remote_out.end());
            if (std::all_of(expected.begin(), expected.end(), [&](const std::string& sid) {
                    return remote_set.count(sid) > 0;
                })) {
                return true;
            }
            error = "Live readback missing expected catalog song IDs";
        }
        if (attempt < 2) std::this_thread::sleep_for(std::chrono::milliseconds(150));
    }
    return false;
}

void normalizeDailyState(nlohmann::json& state) {
    if (!state.is_object()) state = nlohmann::json::object();
    state["schema_version"] = 2;
    if (!state.contains("cumulative_playlist_id") || !state["cumulative_playlist_id"].is_string() ||
        state["cumulative_playlist_id"].get<std::string>().empty()) {
        state["cumulative_playlist_id"] = MusicEngine::kProtectedDailyPlaylistId;
    }
    state["cumulative_playlist_name"] = MusicEngine::kProtectedDailyPlaylistName;
    for (const char* array_key : {"cumulative_track_ids", "pending_song_ids"}) {
        if (!state.contains(array_key) || !state[array_key].is_array()) {
            state[array_key] = nlohmann::json::array();
        }
    }
    for (const char* obj_key : {"synced_dates", "dated_playlists"}) {
        if (!state.contains(obj_key) || !state[obj_key].is_object()) {
            state[obj_key] = nlohmann::json::object();
        }
    }
}

std::unordered_set<std::string> jsonStringSet(const nlohmann::json& array) {
    std::unordered_set<std::string> out;
    if (array.is_array()) {
        for (const auto& item : array) {
            if (item.is_string() && isNumericId(item.get<std::string>())) {
                out.insert(item.get<std::string>());
            }
        }
    }
    return out;
}

} // namespace

omnidesk::CapabilityResult MusicEngine::handlePlaylistRpc(
    const nlohmann::json& req,
    const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    omnidesk::CapabilityResult res;
    res.provider_plugin_id = "plugin-music";
    auto fail = [&](const std::string& code, const std::string& message, nlohmann::json extra = nlohmann::json::object()) {
        res.ok = false;
        res.cancelled = (code == "cancelled") || (cancel && cancel->isCancelled());
        res.error = message;
        extra["error_code"] = res.cancelled ? "cancelled" : code;
        extra["cloud_synced"] = false;
        extra["live_apple_music_synced"] = false;
        res.data = std::move(extra);
        return res;
    };
    if (cancel && cancel->isCancelled()) return fail("cancelled", "Operation cancelled");
    if (!req.is_object()) return fail("invalid_argument", "media.playlist.v1 request must be a JSON object");

    const std::string action = req.value("action", "status");
    const MusicConfig music_config = currentConfig();
    const fs::path data_root = resolveDataRoot(music_config.data_dir);
    const fs::path daily_state_file = data_root / "curation" / "apple-music-playlist-state.json";
    const fs::path interactive_file = data_root / "curation" / "interactive" / "playlists.json";
    const fs::path music_repo = resolveRepoDir(music_config.music_daily_dir, data_root, "music-daily");
    const fs::path vault_repo = resolveRepoDir(music_config.obsidian_vault_dir, data_root, "obsidian-vault");

    if (action == "preflight_cn_catalog") {
        if (req.contains("allow_offline_catalog_fallback") &&
            (!req["allow_offline_catalog_fallback"].is_boolean() || req["allow_offline_catalog_fallback"].get<bool>())) {
            return fail("offline_catalog_fallback_forbidden",
                        "Historical/offline files are metadata only and cannot verify current CN playability");
        }
        if (!req.contains("track_ids") || !req["track_ids"].is_array() || req["track_ids"].empty()) {
            return fail("invalid_argument", "preflight_cn_catalog requires a non-empty track_ids array");
        }
        const std::string sf = lower(trim(req.value("storefront", "cn")));
        if (sf != "cn") return fail("invalid_storefront", "preflight_cn_catalog strictly requires storefront == 'cn'");
        std::vector<std::string> requested;
        std::unordered_set<std::string> seen;
        for (const auto& item : req["track_ids"]) {
            if (!item.is_string()) return fail("invalid_track_id", "track_ids must contain numeric strings");
            std::string sid = trim(item.get<std::string>());
            if (!isNumericId(sid) || !seen.insert(sid).second) {
                return fail("invalid_track_id", "Duplicate or non-numeric catalog song ID: " + sid);
            }
            requested.push_back(sid);
        }
        std::vector<MusicTrack> verified;
        std::vector<std::string> unplayable;
        std::string err;
        if (!adam_.preflightCatalogSongs(requested, sf, verified, unplayable, err, false, "", cancel)) {
            return fail(err.rfind("auth_required", 0) == 0 ? "auth_required" : "catalog_preflight_failed",
                        err, {
                            {"storefront", sf},
                            {"requested_count", requested.size()},
                            {"verified_count", 0},
                            {"all_playable", false},
                            {"verified_ids", nlohmann::json::array()},
                            {"unplayable_ids", requested},
                            {"tracks", nlohmann::json::array()},
                        });
        }
        nlohmann::json verified_ids = nlohmann::json::array();
        nlohmann::json tracks = nlohmann::json::array();
        for (const auto& tr : verified) {
            verified_ids.push_back(tr.id);
            tracks.push_back({
                {"id", tr.id},
                {"title", tr.title},
                {"artist", tr.artist},
                {"album", tr.album},
                {"storefront", sf},
                {"duration_sec", tr.duration_sec},
                {"quality_label", tr.quality_label},
                {"url", "https://music.apple.com/cn/song/" + tr.id},
            });
        }
        res.ok = true;
        res.data = {
            {"storefront", sf},
            {"requested_count", requested.size()},
            {"verified_count", verified.size()},
            {"all_playable", verified.size() == requested.size() && unplayable.empty()},
            {"verified_ids", std::move(verified_ids)},
            {"unplayable_ids", std::move(unplayable)},
            {"tracks", std::move(tracks)},
            {"verification_source", "apple_music_live_catalog"},
        };
        return res;
    }

    if (action == "sync_daily_authorized") {
        if (req.value("mode", "") != "daily_immutable") {
            return fail("invalid_curation_mode", "sync_daily_authorized strictly requires mode == 'daily_immutable'");
        }
        if (!req.contains("qa_verified") || !req["qa_verified"].is_boolean() || !req["qa_verified"].get<bool>()) {
            return fail("qa_not_verified", "sync_daily_authorized requires qa_verified == true plus repository publication proof");
        }
        if (req.contains("local_checkpoint_only") &&
            (!req["local_checkpoint_only"].is_boolean() || req["local_checkpoint_only"].get<bool>())) {
            return fail("local_checkpoint_only_forbidden",
                        "Daily cumulative checkpoints can only be mutated after live Apple Music readback");
        }
        const std::string target_playlist_id = trim(req.value("cumulative_playlist_id", std::string(kProtectedDailyPlaylistId)));
        if (target_playlist_id != kProtectedDailyPlaylistId) {
            return fail("invalid_target_playlist", "Daily authorized sync must target p.YJXV7dEIeb3GzZX");
        }
        const bool sync_all = req.contains("sync_all_rehydrated") && req["sync_all_rehydrated"].is_boolean() &&
                              req["sync_all_rehydrated"].get<bool>();
        const bool dry_run = req.contains("dry_run") && req["dry_run"].is_boolean() && req["dry_run"].get<bool>();
        const bool create_dated = req.contains("create_dated_playlist") &&
                                  req["create_dated_playlist"].is_boolean() &&
                                  req["create_dated_playlist"].get<bool>();
        const std::string dated_prefix = req.value("dated_playlist_prefix", "ChatGPT Daily 5 · ");
        if (create_dated && (sync_all || dated_prefix != "ChatGPT Daily 5 · ")) {
            return fail("invalid_argument", "Dated playlist creation is only allowed for single-day sync with prefix 'ChatGPT Daily 5 · '");
        }

        ScopedFlock lock(daily_state_file.string() + ".lock");
        if (!lock.locked()) return fail("lock_failed", "Another playlist state operation is active");

        nlohmann::json state;
        std::string err;
        if (!readStateObject(daily_state_file, state, err)) return fail("corrupt_state", err);
        normalizeDailyState(state);

        std::vector<ManifestDay> days;
        std::vector<std::string> targets;
        std::string music_head;
        std::vector<std::string> history_anomalies;
        if (!authorizeDailyRepository(req, music_repo, vault_repo, sync_all, days, targets,
                                      music_head, history_anomalies, err, !dry_run)) {
            return fail("publication_not_authorized", err);
        }

        if (dry_run) {
            const auto confirmed = jsonStringSet(state["cumulative_track_ids"]);
            nlohmann::json planned_missing = nlohmann::json::array();
            size_t already_confirmed = 0;
            for (const auto& sid : targets) {
                if (confirmed.count(sid)) ++already_confirmed;
                else planned_missing.push_back(sid);
            }
            res.ok = true;
            res.data = {
                {"mode", "daily_immutable"},
                {"dry_run", true},
                {"cloud_synced", false},
                {"live_apple_music_synced", false},
                {"readback_verified", false},
                {"verified_count", 0},
                {"added_count", 0},
                {"added_ids", nlohmann::json::array()},
                {"dates_synced", 0},
                {"synced_dates", nlohmann::json::array()},
                {"total_cumulative_tracks", confirmed.size()},
                {"planned_dates_count", days.size()},
                {"planned_target_count", targets.size()},
                {"planned_missing_ids", std::move(planned_missing)},
                {"already_confirmed_in_local_ledger_count", already_confirmed},
                {"cumulative_playlist_id", target_playlist_id},
                {"music_head", music_head},
                {"history_anomalies", history_anomalies},
            };
            return res;
        }

        if (!adam_.hasUsableAuthForWrite()) {
            return fail("auth_required", "Apple Music developer token and Music-User-Token are required for cloud sync");
        }

        const std::string dev_tok = adam_.cachedOrEnvDeveloperToken();
        const std::string mut_tok = adam_.resolveMediaUserToken();
        const bool use_python_daily = (!sync_all && !days.empty() &&
                                       fs::exists(music_repo / "scripts" / "local_sync_gate.py") &&
                                       fs::exists(music_repo / "scripts" / "apple_music_sync.py") &&
                                       !resolveWritebackWorkerScript(ctx_).empty());
        if (use_python_daily) {
            nlohmann::json py_days = nlohmann::json::array();
            for (const auto& d : days) {
                py_days.push_back({
                    {"date", d.date},
                    {"rel_path", d.rel_path},
                    {"song_ids", d.song_ids},
                });
            }
            nlohmann::json py_req = {
                {"op", "sync_daily_cloud"},
                {"cumulative_playlist_id", target_playlist_id},
                {"targets", targets},
                {"days", std::move(py_days)},
                {"music_repo", music_repo.string()},
                {"music_head", music_head},
                {"state_file", daily_state_file.string()},
                {"state", state},
                {"create_dated_playlist", create_dated},
                {"dated_playlist_prefix", dated_prefix},
                {"sync_all", sync_all},
                {"history_anomalies", history_anomalies},
                {"base_url", adam_.ampApiBaseUrl()},
                {"proxy", adam_.configuredProxy()},
                {"dev_token", dev_tok},
                {"music_user_token", mut_tok},
                {"no_wrapper", !AdamClient::localWrapperAuthEnabled()},
            };
            nlohmann::json py_data, py_extra;
            std::string py_code, py_err;
            if (!invokePythonWritebackWorker(ctx_, music_repo, py_req, {dev_tok, mut_tok},
                                             cancel, py_data, py_code, py_err, py_extra)) {
                return fail(py_code, py_err, std::move(py_extra));
            }
            ctx_.publish("entity.music.playlist_synced", {
                {"mode", "daily_immutable"},
                {"playlist_id", target_playlist_id},
                {"playlist_name", kProtectedDailyPlaylistName},
                {"business_date", py_data.value("business_date", days.front().date)},
                {"dates_synced", py_data.value("dates_synced", size_t(1))},
                {"added_count", py_data.value("added_count", size_t(0))},
                {"verified_count", py_data.value("verified_count", targets.size())},
                {"total_cumulative_tracks", py_data.value("total_cumulative_tracks", size_t(0))},
                {"readback_verified", py_data.value("readback_verified", true)},
                {"cloud_synced", true},
                {"live_apple_music_synced", true},
            });
            res.ok = true;
            res.data = std::move(py_data);
            return res;
        }

        std::vector<MusicTrack> verified_catalog;
        std::vector<std::string> unplayable;
        if (!adam_.preflightCatalogSongs(targets, "cn", verified_catalog, unplayable, err, false, "", cancel) ||
            !unplayable.empty() || verified_catalog.size() != targets.size()) {
            return fail("catalog_preflight_failed",
                        err.empty() ? "Selected song is not currently playable in CN catalog" : err,
                        {{"unplayable_ids", unplayable}});
        }

        ContainerItem remote_playlist;
        if (!adam_.fetchLibraryPlaylistFromAmp(target_playlist_id, remote_playlist, err, cancel) ||
            remote_playlist.name != kProtectedDailyPlaylistName) {
            return fail("apple_music_playlist_verification_failed",
                        err.empty() ? "Remote playlist name does not match ChatGPT Daily 5" : err);
        }

        std::vector<std::string> remote_ids;
        if (!adam_.fetchLibraryPlaylistCatalogIdsFromAmp(target_playlist_id, remote_ids, err, cancel)) {
            return fail("apple_music_read_failed", err);
        }
        if (!state["pending_song_ids"].empty()) {
            std::vector<std::string> pending;
            for (const auto& item : state["pending_song_ids"]) {
                if (item.is_string()) pending.push_back(item.get<std::string>());
            }
            if (!verifyLivePlaylistContains(adam_, target_playlist_id, pending, remote_ids, err, cancel)) {
                return fail("pending_write_unconfirmed",
                            "Previous pending playlist write is not confirmed by live readback; refusing to resend");
            }
            state["pending_song_ids"] = nlohmann::json::array();
            if (!writeStateObject(daily_state_file, state, err)) return fail("state_write_failed", err);
        }

        std::unordered_set<std::string> remote_set(remote_ids.begin(), remote_ids.end());
        std::vector<std::string> missing;
        for (const auto& sid : targets) {
            if (!remote_set.count(sid)) missing.push_back(sid);
        }
        std::vector<std::string> added_ids;
        for (size_t offset = 0; offset < missing.size(); offset += 25) {
            std::string recheck_head;
            std::vector<ManifestDay> recheck_days;
            std::vector<std::string> recheck_targets;
            std::vector<std::string> recheck_anomalies;
            if (!authorizeDailyRepository(req, music_repo, vault_repo, sync_all, recheck_days,
                                          recheck_targets, recheck_head, recheck_anomalies, err) ||
                recheck_head != music_head || recheck_targets != targets ||
                recheck_anomalies != history_anomalies) {
                return fail("publication_not_authorized", "Authorized repository state changed before cloud write");
            }
            std::vector<std::string> batch(missing.begin() + static_cast<ptrdiff_t>(offset),
                                           missing.begin() + static_cast<ptrdiff_t>(std::min(missing.size(), offset + 25)));
            state["pending_song_ids"] = batch;
            if (!writeStateObject(daily_state_file, state, err)) return fail("state_write_failed", err);
            if (!adam_.appendLibraryPlaylistTracks(target_playlist_id, batch, err, cancel)) {
                std::vector<std::string> post_err_remote;
                std::string rb_err;
                if (verifyLivePlaylistContains(adam_, target_playlist_id, batch, post_err_remote, rb_err, cancel)) {
                    remote_ids = std::move(post_err_remote);
                    state["pending_song_ids"] = nlohmann::json::array();
                    writeStateObject(daily_state_file, state, rb_err);
                }
                return fail("apple_music_append_failed", err);
            }
            if (!verifyLivePlaylistContains(adam_, target_playlist_id, batch, remote_ids, err, cancel)) {
                return fail("readback_verification_failed", err);
            }
            state["pending_song_ids"] = nlohmann::json::array();
            state["cumulative_track_ids"] = remote_ids;
            if (!writeStateObject(daily_state_file, state, err)) return fail("state_write_failed", err);
            added_ids.insert(added_ids.end(), batch.begin(), batch.end());
        }
        if (!verifyLivePlaylistContains(adam_, target_playlist_id, targets, remote_ids, err, cancel)) {
            return fail("readback_verification_failed", err);
        }

        if (create_dated && !days.empty()) {
            const std::string date = days.front().date;
            if (state["dated_playlists"].contains(date) &&
                state["dated_playlists"][date].value("creation_pending", false)) {
                return fail("dated_playlist_pending_unconfirmed",
                            "Previous dated playlist creation is unconfirmed; inspect Apple Music before retrying");
            }
            if (!state["dated_playlists"].contains(date) ||
                !state["dated_playlists"][date].value("cloud_synced", false)) {
                const std::string dated_name = dated_prefix + date;
                state["dated_playlists"][date] = {{"name", dated_name}, {"creation_pending", true}};
                if (!writeStateObject(daily_state_file, state, err)) return fail("state_write_failed", err);
                std::string dated_id;
                if (!adam_.createLibraryPlaylist(dated_name, "OmniDesk Daily 5 (" + date + ")",
                                                 days.front().song_ids, dated_id, err, cancel)) {
                    return fail("dated_playlist_create_failed", err);
                }
                std::vector<std::string> dated_remote;
                if (!verifyLivePlaylistContains(adam_, dated_id, days.front().song_ids, dated_remote, err, cancel)) {
                    return fail("readback_verification_failed", err);
                }
                state["dated_playlists"][date] = {
                    {"name", dated_name},
                    {"playlist_id", dated_id},
                    {"track_ids", days.front().song_ids},
                    {"cloud_synced", true},
                    {"creation_pending", false},
                    {"synced_at", utcNow()},
                };
            }
        }

        std::unordered_set<std::string> final_remote_set(remote_ids.begin(), remote_ids.end());
        nlohmann::json synced_dates = nlohmann::json::array();
        for (const auto& day : days) {
            if (std::all_of(day.song_ids.begin(), day.song_ids.end(), [&](const std::string& sid) {
                    return final_remote_set.count(sid) > 0;
                })) {
                synced_dates.push_back(day.date);
                state["synced_dates"][day.date] = {
                    {"business_date", day.date},
                    {"operation_id", "music:" + day.date},
                    {"music_head", music_head},
                    {"track_ids", day.song_ids},
                    {"cloud_synced", true},
                    {"verified_at", utcNow()},
                };
            }
        }
        state["cumulative_track_ids"] = remote_ids;
        state["last_verified_music_head"] = music_head;
        state["last_updated_at"] = utcNow();
        if (!writeStateObject(daily_state_file, state, err)) return fail("state_write_failed", err);

        ctx_.publish("entity.music.playlist_synced", {
            {"mode", "daily_immutable"},
            {"playlist_id", target_playlist_id},
            {"playlist_name", kProtectedDailyPlaylistName},
            {"business_date", sync_all ? "" : days.front().date},
            {"dates_synced", synced_dates.size()},
            {"added_count", added_ids.size()},
            {"verified_count", targets.size()},
            {"total_cumulative_tracks", remote_ids.size()},
            {"readback_verified", true},
            {"cloud_synced", true},
            {"live_apple_music_synced", true},
        });

        res.ok = true;
        res.data = {
            {"mode", "daily_immutable"},
            {"cumulative_playlist_id", target_playlist_id},
            {"cumulative_playlist_name", kProtectedDailyPlaylistName},
            {"business_date", sync_all ? "" : days.front().date},
            {"music_head", music_head},
            {"dates_synced", synced_dates.size()},
            {"synced_dates", std::move(synced_dates)},
            {"added_ids", std::move(added_ids)},
            {"added_count", missing.size()},
            {"already_present_count", targets.size() - missing.size()},
            {"verified_count", targets.size()},
            {"total_cumulative_tracks", remote_ids.size()},
            {"readback_verified", true},
            {"cloud_synced", true},
            {"live_apple_music_synced", true},
            {"dry_run", false},
            {"state_file", daily_state_file.string()},
            {"history_anomalies", history_anomalies},
        };
        return res;
    }

    if (action == "write_interactive_playlist" || action == "enqueue_playback") {
        nlohmann::json daily_state;
        std::string state_err;
        if (!readStateObject(daily_state_file, daily_state, state_err)) {
            return fail("corrupt_state", state_err);
        }
        normalizeDailyState(daily_state);

        const std::string req_playlist_id = trim(req.value("playlist_id", ""));
        const std::string req_cum_id = trim(req.value("cumulative_playlist_id", ""));
        const std::string req_name = trim(req.value("playlist_name", ""));
        const std::string playlist_name = req_name.empty() ? kDefaultInteractivePlaylist : req_name;
        if (isProtectedPlaylist(req_playlist_id, req_name, daily_state) ||
            isProtectedPlaylist(req_cum_id, playlist_name, daily_state)) {
            return fail("protected_daily_playlist",
                        "Interactive recommendations are strictly forbidden from modifying the protected daily playlist p.YJXV7dEIeb3GzZX (ChatGPT Daily 5)",
                        {{"protected_playlist_id", kProtectedDailyPlaylistId}});
        }
        if (req.value("mode", "interactive_ephemeral") != "interactive_ephemeral") {
            return fail("invalid_curation_mode", action + " strictly requires mode == 'interactive_ephemeral'");
        }
        if (req.contains("record_permanent_history") &&
            (!req["record_permanent_history"].is_boolean() || req["record_permanent_history"].get<bool>())) {
            return fail("permanent_history_forbidden", "Interactive playlists cannot record permanent history");
        }

        std::vector<std::string> requested_ids;
        std::vector<MusicTrack> tracks;
        if (req.contains("track_ids") && req["track_ids"].is_array()) {
            for (const auto& item : req["track_ids"]) {
                if (!item.is_string() || !isNumericId(trim(item.get<std::string>()))) {
                    return fail("invalid_track_id", "Interactive track_ids must be numeric catalog song IDs");
                }
                std::string sid = trim(item.get<std::string>());
                requested_ids.push_back(sid);
                MusicTrack tr;
                tr.id = sid;
                tr.title = sid;
                tr.storefront = "cn";
                tr.quality_label = "Apple Music CN · ALAC";
                tracks.push_back(std::move(tr));
            }
        }
        if (req.contains("tracks") && req["tracks"].is_array() && requested_ids.empty()) {
            for (const auto& item : req["tracks"]) {
                if (!item.is_object()) return fail("invalid_track_id", "tracks entries must be objects");
                std::string sid = trim(item.value("song_id", item.value("id", "")));
                if (!isNumericId(sid)) return fail("invalid_track_id", "Invalid song_id: " + sid);
                requested_ids.push_back(sid);
                MusicTrack tr;
                tr.id = sid;
                tr.title = item.value("title", sid);
                tr.artist = item.value("artist", "");
                tr.album = item.value("album", "");
                tr.storefront = item.value("storefront", "cn");
                tr.quality_label = "Apple Music CN · ALAC";
                tracks.push_back(std::move(tr));
            }
        }
        if (requested_ids.empty()) return fail("invalid_argument", action + " requires non-empty numeric track_ids");

        const std::string strategy = req.value("write_strategy", "append");
        if (strategy != "append" && strategy != "replace") {
            return fail("invalid_argument", "write_strategy must be 'append' or 'replace'");
        }
        const bool dry_run = req.contains("dry_run") && req["dry_run"].is_boolean() && req["dry_run"].get<bool>();
        const bool local_only = req.contains("local_checkpoint_only") &&
                                req["local_checkpoint_only"].is_boolean() &&
                                req["local_checkpoint_only"].get<bool>();
        const bool enqueue_now = !dry_run &&
                                 ((req.contains("enqueue_now") && req["enqueue_now"].is_boolean() &&
                                   req["enqueue_now"].get<bool>()) ||
                                  action == "enqueue_playback");
        const bool wants_playlist_write = (action == "write_interactive_playlist");
        const bool wants_cloud_write = wants_playlist_write && !dry_run && !local_only;
        if (wants_cloud_write && strategy == "replace") {
            return fail("replace_cloud_playlist_unsupported",
                        "Cloud interactive playlists only support append; use local_checkpoint_only for local replacement staging");
        }
        if (wants_cloud_write && !adam_.hasUsableAuthForWrite()) {
            return fail("auth_required",
                        "Apple Music developer token and Music-User-Token are required for cloud interactive playlist writes; pass local_checkpoint_only: true for local-only staging");
        }

        const std::string local_id = (!req_playlist_id.empty() && req_playlist_id.rfind("p.", 0) != 0)
                                         ? req_playlist_id
                                         : ("ix-pl-" + sha256Hex(playlist_name).substr(0, 12));
        if (!isSafeId(local_id) || (!req_playlist_id.empty() && !isSafeId(req_playlist_id))) {
            return fail("invalid_argument", "Invalid playlist_id");
        }

        if (dry_run) {
            res.ok = true;
            res.data = {
                {"mode", "interactive_ephemeral"},
                {"dry_run", true},
                {"playlist_id", local_id},
                {"playlist_name", playlist_name},
                {"planned_track_ids", requested_ids},
                {"requested_count", requested_ids.size()},
                {"verified_count", 0},
                {"cloud_synced", false},
                {"live_apple_music_synced", false},
                {"local_staged", false},
                {"enqueued", false},
            };
            return res;
        }

        std::string apple_playlist_id = (req_playlist_id.rfind("p.", 0) == 0) ? req_playlist_id : "";
        nlohmann::json final_ids = requested_ids;
        bool cloud_synced = false;
        bool local_staged = false;

        if (wants_cloud_write || (enqueue_now && !local_only)) {
            std::vector<MusicTrack> verified_catalog;
            std::vector<std::string> unplayable;
            if (!adam_.preflightCatalogSongs(requested_ids, "cn", verified_catalog, unplayable,
                                             state_err, false, "", cancel)) {
                return fail("catalog_preflight_failed", state_err,
                            {{"unplayable_ids", unplayable}});
            }
            if (!unplayable.empty() || verified_catalog.size() != requested_ids.size()) {
                return fail("catalog_preflight_failed",
                            "Every interactive track must be verified as playable in the CN catalog",
                            {{"requested_count", requested_ids.size()},
                             {"verified_count", verified_catalog.size()},
                             {"unplayable_ids", unplayable}});
            }
            tracks = verified_catalog;
        }

        if (wants_playlist_write) {
            ScopedFlock lock(interactive_file.string() + ".lock");
            if (!lock.locked()) return fail("lock_failed", "Interactive playlist store is locked");

            nlohmann::json store;
            if (!readStateObject(interactive_file, store, state_err)) return fail("corrupt_state", state_err);
            store["schema_version"] = 2;
            if (!store.contains("playlists") || !store["playlists"].is_array()) {
                store["playlists"] = nlohmann::json::array();
            }
            nlohmann::json* entry = nullptr;
            for (auto& item : store["playlists"]) {
                if (item.is_object() &&
                    (item.value("playlist_id", "") == local_id || item.value("name", "") == playlist_name)) {
                    entry = &item;
                    break;
                }
            }
            if (!entry) {
                store["playlists"].push_back({
                    {"playlist_id", local_id},
                    {"apple_music_playlist_id", ""},
                    {"name", playlist_name},
                    {"mode", "interactive_ephemeral"},
                    {"allow_historical_repeats", true},
                    {"cloud_synced", false},
                    {"track_ids", nlohmann::json::array()},
                    {"staged_track_ids", nlohmann::json::array()},
                });
                entry = &store["playlists"].back();
            }
            if (apple_playlist_id.empty()) {
                apple_playlist_id = entry->value("apple_music_playlist_id", "");
            }
            if (isProtectedPlaylist(apple_playlist_id, playlist_name, daily_state)) {
                return fail("protected_daily_playlist",
                            "Interactive recommendations are strictly forbidden from modifying the protected daily playlist p.YJXV7dEIeb3GzZX (ChatGPT Daily 5)",
                            {{"protected_playlist_id", kProtectedDailyPlaylistId}});
            }

            if (wants_cloud_write) {
                const std::string dev_tok = adam_.cachedOrEnvDeveloperToken();
                const std::string mut_tok = adam_.resolveMediaUserToken();
                const fs::path upstream_check =
                    (!music_repo.empty() && fs::exists(music_repo / "scripts" / "apple_music_sync.py"))
                        ? (music_repo / "scripts" / "apple_music_sync.py")
                        : (resolveRepoDir(music_config.music_daily_dir, data_root, "music-daily") / "scripts" / "apple_music_sync.py");
                const bool use_python_interactive =
                    !resolveWritebackWorkerScript(ctx_).empty() &&
                    !upstream_check.empty() && fs::exists(upstream_check);
                std::vector<std::string> remote_ids;
                if (use_python_interactive) {
                    const fs::path py_repo = upstream_check.parent_path().parent_path();
                    nlohmann::json py_req = {
                        {"op", "write_interactive_cloud"},
                        {"music_repo", py_repo.string()},
                        {"playlist_name", playlist_name},
                        {"apple_playlist_id", apple_playlist_id},
                        {"requested_ids", requested_ids},
                        {"daily_state", daily_state},
                        {"checkpoint_dir", (data_root / "curation" / "apple-music-interactive").string()},
                        {"base_url", adam_.ampApiBaseUrl()},
                        {"proxy", adam_.configuredProxy()},
                        {"dev_token", dev_tok},
                        {"music_user_token", mut_tok},
                        {"no_wrapper", !AdamClient::localWrapperAuthEnabled()},
                    };
                    nlohmann::json py_data, py_extra;
                    std::string py_code, py_err;
                    if (!invokePythonWritebackWorker(ctx_, py_repo, py_req, {dev_tok, mut_tok},
                                                     cancel, py_data, py_code, py_err, py_extra)) {
                        return fail(py_code, py_err, std::move(py_extra));
                    }
                    apple_playlist_id = py_data.value("apple_music_playlist_id", apple_playlist_id);
                    if (py_data.contains("remote_track_ids") && py_data["remote_track_ids"].is_array()) {
                        for (const auto& id_item : py_data["remote_track_ids"]) {
                            if (id_item.is_string()) remote_ids.push_back(id_item.get<std::string>());
                        }
                    } else {
                        remote_ids = requested_ids;
                    }
                } else if (!apple_playlist_id.empty()) {
                    ContainerItem remote_meta;
                    if (!adam_.fetchLibraryPlaylistFromAmp(apple_playlist_id, remote_meta, state_err, cancel)) {
                        return fail("apple_music_playlist_verification_failed", state_err);
                    }
                    if (isProtectedPlaylist(remote_meta.id, remote_meta.name, daily_state)) {
                        return fail("protected_daily_playlist",
                                    "Remote Apple Music playlist resolved to protected ChatGPT Daily 5 playlist",
                                    {{"protected_playlist_id", kProtectedDailyPlaylistId},
                                     {"resolved_playlist_id", remote_meta.id},
                                     {"resolved_playlist_name", remote_meta.name}});
                    }
                    if (!adam_.fetchLibraryPlaylistCatalogIdsFromAmp(apple_playlist_id, remote_ids, state_err, cancel)) {
                        return fail("apple_music_read_failed", state_err);
                    }
                    std::unordered_set<std::string> remote_set(remote_ids.begin(), remote_ids.end());
                    std::vector<std::string> missing;
                    for (const auto& sid : requested_ids) {
                        if (!remote_set.count(sid)) missing.push_back(sid);
                    }
                    if (!missing.empty() && !adam_.appendLibraryPlaylistTracks(apple_playlist_id, missing, state_err, cancel)) {
                        return fail("apple_music_append_failed", state_err);
                    }
                    if (!verifyLivePlaylistContains(adam_, apple_playlist_id, requested_ids, remote_ids, state_err, cancel)) {
                        return fail("readback_verification_failed", state_err);
                    }
                } else {
                    if (!adam_.createLibraryPlaylist(playlist_name, "OmniDesk AI 灵感电台", requested_ids,
                                                     apple_playlist_id, state_err, cancel) ||
                        !verifyLivePlaylistContains(adam_, apple_playlist_id, requested_ids, remote_ids, state_err, cancel)) {
                        return fail("apple_music_create_failed", state_err);
                    }
                }
                (*entry)["apple_music_playlist_id"] = apple_playlist_id;
                (*entry)["cloud_synced"] = true;
                (*entry)["track_ids"] = remote_ids;
                (*entry)["staged_track_ids"] = remote_ids;
                (*entry)["updated_at"] = utcNow();
                final_ids = remote_ids;
                cloud_synced = true;
            } else {
                std::vector<std::string> staged;
                std::unordered_set<std::string> seen;
                if (strategy != "replace" && entry->contains("staged_track_ids") && (*entry)["staged_track_ids"].is_array()) {
                    for (const auto& item : (*entry)["staged_track_ids"]) {
                        if (item.is_string() && seen.insert(item.get<std::string>()).second) {
                            staged.push_back(item.get<std::string>());
                        }
                    }
                }
                const bool allow_dup = req.contains("allow_duplicates") &&
                                       req["allow_duplicates"].is_boolean() &&
                                       req["allow_duplicates"].get<bool>();
                for (const auto& sid : requested_ids) {
                    if (allow_dup || seen.insert(sid).second) staged.push_back(sid);
                }
                (*entry)["cloud_synced"] = false;
                (*entry)["staged_track_ids"] = staged;
                (*entry)["track_ids"] = staged;
                (*entry)["updated_at"] = utcNow();
                final_ids = staged;
                local_staged = true;
            }
            if (!writeStateObject(interactive_file, store, state_err)) {
                return fail("state_write_failed", state_err);
            }
        }

        if (enqueue_now) setQueueAndPlay(tracks, 0);
        if (cloud_synced) {
            ctx_.publish("entity.music.playlist_synced", {
                {"mode", "interactive_ephemeral"},
                {"playlist_id", local_id},
                {"apple_music_playlist_id", apple_playlist_id},
                {"playlist_name", playlist_name},
                {"track_count", final_ids.size()},
                {"verified_count", requested_ids.size()},
                {"enqueued", enqueue_now},
                {"cloud_synced", true},
                {"live_apple_music_synced", true},
            });
        }

        res.ok = true;
        res.data = {
            {"mode", "interactive_ephemeral"},
            {"playlist_id", local_id},
            {"apple_music_playlist_id", cloud_synced ? apple_playlist_id : ""},
            {"playlist_name", playlist_name},
            {"write_strategy", strategy},
            {"allow_historical_repeats", true},
            {"track_ids", final_ids},
            {"requested_count", requested_ids.size()},
            {"verified_count", cloud_synced ? requested_ids.size() : 0},
            {"readback_verified", cloud_synced},
            {"cloud_synced", cloud_synced},
            {"live_apple_music_synced", cloud_synced},
            {"local_staged", local_staged},
            {"enqueued", enqueue_now},
            {"playlists_file", interactive_file.string()},
        };
        return res;
    }

    if (action == "status" || action == "list_playlists") {
        nlohmann::json daily_state;
        nlohmann::json interactive_store;
        std::string err;
        if (!readStateObject(daily_state_file, daily_state, err) ||
            !readStateObject(interactive_file, interactive_store, err)) {
            return fail("corrupt_state", err);
        }
        normalizeDailyState(daily_state);
        const auto& synced_dates = daily_state["synced_dates"];
        const auto& cum_ids = daily_state["cumulative_track_ids"];
        res.ok = true;
        res.data = {
            {"protected_daily_playlist", {
                {"playlist_id", daily_state.value("cumulative_playlist_id", std::string(kProtectedDailyPlaylistId))},
                {"name", kProtectedDailyPlaylistName},
                {"synced_dates_count", synced_dates.size()},
                {"cumulative_track_count", cum_ids.size()},
                {"verified_count", cum_ids.size()},
                {"cumulative_track_ids", cum_ids},
                {"pending_song_ids", daily_state["pending_song_ids"]},
                {"synced_dates", synced_dates},
                {"dated_playlists", daily_state["dated_playlists"]},
                {"state_file", daily_state_file.string()},
            }},
            {"interactive_playlists", interactive_store.value("playlists", nlohmann::json::array())},
            {"interactive_playlists_file", interactive_file.string()},
            {"has_live_apple_music_auth", adam_.hasUsableAuthForWrite()},
        };
        return res;
    }

    return fail("invalid_argument", "unsupported media.playlist.v1 action: " + action);
}

} // namespace music_plugin
