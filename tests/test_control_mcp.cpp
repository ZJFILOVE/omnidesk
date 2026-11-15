#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <poll.h>
#include <set>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/config_store.hpp"
#include "omnidesk/domain_registry.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/job_center.hpp"
#include "omnidesk/pm/control_bridge.hpp"
#include "omnidesk/pm/installer.hpp"
#include "omnidesk/pm/manager.hpp"
#include "omnidesk/pm/process.hpp"
#include "omnidesk/worker_supervisor.hpp"

#define TEST_CHECK(cond)                                                               \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            std::cerr << "TEST_CHECK failed at " << __FILE__ << ":" << __LINE__        \
                      << " (" << #cond << ")\n";                                       \
            std::exit(1);                                                              \
        }                                                                              \
    } while (false)

namespace fs = std::filesystem;

namespace {

struct ScopedEnv {
    std::string key;
    bool had_old = false;
    std::string old_val;

    ScopedEnv(const std::string& k, const char* new_val) : key(k) {
        if (const char* cur = std::getenv(key.c_str())) {
            had_old = true;
            old_val = cur;
        }
        if (new_val) {
            ::setenv(key.c_str(), new_val, 1);
        } else {
            ::unsetenv(key.c_str());
        }
    }

    ~ScopedEnv() {
        if (had_old) {
            ::setenv(key.c_str(), old_val.c_str(), 1);
        } else {
            ::unsetenv(key.c_str());
        }
    }
};

struct CliOutput {
    int exit_code = -1;
    std::string stdout_text;
};

CliOutput runCliStdout(const std::vector<std::string>& argv_vec) {
    CliOutput res;
    int out_pipe[2] = {-1, -1};
    if (::pipe(out_pipe) != 0) {
        return res;
    }
    pid_t pid = ::fork();
    if (pid < 0) {
        ::close(out_pipe[0]);
        ::close(out_pipe[1]);
        return res;
    }
    if (pid == 0) {
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::close(out_pipe[0]);
        ::close(out_pipe[1]);
        std::vector<const char*> c_argv;
        c_argv.reserve(argv_vec.size() + 1);
        for (const auto& a : argv_vec) {
            c_argv.push_back(a.c_str());
        }
        c_argv.push_back(nullptr);
        ::execvp(c_argv[0], const_cast<char* const*>(c_argv.data()));
        ::_exit(127);
    }
    ::close(out_pipe[1]);
    char buf[4096];
    while (true) {
        ssize_t n = ::read(out_pipe[0], buf, sizeof(buf));
        if (n > 0) {
            res.stdout_text.append(buf, static_cast<size_t>(n));
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else {
            break;
        }
    }
    ::close(out_pipe[0]);
    int status = 0;
    ::waitpid(pid, &status, 0);
    res.exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    return res;
}

struct McpChild {
    pid_t pid = -1;
    int write_fd = -1;
    int read_fd = -1;
    std::string read_buf;

    bool start(const std::string& pm_exe, const std::vector<std::string>& extra_args = {}) {
        int in_pipe[2] = {-1, -1};
        int out_pipe[2] = {-1, -1};
        if (::pipe(in_pipe) != 0 || ::pipe(out_pipe) != 0) {
            return false;
        }
        pid = ::fork();
        if (pid < 0) {
            ::close(in_pipe[0]);
            ::close(in_pipe[1]);
            ::close(out_pipe[0]);
            ::close(out_pipe[1]);
            return false;
        }
        if (pid == 0) {
            ::dup2(in_pipe[0], STDIN_FILENO);
            ::dup2(out_pipe[1], STDOUT_FILENO);
            ::close(in_pipe[0]);
            ::close(in_pipe[1]);
            ::close(out_pipe[0]);
            ::close(out_pipe[1]);

            std::vector<const char*> argv;
            argv.push_back(pm_exe.c_str());
            argv.push_back("mcp-serve");
            for (const auto& a : extra_args) {
                argv.push_back(a.c_str());
            }
            argv.push_back(nullptr);
            ::execvp(pm_exe.c_str(), const_cast<char* const*>(argv.data()));
            ::_exit(127);
        }
        ::close(in_pipe[0]);
        ::close(out_pipe[1]);
        write_fd = in_pipe[1];
        read_fd = out_pipe[0];
        return true;
    }

    bool sendJson(const nlohmann::json& j) {
        std::string line = j.dump() + "\n";
        size_t sent = 0;
        while (sent < line.size()) {
            ssize_t n = ::write(write_fd, line.data() + sent, line.size() - sent);
            if (n < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            sent += static_cast<size_t>(n);
        }
        return true;
    }

    bool readJsonLine(nlohmann::json& out, int timeout_ms = 10000) {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (true) {
            auto nl = read_buf.find('\n');
            if (nl != std::string::npos) {
                std::string line = read_buf.substr(0, nl);
                read_buf.erase(0, nl + 1);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                if (line.empty()) continue;
                out = nlohmann::json::parse(line);
                return true;
            }
            const auto rem = std::chrono::duration_cast<std::chrono::milliseconds>(
                                 deadline - std::chrono::steady_clock::now())
                                 .count();
            if (rem <= 0) {
                return false;
            }
            struct pollfd pfd{};
            pfd.fd = read_fd;
            pfd.events = POLLIN;
            int prc = ::poll(&pfd, 1, static_cast<int>(rem));
            if (prc < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            if (prc == 0) {
                return false;
            }
            char buf[4096];
            ssize_t n = ::read(read_fd, buf, sizeof(buf));
            if (n < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            if (n == 0) {
                return false;
            }
            read_buf.append(buf, static_cast<size_t>(n));
        }
    }

    int closeAndWait() {
        if (write_fd >= 0) {
            ::close(write_fd);
            write_fd = -1;
        }
        if (read_fd >= 0) {
            ::close(read_fd);
            read_fd = -1;
        }
        int status = 0;
        if (pid > 0) {
            ::waitpid(pid, &status, 0);
            pid = -1;
        }
        return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    }

    ~McpChild() {
        closeAndWait();
    }
};

} // namespace

int main() {
    const fs::path tmp_root =
        fs::temp_directory_path() / ("omnidesk_ctrl_mcp_test_" + std::to_string(::getpid()));
    std::error_code ec;
    fs::remove_all(tmp_root, ec);
    fs::create_directories(tmp_root, ec);

    const std::string data_home = (tmp_root / "data").string();
    const std::string cfg_home = (tmp_root / "config").string();
    const std::string runtime_dir = (tmp_root / "runtime").string();
    fs::create_directories(data_home, ec);
    fs::create_directories(cfg_home, ec);
    fs::create_directories(runtime_dir, ec);

    ScopedEnv env_data("OMNIDESK_DATA_HOME", data_home.c_str());
    ScopedEnv env_cfg("OMNIDESK_CONFIG_HOME", cfg_home.c_str());
    ScopedEnv env_sdk("OMNIDESK_SDK_CMAKE_DIR", OMNIDESK_TEST_SDK_CMAKE_DIR);
    ScopedEnv env_gh("OMNIDESK_DISABLE_GITHUB_API", "1");

    // 1. Socket path resolution order
    {
        ScopedEnv e_ctrl("OMNIDESK_CONTROL_SOCK", "/tmp/custom-omnidesk.sock");
        ScopedEnv e_rt("XDG_RUNTIME_DIR", runtime_dir.c_str());
        TEST_CHECK(omnidesk::pm::resolveControlSocketPath() == "/tmp/custom-omnidesk.sock");
    }
    {
        ScopedEnv e_ctrl("OMNIDESK_CONTROL_SOCK", nullptr);
        ScopedEnv e_rt("XDG_RUNTIME_DIR", runtime_dir.c_str());
        TEST_CHECK(omnidesk::pm::resolveControlSocketPath() ==
                   (fs::path(runtime_dir) / "omnidesk" / "control.sock").string());
    }
    {
        ScopedEnv e_ctrl("OMNIDESK_CONTROL_SOCK", nullptr);
        ScopedEnv e_rt("XDG_RUNTIME_DIR", nullptr);
        TEST_CHECK(omnidesk::pm::resolveControlSocketPath() ==
                   (fs::path(data_home) / "run" / "control.sock").string());
    }

    const std::string sock_path = (fs::path(runtime_dir) / "omnidesk" / "control.sock").string();
    ScopedEnv env_ctrl("OMNIDESK_CONTROL_SOCK", sock_path.c_str());
    ScopedEnv env_rt("XDG_RUNTIME_DIR", runtime_dir.c_str());

    // 2. Install test_plugin_sample so we can verify both online and offline CLI/MCP execution
    const std::string pm_exe = OMNIDESK_TEST_PM_EXE;
    {
        omnidesk::pm::PluginInstaller installer(data_home, OMNIDESK_TEST_SDK_CMAKE_DIR, pm_exe);
        auto res = installer.installFromLocal(OMNIDESK_TEST_SAMPLE_PLUGIN);
        TEST_CHECK(res.ok);
        TEST_CHECK(res.plugin_id == "plugin-sample-echo");
    }

    // 3. Stale socket cleanup, 0600 permissions, ping, capability.call, bus.emit, context.get/set
    {
        fs::create_directories(fs::path(sock_path).parent_path(), ec);
        int stale_fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
        TEST_CHECK(stale_fd >= 0);
        struct sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", sock_path.c_str());
        TEST_CHECK(::bind(stale_fd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) == 0);
        ::close(stale_fd);
        TEST_CHECK(fs::exists(sock_path));

        omnidesk::CapabilityRegistry caps;
        omnidesk::EntityBus bus;
        omnidesk::DomainRegistry domains;
        omnidesk::ConfigStore config(cfg_home);
        omnidesk::SecretVault secrets(cfg_home);
        omnidesk::JobCenter jobs(&bus, 2);
        omnidesk::WorkerSupervisor workers;
        omnidesk::pm::PluginInstaller installer(data_home, OMNIDESK_TEST_SDK_CMAKE_DIR, pm_exe);
        omnidesk::pm::PluginManager manager(installer, caps, bus, domains, config, secrets, &jobs, &workers);
        manager.setWorkerExecutable(OMNIDESK_TEST_WORKER_EXE);
        manager.loadAllEnabled();

        caps.registerCapability(
            "recommend.feed.v1", "mock-curator",
            [](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                omnidesk::CapabilityResult r;
                r.ok = true;
                r.data = {
                    {"action", req.value("action", "")},
                    {"dates", nlohmann::json::array({"2026-11-14", "2026-11-13"})},
                };
                return r;
            });

        caps.registerCapability(
            "book.search.v1", "plugin-zlib",
            [](const nlohmann::json&) -> omnidesk::CapabilityResult {
                omnidesk::CapabilityResult r;
                r.ok = false;
                r.error = "Z-Library account not configured";
                r.data = {{"error_code", "NOT_CONFIGURED"}};
                return r;
            },
            {{"provider", "zlib"}}, 90);

        caps.registerCapability(
            "book.search.v1", "plugin-annas-archive",
            [](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                omnidesk::CapabilityResult r;
                r.ok = true;
                const std::string act = req.value("action", "");
                if (act == "search") {
                    r.data = {
                        {"provider", "annas-archive"},
                        {"query", req.value("query", "")},
                        {"books", nlohmann::json::array({
                            {
                                {"md5", "fedcba9876543210fedcba9876543210"},
                                {"title", "Operating Systems: Three Easy Pieces"},
                                {"author", "Remzi Arpaci-Dusseau"},
                                {"extension", "pdf"},
                            },
                        })},
                    };
                } else if (act == "download") {
                    r.data = {
                        {"provider", "annas-archive"},
                        {"md5", req.value("md5", req.value("id", ""))},
                        {"title", "Operating Systems: Three Easy Pieces"},
                        {"format", "pdf"},
                        {"file_path", "/tmp/books/ostep.pdf"},
                        {"bytes_written", 2097152},
                        {"shelf_book_id", "annas-ostep"},
                    };
                }
                return r;
            },
            {{"provider", "annas-archive"}}, 85);

        omnidesk::pm::ControlServer server(caps, bus, &manager);
        std::string start_err;
        TEST_CHECK(server.start(&start_err));
        TEST_CHECK(server.isRunning());
        TEST_CHECK(server.socketPath() == sock_path);

        struct stat st{};
        TEST_CHECK(::stat(sock_path.c_str(), &st) == 0);
        TEST_CHECK((st.st_mode & 0777) == 0600);

        struct stat pst{};
        TEST_CHECK(::stat(fs::path(sock_path).parent_path().c_str(), &pst) == 0);
        TEST_CHECK((pst.st_mode & 0777) == 0700);

        // Second server on same active socket must fail without clobbering
        omnidesk::pm::ControlServer duplicate(caps, bus, &manager);
        std::string dup_err;
        TEST_CHECK(!duplicate.start(&dup_err, sock_path));

        // ping
        auto ping_resp = omnidesk::pm::sendControlRequest({{"id", "p1"}, {"op", "ping"}});
        TEST_CHECK(ping_resp.transport_ok);
        TEST_CHECK(ping_resp.ok);
        TEST_CHECK(ping_resp.data.value("pong", false) == true);
        TEST_CHECK(ping_resp.data.value("pid", 0) == static_cast<int>(::getpid()));

        // capability.list
        auto list_resp = omnidesk::pm::sendControlRequest({{"id", "l1"}, {"op", "capability.list"}});
        TEST_CHECK(list_resp.transport_ok && list_resp.ok);
        TEST_CHECK(list_resp.data.contains("capabilities") && list_resp.data["capabilities"].is_array());
        TEST_CHECK(list_resp.data["capabilities"].size() >= 2);

        // capability.call
        auto call_resp = omnidesk::pm::sendControlRequest({
            {"id", "c1"},
            {"op", "capability.call"},
            {"capability", "sample.echo.v1"},
            {"payload", {{"hello", "bridge"}}},
            {"caller", "test-harness"},
        });
        TEST_CHECK(call_resp.transport_ok && call_resp.ok);
        TEST_CHECK(call_resp.provider_plugin_id == "plugin-sample-echo");
        TEST_CHECK(call_resp.data["echo"]["hello"] == "bridge");

        // bus.emit + subscriber check
        std::string observed_topic;
        nlohmann::json observed_payload;
        uint64_t sub_tok = bus.subscribe("entity.recommend.updated", "test",
            [&](const std::string& t, const nlohmann::json& p) {
                observed_topic = t;
                observed_payload = p;
            });
        auto emit_resp = omnidesk::pm::sendControlRequest({
            {"id", "e1"},
            {"op", "bus.emit"},
            {"topic", "entity.recommend.updated"},
            {"payload", {{"date", "2026-11-14"}, {"count", 5}}},
        });
        TEST_CHECK(emit_resp.transport_ok && emit_resp.ok);
        TEST_CHECK(emit_resp.data["topic"] == "entity.recommend.updated");
        TEST_CHECK(observed_topic == "entity.recommend.updated");
        TEST_CHECK(observed_payload["count"] == 5);
        bus.unsubscribe(sub_tok);

        // Live EntityBus context tracking
        bus.publish("entity.media.open", {{"id", "trk-1"}, {"title", "So What"}, {"source_id", "plugin-music"}});
        bus.publish("entity.text.open", {{"id", "art-2"}, {"title", "Attention Paper"}, {"page", 4}});
        bus.publish("entity.book.open", {{"id", "bk-3"}, {"title", "Systems Book"}, {"page", 128}});
        bus.publish("host.focus_changed", {{"selected_word", "ephemeral"}});

        auto ctx_resp = omnidesk::pm::sendControlRequest({{"id", "g1"}, {"op", "context.get"}});
        TEST_CHECK(ctx_resp.transport_ok && ctx_resp.ok);
        TEST_CHECK(ctx_resp.data["online"] == true);
        TEST_CHECK(ctx_resp.data["active_media"]["title"] == "So What");
        TEST_CHECK(ctx_resp.data["active_text"]["title"] == "Attention Paper");
        TEST_CHECK(ctx_resp.data["active_book"]["page"] == 128);
        TEST_CHECK(ctx_resp.data["last_recommend_update"]["date"] == "2026-11-14");
        TEST_CHECK(ctx_resp.data["selected_word"] == "ephemeral");
        TEST_CHECK(ctx_resp.data["active_domain"] == "book");

        // context.set updates snapshot and emits host.focus_changed
        nlohmann::json focus_payload;
        uint64_t focus_tok = bus.subscribe("host.focus_changed", "test",
            [&](const std::string&, const nlohmann::json& p) { focus_payload = p; });
        auto set_resp = omnidesk::pm::sendControlRequest({
            {"id", "s1"},
            {"op", "context.set"},
            {"payload", {{"domain", "text"}, {"selected_word", "serendipity"}}},
        });
        TEST_CHECK(set_resp.transport_ok && set_resp.ok);
        TEST_CHECK(set_resp.data["active_domain"] == "text");
        TEST_CHECK(set_resp.data["selected_word"] == "serendipity");
        TEST_CHECK(focus_payload["selected_word"] == "serendipity");
        bus.unsubscribe(focus_tok);

        // 4. Online CLI omnidesk-pm rpc / emit / context
        {
            auto rpc_res = runCliStdout(
                {pm_exe, "rpc", "sample.echo.v1", R"({"msg":"cli-online"})"});
            TEST_CHECK(rpc_res.exit_code == 0);
            auto j = nlohmann::json::parse(rpc_res.stdout_text);
            TEST_CHECK(j["ok"] == true);
            TEST_CHECK(j["transport"] == "control_socket");
            TEST_CHECK(j["provider_plugin_id"] == "plugin-sample-echo");
            TEST_CHECK(j["data"]["echo"]["msg"] == "cli-online");
        }
        {
            const fs::path payload_file = tmp_root / "payload.json";
            {
                std::ofstream ofs(payload_file);
                ofs << R"({"from_file":true,"n":7})";
            }
            auto rpc_res = runCliStdout(
                {pm_exe, "rpc", "sample.echo.v1", "--file", payload_file.string(), "--plugin", "plugin-sample-echo"});
            TEST_CHECK(rpc_res.exit_code == 0);
            auto j = nlohmann::json::parse(rpc_res.stdout_text);
            TEST_CHECK(j["ok"] == true);
            TEST_CHECK(j["data"]["echo"]["from_file"] == true);
            TEST_CHECK(j["data"]["echo"]["n"] == 7);
        }
        {
            auto emit_cli = runCliStdout(
                {pm_exe, "emit", "entity.media.open", R"({"id":"cli-m","title":"CLI Track"})"});
            TEST_CHECK(emit_cli.exit_code == 0);
            auto ctx_cli = runCliStdout({pm_exe, "context", "--json"});
            TEST_CHECK(ctx_cli.exit_code == 0);
            auto j = nlohmann::json::parse(ctx_cli.stdout_text);
            TEST_CHECK(j["online"] == true);
            TEST_CHECK(j["active_media"]["title"] == "CLI Track");
        }

        // 5. Online MCP server (omnidesk-pm mcp-serve)
        {
            McpChild mcp;
            TEST_CHECK(mcp.start(pm_exe));

            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 1},
                {"method", "initialize"},
                {"params", {{"protocolVersion", "2024-11-05"}, {"capabilities", nlohmann::json::object()}}},
            }));
            nlohmann::json init_resp;
            TEST_CHECK(mcp.readJsonLine(init_resp));
            TEST_CHECK(init_resp["id"] == 1);
            TEST_CHECK(init_resp["result"]["protocolVersion"] == "2024-11-05");
            TEST_CHECK(init_resp["result"]["serverInfo"]["name"] == "omnidesk-mcp");
            TEST_CHECK(init_resp["result"]["capabilities"].contains("tools"));
            TEST_CHECK(init_resp["result"]["capabilities"].contains("resources"));

            // Notification must produce no response
            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"method", "notifications/initialized"},
            }));

            TEST_CHECK(mcp.sendJson({{"jsonrpc", "2.0"}, {"id", 2}, {"method", "ping"}}));
            nlohmann::json ping_mcp;
            TEST_CHECK(mcp.readJsonLine(ping_mcp));
            TEST_CHECK(ping_mcp["id"] == 2);
            TEST_CHECK(ping_mcp["result"].is_object());

            // resources/list & resources/read
            TEST_CHECK(mcp.sendJson({{"jsonrpc", "2.0"}, {"id", 3}, {"method", "resources/list"}}));
            nlohmann::json res_list;
            TEST_CHECK(mcp.readJsonLine(res_list));
            TEST_CHECK(res_list["id"] == 3);
            TEST_CHECK(res_list["result"]["resources"].size() == 2);

            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 4},
                {"method", "resources/read"},
                {"params", {{"uri", "omnidesk://context/active"}}},
            }));
            nlohmann::json res_read;
            TEST_CHECK(mcp.readJsonLine(res_read));
            TEST_CHECK(res_read["id"] == 4);
            auto ctx_res_json = nlohmann::json::parse(res_read["result"]["contents"][0]["text"].get<std::string>());
            TEST_CHECK(ctx_res_json["online"] == true);
            TEST_CHECK(ctx_res_json["active_media"]["title"] == "CLI Track");

            // tools/list
            TEST_CHECK(mcp.sendJson({{"jsonrpc", "2.0"}, {"id", 5}, {"method", "tools/list"}}));
            nlohmann::json tools_list;
            TEST_CHECK(mcp.readJsonLine(tools_list));
            TEST_CHECK(tools_list["id"] == 5);
            std::set<std::string> tool_names;
            for (const auto& t : tools_list["result"]["tools"]) {
                tool_names.insert(t["name"].get<std::string>());
            }
            TEST_CHECK(tool_names.count("omnidesk_get_active_context") == 1);
            TEST_CHECK(tool_names.count("omnidesk_book_search") == 1);
            TEST_CHECK(tool_names.count("omnidesk_book_download") == 1);
            TEST_CHECK(tool_names.count("omnidesk_recommend_feed") == 1);
            TEST_CHECK(tool_names.count("omnidesk_media_playlist") == 1);
            TEST_CHECK(tool_names.count("omnidesk_ai_curation") == 1);
            TEST_CHECK(tool_names.count("omnidesk_call_capability") == 1);
            TEST_CHECK(tool_names.count("omnidesk_emit_event") == 1);

            // tools/call: omnidesk_get_active_context
            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 6},
                {"method", "tools/call"},
                {"params", {{"name", "omnidesk_get_active_context"}, {"arguments", nlohmann::json::object()}}},
            }));
            nlohmann::json tool_ctx;
            TEST_CHECK(mcp.readJsonLine(tool_ctx));
            TEST_CHECK(tool_ctx["id"] == 6);
            TEST_CHECK(tool_ctx["result"]["isError"] == false);
            auto parsed_ctx = nlohmann::json::parse(tool_ctx["result"]["content"][0]["text"].get<std::string>());
            TEST_CHECK(parsed_ctx["online"] == true);
            TEST_CHECK(parsed_ctx["active_media"]["title"] == "CLI Track");

            // tools/call: omnidesk_call_capability
            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 7},
                {"method", "tools/call"},
                {"params", {
                    {"name", "omnidesk_call_capability"},
                    {"arguments", {
                        {"capability_id", "sample.echo.v1"},
                        {"payload", {{"from", "mcp-online"}}},
                    }},
                }},
            }));
            nlohmann::json tool_cap;
            TEST_CHECK(mcp.readJsonLine(tool_cap));
            TEST_CHECK(tool_cap["id"] == 7);
            TEST_CHECK(tool_cap["result"]["isError"] == false);
            auto parsed_cap = nlohmann::json::parse(tool_cap["result"]["content"][0]["text"].get<std::string>());
            TEST_CHECK(parsed_cap["ok"] == true);
            TEST_CHECK(parsed_cap["transport"] == "control_socket");
            TEST_CHECK(parsed_cap["data"]["echo"]["from"] == "mcp-online");

            // tools/call: omnidesk_recommend_feed
            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 8},
                {"method", "tools/call"},
                {"params", {
                    {"name", "omnidesk_recommend_feed"},
                    {"arguments", {{"action", "list_archive_dates"}}},
                }},
            }));
            nlohmann::json tool_rec;
            TEST_CHECK(mcp.readJsonLine(tool_rec));
            TEST_CHECK(tool_rec["result"]["isError"] == false);
            auto parsed_rec = nlohmann::json::parse(tool_rec["result"]["content"][0]["text"].get<std::string>());
            TEST_CHECK(parsed_rec["ok"] == true);
            TEST_CHECK(parsed_rec["data"]["action"] == "list_archive_dates");

            // tools/call: omnidesk_book_search (fallback plugin-zlib -> plugin-annas-archive)
            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 801},
                {"method", "tools/call"},
                {"params", {
                    {"name", "omnidesk_book_search"},
                    {"arguments", {{"query", "Three Easy Pieces"}, {"format", "pdf"}}},
                }},
            }));
            nlohmann::json tool_bsearch;
            TEST_CHECK(mcp.readJsonLine(tool_bsearch));
            TEST_CHECK(tool_bsearch["id"] == 801);
            TEST_CHECK(tool_bsearch["result"]["isError"] == false);
            auto parsed_bsearch = nlohmann::json::parse(tool_bsearch["result"]["content"][0]["text"].get<std::string>());
            TEST_CHECK(parsed_bsearch["ok"] == true);
            TEST_CHECK(parsed_bsearch["provider"] == "annas-archive");
            TEST_CHECK(parsed_bsearch["data"]["count"] == 1);
            TEST_CHECK(parsed_bsearch["data"]["books"][0]["download_id"] == "fedcba9876543210fedcba9876543210");

            // tools/call: omnidesk_book_download (auto-infers annas-archive from 32-hex MD5 + opens in workbench)
            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 802},
                {"method", "tools/call"},
                {"params", {
                    {"name", "omnidesk_book_download"},
                    {"arguments", {
                        {"id", "fedcba9876543210fedcba9876543210"},
                        {"open_in_workbench", true},
                    }},
                }},
            }));
            nlohmann::json tool_bdl;
            TEST_CHECK(mcp.readJsonLine(tool_bdl));
            TEST_CHECK(tool_bdl["id"] == 802);
            TEST_CHECK(tool_bdl["result"]["isError"] == false);
            auto parsed_bdl = nlohmann::json::parse(tool_bdl["result"]["content"][0]["text"].get<std::string>());
            TEST_CHECK(parsed_bdl["ok"] == true);
            TEST_CHECK(parsed_bdl["provider"] == "annas-archive");
            TEST_CHECK(parsed_bdl["data"]["file_path"] == "/tmp/books/ostep.pdf");
            TEST_CHECK(parsed_bdl["data"]["opened_in_workbench"] == true);
            TEST_CHECK(server.getContextSnapshot()["active_book"]["file_path"] == "/tmp/books/ostep.pdf");

            // tools/call: omnidesk_emit_event
            TEST_CHECK(mcp.sendJson({
                {"jsonrpc", "2.0"},
                {"id", 9},
                {"method", "tools/call"},
                {"params", {
                    {"name", "omnidesk_emit_event"},
                    {"arguments", {
                        {"topic", "entity.book.open"},
                        {"payload", {{"title", "MCP Emitted Book"}, {"page", 99}}},
                    }},
                }},
            }));
            nlohmann::json tool_emit;
            TEST_CHECK(mcp.readJsonLine(tool_emit));
            TEST_CHECK(tool_emit["result"]["isError"] == false);
            TEST_CHECK(server.getContextSnapshot()["active_book"]["title"] == "MCP Emitted Book");

            TEST_CHECK(mcp.closeAndWait() == 0);
        }

        server.stop();
        TEST_CHECK(!server.isRunning());
        TEST_CHECK(!fs::exists(sock_path));
        manager.unloadAll();
        jobs.shutdown(1000);
        workers.stopAll();
        caps.shutdown(1000);
        domains.shutdown(1000);
    }

    // 6. Offline fallback for omnidesk-pm rpc, context, and mcp-serve (socket stopped)
    {
        auto offline_rpc = runCliStdout(
            {pm_exe, "rpc", "sample.echo.v1", R"({"msg":"cli-offline"})"});
        TEST_CHECK(offline_rpc.exit_code == 0);
        auto j = nlohmann::json::parse(offline_rpc.stdout_text);
        TEST_CHECK(j["ok"] == true);
        TEST_CHECK(j["offline_fallback"] == true);
        TEST_CHECK(j["transport"] == "offline_inprocess");
        TEST_CHECK(j["provider_plugin_id"] == "plugin-sample-echo");
        TEST_CHECK(j["data"]["echo"]["msg"] == "cli-offline");

        auto offline_ctx = runCliStdout({pm_exe, "context", "--json"});
        TEST_CHECK(offline_ctx.exit_code == 0);
        auto cj = nlohmann::json::parse(offline_ctx.stdout_text);
        TEST_CHECK(cj["online"] == false);
        TEST_CHECK(cj["offline_fallback"] == true);
        TEST_CHECK(!cj["capabilities"].empty());

        McpChild mcp_offline;
        TEST_CHECK(mcp_offline.start(pm_exe));
        TEST_CHECK(mcp_offline.sendJson({
            {"jsonrpc", "2.0"},
            {"id", 10},
            {"method", "initialize"},
            {"params", {{"protocolVersion", "2024-11-05"}}},
        }));
        nlohmann::json init_off;
        TEST_CHECK(mcp_offline.readJsonLine(init_off));
        TEST_CHECK(init_off["id"] == 10);

        TEST_CHECK(mcp_offline.sendJson({
            {"jsonrpc", "2.0"},
            {"id", 11},
            {"method", "tools/call"},
            {"params", {{"name", "omnidesk_get_active_context"}, {"arguments", nlohmann::json::object()}}},
        }));
        nlohmann::json ctx_off;
        TEST_CHECK(mcp_offline.readJsonLine(ctx_off));
        TEST_CHECK(ctx_off["result"]["isError"] == false);
        auto parsed_ctx_off = nlohmann::json::parse(ctx_off["result"]["content"][0]["text"].get<std::string>());
        TEST_CHECK(parsed_ctx_off["online"] == false);
        TEST_CHECK(parsed_ctx_off["offline_fallback"] == true);

        TEST_CHECK(mcp_offline.sendJson({
            {"jsonrpc", "2.0"},
            {"id", 12},
            {"method", "tools/call"},
            {"params", {
                {"name", "omnidesk_call_capability"},
                {"arguments", {
                    {"capability_id", "sample.echo.v1"},
                    {"payload", {{"from", "mcp-offline"}}},
                }},
            }},
        }));
        nlohmann::json cap_off;
        TEST_CHECK(mcp_offline.readJsonLine(cap_off));
        TEST_CHECK(cap_off["result"]["isError"] == false);
        auto parsed_cap_off = nlohmann::json::parse(cap_off["result"]["content"][0]["text"].get<std::string>());
        TEST_CHECK(parsed_cap_off["ok"] == true);
        TEST_CHECK(parsed_cap_off["offline_fallback"] == true);
        TEST_CHECK(parsed_cap_off["data"]["echo"]["from"] == "mcp-offline");

        TEST_CHECK(mcp_offline.closeAndWait() == 0);
    }

    fs::remove_all(tmp_root, ec);
    std::cout << "ALL CONTROL BRIDGE & MCP SERVER TESTS PASSED\n";
    return 0;
}
