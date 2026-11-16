#include "ai_curator/archive_rehydrator.hpp"
#include "ai_curator/daily_scheduler.hpp"
#include "ai_curator/git_mirror.hpp"
#include "ai_curator/plugin.hpp"
#include "ai_curator/utils.hpp"

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <omnidesk/plugin.hpp>
#include <omnidesk/sdk.hpp>
#include <unistd.h>
#include <unordered_map>

namespace fs = std::filesystem;

#define CHECK(cond)                                                                                \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            std::cerr << "CHECK FAILED at " << __FILE__ << ":" << __LINE__ << ": " #cond "\n";    \
            return 1;                                                                              \
        }                                                                                          \
    } while (0)

static const char* kRealObsidianVault =
    "/home/zhu/.local/share/omnidesk/curation/repos/obsidian-vault";
static const char* kRealMusicDaily =
    "/home/zhu/.local/share/omnidesk/curation/repos/music-daily";

int main() {
    const fs::path temp_root =
        fs::temp_directory_path() / ("omnidesk-ai-curator-test-" + std::to_string(::getpid()));
    fs::remove_all(temp_root);
    fs::create_directories(temp_root / "data");
    fs::create_directories(temp_root / "config");

    omnidesk::CapabilityRegistry caps;
    omnidesk::EntityBus bus;
    omnidesk::DomainRegistry domains;
    omnidesk::ConfigStore config_store(temp_root / "config");
    omnidesk::SecretVault vault(temp_root / "config");

    // Mock vocab.sm2.v1 to test idempotent sync without overwriting existing intervals
    std::unordered_map<std::string, int> mock_vocab_intervals = {{"procrastinate", 14}};
    int vocab_add_calls = 0;
    caps.registerCapability("vocab.sm2.v1", "mock-vocab",
        [&](const nlohmann::json& req) -> omnidesk::CapabilityResult {
            std::string act = req.value("action", "");
            if (act == "list") {
                nlohmann::json arr = nlohmann::json::array();
                for (const auto& [w, iv] : mock_vocab_intervals) {
                    arr.push_back({{"word", w}, {"interval_days", iv}});
                }
                return {true, {{"cards", std::move(arr)}}, "", "mock-vocab", false};
            }
            if (act == "add") {
                ++vocab_add_calls;
                std::string w = ai_curator::toLowerAscii(ai_curator::trim(req.value("word", "")));
                // Assert existing SM-2 card is NEVER overwritten
                if (w == "procrastinate") {
                    return {false, {}, "Should not overwrite existing word!", "mock-vocab", false};
                }
                mock_vocab_intervals[w] = 1;
                return {true, {{"word", w}}, "", "mock-vocab", false};
            }
            return {false, {}, "unknown", "mock-vocab", false};
        });

    config_store.savePluginConfig("plugin-ai-curator", {
        {"obsidian_vault_dir", kRealObsidianVault},
        {"music_daily_dir", kRealMusicDaily},
        {"auto_rehydrate_on_load", false},
        {"auto_pull_remote", false},
    });

    omnidesk::PluginContext ctx("plugin-ai-curator",
                                temp_root.string(),
                                (temp_root / "data").string(),
                                caps,
                                bus,
                                domains,
                                config_store,
                                vault);

    ai_curator::AiCuratorPlugin plugin;
    CHECK(plugin.onLoad(ctx));

    int recommend_updated_events = 0;
    bus.subscribe("entity.recommend.updated", "test", [&](const std::string&, const nlohmann::json&) {
        ++recommend_updated_events;
    });
    std::string last_ephemeral_note;
    bus.subscribe("entity.recommend.ephemeral_note", "test", [&](const std::string&, const nlohmann::json& p) {
        last_ephemeral_note = p.value("note", "");
    });

    // 1. Real Archive Rehydration
    CHECK(fs::exists(kRealObsidianVault));
    CHECK(fs::exists(kRealMusicDaily));

    auto boot = caps.call("recommend.feed.v1", {
        {"action", "bootstrap_and_rehydrate"},
        {"obsidian_vault_dir", kRealObsidianVault},
        {"music_daily_dir", kRealMusicDaily},
        {"pull_remote", false},
        {"clone_if_missing", false},
    });
    CHECK(boot.ok);
    std::cout << "Rehydrated counts: " << boot.data.dump() << "\n";
    CHECK(boot.data.value("dates_indexed", 0) > 60);
    CHECK(boot.data.value("music_tracks_hydrated", 0) > 300);
    CHECK(boot.data.value("youtube_videos_hydrated", 0) > 300);
    CHECK(boot.data.value("news_days_hydrated", 0) >= 60);
    CHECK(boot.data.value("reddit_days_hydrated", 0) >= 60);
    CHECK(boot.data.value("journal_days_hydrated", 0) >= 13);
    CHECK(boot.data.value("english_notes_hydrated", 0) >= 150);
    CHECK(boot.data.value("vocab_cards_hydrated", 0) >= 350);
    CHECK(recommend_updated_events >= 1);
    CHECK(mock_vocab_intervals["procrastinate"] == 14);
    CHECK(vocab_add_calls > 300);

    // Incremental cache hit check on second run (< 50 ms)
    const int prev_vocab_calls = vocab_add_calls;
    const auto t0 = std::chrono::steady_clock::now();
    auto boot2 = caps.call("recommend.feed.v1", {
        {"action", "bootstrap_and_rehydrate"},
        {"obsidian_vault_dir", kRealObsidianVault},
        {"music_daily_dir", kRealMusicDaily},
        {"pull_remote", false},
    });
    const auto boot2_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                              std::chrono::steady_clock::now() - t0)
                              .count();
    CHECK(boot2.ok);
    CHECK(boot2.data.value("cache_hit", false) == true);
    CHECK(boot2_ms < 250);
    CHECK(vocab_add_calls == prev_vocab_calls); // Idempotent: 0 duplicate add calls!

    // list_archive_dates, search_archive, get_date_bundle
    auto dates_res = caps.call("recommend.feed.v1", {{"action", "list_archive_dates"}});
    CHECK(dates_res.ok);
    CHECK(dates_res.data["dates"].is_array());
    CHECK(dates_res.data["dates"].size() > 60);
    CHECK(dates_res.data["dates"][0]["date"].get<std::string>() >=
          dates_res.data["dates"][1]["date"].get<std::string>());

    auto search_res = caps.call("recommend.feed.v1", {
        {"action", "search_archive"},
        {"query", "王菲"},
        {"domain", "music"},
    });
    CHECK(search_res.ok);
    CHECK(search_res.data.value("total", 0) >= 1);

    auto bundle_res = caps.call("recommend.feed.v1", {
        {"action", "get_date_bundle"},
        {"business_date", "2026-09-04"},
    });
    CHECK(bundle_res.ok);
    CHECK(bundle_res.data["media_items"].size() >= 5);

    // 2. Verify IMediaSource, ITextSource, IBookSource
    auto media_sources = domains.mediaSources();
    CHECK(media_sources.size() == 1);
    auto msrc = media_sources[0];
    CHECK(msrc->sourceId() == "plugin-ai-curator");
    CHECK(msrc->categories().size() == 4);
    auto latest_5 = msrc->fetchItems("每日 5 首 · 最新");
    CHECK(latest_5.size() == 5);
    auto sept4 = msrc->fetchItems("每日 5 首 · 全量曲库", "date:2026-09-04");
    CHECK(sept4.size() == 5);
    CHECK(sept4[0].extra.value("apple_music_song_id", "") == "1463501518");
    CHECK(!sept4[0].extra.value("why_recommended", "").empty());
    CHECK(!sept4[0].extra.value("listening_or_reading_cue", "").empty());
    auto yt_items = msrc->fetchItems("YouTube 每日精选");
    CHECK(yt_items.size() > 300);
    CHECK(ai_curator::startsWith(yt_items[0].cover_url, "https://i.ytimg.com/vi/"));

    auto text_sources = domains.textSources();
    CHECK(text_sources.size() == 1);
    auto tsrc = text_sources[0];
    CHECK(tsrc->category() == "multi");
    auto channels = tsrc->channels();
    CHECK(channels.size() == 5);
    auto j_items = tsrc->fetchFeedInChannel("journal");
    CHECK(j_items.size() >= 39);
    auto j_art = tsrc->fetchArticle(j_items[0].id);
    CHECK(!j_art.doi.empty());
    CHECK(!j_art.paragraphs.empty());
    auto n_items = tsrc->fetchFeedInChannel("news");
    CHECK(n_items.size() > 200);
    auto r_items = tsrc->fetchFeedInChannel("reddit");
    CHECK(r_items.size() > 500);
    auto r_art = tsrc->fetchArticle(r_items[0].id);
    bool found_why_read_in_article = false;
    for (const auto& para : r_art.paragraphs) {
        if (para.original == "Why read") {
            found_why_read_in_article = true;
        } else if (found_why_read_in_article && !para.original.empty()) {
            CHECK(r_items[0].excerpt != para.original);
            break;
        }
    }
    CHECK(found_why_read_in_article);
    auto e_items = tsrc->fetchFeedInChannel("english");
    CHECK(e_items.size() >= 150);

    auto book_sources = domains.bookSources();
    CHECK(book_sources.size() == 1);
    auto bsrc = book_sources[0];
    auto books = bsrc->listBooks();
    CHECK(books.size() > 40);
    CHECK(books[0].id == "english-archive-all");
    auto spread = bsrc->fetchSpread("english-archive-all", 1);
    CHECK(!spread.blocks.empty());
    CHECK(bsrc->listVocab().size() >= 350);

    // Read-only provenance batches must match stable identities without creating recommendations.
    const int events_before_lookup = recommend_updated_events;
    const int vocab_before_lookup = vocab_add_calls;
    auto provenance = caps.call("recommend.feed.v1", {
        {"action", "lookup_provenance"},
        {"items", {
            {{"id", "live-song"}, {"kind", "music"}, {"title", "Different display title"},
             {"play_url", "https://music.apple.com/cn/song/1463501518"}},
            {{"id", "live-doi"}, {"doi", "https://doi.org/10.1126/science.aan6349"}},
            {{"id", "unknown"}, {"title", "Never recommended test item 00000"}},
            {{"id", "ambiguous-song"}, {"kind", "music"}, {"title", "暧昧"}},
            {{"id", "malformed"}, {"title", nullptr}, {"extra", {{"song_id", 123}}}},
        }},
    });
    CHECK(provenance.ok);
    CHECK(provenance.data.value("read_only", false));
    CHECK(provenance.data["matches"].size() == 2);
    CHECK(provenance.data["matches"][0].value("id", "") == "live-song");
    CHECK(provenance.data["matches"][0].value("previously_recommended_on", "") == "2026-09-04");
    CHECK(provenance.data["matches"][1].value("id", "") == "live-doi");
    CHECK(recommend_updated_events == events_before_lookup);
    CHECK(vocab_add_calls == vocab_before_lookup);
    CHECK(!caps.call("recommend.feed.v1", {{"action", "lookup_provenance"}}).ok);

    // 3. Mode A (publish_daily_bundle) Guardrails
    auto bad_mode = caps.call("recommend.feed.v1", {
        {"action", "publish_daily_bundle"},
        {"mode", "interactive_ephemeral"},
        {"qa_verified", true},
    });
    CHECK(!bad_mode.ok);
    CHECK(bad_mode.data.value("error_code", "") == "invalid_curation_mode");

    auto bad_qa = caps.call("recommend.feed.v1", {
        {"action", "publish_daily_bundle"},
        {"mode", "daily_immutable"},
        {"qa_verified", false},
    });
    CHECK(!bad_qa.ok);
    CHECK(bad_qa.data.value("error_code", "") == "qa_not_verified");

    const std::string today = ai_curator::currentShanghaiDate();
    auto needs_evidence = caps.call("recommend.feed.v1", {
        {"action", "publish_daily_bundle"},
        {"mode", "daily_immutable"},
        {"business_date", today},
        {"qa_verified", true},
    });
    CHECK(!needs_evidence.ok);
    CHECK(needs_evidence.data.value("error_code", "") == "needs_verified_bundle");

    auto bad_backfill = caps.call("recommend.feed.v1", {
        {"action", "publish_daily_bundle"},
        {"mode", "daily_immutable"},
        {"business_date", "2026-10-01"},
        {"qa_verified", true},
    });
    CHECK(!bad_backfill.ok);
    CHECK(bad_backfill.data.value("error_code", "") == "historical_backfill_forbidden");

    auto dup_daily = caps.call("recommend.feed.v1", {
        {"action", "publish_daily_bundle"},
        {"mode", "daily_immutable"},
        {"business_date", today},
        {"qa_verified", true},
        {"publication_evidence", {
            {"status", "PUBLISHED"},
            {"publication_verified", true},
            {"operation_id", "daily:" + today},
            {"project", "daily"},
            {"business_date", today},
            {"receipt", {{"path", ".recommendation/runs/" + today + "-daily.json"}}},
        }},
        {"media_items", {{
            {"id", "am-cn-1463501518"},
            {"title", "暧昧"},
            {"creator", "王菲"},
            {"kind", "music"},
            {"play_url", "https://music.apple.com/cn/song/1463501518"},
            {"extra", {{"apple_music_song_id", "1463501518"}, {"canonical_id", "王菲::暧昧"}}},
        }}},
    });
    CHECK(!dup_daily.ok);
    CHECK(dup_daily.data.value("error_code", "") == "duplicate_in_permanent_history");

    // Unreadable != Empty ledger check
    const fs::path broken_vault = temp_root / "broken_vault";
    fs::create_directories(broken_vault / ".recommendation" / "history");
    ai_curator::writeFileAtomic((broken_vault / ".recommendation" / "policy.json").string(), "{corrupt json");
    auto bad_ledger = caps.call("recommend.feed.v1", {
        {"action", "publish_daily_bundle"},
        {"mode", "daily_immutable"},
        {"business_date", today},
        {"qa_verified", true},
        {"publication_evidence", {
            {"status", "PUBLISHED"},
            {"publication_verified", true},
            {"operation_id", "daily:" + today},
            {"project", "daily"},
            {"business_date", today},
            {"receipt", {{"path", ".recommendation/runs/" + today + "-daily.json"}}},
        }},
        {"obsidian_vault_dir", broken_vault.string()},
        {"music_daily_dir", (temp_root / "empty_music").string()},
    });
    CHECK(!bad_ledger.ok);
    CHECK(bad_ledger.data.value("error_code", "") == "unreadable_ledger");

    // Handwritten ## Notes preservation + ephemeral_chat_note (今日饮食) isolation
    const fs::path note_md = temp_root / "daily_note_test.md";
    ai_curator::writeFileAtomic(note_md.string(),
        "---\ndate: " + today + "\n---\n\n<!-- AUTO:START -->\nold content\n<!-- AUTO:END -->\n\n## Notes\n\nMy handwritten reflection.\n");
    auto good_daily = caps.call("recommend.feed.v1", {
        {"action", "publish_daily_bundle"},
        {"mode", "daily_immutable"},
        {"operation_id", "english:" + today},
        {"business_date", today},
        {"qa_verified", true},
        {"publication_evidence", {
            {"status", "PUBLISHED"},
            {"publication_verified", true},
            {"operation_id", "english:" + today},
            {"project", "english"},
            {"business_date", today},
            {"receipt", {{"path", ".recommendation/transactions/english/" + today + ".json"}}},
        }},
        {"obsidian_vault_dir", (temp_root / "clean_empty_obs").string()},
        {"music_daily_dir", (temp_root / "clean_empty_mus").string()},
        {"ephemeral_chat_note", "今日饮食：清蒸鲈鱼配糙米饭"},
        {"markdown_updates", {{{"path", note_md.string()}, {"auto_block", "new auto content"}}}},
        {"vocab_cards", {{{"word", "ubiquitous"}, {"meaning", "无处不在的"}}}},
    });
    CHECK(good_daily.ok);
    CHECK(last_ephemeral_note == "今日饮食：清蒸鲈鱼配糙米饭");
    auto updated_md = ai_curator::readFileText(note_md.string());
    CHECK(updated_md.has_value());
    CHECK(updated_md->find("new auto content") != std::string::npos);
    CHECK(updated_md->find("My handwritten reflection.") != std::string::npos);
    CHECK(updated_md->find("今日饮食") == std::string::npos);
    // Verify persisted daily JSON does NOT contain 今日饮食
    const fs::path persisted_daily =
        temp_root / "data" / "curation" / "daily" / today / ("english_" + today + ".json");
    auto persisted_txt = ai_curator::readFileText(persisted_daily.string());
    CHECK(persisted_txt.has_value());
    CHECK(persisted_txt->find("今日饮食") == std::string::npos);
    CHECK(mock_vocab_intervals["ubiquitous"] == 1);

    // 4. Mode B (push_interactive_set) Guardrails & Zero Repo Mutation
    // Initialize clean temp git repo to verify zero mutation + dirty worktree detection
    const fs::path git_obs = temp_root / "git_obs";
    const fs::path git_mus = temp_root / "git_mus";
    fs::create_directories(git_obs / "News" / "Daily");
    fs::create_directories(git_mus / "state");
    ai_curator::writeFileAtomic((git_obs / "README.md").string(), "hello\n");
    ai_curator::writeFileAtomic((git_mus / "README.md").string(), "hello\n");
    CHECK(ai_curator::runProcess({"git", "init"}, git_obs.string()).exit_code == 0);
    CHECK(ai_curator::runProcess({"git", "config", "user.email", "t@e.com"}, git_obs.string()).exit_code == 0);
    CHECK(ai_curator::runProcess({"git", "config", "user.name", "Test"}, git_obs.string()).exit_code == 0);
    CHECK(ai_curator::runProcess({"git", "add", "."}, git_obs.string()).exit_code == 0);
    CHECK(ai_curator::runProcess({"git", "commit", "-m", "init"}, git_obs.string()).exit_code == 0);

    auto bad_ix_pollute = caps.call("recommend.feed.v1", {
        {"action", "push_interactive_set"},
        {"mode", "interactive_ephemeral"},
        {"record_permanent_history", true},
        {"media_items", nlohmann::json::array()},
    });
    CHECK(!bad_ix_pollute.ok);
    CHECK(bad_ix_pollute.data.value("error_code", "") == "ledger_pollution_forbidden");

    auto good_ix = caps.call("recommend.feed.v1", {
        {"action", "push_interactive_set"},
        {"mode", "interactive_ephemeral"},
        {"session_id", "ix-revisit-test"},
        {"user_prompt", "再来听一次王菲的暧昧"},
        {"repeat_policy", "allow_any"},
        {"record_permanent_history", false},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
        {"media_items", {{
            {"id", "ix-am-1463501518"},
            {"title", "暧昧"},
            {"subtitle", "王菲"},
            {"kind", "music"},
            {"play_url", "https://music.apple.com/cn/song/1463501518"},
            {"extra", {{"apple_music_song_id", "1463501518"}}},
        }}},
        {"text_items", {{
            {"id", "ix-txt-1"},
            {"title", "An algal photoenzyme converts fatty acids to hydrocarbons"},
            {"excerpt", "Revisit classic FAP paper"},
            {"extra", {{"why_recommended", "Revisit classic FAP paper"},
                       {"listening_or_reading_cue", "Read the catalytic mechanism."}}},
            {"doi", "10.1126/science.aan6349"},
        }}},
    });
    CHECK(good_ix.ok);
    CHECK(good_ix.data["media_items"][0]["extra"].value("previously_recommended_on", "") == "2026-09-04");
    CHECK(good_ix.data["media_items"][0]["extra"].value("revisit_badge", "") == "经典重温 · 2026-09-04");
    CHECK(good_ix.data.value("revisit_matches", 0) == 2);

    auto ix_media = msrc->fetchItems("AI 灵感推荐");
    CHECK(ix_media.size() == 1);
    CHECK(ix_media[0].extra.value("previously_recommended_on", "") == "2026-09-04");
    auto ix_texts = tsrc->fetchFeedInChannel("interactive");
    CHECK(ix_texts.size() == 1);
    CHECK(ix_texts[0].excerpt.empty());
    const auto ix_article = tsrc->fetchArticle(ix_texts[0].id);
    int reason_count = 0;
    bool cue_preserved = false;
    for (const auto& para : ix_article.paragraphs) {
        if (para.original == "Revisit classic FAP paper") ++reason_count;
        if (para.original == "Read the catalytic mechanism.") cue_preserved = true;
    }
    CHECK(reason_count == 1);
    CHECK(cue_preserved);

    auto ix_session_dedup = caps.call("recommend.feed.v1", {
        {"action", "push_interactive_set"},
        {"mode", "interactive_ephemeral"},
        {"session_id", "ix-revisit-test"},
        {"repeat_policy", "session_dedup_only"},
        {"record_permanent_history", false},
        {"media_items", {{
            {"id", "ix-am-same-song-different-id"},
            {"title", "暧昧"},
            {"subtitle", "王菲"},
            {"kind", "music"},
            {"play_url", "https://music.apple.com/cn/song/1463501518"},
            {"extra", {{"apple_music_song_id", "1463501518"}}},
        }}},
    });
    CHECK(ix_session_dedup.ok);
    CHECK(ix_session_dedup.data.value("skipped_session_duplicates", 0) == 1);
    CHECK(ix_session_dedup.data.value("added_media_count", 1) == 0);
    CHECK(msrc->fetchItems("AI 灵感推荐").size() == 1);

    // Verify zero mutation of git repo
    auto gstat = ai_curator::runProcess({"git", "status", "--porcelain"}, git_obs.string());
    CHECK(ai_curator::trim(gstat.stdout_text).empty());

    // Verify dirty worktree detection when pulling
    ai_curator::writeFileAtomic((git_obs / "README.md").string(), "user local edit\n");
    auto dirty_sync = ai_curator::GitMirror::syncRepo(git_obs.string(), "", false, true);
    CHECK(dirty_sync.dirty_worktree_preserved == true);
    CHECK(ai_curator::readFileText((git_obs / "README.md").string()).value() == "user local edit\n");

    // Pin, dismiss, clear_interactive
    CHECK(caps.call("recommend.feed.v1", {{"action", "pin_item"}, {"item_id", "ix-am-1463501518"}}).ok);
    CHECK(caps.call("recommend.feed.v1", {{"action", "dismiss_item"}, {"item_id", "ix-am-1463501518"}}).ok);
    CHECK(msrc->fetchItems("AI 灵感推荐").empty());
    CHECK(caps.call("recommend.feed.v1", {{"action", "clear_interactive"}}).ok);

    // Domain filtering fixes: NAS should be empty, music returns music only, listBooks date: filter omits master book
    CHECK(msrc->fetchItems("nas").empty());
    auto music_only = msrc->fetchItems("music");
    CHECK(!music_only.empty());
    for (const auto& mi : music_only) {
        CHECK(mi.kind == "music");
    }
    auto books_on_date = bsrc->listBooks("", "date:2026-09-04");
    for (const auto& bi : books_on_date) {
        CHECK(bi.id != "english-archive-all");
    }

    // Daily Workflow Scheduler tests
    auto sched_status = caps.call("recommend.feed.v1", {
        {"action", "daily_status"},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    });
    CHECK(sched_status.ok);
    CHECK(sched_status.data.value("business_date", "") == today);
    CHECK(sched_status.data["workflows"].size() == 4);

    // Reject historical backfill
    auto hist_backfill = caps.call("recommend.feed.v1", {
        {"action", "run_daily_workflows"},
        {"business_date", "2026-09-04"},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    });
    CHECK(!hist_backfill.ok);
    CHECK(hist_backfill.data.value("error_code", "") == "historical_backfill_forbidden");

    // Configure custom mock agent that writes today's journal target and verifies live archive date reflection
    const fs::path mock_script = temp_root / "mock_journal_agent.sh";
    const fs::path today_journal_md = git_obs / "00-inbox" / "Journal" / (today + ".md");
    ai_curator::writeFileAtomic(
        mock_script.string(),
        "#!/bin/sh\n"
        "mkdir -p \"$(dirname '" + today_journal_md.string() + "')\"\n"
        "cat > '" + today_journal_md.string() + "' <<'EOF'\n"
        "---\n"
        "date: " + today + "\n"
        "---\n"
        "<!-- AUTO:START -->\n"
        "## 1. Mock Today Paper\n"
        "- DOI: 10.1038/mock-today\n"
        "<!-- AUTO:END -->\n"
        "EOF\n"
        "exit 0\n");
    std::error_code perm_ec;
    fs::permissions(mock_script,
                    fs::perms::owner_read | fs::perms::owner_write | fs::perms::owner_exec,
                    fs::perm_options::replace, perm_ec);

    auto agy_cfg_res = caps.call("recommend.feed.v1", {
        {"action", "configure_daily_scheduler"},
        {"auto_run_enabled", false},
        {"agent_runner", "agy"},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    });
    CHECK(agy_cfg_res.ok);
    CHECK(agy_cfg_res.data.value("agent_runner", "") == "agy-cli");

    auto cfg_res = caps.call("recommend.feed.v1", {
        {"action", "configure_daily_scheduler"},
        {"auto_run_enabled", false},
        {"agent_runner", "custom"},
        {"custom_agent_command", mock_script.string()},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    });
    CHECK(cfg_res.ok);
    CHECK(cfg_res.data.value("auto_run_enabled", true) == false);
    CHECK(cfg_res.data.value("agent_runner", "") == "custom");

    auto run_res = caps.call("recommend.feed.v1", {
        {"action", "run_daily_workflows"},
        {"project", "journal"},
        {"wait", true},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    });
    CHECK(run_res.ok);
    CHECK(run_res.data.value("started", false) == true);
    CHECK(fs::exists(today_journal_md));

    auto post_status = caps.call("ai.curation.v1", {
        {"action", "daily_status"},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    });
    CHECK(post_status.ok);
    bool journal_published = false;
    for (const auto& wf : post_status.data["workflows"]) {
        if (wf.value("id", "") == "journal") {
            journal_published = (wf.value("status", "") == "published");
        }
    }
    CHECK(journal_published);

    // Check today appears in list_archive_dates
    auto dates_after = caps.call("recommend.feed.v1", {{"action", "list_archive_dates"}});
    CHECK(dates_after.ok);
    bool found_today = false;
    for (const auto& d : dates_after.data["dates"]) {
        if (d.value("date", "") == today) found_today = true;
    }
    CHECK(found_today);

    // Bidirectional GitHub daily sync tests (bare origin remotes: pull-before-run + post-run push + sync_github)
    {
        const fs::path bare_obs = temp_root / "bare_obsidian.git";
        const fs::path bare_mus = temp_root / "bare_music.git";
        const fs::path seed_obs = temp_root / "seed_obsidian";
        const fs::path seed_mus = temp_root / "seed_music";
        const fs::path clone_obs = temp_root / "clone_obsidian";
        const fs::path clone_mus = temp_root / "clone_music";
        fs::create_directories(seed_obs);
        fs::create_directories(seed_mus);

        CHECK(ai_curator::runProcess({"git", "init", "--bare", "-b", "main", bare_obs.string()}).exit_code == 0);
        CHECK(ai_curator::runProcess({"git", "init", "--bare", "-b", "main", bare_mus.string()}).exit_code == 0);

        auto init_seed = [](const fs::path& dir, const fs::path& bare) {
            ai_curator::runProcess({"git", "init", "-b", "main"}, dir.string());
            ai_curator::runProcess({"git", "config", "user.email", "test@omnidesk.local"}, dir.string());
            ai_curator::runProcess({"git", "config", "user.name", "OmniDesk Test"}, dir.string());
            ai_curator::writeFileAtomic((dir / "README.md").string(), "# Seed\n");
            ai_curator::runProcess({"git", "add", "README.md"}, dir.string());
            ai_curator::runProcess({"git", "commit", "-m", "Initial commit"}, dir.string());
            ai_curator::runProcess({"git", "remote", "add", "origin", bare.string()}, dir.string());
            return ai_curator::runProcess({"git", "push", "-u", "origin", "main"}, dir.string()).exit_code == 0;
        };
        CHECK(init_seed(seed_obs, bare_obs));
        CHECK(init_seed(seed_mus, bare_mus));

        // Clone via GitMirror::syncRepo
        auto c_obs = ai_curator::GitMirror::syncRepo(clone_obs.string(), bare_obs.string(), true, true);
        auto c_mus = ai_curator::GitMirror::syncRepo(clone_mus.string(), bare_mus.string(), true, true);
        CHECK(c_obs.cloned && c_obs.has_remote);
        CHECK(c_mus.cloned && c_mus.has_remote);
        ai_curator::runProcess({"git", "config", "user.email", "agent@omnidesk.local"}, clone_obs.string());
        ai_curator::runProcess({"git", "config", "user.name", "OmniDesk Agent"}, clone_obs.string());
        ai_curator::runProcess({"git", "config", "user.email", "agent@omnidesk.local"}, clone_mus.string());
        ai_curator::runProcess({"git", "config", "user.name", "OmniDesk Agent"}, clone_mus.string());

        // 1) Simulate remote GitHub already publishing today's `english` notes in seed_obs -> push to bare_obs
        ai_curator::RepoPaths seed_paths;
        seed_paths.obsidian_vault_dir = seed_obs.string();
        seed_paths.music_daily_dir = seed_mus.string();
        for (const auto& target :
             ai_curator::DailyWorkflowScheduler::canonicalRepoTargets("english", today, seed_paths)) {
            ai_curator::writeFileAtomic(target, "---\ndate: " + today + "\n---\n# Remote English\n");
        }
        CHECK(ai_curator::runProcess({"git", "add", "-A"}, seed_obs.string()).exit_code == 0);
        CHECK(ai_curator::runProcess({"git", "commit", "-m", "Remote publish english"}, seed_obs.string()).exit_code == 0);
        CHECK(ai_curator::runProcess({"git", "push", "origin", "main"}, seed_obs.string()).exit_code == 0);

        // Calling daily_status with sync_remote=true pulls remote commit and marks `english` published
        auto synced_st = caps.call("recommend.feed.v1", {
            {"action", "daily_status"},
            {"sync_remote", true},
            {"force_sync", true},
            {"obsidian_vault_dir", clone_obs.string()},
            {"music_daily_dir", clone_mus.string()},
        });
        CHECK(synced_st.ok);
        CHECK(synced_st.data["git_sync"].value("pulled_any", false) == true);
        bool english_pulled_published = false;
        for (const auto& wf : synced_st.data["workflows"]) {
            if (wf.value("id", "") == "english") {
                english_pulled_published = (wf.value("status", "") == "published");
            }
        }
        CHECK(english_pulled_published);

        // 2) Mock agent commits today's `news-youtube-reddit` locally WITHOUT pushing; scheduler must auto-push to origin/main
        ai_curator::RepoPaths clone_paths;
        clone_paths.obsidian_vault_dir = clone_obs.string();
        clone_paths.music_daily_dir = clone_mus.string();
        const auto disc_targets =
            ai_curator::DailyWorkflowScheduler::canonicalRepoTargets("news-youtube-reddit", today, clone_paths);
        CHECK(disc_targets.size() == 3);

        const fs::path commit_only_agent = temp_root / "mock_commit_only_agent.sh";
        std::string script_body = "#!/bin/sh\nset -e\n";
        for (const auto& dt : disc_targets) {
            script_body += "mkdir -p \"$(dirname '" + dt + "')\"\n";
            script_body += "printf -- '---\\ndate: " + today + "\\n---\\n## 1. Synced Item\\n' > '" + dt + "'\n";
        }
        script_body += "git -C '" + clone_obs.string() + "' add -A\n";
        script_body += "git -C '" + clone_obs.string() + "' commit -m 'Local agent commit discovery'\n";
        script_body += "exit 0\n";
        ai_curator::writeFileAtomic(commit_only_agent.string(), script_body);
        fs::permissions(commit_only_agent,
                        fs::perms::owner_read | fs::perms::owner_write | fs::perms::owner_exec,
                        fs::perm_options::replace, perm_ec);

        CHECK(caps.call("recommend.feed.v1", {
            {"action", "configure_daily_scheduler"},
            {"git_sync_enabled", true},
            {"agent_runner", "custom"},
            {"custom_agent_command", commit_only_agent.string()},
            {"obsidian_vault_dir", clone_obs.string()},
            {"music_daily_dir", clone_mus.string()},
        }).ok);

        auto disc_run = caps.call("recommend.feed.v1", {
            {"action", "run_daily_workflows"},
            {"project", "news-youtube-reddit"},
            {"wait", true},
            {"obsidian_vault_dir", clone_obs.string()},
            {"music_daily_dir", clone_mus.string()},
        });
        CHECK(disc_run.ok);

        const std::string local_head = ai_curator::trim(
            ai_curator::runProcess({"git", "rev-parse", "HEAD"}, clone_obs.string()).stdout_text);
        const std::string bare_head = ai_curator::trim(
            ai_curator::runProcess({"git", "rev-parse", "refs/heads/main"}, bare_obs.string()).stdout_text);
        CHECK(!local_head.empty());
        CHECK(local_head == bare_head);

        bool disc_pushed = false;
        for (const auto& wf : disc_run.data["workflows"]) {
            if (wf.value("id", "") == "news-youtube-reddit") {
                disc_pushed = (wf.value("status", "") == "published" &&
                               wf.value("git_sync_status", "") == "pushed");
            }
        }
        CHECK(disc_pushed);

        // 3) Explicit `sync_github` RPC pushes pending local commits in music_daily
        ai_curator::writeFileAtomic((clone_mus / "SYNC_CHECK.md").string(), "ok\n");
        CHECK(ai_curator::runProcess({"git", "add", "SYNC_CHECK.md"}, clone_mus.string()).exit_code == 0);
        CHECK(ai_curator::runProcess({"git", "commit", "-m", "Music sync test"}, clone_mus.string()).exit_code == 0);

        auto manual_sync = caps.call("recommend.feed.v1", {
            {"action", "sync_github"},
            {"obsidian_vault_dir", clone_obs.string()},
            {"music_daily_dir", clone_mus.string()},
        });
        CHECK(manual_sync.ok);
        CHECK(manual_sync.data.value("pushed_any", false) == true);
        const std::string mus_local_head = ai_curator::trim(
            ai_curator::runProcess({"git", "rev-parse", "HEAD"}, clone_mus.string()).stdout_text);
        const std::string mus_bare_head = ai_curator::trim(
            ai_curator::runProcess({"git", "rev-parse", "refs/heads/main"}, bare_mus.string()).stdout_text);
        CHECK(mus_local_head == mus_bare_head);
    }

    // Test cancelling a slow background agent process group
    CHECK(caps.call("recommend.feed.v1", {
        {"action", "configure_daily_scheduler"},
        {"custom_agent_command", "sleep 30"},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    }).ok);
    auto slow_run = caps.call("recommend.feed.v1", {
        {"action", "run_daily_workflows"},
        {"project", "music"},
        {"wait", false},
        {"obsidian_vault_dir", git_obs.string()},
        {"music_daily_dir", git_mus.string()},
    });
    CHECK(slow_run.ok);
    CHECK(slow_run.data.value("started", false) == true);
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    auto cancel_res = caps.call("recommend.feed.v1", {{"action", "cancel_daily_workflows"}});
    CHECK(cancel_res.ok);
    CHECK(cancel_res.data.value("was_running", false) == true);

    plugin.onUnload();
    fs::remove_all(temp_root);
    std::cout << "ALL test_ai_curator tests PASSED!\n";
    return 0;
}
