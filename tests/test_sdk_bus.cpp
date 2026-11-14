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
            std::vector<omnidesk::TextFeedItem> fetchFeed(const std::string&) override {
                throw std::runtime_error("text feed boom");
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
        TEST_CHECK(t_srcs.size() == 1 && t_srcs[0]->fetchFeed("").empty());
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

    std::cout << "test_sdk_bus: ALL PASSED (16/16 suites)\n";
    return 0;
}
