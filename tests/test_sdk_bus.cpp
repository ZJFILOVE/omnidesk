#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <sys/stat.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>
#include "omnidesk/sdk.hpp"
#include "omnidesk/host/recycling_grid.hpp"
#include <yoga/Yoga.h>

#define TEST_CHECK(cond)                                                               \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            std::cerr << "TEST_CHECK failed at " << __FILE__ << ":" << __LINE__        \
                      << " (" << #cond << ")\n";                                       \
            std::exit(1);                                                              \
        }                                                                              \
    } while (false)

namespace fs = std::filesystem;

namespace {

class DummyMediaSource final : public omnidesk::IMediaSource {
public:
    explicit DummyMediaSource(int sleep_slices = 0) : sleep_slices_(sleep_slices) {}

    std::string sourceId() const override { return sleep_slices_ > 0 ? "plugin-slow" : "plugin-dummy"; }
    std::string displayName() const override { return "Dummy Media"; }
    std::vector<std::string> categories() const override { return {"video"}; }

    std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
        omnidesk::MediaItem m;
        m.id = "m1";
        m.source_id = sourceId();
        m.title = "Sample Video";
        return {m};
    }

    std::shared_ptr<omnidesk::CancelToken> fetchItemsAsync(
        const std::string& category,
        const std::string& query,
        std::function<void(std::vector<omnidesk::MediaItem> items, bool cancelled)> callback,
        std::shared_ptr<omnidesk::CancelToken> cancel = nullptr) override {
        if (sleep_slices_ <= 0) {
            return IMediaSource::fetchItemsAsync(category, query, std::move(callback), std::move(cancel));
        }
        auto tok = cancel ? std::move(cancel) : omnidesk::CancelToken::create();
        int slices = sleep_slices_;
        std::thread([this, tok, slices, cb = std::move(callback)]() {
            for (int i = 0; i < slices; ++i) {
                if (tok->isCancelled()) {
                    if (cb) cb({}, true);
                    return;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
            if (cb) cb(fetchItems("all", ""), tok->isCancelled());
        }).detach();
        return tok;
    }

private:
    int sleep_slices_ = 0;
};

} // namespace

int main() {
    const fs::path tmp_root = fs::temp_directory_path() / ("omnidesk_sdk_test_" + std::to_string(getpid()));
    std::error_code ec;
    fs::remove_all(tmp_root, ec);
    fs::create_directories(tmp_root, ec);

    // 1. CapabilityRegistry priority & cleanup
    omnidesk::CapabilityRegistry caps;
    int cap_changes = 0;
    auto cap_sub = caps.subscribeChanges([&]() { ++cap_changes; });

    TEST_CHECK(caps.registerCapability("llm.chat.v1", "plugin-a", [](const nlohmann::json&) {
        omnidesk::CapabilityResult r;
        r.ok = true;
        r.data = {{"from", "a"}};
        return r;
    }, {}, 100));

    TEST_CHECK(caps.registerCapability("llm.chat.v1", "plugin-b", [](const nlohmann::json&) {
        omnidesk::CapabilityResult r;
        r.ok = true;
        r.data = {{"from", "b"}};
        return r;
    }, {}, 200));

    TEST_CHECK(caps.has("llm.chat.v1"));
    auto r1 = caps.call("llm.chat.v1", {});
    TEST_CHECK(r1.ok && r1.data["from"] == "b" && r1.provider_plugin_id == "plugin-b");

    caps.unregisterPlugin("plugin-b");
    auto r2 = caps.call("llm.chat.v1", {});
    TEST_CHECK(r2.ok && r2.data["from"] == "a");
    caps.unsubscribeChanges(cap_sub);

    // 2. EntityBus publish & scoped unsubscribeOwner
    omnidesk::EntityBus bus;
    int seen_a = 0;
    int seen_b = 0;
    bus.subscribe("entity.book.open", "plugin-a", [&](const std::string&, const nlohmann::json& p) {
        if (p.value("page", 0) == 42) ++seen_a;
    });
    bus.subscribe("entity.book.open", "plugin-b", [&](const std::string&, const nlohmann::json&) {
        ++seen_b;
    });
    TEST_CHECK(bus.publish("entity.book.open", {{"page", 42}}) == 2);
    TEST_CHECK(seen_a == 1 && seen_b == 1);
    bus.unsubscribeOwner("plugin-a");
    TEST_CHECK(bus.publish("entity.book.open", {{"page", 42}}) == 1);
    TEST_CHECK(seen_a == 1 && seen_b == 2);

    std::atomic<bool> delivery_entered{false};
    std::atomic<bool> release_delivery{false};
    bus.subscribe("entity.book.blocking", "plugin-blocked",
                  [&](const std::string&, const nlohmann::json&) {
                      delivery_entered.store(true, std::memory_order_release);
                      while (!release_delivery.load(std::memory_order_acquire)) {
                          std::this_thread::sleep_for(std::chrono::milliseconds(1));
                      }
                  });
    std::thread blocking_publisher([&]() {
        bus.publish("entity.book.blocking");
    });
    while (!delivery_entered.load(std::memory_order_acquire)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    TEST_CHECK(bus.hasInFlightOwner("plugin-blocked"));
    bus.unsubscribeOwner("plugin-blocked");
    TEST_CHECK(!bus.waitForOwnerIdle("plugin-blocked", 20));
    release_delivery.store(true, std::memory_order_release);
    blocking_publisher.join();
    TEST_CHECK(bus.waitForOwnerIdle("plugin-blocked", 1000));
    TEST_CHECK(!bus.hasInFlightOwner("plugin-blocked"));

    // 3. DomainRegistry registration & unload cleanup
    omnidesk::DomainRegistry domains;
    domains.registerMediaSource("plugin-dummy", std::make_shared<DummyMediaSource>());
    TEST_CHECK(domains.mediaSources().size() == 1);
    domains.unregisterPlugin("plugin-dummy");
    TEST_CHECK(domains.mediaSources().empty());

    // 4. ConfigStore & SecretVault with 0600 permissions
    {
        omnidesk::ConfigStore cfg(tmp_root.string());
        omnidesk::SecretVault vault(tmp_root.string());

        TEST_CHECK(cfg.setPluginValue("plugin-magpie-agent", "selected_harness", "codex"));
        TEST_CHECK(cfg.getPluginValue("plugin-magpie-agent", "selected_harness") == "codex");

        TEST_CHECK(vault.setSecret("plugin-fnos-nas", "password", "super-secret-123"));
        TEST_CHECK(vault.hasSecret("plugin-fnos-nas", "password"));
        TEST_CHECK(vault.getSecret("plugin-fnos-nas", "password").value() == "super-secret-123");

        struct stat st {};
        const fs::path vault_path = tmp_root / "vault.json";
        TEST_CHECK(stat(vault_path.c_str(), &st) == 0);
        TEST_CHECK((st.st_mode & 0777) == 0600);

        TEST_CHECK(vault.purgePlugin("plugin-fnos-nas"));
        TEST_CHECK(!vault.hasSecret("plugin-fnos-nas", "password"));
    }

    // 5. CancelToken + SseParser + callStreamAsync mid-stream cancellation
    {
        omnidesk::SseParser parser;
        std::vector<std::pair<std::string, std::string>> events;
        auto on_ev = [&](const std::string& ev, const std::string& data) {
            events.emplace_back(ev, data);
        };
        parser.feed("event: delta\ndata: {\"delta\":\"Hel", on_ev);
        parser.feed("lo\"}\n\ndata: {\"delta\":\"!\"}\n\nevent: done\ndata: [DONE]\n", on_ev);
        parser.flush(on_ev);
        TEST_CHECK(events.size() == 3);
        TEST_CHECK(events[0].first == "delta" && events[0].second == "{\"delta\":\"Hello\"}");
        TEST_CHECK(events[2].first == "done" && events[2].second == "[DONE]");

        TEST_CHECK(caps.registerStreamingCapability(
            "ai.agent.v1",
            "plugin-stream-test",
            [](const nlohmann::json&,
               const std::shared_ptr<omnidesk::CancelToken>& cancel,
               const omnidesk::StreamCallback& on_chunk) {
                omnidesk::CapabilityResult r;
                r.provider_plugin_id = "plugin-stream-test";
                for (int i = 0; i < 40; ++i) {
                    if (cancel && cancel->isCancelled()) {
                        r.ok = false;
                        r.cancelled = true;
                        return r;
                    }
                    omnidesk::StreamChunk c;
                    c.delta = "tok" + std::to_string(i) + " ";
                    if (on_chunk) on_chunk(c);
                    std::this_thread::sleep_for(std::chrono::milliseconds(15));
                }
                r.ok = true;
                r.data = {{"reply", "done"}};
                return r;
            },
            {},
            300));

        std::atomic<int> chunk_count{0};
        std::atomic<bool> finished{false};
        omnidesk::CapabilityResult final_res;
        auto tok = omnidesk::CancelToken::create();
        caps.callStreamAsync(
            "ai.agent.v1",
            {{"prompt", "test"}},
            [&](const omnidesk::StreamChunk& ch) {
                if (!ch.delta.empty() && chunk_count.fetch_add(1) + 1 >= 3) {
                    tok->cancel();
                }
            },
            [&](const omnidesk::CapabilityResult& res) {
                final_res = res;
                finished.store(true);
            },
            tok);
        for (int w = 0; w < 200 && !finished.load(); ++w) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(finished.load());
        TEST_CHECK(final_res.cancelled);
        TEST_CHECK(chunk_count.load() >= 3 && chunk_count.load() < 40);
        caps.unregisterPlugin("plugin-stream-test");
    }

    // 6. IMediaSource::fetchItemsAsync cancellation
    {
        DummyMediaSource slow_src(40);
        std::atomic<bool> cb_called{false};
        bool cb_cancelled = false;
        auto tok = slow_src.fetchItemsAsync("video", "", [&](std::vector<omnidesk::MediaItem> items, bool cancelled) {
            (void)items;
            cb_cancelled = cancelled;
            cb_called.store(true);
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        tok->cancel();
        for (int w = 0; w < 100 && !cb_called.load(); ++w) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(cb_called.load() && cb_cancelled);
    }

    // 7. JobCenter queue, progress, EntityBus triggerOpen & cancelByPlugin barrier
    {
        omnidesk::JobCenter jobs(&bus, 2);
        std::string opened_job_id;
        auto sub = bus.subscribe("entity.book.open", "test.shell", [&](const std::string&, const nlohmann::json& p) {
            opened_job_id = p.value("job_id", "");
        });

        omnidesk::JobSpec spec;
        spec.title = "BabelDOC Layout Test";
        spec.plugin_id = "plugin-pdf-babeldoc";
        spec.kind = "babeldoc";
        spec.open_target = {"book", "entity.book.open", "book-42", "打开译本", {{"title", "Deep Learning PDF"}}};

        std::string jid = jobs.submit(spec, [](omnidesk::JobHandle& job, std::string&) {
            job.reportProgress(0.5f, "OCR & 双栏重排");
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            job.reportProgress(1.0f, "排版完成");
            return true;
        });
        for (int w = 0; w < 100; ++w) {
            auto st = jobs.getJob(jid);
            if (st && st->state == omnidesk::JobState::Completed) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        auto done_job = jobs.getJob(jid);
        TEST_CHECK(done_job && done_job->state == omnidesk::JobState::Completed);
        TEST_CHECK(jobs.triggerOpen(jid));
        TEST_CHECK(opened_job_id == jid);
        bus.unsubscribe(sub);

        // cancelByPlugin completion barrier
        std::atomic<bool> plugin_task_exited{false};
        omnidesk::JobSpec long_spec{"Long Worker", "plugin-barrier", "transcribe", {}};
        jobs.submit(long_spec, [&](omnidesk::JobHandle& job, std::string&) {
            while (!job.isCancelled()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(30));
            plugin_task_exited.store(true);
            return false;
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
        jobs.cancelByPlugin("plugin-barrier");
        TEST_CHECK(plugin_task_exited.load());
        jobs.shutdown();
    }

    // 8. MPVCore header isolation, log redaction, SubtitleOverlay & SponsorBlock segment
    {
        omnidesk::MediaRequestOptions bili;
        bili.url = "https://upos.example.com/v1.m4s?token=secret123";
        bili.referrer = "https://www.bilibili.com/video/BV1xx";
        bili.user_agent = "BiliClient/2.0";
        bili.http_headers = {{"Cookie", "SESSDATA=abc;"}, {"X-Custom", "1,2"}};
        bili.audio_url = "https://upos.example.com/a1.m4s";
        bili.subtitle_urls = {"/tmp/zh.srt", "/tmp/en.srt"};
        std::string bili_opts = omnidesk::MPVCore::buildLoadfileOptions(bili);
        TEST_CHECK(bili_opts.find("referrer=\"https://www.bilibili.com/video/BV1xx\"") != std::string::npos);
        TEST_CHECK(bili_opts.find("Cookie: SESSDATA=abc;") != std::string::npos);
        TEST_CHECK(bili_opts.find("audio-file=\"https://upos.example.com/a1.m4s\"") != std::string::npos);

        // Next stream with empty headers MUST explicitly clear referrer & http-header-fields
        omnidesk::MediaRequestOptions nas;
        nas.url = "http://nas.local/movie.mkv";
        std::string nas_opts = omnidesk::MPVCore::buildLoadfileOptions(nas);
        TEST_CHECK(nas_opts.find("referrer=\"\"") != std::string::npos);
        TEST_CHECK(nas_opts.find("http-header-fields=\"\"") != std::string::npos);

        std::string redacted_url = omnidesk::MPVCore::redactSensitiveLog(bili.url);
        TEST_CHECK(redacted_url.find("secret123") == std::string::npos);
        std::string redacted_hdr = omnidesk::MPVCore::redactSensitiveLog("Header SESSDATA=abc123");
        TEST_CHECK(redacted_hdr.find("abc123") == std::string::npos);

        omnidesk::SubtitleOverlay subs;
        subs.setLines({{10.0, "Bonjour", "你好"}, {20.0, "Monde", "世界"}});
        auto cue = subs.activeLine(15.0);
        TEST_CHECK(cue.has_value() && cue->original == "Bonjour" && cue->translation == "你好");

        auto& mpv = omnidesk::MPVCore::instance();
        mpv.setChapters({{0.0, 20.0, "Intro", "", "content"}, {20.0, 50.0, "Sponsor", "", "sponsor"}});
        auto ad = mpv.activeAdSegment(30.0);
        TEST_CHECK(ad.has_value() && ad->kind == "sponsor");
        mpv.setChapters({});
    }

    // 9. ImageCache (WebP/PNG/SVG scaled decode + atomic disk LRU + in-flight coalescing)
    {
        static const uint8_t kWebp1x1[] = {
            0x52,0x49,0x46,0x46,0x1c,0x00,0x00,0x00,0x57,0x45,0x42,0x50,0x56,0x50,0x38,0x4c,0x10,0x00,
            0x00,0x00,0x2f,0x00,0x00,0x00,0x00,0x07,0x10,0xfd,0x2f,0x60,0xff,0x03,0x11,0xd1,0xff,0x00
        };
        static const uint8_t kPng1x1[] = {
            0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,0x00,0x00,0x00,0x0d,0x49,0x48,0x44,0x52,0x00,0x00,
            0x00,0x01,0x00,0x00,0x00,0x01,0x08,0x06,0x00,0x00,0x00,0x1f,0x15,0xc4,0x89,0x00,0x00,0x00,
            0x0d,0x49,0x44,0x41,0x54,0x78,0x9c,0x63,0xf8,0xcf,0xd0,0xf0,0x1f,0x00,0x06,0x00,0x02,0x7f,
            0x21,0x02,0x73,0x3b,0x00,0x00,0x00,0x00,0x49,0x45,0x4e,0x44,0xae,0x42,0x60,0x82
        };
        static const char kSvg400x200[] =
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"400\" height=\"200\" viewBox=\"0 0 400 200\">"
            "<rect width=\"400\" height=\"200\" fill=\"#ff0080\"/></svg>";

        auto dw = omnidesk::ImageCache::decodeBytes(kWebp1x1, sizeof(kWebp1x1));
        TEST_CHECK(dw.ok && dw.width == 1 && dw.height == 1 && dw.rgba.size() == 4);
        TEST_CHECK(dw.rgba[0] == 255 && dw.rgba[1] == 0 && dw.rgba[2] == 128 && dw.rgba[3] == 255);

        auto dp = omnidesk::ImageCache::decodeBytes(kPng1x1, sizeof(kPng1x1));
        TEST_CHECK(dp.ok && dp.width == 1 && dp.height == 1 && dp.rgba.size() == 4);
        TEST_CHECK(dp.rgba[0] == 255 && dp.rgba[1] == 0 && dp.rgba[2] == 128 && dp.rgba[3] == 255);

        auto dsvg_full = omnidesk::ImageCache::decodeBytes(
            reinterpret_cast<const uint8_t*>(kSvg400x200), sizeof(kSvg400x200) - 1);
        TEST_CHECK(dsvg_full.ok && dsvg_full.width == 400 && dsvg_full.height == 200);
        auto dsvg_thumb = omnidesk::ImageCache::decodeBytes(
            reinterpret_cast<const uint8_t*>(kSvg400x200), sizeof(kSvg400x200) - 1, 100, 100);
        TEST_CHECK(dsvg_thumb.ok && dsvg_thumb.width == 100 && dsvg_thumb.height == 50);
        TEST_CHECK(dsvg_thumb.rgba.size() == static_cast<size_t>(100 * 50 * 4));

        omnidesk::ImageCacheConfig ic_cfg;
        ic_cfg.cache_dir = (tmp_root / "imgcache").string();
        ic_cfg.max_disk_bytes = 128;
        TEST_CHECK(omnidesk::ImageCache::normalizeImageUrl(
                       "images/pic.png", "/tmp/omnidesk-doc/readme.md") ==
                   "/tmp/omnidesk-doc/images/pic.png");
        TEST_CHECK(omnidesk::ImageCache::normalizeImageUrl(
                       "images/pic%20one.png", "file:///tmp/omnidesk-doc/readme.md") ==
                   "/tmp/omnidesk-doc/images/pic one.png");
        TEST_CHECK(omnidesk::ImageCache::normalizeImageUrl(
                       "images/pic.png", "https://example.com/notes/readme.md") ==
                   "https://example.com/notes/images/pic.png");
        omnidesk::ImageCache cache(ic_cfg);

        std::vector<uint8_t> blob1(60, 0x11), blob2(60, 0x22), blob3(60, 0x33), blob3_small(24, 0x44);
        TEST_CHECK(cache.putCachedRawBytes("k1", blob1.data(), blob1.size()));
        TEST_CHECK(cache.diskUsageBytes() == 60);
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
        TEST_CHECK(cache.putCachedRawBytes("k2", blob2.data(), blob2.size()));
        TEST_CHECK(cache.diskUsageBytes() == 120);
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
        TEST_CHECK(cache.putCachedRawBytes("k3", blob3.data(), blob3.size()));
        TEST_CHECK(cache.diskUsageBytes() <= 128);
        TEST_CHECK(cache.getCachedRawBytes("k3").has_value());
        const size_t before_overwrite = cache.diskUsageBytes();
        TEST_CHECK(cache.putCachedRawBytes("k3", blob3_small.data(), blob3_small.size()));
        TEST_CHECK(cache.diskUsageBytes() + (60 - 24) == before_overwrite);

        const fs::path svg_file = tmp_root / "coalesce.svg";
        {
            std::ofstream ofs(svg_file, std::ios::binary);
            ofs.write(kSvg400x200, sizeof(kSvg400x200) - 1);
        }
        const std::string svg_url = "file://" + svg_file.string();

        // Concurrent in-flight coalescing across cancelled + thumbnail + full-res subscribers
        auto cancelled_tok = omnidesk::CancelToken::create();
        cancelled_tok->cancel();
        std::atomic<int> cancelled_calls{0};
        std::atomic<int> completed_calls{0};
        omnidesk::DecodedImage thumb_res;
        omnidesk::DecodedImage thumb_res2;
        omnidesk::DecodedImage full_res;

        cache.fetchDecoded(svg_url, [&](omnidesk::DecodedImage) {
            cancelled_calls.fetch_add(1);
        }, {}, cancelled_tok, 96, 96);
        cache.fetchDecoded(svg_url, [&](omnidesk::DecodedImage img) {
            thumb_res = std::move(img);
            completed_calls.fetch_add(1);
        }, {}, nullptr, 96, 96);
        cache.fetchDecoded(svg_url, [&](omnidesk::DecodedImage img) {
            thumb_res2 = std::move(img);
            completed_calls.fetch_add(1);
        }, {}, nullptr, 96, 96);
        cache.fetchDecoded(svg_url, [&](omnidesk::DecodedImage img) {
            full_res = std::move(img);
            completed_calls.fetch_add(1);
        }, {}, nullptr, 0, 0);

        for (int w = 0; w < 100 && completed_calls.load() < 3; ++w) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(cancelled_calls.load() == 0);
        TEST_CHECK(completed_calls.load() == 3);
        TEST_CHECK(thumb_res.ok && thumb_res.width <= 128 && thumb_res.height <= 64);
        TEST_CHECK(thumb_res2.ok && thumb_res2.width == thumb_res.width && thumb_res2.height == thumb_res.height);
        TEST_CHECK(full_res.ok && full_res.width == 400 && full_res.height == 200);
        cache.shutdown();
    }

    // 10. WorkerSupervisor: Python3 NDJSON RPC, streaming cancel, idle eviction, respawn & zero orphans
    {
        const fs::path py_script = tmp_root / "worker_test.py";
        {
            std::ofstream ofs(py_script);
            ofs << "import json, sys, time\n"
                   "for line in sys.stdin:\n"
                   "    line = line.strip()\n"
                   "    if not line: continue\n"
                   "    req = json.loads(line)\n"
                   "    op = req.get('op', 'echo')\n"
                   "    if op == 'stream':\n"
                   "        for i in range(40):\n"
                   "            print(json.dumps({'type':'delta','delta':f'c{i} ','done':False}), flush=True)\n"
                   "            time.sleep(0.025)\n"
                   "        print(json.dumps({'type':'done','ok':True,'done':True,'data':{'reply':'fin'}}), flush=True)\n"
                   "    else:\n"
                   "        print(json.dumps({'ok':True,'data':{'echo':req.get('msg',''),'pid':__import__('os').getpid()}}), flush=True)\n";
        }

        omnidesk::WorkerSupervisor sup;
        omnidesk::WorkerSpec spec;
        spec.worker_id = "py-echo";
        spec.plugin_id = "plugin-py-test";
        spec.executable = "python3";
        spec.args = {"-u", py_script.string()};
        spec.idle_timeout_ms = 250;
        spec.stop_grace_ms = 300;
        TEST_CHECK(sup.registerWorker(spec));

        auto st0 = sup.status("py-echo");
        TEST_CHECK(st0.has_value() && !st0->running);

        // Lazy spawn + RPC
        auto r1_w = sup.request("py-echo", {{"op", "echo"}, {"msg", "ping"}});
        TEST_CHECK(r1_w.ok && r1_w.data["echo"] == "ping");
        auto st1 = sup.status("py-echo");
        TEST_CHECK(st1 && st1->running && st1->pid > 0 && st1->requests_served == 1);
        const int first_pid = st1->pid;

        // Streaming + mid-stream cancellation
        std::atomic<int> chunks{0};
        auto cancel = omnidesk::CancelToken::create();
        auto r_stream = sup.streamRequest(
            "py-echo",
            {{"op", "stream"}},
            [&](const omnidesk::StreamChunk& c) {
                if (!c.delta.empty() && chunks.fetch_add(1) + 1 >= 3) {
                    cancel->cancel();
                }
            },
            cancel);
        TEST_CHECK(r_stream.cancelled);
        TEST_CHECK(::kill(first_pid, 0) == -1 && errno == ESRCH);

        // Transparent respawn after termination
        auto r2_w = sup.request("py-echo", {{"op", "echo"}, {"msg", "respawn"}});
        TEST_CHECK(r2_w.ok && r2_w.data["echo"] == "respawn");
        auto st2 = sup.status("py-echo");
        TEST_CHECK(st2 && st2->running && st2->restart_count >= 1);
        const int second_pid = st2->pid;

        // Idle watchdog auto-shutdown
        bool auto_reaped = false;
        for (int w = 0; w < 30; ++w) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            auto cur = sup.status("py-echo");
            if (cur && !cur->running) {
                auto_reaped = true;
                break;
            }
        }
        TEST_CHECK(auto_reaped);
        TEST_CHECK(::kill(second_pid, 0) == -1 && errno == ESRCH);

        // Respawn cleanly then stopAll -> confirm zero orphan processes
        auto r3_w = sup.request("py-echo", {{"op", "echo"}, {"msg", "final"}});
        TEST_CHECK(r3_w.ok);
        const int third_pid = sup.status("py-echo")->pid;
        sup.stopAll();
        TEST_CHECK(::kill(third_pid, 0) == -1 && errno == ESRCH);
    }

    // 11. Pure-JSON domain struct round-trip serialization (types_json.hpp)
    {
        omnidesk::MediaItem mi;
        mi.id = "bv-1";
        mi.source_id = "bilibili";
        mi.title = "ABI Safety Talk";
        mi.creator = "OmniDesk";
        mi.kind = "video";
        mi.source_badge = "Bilibili";
        mi.cover_url = "https://img.example/1.jpg";
        mi.play_url = "https://cdn.example/1.mp4";
        mi.quality_label = "4K HEVC";
        mi.duration_sec = 321.5;
        mi.progress_sec = 42.25;
        mi.view_count = 987654;
        mi.has_bilingual_sub = true;
        mi.extra = {{"cid", 12345}};

        omnidesk::MediaItem mi_out;
        omnidesk::fromJson(omnidesk::toJson(mi), mi_out);
        TEST_CHECK(mi_out.id == mi.id && mi_out.source_id == mi.source_id);
        TEST_CHECK(mi_out.title == mi.title && mi_out.view_count == mi.view_count);
        TEST_CHECK(mi_out.has_bilingual_sub == true && mi_out.extra["cid"] == 12345);

        omnidesk::LyricLine lyr{12.5, "hello", "你好"};
        omnidesk::LyricLine lyr_out;
        omnidesk::fromJson(omnidesk::toJson(lyr), lyr_out);
        TEST_CHECK(lyr_out.original == "hello" && lyr_out.translation == "你好");

        omnidesk::ChapterSegment ch{10.0, 25.0, "Intro", "Overview", "content"};
        auto ch_arr = omnidesk::fromJsonArray<omnidesk::ChapterSegment>(
            omnidesk::toJsonArray(std::vector<omnidesk::ChapterSegment>{ch}));
        TEST_CHECK(ch_arr.size() == 1 && ch_arr[0].label == "Intro");

        omnidesk::TextFeedItem tfi;
        tfi.id = "art-1";
        tfi.source_id = "journals";
        tfi.source_name = "arXiv";
        tfi.category = "journal";
        tfi.title = "Zero-Crash Plugins";
        tfi.tags = {"abi", "ipc"};
        tfi.comment_count = 3;
        tfi.bilingual_cached = true;
        omnidesk::TextFeedItem tfi_out;
        omnidesk::fromJson(omnidesk::toJson(tfi), tfi_out);
        TEST_CHECK(tfi_out.tags.size() == 2 && tfi_out.tags[1] == "ipc" && tfi_out.bilingual_cached);

        omnidesk::TextChannel tch{"hot", "热榜", "50", true};
        omnidesk::TextChannel tch_out;
        omnidesk::fromJson(omnidesk::toJson(tch), tch_out);
        TEST_CHECK(tch_out.id == "hot" && tch_out.name == "热榜" &&
                   tch_out.badge == "50" && tch_out.requires_auth);

        omnidesk::CommentNode child_c{"c2", "bob", "mod", "reply", "回复", "2026-01-01", 2, false, {}};
        omnidesk::CommentNode root_c{"c1", "alice", "op", "root", "主楼", "2026-01-01", 5, true, {child_c}};
        omnidesk::TextArticle art;
        art.id = "art-1";
        art.title = "Zero-Crash Plugins";
        art.doi = "10.1000/test";
        art.paragraphs.push_back({"p1", "paragraph", "Source", "译文"});
        art.comments.push_back(root_c);
        omnidesk::TextArticle art_out;
        omnidesk::fromJson(omnidesk::toJson(art), art_out);
        TEST_CHECK(art_out.doi == "10.1000/test");
        TEST_CHECK(art_out.paragraphs.size() == 1 && art_out.paragraphs[0].translated == "译文");
        TEST_CHECK(art_out.comments.size() == 1 && art_out.comments[0].replies.size() == 1);
        TEST_CHECK(art_out.comments[0].replies[0].author == "bob");

        omnidesk::BookItem bi{"b1", "pdf", "Design", "K", "pdf", "/a.pdf", "/a.png", 3, 100, 3.0, true, 5, true};
        omnidesk::BookItem bi_out;
        omnidesk::fromJson(omnidesk::toJson(bi), bi_out);
        TEST_CHECK(bi_out.title == "Design" && bi_out.bilingual_ready && bi_out.nas_synced);

        omnidesk::BookSpread sp{"b1", "Ch 1", 3, 100, {{"blk1", "paragraph", "Orig", "Trans", {"word"}}}};
        omnidesk::BookSpread sp_out;
        omnidesk::fromJson(omnidesk::toJson(sp), sp_out);
        TEST_CHECK(sp_out.chapter_title == "Ch 1" && sp_out.blocks.size() == 1 &&
                   sp_out.blocks[0].highlighted_words == std::vector<std::string>{"word"});

        omnidesk::VocabCard vc{"ephemeral", "ephemeral", "/ɪˈfemərəl/", "Greek", "短暂的", "ctx", "Design", 4, 2, 2.6};
        omnidesk::VocabCard vc_out;
        omnidesk::fromJson(omnidesk::toJson(vc), vc_out);
        TEST_CHECK(vc_out.word == "ephemeral" && vc_out.interval_days == 4);

        omnidesk::CapabilityResult cr{true, {{"k", "v"}}, "", "prov-1", false};
        omnidesk::CapabilityResult cr_out;
        omnidesk::fromJson(omnidesk::toJson(cr), cr_out);
        TEST_CHECK(cr_out.ok && cr_out.data["k"] == "v" && cr_out.provider_plugin_id == "prov-1");

        omnidesk::StreamChunk sc{"tok", {{"step", 1}}, "delta", false};
        omnidesk::StreamChunk sc_out;
        omnidesk::fromJson(omnidesk::toJson(sc), sc_out);
        TEST_CHECK(sc_out.delta == "tok" && sc_out.data["step"] == 1 && !sc_out.done);
    }

    // 12. Exception barriers across CapabilityRegistry, EntityBus, and DomainRegistry
    {
        struct FaultLog {
            std::string owner;
            std::string comp;
            std::string err;
        };
        std::vector<FaultLog> faults;
        std::mutex fault_mu;

        omnidesk::CapabilityRegistry caps;
        caps.setFaultHandler([&](const std::string& p, const std::string& c, const std::string& e) {
            std::lock_guard<std::mutex> lk(fault_mu);
            faults.push_back({p, c, e});
        });
        TEST_CHECK(caps.registerCapability(
            "fault.throw.v1", "plugin-bad",
            [](const nlohmann::json&) -> omnidesk::CapabilityResult {
                throw std::runtime_error("cap boom");
            }));
        TEST_CHECK(caps.registerStreamingCapability(
            "fault.stream.v1", "plugin-bad",
            [](const nlohmann::json&, const std::shared_ptr<omnidesk::CancelToken>&,
               const omnidesk::StreamCallback&) -> omnidesk::CapabilityResult {
                throw std::runtime_error("stream boom");
            }));
        TEST_CHECK(caps.registerCapability(
            "fault.ok.v1", "plugin-good",
            [](const nlohmann::json&) -> omnidesk::CapabilityResult {
                return {true, {{"alive", true}}, "", ""};
            }));

        auto bad_call = caps.call("fault.throw.v1", {});
        TEST_CHECK(!bad_call.ok && bad_call.error.find("cap boom") != std::string::npos);
        auto bad_stream = caps.callStream("fault.stream.v1", {}, [](const omnidesk::StreamChunk&) {});
        TEST_CHECK(!bad_stream.ok && bad_stream.error.find("stream boom") != std::string::npos);
        TEST_CHECK(caps.call("fault.ok.v1", {}).ok);

        omnidesk::EntityBus bus;
        bus.setFaultHandler([&](const std::string& o, const std::string& t, const std::string& e) {
            std::lock_guard<std::mutex> lk(fault_mu);
            faults.push_back({o, t, e});
        });
        int good_sub_hits = 0;
        bus.subscribe("test.fault", "plugin-bad-sub", [](const std::string&, const nlohmann::json&) {
            throw std::runtime_error("sub boom");
        });
        bus.subscribe("test.fault", "plugin-good-sub", [&](const std::string&, const nlohmann::json&) {
            ++good_sub_hits;
        });
        bus.publish("test.fault", {{"n", 1}});
        TEST_CHECK(good_sub_hits == 1);

        struct ThrowingMedia : public omnidesk::IMediaSource {
            std::string sourceId() const override { return "throw-media"; }
            std::string displayName() const override { return "Throw Media"; }
            std::vector<std::string> categories() const override { return {"all"}; }
            std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
                throw std::runtime_error("media fetch boom");
            }
            std::vector<omnidesk::LyricLine> fetchLyrics(const std::string&) override {
                throw std::runtime_error("lyrics boom");
            }
        };
        struct ThrowingText : public omnidesk::ITextSource {
            std::string sourceId() const override { return "throw-text"; }
            std::string displayName() const override { return "Throw Text"; }
            std::string category() const override { return "blog"; }
            std::vector<omnidesk::TextChannel> channels() const override {
                throw std::runtime_error("channels boom");
            }
            std::vector<omnidesk::TextFeedItem> fetchFeed(const std::string&) override {
                throw std::runtime_error("text feed boom");
            }
            std::vector<omnidesk::TextFeedItem> fetchFeedInChannel(const std::string&,
                                                                 const std::string&) override {
                throw std::runtime_error("channel feed boom");
            }
            omnidesk::TextArticle fetchArticle(const std::string&) override {
                throw std::runtime_error("article boom");
            }
        };
        struct ThrowingBook : public omnidesk::IBookSource {
            std::string sourceId() const override { return "throw-book"; }
            std::string displayName() const override { return "Throw Book"; }
            std::vector<omnidesk::BookItem> listBooks(const std::string&, const std::string&) override {
                throw std::runtime_error("book list boom");
            }
            omnidesk::BookSpread fetchSpread(const std::string&, int) override {
                throw std::runtime_error("spread boom");
            }
        };

        omnidesk::DomainRegistry domains;
        domains.setFaultHandler([&](const std::string& p, const std::string& c, const std::string& e) {
            std::lock_guard<std::mutex> lk(fault_mu);
            faults.push_back({p, c, e});
        });
        domains.registerMediaSource("plugin-bad", std::make_shared<ThrowingMedia>());
        domains.registerTextSource("plugin-bad", std::make_shared<ThrowingText>());
        domains.registerBookSource("plugin-bad", std::make_shared<ThrowingBook>());
        domains.registerWorkspaceTab({
            "throw-tab", "plugin-bad", "Throw Tab", omnidesk::ContentDomain::Core, "",
            []() -> brls::View* { throw std::runtime_error("view boom"); }});

        auto m_srcs = domains.mediaSources();
        TEST_CHECK(m_srcs.size() == 1 && m_srcs[0]->sourceId() == "throw-media");
        TEST_CHECK(m_srcs[0]->fetchItems("all", "").empty());
        TEST_CHECK(m_srcs[0]->fetchLyrics("1").empty());

        auto t_srcs = domains.textSources();
        TEST_CHECK(t_srcs.size() == 1 && t_srcs[0]->channels().empty());
        TEST_CHECK(t_srcs[0]->fetchFeed("").empty());
        TEST_CHECK(t_srcs[0]->fetchFeedInChannel("hot", "").empty());
        TEST_CHECK(t_srcs[0]->fetchArticle("1").title.empty());

        auto b_srcs = domains.bookSources();
        TEST_CHECK(b_srcs.size() == 1 && b_srcs[0]->listBooks("", "").empty());
        TEST_CHECK(b_srcs[0]->fetchSpread("1", 1).blocks.empty());

        auto tabs = domains.workspaceTabs();
        TEST_CHECK(tabs.size() == 1 && tabs[0].create_view() == nullptr);

        std::lock_guard<std::mutex> lk(fault_mu);
        TEST_CHECK(faults.size() >= 10);
    }

    // 13. WorkerSupervisor: SIGSEGV crash detection + auto-restart retry, quarantine & AF_UNIX socket transport
    {
        const fs::path crash_script = tmp_root / "crash_or_unix_worker.py";
        {
            std::ofstream ofs(crash_script);
            ofs << R"PY(
import json, os, signal, socket, sys

sock_fd = os.environ.get("OMNIDESK_WORKER_SOCKET_FD", "")
sock_path = os.environ.get("OMNIDESK_WORKER_SOCKET", "")
if sock_fd:
    fd = int(sock_fd)
    rx = os.fdopen(fd, "r", encoding="utf-8", closefd=False)
    tx = os.fdopen(fd, "w", encoding="utf-8", buffering=1, closefd=False)
elif sock_path:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    rx = sock.makefile("r", encoding="utf-8")
    tx = sock.makefile("w", encoding="utf-8", buffering=1)
else:
    rx = sys.stdin
    tx = sys.stdout

for line in rx:
    s = line.strip()
    if not s:
        continue
    req = json.loads(s)
    op = req.get("op", "")
    if op == "crash_once":
        marker = req["marker"]
        if not os.path.exists(marker):
            with open(marker, "w") as mf:
                mf.write("1")
            os.kill(os.getpid(), signal.SIGSEGV)
        tx.write(json.dumps({"ok": True, "data": {"recovered": True}}) + "\n")
    elif op == "always_crash":
        os.kill(os.getpid(), signal.SIGABRT)
    elif op == "stream":
        tx.write(json.dumps({"type": "delta", "delta": "u1-", "done": False}) + "\n")
        tx.write(json.dumps({"type": "delta", "delta": "u2", "done": False}) + "\n")
        tx.write(json.dumps({"ok": True, "data": {"transport": "unix_socket" if sock_path else "pipe"}, "done": True}) + "\n")
    else:
        tx.write(json.dumps({"ok": True, "data": {"echo": req, "transport": "unix_socket" if sock_path else "pipe"}}) + "\n")
    tx.flush()
)PY";
        }

        omnidesk::WorkerSupervisor sup;
        std::vector<omnidesk::WorkerFaultEvent> worker_faults;
        std::mutex wf_mu;
        sup.setFaultCallback([&](const omnidesk::WorkerFaultEvent& ev) {
            std::lock_guard<std::mutex> lk(wf_mu);
            worker_faults.push_back(ev);
        });

        // 13a. SIGSEGV on first call -> WorkerFaultEvent + transparent auto-restart + retry_on_crash
        const fs::path crash_marker = tmp_root / "crash_once.marker";
        omnidesk::WorkerSpec crash_once_spec;
        crash_once_spec.worker_id = "crash-once";
        crash_once_spec.plugin_id = "plugin-crash-once";
        crash_once_spec.executable = "python3";
        crash_once_spec.args = {"-u", crash_script.string()};
        crash_once_spec.transport = omnidesk::WorkerTransport::StdioPipe;
        crash_once_spec.auto_restart = true;
        crash_once_spec.retry_on_crash = true;
        TEST_CHECK(sup.registerWorker(crash_once_spec));

        auto ping_ok = sup.request("crash-once", {{"op", "ping"}});
        TEST_CHECK(ping_ok.ok && ping_ok.data["transport"] == "pipe");
        const int first_pid = sup.status("crash-once")->pid;
        TEST_CHECK(first_pid > 0);

        auto recovered = sup.request(
            "crash-once", {{"op", "crash_once"}, {"marker", crash_marker.string()}});
        TEST_CHECK(recovered.ok);
        TEST_CHECK(recovered.data.value("recovered", false));
        auto st_after = sup.status("crash-once");
        TEST_CHECK(st_after.has_value());
        TEST_CHECK(st_after->pid > 0 && st_after->pid != first_pid);
        TEST_CHECK(st_after->crash_count >= 1);
        TEST_CHECK(st_after->restart_count >= 1);
        TEST_CHECK(st_after->last_signal == SIGSEGV);
        TEST_CHECK(st_after->last_signal_name == "SIGSEGV");
        TEST_CHECK(!st_after->quarantined);
        {
            std::lock_guard<std::mutex> lk(wf_mu);
            bool saw_segv = false;
            for (const auto& ev : worker_faults) {
                if (ev.worker_id == "crash-once" && ev.signal_number == SIGSEGV &&
                    ev.auto_restarted && ev.restart_succeeded && !ev.quarantined) {
                    saw_segv = true;
                }
            }
            TEST_CHECK(saw_segv);
        }

        // 13b. Crash-loop quarantine
        omnidesk::WorkerSpec loop_spec;
        loop_spec.worker_id = "crash-loop";
        loop_spec.plugin_id = "plugin-crash-loop";
        loop_spec.executable = "python3";
        loop_spec.args = {"-u", crash_script.string()};
        loop_spec.auto_restart = true;
        loop_spec.retry_on_crash = true;
        loop_spec.max_restarts_in_window = 2;
        loop_spec.restart_window_ms = 10000;
        TEST_CHECK(sup.registerWorker(loop_spec));

        bool hit_quarantine = false;
        for (int attempt = 0; attempt < 6; ++attempt) {
            auto r_fail = sup.request("crash-loop", {{"op", "always_crash"}});
            TEST_CHECK(!r_fail.ok);
            auto lst = sup.status("crash-loop");
            if (lst && lst->quarantined) {
                hit_quarantine = true;
                break;
            }
        }
        TEST_CHECK(hit_quarantine);
        auto blocked_after = sup.request("crash-loop", {{"op", "ping"}});
        TEST_CHECK(!blocked_after.ok);
        {
            std::lock_guard<std::mutex> lk(wf_mu);
            bool saw_quarantine = false;
            for (const auto& ev : worker_faults) {
                if (ev.worker_id == "crash-loop" && ev.quarantined) {
                    saw_quarantine = true;
                }
            }
            TEST_CHECK(saw_quarantine);
        }

        // 13c. WorkerTransport::UnixSocket request & NDJSON streamRequest
        omnidesk::WorkerSpec unix_spec;
        unix_spec.worker_id = "unix-echo";
        unix_spec.plugin_id = "plugin-unix";
        unix_spec.executable = "python3";
        unix_spec.args = {"-u", crash_script.string()};
        unix_spec.transport = omnidesk::WorkerTransport::UnixSocket;
        unix_spec.socket_path = (tmp_root / "worker_test.sock").string();
        TEST_CHECK(sup.registerWorker(unix_spec));

        auto u_req = sup.request("unix-echo", {{"op", "echo"}, {"msg", "unix-ok"}});
        TEST_CHECK(u_req.ok && u_req.data["transport"] == "unix_socket");
        TEST_CHECK(u_req.data["echo"]["msg"] == "unix-ok");

        std::string assembled;
        auto u_stream = sup.streamRequest(
            "unix-echo", {{"op", "stream"}},
            [&](const omnidesk::StreamChunk& ch) { assembled += ch.delta; });
        TEST_CHECK(u_stream.ok);
        TEST_CHECK(assembled == "u1-u2");
        TEST_CHECK(u_stream.data["transport"] == "unix_socket");

        sup.stopAll();
    }

    {
        const fs::path fault_script = tmp_root / "fault_worker.py";
        {
            std::ofstream script(fault_script);
            script << R"PY(
import json, os, signal, socket, sys, time

sock_fd = os.environ.get("OMNIDESK_WORKER_SOCKET_FD", "")
sock_path = os.environ.get("OMNIDESK_WORKER_SOCKET", "")
if sock_fd:
    fd = int(sock_fd)
    rx = os.fdopen(fd, "r", encoding="utf-8", closefd=False)
    tx = os.fdopen(fd, "w", encoding="utf-8", closefd=False)
elif sock_path:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    rx = sock.makefile("r", encoding="utf-8")
    tx = sock.makefile("w", encoding="utf-8")
else:
    rx, tx = sys.stdin, sys.stdout
if len(sys.argv) > 1 and sys.argv[1] == "no-read":
    time.sleep(30)
for line in rx:
    request = json.loads(line)
    op = request.get("op", "")
    if op == "hang":
        with open(request["marker"], "w") as marker:
            marker.write("ready")
        time.sleep(30)
    elif op == "malformed":
        tx.write('{"ok": [], "error": null}\n')
    elif op == "flood":
        tx.write("x" * (9 * 1024 * 1024))
    elif op == "log":
        sys.stderr.write("x" * 20000 + "\n")
        sys.stderr.flush()
        tx.write('{"ok": true, "data": {}}\n')
    elif op == "orphan":
        child = os.fork()
        if child == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(30)
            os._exit(0)
        os.kill(os.getpid(), signal.SIGSEGV)
    elif op == "partial-crash":
        tx.write('{"type":"delta", "delta":"once", "done":false}\n')
        tx.flush()
        os.kill(os.getpid(), signal.SIGSEGV)
    else:
        tx.write('{"ok": true, "data": {"alive": true}}\n')
    tx.flush()
)PY";
        }
        for (auto transport : {omnidesk::WorkerTransport::StdioPipe, omnidesk::WorkerTransport::UnixSocket}) {
            omnidesk::WorkerSupervisor supervisor;
            omnidesk::WorkerSpec spec;
            spec.worker_id = "faults";
            spec.plugin_id = "plugin-faults";
            spec.executable = "python3";
            spec.args = {"-u", fault_script.string(), "no-read"};
            spec.transport = transport;
            spec.stop_grace_ms = 50;
            spec.max_restarts_in_window = 1;
            TEST_CHECK(supervisor.registerWorker(spec));
            const auto started = std::chrono::steady_clock::now();
            auto timeout = supervisor.request("faults", {{"large", std::string(2 * 1024 * 1024, 'x')}},
                                              nullptr, 150);
            TEST_CHECK(!timeout.ok && timeout.error.find("timed out") != std::string::npos);
            TEST_CHECK(std::chrono::steady_clock::now() - started < std::chrono::seconds(3));
            TEST_CHECK(supervisor.status("faults")->crash_count == 1);
            auto repeated_timeout = supervisor.request("faults", {{"op", "ping"}}, nullptr, 150);
            TEST_CHECK(!repeated_timeout.ok && supervisor.status("faults")->quarantined);
            TEST_CHECK(supervisor.unregisterWorker("faults"));

            spec.args = {"-u", fault_script.string()};
            spec.max_restarts_in_window = 10;
            TEST_CHECK(supervisor.registerWorker(spec));
            auto malformed = supervisor.request("faults", {{"op", "malformed"}}, nullptr, 2000);
            TEST_CHECK(!malformed.ok && malformed.error.find("envelope") != std::string::npos);
            auto flood = supervisor.request("faults", {{"op", "flood"}}, nullptr, 2000);
            TEST_CHECK(!flood.ok && flood.error.find("limit") != std::string::npos);
            TEST_CHECK(supervisor.request("faults", {{"op", "log"}}, nullptr, 2000).ok);
            auto log_status = supervisor.status("faults");
            for (const auto& line : log_status->recent_stderr) {
                TEST_CHECK(line.size() <= 4096);
            }
            auto orphan = supervisor.request("faults", {{"op", "orphan"}}, nullptr, 2000);
            TEST_CHECK(!orphan.ok);
            TEST_CHECK(supervisor.request("faults", {{"op", "ping"}}, nullptr, 2000).ok);

            const fs::path marker = tmp_root / "hanging_request";
            fs::remove(marker, ec);
            auto cancel = omnidesk::CancelToken::create();
            omnidesk::CapabilityResult hanging;
            std::thread pending([&]() {
                hanging = supervisor.request("faults", {{"op", "hang"}, {"marker", marker.string()}},
                                             cancel, 3000);
            });
            for (int attempt = 0; attempt < 200 && !fs::exists(marker); ++attempt) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            TEST_CHECK(fs::exists(marker));
            auto queued = supervisor.request("faults", {{"op", "ping"}}, nullptr, 100);
            TEST_CHECK(!queued.ok && queued.error.find("pending request") != std::string::npos);
            cancel->cancel();
            pending.join();
            TEST_CHECK(hanging.cancelled);
            TEST_CHECK(supervisor.request("faults", {{"op", "ping"}}, nullptr, 2000).ok);
            fs::remove(marker, ec);
            std::thread unloading([&]() {
                hanging = supervisor.request("faults", {{"op", "hang"}, {"marker", marker.string()}},
                                             nullptr, 3000);
            });
            for (int attempt = 0; attempt < 200 && !fs::exists(marker); ++attempt) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            TEST_CHECK(fs::exists(marker));
            supervisor.unregisterWorker("faults");
            unloading.join();
            TEST_CHECK(!hanging.ok && !supervisor.status("faults").has_value());

            spec.retry_on_crash = true;
            TEST_CHECK(supervisor.registerWorker(spec));
            std::string streamed;
            auto partial = supervisor.streamRequest(
                "faults", {{"op", "partial-crash"}},
                [&](const omnidesk::StreamChunk& chunk) { streamed += chunk.delta; }, nullptr, 2000);
            TEST_CHECK(!partial.ok && streamed == "once");
        }
    }

    fs::remove_all(tmp_root, ec);

    // 15. DomainRegistry async fetch fault containment & unregisterPlugin barrier + JobCenter self-cancel
    {
        class AsyncFaultAndUnloadMediaSource final : public omnidesk::IMediaSource {
        public:
            AsyncFaultAndUnloadMediaSource(std::atomic<bool>& started,
                                           std::atomic<bool>& finished,
                                           bool should_throw)
                : started_(started), finished_(finished), should_throw_(should_throw) {}

            std::string sourceId() const override { return "plugin-async-domain"; }
            std::string displayName() const override { return "Async Domain"; }
            std::vector<std::string> categories() const override { return {"all"}; }
            std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
                started_.store(true);
                if (should_throw_) {
                    throw std::runtime_error("async domain boom");
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(80));
                finished_.store(true);
                return {};
            }

        private:
            std::atomic<bool>& started_;
            std::atomic<bool>& finished_;
            bool should_throw_ = false;
        };

        omnidesk::DomainRegistry dom;
        std::atomic<int> fault_count{0};
        std::string last_fault_err;
        dom.setFaultHandler([&](const std::string& pid, const std::string&, const std::string& err) {
            if (pid == "plugin-async-domain") {
                ++fault_count;
                last_fault_err = err;
            }
        });

        std::atomic<bool> s1{false}, f1{false};
        dom.registerMediaSource(
            "plugin-async-domain",
            std::make_shared<AsyncFaultAndUnloadMediaSource>(s1, f1, true));
        std::atomic<bool> cb1_done{false};
        dom.mediaSources()[0]->fetchItemsAsync("all", "", [&](std::vector<omnidesk::MediaItem> items, bool cancelled) {
            TEST_CHECK(items.empty() && !cancelled);
            cb1_done.store(true);
        });
        for (int i = 0; i < 200 && !cb1_done.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(cb1_done.load());
        TEST_CHECK(fault_count.load() == 1);
        TEST_CHECK(last_fault_err.find("async domain boom") != std::string::npos);
        dom.unregisterPlugin("plugin-async-domain");

        std::atomic<bool> s2{false}, f2{false};
        dom.registerMediaSource(
            "plugin-async-domain",
            std::make_shared<AsyncFaultAndUnloadMediaSource>(s2, f2, false));
        std::atomic<bool> cb2_cancelled{false};
        dom.mediaSources()[0]->fetchItemsAsync("all", "", [&](std::vector<omnidesk::MediaItem>, bool cancelled) {
            cb2_cancelled.store(cancelled);
        });
        for (int i = 0; i < 200 && !s2.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        TEST_CHECK(s2.load());
        dom.unregisterPlugin("plugin-async-domain");
        TEST_CHECK(f2.load());
        TEST_CHECK(cb2_cancelled.load());

        omnidesk::JobCenter self_cancel_jobs(&bus, 1);
        std::atomic<bool> self_cancel_done{false};
        self_cancel_jobs.submit({"self-cancel", "plugin-sc", "test", {}},
            [&](omnidesk::JobHandle&, std::string&) {
                self_cancel_jobs.cancelByPlugin("plugin-sc", true);
                self_cancel_done.store(true);
                return true;
            });
        for (int i = 0; i < 200 && !self_cancel_done.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(self_cancel_done.load());
        self_cancel_jobs.shutdown();
    }

    // 16. Bounded shutdown & unregisterPlugin grace timeout for stuck synchronous tasks
    {
        std::atomic<bool> job_started{false};
        std::atomic<bool> job_finished{false};
        {
            omnidesk::JobCenter stuck_jobs(&bus, 1);
            stuck_jobs.submit({"stuck-sync-job", "plugin-stuck", "fetch", {}},
                [&](omnidesk::JobHandle& job, std::string&) {
                    job_started.store(true);
                    std::this_thread::sleep_for(std::chrono::milliseconds(260));
                    job.reportProgress(1.0f, "late");
                    job_finished.store(true);
                    return true;
                });
            for (int i = 0; i < 200 && !job_started.load(); ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            TEST_CHECK(job_started.load());
            TEST_CHECK(!stuck_jobs.cancelByPlugin("plugin-stuck", true, 50));
            TEST_CHECK(stuck_jobs.hasActiveJobs("plugin-stuck"));
            const auto t0 = std::chrono::steady_clock::now();
            stuck_jobs.shutdown(50);
            const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t0).count();
            TEST_CHECK(elapsed_ms < 220);
        }
        for (int i = 0; i < 200 && !job_finished.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(job_finished.load());

        std::atomic<bool> cap_started{false};
        std::atomic<bool> cap_finished{false};
        {
            omnidesk::CapabilityRegistry stuck_caps;
            stuck_caps.registerCapability("stuck.cap.v1", "plugin-stuck-cap",
                [&](const nlohmann::json&) -> omnidesk::CapabilityResult {
                    cap_started.store(true);
                    std::this_thread::sleep_for(std::chrono::milliseconds(260));
                    cap_finished.store(true);
                    return {true, {}, "", ""};
                });
            stuck_caps.callAsync("stuck.cap.v1", {}, [](const omnidesk::CapabilityResult&) {});
            for (int i = 0; i < 200 && !cap_started.load(); ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            TEST_CHECK(cap_started.load());
            const auto t0 = std::chrono::steady_clock::now();
            ::setenv("OMNIDESK_SHUTDOWN_GRACE_MS", "50", 1);
            stuck_caps.unregisterPlugin("plugin-stuck-cap");
            TEST_CHECK(stuck_caps.hasInFlight("plugin-stuck-cap"));
            stuck_caps.shutdown(40);
            ::unsetenv("OMNIDESK_SHUTDOWN_GRACE_MS");
            const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t0).count();
            TEST_CHECK(elapsed_ms < 220);
        }
        for (int i = 0; i < 200 && !cap_finished.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(cap_finished.load());

        class StuckMediaSource final : public omnidesk::IMediaSource {
        public:
            StuckMediaSource(std::atomic<bool>& started, std::atomic<bool>& finished)
                : started_(started), finished_(finished) {}
            std::string sourceId() const override { return "plugin-stuck-dom"; }
            std::string displayName() const override { return "Stuck"; }
            std::vector<std::string> categories() const override { return {"all"}; }
            std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
                started_.store(true);
                std::this_thread::sleep_for(std::chrono::milliseconds(260));
                finished_.store(true);
                return {};
            }
        private:
            std::atomic<bool>& started_;
            std::atomic<bool>& finished_;
        };
        std::atomic<bool> dom_started{false};
        std::atomic<bool> dom_finished{false};
        {
            omnidesk::DomainRegistry stuck_dom;
            stuck_dom.registerMediaSource(
                "plugin-stuck-dom", std::make_shared<StuckMediaSource>(dom_started, dom_finished));
            stuck_dom.mediaSources()[0]->fetchItemsAsync("all", "", [](std::vector<omnidesk::MediaItem>, bool) {});
            for (int i = 0; i < 200 && !dom_started.load(); ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            TEST_CHECK(dom_started.load());
            ::setenv("OMNIDESK_SHUTDOWN_GRACE_MS", "50", 1);
            const auto t0 = std::chrono::steady_clock::now();
            stuck_dom.unregisterPlugin("plugin-stuck-dom");
            TEST_CHECK(stuck_dom.hasInFlight("plugin-stuck-dom"));
            stuck_dom.shutdown(40);
            ::unsetenv("OMNIDESK_SHUTDOWN_GRACE_MS");
            const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t0).count();
            TEST_CHECK(elapsed_ms < 220);
        }
        for (int i = 0; i < 200 && !dom_finished.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(dom_finished.load());
    }

    // 17. RemoteBridge (CapabilityRegistry + EntityBus) & full-duplex WorkerSupervisor + WorkerHostBridge
    {
        const fs::path dup_root = fs::temp_directory_path() / ("omnidesk_duplex_test_" + std::to_string(getpid()));
        fs::remove_all(dup_root, ec);
        fs::create_directories(dup_root, ec);

        // 17a. CapabilityRegistry RemoteBridge priority & merge
        {
            omnidesk::CapabilityRegistry local_caps;
            local_caps.registerCapability("shared.v1", "plugin-local", [](const nlohmann::json&) {
                return omnidesk::CapabilityResult{true, {{"from", "local"}}, "", "plugin-local"};
            }, {}, 100);

            auto rb = std::make_shared<omnidesk::CapabilityRegistry::RemoteBridge>();
            rb->has = [](const std::string& cid) { return cid == "remote.only.v1" || cid == "shared.v1"; };
            rb->providers_for = [](const std::string& cid) -> std::vector<omnidesk::CapabilityDescriptor> {
                if (cid == "shared.v1") {
                    return {{"shared.v1", "plugin-remote", 250, {}}, {"shared.v1", "plugin-local", 100, {}}};
                }
                if (cid == "remote.only.v1") {
                    return {{"remote.only.v1", "plugin-remote", 150, {}}};
                }
                return {};
            };
            rb->list_all = []() -> std::vector<omnidesk::CapabilityDescriptor> {
                return {{"shared.v1", "plugin-remote", 250, {}},
                        {"shared.v1", "plugin-local", 100, {}},
                        {"remote.only.v1", "plugin-remote", 150, {}}};
            };
            rb->invoke = [](const std::string& pid_filter,
                            const std::string& cid,
                            const nlohmann::json&,
                            const omnidesk::StreamCallback&,
                            const std::shared_ptr<omnidesk::CancelToken>&,
                            bool) -> omnidesk::CapabilityResult {
                return {true, {{"from", "remote"}, {"cid", cid}, {"filter", pid_filter}}, "", "plugin-remote"};
            };
            local_caps.setRemoteBridge(rb);
            TEST_CHECK(local_caps.has("remote.only.v1"));
            auto provs = local_caps.providersFor("shared.v1");
            TEST_CHECK(provs.size() == 2 && provs[0].plugin_id == "plugin-remote" && provs[1].plugin_id == "plugin-local");
            TEST_CHECK(local_caps.listAll().size() == 3);
            // Higher-priority remote wins default call()
            auto r_shared = local_caps.call("shared.v1", {});
            TEST_CHECK(r_shared.ok && r_shared.data["from"] == "remote");
            // Explicit callProvider("plugin-local", ...) executes locally
            auto r_loc = local_caps.callProvider("plugin-local", "shared.v1", {});
            TEST_CHECK(r_loc.ok && r_loc.data["from"] == "local");
            local_caps.setRemoteBridge(nullptr);
        }

        // 17b. EntityBus RemoteBridge refcounted subscriptions & echo-free publish
        {
            omnidesk::EntityBus eb;
            int sub_calls = 0;
            int unsub_calls = 0;
            int pub_calls = 0;
            auto bb = std::make_shared<omnidesk::EntityBus::RemoteBridge>();
            bb->on_subscribe = [&](const std::string& t) { if (t == "t.alpha") ++sub_calls; };
            bb->on_unsubscribe = [&](const std::string& t) { if (t == "t.alpha") ++unsub_calls; };
            bb->on_publish = [&](const std::string&, const nlohmann::json&) -> size_t {
                ++pub_calls;
                return 3;
            };
            eb.setRemoteBridge(bb);

            auto t1 = eb.subscribe("t.alpha", "o1", [](const std::string&, const nlohmann::json&) {});
            auto t2 = eb.subscribe("t.alpha", "o2", [](const std::string&, const nlohmann::json&) {});
            TEST_CHECK(sub_calls == 1 && unsub_calls == 0);
            eb.unsubscribe(t1);
            TEST_CHECK(unsub_calls == 0);
            eb.unsubscribeOwner("o2");
            TEST_CHECK(unsub_calls == 1);

            int local_seen = 0;
            eb.subscribe("t.beta", "o1", [&](const std::string&, const nlohmann::json&) { ++local_seen; });
            TEST_CHECK(eb.publish("t.beta", {{"k", 1}}) == 4);
            TEST_CHECK(local_seen == 1 && pub_calls == 1);
            TEST_CHECK(eb.publishLocalOnly("t.beta", {{"k", 2}}) == 1);
            TEST_CHECK(local_seen == 2 && pub_calls == 1);
            TEST_CHECK(eb.publishExcludingOwner("t.beta", {{"k", 3}}, "o1") == 0);
            TEST_CHECK(local_seen == 2);
            eb.setRemoteBridge(nullptr);
        }

        // 17c. Full-duplex WorkerSupervisor + WorkerHostBridge (pipe & unix_socket):
        //      concurrent calls, worker->host streaming + cancel, re-entrant calls, and bus events
        const fs::path dup_script = dup_root / "duplex_worker.py";
        {
            std::ofstream script(dup_script);
            script << R"PY(
import json, os, socket, sys, threading

sock_fd = os.environ.get("OMNIDESK_WORKER_SOCKET_FD", "")
sock_path = os.environ.get("OMNIDESK_WORKER_SOCKET", "")
if sock_fd:
    fd = int(sock_fd)
    rx = os.fdopen(fd, "r", encoding="utf-8", closefd=False)
    tx = os.fdopen(fd, "w", encoding="utf-8", closefd=False)
elif sock_path:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    rx = sock.makefile("r", encoding="utf-8")
    tx = sock.makefile("w", encoding="utf-8")
else:
    rx, tx = sys.stdin, sys.stdout

tx_lock = threading.Lock()
out_lock = threading.Lock()
pending_out = {}
seq = 0
seen_events = []
events_lock = threading.Lock()

def send_json(obj):
    with tx_lock:
        tx.write(json.dumps(obj) + "\n")
        tx.flush()

def call_host(op, body, stream=False, on_chunk=None):
    global seq
    with out_lock:
        seq += 1
        rid = f"w-{seq}"
        ev = threading.Event()
        entry = {"ev": ev, "resp": None, "on_chunk": on_chunk}
        pending_out[rid] = entry
    msg = dict(body)
    msg["kind"] = "req"
    msg["id"] = rid
    msg["op"] = op
    if stream:
        msg["stream"] = True
    send_json(msg)
    ev.wait(timeout=10.0)
    with out_lock:
        pending_out.pop(rid, None)
    return entry["resp"] or {"ok": False, "error": "timeout"}

def handle_req(msg):
    rid = msg.get("id", "")
    op = msg.get("op", "")
    if op == "ping":
        send_json({"kind": "resp", "id": rid, "done": True, "ok": True, "data": {"pong": True}})
    elif op == "call_host_cap":
        r = call_host("host.cap.invoke", {"capability_id": "host.echo.v1", "request": {"msg": msg.get("msg", "")}})
        send_json({"kind": "resp", "id": rid, "done": True, "ok": r.get("ok", False), "data": r.get("data", {})})
    elif op == "call_host_stream_cancel":
        chunks = []
        def on_ch(ch):
            d = ch.get("delta", "")
            if d:
                chunks.append(d)
                if len(chunks) >= 2:
                    # cancel outbound host call
                    pass
        # Use manual id so we can send cancel after 2 chunks
        global seq
        with out_lock:
            seq += 1
            hrid = f"w-{seq}"
            ev = threading.Event()
            def cancel_on_2(ch):
                if ch.get("delta"):
                    chunks.append(ch["delta"])
                    if len(chunks) == 2:
                        send_json({"kind": "cancel", "id": hrid})
            pending_out[hrid] = {"ev": ev, "resp": None, "on_chunk": cancel_on_2}
        send_json({"kind": "req", "id": hrid, "op": "host.cap.invoke", "capability_id": "host.slow_stream.v1", "stream": True, "request": {}})
        ev.wait(timeout=5.0)
        with out_lock:
            resp = pending_out.pop(hrid, {}).get("resp") or {}
        send_json({"kind": "resp", "id": rid, "done": True, "ok": True, "data": {"chunks": chunks, "cancelled": resp.get("cancelled", False)}})
    elif op == "reenter":
        r = call_host("host.cap.invoke", {"capability_id": "host.reenter.v1", "request": {"v": msg.get("v", 1)}})
        send_json({"kind": "resp", "id": rid, "done": True, "ok": r.get("ok", False), "data": r.get("data", {})})
    elif op == "leaf":
        send_json({"kind": "resp", "id": rid, "done": True, "ok": True, "data": {"leaf_val": msg.get("v", 0) + 10}})
    elif op == "sub_topic":
        r = call_host("host.bus.subscribe", {"topic": msg.get("topic", "")})
        send_json({"kind": "resp", "id": rid, "done": True, "ok": r.get("ok", False), "data": r.get("data", {})})
    elif op == "pub_topic":
        r = call_host("host.bus.publish", {"topic": msg.get("topic", ""), "payload": msg.get("payload", {})})
        send_json({"kind": "resp", "id": rid, "done": True, "ok": r.get("ok", False), "data": r.get("data", {})})
    elif op == "get_events":
        with events_lock:
            evs = list(seen_events)
        send_json({"kind": "resp", "id": rid, "done": True, "ok": True, "data": {"events": evs}})
    else:
        send_json({"kind": "resp", "id": rid, "done": True, "ok": False, "error": "unknown op"})

for line in rx:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    kind = msg.get("kind", "")
    rid = msg.get("id", "")
    if kind == "chunk" and rid:
        with out_lock:
            entry = pending_out.get(rid)
        if entry and entry["on_chunk"]:
            entry["on_chunk"](msg)
    elif kind == "resp" and rid:
        with out_lock:
            entry = pending_out.get(rid)
        if entry:
            entry["resp"] = msg
            entry["ev"].set()
    elif kind == "event":
        if msg.get("op") == "host.bus.event":
            with events_lock:
                seen_events.append({"topic": msg.get("topic"), "payload": msg.get("payload")})
    elif kind == "req":
        threading.Thread(target=handle_req, args=(msg,), daemon=True).start()
)PY";
        }

        for (auto transport : {omnidesk::WorkerTransport::StdioPipe, omnidesk::WorkerTransport::UnixSocket}) {
            omnidesk::CapabilityRegistry host_caps;
            omnidesk::EntityBus host_bus;
            omnidesk::WorkerSupervisor sup;

            host_caps.registerCapability("host.echo.v1", "host.test", [](const nlohmann::json& req) {
                return omnidesk::CapabilityResult{true, {{"echoed", req.value("msg", "")}}, "", "host.test"};
            });
            host_caps.registerStreamingCapability("host.slow_stream.v1", "host.test",
                [](const nlohmann::json&,
                   const std::shared_ptr<omnidesk::CancelToken>& cancel,
                   const omnidesk::StreamCallback& on_chunk) {
                    for (int i = 0; i < 30; ++i) {
                        if (cancel && cancel->isCancelled()) {
                            return omnidesk::CapabilityResult{false, {}, "cancelled", "host.test", true};
                        }
                        if (on_chunk) on_chunk({"tok" + std::to_string(i), nlohmann::json::object(), "delta", false});
                        std::this_thread::sleep_for(std::chrono::milliseconds(15));
                    }
                    return omnidesk::CapabilityResult{true, {}, "", "host.test", false};
                });
            host_caps.registerCapability("host.reenter.v1", "host.test", [&](const nlohmann::json& req) {
                auto res = sup.request("dup-w", {{"op", "leaf"}, {"v", req.value("v", 0)}});
                return res;
            });

            omnidesk::WorkerSpec spec;
            spec.worker_id = "dup-w";
            spec.plugin_id = "plugin-dup";
            spec.executable = "python3";
            spec.args = {"-u", dup_script.string()};
            spec.transport = transport;
            spec.duplex = true;
            TEST_CHECK(sup.registerWorker(spec));
            auto bridge = omnidesk::attachWorkerHostBridge(sup, "dup-w", "plugin-dup", host_caps, host_bus);

            // Worker -> Host capability call
            auto r_hc = sup.request("dup-w", {{"op", "call_host_cap"}, {"msg", "hi-host"}});
            TEST_CHECK(r_hc.ok && r_hc.data["echoed"] == "hi-host");

            // Worker -> Host streaming + mid-stream cancel
            auto r_sc = sup.request("dup-w", {{"op", "call_host_stream_cancel"}});
            TEST_CHECK(r_sc.ok && r_sc.data["cancelled"] == true);
            TEST_CHECK(r_sc.data["chunks"].size() >= 2 && r_sc.data["chunks"].size() < 30);

            // Re-entrant call: Host -> Worker("reenter") -> Host("host.reenter.v1") -> Worker("leaf")
            auto r_re = sup.request("dup-w", {{"op", "reenter"}, {"v", 32}});
            TEST_CHECK(r_re.ok && r_re.data["leaf_val"] == 42);

            // Bus forwarding: Host -> Worker and Worker -> Host (no echo back to same worker!)
            int host_observed = 0;
            auto h_tok = host_bus.subscribe("dup.topic", "host.observer",
                [&](const std::string&, const nlohmann::json& p) {
                    if (p.value("from", "") == "worker") ++host_observed;
                });
            TEST_CHECK(sup.request("dup-w", {{"op", "sub_topic"}, {"topic", "dup.topic"}}).ok);
            TEST_CHECK(sup.request("dup-w", {{"op", "sub_topic"}, {"topic", "*"}}).ok);
            host_bus.publish("dup.topic", {{"from", "host"}});
            TEST_CHECK(sup.request("dup-w", {{"op", "pub_topic"}, {"topic", "dup.topic"}, {"payload", {{"from", "worker"}}}}).ok);
            std::this_thread::sleep_for(std::chrono::milliseconds(40));
            auto evs = sup.request("dup-w", {{"op", "get_events"}});
            TEST_CHECK(evs.ok && evs.data["events"].size() == 1);
            TEST_CHECK(evs.data["events"][0]["payload"]["from"] == "host");
            TEST_CHECK(host_observed == 1);

            host_bus.unsubscribe(h_tok);
            bridge->detach();
            sup.unregisterWorker("dup-w");
            sup.stopAll();
        }
        fs::remove_all(dup_root, ec);
    }

    // 18. CapabilityRegistry: provider-scoped cancellation (no cross-plugin collateral cancel) & detached-worker safety
    {
        omnidesk::CapabilityRegistry reg;
        std::atomic<bool> b_async_entered{false};
        std::atomic<bool> b_stream_entered{false};
        std::atomic<bool> allow_b_finish{false};
        std::atomic<bool> b_collateral_cancelled{false};
        std::atomic<int> a_entered{0};
        std::atomic<int> a_cancelled_inside{0};

        TEST_CHECK(reg.registerStreamingCapability(
            "cap.b.async.v1", "pluginB",
            [&](const nlohmann::json&,
                const std::shared_ptr<omnidesk::CancelToken>& cancel,
                const omnidesk::StreamCallback&) -> omnidesk::CapabilityResult {
                b_async_entered.store(true);
                for (int i = 0; i < 600 && !allow_b_finish.load(); ++i) {
                    if (cancel && cancel->isCancelled()) {
                        b_collateral_cancelled.store(true);
                        return {false, {}, "cancelled", "pluginB", true};
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
                if (cancel && cancel->isCancelled()) {
                    b_collateral_cancelled.store(true);
                    return {false, {}, "cancelled", "pluginB", true};
                }
                return {true, {{"ok_from", "pluginB"}}, "", "pluginB", false};
            }));

        TEST_CHECK(reg.registerStreamingCapability(
            "cap.b.stream.v1", "pluginB",
            [&](const nlohmann::json&,
                const std::shared_ptr<omnidesk::CancelToken>& cancel,
                const omnidesk::StreamCallback& on_chunk) -> omnidesk::CapabilityResult {
                if (on_chunk) on_chunk({"b-chunk", nlohmann::json::object(), "delta", false});
                b_stream_entered.store(true);
                for (int i = 0; i < 600 && !allow_b_finish.load(); ++i) {
                    if (cancel && cancel->isCancelled()) {
                        b_collateral_cancelled.store(true);
                        return {false, {}, "cancelled", "pluginB", true};
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
                if (cancel && cancel->isCancelled()) {
                    b_collateral_cancelled.store(true);
                    return {false, {}, "cancelled", "pluginB", true};
                }
                return {true, {{"stream_from", "pluginB"}}, "", "pluginB", false};
            }));

        TEST_CHECK(reg.registerStreamingCapability(
            "cap.a.v1", "pluginA",
            [&](const nlohmann::json&,
                const std::shared_ptr<omnidesk::CancelToken>& cancel,
                const omnidesk::StreamCallback&) -> omnidesk::CapabilityResult {
                a_entered.fetch_add(1);
                for (int i = 0; i < 600; ++i) {
                    if (cancel && cancel->isCancelled()) {
                        a_cancelled_inside.fetch_add(1);
                        return {false, {}, "cancelled", "pluginA", true};
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
                return {true, {{"from", "pluginA"}}, "", "pluginA", false};
            }));

        std::atomic<bool> async_b_done{false};
        omnidesk::CapabilityResult async_b_res;
        reg.callAsync("cap.b.async.v1", {}, [&](const omnidesk::CapabilityResult& res) {
            async_b_res = res;
            async_b_done.store(true);
        });

        std::atomic<bool> stream_b_done{false};
        std::atomic<int> stream_b_chunks{0};
        omnidesk::CapabilityResult stream_b_res;
        reg.callStreamAsync(
            "cap.b.stream.v1",
            {},
            [&](const omnidesk::StreamChunk& ch) {
                if (!ch.delta.empty()) stream_b_chunks.fetch_add(1);
            },
            [&](const omnidesk::CapabilityResult& res) {
                stream_b_res = res;
                stream_b_done.store(true);
            });

        std::atomic<bool> async_a_done{false};
        omnidesk::CapabilityResult async_a_res;
        reg.callAsync("cap.a.v1", {}, [&](const omnidesk::CapabilityResult& res) {
            async_a_res = res;
            async_a_done.store(true);
        });

        omnidesk::CapabilityResult sync_a_res;
        auto sync_a_tok = omnidesk::CancelToken::create();
        std::thread sync_a_thread([&]() {
            sync_a_res = reg.callCancellable("cap.a.v1", {}, sync_a_tok);
        });

        for (int i = 0; i < 400; ++i) {
            if (b_async_entered.load() && b_stream_entered.load() && a_entered.load() == 2) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(b_async_entered.load() && b_stream_entered.load() && a_entered.load() == 2);

        // Unloading pluginA must cancel both sync callCancellable and dynamic callAsync on pluginA,
        // while leaving pluginB's in-flight callAsync & callStreamAsync completely untouched.
        reg.unregisterPlugin("pluginA");
        sync_a_thread.join();
        for (int i = 0; i < 200 && !async_a_done.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(sync_a_res.cancelled && !sync_a_res.ok);
        TEST_CHECK(async_a_done.load() && async_a_res.cancelled && !async_a_res.ok);
        TEST_CHECK(a_cancelled_inside.load() == 2);
        TEST_CHECK(!reg.hasInFlight("pluginA"));
        TEST_CHECK(!b_collateral_cancelled.load());
        TEST_CHECK(!async_b_done.load() && !stream_b_done.load());

        allow_b_finish.store(true);
        for (int i = 0; i < 200 && (!async_b_done.load() || !stream_b_done.load()); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(async_b_done.load() && async_b_res.ok && !async_b_res.cancelled);
        TEST_CHECK(stream_b_done.load() && stream_b_res.ok && !stream_b_res.cancelled);
        TEST_CHECK(stream_b_chunks.load() >= 1);
        TEST_CHECK(!b_collateral_cancelled.load());
        reg.shutdown();

        // Detached worker after shutdown(grace) + registry destruction must not UAF `this`
        std::atomic<bool> det_entered{false};
        std::atomic<bool> det_exited{false};
        std::atomic<bool> late_cb_called{false};
        {
            omnidesk::CapabilityRegistry short_lived;
            short_lived.registerCapability("det.cap.v1", "plugin-det",
                [&](const nlohmann::json&) -> omnidesk::CapabilityResult {
                    det_entered.store(true);
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                    det_exited.store(true);
                    return {true, {{"done", true}}, "", "plugin-det", false};
                });
            short_lived.callAsync("det.cap.v1", {}, [&](const omnidesk::CapabilityResult&) {
                late_cb_called.store(true);
            });
            for (int i = 0; i < 200 && !det_entered.load(); ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            TEST_CHECK(det_entered.load());
            short_lived.shutdown(15);
        }
        for (int i = 0; i < 200 && !det_exited.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(det_exited.load());
        TEST_CHECK(!late_cb_called.load());
    }

    // 19. WorkerHostBridge: host.cap.invoke self-routing ping-pong prevention
    {
        const fs::path loop_root = fs::temp_directory_path() / ("omnidesk_selfloop_test_" + std::to_string(getpid()));
        fs::remove_all(loop_root, ec);
        fs::create_directories(loop_root, ec);
        const fs::path worker_py = loop_root / "self_loop_worker.py";
        {
            std::ofstream ofs(worker_py);
            ofs << R"PY(
import json, os, sys, threading

rx, tx = sys.stdin, sys.stdout
tx_lock = threading.Lock()
out_lock = threading.Lock()
pending = {}
seq = 0
self_cap_hits = 0
hits_lock = threading.Lock()

def send_json(obj):
    with tx_lock:
        tx.write(json.dumps(obj) + "\n")
        tx.flush()

def call_host_cap(target_pid, cap_id, stream=False):
    global seq
    chunks = []
    with out_lock:
        seq += 1
        rid = f"s-{seq}"
        ev = threading.Event()
        pending[rid] = {"ev": ev, "resp": None, "chunks": chunks}
    send_json({
        "kind": "req",
        "id": rid,
        "op": "host.cap.invoke",
        "plugin_id": target_pid,
        "capability_id": cap_id,
        "stream": stream,
        "request": {"ping": 1},
    })
    ev.wait(timeout=3.0)
    with out_lock:
        entry = pending.pop(rid, None)
    resp = (entry or {}).get("resp") or {"ok": False, "error": "timeout"}
    return resp, chunks

def handle_req(msg):
    global self_cap_hits
    rid = msg.get("id", "")
    op = msg.get("op", "")
    if op == "cap.invoke":
        with hits_lock:
            self_cap_hits += 1
            cur = self_cap_hits
        if cur <= 2:
            r, _ = call_host_cap("", msg.get("capability_id", "shared.cap.v1"), False)
            send_json({"kind": "resp", "id": rid, "done": True, "ok": r.get("ok", False), "error": r.get("error", ""), "data": r.get("data", {})})
        else:
            send_json({"kind": "resp", "id": rid, "done": True, "ok": False, "error": "infinite loop detected"})
    elif op == "test_invoke":
        r, ch = call_host_cap(msg.get("target_plugin_id", ""), msg.get("capability_id", "shared.cap.v1"), bool(msg.get("stream", False)))
        with hits_lock:
            hits = self_cap_hits
        send_json({
            "kind": "resp",
            "id": rid,
            "done": True,
            "ok": True,
            "data": {
                "inner_ok": r.get("ok", False),
                "inner_error": r.get("error", ""),
                "inner_data": r.get("data", {}),
                "chunks": ch,
                "self_hits": hits,
            },
        })
    else:
        send_json({"kind": "resp", "id": rid, "done": True, "ok": False, "error": "unknown op"})

for line in rx:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    kind = msg.get("kind", "")
    rid = msg.get("id", "")
    if kind == "chunk" and rid:
        with out_lock:
            entry = pending.get(rid)
        if entry is not None and msg.get("delta"):
            entry["chunks"].append(msg["delta"])
    elif kind == "resp" and rid:
        with out_lock:
            entry = pending.get(rid)
        if entry is not None:
            entry["resp"] = msg
            entry["ev"].set()
    elif kind == "req":
        threading.Thread(target=handle_req, args=(msg,), daemon=True).start()
)PY";
        }

        omnidesk::CapabilityRegistry host_caps;
        omnidesk::EntityBus host_bus;
        omnidesk::WorkerSupervisor sup;

        omnidesk::WorkerSpec spec;
        spec.worker_id = "self-w";
        spec.plugin_id = "plugin-self";
        spec.executable = "python3";
        spec.args = {"-u", worker_py.string()};
        spec.duplex = true;
        TEST_CHECK(sup.registerWorker(spec));
        auto bridge = omnidesk::attachWorkerHostBridge(sup, "self-w", "plugin-self", host_caps, host_bus);

        // Register plugin-self on host_caps at highest priority (300) pointing right back into self-w
        TEST_CHECK(host_caps.registerStreamingCapability(
            "shared.cap.v1",
            "plugin-self",
            omnidesk::makeWorkerStreamCapabilityHandler(sup, "self-w", "shared.cap.v1"),
            {},
            300));

        // 19a. Explicit plugin_id == worker's own plugin_id is rejected immediately without calling worker back
        auto r_explicit = sup.request("self-w", {
            {"op", "test_invoke"},
            {"target_plugin_id", "plugin-self"},
            {"capability_id", "shared.cap.v1"},
            {"stream", false},
        });
        TEST_CHECK(r_explicit.ok);
        TEST_CHECK(!r_explicit.data["inner_ok"].get<bool>());
        TEST_CHECK(!r_explicit.data["inner_error"].get<std::string>().empty());
        TEST_CHECK(r_explicit.data["self_hits"].get<int>() == 0);

        // 19b. Empty plugin_id when ONLY plugin-self is registered fails cleanly without ping-pong
        auto r_only_self = sup.request("self-w", {
            {"op", "test_invoke"},
            {"target_plugin_id", ""},
            {"capability_id", "shared.cap.v1"},
            {"stream", false},
        });
        TEST_CHECK(r_only_self.ok);
        TEST_CHECK(!r_only_self.data["inner_ok"].get<bool>());
        TEST_CHECK(r_only_self.data["inner_error"].get<std::string>() == "capability not available: shared.cap.v1");
        TEST_CHECK(r_only_self.data["self_hits"].get<int>() == 0);

        // 19c. Empty plugin_id routes to highest-priority OTHER provider even when plugin-self has higher priority
        TEST_CHECK(host_caps.registerStreamingCapability(
            "shared.cap.v1",
            "plugin-fallback",
            [](const nlohmann::json&,
               const std::shared_ptr<omnidesk::CancelToken>&,
               const omnidesk::StreamCallback& on_chunk) -> omnidesk::CapabilityResult {
                if (on_chunk) on_chunk({"fb-delta", nlohmann::json::object(), "delta", false});
                return {true, {{"provider", "plugin-fallback"}}, "", "plugin-fallback", false};
            },
            {},
            150));
        TEST_CHECK(host_caps.registerCapability(
            "shared.cap.v1",
            "plugin-lower",
            [](const nlohmann::json&) -> omnidesk::CapabilityResult {
                return {true, {{"provider", "plugin-lower"}}, "", "plugin-lower", false};
            },
            {},
            50));

        auto r_routed = sup.request("self-w", {
            {"op", "test_invoke"},
            {"target_plugin_id", ""},
            {"capability_id", "shared.cap.v1"},
            {"stream", false},
        });
        TEST_CHECK(r_routed.ok);
        TEST_CHECK(r_routed.data["inner_ok"].get<bool>());
        TEST_CHECK(r_routed.data["inner_data"]["provider"] == "plugin-fallback");
        TEST_CHECK(r_routed.data["self_hits"].get<int>() == 0);

        auto r_routed_stream = sup.request("self-w", {
            {"op", "test_invoke"},
            {"target_plugin_id", ""},
            {"capability_id", "shared.cap.v1"},
            {"stream", true},
        });
        TEST_CHECK(r_routed_stream.ok);
        TEST_CHECK(r_routed_stream.data["inner_ok"].get<bool>());
        TEST_CHECK(r_routed_stream.data["inner_data"]["provider"] == "plugin-fallback");
        TEST_CHECK(r_routed_stream.data["chunks"].size() == 1 && r_routed_stream.data["chunks"][0] == "fb-delta");
        TEST_CHECK(r_routed_stream.data["self_hits"].get<int>() == 0);

        bridge->detach();
        sup.unregisterWorker("self-w");
        sup.stopAll();
        fs::remove_all(loop_root, ec);
    }

    // 20. ConfigStore, SecretVault & ImageCache path traversal rejection + PluginContext::logs() synchronization
    {
        const fs::path sec_root = fs::temp_directory_path() / ("omnidesk_cfg_sec_" + std::to_string(getpid()));
        fs::remove_all(sec_root, ec);
        fs::create_directories(sec_root, ec);

        omnidesk::ConfigStore cfg(sec_root.string());
        omnidesk::SecretVault vault(sec_root.string());

        TEST_CHECK(cfg.saveHostConfig({{"window_theme", "nord"}}));
        TEST_CHECK(vault.setSecret("plugin-legit", "token", "top-secret-value"));

        // BookWorkbench relies on "host-book-vocab"
        TEST_CHECK(cfg.savePluginConfig("host-book-vocab", {{"cards", nlohmann::json::array({"w1", "w2"})}}));
        TEST_CHECK(cfg.loadPluginConfig("host-book-vocab")["cards"].size() == 2);
        TEST_CHECK(vault.setSecret("host-book-vocab", "sync_key", "ok-123"));
        TEST_CHECK(vault.getSecret("host-book-vocab", "sync_key") == std::optional<std::string>("ok-123"));
        TEST_CHECK(vault.listKeys("host-book-vocab").size() == 1);

        const std::vector<std::string> bad_ids = {
            "",
            ".",
            "..",
            ".hidden",
            "../vault",
            "../host",
            "sub/plugin",
            "sub\\plugin",
            "plugin..bad",
            "plugin\nbad",
            std::string("bad\0id", 6),
        };
        for (const auto& bad : bad_ids) {
            auto loaded = cfg.loadPluginConfig(bad);
            TEST_CHECK(loaded.is_object() && loaded.empty());
            TEST_CHECK(!cfg.savePluginConfig(bad, {{"tamper", true}}));
            TEST_CHECK(!cfg.setPluginValue(bad, "tamper", "yes"));
            TEST_CHECK(cfg.getPluginValue(bad, "window_theme", "fallback") == "fallback");
            TEST_CHECK(!cfg.removePluginConfig(bad));

            TEST_CHECK(!vault.setSecret(bad, "k", "v"));
            TEST_CHECK(!vault.getSecret(bad, "token").has_value());
            TEST_CHECK(!vault.hasSecret(bad, "token"));
            TEST_CHECK(!vault.deleteSecret(bad, "token"));
            TEST_CHECK(!vault.purgePlugin(bad));
            TEST_CHECK(vault.listKeys(bad).empty());
        }

        // Verify vault.json and host.json were neither exposed nor modified/deleted
        TEST_CHECK(cfg.loadHostConfig().value("window_theme", "") == "nord");
        TEST_CHECK(vault.getSecret("plugin-legit", "token") == std::optional<std::string>("top-secret-value"));

        // ImageCache raw key validation
        omnidesk::ImageCacheConfig ic_cfg;
        ic_cfg.cache_dir = (sec_root / "imgcache").string();
        omnidesk::ImageCache ic(ic_cfg);
        const uint8_t sample_byte = 0xAB;
        for (const auto& bad : bad_ids) {
            TEST_CHECK(!ic.putCachedRawBytes(bad, &sample_byte, 1));
            TEST_CHECK(!ic.getCachedRawBytes(bad).has_value());
        }
        TEST_CHECK(ic.putCachedRawBytes("safe_key-01.v1", &sample_byte, 1));
        TEST_CHECK(ic.getCachedRawBytes("safe_key-01.v1").has_value());
        ic.shutdown();

        // PluginContext::logs() thread-safe value copy
        omnidesk::DomainRegistry tmp_dom;
        omnidesk::PluginContext pctx(
            "plugin-legit", sec_root.string(), sec_root.string(), caps, bus, tmp_dom, cfg, vault);
        std::thread writer([&]() {
            for (int i = 0; i < 40; ++i) pctx.logInfo("entry-" + std::to_string(i));
        });
        size_t max_seen = 0;
        for (int i = 0; i < 40; ++i) {
            std::vector<std::string> snap = pctx.logs();
            if (snap.size() > max_seen) max_seen = snap.size();
        }
        writer.join();
        TEST_CHECK(pctx.logs().size() == 40 && max_seen <= 40);

        fs::remove_all(sec_root, ec);
    }

    // 21. DomainRegistry: destroying DomainRegistry cleanly unbinds its sources from DomainAsyncPool
    {
        class SlowPoolMediaSource final : public omnidesk::IMediaSource {
        public:
            std::atomic<bool> started{false};
            std::atomic<bool> allow_finish{false};
            std::string sourceId() const override { return "slow-pool-src"; }
            std::string displayName() const override { return "Slow Pool"; }
            std::vector<std::string> categories() const override { return {"all"}; }
            std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
                started.store(true);
                for (int i = 0; i < 200 && !allow_finish.load(); ++i) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
                omnidesk::MediaItem item;
                item.id = "ok-1";
                return {item};
            }
        };

        auto shared_src = std::make_shared<SlowPoolMediaSource>();
        {
            omnidesk::DomainRegistry dom1;
            dom1.registerMediaSource("plugin-unbind-target", shared_src);
            TEST_CHECK(dom1.mediaSources().size() == 1);
        }
        // dom1 is now destroyed without calling unregisterPlugin; shared_src must be unbound from DomainAsyncPool.
        std::atomic<bool> cb_done{false};
        std::atomic<bool> cb_cancelled{true};
        size_t got_count = 0;
        shared_src->fetchItemsAsync("all", "", [&](std::vector<omnidesk::MediaItem> items, bool cancelled) {
            got_count = items.size();
            cb_cancelled.store(cancelled);
            cb_done.store(true);
        });
        for (int i = 0; i < 200 && !shared_src->started.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(shared_src->started.load());

        // Unregistering the same plugin_id on another DomainRegistry must NOT cancel shared_src's unbound call.
        {
            omnidesk::DomainRegistry dom2;
            dom2.unregisterPlugin("plugin-unbind-target");
        }
        shared_src->allow_finish.store(true);
        for (int i = 0; i < 200 && !cb_done.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(cb_done.load());
        TEST_CHECK(!cb_cancelled.load() && got_count == 1);

        // Verify raw source pointer reuse at the exact same address does not inherit stale FaultDispatcher
        struct ThrowingPoolMedia final : public omnidesk::IMediaSource {
            std::string sourceId() const override { return "throw-reuse"; }
            std::string displayName() const override { return "Throw Reuse"; }
            std::vector<std::string> categories() const override { return {"all"}; }
            std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
                throw std::runtime_error("reuse boom");
            }
        };
        alignas(ThrowingPoolMedia) unsigned char reuse_buf[sizeof(ThrowingPoolMedia)];
        std::atomic<int> stale_faults{0};
        {
            std::shared_ptr<ThrowingPoolMedia> first(
                new (reuse_buf) ThrowingPoolMedia(),
                [](ThrowingPoolMedia* p) { p->~ThrowingPoolMedia(); });
            omnidesk::DomainRegistry dom_reuse;
            dom_reuse.setFaultHandler([&](const std::string&, const std::string&, const std::string&) {
                stale_faults.fetch_add(1);
            });
            dom_reuse.registerMediaSource("plugin-reuse", first);
        }
        {
            std::shared_ptr<ThrowingPoolMedia> second(
                new (reuse_buf) ThrowingPoolMedia(),
                [](ThrowingPoolMedia* p) { p->~ThrowingPoolMedia(); });
            std::atomic<bool> reuse_done{false};
            second->fetchItemsAsync("all", "", [&](std::vector<omnidesk::MediaItem>, bool) {
                reuse_done.store(true);
            });
            for (int i = 0; i < 200 && !reuse_done.load(); ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            TEST_CHECK(reuse_done.load());
            TEST_CHECK(stale_faults.load() == 0);
        }
    }

    // 22. Cross-process & multi-thread FileLock + SecretVault non-clobbering concurrency
    {
        const std::string lock_dir =
            (fs::temp_directory_path() / ("omnidesk_lock_test_" + std::to_string(::getpid()))).string();
        fs::remove_all(lock_dir);
        fs::create_directories(lock_dir);

        omnidesk::SecretVault vault(lock_dir);
        std::vector<std::thread> writers;
        for (int t = 0; t < 8; ++t) {
            writers.emplace_back([&vault, t]() {
                for (int k = 0; k < 4; ++k) {
                    TEST_CHECK(vault.setSecret("plugin-flock", "k_" + std::to_string(t * 4 + k), "val_" + std::to_string(t)));
                }
            });
        }
        for (auto& w : writers) w.join();
        for (int i = 0; i < 32; ++i) {
            auto got = vault.getSecret("plugin-flock", "k_" + std::to_string(i));
            TEST_CHECK(got.has_value());
        }

        // Cross-process FileLock + SecretVault write test
        pid_t child = ::fork();
        TEST_CHECK(child >= 0);
        if (child == 0) {
            omnidesk::SecretVault child_vault(lock_dir);
            for (int i = 0; i < 10; ++i) {
                child_vault.setSecret("plugin-proc", "child_" + std::to_string(i), "cval");
            }
            _exit(0);
        } else {
            for (int i = 0; i < 10; ++i) {
                TEST_CHECK(vault.setSecret("plugin-proc", "parent_" + std::to_string(i), "pval"));
            }
            int status = 0;
            ::waitpid(child, &status, 0);
            TEST_CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
            for (int i = 0; i < 10; ++i) {
                TEST_CHECK(vault.getSecret("plugin-proc", "child_" + std::to_string(i)) == "cval");
                TEST_CHECK(vault.getSecret("plugin-proc", "parent_" + std::to_string(i)) == "pval");
            }
        }

        // Cross-process exclusive FileLock mutual exclusion
        const std::string target_file = lock_dir + "/shared.dat";
        omnidesk::FileLock parent_lock(target_file + ".lock", omnidesk::FileLock::Mode::Exclusive);
        TEST_CHECK(parent_lock.lock());
        pid_t contender = ::fork();
        TEST_CHECK(contender >= 0);
        if (contender == 0) {
            omnidesk::FileLock child_lock(target_file + ".lock", omnidesk::FileLock::Mode::Exclusive);
            bool acquired = child_lock.tryLock();
            _exit(acquired ? 2 : 0);
        }
        int cstatus = 0;
        ::waitpid(contender, &cstatus, 0);
        TEST_CHECK(WIFEXITED(cstatus) && WEXITSTATUS(cstatus) == 0);
        parent_lock.unlock();
        fs::remove_all(lock_dir);
    }

    // 23. UiTaskRunner dual-tier non-blocking alongside saturated JobCenter & lane superseding
    {
        omnidesk::EntityBus dual_bus;
        omnidesk::JobCenter heavy_jobs(&dual_bus, 1);
        omnidesk::UiTaskRunner ui_runner(2);

        std::atomic<bool> heavy_started{false};
        std::atomic<bool> release_heavy{false};
        heavy_jobs.submit({"Long ASR Job", "plugin-asr", "asr", {}},
            [&](omnidesk::JobHandle&, std::string&) {
                heavy_started.store(true);
                while (!release_heavy.load()) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
                return true;
            });
        for (int i = 0; i < 100 && !heavy_started.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(heavy_started.load());

        // UiTaskRunner must execute immediately even while heavy JobCenter is completely saturated
        std::atomic<bool> fast_done{false};
        auto t0 = std::chrono::steady_clock::now();
        ui_runner.submit([&](const std::shared_ptr<omnidesk::CancelToken>&) {
            fast_done.store(true);
        });
        for (int i = 0; i < 100 && !fast_done.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(fast_done.load());
        TEST_CHECK(std::chrono::steady_clock::now() - t0 < std::chrono::milliseconds(500));

        // Lane superseding cancels older token in same lane
        std::atomic<bool> first_entered{false};
        std::atomic<bool> first_cancelled{false};
        std::atomic<bool> second_done{false};
        auto t1 = ui_runner.submitInLane("feed", [&](const std::shared_ptr<omnidesk::CancelToken>& c) {
            first_entered.store(true);
            for (int i = 0; i < 100 && !c->isCancelled(); ++i) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            first_cancelled.store(c->isCancelled());
        });
        for (int i = 0; i < 100 && !first_entered.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        auto t2 = ui_runner.submitInLane("feed", [&](const std::shared_ptr<omnidesk::CancelToken>& c) {
            TEST_CHECK(!c->isCancelled());
            second_done.store(true);
        });
        TEST_CHECK(t1 && t1->isCancelled());
        for (int i = 0; i < 200 && !second_done.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(first_cancelled.load());
        TEST_CHECK(second_done.load());

        // runForUi with custom dispatcher
        std::atomic<int> ui_applied{0};
        ui_runner.setUiDispatcher([](std::function<void()> fn) { fn(); });
        auto ui_alive = std::make_shared<std::atomic<bool>>(true);
        ui_runner.runForUi("detail", ui_alive,
            [](const std::shared_ptr<omnidesk::CancelToken>&) { return 42; },
            [&](int val) { ui_applied.store(val); });
        for (int i = 0; i < 100 && ui_applied.load() == 0; ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        TEST_CHECK(ui_applied.load() == 42);

        release_heavy.store(true);
        ui_runner.shutdown(1000);
        heavy_jobs.shutdown(1000);
    }

    // 24. DsoViewLeaseRegistry lifecycle, revocation & zero-lease callbacks
    {
        auto& leases = omnidesk::DsoViewLeaseRegistry::instance();
        const std::string pid = "plugin-lease-test";
        leases.beginPluginSession(pid);
        TEST_CHECK(!leases.isRevoking(pid));
        TEST_CHECK(!leases.hasActiveLeases(pid));

        auto l1 = leases.acquireLease(pid);
        auto l2 = leases.acquireLease(pid);
        TEST_CHECK(l1 != nullptr && l2 != nullptr);
        TEST_CHECK(leases.hasActiveLeases(pid));
        TEST_CHECK(leases.activeLeaseCount(pid) == 2);

        std::atomic<bool> drained{false};
        leases.onAllLeasesReleased(pid, [&]() { drained.store(true); });

        l1.reset();
        TEST_CHECK(leases.activeLeaseCount(pid) == 1);
        TEST_CHECK(!drained.load());

        leases.revokePlugin(pid, 0);
        TEST_CHECK(leases.isRevoking(pid));
        TEST_CHECK(leases.acquireLease(pid) == nullptr);

        l2.reset();
        TEST_CHECK(!leases.hasActiveLeases(pid));
        TEST_CHECK(drained.load());
        leases.beginPluginSession(pid);
    }

    // 25. PlaybackCoordinator session arbitration, breakpoint resume, SponsorBlock/SubHunter & MPRIS2 snapshot
    {
        const std::string pb_dir =
            (fs::temp_directory_path() / ("omnidesk_pb_test_" + std::to_string(::getpid()))).string();
        fs::remove_all(pb_dir);
        fs::create_directories(pb_dir);
        const std::string resume_file = pb_dir + "/playback_resume.json";

        omnidesk::CapabilityRegistry pb_caps;
        omnidesk::EntityBus pb_bus;
        omnidesk::JobCenter pb_jobs(&pb_bus, 1);
        omnidesk::UiTaskRunner pb_ui(2);
        omnidesk::MPVCore test_mpv;

        std::atomic<bool> timeline_called{false};
        pb_caps.registerCapability("player.timeline.v1", "plugin-sponsorblock",
            [&](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                timeline_called.store(true);
                if (req.value("action", "") == "fetch_segments") {
                    return {true, {
                        {"chapters", nlohmann::json::array({
                            {{"start_sec", 0.0}, {"end_sec", 10.0}, {"label", "Intro"}, {"kind", "content"}},
                            {{"start_sec", 10.0}, {"end_sec", 25.0}, {"label", "Sponsor"}, {"kind", "sponsor"}},
                        })}
                    }, "", "plugin-sponsorblock", false};
                }
                return {true, {}, "", "plugin-sponsorblock", false};
            });
        pb_caps.registerCapability("subtitle.fetch.v1", "plugin-sub-hunter",
            [](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                const std::string act = req.value("action", "");
                if (act == "search") {
                    return {true, {{"candidates", nlohmann::json::array({{{"provider", "assrt"}, {"id", "sub1"}}})}}, "", "plugin-sub-hunter", false};
                }
                if (act == "download") {
                    return {true, {{"files", nlohmann::json::array({{{"path", "/tmp/test_sub.srt"}, {"cue_count", 12}}})}}, "", "plugin-sub-hunter", false};
                }
                return {true, {}, "", "plugin-sub-hunter", false};
            });

        auto& coord = omnidesk::PlaybackCoordinator::instance();
        coord.configure(resume_file, &pb_caps, &pb_bus, &pb_jobs, &pb_ui, &test_mpv);
        coord.registerCapabilities(pb_caps);
        TEST_CHECK(pb_caps.has("playback.session.v1"));

        omnidesk::PlaybackTrack m1;
        m1.item = {"track-1", "plugin-music", "Clair de Lune", "Debussy", "music", "Music", "", "https://example.com/m1.flac", "FLAC", 300.0, 0.0, 0, true};
        m1.album = "Suite Bergamasque";
        omnidesk::PlaybackTrack m2;
        m2.item = {"track-2", "plugin-music", "Arabesque", "Debussy", "music", "Music", "", "https://example.com/m2.flac", "FLAC", 240.0, 0.0, 0, false};

        uint64_t music_lease = coord.playMusicQueue("plugin-music", {m1, m2}, 0);
        TEST_CHECK(music_lease != 0);
        coord.seekAbsolute(45.0);
        auto st_music = coord.state();
        TEST_CHECK(st_music.active_role == omnidesk::PlaybackRole::BackgroundMusic);
        TEST_CHECK(st_music.music_queue.size() == 2);

        // Verify MPRIS2 snapshot mapping
        auto mpris_snap = omnidesk::Mpris2Service::snapshotFromSession(st_music);
        TEST_CHECK(mpris_snap.playback_status == "Playing");
        TEST_CHECK(mpris_snap.title == "Clair de Lune");
        TEST_CHECK(mpris_snap.artists.size() == 1 && mpris_snap.artists[0] == "Debussy");
        TEST_CHECK(mpris_snap.album == "Suite Bergamasque");
        TEST_CHECK(mpris_snap.can_go_next);

        // ForegroundVideo preempts BackgroundMusic and saves exact breakpoint 45.0s
        omnidesk::PlaybackTrack v1;
        v1.item = {"BV1test", "plugin-bilibili", "Kernel Talk", "TechLab", "video", "Bilibili", "", "https://example.com/v1.mp4", "4K", 600.0, 0.0, 0, false};
        v1.auto_enrich_timeline = true;
        uint64_t vid_lease = coord.play(omnidesk::PlaybackRole::ForegroundVideo, "host.media_workbench", v1);
        TEST_CHECK(vid_lease != 0);

        for (int i = 0; i < 100 && !timeline_called.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(timeline_called.load());
        // Wait briefly for timeline update to land in session
        for (int i = 0; i < 100 && coord.state().active_track.chapters.empty(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        auto st_vid = coord.state();
        TEST_CHECK(st_vid.active_role == omnidesk::PlaybackRole::ForegroundVideo);
        TEST_CHECK(st_vid.has_suspended_music);
        TEST_CHECK(st_vid.suspended_music_track.item.id == "track-1");
        TEST_CHECK(st_vid.suspended_music_position_sec >= 44.5);
        TEST_CHECK(st_vid.active_track.chapters.size() == 2);

        // SponsorBlock skip inside [10.0, 25.0)
        coord.seekAbsolute(15.0);
        TEST_CHECK(coord.skipActiveSponsor());
        TEST_CHECK(coord.state().mpv.position_sec >= 25.0);

        // SubHunter subtitle attachment
        coord.attachSubtitlesAsync(true);
        for (int i = 0; i < 100 && !coord.state().subtitles_attached; ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(coord.state().subtitles_attached);

        // Releasing video lease restores BackgroundMusic at saved breakpoint
        TEST_CHECK(coord.releaseLease(vid_lease, true));
        auto st_resumed = coord.state();
        TEST_CHECK(st_resumed.active_role == omnidesk::PlaybackRole::BackgroundMusic);
        TEST_CHECK(st_resumed.active_track.item.id == "track-1");
        TEST_CHECK(st_resumed.active_track.options.start_seconds >= 44.5);

        // Verify breakpoint persistence across shutdown + re-configure
        coord.saveResumePosition("BV1test", "Kernel Talk", 123.5, 600.0);
        coord.shutdown();
        coord.configure(resume_file, &pb_caps, &pb_bus, &pb_jobs, &pb_ui, &test_mpv);
        TEST_CHECK(coord.getResumePosition("BV1test") > 123.0);
        coord.shutdown();
        test_mpv.shutdown();
        pb_ui.shutdown(1000);
        pb_jobs.shutdown(1000);
        fs::remove_all(pb_dir);
    }

    // 26. CommentTreeView tree flattening / OP / collapse & SelectionActionPopover token extraction
    {
        omnidesk::CommentNode c1{"c1", "alice", "", "Root comment", "", "1m", 10, false, {}};
        omnidesk::CommentNode c1_1{"c1-1", "bob", "", "Nested reply", "", "1m", 5, true, {}};
        omnidesk::CommentNode c1_1_1{"c1-1-1", "alice", "", "Deep hidden reply", "", "1m", 2, false, {}};
        c1_1.replies.push_back(c1_1_1);
        c1.replies.push_back(c1_1);
        omnidesk::CommentNode c2{"c2", "carol", "OP", "Second root", "", "2m", 1, false, {}};

        std::set<std::string> collapsed{"c1-1"};
        auto flat = omnidesk::CommentTreeView::flattenTree({c1, c2}, collapsed, "alice");
        TEST_CHECK(flat.size() == 3);
        TEST_CHECK(flat[0].node_id == "c1" && flat[0].depth == 0 && flat[0].is_op);
        TEST_CHECK(flat[1].node_id == "c1-1" && flat[1].depth == 1 && !flat[1].is_op && flat[1].collapsed && flat[1].hidden_descendants == 1);
        TEST_CHECK(flat[2].node_id == "c2" && flat[2].depth == 0 && flat[2].is_op);

        auto tokens = omnidesk::SelectionActionPopover::extractSelectableTokens(
            "Ephemeral staging containers avoid degradation and Degradation.", 6);
        TEST_CHECK(tokens.size() == 5);
        TEST_CHECK(tokens[0] == "Ephemeral");
    }

    // 27. SearchIndexService SQLite FTS5 cross-domain indexing & search.index.v1 RPC
    {
        const std::string srch_dir =
            (fs::temp_directory_path() / ("omnidesk_search_test_" + std::to_string(::getpid()))).string();
        fs::remove_all(srch_dir);
        fs::create_directories(srch_dir);

        struct SearchMockBookSource final : public omnidesk::IBookSource {
            std::string sourceId() const override { return "plugin-ebook"; }
            std::string displayName() const override { return "eBook"; }
            std::vector<omnidesk::BookItem> listBooks(const std::string&, const std::string&) override {
                return {{"b1", "plugin-ebook", "Designing Data-Intensive Applications 书架", "Martin Kleppmann",
                         "epub", "/books/ddia.epub", "", 10, 400, 2.5, true, 5, false}};
            }
            omnidesk::BookSpread fetchSpread(const std::string& id, int page) override {
                return {id, "Ch1", page, 400, {}};
            }
            std::vector<omnidesk::VocabCard> listVocab() override {
                return {{"ephemeral", "ephemeral", "/ɪˈfɛmərəl/", "epi + hemera", "短暂的",
                         "Ephemeral staging containers", "DDIA", 1, 0, 2.5}};
            }
        };
        struct SearchMockTextSource final : public omnidesk::ITextSource {
            std::string sourceId() const override { return "plugin-ai-curator"; }
            std::string displayName() const override { return "AI Curator"; }
            std::string category() const override { return "obsidian"; }
            std::vector<omnidesk::TextFeedItem> fetchFeed(const std::string&) override {
                return {{"obs-1", "plugin-ai-curator", "Obsidian 往期精选", "obsidian",
                         "神经网络与微内核精选", "深度解析", "curator", "2026-10-01",
                         "https://example.com", "", {}, {"obsidian", "精选"}, 3, 10, true}};
            }
            omnidesk::TextArticle fetchArticle(const std::string& id) override {
                omnidesk::TextArticle a;
                a.id = id;
                return a;
            }
        };
        struct SearchMockCuratedMedia final : public omnidesk::IMediaSource {
            std::string sourceId() const override { return "plugin-ai-curator"; }
            std::string displayName() const override { return "Curated Media"; }
            std::vector<std::string> categories() const override { return {"all"}; }
            std::vector<omnidesk::MediaItem> fetchItems(const std::string&, const std::string&) override {
                omnidesk::MediaItem item;
                item.id = "archived-song";
                item.source_id = sourceId();
                item.title = "Archived Queue Regression";
                item.creator = "Archive Artist";
                item.kind = "music";
                item.play_url = "https://example.com/song";
                item.extra = {{"business_date", "2026-09-04"}, {"curation_mode", "daily_immutable"}};
                return {item};
            }
        };
        omnidesk::DomainRegistry s_dom;
        omnidesk::CapabilityRegistry s_caps;
        s_dom.registerMediaSource("plugin-ai-curator", std::make_shared<SearchMockCuratedMedia>());
        s_dom.registerBookSource("plugin-ebook", std::make_shared<SearchMockBookSource>());
        s_dom.registerTextSource("plugin-ai-curator", std::make_shared<SearchMockTextSource>());
        s_dom.registerMediaSource("plugin-music", std::make_shared<DummyMediaSource>());
        s_dom.registerWorkspaceTab({"tab-custom", "plugin-music", "Apple Music 乐评", omnidesk::ContentDomain::Media, "Music", []() -> brls::View* { return nullptr; }});
        s_caps.registerCapability("media.clip_jump.v1", "plugin-fnos-nas",
            [](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                const std::string q = req.value("query", "");
                if (q.find("ephemeral") != std::string::npos || q.find("台词") != std::string::npos) {
                    return {true, {{"clips", nlohmann::json::array({
                        {{"series", "Severance"}, {"episode", "S01E01"}, {"start_sec", 88.0}, {"end_sec", 94.0},
                         {"line_en", "Everything here is ephemeral."}, {"line_zh", "这里的一切都是短暂的台词示例。"},
                         {"play_url", "file:///nas/severance.mkv"}}
                    })}}, "", "plugin-fnos-nas", false};
                }
                return {true, {{"clips", nlohmann::json::array()}}, "", "plugin-fnos-nas", false};
            });

        omnidesk::SearchIndexService idx;
        TEST_CHECK(idx.open(srch_dir + "/search_index.db"));
        const size_t hydrated = idx.hydrateEcosystem(&s_dom, &s_caps, srch_dir, srch_dir);
        TEST_CHECK(hydrated >= 10);
        idx.registerCapability(s_caps, &s_dom, srch_dir, srch_dir);
        TEST_CHECK(s_caps.has("search.index.v1"));

        // Archive navigation retains metadata needed to select its date-scoped queue.
        auto archived = s_caps.call("search.index.v1", {
            {"action", "query"}, {"q", "Archived Queue Regression"}, {"kind", "music_review"},
        });
        TEST_CHECK(archived.ok && archived.data["hits"].size() == 1);
        const auto& archive_payload = archived.data["hits"][0]["payload"];
        TEST_CHECK(archive_payload.value("target", "") == "media");
        TEST_CHECK(archive_payload.value("creator", "") == "Archive Artist");
        TEST_CHECK(archive_payload["extra"].value("business_date", "") == "2026-09-04");
        TEST_CHECK(archive_payload["extra"].value("curation_mode", "") == "daily_immutable");

        // English 3+ char FTS5 trigram query
        auto r_vocab = s_caps.call("search.index.v1", {{"action", "query"}, {"q", "ephemeral"}});
        TEST_CHECK(r_vocab.ok);
        TEST_CHECK(r_vocab.data["total"].get<size_t>() >= 2); // vocab + live nas_clip

        // CJK 3+ char FTS5 trigram query
        auto r_cjk3 = s_caps.call("search.index.v1", {{"action", "query"}, {"q", "插件市"}});
        TEST_CHECK(r_cjk3.ok && r_cjk3.data["total"].get<size_t>() >= 1);

        // CJK 1-char & 2-char fallback query
        auto r_cjk2 = s_caps.call("search.index.v1", {{"action", "query"}, {"q", "乐评"}});
        TEST_CHECK(r_cjk2.ok && r_cjk2.data["total"].get<size_t>() >= 1);
        auto r_cjk1 = s_caps.call("search.index.v1", {{"action", "query"}, {"q", "书"}});
        TEST_CHECK(r_cjk1.ok && r_cjk1.data["total"].get<size_t>() >= 1);

        std::string cjk_body;
        for (int i = 0; i < 40; ++i) cjk_body += "中";
        cjk_body += " boundarymarker ";
        for (int i = 0; i < 80; ++i) cjk_body += "文";
        auto r_cjk_snip = s_caps.call("search.index.v1", {
            {"action", "upsert"},
            {"doc", {{"doc_id", "custom:cjk-snippet"}, {"kind", "journal_article"},
                     {"domain", "text"}, {"title", "中文截断回归"}, {"body", cjk_body}}},
        });
        TEST_CHECK(r_cjk_snip.ok);
        auto r_cjk_snip_hit = s_caps.call("search.index.v1", {
            {"action", "query"}, {"q", "boundarymarker"}, {"kind", "journal_article"},
        });
        TEST_CHECK(r_cjk_snip_hit.ok && r_cjk_snip_hit.data["hits"].size() == 1);
        const std::string snippet = r_cjk_snip_hit.data["hits"][0].value("snippet", "");
        TEST_CHECK(snippet.find("boundarymarker") != std::string::npos);
        bool snippet_json_ok = true;
        try {
            nlohmann::json probe = {{"snippet", snippet}};
            (void)probe.dump();
        } catch (...) {
            snippet_json_ok = false;
        }
        TEST_CHECK(snippet_json_ok);

        // Custom upsert + remove + stats
        auto r_up = s_caps.call("search.index.v1", {
            {"action", "upsert"},
            {"doc", {{"doc_id", "custom:1"}, {"kind", "music_review"}, {"domain", "media"}, {"title", "Bill Evans Waltz for Debby"}, {"body", "深夜写C++爵士乐评"}}},
        });
        TEST_CHECK(r_up.ok && r_up.data["upserted"].get<size_t>() == 1);
        auto r_evans = s_caps.call("search.index.v1", {{"action", "query"}, {"q", "爵士乐评"}, {"kind", "music_review"}});
        TEST_CHECK(r_evans.ok && r_evans.data["total"].get<size_t>() >= 1);

        auto r_stats = s_caps.call("search.index.v1", {{"action", "stats"}});
        TEST_CHECK(r_stats.ok && r_stats.data["total_docs"].get<size_t>() >= hydrated);

        idx.close();
        fs::remove_all(srch_dir);
    }

    // 28. CommonMark + GFM + Obsidian Markdown Parser & Excerpt Sanitizer
    {
        // Inline spans: bold, italic, snake_case safety, inline code, strike, highlight, links, wikilinks, math
        const std::string inline_sample =
            "Published in *Nature Chemical Biology* (`reviewer_synthesis`) with **anecdote：** "
            "~~old~~ ==key== [DOI](https://doi.org/10.1038/s41589-026-0001-x) and "
            "[[50-courses/English/Daily/2026-11-15|Daily 2026-11-15]] plus $x_1^2 + \\alpha$.";
        const auto spans = omnidesk::parseMarkdownInlines(inline_sample);
        bool saw_italic_journal = false;
        bool saw_code_snake = false;
        bool saw_cjk_bold = false;
        bool saw_strike = false;
        bool saw_highlight = false;
        bool saw_doi_link = false;
        bool saw_wikilink = false;
        bool saw_math = false;
        for (const auto& sp : spans) {
            if (sp.italic && sp.text == "Nature Chemical Biology") saw_italic_journal = true;
            if (sp.code && sp.text == "reviewer_synthesis") saw_code_snake = true;
            if (sp.bold && sp.text == "anecdote：") saw_cjk_bold = true;
            if (sp.strikethrough && sp.text == "old") saw_strike = true;
            if (sp.highlight && sp.text == "key") saw_highlight = true;
            if (sp.link_url == "https://doi.org/10.1038/s41589-026-0001-x" && sp.text == "DOI") saw_doi_link = true;
            if (sp.link_url == "wikilink:50-courses/English/Daily/2026-11-15" && sp.text == "Daily 2026-11-15") {
                saw_wikilink = true;
            }
            if (sp.math && sp.text.find("α") != std::string::npos) saw_math = true;
        }
        TEST_CHECK(saw_italic_journal);
        TEST_CHECK(saw_code_snake);
        TEST_CHECK(saw_cjk_bold);
        TEST_CHECK(saw_strike);
        TEST_CHECK(saw_highlight);
        TEST_CHECK(saw_doi_link);
        TEST_CHECK(saw_wikilink);
        TEST_CHECK(saw_math);

        // Bare snake_case outside backticks must NOT trigger emphasis
        const auto snake_spans = omnidesk::parseMarkdownInlines("verification_method: creator_official_page");
        for (const auto& sp : snake_spans) {
            TEST_CHECK(!sp.italic && !sp.bold);
        }
        TEST_CHECK(omnidesk::stripMarkdownToPlainText("verification_method: creator_official_page") ==
                   "verification_method: creator_official_page");

        // Block-level AST: headings, Obsidian callouts (including foldable [!answer]-), loose & task lists, GFM table, code, math
        const std::string doc_md = R"MD(# Weekly Long English — 2026-11-15

> [!info] 来源与范围提醒
> 本笔记基于 *Psyche* 长文整理。

1. **The old question**
   Why meaning feels elusive.

2. **The reframing**
   Treat meaning as a testable hypothesis.

- [x] Read section 1
- [ ] Write summary

> [!answer]- 参考译文与解析
> Meaningful work requires deliberate practice.

| 项目 | 英文要求 |
| :--- | ---: |
| 复述 | 4-5 句 |
| 立场 | 120 词 |

```cpp
int main() { return 0; }
```

$$
\sum_{i=1}^{n} x_i = \beta
$$
)MD";
        const auto blocks = omnidesk::parseMarkdownBlocks(doc_md);
        bool has_h1 = false;
        bool has_info_callout = false;
        bool has_foldable_answer = false;
        bool has_ordered_list = false;
        bool has_task_list = false;
        bool has_table = false;
        bool has_code = false;
        bool has_math_blk = false;
        for (const auto& b : blocks) {
            if (b.kind == omnidesk::MarkdownBlockKind::Heading && b.heading_level == 1) has_h1 = true;
            if (b.kind == omnidesk::MarkdownBlockKind::Callout && b.callout_type == "info") {
                has_info_callout = true;
            }
            if (b.kind == omnidesk::MarkdownBlockKind::Callout && b.callout_type == "answer" &&
                b.callout_foldable && b.callout_default_collapsed) {
                has_foldable_answer = true;
            }
            if (b.kind == omnidesk::MarkdownBlockKind::List && !b.list_items.empty() && b.list_items[0].ordered) {
                if (b.list_items.size() == 2 && b.list_items[1].number == 2) has_ordered_list = true;
            }
            if (b.kind == omnidesk::MarkdownBlockKind::List && !b.list_items.empty() && b.list_items[0].task_state == 1) {
                has_task_list = true;
            }
            if (b.kind == omnidesk::MarkdownBlockKind::Table && b.table_headers.size() == 2 &&
                b.table_rows.size() == 2 &&
                b.table_aligns[1] == omnidesk::MarkdownTableAlign::Right) {
                has_table = true;
            }
            if (b.kind == omnidesk::MarkdownBlockKind::CodeBlock && b.code_lang == "cpp") has_code = true;
            if (b.kind == omnidesk::MarkdownBlockKind::MathBlock && b.text.find("∑") != std::string::npos) {
                has_math_blk = true;
            }
        }
        TEST_CHECK(has_h1);
        TEST_CHECK(has_info_callout);
        TEST_CHECK(has_foldable_answer);
        TEST_CHECK(has_ordered_list);
        TEST_CHECK(has_task_list);
        TEST_CHECK(has_table);
        TEST_CHECK(has_code);
        TEST_CHECK(has_math_blk);

        const auto comment_blocks = omnidesk::parseMarkdownBlocks(
            "```html\n<!-- keep -->\n```\n\nafter\n");
        bool kept_code_comment = false;
        bool kept_tail = false;
        for (const auto& b : comment_blocks) {
            if (b.kind == omnidesk::MarkdownBlockKind::CodeBlock &&
                b.text.find("<!-- keep -->") != std::string::npos) {
                kept_code_comment = true;
            }
            if (b.kind == omnidesk::MarkdownBlockKind::Paragraph && b.text == "after") {
                kept_tail = true;
            }
        }
        TEST_CHECK(kept_code_comment);
        TEST_CHECK(kept_tail);

        const auto unclosed_comment_blocks =
            omnidesk::parseMarkdownBlocks("before\n\n<!-- unclosed\n\nstill visible\n");
        bool kept_unclosed_tail = false;
        for (const auto& b : unclosed_comment_blocks) {
            if (b.text.find("still visible") != std::string::npos) {
                kept_unclosed_tail = true;
            }
        }
        TEST_CHECK(kept_unclosed_tail);

        // Excerpt sanitizer strips # headings and [!callout] header tags while preserving clean inline *emphasis*
        const std::string ex = omnidesk::sanitizeMarkdownExcerpt(doc_md, 120);
        TEST_CHECK(ex.find("# Weekly") == std::string::npos);
        TEST_CHECK(ex.find("[!info]") == std::string::npos);
        TEST_CHECK(ex.find("*Psyche*") != std::string::npos);
    }

    // 29. MPVCore loopback TLS/proxy options & PlaybackCoordinator deferred Apple Music resolution for AI-curated tracks
    {
        omnidesk::MediaRequestOptions loop_opts;
        loop_opts.url = "https://127.0.0.1:20025/api/v1/playback/sess-1/audio";
        const std::string loop_loadfile = omnidesk::MPVCore::buildLoadfileOptions(loop_opts);
        TEST_CHECK(loop_loadfile.find("tls-verify=no") != std::string::npos);
        TEST_CHECK(loop_loadfile.find("http-proxy=\"\"") != std::string::npos);
        TEST_CHECK(loop_loadfile.find("network-timeout=30") != std::string::npos);

        omnidesk::MediaRequestOptions ext_opts;
        ext_opts.url = "https://cdn.example.com/audio.flac";
        const std::string ext_loadfile = omnidesk::MPVCore::buildLoadfileOptions(ext_opts);
        TEST_CHECK(ext_loadfile.find("tls-verify=no") == std::string::npos);
        TEST_CHECK(ext_loadfile.find("network-timeout=10") != std::string::npos);

        const std::string pb_dir =
            (fs::temp_directory_path() / ("omnidesk_am_pb_test_" + std::to_string(::getpid()))).string();
        fs::remove_all(pb_dir);
        fs::create_directories(pb_dir);

        omnidesk::CapabilityRegistry am_caps;
        omnidesk::EntityBus am_bus;
        omnidesk::JobCenter am_jobs(&am_bus, 1);
        omnidesk::UiTaskRunner am_ui(2);
        omnidesk::MPVCore am_mpv;

        std::mutex resolved_mu;
        std::vector<std::pair<std::string, std::string>> playurl_calls;
        am_caps.registerCapability("media.source.v1", "plugin-music",
            [&](const nlohmann::json& req) -> omnidesk::CapabilityResult {
                const std::string act = req.value("action", "");
                const std::string sid = req.value("id", "");
                const std::string sf = req.value("storefront", "");
                if (act == "playurl") {
                    {
                        std::lock_guard<std::mutex> lk(resolved_mu);
                        playurl_calls.emplace_back(sid, sf);
                    }
                    return {true, {
                        {"id", sid},
                        {"storefront", sf},
                        {"audio_url", "https://127.0.0.1:20025/api/v1/playback/sess-" + sid + "/audio"},
                        {"cover_url", "https://is1-ssl.mzstatic.com/image/thumb/" + sid + "/600x600bb.jpg"},
                        {"duration_sec", 342.0},
                        {"quality_label", "ALAC 24bit/96kHz"},
                        {"album", "De Todas las Flores"},
                    }, "", "plugin-music", false};
                }
                if (act == "lyrics") {
                    return {true, {
                        {"id", sid},
                        {"lines", nlohmann::json::array({
                            {{"time_sec", 12.5}, {"original", "Canto a la vida"}, {"translation", "向生命歌唱"}},
                        })},
                    }, "", "plugin-music", false};
                }
                return {false, {}, "unsupported", "plugin-music", false};
            });

        auto& coord = omnidesk::PlaybackCoordinator::instance();
        coord.configure(pb_dir + "/resume.json", &am_caps, &am_bus, &am_jobs, &am_ui, &am_mpv);

        omnidesk::PlaybackTrack t1;
        t1.item = {"am-cn-1643009587", "plugin-ai-curator", "Muerte", "Natalia Lafourcade",
                   "music", "Apple Music CN", "", "https://music.apple.com/cn/song/1643009587",
                   "ALAC", 0.0, 0.0, 0, false,
                   {{"apple_music_song_id", "1643009587"}, {"storefront", "cn"}, {"curation_mode", "daily_immutable"}}};
        t1.options.url = t1.item.play_url;

        omnidesk::PlaybackTrack t2;
        t2.item = {"ix-am-1833283533", "plugin-ai-curator", "Tobiume", "Susumu Yokota",
                   "music", "Apple Music JP", "", "https://music.apple.com/jp/album/sakura/1833283500?i=1833283533",
                   "ALAC", 0.0, 0.0, 0, false,
                   {{"curation_mode", "interactive_ephemeral"}}};
        t2.options.url = t2.item.play_url;

        uint64_t lease = coord.playMusicQueue("host.media_workbench", {t1, t2}, 0);
        TEST_CHECK(lease != 0);
        // Must NEVER load the HTML catalog page into MPVCore
        TEST_CHECK(am_mpv.state().current_url.find("music.apple.com") == std::string::npos);

        for (int i = 0; i < 200; ++i) {
            if (am_mpv.state().current_url.find("sess-1643009587/audio") != std::string::npos &&
                !coord.state().active_track.lyrics.empty()) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        auto st1 = coord.state();
        TEST_CHECK(st1.active_role == omnidesk::PlaybackRole::BackgroundMusic);
        TEST_CHECK(am_mpv.state().current_url == "https://127.0.0.1:20025/api/v1/playback/sess-1643009587/audio");
        TEST_CHECK(st1.active_track.item.cover_url.find("1643009587") != std::string::npos);
        TEST_CHECK(st1.active_track.item.duration_sec > 300.0);
        TEST_CHECK(st1.active_track.item.quality_label == "ALAC 24bit/96kHz");
        TEST_CHECK(st1.active_track.lyrics.size() == 1 && st1.active_track.lyrics[0].translation == "向生命歌唱");

        // Advance to second track with ?i=1833283533 and storefront jp
        coord.nextTrack();
        for (int i = 0; i < 200; ++i) {
            if (am_mpv.state().current_url.find("sess-1833283533/audio") != std::string::npos) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(am_mpv.state().current_url == "https://127.0.0.1:20025/api/v1/playback/sess-1833283533/audio");
        {
            std::lock_guard<std::mutex> lk(resolved_mu);
            TEST_CHECK(playurl_calls.size() == 2);
            TEST_CHECK(playurl_calls[0].first == "1643009587" && playurl_calls[0].second == "cn");
            TEST_CHECK(playurl_calls[1].first == "1833283533" && playurl_calls[1].second == "jp");
        }

        // A missing video provider must fail explicitly instead of falling back to
        // the generic media.source.v1 provider (plugin-music).
        const size_t calls_before_missing_video = playurl_calls.size();
        omnidesk::PlaybackTrack missing_video;
        missing_video.item = {"BV1missing", "plugin-bilibili", "Missing provider", "Test",
                              "video", "Bilibili", "", "", "1080p", 60.0, 0.0, 0, false};
        TEST_CHECK(coord.play(omnidesk::PlaybackRole::ForegroundVideo, "host.test.missing_video",
                              missing_video) != 0);
        for (int i = 0; i < 200; ++i) {
            if (coord.state().mpv.last_error.find("plugin-bilibili") != std::string::npos) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(coord.state().mpv.last_error.find("plugin-bilibili") != std::string::npos);
        {
            std::lock_guard<std::mutex> lk(resolved_mu);
            TEST_CHECK(playurl_calls.size() == calls_before_missing_video);
        }
        coord.stop();

        coord.shutdown();
        am_mpv.shutdown();
        am_ui.shutdown(1000);
        am_jobs.shutdown(1000);
        fs::remove_all(pb_dir);
    }

    // 30. WorkerMediaSource / WorkerTextSource metadata caching & reload invalidation
    {
        const fs::path meta_dir =
            fs::temp_directory_path() / ("omnidesk_meta_cache_" + std::to_string(::getpid()));
        fs::remove_all(meta_dir, ec);
        fs::create_directories(meta_dir, ec);
        const fs::path meta_script = meta_dir / "worker_meta_cache.py";
        {
            std::ofstream ofs(meta_script);
            ofs << R"PY(
import json, os, sys

ver = os.environ.get("OMNIDESK_META_VER", "v1")
for line in sys.stdin:
    s = line.strip()
    if not s:
        continue
    req = json.loads(s)
    op = req.get("op", "")
    sid = req.get("source_id", "")
    if op == "media.categories":
        cats = [] if sid == "empty-media" else ["video", f"cat-{ver}"]
        print(json.dumps({"ok": True, "data": {"categories": cats}}), flush=True)
    elif op == "text.channels":
        chans = [] if sid == "empty-text" else [
            {"id": "hot", "name": f"Hot-{ver}", "badge": "10", "requires_auth": False},
            {"id": "latest", "name": "Latest", "badge": "", "requires_auth": False},
        ]
        print(json.dumps({"ok": True, "data": {"channels": chans}}), flush=True)
    else:
        print(json.dumps({"ok": False, "data": {}, "error": "unknown op"}), flush=True)
)PY";
        }
        TEST_CHECK(fs::exists(meta_script));

        omnidesk::WorkerSupervisor meta_sup;
        omnidesk::WorkerSpec spec;
        spec.worker_id = "meta-worker";
        spec.plugin_id = "plugin-meta";
        spec.executable = "python3";
        spec.args = {"-u", meta_script.string()};
        spec.env = {{"OMNIDESK_META_VER", "v1"}};
        spec.idle_timeout_ms = 200;
        spec.stop_grace_ms = 200;
        TEST_CHECK(meta_sup.registerWorker(spec));

        omnidesk::WorkerMediaSource msrc(meta_sup, "meta-worker", "plugin-meta", "m-src", "Media");
        omnidesk::WorkerMediaSource msrc_empty(meta_sup, "meta-worker", "plugin-meta", "empty-media", "EmptyMedia");
        omnidesk::WorkerTextSource tsrc(meta_sup, "meta-worker", "plugin-meta", "t-src", "Text", "multi");
        omnidesk::WorkerTextSource tsrc_empty(meta_sup, "meta-worker", "plugin-meta", "empty-text", "EmptyText", "blog");

        // First access triggers exactly 1 IPC per uncached source; repeated calls hit memory cache
        auto cats1 = msrc.categories();
        TEST_CHECK(cats1.size() == 2 && cats1[1] == "cat-v1");
        const uint64_t reqs_after_m1 = meta_sup.status("meta-worker")->requests_served;
        TEST_CHECK(reqs_after_m1 == 1);
        for (int i = 0; i < 5; ++i) {
            auto cats_cached = msrc.categories();
            TEST_CHECK(cats_cached == cats1);
        }
        TEST_CHECK(meta_sup.status("meta-worker")->requests_served == reqs_after_m1);

        auto chans1 = tsrc.channels();
        TEST_CHECK(chans1.size() == 2 && chans1[0].id == "hot" && chans1[0].name == "Hot-v1");
        const uint64_t reqs_after_t1 = meta_sup.status("meta-worker")->requests_served;
        TEST_CHECK(reqs_after_t1 == 2);
        for (int i = 0; i < 5; ++i) {
            auto chans_cached = tsrc.channels();
            TEST_CHECK(chans_cached.size() == 2 && chans_cached[0].name == "Hot-v1");
        }
        TEST_CHECK(meta_sup.status("meta-worker")->requests_served == reqs_after_t1);

        // Empty array responses ("categories": [] / "channels": []) must also be cached cleanly
        TEST_CHECK(msrc_empty.categories().empty());
        TEST_CHECK(tsrc_empty.channels().empty());
        const uint64_t reqs_after_empty = meta_sup.status("meta-worker")->requests_served;
        TEST_CHECK(reqs_after_empty == 4);
        TEST_CHECK(msrc_empty.categories().empty());
        TEST_CHECK(tsrc_empty.channels().empty());
        TEST_CHECK(meta_sup.status("meta-worker")->requests_served == reqs_after_empty);

        // Let idle watchdog stop child process; reading cached metadata must NOT respawn child
        for (int w = 0; w < 30; ++w) {
            std::this_thread::sleep_for(std::chrono::milliseconds(30));
            if (!meta_sup.status("meta-worker")->running) break;
        }
        TEST_CHECK(!meta_sup.status("meta-worker")->running);
        TEST_CHECK(msrc.categories().size() == 2);
        TEST_CHECK(tsrc.channels().size() == 2);
        TEST_CHECK(!meta_sup.status("meta-worker")->running);

        // Explicit invalidateMetadataCache() forces a fresh IPC on next read
        tsrc.invalidateMetadataCache();
        TEST_CHECK(tsrc.channels().size() == 2);
        TEST_CHECK(meta_sup.status("meta-worker")->requests_served == reqs_after_empty + 1);

        // Re-registering the worker (simulating worker/plugin reload) automatically invalidates cache
        spec.env = {{"OMNIDESK_META_VER", "v2"}};
        TEST_CHECK(meta_sup.registerWorker(spec));
        auto cats2 = msrc.categories();
        auto chans2 = tsrc.channels();
        TEST_CHECK(cats2.size() == 2 && cats2[1] == "cat-v2");
        TEST_CHECK(chans2.size() == 2 && chans2[0].name == "Hot-v2");
        TEST_CHECK(meta_sup.status("meta-worker")->requests_served == 2);
        // Subsequent reads on v2 remain cached
        TEST_CHECK(msrc.categories()[1] == "cat-v2");
        TEST_CHECK(tsrc.channels()[0].name == "Hot-v2");
        TEST_CHECK(meta_sup.status("meta-worker")->requests_served == 2);

        meta_sup.stopAll();
        fs::remove_all(meta_dir, ec);
    }

    // 31. SearchIndexService prepared-statement cache, file mtime skip & scoped incremental hydration
    {
        const fs::path inc_dir =
            fs::temp_directory_path() / ("omnidesk_search_inc_" + std::to_string(::getpid()));
        fs::remove_all(inc_dir, ec);
        const fs::path vault_dir = inc_dir / "curation" / "repos" / "obsidian-vault" / "News" / "Daily";
        const fs::path music_dir = inc_dir / "curation" / "repos" / "music-daily" / "recommendations";
        fs::create_directories(vault_dir, ec);
        fs::create_directories(music_dir, ec);

        const fs::path note1 = vault_dir / "2026-11-10.md";
        const fs::path note2 = vault_dir / "2026-11-11.md";
        {
            std::ofstream f1(note1);
            f1 << "# Incremental Vault Alpha\nFirst archival note body with trigram searchable content.\n";
            std::ofstream f2(note2);
            f2 << "# Incremental Vault Beta\nSecond archival note body.\n";
            std::ofstream fm(music_dir / "2026-11-10.json");
            fm << R"({"date":"2026-11-10","tracks":[{"id":"trk-inc-1","title":"Nocturne Incremental","artist":"Ryuichi Sakamoto","review":"静谧钢琴音景"}]})";
        }

        omnidesk::DomainRegistry inc_dom;
        omnidesk::CapabilityRegistry inc_caps;
        omnidesk::SearchIndexService inc_idx;
        TEST_CHECK(inc_idx.open((inc_dir / "search_inc.db").string()));

        // Prepared statement compilation count stays bounded across repeated upserts & deletes
        const size_t compiles_after_open = inc_idx.preparedCompileCount();
        TEST_CHECK(compiles_after_open >= 8);
        for (int i = 0; i < 40; ++i) {
            omnidesk::SearchDocument d;
            d.doc_id = "bench:" + std::to_string(i);
            d.kind = "article";
            d.domain = "text";
            d.source_id = "plugin-bench";
            d.title = "Prepared Cache Item " + std::to_string(i);
            d.body = "Reusable sqlite3_stmt verification payload " + std::to_string(i);
            TEST_CHECK(inc_idx.upsert(d));
        }
        const size_t compiles_after_upserts = inc_idx.preparedCompileCount();
        TEST_CHECK(compiles_after_upserts == compiles_after_open);

        // Re-upserting identical batch performs 0 SQLite row writes
        omnidesk::SearchDocument same_doc;
        same_doc.doc_id = "bench:0";
        same_doc.kind = "article";
        same_doc.domain = "text";
        same_doc.source_id = "plugin-bench";
        same_doc.title = "Prepared Cache Item 0";
        same_doc.body = "Reusable sqlite3_stmt verification payload 0";
        TEST_CHECK(inc_idx.upsert(same_doc));
        TEST_CHECK(inc_idx.lastWriteCount() == 0);

        for (int i = 0; i < 10; ++i) {
            TEST_CHECK(inc_idx.remove("bench:" + std::to_string(i)));
        }
        TEST_CHECK(inc_idx.preparedCompileCount() == compiles_after_open);

        // Initial archive + command hydration writes all new documents
        const size_t h1 = inc_idx.hydrateEcosystem(&inc_dom, &inc_caps, inc_dir.string(), inc_dir.string());
        TEST_CHECK(h1 >= 3);
        TEST_CHECK(inc_idx.lastWriteCount() >= 3);

        // Immediate second hydration with unchanged files + unchanged commands performs 0 DB/FTS5 writes
        const size_t h2 = inc_idx.hydrateEcosystem(&inc_dom, &inc_caps, inc_dir.string(), inc_dir.string());
        TEST_CHECK(h2 >= 3);
        TEST_CHECK(inc_idx.lastWriteCount() == 0);

        // Modify only note2 (changing size & content) and run scoped archive incremental hydration:
        // note1 and 2026-11-10.json are skipped via source_file_state, so exactly 1 row is written!
        {
            std::ofstream f2(note2, std::ios::trunc);
            f2 << "# Incremental Vault Beta Updated\nModified body with QuantumSuperpositionToken keyword and larger size.\n";
        }
        omnidesk::SearchHydrateOptions arch_only;
        arch_only.scopes = omnidesk::kHydrateArchives;
        const size_t h3 = inc_idx.hydrateIncremental(
            &inc_dom, &inc_caps, inc_dir.string(), inc_dir.string(), arch_only);
        TEST_CHECK(h3 >= 1);
        TEST_CHECK(inc_idx.lastWriteCount() == 1);

        omnidesk::SearchQueryOptions q_opts;
        q_opts.query = "QuantumSuperpositionToken";
        auto hits = inc_idx.search(q_opts);
        TEST_CHECK(hits.size() == 1 && hits[0].title == "2026-11-11 · Daily");

        inc_idx.close();
        fs::remove_all(inc_dir, ec);
    }

    // 32. MarkdownRichTextView O(N) long-token wrapping & layout-time link_boxes hitTest without draw()
    {
        std::string huge_path(5200, 'a');
        const std::string huge_url = "https://example.com/archive/" + huge_path;
        const std::string md_text = "[" + huge_url + "](" + huge_url + ")";

        omnidesk::MarkdownStyle st;
        st.font_size = 16.0f;
        st.line_height = 1.5f;
        st.interactive_links = true;

        auto* root = new brls::Box(brls::Axis::COLUMN);
        root->setWidth(360.0f);
        root->setHeight(2000.0f);
        auto* rich = new omnidesk::MarkdownRichTextView(md_text, st);
        root->addView(rich);

        const auto t_start = std::chrono::steady_clock::now();
        YGNodeCalculateLayout(root->getYGNode(), 360.0f, 2000.0f, YGDirectionLTR);
        rich->onLayout();
        YGNodeCalculateLayout(root->getYGNode(), 360.0f, 2000.0f, YGDirectionLTR);
        const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t_start).count();

        // O(N) wrapping of 5,200+ char unspaced token must finish in a few milliseconds and wrap across many lines
        TEST_CHECK(elapsed_ms < 250);
        TEST_CHECK(rich->getHeight() > 200.0f);

        // Verify link_boxes are already populated during layout (before any NanoVG draw() call!)
        brls::Point probe{rich->getX() + 24.0f, rich->getY() + 12.0f};
        TEST_CHECK(rich->hitTest(probe) == rich);

        delete root;
    }

    // 33. RecyclingGrid viewport virtualization, cell reuse pool & notifyDataChanged incremental pagination
    {
        class BenchCell final : public omnidesk::host::RecyclingGridCell {
        public:
            int bound_value = -1;
            int prepare_calls = 0;
            BenchCell() : RecyclingGridCell(brls::Axis::COLUMN) {
                setFocusable(true);
            }
            void prepareForReuse() override { ++prepare_calls; }
        };

        class BenchDataSource final : public omnidesk::host::RecyclingGridDataSource {
        public:
            size_t count = 1000;
            size_t bind_calls = 0;

            size_t getItemCount() override { return count; }
            omnidesk::host::RecyclingGridCell* cellForRow(
                omnidesk::host::RecyclingGrid* recycler, size_t index) override {
                auto* cell = static_cast<BenchCell*>(recycler->dequeueReusableCell("bench"));
                cell->bound_value = static_cast<int>(index);
                ++bind_calls;
                return cell;
            }
        };

        // 33a. Single-column virtualized feed list (1,000 items)
        {
            auto* grid = new omnidesk::host::RecyclingGrid();
            grid->setWidth(960.0f);
            grid->setHeight(600.0f);
            grid->setSpanCount(1);
            grid->setEstimatedRowHeight(110.0f);
            grid->setEstimatedRowSpace(10.0f);
            grid->setPreFetchLine(2);
            grid->registerCell("bench", []() { return new BenchCell(); });

            auto* ds = new BenchDataSource();
            grid->setDataSource(ds, true);
            YGNodeCalculateLayout(grid->getYGNode(), 960.0f, 600.0f, YGDirectionLTR);
            grid->onLayout();

            TEST_CHECK(grid->totalItemCount() == 1000);
            TEST_CHECK(grid->mountedCellCount() >= 5 && grid->mountedCellCount() <= 14);
            TEST_CHECK(grid->allocatedCellCount() <= 16);

            // Scroll across multiple viewports through the 1,000-item list: cells must be recycled, not reallocated
            for (float scroll_y : {1800.0f, 7200.0f, 24000.0f, 64000.0f, 110000.0f}) {
                grid->setContentOffsetY(scroll_y, false);
                grid->onLayout();
                TEST_CHECK(grid->mountedCellCount() >= 5 && grid->mountedCellCount() <= 14);
                TEST_CHECK(grid->allocatedCellCount() <= 20);
            }
            TEST_CHECK(grid->reusedCellCount() >= 20);

            // Incremental pagination via notifyDataChanged preserves scroll offset without rebuilding visible cells
            const float prev_offset = grid->getContentOffsetY();
            const size_t binds_before = ds->bind_calls;
            ds->count = 1250;
            grid->notifyDataChanged();
            TEST_CHECK(grid->totalItemCount() == 1250);
            TEST_CHECK(std::abs(grid->getContentOffsetY() - prev_offset) < 1.0f);
            TEST_CHECK(ds->bind_calls == binds_before);

            delete grid;
        }

        // 33b. 4-column virtualized media/bookshelf grid (1,000 items -> ~20-32 mounted cells)
        {
            auto* grid = new omnidesk::host::RecyclingGrid();
            grid->setWidth(1200.0f);
            grid->setHeight(680.0f);
            grid->setSpanCount(4);
            grid->setEstimatedRowHeight(240.0f);
            grid->setEstimatedRowSpace(14.0f);
            grid->setColSpace(14.0f);
            grid->setPreFetchLine(1);
            grid->registerCell("bench", []() { return new BenchCell(); });

            auto* ds = new BenchDataSource();
            grid->setDataSource(ds, true);
            YGNodeCalculateLayout(grid->getYGNode(), 1200.0f, 680.0f, YGDirectionLTR);
            grid->onLayout();

            TEST_CHECK(grid->totalItemCount() == 1000);
            TEST_CHECK(grid->mountedCellCount() >= 12 && grid->mountedCellCount() <= 28);
            TEST_CHECK(grid->allocatedCellCount() <= 32);

            for (float scroll_y : {2000.0f, 9000.0f, 32000.0f}) {
                grid->setContentOffsetY(scroll_y, false);
                grid->onLayout();
                TEST_CHECK(grid->mountedCellCount() >= 12 && grid->mountedCellCount() <= 28);
                TEST_CHECK(grid->allocatedCellCount() <= 36);
            }
            TEST_CHECK(grid->reusedCellCount() >= 24);

            delete grid;
        }
    }

    std::cout << "test_sdk_bus: ALL PASSED (33/33 suites)\n";
    return 0;
}
