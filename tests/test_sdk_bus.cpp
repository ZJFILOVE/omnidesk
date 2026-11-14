#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>
#include "omnidesk/sdk.hpp"

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

    // 9. ImageCache (WebP + PNG decode + persistent disk LRU eviction + file:// async fetch)
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

        auto dw = omnidesk::ImageCache::decodeBytes(kWebp1x1, sizeof(kWebp1x1));
        TEST_CHECK(dw.ok && dw.width == 1 && dw.height == 1 && dw.rgba.size() == 4);
        TEST_CHECK(dw.rgba[0] == 255 && dw.rgba[1] == 0 && dw.rgba[2] == 128 && dw.rgba[3] == 255);

        auto dp = omnidesk::ImageCache::decodeBytes(kPng1x1, sizeof(kPng1x1));
        TEST_CHECK(dp.ok && dp.width == 1 && dp.height == 1 && dp.rgba.size() == 4);
        TEST_CHECK(dp.rgba[0] == 255 && dp.rgba[1] == 0 && dp.rgba[2] == 128 && dp.rgba[3] == 255);

        omnidesk::ImageCacheConfig ic_cfg;
        ic_cfg.cache_dir = (tmp_root / "imgcache").string();
        ic_cfg.max_disk_bytes = 128;
        omnidesk::ImageCache cache(ic_cfg);

        std::vector<uint8_t> blob1(60, 0x11), blob2(60, 0x22), blob3(60, 0x33);
        TEST_CHECK(cache.putCachedRawBytes("k1", blob1.data(), blob1.size()));
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
        TEST_CHECK(cache.putCachedRawBytes("k2", blob2.data(), blob2.size()));
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
        TEST_CHECK(cache.putCachedRawBytes("k3", blob3.data(), blob3.size()));
        TEST_CHECK(cache.diskUsageBytes() <= 128);
        TEST_CHECK(cache.getCachedRawBytes("k3").has_value());

        const fs::path webp_file = tmp_root / "test.webp";
        {
            std::ofstream ofs(webp_file, std::ios::binary);
            ofs.write(reinterpret_cast<const char*>(kWebp1x1), sizeof(kWebp1x1));
        }
        std::atomic<bool> decoded_done{false};
        omnidesk::DecodedImage fetched;
        cache.fetchDecoded("file://" + webp_file.string(), [&](omnidesk::DecodedImage img) {
            fetched = std::move(img);
            decoded_done.store(true);
        });
        for (int w = 0; w < 100 && !decoded_done.load(); ++w) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        TEST_CHECK(decoded_done.load() && fetched.ok && fetched.width == 1 && fetched.height == 1);
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

    fs::remove_all(tmp_root, ec);
    std::cout << "test_sdk_bus: ALL PASSED (10/10 suites)\n";
    return 0;
}
