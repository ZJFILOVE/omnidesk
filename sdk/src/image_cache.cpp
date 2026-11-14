#include "omnidesk/image_cache.hpp"

#include <borealis/core/application.hpp>
#include <borealis/core/logger.hpp>
#include <borealis/core/thread.hpp>
#include <curl/curl.h>
#include <openssl/evp.h>
#include <stb_image.h>
#include <webp/decode.h>

#include <algorithm>
#include <condition_variable>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <queue>
#include <sstream>
#include <thread>

namespace fs = std::filesystem;

namespace omnidesk {

namespace {

bool isWebpMagic(const uint8_t* data, size_t size) {
    return data && size >= 12 &&
           std::memcmp(data, "RIFF", 4) == 0 &&
           std::memcmp(data + 8, "WEBP", 4) == 0;
}

std::string sha256Hex(const std::string& input) {
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    unsigned char hash[EVP_MAX_MD_SIZE];
    unsigned int hash_len = 0;
    if (ctx) {
        if (EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr) == 1 &&
            EVP_DigestUpdate(ctx, input.data(), input.size()) == 1 &&
            EVP_DigestFinal_ex(ctx, hash, &hash_len) == 1) {
            EVP_MD_CTX_free(ctx);
            static const char* hex = "0123456789abcdef";
            std::string out;
            out.reserve(hash_len * 2);
            for (unsigned int i = 0; i < hash_len; ++i) {
                out.push_back(hex[(hash[i] >> 4) & 0xF]);
                out.push_back(hex[hash[i] & 0xF]);
            }
            return out;
        }
        EVP_MD_CTX_free(ctx);
    }
    return {};
}

struct CurlWriteBuf {
    std::vector<uint8_t>* out = nullptr;
    size_t max_bytes = 32ull * 1024ull * 1024ull;
};

size_t curlWriteCallback(char* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* wb = static_cast<CurlWriteBuf*>(userdata);
    const size_t total = size * nmemb;
    if (!wb || !wb->out) return 0;
    if (wb->out->size() + total > wb->max_bytes) return 0;
    const auto* u8 = reinterpret_cast<const uint8_t*>(ptr);
    wb->out->insert(wb->out->end(), u8, u8 + total);
    return total;
}

int curlProgressCallback(void* clientp,
                         curl_off_t /*dltotal*/,
                         curl_off_t /*dlnow*/,
                         curl_off_t /*ultotal*/,
                         curl_off_t /*ulnow*/) {
    auto* cancel = static_cast<CancelToken*>(clientp);
    return (cancel && cancel->isCancelled()) ? 1 : 0;
}

bool readFileBytes(const fs::path& p, std::vector<uint8_t>& out) {
    std::ifstream ifs(p, std::ios::binary | std::ios::ate);
    if (!ifs) return false;
    const std::streamsize sz = ifs.tellg();
    if (sz <= 0) return false;
    out.resize(static_cast<size_t>(sz));
    ifs.seekg(0, std::ios::beg);
    return static_cast<bool>(ifs.read(reinterpret_cast<char*>(out.data()), sz));
}

}  // namespace

struct ImageCache::Impl {
    explicit Impl(ImageCacheConfig initial) : cfg(std::move(initial)) {
        std::error_code ec;
        if (!cfg.cache_dir.empty()) {
            fs::create_directories(cfg.cache_dir, ec);
        }
        startWorkers();
    }

    ~Impl() {
        stopWorkers();
    }

    void startWorkers() {
        const int count = std::max(1, cfg.worker_threads);
        stopping = false;
        workers.reserve(static_cast<size_t>(count));
        for (int i = 0; i < count; ++i) {
            workers.emplace_back([this]() { workerLoop(); });
        }
    }

    void stopWorkers() {
        {
            std::lock_guard lock(q_mutex);
            if (stopping) return;
            stopping = true;
        }
        q_cv.notify_all();
        for (auto& w : workers) {
            if (w.joinable()) w.join();
        }
        workers.clear();
    }

    void submit(std::function<void()> fn) {
        {
            std::lock_guard lock(q_mutex);
            if (stopping) return;
            queue.push(std::move(fn));
        }
        q_cv.notify_one();
    }

    void workerLoop() {
        while (true) {
            std::function<void()> task;
            {
                std::unique_lock lock(q_mutex);
                q_cv.wait(lock, [this]() { return stopping || !queue.empty(); });
                if (stopping && queue.empty()) return;
                task = std::move(queue.front());
                queue.pop();
            }
            try {
                if (task) task();
            } catch (...) {
            }
        }
    }

    mutable std::mutex cfg_mutex;
    ImageCacheConfig cfg;

    mutable std::mutex disk_mutex;
    std::atomic<uint64_t> tmp_seq{1};

    std::mutex q_mutex;
    std::condition_variable q_cv;
    std::queue<std::function<void()>> queue;
    std::vector<std::thread> workers;
    bool stopping = false;
};

ImageCache& ImageCache::instance() {
    static ImageCache singleton;
    return singleton;
}

ImageCache::ImageCache(ImageCacheConfig cfg) : impl_(std::make_unique<Impl>(std::move(cfg))) {}

ImageCache::~ImageCache() {
    shutdown();
}

void ImageCache::configure(const ImageCacheConfig& cfg) {
    std::lock_guard lock(impl_->cfg_mutex);
    impl_->cfg = cfg;
    std::error_code ec;
    if (!impl_->cfg.cache_dir.empty()) {
        fs::create_directories(impl_->cfg.cache_dir, ec);
    }
}

ImageCacheConfig ImageCache::config() const {
    std::lock_guard lock(impl_->cfg_mutex);
    return impl_->cfg;
}

void ImageCache::shutdown() {
    if (impl_) impl_->stopWorkers();
}

DecodedImage ImageCache::decodeBytes(const uint8_t* data, size_t size) {
    DecodedImage res;
    if (!data || size == 0) {
        res.error = "empty image buffer";
        return res;
    }

    if (isWebpMagic(data, size)) {
        int w = 0, h = 0;
        uint8_t* rgba = WebPDecodeRGBA(data, size, &w, &h);
        if (!rgba || w <= 0 || h <= 0) {
            if (rgba) WebPFree(rgba);
            res.error = "WebPDecodeRGBA failed";
            return res;
        }
        const size_t byte_len = static_cast<size_t>(w) * static_cast<size_t>(h) * 4u;
        res.rgba.assign(rgba, rgba + byte_len);
        WebPFree(rgba);
        res.width = w;
        res.height = h;
        res.ok = true;
        return res;
    }

    int w = 0, h = 0, channels = 0;
    unsigned char* rgba = stbi_load_from_memory(data, static_cast<int>(size), &w, &h, &channels, 4);
    if (!rgba || w <= 0 || h <= 0) {
        if (rgba) stbi_image_free(rgba);
        res.error = "stbi_load_from_memory failed";
        return res;
    }
    const size_t byte_len = static_cast<size_t>(w) * static_cast<size_t>(h) * 4u;
    res.rgba.assign(rgba, rgba + byte_len);
    stbi_image_free(rgba);
    res.width = w;
    res.height = h;
    res.ok = true;
    return res;
}

std::string ImageCache::cacheKeyFor(const std::string& url,
                                    const std::unordered_map<std::string, std::string>& headers) {
    std::map<std::string, std::string> sorted(headers.begin(), headers.end());
    std::ostringstream ss;
    ss << url;
    for (const auto& [k, v] : sorted) {
        ss << "\n" << k << ":" << v;
    }
    return sha256Hex(ss.str());
}

std::optional<std::vector<uint8_t>> ImageCache::getCachedRawBytes(const std::string& key) {
    if (key.empty()) return std::nullopt;
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return std::nullopt;

    std::lock_guard lock(impl_->disk_mutex);
    const fs::path target = fs::path(cfg.cache_dir) / (key + ".bin");
    std::error_code ec;
    if (!fs::exists(target, ec) || !fs::is_regular_file(target, ec)) {
        return std::nullopt;
    }
    std::vector<uint8_t> bytes;
    if (!readFileBytes(target, bytes)) {
        return std::nullopt;
    }
    fs::last_write_time(target, fs::file_time_type::clock::now(), ec);
    return bytes;
}

bool ImageCache::putCachedRawBytes(const std::string& key, const uint8_t* data, size_t size) {
    if (key.empty() || !data || size == 0) return false;
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return false;

    {
        std::lock_guard lock(impl_->disk_mutex);
        std::error_code ec;
        fs::create_directories(cfg.cache_dir, ec);
        const fs::path dir(cfg.cache_dir);
        const fs::path final_path = dir / (key + ".bin");
        const fs::path tmp_path =
            dir / (key + ".tmp." + std::to_string(impl_->tmp_seq.fetch_add(1)));

        {
            std::ofstream ofs(tmp_path, std::ios::binary | std::ios::trunc);
            if (!ofs) return false;
            ofs.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(size));
            ofs.flush();
            if (!ofs) {
                fs::remove(tmp_path, ec);
                return false;
            }
        }
        fs::rename(tmp_path, final_path, ec);
        if (ec) {
            fs::remove(tmp_path, ec);
            return false;
        }
    }
    evictIfNeeded();
    return true;
}

size_t ImageCache::diskUsageBytes() const {
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return 0;
    std::lock_guard lock(impl_->disk_mutex);
    std::error_code ec;
    if (!fs::exists(cfg.cache_dir, ec)) return 0;

    size_t total = 0;
    for (const auto& entry : fs::directory_iterator(cfg.cache_dir, ec)) {
        if (!entry.is_regular_file(ec)) continue;
        if (entry.path().extension() != ".bin") continue;
        total += static_cast<size_t>(entry.file_size(ec));
    }
    return total;
}

size_t ImageCache::evictIfNeeded() {
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return 0;

    std::lock_guard lock(impl_->disk_mutex);
    std::error_code ec;
    if (!fs::exists(cfg.cache_dir, ec)) return 0;

    struct DiskEntry {
        fs::path path;
        size_t size = 0;
        fs::file_time_type mtime;
    };

    std::vector<DiskEntry> entries;
    size_t total = 0;
    for (const auto& entry : fs::directory_iterator(cfg.cache_dir, ec)) {
        if (!entry.is_regular_file(ec)) continue;
        if (entry.path().extension() != ".bin") continue;
        DiskEntry item;
        item.path = entry.path();
        item.size = static_cast<size_t>(entry.file_size(ec));
        item.mtime = entry.last_write_time(ec);
        total += item.size;
        entries.push_back(std::move(item));
    }

    if (total <= cfg.max_disk_bytes) return 0;

    std::sort(entries.begin(), entries.end(), [](const DiskEntry& a, const DiskEntry& b) {
        return a.mtime < b.mtime;
    });

    size_t removed = 0;
    for (const auto& item : entries) {
        if (total <= cfg.max_disk_bytes) break;
        if (fs::remove(item.path, ec)) {
            total = (item.size <= total) ? (total - item.size) : 0;
            ++removed;
        }
    }
    return removed;
}

std::shared_ptr<CancelToken> ImageCache::fetchDecoded(
    const std::string& url,
    std::function<void(DecodedImage)> on_done,
    std::unordered_map<std::string, std::string> headers,
    std::shared_ptr<CancelToken> cancel) {
    if (!cancel) cancel = CancelToken::create();
    if (url.empty() || cancel->isCancelled()) {
        if (on_done && !cancel->isCancelled()) {
            DecodedImage empty;
            empty.error = "empty image url";
            on_done(std::move(empty));
        }
        return cancel;
    }

    impl_->submit([this, url, headers = std::move(headers),
                   on_done = std::move(on_done), cancel]() {
        if (cancel->isCancelled()) return;

        // Fast path: local file or file:// URI
        if (url.rfind("file://", 0) == 0 || url.rfind("/", 0) == 0) {
            std::string path_str = url.rfind("file://", 0) == 0 ? url.substr(7) : url;
            std::vector<uint8_t> bytes;
            DecodedImage res;
            if (!readFileBytes(path_str, bytes)) {
                res.error = "failed to open local image: " + path_str;
            } else {
                res = decodeBytes(bytes.data(), bytes.size());
            }
            if (!cancel->isCancelled() && on_done) on_done(std::move(res));
            return;
        }

        const std::string key = cacheKeyFor(url, headers);
        if (auto cached = getCachedRawBytes(key)) {
            DecodedImage res = decodeBytes(cached->data(), cached->size());
            if (res.ok) {
                if (!cancel->isCancelled() && on_done) on_done(std::move(res));
                return;
            }
        }

        const auto cfg = config();
        std::vector<uint8_t> downloaded;
        long http_code = 0;
        std::string curl_err;

        CURL* curl = curl_easy_init();
        if (!curl) {
            DecodedImage res;
            res.error = "curl_easy_init failed";
            if (!cancel->isCancelled() && on_done) on_done(std::move(res));
            return;
        }

        struct curl_slist* header_list = nullptr;
        for (const auto& [k, v] : headers) {
            std::string h = k + ": " + v;
            header_list = curl_slist_append(header_list, h.c_str());
        }

        CurlWriteBuf wb{&downloaded};
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, static_cast<long>(cfg.request_timeout_ms));
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
        curl_easy_setopt(curl, CURLOPT_USERAGENT, "OmniDesk/1.0");
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteCallback);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &wb);
        curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
        curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, curlProgressCallback);
        curl_easy_setopt(curl, CURLOPT_XFERINFODATA, cancel.get());
        if (header_list) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, header_list);

        CURLcode code = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
        if (code != CURLE_OK) curl_err = curl_easy_strerror(code);

        if (header_list) curl_slist_free_all(header_list);
        curl_easy_cleanup(curl);

        if (cancel->isCancelled()) return;

        DecodedImage res;
        if (code != CURLE_OK) {
            res.error = "http fetch failed: " + curl_err;
        } else if (http_code < 200 || http_code >= 300 || downloaded.empty()) {
            res.error = "unexpected HTTP status: " + std::to_string(http_code);
        } else {
            res = decodeBytes(downloaded.data(), downloaded.size());
            if (res.ok) {
                putCachedRawBytes(key, downloaded.data(), downloaded.size());
            }
        }

        if (!cancel->isCancelled() && on_done) {
            on_done(std::move(res));
        }
    });

    return cancel;
}

// ---- CachedImageView ----

CachedImageView::CachedImageView(ImageCache* cache)
    : cache_(cache ? cache : &ImageCache::instance()),
      alive_(std::make_shared<std::atomic<bool>>(true)) {
    setScalingType(brls::ImageScalingType::FILL);
    setFreeTexture(true);
}

CachedImageView::~CachedImageView() {
    alive_->store(false);
    cancelPending();
}

void CachedImageView::cancelPending() {
    if (pending_cancel_) {
        pending_cancel_->cancel();
        pending_cancel_.reset();
    }
}

void CachedImageView::setImageUrl(const std::string& url,
                                  std::unordered_map<std::string, std::string> headers) {
    if (url == current_url_ && getTexture() > 0) return;
    cancelPending();
    current_url_ = url;
    if (url.empty()) {
        clear();
        return;
    }

    auto alive = alive_;
    pending_cancel_ = cache_->fetchDecoded(
        url,
        [this, alive, url](DecodedImage img) {
            if (!img.ok || img.rgba.empty() || img.width <= 0 || img.height <= 0) return;
            brls::sync([this, alive, url, img = std::move(img)]() {
                if (!alive->load() || url != current_url_) return;
                NVGcontext* vg = brls::Application::getNVGContext();
                if (!vg) return;
                int tex = nvgCreateImageRGBA(vg, img.width, img.height, 0, img.rgba.data());
                if (tex > 0) {
                    innerSetImage(tex);
                }
            });
        },
        std::move(headers));
}

}  // namespace omnidesk
