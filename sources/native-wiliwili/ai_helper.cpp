#include "utils/session_ui.hpp"
#include "utils/ai_helper.hpp"
#include "utils/shortcut_helper.hpp"
#include "utils/desktop_ui.hpp"
#include "view/mpv_core.hpp"
#include "view/subtitle_core.hpp"
#include "presenter/presenter.h"
#include "utils/activity_helper.hpp"
#include "activity/player_activity.hpp"
#include "view/video_view.hpp"
#include <borealis/views/edit_text_dialog.hpp>
#include <curl/curl.h>
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <regex>
#include <sstream>

using Json = AiHelper::Json;
static std::string stamp(double time) {
    int seconds = std::max(0, (int)time);
    return fmt::format("{:02d}:{:02d}", seconds / 60, seconds % 60);
}
static brls::BrlsKeyCombination key(const std::string& value) {
    auto result = ShortcutHelper::parseKey(value);
    brls::Application::addToWatchedKeys(result);
    return result;
}
static size_t receive(char* data, size_t size, size_t count, void* output) {
    auto* text = static_cast<std::string*>(output);
    if (text->size() + size * count > 4 * 1024 * 1024) return 0;
    text->append(data, size * count);
    return size * count;
}

#include "utils/ai_panel.hpp"

static AiPanel* findPanel() {
    auto stack = brls::Application::getActivitiesStack();
    if (stack.empty()) return nullptr;
    return dynamic_cast<AiPanel*>(stack.back()->getView("ai/panel"));
}
static VideoView* findVideo(brls::View* view) {
    if (auto* video = dynamic_cast<VideoView*>(view)) return video;
    if (auto* box = dynamic_cast<brls::Box*>(view))
        for (auto* child : box->getChildren()) if (auto* video = findVideo(child)) return video;
    return nullptr;
}

AiHelper& AiHelper::instance() { static AiHelper value; return value; }
AiHelper::AiHelper() { worker = std::thread(&AiHelper::run, this); }
AiHelper::~AiHelper() { shutdown(); }
void AiHelper::shutdown() {
    { std::lock_guard<std::mutex> lock(mutex); stopping = true; }
    changed.notify_all();
    if (worker.joinable()) worker.join();
}
uint64_t AiHelper::currentGeneration() { std::lock_guard<std::mutex> lock(mutex); return generation; }
bool AiHelper::snapshotIfChanged(uint64_t& revision, Json& output) {
    std::lock_guard<std::mutex> lock(mutex);
    if (revision == stateRevision) return false;
    output = state;
    output["enabled"] = enabled;
    output["auto_skip"] = autoSkip;
    output["generation"] = generation;
    output["saved_total_seconds"] = savedTotalSeconds;
    if (!output.contains("engine_mode")) output["engine_mode"] = "smart_fallback";
    revision = stateRevision;
    return true;
}
void AiHelper::activate(const Json& video, void* activity) {
    std::lock_guard<std::mutex> lock(mutex);
    if (owner == activity && desired.value("bvid", "") == video.value("bvid", "") && desired.value("cid", uint64_t(0)) == video.value("cid", uint64_t(0))) return;
    ++generation; owner = activity; desired = video; ready = false;
    commands.clear(); optedOut.clear(); lastSkipped = Json::object(); markingStart = -1; countdownKey.clear(); toast.clear();
    savedTotalSeconds = 0; lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
    state = {{"status", "正在连接本机助手"}, {"video", video}, {"ads", Json::array()}, {"engine_mode", "smart_fallback"},
             {"subtitle_info", {{"status", "checking"}, {"available", false}, {"summary", "正在检测可用字幕…"}, {"tracks", Json::array()}}},
             {"subtitle_preview", Json::array()}};
    ++stateRevision;
    changed.notify_all();
}
void AiHelper::invalidate(void* activity) {
    std::lock_guard<std::mutex> lock(mutex);
    if (activity && owner != activity) return;
    ++generation; desired = Json::object(); owner = nullptr; ready = false;
    commands.clear(); optedOut.clear(); lastSkipped = Json::object(); markingStart = -1; countdownKey.clear(); toast.clear();
    savedTotalSeconds = 0; lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
    state = {{"status", "请先打开视频"}, {"ads", Json::array()}, {"subtitle_info", Json::object()}, {"subtitle_preview", Json::array()}};
    ++stateRevision;
    changed.notify_all();
}
void AiHelper::playerReady(void* activity) { std::lock_guard<std::mutex> lock(mutex); if (owner == activity) ready = true; }
void AiHelper::beforePlayerUrl(void* activity) {
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (owner == activity) { ready = false; countdownKey.clear(); return; }
    }
    // A live/season/other activity also uses global libmpv. Never let a previous
    // ordinary-video session seek its replacement before the new activity binds.
    invalidate();
}
void AiHelper::localStatus(const std::string& message) {
    std::lock_guard<std::mutex> lock(mutex);
    state["status"] = message; ++stateRevision; toast = message;
    toastUntil = std::chrono::steady_clock::now() + std::chrono::seconds(5);
}
Json AiHelper::request(const std::string& method, const std::string& path, const Json& body) {
    const char* auth = std::getenv("WILI_AI_TOKEN");
    if (!auth) return {{"error", "请通过 wiliwili-ai 启动器运行"}};
    CURL* curl = curl_easy_init();
    if (!curl) return {{"error", "本机助手连接失败"}};
    const char* port = std::getenv("WILI_AI_PORT");
    const std::string address = port && std::all_of(port, port + std::strlen(port), [](unsigned char c){ return std::isdigit(c); }) ? port : "43189";
    std::string url = "http://127.0.0.1:" + address + path, input = body.dump(), output;
    curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    std::string authorization = std::string("X-Wili-Auth: ") + auth;
    headers = curl_slist_append(headers, authorization.c_str());
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_PROXY, "");
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 1800L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 500L);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &output);
    if (method == "POST") { curl_easy_setopt(curl, CURLOPT_POSTFIELDS, input.c_str()); }
    auto result = curl_easy_perform(curl);
    curl_slist_free_all(headers); curl_easy_cleanup(curl);
    if (result != CURLE_OK) return {{"error", "本机助手未连接，请检查启动器"}};
    try { return Json::parse(output); } catch (...) { return {{"error", "本机助手响应格式错误"}}; }
}
void AiHelper::run() {
    uint64_t seen = UINT64_MAX;
    std::string session;
    while (true) {
        Json video, action = Json::object(); bool active, done; uint64_t gen;
        {
            std::unique_lock<std::mutex> lock(mutex);
            changed.wait_for(lock, std::chrono::milliseconds(400), [&] { return stopping || generation != seen || !commands.empty(); });
            done = stopping; gen = generation; video = desired; active = enabled && !video.empty();
            if (!commands.empty()) { if (commands.front().first == gen) action = commands.front().second; commands.pop_front(); }
        }
        if (done || gen != seen) {
            if (!session.empty()) request("DELETE", "/session/" + session);
            session.clear(); seen = gen;
            if (done) break;
        }
        if (!active) continue;
        Json response;
        if (session.empty()) {
            Json initVideo = video;
            initVideo["position"] = MPVCore::instance().playback_time;
            response = request("POST", "/session", {{"video", initVideo}});
            session = response.value("id", "");
            if (!action.empty() && !session.empty()) response = request("POST", "/session/" + session + "/command", action);
        } else if (!action.empty()) {
            response = request("POST", "/session/" + session + "/command", action);
        } else response = request("GET", "/session/" + session);
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (gen != generation || !enabled || desired != video) continue;
            if (response.contains("error")) {
                if (state["status"] != response["error"]) { state["status"] = response["error"]; ++stateRevision; }
            }
            else {
                std::string identity = video.value("bvid", "") + ":" + std::to_string(video.value("cid", uint64_t(0))) + ":" + std::to_string(video.value("p", 1));
                if (response.value("identity", "") == identity && response.value("id", "") == session && state != response) {
                    state = std::move(response); ++stateRevision;
                }
            }
        }
    }
}
void AiHelper::command(const std::string& action, Json args) {
    std::lock_guard<std::mutex> lock(mutex);
    if (!enabled || desired.empty()) { state["status"] = "请先打开视频并开启 AI"; ++stateRevision; return; }
    if (commands.size() >= 5 && action != "not_ad" && action != "manual") return;
    args["action"] = action;
    if (action == "detect") {
        args["refresh"] = true;
        args["position"] = MPVCore::instance().playback_time;
    }
    if (action == "not_ad") {
        // Suppress immediately; a queued model reply cannot trigger a skip before feedback is saved.
        auto ads = state.value("ads", Json::array()); Json keep = Json::array();
        for (auto& ad : ads) {
            if (args.value("start", 0.0) < ad.value("end", 0.0) && ad.value("start", 0.0) < args.value("end", 0.0)) {
                optedOut.insert(adKey(ad));
            } else keep.push_back(ad);
        }
        state["ads"] = keep;
        countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
        lastSkipped = Json::object();
    }
    commands.emplace_back(generation, args); state["status"] = "请求已排队"; ++stateRevision; changed.notify_all();
}
void AiHelper::ask() {
    if (!findPanel()) openPanel();
    if (auto* panel = findPanel()) panel->selectTab(2);
    auto gen = currentGeneration();
    brls::Application::getImeManager()->openForText([gen](std::string question) {
        auto& ai = AiHelper::instance();
        if (gen == ai.currentGeneration() && !question.empty()) {
            ai.command("ask", {{"question", question}, {"position", MPVCore::instance().playback_time}});
        }
    }, "视频提问", "可问视频、延伸知识或其他问题；Enter 发送，Esc 取消", 2000);
    auto stack = brls::Application::getActivitiesStack();
    if (!stack.empty()) if (auto* input = dynamic_cast<brls::EditTextDialog*>(stack.back()->getContentView())) input->setDockTarget("ai/panel");
}
std::string AiHelper::adKey(const Json& ad) { return fmt::format("{}:{}", ad.value("start", 0.0), ad.value("end", 0.0)); }
Json AiHelper::currentAd(double time, bool recent) {
    const auto ads = state.find("ads");
    if (ads != state.end())
        for (const auto& ad : *ads) if (ad.value("start", 0.0) <= time && time < ad.value("end", 0.0)) return ad;
    if (recent && !lastSkipped.empty() && std::chrono::steady_clock::now() < toastUntil) return lastSkipped;
    return Json::object();
}
Json AiHelper::preRollAd(double time) {
    const auto ads = state.find("ads");
    if (ads == state.end()) return Json::object();
    constexpr double kPreRollSeconds = 3.0;
    Json best = Json::object();
    double bestStart = 1e18;
    for (const auto& ad : *ads) {
        double start = ad.value("start", 0.0);
        if (ad.value("confidence", .5) >= .7 && !optedOut.count(adKey(ad)) && time >= start - kPreRollSeconds && time < start && start < bestStart) {
            best = ad;
            bestStart = start;
        }
    }
    return best;
}
void AiHelper::skip() {
    std::lock_guard<std::mutex> lock(mutex);
    auto& player = MPVCore::instance();
    if (!enabled || !ready || player.video_stopped) return;
    auto ad = currentAd(player.playback_time);
    if (ad.empty()) ad = preRollAd(player.playback_time);
    if (ad.empty()) return;
    double endTime = ad.value("end", 0.0);
    double saved = std::max(0.0, endTime - player.playback_time);
    savedTotalSeconds += saved;
    lastSkipped = ad; skippedFrom = ad.value("start", 0.0); optedOut.insert(adKey(ad));
    countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
    player.seek(fmt::format("{:.3f}", endTime));
    toast = fmt::format("已跳过广告（省时 {:.0f}s · 累计 {:.0f}s）· u / F10 撤回重看", saved, savedTotalSeconds);
    toastUntil = std::chrono::steady_clock::now() + std::chrono::seconds(12);
    ++stateRevision;
    player.showOsdText(toast, 12000);
}
void AiHelper::stay() {
    std::lock_guard<std::mutex> lock(mutex);
    auto& player = MPVCore::instance();
    if (!enabled || !ready || player.video_stopped) return;
    if (!lastSkipped.empty() && std::chrono::steady_clock::now() < toastUntil) {
        optedOut.insert(adKey(lastSkipped));
        countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
        player.seek(fmt::format("{:.3f}", skippedFrom));
        lastSkipped = Json::object();
        toast = "已撤回跳过，回到广告起点 · 本次播放不再跳过";
        toastUntil = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        ++stateRevision;
        player.showOsdText(toast, 4000);
        return;
    }
    auto ad = preRollAd(player.playback_time);
    if (ad.empty()) ad = currentAd(player.playback_time);
    if (ad.empty()) return;
    optedOut.insert(adKey(ad)); countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
    toast = "这段想看 · 本次播放不再跳过";
    toastUntil = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    ++stateRevision;
    player.showOsdText(toast, 3500);
}
void AiHelper::notAd() {
    Json ad;
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (!enabled || !ready) return;
        double pos = MPVCore::instance().playback_time;
        ad = currentAd(pos, true);
        if (ad.empty()) ad = preRollAd(pos);
    }
    if (!ad.empty()) {
        command("not_ad", {{"start", ad["start"]}, {"end", ad["end"]}});
        MPVCore::instance().showOsdText("已排除该区间 · 永久生效", 3000);
    }
}
void AiHelper::mark() {
    Json args; bool startOnly = false;
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (!enabled || !ready || MPVCore::instance().video_stopped) return;
        double position = MPVCore::instance().playback_time;
        if (markingStart < 0) { markingStart = position; startOnly = true; }
        else { if (position - markingStart < 2) { state["status"] = "结束时间须在起点至少 2 秒之后"; ++stateRevision; return; } args = {{"start", markingStart}, {"end", position}}; markingStart = -1; }
    }
    if (startOnly) {
        localStatus("已记录广告起点 · Ctrl+M 记录终点");
        MPVCore::instance().showOsdText("已记录广告起点 · 再按 Ctrl+M 记录终点", 3500);
    } else {
        command("manual", args);
        MPVCore::instance().showOsdText("已补录广告区间并保存", 3000);
    }
}
void AiHelper::cancelMark() {
    { std::lock_guard<std::mutex> lock(mutex); markingStart = -1; }
    localStatus("已取消手动广告区间");
    MPVCore::instance().showOsdText("已取消手动广告打点", 2500);
}
void AiHelper::toggleAuto() {
    std::lock_guard<std::mutex> lock(mutex); autoSkip = !autoSkip; countdownKey.clear();
    lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
    state["status"] = autoSkip ? "自动跳过已开启（提前 3 秒预告，到达起点 0 延迟直跳）" : "自动跳过已关闭";
    ++stateRevision;
}
void AiHelper::cycleEngineMode() {
    std::string nextMode;
    {
        std::lock_guard<std::mutex> lock(mutex);
        std::string cur = state.value("engine_mode", "smart_fallback");
        if (cur == "smart_fallback") nextMode = "hybrid_merge";
        else if (cur == "hybrid_merge") nextMode = "sponsorblock_only";
        else if (cur == "sponsorblock_only") nextMode = "ai_only";
        else nextMode = "smart_fallback";
    }
    command("set_engine_mode", {{"mode", nextMode}});
}
void AiHelper::toggleEnabled() {
    std::lock_guard<std::mutex> lock(mutex); enabled = !enabled; ++generation;
    commands.clear(); state = {{"video", desired}, {"ads", Json::array()}, {"status", enabled ? "正在连接" : "AI 已关闭，请求已取消"}};
    ++stateRevision;
    countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1; markingStart = -1; changed.notify_all();
}
void AiHelper::toggleSubtitle() {
    SubtitleCore::instance().toggleSubtitle();
    std::lock_guard<std::mutex> lock(mutex);
    ++stateRevision;
}
void AiHelper::selectSubtitleTrack(int index, const std::string& lan) {
    if (!lan.empty()) SubtitleCore::instance().selectSubtitleByLan(lan);
    command("select_subtitle", {{"index", index}, {"lan", lan}});
}
void AiHelper::seekChapter(double time, uint64_t gen) {
    std::lock_guard<std::mutex> lock(mutex);
    if (gen != generation || !enabled || !ready || MPVCore::instance().video_stopped || time < 0 || time >= desired.value("duration", 0.0)) return;
    MPVCore::instance().seek((int64_t)time);
    toast = "已跳转到 " + stamp(time); toastUntil = std::chrono::steady_clock::now() + std::chrono::seconds(4);
}
void AiHelper::navigatePart(int delta) {
    void* current; int target;
    {
        std::lock_guard<std::mutex> lock(mutex);
        current = owner; target = desired.value("p", 1) - 1 + delta;
    }
    if (target < 0) return;
    for (auto* activity : brls::Application::getActivitiesStack()) {
        if (activity == current) {
            if (auto* player = dynamic_cast<PlayerActivity*>(activity)) player->onIndexChange(target);
            return;
        }
    }
}
void AiHelper::tick() {
    std::lock_guard<std::mutex> lock(mutex);
    auto& player = MPVCore::instance();
    // Explicit test diagnostics only. Never contains bridge credentials, cookies, or request headers.
    if (const char* diagnostics = std::getenv("WILI_AI_DIAGNOSTICS")) {
        static auto last = std::chrono::steady_clock::now();
        auto now = std::chrono::steady_clock::now();
        if (now - last > std::chrono::milliseconds(200)) {
            last = now;
            Json copy = state; copy["generation"] = generation; copy["position"] = player.playback_time;
            copy["ready"] = ready; copy["enabled"] = enabled; copy["auto_skip"] = autoSkip;
            copy["speed"] = player.getSpeed(); copy["volume"] = player.volume;
            const auto& media = player.mediaDiagnostics;
            copy["media"] = {{"width", media.width}, {"height", media.height}, {"duration", media.duration},
                              {"codec", media.videoCodec}, {"audio_codec", media.audioCodec},
                              {"dropped_frames", media.droppedFrames},
                              {"decoder_dropped_frames", media.decoderDroppedFrames},
                              {"hwdec_current", media.hardwareDecoder}};
            copy["desktop"] = DesktopUi::diagnostics();
            copy["auth"] = SessionUi::diagnostics();
            copy["media"]["fps"] = media.fps;
            copy["media"]["video_bitrate"] = media.videoBitrate;
            copy["media"]["audio_bitrate"] = media.audioBitrate;
            copy["media"]["audio_channels"] = media.audioChannels;
            copy["media"]["audio_rate"] = media.audioRate;
            copy["media"]["selected_quality"] = media.selectedQuality;
            copy["media"]["source"] = media.source;
            copy["dock"] = panelDiagnostics();
            for (auto* view = brls::Application::getCurrentFocus(); view; view = view->getParent()) {
                if (auto* panel = dynamic_cast<AiPanel*>(view)) { copy["panel_tab"] = panel->tabName(); copy["reader_offset"] = panel->offset(); break; }
            }
            copy["paused"] = player.video_paused; copy["stopped"] = player.video_stopped;
            copy["countdown"] = countdownKey; copy["toast"] = toast; copy["marking_start"] = markingStart;
            auto* focus = brls::Application::getCurrentFocus(); copy["focus"] = focus ? focus->describe() : "";
            if (auto* button = dynamic_cast<brls::Button*>(focus)) copy["focus_text"] = button->getText();
            std::ofstream(diagnostics) << copy.dump(2);
        }
    }
    if (!enabled || !ready || player.video_stopped || player.video_paused) {
        countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
        return;
    }
    auto ad = currentAd(player.playback_time);
    auto now = std::chrono::steady_clock::now();
    if (!ad.empty() && !optedOut.count(adKey(ad))) {
        auto id = adKey(ad);
        if (autoSkip && ad.value("confidence", .5) >= .7 && ad.value("end", 0.0) - player.playback_time > 0.5) {
            double endTime = ad.value("end", 0.0);
            double saved = std::max(0.0, endTime - player.playback_time);
            savedTotalSeconds += saved;
            lastSkipped = ad; skippedFrom = ad.value("start", 0.0); optedOut.insert(id);
            countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
            player.seek(fmt::format("{:.3f}", endTime));
            toast = fmt::format("已自动跳过广告（省时 {:.0f}s · 累计 {:.0f}s）· u / F10 撤回重看", saved, savedTotalSeconds);
            toastUntil = now + std::chrono::seconds(12);
            ++stateRevision;
            player.showOsdText(toast, 12000);
        } else {
            if (countdownKey != id) { countdownKey = id; entered = now; }
        }
        return;
    }
    if (autoSkip) {
        auto pre = preRollAd(player.playback_time);
        if (!pre.empty()) {
            auto id = adKey(pre);
            if (countdownKey != id) { countdownKey = id; entered = now; }
            int left = std::max(1, (int)std::ceil(pre.value("start", 0.0) - player.playback_time));
            if (lastPreRollOsdKey != id || lastPreRollOsdSec != left) {
                lastPreRollOsdKey = id; lastPreRollOsdSec = left;
                player.showOsdText(fmt::format("即将跳过广告 · {} 秒后直跳 · u / F10 这段想看", left), 1200);
            }
            return;
        }
    }
    countdownKey.clear(); lastPreRollOsdKey.clear(); lastPreRollOsdSec = -1;
}
void AiHelper::drawMarks(NVGcontext* vg, float x, float y, float width, float height, float alpha) {
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (!enabled) return;
        if (marksRevision != stateRevision) {
            cachedMarks.clear();
            const auto video = state.find("video"), ads = state.find("ads");
            const double duration = video == state.end() ? 0.0 : video->value("duration", 0.0);
            if (duration > 0 && ads != state.end()) {
                cachedMarks.reserve(ads->size());
                for (const auto& ad : *ads) cachedMarks.push_back({
                    std::clamp(ad.value("start", 0.0) / duration, 0.0, 1.0),
                    std::clamp(ad.value("end", 0.0) / duration, 0.0, 1.0),
                    ad.value("confidence", .5) >= .7});
            }
            marksRevision = stateRevision;
        }
    }
    if (cachedMarks.empty()) return;
    nvgSave(vg);
    for (const auto& mark : cachedMarks) {
        auto color = mark.confident ? nvgRGB(255, 114, 84) : nvgRGB(255, 198, 67); color.a = alpha;
        nvgBeginPath(vg); nvgRect(vg, x + mark.start * width, y, (mark.end - mark.start) * width, height); nvgFillColor(vg, color); nvgFill(vg);
    }
    nvgRestore(vg);
}
void AiHelper::drawHint(NVGcontext* vg, float x, float y, float width, float height, float alpha) {
    std::string label;
    bool notice = false;
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (!enabled || desired.empty() || width < 300) return;
        auto now = std::chrono::steady_clock::now();
        if (now < toastUntil) { label = toast; notice = true; }
        else if (markingStart >= 0) { label = "手动广告起点 " + stamp(markingStart) + " · Ctrl+M 结束"; notice = true; }
        else if (!countdownKey.empty()) {
            notice = true;
            double pos = MPVCore::instance().playback_time;
            auto pre = preRollAd(pos);
            if (!pre.empty() && autoSkip) {
                int left = std::max(1, (int)std::ceil(pre.value("start", 0.0) - pos));
                label = fmt::format("即将跳过广告 · {} 秒后 0 延迟直跳 · u / F10 这段想看 · F9 立即跳", left);
            } else {
                label = "疑似广告 · F9 跳过 / u·F10 想看 / Ctrl+F10 不是广告";
            }
        } else label = "AI 助手  F8";
    }
    // Routine status belongs in the reading column. Only short-lived playback notices use a banner.
    if (!notice && findPanel()) return;
    float hintWidth = notice ? width - 28 : 150;
    nvgSave(vg); nvgBeginPath(vg); nvgRoundedRect(vg, x + width - hintWidth - 14, y + 14, hintWidth, 34, 8);
    nvgFillColor(vg, nvgRGBA(20, 24, 35, (unsigned char)(190 * alpha))); nvgFill(vg);
    nvgFontFaceId(vg, brls::Application::getDefaultFont()); nvgFontSize(vg, 16);
    nvgTextAlign(vg, NVG_ALIGN_LEFT | NVG_ALIGN_MIDDLE); nvgFillColor(vg, nvgRGBA(255, 255, 255, (unsigned char)(255 * alpha)));
    nvgScissor(vg, x + width - hintWidth - 10, y + 15, hintWidth - 8, 32);
    nvgText(vg, x + width - hintWidth, y + 31, label.c_str(), nullptr); nvgRestore(vg);
}
void AiHelper::openPanel() {
    if (auto* panel = findPanel()) { panel->closeDock(); return; }
    auto stack = brls::Application::getActivitiesStack();
    if (stack.empty()) return;
    auto* root = dynamic_cast<brls::Box*>(stack.back()->getContentView());
    auto* video = findVideo(root);
    if (!root || !video) return;
    auto* panel = new AiPanel();
    panel->attach(root, video); root->addView(panel);
    brls::Application::giveFocus(panel->getDefaultFocus());
}
bool AiHelper::handlePanelKey(brls::BrlsKeyCombination pressed, bool repeating) {
    auto* panel = findPanel();
    if (!panel) return false;
    if (pressed.code == brls::BRLS_KBD_KEY_TAB && pressed.mod == brls::BRLS_KBD_MODIFIER_CTRL) {
        if (!repeating) panel->toggleFocus();
        return true;
    }
    if (panel->hasReadingFocus() && pressed.mod == 0 && pressed.code == brls::BRLS_KBD_KEY_ESCAPE) {
        if (!repeating) panel->closeDock();
        return true;
    }
    return false;
}
Json AiHelper::panelDiagnostics() {
    auto stack = brls::Application::getActivitiesStack();
    for (auto it = stack.rbegin(); it != stack.rend(); ++it)
        if (auto* panel = dynamic_cast<AiPanel*>((*it)->getView("ai/panel"))) return panel->diagnostics();
    return {{"open", false}};
}
void AiHelper::registerActions(brls::Activity* activity) {
    activity->registerAction(key("ctrl-l"), [](...) {
        brls::Application::getImeManager()->openForText([](std::string input) {
            std::smatch match;
            if (std::regex_search(input, match, std::regex("BV[0-9A-Za-z]{10}"))) Intent::openBVInPlayer(match.str());
        }, "打开 B 站视频", "输入 BV 号或完整视频链接", 300);
        return true;
    });
    activity->registerAction(key("f8"), [](...) { AiHelper::instance().openPanel(); return true; });
    activity->registerAction(key("f6"), [](...) { AiHelper::instance().command("summary"); return true; });
    activity->registerAction(key("f7"), [](...) { AiHelper::instance().ask(); return true; });
    activity->registerAction(key("f9"), [](...) { AiHelper::instance().skip(); return true; });
    activity->registerAction(key("f10"), [](...) { AiHelper::instance().stay(); return true; });
    activity->registerAction(key("u"), [](...) { AiHelper::instance().stay(); return true; });
    activity->registerAction(key("ctrl-f10"), [](...) { AiHelper::instance().notAd(); return true; });
    activity->registerAction(key("ctrl-m"), [](...) { AiHelper::instance().mark(); return true; });
    activity->registerAction(key("ctrl-shift-m"), [](...) { AiHelper::instance().cancelMark(); return true; });
    activity->registerAction(key("ctrl-g"), [](...) { AiHelper::instance().toggleAuto(); return true; });
    activity->registerAction(key("ctrl-shift-a"), [](...) { AiHelper::instance().toggleEnabled(); return true; });
    activity->registerAction(key("c"), [](...) { AiHelper::instance().toggleSubtitle(); return true; });
    activity->registerAction(key("ctrl-pgdn"), [](...) { AiHelper::instance().navigatePart(1); return true; });
    activity->registerAction(key("ctrl-pgup"), [](...) { AiHelper::instance().navigatePart(-1); return true; });
}

// Runs the real native UI and libmpv with a local clip; data source is explicitly a fixture.
class AiTestActivity : public brls::Activity {
public:
    explicit AiTestActivity(std::string path) : path(std::move(path)) {}
    brls::View* createContentView() override {
        return brls::View::createFromXMLString(R"(<brls:Box axis="column"><VideoView id="video" focusable="true" grow="1" width="100%"/><brls:Label height="40" text="本地测试媒体 · F12 换视频 · F11 换分 P · F8 助手 · Space 暂停"/></brls:Box>)");
    }
    void onContentAvailable() override {
        video = static_cast<VideoView*>(getView("video"));
        video->registerCommonActions(this);
        load();
        registerAction(key("f12"), [this](...) { index = index == 1 ? 2 : 1; load(); return true; });
        registerAction(key("f11"), [this](...) { index = index == 3 ? 1 : 3; load(); return true; });
    }
    ~AiTestActivity() override { AiHelper::instance().invalidate(this); video->stop(); }
private:
    std::string path; VideoView* video = nullptr; int index = 1;
    void load() {
        AiHelper::instance().invalidate(this);
        video->stop();
        Json meta = {{"bvid", index == 2 ? "BVAItest0002" : "BVAItest0001"}, {"cid", index}, {"p", index == 3 ? 2 : 1}, {"duration", 200}, {"title", "本地原生播放器测试"}, {"aid", index == 2 ? 2 : 1}};
        AiHelper::instance().activate(meta, this);
        video->setUrl(path, 0, -1);
        AiHelper::instance().playerReady(this);
        int duration = 200; APP_E->fire(VideoView::REAL_DURATION, &duration);
    }
};
void Intent::openAiTestMedia(const std::string& path) { brls::Application::pushActivity(new AiTestActivity(path), brls::TransitionAnimation::NONE); }
