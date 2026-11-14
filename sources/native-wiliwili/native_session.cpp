#include "utils/native_session.hpp"
#include <curl/curl.h>
#include <openssl/evp.h>
#include <algorithm>
#include <array>
#include <cctype>
#include <ctime>
#include <filesystem>
#include <mutex>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace wiliwili::auth {
namespace {
constexpr const char* appKey = "783bbb7264451d82";
// Public TV application signing constant; never an account credential.
constexpr const char* appSecret = "2653583c8873dea268ab9386918b1d65";
std::atomic<uint64_t> epoch{1};
bool stopped(const Cancel& cancel) { return cancel && cancel(); }
struct Reply { std::string body; std::map<std::string, std::string> cookies; Cancel cancel; };
size_t body(char* ptr, size_t size, size_t count, void* userdata) {
    auto& r = *static_cast<Reply*>(userdata);
    const auto n = size * count;
    if (n > 1024 * 1024 - r.body.size()) return 0;
    r.body.append(ptr, n); return n;
}
size_t header(char* ptr, size_t size, size_t count, void* userdata) {
    std::string line(ptr, size * count);
    std::string lower = line.substr(0, 11);
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c){ return std::tolower(c); });
    if (lower == "set-cookie:") {
        auto start = line.find_first_not_of(" \t", 11), eq = line.find('=', start), end = line.find(';', eq);
        if (start != std::string::npos && eq != std::string::npos)
            static_cast<Reply*>(userdata)->cookies[line.substr(start, eq - start)] = line.substr(eq + 1, end - eq - 1);
    }
    return size * count;
}
int progress(void* r, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
    return stopped(static_cast<Reply*>(r)->cancel) ? 1 : 0;
}
std::string encode(CURL* curl, const std::string& text) {
    char* data = curl_easy_escape(curl, text.c_str(), static_cast<int>(text.size()));
    if (!data) return {};
    std::string out(data); curl_free(data); return out;
}
std::string md5(const std::string& text) {
    unsigned char digest[EVP_MAX_MD_SIZE]; unsigned int size = 0;
    if (!EVP_Digest(text.data(), text.size(), digest, &size, EVP_md5(), nullptr)) return {};
    constexpr char hex[] = "0123456789abcdef";
    std::string result;
    for (unsigned i = 0; i < size; ++i) { result += hex[digest[i] >> 4]; result += hex[digest[i] & 15]; }
    return result;
}
Json request(std::string url, std::map<std::string, std::string> params, bool post, bool sign,
             const std::string& proxy, Cancel cancel, Reply* response = nullptr,
             const std::map<std::string, std::string>& cookies = {}) {
    if (stopped(cancel)) return {};
    std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> curl(curl_easy_init(), curl_easy_cleanup);
    if (!curl) return {};
    if (sign) { params["appkey"] = appKey; params["local_id"] = "0"; params["ts"] = std::to_string(std::time(nullptr)); }
    std::string query;
    for (const auto& p : params) { if (!query.empty()) query += '&'; query += encode(curl.get(), p.first) + '=' + encode(curl.get(), p.second); }
    if (sign) query += "&sign=" + md5(query + appSecret);
    if (!post && !query.empty()) url += '?' + query;
    Reply result; result.cancel = std::move(cancel);
    curl_easy_setopt(curl.get(), CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl.get(), CURLOPT_USERAGENT, url.find("/x/v2/account/myinfo") != std::string::npos ?
        "Dalvik/2.1.0 (Linux; U; Android 12) 9.13.0 os/android model/redroid mobi_app/android build/9130500 channel/master network/2 grpc-java-cronet/1.36.1" :
        "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/130.0.0.0 Mobile Safari/537.36");
    curl_easy_setopt(curl.get(), CURLOPT_TIMEOUT, 10L);
    curl_easy_setopt(curl.get(), CURLOPT_CONNECTTIMEOUT, 4L);
    curl_easy_setopt(curl.get(), CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl.get(), CURLOPT_IPRESOLVE, CURL_IPRESOLVE_V4);
    // Do not follow credential-bearing redirects to other hosts.
    curl_easy_setopt(curl.get(), CURLOPT_FOLLOWLOCATION, 0L);
    curl_easy_setopt(curl.get(), CURLOPT_WRITEFUNCTION, body);
    curl_easy_setopt(curl.get(), CURLOPT_WRITEDATA, &result);
    curl_easy_setopt(curl.get(), CURLOPT_HEADERFUNCTION, header);
    curl_easy_setopt(curl.get(), CURLOPT_HEADERDATA, &result);
    curl_easy_setopt(curl.get(), CURLOPT_XFERINFOFUNCTION, progress);
    curl_easy_setopt(curl.get(), CURLOPT_XFERINFODATA, &result);
    curl_easy_setopt(curl.get(), CURLOPT_NOPROGRESS, 0L);
    if (proxy.empty()) curl_easy_setopt(curl.get(), CURLOPT_NOPROXY, "*");
    else curl_easy_setopt(curl.get(), CURLOPT_PROXY, proxy.c_str());
    std::string cookie;
    for (const auto& p : cookies) {
        if (p.first.find_first_of("\r\n;=") != std::string::npos || p.second.find_first_of("\r\n;") != std::string::npos) continue;
        cookie += p.first + '=' + p.second + "; ";
    }
    if (!cookie.empty()) curl_easy_setopt(curl.get(), CURLOPT_COOKIE, cookie.c_str());
    if (post) { curl_easy_setopt(curl.get(), CURLOPT_POSTFIELDS, query.c_str()); curl_easy_setopt(curl.get(), CURLOPT_POSTFIELDSIZE, long(query.size())); }
    long code = 0;
    const auto status = curl_easy_perform(curl.get());
    curl_easy_getinfo(curl.get(), CURLINFO_RESPONSE_CODE, &code);
    if (status != CURLE_OK || code != 200 || stopped(result.cancel))
        return Json{{"transport_error", int(status)}, {"http_status", code}};
    auto json = Json::parse(result.body, nullptr, false);
    if (!json.is_object()) return Json{{"transport_error", 0}, {"http_status", code}};
    if (response) *response = std::move(result);
    return json;
}
std::string id(const Json& value) {
    if (value.is_string()) return value.get<std::string>();
    if (value.is_number_unsigned()) return std::to_string(value.get<uint64_t>());
    if (value.is_number_integer()) return std::to_string(value.get<int64_t>());
    return {};
}
Account account(const Json& reply, Cancel cancel, bool web) try {
    Account r;
    if (stopped(cancel)) { r.state = State::Cancelled; return r; }
    if (!reply.is_object()) { r.state = State::NetworkError; return r; }
    if (reply.contains("http_status")) {
        r.state = State::NetworkError; r.httpStatus = reply.value("http_status", 0); r.transportError = reply.value("transport_error", 0); return r;
    }
    const int code = reply.value("code", -1);
    if (code != 0) { r.state = code == -101 || code == -400 || code == -2 ? State::Expired : State::Rejected; return r; }
    auto data = reply.value("data", Json::object());
    if (web && !data.value("isLogin", false)) { r.state = State::Expired; return r; }
    r.mid = id(data.value("mid", Json{})); r.name = data.value(web ? "uname" : "name", std::string{});
    r.state = !r.mid.empty() && r.mid != "0" ? State::Valid : State::Rejected;
    const auto vip = data.value("vip", Json::object());
    r.vip = vip.value("type", 0) > 0 && vip.value("status", 0) > 0;
    return r;
} catch (...) { return Account{}; }
}
uint64_t generation() { return epoch.load(); }
void changed() { ++epoch; }

bool privatePath(const std::string& path) {
    try {
        if (path.empty()) return false;
        const auto p = std::filesystem::weakly_canonical(path);
        // Secrets may not be saved into any checkout, including ignored paths.
        for (auto parent = p.parent_path(); !parent.empty(); parent = parent.parent_path()) {
            if (std::filesystem::is_regular_file(parent / ".git") ||
                std::filesystem::exists(parent / ".git" / "HEAD")) return false;
            if (parent == parent.root_path()) break;
        }
        return true;
    } catch (...) { return false; }
}
Json readPrivate(const std::string& path) {
    if (!privatePath(path)) return {};
    const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return {};
    struct stat st{}; std::string data;
    if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && st.st_uid == getuid() && (st.st_mode & 0777) == 0600 && st.st_size > 0 && st.st_size <= 65536) {
        data.resize(st.st_size); size_t offset = 0;
        while (offset < data.size()) { auto n = read(fd, data.data() + offset, data.size() - offset); if (n <= 0) break; offset += n; }
        if (offset != data.size()) data.clear();
    }
    close(fd);
    const auto result = Json::parse(data, nullptr, false);
    return result.is_object() ? result : Json{};
}
bool writePrivate(const std::string& path, const Json& data) {
    if (!privatePath(path)) return false;
    try {
        std::filesystem::create_directories(std::filesystem::path(path).parent_path());
        std::string temporary = path + ".tmp.XXXXXX";
        const int fd = mkstemp(temporary.data()); if (fd < 0) return false;
        fchmod(fd, 0600);
        const auto bytes = data.dump(2); size_t offset = 0;
        while (offset < bytes.size()) { auto n = write(fd, bytes.data() + offset, bytes.size() - offset); if (n <= 0) break; offset += n; }
        const bool ok = offset == bytes.size() && fsync(fd) == 0;
        close(fd);
        if (!ok || rename(temporary.c_str(), path.c_str()) != 0) { unlink(temporary.c_str()); return false; }
        return true;
    } catch (...) { return false; }
}
bool removePrivate(const std::string& path) { if (!privatePath(path)) return false; return unlink(path.c_str()) == 0 || errno == ENOENT; }
std::string token(const Json& session) try {
    if (!session.is_object()) return {};
    const auto t = session.value("access_token", std::string{});
    if (t.size() < 16 || t.size() > 256 || !std::all_of(t.begin(), t.end(), [](unsigned char c){ return std::isalnum(c) || c == '_' || c == '-'; })) return {};
    return t;
} catch (...) { return std::string{}; }
bool expired(const Json& data) try {
    if (token(data).empty()) return true;
    return !data.contains("created") || !data.contains("expires_in") ||
           data.at("created").get<double>() + data.at("expires_in").get<double>() <= std::time(nullptr) + 60;
} catch (...) { return true; }
Account appAccount(const Json& session, const std::string& proxy, Cancel cancel) try {
    if (token(session).empty()) return {};
    const std::map<std::string, std::string> params = {{"access_key", token(session)}, {"mobi_app", "android"}, {"build", "9130500"}};
    auto reply = request("https://app.bilibili.com/x/v2/account/myinfo", params, false, true, proxy, cancel);
    if (!stopped(cancel) && reply.value("http_status", 200) == 404)
        reply = request("https://api.bilibili.com/x/v2/account/myinfo", params, false, true, proxy, cancel);
    return account(reply, cancel, false);
} catch (...) { return Account{}; }
Account webAccount(const std::map<std::string, std::string>& cookies, const std::string& proxy, Cancel cancel) try {
    if (!cookies.count("SESSDATA")) return {};
    return account(request("https://api.bilibili.com/x/web-interface/nav", {}, false, false, proxy, cancel, nullptr, cookies), cancel, true);
} catch (...) { return Account{}; }
std::map<std::string, std::string> sessionCookies(const Json& session) {
    std::map<std::string, std::string> cookies;
    try { for (const auto& c : session.at("cookie_info").at("cookies")) cookies[c.at("name").get<std::string>()] = c.at("value").get<std::string>(); } catch (...) {}
    return cookies;
}
bool refresh(Json& session, const std::string& proxy, Cancel cancel) try {
    if (!session.is_object() || token(session).empty() || session.value("refresh_token", std::string{}).empty()) return false;
    auto reply = request("https://passport.bilibili.com/x/passport-login/oauth2/refresh_token",
        {{"access_key", token(session)}, {"access_token", token(session)}, {"refresh_token", session.at("refresh_token").get<std::string>()}},
        true, true, proxy, cancel);
    if (!reply.is_object() || reply.value("code", -1) != 0 || stopped(cancel)) return false;
    auto data = reply.value("data", Json{});
    // Some OAuth responses nest token_info; TV QR responses also duplicate it.
    if (data.contains("token_info")) for (const auto& p : data.at("token_info").items()) data[p.key()] = p.value();
    data["created"] = std::time(nullptr);
    if (expired(data) || appAccount(data, proxy, cancel).state != State::Valid) return false;
    session = std::move(data); return true;
} catch (...) { return false; }
Json usableSession(const std::string& path, const std::string& expectedMid, const std::string& proxy, Cancel cancel) {
    try {
        auto session = readPrivate(path);
        if (token(session).empty() || stopped(cancel)) return {};
        // Cache only validated identity; key includes credential and account generation.
        static std::mutex mutex;
        static std::string cachedPath, cachedToken, cachedMid;
        static uint64_t cachedEpoch = 0; static int64_t until = 0;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (!expired(session) && cachedEpoch == generation() && path == cachedPath && token(session) == cachedToken &&
                std::time(nullptr) < until && (expectedMid.empty() || expectedMid == "0" || expectedMid == cachedMid)) return session;
        }
        auto user = appAccount(session, proxy, cancel);
        if (expired(session) || user.state == State::Expired) {
            if (!refresh(session, proxy, cancel) || stopped(cancel) || !writePrivate(path, session)) return {};
            user = appAccount(session, proxy, cancel);
        }
        if (user.state != State::Valid || stopped(cancel) || (!expectedMid.empty() && expectedMid != "0" && expectedMid != user.mid)) return {};
        {
            std::lock_guard<std::mutex> lock(mutex);
            cachedPath = path; cachedToken = token(session); cachedMid = user.mid; cachedEpoch = generation(); until = std::time(nullptr) + 300;
        }
        return session;
    } catch (...) { return {}; }
}
Challenge prepare(bool app, const std::string& proxy, Cancel cancel) try {
    Challenge q; q.app = app;
    const auto r = request(app ? "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code" :
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate", {}, app, app, proxy, cancel);
    if (!r.is_object() || r.value("code", -1) != 0) return q;
    try {
        const auto d = r.at("data"); q.url = d.at("url").get<std::string>(); q.key = d.at(app ? "auth_code" : "qrcode_key").get<std::string>();
        if (q.url.rfind("https://", 0) != 0 || q.key.empty()) return Challenge{};
        q.deadline = std::time(nullptr) + std::clamp(d.value("expires_in", 180), 30, 600);
    } catch (...) { q.url.clear(); q.key.clear(); }
    return q;
} catch (...) { return Challenge{}; }
QrState pollState(bool app, const Json& r, int64_t deadline) try {
    if (std::time(nullptr) >= deadline) return QrState::Expired;
    if (!r.is_object()) return QrState::Error;
    int code = r.value("code", -1);
    if (!app && code == 0) code = r.value("data", Json::object()).value("code", -1);
    if (code == 0) return QrState::Success;
    if (code == 86038) return QrState::Expired;
    if (code == 86090) return QrState::Scanned;
    if (code == 86039 || code == 86101) return QrState::Ready;
    return QrState::Error;
} catch (...) { return QrState::Error; }
Poll poll(const Challenge& qr, const std::string& proxy, Cancel cancel) try {
    Poll p;
    if (stopped(cancel)) { p.state = QrState::Cancelled; return p; }
    if (std::time(nullptr) >= qr.deadline) { p.state = QrState::Expired; return p; }
    Reply response;
    const auto r = request(qr.app ? "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll" :
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
        {{qr.app ? "auth_code" : "qrcode_key", qr.key}}, qr.app, qr.app, proxy, cancel, &response);
    p.state = stopped(cancel) ? QrState::Cancelled : pollState(qr.app, r, qr.deadline);
    if (p.state == QrState::Success) {
        if (qr.app) { p.session = r.value("data", Json{}); p.session["created"] = std::time(nullptr); p.cookies = sessionCookies(p.session); if (token(p.session).empty()) p.state = QrState::Error; }
        else { p.cookies = response.cookies; p.webRefresh = r.at("data").value("refresh_token", std::string{}); if (!p.cookies.count("SESSDATA")) p.state = QrState::Error; }
    }
    return p;
} catch (...) { return Poll{}; }
}
