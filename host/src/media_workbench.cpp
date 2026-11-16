#include "omnidesk/host/media_workbench.hpp"
#include "omnidesk/host/async_load.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/recycling_grid.hpp"
#include "omnidesk/host/ui_comfort.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <mutex>
#include <sstream>
#include <unordered_map>
#include <utility>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/comment_tree_view.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/mpv_core.hpp"
#include "omnidesk/playback_coordinator.hpp"
#include "omnidesk/selection_action_popover.hpp"
#include "omnidesk/types_json.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

std::string toLowerCopy(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

bool isIsoDatePrefix(const std::string& s) {
    if (s.size() != 10 || s[4] != '-' || s[7] != '-') return false;
    for (size_t i = 0; i < s.size(); ++i) {
        if (i == 4 || i == 7) continue;
        if (!std::isdigit(static_cast<unsigned char>(s[i]))) return false;
    }
    return true;
}

std::string trimSpaces(std::string s) {
    while (!s.empty() && (s.front() == ' ' || s.front() == '\t')) s.erase(s.begin());
    while (!s.empty() && (s.back() == ' ' || s.back() == '\t')) s.pop_back();
    return s;
}

bool isCuratedMedia(const MediaItem& item) {
    return item.source_id == "plugin-ai-curator" ||
        (item.extra.is_object() && !item.extra.value("curation_mode", "").empty());
}

bool mediaItemMatchesQuery(const MediaItem& item, const std::string& query) {
    if (query.empty()) return true;
    if (query.rfind("date:", 0) == 0) {
        const std::string target_date = query.substr(5);
        if (target_date.empty()) return true;
        if (item.extra.is_object()) {
            if (item.extra.value("business_date", "") == target_date) return true;
            if (item.extra.value("previously_recommended_on", "") == target_date) return true;
        }
        return item.source_badge.find(target_date) != std::string::npos ||
               item.title.find(target_date) != std::string::npos;
    }
    const std::string q_low = toLowerCopy(query);
    if (toLowerCopy(item.title).find(q_low) != std::string::npos ||
        toLowerCopy(item.creator).find(q_low) != std::string::npos ||
        toLowerCopy(item.source_badge).find(q_low) != std::string::npos) {
        return true;
    }
    if (item.extra.is_object()) {
        if (toLowerCopy(item.extra.value("series", "")).find(q_low) != std::string::npos) return true;
        if (toLowerCopy(item.extra.value("season_code", "")).find(q_low) != std::string::npos) return true;
        if (item.extra.contains("episodes") && item.extra["episodes"].is_array()) {
            for (const auto& ep : item.extra["episodes"]) {
                if (!ep.is_object()) continue;
                if (toLowerCopy(ep.value("title", "")).find(q_low) != std::string::npos ||
                    toLowerCopy(ep.value("episode_code", "")).find(q_low) != std::string::npos ||
                    toLowerCopy(ep.value("episode_title", "")).find(q_low) != std::string::npos) {
                    return true;
                }
            }
        }
    }
    return false;
}

void appendCuratedMetadataBlock(brls::Box* parent, const MediaItem& item) {
    if (!parent || !item.extra.is_object()) return;
    const auto& ex = item.extra;
    const std::string prev_date = ex.value("previously_recommended_on", "");
    const std::string cur_mode = ex.value("curation_mode", "");
    const std::string biz_date = ex.value("business_date", "");
    const std::string why = ex.value("why_recommended", "");
    const std::string cue = ex.value("listening_or_reading_cue", "");

    if (prev_date.empty() && cur_mode.empty() && why.empty() && cue.empty()) {
        return;
    }

    auto* meta_card = ui::makeCard("desktop/soft", 12.0f, 8.0f);
    meta_card->setMarginTop(8);

    auto* badge_row = new brls::Box(brls::Axis::ROW);
    if (cur_mode == "daily_immutable" && !biz_date.empty()) {
        auto* p = ui::makePill("每日精选 · " + biz_date, "status/ok_bg", "status/ok_fg");
        p->setMarginRight(6);
        badge_row->addView(p);
    } else if (cur_mode == "interactive_ephemeral") {
        auto* p = ui::makePill("AI 灵感推荐 · 可重温", "status/info_bg", "domain/core");
        p->setMarginRight(6);
        badge_row->addView(p);
    }
    if (!prev_date.empty()) {
        badge_row->addView(ui::makePill("经典重温 · " + prev_date, "status/info_bg", "status/info_fg"));
    }
    if (!badge_row->getChildren().empty()) {
        meta_card->addView(badge_row);
    }
    if (!why.empty()) {
        auto* w = ui::makeLabel("推荐理由: " + why, 14.5f, "brls/text", 1.45f);
        w->setMarginTop(6);
        meta_card->addView(w);
    }
    if (!cue.empty()) {
        auto* c = ui::makeLabel("聆听/观看线索: " + cue, 14.0f, "font/grey", 1.42f);
        c->setMarginTop(4);
        meta_card->addView(c);
    }
    parent->addView(meta_card);
}

std::string fileExtLower(const std::string& path) {
    return toLowerCopy(std::filesystem::path(path).extension().string());
}

bool isBookFileExt(const std::string& ext) {
    return ext == ".pdf" || ext == ".epub" || ext == ".mobi" || ext == ".azw3" || ext == ".txt";
}

bool isMediaFileExt(const std::string& ext) {
    return ext == ".mp4" || ext == ".mkv" || ext == ".mov" || ext == ".avi" || ext == ".webm" ||
           ext == ".ts" || ext == ".m4v" || ext == ".flac" || ext == ".mp3" || ext == ".aac" ||
           ext == ".wav" || ext == ".ogg" || ext == ".opus" || ext == ".alac" || ext == ".m4a";
}

std::string formatBytesHuman(uint64_t bytes) {
    if (bytes < 1024) return std::to_string(bytes) + " B";
    double val = static_cast<double>(bytes) / 1024.0;
    const char* unit = "KB";
    if (val >= 1024.0) {
        val /= 1024.0;
        unit = "MB";
    }
    if (val >= 1024.0) {
        val /= 1024.0;
        unit = "GB";
    }
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.1f %s", val, unit);
    return std::string(buf);
}

std::string resolveLocalMediaPath(const MediaItem& item) {
    auto pickString = [](const nlohmann::json& obj, const char* key) -> std::string {
        if (obj.is_object() && obj.contains(key) && obj[key].is_string()) {
            return obj[key].get<std::string>();
        }
        return "";
    };
    for (const char* key : {"abs_path", "local_path"}) {
        std::string p = pickString(item.extra, key);
        if (!p.empty()) {
            return p;
        }
    }
    std::string url = item.play_url;
    constexpr const char kFileScheme[] = "file://";
    if (url.rfind(kFileScheme, 0) == 0) {
        return url.substr(sizeof(kFileScheme) - 1);
    }
    for (const char* remote : {"http://", "https://", "rtsp://", "rtmp://", "smb://"}) {
        if (url.rfind(remote, 0) == 0) {
            return "";
        }
    }
    return url;
}

void appendCuesAsLyrics(const nlohmann::json& cues, std::vector<LyricLine>& out) {
    if (!cues.is_array()) {
        return;
    }
    for (const auto& cue : cues) {
        if (!cue.is_object()) {
            continue;
        }
        const double start_sec = cue.value("start_sec", 0.0);
        std::string text = cue.value("text", std::string{});
        if (!text.empty()) {
            out.push_back(LyricLine{start_sec, std::move(text), ""});
        }
    }
}

std::string formatDuration(double sec) {
    if (std::isnan(sec) || std::isinf(sec) || sec <= 0.0) {
        return "00:00";
    }
    int total = static_cast<int>(std::min(sec, 35999999.0));
    int m = total / 60;
    int s = total % 60;
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", m, s);
    return std::string(buf);
}

std::unordered_map<std::string, std::string> extractHeadersFromItem(const MediaItem& item) {
    std::unordered_map<std::string, std::string> headers;
    if (!item.extra.is_object()) {
        return headers;
    }
    if (item.extra.contains("http_headers") && item.extra["http_headers"].is_object()) {
        for (const auto& kv : item.extra["http_headers"].items()) {
            if (kv.value().is_string()) {
                headers[kv.key()] = kv.value().get<std::string>();
            }
        }
    }
    if (item.extra.contains("referrer") && item.extra["referrer"].is_string()) {
        headers.emplace("Referer", item.extra["referrer"].get<std::string>());
    }
    if (item.extra.contains("user_agent") && item.extra["user_agent"].is_string()) {
        headers.emplace("User-Agent", item.extra["user_agent"].get<std::string>());
    }
    return headers;
}

MediaRequestOptions buildPlaybackOptions(const MediaItem& item) {
    MediaRequestOptions opts;
    opts.url = item.play_url;
    opts.http_headers = extractHeadersFromItem(item);
    opts.start_seconds = item.progress_sec > 0.0 ? item.progress_sec : 0.0;
    if (item.extra.is_object()) {
        if (item.extra.contains("audio_url") && item.extra["audio_url"].is_string()) {
            opts.audio_url = item.extra["audio_url"].get<std::string>();
        }
        if (item.extra.contains("referrer") && item.extra["referrer"].is_string()) {
            opts.referrer = item.extra["referrer"].get<std::string>();
        }
        if (item.extra.contains("user_agent") && item.extra["user_agent"].is_string()) {
            opts.user_agent = item.extra["user_agent"].get<std::string>();
        }
        auto pushSubUnique = [&](const std::string& sp) {
            if (sp.empty()) return;
            if (std::find(opts.subtitle_urls.begin(), opts.subtitle_urls.end(), sp) ==
                opts.subtitle_urls.end()) {
                opts.subtitle_urls.push_back(sp);
            }
        };
        if (item.extra.contains("subtitle_urls") && item.extra["subtitle_urls"].is_array()) {
            for (const auto& s : item.extra["subtitle_urls"]) {
                if (s.is_string()) pushSubUnique(s.get<std::string>());
            }
        }
        if (item.extra.contains("primary_subtitle_path") && item.extra["primary_subtitle_path"].is_string()) {
            pushSubUnique(item.extra["primary_subtitle_path"].get<std::string>());
        }
        if (item.extra.contains("subtitle_paths") && item.extra["subtitle_paths"].is_array()) {
            for (const auto& s : item.extra["subtitle_paths"]) {
                if (s.is_string()) pushSubUnique(s.get<std::string>());
            }
        }
    }
    return opts;
}

std::vector<MediaItem> extractSeasonEpisodes(const MediaItem& parent_or_active) {
    std::vector<MediaItem> out;
    if (!parent_or_active.extra.is_object()) return out;

    const nlohmann::json* ep_arr = nullptr;
    if (parent_or_active.extra.contains("episodes") &&
        parent_or_active.extra["episodes"].is_array() &&
        !parent_or_active.extra["episodes"].empty()) {
        ep_arr = &parent_or_active.extra["episodes"];
    } else if (parent_or_active.extra.contains("season_episodes") &&
               parent_or_active.extra["season_episodes"].is_array() &&
               !parent_or_active.extra["season_episodes"].empty()) {
        ep_arr = &parent_or_active.extra["season_episodes"];
    }
    if (!ep_arr) return out;

    const std::string group_title = parent_or_active.extra.value(
        "season_group_title", parent_or_active.title);
    const std::string series_name = parent_or_active.extra.value(
        "series", parent_or_active.creator);

    out.reserve(ep_arr->size());
    for (const auto& e : *ep_arr) {
        if (!e.is_object()) continue;
        MediaItem ep;
        ep.id = e.value("id", "");
        ep.source_id = parent_or_active.source_id.empty() ? "plugin-fnos-nas" : parent_or_active.source_id;
        ep.title = e.value("title", "");
        ep.creator = series_name.empty() ? parent_or_active.creator : series_name;
        ep.kind = parent_or_active.kind.empty() ? "nas" : parent_or_active.kind;
        ep.source_badge = parent_or_active.source_badge.empty() ? "飞牛 NAS" : parent_or_active.source_badge;
        ep.cover_url = e.value("cover_url", "");
        if (ep.cover_url.empty()) ep.cover_url = parent_or_active.cover_url;
        ep.play_url = e.value("play_url", "");
        ep.quality_label = e.value("quality_label", "");
        ep.duration_sec = e.value("duration_sec", 0.0);
        ep.has_bilingual_sub = e.value("has_bilingual_sub", false);
        ep.extra = {
            {"abs_path", e.value("abs_path", "")},
            {"series", series_name},
            {"season", e.value("season", -1)},
            {"episode", e.value("episode", -1)},
            {"episode_code", e.value("episode_code", "")},
            {"episode_title", e.value("episode_title", "")},
            {"primary_subtitle_path", e.value("primary_subtitle_path", "")},
            {"subtitle_paths", e.value("subtitle_paths", nlohmann::json::array())},
            {"season_group_title", group_title},
            {"season_episodes", *ep_arr},
        };
        if (!ep.id.empty() || !ep.play_url.empty()) {
            out.push_back(std::move(ep));
        }
    }
    return out;
}

bool isDigitsOnlyStr(const std::string& s) {
    if (s.empty()) return false;
    return std::all_of(s.begin(), s.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
}

struct AppleMusicLookup {
    bool is_apple_music = false;
    std::string song_id;
    std::string storefront;
    std::string url;
};

AppleMusicLookup extractAppleMusicLookup(const MediaItem& item) {
    AppleMusicLookup out;
    if (item.id.rfind("demo-", 0) == 0) return out;
    out.url = item.play_url;

    std::string extra_song_id;
    if (item.extra.is_object()) {
        for (const char* k : {"apple_music_song_id", "asset_id", "song_id"}) {
            if (item.extra.contains(k) && item.extra[k].is_string()) {
                extra_song_id = item.extra[k].get<std::string>();
                if (!extra_song_id.empty()) break;
            }
        }
        if (item.extra.contains("storefront") && item.extra["storefront"].is_string()) {
            out.storefront = toLowerCopy(item.extra["storefront"].get<std::string>());
        }
    }

    std::string pref_song_id;
    std::string pref_sf;
    if (item.id.rfind("am-", 0) == 0 && item.id.size() > 6 && item.id[5] == '-' &&
        std::isalpha(static_cast<unsigned char>(item.id[3])) &&
        std::isalpha(static_cast<unsigned char>(item.id[4]))) {
        pref_sf = toLowerCopy(item.id.substr(3, 2));
        std::string rest = item.id.substr(6);
        if (isDigitsOnlyStr(rest)) pref_song_id = rest;
        else {
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && isDigitsOnlyStr(rest.substr(dash + 1))) {
                pref_song_id = rest.substr(dash + 1);
            }
        }
    } else if (item.id.rfind("ix-am-", 0) == 0) {
        std::string rest = item.id.substr(6);
        if (isDigitsOnlyStr(rest)) pref_song_id = rest;
        else {
            size_t dash = rest.rfind('-');
            if (dash != std::string::npos && isDigitsOnlyStr(rest.substr(dash + 1))) {
                pref_song_id = rest.substr(dash + 1);
            }
        }
    }

    std::string url_song_id;
    std::string url_sf;
    size_t host_pos = item.play_url.find("music.apple.com/");
    if (host_pos != std::string::npos) {
        size_t p = host_pos + std::char_traits<char>::length("music.apple.com/");
        size_t slash = item.play_url.find('/', p);
        if (slash != std::string::npos && slash - p == 2 &&
            std::isalpha(static_cast<unsigned char>(item.play_url[p])) &&
            std::isalpha(static_cast<unsigned char>(item.play_url[p + 1]))) {
            url_sf = toLowerCopy(item.play_url.substr(p, 2));
        }
        size_t q_pos = item.play_url.find('?');
        if (q_pos != std::string::npos) {
            std::string query = item.play_url.substr(q_pos + 1);
            size_t hash = query.find('#');
            if (hash != std::string::npos) query.resize(hash);
            size_t cur = 0;
            while (cur < query.size()) {
                size_t amp = query.find('&', cur);
                std::string kv = (amp == std::string::npos) ? query.substr(cur) : query.substr(cur, amp - cur);
                if (kv.rfind("i=", 0) == 0 && isDigitsOnlyStr(kv.substr(2))) {
                    url_song_id = kv.substr(2);
                    break;
                }
                if (amp == std::string::npos) break;
                cur = amp + 1;
            }
        }
        if (url_song_id.empty()) {
            for (const char* marker : {"/song/", "/music-video/"}) {
                size_t pos = item.play_url.find(marker);
                if (pos != std::string::npos) {
                    size_t sp = pos + std::char_traits<char>::length(marker);
                    size_t q = item.play_url.find_first_of("?#", sp);
                    std::string tail = (q == std::string::npos) ? item.play_url.substr(sp) : item.play_url.substr(sp, q - sp);
                    while (!tail.empty() && tail.back() == '/') tail.pop_back();
                    size_t ls = tail.rfind('/');
                    std::string cand = (ls == std::string::npos) ? tail : tail.substr(ls + 1);
                    if (isDigitsOnlyStr(cand)) {
                        url_song_id = cand;
                        break;
                    }
                }
            }
        }
    }

    if (isDigitsOnlyStr(extra_song_id)) {
        out.song_id = extra_song_id;
    } else if (!pref_song_id.empty()) {
        out.song_id = pref_song_id;
    } else if (!url_song_id.empty()) {
        out.song_id = url_song_id;
    } else if (item.source_id == "plugin-music" && !item.id.empty()) {
        out.song_id = item.id;
    } else if (item.source_id == "plugin-ai-curator" && item.kind == "music" && isDigitsOnlyStr(item.id)) {
        out.song_id = item.id;
    }

    if (!out.song_id.empty()) {
        out.is_apple_music = true;
        if (out.storefront.empty()) out.storefront = url_sf;
        if (out.storefront.empty()) out.storefront = pref_sf;
        if (out.storefront.empty() && item.source_id == "plugin-ai-curator") out.storefront = "cn";
    }
    return out;
}

bool isPlayableOrResolvableItem(const MediaItem& item) {
    if (item.id.rfind("demo-", 0) == 0 || item.id.rfind("cache:", 0) == 0) return false;
    if (!item.play_url.empty()) return true;
    if (extractAppleMusicLookup(item).is_apple_music) return true;
    if (item.source_id == "bilibili" || item.source_id == "youtube" ||
        item.source_id == "plugin-bilibili" || item.source_id == "plugin-youtube") {
        return !item.id.empty();
    }
    if (item.extra.is_object()) {
        for (const char* k : {"video_id", "youtube_id", "bvid", "abs_path", "local_path"}) {
            if (item.extra.contains(k) && item.extra[k].is_string() &&
                !item.extra[k].get_ref<const std::string&>().empty()) {
                return true;
            }
        }
    }
    return false;
}

std::vector<MediaItem> buildShowcaseMedia() {
    return {
        {"demo-bili-1", "plugin-bilibili", "从 wiliwili 到 OmniDesk：微内核桌面工作台设计实践", "TechTalk Lab",
         "video", "Bilibili · 预览", "", "", "4K 60fps HEVC", 1425, 310, 184200, true},
        {"demo-yt-1", "plugin-youtube", "Systems Architecture of Decoupled C++ Desktop Workbenches", "Kernel Notes",
         "video", "YouTube · 预览", "", "", "2160p VP9", 2180, 0, 92400, true},
        {"demo-nas-1", "plugin-fnos-nas", "飞牛 fnOS 私有影库 · 纪录片原盘直连串流", "Home NAS",
         "nas", "飞牛 NAS · 预览", "", "", "Dolby Atmos · SMB/WebDAV", 3120, 890, 0, true},
        {"demo-music-1", "plugin-music", "Clair de Lune (Bilingual Study Edition)", "Debussy / Studio",
         "music", "Music · FLAC", "", "", "FLAC 24bit / 96kHz", 304, 72, 0, true},
        {"demo-music-2", "plugin-music", "Midnight Architecture · LRC 双语逐行同步示例", "Acoustic Ensemble",
         "music", "Music · Hi-Res", "", "", "ALAC Lossless", 248, 0, 0, true},
    };
}

bool focusIsInsideView(brls::View* ancestor) {
    if (!ancestor) return false;
    brls::View* focus = brls::Application::getCurrentFocus();
    while (focus) {
        if (focus == ancestor) return true;
        focus = focus->getParent();
    }
    return false;
}

float estimateMediaStageWidth(const brls::View* self, bool immersive) {
    const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    if (immersive) {
        return std::max(320.0f, cw);
    }
    float avail = cw - 260.0f - 48.0f; // left rail (260) + workbench horizontal padding (24*2)
    auto* v = const_cast<brls::View*>(self);
    if (v && v->getParent() && v->getParent()->getParent()) {
        if (auto* split = dynamic_cast<brls::Box*>(v->getParent()->getParent()->getParent())) {
            if (split->getAxis() == brls::Axis::ROW) {
                for (auto* ch : split->getChildren()) {
                    if (ch != v->getParent()->getParent() &&
                        ch->getVisibility() == brls::Visibility::VISIBLE) {
                        avail -= 388.0f;
                    }
                }
            }
        }
    }
    return std::max(320.0f, avail);
}

float estimateMediaStageHeight(const brls::View* self, bool immersive) {
    const float ch = brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
    if (immersive) {
        return std::max(280.0f, ch);
    }
    float avail = ch;
    auto* v = const_cast<brls::View*>(self);
    if (v && v->getParent() && v->getParent()->getParent()) {
        if (auto* split = dynamic_cast<brls::Box*>(v->getParent()->getParent()->getParent())) {
            if (split->getAxis() == brls::Axis::COLUMN) {
                for (auto* sibling : split->getChildren()) {
                    if (sibling != v->getParent()->getParent() &&
                        sibling->getVisibility() == brls::Visibility::VISIBLE) {
                        avail -= 310.0f;
                    }
                }
            }
        }
    }
    return std::max(320.0f, avail);
}

int computeVideoGridColumns(float stage_w) {
    if (stage_w < 560.0f) return 1;
    if (stage_w < 920.0f) return 2;
    if (stage_w < 1520.0f) return 3;
    if (stage_w < 2320.0f) return 4;
    return 5;
}

std::string clipUtf8Bytes(const std::string& s, size_t max_bytes) {
    if (s.size() <= max_bytes) return s;
    size_t cut = max_bytes;
    while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0u) == 0x80u) {
        --cut;
    }
    return s.substr(0, cut) + "…";
}

struct ReusablePillSlot {
    brls::Box* box = nullptr;
    brls::Label* lbl = nullptr;

    static ReusablePillSlot create() {
        ReusablePillSlot s;
        s.box = new brls::Box(brls::Axis::ROW);
        s.box->setPadding(4, 10, 4, 10);
        s.box->setCornerRadius(7);
        s.box->setAlignItems(brls::AlignItems::CENTER);
        s.lbl = ui::makeLabel("", 13.0f, "font/grey", 1.2f);
        s.lbl->setSingleLine(true);
        s.box->addView(s.lbl);
        return s;
    }

    void bind(const std::string& text,
              const std::string& bg_token,
              const std::string& fg_token) {
        if (text.empty()) {
            box->setVisibility(brls::Visibility::GONE);
            return;
        }
        box->setVisibility(brls::Visibility::VISIBLE);
        box->setBackgroundColor(ui::color(bg_token));
        lbl->setTextColor(ui::color(fg_token));
        lbl->setText(text);
    }
};

class MediaVideoGridCell final : public RecyclingGridCell {
public:
    MediaVideoGridCell() : RecyclingGridCell(brls::Axis::COLUMN) {
        setPadding(14.0f);
        setBackgroundColor(ui::color("desktop/surface"));
        setCornerRadius(12.0f);
        setHighlightCornerRadius(12.0f);
        setHideHighlightBackground(true);
        setHideHighlightBorder(true);
        setBorderThickness(1);
        setBorderColor(ui::color("desktop/line"));
        setFocusable(true);

        thumb_card_ = ui::makeCard("desktop/soft", 10.0f, 9.0f);
        thumb_card_->setBorderThickness(0);
        thumb_card_->setMarginBottom(10);

        badge_row_ = new brls::Box(brls::Axis::ROW);
        badge_row_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        source_pill_ = ReusablePillSlot::create();
        dur_pill_ = ReusablePillSlot::create();
        badge_row_->addView(source_pill_.box);
        badge_row_->addView(dur_pill_.box);
        thumb_card_->addView(badge_row_);

        music_cover_wrap_ = new brls::Box(brls::Axis::ROW);
        music_cover_wrap_->setWidthPercentage(100);
        music_cover_wrap_->setMarginTop(6);
        music_cover_wrap_->setJustifyContent(brls::JustifyContent::CENTER);
        music_cover_wrap_->setAlignItems(brls::AlignItems::CENTER);
        music_cover_img_ = new CachedImageView(&ImageCache::instance());
        music_cover_img_->setCornerRadius(8.0f);
        music_cover_img_->setScalingType(brls::ImageScalingType::FILL);
        music_cover_wrap_->addView(music_cover_img_);
        thumb_card_->addView(music_cover_wrap_);

        video_cover_img_ = new CachedImageView(&ImageCache::instance());
        video_cover_img_->setWidthPercentage(100);
        video_cover_img_->setMarginTop(6);
        video_cover_img_->setCornerRadius(7.0f);
        video_cover_img_->setScalingType(brls::ImageScalingType::FILL);
        thumb_card_->addView(video_cover_img_);

        placeholder_box_ = new brls::Box(brls::Axis::COLUMN);
        placeholder_box_->setWidthPercentage(100);
        placeholder_box_->setMarginTop(6);
        placeholder_box_->setJustifyContent(brls::JustifyContent::CENTER);
        placeholder_box_->setAlignItems(brls::AlignItems::CENTER);
        placeholder_lbl_ = ui::makeLabel("16:9 视频预览", 13.0f, "font/grey");
        placeholder_box_->addView(placeholder_lbl_);
        thumb_card_->addView(placeholder_box_);

        pills_row_ = new brls::Box(brls::Axis::ROW);
        pills_row_->setMarginTop(6);
        ui_comfort::enableFlexWrap(pills_row_, 4.0f);
        quality_pill_ = ReusablePillSlot::create();
        quality_pill_.box->setMarginRight(6);
        bilingual_pill_ = ReusablePillSlot::create();
        bilingual_pill_.box->setMarginRight(6);
        prov_pill_ = ReusablePillSlot::create();
        pills_row_->addView(quality_pill_.box);
        pills_row_->addView(bilingual_pill_.box);
        pills_row_->addView(prov_pill_.box);
        thumb_card_->addView(pills_row_);

        addView(thumb_card_);

        title_lbl_ = ui::makeLabel("", 16.0f, "brls/text", 1.38f);
        title_lbl_->setMarginBottom(5);
        addView(title_lbl_);

        creator_lbl_ = ui::makeLabel("", 13.5f, "font/grey");
        creator_lbl_->setSingleLine(true);
        addView(creator_lbl_);

        registerClickAction([this](brls::View*) {
            if (on_activate_) on_activate_();
            return true;
        });

        addGestureRecognizer(new brls::TapGestureRecognizer(
            [this](brls::TapGestureStatus status, brls::Sound* soundToPlay) {
                if (!isFocusable() || getVisibility() != brls::Visibility::VISIBLE) return;
                if (status.state == brls::GestureState::START ||
                    status.state == brls::GestureState::UNSURE) {
                    playClickAnimation(false, false);
                } else if (status.state == brls::GestureState::FAILED ||
                           status.state == brls::GestureState::INTERRUPTED) {
                    playClickAnimation(true);
                } else if (status.state == brls::GestureState::END) {
                    playClickAnimation(true);
                    brls::Application::giveFocus(this);
                    if (soundToPlay) *soundToPlay = brls::SOUND_CLICK;
                    if (on_activate_) on_activate_();
                }
            }));
    }

    void prepareForReuse() override {
        on_activate_ = nullptr;
    }

    void bind(const MediaItem& item,
              float inner_cover_w,
              float cover_h,
              std::function<void()> on_activate) {
        on_activate_ = std::move(on_activate);
        setBackgroundColor(ui::color("desktop/surface"));
        setBorderColor(ui::color("desktop/line"));
        thumb_card_->setBackgroundColor(ui::color("desktop/soft"));

        source_pill_.bind(item.source_badge.empty() ? "Media" : item.source_badge,
                          "desktop/surface", "domain/media");
        std::string right_badge;
        const int ep_count = item.extra.is_object() ? item.extra.value("episode_count", 0) : 0;
        if (ep_count > 0) {
            right_badge = "共 " + std::to_string(ep_count) + " 集";
        } else if (item.duration_sec > 0.0 || item.kind != "nas") {
            right_badge = formatDuration(item.duration_sec);
        }
        dur_pill_.bind(right_badge, "desktop/surface", "brls/text");

        const bool url_changed = (item.cover_url != bound_cover_url_);
        bound_cover_url_ = item.cover_url;

        if (!item.cover_url.empty()) {
            placeholder_box_->setVisibility(brls::Visibility::GONE);
            if (item.kind == "music") {
                if (url_changed) {
                    video_cover_img_->setImageUrl("", {});
                    music_cover_img_->setImageUrl("", {});
                }
                video_cover_img_->setVisibility(brls::Visibility::GONE);
                music_cover_wrap_->setVisibility(brls::Visibility::VISIBLE);
                music_cover_wrap_->setHeight(cover_h);
                const float sq_side = std::min(inner_cover_w, cover_h);
                music_cover_img_->setDimensions(sq_side, sq_side);
                music_cover_img_->setImageUrl(item.cover_url, extractHeadersFromItem(item));
            } else {
                if (url_changed) {
                    music_cover_img_->setImageUrl("", {});
                    video_cover_img_->setImageUrl("", {});
                }
                music_cover_wrap_->setVisibility(brls::Visibility::GONE);
                video_cover_img_->setVisibility(brls::Visibility::VISIBLE);
                video_cover_img_->setHeight(cover_h);
                video_cover_img_->setImageUrl(item.cover_url, extractHeadersFromItem(item));
            }
        } else {
            if (url_changed) {
                music_cover_img_->setImageUrl("", {});
                video_cover_img_->setImageUrl("", {});
            }
            music_cover_wrap_->setVisibility(brls::Visibility::GONE);
            video_cover_img_->setVisibility(brls::Visibility::GONE);
            placeholder_box_->setVisibility(brls::Visibility::VISIBLE);
            placeholder_box_->setHeight(cover_h);
            placeholder_lbl_->setTextColor(ui::color("font/grey"));
            placeholder_lbl_->setText(item.kind == "music" ? "♪ 专辑封面" : "16:9 视频预览");
        }

        const bool quality_is_reason = item.extra.is_object() &&
            (item.quality_label == item.extra.value("reason", "") ||
             item.quality_label == item.extra.value("why_recommended", ""));
        const std::string q_text = (!item.quality_label.empty() && !quality_is_reason)
                                       ? item.quality_label
                                       : std::string{};
        quality_pill_.bind(q_text, "status/info_bg", "status/info_fg");
        bilingual_pill_.bind(item.has_bilingual_sub ? "双语字幕" : "", "status/ok_bg", "status/ok_fg");

        std::string prov_text;
        std::string prov_fg = "status/info_fg";
        if (item.extra.is_object()) {
            const std::string prev_date = item.extra.value("previously_recommended_on", "");
            const std::string cur_mode = item.extra.value("curation_mode", "");
            if (!prev_date.empty()) {
                prov_text = "经典重温 · " + prev_date;
            } else if (cur_mode == "interactive_ephemeral") {
                prov_text = "AI 灵感推荐";
                prov_fg = "domain/core";
            }
        }
        prov_pill_.bind(prov_text, "status/info_bg", prov_fg);

        const bool any_pill = !q_text.empty() || item.has_bilingual_sub || !prov_text.empty();
        pills_row_->setVisibility(any_pill ? brls::Visibility::VISIBLE : brls::Visibility::GONE);

        title_lbl_->setTextColor(ui::color("brls/text"));
        title_lbl_->setText(clipUtf8Bytes(item.title, 96));

        const std::string creator_txt =
            item.creator.empty() ? (item.kind == "music" ? "音乐曲目" : "视频内容") : item.creator;
        creator_lbl_->setTextColor(ui::color("font/grey"));
        creator_lbl_->setText(creator_txt);
    }

private:
    brls::Box* thumb_card_ = nullptr;
    brls::Box* badge_row_ = nullptr;
    ReusablePillSlot source_pill_;
    ReusablePillSlot dur_pill_;
    brls::Box* music_cover_wrap_ = nullptr;
    CachedImageView* music_cover_img_ = nullptr;
    CachedImageView* video_cover_img_ = nullptr;
    brls::Box* placeholder_box_ = nullptr;
    brls::Label* placeholder_lbl_ = nullptr;
    brls::Box* pills_row_ = nullptr;
    ReusablePillSlot quality_pill_;
    ReusablePillSlot bilingual_pill_;
    ReusablePillSlot prov_pill_;
    brls::Label* title_lbl_ = nullptr;
    brls::Label* creator_lbl_ = nullptr;
    std::string bound_cover_url_;
    std::function<void()> on_activate_;
};

class NasMediaGridCell final : public RecyclingGridCell {
public:
    NasMediaGridCell() : RecyclingGridCell(brls::Axis::COLUMN) {
        setPadding(14.0f);
        setBackgroundColor(ui::color("desktop/surface"));
        setCornerRadius(12.0f);
        setHighlightCornerRadius(12.0f);
        setHideHighlightBackground(true);
        setHideHighlightBorder(true);
        setBorderThickness(1);
        setBorderColor(ui::color("desktop/line"));
        setFocusable(true);

        top_row_ = new brls::Box(brls::Axis::ROW);
        top_row_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top_row_->setMarginBottom(6);
        badge_pill_ = ReusablePillSlot::create();
        sub_pill_ = ReusablePillSlot::create();
        top_row_->addView(badge_pill_.box);
        top_row_->addView(sub_pill_.box);
        addView(top_row_);

        title_lbl_ = ui::makeLabel("", 16.0f, "brls/text", 1.38f);
        addView(title_lbl_);

        meta_lbl_ = ui::makeLabel("", 13.5f, "font/grey");
        meta_lbl_->setMarginTop(6);
        meta_lbl_->setSingleLine(true);
        addView(meta_lbl_);

        registerClickAction([this](brls::View*) {
            if (on_activate_) on_activate_();
            return true;
        });

        addGestureRecognizer(new brls::TapGestureRecognizer(
            [this](brls::TapGestureStatus status, brls::Sound* soundToPlay) {
                if (!isFocusable() || getVisibility() != brls::Visibility::VISIBLE) return;
                if (status.state == brls::GestureState::START ||
                    status.state == brls::GestureState::UNSURE) {
                    playClickAnimation(false, false);
                } else if (status.state == brls::GestureState::FAILED ||
                           status.state == brls::GestureState::INTERRUPTED) {
                    playClickAnimation(true);
                } else if (status.state == brls::GestureState::END) {
                    playClickAnimation(true);
                    brls::Application::giveFocus(this);
                    if (soundToPlay) *soundToPlay = brls::SOUND_CLICK;
                    if (on_activate_) on_activate_();
                }
            }));
    }

    void prepareForReuse() override { on_activate_ = nullptr; }

    void bind(const MediaItem& item, std::function<void()> on_activate) {
        on_activate_ = std::move(on_activate);
        setBackgroundColor(ui::color("desktop/surface"));
        setBorderColor(ui::color("desktop/line"));
        badge_pill_.bind(item.source_badge.empty() ? "飞牛 NAS" : item.source_badge,
                         "status/info_bg", "domain/media");
        sub_pill_.bind(item.has_bilingual_sub ? "双语字幕" : "", "status/ok_bg", "status/ok_fg");
        title_lbl_->setTextColor(ui::color("brls/text"));
        title_lbl_->setText(clipUtf8Bytes(item.title, 88));
        meta_lbl_->setTextColor(ui::color("font/grey"));
        meta_lbl_->setText((item.quality_label.empty() ? item.kind : item.quality_label) + " · 点击播放");
    }

private:
    brls::Box* top_row_ = nullptr;
    ReusablePillSlot badge_pill_;
    ReusablePillSlot sub_pill_;
    brls::Label* title_lbl_ = nullptr;
    brls::Label* meta_lbl_ = nullptr;
    std::function<void()> on_activate_;
};

class ImdbDiscoverCell final : public RecyclingGridCell {
public:
    ImdbDiscoverCell() : RecyclingGridCell(brls::Axis::ROW) {
        setWidthPercentage(100);
        setPadding(14.0f);
        setBackgroundColor(ui::color("desktop/surface"));
        setCornerRadius(10.0f);
        setHighlightCornerRadius(10.0f);
        setBorderThickness(1);
        setBorderColor(ui::color("desktop/line"));
        setAlignItems(brls::AlignItems::CENTER);

        poster_img_ = new CachedImageView(&ImageCache::instance());
        poster_img_->setWidth(68.0f);
        poster_img_->setHeight(102.0f);
        poster_img_->setShrink(0.0f);
        poster_img_->setCornerRadius(6.0f);
        poster_img_->setScalingType(brls::ImageScalingType::FILL);
        poster_img_->setMarginRight(14.0f);
        addView(poster_img_);

        info_col_ = new brls::Box(brls::Axis::COLUMN);
        info_col_->setGrow(1.0f);
        info_col_->setShrink(1.0f);

        top_row_ = new brls::Box(brls::Axis::ROW);
        top_row_->setAlignItems(brls::AlignItems::CENTER);
        kind_pill_ = ReusablePillSlot::create();
        kind_pill_.box->setMarginRight(8);
        meta_lbl_ = ui::makeLabel("", 12.5f, "font/grey");
        meta_lbl_->setSingleLine(true);
        top_row_->addView(kind_pill_.box);
        top_row_->addView(meta_lbl_);
        info_col_->addView(top_row_);

        title_lbl_ = ui::makeLabel("", 16.5f, "brls/text");
        title_lbl_->setMarginTop(5);
        title_lbl_->setSingleLine(true);
        info_col_->addView(title_lbl_);

        desc_lbl_ = ui::makeLabel("", 13.0f, "font/grey");
        desc_lbl_->setMarginTop(4);
        info_col_->addView(desc_lbl_);

        btn_row_ = new brls::Box(brls::Axis::ROW);
        btn_row_->setMarginTop(8);
        play_btn_ = ui::makeButton("飞牛匹配播放", [this]() {
            if (on_local_match_) on_local_match_();
        }, false, 30.0f);
        play_btn_->setMarginRight(8);
        btn_row_->addView(play_btn_);

        add_btn_ = ui::makeButton("加入 Arr 选小体积", [this]() {
            if (on_add_arr_) on_add_arr_("compact");
        }, true, 30.0f);
        add_btn_->setMarginRight(8);
        btn_row_->addView(add_btn_);

        add_fast_btn_ = ui::makeButton("加入 Arr 选最快下载", [this]() {
            if (on_add_arr_) on_add_arr_("fastest");
        }, false, 30.0f);
        add_fast_btn_->setMarginRight(8);
        btn_row_->addView(add_fast_btn_);

        ws_btn_ = ui::makeButton("完整选种与队列", [this]() {
            if (on_open_ws_) on_open_ws_();
        }, false, 30.0f);
        btn_row_->addView(ws_btn_);

        info_col_->addView(btn_row_);
        addView(info_col_);
    }

    void prepareForReuse() override {
        on_local_match_ = nullptr;
        on_add_arr_ = nullptr;
        on_open_ws_ = nullptr;
    }

    void bind(const nlohmann::json& entry,
              bool can_local_match,
              bool has_arr,
              std::function<void()> on_local_match,
              std::function<void(const std::string&)> on_add_arr,
              std::function<void()> on_open_ws) {
        on_local_match_ = std::move(on_local_match);
        on_add_arr_ = std::move(on_add_arr);
        on_open_ws_ = std::move(on_open_ws);

        setBackgroundColor(ui::color("desktop/surface"));
        setBorderColor(ui::color("desktop/line"));

        const std::string imdb_id = entry.value("imdb_id", "");
        const std::string title = entry.value("title", "");
        const std::string orig_title = entry.value("original_title", "");
        const std::string kind = entry.value("kind", "movie");
        const int year = entry.value("year", 0);
        const int rank = entry.value("rank", 0);
        const double rating = entry.value("rating", 0.0);
        const std::string poster_url = entry.value("poster_url", "");
        const std::string plot = entry.value("plot", "");
        const std::string cast_summary = entry.value("cast_summary", "");

        if (poster_url != bound_poster_url_) {
            poster_img_->setImageUrl("", {});
            bound_poster_url_ = poster_url;
        }
        if (!poster_url.empty()) {
            poster_img_->setVisibility(brls::Visibility::VISIBLE);
            poster_img_->setImageUrl(poster_url, {{"Referer", "https://www.imdb.com/"}});
        } else {
            poster_img_->setVisibility(brls::Visibility::GONE);
        }

        kind_pill_.bind(kind == "series" ? "剧集 · Sonarr" : "电影 · Radarr",
                        "desktop/soft", "domain/media");

        std::string meta;
        if (rank > 0) meta += "#" + std::to_string(rank) + " · ";
        if (year > 0) meta += std::to_string(year) + " · ";
        if (rating > 0.0) {
            char rbuf[32];
            std::snprintf(rbuf, sizeof(rbuf), "★ %.1f", rating);
            meta += std::string(rbuf) + " · ";
        }
        meta += imdb_id;
        meta_lbl_->setTextColor(ui::color("font/grey"));
        meta_lbl_->setText(meta);

        std::string full_title = title;
        if (!orig_title.empty() && orig_title != title) {
            full_title += " (" + orig_title + ")";
        }
        title_lbl_->setTextColor(ui::color("brls/text"));
        title_lbl_->setText(clipUtf8Bytes(full_title, 110));

        const std::string desc = !plot.empty() ? clipUtf8Bytes(plot, 160) : clipUtf8Bytes(cast_summary, 160);
        if (!desc.empty()) {
            desc_lbl_->setVisibility(brls::Visibility::VISIBLE);
            desc_lbl_->setTextColor(ui::color("font/grey"));
            desc_lbl_->setText(desc);
        } else {
            desc_lbl_->setVisibility(brls::Visibility::GONE);
        }

        play_btn_->setVisibility(can_local_match ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        add_btn_->setVisibility(has_arr ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        add_fast_btn_->setVisibility(has_arr ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }

private:
    CachedImageView* poster_img_ = nullptr;
    brls::Box* info_col_ = nullptr;
    brls::Box* top_row_ = nullptr;
    ReusablePillSlot kind_pill_;
    brls::Label* meta_lbl_ = nullptr;
    brls::Label* title_lbl_ = nullptr;
    brls::Label* desc_lbl_ = nullptr;
    brls::Box* btn_row_ = nullptr;
    brls::Button* play_btn_ = nullptr;
    brls::Button* add_btn_ = nullptr;
    brls::Button* add_fast_btn_ = nullptr;
    brls::Button* ws_btn_ = nullptr;
    std::string bound_poster_url_;
    std::function<void()> on_local_match_;
    std::function<void(const std::string&)> on_add_arr_;
    std::function<void()> on_open_ws_;
};

class MusicTrackRowCell final : public RecyclingGridCell {
public:
    MusicTrackRowCell() : RecyclingGridCell(brls::Axis::ROW) {
        setWidthPercentage(100);
        setPadding(10.0f);
        setCornerRadius(8.0f);
        setHighlightCornerRadius(8.0f);
        setHideHighlightBackground(true);
        setHideHighlightBorder(true);
        setBorderThickness(1);
        setBorderColor(ui::color("desktop/line"));
        setAlignItems(brls::AlignItems::CENTER);
        setFocusable(true);

        thumb_img_ = new CachedImageView(&ImageCache::instance());
        thumb_img_->setDimensions(46, 46);
        thumb_img_->setShrink(0.0f);
        thumb_img_->setCornerRadius(6.0f);
        thumb_img_->setScalingType(brls::ImageScalingType::FILL);
        thumb_img_->setMarginRight(10);
        addView(thumb_img_);

        ph_box_ = ui::makeCard("desktop/surface", 0.0f, 6.0f);
        ph_box_->setDimensions(46, 46);
        ph_box_->setShrink(0.0f);
        ph_box_->setMarginRight(10);
        ph_box_->setJustifyContent(brls::JustifyContent::CENTER);
        ph_box_->setAlignItems(brls::AlignItems::CENTER);
        ph_lbl_ = ui::makeLabel("♪", 15.0f, "font/grey");
        ph_box_->addView(ph_lbl_);
        addView(ph_box_);

        info_col_ = new brls::Box(brls::Axis::COLUMN);
        info_col_->setGrow(1.0f);
        info_col_->setShrink(1.0f);

        top_row_ = new brls::Box(brls::Axis::ROW);
        top_row_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top_row_->setAlignItems(brls::AlignItems::CENTER);
        title_lbl_ = ui::makeLabel("", 14.5f, "brls/text");
        title_lbl_->setShrink(1.0f);
        title_lbl_->setSingleLine(true);
        top_row_->addView(title_lbl_);
        prov_pill_ = ReusablePillSlot::create();
        prov_pill_.box->setMarginLeft(6);
        top_row_->addView(prov_pill_.box);
        info_col_->addView(top_row_);

        sub_lbl_ = ui::makeLabel("", 13.0f, "font/grey");
        sub_lbl_->setMarginTop(2);
        sub_lbl_->setSingleLine(true);
        info_col_->addView(sub_lbl_);
        addView(info_col_);

        registerClickAction([this](brls::View*) {
            if (on_activate_) on_activate_();
            return true;
        });

        addGestureRecognizer(new brls::TapGestureRecognizer(
            [this](brls::TapGestureStatus status, brls::Sound* soundToPlay) {
                if (!isFocusable() || getVisibility() != brls::Visibility::VISIBLE) return;
                if (status.state == brls::GestureState::START ||
                    status.state == brls::GestureState::UNSURE) {
                    playClickAnimation(false, false);
                } else if (status.state == brls::GestureState::FAILED ||
                           status.state == brls::GestureState::INTERRUPTED) {
                    playClickAnimation(true);
                } else if (status.state == brls::GestureState::END) {
                    playClickAnimation(true);
                    brls::Application::giveFocus(this);
                    if (soundToPlay) *soundToPlay = brls::SOUND_CLICK;
                    if (on_activate_) on_activate_();
                }
            }));
    }

    void prepareForReuse() override { on_activate_ = nullptr; }

    void bind(const MediaItem& tr, bool selected, std::function<void()> on_activate) {
        on_activate_ = std::move(on_activate);
        setBackgroundColor(ui::color(selected ? "desktop/active" : "desktop/soft"));
        setBorderColor(ui::color(selected ? "brls/accent" : "desktop/line"));

        if (tr.cover_url != bound_cover_url_) {
            thumb_img_->setImageUrl("", {});
            bound_cover_url_ = tr.cover_url;
        }
        if (!tr.cover_url.empty()) {
            ph_box_->setVisibility(brls::Visibility::GONE);
            thumb_img_->setVisibility(brls::Visibility::VISIBLE);
            thumb_img_->setImageUrl(tr.cover_url, extractHeadersFromItem(tr));
        } else {
            thumb_img_->setVisibility(brls::Visibility::GONE);
            ph_box_->setVisibility(brls::Visibility::VISIBLE);
            ph_box_->setBackgroundColor(ui::color("desktop/surface"));
            ph_lbl_->setTextColor(ui::color(selected ? "brls/accent" : "font/grey"));
        }

        title_lbl_->setTextColor(ui::color(selected ? "brls/accent" : "brls/text"));
        title_lbl_->setText(tr.title);

        const std::string prev_date =
            tr.extra.is_object() ? tr.extra.value("previously_recommended_on", "") : "";
        prov_pill_.bind(prev_date.empty() ? "" : ("经典重温 · " + prev_date),
                        "status/info_bg", "status/info_fg");

        std::string sub_line = tr.creator;
        const bool q_is_reason = tr.extra.is_object() &&
            (tr.quality_label == tr.extra.value("reason", "") ||
             tr.quality_label == tr.extra.value("why_recommended", ""));
        if (!tr.quality_label.empty() && !q_is_reason) {
            if (!sub_line.empty()) sub_line += " · ";
            sub_line += tr.quality_label;
        }
        if (!sub_line.empty()) {
            sub_lbl_->setVisibility(brls::Visibility::VISIBLE);
            sub_lbl_->setTextColor(ui::color("font/grey"));
            sub_lbl_->setText(sub_line);
        } else {
            sub_lbl_->setVisibility(brls::Visibility::GONE);
        }
    }

private:
    CachedImageView* thumb_img_ = nullptr;
    brls::Box* ph_box_ = nullptr;
    brls::Label* ph_lbl_ = nullptr;
    brls::Box* info_col_ = nullptr;
    brls::Box* top_row_ = nullptr;
    brls::Label* title_lbl_ = nullptr;
    ReusablePillSlot prov_pill_;
    brls::Label* sub_lbl_ = nullptr;
    std::string bound_cover_url_;
    std::function<void()> on_activate_;
};

} // namespace

MediaWorkbench::MediaWorkbench(std::function<void(const std::string&)> on_entity_focus,
                               std::function<void()> on_open_marketplace)
    : brls::Box(brls::Axis::COLUMN),
      on_entity_focus_(std::move(on_entity_focus)),
      on_open_marketplace_(std::move(on_open_marketplace)) {
    setGrow(1.0f);
    setShrink(1.0f);
    setMinHeight(0);
    setHeightPercentage(100);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    header_box_ = new brls::Box(brls::Axis::ROW);
    header_box_->setId("omnidesk/pane_header");
    header_box_->setShrink(0.0f);
    header_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header_box_->setAlignItems(brls::AlignItems::CENTER);
    header_box_->setMarginBottom(12);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->setShrink(1.0f);
    left->addView(ui::makeLabel("影视与音乐 · Media Workbench", 24.0f, "brls/text"));
    source_status_label_ = ui::makeLabel("16:9 影视卡片 · 双语歌词工作室 · IMDb 选片 · fnOS NAS · MPV 播放器舞台", 14.5f, "font/grey");
    source_status_label_->setMarginTop(3);
    left->addView(source_status_label_);
    header_box_->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    right->setShrink(0.0f);
    right->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(right, 6.0f);
    date_filter_btn_ = ui::makeButton("日历查找", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptRehydrateOrDateFilter();
    }, false, 35.0f);
    date_filter_btn_->setMarginRight(8);
    right->addView(date_filter_btn_);
    auto* r_btn = ui::makeButton("刷新 (R)", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) refresh(true);
    }, true, 35.0f);
    r_btn->setMarginRight(8);
    right->addView(r_btn);
    auto* s_btn = ui::makeButton("搜索片库 /", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptSearch();
    }, false, 35.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    auto* m_btn = ui::makeButton("管理影视插件", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
            on_open_marketplace_();
        }
    }, false, 35.0f);
    right->addView(m_btn);
    header_box_->addView(right);
    addView(header_box_);

    sub_tabs_box_ = new brls::Box(brls::Axis::ROW);
    sub_tabs_box_->setId("omnidesk/pane_filters");
    sub_tabs_box_->setShrink(0.0f);
    sub_tabs_box_->setMarginBottom(12);
    ui_comfort::enableFlexWrap(sub_tabs_box_, 6.0f);
    addView(sub_tabs_box_);

    class MediaStageDataSource final : public RecyclingGridDataSource {
    public:
        explicit MediaStageDataSource(MediaWorkbench* owner) : owner_(owner) {}

        size_t getItemCount() override {
            if (!owner_) return 0;
            if (owner_->sub_mode_ == SubMode::ImdbDiscover) {
                return owner_->visible_imdb_items_.size();
            }
            return owner_->visible_grid_items_.size();
        }

        RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) override {
            if (!owner_ || !recycler) return nullptr;
            auto* wb = owner_;
            auto alive = owner_->alive_;

            if (wb->sub_mode_ == SubMode::ImdbDiscover) {
                auto* cell = static_cast<ImdbDiscoverCell*>(recycler->dequeueReusableCell("imdb_card"));
                if (!cell || index >= wb->visible_imdb_items_.size()) return cell;
                const nlohmann::json& entry = wb->visible_imdb_items_[index];
                auto& kernel = HostKernel::instance();
                const bool has_arr = kernel.isInitialized() && kernel.capabilities().has("arr.manage.v1");
                const bool can_local_match = has_arr && kernel.capabilities().has("media.local_match.v1");

                const std::string imdb_id = entry.value("imdb_id", "");
                const std::string title = entry.value("title", "");
                const int year = entry.value("year", 0);
                const std::string kind = entry.value("kind", "movie");

                cell->bind(
                    entry,
                    can_local_match,
                    has_arr,
                    [wb, alive, imdb_id, title, year, kind]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        auto& k = HostKernel::instance();
                        if (!k.isInitialized()) return;
                        if (wb->imdb_action_request_) wb->imdb_action_request_->cancel();
                        auto req = CancelToken::create();
                        wb->imdb_action_request_ = req;
                        loadUiInBackground(
                            k.uiTasks(),
                            "media.imdb_local_match",
                            req,
                            [imdb_id, title, year, kind](const auto& cancel) {
                                return HostKernel::instance().capabilities().callCancellable(
                                    "arr.manage.v1",
                                    {
                                        {"action", "local_match"},
                                        {"imdb_id", imdb_id},
                                        {"title", title},
                                        {"year", year},
                                        {"kind", kind},
                                        {"auto_open_player", true},
                                    },
                                    cancel);
                            },
                            [alive](CapabilityResult res) {
                                if (!alive->load(std::memory_order_acquire)) return;
                                if (res.ok && res.data.is_object() && res.data.value("matched", false)) {
                                    brls::Application::notify("飞牛影库已匹配并打开播放");
                                } else {
                                    brls::Application::notify("飞牛本地影库暂未入库该作品，可加入 Arr 下载");
                                }
                            },
                            []() {});
                    },
                    [wb, alive, imdb_id, title, kind](const std::string& sort_mode) {
                        if (!alive->load(std::memory_order_acquire)) return;
                        auto& k = HostKernel::instance();
                        if (!k.isInitialized()) return;
                        if (wb->imdb_action_request_) wb->imdb_action_request_->cancel();
                        auto req = CancelToken::create();
                        wb->imdb_action_request_ = req;
                        const std::string arr_kind = (kind == "series") ? "sonarr" : "radarr";
                        const std::string mode_label =
                            (sort_mode == "fastest") ? "预计最快下载" : "最优小体积";
                        brls::Application::notify("正在提交到 " +
                                                  std::string(arr_kind == "sonarr" ? "Sonarr" : "Radarr") +
                                                  "（" + mode_label + "）...");
                        loadInBackground(
                            k.jobs(),
                            "Arr 选片 · " + title,
                            req,
                            [arr_kind, imdb_id, title, sort_mode](const auto& cancel) {
                                return HostKernel::instance().capabilities().callCancellable(
                                    "arr.manage.v1",
                                    {
                                        {"action", "add"},
                                        {"kind", arr_kind},
                                        {"imdb_id", imdb_id},
                                        {"title", title},
                                        {"monitored", true},
                                        {"search", true},
                                        {"sort_mode", sort_mode},
                                    },
                                    cancel);
                            },
                            [alive](CapabilityResult res) {
                                if (!alive->load(std::memory_order_acquire)) return;
                                if (res.ok) {
                                    brls::Application::notify(
                                        res.data.is_object()
                                            ? res.data.value("message", "已加入 Arr 选片队列")
                                            : "已加入 Arr 选片队列");
                                } else {
                                    brls::Application::notify("加入 Arr 失败: " + res.error);
                                }
                            },
                            []() {});
                    },
                    [alive]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        if (HostKernel::instance().isInitialized()) {
                            HostKernel::instance().bus().publish("entity.workspace.open",
                                                                 {{"tab_id", "tab-arr-imdb"}});
                        }
                    });
                return cell;
            }

            auto* cell = static_cast<MediaVideoGridCell*>(recycler->dequeueReusableCell("media_card"));
            if (!cell || index >= wb->visible_grid_items_.size()) return cell;
            const MediaItem item = wb->visible_grid_items_[index];
            const float inner_cover_w = std::max(160.0f, recycler->cellColumnWidth() - 48.0f);
            cell->bind(item, inner_cover_w, wb->active_video_cover_h_, [wb, alive, item]() {
                brls::sync([wb, alive, item]() {
                    if (alive->load(std::memory_order_acquire)) {
                        wb->activateMediaItem(item);
                    }
                });
            });
            return cell;
        }

    private:
        MediaWorkbench* owner_ = nullptr;
    };

    stage_grid_ = new RecyclingGrid();
    stage_grid_->setId("omnidesk/pane_primary");
    stage_grid_->setGrow(1.0f);
    stage_grid_->setShrink(1.0f);
    stage_grid_->setMinHeight(0);
    stage_grid_->setWidthPercentage(100);
    stage_grid_->setColSpace(14.0f);
    stage_grid_->setEstimatedRowSpace(14.0f);
    stage_grid_->setPreFetchLine(2);
    stage_grid_->registerCell("media_card", []() { return new MediaVideoGridCell(); });
    stage_grid_->registerCell("nas_card", []() { return new NasMediaGridCell(); });
    stage_grid_->registerCell("imdb_card", []() { return new ImdbDiscoverCell(); });
    stage_grid_->setOnNextPage([this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (getVisibility() != brls::Visibility::VISIBLE) return;
        if (sub_mode_ != SubMode::Videos || media_loading_more_ || !media_has_more_ ||
            visible_grid_items_.empty()) {
            return;
        }
        loadMoreMedia();
    });
    stage_grid_->setDataSource(new MediaStageDataSource(this), true);
    addView(stage_grid_);

    mpv_state_sub_ = MPVCore::instance().subscribeState(
        [this, alive = alive_](const MpvPlaybackState& st) {
            brls::sync([this, alive, pos = st.position_sec]() {
                if (!alive->load(std::memory_order_acquire)) return;
                updateActiveLyricHighlight(pos);
            });
        });

    coord_state_sub_ = PlaybackCoordinator::instance().subscribe(
        [this, alive = alive_](const PlaybackSessionState& st) {
            brls::sync([this, alive, st]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (st.active_role != PlaybackRole::BackgroundMusic) {
                    last_coord_track_id_.clear();
                    return;
                }
                const std::string coord_key =
                    st.active_track.item.source_id + ":" + st.active_track.item.id;
                const std::string prev_coord_key = last_coord_track_id_;
                last_coord_track_id_ = coord_key;

                if (has_active_item_ &&
                    st.active_track.item.id == active_item_.id &&
                    st.active_track.item.source_id == active_item_.source_id) {
                    bool changed = false;
                    if (active_item_.cover_url.empty() && !st.active_track.item.cover_url.empty()) {
                        active_item_.cover_url = st.active_track.item.cover_url;
                        changed = true;
                    }
                    if (active_item_.quality_label.empty() && !st.active_track.item.quality_label.empty()) {
                        active_item_.quality_label = st.active_track.item.quality_label;
                        changed = true;
                    }
                    if (active_item_.duration_sec <= 0.0 && st.active_track.item.duration_sec > 0.0) {
                        active_item_.duration_sec = st.active_track.item.duration_sec;
                        changed = true;
                    }
                    if (active_lyrics_.empty() && !st.active_track.lyrics.empty()) {
                        active_lyrics_ = st.active_track.lyrics;
                        changed = true;
                    }
                    if (changed) {
                        for (auto& it : current_items_) {
                            if (it.id == active_item_.id && it.source_id == active_item_.source_id) {
                                if (it.cover_url.empty()) it.cover_url = active_item_.cover_url;
                                if (it.quality_label.empty()) it.quality_label = active_item_.quality_label;
                                if (it.duration_sec <= 0.0) it.duration_sec = active_item_.duration_sec;
                            }
                        }
                        if (sub_mode_ == SubMode::MusicLyrics) {
                            rebuildSubContent();
                        }
                    }
                } else if (!prev_coord_key.empty() && coord_key != prev_coord_key &&
                           has_active_item_ &&
                           (active_item_.source_id + ":" + active_item_.id) == prev_coord_key &&
                           !st.active_track.item.id.empty()) {
                    selectItem(st.active_track.item, false);
                }
            });
        });

    if (HostKernel::instance().isInitialized()) {
        recommend_sub_ = HostKernel::instance().bus().subscribe(
            "entity.recommend.updated",
            "host.media_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh(false);
                    }
                });
            });

        lyrics_updated_sub_ = HostKernel::instance().bus().subscribe(
            "entity.lyrics.updated",
            "host.media_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                if (!payload.is_object() || !payload.contains("lines") || !payload["lines"].is_array()) {
                    return;
                }
                const std::string upd_id = payload.value("id", std::string{});
                std::vector<LyricLine> updated_lines;
                for (const auto& l : payload["lines"]) {
                    if (!l.is_object()) continue;
                    double tsec = 0.0;
                    if (l.contains("time_sec") && l["time_sec"].is_number()) {
                        tsec = l["time_sec"].get<double>();
                    }
                    updated_lines.push_back(LyricLine{
                        tsec,
                        l.value("original", std::string{}),
                        l.value("translation", std::string{}),
                    });
                }
                if (updated_lines.empty()) return;
                brls::sync([this, alive, upd_id, lines = std::move(updated_lines)]() mutable {
                    if (!alive->load(std::memory_order_acquire) || !has_active_item_) return;
                    const auto am = extractAppleMusicLookup(active_item_);
                    if (!upd_id.empty() && active_item_.id != upd_id && am.song_id != upd_id) {
                        return;
                    }
                    active_lyrics_ = std::move(lines);
                    active_item_.has_bilingual_sub = true;
                    lyric_translating_ = false;
                    if (sub_mode_ == SubMode::MusicLyrics) {
                        rebuildSubContent();
                    }
                });
            });
    }

    resize_sub_ = brls::Application::getWindowSizeChangedEvent()->subscribe([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            onViewportLayoutChanged();
        }
    });

    refresh(false);
}

MediaWorkbench::~MediaWorkbench() {
    brls::Application::getWindowSizeChangedEvent()->unsubscribe(resize_sub_);
    if (lyrics_updated_sub_ != 0 && HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().unsubscribe(lyrics_updated_sub_);
        lyrics_updated_sub_ = 0;
    }
    if (recommend_sub_ != 0 && HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().unsubscribe(recommend_sub_);
        recommend_sub_ = 0;
    }
    if (coord_state_sub_ != 0) {
        PlaybackCoordinator::instance().unsubscribe(coord_state_sub_);
        coord_state_sub_ = 0;
    }
    if (mpv_state_sub_ != 0) {
        MPVCore::instance().unsubscribeState(mpv_state_sub_);
        mpv_state_sub_ = 0;
    }
    if (rehydrate_request_) {
        rehydrate_request_->cancel();
        rehydrate_request_.reset();
    }
    if (lyric_translate_request_) {
        lyric_translate_request_->cancel();
        lyric_translate_request_.reset();
    }
    if (lyric_explain_request_) {
        lyric_explain_request_->cancel();
        lyric_explain_request_.reset();
    }
    if (provenance_request_) provenance_request_->cancel();
    if (cover_enrich_request_) cover_enrich_request_->cancel();
    if (nas_cover_enrich_request_) nas_cover_enrich_request_->cancel();
    alive_->store(false, std::memory_order_release);
    cancelActiveFetch();
    lyrics_scroll_ = nullptr;
    music_queue_grid_ = nullptr;
    mpv_stage_ = nullptr;
}

void MediaWorkbench::setFullscreenToggleCallback(std::function<void()> on_toggle_fullscreen) {
    on_toggle_fullscreen_ = std::move(on_toggle_fullscreen);
}

void MediaWorkbench::setImmersiveFullscreen(bool active) {
    if (immersive_fullscreen_ == active) {
        return;
    }
    immersive_fullscreen_ = active;
    if (active && sub_mode_ != SubMode::PlayerStage) {
        sub_mode_ = SubMode::PlayerStage;
    }
    setPadding(active ? 0 : 22, active ? 0 : 24, active ? 0 : 18, active ? 0 : 24);
    if (header_box_) {
        header_box_->setVisibility(active ? brls::Visibility::GONE : brls::Visibility::VISIBLE);
    }
    if (sub_tabs_box_) {
        sub_tabs_box_->setVisibility(active ? brls::Visibility::GONE : brls::Visibility::VISIBLE);
    }
    last_stage_w_ = 0;
    last_stage_h_ = 0;
    rebuildSubContent();
}

void MediaWorkbench::onViewportLayoutChanged() {
    if (getVisibility() != brls::Visibility::VISIBLE) {
        last_stage_w_ = 0;
        last_stage_h_ = 0;
        return;
    }
    const int sw = static_cast<int>(std::round(estimateMediaStageWidth(this, immersive_fullscreen_)));
    const int sh = static_cast<int>(std::round(estimateMediaStageHeight(this, immersive_fullscreen_)));
    if (std::abs(sw - last_stage_w_) >= 12 ||
        ((sub_mode_ == SubMode::PlayerStage || sub_mode_ == SubMode::MusicLyrics) &&
         std::abs(sh - last_stage_h_) >= 12)) {
        rebuildSubContent();
    }
}

bool MediaWorkbench::cancelActiveFetch() {
    bool cancelled_any = false;
    if (!active_fetches_.empty()) {
        for (const auto& tok : active_fetches_) {
            if (tok) {
                tok->cancel();
                cancelled_any = true;
            }
        }
        active_fetches_.clear();
        ++fetch_seq_;
    }
    if (!more_fetches_.empty()) {
        for (const auto& tok : more_fetches_) {
            if (tok) {
                tok->cancel();
                cancelled_any = true;
            }
        }
        more_fetches_.clear();
        media_loading_more_ = false;
    }
    if (detail_request_) {
        if (!detail_request_->isCancelled()) {
            cancelled_any = true;
        }
        detail_request_->cancel();
        detail_request_.reset();
    }
    if (nas_status_request_) {
        if (!nas_status_request_->isCancelled()) {
            cancelled_any = true;
        }
        nas_status_request_->cancel();
        nas_status_request_.reset();
        nas_status_loading_ = false;
    }
    if (nas_dir_request_) {
        if (!nas_dir_request_->isCancelled()) {
            cancelled_any = true;
        }
        nas_dir_request_->cancel();
        nas_dir_request_.reset();
    }
    if (imdb_request_) {
        if (!imdb_request_->isCancelled()) {
            cancelled_any = true;
        }
        imdb_request_->cancel();
        imdb_request_.reset();
        imdb_loading_ = false;
    }
    if (imdb_action_request_) {
        if (!imdb_action_request_->isCancelled()) {
            cancelled_any = true;
        }
        imdb_action_request_->cancel();
        imdb_action_request_.reset();
    }
    if (!cancelled_any) {
        return false;
    }
    if (source_status_label_) {
        source_status_label_->setText("已取消媒体异步拉取");
    }
    return true;
}

bool MediaWorkbench::skipSponsorIfActive() {
    if (mpv_stage_ && mpv_stage_->skipSponsorIfActive()) {
        return true;
    }
    return MPVCore::instance().skipActiveAdSegment();
}

void MediaWorkbench::rebuildTabs() {
    if (!sub_tabs_box_) {
        return;
    }
    sub_tabs_box_->clearViews();
    struct Tab {
        SubMode mode;
        const char* label;
    };
    const Tab tabs[] = {
        {SubMode::Videos, "16:9 影视推荐"},
        {SubMode::DailyCuration, "每日策展"},
        {SubMode::MusicLyrics, "音乐与双语歌词"},
        {SubMode::NasLibrary, "飞牛 fnOS 私有影库"},
        {SubMode::ImdbDiscover, "IMDb 发现与选片"},
        {SubMode::PlayerStage, "MPV 播放舞台 & 章节"},
    };
    for (const auto& t : tabs) {
        auto* btn = ui_comfort::makeFilterChip(t.label, [this, alive = alive_, m = t.mode]() {
            brls::sync([this, alive, m]() {
                if (alive->load(std::memory_order_acquire)) {
                    setSubMode(m);
                }
            });
        }, sub_mode_ == t.mode, "domain/media", 32.0f);
        btn->setMarginRight(7);
        sub_tabs_box_->addView(btn);
    }
}

void MediaWorkbench::refresh(bool force_remote) {
    cached_feeds_.clear();
    if (sub_mode_ == SubMode::ImdbDiscover) {
        imdb_loaded_ = false;
    }
    if (stage_grid_) {
        stage_grid_->setContentOffsetY(0.0f, false);
    }
    fetchCurrentSubMode(false, force_remote);
}

void MediaWorkbench::fetchCurrentSubMode(bool preserve_detail, bool force_refresh) {
    auto detail = preserve_detail ? std::move(detail_request_) : nullptr;
    cancelActiveFetch();
    if (detail) detail_request_ = std::move(detail);
    media_page_ = 1;
    media_loading_more_ = false;
    media_has_more_ = true;
    rebuildTabs();
    if (date_filter_btn_) {
        if (!curation_date_.empty()) {
            date_filter_btn_->setText("日历 · " + curation_date_);
        } else if (search_query_.rfind("date:", 0) == 0 && search_query_.size() > 5) {
            date_filter_btn_->setText("日历 · " + search_query_.substr(5));
        } else {
            date_filter_btn_->setText("日历查找");
        }
    }

    auto& kernel = HostKernel::instance();
    if (sub_mode_ == SubMode::ImdbDiscover) {
        const bool has_imdb = kernel.isInitialized() && kernel.capabilities().has("imdb.discover.v1");
        if (source_status_label_) {
            source_status_label_->setText(has_imdb
                ? "已接入 IMDb 发现与 Radarr / Sonarr 选片扩展 (imdb.discover.v1 · arr.manage.v1)"
                : "未安装 plugin-arr-imdb · 可在插件市场一键启用 IMDb 发现与选片");
        }
        rebuildSubContent();
        return;
    }

    if (!kernel.isInitialized() || kernel.domains().mediaSources().empty()) {
        current_items_ = buildShowcaseMedia();
        source_status_label_->setText("布局预览模式（在插件市场安装 Bilibili / YouTube / 飞牛 NAS / Music 插件后自动接管）");
        if (!has_active_item_ && !current_items_.empty()) {
            active_item_ = current_items_.front();
            has_active_item_ = true;
        }
        rebuildSubContent();
        return;
    }

    auto sources = kernel.domains().mediaSources();
    const bool curated = sub_mode_ == SubMode::DailyCuration ||
        (sub_mode_ == SubMode::MusicLyrics && music_scope_ != MusicScope::Personal);
    sources.erase(std::remove_if(sources.begin(), sources.end(), [curated](const auto& src) {
        return (src->sourceId() == "plugin-ai-curator") != curated;
    }), sources.end());
    if (sources.empty()) {
        current_items_.clear();
        source_status_label_->setText(curated ? "请安装 AI 策展插件以浏览推荐归档" : "当前分类暂无媒体来源");
        rebuildSubContent();
        return;
    }
    std::string cat = "all";
    if (sub_mode_ == SubMode::MusicLyrics) cat = "music";
    else if (sub_mode_ == SubMode::NasLibrary) cat = "nas";
    if (curated) cat = "all";

    const std::string cache_key = (curated ? "curated|" : "personal|") + cat + "|" + search_query_;
    if (!force_refresh) {
        if (auto it = cached_feeds_.find(cache_key); it != cached_feeds_.end()) {
            current_items_ = it->second;
            lookupProvenance();
            enrichMissingMusicCovers();
            enrichMissingNasCovers();
            source_status_label_->setText("已接入 " + std::to_string(sources.size()) +
                                          " 个实时 MediaSource 插件 · 共 " +
                                          std::to_string(current_items_.size()) + " 项");
            if (sub_mode_ == SubMode::MusicLyrics) {
                const std::string prev_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
                ensureVisibleMusicSelection();
                const std::string next_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
                if (prev_key == next_key) {
                    rebuildSubContent();
                }
            } else {
                if (!has_active_item_ && !current_items_.empty()) {
                    active_item_ = current_items_.front();
                    has_active_item_ = true;
                }
                rebuildSubContent();
            }
            return;
        }
    }

    current_items_.clear();
    if (preserve_detail && has_active_item_) current_items_.push_back(active_item_);
    const uint64_t seq = ++fetch_seq_;
    source_status_label_->setText((force_refresh ? "正在强制刷新 " : "正在异步拉取 ") +
                                  std::to_string(sources.size()) + " 个实时 MediaSource 插件...");

    auto pending = std::make_shared<std::atomic<size_t>>(sources.size());
    auto agg_mutex = std::make_shared<std::mutex>();
    auto agg_items = std::make_shared<std::vector<MediaItem>>();
    auto has_rendered_partial = std::make_shared<std::atomic<bool>>(false);

    for (const auto& src : sources) {
        auto cb = [this, alive = alive_, seq, pending, agg_mutex, agg_items, has_rendered_partial,
                   cache_key, preserve_detail, total = sources.size()](std::vector<MediaItem> items, bool cancelled) {
            bool added = false;
            if (!cancelled && alive->load(std::memory_order_acquire) && !items.empty()) {
                std::lock_guard<std::mutex> lk(*agg_mutex);
                agg_items->insert(agg_items->end(),
                                  std::make_move_iterator(items.begin()),
                                  std::make_move_iterator(items.end()));
                added = true;
            }
            const bool is_last = (pending->fetch_sub(1, std::memory_order_acq_rel) == 1);
            if (is_last || (added && !has_rendered_partial->exchange(true, std::memory_order_acq_rel))) {
                brls::sync([this, alive, seq, agg_mutex, agg_items, cache_key, preserve_detail, total, is_last]() {
                    if (!alive->load(std::memory_order_acquire) || seq != fetch_seq_) {
                        return;
                    }
                    if (is_last) {
                        active_fetches_.clear();
                    }
                    {
                        std::lock_guard<std::mutex> lk(*agg_mutex);
                        current_items_ = *agg_items;
                        if (preserve_detail && has_active_item_ &&
                            std::none_of(current_items_.begin(), current_items_.end(), [this](const MediaItem& item) {
                                return item.id == active_item_.id && item.source_id == active_item_.source_id &&
                                    item.extra.value("business_date", "") == active_item_.extra.value("business_date", "");
                            })) current_items_.push_back(active_item_);
                        std::stable_sort(current_items_.begin(), current_items_.end(), [](const MediaItem& a, const MediaItem& b) {
                            const auto ae = a.extra.is_object() ? a.extra : nlohmann::json::object();
                            const auto be = b.extra.is_object() ? b.extra : nlohmann::json::object();
                            if (ae.value("pinned", false) != be.value("pinned", false)) return ae.value("pinned", false);
                            if (ae.value("business_date", "") != be.value("business_date", ""))
                                return ae.value("business_date", "") > be.value("business_date", "");
                            if (ae.value("priority_rank", 999999) != be.value("priority_rank", 999999))
                                return ae.value("priority_rank", 999999) < be.value("priority_rank", 999999);
                            return a.source_id == b.source_id ? a.id < b.id : a.source_id < b.source_id;
                        });
                    }
                    if (is_last) {
                        cached_feeds_[cache_key] = current_items_;
                        lookupProvenance();
                        enrichMissingMusicCovers();
                        enrichMissingNasCovers();
                    }
                    source_status_label_->setText(
                        (is_last ? "已异步接入 " : "正在加载 ") + std::to_string(total) +
                        " 个实时 MediaSource 插件 · 共 " +
                        std::to_string(current_items_.size()) + " 项");
                    if (sub_mode_ == SubMode::MusicLyrics) {
                        const std::string prev_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
                        ensureVisibleMusicSelection();
                        const std::string next_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
                        if (prev_key == next_key) {
                            rebuildSubContent();
                        }
                    } else {
                        if (!has_active_item_ && !current_items_.empty()) {
                            active_item_ = current_items_.front();
                            has_active_item_ = true;
                        }
                        rebuildSubContent();
                    }
                });
            }
        };
        auto tok = (force_refresh && !curated)
            ? src->fetchItemsPageAsync(cat, search_query_, 1, true, std::move(cb))
            : src->fetchItemsAsync(cat, curated ? std::string{} : search_query_, std::move(cb));
        if (tok) {
            active_fetches_.push_back(std::move(tok));
        }
    }

    if (current_items_.empty()) {
        rebuildSubContent();
    }
}

void MediaWorkbench::loadMoreMedia() {
    if (sub_mode_ != SubMode::Videos || media_loading_more_ || !media_has_more_) {
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    auto sources = kernel.domains().mediaSources();
    sources.erase(std::remove_if(sources.begin(), sources.end(), [](const auto& src) {
        return src->sourceId() == "plugin-ai-curator";
    }), sources.end());
    if (sources.empty()) return;

    media_loading_more_ = true;
    const int next_page = media_page_ + 1;
    const uint64_t seq = fetch_seq_;
    const std::string cache_key = "personal|all|" + search_query_;
    if (source_status_label_) {
        source_status_label_->setText("正在加载第 " + std::to_string(next_page) + " 页影视推荐…");
    }
    rebuildVideosFooter(visible_grid_items_.size());

    more_fetches_.clear();
    auto pending = std::make_shared<std::atomic<size_t>>(sources.size());
    auto agg_mutex = std::make_shared<std::mutex>();
    auto agg_items = std::make_shared<std::vector<MediaItem>>();

    for (const auto& src : sources) {
        auto tok = src->fetchItemsPageAsync(
            "all",
            search_query_,
            next_page,
            false,
            [this, alive = alive_, seq, next_page, cache_key, pending, agg_mutex, agg_items](
                std::vector<MediaItem> items, bool cancelled) {
                if (!cancelled && alive->load(std::memory_order_acquire) && !items.empty()) {
                    std::lock_guard<std::mutex> lk(*agg_mutex);
                    agg_items->insert(agg_items->end(),
                                      std::make_move_iterator(items.begin()),
                                      std::make_move_iterator(items.end()));
                }
                if (pending->fetch_sub(1, std::memory_order_acq_rel) == 1) {
                    brls::sync([this, alive, seq, next_page, cache_key, agg_mutex, agg_items]() {
                        if (!alive->load(std::memory_order_acquire) || seq != fetch_seq_) {
                            return;
                        }
                        media_loading_more_ = false;
                        more_fetches_.clear();
                        size_t added = 0;
                        std::vector<MediaItem> appended_visible;
                        {
                            std::lock_guard<std::mutex> lk(*agg_mutex);
                            for (auto& item : *agg_items) {
                                const bool exists = std::any_of(
                                    current_items_.begin(), current_items_.end(),
                                    [&](const MediaItem& ex) {
                                        return ex.source_id == item.source_id && ex.id == item.id;
                                    });
                                if (!exists) {
                                    if (item.kind != "music" && !isCuratedMedia(item) &&
                                        mediaItemMatchesQuery(item, search_query_)) {
                                        appended_visible.push_back(item);
                                    }
                                    current_items_.push_back(std::move(item));
                                    ++added;
                                }
                            }
                        }
                        if (added > 0) {
                            media_page_ = next_page;
                            media_has_more_ = true;
                            cached_feeds_[cache_key] = current_items_;
                            if (source_status_label_) {
                                source_status_label_->setText(
                                    "已加载至第 " + std::to_string(media_page_) + " 页 · 共 " +
                                    std::to_string(current_items_.size()) + " 项");
                            }
                        } else {
                            media_has_more_ = false;
                            if (source_status_label_) {
                                source_status_label_->setText(
                                    "没有更多新推荐了 · 共 " + std::to_string(current_items_.size()) + " 项");
                            }
                        }
                        if (sub_mode_ == SubMode::Videos) {
                            if (!appended_visible.empty()) {
                                visible_grid_items_.insert(
                                    visible_grid_items_.end(),
                                    std::make_move_iterator(appended_visible.begin()),
                                    std::make_move_iterator(appended_visible.end()));
                                if (stage_grid_) {
                                    stage_grid_->notifyDataChanged();
                                }
                            }
                            rebuildVideosFooter(visible_grid_items_.size());
                        }
                        if (added > 0) {
                            lookupProvenance();
                        }
                    });
                }
            });
        if (tok) {
            more_fetches_.push_back(std::move(tok));
        }
    }
}

void MediaWorkbench::setSubMode(SubMode m) {
    if (sub_mode_ == m) return;
    sub_mode_ = m;
    if (stage_grid_) {
        stage_grid_->setContentOffsetY(0.0f, false);
    }
    if (m == SubMode::PlayerStage && has_active_item_) {
        rebuildTabs();
        rebuildSubContent();
        return;
    }
    fetchCurrentSubMode();
}

void MediaWorkbench::rebuildSubContent() {
    const bool had_focus = focusIsInsideView(stage_grid_);
    if (had_focus) {
        brls::Application::giveFocus(nullptr);
    }
    last_stage_w_ = static_cast<int>(std::round(estimateMediaStageWidth(this, immersive_fullscreen_)));
    last_stage_h_ = static_cast<int>(std::round(estimateMediaStageHeight(this, immersive_fullscreen_)));

    mpv_stage_ = nullptr;
    music_queue_grid_ = nullptr;
    lyrics_scroll_ = nullptr;
    lyric_cards_.clear();
    lyric_orig_labels_.clear();
    lyric_trans_labels_.clear();
    lyric_times_.clear();
    active_lyric_idx_ = -1;
    lyric_tutor_status_label_ = nullptr;
    lyric_tutor_target_label_ = nullptr;
    lyric_tutor_body_label_ = nullptr;
    if ((immersive_fullscreen_ || sub_mode_ == SubMode::MusicLyrics) && stage_grid_) {
        stage_grid_->setContentOffsetY(0.0f, false);
    }
    switch (sub_mode_) {
        case SubMode::Videos: renderVideosView(); break;
        case SubMode::DailyCuration: renderDailyCurationView(); break;
        case SubMode::MusicLyrics: renderMusicLyricsView(); break;
        case SubMode::NasLibrary: renderNasView(); break;
        case SubMode::ImdbDiscover: renderImdbView(); break;
        case SubMode::PlayerStage: renderPlayerStageView(); break;
    }
    if (had_focus && stage_grid_) {
        if (auto* df = stage_grid_->getDefaultFocus()) {
            brls::Application::giveFocus(df);
        }
    }
}

bool MediaWorkbench::matchesCurationScope(const MediaItem& item, bool music) const {
    if (!isCuratedMedia(item)) return false;
    const bool interactive = item.extra.value("curation_mode", "") == "interactive_ephemeral";
    if (music) {
        if (item.kind != "music") return false;
        if (interactive != (music_scope_ == MusicScope::Interactive)) return false;
    } else {
        if (interactive != (curation_filter_ == 2)) return false;
        if (curation_filter_ == 0 && item.kind != "music") return false;
        if (curation_filter_ == 1 && item.kind == "music") return false;
    }
    if (!mediaItemMatchesQuery(item, search_query_)) return false;
    if (archive_all_ || (interactive && curation_date_.empty())) return true;
    std::string date = curation_date_;
    if (date.empty()) {
        for (const auto& candidate : current_items_) {
            if (!isCuratedMedia(candidate)) continue;
            const bool ci = candidate.extra.value("curation_mode", "") == "interactive_ephemeral";
            if (ci != interactive) continue;
            if (music && candidate.kind != "music") continue;
            if (!music && curation_filter_ == 0 && candidate.kind != "music") continue;
            if (!music && curation_filter_ == 1 && candidate.kind == "music") continue;
            date = std::max(date, candidate.extra.value("business_date", ""));
        }
    }
    return date.empty() || item.extra.value("business_date", "") == date;
}

std::vector<MediaItem> MediaWorkbench::visibleMusicItems() const {
    std::vector<MediaItem> items;
    for (const auto& item : current_items_) {
        if (item.kind != "music" || !mediaItemMatchesQuery(item, search_query_)) continue;
        if (music_scope_ == MusicScope::Personal) {
            if (isCuratedMedia(item)) continue;
        } else if (!matchesCurationScope(item, true)) continue;
        items.push_back(item);
    }
    return items;
}

void MediaWorkbench::ensureVisibleMusicSelection() {
    if (sub_mode_ != SubMode::MusicLyrics) return;
    const auto vis = visibleMusicItems();
    if (vis.empty()) return;
    const bool active_in_vis = has_active_item_ &&
        std::any_of(vis.begin(), vis.end(), [this](const MediaItem& it) {
            return it.id == active_item_.id && it.source_id == active_item_.source_id;
        });
    if (!active_in_vis) {
        selectItem(vis.front(), false);
    }
}

brls::Box* MediaWorkbench::buildScopeControlsBox(bool music) {
    auto* wrap = new brls::Box(brls::Axis::COLUMN);
    wrap->setWidthPercentage(100);

    auto* row = new brls::Box(brls::Axis::ROW);
    row->setMarginBottom(10);
    ui_comfort::enableFlexWrap(row, 6.0f);
    const std::vector<std::string> labels = music
        ? std::vector<std::string>{"个人曲库", "每日音乐", "交互推荐"}
        : std::vector<std::string>{"每日音乐", "每日视频", "交互推荐"};
    for (int i = 0; i < 3; ++i) {
        auto* button = ui_comfort::makeFilterChip(labels[i], [this, alive = alive_, music, i]() {
            brls::sync([this, alive, music, i]() {
                if (!alive->load(std::memory_order_acquire)) return;
                last_loaded_stream_key_.clear();
                if (music) music_scope_ = static_cast<MusicScope>(i);
                else curation_filter_ = i;
                curation_date_.clear();
                archive_all_ = false;
                fetchCurrentSubMode();
            });
        }, music ? static_cast<int>(music_scope_) == i : curation_filter_ == i, "domain/media", 32.0f);
        button->setMarginRight(7);
        row->addView(button);
    }
    wrap->addView(row);
    if (!music || music_scope_ != MusicScope::Personal) {
        std::string latest;
        for (const auto& item : current_items_) {
            if (!isCuratedMedia(item)) continue;
            const bool interactive = item.extra.value("curation_mode", "") == "interactive_ephemeral";
            if (interactive != (music ? music_scope_ == MusicScope::Interactive : curation_filter_ == 2)) continue;
            if ((music || curation_filter_ == 0) && item.kind != "music") continue;
            if (!music && curation_filter_ == 1 && item.kind == "music") continue;
            latest = std::max(latest, item.extra.value("business_date", ""));
        }
        auto* dates = new brls::Box(brls::Axis::ROW);
        dates->setMarginBottom(12);
        dates->setAlignItems(brls::AlignItems::CENTER);
        ui_comfort::enableFlexWrap(dates, 6.0f);
        const bool interactive_scope = music ? music_scope_ == MusicScope::Interactive : curation_filter_ == 2;
        dates->addView(ui::makeButton(archive_all_ ? "全部归档 · 选择日期" :
            (interactive_scope && curation_date_.empty()) ? "全部交互会话 · 选择日期" :
            (curation_date_.empty() ? (latest.empty() ? "暂无业务日 · 选择日期" : latest + " · 最新") : curation_date_),
            [this]() { promptRehydrateOrDateFilter(); }, false, 32.0f));
        auto* latest_button = ui_comfort::makeFilterChip(interactive_scope ? "全部会话" : "最新", [this]() {
            last_loaded_stream_key_.clear();
            curation_date_.clear();
            archive_all_ = false;
            enrichMissingMusicCovers();
            const std::string prev_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
            ensureVisibleMusicSelection();
            const std::string next_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
            if (prev_key == next_key) {
                rebuildSubContent();
            }
        }, !archive_all_ && curation_date_.empty(), "domain/media", 32.0f);
        latest_button->setMarginLeft(8);
        dates->addView(latest_button);
        auto* all = ui_comfort::makeFilterChip("全部归档", [this]() {
            last_loaded_stream_key_.clear();
            archive_all_ = true;
            enrichMissingMusicCovers();
            const std::string prev_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
            ensureVisibleMusicSelection();
            const std::string next_key = has_active_item_ ? (active_item_.source_id + ":" + active_item_.id) : "";
            if (prev_key == next_key) {
                rebuildSubContent();
            }
        }, archive_all_, "domain/media", 32.0f);
        all->setMarginLeft(8);
        dates->addView(all);
        wrap->addView(dates);
    }
    return wrap;
}

void MediaWorkbench::renderDailyCurationView() {
    auto* hdr = new brls::Box(brls::Axis::COLUMN);
    hdr->setWidthPercentage(100);
    hdr->addView(buildScopeControlsBox(false));
    if (curation_filter_ != 1) {
        auto* play = ui::makeButton("播放当前音乐", [this]() {
            const auto first = std::find_if(current_items_.begin(), current_items_.end(), [this](const MediaItem& item) {
                return item.kind == "music" && matchesCurationScope(item, false);
            });
            if (first == current_items_.end()) {
                brls::Application::notify("当前筛选暂无音乐");
                return;
            }
            const auto item = *first;
            last_loaded_stream_key_.clear();
            selectItem(item, true);
            playVisibleMusic();
        }, true, 36.0f);
        play->setMarginBottom(12);
        hdr->addView(play);
    }
    renderVideosView(hdr);
}

void MediaWorkbench::refreshMediaGridInPlace() {
    if (sub_mode_ == SubMode::Videos || sub_mode_ == SubMode::DailyCuration) {
        visible_grid_items_.clear();
        for (const auto& item : current_items_) {
            if (sub_mode_ == SubMode::DailyCuration) {
                if (!matchesCurationScope(item, false)) continue;
            } else if (item.kind == "music" || isCuratedMedia(item)) {
                continue;
            }
            if (!mediaItemMatchesQuery(item, search_query_)) continue;
            visible_grid_items_.push_back(item);
        }
        if (stage_grid_) {
            stage_grid_->reloadData(false);
        }
        return;
    }
    if (sub_mode_ == SubMode::NasLibrary) {
        visible_grid_items_.clear();
        for (const auto& item : current_items_) {
            if (!isCuratedMedia(item) && (item.source_id == "plugin-fnos-nas" || item.kind == "nas")) {
                if (mediaItemMatchesQuery(item, search_query_)) {
                    visible_grid_items_.push_back(item);
                }
            }
        }
        if (stage_grid_) {
            stage_grid_->reloadData(false);
        }
        return;
    }
    if (sub_mode_ == SubMode::MusicLyrics) {
        visible_music_items_ = visibleMusicItems();
        if (music_queue_grid_) {
            music_queue_grid_->reloadData(false);
        }
    }
}

void MediaWorkbench::playVisibleMusic(double start_sec) {
    auto visible = visibleMusicItems();
    const auto selected = std::find_if(visible.begin(), visible.end(), [this](const MediaItem& item) {
        return item.id == active_item_.id && item.source_id == active_item_.source_id;
    });
    if (selected == visible.end()) {
        brls::Application::notify("请从当前筛选列表选择曲目");
        return;
    }
    const int index = static_cast<int>(std::distance(visible.begin(), selected));
    std::vector<PlaybackTrack> queue;
    for (size_t i = 0; i < visible.size(); ++i) {
        PlaybackTrack track;
        track.item = static_cast<int>(i) == index ? active_item_ : visible[i];
        track.options = buildPlaybackOptions(track.item);
        if (static_cast<int>(i) == index) {
            track.lyrics = active_lyrics_;
            track.chapters = active_chapters_;
            if (start_sec >= 0.0) track.options.start_seconds = start_sec;
        }
        queue.push_back(std::move(track));
    }
    PlaybackCoordinator::instance().playMusicQueue("host.media_workbench", std::move(queue), index);
}

void MediaWorkbench::lookupProvenance() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;
    nlohmann::json items = nlohmann::json::array();
    for (const auto& item : current_items_) {
        if (isCuratedMedia(item)) continue;
        items.push_back({{"id", item.source_id + ":" + item.id}, {"source_id", item.source_id},
            {"creator", item.creator}, {"kind", item.kind}, {"title", item.title},
            {"play_url", item.play_url}, {"extra", item.extra}});
    }
    if (items.empty()) return;
    if (provenance_request_) provenance_request_->cancel();
    auto request = CancelToken::create();
    provenance_request_ = request;
    const auto sequence = fetch_seq_;
    loadUiInBackground(kernel.uiTasks(), "media.provenance", request,
        [items = std::move(items)](const auto& cancel) {
            return HostKernel::instance().capabilities().callCancellable("recommend.feed.v1",
                {{"action", "lookup_provenance"}, {"items", items}}, cancel);
        },
        [this, alive = alive_, sequence](CapabilityResult result) {
            if (!alive->load(std::memory_order_acquire) || sequence != fetch_seq_ || !result.ok) return;
            const auto matches = result.data.value("matches", nlohmann::json::array());
            if (!matches.is_array()) return;
            bool changed_any = false;
            for (auto& item : current_items_) {
                for (const auto& match : matches) {
                    if (match.value("id", "") != item.source_id + ":" + item.id) continue;
                    const auto date = match.value("previously_recommended_on", "");
                    if (!date.empty()) {
                        item.extra["previously_recommended_on"] = date;
                        changed_any = true;
                        if (active_item_.id == item.id && active_item_.source_id == item.source_id)
                            active_item_.extra["previously_recommended_on"] = date;
                    }
                }
            }
            if (changed_any) {
                refreshMediaGridInPlace();
            }
        }, []() {});
}

void MediaWorkbench::enrichMissingMusicCovers() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("media.source.v1")) return;

    nlohmann::json batch_items = nlohmann::json::array();
    std::unordered_map<std::string, bool> seen_ids;
    auto collectItem = [&](const MediaItem& item) {
        if (batch_items.size() >= 150 || !item.cover_url.empty()) return;
        const auto am = extractAppleMusicLookup(item);
        if (!am.is_apple_music || am.song_id.empty()) return;
        if (!seen_ids.emplace(am.song_id, true).second) return;
        batch_items.push_back({
            {"id", am.song_id},
            {"storefront", am.storefront},
            {"url", am.url},
        });
    };

    const bool music_mode = (sub_mode_ == SubMode::MusicLyrics);
    for (const auto& item : current_items_) {
        if (matchesCurationScope(item, music_mode)) {
            collectItem(item);
        }
    }
    for (const auto& item : current_items_) {
        collectItem(item);
    }
    if (batch_items.empty()) return;

    if (cover_enrich_request_) cover_enrich_request_->cancel();
    auto request = CancelToken::create();
    cover_enrich_request_ = request;
    const auto sequence = fetch_seq_;
    loadUiInBackground(kernel.uiTasks(), "media.music_covers", request,
        [batch_items = std::move(batch_items)](const auto& cancel) {
            if (cancel && cancel->isCancelled()) return CapabilityResult{};
            auto& caps = HostKernel::instance().capabilities();
            nlohmann::json req{{"action", "batch_metadata"}, {"items", batch_items}};
            auto res = caps.callProvider("plugin-music", "media.source.v1", req);
            if (!res.ok && res.error.find("not found") != std::string::npos) {
                res = caps.call("media.source.v1", req);
            }
            return res;
        },
        [this, alive = alive_, sequence](CapabilityResult result) {
            if (!alive->load(std::memory_order_acquire) || sequence != fetch_seq_ ||
                !result.ok || !result.data.is_object()) {
                return;
            }
            if (!result.data.contains("tracks")) return;
            const auto& tracks = result.data["tracks"];
            if ((!tracks.is_object() && !tracks.is_array()) || tracks.empty()) return;

            struct CoverMeta {
                std::string cover_url;
                double duration_sec = 0.0;
                std::string quality_label;
            };
            std::unordered_map<std::string, CoverMeta> by_id;
            auto ingestTrackJson = [&](const std::string& fallback_id, const nlohmann::json& t) {
                if (!t.is_object()) return;
                std::string id = t.value("id", fallback_id);
                if (id.empty()) return;
                CoverMeta cm;
                cm.cover_url = t.value("cover_url", "");
                cm.duration_sec = t.value("duration_sec", 0.0);
                cm.quality_label = t.value("quality_label", "");
                if (!cm.cover_url.empty() || cm.duration_sec > 0.0 || !cm.quality_label.empty()) {
                    by_id[id] = std::move(cm);
                }
            };
            if (tracks.is_object()) {
                for (const auto& kv : tracks.items()) {
                    ingestTrackJson(kv.key(), kv.value());
                }
            } else {
                for (const auto& t : tracks) {
                    ingestTrackJson("", t);
                }
            }
            if (by_id.empty()) return;

            auto applyToItem = [&](MediaItem& item) -> bool {
                const auto am = extractAppleMusicLookup(item);
                if (!am.is_apple_music || am.song_id.empty()) return false;
                auto it = by_id.find(am.song_id);
                if (it == by_id.end()) return false;
                bool changed = false;
                if (item.cover_url.empty() && !it->second.cover_url.empty()) {
                    item.cover_url = it->second.cover_url;
                    changed = true;
                }
                if (item.duration_sec <= 0.0 && it->second.duration_sec > 0.0) {
                    item.duration_sec = it->second.duration_sec;
                    changed = true;
                }
                if (item.quality_label.empty() && !it->second.quality_label.empty()) {
                    item.quality_label = it->second.quality_label;
                    changed = true;
                }
                return changed;
            };

            bool updated_any = false;
            bool active_changed = false;
            for (auto& item : current_items_) {
                if (applyToItem(item)) updated_any = true;
            }
            if (has_active_item_) {
                if (applyToItem(active_item_)) {
                    updated_any = true;
                    active_changed = true;
                }
            }
            for (auto& kv : cached_feeds_) {
                for (auto& item : kv.second) {
                    applyToItem(item);
                }
            }
            if (updated_any) {
                if (sub_mode_ == SubMode::MusicLyrics && active_changed) {
                    rebuildSubContent();
                } else {
                    refreshMediaGridInPlace();
                }
            }
        }, []() {});
}

void MediaWorkbench::enrichMissingNasCovers() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("imdb.discover.v1")) return;

    auto lookupKeyForNas = [](const MediaItem& item) -> std::string {
        if (item.source_id != "plugin-fnos-nas" || item.kind != "nas" ||
            item.id.rfind("demo-", 0) == 0) {
            return {};
        }
        std::string q;
        if (item.extra.is_object()) {
            q = trimSpaces(item.extra.value("series", ""));
        }
        if (q.empty()) {
            q = trimSpaces(item.title);
        }
        return q;
    };

    bool applied_cached = false;
    std::vector<std::string> pending_queries;
    std::unordered_map<std::string, bool> queued;

    for (auto& item : current_items_) {
        if (!item.cover_url.empty()) continue;
        std::string q = lookupKeyForNas(item);
        if (q.empty()) continue;
        std::string norm_q = toLowerCopy(q);
        auto cit = nas_imdb_cover_cache_.find(norm_q);
        if (cit != nas_imdb_cover_cache_.end()) {
            if (!cit->second.empty()) {
                item.cover_url = cit->second;
                applied_cached = true;
            }
            continue;
        }
        if (pending_queries.size() < 16 && queued.emplace(norm_q, true).second) {
            pending_queries.push_back(q);
        }
    }
    if (applied_cached) {
        refreshMediaGridInPlace();
    }
    if (pending_queries.empty()) return;

    if (nas_cover_enrich_request_) nas_cover_enrich_request_->cancel();
    auto request = CancelToken::create();
    nas_cover_enrich_request_ = request;
    const auto sequence = fetch_seq_;

    loadUiInBackground(
        kernel.uiTasks(),
        "media.nas_covers",
        request,
        [pending_queries = std::move(pending_queries)](const auto& cancel) {
            std::unordered_map<std::string, std::string> resolved;
            auto& caps = HostKernel::instance().capabilities();
            for (const auto& q : pending_queries) {
                if (cancel && cancel->isCancelled()) break;
                auto res = caps.callCancellable(
                    "imdb.discover.v1",
                    {{"action", "search"}, {"query", q}, {"limit", 3}},
                    cancel);
                std::string found_poster;
                if (res.ok && res.data.is_object() &&
                    res.data.contains("items") && res.data["items"].is_array()) {
                    for (const auto& it : res.data["items"]) {
                        if (!it.is_object()) continue;
                        std::string p = it.value("poster_url", "");
                        if (!p.empty()) {
                            found_poster = std::move(p);
                            break;
                        }
                    }
                }
                resolved[toLowerCopy(q)] = std::move(found_poster);
            }
            CapabilityResult out;
            out.ok = true;
            out.data = resolved;
            return out;
        },
        [this, alive = alive_, sequence](CapabilityResult result) {
            if (!alive->load(std::memory_order_acquire) || sequence != fetch_seq_ ||
                !result.ok || !result.data.is_object()) {
                return;
            }
            for (const auto& kv : result.data.items()) {
                if (kv.value().is_string()) {
                    nas_imdb_cover_cache_[kv.key()] = kv.value().get<std::string>();
                }
            }
            auto applyCover = [&](MediaItem& item) -> bool {
                if (!item.cover_url.empty() || item.source_id != "plugin-fnos-nas" ||
                    item.kind != "nas") {
                    return false;
                }
                std::string q = item.extra.is_object() ? trimSpaces(item.extra.value("series", "")) : "";
                if (q.empty()) q = trimSpaces(item.title);
                auto it = nas_imdb_cover_cache_.find(toLowerCopy(q));
                if (it == nas_imdb_cover_cache_.end() || it->second.empty()) return false;
                item.cover_url = it->second;
                if (item.extra.is_object() && item.extra.contains("episodes") &&
                    item.extra["episodes"].is_array()) {
                    for (auto& ep_j : item.extra["episodes"]) {
                        if (ep_j.is_object() && ep_j.value("cover_url", "").empty()) {
                            ep_j["cover_url"] = it->second;
                        }
                    }
                }
                return true;
            };

            bool updated = false;
            for (auto& item : current_items_) {
                if (applyCover(item)) updated = true;
            }
            for (auto& kv : cached_feeds_) {
                for (auto& item : kv.second) {
                    applyCover(item);
                }
            }
            if (updated) {
                refreshMediaGridInPlace();
            }
        },
        []() {});
}

void MediaWorkbench::rebuildVideosFooter(size_t total_count) {
    if (!stage_grid_) return;
    if (total_count == 0 || sub_mode_ != SubMode::Videos) {
        stage_grid_->setFooterView(nullptr);
        return;
    }

    auto* footer = ui::makeCard("desktop/soft", 12.0f, 10.0f);
    footer->setMarginTop(4);
    footer->setMarginBottom(16);
    auto* f_row = new brls::Box(brls::Axis::ROW);
    f_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    f_row->setAlignItems(brls::AlignItems::CENTER);

    const std::string more_lbl = media_loading_more_
        ? "⏳ 正在加载下一页…"
        : (media_has_more_
               ? ("⬇ 加载下一页 (第 " + std::to_string(media_page_ + 1) + " 页 · 当前 " + std::to_string(total_count) + " 条)")
               : "✓ 已到底部 · 点击重新检测");
    auto* more_btn = ui::makeButton(more_lbl, [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire) || media_loading_more_) return;
            media_has_more_ = true;
            loadMoreMedia();
        });
    }, media_has_more_ && !media_loading_more_, 36.0f);
    more_btn->setGrow(1.0f);
    more_btn->setMarginRight(10);
    f_row->addView(more_btn);

    auto* ref_btn = ui::makeButton("↻ 换一批 / 刷新", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                refresh(true);
            }
        });
    }, false, 36.0f);
    ref_btn->setMarginRight(10);
    f_row->addView(ref_btn);

    auto* top_btn = ui::makeButton("⬆ 回到顶部", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire) && stage_grid_) {
                stage_grid_->setContentOffsetY(0.0f, false);
            }
        });
    }, false, 36.0f);
    f_row->addView(top_btn);

    footer->addView(f_row);
    stage_grid_->setFooterView(footer);
}

void MediaWorkbench::renderVideosView(brls::Box* header_prefix) {
    if (!stage_grid_) {
        if (header_prefix) {
            delete header_prefix;
        }
        return;
    }

    const float stage_w = estimateMediaStageWidth(this, false);
    const int cols = computeVideoGridColumns(stage_w);
    constexpr float kGap = 14.0f;
    const float card_w =
        std::max(220.0f, (stage_w - kGap * static_cast<float>(cols - 1)) / static_cast<float>(cols));
    // Subtract card padding (14*2) and inner thumb padding (10*2) for true 16:9 cover aspect ratio
    const float inner_cover_w = std::max(160.0f, card_w - 48.0f);
    active_video_cover_h_ = std::round(inner_cover_w * 9.0f / 16.0f);

    visible_grid_items_.clear();
    visible_imdb_items_.clear();
    for (const auto& item : current_items_) {
        if (sub_mode_ == SubMode::DailyCuration) {
            if (!matchesCurationScope(item, false)) continue;
        } else if (item.kind == "music" || isCuratedMedia(item)) {
            continue;
        }
        if (!mediaItemMatchesQuery(item, search_query_)) continue;
        visible_grid_items_.push_back(item);
    }

    auto* empty_lbl = ui::makeLabel("当前筛选暂无媒体，试试切换分类、日期或全部归档", 15.0f, "font/grey");
    empty_lbl->setMarginTop(8);
    empty_lbl->setMarginBottom(12);

    stage_grid_->setColSpace(kGap);
    stage_grid_->setEstimatedRowSpace(14.0f);
    stage_grid_->setEstimatedRowHeight(active_video_cover_h_ + 186.0f);
    stage_grid_->setHeaderView(header_prefix);
    stage_grid_->setEmptyView(empty_lbl);
    rebuildVideosFooter(visible_grid_items_.size());
    stage_grid_->setSpanCount(cols);
    stage_grid_->reloadData(false);
}

void MediaWorkbench::renderMusicLyricsView() {
    if (!stage_grid_) return;

    auto* static_root = new brls::Box(brls::Axis::COLUMN);
    static_root->setWidthPercentage(100);
    static_root->addView(buildScopeControlsBox(true));

    const float stage_w = estimateMediaStageWidth(this, false);
    const float stage_h = estimateMediaStageHeight(this, false);
    const float scope_overhead = (music_scope_ == MusicScope::Personal) ? 46.0f : 90.0f;
    const float split_h = std::max(440.0f, stage_h - 154.0f - scope_overhead);
    const float art_side = std::clamp(std::round(stage_w * 0.085f), 84.0f, 118.0f);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);
    split->setHeight(split_h);

    // =========================================================================
    // Left Column: Compact Now-Playing Hero + Full-Height Independent Queue Grid
    // (Fills split_h completely so scrolling lyrics NEVER exposes empty black space)
    // =========================================================================
    auto* left_card = ui::makeCard("desktop/surface", 16.0f, 14.0f);
    left_card->setWidthPercentage(37);
    left_card->setHeight(split_h);
    left_card->setShrink(0.0f);
    left_card->setMarginRight(16);

    auto* hero_row = new brls::Box(brls::Axis::ROW);
    hero_row->setWidthPercentage(100);
    hero_row->setShrink(0.0f);
    hero_row->setAlignItems(brls::AlignItems::CENTER);

    auto* art = ui::makeCard("desktop/active", 6.0f, 12.0f);
    art->setDimensions(art_side + 12.0f, art_side + 12.0f);
    art->setShrink(0.0f);
    art->setMarginRight(12);
    art->setJustifyContent(brls::JustifyContent::CENTER);
    art->setAlignItems(brls::AlignItems::CENTER);
    if (!active_item_.cover_url.empty()) {
        auto* cover = new CachedImageView(&ImageCache::instance());
        cover->setDimensions(art_side, art_side);
        cover->setCornerRadius(9.0f);
        cover->setScalingType(brls::ImageScalingType::FILL);
        cover->setImageUrl(active_item_.cover_url, extractHeadersFromItem(active_item_));
        art->addView(cover);
    } else {
        art->addView(ui::makeLabel("♪", 26.0f, "brls/accent"));
    }
    hero_row->addView(art);

    auto* hero_meta = new brls::Box(brls::Axis::COLUMN);
    hero_meta->setGrow(1.0f);
    hero_meta->setShrink(1.0f);
    hero_meta->setMinWidth(0);

    auto* track_title = ui::makeLabel(
        active_item_.title.empty() ? "请选择曲目" : active_item_.title,
        17.5f, "brls/text", 1.32f);
    hero_meta->addView(track_title);

    if (!active_item_.creator.empty()) {
        auto* artist = ui::makeLabel(active_item_.creator, 13.5f, "font/grey", 1.28f);
        artist->setMarginTop(4);
        hero_meta->addView(artist);
    }

    auto* meta_pills = new brls::Box(brls::Axis::ROW);
    meta_pills->setMarginTop(7);
    ui_comfort::enableFlexWrap(meta_pills, 4.0f);
    if (!active_item_.quality_label.empty()) {
        auto* qp = ui::makePill(active_item_.quality_label, "status/ok_bg", "status/ok_fg");
        qp->setMarginRight(6);
        meta_pills->addView(qp);
    }
    if (active_item_.duration_sec > 0.0) {
        auto* dp = ui::makePill(formatDuration(active_item_.duration_sec), "desktop/soft", "font/grey");
        meta_pills->addView(dp);
    }
    if (!meta_pills->getChildren().empty()) {
        hero_meta->addView(meta_pills);
    }
    hero_row->addView(hero_meta);
    left_card->addView(hero_row);

    auto* controls = new brls::Box(brls::Axis::ROW);
    controls->setShrink(0.0f);
    controls->setMarginTop(12);
    ui_comfort::enableFlexWrap(controls, 6.0f);
    controls->addView(ui::makeButton("上一曲", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                selectAdjacentMusicTrack(-1);
            }
        });
    }, false, 34.0f));

    auto* play_b = ui::makeButton("播放 / 暂停", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        const auto visible = visibleMusicItems();
        if (std::none_of(visible.begin(), visible.end(), [this](const MediaItem& item) {
            return item.id == active_item_.id && item.source_id == active_item_.source_id;
        })) {
            brls::Application::notify("请从当前筛选列表选择曲目");
            return;
        }
        const auto coord_st = PlaybackCoordinator::instance().state();
        const bool same_coord_track =
            coord_st.active_role != PlaybackRole::None &&
            coord_st.active_track.item.id == active_item_.id &&
            coord_st.active_track.item.source_id == active_item_.source_id;
        if (same_coord_track && (!MPVCore::instance().state().current_url.empty() || !coord_st.mpv.stopped)) {
            PlaybackCoordinator::instance().playPause();
            return;
        }
        if (isPlayableOrResolvableItem(active_item_)) {
            auto opts = buildPlaybackOptions(active_item_);
            last_loaded_stream_key_ =
                active_item_.source_id + ":" + active_item_.id + "|" + opts.url + "|" + opts.audio_url;
            playVisibleMusic();
            return;
        }
        if (!MPVCore::instance().state().current_url.empty() ||
            coord_st.active_role != PlaybackRole::None) {
            PlaybackCoordinator::instance().playPause();
        } else {
            brls::Application::notify("当前曲目暂不可播放: " + active_item_.title);
        }
    }, true, 34.0f);
    play_b->setMarginLeft(7);
    controls->addView(play_b);

    auto* next_b = ui::makeButton("下一曲", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                selectAdjacentMusicTrack(1);
            }
        });
    }, false, 34.0f);
    next_b->setMarginLeft(7);
    controls->addView(next_b);

    auto* ai_b = ui::makeButton("精讲本句", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (active_lyrics_.empty()) {
            brls::Application::notify("当前曲目暂无可精讲的歌词");
            return;
        }
        const int target_i = (selected_lyric_idx_ >= 0 &&
                              static_cast<size_t>(selected_lyric_idx_) < active_lyrics_.size())
                                 ? selected_lyric_idx_
                                 : (active_lyric_idx_ >= 0 ? active_lyric_idx_ : 0);
        explainLyricLineWithAi(static_cast<size_t>(target_i));
    }, false, 34.0f);
    ai_b->setMarginLeft(7);
    controls->addView(ai_b);
    left_card->addView(controls);

    appendCuratedMetadataBlock(left_card, active_item_);

    visible_music_items_ = visibleMusicItems();
    if (visible_music_items_.empty()) {
        auto* empty_box = new brls::Box(brls::Axis::COLUMN);
        empty_box->setGrow(1.0f);
        empty_box->setJustifyContent(brls::JustifyContent::CENTER);
        empty_box->addView(ui::makeLabel(
            "当前范围暂无曲目，可切换个人曲库、每日音乐或交互推荐", 14.0f, "font/grey"));
        left_card->addView(empty_box);
    } else {
        auto* queue_hdr = ui::makeLabel(
            "曲库与 AI 策展列表 (" + std::to_string(visible_music_items_.size()) + " 首)",
            14.5f, "brls/text");
        queue_hdr->setShrink(0.0f);
        queue_hdr->setMarginTop(12);
        queue_hdr->setMarginBottom(6);
        left_card->addView(queue_hdr);

        class MusicQueueDataSource final : public RecyclingGridDataSource {
        public:
            explicit MusicQueueDataSource(MediaWorkbench* owner) : owner_(owner) {}

            size_t getItemCount() override {
                return owner_ ? owner_->visible_music_items_.size() : 0;
            }

            RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) override {
                if (!owner_ || !recycler) return nullptr;
                auto* cell = static_cast<MusicTrackRowCell*>(recycler->dequeueReusableCell("music_track"));
                if (!cell || index >= owner_->visible_music_items_.size()) return cell;
                auto* wb = owner_;
                auto alive = wb->alive_;
                const MediaItem tr = wb->visible_music_items_[index];
                const bool selected =
                    (tr.id == wb->active_item_.id && tr.source_id == wb->active_item_.source_id);
                cell->bind(tr, selected, [wb, alive, tr]() {
                    brls::sync([wb, alive, tr]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        const auto coord_st = PlaybackCoordinator::instance().state();
                        if (coord_st.active_role == PlaybackRole::BackgroundMusic &&
                            coord_st.active_track.item.id == tr.id &&
                            coord_st.active_track.item.source_id == tr.source_id &&
                            !MPVCore::instance().state().current_url.empty()) {
                            PlaybackCoordinator::instance().playPause();
                            return;
                        }
                        wb->last_loaded_stream_key_.clear();
                        wb->selectItem(tr, true);
                        if (isPlayableOrResolvableItem(tr)) {
                            auto opts = buildPlaybackOptions(wb->active_item_);
                            wb->last_loaded_stream_key_ =
                                wb->active_item_.source_id + ":" + wb->active_item_.id + "|" +
                                opts.url + "|" + opts.audio_url;
                            wb->playVisibleMusic();
                        }
                    });
                });
                return cell;
            }

        private:
            MediaWorkbench* owner_ = nullptr;
        };

        music_queue_grid_ = new RecyclingGrid();
        music_queue_grid_->setWidthPercentage(100);
        music_queue_grid_->setGrow(1.0f);
        music_queue_grid_->setShrink(1.0f);
        music_queue_grid_->setMinHeight(120.0f);
        music_queue_grid_->setSpanCount(1);
        music_queue_grid_->setColSpace(0.0f);
        music_queue_grid_->setEstimatedRowHeight(68.0f);
        music_queue_grid_->setEstimatedRowSpace(8.0f);
        music_queue_grid_->setPreFetchLine(3);
        music_queue_grid_->registerCell("music_track", []() { return new MusicTrackRowCell(); });
        music_queue_grid_->setDataSource(new MusicQueueDataSource(this), true);
        left_card->addView(music_queue_grid_);
    }
    split->addView(left_card);

    // =========================================================================
    // Right Column: Unified Fluid-Cloud Lyrics Sheet + AI Translation & Tutor Window
    // =========================================================================
    auto* right_col = new brls::Box(brls::Axis::COLUMN);
    right_col->setGrow(1.0f);
    right_col->setShrink(1.0f);
    right_col->setMinWidth(0);
    right_col->setHeight(split_h);

    auto* lyrics_card = ui::makeCard("desktop/surface", 16.0f, 14.0f);
    lyrics_card->setWidthPercentage(100);
    lyrics_card->setGrow(1.0f);
    lyrics_card->setShrink(1.0f);
    lyrics_card->setMinHeight(180.0f);

    const std::vector<LyricLine>& lines = active_lyrics_;
    const bool any_translated = std::any_of(lines.begin(), lines.end(), [](const LyricLine& l) {
        return !l.translation.empty() && l.translation != l.original;
    });

    auto* lyr_hdr = new brls::Box(brls::Axis::ROW);
    lyr_hdr->setWidthPercentage(100);
    lyr_hdr->setShrink(0.0f);
    lyr_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    lyr_hdr->setAlignItems(brls::AlignItems::CENTER);
    lyr_hdr->setMarginBottom(10);
    ui_comfort::enableFlexWrap(lyr_hdr, 6.0f);

    auto* lyr_hdr_left = new brls::Box(brls::Axis::ROW);
    lyr_hdr_left->setAlignItems(brls::AlignItems::CENTER);
    auto* lyr_title = ui::makeLabel("流体云歌词", 17.5f, "brls/text");
    lyr_title->setMarginRight(10);
    lyr_hdr_left->addView(lyr_title);

    std::string status_pill_txt = lines.empty()
                                      ? "暂无歌词"
                                      : (lyric_translating_
                                             ? "AI 正在补全中文…"
                                             : (any_translated ? "双语同步就绪" : "原文同步 · 可 AI 补译"));
    const char* status_bg = lyric_translating_
                                ? "status/info_bg"
                                : (any_translated ? "status/ok_bg" : "desktop/soft");
    const char* status_fg = lyric_translating_
                                ? "brls/accent"
                                : (any_translated ? "status/ok_fg" : "font/grey");
    lyr_hdr_left->addView(ui::makePill(status_pill_txt, status_bg, status_fg));
    lyr_hdr->addView(lyr_hdr_left);

    auto* lyr_hdr_right = new brls::Box(brls::Axis::ROW);
    lyr_hdr_right->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(lyr_hdr_right, 5.0f);
    for (const auto& [mode_id, mode_lbl] : std::vector<std::pair<std::string, std::string>>{
             {"bilingual", "双语"}, {"original", "原文"}, {"translation", "译文"}}) {
        auto* chip = ui_comfort::makeFilterChip(
            mode_lbl,
            [this, alive = alive_, mode_id]() {
                brls::sync([this, alive, mode_id]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    lyrics_display_mode_ = mode_id;
                    rebuildSubContent();
                });
            },
            lyrics_display_mode_ == mode_id,
            "domain/media",
            28.0f);
        chip->setMarginRight(5);
        lyr_hdr_right->addView(chip);
    }

    auto* tr_btn = ui::makeButton(
        lyric_translating_ ? "翻译中…" : "AI 补译中文",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                triggerAiLyricsTranslate();
            }
        },
        !any_translated && !lines.empty(),
        29.0f);
    lyr_hdr_right->addView(tr_btn);
    lyr_hdr->addView(lyr_hdr_right);
    lyrics_card->addView(lyr_hdr);

    // Unified One-Piece Fluid-Cloud Sheet (no separate boxy borders per line)
    auto* fluid_sheet_wrap = ui::makeCard("desktop/soft", 8.0f, 12.0f);
    fluid_sheet_wrap->setWidthPercentage(100);
    fluid_sheet_wrap->setGrow(1.0f);
    fluid_sheet_wrap->setShrink(1.0f);
    fluid_sheet_wrap->setMinHeight(120.0f);
    fluid_sheet_wrap->setBorderThickness(0);

    lyrics_scroll_ = new brls::ScrollingFrame();
    lyrics_scroll_->setWidthPercentage(100);
    lyrics_scroll_->setGrow(1.0f);
    lyrics_scroll_->setShrink(1.0f);
    lyrics_scroll_->setMinHeight(100.0f);

    auto* lyrics_sheet = new brls::Box(brls::Axis::COLUMN);
    lyrics_sheet->setWidthPercentage(100);
    lyrics_sheet->setPadding(6, 6, 18, 6);

    if (lines.empty()) {
        auto* empty_lyr = new brls::Box(brls::Axis::COLUMN);
        empty_lyr->setPadding(28, 18, 28, 18);
        empty_lyr->addView(ui::makeLabel(
            "当前曲目暂无时间轴歌词（纯音乐或未收录）", 15.5f, "font/grey", 1.45f));
        lyrics_sheet->addView(empty_lyr);
    } else {
        const double pos_sec = MPVCore::instance().state().position_sec;
        int initial_idx = -1;
        for (size_t i = 0; i < lines.size(); ++i) {
            if (pos_sec >= lines[i].time_sec) {
                initial_idx = static_cast<int>(i);
            } else {
                break;
            }
        }
        active_lyric_idx_ = initial_idx;

        for (size_t i = 0; i < lines.size(); ++i) {
            const bool active_line = (static_cast<int>(i) == initial_idx);
            const bool selected_line = (static_cast<int>(i) == selected_lyric_idx_);

            auto* row = new brls::Box(brls::Axis::COLUMN);
            row->setWidthPercentage(100);
            row->setPadding(9, 14, 9, 14);
            row->setCornerRadius(10.0f);
            row->setHighlightCornerRadius(10.0f);
            row->setBackgroundColor(active_line ? ui::color("desktop/active") : nvgRGBA(0, 0, 0, 0));
            if (selected_line && !active_line) {
                row->setBorderThickness(1.0f);
                row->setBorderColor(ui::color("brls/accent"));
            } else {
                row->setBorderThickness(0.0f);
            }
            row->setMarginBottom(2);
            row->setFocusable(true);

            auto* top_line = new brls::Box(brls::Axis::ROW);
            top_line->setWidthPercentage(100);
            top_line->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            top_line->setAlignItems(brls::AlignItems::CENTER);

            std::string main_txt = lines[i].original;
            if (lyrics_display_mode_ == "translation" && !lines[i].translation.empty()) {
                main_txt = lines[i].translation;
            }
            auto* orig_lbl = ui::makeLabel(
                main_txt,
                active_line ? 18.5f : 16.0f,
                active_line ? "brls/accent" : "brls/text",
                1.38f);
            orig_lbl->setGrow(1.0f);
            top_line->addView(orig_lbl);

            auto* ts_lbl = ui::makeLabel(formatDuration(lines[i].time_sec), 12.0f, "font/grey");
            ts_lbl->setMarginLeft(12);
            ts_lbl->setShrink(0.0f);
            top_line->addView(ts_lbl);
            row->addView(top_line);

            brls::Label* tr_lbl = nullptr;
            const bool show_sub_tr =
                lyrics_display_mode_ == "bilingual" &&
                !lines[i].translation.empty() &&
                lines[i].translation != lines[i].original;
            if (show_sub_tr) {
                tr_lbl = ui::makeLabel(
                    lines[i].translation,
                    active_line ? 15.0f : 14.2f,
                    active_line ? "brls/text" : "font/grey",
                    1.36f);
                tr_lbl->setMarginTop(3);
                row->addView(tr_lbl);
            }

            const double seek_sec = lines[i].time_sec;
            row->registerClickAction([this, alive = alive_, i, seek_sec](brls::View*) {
                if (!alive->load(std::memory_order_acquire)) return true;
                selected_lyric_idx_ = static_cast<int>(i);
                const auto coord_st = PlaybackCoordinator::instance().state();
                const bool same_coord_track =
                    coord_st.active_role != PlaybackRole::None &&
                    coord_st.active_track.item.id == active_item_.id &&
                    coord_st.active_track.item.source_id == active_item_.source_id &&
                    !MPVCore::instance().state().current_url.empty();
                if (!same_coord_track && isPlayableOrResolvableItem(active_item_)) {
                    auto opts = buildPlaybackOptions(active_item_);
                    last_loaded_stream_key_ =
                        active_item_.source_id + ":" + active_item_.id + "|" + opts.url + "|" + opts.audio_url;
                    playVisibleMusic(seek_sec);
                } else {
                    PlaybackCoordinator::instance().seekAbsolute(seek_sec);
                }
                explainLyricLineWithAi(i);
                return true;
            });

            lyric_cards_.push_back(row);
            lyric_orig_labels_.push_back(orig_lbl);
            lyric_trans_labels_.push_back(tr_lbl);
            lyric_times_.push_back(lines[i].time_sec);
            lyrics_sheet->addView(row);
        }
    }

    lyrics_scroll_->setContentView(lyrics_sheet);
    fluid_sheet_wrap->addView(lyrics_scroll_);
    lyrics_card->addView(fluid_sheet_wrap);
    right_col->addView(lyrics_card);

    // =========================================================================
    // Dedicated AI Translation & Phrase Deep-Dive Window (Docked inside Studio)
    // =========================================================================
    auto* tutor_card = ui::makeCard("desktop/surface", 14.0f, 14.0f);
    tutor_card->setWidthPercentage(100);
    tutor_card->setShrink(0.0f);
    tutor_card->setMarginTop(12);

    auto* tutor_hdr = new brls::Box(brls::Axis::ROW);
    tutor_hdr->setWidthPercentage(100);
    tutor_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    tutor_hdr->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(tutor_hdr, 6.0f);

    auto* tutor_hdr_left = new brls::Box(brls::Axis::ROW);
    tutor_hdr_left->setAlignItems(brls::AlignItems::CENTER);
    auto* tutor_title = ui::makeLabel("AI 歌词翻译与精讲窗口", 15.5f, "brls/accent");
    tutor_title->setMarginRight(8);
    tutor_hdr_left->addView(tutor_title);

    const std::string init_tutor_status =
        lyric_explaining_
            ? "AI 流式生成中…"
            : (!lyric_tutor_provider_.empty() ? lyric_tutor_provider_ : "点击任意歌词即刻精讲");
    lyric_tutor_status_label_ = ui::makeLabel(init_tutor_status, 12.5f, "font/grey");
    tutor_hdr_left->addView(lyric_tutor_status_label_);
    tutor_hdr->addView(tutor_hdr_left);

    auto* tutor_btns = new brls::Box(brls::Axis::ROW);
    tutor_btns->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(tutor_btns, 5.0f);

    auto* exp_now_btn = ui::makeButton("AI 精讲当前句", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (active_lyrics_.empty()) {
            brls::Application::notify("当前曲目暂无歌词可精讲");
            return;
        }
        const int idx = (selected_lyric_idx_ >= 0 &&
                         static_cast<size_t>(selected_lyric_idx_) < active_lyrics_.size())
                            ? selected_lyric_idx_
                            : (active_lyric_idx_ >= 0 ? active_lyric_idx_ : 0);
        explainLyricLineWithAi(static_cast<size_t>(idx));
    }, true, 28.0f);
    exp_now_btn->setMarginRight(6);
    tutor_btns->addView(exp_now_btn);

    auto* dict_btn = ui::makeButton("划词查词", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (active_lyrics_.empty()) return;
        const int idx = (selected_lyric_idx_ >= 0 &&
                         static_cast<size_t>(selected_lyric_idx_) < active_lyrics_.size())
                            ? selected_lyric_idx_
                            : (active_lyric_idx_ >= 0 ? active_lyric_idx_ : 0);
        openWordLookupForLyric(static_cast<size_t>(idx));
    }, false, 28.0f);
    dict_btn->setMarginRight(6);
    tutor_btns->addView(dict_btn);

    auto* f8_btn = ui::makeButton("F8 深入对话", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        std::string ctx_summary = active_item_.title;
        if (!lyric_tutor_target_.empty()) {
            ctx_summary += " · " + lyric_tutor_target_;
        }
        if (on_entity_focus_) {
            on_entity_focus_(ctx_summary + " [歌词语言分析]");
        }
        if (HostKernel::instance().isInitialized()) {
            HostKernel::instance().bus().publish("host.companion.open", {
                {"domain", "media"},
                {"summary", ctx_summary},
            });
        }
    }, false, 28.0f);
    f8_btn->setMarginRight(6);
    tutor_btns->addView(f8_btn);

    auto* collapse_btn = ui::makeButton(lyric_tutor_expanded_ ? "收起" : "展开", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            lyric_tutor_expanded_ = !lyric_tutor_expanded_;
            rebuildSubContent();
        });
    }, false, 28.0f);
    tutor_btns->addView(collapse_btn);

    tutor_hdr->addView(tutor_btns);
    tutor_card->addView(tutor_hdr);

    if (lyric_tutor_expanded_) {
        auto* tutor_scroll = new brls::ScrollingFrame();
        tutor_scroll->setWidthPercentage(100);
        tutor_scroll->setHeight(148.0f);
        tutor_scroll->setMarginTop(8);

        auto* tutor_body = new brls::Box(brls::Axis::COLUMN);
        tutor_body->setWidthPercentage(100);
        tutor_body->setPadding(4, 4, 8, 4);

        std::string target_line_preview = lyric_tutor_target_;
        if (target_line_preview.empty() && !lines.empty()) {
            const size_t preview_i = active_lyric_idx_ >= 0 ? static_cast<size_t>(active_lyric_idx_) : 0;
            target_line_preview = lines[preview_i].original;
            if (!lines[preview_i].translation.empty() &&
                lines[preview_i].translation != lines[preview_i].original) {
                target_line_preview += "  ｜  " + lines[preview_i].translation;
            }
        }

        lyric_tutor_target_label_ = ui::makeLabel(
            target_line_preview.empty() ? "" : ("▶ " + target_line_preview),
            14.2f, "brls/text", 1.38f);
        if (target_line_preview.empty()) {
            lyric_tutor_target_label_->setVisibility(brls::Visibility::GONE);
        }
        tutor_body->addView(lyric_tutor_target_label_);

        const std::string default_hint =
            "点击上方流体云中的任意歌词行（或点「AI 精讲当前句」），即可由 AI 实时输出：\n"
            "1. 自然中文意译与情绪语气  ·  2. 核心词汇、俚语缩略与语法拆解  ·  3. 文化典故与演唱连读提示";
        lyric_tutor_body_label_ = ui::makeLabel(
            lyric_tutor_explanation_.empty() ? default_hint : lyric_tutor_explanation_,
            13.8f,
            lyric_tutor_explanation_.empty() ? "font/grey" : "brls/text",
            1.48f);
        lyric_tutor_body_label_->setMarginTop(target_line_preview.empty() ? 0 : 6);
        tutor_body->addView(lyric_tutor_body_label_);

        tutor_scroll->setContentView(tutor_body);
        tutor_card->addView(tutor_scroll);
    }

    right_col->addView(tutor_card);
    split->addView(right_col);
    static_root->addView(split);
    stage_grid_->setStaticContent(static_root);
}

void MediaWorkbench::renderNasView() {
    if (!stage_grid_) return;

    auto& kernel = HostKernel::instance();
    const bool has_nas = kernel.isInitialized() && kernel.capabilities().has("nas.connection.v1");

    auto* nas_header = new brls::Box(brls::Axis::COLUMN);
    nas_header->setWidthPercentage(100);

    auto* status_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    status_card->setMarginBottom(14);
    status_card->addView(ui::makeLabel("飞牛 fnOS NAS 插件解耦接入中心", 19.0f, "brls/text"));

    visible_grid_items_.clear();
    visible_imdb_items_.clear();

    if (!has_nas) {
        auto* hint = ui::makeLabel(
            "fnOS 连接器作为独立插件 (plugin-fnos-nas) 从主程序完全解耦。\n"
            "安装该插件并在设置表单填入你自己的 NAS 地址/协议/凭据后，即可在此统一浏览私人影库与无损音乐库，"
            "并自动打通 media.clip_jump.v1 跨插件片段跳转。",
            15.0f, "font/grey", 1.5f);
        hint->setMarginTop(8);
        hint->setMarginBottom(12);
        status_card->addView(hint);
        status_card->addView(ui::makeButton("前往插件市场安装 plugin-fnos-nas", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
                on_open_marketplace_();
            }
        }, true, 38.0f));
        nas_header->addView(status_card);
        stage_grid_->setHeaderView(nas_header);
        stage_grid_->setFooterView(nullptr);
        stage_grid_->setEmptyView(nullptr);
        stage_grid_->setSpanCount(1);
        stage_grid_->reloadData(false);
        return;
    }

    if (!nas_status_loaded_ && !nas_status_loading_) {
        loadNasStatus(false);
    }

    if (nas_status_loading_ && !nas_status_loaded_) {
        auto* loading = ui::makeLabel("正在探测飞牛 fnOS NAS 挂载与服务状态...", 14.5f, "font/grey");
        loading->setMarginTop(8);
        status_card->addView(loading);
    } else if (nas_status_json_.is_object()) {
        const bool configured = nas_status_json_.value("configured", false);
        const bool connected = nas_status_json_.value("connected", false);
        const bool mount_exists = nas_status_json_.value("mount_exists", false);
        const std::string mount_root = nas_status_json_.value("mount_root", std::string{});
        const size_t media_n = nas_status_json_.value("indexed_media_count", size_t{0});
        const size_t book_n = nas_status_json_.value("indexed_book_count", size_t{0});
        const size_t cue_n = nas_status_json_.value("indexed_subtitle_cues", size_t{0});

        auto* pill_row = new brls::Box(brls::Axis::ROW);
        pill_row->setMarginTop(10);
        pill_row->setMarginBottom(8);
        auto* conn_p = ui::makePill(
            connected ? "NAS 已连接" : (configured ? "未连通/待探测" : "未配置路径"),
            connected ? "status/ok_bg" : "status/warn_bg",
            connected ? "status/ok_fg" : "status/warn_fg");
        conn_p->setMarginRight(8);
        pill_row->addView(conn_p);

        auto* mnt_p = ui::makePill(
            mount_exists ? ("L1 挂载就绪: " + mount_root) : "L1 本地挂载未就绪",
            mount_exists ? "status/info_bg" : "desktop/soft",
            mount_exists ? "status/info_fg" : "font/grey");
        mnt_p->setMarginRight(8);
        pill_row->addView(mnt_p);

        pill_row->addView(ui::makePill(
            "媒体 " + std::to_string(media_n) + " · 书籍 " + std::to_string(book_n) + " · 字幕句 " + std::to_string(cue_n),
            "desktop/soft", "brls/text"));
        status_card->addView(pill_row);

        if (nas_status_json_.contains("services") && nas_status_json_["services"].is_object()) {
            const auto& svc = nas_status_json_["services"];
            auto addSvcState = [](std::ostringstream& oss, const char* label, const nlohmann::json& obj) {
                if (!obj.is_object()) return;
                const bool cfg = obj.value("configured", false);
                const bool ok = obj.value("connected", false);
                oss << label << ": " << (!cfg ? "未配置" : (ok ? "在线" : "离线"));
            };
            std::ostringstream oss;
            addSvcState(oss, "L2 字幕检索", svc.value("subtitle_service", nlohmann::json::object()));
            oss << "  ·  ";
            addSvcState(oss, "L2 同步", svc.value("companion_sync", nlohmann::json::object()));
            oss << "  ·  ";
            addSvcState(oss, "Radarr", svc.value("radarr", nlohmann::json::object()));
            oss << "  ·  ";
            addSvcState(oss, "Sonarr", svc.value("sonarr", nlohmann::json::object()));
            auto* svc_lbl = ui::makeLabel(oss.str(), 13.5f, "font/grey");
            svc_lbl->setMarginBottom(10);
            status_card->addView(svc_lbl);
        }
    }

    auto* action_row = new brls::Box(brls::Axis::ROW);
    action_row->setMarginTop(4);
    auto* scan_btn = ui::makeButton("重新扫描 NAS 索引", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        auto& k = HostKernel::instance();
        if (!k.isInitialized()) return;
        if (nas_status_request_) {
            nas_status_request_->cancel();
        }
        auto req = CancelToken::create();
        nas_status_request_ = req;
        nas_status_loading_ = true;
        loadInBackground(k.jobs(), "扫描飞牛 NAS 索引", req,
            [](const auto&) {
                return HostKernel::instance().capabilities().call("nas.connection.v1", {{"action", "scan_now"}});
            },
            [this, alive, req](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire)) return;
                if (nas_status_request_ == req) {
                    nas_status_request_.reset();
                }
                nas_status_loading_ = false;
                if (res.ok) {
                    nas_status_json_ = std::move(res.data);
                    nas_status_loaded_ = true;
                    brls::Application::notify("飞牛 NAS 索引扫描完成");
                    refresh();
                } else {
                    brls::Application::notify("扫描失败: " + res.error);
                    if (sub_mode_ == SubMode::NasLibrary) {
                        rebuildSubContent();
                    }
                }
            },
            [this, alive, req]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (nas_status_request_ && nas_status_request_ != req) return;
                nas_status_request_.reset();
                nas_status_loading_ = false;
                if (sub_mode_ == SubMode::NasLibrary) {
                    rebuildSubContent();
                }
            });
    }, true, 34.0f);
    scan_btn->setMarginRight(8);
    action_row->addView(scan_btn);

    auto* probe_btn = ui::makeButton("探测远端服务", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            loadNasStatus(true);
        }
    }, false, 34.0f);
    probe_btn->setMarginRight(8);
    action_row->addView(probe_btn);

    auto* browse_btn = ui::makeButton("浏览挂载目录", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            loadNasDirectory(".");
        }
    }, false, 34.0f);
    browse_btn->setMarginRight(8);
    action_row->addView(browse_btn);

    action_row->addView(ui::makeButton("插件参数设置", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
            on_open_marketplace_();
        }
    }, false, 34.0f));
    status_card->addView(action_row);
    nas_header->addView(status_card);

    if (!nas_browse_dir_.empty() || !nas_dir_entries_.empty() || !nas_dir_error_.empty()) {
        auto* dir_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
        dir_card->setMarginBottom(14);
        auto* dir_hdr = new brls::Box(brls::Axis::ROW);
        dir_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        dir_hdr->setAlignItems(brls::AlignItems::CENTER);
        dir_hdr->setMarginBottom(8);
        dir_hdr->addView(ui::makeLabel(
            "NAS 目录浏览: " + (nas_browse_dir_.empty() ? "." : nas_browse_dir_),
            16.5f, "brls/text"));

        auto* nav_row = new brls::Box(brls::Axis::ROW);
        auto* up_btn = ui::makeButton("上级目录", [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (nas_browse_dir_.empty() || nas_browse_dir_ == ".") return;
            std::string parent = std::filesystem::path(nas_browse_dir_).parent_path().string();
            loadNasDirectory(parent.empty() ? "." : parent);
        }, false, 34.0f);
        up_btn->setMarginRight(8);
        nav_row->addView(up_btn);
        nav_row->addView(ui::makeButton("根目录", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                loadNasDirectory(".");
            }
        }, false, 34.0f));
        dir_hdr->addView(nav_row);
        dir_card->addView(dir_hdr);

        if (!nas_dir_error_.empty()) {
            dir_card->addView(ui::makeLabel(nas_dir_error_, 14.0f, "status/warn_fg"));
        } else if (nas_dir_entries_.is_array() && nas_dir_entries_.empty()) {
            dir_card->addView(ui::makeLabel("该目录为空。", 14.0f, "font/grey"));
        } else if (nas_dir_entries_.is_array()) {
            size_t shown = 0;
            for (const auto& entry : nas_dir_entries_) {
                if (!entry.is_object()) continue;
                const std::string ename = entry.value("name", "");
                const std::string epath = entry.value("path", "");
                const bool is_dir = entry.value("is_dir", false);
                const uint64_t size_b = entry.value("size_bytes", uint64_t{0});
                const std::string ext = fileExtLower(epath);

                auto* row = ui::makeCard("desktop/soft", 10.0f, 8.0f);
                row->setMarginTop(6);
                row->setFocusable(true);
                auto* rbox = new brls::Box(brls::Axis::ROW);
                rbox->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
                rbox->setAlignItems(brls::AlignItems::CENTER);
                rbox->addView(ui::makeLabel(
                    (is_dir ? "[目录] " : "[文件] ") + ename,
                    15.0f, is_dir ? "brls/accent" : "brls/text"));
                rbox->addView(ui::makeLabel(
                    is_dir ? "打开" : formatBytesHuman(size_b),
                    13.5f, "font/grey"));
                row->addView(rbox);

                row->registerClickAction([this, alive = alive_, is_dir, epath, ename, ext](brls::View*) {
                    if (!alive->load(std::memory_order_acquire)) return true;
                    if (is_dir) {
                        loadNasDirectory(epath);
                    } else if (isBookFileExt(ext)) {
                        if (HostKernel::instance().isInitialized()) {
                            HostKernel::instance().bus().publish("entity.book.open", {
                                {"book_id", "nas-file:" + ename},
                                {"title", ename},
                                {"file_path", epath},
                                {"format", ext.size() > 1 ? ext.substr(1) : "pdf"},
                                {"page", 1},
                            });
                        }
                    } else if (isMediaFileExt(ext)) {
                        auto& k = HostKernel::instance();
                        if (!k.isInitialized()) return true;
                        if (nas_dir_request_) {
                            nas_dir_request_->cancel();
                        }
                        auto req = CancelToken::create();
                        nas_dir_request_ = req;
                        loadUiInBackground(k.uiTasks(), "media.nas_stream", req,
                            [epath](const auto&) {
                                return HostKernel::instance().capabilities().call(
                                    "nas.connection.v1", {{"action", "stream_url"}, {"path", epath}});
                            },
                            [this, alive, req, epath, ename, ext](CapabilityResult res) {
                                if (!alive->load(std::memory_order_acquire)) return;
                                if (nas_dir_request_ == req) {
                                    nas_dir_request_.reset();
                                }
                                if (!res.ok) {
                                    brls::Application::notify("无法获取 NAS 播放地址: " + res.error);
                                    return;
                                }
                                MediaItem item;
                                item.id = "nas-file:" + epath;
                                item.source_id = "plugin-fnos-nas";
                                item.title = ename;
                                item.creator = "飞牛 fnOS NAS";
                                item.kind = (ext == ".flac" || ext == ".mp3" || ext == ".aac" ||
                                             ext == ".wav" || ext == ".ogg" || ext == ".opus" ||
                                             ext == ".alac" || ext == ".m4a")
                                                ? "music"
                                                : "nas";
                                item.source_badge = "飞牛 NAS";
                                item.play_url = res.data.value("play_url", "");
                                item.extra = {{"local_path", res.data.value("path", epath)}};
                                selectItem(item, true);
                            },
                            [this, alive, req]() {
                                if (!alive->load(std::memory_order_acquire)) return;
                                if (nas_dir_request_ == req) {
                                    nas_dir_request_.reset();
                                }
                            });
                    } else {
                        brls::Application::notify("暂不支持直接预览此文件类型: " + ename);
                    }
                    return true;
                });
                dir_card->addView(row);
                if (++shown >= 40) break;
            }
        }
        nas_header->addView(dir_card);
    }

    for (const auto& item : current_items_) {
        if (!isCuratedMedia(item) && (item.source_id == "plugin-fnos-nas" || item.kind == "nas")) {
            if (mediaItemMatchesQuery(item, search_query_)) {
                visible_grid_items_.push_back(item);
            }
        }
    }
    if (!visible_grid_items_.empty()) {
        auto* lib_hdr = ui::makeLabel(
            "NAS 已索引媒体库 (" + std::to_string(visible_grid_items_.size()) + " 项)",
            17.5f, "brls/text");
        lib_hdr->setMarginBottom(10);
        nas_header->addView(lib_hdr);
    }

    const float stage_w = estimateMediaStageWidth(this, false);
    const int cols = computeVideoGridColumns(stage_w);
    constexpr float kGap = 14.0f;
    const float card_w =
        std::max(220.0f, (stage_w - kGap * static_cast<float>(cols - 1)) / static_cast<float>(cols));
    const float inner_cover_w = std::max(160.0f, card_w - 48.0f);
    active_video_cover_h_ = std::round(inner_cover_w * 9.0f / 16.0f);

    stage_grid_->setColSpace(kGap);
    stage_grid_->setEstimatedRowSpace(14.0f);
    stage_grid_->setEstimatedRowHeight(active_video_cover_h_ + 186.0f);
    stage_grid_->setHeaderView(nas_header);
    stage_grid_->setFooterView(nullptr);
    stage_grid_->setEmptyView(nullptr);
    stage_grid_->setSpanCount(cols);
    stage_grid_->reloadData(false);
}

void MediaWorkbench::loadImdbDiscover(const std::string& chart_id, const std::string& query) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("imdb.discover.v1")) {
        return;
    }
    if (imdb_request_) {
        imdb_request_->cancel();
        imdb_request_.reset();
    }
    if (!chart_id.empty()) {
        imdb_chart_ = chart_id;
    }
    imdb_query_ = trimSpaces(query);
    imdb_loading_ = true;
    imdb_error_.clear();
    if (sub_mode_ == SubMode::ImdbDiscover) {
        rebuildSubContent();
    }

    auto req = CancelToken::create();
    imdb_request_ = req;
    const std::string target_chart = imdb_chart_;
    const std::string target_query = imdb_query_;

    loadUiInBackground(
        kernel.uiTasks(),
        "media.imdb_discover",
        req,
        [target_chart, target_query](const auto& cancel) {
            nlohmann::json payload;
            if (!target_query.empty()) {
                payload = {{"action", "search"}, {"query", target_query}, {"limit", 36}};
            } else {
                payload = {{"action", "chart"}, {"chart", target_chart}, {"limit", 50}};
            }
            return HostKernel::instance().capabilities().callCancellable(
                "imdb.discover.v1", payload, cancel);
        },
        [this, alive = alive_, req](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (imdb_request_ == req) {
                imdb_request_.reset();
            }
            imdb_loading_ = false;
            imdb_loaded_ = true;
            if (!res.ok) {
                imdb_items_ = nlohmann::json::array();
                imdb_error_ = res.error.empty() ? "加载 IMDb 数据失败" : res.error;
            } else {
                imdb_items_ = res.data.is_object()
                                  ? res.data.value("items", nlohmann::json::array())
                                  : nlohmann::json::array();
                imdb_error_.clear();
            }
            if (sub_mode_ == SubMode::ImdbDiscover) {
                rebuildSubContent();
            }
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (imdb_request_ == req) {
                imdb_request_.reset();
            }
            imdb_loading_ = false;
        });
}

void MediaWorkbench::renderImdbView() {
    if (!stage_grid_) return;

    auto& kernel = HostKernel::instance();
    const bool has_imdb = kernel.isInitialized() && kernel.capabilities().has("imdb.discover.v1");

    visible_grid_items_.clear();
    visible_imdb_items_.clear();

    auto* imdb_header = new brls::Box(brls::Axis::COLUMN);
    imdb_header->setWidthPercentage(100);

    auto* ctrl_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    ctrl_card->setWidthPercentage(100);
    ctrl_card->setShrink(0.0f);
    ctrl_card->setMarginBottom(14);

    auto* hdr_row = new brls::Box(brls::Axis::ROW);
    hdr_row->setWidthPercentage(100);
    hdr_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    hdr_row->setAlignItems(brls::AlignItems::CENTER);
    hdr_row->addView(ui::makeLabel("IMDb 发现与 Radarr / Sonarr 对数做种分档选片", 19.0f, "brls/text"));

    if (has_imdb) {
        auto* full_btn = ui::makeButton(
            "打开完整 IMDb 选片与下载队列",
            [alive = alive_]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (HostKernel::instance().isInitialized()) {
                    HostKernel::instance().bus().publish("entity.workspace.open",
                                                         {{"tab_id", "tab-arr-imdb"}});
                }
            },
            false, 34.0f);
        hdr_row->addView(full_btn);
    }
    ctrl_card->addView(hdr_row);

    auto finishHeaderOnly = [&]() {
        stage_grid_->setColSpace(0.0f);
        stage_grid_->setEstimatedRowSpace(10.0f);
        stage_grid_->setEstimatedRowHeight(152.0f);
        stage_grid_->setHeaderView(imdb_header);
        stage_grid_->setFooterView(nullptr);
        stage_grid_->setEmptyView(nullptr);
        stage_grid_->setSpanCount(1);
        stage_grid_->reloadData(false);
    };

    if (!has_imdb) {
        auto* hint = ui::makeLabel(
            "IMDb 发现与选片作为独立插件 (plugin-arr-imdb) 从宿主解耦。\n"
            "安装启用后即可在此浏览 IMDb Top 250 / 热门电影与剧集、检索 tt 编号，并联动飞牛 NAS 本地匹配播放与 Radarr / Sonarr 对数做种分档选小体积下载。",
            15.0f, "font/grey", 1.5f);
        hint->setMarginTop(8);
        hint->setMarginBottom(12);
        ctrl_card->addView(hint);
        ctrl_card->addView(ui::makeButton("前往插件市场启用 plugin-arr-imdb", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
                on_open_marketplace_();
            }
        }, true, 38.0f));
        imdb_header->addView(ctrl_card);
        finishHeaderOnly();
        return;
    }

    auto* rule_lbl = ui::makeLabel(
        "选片策略：最高做种档 round(log10(seeders)) 内优先较小体积 · 自动过滤枪版 · 可一键匹配飞牛本地片库直连播放",
        13.5f, "font/grey");
    rule_lbl->setMarginTop(6);
    ctrl_card->addView(rule_lbl);

    auto* chart_row = new brls::Box(brls::Axis::ROW);
    chart_row->setMarginTop(10);
    struct ChartOpt {
        const char* id;
        const char* label;
    };
    const ChartOpt charts[] = {
        {"top250", "Top 250 电影"},
        {"moviemeter", "热门电影趋势"},
        {"toptv", "Top 250 剧集"},
        {"tvmeter", "热门剧集趋势"},
    };
    for (const auto& c : charts) {
        const bool active = imdb_query_.empty() && imdb_chart_ == c.id;
        auto* btn = ui::makeButton(
            c.label,
            [this, alive = alive_, cid = std::string(c.id)]() {
                if (alive->load(std::memory_order_acquire)) {
                    loadImdbDiscover(cid, "");
                }
            },
            active, 32.0f);
        btn->setMarginRight(8);
        chart_row->addView(btn);
    }

    auto* search_btn = ui::makeButton(
        imdb_query_.empty() ? "搜索片名 / ttID" : ("搜索: " + imdb_query_),
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                promptSearch();
            }
        },
        !imdb_query_.empty(), 32.0f);
    search_btn->setMarginRight(8);
    chart_row->addView(search_btn);

    auto* reload_btn = ui::makeButton(
        "刷新",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                loadImdbDiscover(imdb_chart_, imdb_query_);
            }
        },
        false, 32.0f);
    chart_row->addView(reload_btn);
    ctrl_card->addView(chart_row);
    imdb_header->addView(ctrl_card);

    if (!imdb_loaded_ && !imdb_loading_) {
        finishHeaderOnly();
        loadImdbDiscover(imdb_chart_, imdb_query_);
        return;
    }

    if (imdb_loading_) {
        auto* loading_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
        loading_card->setWidthPercentage(100);
        loading_card->setShrink(0.0f);
        loading_card->addView(ui::makeLabel(
            imdb_query_.empty() ? ("正在拉取 IMDb 榜单 (" + imdb_chart_ + ")...")
                                : ("正在检索 IMDb: \"" + imdb_query_ + "\"..."),
            14.5f, "font/grey"));
        imdb_header->addView(loading_card);
        finishHeaderOnly();
        return;
    }

    if (!imdb_error_.empty()) {
        auto* err_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
        err_card->setWidthPercentage(100);
        err_card->setShrink(0.0f);
        err_card->addView(ui::makeLabel("IMDb 加载失败: " + imdb_error_, 14.5f, "status/warn_fg"));
        imdb_header->addView(err_card);
        finishHeaderOnly();
        return;
    }

    if (imdb_items_.is_array()) {
        for (const auto& entry : imdb_items_) {
            if (entry.is_object()) {
                visible_imdb_items_.push_back(entry);
            }
        }
    }

    auto* empty_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    empty_card->setWidthPercentage(100);
    empty_card->setShrink(0.0f);
    empty_card->addView(ui::makeLabel("暂无匹配的 IMDb 影视条目。", 14.5f, "font/grey"));

    stage_grid_->setColSpace(0.0f);
    stage_grid_->setEstimatedRowSpace(10.0f);
    stage_grid_->setEstimatedRowHeight(152.0f);
    stage_grid_->setHeaderView(imdb_header);
    stage_grid_->setFooterView(nullptr);
    stage_grid_->setEmptyView(empty_card);
    stage_grid_->setSpanCount(1);
    stage_grid_->reloadData(false);
}

void MediaWorkbench::renderPlayerStageView() {
    if (!stage_grid_) return;

    const float stage_w = estimateMediaStageWidth(this, immersive_fullscreen_);
    const float stage_h = estimateMediaStageHeight(this, immersive_fullscreen_);

    std::vector<ChapterSegment> chapters = active_chapters_;
    if (chapters.empty() && active_item_.extra.is_object() &&
        active_item_.extra.contains("chapters") && active_item_.extra["chapters"].is_array()) {
        for (const auto& c : active_item_.extra["chapters"]) {
            if (!c.is_object()) continue;
            ChapterSegment seg;
            seg.start_sec = c.value("start_sec", 0.0);
            seg.end_sec = c.value("end_sec", seg.start_sec);
            seg.label = c.value("label", std::string{});
            seg.digest = c.value("digest", std::string{});
            seg.kind = c.value("kind", std::string("content"));
            chapters.push_back(std::move(seg));
        }
    }
    if (chapters.empty() && active_item_.id.rfind("demo-", 0) == 0) {
        chapters = {
            {0.0, 225.0, "00:00 开篇背景", "工作台与插件解耦概述", "content"},
            {225.0, 268.0, "03:45 SponsorBlock 赞助", "按 F9 一键跳过广告片段", "sponsor"},
            {268.0, 680.0, "04:28 架构对比", "独立进程 Worker 与主机分离", "content"},
            {680.0, 1130.0, "11:20 ABI 热插拔演示", "动态卸载与流式取消", "content"},
        };
    }

    mpv_stage_ = new MpvStageView(&MPVCore::instance());
    mpv_stage_->setWidthPercentage(100);
    mpv_stage_->setFullscreenLabel(immersive_fullscreen_);
    mpv_stage_->setFullscreenCallback([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_toggle_fullscreen_) {
            on_toggle_fullscreen_();
        }
    });

    MediaRequestOptions opts = buildPlaybackOptions(active_item_);
    const std::string stream_key = active_item_.source_id + ":" + active_item_.id + "|" + opts.url + "|" + opts.audio_url;
    const auto coord_st = PlaybackCoordinator::instance().state();
    const bool already_active_in_coord =
        coord_st.active_role != PlaybackRole::None &&
        coord_st.active_track.item.id == active_item_.id &&
        coord_st.active_track.item.source_id == active_item_.source_id &&
        !MPVCore::instance().state().current_url.empty();
    if (isPlayableOrResolvableItem(active_item_) && last_loaded_stream_key_ != stream_key && !already_active_in_coord) {
        last_loaded_stream_key_ = stream_key;
        mpv_stage_->setDanmaku(active_danmaku_);
        mpv_stage_->setSubtitles(active_lyrics_);
        mpv_stage_->setTitle(active_item_.title);
        PlaybackTrack tr;
        tr.item = active_item_;
        tr.options = opts;
        tr.lyrics = active_lyrics_;
        tr.danmaku = active_danmaku_;
        tr.chapters = chapters;
        tr.auto_enrich_timeline = (active_item_.kind != "music");
        const PlaybackRole target_role =
            (active_item_.kind == "music") ? PlaybackRole::BackgroundMusic : PlaybackRole::ForegroundVideo;
        PlaybackCoordinator::instance().play(
            target_role, "host.media_workbench", std::move(tr));
    } else {
        if (already_active_in_coord) {
            last_loaded_stream_key_ = stream_key;
        }
        mpv_stage_->setTitle(active_item_.title);
        mpv_stage_->setSubtitles(active_lyrics_);
        mpv_stage_->setDanmaku(active_danmaku_);
        mpv_stage_->setChapters(chapters);
        MPVCore::instance().setChapters(chapters);
    }

    if (immersive_fullscreen_) {
        auto* immersive_wrap = new brls::Box(brls::Axis::COLUMN);
        immersive_wrap->setWidthPercentage(100);
        immersive_wrap->setPadding(8, 12, 8, 12);
        immersive_wrap->setBackgroundColor(nvgRGBA(8, 8, 10, 255));
        mpv_stage_->setStageMinHeight(std::max(240.0f, stage_h - 94.0f));
        immersive_wrap->addView(mpv_stage_);
        stage_grid_->setStaticContent(immersive_wrap);
        return;
    }

    const float ideal_16_9 = std::round((stage_w - 36.0f) * 9.0f / 16.0f);
    const float max_canvas_h = std::max(280.0f, stage_h - 320.0f);
    mpv_stage_->setStageMinHeight(std::clamp(ideal_16_9, 260.0f, max_canvas_h));
    mpv_stage_->setMarginTop(12);

    auto* player_card = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    player_card->addView(ui::makeLabel("MPV 桌面播放舞台 · " + active_item_.title, 19.0f, "brls/text"));
    player_card->addView(mpv_stage_);

    if (!isPlayableOrResolvableItem(active_item_)) {
        auto* preview_hint = ui::makeLabel(
            "当前为布局预览条目（play_url 为空）；接入 Bilibili / YouTube / fnOS NAS 插件返回直连流与 Referer/Cookie 后，libmpv OpenGL/FBO 将自动硬解播放",
            13.5f, "font/grey", 1.4f);
        preview_hint->setMarginTop(8);
        player_card->addView(preview_hint);
    }

    const auto season_eps = extractSeasonEpisodes(active_item_);
    if (season_eps.size() > 1) {
        int cur_ep_idx = -1;
        for (size_t i = 0; i < season_eps.size(); ++i) {
            if (season_eps[i].id == active_item_.id ||
                (!active_item_.play_url.empty() && season_eps[i].play_url == active_item_.play_url)) {
                cur_ep_idx = static_cast<int>(i);
                break;
            }
        }
        const std::string group_title = active_item_.extra.is_object()
                                            ? active_item_.extra.value("season_group_title", std::string{})
                                            : std::string{};

        auto* ep_hdr_row = new brls::Box(brls::Axis::ROW);
        ep_hdr_row->setWidthPercentage(100);
        ep_hdr_row->setMarginTop(14);
        ep_hdr_row->setMarginBottom(8);
        ep_hdr_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        ep_hdr_row->setAlignItems(brls::AlignItems::CENTER);

        const std::string hdr_txt =
            "本季选集" + (group_title.empty() ? std::string{} : (" · " + group_title)) +
            " (" + std::to_string(season_eps.size()) + " 集)";
        ep_hdr_row->addView(ui::makeLabel(hdr_txt, 16.0f, "brls/text"));

        if (cur_ep_idx >= 0 && static_cast<size_t>(cur_ep_idx + 1) < season_eps.size()) {
            const MediaItem next_ep = season_eps[static_cast<size_t>(cur_ep_idx + 1)];
            std::string next_code = next_ep.extra.is_object()
                                        ? next_ep.extra.value("episode_code", std::string{})
                                        : std::string{};
            auto* next_btn = ui::makeButton(
                "下一集" + (next_code.empty() ? std::string{} : (" · " + next_code)),
                [this, alive = alive_, next_ep]() {
                    brls::sync([this, alive, next_ep]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        last_loaded_stream_key_.clear();
                        selectItem(next_ep, true);
                    });
                },
                true, 32.0f);
            ep_hdr_row->addView(next_btn);
        }
        player_card->addView(ep_hdr_row);

        auto* ep_chips = new brls::Box(brls::Axis::ROW);
        ui_comfort::enableFlexWrap(ep_chips, 6.0f);
        for (size_t i = 0; i < season_eps.size(); ++i) {
            const MediaItem ep = season_eps[i];
            const bool is_cur = (static_cast<int>(i) == cur_ep_idx);
            std::string code = ep.extra.is_object() ? ep.extra.value("episode_code", "") : "";
            std::string sub_t = ep.extra.is_object() ? ep.extra.value("episode_title", "") : "";
            std::string lbl = !code.empty() ? code : ("第 " + std::to_string(i + 1) + " 集");
            if (!sub_t.empty()) {
                lbl += " · " + clipUtf8Bytes(sub_t, 20);
            }
            auto* chip = ui_comfort::makeFilterChip(
                lbl,
                [this, alive = alive_, ep, is_cur]() {
                    brls::sync([this, alive, ep, is_cur]() {
                        if (!alive->load(std::memory_order_acquire) || is_cur) return;
                        last_loaded_stream_key_.clear();
                        selectItem(ep, true);
                    });
                },
                is_cur, "domain/media", 32.0f);
            chip->setMarginRight(6);
            ep_chips->addView(chip);
        }
        player_card->addView(ep_chips);
    }

    auto* chap_hdr = ui::makeLabel("AI 智能章节与 SponsorBlock 时间轴标记 (F9 跳过赞助)", 16.0f, "brls/text");
    chap_hdr->setMarginTop(14);
    chap_hdr->setMarginBottom(8);
    player_card->addView(chap_hdr);

    auto* chap_row = new brls::Box(brls::Axis::ROW);
    for (size_t i = 0; i < chapters.size() && i < 6; ++i) {
        const auto ch = chapters[i];
        const bool is_ad = (ch.kind == "ad" || ch.kind == "sponsor");
        const std::string btn_label = is_ad ? ("[赞助] " + ch.label) : ch.label;
        auto* btn = ui::makeButton(btn_label, [this, alive = alive_, ch, is_ad]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (is_ad) {
                if (ch.end_sec > ch.start_sec) {
                    MPVCore::instance().seekAbsolute(ch.end_sec);
                } else if (mpv_stage_) {
                    mpv_stage_->skipSponsorIfActive();
                }
                brls::Application::notify("已跳过赞助片段: " + ch.label);
            } else {
                MPVCore::instance().seekAbsolute(ch.start_sec);
                brls::Application::notify("跳转至章节: " + ch.label);
            }
        }, false, 34.0f);
        btn->setMarginRight(8);
        chap_row->addView(btn);
    }
    auto* skip_btn = ui::makeButton("跳过广告 F9", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (skipSponsorIfActive()) {
            brls::Application::notify("已跳过当前 SponsorBlock 片段");
        } else {
            brls::Application::notify("当前播放时间不在赞助片段内");
        }
    }, true, 34.0f);
    chap_row->addView(skip_btn);

    if (HostKernel::instance().isInitialized() &&
        HostKernel::instance().capabilities().has("speech.asr.v1")) {
        bool asr_running = false;
        if (!active_asr_job_id_.empty()) {
            if (auto j = HostKernel::instance().jobs().getJob(active_asr_job_id_)) {
                asr_running = (j->state == JobState::Queued || j->state == JobState::Running);
            }
        }
        auto* asr_btn = ui::makeButton(
            asr_running ? "ASR 转写中..." : "FunASR 生成字幕",
            [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire)) triggerLocalAsrGeneration();
            },
            false,
            34.0f);
        asr_btn->setMarginLeft(8);
        chap_row->addView(asr_btn);
    }

    if (HostKernel::instance().isInitialized() &&
        HostKernel::instance().capabilities().has("subtitle.fetch.v1")) {
        bool sub_running = false;
        if (!active_sub_job_id_.empty()) {
            if (auto j = HostKernel::instance().jobs().getJob(active_sub_job_id_)) {
                sub_running = (j->state == JobState::Queued || j->state == JobState::Running);
            }
        }
        auto* sub_btn = ui::makeButton(
            sub_running ? "字幕匹配中..." : "SubHunter 匹配字幕",
            [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire)) triggerSubHunterMatch();
            },
            false,
            34.0f);
        sub_btn->setMarginLeft(8);
        chap_row->addView(sub_btn);
    }

    if (HostKernel::instance().isInitialized() &&
        HostKernel::instance().capabilities().has("text.comments.v1")) {
        auto* cmt_btn = ui::makeButton(
            "嵌套评论区",
            [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire)) showVideoCommentsDialog();
            },
            false, 34.0f);
        cmt_btn->setMarginLeft(8);
        chap_row->addView(cmt_btn);
    }

    player_card->addView(chap_row);
    appendCuratedMetadataBlock(player_card, active_item_);
    stage_grid_->setStaticContent(player_card);
}

void MediaWorkbench::activateMediaItem(const MediaItem& item) {
    if (item.extra.is_object() && item.extra.value("is_season_group", false)) {
        const auto episodes = extractSeasonEpisodes(item);
        if (!episodes.empty()) {
            showSeasonEpisodesDialog(item, episodes);
            return;
        }
    }
    last_loaded_stream_key_.clear();
    selectItem(item, true);
    if (item.kind == "music" && isPlayableOrResolvableItem(item)) {
        auto opts = buildPlaybackOptions(active_item_);
        last_loaded_stream_key_ =
            active_item_.source_id + ":" + active_item_.id + "|" + opts.url + "|" + opts.audio_url;
        playVisibleMusic();
    }
}

void MediaWorkbench::showSeasonEpisodesDialog(const MediaItem& season_item,
                                              const std::vector<MediaItem>& episodes) {
    if (episodes.empty()) return;

    auto* box = new brls::Box(brls::Axis::COLUMN);
    box->setPadding(20.0f);
    box->setWidth(720.0f);

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setWidthPercentage(100);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setMarginBottom(14.0f);

    const std::string season_poster =
        season_item.extra.is_object() ? season_item.extra.value("poster_url", "") : "";
    const std::string hdr_img = !season_poster.empty() ? season_poster : season_item.cover_url;
    if (!hdr_img.empty()) {
        auto* cover = new CachedImageView(&ImageCache::instance());
        if (!season_poster.empty() && season_poster != season_item.cover_url) {
            cover->setDimensions(76.0f, 114.0f);
        } else {
            cover->setDimensions(144.0f, 81.0f);
        }
        cover->setCornerRadius(8.0f);
        cover->setScalingType(brls::ImageScalingType::FILL);
        cover->setMarginRight(14.0f);
        cover->setImageUrl(hdr_img, extractHeadersFromItem(season_item));
        hdr->addView(cover);
    }

    auto* title_col = new brls::Box(brls::Axis::COLUMN);
    title_col->setGrow(1.0f);
    title_col->setShrink(1.0f);
    title_col->addView(ui::makeLabel(season_item.title, 20.0f, "brls/text"));
    std::string sub_desc = "共 " + std::to_string(episodes.size()) + " 集";
    if (!season_item.quality_label.empty()) {
        sub_desc += " · " + season_item.quality_label;
    }
    sub_desc += " · 点击任一集直接进入 MPV 播放";
    auto* sub_lbl = ui::makeLabel(sub_desc, 13.5f, "font/grey");
    sub_lbl->setMarginTop(4.0f);
    title_col->addView(sub_lbl);
    hdr->addView(title_col);
    box->addView(hdr);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setWidthPercentage(100);
    const float list_h = std::clamp(static_cast<float>(episodes.size()) * 62.0f, 140.0f, 380.0f);
    scroll->setHeight(list_h);

    auto* list_col = new brls::Box(brls::Axis::COLUMN);
    list_col->setWidthPercentage(100);

    auto dlg_holder = std::make_shared<brls::Dialog*>(nullptr);

    for (size_t i = 0; i < episodes.size(); ++i) {
        const MediaItem ep = episodes[i];
        auto* row = ui::makeCard("desktop/soft", 12.0f, 9.0f);
        if (i > 0) row->setMarginTop(8.0f);
        row->setFocusable(true);

        auto* rbox = new brls::Box(brls::Axis::ROW);
        rbox->setWidthPercentage(100);
        rbox->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        rbox->setAlignItems(brls::AlignItems::CENTER);

        auto* left_row = new brls::Box(brls::Axis::ROW);
        left_row->setAlignItems(brls::AlignItems::CENTER);
        left_row->setGrow(1.0f);
        left_row->setShrink(1.0f);

        std::string code = ep.extra.is_object() ? ep.extra.value("episode_code", "") : "";
        if (code.empty()) code = "第 " + std::to_string(i + 1) + " 集";
        auto* code_pill = ui::makePill(code, "desktop/surface", "domain/media");
        code_pill->setMarginRight(10.0f);
        left_row->addView(code_pill);

        std::string ep_sub = ep.extra.is_object() ? ep.extra.value("episode_title", "") : "";
        std::string main_txt = !ep_sub.empty() ? ep_sub : ep.title;
        auto* ep_lbl = ui::makeLabel(clipUtf8Bytes(main_txt, 64), 15.5f, "brls/text");
        ep_lbl->setSingleLine(true);
        left_row->addView(ep_lbl);
        rbox->addView(left_row);

        auto* right_row = new brls::Box(brls::Axis::ROW);
        right_row->setAlignItems(brls::AlignItems::CENTER);
        right_row->setShrink(0.0f);
        if (!ep.quality_label.empty()) {
            auto* qp = ui::makePill(ep.quality_label, "status/info_bg", "status/info_fg");
            qp->setMarginRight(6.0f);
            right_row->addView(qp);
        }
        if (ep.has_bilingual_sub) {
            auto* sp = ui::makePill("双语字幕", "status/ok_bg", "status/ok_fg");
            sp->setMarginRight(6.0f);
            right_row->addView(sp);
        }
        right_row->addView(ui::makeLabel("播放", 13.5f, "brls/accent"));
        rbox->addView(right_row);

        row->addView(rbox);

        auto onPick = [this, alive = alive_, dlg_holder, ep]() {
            if (*dlg_holder) {
                (*dlg_holder)->dismiss();
                *dlg_holder = nullptr;
            }
            brls::sync([this, alive, ep]() {
                if (!alive->load(std::memory_order_acquire)) return;
                last_loaded_stream_key_.clear();
                selectItem(ep, true);
            });
        };

        row->registerClickAction([onPick](brls::View*) {
            onPick();
            return true;
        });
        row->addGestureRecognizer(new brls::TapGestureRecognizer(
            [row, onPick](brls::TapGestureStatus status, brls::Sound* soundToPlay) {
                if (!row->isFocusable() || row->getVisibility() != brls::Visibility::VISIBLE) return;
                if (status.state == brls::GestureState::END) {
                    if (soundToPlay) *soundToPlay = brls::SOUND_CLICK;
                    onPick();
                }
            }));

        list_col->addView(row);
    }

    scroll->setContentView(list_col);
    box->addView(scroll);

    auto* dlg = new brls::Dialog(box);
    *dlg_holder = dlg;
    const MediaItem first_ep = episodes.front();
    dlg->addButton("从第 1 集开始播放", [this, alive = alive_, dlg_holder, first_ep]() {
        *dlg_holder = nullptr;
        brls::sync([this, alive, first_ep]() {
            if (!alive->load(std::memory_order_acquire)) return;
            last_loaded_stream_key_.clear();
            selectItem(first_ep, true);
        });
    });
    dlg->addButton("取消", [dlg_holder]() { *dlg_holder = nullptr; });
    dlg->open();
}

void MediaWorkbench::selectItem(const MediaItem& item, bool switch_to_player) {
    if (detail_request_) {
        detail_request_->cancel();
    }
    auto req = CancelToken::create();
    detail_request_ = req;
    if (sub_mode_ == SubMode::DailyCuration && item.kind == "music" && isCuratedMedia(item)) {
        music_scope_ = item.extra.value("curation_mode", "") == "interactive_ephemeral"
            ? MusicScope::Interactive : MusicScope::Daily;
        curation_date_ = item.extra.value("business_date", "");
        archive_all_ = false;
    }
    if (item.kind == "music" && !isCuratedMedia(item)) music_scope_ = MusicScope::Personal;
    if (lyric_tutor_track_id_ != item.id) {
        lyric_tutor_track_id_ = item.id;
        selected_lyric_idx_ = -1;
        lyric_translating_ = false;
        lyric_explaining_ = false;
        lyric_tutor_target_.clear();
        lyric_tutor_provider_.clear();
        lyric_tutor_explanation_.clear();
        if (lyric_translate_request_) {
            lyric_translate_request_->cancel();
            lyric_translate_request_.reset();
        }
        if (lyric_explain_request_) {
            lyric_explain_request_->cancel();
            lyric_explain_request_.reset();
        }
    }
    active_item_ = item;
    has_active_item_ = true;
    active_lyrics_.clear();
    active_danmaku_.clear();
    active_chapters_.clear();

    if (HostKernel::instance().isInitialized()) {
        auto& kernel = HostKernel::instance();
        struct LoadedMediaDetails {
            std::vector<LyricLine> lyrics;
            std::vector<DanmakuCue> danmaku;
            std::vector<ChapterSegment> chapters;
            std::string play_url;
            std::string audio_url;
            std::string cover_url;
            std::string quality_label;
            double duration_sec = 0.0;
            bool android_media = false;
            bool has_stream = false;
        };
        loadUiInBackground(kernel.uiTasks(), "media.detail", req,
            [sources = kernel.domains().mediaSources(), &caps = kernel.capabilities(), item](const auto& cancel) {
                LoadedMediaDetails details;
                const bool is_demo = (item.id.rfind("demo-", 0) == 0);
                const auto am = extractAppleMusicLookup(item);
                if (!is_demo && item.play_url.empty() &&
                    (item.source_id == "bilibili" || item.source_id == "youtube") &&
                    caps.has("media.source.v1")) {
                    const std::string target_plugin =
                        (item.source_id == "youtube") ? "plugin-youtube" : "plugin-bilibili";
                    const nlohmann::json req_json = {{"action", "playurl"}, {"id", item.id}};
                    auto res = caps.callProvider(target_plugin, "media.source.v1", req_json);
                    if (!res.ok && res.error.find("not found") != std::string::npos) {
                        res = caps.call("media.source.v1", req_json);
                    }
                    if (res.ok && res.data.is_object()) {
                        details.play_url = res.data.value("video_url", std::string{});
                        details.audio_url = res.data.value("audio_url", std::string{});
                        details.quality_label = res.data.value("quality_label", std::string{});
                        details.android_media = res.data.value("android_media", false);
                        details.has_stream = !details.play_url.empty();
                    }
                }
                if (!cancel->isCancelled() && !is_demo && am.is_apple_music &&
                    (item.cover_url.empty() || item.duration_sec <= 0.0) &&
                    caps.has("media.source.v1")) {
                    nlohmann::json meta_req = {
                        {"action", "metadata"},
                        {"id", am.song_id},
                        {"url", am.url},
                    };
                    if (!am.storefront.empty()) {
                        meta_req["storefront"] = am.storefront;
                    }
                    auto meta_res = caps.callProvider("plugin-music", "media.source.v1", meta_req);
                    if (!meta_res.ok && meta_res.error.find("not found") != std::string::npos) {
                        meta_res = caps.call("media.source.v1", meta_req);
                    }
                    if (meta_res.ok && meta_res.data.is_object()) {
                        details.cover_url = meta_res.data.value("cover_url", std::string{});
                        details.duration_sec = meta_res.data.value("duration_sec", 0.0);
                        if (item.quality_label.empty()) {
                            details.quality_label = meta_res.data.value("quality_label", std::string{});
                        }
                    }
                }
                if (!cancel->isCancelled() && item.source_id == "bilibili" && caps.has("media.danmaku.v1")) {
                    auto dm_res = caps.call("media.danmaku.v1", {{"bvid", item.id}});
                    if (dm_res.ok && dm_res.data.contains("items") && dm_res.data["items"].is_array()) {
                        for (const auto& row : dm_res.data["items"]) {
                            if (!row.is_object()) continue;
                            details.danmaku.push_back(DanmakuCue{
                                row.value("time_sec", 0.0),
                                row.value("mode", 1),
                                row.value("color", uint32_t{0xFFFFFF}),
                                row.value("text", std::string{}),
                            });
                        }
                    }
                }
                for (const auto& source : sources) {
                    if (cancel->isCancelled()) break;
                    if (!item.source_id.empty() && source->sourceId() != item.source_id) continue;
                    if (details.lyrics.empty()) details.lyrics = source->fetchLyrics(item.id);
                    if (cancel->isCancelled()) break;
                    if (details.chapters.empty()) details.chapters = source->fetchChapters(item.id);
                }
                if (!cancel->isCancelled() && details.lyrics.empty() && am.is_apple_music) {
                    const std::string lookup_id =
                        !am.storefront.empty() ? ("am-" + am.storefront + "-" + am.song_id) : am.song_id;
                    for (const auto& source : sources) {
                        if (cancel->isCancelled()) break;
                        if (source->sourceId() != "plugin-music") continue;
                        details.lyrics = source->fetchLyrics(lookup_id);
                        break;
                    }
                    if (!cancel->isCancelled() && details.lyrics.empty() && caps.has("media.source.v1")) {
                        nlohmann::json lyr_req = {
                            {"action", "lyrics"},
                            {"id", am.song_id},
                            {"url", am.url},
                        };
                        if (!am.storefront.empty()) {
                            lyr_req["storefront"] = am.storefront;
                        }
                        auto lyr_res = caps.callProvider("plugin-music", "media.source.v1", lyr_req);
                        if (!lyr_res.ok && lyr_res.error.find("not found") != std::string::npos) {
                            lyr_res = caps.call("media.source.v1", lyr_req);
                        }
                        if (lyr_res.ok && lyr_res.data.is_object()) {
                            const nlohmann::json* larr = nullptr;
                            if (lyr_res.data.contains("lyrics") && lyr_res.data["lyrics"].is_array()) {
                                larr = &lyr_res.data["lyrics"];
                            } else if (lyr_res.data.contains("lines") && lyr_res.data["lines"].is_array()) {
                                larr = &lyr_res.data["lines"];
                            }
                            if (larr) {
                                for (const auto& l : *larr) {
                                    if (!l.is_object()) continue;
                                    details.lyrics.push_back(LyricLine{
                                        l.value("time_sec", 0.0),
                                        l.value("original", std::string{}),
                                        l.value("translation", std::string{}),
                                    });
                                }
                            }
                        }
                    }
                }
                if (!cancel->isCancelled() && caps.has("player.timeline.v1") && item.kind != "music") {
                    std::string timeline_vid = item.id;
                    uint64_t timeline_cid = 0;
                    if (item.extra.is_object()) {
                        for (const char* key : {"bvid", "youtube_id", "video_id"}) {
                            if (item.extra.contains(key) && item.extra[key].is_string() &&
                                !item.extra[key].get_ref<const std::string&>().empty()) {
                                timeline_vid = item.extra[key].get<std::string>();
                                break;
                            }
                        }
                        if (item.extra.contains("cid")) {
                            if (item.extra["cid"].is_number_integer() || item.extra["cid"].is_number_unsigned()) {
                                timeline_cid = item.extra["cid"].get<uint64_t>();
                            }
                        }
                    }
                    if (!timeline_vid.empty() && timeline_vid.rfind("demo-", 0) != 0) {
                        nlohmann::json sb_req{
                            {"action", "fetch_segments"},
                            {"video_id", timeline_vid},
                            {"platform", item.source_id.empty() ? "auto" : item.source_id},
                            {"duration_sec", item.duration_sec},
                            {"existing_chapters", toJsonArray(details.chapters)},
                        };
                        if (timeline_cid > 0) {
                            sb_req["cid"] = timeline_cid;
                        }
                        auto sb_res = caps.callCancellable("player.timeline.v1", sb_req, cancel);
                        if (sb_res.ok && sb_res.data.is_object() &&
                            sb_res.data.contains("chapters") && sb_res.data["chapters"].is_array()) {
                            details.chapters = fromJsonArray<ChapterSegment>(sb_res.data["chapters"]);
                        }
                    }
                }
                return details;
            },
            [this, alive = alive_, req, item_id = item.id, source_id = item.source_id](auto details) {
                if (!alive->load(std::memory_order_acquire)) return;
                if (detail_request_ == req) {
                    detail_request_.reset();
                }
                if (active_item_.id != item_id || active_item_.source_id != source_id) return;
                if (!details.lyrics.empty()) {
                    active_lyrics_ = std::move(details.lyrics);
                }
                active_danmaku_ = std::move(details.danmaku);
                active_chapters_ = std::move(details.chapters);
                if (!details.cover_url.empty() && active_item_.cover_url.empty()) {
                    active_item_.cover_url = std::move(details.cover_url);
                }
                if (details.duration_sec > 0.0 && active_item_.duration_sec <= 0.0) {
                    active_item_.duration_sec = details.duration_sec;
                }
                if (!details.quality_label.empty() && active_item_.quality_label.empty()) {
                    active_item_.quality_label = details.quality_label;
                }
                for (auto& it : current_items_) {
                    if (it.id == active_item_.id && it.source_id == active_item_.source_id) {
                        if (it.cover_url.empty() && !active_item_.cover_url.empty()) {
                            it.cover_url = active_item_.cover_url;
                        }
                        if (it.duration_sec <= 0.0 && active_item_.duration_sec > 0.0) {
                            it.duration_sec = active_item_.duration_sec;
                        }
                        if (it.quality_label.empty() && !active_item_.quality_label.empty()) {
                            it.quality_label = active_item_.quality_label;
                        }
                    }
                }
                if (details.has_stream) {
                    active_item_.play_url = std::move(details.play_url);
                    if (!details.quality_label.empty()) {
                        active_item_.quality_label = std::move(details.quality_label);
                    }
                    if (!active_item_.extra.is_object()) active_item_.extra = nlohmann::json::object();
                    if (!details.audio_url.empty()) {
                        active_item_.extra["audio_url"] = std::move(details.audio_url);
                    }
                    if (active_item_.source_id == "bilibili") {
                        if (details.android_media) {
                            active_item_.extra.erase("http_headers");
                            active_item_.extra["referrer"] = "";
                            active_item_.extra["user_agent"] = "Bilibili Freedoooooom/MarkII";
                        } else {
                            active_item_.extra["referrer"] = "https://www.bilibili.com";
                            active_item_.extra["user_agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36";
                        }
                    } else if (active_item_.source_id == "youtube") {
                        active_item_.extra.erase("referrer");
                        active_item_.extra["user_agent"] =
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36";
                    }
                }
                rebuildSubContent();
            },
            [this, alive = alive_, req]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (detail_request_ == req) {
                    detail_request_.reset();
                }
            });
        kernel.bus().publish("entity.media.open", {
                                                      {"id", item.id},
                                                      {"title", item.title},
                                                      {"creator", item.creator},
                                                      {"kind", item.kind},
                                                      {"source_id", item.source_id},
                                                      {"play_url", item.play_url},
                                                      {"cover_url", item.cover_url},
                                                      {"extra", item.extra},
                                                      {"origin", "host_workbench"},
                                                  });
        kernel.bus().publish("host.focus_changed", {
                                                       {"domain", "media"},
                                                       {"id", item.id},
                                                       {"title", item.title},
                                                       {"subtitle", item.creator},
                                                       {"author", item.creator},
                                                       {"kind", item.kind},
                                                       {"source_id", item.source_id},
                                                       {"page_number", 0},
                                                       {"play_url", item.play_url},
                                                       {"extra", item.extra},
                                                   });
    }
    if (on_entity_focus_) {
        std::string summary = item.title;
        if (!item.kind.empty()) summary += " · " + item.kind;
        if (!item.creator.empty()) summary += " · " + item.creator;
        if (item.duration_sec > 0.0) {
            summary += " · 进度 " + std::to_string(static_cast<int>(item.progress_sec)) +
                       "s/" + std::to_string(static_cast<int>(item.duration_sec)) + "s";
        }
        on_entity_focus_(std::move(summary));
    }
    if (switch_to_player) {
        sub_mode_ = (item.kind == "music") ? SubMode::MusicLyrics : SubMode::PlayerStage;
    }
    rebuildTabs();
    rebuildSubContent();
}

void MediaWorkbench::selectAdjacentMusicTrack(int delta) {
    const auto pool = visibleMusicItems();
    if (pool.empty()) {
        brls::Application::notify("当前曲库暂无可切换曲目");
        return;
    }
    int cur = delta > 0 ? -1 : 0;
    if (has_active_item_) {
        for (size_t i = 0; i < pool.size(); ++i) {
            if (pool[i].id == active_item_.id && pool[i].source_id == active_item_.source_id) {
                cur = static_cast<int>(i);
                break;
            }
        }
    }
    const int n = static_cast<int>(pool.size());
    const int next_idx = ((static_cast<int>(cur) + delta) % n + n) % n;
    const MediaItem& target = pool[static_cast<size_t>(next_idx)];
    last_loaded_stream_key_.clear();
    selectItem(target, true);
    if (isPlayableOrResolvableItem(target)) {
        auto opts = buildPlaybackOptions(active_item_);
        last_loaded_stream_key_ =
            active_item_.source_id + ":" + active_item_.id + "|" + opts.url + "|" + opts.audio_url;
        playVisibleMusic();
    }
}

void MediaWorkbench::updateActiveLyricHighlight(double position_sec) {
    if (sub_mode_ != SubMode::MusicLyrics || lyric_cards_.empty() ||
        lyric_cards_.size() != lyric_times_.size() ||
        lyric_orig_labels_.size() != lyric_times_.size()) {
        return;
    }
    int idx = -1;
    for (size_t i = 0; i < lyric_times_.size(); ++i) {
        if (position_sec >= lyric_times_[i]) {
            idx = static_cast<int>(i);
        } else {
            break;
        }
    }
    if (idx == active_lyric_idx_) {
        return;
    }
    if (active_lyric_idx_ >= 0 && static_cast<size_t>(active_lyric_idx_) < lyric_cards_.size()) {
        const size_t prev_i = static_cast<size_t>(active_lyric_idx_);
        const bool is_sel = (active_lyric_idx_ == selected_lyric_idx_);
        lyric_cards_[prev_i]->setBackgroundColor(nvgRGBA(0, 0, 0, 0));
        lyric_cards_[prev_i]->setBorderThickness(is_sel ? 1.0f : 0.0f);
        if (is_sel) lyric_cards_[prev_i]->setBorderColor(ui::color("brls/accent"));
        lyric_orig_labels_[prev_i]->setTextColor(ui::color("brls/text"));
        lyric_orig_labels_[prev_i]->setFontSize(16.0f);
        if (prev_i < lyric_trans_labels_.size() && lyric_trans_labels_[prev_i]) {
            lyric_trans_labels_[prev_i]->setTextColor(ui::color("font/grey"));
            lyric_trans_labels_[prev_i]->setFontSize(14.2f);
        }
    }
    active_lyric_idx_ = idx;
    if (active_lyric_idx_ >= 0 && static_cast<size_t>(active_lyric_idx_) < lyric_cards_.size()) {
        const size_t cur_i = static_cast<size_t>(active_lyric_idx_);
        lyric_cards_[cur_i]->setBackgroundColor(ui::color("desktop/active"));
        lyric_cards_[cur_i]->setBorderThickness(0.0f);
        lyric_orig_labels_[cur_i]->setTextColor(ui::color("brls/accent"));
        lyric_orig_labels_[cur_i]->setFontSize(18.5f);
        if (cur_i < lyric_trans_labels_.size() && lyric_trans_labels_[cur_i]) {
            lyric_trans_labels_[cur_i]->setTextColor(ui::color("brls/text"));
            lyric_trans_labels_[cur_i]->setFontSize(15.0f);
        }

        // Smoothly keep active lyric near upper-center of Fluid-Cloud scroll when user isn't browsing it
        if (lyrics_scroll_ && !focusIsInsideView(lyrics_scroll_)) {
            const float scroll_h = lyrics_scroll_->getHeight();
            const float row_y = lyric_cards_[cur_i]->getY();
            const float sf_y = lyrics_scroll_->getY();
            if (scroll_h > 60.0f && row_y > 0.0f) {
                const float content_y = (row_y - sf_y) + lyrics_scroll_->getContentOffsetY();
                const float target_off = std::max(0.0f, content_y - scroll_h * 0.34f);
                lyrics_scroll_->setContentOffsetY(target_off, true);
            }
        }

        if (lyric_tutor_explanation_.empty() && lyric_tutor_target_label_ &&
            cur_i < active_lyrics_.size()) {
            std::string preview = active_lyrics_[cur_i].original;
            if (!active_lyrics_[cur_i].translation.empty() &&
                active_lyrics_[cur_i].translation != active_lyrics_[cur_i].original) {
                preview += "  ｜  " + active_lyrics_[cur_i].translation;
            }
            lyric_tutor_target_label_->setText("▶ " + preview);
            lyric_tutor_target_label_->setVisibility(brls::Visibility::VISIBLE);
        }
    }
}

void MediaWorkbench::triggerAiLyricsTranslate() {
    if (!has_active_item_ || active_lyrics_.empty()) {
        brls::Application::notify("当前曲目暂无可翻译的歌词");
        return;
    }
    if (lyric_translating_) {
        brls::Application::notify("AI 正在补译整首歌词，请稍候…");
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    if (lyric_translate_request_) {
        lyric_translate_request_->cancel();
    }
    auto req = CancelToken::create();
    lyric_translate_request_ = req;
    lyric_translating_ = true;
    if (sub_mode_ == SubMode::MusicLyrics) {
        rebuildSubContent();
    }

    const MediaItem item_snap = active_item_;
    const std::vector<LyricLine> lines_snap = active_lyrics_;
    const auto am = extractAppleMusicLookup(item_snap);

    loadUiInBackground(
        kernel.uiTasks(),
        "media.lyrics_translate",
        req,
        [item_snap, lines_snap, am](const auto& cancel) {
            CapabilityResult out;
            if (cancel && cancel->isCancelled()) return out;
            auto& caps = HostKernel::instance().capabilities();

            nlohmann::json lines_json = nlohmann::json::array();
            for (const auto& l : lines_snap) {
                lines_json.push_back({
                    {"time_sec", l.time_sec},
                    {"original", l.original},
                    {"translation", l.translation},
                });
            }

            if (caps.has("media.lyrics.v1")) {
                nlohmann::json lreq = {
                    {"action", "translate"},
                    {"id", !am.song_id.empty() ? am.song_id : item_snap.id},
                    {"title", item_snap.title},
                    {"artist", item_snap.creator},
                    {"target_language", "zh-CN"},
                    {"lines", lines_json},
                };
                out = caps.callCancellable("media.lyrics.v1", lreq, cancel);
                if (out.ok) return out;
            }

            // Direct LLM fallback when media.lyrics.v1 is unavailable
            if (caps.has("llm.chat.v1") || caps.has("ai.agent.v1")) {
                nlohmann::json input_arr = nlohmann::json::array();
                std::unordered_map<std::string, std::vector<size_t>> uniq_map;
                std::vector<std::string> uniq_texts;
                for (size_t i = 0; i < lines_snap.size(); ++i) {
                    const std::string t = trimSpaces(lines_snap[i].original);
                    if (t.empty() || !lines_snap[i].translation.empty()) continue;
                    auto& b = uniq_map[t];
                    if (b.empty()) {
                        input_arr.push_back({{"id", std::to_string(uniq_texts.size())}, {"text", t}});
                        uniq_texts.push_back(t);
                    }
                    b.push_back(i);
                }
                if (uniq_texts.empty()) {
                    out.ok = true;
                    out.data = {{"lines", lines_json}};
                    return out;
                }
                const std::string sys_prompt =
                    "Translate song lyrics faithfully into natural Simplified Chinese (zh-CN). "
                    "Return ONLY a JSON array of objects with exact matching id and translated text fields.";
                const std::string usr_prompt =
                    "Track: " + item_snap.title + " - " + item_snap.creator + "\n" + input_arr.dump();
                CapabilityResult chat_res;
                if (caps.has("llm.chat.v1")) {
                    chat_res = caps.callCancellable(
                        "llm.chat.v1",
                        {{"system_prompt", sys_prompt}, {"prompt", usr_prompt}, {"temperature", 0.2}},
                        cancel);
                } else {
                    chat_res = caps.callCancellable(
                        "ai.agent.v1",
                        {{"action", "chat"}, {"domain", "media"}, {"prompt", sys_prompt + "\n\n" + usr_prompt}},
                        cancel);
                }
                if (!chat_res.ok) return chat_res;
                std::string reply = chat_res.data.value("reply", chat_res.data.value("text", ""));
                size_t lb = reply.find('[');
                size_t rb = reply.rfind(']');
                if (lb != std::string::npos && rb != std::string::npos && rb > lb) {
                    auto parsed = nlohmann::json::parse(reply.substr(lb, rb - lb + 1), nullptr, false);
                    if (parsed.is_array()) {
                        for (size_t k = 0; k < parsed.size() && k < uniq_texts.size(); ++k) {
                            std::string tr_txt;
                            if (parsed[k].is_object()) {
                                tr_txt = parsed[k].value("text", parsed[k].value("translation", ""));
                            } else if (parsed[k].is_string()) {
                                tr_txt = parsed[k].get<std::string>();
                            }
                            if (!tr_txt.empty()) {
                                for (size_t li : uniq_map[uniq_texts[k]]) {
                                    lines_json[li]["translation"] = tr_txt;
                                }
                            }
                        }
                        out.ok = true;
                        out.data = {{"lines", std::move(lines_json)}};
                        return out;
                    }
                }
                out.ok = false;
                out.error = "AI 返回的歌词翻译 JSON 解析失败";
                return out;
            }

            out.ok = false;
            out.error = "未检测到 AI 翻译插件（请启用 plugin-magpie-agent）";
            return out;
        },
        [this, alive = alive_, req, item_id = item_snap.id](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (lyric_translate_request_ == req) {
                lyric_translate_request_.reset();
            }
            lyric_translating_ = false;
            if (!has_active_item_ || active_item_.id != item_id) return;

            if (!res.ok) {
                brls::Application::notify("歌词翻译未完成: " + (res.error.empty() ? "请检查 AI 配置" : res.error));
                if (sub_mode_ == SubMode::MusicLyrics) rebuildSubContent();
                return;
            }
            if (res.data.is_object() && res.data.contains("lines") && res.data["lines"].is_array()) {
                std::vector<LyricLine> next_lines;
                for (const auto& l : res.data["lines"]) {
                    if (!l.is_object()) continue;
                    double tsec = 0.0;
                    if (l.contains("time_sec") && l["time_sec"].is_number()) {
                        tsec = l["time_sec"].get<double>();
                    }
                    next_lines.push_back(LyricLine{
                        tsec,
                        l.value("original", std::string{}),
                        l.value("translation", std::string{}),
                    });
                }
                if (!next_lines.empty()) {
                    active_lyrics_ = std::move(next_lines);
                    active_item_.has_bilingual_sub = true;
                }
            }
            brls::Application::notify("流体云双语歌词已由 AI 补全");
            if (sub_mode_ == SubMode::MusicLyrics) {
                rebuildSubContent();
            }
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (lyric_translate_request_ == req) {
                lyric_translate_request_.reset();
            }
            lyric_translating_ = false;
        });
}

void MediaWorkbench::explainLyricLineWithAi(size_t line_index) {
    if (!has_active_item_ || line_index >= active_lyrics_.size()) {
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    selected_lyric_idx_ = static_cast<int>(line_index);
    lyric_tutor_expanded_ = true;
    const LyricLine target = active_lyrics_[line_index];
    const std::string prev_line = (line_index > 0) ? active_lyrics_[line_index - 1].original : "";
    const std::string next_line =
        (line_index + 1 < active_lyrics_.size()) ? active_lyrics_[line_index + 1].original : "";

    std::string target_header = target.original;
    if (!target.translation.empty() && target.translation != target.original) {
        target_header += "  ｜  " + target.translation;
    }
    lyric_tutor_target_ = target_header;

    for (size_t i = 0; i < lyric_cards_.size(); ++i) {
        const bool is_act = (static_cast<int>(i) == active_lyric_idx_);
        const bool is_sel = (i == line_index);
        if (!is_act) {
            lyric_cards_[i]->setBorderThickness(is_sel ? 1.0f : 0.0f);
            if (is_sel) lyric_cards_[i]->setBorderColor(ui::color("brls/accent"));
        }
    }

    const std::string cache_key = active_item_.id + "|" + target.original;
    if (auto it = lyric_explain_cache_.find(cache_key); it != lyric_explain_cache_.end() && !it->second.empty()) {
        lyric_explaining_ = false;
        lyric_tutor_provider_ = "AI 精讲 · 已缓存";
        lyric_tutor_explanation_ = it->second;
        if (lyric_tutor_target_label_) {
            lyric_tutor_target_label_->setText("▶ " + lyric_tutor_target_);
            lyric_tutor_target_label_->setVisibility(brls::Visibility::VISIBLE);
        }
        if (lyric_tutor_status_label_) {
            lyric_tutor_status_label_->setText(lyric_tutor_provider_);
        }
        if (lyric_tutor_body_label_) {
            lyric_tutor_body_label_->setTextColor(ui::color("brls/text"));
            lyric_tutor_body_label_->setText(lyric_tutor_explanation_);
        } else if (sub_mode_ == SubMode::MusicLyrics) {
            rebuildSubContent();
        }
        return;
    }

    if (lyric_explain_request_) {
        lyric_explain_request_->cancel();
    }
    auto req = CancelToken::create();
    lyric_explain_request_ = req;
    lyric_explaining_ = true;
    lyric_tutor_provider_ = "AI 流式生成中…";
    lyric_tutor_explanation_ = "正在调用 AI 解析句意、词汇俚语与演唱语境…";

    if (!lyric_tutor_body_label_ && sub_mode_ == SubMode::MusicLyrics) {
        rebuildSubContent();
    } else {
        if (lyric_tutor_target_label_) {
            lyric_tutor_target_label_->setText("▶ " + lyric_tutor_target_);
            lyric_tutor_target_label_->setVisibility(brls::Visibility::VISIBLE);
        }
        if (lyric_tutor_status_label_) {
            lyric_tutor_status_label_->setText(lyric_tutor_provider_);
        }
        if (lyric_tutor_body_label_) {
            lyric_tutor_body_label_->setTextColor(ui::color("font/grey"));
            lyric_tutor_body_label_->setText(lyric_tutor_explanation_);
        }
    }

    const MediaItem item_snap = active_item_;
    std::weak_ptr<std::atomic<bool>> alive_weak = alive_;

    loadUiInBackground(
        kernel.uiTasks(),
        "media.lyric_explain",
        req,
        [this, alive_weak, req, item_snap, target, prev_line, next_line](const auto& cancel) {
            struct ExplainOutcome {
                bool ok = false;
                std::string text;
                std::string provider;
                std::string error;
            } out;
            if (cancel && cancel->isCancelled()) return out;
            auto& caps = HostKernel::instance().capabilities();

            std::ostringstream prompt;
            prompt << "请用简体中文精讲这句歌词（紧凑清晰，适合边听边看）：\n"
                   << "歌曲: " << item_snap.title;
            if (!item_snap.creator.empty()) prompt << " — " << item_snap.creator;
            prompt << "\n";
            if (!prev_line.empty()) prompt << "上句: " << prev_line << "\n";
            prompt << "当前句: " << target.original << "\n";
            if (!target.translation.empty()) prompt << "已有译文: " << target.translation << "\n";
            if (!next_line.empty()) prompt << "下句: " << next_line << "\n";
            prompt << "请分 3 点输出：\n"
                   << "1. 【精准意译与情绪】自然贴合旋律语气的中文翻译及情感基调\n"
                   << "2. 【词汇/俚语/语法】拆解核心词组、口语拼写/方言缩略或句法亮点\n"
                   << "3. 【文化背景与听感】文化意象或连读咬字提示";

            std::string streamed;
            auto pushPartial = [&](const std::string& delta) {
                if (delta.empty()) return;
                streamed += delta;
                const std::string snap = streamed;
                brls::sync([this, alive_weak, req, snap]() {
                    auto a = alive_weak.lock();
                    if (!a || !a->load(std::memory_order_acquire)) return;
                    if (lyric_explain_request_ != req) return;
                    lyric_tutor_explanation_ = snap;
                    if (lyric_tutor_body_label_) {
                        lyric_tutor_body_label_->setTextColor(ui::color("brls/text"));
                        lyric_tutor_body_label_->setText(snap);
                    }
                });
            };

            if (caps.has("llm.chat.v1")) {
                auto res = caps.callStream(
                    "llm.chat.v1",
                    {
                        {"system_prompt", "You are a concise, insightful bilingual music & language tutor."},
                        {"prompt", prompt.str()},
                        {"temperature", 0.35},
                    },
                    [&](const StreamChunk& ch) {
                        if (ch.done) return;
                        if (ch.data.is_object()) {
                            pushPartial(ch.data.value("delta", ch.data.value("text", "")));
                        }
                    },
                    cancel);
                if (res.ok) {
                    std::string full = res.data.is_object()
                                           ? res.data.value("reply", res.data.value("text", streamed))
                                           : streamed;
                    if (full.empty()) full = streamed;
                    if (!full.empty()) {
                        out.ok = true;
                        out.text = std::move(full);
                        out.provider = res.provider_plugin_id.empty() ? "Magpie AI" : res.provider_plugin_id;
                        return out;
                    }
                }
            }

            if (caps.has("media.lyrics.v1")) {
                auto res = caps.callCancellable(
                    "media.lyrics.v1",
                    {
                        {"action", "explain"},
                        {"title", item_snap.title},
                        {"artist", item_snap.creator},
                        {"original", target.original},
                        {"translation", target.translation},
                        {"prev_line", prev_line},
                        {"next_line", next_line},
                        {"target_language", "zh-CN"},
                    },
                    cancel);
                if (res.ok && res.data.is_object()) {
                    const std::string exp = res.data.value("explanation", "");
                    if (!exp.empty()) {
                        out.ok = true;
                        out.text = exp;
                        out.provider = res.data.value("provider", "plugin-music");
                        return out;
                    }
                }
                out.error = res.error;
            } else {
                out.error = "未检测到 AI 能力插件（请启用 plugin-magpie-agent）";
            }
            return out;
        },
        [this, alive = alive_, req, cache_key](auto out) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (lyric_explain_request_ == req) {
                lyric_explain_request_.reset();
            }
            lyric_explaining_ = false;
            if (out.ok && !out.text.empty()) {
                lyric_tutor_provider_ = "AI 精讲 · " + out.provider;
                lyric_tutor_explanation_ = out.text;
                lyric_explain_cache_[cache_key] = out.text;
            } else {
                lyric_tutor_provider_ = "精讲失败";
                lyric_tutor_explanation_ =
                    "未能获取 AI 精讲结果：" + (out.error.empty() ? "请检查 Magpie AI 网关或模型配置" : out.error);
            }
            if (lyric_tutor_status_label_) {
                lyric_tutor_status_label_->setText(lyric_tutor_provider_);
            }
            if (lyric_tutor_body_label_) {
                lyric_tutor_body_label_->setTextColor(
                    out.ok ? ui::color("brls/text") : ui::color("status/warn_fg"));
                lyric_tutor_body_label_->setText(lyric_tutor_explanation_);
            } else if (sub_mode_ == SubMode::MusicLyrics) {
                rebuildSubContent();
            }
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (lyric_explain_request_ == req) {
                lyric_explain_request_.reset();
            }
            lyric_explaining_ = false;
        });
}

void MediaWorkbench::openWordLookupForLyric(size_t line_index) {
    if (!has_active_item_ || line_index >= active_lyrics_.size()) return;
    auto& k = HostKernel::instance();
    SelectionContext sctx;
    sctx.context_sentence = active_lyrics_[line_index].original;
    sctx.source_title = active_item_.title;
    if (!active_item_.kind.empty()) sctx.source_title += " · " + active_item_.kind;
    if (!active_item_.creator.empty()) sctx.source_title += " · " + active_item_.creator;
    sctx.source_id = active_item_.source_id;
    sctx.domain = ContentDomain::Media;
    if (k.isInitialized()) {
        sctx.capabilities = &k.capabilities();
        sctx.bus = &k.bus();
        sctx.ui_tasks = &k.uiTasks();
    }
    SelectionActionPopover::show(std::move(sctx));
}

void MediaWorkbench::loadNasStatus(bool probe_remote) {
    auto& k = HostKernel::instance();
    if (!k.isInitialized() || !k.capabilities().has("nas.connection.v1")) {
        return;
    }
    if (nas_status_request_) {
        nas_status_request_->cancel();
    }
    auto req = CancelToken::create();
    nas_status_request_ = req;
    nas_status_loading_ = true;
    loadUiInBackground(k.uiTasks(), "media.nas_status", req,
        [probe_remote](const auto&) {
            return HostKernel::instance().capabilities().call(
                "nas.connection.v1", {{"action", probe_remote ? "probe" : "status"}, {"probe_remote", probe_remote}});
        },
        [this, alive = alive_, req, probe_remote](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (nas_status_request_ == req) {
                nas_status_request_.reset();
            }
            nas_status_loading_ = false;
            nas_status_loaded_ = true;
            if (res.ok) {
                nas_status_json_ = std::move(res.data);
                if (probe_remote) {
                    brls::Application::notify("飞牛 NAS 远程探测完成");
                }
            } else {
                nas_dir_error_ = res.error;
            }
            if (sub_mode_ == SubMode::NasLibrary) {
                rebuildSubContent();
            }
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (nas_status_request_ && nas_status_request_ != req) return;
            nas_status_request_.reset();
            nas_status_loading_ = false;
            if (sub_mode_ == SubMode::NasLibrary) {
                rebuildSubContent();
            }
        });
}

void MediaWorkbench::loadNasDirectory(const std::string& path) {
    auto& k = HostKernel::instance();
    if (!k.isInitialized() || !k.capabilities().has("nas.connection.v1")) {
        return;
    }
    if (nas_dir_request_) {
        nas_dir_request_->cancel();
    }
    auto req = CancelToken::create();
    nas_dir_request_ = req;
    nas_dir_error_.clear();
    loadUiInBackground(k.uiTasks(), "media.nas_dir", req,
        [path](const auto&) {
            return HostKernel::instance().capabilities().call(
                "nas.connection.v1", {{"action", "list_dir"}, {"path", path}});
        },
        [this, alive = alive_, req, path](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (nas_dir_request_ == req) {
                nas_dir_request_.reset();
            }
            if (res.ok) {
                nas_browse_dir_ = res.data.value("dir", path);
                nas_dir_entries_ = res.data.value("entries", nlohmann::json::array());
                nas_dir_error_.clear();
            } else {
                nas_dir_error_ = res.error.empty() ? "目录读取失败" : res.error;
            }
            if (sub_mode_ == SubMode::NasLibrary) {
                rebuildSubContent();
            }
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (nas_dir_request_ == req) {
                nas_dir_request_.reset();
            }
        });
}

void MediaWorkbench::openExternalMedia(const nlohmann::json& payload) {
    if (!payload.is_object()) return;
    const std::string id = payload.value("item_id", payload.value("id", ""));
    const std::string play_url = payload.value("play_url", "");
    const double start_sec = payload.value("start_sec", 0.0);

    MediaItem target;
    bool found = false;
    if (!id.empty()) {
        for (const auto& it : current_items_) {
            if (it.id == id && (payload.value("source_id", "").empty() ||
                it.source_id == payload.value("source_id", ""))) {
                target = it;
                found = true;
                break;
            }
        }
    }
    if (!found) {
        std::string default_kind = "video";
        if (payload.value("source_id", "") == "plugin-music" ||
            play_url.find("music.apple.com/") != std::string::npos ||
            id.rfind("am-", 0) == 0 || id.rfind("ix-am-", 0) == 0) {
            default_kind = "music";
        }
        target.id = !id.empty() ? id : ("ext:" + play_url);
        target.source_id = payload.value("source_id", "");
        target.title = payload.value("title", "外部跳转媒体");
        target.creator = payload.value("creator", target.source_id);
        target.kind = payload.value("kind", default_kind);
        target.source_badge = payload.value("source_badge", target.source_id);
        target.cover_url = payload.value("cover_url", "");
        target.play_url = play_url;
        target.quality_label = payload.value("quality_label", "");
        target.extra = payload.value("extra", nlohmann::json::object());
        target.duration_sec = payload.value("duration_sec", 0.0);
    }
    if (!play_url.empty()) {
        target.play_url = play_url;
    }
    if (start_sec > 0.0) {
        target.progress_sec = start_sec;
    }
    if (target.id.empty() && target.play_url.empty()) {
        return;
    }

    if (target.kind == "music") {
        if (!isCuratedMedia(target)) music_scope_ = MusicScope::Personal;
        search_query_.clear();
        const auto same = [&target](const MediaItem& it) { return it.id == target.id && it.source_id == target.source_id; };
        if (std::none_of(current_items_.begin(), current_items_.end(), same)) current_items_.push_back(target);
    }
    if (target.kind == "music" && isCuratedMedia(target)) {
        music_scope_ = target.extra.value("curation_mode", "") == "interactive_ephemeral"
            ? MusicScope::Interactive : MusicScope::Daily;
        curation_date_ = target.extra.value("business_date", "");
        archive_all_ = false;
    }
    if (start_sec <= 0.0 && target.extra.is_object() &&
        target.extra.value("is_season_group", false)) {
        if (sub_mode_ != SubMode::Videos && sub_mode_ != SubMode::NasLibrary) {
            setSubMode(SubMode::NasLibrary);
        }
        activateMediaItem(target);
        return;
    }
    last_loaded_stream_key_.clear();
    selectItem(target, true);
    if (target.kind == "music") fetchCurrentSubMode(true);
    if (target.kind == "music" && isPlayableOrResolvableItem(target) && sub_mode_ == SubMode::MusicLyrics) {
        auto opts = buildPlaybackOptions(target);
        last_loaded_stream_key_ = target.source_id + ":" + target.id + "|" + opts.url + "|" + opts.audio_url;
        playVisibleMusic(start_sec > 0.0 ? start_sec : -1.0);
    }
}

void MediaWorkbench::focusPlaybackStage(PlaybackRole role) {
    setSubMode(role == PlaybackRole::BackgroundMusic ? SubMode::MusicLyrics : SubMode::PlayerStage);
}

void MediaWorkbench::showVideoCommentsDialog() {
    if (!has_active_item_ || !HostKernel::instance().isInitialized()) return;
    auto& k = HostKernel::instance();
    auto* box = new brls::Box(brls::Axis::COLUMN);
    box->setPadding(18);
    box->setWidth(760);

    auto* tree = new CommentTreeView();
    tree->setServices(&k.capabilities(), &k.bus(), &k.uiTasks());
    tree->setArticleContext(active_item_.source_id, active_item_.id,
                          active_item_.title, active_item_.creator);
    tree->setViewportHeight(420.0f);
    tree->fetchFromBackend(false);
    box->addView(tree);

    auto* dlg = new brls::Dialog(box);
    dlg->addButton("关闭", [] {});
    dlg->open();
}

void MediaWorkbench::promptSearch() {
    if (auto* plat = brls::Application::getPlatform()) {
        if (sub_mode_ == SubMode::ImdbDiscover) {
            plat->getImeManager()->openForText(
                [this, alive = alive_](std::string q) {
                    brls::sync([this, alive, q = std::move(q)]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        loadImdbDiscover(imdb_chart_, q);
                    });
                },
                "搜索 IMDb 电影 / 剧集 / ttID",
                "输入中英文片名或 IMDb 编号 (如 tt0111161)，留空返回当前榜单",
                160,
                imdb_query_);
            return;
        }
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string q) {
                brls::sync([this, alive, q = std::move(q)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    last_loaded_stream_key_.clear();
                    search_query_ = q;
                    refresh();
                });
            },
            "搜索影视、音乐与 NAS 媒体",
            "按标题或来源过滤当前媒体流",
            128,
            search_query_);
    }
}

void MediaWorkbench::applyDateFilter(const std::string& date, const std::string& domain) {
    if (date.empty()) return;
    last_loaded_stream_key_.clear();
    curation_date_ = date;
    archive_all_ = false;
    if (domain == "music") {
        sub_mode_ = SubMode::MusicLyrics;
        music_scope_ = MusicScope::Daily;
    } else if (domain == "youtube" || domain == "video") {
        sub_mode_ = SubMode::DailyCuration;
        curation_filter_ = 1;
    } else if (sub_mode_ != SubMode::MusicLyrics && sub_mode_ != SubMode::DailyCuration) {
        sub_mode_ = SubMode::DailyCuration;
    }
    refresh();
}

void MediaWorkbench::promptRehydrateOrDateFilter() {
    std::string init_date = curation_date_;
    if (search_query_.rfind("date:", 0) == 0 && search_query_.size() >= 15) {
        init_date = search_query_.substr(5, 10);
    }
    std::string init_dom = "all";
    if (sub_mode_ == SubMode::MusicLyrics) init_dom = "music";
    else if (sub_mode_ == SubMode::Videos) init_dom = "youtube";

    CurationCalendarDialog::open({
        .initial_date = init_date,
        .initial_domain = init_dom,
        .on_apply_date_filter = [this, alive = alive_, init_dom](const std::string& d) {
            if (!alive->load(std::memory_order_acquire)) return;
            applyDateFilter(d, init_dom);
        },
        .on_clear_date_filter = [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            last_loaded_stream_key_.clear();
            curation_date_.clear();
            archive_all_ = true;
            refresh();
        },
    });
}

void MediaWorkbench::triggerArchiveRehydrate() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) {
        brls::Application::notify("未装载 recommend.feed.v1 (plugin-ai-curator)");
        return;
    }
    if (rehydrate_request_) {
        rehydrate_request_->cancel();
    }
    auto req = CancelToken::create();
    rehydrate_request_ = req;
    if (source_status_label_) {
        source_status_label_->setText("正在同步 obsidian-vault 与 music-daily 双仓并回灌历史推荐...");
    }
    loadInBackground(kernel.jobs(), "同步双仓回灌历史推荐", req,
        [](const auto& cancel) {
            return HostKernel::instance().capabilities().callCancellable(
                "recommend.feed.v1",
                {{"action", "bootstrap_and_rehydrate"}, {"pull_remote", true}},
                cancel);
        },
        [this, alive = alive_, req](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
            if (rehydrate_request_ == req) {
                rehydrate_request_.reset();
            }
            if (res.ok) {
                brls::Application::notify("双仓历史推荐回灌完成");
                refresh();
            } else {
                brls::Application::notify("历史回灌失败: " + res.error);
                if (source_status_label_) {
                    source_status_label_->setText("历史回灌失败: " + res.error);
                }
            }
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (rehydrate_request_ == req) {
                rehydrate_request_.reset();
            }
        });
}

void MediaWorkbench::triggerLocalAsrGeneration() {
    if (!HostKernel::instance().isInitialized() || !has_active_item_) {
        brls::Application::notify("请先选择一个媒体条目");
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.capabilities().has("speech.asr.v1")) {
        brls::Application::notify("未加载 speech.asr.v1 本地语音转写插件");
        return;
    }
    if (!active_asr_job_id_.empty()) {
        if (auto prev = kernel.jobs().getJob(active_asr_job_id_)) {
            if (prev->state == JobState::Queued || prev->state == JobState::Running) {
                brls::Application::notify("当前已有 FunASR 字幕任务正在后台执行");
                return;
            }
        }
    }

    const std::string local_path = resolveLocalMediaPath(active_item_);
    if (local_path.empty()) {
        brls::Application::notify("仅支持本地媒体或 NAS 本地路径生成字幕，当前条目为在线流");
        return;
    }

    const std::string item_id = active_item_.id;
    const std::string item_title = active_item_.title.empty() ? "本地媒体" : active_item_.title;
    std::weak_ptr<std::atomic<bool>> alive_weak = alive_;

    JobSpec spec;
    spec.title = "ASR 生成字幕 · " + item_title;
    spec.plugin_id = "plugin-asr-funasr";
    spec.kind = "asr";
    spec.open_target = JobTarget{
        "media",
        "entity.media.open",
        item_id,
        "查看媒体",
        {{"id", item_id}, {"title", item_title}},
    };

    active_asr_job_id_ = kernel.jobs().submit(
        spec,
        [this, alive_weak, local_path, item_id](JobHandle& job, std::string& error) -> bool {
            if (!HostKernel::instance().isInitialized()) {
                error = "宿主内核已关闭";
                return false;
            }
            auto& k = HostKernel::instance();
            std::vector<LyricLine> live_lyrics;
            auto syncSubtitles = [this, alive_weak, item_id](std::vector<LyricLine> snap,
                                                              const std::string& toast) {
                brls::sync([this, alive_weak, item_id, snap = std::move(snap), toast]() {
                    auto alive_sp = alive_weak.lock();
                    if (!alive_sp || !alive_sp->load(std::memory_order_acquire)) {
                        return;
                    }
                    if (!has_active_item_ || active_item_.id != item_id) {
                        if (!toast.empty()) {
                            brls::Application::notify(toast);
                        }
                        return;
                    }
                    active_lyrics_ = snap;
                    if (mpv_stage_) {
                        mpv_stage_->setSubtitles(active_lyrics_);
                    }
                    if (!toast.empty()) {
                        brls::Application::notify(toast);
                    }
                });
            };

            const nlohmann::json req = {
                {"action", "transcribe_media"},
                {"media_path", local_path},
                {"job_id", job.jobId()},
            };
            CapabilityResult res = k.capabilities().callStream(
                "speech.asr.v1",
                req,
                [&](const StreamChunk& ch) {
                    if (ch.done) {
                        return;
                    }
                    if (ch.event == "resumed" || ch.event == "chunk") {
                        const float prog = static_cast<float>(ch.data.value("progress", 0.0));
                        const int done_n = ch.data.value("completed_chunks", 0);
                        const int total_n = ch.data.value("total_chunks", 0);
                        const std::string prefix =
                            (ch.event == "resumed") ? "恢复断点 " : "识别分块 ";
                        job.reportProgress(prog,
                                           prefix + std::to_string(done_n) + "/" +
                                               std::to_string(total_n));
                        if (ch.data.contains("cues") && ch.data["cues"].is_array()) {
                            appendCuesAsLyrics(ch.data["cues"], live_lyrics);
                            syncSubtitles(live_lyrics, "");
                        }
                    } else if (ch.event == "stage") {
                        job.reportProgress(static_cast<float>(ch.data.value("progress", 0.0)),
                                           ch.data.value("message", std::string{}));
                    }
                },
                job.cancelToken());

            if (!res.ok) {
                error = res.error.empty() ? "FunASR 字幕生成失败" : res.error;
                if (!res.cancelled) {
                    brls::sync([alive_weak, error]() {
                        auto alive_sp = alive_weak.lock();
                        if (alive_sp && alive_sp->load(std::memory_order_acquire)) {
                            brls::Application::notify("FunASR 字幕生成失败: " + error);
                        }
                    });
                }
                return false;
            }

            if (res.data.contains("cues") && res.data["cues"].is_array()) {
                std::vector<LyricLine> merged;
                appendCuesAsLyrics(res.data["cues"], merged);
                syncSubtitles(std::move(merged),
                              "FunASR 字幕已加载到播放器");
            } else {
                syncSubtitles(std::move(live_lyrics),
                              "FunASR 字幕已加载到播放器");
            }
            job.reportProgress(1.0f, "字幕生成完成");
            return true;
        });

    brls::Application::notify("已提交后台字幕生成任务，可在顶部状态栏实时查看进度或取消");
}

void MediaWorkbench::triggerSubHunterMatch() {
    if (!HostKernel::instance().isInitialized() || !has_active_item_) {
        brls::Application::notify("请先选择一个媒体条目");
        return;
    }
    auto& kernel = HostKernel::instance();
    if (!kernel.capabilities().has("subtitle.fetch.v1")) {
        brls::Application::notify("未加载 subtitle.fetch.v1 字幕搜索插件");
        return;
    }
    if (!active_sub_job_id_.empty()) {
        if (auto prev = kernel.jobs().getJob(active_sub_job_id_)) {
            if (prev->state == JobState::Queued || prev->state == JobState::Running) {
                brls::Application::notify("当前已有 SubHunter 字幕任务正在后台执行");
                return;
            }
        }
    }

    const std::string local_path = resolveLocalMediaPath(active_item_);
    const std::string item_id = active_item_.id;
    const std::string item_title = active_item_.title.empty() ? "媒体字幕" : active_item_.title;
    if (local_path.empty() && item_title.empty()) {
        brls::Application::notify("缺少可用于搜索字幕的媒体路径或标题");
        return;
    }

    std::weak_ptr<std::atomic<bool>> alive_weak = alive_;
    JobSpec spec;
    spec.title = "SubHunter 匹配字幕 · " + item_title;
    spec.plugin_id = "plugin-sub-hunter";
    spec.kind = "subtitle";
    spec.open_target = JobTarget{
        "media",
        "entity.media.open",
        item_id,
        "查看媒体",
        {{"id", item_id}, {"title", item_title}},
    };

    active_sub_job_id_ = kernel.jobs().submit(
        spec,
        [this, alive_weak, local_path, item_id, item_title](JobHandle& job, std::string& error) -> bool {
            if (!HostKernel::instance().isInitialized()) {
                error = "宿主内核已关闭";
                return false;
            }
            auto& k = HostKernel::instance();
            job.reportProgress(0.15f, "正在检索 SubHD / ASSRT 字幕...");
            nlohmann::json search_req = {
                {"action", "search"},
                {"provider", "all"},
            };
            if (!local_path.empty()) {
                search_req["filename"] = local_path;
            } else {
                search_req["query"] = item_title;
            }

            CapabilityResult sres = k.capabilities().callCancellable(
                "subtitle.fetch.v1",
                search_req,
                job.cancelToken());
            if (!sres.ok) {
                error = sres.error.empty() ? "SubHunter 检索字幕失败" : sres.error;
                return false;
            }

            const auto cands = sres.data.value("candidates", nlohmann::json::array());
            if (!cands.is_array() || cands.empty() || !cands[0].is_object()) {
                error = "未检索到匹配的在线字幕";
                return false;
            }

            job.reportProgress(0.45f, "正在下载首选匹配字幕...");
            CapabilityResult dres = k.capabilities().callCancellable(
                "subtitle.fetch.v1",
                {
                    {"action", "download"},
                    {"provider", cands[0].value("provider", std::string{})},
                    {"id", cands[0].value("id", std::string{})},
                },
                job.cancelToken());
            if (!dres.ok) {
                error = dres.error.empty() ? "SubHunter 下载字幕失败" : dres.error;
                return false;
            }

            const auto files = dres.data.value("files", nlohmann::json::array());
            if (!files.is_array() || files.empty() || !files[0].is_object()) {
                error = "下载包内未包含可用文本字幕";
                return false;
            }

            std::string final_sub_path = files[0].value("path", std::string{});
            int cue_count = files[0].value("cue_count", 0);

            if (!local_path.empty() && !final_sub_path.empty() &&
                k.capabilities().has("subtitle.sync.v1")) {
                job.reportProgress(0.75f, "正在自动对齐字幕时间轴...");
                CapabilityResult sync_res = k.capabilities().callStream(
                    "subtitle.sync.v1",
                    {
                        {"action", "sync"},
                        {"subtitle_path", final_sub_path},
                        {"reference_path", local_path},
                        {"mode", "auto"},
                    },
                    [&](const StreamChunk& ch) {
                        if (!ch.done && ch.event == "stage") {
                            job.reportProgress(static_cast<float>(ch.data.value("progress", 0.75)),
                                               ch.data.value("message", std::string{}));
                        }
                    },
                    job.cancelToken());
                if (sync_res.ok && !sync_res.data.value("output_path", std::string{}).empty()) {
                    final_sub_path = sync_res.data["output_path"].get<std::string>();
                    cue_count = sync_res.data.value("cue_count", cue_count);
                }
            }

            if (final_sub_path.empty()) {
                error = "未生成有效外挂字幕路径";
                return false;
            }

            brls::sync([this, alive_weak, item_id, final_sub_path, cue_count]() {
                auto alive_sp = alive_weak.lock();
                if (!alive_sp || !alive_sp->load(std::memory_order_acquire)) return;
                if (!has_active_item_ || active_item_.id != item_id) return;
                if (!active_item_.extra.is_object()) active_item_.extra = nlohmann::json::object();
                active_item_.extra["subtitle_urls"] = nlohmann::json::array({final_sub_path});
                active_item_.has_bilingual_sub = true;
                if (mpv_stage_) {
                    MediaRequestOptions opts = buildPlaybackOptions(active_item_);
                    if (!opts.url.empty()) {
                        mpv_stage_->loadMedia(opts, active_lyrics_, active_chapters_, active_item_.title);
                    }
                }
                brls::Application::notify("SubHunter 已挂载字幕 (" + std::to_string(cue_count) + " 条): " +
                                          std::filesystem::path(final_sub_path).filename().string());
            });
            job.reportProgress(1.0f, "字幕匹配完成");
            return true;
        });

    brls::Application::notify("已提交后台 SubHunter 字幕匹配任务");
}

} // namespace omnidesk::host
