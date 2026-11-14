#include <omnidesk/worker_supervisor.hpp>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <sstream>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

namespace omnidesk {

namespace {

int64_t steadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

void ignoreSigpipeOnce() {
    static std::once_flag once;
    std::call_once(once, []() {
        struct sigaction old_sa{};
        if (sigaction(SIGPIPE, nullptr, &old_sa) == 0 && old_sa.sa_handler == SIG_DFL) {
            std::signal(SIGPIPE, SIG_IGN);
        }
    });
}

void setCloexec(int fd) {
    if (fd >= 0) {
        int flags = fcntl(fd, F_GETFD, 0);
        if (flags >= 0) fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
    }
}

void closeFd(int& fd) {
    if (fd >= 0) {
        close(fd);
        fd = -1;
    }
}

bool writeAll(int fd, const std::string& data) {
    size_t sent = 0;
    while (sent < data.size()) {
        ssize_t n = write(fd, data.data() + sent, data.size() - sent);
        if (n > 0) {
            sent += static_cast<size_t>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        return false;
    }
    return true;
}

std::string extractJsonDelta(const nlohmann::json& obj) {
    if (!obj.is_object()) return {};
    for (const char* k : {"delta", "token", "text", "chunk"}) {
        if (obj.contains(k) && obj[k].is_string()) {
            return obj[k].get<std::string>();
        }
    }
    return {};
}

std::string signalNameFromNumber(int sig) {
    switch (sig) {
        case SIGSEGV: return "SIGSEGV";
        case SIGABRT: return "SIGABRT";
        case SIGBUS: return "SIGBUS";
        case SIGFPE: return "SIGFPE";
        case SIGILL: return "SIGILL";
        case SIGKILL: return "SIGKILL";
        case SIGTERM: return "SIGTERM";
        case SIGPIPE: return "SIGPIPE";
        case SIGHUP: return "SIGHUP";
        case SIGINT: return "SIGINT";
        default:
            if (sig > 0) return "SIG" + std::to_string(sig);
            return {};
    }
}

std::string makeDefaultSocketPath(const std::string& worker_id, uint64_t seq) {
    std::string safe;
    safe.reserve(32);
    for (unsigned char ch : worker_id) {
        if (std::isalnum(ch) || ch == '-' || ch == '_') {
            safe.push_back(static_cast<char>(ch));
        } else {
            safe.push_back('_');
        }
        if (safe.size() >= 32) break;
    }
    if (safe.empty()) safe = "worker";
    std::ostringstream oss;
    oss << "/tmp/odw-" << static_cast<long>(getpid()) << "-" << seq << "-" << safe << ".sock";
    std::string path = oss.str();
    if (path.size() >= sizeof(sockaddr_un::sun_path)) {
        path.resize(sizeof(sockaddr_un::sun_path) - 6);
        path += ".sock";
    }
    return path;
}

std::atomic<uint64_t> g_socket_seq{1};

}  // namespace

WorkerSupervisor::WorkerSupervisor() {
    watchdog_thread_ = std::thread([this]() { watchdogLoop(); });
}

WorkerSupervisor::~WorkerSupervisor() {
    {
        std::lock_guard lock(mutex_);
        shutting_down_ = true;
    }
    watchdog_cv_.notify_all();
    stopAll();
    if (watchdog_thread_.joinable()) {
        watchdog_thread_.join();
    }
}

void WorkerSupervisor::setFaultCallback(FaultCallback cb) {
    std::lock_guard lock(fault_cb_mutex_);
    fault_cb_ = std::move(cb);
}

void WorkerSupervisor::notifyFault(const WorkerFaultEvent& evt) const {
    FaultCallback cb;
    {
        std::lock_guard lock(fault_cb_mutex_);
        cb = fault_cb_;
    }
    if (cb) {
        try {
            cb(evt);
        } catch (...) {
        }
    }
}

void WorkerSupervisor::closeIoFdsLocked(WorkerRuntime& rt) {
    if (rt.shared_io_fd) {
        closeFd(rt.stdin_fd);
        rt.stdout_fd = -1;
        rt.shared_io_fd = false;
    } else {
        closeFd(rt.stdin_fd);
        closeFd(rt.stdout_fd);
    }
    if (!rt.active_socket_path.empty()) {
        ::unlink(rt.active_socket_path.c_str());
        rt.active_socket_path.clear();
    }
}

bool WorkerSupervisor::registerWorker(const WorkerSpec& spec) {
    if (spec.worker_id.empty() || spec.executable.empty()) {
        return false;
    }
    std::shared_ptr<WorkerRuntime> previous;
    {
        std::lock_guard lock(mutex_);
        auto it = workers_.find(spec.worker_id);
        if (it != workers_.end()) {
            previous = it->second;
        }
        auto rt = std::make_shared<WorkerRuntime>();
        rt->spec = spec;
        workers_[spec.worker_id] = std::move(rt);
    }
    if (previous) {
        stopRuntime(*previous);
    }
    return true;
}

bool WorkerSupervisor::unregisterWorker(const std::string& worker_id) {
    std::shared_ptr<WorkerRuntime> rt;
    {
        std::lock_guard lock(mutex_);
        auto it = workers_.find(worker_id);
        if (it == workers_.end()) return false;
        rt = std::move(it->second);
        workers_.erase(it);
    }
    if (rt) stopRuntime(*rt);
    return true;
}

std::shared_ptr<WorkerSupervisor::WorkerRuntime> WorkerSupervisor::findRuntime(
    const std::string& worker_id) const {
    std::lock_guard lock(mutex_);
    auto it = workers_.find(worker_id);
    return it == workers_.end() ? nullptr : it->second;
}

bool WorkerSupervisor::reapExitedLocked(WorkerRuntime& rt,
                                        bool* unexpected_crash,
                                        WorkerFaultEvent* crash_event,
                                        bool force_unexpected) {
    if (unexpected_crash) *unexpected_crash = false;
    if (rt.pid <= 0 || rt.stopping_child) return false;

    int status = 0;
    pid_t res = waitpid(rt.pid, &status, WNOHANG);
    if (res == 0) {
        return false;
    }
    if (res < 0 && errno != ECHILD) {
        return false;
    }

    int exit_code = 0;
    int sig = 0;
    std::string sig_name;
    if (res == rt.pid) {
        if (WIFEXITED(status)) {
            exit_code = WEXITSTATUS(status);
        } else if (WIFSIGNALED(status)) {
            sig = WTERMSIG(status);
            exit_code = 128 + sig;
            sig_name = signalNameFromNumber(sig);
        }
    }

    const int dead_pid = rt.pid;
    rt.pid = -1;
    closeIoFdsLocked(rt);

    const bool crashed = !rt.stopping_child && (force_unexpected || sig != 0 || exit_code != 0);
    if (crashed) {
        const int64_t now = steadyNowMs();
        rt.last_exit_code = exit_code;
        rt.last_signal = sig;
        rt.last_signal_name = sig_name;
        rt.last_crash_ms = now;
        rt.last_exited_unexpectedly = true;
        ++rt.crash_count;

        std::string err = "worker process crashed";
        if (!sig_name.empty()) {
            err += " (" + sig_name + ")";
        } else if (exit_code != 0) {
            err += " (exit=" + std::to_string(exit_code) + ")";
        } else {
            err += " (unexpected EOF)";
        }
        rt.last_error = err;

        const int64_t window_ms = std::max<int64_t>(1000, rt.spec.restart_window_ms);
        const int threshold = std::max(1, rt.spec.max_restarts_in_window);
        rt.recent_crash_times_ms.push_back(now);
        while (!rt.recent_crash_times_ms.empty() &&
               (now - rt.recent_crash_times_ms.front()) > window_ms) {
            rt.recent_crash_times_ms.pop_front();
        }
        if (static_cast<int>(rt.recent_crash_times_ms.size()) > threshold) {
            rt.quarantined = true;
        }
        rt.pending_auto_restart = (rt.spec.auto_restart && !rt.quarantined);

        if (unexpected_crash) *unexpected_crash = true;
        if (crash_event) {
            WorkerFaultEvent evt;
            evt.worker_id = rt.spec.worker_id;
            evt.plugin_id = rt.spec.plugin_id;
            evt.pid = dead_pid;
            evt.exit_code = exit_code;
            evt.signal_number = sig;
            evt.signal_name = sig_name;
            evt.crash_count = rt.crash_count;
            evt.restart_count = rt.restart_count;
            evt.quarantined = rt.quarantined;
            evt.error = err;
            const size_t tail = std::min<size_t>(10, rt.stderr_ring.size());
            if (tail > 0) {
                evt.recent_stderr.assign(rt.stderr_ring.end() - tail, rt.stderr_ring.end());
            }
            *crash_event = std::move(evt);
        }
    } else if (res == dead_pid) {
        rt.last_exit_code = exit_code;
        rt.last_signal = 0;
        rt.last_signal_name.clear();
        rt.last_exited_unexpectedly = false;
        rt.pending_auto_restart = false;
    }

    return true;
}

bool WorkerSupervisor::spawnLocked(WorkerRuntime& rt,
                                   std::unique_lock<std::mutex>& lk,
                                   std::string* error,
                                   WorkerFaultEvent* crash_out) {
    ignoreSigpipeOnce();
    rt.lifecycle_cv.wait(lk, [&rt]() { return !rt.stopping_child; });

    bool crashed_prior = false;
    WorkerFaultEvent local_crash;
    reapExitedLocked(rt, &crashed_prior, &local_crash, false);
    if (crashed_prior && crash_out) {
        *crash_out = local_crash;
    }
    if (rt.pid > 0) return true;
    if (rt.quarantined) {
        if (error) {
            *error = "worker quarantined after repeated crashes (" + rt.last_error + ")";
        }
        return false;
    }

    closeIoFdsLocked(rt);
    closeFd(rt.stderr_fd);
    if (rt.stderr_thread.joinable()) {
        rt.stopping_child = true;
        std::thread err_join = std::move(rt.stderr_thread);
        lk.unlock();
        err_join.join();
        lk.lock();
        rt.stopping_child = false;
        rt.lifecycle_cv.notify_all();
        reapExitedLocked(rt, nullptr, nullptr, false);
        if (rt.pid > 0) return true;
        closeIoFdsLocked(rt);
        closeFd(rt.stderr_fd);
    }

    int err_pipe[2] = {-1, -1};
    if (pipe(err_pipe) != 0) {
        if (error) *error = std::string("stderr pipe failed: ") + std::strerror(errno);
        return false;
    }
    setCloexec(err_pipe[0]);
    setCloexec(err_pipe[1]);

    int in_pipe[2] = {-1, -1};
    int out_pipe[2] = {-1, -1};
    int listen_fd = -1;
    std::string sock_path;

    if (rt.spec.transport == WorkerTransport::UnixSocket) {
        sock_path = rt.spec.socket_path.empty()
                        ? makeDefaultSocketPath(rt.spec.worker_id, g_socket_seq.fetch_add(1))
                        : rt.spec.socket_path;
        if (sock_path.size() >= sizeof(sockaddr_un::sun_path)) {
            closeFd(err_pipe[0]);
            closeFd(err_pipe[1]);
            if (error) *error = "unix socket path too long: " + sock_path;
            return false;
        }
        ::unlink(sock_path.c_str());
        listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (listen_fd < 0) {
            closeFd(err_pipe[0]);
            closeFd(err_pipe[1]);
            if (error) *error = std::string("socket(AF_UNIX) failed: ") + std::strerror(errno);
            return false;
        }
        setCloexec(listen_fd);

        struct sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", sock_path.c_str());
        if (bind(listen_fd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) != 0 ||
            listen(listen_fd, 4) != 0) {
            closeFd(listen_fd);
            ::unlink(sock_path.c_str());
            closeFd(err_pipe[0]);
            closeFd(err_pipe[1]);
            if (error) *error = std::string("bind/listen AF_UNIX failed: ") + std::strerror(errno);
            return false;
        }
        chmod(sock_path.c_str(), 0600);
    } else {
        if (pipe(in_pipe) != 0 || pipe(out_pipe) != 0) {
            if (error) *error = std::string("pipe failed: ") + std::strerror(errno);
            for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) {
                closeFd(fd);
            }
            return false;
        }
        for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1]}) {
            setCloexec(fd);
        }
    }

    const pid_t parent_pid = getpid();
    pid_t pid = fork();
    if (pid < 0) {
        if (error) *error = std::string("fork failed: ") + std::strerror(errno);
        for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1], listen_fd}) {
            closeFd(fd);
        }
        if (!sock_path.empty()) ::unlink(sock_path.c_str());
        return false;
    }

    if (pid == 0) {
        setpgid(0, 0);
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        if (getppid() != parent_pid) {
            _exit(1);
        }

        if (rt.spec.transport == WorkerTransport::UnixSocket) {
            int devnull = open("/dev/null", O_RDWR);
            if (devnull >= 0) {
                dup2(devnull, STDIN_FILENO);
                dup2(devnull, STDOUT_FILENO);
                if (devnull > STDERR_FILENO) close(devnull);
            }
            dup2(err_pipe[1], STDERR_FILENO);
            close(err_pipe[0]);
            close(err_pipe[1]);
            if (listen_fd >= 0) close(listen_fd);
            setenv("OMNIDESK_WORKER_SOCKET", sock_path.c_str(), 1);
        } else {
            dup2(in_pipe[0], STDIN_FILENO);
            dup2(out_pipe[1], STDOUT_FILENO);
            dup2(err_pipe[1], STDERR_FILENO);
            for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) {
                close(fd);
            }
        }

        if (!rt.spec.working_dir.empty()) {
            if (chdir(rt.spec.working_dir.c_str()) != 0) {
                _exit(126);
            }
        }
        for (const auto& [k, v] : rt.spec.env) {
            setenv(k.c_str(), v.c_str(), 1);
        }

        std::vector<char*> argv;
        argv.reserve(rt.spec.args.size() + 2);
        argv.push_back(const_cast<char*>(rt.spec.executable.c_str()));
        for (const auto& arg : rt.spec.args) {
            argv.push_back(const_cast<char*>(arg.c_str()));
        }
        argv.push_back(nullptr);

        execvp(rt.spec.executable.c_str(), argv.data());
        _exit(127);
    }

    setpgid(pid, pid);
    close(err_pipe[1]);

    if (rt.spec.transport == WorkerTransport::UnixSocket) {
        int conn_fd = -1;
        const int64_t accept_deadline = steadyNowMs() + 5000;
        while (steadyNowMs() < accept_deadline) {
            struct pollfd pfd{listen_fd, POLLIN, 0};
            int pr = poll(&pfd, 1, 25);
            if (pr > 0 && (pfd.revents & POLLIN)) {
                conn_fd = accept(listen_fd, nullptr, nullptr);
                if (conn_fd >= 0) {
                    setCloexec(conn_fd);
                    break;
                }
            }
            int st = 0;
            pid_t exited = waitpid(pid, &st, WNOHANG);
            if (exited == pid) {
                break;
            }
        }
        closeFd(listen_fd);
        if (conn_fd < 0) {
            closeFd(err_pipe[0]);
            kill(-pid, SIGKILL);
            int st = 0;
            while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {}
            ::unlink(sock_path.c_str());
            if (error) *error = "worker failed to connect to AF_UNIX socket within 5s";
            return false;
        }
        rt.pid = pid;
        rt.stdin_fd = conn_fd;
        rt.stdout_fd = conn_fd;
        rt.shared_io_fd = true;
        rt.active_socket_path = sock_path;
    } else {
        close(in_pipe[0]);
        close(out_pipe[1]);
        rt.pid = pid;
        rt.stdin_fd = in_pipe[1];
        rt.stdout_fd = out_pipe[0];
        rt.shared_io_fd = false;
    }

    rt.stderr_fd = err_pipe[0];
    rt.last_active_ms = steadyNowMs();
    rt.pending_auto_restart = false;
    ++rt.restart_count;

    if (crashed_prior && crash_out) {
        crash_out->auto_restarted = true;
        crash_out->restart_succeeded = true;
        crash_out->restart_count = rt.restart_count;
    }

    int err_fd = rt.stderr_fd;
    rt.stderr_thread = std::thread([&rt, err_fd]() {
        char buf[1024];
        std::string line;
        while (true) {
            ssize_t n = read(err_fd, buf, sizeof(buf));
            if (n <= 0) {
                if (n < 0 && errno == EINTR) continue;
                break;
            }
            for (ssize_t i = 0; i < n; ++i) {
                if (buf[i] == '\r') continue;
                if (buf[i] == '\n') {
                    std::lock_guard lk(rt.state_mutex);
                    rt.stderr_ring.push_back(std::move(line));
                    line.clear();
                    while (rt.stderr_ring.size() > 100) rt.stderr_ring.pop_front();
                } else {
                    line.push_back(buf[i]);
                }
            }
        }
        if (!line.empty()) {
            std::lock_guard lk(rt.state_mutex);
            rt.stderr_ring.push_back(std::move(line));
            while (rt.stderr_ring.size() > 100) rt.stderr_ring.pop_front();
        }
    });

    return true;
}

void WorkerSupervisor::stopRuntime(WorkerRuntime& rt) {
    int pid = -1;
    int64_t grace_ms = 1200;
    {
        std::unique_lock<std::mutex> lk(rt.state_mutex);
        rt.lifecycle_cv.wait(lk, [&rt]() { return !rt.stopping_child; });
        if (rt.pid <= 0 && rt.stdin_fd < 0 && rt.stdout_fd < 0 && rt.stderr_fd < 0 &&
            !rt.stderr_thread.joinable()) {
            if (!rt.active_socket_path.empty()) {
                ::unlink(rt.active_socket_path.c_str());
                rt.active_socket_path.clear();
            }
            return;
        }
        rt.stopping_child = true;
        pid = rt.pid;
        grace_ms = std::max<int64_t>(50, rt.spec.stop_grace_ms);
        if (rt.shared_io_fd) {
            if (rt.stdin_fd >= 0) ::shutdown(rt.stdin_fd, SHUT_WR);
        } else {
            closeFd(rt.stdin_fd);
        }
    }

    int exit_code = 0;
    int sig = 0;
    bool have_exit_code = false;
    if (pid > 0) {
        kill(-pid, SIGTERM);
        int status = 0;
        const int64_t deadline = steadyNowMs() + grace_ms;
        bool reaped = false;
        while (steadyNowMs() < deadline) {
            pid_t r = waitpid(pid, &status, WNOHANG);
            if (r == pid) {
                reaped = true;
                have_exit_code = true;
                if (WIFEXITED(status)) {
                    exit_code = WEXITSTATUS(status);
                } else if (WIFSIGNALED(status)) {
                    sig = WTERMSIG(status);
                    exit_code = 128 + sig;
                }
                break;
            }
            if (r < 0 && errno == ECHILD) {
                reaped = true;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(15));
        }
        if (!reaped) {
            kill(-pid, SIGKILL);
            pid_t kr = -1;
            while ((kr = waitpid(pid, &status, 0)) < 0 && errno == EINTR) {}
            if (kr == pid) {
                have_exit_code = true;
                if (WIFEXITED(status)) {
                    exit_code = WEXITSTATUS(status);
                } else if (WIFSIGNALED(status)) {
                    sig = WTERMSIG(status);
                    exit_code = 128 + sig;
                }
            }
        }
    }

    std::thread err_join;
    {
        std::lock_guard lk(rt.state_mutex);
        if (pid > 0) {
            if (have_exit_code) {
                rt.last_exit_code = exit_code;
                rt.last_signal = sig;
                rt.last_signal_name = sig > 0 ? signalNameFromNumber(sig) : "";
            }
            rt.pid = -1;
        }
        rt.pending_auto_restart = false;
        closeIoFdsLocked(rt);
        if (rt.stderr_thread.joinable()) {
            err_join = std::move(rt.stderr_thread);
        }
    }
    if (err_join.joinable()) {
        err_join.join();
    }
    {
        std::lock_guard lk(rt.state_mutex);
        closeFd(rt.stderr_fd);
        rt.stopping_child = false;
    }
    rt.lifecycle_cv.notify_all();
}

bool WorkerSupervisor::ensureRunning(const std::string& worker_id, std::string* error) {
    auto rt = findRuntime(worker_id);
    if (!rt) {
        if (error) *error = "worker not registered: " + worker_id;
        return false;
    }
    WorkerFaultEvent crash_evt;
    bool ok = false;
    {
        std::unique_lock<std::mutex> lk(rt->state_mutex);
        ok = spawnLocked(*rt, lk, error, &crash_evt);
    }
    if (!crash_evt.worker_id.empty()) {
        notifyFault(crash_evt);
    }
    return ok;
}

bool WorkerSupervisor::stopWorker(const std::string& worker_id) {
    auto rt = findRuntime(worker_id);
    if (!rt) return false;
    stopRuntime(*rt);
    return true;
}

void WorkerSupervisor::stopByPlugin(const std::string& plugin_id) {
    if (plugin_id.empty()) return;
    std::vector<std::shared_ptr<WorkerRuntime>> matches;
    {
        std::lock_guard lock(mutex_);
        for (const auto& [_, rt] : workers_) {
            if (rt && rt->spec.plugin_id == plugin_id) {
                matches.push_back(rt);
            }
        }
    }
    for (auto& rt : matches) {
        stopRuntime(*rt);
    }
}

void WorkerSupervisor::stopAll() {
    std::vector<std::shared_ptr<WorkerRuntime>> all;
    {
        std::lock_guard lock(mutex_);
        for (const auto& [_, rt] : workers_) {
            if (rt) all.push_back(rt);
        }
    }
    for (auto& rt : all) {
        stopRuntime(*rt);
    }
}

size_t WorkerSupervisor::evictIdleNow() {
    std::vector<std::shared_ptr<WorkerRuntime>> all;
    bool shutting_down = false;
    {
        std::lock_guard lock(mutex_);
        shutting_down = shutting_down_;
        for (const auto& [_, rt] : workers_) {
            if (rt) all.push_back(rt);
        }
    }

    const int64_t now = steadyNowMs();
    size_t stopped = 0;
    std::vector<WorkerFaultEvent> faults;

    for (auto& rt : all) {
        bool should_stop = false;
        WorkerFaultEvent crash_evt;
        bool crashed_now = false;
        {
            std::unique_lock<std::mutex> lk(rt->state_mutex);
            reapExitedLocked(*rt, &crashed_now, &crash_evt, false);
            if ((crashed_now || rt->pending_auto_restart) && !shutting_down &&
                rt->spec.auto_restart && !rt->quarantined && !rt->stopping_child &&
                rt->in_flight == 0) {
                rt->pending_auto_restart = false;
                std::string err;
                const bool respawn_ok = spawnLocked(*rt, lk, &err);
                if (crashed_now) {
                    crash_evt.auto_restarted = true;
                    crash_evt.restart_succeeded = respawn_ok;
                    crash_evt.restart_count = rt->restart_count;
                    if (!respawn_ok && !err.empty()) {
                        crash_evt.error += "; respawn failed: " + err;
                    }
                }
            }
            if (!rt->stopping_child && rt->pid > 0 && rt->in_flight == 0 &&
                rt->spec.idle_timeout_ms > 0 &&
                (now - rt->last_active_ms) >= rt->spec.idle_timeout_ms) {
                should_stop = true;
            }
        }
        if (crashed_now) {
            faults.push_back(std::move(crash_evt));
        }
        if (should_stop) {
            stopRuntime(*rt);
            ++stopped;
        }
    }

    for (const auto& evt : faults) {
        notifyFault(evt);
    }
    return stopped;
}

void WorkerSupervisor::watchdogLoop() {
    while (true) {
        {
            std::unique_lock lock(mutex_);
            watchdog_cv_.wait_for(lock, std::chrono::milliseconds(150), [this]() {
                return shutting_down_;
            });
            if (shutting_down_) return;
        }
        evictIdleNow();
    }
}

bool WorkerSupervisor::readLineInterruptible(WorkerRuntime& rt,
                                             std::string& out_line,
                                             const std::shared_ptr<CancelToken>& cancel,
                                             int64_t deadline_ms,
                                             bool& timed_out,
                                             bool& cancelled,
                                             bool& eof) {
    timed_out = false;
    cancelled = false;
    eof = false;

    while (true) {
        auto nl = rt.stdout_buf.find('\n');
        if (nl != std::string::npos) {
            out_line = rt.stdout_buf.substr(0, nl);
            if (!out_line.empty() && out_line.back() == '\r') out_line.pop_back();
            rt.stdout_buf.erase(0, nl + 1);
            return true;
        }

        if (cancel && cancel->isCancelled()) {
            cancelled = true;
            return false;
        }

        int64_t remaining = deadline_ms - steadyNowMs();
        if (remaining <= 0) {
            timed_out = true;
            return false;
        }

        int fd = -1;
        {
            std::lock_guard lk(rt.state_mutex);
            fd = rt.stdout_fd;
        }
        if (fd < 0) {
            eof = true;
            return false;
        }

        struct pollfd pfd{fd, POLLIN, 0};
        const int slice = static_cast<int>(std::min<int64_t>(remaining, 50));
        int pr = poll(&pfd, 1, slice);
        if (pr < 0) {
            if (errno == EINTR) continue;
            eof = true;
            return false;
        }
        if (pr == 0) {
            continue;
        }

        char buf[4096];
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n > 0) {
            rt.stdout_buf.append(buf, static_cast<size_t>(n));
            continue;
        }
        if (n == 0) {
            if (!rt.stdout_buf.empty()) {
                out_line = std::move(rt.stdout_buf);
                rt.stdout_buf.clear();
                if (!out_line.empty() && out_line.back() == '\r') out_line.pop_back();
                return true;
            }
            eof = true;
            return false;
        }
        if (errno == EINTR || errno == EAGAIN) continue;
        eof = true;
        return false;
    }
}

CapabilityResult WorkerSupervisor::request(const std::string& worker_id,
                                           const nlohmann::json& payload,
                                           const std::shared_ptr<CancelToken>& cancel,
                                           int64_t timeout_ms) {
    return streamRequest(worker_id, payload, nullptr, cancel, timeout_ms);
}

CapabilityResult WorkerSupervisor::streamRequest(const std::string& worker_id,
                                                 const nlohmann::json& payload,
                                                 const StreamCallback& on_chunk,
                                                 const std::shared_ptr<CancelToken>& cancel,
                                                 int64_t timeout_ms) {
    if (cancel && cancel->isCancelled()) {
        return {false, nlohmann::json::object(), "cancelled", "", true};
    }

    auto rt = findRuntime(worker_id);
    if (!rt) {
        return {false, nlohmann::json::object(), "worker not registered: " + worker_id, "", false};
    }

    std::lock_guard io_lk(rt->io_mutex);

    struct FlightGuard {
        WorkerRuntime& rt;
        ~FlightGuard() {
            std::lock_guard lk(rt.state_mutex);
            if (rt.in_flight > 0) --rt.in_flight;
            rt.last_active_ms = steadyNowMs();
        }
    };

    const int max_attempts = rt->spec.retry_on_crash ? 2 : 1;

    for (int attempt = 0; attempt < max_attempts; ++attempt) {
        int stdin_fd = -1;
        uint64_t gen = 0;
        WorkerFaultEvent pre_crash;
        bool spawn_ok = false;
        std::string spawn_err;
        {
            std::unique_lock<std::mutex> lk(rt->state_mutex);
            spawn_ok = spawnLocked(*rt, lk, &spawn_err, &pre_crash);
            if (spawn_ok) {
                ++rt->in_flight;
                rt->last_active_ms = steadyNowMs();
                stdin_fd = rt->stdin_fd;
                gen = rt->restart_count;
            }
        }
        if (!pre_crash.worker_id.empty()) {
            notifyFault(pre_crash);
        }
        if (!spawn_ok) {
            return {false, nlohmann::json::object(), spawn_err, rt->spec.plugin_id, false};
        }

        FlightGuard guard{*rt};
        if (rt->io_generation != gen) {
            rt->stdout_buf.clear();
            rt->io_generation = gen;
        }

        auto handleUnexpectedCrash = [&](const std::string& fallback_reason,
                                         bool& out_restarted_ok) -> CapabilityResult {
            out_restarted_ok = false;
            // Briefly wait for zombie status so signal (e.g. SIGSEGV) is accurately captured.
            bool crashed = false;
            WorkerFaultEvent evt;
            const int64_t wait_end = steadyNowMs() + 100;
            while (steadyNowMs() < wait_end) {
                {
                    std::lock_guard lk(rt->state_mutex);
                    if (reapExitedLocked(*rt, &crashed, &evt, true) || rt->pid <= 0) {
                        break;
                    }
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            if (!crashed) {
                bool alive = false;
                {
                    std::lock_guard lk(rt->state_mutex);
                    alive = (rt->pid > 0);
                }
                if (alive) {
                    stopRuntime(*rt);
                }
                std::lock_guard lk(rt->state_mutex);
                rt->last_exited_unexpectedly = true;
                rt->last_crash_ms = steadyNowMs();
                ++rt->crash_count;
                rt->last_error = fallback_reason;
                evt.worker_id = rt->spec.worker_id;
                evt.plugin_id = rt->spec.plugin_id;
                evt.exit_code = rt->last_exit_code;
                evt.signal_number = rt->last_signal;
                evt.signal_name = rt->last_signal_name;
                evt.crash_count = rt->crash_count;
                evt.restart_count = rt->restart_count;
                evt.quarantined = rt->quarantined;
                evt.error = fallback_reason;
                const size_t tail = std::min<size_t>(10, rt->stderr_ring.size());
                if (tail > 0) {
                    evt.recent_stderr.assign(rt->stderr_ring.end() - tail, rt->stderr_ring.end());
                }
            }

            bool shutting_down = false;
            {
                std::lock_guard lock(mutex_);
                shutting_down = shutting_down_;
            }
            if (!shutting_down && rt->spec.auto_restart && !rt->quarantined) {
                std::unique_lock<std::mutex> lk(rt->state_mutex);
                std::string respawn_err;
                out_restarted_ok = spawnLocked(*rt, lk, &respawn_err);
                evt.auto_restarted = true;
                evt.restart_succeeded = out_restarted_ok;
                evt.restart_count = rt->restart_count;
                if (!out_restarted_ok && !respawn_err.empty()) {
                    evt.error += "; respawn failed: " + respawn_err;
                }
            }

            notifyFault(evt);
            return {false, nlohmann::json::object(), evt.error, rt->spec.plugin_id, false};
        };

        const std::string line = payload.dump() + "\n";
        if (!writeAll(stdin_fd, line)) {
            bool restarted_ok = false;
            CapabilityResult err_res = handleUnexpectedCrash("failed to write request to worker", restarted_ok);
            if (attempt + 1 < max_attempts && restarted_ok) {
                continue;
            }
            return err_res;
        }

        const int64_t deadline = steadyNowMs() + std::max<int64_t>(100, timeout_ms);
        CapabilityResult result;
        result.provider_plugin_id = rt->spec.plugin_id;
        std::string accumulated;

        bool retry_this_attempt = false;
        while (true) {
            std::string resp_line;
            bool timed_out = false;
            bool cancelled = false;
            bool eof = false;
            if (!readLineInterruptible(*rt, resp_line, cancel, deadline, timed_out, cancelled, eof)) {
                if (cancelled) {
                    stopRuntime(*rt);
                    result.ok = false;
                    result.cancelled = true;
                    result.error = "cancelled";
                    return result;
                }
                if (timed_out) {
                    stopRuntime(*rt);
                    result.ok = false;
                    result.error = "worker request timed out";
                    return result;
                }
                bool restarted_ok = false;
                CapabilityResult err_res = handleUnexpectedCrash(
                    "worker closed stream unexpectedly before returning final response", restarted_ok);
                if (attempt + 1 < max_attempts && restarted_ok) {
                    retry_this_attempt = true;
                    break;
                }
                return err_res;
            }

            if (resp_line.empty()) continue;

            nlohmann::json obj = nlohmann::json::parse(resp_line, nullptr, false);
            if (obj.is_discarded() || !obj.is_object()) {
                if (on_chunk) {
                    StreamChunk chunk;
                    chunk.delta = resp_line + "\n";
                    chunk.event = "delta";
                    try {
                        on_chunk(chunk);
                    } catch (...) {
                    }
                }
                accumulated += resp_line;
                continue;
            }

            const std::string type = obj.value("type", "");
            const bool has_done_flag = obj.contains("done") && obj["done"].is_boolean();
            const bool is_done_true = has_done_flag && obj["done"].get<bool>();
            const bool is_done_false = has_done_flag && !obj["done"].get<bool>();
            const bool is_intermediate_event =
                is_done_false || type == "delta" || type == "chunk" || type == "progress" ||
                type == "meta" || (!obj.contains("ok") && obj.contains("event") && !is_done_true);

            std::string delta = extractJsonDelta(obj);
            if (!delta.empty()) accumulated += delta;

            if (is_intermediate_event && type != "done" && type != "result" && type != "error") {
                if (on_chunk) {
                    StreamChunk chunk;
                    chunk.delta = std::move(delta);
                    chunk.data = obj.contains("data") ? obj["data"] : obj;
                    chunk.event = obj.value("event", type.empty() ? "delta" : type);
                    chunk.done = false;
                    try {
                        on_chunk(chunk);
                    } catch (...) {
                    }
                }
                continue;
            }

            const bool ok = obj.contains("ok") && obj["ok"].is_boolean()
                                ? obj["ok"].get<bool>()
                                : obj.value("error", "").empty();
            nlohmann::json final_data = obj.contains("data") ? obj["data"] : obj;
            if (!delta.empty() && on_chunk && (!final_data.is_object() || obj.contains("delta"))) {
                StreamChunk delta_chunk;
                delta_chunk.delta = delta;
                delta_chunk.data = final_data;
                delta_chunk.event = "delta";
                delta_chunk.done = false;
                try {
                    on_chunk(delta_chunk);
                } catch (...) {
                }
            }
            if (final_data.is_object() && !accumulated.empty() &&
                !final_data.contains("text") && !final_data.contains("answer")) {
                final_data["text"] = accumulated;
            }

            result.ok = ok;
            result.data = std::move(final_data);
            result.error = obj.value("error", ok ? "" : "worker returned error");
            {
                std::lock_guard lk(rt->state_mutex);
                ++rt->requests_served;
            }
            if (on_chunk) {
                StreamChunk end_chunk;
                end_chunk.data = result.data;
                end_chunk.event = result.ok ? "done" : "error";
                end_chunk.done = true;
                try {
                    on_chunk(end_chunk);
                } catch (...) {
                }
            }
            return result;
        }
        if (retry_this_attempt) {
            continue;
        }
    }

    return {false, nlohmann::json::object(), "worker retry exhausted", rt->spec.plugin_id, false};
}

std::optional<WorkerStatus> WorkerSupervisor::status(const std::string& worker_id) const {
    auto rt = findRuntime(worker_id);
    if (!rt) return std::nullopt;
    bool crashed_now = false;
    WorkerFaultEvent evt;
    WorkerStatus st;
    {
        std::lock_guard lk(rt->state_mutex);
        const_cast<WorkerSupervisor*>(this)->reapExitedLocked(*rt, &crashed_now, &evt, false);
        st.worker_id = rt->spec.worker_id;
        st.plugin_id = rt->spec.plugin_id;
        st.registered = true;
        st.running = (rt->pid > 0 || rt->stopping_child);
        st.pid = rt->pid;
        st.requests_served = rt->requests_served;
        st.restart_count = rt->restart_count;
        st.last_active_ms = rt->last_active_ms;
        st.idle_timeout_ms = rt->spec.idle_timeout_ms;
        st.last_exit_code = rt->last_exit_code;
        st.recent_stderr.assign(rt->stderr_ring.begin(), rt->stderr_ring.end());
        st.transport = rt->spec.transport;
        st.crash_count = rt->crash_count;
        st.last_signal = rt->last_signal;
        st.last_signal_name = rt->last_signal_name;
        st.last_crash_ms = rt->last_crash_ms;
        st.last_exited_unexpectedly = rt->last_exited_unexpectedly;
        st.quarantined = rt->quarantined;
        st.last_error = rt->last_error;
    }
    if (crashed_now) {
        notifyFault(evt);
    }
    return st;
}

std::vector<WorkerStatus> WorkerSupervisor::listWorkers() const {
    std::vector<std::string> ids;
    {
        std::lock_guard lock(mutex_);
        for (const auto& [id, _] : workers_) ids.push_back(id);
    }
    std::vector<WorkerStatus> out;
    for (const auto& id : ids) {
        if (auto st = status(id)) out.push_back(std::move(*st));
    }
    return out;
}

}  // namespace omnidesk
