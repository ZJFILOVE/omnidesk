#include "omnidesk/ui_task_runner.hpp"
#include "shutdown_grace.hpp"

#include <algorithm>
#include <borealis/core/thread.hpp>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <mutex>
#include <queue>
#include <thread>
#include <unordered_map>

namespace omnidesk {

struct UiTaskRunner::State {
    struct QueuedTask {
        std::string lane;
        uint64_t generation = 0;
        std::shared_ptr<CancelToken> cancel;
        TaskFn work;
    };

    struct LaneEntry {
        uint64_t generation = 0;
        std::shared_ptr<CancelToken> cancel;
    };

    struct WorkerEntry {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> exited;
    };

    std::mutex mutex;
    std::condition_variable cv;
    std::condition_variable done_cv;
    std::queue<QueuedTask> queue;
    std::unordered_map<std::string, LaneEntry> lanes;
    std::vector<std::shared_ptr<CancelToken>> active_tokens;
    std::vector<WorkerEntry> workers;
    std::vector<std::thread::id> worker_ids;
    size_t active_workers = 0;
    size_t running_tasks = 0;
    uint64_t next_gen = 1;
    bool stopping = false;
    std::shared_ptr<std::atomic<bool>> alive = std::make_shared<std::atomic<bool>>(true);

    std::mutex disp_mutex;
    UiDispatcher ui_dispatcher;

    static void workerLoop(std::shared_ptr<State> st, std::shared_ptr<std::atomic<bool>> exited) {
        struct ExitGuard {
            std::shared_ptr<State> state;
            std::shared_ptr<std::atomic<bool>> flag;
            ~ExitGuard() {
                {
                    std::lock_guard<std::mutex> lk(state->mutex);
                    if (state->active_workers > 0) --state->active_workers;
                    state->done_cv.notify_all();
                }
                flag->store(true, std::memory_order_release);
            }
        } guard{st, exited};

        while (true) {
            QueuedTask task;
            {
                std::unique_lock<std::mutex> lk(st->mutex);
                st->cv.wait(lk, [&st]() { return st->stopping || !st->queue.empty(); });
                if (st->stopping && st->queue.empty()) {
                    return;
                }
                task = std::move(st->queue.front());
                st->queue.pop();
                ++st->running_tasks;
            }

            try {
                if (task.work) {
                    task.work(task.cancel);
                }
            } catch (const std::exception& e) {
                std::fprintf(stderr, "[UiTaskRunner] uncaught task exception: %s\n", e.what());
            } catch (...) {
                std::fprintf(stderr, "[UiTaskRunner] uncaught unknown task exception\n");
            }
            task.work = {};

            {
                std::lock_guard<std::mutex> lk(st->mutex);
                if (st->running_tasks > 0) --st->running_tasks;
                if (!task.lane.empty()) {
                    auto it = st->lanes.find(task.lane);
                    if (it != st->lanes.end() && it->second.generation == task.generation) {
                        st->lanes.erase(it);
                    }
                }
                if (task.cancel) {
                    st->active_tokens.erase(
                        std::remove(st->active_tokens.begin(), st->active_tokens.end(), task.cancel),
                        st->active_tokens.end());
                }
                st->done_cv.notify_all();
            }
        }
    }
};

UiTaskRunner::UiTaskRunner(size_t worker_count) : state_(std::make_shared<State>()) {
    const size_t count = std::max<size_t>(1, worker_count);
    std::lock_guard<std::mutex> lk(state_->mutex);
    state_->active_workers = count;
    state_->workers.reserve(count);
    state_->worker_ids.reserve(count);
    for (size_t i = 0; i < count; ++i) {
        auto exited = std::make_shared<std::atomic<bool>>(false);
        std::thread t([st = state_, exited]() { State::workerLoop(st, exited); });
        state_->worker_ids.push_back(t.get_id());
        state_->workers.push_back(State::WorkerEntry{std::move(t), std::move(exited)});
    }
}

UiTaskRunner::~UiTaskRunner() {
    shutdown();
}

std::shared_ptr<std::atomic<bool>> UiTaskRunner::lifetimeToken() const {
    return state_->alive;
}

void UiTaskRunner::setUiDispatcher(UiDispatcher dispatcher) {
    std::lock_guard<std::mutex> lk(state_->disp_mutex);
    state_->ui_dispatcher = std::move(dispatcher);
}

void UiTaskRunner::dispatchToUi(std::function<void()> fn) const {
    if (!fn) return;
    UiDispatcher disp;
    {
        std::lock_guard<std::mutex> lk(state_->disp_mutex);
        disp = state_->ui_dispatcher;
    }
    if (disp) {
        disp(std::move(fn));
    } else {
        brls::sync(std::move(fn));
    }
}

std::shared_ptr<CancelToken> UiTaskRunner::submit(TaskFn work,
                                                  std::shared_ptr<CancelToken> cancel) {
    return submitInLane("", std::move(work), std::move(cancel));
}

std::shared_ptr<CancelToken> UiTaskRunner::submitInLane(
    const std::string& lane,
    TaskFn work,
    std::shared_ptr<CancelToken> external_cancel) {
    if (!work) return nullptr;

    auto tok = CancelToken::create();
    if (external_cancel) {
        if (external_cancel->isCancelled()) {
            tok->cancel();
            return nullptr;
        }
        external_cancel->onCancel([tok]() { tok->cancel(); });
        tok->onCancel([external_cancel]() { external_cancel->cancel(); });
    }

    std::shared_ptr<CancelToken> prev_lane_tok;
    {
        std::lock_guard<std::mutex> lk(state_->mutex);
        if (state_->stopping) {
            tok->cancel();
            return nullptr;
        }
        const uint64_t gen = state_->next_gen++;
        if (!lane.empty()) {
            auto it = state_->lanes.find(lane);
            if (it != state_->lanes.end()) {
                prev_lane_tok = it->second.cancel;
            }
            state_->lanes[lane] = State::LaneEntry{gen, tok};
        }
        state_->active_tokens.push_back(tok);
        state_->queue.push(State::QueuedTask{lane, gen, tok, std::move(work)});
    }
    if (prev_lane_tok) {
        prev_lane_tok->cancel();
    }
    state_->cv.notify_one();
    return tok;
}

bool UiTaskRunner::cancelLane(const std::string& lane) {
    if (lane.empty()) return false;
    std::shared_ptr<CancelToken> tok;
    {
        std::lock_guard<std::mutex> lk(state_->mutex);
        auto it = state_->lanes.find(lane);
        if (it == state_->lanes.end()) return false;
        tok = it->second.cancel;
        state_->lanes.erase(it);
    }
    if (tok) tok->cancel();
    return true;
}

void UiTaskRunner::cancelAll() {
    std::vector<std::shared_ptr<CancelToken>> tokens;
    {
        std::lock_guard<std::mutex> lk(state_->mutex);
        tokens = state_->active_tokens;
        state_->lanes.clear();
    }
    for (auto& t : tokens) {
        if (t) t->cancel();
    }
}

size_t UiTaskRunner::activeCount() const {
    std::lock_guard<std::mutex> lk(state_->mutex);
    return state_->running_tasks;
}

size_t UiTaskRunner::queuedCount() const {
    std::lock_guard<std::mutex> lk(state_->mutex);
    return state_->queue.size();
}

void UiTaskRunner::shutdown(int timeout_ms) {
    std::vector<std::shared_ptr<CancelToken>> tokens;
    std::vector<State::QueuedTask> dropped;
    std::vector<State::WorkerEntry> workers_to_join;
    {
        std::lock_guard<std::mutex> lk(state_->mutex);
        if (state_->stopping) return;
        state_->stopping = true;
        state_->alive->store(false, std::memory_order_release);
        tokens = std::move(state_->active_tokens);
        state_->lanes.clear();
        while (!state_->queue.empty()) {
            dropped.push_back(std::move(state_->queue.front()));
            state_->queue.pop();
        }
        workers_to_join = std::move(state_->workers);
    }
    dropped.clear();
    for (auto& t : tokens) {
        if (t) t->cancel();
    }
    state_->cv.notify_all();

    const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
    const auto self_id = std::this_thread::get_id();
    {
        std::unique_lock<std::mutex> lk(state_->mutex);
        if (grace_ms > 0) {
            state_->done_cv.wait_for(lk, std::chrono::milliseconds(grace_ms), [this]() {
                return state_->active_workers == 0;
            });
        }
    }
    for (auto& w : workers_to_join) {
        if (!w.thread.joinable()) continue;
        if (w.thread.get_id() == self_id || !w.exited->load(std::memory_order_acquire)) {
            w.thread.detach();
        } else {
            w.thread.join();
        }
    }
}

} // namespace omnidesk
