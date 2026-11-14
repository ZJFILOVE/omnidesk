#include "omnidesk/host/media_workbench.hpp"
#include "omnidesk/host/async_load.hpp"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <filesystem>
#include <mutex>
#include <sstream>
#include <unordered_map>
#include <utility>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/mpv_core.hpp"
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
    int total = static_cast<int>(sec);
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
        if (item.extra.contains("subtitle_urls") && item.extra["subtitle_urls"].is_array()) {
            for (const auto& s : item.extra["subtitle_urls"]) {
                if (s.is_string() && !s.get_ref<const std::string&>().empty()) {
                    opts.subtitle_urls.push_back(s.get<std::string>());
                }
            }
        }
    }
    return opts;
}

std::vector<MediaItem> buildShowcaseMedia() {
    return {
        {"demo-bili-1", "plugin-bilibili", "从 wiliwili 到 OmniDesk：微内核桌面工作台设计实践", "TechTalk Lab",
         "video", "Bilibili · 预览", "", "", "4K 60fps HEVC", 1425, 310, 184200, true},
        {"demo-yt-1", "plugin-youtube", "Systems Architecture of Decoupled C++ Desktop Workbenches", "Kernel Notes",
         "video", "YouTube · 预览", "", "", "2160p VP9", 2180, 0, 92400, true},
        {"demo-nas-1", "plugin-fnos-nas", "飞牛 fnOS 私有影库 · 纪录片原盘直连串流", "Home NAS",
         "nas", "飞牛 NAS · 预览", "", "", "Dolby Atmos · SMB/WebDAV", 3120, 890, 0, true},
        {"demo-music-1", "plugin-music-lyrics", "Clair de Lune (Bilingual Study Edition)", "Debussy / Studio",
         "music", "Music · FLAC", "", "", "FLAC 24bit / 96kHz", 304, 72, 0, true},
        {"demo-music-2", "plugin-music-lyrics", "Midnight Architecture · LRC 双语逐行同步示例", "Acoustic Ensemble",
         "music", "Music · Hi-Res", "", "", "ALAC Lossless", 248, 0, 0, true},
    };
}

} // namespace

MediaWorkbench::MediaWorkbench(std::function<void(const std::string&)> on_entity_focus,
                               std::function<void()> on_open_marketplace)
    : brls::Box(brls::Axis::COLUMN),
      on_entity_focus_(std::move(on_entity_focus)),
      on_open_marketplace_(std::move(on_open_marketplace)) {
    setGrow(1.0f);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    auto* header = new brls::Box(brls::Axis::ROW);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(12);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->addView(ui::makeLabel("影视与音乐 · Media Workbench", 24.0f, "brls/text"));
    source_status_label_ = ui::makeLabel("16:9 影视卡片 · 双语歌词工作室 · fnOS NAS · MPV 播放器舞台", 14.5f, "font/grey");
    source_status_label_->setMarginTop(3);
    left->addView(source_status_label_);
    header->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    auto* s_btn = ui::makeButton("搜索片库 /", [this]() { promptSearch(); }, false, 36.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    auto* m_btn = ui::makeButton("管理影视插件", [this]() {
        if (on_open_marketplace_) on_open_marketplace_();
    }, false, 36.0f);
    right->addView(m_btn);
    header->addView(right);
    addView(header);

    sub_tabs_box_ = new brls::Box(brls::Axis::ROW);
    sub_tabs_box_->setMarginBottom(14);
    addView(sub_tabs_box_);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setGrow(1.0f);
    scroll->setWidthPercentage(100);
    stage_box_ = new brls::Box(brls::Axis::COLUMN);
    stage_box_->setWidthPercentage(100);
    scroll->setContentView(stage_box_);
    addView(scroll);

    mpv_state_sub_ = MPVCore::instance().subscribeState(
        [this, alive = alive_](const MpvPlaybackState& st) {
            brls::sync([this, alive, pos = st.position_sec]() {
                if (!alive->load(std::memory_order_acquire)) return;
                updateActiveLyricHighlight(pos);
            });
        });

    refresh();
}

MediaWorkbench::~MediaWorkbench() {
    detail_request_->cancel();
    nas_request_->cancel();
    if (mpv_state_sub_ != 0) {
        MPVCore::instance().unsubscribeState(mpv_state_sub_);
        mpv_state_sub_ = 0;
    }
    alive_->store(false, std::memory_order_release);
    cancelActiveFetch();
    mpv_stage_ = nullptr;
}

bool MediaWorkbench::cancelActiveFetch() {
    if (active_fetches_.empty()) {
        return false;
    }
    for (const auto& tok : active_fetches_) {
        if (tok) {
            tok->cancel();
        }
    }
    active_fetches_.clear();
    ++fetch_seq_;
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
        {SubMode::MusicLyrics, "音乐与双语歌词"},
        {SubMode::NasLibrary, "飞牛 fnOS 私有影库"},
        {SubMode::PlayerStage, "MPV 播放舞台 & 章节"},
    };
    for (const auto& t : tabs) {
        auto* btn = ui::makeButton(t.label, [this, m = t.mode]() {
            brls::sync([this, m]() { setSubMode(m); });
        }, sub_mode_ == t.mode, 34.0f);
        btn->setMarginRight(8);
        sub_tabs_box_->addView(btn);
    }
}

void MediaWorkbench::refresh() {
    cancelActiveFetch();
    rebuildTabs();

    auto& kernel = HostKernel::instance();
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

    const auto sources = kernel.domains().mediaSources();
    std::string cat = "all";
    if (sub_mode_ == SubMode::MusicLyrics) cat = "music";
    else if (sub_mode_ == SubMode::NasLibrary) cat = "nas";

    const uint64_t seq = ++fetch_seq_;
    source_status_label_->setText("正在异步拉取 " + std::to_string(sources.size()) + " 个实时 MediaSource 插件...");

    auto pending = std::make_shared<std::atomic<size_t>>(sources.size());
    auto agg_mutex = std::make_shared<std::mutex>();
    auto agg_items = std::make_shared<std::vector<MediaItem>>();

    for (const auto& src : sources) {
        auto tok = src->fetchItemsAsync(
            cat,
            search_query_,
            [this, alive = alive_, seq, pending, agg_mutex, agg_items, total = sources.size()](
                std::vector<MediaItem> items, bool cancelled) {
                if (!cancelled && alive->load(std::memory_order_acquire)) {
                    std::lock_guard<std::mutex> lk(*agg_mutex);
                    agg_items->insert(agg_items->end(),
                                      std::make_move_iterator(items.begin()),
                                      std::make_move_iterator(items.end()));
                }
                if (pending->fetch_sub(1, std::memory_order_acq_rel) == 1) {
                    brls::sync([this, alive, seq, agg_mutex, agg_items, total]() {
                        if (!alive->load(std::memory_order_acquire) || seq != fetch_seq_) {
                            return;
                        }
                        active_fetches_.clear();
                        {
                            std::lock_guard<std::mutex> lk(*agg_mutex);
                            current_items_ = std::move(*agg_items);
                        }
                        if (!has_active_item_ && !current_items_.empty()) {
                            active_item_ = current_items_.front();
                            has_active_item_ = true;
                        }
                        source_status_label_->setText("已异步接入 " + std::to_string(total) +
                                                      " 个实时 MediaSource 插件 · 共 " +
                                                      std::to_string(current_items_.size()) + " 项");
                        rebuildSubContent();
                    });
                }
            });
        if (tok) {
            active_fetches_.push_back(std::move(tok));
        }
    }

    if (current_items_.empty()) {
        rebuildSubContent();
    }
}

void MediaWorkbench::setSubMode(SubMode m) {
    sub_mode_ = m;
    if (m == SubMode::PlayerStage && has_active_item_) {
        rebuildTabs();
        rebuildSubContent();
        return;
    }
    refresh();
}

void MediaWorkbench::rebuildSubContent() {
    mpv_stage_ = nullptr;
    lyric_cards_.clear();
    lyric_orig_labels_.clear();
    lyric_times_.clear();
    active_lyric_idx_ = -1;
    stage_box_->clearViews();
    switch (sub_mode_) {
        case SubMode::Videos: renderVideosView(); break;
        case SubMode::MusicLyrics: renderMusicLyricsView(); break;
        case SubMode::NasLibrary: renderNasView(); break;
        case SubMode::PlayerStage: renderPlayerStageView(); break;
    }
}

void MediaWorkbench::renderVideosView() {
    auto* grid = new brls::Box(brls::Axis::COLUMN);
    brls::Box* row = nullptr;
    int col = 0;

    for (const auto& item : current_items_) {
        if (item.kind == "music" && current_items_.size() > 3) continue;
        if (!search_query_.empty() && item.title.find(search_query_) == std::string::npos) continue;

        if (col % 3 == 0) {
            row = new brls::Box(brls::Axis::ROW);
            row->setWidthPercentage(100);
            row->setMarginBottom(14);
            grid->addView(row);
        }

        auto* card = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        card->setWidthPercentage(32.0f);
        if (col % 3 != 2) card->setMarginRight(14);
        card->setFocusable(true);

        // 16:9 Cover Canvas (async ImageCache when cover_url is provided)
        auto* thumb = ui::makeCard("desktop/soft", 10.0f, 8.0f);
        thumb->setHeight(item.cover_url.empty() ? 138 : 176);
        thumb->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        thumb->setMarginBottom(10);

        auto* badge_row = new brls::Box(brls::Axis::ROW);
        badge_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        badge_row->addView(ui::makePill(item.source_badge, "desktop/surface", "domain/media"));
        badge_row->addView(ui::makePill(formatDuration(item.duration_sec), "desktop/surface", "brls/text"));
        thumb->addView(badge_row);

        if (!item.cover_url.empty()) {
            auto* cover = new CachedImageView(&ImageCache::instance());
            cover->setWidthPercentage(100);
            cover->setHeight(92);
            cover->setMarginTop(4);
            cover->setMarginBottom(4);
            cover->setCornerRadius(6.0f);
            cover->setScalingType(brls::ImageScalingType::FILL);
            cover->setImageUrl(item.cover_url, extractHeadersFromItem(item));
            thumb->addView(cover);
        }

        auto* q_row = new brls::Box(brls::Axis::ROW);
        q_row->addView(ui::makePill(item.quality_label, "status/info_bg", "status/info_fg"));
        if (item.has_bilingual_sub) {
            auto* sub_p = ui::makePill("双语字幕", "status/ok_bg", "status/ok_fg");
            sub_p->setMarginLeft(6);
            q_row->addView(sub_p);
        }
        thumb->addView(q_row);
        card->addView(thumb);

        auto* title = ui::makeLabel(item.title, 16.5f, "brls/text", 1.38f);
        title->setMarginBottom(6);
        card->addView(title);

        auto* meta = ui::makeLabel(item.creator + " · 点击进入 MPV 舞台", 13.5f, "font/grey");
        card->addView(meta);

        card->registerClickAction([this, item](brls::View*) {
            brls::sync([this, item]() { selectItem(item, true); });
            return true;
        });
        row->addView(card);
        ++col;
    }

    stage_box_->addView(grid);
}

void MediaWorkbench::renderMusicLyricsView() {
    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    // Left Square Cover & Transport Studio
    auto* left_card = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    left_card->setWidthPercentage(38);
    left_card->setMarginRight(16);

    auto* art = ui::makeCard("desktop/active", 16.0f, 12.0f);
    art->setHeight(240);
    art->setJustifyContent(brls::JustifyContent::CENTER);
    art->setAlignItems(brls::AlignItems::CENTER);
    if (!active_item_.cover_url.empty()) {
        auto* cover = new CachedImageView(&ImageCache::instance());
        cover->setWidthPercentage(100);
        cover->setHeight(216);
        cover->setCornerRadius(10.0f);
        cover->setScalingType(brls::ImageScalingType::FILL);
        cover->setImageUrl(active_item_.cover_url, extractHeadersFromItem(active_item_));
        art->addView(cover);
    } else {
        art->addView(ui::makePill("1:1 专辑封面舞台", "desktop/surface", "brls/accent"));
    }
    left_card->addView(art);

    auto* track_title = ui::makeLabel(active_item_.title.empty() ? "Clair de Lune" : active_item_.title,
                                      18.0f, "brls/text");
    track_title->setMarginTop(12);
    left_card->addView(track_title);

    auto* artist = ui::makeLabel(active_item_.creator + " · " + active_item_.quality_label, 14.0f, "font/grey");
    artist->setMarginTop(4);
    left_card->addView(artist);

    auto* controls = new brls::Box(brls::Axis::ROW);
    controls->setMarginTop(14);
    controls->addView(ui::makeButton("上一曲", [this]() {
        brls::sync([this]() { selectAdjacentMusicTrack(-1); });
    }, false, 36.0f));
    auto* play_b = ui::makeButton("播放 / 暂停", [this]() {
        if (!active_item_.play_url.empty()) {
            auto opts = buildPlaybackOptions(active_item_);
            const std::string key = active_item_.id + "|" + opts.url + "|" + opts.audio_url;
            if (last_loaded_stream_key_ != key) {
                last_loaded_stream_key_ = key;
                MPVCore::instance().setChapters(active_chapters_);
                MPVCore::instance().load(opts);
                return;
            }
        }
        if (MPVCore::instance().isInitialized() && !MPVCore::instance().state().current_url.empty()) {
            MPVCore::instance().togglePause();
        } else {
            brls::Application::notify("MPV 音频引擎就绪: " + active_item_.title);
        }
    }, true, 36.0f);
    play_b->setMarginLeft(8);
    controls->addView(play_b);
    auto* next_b = ui::makeButton("下一曲", [this]() {
        brls::sync([this]() { selectAdjacentMusicTrack(1); });
    }, false, 36.0f);
    next_b->setMarginLeft(8);
    controls->addView(next_b);
    auto* ai_b = ui::makeButton("F8 逐句精讲", [this]() {
        if (on_entity_focus_) on_entity_focus_(active_item_.title + " [歌词语言分析]");
    }, false, 36.0f);
    ai_b->setMarginLeft(8);
    controls->addView(ai_b);
    left_card->addView(controls);
    split->addView(left_card);

    // Right Bilingual Synced Lyrics Canvas
    auto* lyrics_card = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    lyrics_card->setWidthPercentage(60);
    lyrics_card->addView(ui::makeLabel("双语同步歌词 · LRC Studio", 18.0f, "brls/text"));

    std::vector<LyricLine> lines = active_lyrics_;
    if (lines.empty()) {
        lines = {
            {12.0, "Au calme clair de lune triste et beau,", "在静谧凄美的月光清辉下，"},
            {18.5, "Qui fait rêver les oiseaux dans les arbres,", "令林间栖鸟沉入温柔梦乡，"},
            {25.0, "Et sangloter d'extase les jets d'eau,", "令喷泉在狂喜之中轻声呜咽，"},
            {32.0, "Les grands jets d'eau sveltes parmi les marbres.", "修长水柱亭亭立于大理石丛旁。"},
        };
    }

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
        auto* row = ui::makeCard(active_line ? "desktop/active" : "desktop/soft", 12.0f, 8.0f);
        row->setMarginTop(10);
        row->setFocusable(true);
        auto* orig_lbl = ui::makeLabel(formatDuration(lines[i].time_sec) + "  " + lines[i].original,
                                       16.5f, active_line ? "brls/accent" : "brls/text");
        row->addView(orig_lbl);
        auto* tr = ui::makeLabel(lines[i].translation, 14.5f, "font/grey");
        tr->setMarginTop(4);
        row->addView(tr);
        const double seek_sec = lines[i].time_sec;
        row->registerClickAction([this, seek_sec](brls::View*) {
            if (!active_item_.play_url.empty()) {
                auto opts = buildPlaybackOptions(active_item_);
                const std::string key = active_item_.id + "|" + opts.url + "|" + opts.audio_url;
                if (last_loaded_stream_key_ != key) {
                    last_loaded_stream_key_ = key;
                    opts.start_seconds = seek_sec;
                    MPVCore::instance().setChapters(active_chapters_);
                    MPVCore::instance().load(opts);
                    return true;
                }
            }
            MPVCore::instance().seekAbsolute(seek_sec);
            return true;
        });
        lyric_cards_.push_back(row);
        lyric_orig_labels_.push_back(orig_lbl);
        lyric_times_.push_back(lines[i].time_sec);
        lyrics_card->addView(row);
    }

    split->addView(lyrics_card);
    stage_box_->addView(split);
}

void MediaWorkbench::renderNasView() {
    auto& kernel = HostKernel::instance();
    const bool has_nas = kernel.isInitialized() && kernel.capabilities().has("nas.connection.v1");

    auto* status_card = ui::makeCard("desktop/surface", 18.0f, 10.0f);
    status_card->setMarginBottom(14);
    status_card->addView(ui::makeLabel("飞牛 fnOS NAS 插件解耦接入中心", 19.0f, "brls/text"));

    if (!has_nas) {
        auto* hint = ui::makeLabel(
            "fnOS 连接器作为独立插件 (plugin-fnos-nas) 从主程序完全解耦。\n"
            "安装该插件并在设置表单填入你自己的 NAS 地址/协议/凭据后，即可在此统一浏览私人影库与无损音乐库，"
            "并自动打通 media.clip_jump.v1 跨插件片段跳转。",
            15.0f, "font/grey", 1.5f);
        hint->setMarginTop(8);
        hint->setMarginBottom(12);
        status_card->addView(hint);
        status_card->addView(ui::makeButton("前往插件市场安装 plugin-fnos-nas", [this]() {
            if (on_open_marketplace_) on_open_marketplace_();
        }, true, 38.0f));
        stage_box_->addView(status_card);
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
    auto* scan_btn = ui::makeButton("重新扫描 NAS 索引", [this]() {
        auto& k = HostKernel::instance();
        if (!k.isInitialized()) return;
        nas_request_->cancel();
        nas_request_ = CancelToken::create();
        loadInBackground(k.jobs(), "扫描飞牛 NAS 索引", nas_request_,
            [](const auto&) {
                return HostKernel::instance().capabilities().call("nas.connection.v1", {{"action", "scan_now"}});
            },
            [this, alive = alive_](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire)) return;
                if (res.ok) {
                    nas_status_json_ = std::move(res.data);
                    nas_status_loaded_ = true;
                    brls::Application::notify("飞牛 NAS 索引扫描完成");
                    refresh();
                } else {
                    brls::Application::notify("扫描失败: " + res.error);
                }
            });
    }, true, 34.0f);
    scan_btn->setMarginRight(8);
    action_row->addView(scan_btn);

    auto* probe_btn = ui::makeButton("探测远端服务", [this]() {
        loadNasStatus(true);
    }, false, 34.0f);
    probe_btn->setMarginRight(8);
    action_row->addView(probe_btn);

    auto* browse_btn = ui::makeButton("浏览挂载目录", [this]() {
        loadNasDirectory(".");
    }, false, 34.0f);
    browse_btn->setMarginRight(8);
    action_row->addView(browse_btn);

    action_row->addView(ui::makeButton("插件参数设置", [this]() {
        if (on_open_marketplace_) on_open_marketplace_();
    }, false, 34.0f));
    status_card->addView(action_row);
    stage_box_->addView(status_card);

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
        auto* up_btn = ui::makeButton("上级目录", [this]() {
            if (nas_browse_dir_.empty() || nas_browse_dir_ == ".") return;
            std::string parent = std::filesystem::path(nas_browse_dir_).parent_path().string();
            loadNasDirectory(parent.empty() ? "." : parent);
        }, false, 30.0f);
        up_btn->setMarginRight(8);
        nav_row->addView(up_btn);
        nav_row->addView(ui::makeButton("根目录", [this]() {
            loadNasDirectory(".");
        }, false, 30.0f));
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
                    14.5f, is_dir ? "brls/accent" : "brls/text"));
                rbox->addView(ui::makeLabel(
                    is_dir ? "打开" : formatBytesHuman(size_b),
                    13.0f, "font/grey"));
                row->addView(rbox);

                row->registerClickAction([this, is_dir, epath, ename, ext](brls::View*) {
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
                        nas_request_->cancel();
                        nas_request_ = CancelToken::create();
                        loadInBackground(k.jobs(), "解析 NAS 媒体流", nas_request_,
                            [epath](const auto&) {
                                return HostKernel::instance().capabilities().call(
                                    "nas.connection.v1", {{"action", "stream_url"}, {"path", epath}});
                            },
                            [this, alive = alive_, epath, ename, ext](CapabilityResult res) {
                                if (!alive->load(std::memory_order_acquire)) return;
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
        stage_box_->addView(dir_card);
    }

    std::vector<MediaItem> nas_items;
    for (const auto& item : current_items_) {
        if (item.source_id == "plugin-fnos-nas" || item.kind == "nas") {
            if (search_query_.empty() || item.title.find(search_query_) != std::string::npos) {
                nas_items.push_back(item);
            }
        }
    }
    if (!nas_items.empty()) {
        auto* lib_hdr = ui::makeLabel(
            "NAS 已索引媒体库 (" + std::to_string(nas_items.size()) + " 项)",
            17.5f, "brls/text");
        lib_hdr->setMarginBottom(10);
        stage_box_->addView(lib_hdr);

        auto* grid = new brls::Box(brls::Axis::COLUMN);
        brls::Box* row = nullptr;
        int col = 0;
        for (const auto& item : nas_items) {
            if (col % 3 == 0) {
                row = new brls::Box(brls::Axis::ROW);
                row->setWidthPercentage(100);
                row->setMarginBottom(12);
                grid->addView(row);
            }
            auto* card = ui::makeCard("desktop/surface", 12.0f, 10.0f);
            card->setWidthPercentage(32.0f);
            if (col % 3 != 2) card->setMarginRight(14);
            card->setFocusable(true);

            auto* top = new brls::Box(brls::Axis::ROW);
            top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            top->setMarginBottom(6);
            top->addView(ui::makePill(item.source_badge.empty() ? "飞牛 NAS" : item.source_badge,
                                      "status/info_bg", "domain/media"));
            if (item.has_bilingual_sub) {
                top->addView(ui::makePill("双语字幕", "status/ok_bg", "status/ok_fg"));
            }
            card->addView(top);
            card->addView(ui::makeLabel(item.title, 15.5f, "brls/text", 1.35f));
            auto* sub = ui::makeLabel(
                (item.quality_label.empty() ? item.kind : item.quality_label) + " · 点击播放",
                13.0f, "font/grey");
            sub->setMarginTop(6);
            card->addView(sub);

            card->registerClickAction([this, item](brls::View*) {
                brls::sync([this, item]() { selectItem(item, true); });
                return true;
            });
            row->addView(card);
            ++col;
        }
        stage_box_->addView(grid);
    }
}

void MediaWorkbench::renderPlayerStageView() {
    auto* player_card = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    player_card->addView(ui::makeLabel("MPV 桌面播放舞台 · " + active_item_.title, 19.0f, "brls/text"));

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
    mpv_stage_->setMarginTop(12);
    player_card->addView(mpv_stage_);

    MediaRequestOptions opts = buildPlaybackOptions(active_item_);
    const std::string stream_key = active_item_.id + "|" + opts.url + "|" + opts.audio_url;
    if (!opts.url.empty() && last_loaded_stream_key_ != stream_key) {
        last_loaded_stream_key_ = stream_key;
        mpv_stage_->setDanmaku(active_danmaku_);
        mpv_stage_->loadMedia(opts, active_lyrics_, chapters, active_item_.title);
    } else {
        mpv_stage_->setSubtitles(active_lyrics_);
        mpv_stage_->setDanmaku(active_danmaku_);
        mpv_stage_->setChapters(chapters);
        MPVCore::instance().setChapters(chapters);
    }

    if (opts.url.empty()) {
        auto* preview_hint = ui::makeLabel(
            "当前为布局预览条目（play_url 为空）；接入 Bilibili / YouTube / fnOS NAS 插件返回直连流与 Referer/Cookie 后，libmpv OpenGL/FBO 将自动硬解播放",
            13.5f, "font/grey", 1.4f);
        preview_hint->setMarginTop(8);
        player_card->addView(preview_hint);
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
        auto* btn = ui::makeButton(btn_label, [this, ch, is_ad]() {
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
    auto* skip_btn = ui::makeButton("跳过广告 F9", [this]() {
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
            [this]() { triggerLocalAsrGeneration(); },
            false,
            34.0f);
        asr_btn->setMarginLeft(8);
        chap_row->addView(asr_btn);
    }

    player_card->addView(chap_row);
    stage_box_->addView(player_card);
}

void MediaWorkbench::selectItem(const MediaItem& item, bool switch_to_player) {
    detail_request_->cancel();
    detail_request_ = CancelToken::create();
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
            bool android_media = false;
            bool has_stream = false;
        };
        loadInBackground(kernel.jobs(), "加载媒体流、字幕与弹幕", detail_request_,
            [sources = kernel.domains().mediaSources(), &caps = kernel.capabilities(), item](const auto& cancel) {
                LoadedMediaDetails details;
                if (item.play_url.empty() && item.source_id == "bilibili" && caps.has("media.source.v1")) {
                    auto res = caps.call("media.source.v1", {{"action", "playurl"}, {"id", item.id}});
                    if (res.ok && res.data.is_object()) {
                        details.play_url = res.data.value("video_url", std::string{});
                        details.audio_url = res.data.value("audio_url", std::string{});
                        details.android_media = res.data.value("android_media", false);
                        details.has_stream = !details.play_url.empty();
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
                if (!cancel->isCancelled() && caps.has("player.timeline.v1")) {
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
            [this, alive = alive_, item_id = item.id](auto details) {
                if (!alive->load(std::memory_order_acquire)) return;
                if (active_item_.id != item_id) return;
                active_lyrics_ = std::move(details.lyrics);
                active_danmaku_ = std::move(details.danmaku);
                active_chapters_ = std::move(details.chapters);
                if (details.has_stream) {
                    active_item_.play_url = std::move(details.play_url);
                    if (!active_item_.extra.is_object()) active_item_.extra = nlohmann::json::object();
                    if (!details.audio_url.empty()) {
                        active_item_.extra["audio_url"] = std::move(details.audio_url);
                    }
                    if (details.android_media) {
                        active_item_.extra.erase("http_headers");
                        active_item_.extra["referrer"] = "";
                        active_item_.extra["user_agent"] = "Bilibili Freedoooooom/MarkII";
                    } else {
                        active_item_.extra["referrer"] = "https://www.bilibili.com";
                        active_item_.extra["user_agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36";
                    }
                }
                rebuildSubContent();
            });
        kernel.bus().publish("entity.media.open", {
                                                      {"id", item.id},
                                                      {"title", item.title},
                                                      {"source_id", item.source_id},
                                                      {"origin", "host_workbench"},
                                                  });
    }
    if (on_entity_focus_) {
        on_entity_focus_(item.title);
    }
    if (switch_to_player) {
        sub_mode_ = (item.kind == "music") ? SubMode::MusicLyrics : SubMode::PlayerStage;
    }
    rebuildTabs();
    rebuildSubContent();
}

void MediaWorkbench::selectAdjacentMusicTrack(int delta) {
    std::vector<MediaItem> pool;
    for (const auto& item : current_items_) {
        if (item.kind == "music") {
            pool.push_back(item);
        }
    }
    if (pool.empty()) {
        pool = current_items_;
    }
    if (pool.empty()) {
        brls::Application::notify("当前曲库暂无可切换曲目");
        return;
    }
    size_t cur = 0;
    if (has_active_item_) {
        for (size_t i = 0; i < pool.size(); ++i) {
            if (pool[i].id == active_item_.id) {
                cur = i;
                break;
            }
        }
    }
    const int n = static_cast<int>(pool.size());
    const int next_idx = ((static_cast<int>(cur) + delta) % n + n) % n;
    const MediaItem& target = pool[static_cast<size_t>(next_idx)];
    last_loaded_stream_key_.clear();
    selectItem(target, true);
    if (!target.play_url.empty()) {
        auto opts = buildPlaybackOptions(target);
        last_loaded_stream_key_ = target.id + "|" + opts.url + "|" + opts.audio_url;
        MPVCore::instance().setChapters(active_chapters_);
        MPVCore::instance().load(opts);
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
        lyric_cards_[active_lyric_idx_]->setBackgroundColor(ui::color("desktop/soft"));
        lyric_orig_labels_[active_lyric_idx_]->setTextColor(ui::color("brls/text"));
    }
    active_lyric_idx_ = idx;
    if (active_lyric_idx_ >= 0 && static_cast<size_t>(active_lyric_idx_) < lyric_cards_.size()) {
        lyric_cards_[active_lyric_idx_]->setBackgroundColor(ui::color("desktop/active"));
        lyric_orig_labels_[active_lyric_idx_]->setTextColor(ui::color("brls/accent"));
    }
}

void MediaWorkbench::loadNasStatus(bool probe_remote) {
    auto& k = HostKernel::instance();
    if (!k.isInitialized() || !k.capabilities().has("nas.connection.v1")) {
        return;
    }
    nas_request_->cancel();
    nas_request_ = CancelToken::create();
    nas_status_loading_ = true;
    loadInBackground(k.jobs(), probe_remote ? "探测飞牛 NAS 状态" : "读取飞牛 NAS 状态", nas_request_,
        [probe_remote](const auto&) {
            return HostKernel::instance().capabilities().call(
                "nas.connection.v1", {{"action", probe_remote ? "probe" : "status"}, {"probe_remote", probe_remote}});
        },
        [this, alive = alive_, probe_remote](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
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
        });
}

void MediaWorkbench::loadNasDirectory(const std::string& path) {
    auto& k = HostKernel::instance();
    if (!k.isInitialized() || !k.capabilities().has("nas.connection.v1")) {
        return;
    }
    nas_request_->cancel();
    nas_request_ = CancelToken::create();
    nas_dir_error_.clear();
    loadInBackground(k.jobs(), "浏览飞牛 NAS 目录", nas_request_,
        [path](const auto&) {
            return HostKernel::instance().capabilities().call(
                "nas.connection.v1", {{"action", "list_dir"}, {"path", path}});
        },
        [this, alive = alive_, path](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire)) return;
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
            if (it.id == id) {
                target = it;
                found = true;
                break;
            }
        }
    }
    if (!found) {
        target.id = !id.empty() ? id : ("ext:" + play_url);
        target.source_id = payload.value("source_id", "");
        target.title = payload.value("title", "外部跳转媒体");
        target.creator = payload.value("creator", target.source_id);
        target.kind = payload.value("kind", "video");
        target.source_badge = payload.value("source_badge", target.source_id);
        target.cover_url = payload.value("cover_url", "");
        target.play_url = play_url;
        target.quality_label = payload.value("quality_label", "");
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

    last_loaded_stream_key_.clear();
    selectItem(target, true);
    if (start_sec > 0.0 && !target.play_url.empty() && sub_mode_ == SubMode::MusicLyrics) {
        auto opts = buildPlaybackOptions(target);
        last_loaded_stream_key_ = target.id + "|" + opts.url + "|" + opts.audio_url;
        MPVCore::instance().load(opts);
    }
}

void MediaWorkbench::promptSearch() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this](std::string q) {
                brls::sync([this, q]() {
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

} // namespace omnidesk::host
