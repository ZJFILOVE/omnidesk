#include "omnidesk/domain_registry.hpp"
#include "omnidesk/dso_view_lease.hpp"
#include "shutdown_grace.hpp"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <optional>
#include <queue>
#include <thread>
#include <unordered_map>
#include <unordered_set>

namespace omnidesk {

struct DomainRegistry::FlightTracker {
    struct Guard {
        std::shared_ptr<FlightTracker> tracker;
        std::string plugin_id;
        Guard(std::shared_ptr<FlightTracker> t, std::string pid)
            : tracker(std::move(t)), plugin_id(std::move(pid)) {
            if (tracker && !plugin_id.empty()) {
                tracker->trackStart(plugin_id);
            }
        }
        ~Guard() {
            if (tracker && !plugin_id.empty()) {
                tracker->trackEnd(plugin_id);
            }
        }
        Guard(const Guard&) = delete;
        Guard& operator=(const Guard&) = delete;
    };

    void trackStart(const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::lock_guard lock(mutex);
        ++in_flight_by_plugin[plugin_id];
        ++total_in_flight;
    }

    void trackEnd(const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::lock_guard lock(mutex);
        auto it = in_flight_by_plugin.find(plugin_id);
        if (it != in_flight_by_plugin.end() && it->second > 0) {
            --it->second;
            if (it->second == 0) in_flight_by_plugin.erase(it);
        }
        if (total_in_flight > 0) --total_in_flight;
        done_cv.notify_all();
    }

    bool hasInFlight(const std::string& plugin_id) const {
        if (plugin_id.empty()) return false;
        std::lock_guard lock(mutex);
        auto it = in_flight_by_plugin.find(plugin_id);
        return it != in_flight_by_plugin.end() && it->second > 0;
    }

    void waitPlugin(const std::string& plugin_id, int timeout_ms) {
        if (plugin_id.empty()) return;
        std::unique_lock lock(mutex);
        if (stopping) return;
        const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
        if (grace_ms > 0) {
            done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this, &plugin_id]() {
                auto it = in_flight_by_plugin.find(plugin_id);
                return it == in_flight_by_plugin.end() || it->second == 0;
            });
        }
    }

    void waitAll(int timeout_ms) {
        std::unique_lock lock(mutex);
        if (stopping) return;
        stopping = true;
        const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
        if (grace_ms > 0) {
            done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this]() {
                return total_in_flight == 0;
            });
        }
    }

    mutable std::mutex mutex;
    std::condition_variable done_cv;
    std::unordered_map<std::string, size_t> in_flight_by_plugin;
    size_t total_in_flight = 0;
    bool stopping = false;
};

namespace {

class DomainAsyncPool {
public:
    struct SourceBinding {
        const DomainRegistry* owner = nullptr;
        std::string plugin_id;
        std::shared_ptr<DomainRegistry::FaultDispatcher> disp;
        std::weak_ptr<DomainRegistry::FlightTracker> weak_tracker;
        std::weak_ptr<void> weak_owner;
    };

    static DomainAsyncPool& instance() {
        static DomainAsyncPool pool;
        return pool;
    }

    void bindSource(const DomainRegistry* owner,
                    const void* ptr,
                    std::string plugin_id,
                    std::shared_ptr<DomainRegistry::FaultDispatcher> disp,
                    std::weak_ptr<DomainRegistry::FlightTracker> weak_tracker,
                    std::weak_ptr<void> weak_owner) {
        if (!ptr) return;
        std::lock_guard lock(state_->mutex);
        state_->bindings[ptr] = SourceBinding{
            owner,
            std::move(plugin_id),
            std::move(disp),
            std::move(weak_tracker),
            std::move(weak_owner)};
    }

    void unbindPlugin(const DomainRegistry* owner, const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::lock_guard lock(state_->mutex);
        for (auto it = state_->bindings.begin(); it != state_->bindings.end();) {
            if ((owner == nullptr || it->second.owner == owner) &&
                it->second.plugin_id == plugin_id) {
                it = state_->bindings.erase(it);
            } else {
                ++it;
            }
        }
    }

    void unbindOwner(const DomainRegistry* owner) {
        if (!owner) return;
        std::lock_guard lock(state_->mutex);
        for (auto it = state_->bindings.begin(); it != state_->bindings.end();) {
            if (it->second.owner == owner) {
                it = state_->bindings.erase(it);
            } else {
                ++it;
            }
        }
    }

    std::optional<SourceBinding> findBinding(const void* ptr) {
        std::lock_guard lock(state_->mutex);
        auto it = state_->bindings.find(ptr);
        if (it == state_->bindings.end()) return std::nullopt;
        return it->second;
    }

    bool isWorkerThread() const {
        const auto self_id = std::this_thread::get_id();
        std::lock_guard lock(state_->mutex);
        for (const auto& wid : state_->worker_ids) {
            if (wid == self_id) return true;
        }
        return false;
    }

    void submit(const std::string& plugin_id,
                std::shared_ptr<CancelToken> cancel,
                std::function<void()> fn) {
        {
            std::lock_guard lock(state_->mutex);
            if (state_->stopping) {
                if (cancel) cancel->cancel();
                return;
            }
            const uint64_t id = state_->next_id++;
            if (!plugin_id.empty()) {
                state_->active_tokens[id] = {plugin_id, cancel};
                ++state_->in_flight_by_plugin[plugin_id];
            }
            state_->queue.push(TaskRecord{id, plugin_id, std::move(fn)});
        }
        state_->cv.notify_one();
    }

    void cancelPlugin(const std::string& plugin_id) {
        if (plugin_id.empty()) return;
        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        {
            std::lock_guard lock(state_->mutex);
            for (const auto& [_, entry] : state_->active_tokens) {
                if (entry.first == plugin_id && entry.second) {
                    to_cancel.push_back(entry.second);
                }
            }
        }
        for (auto& tok : to_cancel) {
            tok->cancel();
        }
    }

    void cancelAndWaitPlugin(const std::string& plugin_id, int timeout_ms = -1) {
        if (plugin_id.empty()) return;
        cancelPlugin(plugin_id);
        if (isWorkerThread()) return;
        std::unique_lock lock(state_->mutex);
        const int grace_ms = detail::resolveShutdownGraceMs(timeout_ms);
        if (grace_ms > 0) {
            state_->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this, &plugin_id]() {
                auto it = state_->in_flight_by_plugin.find(plugin_id);
                return it == state_->in_flight_by_plugin.end() || it->second == 0;
            });
        }
    }

private:
    struct TaskRecord {
        uint64_t id = 0;
        std::string plugin_id;
        std::function<void()> fn;
    };

    struct State {
        std::mutex mutex;
        std::condition_variable cv;
        std::condition_variable done_cv;
        std::queue<TaskRecord> queue;
        std::vector<std::thread::id> worker_ids;
        std::unordered_map<const void*, SourceBinding> bindings;
        std::unordered_map<uint64_t, std::pair<std::string, std::shared_ptr<CancelToken>>> active_tokens;
        std::unordered_map<std::string, size_t> in_flight_by_plugin;
        size_t active_workers = 0;
        uint64_t next_id = 1;
        bool stopping = false;
    };

    struct WorkerEntry {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> exited;
    };

    static void workerLoop(std::shared_ptr<State> st, std::shared_ptr<std::atomic<bool>> exited) {
        struct ExitGuard {
            std::shared_ptr<State> state_ptr;
            std::shared_ptr<std::atomic<bool>> exit_flag;
            ~ExitGuard() {
                {
                    std::lock_guard lock(state_ptr->mutex);
                    if (state_ptr->active_workers > 0) --state_ptr->active_workers;
                    state_ptr->done_cv.notify_all();
                }
                exit_flag->store(true, std::memory_order_release);
            }
        } exit_guard{st, exited};

        while (true) {
            TaskRecord task;
            {
                std::unique_lock lock(st->mutex);
                st->cv.wait(lock, [&st]() { return st->stopping || !st->queue.empty(); });
                if (st->stopping && st->queue.empty()) return;
                task = std::move(st->queue.front());
                st->queue.pop();
            }
            try {
                if (task.fn) task.fn();
            } catch (...) {
            }
            task.fn = {};
            {
                std::lock_guard lock(st->mutex);
                st->active_tokens.erase(task.id);
                if (!task.plugin_id.empty()) {
                    auto it = st->in_flight_by_plugin.find(task.plugin_id);
                    if (it != st->in_flight_by_plugin.end() && it->second > 0) {
                        --it->second;
                        if (it->second == 0) {
                            st->in_flight_by_plugin.erase(it);
                            st->done_cv.notify_all();
                        }
                    }
                }
            }
        }
    }

    DomainAsyncPool() : state_(std::make_shared<State>()) {
        constexpr size_t kWorkers = 4;
        std::lock_guard lock(state_->mutex);
        state_->active_workers = kWorkers;
        state_->worker_ids.reserve(kWorkers);
        workers_.reserve(kWorkers);
        for (size_t i = 0; i < kWorkers; ++i) {
            auto exited = std::make_shared<std::atomic<bool>>(false);
            std::thread t([st = state_, exited]() { workerLoop(st, exited); });
            state_->worker_ids.push_back(t.get_id());
            workers_.push_back(WorkerEntry{std::move(t), std::move(exited)});
        }
    }

    ~DomainAsyncPool() {
        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        std::vector<TaskRecord> dropped;
        {
            std::lock_guard lock(state_->mutex);
            state_->stopping = true;
            for (const auto& [_, entry] : state_->active_tokens) {
                if (entry.second) to_cancel.push_back(entry.second);
            }
            while (!state_->queue.empty()) {
                dropped.push_back(std::move(state_->queue.front()));
                state_->queue.pop();
            }
        }
        dropped.clear();
        for (auto& tok : to_cancel) tok->cancel();
        state_->cv.notify_all();

        const int grace_ms = std::min(detail::resolveShutdownGraceMs(-1), 250);
        {
            std::unique_lock lock(state_->mutex);
            if (grace_ms > 0) {
                state_->done_cv.wait_for(lock, std::chrono::milliseconds(grace_ms), [this]() {
                    return state_->active_workers == 0;
                });
            }
        }
        const auto self_id = std::this_thread::get_id();
        for (auto& w : workers_) {
            if (!w.thread.joinable()) continue;
            if (w.thread.get_id() == self_id || !w.exited->load(std::memory_order_acquire)) {
                w.thread.detach();
            } else {
                w.thread.join();
            }
        }
    }

    std::shared_ptr<State> state_;
    std::vector<WorkerEntry> workers_;
};

template <typename Fn, typename FallbackFn>
auto guardCall(const std::shared_ptr<DomainRegistry::FaultDispatcher>& disp,
               const std::shared_ptr<DomainRegistry::FlightTracker>& tracker,
               const std::string& plugin_id,
               const char* component,
               Fn&& fn,
               FallbackFn&& fallback) -> decltype(fn()) {
    DomainRegistry::FlightTracker::Guard flight(tracker, plugin_id);
    try {
        return fn();
    } catch (const std::exception& e) {
        if (disp) disp->report(plugin_id, component, std::string("plugin domain source threw exception: ") + e.what());
    } catch (...) {
        if (disp) disp->report(plugin_id, component, "plugin domain source threw unknown exception");
    }
    return fallback();
}

class GuardedMediaSource final : public IMediaSource {
public:
    GuardedMediaSource(std::string plugin_id,
                       std::shared_ptr<IMediaSource> inner,
                       std::shared_ptr<DomainRegistry::FaultDispatcher> disp,
                       std::shared_ptr<DomainRegistry::FlightTracker> tracker)
        : plugin_id_(std::move(plugin_id)),
          inner_(std::move(inner)),
          disp_(std::move(disp)),
          tracker_(std::move(tracker)) {}

    std::string sourceId() const override {
        return guardCall(disp_, tracker_, plugin_id_, "media.sourceId",
                         [&]() { return inner_->sourceId(); },
                         [&]() { return plugin_id_; });
    }
    std::string displayName() const override {
        return guardCall(disp_, tracker_, plugin_id_, "media.displayName",
                         [&]() { return inner_->displayName(); },
                         [&]() { return plugin_id_; });
    }
    std::vector<std::string> categories() const override {
        return guardCall(disp_, tracker_, plugin_id_, "media.categories",
                         [&]() { return inner_->categories(); },
                         []() { return std::vector<std::string>{}; });
    }
    std::vector<MediaItem> fetchItems(const std::string& category, const std::string& query) override {
        return guardCall(disp_, tracker_, plugin_id_, "media.fetchItems",
                         [&]() { return inner_->fetchItems(category, query); },
                         []() { return std::vector<MediaItem>{}; });
    }
    std::vector<MediaItem> fetchItemsPage(const std::string& category,
                                          const std::string& query,
                                          int page,
                                          bool force_refresh) override {
        return guardCall(disp_, tracker_, plugin_id_, "media.fetchItemsPage",
                         [&]() { return inner_->fetchItemsPage(category, query, page, force_refresh); },
                         []() { return std::vector<MediaItem>{}; });
    }
    std::shared_ptr<CancelToken> fetchItemsAsync(
        const std::string& category,
        const std::string& query,
        std::function<void(std::vector<MediaItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel) override {
        return guardCall(
            disp_, tracker_, plugin_id_, "media.fetchItemsAsync",
            [&]() { return inner_->fetchItemsAsync(category, query, std::move(callback), std::move(cancel)); },
            []() { return CancelToken::create(); });
    }
    std::shared_ptr<CancelToken> fetchItemsPageAsync(
        const std::string& category,
        const std::string& query,
        int page,
        bool force_refresh,
        std::function<void(std::vector<MediaItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel) override {
        return guardCall(
            disp_, tracker_, plugin_id_, "media.fetchItemsPageAsync",
            [&]() {
                return inner_->fetchItemsPageAsync(
                    category, query, page, force_refresh, std::move(callback), std::move(cancel));
            },
            []() { return CancelToken::create(); });
    }
    std::vector<LyricLine> fetchLyrics(const std::string& item_id) override {
        return guardCall(disp_, tracker_, plugin_id_, "media.fetchLyrics",
                         [&]() { return inner_->fetchLyrics(item_id); },
                         []() { return std::vector<LyricLine>{}; });
    }
    std::vector<ChapterSegment> fetchChapters(const std::string& item_id) override {
        return guardCall(disp_, tracker_, plugin_id_, "media.fetchChapters",
                         [&]() { return inner_->fetchChapters(item_id); },
                         []() { return std::vector<ChapterSegment>{}; });
    }

private:
    std::string plugin_id_;
    std::shared_ptr<IMediaSource> inner_;
    std::shared_ptr<DomainRegistry::FaultDispatcher> disp_;
    std::shared_ptr<DomainRegistry::FlightTracker> tracker_;
};

class GuardedTextSource final : public ITextSource {
public:
    GuardedTextSource(std::string plugin_id,
                      std::shared_ptr<ITextSource> inner,
                      std::shared_ptr<DomainRegistry::FaultDispatcher> disp,
                      std::shared_ptr<DomainRegistry::FlightTracker> tracker)
        : plugin_id_(std::move(plugin_id)),
          inner_(std::move(inner)),
          disp_(std::move(disp)),
          tracker_(std::move(tracker)) {}

    std::string sourceId() const override {
        return guardCall(disp_, tracker_, plugin_id_, "text.sourceId",
                         [&]() { return inner_->sourceId(); },
                         [&]() { return plugin_id_; });
    }
    std::string displayName() const override {
        return guardCall(disp_, tracker_, plugin_id_, "text.displayName",
                         [&]() { return inner_->displayName(); },
                         [&]() { return plugin_id_; });
    }
    std::string category() const override {
        return guardCall(disp_, tracker_, plugin_id_, "text.category",
                         [&]() { return inner_->category(); },
                         []() { return std::string("blog"); });
    }
    std::vector<TextChannel> channels() const override {
        return guardCall(disp_, tracker_, plugin_id_, "text.channels",
                         [&]() { return inner_->channels(); },
                         []() { return std::vector<TextChannel>{}; });
    }
    std::vector<TextFeedItem> fetchFeed(const std::string& query) override {
        return guardCall(disp_, tracker_, plugin_id_, "text.fetchFeed",
                         [&]() { return inner_->fetchFeed(query); },
                         []() { return std::vector<TextFeedItem>{}; });
    }
    std::vector<TextFeedItem> fetchFeedInChannel(const std::string& channel_id,
                                                 const std::string& query) override {
        return guardCall(disp_, tracker_, plugin_id_, "text.fetchFeedInChannel",
                         [&]() { return inner_->fetchFeedInChannel(channel_id, query); },
                         []() { return std::vector<TextFeedItem>{}; });
    }
    std::vector<TextFeedItem> fetchFeedPage(const std::string& channel_id,
                                            const std::string& query,
                                            int page,
                                            bool force_refresh) override {
        return guardCall(disp_, tracker_, plugin_id_, "text.fetchFeedPage",
                         [&]() { return inner_->fetchFeedPage(channel_id, query, page, force_refresh); },
                         []() { return std::vector<TextFeedItem>{}; });
    }
    std::shared_ptr<CancelToken> fetchFeedAsync(
        const std::string& query,
        std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel) override {
        return guardCall(
            disp_, tracker_, plugin_id_, "text.fetchFeedAsync",
            [&]() { return inner_->fetchFeedAsync(query, std::move(callback), std::move(cancel)); },
            []() { return CancelToken::create(); });
    }
    std::shared_ptr<CancelToken> fetchFeedInChannelAsync(
        const std::string& channel_id,
        const std::string& query,
        std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel) override {
        return guardCall(
            disp_, tracker_, plugin_id_, "text.fetchFeedInChannelAsync",
            [&]() {
                return inner_->fetchFeedInChannelAsync(
                    channel_id, query, std::move(callback), std::move(cancel));
            },
            []() { return CancelToken::create(); });
    }
    std::shared_ptr<CancelToken> fetchFeedPageAsync(
        const std::string& channel_id,
        const std::string& query,
        int page,
        bool force_refresh,
        std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel) override {
        return guardCall(
            disp_, tracker_, plugin_id_, "text.fetchFeedPageAsync",
            [&]() {
                return inner_->fetchFeedPageAsync(
                    channel_id, query, page, force_refresh, std::move(callback), std::move(cancel));
            },
            []() { return CancelToken::create(); });
    }
    TextArticle fetchArticle(const std::string& article_id) override {
        return guardCall(disp_, tracker_, plugin_id_, "text.fetchArticle",
                         [&]() { return inner_->fetchArticle(article_id); },
                         []() { return TextArticle{}; });
    }

private:
    std::string plugin_id_;
    std::shared_ptr<ITextSource> inner_;
    std::shared_ptr<DomainRegistry::FaultDispatcher> disp_;
    std::shared_ptr<DomainRegistry::FlightTracker> tracker_;
};

class GuardedBookSource final : public IBookSource {
public:
    GuardedBookSource(std::string plugin_id,
                      std::shared_ptr<IBookSource> inner,
                      std::shared_ptr<DomainRegistry::FaultDispatcher> disp,
                      std::shared_ptr<DomainRegistry::FlightTracker> tracker)
        : plugin_id_(std::move(plugin_id)),
          inner_(std::move(inner)),
          disp_(std::move(disp)),
          tracker_(std::move(tracker)) {}

    std::string sourceId() const override {
        return guardCall(disp_, tracker_, plugin_id_, "book.sourceId",
                         [&]() { return inner_->sourceId(); },
                         [&]() { return plugin_id_; });
    }
    std::string displayName() const override {
        return guardCall(disp_, tracker_, plugin_id_, "book.displayName",
                         [&]() { return inner_->displayName(); },
                         [&]() { return plugin_id_; });
    }
    std::vector<BookItem> listBooks(const std::string& format_filter, const std::string& query) override {
        return guardCall(disp_, tracker_, plugin_id_, "book.listBooks",
                         [&]() { return inner_->listBooks(format_filter, query); },
                         []() { return std::vector<BookItem>{}; });
    }
    std::shared_ptr<CancelToken> listBooksAsync(
        const std::string& format_filter,
        const std::string& query,
        std::function<void(std::vector<BookItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel) override {
        return guardCall(
            disp_, tracker_, plugin_id_, "book.listBooksAsync",
            [&]() { return inner_->listBooksAsync(format_filter, query, std::move(callback), std::move(cancel)); },
            []() { return CancelToken::create(); });
    }
    BookSpread fetchSpread(const std::string& book_id, int page_number) override {
        return guardCall(disp_, tracker_, plugin_id_, "book.fetchSpread",
                         [&]() { return inner_->fetchSpread(book_id, page_number); },
                         []() { return BookSpread{}; });
    }
    std::vector<VocabCard> listVocab() override {
        return guardCall(disp_, tracker_, plugin_id_, "book.listVocab",
                         [&]() { return inner_->listVocab(); },
                         []() { return std::vector<VocabCard>{}; });
    }

private:
    std::string plugin_id_;
    std::shared_ptr<IBookSource> inner_;
    std::shared_ptr<DomainRegistry::FaultDispatcher> disp_;
    std::shared_ptr<DomainRegistry::FlightTracker> tracker_;
};

ViewFactory guardViewFactory(std::string plugin_id,
                             const char* component,
                             ViewFactory fn,
                             std::shared_ptr<DomainRegistry::FaultDispatcher> disp,
                             std::shared_ptr<DomainRegistry::FlightTracker> tracker) {
    if (!fn) return {};
    return [pid = std::move(plugin_id), component, fn = std::move(fn), disp = std::move(disp),
            tracker = std::move(tracker)]() -> brls::View* {
        brls::View* view = guardCall(
            disp, tracker, pid, component, [&]() { return fn(); }, []() -> brls::View* { return nullptr; });
        return DsoViewLeaseRegistry::instance().leaseView(pid, view);
    };
}

}  // namespace

DomainRegistry::DomainRegistry() : flight_tracker_(std::make_shared<FlightTracker>()) {}

DomainRegistry::~DomainRegistry() {
    DomainAsyncPool::instance().unbindOwner(this);
    shutdown(-1);
}

void DomainRegistry::shutdown(int timeout_ms) {
    std::unordered_set<std::string> pids;
    {
        std::lock_guard lock(mutex_);
        for (const auto& item : media_sources_) pids.insert(item.plugin_id);
        for (const auto& item : text_sources_) pids.insert(item.plugin_id);
        for (const auto& item : book_sources_) pids.insert(item.plugin_id);
        for (const auto& item : workspace_tabs_) pids.insert(item.plugin_id);
        for (const auto& item : dock_widgets_) pids.insert(item.plugin_id);
    }
    DomainAsyncPool::instance().unbindOwner(this);
    if (fault_dispatcher_) {
        std::lock_guard lock(fault_dispatcher_->mutex);
        fault_dispatcher_->handler = {};
    }
    for (const auto& pid : pids) {
        DomainAsyncPool::instance().cancelPlugin(pid);
    }
    if (flight_tracker_) {
        flight_tracker_->waitAll(timeout_ms);
    }
}

bool DomainRegistry::hasInFlight(const std::string& plugin_id) const {
    return flight_tracker_ ? flight_tracker_->hasInFlight(plugin_id) : false;
}

void DomainRegistry::setFaultHandler(FaultHandler handler) {
    std::lock_guard lock(fault_dispatcher_->mutex);
    fault_dispatcher_->handler = std::move(handler);
}

void DomainRegistry::registerMediaSource(const std::string& plugin_id,
                                         std::shared_ptr<IMediaSource> source) {
    if (plugin_id.empty() || !source) return;
    const void* raw_ptr = source.get();
    auto guarded = std::make_shared<GuardedMediaSource>(
        plugin_id, std::move(source), fault_dispatcher_, flight_tracker_);
    DomainAsyncPool::instance().bindSource(
        this, raw_ptr, plugin_id, fault_dispatcher_, flight_tracker_, guarded);
    {
        std::lock_guard lock(mutex_);
        media_sources_.push_back({plugin_id, std::move(guarded)});
    }
    notifyChanged();
}

void DomainRegistry::registerTextSource(const std::string& plugin_id,
                                        std::shared_ptr<ITextSource> source) {
    if (plugin_id.empty() || !source) return;
    const void* raw_ptr = source.get();
    auto guarded = std::make_shared<GuardedTextSource>(
        plugin_id, std::move(source), fault_dispatcher_, flight_tracker_);
    DomainAsyncPool::instance().bindSource(
        this, raw_ptr, plugin_id, fault_dispatcher_, flight_tracker_, guarded);
    {
        std::lock_guard lock(mutex_);
        text_sources_.push_back({plugin_id, std::move(guarded)});
    }
    notifyChanged();
}

void DomainRegistry::registerBookSource(const std::string& plugin_id,
                                        std::shared_ptr<IBookSource> source) {
    if (plugin_id.empty() || !source) return;
    const void* raw_ptr = source.get();
    auto guarded = std::make_shared<GuardedBookSource>(
        plugin_id, std::move(source), fault_dispatcher_, flight_tracker_);
    DomainAsyncPool::instance().bindSource(
        this, raw_ptr, plugin_id, fault_dispatcher_, flight_tracker_, guarded);
    {
        std::lock_guard lock(mutex_);
        book_sources_.push_back({plugin_id, std::move(guarded)});
    }
    notifyChanged();
}

void DomainRegistry::registerWorkspaceTab(WorkspaceTabSpec spec) {
    if (spec.plugin_id.empty() || spec.tab_id.empty() || !spec.create_view) return;
    spec.create_view = guardViewFactory(
        spec.plugin_id, "ui.workspace_tab", std::move(spec.create_view), fault_dispatcher_, flight_tracker_);
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
    spec.create_view = guardViewFactory(
        spec.plugin_id, "ui.dock_widget", std::move(spec.create_view), fault_dispatcher_, flight_tracker_);
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
    DomainAsyncPool::instance().unbindPlugin(this, plugin_id);
    DomainAsyncPool::instance().cancelPlugin(plugin_id);
    if (!DomainAsyncPool::instance().isWorkerThread()) {
        if (flight_tracker_) {
            flight_tracker_->waitPlugin(plugin_id, -1);
        } else {
            DomainAsyncPool::instance().cancelAndWaitPlugin(plugin_id, -1);
        }
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
    for (const auto& cb : snapshot) {
        try {
            cb();
        } catch (...) {
        }
    }
}

namespace {

template <typename ItemT, typename FetchFn>
std::shared_ptr<CancelToken> submitDomainAsyncFetch(
    const void* source_ptr,
    const char* component,
    FetchFn&& fetch_fn,
    std::function<void(std::vector<ItemT> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    if (!cancel) cancel = CancelToken::create();
    if (cancel->isCancelled()) {
        if (callback) {
            try { callback({}, true); } catch (...) {}
        }
        return cancel;
    }
    auto binding = DomainAsyncPool::instance().findBinding(source_ptr);
    std::string plugin_id = binding ? binding->plugin_id : std::string{};
    std::shared_ptr<DomainRegistry::FaultDispatcher> disp = binding ? binding->disp : nullptr;
    std::shared_ptr<DomainRegistry::FlightTracker> tracker = binding ? binding->weak_tracker.lock() : nullptr;
    std::shared_ptr<void> keep_alive = binding ? binding->weak_owner.lock() : nullptr;
    auto flight_guard = std::make_shared<DomainRegistry::FlightTracker::Guard>(tracker, plugin_id);

    DomainAsyncPool::instance().submit(
        plugin_id,
        cancel,
        [fn = std::forward<FetchFn>(fetch_fn), cancel, callback = std::move(callback),
         plugin_id, component, disp, flight_guard, keep_alive]() {
            if (cancel->isCancelled()) {
                if (callback) {
                    try { callback({}, true); } catch (...) {}
                }
                return;
            }
            std::vector<ItemT> items;
            try {
                items = fn();
            } catch (const std::exception& e) {
                items.clear();
                if (disp && !plugin_id.empty()) {
                    disp->report(plugin_id, component,
                                 std::string("plugin domain source threw exception: ") + e.what());
                }
            } catch (...) {
                items.clear();
                if (disp && !plugin_id.empty()) {
                    disp->report(plugin_id, component,
                                 "plugin domain source threw unknown exception");
                }
            }
            const bool is_cancelled = cancel->isCancelled();
            if (callback) {
                try {
                    callback(is_cancelled ? std::vector<ItemT>{} : std::move(items), is_cancelled);
                } catch (...) {
                }
            }
        });
    return cancel;
}

}  // namespace

std::shared_ptr<CancelToken> IMediaSource::fetchItemsAsync(
    const std::string& category,
    const std::string& query,
    std::function<void(std::vector<MediaItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    return submitDomainAsyncFetch<MediaItem>(
        this,
        "media.fetchItems",
        [this, category, query]() { return fetchItems(category, query); },
        std::move(callback),
        std::move(cancel));
}

std::shared_ptr<CancelToken> IMediaSource::fetchItemsPageAsync(
    const std::string& category,
    const std::string& query,
    int page,
    bool force_refresh,
    std::function<void(std::vector<MediaItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    return submitDomainAsyncFetch<MediaItem>(
        this,
        "media.fetchItemsPage",
        [this, category, query, page, force_refresh]() {
            return (page > 1 || force_refresh)
                       ? fetchItemsPage(category, query, page, force_refresh)
                       : fetchItems(category, query);
        },
        std::move(callback),
        std::move(cancel));
}

std::shared_ptr<CancelToken> ITextSource::fetchFeedAsync(
    const std::string& query,
    std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    return submitDomainAsyncFetch<TextFeedItem>(
        this,
        "text.fetchFeed",
        [this, query]() { return fetchFeed(query); },
        std::move(callback),
        std::move(cancel));
}

std::shared_ptr<CancelToken> ITextSource::fetchFeedInChannelAsync(
    const std::string& channel_id,
    const std::string& query,
    std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    return submitDomainAsyncFetch<TextFeedItem>(
        this,
        "text.fetchFeedInChannel",
        [this, channel_id, query]() { return fetchFeedInChannel(channel_id, query); },
        std::move(callback),
        std::move(cancel));
}

std::shared_ptr<CancelToken> ITextSource::fetchFeedPageAsync(
    const std::string& channel_id,
    const std::string& query,
    int page,
    bool force_refresh,
    std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    return submitDomainAsyncFetch<TextFeedItem>(
        this,
        "text.fetchFeedPage",
        [this, channel_id, query, page, force_refresh]() {
            if (page > 1 || force_refresh) {
                return fetchFeedPage(channel_id, query, page, force_refresh);
            }
            return channel_id.empty() ? fetchFeed(query) : fetchFeedInChannel(channel_id, query);
        },
        std::move(callback),
        std::move(cancel));
}

std::shared_ptr<CancelToken> IBookSource::listBooksAsync(
    const std::string& format_filter,
    const std::string& query,
    std::function<void(std::vector<BookItem> items, bool cancelled)> callback,
    std::shared_ptr<CancelToken> cancel) {
    return submitDomainAsyncFetch<BookItem>(
        this,
        "book.listBooks",
        [this, format_filter, query]() { return listBooks(format_filter, query); },
        std::move(callback),
        std::move(cancel));
}

} // namespace omnidesk
