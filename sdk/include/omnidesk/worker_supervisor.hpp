#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include <omnidesk/async.hpp>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/export.h>

#define OMNIDESK_WORKER_INTERFACE_REVISION 3u

namespace omnidesk {

enum class WorkerTransport {
    StdioPipe = 0,
    UnixSocket = 1,
};

/// Specification for a supervised sidecar process (e.g. Python worker / plugin-worker CLI)
/// speaking line-delimited JSON (NDJSON) over stdin/stdout pipes or an AF_UNIX stream socket.
struct OMNIDESK_API WorkerSpec {
    std::string worker_id;
    std::string plugin_id;
    std::string executable;
    std::vector<std::string> args;
    std::string working_dir;
    std::vector<std::pair<std::string, std::string>> env;
    int64_t idle_timeout_ms = 5 * 60 * 1000;  ///< Auto-stop idle process after N ms (<=0 disables). Default 5 min.
    int64_t stop_grace_ms = 1200;             ///< Wait for SIGTERM before SIGKILL on process group.
    WorkerTransport transport = WorkerTransport::StdioPipe;
    std::string socket_path;                  ///< Optional AF_UNIX socket path (auto-generated when empty).
    bool auto_restart = true;                 ///< Automatically restart crashed workers outside clean idle stops.
    bool retry_on_crash = false;              ///< Retry an idempotent request once if worker crashes mid-call.
    int max_restarts_in_window = 5;           ///< Quarantine worker if crashes exceed threshold inside window.
    int64_t restart_window_ms = 15000;        ///< Sliding window for crash-loop circuit breaker.
    bool duplex = false;                      ///< Enable multiplexed full-duplex NDJSON RPC & host callbacks.
};

struct OMNIDESK_API WorkerStatus {
    std::string worker_id;
    std::string plugin_id;
    bool registered = false;
    bool running = false;
    int pid = -1;
    uint64_t requests_served = 0;
    uint64_t restart_count = 0;
    int64_t last_active_ms = 0;
    int64_t idle_timeout_ms = 0;
    int last_exit_code = 0;
    std::vector<std::string> recent_stderr;
    WorkerTransport transport = WorkerTransport::StdioPipe;
    uint64_t crash_count = 0;
    int last_signal = 0;
    std::string last_signal_name;
    int64_t last_crash_ms = 0;
    bool last_exited_unexpectedly = false;
    bool quarantined = false;
    std::string last_error;
};

struct OMNIDESK_API WorkerFaultEvent {
    std::string worker_id;
    std::string plugin_id;
    int pid = -1;
    int exit_code = 0;
    int signal_number = 0;
    std::string signal_name;
    uint64_t crash_count = 0;
    uint64_t restart_count = 0;
    bool auto_restarted = false;
    bool restart_succeeded = false;
    bool quarantined = false;
    std::string error;
    std::vector<std::string> recent_stderr;
};

/// Manages isolated sidecar & plugin worker processes on behalf of plugins and host services:
/// - Guarantees child death with host (PR_SET_PDEATHSIG + process-group cleanup).
/// - Supports NDJSON over pipe stdin/stdout or AF_UNIX local sockets.
/// - Detects unexpected crashes (SIGSEGV/SIGABRT/nonzero exit), emits fault events,
///   and automatically restarts workers with crash-loop quarantine protection.
class OMNIDESK_API WorkerSupervisor {
public:
    using FaultCallback = std::function<void(const WorkerFaultEvent& event)>;
    using InboundRequestHandler = std::function<CapabilityResult(
        const std::string& worker_id,
        const std::string& plugin_id,
        const nlohmann::json& request,
        const std::shared_ptr<CancelToken>& cancel,
        const StreamCallback& on_chunk)>;
    using InboundEventHandler = std::function<void(
        const std::string& worker_id,
        const std::string& plugin_id,
        const nlohmann::json& event)>;
    using WorkerExitHandler = std::function<void(
        const std::string& worker_id,
        const std::string& plugin_id)>;

    WorkerSupervisor();
    ~WorkerSupervisor();

    WorkerSupervisor(const WorkerSupervisor&) = delete;
    WorkerSupervisor& operator=(const WorkerSupervisor&) = delete;

    void setFaultCallback(FaultCallback cb);
    void setWorkerCallbacks(const std::string& worker_id,
                            InboundRequestHandler on_request,
                            InboundEventHandler on_event = nullptr,
                            WorkerExitHandler on_exit = nullptr);
    bool sendEvent(const std::string& worker_id, const nlohmann::json& event);

    bool registerWorker(const WorkerSpec& spec);
    bool unregisterWorker(const std::string& worker_id);

    bool ensureRunning(const std::string& worker_id, std::string* error = nullptr);
    bool stopWorker(const std::string& worker_id);
    void stopByPlugin(const std::string& plugin_id);
    void stopAll();

    /// Evicts idle workers and detects/restarts any asynchronously crashed workers.
    size_t evictIdleNow();

    /// Sends a JSON request line and waits for the response JSON line.
    CapabilityResult request(const std::string& worker_id,
                             const nlohmann::json& payload,
                             const std::shared_ptr<CancelToken>& cancel = nullptr,
                             int64_t timeout_ms = 60000);

    /// Sends a JSON request line and streams NDJSON chunk lines until completion/cancellation.
    CapabilityResult streamRequest(const std::string& worker_id,
                                   const nlohmann::json& payload,
                                   const StreamCallback& on_chunk,
                                   const std::shared_ptr<CancelToken>& cancel = nullptr,
                                   int64_t timeout_ms = 300000);

    std::optional<WorkerStatus> status(const std::string& worker_id) const;
    std::vector<WorkerStatus> listWorkers() const;

    /// Returns a monotonic non-zero token per registerWorker() call (0 if unregistered).
    /// Remains stable across idle stops and crash restarts, changing only when re-registered/unloaded.
    uint64_t registrationGeneration(const std::string& worker_id) const;

private:
    struct PendingOutbound;
    struct InboundExecutor;

    struct WorkerRuntime {
        WorkerSpec spec;
        uint64_t registration_id = 0;
        std::mutex state_mutex;
        std::timed_mutex io_mutex;
        std::timed_mutex write_mutex;
        int pid = -1;
        int stdin_fd = -1;
        int stdout_fd = -1;
        int stderr_fd = -1;
        bool shared_io_fd = false;
        std::string active_socket_path;
        std::thread stderr_thread;
        std::thread reader_thread;
        std::string stdout_buf;
        std::deque<std::string> stderr_ring;
        std::deque<int64_t> recent_crash_times_ms;
        uint64_t requests_served = 0;
        uint64_t restart_count = 0;
        uint64_t crash_count = 0;
        uint64_t io_generation = 0;
        uint64_t handled_crash_for_gen = 0;
        std::atomic<uint64_t> next_req_seq{1};
        int64_t last_active_ms = 0;
        int64_t last_crash_ms = 0;
        int last_exit_code = 0;
        int last_signal = 0;
        std::string last_signal_name;
        std::string last_error;
        bool last_exited_unexpectedly = false;
        bool quarantined = false;
        bool pending_auto_restart = false;
        bool pending_exit_notify = false;
        int in_flight = 0;
        bool stopping_child = false;
        bool retired = false;
        uint64_t stop_generation = 0;
        bool reader_active = false;
        std::condition_variable lifecycle_cv;
        std::mutex outbound_mutex;
        std::unordered_map<std::string, std::shared_ptr<PendingOutbound>> pending_outbound;
        std::mutex inbound_mutex;
        std::unordered_map<std::string, std::shared_ptr<CancelToken>> active_inbound;
        InboundRequestHandler inbound_req_handler;
        InboundEventHandler inbound_evt_handler;
        WorkerExitHandler exit_handler;
    };

    std::shared_ptr<WorkerRuntime> findRuntime(const std::string& worker_id) const;
    void notifyFault(const WorkerFaultEvent& evt) const;
    void closeIoFdsLocked(WorkerRuntime& rt);
    void recordFailureLocked(WorkerRuntime& rt, int pid, const std::string& error,
                             WorkerFaultEvent& event);
    bool spawnLocked(const std::shared_ptr<WorkerRuntime>& rt_ptr,
                     std::unique_lock<std::mutex>& lk,
                     std::string* error,
                     WorkerFaultEvent* crash_out = nullptr);
    void stopRuntime(WorkerRuntime& rt);
    bool reapExitedLocked(WorkerRuntime& rt,
                          bool* unexpected_crash = nullptr,
                          WorkerFaultEvent* crash_event = nullptr,
                          bool force_unexpected = false);
    bool readLineInterruptible(WorkerRuntime& rt,
                               std::string& out_line,
                               const std::shared_ptr<CancelToken>& cancel,
                               int64_t deadline_ms,
                               bool& timed_out,
                               bool& cancelled,
                               bool& eof);
    bool writeLineInterruptible(WorkerRuntime& rt,
                                const std::string& line,
                                const std::shared_ptr<CancelToken>& cancel,
                                int64_t deadline_ms,
                                bool& timed_out,
                                bool& cancelled);
    bool writeLineDuplex(WorkerRuntime& rt,
                         const std::string& line,
                         const std::shared_ptr<CancelToken>& cancel,
                         int64_t deadline_ms,
                         bool& timed_out,
                         bool& cancelled);
    void readerLoopDuplex(std::shared_ptr<WorkerRuntime> rt, uint64_t gen);
    CapabilityResult streamRequestDuplex(const std::shared_ptr<WorkerRuntime>& rt,
                                         const nlohmann::json& payload,
                                         const StreamCallback& on_chunk,
                                         const std::shared_ptr<CancelToken>& cancel,
                                         int64_t timeout_ms);
    void watchdogLoop();

    mutable std::mutex mutex_;
    std::unordered_map<std::string, std::shared_ptr<WorkerRuntime>> workers_;
    mutable std::mutex fault_cb_mutex_;
    FaultCallback fault_cb_;
    std::unique_ptr<InboundExecutor> inbound_executor_;
    std::condition_variable watchdog_cv_;
    std::thread watchdog_thread_;
    uint64_t next_registration_id_ = 1;
    bool shutting_down_ = false;
};

}  // namespace omnidesk
