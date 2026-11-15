#include "omnidesk/host/curation_calendar_dialog.hpp"

#include <algorithm>
#include <atomic>
#include <borealis.hpp>
#include <chrono>
#include <ctime>
#include <iomanip>
#include <map>
#include <memory>
#include <sstream>
#include <vector>
#include "omnidesk/async.hpp"
#include "omnidesk/host/curation_scheduler_dialog.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/types.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {

namespace {

struct DateCountStats {
    int music = 0;
    int youtube = 0;
    int news = 0;
    int reddit = 0;
    int journal = 0;
    int english = 0;
    int total = 0;

    int countForDomain(const std::string& domain) const {
        if (domain.empty() || domain == "all") return total;
        if (domain == "music") return music;
        if (domain == "youtube" || domain == "video") return youtube;
        if (domain == "news") return news;
        if (domain == "reddit") return reddit;
        if (domain == "journal") return journal;
        if (domain == "english") return english;
        return total;
    }
};

std::string currentShanghaiDateStr() {
    using namespace std::chrono;
    const auto now = system_clock::now() + hours(8);
    const std::time_t tt = system_clock::to_time_t(now);
    std::tm tm_val{};
    gmtime_r(&tt, &tm_val);
    char buf[16]{};
    std::strftime(buf, sizeof(buf), "%Y-%m-%d", &tm_val);
    return std::string(buf);
}

bool parseIsoYmd(const std::string& s, int& out_y, int& out_m, int& out_d) {
    if (s.size() != 10 || s[4] != '-' || s[7] != '-') return false;
    try {
        out_y = std::stoi(s.substr(0, 4));
        out_m = std::stoi(s.substr(5, 2));
        out_d = std::stoi(s.substr(8, 2));
        return out_y >= 1970 && out_m >= 1 && out_m <= 12 && out_d >= 1 && out_d <= 31;
    } catch (...) {
        return false;
    }
}

std::string formatIsoYmd(int y, int m, int d) {
    std::ostringstream oss;
    oss << std::setfill('0') << std::setw(4) << y << "-"
        << std::setw(2) << m << "-" << std::setw(2) << d;
    return oss.str();
}

int daysInMonth(int y, int m) {
    static const int kDays[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (m < 1 || m > 12) return 30;
    if (m == 2) {
        const bool leap = (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0));
        return leap ? 29 : 28;
    }
    return kDays[m - 1];
}

// 0 = Monday .. 6 = Sunday
int weekdayMondayZero(int y, int m, int d) {
    y -= (m <= 2) ? 1 : 0;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = (153u * static_cast<unsigned>(m + (m > 2 ? -3 : 9)) + 2u) / 5u +
                         static_cast<unsigned>(d) - 1u;
    const unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    const int z = era * 146097 + static_cast<int>(doe) - 719468;
    return ((z + 3) % 7 + 7) % 7;
}

std::string domainBadgeLabel(const std::string& cat) {
    if (cat == "music") return "音乐";
    if (cat == "video" || cat == "youtube") return "YouTube";
    if (cat == "news") return "早报";
    if (cat == "reddit") return "Reddit";
    if (cat == "journal") return "科学期刊";
    if (cat == "english") return "英语伴读";
    return cat;
}

const char* domainBadgeColor(const std::string& cat) {
    if (cat == "music" || cat == "video" || cat == "youtube") return "domain/media";
    if (cat == "english") return "domain/book";
    return "domain/text";
}

class CalendarDialogBox final : public brls::Box {
public:
    explicit CalendarDialogBox(CalendarDialogOptions options)
        : brls::Box(brls::Axis::COLUMN),
          options_(std::move(options)),
          today_date_(currentShanghaiDateStr()),
          active_domain_(options_.initial_domain.empty() ? "all" : options_.initial_domain) {
        setPadding(18, 20, 16, 20);
        setWidth(980);

        int y = 2026, m = 11, d = 1;
        if (!parseIsoYmd(options_.initial_date, y, m, d)) {
            parseIsoYmd(today_date_, y, m, d);
            selected_date_ = today_date_;
        } else {
            selected_date_ = options_.initial_date;
        }
        view_year_ = y;
        view_month_ = m;

        // Top Header
        auto* hdr = new brls::Box(brls::Axis::ROW);
        hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        hdr->setAlignItems(brls::AlignItems::CENTER);
        hdr->setMarginBottom(10);
        hdr->addView(ui::makeLabel("📅 推荐日历与跨三域每日清单浏览器", 19.5f, "brls/text"));
        archive_summary_label_ = ui::makeLabel("正在加载推荐日期索引…", 13.0f, "font/grey");
        hdr->addView(archive_summary_label_);
        addView(hdr);

        // Domain filter bar
        domain_bar_ = new brls::Box(brls::Axis::ROW);
        domain_bar_->setMarginBottom(10);
        rebuildDomainPills();
        addView(domain_bar_);

        // Two-pane body
        auto* split = new brls::Box(brls::Axis::ROW);
        split->setHeight(430);
        split->setWidthPercentage(100);

        // Left pane: calendar month grid + actions
        auto* left_pane = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        left_pane->setWidth(430);
        left_pane->setMarginRight(14);
        left_pane->setHeightPercentage(100);

        // Month nav row
        auto* month_nav = new brls::Box(brls::Axis::ROW);
        month_nav->setAlignItems(brls::AlignItems::CENTER);
        month_nav->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        month_nav->setMarginBottom(8);

        auto* prev_btn = ui::makeButton("◀ 上月", [this]() { shiftMonth(-1); }, false, 30.0f);
        month_title_label_ = ui::makeLabel("", 16.0f, "brls/text");
        auto* next_btn = ui::makeButton("下月 ▶", [this]() { shiftMonth(1); }, false, 30.0f);
        auto* today_btn = ui::makeButton("今天", [this]() { jumpToDate(today_date_); }, false, 30.0f);
        auto* latest_btn = ui::makeButton("最新", [this]() {
            if (!latest_date_with_data_.empty()) jumpToDate(latest_date_with_data_);
        }, false, 30.0f);

        month_nav->addView(prev_btn);
        month_nav->addView(month_title_label_);
        month_nav->addView(next_btn);
        month_nav->addView(today_btn);
        month_nav->addView(latest_btn);
        left_pane->addView(month_nav);

        // Weekday header
        auto* wd_row = new brls::Box(brls::Axis::ROW);
        wd_row->setMarginBottom(4);
        static const char* kWd[] = {"一", "二", "三", "四", "五", "六", "日"};
        for (const char* w : kWd) {
            auto* cell = new brls::Box(brls::Axis::COLUMN);
            cell->setGrow(1.0f);
            cell->setAlignItems(brls::AlignItems::CENTER);
            cell->addView(ui::makeLabel(w, 12.5f, "font/grey"));
            wd_row->addView(cell);
        }
        left_pane->addView(wd_row);

        calendar_grid_box_ = new brls::Box(brls::Axis::COLUMN);
        calendar_grid_box_->setGrow(1.0f);
        left_pane->addView(calendar_grid_box_);

        // Footer actions under calendar
        auto* left_footer = new brls::Box(brls::Axis::ROW);
        left_footer->setMarginTop(8);
        left_footer->setAlignItems(brls::AlignItems::CENTER);
        apply_filter_btn_ = ui::makeButton(
            "按选中日期筛选工作台",
            [this]() {
                auto cb = options_.on_apply_date_filter;
                std::string d = selected_date_;
                if (dialog_) dialog_->dismiss();
                if (cb && !d.empty()) brls::sync([cb, d]() { cb(d); });
            },
            true, 34.0f);
        apply_filter_btn_->setGrow(1.0f);
        apply_filter_btn_->setMarginRight(6);
        left_footer->addView(apply_filter_btn_);

        if (options_.on_clear_date_filter) {
            auto* clr_btn = ui::makeButton(
                "清除筛选",
                [this]() {
                    auto cb = options_.on_clear_date_filter;
                    if (dialog_) dialog_->dismiss();
                    if (cb) brls::sync([cb]() { cb(); });
                },
                false, 34.0f);
            clr_btn->setMarginRight(6);
            left_footer->addView(clr_btn);
        }

        auto* sched_btn = ui::makeButton(
            "定时开关",
            [this]() {
                auto cb = options_.on_open_scheduler;
                if (dialog_) dialog_->dismiss();
                if (cb) {
                    brls::sync([cb]() { cb(); });
                } else {
                    brls::sync([]() { CurationSchedulerDialog::open(); });
                }
            },
            false, 34.0f);
        left_footer->addView(sched_btn);

        left_pane->addView(left_footer);
        split->addView(left_pane);

        // Right pane: selected date bundle preview + click-to-open
        auto* right_pane = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        right_pane->setGrow(1.0f);
        right_pane->setHeightPercentage(100);

        auto* r_hdr = new brls::Box(brls::Axis::ROW);
        r_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        r_hdr->setAlignItems(brls::AlignItems::CENTER);
        r_hdr->setMarginBottom(6);
        selected_date_title_ = ui::makeLabel("选中日期：" + selected_date_, 16.5f, "brls/text");
        r_hdr->addView(selected_date_title_);
        r_hdr->addView(ui::makeButton("同步双仓归档", [this]() { triggerRehydrate(); }, false, 28.0f));
        right_pane->addView(r_hdr);

        selected_date_stats_label_ = ui::makeLabel("点击日历格子查看当天全部推荐…", 13.0f, "font/grey");
        selected_date_stats_label_->setMarginBottom(8);
        right_pane->addView(selected_date_stats_label_);

        today_banner_box_ = new brls::Box(brls::Axis::ROW);
        today_banner_box_->setAlignItems(brls::AlignItems::CENTER);
        today_banner_box_->setMarginBottom(6);
        today_banner_box_->setVisibility(brls::Visibility::GONE);
        right_pane->addView(today_banner_box_);

        auto* right_scroll = new brls::ScrollingFrame();
        right_scroll->setWidthPercentage(100);
        right_scroll->setGrow(1.0f);
        bundle_items_box_ = new brls::Box(brls::Axis::COLUMN);
        bundle_items_box_->setWidthPercentage(100);
        right_scroll->setContentView(bundle_items_box_);
        right_pane->addView(right_scroll);

        split->addView(right_pane);
        addView(split);

        rebuildCalendarGrid();
        loadArchiveDates();
        loadSelectedDateBundle();
    }

    ~CalendarDialogBox() override {
        alive_->store(false, std::memory_order_release);
        if (dates_req_) dates_req_->cancel();
        if (bundle_req_) bundle_req_->cancel();
    }

    void setDialog(brls::Dialog* dlg) { dialog_ = dlg; }

private:
    void rebuildDomainPills() {
        if (!domain_bar_) return;
        domain_bar_->clearViews();
        const struct {
            const char* id;
            const char* label;
        } kDomains[] = {
            {"all", "全部领域"},
            {"music", "音乐"},
            {"youtube", "YouTube"},
            {"news", "早报"},
            {"reddit", "Reddit"},
            {"journal", "科学期刊"},
            {"english", "英语伴读"},
        };
        for (const auto& dom : kDomains) {
            const std::string id = dom.id;
            auto* btn = ui::makeButton(
                dom.label,
                [this, id]() {
                    active_domain_ = id;
                    rebuildDomainPills();
                    rebuildCalendarGrid();
                    renderSelectedBundleItems();
                },
                active_domain_ == id, 30.0f);
            btn->setMarginRight(6);
            domain_bar_->addView(btn);
        }
    }

    void shiftMonth(int delta) {
        view_month_ += delta;
        if (view_month_ < 1) {
            view_month_ = 12;
            view_year_ -= 1;
        } else if (view_month_ > 12) {
            view_month_ = 1;
            view_year_ += 1;
        }
        rebuildCalendarGrid();
    }

    void jumpToDate(const std::string& date) {
        int y = 0, m = 0, d = 0;
        if (!parseIsoYmd(date, y, m, d)) return;
        selected_date_ = date;
        view_year_ = y;
        view_month_ = m;
        rebuildCalendarGrid();
        loadSelectedDateBundle();
    }

    void rebuildCalendarGrid() {
        if (!calendar_grid_box_) return;
        calendar_grid_box_->clearViews();

        if (month_title_label_) {
            month_title_label_->setText(std::to_string(view_year_) + " 年 " +
                                        (view_month_ < 10 ? "0" : "") + std::to_string(view_month_) + " 月");
        }
        if (apply_filter_btn_) {
            apply_filter_btn_->setText("按 " + selected_date_ + " 筛选工作台");
        }

        const int first_wd = weekdayMondayZero(view_year_, view_month_, 1);
        const int num_days = daysInMonth(view_year_, view_month_);

        int day = 1;
        for (int row = 0; row < 6; ++row) {
            auto* row_box = new brls::Box(brls::Axis::ROW);
            row_box->setGrow(1.0f);
            row_box->setMarginBottom(4);

            for (int col = 0; col < 7; ++col) {
                const int cell_idx = row * 7 + col;
                if (cell_idx < first_wd || day > num_days) {
                    auto* spacer = new brls::Box(brls::Axis::COLUMN);
                    spacer->setGrow(1.0f);
                    spacer->setMarginRight(col < 6 ? 4 : 0);
                    row_box->addView(spacer);
                    continue;
                }

                const int cur_day = day++;
                const std::string iso = formatIsoYmd(view_year_, view_month_, cur_day);
                const bool is_selected = (iso == selected_date_);
                const bool is_today = (iso == today_date_);

                int cnt = 0;
                auto dit = date_stats_.find(iso);
                if (dit != date_stats_.end()) {
                    cnt = dit->second.countForDomain(active_domain_);
                }

                auto* cell = new brls::Box(brls::Axis::COLUMN);
                cell->setGrow(1.0f);
                cell->setMarginRight(col < 6 ? 4 : 0);
                cell->setPadding(4, 3, 4, 3);
                cell->setCornerRadius(6.0f);
                cell->setAlignItems(brls::AlignItems::CENTER);
                cell->setJustifyContent(brls::JustifyContent::CENTER);
                cell->setFocusable(true);

                if (is_selected) {
                    cell->setBackgroundColor(ui::color("brls/accent"));
                } else if (is_today) {
                    cell->setBackgroundColor(ui::color("desktop/panel"));
                    cell->setBorderThickness(1.5f);
                    cell->setBorderColor(ui::color("brls/accent"));
                } else if (cnt > 0) {
                    cell->setBackgroundColor(ui::color("desktop/panel"));
                } else {
                    cell->setBackgroundColor(ui::color("brls/background"));
                }

                std::string top_txt = std::to_string(cur_day);
                if (is_today) top_txt += "今";
                const char* num_tok = is_selected ? "brls/text" : (cnt > 0 || is_today ? "brls/text" : "font/grey");
                cell->addView(ui::makeLabel(top_txt, 13.0f, num_tok));

                std::string sub_txt = (cnt > 0) ? (std::to_string(cnt) + "条") : "·";
                const char* sub_tok = is_selected ? "brls/text" : (cnt > 0 ? "domain/core" : "font/grey");
                cell->addView(ui::makeLabel(sub_txt, 10.5f, sub_tok));

                cell->registerClickAction([this, iso](brls::View*) {
                    selected_date_ = iso;
                    rebuildCalendarGrid();
                    loadSelectedDateBundle();
                    return true;
                });
                row_box->addView(cell);
            }
            calendar_grid_box_->addView(row_box);
        }
    }

    void loadArchiveDates() {
        auto& kernel = HostKernel::instance();
        if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) {
            if (archive_summary_label_) archive_summary_label_->setText("未安装 AI 策展插件");
            return;
        }
        if (dates_req_) dates_req_->cancel();
        dates_req_ = CancelToken::create();
        const auto alive = alive_;
        const auto tok = dates_req_;

        kernel.capabilities().callAsync(
            "recommend.feed.v1",
            {{"action", "list_archive_dates"}},
            [this, alive, tok](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire) || tok->isCancelled() || !res.ok) return;
                std::map<std::string, DateCountStats> parsed;
                std::string latest;
                if (res.data.contains("dates") && res.data["dates"].is_array()) {
                    for (const auto& entry : res.data["dates"]) {
                        if (!entry.is_object()) continue;
                        const std::string d = entry.value("date", "");
                        if (d.empty()) continue;
                        DateCountStats st;
                        st.music = entry.value("music", 0);
                        st.youtube = entry.value("youtube", 0);
                        st.news = entry.value("news", 0);
                        st.reddit = entry.value("reddit", 0);
                        st.journal = entry.value("journal", 0);
                        st.english = entry.value("english", 0);
                        st.total = entry.value("total", st.music + st.youtube + st.news + st.reddit + st.journal + st.english);
                        parsed[d] = st;
                        if (d > latest) latest = d;
                    }
                }
                const int total_dates = res.data.value("total_dates", static_cast<int>(parsed.size()));
                brls::sync([this, alive, tok, parsed = std::move(parsed), latest, total_dates]() mutable {
                    if (!alive->load(std::memory_order_acquire) || tok->isCancelled()) return;
                    date_stats_ = std::move(parsed);
                    latest_date_with_data_ = latest;
                    if (archive_summary_label_) {
                        archive_summary_label_->setText("已索引 " + std::to_string(total_dates) + " 天" +
                                                        (latest.empty() ? "" : (" · 最新 " + latest)));
                    }
                    if (options_.initial_date.empty() &&
                        date_stats_.find(selected_date_) == date_stats_.end() &&
                        !latest.empty()) {
                        jumpToDate(latest);
                    } else {
                        rebuildCalendarGrid();
                    }
                });
            },
            tok);
    }

    void loadSelectedDateBundle() {
        if (selected_date_title_) {
            selected_date_title_->setText("日期清单：" + selected_date_ +
                                          (selected_date_ == today_date_ ? " (今天)" : ""));
        }
        updateTodaySchedulerBanner();

        auto& kernel = HostKernel::instance();
        if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;

        if (bundle_req_) bundle_req_->cancel();
        bundle_req_ = CancelToken::create();
        const auto alive = alive_;
        const auto tok = bundle_req_;
        const std::string req_date = selected_date_;

        if (selected_date_stats_label_) selected_date_stats_label_->setText("正在加载 " + req_date + " 推荐内容…");

        kernel.capabilities().callAsync(
            "recommend.feed.v1",
            {{"action", "get_date_bundle"}, {"business_date", req_date}},
            [this, alive, tok, req_date](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire) || tok->isCancelled()) return;
                if (!res.ok) {
                    brls::sync([this, alive, tok, err = res.error]() {
                        if (!alive->load(std::memory_order_acquire) || tok->isCancelled()) return;
                        if (selected_date_stats_label_) selected_date_stats_label_->setText("加载失败: " + err);
                    });
                    return;
                }
                brls::sync([this, alive, tok, data = std::move(res.data)]() mutable {
                    if (!alive->load(std::memory_order_acquire) || tok->isCancelled()) return;
                    current_bundle_ = std::move(data);
                    renderSelectedBundleItems();
                });
            },
            tok);
    }

    void updateTodaySchedulerBanner() {
        if (!today_banner_box_) return;
        today_banner_box_->clearViews();
        if (selected_date_ != today_date_) {
            today_banner_box_->setVisibility(brls::Visibility::GONE);
            return;
        }
        const auto st = CurationSchedulerManager::instance().latestStatus();
        if (!st.is_object() || st.empty()) {
            today_banner_box_->setVisibility(brls::Visibility::GONE);
            return;
        }
        const bool running = st.value("running", false);
        const int missing = st.value("missing_enabled_count", 0);
        if (!running && missing <= 0) {
            today_banner_box_->setVisibility(brls::Visibility::GONE);
            return;
        }
        today_banner_box_->setVisibility(brls::Visibility::VISIBLE);
        auto* lbl = ui::makeLabel(st.value("summary_text", ""), 13.0f,
                                  running ? "domain/core" : "status/warn_fg");
        lbl->setGrow(1.0f);
        lbl->setMarginRight(8);
        today_banner_box_->addView(lbl);
        if (!running) {
            today_banner_box_->addView(ui::makeButton(
                "▶ 补跑今日未完成",
                []() { CurationSchedulerManager::instance().runMissingToday("manual"); },
                true, 28.0f));
        } else {
            today_banner_box_->addView(ui::makeButton(
                "查看进度",
                [this]() {
                    if (dialog_) dialog_->dismiss();
                    brls::sync([]() { CurationSchedulerDialog::open(); });
                },
                false, 28.0f));
        }
    }

    void renderSelectedBundleItems() {
        if (!bundle_items_box_) return;
        bundle_items_box_->clearViews();

        const auto media_arr = current_bundle_.value("media_items", nlohmann::json::array());
        const auto text_arr = current_bundle_.value("text_items", nlohmann::json::array());
        const auto book_arr = current_bundle_.value("book_items", nlohmann::json::array());

        int shown = 0;

        auto add_card = [&](const std::string& domain_cat,
                            const std::string& title,
                            const std::string& subtitle,
                            std::function<void()> on_open) {
            if (active_domain_ != "all") {
                if (active_domain_ == "youtube" && domain_cat != "video" && domain_cat != "youtube") return;
                if (active_domain_ != "youtube" && domain_cat != active_domain_) return;
            }
            ++shown;
            auto* card = ui::makeCard("desktop/panel", 10.0f, 8.0f);
            card->setMarginBottom(6);
            card->setFocusable(true);

            auto* row = new brls::Box(brls::Axis::ROW);
            row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            row->setAlignItems(brls::AlignItems::CENTER);

            auto* left = new brls::Box(brls::Axis::COLUMN);
            left->setGrow(1.0f);
            left->setMarginRight(8);
            left->addView(ui::makeLabel("[" + domainBadgeLabel(domain_cat) + "] " + title, 14.5f,
                                        domainBadgeColor(domain_cat)));
            if (!subtitle.empty()) {
                auto* sub = ui::makeLabel(subtitle, 12.5f, "font/grey");
                sub->setMarginTop(2);
                left->addView(sub);
            }
            row->addView(left);
            row->addView(ui::makeLabel("打开 →", 12.5f, "brls/accent"));
            card->addView(row);

            card->registerClickAction([on_open = std::move(on_open)](brls::View*) {
                on_open();
                return true;
            });
            bundle_items_box_->addView(card);
        };

        for (const auto& mj : media_arr) {
            if (!mj.is_object()) continue;
            const std::string kind = mj.value("kind", "video");
            const std::string cat = (kind == "music") ? "music" : "youtube";
            const std::string title = mj.value("title", "");
            const std::string creator = mj.value("creator", "");
            add_card(cat, title, creator, [this, mj]() {
                if (dialog_) dialog_->dismiss();
                if (HostKernel::instance().isInitialized()) {
                    HostKernel::instance().bus().publish("entity.media.open", mj);
                }
            });
        }

        for (const auto& tj : text_arr) {
            if (!tj.is_object()) continue;
            const std::string cat = tj.value("category", "news");
            const std::string title = tj.value("title", "");
            const std::string src = tj.value("source_name", "");
            add_card(cat, title, src, [this, tj]() {
                if (dialog_) dialog_->dismiss();
                if (HostKernel::instance().isInitialized()) {
                    HostKernel::instance().bus().publish("entity.text.open", tj);
                }
            });
        }

        for (const auto& bj : book_arr) {
            if (!bj.is_object()) continue;
            const std::string title = bj.value("title", "");
            const int vc = bj.value("vocab_count", 0);
            const std::string sub = "英语伴读归档 · 词汇 " + std::to_string(vc) + " 个";
            add_card("english", title, sub, [this, bj]() {
                if (dialog_) dialog_->dismiss();
                if (HostKernel::instance().isInitialized()) {
                    HostKernel::instance().bus().publish("entity.book.open", bj);
                }
            });
        }

        if (selected_date_stats_label_) {
            selected_date_stats_label_->setText(
                "共 " + std::to_string(shown) + " 条推荐 · 点击任意卡片直接跳转对应工作台");
        }
        if (shown == 0) {
            bundle_items_box_->addView(ui::makeLabel(
                "该日期在当前筛选领域下暂无推荐记录。\n可切换左侧日期、顶部领域筛选，或点击「同步双仓归档」。",
                14.0f, "font/grey", 1.5f));
        }
    }

    void triggerRehydrate() {
        auto& kernel = HostKernel::instance();
        if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;
        if (archive_summary_label_) archive_summary_label_->setText("正在同步并回灌双仓历史归档…");
        const auto alive = alive_;
        kernel.capabilities().callAsync(
            "recommend.feed.v1",
            {{"action", "bootstrap_and_rehydrate"}, {"pull_remote", true}, {"clone_if_missing", false}},
            [this, alive](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire)) return;
                brls::sync([this, alive, res]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (res.ok) {
                        brls::Application::notify("双仓归档已刷新");
                        loadArchiveDates();
                        loadSelectedDateBundle();
                    } else {
                        brls::Application::notify("同步失败: " + res.error);
                    }
                });
            });
    }

    CalendarDialogOptions options_;
    std::string today_date_;
    std::string active_domain_;
    std::string selected_date_;
    std::string latest_date_with_data_;
    int view_year_ = 2026;
    int view_month_ = 11;

    std::map<std::string, DateCountStats> date_stats_;
    nlohmann::json current_bundle_ = nlohmann::json::object();

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> dates_req_;
    std::shared_ptr<CancelToken> bundle_req_;

    brls::Dialog* dialog_ = nullptr;
    brls::Label* archive_summary_label_ = nullptr;
    brls::Box* domain_bar_ = nullptr;
    brls::Label* month_title_label_ = nullptr;
    brls::Box* calendar_grid_box_ = nullptr;
    brls::Button* apply_filter_btn_ = nullptr;
    brls::Label* selected_date_title_ = nullptr;
    brls::Label* selected_date_stats_label_ = nullptr;
    brls::Box* today_banner_box_ = nullptr;
    brls::Box* bundle_items_box_ = nullptr;
};

} // namespace

void CurationCalendarDialog::open(CalendarDialogOptions options) {
    auto* box = new CalendarDialogBox(std::move(options));
    auto* dialog = new brls::Dialog(box);
    box->setDialog(dialog);
    dialog->addButton("关闭", []() {});
    dialog->open();
}

} // namespace omnidesk::host
