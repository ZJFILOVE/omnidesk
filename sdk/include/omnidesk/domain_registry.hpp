#pragma once

#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/export.h"
#include "omnidesk/types.hpp"

namespace brls {
class View;
}

namespace omnidesk {

/**
 * Domain 1 (Media & Music) data provider interface implemented by plugins
 * such as Bilibili, YouTube, fnOS NAS, or Music Lyrics.
 */
class OMNIDESK_API IMediaSource {
public:
    virtual ~IMediaSource() = default;
    virtual std::string sourceId() const = 0;
    virtual std::string displayName() const = 0;
    virtual std::vector<std::string> categories() const = 0;
    virtual std::vector<MediaItem> fetchItems(const std::string& category,
                                              const std::string& query = "") = 0;
    virtual std::vector<MediaItem> fetchItemsPage(const std::string& category,
                                                  const std::string& query,
                                                  int page,
                                                  bool force_refresh = false) {
        (void)force_refresh;
        return page <= 1 ? fetchItems(category, query) : std::vector<MediaItem>{};
    }
    virtual std::shared_ptr<CancelToken> fetchItemsAsync(
        const std::string& category,
        const std::string& query,
        std::function<void(std::vector<MediaItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel = nullptr);
    virtual std::shared_ptr<CancelToken> fetchItemsPageAsync(
        const std::string& category,
        const std::string& query,
        int page,
        bool force_refresh,
        std::function<void(std::vector<MediaItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel = nullptr);
    virtual std::vector<LyricLine> fetchLyrics(const std::string& item_id) {
        (void)item_id;
        return {};
    }
    virtual std::vector<ChapterSegment> fetchChapters(const std::string& item_id) {
        (void)item_id;
        return {};
    }
};

/**
 * Domain 2 (Text, Blogs, Forums & Scientific Journals) provider interface
 * implemented by Zhihu, Linux.do, Reddit, Blogs, arXiv/Nature, Chrome Web plugins.
 */
class OMNIDESK_API ITextSource {
public:
    virtual ~ITextSource() = default;
    virtual std::string sourceId() const = 0;
    virtual std::string displayName() const = 0;
    virtual std::string category() const = 0; ///< "blog" | "zhihu" | "linuxdo" | "shuiyuan" | "v2ex" | "reddit" | "x" | "journal"
    virtual std::vector<TextChannel> channels() const { return {}; }
    virtual std::vector<TextFeedItem> fetchFeed(const std::string& query = "") = 0;
    virtual std::vector<TextFeedItem> fetchFeedInChannel(const std::string& channel_id,
                                                         const std::string& query = "") {
        (void)channel_id;
        return fetchFeed(query);
    }
    virtual std::vector<TextFeedItem> fetchFeedPage(const std::string& channel_id,
                                                    const std::string& query,
                                                    int page,
                                                    bool force_refresh = false) {
        (void)force_refresh;
        if (page > 1) return {};
        return channel_id.empty() ? fetchFeed(query) : fetchFeedInChannel(channel_id, query);
    }
    virtual std::shared_ptr<CancelToken> fetchFeedAsync(
        const std::string& query,
        std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel = nullptr);
    virtual std::shared_ptr<CancelToken> fetchFeedInChannelAsync(
        const std::string& channel_id,
        const std::string& query,
        std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel = nullptr);
    virtual std::shared_ptr<CancelToken> fetchFeedPageAsync(
        const std::string& channel_id,
        const std::string& query,
        int page,
        bool force_refresh,
        std::function<void(std::vector<TextFeedItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel = nullptr);
    virtual TextArticle fetchArticle(const std::string& article_id) = 0;
};

/**
 * Domain 3 (Books, BabelDOC PDF & EPUB Companion) provider interface.
 */
class OMNIDESK_API IBookSource {
public:
    virtual ~IBookSource() = default;
    virtual std::string sourceId() const = 0;
    virtual std::string displayName() const = 0;
    virtual std::vector<BookItem> listBooks(const std::string& format_filter = "",
                                            const std::string& query = "") = 0;
    virtual std::shared_ptr<CancelToken> listBooksAsync(
        const std::string& format_filter,
        const std::string& query,
        std::function<void(std::vector<BookItem> items, bool cancelled)> callback,
        std::shared_ptr<CancelToken> cancel = nullptr);
    virtual BookSpread fetchSpread(const std::string& book_id, int page_number) = 0;
    virtual std::vector<VocabCard> listVocab() { return {}; }
};

using ViewFactory = std::function<brls::View*()>;

struct WorkspaceTabSpec {
    std::string tab_id;
    std::string plugin_id;
    std::string title;
    ContentDomain domain = ContentDomain::Core;
    std::string badge;
    ViewFactory create_view;
};

struct DockWidgetSpec {
    std::string widget_id;
    std::string plugin_id;
    std::string title;
    std::vector<ContentDomain> supported_domains;
    ViewFactory create_view;
};

#define OMNIDESK_DOMAIN_INTERFACE_REVISION 3u

/**
 * Registry connecting decoupled .so plugins to the Host Workspace Shell,
 * the 3 Domain Workbenches (Media, Text, Book), and the F8 Companion Dock.
 * Wraps domain sources and view factories in exception-safe proxies so an
 * unhandled exception in any plugin does not terminate the host GUI.
 */
class OMNIDESK_API DomainRegistry {
public:
    struct FlightTracker;
    using ChangeCallback = std::function<void()>;
    using FaultHandler = std::function<void(const std::string& plugin_id,
                                            const std::string& component,
                                            const std::string& error)>;

    struct FaultDispatcher {
        std::mutex mutex;
        FaultHandler handler;

        void report(const std::string& plugin_id,
                    const std::string& component,
                    const std::string& error) const {
            FaultHandler cb;
            {
                std::lock_guard lock(const_cast<std::mutex&>(mutex));
                cb = handler;
            }
            if (cb) {
                try {
                    cb(plugin_id, component, error);
                } catch (...) {
                }
            }
        }
    };

    DomainRegistry();
    ~DomainRegistry();

    void shutdown(int timeout_ms = -1);
    bool hasInFlight(const std::string& plugin_id) const;

    void setFaultHandler(FaultHandler handler);

    void registerMediaSource(const std::string& plugin_id, std::shared_ptr<IMediaSource> source);
    void registerTextSource(const std::string& plugin_id, std::shared_ptr<ITextSource> source);
    void registerBookSource(const std::string& plugin_id, std::shared_ptr<IBookSource> source);
    void registerWorkspaceTab(WorkspaceTabSpec spec);
    void registerDockWidget(DockWidgetSpec spec);

    void unregisterPlugin(const std::string& plugin_id);

    std::vector<std::shared_ptr<IMediaSource>> mediaSources() const;
    std::vector<std::shared_ptr<ITextSource>> textSources() const;
    std::vector<std::shared_ptr<IBookSource>> bookSources() const;
    std::vector<WorkspaceTabSpec> workspaceTabs() const;
    std::vector<DockWidgetSpec> dockWidgets(ContentDomain domain) const;

    uint64_t subscribeChanges(ChangeCallback cb);
    void unsubscribeChanges(uint64_t token);

private:
    template <typename T>
    struct Owned {
        std::string plugin_id;
        T value;
    };

    void notifyChanged();

    mutable std::mutex mutex_;
    std::vector<Owned<std::shared_ptr<IMediaSource>>> media_sources_;
    std::vector<Owned<std::shared_ptr<ITextSource>>> text_sources_;
    std::vector<Owned<std::shared_ptr<IBookSource>>> book_sources_;
    std::vector<WorkspaceTabSpec> workspace_tabs_;
    std::vector<DockWidgetSpec> dock_widgets_;

    std::shared_ptr<FaultDispatcher> fault_dispatcher_ = std::make_shared<FaultDispatcher>();
    std::shared_ptr<FlightTracker> flight_tracker_;

    std::mutex listeners_mutex_;
    std::unordered_map<uint64_t, ChangeCallback> listeners_;
    uint64_t next_token_ = 1;
};

} // namespace omnidesk
