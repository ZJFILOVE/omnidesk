#pragma once
#include <borealis.hpp>
#include <nlohmann/json.hpp>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <set>
#include <thread>
#include <vector>

// UI-thread player operations, worker-thread loopback HTTP, generation-checked publication.
class AiHelper {
public:
    using Json = nlohmann::json;
    static AiHelper& instance();
    void activate(const Json& video, void* owner);
    void invalidate(void* owner = nullptr);
    void playerReady(void* owner);
    void beforePlayerUrl(void* activity);
    void shutdown();
    void registerActions(brls::Activity* activity);
    void openPanel();
    bool handlePanelKey(brls::BrlsKeyCombination key, bool repeating);
    Json panelDiagnostics();
    void command(const std::string& action, Json args = Json::object());
    void ask();
    void skip();
    void stay();
    void notAd();
    void mark();
    void cancelMark();
    void toggleAuto();
    void toggleEnabled();
    void cycleEngineMode();
    void toggleSubtitle();
    void selectSubtitleTrack(int index, const std::string& lan);
    void seekChapter(double time, uint64_t generation);
    void navigatePart(int delta);
    // Copy the UI state only when it changed; revision 0 requests the first snapshot.
    bool snapshotIfChanged(uint64_t& revision, Json& output);
    uint64_t currentGeneration();
    void tick();
    void drawMarks(NVGcontext* vg, float x, float y, float width, float height, float alpha = 1);
    void drawHint(NVGcontext* vg, float x, float y, float width, float height, float alpha);
private:
    AiHelper();
    ~AiHelper();
    void run();
    Json request(const std::string& method, const std::string& path, const Json& body = Json::object());
    Json currentAd(double time, bool includeRecent = false);
    Json preRollAd(double time);
    std::string adKey(const Json& ad);
    void localStatus(const std::string& text);
    std::mutex mutex;
    std::condition_variable changed;
    std::thread worker;
    bool stopping = false, enabled = true, autoSkip = true, ready = false;
    void* owner = nullptr;
    uint64_t generation = 0;
    uint64_t stateRevision = 1;
    Json desired = Json::object(), state = Json::object();
    struct AdMark { double start, end; bool confident; };
    // Only the UI thread reads/writes the drawing cache; source state is locked.
    std::vector<AdMark> cachedMarks;
    uint64_t marksRevision = 0;
    std::deque<std::pair<uint64_t, Json>> commands;
    std::set<std::string> optedOut;
    Json lastSkipped = Json::object();
    double skippedFrom = 0, markingStart = -1, savedTotalSeconds = 0;
    std::string countdownKey, toast;
    std::string lastPreRollOsdKey;
    int lastPreRollOsdSec = -1;
    std::chrono::steady_clock::time_point entered{}, toastUntil{};
};
