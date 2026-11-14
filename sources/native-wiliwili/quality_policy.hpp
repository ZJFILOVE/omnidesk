#pragma once

#include <algorithm>
#include <string>
#include <vector>

#include "bilibili/result/video_detail_result.h"

// Native counterpart of Bilibili-Auto-Quality's capability flags and automatic
// selection. Only streams actually returned for the current account are used.
namespace wiliwili::quality {
constexpr int automatic = 0;
constexpr int highest = 127;

inline int requestQuality(int preference) { return preference == automatic ? highest : preference; }

inline std::string requestFeatures(const std::string& format) {
    // Keep the explicit FLV/MP4 choice. DASH: HDR, 4K, Dolby audio/vision, 8K,
    // AV1. Web playurl rejects the extra 8192 flag on the tested API.
    return format == "0" ? "0" : std::to_string(std::stoi(format) | 4048);
}

inline bool isHdr(int quality) { return quality == 125 || quality == 126; }

inline int availableQuality(const bilibili::VideoUrlResult& result) {
    int quality = 0;
    for (const auto& stream : result.dash.video)
        if (!stream.base_url.empty()) quality = std::max(quality, stream.id);
    for (const auto& segment : result.durl)
        if (!segment.url.empty()) quality = std::max(quality, result.quality);
    return quality;
}

inline bool needsCompatibilityStream(const bilibili::VideoUrlResult& result, int preference) {
    return availableQuality(result) < std::min(80, requestQuality(preference));
}

inline bool useAndroidStream(bilibili::VideoUrlResult& result, const bilibili::VideoUrlResult& candidate) {
    if (!candidate.android_media || candidate.dash.video.empty() || candidate.dash.audio.empty() ||
        candidate.timelength <= 0 ||
        std::abs(int64_t(candidate.timelength) - result.timelength) > 2000 ||
        availableQuality(candidate) <= availableQuality(result)) return false;
    result = candidate;
    return true;
}

inline bool useCompatibilityStream(bilibili::VideoUrlResult& result,
                                   const bilibili::VideoUrlResult& candidate, int preference) {
    // A menu entry or top-level quality value alone is not evidence of a stream.
    if (candidate.durl.empty() || std::any_of(candidate.durl.begin(), candidate.durl.end(),
                                             [](const auto& part) { return part.url.empty(); })) return false;
    const int quality = availableQuality(candidate);
    if (quality <= availableQuality(result) || quality > requestQuality(preference)) return false;
    // Reject short previews, preserve full-length playback, and retain the DASH
    // menu so users can still request a higher quality after signing in.
    int64_t length = 0;
    for (const auto& part : candidate.durl) length += part.length;
    if (length <= 0 || length + 2000 < result.timelength || candidate.timelength + 2000 < result.timelength)
        return false;
    result.quality = candidate.quality;
    result.timelength = candidate.timelength;
    result.durl = candidate.durl;
    result.dash = {};
    return true;
}

inline const bilibili::DashMedia* selectAudio(const bilibili::Dash& dash, int preference) {
    auto best = [](const auto& streams, int id = 0) -> const bilibili::DashMedia* {
        const bilibili::DashMedia* result = nullptr;
        for (const auto& stream : streams) {
            if (stream.base_url.empty() || (id && stream.id != id)) continue;
            if (!result || stream.bandwidth > result->bandwidth) result = &stream;
        }
        return result;
    };
    const auto* dolby = best(dash.dolby_audio);
    const auto* flac = dash.has_flac && !dash.flac_audio.base_url.empty() ? &dash.flac_audio : nullptr;
    if (preference == 30250 && (dolby || flac)) return dolby ? dolby : flac;
    if (preference == 30251 && (flac || dolby)) return flac ? flac : dolby;
    for (int id : {preference, 30280, 30232, 30216})
        if (const auto* stream = best(dash.audio, id)) return stream;
    if (const auto* stream = best(dash.audio)) return stream;
    return dolby ? dolby : flac;
}

inline const bilibili::DashMedia* selectVideo(const std::vector<bilibili::DashMedia>& streams,
                                             int preference, bool autoHdr, int landscapeMax,
                                             int portraitMax, int preferredCodec) {
    const bilibili::DashMedia* best = nullptr;
    const bilibili::DashMedia* fallback = nullptr;
    auto betterCodec = [preferredCodec](const auto& a, const auto& b) {
        if ((a.codecid == preferredCodec) != (b.codecid == preferredCodec))
            return a.codecid == preferredCodec;
        return a.bandwidth > b.bandwidth;
    };
    for (const auto& stream : streams) {
        if (stream.base_url.empty() || stream.id <= 0) continue;
        if (preference == automatic && !autoHdr && isHdr(stream.id)) continue;
        const int limit = stream.height > stream.width ? portraitMax : landscapeMax;
        if (stream.id > limit) continue;
        if (!fallback || stream.id < fallback->id ||
            (stream.id == fallback->id && betterCodec(stream, *fallback))) fallback = &stream;
        if (stream.id > requestQuality(preference)) continue;
        if (!best || stream.id > best->id ||
            (stream.id == best->id && betterCodec(stream, *best))) best = &stream;
    }
    return best ? best : fallback;
}
}  // namespace wiliwili::quality
