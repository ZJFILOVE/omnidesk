#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstddef>
#include <cstring>
#include <dlfcn.h>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <poll.h>
#include <queue>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>
#include <unordered_map>
#include <vector>

#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/plugin.hpp"
#include "omnidesk/plugin_abi.h"
#include "omnidesk/types_json.hpp"
#include "omnidesk/worker_supervisor.hpp"

namespace fs = std::filesystem;

namespace {

volatile sig_atomic_t g_termination_signal = 0;

void handleTerminationSignal(int sig) {
    g_termination_signal = sig;
}

bool terminationRequested() {
    return g_termination_signal != 0;
}

bool writeAllFd(int fd, const std::string& data) {
    size_t sent = 0;
    while (sent < data.size()) {
        ssize_t n = ::write(fd, data.data() + sent, data.size() - sent);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        sent += static_cast<size_t>(n);
    }
    return true;
}

class NdjsonChannel {
public:
    NdjsonChannel(int read_fd, int write_fd, bool shared_fd)
        : read_fd_(read_fd), write_fd_(write_fd), shared_fd_(shared_fd) {}

    ~NdjsonChannel() { close(); }

    void close() {
        std::lock_guard<std::mutex> lk(write_mutex_);
        if (read_fd_ >= 0) {
            ::close(read_fd_);
            if (shared_fd_) write_fd_ = -1;
            read_fd_ = -1;
        }
        if (write_fd_ >= 0) {
            ::close(write_fd_);
            write_fd_ = -1;
        }
    }

    bool sendJson(const nlohmann::json& j) {
        const std::string line = j.dump() + "\n";
        std::lock_guard<std::mutex> lk(write_mutex_);
        if (write_fd_ < 0) return false;
        return writeAllFd(write_fd_, line);
    }

    bool readLine(std::string& out_line) {
        out_line.clear();
        while (!terminationRequested()) {
            size_t nl = buf_.find('\n');
            if (nl != std::string::npos) {
                out_line = buf_.substr(0, nl);
                buf_.erase(0, nl + 1);
                if (!out_line.empty() && out_line.back() == '\r') {
                    out_line.pop_back();
                }
                return true;
            }
            struct pollfd pfd{read_fd_, POLLIN, 0};
            int pr = ::poll(&pfd, 1, 50);
            if (pr < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            if (pr == 0) continue;
            if (pfd.revents & POLLNVAL) return false;
            char tmp[4096];
            ssize_t n = ::read(read_fd_, tmp, sizeof(tmp));
            if (n < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                return false;
            }
            if (n == 0) {
                if (!buf_.empty()) {
                    out_line = std::move(buf_);
                    buf_.clear();
                    if (!out_line.empty() && out_line.back() == '\r') {
                        out_line.pop_back();
                    }
                    return true;
                }
                return false;
            }
            buf_.append(tmp, static_cast<size_t>(n));
            if (buf_.size() > 8 * 1024 * 1024) return false;
        }
        return false;
    }

    bool readLineInterruptible(std::string& out_line, const std::atomic<bool>& stop_flag) {
        out_line.clear();
        while (!stop_flag.load(std::memory_order_acquire) && !terminationRequested()) {
            size_t nl = buf_.find('\n');
            if (nl != std::string::npos) {
                out_line = buf_.substr(0, nl);
                buf_.erase(0, nl + 1);
                if (!out_line.empty() && out_line.back() == '\r') {
                    out_line.pop_back();
                }
                return true;
            }
            struct pollfd pfd{read_fd_, POLLIN, 0};
            int pr = ::poll(&pfd, 1, 50);
            if (pr < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            if (pr == 0) continue;
            if (pfd.revents & POLLNVAL) return false;
            char tmp[4096];
            ssize_t n = ::read(read_fd_, tmp, sizeof(tmp));
            if (n < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                return false;
            }
            if (n == 0) {
                if (!buf_.empty()) {
                    out_line = std::move(buf_);
                    buf_.clear();
                    if (!out_line.empty() && out_line.back() == '\r') {
                        out_line.pop_back();
                    }
                    return true;
                }
                return false;
            }
            buf_.append(tmp, static_cast<size_t>(n));
            if (buf_.size() > 8 * 1024 * 1024) return false;
        }
        return false;
    }

    int readFd() const { return read_fd_; }

private:
    int read_fd_ = -1;
    int write_fd_ = -1;
    bool shared_fd_ = false;
    std::string buf_;
    std::mutex write_mutex_;
};

int connectUnixSocket(const std::string& socket_path, std::string& error) {
    if (socket_path.size() >= sizeof(sockaddr_un{}.sun_path)) {
        error = "Unix socket path too long: " + socket_path;
        return -1;
    }
    int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        error = std::string("socket(AF_UNIX) failed: ") + std::strerror(errno);
        return -1;
    }
    int flags = ::fcntl(fd, F_GETFD);
    if (flags >= 0) ::fcntl(fd, F_SETFD, flags | FD_CLOEXEC);

    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", socket_path.c_str());
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        error = std::string("connect(") + socket_path + ") failed: " + std::strerror(errno);
        ::close(fd);
        return -1;
    }
    return fd;
}

template <typename PtrVec>
auto findSourceById(const PtrVec& vec, const std::string& source_id)
    -> typename PtrVec::value_type {
    for (const auto& s : vec) {
        if (s && s->sourceId() == source_id) {
            return s;
        }
    }
    return nullptr;
}

nlohmann::json makeErr(const std::string& msg) {
    return nlohmann::json{{"ok", false}, {"data", nlohmann::json::object()}, {"error", msg}};
}

int64_t steadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

class WorkerPool {
public:
    explicit WorkerPool(size_t num_threads) {
        threads_.reserve(num_threads);
        for (size_t i = 0; i < num_threads; ++i) {
            threads_.emplace_back([this]() { loop(); });
        }
    }

    ~WorkerPool() { shutdown(); }

    void submit(std::function<void()> fn) {
        std::lock_guard lk(mutex_);
        if (stopping_) return;
        queue_.push(std::move(fn));
        cv_.notify_one();
    }

    void shutdown() {
        {
            std::lock_guard lk(mutex_);
            if (stopping_) return;
            stopping_ = true;
        }
        cv_.notify_all();
        for (auto& t : threads_) {
            if (t.joinable()) t.join();
        }
        threads_.clear();
    }

private:
    void loop() {
        while (true) {
            std::function<void()> fn;
            {
                std::unique_lock lk(mutex_);
                cv_.wait(lk, [this]() { return stopping_ || !queue_.empty(); });
                if (stopping_ && queue_.empty()) return;
                fn = std::move(queue_.front());
                queue_.pop();
            }
            try {
                if (fn) fn();
            } catch (...) {
            }
        }
    }

    std::mutex mutex_;
    std::condition_variable cv_;
    std::queue<std::function<void()>> queue_;
    std::vector<std::thread> threads_;
    bool stopping_ = false;
};

struct PendingHostCall {
    std::mutex mutex;
    std::condition_variable cv;
    omnidesk::StreamCallback on_chunk;
    bool done = false;
    bool failed = false;
    bool saw_done_chunk = false;
    std::string accumulated;
    std::string error;
    omnidesk::CapabilityResult result;
};

}  // namespace

int main(int argc, char** argv) {
    ::signal(SIGPIPE, SIG_IGN);
    struct sigaction term_sa {};
    term_sa.sa_handler = handleTerminationSignal;
    sigemptyset(&term_sa.sa_mask);
    term_sa.sa_flags = 0;
    ::sigaction(SIGTERM, &term_sa, nullptr);
    ::sigaction(SIGINT, &term_sa, nullptr);

    std::string plugin_id;
    std::string so_path;
    std::string plugin_dir;
    std::string data_dir;
    std::string config_dir;
    std::string socket_path;
    bool duplex_mode = false;
    int socket_fd_env = -1;
    if (const char* env_fd = std::getenv("OMNIDESK_WORKER_SOCKET_FD")) {
        if (*env_fd) socket_fd_env = std::atoi(env_fd);
    }
    if (const char* env_sock = std::getenv("OMNIDESK_WORKER_SOCKET")) {
        socket_path = env_sock;
    }
    if (socket_fd_env < 0 && socket_path.rfind("fd://", 0) == 0) {
        socket_fd_env = std::atoi(socket_path.c_str() + 5);
    }

    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--duplex") {
            duplex_mode = true;
        } else if (i + 1 < argc) {
            if (a == "--plugin-id") plugin_id = argv[++i];
            else if (a == "--so") so_path = argv[++i];
            else if (a == "--plugin-dir") plugin_dir = argv[++i];
            else if (a == "--data-dir") data_dir = argv[++i];
            else if (a == "--config-dir") config_dir = argv[++i];
            else if (a == "--socket") socket_path = argv[++i];
        }
    }

    if (plugin_id.empty() || so_path.empty()) {
        std::cerr << "Usage: omnidesk-plugin-worker --plugin-id <id> --so <path> "
                     "[--plugin-dir <dir>] [--data-dir <dir>] [--config-dir <dir>] [--socket <path>]\n";
        return 2;
    }

    // Acquire dedicated NDJSON transport before loading any third-party .so code.
    int rpc_read_fd = -1;
    int rpc_write_fd = -1;
    bool shared_io = false;
    if (socket_fd_env >= 0) {
        int flags = ::fcntl(socket_fd_env, F_GETFD);
        if (flags < 0) {
            std::cerr << "[worker:" << plugin_id
                      << "] invalid OMNIDESK_WORKER_SOCKET_FD=" << socket_fd_env << "\n";
            return 2;
        }
        ::fcntl(socket_fd_env, F_SETFD, flags | FD_CLOEXEC);
        rpc_read_fd = socket_fd_env;
        rpc_write_fd = socket_fd_env;
        shared_io = true;
    } else if (!socket_path.empty()) {
        std::string sock_err;
        int sfd = connectUnixSocket(socket_path, sock_err);
        if (sfd < 0) {
            std::cerr << "[worker:" << plugin_id << "] " << sock_err << "\n";
            return 2;
        }
        rpc_read_fd = sfd;
        rpc_write_fd = sfd;
        shared_io = true;
    } else {
        rpc_read_fd = ::dup(STDIN_FILENO);
        rpc_write_fd = ::dup(STDOUT_FILENO);
        if (rpc_read_fd < 0 || rpc_write_fd < 0) {
            std::cerr << "[worker:" << plugin_id << "] failed duplicating stdio descriptors\n";
            return 2;
        }
        ::fcntl(rpc_read_fd, F_SETFD, FD_CLOEXEC);
        ::fcntl(rpc_write_fd, F_SETFD, FD_CLOEXEC);
        // Redirect stdout so plugin printf/cout writes land in stderr_ring instead of corrupting NDJSON.
        ::dup2(STDERR_FILENO, STDOUT_FILENO);
    }
    ::dup2(STDERR_FILENO, STDOUT_FILENO);
    int null_input = ::open("/dev/null", O_RDONLY);
    if (null_input >= 0) {
        ::dup2(null_input, STDIN_FILENO);
        if (null_input != STDIN_FILENO) ::close(null_input);
    }

    NdjsonChannel channel(rpc_read_fd, rpc_write_fd, shared_io);

    void* dl_handle = ::dlopen(so_path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!dl_handle) {
        const char* dlerr = ::dlerror();
        std::cerr << "[worker:" << plugin_id << "] dlopen failed: "
                  << (dlerr ? dlerr : "unknown") << "\n";
        return 3;
    }

    auto abi_fn = reinterpret_cast<OmniDeskAbiInfoFn>(::dlsym(dl_handle, "omnidesk_plugin_abi_info"));
    auto create_fn =
        reinterpret_cast<OmniDeskPluginCreateFn>(::dlsym(dl_handle, "omnidesk_plugin_create"));
    auto destroy_fn =
        reinterpret_cast<OmniDeskPluginDestroyFn>(::dlsym(dl_handle, "omnidesk_plugin_destroy"));

    if (!abi_fn || !create_fn || !destroy_fn) {
        std::cerr << "[worker:" << plugin_id << "] plugin missing mandatory C ABI symbols\n";
        ::dlclose(dl_handle);
        return 3;
    }

    const OmniDeskPluginInfo* info = abi_fn();
    constexpr size_t kLegacyBaseInfoSize = offsetof(OmniDeskPluginInfo, sdk_abi_signature);
    if (!info || info->abi_version != OMNIDESK_PLUGIN_ABI_VERSION ||
        info->struct_size < kLegacyBaseInfoSize || !info->id || plugin_id != info->id) {
        std::cerr << "[worker:" << plugin_id << "] plugin base ABI handshake failed\n";
        ::dlclose(dl_handle);
        return 3;
    }
    if (info->struct_size < sizeof(OmniDeskPluginInfo) || !info->sdk_abi_signature ||
        !*info->sdk_abi_signature ||
        std::string(info->sdk_abi_signature) != omnidesk::sdkAbiSignature()) {
        std::cerr << "[worker:" << plugin_id
                  << "] SDK ABI signature mismatch (rebuild plugin binary against SDK "
                  << omnidesk::sdkVersion() << ")\n";
        ::dlclose(dl_handle);
        return 4;
    }

    omnidesk::IPlugin* instance = nullptr;
    try {
        instance = static_cast<omnidesk::IPlugin*>(create_fn());
    } catch (const std::exception& e) {
        std::cerr << "[worker:" << plugin_id << "] create_fn threw exception: " << e.what() << "\n";
        ::dlclose(dl_handle);
        return 3;
    } catch (...) {
        std::cerr << "[worker:" << plugin_id << "] create_fn threw unknown exception\n";
        ::dlclose(dl_handle);
        return 3;
    }
    if (!instance) {
        std::cerr << "[worker:" << plugin_id << "] omnidesk_plugin_create() returned nullptr\n";
        ::dlclose(dl_handle);
        return 3;
    }

    if (plugin_dir.empty()) {
        plugin_dir = fs::path(so_path).parent_path().parent_path().string();
    }
    if (data_dir.empty()) {
        data_dir = (fs::temp_directory_path() / ("omnidesk-worker-data-" + plugin_id)).string();
    }
    if (config_dir.empty()) {
        const char* env_cfg = std::getenv("OMNIDESK_CONFIG_HOME");
        if (env_cfg && *env_cfg) {
            config_dir = env_cfg;
        } else {
            const char* home = std::getenv("HOME");
            config_dir = (fs::path(home ? home : ".") / ".config" / "omnidesk").string();
        }
    }
    std::error_code ec;
    fs::create_directories(data_dir, ec);

    omnidesk::EntityBus bus;
    omnidesk::CapabilityRegistry caps;
    omnidesk::DomainRegistry domains;
    omnidesk::ConfigStore config(config_dir);
    omnidesk::SecretVault secrets(config_dir);
    omnidesk::JobCenter jobs(&bus, 2);
    omnidesk::WorkerSupervisor sub_workers;

    if (duplex_mode) {
        std::atomic<bool> stop_requested{false};
        std::atomic<bool> disconnected{false};
        std::mutex stop_mutex;
        std::condition_variable stop_cv;
        auto triggerStop = [&]() {
            {
                std::lock_guard lk(stop_mutex);
                stop_requested.store(true, std::memory_order_release);
            }
            stop_cv.notify_all();
        };

        std::mutex onload_mutex;
        std::condition_variable onload_cv;
        bool onload_done = false;
        bool onload_ok = false;
        std::string onload_error;

        std::atomic<uint64_t> next_req_seq{1};
        std::mutex outbound_mutex;
        std::unordered_map<std::string, std::shared_ptr<PendingHostCall>> pending_outbound;

        std::mutex inbound_mutex;
        std::unordered_map<std::string, std::shared_ptr<omnidesk::CancelToken>> active_inbound;

        auto callHost = [&](const std::string& op,
                            nlohmann::json body,
                            const omnidesk::StreamCallback& on_chunk,
                            const std::shared_ptr<omnidesk::CancelToken>& cancel,
                            bool stream_mode,
                            int64_t timeout_ms) -> omnidesk::CapabilityResult {
            auto emitDoneIfNeeded = [&](const omnidesk::CapabilityResult& res, bool saw_done) {
                if (!stream_mode || !on_chunk || saw_done) return;
                omnidesk::StreamChunk terminal;
                terminal.done = true;
                terminal.data = res.data;
                terminal.event = res.cancelled ? "cancelled" : (res.ok ? "done" : "error");
                try { on_chunk(terminal); } catch (...) {}
            };
            if (cancel && cancel->isCancelled()) {
                omnidesk::CapabilityResult r{false, nlohmann::json::object(), "cancelled", "", true};
                emitDoneIfNeeded(r, false);
                return r;
            }
            if (disconnected.load(std::memory_order_acquire) ||
                stop_requested.load(std::memory_order_acquire) ||
                terminationRequested()) {
                omnidesk::CapabilityResult r{
                    false, nlohmann::json::object(), "worker host bridge disconnected", "", false};
                emitDoneIfNeeded(r, false);
                return r;
            }
            const std::string req_id = "w-" + std::to_string(next_req_seq.fetch_add(1));
            auto pending = std::make_shared<PendingHostCall>();
            pending->on_chunk = on_chunk;
            bool early_disc = false;
            {
                std::lock_guard lk(outbound_mutex);
                if (disconnected.load(std::memory_order_acquire) ||
                    stop_requested.load(std::memory_order_acquire) ||
                    terminationRequested()) {
                    early_disc = true;
                } else {
                    pending_outbound[req_id] = pending;
                }
            }
            if (early_disc) {
                omnidesk::CapabilityResult r{
                    false, nlohmann::json::object(), "worker host bridge disconnected", "", false};
                emitDoneIfNeeded(r, false);
                return r;
            }

            nlohmann::json wire = body.is_object() ? std::move(body) : nlohmann::json::object();
            wire["kind"] = "req";
            wire["id"] = req_id;
            wire["op"] = op;
            if (stream_mode) wire["stream"] = true;

            if (!channel.sendJson(wire)) {
                std::lock_guard lk(outbound_mutex);
                pending_outbound.erase(req_id);
                omnidesk::CapabilityResult r{
                    false, nlohmann::json::object(), "failed sending request to host", "", false};
                emitDoneIfNeeded(r, false);
                return r;
            }

            const int64_t deadline = steadyNowMs() + std::max<int64_t>(100, timeout_ms);
            bool sent_cancel = false;
            int64_t cancel_deadline_ms = 0;
            bool wait_done = false;
            bool wait_failed = false;
            bool wait_saw_done = false;
            std::string wait_err;
            omnidesk::CapabilityResult wait_res;
            {
                std::unique_lock<std::mutex> plk(pending->mutex);
                while (!pending->done && !pending->failed) {
                    if (disconnected.load(std::memory_order_acquire) ||
                        stop_requested.load(std::memory_order_acquire) ||
                        terminationRequested()) {
                        pending->failed = true;
                        pending->error = "host connection closed";
                        break;
                    }
                    if (cancel && cancel->isCancelled()) {
                        if (!sent_cancel) {
                            sent_cancel = true;
                            cancel_deadline_ms = std::min(deadline, steadyNowMs() + 500);
                            plk.unlock();
                            channel.sendJson({{"kind", "cancel"}, {"id", req_id}});
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
                wait_saw_done = pending->saw_done_chunk;
                wait_err = pending->error;
                wait_res = pending->result;
            }

            {
                std::lock_guard lk(outbound_mutex);
                pending_outbound.erase(req_id);
            }

            if (wait_done) {
                emitDoneIfNeeded(wait_res, wait_saw_done);
                return wait_res;
            }
            if (cancel && cancel->isCancelled()) {
                omnidesk::CapabilityResult r{false, nlohmann::json::object(), "cancelled", "", true};
                emitDoneIfNeeded(r, wait_saw_done);
                return r;
            }
            if (wait_failed) {
                omnidesk::CapabilityResult r{
                    false, nlohmann::json::object(),
                    wait_err.empty() ? "host call failed" : wait_err, "", false};
                emitDoneIfNeeded(r, wait_saw_done);
                return r;
            }
            omnidesk::CapabilityResult r{false, nlohmann::json::object(), "host request timed out", "", false};
            emitDoneIfNeeded(r, wait_saw_done);
            return r;
        };

        auto cap_bridge = std::make_shared<omnidesk::CapabilityRegistry::RemoteBridge>();
        static thread_local bool suppress_remote_cap_list = false;
        cap_bridge->has = [&](const std::string& cap_id) -> bool {
            auto res = callHost("host.cap.has", {{"capability_id", cap_id}}, nullptr, nullptr, false, 5000);
            return res.ok && res.data.value("has", false);
        };
        cap_bridge->providers_for = [&](const std::string& cap_id) {
            std::vector<omnidesk::CapabilityDescriptor> out;
            auto res = callHost("host.cap.providers", {{"capability_id", cap_id}}, nullptr, nullptr, false, 5000);
            if (res.ok && res.data.contains("providers") && res.data["providers"].is_array()) {
                for (const auto& item : res.data["providers"]) {
                    omnidesk::CapabilityDescriptor d;
                    d.capability_id = item.value("capability_id", "");
                    d.plugin_id = item.value("plugin_id", "");
                    d.priority = item.value("priority", 100);
                    if (item.contains("metadata") && item["metadata"].is_object()) {
                        d.metadata = item["metadata"];
                    }
                    if (!d.capability_id.empty() && !d.plugin_id.empty()) {
                        out.push_back(std::move(d));
                    }
                }
            }
            return out;
        };
        cap_bridge->list_all = [&]() {
            std::vector<omnidesk::CapabilityDescriptor> out;
            if (suppress_remote_cap_list) {
                return out;
            }
            auto res = callHost("host.cap.list", nlohmann::json::object(), nullptr, nullptr, false, 5000);
            if (res.ok && res.data.contains("capabilities") && res.data["capabilities"].is_array()) {
                for (const auto& item : res.data["capabilities"]) {
                    omnidesk::CapabilityDescriptor d;
                    d.capability_id = item.value("capability_id", "");
                    d.plugin_id = item.value("plugin_id", "");
                    d.priority = item.value("priority", 100);
                    if (item.contains("metadata") && item["metadata"].is_object()) {
                        d.metadata = item["metadata"];
                    }
                    if (!d.capability_id.empty() && !d.plugin_id.empty()) {
                        out.push_back(std::move(d));
                    }
                }
            }
            return out;
        };
        cap_bridge->invoke = [&](const std::string& plugin_id_filter,
                                 const std::string& capability_id,
                                 const nlohmann::json& request,
                                 const omnidesk::StreamCallback& on_chunk,
                                 const std::shared_ptr<omnidesk::CancelToken>& cancel,
                                 bool stream_mode) -> omnidesk::CapabilityResult {
            if (!plugin_id_filter.empty() && plugin_id_filter == plugin_id) {
                return {false,
                        nlohmann::json::object(),
                        "provider '" + plugin_id + "' missing capability '" + capability_id + "'",
                        "",
                        false};
            }
            nlohmann::json body = {
                {"capability_id", capability_id},
                {"plugin_id", plugin_id_filter},
                {"stream", stream_mode},
                {"request", request.is_null() ? nlohmann::json::object() : request},
            };
            return callHost("host.cap.invoke", std::move(body), on_chunk, cancel, stream_mode, 300000);
        };
        caps.setRemoteBridge(cap_bridge);

        auto bus_bridge = std::make_shared<omnidesk::EntityBus::RemoteBridge>();
        bus_bridge->on_subscribe = [&](const std::string& topic) {
            callHost("host.bus.subscribe", {{"topic", topic}}, nullptr, nullptr, false, 5000);
        };
        bus_bridge->on_unsubscribe = [&](const std::string& topic) {
            callHost("host.bus.unsubscribe", {{"topic", topic}}, nullptr, nullptr, false, 2000);
        };
        bus_bridge->on_publish = [&](const std::string& topic, const nlohmann::json& payload) -> size_t {
            auto res = callHost("host.bus.publish",
                                {{"topic", topic},
                                 {"payload", payload.is_null() ? nlohmann::json::object() : payload}},
                                nullptr, nullptr, false, 5000);
            if (res.ok && res.data.is_object()) {
                return res.data.value("delivered", size_t{0});
            }
            return 0;
        };
        bus.setRemoteBridge(bus_bridge);

        caps.setFaultHandler([&](const std::string& pid, const std::string& comp, const std::string& err) {
            std::cerr << "[worker:" << pid << "][" << comp << "] capability fault: " << err << "\n";
            callHost("host.bus.publish",
                     {{"topic", "plugin.fault"},
                      {"payload", {{"plugin_id", pid}, {"component", "capability:" + comp}, {"error", err}}}},
                     nullptr, nullptr, false, 1000);
        });
        domains.setFaultHandler([&](const std::string& pid, const std::string& comp, const std::string& err) {
            std::cerr << "[worker:" << pid << "][" << comp << "] domain fault: " << err << "\n";
            callHost("host.bus.publish",
                     {{"topic", "plugin.fault"},
                      {"payload", {{"plugin_id", pid}, {"component", "domain:" + comp}, {"error", err}}}},
                     nullptr, nullptr, false, 1000);
        });
        bus.setFaultHandler([&](const std::string& owner, const std::string& topic, const std::string& err) {
            std::cerr << "[worker:" << owner << "][" << topic << "] bus fault: " << err << "\n";
            thread_local bool in_bus_fault = false;
            if (in_bus_fault) return;
            in_bus_fault = true;
            callHost("host.bus.publish",
                     {{"topic", "plugin.fault"},
                      {"payload", {{"plugin_id", owner}, {"component", "event_bus:" + topic}, {"error", err}}}},
                     nullptr, nullptr, false, 1000);
            in_bus_fault = false;
        });

        WorkerPool request_pool(4);
        WorkerPool event_pool(1);

        auto handleInboundRequest = [&](const std::string& req_id,
                                        const nlohmann::json& req,
                                        const std::shared_ptr<omnidesk::CancelToken>& cancel) {
            auto sendResp = [&](bool ok, nlohmann::json data, std::string error, bool cancelled = false) {
                nlohmann::json rwire = {
                    {"kind", "resp"},
                    {"id", req_id},
                    {"done", true},
                    {"ok", ok},
                    {"data", data.is_null() ? nlohmann::json::object() : std::move(data)},
                    {"error", std::move(error)},
                    {"cancelled", cancelled},
                    {"provider_plugin_id", plugin_id},
                };
                channel.sendJson(rwire);
            };

            if (req.contains("op") && !req["op"].is_string()) {
                sendResp(false, nlohmann::json::object(), "invalid operation type");
                return;
            }
            const std::string op = req.value("op", "");
            try {
                {
                    std::unique_lock lk(onload_mutex);
                    onload_cv.wait(lk, [&]() {
                        return onload_done || stop_requested.load(std::memory_order_acquire) ||
                               terminationRequested();
                    });
                }
                if (!onload_ok) {
                    sendResp(false, nlohmann::json::object(),
                             onload_error.empty() ? "plugin initialization failed" : onload_error);
                    return;
                }

                if (op == "describe") {
                    struct ScopedLocalDescribe {
                        bool prev;
                        ScopedLocalDescribe() : prev(suppress_remote_cap_list) {
                            suppress_remote_cap_list = true;
                        }
                        ~ScopedLocalDescribe() {
                            suppress_remote_cap_list = prev;
                        }
                    } local_only_guard;

                    nlohmann::json caps_arr = nlohmann::json::array();
                    for (const auto& d : caps.listAll()) {
                        if (d.plugin_id != plugin_id) continue;
                        caps_arr.push_back({
                            {"capability_id", d.capability_id},
                            {"plugin_id", d.plugin_id},
                            {"priority", d.priority},
                            {"metadata", d.metadata},
                        });
                    }
                    nlohmann::json media_arr = nlohmann::json::array();
                    for (const auto& s : domains.mediaSources()) {
                        if (!s) continue;
                        media_arr.push_back({
                            {"source_id", s->sourceId()},
                            {"display_name", s->displayName()},
                            {"categories", s->categories()},
                        });
                    }
                    nlohmann::json text_arr = nlohmann::json::array();
                    for (const auto& s : domains.textSources()) {
                        if (!s) continue;
                        text_arr.push_back({
                            {"source_id", s->sourceId()},
                            {"display_name", s->displayName()},
                            {"category", s->category()},
                            {"channels", omnidesk::toJsonArray(s->channels())},
                        });
                    }
                    nlohmann::json book_arr = nlohmann::json::array();
                    for (const auto& s : domains.bookSources()) {
                        if (!s) continue;
                        book_arr.push_back({
                            {"source_id", s->sourceId()},
                            {"display_name", s->displayName()},
                        });
                    }
                    sendResp(true,
                             {
                                 {"plugin_id", plugin_id},
                                 {"pid", static_cast<int>(::getpid())},
                                 {"sdk_version", omnidesk::sdkVersion()},
                                 {"sdk_abi_signature", omnidesk::sdkAbiSignature()},
                                 {"capabilities", std::move(caps_arr)},
                                 {"media_sources", std::move(media_arr)},
                                 {"text_sources", std::move(text_arr)},
                                 {"book_sources", std::move(book_arr)},
                             },
                             "");
                    return;
                }

                if (op == "health") {
                    nlohmann::json hc = instance->onHealthCheck();
                    sendResp(true, std::move(hc), "");
                    return;
                }

                if (op == "cap.invoke") {
                    const std::string cap_id = req.value("capability_id", "");
                    const bool stream = req.value("stream", false);
                    const nlohmann::json inner =
                        req.contains("request") ? req["request"] : nlohmann::json::object();

                    if (stream) {
                        omnidesk::CapabilityResult res = caps.callProviderStream(
                            plugin_id, cap_id, inner,
                            [&](const omnidesk::StreamChunk& chunk) {
                                if (chunk.done) return;
                                channel.sendJson({
                                    {"kind", "chunk"},
                                    {"id", req_id},
                                    {"type", "delta"},
                                    {"done", false},
                                    {"event", chunk.event.empty() ? "delta" : chunk.event},
                                    {"delta", chunk.delta},
                                    {"data", chunk.data},
                                });
                            },
                            cancel);
                        sendResp(res.ok, res.data, res.error, res.cancelled);
                    } else {
                        omnidesk::CapabilityResult res =
                            caps.callProviderCancellable(plugin_id, cap_id, inner, cancel);
                        sendResp(res.ok, res.data, res.error, res.cancelled);
                    }
                    return;
                }

                if (op == "media.categories") {
                    auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "media source not found");
                    else sendResp(true, {{"categories", src->categories()}}, "");
                    return;
                }
                if (op == "media.fetch_items") {
                    auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "media source not found");
                    else {
                        const std::string category = req.value("category", "all");
                        const std::string query = req.value("query", "");
                        const int page = req.value("page", 1);
                        const bool force_refresh = req.value("force_refresh", false);
                        auto items = (page > 1 || force_refresh)
                                         ? src->fetchItemsPage(category, query, page, force_refresh)
                                         : src->fetchItems(category, query);
                        sendResp(true, {{"items", omnidesk::toJsonArray(items)}}, "");
                    }
                    return;
                }
                if (op == "media.fetch_lyrics") {
                    auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "media source not found");
                    else {
                        auto lyrics = src->fetchLyrics(req.value("item_id", ""));
                        sendResp(true, {{"lyrics", omnidesk::toJsonArray(lyrics)}}, "");
                    }
                    return;
                }
                if (op == "media.fetch_chapters") {
                    auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "media source not found");
                    else {
                        auto chapters = src->fetchChapters(req.value("item_id", ""));
                        sendResp(true, {{"chapters", omnidesk::toJsonArray(chapters)}}, "");
                    }
                    return;
                }
                if (op == "text.channels") {
                    auto src = findSourceById(domains.textSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "text source not found");
                    else {
                        auto chans = src->channels();
                        sendResp(true, {{"channels", omnidesk::toJsonArray(chans)}}, "");
                    }
                    return;
                }
                if (op == "text.fetch_feed") {
                    auto src = findSourceById(domains.textSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "text source not found");
                    else {
                        const std::string channel_id = req.value("channel_id", "");
                        const std::string query = req.value("query", "");
                        const int page = req.value("page", 1);
                        const bool force_refresh = req.value("force_refresh", false);
                        auto items = (page > 1 || force_refresh)
                                         ? src->fetchFeedPage(channel_id, query, page, force_refresh)
                                         : (channel_id.empty()
                                                ? src->fetchFeed(query)
                                                : src->fetchFeedInChannel(channel_id, query));
                        sendResp(true, {{"items", omnidesk::toJsonArray(items)}}, "");
                    }
                    return;
                }
                if (op == "text.fetch_article") {
                    auto src = findSourceById(domains.textSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "text source not found");
                    else {
                        auto article = src->fetchArticle(req.value("article_id", ""));
                        sendResp(true, {{"article", omnidesk::toJson(article)}}, "");
                    }
                    return;
                }
                if (op == "book.list_books") {
                    auto src = findSourceById(domains.bookSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "book source not found");
                    else {
                        auto books = src->listBooks(req.value("format_filter", ""), req.value("query", ""));
                        sendResp(true, {{"items", omnidesk::toJsonArray(books)}}, "");
                    }
                    return;
                }
                if (op == "book.fetch_spread") {
                    auto src = findSourceById(domains.bookSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "book source not found");
                    else {
                        auto spread = src->fetchSpread(req.value("book_id", ""), req.value("page_number", 0));
                        sendResp(true, {{"spread", omnidesk::toJson(spread)}}, "");
                    }
                    return;
                }
                if (op == "book.list_vocab") {
                    auto src = findSourceById(domains.bookSources(), req.value("source_id", ""));
                    if (!src) sendResp(false, nlohmann::json::object(), "book source not found");
                    else {
                        auto vocab = src->listVocab();
                        sendResp(true, {{"vocab", omnidesk::toJsonArray(vocab)}}, "");
                    }
                    return;
                }
                sendResp(false, nlohmann::json::object(), "unsupported worker operation: " + op);
            } catch (const std::exception& e) {
                sendResp(false, nlohmann::json::object(), std::string("worker exception: ") + e.what());
            } catch (...) {
                sendResp(false, nlohmann::json::object(), "worker unknown C++ exception");
            }
        };

        std::thread reader_thread([&]() {
            std::string rline;
            while (channel.readLineInterruptible(rline, stop_requested)) {
                if (rline.empty()) continue;
                nlohmann::json msg = nlohmann::json::parse(rline, nullptr, false);
                if (msg.is_discarded() || !msg.is_object()) continue;
                const bool has_kind = msg.contains("kind") && !msg["kind"].is_null();
                if (has_kind && !msg["kind"].is_string()) continue;
                const std::string kind = has_kind ? msg["kind"].get<std::string>() : std::string{};
                if (msg.contains("id") && !msg["id"].is_null() && !msg["id"].is_string()) continue;
                const std::string id =
                    (msg.contains("id") && msg["id"].is_string()) ? msg["id"].get<std::string>() : std::string{};

                if (kind == "chunk" && !id.empty()) {
                    std::shared_ptr<PendingHostCall> pending;
                    {
                        std::lock_guard lk(outbound_mutex);
                        auto it = pending_outbound.find(id);
                        if (it != pending_outbound.end()) pending = it->second;
                    }
                    if (!pending) continue;
                    std::string delta = msg.value("delta", "");
                    omnidesk::StreamCallback cb;
                    {
                        std::lock_guard plk(pending->mutex);
                        if (!delta.empty()) {
                            pending->accumulated += delta;
                            if (pending->accumulated.size() > 8 * 1024 * 1024) {
                                pending->failed = true;
                                pending->error = "host stream exceeds 8 MiB limit";
                                pending->cv.notify_all();
                                continue;
                            }
                        }
                        cb = pending->on_chunk;
                    }
                    if (cb) {
                        omnidesk::StreamChunk chunk;
                        chunk.delta = std::move(delta);
                        chunk.data = msg.contains("data") ? msg["data"] : msg;
                        chunk.event = msg.value("event", msg.value("type", "delta"));
                        chunk.done = false;
                        try { cb(chunk); } catch (...) {}
                    }
                    continue;
                }

                if (kind == "resp" && !id.empty()) {
                    std::shared_ptr<PendingHostCall> pending;
                    {
                        std::lock_guard lk(outbound_mutex);
                        auto it = pending_outbound.find(id);
                        if (it != pending_outbound.end()) {
                            pending = std::move(it->second);
                            pending_outbound.erase(it);
                        }
                    }
                    if (!pending) continue;
                    const bool ok = msg.value("ok", false);
                    const bool was_cancelled = msg.value("cancelled", false);
                    nlohmann::json final_data = msg.contains("data") ? msg["data"] : nlohmann::json::object();
                    omnidesk::StreamCallback cb;
                    omnidesk::CapabilityResult res;
                    {
                        std::lock_guard plk(pending->mutex);
                        if (final_data.is_object() && !pending->accumulated.empty() &&
                            !final_data.contains("text") && !final_data.contains("answer")) {
                            final_data["text"] = pending->accumulated;
                        }
                        res.ok = ok;
                        res.data = std::move(final_data);
                        res.error = msg.value("error", "");
                        res.cancelled = was_cancelled;
                        res.provider_plugin_id = msg.value("provider_plugin_id", "");
                        cb = pending->on_chunk;
                        pending->result = res;
                        pending->done = true;
                        if (cb) pending->saw_done_chunk = true;
                    }
                    if (cb) {
                        omnidesk::StreamChunk end_chunk;
                        end_chunk.data = res.data;
                        end_chunk.event = res.cancelled ? "cancelled" : (res.ok ? "done" : "error");
                        end_chunk.done = true;
                        try { cb(end_chunk); } catch (...) {}
                    }
                    pending->cv.notify_all();
                    continue;
                }

                if (kind == "cancel" && !id.empty()) {
                    std::shared_ptr<omnidesk::CancelToken> tok;
                    {
                        std::lock_guard ilk(inbound_mutex);
                        auto it = active_inbound.find(id);
                        if (it != active_inbound.end()) tok = it->second;
                    }
                    if (tok) tok->cancel();
                    continue;
                }

                if (kind == "event") {
                    const std::string op = msg.value("op", "");
                    if (op == "host.bus.event") {
                        const std::string topic = msg.value("topic", "");
                        const nlohmann::json payload =
                            msg.contains("payload") ? msg["payload"] : nlohmann::json::object();
                        if (!topic.empty()) {
                            event_pool.submit([&bus, topic, payload]() {
                                bus.publishLocalOnly(topic, payload);
                            });
                        }
                    } else if (op == "host.cap.changed") {
                        event_pool.submit([&caps]() {
                            caps.notifyRemoteChanged();
                        });
                    }
                    continue;
                }

                if (kind == "req" || (kind.empty() && msg.contains("op"))) {
                    const std::string op = msg.value("op", "");
                    if (op == "shutdown") {
                        channel.sendJson({
                            {"kind", "resp"},
                            {"id", id},
                            {"done", true},
                            {"ok", true},
                            {"data", {{"shutdown", true}}},
                            {"error", ""},
                        });
                        disconnected.store(true, std::memory_order_release);
                        triggerStop();
                        break;
                    }
                    auto cancel_tok = omnidesk::CancelToken::create();
                    if (!id.empty()) {
                        std::lock_guard ilk(inbound_mutex);
                        active_inbound[id] = cancel_tok;
                    }
                    request_pool.submit([&, id, msg = std::move(msg), cancel_tok]() {
                        handleInboundRequest(id, msg, cancel_tok);
                        if (!id.empty()) {
                            std::lock_guard ilk(inbound_mutex);
                            active_inbound.erase(id);
                        }
                    });
                }
            }

            disconnected.store(true, std::memory_order_release);
            {
                std::lock_guard lk(outbound_mutex);
                for (auto& [_, p] : pending_outbound) {
                    std::lock_guard plk(p->mutex);
                    if (!p->done && !p->failed) {
                        p->failed = true;
                        p->error = "host connection closed";
                    }
                    p->cv.notify_all();
                }
                pending_outbound.clear();
            }
            {
                std::lock_guard ilk(inbound_mutex);
                for (auto& [_, tok] : active_inbound) {
                    if (tok) tok->cancel();
                }
            }
            triggerStop();
            {
                std::lock_guard lk(onload_mutex);
            }
            onload_cv.notify_all();
        });

        omnidesk::PluginContext ctx(plugin_id, plugin_dir, data_dir, caps, bus, domains, config,
                                    secrets, &jobs, &sub_workers);

        bool loaded = false;
        std::string load_err;
        try {
            loaded = instance->onLoad(ctx);
            if (!loaded) load_err = "IPlugin::onLoad() returned false";
        } catch (const std::exception& e) {
            loaded = false;
            load_err = std::string("onLoad threw exception: ") + e.what();
            std::cerr << "[worker:" << plugin_id << "] " << load_err << "\n";
        } catch (...) {
            loaded = false;
            load_err = "onLoad threw unknown exception";
            std::cerr << "[worker:" << plugin_id << "] " << load_err << "\n";
        }

        bool has_ui = false;
        if (loaded) {
            has_ui = !domains.workspaceTabs().empty();
            for (auto domain : {omnidesk::ContentDomain::Core, omnidesk::ContentDomain::Media,
                                omnidesk::ContentDomain::Text, omnidesk::ContentDomain::Book}) {
                has_ui = has_ui || !domains.dockWidgets(domain).empty();
            }
            if (has_ui) {
                load_err = "UI registrations require an in_process shell with separate workers";
                std::cerr << "[worker:" << plugin_id << "] " << load_err << "\n";
            }
        }

        {
            std::lock_guard lk(onload_mutex);
            onload_done = true;
            onload_ok = (loaded && !has_ui);
            onload_error = load_err;
        }
        onload_cv.notify_all();

        if (!loaded || has_ui) {
            if (!terminationRequested()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            triggerStop();
            if (reader_thread.joinable()) reader_thread.join();
            request_pool.shutdown();
            event_pool.shutdown();
            caps.setRemoteBridge(nullptr);
            bus.setRemoteBridge(nullptr);
            try {
                if (loaded) instance->onUnload();
            } catch (...) {
            }
            caps.unregisterPlugin(plugin_id);
            jobs.shutdown();
            sub_workers.stopAll();
            bus.unsubscribeOwner(plugin_id);
            domains.unregisterPlugin(plugin_id);
            caps.shutdown();
            channel.close();
            try {
                destroy_fn(instance);
            } catch (...) {
            }
            ::dlclose(dl_handle);
            return 3;
        }

        {
            std::unique_lock lk(stop_mutex);
            while (!stop_requested.load(std::memory_order_acquire) && !terminationRequested()) {
                stop_cv.wait_for(lk, std::chrono::milliseconds(50));
            }
            if (terminationRequested()) {
                disconnected.store(true, std::memory_order_release);
                stop_requested.store(true, std::memory_order_release);
            }
        }

        {
            std::lock_guard ilk(inbound_mutex);
            for (auto& [_, tok] : active_inbound) {
                if (tok) tok->cancel();
            }
        }
        request_pool.shutdown();
        event_pool.shutdown();
        try {
            instance->onUnload();
        } catch (...) {
        }
        caps.unregisterPlugin(plugin_id);
        jobs.shutdown();
        sub_workers.stopAll();
        bus.unsubscribeOwner(plugin_id);
        domains.unregisterPlugin(plugin_id);
        caps.setRemoteBridge(nullptr);
        bus.setRemoteBridge(nullptr);
        if (reader_thread.joinable()) reader_thread.join();
        caps.shutdown();
        channel.close();
        try {
            destroy_fn(instance);
        } catch (...) {
        }
        ::dlclose(dl_handle);
        return 0;
    }

    caps.setFaultHandler([&](const std::string& pid, const std::string& comp, const std::string& err) {
        std::cerr << "[worker:" << pid << "][" << comp << "] capability fault: " << err << "\n";
    });
    domains.setFaultHandler([&](const std::string& pid, const std::string& comp, const std::string& err) {
        std::cerr << "[worker:" << pid << "][" << comp << "] domain fault: " << err << "\n";
    });
    bus.setFaultHandler([&](const std::string& owner, const std::string& topic, const std::string& err) {
        std::cerr << "[worker:" << owner << "][" << topic << "] bus fault: " << err << "\n";
    });

    omnidesk::PluginContext ctx(plugin_id, plugin_dir, data_dir, caps, bus, domains, config,
                                secrets, &jobs, &sub_workers);

    bool loaded = false;
    try {
        loaded = instance->onLoad(ctx);
    } catch (const std::exception& e) {
        std::cerr << "[worker:" << plugin_id << "] onLoad threw exception: " << e.what() << "\n";
        loaded = false;
    } catch (...) {
        std::cerr << "[worker:" << plugin_id << "] onLoad threw unknown exception\n";
        loaded = false;
    }

    auto shutdownPlugin = [&]() {
        try {
            if (loaded) instance->onUnload();
        } catch (...) {
        }
        caps.unregisterPlugin(plugin_id);
        jobs.shutdown();
        sub_workers.stopAll();
        bus.unsubscribeOwner(plugin_id);
        domains.unregisterPlugin(plugin_id);
        try {
            destroy_fn(instance);
        } catch (...) {
        }
        ::dlclose(dl_handle);
    };

    if (!loaded) {
        std::cerr << "[worker:" << plugin_id << "] IPlugin::onLoad() returned false\n";
        shutdownPlugin();
        return 3;
    }

    bool has_ui = !domains.workspaceTabs().empty();
    for (auto domain : {omnidesk::ContentDomain::Core, omnidesk::ContentDomain::Media,
                        omnidesk::ContentDomain::Text, omnidesk::ContentDomain::Book}) {
        has_ui = has_ui || !domains.dockWidgets(domain).empty();
    }
    if (has_ui) {
        std::cerr << "[worker:" << plugin_id
                  << "] UI registrations require an in_process shell with separate workers\n";
        shutdownPlugin();
        return 3;
    }

    std::string line;
    while (channel.readLine(line)) {
        if (line.empty()) continue;

        nlohmann::json req = nlohmann::json::parse(line, nullptr, false);
        if (req.is_discarded() || !req.is_object()) {
            channel.sendJson(makeErr("invalid JSON request envelope"));
            continue;
        }

        if (req.contains("op") && !req["op"].is_string()) {
            channel.sendJson(makeErr("invalid operation type"));
            continue;
        }
        const std::string op = req.value("op", "");
        if (op == "shutdown") {
            channel.sendJson({{"ok", true}, {"data", {{"shutdown", true}}}});
            break;
        }

        try {
            if (op == "describe") {
                nlohmann::json caps_arr = nlohmann::json::array();
                for (const auto& d : caps.listAll()) {
                    caps_arr.push_back({
                        {"capability_id", d.capability_id},
                        {"plugin_id", d.plugin_id},
                        {"priority", d.priority},
                        {"metadata", d.metadata},
                    });
                }

                nlohmann::json media_arr = nlohmann::json::array();
                for (const auto& s : domains.mediaSources()) {
                    if (!s) continue;
                    media_arr.push_back({
                        {"source_id", s->sourceId()},
                        {"display_name", s->displayName()},
                        {"categories", s->categories()},
                    });
                }

                nlohmann::json text_arr = nlohmann::json::array();
                for (const auto& s : domains.textSources()) {
                    if (!s) continue;
                    text_arr.push_back({
                        {"source_id", s->sourceId()},
                        {"display_name", s->displayName()},
                        {"category", s->category()},
                        {"channels", omnidesk::toJsonArray(s->channels())},
                    });
                }

                nlohmann::json book_arr = nlohmann::json::array();
                for (const auto& s : domains.bookSources()) {
                    if (!s) continue;
                    book_arr.push_back({
                        {"source_id", s->sourceId()},
                        {"display_name", s->displayName()},
                    });
                }

                channel.sendJson({
                    {"ok", true},
                    {"data",
                     {
                         {"plugin_id", plugin_id},
                         {"pid", static_cast<int>(::getpid())},
                         {"sdk_version", omnidesk::sdkVersion()},
                         {"sdk_abi_signature", omnidesk::sdkAbiSignature()},
                         {"capabilities", std::move(caps_arr)},
                         {"media_sources", std::move(media_arr)},
                         {"text_sources", std::move(text_arr)},
                         {"book_sources", std::move(book_arr)},
                     }},
                });
                continue;
            }

            if (op == "health") {
                nlohmann::json hc = instance->onHealthCheck();
                channel.sendJson({{"ok", true}, {"data", hc}});
                continue;
            }

            if (op == "cap.invoke") {
                const std::string cap_id = req.value("capability_id", "");
                const bool stream = req.value("stream", false);
                const nlohmann::json inner =
                    req.contains("request") ? req["request"] : nlohmann::json::object();

                if (stream) {
                    omnidesk::CapabilityResult res = caps.callProviderStream(
                        plugin_id, cap_id, inner,
                        [&](const omnidesk::StreamChunk& chunk) {
                            if (chunk.done) return;
                            channel.sendJson({
                                {"type", "delta"},
                                {"done", false},
                                {"event", chunk.event.empty() ? "delta" : chunk.event},
                                {"delta", chunk.delta},
                                {"data", chunk.data},
                            });
                        },
                        nullptr);
                    channel.sendJson({
                        {"type", res.ok ? "done" : "error"},
                        {"done", true},
                        {"ok", res.ok},
                        {"data", res.data},
                        {"error", res.error},
                    });
                } else {
                    omnidesk::CapabilityResult res = caps.callProvider(plugin_id, cap_id, inner);
                    channel.sendJson({
                        {"ok", res.ok},
                        {"data", res.data},
                        {"error", res.error},
                    });
                }
                continue;
            }

            if (op == "media.categories") {
                auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("media source not found"));
                } else {
                    channel.sendJson({{"ok", true}, {"data", {{"categories", src->categories()}}}});
                }
                continue;
            }
            if (op == "media.fetch_items") {
                auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("media source not found"));
                } else {
                    auto items =
                        src->fetchItems(req.value("category", "all"), req.value("query", ""));
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"items", omnidesk::toJsonArray(items)}}}});
                }
                continue;
            }
            if (op == "media.fetch_lyrics") {
                auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("media source not found"));
                } else {
                    auto lyrics = src->fetchLyrics(req.value("item_id", ""));
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"lyrics", omnidesk::toJsonArray(lyrics)}}}});
                }
                continue;
            }
            if (op == "media.fetch_chapters") {
                auto src = findSourceById(domains.mediaSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("media source not found"));
                } else {
                    auto chapters = src->fetchChapters(req.value("item_id", ""));
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"chapters", omnidesk::toJsonArray(chapters)}}}});
                }
                continue;
            }

            if (op == "text.channels") {
                auto src = findSourceById(domains.textSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("text source not found"));
                } else {
                    auto chans = src->channels();
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"channels", omnidesk::toJsonArray(chans)}}}});
                }
                continue;
            }
            if (op == "text.fetch_feed") {
                auto src = findSourceById(domains.textSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("text source not found"));
                } else {
                    const std::string channel_id = req.value("channel_id", "");
                    const std::string query = req.value("query", "");
                    auto items = channel_id.empty()
                                     ? src->fetchFeed(query)
                                     : src->fetchFeedInChannel(channel_id, query);
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"items", omnidesk::toJsonArray(items)}}}});
                }
                continue;
            }
            if (op == "text.fetch_article") {
                auto src = findSourceById(domains.textSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("text source not found"));
                } else {
                    auto article = src->fetchArticle(req.value("article_id", ""));
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"article", omnidesk::toJson(article)}}}});
                }
                continue;
            }

            if (op == "book.list_books") {
                auto src = findSourceById(domains.bookSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("book source not found"));
                } else {
                    auto books =
                        src->listBooks(req.value("format_filter", ""), req.value("query", ""));
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"items", omnidesk::toJsonArray(books)}}}});
                }
                continue;
            }
            if (op == "book.fetch_spread") {
                auto src = findSourceById(domains.bookSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("book source not found"));
                } else {
                    auto spread = src->fetchSpread(req.value("book_id", ""), req.value("page_number", 0));
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"spread", omnidesk::toJson(spread)}}}});
                }
                continue;
            }
            if (op == "book.list_vocab") {
                auto src = findSourceById(domains.bookSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("book source not found"));
                } else {
                    auto vocab = src->listVocab();
                    channel.sendJson(
                        {{"ok", true}, {"data", {{"vocab", omnidesk::toJsonArray(vocab)}}}});
                }
                continue;
            }

            channel.sendJson(makeErr("unsupported worker operation: " + op));
        } catch (const std::exception& e) {
            channel.sendJson(makeErr(std::string("worker exception: ") + e.what()));
        } catch (...) {
            channel.sendJson(makeErr("worker unknown C++ exception"));
        }
    }

    shutdownPlugin();
    return 0;
}
