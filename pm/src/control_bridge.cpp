#include "omnidesk/pm/control_bridge.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <mutex>
#include <poll.h>
#include <set>
#include <stdexcept>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>

namespace fs = std::filesystem;

namespace omnidesk::pm {
namespace {

using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
constexpr int kIoTimeoutMs = 2000;

struct FileDescriptor {
    int value = -1;
    explicit FileDescriptor(int descriptor = -1) : value(descriptor) {}
    ~FileDescriptor() { if (value >= 0) ::close(value); }
    FileDescriptor(const FileDescriptor&) = delete;
    FileDescriptor& operator=(const FileDescriptor&) = delete;
};

int64_t nowEpochMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

sockaddr_un socketAddress(const std::string& path) {
    sockaddr_un address{};
    if (path.empty() || path.find('\0') != std::string::npos ||
        path.size() >= sizeof(address.sun_path)) {
        throw std::runtime_error("invalid control socket path");
    }
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
    return address;
}

bool sameUser(int descriptor) {
    ucred credentials{};
    socklen_t size = sizeof(credentials);
    return ::getsockopt(descriptor, SOL_SOCKET, SO_PEERCRED, &credentials, &size) == 0 &&
           size == sizeof(credentials) && credentials.uid == ::geteuid();
}

bool waitFor(int descriptor, short events, Clock::time_point deadline,
             const std::function<bool()>& interrupted, std::string& error) {
    while (true) {
        if (interrupted && interrupted()) {
            error = "control request interrupted";
            return false;
        }
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - Clock::now()).count();
        if (remaining <= 0) {
            error = "control socket deadline exceeded";
            return false;
        }
        pollfd descriptor_event{descriptor, events, 0};
        const int result = ::poll(&descriptor_event, 1,
                                  static_cast<int>(std::min<int64_t>(remaining, 50)));
        if (result < 0 && errno == EINTR) continue;
        if (result < 0) {
            error = std::strerror(errno);
            return false;
        }
        if (result > 0) {
            if (descriptor_event.revents & POLLNVAL) {
                error = "invalid control socket descriptor";
                return false;
            }
            return true;
        }
    }
}

bool writeFrame(int descriptor, const std::string& wire, Clock::time_point deadline,
                const std::function<bool()>& interrupted, std::string& error) {
    size_t offset = 0;
    while (offset < wire.size()) {
        if (!waitFor(descriptor, POLLOUT, deadline, interrupted, error)) return false;
        const ssize_t count = ::send(descriptor, wire.data() + offset, wire.size() - offset,
                                     MSG_NOSIGNAL | MSG_DONTWAIT);
        if (count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (count <= 0) {
            error = count == 0 ? "control socket closed while writing" : std::strerror(errno);
            return false;
        }
        offset += static_cast<size_t>(count);
    }
    return true;
}

bool readFrame(int descriptor, std::string& pending, std::string& line,
               Clock::time_point deadline, const std::function<bool()>& interrupted,
               std::string& error) {
    while (true) {
        const size_t newline = pending.find('\n');
        if (newline != std::string::npos) {
            if (newline + 1 > kMaxControlFrameBytes) {
                error = "frame exceeds 8 MiB limit";
                return false;
            }
            line = pending.substr(0, newline);
            pending.erase(0, newline + 1);
            return true;
        }
        if (pending.size() >= kMaxControlFrameBytes) {
            error = "frame exceeds 8 MiB limit";
            return false;
        }
        if (!waitFor(descriptor, POLLIN, deadline, interrupted, error)) return false;
        char buffer[8192];
        const ssize_t count = ::recv(descriptor, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (count <= 0) {
            error = count == 0 ? "control socket closed before newline" : std::strerror(errno);
            return false;
        }
        pending.append(buffer, static_cast<size_t>(count));
    }
}

bool sameInode(const struct stat& first, const struct stat& second) {
    return first.st_dev == second.st_dev && first.st_ino == second.st_ino &&
           first.st_uid == second.st_uid;
}

Json failure(const Json& id, const std::string& error) {
    return {{"id", id}, {"ok", false}, {"error", error}};
}

Json capabilitiesJson(const CapabilityRegistry& capabilities) {
    Json result = Json::array();
    for (const auto& descriptor : capabilities.listAll()) {
        result.push_back({{"capability_id", descriptor.capability_id},
                          {"plugin_id", descriptor.plugin_id}, {"priority", descriptor.priority},
                          {"metadata", descriptor.metadata}});
    }
    return result;
}

struct FocusContext {
    mutable std::mutex mutex;
    Json state = {
        {"active_domain", ""}, {"selected_word", ""},
        {"active_media", Json::object()}, {"active_text", Json::object()},
        {"active_book", Json::object()}, {"focus", Json::object()},
        {"last_recommend_update", Json::object()}, {"updated_at_ms", nowEpochMs()},
    };
    std::string socket_path;
    bool active = true;

    void update(const std::string& topic, const Json& payload) {
        std::lock_guard<std::mutex> lock(mutex);
        if (!active || !payload.is_object()) return;
        if (topic == "host.focus_changed") {
            for (auto field = payload.begin(); field != payload.end(); ++field) {
                state["focus"][field.key()] = field.value();
            }
            for (const char* field : {"active_media", "active_text", "active_book"}) {
                if (payload.contains(field) && payload[field].is_object()) state[field] = payload[field];
            }
            for (const char* field : {"active_domain", "domain"}) {
                if (payload.contains(field) && payload[field].is_string()) state["active_domain"] = payload[field];
            }
            for (const char* field : {"word", "selected_word"}) {
                if (payload.contains(field) && payload[field].is_string()) state["selected_word"] = payload[field];
            }
        } else if (topic == "entity.recommend.updated") {
            state["last_recommend_update"] = payload;
        } else {
            const std::string domain = topic == "entity.media.open" ? "media" :
                                       topic == "entity.text.open" ? "text" : "book";
            state["active_" + domain] = payload;
            state["active_domain"] = domain;
        }
        state["updated_at_ms"] = nowEpochMs();
    }
};

std::atomic<size_t> pending_calls{0};

struct PendingCall {
    std::mutex mutex;
    std::condition_variable ready;
    CapabilityResult result;
    bool done = false;
    ~PendingCall() { pending_calls.fetch_sub(1); }
};

} // namespace

std::string resolveControlSocketPath() {
    if (const char* value = std::getenv("OMNIDESK_CONTROL_SOCK"); value && *value) return value;
    if (const char* value = std::getenv("XDG_RUNTIME_DIR"); value && *value) {
        return (fs::path(value) / "omnidesk/control.sock").string();
    }
    fs::path data_root;
    if (const char* value = std::getenv("OMNIDESK_DATA_HOME"); value && *value) {
        data_root = value;
    } else if (const char* value = std::getenv("XDG_DATA_HOME"); value && *value) {
        data_root = fs::path(value) / "omnidesk";
    } else {
        const char* home = std::getenv("HOME");
        data_root = fs::path(home ? home : ".") / ".local/share/omnidesk";
    }
    return (data_root / "run/control.sock").string();
}

ControlResponse sendControlRequest(const Json& request, int timeout_ms, const std::string& socket_path) {
    ControlResponse response;
    try {
        const std::string wire = request.dump() + "\n";
        if (wire.size() > kMaxControlFrameBytes) throw std::runtime_error("request exceeds 8 MiB limit");
        const auto address = socketAddress(socket_path.empty() ? resolveControlSocketPath() : socket_path);
        const auto deadline = Clock::now() + std::chrono::milliseconds(std::max(1, timeout_ms));
        FileDescriptor descriptor(::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0));
        if (descriptor.value < 0) throw std::runtime_error(std::strerror(errno));
        if (::connect(descriptor.value, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
            int connect_error = errno;
            if (connect_error == EINPROGRESS) {
                std::string error;
                if (!waitFor(descriptor.value, POLLOUT, deadline, {}, error)) throw std::runtime_error(error);
                socklen_t size = sizeof(connect_error);
                if (::getsockopt(descriptor.value, SOL_SOCKET, SO_ERROR, &connect_error, &size) != 0) {
                    throw std::runtime_error(std::strerror(errno));
                }
            }
            if (connect_error != 0) {
                response.server_unreachable = connect_error == ENOENT || connect_error == ECONNREFUSED;
                throw std::runtime_error(std::string("connect failed: ") + std::strerror(connect_error));
            }
        }
        if (!sameUser(descriptor.value)) throw std::runtime_error("control socket peer UID mismatch");
        std::string error;
        if (!writeFrame(descriptor.value, wire, deadline, {}, error)) {
            throw std::runtime_error(error + "; execution uncertain, request was not replayed");
        }
        std::string pending;
        std::string line;
        if (!readFrame(descriptor.value, pending, line, deadline, {}, error)) {
            throw std::runtime_error(error + "; execution uncertain, request was not replayed");
        }
        response.raw = Json::parse(line);
        const auto& reply = response.raw;
        if (!reply.is_object() || !reply.contains("ok") || !reply["ok"].is_boolean() ||
            !reply.contains("id") || reply["id"] != request.value("id", Json(nullptr)) ||
            (reply.contains("provider_plugin_id") && !reply["provider_plugin_id"].is_string()) ||
            (reply.contains("error") && !reply["error"].is_string())) {
            throw std::runtime_error("invalid control response envelope; request was not replayed");
        }
        response.ok = reply["ok"].get<bool>();
        response.provider_plugin_id = reply.value("provider_plugin_id", "");
        response.data = reply.value("data", Json::object());
        response.error = reply.value("error", "");
        response.transport_ok = true;
    } catch (const std::exception& exception) {
        response.error = exception.what();
    }
    return response;
}

CapabilityResult callControlCapability(CapabilityRegistry& capabilities, const std::string& capability_id,
                                       const Json& payload, const std::string& plugin_id, int timeout_ms,
                                       const std::function<bool()>& interrupted) {
    CapabilityResult result;
    if (pending_calls.fetch_add(1) >= kMaxControlClients) {
        pending_calls.fetch_sub(1);
        result.error = "control capability capacity exhausted";
        return result;
    }
    auto pending = std::make_shared<PendingCall>();
    auto cancel = CancelToken::create();
    capabilities.callProviderAsync(plugin_id, capability_id, payload, [pending](const CapabilityResult& reply) {
        std::lock_guard<std::mutex> lock(pending->mutex);
        pending->result = reply;
        pending->done = true;
        pending->ready.notify_all();
    }, cancel);
    const auto deadline = Clock::now() + std::chrono::milliseconds(std::max(1, timeout_ms));
    std::unique_lock<std::mutex> lock(pending->mutex);
    while (!pending->done) {
        if (Clock::now() >= deadline || (interrupted && interrupted())) {
            lock.unlock();
            cancel->cancel();
            result.cancelled = true;
            result.error = "control capability cancelled or timed out; execution may have occurred";
            return result;
        }
        pending->ready.wait_for(lock, std::chrono::milliseconds(20));
    }
    return pending->result;
}

struct ControlServer::Impl {
    struct Client {
        std::thread thread;
        std::atomic<bool> finished{false};
    };

    CapabilityRegistry& capabilities;
    EntityBus& bus;
    PluginManager* plugins;
    PluginInstaller* installer;
    std::shared_ptr<FocusContext> context = std::make_shared<FocusContext>();
    std::mutex lifecycle_mutex;
    std::atomic<bool> running{false};
    int listen_fd = -1;
    int directory_fd = -1;
    int lock_fd = -1;
    struct stat socket_stat{};
    std::string socket_name;
    bool owns_socket = false;
    std::thread accept_thread;
    std::vector<std::shared_ptr<Client>> clients;
    std::vector<uint64_t> subscriptions;

    Impl(CapabilityRegistry& registry, EntityBus& entity_bus, PluginManager* manager, PluginInstaller* store)
        : capabilities(registry), bus(entity_bus), plugins(manager), installer(store) {}

    void cleanupSocket() {
        if (owns_socket) {
            struct stat current{};
            if (::fstatat(directory_fd, socket_name.c_str(), &current, AT_SYMLINK_NOFOLLOW) == 0 &&
                S_ISSOCK(current.st_mode) && sameInode(current, socket_stat)) {
                ::unlinkat(directory_fd, socket_name.c_str(), 0);
            }
        }
        owns_socket = false;
        for (int* descriptor : {&listen_fd, &lock_fd, &directory_fd}) {
            if (*descriptor >= 0) ::close(*descriptor);
            *descriptor = -1;
        }
        std::lock_guard<std::mutex> lock(context->mutex);
        context->socket_path.clear();
    }

    Json snapshot() const {
        Json snapshot;
        {
            std::lock_guard<std::mutex> lock(context->mutex);
            snapshot = context->state;
            snapshot["socket_path"] = context->socket_path;
        }
        snapshot["online"] = running.load();
        snapshot["pid"] = ::getpid();
        snapshot["capabilities"] = capabilitiesJson(capabilities);
        snapshot["plugins"] = Json::array();
        if (installer && plugins) {
            for (const auto& record : installer->listInstalled()) {
                snapshot["plugins"].push_back({
                    {"id", record.manifest.id}, {"name", record.manifest.name},
                    {"version", record.manifest.version}, {"enabled", record.enabled},
                    {"loaded", plugins->isLoaded(record.manifest.id)},
                });
            }
        } else {
            std::set<std::string> providers;
            for (const auto& descriptor : snapshot["capabilities"]) {
                providers.insert(descriptor["plugin_id"].get<std::string>());
            }
            for (const auto& provider : providers) {
                snapshot["plugins"].push_back({{"id", provider},
                    {"loaded", plugins ? plugins->isLoaded(provider) : true}});
            }
        }
        return snapshot;
    }

    Json dispatch(const Json& request, int descriptor) {
        if (!request.is_object()) return failure(nullptr, "request must be an object");
        const Json id = request.value("id", Json(nullptr));
        if (!id.is_null() && !id.is_string() && !id.is_number_integer()) return failure(nullptr, "invalid id");
        if (!request.contains("op") || !request["op"].is_string()) return failure(id, "op must be a string");
        for (const char* field : {"plugin_id", "caller"}) {
            if (request.contains(field) && !request[field].is_string()) {
                return failure(id, std::string(field) + " must be a string");
            }
        }
        const std::string operation = request["op"];
        if (operation == "ping") return {{"id", id}, {"ok", true}, {"data", {{"pong", true}, {"pid", ::getpid()}}}};
        if (operation == "context.get") return {{"id", id}, {"ok", true}, {"data", snapshot()}};
        if (operation == "capability.list") {
            return {{"id", id}, {"ok", true}, {"data", {{"capabilities", capabilitiesJson(capabilities)}}}};
        }
        Json payload = request.value("payload", Json::object());
        if (!payload.is_object()) return failure(id, "payload must be an object");
        if (operation == "capability.call") {
            if (!request.contains("capability") || !request["capability"].is_string() ||
                request["capability"].get<std::string>().empty()) return failure(id, "capability must be a non-empty string");
            int timeout_ms = 30000;
            if (request.contains("timeout_ms")) {
                if (!request["timeout_ms"].is_number_integer() || request["timeout_ms"] < 1 ||
                    request["timeout_ms"] > 300000) return failure(id, "timeout_ms must be between 1 and 300000");
                timeout_ms = request["timeout_ms"].get<int>();
            }
            const auto result = callControlCapability(capabilities, request["capability"], payload,
                request.value("plugin_id", ""), timeout_ms, [this, descriptor]() {
                    if (!running.load()) return true;
                    char peek;
                    const ssize_t count = ::recv(descriptor, &peek, 1, MSG_PEEK | MSG_DONTWAIT);
                    return count == 0 || (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR);
                });
            return {{"id", id}, {"ok", result.ok}, {"provider_plugin_id", result.provider_plugin_id},
                    {"data", result.data}, {"error", result.error}};
        }
        if (operation == "bus.emit") {
            if (!request.contains("topic") || !request["topic"].is_string() ||
                request["topic"].get<std::string>().empty()) return failure(id, "topic must be a non-empty string");
            const std::string topic = request["topic"];
            const size_t delivered = bus.publish(topic, payload);
            return {{"id", id}, {"ok", true}, {"data", {{"topic", topic}, {"delivered", delivered}, {"desktop_delivery", true}}}};
        }
        if (operation == "context.set") {
            if (!request.contains("payload") && request.contains("context")) payload = request["context"];
            if (!payload.is_object()) return failure(id, "context must be an object");
            context->update("host.focus_changed", payload);
            bus.publish("host.focus_changed", payload);
            return {{"id", id}, {"ok", true}, {"data", snapshot()}};
        }
        return failure(id, "unknown op: " + operation);
    }

    void handleClient(int descriptor) {
        FileDescriptor connection(descriptor);
        const auto interrupted = [this]() { return !running.load(); };
        std::string pending;
        while (running.load()) {
            std::string line;
            std::string error;
            Json reply;
            const bool received = readFrame(descriptor, pending, line,
                Clock::now() + std::chrono::milliseconds(kIoTimeoutMs), interrupted, error);
            if (!received) {
                if (error != "frame exceeds 8 MiB limit") return;
                reply = failure(nullptr, error);
            } else {
                Json request;
                try {
                    request = Json::parse(line);
                    reply = dispatch(request, descriptor);
                } catch (const std::exception& exception) {
                    reply = failure(request.is_object() ? request.value("id", Json(nullptr)) : Json(nullptr),
                                    std::string("invalid request: ") + exception.what());
                }
            }
            std::string wire = reply.dump(-1, ' ', false, Json::error_handler_t::replace) + "\n";
            if (wire.size() > kMaxControlFrameBytes) {
                wire = failure(reply.value("id", Json(nullptr)), "response exceeds 8 MiB limit").dump() + "\n";
            }
            if (!writeFrame(descriptor, wire, Clock::now() + std::chrono::milliseconds(kIoTimeoutMs),
                            interrupted, error) || !received) return;
        }
    }

    void acceptLoop() {
        while (running.load()) {
            for (auto iterator = clients.begin(); iterator != clients.end();) {
                if ((*iterator)->finished.load()) {
                    (*iterator)->thread.join();
                    iterator = clients.erase(iterator);
                } else {
                    ++iterator;
                }
            }
            pollfd listener{listen_fd, POLLIN, 0};
            const int ready = ::poll(&listener, 1, 50);
            if (ready < 0 && errno == EINTR) continue;
            if (ready < 0 || (listener.revents & (POLLERR | POLLHUP | POLLNVAL))) break;
            if (ready == 0) continue;
            const int descriptor = ::accept4(listen_fd, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
            if (descriptor < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                break;
            }
            if (!sameUser(descriptor) || clients.size() >= kMaxControlClients) {
                ::close(descriptor);
                continue;
            }
            auto client = std::make_shared<Client>();
            try {
                clients.push_back(client);
                client->thread = std::thread([this, client, descriptor]() {
                    try { handleClient(descriptor); } catch (...) {}
                    client->finished.store(true);
                });
            } catch (...) {
                if (!clients.empty() && clients.back() == client) clients.pop_back();
                ::close(descriptor);
            }
        }
        running.store(false);
    }
};

ControlServer::ControlServer(CapabilityRegistry& capabilities, EntityBus& bus,
                             PluginManager* plugins, PluginInstaller* installer)
    : impl_(std::make_unique<Impl>(capabilities, bus, plugins, installer)) {}

ControlServer::~ControlServer() { stop(); }

bool ControlServer::start(std::string* error, const std::string& socket_path) {
    std::lock_guard<std::mutex> lifecycle(impl_->lifecycle_mutex);
    if (impl_->running.load()) return true;
    if (impl_->accept_thread.joinable()) {
        if (error) *error = "stop the previous control server before restarting";
        return false;
    }
    try {
        const fs::path path = fs::absolute(socket_path.empty() ? resolveControlSocketPath() : socket_path);
        socketAddress(path.string());
        const fs::path parent = path.parent_path();
        fs::create_directories(parent);
        impl_->directory_fd = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        struct stat directory_stat{};
        if (impl_->directory_fd < 0 || ::fstat(impl_->directory_fd, &directory_stat) != 0 ||
            directory_stat.st_uid != ::geteuid() || (directory_stat.st_mode & S_ISVTX)) {
            throw std::runtime_error("control socket requires a dedicated, same-UID directory without symlinks");
        }
        if (::fchmod(impl_->directory_fd, 0700) != 0) throw std::runtime_error("cannot secure control directory");
        impl_->socket_name = path.filename().string();
        const std::string lock_name = impl_->socket_name + ".lock";
        impl_->lock_fd = ::openat(impl_->directory_fd, lock_name.c_str(),
                                 O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
        struct stat lock_stat{};
        if (impl_->lock_fd < 0 || ::fstat(impl_->lock_fd, &lock_stat) != 0 ||
            !S_ISREG(lock_stat.st_mode) || lock_stat.st_uid != ::geteuid() || lock_stat.st_nlink != 1 ||
            ::fchmod(impl_->lock_fd, 0600) != 0 || ::flock(impl_->lock_fd, LOCK_EX | LOCK_NB) != 0) {
            throw std::runtime_error("control socket ownership lock unavailable");
        }
        const std::string anchored_path = "/proc/self/fd/" + std::to_string(impl_->directory_fd) + "/" + impl_->socket_name;
        const auto address = socketAddress(anchored_path);
        struct stat previous{};
        if (::fstatat(impl_->directory_fd, impl_->socket_name.c_str(), &previous, AT_SYMLINK_NOFOLLOW) == 0) {
            if (!S_ISSOCK(previous.st_mode) || previous.st_uid != ::geteuid()) {
                throw std::runtime_error("existing control path is not a same-UID socket");
            }
            FileDescriptor probe(::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
            if (probe.value < 0) throw std::runtime_error("cannot probe existing control socket");
            const int connected = ::connect(probe.value, reinterpret_cast<const sockaddr*>(&address), sizeof(address));
            if (connected == 0 || errno != ECONNREFUSED) {
                throw std::runtime_error("existing control socket is active or its state is uncertain");
            }
            struct stat checked{};
            if (::fstatat(impl_->directory_fd, impl_->socket_name.c_str(), &checked, AT_SYMLINK_NOFOLLOW) != 0 ||
                !sameInode(previous, checked) || !S_ISSOCK(checked.st_mode) ||
                ::unlinkat(impl_->directory_fd, impl_->socket_name.c_str(), 0) != 0) {
                throw std::runtime_error("stale control socket changed during inspection");
            }
        } else if (errno != ENOENT) {
            throw std::runtime_error("cannot inspect existing control socket");
        }
        impl_->listen_fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (impl_->listen_fd < 0 ||
            ::bind(impl_->listen_fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
            throw std::runtime_error(std::string("control socket bind failed: ") + std::strerror(errno));
        }
        if (::fstatat(impl_->directory_fd, impl_->socket_name.c_str(), &impl_->socket_stat, AT_SYMLINK_NOFOLLOW) != 0) {
            throw std::runtime_error("cannot identify bound control socket");
        }
        impl_->owns_socket = true;
        struct stat visible_directory{};
        if (::lstat(parent.c_str(), &visible_directory) != 0 || !sameInode(directory_stat, visible_directory) ||
            ::chmod(anchored_path.c_str(), 0600) != 0 || ::listen(impl_->listen_fd, 32) != 0) {
            throw std::runtime_error("cannot secure or listen on control socket");
        }
        {
            std::lock_guard<std::mutex> lock(impl_->context->mutex);
            impl_->context->socket_path = path.string();
            impl_->context->active = true;
        }
        for (const char* topic : {"host.focus_changed", "entity.media.open", "entity.text.open",
                                 "entity.book.open", "entity.recommend.updated"}) {
            impl_->subscriptions.push_back(impl_->bus.subscribe(topic, "host.control_bridge",
                [context = impl_->context](const std::string& event, const Json& payload) { context->update(event, payload); }));
        }
        impl_->running.store(true);
        impl_->accept_thread = std::thread([this]() { impl_->acceptLoop(); });
        return true;
    } catch (const std::exception& exception) {
        impl_->running.store(false);
        for (uint64_t token : impl_->subscriptions) impl_->bus.unsubscribe(token);
        impl_->subscriptions.clear();
        impl_->cleanupSocket();
        if (error) *error = exception.what();
        return false;
    }
}

void ControlServer::stop() {
    std::lock_guard<std::mutex> lifecycle(impl_->lifecycle_mutex);
    impl_->running.store(false);
    {
        std::lock_guard<std::mutex> lock(impl_->context->mutex);
        impl_->context->active = false;
    }
    for (uint64_t token : impl_->subscriptions) impl_->bus.unsubscribe(token);
    impl_->subscriptions.clear();
    if (impl_->accept_thread.joinable()) impl_->accept_thread.join();
    for (const auto& client : impl_->clients) client->thread.join();
    impl_->clients.clear();
    impl_->cleanupSocket();
}

bool ControlServer::isRunning() const { return impl_->running.load(); }

std::string ControlServer::socketPath() const {
    std::lock_guard<std::mutex> lock(impl_->context->mutex);
    return impl_->context->socket_path;
}

Json ControlServer::getContextSnapshot() const { return impl_->snapshot(); }

void ControlServer::setContextSnapshot(const Json& patch) { impl_->context->update("host.focus_changed", patch); }

} // namespace omnidesk::pm
