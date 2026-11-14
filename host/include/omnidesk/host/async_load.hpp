#pragma once

#include <borealis.hpp>
#include <omnidesk/job_center.hpp>

namespace omnidesk::host {

template <typename Fetch, typename Apply>
void loadInBackground(JobCenter& jobs, const std::string& title,
                      const std::shared_ptr<CancelToken>& cancel, Fetch fetch, Apply apply) {
    jobs.submit({title, "host.reader", "plugin-read", {}},
        [cancel, fetch = std::move(fetch), apply = std::move(apply)](
            JobHandle& job, std::string&) mutable {
            job.cancelToken()->onCancel([cancel]() { cancel->cancel(); });
            if (cancel->isCancelled() || job.isCancelled()) return false;
            auto result = fetch(cancel);
            if (cancel->isCancelled() || job.isCancelled()) return false;
            brls::sync([cancel, result = std::move(result), apply = std::move(apply)]() mutable {
                if (!cancel->isCancelled()) apply(std::move(result));
            });
            return true;
        });
}

}
