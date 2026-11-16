#include <omnidesk/worker_domain_bridge.hpp>

#include <atomic>
#include <mutex>
#include <unordered_set>
#include <omnidesk/types_json.hpp>

namespace omnidesk {

WorkerMediaSource::WorkerMediaSource(WorkerSupervisor& workers,
                                     std::string worker_id,
                                     std::string plugin_id,
                                     std::string source_id,
                                     std::string display_name,
                                     std::vector<std::string> categories)
    : workers_(workers),
      worker_id_(std::move(worker_id)),
      plugin_id_(std::move(plugin_id)),
      source_id_(std::move(source_id)),
      display_name_(std::move(display_name)),
      cached_categories_(std::move(categories)),
      categories_cached_(!cached_categories_.empty()),
      cached_registration_gen_(
          categories_cached_ ? workers_.registrationGeneration(worker_id_) : 0) {}

void WorkerMediaSource::seedCategories(std::vector<std::string> categories) {
    std::lock_guard<std::mutex> lk(metadata_mutex_);
    ++metadata_epoch_;
    cached_categories_ = std::move(categories);
    categories_cached_ = true;
    cached_registration_gen_ = workers_.registrationGeneration(worker_id_);
}

void WorkerMediaSource::invalidateMetadataCache() {
    std::lock_guard<std::mutex> lk(metadata_mutex_);
    ++metadata_epoch_;
    cached_categories_.clear();
    categories_cached_ = false;
    cached_registration_gen_ = 0;
}

std::vector<std::string> WorkerMediaSource::categories() const {
    const uint64_t cur_gen = workers_.registrationGeneration(worker_id_);
    uint64_t fetch_epoch = 0;
    {
        std::lock_guard<std::mutex> lk(metadata_mutex_);
        if (categories_cached_) {
            if (cached_registration_gen_ == 0) {
                if (cur_gen != 0) cached_registration_gen_ = cur_gen;
                return cached_categories_;
            }
            if (cur_gen == cached_registration_gen_) {
                return cached_categories_;
            }
            cached_categories_.clear();
            categories_cached_ = false;
            cached_registration_gen_ = 0;
            ++metadata_epoch_;
        }
        fetch_epoch = metadata_epoch_;
    }
    CapabilityResult res = workers_.request(
        worker_id_,
        {{"op", "media.categories"}, {"source_id", source_id_}},
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("categories") || !res.data["categories"].is_array()) {
        return {};
    }
    std::vector<std::string> out;
    out.reserve(res.data["categories"].size());
    for (const auto& c : res.data["categories"]) {
        if (c.is_string()) out.push_back(c.get<std::string>());
    }
    const uint64_t post_gen = workers_.registrationGeneration(worker_id_);
    if (post_gen != 0 && (cur_gen == 0 || post_gen == cur_gen)) {
        std::lock_guard<std::mutex> lk(metadata_mutex_);
        if (metadata_epoch_ == fetch_epoch) {
            cached_categories_ = out;
            categories_cached_ = true;
            cached_registration_gen_ = post_gen;
        }
    }
    return out;
}

std::vector<MediaItem> WorkerMediaSource::fetchItems(const std::string& category,
                                                     const std::string& query) {
    return fetchItemsPage(category, query, 1, false);
}

std::vector<MediaItem> WorkerMediaSource::fetchItemsPage(const std::string& category,
                                                         const std::string& query,
                                                         int page,
                                                         bool force_refresh) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "media.fetch_items"},
            {"source_id", source_id_},
            {"category", category},
            {"query", query},
            {"page", page},
            {"force_refresh", force_refresh},
        },
        nullptr,
        30000);
    if (!res.ok || !res.data.contains("items")) {
        return {};
    }
    return fromJsonArray<MediaItem>(res.data["items"]);
}

std::vector<LyricLine> WorkerMediaSource::fetchLyrics(const std::string& item_id) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "media.fetch_lyrics"},
            {"source_id", source_id_},
            {"item_id", item_id},
        },
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("lyrics")) {
        return {};
    }
    return fromJsonArray<LyricLine>(res.data["lyrics"]);
}

std::vector<ChapterSegment> WorkerMediaSource::fetchChapters(const std::string& item_id) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "media.fetch_chapters"},
            {"source_id", source_id_},
            {"item_id", item_id},
        },
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("chapters")) {
        return {};
    }
    return fromJsonArray<ChapterSegment>(res.data["chapters"]);
}

WorkerTextSource::WorkerTextSource(WorkerSupervisor& workers,
                                   std::string worker_id,
                                   std::string plugin_id,
                                   std::string source_id,
                                   std::string display_name,
                                   std::string category,
                                   std::vector<TextChannel> channels)
    : workers_(workers),
      worker_id_(std::move(worker_id)),
      plugin_id_(std::move(plugin_id)),
      source_id_(std::move(source_id)),
      display_name_(std::move(display_name)),
      category_(std::move(category)),
      cached_channels_(std::move(channels)),
      channels_cached_(!cached_channels_.empty()),
      cached_registration_gen_(
          channels_cached_ ? workers_.registrationGeneration(worker_id_) : 0) {}

void WorkerTextSource::seedChannels(std::vector<TextChannel> channels) {
    std::lock_guard<std::mutex> lk(metadata_mutex_);
    ++metadata_epoch_;
    cached_channels_ = std::move(channels);
    channels_cached_ = true;
    cached_registration_gen_ = workers_.registrationGeneration(worker_id_);
}

void WorkerTextSource::invalidateMetadataCache() {
    std::lock_guard<std::mutex> lk(metadata_mutex_);
    ++metadata_epoch_;
    cached_channels_.clear();
    channels_cached_ = false;
    cached_registration_gen_ = 0;
}

std::vector<TextChannel> WorkerTextSource::channels() const {
    const uint64_t cur_gen = workers_.registrationGeneration(worker_id_);
    uint64_t fetch_epoch = 0;
    {
        std::lock_guard<std::mutex> lk(metadata_mutex_);
        if (channels_cached_) {
            if (cached_registration_gen_ == 0) {
                if (cur_gen != 0) cached_registration_gen_ = cur_gen;
                return cached_channels_;
            }
            if (cur_gen == cached_registration_gen_) {
                return cached_channels_;
            }
            cached_channels_.clear();
            channels_cached_ = false;
            cached_registration_gen_ = 0;
            ++metadata_epoch_;
        }
        fetch_epoch = metadata_epoch_;
    }
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "text.channels"},
            {"source_id", source_id_},
        },
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("channels") || !res.data["channels"].is_array()) {
        return {};
    }
    auto out = fromJsonArray<TextChannel>(res.data["channels"]);
    const uint64_t post_gen = workers_.registrationGeneration(worker_id_);
    if (post_gen != 0 && (cur_gen == 0 || post_gen == cur_gen)) {
        std::lock_guard<std::mutex> lk(metadata_mutex_);
        if (metadata_epoch_ == fetch_epoch) {
            cached_channels_ = out;
            channels_cached_ = true;
            cached_registration_gen_ = post_gen;
        }
    }
    return out;
}

std::vector<TextFeedItem> WorkerTextSource::fetchFeed(const std::string& query) {
    return fetchFeedPage("", query, 1, false);
}

std::vector<TextFeedItem> WorkerTextSource::fetchFeedInChannel(const std::string& channel_id,
                                                               const std::string& query) {
    return fetchFeedPage(channel_id, query, 1, false);
}

std::vector<TextFeedItem> WorkerTextSource::fetchFeedPage(const std::string& channel_id,
                                                          const std::string& query,
                                                          int page,
                                                          bool force_refresh) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "text.fetch_feed"},
            {"source_id", source_id_},
            {"channel_id", channel_id},
            {"query", query},
            {"page", page},
            {"force_refresh", force_refresh},
        },
        nullptr,
        30000);
    if (!res.ok || !res.data.contains("items")) {
        return {};
    }
    return fromJsonArray<TextFeedItem>(res.data["items"]);
}

TextArticle WorkerTextSource::fetchArticle(const std::string& article_id) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "text.fetch_article"},
            {"source_id", source_id_},
            {"article_id", article_id},
        },
        nullptr,
        30000);
    TextArticle out;
    if (!res.ok || !res.data.contains("article")) {
        return out;
    }
    fromJson(res.data["article"], out);
    return out;
}

WorkerBookSource::WorkerBookSource(WorkerSupervisor& workers,
                                   std::string worker_id,
                                   std::string plugin_id,
                                   std::string source_id,
                                   std::string display_name)
    : workers_(workers),
      worker_id_(std::move(worker_id)),
      plugin_id_(std::move(plugin_id)),
      source_id_(std::move(source_id)),
      display_name_(std::move(display_name)) {}

std::vector<BookItem> WorkerBookSource::listBooks(const std::string& format_filter,
                                                  const std::string& query) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "book.list_books"},
            {"source_id", source_id_},
            {"format_filter", format_filter},
            {"query", query},
        },
        nullptr,
        30000);
    if (!res.ok || !res.data.contains("items")) {
        return {};
    }
    return fromJsonArray<BookItem>(res.data["items"]);
}

BookSpread WorkerBookSource::fetchSpread(const std::string& book_id, int page_number) {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "book.fetch_spread"},
            {"source_id", source_id_},
            {"book_id", book_id},
            {"page_number", page_number},
        },
        nullptr,
        30000);
    BookSpread out;
    if (!res.ok || !res.data.contains("spread")) {
        return out;
    }
    fromJson(res.data["spread"], out);
    return out;
}

std::vector<VocabCard> WorkerBookSource::listVocab() {
    CapabilityResult res = workers_.request(
        worker_id_,
        {
            {"op", "book.list_vocab"},
            {"source_id", source_id_},
        },
        nullptr,
        15000);
    if (!res.ok || !res.data.contains("vocab")) {
        return {};
    }
    return fromJsonArray<VocabCard>(res.data["vocab"]);
}

CapabilityStreamHandler makeWorkerStreamCapabilityHandler(
    WorkerSupervisor& workers,
    std::string worker_id,
    std::string capability_id,
    std::string envelope_op,
    std::string default_action) {
    return [&workers,
            worker_id = std::move(worker_id),
            capability_id = std::move(capability_id),
            envelope_op = std::move(envelope_op),
            default_action = std::move(default_action)](
               const nlohmann::json& request,
               const std::shared_ptr<CancelToken>& cancel,
               const StreamCallback& on_chunk) -> CapabilityResult {
        nlohmann::json payload;
        if (!envelope_op.empty()) {
            payload = {
                {"op", envelope_op},
                {"capability_id", capability_id},
                {"stream", static_cast<bool>(on_chunk)},
                {"request", request.is_object() ? request : nlohmann::json::object()},
            };
        } else {
            payload = request.is_object() ? request : nlohmann::json::object();
            if (!default_action.empty() && !payload.contains("action")) {
                payload["action"] = default_action;
            }
        }
        return workers.streamRequest(worker_id, payload, on_chunk, cancel, 300000);
    };
}

namespace {

struct HostBridgeState {
    WorkerSupervisor& workers;
    std::string worker_id;
    std::string plugin_id;
    std::string bridge_owner_id;
    CapabilityRegistry& capabilities;
    EntityBus& bus;
    std::atomic<bool> alive{true};
    std::atomic<bool> detached{false};
    std::mutex bus_op_mutex;
    std::mutex bus_mutex;
    std::unordered_set<std::string> subscribed_topics;
    uint64_t bus_token = 0;
    uint64_t cap_change_token = 0;

    HostBridgeState(WorkerSupervisor& w,
                    std::string wid,
                    std::string pid,
                    CapabilityRegistry& caps,
                    EntityBus& b)
        : workers(w),
          worker_id(std::move(wid)),
          plugin_id(std::move(pid)),
          bridge_owner_id("__worker_bridge__:" + worker_id),
          capabilities(caps),
          bus(b) {}
};

void clearBridgeSubscriptions(HostBridgeState& st) {
    std::lock_guard op_lk(st.bus_op_mutex);
    uint64_t tok = 0;
    {
        std::lock_guard lk(st.bus_mutex);
        tok = st.bus_token;
        st.bus_token = 0;
        st.subscribed_topics.clear();
    }
    if (tok != 0) {
        st.bus.unsubscribe(tok);
    }
}

class WorkerHostBridgeImpl final : public WorkerHostBridge {
public:
    explicit WorkerHostBridgeImpl(std::shared_ptr<HostBridgeState> state)
        : state_(std::move(state)) {}

    ~WorkerHostBridgeImpl() override {
        detach();
    }

    void detach() override {
        if (!state_) return;
        if (state_->detached.exchange(true)) return;
        state_->alive.store(false);
        state_->workers.setWorkerCallbacks(state_->worker_id, nullptr, nullptr, nullptr);
        if (state_->cap_change_token != 0) {
            state_->capabilities.unsubscribeChanges(state_->cap_change_token);
            state_->cap_change_token = 0;
        }
        clearBridgeSubscriptions(*state_);
    }

private:
    std::shared_ptr<HostBridgeState> state_;
};

}  // namespace

std::shared_ptr<WorkerHostBridge> attachWorkerHostBridge(
    WorkerSupervisor& workers,
    const std::string& worker_id,
    const std::string& plugin_id,
    CapabilityRegistry& capabilities,
    EntityBus& bus) {
    auto st = std::make_shared<HostBridgeState>(workers, worker_id, plugin_id, capabilities, bus);

    st->cap_change_token = capabilities.subscribeChanges([weak_st = std::weak_ptr<HostBridgeState>(st)]() {
        auto s = weak_st.lock();
        if (!s || !s->alive.load()) return;
        s->workers.sendEvent(s->worker_id, {
            {"kind", "event"},
            {"op", "host.cap.changed"},
        });
    });

    auto on_request = [st](const std::string&,
                           const std::string&,
                           const nlohmann::json& req,
                           const std::shared_ptr<CancelToken>& cancel,
                           const StreamCallback& on_chunk) -> CapabilityResult {
        if (!st->alive.load()) {
            return {false, nlohmann::json::object(), "worker host bridge detached", "", false};
        }
        const std::string op = req.value("op", "");
        if (op == "host.cap.has") {
            const std::string cap_id = req.value("capability_id", "");
            return {true, {{"has", st->capabilities.has(cap_id)}}, "", "", false};
        }
        if (op == "host.cap.providers") {
            const std::string cap_id = req.value("capability_id", "");
            nlohmann::json arr = nlohmann::json::array();
            for (const auto& d : st->capabilities.providersFor(cap_id)) {
                arr.push_back({
                    {"capability_id", d.capability_id},
                    {"plugin_id", d.plugin_id},
                    {"priority", d.priority},
                    {"metadata", d.metadata},
                });
            }
            return {true, {{"providers", std::move(arr)}}, "", "", false};
        }
        if (op == "host.cap.list") {
            nlohmann::json arr = nlohmann::json::array();
            for (const auto& d : st->capabilities.listAll()) {
                arr.push_back({
                    {"capability_id", d.capability_id},
                    {"plugin_id", d.plugin_id},
                    {"priority", d.priority},
                    {"metadata", d.metadata},
                });
            }
            return {true, {{"capabilities", std::move(arr)}}, "", "", false};
        }
        if (op == "host.cap.invoke") {
            const std::string cap_id = req.value("capability_id", "");
            const std::string target_plugin_id = req.value("plugin_id", "");
            const bool stream = req.value("stream", false);
            const nlohmann::json inner =
                req.contains("request") ? req["request"] : nlohmann::json::object();

            if (!st->plugin_id.empty() && target_plugin_id == st->plugin_id) {
                return {false,
                        nlohmann::json::object(),
                        "self-routing host.cap.invoke rejected for plugin '" + st->plugin_id + "'",
                        "",
                        false};
            }

            std::string resolved_plugin_id = target_plugin_id;
            if (resolved_plugin_id.empty()) {
                for (const auto& d : st->capabilities.providersFor(cap_id)) {
                    if (!d.plugin_id.empty() && d.plugin_id != st->plugin_id) {
                        resolved_plugin_id = d.plugin_id;
                        break;
                    }
                }
                if (resolved_plugin_id.empty()) {
                    return {false,
                            nlohmann::json::object(),
                            "capability not available: " + cap_id,
                            "",
                            false};
                }
            }

            if (stream) {
                return st->capabilities.callProviderStream(
                    resolved_plugin_id, cap_id, inner, on_chunk, cancel);
            }
            return st->capabilities.callProviderCancellable(
                resolved_plugin_id, cap_id, inner, cancel);
        }
        if (op == "host.bus.subscribe") {
            const std::string topic = req.value("topic", "");
            if (topic.empty()) {
                return {false, nlohmann::json::object(), "topic is required", "", false};
            }
            std::lock_guard op_lk(st->bus_op_mutex);
            if (!st->alive.load()) {
                return {false, nlohmann::json::object(), "worker host bridge detached", "", false};
            }
            bool need_sub = false;
            {
                std::lock_guard lk(st->bus_mutex);
                st->subscribed_topics.insert(topic);
                need_sub = (st->bus_token == 0);
            }
            if (need_sub) {
                uint64_t tok = st->bus.subscribe(
                    "*", st->bridge_owner_id,
                    [weak_st = std::weak_ptr<HostBridgeState>(st)](
                        const std::string& ev_topic, const nlohmann::json& payload) {
                        auto s = weak_st.lock();
                        if (!s || !s->alive.load()) return;
                        bool want = false;
                        {
                            std::lock_guard lk(s->bus_mutex);
                            want = s->subscribed_topics.count("*") > 0 ||
                                   s->subscribed_topics.count(ev_topic) > 0;
                        }
                        if (want) {
                            s->workers.sendEvent(s->worker_id, {
                                {"kind", "event"},
                                {"op", "host.bus.event"},
                                {"topic", ev_topic},
                                {"payload", payload},
                            });
                        }
                    });
                std::lock_guard lk(st->bus_mutex);
                st->bus_token = tok;
            }
            return {true, {{"subscribed", true}, {"topic", topic}}, "", "", false};
        }
        if (op == "host.bus.unsubscribe") {
            const std::string topic = req.value("topic", "");
            if (topic.empty()) {
                return {false, nlohmann::json::object(), "topic is required", "", false};
            }
            std::lock_guard op_lk(st->bus_op_mutex);
            uint64_t unsub_tok = 0;
            {
                std::lock_guard lk(st->bus_mutex);
                st->subscribed_topics.erase(topic);
                if (st->subscribed_topics.empty() && st->bus_token != 0) {
                    unsub_tok = st->bus_token;
                    st->bus_token = 0;
                }
            }
            if (unsub_tok != 0) {
                st->bus.unsubscribe(unsub_tok);
            }
            return {true, {{"unsubscribed", true}, {"topic", topic}}, "", "", false};
        }
        if (op == "host.bus.publish") {
            const std::string topic = req.value("topic", "");
            if (topic.empty()) {
                return {false, nlohmann::json::object(), "topic is required", "", false};
            }
            const nlohmann::json payload =
                req.contains("payload") ? req["payload"] : nlohmann::json::object();
            const size_t delivered =
                st->bus.publishExcludingOwner(topic, payload, st->bridge_owner_id);
            return {true, {{"delivered", delivered}}, "", "", false};
        }
        return {false, nlohmann::json::object(), "unsupported host operation: " + op, "", false};
    };

    auto on_exit = [weak_st = std::weak_ptr<HostBridgeState>(st)](const std::string&,
                                                                  const std::string&) {
        if (auto s = weak_st.lock()) {
            clearBridgeSubscriptions(*s);
        }
    };

    workers.setWorkerCallbacks(worker_id, std::move(on_request), nullptr, std::move(on_exit));
    return std::make_shared<WorkerHostBridgeImpl>(std::move(st));
}

}  // namespace omnidesk
