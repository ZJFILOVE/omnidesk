#include "omnidesk/host/companion_dock.hpp"

#include <SDL2/SDL.h>
#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <iomanip>
#include <sstream>
#include <vector>

#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/curation_scheduler_dialog.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/nav_shortcuts.hpp"
#include "omnidesk/host/svg_view.hpp"
#include "omnidesk/markdown_view.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

constexpr uint64_t kStreamUiFlushIntervalMs = 32;

uint64_t nowSteadyMs() {
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch())
            .count());
}

int64_t nowUnixSec() {
    return static_cast<int64_t>(std::time(nullptr));
}

std::string trimWhitespace(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && (s[b] == ' ' || s[b] == '\t' || s[b] == '\r' || s[b] == '\n')) {
        ++b;
    }
    size_t e = s.size();
    while (e > b && (s[e - 1] == ' ' || s[e - 1] == '\t' || s[e - 1] == '\r' || s[e - 1] == '\n')) {
        --e;
    }
    return s.substr(b, e - b);
}

std::string toLowerAscii(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

std::string clipUtf8Codepoints(const std::string& s, size_t max_cp) {
    size_t cp = 0;
    size_t i = 0;
    while (i < s.size() && cp < max_cp) {
        const unsigned char ch = static_cast<unsigned char>(s[i]);
        size_t adv = 1;
        if ((ch & 0x80u) == 0) adv = 1;
        else if ((ch & 0xE0u) == 0xC0u) adv = 2;
        else if ((ch & 0xF0u) == 0xE0u) adv = 3;
        else if ((ch & 0xF8u) == 0xF0u) adv = 4;
        if (i + adv > s.size()) break;
        i += adv;
        ++cp;
    }
    if (i < s.size()) {
        return s.substr(0, i) + "…";
    }
    return s;
}

std::string formatDurationSeconds(double sec) {
    if (sec <= 0.05) return "";
    std::ostringstream oss;
    oss << std::fixed << std::setprecision(1) << sec;
    return oss.str();
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

std::string nvgHex(NVGcolor col) {
    const int r = std::clamp(static_cast<int>(std::lround(col.r * 255.0f)), 0, 255);
    const int g = std::clamp(static_cast<int>(std::lround(col.g * 255.0f)), 0, 255);
    const int b = std::clamp(static_cast<int>(std::lround(col.b * 255.0f)), 0, 255);
    char buf[16];
    std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", r, g, b);
    return std::string(buf);
}

std::string svgAiEmblem(const std::string& primary_hex, const std::string& secondary_hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<path d=\"M12 2.5l2.3 6.2L20.5 11l-6.2 2.3L12 19.5l-2.3-6.2L3.5 11l6.2-2.3L12 2.5z\" "
           "stroke=\"" + primary_hex + "\" stroke-width=\"1.85\"/>"
           "<path d=\"M19 15.5l.9 2.2 2.1.8-2.1.9-.9 2.1-.8-2.1-2.2-.9 2.2-.8.8-2.2z\" "
           "stroke=\"" + secondary_hex + "\" stroke-width=\"1.75\"/>"
           "</svg>";
}

std::string svgPlus(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<line x1=\"12\" y1=\"5\" x2=\"12\" y2=\"19\"/>"
           "<line x1=\"5\" y1=\"12\" x2=\"19\" y2=\"12\"/>"
           "</svg>";
}

std::string svgExpandStage(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"15\" height=\"15\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<polyline points=\"15 3 21 3 21 9\"/>"
           "<polyline points=\"9 21 3 21 3 15\"/>"
           "<line x1=\"21\" y1=\"3\" x2=\"14\" y2=\"10\"/>"
           "<line x1=\"3\" y1=\"21\" x2=\"10\" y2=\"14\"/>"
           "</svg>";
}

std::string svgDockCollapse(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"15\" height=\"15\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<rect x=\"3\" y=\"3\" width=\"18\" height=\"18\" rx=\"3\"/>"
           "<line x1=\"15\" y1=\"3\" x2=\"15\" y2=\"21\"/>"
           "<polyline points=\"9 9 12 12 9 15\"/>"
           "</svg>";
}

std::string svgChevronDown(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"12\" height=\"12\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<polyline points=\"6 9 12 15 18 9\"/>"
           "</svg>";
}

std::string svgSparkleTab(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<path d=\"M12 3l1.9 5.3L19.2 10l-5.3 1.9L12 17.2l-1.9-5.3L4.8 10l5.3-1.7L12 3z\"/>"
           "</svg>";
}

std::string svgWidgetsGrid(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<rect x=\"3\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/>"
           "<rect x=\"14\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/>"
           "<rect x=\"14\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/>"
           "<rect x=\"3\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/>"
           "</svg>";
}

std::string svgCalendarQuick(const std::string& hex) {
    return "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" "
           "fill=\"none\" stroke=\"" + hex +
           "\" stroke-width=\"2.0\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
           "<rect x=\"3\" y=\"4\" width=\"18\" height=\"18\" rx=\"2\"/>"
           "<line x1=\"16\" y1=\"2\" x2=\"16\" y2=\"6\"/>"
           "<line x1=\"8\" y1=\"2\" x2=\"8\" y2=\"6\"/>"
           "<line x1=\"3\" y1=\"10\" x2=\"21\" y2=\"10\"/>"
           "</svg>";
}

bool extractCurationCommand(const std::string& prompt, std::string* query_out) {
    static const char* kPrefixes[] = {
        "/curate ",
        "策展：",
        "策展:",
        "推荐：",
        "推荐:",
    };
    for (const char* prefix : kPrefixes) {
        const std::string p(prefix);
        if (prompt.rfind(p, 0) == 0) {
            std::string rest = trimWhitespace(prompt.substr(p.size()));
            *query_out = rest.empty() ? prompt : rest;
            return true;
        }
    }
    return false;
}

bool isWidgetPrimaryForDomain(const DockWidgetSpec& w, ContentDomain domain) {
    const std::string key = toLowerAscii(w.plugin_id + " " + w.widget_id + " " + w.title);
    auto hasAny = [&](std::initializer_list<const char*> needles) {
        for (const char* n : needles) {
            if (key.find(n) != std::string::npos) return true;
        }
        return false;
    };

    switch (domain) {
        case ContentDomain::Media:
            if (hasAny({"music", "bilibili", "b站", "imdb", "youtube", "fnos", "media", "video"})) {
                return true;
            }
            break;
        case ContentDomain::Book:
            if (hasAny({"anna", "zlib", "z-lib", "book", "pdf", "epub", "babeldoc", "vocab"})) {
                return true;
            }
            break;
        case ContentDomain::Text:
            if (hasAny({"chrome", "web", "blog", "reddit", "rss", "news", "journal", "linuxdo"})) {
                return true;
            }
            break;
        case ContentDomain::Core:
            if (hasAny({"magpie", "router", "agent", "curator"})) {
                return true;
            }
            break;
    }

    if (w.supported_domains.size() == 1 && w.supported_domains.front() == domain) {
        return true;
    }
    if (domain != ContentDomain::Core && !w.supported_domains.empty()) {
        const bool matches_domain =
            std::find(w.supported_domains.begin(), w.supported_domains.end(), domain) !=
            w.supported_domains.end();
        const bool is_generic_core_tool = hasAny({"magpie", "router"});
        if (matches_domain && !is_generic_core_tool) {
            return true;
        }
    }
    return false;
}

void harmonizeDockWidgetSubtree(brls::View* node, bool is_root) {
    if (!node) return;

    if (auto* btn = dynamic_cast<brls::Button*>(node)) {
        const bool was_unbordered_primary = (btn->getBorderThickness() < 0.5f);
        btn->setCornerRadius(9.0f);
        btn->setHighlightCornerRadius(9.0f);
        btn->setHideHighlightBackground(true);
        btn->setHideHighlightBorder(true);
        btn->setStyle(&brls::BUTTONSTYLE_BORDERED);
        if (was_unbordered_primary) {
            btn->setTextColor(ui::color("brls/accent"));
            btn->setBackgroundColor(ui::color("desktop/active"));
            btn->setBorderThickness(1.0f);
            btn->setBorderColor(ui::color("brls/accent"));
        } else {
            btn->setTextColor(ui::color("brls/text"));
            btn->setBackgroundColor(ui::color("desktop/soft"));
            btn->setBorderThickness(1.0f);
            btn->setBorderColor(ui::color("desktop/line"));
        }
        return;
    }

    if (auto* box = dynamic_cast<brls::Box*>(node)) {
        if (is_root) {
            if (box->getCornerRadius() < 4.0f) {
                box->setPadding(12, 14, 12, 14);
            }
            box->setWidthPercentage(100);
            box->setCornerRadius(12.0f);
            box->setBackgroundColor(ui::color("desktop/surface"));
            box->setBorderThickness(1.0f);
            box->setBorderColor(ui::color("desktop/line"));
            box->setMarginBottom(10.0f);
        }
        for (brls::View* child : box->getChildren()) {
            harmonizeDockWidgetSubtree(child, false);
        }
    }
}

brls::Box* makeIconActionButton(SvgIconView** out_icon,
                                const std::string& svg_xml,
                                const char* bg_token,
                                const char* border_token,
                                std::function<void()> on_click) {
    auto* btn = new brls::Box(brls::Axis::ROW);
    btn->setDimensions(30.0f, 30.0f);
    btn->setCornerRadius(8.0f);
    btn->setHighlightCornerRadius(8.0f);
    btn->setFocusable(true);
    btn->setShrink(0.0f);
    btn->setAlignItems(brls::AlignItems::CENTER);
    btn->setJustifyContent(brls::JustifyContent::CENTER);
    btn->setBackgroundColor(ui::color(bg_token));
    btn->setBorderThickness(1.0f);
    btn->setBorderColor(ui::color(border_token));

    auto* icon = new SvgIconView();
    icon->setDimensions(14, 14);
    icon->setSvgString(svg_xml);
    btn->addView(icon);
    if (out_icon) *out_icon = icon;

    btn->registerClickAction([fn = std::move(on_click)](brls::View*) {
        if (fn) fn();
        return true;
    });
    return btn;
}

} // namespace

AiCompanionDock::AiCompanionDock(
    std::function<void()> on_open_marketplace,
    std::function<void()> on_open_ai_workspace,
    std::function<void(const std::string& session_id)> on_expand_session)
    : brls::Box(brls::Axis::COLUMN),
      on_open_marketplace_(std::move(on_open_marketplace)),
      on_open_ai_workspace_(std::move(on_open_ai_workspace)),
      on_expand_session_(std::move(on_expand_session)) {
    setVisibility(brls::Visibility::GONE);
    setBackgroundColor(ui::color("brls/background"));
    setBorderThickness(1.0f);
    setBorderColor(ui::color("desktop/line"));
    setPadding(12, 14, 12, 14);
    setWidth(432);

    // ========================================================================
    // 1. Minimal Unified Top Header (matches AiWorkbench stage header)
    // ========================================================================
    auto* header = new brls::Box(brls::Axis::ROW);
    header->setWidthPercentage(100);
    header->setShrink(0.0f);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setMarginBottom(8.0f);

    session_selector_btn_ = new brls::Box(brls::Axis::ROW);
    session_selector_btn_->setHeight(30.0f);
    session_selector_btn_->setPadding(0, 10, 0, 9);
    session_selector_btn_->setCornerRadius(9.0f);
    session_selector_btn_->setHighlightCornerRadius(9.0f);
    session_selector_btn_->setFocusable(true);
    session_selector_btn_->setShrink(1.0f);
    session_selector_btn_->setMinWidth(0);
    session_selector_btn_->setMarginRight(8.0f);
    session_selector_btn_->setAlignItems(brls::AlignItems::CENTER);
    session_selector_btn_->setBackgroundColor(ui::color("desktop/surface"));
    session_selector_btn_->setBorderThickness(1.0f);
    session_selector_btn_->setBorderColor(ui::color("desktop/line"));

    header_emblem_ = new SvgIconView();
    header_emblem_->setDimensions(16, 16);
    header_emblem_->setShrink(0.0f);
    header_emblem_->setMarginRight(7.0f);
    header_emblem_->setSvgString(svgAiEmblem(nvgHex(ui::color("desktop/focus_ring")),
                                             nvgHex(ui::color("brls/accent"))));
    session_selector_btn_->addView(header_emblem_);

    session_title_label_ = ui::makeLabel("新对话", 14.0f, "brls/text", 1.2f);
    session_title_label_->setSingleLine(true);
    session_title_label_->setShrink(1.0f);
    session_selector_btn_->addView(session_title_label_);

    session_chevron_ = new SvgIconView();
    session_chevron_->setDimensions(12, 12);
    session_chevron_->setShrink(0.0f);
    session_chevron_->setMarginLeft(6.0f);
    session_chevron_->setSvgString(svgChevronDown(nvgHex(ui::color("font/grey"))));
    session_selector_btn_->addView(session_chevron_);

    session_selector_btn_->registerClickAction([this, alive = alive_](brls::View*) {
        if (alive->load(std::memory_order_acquire)) {
            openSessionAndUtilityDialog();
        }
        return true;
    });
    header->addView(session_selector_btn_);

    auto* hdr_actions = new brls::Box(brls::Axis::ROW);
    hdr_actions->setAlignItems(brls::AlignItems::CENTER);
    hdr_actions->setShrink(0.0f);

    auto* new_btn = makeIconActionButton(
        nullptr,
        svgPlus(nvgHex(ui::color("brls/accent"))),
        "desktop/active",
        "brls/accent",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                switchTab(DockTab::Chat);
                startNewSession(false);
                focusComposer(true);
            }
        });
    new_btn->setMarginRight(5.0f);
    hdr_actions->addView(new_btn);

    auto* expand_btn = makeIconActionButton(
        nullptr,
        svgExpandStage(nvgHex(ui::color("brls/text"))),
        "desktop/surface",
        "desktop/line",
        [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (alive->load(std::memory_order_acquire)) {
                    expandToFullAiWorkspace();
                }
            });
        });
    expand_btn->setMarginRight(5.0f);
    hdr_actions->addView(expand_btn);

    auto* close_btn = makeIconActionButton(
        nullptr,
        svgDockCollapse(nvgHex(ui::color("font/grey"))),
        "desktop/surface",
        "desktop/line",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                toggleVisible();
            }
        });
    hdr_actions->addView(close_btn);

    header->addView(hdr_actions);
    addView(header);

    // ========================================================================
    // 2. Unified Subbar: [AI 伴读 | 组件 · N] + Calendar/Curation + Route Pill
    // ========================================================================
    auto* subbar = new brls::Box(brls::Axis::ROW);
    subbar->setWidthPercentage(100);
    subbar->setShrink(0.0f);
    subbar->setAlignItems(brls::AlignItems::CENTER);
    subbar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    subbar->setMarginBottom(8.0f);

    auto* left_controls = new brls::Box(brls::Axis::ROW);
    left_controls->setAlignItems(brls::AlignItems::CENTER);
    left_controls->setShrink(0.0f);

    auto* seg_group = new brls::Box(brls::Axis::ROW);
    seg_group->setPadding(2.0f, 2.0f, 2.0f, 2.0f);
    seg_group->setCornerRadius(14.0f);
    seg_group->setBackgroundColor(ui::color("desktop/surface"));
    seg_group->setBorderThickness(1.0f);
    seg_group->setBorderColor(ui::color("desktop/line"));
    seg_group->setAlignItems(brls::AlignItems::CENTER);

    tab_chat_btn_ = new brls::Box(brls::Axis::ROW);
    tab_chat_btn_->setHeight(24.0f);
    tab_chat_btn_->setPadding(0, 9, 0, 8);
    tab_chat_btn_->setCornerRadius(12.0f);
    tab_chat_btn_->setHighlightCornerRadius(12.0f);
    tab_chat_btn_->setHideHighlightBackground(true);
    tab_chat_btn_->setHideHighlightBorder(true);
    tab_chat_btn_->setFocusable(true);
    tab_chat_btn_->setAlignItems(brls::AlignItems::CENTER);
    tab_chat_icon_ = new SvgIconView();
    tab_chat_icon_->setDimensions(12, 12);
    tab_chat_icon_->setMarginRight(4.5f);
    tab_chat_btn_->addView(tab_chat_icon_);
    tab_chat_label_ = ui::makeLabel("AI 伴读", 12.2f, "desktop/focus_ring", 1.1f);
    tab_chat_label_->setSingleLine(true);
    tab_chat_btn_->addView(tab_chat_label_);
    tab_chat_btn_->registerClickAction([this, alive = alive_](brls::View*) {
        if (alive->load(std::memory_order_acquire)) {
            switchTab(DockTab::Chat);
        }
        return true;
    });
    seg_group->addView(tab_chat_btn_);

    tab_tools_btn_ = new brls::Box(brls::Axis::ROW);
    tab_tools_btn_->setHeight(24.0f);
    tab_tools_btn_->setPadding(0, 9, 0, 8);
    tab_tools_btn_->setMarginLeft(2.0f);
    tab_tools_btn_->setCornerRadius(12.0f);
    tab_tools_btn_->setHighlightCornerRadius(12.0f);
    tab_tools_btn_->setHideHighlightBackground(true);
    tab_tools_btn_->setHideHighlightBorder(true);
    tab_tools_btn_->setFocusable(true);
    tab_tools_btn_->setAlignItems(brls::AlignItems::CENTER);
    tab_tools_icon_ = new SvgIconView();
    tab_tools_icon_->setDimensions(12, 12);
    tab_tools_icon_->setMarginRight(4.5f);
    tab_tools_btn_->addView(tab_tools_icon_);
    tab_tools_label_ = ui::makeLabel("组件", 12.2f, "font/grey", 1.1f);
    tab_tools_label_->setSingleLine(true);
    tab_tools_btn_->addView(tab_tools_label_);
    tab_tools_btn_->registerClickAction([this, alive = alive_](brls::View*) {
        if (alive->load(std::memory_order_acquire)) {
            switchTab(DockTab::Tools);
        }
        return true;
    });
    seg_group->addView(tab_tools_btn_);
    left_controls->addView(seg_group);

    auto* cal_btn = makeIconActionButton(
        nullptr,
        svgCalendarQuick(nvgHex(ui::color("font/grey"))),
        "desktop/surface",
        "desktop/line",
        [alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) {
                CurationCalendarDialog::open({
                    .on_open_scheduler = [alive]() {
                        if (alive->load(std::memory_order_acquire)) {
                            CurationSchedulerDialog::open([]() { CurationCalendarDialog::open(); });
                        }
                    },
                });
            }
        });
    cal_btn->setDimensions(28.0f, 28.0f);
    cal_btn->setCornerRadius(14.0f);
    cal_btn->setHighlightCornerRadius(14.0f);
    cal_btn->setMarginLeft(6.0f);
    left_controls->addView(cal_btn);

    subbar->addView(left_controls);

    route_chip_ = new brls::Box(brls::Axis::ROW);
    route_chip_->setHeight(28.0f);
    route_chip_->setPadding(0, 10, 0, 9);
    route_chip_->setMarginLeft(6.0f);
    route_chip_->setCornerRadius(14.0f);
    route_chip_->setHighlightCornerRadius(14.0f);
    route_chip_->setFocusable(true);
    route_chip_->setShrink(1.0f);
    route_chip_->setMinWidth(0);
    route_chip_->setAlignItems(brls::AlignItems::CENTER);
    route_chip_->setBackgroundColor(ui::color("desktop/surface"));
    route_chip_->setBorderThickness(1.0f);
    route_chip_->setBorderColor(ui::color("desktop/line"));

    route_dot_ = new brls::Box(brls::Axis::ROW);
    route_dot_->setDimensions(6.0f, 6.0f);
    route_dot_->setCornerRadius(3.0f);
    route_dot_->setShrink(0.0f);
    route_dot_->setMarginRight(6.0f);
    route_dot_->setBackgroundColor(ui::color("font/grey"));
    route_chip_->addView(route_dot_);

    route_badge_label_ = ui::makeLabel("路由检测中...", 12.0f, "font/grey", 1.15f);
    route_badge_label_->setSingleLine(true);
    route_badge_label_->setShrink(1.0f);
    route_chip_->addView(route_badge_label_);

    route_chip_->registerClickAction([this, alive = alive_](brls::View*) {
        if (alive->load(std::memory_order_acquire)) {
            openModelPickerDialog();
        }
        return true;
    });
    subbar->addView(route_chip_);
    addView(subbar);

    // ========================================================================
    // 3A. Empty-Session Hero + Domain Starter Cards (Scrollable, zero clipping)
    // ========================================================================
    empty_scroll_ = new brls::ScrollingFrame();
    empty_scroll_->setGrow(1.0f);
    empty_scroll_->setShrink(1.0f);
    empty_scroll_->setMinHeight(0);
    empty_scroll_->setWidthPercentage(100);

    empty_inner_ = new brls::Box(brls::Axis::COLUMN);
    empty_inner_->setWidthPercentage(100);
    empty_inner_->setPadding(14, 2, 10, 2);

    auto* hero_box = new brls::Box(brls::Axis::COLUMN);
    hero_box->setWidthPercentage(100);
    hero_box->setAlignItems(brls::AlignItems::CENTER);
    hero_box->setMarginBottom(14.0f);

    auto* hero_emblem_wrap = new brls::Box(brls::Axis::ROW);
    hero_emblem_wrap->setDimensions(42.0f, 42.0f);
    hero_emblem_wrap->setCornerRadius(21.0f);
    hero_emblem_wrap->setAlignItems(brls::AlignItems::CENTER);
    hero_emblem_wrap->setJustifyContent(brls::JustifyContent::CENTER);
    hero_emblem_wrap->setBackgroundColor(ui::color("status/info_bg"));
    hero_emblem_wrap->setMarginBottom(8.0f);
    auto* hero_icon = new SvgIconView();
    hero_icon->setDimensions(22, 22);
    hero_icon->setSvgString(svgAiEmblem(nvgHex(ui::color("desktop/focus_ring")),
                                        nvgHex(ui::color("brls/accent"))));
    hero_emblem_wrap->addView(hero_icon);
    hero_box->addView(hero_emblem_wrap);

    auto* hero_title = ui::makeLabel("今天想伴读什么，或策展灵感？", 16.8f, "brls/text");
    hero_title->setMarginBottom(6.0f);
    hero_box->addView(hero_title);

    auto* dom_pill = new brls::Box(brls::Axis::ROW);
    dom_pill->setPadding(3.5f, 10.0f, 3.5f, 10.0f);
    dom_pill->setCornerRadius(11.0f);
    dom_pill->setBackgroundColor(ui::color("desktop/surface"));
    dom_pill->setBorderThickness(1.0f);
    dom_pill->setBorderColor(ui::color("desktop/line"));
    dom_pill->setAlignItems(brls::AlignItems::CENTER);

    hero_domain_dot_ = new brls::Box(brls::Axis::ROW);
    hero_domain_dot_->setDimensions(6.0f, 6.0f);
    hero_domain_dot_->setCornerRadius(3.0f);
    hero_domain_dot_->setShrink(0.0f);
    hero_domain_dot_->setMarginRight(6.0f);
    hero_domain_dot_->setBackgroundColor(ui::color("domain/core"));
    dom_pill->addView(hero_domain_dot_);

    hero_domain_label_ = ui::makeLabel("当前域 · 底座与插件", 12.0f, "font/grey", 1.15f);
    hero_domain_label_->setSingleLine(true);
    dom_pill->addView(hero_domain_label_);
    hero_box->addView(dom_pill);

    empty_inner_->addView(hero_box);

    starter_cards_box_ = new brls::Box(brls::Axis::COLUMN);
    starter_cards_box_->setWidthPercentage(100);
    empty_inner_->addView(starter_cards_box_);

    empty_scroll_->setContentView(empty_inner_);
    addView(empty_scroll_);

    // ========================================================================
    // 3B. Active Conversation Thread (DeepSeek message layout matching AiWorkbench)
    // ========================================================================
    thread_scroll_ = new brls::ScrollingFrame();
    thread_scroll_->setGrow(1.0f);
    thread_scroll_->setShrink(1.0f);
    thread_scroll_->setMinHeight(0);
    thread_scroll_->setWidthPercentage(100);
    thread_scroll_->setVisibility(brls::Visibility::GONE);

    messages_box_ = new brls::Box(brls::Axis::COLUMN);
    messages_box_->setWidthPercentage(100);
    messages_box_->setPadding(4, 2, 10, 2);
    thread_scroll_->setContentView(messages_box_);
    addView(thread_scroll_);

    // ========================================================================
    // 3C. Dedicated Scrollable & Harmonized Scene Companion Tools View
    // ========================================================================
    tools_scroll_ = new brls::ScrollingFrame();
    tools_scroll_->setGrow(1.0f);
    tools_scroll_->setShrink(1.0f);
    tools_scroll_->setMinHeight(0);
    tools_scroll_->setWidthPercentage(100);
    tools_scroll_->setVisibility(brls::Visibility::GONE);

    tools_inner_ = new brls::Box(brls::Axis::COLUMN);
    tools_inner_->setWidthPercentage(100);
    tools_inner_->setPadding(4, 2, 12, 2);
    tools_scroll_->setContentView(tools_inner_);
    addView(tools_scroll_);

    // ========================================================================
    // 4. Compact Domain Quick-Prompt Strip (shown above composer in active chat)
    // ========================================================================
    quick_chips_row_ = new brls::Box(brls::Axis::ROW);
    quick_chips_row_->setWidthPercentage(100);
    quick_chips_row_->setShrink(0.0f);
    quick_chips_row_->setMarginTop(6.0f);
    quick_chips_row_->setVisibility(brls::Visibility::GONE);
    addView(quick_chips_row_);

    // ========================================================================
    // 5. Persistent Inline DeepSeekComposer (shared DNA with AiWorkbench)
    // ========================================================================
    composer_ = new DeepSeekComposer({.compact = true, .show_model_pill = false});
    composer_->setShrink(0.0f);
    composer_->setMarginTop(6.0f);
    composer_->setSyncPlaylist(sync_playlist_enabled_);
    composer_->setOnSubmit([this, alive = alive_](const DeepSeekComposerSubmission& sub) {
        if (alive->load(std::memory_order_acquire)) {
            handleComposerSubmit(sub);
        }
    });
    composer_->setOnCancelStream([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            cancelActiveStream();
        }
    });
    composer_->setOnModelPillClick([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            openModelPickerDialog();
        }
    });
    composer_->setOnStateChanged([this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire) || !composer_) return;
        sync_playlist_enabled_ = composer_->isSyncPlaylist();
        auto& sess = currentSession();
        sess.curation_mode = composer_->isCurationMode();
        sess.deep_thinking = composer_->isDeepThinking();
    });
    addView(composer_);

    loadSessionsFromDisk();
    rebuildHeroAndStarters();
    rebuildQuickPromptChips();
    rebuildThreadViews();
    updateHeaderAndTabsStyling();
    updateStageVisibility();

    if (HostKernel::instance().isInitialized()) {
        ephemeral_note_sub_ = HostKernel::instance().bus().subscribe(
            "entity.recommend.ephemeral_note",
            "host.companion_dock",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                brls::sync([this, alive, payload]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    std::string note;
                    if (payload.is_object()) {
                        note = payload.value(
                            "note",
                            payload.value("ephemeral_chat_note",
                                          payload.value("message", payload.value("text", ""))));
                    } else if (payload.is_string()) {
                        note = payload.get<std::string>();
                    }
                    if (!note.empty() && !sessions_.empty()) {
                        auto& sess = currentSession();
                        AiChatMessage m;
                        m.role = "system";
                        m.display_role = "瞬时伴读提示 (不入库)";
                        m.content = note;
                        sess.messages.push_back(std::move(m));
                        if (!active_cancel_) {
                            appendMessageTurnView(sess.messages.size() - 1);
                            updateStageVisibility();
                            scrollToBottom(false);
                        }
                    }
                });
            });

        sessions_sync_sub_ = HostKernel::instance().bus().subscribe(
            "entity.ai.sessions_updated",
            "host.companion_dock.sync",
            [this, alive = alive_](const std::string&, const nlohmann::json& payload) {
                if (!payload.is_object() || payload.value("origin", "") == "dock") {
                    return;
                }
                const std::string target_id = payload.value("active_session_id", "");
                brls::sync([this, alive, target_id]() {
                    if (!alive->load(std::memory_order_acquire) || active_cancel_) return;
                    const std::string keep_id = target_id.empty() ? activeSessionId() : target_id;
                    sessions_ = loadAiChatSessionsFromDisk();
                    active_session_idx_ = 0;
                    for (size_t i = 0; i < sessions_.size(); ++i) {
                        if (sessions_[i].id == keep_id) {
                            active_session_idx_ = i;
                            break;
                        }
                    }
                    if (composer_) {
                        composer_->setCurationMode(currentSession().curation_mode);
                        composer_->setDeepThinking(currentSession().deep_thinking);
                    }
                    rebuildThreadViews();
                    updateHeaderAndTabsStyling();
                    updateStageVisibility();
                    scrollToBottom(true);
                });
            });
    }
}

AiCompanionDock::~AiCompanionDock() {
    alive_->store(false, std::memory_order_release);
    if (HostKernel::instance().isInitialized()) {
        if (ephemeral_note_sub_ != 0) {
            HostKernel::instance().bus().unsubscribe(ephemeral_note_sub_);
            ephemeral_note_sub_ = 0;
        }
        if (sessions_sync_sub_ != 0) {
            HostKernel::instance().bus().unsubscribe(sessions_sync_sub_);
            sessions_sync_sub_ = 0;
        }
    }
    if (route_cancel_) {
        route_cancel_->cancel();
        route_cancel_.reset();
    }
    if (active_cancel_) {
        active_cancel_->cancel();
        active_cancel_.reset();
    }
}

brls::View* AiCompanionDock::getDefaultFocus() {
    if (active_tab_ == DockTab::Chat && composer_ && composer_->editor()) {
        return composer_->editor();
    }
    if (active_tab_ == DockTab::Tools && tools_scroll_) {
        if (brls::View* f = tools_scroll_->getDefaultFocus()) {
            return f;
        }
    }
    return brls::Box::getDefaultFocus();
}

void AiCompanionDock::focusComposer(bool start_editing) {
    if (composer_) {
        composer_->focusInput(start_editing);
    }
}

void AiCompanionDock::toggleVisible() {
    open_ = !open_;
    if (!open_) {
        InlineEditorRegistry::blurActiveEditor();
    }
    setVisibility(open_ ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    if (open_) {
        if (route_dirty_) {
            refreshAiRouteStatus();
        }
        if (starters_dirty_) {
            rebuildHeroAndStarters();
            rebuildQuickPromptChips();
        }
        if (tools_dirty_) {
            if (active_tab_ == DockTab::Tools) {
                rebuildPluginToolsView();
            } else {
                // Count domain tools lazily without instantiating heavy plugin views until Tools tab is opened
                auto& kernel = HostKernel::instance();
                if (kernel.isInitialized()) {
                    const auto specs = kernel.domains().dockWidgets(domain_);
                    total_tool_count_ = specs.size();
                    primary_tool_count_ = 0;
                    for (const auto& s : specs) {
                        if (isWidgetPrimaryForDomain(s, domain_)) ++primary_tool_count_;
                    }
                    updateHeaderAndTabsStyling();
                }
            }
        }
    }
    invalidate();
}

void AiCompanionDock::applyResponsiveMode(float window_width, float window_height) {
    if (window_height <= 1.0f) {
        return;
    }
    const bool narrow = (window_width / window_height) < 1.45f;
    if (narrow == bottom_mode_) {
        return;
    }
    bottom_mode_ = narrow;
    if (bottom_mode_) {
        setWidthPercentage(100);
        setHeight(360);
    } else {
        setHeightPercentage(100);
        setWidth(432);
    }
}

void AiCompanionDock::switchTab(DockTab tab) {
    if (active_tab_ == tab) {
        return;
    }
    InlineEditorRegistry::blurActiveEditor();
    active_tab_ = tab;
    if (active_tab_ == DockTab::Tools && tools_dirty_) {
        rebuildPluginToolsView();
    }
    updateHeaderAndTabsStyling();
    updateStageVisibility();
    invalidate();
}

void AiCompanionDock::onDomainsChanged() {
    tools_dirty_ = true;
    route_dirty_ = true;
    if (!open_) {
        return;
    }
    refreshAiRouteStatus();
    if (active_tab_ == DockTab::Tools) {
        rebuildPluginToolsView();
    } else {
        auto& kernel = HostKernel::instance();
        if (kernel.isInitialized()) {
            const auto specs = kernel.domains().dockWidgets(domain_);
            total_tool_count_ = specs.size();
            primary_tool_count_ = 0;
            for (const auto& s : specs) {
                if (isWidgetPrimaryForDomain(s, domain_)) ++primary_tool_count_;
            }
        }
    }
    updateHeaderAndTabsStyling();
}

void AiCompanionDock::setDomainContext(ContentDomain domain, const std::string& entity_summary) {
    const bool domain_changed = (domain != domain_);
    domain_ = domain;
    entity_summary_ = entity_summary;

    if (!sessions_.empty() && currentSession().messages.empty()) {
        currentSession().domain = domain;
        currentSession().context_summary = entity_summary;
    }

    if (composer_) {
        if (!entity_summary_.empty()) {
            composer_->setContextBadge(
                toDisplayLabel(domain_) + " · " + clipUtf8Codepoints(entity_summary_, 24),
                [this, alive = alive_]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    setDomainContext(domain_, "");
                });
        } else {
            composer_->setContextBadge("");
        }
    }

    if (domain_changed) {
        tools_dirty_ = true;
        other_tools_expanded_ = false;
    }
    starters_dirty_ = true;

    if (!open_) {
        return;
    }

    if (route_dirty_) {
        refreshAiRouteStatus();
    }
    rebuildHeroAndStarters();
    rebuildQuickPromptChips();
    if (domain_changed) {
        if (active_tab_ == DockTab::Tools) {
            rebuildPluginToolsView();
        } else if (HostKernel::instance().isInitialized()) {
            const auto specs = HostKernel::instance().domains().dockWidgets(domain_);
            total_tool_count_ = specs.size();
            primary_tool_count_ = 0;
            for (const auto& s : specs) {
                if (isWidgetPrimaryForDomain(s, domain_)) ++primary_tool_count_;
            }
        }
    }
    updateHeaderAndTabsStyling();
    updateStageVisibility();
}

void AiCompanionDock::loadSessionsFromDisk() {
    sessions_ = loadAiChatSessionsFromDisk();
    active_session_idx_ = 0;
}

void AiCompanionDock::saveSessionsToDisk() const {
    saveAiChatSessionsToDisk(sessions_);
    notifyAiSessionsUpdated("dock", activeSessionId());
}

AiChatSession& AiCompanionDock::currentSession() {
    if (sessions_.empty()) {
        AiChatSession fresh;
        fresh.id = makeAiChatSessionId();
        fresh.domain = domain_;
        fresh.context_summary = entity_summary_;
        fresh.updated_at = nowUnixSec();
        sessions_.push_back(std::move(fresh));
        active_session_idx_ = 0;
    }
    if (active_session_idx_ >= sessions_.size()) {
        active_session_idx_ = 0;
    }
    return sessions_[active_session_idx_];
}

const AiChatSession& AiCompanionDock::currentSession() const {
    return sessions_[std::min(active_session_idx_, sessions_.size() - 1)];
}

std::string AiCompanionDock::activeSessionId() const {
    if (sessions_.empty()) return "";
    return currentSession().id;
}

void AiCompanionDock::selectSessionById(const std::string& session_id) {
    if (session_id.empty()) return;
    if (!active_cancel_) {
        sessions_ = loadAiChatSessionsFromDisk();
    }
    for (size_t i = 0; i < sessions_.size(); ++i) {
        if (sessions_[i].id == session_id) {
            selectSession(i);
            return;
        }
    }
}

void AiCompanionDock::startNewSession(bool curation_mode) {
    cancelActiveStream();
    if (!sessions_.empty() && sessions_.front().messages.empty()) {
        active_session_idx_ = 0;
        sessions_.front().curation_mode = curation_mode;
        sessions_.front().domain = domain_;
        sessions_.front().context_summary = entity_summary_;
    } else {
        AiChatSession fresh;
        fresh.id = makeAiChatSessionId();
        fresh.title = curation_mode ? "新灵感策展" : "新对话";
        fresh.domain = domain_;
        fresh.context_summary = entity_summary_;
        fresh.curation_mode = curation_mode;
        fresh.updated_at = nowUnixSec();
        sessions_.insert(sessions_.begin(), std::move(fresh));
        active_session_idx_ = 0;
    }

    if (composer_) {
        composer_->setCurationMode(currentSession().curation_mode);
        composer_->setDeepThinking(currentSession().deep_thinking);
    }
    saveSessionsToDisk();
    rebuildThreadViews();
    updateHeaderAndTabsStyling();
    updateStageVisibility();
    if (empty_scroll_) {
        empty_scroll_->setContentOffsetY(0.0f, false);
    }
}

void AiCompanionDock::clearConversation() {
    startNewSession(false);
}

void AiCompanionDock::selectSession(size_t index) {
    if (index >= sessions_.size()) return;
    if (active_cancel_ && streaming_session_idx_ != index) {
        cancelActiveStream();
    }
    active_session_idx_ = index;
    const auto& sess = currentSession();
    if (composer_) {
        composer_->setCurationMode(sess.curation_mode);
        composer_->setDeepThinking(sess.deep_thinking);
    }
    switchTab(DockTab::Chat);
    rebuildThreadViews();
    updateHeaderAndTabsStyling();
    updateStageVisibility();
    scrollToBottom(true);
}

void AiCompanionDock::deleteSession(size_t index) {
    if (index >= sessions_.size()) return;
    if (active_cancel_ && streaming_session_idx_ == index) {
        cancelActiveStream();
    } else if (active_cancel_ && index < streaming_session_idx_) {
        --streaming_session_idx_;
    }
    sessions_.erase(sessions_.begin() + static_cast<std::ptrdiff_t>(index));
    if (sessions_.empty()) {
        AiChatSession fresh;
        fresh.id = makeAiChatSessionId();
        fresh.domain = domain_;
        fresh.context_summary = entity_summary_;
        fresh.updated_at = nowUnixSec();
        sessions_.push_back(std::move(fresh));
        active_session_idx_ = 0;
    } else if (active_session_idx_ >= sessions_.size()) {
        active_session_idx_ = sessions_.size() - 1;
    } else if (index < active_session_idx_) {
        --active_session_idx_;
    }
    saveSessionsToDisk();
    rebuildThreadViews();
    updateHeaderAndTabsStyling();
    updateStageVisibility();
}

void AiCompanionDock::expandToFullAiWorkspace() {
    if (active_cancel_) {
        cancelActiveStream();
    } else {
        saveSessionsToDisk();
    }
    const std::string sid = activeSessionId();
    if (on_expand_session_) {
        on_expand_session_(sid);
    } else if (on_open_ai_workspace_) {
        on_open_ai_workspace_();
    }
}

void AiCompanionDock::updateHeaderAndTabsStyling() {
    const auto& sess = currentSession();
    if (session_title_label_) {
        session_title_label_->setText(
            clipUtf8Codepoints(sess.title.empty() ? "新对话" : sess.title, 15));
    }

    const bool chat_active = (active_tab_ == DockTab::Chat);
    if (tab_chat_btn_ && tab_chat_label_ && tab_chat_icon_) {
        const NVGcolor fg = ui::color(chat_active ? "desktop/focus_ring" : "font/grey");
        tab_chat_btn_->setBackgroundColor(
            chat_active ? ui::color("status/info_bg") : nvgRGBA(0, 0, 0, 0));
        tab_chat_label_->setTextColor(fg);
        tab_chat_icon_->setSvgString(svgSparkleTab(nvgHex(fg)));
    }

    if (tab_tools_btn_ && tab_tools_label_ && tab_tools_icon_) {
        const bool tools_active = (active_tab_ == DockTab::Tools);
        const NVGcolor fg = ui::color(tools_active ? "brls/accent" : "font/grey");
        tab_tools_btn_->setBackgroundColor(
            tools_active ? ui::color("desktop/active") : nvgRGBA(0, 0, 0, 0));
        const size_t badge_n = primary_tool_count_ > 0 ? primary_tool_count_ : total_tool_count_;
        tab_tools_label_->setText(
            badge_n > 0 ? ("组件 · " + std::to_string(badge_n)) : "组件");
        tab_tools_label_->setTextColor(fg);
        tab_tools_icon_->setSvgString(svgWidgetsGrid(nvgHex(fg)));
    }
}

void AiCompanionDock::updateStageVisibility() {
    const bool is_chat = (active_tab_ == DockTab::Chat);
    const bool is_empty = currentSession().messages.empty() && !active_cancel_;

    if (empty_scroll_) {
        empty_scroll_->setVisibility(
            (is_chat && is_empty) ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (thread_scroll_) {
        thread_scroll_->setVisibility(
            (is_chat && !is_empty) ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (quick_chips_row_) {
        quick_chips_row_->setVisibility(
            (is_chat && !is_empty) ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (composer_) {
        composer_->setVisibility(is_chat ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
    if (tools_scroll_) {
        tools_scroll_->setVisibility(!is_chat ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
    }
}

void AiCompanionDock::rebuildHeroAndStarters() {
    starters_dirty_ = false;
    if (hero_domain_dot_) {
        hero_domain_dot_->setBackgroundColor(ui::color(domainColorToken(domain_)));
    }
    if (hero_domain_label_) {
        std::string txt = "当前域 · " + toDisplayLabel(domain_);
        if (!entity_summary_.empty()) {
            txt += " · " + clipUtf8Codepoints(entity_summary_, 16);
        }
        hero_domain_label_->setText(txt);
    }

    if (!starter_cards_box_) return;
    if (focusIsInsideView(starter_cards_box_)) {
        brls::Application::giveFocus(nullptr);
    }
    starter_cards_box_->clearViews();

    struct StarterItem {
        const char* title;
        const char* subtitle;
        const char* prompt;
        bool curation = false;
        bool deep_think = false;
        bool immediate_send = false;
    };

    std::vector<StarterItem> items;
    switch (domain_) {
        case ContentDomain::Media:
            items = {
                {"章节时间轴与核心看点速览",
                 "基于当前视频 / 播客上下文生成结构化时间轴摘要与核心结论",
                 "请基于当前影音上下文生成时间轴章节摘要与核心看点。",
                 false, false, true},
                {"双语歌词意象与语法精讲",
                 "拆解当前播放曲目的修辞意象、文化背景与双语对照意译",
                 "请分析当前曲目歌词的意象、语法结构并给出双语意译。",
                 false, false, true},
                {"AI 灵感策展 (反写工作台 + 歌单)",
                 "例如：雨夜写 C++ 来 5 首耐听器乐，直接同步 Apple Music 歌单",
                 "雨夜写 C++ 来 5 首耐听器乐，以前推过的也可",
                 true, false, false},
                {"匹配飞牛 NAS 片源与同风格延伸",
                 "检索本地 NAS 片库或生成相关导演 / 专辑深度片单",
                 "请匹配飞牛 NAS 本地片库或相关导演/专辑推荐。",
                 false, false, true},
            };
            break;
        case ContentDomain::Text:
            items = {
                {"提炼技术长文结论与代码要点",
                 "归纳当前文章 / 帖子的核心论据、架构设计与关键实现细节",
                 "请提炼当前技术长文或帖子的核心结论、论据与代码要点。",
                 false, false, true},
                {"汇总讨论区共识与分歧焦点",
                 "梳理高赞回复的核心共识、争议边界与延伸阅读线索",
                 "请汇总当前话题下的核心共识、分歧点与延伸阅读方向。",
                 false, false, true},
                {"生成学术期刊研究摘要卡片",
                 "开启深度思考，提炼研究问题、方法论、实验指标与局限性",
                 "请生成结构化研究问题、方法论与创新点摘要卡片。",
                 false, true, true},
                {"AI 灵感策展 (主题好文检索反写)",
                 "描述感兴趣的技术或人文议题，实时策展并写回文字与期刊工作台",
                 "策展近期值得精读的系统架构与编译器优化深度长文",
                 true, false, false},
            };
            break;
        case ContentDomain::Book:
            items = {
                {"检索并下载书籍到书架",
                 "输入书名 / 作者 / ISBN，跨源检索 EPUB / PDF 自动入架",
                 "请帮我检索并下载书籍到书架（优先 EPUB/PDF，下载完成后刷新书架）：",
                 false, false, false},
                {"长难句语法拆解与词根助记",
                 "拆解当前阅读段落的从句结构、学术句式与核心词根记忆提示",
                 "请拆解当前段落的长难句语法结构，并给出核心词根记忆提示。",
                 false, false, true},
                {"提取高价值生词与双语例句卡",
                 "从当前章节提取关键学术 / 文学词汇并生成释义与语境例句",
                 "请提取当前阅读上下文中的高价值学术/文学词汇并生成释义与例句。",
                 false, false, true},
                {"AI 灵感策展 (主题书单推荐)",
                 "围绕研究方向策展必读书目并一键反写书本与 PDF 工作台",
                 "推荐 4 本现代操作系统与分布式存储经典必读书籍",
                 true, false, false},
            };
            break;
        case ContentDomain::Core:
            items = {
                {"AI 灵感策展 (反写工作台 + 歌单)",
                 "例如：雨夜写 C++ 来 5 首耐听器乐，以前推过的也可",
                 "雨夜写 C++ 来 5 首耐听器乐，以前推过的也可",
                 true, false, false},
                {"检索并下载书籍到书架",
                 "直接补充书名 / 作者 / ISBN，自动回退 Z-Library 与 Anna's Archive",
                 "请帮我检索并下载书籍到书架（优先 EPUB/PDF，下载完成后刷新书架）：",
                 false, false, false},
                {"无限长文 / 代码粘贴与深度思考",
                 "开启深度思考模式，粘贴论文段落或长代码进行严谨推演剖析",
                 "请深入分析以下内容的核心架构、关键细节与潜在边界问题：\n\n",
                 false, true, false},
                {"诊断本机 Magpie 路由与配额余量",
                 "即时读取订阅窗口余量、激活分组与已装载插件能力健康拓扑",
                 "请读取本机 Magpie 可用路由与配额余量，并诊断当前已装载插件状态。",
                 false, false, true},
            };
            break;
    }

    for (size_t i = 0; i < items.size(); ++i) {
        const auto sp = items[i];
        auto* card = ui::makeCard("desktop/surface", 11.0f, 12.0f);
        card->setFocusable(true);
        if (i + 1 < items.size()) {
            card->setMarginBottom(8.0f);
        }

        auto* t = ui::makeLabel(sp.title, 13.8f, sp.curation ? "brls/accent" : "brls/text");
        t->setSingleLine(true);
        t->setMarginBottom(3.0f);
        card->addView(t);

        auto* s = ui::makeLabel(sp.subtitle, 12.2f, "font/grey", 1.35f);
        card->addView(s);

        card->registerClickAction([this, alive = alive_, sp](brls::View*) {
            if (!alive->load(std::memory_order_acquire) || !composer_) return true;
            composer_->setCurationMode(sp.curation);
            composer_->setDeepThinking(sp.deep_think);
            currentSession().curation_mode = sp.curation;
            currentSession().deep_thinking = sp.deep_think;
            if (sp.immediate_send) {
                DeepSeekComposerSubmission sub;
                sub.prompt = sp.prompt;
                sub.curation_mode = sp.curation;
                sub.deep_thinking = sp.deep_think;
                sub.sync_playlist = composer_->isSyncPlaylist();
                handleComposerSubmit(sub);
            } else {
                composer_->setDraftText(sp.prompt, true);
            }
            return true;
        });
        starter_cards_box_->addView(card);
    }
}

void AiCompanionDock::rebuildQuickPromptChips() {
    if (!quick_chips_row_) return;
    if (focusIsInsideView(quick_chips_row_)) {
        brls::Application::giveFocus(nullptr);
    }
    quick_chips_row_->clearViews();

    struct QuickChip {
        const char* label;
        const char* prompt;
        bool prefill_only = false;
    };
    std::vector<QuickChip> chips;
    switch (domain_) {
        case ContentDomain::Media:
            chips = {
                {"章节摘要", "请基于当前影音上下文生成时间轴章节摘要与核心看点。", false},
                {"歌词意译", "请分析当前曲目歌词的意象、语法结构并给出双语意译。", false},
                {"关联片源", "请匹配飞牛 NAS 本地片库或相关导演/专辑推荐。", false},
            };
            break;
        case ContentDomain::Text:
            chips = {
                {"提炼结论", "请提炼当前技术长文或帖子的核心结论、论据与代码要点。", false},
                {"评论共识", "请汇总当前话题下的核心共识、分歧点与延伸阅读方向。", false},
                {"期刊卡片", "请生成结构化研究问题、方法论与创新点摘要卡片。", false},
            };
            break;
        case ContentDomain::Book:
            chips = {
                {"找书入架", "请帮我检索并下载书籍到书架（优先 EPUB/PDF，下载完成后刷新书架）：", true},
                {"长难句拆解", "请拆解当前段落的长难句语法结构，并给出核心词根记忆提示。", false},
                {"提取生词卡", "请提取当前阅读上下文中的高价值学术/文学词汇并生成释义与例句。", false},
            };
            break;
        case ContentDomain::Core:
            chips = {
                {"诊断拓扑", "请诊断当前 OmniDesk 已装载插件能力与依赖健康状态。", false},
                {"路由配额", "请读取本机 Magpie 可用模型路由、分组与当前配额余量。", false},
                {"展开全屏", "", true},
            };
            break;
    }

    for (size_t i = 0; i < chips.size(); ++i) {
        const std::string lbl = chips[i].label;
        const std::string pr = chips[i].prompt;
        const bool prefill = chips[i].prefill_only;
        auto* btn = ui::makeButton(
            lbl,
            [this, alive = alive_, pr, prefill]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (pr.empty()) {
                    expandToFullAiWorkspace();
                    return;
                }
                if (prefill) {
                    if (composer_) {
                        composer_->setCurationMode(false);
                        composer_->setDraftText(pr, true);
                    }
                    return;
                }
                submitPrompt(pr, composer_ ? composer_->isDeepThinking() : false);
            },
            false,
            26.0f);
        btn->setGrow(1.0f);
        if (i + 1 < chips.size()) {
            btn->setMarginRight(5.0f);
        }
        quick_chips_row_->addView(btn);
    }
}

void AiCompanionDock::rebuildPluginToolsView() {
    tools_dirty_ = false;
    if (!tools_inner_) return;
    if (focusIsInsideView(tools_inner_)) {
        brls::Application::giveFocus(nullptr);
    }
    tools_inner_->clearViews();

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        return;
    }

    const auto all_specs = kernel.domains().dockWidgets(domain_);
    total_tool_count_ = all_specs.size();
    std::vector<DockWidgetSpec> primary_specs;
    std::vector<DockWidgetSpec> secondary_specs;
    for (const auto& sp : all_specs) {
        if (isWidgetPrimaryForDomain(sp, domain_)) {
            primary_specs.push_back(sp);
        } else {
            secondary_specs.push_back(sp);
        }
    }
    primary_tool_count_ = primary_specs.size();

    if (all_specs.empty()) {
        auto* empty_card = ui::makeCard("desktop/surface", 16.0f, 12.0f);
        empty_card->addView(ui::makeLabel("当前未装载伴读小组件", 14.8f, "brls/text"));
        auto* sub = ui::makeLabel(
            "可在插件市场安装 Apple Music、B站、Anna's Archive、Chrome 伴读桥或 Magpie 路由插件。",
            12.8f,
            "font/grey",
            1.45f);
        sub->setMarginTop(5.0f);
        empty_card->addView(sub);
        if (on_open_marketplace_) {
            auto* act = new brls::Box(brls::Axis::ROW);
            act->setMarginTop(10.0f);
            act->addView(ui::makeButton("前往插件市场", [this, alive = alive_]() {
                if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
                    on_open_marketplace_();
                }
            }, false, 30.0f));
            empty_card->addView(act);
        }
        tools_inner_->addView(empty_card);
        updateHeaderAndTabsStyling();
        return;
    }

    auto mountSpecList = [](brls::Box* container, const std::vector<DockWidgetSpec>& list) {
        for (const auto& w : list) {
            if (!w.create_view) continue;
            brls::View* v = w.create_view();
            if (!v) continue;
            if (auto* as_box = dynamic_cast<brls::Box*>(v)) {
                harmonizeDockWidgetSubtree(as_box, true);
                container->addView(as_box);
            } else {
                auto* wrapper = ui::makeCard("desktop/surface", 12.0f, 12.0f);
                wrapper->setMarginBottom(10.0f);
                wrapper->addView(v);
                container->addView(wrapper);
            }
        }
    };

    const bool has_primary = !primary_specs.empty();
    if (has_primary) {
        auto* sec_hdr = ui::makeLabel(
            "当前场景组件 · " + toDisplayLabel(domain_), 12.2f, "font/grey");
        sec_hdr->setMarginBottom(6.0f);
        sec_hdr->setMarginLeft(2.0f);
        tools_inner_->addView(sec_hdr);
        mountSpecList(tools_inner_, primary_specs);
    }

    if (!secondary_specs.empty()) {
        if (!has_primary) {
            auto* sec_hdr = ui::makeLabel("已装载伴读组件", 12.2f, "font/grey");
            sec_hdr->setMarginBottom(6.0f);
            sec_hdr->setMarginLeft(2.0f);
            tools_inner_->addView(sec_hdr);
            mountSpecList(tools_inner_, secondary_specs);
        } else {
            auto* fold_hdr = new brls::Box(brls::Axis::ROW);
            fold_hdr->setWidthPercentage(100);
            fold_hdr->setHeight(32.0f);
            fold_hdr->setPadding(0, 12, 0, 12);
            fold_hdr->setMarginTop(2.0f);
            fold_hdr->setMarginBottom(8.0f);
            fold_hdr->setCornerRadius(9.0f);
            fold_hdr->setHighlightCornerRadius(9.0f);
            fold_hdr->setFocusable(true);
            fold_hdr->setAlignItems(brls::AlignItems::CENTER);
            fold_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            fold_hdr->setBackgroundColor(ui::color("desktop/surface"));
            fold_hdr->setBorderThickness(1.0f);
            fold_hdr->setBorderColor(ui::color("desktop/line"));

            const std::string base_txt =
                "其他跨场景组件 (" + std::to_string(secondary_specs.size()) + ")";
            auto* fold_lbl = ui::makeLabel(
                (other_tools_expanded_ ? "▾ " : "▸ ") + base_txt, 12.5f, "font/grey", 1.2f);
            fold_hdr->addView(fold_lbl);

            auto* other_box = new brls::Box(brls::Axis::COLUMN);
            other_box->setWidthPercentage(100);
            other_box->setVisibility(
                other_tools_expanded_ ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
            mountSpecList(other_box, secondary_specs);

            fold_hdr->registerClickAction([this, alive = alive_, other_box, fold_lbl, base_txt](brls::View*) {
                if (!alive->load(std::memory_order_acquire)) return true;
                other_tools_expanded_ = !other_tools_expanded_;
                other_box->setVisibility(
                    other_tools_expanded_ ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
                fold_lbl->setText((other_tools_expanded_ ? "▾ " : "▸ ") + base_txt);
                if (tools_inner_) tools_inner_->invalidate();
                return true;
            });

            tools_inner_->addView(fold_hdr);
            tools_inner_->addView(other_box);
        }
    }

    updateHeaderAndTabsStyling();
    tools_inner_->invalidate();
}

void AiCompanionDock::refreshAiRouteStatus() {
    if (!open_) {
        route_dirty_ = true;
        return;
    }
    route_dirty_ = false;
    if (!route_badge_label_) return;

    if (route_cancel_) {
        route_cancel_->cancel();
        route_cancel_.reset();
    }
    const uint64_t seq = ++route_req_seq_;

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    auto providers = caps.providersFor("ai.agent.v1");
    if (providers.empty()) {
        providers = caps.providersFor("llm.chat.v1");
    }
    if (providers.empty()) {
        route_ready_ = false;
        route_badge_label_->setText("未接入插件");
        route_badge_label_->setTextColor(ui::color("status/warn_fg"));
        if (route_dot_) route_dot_->setBackgroundColor(ui::color("status/warn_fg"));
        return;
    }

    route_ready_ = true;
    if (route_dot_) route_dot_->setBackgroundColor(ui::color("status/ok_fg"));
    route_badge_label_->setText(clipUtf8Codepoints(providers.front().plugin_id, 16));
    route_badge_label_->setTextColor(ui::color("brls/text"));

    if (caps.has("llm.router.v1")) {
        auto cancel = CancelToken::create();
        route_cancel_ = cancel;
        caps.callAsync(
            "llm.router.v1",
            {{"action", "current_route"}},
            [this, alive = alive_, seq](const CapabilityResult& r) {
                brls::sync([this, alive, seq, r]() {
                    if (!alive->load(std::memory_order_acquire) || seq != route_req_seq_ ||
                        !route_badge_label_) {
                        return;
                    }
                    route_cancel_.reset();
                    if (r.ok && r.data.is_object()) {
                        const std::string model = r.data.value("model", "");
                        const std::string summary = r.data.value("summary", "");
                        current_route_model_ = !model.empty()
                                                   ? model
                                                   : (!summary.empty() ? summary : "Auto Route");
                        route_badge_label_->setText(clipUtf8Codepoints(current_route_model_, 18));
                        route_badge_label_->setTextColor(ui::color("brls/text"));
                        if (composer_) {
                            composer_->setModelLabel(current_route_model_);
                        }
                    }
                });
            },
            cancel);
    }
}

void AiCompanionDock::openModelPickerDialog() {
    openAiModelPickerDialog(
        current_route_model_,
        on_open_marketplace_,
        [this, alive = alive_](const std::string& mid) {
            if (!alive->load(std::memory_order_acquire)) return;
            current_route_model_ = mid;
            if (composer_) composer_->setModelLabel(mid);
            refreshAiRouteStatus();
        });
}

void AiCompanionDock::openSessionAndUtilityDialog() {
    if (!active_cancel_) {
        const std::string keep_id = activeSessionId();
        sessions_ = loadAiChatSessionsFromDisk();
        for (size_t i = 0; i < sessions_.size(); ++i) {
            if (sessions_[i].id == keep_id) {
                active_session_idx_ = i;
                break;
            }
        }
    }

    auto* box = new brls::Box(brls::Axis::COLUMN);
    box->setPadding(20);
    box->setWidth(520);

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    hdr->setMarginBottom(6.0f);
    hdr->addView(ui::makeLabel("AI 伴读与对话记录", 18.5f, "brls/text"));
    hdr->addView(ui::makePill("与全屏 AI 助手实时同步", "status/info_bg", "status/info_fg"));
    box->addView(hdr);

    auto* sub = ui::makeLabel(
        "侧边伴读栏与 Ctrl+5 全屏 AI 助手共享同一套对话历史、深度思考记录与策展配置。",
        13.0f,
        "font/grey");
    sub->setMarginBottom(12.0f);
    box->addView(sub);

    auto* dlg = new brls::Dialog(box);

    auto* top_btns = new brls::Box(brls::Axis::ROW);
    top_btns->setMarginBottom(10.0f);

    auto* new_chat = ui::makeButton("+ 开启新对话", [this, alive = alive_, dlg]() {
        dlg->close();
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            switchTab(DockTab::Chat);
            startNewSession(false);
            focusComposer(true);
        });
    }, false, 34.0f);
    new_chat->setGrow(1.0f);
    new_chat->setMarginRight(8.0f);
    top_btns->addView(new_chat);

    auto* new_curate = ui::makeButton("✦ 开启灵感策展", [this, alive = alive_, dlg]() {
        dlg->close();
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            switchTab(DockTab::Chat);
            startNewSession(true);
            focusComposer(true);
        });
    }, false, 34.0f);
    new_curate->setGrow(1.0f);
    new_curate->setMarginRight(8.0f);
    top_btns->addView(new_curate);

    auto* full_btn = ui::makeButton("⤢ 展开至全屏 AI", [this, alive = alive_, dlg]() {
        dlg->close();
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) {
                expandToFullAiWorkspace();
            }
        });
    }, false, 34.0f);
    full_btn->setShrink(0.0f);
    top_btns->addView(full_btn);
    box->addView(top_btns);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setWidthPercentage(100);
    scroll->setHeight(260);
    auto* list = new brls::Box(brls::Axis::COLUMN);
    list->setWidthPercentage(100);

    for (size_t i = 0; i < sessions_.size(); ++i) {
        const auto& s = sessions_[i];
        const bool is_cur = (i == active_session_idx_);

        auto* row = new brls::Box(brls::Axis::ROW);
        row->setWidthPercentage(100);
        row->setAlignItems(brls::AlignItems::CENTER);
        row->setMarginBottom(6.0f);

        std::string label = is_cur ? "● " : "  ";
        if (s.curation_mode) label += "[策展] ";
        label += s.title.empty() ? "新对话" : s.title;
        if (!s.messages.empty()) {
            label += "  (" + std::to_string(s.messages.size()) + " 条)";
        }

        auto* sel_btn = ui::makeButton(label, [this, alive = alive_, i, dlg]() {
            dlg->close();
            brls::sync([this, alive, i]() {
                if (alive->load(std::memory_order_acquire)) {
                    selectSession(i);
                }
            });
        }, is_cur, 34.0f);
        sel_btn->setGrow(1.0f);
        row->addView(sel_btn);

        if (sessions_.size() > 1 || !s.messages.empty()) {
            auto* del_btn = ui::makeButton("删除", [this, alive = alive_, i, dlg]() {
                dlg->close();
                brls::sync([this, alive, i]() {
                    if (alive->load(std::memory_order_acquire)) {
                        deleteSession(i);
                    }
                });
            }, false, 34.0f);
            del_btn->setMarginLeft(6.0f);
            del_btn->setShrink(0.0f);
            row->addView(del_btn);
        }
        list->addView(row);
    }

    scroll->setContentView(list);
    box->addView(scroll);

    box->addView(ui::makeDivider(10.0f));

    auto* util_row = new brls::Box(brls::Axis::ROW);
    util_row->setWidthPercentage(100);

    auto* rehydrate_btn = ui::makeButton("回灌历史推荐", [this, alive = alive_, dlg]() {
        dlg->close();
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire)) triggerArchiveRehydrate();
        });
    }, false, 32.0f);
    rehydrate_btn->setGrow(1.0f);
    rehydrate_btn->setMarginRight(6.0f);
    util_row->addView(rehydrate_btn);

    auto* cal_btn = ui::makeButton("推荐日历", [alive = alive_, dlg]() {
        dlg->close();
        brls::sync([alive]() {
            if (alive->load(std::memory_order_acquire)) CurationCalendarDialog::open();
        });
    }, false, 32.0f);
    cal_btn->setGrow(1.0f);
    cal_btn->setMarginRight(6.0f);
    util_row->addView(cal_btn);

    auto* sched_btn = ui::makeButton("定时策展", [alive = alive_, dlg]() {
        dlg->close();
        brls::sync([alive]() {
            if (alive->load(std::memory_order_acquire)) {
                CurationSchedulerDialog::open([]() { CurationCalendarDialog::open(); });
            }
        });
    }, false, 32.0f);
    sched_btn->setGrow(1.0f);
    sched_btn->setMarginRight(6.0f);
    util_row->addView(sched_btn);

    auto* mkt_btn = ui::makeButton("AI 插件", [this, alive = alive_, dlg]() {
        dlg->close();
        brls::sync([this, alive]() {
            if (alive->load(std::memory_order_acquire) && on_open_marketplace_) {
                on_open_marketplace_();
            }
        });
    }, false, 32.0f);
    mkt_btn->setGrow(1.0f);
    util_row->addView(mkt_btn);

    box->addView(util_row);

    dlg->addButton("关闭", [] {});
    dlg->open();
}

void AiCompanionDock::resetLiveTurnRefs() {
    live_turn_box_ = nullptr;
    live_role_label_ = nullptr;
    live_think_box_ = nullptr;
    live_think_header_label_ = nullptr;
    live_think_body_label_ = nullptr;
    live_reply_host_ = nullptr;
    live_reply_label_ = nullptr;
}

brls::Box* AiCompanionDock::buildMessageTurnView(size_t msg_idx) {
    auto& sess = currentSession();
    if (msg_idx >= sess.messages.size()) return nullptr;
    const auto& m = sess.messages[msg_idx];

    if (m.role == "user") {
        // Right-aligned DeepSeek User Message Bubble (unified with AiWorkbench)
        auto* outer = new brls::Box(brls::Axis::COLUMN);
        outer->setWidthPercentage(100);
        outer->setAlignItems(brls::AlignItems::FLEX_END);
        outer->setMarginBottom(14.0f);

        auto* bubble = new brls::Box(brls::Axis::COLUMN);
        bubble->setMaxWidthPercentage(90);
        bubble->setPadding(10, 14, 10, 14);
        bubble->setCornerRadius(15.0f);
        bubble->setBackgroundColor(ui::color(m.curation ? "desktop/active" : "desktop/soft"));
        bubble->setBorderThickness(1.0f);
        bubble->setBorderColor(ui::color(m.curation ? "brls/accent" : "desktop/line"));

        if (m.curation) {
            auto* badge = ui::makeLabel("灵感策展指令", 11.8f, "brls/accent", 1.2f);
            badge->setMarginBottom(3.0f);
            bubble->addView(badge);
        }

        MarkdownStyle u_style;
        u_style.font_size = 14.5f;
        u_style.line_height = 1.52f;
        u_style.text_color_token = "brls/text";
        u_style.block_gap = 5.0f;
        bubble->addView(new MarkdownView(m.content, std::move(u_style)));
        outer->addView(bubble);

        auto* act_row = new brls::Box(brls::Axis::ROW);
        act_row->setMarginTop(4.0f);
        auto* copy_btn = ui::makeButton("复制", [txt = m.content]() {
            SDL_SetClipboardText(txt.c_str());
            brls::Application::notify("已复制提问内容");
        }, false, 24.0f);
        copy_btn->setMarginRight(5.0f);
        act_row->addView(copy_btn);

        auto* edit_btn = ui::makeButton("重新编辑", [this, alive = alive_, txt = m.content, cur = m.curation]() {
            if (!alive->load(std::memory_order_acquire) || !composer_) return;
            composer_->setCurationMode(cur);
            composer_->setDraftText(txt, true);
        }, false, 24.0f);
        act_row->addView(edit_btn);
        outer->addView(act_row);
        return outer;
    }

    // Left-aligned open DeepSeek Assistant / System Message Turn (unified with AiWorkbench)
    auto* turn = new brls::Box(brls::Axis::COLUMN);
    turn->setWidthPercentage(100);
    turn->setMarginBottom(18.0f);

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setMarginBottom(6.0f);

    auto* avatar = new brls::Box(brls::Axis::ROW);
    avatar->setDimensions(24.0f, 24.0f);
    avatar->setCornerRadius(12.0f);
    avatar->setMarginRight(7.0f);
    avatar->setShrink(0.0f);
    avatar->setAlignItems(brls::AlignItems::CENTER);
    avatar->setJustifyContent(brls::JustifyContent::CENTER);
    avatar->setBackgroundColor(ui::color(m.curation ? "desktop/active" : "status/info_bg"));
    auto* av_icon = new SvgIconView();
    av_icon->setDimensions(14, 14);
    av_icon->setSvgString(svgAiEmblem(
        nvgHex(ui::color(m.curation ? "brls/accent" : "desktop/focus_ring")),
        nvgHex(ui::color("brls/accent"))));
    avatar->addView(av_icon);
    hdr->addView(avatar);

    const char* role_col = m.error ? "status/err_fg"
                                   : (m.curation ? "brls/accent" : "brls/text");
    auto* role_lbl = ui::makeLabel(m.display_role.empty() ? "AI" : m.display_role, 13.2f, role_col);
    role_lbl->setSingleLine(true);
    role_lbl->setShrink(1.0f);
    hdr->addView(role_lbl);
    turn->addView(hdr);

    // Collapsible DeepSeek Reasoning / Thought Block
    if (!m.reasoning.empty()) {
        auto* think_wrap = new brls::Box(brls::Axis::COLUMN);
        think_wrap->setWidthPercentage(100);
        think_wrap->setMarginBottom(8.0f);
        think_wrap->setPadding(0, 0, 0, 31);

        const std::string dur = formatDurationSeconds(m.thinking_sec);
        const std::string base_hdr =
            dur.empty() ? "已深度思考" : ("已深度思考 (用时 " + dur + " 秒)");

        auto* toggle_btn = new brls::Box(brls::Axis::ROW);
        toggle_btn->setPadding(4, 10, 4, 10);
        toggle_btn->setCornerRadius(8.0f);
        toggle_btn->setHighlightCornerRadius(8.0f);
        toggle_btn->setFocusable(true);
        toggle_btn->setAlignItems(brls::AlignItems::CENTER);
        toggle_btn->setBackgroundColor(ui::color("desktop/soft"));
        toggle_btn->setBorderThickness(1.0f);
        toggle_btn->setBorderColor(ui::color("desktop/line"));

        auto* toggle_lbl = ui::makeLabel(
            (m.thinking_collapsed ? "▸ " : "▾ ") + base_hdr,
            12.2f,
            "font/grey",
            1.2f);
        toggle_btn->addView(toggle_lbl);

        auto* quote_row = new brls::Box(brls::Axis::ROW);
        quote_row->setWidthPercentage(100);
        quote_row->setMarginTop(6.0f);
        quote_row->setVisibility(
            m.thinking_collapsed ? brls::Visibility::GONE : brls::Visibility::VISIBLE);

        auto* quote_bar = new brls::Box(brls::Axis::COLUMN);
        quote_bar->setWidth(2.5f);
        quote_bar->setShrink(0.0f);
        quote_bar->setCornerRadius(1.2f);
        quote_bar->setBackgroundColor(ui::color("desktop/line"));
        quote_bar->setMarginRight(10.0f);
        quote_row->addView(quote_bar);

        auto* thought_body = ui::makeLabel(m.reasoning, 13.0f, "font/grey", 1.48f);
        thought_body->setGrow(1.0f);
        thought_body->setShrink(1.0f);
        quote_row->addView(thought_body);

        toggle_btn->registerClickAction([this, alive = alive_, msg_idx, quote_row, toggle_lbl, base_hdr](brls::View*) {
            if (!alive->load(std::memory_order_acquire)) return true;
            auto& cur_s = currentSession();
            if (msg_idx >= cur_s.messages.size()) return true;
            bool& collapsed = cur_s.messages[msg_idx].thinking_collapsed;
            collapsed = !collapsed;
            quote_row->setVisibility(collapsed ? brls::Visibility::GONE : brls::Visibility::VISIBLE);
            toggle_lbl->setText((collapsed ? "▸ " : "▾ ") + base_hdr);
            if (messages_box_) messages_box_->invalidate();
            return true;
        });

        auto* btn_holder = new brls::Box(brls::Axis::ROW);
        btn_holder->addView(toggle_btn);
        think_wrap->addView(btn_holder);
        think_wrap->addView(quote_row);
        turn->addView(think_wrap);
    }

    auto* body_wrap = new brls::Box(brls::Axis::COLUMN);
    body_wrap->setWidthPercentage(100);
    body_wrap->setPadding(0, 0, 0, 31);

    MarkdownStyle md_style;
    md_style.font_size = 14.6f;
    md_style.line_height = 1.58f;
    md_style.text_color_token = m.error ? "status/err_fg" : "brls/text";
    md_style.block_gap = 6.0f;
    body_wrap->addView(new MarkdownView(m.content, std::move(md_style)));

    if (m.role == "assistant") {
        auto* actions = new brls::Box(brls::Axis::ROW);
        actions->setMarginTop(6.0f);
        auto* cp_btn = ui::makeButton("复制回复", [txt = m.content]() {
            SDL_SetClipboardText(txt.c_str());
            brls::Application::notify("已复制 AI 回复全文");
        }, false, 25.0f);
        cp_btn->setMarginRight(6.0f);
        actions->addView(cp_btn);

        const size_t assistant_idx = msg_idx;
        auto* regen_btn = ui::makeButton("重新生成", [this, alive = alive_, assistant_idx]() {
            if (alive->load(std::memory_order_acquire)) {
                regenerateFromAssistantIndex(assistant_idx);
            }
        }, false, 25.0f);
        actions->addView(regen_btn);
        body_wrap->addView(actions);
    }

    turn->addView(body_wrap);
    return turn;
}

void AiCompanionDock::appendMessageTurnView(size_t msg_idx) {
    if (!messages_box_) return;
    if (brls::Box* turn = buildMessageTurnView(msg_idx)) {
        messages_box_->addView(turn);
        messages_box_->invalidate();
    }
}

void AiCompanionDock::rebuildThreadViews() {
    resetLiveTurnRefs();
    if (!messages_box_) return;
    if (focusIsInsideView(messages_box_)) {
        brls::Application::giveFocus(nullptr);
    }
    messages_box_->clearViews();

    auto& sess = currentSession();
    for (size_t i = 0; i < sess.messages.size(); ++i) {
        if (brls::Box* turn = buildMessageTurnView(i)) {
            messages_box_->addView(turn);
        }
    }
    messages_box_->invalidate();
}

void AiCompanionDock::scrollToBottom(bool immediate) {
    if (!thread_scroll_ || !messages_box_) return;
    messages_box_->invalidate();
    brls::sync([this, alive = alive_, immediate]() {
        if (!alive->load(std::memory_order_acquire) || !thread_scroll_ || !messages_box_) return;
        const float max_y = std::max(0.0f, messages_box_->getHeight() - thread_scroll_->getHeight());
        thread_scroll_->setContentOffsetY(max_y, !immediate);
    });
}

void AiCompanionDock::askWithContext(ContentDomain domain,
                                     const std::string& context_summary,
                                     const std::string& prompt) {
    if (!open_) {
        toggleVisible();
    }
    switchTab(DockTab::Chat);
    setDomainContext(domain, context_summary);
    if (!prompt.empty()) {
        submitPrompt(prompt, composer_ ? composer_->isDeepThinking() : false);
    } else {
        focusComposer(true);
    }
}

void AiCompanionDock::handleComposerSubmit(const DeepSeekComposerSubmission& sub) {
    sync_playlist_enabled_ = sub.sync_playlist;
    std::string prompt = trimWhitespace(sub.prompt);
    if (prompt.empty()) return;

    std::string extracted_curation;
    const bool force_curation = extractCurationCommand(prompt, &extracted_curation);
    if (force_curation) {
        prompt = extracted_curation;
    }

    if (sub.curation_mode || force_curation) {
        dispatchCurationRequest(prompt, sub.sync_playlist);
    } else {
        submitPrompt(prompt, sub.deep_thinking);
    }
}

void AiCompanionDock::submitCurationPrompt(const std::string& prompt) {
    const std::string clean = trimWhitespace(prompt);
    if (clean.empty()) return;
    switchTab(DockTab::Chat);
    dispatchCurationRequest(clean, sync_playlist_enabled_);
}

void AiCompanionDock::regenerateFromAssistantIndex(size_t assistant_msg_idx) {
    cancelActiveStream();

    auto& sess = currentSession();
    if (assistant_msg_idx == 0 || assistant_msg_idx >= sess.messages.size()) return;

    size_t user_idx = assistant_msg_idx;
    while (user_idx > 0) {
        --user_idx;
        if (sess.messages[user_idx].role == "user") {
            break;
        }
    }
    if (sess.messages[user_idx].role != "user") return;

    const std::string prompt = sess.messages[user_idx].content;
    const bool cur = sess.messages[user_idx].curation;
    sess.messages.resize(user_idx);

    rebuildThreadViews();
    updateStageVisibility();

    if (cur) {
        dispatchCurationRequest(prompt, sync_playlist_enabled_);
    } else {
        submitPrompt(prompt, composer_ ? composer_->isDeepThinking() : false);
    }
}

void AiCompanionDock::submitPrompt(const std::string& prompt, bool deep_thinking) {
    std::string curation_query;
    if (extractCurationCommand(prompt, &curation_query)) {
        dispatchCurationRequest(curation_query, sync_playlist_enabled_);
        return;
    }

    switchTab(DockTab::Chat);
    cancelActiveStream();

    auto& sess = currentSession();
    const bool first_msg = sess.messages.empty();
    sess.domain = domain_;
    if (!entity_summary_.empty()) {
        sess.context_summary = entity_summary_;
    }

    nlohmann::json history = nlohmann::json::array();
    if (!entity_summary_.empty()) {
        history.push_back({
            {"role", "system"},
            {"content", "Current OmniDesk domain: " + toDisplayLabel(domain_) +
                            " | Active context: " + entity_summary_},
        });
    }
    for (const auto& prev : sess.messages) {
        if ((prev.role == "user" || prev.role == "assistant") && !prev.error && !prev.content.empty()) {
            history.push_back({{"role", prev.role}, {"content", prev.content}});
        }
    }

    AiChatMessage user_msg;
    user_msg.role = "user";
    user_msg.display_role = "你";
    user_msg.content = prompt;
    user_msg.curation = false;
    sess.messages.push_back(std::move(user_msg));
    if (first_msg || sess.title == "新对话" || sess.title == "新灵感策展") {
        sess.title = summarizeAiSessionTitle(prompt);
    }
    sess.curation_mode = false;
    sess.deep_thinking = deep_thinking;
    sess.updated_at = nowUnixSec();

    appendMessageTurnView(sess.messages.size() - 1);
    updateHeaderAndTabsStyling();
    updateStageVisibility();

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    std::string cap_id;
    std::string role_prefix;
    nlohmann::json req = nlohmann::json::object();

    if (caps.has("ai.agent.v1")) {
        cap_id = "ai.agent.v1";
        role_prefix = "AI Agent";
        req = {
            {"action", "chat"},
            {"domain", toString(domain_)},
            {"context", entity_summary_},
            {"messages", history},
            {"prompt", prompt},
            {"stream", true},
        };
    } else if (caps.has("llm.chat.v1")) {
        cap_id = "llm.chat.v1";
        role_prefix = "LLM";
        history.push_back({{"role", "user"}, {"content", prompt}});
        req = {
            {"domain", toString(domain_)},
            {"context", entity_summary_},
            {"messages", history},
            {"prompt", prompt},
            {"stream", true},
        };
    } else {
        AiChatMessage err_msg;
        err_msg.role = "assistant";
        err_msg.display_role = "未安装 AI 能力插件";
        err_msg.content =
            "尚未安装提供 `ai.agent.v1` 或 `llm.chat.v1` 的独立插件（如 `plugin-magpie-agent`）。\n"
            "可点击顶部对话标题菜单中的「AI 插件」前往插件市场一键安装。";
        err_msg.error = true;
        sess.messages.push_back(std::move(err_msg));
        saveSessionsToDisk();
        appendMessageTurnView(sess.messages.size() - 1);
        scrollToBottom(false);
        return;
    }

    req["deep_thinking"] = deep_thinking;
    if (deep_thinking) {
        req["reasoning_effort"] = "high";
    }

    // Build live streaming turn (identical structure to AiWorkbench)
    auto* turn = new brls::Box(brls::Axis::COLUMN);
    turn->setWidthPercentage(100);
    turn->setMarginBottom(18.0f);
    live_turn_box_ = turn;

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setMarginBottom(6.0f);

    auto* avatar = new brls::Box(brls::Axis::ROW);
    avatar->setDimensions(24.0f, 24.0f);
    avatar->setCornerRadius(12.0f);
    avatar->setMarginRight(7.0f);
    avatar->setShrink(0.0f);
    avatar->setAlignItems(brls::AlignItems::CENTER);
    avatar->setJustifyContent(brls::JustifyContent::CENTER);
    avatar->setBackgroundColor(ui::color("status/info_bg"));
    auto* av_icon = new SvgIconView();
    av_icon->setDimensions(14, 14);
    av_icon->setSvgString(svgAiEmblem(nvgHex(ui::color("desktop/focus_ring")),
                                      nvgHex(ui::color("brls/accent"))));
    avatar->addView(av_icon);
    hdr->addView(avatar);

    live_role_label_ = ui::makeLabel(
        role_prefix + (deep_thinking ? " · 深度思考中..." : " · 正在生成..."),
        13.2f,
        "desktop/focus_ring");
    hdr->addView(live_role_label_);
    turn->addView(hdr);

    live_think_box_ = new brls::Box(brls::Axis::COLUMN);
    live_think_box_->setWidthPercentage(100);
    live_think_box_->setMarginBottom(8.0f);
    live_think_box_->setPadding(0, 0, 0, 31);
    live_think_box_->setVisibility(deep_thinking ? brls::Visibility::VISIBLE : brls::Visibility::GONE);

    live_think_header_label_ = ui::makeLabel("▾ 正在深度思考...", 12.2f, "font/grey", 1.2f);
    live_think_header_label_->setMarginBottom(5.0f);
    live_think_box_->addView(live_think_header_label_);

    auto* quote_row = new brls::Box(brls::Axis::ROW);
    quote_row->setWidthPercentage(100);
    auto* quote_bar = new brls::Box(brls::Axis::COLUMN);
    quote_bar->setWidth(2.5f);
    quote_bar->setShrink(0.0f);
    quote_bar->setCornerRadius(1.2f);
    quote_bar->setBackgroundColor(ui::color("desktop/line"));
    quote_bar->setMarginRight(10.0f);
    quote_row->addView(quote_bar);

    live_think_body_label_ = ui::makeLabel(deep_thinking ? "▍" : "", 13.0f, "font/grey", 1.48f);
    live_think_body_label_->setGrow(1.0f);
    live_think_body_label_->setShrink(1.0f);
    quote_row->addView(live_think_body_label_);
    live_think_box_->addView(quote_row);
    turn->addView(live_think_box_);

    live_reply_host_ = new brls::Box(brls::Axis::COLUMN);
    live_reply_host_->setWidthPercentage(100);
    live_reply_host_->setPadding(0, 0, 0, 31);
    live_reply_label_ = ui::makeLabel(deep_thinking ? "" : "▍", 14.6f, "brls/text", 1.56f);
    live_reply_host_->addView(live_reply_label_);
    turn->addView(live_reply_host_);

    if (messages_box_) {
        messages_box_->addView(turn);
        messages_box_->invalidate();
    }
    scrollToBottom(false);

    const uint64_t seq = ++active_req_seq_;
    auto cancel = CancelToken::create();
    active_cancel_ = cancel;
    streaming_session_idx_ = active_session_idx_;
    stream_start_ms_ = nowSteadyMs();
    reasoning_end_ms_ = 0;
    live_raw_content_.clear();
    live_reasoning_.clear();
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        pending_content_delta_.clear();
        pending_reasoning_delta_.clear();
        flush_scheduled_ = false;
        last_stream_flush_ms_ = 0;
    }
    if (composer_) {
        composer_->setStreaming(true);
    }

    caps.callStreamAsync(
        cap_id,
        req,
        [this, alive = alive_, cancel, seq](const StreamChunk& chunk) {
            if (!alive->load(std::memory_order_acquire) || cancel->isCancelled() || chunk.delta.empty()) {
                return;
            }
            {
                std::lock_guard<std::mutex> lk(stream_mu_);
                if (chunk.event == "reasoning") {
                    pending_reasoning_delta_ += chunk.delta;
                } else {
                    pending_content_delta_ += chunk.delta;
                }
            }
            scheduleStreamUiFlush(seq, false);
        },
        [this, alive = alive_, cancel, seq, role_prefix](const CapabilityResult& res) {
            brls::sync([this, alive, cancel, seq, role_prefix, res]() {
                if (!alive->load(std::memory_order_acquire) || seq != active_req_seq_) {
                    return;
                }
                std::string c_delta;
                std::string r_delta;
                {
                    std::lock_guard<std::mutex> lk(stream_mu_);
                    c_delta.swap(pending_content_delta_);
                    r_delta.swap(pending_reasoning_delta_);
                    flush_scheduled_ = false;
                }
                live_reasoning_ += r_delta;
                live_raw_content_ += c_delta;

                std::string model_tag;
                if (res.data.is_object() && res.data.contains("model") && res.data["model"].is_string()) {
                    model_tag = res.data["model"].get<std::string>();
                }
                const std::string provider = !model_tag.empty()
                                                 ? (role_prefix + " · " + model_tag)
                                                 : (!res.provider_plugin_id.empty()
                                                        ? (role_prefix + " (" + res.provider_plugin_id + ")")
                                                        : role_prefix);

                if (res.cancelled || cancel->isCancelled()) {
                    const std::string body = live_raw_content_.empty()
                                                 ? "[已中断本次生成]"
                                                 : (live_raw_content_ + "\n\n*[已中断]*");
                    finalizeActiveStream(provider + " · 已中断", body, live_reasoning_, false);
                } else if (res.ok) {
                    std::string final_raw = live_raw_content_;
                    if (final_raw.empty() && res.data.is_object() && res.data.contains("reply") &&
                        res.data["reply"].is_string()) {
                        final_raw = res.data["reply"].get<std::string>();
                    }
                    if (final_raw.empty()) {
                        final_raw = res.data.empty() ? "(空回复)" : res.data.dump(2);
                    }
                    std::string final_reason = live_reasoning_;
                    if (final_reason.empty() && res.data.is_object() && res.data.contains("reasoning") &&
                        res.data["reasoning"].is_string()) {
                        final_reason = res.data["reasoning"].get<std::string>();
                    }
                    if (res.data.is_object() && res.data.contains("tool_calls_executed") &&
                        res.data["tool_calls_executed"].is_array() &&
                        HostKernel::instance().isInitialized()) {
                        for (const auto& tc : res.data["tool_calls_executed"]) {
                            if (!tc.is_object() || !tc.value("ok", false)) continue;
                            const std::string tname = tc.value("name", "");
                            if (tname == "omnidesk_curation_writeback" ||
                                tname == "omnidesk_curation_rehydrate_archive") {
                                HostKernel::instance().bus().publish("entity.recommend.updated", {
                                    {"mode", "interactive_ephemeral"},
                                    {"source", tname},
                                });
                                break;
                            }
                        }
                    }
                    finalizeActiveStream(provider, final_raw, final_reason, false);
                } else {
                    finalizeActiveStream("请求失败 · " + provider,
                                         res.error.empty() ? "AI 能力调用失败" : res.error,
                                         live_reasoning_,
                                         true);
                }
            });
        },
        cancel);
}

void AiCompanionDock::dispatchCurationRequest(const std::string& prompt, bool sync_playlist) {
    cancelActiveStream();
    switchTab(DockTab::Chat);

    auto& sess = currentSession();
    const std::string session_id = sess.id;
    const bool first_msg = sess.messages.empty();
    sess.domain = domain_;
    if (!entity_summary_.empty()) {
        sess.context_summary = entity_summary_;
    }

    AiChatMessage user_msg;
    user_msg.role = "user";
    user_msg.display_role = "你 · 灵感策展";
    user_msg.content = prompt;
    user_msg.curation = true;
    sess.messages.push_back(std::move(user_msg));
    if (first_msg || sess.title == "新对话" || sess.title == "新灵感策展") {
        sess.title = summarizeAiSessionTitle(prompt);
    }
    sess.curation_mode = true;
    sess.updated_at = nowUnixSec();

    appendMessageTurnView(sess.messages.size() - 1);
    updateHeaderAndTabsStyling();
    updateStageVisibility();

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    auto& caps = kernel.capabilities();
    const bool has_curation = caps.has("ai.curation.v1");
    const bool has_feed = caps.has("recommend.feed.v1");

    if (!has_curation && !has_feed) {
        AiChatMessage err_msg;
        err_msg.role = "assistant";
        err_msg.display_role = "未安装策展插件";
        err_msg.content =
            "尚未安装 `plugin-magpie-agent` (`ai.curation.v1`) 或 `plugin-ai-curator` (`recommend.feed.v1`)。\n"
            "请在插件市场安装后即可直接策展并反写三大工作台与 Apple Music 灵感歌单。";
        err_msg.curation = true;
        err_msg.error = true;
        sess.messages.push_back(std::move(err_msg));
        saveSessionsToDisk();
        appendMessageTurnView(sess.messages.size() - 1);
        scrollToBottom(false);
        return;
    }

    auto* turn = new brls::Box(brls::Axis::COLUMN);
    turn->setWidthPercentage(100);
    turn->setMarginBottom(18.0f);
    live_turn_box_ = turn;

    auto* hdr = new brls::Box(brls::Axis::ROW);
    hdr->setAlignItems(brls::AlignItems::CENTER);
    hdr->setMarginBottom(6.0f);

    auto* avatar = new brls::Box(brls::Axis::ROW);
    avatar->setDimensions(24.0f, 24.0f);
    avatar->setCornerRadius(12.0f);
    avatar->setMarginRight(7.0f);
    avatar->setShrink(0.0f);
    avatar->setAlignItems(brls::AlignItems::CENTER);
    avatar->setJustifyContent(brls::JustifyContent::CENTER);
    avatar->setBackgroundColor(ui::color("desktop/active"));
    auto* av_icon = new SvgIconView();
    av_icon->setDimensions(14, 14);
    av_icon->setSvgString(svgAiEmblem(nvgHex(ui::color("brls/accent")),
                                      nvgHex(ui::color("desktop/focus_ring"))));
    avatar->addView(av_icon);
    hdr->addView(avatar);

    live_role_label_ = ui::makeLabel(
        has_curation ? "AI 策展大脑 · 编排并反写中..." : "历史归档策展 · 检索中...",
        13.2f,
        "brls/accent");
    hdr->addView(live_role_label_);
    turn->addView(hdr);

    live_reply_host_ = new brls::Box(brls::Axis::COLUMN);
    live_reply_host_->setWidthPercentage(100);
    live_reply_host_->setPadding(0, 0, 0, 31);
    live_reply_label_ = ui::makeLabel("▍", 14.6f, "brls/text", 1.56f);
    live_reply_host_->addView(live_reply_label_);
    turn->addView(live_reply_host_);

    if (messages_box_) {
        messages_box_->addView(turn);
        messages_box_->invalidate();
    }
    scrollToBottom(false);

    const uint64_t seq = ++active_req_seq_;
    auto cancel = CancelToken::create();
    active_cancel_ = cancel;
    streaming_session_idx_ = active_session_idx_;
    stream_start_ms_ = nowSteadyMs();
    reasoning_end_ms_ = 0;
    live_raw_content_.clear();
    live_reasoning_.clear();
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        pending_content_delta_.clear();
        pending_reasoning_delta_.clear();
        flush_scheduled_ = false;
        last_stream_flush_ms_ = 0;
    }
    if (composer_) {
        composer_->setStreaming(true);
    }

    if (has_curation) {
        nlohmann::json req = {
            {"action", "curate_interactive"},
            {"prompt", prompt},
            {"domain", toString(domain_)},
            {"context", entity_summary_},
            {"repeat_policy", "allow_any"},
            {"sync_playlist", sync_playlist},
            {"playlist_name", "OmniDesk AI 灵感电台"},
            {"stream", true},
            {"session_id", session_id},
        };
        caps.callStreamAsync(
            "ai.curation.v1",
            req,
            [this, alive = alive_, cancel, seq](const StreamChunk& chunk) {
                if (!alive->load(std::memory_order_acquire) || cancel->isCancelled() || chunk.delta.empty()) {
                    return;
                }
                {
                    std::lock_guard<std::mutex> lk(stream_mu_);
                    pending_content_delta_ += chunk.delta;
                }
                scheduleStreamUiFlush(seq, true);
            },
            [this, alive = alive_, cancel, seq](const CapabilityResult& res) {
                brls::sync([this, alive, cancel, seq, res]() {
                    if (!alive->load(std::memory_order_acquire) || seq != active_req_seq_) return;
                    std::string delta;
                    {
                        std::lock_guard<std::mutex> lk(stream_mu_);
                        delta.swap(pending_content_delta_);
                        flush_scheduled_ = false;
                    }
                    live_raw_content_ += delta;

                    const std::string provider = res.provider_plugin_id.empty()
                                                     ? "AI 策展大脑"
                                                     : ("AI 策展 (" + res.provider_plugin_id + ")");
                    if (res.cancelled || cancel->isCancelled()) {
                        const std::string body = live_raw_content_.empty()
                                                     ? "[已中断策展]"
                                                     : (live_raw_content_ + "\n\n*[已中断]*");
                        finalizeActiveStream(provider + " · 已中断", body, "", false);
                    } else if (res.ok) {
                        std::string final_text;
                        if (res.data.is_object() && res.data.contains("reply") &&
                            res.data["reply"].is_string() &&
                            !res.data["reply"].get_ref<const std::string&>().empty()) {
                            final_text = res.data["reply"].get<std::string>();
                        } else if (!live_raw_content_.empty()) {
                            final_text = live_raw_content_;
                        } else {
                            final_text = res.data.empty() ? "(策展完成)" : res.data.dump(2);
                        }
                        finalizeActiveStream(provider, final_text, "", false);
                    } else {
                        finalizeActiveStream("策展失败 · " + provider,
                                             res.error.empty() ? "ai.curation.v1 调用失败" : res.error,
                                             "",
                                             true);
                    }
                });
            },
            cancel);
        return;
    }

    // Fallback when only plugin-ai-curator (recommend.feed.v1) is loaded
    caps.callAsync(
        "recommend.feed.v1",
        {{"action", "search_archive"}, {"query", prompt}, {"limit", 8}},
        [this, alive = alive_, cancel, seq, prompt, session_id](const CapabilityResult& res) {
            brls::sync([this, alive, cancel, seq, prompt, session_id, res]() {
                if (!alive->load(std::memory_order_acquire) || seq != active_req_seq_) return;
                if (res.cancelled || cancel->isCancelled()) {
                    finalizeActiveStream("历史归档策展 · 已中断", "[已取消]", "", false);
                    return;
                }
                if (!res.ok) {
                    finalizeActiveStream("策展检索失败",
                                         res.error.empty() ? "recommend.feed.v1 调用失败" : res.error,
                                         "",
                                         true);
                    return;
                }
                nlohmann::json media_items = nlohmann::json::array();
                nlohmann::json text_items = nlohmann::json::array();
                nlohmann::json book_items = nlohmann::json::array();
                std::string summary = "从已回灌历史归档中找到匹配推荐（未装载 `plugin-magpie-agent`，使用归档回退检索）：\n\n";
                size_t count = 0;
                for (const char* k : {"items", "results", "matches"}) {
                    if (res.data.contains(k) && res.data[k].is_array()) {
                        for (const auto& it : res.data[k]) {
                            if (!it.is_object()) continue;
                            ++count;
                            summary += std::to_string(count) + ". **" + it.value("title", "未命名") + "**";
                            std::string dt = it.value("business_date", it.value("date", ""));
                            if (!dt.empty()) summary += " `[" + dt + "]`";
                            summary += "\n";
                            const std::string dom = classifyAiCurationItem(it);
                            if (dom == "text") text_items.push_back(it);
                            else if (dom == "book") book_items.push_back(it);
                            else media_items.push_back(it);
                        }
                        break;
                    }
                }
                if (count == 0) {
                    summary = "未在本地归档中匹配到与「" + prompt + "」直接相关的历史条目。";
                } else {
                    HostKernel::instance().capabilities().callAsync(
                        "recommend.feed.v1",
                        {
                            {"action", "push_interactive_set"},
                            {"mode", "interactive_ephemeral"},
                            {"session_id", session_id},
                            {"title", "归档匹配 · " + prompt},
                            {"user_prompt", prompt},
                            {"repeat_policy", "prefer_familiar"},
                            {"record_permanent_history", false},
                            {"auto_focus", true},
                            {"media_items", media_items},
                            {"text_items", text_items},
                            {"book_items", book_items},
                        },
                        [](const CapabilityResult&) {},
                        cancel);
                }
                finalizeActiveStream("历史归档策展 (plugin-ai-curator)", summary, "", false);
            });
        },
        cancel);
}

void AiCompanionDock::scheduleStreamUiFlush(uint64_t seq, bool curation_only) {
    uint64_t delay_ms = 0;
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        if (flush_scheduled_) {
            return;
        }
        flush_scheduled_ = true;
        const uint64_t now_ms = nowSteadyMs();
        if (last_stream_flush_ms_ > 0 && now_ms > last_stream_flush_ms_ &&
            (now_ms - last_stream_flush_ms_) < kStreamUiFlushIntervalMs) {
            delay_ms = kStreamUiFlushIntervalMs - (now_ms - last_stream_flush_ms_);
        }
    }

    auto alive = alive_;
    if (delay_ms == 0) {
        brls::sync([this, alive, seq, curation_only]() {
            if (alive->load(std::memory_order_acquire)) {
                flushStreamUiNow(seq, curation_only);
            }
        });
    } else {
        brls::Threading::delay(static_cast<long>(delay_ms), [this, alive, seq, curation_only]() {
            if (alive->load(std::memory_order_acquire)) {
                flushStreamUiNow(seq, curation_only);
            }
        });
    }
}

void AiCompanionDock::flushStreamUiNow(uint64_t seq, bool curation_only) {
    if (seq != active_req_seq_) {
        return;
    }

    std::string c_delta;
    std::string r_delta;
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        c_delta.swap(pending_content_delta_);
        r_delta.swap(pending_reasoning_delta_);
        flush_scheduled_ = false;
        last_stream_flush_ms_ = nowSteadyMs();
    }

    if (curation_only) {
        if (c_delta.empty()) return;
        live_raw_content_ += c_delta;
        if (live_reply_label_) {
            live_reply_label_->setText(live_raw_content_ + "▍");
        }
        if (messages_box_) messages_box_->invalidate();
        scrollToBottom(true);
        return;
    }

    if (!r_delta.empty()) {
        live_reasoning_ += r_delta;
        reasoning_end_ms_ = nowSteadyMs();
    }
    if (!c_delta.empty()) {
        if (reasoning_end_ms_ == 0 && !live_reasoning_.empty()) {
            reasoning_end_ms_ = nowSteadyMs();
        }
        live_raw_content_ += c_delta;
    }

    std::string parsed_think;
    std::string parsed_reply;
    bool in_think = false;
    splitAiThinkAndReply(live_raw_content_, live_reasoning_, &parsed_think, &parsed_reply, &in_think);
    if (in_think) {
        reasoning_end_ms_ = nowSteadyMs();
    }

    if (!parsed_think.empty() && live_think_box_ && live_think_body_label_) {
        live_think_box_->setVisibility(brls::Visibility::VISIBLE);
        const bool still_thinking = in_think || parsed_reply.empty();
        if (live_think_header_label_) {
            live_think_header_label_->setText(
                still_thinking ? "▾ 正在深度思考..." : "▾ 思考完成，正在输出回答...");
        }
        live_think_body_label_->setText(parsed_think + (still_thinking ? "▍" : ""));
    }

    if (live_reply_label_) {
        if (!parsed_reply.empty() || parsed_think.empty()) {
            live_reply_label_->setText(parsed_reply + "▍");
        } else {
            live_reply_label_->setText("");
        }
    }
    if (messages_box_) messages_box_->invalidate();
    scrollToBottom(true);
}

bool AiCompanionDock::cancelActiveStream() {
    if (!active_cancel_) {
        return false;
    }
    auto cancel = active_cancel_;
    cancel->cancel();
    ++active_req_seq_;

    std::string c_delta;
    std::string r_delta;
    {
        std::lock_guard<std::mutex> lk(stream_mu_);
        c_delta.swap(pending_content_delta_);
        r_delta.swap(pending_reasoning_delta_);
        flush_scheduled_ = false;
    }
    live_reasoning_ += r_delta;
    live_raw_content_ += c_delta;

    const std::string body = live_raw_content_.empty()
                                 ? "[已取消 AI 请求]"
                                 : (live_raw_content_ + "\n\n*[已中断]*");
    finalizeActiveStream("AI · 已中断", body, live_reasoning_, false);
    return true;
}

void AiCompanionDock::finalizeActiveStream(const std::string& display_role,
                                           const std::string& raw_reply,
                                           const std::string& explicit_reasoning,
                                           bool is_error) {
    std::string parsed_think;
    std::string parsed_reply;
    splitAiThinkAndReply(raw_reply, explicit_reasoning, &parsed_think, &parsed_reply, nullptr);
    if (parsed_reply.empty() && !raw_reply.empty() && parsed_think.empty()) {
        parsed_reply = raw_reply;
    }

    const uint64_t end_ms = reasoning_end_ms_ > stream_start_ms_ ? reasoning_end_ms_ : nowSteadyMs();
    const double think_sec =
        (!parsed_think.empty() && end_ms > stream_start_ms_)
            ? static_cast<double>(end_ms - stream_start_ms_) / 1000.0
            : 0.0;

    const size_t target_idx = std::min(streaming_session_idx_, sessions_.size() - 1);
    auto& sess = sessions_[target_idx];

    AiChatMessage msg;
    msg.role = "assistant";
    msg.display_role = display_role;
    msg.content = parsed_reply.empty() ? "(思考已完成)" : parsed_reply;
    msg.reasoning = parsed_think;
    msg.thinking_sec = think_sec;
    msg.curation = sess.curation_mode;
    msg.error = is_error;
    msg.thinking_collapsed = false;
    sess.messages.push_back(std::move(msg));
    sess.updated_at = nowUnixSec();

    active_cancel_.reset();
    live_raw_content_.clear();
    live_reasoning_.clear();
    if (composer_) {
        composer_->setStreaming(false);
    }

    saveSessionsToDisk();

    if (target_idx == active_session_idx_) {
        if (messages_box_ && live_turn_box_) {
            if (focusIsInsideView(live_turn_box_)) {
                brls::Application::giveFocus(nullptr);
            }
            brls::Box* stale_live = live_turn_box_;
            resetLiveTurnRefs();
            messages_box_->removeView(stale_live, true);
            appendMessageTurnView(sess.messages.size() - 1);
        } else {
            rebuildThreadViews();
        }
        updateHeaderAndTabsStyling();
        updateStageVisibility();
        scrollToBottom(false);
    } else {
        resetLiveTurnRefs();
    }
}

void AiCompanionDock::triggerArchiveRehydrate() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    auto& caps = kernel.capabilities();
    if (!caps.has("recommend.feed.v1") && !caps.has("ai.curation.v1")) {
        brls::Application::notify("尚未安装 plugin-ai-curator，无法回灌历史推荐");
        return;
    }

    brls::Application::notify("正在后台同步 obsidian-vault 与 music-daily 历史归档...");
    const std::string cap = caps.has("recommend.feed.v1") ? "recommend.feed.v1" : "ai.curation.v1";
    const nlohmann::json req =
        caps.has("recommend.feed.v1")
            ? nlohmann::json{{"action", "bootstrap_and_rehydrate"}, {"pull_remote", true}}
            : nlohmann::json{{"action", "rehydrate_archive"}, {"pull_remote", true}};
    caps.callAsync(cap, req, [alive = alive_](const CapabilityResult& res) {
        brls::sync([alive, res]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (res.ok) {
                std::string msg = "双仓历史推荐回灌完成";
                if (res.data.is_object() && res.data.contains("dates_indexed")) {
                    msg += "（已索引 " + std::to_string(res.data.value("dates_indexed", 0)) + " 天归档）";
                }
                brls::Application::notify(msg);
            } else {
                brls::Application::notify("历史回灌失败: " + res.error);
            }
        });
    });
}

} // namespace omnidesk::host
