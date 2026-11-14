#include "utils/android_playurl.hpp"
#include "utils/native_session.hpp"

#include <curl/curl.h>
#include <nlohmann/json.hpp>
#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <ctime>
#include <fstream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <vector>
#ifndef _WIN32
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace wiliwili::android {
namespace {
constexpr size_t maxReply = 3 * 1024 * 1024;
struct Field { unsigned number, wire; uint64_t value = 0; std::string_view bytes; };
using Fields = std::vector<Field>;

uint64_t readVarint(std::string_view data, size_t& offset) {
    uint64_t value = 0;
    for (unsigned shift = 0; shift < 64; shift += 7) {
        if (offset >= data.size()) throw std::runtime_error("truncated protobuf");
        const auto byte = static_cast<unsigned char>(data[offset++]);
        if (shift == 63 && byte > 1) throw std::runtime_error("oversize protobuf integer");
        value |= uint64_t(byte & 127) << shift;
        if (!(byte & 128)) return value;
    }
    throw std::runtime_error("unterminated protobuf integer");
}

Fields decode(std::string_view data) {
    if (data.size() > maxReply) throw std::runtime_error("oversize protobuf");
    Fields fields;
    size_t offset = 0;
    while (offset < data.size()) {
        const auto tag = readVarint(data, offset);
        if ((tag >> 3) == 0 || (tag >> 3) >= (1u << 29)) throw std::runtime_error("invalid protobuf tag");
        Field field{static_cast<unsigned>(tag >> 3), static_cast<unsigned>(tag & 7), 0, {}};
        if (field.wire == 0) field.value = readVarint(data, offset);
        else {
            const auto size = field.wire == 2 ? readVarint(data, offset) :
                              field.wire == 1 ? 8 : field.wire == 5 ? 4 : maxReply + 1;
            if (size > data.size() - offset) throw std::runtime_error("invalid protobuf field");
            field.bytes = data.substr(offset, size);
            offset += size;
        }
        fields.push_back(field);
    }
    return fields;
}

uint64_t number(const Fields& fields, unsigned n) {
    for (const auto& f : fields) if (f.number == n && f.wire == 0) return f.value;
    return 0;
}
std::string_view bytes(const Fields& fields, unsigned n) {
    for (const auto& f : fields) if (f.number == n && f.wire == 2) return f.bytes;
    return {};
}
bool mediaUrl(std::string_view url) {
    // Media URLs are passed to mpv as quoted loadfile options. Reject control
    // characters and quotes instead of letting them become extra options.
    return (url.substr(0, 8) == "https://" || url.substr(0, 7) == "http://") &&
           std::none_of(url.begin(), url.end(), [](unsigned char c) { return c < 32 || c == '"' || c == '\\'; });
}
std::vector<std::string> backups(const Fields& fields, unsigned n) {
    std::vector<std::string> result;
    for (const auto& f : fields)
        if (f.number == n && f.wire == 2 && mediaUrl(f.bytes)) result.emplace_back(f.bytes);
    return result;
}
void varint(std::string& out, uint64_t value) {
    do {
        const auto byte = value & 127;
        value >>= 7;
        out += static_cast<char>(byte | (value ? 128 : 0));
    } while (value);
}
void put(std::string& out, unsigned n, uint64_t value) { varint(out, n << 3); varint(out, value); }
void put(std::string& out, unsigned n, std::string_view value) {
    varint(out, (n << 3) | 2); varint(out, value.size()); out.append(value);
}
std::string base64(std::string_view input) {
    constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string output;
    for (size_t i = 0; i < input.size(); i += 3) {
        const uint32_t value = uint32_t(static_cast<unsigned char>(input[i])) << 16 |
            (i + 1 < input.size() ? uint32_t(static_cast<unsigned char>(input[i + 1])) << 8 : 0) |
            (i + 2 < input.size() ? static_cast<unsigned char>(input[i + 2]) : 0);
        output += alphabet[(value >> 18) & 63]; output += alphabet[(value >> 12) & 63];
        output += i + 1 < input.size() ? alphabet[(value >> 6) & 63] : '=';
        output += i + 2 < input.size() ? alphabet[value & 63] : '=';
    }
    return output;
}

struct Response { std::string body; int grpcStatus = -1; std::function<bool()> cancelled; };
int cancelTransfer(void* opaque, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
    const auto& cancel = static_cast<Response*>(opaque)->cancelled;
    return cancel && cancel() ? 1 : 0;
}
size_t writeBody(char* data, size_t size, size_t count, void* opaque) {
    const auto length = size * count;
    auto& response = *static_cast<Response*>(opaque);
    if (length > maxReply - response.body.size()) return 0;
    response.body.append(data, length);
    return length;
}
size_t readHeader(char* data, size_t size, size_t count, void* opaque) {
    const auto length = size * count;
    std::string_view line(data, length);
    if (line.substr(0, 12) == "grpc-status:") {
        try { static_cast<Response*>(opaque)->grpcStatus = std::stoi(std::string(line.substr(12))); }
        catch (...) { static_cast<Response*>(opaque)->grpcStatus = -1; }
    }
    return length;
}
}

bool parsePlayReply(std::string_view data, bilibili::VideoUrlResult& result) {
    try {
        const auto reply = decode(data);
        const auto vod = decode(bytes(reply, 1));
        const auto duration = number(vod, 3);
        if (!duration || duration > uint64_t(std::numeric_limits<int>::max())) return false;
        bilibili::VideoUrlResult candidate{};
        candidate.quality = static_cast<int>(number(vod, 1));
        candidate.timelength = static_cast<int>(duration);
        candidate.dash.duration = duration / 1000;
        candidate.dash.min_buffer_time = 1.5f;
        for (const auto& field : vod) {
            if (field.wire != 2) continue;
            if (field.number == 5) {
                const auto stream = decode(field.bytes);
                const auto info = decode(bytes(stream, 1));
                const auto dash = decode(bytes(stream, 2));
                const auto quality = number(info, 1);
                // Intact + an actual DASH address are required; local menu/VIP
                // flags never create media. Encrypted streams remain unsupported.
                if (!number(info, 8) || !mediaUrl(bytes(dash, 1)) || !quality || quality > 127 ||
                    !bytes(dash, 12).empty() || !bytes(dash, 13).empty()) continue;
                bilibili::DashMedia video{};
                video.id = quality; video.base_url = std::string(bytes(dash, 1));
                video.backup_url = backups(dash, 2); video.bandwidth = number(dash, 3);
                const auto width = number(dash, 10), height = number(dash, 11);
                if (width == 0 || width > 16384 || height == 0 || height > 16384) continue;
                video.codecid = number(dash, 4); video.width = width; video.height = height;
                if (quality == 120 && (std::max(width, height) < 3840 || std::min(width, height) < 1600)) continue;
                if (quality == 127 && (std::max(width, height) < 7680 || std::min(width, height) < 3200)) continue;
                candidate.dash.video.push_back(video);
                candidate.accept_quality.push_back(video.id);
                auto description = std::string(bytes(info, 3));
                if (description.empty()) description = video.id == 120 ? "4K 超清" : std::to_string(video.height) + "P";
                candidate.accept_description.push_back(description);
            } else if (field.number == 6) {
                const auto dash = decode(field.bytes);
                if (!mediaUrl(bytes(dash, 2)) || !bytes(dash, 9).empty() || !bytes(dash, 10).empty()) continue;
                bilibili::DashMedia audio{};
                audio.id = number(dash, 1); audio.base_url = std::string(bytes(dash, 2));
                audio.backup_url = backups(dash, 3); audio.bandwidth = number(dash, 4); audio.codecid = number(dash, 5);
                candidate.dash.audio.push_back(audio);
            }
        }
        if (candidate.dash.video.empty() || candidate.dash.audio.empty()) return false;
        candidate.android_media = true;
        result = std::move(candidate);
        return true;
    } catch (...) { return false; }
}

bool getPlayUrl(const std::string& idKey, const std::string& id, uint64_t cid,
                int quality, int codec, const std::string& sessionPath,
                const std::string& proxy, bilibili::VideoUrlResult& result,
                std::function<bool()> cancelled, const std::string& expectedMid) {
    try {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
        const auto callerCancel = std::move(cancelled);
        cancelled = [callerCancel, deadline] {
            return (callerCancel && callerCancel()) || std::chrono::steady_clock::now() >= deadline;
        };
        const auto token = auth::token(auth::usableSession(sessionPath, expectedMid, proxy, cancelled));
        if (token.empty() || !cid || quality < 1 || quality > 127) return false;
        std::string vod, request;
        put(vod, 1, idKey == "aid" ? std::stoull(id) : 0); put(vod, 2, cid); put(vod, 3, quality);
        put(vod, 5, 4048); put(vod, 7, 2); put(vod, 8, 1);
        put(vod, 9, codec == 12 ? 2 : codec == 13 ? 3 : 1); put(vod, 11, 1);
        put(request, 1, vod); put(request, 2, "united.player-video-detail.0.0");
        put(request, 3, "main.my-history.0.0");
        if (idKey == "bvid") put(request, 5, id);
        std::string frame(5, '\0');
        for (int i = 1; i <= 4; ++i) frame[i] = static_cast<char>(request.size() >> ((4 - i) * 8));
        frame += request;
        std::string metadata, device, network, fawkes;
        put(metadata, 1, token); put(metadata, 2, "android"); put(metadata, 4, 9130500);
        put(metadata, 5, "master"); put(metadata, 7, "android");
        put(device, 1, 1); put(device, 2, 9130500); put(device, 4, "android"); put(device, 5, "android");
        put(device, 7, "master"); put(device, 8, "redroid"); put(device, 9, "redroid"); put(device, 10, "12");
        put(network, 1, 1); put(network, 2, "46007");
        put(fawkes, 1, "android64"); put(fawkes, 2, "prod"); put(fawkes, 3, "wiliwili-ai-3");
        const std::vector<std::string> headers = {
            "content-type: application/grpc", "te: trailers", "grpc-accept-encoding: identity",
            "user-agent: Dalvik/2.1.0 (Linux; U; Android 12) 9.13.0 os/android model/redroid mobi_app/android build/9130500 channel/master network/2 grpc-java-cronet/1.36.1",
            "authorization: identify_v1 " + token, "x-bili-metadata-bin: " + base64(metadata),
            "x-bili-device-bin: " + base64(device), "x-bili-network-bin: " + base64(network),
            "x-bili-fawkes-req-bin: " + base64(fawkes)};
        curl_slist* list = nullptr;
        for (const auto& header : headers) list = curl_slist_append(list, header.c_str());
        const std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> ownedHeaders(list, curl_slist_free_all);
        const std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> curl(curl_easy_init(), curl_easy_cleanup);
        if (!curl) return false;
        Response response;
        response.cancelled = std::move(cancelled);
        curl_easy_setopt(curl.get(), CURLOPT_URL, "https://grpc.biliapi.net/bilibili.app.playerunite.v1.Player/PlayViewUnite");
        curl_easy_setopt(curl.get(), CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_2TLS);
        curl_easy_setopt(curl.get(), CURLOPT_HTTPHEADER, list);
        curl_easy_setopt(curl.get(), CURLOPT_POSTFIELDS, frame.data());
        curl_easy_setopt(curl.get(), CURLOPT_POSTFIELDSIZE, static_cast<long>(frame.size()));
        curl_easy_setopt(curl.get(), CURLOPT_TIMEOUT, 10L);
        curl_easy_setopt(curl.get(), CURLOPT_CONNECTTIMEOUT, 6L);
        curl_easy_setopt(curl.get(), CURLOPT_NOSIGNAL, 1L);
        curl_easy_setopt(curl.get(), CURLOPT_IPRESOLVE, CURL_IPRESOLVE_V4);
        curl_easy_setopt(curl.get(), CURLOPT_SSL_VERIFYPEER, 1L);
        curl_easy_setopt(curl.get(), CURLOPT_SSL_VERIFYHOST, 2L);
        if (proxy.empty()) curl_easy_setopt(curl.get(), CURLOPT_NOPROXY, "*");
        else curl_easy_setopt(curl.get(), CURLOPT_PROXY, proxy.c_str());
        curl_easy_setopt(curl.get(), CURLOPT_WRITEFUNCTION, writeBody);
        curl_easy_setopt(curl.get(), CURLOPT_WRITEDATA, &response);
        curl_easy_setopt(curl.get(), CURLOPT_HEADERFUNCTION, readHeader);
        curl_easy_setopt(curl.get(), CURLOPT_HEADERDATA, &response);
        curl_easy_setopt(curl.get(), CURLOPT_XFERINFOFUNCTION, cancelTransfer);
        curl_easy_setopt(curl.get(), CURLOPT_XFERINFODATA, &response);
        curl_easy_setopt(curl.get(), CURLOPT_NOPROGRESS, 0L);
        const auto status = curl_easy_perform(curl.get());
        long httpStatus = 0;
        curl_easy_getinfo(curl.get(), CURLINFO_RESPONSE_CODE, &httpStatus);
        if ((response.cancelled && response.cancelled()) || status != CURLE_OK || httpStatus != 200 || response.grpcStatus != 0 || response.body.size() < 5 || response.body[0] != 0)
            return false;
        uint32_t size = 0;
        for (int i = 1; i <= 4; ++i) size = (size << 8) | static_cast<unsigned char>(response.body[i]);
        if (size != response.body.size() - 5) return false;
        return parsePlayReply(std::string_view(response.body).substr(5), result);
    } catch (...) { return false; } // Errors never include credentials or signed media addresses.
}
}
