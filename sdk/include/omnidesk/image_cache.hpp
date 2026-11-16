#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include <borealis/views/image.hpp>

#include "omnidesk/async.hpp"
#include "omnidesk/export.h"

namespace omnidesk {

struct OMNIDESK_API ImageCacheConfig {
    std::string cache_dir = "/tmp/omnidesk-imgcache";
    size_t max_disk_bytes = 256ull * 1024ull * 1024ull;
    int worker_threads = 4;
    int64_t request_timeout_ms = 12000;
};

struct OMNIDESK_API DecodedImage {
    bool ok = false;
    int width = 0;
    int height = 0;
    std::vector<uint8_t> rgba;
    std::string error;
};

/// Asynchronous HTTP + local image loader backed by an atomic persistent disk LRU cache.
/// Decodes WebP (libwebp) as well as JPG/PNG/BMP/GIF (stb_image) off the UI render thread.
class OMNIDESK_API ImageCache {
public:
    static ImageCache& instance();

    explicit ImageCache(ImageCacheConfig cfg = {});
    ~ImageCache();

    ImageCache(const ImageCache&) = delete;
    ImageCache& operator=(const ImageCache&) = delete;

    void configure(const ImageCacheConfig& cfg);
    ImageCacheConfig config() const;
    void shutdown();

    static DecodedImage decodeBytes(const uint8_t* data, size_t size);
    static DecodedImage decodeBytes(const uint8_t* data, size_t size, int max_width, int max_height);
    static std::string normalizeImageUrl(const std::string& raw, const std::string& base_url = "");
    static std::string cacheKeyFor(const std::string& url,
                                   const std::unordered_map<std::string, std::string>& headers = {});

    void setDomainHeaders(const std::string& domain_suffix,
                          std::unordered_map<std::string, std::string> headers);
    void clearDomainHeaders(const std::string& domain_suffix);
    std::unordered_map<std::string, std::string> headersForUrl(
        const std::string& url,
        const std::unordered_map<std::string, std::string>& extra = {}) const;

    std::optional<std::vector<uint8_t>> getCachedRawBytes(const std::string& key);
    bool putCachedRawBytes(const std::string& key, const uint8_t* data, size_t size);
    size_t evictIfNeeded();
    size_t diskUsageBytes() const;

    std::shared_ptr<CancelToken> fetchDecoded(
        const std::string& url,
        std::function<void(DecodedImage)> on_done,
        std::unordered_map<std::string, std::string> headers = {},
        std::shared_ptr<CancelToken> cancel = nullptr);

    std::shared_ptr<CancelToken> fetchDecoded(
        const std::string& url,
        std::function<void(DecodedImage)> on_done,
        std::unordered_map<std::string, std::string> headers,
        std::shared_ptr<CancelToken> cancel,
        int max_width,
        int max_height);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

/// Drop-in Borealis Image widget backed by ImageCache with automatic request cancellation
/// when reassigned or destroyed.
class OMNIDESK_API CachedImageView : public brls::Image {
public:
    explicit CachedImageView(ImageCache* cache = nullptr);
    ~CachedImageView() override;

    void setImageUrl(const std::string& url,
                     std::unordered_map<std::string, std::string> headers = {});
    void setImageUrl(const std::string& url,
                     std::unordered_map<std::string, std::string> headers,
                     int max_width,
                     int max_height);
    void cancelPending();

private:
    ImageCache* cache_ = nullptr;
    std::string current_url_;
    int current_max_w_ = 0;
    int current_max_h_ = 0;
    std::shared_ptr<CancelToken> pending_cancel_;
    std::shared_ptr<std::atomic<bool>> alive_;
};

}  // namespace omnidesk
