#include "omnidesk/dso_view_lease.hpp"
#include "omnidesk/ui_theme.hpp"

#include <algorithm>
#include <borealis/core/application.hpp>
#include <borealis/core/thread.hpp>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace omnidesk {

class LeasedViewBox;

struct DsoViewLeaseRegistry::Impl {
    std::mutex mutex;
    std::condition_variable cv;
    std::unordered_map<std::string, size_t> active_counts;
    std::unordered_set<std::string> revoking_plugins;
    std::unordered_map<std::string, std::vector<std::function<void()>>> release_callbacks;
    std::unordered_map<std::string,
                       std::vector<std::pair<LeasedViewBox*, std::shared_ptr<std::atomic<bool>>>>>
        live_boxes;
    std::atomic<bool> has_ui_thread{false};
    std::thread::id ui_thread_id;

    void increment(const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::lock_guard<std::mutex> lk(mutex);
        ++active_counts[plugin_id];
    }

    void decrement(const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::vector<std::function<void()>> cbs;
        {
            std::lock_guard<std::mutex> lk(mutex);
            auto it = active_counts.find(plugin_id);
            if (it != active_counts.end() && it->second > 0) {
                --it->second;
                if (it->second == 0) {
                    active_counts.erase(it);
                    auto cbit = release_callbacks.find(plugin_id);
                    if (cbit != release_callbacks.end()) {
                        cbs = std::move(cbit->second);
                        release_callbacks.erase(cbit);
                    }
                    cv.notify_all();
                }
            }
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
};

class LeasedViewBox final : public brls::Box {
public:
    LeasedViewBox(std::string plugin_id,
                  std::shared_ptr<DsoViewLeaseRegistry::LeaseToken> token,
                  brls::View* inner,
                  DsoViewLeaseRegistry::Impl* impl)
        : brls::Box(brls::Axis::COLUMN),
          plugin_id_(std::move(plugin_id)),
          token_(std::move(token)),
          inner_(inner),
          impl_(impl) {
        setWidthPercentage(100);
        if (inner_) {
            addView(inner_);
        }
        if (impl_) {
            std::lock_guard<std::mutex> lk(impl_->mutex);
            impl_->live_boxes[plugin_id_].emplace_back(this, alive_);
        }
    }

    ~LeasedViewBox() override {
        alive_->store(false, std::memory_order_release);
        unregisterSelf();
        disposeInner(false);
    }

    void revokeNow() {
        if (!alive_->load(std::memory_order_acquire) || revoked_) {
            return;
        }
        revoked_ = true;
        disposeInner(true);
        auto* placeholder = ui::makeLabel("插件视图已安全卸载", 13.5f, "font/grey");
        placeholder->setMarginTop(4);
        addView(placeholder);
        invalidate();
    }

private:
    void unregisterSelf() {
        if (!impl_) return;
        std::lock_guard<std::mutex> lk(impl_->mutex);
        auto it = impl_->live_boxes.find(plugin_id_);
        if (it == impl_->live_boxes.end()) return;
        auto& vec = it->second;
        vec.erase(std::remove_if(vec.begin(), vec.end(),
                                 [this](const auto& entry) { return entry.first == this; }),
                  vec.end());
        if (vec.empty()) {
            impl_->live_boxes.erase(it);
        }
    }

    void disposeInner(bool from_revoke) {
        brls::View* v = inner_;
        inner_ = nullptr;
        if (!v) {
            if (from_revoke) {
                token_.reset();
            }
            return;
        }
        // Detach without scheduling freeView() so we can synchronously delete if unlocked.
        removeView(v, false);
        if (!v->isPtrLocked()) {
            delete v;
            token_.reset();
        } else {
            v->freeView();
            if (brls::Application::getPlatform()) {
                auto tok = std::move(token_);
                brls::delay(50, [tok = std::move(tok)]() mutable { tok.reset(); });
            }
        }
    }

    std::string plugin_id_;
    std::shared_ptr<DsoViewLeaseRegistry::LeaseToken> token_;
    brls::View* inner_ = nullptr;
    DsoViewLeaseRegistry::Impl* impl_ = nullptr;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    bool revoked_ = false;
};

DsoViewLeaseRegistry::LeaseToken::LeaseToken(std::string plugin_id, Impl* impl)
    : plugin_id_(std::move(plugin_id)), impl_(impl) {
    if (impl_) {
        impl_->increment(plugin_id_);
    }
}

DsoViewLeaseRegistry::LeaseToken::~LeaseToken() {
    if (impl_) {
        impl_->decrement(plugin_id_);
        impl_ = nullptr;
    }
}

DsoViewLeaseRegistry& DsoViewLeaseRegistry::instance() {
    static DsoViewLeaseRegistry reg;
    return reg;
}

DsoViewLeaseRegistry::DsoViewLeaseRegistry() : impl_(std::make_unique<Impl>()) {}

DsoViewLeaseRegistry::~DsoViewLeaseRegistry() = default;

void DsoViewLeaseRegistry::markUiThread() {
    impl_->ui_thread_id = std::this_thread::get_id();
    impl_->has_ui_thread.store(true, std::memory_order_release);
}

bool DsoViewLeaseRegistry::isUiThread() const {
    return impl_->has_ui_thread.load(std::memory_order_acquire) &&
           std::this_thread::get_id() == impl_->ui_thread_id;
}

void DsoViewLeaseRegistry::beginPluginSession(const std::string& plugin_id) {
    if (plugin_id.empty()) return;
    std::lock_guard<std::mutex> lk(impl_->mutex);
    impl_->revoking_plugins.erase(plugin_id);
}

std::shared_ptr<DsoViewLeaseRegistry::LeaseToken> DsoViewLeaseRegistry::acquireLease(
    const std::string& plugin_id) {
    if (plugin_id.empty()) return nullptr;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        if (impl_->revoking_plugins.count(plugin_id) > 0) {
            return nullptr;
        }
    }
    return std::make_shared<LeaseToken>(plugin_id, impl_.get());
}

brls::Box* DsoViewLeaseRegistry::leaseView(const std::string& plugin_id, brls::View* plugin_view) {
    if (!plugin_view) return nullptr;
    auto tok = acquireLease(plugin_id);
    if (!tok) {
        if (!plugin_view->isPtrLocked()) {
            delete plugin_view;
        } else {
            plugin_view->freeView();
        }
        return nullptr;
    }
    return new LeasedViewBox(plugin_id, std::move(tok), plugin_view, impl_.get());
}

void DsoViewLeaseRegistry::revokePlugin(const std::string& plugin_id, int wait_ms) {
    if (plugin_id.empty()) return;
    std::vector<std::pair<LeasedViewBox*, std::shared_ptr<std::atomic<bool>>>> boxes;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        impl_->revoking_plugins.insert(plugin_id);
        auto it = impl_->live_boxes.find(plugin_id);
        if (it != impl_->live_boxes.end()) {
            boxes = it->second;
        }
    }

    if (!boxes.empty()) {
        auto teardown = [boxes = std::move(boxes)]() {
            for (const auto& entry : boxes) {
                if (entry.first && entry.second &&
                    entry.second->load(std::memory_order_acquire)) {
                    entry.first->revokeNow();
                }
            }
        };
        if (isUiThread() || !brls::Application::getPlatform()) {
            teardown();
        } else {
            brls::sync(std::move(teardown));
        }
    }

    if (wait_ms > 0 && !isUiThread()) {
        std::unique_lock<std::mutex> lk(impl_->mutex);
        impl_->cv.wait_for(lk, std::chrono::milliseconds(wait_ms), [&]() {
            auto it = impl_->active_counts.find(plugin_id);
            return it == impl_->active_counts.end() || it->second == 0;
        });
    }
}

bool DsoViewLeaseRegistry::isRevoking(const std::string& plugin_id) const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    return impl_->revoking_plugins.count(plugin_id) > 0;
}

bool DsoViewLeaseRegistry::hasActiveLeases(const std::string& plugin_id) const {
    return activeLeaseCount(plugin_id) > 0;
}

size_t DsoViewLeaseRegistry::activeLeaseCount(const std::string& plugin_id) const {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    auto it = impl_->active_counts.find(plugin_id);
    return it == impl_->active_counts.end() ? 0 : it->second;
}

void DsoViewLeaseRegistry::onAllLeasesReleased(const std::string& plugin_id,
                                               std::function<void()> cb) {
    if (plugin_id.empty() || !cb) return;
    bool already_zero = false;
    {
        std::lock_guard<std::mutex> lk(impl_->mutex);
        auto it = impl_->active_counts.find(plugin_id);
        if (it == impl_->active_counts.end() || it->second == 0) {
            already_zero = true;
        } else {
            impl_->release_callbacks[plugin_id].push_back(std::move(cb));
        }
    }
    if (already_zero) {
        cb();
    }
}

} // namespace omnidesk
