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

void shiftYearMonth(int y, int m, int delta, int& out_y, int& out_m) {
    int total = y * 12 + (m - 1) + delta;
    out_y = total / 12;
    out_m = (total % 12) + 1;
    if (out_m <= 0) {
        out_m += 12;
        out_y -= 1;
    }
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

bool isDescendantOfBox(brls::View* view, brls::Box* ancestor) {
    if (!view || !ancestor) return false;
    for (brls::View* cur = view; cur != nullptr; cur = cur->getParent()) {
        if (cur == ancestor) return true;
    }
    return false;
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

std::string jsonStr(const nlohmann::json& obj, const char* key, const std::string& fallback = "") {
    if (!obj.is_object()) return fallback;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return fallback;
    if (it->is_string()) return it->get<std::string>();
    return fallback;
}

int jsonInt(const nlohmann::json& obj, const char* key, int fallback = 0) {
    if (!obj.is_object()) return fallback;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return fallback;
    if (it->is_number_integer()) return it->get<int>();
    if (it->is_number()) return static_cast<int>(it->get<double>());
    return fallback;
}

bool jsonBool(const nlohmann::json& obj, const char* key, bool fallback = false) {
    if (!obj.is_object()) return fallback;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return fallback;
    if (it->is_boolean()) return it->get<bool>();
    return fallback;
}

class CalendarDialogBox final : public brls::Box {
public:
    explicit CalendarDialogBox(CalendarDialogOptions options)
        : brls::Box(brls::Axis::COLUMN),
          options_(std::move(options)),
          today_date_(currentShanghaiDateStr()),
          active_domain_(options_.initial_domain.empty() ? "all" : options_.initial_domain) {
        const float viewport_w =
            brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
        const float viewport_h =
            brls::Application::contentHeight > 0 ? brls::Application::contentHeight : 720.0f;
        dialog_width_ = std::clamp(viewport_w - 64.0f, 760.0f, 980.0f);
        const float split_h = std::clamp(viewport_h - 210.0f, 350.0f, 440.0f);
        const float left_w = (dialog_width_ < 860.0f) ? 380.0f : 436.0f;

        setPadding(18, 20, 14, 20);
        setWidth(dialog_width_);
        setBackgroundColor(ui::color("brls/background"));
        setClipsToBounds(true);

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
        hdr->setWidthPercentage(100);
        hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        hdr->setAlignItems(brls::AlignItems::CENTER);
        hdr->setMarginBottom(10);
        auto* title_lbl = ui::makeLabel("推荐日历与跨三域每日清单浏览器", 19.5f, "brls/text");
        title_lbl->setSingleLine(true);
        hdr->addView(title_lbl);
        archive_summary_label_ = ui::makeLabel("正在加载推荐日期索引…", 13.0f, "font/grey");
        archive_summary_label_->setSingleLine(true);
        archive_summary_label_->setShrink(0.0f);
        archive_summary_label_->setMarginLeft(12);
        hdr->addView(archive_summary_label_);
        addView(hdr);

        // Domain filter bar
        domain_bar_ = new brls::Box(brls::Axis::ROW);
        domain_bar_->setWidthPercentage(100);
        domain_bar_->setAlignItems(brls::AlignItems::CENTER);
        domain_bar_->setMarginBottom(10);
        rebuildDomainPills();
        addView(domain_bar_);

        // Two-pane body
        auto* split = new brls::Box(brls::Axis::ROW);
        split->setHeight(split_h);
        split->setWidthPercentage(100);

        // Left pane: calendar month grid + actions
        auto* left_pane = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        left_pane->setWidth(left_w);
        left_pane->setShrink(0.0f);
        left_pane->setMarginRight(14);
        left_pane->setHeightPercentage(100);
        left_pane->setClipsToBounds(true);

        // Month nav row
        auto* month_nav = new brls::Box(brls::Axis::ROW);
        month_nav->setWidthPercentage(100);
        month_nav->setAlignItems(brls::AlignItems::CENTER);
        month_nav->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        month_nav->setMarginBottom(8);

        auto* prev_btn = ui::makeButton("◀ 上月", [this]() { shiftMonth(-1); }, false, 30.0f);
        prev_btn->setShrink(0.0f);
        month_title_label_ = ui::makeLabel("", 15.5f, "brls/text");
        month_title_label_->setSingleLine(true);
        auto* next_btn = ui::makeButton("下月 ▶", [this]() { shiftMonth(1); }, false, 30.0f);
        next_btn->setShrink(0.0f);
        auto* today_btn = ui::makeButton("今天", [this]() { jumpToDate(today_date_); }, false, 30.0f);
        today_btn->setShrink(0.0f);
        auto* latest_btn = ui::makeButton(
            "最新",
            [this]() {
                if (!latest_date_with_data_.empty()) {
                    jumpToDate(latest_date_with_data_);
                } else {
                    jumpToDate(today_date_);
                }
            },
            false, 30.0f);
        latest_btn->setShrink(0.0f);

        month_nav->addView(prev_btn);
        month_nav->addView(month_title_label_);
        month_nav->addView(next_btn);
        month_nav->addView(today_btn);
        month_nav->addView(latest_btn);
        left_pane->addView(month_nav);

        // Weekday header (exact same flex-basis/margin math as the 7 day columns)
        auto* wd_row = new brls::Box(brls::Axis::ROW);
        wd_row->setWidthPercentage(100);
        wd_row->setMarginBottom(5);
        static const char* kWd[] = {"一", "二", "三", "四", "五", "六", "日"};
        for (int col = 0; col < 7; ++col) {
            auto* cell = new brls::Box(brls::Axis::COLUMN);
            cell->setWidth(0);
            cell->setGrow(1.0f);
            cell->setShrink(1.0f);
            cell->setMarginRight(col < 6 ? 4 : 0);
            cell->setAlignItems(brls::AlignItems::CENTER);
            auto* wl = ui::makeLabel(kWd[col], 12.5f, col >= 5 ? "brls/accent" : "font/grey");
            wl->setSingleLine(true);
            cell->addView(wl);
            wd_row->addView(cell);
        }
        left_pane->addView(wd_row);

        calendar_grid_box_ = new brls::Box(brls::Axis::COLUMN);
        calendar_grid_box_->setWidthPercentage(100);
        calendar_grid_box_->setHeight(0);
        calendar_grid_box_->setGrow(1.0f);
        calendar_grid_box_->setShrink(1.0f);
        left_pane->addView(calendar_grid_box_);

        // Footer actions under calendar
        auto* left_footer = new brls::Box(brls::Axis::ROW);
        left_footer->setWidthPercentage(100);
        left_footer->setMarginTop(8);
        left_footer->setAlignItems(brls::AlignItems::CENTER);
        apply_filter_btn_ = ui::makeButton(
            "按选中日期筛选工作台",
            [this]() {
                auto cb = options_.on_apply_date_filter;
                const std::string d = selected_date_;
                const std::string dom = active_domain_;
                if (dialog_) dialog_->dismiss();
                if (d.empty()) return;
                if (cb) {
                    brls::sync([cb, d]() { cb(d); });
                } else if (HostKernel::instance().isInitialized()) {
                    brls::sync([d, dom]() {
                        if (HostKernel::instance().isInitialized()) {
                            HostKernel::instance().bus().publish(
                                "entity.curation.filter_date",
                                {{"business_date", d}, {"domain", dom}});
                        }
                    });
                }
            },
            true, 34.0f);
        apply_filter_btn_->setWidth(0);
        apply_filter_btn_->setGrow(1.0f);
        apply_filter_btn_->setShrink(1.0f);
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
            clr_btn->setShrink(0.0f);
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
                    brls::sync([]() {
                        CurationSchedulerDialog::open([]() { CurationCalendarDialog::open(); });
                    });
                }
            },
            false, 34.0f);
        sched_btn->setShrink(0.0f);
        left_footer->addView(sched_btn);

        left_pane->addView(left_footer);
        split->addView(left_pane);

        // Right pane: selected date bundle preview + click-to-open
        // NOTE: override ui::makeCard's widthPercentage(100) with setWidth(0)+grow+shrink so it
        // fills remaining ROW width cleanly instead of overflowing the dialog horizontally.
        auto* right_pane = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        right_pane->setWidth(0);
        right_pane->setGrow(1.0f);
        right_pane->setShrink(1.0f);
        right_pane->setHeightPercentage(100);
        right_pane->setClipsToBounds(true);

        auto* r_hdr = new brls::Box(brls::Axis::ROW);
        r_hdr->setWidthPercentage(100);
        r_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        r_hdr->setAlignItems(brls::AlignItems::CENTER);
        r_hdr->setMarginBottom(6);
        selected_date_title_ = ui::makeLabel("选中日期：" + selected_date_, 16.5f, "brls/text");
        selected_date_title_->setWidth(0);
        selected_date_title_->setGrow(1.0f);
        selected_date_title_->setShrink(1.0f);
        selected_date_title_->setSingleLine(true);
        selected_date_title_->setMarginRight(8);
        r_hdr->addView(selected_date_title_);
        auto* sync_btn = ui::makeButton("同步双仓归档", [this]() { triggerRehydrate(); }, false, 30.0f);
        sync_btn->setShrink(0.0f);
        r_hdr->addView(sync_btn);
        right_pane->addView(r_hdr);

        selected_date_stats_label_ = ui::makeLabel("点击日历格子查看当天全部推荐…", 13.0f, "font/grey");
        selected_date_stats_label_->setSingleLine(true);
        selected_date_stats_label_->setMarginBottom(8);
        right_pane->addView(selected_date_stats_label_);

        today_banner_box_ = new brls::Box(brls::Axis::ROW);
        today_banner_box_->setWidthPercentage(100);
        today_banner_box_->setAlignItems(brls::AlignItems::CENTER);
        today_banner_box_->setMarginBottom(8);
        today_banner_box_->setVisibility(brls::Visibility::GONE);
        right_pane->addView(today_banner_box_);

        auto* right_scroll = new brls::ScrollingFrame();
        right_scroll->setWidthPercentage(100);
        right_scroll->setHeight(0);
        right_scroll->setGrow(1.0f);
        right_scroll->setShrink(1.0f);
        bundle_items_box_ = new brls::Box(brls::Axis::COLUMN);
        bundle_items_box_->setWidthPercentage(100);
        right_scroll->setContentView(bundle_items_box_);
        right_pane->addView(right_scroll);

        split->addView(right_pane);
        addView(split);

        sched_sub_token_ = CurationSchedulerManager::instance().subscribeStatus(
            [alive = alive_, this](const nlohmann::json&) {
                if (!alive->load(std::memory_order_acquire)) return;
                brls::sync([alive, this]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    updateTodaySchedulerBanner();
                });
            });

        if (HostKernel::instance().isInitialized()) {
            rec_bus_token_ = HostKernel::instance().bus().subscribe(
                "entity.recommend.updated",
                "host.curation_calendar_dialog",
                [alive = alive_, this](const std::string&, const nlohmann::json&) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    brls::sync([alive, this]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        loadArchiveDates();
                        loadSelectedDateBundle();
                    });
                });
        }

        rebuildCalendarGrid();
        loadArchiveDates();
        loadSelectedDateBundle();
    }

    ~CalendarDialogBox() override {
        alive_->store(false, std::memory_order_release);
        if (dates_req_) dates_req_->cancel();
        if (bundle_req_) bundle_req_->cancel();
        if (sched_sub_token_ != 0) {
            CurationSchedulerManager::instance().unsubscribeStatus(sched_sub_token_);
            sched_sub_token_ = 0;
        }
        if (rec_bus_token_ != 0 && HostKernel::instance().isInitialized()) {
            HostKernel::instance().bus().unsubscribe(rec_bus_token_);
            rec_bus_token_ = 0;
        }
    }

    void setDialog(brls::Dialog* dlg) { dialog_ = dlg; }
    float dialogWidth() const { return dialog_width_; }

private:
    void rebuildDomainPills() {
        if (!domain_bar_) return;
        const bool had_focus = isDescendantOfBox(brls::Application::getCurrentFocus(), domain_bar_);
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
        brls::Button* active_btn = nullptr;
        for (const auto& dom : kDomains) {
            const std::string id = dom.id;
            const bool is_active = (active_domain_ == id);
            auto* btn = ui::makeButton(
                dom.label,
                [this, id]() {
                    active_domain_ = id;
                    rebuildDomainPills();
                    rebuildCalendarGrid();
                    renderSelectedBundleItems();
                },
                is_active, 30.0f);
            btn->setShrink(0.0f);
            btn->setMarginRight(6);
            if (is_active) active_btn = btn;
            domain_bar_->addView(btn);
        }
        if (had_focus && active_btn) {
            brls::Application::giveFocus(active_btn);
        }
    }

    void shiftMonth(int delta) {
        shiftYearMonth(view_year_, view_month_, delta, view_year_, view_month_);
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
        const bool focus_was_in_grid =
            isDescendantOfBox(brls::Application::getCurrentFocus(), calendar_grid_box_);
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
        int prev_y = view_year_, prev_m = view_month_;
        shiftYearMonth(view_year_, view_month_, -1, prev_y, prev_m);
        const int prev_num_days = daysInMonth(prev_y, prev_m);
        int next_y = view_year_, next_m = view_month_;
        shiftYearMonth(view_year_, view_month_, 1, next_y, next_m);

        brls::Box* selected_cell = nullptr;
        brls::Box* fallback_cell = nullptr;

        for (int row = 0; row < 6; ++row) {
            auto* row_box = new brls::Box(brls::Axis::ROW);
            row_box->setWidthPercentage(100);
            row_box->setHeight(0);
            row_box->setGrow(1.0f);
            row_box->setShrink(1.0f);
            row_box->setAlignItems(brls::AlignItems::STRETCH);
            row_box->setMarginBottom(row < 5 ? 4 : 0);

            for (int col = 0; col < 7; ++col) {
                const int cell_idx = row * 7 + col;
                int cell_y = view_year_;
                int cell_m = view_month_;
                int cell_d = 1;
                bool in_current_month = true;

                if (cell_idx < first_wd) {
                    in_current_month = false;
                    cell_y = prev_y;
                    cell_m = prev_m;
                    cell_d = prev_num_days - (first_wd - 1 - cell_idx);
                } else if (cell_idx - first_wd + 1 > num_days) {
                    in_current_month = false;
                    cell_y = next_y;
                    cell_m = next_m;
                    cell_d = (cell_idx - first_wd + 1) - num_days;
                } else {
                    cell_d = cell_idx - first_wd + 1;
                }

                const std::string iso = formatIsoYmd(cell_y, cell_m, cell_d);
                const bool is_selected = (iso == selected_date_);
                const bool is_today = (iso == today_date_);

                int cnt = 0;
                auto dit = date_stats_.find(iso);
                if (dit != date_stats_.end()) {
                    cnt = dit->second.countForDomain(active_domain_);
                }

                auto* cell = new brls::Box(brls::Axis::COLUMN);
                cell->setWidth(0);
                cell->setGrow(1.0f);
                cell->setShrink(1.0f);
                cell->setMarginRight(col < 6 ? 4 : 0);
                cell->setPadding(2, 2, 2, 2);
                cell->setCornerRadius(7.0f);
                cell->setHighlightCornerRadius(7.0f);
                cell->setHideHighlightBackground(true);
                cell->setHideHighlightBorder(true);
                cell->setAlignItems(brls::AlignItems::CENTER);
                cell->setJustifyContent(brls::JustifyContent::CENTER);
                cell->setFocusable(true);

                const char* num_tok = "font/grey";
                const char* sub_tok = "font/grey";

                if (is_selected) {
                    cell->setBackgroundColor(ui::color("brls/button/primary_enabled_background"));
                    cell->setBorderThickness(1.5f);
                    cell->setBorderColor(ui::color("brls/accent"));
                    num_tok = "brls/button/primary_enabled_text";
                    sub_tok = "brls/button/primary_enabled_text";
                } else if (!in_current_month) {
                    cell->setBackgroundColor(ui::color("brls/background"));
                    cell->setAlpha(0.45f);
                    num_tok = "font/grey";
                    sub_tok = (cnt > 0) ? "domain/core" : "font/grey";
                } else if (is_today) {
                    cell->setBackgroundColor(ui::color("desktop/active"));
                    cell->setBorderThickness(1.5f);
                    cell->setBorderColor(ui::color("brls/accent"));
                    num_tok = "brls/accent";
                    sub_tok = (cnt > 0) ? "domain/core" : "brls/accent";
                } else if (cnt > 0) {
                    cell->setBackgroundColor(ui::color("desktop/soft"));
                    cell->setBorderThickness(1.0f);
                    cell->setBorderColor(ui::color("desktop/line"));
                    num_tok = "brls/text";
                    sub_tok = "domain/core";
                } else {
                    cell->setBackgroundColor(ui::color("brls/background"));
                    cell->setBorderThickness(1.0f);
                    cell->setBorderColor(ui::color("desktop/line"));
                    num_tok = "font/grey";
                    sub_tok = "font/grey";
                }

                std::string top_txt = std::to_string(cell_d);
                if (is_today) top_txt += "今";
                auto* num_lbl = ui::makeLabel(top_txt, 13.0f, num_tok, 1.15f);
                num_lbl->setSingleLine(true);
                cell->addView(num_lbl);

                std::string sub_txt = (cnt > 0) ? (std::to_string(cnt) + "条") : "·";
                auto* sub_lbl = ui::makeLabel(sub_txt, 10.5f, sub_tok, 1.15f);
                sub_lbl->setSingleLine(true);
                cell->addView(sub_lbl);

                cell->registerClickAction([this, iso, in_current_month](brls::View*) {
                    if (!in_current_month) {
                        jumpToDate(iso);
                    } else {
                        selected_date_ = iso;
                        rebuildCalendarGrid();
                        loadSelectedDateBundle();
                    }
                    return true;
                });

                if (is_selected) {
                    selected_cell = cell;
                }
                if (in_current_month && cell_d == 1) {
                    fallback_cell = cell;
                }
                row_box->addView(cell);
            }
            calendar_grid_box_->addView(row_box);
        }

        if (focus_was_in_grid) {
            if (selected_cell) {
                brls::Application::giveFocus(selected_cell);
            } else if (fallback_cell) {
                brls::Application::giveFocus(fallback_cell);
            }
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
                if (res.data.is_object() && res.data.contains("dates") && res.data["dates"].is_array()) {
                    for (const auto& entry : res.data["dates"]) {
                        if (!entry.is_object()) continue;
                        const std::string d = jsonStr(entry, "date");
                        if (d.empty()) continue;
                        DateCountStats st;
                        st.music = jsonInt(entry, "music", 0);
                        st.youtube = jsonInt(entry, "youtube", 0);
                        st.news = jsonInt(entry, "news", 0);
                        st.reddit = jsonInt(entry, "reddit", 0);
                        st.journal = jsonInt(entry, "journal", 0);
                        st.english = jsonInt(entry, "english", 0);
                        st.total = jsonInt(
                            entry,
                            "total",
                            st.music + st.youtube + st.news + st.reddit + st.journal + st.english);
                        parsed[d] = st;
                        if (d > latest) latest = d;
                    }
                }
                const int total_dates = jsonInt(res.data, "total_dates", static_cast<int>(parsed.size()));
                brls::sync([this, alive, tok, parsed = std::move(parsed), latest, total_dates]() mutable {
                    if (!alive->load(std::memory_order_acquire) || tok->isCancelled()) return;
                    const bool first_hydration = !initial_dates_hydrated_;
                    initial_dates_hydrated_ = true;
                    date_stats_ = std::move(parsed);
                    latest_date_with_data_ = latest;
                    if (archive_summary_label_) {
                        archive_summary_label_->setText("已索引 " + std::to_string(total_dates) + " 天" +
                                                        (latest.empty() ? "" : (" · 最新 " + latest)));
                    }
                    if (first_hydration &&
                        options_.initial_date.empty() &&
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
        if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) {
            if (selected_date_stats_label_) {
                selected_date_stats_label_->setText("未检测到 AI 策展插件 (plugin-ai-curator)");
            }
            return;
        }

        if (bundle_req_) bundle_req_->cancel();
        bundle_req_ = CancelToken::create();
        const auto alive = alive_;
        const auto tok = bundle_req_;
        const std::string req_date = selected_date_;

        if (selected_date_stats_label_) {
            selected_date_stats_label_->setText("正在加载 " + req_date + " 推荐内容…");
        }

        kernel.capabilities().callAsync(
            "recommend.feed.v1",
            {{"action", "get_date_bundle"}, {"business_date", req_date}},
            [this, alive, tok, req_date](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire) || tok->isCancelled()) return;
                if (!res.ok) {
                    brls::sync([this, alive, tok, err = res.error]() {
                        if (!alive->load(std::memory_order_acquire) || tok->isCancelled()) return;
                        if (selected_date_stats_label_) {
                            selected_date_stats_label_->setText("加载失败: " + err);
                        }
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
        const bool running = jsonBool(st, "running", false);
        const int missing = jsonInt(st, "missing_enabled_count", 0);
        if (!running && missing <= 0) {
            today_banner_box_->setVisibility(brls::Visibility::GONE);
            return;
        }
        today_banner_box_->setVisibility(brls::Visibility::VISIBLE);
        auto* lbl = ui::makeLabel(jsonStr(st, "summary_text"), 13.0f,
                                  running ? "domain/core" : "status/warn_fg", 1.3f);
        lbl->setWidth(0);
        lbl->setGrow(1.0f);
        lbl->setShrink(1.0f);
        lbl->setMarginRight(8);
        today_banner_box_->addView(lbl);
        if (!running) {
            auto* run_btn = ui::makeButton(
                "▶ 补跑今日未完成",
                []() { CurationSchedulerManager::instance().runMissingToday("manual"); },
                true, 30.0f);
            run_btn->setShrink(0.0f);
            today_banner_box_->addView(run_btn);
        } else {
            auto* prog_btn = ui::makeButton(
                "查看进度",
                [this]() {
                    if (dialog_) dialog_->dismiss();
                    brls::sync([]() {
                        CurationSchedulerDialog::open([]() { CurationCalendarDialog::open(); });
                    });
                },
                false, 30.0f);
            prog_btn->setShrink(0.0f);
            today_banner_box_->addView(prog_btn);
        }
    }

    void renderSelectedBundleItems() {
        if (!bundle_items_box_) return;
        bundle_items_box_->clearViews();

        auto get_arr = [](const nlohmann::json& obj, const char* k) -> nlohmann::json {
            if (!obj.is_object()) return nlohmann::json::array();
            auto it = obj.find(k);
            return (it != obj.end() && it->is_array()) ? *it : nlohmann::json::array();
        };
        const auto media_arr = get_arr(current_bundle_, "media_items");
        const auto text_arr = get_arr(current_bundle_, "text_items");
        const auto book_arr = get_arr(current_bundle_, "book_items");

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
            auto* card = ui::makeCard("desktop/soft", 11.0f, 9.0f);
            card->setMarginBottom(7);
            card->setFocusable(true);
            card->setClipsToBounds(true);

            auto* top = new brls::Box(brls::Axis::ROW);
            top->setWidthPercentage(100);
            top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            top->setAlignItems(brls::AlignItems::CENTER);
            top->setMarginBottom(5);

            auto* meta_left = new brls::Box(brls::Axis::ROW);
            meta_left->setWidth(0);
            meta_left->setGrow(1.0f);
            meta_left->setShrink(1.0f);
            meta_left->setAlignItems(brls::AlignItems::CENTER);
            meta_left->setMarginRight(8);

            auto* pill = ui::makePill(domainBadgeLabel(domain_cat), "desktop/surface",
                                      domainBadgeColor(domain_cat));
            pill->setShrink(0.0f);
            meta_left->addView(pill);

            if (!subtitle.empty()) {
                auto* sub = ui::makeLabel(subtitle, 12.5f, "font/grey");
                sub->setMarginLeft(8);
                sub->setWidth(0);
                sub->setGrow(1.0f);
                sub->setShrink(1.0f);
                sub->setSingleLine(true);
                meta_left->addView(sub);
            }
            top->addView(meta_left);

            auto* open_lbl = ui::makeLabel("打开 →", 12.5f, "brls/accent");
            open_lbl->setShrink(0.0f);
            open_lbl->setSingleLine(true);
            top->addView(open_lbl);
            card->addView(top);

            auto* ttl = ui::makeLabel(title.empty() ? "(无标题)" : title, 14.5f, "brls/text", 1.38f);
            card->addView(ttl);

            card->registerClickAction([on_open = std::move(on_open)](brls::View*) {
                on_open();
                return true;
            });
            bundle_items_box_->addView(card);
        };

        for (const auto& mj : media_arr) {
            if (!mj.is_object()) continue;
            const std::string kind = jsonStr(mj, "kind", "video");
            const std::string cat = (kind == "music") ? "music" : "youtube";
            const std::string title = jsonStr(mj, "title");
            const std::string creator = jsonStr(mj, "creator");
            add_card(cat, title, creator, [this, mj]() {
                if (dialog_) dialog_->dismiss();
                if (HostKernel::instance().isInitialized()) {
                    HostKernel::instance().bus().publish("entity.media.open", mj);
                }
            });
        }

        for (const auto& tj : text_arr) {
            if (!tj.is_object()) continue;
            const std::string cat = jsonStr(tj, "category", "news");
            const std::string title = jsonStr(tj, "title");
            const std::string src = jsonStr(tj, "source_name");
            add_card(cat, title, src, [this, tj]() {
                if (dialog_) dialog_->dismiss();
                if (HostKernel::instance().isInitialized()) {
                    HostKernel::instance().bus().publish("entity.text.open", tj);
                }
            });
        }

        for (const auto& bj : book_arr) {
            if (!bj.is_object()) continue;
            const std::string title = jsonStr(bj, "title");
            const int vc = jsonInt(bj, "vocab_count", 0);
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
            auto* empty_card = ui::makeCard("desktop/soft", 14.0f, 9.0f);
            empty_card->addView(ui::makeLabel(
                "该日期在当前筛选领域下暂无推荐记录。\n可切换左侧日期、顶部领域筛选，或点击右上角「同步双仓归档」。",
                14.0f, "font/grey", 1.5f));
            bundle_items_box_->addView(empty_card);
        }
    }

    void triggerRehydrate() {
        auto& kernel = HostKernel::instance();
        if (!kernel.isInitialized() || !kernel.capabilities().has("recommend.feed.v1")) return;
        if (archive_summary_label_) archive_summary_label_->setText("正在同步并回灌双仓历史归档…");
        const auto alive = alive_;
        kernel.capabilities().callAsync(
            "recommend.feed.v1",
            {{"action", "bootstrap_and_rehydrate"},
             {"pull_remote", true},
             {"push_remote", true},
             {"clone_if_missing", true}},
            [this, alive](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire)) return;
                brls::sync([this, alive, res]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (res.ok) {
                        brls::Application::notify("GitHub 双仓已双向同步并刷新归档");
                        loadArchiveDates();
                        loadSelectedDateBundle();
                    } else {
                        brls::Application::notify("同步失败: " + res.error);
                    }
                });
            });
    }

    CalendarDialogOptions options_;
    float dialog_width_ = 980.0f;
    std::string today_date_;
    std::string active_domain_;
    std::string selected_date_;
    std::string latest_date_with_data_;
    int view_year_ = 2026;
    int view_month_ = 11;
    bool initial_dates_hydrated_ = false;

    std::map<std::string, DateCountStats> date_stats_;
    nlohmann::json current_bundle_ = nlohmann::json::object();

    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::shared_ptr<CancelToken> dates_req_;
    std::shared_ptr<CancelToken> bundle_req_;
    uint64_t sched_sub_token_ = 0;
    uint64_t rec_bus_token_ = 0;

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
    if (!brls::Application::getPlatform()) {
        return;
    }
    auto* box = new CalendarDialogBox(std::move(options));
    auto* dialog = new brls::Dialog(box);
    box->setDialog(dialog);
    if (auto* applet = dynamic_cast<brls::Box*>(dialog->getView("brls/dialog/applet"))) {
        applet->setWidth(box->dialogWidth());
        applet->setCornerRadius(14.0f);
        applet->setBorderThickness(1.0f);
        applet->setBorderColor(ui::color("desktop/line"));
        applet->setBackgroundColor(ui::color("brls/background"));
        applet->setClipsToBounds(true);
    }
    dialog->addButton("关闭 Esc", []() {});
    if (auto* btn1 = dialog->getView("brls/dialog/button1")) {
        if (auto* btn_row = btn1->getParent()) {
            btn_row->setHeight(52.0f);
        }
    }
    dialog->open();
}

} // namespace omnidesk::host
