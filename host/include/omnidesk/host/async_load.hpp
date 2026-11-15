#pragma once

#include <atomic>
#include <borealis.hpp>
#include <functional>
#include <memory>
#include <omnidesk/job_center.hpp>
#include <omnidesk/ui_task_runner.hpp>

namespace omnidesk::host {

namespace detail {

struct CancelCleanup {
    std::function<void()> on_cancel;
    std::atomic<bool> done{false};

    explicit CancelCleanup(std::function<void()> cb) : on_cancel(std::move(cb)) {}

    void markDone() noexcept {
        done.store(true, std::memory_order_release);
    }

    void trigger() {
        if (done.exchange(true, std::memory_order_acq_rel)) {
            return;
        }
        if (on_cancel) {
            brls::sync(std::move(on_cancel));
        }
    }

    ~CancelCleanup() {
        trigger();
    }
};

} // namespace detail

template <typename Fetch, typename Apply>
void loadInBackground(JobCenter& jobs, const std::string& title,
                      const std::shared_ptr<CancelToken>& cancel, Fetch fetch, Apply apply,
                      std::function<void()> on_cancel = {}) {
    auto cleanup = std::make_shared<detail::CancelCleanup>(std::move(on_cancel));
    if (cancel && cancel->isCancelled()) {
        cleanup->trigger();
        return;
    }
    const std::string job_id = jobs.submit({title, "host.reader", "plugin-read", {}},
        [cancel, fetch = std::move(fetch), apply = std::move(apply), cleanup](
            JobHandle& job, std::string&) mutable {
            if (cancel) {
                job.cancelToken()->onCancel([cancel]() { cancel->cancel(); });
            }
            if ((cancel && cancel->isCancelled()) || job.isCancelled()) {
                cleanup->trigger();
                return false;
            }
            auto result = fetch(cancel);
            if ((cancel && cancel->isCancelled()) || job.isCancelled()) {
                cleanup->trigger();
                return false;
            }
            brls::sync([cancel, result = std::move(result), apply = std::move(apply), cleanup]() mutable {
                if (cancel && cancel->isCancelled()) {
                    cleanup->markDone();
                    if (cleanup->on_cancel) {
                        cleanup->on_cancel();
                    }
                    return;
                }
                cleanup->markDone();
                apply(std::move(result));
            });
            return true;
        });
    if (job_id.empty()) {
        cleanup->trigger();
    }
}

template <typename Fetch, typename Apply>
void loadUiInBackground(UiTaskRunner& runner,
                        const std::string& lane,
                        const std::shared_ptr<CancelToken>& cancel,
                        Fetch fetch,
                        Apply apply,
                        std::function<void()> on_cancel = {}) {
    auto cleanup = std::make_shared<detail::CancelCleanup>(std::move(on_cancel));
    if (cancel && cancel->isCancelled()) {
        cleanup->trigger();
        return;
    }
    auto tok = runner.submitInLane(
        lane,
        [fetch = std::move(fetch), apply = std::move(apply), cleanup](
            const std::shared_ptr<CancelToken>& lane_cancel) mutable {
            if (lane_cancel && lane_cancel->isCancelled()) {
                cleanup->trigger();
                return;
            }
            auto result = fetch(lane_cancel);
            if (lane_cancel && lane_cancel->isCancelled()) {
                cleanup->trigger();
                return;
            }
            brls::sync([lane_cancel, result = std::move(result), apply = std::move(apply), cleanup]() mutable {
                if (lane_cancel && lane_cancel->isCancelled()) {
                    cleanup->markDone();
                    if (cleanup->on_cancel) {
                        cleanup->on_cancel();
                    }
                    return;
                }
                cleanup->markDone();
                apply(std::move(result));
            });
        },
        cancel);
    if (!tok) {
        cleanup->trigger();
    }
}

}
