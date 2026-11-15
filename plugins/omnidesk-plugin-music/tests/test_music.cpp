#include "music/adam_client.hpp"
#include "music/config.hpp"
#include "music/llm_translator.hpp"
#include "music/lrc_parser.hpp"
#include "music/lrclib_client.hpp"
#include "music/lyrics_cache.hpp"
#include "music/music_engine.hpp"

#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <cstring>
#include <dlfcn.h>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <netinet/in.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>
#include <omnidesk/capabilities.hpp>
#include <omnidesk/config_store.hpp>
#include <omnidesk/domain_registry.hpp>
#include <omnidesk/entity_bus.hpp>
#include <omnidesk/plugin.hpp>
#include <omnidesk/plugin_abi.h>
#include <poll.h>
#include <sstream>
#include <csignal>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <unordered_map>
#include <unordered_set>

namespace fs = std::filesystem;

#define REQUIRE(cond)                                                                      \
    do {                                                                                   \
        if (!(cond)) {                                                                     \
            std::cerr << "FAILED at " << __FILE__ << ":" << __LINE__ << ": " << #cond << "\n"; \
            return 1;                                                                      \
        }                                                                                  \
    } while (0)

namespace {

class MockAdamTlsServer {
public:
    MockAdamTlsServer() = default;
    ~MockAdamTlsServer() { stop(); }

    bool start() {
        sandbox_blocked_ = false;
        EVP_PKEY* pkey = EVP_RSA_gen(2048);
        if (!pkey) { std::cerr << "EVP_RSA_gen failed\n"; return false; }

        X509* x509 = X509_new();
        X509_set_version(x509, 2);
        ASN1_INTEGER_set(X509_get_serialNumber(x509), 1);
        X509_gmtime_adj(X509_getm_notBefore(x509), -60);
        X509_gmtime_adj(X509_getm_notAfter(x509), 3600);
        X509_set_pubkey(x509, pkey);
        X509_NAME* name = X509_NAME_new();
        X509_NAME_add_entry_by_txt(name, "CN", MBSTRING_ASC,
                                   reinterpret_cast<const unsigned char*>("127.0.0.1"), -1, -1, 0);
        X509_set_subject_name(x509, name);
        X509_set_issuer_name(x509, name);
        X509_NAME_free(name);

        X509V3_CTX ctx;
        X509V3_set_ctx_nodb(&ctx);
        X509V3_set_ctx(&ctx, x509, x509, nullptr, nullptr, 0);
        X509_EXTENSION* ext = X509V3_EXT_conf_nid(nullptr, &ctx, NID_subject_alt_name,
                                                  const_cast<char*>("IP:127.0.0.1"));
        if (ext) {
            X509_add_ext(x509, ext, -1);
            X509_EXTENSION_free(ext);
        }
        X509_sign(x509, pkey, EVP_sha256());

        ssl_ctx_ = SSL_CTX_new(TLS_server_method());
        if (!ssl_ctx_) { std::cerr << "SSL_CTX_new failed\n"; X509_free(x509); EVP_PKEY_free(pkey); return false; }
        SSL_CTX_use_certificate(ssl_ctx_, x509);
        SSL_CTX_use_PrivateKey(ssl_ctx_, pkey);
        X509_free(x509);
        EVP_PKEY_free(pkey);

        listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        if (listen_fd_ < 0) {
            if (errno == EPERM || errno == EACCES) sandbox_blocked_ = true;
            std::perror("socket");
            return false;
        }
        int opt = 1;
        ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(0);
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 || ::listen(listen_fd_, 8) != 0) { std::perror("bind/listen");
            ::close(listen_fd_);
            listen_fd_ = -1;
            return false;
        }
        socklen_t len = sizeof(addr);
        ::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&addr), &len);
        port_ = ntohs(addr.sin_port);

        running_ = true;
        th_ = std::thread([this]() { serveLoop(); });
        return true;
    }

    void stop() {
        running_ = false;
        if (listen_fd_ >= 0) {
            ::shutdown(listen_fd_, SHUT_RDWR);
            ::close(listen_fd_);
            listen_fd_ = -1;
        }
        if (th_.joinable()) th_.join();
        if (ssl_ctx_) {
            SSL_CTX_free(ssl_ctx_);
            ssl_ctx_ = nullptr;
        }
    }

    int port() const { return port_; }
    bool sandboxBlocked() const { return sandbox_blocked_; }

private:
    void serveLoop() {
        while (running_) {
            struct pollfd pfd{listen_fd_, POLLIN, 0};
            if (::poll(&pfd, 1, 100) <= 0) continue;
            int cfd = ::accept(listen_fd_, nullptr, nullptr);
            if (cfd < 0) continue;
            SSL* ssl = SSL_new(ssl_ctx_);
            SSL_set_fd(ssl, cfd);
            if (SSL_accept(ssl) == 1) {
                char buf[8192];
                int n = SSL_read(ssl, buf, sizeof(buf) - 1);
                if (n > 0) {
                    buf[n] = '\0';
                    std::string req(buf, static_cast<size_t>(n));
                    std::istringstream iss(req);
                    std::string method, path;
                    iss >> method >> path;

                    int code = 404;
                    std::string ctype = "application/json";
                    std::string body = R"({"error":"not found"})";

                    if (path == "/api/v1/drm/status") {
                        code = 200;
                        body = nlohmann::json({
                            {"available", true},
                            {"backend", {{"selected", "adamplay"}}},
                            {"state", {
                                {"manager", "ready"},
                                {"process", "running"},
                                {"authentication", auth_state_},
                                {"fairplay", fairplay_state_},
                                {"session", "valid"},
                            }},
                            {"capabilities", {
                                {"cbcs", true}, {"alac", true}, {"hiRes", true}, {"atmos", false}
                            }},
                        }).dump();
                    } else if (method == "POST" && path == "/api/v1/drm/authenticate") {
                        auth_state_ = "challenging";
                        code = 202;
                        body = nlohmann::json({
                            {"available", true},
                            {"state", {{"authentication", auth_state_}, {"fairplay", "unknown"}}},
                            {"challenge", {{"prompt", "Enter 6-digit code"}}},
                        }).dump();
                    } else if (method == "POST" && path == "/api/v1/drm/challenge") {
                        auth_state_ = "logged_in";
                        fairplay_state_ = "ready";
                        code = 200;
                        body = nlohmann::json({
                            {"available", true},
                            {"state", {{"authentication", auth_state_}, {"fairplay", fairplay_state_}}},
                            {"capabilities", {{"cbcs", true}, {"alac", true}, {"hiRes", true}}},
                        }).dump();
                    } else if (method == "POST" && path == "/api/v1/library/token") {
                        code = 200;
                        body = R"({"ok":true})";
                    } else if (method == "POST" && path == "/api/v1/playback") {
                        code = 201;
                        body = nlohmann::json({
                            {"sessionId", "sess-mock-1"},
                            {"assetId", "1440857781"},
                            {"storefront", "us"},
                            {"type", "song"},
                            {"codec", "alac"},
                            {"sampleRate", 192000},
                            {"bitDepth", 24},
                            {"channelCount", 2},
                            {"title", "Midnight City"},
                            {"artistName", "M83"},
                            {"albumName", "Hurry Up, We're Dreaming"},
                            {"durationMs", 243000},
                            {"streams", {{"audio", "/api/v1/playback/sess-mock-1/audio"}}},
                        }).dump();
                    } else if (method == "DELETE" && path == "/api/v1/playback/sess-mock-1") {
                        code = 200;
                        body = R"({"ok":true})";
                    } else if (path.rfind("/api/v1/lyrics/1440857781", 0) == 0) {
                        code = 200;
                        ctype = "text/plain; charset=utf-8";
                        body = "[00:12.00]Waiting in a car\n[00:18.50]Waiting for a ride in the dark\n";
                    } else if (path == "/api/v1/library/playlists") {
                        code = 200;
                        body = R"({"playlists":[{"lid":"p.123","name":"Lossless Favorites","trackCount":12}]})";
                    } else if (path.rfind("/api/v1/recommendations", 0) == 0) {
                        code = 200;
                        body = nlohmann::json({
                            {"data", {{
                                {"id", "rec-1"},
                                {"type", "personal-recommendation"},
                                {"relationships", {
                                    {"contents", {
                                        {"data", {{
                                            {"id", "1440857781"},
                                            {"type", "songs"},
                                            {"attributes", {
                                                {"name", "Midnight City"},
                                                {"artistName", "M83"},
                                                {"albumName", "Hurry Up, We're Dreaming"},
                                                {"durationInMillis", 243000},
                                                {"hasLyrics", true},
                                                {"audioTraits", {"lossless", "hi-res-lossless"}},
                                                {"artwork", {{"url", "https://example.com/{w}x{h}.jpg"}}},
                                            }},
                                        }}}
                                    }}
                                }}
                            }}}
                        }).dump();
                    }

                    std::ostringstream resp;
                    resp << "HTTP/1.1 " << code << " OK\r\n"
                         << "Content-Type: " << ctype << "\r\n"
                         << "Content-Length: " << body.size() << "\r\n"
                         << "Connection: close\r\n\r\n"
                         << body;
                    std::string wire = resp.str();
                    SSL_write(ssl, wire.data(), static_cast<int>(wire.size()));
                }
            }
            SSL_shutdown(ssl);
            SSL_free(ssl);
            ::close(cfd);
        }
    }

    std::atomic<bool> running_{false};
    int listen_fd_ = -1;
    int port_ = 0;
    bool sandbox_blocked_ = false;
    SSL_CTX* ssl_ctx_ = nullptr;
    std::thread th_;
    std::string auth_state_ = "logged_out";
    std::string fairplay_state_ = "unknown";
};

int testLrcAndBilingualParser() {
    std::string lrc =
        "[ti:Test]\n"
        "[offset:500]\n"
        "[00:10.00]Under silver moonlight\n"
        "[00:10.00]在银色月光下\n"
        "[00:20.250][00:40.000]<00:20.50>Echoes fade away\n";
    auto lines = music_plugin::parseLyricsText(lrc);
    REQUIRE(lines.size() == 3);
    REQUIRE(lines[0].has_time);
    REQUIRE(std::abs(lines[0].time_sec - 10.5) < 0.01);
    REQUIRE(lines[0].original == "Under silver moonlight");
    REQUIRE(lines[0].translation == "在银色月光下");
    REQUIRE(lines[1].original == "Echoes fade away");
    REQUIRE(std::abs(lines[1].time_sec - 20.75) < 0.01);
    REQUIRE(std::abs(lines[2].time_sec - 40.5) < 0.01);

    std::string composed = music_plugin::composeLrc(lines, "bilingual");
    REQUIRE(composed.find("[00:10.500]Under silver moonlight") != std::string::npos);
    REQUIRE(composed.find("[00:10.500]在银色月光下") != std::string::npos);

    REQUIRE(music_plugin::containsChineseOnlyScript("夜风吹过街角"));
    REQUIRE(!music_plugin::containsChineseOnlyScript("東京タワー"));
    return 0;
}

int testLrclibExactMatch() {
    music_plugin::MusicTrack tr;
    tr.title = "Midnight City";
    tr.artist = "M83";
    tr.album = "Hurry Up, We're Dreaming";
    tr.duration_sec = 243.0;

    nlohmann::json good = {
        {"trackName", "midnight city"},
        {"artistName", "M83"},
        {"albumName", "Hurry Up, We're Dreaming"},
        {"duration", 244.0},
    };
    REQUIRE(music_plugin::LrclibClient::isExactMatch(tr, good));

    nlohmann::json bad_dur = good;
    bad_dur["duration"] = 250.0;
    REQUIRE(!music_plugin::LrclibClient::isExactMatch(tr, bad_dur));
    return 0;
}

int testMockAdamAndLlmPipeline(const fs::path& tmp) {
    MockAdamTlsServer mock;
    if (!mock.start()) {
        if (mock.sandboxBlocked()) {
            std::cout << "[SKIP] MockAdamTlsServer skipped in seccomp sandbox\n";
            return 0;
        }
        REQUIRE(false);
    }

    omnidesk::CapabilityRegistry caps;
    omnidesk::EntityBus bus;
    omnidesk::DomainRegistry domains;
    omnidesk::ConfigStore cfg_store((tmp / "cfg").string());
    omnidesk::SecretVault vault((tmp / "cfg").string());

    cfg_store.savePluginConfig("plugin-music", {
        {"engine_port", mock.port()},
        {"auto_launch_engine", false},
        {"enable_lrclib", false},
        {"auto_translate_lyrics", true},
        {"storefront", "us"},
    });

    // Mock llm.chat.v1 provider
    caps.registerCapability("llm.chat.v1", "plugin-mock-llm",
        [](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            omnidesk::CapabilityResult r;
            r.ok = true;
            std::string prompt = req.value("prompt", "");
            if (prompt.find("精讲") != std::string::npos) {
                r.data = {{"reply", "1. 意译：在车里等待。\n2. 词组：waiting for 表示等待。"}};
                return r;
            }
            size_t arr_pos = prompt.find('[');
            auto arr = nlohmann::json::parse(prompt.substr(arr_pos), nullptr, false);
            nlohmann::json out = nlohmann::json::array();
            for (const auto& item : arr) {
                std::string txt = item.value("text", "");
                std::string tr = (txt.find("car") != std::string::npos)
                                     ? "在车里静静等待"
                                     : "在黑夜中等候一程";
                out.push_back({{"id", item["id"]}, {"text", tr}});
            }
            r.data = {{"reply", out.dump()}};
            return r;
        });

    omnidesk::PluginContext ctx("plugin-music", tmp.string(), (tmp / "data").string(),
                                caps, bus, domains, cfg_store, vault);
    music_plugin::MusicEngine engine(ctx);
    engine.initializeFast();
    engine.adamClient().setMediaUserToken("mock-mut");

    auto drm0 = engine.refreshDrmStatus(false);
    REQUIRE(drm0.engine_reachable);
    REQUIRE(drm0.auth_state == "logged_out");

    std::string err;
    music_plugin::DrmStatus drm1;
    REQUIRE(engine.authenticateAppleId("user@example.com", "secret", drm1, err));
    REQUIRE(drm1.auth_state == "challenging");

    music_plugin::DrmStatus drm2;
    REQUIRE(engine.submitTwoFactorCode("123456", drm2, err));
    REQUIRE(drm2.auth_state == "logged_in");
    REQUIRE(drm2.fairplay_state == "ready");
    REQUIRE(drm2.cap_hi_res);

    auto feed = engine.loadFeed("recommendations", "", true);
    REQUIRE(feed.ok);
    REQUIRE(feed.tracks.size() == 1);
    REQUIRE(feed.tracks[0].id == "1440857781");
    REQUIRE(feed.tracks[0].is_hires);

    auto pl_feed = engine.loadFeed("library-playlists", "", true);
    REQUIRE(pl_feed.ok);
    REQUIRE(pl_feed.containers.size() == 1);
    REQUIRE(pl_feed.containers[0].name == "Lossless Favorites");

    auto sess = engine.adamClient().createPlaybackSession("1440857781", "us", true, true, false, false);
    REQUIRE(sess.ok);
    REQUIRE(sess.codec == "alac");
    REQUIRE(sess.sample_rate == 192000);
    REQUIRE(sess.bit_depth == 24);
    REQUIRE(sess.formattedQuality() == "ALAC 24bit/192kHz");
    REQUIRE(engine.adamClient().deletePlaybackSession(sess.session_id));

    auto lyr = engine.getLyricsForTrack(feed.tracks[0], false, true, 0);
    REQUIRE(lyr.source == "adam");
    REQUIRE(lyr.lines.size() == 2);
    REQUIRE(lyr.translated);
    REQUIRE(lyr.lines[0].translation == "在车里静静等待");
    REQUIRE(fs::exists(lyr.bilingual_lrc_path));

    // Verify media.lyrics.v1 explain RPC
    auto exp = engine.handleLyricsRpc({
        {"action", "explain"},
        {"original", "Waiting for a ride in the dark"},
        {"translation", "在黑夜中等候一程"},
        {"focus_phrase", "in the dark"},
    });
    REQUIRE(exp.ok);
    REQUIRE(!exp.data.value("degraded", true));
    REQUIRE(exp.data.value("explanation", "").find("waiting for") != std::string::npos);

    engine.shutdown();
    return 0;
}

int testDlopenPluginLifecycle(const std::string& so_path, const fs::path& tmp) {
    void* h = dlopen(so_path.c_str(), RTLD_NOW | RTLD_LOCAL);
    REQUIRE(h != nullptr);
    auto info_fn = reinterpret_cast<OmniDeskAbiInfoFn>(dlsym(h, "omnidesk_plugin_abi_info"));
    auto create_fn = reinterpret_cast<OmniDeskPluginCreateFn>(dlsym(h, "omnidesk_plugin_create"));
    auto destroy_fn = reinterpret_cast<OmniDeskPluginDestroyFn>(dlsym(h, "omnidesk_plugin_destroy"));
    REQUIRE(info_fn && create_fn && destroy_fn);

    const OmniDeskPluginInfo* info = info_fn();
    REQUIRE(info && std::string(info->id) == "plugin-music");
    REQUIRE(std::string(info->sdk_abi_signature) == omnidesk::sdkAbiSignature());

    auto* plugin = static_cast<omnidesk::IPlugin*>(create_fn());
    REQUIRE(plugin != nullptr);

    {
        omnidesk::CapabilityRegistry caps;
        omnidesk::EntityBus bus;
        omnidesk::DomainRegistry domains;
        omnidesk::ConfigStore cfg_store((tmp / "dl_cfg").string());
        omnidesk::SecretVault vault((tmp / "dl_cfg").string());
        cfg_store.savePluginConfig("plugin-music", {
            {"auto_launch_engine", false},
            {"engine_port", 29998},
        });
        omnidesk::PluginContext ctx("plugin-music", tmp.string(), (tmp / "dl_data").string(),
                                    caps, bus, domains, cfg_store, vault);

        REQUIRE(plugin->onLoad(ctx));
        REQUIRE(caps.has("media.source.v1"));
        REQUIRE(caps.has("media.lyrics.v1"));
        REQUIRE(caps.has("media.playlist.v1"));
        REQUIRE(!domains.mediaSources().empty());
        REQUIRE(!domains.workspaceTabs().empty());
        REQUIRE(!domains.dockWidgets(omnidesk::ContentDomain::Media).empty());

        auto st_res = caps.call("media.lyrics.v1", {{"action", "status"}});
        REQUIRE(st_res.ok);
        REQUIRE(st_res.data.value("initialized", false));

        auto hc = plugin->onHealthCheck();
        REQUIRE(hc.value("ok", false));

        // Test entity.media.open from plugin-ai-curator (Daily 5 & interactive items)
        // Wait briefly or check active track via media.lyrics.v1 status
        bus.publish("entity.media.open", {
            {"id", "yt-abc123"},
            {"source_id", "plugin-ai-curator"},
            {"kind", "video"},
            {"title", "Ignored YouTube Video"},
            {"origin", "host_workbench"},
        });
        auto st_after_yt = caps.call("media.lyrics.v1", {{"action", "status"}});
        REQUIRE(st_after_yt.ok);
        REQUIRE(st_after_yt.data.value("active_track_id", "") == "");

        bus.publish("entity.media.open", {
            {"id", "am-cn-1643009587"},
            {"source_id", "plugin-ai-curator"},
            {"kind", "music"},
            {"title", "Muerte"},
            {"origin", "host_workbench"},
            {"extra", {
                {"apple_music_song_id", "1643009587"},
                {"storefront", "cn"},
            }},
        });
        auto st_after_am = caps.call("media.lyrics.v1", {{"action", "status"}});
        REQUIRE(st_after_am.ok);
        REQUIRE(st_after_am.data.value("active_track_id", "") == "1643009587");

        bus.publish("entity.media.open", {
            {"id", "ix-am-1833283533"},
            {"source_id", "plugin-ai-curator"},
            {"kind", "music"},
            {"title", "Tobiume (2025 Remaster)"},
            {"origin", "host_workbench"},
        });
        auto st_after_ix = caps.call("media.lyrics.v1", {{"action", "status"}});
        REQUIRE(st_after_ix.ok);
        REQUIRE(st_after_ix.data.value("active_track_id", "") == "1833283533");

        plugin->onUnload();
        REQUIRE(!caps.has("media.source.v1"));
        REQUIRE(!caps.has("media.lyrics.v1"));
        REQUIRE(!caps.has("media.playlist.v1"));
        REQUIRE(domains.mediaSources().empty());
        destroy_fn(plugin);
    }

    dlclose(h);
    return 0;
}

class MockAmpHttpServer {
public:
    MockAmpHttpServer() = default;
    ~MockAmpHttpServer() { stop(); }

    bool start() {
        sandbox_blocked_ = false;
        listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        if (listen_fd_ < 0) {
            if (errno == EPERM || errno == EACCES) sandbox_blocked_ = true;
            return false;
        }
        int opt = 1;
        ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(0);
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
            ::listen(listen_fd_, 16) != 0) {
            ::close(listen_fd_);
            listen_fd_ = -1;
            return false;
        }
        socklen_t len = sizeof(addr);
        ::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&addr), &len);
        port_ = ntohs(addr.sin_port);

        playlists_["p.YJXV7dEIeb3GzZX"] = PlaylistState{"ChatGPT Daily 5", true, {"1643009587"}};
        playlists_["p.ALIAS_DAILY"] = PlaylistState{"ChatGPT Daily 5 · 2026-11-12", true, {}};

        running_ = true;
        th_ = std::thread([this]() { serveLoop(); });
        return true;
    }

    void stop() {
        running_ = false;
        if (listen_fd_ >= 0) {
            ::shutdown(listen_fd_, SHUT_RDWR);
            ::close(listen_fd_);
            listen_fd_ = -1;
        }
        if (th_.joinable()) th_.join();
    }

    int port() const { return port_; }
    bool sandboxBlocked() const { return sandbox_blocked_; }
    std::string baseUrl() const { return "http://127.0.0.1:" + std::to_string(port_); }

    void setFailReadback(bool v) {
        std::lock_guard<std::mutex> lk(mu_);
        fail_readback_ = v;
    }

    void setUnstreamableId(std::string id) {
        std::lock_guard<std::mutex> lk(mu_);
        unstreamable_id_ = std::move(id);
    }

    int appendCount(const std::string& pid) {
        std::lock_guard<std::mutex> lk(mu_);
        return append_counts_[pid];
    }

    std::vector<std::string> playlistTracks(const std::string& pid) {
        std::lock_guard<std::mutex> lk(mu_);
        return playlists_[pid].track_ids;
    }

private:
    struct PlaylistState {
        std::string name;
        bool can_edit = true;
        std::vector<std::string> track_ids;
    };

    void serveLoop() {
        while (running_) {
            struct pollfd pfd{listen_fd_, POLLIN, 0};
            if (::poll(&pfd, 1, 100) <= 0) continue;
            int cfd = ::accept(listen_fd_, nullptr, nullptr);
            if (cfd < 0) continue;
            std::string raw;
            char buf[8192];
            while (true) {
                ssize_t n = ::recv(cfd, buf, sizeof(buf), 0);
                if (n <= 0) break;
                raw.append(buf, static_cast<size_t>(n));
                size_t hdr_end = raw.find("\r\n\r\n");
                if (hdr_end != std::string::npos) {
                    size_t cl = 0;
                    std::string hdrs = raw.substr(0, hdr_end);
                    std::istringstream hss(hdrs);
                    std::string line;
                    while (std::getline(hss, line)) {
                        std::string low = line;
                        for (char& ch : low) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
                        if (low.rfind("content-length:", 0) == 0) {
                            cl = std::stoul(low.substr(15));
                        }
                    }
                    if (raw.size() >= hdr_end + 4 + cl) break;
                }
            }
            size_t hdr_end = raw.find("\r\n\r\n");
            std::string body_in = (hdr_end != std::string::npos) ? raw.substr(hdr_end + 4) : "";
            std::istringstream iss(raw);
            std::string method, path;
            iss >> method >> path;

            int code = 404;
            std::string resp_body = R"({"errors":[{"title":"Not Found"}]})";

            std::lock_guard<std::mutex> lk(mu_);
            if (method == "GET" && path.rfind("/v1/catalog/cn/songs?ids=", 0) == 0) {
                std::string ids_str = path.substr(25);
                for (size_t pos = 0; (pos = ids_str.find("%2C", pos)) != std::string::npos;) {
                    ids_str.replace(pos, 3, ",");
                    pos += 1;
                }
                for (size_t pos = 0; (pos = ids_str.find("%2c", pos)) != std::string::npos;) {
                    ids_str.replace(pos, 3, ",");
                    pos += 1;
                }
                nlohmann::json data = nlohmann::json::array();
                std::istringstream idss(ids_str);
                std::string sid;
                while (std::getline(idss, sid, ',')) {
                    if (sid == "999999999999") continue;
                    bool streamable = (sid != unstreamable_id_);
                    data.push_back({
                        {"id", sid},
                        {"type", "songs"},
                        {"attributes", {
                            {"name", "Song " + sid},
                            {"artistName", "Artist " + sid},
                            {"albumName", "Album " + sid},
                            {"durationInMillis", 200000},
                            {"isStreamable", streamable},
                            {"playParams", {{"id", sid}, {"kind", "song"}}},
                            {"audioTraits", {"lossless"}},
                        }},
                    });
                }
                code = 200;
                resp_body = nlohmann::json({{"data", std::move(data)}}).dump();
            } else if (method == "GET" && (path == "/v1/me/library/playlists" || path.rfind("/v1/me/library/playlists?", 0) == 0)) {
                code = 200;
                nlohmann::json data = nlohmann::json::array();
                for (const auto& [pid, ps] : playlists_) {
                    data.push_back({
                        {"id", pid},
                        {"type", "library-playlists"},
                        {"attributes", {
                            {"name", ps.name},
                            {"canEdit", ps.can_edit},
                        }},
                    });
                }
                resp_body = nlohmann::json({{"data", std::move(data)}}).dump();
            } else if (method == "GET" && path.rfind("/v1/me/library/playlists/", 0) == 0) {
                std::string rest = path.substr(25);
                size_t slash = rest.find('/');
                std::string pid = (slash == std::string::npos) ? rest : rest.substr(0, slash);
                auto it = playlists_.find(pid);
                if (it != playlists_.end()) {
                    if (slash == std::string::npos) {
                        code = 200;
                        resp_body = nlohmann::json({{"data", {{
                            {"id", pid},
                            {"type", "library-playlists"},
                            {"attributes", {
                                {"name", it->second.name},
                                {"canEdit", it->second.can_edit},
                            }},
                        }}}}).dump();
                    } else if (rest.find("/tracks") != std::string::npos) {
                        code = 200;
                        nlohmann::json data = nlohmann::json::array();
                        for (const auto& sid : it->second.track_ids) {
                            if (fail_readback_ && sid != "1643009587") continue;
                            data.push_back({
                                {"id", "i." + sid},
                                {"type", "library-songs"},
                                {"attributes", {
                                    {"name", "Song " + sid},
                                    {"playParams", {{"id", "i." + sid}, {"catalogId", sid}}},
                                }},
                            });
                        }
                        resp_body = nlohmann::json({{"data", std::move(data)}}).dump();
                    }
                }
            } else if (method == "POST" && path == "/v1/me/library/playlists") {
                auto j = nlohmann::json::parse(body_in, nullptr, false);
                if (j.is_object() && j.contains("attributes")) {
                    std::string new_id = "p.CREATED_" + std::to_string(++next_pl_id_);
                    PlaylistState ps;
                    ps.name = j["attributes"].value("name", "Created");
                    if (j.contains("relationships") && j["relationships"].contains("tracks") &&
                        j["relationships"]["tracks"].contains("data")) {
                        for (const auto& tr : j["relationships"]["tracks"]["data"]) {
                            ps.track_ids.push_back(tr.value("id", ""));
                        }
                    }
                    playlists_[new_id] = std::move(ps);
                    code = 201;
                    resp_body = nlohmann::json({{"data", {{{"id", new_id}, {"type", "library-playlists"}}}}}).dump();
                }
            } else if (method == "POST" && path.rfind("/v1/me/library/playlists/", 0) == 0) {
                std::string rest = path.substr(25);
                size_t slash = rest.find("/tracks");
                if (slash != std::string::npos) {
                    std::string pid = rest.substr(0, slash);
                    auto it = playlists_.find(pid);
                    auto j = nlohmann::json::parse(body_in, nullptr, false);
                    if (it != playlists_.end() && j.is_object() && j.contains("data") && j["data"].is_array()) {
                        append_counts_[pid]++;
                        for (const auto& tr : j["data"]) {
                            it->second.track_ids.push_back(tr.value("id", ""));
                        }
                        code = 204;
                        resp_body = "";
                    }
                }
            }

            std::ostringstream resp;
            resp << "HTTP/1.1 " << code << " OK\r\n"
                 << "Content-Type: application/json\r\n"
                 << "Content-Length: " << resp_body.size() << "\r\n"
                 << "Connection: close\r\n\r\n"
                 << resp_body;
            std::string wire = resp.str();
            ::send(cfd, wire.data(), wire.size(), MSG_NOSIGNAL);
            ::close(cfd);
        }
    }

    std::atomic<bool> running_{false};
    int listen_fd_ = -1;
    int port_ = 0;
    bool sandbox_blocked_ = false;
    std::thread th_;
    std::mutex mu_;
    std::unordered_map<std::string, PlaylistState> playlists_;
    std::unordered_map<std::string, int> append_counts_;
    bool fail_readback_ = false;
    std::string unstreamable_id_;
    int next_pl_id_ = 0;
};

bool runGitCmd(const fs::path& cwd, const std::vector<std::string>& args) {
    pid_t pid = ::fork();
    if (pid < 0) return false;
    if (pid == 0) {
        if (!cwd.empty() && ::chdir(cwd.c_str()) != 0) ::_exit(126);
        int devnull = ::open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            ::dup2(devnull, STDOUT_FILENO);
            ::dup2(devnull, STDERR_FILENO);
            ::close(devnull);
        }
        std::vector<char*> argv;
        argv.push_back(const_cast<char*>("/usr/bin/git"));
        for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        ::execv("/usr/bin/git", argv.data());
        ::_exit(127);
    }
    int st = 0;
    ::waitpid(pid, &st, 0);
    return WIFEXITED(st) && WEXITSTATUS(st) == 0;
}

std::string blobSha(const std::string& content) {
    std::string data = "blob " + std::to_string(content.size()) + std::string("\0", 1) + content;
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int len = 0;
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    if (!ctx) return "";
    if (EVP_DigestInit_ex(ctx, EVP_sha1(), nullptr) != 1 ||
        EVP_DigestUpdate(ctx, data.data(), data.size()) != 1 ||
        EVP_DigestFinal_ex(ctx, digest, &len) != 1) {
        EVP_MD_CTX_free(ctx);
        return "";
    }
    EVP_MD_CTX_free(ctx);
    std::ostringstream oss;
    for (unsigned int i = 0; i < len; ++i) {
        oss << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(digest[i]);
    }
    return oss.str();
}

void writeFileText(const fs::path& p, const std::string& content) {
    fs::create_directories(p.parent_path());
    std::ofstream ofs(p, std::ios::binary | std::ios::trunc);
    ofs.write(content.data(), static_cast<std::streamsize>(content.size()));
}

int testMediaPlaylistV1(const fs::path& tmp) {
    omnidesk::CapabilityRegistry caps;
    omnidesk::EntityBus bus;
    omnidesk::DomainRegistry domains;
    fs::path pl_tmp = tmp / "playlist_test";
    fs::remove_all(pl_tmp);
    fs::create_directories(pl_tmp);

    omnidesk::ConfigStore cfg_store((pl_tmp / "cfg").string());
    omnidesk::SecretVault vault((pl_tmp / "cfg").string());
    cfg_store.savePluginConfig("plugin-music", {
        {"auto_launch_engine", false},
        {"engine_port", 29997},
        {"storefront", "cn"},
    });

    omnidesk::PluginContext ctx("plugin-music", pl_tmp.string(), (pl_tmp / "data").string(),
                                caps, bus, domains, cfg_store, vault);
    music_plugin::MusicEngine engine(ctx);
    engine.initializeFast();

    int playlist_sync_events = 0;
    uint64_t bus_tok = bus.subscribe("entity.music.playlist_synced", "test-playlist",
        [&](const std::string&, const nlohmann::json&) {
            ++playlist_sync_events;
        });

    // Build clean synthetic music-daily & obsidian-vault Git repositories with full publication proof
    fs::path fixture_repo = pl_tmp / "fixture_music_daily";
    fs::path fixture_vault = pl_tmp / "fixture_obsidian_vault";
    fs::create_directories(fixture_repo);
    fs::create_directories(fixture_vault);
    REQUIRE(runGitCmd(fixture_repo, {"init", "-b", "main"}));
    REQUIRE(runGitCmd(fixture_repo, {"config", "user.email", "test@omnidesk.local"}));
    REQUIRE(runGitCmd(fixture_repo, {"config", "user.name", "Test"}));
    REQUIRE(runGitCmd(fixture_vault, {"init", "-b", "main"}));
    REQUIRE(runGitCmd(fixture_vault, {"config", "user.email", "test@omnidesk.local"}));
    REQUIRE(runGitCmd(fixture_vault, {"config", "user.name", "Test"}));

    const std::string manifest_rel = "recommendations/2026/11/2026-11-13.json";
    const nlohmann::json manifest_json = {
        {"schema_version", 1},
        {"provider", "apple_music"},
        {"date", "2026-11-13"},
        {"tracks", {
            {{"position", 1}, {"title", "Muerte"}, {"artist", "Natalia Lafourcade"},
             {"album", "De Todas las Flores"}, {"canonical_id", "Natalia Lafourcade::Muerte"},
             {"analysis_summary", "Summary 1"}, {"listening_cue", "Cue 1"},
             {"apple_music", {{"song_id", "1643009587"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/1643009587"}}}},
            {{"position", 2}, {"title", "Plastic 100°C"}, {"artist", "Sampha"},
             {"album", "Process"}, {"canonical_id", "Sampha::Plastic 100°C"},
             {"analysis_summary", "Summary 2"}, {"listening_cue", "Cue 2"},
             {"apple_music", {{"song_id", "1607100336"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/1607100336"}}}},
            {{"position", 3}, {"title", "Tobiume (2025 Remaster)"}, {"artist", "Susumu Yokota"},
             {"album", "Sakura"}, {"canonical_id", "Susumu Yokota::Tobiume (2025 Remaster)"},
             {"analysis_summary", "Summary 3"}, {"listening_cue", "Cue 3"},
             {"apple_music", {{"song_id", "1833283533"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/1833283533"}}}},
            {{"position", 4}, {"title", "O Leaozinho"}, {"artist", "Caetano Veloso"},
             {"album", "Beleza Tropical"}, {"canonical_id", "Caetano Veloso::O Leaozinho"},
             {"analysis_summary", "Summary 4"}, {"listening_cue", "Cue 4"},
             {"apple_music", {{"song_id", "3735675"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/3735675"}}}},
            {{"position", 5}, {"title", "Trouble of the World"}, {"artist", "Mahalia Jackson"},
             {"album", "Gospels"}, {"canonical_id", "Mahalia Jackson::Trouble of the World"},
             {"analysis_summary", "Summary 5"}, {"listening_cue", "Cue 5"},
             {"apple_music", {{"song_id", "1032611724"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/1032611724"}}}},
        }},
    };
    const std::string manifest_raw = manifest_json.dump(2) + "\n";
    const std::string anomaly_rel = "recommendations/2026/11/2026-11-06.json";
    const nlohmann::json anomaly_manifest_json = {
        {"schema_version", 1},
        {"provider", "apple_music"},
        {"date", "2026-11-06"},
        {"tracks", {
            {{"position", 1}, {"title", "Anomaly One"}, {"artist", "Archive Artist"},
             {"album", "Known Partial Day"}, {"canonical_id", "Archive Artist::Anomaly One"},
             {"analysis_summary", "Historical summary 1"}, {"listening_cue", "Historical cue 1"},
             {"apple_music", {{"song_id", "2000000001"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/2000000001"}}}},
            {{"position", 2}, {"title", "Anomaly Two"}, {"artist", "Archive Artist"},
             {"album", "Known Partial Day"}, {"canonical_id", "Archive Artist::Anomaly Two"},
             {"analysis_summary", "Historical summary 2"}, {"listening_cue", "Historical cue 2"},
             {"apple_music", {{"song_id", "2000000002"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/2000000002"}}}},
            {{"position", 3}, {"title", "Anomaly Three"}, {"artist", "Archive Artist"},
             {"album", "Known Partial Day"}, {"canonical_id", "Archive Artist::Anomaly Three"},
             {"analysis_summary", "Historical summary 3"}, {"listening_cue", "Historical cue 3"},
             {"apple_music", {{"song_id", "2000000003"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/2000000003"}}}},
            {{"position", 4}, {"title", "Anomaly Four"}, {"artist", "Archive Artist"},
             {"album", "Known Partial Day"}, {"canonical_id", "Archive Artist::Anomaly Four"},
             {"analysis_summary", "Historical summary 4"}, {"listening_cue", "Historical cue 4"},
             {"apple_music", {{"song_id", "2000000004"}, {"storefront", "cn"}, {"url", "https://music.apple.com/cn/song/2000000004"}}}},
        }},
    };
    const std::string anomaly_manifest_raw = anomaly_manifest_json.dump(2) + "\n";
    const std::string state_raw = nlohmann::json({
        {"schema_version", 1},
        {"provider", "apple_music"},
        {"recommended", {
            {"Natalia Lafourcade::Muerte", 1},
            {"Sampha::Plastic 100°C", 1},
            {"Susumu Yokota::Tobiume (2025 Remaster)", 1},
            {"Caetano Veloso::O Leaozinho", 1},
            {"Mahalia Jackson::Trouble of the World", 1},
            {"Archive Artist::Anomaly One", 1},
            {"Archive Artist::Anomaly Two", 1},
            {"Archive Artist::Anomaly Three", 1},
            {"Archive Artist::Anomaly Four", 1},
        }},
    }).dump(2) + "\n";
    const std::string note_raw =
        "# 2026-11-13\n"
        "https://music.apple.com/cn/song/1643009587\n"
        "https://music.apple.com/cn/song/1607100336\n"
        "https://music.apple.com/cn/song/1833283533\n"
        "https://music.apple.com/cn/song/3735675\n"
        "https://music.apple.com/cn/song/1032611724\n";
    const std::string bundle_sha = music_plugin::sha256Hex("bundle-2026-11-13");
    const std::string plan_sha = music_plugin::sha256Hex("plan-2026-11-13");
    const std::string bundle_raw = nlohmann::json({
        {"status", "PUBLISHED"},
        {"operation_id", "music:2026-11-13"},
        {"bundle_sha256", bundle_sha},
        {"plan_sha256", plan_sha},
        {"payloads", {
            {"ZJFILOVE/music-daily:" + manifest_rel, manifest_raw},
            {"ZJFILOVE/music-daily:state/tracks.json", state_raw},
            {"ZJFILOVE/obsidian-vault:Music/Daily/2026-11-13.md", note_raw},
            {"ZJFILOVE/obsidian-vault:Music/Daily/manifests/2026-11-13.json", manifest_raw},
            {"ZJFILOVE/obsidian-vault:Music/Daily/state/tracks.json", state_raw},
        }},
    }).dump(2) + "\n";
    const std::string qa_raw = nlohmann::json({
        {"status", "passed_current_publication"},
        {"project", "music"},
        {"date", "2026-11-13"},
        {"operation_id", "music:2026-11-13"},
        {"bundle", {
            {"path", ".recommendation/transactions/music/2026-11-13.json"},
            {"bundle_sha256", bundle_sha},
            {"plan_sha256", plan_sha},
        }},
        {"readbacks", {
            {"ZJFILOVE/music-daily:" + manifest_rel, {{"sha", blobSha(manifest_raw)}, {"bytes", manifest_raw.size()}}},
            {"ZJFILOVE/music-daily:state/tracks.json", {{"sha", blobSha(state_raw)}, {"bytes", state_raw.size()}}},
            {"ZJFILOVE/obsidian-vault:Music/Daily/2026-11-13.md", {{"sha", blobSha(note_raw)}, {"bytes", note_raw.size()}}},
            {"ZJFILOVE/obsidian-vault:Music/Daily/manifests/2026-11-13.json", {{"sha", blobSha(manifest_raw)}, {"bytes", manifest_raw.size()}}},
            {"ZJFILOVE/obsidian-vault:Music/Daily/state/tracks.json", {{"sha", blobSha(state_raw)}, {"bytes", state_raw.size()}}},
        }},
    }).dump(2) + "\n";

    writeFileText(fixture_repo / manifest_rel, manifest_raw);
    writeFileText(fixture_repo / anomaly_rel, anomaly_manifest_raw);
    writeFileText(fixture_repo / "state/tracks.json", state_raw);
    REQUIRE(runGitCmd(fixture_repo, {"add", "-A"}));
    REQUIRE(runGitCmd(fixture_repo, {"commit", "-m", "publish 2026-11-13"}));

    writeFileText(fixture_vault / "Music/Daily/2026-11-13.md", note_raw);
    writeFileText(fixture_vault / "Music/Daily/manifests/2026-11-13.json", manifest_raw);
    writeFileText(fixture_vault / "Music/Daily/state/tracks.json", state_raw);
    writeFileText(fixture_vault / ".recommendation/transactions/music/2026-11-13.json", bundle_raw);
    writeFileText(fixture_vault / ".recommendation/runs/2026-11-13-music.json", qa_raw);
    REQUIRE(runGitCmd(fixture_vault, {"add", "-A"}));
    REQUIRE(runGitCmd(fixture_vault, {"commit", "-m", "publish receipt 2026-11-13"}));

    // 1. Strict online preflight: offline fallback forbidden + fails closed without auth
    auto pre_offline_forbidden = engine.handlePlaylistRpc({
        {"action", "preflight_cn_catalog"},
        {"storefront", "cn"},
        {"allow_offline_catalog_fallback", true},
        {"track_ids", {"1643009587"}},
    });
    REQUIRE(!pre_offline_forbidden.ok);
    REQUIRE(pre_offline_forbidden.data.value("error_code", "") == "offline_catalog_fallback_forbidden");

    auto pre_no_auth = engine.handlePlaylistRpc({
        {"action", "preflight_cn_catalog"},
        {"storefront", "cn"},
        {"track_ids", {"1643009587", "1607100336", "1833283533", "3735675", "1032611724"}},
    });
    REQUIRE(!pre_no_auth.ok);
    REQUIRE(pre_no_auth.data.value("error_code", "") == "auth_required");
    REQUIRE(!pre_no_auth.data.value("all_playable", true));
    REQUIRE(pre_no_auth.data.value("verified_count", -1) == 0);

    // 2. Hard guardrail: write_interactive_playlist & enqueue_playback targeting protected daily playlist
    for (const char* act_cstr : {"write_interactive_playlist", "enqueue_playback"}) {
        const std::string act(act_cstr);
        auto blk_id = engine.handlePlaylistRpc({
            {"action", act},
            {"mode", "interactive_ephemeral"},
            {"playlist_id", "p.YJXV7dEIeb3GzZX"},
            {"track_ids", {"1833283533"}},
        });
        REQUIRE(!blk_id.ok);
        REQUIRE(blk_id.data.value("error_code", "") == "protected_daily_playlist");
        REQUIRE(blk_id.data.value("protected_playlist_id", "") == "p.YJXV7dEIeb3GzZX");

        auto blk_name = engine.handlePlaylistRpc({
            {"action", act},
            {"mode", "interactive_ephemeral"},
            {"playlist_name", "chatgpt daily 5 · 2026-11-14"},
            {"track_ids", {"1833283533"}},
        });
        REQUIRE(!blk_name.ok);
        REQUIRE(blk_name.data.value("error_code", "") == "protected_daily_playlist");
    }

    // 3. sync_daily_authorized rejection on invalid mode, qa_verified=false, local_checkpoint_only, or unverified selection
    auto rej_mode = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "interactive_ephemeral"},
        {"qa_verified", true},
        {"business_date", "2026-11-13"},
        {"track_ids", {"1643009587"}},
    });
    REQUIRE(!rej_mode.ok);
    REQUIRE(rej_mode.data.value("error_code", "") == "invalid_curation_mode");

    auto rej_qa = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", false},
        {"business_date", "2026-11-13"},
        {"track_ids", {"1643009587"}},
    });
    REQUIRE(!rej_qa.ok);
    REQUIRE(rej_qa.data.value("error_code", "") == "qa_not_verified");

    auto rej_local_only = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"local_checkpoint_only", true},
        {"business_date", "2026-11-13"},
        {"track_ids", {"1643009587", "1607100336", "1833283533", "3735675", "1032611724"}},
    });
    REQUIRE(!rej_local_only.ok);
    REQUIRE(rej_local_only.data.value("error_code", "") == "local_checkpoint_only_forbidden");

    // Reject even with qa_verified=true when track_ids do not match committed 5-track manifest
    auto rej_mismatch = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"business_date", "2026-11-13"},
        {"music_daily_dir", fixture_repo.string()},
        {"obsidian_vault_dir", fixture_vault.string()},
        {"track_ids", {"1643009587"}},
    });
    REQUIRE(!rej_mismatch.ok);
    REQUIRE(rej_mismatch.data.value("error_code", "") == "publication_not_authorized");

    // 4. dry_run returns planned counts only, cloud_synced=false, zero mutations, zero bus events
    auto dry = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"dry_run", true},
        {"business_date", "2026-11-13"},
        {"operation_id", "music:2026-11-13"},
        {"music_daily_dir", fixture_repo.string()},
        {"obsidian_vault_dir", fixture_vault.string()},
        {"track_ids", {"1643009587", "1607100336", "1833283533", "3735675", "1032611724"}},
    });
    REQUIRE(dry.ok);
    REQUIRE(dry.data.value("dry_run", false));
    REQUIRE(!dry.data.value("cloud_synced", true));
    REQUIRE(!dry.data.value("live_apple_music_synced", true));
    REQUIRE(!dry.data.value("readback_verified", true));
    REQUIRE(dry.data.value("verified_count", -1) == 0);
    REQUIRE(dry.data.value("dates_synced", -1) == 0);
    REQUIRE(dry.data.value("total_cumulative_tracks", -1) == 0);
    REQUIRE(dry.data["planned_missing_ids"].size() == 5);
    REQUIRE(playlist_sync_events == 0);

    auto dry_all = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"dry_run", true},
        {"sync_all_rehydrated", true},
        {"music_daily_dir", fixture_repo.string()},
        {"obsidian_vault_dir", fixture_vault.string()},
    });
    REQUIRE(dry_all.ok);
    REQUIRE(dry_all.data.value("planned_dates_count", 0) == 2);
    REQUIRE(dry_all.data.value("planned_target_count", 0) == 9);
    REQUIRE(dry_all.data["history_anomalies"].size() == 1);
    REQUIRE(dry_all.data["history_anomalies"][0].get<std::string>().find("2026-11-06") !=
            std::string::npos);
    REQUIRE(dry_all.data["history_anomalies"][0].get<std::string>().find("4 tracks") !=
            std::string::npos);
    REQUIRE(playlist_sync_events == 0);

    // Live daily sync without MUT fails closed with auth_required and zero cloud mutations
    auto no_mut_sync = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"business_date", "2026-11-13"},
        {"operation_id", "music:2026-11-13"},
        {"music_daily_dir", fixture_repo.string()},
        {"obsidian_vault_dir", fixture_vault.string()},
        {"track_ids", {"1643009587", "1607100336", "1833283533", "3735675", "1032611724"}},
    });
    REQUIRE(!no_mut_sync.ok);
    REQUIRE(no_mut_sync.data.value("error_code", "") == "auth_required");
    REQUIRE(!no_mut_sync.data.value("cloud_synced", true));
    REQUIRE(playlist_sync_events == 0);

    // 5. Interactive playlist: no MUT => auth_required; local_checkpoint_only => local_staged=true, cloud_synced=false
    auto ix_no_auth = engine.handlePlaylistRpc({
        {"action", "write_interactive_playlist"},
        {"mode", "interactive_ephemeral"},
        {"playlist_name", "OmniDesk AI 灵感电台"},
        {"track_ids", {"1833283533", "1643009587"}},
    });
    REQUIRE(!ix_no_auth.ok);
    REQUIRE(ix_no_auth.data.value("error_code", "") == "auth_required");

    auto ix_local = engine.handlePlaylistRpc({
        {"action", "write_interactive_playlist"},
        {"mode", "interactive_ephemeral"},
        {"playlist_name", "OmniDesk AI 灵感电台"},
        {"write_strategy", "append"},
        {"local_checkpoint_only", true},
        {"enqueue_now", true},
        {"track_ids", {"1833283533", "1643009587"}},
    });
    REQUIRE(ix_local.ok);
    REQUIRE(ix_local.data.value("local_staged", false));
    REQUIRE(!ix_local.data.value("cloud_synced", true));
    REQUIRE(!ix_local.data.value("live_apple_music_synced", true));
    REQUIRE(ix_local.data.value("verified_count", -1) == 0);
    REQUIRE(ix_local.data.value("enqueued", false));
    REQUIRE(playlist_sync_events == 0);

    // Cancellation token must fail closed cleanly
    auto cancel = omnidesk::CancelToken::create();
    cancel->cancel();
    auto cancelled_res = engine.handlePlaylistRpc({
        {"action", "preflight_cn_catalog"},
        {"storefront", "cn"},
        {"track_ids", {"1643009587"}},
    }, cancel);
    REQUIRE(!cancelled_res.ok);
    REQUIRE(cancelled_res.cancelled);
    REQUIRE(cancelled_res.data.value("error_code", "") == "cancelled");

    // 6. Mock live AMP server tests (online preflight, remote append/readback, dated daily ID guard, remote name guard)
    MockAmpHttpServer amp;
    if (!amp.start()) {
        if (amp.sandboxBlocked()) {
            std::cout << "[SKIP] MockAmpHttpServer skipped in seccomp sandbox\n";
            bus.unsubscribe(bus_tok);
            engine.shutdown();
            return 0;
        }
        REQUIRE(false);
    }
    engine.adamClient().setDeveloperTokenForTesting("mock-dev-jwt");
    engine.adamClient().setMediaUserToken("mock-mut");
    engine.adamClient().setAmpApiBaseUrlForTesting(amp.baseUrl());

    // Online preflight detects missing and unstreamable songs accurately
    amp.setUnstreamableId("1032611724");
    auto pre_live = engine.handlePlaylistRpc({
        {"action", "preflight_cn_catalog"},
        {"storefront", "cn"},
        {"track_ids", {"1643009587", "1032611724", "999999999999"}},
    });
    REQUIRE(pre_live.ok);
    REQUIRE(pre_live.data.value("verified_count", 0) == 1);
    REQUIRE(!pre_live.data.value("all_playable", true));
    REQUIRE(pre_live.data["unplayable_ids"].size() == 2);
    amp.setUnstreamableId("");

    // Interactive write targeting an unknown ID whose remote name resolves to ChatGPT Daily 5* must be blocked
    auto blk_remote_daily = engine.handlePlaylistRpc({
        {"action", "write_interactive_playlist"},
        {"mode", "interactive_ephemeral"},
        {"playlist_id", "p.ALIAS_DAILY"},
        {"playlist_name", "Innocent Alias"},
        {"track_ids", {"1833283533"}},
    });
    REQUIRE(!blk_remote_daily.ok);
    REQUIRE(blk_remote_daily.data.value("error_code", "") == "protected_daily_playlist");
    REQUIRE(amp.appendCount("p.ALIAS_DAILY") == 0);

    // Live daily sync: missing-only append (1643009587 already in playlist -> appends 4), readback verified, dated created
    auto live_sync1 = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"business_date", "2026-11-13"},
        {"operation_id", "music:2026-11-13"},
        {"create_dated_playlist", true},
        {"music_daily_dir", fixture_repo.string()},
        {"obsidian_vault_dir", fixture_vault.string()},
        {"track_ids", {"1643009587", "1607100336", "1833283533", "3735675", "1032611724"}},
    });
    REQUIRE(live_sync1.ok);
    REQUIRE(live_sync1.data.value("cloud_synced", false));
    REQUIRE(live_sync1.data.value("live_apple_music_synced", false));
    REQUIRE(live_sync1.data.value("readback_verified", false));
    REQUIRE(live_sync1.data.value("added_count", 0) == 4);
    REQUIRE(live_sync1.data.value("already_present_count", 0) == 1);
    REQUIRE(live_sync1.data.value("verified_count", 0) == 5);
    REQUIRE(amp.appendCount("p.YJXV7dEIeb3GzZX") == 1);
    REQUIRE(playlist_sync_events == 1);

    struct stat st_buf {};
    REQUIRE(::stat(live_sync1.data.value("state_file", "").c_str(), &st_buf) == 0);
    REQUIRE((st_buf.st_mode & 0777) == 0600);

    // Idempotency: re-running same day adds 0 and does not call POST append again
    auto live_sync2 = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"business_date", "2026-11-13"},
        {"operation_id", "music:2026-11-13"},
        {"music_daily_dir", fixture_repo.string()},
        {"obsidian_vault_dir", fixture_vault.string()},
        {"track_ids", {"1643009587", "1607100336", "1833283533", "3735675", "1032611724"}},
    });
    REQUIRE(live_sync2.ok);
    REQUIRE(live_sync2.data.value("added_count", -1) == 0);
    REQUIRE(amp.appendCount("p.YJXV7dEIeb3GzZX") == 1);

    // Interactive write cannot target the dated daily playlist ID stored in state either
    auto st_live = engine.handlePlaylistRpc({{"action", "status"}});
    REQUIRE(st_live.ok);
    std::string dated_id = st_live.data["protected_daily_playlist"]["dated_playlists"]["2026-11-13"].value("playlist_id", "");
    REQUIRE(!dated_id.empty());
    auto blk_dated = engine.handlePlaylistRpc({
        {"action", "write_interactive_playlist"},
        {"mode", "interactive_ephemeral"},
        {"playlist_id", dated_id},
        {"playlist_name", "Innocent"},
        {"track_ids", {"1833283533"}},
    });
    REQUIRE(!blk_dated.ok);
    REQUIRE(blk_dated.data.value("error_code", "") == "protected_daily_playlist");

    // Live interactive playlist creation + append allowing historical repeats
    auto ix_live1 = engine.handlePlaylistRpc({
        {"action", "write_interactive_playlist"},
        {"mode", "interactive_ephemeral"},
        {"playlist_name", "OmniDesk AI 灵感电台"},
        {"track_ids", {"1833283533", "1643009587"}},
    });
    REQUIRE(ix_live1.ok);
    REQUIRE(ix_live1.data.value("cloud_synced", false));
    REQUIRE(ix_live1.data.value("readback_verified", false));
    REQUIRE(ix_live1.data.value("verified_count", 0) == 2);
    REQUIRE(!ix_live1.data.value("apple_music_playlist_id", "").empty());

    auto ix_live2 = engine.handlePlaylistRpc({
        {"action", "write_interactive_playlist"},
        {"mode", "interactive_ephemeral"},
        {"playlist_name", "OmniDesk AI 灵感电台"},
        {"track_ids", {"3735675"}},
    });
    REQUIRE(ix_live2.ok);
    REQUIRE(ix_live2.data["track_ids"].size() == 3);

    // Dirty repo must block daily sync
    writeFileText(fixture_repo / "dirty.txt", "dirty\n");
    auto rej_dirty = engine.handlePlaylistRpc({
        {"action", "sync_daily_authorized"},
        {"mode", "daily_immutable"},
        {"qa_verified", true},
        {"business_date", "2026-11-13"},
        {"music_daily_dir", fixture_repo.string()},
        {"obsidian_vault_dir", fixture_vault.string()},
        {"track_ids", {"1643009587", "1607100336", "1833283533", "3735675", "1032611724"}},
    });
    REQUIRE(!rej_dirty.ok);
    REQUIRE(rej_dirty.data.value("error_code", "") == "publication_not_authorized");

    amp.stop();
    bus.unsubscribe(bus_tok);
    engine.shutdown();
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    std::signal(SIGPIPE, SIG_IGN);
    if (argc < 2) {
        std::cerr << "Usage: test_music <plugin_so_path>\n";
        return 2;
    }

    fs::path tmp = fs::temp_directory_path() / ("omnidesk_music_test_" + std::to_string(::getpid()));
    fs::remove_all(tmp);
    fs::create_directories(tmp);

    int rc = 0;
    if (rc == 0) rc = testLrcAndBilingualParser();
    if (rc == 0) rc = testLrclibExactMatch();
    if (rc == 0) rc = testMockAdamAndLlmPipeline(tmp);
    if (rc == 0) rc = testDlopenPluginLifecycle(argv[1], tmp);
    if (rc == 0) rc = testMediaPlaylistV1(tmp);

    std::error_code ec;
    fs::remove_all(tmp, ec);
    if (rc == 0) {
        std::cout << "ALL plugin-music tests passed!\n";
    }
    return rc;
}
