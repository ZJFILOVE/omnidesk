#pragma once

#include <cstddef>
#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include "omnidesk/capabilities.hpp"
#include "omnidesk/entity_bus.hpp"
#include "omnidesk/export.h"
#include "omnidesk/pm/manager.hpp"

namespace omnidesk::pm {

constexpr size_t kMaxControlFrameBytes = 8u * 1024u * 1024u;
constexpr size_t kMaxControlClients = 16;

struct OMNIDESK_API ControlResponse {
    bool transport_ok = false;
    bool server_unreachable = false;
    bool ok = false;
    std::string provider_plugin_id;
    nlohmann::json data = nlohmann::json::object();
    std::string error;
    nlohmann::json raw = nlohmann::json::object();
};

/**
 * Resolves the Unix domain socket path for OmniDesk host inbound control:
 *  1. $OMNIDESK_CONTROL_SOCK (if non-empty)
 *  2. $XDG_RUNTIME_DIR/omnidesk/control.sock (if non-empty)
 *  3. <resolveDataRoot()>/run/control.sock
 */
OMNIDESK_API std::string resolveControlSocketPath();

/**
 * Connects to the OmniDesk ControlServer Unix domain socket, sends a single-line
 * NDJSON request, and reads the single-line NDJSON response within timeout_ms.
 */
OMNIDESK_API ControlResponse sendControlRequest(
    const nlohmann::json& req_json,
    int timeout_ms = 15000,
    const std::string& sock_path = "");

OMNIDESK_API CapabilityResult callControlCapability(
    CapabilityRegistry& capabilities,
    const std::string& capability_id,
    const nlohmann::json& payload,
    const std::string& plugin_id = "",
    int timeout_ms = 30000,
    const std::function<bool()>& interrupted = {});

/**
 * Host inbound control bridge listening on an AF_UNIX SOCK_STREAM socket (0600)
 * using UTF-8 NDJSON frames (max 8 MiB). Dispatches capability calls, EntityBus
 * events, and live desktop focus context snapshots on background handler threads.
 */
class OMNIDESK_API ControlServer {
public:
    ControlServer(CapabilityRegistry& capabilities,
                  EntityBus& bus,
                  PluginManager* plugins = nullptr,
                  PluginInstaller* installer = nullptr);
    ~ControlServer();

    ControlServer(const ControlServer&) = delete;
    ControlServer& operator=(const ControlServer&) = delete;

    bool start(std::string* error = nullptr, const std::string& socket_path = "");
    void stop();

    bool isRunning() const;
    std::string socketPath() const;

    nlohmann::json getContextSnapshot() const;
    void setContextSnapshot(const nlohmann::json& patch);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace omnidesk::pm
