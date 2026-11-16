#include "ai_curator/archive_rehydrator.hpp"

#include "ai_curator/utils.hpp"
#include <algorithm>
#include <cctype>
#include <chrono>
#include <filesystem>
#include <omnidesk/markdown_view.hpp>
#include <omnidesk/types_json.hpp>
#include <sstream>

namespace fs = std::filesystem;

namespace ai_curator {
namespace {

struct ManifestFileEntry {
    std::string rel_path;
    uintmax_t size = 0;
    int64_t mtime_ns = 0;
};

int64_t fileMtimeNs(const fs::directory_entry& entry) {
    std::error_code ec;
    auto ft = entry.last_write_time(ec);
    if (ec) return 0;
    return std::chrono::duration_cast<std::chrono::nanoseconds>(ft.time_since_epoch()).count();
}

std::string extractDateFromFilename(const fs::path& p) {
    std::string stem = p.stem().string();
    if (isValidIsoDate(stem)) return stem;
    return "";
}

void recordProvenance(std::unordered_map<std::string, std::string>& map,
                      const std::string& key,
                      const std::string& date) {
    if (key.empty() || date.empty()) return;
    auto it = map.find(key);
    if (it == map.end() || date < it->second) {
        map[key] = date;
    }
}

std::string rtrim(const std::string& s) {
    size_t end = s.size();
    while (end > 0 && (s[end - 1] == ' ' || s[end - 1] == '\t' || s[end - 1] == '\r')) {
        --end;
    }
    return s.substr(0, end);
}

std::vector<std::string> splitLines(const std::string& s) {
    std::vector<std::string> lines;
    std::istringstream iss(s);
    std::string line;
    while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        lines.push_back(std::move(line));
    }
    return lines;
}

std::string stripAutoComments(const std::string& body) {
    std::string out = body;
    const char* markers[] = {"<!-- AUTO:START -->", "<!-- AUTO:END -->"};
    for (const char* m : markers) {
        std::string marker(m);
        size_t pos = 0;
        while ((pos = out.find(marker, pos)) != std::string::npos) {
            out.erase(pos, marker.size());
        }
    }
    return out;
}

bool matchBoldField(const std::string& line,
                    const std::vector<std::string>& keys,
                    std::string& out_val) {
    std::string t = trim(line);
    if (startsWith(t, "- ")) t = trim(t.substr(2));
    for (const auto& k : keys) {
        for (const std::string& pat : {"**" + k + ":**", "**" + k + "**:", "**" + k + "：**", "**" + k + "**："}) {
            if (startsWith(t, pat)) {
                out_val = trim(t.substr(pat.size()));
                if (startsWith(out_val, "`") && endsWith(out_val, "`") && out_val.size() >= 2) {
                    out_val = out_val.substr(1, out_val.size() - 2);
                }
                return true;
            }
        }
        for (const std::string& pat : {"**" + k + "：", "**" + k + ":"}) {
            if (startsWith(t, pat)) {
                std::string val = trim(t.substr(pat.size()));
                if (endsWith(val, "**")) val = trim(val.substr(0, val.size() - 2));
                out_val = val;
                return true;
            }
        }
    }
    return false;
}

int parseLeadingNumberedHeading(const std::string& line, std::string& out_title) {
    std::string t = trim(line);
    if (!startsWith(t, "## ")) return -1;
    std::string rest = trim(t.substr(3));
    size_t i = 0;
    while (i < rest.size() && std::isdigit(static_cast<unsigned char>(rest[i]))) ++i;
    if (i > 0 && i < rest.size() && (rest[i] == '.' || rest[i] == ' ')) {
        int num = std::stoi(rest.substr(0, i));
        size_t j = i;
        if (j < rest.size() && rest[j] == '.') ++j;
        out_title = trim(rest.substr(j));
        return num;
    }
    return -1;
}

int parseJournalHeading(const std::string& line, std::string& out_title) {
    std::string t = trim(line);
    if (!startsWith(t, "## ")) return -1;
    std::string rest = trim(t.substr(3));
    static const std::pair<const char*, int> circled[] = {
        {"①", 1}, {"②", 2}, {"③", 3}, {"④", 4}, {"⑤", 5},
    };
    for (const auto& [sym, num] : circled) {
        if (startsWith(rest, sym)) {
            std::string after = trim(rest.substr(std::char_traits<char>::length(sym)));
            size_t bar = after.find("｜");
            if (bar != std::string::npos && bar < 30) {
                std::string candidate = trim(after.substr(bar + std::char_traits<char>::length("｜")));
                if (!candidate.empty()) after = candidate;
            }
            out_title = after;
            return num;
        }
    }
    return parseLeadingNumberedHeading(line, out_title);
}

std::string extractMarkdownLinkUrl(const std::string& text, const std::string& label_contains) {
    size_t pos = 0;
    while ((pos = text.find('[', pos)) != std::string::npos) {
        size_t cb = text.find(']', pos + 1);
        if (cb == std::string::npos) break;
        if (cb + 1 < text.size() && text[cb + 1] == '(') {
            size_t cp = text.find(')', cb + 2);
            if (cp != std::string::npos) {
                std::string label = text.substr(pos + 1, cb - (pos + 1));
                std::string url = text.substr(cb + 2, cp - (cb + 2));
                if (toLowerAscii(label).find(toLowerAscii(label_contains)) != std::string::npos) {
                    return trim(url);
                }
                pos = cp + 1;
                continue;
            }
        }
        pos = cb + 1;
    }
    return "";
}

void parseMusicDailyStream(const std::string& obsidian_vault_dir,
                           const std::string& music_daily_dir,
                           HydratedArchive& out) {
    std::error_code ec;
    std::unordered_map<std::string, std::string> music_user_notes;
    fs::path obs_music_dir = fs::path(obsidian_vault_dir) / "Music" / "Daily";
    if (fs::exists(obs_music_dir, ec)) {
        for (const auto& entry : fs::directory_iterator(obs_music_dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".md") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            auto txt = readFileText(entry.path().string());
            if (txt.has_value()) {
                std::string notes = extractUserNotesAfterAutoEnd(*txt);
                if (!notes.empty()) {
                    music_user_notes[date] = std::move(notes);
                }
            }
        }
    }

    std::map<std::string, fs::path, std::greater<std::string>> date_to_manifest;
    fs::path md_recs = fs::path(music_daily_dir) / "recommendations";
    if (fs::exists(md_recs, ec)) {
        for (const auto& entry : fs::recursive_directory_iterator(md_recs, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            date_to_manifest[date] = entry.path();
        }
    }
    fs::path obs_manifests = obs_music_dir / "manifests";
    if (fs::exists(obs_manifests, ec)) {
        for (const auto& entry : fs::directory_iterator(obs_manifests, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            if (date_to_manifest.find(date) == date_to_manifest.end()) {
                date_to_manifest[date] = entry.path();
            }
        }
    }

    for (const auto& [date, json_path] : date_to_manifest) {
        auto jopt = readJsonFile(json_path.string());
        if (!jopt.has_value() || !jopt->is_object()) continue;
        const auto& j = *jopt;
        const std::string doc_date = j.value("date", date);
        if (!j.contains("tracks") || !j["tracks"].is_array()) continue;

        const std::string notes = music_user_notes.count(doc_date) ? music_user_notes[doc_date] : "";
        for (const auto& tr : j["tracks"]) {
            if (!tr.is_object()) continue;
            const int position = tr.value("position", 0);
            const std::string title = tr.value("title", "");
            const std::string artist = tr.value("artist", "");
            std::string album;
            if (tr.contains("album") && tr["album"].is_string()) {
                album = tr["album"].get<std::string>();
            }
            std::string canonical_id = tr.value("canonical_id", "");
            if (canonical_id.empty() && !artist.empty() && !title.empty()) {
                canonical_id = artist + "::" + title;
            }
            std::string song_id;
            std::string play_url;
            std::string storefront = "cn";
            if (tr.contains("apple_music") && tr["apple_music"].is_object()) {
                const auto& am = tr["apple_music"];
                if (am.contains("song_id") && am["song_id"].is_string()) {
                    song_id = am["song_id"].get<std::string>();
                } else if (am.contains("song_id") && am["song_id"].is_number()) {
                    song_id = std::to_string(am["song_id"].get<int64_t>());
                }
                if (am.contains("url") && am["url"].is_string()) {
                    play_url = am["url"].get<std::string>();
                }
                if (am.contains("storefront") && am["storefront"].is_string()) {
                    storefront = am["storefront"].get<std::string>();
                }
            }
            if (song_id.empty() && !play_url.empty()) {
                song_id = extractAppleMusicSongId(play_url);
            }
            const std::string analysis = tr.value("analysis_summary", "");
            const std::string cue = tr.value("listening_cue", "");

            omnidesk::MediaItem item;
            item.id = !song_id.empty() ? ("am-cn-" + song_id)
                                       : ("am-cn-" + doc_date + "-" + std::to_string(position));
            item.source_id = "plugin-ai-curator";
            item.title = title;
            item.creator = artist + (album.empty() ? "" : (" · " + album));
            item.kind = "music";
            item.source_badge = "每日 5 首 · " + doc_date;
            item.play_url = play_url;
            item.quality_label = "Apple Music CN · ALAC";
            item.has_bilingual_sub = true;
            item.extra = {
                {"curation_mode", "daily_immutable"},
                {"domain", "music"},
                {"business_date", doc_date},
                {"priority_rank", position},
                {"apple_music_song_id", song_id},
                {"storefront", storefront},
                {"canonical_id", canonical_id},
                {"artist", artist},
                {"album", album},
                {"why_recommended", analysis},
                {"listening_or_reading_cue", cue},
                {"user_notes", notes},
                {"verification_method", "apple_music_cn_catalog"},
            };

            out.media_items.push_back(std::move(item));
            out.music_tracks_hydrated++;
            out.date_stats[doc_date].music++;
        }
    }
}

void parseYouTubeDailyStream(const std::string& obsidian_vault_dir, HydratedArchive& out) {
    std::error_code ec;
    fs::path yt_dir = fs::path(obsidian_vault_dir) / "Resources" / "YouTube" / "Daily";
    std::map<std::string, fs::path, std::greater<std::string>> files;
    if (fs::exists(yt_dir, ec)) {
        for (const auto& entry : fs::directory_iterator(yt_dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".md") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            files[date] = entry.path();
        }
    }

    for (const auto& [date, path] : files) {
        auto raw_opt = readFileText(path.string());
        if (!raw_opt.has_value()) continue;
        std::unordered_map<std::string, std::string> fm;
        std::string body;
        parseFrontmatter(*raw_opt, fm, body);
        const std::string user_notes = extractUserNotesAfterAutoEnd(*raw_opt);
        body = stripAutoComments(body);
        const auto lines = splitLines(body);

        std::string daily_priority;
        std::string verification_banner;
        bool in_priority = false;

        struct YtParsed {
            int rank = 0;
            std::string title;
            std::string channel;
            std::string topic;
            std::string url;
            std::string video_id;
            std::string why_watch;
            std::string verification_note;
            std::string verification_method;
        };
        std::vector<YtParsed> videos;
        YtParsed current;
        bool in_video = false;
        std::string sub_section;

        auto flush_video = [&]() {
            if (!in_video) return;
            current.why_watch = trim(current.why_watch);
            current.verification_note = trim(current.verification_note);
            if (current.video_id.empty() && !current.url.empty()) {
                current.video_id = extractYouTubeVideoId(current.url);
            }
            if (current.verification_method.empty() &&
                current.verification_note.find("creator_official_page") != std::string::npos) {
                current.verification_method = "creator_official_page";
            }
            videos.push_back(std::move(current));
            current = YtParsed{};
            in_video = false;
            sub_section.clear();
        };

        for (const auto& line : lines) {
            std::string t = trim(line);
            std::string heading_title;
            int rank = parseLeadingNumberedHeading(t, heading_title);
            if (rank > 0) {
                in_priority = false;
                flush_video();
                in_video = true;
                current.rank = rank;
                current.title = heading_title;
                continue;
            }
            if (startsWith(t, "## ")) {
                flush_video();
                in_priority = (t == "## 今日优先级");
                continue;
            }
            if (in_priority) {
                if (startsWith(t, ">")) {
                    std::string q = trim(t.substr(1));
                    if (!q.empty()) {
                        if (!verification_banner.empty()) verification_banner += " ";
                        verification_banner += q;
                    }
                } else if (!t.empty()) {
                    if (!daily_priority.empty()) daily_priority += "\n";
                    daily_priority += t;
                }
                continue;
            }
            if (!in_video) {
                if (startsWith(t, ">") && verification_banner.empty()) {
                    verification_banner = trim(t.substr(1));
                }
                continue;
            }

            if (startsWith(t, "### ")) {
                std::string sh = toLowerAscii(trim(t.substr(4)));
                if (sh.find("why") != std::string::npos) sub_section = "why";
                else if (sh.find("核验") != std::string::npos || sh.find("verify") != std::string::npos) sub_section = "verify";
                else sub_section.clear();
                continue;
            }

            std::string val;
            if (matchBoldField(t, {"Channel", "频道"}, val)) {
                current.channel = val;
                continue;
            }
            if (matchBoldField(t, {"Topic", "主题"}, val)) {
                current.topic = val;
                continue;
            }
            if (matchBoldField(t, {"URL", "链接"}, val)) {
                auto urls = extractUrlsFromText(val);
                current.url = urls.empty() ? val : urls.front();
                current.video_id = extractYouTubeVideoId(current.url);
                continue;
            }
            if (matchBoldField(t, {"Why watch"}, val)) {
                if (!current.why_watch.empty()) current.why_watch += "\n";
                current.why_watch += val;
                sub_section = "why";
                continue;
            }

            if (sub_section == "why" && !t.empty()) {
                if (!current.why_watch.empty()) current.why_watch += "\n";
                current.why_watch += t;
            } else if (sub_section == "verify" && !t.empty()) {
                if (!current.verification_note.empty()) current.verification_note += "\n";
                current.verification_note += t;
                size_t vm = t.find("verification_method:");
                if (vm != std::string::npos) {
                    std::string m = trim(t.substr(vm + 20));
                    while (!m.empty() && (m.back() == '.' || m.back() == ',' || m.back() == '`')) m.pop_back();
                    current.verification_method = m;
                }
            }
        }
        flush_video();

        for (const auto& v : videos) {
            const bool is_wildcard = toLowerAscii(v.topic).find("wildcard") != std::string::npos;
            omnidesk::MediaItem item;
            item.id = "yt-" + date + "-" + (!v.video_id.empty() ? v.video_id : std::to_string(v.rank));
            item.source_id = "plugin-ai-curator";
            item.title = v.title;
            item.creator = v.channel;
            item.kind = "video";
            item.source_badge = "YouTube 精选 · " + date;
            if (v.video_id.size() == 11) {
                item.cover_url = "https://i.ytimg.com/vi/" + v.video_id + "/hqdefault.jpg";
            }
            item.play_url = v.url;
            item.quality_label = is_wildcard ? "YouTube · Wildcard" : "YouTube";
            item.has_bilingual_sub = false;
            item.extra = {
                {"curation_mode", "daily_immutable"},
                {"domain", "youtube"},
                {"business_date", date},
                {"priority_rank", v.rank},
                {"video_id", v.video_id},
                {"topic", v.topic},
                {"is_wildcard", is_wildcard},
                {"why_recommended", v.why_watch},
                {"listening_or_reading_cue", daily_priority},
                {"verification_note", v.verification_note.empty() ? verification_banner : v.verification_note},
                {"verification_method", v.verification_method},
                {"user_notes", user_notes},
            };
            out.media_items.push_back(std::move(item));
            out.youtube_videos_hydrated++;
            out.date_stats[date].youtube++;
        }
    }

    fs::path hist_path = fs::path(obsidian_vault_dir) / ".recommendation" / "history" / "youtube.jsonl";
    if (auto txt = readFileText(hist_path.string())) {
        for (const auto& line : splitLines(*txt)) {
            std::string t = trim(line);
            if (t.empty()) continue;
            try {
                auto j = nlohmann::json::parse(t);
                std::string d = j.value("date", "");
                std::string vid = j.value("video_id", "");
                std::string title = j.value("title", "");
                if (!d.empty() && !vid.empty()) {
                    recordProvenance(out.video_id_to_date, vid, d);
                    recordProvenance(out.url_to_date, "https://www.youtube.com/watch?v=" + vid, d);
                }
                if (!d.empty() && !title.empty()) {
                    recordProvenance(out.norm_title_to_date, normalizeForDedup(title), d);
                }
            } catch (...) {
            }
        }
    }
}

void parseNewsDailyStream(const std::string& obsidian_vault_dir, HydratedArchive& out) {
    std::error_code ec;
    fs::path news_dir = fs::path(obsidian_vault_dir) / "News" / "Daily";
    std::map<std::string, fs::path, std::greater<std::string>> files;
    if (fs::exists(news_dir, ec)) {
        for (const auto& entry : fs::directory_iterator(news_dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".md") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            files[date] = entry.path();
        }
    }

    for (const auto& [date, path] : files) {
        auto raw_opt = readFileText(path.string());
        if (!raw_opt.has_value()) continue;
        std::unordered_map<std::string, std::string> fm;
        std::string body;
        parseFrontmatter(*raw_opt, fm, body);
        const std::string user_notes = extractUserNotesAfterAutoEnd(*raw_opt);
        body = stripAutoComments(body);
        const auto lines = splitLines(body);

        std::string exec_view;
        bool in_exec = false;
        struct Story {
            int rank = 0;
            std::string title;
            std::string event_date;
            std::string impact;
            std::string facts;
            std::string why;
            std::string impact_text;
            std::string watch;
            std::string sources_text;
            std::vector<std::string> source_urls;
        };
        std::vector<Story> stories;
        Story cur;
        bool in_story = false;
        std::string sub;

        auto flush_story = [&]() {
            if (!in_story) return;
            cur.facts = trim(cur.facts);
            cur.why = trim(cur.why);
            cur.impact_text = trim(cur.impact_text);
            cur.watch = trim(cur.watch);
            cur.sources_text = trim(cur.sources_text);
            cur.source_urls = extractUrlsFromText(cur.sources_text);
            stories.push_back(std::move(cur));
            cur = Story{};
            in_story = false;
            sub.clear();
        };

        for (const auto& line : lines) {
            std::string t = trim(line);
            std::string st_title;
            int rank = parseLeadingNumberedHeading(t, st_title);
            if (rank > 0) {
                in_exec = false;
                flush_story();
                in_story = true;
                cur.rank = rank;
                cur.title = st_title;
                continue;
            }
            if (startsWith(t, "## ")) {
                flush_story();
                in_exec = (t == "## Executive View");
                continue;
            }
            if (in_exec) {
                if (t == "---") continue;
                if (!t.empty()) {
                    if (!exec_view.empty()) exec_view += "\n";
                    exec_view += t;
                }
                continue;
            }
            if (!in_story) continue;

            if (startsWith(t, "### ")) {
                std::string sh = toLowerAscii(trim(t.substr(4)));
                if (sh.find("事实") != std::string::npos || sh.find("发生") != std::string::npos) sub = "facts";
                else if (sh.find("why it matters") != std::string::npos) sub = "why";
                else if (sh.find("long-term") != std::string::npos) sub = "impact";
                else if (sh.find("watch") != std::string::npos) sub = "watch";
                else if (sh.find("source") != std::string::npos) sub = "sources";
                else sub.clear();
                continue;
            }

            std::string val;
            if (sub.empty() && matchBoldField(t, {"事件日期", "Event date"}, val)) {
                cur.event_date = val;
                continue;
            }
            if (sub.empty() && matchBoldField(t, {"影响等级", "Impact"}, val)) {
                cur.impact = val;
                continue;
            }
            if (t == "---") continue;
            if (!t.empty()) {
                std::string* dst = nullptr;
                if (sub == "facts") dst = &cur.facts;
                else if (sub == "why") dst = &cur.why;
                else if (sub == "impact") dst = &cur.impact_text;
                else if (sub == "watch") dst = &cur.watch;
                else if (sub == "sources") dst = &cur.sources_text;
                if (dst) {
                    if (!dst->empty()) *dst += "\n";
                    *dst += t;
                }
            }
        }
        flush_story();
        exec_view = trim(exec_view);

        if (stories.empty() && exec_view.empty()) continue;
        out.news_days_hydrated++;

        {
            const std::string issue_id = "news-" + date;
            omnidesk::TextFeedItem fi;
            fi.id = issue_id;
            fi.source_id = "plugin-ai-curator";
            fi.source_name = "每日早报 (News)";
            fi.category = "news";
            fi.title = "Daily News · " + date + "（" + std::to_string(stories.size()) + " 条深度早报）";
            fi.excerpt = utf8Truncate(exec_view.empty() ? (stories.empty() ? "" : stories[0].facts) : exec_view, 220);
            fi.author = "AI Curation";
            fi.published_at = date;
            fi.tags = {"news", date};
            out.text_items.push_back(fi);

            omnidesk::TextArticle art;
            art.id = issue_id;
            art.source_name = "每日早报 (News)";
            art.category = "news";
            art.title = fi.title;
            art.subtitle = "Long-Horizon Daily News · " + date;
            art.authors = "AI Curation";
            art.published_at = date;
            art.tags = fi.tags;
            int pidx = 1;
            if (!exec_view.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Executive View", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", exec_view, ""});
            }
            for (const auto& s : stories) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading",
                                          std::to_string(s.rank) + ". " + s.title, ""});
                if (!s.event_date.empty() || !s.impact.empty()) {
                    art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote",
                                              "事件日期: " + s.event_date + " · 影响等级: " + s.impact, ""});
                }
                if (!s.facts.empty()) art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", "【事实摘要】\n" + s.facts, ""});
                if (!s.why.empty()) art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", "【Why it matters】\n" + s.why, ""});
                if (!s.impact_text.empty()) art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", "【Long-term impact】\n" + s.impact_text, ""});
                if (!s.watch.empty()) art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", "【What to watch next】\n" + s.watch, ""});
                if (!s.sources_text.empty()) art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote", "Sources:\n" + s.sources_text, ""});
            }
            if (!user_notes.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "My Notes", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote", user_notes, ""});
            }
            out.articles[issue_id] = std::move(art);
        }

        for (const auto& s : stories) {
            const std::string sid = "news-" + date + "-" + std::to_string(s.rank);
            omnidesk::TextFeedItem fi;
            fi.id = sid;
            fi.source_id = "plugin-ai-curator";
            fi.source_name = "每日早报 (News)";
            fi.category = "news";
            fi.title = s.title;
            fi.excerpt = utf8Truncate(s.facts, 220);
            fi.author = s.impact.empty() ? "News Daily" : ("影响等级 " + s.impact);
            fi.published_at = date;
            fi.url = s.source_urls.empty() ? "" : s.source_urls.front();
            fi.tags = {"news", date};
            if (!s.impact.empty()) fi.tags.push_back("Impact " + s.impact);
            out.text_items.push_back(fi);

            omnidesk::TextArticle art;
            art.id = sid;
            art.source_name = "每日早报 (News)";
            art.category = "news";
            art.title = s.title;
            art.subtitle = date + (s.event_date.empty() ? "" : (" · 事件日期 " + s.event_date)) +
                           (s.impact.empty() ? "" : (" · 影响等级 " + s.impact));
            art.authors = "AI Curation";
            art.published_at = date;
            art.url = fi.url;
            art.tags = fi.tags;
            int pidx = 1;
            if (!s.facts.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "事实摘要", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", s.facts, ""});
            }
            if (!s.why.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Why it matters", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", s.why, ""});
            }
            if (!s.impact_text.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Long-term impact", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", s.impact_text, ""});
            }
            if (!s.watch.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "What to watch next", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", s.watch, ""});
            }
            if (!s.sources_text.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Sources", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote", s.sources_text, ""});
            }
            out.articles[sid] = std::move(art);
            out.news_stories_hydrated++;
            out.date_stats[date].news++;
        }
    }
}

void parseRedditDailyStream(const std::string& obsidian_vault_dir, HydratedArchive& out) {
    std::error_code ec;
    fs::path red_dir = fs::path(obsidian_vault_dir) / "Resources" / "Reddit" / "Daily";
    std::map<std::string, fs::path, std::greater<std::string>> files;
    if (fs::exists(red_dir, ec)) {
        for (const auto& entry : fs::directory_iterator(red_dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".md") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            files[date] = entry.path();
        }
    }

    for (const auto& [date, path] : files) {
        auto raw_opt = readFileText(path.string());
        if (!raw_opt.has_value()) continue;
        std::unordered_map<std::string, std::string> fm;
        std::string body;
        parseFrontmatter(*raw_opt, fm, body);
        body = stripAutoComments(body);
        const auto lines = splitLines(body);

        struct Post {
            int rank = 0;
            std::string title;
            std::string subreddit;
            std::string topic;
            std::string url;
            std::string post_id;
            std::string age;
            std::string verification;
            std::string why_read;
            std::string discussion_note;
        };
        std::vector<Post> posts;
        Post cur;
        bool in_post = false;
        bool in_why = false;

        auto flush_post = [&]() {
            if (!in_post) return;
            cur.why_read = trim(cur.why_read);
            if (cur.post_id.empty() && !cur.url.empty()) {
                cur.post_id = extractRedditPostId(cur.url);
            }
            posts.push_back(std::move(cur));
            cur = Post{};
            in_post = false;
            in_why = false;
        };

        for (const auto& line : lines) {
            std::string t = trim(line);
            std::string ptitle;
            int rank = parseLeadingNumberedHeading(t, ptitle);
            if (rank > 0) {
                flush_post();
                in_post = true;
                cur.rank = rank;
                cur.title = ptitle;
                continue;
            }
            if (startsWith(t, "## ")) {
                flush_post();
                continue;
            }
            if (!in_post) continue;
            if (startsWith(t, "### ")) {
                std::string sh = toLowerAscii(trim(t.substr(4)));
                in_why = (sh.find("why") != std::string::npos);
                continue;
            }
            std::string val;
            if (matchBoldField(t, {"Subreddit", "subreddit"}, val)) {
                cur.subreddit = val;
                continue;
            }
            if (matchBoldField(t, {"Topic", "主题"}, val)) {
                cur.topic = val;
                continue;
            }
            if (matchBoldField(t, {"URL", "链接"}, val)) {
                auto urls = extractUrlsFromText(val);
                cur.url = urls.empty() ? val : urls.front();
                cur.post_id = extractRedditPostId(cur.url);
                continue;
            }
            if (matchBoldField(t, {"Age", "日期/约龄"}, val)) {
                cur.age = val;
                continue;
            }
            if (matchBoldField(t, {"original_post_verification"}, val)) {
                cur.verification = val;
                continue;
            }
            if (matchBoldField(t, {"Why read"}, val)) {
                cur.why_read = val;
                in_why = true;
                continue;
            }
            if (matchBoldField(t, {"Discussion note"}, val)) {
                cur.discussion_note = val;
                in_why = false;
                continue;
            }
            if (in_why && !t.empty()) {
                if (!cur.why_read.empty()) cur.why_read += "\n";
                cur.why_read += t;
            }
        }
        flush_post();
        if (!posts.empty()) out.reddit_days_hydrated++;

        for (const auto& p : posts) {
            const std::string pid = "reddit-" + date + "-" + (!p.post_id.empty() ? p.post_id : std::to_string(p.rank));
            omnidesk::TextFeedItem fi;
            fi.id = pid;
            fi.source_id = "plugin-ai-curator";
            fi.source_name = p.subreddit.empty() ? "Reddit 每日精选" : p.subreddit;
            fi.category = "reddit";
            fi.title = p.title;
            std::string reddit_meta = p.topic;
            if (!p.age.empty()) {
                reddit_meta += (reddit_meta.empty() ? "" : " · ") + p.age;
            }
            fi.excerpt = utf8Truncate(reddit_meta, 120);
            fi.author = p.subreddit;
            fi.published_at = date;
            fi.url = p.url;
            fi.tags = {"reddit", date};
            if (!p.subreddit.empty()) fi.tags.push_back(p.subreddit);
            if (toLowerAscii(p.topic).find("wildcard") != std::string::npos) fi.tags.push_back("wildcard");
            out.text_items.push_back(fi);

            omnidesk::TextArticle art;
            art.id = pid;
            art.source_name = fi.source_name;
            art.category = "reddit";
            art.title = p.title;
            art.subtitle = p.subreddit + (p.topic.empty() ? "" : (" · " + p.topic)) + " · " + date;
            art.authors = p.subreddit;
            art.published_at = date;
            art.url = p.url;
            art.tags = fi.tags;
            int pidx = 1;
            std::string meta = "Subreddit: " + p.subreddit;
            if (!p.topic.empty()) meta += "\nTopic: " + p.topic;
            if (!p.age.empty()) meta += "\nAge: " + p.age;
            if (!p.verification.empty()) meta += "\noriginal_post_verification: " + p.verification;
            if (!p.url.empty()) meta += "\nURL: " + p.url;
            art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote", meta, ""});
            if (!p.why_read.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Why read", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", p.why_read, ""});
            }
            if (!p.discussion_note.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "Discussion note", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", p.discussion_note, ""});
            }
            out.articles[pid] = std::move(art);
            out.reddit_posts_hydrated++;
            out.date_stats[date].reddit++;
        }
    }

    fs::path hist_path = fs::path(obsidian_vault_dir) / ".recommendation" / "history" / "reddit.jsonl";
    if (auto txt = readFileText(hist_path.string())) {
        for (const auto& line : splitLines(*txt)) {
            std::string t = trim(line);
            if (t.empty()) continue;
            try {
                auto j = nlohmann::json::parse(t);
                std::string d = j.value("date", "");
                std::string pid = j.value("post_id", "");
                if (!d.empty() && !pid.empty()) {
                    recordProvenance(out.post_id_to_date, pid, d);
                }
            } catch (...) {
            }
        }
    }
}

void parseJournalDailyStream(const std::string& obsidian_vault_dir, HydratedArchive& out) {
    std::error_code ec;
    fs::path j_dir = fs::path(obsidian_vault_dir) / "00-inbox" / "Journal";
    std::map<std::string, fs::path, std::greater<std::string>> files;
    if (fs::exists(j_dir, ec)) {
        for (const auto& entry : fs::directory_iterator(j_dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".md") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            files[date] = entry.path();
        }
    }

    for (const auto& [date, path] : files) {
        auto raw_opt = readFileText(path.string());
        if (!raw_opt.has_value()) continue;
        std::unordered_map<std::string, std::string> fm;
        std::string body;
        parseFrontmatter(*raw_opt, fm, body);
        body = stripAutoComments(body);
        const auto lines = splitLines(body);

        std::string sci_question;
        std::string reading_order;
        bool in_sci_q = false;
        bool in_order = false;

        struct Paper {
            int rank = 0;
            std::string title;
            std::string chinese_sub;
            std::string bib_line;
            std::string bib_line_md;
            std::string doi;
            std::string doi_url;
            std::string pubmed_url;
            std::string pmc_url;
            std::vector<std::pair<std::string, std::string>> sections;
        };
        std::vector<Paper> papers;
        Paper cur;
        bool in_paper = false;

        auto flush_paper = [&]() {
            if (!in_paper) return;
            if (cur.doi.empty()) {
                cur.doi = extractDoiFromText(cur.bib_line + " " + cur.chinese_sub);
            }
            if (cur.doi_url.empty() && !cur.doi.empty()) {
                cur.doi_url = "https://doi.org/" + cur.doi;
            }
            papers.push_back(std::move(cur));
            cur = Paper{};
            in_paper = false;
        };

        for (const auto& line : lines) {
            std::string t = trim(line);
            std::string ptitle;
            int rank = parseJournalHeading(t, ptitle);
            if (rank > 0) {
                in_sci_q = false;
                in_order = false;
                flush_paper();
                in_paper = true;
                cur.rank = rank;
                cur.title = ptitle;
                continue;
            }
            if (startsWith(t, "## ")) {
                flush_paper();
                in_sci_q = (t == "## 今日科学问题");
                in_order = (!in_sci_q && t != "## Notes" && t != "## My Notes");
                if (in_order && !reading_order.empty()) reading_order += "\n\n";
                if (in_order) reading_order += t.substr(3);
                continue;
            }
            if (in_sci_q) {
                if (!t.empty()) {
                    if (!sci_question.empty()) sci_question += "\n";
                    sci_question += rtrim(line);
                }
                continue;
            }
            if (in_order) {
                if (!t.empty() && !startsWith(t, "[[") && !startsWith(t, "[Journal 索引]")) {
                    if (!reading_order.empty()) reading_order += "\n";
                    reading_order += rtrim(line);
                }
                continue;
            }
            if (!in_paper || t.empty()) continue;

            if (startsWith(t, "- **")) {
                size_t close_b = t.find("**", 4);
                if (close_b != std::string::npos) {
                    std::string label = trim(t.substr(4, close_b - 4));
                    while (!label.empty() && (label.back() == ':' || endsWith(label, "："))) {
                        if (label.back() == ':') label.pop_back();
                        else label.erase(label.size() - std::char_traits<char>::length("："));
                    }
                    std::string val = trim(t.substr(close_b + 2));
                    if (startsWith(val, "：") || startsWith(val, ":")) {
                        val = trim(val.substr(startsWith(val, "：") ? std::char_traits<char>::length("：") : 1));
                    }
                    cur.sections.emplace_back(label, val);
                    continue;
                }
            }
            if (cur.doi.empty()) {
                std::string d = extractDoiFromText(t);
                if (!d.empty()) cur.doi = d;
            }
            if (cur.doi_url.empty()) {
                std::string u = extractMarkdownLinkUrl(t, "doi");
                if (!u.empty()) cur.doi_url = u;
            }
            if (cur.pubmed_url.empty()) {
                std::string u = extractMarkdownLinkUrl(t, "pubmed");
                if (u.empty()) u = extractMarkdownLinkUrl(t, "核验");
                if (!u.empty()) cur.pubmed_url = u;
            }
            if (cur.pmc_url.empty()) {
                std::string u = extractMarkdownLinkUrl(t, "pmc");
                if (!u.empty()) cur.pmc_url = u;
            }
            for (const auto& u : extractUrlsFromText(t)) {
                if (cur.doi_url.empty() && u.find("doi.org/") != std::string::npos) cur.doi_url = u;
                else if (cur.pubmed_url.empty() && u.find("pubmed.ncbi.nlm.nih.gov") != std::string::npos) cur.pubmed_url = u;
                else if (cur.pmc_url.empty() && u.find("pmc.ncbi.nlm.nih.gov") != std::string::npos) cur.pmc_url = u;
            }
            if (startsWith(t, "**中文释义：")) {
                std::string cs = t.substr(std::char_traits<char>::length("**中文释义："));
                if (endsWith(cs, "**")) cs = cs.substr(0, cs.size() - 2);
                cur.chinese_sub = trim(cs);
            } else if (cur.bib_line.empty()) {
                cur.bib_line_md = t;
                cur.bib_line = stripMarkdownLinks(t);
            } else if (!cur.sections.empty()) {
                cur.sections.back().second += "\n" + rtrim(line);
            }
        }
        flush_paper();
        if (!papers.empty()) out.journal_days_hydrated++;

        for (const auto& p : papers) {
            const std::string jid = "journal-" + date + "-" + std::to_string(p.rank);
            std::string primary_url = !p.pmc_url.empty() ? p.pmc_url
                                                         : (!p.doi_url.empty() ? p.doi_url : p.pubmed_url);
            std::string excerpt = !p.chinese_sub.empty() ? p.chinese_sub : p.bib_line;
            std::string authors;
            std::string journal_name = "科学期刊精读";
            if (!p.bib_line.empty()) {
                size_t dot = p.bib_line.find("et al.");
                if (dot != std::string::npos) {
                    authors = trim(p.bib_line.substr(0, dot + 6));
                }
                size_t star1 = p.bib_line.find('*');
                if (star1 != std::string::npos) {
                    size_t star2 = p.bib_line.find('*', star1 + 1);
                    if (star2 != std::string::npos && star2 > star1 + 1) {
                        journal_name = p.bib_line.substr(star1 + 1, star2 - star1 - 1);
                    }
                }
            }

            omnidesk::TextFeedItem fi;
            fi.id = jid;
            fi.source_id = "plugin-ai-curator";
            fi.source_name = journal_name;
            fi.category = "journal";
            fi.title = p.title;
            fi.excerpt = utf8Truncate(excerpt, 240);
            fi.author = authors.empty() ? "Journal Daily" : authors;
            fi.published_at = date;
            fi.url = primary_url;
            fi.tags = {"journal", date};
            if (!p.doi.empty()) fi.tags.push_back("DOI:" + p.doi);
            out.text_items.push_back(fi);

            omnidesk::TextArticle art;
            art.id = jid;
            art.source_name = journal_name;
            art.category = "journal";
            art.title = p.title;
            art.subtitle = p.chinese_sub.empty() ? p.bib_line : p.chinese_sub;
            art.authors = authors;
            art.published_at = date;
            art.url = primary_url;
            art.doi = p.doi;
            art.tags = fi.tags;
            int pidx = 1;
            if (!sci_question.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "今日科学问题", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote", sci_question, ""});
            }
            if (!p.bib_line.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote",
                                          p.bib_line_md.empty() ? p.bib_line : p.bib_line_md, ""});
            }
            for (const auto& [label, content] : p.sections) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", label, ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "paragraph", content, ""});
            }
            if (!reading_order.empty()) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "heading", "建议阅读顺序与范围边界", ""});
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), "quote", trim(reading_order), ""});
            }
            out.articles[jid] = std::move(art);
            out.journal_papers_hydrated++;
            out.date_stats[date].journal++;
        }
    }

    fs::path hist_path = fs::path(obsidian_vault_dir) / ".recommendation" / "history" / "journal.jsonl";
    if (auto txt = readFileText(hist_path.string())) {
        for (const auto& line : splitLines(*txt)) {
            std::string t = trim(line);
            if (t.empty()) continue;
            try {
                auto j = nlohmann::json::parse(t);
                std::string d = j.value("date", "");
                std::string doi = toLowerAscii(j.value("normalized_doi", j.value("doi", "")));
                std::string title = j.value("title", "");
                std::string url = j.value("source_url", "");
                if (!d.empty() && !doi.empty()) recordProvenance(out.doi_to_date, doi, d);
                if (!d.empty() && !url.empty()) recordProvenance(out.url_to_date, url, d);
                if (!d.empty() && !title.empty()) recordProvenance(out.norm_title_to_date, normalizeForDedup(title), d);
            } catch (...) {
            }
        }
    }
}

std::vector<omnidesk::BookPageBlock> parseTranslationBlocks(const std::string& md_body) {
    std::vector<omnidesk::BookPageBlock> blocks;
    const auto lines = splitLines(stripAutoComments(md_body));
    std::string cur_title;
    std::string cur_zh;
    std::string cur_en;
    bool in_answer = false;
    int bidx = 1;

    auto flush = [&]() {
        cur_zh = trim(cur_zh);
        cur_en = trim(cur_en);
        if (cur_zh.empty() && cur_en.empty()) return;
        if (!cur_title.empty()) {
            blocks.push_back({"b-" + std::to_string(bidx++), "heading", cur_title, "", {}});
        }
        blocks.push_back({"b-" + std::to_string(bidx++), "paragraph",
                          cur_en.empty() ? cur_zh : cur_en,
                          cur_en.empty() ? "" : cur_zh,
                          {}});
        cur_title.clear();
        cur_zh.clear();
        cur_en.clear();
        in_answer = false;
    };

    for (const auto& line : lines) {
        std::string t = trim(line);
        if (startsWith(t, "## 练习") || startsWith(t, "## Exercise") || startsWith(t, "## 间隔复习")) {
            flush();
            cur_title = trim(t.substr(3));
            continue;
        }
        if (startsWith(t, "> [!answer]")) {
            in_answer = true;
            continue;
        }
        if (in_answer) {
            if (startsWith(t, ">")) {
                std::string ans = trim(t.substr(1));
                if (!ans.empty()) {
                    if (!cur_en.empty()) cur_en += "\n";
                    cur_en += ans;
                }
            } else if (t.empty() || startsWith(t, "#")) {
                in_answer = false;
            }
            continue;
        }
        if (!cur_title.empty() && !t.empty() && !startsWith(t, "### ") && !startsWith(t, "**建议时间")) {
            if (!cur_zh.empty()) cur_zh += "\n";
            cur_zh += t;
        }
    }
    flush();
    return blocks;
}

std::vector<omnidesk::BookPageBlock> parseMarkdownAsBlocks(
    const std::string& md_body,
    const std::vector<std::string>& vocab_words = {},
    const std::string& skip_h1_title = "") {
    std::vector<omnidesk::BookPageBlock> blocks;
    const auto lines = splitLines(stripAutoComments(md_body));
    std::string cur_para;
    int bidx = 1;
    bool skipped_h1 = false;
    bool in_fence = false;
    char fence_ch = 0;
    size_t fence_len = 0;

    auto flush_para = [&]() {
        size_t start = 0;
        while (start < cur_para.size() && (cur_para[start] == '\n' || cur_para[start] == '\r')) ++start;
        size_t end = cur_para.size();
        while (end > start && (cur_para[end - 1] == ' ' || cur_para[end - 1] == '\t' ||
                               cur_para[end - 1] == '\n' || cur_para[end - 1] == '\r')) {
            --end;
        }
        std::string p = cur_para.substr(start, end - start);
        if (!p.empty() && !trim(p).empty()) {
            std::vector<std::string> hits;
            std::string lp = toLowerAscii(p);
            for (const auto& w : vocab_words) {
                if (!w.empty() && lp.find(toLowerAscii(w)) != std::string::npos) {
                    hits.push_back(w);
                }
            }
            blocks.push_back({"b-" + std::to_string(bidx++), "paragraph", p, "", std::move(hits)});
        }
        cur_para.clear();
    };

    for (const auto& line : lines) {
        std::string t = trim(line);
        if (!in_fence && (startsWith(t, "```") || startsWith(t, "~~~"))) {
            in_fence = true;
            fence_ch = t[0];
            fence_len = 0;
            while (fence_len < t.size() && t[fence_len] == fence_ch) ++fence_len;
            if (!cur_para.empty()) cur_para += "\n";
            cur_para += rtrim(line);
            continue;
        }
        if (in_fence) {
            if (!cur_para.empty()) cur_para += "\n";
            cur_para += rtrim(line);
            size_t run = 0;
            while (run < t.size() && t[run] == fence_ch) ++run;
            if (run >= fence_len && trim(t.substr(run)).empty()) {
                in_fence = false;
                flush_para();
            }
            continue;
        }
        if (t.empty()) {
            flush_para();
            continue;
        }
        if (startsWith(t, "# ")) {
            flush_para();
            std::string h1_txt = trim(t.substr(2));
            if (!skipped_h1 && !skip_h1_title.empty() && h1_txt == skip_h1_title) {
                skipped_h1 = true;
                continue;
            }
            blocks.push_back({"b-" + std::to_string(bidx++), "heading", h1_txt, "", {}});
        } else if (startsWith(t, "## ") || startsWith(t, "### ")) {
            flush_para();
            size_t off = startsWith(t, "### ") ? 4 : 3;
            blocks.push_back({"b-" + std::to_string(bidx++), "heading", trim(t.substr(off)), "", {}});
        } else {
            if (!cur_para.empty()) cur_para += "\n";
            cur_para += rtrim(line);
        }
    }
    flush_para();
    return blocks;
}

void parseEnglishStream(const std::string& obsidian_vault_dir, HydratedArchive& out) {
    std::error_code ec;
    struct SubDirSpec {
        std::string sub_rel;
        std::string component;
        std::string label;
    };
    const SubDirSpec specs[] = {
        {"Daily", "daily", "English Daily"},
        {"Translation", "translation", "English Translation"},
        {"Vocabulary", "vocabulary", "English Vocabulary"},
        {"Weekly", "weekly", "English Weekly"},
        {"Weekly/TV", "weekly-tv", "English Weekly TV"},
    };

    fs::path eng_root = fs::path(obsidian_vault_dir) / "50-courses" / "English";
    std::unordered_set<std::string> seen_vocab_lower;

    for (const auto& spec : specs) {
        fs::path dir = eng_root / spec.sub_rel;
        if (!fs::exists(dir, ec)) continue;
        std::map<std::string, fs::path, std::greater<std::string>> files;
        for (const auto& entry : fs::directory_iterator(dir, ec)) {
            if (!entry.is_regular_file() || entry.path().extension() != ".md") continue;
            std::string date = extractDateFromFilename(entry.path());
            if (date.empty() || date < "2026-09-04") continue;
            files[date] = entry.path();
        }

        for (const auto& [date, path] : files) {
            auto raw_opt = readFileText(path.string());
            if (!raw_opt.has_value()) continue;
            std::unordered_map<std::string, std::string> fm;
            std::string body;
            parseFrontmatter(*raw_opt, fm, body);
            std::string clean_body = stripAutoComments(body);

            std::string title;
            for (const auto& line : splitLines(clean_body)) {
                std::string t = trim(line);
                if (startsWith(t, "# ")) {
                    title = trim(t.substr(2));
                    break;
                }
            }
            if (title.empty()) {
                title = fm.count("source_title") ? fm["source_title"]
                                                 : (spec.label + " · " + date);
            }

            std::vector<omnidesk::VocabCard> day_vocabs;
            if (spec.component == "vocabulary") {
                day_vocabs = ArchiveRehydrator::parseVocabularyMarkdown(*raw_opt, date);
                for (const auto& vc : day_vocabs) {
                    std::string key = toLowerAscii(trim(vc.word));
                    if (!key.empty() && seen_vocab_lower.insert(key).second) {
                        out.vocab_cards.push_back(vc);
                        out.vocab_cards_hydrated++;
                    }
                }
            }

            std::vector<std::string> vocab_words;
            for (const auto& vc : day_vocabs) vocab_words.push_back(vc.word);
            const std::vector<omnidesk::BookPageBlock> full_md_blocks =
                parseMarkdownAsBlocks(clean_body, vocab_words, title);
            std::vector<omnidesk::BookPageBlock> book_blocks;
            if (spec.component == "translation") {
                book_blocks = parseTranslationBlocks(clean_body);
            }
            if (book_blocks.empty()) {
                book_blocks = full_md_blocks;
            }

            EnglishNoteEntry note;
            note.component = spec.component;
            note.date = date;
            note.title = title;
            note.rel_path = "50-courses/English/" + spec.sub_rel + "/" + date + ".md";
            note.raw_markdown = clean_body;
            note.blocks = book_blocks;
            note.vocab_cards = day_vocabs;
            auto& day_bundle = out.english_days[date];
            day_bundle.date = date;
            day_bundle.notes.push_back(std::move(note));

            const std::string eid = "english-" + spec.component + "-" + date;
            omnidesk::TextFeedItem fi;
            fi.id = eid;
            fi.source_id = "plugin-ai-curator";
            fi.source_name = spec.label;
            fi.category = "english";
            fi.title = title;
            std::string eng_excerpt;
            if (fm.count("source_title") && !fm["source_title"].empty()) {
                eng_excerpt = fm["source_title"];
                if (fm.count("level") && !fm["level"].empty()) {
                    eng_excerpt += " · " + fm["level"];
                }
            } else {
                eng_excerpt = omnidesk::sanitizeMarkdownExcerpt(clean_body, 180);
            }
            fi.excerpt = utf8Truncate(eng_excerpt, 220);
            fi.author = fm.count("source") ? fm["source"] : "AI Curation";
            fi.published_at = date;
            fi.url = fm.count("source_url") ? fm["source_url"] : "";
            fi.tags = {"english", spec.component, date};
            fi.bilingual_cached = (spec.component == "translation" || spec.component == "vocabulary");
            out.text_items.push_back(fi);

            omnidesk::TextArticle art;
            art.id = eid;
            art.source_name = spec.label;
            art.category = "english";
            art.title = title;
            art.subtitle = spec.label + " · " + date +
                           (fm.count("level") && !fm["level"].empty() ? (" · " + fm["level"]) : "");
            art.authors = fi.author;
            art.published_at = date;
            art.url = fi.url;
            art.tags = fi.tags;
            int pidx = 1;
            for (const auto& blk : full_md_blocks) {
                art.paragraphs.push_back({"p-" + std::to_string(pidx++), blk.kind, blk.original, blk.translated});
            }
            out.articles[eid] = std::move(art);
            out.english_notes_hydrated++;
            out.date_stats[date].english++;
        }
    }

    fs::path hist_path = fs::path(obsidian_vault_dir) / ".recommendation" / "history" / "english.jsonl";
    if (auto txt = readFileText(hist_path.string())) {
        for (const auto& line : splitLines(*txt)) {
            std::string t = trim(line);
            if (t.empty()) continue;
            try {
                auto j = nlohmann::json::parse(t);
                std::string d = j.value("date", "");
                std::string url = j.value("source_url", "");
                std::string title = j.value("title", "");
                if (!d.empty() && !url.empty()) recordProvenance(out.url_to_date, url, d);
                if (!d.empty() && !title.empty()) recordProvenance(out.norm_title_to_date, normalizeForDedup(title), d);
            } catch (...) {
            }
        }
    }
}

std::vector<ManifestFileEntry> collectManifestFiles(const std::string& obsidian_vault_dir,
                                                    const std::string& music_daily_dir) {
    std::vector<ManifestFileEntry> out;
    std::error_code ec;
    auto scan_dir = [&](const fs::path& base, const fs::path& root_label_base, bool recursive, const std::string& ext) {
        if (!fs::exists(base, ec)) return;
        if (recursive) {
            for (const auto& e : fs::recursive_directory_iterator(base, ec)) {
                if (!e.is_regular_file() || e.path().extension() != ext) continue;
                out.push_back({fs::relative(e.path(), root_label_base, ec).string(),
                               e.file_size(ec), fileMtimeNs(e)});
            }
        } else {
            for (const auto& e : fs::directory_iterator(base, ec)) {
                if (!e.is_regular_file() || e.path().extension() != ext) continue;
                out.push_back({fs::relative(e.path(), root_label_base, ec).string(),
                               e.file_size(ec), fileMtimeNs(e)});
            }
        }
    };

    scan_dir(fs::path(music_daily_dir) / "recommendations", music_daily_dir, true, ".json");
    const char* subs[] = {"Music/Daily", "Music/Daily/manifests", "Resources/YouTube/Daily",
                          "News/Daily", "Resources/Reddit/Daily", "00-inbox/Journal",
                          "50-courses/English/Daily", "50-courses/English/Translation",
                          "50-courses/English/Vocabulary", "50-courses/English/Weekly",
                          "50-courses/English/Weekly/TV"};
    for (const char* sub : subs) {
        std::string ext = (std::string(sub) == "Music/Daily/manifests") ? ".json" : ".md";
        scan_dir(fs::path(obsidian_vault_dir) / sub, obsidian_vault_dir, false, ext);
    }
    const char* hists[] = {"youtube.jsonl", "reddit.jsonl", "journal.jsonl", "english.jsonl"};
    for (const char* h : hists) {
        fs::path hp = fs::path(obsidian_vault_dir) / ".recommendation" / "history" / h;
        if (fs::exists(hp, ec)) {
            fs::directory_entry de(hp, ec);
            out.push_back({std::string(".recommendation/history/") + h, de.file_size(ec), fileMtimeNs(de)});
        }
    }
    std::sort(out.begin(), out.end(), [](const auto& a, const auto& b) {
        return a.rel_path < b.rel_path;
    });
    return out;
}

nlohmann::json serializeArchive(const HydratedArchive& a) {
    nlohmann::json eng_days = nlohmann::json::array();
    for (const auto& [date, bundle] : a.english_days) {
        nlohmann::json notes_arr = nlohmann::json::array();
        for (const auto& n : bundle.notes) {
            notes_arr.push_back({
                {"component", n.component},
                {"date", n.date},
                {"title", n.title},
                {"rel_path", n.rel_path},
                {"blocks", omnidesk::toJsonArray(n.blocks)},
                {"vocab_cards", omnidesk::toJsonArray(n.vocab_cards)},
            });
        }
        eng_days.push_back({{"date", date}, {"notes", std::move(notes_arr)}});
    }

    nlohmann::json stats_obj = nlohmann::json::object();
    for (const auto& [date, st] : a.date_stats) {
        stats_obj[date] = {
            {"music", st.music},
            {"youtube", st.youtube},
            {"news", st.news},
            {"reddit", st.reddit},
            {"journal", st.journal},
            {"english", st.english},
        };
    }

    nlohmann::json arts = nlohmann::json::object();
    for (const auto& [id, art] : a.articles) {
        arts[id] = omnidesk::toJson(art);
    }

    return {
        {"schema_version", 3},
        {"media_items", omnidesk::toJsonArray(a.media_items)},
        {"text_items", omnidesk::toJsonArray(a.text_items)},
        {"articles", std::move(arts)},
        {"vocab_cards", omnidesk::toJsonArray(a.vocab_cards)},
        {"english_days", std::move(eng_days)},
        {"date_stats", std::move(stats_obj)},
        {"song_id_to_date", a.song_id_to_date},
        {"canonical_id_to_date", a.canonical_id_to_date},
        {"video_id_to_date", a.video_id_to_date},
        {"post_id_to_date", a.post_id_to_date},
        {"doi_to_date", a.doi_to_date},
        {"url_to_date", a.url_to_date},
        {"norm_title_to_date", a.norm_title_to_date},
        {"counts", {
            {"dates_indexed", a.dates_indexed},
            {"music_tracks_hydrated", a.music_tracks_hydrated},
            {"youtube_videos_hydrated", a.youtube_videos_hydrated},
            {"news_days_hydrated", a.news_days_hydrated},
            {"news_stories_hydrated", a.news_stories_hydrated},
            {"reddit_days_hydrated", a.reddit_days_hydrated},
            {"reddit_posts_hydrated", a.reddit_posts_hydrated},
            {"journal_days_hydrated", a.journal_days_hydrated},
            {"journal_papers_hydrated", a.journal_papers_hydrated},
            {"english_notes_hydrated", a.english_notes_hydrated},
            {"vocab_cards_hydrated", a.vocab_cards_hydrated},
        }},
    };
}

bool deserializeArchive(const nlohmann::json& j, HydratedArchive& out) {
    if (!j.is_object() || j.value("schema_version", 0) != 3) return false;
    out = HydratedArchive{};
    if (j.contains("media_items")) out.media_items = omnidesk::fromJsonArray<omnidesk::MediaItem>(j["media_items"]);
    if (j.contains("text_items")) out.text_items = omnidesk::fromJsonArray<omnidesk::TextFeedItem>(j["text_items"]);
    if (j.contains("vocab_cards")) out.vocab_cards = omnidesk::fromJsonArray<omnidesk::VocabCard>(j["vocab_cards"]);
    if (j.contains("articles") && j["articles"].is_object()) {
        for (auto it = j["articles"].begin(); it != j["articles"].end(); ++it) {
            omnidesk::TextArticle art;
            omnidesk::fromJson(it.value(), art);
            out.articles[it.key()] = std::move(art);
        }
    }
    if (j.contains("english_days") && j["english_days"].is_array()) {
        for (const auto& ed : j["english_days"]) {
            if (!ed.is_object()) continue;
            EnglishDayBundle b;
            b.date = ed.value("date", "");
            if (ed.contains("notes") && ed["notes"].is_array()) {
                for (const auto& nj : ed["notes"]) {
                    EnglishNoteEntry n;
                    n.component = nj.value("component", "");
                    n.date = nj.value("date", "");
                    n.title = nj.value("title", "");
                    n.rel_path = nj.value("rel_path", "");
                    n.raw_markdown = nj.value("raw_markdown", "");
                    if (nj.contains("blocks")) n.blocks = omnidesk::fromJsonArray<omnidesk::BookPageBlock>(nj["blocks"]);
                    if (nj.contains("vocab_cards")) n.vocab_cards = omnidesk::fromJsonArray<omnidesk::VocabCard>(nj["vocab_cards"]);
                    b.notes.push_back(std::move(n));
                }
            }
            if (!b.date.empty()) out.english_days[b.date] = std::move(b);
        }
    }
    if (j.contains("date_stats") && j["date_stats"].is_object()) {
        for (auto it = j["date_stats"].begin(); it != j["date_stats"].end(); ++it) {
            HydratedDateStats st;
            st.music = it.value().value("music", 0);
            st.youtube = it.value().value("youtube", 0);
            st.news = it.value().value("news", 0);
            st.reddit = it.value().value("reddit", 0);
            st.journal = it.value().value("journal", 0);
            st.english = it.value().value("english", 0);
            out.date_stats[it.key()] = st;
        }
    }
    auto load_map = [&](const char* k, std::unordered_map<std::string, std::string>& dst) {
        if (j.contains(k) && j[k].is_object()) {
            for (auto it = j[k].begin(); it != j[k].end(); ++it) {
                if (it.value().is_string()) dst[it.key()] = it.value().get<std::string>();
            }
        }
    };
    load_map("song_id_to_date", out.song_id_to_date);
    load_map("canonical_id_to_date", out.canonical_id_to_date);
    load_map("video_id_to_date", out.video_id_to_date);
    load_map("post_id_to_date", out.post_id_to_date);
    load_map("doi_to_date", out.doi_to_date);
    load_map("url_to_date", out.url_to_date);
    load_map("norm_title_to_date", out.norm_title_to_date);

    if (j.contains("counts") && j["counts"].is_object()) {
        const auto& c = j["counts"];
        out.dates_indexed = c.value("dates_indexed", static_cast<int>(out.date_stats.size()));
        out.music_tracks_hydrated = c.value("music_tracks_hydrated", 0);
        out.youtube_videos_hydrated = c.value("youtube_videos_hydrated", 0);
        out.news_days_hydrated = c.value("news_days_hydrated", 0);
        out.news_stories_hydrated = c.value("news_stories_hydrated", 0);
        out.reddit_days_hydrated = c.value("reddit_days_hydrated", 0);
        out.reddit_posts_hydrated = c.value("reddit_posts_hydrated", 0);
        out.journal_days_hydrated = c.value("journal_days_hydrated", 0);
        out.journal_papers_hydrated = c.value("journal_papers_hydrated", 0);
        out.english_notes_hydrated = c.value("english_notes_hydrated", 0);
        out.vocab_cards_hydrated = c.value("vocab_cards_hydrated", 0);
    }
    return true;
}

} // namespace

void HydratedArchive::rebuildIndices() {
    dates_indexed = static_cast<int>(date_stats.size());

    for (const auto& m : media_items) {
        std::string d = m.extra.value("business_date", "");
        if (d.empty()) continue;
        std::string sid = m.extra.value("apple_music_song_id", "");
        std::string cid = m.extra.value("canonical_id", "");
        std::string vid = m.extra.value("video_id", "");
        recordProvenance(song_id_to_date, sid, d);
        recordProvenance(canonical_id_to_date, cid, d);
        recordProvenance(video_id_to_date, vid, d);
        recordProvenance(url_to_date, m.play_url, d);
        recordProvenance(norm_title_to_date, normalizeForDedup(m.title), d);
    }
    for (const auto& t : text_items) {
        std::string d = t.published_at;
        if (!isValidIsoDate(d)) continue;
        recordProvenance(url_to_date, t.url, d);
        recordProvenance(norm_title_to_date, normalizeForDedup(t.title), d);
        if (t.category == "reddit" && !t.url.empty()) {
            recordProvenance(post_id_to_date, extractRedditPostId(t.url), d);
        }
        auto ait = articles.find(t.id);
        if (ait != articles.end() && !ait->second.doi.empty()) {
            recordProvenance(doi_to_date, toLowerAscii(ait->second.doi), d);
        }
    }
}

std::vector<omnidesk::VocabCard> ArchiveRehydrator::parseVocabularyMarkdown(const std::string& md,
                                                                            const std::string& date) {
    std::vector<omnidesk::VocabCard> cards;
    std::unordered_map<std::string, std::string> fm;
    std::string body;
    parseFrontmatter(md, fm, body);
    body = stripAutoComments(body);
    const auto lines = splitLines(body);

    omnidesk::VocabCard cur;
    bool in_card = false;
    std::string sub;
    std::string extra_notes;

    auto flush_card = [&]() {
        if (!in_card || trim(cur.word).empty()) {
            cur = omnidesk::VocabCard{};
            in_card = false;
            sub.clear();
            extra_notes.clear();
            return;
        }
        cur.word = trim(cur.word);
        if (cur.lemma.empty()) cur.lemma = toLowerAscii(cur.word);
        cur.meaning = trim(cur.meaning);
        cur.etymology = trim(cur.etymology);
        cur.context_sentence = trim(cur.context_sentence);
        if (!extra_notes.empty()) {
            if (!cur.etymology.empty()) cur.etymology += "\n";
            cur.etymology += trim(extra_notes);
        }
        cur.source_book = "English Vocabulary · " + date;
        cur.interval_days = 1;
        cur.repetitions = 0;
        cur.ease_factor = 2.5;
        cards.push_back(std::move(cur));
        cur = omnidesk::VocabCard{};
        in_card = false;
        sub.clear();
        extra_notes.clear();
    };

    for (const auto& line : lines) {
        std::string t = trim(line);
        std::string wtitle;
        int rank = parseLeadingNumberedHeading(t, wtitle);
        if (rank > 0) {
            flush_card();
            in_card = true;
            size_t s1 = wtitle.find('/');
            if (s1 != std::string::npos) {
                size_t s2 = wtitle.find('/', s1 + 1);
                if (s2 != std::string::npos && s2 > s1 + 1) {
                    cur.phonetic = wtitle.substr(s1, s2 - s1 + 1);
                    cur.word = trim(wtitle.substr(0, s1));
                } else {
                    cur.word = wtitle;
                }
            } else {
                cur.word = wtitle;
            }
            continue;
        }
        if (startsWith(t, "## ")) {
            flush_card();
            continue;
        }
        if (!in_card || t.empty() || t == "---") continue;

        if (startsWith(t, "### ")) {
            std::string sh = toLowerAscii(trim(t.substr(4)));
            if (sh.find("origin") != std::string::npos || sh.find("memory") != std::string::npos) sub = "origin";
            else if (sh.find("family") != std::string::npos) sub = "family";
            else if (sh.find("collocation") != std::string::npos || sh.find("use") != std::string::npos) sub = "collocations";
            else if (sh.find("example") != std::string::npos) sub = "example";
            else sub = "other";
            continue;
        }

        std::string val;
        if (matchBoldField(t, {"含义", "Meaning"}, val)) {
            cur.meaning = val;
            continue;
        }
        if (matchBoldField(t, {"常见搭配", "Collocations"}, val)) {
            if (!extra_notes.empty()) extra_notes += "\n";
            extra_notes += "搭配: " + val;
            continue;
        }
        if (matchBoldField(t, {"例句", "Example"}, val)) {
            cur.context_sentence = val;
            continue;
        }
        if (matchBoldField(t, {"词族", "词形"}, val)) {
            if (!cur.etymology.empty()) cur.etymology += "\n";
            cur.etymology += "词族: " + val;
            continue;
        }
        if (matchBoldField(t, {"对比"}, val)) {
            if (!extra_notes.empty()) extra_notes += "\n";
            extra_notes += "对比: " + val;
            continue;
        }

        if (sub.empty() && startsWith(t, "**") && endsWith(t, "**") && t.size() > 4 && cur.meaning.empty()) {
            cur.meaning = trim(t.substr(2, t.size() - 4));
            continue;
        }

        if (sub == "origin") {
            if (!cur.etymology.empty()) cur.etymology += "\n";
            cur.etymology += t;
        } else if (sub == "family" || sub == "collocations") {
            if (!extra_notes.empty()) extra_notes += "\n";
            extra_notes += t;
        } else if (sub == "example") {
            std::string ex = startsWith(t, ">") ? trim(t.substr(1)) : t;
            if (!cur.context_sentence.empty()) cur.context_sentence += " ";
            cur.context_sentence += ex;
        }
    }
    flush_card();
    return cards;
}

bool ArchiveRehydrator::loadCachedIndex(const std::string& curation_dir, HydratedArchive& out) {
    const fs::path idx_path = fs::path(curation_dir) / "hydrated_index.json";
    auto jopt = readJsonFile(idx_path.string());
    if (!jopt.has_value()) return false;
    return deserializeArchive(*jopt, out);
}

bool ArchiveRehydrator::saveCachedIndex(const std::string& curation_dir, const HydratedArchive& archive) {
    const fs::path idx_path = fs::path(curation_dir) / "hydrated_index.json";
    return writeJsonFileAtomic(idx_path.string(), serializeArchive(archive), -1);
}

bool ArchiveRehydrator::rehydrate(const std::string& obsidian_vault_dir,
                                  const std::string& music_daily_dir,
                                  const std::string& curation_dir,
                                  bool force_full,
                                  HydratedArchive& out,
                                  bool& out_cache_hit,
                                  std::string& out_error) {
    out_cache_hit = false;
    out_error.clear();

    const fs::path manifest_path = fs::path(curation_dir) / "rehydrate_manifest.json";
    const fs::path index_path = fs::path(curation_dir) / "hydrated_index.json";
    const auto current_files = collectManifestFiles(obsidian_vault_dir, music_daily_dir);

    if (!force_full && fs::exists(manifest_path) && fs::exists(index_path)) {
        auto mopt = readJsonFile(manifest_path.string());
        if (mopt.has_value() && mopt->is_object() &&
            mopt->value("schema_version", 0) == 2 &&
            mopt->value("obsidian_vault_dir", "") == obsidian_vault_dir &&
            mopt->value("music_daily_dir", "") == music_daily_dir &&
            mopt->contains("files") && (*mopt)["files"].is_array() &&
            (*mopt)["files"].size() == current_files.size()) {
            const auto& arr = (*mopt)["files"];
            bool exact_match = true;
            for (size_t i = 0; i < current_files.size(); ++i) {
                if (!arr[i].is_object() ||
                    arr[i].value("rel_path", "") != current_files[i].rel_path ||
                    arr[i].value("size", static_cast<uintmax_t>(0)) != current_files[i].size ||
                    arr[i].value("mtime_ns", int64_t{0}) != current_files[i].mtime_ns) {
                    exact_match = false;
                    break;
                }
            }
            if (exact_match &&
                (!out.date_stats.empty() || loadCachedIndex(curation_dir, out))) {
                out_cache_hit = true;
                return true;
            }
        }
    }

    HydratedArchive fresh;
    parseMusicDailyStream(obsidian_vault_dir, music_daily_dir, fresh);
    parseYouTubeDailyStream(obsidian_vault_dir, fresh);
    parseNewsDailyStream(obsidian_vault_dir, fresh);
    parseRedditDailyStream(obsidian_vault_dir, fresh);
    parseJournalDailyStream(obsidian_vault_dir, fresh);
    parseEnglishStream(obsidian_vault_dir, fresh);

    std::sort(fresh.media_items.begin(), fresh.media_items.end(), [](const auto& a, const auto& b) {
        std::string da = a.extra.value("business_date", "");
        std::string db = b.extra.value("business_date", "");
        if (da != db) return da > db;
        return a.extra.value("priority_rank", 999) < b.extra.value("priority_rank", 999);
    });
    std::sort(fresh.text_items.begin(), fresh.text_items.end(), [](const auto& a, const auto& b) {
        if (a.published_at != b.published_at) return a.published_at > b.published_at;
        return a.id < b.id;
    });

    fresh.rebuildIndices();
    out = std::move(fresh);

    if (!curation_dir.empty()) {
        nlohmann::json files_json = nlohmann::json::array();
        for (const auto& f : current_files) {
            files_json.push_back({
                {"rel_path", f.rel_path},
                {"size", f.size},
                {"mtime_ns", f.mtime_ns},
            });
        }
        nlohmann::json manifest = {
            {"schema_version", 2},
            {"obsidian_vault_dir", obsidian_vault_dir},
            {"music_daily_dir", music_daily_dir},
            {"files", std::move(files_json)},
        };
        std::string err;
        if (!saveCachedIndex(curation_dir, out)) {
            out_error = "Failed to save hydrated_index.json";
        }
        if (!writeJsonFileAtomic(manifest_path.string(), manifest, -1, &err)) {
            out_error = err;
        }
    }
    return true;
}

} // namespace ai_curator
