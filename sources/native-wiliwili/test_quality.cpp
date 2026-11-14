#include "quality_policy.hpp"
#include <cassert>
#include <iostream>

using bilibili::DashMedia;
using namespace wiliwili::quality;
DashMedia media(int id, int codec = 7, unsigned bandwidth = 1000, int width = 1920, int height = 1080) {
    DashMedia result{};
    result.id = id; result.codecid = codec; result.bandwidth = bandwidth;
    result.width = width; result.height = height; result.base_url = "https://example.invalid/media";
    return result;
}
int main() {
    assert(requestQuality(0) == 127 && requestQuality(80) == 80);
    assert(requestFeatures("0") == "0" && requestFeatures("4048") == "4048");
    assert(requestFeatures("16") == "4048");
    std::vector<DashMedia> streams{media(64), media(127, 12), media(80), media(120), media(126), media(125)};
    auto pick = [&](int preference = 0, bool hdr = false, int landscape = 128, int portrait = 128, int codec = 7) {
        return selectVideo(streams, preference, hdr, landscape, portrait, codec);
    };
    // Unordered API responses and unavailable preferred codecs still select 8K.
    assert(pick()->id == 127 && pick()->codecid == 12);
    std::reverse(streams.begin(), streams.end());
    assert(pick()->id == 127);
    // Manual preference, missing/unauthorized qualities and dimension limits.
    assert(pick(80)->id == 80 && pick(116)->id == 80);
    assert(pick(0, false, 80)->id == 80);
    assert(pick(0, false, 16) == nullptr);
    assert(pick(16)->id == 64);
    streams = {media(125), media(126), media(120)};
    assert(pick()->id == 120 && pick(0, true)->id == 126);
    assert(pick(125)->id == 125); // Explicit HDR remains available with auto-HDR off.
    streams = {media(126)};
    assert(pick() == nullptr && pick(0, true)->id == 126);
    streams = {media(120, 7, 1000, 2160, 3840), media(80)};
    assert(pick(0, false, 128, 80)->id == 80);
    streams = {media(80, 12, 9000), media(80, 7, 1000), media(80, 7, 2000)};
    assert(pick()->bandwidth == 2000);
    assert(pick(0, false, 128, 128, 12)->bandwidth == 9000);
    streams.push_back(media(127)); streams.back().base_url.clear();
    assert(pick()->id == 80);
    streams.clear(); assert(pick() == nullptr);
    streams = {media(32), media(16)}; // Anonymous/non-VIP response: no fabricated high streams.
    assert(pick()->id == 32);

    bilibili::VideoUrlResult original{};
    original.quality = 64; original.timelength = 100000;
    original.accept_quality = {120, 80, 64, 32, 16};
    original.accept_description = {"4K", "1080P", "720P", "480P", "360P"};
    original.dash.video = {media(32), media(16)};
    assert(availableQuality(original) == 32); // Top-level 64 does not mean a 720P stream exists.
    assert(needsCompatibilityStream(original, 0) && !needsCompatibilityStream(original, 32));
    bilibili::VideoUrlResult candidate{};
    candidate.quality = 80; candidate.timelength = 100000;
    bilibili::VideoDUrl part{}; part.url = "https://example.invalid/full.mp4"; part.length = 100000;
    candidate.durl = {part};
    auto merged = original;
    assert(useCompatibilityStream(merged, candidate, 0));
    assert(merged.quality == 80 && merged.dash.video.empty() && merged.durl.size() == 1);
    assert(merged.accept_quality == original.accept_quality && merged.timelength == 100000);
    assert(!useCompatibilityStream(merged, candidate, 0)); // No pointless replacement.
    merged = original;
    assert(!useCompatibilityStream(merged, candidate, 64)); // Respect explicit lower preference.
    candidate.durl[0].length = 6000;
    assert(!useCompatibilityStream(merged, candidate, 0)); // Reject preview even if total metadata is full length.
    candidate.durl[0].length = 100000; candidate.timelength = 6000;
    assert(!useCompatibilityStream(merged, candidate, 0));
    candidate.timelength = 100000; candidate.durl[0].url.clear();
    assert(!useCompatibilityStream(merged, candidate, 0));
    assert(merged.dash.video.size() == 2 && merged.quality == 64);

    bilibili::Dash dash{};
    dash.audio = {media(30216), media(30280), media(30232)};
    assert(selectAudio(dash, 30250)->id == 30280);
    assert(selectAudio(dash, 30232)->id == 30232);
    dash.dolby_audio = {media(30250, 0, 500), media(30250, 0, 1000)};
    dash.flac_audio = media(30251); dash.has_flac = true;
    assert(selectAudio(dash, 30250)->bandwidth == 1000);
    assert(selectAudio(dash, 30251)->id == 30251);
    dash.audio.clear(); assert(selectAudio(dash, 30280)->id == 30250);
    dash.dolby_audio.clear(); assert(selectAudio(dash, 30250)->id == 30251);
    dash.flac_audio.base_url.clear(); assert(selectAudio(dash, 30250) == nullptr);

    // Real API shapes: audio may omit dimensions/codecid or use camelCase URLs.
    auto parsed = nlohmann::json::parse(R"({"id":30251,"bandwidth":1000,"baseUrl":"https://example.invalid/flac","backupUrl":null,"height":null})").get<DashMedia>();
    assert(parsed.id == 30251 && parsed.width == 0 && parsed.height == 0 && parsed.codecid == 0);
    assert(!parsed.base_url.empty() && parsed.backup_url.empty());
    auto parsedDash = nlohmann::json::parse(R"({"duration":1,"min_buffer_time":1,"video":[],"audio":null,"dolby":{"type":2,"audio":null},"flac":{"display":true,"audio":{"id":30251,"bandwidth":1000,"base_url":"https://example.invalid/flac"}}})").get<bilibili::Dash>();
    assert(parsedDash.audio.empty() && parsedDash.has_flac && selectAudio(parsedDash, 30250)->id == 30251);
    std::cout << "Quality policy: all checks passed\n";
}
