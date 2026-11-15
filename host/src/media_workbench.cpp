#include "omnidesk/host/media_workbench.hpp"
#include "omnidesk/host/async_load.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"

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
    return toLowerCopy(item.title).find(q_low) != std::string::npos ||
           toLowerCopy(item.creator).find(q_low) != std::string::npos ||
           toLowerCopy(item.source_badge).find(q_low) != std::string::npos;
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

void appendGridRowSpacers(brls::Box* row, int total_items, int cols, float gap) {
    if (!row || cols <= 1 || total_items <= 0) return;
    const int rem = total_items % cols;
    if (rem == 0) return;
    for (int i = rem; i < cols; ++i) {
        auto* spacer = new brls::Box(brls::Axis::COLUMN);
        spacer->setWidth(0);
        spacer->setGrow(1.0f);
        spacer->setShrink(1.0f);
        spacer->setFocusable(false);
        if (i != cols - 1) {
            spacer->setMarginRight(gap);
        }
        row->addView(spacer);
    }
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

    header_box_ = new brls::Box(brls::Axis::ROW);
    header_box_->setId("omnidesk/pane_header");
    header_box_->setShrink(0.0f);
    header_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header_box_->setAlignItems(brls::AlignItems::CENTER);
    header_box_->setMarginBottom(12);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->setShrink(1.0f);
    left->addView(ui::makeLabel("影视与音乐 · Media Workbench", 24.0f, "brls/text"));
    source_status_label_ = ui::makeLabel("16:9 影视卡片 · 双语歌词工作室 · fnOS NAS · MPV 播放器舞台", 14.5f, "font/grey");
    source_status_label_->setMarginTop(3);
    left->addView(source_status_label_);
    header_box_->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    right->setShrink(0.0f);
    date_filter_btn_ = ui::makeButton("📅 日历查找", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptRehydrateOrDateFilter();
    }, false, 36.0f);
    date_filter_btn_->setMarginRight(8);
    right->addView(date_filter_btn_);
    auto* s_btn = ui::makeButton("搜索片库 /", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptSearch();
    }, false, 36.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    auto* m_btn = ui::makeButton("管理影视插件", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
            on_open_marketplace_();
        }
    }, false, 36.0f);
    right->addView(m_btn);
    header_box_->addView(right);
    addView(header_box_);

    sub_tabs_box_ = new brls::Box(brls::Axis::ROW);
    sub_tabs_box_->setId("omnidesk/pane_filters");
    sub_tabs_box_->setShrink(0.0f);
    sub_tabs_box_->setMarginBottom(14);
    addView(sub_tabs_box_);

    stage_scroll_ = new brls::ScrollingFrame();
    stage_scroll_->setId("omnidesk/pane_primary");
    stage_scroll_->setGrow(1.0f);
    stage_scroll_->setWidthPercentage(100);
    stage_box_ = new brls::Box(brls::Axis::COLUMN);
    stage_box_->setWidthPercentage(100);
    stage_scroll_->setContentView(stage_box_);
    addView(stage_scroll_);

    mpv_state_sub_ = MPVCore::instance().subscribeState(
        [this, alive = alive_](const MpvPlaybackState& st) {
            brls::sync([this, alive, pos = st.position_sec]() {
                if (!alive->load(std::memory_order_acquire)) return;
                updateActiveLyricHighlight(pos);
            });
        });

    if (HostKernel::instance().isInitialized()) {
        recommend_sub_ = HostKernel::instance().bus().subscribe(
            "entity.recommend.updated",
            "host.media_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
    }

    resize_sub_ = brls::Application::getWindowSizeChangedEvent()->subscribe([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            onViewportLayoutChanged();
        }
    });

    refresh();
}

MediaWorkbench::~MediaWorkbench() {
    brls::Application::getWindowSizeChangedEvent()->unsubscribe(resize_sub_);
    if (recommend_sub_ != 0 && HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().unsubscribe(recommend_sub_);
        recommend_sub_ = 0;
    }
    if (mpv_state_sub_ != 0) {
        MPVCore::instance().unsubscribeState(mpv_state_sub_);
        mpv_state_sub_ = 0;
    }
    if (rehydrate_request_) {
        rehydrate_request_->cancel();
        rehydrate_request_.reset();
    }
    if (provenance_request_) provenance_request_->cancel();
    alive_->store(false, std::memory_order_release);
    cancelActiveFetch();
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
        (sub_mode_ == SubMode::PlayerStage && std::abs(sh - last_stage_h_) >= 12)) {
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
        {SubMode::PlayerStage, "MPV 播放舞台 & 章节"},
    };
    for (const auto& t : tabs) {
        auto* btn = ui::makeButton(t.label, [this, alive = alive_, m = t.mode]() {
            brls::sync([this, alive, m]() {
                if (alive->load(std::memory_order_acquire)) {
                    setSubMode(m);
                }
            });
        }, sub_mode_ == t.mode, 34.0f);
        btn->setMarginRight(8);
        sub_tabs_box_->addView(btn);
    }
}

void MediaWorkbench::refresh() {
    cached_feeds_.clear();
    fetchCurrentSubMode();
}

void MediaWorkbench::fetchCurrentSubMode(bool preserve_detail) {
    auto detail = preserve_detail ? std::move(detail_request_) : nullptr;
    cancelActiveFetch();
    if (detail) detail_request_ = std::move(detail);
    rebuildTabs();
    if (date_filter_btn_) {
        if (!curation_date_.empty()) {
            date_filter_btn_->setText("日历 · " + curation_date_);
        } else if (search_query_.rfind("date:", 0) == 0 && search_query_.size() > 5) {
            date_filter_btn_->setText("📅 " + search_query_.substr(5));
        } else {
            date_filter_btn_->setText("📅 日历查找");
        }
    }

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
    if (auto it = cached_feeds_.find(cache_key); it != cached_feeds_.end()) {
        current_items_ = it->second;
        lookupProvenance();
        if (!has_active_item_ && !current_items_.empty()) {
            active_item_ = current_items_.front();
            has_active_item_ = true;
        }
        source_status_label_->setText("已接入 " + std::to_string(sources.size()) +
                                      " 个实时 MediaSource 插件 · 共 " +
                                      std::to_string(current_items_.size()) + " 项");
        rebuildSubContent();
        return;
    }

    current_items_.clear();
    if (preserve_detail && has_active_item_) current_items_.push_back(active_item_);
    const uint64_t seq = ++fetch_seq_;
    source_status_label_->setText("正在异步拉取 " + std::to_string(sources.size()) + " 个实时 MediaSource 插件...");

    auto pending = std::make_shared<std::atomic<size_t>>(sources.size());
    auto agg_mutex = std::make_shared<std::mutex>();
    auto agg_items = std::make_shared<std::vector<MediaItem>>();
    auto has_rendered_partial = std::make_shared<std::atomic<bool>>(false);

    for (const auto& src : sources) {
        auto tok = src->fetchItemsAsync(
            cat,
            curated ? std::string{} : search_query_,
            [this, alive = alive_, seq, pending, agg_mutex, agg_items, has_rendered_partial,
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
                        }
                        if (!has_active_item_ && !current_items_.empty()) {
                            active_item_ = current_items_.front();
                            has_active_item_ = true;
                        }
                        source_status_label_->setText(
                            (is_last ? "已异步接入 " : "正在加载 ") + std::to_string(total) +
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
    if (sub_mode_ == m) return;
    sub_mode_ = m;
    if (m == SubMode::PlayerStage && has_active_item_) {
        rebuildTabs();
        rebuildSubContent();
        return;
    }
    fetchCurrentSubMode();
}

void MediaWorkbench::rebuildSubContent() {
    const bool had_focus = focusIsInsideView(stage_box_);
    if (had_focus) {
        brls::Application::giveFocus(nullptr);
    }
    last_stage_w_ = static_cast<int>(std::round(estimateMediaStageWidth(this, immersive_fullscreen_)));
    last_stage_h_ = static_cast<int>(std::round(estimateMediaStageHeight(this, immersive_fullscreen_)));

    mpv_stage_ = nullptr;
    lyric_cards_.clear();
    lyric_orig_labels_.clear();
    lyric_times_.clear();
    active_lyric_idx_ = -1;
    stage_box_->clearViews();
    if (immersive_fullscreen_ && stage_scroll_) {
        stage_scroll_->setContentOffsetY(0.0f, false);
    }
    switch (sub_mode_) {
        case SubMode::Videos: renderVideosView(); break;
        case SubMode::DailyCuration: renderDailyCurationView(); break;
        case SubMode::MusicLyrics: renderMusicLyricsView(); break;
        case SubMode::NasLibrary: renderNasView(); break;
        case SubMode::PlayerStage: renderPlayerStageView(); break;
    }
    if (had_focus) {
        if (auto* df = stage_box_->getDefaultFocus()) {
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

void MediaWorkbench::renderScopeControls(bool music) {
    auto* row = new brls::Box(brls::Axis::ROW);
    row->setMarginBottom(12);
    const std::vector<std::string> labels = music
        ? std::vector<std::string>{"个人曲库", "每日音乐", "交互推荐"}
        : std::vector<std::string>{"每日音乐", "每日视频", "交互推荐"};
    for (int i = 0; i < 3; ++i) {
        auto* button = ui::makeButton(labels[i], [this, alive = alive_, music, i]() {
            brls::sync([this, alive, music, i]() {
                if (!alive->load(std::memory_order_acquire)) return;
                last_loaded_stream_key_.clear();
                if (music) music_scope_ = static_cast<MusicScope>(i);
                else curation_filter_ = i;
                curation_date_.clear();
                archive_all_ = false;
                fetchCurrentSubMode();
            });
        }, music ? static_cast<int>(music_scope_) == i : curation_filter_ == i, 36.0f);
        button->setMarginRight(8);
        row->addView(button);
    }
    stage_box_->addView(row);
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
        const bool interactive_scope = music ? music_scope_ == MusicScope::Interactive : curation_filter_ == 2;
        dates->addView(ui::makeButton(archive_all_ ? "全部归档 · 选择日期" :
            (interactive_scope && curation_date_.empty()) ? "全部交互会话 · 选择日期" :
            (curation_date_.empty() ? (latest.empty() ? "暂无业务日 · 选择日期" : latest + " · 最新") : curation_date_),
            [this]() { promptRehydrateOrDateFilter(); }, false, 36.0f));
        auto* latest_button = ui::makeButton(interactive_scope ? "全部会话" : "最新", [this]() {
            last_loaded_stream_key_.clear();
            curation_date_.clear();
            archive_all_ = false;
            rebuildSubContent();
        }, !archive_all_ && curation_date_.empty(), 36.0f);
        latest_button->setMarginLeft(8);
        dates->addView(latest_button);
        auto* all = ui::makeButton("全部归档", [this]() {
            last_loaded_stream_key_.clear();
            archive_all_ = true;
            rebuildSubContent();
        }, archive_all_, 36.0f);
        all->setMarginLeft(8);
        dates->addView(all);
        stage_box_->addView(dates);
    }
}

void MediaWorkbench::renderDailyCurationView() {
    renderScopeControls(false);
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
        stage_box_->addView(play);
    }
    renderVideosView();
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
            for (auto& item : current_items_) {
                for (const auto& match : matches) {
                    if (match.value("id", "") != item.source_id + ":" + item.id) continue;
                    const auto date = match.value("previously_recommended_on", "");
if (!date.empty()) {
                        item.extra["previously_recommended_on"] = date;
                        if (active_item_.id == item.id && active_item_.source_id == item.source_id)
                            active_item_.extra["previously_recommended_on"] = date;
                    }
                }
            }
            rebuildSubContent();
        }, []() {});
}

void MediaWorkbench::renderVideosView() {
    const float stage_w = estimateMediaStageWidth(this, false);
    const int cols = computeVideoGridColumns(stage_w);
    constexpr float kGap = 14.0f;
    const float card_w =
        std::max(220.0f, (stage_w - kGap * static_cast<float>(cols - 1)) / static_cast<float>(cols));
    // Subtract card padding (14*2) and inner thumb padding (10*2) for true 16:9 cover aspect ratio
    const float inner_cover_w = std::max(160.0f, card_w - 48.0f);
    const float cover_h = std::round(inner_cover_w * 9.0f / 16.0f);

    auto* grid = new brls::Box(brls::Axis::COLUMN);
    grid->setWidthPercentage(100);
    brls::Box* row = nullptr;
    int col = 0;

    for (const auto& item : current_items_) {
        if (sub_mode_ == SubMode::DailyCuration) {
            if (!matchesCurationScope(item, false)) continue;
        } else if (item.kind == "music" || isCuratedMedia(item)) continue;
        if (!mediaItemMatchesQuery(item, search_query_)) continue;

        if (col % cols == 0) {
            row = new brls::Box(brls::Axis::ROW);
            row->setWidthPercentage(100);
            row->setMarginBottom(14);
            grid->addView(row);
        }

        auto* card = ui::makeCard("desktop/surface", 14.0f, 12.0f);
        card->setWidth(0);
        card->setGrow(1.0f);
        card->setShrink(1.0f);
        if (col % cols != cols - 1) card->setMarginRight(kGap);
        card->setFocusable(true);

        // 16:9 Cover Canvas (async ImageCache when cover_url is provided)
        auto* thumb = ui::makeCard("desktop/soft", 10.0f, 8.0f);
        thumb->setMarginBottom(10);

        auto* badge_row = new brls::Box(brls::Axis::ROW);
        badge_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        badge_row->addView(ui::makePill(
            item.source_badge.empty() ? "Media" : item.source_badge,
            "desktop/surface", "domain/media"));
        badge_row->addView(ui::makePill(formatDuration(item.duration_sec), "desktop/surface", "brls/text"));
        thumb->addView(badge_row);

        if (!item.cover_url.empty()) {
            auto* cover = new CachedImageView(&ImageCache::instance());
            cover->setWidthPercentage(100);
            cover->setHeight(cover_h);
            cover->setMarginTop(6);
            cover->setCornerRadius(6.0f);
            cover->setScalingType(brls::ImageScalingType::FILL);
            cover->setImageUrl(item.cover_url, extractHeadersFromItem(item));
            thumb->addView(cover);
        } else {
            auto* placeholder = new brls::Box(brls::Axis::COLUMN);
            placeholder->setWidthPercentage(100);
            placeholder->setHeight(std::max(96.0f, std::round(cover_h * 0.72f)));
            placeholder->setMarginTop(6);
            placeholder->setJustifyContent(brls::JustifyContent::CENTER);
            placeholder->setAlignItems(brls::AlignItems::CENTER);
            placeholder->addView(ui::makeLabel("16:9 视频舞台预览", 13.0f, "font/grey"));
            thumb->addView(placeholder);
        }

        std::vector<brls::View*> q_pills;
        // Some sources put their recommendation reason in the quality label.
        const bool quality_is_reason = item.extra.is_object() &&
            (item.quality_label == item.extra.value("reason", "") ||
             item.quality_label == item.extra.value("why_recommended", ""));
        if (!item.quality_label.empty() && !quality_is_reason) {
            q_pills.push_back(ui::makePill(item.quality_label, "status/info_bg", "status/info_fg"));
        }
        if (item.has_bilingual_sub) {
            q_pills.push_back(ui::makePill("双语字幕", "status/ok_bg", "status/ok_fg"));
        }
        if (item.extra.is_object()) {
            const std::string prev_date = item.extra.value("previously_recommended_on", "");
            const std::string cur_mode = item.extra.value("curation_mode", "");
            if (!prev_date.empty()) {
                q_pills.push_back(
                    ui::makePill("经典重温 · " + prev_date, "status/info_bg", "status/info_fg"));
            } else if (cur_mode == "interactive_ephemeral") {
                q_pills.push_back(ui::makePill("AI 灵感推荐", "status/info_bg", "domain/core"));
            }
        }
        if (!q_pills.empty()) {
            auto* q_row = new brls::Box(brls::Axis::ROW);
            q_row->setMarginTop(6);
            for (size_t pi = 0; pi < q_pills.size(); ++pi) {
                if (pi > 0) {
                    q_pills[pi]->setMarginLeft(6);
                }
                q_row->addView(q_pills[pi]);
            }
            thumb->addView(q_row);
        }
        card->addView(thumb);

        auto* title = ui::makeLabel(item.title, 16.5f, "brls/text", 1.4f);
        title->setMarginBottom(6);
        card->addView(title);

        auto* meta = ui::makeLabel(
            (item.creator.empty() ? "点击进入 MPV 舞台" : (item.creator + " · 点击进入 MPV 舞台")),
            14.0f, "font/grey");
        card->addView(meta);

        card->registerClickAction([this, alive = alive_, item](brls::View*) {
            brls::sync([this, alive, item]() {
                if (alive->load(std::memory_order_acquire)) {
                    selectItem(item, true);
                }
            });
            return true;
        });
        row->addView(card);
        ++col;
    }

    if (col == 0) grid->addView(ui::makeLabel("当前筛选暂无媒体，试试切换分类、日期或全部归档", 15.0f, "font/grey"));
    appendGridRowSpacers(row, col, cols, kGap);
    stage_box_->addView(grid);
}

void MediaWorkbench::renderMusicLyricsView() {
    renderScopeControls(true);
    const float stage_w = estimateMediaStageWidth(this, false);
    const float art_side = std::clamp(std::round(stage_w * 0.38f - 68.0f), 160.0f, 320.0f);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    // Left Square Cover & Transport Studio
    auto* left_card = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    left_card->setWidthPercentage(38);
    left_card->setMarginRight(16);

    auto* art = ui::makeCard("desktop/active", 16.0f, 12.0f);
    art->setHeight(art_side + 24.0f);
    art->setJustifyContent(brls::JustifyContent::CENTER);
    art->setAlignItems(brls::AlignItems::CENTER);
    if (!active_item_.cover_url.empty()) {
        auto* cover = new CachedImageView(&ImageCache::instance());
        cover->setDimensions(art_side, art_side);
        cover->setCornerRadius(10.0f);
        cover->setScalingType(brls::ImageScalingType::FILL);
        cover->setImageUrl(active_item_.cover_url, extractHeadersFromItem(active_item_));
        art->addView(cover);
    } else {
        art->addView(ui::makePill("1:1 专辑封面舞台", "desktop/surface", "brls/accent"));
    }
    left_card->addView(art);

    auto* track_title = ui::makeLabel(active_item_.title.empty() ? "请选择曲目" : active_item_.title,
                                      18.0f, "brls/text");
    track_title->setMarginTop(12);
    left_card->addView(track_title);

    std::string artist_text = active_item_.creator;
    if (!active_item_.quality_label.empty()) {
        if (!artist_text.empty()) artist_text += " · ";
        artist_text += active_item_.quality_label;
    }
    auto* artist = ui::makeLabel(artist_text, 14.0f, "font/grey");
    artist->setMarginTop(4);
    left_card->addView(artist);

    auto* controls = new brls::Box(brls::Axis::ROW);
    controls->setMarginTop(14);
    controls->addView(ui::makeButton("上一曲", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                selectAdjacentMusicTrack(-1);
            }
        });
    }, false, 36.0f));
    auto* play_b = ui::makeButton("播放 / 暂停", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        const auto visible = visibleMusicItems();
        if (std::none_of(visible.begin(), visible.end(), [this](const MediaItem& item) {
            return item.id == active_item_.id && item.source_id == active_item_.source_id;
        })) {
            brls::Application::notify("请从当前筛选列表选择曲目");
            return;
        }
        if (!active_item_.play_url.empty()) {
            auto opts = buildPlaybackOptions(active_item_);
            const std::string key = active_item_.source_id + ":" + active_item_.id + "|" + opts.url + "|" + opts.audio_url;
            if (last_loaded_stream_key_ != key) {
                last_loaded_stream_key_ = key;
                playVisibleMusic();
                return;
            }
        }
        if (!MPVCore::instance().state().current_url.empty() ||
            PlaybackCoordinator::instance().state().active_role != PlaybackRole::None) {
            PlaybackCoordinator::instance().playPause();
        } else {
            brls::Application::notify("MPV 音频引擎就绪: " + active_item_.title);
        }
    }, true, 36.0f);
    play_b->setMarginLeft(8);
    controls->addView(play_b);
    auto* next_b = ui::makeButton("下一曲", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                selectAdjacentMusicTrack(1);
            }
        });
    }, false, 36.0f);
    next_b->setMarginLeft(8);
    controls->addView(next_b);
    auto* ai_b = ui::makeButton("F8 逐句精讲", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_entity_focus_) {
            on_entity_focus_(active_item_.title + " [歌词语言分析]");
        }
    }, false, 36.0f);
    ai_b->setMarginLeft(8);
    controls->addView(ai_b);
    left_card->addView(controls);
    appendCuratedMetadataBlock(left_card, active_item_);

    const auto music_list = visibleMusicItems();
    if (music_list.empty()) {
        left_card->addView(ui::makeLabel("当前范围暂无曲目，可切换个人曲库、每日音乐或交互推荐", 14.0f, "font/grey"));
    }
    if (!music_list.empty()) {
        auto* queue_hdr = ui::makeLabel(
            "曲库与 AI 策展列表 (" + std::to_string(music_list.size()) + " 首)",
            15.0f, "brls/text");
        queue_hdr->setMarginTop(14);
        queue_hdr->setMarginBottom(6);
        left_card->addView(queue_hdr);

        for (const auto& tr : music_list) {
            const bool selected = (tr.id == active_item_.id);
            auto* row = ui::makeCard(selected ? "desktop/active" : "desktop/soft", 10.0f, 8.0f);
            row->setMarginBottom(8);
            row->setFocusable(true);
            auto* r_top = new brls::Box(brls::Axis::ROW);
            r_top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            r_top->addView(ui::makeLabel(tr.title, 14.5f, selected ? "brls/accent" : "brls/text"));
            if (tr.extra.is_object() && !tr.extra.value("previously_recommended_on", "").empty()) {
                r_top->addView(ui::makePill(
                    "经典重温 · " + tr.extra.value("previously_recommended_on", ""),
                    "status/info_bg", "status/info_fg"));
            }
            row->addView(r_top);
            if (!tr.creator.empty()) {
                row->addView(ui::makeLabel(tr.creator, 13.5f, "font/grey"));
            }
            row->registerClickAction([this, alive = alive_, tr](brls::View*) {
                brls::sync([this, alive, tr]() {
                    if (alive->load(std::memory_order_acquire)) {
                        last_loaded_stream_key_.clear();
                        selectItem(tr, true);
                    }
                });
                return true;
            });
            left_card->addView(row);
        }
    }
    split->addView(left_card);

    // Right Bilingual Synced Lyrics Canvas
    auto* lyrics_card = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    lyrics_card->setWidthPercentage(60);
    lyrics_card->addView(ui::makeLabel("双语同步歌词 · LRC Studio", 18.0f, "brls/text"));

    std::vector<LyricLine> lines = active_lyrics_;
    if (lines.empty()) {
        lyrics_card->addView(ui::makeLabel("此曲暂无可用歌词", 15.0f, "font/grey"));
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
        auto* row = ui::makeCard(active_line ? "desktop/active" : "desktop/soft", 14.0f, 10.0f);
        row->setMarginTop(10);
        row->setFocusable(true);
        auto* top_line = new brls::Box(brls::Axis::ROW);
        top_line->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top_line->setAlignItems(brls::AlignItems::CENTER);
        auto* orig_lbl = ui::makeLabel(formatDuration(lines[i].time_sec) + "  " + lines[i].original,
                                       17.0f, active_line ? "brls/accent" : "brls/text");
        orig_lbl->setGrow(1.0f);
        top_line->addView(orig_lbl);
        auto* sel_btn = ui::makeButton("划词", [this, alive = alive_, line = lines[i]]() {
            if (!alive->load(std::memory_order_acquire)) return;
            auto& k = HostKernel::instance();
            SelectionContext sctx;
            sctx.context_sentence = line.original;
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
        }, false, 30.0f);
        sel_btn->setMarginLeft(8);
        top_line->addView(sel_btn);
        row->addView(top_line);
        auto* tr = ui::makeLabel(lines[i].translation, 15.0f, "font/grey");
        tr->setMarginTop(4);
        row->addView(tr);
        const double seek_sec = lines[i].time_sec;
        row->registerClickAction([this, alive = alive_, seek_sec](brls::View*) {
            if (!alive->load(std::memory_order_acquire)) return true;
            if (!active_item_.play_url.empty()) {
                auto opts = buildPlaybackOptions(active_item_);
                const std::string key = active_item_.source_id + ":" + active_item_.id + "|" + opts.url + "|" + opts.audio_url;
                if (last_loaded_stream_key_ != key) {
                    last_loaded_stream_key_ = key;
                    opts.start_seconds = seek_sec;
                    playVisibleMusic(seek_sec);
                    return true;
                }
            }
            PlaybackCoordinator::instance().seekAbsolute(seek_sec);
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
        status_card->addView(ui::makeButton("前往插件市场安装 plugin-fnos-nas", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
                on_open_marketplace_();
            }
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
        stage_box_->addView(dir_card);
    }

    std::vector<MediaItem> nas_items;
    for (const auto& item : current_items_) {
        if (!isCuratedMedia(item) && (item.source_id == "plugin-fnos-nas" || item.kind == "nas")) {
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

        const float stage_w = estimateMediaStageWidth(this, false);
        const int cols = computeVideoGridColumns(stage_w);
        constexpr float kGap = 14.0f;

        auto* grid = new brls::Box(brls::Axis::COLUMN);
        grid->setWidthPercentage(100);
        brls::Box* row = nullptr;
        int col = 0;
        for (const auto& item : nas_items) {
            if (col % cols == 0) {
                row = new brls::Box(brls::Axis::ROW);
                row->setWidthPercentage(100);
                row->setMarginBottom(12);
                grid->addView(row);
            }
            auto* card = ui::makeCard("desktop/surface", 14.0f, 12.0f);
            card->setWidth(0);
            card->setGrow(1.0f);
            card->setShrink(1.0f);
            if (col % cols != cols - 1) card->setMarginRight(kGap);
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
            card->addView(ui::makeLabel(item.title, 16.0f, "brls/text", 1.38f));
            auto* sub = ui::makeLabel(
                (item.quality_label.empty() ? item.kind : item.quality_label) + " · 点击播放",
                13.5f, "font/grey");
            sub->setMarginTop(6);
            card->addView(sub);

            card->registerClickAction([this, alive = alive_, item](brls::View*) {
                brls::sync([this, alive, item]() {
                    if (alive->load(std::memory_order_acquire)) {
                        selectItem(item, true);
                    }
                });
                return true;
            });
            row->addView(card);
            ++col;
        }
        appendGridRowSpacers(row, col, cols, kGap);
        stage_box_->addView(grid);
    }
}

void MediaWorkbench::renderPlayerStageView() {
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
    if (!opts.url.empty() && last_loaded_stream_key_ != stream_key) {
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
        tr.auto_enrich_timeline = true;
        PlaybackCoordinator::instance().play(
            PlaybackRole::ForegroundVideo, "host.media_workbench", std::move(tr));
    } else {
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
        stage_box_->addView(immersive_wrap);
        return;
    }

    const float ideal_16_9 = std::round((stage_w - 36.0f) * 9.0f / 16.0f);
    const float max_canvas_h = std::max(280.0f, stage_h - 320.0f);
    mpv_stage_->setStageMinHeight(std::clamp(ideal_16_9, 260.0f, max_canvas_h));
    mpv_stage_->setMarginTop(12);

    auto* player_card = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    player_card->addView(ui::makeLabel("MPV 桌面播放舞台 · " + active_item_.title, 19.0f, "brls/text"));
    player_card->addView(mpv_stage_);

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
    stage_box_->addView(player_card);
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
            std::string quality_label;
            bool android_media = false;
            bool has_stream = false;
        };
        loadUiInBackground(kernel.uiTasks(), "media.detail", req,
            [sources = kernel.domains().mediaSources(), &caps = kernel.capabilities(), item](const auto& cancel) {
                LoadedMediaDetails details;
                const bool is_demo = (item.id.rfind("demo-", 0) == 0);
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
            [this, alive = alive_, req, item_id = item.id, source_id = item.source_id](auto details) {
                if (!alive->load(std::memory_order_acquire)) return;
                if (detail_request_ == req) {
                    detail_request_.reset();
                }
                if (active_item_.id != item_id || active_item_.source_id != source_id) return;
                active_lyrics_ = std::move(details.lyrics);
                active_danmaku_ = std::move(details.danmaku);
                active_chapters_ = std::move(details.chapters);
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
                                                      {"source_id", item.source_id},
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
    if (!target.play_url.empty()) playVisibleMusic();
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
        target.id = !id.empty() ? id : ("ext:" + play_url);
        target.source_id = payload.value("source_id", "");
        target.title = payload.value("title", "外部跳转媒体");
        target.creator = payload.value("creator", target.source_id);
        target.kind = payload.value("kind", "video");
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
    last_loaded_stream_key_.clear();
    selectItem(target, true);
    if (target.kind == "music") fetchCurrentSubMode(true);
    if (start_sec > 0.0 && !target.play_url.empty() && sub_mode_ == SubMode::MusicLyrics) {
        auto opts = buildPlaybackOptions(target);
        last_loaded_stream_key_ = target.source_id + ":" + target.id + "|" + opts.url + "|" + opts.audio_url;
        playVisibleMusic(start_sec);
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
        .on_apply_date_filter = [this, alive = alive_](const std::string& d) {
            if (!alive->load(std::memory_order_acquire)) return;
            last_loaded_stream_key_.clear();
            curation_date_ = d;
            archive_all_ = false;
            refresh();
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
