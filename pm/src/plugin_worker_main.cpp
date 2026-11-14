#include <cerrno>
#include <csignal>
#include <cstddef>
#include <cstring>
#include <dlfcn.h>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
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
        while (true) {
            size_t nl = buf_.find('\n');
            if (nl != std::string::npos) {
                out_line = buf_.substr(0, nl);
                buf_.erase(0, nl + 1);
                if (!out_line.empty() && out_line.back() == '\r') {
                    out_line.pop_back();
                }
                return true;
            }
            char tmp[4096];
            ssize_t n = ::read(read_fd_, tmp, sizeof(tmp));
            if (n < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            if (n == 0) {
                if (!buf_.empty()) {
                    out_line = std::move(buf_);
                    buf_.clear();
                    return true;
                }
                return false;
            }
            buf_.append(tmp, static_cast<size_t>(n));
            if (buf_.size() > 8 * 1024 * 1024) return false;
        }
    }

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

}  // namespace

int main(int argc, char** argv) {
    ::signal(SIGPIPE, SIG_IGN);

    std::string plugin_id;
    std::string so_path;
    std::string plugin_dir;
    std::string data_dir;
    std::string config_dir;
    std::string socket_path;
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

    for (int i = 1; i + 1 < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--plugin-id") plugin_id = argv[++i];
        else if (a == "--so") so_path = argv[++i];
        else if (a == "--plugin-dir") plugin_dir = argv[++i];
        else if (a == "--data-dir") data_dir = argv[++i];
        else if (a == "--config-dir") config_dir = argv[++i];
        else if (a == "--socket") socket_path = argv[++i];
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

            if (op == "text.fetch_feed") {
                auto src = findSourceById(domains.textSources(), req.value("source_id", ""));
                if (!src) {
                    channel.sendJson(makeErr("text source not found"));
                } else {
                    auto items = src->fetchFeed(req.value("query", ""));
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
