#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include <omnidesk/export.h>

namespace omnidesk {

/// Cooperative cancellation token shared across callers, async capabilities,
/// background jobs, media fetches, image downloads, and worker requests.
class OMNIDESK_API CancelToken {
public:
    CancelToken() = default;

    static std::shared_ptr<CancelToken> create() {
        return std::make_shared<CancelToken>();
    }

    void cancel();
    bool isCancelled() const noexcept {
        return cancelled_.load(std::memory_order_acquire);
    }

    /// Registers a callback invoked immediately if already cancelled, or when cancel() is first called.
    void onCancel(std::function<void()> cb);

private:
    std::atomic<bool> cancelled_{false};
    mutable std::mutex mutex_;
    std::vector<std::function<void()>> callbacks_;
};

/// Single chunk emitted by a streaming capability or SSE/worker response.
struct OMNIDESK_API StreamChunk {
    std::string delta;
    nlohmann::json data = nlohmann::json::object();
    std::string event = "delta";
    bool done = false;
};

using StreamCallback = std::function<void(const StreamChunk& chunk)>;

/// Incremental Server-Sent Events (SSE) parser for OpenAI / Gemini / LLM HTTP streams.
class OMNIDESK_API SseParser {
public:
    using EventCallback = std::function<void(const std::string& event_name, const std::string& data)>;

    /// Feeds raw bytes from an HTTP chunk callback and dispatches complete SSE events.
    void feed(std::string_view chunk, const EventCallback& on_event);

    /// Flushes any trailing buffered event at end-of-stream.
    void flush(const EventCallback& on_event);

    void reset();

private:
    void processLine(const std::string& line, const EventCallback& on_event);
    void dispatchBuffered(const EventCallback& on_event);

    std::string line_buffer_;
    std::string current_event_;
    std::vector<std::string> data_lines_;
};

}  // namespace omnidesk
