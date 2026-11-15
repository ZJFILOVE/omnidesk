#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "omnidesk/async.hpp"
#include "omnidesk/export.h"

namespace omnidesk {

/**
 * Lightweight fast-lane task runner for interactive UI reads (feed lists,
 * article bodies, comment trees, book spreads, NAS dir browsing, FTS5 search,
 * timeline/subtitle queries). Isolated from heavy long-running JobCenter
 * pipelines (e.g. 10-minute FunASR or BabelDOC jobs) so UI responsiveness never stalls.
 *
 * Supports lane-scoped superseding cancellation (submitting a new request in
 * the same lane automatically cancels the prior in-flight CancelToken).
 */
class OMNIDESK_API UiTaskRunner {
public:
    using TaskFn = std::function<void(const std::shared_ptr<CancelToken>& cancel)>;
    using UiDispatcher = std::function<void(std::function<void()>)>;

    explicit UiTaskRunner(size_t worker_count = 4);
    ~UiTaskRunner();

    UiTaskRunner(const UiTaskRunner&) = delete;
    UiTaskRunner& operator=(const UiTaskRunner&) = delete;

    void setUiDispatcher(UiDispatcher dispatcher);
    void dispatchToUi(std::function<void()> fn) const;

    std::shared_ptr<CancelToken> submit(TaskFn work,
                                        std::shared_ptr<CancelToken> cancel = nullptr);

    std::shared_ptr<CancelToken> submitInLane(const std::string& lane,
                                              TaskFn work,
                                              std::shared_ptr<CancelToken> external_cancel = nullptr);

    bool cancelLane(const std::string& lane);
    void cancelAll();

    size_t activeCount() const;
    size_t queuedCount() const;
    void shutdown(int timeout_ms = -1);

    template <typename FetchFn, typename ApplyFn>
    std::shared_ptr<CancelToken> runForUi(
        const std::string& lane,
        std::shared_ptr<std::atomic<bool>> alive,
        FetchFn fetch,
        ApplyFn apply,
        std::function<void()> on_cancel = {},
        std::shared_ptr<CancelToken> external_cancel = nullptr) {
        auto runner_alive = lifetimeToken();
        auto dispatch_cancel = [this, alive, runner_alive, on_cancel = std::move(on_cancel)](
                                   std::shared_ptr<std::atomic<bool>> handled) mutable {
            if (!handled->exchange(true, std::memory_order_acq_rel)) {
                if (!on_cancel) return;
                dispatchToUi([alive, runner_alive, fn = std::move(on_cancel)]() mutable {
                    if (runner_alive->load(std::memory_order_acquire) &&
                        (!alive || alive->load(std::memory_order_acquire))) {
                        fn();
                    }
                });
            }
        };

        auto handled = std::make_shared<std::atomic<bool>>(false);
        auto tok = submitInLane(
            lane,
            [this, alive, runner_alive, handled,
             fetch = std::move(fetch),
             apply = std::move(apply),
             dispatch_cancel](const std::shared_ptr<CancelToken>& cancel) mutable {
                if ((alive && !alive->load(std::memory_order_acquire)) ||
                    (cancel && cancel->isCancelled())) {
                    dispatch_cancel(handled);
                    return;
                }
                auto result = fetch(cancel);
                if ((alive && !alive->load(std::memory_order_acquire)) ||
                    (cancel && cancel->isCancelled())) {
                    dispatch_cancel(handled);
                    return;
                }
                handled->store(true, std::memory_order_release);
                dispatchToUi([alive, runner_alive, cancel, result = std::move(result),
                              apply = std::move(apply)]() mutable {
                    if (!runner_alive->load(std::memory_order_acquire)) return;
                    if (alive && !alive->load(std::memory_order_acquire)) return;
                    if (cancel && cancel->isCancelled()) return;
                    apply(std::move(result));
                });
            },
            std::move(external_cancel));
        if (!tok) {
            dispatch_cancel(handled);
        }
        return tok;
    }

private:
    std::shared_ptr<std::atomic<bool>> lifetimeToken() const;

    struct State;
    std::shared_ptr<State> state_;
};

} // namespace omnidesk
