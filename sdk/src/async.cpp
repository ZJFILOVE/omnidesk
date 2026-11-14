#include <omnidesk/async.hpp>

#include <utility>

namespace omnidesk {

void CancelToken::cancel() {
    bool expected = false;
    if (!cancelled_.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
        return;
    }

    std::vector<std::function<void()>> cbs;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        cbs.swap(callbacks_);
    }
    for (auto& cb : cbs) {
        if (cb) {
            try {
                cb();
            } catch (...) {
            }
        }
    }
}

void CancelToken::onCancel(std::function<void()> cb) {
    if (!cb) return;
    if (isCancelled()) {
        try {
            cb();
        } catch (...) {
        }
        return;
    }

    bool invoke_now = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (isCancelled()) {
            invoke_now = true;
        } else {
            callbacks_.push_back(std::move(cb));
        }
    }
    if (invoke_now) {
        try {
            cb();
        } catch (...) {
        }
    }
}

void SseParser::feed(std::string_view chunk, const EventCallback& on_event) {
    for (char c : chunk) {
        if (c == '\r') continue;
        if (c == '\n') {
            std::string line = std::move(line_buffer_);
            line_buffer_.clear();
            processLine(line, on_event);
        } else {
            line_buffer_.push_back(c);
        }
    }
}

void SseParser::flush(const EventCallback& on_event) {
    if (!line_buffer_.empty()) {
        std::string line = std::move(line_buffer_);
        line_buffer_.clear();
        processLine(line, on_event);
    }
    dispatchBuffered(on_event);
}

void SseParser::reset() {
    line_buffer_.clear();
    current_event_.clear();
    data_lines_.clear();
}

void SseParser::processLine(const std::string& line, const EventCallback& on_event) {
    if (line.empty()) {
        dispatchBuffered(on_event);
        return;
    }
    if (line[0] == ':') {
        // SSE comment / keep-alive heartbeat
        return;
    }

    std::string field;
    std::string value;
    auto colon = line.find(':');
    if (colon == std::string::npos) {
        field = line;
    } else {
        field = line.substr(0, colon);
        size_t start = colon + 1;
        if (start < line.size() && line[start] == ' ') {
            ++start;
        }
        value = line.substr(start);
    }

    if (field == "event") {
        current_event_ = value;
    } else if (field == "data") {
        data_lines_.push_back(value);
    }
}

void SseParser::dispatchBuffered(const EventCallback& on_event) {
    if (data_lines_.empty() && current_event_.empty()) return;

    std::string event_name = current_event_.empty() ? "message" : current_event_;
    std::string payload;
    for (size_t i = 0; i < data_lines_.size(); ++i) {
        if (i > 0) payload.push_back('\n');
        payload += data_lines_[i];
    }
    current_event_.clear();
    data_lines_.clear();

    if (on_event) {
        on_event(event_name, payload);
    }
}

}  // namespace omnidesk
