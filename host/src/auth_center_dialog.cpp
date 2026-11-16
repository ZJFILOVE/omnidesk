#include "omnidesk/host/auth_center_dialog.hpp"

#include <arpa/inet.h>
#include <borealis.hpp>
#include <borealis/views/dialog.hpp>
#include <dirent.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <poll.h>
#include <signal.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <set>
#include <sstream>
#include <thread>

#include "omnidesk/host/desktop_ui.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/ui_comfort.hpp"
#include "omnidesk/ui_theme.hpp"

namespace fs = std::filesystem;

namespace omnidesk::host {
namespace {

std::string trimAscii(std::string_view sv) {
    size_t b = 0;
    while (b < sv.size() && std::isspace(static_cast<unsigned char>(sv[b]))) ++b;
    size_t e = sv.size();
    while (e > b && std::isspace(static_cast<unsigned char>(sv[e - 1]))) --e;
    return std::string(sv.substr(b, e - b));
}

std::string toLowerAscii(std::string s) {
    for (char& c : s) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return s;
}

std::string stripQuotes(std::string s) {
    s = trimAscii(s);
    if (s.size() >= 2 &&
        ((s.front() == '"' && s.back() == '"') || (s.front() == '\'' && s.back() == '\''))) {
        return s.substr(1, s.size() - 2);
    }
    return s;
}

std::string cleanDomain(std::string d) {
    d = toLowerAscii(trimAscii(d));
    while (!d.empty() && d.front() == '.') {
        d.erase(d.begin());
    }
    return d;
}

std::string extractHostFromUrl(const std::string& raw_url) {
    std::string t = trimAscii(raw_url);
    const std::string low = toLowerAscii(t);
    if (low.rfind("https://", 0) == 0) {
        t = t.substr(8);
    } else if (low.rfind("http://", 0) == 0) {
        t = t.substr(7);
    } else {
        return "";
    }
    const size_t slash = t.find_first_of("/?#");
    std::string authority = (slash == std::string::npos) ? t : t.substr(0, slash);
    const size_t at = authority.find('@');
    if (at != std::string::npos) authority = authority.substr(at + 1);
    const size_t colon = authority.rfind(':');
    if (colon != std::string::npos && authority.find(']') == std::string::npos) {
        authority = authority.substr(0, colon);
    }
    return cleanDomain(authority);
}

std::string guessRegistrableRootDomain(const std::string& host) {
    const std::string h = cleanDomain(host);
    if (h.empty()) return "";
    std::vector<std::string> parts;
    std::istringstream iss(h);
    std::string seg;
    while (std::getline(iss, seg, '.')) {
        if (!seg.empty()) parts.push_back(seg);
    }
    if (parts.size() <= 2) return h;
    // Handle multi-part academic/regional suffixes such as .edu.cn, .com.cn, .co.uk, .org.uk
    const std::string& last = parts.back();
    const std::string& prev = parts[parts.size() - 2];
    if (last.size() == 2 &&
        (prev == "edu" || prev == "com" || prev == "org" || prev == "net" ||
         prev == "gov" || prev == "ac" || prev == "co") &&
        parts.size() >= 3) {
        return parts[parts.size() - 3] + "." + prev + "." + last;
    }
    return prev + "." + last;
}

bool domainMatchesTarget(const std::string& cookie_domain, const std::string& target_domain) {
    const std::string cd = cleanDomain(cookie_domain);
    const std::string td = cleanDomain(target_domain);
    if (cd.empty() || td.empty()) return false;
    if (cd == td) return true;
    if (cd.size() > td.size()) {
        const size_t pos = cd.size() - td.size();
        if (cd.compare(pos, td.size(), td) == 0 && cd[pos - 1] == '.') return true;
    }
    if (td.size() > cd.size()) {
        const size_t pos = td.size() - cd.size();
        if (td.compare(pos, cd.size(), cd) == 0 && td[pos - 1] == '.') return true;
    }
    return false;
}

std::string base64EncodeBytes(const uint8_t* data, size_t len) {
    static constexpr char kTable[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve(((len + 2) / 3) * 4);
    for (size_t i = 0; i < len; i += 3) {
        uint32_t v = static_cast<uint32_t>(data[i]) << 16;
        if (i + 1 < len) v |= static_cast<uint32_t>(data[i + 1]) << 8;
        if (i + 2 < len) v |= static_cast<uint32_t>(data[i + 2]);
        out.push_back(kTable[(v >> 18) & 0x3F]);
        out.push_back(kTable[(v >> 12) & 0x3F]);
        out.push_back((i + 1 < len) ? kTable[(v >> 6) & 0x3F] : '=');
        out.push_back((i + 2 < len) ? kTable[v & 0x3F] : '=');
    }
    return out;
}

std::string base64UrlDecodeToString(const std::string& input) {
    std::string norm;
    norm.reserve(input.size() + 4);
    for (char ch : input) {
        if (ch == '-') norm.push_back('+');
        else if (ch == '_') norm.push_back('/');
        else if (!std::isspace(static_cast<unsigned char>(ch))) norm.push_back(ch);
    }
    while (norm.size() % 4 != 0) norm.push_back('=');

    static const int kDec[256] = {
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,62,-1,-1,-1,63,
        52,53,54,55,56,57,58,59,60,61,-1,-1,-1,-1,-1,-1,
        -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,
        15,16,17,18,19,20,21,22,23,24,25,-1,-1,-1,-1,-1,
        -1,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,
        41,42,43,44,45,46,47,48,49,50,51,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    };

    std::string out;
    out.reserve((norm.size() * 3) / 4);
    uint32_t val = 0;
    int valb = -8;
    for (unsigned char c : norm) {
        if (c == '=') break;
        const int d = kDec[c];
        if (d < 0) continue;
        val = (val << 6) | static_cast<uint32_t>(d);
        valb += 6;
        if (valb >= 0) {
            out.push_back(static_cast<char>((val >> valb) & 0xFF));
            valb -= 8;
        }
    }
    return out;
}

bool isRedditTokenV2LoggedIn(const std::string& raw_jwt) {
    const std::string jwt = stripQuotes(raw_jwt);
    if (jwt.empty()) return false;
    const size_t dot1 = jwt.find('.');
    if (dot1 == std::string::npos) return false;
    const size_t dot2 = jwt.find('.', dot1 + 1);
    if (dot2 == std::string::npos || dot2 <= dot1 + 1) return false;
    const std::string payload_str = base64UrlDecodeToString(jwt.substr(dot1 + 1, dot2 - dot1 - 1));
    auto j = nlohmann::json::parse(payload_str, nullptr, false);
    if (!j.is_object()) return false;
    const std::string sub = toLowerAscii(trimAscii(j.value("sub", "")));
    if (sub.empty() || sub == "anon" || sub == "logged_out" || sub == "anonymous") {
        return false;
    }
    return true;
}

struct CookieItem {
    std::string name;
    std::string value;
    std::string domain;
    std::string path = "/";
    bool host_only = false;
    bool secure = false;
    bool http_only = false;
    int64_t expires = 0;
};

std::unordered_map<std::string, std::string> parseCookieHeaderMap(const std::string& hdr) {
    std::unordered_map<std::string, std::string> out;
    std::istringstream iss(hdr);
    std::string part;
    while (std::getline(iss, part, ';')) {
        const std::string t = trimAscii(part);
        const size_t eq = t.find('=');
        if (eq == std::string::npos) continue;
        const std::string k = trimAscii(t.substr(0, eq));
        const std::string v = stripQuotes(t.substr(eq + 1));
        if (!k.empty()) {
            out[k] = v;
        }
    }
    return out;
}

std::vector<CookieItem> loadChromeCookiesCacheFile(const std::string& path) {
    std::vector<CookieItem> out;
    if (path.empty()) return out;
    std::ifstream ifs(path);
    if (!ifs.is_open()) return out;
    auto parsed = nlohmann::json::parse(ifs, nullptr, false);
    if (!parsed.is_object() || !parsed.contains("cookies") || !parsed["cookies"].is_array()) {
        return out;
    }
    const int64_t now_sec = static_cast<int64_t>(std::time(nullptr));
    for (const auto& item : parsed["cookies"]) {
        if (!item.is_object()) continue;
        CookieItem c;
        c.name = trimAscii(item.value("name", ""));
        c.value = item.value("value", "");
        c.domain = cleanDomain(item.value("domain", ""));
        c.path = item.value("path", "/");
        if (c.path.empty()) c.path = "/";
        c.host_only = item.value("hostOnly", false);
        c.secure = item.value("secure", false);
        c.http_only = item.value("httpOnly", false);
        if (item.contains("expirationDate") && item["expirationDate"].is_number()) {
            c.expires = static_cast<int64_t>(item["expirationDate"].get<double>());
        } else if (item.contains("expires") && item["expires"].is_number()) {
            c.expires = static_cast<int64_t>(item["expires"].get<double>());
        }
        if (c.expires > 0 && c.expires <= now_sec) continue;
        if (!c.name.empty() && !c.domain.empty()) {
            out.push_back(std::move(c));
        }
    }
    return out;
}

std::vector<CookieItem> loadNetscapeOrJsonCookiesFile(const std::string& path) {
    std::vector<CookieItem> out;
    if (path.empty()) return out;
    std::ifstream ifs(path);
    if (!ifs.is_open()) return out;
    std::ostringstream oss;
    oss << ifs.rdbuf();
    const std::string text = oss.str();
    if (text.empty()) return out;

    const int64_t now_sec = static_cast<int64_t>(std::time(nullptr));
    auto jp = nlohmann::json::parse(text, nullptr, false);
    if (!jp.is_discarded()) {
        const nlohmann::json* arr = nullptr;
        if (jp.is_array()) arr = &jp;
        else if (jp.is_object() && jp.contains("cookies") && jp["cookies"].is_array()) {
            arr = &jp["cookies"];
        }
        if (arr) {
            for (const auto& item : *arr) {
                if (!item.is_object()) continue;
                CookieItem c;
                c.name = trimAscii(item.value("name", ""));
                c.value = item.value("value", "");
                c.domain = cleanDomain(item.value("domain", ""));
                c.path = item.value("path", "/");
                c.host_only = item.value("hostOnly", false);
                c.secure = item.value("secure", false);
                c.http_only = item.value("httpOnly", false);
                if (item.contains("expirationDate") && item["expirationDate"].is_number()) {
                    c.expires = static_cast<int64_t>(item["expirationDate"].get<double>());
                } else if (item.contains("expires") && item["expires"].is_number()) {
                    c.expires = static_cast<int64_t>(item["expires"].get<double>());
                }
                if (c.expires > 0 && c.expires <= now_sec) continue;
                if (!c.name.empty() && !c.domain.empty()) out.push_back(std::move(c));
            }
            return out;
        }
    }

    std::istringstream lines(text);
    std::string line;
    while (std::getline(lines, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        std::string t = trimAscii(line);
        if (t.empty()) continue;
        bool http_only = false;
        if (t.rfind("#HttpOnly_", 0) == 0) {
            http_only = true;
            t = t.substr(10);
        } else if (t.front() == '#') {
            continue;
        }
        std::vector<std::string> cols;
        std::istringstream row(t);
        std::string cell;
        while (std::getline(row, cell, '\t')) cols.push_back(cell);
        if (cols.size() < 7) continue;
        CookieItem c;
        c.domain = cleanDomain(cols[0]);
        c.host_only = (toLowerAscii(trimAscii(cols[1])) != "true");
        c.path = cols[2].empty() ? "/" : cols[2];
        c.secure = (toLowerAscii(trimAscii(cols[3])) == "true");
        c.http_only = http_only;
        try {
            c.expires = std::stoll(trimAscii(cols[4]));
        } catch (...) {
            c.expires = 0;
        }
        if (c.expires > 0 && c.expires <= now_sec) continue;
        c.name = trimAscii(cols[5]);
        c.value = trimAscii(cols[6]);
        if (!c.name.empty() && !c.domain.empty()) {
            out.push_back(std::move(c));
        }
    }
    return out;
}

std::vector<CookieItem> filterCookiesForDomains(const std::vector<CookieItem>& all,
                                                const std::vector<std::string>& domains) {
    std::vector<CookieItem> out;
    const int64_t now_sec = static_cast<int64_t>(std::time(nullptr));
    for (const auto& c : all) {
        if (c.expires > 0 && c.expires <= now_sec) continue;
        for (const auto& dom : domains) {
            if (domainMatchesTarget(c.domain, dom)) {
                out.push_back(c);
                break;
            }
        }
    }
    return out;
}

std::string buildCookieHeaderFromItems(const std::vector<CookieItem>& items) {
    std::vector<const CookieItem*> sorted;
    sorted.reserve(items.size());
    for (const auto& c : items) {
        if (!c.name.empty()) sorted.push_back(&c);
    }
    std::sort(sorted.begin(), sorted.end(), [](const CookieItem* a, const CookieItem* b) {
        return a->path.size() > b->path.size();
    });
    std::ostringstream oss;
    std::set<std::string> seen;
    bool first = true;
    for (const CookieItem* c : sorted) {
        if (!seen.insert(c->name).second) continue;
        if (!first) oss << "; ";
        first = false;
        oss << c->name << "=" << c->value;
    }
    return oss.str();
}

bool writeFileAtomic0600(const fs::path& path, const std::string& content) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    const fs::path tmp = path.string() + ".tmp." + std::to_string(::getpid());
    int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;
    size_t written = 0;
    while (written < content.size()) {
        ssize_t n = ::write(fd, content.data() + written, content.size() - written);
        if (n <= 0) {
            if (errno == EINTR) continue;
            ::close(fd);
            fs::remove(tmp, ec);
            return false;
        }
        written += static_cast<size_t>(n);
    }
    ::fchmod(fd, 0600);
    ::close(fd);
    fs::rename(tmp, path, ec);
    if (ec) {
        fs::remove(tmp, ec);
        return false;
    }
    return true;
}

void writeNetscapeCookiesFile(const fs::path& path, const std::vector<CookieItem>& cookies) {
    std::ostringstream oss;
    oss << "# Netscape HTTP Cookie File\n"
        << "# Auto-synced by OmniDesk AuthCenterManager (0600)\n\n";
    for (const auto& c : cookies) {
        if (c.name.empty() || c.domain.empty()) continue;
        const std::string dom =
            (!c.host_only && c.domain.front() != '.') ? ("." + c.domain) : c.domain;
        if (c.http_only) oss << "#HttpOnly_";
        oss << dom << "\t"
            << (c.host_only ? "FALSE" : "TRUE") << "\t"
            << (c.path.empty() ? "/" : c.path) << "\t"
            << (c.secure ? "TRUE" : "FALSE") << "\t"
            << (c.expires > 0 ? c.expires : 0) << "\t"
            << c.name << "\t"
            << c.value << "\n";
    }
    writeFileAtomic0600(path, oss.str());
}

void mergeIntoChromeWebCookiesCache(const std::string& data_root,
                                    const std::vector<CookieItem>& items) {
    if (data_root.empty() || items.empty()) return;
    const fs::path cache_path =
        fs::path(data_root) / "plugin-data" / "plugin-chrome-web" / "cookies_cache.json";
    std::vector<CookieItem> existing = loadChromeCookiesCacheFile(cache_path.string());

    auto makeKey = [](const CookieItem& c) {
        return cleanDomain(c.domain) + "|" + (c.path.empty() ? "/" : c.path) + "|" + c.name;
    };
    std::unordered_map<std::string, size_t> idx;
    for (size_t i = 0; i < existing.size(); ++i) {
        idx[makeKey(existing[i])] = i;
    }
    for (const auto& c : items) {
        const std::string k = makeKey(c);
        auto it = idx.find(k);
        if (it != idx.end()) {
            existing[it->second] = c;
        } else {
            idx[k] = existing.size();
            existing.push_back(c);
        }
    }

    nlohmann::json arr = nlohmann::json::array();
    for (const auto& c : existing) {
        const std::string dom =
            (!c.host_only && !c.domain.empty() && c.domain.front() != '.')
                ? ("." + c.domain)
                : c.domain;
        arr.push_back({
            {"name", c.name},
            {"value", c.value},
            {"domain", dom},
            {"path", c.path.empty() ? "/" : c.path},
            {"hostOnly", c.host_only},
            {"secure", c.secure},
            {"httpOnly", c.http_only},
            {"expirationDate", c.expires},
        });
    }
    nlohmann::json out = {{"cookies", std::move(arr)}};
    writeFileAtomic0600(cache_path, out.dump(2));
}

bool isExecutableFile(const fs::path& p) {
    if (p.empty()) return false;
    std::error_code ec;
    if (!fs::exists(p, ec) || !fs::is_regular_file(p, ec)) return false;
    return ::access(p.c_str(), X_OK) == 0;
}

std::string searchPathBinary(const std::string& name) {
    if (name.empty()) return {};
    if (name.find('/') != std::string::npos) {
        return isExecutableFile(name) ? name : std::string{};
    }
    const char* path_env = std::getenv("PATH");
    if (!path_env) return {};
    std::istringstream iss(path_env);
    std::string dir;
    while (std::getline(iss, dir, ':')) {
        if (dir.empty()) continue;
        fs::path cand = fs::path(dir) / name;
        if (isExecutableFile(cand)) return cand.string();
    }
    return {};
}

std::string findChromeExecutable() {
    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        const auto cw_cfg = kernel.config().loadPluginConfig("plugin-chrome-web");
        if (cw_cfg.is_object()) {
            const std::string cfg_bin = trimAscii(cw_cfg.value("chrome_bin", ""));
            const std::string resolved = searchPathBinary(cfg_bin);
            if (!resolved.empty()) return resolved;
        }
    }
    if (const char* env_bin = std::getenv("OMNIDESK_CHROME_BIN")) {
        const std::string resolved = searchPathBinary(env_bin);
        if (!resolved.empty()) return resolved;
    }
    if (const char* home = std::getenv("HOME")) {
        const fs::path local_bin = fs::path(home) / ".local" / "bin" / "google-chrome";
        if (isExecutableFile(local_bin)) return local_bin.string();
    }
    static const char* kCandidates[] = {
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/chrome",
    };
    for (const char* c : kCandidates) {
        const std::string resolved = searchPathBinary(c);
        if (!resolved.empty()) return resolved;
    }
    return {};
}

void terminatePidGroupGracefully(int pid) {
    if (pid <= 1 || pid == ::getpid()) return;
    ::kill(-pid, SIGTERM);
    ::kill(pid, SIGTERM);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(450);
    while (std::chrono::steady_clock::now() < deadline) {
        int status = 0;
        pid_t rc = ::waitpid(pid, &status, WNOHANG);
        if (rc == pid || (rc < 0 && errno == ECHILD)) return;
        if (::kill(pid, 0) != 0) return;
        struct timespec ts{0, 25 * 1000 * 1000};
        ::nanosleep(&ts, nullptr);
    }
    ::kill(-pid, SIGKILL);
    ::kill(pid, SIGKILL);
    int status = 0;
    ::waitpid(pid, &status, WNOHANG);
}

void reclaimAuthProfileLock(const std::string& profile_dir) {
    if (profile_dir.empty()) return;
    std::set<pid_t> pids;
    if (DIR* dir = ::opendir("/proc")) {
        const uid_t my_uid = ::getuid();
        while (struct dirent* ent = ::readdir(dir)) {
            if (!std::isdigit(static_cast<unsigned char>(ent->d_name[0]))) continue;
            char* endp = nullptr;
            long val = std::strtol(ent->d_name, &endp, 10);
            if (!endp || *endp != '\0' || val <= 1 || val == ::getpid()) continue;
            struct stat st{};
            const std::string proc_dir = std::string("/proc/") + ent->d_name;
            if (::stat(proc_dir.c_str(), &st) != 0 || st.st_uid != my_uid) continue;
            std::ifstream ifs(proc_dir + "/cmdline", std::ios::binary);
            if (!ifs) continue;
            std::string cmd((std::istreambuf_iterator<char>(ifs)), std::istreambuf_iterator<char>());
            for (char& c : cmd) {
                if (c == '\0') c = ' ';
            }
            if ((cmd.find("chrome") != std::string::npos ||
                 cmd.find("chromium") != std::string::npos) &&
                cmd.find("--user-data-dir=" + profile_dir) != std::string::npos &&
                cmd.find("--type=") == std::string::npos) {
                pids.insert(static_cast<pid_t>(val));
            }
        }
        ::closedir(dir);
    }
    for (pid_t pid : pids) {
        terminatePidGroupGracefully(static_cast<int>(pid));
    }
    std::error_code ec;
    fs::remove(fs::path(profile_dir) / "SingletonLock", ec);
    fs::remove(fs::path(profile_dir) / "SingletonCookie", ec);
    fs::remove(fs::path(profile_dir) / "SingletonSocket", ec);
}

/**
 * Minimal local loopback RFC 6455 WebSocket client for Chrome DevTools Protocol
 * (`127.0.0.1:<DevToolsActivePort>` -> `Storage.getCookies` / `Storage.setCookies`).
 */
class LocalCdpSocket {
public:
    LocalCdpSocket() = default;
    ~LocalCdpSocket() { close(); }

    bool connectLoopback(int port, const std::string& ws_path, int timeout_ms = 2500) {
        close();
        if (port <= 0 || port > 65535 || ws_path.empty()) return false;

        int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd < 0) return false;

        int one = 1;
        ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

        const int orig_flags = ::fcntl(fd, F_GETFL, 0);
        ::fcntl(fd, F_SETFL, orig_flags | O_NONBLOCK);

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<uint16_t>(port));
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

        int rc = ::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
        if (rc < 0 && errno != EINPROGRESS) {
            ::close(fd);
            return false;
        }
        if (rc < 0) {
            pollfd pfd{fd, POLLOUT, 0};
            if (::poll(&pfd, 1, timeout_ms) <= 0) {
                ::close(fd);
                return false;
            }
            int so_err = 0;
            socklen_t slen = sizeof(so_err);
            if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &so_err, &slen) < 0 || so_err != 0) {
                ::close(fd);
                return false;
            }
        }
        ::fcntl(fd, F_SETFL, orig_flags);
        fd_ = fd;

        uint8_t nonce[16]{};
        if (RAND_bytes(nonce, sizeof(nonce)) != 1) {
            for (size_t i = 0; i < sizeof(nonce); ++i) nonce[i] = static_cast<uint8_t>(i * 17 + 41);
        }
        const std::string ws_key = base64EncodeBytes(nonce, sizeof(nonce));

        std::ostringstream req;
        req << "GET " << ws_path << " HTTP/1.1\r\n"
            << "Host: 127.0.0.1:" << port << "\r\n"
            << "Upgrade: websocket\r\n"
            << "Connection: Upgrade\r\n"
            << "Sec-WebSocket-Key: " << ws_key << "\r\n"
            << "Sec-WebSocket-Version: 13\r\n\r\n";

        if (!sendAll(req.str(), timeout_ms)) {
            close();
            return false;
        }

        std::string hdr_buf;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now() < deadline) {
            const auto pos = rx_buf_.find("\r\n\r\n");
            if (pos != std::string::npos) {
                hdr_buf = rx_buf_.substr(0, pos);
                rx_buf_.erase(0, pos + 4);
                break;
            }
            if (!pumpRecv(250)) {
                close();
                return false;
            }
        }
        if (hdr_buf.find(" 101 ") == std::string::npos) {
            close();
            return false;
        }
        return true;
    }

    bool isConnected() const { return fd_ >= 0; }

    void close() {
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
        rx_buf_.clear();
    }

    bool call(const std::string& method,
              const nlohmann::json& params,
              int timeout_ms,
              nlohmann::json* out_result) {
        if (fd_ < 0) return false;
        const int req_id = ++next_id_;
        const std::string payload = nlohmann::json{
            {"id", req_id},
            {"method", method},
            {"params", params.is_object() ? params : nlohmann::json::object()},
        }.dump();

        if (!sendFrame(0x1, payload, timeout_ms)) {
            close();
            return false;
        }

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        std::string frag_accum;
        while (std::chrono::steady_clock::now() < deadline) {
            uint8_t opcode = 0;
            bool fin = true;
            std::string frame_data;
            const auto rem = std::chrono::duration_cast<std::chrono::milliseconds>(
                deadline - std::chrono::steady_clock::now()).count();
            if (!readFrame(static_cast<int>(std::max<int64_t>(50, rem)), opcode, fin, frame_data)) {
                close();
                return false;
            }
            if (opcode == 0x8) { // Close
                close();
                return false;
            }
            if (opcode == 0x9) { // Ping -> Pong
                sendFrame(0xA, frame_data, 500);
                continue;
            }
            if (opcode == 0xA) {
                continue;
            }
            if (opcode == 0x1) {
                frag_accum = std::move(frame_data);
            } else if (opcode == 0x0) {
                frag_accum.append(frame_data);
            }
            if (!fin) continue;

            auto msg = nlohmann::json::parse(frag_accum, nullptr, false);
            frag_accum.clear();
            if (!msg.is_object()) continue;
            if (msg.value("id", -1) != req_id) continue;
            if (msg.contains("error")) return false;
            if (out_result) {
                *out_result = msg.contains("result") ? msg["result"] : nlohmann::json::object();
            }
            return true;
        }
        return false;
    }

private:
    bool sendAll(const std::string& bytes, int timeout_ms) {
        size_t sent = 0;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (sent < bytes.size()) {
            const auto rem = std::chrono::duration_cast<std::chrono::milliseconds>(
                deadline - std::chrono::steady_clock::now()).count();
            if (rem <= 0) return false;
            pollfd pfd{fd_, POLLOUT, 0};
            int pr = ::poll(&pfd, 1, static_cast<int>(rem));
            if (pr <= 0) {
                if (pr < 0 && errno == EINTR) continue;
                return false;
            }
            ssize_t n = ::send(fd_, bytes.data() + sent, bytes.size() - sent, MSG_NOSIGNAL);
            if (n <= 0) {
                if (n < 0 && errno == EINTR) continue;
                return false;
            }
            sent += static_cast<size_t>(n);
        }
        return true;
    }

    bool pumpRecv(int timeout_ms) {
        pollfd pfd{fd_, POLLIN, 0};
        int pr = ::poll(&pfd, 1, timeout_ms);
        if (pr <= 0) return false;
        char buf[16384];
        ssize_t n = ::recv(fd_, buf, sizeof(buf), 0);
        if (n <= 0) return false;
        rx_buf_.append(buf, static_cast<size_t>(n));
        return true;
    }

    bool ensureRxBytes(size_t needed, int timeout_ms) {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (rx_buf_.size() < needed) {
            const auto rem = std::chrono::duration_cast<std::chrono::milliseconds>(
                deadline - std::chrono::steady_clock::now()).count();
            if (rem <= 0) return false;
            if (!pumpRecv(static_cast<int>(rem))) return false;
        }
        return true;
    }

    bool sendFrame(uint8_t opcode, const std::string& payload, int timeout_ms) {
        std::string frame;
        const size_t len = payload.size();
        frame.reserve(len + 14);
        frame.push_back(static_cast<char>(0x80u | (opcode & 0x0Fu)));
        if (len < 126) {
            frame.push_back(static_cast<char>(0x80u | static_cast<uint8_t>(len)));
        } else if (len <= 0xFFFFu) {
            frame.push_back(static_cast<char>(0x80u | 126u));
            frame.push_back(static_cast<char>((len >> 8) & 0xFFu));
            frame.push_back(static_cast<char>(len & 0xFFu));
        } else {
            frame.push_back(static_cast<char>(0x80u | 127u));
            for (int shift = 56; shift >= 0; shift -= 8) {
                frame.push_back(static_cast<char>((static_cast<uint64_t>(len) >> shift) & 0xFFu));
            }
        }
        uint8_t mask[4]{};
        if (RAND_bytes(mask, sizeof(mask)) != 1) {
            mask[0] = 0x37; mask[1] = 0xFA; mask[2] = 0x21; mask[3] = 0x3D;
        }
        for (int i = 0; i < 4; ++i) frame.push_back(static_cast<char>(mask[i]));
        for (size_t i = 0; i < len; ++i) {
            frame.push_back(static_cast<char>(static_cast<uint8_t>(payload[i]) ^ mask[i & 3]));
        }
        return sendAll(frame, timeout_ms);
    }

    bool readFrame(int timeout_ms, uint8_t& out_opcode, bool& out_fin, std::string& out_data) {
        if (!ensureRxBytes(2, timeout_ms)) return false;
        const uint8_t b0 = static_cast<uint8_t>(rx_buf_[0]);
        const uint8_t b1 = static_cast<uint8_t>(rx_buf_[1]);
        out_fin = (b0 & 0x80u) != 0;
        out_opcode = b0 & 0x0Fu;
        const bool masked = (b1 & 0x80u) != 0;
        uint64_t payload_len = b1 & 0x7Fu;
        size_t header_len = 2;

        if (payload_len == 126) {
            header_len += 2;
            if (!ensureRxBytes(header_len, timeout_ms)) return false;
            payload_len = (static_cast<uint64_t>(static_cast<uint8_t>(rx_buf_[2])) << 8) |
                          static_cast<uint64_t>(static_cast<uint8_t>(rx_buf_[3]));
        } else if (payload_len == 127) {
            header_len += 8;
            if (!ensureRxBytes(header_len, timeout_ms)) return false;
            payload_len = 0;
            for (int i = 0; i < 8; ++i) {
                payload_len = (payload_len << 8) |
                              static_cast<uint64_t>(static_cast<uint8_t>(rx_buf_[2 + i]));
            }
        }
        if (payload_len > 32 * 1024 * 1024) return false; // 32 MiB sanity cap
        uint8_t mask[4]{};
        if (masked) {
            if (!ensureRxBytes(header_len + 4, timeout_ms)) return false;
            for (int i = 0; i < 4; ++i) {
                mask[i] = static_cast<uint8_t>(rx_buf_[header_len + i]);
            }
            header_len += 4;
        }
        const size_t total_len = header_len + static_cast<size_t>(payload_len);
        if (!ensureRxBytes(total_len, timeout_ms)) return false;

        out_data.assign(rx_buf_.data() + header_len, static_cast<size_t>(payload_len));
        if (masked) {
            for (size_t i = 0; i < out_data.size(); ++i) {
                out_data[i] = static_cast<char>(static_cast<uint8_t>(out_data[i]) ^ mask[i & 3]);
            }
        }
        rx_buf_.erase(0, total_len);
        return true;
    }

    int fd_ = -1;
    int next_id_ = 0;
    std::string rx_buf_;
};

std::vector<CookieItem> pullCookiesOverCdp(LocalCdpSocket& cdp) {
    nlohmann::json res;
    if (!cdp.call("Storage.getCookies", nlohmann::json::object(), 2000, &res)) {
        if (!cdp.call("Network.getAllCookies", nlohmann::json::object(), 2000, &res)) {
            return {};
        }
    }
    if (!res.is_object() || !res.contains("cookies") || !res["cookies"].is_array()) {
        return {};
    }
    std::vector<CookieItem> out;
    out.reserve(res["cookies"].size());
    for (const auto& item : res["cookies"]) {
        if (!item.is_object()) continue;
        CookieItem c;
        c.name = trimAscii(item.value("name", ""));
        c.value = item.value("value", "");
        const std::string raw_dom = trimAscii(item.value("domain", ""));
        c.host_only = !raw_dom.empty() && raw_dom.front() != '.';
        c.domain = cleanDomain(raw_dom);
        c.path = item.value("path", "/");
        if (c.path.empty()) c.path = "/";
        c.secure = item.value("secure", false);
        c.http_only = item.value("httpOnly", false);
        if (item.contains("expires") && item["expires"].is_number()) {
            const double exp = item["expires"].get<double>();
            c.expires = exp > 0.0 ? static_cast<int64_t>(exp) : 0;
        }
        if (!c.name.empty() && !c.domain.empty()) {
            out.push_back(std::move(c));
        }
    }
    return out;
}

void seedExistingChromeCookiesIntoCdpOnce(LocalCdpSocket& cdp,
                                          const std::string& profile_dir,
                                          const std::string& data_root) {
    if (profile_dir.empty() || data_root.empty()) return;
    const fs::path marker = fs::path(profile_dir) / ".omnidesk_seeded";
    std::error_code ec;
    if (fs::exists(marker, ec)) return;

    const fs::path cache_file =
        fs::path(data_root) / "plugin-data" / "plugin-chrome-web" / "cookies_cache.json";
    const auto existing = loadChromeCookiesCacheFile(cache_file.string());
    if (!existing.empty()) {
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& c : existing) {
            if (c.name.empty() || c.domain.empty()) continue;
            nlohmann::json cj = {
                {"name", c.name},
                {"value", c.value},
                {"domain", c.host_only ? c.domain : ("." + c.domain)},
                {"path", c.path.empty() ? "/" : c.path},
                {"secure", c.secure},
                {"httpOnly", c.http_only},
            };
            if (c.expires > 0) cj["expires"] = c.expires;
            arr.push_back(std::move(cj));
        }
        if (!arr.empty()) {
            nlohmann::json ignored;
            cdp.call("Storage.setCookies", {{"cookies", std::move(arr)}}, 2500, &ignored);
        }
    }
    writeFileAtomic0600(marker, "seeded\n");
}

struct CookieEvaluation {
    bool strict_authenticated = false;
    bool has_any_cookies = false;
    std::vector<std::string> verified_keys;
    std::vector<std::string> missing_keys;
    std::string auth_fingerprint;
};

CookieEvaluation evaluatePluginCookieMap(const std::string& plugin_id,
                                         const std::unordered_map<std::string, std::string>& cmap) {
    CookieEvaluation ev;
    ev.has_any_cookies = !cmap.empty();

    auto hasNonEmpty = [&](const char* key) -> bool {
        auto it = cmap.find(key);
        return it != cmap.end() && !trimAscii(it->second).empty();
    };
    auto getVal = [&](const char* key) -> std::string {
        auto it = cmap.find(key);
        return it != cmap.end() ? trimAscii(it->second) : std::string{};
    };

    if (plugin_id == "plugin-zhihu") {
        if (hasNonEmpty("z_c0")) {
            ev.strict_authenticated = true;
            ev.verified_keys.push_back("z_c0");
            ev.auth_fingerprint = getVal("z_c0");
        } else {
            ev.missing_keys.push_back("z_c0");
        }
        if (hasNonEmpty("d_c0")) ev.verified_keys.push_back("d_c0");
        if (hasNonEmpty("_xsrf")) ev.verified_keys.push_back("_xsrf");
        if (hasNonEmpty("__zse_ck")) ev.verified_keys.push_back("__zse_ck");
    } else if (plugin_id == "plugin-linuxdo") {
        if (hasNonEmpty("_t")) {
            ev.strict_authenticated = true;
            ev.verified_keys.push_back("_t");
            ev.auth_fingerprint = getVal("_t");
        } else {
            ev.missing_keys.push_back("_t");
        }
        if (hasNonEmpty("_forum_session")) ev.verified_keys.push_back("_forum_session");
        if (hasNonEmpty("cf_clearance")) ev.verified_keys.push_back("cf_clearance");
    } else if (plugin_id == "plugin-shuiyuan") {
        if (hasNonEmpty("_t")) {
            ev.strict_authenticated = true;
            ev.verified_keys.push_back("_t");
            ev.auth_fingerprint = getVal("_t");
        } else {
            ev.missing_keys.push_back("_t");
        }
        if (hasNonEmpty("_forum_session")) ev.verified_keys.push_back("_forum_session");
    } else if (plugin_id == "plugin-v2ex") {
        if (hasNonEmpty("A2")) {
            ev.strict_authenticated = true;
            ev.verified_keys.push_back("A2");
            ev.auth_fingerprint = getVal("A2");
        } else {
            ev.missing_keys.push_back("A2");
        }
        if (hasNonEmpty("PB3_SESSION")) ev.verified_keys.push_back("PB3_SESSION");
    } else if (plugin_id == "plugin-reddit") {
        const bool has_rs = hasNonEmpty("reddit_session");
        const bool has_tv2_user = hasNonEmpty("token_v2") && isRedditTokenV2LoggedIn(getVal("token_v2"));
        if (has_rs) ev.verified_keys.push_back("reddit_session");
        if (has_tv2_user) ev.verified_keys.push_back("token_v2(实名会话)");
        if (has_rs || has_tv2_user) {
            ev.strict_authenticated = true;
            ev.auth_fingerprint = getVal("reddit_session") + "|" + getVal("token_v2");
        } else {
            ev.missing_keys.push_back("reddit_session 或 登录态 token_v2");
        }
    } else if (plugin_id == "plugin-x") {
        const bool has_at = hasNonEmpty("auth_token");
        const bool has_ct0 = hasNonEmpty("ct0");
        if (has_at) ev.verified_keys.push_back("auth_token");
        else ev.missing_keys.push_back("auth_token");
        if (has_ct0) ev.verified_keys.push_back("ct0");
        else ev.missing_keys.push_back("ct0");
        if (hasNonEmpty("twid")) ev.verified_keys.push_back("twid");
        if (has_at && has_ct0) {
            ev.strict_authenticated = true;
            ev.auth_fingerprint = getVal("auth_token") + "|" + getVal("ct0");
        }
    } else if (plugin_id == "plugin-youtube") {
        const bool has_sapisid =
            hasNonEmpty("SAPISID") || hasNonEmpty("__Secure-1PAPISID") || hasNonEmpty("__Secure-3PAPISID");
        const bool has_sid =
            hasNonEmpty("SID") || hasNonEmpty("__Secure-1PSID") || hasNonEmpty("__Secure-3PSID");
        if (has_sapisid) ev.verified_keys.push_back("SAPISID");
        else ev.missing_keys.push_back("SAPISID");
        if (has_sid) ev.verified_keys.push_back("SID/__Secure-1PSID");
        else ev.missing_keys.push_back("SID/__Secure-1PSID");
        if (has_sapisid && has_sid) {
            ev.strict_authenticated = true;
            ev.auth_fingerprint =
                getVal("SAPISID") + "|" + getVal("__Secure-1PSID") + "|" + getVal("SID");
        }
    } else if (plugin_id == "plugin-bilibili") {
        const bool has_sd = hasNonEmpty("SESSDATA");
        const bool has_jct = hasNonEmpty("bili_jct");
        if (has_sd) ev.verified_keys.push_back("SESSDATA");
        else ev.missing_keys.push_back("SESSDATA");
        if (has_jct) ev.verified_keys.push_back("bili_jct");
        else ev.missing_keys.push_back("bili_jct");
        if (hasNonEmpty("DedeUserID")) ev.verified_keys.push_back("DedeUserID");
        if (has_sd && has_jct) {
            ev.strict_authenticated = true;
            ev.auth_fingerprint = getVal("SESSDATA") + "|" + getVal("bili_jct");
        }
    } else if (plugin_id == "plugin-zlib") {
        const bool has_uid = hasNonEmpty("remix_userid");
        const bool has_ukey = hasNonEmpty("remix_userkey");
        if (has_uid) ev.verified_keys.push_back("remix_userid");
        else ev.missing_keys.push_back("remix_userid");
        if (has_ukey) ev.verified_keys.push_back("remix_userkey");
        else ev.missing_keys.push_back("remix_userkey");
        if (has_uid && has_ukey) {
            ev.strict_authenticated = true;
            ev.auth_fingerprint = getVal("remix_userid") + "|" + getVal("remix_userkey");
        }
    } else if (plugin_id == "plugin-music") {
        const std::string mut =
            hasNonEmpty("media-user-token") ? getVal("media-user-token") : getVal("media_user_token");
        if (!mut.empty()) {
            ev.strict_authenticated = true;
            ev.verified_keys.push_back("media-user-token");
            ev.auth_fingerprint = mut;
        } else {
            ev.missing_keys.push_back("media-user-token");
        }
    } else {
        // Flexible / challenge cookie sites (Anna's Archive, SubHD, Blog, Feed Recipe)
        if (hasNonEmpty("cf_clearance")) ev.verified_keys.push_back("cf_clearance");
        for (const auto& [k, v] : cmap) {
            if (v.empty()) continue;
            const std::string lk = toLowerAscii(k);
            if (lk.find("session") != std::string::npos ||
                lk.find("sid") != std::string::npos ||
                lk.find("token") != std::string::npos ||
                lk.find("auth") != std::string::npos ||
                lk == "_t" || lk == "substack.sid" || lk == "uid") {
                if (ev.verified_keys.size() < 4) ev.verified_keys.push_back(k);
            }
        }
        if (!ev.verified_keys.empty()) {
            ev.strict_authenticated = true;
            for (const auto& k : ev.verified_keys) {
                auto it = cmap.find(k);
                if (it != cmap.end()) ev.auth_fingerprint += k + "=" + it->second + ";";
            }
        }
    }
    return ev;
}

bool isStrictRulePlugin(const std::string& plugin_id) {
    return plugin_id == "plugin-zhihu" ||
           plugin_id == "plugin-linuxdo" ||
           plugin_id == "plugin-shuiyuan" ||
           plugin_id == "plugin-v2ex" ||
           plugin_id == "plugin-reddit" ||
           plugin_id == "plugin-x" ||
           plugin_id == "plugin-youtube" ||
           plugin_id == "plugin-bilibili" ||
           plugin_id == "plugin-zlib" ||
           plugin_id == "plugin-music";
}

std::vector<std::string> domainsForPlugin(const std::string& plugin_id,
                                          const std::string& effective_url) {
    if (plugin_id == "plugin-zhihu") return {"zhihu.com"};
    if (plugin_id == "plugin-linuxdo") return {"linux.do"};
    if (plugin_id == "plugin-shuiyuan") return {"shuiyuan.sjtu.edu.cn", "jaccount.sjtu.edu.cn"};
    if (plugin_id == "plugin-v2ex") return {"v2ex.com"};
    if (plugin_id == "plugin-reddit") return {"reddit.com"};
    if (plugin_id == "plugin-x") return {"x.com", "twitter.com"};
    if (plugin_id == "plugin-youtube") return {"youtube.com", "google.com", "accounts.google.com"};
    if (plugin_id == "plugin-bilibili") return {"bilibili.com"};
    if (plugin_id == "plugin-music") return {"music.apple.com", "apple.com"};
    if (plugin_id == "plugin-sub-hunter") return {"subhd.tv", "subhd.com"};

    const std::string host = extractHostFromUrl(effective_url);
    const std::string root = guessRegistrableRootDomain(host);
    if (!root.empty() && root != host) return {root, host};
    if (!host.empty()) return {host};

    if (plugin_id == "plugin-zlib") return {"z-lib.gd", "singlelogin.re", "z-library.sk"};
    if (plugin_id == "plugin-annas-archive") return {"annas-archive.org", "annas-archive.li", "annas-archive.se"};
    return {};
}

std::string cookieSecretKeyForPlugin(const std::string& plugin_id) {
    if (plugin_id == "plugin-sub-hunter") return "subhd_cookie";
    if (plugin_id == "plugin-feed-recipe") return "custom_cookie_header";
    if (plugin_id == "plugin-music") return "media_user_token";
    if (plugin_id == "plugin-bilibili") return "web_sessdata";
    return "cookie_header";
}

std::string joinList(const std::vector<std::string>& items, const std::string& sep = ", ") {
    std::string out;
    for (size_t i = 0; i < items.size(); ++i) {
        if (i > 0) out += sep;
        out += items[i];
    }
    return out;
}

void styleStatusTokens(AuthStatusKind st,
                       std::string& badge,
                       std::string& bg_tok,
                       std::string& fg_tok) {
    switch (st) {
        case AuthStatusKind::Authenticated:
            badge = "✓ 已登录授权";
            bg_tok = "status/ok_bg";
            fg_tok = "status/ok_fg";
            break;
        case AuthStatusKind::GuestCookieOnly:
            badge = "⚠ 仅访客Cookie·未登录";
            bg_tok = "status/warn_bg";
            fg_tok = "status/warn_fg";
            break;
        case AuthStatusKind::OptionalGuest:
            badge = "○ 游客可用·可登录";
            bg_tok = "status/info_bg";
            fg_tok = "status/info_fg";
            break;
        case AuthStatusKind::MissingRequired:
            badge = "✕ 待授权配置";
            bg_tok = "status/err_bg";
            fg_tok = "status/err_fg";
            break;
        case AuthStatusKind::NoAuthNeeded:
            badge = "免登录底座";
            bg_tok = "desktop/soft";
            fg_tok = "font/grey";
            break;
    }
}

bool focusIsInsideView(brls::View* ancestor) {
    if (!ancestor) return false;
    brls::View* cur = brls::Application::getCurrentFocus();
    while (cur) {
        if (cur == ancestor) return true;
        cur = cur->getParent();
    }
    return false;
}

void clearBoxSafely(brls::Box* box) {
    if (!box) return;
    if (focusIsInsideView(box)) {
        brls::Application::giveFocus(nullptr);
    }
    box->clearViews();
}

std::optional<WorkspaceTabSpec> findCustomTab(const std::string& plugin_id) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return std::nullopt;
    for (const auto& t : kernel.domains().workspaceTabs()) {
        if (t.plugin_id == plugin_id && !t.tab_id.empty()) return t;
    }
    return std::nullopt;
}

void persistCapturedCookiesToHost(const std::string& plugin_id,
                                  const std::string& target_url,
                                  const std::vector<CookieItem>& domain_cookies) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || domain_cookies.empty()) return;

    const std::string cookie_hdr = buildCookieHeaderFromItems(domain_cookies);
    const auto cmap = parseCookieHeaderMap(cookie_hdr);

    // 1. Update Chrome CookieStore (both live capability & persistent cache file)
    mergeIntoChromeWebCookiesCache(kernel.dataRoot(), domain_cookies);
    if (kernel.capabilities().has("web.cookies.v1")) {
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& c : domain_cookies) {
            arr.push_back({
                {"name", c.name},
                {"value", c.value},
                {"domain", c.host_only ? c.domain : ("." + c.domain)},
                {"path", c.path.empty() ? "/" : c.path},
                {"hostOnly", c.host_only},
                {"secure", c.secure},
                {"httpOnly", c.http_only},
                {"expirationDate", c.expires},
            });
        }
        const std::string dom = guessRegistrableRootDomain(extractHostFromUrl(target_url));
        kernel.capabilities().call("web.cookies.v1", {
            {"action", "set_cookies"},
            {"domain", dom},
            {"cookies", std::move(arr)},
        });
    }

    // 2. Save into plugin's SecretVault & structured config keys
    if (plugin_id == "plugin-bilibili") {
        if (auto it = cmap.find("SESSDATA"); it != cmap.end() && !it->second.empty()) {
            kernel.secrets().setSecret(plugin_id, "web_sessdata", it->second);
        }
        if (auto it = cmap.find("bili_jct"); it != cmap.end() && !it->second.empty()) {
            kernel.secrets().setSecret(plugin_id, "web_bili_jct", it->second);
        }
        if (auto it = cmap.find("buvid3"); it != cmap.end() && !it->second.empty()) {
            kernel.secrets().setSecret(plugin_id, "web_buvid3", it->second);
        }
        if (auto it = cmap.find("DedeUserID"); it != cmap.end() && !it->second.empty()) {
            kernel.config().setPluginValue(plugin_id, "web_dede_userid", it->second);
        }
    } else if (plugin_id == "plugin-music") {
        std::string mut;
        if (auto it = cmap.find("media-user-token"); it != cmap.end()) mut = it->second;
        else if (auto it2 = cmap.find("media_user_token"); it2 != cmap.end()) mut = it2->second;
        if (!mut.empty()) {
            kernel.secrets().setSecret(plugin_id, "media_user_token", mut);
        }
    } else if (plugin_id == "plugin-zlib") {
        if (!cookie_hdr.empty()) {
            kernel.secrets().setSecret(plugin_id, "cookie_header", cookie_hdr);
        }
        if (auto it = cmap.find("remix_userid"); it != cmap.end() && !it->second.empty()) {
            kernel.config().setPluginValue(plugin_id, "remix_userid", it->second);
        }
        if (auto it = cmap.find("remix_userkey"); it != cmap.end() && !it->second.empty()) {
            kernel.secrets().setSecret(plugin_id, "remix_userkey", it->second);
        }
    } else if (plugin_id == "plugin-youtube") {
        if (!cookie_hdr.empty()) {
            kernel.secrets().setSecret(plugin_id, "cookie_header", cookie_hdr);
        }
        nlohmann::json yt_cfg = kernel.config().loadPluginConfig(plugin_id);
        std::string cfile = yt_cfg.is_object() ? trimAscii(yt_cfg.value("cookies_file", "")) : "";
        if (cfile.empty()) {
            cfile = (fs::path(kernel.dataRoot()) / "plugin-data" / "plugin-youtube" / "cookies.txt").string();
            kernel.config().setPluginValue(plugin_id, "cookies_file", cfile);
        }
        writeNetscapeCookiesFile(cfile, domain_cookies);
    } else {
        const std::string skey = cookieSecretKeyForPlugin(plugin_id);
        if (!skey.empty() && !cookie_hdr.empty()) {
            kernel.secrets().setSecret(plugin_id, skey, cookie_hdr);
        }
        nlohmann::json pcfg = kernel.config().loadPluginConfig(plugin_id);
        if (pcfg.is_object() &&
            toLowerAscii(trimAscii(pcfg.value("chrome_cookie_source", "auto"))) == "disabled") {
            kernel.config().setPluginValue(plugin_id, "chrome_cookie_source", "auto");
        }
    }
}

} // namespace

AuthCenterManager& AuthCenterManager::instance() {
    static AuthCenterManager inst;
    return inst;
}

AuthCenterManager::~AuthCenterManager() {
    stop();
}

void AuthCenterManager::stop() {
    int pid_to_kill = -1;
    {
        std::lock_guard<std::mutex> lock(login_mutex_);
        stopping_ = true;
        login_cancel_requested_ = true;
        ++login_seq_;
        pid_to_kill = login_chrome_pid_;
        login_chrome_pid_ = -1;
        login_running_ = false;
    }
    login_cv_.notify_all();
    if (pid_to_kill > 1) {
        terminatePidGroupGracefully(pid_to_kill);
    }
    std::unique_lock<std::mutex> lock(login_mutex_);
    login_cv_.wait_for(lock, std::chrono::milliseconds(5000), [this]() {
        return active_bg_threads_ == 0;
    });
}

bool AuthCenterManager::supportsBrowserCookieLogin(const std::string& plugin_id) const {
    static const std::set<std::string> kSupported = {
        "plugin-zhihu",
        "plugin-linuxdo",
        "plugin-shuiyuan",
        "plugin-v2ex",
        "plugin-reddit",
        "plugin-x",
        "plugin-youtube",
        "plugin-bilibili",
        "plugin-zlib",
        "plugin-annas-archive",
        "plugin-sub-hunter",
        "plugin-blog",
        "plugin-feed-recipe",
        "plugin-music",
    };
    return kSupported.count(plugin_id) > 0;
}

bool AuthCenterManager::allowsCustomLoginUrl(const std::string& plugin_id) const {
    return plugin_id == "plugin-blog" || plugin_id == "plugin-feed-recipe";
}

std::string AuthCenterManager::defaultLoginUrlForPlugin(const std::string& plugin_id) const {
    auto& kernel = HostKernel::instance();
    if (plugin_id == "plugin-zhihu") return "https://www.zhihu.com/signin";
    if (plugin_id == "plugin-linuxdo") return "https://linux.do/login";
    if (plugin_id == "plugin-shuiyuan") return "https://shuiyuan.sjtu.edu.cn/login";
    if (plugin_id == "plugin-v2ex") return "https://www.v2ex.com/signin";
    if (plugin_id == "plugin-reddit") return "https://www.reddit.com/login";
    if (plugin_id == "plugin-x") return "https://x.com/i/flow/login";
    if (plugin_id == "plugin-youtube") {
        return "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F";
    }
    if (plugin_id == "plugin-bilibili") return "https://passport.bilibili.com/login";
    if (plugin_id == "plugin-music") return "https://music.apple.com/cn/browse";
    if (plugin_id == "plugin-sub-hunter") return "https://subhd.tv/";
    if (plugin_id == "plugin-zlib") {
        if (kernel.isInitialized()) {
            const auto cfg = kernel.config().loadPluginConfig("plugin-zlib");
            const std::string dom = cfg.is_object() ? trimAscii(cfg.value("domain", "")) : "";
            if (dom.rfind("http://", 0) == 0 || dom.rfind("https://", 0) == 0) return dom;
            if (!dom.empty()) return "https://" + dom;
        }
        return "https://z-lib.gd";
    }
    if (plugin_id == "plugin-annas-archive") {
        if (kernel.isInitialized()) {
            const auto cfg = kernel.config().loadPluginConfig("plugin-annas-archive");
            const std::string mir = cfg.is_object() ? trimAscii(cfg.value("mirror", "")) : "";
            if (mir.rfind("http://", 0) == 0 || mir.rfind("https://", 0) == 0) return mir;
            if (!mir.empty()) return "https://" + mir;
        }
        return "https://annas-archive.org";
    }
    if (plugin_id == "plugin-blog") return "https://substack.com/sign-in";
    if (plugin_id == "plugin-feed-recipe") return "https://news.ycombinator.com/login";
    return "";
}

PluginAuthReport AuthCenterManager::inspectPlugin(const std::string& plugin_id) const {
    PluginAuthReport r;
    r.plugin_id = plugin_id;
    r.supports_browser_cookie_login = supportsBrowserCookieLogin(plugin_id);
    r.allows_custom_login_url = allowsCustomLoginUrl(plugin_id);
    r.default_login_url = defaultLoginUrlForPlugin(plugin_id);
    r.secret_key_for_cookie = cookieSecretKeyForPlugin(plugin_id);

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return r;

    const auto inst = kernel.installer().getInstalled(plugin_id);
    const auto st = kernel.plugins().statusOf(plugin_id);
    r.is_installed = inst.has_value();
    r.is_loaded = st && st->loaded;

    const nlohmann::json cfg = kernel.config().loadPluginConfig(plugin_id);
    const fs::path chrome_cache_path =
        fs::path(kernel.dataRoot()) / "plugin-data" / "plugin-chrome-web" / "cookies_cache.json";
    const auto chrome_cookies = loadChromeCookiesCacheFile(chrome_cache_path.string());

    nlohmann::json live_probe;
    {
        std::lock_guard<std::mutex> lk(mutex_);
        auto pit = live_probe_cache_.find(plugin_id);
        if (pit != live_probe_cache_.end()) live_probe = pit->second;
    }

    auto collectMergedCookieSources = [&](const std::vector<std::string>& doms,
                                          const std::string& secret_key,
                                          const std::string& file_cfg_key,
                                          std::vector<std::string>& sources_present,
                                          CookieEvaluation& best_eval,
                                          CookieEvaluation& vault_eval) {
        const std::string vault_hdr =
            secret_key.empty() ? "" : trimAscii(kernel.secrets().getSecret(plugin_id, secret_key).value_or(""));
        if (!vault_hdr.empty()) {
            vault_eval = evaluatePluginCookieMap(plugin_id, parseCookieHeaderMap(vault_hdr));
            if (vault_eval.strict_authenticated) {
                best_eval = vault_eval;
                sources_present.push_back("SecretVault");
            }
        }

        const auto matched_chrome = filterCookiesForDomains(chrome_cookies, doms);
        if (!matched_chrome.empty()) {
            const auto ce = evaluatePluginCookieMap(
                plugin_id, parseCookieHeaderMap(buildCookieHeaderFromItems(matched_chrome)));
            if (ce.strict_authenticated) {
                if (!best_eval.strict_authenticated) best_eval = ce;
                sources_present.push_back("Chrome会话桥");
            } else if (!best_eval.strict_authenticated && ce.has_any_cookies && !vault_eval.has_any_cookies) {
                best_eval = ce;
            }
        }

        std::string fpath =
            (!file_cfg_key.empty() && cfg.is_object()) ? trimAscii(cfg.value(file_cfg_key, "")) : "";
        if (fpath.empty() && plugin_id == "plugin-youtube") {
            const fs::path def_yt =
                fs::path(kernel.dataRoot()) / "plugin-data" / "plugin-youtube" / "cookies.txt";
            std::error_code ec;
            if (fs::exists(def_yt, ec)) fpath = def_yt.string();
        }
        if (!fpath.empty()) {
            const auto fc = filterCookiesForDomains(loadNetscapeOrJsonCookiesFile(fpath), doms);
            if (!fc.empty()) {
                const auto fe = evaluatePluginCookieMap(
                    plugin_id, parseCookieHeaderMap(buildCookieHeaderFromItems(fc)));
                if (fe.strict_authenticated) {
                    if (!best_eval.strict_authenticated) best_eval = fe;
                    sources_present.push_back("cookies.txt");
                }
            }
        }

        if (!best_eval.strict_authenticated && vault_eval.has_any_cookies) {
            best_eval = vault_eval;
        }
    };

    // --- 1. Cookie-First Web Community & Media/Book Sites ---
    if (plugin_id == "plugin-zhihu" || plugin_id == "plugin-linuxdo" ||
        plugin_id == "plugin-shuiyuan" || plugin_id == "plugin-v2ex" ||
        plugin_id == "plugin-reddit" || plugin_id == "plugin-x") {
        r.method = AuthMethodKind::BrowserCookie;
        r.category_label = "Web 社区 / Cookie";

        if (plugin_id == "plugin-zhihu") {
            r.title = "知乎深度问答 (plugin-zhihu)";
            r.where_to_auth_hint = "授权入口: 本中心「🌐 浏览器登录自动取 Cookie」或 插件市场 → 知乎 → 参数设置";
        } else if (plugin_id == "plugin-linuxdo") {
            r.title = "Linux.do 社区 (plugin-linuxdo)";
            r.where_to_auth_hint = "授权入口: 本中心「🌐 浏览器登录自动取 Cookie」（支持过 Cloudflare 验证）";
        } else if (plugin_id == "plugin-shuiyuan") {
            r.title = "交大水源社区 jAccount (plugin-shuiyuan)";
            r.where_to_auth_hint = "授权入口: 本中心「🌐 浏览器登录自动取 Cookie」（支持 jAccount 扫码 / 2FA）";
        } else if (plugin_id == "plugin-v2ex") {
            r.title = "V2EX 技术社区 (plugin-v2ex)";
            r.where_to_auth_hint = "授权入口: 本中心一键浏览器登录取 A2 Cookie，或在插件参数填写 API v2 PAT";
        } else if (plugin_id == "plugin-reddit") {
            r.title = "Reddit 技术版块 (plugin-reddit)";
            r.where_to_auth_hint = "授权入口: 本中心「🌐 浏览器登录自动取 Cookie」（公开 Subreddit 免登可读）";
        } else if (plugin_id == "plugin-x") {
            r.title = "X / Twitter 时间线与长推 (plugin-x)";
            r.where_to_auth_hint = "授权入口: 本中心「🌐 浏览器登录自动取 Cookie」（自动提取 auth_token + ct0）";
        }

        std::vector<std::string> sources;
        CookieEvaluation best_ev, vault_ev;
        collectMergedCookieSources(domainsForPlugin(plugin_id, r.default_login_url),
                                   "cookie_header", "cookie_file_path",
                                   sources, best_ev, vault_ev);

        const bool has_v2ex_pat =
            (plugin_id == "plugin-v2ex") && kernel.secrets().hasSecret(plugin_id, "api_token");
        if (has_v2ex_pat) sources.push_back("SecretVault(PAT)");

        if (best_ev.strict_authenticated || has_v2ex_pat) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = joinList(sources, " + ");
            std::string keys = joinList(best_ev.verified_keys);
            if (has_v2ex_pat) keys = keys.empty() ? "api_token(PAT)" : ("api_token + " + keys);
            r.detail_summary = "已验证有效登录凭证: " + keys + " · 来源: " + r.credential_source;
        } else if (vault_ev.has_any_cookies || best_ev.has_any_cookies) {
            r.status = AuthStatusKind::GuestCookieOnly;
            r.credential_source = vault_ev.has_any_cookies ? "SecretVault(仅匿名Cookie)" : "Chrome缓存(仅匿名Cookie)";
            r.detail_summary =
                "检测到已保存 Cookie 仅含匿名访客标识，缺少真正登录态所需凭证 (" +
                joinList(best_ev.missing_keys) + ")。点击下方「🌐 浏览器登录自动取 Cookie」完成登录即可自动覆盖修复。";
        } else {
            r.status = (plugin_id == "plugin-x" || plugin_id == "plugin-shuiyuan")
                           ? AuthStatusKind::MissingRequired
                           : AuthStatusKind::OptionalGuest;
            r.detail_summary =
                (plugin_id == "plugin-x")
                    ? "尚未登录 X 账号（X 时间线与长推必须具有 auth_token + ct0 才可读取）。"
                    : (plugin_id == "plugin-shuiyuan")
                          ? "尚未登录交大水源 jAccount（需 _t 会话 Cookie 才可浏览校内帖子）。"
                          : "当前处于免登游客模式（公开热榜/板块可读；登录后解锁关注流、个性化推荐与完整评论）。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-youtube") {
        r.title = "YouTube 双语字幕工作台 (plugin-youtube)";
        r.category_label = "影视流媒体 / Cookie";
        r.method = AuthMethodKind::BrowserCookie;
        r.where_to_auth_hint = "授权入口: 本中心「🌐 浏览器登录自动取 Cookie」（自动同步至 SecretVault + yt-dlp cookies.txt）";

        std::vector<std::string> sources;
        CookieEvaluation best_ev, vault_ev;
        collectMergedCookieSources(domainsForPlugin(plugin_id, r.default_login_url),
                                   "cookie_header", "cookies_file",
                                   sources, best_ev, vault_ev);
        const bool has_api_key = kernel.secrets().hasSecret(plugin_id, "youtube_api_key");
        if (has_api_key) sources.push_back("Data API Key");

        if (best_ev.strict_authenticated) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = joinList(sources, " + ");
            r.detail_summary =
                "YouTube / Google 登录会话有效 (" + joinList(best_ev.verified_keys) +
                ") · 来源: " + r.credential_source +
                (has_api_key ? "（含 Data API v3 Key）" : "（yt-dlp 与直连字幕均可用）");
        } else if (vault_ev.has_any_cookies) {
            r.status = AuthStatusKind::GuestCookieOnly;
            r.detail_summary = "已保存 Cookie 缺少 Google 登录凭证 (SAPISID / SID)，仅处于访客状态。";
        } else {
            r.status = AuthStatusKind::OptionalGuest;
            r.detail_summary = "公开视频免登可播；若遇年龄限制 / 高码率风控或需拉取个人订阅，请点击浏览器登录自动获取 Cookie。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-bilibili") {
        r.title = "Bilibili 视频与番剧 (plugin-bilibili)";
        r.category_label = "影视流媒体 / 扫码或Cookie";
        r.method = AuthMethodKind::BrowserCookie;
        r.where_to_auth_hint = "授权入口: ① 左侧栏「B站」专属页扫码登录，或 ② 本中心「🌐 浏览器登录自动取 Cookie」";

        const bool has_sess = kernel.secrets().hasSecret(plugin_id, "web_sessdata");
        const bool has_jct = kernel.secrets().hasSecret(plugin_id, "web_bili_jct");
        const bool has_android = kernel.secrets().hasSecret(plugin_id, "android_access_token");
        const std::string uid = cfg.is_object() ? trimAscii(cfg.value("web_dede_userid", "")) : "";

        if ((has_sess && has_jct) || has_android) {
            r.status = AuthStatusKind::Authenticated;
            std::vector<std::string> parts;
            if (has_sess && has_jct) parts.push_back("Web SESSDATA + bili_jct");
            if (has_android) parts.push_back("Android AccessToken");
            r.credential_source = joinList(parts, " + ");
            r.detail_summary = "B 站账号已授权 (" + r.credential_source + ")" +
                               (uid.empty() ? "" : (" · UID: " + uid)) + " · 4K/高码率与关注动态已解锁";
        } else {
            r.status = AuthStatusKind::OptionalGuest;
            r.detail_summary = "当前未登录（仅试看/低清免登流）；可通过侧栏「B站」扫码或点击下方浏览器登录自动提取 SESSDATA。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-music") {
        r.title = "Apple Music 无损工作室 (plugin-music)";
        r.category_label = "音乐 / 原生 2FA + Token";
        r.method = AuthMethodKind::NativeOrServiceKey;
        r.where_to_auth_hint = "授权入口: 左侧栏「Apple Music」→ 右上角「Apple ID / 账号」（原生 FairPlay 2FA 登录），或浏览器获取 Web Token";

        const bool has_web_tok = kernel.secrets().hasSecret(plugin_id, "media_user_token");
        bool live_ok = false;
        if (live_probe.is_object() && live_probe.value("has_live_apple_music_auth", false)) {
            live_ok = true;
        } else {
            // Fast check whether Adam engine data dir exists and dev_token / config present
            const std::string edata = cfg.is_object() ? trimAscii(cfg.value("engine_data_dir", "")) : "";
            const fs::path dev_tok =
                fs::path(kernel.dataRoot()) / "plugin-data" / "plugin-music" / "dev_token.json";
            std::error_code ec;
            if (!edata.empty() && fs::exists(fs::path(edata) / "config.yaml", ec) && fs::exists(dev_tok, ec)) {
                // Confirm via lightweight live capability if already registered in-process
                if (kernel.capabilities().has("media.playlist.v1")) {
                    auto res = kernel.capabilities().call("media.playlist.v1", {{"action", "status"}});
                    if (res.ok && res.data.is_object() && res.data.value("has_live_apple_music_auth", false)) {
                        live_ok = true;
                    }
                }
            }
        }

        if (live_ok || has_web_tok) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = live_ok
                                      ? (has_web_tok ? "Adam FairPlay 引擎 + Web Token" : "Adam Go 原生会话")
                                      : "SecretVault (media_user_token)";
            r.detail_summary =
                "Apple Music 账号授权就绪 (" + r.credential_source + ") · 无损播放与歌单写回可用。";
        } else {
            r.status = AuthStatusKind::MissingRequired;
            r.detail_summary =
                "尚未完成 Apple Music 授权。请点「打开专属面板 (Apple Music)」→ 右上角「Apple ID / 账号」完成 2FA 登录，或浏览器登录提取 Web Token。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-zlib") {
        r.title = "Z-Library 数字图书馆 (plugin-zlib)";
        r.category_label = "书库 / Cookie 或账号";
        r.method = AuthMethodKind::BrowserCookie;
        r.where_to_auth_hint = "授权入口: ① 本中心「🌐 浏览器登录自动取 Cookie」（自动提取 remix_userid + remix_userkey），或 ② 侧栏「Z-Library」页邮箱登录";

        const std::string r_uid = cfg.is_object() ? trimAscii(cfg.value("remix_userid", "")) : "";
        const bool has_r_key = kernel.secrets().hasSecret(plugin_id, "remix_userkey");
        const std::string email = cfg.is_object() ? trimAscii(cfg.value("email", "")) : "";
        const bool has_pwd = kernel.secrets().hasSecret(plugin_id, "password");

        std::vector<std::string> sources;
        CookieEvaluation best_ev, vault_ev;
        collectMergedCookieSources(domainsForPlugin(plugin_id, r.default_login_url),
                                   "cookie_header", "", sources, best_ev, vault_ev);

        if ((!r_uid.empty() && has_r_key) || best_ev.strict_authenticated || (!email.empty() && has_pwd)) {
            r.status = AuthStatusKind::Authenticated;
            if (!r_uid.empty() && has_r_key) sources.push_back("remix_userid+userkey");
            if (!email.empty() && has_pwd) sources.push_back("邮箱密码");
            r.credential_source = joinList(sources, " + ");
            r.detail_summary = "Z-Library 账号凭据已就绪 (" + r.credential_source + ") · EAPI 搜书与下载可用。";
        } else if (vault_ev.has_any_cookies) {
            r.status = AuthStatusKind::GuestCookieOnly;
            r.detail_summary = "已存 Cookie 缺少 remix_userid + remix_userkey，尚未登录 Z-Library 账号。";
        } else {
            r.status = AuthStatusKind::MissingRequired;
            r.detail_summary = "尚未配置 Z-Library 登录凭证（需登录才可下载电子书）。点击下方浏览器登录即可自动提取 remix_userid / remix_userkey。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-annas-archive") {
        r.title = "Anna's Archive 元书库 (plugin-annas-archive)";
        r.category_label = "书库 / 会员Key + 过盾Cookie";
        r.method = AuthMethodKind::BrowserCookie;
        r.where_to_auth_hint = "授权入口: 本中心浏览器过盾取验证 Cookie，或在插件参数填写会员 fast_download_key";

        const bool has_fast = kernel.secrets().hasSecret(plugin_id, "fast_download_key");
        const bool has_ck = kernel.secrets().hasSecret(plugin_id, "cookie_header");
        if (has_fast || has_ck) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = has_fast && has_ck ? "Fast Download Key + 验证 Cookie"
                                                     : (has_fast ? "Fast Download Key" : "浏览器验证 Cookie");
            r.detail_summary = "已配置 " + r.credential_source + "。";
        } else {
            r.status = AuthStatusKind::OptionalGuest;
            r.detail_summary = "公开元搜索与外部慢速镜像免登可用；如遇人机验证或有会员极速下载密钥，可在此一键浏览器过盾或填写密钥。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-sub-hunter") {
        r.title = "SubHD / ASSRT 字幕猎手 (plugin-sub-hunter)";
        r.category_label = "字幕 / API Token + Cookie";
        r.method = AuthMethodKind::BrowserCookie;
        r.where_to_auth_hint = "授权入口: 本中心浏览器取 SubHD Cookie；射手网 (ASSRT) Token 在「⚙ 插件参数与密钥」填写";

        const bool has_assrt = kernel.secrets().hasSecret(plugin_id, "assrt_token");
        const bool has_subhd = kernel.secrets().hasSecret(plugin_id, "subhd_cookie");
        if (has_assrt || has_subhd) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = (has_assrt && has_subhd) ? "ASSRT Token + SubHD Cookie"
                                                           : (has_assrt ? "ASSRT API Token" : "SubHD 浏览器 Cookie");
            r.detail_summary = "已配置字幕鉴权: " + r.credential_source + "。";
        } else {
            r.status = AuthStatusKind::OptionalGuest;
            r.detail_summary = "SubHD 公开检索可用；建议配置 ASSRT API Token 或通过浏览器获取 SubHD 验证 Cookie 以提升下载成功率。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-blog") {
        r.title = "个人博客与会员专栏阅读器 (plugin-blog)";
        r.category_label = "博客 / 按域名浏览器登录";
        r.method = AuthMethodKind::BrowserCookie;
        r.where_to_auth_hint = "授权入口: 点下方「🌐 输入博客网址登录取 Cookie」（支持 Substack / Medium / 付费独立站）";

        const bool has_vault = kernel.secrets().hasSecret(plugin_id, "cookie_header");
        std::set<std::string> doms;
        for (const auto& c : chrome_cookies) doms.insert(guessRegistrableRootDomain(c.domain));
        r.status = (has_vault || !doms.empty()) ? AuthStatusKind::Authenticated : AuthStatusKind::OptionalGuest;
        r.credential_source = "Chrome 会话桥 (" + std::to_string(doms.size()) + " 个域名)" +
                              (has_vault ? " + SecretVault" : "");
        r.detail_summary = "公开博客免登即读；遇到需订阅登录的专栏，点下方按钮输入站点登录页网址即可自动捕获该域名 Cookie。";
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-feed-recipe") {
        r.title = "声明式源配方引擎 (plugin-feed-recipe)";
        r.category_label = "自定义源 / 按域名 Cookie";
        r.method = AuthMethodKind::BrowserCookie;
        r.where_to_auth_hint = "授权入口: 点下方「🌐 输入站点网址登录取 Cookie」自动同步至配方引擎与 Chrome 会话桥";

        const bool has_vault = kernel.secrets().hasSecret(plugin_id, "custom_cookie_header");
        r.status = has_vault ? AuthStatusKind::Authenticated : AuthStatusKind::OptionalGuest;
        r.credential_source = has_vault ? "SecretVault + Chrome 桥" : "Chrome 自动会话桥";
        r.detail_summary = "内置 Hacker News 等公开配方免登可用；私密站点可输入登录 URL 弹窗登录并自动提取 Cookie。";
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    // --- 2. Local / Self-Hosted Service Keys & AI Gateways ---
    if (plugin_id == "plugin-fnos-nas") {
        r.title = "飞牛 fnOS NAS 媒体与同步底座 (plugin-fnos-nas)";
        r.category_label = "私有云 / 挂载与 API Key";
        r.method = AuthMethodKind::NativeOrServiceKey;
        r.where_to_auth_hint = "授权入口: 插件市场 → 飞牛 fnOS NAS → 参数设置（挂载目录与 Radarr/Sonarr/同步密钥）";

        const std::string mount_root = cfg.is_object() ? trimAscii(cfg.value("mount_root", "")) : "";
        std::error_code ec;
        const bool mount_ok = !mount_root.empty() && fs::exists(mount_root, ec);
        const bool has_radarr = kernel.secrets().hasSecret(plugin_id, "radarr_api_key");
        const bool has_sonarr = kernel.secrets().hasSecret(plugin_id, "sonarr_api_key");
        const bool has_sync_tok = kernel.secrets().hasSecret(plugin_id, "companion_sync_token");

        if (mount_ok) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = "本地挂载 (" + mount_root + ")";
            r.detail_summary = "NAS 挂载目录在线可读" +
                               std::string(has_radarr && has_sonarr ? " · Radarr/Sonarr API Key 已配" : "") +
                               std::string(has_sync_tok ? " · 阅读同步令牌已配" : "");
        } else if (!mount_root.empty() || has_radarr || has_sonarr) {
            r.status = AuthStatusKind::MissingRequired;
            r.detail_summary = "已填写配置但挂载路径当前不可访问: " + mount_root;
        } else {
            r.status = AuthStatusKind::MissingRequired;
            r.detail_summary = "尚未配置 NAS 挂载根目录 (mount_root) 与服务密钥。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-arr-imdb") {
        r.title = "IMDb 发现与 Radarr/Sonarr/qB 选片 (plugin-arr-imdb)";
        r.category_label = "私有云 / API Key + 密码";
        r.method = AuthMethodKind::NativeOrServiceKey;
        r.where_to_auth_hint = "授权入口: 插件市场 → IMDb 发现与 Radarr/Sonarr → 参数设置";

        const bool has_radarr = kernel.secrets().hasSecret(plugin_id, "radarr_api_key");
        const bool has_sonarr = kernel.secrets().hasSecret(plugin_id, "sonarr_api_key");
        const bool has_qb = kernel.secrets().hasSecret(plugin_id, "qbittorrent_password");
        if (has_radarr || has_sonarr) {
            r.status = AuthStatusKind::Authenticated;
            std::vector<std::string> keys;
            if (has_radarr) keys.push_back("Radarr Key");
            if (has_sonarr) keys.push_back("Sonarr Key");
            if (has_qb) keys.push_back("qBittorrent 密码");
            r.credential_source = joinList(keys, " + ");
            std::string reach;
            if (live_probe.is_object() && live_probe.contains("radarr") && live_probe["radarr"].is_object()) {
                const bool r_conn = live_probe["radarr"].value("connected", false);
                const bool s_conn = live_probe.contains("sonarr") && live_probe["sonarr"].is_object() &&
                                    live_probe["sonarr"].value("connected", false);
                reach = (r_conn || s_conn) ? " · 实测局域网服务在线" : " · 密钥已配（局域网服务当前离线或不可达）";
            }
            r.detail_summary = "已加密保存服务凭据: " + r.credential_source + reach;
        } else {
            r.status = AuthStatusKind::OptionalGuest;
            r.detail_summary = "IMDb 榜单免登可浏览；如需一键推送下载需在参数设置填写 Radarr / Sonarr API Key。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-magpie-agent") {
        r.title = "Magpie AI 路由与模型网关 (plugin-magpie-agent)";
        r.category_label = "AI 网关 / 本地 CLI & 路由";
        r.method = AuthMethodKind::NativeOrServiceKey;
        r.where_to_auth_hint = "授权入口: 左侧栏「AI 对话」/ F8「Magpie 路由与额度」面板，或终端 `magpie` 命令";

        const std::string mbin = cfg.is_object() ? trimAscii(cfg.value("magpie_bin", "magpie")) : "magpie";
        const bool bin_ok = !searchPathBinary(mbin.empty() ? "magpie" : mbin).empty();
        const bool cap_ok = kernel.capabilities().has("llm.router.v1");
        const bool has_gw_key = kernel.secrets().hasSecret(plugin_id, "gateway_key");

        if (cap_ok || bin_ok) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = std::string("本地 Magpie 网关") + (has_gw_key ? " + Gateway Key" : "");
            r.detail_summary = "AI 路由能力已就绪 (`llm.router.v1` / `llm.chat.v1`)，供应商订阅与 API 密钥由本机 magpie 托管。";
        } else {
            r.status = AuthStatusKind::MissingRequired;
            r.detail_summary = "未检测到本机 magpie 可执行程序或路由能力。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-ai-curator") {
        r.title = "AI 策展双仓同步中心 (plugin-ai-curator)";
        r.category_label = "GitHub 仓库 / SSH·Git 鉴权";
        r.method = AuthMethodKind::NativeOrServiceKey;
        r.where_to_auth_hint = "授权入口: 复用本机 Git / SSH Key 同步 ZJFILOVE/obsidian-vault 与 music-daily；路径在插件参数设置";

        std::string vdir = cfg.is_object() ? trimAscii(cfg.value("obsidian_vault_dir", "")) : "";
        std::string mdir = cfg.is_object() ? trimAscii(cfg.value("music_daily_dir", "")) : "";
        if (vdir.empty()) vdir = (fs::path(kernel.dataRoot()) / "curation" / "repos" / "obsidian-vault").string();
        if (mdir.empty()) mdir = (fs::path(kernel.dataRoot()) / "curation" / "repos" / "music-daily").string();
        std::error_code ec;
        const bool v_ok = fs::exists(fs::path(vdir) / ".git", ec);
        const bool m_ok = fs::exists(fs::path(mdir) / ".git", ec);

        if (v_ok && m_ok) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = "本地 Git 双仓已克隆就绪";
            r.detail_summary = "obsidian-vault 与 music-daily 均已通过本机 Git 凭证同步就绪。";
        } else {
            r.status = AuthStatusKind::OptionalGuest;
            r.detail_summary = "本地双仓尚未完整初始化（首次运行推荐日历同步或 `gh auth status` / SSH Key 即可自动拉取）。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    if (plugin_id == "plugin-chrome-web") {
        r.title = "Chrome 网页阅读与会话桥底座 (plugin-chrome-web)";
        r.category_label = "浏览器会话底座";
        r.method = AuthMethodKind::NativeOrServiceKey;
        r.where_to_auth_hint = "自动管理: 为所有社区与阅读插件提供浏览器登录、CDP Cookie 提取与同源请求代理";

        const std::string cbin = findChromeExecutable();
        const bool has_tok = kernel.secrets().hasSecret(plugin_id, "chrome_bridge_token") ||
                             kernel.secrets().hasSecret(plugin_id, "bridge_token");
        std::set<std::string> doms;
        for (const auto& c : chrome_cookies) doms.insert(guessRegistrableRootDomain(c.domain));

        if (!cbin.empty()) {
            r.status = AuthStatusKind::Authenticated;
            r.credential_source = cbin;
            r.detail_summary =
                "Chrome 可执行文件就绪 (" + cbin + ") · 本地会话桥已缓存 " +
                std::to_string(chrome_cookies.size()) + " 枚 Cookie（覆盖 " +
                std::to_string(doms.size()) + " 个根域名）" +
                (has_tok ? " · 配对密钥已加密" : "");
        } else {
            r.status = AuthStatusKind::MissingRequired;
            r.detail_summary = "未探测到本机 google-chrome / chromium 可执行文件，请在插件参数指定 chrome_bin。";
        }
        styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
        return r;
    }

    // --- 3. Local / Open-Access No-Auth Plugins ---
    r.method = AuthMethodKind::NoAuth;
    r.status = AuthStatusKind::NoAuthNeeded;
    r.category_label = "本地 / 公开免登录";
    r.where_to_auth_hint = "无需登录鉴权，安装启用后即开即用";
    if (plugin_id == "plugin-asr-funasr") {
        r.title = "FunASR 本地 GPU 语音转写 (plugin-asr-funasr)";
        r.detail_summary = "纯本地离线 Vulkan/CPU 语音识别引擎，无需任何账号或联网授权。";
    } else if (plugin_id == "plugin-ebook") {
        r.title = "eBook 双语伴读与生词卡 (plugin-ebook)";
        r.detail_summary = "本地 EPUB/MOBI/AZW3 阅读器，翻译与同步自动复用 AI 路由与飞牛 NAS 能力。";
    } else if (plugin_id == "plugin-pdf-babeldoc") {
        r.title = "PDF & BabelDOC 双语排版引擎 (plugin-pdf-babeldoc)";
        r.detail_summary = "本地 PDF 解析与双栏排版翻译引擎，自动复用 AI 路由能力，无需单独登录。";
    } else if (plugin_id == "plugin-journals") {
        r.title = "arXiv / Nature / PubMed 学术期刊 (plugin-journals)";
        r.detail_summary = "直连 arXiv、bioRxiv、PubMed 与 Nature 公开学术数据源，无需账号登录。";
    } else if (plugin_id == "plugin-sponsorblock") {
        r.title = "SponsorBlock 空降助手 (plugin-sponsorblock)";
        r.detail_summary = "基于 SHA-256 隐私前缀查询公开空降片段库 (bsbsb.top / sponsor.ajay.app)，无需登录。";
    } else {
        r.title = inst ? inst->manifest.name : plugin_id;
        r.detail_summary = "此插件无需额外账号登录鉴权。";
    }
    styleStatusTokens(r.status, r.status_badge_text, r.status_bg_token, r.status_fg_token);
    return r;
}

std::vector<PluginAuthReport> AuthCenterManager::inspectAll() const {
    static const char* kOrderedPluginIds[] = {
        // Cookie / Web Auth Sites
        "plugin-zhihu",
        "plugin-x",
        "plugin-reddit",
        "plugin-v2ex",
        "plugin-linuxdo",
        "plugin-shuiyuan",
        "plugin-youtube",
        "plugin-bilibili",
        "plugin-zlib",
        "plugin-annas-archive",
        "plugin-sub-hunter",
        "plugin-blog",
        "plugin-feed-recipe",
        // Native / Service Key / Infrastructure
        "plugin-music",
        "plugin-fnos-nas",
        "plugin-arr-imdb",
        "plugin-magpie-agent",
        "plugin-ai-curator",
        "plugin-chrome-web",
        // No-Auth Local/Open Plugins
        "plugin-journals",
        "plugin-pdf-babeldoc",
        "plugin-ebook",
        "plugin-asr-funasr",
        "plugin-sponsorblock",
    };

    std::vector<PluginAuthReport> out;
    out.reserve(sizeof(kOrderedPluginIds) / sizeof(kOrderedPluginIds[0]));
    for (const char* pid : kOrderedPluginIds) {
        out.push_back(inspectPlugin(pid));
    }
    return out;
}

AuthSummaryCounts AuthCenterManager::summarizeCounts(const std::vector<PluginAuthReport>& reports) const {
    AuthSummaryCounts c;
    for (const auto& r : reports) {
        if (r.method == AuthMethodKind::NoAuth) {
            ++c.no_auth_count;
            continue;
        }
        ++c.total_auth_plugins;
        switch (r.status) {
            case AuthStatusKind::Authenticated:
                ++c.authenticated_count;
                break;
            case AuthStatusKind::GuestCookieOnly:
                ++c.guest_cookie_warning_count;
                break;
            case AuthStatusKind::OptionalGuest:
                ++c.optional_guest_count;
                break;
            case AuthStatusKind::MissingRequired:
                ++c.missing_required_count;
                break;
            case AuthStatusKind::NoAuthNeeded:
                ++c.no_auth_count;
                break;
        }
    }
    return c;
}

bool AuthCenterManager::diagnoseCookieSecretField(const std::string& plugin_id,
                                                  const std::string& field_key,
                                                  std::string& out_summary,
                                                  std::string& out_color_token) const {
    const bool is_cookie_field =
        (field_key == "cookie_header" || field_key == "subhd_cookie" ||
         field_key == "custom_cookie_header" || field_key == "media_user_token");
    if (!is_cookie_field) return false;

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return false;

    const std::string vault_val = trimAscii(kernel.secrets().getSecret(plugin_id, field_key).value_or(""));
    const PluginAuthReport rep = inspectPlugin(plugin_id);

    if (!vault_val.empty()) {
        if (field_key == "media_user_token") {
            out_summary = "当前状态: ✓ 已加密保存 Apple Music Web Media-User-Token";
            out_color_token = "status/ok_fg";
            return true;
        }
        const auto ev = evaluatePluginCookieMap(plugin_id, parseCookieHeaderMap(vault_val));
        if (ev.strict_authenticated || !isStrictRulePlugin(plugin_id)) {
            const std::string keys = ev.verified_keys.empty() ? "已保存会话" : joinList(ev.verified_keys);
            out_summary = "当前状态: ✓ 已加密保存有效登录 Cookie (" + keys + ")";
            out_color_token = "status/ok_fg";
            return true;
        }
        out_summary = "当前状态: ⚠ 已存匿名访客 Cookie，未真正登录 (缺少 " +
                      joinList(ev.missing_keys) + ") · 请点上方「🌐 浏览器登录自动获取」";
        out_color_token = "status/warn_fg";
        return true;
    }

    if (rep.status == AuthStatusKind::Authenticated) {
        out_summary = "当前状态: ✓ Vault 未单独存副本，但已通过 " + rep.credential_source + " 生效";
        out_color_token = "status/ok_fg";
        return true;
    }

    out_summary = "当前状态: 未设置密钥（推荐直接点上方「🌐 浏览器登录自动获取 Cookie」）";
    out_color_token = "font/grey";
    return true;
}

bool AuthCenterManager::isBrowserLoginRunning(std::string* out_plugin_id,
                                              std::string* out_status_text) const {
    std::lock_guard<std::mutex> lock(login_mutex_);
    if (out_plugin_id) *out_plugin_id = login_plugin_id_;
    if (out_status_text) *out_status_text = login_status_text_;
    return login_running_;
}

void AuthCenterManager::finishActiveLoginCaptureNow() {
    {
        std::lock_guard<std::mutex> lock(login_mutex_);
        if (!login_running_) return;
        login_force_capture_now_ = true;
    }
    login_cv_.notify_all();
}

void AuthCenterManager::cancelActiveBrowserLogin() {
    int pid_to_kill = -1;
    {
        std::lock_guard<std::mutex> lock(login_mutex_);
        if (!login_running_) return;
        login_cancel_requested_ = true;
        ++login_seq_;
        pid_to_kill = login_chrome_pid_;
        login_chrome_pid_ = -1;
        login_running_ = false;
        login_status_text_.clear();
    }
    login_cv_.notify_all();
    if (pid_to_kill > 1) {
        std::thread([pid_to_kill]() { terminatePidGroupGracefully(pid_to_kill); }).detach();
    }
    notifyChanges();
}

void AuthCenterManager::startBrowserCookieLogin(
    const std::string& plugin_id,
    const std::string& custom_url_override,
    std::function<void(bool ok, const std::string& message)> on_finished) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    const std::string target_url =
        trimAscii(custom_url_override.empty() ? defaultLoginUrlForPlugin(plugin_id) : custom_url_override);
    if (target_url.rfind("http://", 0) != 0 && target_url.rfind("https://", 0) != 0) {
        if (on_finished) on_finished(false, "请输入以 https:// 或 http:// 开头的有效登录网址");
        return;
    }

    const std::string chrome_bin = findChromeExecutable();
    if (chrome_bin.empty()) {
        if (on_finished) on_finished(false, "未找到 google-chrome 或 chromium 可执行文件");
        return;
    }

    // Snapshot pre-login state so if the user is ALREADY authenticated and clicks "Re-login",
    // we wait for a session change or manual "Finish" click rather than closing immediately.
    const PluginAuthReport before_rep = inspectPlugin(plugin_id);
    const bool was_authed_before = (before_rep.status == AuthStatusKind::Authenticated);
    std::string initial_vault_fp;
    {
        const std::string skey = cookieSecretKeyForPlugin(plugin_id);
        const std::string vhdr = kernel.secrets().getSecret(plugin_id, skey).value_or("");
        if (!vhdr.empty()) {
            initial_vault_fp = evaluatePluginCookieMap(plugin_id, parseCookieHeaderMap(vhdr)).auth_fingerprint;
        }
    }

    // Resolve optional proxy
    std::string proxy_server;
    {
        const auto pcfg = kernel.config().loadPluginConfig(plugin_id);
        if (pcfg.is_object()) {
            proxy_server = trimAscii(pcfg.value("proxy", pcfg.value("proxy_url", "")));
        }
        if (proxy_server.empty()) {
            const auto cwcfg = kernel.config().loadPluginConfig("plugin-chrome-web");
            if (cwcfg.is_object()) {
                proxy_server = trimAscii(cwcfg.value("proxy_server", ""));
            }
        }
    }

    int prev_pid = -1;
    uint64_t my_seq = 0;
    {
        std::lock_guard<std::mutex> lock(login_mutex_);
        if (stopping_) return;
        prev_pid = login_chrome_pid_;
        login_chrome_pid_ = -1;
        my_seq = ++login_seq_;
        login_running_ = true;
        login_force_capture_now_ = false;
        login_cancel_requested_ = false;
        login_plugin_id_ = plugin_id;
        login_status_text_ = "正在启动独立 Chrome 登录窗口 (" + target_url + ")…";
        ++active_bg_threads_;
    }
    login_cv_.notify_all();
    notifyChanges();

    const std::string data_root = kernel.dataRoot();
    const std::vector<std::string> target_domains = domainsForPlugin(plugin_id, target_url);
    const bool strict_rule = isStrictRulePlugin(plugin_id);

    std::thread([this, my_seq, prev_pid, plugin_id, target_url, chrome_bin, proxy_server,
                 data_root, target_domains, strict_rule, was_authed_before, initial_vault_fp,
                 on_finished]() {
        auto finishWith = [&](bool ok, const std::string& msg, int pid_cleanup) {
            if (pid_cleanup > 1) {
                terminatePidGroupGracefully(pid_cleanup);
            }
            bool is_current = false;
            {
                std::lock_guard<std::mutex> lk(login_mutex_);
                if (login_seq_ == my_seq) {
                    is_current = true;
                    login_running_ = false;
                    login_chrome_pid_ = -1;
                    login_status_text_.clear();
                }
                --active_bg_threads_;
            }
            login_cv_.notify_all();
            if (!is_current) return;

            brls::sync([this, plugin_id, ok, msg, on_finished]() {
                if (ok && HostKernel::instance().isInitialized()) {
                    std::string err;
                    if (HostKernel::instance().plugins().isLoaded(plugin_id)) {
                        HostKernel::instance().plugins().reloadPlugin(plugin_id, err);
                    }
                }
                notifyChanges();
                if (!msg.empty()) {
                    brls::Application::notify(msg);
                }
                if (on_finished) {
                    on_finished(ok, msg);
                }
            });
        };

        if (prev_pid > 1) {
            terminatePidGroupGracefully(prev_pid);
        }

        const fs::path profile_dir = fs::path(data_root) / "auth-browser-profile";
        std::error_code ec;
        fs::create_directories(profile_dir, ec);
        ::chmod(profile_dir.c_str(), 0700);
        reclaimAuthProfileLock(profile_dir.string());

        const fs::path port_file = profile_dir / "DevToolsActivePort";
        fs::remove(port_file, ec);

        std::vector<std::string> argv_str = {
            chrome_bin,
            "--app=" + target_url,
            "--user-data-dir=" + profile_dir.string(),
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--disable-blink-features=AutomationControlled",
            "--class=omnidesk-auth-login",
            "--window-size=1100,800",
            "--new-window",
        };
        if (!proxy_server.empty()) {
            argv_str.push_back("--proxy-server=" + proxy_server);
        }

        std::vector<char*> c_argv;
        c_argv.reserve(argv_str.size() + 1);
        for (const auto& s : argv_str) c_argv.push_back(const_cast<char*>(s.c_str()));
        c_argv.push_back(nullptr);

        const pid_t parent_pid = ::getpid();
        pid_t pid = ::fork();
        if (pid < 0) {
            finishWith(false, "启动浏览器进程失败 (fork error)", -1);
            return;
        }
        if (pid == 0) {
            ::setsid();
#ifdef __linux__
            ::prctl(PR_SET_PDEATHSIG, SIGTERM);
            if (::getppid() != parent_pid) ::_exit(0);
#endif
            int devnull = ::open("/dev/null", O_RDWR);
            if (devnull >= 0) {
                ::dup2(devnull, STDIN_FILENO);
                ::dup2(devnull, STDOUT_FILENO);
                ::dup2(devnull, STDERR_FILENO);
                if (devnull > 2) ::close(devnull);
            }
            for (int fd = 3; fd < 1024; ++fd) ::close(fd);
            ::execvp(chrome_bin.c_str(), c_argv.data());
            ::_exit(127);
        }

        {
            std::lock_guard<std::mutex> lk(login_mutex_);
            if (login_seq_ != my_seq || login_cancel_requested_ || stopping_) {
                finishWith(false, "", static_cast<int>(pid));
                return;
            }
            login_chrome_pid_ = static_cast<int>(pid);
            login_status_text_ =
                "正在连接浏览器安全会话桥 (CDP 127.0.0.1)… 请在弹出的窗口完成扫码 / 密码 / 2FA 登录";
        }
        brls::sync([this]() { notifyChanges(); });

        // Wait up to 8 seconds for DevToolsActivePort
        int cdp_port = 0;
        std::string cdp_ws_path;
        for (int attempt = 0; attempt < 80; ++attempt) {
            {
                std::lock_guard<std::mutex> lk(login_mutex_);
                if (login_seq_ != my_seq || login_cancel_requested_ || stopping_) {
                    finishWith(false, "", static_cast<int>(pid));
                    return;
                }
            }
            int wstatus = 0;
            if (::waitpid(pid, &wstatus, WNOHANG) == pid) {
                finishWith(false, "浏览器窗口在初始化完成前已退出", -1);
                return;
            }
            std::ifstream pifs(port_file);
            if (pifs.is_open()) {
                std::string l1, l2;
                if (std::getline(pifs, l1) && std::getline(pifs, l2)) {
                    try {
                        cdp_port = std::stoi(trimAscii(l1));
                        cdp_ws_path = trimAscii(l2);
                    } catch (...) {
                        cdp_port = 0;
                    }
                    if (cdp_port > 0 && !cdp_ws_path.empty()) break;
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

        if (cdp_port <= 0 || cdp_ws_path.empty()) {
            finishWith(false, "未能连接 Chrome DevTools 会话端口，请检查 Chrome 版本或配置", static_cast<int>(pid));
            return;
        }

        LocalCdpSocket cdp;
        if (!cdp.connectLoopback(cdp_port, cdp_ws_path, 3000)) {
            finishWith(false, "连接本地 Chrome CDP WebSocket 失败", static_cast<int>(pid));
            return;
        }

        seedExistingChromeCookiesIntoCdpOnce(cdp, profile_dir.string(), data_root);

        {
            std::lock_guard<std::mutex> lk(login_mutex_);
            if (login_seq_ == my_seq) {
                login_status_text_ = strict_rule
                    ? "浏览器已打开：请完成登录（扫码 / 密码 / 2FA / 人机验证），检测到有效登录态后将自动提取保存并关闭窗口…"
                    : "浏览器已打开：请完成登录或人机验证，完成后关闭浏览器窗口或点「完成并提取」即可自动写入 Cookie…";
            }
        }
        brls::sync([this]() { notifyChanges(); });

        std::vector<CookieItem> latest_domain_cookies;
        std::string baseline_browser_fp;
        bool baseline_recorded = false;
        const auto start_tp = std::chrono::steady_clock::now();
        const auto max_deadline = start_tp + std::chrono::minutes(5);

        while (std::chrono::steady_clock::now() < max_deadline) {
            bool force_now = false;
            {
                std::unique_lock<std::mutex> lk(login_mutex_);
                login_cv_.wait_for(lk, std::chrono::milliseconds(1050), [&]() {
                    return login_seq_ != my_seq || login_cancel_requested_ ||
                           login_force_capture_now_ || stopping_;
                });
                if (login_seq_ != my_seq || login_cancel_requested_ || stopping_) {
                    finishWith(false, "", static_cast<int>(pid));
                    return;
                }
                force_now = login_force_capture_now_;
                login_force_capture_now_ = false;
            }

            int wstatus = 0;
            const bool chrome_exited = (::waitpid(pid, &wstatus, WNOHANG) == pid);

            if (!chrome_exited) {
                if (!cdp.isConnected()) {
                    cdp.connectLoopback(cdp_port, cdp_ws_path, 1200);
                }
                if (cdp.isConnected()) {
                    auto all_ck = pullCookiesOverCdp(cdp);
                    if (!all_ck.empty()) {
                        latest_domain_cookies = filterCookiesForDomains(all_ck, target_domains);
                    }
                }
            }

            const std::string cur_hdr = buildCookieHeaderFromItems(latest_domain_cookies);
            const CookieEvaluation ev =
                evaluatePluginCookieMap(plugin_id, parseCookieHeaderMap(cur_hdr));

            if (!baseline_recorded && !chrome_exited) {
                baseline_browser_fp = ev.auth_fingerprint;
                baseline_recorded = true;
            }

            const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - start_tp).count();

            // Automatic completion check for strict-rule sites
            if (!chrome_exited && strict_rule && ev.strict_authenticated) {
                const bool session_is_new_or_needed =
                    !was_authed_before ||
                    (ev.auth_fingerprint != initial_vault_fp && ev.auth_fingerprint != baseline_browser_fp) ||
                    force_now;
                if (session_is_new_or_needed && (elapsed_ms >= 2000 || force_now)) {
                    // Brief 800ms grace wait so post-login redirect cookies (_xsrf, d_c0, ct0, cf_clearance) settle
                    std::this_thread::sleep_for(std::chrono::milliseconds(800));
                    if (cdp.isConnected()) {
                        auto final_ck = pullCookiesOverCdp(cdp);
                        if (!final_ck.empty()) {
                            latest_domain_cookies = filterCookiesForDomains(final_ck, target_domains);
                        }
                    }
                    persistCapturedCookiesToHost(plugin_id, target_url, latest_domain_cookies);
                    finishWith(true,
                               "✓ 登录成功！已自动捕获并加密保存 Cookie (" +
                                   joinList(ev.verified_keys) + ")",
                               static_cast<int>(pid));
                    return;
                }
            }

            // Manual "Finish & Save Now" button clicked
            if (force_now) {
                if (strict_rule && !ev.strict_authenticated) {
                    std::lock_guard<std::mutex> lk(login_mutex_);
                    if (login_seq_ == my_seq) {
                        login_status_text_ =
                            "尚未检测到完整登录凭证（缺少 " + joinList(ev.missing_keys) +
                            "），请先在弹出的 Chrome 窗口中完成登录…";
                    }
                    brls::sync([this]() {
                        notifyChanges();
                        brls::Application::notify("尚未检测到登录态 Cookie，请先在浏览器窗口完成登录");
                    });
                    continue;
                }
                if (latest_domain_cookies.empty()) {
                    brls::sync([]() {
                        brls::Application::notify("当前目标域名暂无可提取的 Cookie，请等待页面加载完成");
                    });
                    continue;
                }
                persistCapturedCookiesToHost(plugin_id, target_url, latest_domain_cookies);
                finishWith(true,
                           "✓ 已提取并保存浏览器 Cookie（共 " +
                               std::to_string(latest_domain_cookies.size()) + " 项）",
                           static_cast<int>(pid));
                return;
            }

            // User closed the Chrome window manually
            if (chrome_exited) {
                if (strict_rule) {
                    if (ev.strict_authenticated) {
                        persistCapturedCookiesToHost(plugin_id, target_url, latest_domain_cookies);
                        finishWith(true,
                                   "✓ 已从浏览器保存有效登录 Cookie (" + joinList(ev.verified_keys) + ")",
                                   -1);
                    } else {
                        finishWith(false,
                                   "浏览器已关闭：未检测到登录凭证（缺少 " + joinList(ev.missing_keys) +
                                       "），已忽略匿名访客 Cookie",
                                   -1);
                    }
                } else {
                    if (!latest_domain_cookies.empty()) {
                        persistCapturedCookiesToHost(plugin_id, target_url, latest_domain_cookies);
                        finishWith(true,
                                   "✓ 已保存站点浏览器 Cookie（共 " +
                                       std::to_string(latest_domain_cookies.size()) + " 项）",
                                   -1);
                    } else {
                        finishWith(false, "浏览器已关闭，未捕获到目标站点 Cookie", -1);
                    }
                }
                return;
            }

            // Update live counter text for flexible sites
            if (!strict_rule && !latest_domain_cookies.empty()) {
                bool changed = false;
                const std::string next_txt =
                    "浏览器运行中（已实时捕获目标站点 " + std::to_string(latest_domain_cookies.size()) +
                    " 枚 Cookie）— 完成登录/验证后直接关闭窗口或点「完成并提取」即可保存";
                {
                    std::lock_guard<std::mutex> lk(login_mutex_);
                    if (login_seq_ == my_seq && login_status_text_ != next_txt) {
                        login_status_text_ = next_txt;
                        changed = true;
                    }
                }
                if (changed) {
                    brls::sync([this]() { notifyChanges(); });
                }
            }
        }

        finishWith(false, "浏览器登录等待已超时（5 分钟），请重试", static_cast<int>(pid));
    }).detach();
}

bool AuthCenterManager::syncCookiesFromExistingStores(const std::string& plugin_id,
                                                      std::string& out_message) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) {
        out_message = "宿主未初始化";
        return false;
    }
    const std::string url = defaultLoginUrlForPlugin(plugin_id);
    const auto doms = domainsForPlugin(plugin_id, url);
    if (doms.empty()) {
        out_message = "该插件无固定站点域名，请使用「🌐 浏览器登录自动取 Cookie」";
        return false;
    }

    // Pull live cookies if embedded Chrome is running
    if (kernel.capabilities().has("web.cookies.v1")) {
        kernel.capabilities().call("web.cookies.v1", {
            {"action", "get_cookies"},
            {"url", url},
            {"domain", doms.front()},
            {"include_subdomains", true},
        });
    }

    const fs::path chrome_cache_path =
        fs::path(kernel.dataRoot()) / "plugin-data" / "plugin-chrome-web" / "cookies_cache.json";
    auto matched = filterCookiesForDomains(loadChromeCookiesCacheFile(chrome_cache_path.string()), doms);

    if (matched.empty() && plugin_id == "plugin-youtube") {
        const fs::path yt_file =
            fs::path(kernel.dataRoot()) / "plugin-data" / "plugin-youtube" / "cookies.txt";
        matched = filterCookiesForDomains(loadNetscapeOrJsonCookiesFile(yt_file.string()), doms);
    }

    if (matched.empty()) {
        out_message = "现有 Chrome 会话桥中暂无该站点 Cookie，请点击「🌐 浏览器登录自动取 Cookie」";
        return false;
    }

    const auto ev = evaluatePluginCookieMap(
        plugin_id, parseCookieHeaderMap(buildCookieHeaderFromItems(matched)));
    if (isStrictRulePlugin(plugin_id) && !ev.strict_authenticated) {
        out_message = "现有会话仅含匿名访客 Cookie（缺少 " + joinList(ev.missing_keys) +
                      "），请点击「🌐 浏览器登录自动取 Cookie」完成登录";
        return false;
    }

    persistCapturedCookiesToHost(plugin_id, url, matched);
    std::string err;
    if (kernel.plugins().isLoaded(plugin_id)) {
        kernel.plugins().reloadPlugin(plugin_id, err);
    }
    notifyChanges();
    out_message = "✓ 已从现有会话桥同步并保存有效 Cookie (" +
                  (ev.verified_keys.empty() ? std::to_string(matched.size()) + " 项"
                                            : joinList(ev.verified_keys)) +
                  ")";
    return true;
}

bool AuthCenterManager::clearPluginCookieAuth(const std::string& plugin_id,
                                              std::string& out_message) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return false;

    if (plugin_id == "plugin-bilibili") {
        kernel.secrets().deleteSecret(plugin_id, "web_sessdata");
        kernel.secrets().deleteSecret(plugin_id, "web_bili_jct");
        kernel.secrets().deleteSecret(plugin_id, "web_buvid3");
        kernel.secrets().deleteSecret(plugin_id, "android_access_token");
    } else if (plugin_id == "plugin-zlib") {
        kernel.secrets().deleteSecret(plugin_id, "cookie_header");
        kernel.secrets().deleteSecret(plugin_id, "remix_userkey");
    } else {
        const std::string skey = cookieSecretKeyForPlugin(plugin_id);
        if (!skey.empty()) {
            kernel.secrets().deleteSecret(plugin_id, skey);
        }
    }

    std::string err;
    if (kernel.plugins().isLoaded(plugin_id)) {
        kernel.plugins().reloadPlugin(plugin_id, err);
    }
    notifyChanges();
    out_message = "已清除 SecretVault 中的保存凭据: " + plugin_id;
    return true;
}

void AuthCenterManager::triggerAsyncLiveVerification(std::function<void()> on_updated) {
    {
        std::lock_guard<std::mutex> lk(mutex_);
        if (probe_running_) return;
        probe_running_ = true;
    }
    {
        std::lock_guard<std::mutex> lk(login_mutex_);
        if (stopping_) {
            std::lock_guard<std::mutex> lk2(mutex_);
            probe_running_ = false;
            return;
        }
        ++active_bg_threads_;
    }

    std::thread([this, on_updated]() {
        std::unordered_map<std::string, nlohmann::json> results;
        auto& kernel = HostKernel::instance();
        if (kernel.isInitialized()) {
            auto probe = [&](const std::string& pid, const std::string& cap, const nlohmann::json& req) {
                bool stop_now = false;
                {
                    std::lock_guard<std::mutex> lk(login_mutex_);
                    stop_now = stopping_;
                }
                if (stop_now) return;
                if (!kernel.capabilities().has(cap)) return;
                auto res = kernel.capabilities().call(cap, req);
                if (res.ok && res.data.is_object()) {
                    results[pid] = res.data;
                }
            };
            probe("plugin-music", "media.playlist.v1", {{"action", "status"}});
            probe("plugin-fnos-nas", "nas.connection.v1", {{"action", "status"}});
            probe("plugin-arr-imdb", "arr.manage.v1", {{"action", "status"}});
        }

        {
            std::lock_guard<std::mutex> lk(mutex_);
            live_probe_cache_ = std::move(results);
            probe_running_ = false;
        }
        bool was_stopping = false;
        {
            std::lock_guard<std::mutex> lk(login_mutex_);
            was_stopping = stopping_;
            --active_bg_threads_;
        }
        login_cv_.notify_all();
        if (!was_stopping) {
            brls::sync([this, on_updated]() {
                notifyChanges();
                if (on_updated) on_updated();
            });
        }
    }).detach();
}

uint64_t AuthCenterManager::subscribeChanges(ChangeListener listener) {
    std::lock_guard<std::mutex> lk(listener_mutex_);
    const uint64_t id = next_listener_id_++;
    listeners_.emplace_back(id, std::move(listener));
    return id;
}

void AuthCenterManager::unsubscribeChanges(uint64_t token) {
    std::lock_guard<std::mutex> lk(listener_mutex_);
    listeners_.erase(
        std::remove_if(listeners_.begin(), listeners_.end(),
                       [token](const auto& kv) { return kv.first == token; }),
        listeners_.end());
}

void AuthCenterManager::notifyChanges() {
    std::vector<ChangeListener> copy;
    {
        std::lock_guard<std::mutex> lk(listener_mutex_);
        copy.reserve(listeners_.size());
        for (const auto& [_, fn] : listeners_) {
            if (fn) copy.push_back(fn);
        }
    }
    for (const auto& fn : copy) fn();
}

// =============================================================================
// AuthCenterDialog UI
// =============================================================================
namespace {

class AuthCenterDialogBox final : public brls::Box {
public:
    AuthCenterDialogBox(AuthCenterCallbacks callbacks, std::string initial_plugin_id)
        : brls::Box(brls::Axis::COLUMN),
          callbacks_(std::move(callbacks)),
          focus_plugin_id_(std::move(initial_plugin_id)) {
        setPadding(20, 22, 14, 22);
        setWidth(920);

        // Header
        auto* hdr = new brls::Box(brls::Axis::ROW);
        hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        hdr->setAlignItems(brls::AlignItems::CENTER);
        hdr->setMarginBottom(8);

        auto* title_col = new brls::Box(brls::Axis::COLUMN);
        title_col->setShrink(1.0f);
        title_col->addView(ui::makeLabel(
            "统一账号授权与浏览器自动 Cookie 中心 · Auth & Session Hub", 20.0f, "brls/text"));
        summary_label_ = ui::makeLabel("正在汇总全量插件登录授权状态…", 13.5f, "font/grey");
        summary_label_->setMarginTop(3);
        title_col->addView(summary_label_);
        hdr->addView(title_col);

        auto* hdr_actions = new brls::Box(brls::Axis::ROW);
        hdr_actions->setAlignItems(brls::AlignItems::CENTER);

        auto* custom_url_btn = ui::makeButton("🌐 任意网址登录取Cookie", [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            promptCustomSiteBrowserLogin("plugin-blog", "https://");
        }, false, 34.0f);
        custom_url_btn->setMarginRight(8);
        hdr_actions->addView(custom_url_btn);

        auto* verify_btn = ui::makeButton("⚡ 实测校验状态", [alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            brls::Application::notify("正在后台实测校验 Apple Music / NAS / Arr 服务状态…");
            AuthCenterManager::instance().triggerAsyncLiveVerification([]() {
                brls::Application::notify("已完成全量授权状态校验");
            });
        }, true, 34.0f);
        hdr_actions->addView(verify_btn);

        hdr->addView(hdr_actions);
        addView(hdr);

        // Active browser login status banner (shown dynamically)
        active_login_box_ = new brls::Box(brls::Axis::COLUMN);
        active_login_box_->setWidthPercentage(100);
        active_login_box_->setVisibility(brls::Visibility::GONE);
        active_login_box_->setMarginBottom(10);
        addView(active_login_box_);

        // Filter bar
        filter_bar_ = new brls::Box(brls::Axis::ROW);
        filter_bar_->setAlignItems(brls::AlignItems::CENTER);
        filter_bar_->setMarginBottom(10);
        ui_comfort::enableFlexWrap(filter_bar_, 6.0f);
        addView(filter_bar_);

        // Scrollable list of auth cards
        list_scroll_ = new brls::ScrollingFrame();
        list_scroll_->setWidthPercentage(100);
        list_scroll_->setHeight(460);
        list_box_ = new brls::Box(brls::Axis::COLUMN);
        list_box_->setWidthPercentage(100);
        list_scroll_->setContentView(list_box_);
        addView(list_scroll_);

        sub_token_ = AuthCenterManager::instance().subscribeChanges([this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (alive->load(std::memory_order_acquire)) {
                    rebuild();
                }
            });
        });

        rebuild();
        AuthCenterManager::instance().triggerAsyncLiveVerification();
    }

    ~AuthCenterDialogBox() override {
        alive_->store(false, std::memory_order_release);
        if (sub_token_ != 0) {
            AuthCenterManager::instance().unsubscribeChanges(sub_token_);
            sub_token_ = 0;
        }
    }

    void setDialog(brls::Dialog* dlg) { dialog_ = dlg; }

private:
    enum class FilterMode {
        AllAuth = 0,
        NeedsAttention = 1,
        CookieSites = 2,
        NativeAndKeys = 3,
        NoAuth = 4,
    };

    void promptCustomSiteBrowserLogin(const std::string& plugin_id, const std::string& prefill) {
        if (auto* plat = brls::Application::getPlatform()) {
            plat->getImeManager()->openForText(
                [alive = alive_, plugin_id](std::string url) {
                    brls::sync([alive, plugin_id, url = std::move(url)]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        const std::string clean = trimAscii(url);
                        if (clean.empty()) return;
                        AuthCenterManager::instance().startBrowserCookieLogin(plugin_id, clean);
                    });
                },
                "输入要登录并提取 Cookie 的网站地址",
                "将弹出独立 Chrome 登录窗口，完成登录或验证后关闭窗口即可自动同步该域名全部 Cookie（含 HttpOnly）",
                512,
                prefill);
        }
    }

    void rebuild() {
        auto& mgr = AuthCenterManager::instance();
        const auto reports = mgr.inspectAll();
        const auto counts = mgr.summarizeCounts(reports);

        if (summary_label_) {
            std::string sum =
                "需鉴权模块 " + std::to_string(counts.total_auth_plugins) + " 个  ·  ✓ 已登录授权 " +
                std::to_string(counts.authenticated_count) + " 个";
            if (counts.guest_cookie_warning_count > 0) {
                sum += "  ·  ⚠ 仅访客Cookie(假登录) " +
                       std::to_string(counts.guest_cookie_warning_count) + " 个";
            }
            if (counts.missing_required_count > 0) {
                sum += "  ·  ✕ 待授权 " + std::to_string(counts.missing_required_count) + " 个";
            }
            sum += "  ·  ○ 免登可用 " + std::to_string(counts.optional_guest_count) + " 个";
            summary_label_->setText(sum);
        }

        // Active browser login banner
        clearBoxSafely(active_login_box_);
        std::string running_pid, running_txt;
        if (mgr.isBrowserLoginRunning(&running_pid, &running_txt)) {
            active_login_box_->setVisibility(brls::Visibility::VISIBLE);
            auto* banner = ui::makeCard("status/info_bg", 12.0f, 10.0f);
            banner->setBorderThickness(1.0f);
            banner->setBorderColor(ui::color("brls/accent"));

            auto* b_top = new brls::Box(brls::Axis::ROW);
            b_top->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            b_top->setAlignItems(brls::AlignItems::CENTER);

            auto* b_lbl = ui::makeLabel("🌐 正在浏览器登录: " + running_pid, 14.5f, "status/info_fg");
            b_lbl->setShrink(1.0f);
            b_top->addView(b_lbl);

            auto* b_btns = new brls::Box(brls::Axis::ROW);
            auto* finish_btn = ui::makeButton("✓ 我已登录·立即提取保存", []() {
                AuthCenterManager::instance().finishActiveLoginCaptureNow();
            }, true, 32.0f);
            finish_btn->setMarginRight(8);
            b_btns->addView(finish_btn);

            b_btns->addView(ui::makeButton("取消并关闭窗口", []() {
                AuthCenterManager::instance().cancelActiveBrowserLogin();
            }, false, 32.0f));
            b_top->addView(b_btns);
            banner->addView(b_top);

            if (!running_txt.empty()) {
                auto* sub = ui::makeLabel(running_txt, 13.0f, "brls/text", 1.38f);
                sub->setMarginTop(6);
                banner->addView(sub);
            }
            active_login_box_->addView(banner);
        } else {
            active_login_box_->setVisibility(brls::Visibility::GONE);
        }

        // Filter chips
        clearBoxSafely(filter_bar_);
        const int attention_cnt = counts.guest_cookie_warning_count + counts.missing_required_count +
                                  counts.optional_guest_count;
        struct ChipSpec {
            FilterMode mode;
            std::string label;
            const char* accent;
        };
        const ChipSpec chips[] = {
            {FilterMode::AllAuth,
             "全部鉴权项 (" + std::to_string(counts.total_auth_plugins) + ")",
             "brls/accent"},
            {FilterMode::NeedsAttention,
             "未登录 / 待处理 (" + std::to_string(attention_cnt) + ")",
             "status/warn_fg"},
            {FilterMode::CookieSites, "Cookie 浏览器自动登录", "domain/text"},
            {FilterMode::NativeAndKeys, "原生账号与服务密钥", "domain/media"},
            {FilterMode::NoAuth,
             "免登录底座 (" + std::to_string(counts.no_auth_count) + ")",
             "font/grey"},
        };
        for (const auto& ch : chips) {
            auto* btn = ui_comfort::makeFilterChip(
                ch.label,
                [this, alive = alive_, m = ch.mode]() {
                    brls::sync([this, alive, m]() {
                        if (!alive->load(std::memory_order_acquire)) return;
                        filter_ = m;
                        if (list_scroll_) list_scroll_->setContentOffsetY(0.0f, false);
                        rebuild();
                    });
                },
                filter_ == ch.mode, ch.accent, 31.0f);
            btn->setMarginRight(6);
            filter_bar_->addView(btn);
        }

        // Cards list
        const float saved_y = list_scroll_ ? list_scroll_->getContentOffsetY() : 0.0f;
        clearBoxSafely(list_box_);

        int shown = 0;
        for (const auto& r : reports) {
            if (filter_ == FilterMode::AllAuth && r.method == AuthMethodKind::NoAuth) continue;
            if (filter_ == FilterMode::NeedsAttention &&
                (r.method == AuthMethodKind::NoAuth || r.status == AuthStatusKind::Authenticated)) {
                continue;
            }
            if (filter_ == FilterMode::CookieSites && !r.supports_browser_cookie_login) continue;
            if (filter_ == FilterMode::NativeAndKeys && r.method != AuthMethodKind::NativeOrServiceKey) {
                continue;
            }
            if (filter_ == FilterMode::NoAuth && r.method != AuthMethodKind::NoAuth) continue;

            ++shown;
            auto* card = ui::makeCard(
                (r.plugin_id == focus_plugin_id_) ? "desktop/active" : "desktop/soft",
                14.0f, 10.0f);
            card->setMarginBottom(9);
            if (r.status == AuthStatusKind::GuestCookieOnly) {
                card->setBorderThickness(1.0f);
                card->setBorderColor(ui::color("status/warn_fg"));
            }

            auto* row1 = new brls::Box(brls::Axis::ROW);
            row1->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            row1->setAlignItems(brls::AlignItems::CENTER);

            auto* left_title = new brls::Box(brls::Axis::ROW);
            left_title->setAlignItems(brls::AlignItems::CENTER);
            left_title->setShrink(1.0f);
            auto* ttl = ui::makeLabel(r.title, 16.0f, "brls/text");
            ttl->setShrink(1.0f);
            left_title->addView(ttl);
            auto* cat_pill = ui::makePill(r.category_label, "desktop/surface", "font/grey");
            cat_pill->setMarginLeft(8);
            cat_pill->setShrink(0.0f);
            left_title->addView(cat_pill);
            row1->addView(left_title);

            auto* st_pill = ui::makePill(r.status_badge_text, r.status_bg_token, r.status_fg_token);
            st_pill->setShrink(0.0f);
            row1->addView(st_pill);
            card->addView(row1);

            auto* detail_lbl = ui::makeLabel(
                r.detail_summary,
                13.5f,
                r.status == AuthStatusKind::GuestCookieOnly ? "status/warn_fg" : "brls/text",
                1.42f);
            detail_lbl->setMarginTop(5);
            card->addView(detail_lbl);

            auto* where_lbl = ui::makeLabel(r.where_to_auth_hint, 12.5f, "font/grey", 1.35f);
            where_lbl->setMarginTop(3);
            card->addView(where_lbl);

            if (r.method != AuthMethodKind::NoAuth) {
                auto* actions = new brls::Box(brls::Axis::ROW);
                actions->setAlignItems(brls::AlignItems::CENTER);
                actions->setMarginTop(8);
                ui_comfort::enableFlexWrap(actions, 6.0f);

                if (r.supports_browser_cookie_login) {
                    const std::string login_btn_lbl =
                        r.allows_custom_login_url
                            ? "🌐 输入网址登录自动取 Cookie"
                            : (r.status == AuthStatusKind::Authenticated
                                   ? "🌐 重新浏览器登录更新 Cookie"
                                   : "🌐 浏览器登录自动取 Cookie");
                    auto* b_login = ui::makeButton(
                        login_btn_lbl,
                        [this, alive = alive_, pid = r.plugin_id,
                         custom = r.allows_custom_login_url, def_url = r.default_login_url]() {
                            if (!alive->load(std::memory_order_acquire)) return;
                            if (custom) {
                                promptCustomSiteBrowserLogin(pid, def_url);
                            } else {
                                AuthCenterManager::instance().startBrowserCookieLogin(pid);
                            }
                        },
                        r.status != AuthStatusKind::Authenticated, 33.0f);
                    b_login->setMarginRight(7);
                    actions->addView(b_login);

                    if (!r.allows_custom_login_url) {
                        auto* sync_btn = ui::makeButton(
                            "🔄 同步现有会话",
                            [alive = alive_, pid = r.plugin_id]() {
                                if (!alive->load(std::memory_order_acquire)) return;
                                std::string msg;
                                AuthCenterManager::instance().syncCookiesFromExistingStores(pid, msg);
                                if (!msg.empty()) brls::Application::notify(msg);
                            },
                            false, 33.0f);
                        sync_btn->setMarginRight(7);
                        actions->addView(sync_btn);
                    }

                    if (r.status == AuthStatusKind::GuestCookieOnly ||
                        (r.status == AuthStatusKind::Authenticated &&
                         r.method == AuthMethodKind::BrowserCookie)) {
                        auto* clr_btn = ui::makeButton(
                            r.status == AuthStatusKind::GuestCookieOnly ? "清理无效访客Cookie" : "清除保存Cookie",
                            [alive = alive_, pid = r.plugin_id]() {
                                if (!alive->load(std::memory_order_acquire)) return;
                                std::string msg;
                                AuthCenterManager::instance().clearPluginCookieAuth(pid, msg);
                                if (!msg.empty()) brls::Application::notify(msg);
                            },
                            false, 33.0f);
                        clr_btn->setMarginRight(7);
                        actions->addView(clr_btn);
                    }
                }

                if (auto tab = findCustomTab(r.plugin_id)) {
                    auto* tab_btn = ui::makeButton(
                        "打开专属页: " + tab->title + " ▸",
                        [this, tid = tab->tab_id]() {
                            auto cb = callbacks_.on_open_custom_tab;
                            if (dialog_) dialog_->dismiss();
                            if (cb) brls::sync([cb, tid]() { cb(tid); });
                        },
                        r.plugin_id == "plugin-music" && r.status != AuthStatusKind::Authenticated,
                        33.0f);
                    tab_btn->setMarginRight(7);
                    actions->addView(tab_btn);
                } else if (r.plugin_id == "plugin-magpie-agent" && callbacks_.on_open_ai_workbench) {
                    auto* ai_btn = ui::makeButton(
                        "打开 AI 对话与路由面板 ▸",
                        [this]() {
                            auto cb = callbacks_.on_open_ai_workbench;
                            if (dialog_) dialog_->dismiss();
                            if (cb) brls::sync([cb]() { cb(); });
                        },
                        false, 33.0f);
                    ai_btn->setMarginRight(7);
                    actions->addView(ai_btn);
                }

                if (r.is_installed && callbacks_.on_open_plugin_config) {
                    auto* cfg_btn = ui::makeButton(
                        "⚙ 插件参数与密钥 ▸",
                        [this, pid = r.plugin_id]() {
                            auto cb = callbacks_.on_open_plugin_config;
                            if (dialog_) dialog_->dismiss();
                            if (cb) brls::sync([cb, pid]() { cb(pid); });
                        },
                        false, 33.0f);
                    actions->addView(cfg_btn);
                }

                card->addView(actions);
            }

            list_box_->addView(card);
        }

        if (shown == 0) {
            auto* empty = ui::makeCard("desktop/soft", 18.0f, 10.0f);
            empty->addView(ui::makeLabel("当前筛选分类下暂无需要处理的项目。", 14.5f, "font/grey"));
            list_box_->addView(empty);
        }

        if (list_scroll_ && saved_y > 0.0f) {
            list_scroll_->setContentOffsetY(saved_y, false);
        }
        DesktopInputController::markDirty(this);
    }

    AuthCenterCallbacks callbacks_;
    std::string focus_plugin_id_;
    FilterMode filter_ = FilterMode::AllAuth;
    brls::Dialog* dialog_ = nullptr;
    brls::Label* summary_label_ = nullptr;
    brls::Box* active_login_box_ = nullptr;
    brls::Box* filter_bar_ = nullptr;
    brls::ScrollingFrame* list_scroll_ = nullptr;
    brls::Box* list_box_ = nullptr;
    uint64_t sub_token_ = 0;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
};

} // namespace

void AuthCenterDialog::open(AuthCenterCallbacks callbacks,
                            const std::string& initial_focus_plugin_id) {
    if (!brls::Application::getPlatform()) {
        return;
    }
    auto* box = new AuthCenterDialogBox(std::move(callbacks), initial_focus_plugin_id);
    auto* dialog = new brls::Dialog(box);
    box->setDialog(dialog);
    if (auto* applet = dynamic_cast<brls::Box*>(dialog->getView("brls/dialog/applet"))) {
        applet->setWidth(960.0f);
        applet->setCornerRadius(14.0f);
        applet->setBorderThickness(1.0f);
        applet->setBorderColor(ui::color("desktop/line"));
        applet->setBackgroundColor(ui::color("brls/background"));
        applet->setClipsToBounds(true);
    }
    dialog->addButton("关闭 Esc", []() {});
    if (auto* btn1 = dialog->getView("brls/dialog/button1")) {
        if (auto* btn_row = btn1->getParent()) {
            btn_row->setHeight(52.0f);
        }
    }
    dialog->open();
}

} // namespace omnidesk::host
