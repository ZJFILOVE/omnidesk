#include <omnidesk/worker_supervisor.hpp>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <queue>
#include <sstream>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>
#include "shutdown_grace.hpp"

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

constexpr size_t max_frame_bytes = 8 * 1024 * 1024;
constexpr size_t max_stderr_line_bytes = 4096;

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

struct WorkerSupervisor::PendingOutbound {
    std::mutex mutex;
    std::condition_variable cv;
    StreamCallback on_chunk;
    std::shared_ptr<CancelToken> cancel;
    bool done = false;
    bool failed = false;
    bool delivered_chunk = false;
    bool saw_done_chunk = false;
    std::string accumulated;
    std::string error;
    CapabilityResult result;
};

struct WorkerSupervisor::InboundExecutor {
    struct Task {
        std::string worker_id;
        std::function<void()> fn;
    };

    struct State {
        std::mutex mutex;
        std::condition_variable cv;
        std::condition_variable done_cv;
        std::queue<Task> queue;
        std::vector<std::thread::id> worker_ids;
        std::unordered_map<std::string, size_t> in_flight_by_worker;
        size_t active_workers = 0;
        bool stopping = false;
    };

    struct WorkerEntry {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> exited;
    };

    explicit InboundExecutor(size_t num_threads = 12) {
        state->active_workers = num_threads;
        workers.reserve(num_threads);
        for (size_t i = 0; i < num_threads; ++i) {
            auto exited = std::make_shared<std::atomic<bool>>(false);
            std::thread t([st = state, exited]() { loop(st, exited); });
            {
                std::lock_guard lock(state->mutex);
                state->worker_ids.push_back(t.get_id());
            }
            workers.push_back(WorkerEntry{std::move(t), std::move(exited)});
        }
    }

    ~InboundExecutor() {
        shutdown(200);
    }

    void submit(const std::string& worker_id, std::function<void()> fn) {
        std::lock_guard lock(state->mutex);
        if (state->stopping) return;
        ++state->in_flight_by_worker[worker_id];
        state->queue.push(Task{worker_id, std::move(fn)});
        state->cv.notify_one();
    }

    bool isExecutorThread() const {
        const auto self_id = std::this_thread::get_id();
        std::lock_guard lock(state->mutex);
        for (const auto& wid : state->worker_ids) {
            if (wid == self_id) return true;
        }
        return false;
    }

    void waitForWorker(const std::string& worker_id, int64_t timeout_ms) {
        if (isExecutorThread()) return;
        std::unique_lock lock(state->mutex);
        if (state->stopping) return;
        const int64_t wait_ms = std::max<int64_t>(0, timeout_ms);
        state->done_cv.wait_for(lock, std::chrono::milliseconds(wait_ms), [&]() {
            auto it = state->in_flight_by_worker.find(worker_id);
            return it == state->in_flight_by_worker.end() || it->second == 0;
        });
    }

    void shutdown(int timeout_ms) {
        std::queue<Task> dropped;
        {
            std::lock_guard lock(state->mutex);
            if (state->stopping) return;
            state->stopping = true;
            std::swap(dropped, state->queue);
        }
        while (!dropped.empty()) {
            auto it_w = dropped.front().worker_id;
            dropped.pop();
            std::lock_guard lock(state->mutex);
            auto it = state->in_flight_by_worker.find(it_w);
            if (it != state->in_flight_by_worker.end() && it->second > 0) {
                --it->second;
                if (it->second == 0) state->in_flight_by_worker.erase(it);
            }
        }
        state->cv.notify_all();
        const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
        const auto self_id = std::this_thread::get_id();
        {
            std::unique_lock lock(state->mutex);
            if (grace_ms > 0) {
                state->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this]() {
                    return state->active_workers == 0;
                });
            }
        }
        for (auto& w : workers) {
            if (!w.thread.joinable()) continue;
            if (w.thread.get_id() == self_id || !w.exited->load(std::memory_order_acquire)) {
                w.thread.detach();
            } else {
                w.thread.join();
            }
        }
        workers.clear();
    }

    static void loop(std::shared_ptr<State> st, std::shared_ptr<std::atomic<bool>> exited) {
        struct ExitGuard {
            std::shared_ptr<State> st_ptr;
            std::shared_ptr<std::atomic<bool>> ex_ptr;
            ~ExitGuard() {
                {
                    std::lock_guard lock(st_ptr->mutex);
                    if (st_ptr->active_workers > 0) --st_ptr->active_workers;
                    st_ptr->done_cv.notify_all();
                }
                ex_ptr->store(true, std::memory_order_release);
            }
        } exit_guard{st, exited};

        while (true) {
            Task task;
            {
                std::unique_lock lock(st->mutex);
                st->cv.wait(lock, [&st]() { return st->stopping || !st->queue.empty(); });
                if (st->stopping && st->queue.empty()) return;
                task = std::move(st->queue.front());
                st->queue.pop();
            }
            try {
                if (task.fn) task.fn();
            } catch (...) {
            }
            task.fn = {};
            {
                std::lock_guard lock(st->mutex);
                auto it = st->in_flight_by_worker.find(task.worker_id);
                if (it != st->in_flight_by_worker.end() && it->second > 0) {
                    --it->second;
                    if (it->second == 0) {
                        st->in_flight_by_worker.erase(it);
                        st->done_cv.notify_all();
                    }
                }
            }
        }
    }

    std::shared_ptr<State> state = std::make_shared<State>();
    std::vector<WorkerEntry> workers;
};

WorkerSupervisor::WorkerSupervisor()
    : inbound_executor_(std::make_unique<InboundExecutor>(12)) {
    watchdog_thread_ = std::thread([this]() { watchdogLoop(); });
}

WorkerSupervisor::~WorkerSupervisor() {
    {
        std::lock_guard lock(mutex_);
        shutting_down_ = true;
    }
    watchdog_cv_.notify_all();
    stopAll();
    if (inbound_executor_) {
        inbound_executor_->shutdown(300);
    }
    if (watchdog_thread_.joinable()) {
        watchdog_thread_.join();
    }
}

void WorkerSupervisor::setWorkerCallbacks(const std::string& worker_id,
                                        InboundRequestHandler on_request,
                                        InboundEventHandler on_event,
                                        WorkerExitHandler on_exit) {
    auto rt = findRuntime(worker_id);
    if (!rt) return;
    std::lock_guard lk(rt->state_mutex);
    rt->inbound_req_handler = std::move(on_request);
    rt->inbound_evt_handler = std::move(on_event);
    rt->exit_handler = std::move(on_exit);
}

bool WorkerSupervisor::sendEvent(const std::string& worker_id, const nlohmann::json& event) {
    auto rt = findRuntime(worker_id);
    if (!rt || !rt->spec.duplex) return false;
    {
        std::lock_guard lk(rt->state_mutex);
        if (rt->pid <= 0 || rt->stopping_child || rt->retired || rt->stdin_fd < 0) {
            return false;
        }
    }
    nlohmann::json wire = event.is_object() ? event : nlohmann::json::object();
    if (!wire.contains("kind")) wire["kind"] = "event";
    const std::string line = wire.dump() + "\n";
    if (line.size() > max_frame_bytes) return false;
    bool timed_out = false;
    bool cancelled = false;
    return writeLineDuplex(*rt, line, nullptr, steadyNowMs() + 100, timed_out, cancelled);
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
        {
            std::lock_guard lock(previous->state_mutex);
            previous->retired = true;
        }
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
    if (rt) {
        {
            std::lock_guard lock(rt->state_mutex);
            rt->retired = true;
        }
        stopRuntime(*rt);
    }
    return true;
}

std::shared_ptr<WorkerSupervisor::WorkerRuntime> WorkerSupervisor::findRuntime(
    const std::string& worker_id) const {
    std::lock_guard lock(mutex_);
    auto it = workers_.find(worker_id);
    return it == workers_.end() ? nullptr : it->second;
}

void WorkerSupervisor::recordFailureLocked(WorkerRuntime& rt, int pid,
                                           const std::string& error,
                                           WorkerFaultEvent& event) {
    const int64_t now = steadyNowMs();
    rt.last_crash_ms = now;
    rt.last_exited_unexpectedly = true;
    rt.last_error = error;
    ++rt.crash_count;
    rt.recent_crash_times_ms.push_back(now);
    const int64_t window_ms = std::max<int64_t>(1000, rt.spec.restart_window_ms);
    while (!rt.recent_crash_times_ms.empty() &&
           now - rt.recent_crash_times_ms.front() > window_ms) {
        rt.recent_crash_times_ms.pop_front();
    }
    rt.quarantined = rt.quarantined ||
        rt.recent_crash_times_ms.size() > static_cast<size_t>(std::max(1, rt.spec.max_restarts_in_window));
    rt.pending_auto_restart = rt.spec.auto_restart && !rt.quarantined;
    event.worker_id = rt.spec.worker_id;
    event.plugin_id = rt.spec.plugin_id;
    event.pid = pid;
    event.exit_code = rt.last_exit_code;
    event.signal_number = rt.last_signal;
    event.signal_name = rt.last_signal_name;
    event.crash_count = rt.crash_count;
    event.restart_count = rt.restart_count;
    event.quarantined = rt.quarantined;
    event.error = error;
    const size_t tail = std::min<size_t>(10, rt.stderr_ring.size());
    event.recent_stderr.assign(rt.stderr_ring.end() - tail, rt.stderr_ring.end());
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
    if (res == dead_pid) kill(-dead_pid, SIGKILL);
    rt.pid = -1;
    closeIoFdsLocked(rt);
    rt.pending_exit_notify = true;

    const bool crashed = !rt.stopping_child && (force_unexpected || sig != 0 || exit_code != 0);
    if (crashed) {
        rt.last_exit_code = exit_code;
        rt.last_signal = sig;
        rt.last_signal_name = sig_name;

        std::string err = "worker process crashed";
        if (!sig_name.empty()) {
            err += " (" + sig_name + ")";
        } else if (exit_code != 0) {
            err += " (exit=" + std::to_string(exit_code) + ")";
        } else {
            err += " (unexpected EOF)";
        }
        WorkerFaultEvent event;
        recordFailureLocked(rt, dead_pid, err, event);
        if (unexpected_crash) *unexpected_crash = true;
        if (crash_event) {
            *crash_event = std::move(event);
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

bool WorkerSupervisor::spawnLocked(const std::shared_ptr<WorkerRuntime>& rt_ptr,
                                   std::unique_lock<std::mutex>& lk,
                                   std::string* error,
                                   WorkerFaultEvent* crash_out) {
    WorkerRuntime& rt = *rt_ptr;
    ignoreSigpipeOnce();
    rt.lifecycle_cv.wait(lk, [&rt]() { return !rt.stopping_child; });
    if (rt.retired) {
        if (error) *error = "worker unregistered";
        return false;
    }

    bool crashed_prior = false;
    WorkerFaultEvent local_crash;
    bool reaped_prior = reapExitedLocked(rt, &crashed_prior, &local_crash, false);
    bool need_exit_notify = rt.pending_exit_notify;
    if (crashed_prior && crash_out) {
        *crash_out = local_crash;
    }
    if (rt.pid > 0) return true;
    if (rt.quarantined) {
        if (need_exit_notify && rt.exit_handler) {
            rt.pending_exit_notify = false;
            auto exit_cb = rt.exit_handler;
            const std::string wid = rt.spec.worker_id;
            const std::string pid_str = rt.spec.plugin_id;
            lk.unlock();
            try { exit_cb(wid, pid_str); } catch (...) {}
            lk.lock();
        }
        if (error) {
            *error = "worker quarantined after repeated crashes (" + rt.last_error + ")";
        }
        return false;
    }

    closeIoFdsLocked(rt);
    closeFd(rt.stderr_fd);
    if (rt.reader_thread.joinable() || rt.stderr_thread.joinable() || (need_exit_notify && rt.exit_handler)) {
        rt.stopping_child = true;
        std::thread reader_join = std::move(rt.reader_thread);
        std::thread err_join = std::move(rt.stderr_thread);
        auto exit_cb = need_exit_notify ? rt.exit_handler : WorkerExitHandler{};
        if (need_exit_notify) rt.pending_exit_notify = false;
        const std::string wid = rt.spec.worker_id;
        const std::string pid_str = rt.spec.plugin_id;
        lk.unlock();
        if (reader_join.joinable()) reader_join.join();
        if (err_join.joinable()) err_join.join();
        if (exit_cb) {
            try { exit_cb(wid, pid_str); } catch (...) {}
        }
        lk.lock();
        rt.stopping_child = false;
        rt.lifecycle_cv.notify_all();
        reapExitedLocked(rt, nullptr, nullptr, false);
        if (rt.pid > 0) return true;
        if (rt.retired) {
            if (error) *error = "worker unregistered";
            return false;
        }
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
    int sock_pair[2] = {-1, -1};
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
            sock_path.clear();
            // Fallback to unnamed AF_UNIX socketpair when filesystem bind() is blocked
            // by seccomp sandboxes (EPERM/EACCES) or read-only /tmp.
            if (socketpair(AF_UNIX, SOCK_STREAM, 0, sock_pair) != 0) {
                closeFd(err_pipe[0]);
                closeFd(err_pipe[1]);
                if (error) {
                    *error = std::string("bind/listen AF_UNIX and socketpair fallback failed: ") +
                             std::strerror(errno);
                }
                return false;
            }
            setCloexec(sock_pair[0]);
        } else {
            chmod(sock_path.c_str(), 0600);
        }
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
        for (int fd : {in_pipe[0], in_pipe[1], out_pipe[0], out_pipe[1],
                       err_pipe[0], err_pipe[1], listen_fd, sock_pair[0], sock_pair[1]}) {
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
            if (sock_pair[0] >= 0) close(sock_pair[0]);
            if (sock_pair[1] >= 0) {
                int fd_flags = fcntl(sock_pair[1], F_GETFD, 0);
                if (fd_flags >= 0) {
                    fcntl(sock_pair[1], F_SETFD, fd_flags & ~FD_CLOEXEC);
                }
                const std::string fd_str = std::to_string(sock_pair[1]);
                setenv("OMNIDESK_WORKER_SOCKET_FD", fd_str.c_str(), 1);
                setenv("OMNIDESK_WORKER_SOCKET", ("fd://" + fd_str).c_str(), 1);
            } else {
                setenv("OMNIDESK_WORKER_SOCKET", sock_path.c_str(), 1);
            }
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
        if (sock_pair[0] >= 0) {
            closeFd(sock_pair[1]);
            conn_fd = sock_pair[0];
            sock_pair[0] = -1;
        } else {
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
    for (int fd : {rt.stdin_fd, rt.stdout_fd, rt.stderr_fd}) {
        int flags = fcntl(fd, F_GETFL, 0);
        if (flags >= 0) fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }
    rt.last_active_ms = steadyNowMs();
    rt.pending_auto_restart = false;
    ++rt.restart_count;
    rt.stdout_buf.clear();
    rt.io_generation = rt.restart_count;

    if (crashed_prior && crash_out) {
        crash_out->auto_restarted = true;
        crash_out->restart_succeeded = true;
        crash_out->restart_count = rt.restart_count;
    }

    int err_fd = rt.stderr_fd;
    rt.stderr_thread = std::thread([rt_ptr, err_fd]() {
        WorkerRuntime& rt = *rt_ptr;
        char buf[1024];
        std::string line;
        while (true) {
            {
                std::lock_guard lk(rt.state_mutex);
                if (rt.stderr_fd != err_fd || rt.stopping_child) break;
            }
            struct pollfd descriptor{err_fd, POLLIN, 0};
            int ready = poll(&descriptor, 1, 50);
            if (ready == 0 || (ready < 0 && errno == EINTR)) continue;
            if (ready < 0) break;
            ssize_t n = read(err_fd, buf, sizeof(buf));
            if (n <= 0) {
                if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
                break;
            }
            for (ssize_t i = 0; i < n; ++i) {
                if (buf[i] == '\r') continue;
                if (buf[i] == '\n' || line.size() >= max_stderr_line_bytes) {
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

    if (rt.spec.duplex) {
        const uint64_t gen = rt.io_generation;
        {
            std::lock_guard olk(rt.outbound_mutex);
            rt.reader_active = true;
        }
        rt.reader_thread = std::thread([this, rt_ptr, gen]() {
            readerLoopDuplex(rt_ptr, gen);
        });
    }

    return true;
}

void WorkerSupervisor::stopRuntime(WorkerRuntime& rt) {
    int pid = -1;
    int64_t grace_ms = 1200;
    bool is_duplex = false;
    std::string wid;
    std::string pid_str;
    {
        std::unique_lock<std::mutex> lk(rt.state_mutex);
        rt.lifecycle_cv.wait(lk, [&rt]() { return !rt.stopping_child; });
        ++rt.stop_generation;
        if (rt.pid <= 0 && rt.stdin_fd < 0 && rt.stdout_fd < 0 && rt.stderr_fd < 0 &&
            !rt.stderr_thread.joinable() && !rt.reader_thread.joinable()) {
            if (!rt.active_socket_path.empty()) {
                ::unlink(rt.active_socket_path.c_str());
                rt.active_socket_path.clear();
            }
            return;
        }
        rt.stopping_child = true;
        pid = rt.pid;
        grace_ms = std::max<int64_t>(50, rt.spec.stop_grace_ms);
        is_duplex = rt.spec.duplex;
        wid = rt.spec.worker_id;
        pid_str = rt.spec.plugin_id;
        if (!is_duplex) {
            if (rt.shared_io_fd) {
                if (rt.stdin_fd >= 0) ::shutdown(rt.stdin_fd, SHUT_WR);
            } else {
                closeFd(rt.stdin_fd);
            }
        }
    }

    if (is_duplex) {
        {
            std::lock_guard ilk(rt.inbound_mutex);
            for (auto& [_, tok] : rt.active_inbound) {
                if (tok) tok->cancel();
            }
        }
        std::unique_lock<std::timed_mutex> wlk(rt.write_mutex, std::chrono::milliseconds(100));
        std::lock_guard lk(rt.state_mutex);
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

    if (pid > 0) kill(-pid, SIGKILL);
    std::thread err_join;
    std::thread reader_join;
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
        if (rt.reader_thread.joinable()) {
            reader_join = std::move(rt.reader_thread);
        }
        closeIoFdsLocked(rt);
        if (rt.stderr_thread.joinable()) {
            err_join = std::move(rt.stderr_thread);
        }
    }
    if (reader_join.joinable()) {
        reader_join.join();
    }
    if (err_join.joinable()) {
        err_join.join();
    }
    if (is_duplex && inbound_executor_) {
        inbound_executor_->waitForWorker(wid, grace_ms);
    }
    WorkerExitHandler exit_cb;
    {
        std::lock_guard lk(rt.state_mutex);
        closeFd(rt.stderr_fd);
        rt.stopping_child = false;
        rt.pending_exit_notify = false;
        exit_cb = rt.exit_handler;
    }
    rt.lifecycle_cv.notify_all();
    if (exit_cb) {
        try { exit_cb(wid, pid_str); } catch (...) {}
    }
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
        ok = spawnLocked(rt, lk, error, &crash_evt);
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
        bool reaped_now = false;
        WorkerExitHandler exit_cb;
        std::string wid;
        std::string pid_str;
        {
            std::unique_lock<std::mutex> lk(rt->state_mutex);
            if (rt->in_flight > 0 || rt->retired) continue;
            reaped_now = reapExitedLocked(*rt, &crashed_now, &crash_evt, false);
            if ((crashed_now || rt->pending_auto_restart) && !shutting_down &&
                rt->spec.auto_restart && !rt->quarantined && !rt->stopping_child &&
                rt->in_flight == 0) {
                rt->pending_auto_restart = false;
                std::string err;
                const bool respawn_ok = spawnLocked(rt, lk, &err);
                if (crashed_now) {
                    crash_evt.auto_restarted = true;
                    crash_evt.restart_succeeded = respawn_ok;
                    crash_evt.restart_count = rt->restart_count;
                    if (!respawn_ok && !err.empty()) {
                        crash_evt.error += "; respawn failed: " + err;
                    }
                }
            } else if (reaped_now || rt->pending_exit_notify) {
                rt->pending_exit_notify = false;
                exit_cb = rt->exit_handler;
                wid = rt->spec.worker_id;
                pid_str = rt->spec.plugin_id;
            }
            if (!rt->stopping_child && rt->pid > 0 && rt->in_flight == 0 &&
                rt->spec.idle_timeout_ms > 0 &&
                (now - rt->last_active_ms) >= rt->spec.idle_timeout_ms) {
                should_stop = true;
            }
        }
        if (exit_cb) {
            try { exit_cb(wid, pid_str); } catch (...) {}
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

bool WorkerSupervisor::writeLineInterruptible(WorkerRuntime& rt,
                                              const std::string& line,
                                              const std::shared_ptr<CancelToken>& cancel,
                                              int64_t deadline_ms,
                                              bool& timed_out,
                                              bool& cancelled) {
    size_t sent = 0;
    while (sent < line.size()) {
        if (cancel && cancel->isCancelled()) {
            cancelled = true;
            return false;
        }
        const int64_t remaining = deadline_ms - steadyNowMs();
        if (remaining <= 0) {
            timed_out = true;
            return false;
        }
        int descriptor = -1;
        ssize_t written = -1;
        {
            std::lock_guard lock(rt.state_mutex);
            descriptor = rt.stdin_fd;
            if (descriptor < 0 || rt.stopping_child) return false;
            written = write(descriptor, line.data() + sent, line.size() - sent);
        }
        if (written > 0) {
            sent += static_cast<size_t>(written);
            continue;
        }
        if (written < 0 && errno == EINTR) continue;
        if (written >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK)) return false;
        struct pollfd poll_descriptor{descriptor, POLLOUT, 0};
        int ready = poll(&poll_descriptor, 1, static_cast<int>(std::min<int64_t>(remaining, 50)));
        if (ready < 0 && errno != EINTR) return false;
    }
    return true;
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
        if (cancel && cancel->isCancelled()) {
            cancelled = true;
            return false;
        }

        auto nl = rt.stdout_buf.find('\n');
        if (nl != std::string::npos) {
            out_line = rt.stdout_buf.substr(0, nl);
            if (!out_line.empty() && out_line.back() == '\r') out_line.pop_back();
            rt.stdout_buf.erase(0, nl + 1);
            return true;
        }
        if (rt.stdout_buf.size() >= max_frame_bytes) {
            eof = true;
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
        ssize_t n = -1;
        {
            std::lock_guard lock(rt.state_mutex);
            if (fd != rt.stdout_fd || rt.stopping_child) {
                eof = true;
                return false;
            }
            n = read(fd, buf, sizeof(buf));
        }
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

bool WorkerSupervisor::writeLineDuplex(WorkerRuntime& rt,
                                       const std::string& line,
                                       const std::shared_ptr<CancelToken>& cancel,
                                       int64_t deadline_ms,
                                       bool& timed_out,
                                       bool& cancelled) {
    timed_out = false;
    cancelled = false;
    std::unique_lock<std::timed_mutex> wlk(rt.write_mutex, std::defer_lock);
    while (!wlk.try_lock_for(std::chrono::milliseconds(20))) {
        if (cancel && cancel->isCancelled()) {
            cancelled = true;
            return false;
        }
        {
            std::lock_guard lk(rt.state_mutex);
            if (rt.stopping_child || rt.retired || rt.stdin_fd < 0) return false;
        }
        if (steadyNowMs() >= deadline_ms) {
            timed_out = true;
            return false;
        }
    }

    size_t sent = 0;
    while (sent < line.size()) {
        if (cancel && cancel->isCancelled()) {
            cancelled = true;
            return false;
        }
        const int64_t remaining = deadline_ms - steadyNowMs();
        if (remaining <= 0) {
            timed_out = true;
            return false;
        }
        int descriptor = -1;
        ssize_t written = -1;
        {
            std::lock_guard lock(rt.state_mutex);
            descriptor = rt.stdin_fd;
            if (descriptor < 0 || rt.stopping_child || rt.retired) return false;
            written = write(descriptor, line.data() + sent, line.size() - sent);
        }
        if (written > 0) {
            sent += static_cast<size_t>(written);
            continue;
        }
        if (written < 0 && errno == EINTR) continue;
        if (written >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK)) return false;
        struct pollfd poll_descriptor{descriptor, POLLOUT, 0};
        int ready = poll(&poll_descriptor, 1, static_cast<int>(std::min<int64_t>(remaining, 25)));
        if (ready < 0 && errno != EINTR) return false;
    }
    return true;
}

void WorkerSupervisor::readerLoopDuplex(std::shared_ptr<WorkerRuntime> rt, uint64_t gen) {
    auto failAllPending = [&](const std::string& reason) {
        std::unordered_map<std::string, std::shared_ptr<PendingOutbound>> drained;
        {
            std::lock_guard olk(rt->outbound_mutex);
            rt->reader_active = false;
            std::swap(drained, rt->pending_outbound);
        }
        for (auto& [_, p] : drained) {
            std::lock_guard plk(p->mutex);
            if (!p->done && !p->failed) {
                p->failed = true;
                p->error = reason;
            }
            p->cv.notify_all();
        }
        std::lock_guard ilk(rt->inbound_mutex);
        for (auto& [_, tok] : rt->active_inbound) {
            if (tok) tok->cancel();
        }
    };

    std::string fail_reason = "worker closed stream unexpectedly before returning final response";
    while (true) {
        int fd = -1;
        {
            std::lock_guard lk(rt->state_mutex);
            if (rt->stopping_child || rt->retired || rt->io_generation != gen) {
                fail_reason = "worker stopped";
                break;
            }
            fd = rt->stdout_fd;
        }
        if (fd < 0) break;

        auto nl = rt->stdout_buf.find('\n');
        if (nl == std::string::npos) {
            if (rt->stdout_buf.size() >= max_frame_bytes) {
                fail_reason = "worker response exceeds 8 MiB frame limit";
                break;
            }
            struct pollfd pfd{fd, POLLIN, 0};
            int pr = poll(&pfd, 1, 50);
            if (pr < 0) {
                if (errno == EINTR) continue;
                break;
            }
            if (pr == 0) continue;
            if (pfd.revents & POLLNVAL) break;

            char buf[4096];
            ssize_t n = -1;
            {
                std::lock_guard lk(rt->state_mutex);
                if (fd != rt->stdout_fd || rt->stopping_child || rt->retired || rt->io_generation != gen) {
                    fail_reason = "worker stopped";
                    break;
                }
                n = read(fd, buf, sizeof(buf));
            }
            if (n > 0) {
                rt->stdout_buf.append(buf, static_cast<size_t>(n));
                continue;
            }
            if (n == 0) {
                if (rt->stdout_buf.empty()) break;
                nl = rt->stdout_buf.size();
            } else {
                if (errno == EINTR || errno == EAGAIN) continue;
                break;
            }
        }

        std::string line = rt->stdout_buf.substr(0, nl);
        if (nl < rt->stdout_buf.size()) {
            rt->stdout_buf.erase(0, nl + 1);
        } else {
            rt->stdout_buf.clear();
        }
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;

        nlohmann::json obj = nlohmann::json::parse(line, nullptr, false);
        if (obj.is_discarded() || !obj.is_object()) {
            continue;
        }

        const std::string kind = obj.value("kind", "");
        const std::string id = obj.value("id", "");

        if (kind == "chunk" && !id.empty()) {
            std::shared_ptr<PendingOutbound> pending;
            {
                std::lock_guard olk(rt->outbound_mutex);
                auto it = rt->pending_outbound.find(id);
                if (it != rt->pending_outbound.end()) pending = it->second;
            }
            if (!pending) continue;
            if ((obj.contains("type") && !obj["type"].is_string()) ||
                (obj.contains("event") && !obj["event"].is_string())) {
                std::lock_guard plk(pending->mutex);
                pending->failed = true;
                pending->error = "invalid worker chunk envelope";
                pending->cv.notify_all();
                continue;
            }
            std::string delta = extractJsonDelta(obj);
            StreamCallback cb;
            {
                std::lock_guard plk(pending->mutex);
                if (!delta.empty()) {
                    pending->accumulated += delta;
                    if (pending->accumulated.size() > max_frame_bytes) {
                        pending->failed = true;
                        pending->error = "worker stream exceeds 8 MiB limit";
                        pending->cv.notify_all();
                        continue;
                    }
                }
                cb = pending->on_chunk;
                if (cb) pending->delivered_chunk = true;
            }
            if (cb) {
                StreamChunk chunk;
                chunk.delta = std::move(delta);
                chunk.data = obj.contains("data") ? obj["data"] : obj;
                chunk.event = obj.value("event", obj.value("type", "delta"));
                chunk.done = false;
                try { cb(chunk); } catch (...) {}
            }
            continue;
        }

        if (kind == "resp" && !id.empty()) {
            std::shared_ptr<PendingOutbound> pending;
            {
                std::lock_guard olk(rt->outbound_mutex);
                auto it = rt->pending_outbound.find(id);
                if (it != rt->pending_outbound.end()) {
                    pending = std::move(it->second);
                    rt->pending_outbound.erase(it);
                }
            }
            if (!pending) continue;
            if ((obj.contains("ok") && !obj["ok"].is_boolean()) ||
                (obj.contains("error") && !obj["error"].is_string()) ||
                (obj.contains("cancelled") && !obj["cancelled"].is_boolean())) {
                std::lock_guard plk(pending->mutex);
                pending->failed = true;
                pending->error = "invalid worker response envelope";
                pending->cv.notify_all();
                continue;
            }
            const bool ok = obj.contains("ok") ? obj["ok"].get<bool>() : obj.value("error", "").empty();
            const bool was_cancelled = obj.value("cancelled", false);
            nlohmann::json final_data = obj.contains("data") ? obj["data"] : obj;
            StreamCallback cb;
            CapabilityResult res;
            {
                std::lock_guard plk(pending->mutex);
                if (final_data.is_object() && !pending->accumulated.empty() &&
                    !final_data.contains("text") && !final_data.contains("answer")) {
                    final_data["text"] = pending->accumulated;
                }
                res.ok = ok;
                res.data = std::move(final_data);
                res.error = obj.value("error", ok ? "" : (was_cancelled ? "cancelled" : "worker returned error"));
                res.cancelled = was_cancelled;
                res.provider_plugin_id = obj.value("provider_plugin_id", rt->spec.plugin_id);
                if (res.provider_plugin_id.empty()) res.provider_plugin_id = rt->spec.plugin_id;
                cb = pending->on_chunk;
                pending->result = res;
                pending->done = true;
                if (cb) pending->saw_done_chunk = true;
            }
            if (cb) {
                StreamChunk end_chunk;
                end_chunk.data = res.data;
                end_chunk.event = res.cancelled ? "cancelled" : (res.ok ? "done" : "error");
                end_chunk.done = true;
                try { cb(end_chunk); } catch (...) {}
            }
            pending->cv.notify_all();
            continue;
        }

        if (kind == "cancel" && !id.empty()) {
            std::shared_ptr<CancelToken> tok;
            {
                std::lock_guard ilk(rt->inbound_mutex);
                auto it = rt->active_inbound.find(id);
                if (it != rt->active_inbound.end()) tok = it->second;
            }
            if (tok) tok->cancel();
            continue;
        }

        if (kind == "event") {
            InboundEventHandler evt_handler;
            std::string wid, pid_str;
            {
                std::lock_guard lk(rt->state_mutex);
                evt_handler = rt->inbound_evt_handler;
                wid = rt->spec.worker_id;
                pid_str = rt->spec.plugin_id;
            }
            if (evt_handler && inbound_executor_) {
                inbound_executor_->submit(wid, [evt_handler = std::move(evt_handler), wid, pid_str, obj = std::move(obj)]() {
                    evt_handler(wid, pid_str, obj);
                });
            }
            continue;
        }

        if (kind == "req" && !id.empty()) {
            InboundRequestHandler req_handler;
            std::string wid, pid_str;
            {
                std::lock_guard lk(rt->state_mutex);
                req_handler = rt->inbound_req_handler;
                wid = rt->spec.worker_id;
                pid_str = rt->spec.plugin_id;
            }
            auto cancel_tok = CancelToken::create();
            {
                std::lock_guard ilk(rt->inbound_mutex);
                rt->active_inbound[id] = cancel_tok;
            }
            if (!inbound_executor_) {
                std::lock_guard ilk(rt->inbound_mutex);
                rt->active_inbound.erase(id);
                continue;
            }
            const bool wants_stream = obj.value("stream", false);
            inbound_executor_->submit(wid, [this, rt, gen, id, wid, pid_str, obj = std::move(obj),
                                            cancel_tok, req_handler = std::move(req_handler), wants_stream]() {
                auto canSend = [&]() -> bool {
                    std::lock_guard lk(rt->state_mutex);
                    return !rt->stopping_child && !rt->retired && rt->io_generation == gen && rt->stdin_fd >= 0;
                };
                CapabilityResult res;
                if (!req_handler) {
                    res = {false, nlohmann::json::object(), "no inbound host handler registered", "", false};
                } else {
                    StreamCallback stream_cb;
                    if (wants_stream) {
                        stream_cb = [this, &rt, &id, &canSend](const StreamChunk& chunk) {
                            if (chunk.done || !canSend()) return;
                            nlohmann::json cwire = {
                                {"kind", "chunk"},
                                {"id", id},
                                {"type", "delta"},
                                {"done", false},
                                {"event", chunk.event.empty() ? "delta" : chunk.event},
                                {"delta", chunk.delta},
                                {"data", chunk.data},
                            };
                            bool to = false, ca = false;
                            writeLineDuplex(*rt, cwire.dump() + "\n", nullptr, steadyNowMs() + 1000, to, ca);
                        };
                    }
                    try {
                        res = req_handler(wid, pid_str, obj, cancel_tok, stream_cb);
                    } catch (const std::exception& e) {
                        res = {false, nlohmann::json::object(), e.what(), "", false};
                    } catch (...) {
                        res = {false, nlohmann::json::object(), "host inbound handler exception", "", false};
                    }
                }
                {
                    std::lock_guard ilk(rt->inbound_mutex);
                    rt->active_inbound.erase(id);
                }
                if (canSend()) {
                    nlohmann::json rwire = {
                        {"kind", "resp"},
                        {"id", id},
                        {"done", true},
                        {"ok", res.ok},
                        {"data", res.data.is_null() ? nlohmann::json::object() : res.data},
                        {"error", res.error},
                        {"cancelled", res.cancelled},
                        {"provider_plugin_id", res.provider_plugin_id},
                    };
                    bool to = false, ca = false;
                    writeLineDuplex(*rt, rwire.dump() + "\n", nullptr, steadyNowMs() + 2000, to, ca);
                }
            });
            continue;
        }
    }

    failAllPending(fail_reason);
}

CapabilityResult WorkerSupervisor::streamRequestDuplex(const std::shared_ptr<WorkerRuntime>& rt,
                                                       const nlohmann::json& payload,
                                                       const StreamCallback& on_chunk,
                                                       const std::shared_ptr<CancelToken>& cancel,
                                                       int64_t timeout_ms) {
    const int64_t deadline = steadyNowMs() + std::max<int64_t>(100, timeout_ms);
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
        uint64_t gen = 0;
        uint64_t stop_generation = 0;
        WorkerFaultEvent pre_crash;
        bool spawn_ok = false;
        std::string spawn_err;
        {
            std::unique_lock<std::mutex> lk(rt->state_mutex);
            spawn_ok = spawnLocked(rt, lk, &spawn_err, &pre_crash);
            if (spawn_ok) {
                ++rt->in_flight;
                rt->last_active_ms = steadyNowMs();
                gen = rt->io_generation;
                stop_generation = rt->stop_generation;
            }
        }
        if (!pre_crash.worker_id.empty()) {
            notifyFault(pre_crash);
        }
        if (!spawn_ok) {
            return {false, nlohmann::json::object(), spawn_err, rt->spec.plugin_id, false};
        }

        FlightGuard guard{*rt};
        const std::string req_id = "h-" + std::to_string(rt->next_req_seq.fetch_add(1));
        nlohmann::json wire = payload.is_object() ? payload : nlohmann::json::object();
        wire["kind"] = "req";
        wire["id"] = req_id;
        if (on_chunk && !wire.contains("stream")) {
            wire["stream"] = true;
        }
        const std::string line = wire.dump() + "\n";
        if (line.size() > max_frame_bytes) {
            return {false, nlohmann::json::object(), "worker request exceeds 8 MiB frame limit",
                    rt->spec.plugin_id, false};
        }

        auto pending = std::make_shared<PendingOutbound>();
        pending->on_chunk = on_chunk;
        pending->cancel = cancel;
        bool reader_dead = false;
        {
            std::lock_guard olk(rt->outbound_mutex);
            if (!rt->reader_active) {
                reader_dead = true;
            } else {
                rt->pending_outbound[req_id] = pending;
            }
        }

        auto handleUnexpectedCrashDuplex = [&](const std::string& fallback_reason,
                                               bool& out_restarted_ok) -> CapabilityResult {
            out_restarted_ok = false;
            {
                std::lock_guard lock(rt->state_mutex);
                if (rt->retired || rt->stop_generation != stop_generation) {
                    return {false, nlohmann::json::object(), "worker stopped", rt->spec.plugin_id, false};
                }
                if (rt->handled_crash_for_gen >= gen) {
                    return {false, nlohmann::json::object(),
                            rt->last_error.empty() ? fallback_reason : rt->last_error,
                            rt->spec.plugin_id, false};
                }
                rt->handled_crash_for_gen = gen;
            }

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
                int failed_pid = -1;
                {
                    std::lock_guard lk(rt->state_mutex);
                    alive = (rt->pid > 0);
                    failed_pid = rt->pid;
                }
                if (alive) {
                    stopRuntime(*rt);
                }
                std::lock_guard lk(rt->state_mutex);
                recordFailureLocked(*rt, failed_pid, fallback_reason, evt);
            } else {
                WorkerExitHandler exit_cb;
                std::string wid, pid_str;
                {
                    std::lock_guard lk(rt->state_mutex);
                    rt->pending_exit_notify = false;
                    exit_cb = rt->exit_handler;
                    wid = rt->spec.worker_id;
                    pid_str = rt->spec.plugin_id;
                }
                if (exit_cb) {
                    try { exit_cb(wid, pid_str); } catch (...) {}
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
                out_restarted_ok = spawnLocked(rt, lk, &respawn_err);
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

        if (reader_dead) {
            bool restarted_ok = false;
            CapabilityResult err_res = handleUnexpectedCrashDuplex(
                "worker reader closed before request was sent", restarted_ok);
            if (attempt + 1 < max_attempts && restarted_ok) {
                continue;
            }
            return err_res;
        }

        bool write_timed_out = false;
        bool write_cancelled = false;
        if (!writeLineDuplex(*rt, line, cancel, deadline, write_timed_out, write_cancelled)) {
            {
                std::lock_guard olk(rt->outbound_mutex);
                rt->pending_outbound.erase(req_id);
            }
            if (write_cancelled) {
                stopRuntime(*rt);
                return {false, nlohmann::json::object(), "cancelled", rt->spec.plugin_id, true};
            }
            bool restarted_ok = false;
            CapabilityResult err_res = handleUnexpectedCrashDuplex(
                write_timed_out ? "worker request timed out writing request" : "failed to write request to worker",
                restarted_ok);
            if (!write_timed_out && attempt + 1 < max_attempts && restarted_ok) {
                continue;
            }
            return err_res;
        }

        bool sent_cancel = false;
        int64_t cancel_deadline_ms = 0;
        bool wait_done = false;
        bool wait_failed = false;
        bool wait_delivered_chunk = false;
        std::string wait_error;
        CapabilityResult wait_result;
        {
            std::unique_lock<std::mutex> plk(pending->mutex);
            while (!pending->done && !pending->failed) {
                if (cancel && cancel->isCancelled()) {
                    if (!sent_cancel) {
                        sent_cancel = true;
                        cancel_deadline_ms = std::min(deadline, steadyNowMs() + 500);
                        plk.unlock();
                        bool cto = false, cca = false;
                        const std::string cline =
                            nlohmann::json{{"kind", "cancel"}, {"id", req_id}}.dump() + "\n";
                        writeLineDuplex(*rt, cline, nullptr, steadyNowMs() + 100, cto, cca);
                        plk.lock();
                        continue;
                    }
                    if (steadyNowMs() >= cancel_deadline_ms) {
                        break;
                    }
                }
                const int64_t now = steadyNowMs();
                const int64_t target = sent_cancel ? cancel_deadline_ms : deadline;
                if (now >= target) break;
                const int64_t slice = std::min<int64_t>(target - now, 25);
                pending->cv.wait_for(plk, std::chrono::milliseconds(slice));
            }
            wait_done = pending->done;
            wait_failed = pending->failed;
            wait_delivered_chunk = pending->delivered_chunk;
            wait_error = pending->error;
            wait_result = pending->result;
        }

        {
            std::lock_guard olk(rt->outbound_mutex);
            rt->pending_outbound.erase(req_id);
        }

        if (wait_done) {
            std::lock_guard lk(rt->state_mutex);
            ++rt->requests_served;
            return wait_result;
        }
        if (cancel && cancel->isCancelled()) {
            stopRuntime(*rt);
            return {false, nlohmann::json::object(), "cancelled", rt->spec.plugin_id, true};
        }
        if (wait_failed) {
            bool restarted_ok = false;
            CapabilityResult err_res = handleUnexpectedCrashDuplex(
                wait_error.empty() ? "worker closed stream unexpectedly before returning final response"
                                   : wait_error,
                restarted_ok);
            if (!wait_delivered_chunk && attempt + 1 < max_attempts && restarted_ok) {
                continue;
            }
            return err_res;
        }
        bool restarted_ok = false;
        return handleUnexpectedCrashDuplex("worker request timed out", restarted_ok);
    }

    return {false, nlohmann::json::object(), "worker retry exhausted", rt->spec.plugin_id, false};
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

    if (rt->spec.duplex) {
        return streamRequestDuplex(rt, payload, on_chunk, cancel, timeout_ms);
    }

    const int64_t deadline = steadyNowMs() + std::max<int64_t>(100, timeout_ms);
    std::unique_lock<std::timed_mutex> io_lk(rt->io_mutex, std::defer_lock);
    while (!io_lk.try_lock_for(std::chrono::milliseconds(25))) {
        if (cancel && cancel->isCancelled()) {
            return {false, nlohmann::json::object(), "cancelled", rt->spec.plugin_id, true};
        }
        if (steadyNowMs() >= deadline) {
            return {false, nlohmann::json::object(), "worker request timed out waiting for pending request",
                    rt->spec.plugin_id, false};
        }
    }
    const std::string line = payload.dump() + "\n";
    if (line.size() > max_frame_bytes) {
        return {false, nlohmann::json::object(), "worker request exceeds 8 MiB frame limit",
                rt->spec.plugin_id, false};
    }

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
        uint64_t gen = 0;
        uint64_t stop_generation = 0;
        WorkerFaultEvent pre_crash;
        bool spawn_ok = false;
        std::string spawn_err;
        {
            std::unique_lock<std::mutex> lk(rt->state_mutex);
            spawn_ok = spawnLocked(rt, lk, &spawn_err, &pre_crash);
            if (spawn_ok) {
                ++rt->in_flight;
                rt->last_active_ms = steadyNowMs();
                gen = rt->restart_count;
                stop_generation = rt->stop_generation;
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
            {
                std::lock_guard lock(rt->state_mutex);
                if (rt->retired || rt->stop_generation != stop_generation) {
                    return {false, nlohmann::json::object(), "worker stopped", rt->spec.plugin_id, false};
                }
            }
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
                int failed_pid = -1;
                {
                    std::lock_guard lk(rt->state_mutex);
                    alive = (rt->pid > 0);
                    failed_pid = rt->pid;
                }
                if (alive) {
                    stopRuntime(*rt);
                }
                std::lock_guard lk(rt->state_mutex);
                recordFailureLocked(*rt, failed_pid, fallback_reason, evt);
            }

            bool shutting_down = false;
            {
                std::lock_guard lock(mutex_);
                shutting_down = shutting_down_;
            }
            if (!shutting_down && rt->spec.auto_restart && !rt->quarantined) {
                std::unique_lock<std::mutex> lk(rt->state_mutex);
                std::string respawn_err;
                out_restarted_ok = spawnLocked(rt, lk, &respawn_err);
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

        bool write_timed_out = false;
        bool write_cancelled = false;
        if (!writeLineInterruptible(*rt, line, cancel, deadline, write_timed_out, write_cancelled)) {
            if (write_cancelled) {
                stopRuntime(*rt);
                return {false, nlohmann::json::object(), "cancelled", rt->spec.plugin_id, true};
            }
            bool restarted_ok = false;
            CapabilityResult err_res = handleUnexpectedCrash(
                write_timed_out ? "worker request timed out writing request" : "failed to write request to worker",
                restarted_ok);
            if (!write_timed_out && attempt + 1 < max_attempts && restarted_ok) {
                continue;
            }
            return err_res;
        }

        CapabilityResult result;
        result.provider_plugin_id = rt->spec.plugin_id;
        std::string accumulated;
        bool delivered_chunk = false;

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
                    bool restarted_ok = false;
                    return handleUnexpectedCrash("worker request timed out", restarted_ok);
                }
                bool restarted_ok = false;
                CapabilityResult err_res = handleUnexpectedCrash(
                    rt->stdout_buf.size() >= max_frame_bytes
                        ? "worker response exceeds 8 MiB frame limit"
                        : "worker closed stream unexpectedly before returning final response", restarted_ok);
                if (!delivered_chunk && attempt + 1 < max_attempts && restarted_ok) {
                    retry_this_attempt = true;
                    break;
                }
                return err_res;
            }

            if (resp_line.empty()) continue;

            nlohmann::json obj = nlohmann::json::parse(resp_line, nullptr, false);
            if (obj.is_discarded() || !obj.is_object()) {
                if (on_chunk) {
                    delivered_chunk = true;
                    StreamChunk chunk;
                    chunk.delta = resp_line + "\n";
                    chunk.event = "delta";
                    try {
                        on_chunk(chunk);
                    } catch (...) {
                    }
                }
                accumulated += resp_line;
                if (accumulated.size() > max_frame_bytes) {
                    bool restarted_ok = false;
                    return handleUnexpectedCrash("worker stream exceeds 8 MiB limit", restarted_ok);
                }
                continue;
            }

            if ((obj.contains("type") && !obj["type"].is_string()) ||
                (obj.contains("event") && !obj["event"].is_string()) ||
                (obj.contains("error") && !obj["error"].is_string()) ||
                (obj.contains("ok") && !obj["ok"].is_boolean()) ||
                (obj.contains("done") && !obj["done"].is_boolean())) {
                bool restarted_ok = false;
                return handleUnexpectedCrash("invalid worker response envelope", restarted_ok);
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
            if (accumulated.size() > max_frame_bytes) {
                bool restarted_ok = false;
                return handleUnexpectedCrash("worker stream exceeds 8 MiB limit", restarted_ok);
            }

            if (is_intermediate_event && type != "done" && type != "result" && type != "error") {
                if (on_chunk) {
                    delivered_chunk = true;
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
        if (rt->in_flight == 0) {
            const_cast<WorkerSupervisor*>(this)->reapExitedLocked(*rt, &crashed_now, &evt, false);
        }
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
