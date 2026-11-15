#pragma once

#include <functional>
#include <string>

namespace omnidesk::host {

struct CalendarDialogOptions {
    std::string initial_date;    ///< YYYY-MM-DD (empty -> latest archived date or today)
    std::string initial_domain;  ///< ""/"all" | "music" | "youtube" | "news" | "reddit" | "journal" | "english"
    std::function<void(const std::string& selected_date)> on_apply_date_filter;
    std::function<void()> on_clear_date_filter;
    std::function<void()> on_open_scheduler;
};

class CurationCalendarDialog {
public:
    static void open(CalendarDialogOptions options = {});
};

} // namespace omnidesk::host
