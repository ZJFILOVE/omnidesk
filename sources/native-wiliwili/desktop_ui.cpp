#include "utils/media_info.hpp"
#include "utils/desktop_ui.hpp"
#include "utils/session_ui.hpp"
#include "utils/ai_helper.hpp"
#include "utils/activity_helper.hpp"
#include "utils/config_helper.hpp"
#include "utils/shortcut_helper.hpp"
#include "view/recycling_grid.hpp"
#include "view/video_view.hpp"
#include "view/auto_tab_frame.hpp"
#include "view/button_refresh.hpp"
#include <borealis/views/edit_text_dialog.hpp>
#include <algorithm>
#include <chrono>
#include <regex>

namespace DesktopUi {
namespace {
struct Target { brls::View* view; brls::Rect rect; std::string label; };
std::vector<Target> targets;
brls::View* hintRoot = nullptr;
std::string prefix;
bool hinting = false;
size_t homeRefreshCount = 0;
auto lastG = std::chrono::steady_clock::time_point{};
brls::View* gRoot = nullptr;
const std::string alphabet = "asdfghjklqwertyuiopzxcvbnm";

brls::View* root() {
    auto stack = brls::Application::getActivitiesStack();
    return stack.empty() ? nullptr : stack.back()->getContentView();
}
bool editing() { return dynamic_cast<brls::EditTextDialog*>(root()) != nullptr; }
brls::Rect intersect(const brls::Rect& a, const brls::Rect& b) {
    float x = std::max(a.getMinX(), b.getMinX()), y = std::max(a.getMinY(), b.getMinY());
    return {x, y, std::max(0.f, std::min(a.getMaxX(), b.getMaxX()) - x),
                  std::max(0.f, std::min(a.getMaxY(), b.getMaxY()) - y)};
}
void collect(brls::View* view, brls::Rect clip, std::vector<Target>& output) {
    if (!view || view->getVisibility() != brls::Visibility::VISIBLE || view->getAlpha() < .1f) return;
    auto frame = intersect(view->getFrame(), clip);
    if (frame.getWidth() < 12 || frame.getHeight() < 12) return;
    bool clickable = false;
    for (const auto& action : view->getActions())
        if (action->getType() == brls::ACTION_GAMEPAD && action->getButton() == brls::BUTTON_A && action->isAvailable()) clickable = true;
    if (view->isFocusable() && clickable) { output.push_back({view, frame, ""}); return; }
    if (auto* box = dynamic_cast<brls::Box*>(view)) {
        if (dynamic_cast<brls::ScrollingFrame*>(view)) clip = frame;
        for (auto* child : box->getChildren()) collect(child, clip, output);
    }
}
std::vector<Target> visibleTargets() {
    std::vector<Target> result;
    collect(root(), {0, 0, brls::Application::contentWidth, brls::Application::contentHeight}, result);
    return result;
}
void startHints() {
    targets = visibleTargets();
    const size_t base = alphabet.size();
    size_t digits = 1, capacity = base;
    while (capacity < targets.size()) { ++digits; capacity *= base; }
    for (size_t i = 0; i < targets.size(); ++i) {
        size_t index = i;
        targets[i].label.assign(digits, alphabet[0]);
        for (size_t j = digits; j > 0; --j) { targets[i].label[j - 1] = alphabet[index % base]; index /= base; }
    }
    hintRoot = root(); prefix.clear(); hinting = !targets.empty();
}
bool stillVisible(brls::View* view) {
    auto visible = visibleTargets();
    return std::any_of(visible.begin(), visible.end(), [view](const Target& item) { return item.view == view; });
}
void scrollPage(bool end) {
    for (auto* view = brls::Application::getCurrentFocus(); view; view = view->getParent()) {
        if (auto* grid = dynamic_cast<RecyclingGrid*>(view)) {
            auto* source = grid->getDataSource();
            if (source && source->getItemCount()) grid->selectRowAt(end ? source->getItemCount() - 1 : 0, true);
            return;
        }
        if (auto* scroll = dynamic_cast<brls::ScrollingFrame*>(view)) {
            scroll->setContentOffsetY(end ? 100000 : 0, true); return;
        }
    }
}
void halfPage(float delta) {
    for (auto* view = brls::Application::getCurrentFocus(); view; view = view->getParent()) {
        if (auto* scroll = dynamic_cast<brls::ScrollingFrame*>(view)) {
            scroll->setContentOffsetY(std::max(0.f, scroll->getContentOffsetY() + delta * scroll->getHeight()), true);
            return;
        }
    }
    for (int i = 0; i < 4; ++i) brls::Application::onControllerButtonPressed(delta > 0 ? brls::BUTTON_NAV_DOWN : brls::BUTTON_NAV_UP, false);
}
// Use the visible native refresh button, including nested popular tabs.
// Do not dispatch BUTTON_X: live/ranking bind it to a category picker.
ButtonRefresh* refreshButton(brls::View* view) {
    if (!view || view->getVisibility() != brls::Visibility::VISIBLE) return nullptr;
    if (auto* button = dynamic_cast<ButtonRefresh*>(view)) return button;
    if (auto* box = dynamic_cast<brls::Box*>(view))
        for (auto* child : box->getChildren())
            if (auto* button = refreshButton(child)) return button;
    return nullptr;
}
bool handle(brls::BrlsKeyCombination key, bool repeating, bool fallback) {
    if (editing()) { cancelHints(); return fallback; }
    if (!fallback && !repeating && key.mod == brls::BRLS_KBD_MODIFIER_CTRL && key.code == brls::BRLS_KBD_KEY_F5) { SessionUi::open(); return true; }
    if (!fallback && !repeating && key.mod == brls::BRLS_KBD_MODIFIER_CTRL && key.code == brls::BRLS_KBD_KEY_F3) { NativeMediaInfo::open(); return true; }
    if (hinting && hintRoot != root()) cancelHints();
    if (!fallback && hinting) {
        if (key.code == brls::BRLS_KBD_KEY_ESCAPE) { cancelHints(); return true; }
        if (key.code == brls::BRLS_KBD_KEY_BACKSPACE) { if (!prefix.empty()) prefix.pop_back(); return true; }
        if (!repeating && key.mod == 0 && key.code >= brls::BRLS_KBD_KEY_A && key.code <= brls::BRLS_KBD_KEY_Z) {
            prefix += char('a' + key.code - brls::BRLS_KBD_KEY_A);
            bool match = false;
            for (auto& target : targets) {
                if (target.label.compare(0, prefix.size(), prefix) != 0) continue;
                match = true;
                if (target.label == prefix) {
                    auto* selected = target.view;
                    bool valid = stillVisible(selected);
                    cancelHints();
                    if (valid) {
                        if (brls::Application::getInputType() != brls::InputType::GAMEPAD)
                            brls::Application::onControllerButtonPressed(brls::BUTTON_A, false);
                        brls::Application::giveFocus(selected);
                        brls::Application::onControllerButtonPressed(brls::BUTTON_A, false);
                    }
                    return true;
                }
            }
            if (!match) prefix.clear();
        }
        return true;
    }
    if (!fallback && key.mod == 0 && key.code == brls::BRLS_KBD_KEY_R) {
        auto* tabs = root() ? dynamic_cast<AutoTabFrame*>(root()->getView("home/tab/frame")) : nullptr;
        if (tabs) {
            if (!repeating) if (auto* button = refreshButton(tabs->getActiveTab())) {
                for (const auto& action : button->getActions()) {
                    if (action->getType() == brls::ACTION_GAMEPAD && action->getButton() == brls::BUTTON_A && action->isAvailable()) {
                        auto listener = action->getActionListener();
                        if (listener && listener(button)) ++homeRefreshCount;
                        break;
                    }
                }
            }
            return true;
        }
    }
    if (!fallback && AiHelper::instance().handlePanelKey(key, repeating)) return true;
    if (!fallback) {
        if (key.mod == 0 && key.code == brls::BRLS_KBD_KEY_F && !repeating) { startHints(); return true; }
        if (key.mod == 0 && (key.code == brls::BRLS_KBD_KEY_SLASH || key.code == brls::BRLS_KBD_KEY_I) && !repeating) { openSearch(); return true; }
        if (key.mod == brls::BRLS_KBD_MODIFIER_SHIFT && key.code == brls::BRLS_KBD_KEY_SLASH && !repeating) { showHelp(); return true; }
        return false;
    }
    if (key.mod == brls::BRLS_KBD_MODIFIER_CTRL && (key.code == brls::BRLS_KBD_KEY_D || key.code == brls::BRLS_KBD_KEY_U)) {
        halfPage(key.code == brls::BRLS_KBD_KEY_D ? .65f : -.65f); return true;
    }
    if (key.mod == brls::BRLS_KBD_MODIFIER_SHIFT && key.code == brls::BRLS_KBD_KEY_H) {
        brls::Application::onControllerButtonPressed(brls::BUTTON_B, repeating); return true;
    }
    if (key.code == brls::BRLS_KBD_KEY_G && !repeating) {
        auto now = std::chrono::steady_clock::now();
        if (key.mod == brls::BRLS_KBD_MODIFIER_SHIFT) { scrollPage(true); lastG = {}; return true; }
        if (key.mod == 0) {
            if (gRoot == root() && now - lastG < std::chrono::milliseconds(650)) { scrollPage(false); lastG = {}; }
            else { lastG = now; gRoot = root(); }
            return true;
        }
    }
    lastG = {};
    if (key.mod == 0) {
        int button = -1;
        if (key.code == brls::BRLS_KBD_KEY_H) button = brls::BUTTON_NAV_LEFT;
        if (key.code == brls::BRLS_KBD_KEY_J) button = brls::BUTTON_NAV_DOWN;
        if (key.code == brls::BRLS_KBD_KEY_K) button = brls::BUTTON_NAV_UP;
        if (key.code == brls::BRLS_KBD_KEY_L) button = brls::BUTTON_NAV_RIGHT;
        if (button >= 0) { brls::Application::onControllerButtonPressed(static_cast<brls::ControllerButton>(button), repeating); return true; }
    }
    return false;
}
void drawHints(NVGcontext* vg) {
    if (!hinting) return;
    if (hintRoot != root() || editing()) { cancelHints(); return; }
    auto current = visibleTargets();
    nvgSave(vg); nvgResetScissor(vg);
    nvgFontFaceId(vg, brls::Application::getDefaultFont()); nvgFontSize(vg, 15);
    nvgTextAlign(vg, NVG_ALIGN_CENTER | NVG_ALIGN_MIDDLE);
    for (const auto& target : targets) {
        if (target.label.compare(0, prefix.size(), prefix) != 0) continue;
        auto found = std::find_if(current.begin(), current.end(), [&](const Target& item) { return item.view == target.view; });
        if (found == current.end()) continue;
        float x = found->rect.getMinX() + 6, y = found->rect.getMinY() + 6, w = 16 + target.label.size() * 11;
        nvgBeginPath(vg); nvgRoundedRect(vg, x, y, w, 26, 5);
        nvgFillColor(vg, nvgRGB(250, 211, 99)); nvgFill(vg);
        nvgStrokeColor(vg, nvgRGB(92, 69, 17)); nvgStrokeWidth(vg, 1); nvgStroke(vg);
        nvgFillColor(vg, nvgRGB(34, 29, 19)); nvgText(vg, x + w / 2, y + 13, target.label.c_str(), nullptr);
    }
    nvgRestore(vg);
}
}
void cancelHints() { hinting = false; targets.clear(); prefix.clear(); hintRoot = nullptr; }
void openSearch(const std::string& initial) {
    cancelHints();
    brls::Application::getImeManager()->openForText([](std::string text) {
        auto first = text.find_first_not_of(" \t\r\n"), last = text.find_last_not_of(" \t\r\n");
        if (first == std::string::npos) return;
        text = text.substr(first, last - first + 1);
        std::smatch match;
        if (std::regex_search(text, match, std::regex("BV[0-9A-Za-z]{10}"))) Intent::openBVInPlayer(match.str());
        else Intent::openSearch(text);
    }, "搜索", "输入关键词、BV 号或视频链接", 300, initial);
}
void showHelp() {
    cancelHints();
    auto* label = new brls::Label();
    label->setText("键盘操作\n\nCtrl+F5    登录与高清授权\nCtrl+F3    实际媒体信息（播放中）\n首页 1…5    直播 / 推荐 / 热门 / 追番 / 影视\n在列表任意位置直接切栏目，再按当前数字回到栏目按钮\nr    刷新首页当前栏目\n/ 或 i    搜索，输入关键词 / BV / 链接\nf    显示点击字母提示；Esc 退出提示\nh j k l    列表中向左 / 下 / 上 / 右移动\ngg / G    列表开头 / 结尾\nCtrl+D / Ctrl+U    向下 / 向上翻页\nH / Esc    返回    ·    Enter    打开\n\n视频播放\nh / l    后退 / 前进 5 秒    ·    H / L    30 秒\nj / k    音量减 / 加 5%    ·    Space    暂停\n, / .    倍速减 / 加 0.25    ·    =    恢复 1×\n0…9    跳到视频 0%…90%    ·    v    全屏\nF8    AI 阅读栏    ·    F9    跳过广告\n\nAI 阅读栏\nCtrl+Tab    在视频与阅读栏之间切换\nSpace    暂停 / 播放视频\n1…4 或 h / l    总结 / 章节 / 问答 / 广告\nj / k    滚动阅读    ·    Tab    选择操作\nF6    生成总结    ·    F7    提问\n\n输入框内直接打字，Ctrl+V 粘贴，Enter 提交，Esc 取消。");
    label->setFontSize(18); label->setLineHeight(1.4f); label->setWidthPercentage(100);
    auto* scroll = new brls::ScrollingFrame(); scroll->setHeight(std::min(580, (int)brls::Application::ORIGINAL_WINDOW_HEIGHT - 120));
    scroll->setContentView(label); scroll->setPadding(24, 24, 24, 24);
    auto* dialog = new brls::Dialog(scroll); dialog->addButton("关闭 · Esc", [] {}); dialog->open();
}
nlohmann::json diagnostics() {
    nlohmann::json result = {{"hint_mode", hinting}, {"hint_prefix", prefix}, {"hint_count", targets.size()}, {"editing", editing()}, {"font_scale", brls::Label::FONT_SCALE}};
    result["home_refresh_count"] = homeRefreshCount;
    result["window_width"] = brls::Application::windowWidth;
    result["window_height"] = brls::Application::windowHeight;
    result["window_scale"] = brls::Application::windowScale;
    result["activity_count"] = brls::Application::getActivitiesStack().size();
    result["player_fullscreen"] = false;
    result["grids"] = nlohmann::json::array();
    std::function<void(brls::View*)> inspect = [&](brls::View* view) {
        if (!view || view->getVisibility() != brls::Visibility::VISIBLE) return;
        if (auto* video = dynamic_cast<VideoView*>(view)) result["player_fullscreen"] = video->isFullscreen();
        if (auto* tabs = dynamic_cast<AutoTabFrame*>(view); tabs && tabs->getView("home/tab/frame") == tabs) {
            result["home_tab_index"] = tabs->getActiveIndex();
            result["home_tab_labels"] = nlohmann::json::array();
            for (size_t i = 0; i < tabs->getSidebar()->getChildren().size(); ++i)
                if (auto* tab = tabs->getTab(i)) result["home_tab_labels"].push_back(tab->getLabel());
        }
        if (auto* grid = dynamic_cast<RecyclingGrid*>(view)) {
            nlohmann::json entry = {{"view", grid->describe()}, {"offset", grid->getContentOffsetY()},
                                   {"row_height", grid->estimatedRowHeight}, {"columns", grid->spanCount},
                                   {"item_count", grid->getDataSource() ? grid->getItemCount() : 0}};
            for (auto* cell : grid->getGridItems()) {
                auto* image = dynamic_cast<brls::Image*>(cell->getView("video/card/picture"));
                if (!image) continue;
                entry["cover_width"] = image->getWidth();
                entry["cover_height"] = image->getHeight();
                entry["image_width"] = image->getOriginalImageWidth();
                entry["image_height"] = image->getOriginalImageHeight();
                entry["card_height"] = cell->getHeight();
                break;
            }
            result["grids"].push_back(entry);
        }
        if (auto* box = dynamic_cast<brls::Box*>(view)) for (auto* child : box->getChildren()) inspect(child);
    };
    inspect(root());
    for (auto* view = brls::Application::getCurrentFocus(); view; view = view->getParent()) {
        if (auto* tab = dynamic_cast<AutoSidebarItem*>(view); tab && !result.contains("focused_tab_label"))
            result["focused_tab_label"] = tab->getLabel();
        if (auto* cell = dynamic_cast<RecyclingGridItem*>(view)) result["focused_item_index"] = cell->getIndex();
    }
    if (root()) if (auto* label = dynamic_cast<brls::Label*>(root()->getView("ai/body")))
        result["body_font_pixels"] = label->getRenderedFontSize() * brls::Application::windowScale;
    result["hint_labels"] = nlohmann::json::array();
    result["hint_texts"] = nlohmann::json::array();
    for (auto& item : targets) {
        result["hint_labels"].push_back(item.label);
        auto* button = dynamic_cast<brls::Button*>(item.view);
        result["hint_texts"].push_back(button ? button->getText() : "");
    }
    if (auto* input = dynamic_cast<brls::EditTextDialog*>(root())) {
        result["input_text"] = input->getText();
        auto rect = input->getView("brls/dialog/applet")->getFrame();
        result["input_rect"] = {{"x", rect.getMinX()}, {"y", rect.getMinY()}, {"width", rect.getWidth()}, {"height", rect.getHeight()}};
    }
    return result;
}
void init() {
    brls::Label::FONT_SCALE = .82f;
    brls::Application::minimumWindowScale = .8f;
    brls::Application::maximumWindowScale = 1.f;
    brls::Application::setWindowSize(brls::Application::windowWidth, brls::Application::windowHeight);
    // Paper / graphite / Bilibili rose: content discovery and quiet companion reading.
    for (bool dark : {false, true}) {
        auto& theme = dark ? brls::Theme::getDarkTheme() : brls::Theme::getLightTheme();
        auto surface = dark ? nvgRGB(35, 33, 34) : nvgRGB(252, 251, 249);
        auto canvas = dark ? nvgRGB(24, 23, 24) : nvgRGB(246, 245, 243);
        auto text = dark ? nvgRGB(241, 238, 235) : nvgRGB(41, 39, 38);
        auto muted = dark ? nvgRGB(181, 174, 172) : nvgRGB(108, 103, 98);
        auto line = dark ? nvgRGB(65, 60, 62) : nvgRGB(224, 221, 216);
        auto accent = dark ? nvgRGB(255, 147, 180) : nvgRGB(194, 59, 98);
        auto soft = dark ? nvgRGB(43, 39, 41) : nvgRGB(241, 239, 236);
        auto active = dark ? nvgRGB(66, 38, 48) : nvgRGB(249, 231, 237);
        theme.addColor("brls/clear", canvas); theme.addColor("brls/background", canvas);
        theme.addColor("brls/text", text); theme.addColor("brls/text_disabled", muted);
        theme.addColor("brls/backdrop", nvgRGBA(24, 23, 24, 140));
        theme.addColor("brls/accent", accent); theme.addColor("color/bilibili", accent);
        theme.addColor("font/grey", muted); theme.addColor("color/line", line);
        theme.addColor("color/grey_1", surface); theme.addColor("color/grey_2", surface);
        theme.addColor("desktop/surface", surface); theme.addColor("desktop/line", line);
        theme.addColor("desktop/soft", soft); theme.addColor("desktop/active", active);
        theme.addColor("brls/highlight/background", active);
        theme.addColor("brls/highlight/color1", accent); theme.addColor("brls/highlight/color2", accent);
        theme.addColor("brls/sidebar/background", canvas); theme.addColor("brls/sidebar/separator", line);
        theme.addColor("brls/sidebar/active_item", accent);
        theme.addColor("brls/button/default_enabled_background", surface);
        theme.addColor("brls/button/default_enabled_text", text);
        theme.addColor("brls/button/default_disabled_background", canvas);
        theme.addColor("brls/button/default_disabled_text", muted);
        theme.addColor("brls/button/enabled_border_color", line);
        theme.addColor("brls/button/highlight_enabled_text", accent);
        theme.addColor("brls/button/primary_enabled_background", nvgRGB(194, 59, 98));
        theme.addColor("brls/button/primary_enabled_text", nvgRGB(255, 255, 255));
        theme.addColor("brls/button/primary_disabled_background", soft);
        theme.addColor("brls/button/primary_disabled_text", muted);
        theme.addColor("brls/list/listItem_value_color", accent);
        theme.addColor("brls/slider/line_filled", accent);
        theme.addColor("color/pink_1", active);
    }
    auto style = brls::getStyle();
    style.addMetric("brls/highlight/stroke_width", 2);
    style.addMetric("brls/highlight/shadow_opacity", 0);
    style.addMetric("brls/highlight/shadow_offset", 0);
    style.addMetric("brls/highlight/corner_radius", 10);
    style.addMetric("brls/button/corner_radius", 10);
    style.addMetric("brls/button/border_thickness", 1);
    style.addMetric("brls/button/padding_top_bottom", 10);
    style.addMetric("brls/button/shadow_width", 0);
    brls::Application::keyboardShortcutHandler = handle;
    brls::Application::keyboardOverlayRenderer = drawHints;
    brls::Application::controllerShortcutHandler = [](brls::ControllerButton button, bool) {
        if (!hinting) return false;
        if (button == brls::BUTTON_B) { cancelHints(); return true; }
        if (button == brls::BUTTON_A) return true;
        return false;
    };
    for (const auto& name : {"ctrl-f3", "ctrl-f5", "r", "h", "j", "k", "l", "f", "g", "i", "/", "shift-h", "shift-l", "shift-g", "shift-/", "ctrl-d", "ctrl-u", "v", ",", ".", "shift-,", "shift-.", "=", "tab", "shift-tab", "ctrl-tab", "escape", "backspace"}) {
        auto shortcut = ShortcutHelper::parseKey(name);
        if (shortcut.code != brls::BRLS_KBD_KEY_UNKNOWN) brls::Application::addToWatchedKeys(shortcut);
    }
    for (char c = 'a'; c <= 'z'; ++c) brls::Application::addToWatchedKeys({static_cast<brls::BrlsKeyboardScancode>(brls::BRLS_KBD_KEY_A + c - 'a')});
    for (char c = '0'; c <= '9'; ++c) brls::Application::addToWatchedKeys({static_cast<brls::BrlsKeyboardScancode>(brls::BRLS_KBD_KEY_0 + c - '0')});
}
}
