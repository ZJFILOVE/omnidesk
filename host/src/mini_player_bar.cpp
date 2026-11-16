#include "omnidesk/host/mini_player_bar.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>

#include "omnidesk/host/ui_comfort.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

std::string formatTimeMmSs(double sec) {
    if (!std::isfinite(sec) || sec <= 0.0) return "00:00";
    const int total = static_cast<int>(std::min(sec, 359999.0));
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", total / 60, total % 60);
    return std::string(buf);
}

} // namespace

MiniPlayerBar::MiniPlayerBar(std::function<void(PlaybackRole)> on_open_stage)
    : brls::Box(brls::Axis::COLUMN),
      on_open_stage_(std::move(on_open_stage)) {
    setWidthPercentage(100);
    setBackgroundColor(ui::color("desktop/surface"));
    setBorderThickness(1);
    setBorderColor(ui::color("desktop/line"));
    setVisibility(brls::Visibility::GONE);

    buildStaticUi();

    sub_token_ = PlaybackCoordinator::instance().subscribe(
        [this, alive = alive_](const PlaybackSessionState& session) {
            brls::sync([this, alive, session]() {
                if (alive->load(std::memory_order_acquire)) {
                    refresh(session);
                }
            });
        });
    refresh(PlaybackCoordinator::instance().state());
}

MiniPlayerBar::~MiniPlayerBar() {
    alive_->store(false, std::memory_order_release);
    if (sub_token_ != 0) {
        PlaybackCoordinator::instance().unsubscribe(sub_token_);
        sub_token_ = 0;
    }
}

void MiniPlayerBar::buildStaticUi() {
    // Slim top progress bar
    auto* track = new brls::Box(brls::Axis::ROW);
    track->setWidthPercentage(100);
    track->setHeight(3);
    track->setBackgroundColor(ui::color("desktop/soft"));
    progress_fill_ = new brls::Box(brls::Axis::ROW);
    progress_fill_->setHeight(3);
    progress_fill_->setWidthPercentage(0.0f);
    progress_fill_->setBackgroundColor(ui::color("brls/accent"));
    track->addView(progress_fill_);
    addView(track);

    auto* row = new brls::Box(brls::Axis::ROW);
    row->setWidthPercentage(100);
    row->setPadding(8, 18, 8, 18);
    row->setAlignItems(brls::AlignItems::CENTER);
    row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    ui_comfort::enableFlexWrap(row, 6.0f);

    auto* left = new brls::Box(brls::Axis::ROW);
    left->setAlignItems(brls::AlignItems::CENTER);
    left->setGrow(1.0f);
    left->setShrink(1.0f);

    thumb_box_ = new brls::Box(brls::Axis::ROW);
    thumb_box_->setDimensions(38, 38);
    thumb_box_->setCornerRadius(7.0f);
    thumb_box_->setBackgroundColor(ui::color("desktop/soft"));
    thumb_box_->setMarginRight(12);
    thumb_box_->setVisibility(brls::Visibility::GONE);
    left->addView(thumb_box_);

    role_pill_box_ = new brls::Box(brls::Axis::ROW);
    role_pill_box_->setPadding(4, 10, 4, 10);
    role_pill_box_->setCornerRadius(7.0f);
    role_pill_box_->setBackgroundColor(ui::color("desktop/active"));
    role_pill_box_->setMarginRight(12);
    role_pill_label_ = ui::makeLabel("音乐", 12.8f, "brls/accent", 1.2f);
    role_pill_label_->setSingleLine(true);
    role_pill_box_->addView(role_pill_label_);
    left->addView(role_pill_box_);

    auto* info_col = new brls::Box(brls::Axis::COLUMN);
    info_col->setGrow(1.0f);
    info_col->setShrink(1.0f);
    title_label_ = ui::makeLabel("未在播放", 14.8f, "brls/text", 1.25f);
    title_label_->setSingleLine(true);
    meta_label_ = ui::makeLabel("", 12.8f, "font/grey", 1.2f);
    meta_label_->setSingleLine(true);
    info_col->addView(title_label_);
    info_col->addView(meta_label_);
    left->addView(info_col);

    time_label_ = ui::makeLabel("00:00 / 00:00", 13.0f, "font/grey");
    time_label_->setMarginLeft(12);
    time_label_->setMarginRight(14);
    left->addView(time_label_);
    row->addView(left);

    auto* controls = new brls::Box(brls::Axis::ROW);
    controls->setAlignItems(brls::AlignItems::CENTER);
    ui_comfort::enableFlexWrap(controls, 4.0f);

    prev_btn_ = ui::makeButton("上一曲", []() {
        PlaybackCoordinator::instance().prevTrack();
    }, false, 33.0f);
    prev_btn_->setMarginRight(6);
    prev_btn_->setVisibility(brls::Visibility::GONE);
    controls->addView(prev_btn_);

    play_btn_ = ui::makeButton("播放", []() {
        PlaybackCoordinator::instance().playPause();
    }, true, 33.0f);
    play_btn_->setMarginRight(6);
    controls->addView(play_btn_);

    next_btn_ = ui::makeButton("下一曲", []() {
        PlaybackCoordinator::instance().nextTrack();
    }, false, 33.0f);
    next_btn_->setMarginRight(6);
    next_btn_->setVisibility(brls::Visibility::GONE);
    controls->addView(next_btn_);

    sponsor_btn_ = ui::makeButton("跳过赞助 F9", []() {
        PlaybackCoordinator::instance().skipActiveSponsor();
    }, false, 33.0f);
    sponsor_btn_->setMarginRight(6);
    sponsor_btn_->setVisibility(brls::Visibility::GONE);
    controls->addView(sponsor_btn_);

    resume_music_btn_ = ui::makeButton("恢复音乐", []() {
        PlaybackCoordinator::instance().resumeSuspendedMusic();
    }, false, 33.0f);
    resume_music_btn_->setMarginRight(6);
    resume_music_btn_->setVisibility(brls::Visibility::GONE);
    controls->addView(resume_music_btn_);

    open_stage_btn_ = ui::makeButton("舞台", [this]() {
        const auto st = PlaybackCoordinator::instance().state();
        if (on_open_stage_) on_open_stage_(st.active_role);
    }, false, 33.0f);
    open_stage_btn_->setMarginRight(6);
    controls->addView(open_stage_btn_);

    controls->addView(ui::makeButton("×", []() {
        PlaybackCoordinator::instance().stop(false);
    }, false, 33.0f));

    row->addView(controls);
    addView(row);
}

void MiniPlayerBar::refresh(const PlaybackSessionState& session) {
    const bool has_active =
        (session.active_role != PlaybackRole::None && !session.active_track.item.id.empty());
    const bool visible = has_active || session.has_suspended_music;

    if (visible != last_visible_) {
        last_visible_ = visible;
        setVisibility(visible ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (!visible) {
        return;
    }

    const PlaybackTrack& shown_track =
        has_active ? session.active_track : session.suspended_music_track;
    const std::string item_key =
        shown_track.item.id + "|" + shown_track.item.cover_url + "|" +
        std::to_string(static_cast<int>(session.active_role));

    if (item_key != last_item_key_) {
        last_item_key_ = item_key;
        if (role_pill_label_) {
            if (!has_active && session.has_suspended_music) {
                role_pill_label_->setText("音乐已暂停");
            } else if (session.active_role == PlaybackRole::BackgroundMusic) {
                role_pill_label_->setText("音乐");
            } else {
                role_pill_label_->setText("视频");
            }
        }
        if (title_label_) {
            title_label_->setText(
                shown_track.item.title.empty() ? "正在播放" : shown_track.item.title);
        }
        if (thumb_box_) {
            thumb_box_->clearViews();
            if (!shown_track.item.cover_url.empty()) {
                auto* img = new CachedImageView(&ImageCache::instance());
                img->setDimensions(34, 34);
                img->setCornerRadius(6.0f);
                img->setScalingType(brls::ImageScalingType::FILL);
                img->setImageUrl(shown_track.item.cover_url, shown_track.options.http_headers);
                thumb_box_->addView(img);
                thumb_box_->setVisibility(brls::Visibility::VISIBLE);
            } else {
                thumb_box_->setVisibility(brls::Visibility::GONE);
            }
        }
    }

    if (meta_label_) {
        std::string meta = shown_track.item.creator;
        if (!shown_track.item.source_badge.empty()) {
            if (!meta.empty()) meta += " · ";
            meta += shown_track.item.source_badge;
        }
        if (!session.subtitle_status.empty()) {
            if (!meta.empty()) meta += " · ";
            meta += session.subtitle_status;
        } else if (session.has_suspended_music && has_active) {
            if (!meta.empty()) meta += " · ";
            meta += "已暂存音乐: " + session.suspended_music_track.item.title;
        }
        meta_label_->setText(meta);
    }

    const bool paused = session.mpv.paused || !session.mpv.playing;
    if (play_btn_ && paused != last_paused_) {
        last_paused_ = paused;
        play_btn_->setText(paused ? "播放" : "暂停");
    }

    const bool has_queue = session.music_queue.size() > 1;
    if (has_queue != last_has_queue_) {
        last_has_queue_ = has_queue;
        const auto vis = has_queue ? brls::Visibility::VISIBLE : brls::Visibility::GONE;
        if (prev_btn_) prev_btn_->setVisibility(vis);
        if (next_btn_) next_btn_->setVisibility(vis);
    }

    const bool has_sponsor = session.active_sponsor.has_value();
    if (sponsor_btn_ && has_sponsor != last_has_sponsor_) {
        last_has_sponsor_ = has_sponsor;
        sponsor_btn_->setVisibility(has_sponsor ? brls::Visibility::VISIBLE
                                                : brls::Visibility::GONE);
    }

    if (resume_music_btn_ && session.has_suspended_music != last_has_suspended_) {
        last_has_suspended_ = session.has_suspended_music;
        resume_music_btn_->setVisibility(
            session.has_suspended_music ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }

    updateProgress(session);
}

void MiniPlayerBar::updateProgress(const PlaybackSessionState& session) {
    const double pos = std::max(0.0, session.mpv.position_sec);
    const double dur = std::max(0.0, session.mpv.duration_sec);
    if (time_label_) {
        time_label_->setText(formatTimeMmSs(pos) + " / " + formatTimeMmSs(dur));
    }
    if (progress_fill_) {
        const float pct = (dur > 0.0)
                              ? static_cast<float>(std::clamp((pos / dur) * 100.0, 0.0, 100.0))
                              : 0.0f;
        progress_fill_->setWidthPercentage(pct);
    }
}

} // namespace omnidesk::host