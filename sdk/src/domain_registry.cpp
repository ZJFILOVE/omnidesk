#include "omnidesk/domain_registry.hpp"
#include <algorithm>
#include <condition_variable>
#include <queue>
#include <thread>

namespace omnidesk {

void DomainRegistry::registerMediaSource(const std::string& plugin_id,
                                         std::shared_ptr<IMediaSource> source) {
    if (plugin_id.empty() || !source) return;
    {
        std::lock_guard lock(mutex_);
        media_sources_.push_back({plugin_id, std::move(source)});
    }
    notifyChanged();
}

void DomainRegistry::registerTextSource(const std::string& plugin_id,
                                        std::shared_ptr<ITextSource> source) {
    if (plugin_id.empty() || !source) return;
    {
        std::lock_guard lock(mutex_);
        text_sources_.push_back({plugin_id, std::move(source)});
    }
    notifyChanged();
}

void DomainRegistry::registerBookSource(const std::string& plugin_id,
                                        std::shared_ptr<IBookSource> source) {
    if (plugin_id.empty() || !source) return;
    {
        std::lock_guard lock(mutex_);
        book_sources_.push_back({plugin_id, std::move(source)});
    }
    notifyChanged();
}

void DomainRegistry::registerWorkspaceTab(WorkspaceTabSpec spec) {
    if (spec.plugin_id.empty() || spec.tab_id.empty() || !spec.create_view) return;
    {
        std::lock_guard lock(mutex_);
        workspace_tabs_.erase(std::remove_if(workspace_tabs_.begin(), workspace_tabs_.end(),
            [&](const WorkspaceTabSpec& s) {
                return s.tab_id == spec.tab_id && s.plugin_id == spec.plugin_id;
            }), workspace_tabs_.end());
        workspace_tabs_.push_back(std::move(spec));
    }
    notifyChanged();
}

void DomainRegistry::registerDockWidget(DockWidgetSpec spec) {
    if (spec.plugin_id.empty() || spec.widget_id.empty() || !spec.create_view) return;
    {
        std::lock_guard lock(mutex_);
        dock_widgets_.erase(std::remove_if(dock_widgets_.begin(), dock_widgets_.end(),
            [&](const DockWidgetSpec& s) {
                return s.widget_id == spec.widget_id && s.plugin_id == spec.plugin_id;
            }), dock_widgets_.end());
        dock_widgets_.push_back(std::move(spec));
    }
    notifyChanged();
}

void DomainRegistry::unregisterPlugin(const std::string& plugin_id) {
    bool changed = false;
    {
        std::lock_guard lock(mutex_);
        auto eraseOwned = [&](auto& vec) {
            size_t before = vec.size();
            vec.erase(std::remove_if(vec.begin(), vec.end(),
                [&](const auto& item) { return item.plugin_id == plugin_id; }), vec.end());
            if (vec.size() != before) changed = true;
        };
        eraseOwned(media_sources_);
        eraseOwned(text_sources_);
        eraseOwned(book_sources_);
        eraseOwned(workspace_tabs_);
        eraseOwned(dock_widgets_);
    }
    if (changed) notifyChanged();
}

std::vector<std::shared_ptr<IMediaSource>> DomainRegistry::mediaSources() const {
    std::lock_guard lock(mutex_);
    std::vector<std::shared_ptr<IMediaSource>> out;
    for (const auto& item : media_sources_) out.push_back(item.value);
    return out;
}

std::vector<std::shared_ptr<ITextSource>> DomainRegistry::textSources() const {
    std::lock_guard lock(mutex_);
    std::vector<std::shared_ptr<ITextSource>> out;
    for (const auto& item : text_sources_) out.push_back(item.value);
    return out;
}

std::vector<std::shared_ptr<IBookSource>> DomainRegistry::bookSources() const {
    std::lock_guard lock(mutex_);
    std::vector<std::shared_ptr<IBookSource>> out;
    for (const auto& item : book_sources_) out.push_back(item.value);
    return out;
}

std::vector<WorkspaceTabSpec> DomainRegistry::workspaceTabs() const {
    std::lock_guard lock(mutex_);
    return workspace_tabs_;
}

std::vector<DockWidgetSpec> DomainRegistry::dockWidgets(ContentDomain domain) const {
    std::lock_guard lock(mutex_);
    std::vector<DockWidgetSpec> out;
    for (const auto& item : dock_widgets_) {
        if (item.supported_domains.empty() ||
            std::find(item.supported_domains.begin(), item.supported_domains.end(), domain) !=
                item.supported_domains.end()) {
            out.push_back(item);
        }
    }
    return out;
}

uint64_t DomainRegistry::subscribeChanges(ChangeCallback cb) {
    std::lock_guard lock(listeners_mutex_);
    uint64_t token = next_token_++;
    listeners_[token] = std::move(cb);
    return token;
}

void DomainRegistry::unsubscribeChanges(uint64_t token) {
    std::lock_guard lock(listeners_mutex_);
    listeners_.erase(token);
}

void DomainRegistry::notifyChanged() {
    std::vector<ChangeCallback> snapshot;
    {
        std::lock_guard lock(listeners_mutex_);
        for (const auto& [_, cb] : listeners_) {
            if (cb) snapshot.push_back(cb);
        }
    }
    for (const auto& cb : snapshot) cb();
}

namespace {

class DomainAsyncPool {
public:
    static DomainAsyncPool& instance() {
        static DomainAsyncPool pool;
        return pool;
    }

    void submit(std::function<void()> fn) {
        {
            std::lock_guard lock(mutex_);
            if (stopping_) return;
            queue_.push(std::move(fn));
        }
        cv_.notify_one();
    }

private:
    DomainAsyncPool() {
        for (size_t i = 0; i < 4; ++i) {
            workers_.emplace_back([this]() {
                while (true) {
                    std::function<void()> task;
                    {
                        std::unique_lock lock(mutex_);
                        cv_.wait(lock, [this]() { return stopping_ || !queue_.empty(); });
                        if (stopping_ && queue_.empty()) return;
                        task = std::move(queue_.front());
                        queue_.pop();
                    }
                    try {
                        if (task) task();
                    } catch (...) {
                    }
                }
            });
        }
    }

    ~DomainAsyncPool() {
        {
            std::lock_guard lock(mutex_);
            stopping_ = true;
        }
        cv_.notify_all();
        for (auto& t : workers_) {
            if (t.joinable()) t.join();
        }
    }

    std::mutex mutex_;
    std::condition_variable cv_;
    std::queue<std::function<void()>> queue_;
    std::vector<std::thread> workers_;
    bool stopping_ = false;
};

}  // namespace

std::shared_ptr<CancelToken> IMediaSource::fetchItemsAsync(
    const std::string& category,
    const std::string& query,
    std::function<void(std::vector<MediaItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    if (!cancel) cancel = CancelToken::create();
    if (cancel->isCancelled()) {
        if (callback) callback({}, true);
        return cancel;
    }
    DomainAsyncPool::instance().submit(
        [this, category, query, cancel, callback = std::move(callback)]() {
            if (cancel->isCancelled()) {
                if (callback) callback({}, true);
                return;
            }
            auto items = fetchItems(category, query);
            const bool is_cancelled = cancel->isCancelled();
            if (callback) {
                callback(is_cancelled ? std::vector<MediaItem>{} : std::move(items), is_cancelled);
            }
        });
    return cancel;
}

std::shared_ptr<CancelToken> ITextSource::fetchFeedAsync(
    const std::string& query,
    std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    if (!cancel) cancel = CancelToken::create();
    if (cancel->isCancelled()) {
        if (callback) callback({}, true);
        return cancel;
    }
    DomainAsyncPool::instance().submit(
        [this, query, cancel, callback = std::move(callback)]() {
            if (cancel->isCancelled()) {
                if (callback) callback({}, true);
                return;
            }
            auto items = fetchFeed(query);
            const bool is_cancelled = cancel->isCancelled();
            if (callback) {
                callback(is_cancelled ? std::vector<TextFeedItem>{} : std::move(items), is_cancelled);
            }
        });
    return cancel;
}

std::shared_ptr<CancelToken> IBookSource::listBooksAsync(
    const std::string& format_filter,
    const std::string& query,
    std::function<void(std::vector<BookItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    if (!cancel) cancel = CancelToken::create();
    if (cancel->isCancelled()) {
        if (callback) callback({}, true);
        return cancel;
    }
    DomainAsyncPool::instance().submit(
        [this, format_filter, query, cancel, callback = std::move(callback)]() {
            if (cancel->isCancelled()) {
                if (callback) callback({}, true);
                return;
            }
            auto items = listBooks(format_filter, query);
            const bool is_cancelled = cancel->isCancelled();
            if (callback) {
                callback(is_cancelled ? std::vector<BookItem>{} : std::move(items), is_cancelled);
            }
        });
    return cancel;
}

} // namespace omnidesk
