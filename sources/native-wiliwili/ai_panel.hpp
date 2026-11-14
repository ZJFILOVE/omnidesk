#pragma once
#include <yoga/Yoga.h>

// A companion reading column in the same activity as the video.
class AiReadingFrame : public brls::ScrollingFrame {
public:
    brls::View* getDefaultFocus() override { return this; }
    void willAppear(bool resetState) override {
        brls::ScrollingFrame::willAppear(resetState);
        updateScrollingOnNextFrame = false;
    }
    void onChildFocusGained(brls::View* directChild, brls::View* focusedView) override {
        brls::Box::onChildFocusGained(directChild, focusedView);
        childFocused = true;
        if (brls::Application::getInputType() != brls::InputType::GAMEPAD) return;
        // Keyboard navigation only reveals an offscreen action; it never centers
        // the reader itself or recenters a visible chapter during a rebuild.
        auto area = getFrame(), item = focusedView->getFrame();
        if (item.getWidth() <= 0 || item.getHeight() <= 0) return;
        float offset = getContentOffsetY();
        if (item.getMinY() < area.getMinY()) offset += item.getMinY() - area.getMinY();
        else if (item.getMaxY() > area.getMaxY()) offset += item.getMaxY() - area.getMaxY();
        setContentOffsetY(std::max(0.f, offset), true);
    }
    void draw(NVGcontext* vg, float x, float y, float width, float height, brls::Style style, brls::FrameContext* ctx) override {
        updateScrollingOnNextFrame = false;
        brls::ScrollingFrame::draw(vg, x, y, width, height, style, ctx);
    }
};

class AiPanel : public brls::Box {
public:
    AiPanel() {
        setId("ai/panel"); setAxis(brls::Axis::COLUMN); setPadding(20); setFocusable(true);
        setPositionType(brls::PositionType::ABSOLUTE);
        setBackgroundColor(color("desktop/surface")); setCornerRadius(12);
        setBorderThickness(1); setBorderColor(color("desktop/line"));
        auto* heading = new brls::Box(brls::Axis::ROW); heading->setAlignItems(brls::AlignItems::CENTER);
        auto* title = label("边看边读", 26); title->setGrow(1); heading->addView(title);
        close = makeButton("收起  F8", [this] { closeDock(); }, 112);
        close->setStyle(&brls::BUTTONSTYLE_BORDERLESS); heading->addView(close); addView(heading);
        videoTitle = label("", 17); videoTitle->setSingleLine(true); videoTitle->setMarginTop(4); addView(videoTitle);
        status = label("", 15, true); status->setSingleLine(true); status->setMarginTop(4); addView(status);
        subtitleBadge = label("", 14, true); subtitleBadge->setSingleLine(true); subtitleBadge->setMarginTop(2); addView(subtitleBadge);
        auto* tabs = new brls::Box(brls::Axis::ROW); tabs->setMarginTop(16); tabs->setMarginBottom(12);
        tabs->setBackgroundColor(color("desktop/soft")); tabs->setPadding(4); tabs->setCornerRadius(12);
        for (int i = 0; i < 5; ++i) {
            auto* item = makeButton(tabNames[i] + "  " + std::to_string(i + 1), [this, i] { selectTab(i); });
            item->setWidthPercentage(20); item->setHeight(44); item->setStyle(&brls::BUTTONSTYLE_BORDERLESS);
            tabs->addView(item); tabButtons.push_back(item);
            registerAction(key(std::to_string(i + 1)), [this, i](...) { selectTab(i); return true; });
        }
        addView(tabs);
        toolbar = new brls::Box(brls::Axis::COLUMN); toolbar->setMarginBottom(12);
        sources = label("", 14, true); sources->setMarginBottom(8); toolbar->addView(sources);
        generate = makeButton("生成总结 · F6", [] { AiHelper::instance().command("summary"); });
        generate->setWidthPercentage(100);
        generate->setStyle(&brls::BUTTONSTYLE_PRIMARY); toolbar->addView(generate); addView(toolbar);
        scrolling = new AiReadingFrame(); scrolling->setId("ai/reader");
        scrolling->setFocusable(true); scrolling->setHideHighlight(true);
        scrolling->setGrow(1); scrolling->setWidthPercentage(100);
        scrolling->setScrollingBehavior(brls::ScrollingBehavior::CENTERED);
        content = new brls::Box(brls::Axis::COLUMN); content->setWidthPercentage(100); content->setPadding(4, 8, 12, 0);
        scrolling->setContentView(content); addView(scrolling);
        auto* transport = new brls::Box(brls::Axis::ROW); transport->setMarginTop(12);
        auto* backward = makeButton("−5 秒", [] { MPVCore::instance().seekRelative(-5); });
        play = makeButton("暂停", [] { auto& mpv = MPVCore::instance(); if (mpv.video_paused) mpv.resume(); else mpv.pause(); });
        auto* forward = makeButton("+5 秒", [] { MPVCore::instance().seekRelative(5); });
        for (auto* control : {backward, play, forward}) { control->setWidthPercentage(33.333f); transport->addView(control); playbackButtons.push_back(control); }
        addView(transport);
        auto* bottom = new brls::Box(brls::Axis::ROW); bottom->setMarginTop(14); bottom->setAlignItems(brls::AlignItems::CENTER);
        askButton = makeButton("问问这个视频…   F7", [this] { selectTab(2); AiHelper::instance().ask(); });
        askButton->setGrow(1); askButton->setHeight(44); bottom->addView(askButton);
        addView(bottom);
        hint = label("", 14, true); hint->setMarginTop(8); addView(hint);
        registerAction("收起助手", brls::BUTTON_B, [this](...) { closeDock(); return true; }, true);
        registerAction(key("tab"), [this](...) { advanceFocus(1); return true; });
        registerAction(key("shift-tab"), [this](...) { advanceFocus(-1); return true; });
        for (auto spec : {std::pair<const char*, float>{"j", 72}, {"k", -72}, {"pgdn", 320}, {"pgup", -320}, {"ctrl-d", 260}, {"ctrl-u", -260}})
            registerAction(key(spec.first), [this, spec](...) { scrollBy(spec.second); return true; }, true);
        registerAction(key("h"), [this](...) { selectTab((selected + 4) % 5); return true; });
        registerAction(key("l"), [this](...) { selectTab((selected + 1) % 5); return true; });
        registerAction(key("shift-g"), [this](...) { scrollBy(100000); return true; });
        registerAction(key("home"), [this](...) { scrolling->setContentOffsetY(0, true); return true; });
        registerAction(key("end"), [this](...) { scrollBy(100000); return true; });
        registerAction(key("f6"), [this](...) { selectTab(0); AiHelper::instance().command("summary"); return true; });
        registerAction(key("f7"), [this](...) { selectTab(2); AiHelper::instance().ask(); return true; });
        rebuild();
    }
    brls::View* getDefaultFocus() override { return generate; }
    void attach(brls::Box* root, VideoView* player) {
        host = root; video = player;
        originalRight = host->getPaddingRight(); originalBottom = host->getPaddingBottom();
        if (!std::isfinite(originalRight)) originalRight = 0;
        if (!std::isfinite(originalBottom)) originalBottom = 0;
        left = host->getView("video_detail_left_box"); right = host->getView("video_detail_right_box");
        details = host->getView("video_detail_info_box"); entryButton = host->getView("video/ai/button");
        if (details) originalDetailsVisibility = details->getVisibility();
        if (entryButton) {
            originalEntryVisibility = entryButton->getVisibility();
            entryButton->setVisibility(brls::Visibility::GONE);
        }
        originalVideoWidth = YGNodeStyleGetWidth(video->getYGNode());
        video->setWidthPercentage(100);
        if (left) {
            originalLeftWidth = YGNodeStyleGetWidth(left->getYGNode()); originalLeftGrow = YGNodeStyleGetFlexGrow(left->getYGNode());
            if (!std::isfinite(originalLeftGrow)) originalLeftGrow = 0;
            left->setWidth(brls::View::AUTO); left->setGrow(1);
        }
        if (right) { originalRightVisibility = right->getVisibility(); right->setVisibility(brls::Visibility::GONE); }
        layoutDock();
    }
    bool hasReadingFocus() const {
        for (auto* view = brls::Application::getCurrentFocus(); view; view = view->getParent()) if (view == this) return true;
        return false;
    }
    void toggleFocus() { brls::Application::giveFocus(hasReadingFocus() ? static_cast<brls::View*>(video) : scrolling); }
    void closeDock() {
        if (closing) return;
        closing = true; DesktopUi::cancelHints(); ASYNC_RETAIN
        brls::sync([ASYNC_TOKEN]() {
            ASYNC_RELEASE
            brls::Application::giveFocus(video);
            host->setPaddingRight(originalRight); host->setPaddingBottom(originalBottom);
            restoreWidth(video, originalVideoWidth);
            if (left) { restoreWidth(left, originalLeftWidth); left->setGrow(originalLeftGrow); }
            if (right) right->setVisibility(originalRightVisibility);
            if (details) details->setVisibility(originalDetailsVisibility);
            if (entryButton) entryButton->setVisibility(originalEntryVisibility);
            host->removeView(this);
        });
    }
    Json diagnostics() const {
        auto rect = [](brls::View* view) { auto r = view->getFrame(); return Json{{"x", r.getMinX()}, {"y", r.getMinY()}, {"width", r.getWidth()}, {"height", r.getHeight()}}; };
        return {{"open", true}, {"layout", stacked ? "bottom" : "right"}, {"reading_focus", hasReadingFocus()},
                {"panel", rect(const_cast<AiPanel*>(this))}, {"video", rect(video)},
                {"rebuild_count", rebuildCount}, {"snapshot_revision", snapshotRevision},
                {"rendered_summary_bytes", renderedSummaryBytes}, {"rendered_segments", renderedSegments},
                {"rendered_messages", renderedMessages}, {"status", status->getFullText()}, {"title", videoTitle->getFullText()}};
    }
    void onLayout() override { brls::Box::onLayout(); layoutDock(); }
    void advanceFocus(int delta) {
        std::vector<brls::View*> items{close}; items.insert(items.end(), tabButtons.begin(), tabButtons.end());
        if (toolbar->getVisibility() == brls::Visibility::VISIBLE) items.push_back(generate);
        items.push_back(scrolling);
        items.insert(items.end(), actions.begin(), actions.end());
        items.insert(items.end(), playbackButtons.begin(), playbackButtons.end()); items.push_back(askButton);
        auto it = std::find(items.begin(), items.end(), brls::Application::getCurrentFocus());
        int index = it == items.end() ? 4 : (int)std::distance(items.begin(), it);
        index = (index + delta + items.size()) % items.size(); brls::Application::giveFocus(items[index]);
    }
    void selectTab(int value) {
        if (selected == value) return;
        offsets[selected] = scrolling->getContentOffsetY(); selected = value;
        // Tab callbacks can still be in the action traversal: never delete their view here.
        queueRebuild(true);
    }
    void scrollBy(float delta) {
        // Reading must not auto-recenter a previously focused chapter or button.
        brls::Application::giveFocus(scrolling);
        float maximum = std::max(0.f, content->getHeight() - scrolling->getHeight());
        scrolling->setContentOffsetY(std::clamp(scrolling->getContentOffsetY() + delta, 0.f, maximum), true);
    }
    std::string tabName() const { return tabNames[selected]; }
    float offset() const { return scrolling->getContentOffsetY(); }
    void draw(NVGcontext* vg, float x, float y, float w, float h, brls::Style style, brls::FrameContext* ctx) override {
        if (refreshData() && shape(panelData) != lastShape) queueRebuild(false);
        auto& player = MPVCore::instance();
        if (player.video_paused != lastPaused) {
            lastPaused = player.video_paused;
            play->setText(lastPaused ? "播放" : "暂停");
        }
        const int second = std::max(0, (int)player.playback_time);
        if (second != lastHintSecond || stacked != lastHintStacked) {
            lastHintSecond = second; lastHintStacked = stacked;
            hint->setText(stamp(second) + (stacked
                ? "  ·  Space 播放 / 暂停  ·  Ctrl+Tab 切换焦点"
                : "  ·  Space 播放 / 暂停\nCtrl+Tab 切换视频与阅读  ·  j / k 阅读"));
        }
        brls::Box::draw(vg, x, y, w, h, style, ctx);
    }
private:
    const std::vector<std::string> tabNames{"总结", "章节", "问答", "广告", "字幕"};
    brls::Label *status = nullptr, *subtitleBadge = nullptr, *sources = nullptr, *videoTitle = nullptr, *hint = nullptr;
    brls::ScrollingFrame* scrolling = nullptr;
    brls::Box *content = nullptr, *toolbar = nullptr;
    brls::Button *generate = nullptr, *askButton = nullptr, *close = nullptr, *play = nullptr;
    std::vector<brls::Button*> tabButtons;
    std::vector<brls::Button*> playbackButtons;
    std::vector<brls::View*> actions;
    int selected = 0; float offsets[5]{};
    bool queued = false, switching = false;
    std::string lastShape;
    Json panelData = Json::object();
    uint64_t snapshotRevision = 0;
    uint64_t rebuildCount = 0;
    size_t renderedSummaryBytes = 0, renderedSegments = 0, renderedMessages = 0;
    int lastHintSecond = -1;
    bool lastPaused = false, lastHintStacked = false;
    brls::Box* host = nullptr;
    VideoView* video = nullptr;
    brls::View *left = nullptr, *right = nullptr, *details = nullptr, *entryButton = nullptr;
    YGValue originalVideoWidth{}, originalLeftWidth{};
    brls::Visibility originalRightVisibility = brls::Visibility::VISIBLE;
    brls::Visibility originalDetailsVisibility = brls::Visibility::VISIBLE, originalEntryVisibility = brls::Visibility::VISIBLE;
    float originalRight = 0, originalBottom = 0, originalLeftGrow = 0, layoutWidth = 0, layoutHeight = 0;
    bool stacked = false, closing = false;
    bool refreshData() {
        if (!AiHelper::instance().snapshotIfChanged(snapshotRevision, panelData)) return false;
        auto title = panelData.value("video", Json::object()).value("title", "当前视频");
        auto message = panelData.value("status", "请先打开一个视频");
        std::string subLine;
        if (!panelData.value("enabled", true)) {
            subLine = "字幕：AI 已关闭";
        } else {
            auto subInfo = panelData.value("subtitle_info", Json::object());
            subLine = subInfo.value("summary", "");
            if (subLine.empty()) subLine = "正在检测可用字幕…";
        }
        // setText invalidates Yoga even when its value is unchanged.
        if (videoTitle->getFullText() != title) videoTitle->setText(title);
        if (status->getFullText() != message) status->setText(message);
        if (subtitleBadge && subtitleBadge->getFullText() != subLine) subtitleBadge->setText(subLine);
        return true;
    }
    static void restoreWidth(brls::View* view, YGValue value) {
        if (value.unit == YGUnitPercent) view->setWidthPercentage(value.value);
        else if (value.unit == YGUnitPoint) view->setWidth(value.value);
        else view->setWidth(brls::View::AUTO);
    }
    void layoutDock() {
        if (!host) return;
        float width = brls::Application::contentWidth, height = brls::Application::contentHeight;
        if (width == layoutWidth && height == layoutHeight) return;
        layoutWidth = width; layoutHeight = height; stacked = width / height < 1.45f;
        if (details) details->setVisibility(stacked ? brls::Visibility::GONE : originalDetailsVisibility);
        float dockWidth = std::clamp(width * .30f, 400.f, 560.f);
        float dockHeight = std::min(height * .48f, 520.f);
        host->setPaddingRight(stacked ? originalRight : originalRight + dockWidth + 16);
        host->setPaddingBottom(stacked ? originalBottom + dockHeight + 16 : originalBottom);
        setPositionRight(8); setPositionTop(stacked ? height - dockHeight - 8 : 8);
        setWidth(stacked ? width - 16 : dockWidth); setHeight(stacked ? dockHeight : height - 16);
    }
    NVGcolor color(const std::string& name) { return brls::Application::getTheme().getColor(name); }
    brls::Label* label(const std::string& value, int size = 21, bool muted = false) {
        auto* item = new brls::Label(); item->setText(value); item->setFontSize(size);
        item->setLineHeight(1.5f); item->setTextColor(color(muted ? "font/grey" : "brls/text")); return item;
    }
    brls::Button* makeButton(const std::string& value, std::function<void()> action, float width = 0) {
        auto* item = new brls::Button(); item->setText(value); item->setFontSize(18); item->setHeight(44);
        item->setStyle(&brls::BUTTONSTYLE_BORDERED);
        if (width) item->setWidth(width);
        item->setCornerRadius(10); item->setHighlightCornerRadius(10);
        item->registerClickAction([action](...) { action(); return true; }); return item;
    }
    brls::Box* card(const std::string& title = "") {
        auto* box = new brls::Box(brls::Axis::COLUMN); box->setWidthPercentage(100); box->setPadding(16);
        box->setBackgroundColor(color("desktop/soft")); box->setCornerRadius(8); box->setMarginBottom(12);
        if (!title.empty()) { auto* heading = label(title, 16, true); heading->setMarginBottom(10); box->addView(heading); }
        content->addView(box); return box;
    }
    void text(brls::Box* parent, const std::string& value, int size = 19, bool muted = false) {
        if (value.empty()) return;
        auto* item = label(value, size, muted); item->setId("ai/body"); item->setWidthPercentage(100); item->setMarginBottom(9); parent->addView(item);
    }
    static std::string inlineText(std::string value) {
        value = std::regex_replace(value, std::regex("\\[([^\\]]+)\\]\\([^\\)]+\\)"), "$1");
        value = std::regex_replace(value, std::regex("\\*\\*([^*]+)\\*\\*"), "$1");
        value = std::regex_replace(value, std::regex("`([^`]+)`"), "$1");
        return value;
    }
    void markdown(brls::Box* parent, const std::string& value) {
        std::istringstream lines(value); std::string line, paragraph;
        auto flush = [&] { if (!paragraph.empty()) { text(parent, inlineText(paragraph), 20); paragraph.clear(); } };
        while (std::getline(lines, line)) {
            if (line.empty()) { flush(); continue; }
            if (line.rfind("#", 0) == 0) {
                flush(); auto start = line.find_first_not_of("# ");
                if (start != std::string::npos) { auto* heading = label(inlineText(line.substr(start)), 23); heading->setMarginTop(12); heading->setMarginBottom(12); parent->addView(heading); }
            } else if (line.rfind("- ", 0) == 0 || line.rfind("* ", 0) == 0) {
                flush(); text(parent, "•  " + inlineText(line.substr(2)), 20);
            } else { if (!paragraph.empty()) paragraph += "\n"; paragraph += line; }
        }
        flush();
    }
    void citationButtons(brls::Box* parent, const std::string& value, uint64_t gen) {
        std::vector<double> targets;
        std::regex subRe(R"(〔\s*字幕\s*([0-9]{1,3}):([0-9]{2}))");
        for (std::sregex_iterator it(value.begin(), value.end(), subRe), end; it != end && targets.size() < 6; ++it) {
            double secs = std::stoi((*it)[1].str()) * 60.0 + std::stoi((*it)[2].str());
            if (std::none_of(targets.begin(), targets.end(), [secs](double t) { return std::abs(t - secs) < 1.0; }))
                targets.push_back(secs);
        }
        std::regex dmRe(R"(〔\s*弹幕\s*([0-9]{1,5})s)");
        for (std::sregex_iterator it(value.begin(), value.end(), dmRe), end; it != end && targets.size() < 6; ++it) {
            double secs = std::stod((*it)[1].str());
            if (std::none_of(targets.begin(), targets.end(), [secs](double t) { return std::abs(t - secs) < 1.0; }))
                targets.push_back(secs);
        }
        for (double t : targets) {
            action(parent, "跳到引用位置 · " + stamp(t), [t, gen] { AiHelper::instance().seekChapter(t, gen); });
        }
    }
    void action(brls::Box* parent, const std::string& value, std::function<void()> callback) {
        auto* item = makeButton(value, callback); item->setWidthPercentage(100); item->setMarginBottom(8);
        parent->addView(item); actions.push_back(item);
    }
    void chapters(const Json& data, uint64_t gen) {
        auto segments = data.value("segments", Json::array());
        if (segments.empty()) { text(card("视频章节"), "生成总结后，按时间查看要点；选择章节即可跳到对应位置。", 19, true); return; }
        for (auto& chapter : segments) {
            double start = chapter.value("start", 0.0), end = chapter.value("end", 0.0);
            bool ad = chapter.value("type", "content") == "ad";
            for (auto& item : data.value("ads", Json::array())) if (item.value("start", 0.0) < end && start < item.value("end", 0.0)) ad = true;
            auto* box = card();
            action(box, stamp(start) + "   " + chapter.value("label", "章节") + (ad ? "   · 广告" : ""), [start, gen] { AiHelper::instance().seekChapter(start, gen); });
            text(box, chapter.value("digest", ""), 18, true);
        }
    }
    std::string shape(const Json& data) {
        return std::to_string(data.value("generation", uint64_t(0))) + ":" + std::to_string(selected)
             + data.value("segments", Json::array()).dump() + data.value("ads", Json::array()).dump()
             + data.value("summary", "") + data.value("messages", Json::array()).dump() + data.value("sources", Json::object()).dump()
             + data.value("subtitle_info", Json::object()).dump() + data.value("subtitle_preview", Json::array()).dump()
             + SubtitleCore::instance().getCurrentSubtitleId()
             + std::to_string(SubtitleCore::instance().isAvailable())
             + std::to_string(SubtitleCore::instance().isShowing())
             + data.value("warnings", Json::array()).dump() + std::to_string(data.value("busy", false))
             + std::to_string(data.value("enabled", true)) + std::to_string(data.value("auto_skip", true))
             + data.value("engine_mode", "smart_fallback")
             + std::to_string((int)data.value("saved_total_seconds", 0.0));
    }
    void queueRebuild(bool switchFocus) {
        switching |= switchFocus;
        if (queued) return;
        queued = true; ASYNC_RETAIN
        brls::sync([ASYNC_TOKEN]() { ASYNC_RELEASE queued = false; rebuild(); });
    }
    void rebuild() {
        refreshData(); const auto& data = panelData; auto gen = data.value("generation", uint64_t(0));
        auto* focused = brls::Application::getCurrentFocus();
        auto it = std::find(actions.begin(), actions.end(), focused);
        bool contentFocus = it != actions.end(); size_t index = contentFocus ? std::distance(actions.begin(), it) : 0;
        float previousOffset = switching ? offsets[selected] : scrolling->getContentOffsetY();
        if (contentFocus) brls::Application::giveFocus(scrolling);
        content->clearViews(); actions.clear();
        for (int i = 0; i < 5; ++i) {
            tabButtons[i]->setBackgroundColor(color(i == selected ? "desktop/surface" : "desktop/soft"));
            tabButtons[i]->setTextColor(color(i == selected ? "brls/accent" : "font/grey"));
        }
        auto source = data.value("sources", Json::object());
        auto subInfo = data.value("subtitle_info", Json::object());
        std::string subSourceText;
        if (subInfo.value("available", false)) {
            subSourceText = fmt::format("{}({})", subInfo.value("line_count", source.value("subtitles", 0)),
                                        subInfo.value("selected_ai", false) ? "AI" : "人工");
        } else if (subInfo.value("status", "checking") == "checking") {
            subSourceText = "检测中";
        } else {
            subSourceText = "无";
        }
        sources->setText(fmt::format("字幕 {} · 弹幕 {} · 评论 {}", subSourceText, source.value("danmaku", 0), source.value("comments", 0)));
        bool busy = data.value("busy", false);
        toolbar->setVisibility(selected <= 1 ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        generate->setText(busy ? "正在整理视频…" : (data.value("summary", "").empty() ? "生成总结 · F6" : "重新生成 · F6"));
        generate->setState(busy || !data.value("enabled", true) ? brls::ButtonState::DISABLED : brls::ButtonState::ENABLED);
        if (!data.value("enabled", true)) {
            auto* box = card("AI 已关闭"); text(box, "开启后可以阅读视频总结、按章节跳转和提问。", 19, true);
            action(box, "开启 AI", [] { AiHelper::instance().toggleEnabled(); });
        } else if (selected == 0) {
            std::string summary = data.value("summary", "");
            if (summary.empty()) {
                auto* box = card(data.value("busy", false) ? "正在准备视频内容…" : "先读要点，再看视频");
                text(box, "生成一份可以直接阅读的总结，并把关键内容整理成可跳转的章节。", 20);
                std::string subHint = subInfo.value("summary", "正在检测可用字幕…");
                text(box, "字幕状态：" + subHint, 16, true);
                action(box, "查看可用字幕 · 5", [this] { selectTab(4); });
                text(box, "点击“生成总结”，或按 F6。", 17, true);
            } else {
                markdown(card("视频总结"), summary);
                if (!data.value("segments", Json::array()).empty()) { text(content, "视频分段", 22); chapters(data, gen); }
            }
        } else if (selected == 1) { chapters(data, gen); }
        else if (selected == 2) {
            auto messages = data.value("messages", Json::array());
            if (messages.empty()) {
                auto* box = card("围绕视频聊一聊"); text(box, "有哪里没看懂，或者想继续追问？", 22);
                text(box, "可问视频内容、延伸知识或其他问题。按 F7 提问。", 18, true);
            }
            for (auto& message : messages) {
                bool isUser = message.value("role", "") == "user";
                auto* msgBox = card(isUser ? "你" : "AI 助手");
                std::string body = message.value("content", "");
                markdown(msgBox, body);
                if (!isUser) citationButtons(msgBox, body, gen);
            }
        } else if (selected == 3) {
            auto* settings = card("广告设置");
            std::string mode = data.value("engine_mode", "smart_fallback");
            std::string modeLabel = mode == "hybrid_merge" ? "空降 + AI 双擎融合"
                : (mode == "sponsorblock_only" ? "仅空降助手 (0 Token)"
                : (mode == "ai_only" ? "仅 AI 识别" : "空降优先 + AI兜底"));
            action(settings, "识别引擎：" + modeLabel + "（点击切换）", [] { AiHelper::instance().cycleEngineMode(); });
            action(settings, data.value("auto_skip", true) ? "自动跳过：开启 · Ctrl+G" : "自动跳过：关闭 · Ctrl+G", [] { AiHelper::instance().toggleAuto(); });
            double savedTotal = data.value("saved_total_seconds", 0.0);
            if (savedTotal > 0.5) text(settings, fmt::format("本场累计省时：{:.0f} 秒", savedTotal), 15, true);
            action(settings, "重新识别广告", [] { AiHelper::instance().command("detect"); });
            action(settings, "开始 / 结束手动区间 · Ctrl+M", [] { AiHelper::instance().mark(); });
            action(settings, "关闭 AI · Ctrl+Shift+A", [] { AiHelper::instance().toggleEnabled(); });
            auto ads = data.value("ads", Json::array());
            if (ads.empty()) text(card(), "当前没有可跳过的广告区间。", 18, true);
            for (auto& ad : ads) {
                double start = ad.value("start", 0.0), end = ad.value("end", 0.0);
                std::string src = ad.value("source", "model");
                std::string srcBadge = src == "sponsorblock" ? "空降助手"
                    : (src == "manual" ? "手动补录" : (src == "summary" ? "AI 章节" : "AI 识别"));
                auto* box = card(stamp(start) + " — " + stamp(end));
                text(box, ad.value("product_name", "广告区间"), 22);
                text(box, fmt::format("{} · 置信度 {:.0f}%", srcBadge, ad.value("confidence", .5) * 100), 16, true);
                action(box, "跳到起点", [start, gen] { AiHelper::instance().seekChapter(start, gen); });
                action(box, "这段不是广告", [start, end, gen] {
                    if (gen == AiHelper::instance().currentGeneration()) AiHelper::instance().command("not_ad", {{"start", start}, {"end", end}});
                });
            }
            action(settings, "跳过当前区间 · F9", [] { AiHelper::instance().skip(); });
            action(settings, "这段想看 / 撤回跳过 · u / F10", [] { AiHelper::instance().stay(); });
        } else {
            auto* overview = card("可用字幕状态");
            text(overview, subInfo.value("summary", "正在检测可用字幕…"), 20);
            if (subInfo.contains("logged_in") && !subInfo["logged_in"].is_null()) {
                bool loggedIn = subInfo.value("logged_in", false);
                text(overview, loggedIn ? "B 站登录：已登录（支持获取 AI 字幕）"
                                        : "B 站登录：未登录（按 Ctrl+F5 扫码后可解锁 AI 字幕）", 16, true);
            }
            auto& playerSub = SubtitleCore::instance();
            std::string osdState = playerSub.isShowing()
                ? fmt::format("画面字幕：已开启 · {}（{} 行）", playerSub.getCurrentSubtitleLabel(), playerSub.getCurrentLineCount())
                : (playerSub.isAvailable() ? "画面字幕：已关闭（可按 c 一键开启）" : "画面字幕：暂无可用轨道");
            text(overview, osdState, 16, true);
            action(overview, "重新检测可用字幕", [] { AiHelper::instance().command("refresh_subtitles"); });
            action(overview, playerSub.isShowing() ? "隐藏画面字幕 · c" : "显示画面字幕 · c",
                   [] { AiHelper::instance().toggleSubtitle(); });

            auto tracks = subInfo.value("tracks", Json::array());
            auto* trackCard = card("可用字幕轨道");
            if (tracks.empty()) {
                std::string emptyReason = subInfo.value("status", "") == "login_required"
                    ? "当前未检测到可用字幕轨；未登录状态下 B 站通常会隐藏 AI 字幕。"
                    : "当前视频未返回可用字幕轨道。";
                text(trackCard, emptyReason, 17, true);
            } else {
                for (auto& tr : tracks) {
                    int idx = tr.value("index", 0);
                    std::string lan = tr.value("lan", "");
                    std::string labelStr = tr.value("lan_doc", lan);
                    bool isAi = tr.value("ai", false);
                    bool isSelected = tr.value("selected", false);
                    std::string btnTitle = (isSelected ? "✓ " : "") + labelStr + (isAi ? " · AI字幕" : " · UP主字幕");
                    action(trackCard, btnTitle, [idx, lan] { AiHelper::instance().selectSubtitleTrack(idx, lan); });
                }
            }

            auto preview = data.value("subtitle_preview", Json::array());
            if (!preview.empty()) {
                auto* previewCard = card(fmt::format("字幕内容预览（展示 {} 段 · 点击跳转）", preview.size()));
                for (auto& line : preview) {
                    double start = line.value("start", 0.0);
                    std::string lineText = line.value("text", "");
                    if (lineText.empty()) continue;
                    action(previewCard, stamp(start) + "  " + lineText, [start, gen] {
                        AiHelper::instance().seekChapter(start, gen);
                    });
                }
            }
        }
        for (auto& warning : data.value("warnings", Json::array())) text(content, warning.get<std::string>(), 15, true);
        lastShape = shape(data);
        ++rebuildCount;
        renderedSummaryBytes = data.value("summary", "").size();
        renderedSegments = data.value("segments", Json::array()).size();
        renderedMessages = data.value("messages", Json::array()).size();
        // Restore reading position after async content updates, rather than jumping to the top.
        scrolling->setContentOffsetY(previousOffset, false);
        if (switching) {
            switching = false;
            auto stack = brls::Application::getActivitiesStack();
            if (!stack.empty() && stack.back() == getParentActivity()) brls::Application::giveFocus(scrolling);
        }
        else if (contentFocus && index < actions.size()) brls::Application::giveFocus(actions[index]);
    }
};
