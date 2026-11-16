#include "omnidesk/image_cache.hpp"

#include <SDL.h>
#include <borealis/core/application.hpp>
#include <borealis/core/logger.hpp>
#include <borealis/core/thread.hpp>
#include <curl/curl.h>
#include <lunasvg.h>
#include <openssl/evp.h>
#include <stb_image.h>
#ifdef OMNIDESK_HAS_TURBOJPEG
#include <turbojpeg.h>
#endif
#include <webp/decode.h>
#include <yoga/Yoga.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <list>
#include <map>
#include <mutex>
#include <queue>
#include <sstream>
#include <thread>

namespace fs = std::filesystem;

namespace omnidesk {

namespace {

thread_local CURL* g_worker_curl = nullptr;
std::atomic<bool> g_ui_wake_pending{false};

void wakeSdlEventLoopOnce() {
    if (SDL_WasInit(SDL_INIT_EVENTS) == 0) return;
    static const Uint32 kWakeType = SDL_RegisterEvents(1);
    if (kWakeType == static_cast<Uint32>(-1)) return;
    SDL_Event ev{};
    ev.type = kWakeType;
    SDL_PushEvent(&ev);
}

void ensureCurlGlobalInit() {
    static std::once_flag once;
    std::call_once(once, []() { curl_global_init(CURL_GLOBAL_DEFAULT); });
}

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

bool computeScaledSize(int src_w, int src_h, int max_w, int max_h, int& out_w, int& out_h) {
    out_w = src_w;
    out_h = src_h;
    if (src_w <= 0 || src_h <= 0 || (max_w <= 0 && max_h <= 0)) {
        return false;
    }
    const double sx = (max_w > 0 && src_w > max_w)
                          ? (static_cast<double>(max_w) / static_cast<double>(src_w))
                          : 1.0;
    const double sy = (max_h > 0 && src_h > max_h)
                          ? (static_cast<double>(max_h) / static_cast<double>(src_h))
                          : 1.0;
    const double scale = std::min(sx, sy);
    if (scale >= 0.999) {
        return false;
    }
    out_w = std::max(1, static_cast<int>(std::lround(static_cast<double>(src_w) * scale)));
    out_h = std::max(1, static_cast<int>(std::lround(static_cast<double>(src_h) * scale)));
    if (max_w > 0 && out_w > max_w) out_w = max_w;
    if (max_h > 0 && out_h > max_h) out_h = max_h;
    return out_w < src_w || out_h < src_h;
}

std::vector<uint8_t> downsampleRgbaArea(const uint8_t* src,
                                        int src_w,
                                        int src_h,
                                        int dst_w,
                                        int dst_h) {
    std::vector<uint8_t> dst(static_cast<size_t>(dst_w) * static_cast<size_t>(dst_h) * 4u);
    for (int dy = 0; dy < dst_h; ++dy) {
        const int y0 = static_cast<int>((static_cast<int64_t>(dy) * src_h) / dst_h);
        const int y1 = std::max(
            y0 + 1,
            std::min(src_h, static_cast<int>(((static_cast<int64_t>(dy) + 1) * src_h + dst_h - 1) / dst_h)));
        uint8_t* dst_row = dst.data() + static_cast<size_t>(dy) * static_cast<size_t>(dst_w) * 4u;
        for (int dx = 0; dx < dst_w; ++dx) {
            const int x0 = static_cast<int>((static_cast<int64_t>(dx) * src_w) / dst_w);
            const int x1 = std::max(
                x0 + 1,
                std::min(src_w, static_cast<int>(((static_cast<int64_t>(dx) + 1) * src_w + dst_w - 1) / dst_w)));
            uint32_t r = 0, g = 0, b = 0, a = 0;
            uint32_t count = 0;
            for (int sy = y0; sy < y1; ++sy) {
                const uint8_t* p =
                    src + (static_cast<size_t>(sy) * static_cast<size_t>(src_w) + static_cast<size_t>(x0)) * 4u;
                for (int sx = x0; sx < x1; ++sx, p += 4) {
                    r += p[0];
                    g += p[1];
                    b += p[2];
                    a += p[3];
                    ++count;
                }
            }
            const uint32_t half = count / 2u;
            uint8_t* out_px = dst_row + static_cast<size_t>(dx) * 4u;
            out_px[0] = static_cast<uint8_t>((r + half) / count);
            out_px[1] = static_cast<uint8_t>((g + half) / count);
            out_px[2] = static_cast<uint8_t>((b + half) / count);
            out_px[3] = static_cast<uint8_t>((a + half) / count);
        }
    }
    return dst;
}

int quantizeDimHint(int v) {
    if (v <= 0) return 0;
    if (v <= 64) return 64;
    return ((v + 63) / 64) * 64;
}

std::string makeMemCacheKey(const std::string& raw_key, int max_w, int max_h) {
    const int qw = quantizeDimHint(max_w);
    const int qh = quantizeDimHint(max_h);
    if (qw <= 0 && qh <= 0) return raw_key;
    return raw_key + "@" + std::to_string(qw) + "x" + std::to_string(qh);
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

bool readFileBytes(const fs::path& p, std::vector<uint8_t>& out) {
    std::ifstream ifs(p, std::ios::binary | std::ios::ate);
    if (!ifs) return false;
    const std::streamsize sz = ifs.tellg();
    if (sz <= 0) return false;
    out.resize(static_cast<size_t>(sz));
    ifs.seekg(0, std::ios::beg);
    return static_cast<bool>(ifs.read(reinterpret_cast<char*>(out.data()), sz));
}

bool isSafeCacheKey(const std::string& key) {
    if (key.empty() || key.size() > 200) return false;
    if (key.front() == '.') return false;
    if (key.find("..") != std::string::npos) return false;
    for (char c : key) {
        const auto uc = static_cast<unsigned char>(c);
        const bool ok = (uc >= 'A' && uc <= 'Z') ||
                        (uc >= 'a' && uc <= 'z') ||
                        (uc >= '0' && uc <= '9') ||
                        uc == '.' || uc == '_' || uc == '-';
        if (!ok) return false;
    }
    return true;
}

std::string toLowerAscii(std::string s) {
    for (char& c : s) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return s;
}

std::string trimAscii(std::string s) {
    auto not_space = [](unsigned char c) { return !std::isspace(c); };
    s.erase(s.begin(), std::find_if(s.begin(), s.end(), not_space));
    s.erase(std::find_if(s.rbegin(), s.rend(), not_space).base(), s.end());
    return s;
}

void replaceAllInPlace(std::string& s, const std::string& from, const std::string& to) {
    if (from.empty()) return;
    size_t pos = 0;
    while ((pos = s.find(from, pos)) != std::string::npos) {
        s.replace(pos, from.size(), to);
        pos += to.size();
    }
}

bool startsWithNoCase(const std::string& s, const std::string& prefix) {
    if (s.size() < prefix.size()) return false;
    for (size_t i = 0; i < prefix.size(); ++i) {
        if (std::tolower(static_cast<unsigned char>(s[i])) !=
            std::tolower(static_cast<unsigned char>(prefix[i]))) {
            return false;
        }
    }
    return true;
}

bool endsWithNoCase(const std::string& s, const std::string& suffix) {
    if (s.size() < suffix.size()) return false;
    const size_t offset = s.size() - suffix.size();
    for (size_t i = 0; i < suffix.size(); ++i) {
        if (std::tolower(static_cast<unsigned char>(s[offset + i])) !=
            std::tolower(static_cast<unsigned char>(suffix[i]))) {
            return false;
        }
    }
    return true;
}

bool hostMatchesDomain(const std::string& host, const std::string& domain) {
    if (host.empty() || domain.empty()) return false;
    std::string h = toLowerAscii(host);
    std::string d = toLowerAscii(domain);
    if (!d.empty() && d.front() == '.') d.erase(d.begin());
    if (h == d) return true;
    return h.size() > d.size() + 1 &&
           h[h.size() - d.size() - 1] == '.' &&
           endsWithNoCase(h, d);
}

std::string extractUrlSchemeAndHost(const std::string& url,
                                    std::string* scheme_out = nullptr,
                                    std::string* host_out = nullptr) {
    const size_t sep = url.find("://");
    if (sep == std::string::npos) return {};
    const std::string scheme = toLowerAscii(url.substr(0, sep));
    const size_t host_start = sep + 3;
    const size_t path_pos = url.find_first_of("/?#", host_start);
    std::string authority = (path_pos == std::string::npos)
                                ? url.substr(host_start)
                                : url.substr(host_start, path_pos - host_start);
    if (scheme_out) *scheme_out = scheme;
    std::string host = authority;
    const size_t at_pos = host.rfind('@');
    if (at_pos != std::string::npos) host = host.substr(at_pos + 1);
    if (!host.empty() && host.front() == '[') {
        const size_t close_br = host.find(']');
        if (close_br != std::string::npos) host = host.substr(0, close_br + 1);
    } else {
        const size_t colon = host.find(':');
        if (colon != std::string::npos) host = host.substr(0, colon);
    }
    if (host_out) *host_out = toLowerAscii(host);
    return scheme + "://" + authority + "/";
}

bool isPrivateOrLocalHost(const std::string& host) {
    if (host.empty()) return true;
    if (host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "[::1]") return true;
    if (endsWithNoCase(host, ".local") || endsWithNoCase(host, ".lan")) return true;
    if (host.rfind("192.168.", 0) == 0 || host.rfind("10.", 0) == 0) return true;
    if (host.rfind("172.", 0) == 0) return true;
    return false;
}

std::string inferDefaultReferer(const std::string& url) {
    std::string host;
    const std::string origin = extractUrlSchemeAndHost(url, nullptr, &host);
    if (host.empty()) return {};
    if (hostMatchesDomain(host, "hdslb.com") || hostMatchesDomain(host, "bilibili.com") ||
        hostMatchesDomain(host, "biliimg.com")) {
        return "https://www.bilibili.com/";
    }
    if (hostMatchesDomain(host, "zhimg.com") || hostMatchesDomain(host, "zhihu.com")) {
        return "https://www.zhihu.com/";
    }
    if (hostMatchesDomain(host, "twimg.com") || hostMatchesDomain(host, "x.com") ||
        hostMatchesDomain(host, "twitter.com")) {
        return "https://x.com/";
    }
    if (hostMatchesDomain(host, "redd.it") || hostMatchesDomain(host, "redditmedia.com") ||
        hostMatchesDomain(host, "redditstatic.com") || hostMatchesDomain(host, "reddit.com")) {
        return "https://www.reddit.com/";
    }
    if (hostMatchesDomain(host, "v2ex.co") || hostMatchesDomain(host, "v2ex.com")) {
        return "https://www.v2ex.com/";
    }
    if (hostMatchesDomain(host, "imgur.com")) {
        return "https://imgur.com/";
    }
    if (hostMatchesDomain(host, "media-amazon.com") || hostMatchesDomain(host, "imdb.com")) {
        return "https://www.imdb.com/";
    }
    if (hostMatchesDomain(host, "doubanio.com") || hostMatchesDomain(host, "douban.com")) {
        return "https://movie.douban.com/";
    }
    if (hostMatchesDomain(host, "sinaimg.cn") || hostMatchesDomain(host, "weibo.com")) {
        return "https://weibo.com/";
    }
    if (hostMatchesDomain(host, "mzstatic.com") || hostMatchesDomain(host, "apple.com")) {
        return "https://music.apple.com/";
    }
    return origin;
}

bool isSvgPayload(const uint8_t* data, size_t size) {
    if (!data || size < 4) return false;
    const size_t scan_len = std::min<size_t>(size, 512);
    std::string head(reinterpret_cast<const char*>(data), scan_len);
    std::string low = toLowerAscii(head);
    return low.find("<svg") != std::string::npos;
}

std::vector<uint8_t> decodeBase64Bytes(const std::string& input) {
    static const int8_t kDec[256] = {
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,62,-1,62,-1,63,
        52,53,54,55,56,57,58,59,60,61,-1,-1,-1,-1,-1,-1,
        -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,
        15,16,17,18,19,20,21,22,23,24,25,-1,-1,-1,-1,63,
        -1,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,
        41,42,43,44,45,46,47,48,49,50,51,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1
    };
    std::vector<uint8_t> out;
    out.reserve((input.size() * 3) / 4);
    uint32_t val = 0;
    int valb = -8;
    for (unsigned char c : input) {
        if (std::isspace(c) || c == '=') continue;
        int d = kDec[c];
        if (d == -1) continue;
        val = (val << 6) + static_cast<uint32_t>(d);
        valb += 6;
        if (valb >= 0) {
            out.push_back(static_cast<uint8_t>((val >> valb) & 0xFF));
            valb -= 8;
        }
    }
    return out;
}

std::string percentDecodeAscii(const std::string& input) {
    std::string out;
    out.reserve(input.size());
    for (size_t i = 0; i < input.size(); ++i) {
        if (input[i] == '%' && i + 2 < input.size()) {
            auto hexVal = [](char c) -> int {
                if (c >= '0' && c <= '9') return c - '0';
                if (c >= 'a' && c <= 'f') return c - 'a' + 10;
                if (c >= 'A' && c <= 'F') return c - 'A' + 10;
                return -1;
            };
            int hi = hexVal(input[i + 1]);
            int lo = hexVal(input[i + 2]);
            if (hi >= 0 && lo >= 0) {
                out.push_back(static_cast<char>((hi << 4) | lo));
                i += 2;
                continue;
            }
        }
        out.push_back(input[i]);
    }
    return out;
}

std::vector<uint8_t> decodeDataUriBytes(const std::string& url) {
    const size_t comma = url.find(',');
    if (comma == std::string::npos) return {};
    const std::string meta = toLowerAscii(url.substr(0, comma));
    const std::string payload = url.substr(comma + 1);
    if (meta.find(";base64") != std::string::npos) {
        return decodeBase64Bytes(payload);
    }
    std::string raw = percentDecodeAscii(payload);
    return std::vector<uint8_t>(raw.begin(), raw.end());
}

}  // namespace

struct ImageCache::Impl {
    struct TaskItem {
        uint64_t id = 0;
        std::shared_ptr<CancelToken> cancel;
        std::function<void()> fn;
    };

    struct CoalescedWaiter {
        std::shared_ptr<CancelToken> cancel;
        int max_w = 0;
        int max_h = 0;
        std::function<void(DecodedImage)> on_done;
    };

    struct InFlightFetch {
        std::shared_ptr<CancelToken> shared_cancel = CancelToken::create();
        std::vector<CoalescedWaiter> waiters;
    };

    struct ProgressCtx {
        Impl* impl = nullptr;
        InFlightFetch* entry = nullptr;
    };

    static void curlShareLockCb(CURL* /*handle*/,
                                curl_lock_data data,
                                curl_lock_access /*access*/,
                                void* userptr) {
        auto* self = static_cast<Impl*>(userptr);
        const size_t idx = static_cast<size_t>(data);
        if (self && idx < self->share_mutexes.size()) {
            self->share_mutexes[idx].lock();
        }
    }

    static void curlShareUnlockCb(CURL* /*handle*/,
                                  curl_lock_data data,
                                  void* userptr) {
        auto* self = static_cast<Impl*>(userptr);
        const size_t idx = static_cast<size_t>(data);
        if (self && idx < self->share_mutexes.size()) {
            self->share_mutexes[idx].unlock();
        }
    }

    static int curlCoalescedProgressCb(void* clientp,
                                       curl_off_t /*dltotal*/,
                                       curl_off_t /*dlnow*/,
                                       curl_off_t /*ultotal*/,
                                       curl_off_t /*ulnow*/) {
        auto* ctx = static_cast<ProgressCtx*>(clientp);
        if (!ctx || !ctx->impl || !ctx->entry) return 0;
        if (ctx->entry->shared_cancel->isCancelled()) return 1;
        std::lock_guard lk(ctx->impl->inflight_mutex);
        if (ctx->entry->waiters.empty()) return 0;
        for (const auto& w : ctx->entry->waiters) {
            if (!w.cancel || !w.cancel->isCancelled()) {
                return 0;
            }
        }
        ctx->entry->shared_cancel->cancel();
        return 1;
    }

    explicit Impl(ImageCacheConfig initial) : cfg(std::move(initial)) {
        ensureCurlGlobalInit();
        curl_share = curl_share_init();
        if (curl_share) {
            curl_share_setopt(curl_share, CURLSHOPT_USERDATA, this);
            curl_share_setopt(curl_share, CURLSHOPT_LOCKFUNC, &Impl::curlShareLockCb);
            curl_share_setopt(curl_share, CURLSHOPT_UNLOCKFUNC, &Impl::curlShareUnlockCb);
            curl_share_setopt(curl_share, CURLSHOPT_SHARE, CURL_LOCK_DATA_DNS);
            curl_share_setopt(curl_share, CURLSHOPT_SHARE, CURL_LOCK_DATA_SSL_SESSION);
            curl_share_setopt(curl_share, CURLSHOPT_SHARE, CURL_LOCK_DATA_CONNECT);
        }

        std::error_code ec;
        if (!cfg.cache_dir.empty()) {
            fs::create_directories(cfg.cache_dir, ec);
        }
        startWorkers();
    }

    ~Impl() {
        stopWorkers();
        if (curl_share) {
            curl_share_cleanup(curl_share);
            curl_share = nullptr;
        }
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
        std::vector<std::shared_ptr<CancelToken>> to_cancel;
        std::vector<TaskItem> dropped;
        {
            std::lock_guard lock(q_mutex);
            if (stopping) return;
            stopping = true;
            for (const auto& [_, tok] : active_cancels) {
                if (tok) to_cancel.push_back(tok);
            }
            while (!queue.empty()) {
                dropped.push_back(std::move(queue.front()));
                queue.pop();
            }
        }
        {
            std::lock_guard lk(inflight_mutex);
            for (auto& [_, entry] : inflight_map) {
                if (entry && entry->shared_cancel) {
                    to_cancel.push_back(entry->shared_cancel);
                }
            }
            inflight_map.clear();
        }
        dropped.clear();
        for (auto& tok : to_cancel) {
            tok->cancel();
        }
        q_cv.notify_all();
        const auto self_id = std::this_thread::get_id();
        for (auto& w : workers) {
            if (!w.joinable()) continue;
            if (w.get_id() == self_id) {
                w.detach();
            } else {
                w.join();
            }
        }
        workers.clear();
    }

    bool submit(std::shared_ptr<CancelToken> cancel, std::function<void()> fn) {
        {
            std::lock_guard lock(q_mutex);
            if (stopping) {
                if (cancel) cancel->cancel();
                return false;
            }
            const uint64_t id = next_task_id++;
            if (cancel) {
                active_cancels[id] = cancel;
            }
            queue.push(TaskItem{id, std::move(cancel), std::move(fn)});
        }
        q_cv.notify_one();
        return true;
    }

    void workerLoop() {
        ensureCurlGlobalInit();
        CURL* local_curl = curl_easy_init();
        g_worker_curl = local_curl;
        struct WorkerCurlGuard {
            CURL* handle = nullptr;
            ~WorkerCurlGuard() {
                g_worker_curl = nullptr;
                if (handle) {
                    curl_easy_cleanup(handle);
                }
            }
        } curl_guard{local_curl};

        while (true) {
            TaskItem item;
            {
                std::unique_lock lock(q_mutex);
                q_cv.wait(lock, [this]() { return stopping || !queue.empty(); });
                if (stopping && queue.empty()) return;
                item = std::move(queue.front());
                queue.pop();
            }
            try {
                if (item.fn && !(item.cancel && item.cancel->isCancelled())) {
                    item.fn();
                }
            } catch (...) {
            }
            item.fn = {};
            {
                std::lock_guard lock(q_mutex);
                active_cancels.erase(item.id);
            }
        }
    }

    size_t ensureDiskUsageInitializedLocked(const fs::path& dir) const {
        const std::string dir_str = dir.string();
        if (disk_usage_initialized.load(std::memory_order_acquire) &&
            tracked_cache_dir == dir_str) {
            return disk_usage_bytes.load(std::memory_order_relaxed);
        }
        std::error_code ec;
        size_t total = 0;
        if (!dir_str.empty() && fs::exists(dir, ec)) {
            for (const auto& entry : fs::directory_iterator(dir, ec)) {
                if (!entry.is_regular_file(ec)) continue;
                if (entry.path().extension() != ".bin") continue;
                total += static_cast<size_t>(entry.file_size(ec));
            }
        }
        tracked_cache_dir = dir_str;
        disk_usage_bytes.store(total, std::memory_order_release);
        disk_usage_initialized.store(true, std::memory_order_release);
        return total;
    }

    void completeCoalescedFetch(const std::string& raw_key,
                                const std::shared_ptr<InFlightFetch>& entry,
                                bool raw_ok,
                                const std::vector<uint8_t>& raw_bytes,
                                const std::string& fetch_error) {
        std::unordered_map<std::string, DecodedImage> decoded_buckets;
        while (true) {
            std::vector<CoalescedWaiter> batch;
            {
                std::lock_guard lk(inflight_mutex);
                if (entry->waiters.empty()) {
                    auto it = inflight_map.find(raw_key);
                    if (it != inflight_map.end() && it->second == entry) {
                        inflight_map.erase(it);
                    }
                    break;
                }
                batch = std::move(entry->waiters);
                entry->waiters.clear();
            }

            for (auto& w : batch) {
                if (w.cancel && w.cancel->isCancelled()) continue;
                DecodedImage res;
                if (!raw_ok) {
                    res.error = fetch_error;
                } else {
                    const std::string mkey = makeMemCacheKey(raw_key, w.max_w, w.max_h);
                    if (auto dit = decoded_buckets.find(mkey); dit != decoded_buckets.end()) {
                        res = dit->second;
                    } else if (auto mhit = getMemCached(mkey)) {
                        res = std::move(*mhit);
                        decoded_buckets.emplace(mkey, res);
                    } else {
                        res = ImageCache::decodeBytes(
                            raw_bytes.data(), raw_bytes.size(), w.max_w, w.max_h);
                        if (res.ok) {
                            putMemCached(mkey, res);
                        }
                        decoded_buckets.emplace(mkey, res);
                    }
                }
                if (!(w.cancel && w.cancel->isCancelled()) && w.on_done) {
                    w.on_done(std::move(res));
                }
            }
        }
    }

    CURLSH* curl_share = nullptr;
    std::array<std::mutex, CURL_LOCK_DATA_LAST> share_mutexes;

    mutable std::mutex cfg_mutex;
    ImageCacheConfig cfg;

    mutable std::mutex domain_headers_mutex;
    std::unordered_map<std::string, std::unordered_map<std::string, std::string>> domain_headers;

    mutable std::mutex disk_mutex;
    mutable std::atomic<size_t> disk_usage_bytes{0};
    mutable std::atomic<bool> disk_usage_initialized{false};
    mutable std::string tracked_cache_dir;
    std::atomic<uint64_t> tmp_seq{1};

    std::mutex inflight_mutex;
    std::unordered_map<std::string, std::shared_ptr<InFlightFetch>> inflight_map;

    static constexpr size_t kMaxMemCacheBytes = 96ull * 1024ull * 1024ull;
    static constexpr size_t kMaxMemEntryBytes = 16ull * 1024ull * 1024ull;
    struct MemCacheEntry {
        std::string key;
        DecodedImage image;
    };
    mutable std::mutex mem_mutex;
    std::list<MemCacheEntry> mem_lru;
    std::unordered_map<std::string, std::list<MemCacheEntry>::iterator> mem_map;
    size_t mem_bytes = 0;

    std::optional<DecodedImage> getMemCached(const std::string& key) {
        std::lock_guard lock(mem_mutex);
        auto it = mem_map.find(key);
        if (it == mem_map.end()) return std::nullopt;
        mem_lru.splice(mem_lru.begin(), mem_lru, it->second);
        return it->second->image;
    }

    void putMemCached(const std::string& key, const DecodedImage& img) {
        if (!img.ok || img.rgba.empty() || img.rgba.size() > kMaxMemEntryBytes) return;
        std::lock_guard lock(mem_mutex);
        if (auto it = mem_map.find(key); it != mem_map.end()) {
            mem_bytes -= it->second->image.rgba.size();
            mem_lru.erase(it->second);
            mem_map.erase(it);
        }
        while (mem_bytes + img.rgba.size() > kMaxMemCacheBytes && !mem_lru.empty()) {
            auto last = std::prev(mem_lru.end());
            mem_bytes -= last->image.rgba.size();
            mem_map.erase(last->key);
            mem_lru.erase(last);
        }
        mem_lru.push_front(MemCacheEntry{key, img});
        mem_map[key] = mem_lru.begin();
        mem_bytes += img.rgba.size();
    }

    std::mutex q_mutex;
    std::condition_variable q_cv;
    std::queue<TaskItem> queue;
    std::unordered_map<uint64_t, std::shared_ptr<CancelToken>> active_cancels;
    uint64_t next_task_id = 1;
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
    bool dir_changed = false;
    {
        std::lock_guard lock(impl_->cfg_mutex);
        dir_changed = (impl_->cfg.cache_dir != cfg.cache_dir);
        impl_->cfg = cfg;
        std::error_code ec;
        if (!impl_->cfg.cache_dir.empty()) {
            fs::create_directories(impl_->cfg.cache_dir, ec);
        }
    }
    if (dir_changed) {
        std::lock_guard dlock(impl_->disk_mutex);
        impl_->disk_usage_initialized.store(false, std::memory_order_release);
        impl_->tracked_cache_dir.clear();
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
    return decodeBytes(data, size, 0, 0);
}

DecodedImage ImageCache::decodeBytes(const uint8_t* data,
                                     size_t size,
                                     int max_width,
                                     int max_height) {
    DecodedImage res;
    if (!data || size == 0) {
        res.error = "empty image buffer";
        return res;
    }
    if (size > 64ull * 1024ull * 1024ull) {
        res.error = "image buffer exceeds 64 MiB limit";
        return res;
    }

    if (isWebpMagic(data, size)) {
        WebPDecoderConfig config;
        if (!WebPInitDecoderConfig(&config)) {
            res.error = "WebPInitDecoderConfig failed";
            return res;
        }
        if (WebPGetFeatures(data, size, &config.input) != VP8_STATUS_OK ||
            config.input.width <= 0 || config.input.height <= 0 ||
            config.input.width > 16384 || config.input.height > 16384) {
            res.error = "WebPGetFeatures failed";
            return res;
        }
        int out_w = config.input.width;
        int out_h = config.input.height;
        if (computeScaledSize(config.input.width, config.input.height,
                              max_width, max_height, out_w, out_h)) {
            config.options.use_scaling = 1;
            config.options.scaled_width = out_w;
            config.options.scaled_height = out_h;
        }
        const size_t byte_len = static_cast<size_t>(out_w) * static_cast<size_t>(out_h) * 4u;
        res.rgba.resize(byte_len);
        config.output.colorspace = MODE_RGBA;
        config.output.u.RGBA.rgba = res.rgba.data();
        config.output.u.RGBA.stride = out_w * 4;
        config.output.u.RGBA.size = byte_len;
        config.output.is_external_memory = 1;
        if (WebPDecode(data, size, &config) != VP8_STATUS_OK) {
            WebPFreeDecBuffer(&config.output);
            res.rgba.clear();
            res.error = "WebPDecode failed";
            return res;
        }
        WebPFreeDecBuffer(&config.output);
        res.width = out_w;
        res.height = out_h;
        res.ok = true;
        return res;
    }

    if (isSvgPayload(data, size)) {
        auto doc = lunasvg::Document::loadFromData(reinterpret_cast<const char*>(data), size);
        if (!doc) {
            res.error = "lunasvg loadFromData failed";
            return res;
        }
        double dw = doc->width();
        double dh = doc->height();
        if (dw <= 0.0 || dh <= 0.0) {
            dw = 256.0;
            dh = 256.0;
        }
        double scale = 1.0;
        if (max_width > 0 || max_height > 0) {
            const double cap_w = (max_width > 0) ? static_cast<double>(max_width) : 512.0;
            const double cap_h = (max_height > 0) ? static_cast<double>(max_height) : 512.0;
            scale = std::min(cap_w / dw, cap_h / dh);
            if (scale > 1.0 && std::max(dw, dh) >= 64.0) {
                scale = 1.0;
            }
        } else {
            const double max_dim = std::max(dw, dh);
            scale = max_dim > 512.0 ? (512.0 / max_dim) : (max_dim < 64.0 ? (128.0 / max_dim) : 1.0);
        }
        const uint32_t w = std::max<uint32_t>(1, static_cast<uint32_t>(dw * scale + 0.5));
        const uint32_t h = std::max<uint32_t>(1, static_cast<uint32_t>(dh * scale + 0.5));
        lunasvg::Bitmap bmp = doc->renderToBitmap(w, h);
        if (!bmp.valid()) {
            res.error = "lunasvg renderToBitmap failed";
            return res;
        }
        bmp.convertToRGBA();
        const size_t byte_len = static_cast<size_t>(w) * static_cast<size_t>(h) * 4u;
        res.rgba.assign(bmp.data(), bmp.data() + byte_len);
        res.width = static_cast<int>(w);
        res.height = static_cast<int>(h);
        res.ok = true;
        return res;
    }

#ifdef OMNIDESK_HAS_TURBOJPEG
    if (size >= 3 && data[0] == 0xFF && data[1] == 0xD8 && data[2] == 0xFF) {
        struct ThreadLocalTj {
            tjhandle handle = nullptr;
            ThreadLocalTj() : handle(tjInitDecompress()) {}
            ~ThreadLocalTj() {
                if (handle) {
                    tjDestroy(handle);
                    handle = nullptr;
                }
            }
        };
        thread_local ThreadLocalTj tl_tj;
        if (tl_tj.handle) {
            int jw = 0, jh = 0, subsamp = 0, colorspace = 0;
            if (tjDecompressHeader3(tl_tj.handle, data, static_cast<unsigned long>(size),
                                    &jw, &jh, &subsamp, &colorspace) == 0 &&
                jw > 0 && jh > 0 && jw <= 16384 && jh <= 16384) {
                int decode_w = jw;
                int decode_h = jh;
                int target_w = jw;
                int target_h = jh;
                const bool want_downscale =
                    computeScaledSize(jw, jh, max_width, max_height, target_w, target_h);
                if (want_downscale) {
                    int nfactors = 0;
                    const tjscalingfactor* factors = tjGetScalingFactors(&nfactors);
                    if (factors && nfactors > 0) {
                        int best_w = jw;
                        int best_h = jh;
                        for (int i = 0; i < nfactors; ++i) {
                            const int sw = TJSCALED(jw, factors[i]);
                            const int sh = TJSCALED(jh, factors[i]);
                            if (sw >= target_w && sh >= target_h &&
                                (sw < best_w || sh < best_h)) {
                                best_w = sw;
                                best_h = sh;
                            }
                        }
                        decode_w = best_w;
                        decode_h = best_h;
                    }
                }

                std::vector<uint8_t> decoded(
                    static_cast<size_t>(decode_w) * static_cast<size_t>(decode_h) * 4u);
                if (tjDecompress2(tl_tj.handle,
                                  data,
                                  static_cast<unsigned long>(size),
                                  decoded.data(),
                                  decode_w,
                                  decode_w * 4,
                                  decode_h,
                                  TJPF_RGBA,
                                  TJFLAG_FASTDCT) == 0) {
                    if (want_downscale && (decode_w > target_w || decode_h > target_h)) {
                        res.rgba =
                            downsampleRgbaArea(decoded.data(), decode_w, decode_h, target_w, target_h);
                        res.width = target_w;
                        res.height = target_h;
                    } else {
                        res.rgba = std::move(decoded);
                        res.width = decode_w;
                        res.height = decode_h;
                    }
                    res.ok = true;
                    return res;
                }
            }
        }
    }
#endif

    int w = 0, h = 0, channels = 0;
    unsigned char* rgba = stbi_load_from_memory(data, static_cast<int>(size), &w, &h, &channels, 4);
    if (!rgba || w <= 0 || h <= 0 || w > 16384 || h > 16384) {
        if (rgba) stbi_image_free(rgba);
        res.error = "stbi_load_from_memory failed";
        return res;
    }
    int out_w = w;
    int out_h = h;
    if (computeScaledSize(w, h, max_width, max_height, out_w, out_h)) {
        res.rgba = downsampleRgbaArea(rgba, w, h, out_w, out_h);
        stbi_image_free(rgba);
        res.width = out_w;
        res.height = out_h;
    } else {
        const size_t byte_len = static_cast<size_t>(w) * static_cast<size_t>(h) * 4u;
        res.rgba.assign(rgba, rgba + byte_len);
        stbi_image_free(rgba);
        res.width = w;
        res.height = h;
    }
    res.ok = true;
    return res;
}

std::string ImageCache::normalizeImageUrl(const std::string& raw, const std::string& base_url) {
    std::string u = trimAscii(raw);
    if (u.empty()) return {};
    if (u.size() >= 2 && u.front() == '<' && u.back() == '>') {
        u = trimAscii(u.substr(1, u.size() - 2));
    }
    replaceAllInPlace(u, "&amp;", "&");
    replaceAllInPlace(u, "&#38;", "&");
    replaceAllInPlace(u, "&#x26;", "&");
    replaceAllInPlace(u, "&#X26;", "&");
    if (u.find("{w}") != std::string::npos || u.find("{h}") != std::string::npos) {
        replaceAllInPlace(u, "{w}", "500");
        replaceAllInPlace(u, "{h}", "500");
        replaceAllInPlace(u, "{c}", "bb");
        replaceAllInPlace(u, "{f}", "jpg");
    }

    if (startsWithNoCase(u, "data:") || startsWithNoCase(u, "file://")) {
        return u;
    }
    if (startsWithNoCase(u, "http://") || startsWithNoCase(u, "https://")) {
        replaceAllInPlace(u, " ", "%20");
        return u;
    }
    if (u.rfind("//", 0) == 0) {
        std::string scheme = "https";
        if (startsWithNoCase(base_url, "http://")) scheme = "http";
        u = scheme + ":" + u;
        replaceAllInPlace(u, " ", "%20");
        return u;
    }
    if (!u.empty() && u.front() != '/') {
        std::string base_path;
        if (startsWithNoCase(base_url, "file://")) {
            base_path = base_url.substr(7);
            if (startsWithNoCase(base_path, "localhost/")) {
                base_path = base_path.substr(9);
            }
            base_path = percentDecodeAscii(base_path);
        } else if (!base_url.empty() && base_url.front() == '/' &&
                   (base_url.size() == 1 || base_url[1] != '/')) {
            base_path = base_url;
        }
        if (!base_path.empty()) {
            std::error_code ec;
            fs::path base(base_path);
            if (!fs::is_directory(base, ec)) base = base.parent_path();
            const fs::path resolved = (base / fs::path(percentDecodeAscii(u))).lexically_normal();
            return resolved.generic_string();
        }
    }
    if (!base_url.empty() && (startsWithNoCase(base_url, "http://") || startsWithNoCase(base_url, "https://"))) {
        const size_t sep = base_url.find("://");
        const size_t host_start = sep + 3;
        const size_t path_pos = base_url.find_first_of("/?#", host_start);
        const std::string origin = (path_pos == std::string::npos)
                                       ? base_url
                                       : base_url.substr(0, path_pos);
        if (u.front() == '/') {
            u = origin + u;
            replaceAllInPlace(u, " ", "%20");
            return u;
        }
        std::string dir = base_url;
        const size_t q_pos = dir.find_first_of("?#");
        if (q_pos != std::string::npos) dir = dir.substr(0, q_pos);
        const size_t slash = dir.rfind('/');
        if (slash != std::string::npos && slash >= host_start) {
            dir = dir.substr(0, slash + 1);
        } else {
            dir = origin + "/";
        }
        u = dir + u;
        replaceAllInPlace(u, " ", "%20");
        return u;
    }
    return u;
}

void ImageCache::setDomainHeaders(const std::string& domain_suffix,
                                  std::unordered_map<std::string, std::string> headers) {
    std::string key = toLowerAscii(trimAscii(domain_suffix));
    if (!key.empty() && key.front() == '.') key.erase(key.begin());
    if (key.empty()) return;
    std::lock_guard lock(impl_->domain_headers_mutex);
    if (headers.empty()) {
        impl_->domain_headers.erase(key);
    } else {
        impl_->domain_headers[key] = std::move(headers);
    }
}

void ImageCache::clearDomainHeaders(const std::string& domain_suffix) {
    std::string key = toLowerAscii(trimAscii(domain_suffix));
    if (!key.empty() && key.front() == '.') key.erase(key.begin());
    if (key.empty()) return;
    std::lock_guard lock(impl_->domain_headers_mutex);
    impl_->domain_headers.erase(key);
}

std::unordered_map<std::string, std::string> ImageCache::headersForUrl(
    const std::string& url,
    const std::unordered_map<std::string, std::string>& extra) const {
    std::string host;
    extractUrlSchemeAndHost(url, nullptr, &host);

    std::unordered_map<std::string, std::string> merged;
    {
        std::lock_guard lock(impl_->domain_headers_mutex);
        size_t best_len = 0;
        for (const auto& [domain, hdrs] : impl_->domain_headers) {
            if (hostMatchesDomain(host, domain) && domain.size() >= best_len) {
                best_len = domain.size();
                merged = hdrs;
            }
        }
    }
    for (const auto& [k, v] : extra) {
        if (!v.empty()) {
            merged[k] = v;
        }
    }
    return merged;
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
    if (!isSafeCacheKey(key)) return std::nullopt;
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return std::nullopt;

    std::lock_guard lock(impl_->disk_mutex);
    const fs::path target = fs::path(cfg.cache_dir) / (key + ".bin");
    std::error_code ec;
    if (fs::is_symlink(target, ec) || !fs::exists(target, ec) || !fs::is_regular_file(target, ec)) {
        return std::nullopt;
    }
    std::vector<uint8_t> bytes;
    if (!readFileBytes(target, bytes)) {
        return std::nullopt;
    }
    const auto now = fs::file_time_type::clock::now();
    const auto mtime = fs::last_write_time(target, ec);
    if (!ec && (now - mtime) > std::chrono::hours(1)) {
        fs::last_write_time(target, now, ec);
    }
    return bytes;
}

bool ImageCache::putCachedRawBytes(const std::string& key, const uint8_t* data, size_t size) {
    if (!isSafeCacheKey(key) || !data || size == 0) return false;
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return false;

    bool need_evict = false;
    {
        std::lock_guard lock(impl_->disk_mutex);
        std::error_code ec;
        fs::create_directories(cfg.cache_dir, ec);
        const fs::path dir(cfg.cache_dir);
        size_t current_usage = impl_->ensureDiskUsageInitializedLocked(dir);
        const fs::path final_path = dir / (key + ".bin");
        size_t old_size = 0;
        if (fs::exists(final_path, ec) && fs::is_regular_file(final_path, ec)) {
            old_size = static_cast<size_t>(fs::file_size(final_path, ec));
        }
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
        current_usage = (old_size <= current_usage ? (current_usage - old_size) : 0) + size;
        impl_->disk_usage_bytes.store(current_usage, std::memory_order_release);
        need_evict = (current_usage > cfg.max_disk_bytes);
    }
    if (need_evict) {
        evictIfNeeded();
    }
    return true;
}

size_t ImageCache::diskUsageBytes() const {
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return 0;
    std::lock_guard lock(impl_->disk_mutex);
    return impl_->ensureDiskUsageInitializedLocked(fs::path(cfg.cache_dir));
}

size_t ImageCache::evictIfNeeded() {
    const auto cfg = config();
    if (cfg.cache_dir.empty()) return 0;

    if (impl_->disk_usage_initialized.load(std::memory_order_acquire) &&
        impl_->disk_usage_bytes.load(std::memory_order_relaxed) <= cfg.max_disk_bytes) {
        return 0;
    }

    std::lock_guard lock(impl_->disk_mutex);
    const fs::path dir(cfg.cache_dir);
    std::error_code ec;
    if (!fs::exists(dir, ec)) {
        impl_->tracked_cache_dir = cfg.cache_dir;
        impl_->disk_usage_bytes.store(0, std::memory_order_release);
        impl_->disk_usage_initialized.store(true, std::memory_order_release);
        return 0;
    }

    struct DiskEntry {
        fs::path path;
        size_t size = 0;
        fs::file_time_type mtime;
    };

    std::vector<DiskEntry> entries;
    size_t total = 0;
    for (const auto& entry : fs::directory_iterator(dir, ec)) {
        if (!entry.is_regular_file(ec)) continue;
        if (entry.path().extension() != ".bin") continue;
        DiskEntry item;
        item.path = entry.path();
        item.size = static_cast<size_t>(entry.file_size(ec));
        item.mtime = entry.last_write_time(ec);
        total += item.size;
        entries.push_back(std::move(item));
    }

    impl_->tracked_cache_dir = cfg.cache_dir;
    if (total <= cfg.max_disk_bytes) {
        impl_->disk_usage_bytes.store(total, std::memory_order_release);
        impl_->disk_usage_initialized.store(true, std::memory_order_release);
        return 0;
    }

    std::sort(entries.begin(), entries.end(), [](const DiskEntry& a, const DiskEntry& b) {
        return a.mtime < b.mtime;
    });

    // Evict slightly below max_disk_bytes (90% low-water mark) so sequential writes do not rescan.
    const size_t low_water_bytes =
        (cfg.max_disk_bytes >= 64) ? ((cfg.max_disk_bytes * 9u) / 10u) : cfg.max_disk_bytes;

    size_t removed = 0;
    for (const auto& item : entries) {
        if (total <= low_water_bytes) break;
        if (fs::remove(item.path, ec)) {
            total = (item.size <= total) ? (total - item.size) : 0;
            ++removed;
        }
    }
    impl_->disk_usage_bytes.store(total, std::memory_order_release);
    impl_->disk_usage_initialized.store(true, std::memory_order_release);
    return removed;
}

std::shared_ptr<CancelToken> ImageCache::fetchDecoded(
    const std::string& url,
    std::function<void(DecodedImage)> on_done,
    std::unordered_map<std::string, std::string> headers,
    std::shared_ptr<CancelToken> cancel) {
    return fetchDecoded(std::move(url), std::move(on_done), std::move(headers), std::move(cancel), 0, 0);
}

std::shared_ptr<CancelToken> ImageCache::fetchDecoded(
    const std::string& url,
    std::function<void(DecodedImage)> on_done,
    std::unordered_map<std::string, std::string> headers,
    std::shared_ptr<CancelToken> cancel,
    int max_width,
    int max_height) {
    if (!cancel) cancel = CancelToken::create();
    const std::string norm_url = normalizeImageUrl(url);
    if (norm_url.empty() || cancel->isCancelled()) {
        if (on_done && !cancel->isCancelled()) {
            DecodedImage empty;
            empty.error = "empty image url";
            on_done(std::move(empty));
        }
        return cancel;
    }

    // Fast path: inline data:image/... URI
    if (startsWithNoCase(norm_url, "data:")) {
        impl_->submit(cancel, [norm_url, max_width, max_height,
                               on_done = std::move(on_done), cancel]() {
            if (cancel->isCancelled()) return;
            std::vector<uint8_t> bytes = decodeDataUriBytes(norm_url);
            DecodedImage res;
            if (bytes.empty()) {
                res.error = "empty or invalid data URI";
            } else {
                res = decodeBytes(bytes.data(), bytes.size(), max_width, max_height);
            }
            if (!cancel->isCancelled() && on_done) on_done(std::move(res));
        });
        return cancel;
    }

    // Local file:// URI or filesystem absolute path (must NOT match //protocol-relative)
    const bool is_file_uri = startsWithNoCase(norm_url, "file://");
    const bool is_abs_path = !norm_url.empty() && norm_url[0] == '/' &&
                             (norm_url.size() == 1 || norm_url[1] != '/');
    if (is_file_uri || is_abs_path) {
        const std::string raw_key = "file:" + norm_url;
        const std::string mem_key = makeMemCacheKey(raw_key, max_width, max_height);
        if (auto mem_hit = impl_->getMemCached(mem_key)) {
            impl_->submit(cancel, [img = std::move(*mem_hit), on_done = std::move(on_done), cancel]() mutable {
                if (!cancel->isCancelled() && on_done) on_done(std::move(img));
            });
            return cancel;
        }

        std::shared_ptr<Impl::InFlightFetch> inflight;
        {
            std::lock_guard lk(impl_->inflight_mutex);
            auto it = impl_->inflight_map.find(raw_key);
            if (it != impl_->inflight_map.end() && it->second) {
                if (!it->second->shared_cancel->isCancelled()) {
                    it->second->waiters.push_back(
                        Impl::CoalescedWaiter{cancel, max_width, max_height, std::move(on_done)});
                    return cancel;
                }
                impl_->inflight_map.erase(it);
            }
            inflight = std::make_shared<Impl::InFlightFetch>();
            inflight->waiters.push_back(
                Impl::CoalescedWaiter{cancel, max_width, max_height, std::move(on_done)});
            impl_->inflight_map[raw_key] = inflight;
        }

        std::string path_str = norm_url;
        if (is_file_uri) {
            std::string raw_file = norm_url.substr(7);
            if (startsWithNoCase(raw_file, "localhost/")) {
                raw_file = raw_file.substr(9);
            }
            path_str = percentDecodeAscii(raw_file);
        }
        if (!impl_->submit(inflight->shared_cancel, [this, raw_key, path_str, inflight]() {
                std::vector<uint8_t> bytes;
                const bool ok = !inflight->shared_cancel->isCancelled() && readFileBytes(path_str, bytes);
                const std::string err = ok ? "" : ("failed to open local image: " + path_str);
                impl_->completeCoalescedFetch(raw_key, inflight, ok, bytes, err);
            })) {
            std::lock_guard lk(impl_->inflight_mutex);
            impl_->inflight_map.erase(raw_key);
        }
        return cancel;
    }

    auto eff_headers = headersForUrl(norm_url, headers);
    const std::string raw_key = cacheKeyFor(norm_url, eff_headers);
    const std::string mem_key = makeMemCacheKey(raw_key, max_width, max_height);
    if (auto mem_hit = impl_->getMemCached(mem_key)) {
        impl_->submit(cancel, [img = std::move(*mem_hit), on_done = std::move(on_done), cancel]() mutable {
            if (!cancel->isCancelled() && on_done) on_done(std::move(img));
        });
        return cancel;
    }

    // In-flight coalescing: merge concurrent identical URL + header requests into a single transfer.
    std::shared_ptr<Impl::InFlightFetch> inflight;
    {
        std::lock_guard lk(impl_->inflight_mutex);
        auto it = impl_->inflight_map.find(raw_key);
        if (it != impl_->inflight_map.end() && it->second) {
            if (!it->second->shared_cancel->isCancelled()) {
                it->second->waiters.push_back(
                    Impl::CoalescedWaiter{cancel, max_width, max_height, std::move(on_done)});
                return cancel;
            }
            impl_->inflight_map.erase(it);
        }
        inflight = std::make_shared<Impl::InFlightFetch>();
        inflight->waiters.push_back(
            Impl::CoalescedWaiter{cancel, max_width, max_height, std::move(on_done)});
        impl_->inflight_map[raw_key] = inflight;
    }

    if (!impl_->submit(inflight->shared_cancel,
                       [this, norm_url, raw_key, eff_headers = std::move(eff_headers), inflight]() mutable {
        if (inflight->shared_cancel->isCancelled()) {
            impl_->completeCoalescedFetch(raw_key, inflight, false, {}, "cancelled");
            return;
        }

        if (auto cached = getCachedRawBytes(raw_key)) {
            impl_->completeCoalescedFetch(raw_key, inflight, true, *cached, "");
            return;
        }

        const auto cfg = config();
        ensureCurlGlobalInit();

        auto hasHeaderCI = [&](const std::string& target) {
            for (const auto& [k, _] : eff_headers) {
                if (toLowerAscii(k) == target) return true;
            }
            return false;
        };
        if (!hasHeaderCI("referer")) {
            std::string ref = inferDefaultReferer(norm_url);
            if (!ref.empty()) eff_headers["Referer"] = std::move(ref);
        }
        if (!hasHeaderCI("accept")) {
            eff_headers["Accept"] = "image/webp,image/png,image/jpeg,image/svg+xml,image/*,*/*;q=0.8";
        }
        std::string user_agent =
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
        for (const auto& [k, v] : eff_headers) {
            if (toLowerAscii(k) == "user-agent" && !v.empty()) {
                user_agent = v;
                break;
            }
        }

        Impl::ProgressCtx progress_ctx{impl_.get(), inflight.get()};

        auto doFetch = [&](const std::string& target_url,
                           std::vector<uint8_t>& out,
                           long& http_code,
                           std::string& curl_err) -> CURLcode {
            out.clear();
            http_code = 0;
            curl_err.clear();

            bool temp_handle = false;
            CURL* curl = g_worker_curl;
            if (curl) {
                curl_easy_reset(curl);
            } else {
                curl = curl_easy_init();
                temp_handle = true;
            }
            if (!curl) {
                curl_err = "curl_easy_init failed";
                return CURLE_FAILED_INIT;
            }

            struct curl_slist* header_list = nullptr;
            for (const auto& [k, v] : eff_headers) {
                if (toLowerAscii(k) == "user-agent") continue;
                std::string h = k + ": " + v;
                header_list = curl_slist_append(header_list, h.c_str());
            }

            CurlWriteBuf wb{&out};
            if (impl_->curl_share) {
                curl_easy_setopt(curl, CURLOPT_SHARE, impl_->curl_share);
            }
            std::string target_host;
            extractUrlSchemeAndHost(target_url, nullptr, &target_host);
            if (isPrivateOrLocalHost(target_host)) {
                curl_easy_setopt(curl, CURLOPT_NOPROXY, "*");
            }
            curl_easy_setopt(curl, CURLOPT_URL, target_url.c_str());
            curl_easy_setopt(curl, CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_2TLS);
            curl_easy_setopt(curl, CURLOPT_TCP_KEEPALIVE, 1L);
            curl_easy_setopt(curl, CURLOPT_TCP_KEEPIDLE, 30L);
            curl_easy_setopt(curl, CURLOPT_TCP_KEEPINTVL, 15L);
            curl_easy_setopt(curl, CURLOPT_DNS_CACHE_TIMEOUT, 300L);
            curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
            curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
            curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 8L);
            curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
            curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, static_cast<long>(cfg.request_timeout_ms));
            curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
            curl_easy_setopt(curl, CURLOPT_HAPPY_EYEBALLS_TIMEOUT_MS, 250L);
            curl_easy_setopt(curl, CURLOPT_USERAGENT, user_agent.c_str());
            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteCallback);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, &wb);
            curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
            curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, &Impl::curlCoalescedProgressCb);
            curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &progress_ctx);
            if (header_list) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, header_list);

            CURLcode code = curl_easy_perform(curl);
            curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
            if (code != CURLE_OK) curl_err = curl_easy_strerror(code);

            // Detach stack pointers before reusing handle on next task.
            curl_easy_setopt(curl, CURLOPT_HTTPHEADER, nullptr);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, nullptr);
            curl_easy_setopt(curl, CURLOPT_XFERINFODATA, nullptr);

            if (header_list) curl_slist_free_all(header_list);
            if (temp_handle) curl_easy_cleanup(curl);
            return code;
        };

        std::vector<uint8_t> downloaded;
        long http_code = 0;
        std::string curl_err;
        CURLcode code = doFetch(norm_url, downloaded, http_code, curl_err);

        // Retry http:// -> https:// for non-LAN hosts if connection or 403/404 failed
        if (!inflight->shared_cancel->isCancelled() &&
            startsWithNoCase(norm_url, "http://") &&
            (code != CURLE_OK || http_code == 403 || http_code == 404 || downloaded.empty())) {
            std::string host;
            extractUrlSchemeAndHost(norm_url, nullptr, &host);
            if (!isPrivateOrLocalHost(host)) {
                std::string https_url = "https://" + norm_url.substr(7);
                std::vector<uint8_t> https_bytes;
                long https_code = 0;
                std::string https_err;
                CURLcode code2 = doFetch(https_url, https_bytes, https_code, https_err);
                if (code2 == CURLE_OK && https_code >= 200 && https_code < 300 && !https_bytes.empty()) {
                    downloaded = std::move(https_bytes);
                    http_code = https_code;
                    curl_err.clear();
                    code = code2;
                }
            }
        }

        if (inflight->shared_cancel->isCancelled()) {
            impl_->completeCoalescedFetch(raw_key, inflight, false, {}, "cancelled");
            return;
        }

        if (code != CURLE_OK) {
            impl_->completeCoalescedFetch(raw_key, inflight, false, {}, "http fetch failed: " + curl_err);
            return;
        }
        if (http_code < 200 || http_code >= 300 || downloaded.empty()) {
            impl_->completeCoalescedFetch(
                raw_key, inflight, false, {}, "unexpected HTTP status: " + std::to_string(http_code));
            return;
        }

        putCachedRawBytes(raw_key, downloaded.data(), downloaded.size());
        impl_->completeCoalescedFetch(raw_key, inflight, true, downloaded, "");
    })) {
        std::lock_guard lk(impl_->inflight_mutex);
        impl_->inflight_map.erase(raw_key);
    }

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
    setImageUrl(url, std::move(headers), 0, 0);
}

void CachedImageView::setImageUrl(const std::string& url,
                                  std::unordered_map<std::string, std::string> headers,
                                  int max_width,
                                  int max_height) {
    if (max_width <= 0 && max_height <= 0) {
        // Automatically infer decode target size from widget Yoga dimensions + HiDPI scale
        // so card thumbnails do not decode or upload multi-megabyte full-resolution RGBA textures.
        YGNodeRef yn = getYGNode();
        float pt_w = 0.0f;
        float pt_h = 0.0f;
        if (yn) {
            const YGValue yw = YGNodeStyleGetWidth(yn);
            const YGValue ymw = YGNodeStyleGetMaxWidth(yn);
            const YGValue yh = YGNodeStyleGetHeight(yn);
            const YGValue ymh = YGNodeStyleGetMaxHeight(yn);
            if (yw.unit == YGUnitPoint && yw.value > 0.0f) pt_w = yw.value;
            else if (ymw.unit == YGUnitPoint && ymw.value > 0.0f) pt_w = ymw.value;
            if (yh.unit == YGUnitPoint && yh.value > 0.0f) pt_h = yh.value;
            else if (ymh.unit == YGUnitPoint && ymh.value > 0.0f) pt_h = ymh.value;
        }
        if (pt_w <= 0.0f && getWidth() > 1.0f) pt_w = getWidth();
        if (pt_h <= 0.0f && getHeight() > 1.0f) pt_h = getHeight();

        const float dpr = std::clamp(
            brls::Application::windowScale > 0.5f ? brls::Application::windowScale : 2.0f,
            2.0f, 3.0f);
        if (pt_w > 0.0f && pt_h > 0.0f) {
            if (getScalingType() == brls::ImageScalingType::FILL) {
                const int bound = quantizeDimHint(
                    std::max(64, static_cast<int>(std::ceil(std::max(pt_w, pt_h) * dpr))));
                max_width = bound;
                max_height = bound;
            } else {
                max_width = quantizeDimHint(std::max(64, static_cast<int>(std::ceil(pt_w * dpr))));
                max_height = quantizeDimHint(std::max(64, static_cast<int>(std::ceil(pt_h * dpr))));
            }
        } else if (pt_h > 0.0f) {
            max_width = 1600;
            max_height = quantizeDimHint(std::max(128, static_cast<int>(std::ceil(pt_h * dpr * 1.5f))));
        } else {
            max_width = 1920;
            max_height = 1920;
        }
    }

    if (url == current_url_ &&
        max_width <= current_max_w_ &&
        max_height <= current_max_h_ &&
        getTexture() > 0) {
        return;
    }
    cancelPending();
    current_url_ = url;
    current_max_w_ = max_width;
    current_max_h_ = max_height;
    if (url.empty()) {
        clear();
        return;
    }

    auto alive = alive_;
    pending_cancel_ = cache_->fetchDecoded(
        url,
        [this, alive, url](DecodedImage img) {
            if (!img.ok || img.rgba.empty() || img.width <= 0 || img.height <= 0) return;
            const bool need_wake = !g_ui_wake_pending.exchange(true, std::memory_order_acq_rel);
            brls::sync([this, alive, url, img = std::move(img)]() {
                g_ui_wake_pending.store(false, std::memory_order_release);
                brls::Application::setActiveEvent(true);
                if (!alive->load() || url != current_url_) return;
                auto* plat = brls::Application::getPlatform();
                if (!plat || !plat->getVideoContext()) return;
                NVGcontext* vg = plat->getVideoContext()->getNVGContext();
                if (!vg) return;
                int tex = nvgCreateImageRGBA(vg, img.width, img.height, 0, img.rgba.data());
                if (tex > 0) {
                    innerSetImage(tex);
                }
            });
            if (need_wake) {
                wakeSdlEventLoopOnce();
            }
        },
        std::move(headers),
        nullptr,
        max_width,
        max_height);
}

}  // namespace omnidesk
