#include "music/ui_views.hpp"
#include "music/lrc_parser.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <omnidesk/image_cache.hpp>
#include <omnidesk/ui_theme.hpp>
#include <thread>

namespace fs = std::filesystem;

namespace music_plugin {
namespace {

std::string formatDuration(double sec) {
    if (sec <= 0.0) return "00:00";
    int total = static_cast<int>(sec);
    int m = total / 60;
    int s = total % 60;
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", m, s);
    return std::string(buf);
}

std::string clipText(const std::string& s, size_t max_bytes) {
    if (s.size() <= max_bytes) return s;
    size_t cut = max_bytes;
    while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0) == 0x80) --cut;
    return s.substr(0, cut) + "...";
}

std::string drmBadgeText(const DrmStatus& drm) {
    if (!drm.engine_reachable) return "引擎未连接";
    if (drm.auth_state == "challenging") return "等待 2FA 验证码";
    if (drm.auth_state == "logging_in") return "正在登录…";
    if (drm.fairplay_state == "ready") {
        if (drm.cap_hi_res) return "Hi-Res ALAC 就绪";
        if (drm.cap_alac) return "FairPlay ALAC 就绪";
        return "FairPlay 就绪";
    }
    if (drm.auth_state == "logged_in") return "初始化 FairPlay…";
    return "未登录 Apple ID";
}

std::string phaseLabel(const std::string& phase) {
    if (phase == "queued") return "排队中";
    if (phase == "resolving") return "解析元数据";
    if (phase == "downloading") return "解密下载中";
    if (phase == "tagging") return "封装封面歌词";
    if (phase == "moving") return "写入磁盘";
    if (phase == "done") return "已完成";
    if (phase == "failed") return "失败";
    if (phase == "cancelled") return "已取消";
    return phase;
}

brls::Box* makeSquarePlaceholder(float dim, const std::string& glyph) {
    auto* ph = new brls::Box(brls::Axis::COLUMN);
    ph->setDimensions(dim, dim);
    ph->setShrink(0.0f);
    ph->setCornerRadius(6.0f);
    ph->setBackgroundColor(omnidesk::ui::color("desktop/soft"));
    ph->setJustifyContent(brls::JustifyContent::CENTER);
    ph->setAlignItems(brls::AlignItems::CENTER);
    ph->addView(omnidesk::ui::makeLabel(glyph, 15.0f, "domain/media"));
    return ph;
}

} // namespace

MusicWorkspaceView::MusicWorkspaceView(std::shared_ptr<MusicEngine> engine)
    : brls::Box(brls::Axis::COLUMN), engine_(std::move(engine)) {
    feed_loading_ = (engine_ != nullptr);
    curation_loading_ = (engine_ != nullptr);
    buildUi();

    if (engine_) {
        std::weak_ptr<std::atomic<bool>> alive_w = alive_;
        engine_sub_ = engine_->subscribeState([this, alive_w]() {
            brls::sync([this, alive_w]() {
                auto a = alive_w.lock();
                if (!a || !a->load(std::memory_order_acquire)) return;
                refreshHeader();
                refreshIslandBar();
                if (active_tab_ == SubTab::NowPlaying) {
                    renderNowPlayingBody();
                } else if (active_tab_ == SubTab::AiCuration || active_tab_ == SubTab::ForYou) {
                    triggerCurationLoad(selected_archive_date_, true);
                }
            });
        });

        mpv_sub_ = omnidesk::MPVCore::instance().subscribeState(
            [this, alive_w](const omnidesk::MpvPlaybackState& st) {
                brls::sync([this, alive_w, pos = st.position_sec]() {
                    auto a = alive_w.lock();
                    if (!a || !a->load(std::memory_order_acquire)) return;
                    refreshIslandBar();
                    if (active_tab_ == SubTab::NowPlaying) {
                        updateActiveLyricHighlight(pos);
                    }
                });
            });

        // Initial async load: DRM status + personalized recommendations + AI Curation snapshot
        const uint64_t seq = ++feed_req_seq_;
        const uint64_t cseq = ++curation_req_seq_;
        std::thread([eng = engine_, this, alive_w, seq, cseq]() {
            if (!eng || !eng->isAlive()) return;
            eng->refreshDrmStatus(true);
            CurationWorkspaceSnapshot cur = eng->loadCurationSnapshot("", true);
            brls::sync([this, alive_w, cseq, cur]() {
                auto a = alive_w.lock();
                if (!a || !a->load(std::memory_order_acquire)) return;
                refreshHeader();
                if (cseq == curation_req_seq_) {
                    curation_loading_ = false;
                    curation_snap_ = cur;
                    if (selected_archive_date_.empty()) {
                        selected_archive_date_ = cur.selected_date;
                    }
                    renderActiveTab();
                }
            });

            if (!eng->isAlive()) return;
            FeedOutcome rec = eng->loadFeed("recommendations", "", false);
            brls::sync([this, alive_w, seq, rec]() {
                auto a = alive_w.lock();
                if (!a || !a->load(std::memory_order_acquire)) return;
                refreshHeader();
                if (seq == feed_req_seq_) {
                    feed_loading_ = false;
                    current_feed_ = rec;
                    renderActiveTab();
                }
            });
        }).detach();
    }
}

MusicWorkspaceView::~MusicWorkspaceView() {
    alive_->store(false, std::memory_order_release);
    if (engine_ && engine_sub_ != 0) {
        engine_->unsubscribeState(engine_sub_);
        engine_sub_ = 0;
    }
    if (mpv_sub_ != 0) {
        omnidesk::MPVCore::instance().unsubscribeState(mpv_sub_);
        mpv_sub_ = 0;
    }
    mv_stage_ = nullptr;
}

void MusicWorkspaceView::buildUi() {
    setGrow(1.0f);
    setShrink(1.0f);
    setWidthPercentage(100);
    setHeightPercentage(100);
    setMinWidth(0);
    setMinHeight(0);
    setPadding(16, 22, 14, 22);
    setBackgroundColor(omnidesk::ui::color("brls/background"));

    // Compact responsive header (never wraps or pushes buttons off-screen)
    auto* header = new brls::Box(brls::Axis::ROW);
    header->setWidthPercentage(100);
    header->setShrink(0.0f);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(8);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->setShrink(1.0f);
    left->setMinWidth(0);
    left->setMarginRight(12);
    auto* main_title = omnidesk::ui::makeLabel("Apple Music · FairPlay 无损工作室", 20.5f, "brls/text");
    main_title->setSingleLine(true);
    left->addView(main_title);
    status_label_ = omnidesk::ui::makeLabel(
        "24bit/192kHz ALAC 解密 · 双语灵动歌词 · AI 策展与歌单反写", 12.5f, "font/grey");
    status_label_->setSingleLine(true);
    status_label_->setMarginTop(2);
    left->addView(status_label_);
    header->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    right->setShrink(0.0f);
    right->setAlignItems(brls::AlignItems::CENTER);

    drm_pill_box_ = new brls::Box(brls::Axis::ROW);
    drm_pill_box_->setShrink(0.0f);
    drm_pill_box_->setPadding(5, 10, 5, 10);
    drm_pill_box_->setCornerRadius(999.0f);
    drm_pill_box_->setBackgroundColor(omnidesk::ui::color("status/ok_bg"));
    drm_pill_box_->setMarginRight(6);
    drm_pill_label_ = omnidesk::ui::makeLabel("检测引擎…", 12.5f, "status/ok_fg");
    drm_pill_label_->setSingleLine(true);
    drm_pill_box_->addView(drm_pill_label_);
    right->addView(drm_pill_box_);

    auto* ai_btn = omnidesk::ui::makeButton("✨ AI 反写", [this]() {
        if (active_tab_ != SubTab::AiCuration) {
            setSubTab(SubTab::AiCuration);
        } else {
            promptAiWriteback();
        }
    }, true, 32.0f);
    ai_btn->setShrink(0.0f);
    ai_btn->setMarginRight(6);
    right->addView(ai_btn);

    auto* search_btn = omnidesk::ui::makeButton("搜索", [this]() { promptSearch(); }, false, 32.0f);
    search_btn->setShrink(0.0f);
    search_btn->setMarginRight(6);
    right->addView(search_btn);

    auto* dl_btn = omnidesk::ui::makeButton("⬇ 下载", [this]() {
        show_downloads_panel_ = !show_downloads_panel_;
        if (show_downloads_panel_) {
            triggerExportsRefresh();
        } else {
            renderDownloadsPanel();
        }
    }, false, 32.0f);
    dl_btn->setShrink(0.0f);
    dl_btn->setMarginRight(6);
    right->addView(dl_btn);

    auto* refresh_btn = omnidesk::ui::makeButton("刷新", [this]() {
        if (showing_container_detail_) {
            openContainerView(active_container_);
            return;
        }
        if (active_tab_ == SubTab::ForYou) {
            triggerCurationLoad(selected_archive_date_, false);
            triggerFeedLoad("recommendations", "", true);
        } else if (active_tab_ == SubTab::AiCuration) {
            triggerCurationLoad(selected_archive_date_, false);
        } else if (active_tab_ == SubTab::Recent) {
            triggerFeedLoad("heavy-rotation", "", true);
        } else if (active_tab_ == SubTab::Library) {
            triggerFeedLoad("library-playlists", "", true);
        } else if (active_tab_ == SubTab::Search) {
            if (!search_query_.empty()) triggerFeedLoad("search", search_query_, true);
            else promptSearch();
        } else {
            triggerExportsRefresh();
            renderNowPlayingBody();
        }
    }, false, 32.0f);
    refresh_btn->setShrink(0.0f);
    refresh_btn->setMarginRight(6);
    right->addView(refresh_btn);

    auto* auth_btn = omnidesk::ui::makeButton("账号", [this]() {
        show_auth_panel_ = !show_auth_panel_;
        refreshHeader();
    }, false, 32.0f);
    auth_btn->setShrink(0.0f);
    right->addView(auth_btn);

    header->addView(right);
    addView(header);

    // Island Lyrics floating live strip (AppleMusicIslandLyrics style)
    island_bar_box_ = new brls::Box(brls::Axis::ROW);
    island_bar_box_->setWidthPercentage(100);
    island_bar_box_->setShrink(0.0f);
    island_bar_box_->setPadding(7, 14, 7, 14);
    island_bar_box_->setCornerRadius(999.0f);
    island_bar_box_->setBackgroundColor(omnidesk::ui::color("desktop/active"));
    island_bar_box_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    island_bar_box_->setAlignItems(brls::AlignItems::CENTER);
    island_bar_box_->setMarginBottom(8);
    island_bar_box_->setVisibility(brls::Visibility::GONE);

    auto* island_left = new brls::Box(brls::Axis::ROW);
    island_left->setAlignItems(brls::AlignItems::CENTER);
    island_left->setGrow(1.0f);
    island_left->setShrink(1.0f);
    island_left->setMinWidth(0);
    island_left->setFocusable(true);
    island_title_label_ = omnidesk::ui::makeLabel("", 13.0f, "brls/accent");
    island_title_label_->setSingleLine(true);
    island_title_label_->setShrink(0.0f);
    island_title_label_->setMarginRight(12);
    island_left->addView(island_title_label_);
    island_lyric_label_ = omnidesk::ui::makeLabel("", 13.0f, "brls/text");
    island_lyric_label_->setSingleLine(true);
    island_lyric_label_->setGrow(1.0f);
    island_lyric_label_->setShrink(1.0f);
    island_left->addView(island_lyric_label_);
    island_left->registerClickAction([this](brls::View*) {
        brls::sync([this]() { setSubTab(SubTab::NowPlaying); });
        return true;
    });
    island_bar_box_->addView(island_left);

    auto* island_btns = new brls::Box(brls::Axis::ROW);
    island_btns->setShrink(0.0f);
    auto* is_pp = omnidesk::ui::makeButton("播放/暂停", [this]() {
        if (engine_) engine_->togglePlayPause();
    }, false, 26.0f);
    is_pp->setMarginLeft(8);
    island_btns->addView(is_pp);
    auto* is_nx = omnidesk::ui::makeButton("下一首", [this]() {
        if (engine_) engine_->playAdjacentTrack(1);
    }, false, 26.0f);
    is_nx->setMarginLeft(6);
    island_btns->addView(is_nx);
    island_bar_box_->addView(island_btns);
    addView(island_bar_box_);

    auth_panel_box_ = new brls::Box(brls::Axis::COLUMN);
    auth_panel_box_->setWidthPercentage(100);
    auth_panel_box_->setShrink(0.0f);
    auth_panel_box_->setMarginBottom(8);
    auth_panel_box_->setVisibility(brls::Visibility::GONE);
    addView(auth_panel_box_);

    downloads_panel_box_ = new brls::Box(brls::Axis::COLUMN);
    downloads_panel_box_->setWidthPercentage(100);
    downloads_panel_box_->setShrink(0.0f);
    downloads_panel_box_->setMarginBottom(8);
    downloads_panel_box_->setVisibility(brls::Visibility::GONE);
    addView(downloads_panel_box_);

    sub_tabs_box_ = new brls::Box(brls::Axis::ROW);
    sub_tabs_box_->setWidthPercentage(100);
    sub_tabs_box_->setShrink(0.0f);
    sub_tabs_box_->setMarginBottom(10);
    addView(sub_tabs_box_);

    scroll_frame_ = new brls::ScrollingFrame();
    scroll_frame_->setGrow(1.0f);
    scroll_frame_->setShrink(1.0f);
    scroll_frame_->setWidthPercentage(100);
    scroll_frame_->setMinHeight(0);
    stage_box_ = new brls::Box(brls::Axis::COLUMN);
    stage_box_->setWidthPercentage(100);
    scroll_frame_->setContentView(stage_box_);
    stage_box_->setWidthPercentage(100);
    addView(scroll_frame_);

    refreshHeader();
    refreshIslandBar();
    renderActiveTab();
}

void MusicWorkspaceView::syncStageLayout() {
    if (!stage_box_ || !scroll_frame_) return;
    const float sw = scroll_frame_->getWidth();
    if (!std::isnan(sw) && sw > 16.0f) {
        stage_box_->setWidth(sw);
    } else {
        stage_box_->setWidthPercentage(100);
    }
    stage_box_->invalidate();
    scroll_frame_->invalidate();
}

void MusicWorkspaceView::refreshIslandBar() {
    if (!island_bar_box_ || !engine_) return;
    MusicTrack tr;
    PlaybackSessionInfo sess;
    LyricBundle lyr;
    std::vector<MusicTrack> q;
    size_t qidx = 0;
    if (!engine_->getActivePlaybackState(tr, sess, lyr, q, qidx)) {
        island_bar_box_->setVisibility(brls::Visibility::GONE);
        return;
    }
    island_bar_box_->setVisibility(brls::Visibility::VISIBLE);
    const std::string qbadge = sess.ok ? sess.formattedQuality()
                                       : (tr.quality_label.empty() ? "ALAC" : tr.quality_label);
    if (island_title_label_) {
        island_title_label_->setText("♫ " + clipText(tr.title, 28) + " · " + clipText(tr.artist, 20) + " [" + qbadge + "]");
    }
    if (island_lyric_label_) {
        std::string line_txt = lyr.status_text.empty() ? "点击进入双语同步歌词舞台" : lyr.status_text;
        if (!lyr.lines.empty()) {
            const double pos = omnidesk::MPVCore::instance().state().position_sec;
            int idx = 0;
            for (size_t i = 0; i < lyr.lines.size(); ++i) {
                if (lyr.lines[i].has_time && pos >= lyr.lines[i].time_sec) idx = static_cast<int>(i);
            }
            const auto& ln = lyr.lines[static_cast<size_t>(idx)];
            line_txt = ln.original;
            if (!ln.translation.empty()) line_txt += "  ｜  " + ln.translation;
        }
        island_lyric_label_->setText(clipText(line_txt, 96));
    }
}

void MusicWorkspaceView::refreshHeader() {
    if (!engine_) return;
    DrmStatus drm = engine_->cachedDrmStatus();
    if (drm.auth_state == "challenging") show_auth_panel_ = true;

    if (drm_pill_box_ && drm_pill_label_) {
        const bool ready = (drm.fairplay_state == "ready");
        drm_pill_box_->setBackgroundColor(
            omnidesk::ui::color(ready ? "status/ok_bg" : "status/warn_bg"));
        drm_pill_label_->setTextColor(
            omnidesk::ui::color(ready ? "status/ok_fg" : "status/warn_fg"));
        drm_pill_label_->setText(drmBadgeText(drm));
    }

    if (sub_tabs_box_) {
        sub_tabs_box_->clearViews();
        struct TabDef { SubTab t; const char* label; };
        const TabDef tabs[] = {
            {SubTab::ForYou, "为你推荐"},
            {SubTab::AiCuration, "✨ AI 策展反写"},
            {SubTab::Recent, "最近常听"},
            {SubTab::Library, "资料库歌单"},
            {SubTab::Search, "搜索发现"},
            {SubTab::NowPlaying, "正在播放 & 双语歌词"},
        };
        for (const auto& td : tabs) {
            auto* b = omnidesk::ui::makeButton(td.label, [this, tab = td.t]() {
                brls::sync([this, tab]() { setSubTab(tab); });
            }, active_tab_ == td.t, 33.0f);
            b->setShrink(0.0f);
            b->setMarginRight(8);
            sub_tabs_box_->addView(b);
        }
    }

    if (auth_panel_box_) {
        auth_panel_box_->clearViews();
        if (!show_auth_panel_) {
            auth_panel_box_->setVisibility(brls::Visibility::GONE);
        } else {
            auth_panel_box_->setVisibility(brls::Visibility::VISIBLE);
            auto* card = omnidesk::ui::makeCard("desktop/surface", 14.0f, 10.0f);
            card->addView(omnidesk::ui::makeLabel(
                "Apple ID 原生 FairPlay 无损认证与云端写回 Token 管理", 15.0f, "brls/text"));

            std::string desc = "当前状态: " + drmBadgeText(drm);
            desc += engine_->adamClient().hasUsableAuthForWrite()
                        ? " · 云端歌单写回已就绪 (Developer JWT + MUT)"
                        : " · 云端歌单写回待导入 media-user-token（当前自动本地暂存）";
            if (!drm.challenge_prompt.empty()) desc += " · " + drm.challenge_prompt;
            if (!drm.error.empty()) desc += " (" + drm.error + ")";
            auto* info = omnidesk::ui::makeLabel(desc, 12.5f, "font/grey");
            info->setMarginTop(4);
            info->setMarginBottom(8);
            card->addView(info);

            auto* row1 = new brls::Box(brls::Axis::ROW);
            auto* login_btn = omnidesk::ui::makeButton("Apple ID 登录", [this]() {
                promptAppleIdLogin();
            }, true, 32.0f);
            login_btn->setMarginRight(8);
            row1->addView(login_btn);

            auto* code_btn = omnidesk::ui::makeButton("填入 2FA 验证码", [this]() {
                promptTwoFactorCode();
            }, drm.auth_state == "challenging", 32.0f);
            code_btn->setMarginRight(8);
            row1->addView(code_btn);

            auto* resend_btn = omnidesk::ui::makeButton("重发验证码", [this]() {
                std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                std::thread([eng = engine_, alive_w]() {
                    DrmStatus st;
                    std::string err;
                    bool ok = eng->resendTwoFactorCode(st, err);
                    brls::sync([alive_w, ok, err]() {
                        auto a = alive_w.lock();
                        if (!a || !a->load(std::memory_order_acquire)) return;
                        brls::Application::notify(ok ? "已请求重发 2FA 验证码" : ("重发失败: " + err));
                    });
                }).detach();
            }, false, 32.0f);
            resend_btn->setMarginRight(8);
            row1->addView(resend_btn);

            auto* chrome_btn = omnidesk::ui::makeButton("从 Chrome 导入 Token", [this]() {
                std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                std::thread([this, eng = engine_, alive_w]() {
                    std::string err;
                    bool ok = eng->tryImportMutFromChromeCookies(err);
                    brls::sync([this, alive_w, ok, err]() {
                        auto a = alive_w.lock();
                        if (!a || !a->load(std::memory_order_acquire)) return;
                        refreshHeader();
                        brls::Application::notify(ok ? "已导入 media-user-token，云端歌单反写已激活" : err);
                    });
                }).detach();
            }, false, 32.0f);
            chrome_btn->setMarginRight(8);
            row1->addView(chrome_btn);

            auto* mut_btn = omnidesk::ui::makeButton("手动填入 MUT", [this]() {
                promptMediaUserToken();
            }, false, 32.0f);
            mut_btn->setMarginRight(8);
            row1->addView(mut_btn);

            auto* logout_btn = omnidesk::ui::makeButton("退出 DRM", [this]() {
                std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                std::thread([eng = engine_, alive_w]() {
                    std::string err;
                    bool ok = eng->logoutAppleId(err);
                    brls::sync([alive_w, ok, err]() {
                        auto a = alive_w.lock();
                        if (!a || !a->load(std::memory_order_acquire)) return;
                        brls::Application::notify(ok ? "已退出 Apple ID DRM 会话" : err);
                    });
                }).detach();
            }, false, 32.0f);
            row1->addView(logout_btn);

            card->addView(row1);
            auth_panel_box_->addView(card);
        }
    }
}

void MusicWorkspaceView::renderDownloadsPanel() {
    if (!downloads_panel_box_) return;
    downloads_panel_box_->clearViews();
    if (!show_downloads_panel_) {
        downloads_panel_box_->setVisibility(brls::Visibility::GONE);
        return;
    }
    downloads_panel_box_->setVisibility(brls::Visibility::VISIBLE);

    auto* card = omnidesk::ui::makeCard("desktop/surface", 14.0f, 10.0f);
    auto* top = new brls::Box(brls::Axis::ROW);
    top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    top->setAlignItems(brls::AlignItems::CENTER);
    top->addView(omnidesk::ui::makeLabel(
        "Adam 无损导出队列 (保存至 ~/Music/AppleMusic · 内嵌 3000px 封面与同步歌词)", 14.5f, "brls/text"));

    auto* btns = new brls::Box(brls::Axis::ROW);
    auto* ref_b = omnidesk::ui::makeButton("刷新进度", [this]() { triggerExportsRefresh(); }, false, 28.0f);
    ref_b->setMarginRight(6);
    btns->addView(ref_b);
    btns->addView(omnidesk::ui::makeButton("收起", [this]() {
        show_downloads_panel_ = false;
        renderDownloadsPanel();
    }, false, 28.0f));
    top->addView(btns);
    card->addView(top);

    if (export_jobs_.empty()) {
        auto* empty = omnidesk::ui::makeLabel(
            "当前暂无导出任务。点击任意歌曲或歌单右侧「⬇ 无损」即可直接解密导出 ALAC .m4a / FLAC。",
            13.0f, "font/grey");
        empty->setMarginTop(6);
        card->addView(empty);
    } else {
        const size_t max_show = std::min<size_t>(export_jobs_.size(), 6);
        for (size_t i = 0; i < max_show; ++i) {
            const auto& jb = export_jobs_[i];
            auto* row = omnidesk::ui::makeCard("desktop/soft", 8.0f, 6.0f);
            row->setAxis(brls::Axis::ROW);
            row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            row->setAlignItems(brls::AlignItems::CENTER);
            row->setMarginTop(6);

            std::string title = jb.title.empty() ? jb.asset_id : (jb.title + (jb.artist.empty() ? "" : (" - " + jb.artist)));
            std::string detail = phaseLabel(jb.phase);
            if (jb.phase == "downloading") detail += " " + std::to_string(jb.percent) + "%";
            if (!jb.output.empty() && jb.phase == "done") detail += " · " + jb.output;
            if (!jb.error.empty()) detail += " · " + jb.error;

            auto* col = new brls::Box(brls::Axis::COLUMN);
            col->setGrow(1.0f);
            col->addView(omnidesk::ui::makeLabel(clipText(title, 60), 13.5f, "brls/text"));
            col->addView(omnidesk::ui::makeLabel(clipText(detail, 88), 12.0f, "font/grey"));
            row->addView(col);

            if (jb.phase == "failed" || jb.phase == "cancelled") {
                row->addView(omnidesk::ui::makeButton("重试", [this, jid = jb.job_id]() {
                    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                    std::thread([this, eng = engine_, alive_w, jid]() {
                        ExportJobInfo dummy;
                        std::string err;
                        eng->retryExportJob(jid, dummy, err);
                        brls::sync([this, alive_w]() {
                            auto a = alive_w.lock();
                            if (a && a->load(std::memory_order_acquire)) triggerExportsRefresh();
                        });
                    }).detach();
                }, false, 26.0f));
            } else if (jb.phase != "done") {
                row->addView(omnidesk::ui::makeButton("取消", [this, jid = jb.job_id]() {
                    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                    std::thread([this, eng = engine_, alive_w, jid]() {
                        std::string err;
                        eng->cancelExportJob(jid, err);
                        brls::sync([this, alive_w]() {
                            auto a = alive_w.lock();
                            if (a && a->load(std::memory_order_acquire)) triggerExportsRefresh();
                        });
                    }).detach();
                }, false, 26.0f));
            }
            card->addView(row);
        }
    }
    downloads_panel_box_->addView(card);
}

void MusicWorkspaceView::setSubTab(SubTab tab) {
    active_tab_ = tab;
    showing_container_detail_ = false;
    refreshHeader();
    if (tab == SubTab::ForYou) {
        if (curation_snap_.available_dates.empty()) triggerCurationLoad(selected_archive_date_, false);
        triggerFeedLoad("recommendations");
    } else if (tab == SubTab::AiCuration) {
        triggerCurationLoad(selected_archive_date_, false);
    } else if (tab == SubTab::Recent) {
        triggerFeedLoad("heavy-rotation");
    } else if (tab == SubTab::Library) {
        triggerFeedLoad("library-playlists");
    } else if (tab == SubTab::Search) {
        if (!search_query_.empty()) triggerFeedLoad("search", search_query_);
        else renderActiveTab();
    } else {
        triggerExportsRefresh();
        renderActiveTab();
    }
}

void MusicWorkspaceView::triggerFeedLoad(const std::string& feed_id, const std::string& query, bool force) {
    if (!engine_) return;
    feed_loading_ = true;
    const uint64_t seq = ++feed_req_seq_;
    renderActiveTab();

    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w, seq, feed_id, query, force]() {
        FeedOutcome out = eng->loadFeed(feed_id, query, force);
        if (feed_id == "heavy-rotation" && (!out.ok || (out.containers.empty() && out.tracks.empty()))) {
            out = eng->loadFeed("recently-played", "", force);
        }
        brls::sync([this, alive_w, seq, feed_id, out]() {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            if (feed_id == "library-playlists" && out.ok) {
                library_playlists_ = out.containers;
            }
            if (seq != feed_req_seq_) return;
            feed_loading_ = false;
            current_feed_ = out;
            renderActiveTab();
        });
    }).detach();
}

void MusicWorkspaceView::triggerCurationLoad(const std::string& requested_date, bool silent) {
    if (!engine_) return;
    if (!silent) {
        curation_loading_ = true;
        renderActiveTab();
    }
    const uint64_t seq = ++curation_req_seq_;
    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w, seq, requested_date]() {
        CurationWorkspaceSnapshot snap = eng->loadCurationSnapshot(requested_date, true);
        brls::sync([this, alive_w, seq, snap]() {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            if (seq != curation_req_seq_) return;
            curation_loading_ = false;
            curation_snap_ = snap;
            selected_archive_date_ = snap.selected_date;
            if (active_tab_ == SubTab::AiCuration || active_tab_ == SubTab::ForYou) {
                renderActiveTab();
            }
        });
    }).detach();
}

void MusicWorkspaceView::triggerExportsRefresh() {
    if (!engine_) return;
    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w]() {
        std::string err;
        auto jobs = eng->listExportJobs(err);
        brls::sync([this, alive_w, jobs = std::move(jobs)]() mutable {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            export_jobs_ = std::move(jobs);
            renderDownloadsPanel();
        });
    }).detach();
}

void MusicWorkspaceView::triggerTrackExport(const MusicTrack& track, bool convert_to_flac) {
    if (!engine_) return;
    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w, track, convert_to_flac]() {
        ExportJobInfo job;
        std::string err;
        bool ok = eng->exportTrack(track, convert_to_flac, job, err);
        brls::sync([this, alive_w, ok, track, convert_to_flac, err]() {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            if (ok) {
                show_downloads_panel_ = true;
                triggerExportsRefresh();
                brls::Application::notify("已加入无损下载队列 (" +
                                          std::string(convert_to_flac ? "FLAC" : "ALAC M4A") + "): " + track.title);
            } else {
                brls::Application::notify("无损导出失败: " + err);
            }
        });
    }).detach();
}

void MusicWorkspaceView::triggerContainerExport(const ContainerItem& container, bool convert_to_flac) {
    if (!engine_) return;
    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w, container, convert_to_flac]() {
        ExportJobInfo job;
        std::string err;
        bool ok = eng->exportContainer(container, convert_to_flac, job, err);
        brls::sync([this, alive_w, ok, container, err]() {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            if (ok) {
                show_downloads_panel_ = true;
                triggerExportsRefresh();
                brls::Application::notify("已提交整单无损导出任务: " + container.name);
            } else {
                brls::Application::notify("整单导出失败: " + err);
            }
        });
    }).detach();
}

void MusicWorkspaceView::triggerInteractiveWriteback(const std::string& prompt) {
    if (!engine_ || ai_curating_) return;
    ai_prompt_draft_ = prompt;
    ai_curating_ = true;
    last_ai_banner_ = "正在执行 AI 选曲、中国区可播性预检并反写至 OmniDesk 与「" + ai_target_playlist_ + "」…";
    last_ai_banner_ok_ = true;
    if (active_tab_ != SubTab::AiCuration) {
        active_tab_ = SubTab::AiCuration;
        refreshHeader();
    }
    renderActiveTab();

    const std::string target_pl = ai_target_playlist_;
    const bool sync_pl = ai_sync_playlist_;
    const bool autoplay = ai_autoplay_;
    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w, prompt, target_pl, sync_pl, autoplay]() {
        InteractiveWritebackOutcome out = eng->curateAndWriteBackInteractive(
            prompt, target_pl, sync_pl, autoplay);
        CurationWorkspaceSnapshot snap = eng->loadCurationSnapshot(selected_archive_date_, true);
        brls::sync([this, alive_w, out, snap]() {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            ai_curating_ = false;
            curation_snap_ = snap;
            if (out.ok) {
                std::string msg = "✓ AI 反写成功：已生成 " + std::to_string(out.tracks.size()) +
                                  " 首中国区可播曲目并写回前端会话";
                if (out.revisit_count > 0) {
                    msg += "（含 " + std::to_string(out.revisit_count) + " 首经典重温）";
                }
                if (out.playlist_cloud_synced) {
                    msg += " · 已同步写回云端歌单「" + out.target_playlist_name + "」";
                } else if (out.playlist_local_staged) {
                    msg += " · 已暂存至本地交互歌单「" + out.target_playlist_name + "」";
                }
                last_ai_banner_ = msg;
                last_ai_banner_ok_ = true;
                brls::Application::notify(msg);
            } else {
                last_ai_banner_ = "⚠ AI 反写中止 [" + out.error_code + "]: " + out.error;
                last_ai_banner_ok_ = false;
                brls::Application::notify(last_ai_banner_);
            }
            renderActiveTab();
        });
    }).detach();
}

void MusicWorkspaceView::openContainerView(const ContainerItem& container) {
    if (!engine_) return;
    showing_container_detail_ = true;
    active_container_ = container;
    container_tracks_.clear();
    feed_loading_ = true;
    const uint64_t seq = ++feed_req_seq_;
    renderActiveTab();

    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w, seq, container]() {
        std::vector<MusicTrack> tracks;
        std::string err;
        bool ok = eng->openContainer(container, tracks, err);
        brls::sync([this, alive_w, seq, ok, tracks = std::move(tracks), err]() mutable {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            if (seq != feed_req_seq_) return;
            feed_loading_ = false;
            if (!ok && !err.empty()) {
                brls::Application::notify(err);
            }
            container_tracks_ = std::move(tracks);
            if (active_container_.cover_url.empty() &&
                !container_tracks_.empty() &&
                !container_tracks_.front().cover_url.empty()) {
                active_container_.cover_url = container_tracks_.front().cover_url;
            }
            renderActiveTab();
        });
    }).detach();
}

void MusicWorkspaceView::addTrackRowCard(brls::Box* parent,
                                         const std::vector<MusicTrack>& queue_context,
                                         size_t index,
                                         const std::string& fallback_cover) {
    if (!parent || index >= queue_context.size()) return;
    const auto& tr = queue_context[index];

    auto* row = omnidesk::ui::makeCard("desktop/surface", 11.0f, 9.0f);
    row->setAxis(brls::Axis::ROW);
    row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    row->setAlignItems(brls::AlignItems::CENTER);
    row->setMarginBottom(7);

    auto* left = new brls::Box(brls::Axis::ROW);
    left->setAlignItems(brls::AlignItems::CENTER);
    left->setGrow(1.0f);
    left->setShrink(1.0f);
    left->setMinWidth(0);
    left->setFocusable(true);

    const std::string cov = !tr.cover_url.empty() ? tr.cover_url : fallback_cover;
    if (!cov.empty()) {
        auto* iv = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
        iv->setDimensions(46, 46);
        iv->setShrink(0.0f);
        iv->setCornerRadius(6.0f);
        iv->setScalingType(brls::ImageScalingType::FILL);
        iv->setMarginRight(10);
        iv->setImageUrl(cov);
        left->addView(iv);
    } else {
        auto* ph = makeSquarePlaceholder(46.0f, tr.kind == "mv" ? "MV" : "♪");
        ph->setMarginRight(10);
        left->addView(ph);
    }

    auto* meta = new brls::Box(brls::Axis::COLUMN);
    meta->setGrow(1.0f);
    meta->setShrink(1.0f);
    meta->setMinWidth(0);
    auto* t_lbl = omnidesk::ui::makeLabel(
        std::to_string(index + 1) + ". " + clipText(tr.title, 64), 15.0f, "brls/text");
    t_lbl->setSingleLine(true);
    meta->addView(t_lbl);
    auto* s_lbl = omnidesk::ui::makeLabel(
        clipText(tr.artist + (tr.album.empty() ? "" : (" · " + tr.album)), 72), 13.0f, "font/grey");
    s_lbl->setSingleLine(true);
    meta->addView(s_lbl);
    left->addView(meta);
    left->registerClickAction([this, queue_context, index](brls::View*) {
        engine_->setQueueAndPlay(queue_context, index);
        brls::sync([this]() { setSubTab(SubTab::NowPlaying); });
        return true;
    });
    row->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    right->setShrink(0.0f);
    right->setAlignItems(brls::AlignItems::CENTER);
    if (!tr.quality_label.empty()) {
        auto* q = omnidesk::ui::makePill(tr.quality_label, "status/info_bg", "status/info_fg");
        q->setShrink(0.0f);
        q->setMarginRight(8);
        right->addView(q);
    }
    if (tr.duration_sec > 0.0) {
        auto* dl = omnidesk::ui::makeLabel(formatDuration(tr.duration_sec), 12.5f, "font/grey");
        dl->setMarginRight(8);
        right->addView(dl);
    }
    auto* next_b = omnidesk::ui::makeButton("+队列", [this, tr]() {
        engine_->appendTrackNext(tr);
        brls::Application::notify("已加入下一首播放: " + tr.title);
    }, false, 28.0f);
    next_b->setMarginRight(6);
    right->addView(next_b);

    auto* exp_b = omnidesk::ui::makeButton("⬇ 无损", [this, tr]() {
        triggerTrackExport(tr, false);
    }, false, 28.0f);
    right->addView(exp_b);

    row->addView(right);
    parent->addView(row);
}

void MusicWorkspaceView::renderForYouHeroCard() {
    if (curation_snap_.daily_tracks.empty()) return;

    auto* hero = omnidesk::ui::makeCard("desktop/surface", 16.0f, 12.0f);
    hero->setMarginBottom(14);

    auto* htop = new brls::Box(brls::Axis::ROW);
    htop->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    htop->setAlignItems(brls::AlignItems::CENTER);

    auto* hleft = new brls::Box(brls::Axis::COLUMN);
    hleft->setGrow(1.0f);
    hleft->addView(omnidesk::ui::makeLabel(
        "✦ AI 今日精选 · ChatGPT Daily 5 (" + curation_snap_.selected_date + ")", 17.5f, "brls/accent"));
    auto* sub = omnidesk::ui::makeLabel(
        "已回灌 " + std::to_string(curation_snap_.total_dates) + " 期归档 / 共 " +
            std::to_string(curation_snap_.total_hydrated_tracks) +
            " 首中国区验证无损曲目 · 支持一键播放与 AI 灵感反写",
        12.5f, "font/grey");
    sub->setMarginTop(3);
    hleft->addView(sub);
    htop->addView(hleft);

    auto* hbtns = new brls::Box(brls::Axis::ROW);
    hbtns->setShrink(0.0f);
    std::vector<MusicTrack> daily_q;
    for (const auto& ct : curation_snap_.daily_tracks) daily_q.push_back(ct.track);

    auto* play_b = omnidesk::ui::makeButton("▶ 播放今日 5 首", [this, daily_q]() {
        engine_->setQueueAndPlay(daily_q, 0);
        setSubTab(SubTab::NowPlaying);
    }, true, 32.0f);
    play_b->setMarginRight(8);
    hbtns->addView(play_b);

    hbtns->addView(omnidesk::ui::makeButton("打开 AI 策展反写台 →", [this]() {
        setSubTab(SubTab::AiCuration);
    }, false, 32.0f));
    htop->addView(hbtns);
    hero->addView(htop);

    auto* strip = new brls::Box(brls::Axis::ROW);
    strip->setWidthPercentage(100);
    strip->setMarginTop(12);
    const size_t n = std::min<size_t>(curation_snap_.daily_tracks.size(), 5);
    for (size_t i = 0; i < n; ++i) {
        const auto& ct = curation_snap_.daily_tracks[i];
        auto* tile = omnidesk::ui::makeCard("desktop/soft", 10.0f, 9.0f);
        tile->setWidth(0);
        tile->setGrow(1.0f);
        tile->setShrink(1.0f);
        if (i + 1 < n) tile->setMarginRight(10);
        tile->setFocusable(true);

        auto* r1 = new brls::Box(brls::Axis::ROW);
        r1->setAlignItems(brls::AlignItems::CENTER);
        if (!ct.track.cover_url.empty()) {
            auto* iv = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
            iv->setDimensions(44, 44);
            iv->setShrink(0.0f);
            iv->setCornerRadius(6.0f);
            iv->setScalingType(brls::ImageScalingType::FILL);
            iv->setMarginRight(8);
            iv->setImageUrl(ct.track.cover_url);
            r1->addView(iv);
        } else {
            auto* ph = makeSquarePlaceholder(44.0f, "#" + std::to_string(i + 1));
            ph->setMarginRight(8);
            r1->addView(ph);
        }
        auto* m = new brls::Box(brls::Axis::COLUMN);
        m->setGrow(1.0f);
        m->setShrink(1.0f);
        m->setMinWidth(0);
        auto* tl = omnidesk::ui::makeLabel(clipText(ct.track.title, 26), 13.5f, "brls/text");
        tl->setSingleLine(true);
        m->addView(tl);
        auto* al = omnidesk::ui::makeLabel(clipText(ct.track.artist, 24), 12.0f, "font/grey");
        al->setSingleLine(true);
        m->addView(al);
        r1->addView(m);
        tile->addView(r1);

        if (!ct.why_recommended.empty()) {
            auto* wl = omnidesk::ui::makeLabel(clipText(ct.why_recommended, 66), 11.5f, "font/grey", 1.35f);
            wl->setMarginTop(6);
            tile->addView(wl);
        }

        tile->registerClickAction([this, daily_q, i](brls::View*) {
            engine_->setQueueAndPlay(daily_q, i);
            brls::sync([this]() { setSubTab(SubTab::NowPlaying); });
            return true;
        });
        strip->addView(tile);
    }
    hero->addView(strip);
    stage_box_->addView(hero);
}

void MusicWorkspaceView::renderAiCurationBody() {
    // 1. Studio Header & Reverse-Write Console
    auto* console = omnidesk::ui::makeCard("desktop/surface", 16.0f, 12.0f);
    console->setMarginBottom(14);

    auto* top = new brls::Box(brls::Axis::ROW);
    top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    top->setAlignItems(brls::AlignItems::CENTER);

    auto* tcol = new brls::Box(brls::Axis::COLUMN);
    tcol->setGrow(1.0f);
    tcol->addView(omnidesk::ui::makeLabel(
        "✨ OmniDesk AI 音乐策展与双账本歌单反写台", 18.0f, "brls/text"));
    auto* sub = omnidesk::ui::makeLabel(
        "支持界面直接 AI 点歌反写，也支持外部 Agent 调用 recommend.feed.v1 / media.playlist.v1 实时写回并自动同步刷新",
        12.5f, "font/grey");
    sub->setMarginTop(3);
    tcol->addView(sub);
    top->addView(tcol);

    auto* prompt_btn = omnidesk::ui::makeButton(
        ai_curating_ ? "⏳ 正在生成并反写…" : "✍️ 输入灵感提示词并 AI 反写",
        [this]() { promptAiWriteback(); }, true, 36.0f);
    prompt_btn->setShrink(0.0f);
    top->addView(prompt_btn);
    console->addView(top);

    // Quick scene chips
    auto* chips = new brls::Box(brls::Axis::ROW);
    chips->setMarginTop(10);
    const std::pair<const char*, const char*> presets[] = {
        {"🌧 雨夜器乐钢琴", "雨夜安静阅读与思考的现代古典钢琴与氛围器乐"},
        {"🎷 深夜爵士人声", "深夜微醺放松的温暖爵士三重奏与柔和人声"},
        {"⚡ 深度专注电子", "适合深夜写代码深度专注的克制电子与后摇律动"},
        {"🌅 清晨原声民谣", "清晨阳光下的清新原声木吉他与独立民谣"},
        {"💎 历史遗珠重温", "从历史精选归档中挑选值得重温的高分经典曲目"},
    };
    for (const auto& [label, full_prompt] : presets) {
        auto* cb = omnidesk::ui::makeButton(label, [this, p = std::string(full_prompt)]() {
            triggerInteractiveWriteback(p);
        }, false, 30.0f);
        cb->setMarginRight(8);
        chips->addView(cb);
    }
    console->addView(chips);

    // Options & Guardrail status pills
    auto* opt_row = new brls::Box(brls::Axis::ROW);
    opt_row->setAlignItems(brls::AlignItems::CENTER);
    opt_row->setMarginTop(10);

    auto* sync_toggle = omnidesk::ui::makeButton(
        ai_sync_playlist_ ? "✓ 写回 Apple Music 灵感歌单" : "○ 仅写回 OmniDesk 前端",
        [this]() {
            ai_sync_playlist_ = !ai_sync_playlist_;
            renderActiveTab();
        }, ai_sync_playlist_, 28.0f);
    sync_toggle->setMarginRight(8);
    opt_row->addView(sync_toggle);

    auto* play_toggle = omnidesk::ui::makeButton(
        ai_autoplay_ ? "✓ 反写后立即播放" : "○ 反写后不打断播放",
        [this]() {
            ai_autoplay_ = !ai_autoplay_;
            renderActiveTab();
        }, ai_autoplay_, 28.0f);
    play_toggle->setMarginRight(8);
    opt_row->addView(play_toggle);

    auto* target_btn = omnidesk::ui::makeButton(
        "目标歌单: " + clipText(ai_target_playlist_, 28),
        [this]() { promptTargetPlaylistName(); }, false, 28.0f);
    target_btn->setMarginRight(10);
    opt_row->addView(target_btn);

    auto* guard_pill = omnidesk::ui::makePill(
        "🔒 每日账本保护: ChatGPT Daily 5 (" + curation_snap_.protected_playlist_id + ") 严禁交互篡改",
        "status/ok_bg", "status/ok_fg");
    opt_row->addView(guard_pill);
    console->addView(opt_row);

    if (!last_ai_banner_.empty()) {
        auto* banner = omnidesk::ui::makeCard(
            last_ai_banner_ok_ ? "status/ok_bg" : "status/warn_bg", 10.0f, 8.0f);
        banner->setMarginTop(10);
        banner->addView(omnidesk::ui::makeLabel(
            last_ai_banner_, 13.0f, last_ai_banner_ok_ ? "status/ok_fg" : "status/warn_fg"));
        console->addView(banner);
    }
    stage_box_->addView(console);

    // 2. Interactive Ephemeral Sessions (按需可重复灵感推荐集)
    auto* ix_hdr = new brls::Box(brls::Axis::ROW);
    ix_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    ix_hdr->setAlignItems(brls::AlignItems::CENTER);
    ix_hdr->setMarginBottom(8);
    ix_hdr->addView(omnidesk::ui::makeLabel(
        "按需可重复灵感推荐集 (interactive_ephemeral · 允许重温 · 不污染永久历史)", 16.5f, "brls/text"));
    if (!curation_snap_.interactive_sessions.empty()) {
        ix_hdr->addView(omnidesk::ui::makeButton("清空全部灵感会话", [this]() {
            std::weak_ptr<std::atomic<bool>> alive_w = alive_;
            std::thread([this, eng = engine_, alive_w]() {
                std::string err;
                eng->clearInteractiveSessions("", err);
                brls::sync([this, alive_w]() {
                    auto a = alive_w.lock();
                    if (a && a->load(std::memory_order_acquire)) {
                        triggerCurationLoad(selected_archive_date_, false);
                    }
                });
            }).detach();
        }, false, 28.0f));
    }
    stage_box_->addView(ix_hdr);

    if (curation_snap_.interactive_sessions.empty()) {
        auto* empty_ix = omnidesk::ui::makeCard("desktop/surface", 14.0f, 10.0f);
        empty_ix->setMarginBottom(14);
        empty_ix->addView(omnidesk::ui::makeLabel(
            "当前暂无交互式灵感会话。点击上方任意场景标签或「✍️ 输入灵感提示词并 AI 反写」，即可生成中国区可播曲目并写回「OmniDesk AI 灵感电台」。",
            13.5f, "font/grey", 1.45f));
        stage_box_->addView(empty_ix);
    } else {
        for (const auto& sess : curation_snap_.interactive_sessions) {
            auto* scard = omnidesk::ui::makeCard("desktop/surface", 14.0f, 10.0f);
            scard->setMarginBottom(12);

            auto* srow = new brls::Box(brls::Axis::ROW);
            srow->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            srow->setAlignItems(brls::AlignItems::CENTER);
            srow->setMarginBottom(8);

            auto* scol = new brls::Box(brls::Axis::COLUMN);
            scol->setGrow(1.0f);
            scol->addView(omnidesk::ui::makeLabel(sess.title, 15.5f, "brls/accent"));
            if (!sess.user_prompt.empty()) {
                scol->addView(omnidesk::ui::makeLabel("Prompt: " + sess.user_prompt, 12.5f, "font/grey"));
            }
            srow->addView(scol);

            std::vector<MusicTrack> sq;
            for (const auto& ct : sess.tracks) sq.push_back(ct.track);

            auto* sbtns = new brls::Box(brls::Axis::ROW);
            sbtns->setShrink(0.0f);
            auto* sp_b = omnidesk::ui::makeButton("▶ 播放本组", [this, sq]() {
                engine_->setQueueAndPlay(sq, 0);
                setSubTab(SubTab::NowPlaying);
            }, true, 30.0f);
            sp_b->setMarginRight(6);
            sbtns->addView(sp_b);

            auto* sw_b = omnidesk::ui::makeButton("♫ 写回灵感歌单", [this, items = sess.tracks]() {
                const std::string target = ai_target_playlist_;
                std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                std::thread([this, eng = engine_, alive_w, items, target]() {
                    nlohmann::json res;
                    std::string err;
                    bool ok = eng->syncInteractiveTracksToPlaylist(items, target, false, res, err);
                    brls::sync([this, alive_w, ok, res, target, err]() {
                        auto a = alive_w.lock();
                        if (!a || !a->load(std::memory_order_acquire)) return;
                        if (ok) {
                            bool cloud = res.is_object() && res.contains("cloud_synced") &&
                                         res["cloud_synced"].is_boolean() && res["cloud_synced"].get<bool>();
                            brls::Application::notify(cloud
                                ? ("已同步写回 Apple Music 云端歌单「" + target + "」")
                                : ("已暂存至本地交互歌单「" + target + "」"));
                            triggerCurationLoad(selected_archive_date_, true);
                        } else {
                            brls::Application::notify("歌单写回失败: " + err);
                        }
                    });
                }).detach();
            }, false, 30.0f);
            sw_b->setMarginRight(6);
            sbtns->addView(sw_b);

            sbtns->addView(omnidesk::ui::makeButton("移除", [this, sid = sess.session_id]() {
                std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                std::thread([this, eng = engine_, alive_w, sid]() {
                    std::string err;
                    eng->clearInteractiveSessions(sid, err);
                    brls::sync([this, alive_w]() {
                        auto a = alive_w.lock();
                        if (a && a->load(std::memory_order_acquire)) {
                            triggerCurationLoad(selected_archive_date_, false);
                        }
                    });
                }).detach();
            }, false, 30.0f));
            srow->addView(sbtns);
            scard->addView(srow);

            for (size_t i = 0; i < sess.tracks.size(); ++i) {
                const auto& ct = sess.tracks[i];
                auto* trow = omnidesk::ui::makeCard("desktop/soft", 10.0f, 8.0f);
                trow->setMarginTop(6);

                auto* top_line = new brls::Box(brls::Axis::ROW);
                top_line->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
                top_line->setAlignItems(brls::AlignItems::CENTER);

                auto* tl_left = new brls::Box(brls::Axis::ROW);
                tl_left->setAlignItems(brls::AlignItems::CENTER);
                tl_left->setGrow(1.0f);
                tl_left->setFocusable(true);
                if (!ct.track.cover_url.empty()) {
                    auto* iv = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
                    iv->setDimensions(44, 44);
                    iv->setShrink(0.0f);
                    iv->setCornerRadius(6.0f);
                    iv->setScalingType(brls::ImageScalingType::FILL);
                    iv->setMarginRight(10);
                    iv->setImageUrl(ct.track.cover_url);
                    tl_left->addView(iv);
                } else {
                    auto* ph = makeSquarePlaceholder(44.0f, "♪");
                    ph->setMarginRight(10);
                    tl_left->addView(ph);
                }
                auto* tmeta = new brls::Box(brls::Axis::COLUMN);
                tmeta->setGrow(1.0f);
                tmeta->addView(omnidesk::ui::makeLabel(
                    std::to_string(i + 1) + ". " + ct.track.title + " — " + ct.track.artist, 14.5f, "brls/text"));
                if (!ct.track.album.empty()) {
                    tmeta->addView(omnidesk::ui::makeLabel(ct.track.album, 12.0f, "font/grey"));
                }
                tl_left->addView(tmeta);
                tl_left->registerClickAction([this, sq, i](brls::View*) {
                    engine_->setQueueAndPlay(sq, i);
                    brls::sync([this]() { setSubTab(SubTab::NowPlaying); });
                    return true;
                });
                top_line->addView(tl_left);

                auto* tl_right = new brls::Box(brls::Axis::ROW);
                tl_right->setShrink(0.0f);
                tl_right->setAlignItems(brls::AlignItems::CENTER);
                if (!ct.revisit_badge.empty()) {
                    auto* rp = omnidesk::ui::makePill(ct.revisit_badge, "status/info_bg", "status/info_fg");
                    rp->setMarginRight(6);
                    tl_right->addView(rp);
                }
                tl_right->addView(omnidesk::ui::makeButton("⬇ 无损", [this, tr = ct.track]() {
                    triggerTrackExport(tr, false);
                }, false, 26.0f));
                top_line->addView(tl_right);
                trow->addView(top_line);

                if (!ct.why_recommended.empty()) {
                    auto* wy = omnidesk::ui::makeLabel("推荐理由：" + ct.why_recommended, 12.5f, "brls/text", 1.4f);
                    wy->setMarginTop(6);
                    trow->addView(wy);
                }
                if (!ct.listening_cue.empty()) {
                    auto* cu = omnidesk::ui::makeLabel("收听切入点：" + ct.listening_cue, 12.0f, "font/grey", 1.4f);
                    cu->setMarginTop(3);
                    trow->addView(cu);
                }
                scard->addView(trow);
            }
            stage_box_->addView(scard);
        }
    }

    // 3. Daily Immutable Archive (每日不重复精选归档 · ChatGPT Daily 5)
    auto* d_hdr = new brls::Box(brls::Axis::ROW);
    d_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    d_hdr->setAlignItems(brls::AlignItems::CENTER);
    d_hdr->setMarginTop(6);
    d_hdr->setMarginBottom(8);

    d_hdr->addView(omnidesk::ui::makeLabel(
        "每日不重复精选归档 · ChatGPT Daily 5 (daily_immutable · 共 " +
            std::to_string(curation_snap_.total_dates) + " 期 / " +
            std::to_string(curation_snap_.total_hydrated_tracks) + " 首)",
        16.5f, "brls/text"));

    auto* nav = new brls::Box(brls::Axis::ROW);
    nav->setShrink(0.0f);
    const auto& dates = curation_snap_.available_dates;
    auto it = std::find(dates.begin(), dates.end(), curation_snap_.selected_date);
    const bool has_older = (it != dates.end() && (it + 1) != dates.end());
    const bool has_newer = (it != dates.end() && it != dates.begin());

    auto* older_b = omnidesk::ui::makeButton("◀ 前一期", [this, has_older, it]() {
        if (has_older) triggerCurationLoad(*(it + 1), false);
    }, false, 30.0f);
    older_b->setMarginRight(6);
    nav->addView(older_b);

    auto* pick_b = omnidesk::ui::makeButton(
        "📅 " + (curation_snap_.selected_date.empty() ? std::string("选择日期") : curation_snap_.selected_date),
        [this]() { promptSelectArchiveDate(); }, false, 30.0f);
    pick_b->setMarginRight(6);
    nav->addView(pick_b);

    auto* newer_b = omnidesk::ui::makeButton("后一期 ▶", [this, has_newer, it]() {
        if (has_newer) triggerCurationLoad(*(it - 1), false);
    }, false, 30.0f);
    newer_b->setMarginRight(8);
    nav->addView(newer_b);

    std::vector<MusicTrack> dq;
    for (const auto& ct : curation_snap_.daily_tracks) dq.push_back(ct.track);
    auto* play_d = omnidesk::ui::makeButton("▶ 播放当期 5 首", [this, dq]() {
        if (!dq.empty()) {
            engine_->setQueueAndPlay(dq, 0);
            setSubTab(SubTab::NowPlaying);
        }
    }, true, 30.0f);
    play_d->setMarginRight(6);
    nav->addView(play_d);

    nav->addView(omnidesk::ui::makeButton("☁ 账本预检", [this, d = curation_snap_.selected_date]() {
        std::weak_ptr<std::atomic<bool>> alive_w = alive_;
        std::thread([eng = engine_, alive_w, d]() {
            nlohmann::json out;
            std::string err;
            bool ok = eng->checkDailyPlaylistSyncDryRun(d, out, err);
            brls::sync([alive_w, ok, out, err]() {
                auto a = alive_w.lock();
                if (!a || !a->load(std::memory_order_acquire)) return;
                if (ok) {
                    int planned = out.contains("planned_target_count") && out["planned_target_count"].is_number_integer()
                                      ? out["planned_target_count"].get<int>() : 0;
                    brls::Application::notify("双仓提交签名预检通过 · 计划曲目数: " + std::to_string(planned));
                } else {
                    brls::Application::notify("每日账本门禁提示: " + err);
                }
            });
        }).detach();
    }, false, 30.0f));

    d_hdr->addView(nav);
    stage_box_->addView(d_hdr);

    if (curation_loading_ && curation_snap_.daily_tracks.empty()) {
        stage_box_->addView(omnidesk::ui::makeLabel("正在加载归档数据与专辑封面…", 14.0f, "font/grey"));
        return;
    }

    for (size_t i = 0; i < curation_snap_.daily_tracks.size(); ++i) {
        const auto& ct = curation_snap_.daily_tracks[i];
        auto* card = omnidesk::ui::makeCard("desktop/surface", 14.0f, 10.0f);
        card->setMarginBottom(8);

        auto* r1 = new brls::Box(brls::Axis::ROW);
        r1->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        r1->setAlignItems(brls::AlignItems::CENTER);

        auto* left = new brls::Box(brls::Axis::ROW);
        left->setAlignItems(brls::AlignItems::CENTER);
        left->setGrow(1.0f);
        left->setFocusable(true);
        if (!ct.track.cover_url.empty()) {
            auto* iv = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
            iv->setDimensions(52, 52);
            iv->setShrink(0.0f);
            iv->setCornerRadius(7.0f);
            iv->setScalingType(brls::ImageScalingType::FILL);
            iv->setMarginRight(12);
            iv->setImageUrl(ct.track.cover_url);
            left->addView(iv);
        } else {
            auto* ph = makeSquarePlaceholder(52.0f, "#" + std::to_string(i + 1));
            ph->setMarginRight(12);
            left->addView(ph);
        }
        auto* meta = new brls::Box(brls::Axis::COLUMN);
        meta->setGrow(1.0f);
        meta->addView(omnidesk::ui::makeLabel(
            "#" + std::to_string(ct.position > 0 ? ct.position : static_cast<int>(i + 1)) + "  " +
                ct.track.title + " — " + ct.track.artist,
            15.5f, "brls/text"));
        meta->addView(omnidesk::ui::makeLabel(
            (ct.track.album.empty() ? "Apple Music 中国区精选" : ct.track.album) +
                " · song_id: " + ct.track.id,
            12.5f, "font/grey"));
        left->addView(meta);
        left->registerClickAction([this, dq, i](brls::View*) {
            engine_->setQueueAndPlay(dq, i);
            brls::sync([this]() { setSubTab(SubTab::NowPlaying); });
            return true;
        });
        r1->addView(left);

        auto* right = new brls::Box(brls::Axis::ROW);
        right->setShrink(0.0f);
        right->setAlignItems(brls::AlignItems::CENTER);
        auto* q = omnidesk::ui::makePill(
            ct.track.quality_label.empty() ? "CN · ALAC" : ct.track.quality_label,
            "status/info_bg", "status/info_fg");
        q->setMarginRight(8);
        right->addView(q);
        right->addView(omnidesk::ui::makeButton("⬇ 无损", [this, tr = ct.track]() {
            triggerTrackExport(tr, false);
        }, false, 28.0f));
        r1->addView(right);
        card->addView(r1);

        if (!ct.why_recommended.empty()) {
            auto* wy = omnidesk::ui::makeLabel("推荐理由：" + ct.why_recommended, 13.0f, "brls/text", 1.42f);
            wy->setMarginTop(8);
            card->addView(wy);
        }
        if (!ct.listening_cue.empty()) {
            auto* cu = omnidesk::ui::makeLabel("收听切入点：" + ct.listening_cue, 12.5f, "font/grey", 1.4f);
            cu->setMarginTop(4);
            card->addView(cu);
        }
        stage_box_->addView(card);
    }
}

void MusicWorkspaceView::renderRecentBody() {
    auto local_hist = engine_->recentPlayHistory();
    if (!local_hist.empty()) {
        auto* hdr = new brls::Box(brls::Axis::ROW);
        hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        hdr->setAlignItems(brls::AlignItems::CENTER);
        hdr->setMarginBottom(8);
        hdr->addView(omnidesk::ui::makeLabel(
            "本机 FairPlay 最近播放 (" + std::to_string(local_hist.size()) + ")", 16.5f, "brls/text"));
        hdr->addView(omnidesk::ui::makeButton("▶ 续播最近列表", [this, local_hist]() {
            engine_->setQueueAndPlay(local_hist, 0);
            setSubTab(SubTab::NowPlaying);
        }, false, 30.0f));
        stage_box_->addView(hdr);

        const size_t max_local = std::min<size_t>(local_hist.size(), 10);
        for (size_t i = 0; i < max_local; ++i) {
            addTrackRowCard(stage_box_, local_hist, i);
        }
    }

    auto* cloud_hdr = omnidesk::ui::makeLabel("Apple Music 云端最近常听 (Heavy Rotation)", 16.5f, "brls/text");
    cloud_hdr->setMarginTop(local_hist.empty() ? 0 : 12);
    cloud_hdr->setMarginBottom(8);
    stage_box_->addView(cloud_hdr);

    renderFeedBody(current_feed_);
}

void MusicWorkspaceView::renderActiveTab() {
    if (!stage_box_) return;
    if (active_tab_ == SubTab::NowPlaying) {
        renderNowPlayingBody();
        return;
    }

    mv_stage_ = nullptr;
    lyric_cards_.clear();
    lyric_orig_labels_.clear();
    lyric_times_.clear();
    stage_box_->clearViews();

    if (showing_container_detail_) {
        auto* top = new brls::Box(brls::Axis::ROW);
        top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top->setAlignItems(brls::AlignItems::CENTER);
        top->setMarginBottom(12);

        auto* tleft = new brls::Box(brls::Axis::ROW);
        tleft->setAlignItems(brls::AlignItems::CENTER);
        tleft->setGrow(1.0f);
        auto* back_b = omnidesk::ui::makeButton("← 返回", [this]() {
            ++feed_req_seq_;
            feed_loading_ = false;
            showing_container_detail_ = false;
            renderActiveTab();
        }, false, 32.0f);
        back_b->setMarginRight(12);
        tleft->addView(back_b);
        if (!active_container_.cover_url.empty()) {
            auto* civ = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
            civ->setDimensions(54, 54);
            civ->setShrink(0.0f);
            civ->setCornerRadius(8.0f);
            civ->setScalingType(brls::ImageScalingType::FILL);
            civ->setMarginRight(12);
            civ->setImageUrl(active_container_.cover_url);
            tleft->addView(civ);
        }
        auto* title_col = new brls::Box(brls::Axis::COLUMN);
        title_col->addView(omnidesk::ui::makeLabel(active_container_.name, 17.5f, "brls/text"));
        if (!active_container_.subtitle.empty()) {
            title_col->addView(omnidesk::ui::makeLabel(active_container_.subtitle, 13.0f, "font/grey"));
        }
        tleft->addView(title_col);
        top->addView(tleft);

        auto* tright = new brls::Box(brls::Axis::ROW);
        tright->setShrink(0.0f);
        if (!container_tracks_.empty()) {
            auto* play_all = omnidesk::ui::makeButton(
                "▶ 顺序播放 (" + std::to_string(container_tracks_.size()) + ")",
                [this]() {
                    engine_->setQueueAndPlay(container_tracks_, 0);
                    setSubTab(SubTab::NowPlaying);
                }, true, 34.0f);
            play_all->setMarginRight(8);
            tright->addView(play_all);
        }
        tright->addView(omnidesk::ui::makeButton("⬇ 整单无损导出", [this]() {
            triggerContainerExport(active_container_, false);
        }, false, 34.0f));
        top->addView(tright);
        stage_box_->addView(top);

        if (feed_loading_) {
            stage_box_->addView(omnidesk::ui::makeLabel("正在加载曲目列表…", 14.5f, "font/grey"));
            syncStageLayout();
            return;
        }
        if (container_tracks_.empty()) {
            stage_box_->addView(omnidesk::ui::makeLabel("暂无曲目（可尝试同步资料库或检查地区设置）", 14.5f, "font/grey"));
            syncStageLayout();
            return;
        }
        for (size_t i = 0; i < container_tracks_.size(); ++i) {
            addTrackRowCard(stage_box_, container_tracks_, i, active_container_.cover_url);
        }
        syncStageLayout();
        return;
    }

    if (active_tab_ == SubTab::AiCuration) {
        renderAiCurationBody();
        syncStageLayout();
        return;
    }

    if (active_tab_ == SubTab::Recent) {
        renderRecentBody();
        syncStageLayout();
        return;
    }

    if (active_tab_ == SubTab::Library) {
        renderLibraryBody();
        syncStageLayout();
        return;
    }

    if (active_tab_ == SubTab::Search) {
        auto* sbar = new brls::Box(brls::Axis::ROW);
        sbar->setAlignItems(brls::AlignItems::CENTER);
        sbar->setMarginBottom(12);
        auto* sb = omnidesk::ui::makeButton(
            search_query_.empty() ? "🔍 输入歌曲 / 专辑 / 艺人关键词…" : ("🔍 " + clipText(search_query_, 36)),
            [this]() { promptSearch(); }, true, 35.0f);
        sb->setMarginRight(10);
        sbar->addView(sb);

        for (const auto& [flt, lbl] : std::vector<std::pair<std::string, std::string>>{
                 {"all", "全部"}, {"songs", "单曲"}, {"containers", "专辑与歌单"}, {"mv", "MV"}}) {
            auto* fb = omnidesk::ui::makeButton(lbl, [this, flt]() {
                search_filter_ = flt;
                renderActiveTab();
            }, search_filter_ == flt, 30.0f);
            fb->setMarginRight(6);
            sbar->addView(fb);
        }

        if (!search_query_.empty()) {
            auto* ai_q = omnidesk::ui::makeButton("✨ 以此词 AI 策展反写", [this]() {
                triggerInteractiveWriteback(search_query_);
            }, false, 30.0f);
            sbar->addView(ai_q);
        }
        stage_box_->addView(sbar);

        FeedOutcome filtered = current_feed_;
        if (search_filter_ == "songs") {
            filtered.containers.clear();
            filtered.tracks.erase(
                std::remove_if(filtered.tracks.begin(), filtered.tracks.end(),
                               [](const MusicTrack& t) { return t.kind == "mv"; }),
                filtered.tracks.end());
        } else if (search_filter_ == "containers") {
            filtered.tracks.clear();
        } else if (search_filter_ == "mv") {
            filtered.containers.clear();
            filtered.tracks.erase(
                std::remove_if(filtered.tracks.begin(), filtered.tracks.end(),
                               [](const MusicTrack& t) { return t.kind != "mv"; }),
                filtered.tracks.end());
        }
        renderFeedBody(filtered);
        syncStageLayout();
        return;
    }

    // SubTab::ForYou
    renderForYouHeroCard();
    renderFeedBody(current_feed_);
    syncStageLayout();
}

void MusicWorkspaceView::renderLibraryBody() {
    auto* bar = new brls::Box(brls::Axis::ROW);
    bar->setAlignItems(brls::AlignItems::CENTER);
    bar->setMarginBottom(12);
    auto* sync_b = omnidesk::ui::makeButton("同步 Apple Music 云端资料库", [this]() {
        std::weak_ptr<std::atomic<bool>> alive_w = alive_;
        std::thread([this, eng = engine_, alive_w]() {
            nlohmann::json st;
            std::string err;
            bool ok = eng->syncLibrary(st, err);
            brls::sync([this, alive_w, ok, err]() {
                auto a = alive_w.lock();
                if (!a || !a->load(std::memory_order_acquire)) return;
                if (ok) {
                    brls::Application::notify("资料库同步完成");
                    triggerFeedLoad("library-playlists", "", true);
                } else {
                    brls::Application::notify("同步失败: " + err);
                }
            });
        }).detach();
    }, true, 34.0f);
    sync_b->setMarginRight(10);
    bar->addView(sync_b);

    auto* ref_b = omnidesk::ui::makeButton("刷新歌单", [this]() {
        triggerFeedLoad("library-playlists", "", true);
    }, false, 34.0f);
    ref_b->setMarginRight(10);
    bar->addView(ref_b);

    bar->addView(omnidesk::ui::makePill(
        "🔒 ChatGPT Daily 5 受每日门禁保护 · ✨ OmniDesk AI 灵感电台 接收交互反写",
        "desktop/soft", "font/grey"));
    stage_box_->addView(bar);

    renderFeedBody(current_feed_);
}

void MusicWorkspaceView::renderFeedBody(const FeedOutcome& outcome) {
    if (feed_loading_) {
        stage_box_->addView(omnidesk::ui::makeLabel("正在加载 Apple Music 曲库与推荐…", 14.5f, "font/grey"));
        return;
    }

    if (!outcome.ok && outcome.containers.empty() && outcome.tracks.empty()) {
        auto* card = omnidesk::ui::makeCard("desktop/surface", 16.0f, 10.0f);
        card->addView(omnidesk::ui::makeLabel(
            outcome.error.empty()
                ? "连接个性化曲库需要导入网页端 media-user-token 或登录 Apple ID（上方 AI 今日精选已可直接无损播放）"
                : outcome.error,
            14.0f, "font/grey", 1.45f));
        auto* btn_row = new brls::Box(brls::Axis::ROW);
        btn_row->setMarginTop(10);
        auto* login_b = omnidesk::ui::makeButton("账号与 Token 管理", [this]() {
            show_auth_panel_ = true;
            refreshHeader();
        }, true, 32.0f);
        login_b->setMarginRight(8);
        btn_row->addView(login_b);
        auto* search_b = omnidesk::ui::makeButton("搜索全站曲库", [this]() {
            promptSearch();
        }, false, 32.0f);
        search_b->setMarginRight(8);
        btn_row->addView(search_b);
        btn_row->addView(omnidesk::ui::makeButton("重试", [this]() {
            if (active_tab_ == SubTab::ForYou) triggerFeedLoad("recommendations", "", true);
            else if (active_tab_ == SubTab::Recent) triggerFeedLoad("heavy-rotation", "", true);
            else if (active_tab_ == SubTab::Library) triggerFeedLoad("library-playlists", "", true);
            else if (active_tab_ == SubTab::Search) promptSearch();
        }, false, 32.0f));
        card->addView(btn_row);
        stage_box_->addView(card);
        return;
    }

    if (outcome.containers.empty() && outcome.tracks.empty()) {
        auto* card = omnidesk::ui::makeCard("desktop/surface", 16.0f, 10.0f);
        const std::string empty_msg =
            (active_tab_ == SubTab::Search)
                ? (search_query_.empty() ? "点击上方「🔍 输入歌曲 / 专辑 / 艺人关键词…」检索 Apple Music 全站无损目录"
                                         : ("未找到与「" + search_query_ + "」匹配的结果"))
                : "当前栏目暂无更多云端条目，可浏览「✨ AI 策展反写」或直接搜索曲库";
        card->addView(omnidesk::ui::makeLabel(empty_msg, 14.0f, "font/grey", 1.45f));
        stage_box_->addView(card);
        return;
    }

    if (!outcome.containers.empty()) {
        stage_box_->addView(omnidesk::ui::makeLabel("精选歌单与专辑", 16.5f, "brls/text"));
        brls::Box* row = nullptr;
        int col = 0;
        for (const auto& c : outcome.containers) {
            if (col % 3 == 0) {
                row = new brls::Box(brls::Axis::ROW);
                row->setWidthPercentage(100);
                row->setMarginTop(12);
                stage_box_->addView(row);
            }
            auto* card = omnidesk::ui::makeCard("desktop/surface", 12.0f, 11.0f);
            card->setWidth(0);
            card->setGrow(1.0f);
            card->setShrink(1.0f);
            if (col % 3 != 2) card->setMarginRight(12);
            card->setFocusable(true);

            if (!c.cover_url.empty()) {
                auto* iv = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
                iv->setWidthPercentage(100);
                iv->setHeight(148);
                iv->setCornerRadius(8.0f);
                iv->setScalingType(brls::ImageScalingType::FILL);
                iv->setMarginBottom(8);
                iv->setImageUrl(c.cover_url);
                card->addView(iv);
            } else {
                auto* ph = new brls::Box(brls::Axis::COLUMN);
                ph->setWidthPercentage(100);
                ph->setHeight(148);
                ph->setCornerRadius(8.0f);
                ph->setBackgroundColor(omnidesk::ui::color("desktop/soft"));
                ph->setJustifyContent(brls::JustifyContent::CENTER);
                ph->setAlignItems(brls::AlignItems::CENTER);
                ph->setMarginBottom(8);
                ph->addView(omnidesk::ui::makePill("Apple Music", "desktop/surface", "domain/media"));
                card->addView(ph);
            }

            if (c.id == MusicEngine::kProtectedDailyPlaylistId ||
                c.name == MusicEngine::kProtectedDailyPlaylistName) {
                auto* b = omnidesk::ui::makePill("🔒 每日精选账本", "status/ok_bg", "status/ok_fg");
                b->setMarginBottom(5);
                card->addView(b);
            } else if (c.name == MusicEngine::kDefaultInteractivePlaylistName) {
                auto* b = omnidesk::ui::makePill("✨ AI 灵感反写目标", "status/info_bg", "status/info_fg");
                b->setMarginBottom(5);
                card->addView(b);
            }

            card->addView(omnidesk::ui::makeLabel(clipText(c.name, 52), 15.0f, "brls/text", 1.32f));
            std::string sub = c.subtitle.empty() ? c.type : c.subtitle;
            auto* sl = omnidesk::ui::makeLabel(clipText(sub, 46), 13.0f, "font/grey");
            sl->setMarginTop(3);
            card->addView(sl);

            card->registerClickAction([this, c](brls::View*) {
                brls::sync([this, c]() { openContainerView(c); });
                return true;
            });
            row->addView(card);
            ++col;
        }
        if (row && (col % 3) != 0) {
            for (int s = col % 3; s < 3; ++s) {
                auto* spacer = new brls::Box(brls::Axis::COLUMN);
                spacer->setWidth(0);
                spacer->setGrow(1.0f);
                spacer->setShrink(1.0f);
                spacer->setFocusable(false);
                if (s != 2) spacer->setMarginRight(12);
                row->addView(spacer);
            }
        }
    }

    if (!outcome.tracks.empty()) {
        auto* hdr = omnidesk::ui::makeLabel("单曲与 MV", 16.5f, "brls/text");
        hdr->setMarginTop(outcome.containers.empty() ? 0 : 16);
        hdr->setMarginBottom(8);
        stage_box_->addView(hdr);

        for (size_t i = 0; i < outcome.tracks.size(); ++i) {
            addTrackRowCard(stage_box_, outcome.tracks, i);
        }
    }
}

void MusicWorkspaceView::renderNowPlayingBody() {
    if (!stage_box_ || !engine_) return;

    MusicTrack track;
    PlaybackSessionInfo sess;
    LyricBundle lyr;
    std::vector<MusicTrack> queue;
    size_t qidx = 0;
    bool has_track = engine_->getActivePlaybackState(track, sess, lyr, queue, qidx);
    std::string disp_mode = engine_->displayMode();

    mv_stage_ = nullptr;
    lyric_cards_.clear();
    lyric_orig_labels_.clear();
    lyric_times_.clear();
    active_lyric_idx_ = -1;
    stage_box_->clearViews();

    if (!has_track) {
        auto* empty = omnidesk::ui::makeCard("desktop/surface", 20.0f, 12.0f);
        empty->addView(omnidesk::ui::makeLabel("尚未播放曲目", 18.0f, "brls/text"));
        auto* hint = omnidesk::ui::makeLabel(
            "可在「为你推荐」点击今日 Daily 5，或在「✨ AI 策展反写 / 资料库歌单 / 搜索发现」点选任意曲目，通过本机 Adam FairPlay 解密 24bit/192kHz ALAC 播放。",
            14.0f, "font/grey", 1.5f);
        hint->setMarginTop(8);
        empty->addView(hint);
        stage_box_->addView(empty);
        syncStageLayout();
        return;
    }

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    // Left: Cover / MV Stage + Transport + Lossless Export + Queue
    auto* left_card = omnidesk::ui::makeCard("desktop/surface", 16.0f, 12.0f);
    left_card->setWidthPercentage(38);
    left_card->setShrink(0.0f);
    left_card->setMarginRight(16);

    if (track.kind == "mv" && sess.ok) {
        mv_stage_ = new omnidesk::MpvStageView(&omnidesk::MPVCore::instance());
        mv_stage_->setStageMinHeight(220.0f);
        mv_stage_->setTitle(track.title);
        mv_stage_->setSubtitles(toSdkLyricLines(lyr.lines));
        left_card->addView(mv_stage_);
    } else {
        auto* art = omnidesk::ui::makeCard("desktop/active", 12.0f, 10.0f);
        art->setHeight(226);
        art->setJustifyContent(brls::JustifyContent::CENTER);
        art->setAlignItems(brls::AlignItems::CENTER);
        const std::string cov = !track.cover_url.empty() ? track.cover_url : sess.cover_url;
        if (!cov.empty()) {
            auto* cover = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
            cover->setDimensions(202, 202);
            cover->setCornerRadius(8.0f);
            cover->setScalingType(brls::ImageScalingType::FILL);
            cover->setImageUrl(cov);
            art->addView(cover);
        } else {
            art->addView(omnidesk::ui::makePill("ALAC 无损舞台", "desktop/surface", "brls/accent"));
        }
        left_card->addView(art);
    }

    auto* title_lbl = omnidesk::ui::makeLabel(track.title, 18.0f, "brls/text");
    title_lbl->setMarginTop(12);
    left_card->addView(title_lbl);

    auto* sub_lbl = omnidesk::ui::makeLabel(
        track.artist + (track.album.empty() ? "" : (" · " + track.album)), 13.5f, "font/grey");
    sub_lbl->setMarginTop(4);
    left_card->addView(sub_lbl);

    std::string q_text = sess.ok ? sess.formattedQuality()
                                 : (!sess.error.empty() ? ("播放错误: " + sess.error)
                                                        : (track.quality_label.empty() ? "准备无损流…" : track.quality_label));
    auto* q_pill = omnidesk::ui::makePill(
        q_text,
        sess.error.empty() ? "status/ok_bg" : "status/warn_bg",
        sess.error.empty() ? "status/ok_fg" : "status/warn_fg");
    q_pill->setMarginTop(8);
    left_card->addView(q_pill);

    auto mpv_st = omnidesk::MPVCore::instance().state();
    auto* pos_lbl = omnidesk::ui::makeLabel(
        formatDuration(mpv_st.position_sec) + " / " +
            formatDuration(mpv_st.duration_sec > 0.0 ? mpv_st.duration_sec : track.duration_sec),
        13.0f, "font/grey");
    pos_lbl->setMarginTop(8);
    left_card->addView(pos_lbl);

    auto* controls = new brls::Box(brls::Axis::ROW);
    controls->setMarginTop(10);
    controls->addView(omnidesk::ui::makeButton("上一曲", [this]() {
        engine_->playAdjacentTrack(-1);
    }, false, 34.0f));

    auto* pp = omnidesk::ui::makeButton(mpv_st.paused ? "播放" : "暂停", [this]() {
        engine_->togglePlayPause();
    }, true, 34.0f);
    pp->setMarginLeft(8);
    controls->addView(pp);

    auto* nxt = omnidesk::ui::makeButton("下一曲", [this]() {
        engine_->playAdjacentTrack(1);
    }, false, 34.0f);
    nxt->setMarginLeft(8);
    controls->addView(nxt);
    left_card->addView(controls);

    auto* seek_row = new brls::Box(brls::Axis::ROW);
    seek_row->setMarginTop(8);
    seek_row->addView(omnidesk::ui::makeButton("-10s", []() {
        omnidesk::MPVCore::instance().seekRelative(-10.0);
    }, false, 30.0f));
    auto* fwd = omnidesk::ui::makeButton("+10s", []() {
        omnidesk::MPVCore::instance().seekRelative(10.0);
    }, false, 30.0f);
    fwd->setMarginLeft(6);
    seek_row->addView(fwd);
    auto* ll = omnidesk::ui::makeButton(
        engine_->currentConfig().lossless_enabled ? "ALAC: 开" : "ALAC: 关",
        [this]() {
            engine_->setLosslessEnabled(!engine_->currentConfig().lossless_enabled);
        }, false, 30.0f);
    ll->setMarginLeft(6);
    seek_row->addView(ll);
    left_card->addView(seek_row);

    auto* exp_row = new brls::Box(brls::Axis::ROW);
    exp_row->setMarginTop(8);
    exp_row->addView(omnidesk::ui::makeButton("⬇ 导出 ALAC (.m4a)", [this, track]() {
        triggerTrackExport(track, false);
    }, false, 30.0f));
    auto* flac_b = omnidesk::ui::makeButton("⬇ 导出 FLAC", [this, track]() {
        triggerTrackExport(track, true);
    }, false, 30.0f);
    flac_b->setMarginLeft(8);
    exp_row->addView(flac_b);
    left_card->addView(exp_row);

    if (!queue.empty()) {
        auto* qh = omnidesk::ui::makeLabel("播放队列 (" + std::to_string(queue.size()) + ")", 14.0f, "brls/text");
        qh->setMarginTop(14);
        qh->setMarginBottom(6);
        left_card->addView(qh);
        size_t max_show = std::min<size_t>(queue.size(), 8);
        for (size_t i = 0; i < max_show; ++i) {
            bool cur = (i == qidx);
            auto* qrow = omnidesk::ui::makeCard(cur ? "desktop/active" : "desktop/soft", 8.0f, 6.0f);
            qrow->setAxis(brls::Axis::ROW);
            qrow->setAlignItems(brls::AlignItems::CENTER);
            qrow->setMarginBottom(4);
            qrow->setFocusable(true);
            if (!queue[i].cover_url.empty()) {
                auto* qiv = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
                qiv->setDimensions(30, 30);
                qiv->setShrink(0.0f);
                qiv->setCornerRadius(5.0f);
                qiv->setScalingType(brls::ImageScalingType::FILL);
                qiv->setMarginRight(8);
                qiv->setImageUrl(queue[i].cover_url);
                qrow->addView(qiv);
            }
            qrow->addView(omnidesk::ui::makeLabel(
                (cur ? "▶ " : "") + clipText(queue[i].title, 34), 13.0f,
                cur ? "brls/accent" : "brls/text"));
            qrow->registerClickAction([this, queue, i](brls::View*) {
                engine_->setQueueAndPlay(queue, i);
                return true;
            });
            left_card->addView(qrow);
        }
    }
    split->addView(left_card);

    // Right: Synced Bilingual Lyrics + AI Language Explanation
    auto* right_card = omnidesk::ui::makeCard("desktop/surface", 16.0f, 12.0f);
    right_card->setWidth(0);
    right_card->setGrow(1.0f);
    right_card->setShrink(1.0f);

    auto* lhdr = new brls::Box(brls::Axis::ROW);
    lhdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    lhdr->setAlignItems(brls::AlignItems::CENTER);
    lhdr->addView(omnidesk::ui::makeLabel("双语同步歌词 · Island LRC Studio", 17.5f, "brls/text"));

    auto* mode_row = new brls::Box(brls::Axis::ROW);
    for (const auto& [m, lbl] : std::vector<std::pair<std::string, std::string>>{
             {"bilingual", "双语"}, {"original", "原文"}, {"translation", "译文"}}) {
        auto* mb = omnidesk::ui::makeButton(lbl, [this, m]() {
            engine_->setDisplayMode(m);
            renderNowPlayingBody();
        }, disp_mode == m, 28.0f);
        mb->setMarginLeft(6);
        mode_row->addView(mb);
    }
    lhdr->addView(mode_row);
    right_card->addView(lhdr);

    auto* act_row = new brls::Box(brls::Axis::ROW);
    act_row->setMarginTop(8);
    act_row->setMarginBottom(6);
    act_row->addView(omnidesk::ui::makeButton("翻译中文", [this]() {
        engine_->triggerAsyncLyricsRefresh(false, true);
    }, true, 30.0f));
    auto* retry_b = omnidesk::ui::makeButton("重查歌词", [this]() {
        engine_->triggerAsyncLyricsRefresh(true, false);
    }, false, 30.0f);
    retry_b->setMarginLeft(8);
    act_row->addView(retry_b);
    auto* imp_b = omnidesk::ui::makeButton("导入 LRC", [this]() {
        promptImportLyrics();
    }, false, 30.0f);
    imp_b->setMarginLeft(8);
    act_row->addView(imp_b);
    auto* exp_lrc_b = omnidesk::ui::makeButton("导出双语 LRC", [this, track]() {
        std::string err;
        std::string lrc = engine_->exportActiveLrc("bilingual", err);
        if (lrc.empty()) {
            brls::Application::notify(err.empty() ? "当前无歌词可导出" : err);
            return;
        }
        const char* home = std::getenv("HOME");
        fs::path out_dir = fs::path(home && *home ? home : "/tmp") / "Music" / "AppleMusic";
        std::error_code ec;
        fs::create_directories(out_dir, ec);
        fs::path out_file = out_dir / ((track.title.empty() ? track.id : track.title) + ".bilingual.lrc");
        std::ofstream ofs(out_file, std::ios::trunc);
        if (ofs.is_open()) {
            ofs << lrc;
            brls::Application::notify("双语 LRC 已导出至: " + out_file.string());
        }
    }, false, 30.0f);
    exp_lrc_b->setMarginLeft(8);
    act_row->addView(exp_lrc_b);
    right_card->addView(act_row);

    std::string st_txt = lyr.status_text;
    if (!lyr.error.empty()) st_txt += (st_txt.empty() ? "" : " · ") + lyr.error;
    if (!st_txt.empty()) {
        auto* sl = omnidesk::ui::makeLabel(st_txt, 12.5f, "font/grey");
        sl->setMarginBottom(6);
        right_card->addView(sl);
    }

    if (lyr.lines.empty()) {
        right_card->addView(omnidesk::ui::makeLabel("当前暂无歌词，可点击「重查歌词」或「导入 LRC」。", 14.0f, "font/grey"));
    } else {
        const double pos_sec = mpv_st.position_sec;
        int init_idx = -1;
        for (size_t i = 0; i < lyr.lines.size(); ++i) {
            if (lyr.lines[i].has_time && pos_sec >= lyr.lines[i].time_sec) init_idx = static_cast<int>(i);
        }
        active_lyric_idx_ = init_idx;

        for (size_t i = 0; i < lyr.lines.size(); ++i) {
            const auto& line = lyr.lines[i];
            bool active_line = (static_cast<int>(i) == init_idx);
            auto* lcard = omnidesk::ui::makeCard(active_line ? "desktop/active" : "desktop/soft", 12.0f, 10.0f);
            lcard->setMarginTop(8);
            lcard->setFocusable(true);

            auto* top_row = new brls::Box(brls::Axis::ROW);
            top_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            top_row->setAlignItems(brls::AlignItems::CENTER);

            std::string main_txt = line.original;
            if (disp_mode == "translation" && !line.translation.empty()) main_txt = line.translation;
            std::string stamp = line.has_time ? (formatDuration(line.time_sec) + "  ") : "";
            auto* orig_lbl = omnidesk::ui::makeLabel(
                stamp + main_txt, 16.0f, active_line ? "brls/accent" : "brls/text");
            orig_lbl->setGrow(1.0f);
            top_row->addView(orig_lbl);

            auto* exp_btn = omnidesk::ui::makeButton("精讲", [this, i]() {
                explainLyricAtIndex(i);
            }, false, 28.0f);
            exp_btn->setShrink(0.0f);
            exp_btn->setMarginLeft(8);
            top_row->addView(exp_btn);
            lcard->addView(top_row);

            if (disp_mode == "bilingual" && !line.translation.empty()) {
                auto* tr_lbl = omnidesk::ui::makeLabel(line.translation, 14.0f, "font/grey");
                tr_lbl->setMarginTop(4);
                lcard->addView(tr_lbl);
            }

            if (line.has_time) {
                double seek_t = line.time_sec;
                lcard->registerClickAction([this, seek_t](brls::View*) {
                    engine_->seekSeconds(seek_t);
                    return true;
                });
            }

            lyric_cards_.push_back(lcard);
            lyric_orig_labels_.push_back(orig_lbl);
            lyric_times_.push_back(line.has_time ? line.time_sec : -1.0);
            right_card->addView(lcard);
        }
    }

    if (has_explanation_) {
        auto* exp_card = omnidesk::ui::makeCard("desktop/soft", 12.0f, 8.0f);
        exp_card->setMarginTop(14);
        exp_card->addView(omnidesk::ui::makeLabel(
            "AI 逐句精讲 (" + last_explanation_.provider + ")", 14.5f, "brls/accent"));
        auto* body = omnidesk::ui::makeLabel(last_explanation_.explanation, 13.5f, "brls/text", 1.45f);
        body->setMarginTop(6);
        exp_card->addView(body);
        right_card->addView(exp_card);
    }

    split->addView(right_card);
    stage_box_->addView(split);
    syncStageLayout();
}

void MusicWorkspaceView::updateActiveLyricHighlight(double position_sec) {
    if (lyric_cards_.empty() || lyric_cards_.size() != lyric_times_.size() ||
        lyric_orig_labels_.size() != lyric_times_.size()) {
        return;
    }
    int idx = -1;
    for (size_t i = 0; i < lyric_times_.size(); ++i) {
        if (lyric_times_[i] >= 0.0 && position_sec >= lyric_times_[i]) idx = static_cast<int>(i);
    }
    if (idx == active_lyric_idx_) return;

    if (active_lyric_idx_ >= 0 && static_cast<size_t>(active_lyric_idx_) < lyric_cards_.size()) {
        lyric_cards_[active_lyric_idx_]->setBackgroundColor(omnidesk::ui::color("desktop/soft"));
        lyric_orig_labels_[active_lyric_idx_]->setTextColor(omnidesk::ui::color("brls/text"));
    }
    active_lyric_idx_ = idx;
    if (active_lyric_idx_ >= 0 && static_cast<size_t>(active_lyric_idx_) < lyric_cards_.size()) {
        lyric_cards_[active_lyric_idx_]->setBackgroundColor(omnidesk::ui::color("desktop/active"));
        lyric_orig_labels_[active_lyric_idx_]->setTextColor(omnidesk::ui::color("brls/accent"));
    }
}

void MusicWorkspaceView::explainLyricAtIndex(size_t index, const std::string& focus_phrase) {
    if (!engine_) return;
    std::weak_ptr<std::atomic<bool>> alive_w = alive_;
    std::thread([this, eng = engine_, alive_w, index, focus_phrase]() {
        PhraseExplanation note = eng->explainLine(index, focus_phrase);
        brls::sync([this, alive_w, note]() {
            auto a = alive_w.lock();
            if (!a || !a->load(std::memory_order_acquire)) return;
            last_explanation_ = note;
            has_explanation_ = true;
            renderNowPlayingBody();
        });
    }).detach();
}

void MusicWorkspaceView::promptAiWriteback() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string prompt) {
            if (prompt.empty()) return;
            brls::sync([this, prompt]() { triggerInteractiveWriteback(prompt); });
        },
        "AI 音乐策展与歌单反写",
        "描述你想听的场景、情绪或艺人（可重复推荐，自动预检中国区曲库并写回 OmniDesk 与灵感歌单）",
        256,
        ai_prompt_draft_);
}

void MusicWorkspaceView::promptTargetPlaylistName() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string name) {
            if (name.empty()) return;
            brls::sync([this, name]() {
                ai_target_playlist_ = name;
                renderActiveTab();
            });
        },
        "设置交互反写目标歌单名",
        "默认写入「OmniDesk AI 灵感电台」（严禁填写 ChatGPT Daily 5）",
        96,
        ai_target_playlist_);
}

void MusicWorkspaceView::promptSelectArchiveDate() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string date) {
            if (date.empty()) return;
            brls::sync([this, date]() { triggerCurationLoad(date, false); });
        },
        "跳转至归档日期 (YYYY-MM-DD)",
        "输入历史精选日期，例如 2026-11-16",
        16,
        curation_snap_.selected_date);
}

void MusicWorkspaceView::promptAppleIdLogin() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string email) {
            if (email.empty()) return;
            pending_email_ = email;
            brls::sync([this, email]() {
                auto* plat2 = brls::Application::getPlatform();
                if (!plat2) return;
                plat2->getImeManager()->openForText(
                    [this, email](std::string password) {
                        if (password.empty()) return;
                        std::weak_ptr<std::atomic<bool>> alive_w = alive_;
                        std::thread([this, eng = engine_, alive_w, email, password]() {
                            DrmStatus st;
                            std::string err;
                            bool ok = eng->authenticateAppleId(email, password, st, err);
                            brls::sync([this, alive_w, ok, err]() {
                                auto a = alive_w.lock();
                                if (!a || !a->load(std::memory_order_acquire)) return;
                                refreshHeader();
                                brls::Application::notify(ok ? "已提交 Apple ID 登录，若弹出受信任设备请填写 2FA 验证码"
                                                             : ("登录失败: " + err));
                            });
                        }).detach();
                    },
                    "输入 Apple ID 密码",
                    "仅提交至本机 127.0.0.1 Adam FairPlay 引擎，不经过任何第三方服务器",
                    128,
                    "");
            });
        },
        "输入 Apple ID 邮箱",
        "用于本机 FairPlay ALAC 无损解密认证",
        128,
        pending_email_);
}

void MusicWorkspaceView::promptTwoFactorCode() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string code) {
            if (code.empty()) return;
            std::weak_ptr<std::atomic<bool>> alive_w = alive_;
            std::thread([this, eng = engine_, alive_w, code]() {
                DrmStatus st;
                std::string err;
                bool ok = eng->submitTwoFactorCode(code, st, err);
                brls::sync([this, alive_w, ok, err]() {
                    auto a = alive_w.lock();
                    if (!a || !a->load(std::memory_order_acquire)) return;
                    refreshHeader();
                    brls::Application::notify(ok ? "2FA 验证码已提交，正在初始化 FairPlay 会话"
                                                 : ("2FA 验证失败: " + err));
                });
            }).detach();
        },
        "输入 6 位 Apple ID 验证码",
        "受信任设备或短信收到的 6 位验证码",
        16,
        "");
}

void MusicWorkspaceView::promptSearch() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string q) {
            if (q.empty()) return;
            brls::sync([this, q]() {
                search_query_ = q;
                active_tab_ = SubTab::Search;
                showing_container_detail_ = false;
                refreshHeader();
                triggerFeedLoad("search", q, true);
            });
        },
        "搜索 Apple Music 全站曲库",
        "输入歌曲、专辑、歌单或艺人名称",
        128,
        search_query_);
}

void MusicWorkspaceView::promptImportLyrics() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string raw) {
            if (raw.empty()) return;
            std::string err;
            engine_->importLyricsForActiveOrTrack(nullptr, raw, err);
            brls::sync([err]() {
                brls::Application::notify(err.empty() ? "已导入歌词并保存至本地缓存" : err);
            });
        },
        "导入 LRC / 纯文本歌词",
        "粘贴 UTF-8 LRC（支持双语同时间戳合并）或纯文本歌词",
        8192,
        "");
}

void MusicWorkspaceView::promptMediaUserToken() {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;
    plat->getImeManager()->openForText(
        [this](std::string mut) {
            if (mut.empty()) return;
            std::string err;
            bool ok = engine_->importMediaUserToken(mut, err);
            brls::sync([this, ok, err]() {
                refreshHeader();
                brls::Application::notify(ok ? "media-user-token 已保存至 0600 SecretVault 并推送引擎" : err);
            });
        },
        "填入 Apple Music media-user-token",
        "用于与网页端同步个人资料库、个性化推荐与云端歌单反写",
        2048,
        "");
}

// ---- MusicCompanionDockView (F8) ----

MusicCompanionDockView::MusicCompanionDockView(std::shared_ptr<MusicEngine> engine)
    : brls::Box(brls::Axis::COLUMN), engine_(std::move(engine)) {
    setPadding(12);
    setCornerRadius(10.0f);
    setBackgroundColor(omnidesk::ui::color("desktop/soft"));
    setMarginBottom(8);

    auto* hdr_row = new brls::Box(brls::Axis::ROW);
    hdr_row->setAlignItems(brls::AlignItems::CENTER);
    cover_view_ = new omnidesk::CachedImageView(&omnidesk::ImageCache::instance());
    cover_view_->setDimensions(42, 42);
    cover_view_->setCornerRadius(6.0f);
    cover_view_->setScalingType(brls::ImageScalingType::FILL);
    cover_view_->setMarginRight(10);
    cover_view_->setVisibility(brls::Visibility::GONE);
    hdr_row->addView(cover_view_);

    auto* hdr_meta = new brls::Box(brls::Axis::COLUMN);
    hdr_meta->setGrow(1.0f);
    hdr_meta->addView(omnidesk::ui::makeLabel("Apple Music 双语精讲", 15.0f, "brls/accent"));
    track_label_ = omnidesk::ui::makeLabel("尚未播放曲目", 14.0f, "brls/text");
    track_label_->setMarginTop(4);
    hdr_meta->addView(track_label_);
    hdr_row->addView(hdr_meta);
    addView(hdr_row);

    lyric_orig_label_ = omnidesk::ui::makeLabel("等待歌词同步…", 14.0f, "brls/text", 1.38f);
    lyric_orig_label_->setMarginTop(6);
    addView(lyric_orig_label_);

    lyric_trans_label_ = omnidesk::ui::makeLabel("", 13.5f, "font/grey", 1.38f);
    lyric_trans_label_->setMarginTop(2);
    addView(lyric_trans_label_);

    auto* btns = new brls::Box(brls::Axis::ROW);
    btns->setMarginTop(8);
    auto* exp_b = omnidesk::ui::makeButton("精讲当前句", [this]() {
        if (!engine_) return;
        std::weak_ptr<std::atomic<bool>> alive_w = alive_;
        std::thread([this, eng = engine_, alive_w]() {
            MusicTrack tr; PlaybackSessionInfo s; LyricBundle lyr; std::vector<MusicTrack> q; size_t qi = 0;
            eng->getActivePlaybackState(tr, s, lyr, q, qi);
            double pos = omnidesk::MPVCore::instance().state().position_sec;
            size_t idx = 0;
            for (size_t i = 0; i < lyr.lines.size(); ++i) {
                if (lyr.lines[i].has_time && pos >= lyr.lines[i].time_sec) idx = i;
            }
            PhraseExplanation note = eng->explainLine(idx);
            brls::sync([this, alive_w, note]() {
                auto a = alive_w.lock();
                if (!a || !a->load(std::memory_order_acquire)) return;
                if (note_label_) note_label_->setText(clipText(note.explanation, 360));
            });
        }).detach();
    }, true, 28.0f);
    exp_b->setMarginRight(6);
    btns->addView(exp_b);

    btns->addView(omnidesk::ui::makeButton("翻译缺失歌词", [this]() {
        if (engine_) engine_->triggerAsyncLyricsRefresh(false, true);
    }, false, 28.0f));
    addView(btns);

    note_label_ = omnidesk::ui::makeLabel("", 12.0f, "font/grey", 1.4f);
    note_label_->setMarginTop(6);
    addView(note_label_);

    if (engine_) {
        std::weak_ptr<std::atomic<bool>> alive_w = alive_;
        engine_sub_ = engine_->subscribeState([this, alive_w]() {
            brls::sync([this, alive_w]() {
                auto a = alive_w.lock();
                if (!a || !a->load(std::memory_order_acquire)) return;
                refreshSummary();
            });
        });
        mpv_sub_ = omnidesk::MPVCore::instance().subscribeState(
            [this, alive_w](const omnidesk::MpvPlaybackState&) {
                brls::sync([this, alive_w]() {
                    auto a = alive_w.lock();
                    if (!a || !a->load(std::memory_order_acquire)) return;
                    refreshSummary();
                });
            });
        refreshSummary();
    }
}

MusicCompanionDockView::~MusicCompanionDockView() {
    alive_->store(false, std::memory_order_release);
    if (engine_ && engine_sub_ != 0) {
        engine_->unsubscribeState(engine_sub_);
        engine_sub_ = 0;
    }
    if (mpv_sub_ != 0) {
        omnidesk::MPVCore::instance().unsubscribeState(mpv_sub_);
        mpv_sub_ = 0;
    }
}

void MusicCompanionDockView::refreshSummary() {
    if (!engine_) return;
    MusicTrack tr; PlaybackSessionInfo s; LyricBundle lyr; std::vector<MusicTrack> q; size_t qi = 0;
    if (!engine_->getActivePlaybackState(tr, s, lyr, q, qi)) {
        if (cover_view_) cover_view_->setVisibility(brls::Visibility::GONE);
        if (track_label_) track_label_->setText("尚未播放曲目");
        return;
    }
    const std::string cov = !tr.cover_url.empty() ? tr.cover_url : s.cover_url;
    if (cover_view_) {
        if (!cov.empty()) {
            cover_view_->setVisibility(brls::Visibility::VISIBLE);
            cover_view_->setImageUrl(cov);
        } else {
            cover_view_->setVisibility(brls::Visibility::GONE);
        }
    }
    if (track_label_) {
        track_label_->setText(tr.title + " · " + (s.ok ? s.formattedQuality() : tr.artist));
    }
    if (lyr.lines.empty()) {
        if (lyric_orig_label_) lyric_orig_label_->setText(lyr.status_text.empty() ? "暂无歌词" : lyr.status_text);
        if (lyric_trans_label_) lyric_trans_label_->setText("");
        return;
    }
    double pos = omnidesk::MPVCore::instance().state().position_sec;
    int idx = -1;
    for (size_t i = 0; i < lyr.lines.size(); ++i) {
        if (lyr.lines[i].has_time && pos >= lyr.lines[i].time_sec) idx = static_cast<int>(i);
    }
    if (idx < 0) idx = 0;
    const auto& line = lyr.lines[static_cast<size_t>(idx)];
    if (lyric_orig_label_) lyric_orig_label_->setText(line.original);
    if (lyric_trans_label_) lyric_trans_label_->setText(line.translation);
}

} // namespace music_plugin
