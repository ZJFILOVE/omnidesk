#include "omnidesk/host/media_workbench.hpp"

#include <cstdio>
#include <mutex>
#include <sstream>
#include <unordered_map>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/mpv_core.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

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

    refresh();
}

MediaWorkbench::~MediaWorkbench() {
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
    controls->addView(ui::makeButton("上一曲", [] {}, false, 36.0f));
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

    for (size_t i = 0; i < lines.size(); ++i) {
        auto* row = ui::makeCard(i == 1 ? "desktop/active" : "desktop/soft", 12.0f, 8.0f);
        row->setMarginTop(10);
        row->addView(ui::makeLabel(formatDuration(lines[i].time_sec) + "  " + lines[i].original,
                                   16.5f, i == 1 ? "brls/accent" : "brls/text"));
        auto* tr = ui::makeLabel(lines[i].translation, 14.5f, "font/grey");
        tr->setMarginTop(4);
        row->addView(tr);
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
    } else {
        auto res = kernel.capabilities().call("nas.connection.v1", {{"action", "status"}});
        status_card->addView(ui::makeLabel("连接状态: " + res.data.dump(2), 14.5f, "status/ok_fg"));
    }
    stage_box_->addView(status_card);
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
    if (chapters.empty() && !active_item_.id.empty()) {
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
        mpv_stage_->loadMedia(opts, active_lyrics_, chapters, active_item_.title);
    } else {
        mpv_stage_->setSubtitles(active_lyrics_);
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

    player_card->addView(chap_row);
    stage_box_->addView(player_card);
}

void MediaWorkbench::selectItem(const MediaItem& item, bool switch_to_player) {
    active_item_ = item;
    has_active_item_ = true;
    active_lyrics_.clear();
    active_chapters_.clear();

    if (HostKernel::instance().isInitialized()) {
        auto& kernel = HostKernel::instance();
        for (const auto& src : kernel.domains().mediaSources()) {
            if (item.source_id.empty() || src->sourceId() == item.source_id) {
                if (active_lyrics_.empty()) {
                    active_lyrics_ = src->fetchLyrics(item.id);
                }
                if (active_chapters_.empty()) {
                    active_chapters_ = src->fetchChapters(item.id);
                }
            }
        }
        kernel.bus().publish("entity.media.open", {
                                                      {"id", item.id},
                                                      {"title", item.title},
                                                      {"source_id", item.source_id},
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

} // namespace omnidesk::host
