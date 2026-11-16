#include "music/adam_client.hpp"
#include "music/config.hpp"
#include "music/lrclib_client.hpp"

#include <arpa/inet.h>
#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <unordered_map>
#include <unordered_set>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <curl/curl.h>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <netinet/in.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <poll.h>
#include <regex>
#include <signal.h>
#include <sstream>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

namespace fs = std::filesystem;

namespace music_plugin {
namespace {

constexpr size_t kMaxHttpBytes = 16 * 1024 * 1024;

void ensureCurlInit() {
    static std::once_flag once;
    std::call_once(once, []() { curl_global_init(CURL_GLOBAL_DEFAULT); });
}

size_t curlWriteCb(char* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* out = static_cast<std::string*>(userdata);
    size_t total = size * nmemb;
    if (out->size() + total > kMaxHttpBytes) return 0;
    out->append(ptr, total);
    return total;
}

std::string toLowerCopy(std::string s) {
    for (char& ch : s) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return s;
}

std::string trimCopy(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    size_t e = s.size();
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

std::string unquoteCopy(std::string s) {
    s = trimCopy(s);
    if (s.size() >= 2 &&
        ((s.front() == '"' && s.back() == '"') || (s.front() == '\'' && s.back() == '\''))) {
        s = trimCopy(s.substr(1, s.size() - 2));
    }
    return s;
}

std::string mapNumericStorefrontId(const std::string& raw) {
    std::string trimmed = unquoteCopy(raw);
    if (trimmed.empty()) return "";
    size_t pos = 0;
    while (pos < trimmed.size() && std::isdigit(static_cast<unsigned char>(trimmed[pos]))) {
        ++pos;
    }
    if (pos == 0) {
        std::string low = toLowerCopy(trimmed);
        if (low.size() == 2 && std::isalpha(static_cast<unsigned char>(low[0])) &&
            std::isalpha(static_cast<unsigned char>(low[1]))) {
            return low;
        }
        return "";
    }
    const std::string code = trimmed.substr(0, pos);
    static const std::unordered_map<std::string, std::string> kMap = {
        {"143465", "cn"}, {"143441", "us"}, {"143462", "jp"}, {"143463", "hk"},
        {"143470", "tw"}, {"143444", "gb"}, {"143464", "sg"}, {"143466", "kr"},
        {"143442", "fr"}, {"143443", "de"}, {"143455", "ca"}, {"143460", "au"},
        {"143450", "it"}, {"143454", "es"}, {"143452", "nl"}, {"143456", "se"},
        {"143459", "ch"}, {"143503", "br"}, {"143468", "mx"}, {"143467", "in"},
        {"143473", "my"}, {"143475", "th"}, {"143474", "ph"}, {"143476", "id"},
        {"143471", "vn"}, {"143515", "mo"},
    };
    auto it = kMap.find(code);
    return (it != kMap.end()) ? it->second : "";
}

std::string readSmallOwnedTextFile(const fs::path& p, size_t min_len, size_t max_len) {
    int fd = ::open(p.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return "";
    struct stat st {};
    if (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_uid != ::geteuid() ||
        st.st_size < 0 || static_cast<size_t>(st.st_size) > max_len) {
        ::close(fd);
        return "";
    }
    std::string buf(static_cast<size_t>(st.st_size), '\0');
    size_t total = 0;
    while (total < buf.size()) {
        ssize_t n = ::read(fd, buf.data() + total, buf.size() - total);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        total += static_cast<size_t>(n);
    }
    ::close(fd);
    buf.resize(total);
    buf = unquoteCopy(buf);
    return (buf.size() >= min_len) ? buf : "";
}

std::string formatArtworkUrl(std::string tpl, int size = 500) {
    if (tpl.empty()) return "";
    std::string s = std::to_string(size);
    auto replaceAll = [&](const std::string& from, const std::string& to) {
        size_t pos = 0;
        while ((pos = tpl.find(from, pos)) != std::string::npos) {
            tpl.replace(pos, from.size(), to);
            pos += to.size();
        }
    };
    replaceAll("{w}", s);
    replaceAll("{h}", s);
    replaceAll("{c}", "bb");
    replaceAll("{f}", "jpg");
    return tpl;
}

int connectLoopbackTimeout(uint16_t port, int timeout_ms) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int flags = ::fcntl(fd, F_GETFL, 0);
    ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    int rc = ::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    if (rc < 0 && errno != EINPROGRESS && errno != EINTR) {
        ::close(fd);
        return -1;
    }
    if (rc != 0) {
        int prc = 0;
        do {
            struct pollfd pfd{fd, POLLOUT, 0};
            prc = ::poll(&pfd, 1, timeout_ms);
        } while (prc < 0 && errno == EINTR);
        if (prc <= 0) {
            ::close(fd);
            return -1;
        }
        int err = 0;
        socklen_t len = sizeof(err);
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) != 0 || err != 0) {
            ::close(fd);
            return -1;
        }
    }
    ::fcntl(fd, F_SETFL, flags);
    struct timeval tv{timeout_ms / 1000, (timeout_ms % 1000) * 1000};
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    return fd;
}

std::vector<uint8_t> getPeerDerCert(SSL* ssl) {
    X509* cert = SSL_get1_peer_certificate(ssl);
    if (!cert) return {};
    int len = i2d_X509(cert, nullptr);
    std::vector<uint8_t> der;
    if (len > 0 && len <= 65536) {
        der.resize(static_cast<size_t>(len));
        uint8_t* p = der.data();
        i2d_X509(cert, &p);
    }
    X509_free(cert);
    return der;
}


int nosigpipeBioWrite(BIO* b, const char* data, int len) {
    int fd = -1;
    BIO_get_fd(b, &fd);
    BIO_clear_retry_flags(b);
    ssize_t n = -1;
    do {
        n = ::send(fd, data, static_cast<size_t>(len), MSG_NOSIGNAL);
    } while (n < 0 && errno == EINTR);
    if (n <= 0) {
        return -1;
    }
    return static_cast<int>(n);
}

int nosigpipeBioRead(BIO* b, char* data, int len) {
    int fd = -1;
    BIO_get_fd(b, &fd);
    BIO_clear_retry_flags(b);
    ssize_t n = -1;
    do {
        n = ::recv(fd, data, static_cast<size_t>(len), 0);
    } while (n < 0 && errno == EINTR);
    if (n < 0) {
        return -1;
    }
    return static_cast<int>(n);
}


BIO* makeNosigpipeSocketBio(int fd) {
    static BIO_METHOD* method = []() {
        BIO_METHOD* m = BIO_meth_new(BIO_TYPE_SOCKET, "nosigpipe_socket");
        BIO_meth_set_write(m, nosigpipeBioWrite);
        BIO_meth_set_read(m, nosigpipeBioRead);
        BIO_meth_set_ctrl(m, [](BIO* b, int cmd, long /*num*/, void* ptr) -> long {
            int cur_fd = -1;
            switch (cmd) {
                case BIO_C_SET_FD:
                    BIO_set_data(b, reinterpret_cast<void*>(static_cast<intptr_t>(*static_cast<int*>(ptr))));
                    BIO_set_init(b, 1);
                    return 1;
                case BIO_C_GET_FD:
                    cur_fd = static_cast<int>(reinterpret_cast<intptr_t>(BIO_get_data(b)));
                    if (ptr) *static_cast<int*>(ptr) = cur_fd;
                    return cur_fd;
                case BIO_CTRL_FLUSH:
                case BIO_CTRL_DUP:
                    return 1;
                default:
                    return 0;
            }
        });
        BIO_meth_set_create(m, [](BIO* b) -> int {
            BIO_set_init(b, 0);
            BIO_set_data(b, reinterpret_cast<void*>(static_cast<intptr_t>(-1)));
            return 1;
        });
        BIO_meth_set_destroy(m, [](BIO*) -> int { return 1; });
        return m;
    }();
    BIO* bio = BIO_new(method);
    if (bio) {
        BIO_set_fd(bio, fd, BIO_NOCLOSE);
    }
    return bio;
}

std::string jStr(const nlohmann::json& j, const char* key, const std::string& def = "") {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    return def;
}

int jInt(const nlohmann::json& j, const char* key, int def = 0) {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_number_integer()) return v.get<int>();
    if (v.is_number_float()) return static_cast<int>(v.get<double>());
    return def;
}

int64_t jInt64(const nlohmann::json& j, const char* key, int64_t def = 0) {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_number_integer()) return v.get<int64_t>();
    if (v.is_number_float()) return static_cast<int64_t>(v.get<double>());
    return def;
}

bool jBool(const nlohmann::json& j, const char* key, bool def = false) {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number_integer()) return v.get<int>() != 0;
    return def;
}

DrmStatus parseDrmSnapshot(const nlohmann::json& j) {
    DrmStatus st;
    if (!j.is_object()) return st;
    st.engine_reachable = true;
    st.available = jBool(j, "available", true);
    if (j.contains("backend") && j["backend"].is_object()) {
        st.backend = jStr(j["backend"], "selected");
    }
    if (j.contains("state") && j["state"].is_object()) {
        const auto& s = j["state"];
        st.manager_state = jStr(s, "manager", "unknown");
        st.process_state = jStr(s, "process", "unknown");
        st.auth_state = jStr(s, "authentication", "unknown");
        st.fairplay_state = jStr(s, "fairplay", "unknown");
        st.session_state = jStr(s, "session", "unknown");
    }
    if (j.contains("capabilities") && j["capabilities"].is_object()) {
        const auto& c = j["capabilities"];
        st.cap_cbcs = jBool(c, "cbcs", false);
        st.cap_alac = jBool(c, "alac", false);
        st.cap_atmos = jBool(c, "atmos", false);
        st.cap_hi_res = jBool(c, "hiRes", false);
    }
    if (j.contains("challenge") && j["challenge"].is_object()) {
        st.challenge_prompt = jStr(j["challenge"], "prompt", jStr(j["challenge"], "message"));
    }
    st.message = jStr(j, "message");
    return st;
}

void applyAudioTraits(const nlohmann::json& traits, MusicTrack& tr) {
    if (!traits.is_array()) return;
    for (const auto& t : traits) {
        if (!t.is_string()) continue;
        std::string s = t.get<std::string>();
        if (s == "hi-res-lossless") tr.is_hires = true;
        else if (s == "lossless") tr.is_lossless = true;
        else if (s == "atmos" || s == "spatial") tr.is_atmos = true;
    }
    if (tr.is_hires) tr.quality_label = "Hi-Res ALAC";
    else if (tr.is_lossless) tr.quality_label = "ALAC Lossless";
    else if (tr.is_atmos) tr.quality_label = "Dolby Atmos";
    else tr.quality_label = "AAC 256kbps";
}

} // namespace

std::string PlaybackSessionInfo::formattedQuality() const {
    if (type == "mv") return "Apple Music MV";
    std::string c = codec.empty() ? "ALAC" : codec;
    for (char& ch : c) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    if (bit_depth > 0 && sample_rate > 0) {
        std::ostringstream oss;
        oss << c << " " << bit_depth << "bit/";
        if (sample_rate % 1000 == 0) oss << (sample_rate / 1000) << "kHz";
        else oss << (sample_rate / 1000.0) << "kHz";
        return oss.str();
    }
    if (bit_rate > 0) {
        return c + " " + std::to_string(bit_rate / 1000) + "kbps";
    }
    return c;
}

bool AdamClient::isLoopbackPortOwnedByUid(uint16_t port, uint32_t expected_uid) {
    char expected[32];
    std::snprintf(expected, sizeof(expected), "0100007F:%04X", static_cast<unsigned>(port));
    std::ifstream ifs("/proc/net/tcp");
    if (!ifs.is_open()) return false;
    std::string line;
    std::getline(ifs, line);
    while (std::getline(ifs, line)) {
        std::istringstream iss(line);
        std::string sl, local_addr, rem_addr, st, tx_rx, tr_tm, retrnsmt, uid_str;
        if (!(iss >> sl >> local_addr >> rem_addr >> st >> tx_rx >> tr_tm >> retrnsmt >> uid_str)) {
            continue;
        }
        std::string upper = local_addr;
        for (char& ch : upper) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
        if (upper == expected && st == "0A") {
            try {
                if (std::stoul(uid_str) == expected_uid) return true;
            } catch (...) {
            }
        }
    }
    return false;
}

std::string AdamClient::decodeChunkedBody(const std::string& raw) {
    std::string out;
    size_t pos = 0;
    while (pos < raw.size()) {
        size_t line_end = raw.find("\r\n", pos);
        if (line_end == std::string::npos) break;
        std::string hex_len = raw.substr(pos, line_end - pos);
        size_t semi = hex_len.find(';');
        if (semi != std::string::npos) hex_len = hex_len.substr(0, semi);
        size_t chunk_len = 0;
        try {
            chunk_len = std::stoul(trimCopy(hex_len), nullptr, 16);
        } catch (...) {
            break;
        }
        pos = line_end + 2;
        if (chunk_len == 0) break;
        if (pos + chunk_len > raw.size()) {
            out.append(raw.data() + pos, raw.size() - pos);
            break;
        }
        out.append(raw.data() + pos, chunk_len);
        pos += chunk_len + 2;
    }
    return out;
}

AdamClient::AdamClient(LyricsCacheStore* cache) : cache_(cache) {
    if (const char* tok = std::getenv("ADAM_API_TOKEN"); tok && *tok) {
        api_token_env_ = tok;
    }
}

AdamClient::~AdamClient() {
    stopManagedEngine();
}

void AdamClient::configure(const MusicConfig& cfg) {
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        cfg_ = cfg;
    }
    if (!cfg.media_user_token.empty()) {
        std::lock_guard<std::mutex> lk(token_mutex_);
        media_user_token_ = cfg.media_user_token;
    }
}

bool AdamClient::isPortReachable() const {
    uint16_t port = 20025;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        port = static_cast<uint16_t>(cfg_.engine_port);
    }
    if (!isLoopbackPortOwnedByUid(port, static_cast<uint32_t>(::geteuid()))) {
        return false;
    }
    int fd = connectLoopbackTimeout(port, 400);
    if (fd >= 0) {
        ::close(fd);
        return true;
    }
    return false;
}

bool AdamClient::ensureEngineRunning(std::string& error) {
    if (isPortReachable()) return true;

    MusicConfig cfg;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        cfg = cfg_;
    }
    if (!cfg.auto_launch_engine) {
        error = "Adam Go 引擎未运行且已关闭自动拉起";
        return false;
    }
    if (cfg.engine_bin.empty()) {
        error = "未找到 Adam Go 引擎二进制文件（请检查设置中的 engine_bin 或 /home/zhu/Projects/apple-music-online/electron/dist/resources/engine）";
        return false;
    }

    std::error_code ec;
    if (!fs::exists(cfg.engine_bin, ec)) {
        error = "Adam Go 引擎路径不存在: " + cfg.engine_bin;
        return false;
    }

    std::lock_guard<std::mutex> lk(proc_mutex_);
    if (isPortReachable()) return true;

    // Clean up exited child if any
    if (managed_pid_ > 0) {
        int st = 0;
        pid_t w = ::waitpid(managed_pid_, &st, WNOHANG);
        if (w == managed_pid_ || w < 0) managed_pid_ = -1;
        else return true; // still starting
    }

    std::string data_dir = cfg.engine_data_dir;
    if (data_dir.empty()) data_dir = expandUserPath("~/.config/adam/engine-data");
    fs::create_directories(data_dir, ec);

    // Ensure config.yaml exists inside engine_data_dir
    fs::path cfg_yaml = fs::path(data_dir) / "config.yaml";
    if (!fs::exists(cfg_yaml, ec)) {
        std::string drm_base = expandUserPath("~/.config/adam/drm/files");
        std::string repo_drm = "/home/zhu/Projects/apple-music-online/drm/files";
        if (!fs::exists(drm_base, ec) && fs::exists(repo_drm, ec)) {
            drm_base = repo_drm;
        }
        fs::create_directories(drm_base, ec);
        std::ofstream ofs(cfg_yaml);
        if (ofs.is_open()) {
            ofs << "storefront: " << (cfg.storefront.empty() ? "us" : cfg.storefront) << "\n"
                << "media-user-token: \"\"\n"
                << "authorization-token: \"\"\n"
                << "decrypt-m3u8-port: 127.0.0.1:10020\n"
                << "get-m3u8-port: 127.0.0.1:20020\n"
                << "get-m3u8-from-device: true\n"
                << "get-m3u8-mode: " << (cfg.hires_enabled ? "hires" : "lossless") << "\n"
                << "aac-type: aac-lc\n"
                << "alac-max: 192000\n"
                << "drm-base-dir: \"" << drm_base << "\"\n";
        }
    }

    std::string log_path = cfg.data_dir.empty()
                               ? "/tmp/omnidesk-adam-engine.log"
                               : (fs::path(cfg.data_dir) / "adam-engine.log").string();
    fs::create_directories(fs::path(log_path).parent_path(), ec);

    std::string engine_dir = fs::path(cfg.engine_bin).parent_path().string();
    std::string port_str = std::to_string(cfg.engine_port);

    pid_t pid = ::fork();
    if (pid < 0) {
        error = "fork Adam Go 引擎失败";
        return false;
    }
    if (pid == 0) {
        ::setpgid(0, 0);
        if (::chdir(data_dir.c_str()) != 0) {
            ::_exit(126);
        }
        int devnull = ::open("/dev/null", O_RDONLY);
        if (devnull >= 0) {
            ::dup2(devnull, STDIN_FILENO);
            ::close(devnull);
        }
        int logfd = ::open(log_path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (logfd >= 0) {
            ::dup2(logfd, STDOUT_FILENO);
            ::dup2(logfd, STDERR_FILENO);
            ::close(logfd);
        }
        ::setenv("GODEBUG", "netdns=go", 1);
        const char* cur_ld = ::getenv("LD_LIBRARY_PATH");
        std::string new_ld = engine_dir + (cur_ld && *cur_ld ? (std::string(":") + cur_ld) : "");
        ::setenv("LD_LIBRARY_PATH", new_ld.c_str(), 1);

        char* argv[] = {
            const_cast<char*>(cfg.engine_bin.c_str()),
            const_cast<char*>("--api"),
            const_cast<char*>(port_str.c_str()),
            nullptr
        };
        ::execvp(argv[0], argv);
        ::_exit(127);
    }

    managed_pid_ = pid;
    for (int i = 0; i < 35; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (isPortReachable()) return true;
        int st = 0;
        if (::waitpid(pid, &st, WNOHANG) == pid) {
            managed_pid_ = -1;
            error = "Adam Go 引擎启动后提前退出，请查看日志: " + log_path;
            return false;
        }
    }
    error = "等待 Adam Go 引擎监听端口超时 (" + port_str + ")";
    return false;
}

void AdamClient::stopManagedEngine() {
    std::lock_guard<std::mutex> lk(proc_mutex_);
    if (managed_pid_ <= 0) return;

    pid_t pid = managed_pid_;
    managed_pid_ = -1;
    ::kill(-pid, SIGTERM);
    ::kill(pid, SIGTERM);

    for (int i = 0; i < 20; ++i) {
        int st = 0;
        pid_t w = ::waitpid(pid, &st, WNOHANG);
        if (w == pid || w < 0) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    ::kill(-pid, SIGKILL);
    ::kill(pid, SIGKILL);
    int st = 0;
    ::waitpid(pid, &st, 0);
}

HttpResponse AdamClient::engineRequest(const std::string& method,
                                       const std::string& path,
                                       const std::string& body,
                                       const std::string& content_type,
                                       const std::string& accept,
                                       int timeout_ms) {
    HttpResponse resp;
    uint16_t port = 20025;
    std::string lang = "zh-CN";
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        port = static_cast<uint16_t>(cfg_.engine_port);
        lang = cfg_.language;
    }

    if (!isLoopbackPortOwnedByUid(port, static_cast<uint32_t>(::geteuid()))) {
        resp.error = "Adam Go 引擎未运行或端口归属当前用户校验失败";
        return resp;
    }

    auto doTlsConnect = [&](const std::vector<uint8_t>* pin_der,
                            std::vector<uint8_t>* captured_der,
                            int& fd_out,
                            SSL*& ssl_out,
                            SSL_CTX*& ctx_out) -> bool {
        fd_out = connectLoopbackTimeout(port, timeout_ms);
        if (fd_out < 0) return false;

        ctx_out = SSL_CTX_new(TLS_client_method());
        if (!ctx_out) { ::close(fd_out); return false; }
        SSL_CTX_set_min_proto_version(ctx_out, TLS1_2_VERSION);

        if (pin_der && !pin_der->empty()) {
            const uint8_t* p = pin_der->data();
            X509* trusted = d2i_X509(nullptr, &p, static_cast<long>(pin_der->size()));
            if (!trusted) {
                SSL_CTX_free(ctx_out);
                ::close(fd_out);
                return false;
            }
            X509_STORE* store = SSL_CTX_get_cert_store(ctx_out);
            X509_STORE_add_cert(store, trusted);
            X509_free(trusted);
            X509_VERIFY_PARAM* param = SSL_CTX_get0_param(ctx_out);
            X509_VERIFY_PARAM_set_flags(param, X509_V_FLAG_PARTIAL_CHAIN);
            X509_VERIFY_PARAM_clear_flags(param, X509_V_FLAG_X509_STRICT);
            SSL_CTX_set_verify(ctx_out, SSL_VERIFY_PEER, nullptr);
        } else {
            SSL_CTX_set_verify(ctx_out, SSL_VERIFY_NONE, nullptr);
        }

        ssl_out = SSL_new(ctx_out);
        if (!ssl_out) { SSL_CTX_free(ctx_out); ::close(fd_out); return false; }
        SSL_set_tlsext_host_name(ssl_out, "127.0.0.1");
        if (pin_der && !pin_der->empty()) X509_VERIFY_PARAM_set1_ip_asc(SSL_get0_param(ssl_out), "127.0.0.1");
        BIO* bio = makeNosigpipeSocketBio(fd_out);
        if (!bio) { SSL_free(ssl_out); SSL_CTX_free(ctx_out); ::close(fd_out); return false; }
        SSL_set_bio(ssl_out, bio, bio);
        if (SSL_connect(ssl_out) != 1) {
            SSL_free(ssl_out);
            SSL_CTX_free(ctx_out);
            ::close(fd_out);
            ssl_out = nullptr; ctx_out = nullptr; fd_out = -1;
            return false;
        }
        if (captured_der) *captured_der = getPeerDerCert(ssl_out);
        return true;
    };

    std::vector<uint8_t> leaf_der;
    {
        int fd = -1; SSL* ssl = nullptr; SSL_CTX* ctx = nullptr;
        if (!doTlsConnect(nullptr, &leaf_der, fd, ssl, ctx) || leaf_der.empty()) {
            resp.error = "无法捕获 Adam 回环 TLS 证书";
            if (ssl) { SSL_shutdown(ssl); SSL_free(ssl); }
            if (ctx) SSL_CTX_free(ctx);
            if (fd >= 0) ::close(fd);
            return resp;
        }
        SSL_shutdown(ssl); SSL_free(ssl); SSL_CTX_free(ctx); ::close(fd);
    }

    int fd = -1; SSL* ssl = nullptr; SSL_CTX* ctx = nullptr;
    std::vector<uint8_t> actual_der;
    if (!doTlsConnect(&leaf_der, &actual_der, fd, ssl, ctx)) {
        resp.error = "Adam 回环 TLS 校验失败";
        return resp;
    }
    if (actual_der != leaf_der) {
        resp.error = "Adam 回环证书发生变更，请重试";
        SSL_shutdown(ssl); SSL_free(ssl); SSL_CTX_free(ctx); ::close(fd);
        return resp;
    }

    std::ostringstream req;
    req << method << " " << path << " HTTP/1.1\r\n"
        << "Host: 127.0.0.1:" << port << "\r\n"
        << "Accept: " << accept << "\r\n"
        << "Accept-Language: " << lang << "\r\n"
        << "Accept-Encoding: identity\r\n"
        << "Connection: close\r\n";
    if (!api_token_env_.empty()) {
        req << "X-Api-Token: " << api_token_env_ << "\r\n";
    }
    if (!body.empty()) {
        req << "Content-Type: " << content_type << "\r\n"
            << "Content-Length: " << body.size() << "\r\n";
    }
    req << "\r\n" << body;
    std::string req_str = req.str();
    size_t total_written = 0;
    while (total_written < req_str.size()) {
        int wn = SSL_write(ssl,
                           req_str.data() + total_written,
                           static_cast<int>(req_str.size() - total_written));
        if (wn > 0) {
            total_written += static_cast<size_t>(wn);
            continue;
        }
        int ssl_err = SSL_get_error(ssl, wn);
        if (ssl_err == SSL_ERROR_WANT_READ || ssl_err == SSL_ERROR_WANT_WRITE) {
            continue;
        }
        break;
    }

    std::string raw;
    char buf[8192];
    while (raw.size() < kMaxHttpBytes) {
        int n = SSL_read(ssl, buf, sizeof(buf));
        if (n > 0) {
            raw.append(buf, static_cast<size_t>(n));
            continue;
        }
        int ssl_err = SSL_get_error(ssl, n);
        if (ssl_err == SSL_ERROR_WANT_READ || ssl_err == SSL_ERROR_WANT_WRITE) {
            continue;
        }
        break;
    }
    SSL_shutdown(ssl); SSL_free(ssl); SSL_CTX_free(ctx); ::close(fd);

    size_t hdr_end = raw.find("\r\n\r\n");
    if (hdr_end == std::string::npos) {
        resp.error = "Adam HTTP 响应不完整";
        return resp;
    }
    std::string hdr_block = raw.substr(0, hdr_end);
    std::string raw_body = raw.substr(hdr_end + 4);

    std::istringstream hss(hdr_block);
    std::string status_line;
    std::getline(hss, status_line);
    std::istringstream sls(status_line);
    std::string ver;
    sls >> ver >> resp.status;

    bool chunked = false;
    std::string hline;
    while (std::getline(hss, hline)) {
        if (!hline.empty() && hline.back() == '\r') hline.pop_back();
        size_t colon = hline.find(':');
        if (colon == std::string::npos) continue;
        std::string k = toLowerCopy(trimCopy(hline.substr(0, colon)));
        std::string v = trimCopy(hline.substr(colon + 1));
        if (k == "content-type") resp.content_type = v;
        if (k == "transfer-encoding" && toLowerCopy(v).find("chunked") != std::string::npos) {
            chunked = true;
        }
    }

    resp.body = chunked ? decodeChunkedBody(raw_body) : std::move(raw_body);
    resp.ok = (resp.status >= 200 && resp.status < 300);
    if (!resp.ok && resp.error.empty()) {
        resp.error = trimCopy(resp.body).empty()
                         ? ("HTTP " + std::to_string(resp.status))
                         : trimCopy(resp.body).substr(0, 240);
    }
    return resp;
}

std::string AdamClient::fetchDeveloperJwtFromApple() {
    ensureCurlInit();
    std::string proxy;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        proxy = cfg_.proxy;
    }

    auto curlGet = [&](const std::string& url, std::string& out) -> bool {
        CURL* curl = curl_easy_init();
        if (!curl) return false;
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteCb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &out);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10000L);
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(curl, CURLOPT_USERAGENT,
                         "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36");
        if (!proxy.empty()) curl_easy_setopt(curl, CURLOPT_PROXY, proxy.c_str());
        CURLcode rc = curl_easy_perform(curl);
        long code = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
        curl_easy_cleanup(curl);
        return rc == CURLE_OK && code >= 200 && code < 300;
    };

    std::string html;
    if (!curlGet("https://music.apple.com", html)) return "";

    static const std::regex kIndexRe(R"(/assets/index~[^"'\s]+\.js)");
    std::smatch m;
    if (!std::regex_search(html, m, kIndexRe)) return "";

    std::string js;
    if (!curlGet("https://music.apple.com" + m[0].str(), js)) return "";

    static const std::regex kJwtRe(R"(eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})");
    std::smatch jm;
    if (std::regex_search(js, jm, kJwtRe)) {
        return jm[0].str();
    }
    return "";
}

std::string AdamClient::getDeveloperToken(bool force_refresh) {
    {
        std::lock_guard<std::mutex> lk(token_mutex_);
        if (!force_refresh && !dev_token_.empty()) return dev_token_;
    }
    if (!force_refresh) {
        if (const char* env_dev = std::getenv("APPLE_MUSIC_DEVELOPER_TOKEN"); env_dev && *env_dev) {
            std::string trimmed = trimCopy(env_dev);
            if (!trimmed.empty()) {
                std::lock_guard<std::mutex> lk(token_mutex_);
                dev_token_ = trimmed;
                return dev_token_;
            }
        }
    }
    if (!force_refresh && cache_) {
        if (auto cached = cache_->loadCachedDevToken(); cached.has_value()) {
            std::lock_guard<std::mutex> lk(token_mutex_);
            dev_token_ = *cached;
            return dev_token_;
        }
    }
    std::string fetched = fetchDeveloperJwtFromApple();
    if (!fetched.empty()) {
        {
            std::lock_guard<std::mutex> lk(token_mutex_);
            dev_token_ = fetched;
        }
        if (cache_) cache_->saveCachedDevToken(fetched);
    }
    return fetched;
}

std::string AdamClient::cachedOrEnvDeveloperToken() const {
    {
        std::lock_guard<std::mutex> lk(token_mutex_);
        if (!dev_token_.empty()) return dev_token_;
    }
    if (const char* env_dev = std::getenv("APPLE_MUSIC_DEVELOPER_TOKEN"); env_dev && *env_dev) {
        std::string trimmed = trimCopy(env_dev);
        if (!trimmed.empty()) return trimmed;
    }
    if (cache_) {
        if (auto cached = cache_->loadCachedDevToken(); cached.has_value() && !cached->empty()) {
            return *cached;
        }
    }
    return "";
}

void AdamClient::setMediaUserToken(const std::string& mut) {
    std::lock_guard<std::mutex> lk(token_mutex_);
    media_user_token_ = trimCopy(mut);
}

std::string AdamClient::mediaUserToken() const {
    std::lock_guard<std::mutex> lk(token_mutex_);
    return media_user_token_;
}

bool AdamClient::hasDeveloperToken() const {
    return !cachedOrEnvDeveloperToken().empty() || localWrapperAuthEnabled();
}

bool AdamClient::allowLocalAdamDrmFallback() const {
    if (ampApiBaseUrl() != "https://amp-api.music.apple.com") {
        return false;
    }
    std::lock_guard<std::mutex> lk(cfg_mutex_);
    return cfg_.auto_launch_engine || cfg_.engine_port == 20025 || cfg_.engine_data_dir_explicit;
}

std::string AdamClient::discoverLocalAdamMusicToken() const {
    if (!allowLocalAdamDrmFallback()) return "";

    std::string engine_data_dir;
    bool explicit_dir = false;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        engine_data_dir = cfg_.engine_data_dir;
        explicit_dir = cfg_.engine_data_dir_explicit;
    }

    std::vector<fs::path> drm_dirs;
    if (!engine_data_dir.empty()) {
        fs::path ed(engine_data_dir);
        if (ed.has_parent_path()) {
            drm_dirs.push_back(ed.parent_path() / "drm" / "files");
        }
        std::string cfg_raw = readSmallOwnedTextFile(ed / "config.yaml", 1, 65536);
        if (!cfg_raw.empty()) {
            std::istringstream iss(cfg_raw);
            std::string line;
            while (std::getline(iss, line)) {
                size_t colon = line.find(':');
                if (colon == std::string::npos) continue;
                std::string k = toLowerCopy(trimCopy(line.substr(0, colon)));
                std::string v = unquoteCopy(line.substr(colon + 1));
                if (k == "media-user-token" && v.size() >= 16) {
                    return v;
                }
                if (k == "drm-base-dir" && !v.empty()) {
                    drm_dirs.emplace_back(expandUserPath(v));
                }
            }
        }
    }
    if (!explicit_dir) {
        std::string def_drm = expandUserPath("~/.config/adam/drm/files");
        if (!def_drm.empty()) drm_dirs.emplace_back(def_drm);
    }

    for (const auto& d : drm_dirs) {
        std::string tok = readSmallOwnedTextFile(d / "MUSIC_TOKEN", 16, 16384);
        if (!tok.empty()) return tok;
    }
    return "";
}

std::string AdamClient::discoverLocalAdamStorefront() const {
    if (!allowLocalAdamDrmFallback()) return "";

    std::string engine_data_dir;
    bool explicit_dir = false;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        engine_data_dir = cfg_.engine_data_dir;
        explicit_dir = cfg_.engine_data_dir_explicit;
    }

    std::vector<fs::path> drm_dirs;
    std::string cfg_sf;
    if (!engine_data_dir.empty()) {
        fs::path ed(engine_data_dir);
        if (ed.has_parent_path()) {
            drm_dirs.push_back(ed.parent_path() / "drm" / "files");
        }
        std::string cfg_raw = readSmallOwnedTextFile(ed / "config.yaml", 1, 65536);
        if (!cfg_raw.empty()) {
            std::istringstream iss(cfg_raw);
            std::string line;
            while (std::getline(iss, line)) {
                size_t colon = line.find(':');
                if (colon == std::string::npos) continue;
                std::string k = toLowerCopy(trimCopy(line.substr(0, colon)));
                std::string v = unquoteCopy(line.substr(colon + 1));
                if (k == "drm-base-dir" && !v.empty()) {
                    drm_dirs.emplace_back(expandUserPath(v));
                } else if (k == "storefront" && v.size() == 2) {
                    cfg_sf = toLowerCopy(v);
                }
            }
        }
    }
    if (!explicit_dir) {
        std::string def_drm = expandUserPath("~/.config/adam/drm/files");
        if (!def_drm.empty()) drm_dirs.emplace_back(def_drm);
    }

    for (const auto& d : drm_dirs) {
        std::string raw_sf = readSmallOwnedTextFile(d / "STOREFRONT_ID", 2, 128);
        std::string mapped = mapNumericStorefrontId(raw_sf);
        if (!mapped.empty()) return mapped;
    }
    return cfg_sf;
}

std::string AdamClient::resolveMediaUserTokenWithFallback() const {
    std::string mut = mediaUserToken();
    if (!mut.empty()) return mut;
    if (const char* env_mut = std::getenv("APPLE_MUSIC_USER_TOKEN"); env_mut && *env_mut) {
        mut = trimCopy(env_mut);
        if (!mut.empty()) return mut;
    }
    return discoverLocalAdamMusicToken();
}

std::string AdamClient::resolveMediaUserToken() const {
    return resolveMediaUserTokenWithFallback();
}

bool AdamClient::localWrapperAuthEnabled() {
    const char* env = std::getenv("OMNIDESK_APPLE_MUSIC_WRAPPER");
    if (!env || !*env) return false;
    std::string val = toLowerCopy(trimCopy(env));
    return val == "1" || val == "true" || val == "yes";
}

std::string AdamClient::ampApiBaseUrl() const {
    std::lock_guard<std::mutex> lk(token_mutex_);
    return amp_api_base_url_;
}

std::string AdamClient::configuredProxy() const {
    std::lock_guard<std::mutex> lk(cfg_mutex_);
    return cfg_.proxy;
}

bool AdamClient::hasUsableAuthForWrite() const {
    if (localWrapperAuthEnabled()) return true;
    return !cachedOrEnvDeveloperToken().empty() && !resolveMediaUserTokenWithFallback().empty();
}

void AdamClient::setDeveloperTokenForTesting(const std::string& token) {
    std::lock_guard<std::mutex> lk(token_mutex_);
    dev_token_ = trimCopy(token);
}

void AdamClient::setAmpApiBaseUrlForTesting(const std::string& base_url) {
    if (!base_url.empty() && !std::regex_match(base_url, std::regex(R"(http://127\.0\.0\.1:[0-9]{1,5})"))) {
        throw std::invalid_argument("Test AMP endpoint must be an HTTP IPv4 loopback port");
    }
    std::lock_guard<std::mutex> lk(token_mutex_);
    amp_api_base_url_ = base_url.empty() ? "https://amp-api.music.apple.com" : base_url;
    while (amp_api_base_url_.size() > 1 && amp_api_base_url_.back() == '/') {
        amp_api_base_url_.pop_back();
    }
}

bool AdamClient::pushTokensToEngine(std::string& error) {
    std::string dev = getDeveloperToken(false);
    std::string mut = resolveMediaUserTokenWithFallback();
    if (dev.empty() && mut.empty()) {
        error = "暂无可推送的 Developer JWT / media-user-token";
        return false;
    }
    nlohmann::json body = nlohmann::json::object();
    if (!dev.empty()) body["developerToken"] = dev;
    if (!mut.empty()) body["musicUserToken"] = mut;
    HttpResponse r = engineRequest("POST", "/api/v1/library/token", body.dump());
    if (!r.ok) {
        error = r.error;
        return false;
    }
    return true;
}

std::string AdamClient::effectiveStorefront(const std::string& override_sf) const {
    if (!override_sf.empty()) return toLowerCopy(override_sf);
    bool sf_explicit = false;
    std::string cfg_sf;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        sf_explicit = cfg_.storefront_explicit;
        cfg_sf = cfg_.storefront;
    }
    if (sf_explicit && !cfg_sf.empty()) {
        return cfg_sf;
    }
    std::string drm_sf = discoverLocalAdamStorefront();
    if (!drm_sf.empty()) {
        return drm_sf;
    }
    return cfg_sf.empty() ? "us" : cfg_sf;
}

DrmStatus AdamClient::getDrmStatus() {
    HttpResponse r = engineRequest("GET", "/api/v1/drm/status");
    if (!r.ok) {
        DrmStatus st;
        st.engine_reachable = false;
        st.error = r.error;
        return st;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (j.is_discarded()) {
        DrmStatus st;
        st.engine_reachable = true;
        st.error = "DRM 状态响应 JSON 无效";
        return st;
    }
    return parseDrmSnapshot(j);
}

bool AdamClient::authenticateDrm(const std::string& email,
                                   const std::string& password,
                                   DrmStatus& status_out,
                                   std::string& error) {
    if (email.empty() || password.empty()) {
        error = "请输入 Apple ID 邮箱与密码";
        return false;
    }
    nlohmann::json body = {{"email", email}, {"password", password}};
    HttpResponse r = engineRequest("POST", "/api/v1/drm/authenticate", body.dump(),
                                   "application/json", "application/json", 12000);
    if (!r.ok) {
        error = r.error;
        return false;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (j.is_object()) status_out = parseDrmSnapshot(j);
    return true;
}

bool AdamClient::submitDrmChallenge(const std::string& code,
                                    DrmStatus& status_out,
                                    std::string& error) {
    if (code.empty()) {
        error = "请输入 6 位双重认证验证码";
        return false;
    }
    nlohmann::json body = {{"reply", trimCopy(code)}};
    HttpResponse r = engineRequest("POST", "/api/v1/drm/challenge", body.dump(),
                                   "application/json", "application/json", 12000);
    if (!r.ok) {
        error = r.error;
        return false;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (j.is_object()) status_out = parseDrmSnapshot(j);
    return true;
}

bool AdamClient::resendDrmChallenge(DrmStatus& status_out, std::string& error) {
    HttpResponse r = engineRequest("POST", "/api/v1/drm/challenge/resend", "{}");
    if (!r.ok) {
        error = r.error;
        return false;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (j.is_object()) status_out = parseDrmSnapshot(j);
    return true;
}

bool AdamClient::logoutDrm(std::string& error) {
    HttpResponse r = engineRequest("POST", "/api/v1/drm/logout", "{}");
    if (!r.ok) {
        error = r.error;
        return false;
    }
    return true;
}

PlaybackSessionInfo AdamClient::createPlaybackSession(const std::string& asset_id,
                                                      const std::string& storefront,
                                                      bool lossless,
                                                      bool hires,
                                                      bool video,
                                                      bool atmos) {
    PlaybackSessionInfo info;
    if (asset_id.empty()) {
        info.error = "缺少曲目 assetId";
        return info;
    }

    std::string dev = getDeveloperToken(false);
    std::string mut = resolveMediaUserTokenWithFallback();
    nlohmann::json req = {
        {"assetId", asset_id},
        {"capabilities", {
            {"lossless", lossless},
            {"hiRes", hires},
            {"video", video},
            {"atmos", atmos},
        }},
    };
    if (!storefront.empty()) req["storefront"] = storefront;
    if (!dev.empty()) req["token"] = dev;
    if (!mut.empty()) req["mediaUserToken"] = mut;

    HttpResponse r = engineRequest("POST", "/api/v1/playback", req.dump(),
                                   "application/json", "application/json", 15000);
    if (!r.ok) {
        info.error = r.error;
        return info;
    }

    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || jStr(j, "sessionId").empty()) {
        info.error = "播放会话响应无效";
        return info;
    }

    uint16_t port = 20025;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        port = static_cast<uint16_t>(cfg_.engine_port);
    }
    std::string base = "https://127.0.0.1:" + std::to_string(port);

    info.ok = true;
    info.session_id = jStr(j, "sessionId");
    info.asset_id = jStr(j, "assetId", asset_id);
    info.storefront = jStr(j, "storefront");
    info.type = jStr(j, "type", video ? "mv" : "song");
    info.codec = jStr(j, "codec");
    info.sample_rate = jInt(j, "sampleRate", 0);
    info.bit_depth = jInt(j, "bitDepth", 0);
    info.bit_rate = jInt(j, "bitRate", 0);
    info.channel_count = jInt(j, "channelCount", 0);
    info.spatial_audio = jStr(j, "spatialAudio");
    info.title = jStr(j, "title");
    info.artist = jStr(j, "artistName");
    info.album = jStr(j, "albumName");
    info.cover_url = formatArtworkUrl(jStr(j, "artworkUrl"), 500);
    info.duration_sec = jInt64(j, "durationMs", 0) / 1000.0;

    std::string audio_rel = "/api/v1/playback/" + info.session_id + "/audio";
    if (j.contains("streams") && j["streams"].is_object()) {
        std::string sa = jStr(j["streams"], "audio");
        if (!sa.empty()) audio_rel = sa;
    }
    info.audio_url = (audio_rel.rfind("http", 0) == 0) ? audio_rel : (base + audio_rel);
    if (info.type == "mv") {
        info.video_url = base + "/api/v1/playback/" + info.session_id + "/vseg/manifest";
    }
    return info;
}

bool AdamClient::deletePlaybackSession(const std::string& session_id) {
    if (session_id.empty()) return true;
    HttpResponse r = engineRequest("DELETE", "/api/v1/playback/" + session_id, "", "application/json", "application/json", 3000);
    return r.ok;
}

bool AdamClient::fetchMetadata(const std::string& asset_id,
                               const std::string& storefront,
                               MusicTrack& out,
                               std::string& error) {
    std::string dummy_err;
    pushTokensToEngine(dummy_err);
    const std::string eff_sf = effectiveStorefront(storefront);
    std::string path = "/api/v1/metadata/" + asset_id;
    if (!eff_sf.empty()) path += "?sf=" + eff_sf;
    HttpResponse r = engineRequest("GET", path);
    if (r.status == 404 && !eff_sf.empty()) {
        const std::string alt_sf = (eff_sf == "cn") ? "us" : "cn";
        r = engineRequest("GET", "/api/v1/metadata/" + asset_id + "?sf=" + alt_sf);
    }
    if (r.ok) {
        auto j = nlohmann::json::parse(r.body, nullptr, false);
        if (j.is_object()) {
            out.id = jStr(j, "id", asset_id);
            out.kind = jStr(j, "type", "song");
            out.title = jStr(j, "title");
            out.artist = jStr(j, "artistName");
            out.album = jStr(j, "albumName");
            out.duration_sec = jInt64(j, "durationMs", 0) / 1000.0;
            out.cover_url = formatArtworkUrl(jStr(j, "artworkUrl"), 500);
            out.has_lyrics = jBool(j, "hasLyrics", false);
            out.storefront = eff_sf;
            if (j.contains("availableStreams") && j["availableStreams"].is_array() &&
                !j["availableStreams"].empty()) {
                const auto& s0 = j["availableStreams"][0];
                std::string c = jStr(s0, "codec");
                int sr = jInt(s0, "sampleRate", 0);
                int bd = jInt(s0, "bitDepth", 0);
                if (bd > 0 && sr > 0) {
                    out.is_lossless = true;
                    out.is_hires = (sr > 48000 || bd > 16);
                    out.quality_label = c + " " + std::to_string(bd) + "bit/" + std::to_string(sr / 1000) + "kHz";
                } else if (!c.empty()) {
                    out.quality_label = c;
                }
            }
            if (!out.title.empty() || !out.cover_url.empty()) {
                error.clear();
                return true;
            }
        }
    } else {
        error = r.error;
    }

    // Fallback to Apple Music AMP catalog API across effective + alternate storefront
    if (allowLocalAdamDrmFallback() || ampApiBaseUrl() != "https://amp-api.music.apple.com" ||
        !cachedOrEnvDeveloperToken().empty()) {
        std::string lang;
        {
            std::lock_guard<std::mutex> lk(cfg_mutex_);
            lang = cfg_.language;
        }
        std::vector<std::string> sfs;
        if (!eff_sf.empty()) sfs.push_back(eff_sf);
        const std::string user_sf = effectiveStorefront();
        if (!user_sf.empty() && std::find(sfs.begin(), sfs.end(), user_sf) == sfs.end()) {
            sfs.push_back(user_sf);
        }
        for (const char* fb : {"cn", "us"}) {
            if (std::find(sfs.begin(), sfs.end(), fb) == sfs.end()) sfs.emplace_back(fb);
        }
        for (const auto& cand_sf : sfs) {
            HttpResponse ar = ampApiGet(
                "/v1/catalog/" + cand_sf + "/songs/" + LrclibClient::urlEncode(asset_id) +
                    "?l=" + LrclibClient::urlEncode(lang),
                false);
            if (!ar.ok) continue;
            auto aj = nlohmann::json::parse(ar.body, nullptr, false);
            if (!aj.is_object() || !aj.contains("data") || !aj["data"].is_array() || aj["data"].empty()) {
                continue;
            }
            std::vector<ContainerItem> dc;
            std::vector<MusicTrack> dt;
            parseResourceNode(aj["data"][0], cand_sf, dc, dt);
            if (!dt.empty()) {
                out = std::move(dt.front());
                error.clear();
                return true;
            }
        }
    }

    if (error.empty()) error = "未找到曲目元数据";
    return false;
}

bool AdamClient::fetchBatchMetadata(const std::vector<std::pair<std::string, std::string>>& id_and_sf,
                                    std::unordered_map<std::string, MusicTrack>& out,
                                    std::string& error) {
    out.clear();
    if (id_and_sf.empty()) return true;

    std::string lang;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        lang = cfg_.language;
    }
    const std::string default_sf = effectiveStorefront();

    std::unordered_map<std::string, std::vector<std::string>> by_sf;
    for (const auto& [raw_id, raw_sf] : id_and_sf) {
        std::string id = trimCopy(raw_id);
        if (id.empty()) continue;
        bool digits = std::all_of(id.begin(), id.end(), [](unsigned char ch) { return std::isdigit(ch) != 0; });
        if (!digits) continue;
        std::string sf = effectiveStorefront(raw_sf);
        if (sf.empty()) sf = default_sf.empty() ? "us" : default_sf;
        auto& vec = by_sf[sf];
        if (std::find(vec.begin(), vec.end(), id) == vec.end()) {
            vec.push_back(std::move(id));
        }
    }
    if (by_sf.empty()) return true;

    auto queryBatchForSf = [&](const std::string& sf, const std::vector<std::string>& ids) {
        for (size_t offset = 0; offset < ids.size(); offset += 50) {
            const size_t end = std::min(ids.size(), offset + 50);
            std::string joined;
            for (size_t i = offset; i < end; ++i) {
                if (!joined.empty()) joined.push_back(',');
                joined += ids[i];
            }
            HttpResponse r = ampApiGet(
                "/v1/catalog/" + sf + "/songs?ids=" + joined + "&l=" + LrclibClient::urlEncode(lang),
                false);
            if (!r.ok) {
                if (error.empty()) error = r.error;
                continue;
            }
            auto j = nlohmann::json::parse(r.body, nullptr, false);
            if (!j.is_object() || !j.contains("data") || !j["data"].is_array()) continue;
            std::vector<ContainerItem> dc;
            std::vector<MusicTrack> dt;
            for (const auto& item : j["data"]) {
                parseResourceNode(item, sf, dc, dt);
            }
            for (auto& tr : dt) {
                if (!tr.id.empty()) {
                    out[tr.id] = std::move(tr);
                }
            }
        }
    };

    const bool can_use_amp =
        !cachedOrEnvDeveloperToken().empty() ||
        allowLocalAdamDrmFallback() ||
        ampApiBaseUrl() != "https://amp-api.music.apple.com";

    if (can_use_amp) {
        for (const auto& [sf, ids] : by_sf) {
            queryBatchForSf(sf, ids);
        }

        // Retry any unresolved IDs against alternate storefront (cn <-> us)
        std::unordered_map<std::string, std::vector<std::string>> retry_by_sf;
        for (const auto& [sf, ids] : by_sf) {
            std::string alt_sf = !default_sf.empty() && default_sf != sf ? default_sf : (sf == "cn" ? "us" : "cn");
            for (const auto& id : ids) {
                if (out.find(id) == out.end()) {
                    auto& vec = retry_by_sf[alt_sf];
                    if (std::find(vec.begin(), vec.end(), id) == vec.end()) {
                        vec.push_back(id);
                    }
                }
            }
        }
        for (const auto& [alt_sf, ids] : retry_by_sf) {
            queryBatchForSf(alt_sf, ids);
        }
    }

    // Fallback to local Go engine metadata endpoint for remaining unresolved items (up to 25)
    size_t engine_fb_count = 0;
    for (const auto& [sf, ids] : by_sf) {
        for (const auto& id : ids) {
            if (out.find(id) != out.end()) continue;
            if (engine_fb_count >= 25) break;
            ++engine_fb_count;
            MusicTrack tr;
            std::string merr;
            if (fetchMetadata(id, sf, tr, merr) && !tr.id.empty()) {
                out[id] = std::move(tr);
            } else if (error.empty() && !merr.empty()) {
                error = merr;
            }
        }
    }

    if (!out.empty()) error.clear();
    return !out.empty() || error.empty();
}

bool AdamClient::fetchNativeLyrics(const std::string& asset_id,
                                   const std::string& storefront,
                                   std::string& lrc_out,
                                   std::string& error) {
    if (asset_id.empty()) {
        error = "empty asset_id";
        return false;
    }
    std::string dummy_err;
    pushTokensToEngine(dummy_err);

    std::string sf = storefront;
    std::string path = "/api/v1/lyrics/" + asset_id + "?format=lrc";
    if (!sf.empty()) path += "&sf=" + sf;

    HttpResponse r = engineRequest("GET", path, "", "application/json", "text/plain");
    if (r.status == 404) {
        std::string fb_sf = (sf == "cn") ? "us" : "cn";
        r = engineRequest("GET", "/api/v1/lyrics/" + asset_id + "?format=lrc&sf=" + fb_sf,
                          "", "application/json", "text/plain");
    }
    if (!r.ok || trimCopy(r.body).empty()) {
        error = r.error.empty() ? "未找到 Adam 原生歌词" : r.error;
        return false;
    }
    lrc_out = r.body;
    return true;
}

bool AdamClient::fetchLibraryStatus(nlohmann::json& out, std::string& error) {
    HttpResponse r = engineRequest("GET", "/api/v1/library/status");
    if (!r.ok) { error = r.error; return false; }
    out = nlohmann::json::parse(r.body, nullptr, false);
    if (!out.is_object()) { error = "library status JSON invalid"; return false; }
    return true;
}

std::vector<ContainerItem> AdamClient::fetchLibraryPlaylists(std::string& error) {
    std::vector<ContainerItem> out;
    HttpResponse r = engineRequest("GET", "/api/v1/library/playlists");
    if (r.ok) {
        auto j = nlohmann::json::parse(r.body, nullptr, false);
        if (j.is_object() && j.contains("playlists") && j["playlists"].is_array()) {
            for (const auto& p : j["playlists"]) {
                if (!p.is_object()) continue;
                ContainerItem c;
                c.id = p.value("lid", "");
                c.type = "library-playlists";
                c.name = p.value("name", c.id);
                c.track_count = p.value("trackCount", 0);
                c.cover_url = formatArtworkUrl(p.value("artworkUrl", ""), 500);
                c.subtitle = c.track_count > 0
                                 ? (std::to_string(c.track_count) + " 首歌曲")
                                 : "Apple Music 资料库歌单";
                if (!c.id.empty()) out.push_back(std::move(c));
            }
        }
    } else {
        error = r.error;
    }

    const bool all_missing_covers = !out.empty() &&
        std::all_of(out.begin(), out.end(), [](const ContainerItem& c) { return c.cover_url.empty(); });
    const bool can_enrich_from_amp =
        allowLocalAdamDrmFallback() || ampApiBaseUrl() != "https://amp-api.music.apple.com";

    if ((out.empty() || (all_missing_covers && can_enrich_from_amp)) &&
        !resolveMediaUserTokenWithFallback().empty()) {
        if (allowLocalAdamDrmFallback() && cachedOrEnvDeveloperToken().empty()) {
            getDeveloperToken(false);
        }
        if (!cachedOrEnvDeveloperToken().empty()) {
            std::string lang;
            {
                std::lock_guard<std::mutex> lk(cfg_mutex_);
                lang = cfg_.language;
            }
            const std::string eff_sf = effectiveStorefront();
            std::string next_pl = "/v1/me/library/playlists?limit=100&l=" + LrclibClient::urlEncode(lang);
            std::vector<ContainerItem> amp_playlists;
            std::vector<MusicTrack> dummy_tracks;
            for (int page = 0; page < 3 && !next_pl.empty(); ++page) {
                HttpResponse ar = ampApiGet(next_pl, true);
                if (!ar.ok) {
                    if (out.empty() && error.empty()) error = ar.error;
                    break;
                }
                error.clear();
                auto aj = nlohmann::json::parse(ar.body, nullptr, false);
                if (!aj.is_object() || !aj.contains("data") || !aj["data"].is_array()) break;
                for (const auto& item : aj["data"]) {
                    size_t before = amp_playlists.size();
                    parseResourceNode(item, eff_sf, amp_playlists, dummy_tracks);
                    if (amp_playlists.size() > before && amp_playlists.back().subtitle.empty()) {
                        amp_playlists.back().subtitle =
                            amp_playlists.back().track_count > 0
                                ? (std::to_string(amp_playlists.back().track_count) + " 首歌曲")
                                : "Apple Music 资料库歌单";
                    }
                }
                next_pl = aj.value("next", "");
            }
            if (!amp_playlists.empty()) {
                if (out.empty()) {
                    out = std::move(amp_playlists);
                } else {
                    std::unordered_map<std::string, ContainerItem> by_id;
                    for (auto& ap : amp_playlists) by_id[ap.id] = std::move(ap);
                    for (auto& c : out) {
                        auto it = by_id.find(c.id);
                        if (it != by_id.end()) {
                            if (c.cover_url.empty() && !it->second.cover_url.empty()) {
                                c.cover_url = it->second.cover_url;
                            }
                            if (c.track_count <= 0 && it->second.track_count > 0) {
                                c.track_count = it->second.track_count;
                                c.subtitle = std::to_string(c.track_count) + " 首歌曲";
                            }
                        }
                    }
                }
            }
        }
    }
    return out;
}

std::vector<MusicTrack> AdamClient::fetchLibraryPlaylistTracks(const std::string& playlist_id, std::string& error) {
    std::string dummy_err;
    pushTokensToEngine(dummy_err);
    std::vector<MusicTrack> out;

    if (!playlist_id.empty() && !resolveMediaUserTokenWithFallback().empty()) {
        if (allowLocalAdamDrmFallback() && cachedOrEnvDeveloperToken().empty()) {
            getDeveloperToken(false);
        }
        if (!cachedOrEnvDeveloperToken().empty()) {
            std::string lang;
            {
                std::lock_guard<std::mutex> lk(cfg_mutex_);
                lang = cfg_.language;
            }
            const std::string eff_sf = effectiveStorefront();
            std::string next_url = "/v1/me/library/playlists/" + LrclibClient::urlEncode(playlist_id) +
                                   "/tracks?limit=100&l=" + LrclibClient::urlEncode(lang);
            std::vector<ContainerItem> dummy_containers;
            bool amp_ok = false;
            for (int page = 0; page < 5 && !next_url.empty(); ++page) {
                HttpResponse ar = ampApiGet(next_url, true);
                if (!ar.ok) break;
                amp_ok = true;
                auto aj = nlohmann::json::parse(ar.body, nullptr, false);
                if (!aj.is_object() || !aj.contains("data") || !aj["data"].is_array()) break;
                for (const auto& item : aj["data"]) {
                    size_t before = out.size();
                    parseResourceNode(item, eff_sf, dummy_containers, out);
                    if (out.size() > before && out.back().quality_label.empty()) {
                        out.back().quality_label = "ALAC Lossless";
                    }
                }
                next_url = aj.value("next", "");
            }
            if (amp_ok) {
                error.clear();
                return out;
            }
        }
    }

    HttpResponse r = engineRequest("GET", "/api/v1/library/playlists/" + playlist_id + "/tracks");
    if (!r.ok) { error = r.error; return out; }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || !j.contains("tracks") || !j["tracks"].is_array()) return out;
    for (const auto& t : j["tracks"]) {
        if (!t.is_object()) continue;
        MusicTrack tr;
        tr.library_id = t.value("lid", "");
        tr.id = t.value("cid", "");
        if (tr.id.empty()) tr.id = tr.library_id;
        tr.title = t.value("name", tr.id);
        tr.artist = t.value("artist", "资料库曲目");
        tr.album = t.value("album", "");
        tr.duration_sec = t.value("ms", 0) / 1000.0;
        tr.quality_label = "资料库";
        if (!tr.id.empty()) out.push_back(std::move(tr));
    }
    return out;
}

std::vector<MusicTrack> AdamClient::fetchLibraryAlbumTracks(const std::string& album_id, std::string& error) {
    std::vector<MusicTrack> out;
    HttpResponse r = engineRequest("GET", "/api/v1/library/albums/" + album_id + "/tracks");
    if (!r.ok) { error = r.error; return out; }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || !j.contains("tracks") || !j["tracks"].is_array()) return out;
    for (const auto& t : j["tracks"]) {
        if (!t.is_object()) continue;
        MusicTrack tr;
        tr.library_id = t.value("lid", "");
        tr.id = t.value("cid", "");
        if (tr.id.empty()) tr.id = tr.library_id;
        tr.title = t.value("name", tr.id);
        tr.artist = t.value("artist", "资料库曲目");
        tr.album = t.value("album", "");
        tr.duration_sec = t.value("ms", 0) / 1000.0;
        tr.quality_label = "资料库";
        if (!tr.id.empty()) out.push_back(std::move(tr));
    }
    return out;
}

HttpResponse AdamClient::ampApiRequest(const std::string& method,
                                       const std::string& path_and_query,
                                       const std::string& json_body,
                                       bool require_mut,
                                       const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    HttpResponse res;
    if (cancel && cancel->isCancelled()) {
        res.cancelled = true;
        res.error = "cancelled";
        return res;
    }
    if (path_and_query.rfind("/v1/", 0) != 0 ||
        path_and_query.find_first_of("\r\n#\\") != std::string::npos ||
        path_and_query.find("..") != std::string::npos) {
        res.error = "Invalid AMP API path";
        return res;
    }
    std::string mut = resolveMediaUserTokenWithFallback();
    if (require_mut && mut.empty()) {
        res.error = "auth_required: Music-User-Token is not configured";
        return res;
    }
    std::string dev = cachedOrEnvDeveloperToken();
    if (dev.empty() && !require_mut) dev = getDeveloperToken(false);
    if (dev.empty()) {
        res.error = "auth_required: Apple Music developer token unavailable";
        return res;
    }

    ensureCurlInit();
    std::string proxy;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        proxy = cfg_.proxy;
    }
    std::string base_url;
    {
        std::lock_guard<std::mutex> lk(token_mutex_);
        base_url = amp_api_base_url_;
    }
    if (base_url.empty()) base_url = "https://amp-api.music.apple.com";

    std::string url = base_url + path_and_query;
    CURL* curl = curl_easy_init();
    if (!curl) { res.error = "curl_easy_init failed"; return res; }

    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, ("Authorization: Bearer " + dev).c_str());
    headers = curl_slist_append(headers, "Origin: https://music.apple.com");
    headers = curl_slist_append(headers, "Accept: application/json");
    if (method == "POST" || !json_body.empty()) {
        headers = curl_slist_append(headers, "Content-Type: application/json");
    }
    if (!mut.empty()) {
        headers = curl_slist_append(headers, ("Media-User-Token: " + mut).c_str());
        headers = curl_slist_append(headers, ("Music-User-Token: " + mut).c_str());
        if (base_url.find("amp-api.music.apple.com") != std::string::npos) {
            headers = curl_slist_append(headers, ("Cookie: media-user-token=" + mut).c_str());
        }
    }

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteCb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &res.body);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 12000L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION,
        +[](void* context, curl_off_t, curl_off_t, curl_off_t, curl_off_t) -> int {
            const auto* token = static_cast<const omnidesk::CancelToken*>(context);
            return token && token->isCancelled() ? 1 : 0;
        });
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, cancel.get());
    curl_easy_setopt(curl, CURLOPT_USERAGENT,
                     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36");
    if (method == "POST") {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_body.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(json_body.size()));
    } else if (method != "GET") {
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method.c_str());
        if (!json_body.empty()) {
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_body.c_str());
            curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(json_body.size()));
        }
    }
    if (base_url.rfind("http://127.0.0.1:", 0) == 0) {
        curl_easy_setopt(curl, CURLOPT_PROXY, "");
    } else if (!proxy.empty()) {
        curl_easy_setopt(curl, CURLOPT_PROXY, proxy.c_str());
    }

    CURLcode rc = curl_easy_perform(curl);
    long code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    res.status = static_cast<int>(code);
    res.cancelled = cancel && cancel->isCancelled();
    res.ok = (rc == CURLE_OK && code >= 200 && code < 300 && !res.cancelled);
    if (!res.ok) {
        res.error = res.cancelled ? "cancelled" :
            ((rc != CURLE_OK) ? curl_easy_strerror(rc) : ("AMP API HTTP " + std::to_string(code)));
    }
    return res;
}

HttpResponse AdamClient::ampApiGet(const std::string& path_and_query, bool require_mut,
                                   const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    return ampApiRequest("GET", path_and_query, "", require_mut, cancel);
}

HttpResponse AdamClient::ampApiPost(const std::string& path_and_query,
                                    const std::string& json_body,
                                    bool require_mut,
                                    const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    return ampApiRequest("POST", path_and_query, json_body, require_mut, cancel);
}

bool AdamClient::syncLibraryFromApple(nlohmann::json& stats_out, std::string& error) {
    nlohmann::json playlists = nlohmann::json::array();
    nlohmann::json songs = nlohmann::json::array();
    nlohmann::json albums = nlohmann::json::array();

    if (allowLocalAdamDrmFallback() && cachedOrEnvDeveloperToken().empty()) {
        getDeveloperToken(false);
    }

    std::string lang;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        lang = cfg_.language;
    }

    // Fetch playlists (up to 3 pages of 100)
    std::string next_pl = "/v1/me/library/playlists?limit=100&l=" + LrclibClient::urlEncode(lang);
    for (int page = 0; page < 3 && !next_pl.empty(); ++page) {
        HttpResponse r = ampApiGet(next_pl, true);
        if (!r.ok) { error = r.error; return false; }
        auto j = nlohmann::json::parse(r.body, nullptr, false);
        if (!j.is_object() || !j.contains("data") || !j["data"].is_array()) break;
        for (const auto& item : j["data"]) {
            if (!item.is_object()) continue;
            const auto& attr = (item.contains("attributes") && item["attributes"].is_object())
                                   ? item["attributes"]
                                   : nlohmann::json::object();
            playlists.push_back({
                {"lid", jStr(item, "id")},
                {"name", jStr(attr, "name")},
                {"trackCount", jInt(attr, "trackCount", 0)},
            });
        }
        next_pl = jStr(j, "next");
    }

    // Fetch library songs (up to 5 pages of 100)
    std::string next_songs = "/v1/me/library/songs?limit=100&l=" + LrclibClient::urlEncode(lang);
    for (int page = 0; page < 5 && !next_songs.empty(); ++page) {
        HttpResponse r = ampApiGet(next_songs, true);
        if (!r.ok) break;
        auto j = nlohmann::json::parse(r.body, nullptr, false);
        if (!j.is_object() || !j.contains("data") || !j["data"].is_array()) break;
        for (const auto& item : j["data"]) {
            if (!item.is_object()) continue;
            const auto& attr = (item.contains("attributes") && item["attributes"].is_object())
                                   ? item["attributes"]
                                   : nlohmann::json::object();
            std::string cid;
            if (attr.contains("playParams") && attr["playParams"].is_object()) {
                cid = jStr(attr["playParams"], "catalogId");
            }
            songs.push_back({
                {"lid", jStr(item, "id")},
                {"cid", cid},
                {"name", jStr(attr, "name")},
                {"artist", jStr(attr, "artistName")},
                {"album", jStr(attr, "albumName")},
                {"ms", jInt64(attr, "durationInMillis", 0)},
                {"track", jInt(attr, "trackNumber", 0)},
                {"disc", jInt(attr, "discNumber", 0)},
            });
        }
        next_songs = jStr(j, "next");
    }

    nlohmann::json ingest = {
        {"playlists", std::move(playlists)},
        {"songs", std::move(songs)},
        {"albums", std::move(albums)},
    };
    HttpResponse ir = engineRequest("POST", "/api/v1/library/ingest", ingest.dump(),
                                    "application/json", "application/json", 15000);
    if (!ir.ok) {
        error = ir.error;
        return false;
    }
    stats_out = nlohmann::json::parse(ir.body, nullptr, false);
    return true;
}

void AdamClient::parseResourceNode(const nlohmann::json& node,
                                   const std::string& default_sf,
                                   std::vector<ContainerItem>& containers,
                                   std::vector<MusicTrack>& tracks) {
    if (!node.is_object()) return;
    std::string type = jStr(node, "type");
    std::string id = jStr(node, "id");
    const auto attr = (node.contains("attributes") && node["attributes"].is_object())
                          ? node["attributes"]
                          : nlohmann::json::object();

    if (type == "songs" || type == "music-videos" || type == "library-songs") {
        MusicTrack tr;
        tr.id = id;
        if (attr.contains("playParams") && attr["playParams"].is_object()) {
            std::string cid = jStr(attr["playParams"], "catalogId");
            if (!cid.empty()) {
                tr.library_id = id;
                tr.id = cid;
            }
        }
        tr.kind = (type == "music-videos") ? "mv" : "song";
        tr.title = jStr(attr, "name");
        tr.artist = jStr(attr, "artistName");
        tr.album = jStr(attr, "albumName");
        tr.duration_sec = jInt64(attr, "durationInMillis", 0) / 1000.0;
        tr.has_lyrics = jBool(attr, "hasLyrics", false);
        tr.storefront = default_sf;
        if (attr.contains("artwork") && attr["artwork"].is_object()) {
            tr.cover_url = formatArtworkUrl(jStr(attr["artwork"], "url"), 500);
        }
        if (attr.contains("audioTraits")) applyAudioTraits(attr["audioTraits"], tr);
        if (tr.kind == "mv") tr.quality_label = "MV";
        if (!tr.id.empty() && !tr.title.empty()) tracks.push_back(std::move(tr));
        return;
    }

    if (type == "albums" || type == "playlists" || type == "library-playlists" ||
        type == "library-albums" || type == "artists") {
        ContainerItem c;
        c.id = id;
        c.type = type;
        c.name = jStr(attr, "name");
        c.subtitle = jStr(attr, "artistName", jStr(attr, "curatorName"));
        if (c.subtitle.empty() && attr.contains("genreNames") && attr["genreNames"].is_array() &&
            !attr["genreNames"].empty() && attr["genreNames"][0].is_string()) {
            c.subtitle = attr["genreNames"][0].get<std::string>();
        }
        c.track_count = jInt(attr, "trackCount", 0);
        c.storefront = default_sf;
        if (attr.contains("artwork") && attr["artwork"].is_object()) {
            c.cover_url = formatArtworkUrl(jStr(attr["artwork"], "url"), 500);
        }
        if (!c.id.empty() && !c.name.empty()) containers.push_back(std::move(c));
        return;
    }

    // Personal recommendation containers wrap nested albums/playlists/songs
    if (node.contains("relationships") && node["relationships"].is_object()) {
        const auto& rels = node["relationships"];
        for (const char* rel_key : {"contents", "tracks"}) {
            if (rels.contains(rel_key) && rels[rel_key].is_object() &&
                rels[rel_key].contains("data") && rels[rel_key]["data"].is_array()) {
                for (const auto& child : rels[rel_key]["data"]) {
                    parseResourceNode(child, default_sf, containers, tracks);
                }
            }
        }
    }
}

FeedOutcome AdamClient::fetchRecommendations(const std::string& kind, int limit) {
    FeedOutcome out;
    out.feed_id = kind;

    std::string lang;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        lang = cfg_.language;
    }
    const std::string sf = effectiveStorefront();

    auto deduplicateOutcome = [](FeedOutcome& fo) {
        std::unordered_set<std::string> seen_c;
        std::vector<ContainerItem> uniq_c;
        uniq_c.reserve(fo.containers.size());
        for (auto& c : fo.containers) {
            std::string key = c.type + ":" + c.id;
            if (seen_c.insert(std::move(key)).second) {
                uniq_c.push_back(std::move(c));
            }
        }
        fo.containers = std::move(uniq_c);

        std::unordered_set<std::string> seen_t;
        std::vector<MusicTrack> uniq_t;
        uniq_t.reserve(fo.tracks.size());
        for (auto& t : fo.tracks) {
            if (seen_t.insert(t.id).second) {
                if (t.quality_label.empty() && t.kind != "mv") {
                    t.quality_label = "ALAC Lossless";
                }
                uniq_t.push_back(std::move(t));
            }
        }
        fo.tracks = std::move(uniq_t);
    };

    if (kind == "recently-played-tracks") {
        const int eff_limit = std::clamp(limit, 1, 30);
        if (allowLocalAdamDrmFallback() && cachedOrEnvDeveloperToken().empty()) {
            getDeveloperToken(false);
        }
        HttpResponse ar = ampApiGet(
            "/v1/me/recent/played/tracks?limit=" + std::to_string(eff_limit) +
                "&l=" + LrclibClient::urlEncode(lang),
            true);
        if (!ar.ok) {
            out.error = ar.error;
            return out;
        }
        auto aj = nlohmann::json::parse(ar.body, nullptr, false);
        if (!aj.is_object() || !aj.contains("data") || !aj["data"].is_array()) {
            out.error = "最近播放曲目响应格式无效";
            return out;
        }
        for (const auto& item : aj["data"]) {
            parseResourceNode(item, sf, out.containers, out.tracks);
        }
        deduplicateOutcome(out);
        out.ok = true;
        return out;
    }

    std::string dummy_err;
    pushTokensToEngine(dummy_err);

    // Apple Music AMP API enforces limit <= 10 on heavy-rotation and recent/played
    int eff_limit = std::clamp(limit, 1, 15);
    std::string path = "/api/v1/recommendations";
    if (kind == "heavy-rotation") {
        path = "/api/v1/recommendations/heavy-rotation";
        eff_limit = std::clamp(limit, 1, 10);
    } else if (kind == "recently-played") {
        path = "/api/v1/recommendations/recently-played";
        eff_limit = std::clamp(limit, 1, 10);
    }
    path += "?limit=" + std::to_string(eff_limit);

    HttpResponse r = engineRequest("GET", path, "", "application/json", "application/json", 25000);
    if (!r.ok && !resolveMediaUserTokenWithFallback().empty()) {
        if (allowLocalAdamDrmFallback() && cachedOrEnvDeveloperToken().empty()) {
            getDeveloperToken(false);
        }
        if (!cachedOrEnvDeveloperToken().empty()) {
            std::string amp_path = "/v1/me/recommendations?limit=" + std::to_string(eff_limit) +
                                   "&l=" + LrclibClient::urlEncode(lang);
            if (kind == "heavy-rotation") {
                amp_path = "/v1/me/history/heavy-rotation?limit=" + std::to_string(eff_limit) +
                           "&l=" + LrclibClient::urlEncode(lang);
            } else if (kind == "recently-played") {
                amp_path = "/v1/me/recent/played?limit=" + std::to_string(eff_limit) +
                           "&l=" + LrclibClient::urlEncode(lang);
            }
            HttpResponse ar = ampApiGet(amp_path, true);
            if (ar.ok) {
                r = std::move(ar);
            }
        }
    }
    if (!r.ok) {
        out.error = r.error;
        return out;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || !j.contains("data") || !j["data"].is_array()) {
        out.error = "推荐响应格式无效";
        return out;
    }
    for (const auto& item : j["data"]) {
        parseResourceNode(item, sf, out.containers, out.tracks);
    }
    deduplicateOutcome(out);
    out.ok = true;
    return out;
}

bool AdamClient::fetchCatalogAlbum(const std::string& album_id,
                                   const std::string& sf,
                                   ContainerItem& album_out,
                                   std::vector<MusicTrack>& tracks_out,
                                   std::string& error) {
    std::string dummy_err;
    pushTokensToEngine(dummy_err);
    std::string eff_sf = effectiveStorefront(sf);
    std::string lang;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        lang = cfg_.language;
    }
    HttpResponse r = engineRequest("GET", "/api/v1/catalog/albums/" + album_id + "?sf=" + eff_sf);
    if (!r.ok && (allowLocalAdamDrmFallback() || !cachedOrEnvDeveloperToken().empty())) {
        HttpResponse ar = ampApiGet(
            "/v1/catalog/" + eff_sf + "/albums/" + LrclibClient::urlEncode(album_id) +
                "?include=tracks&l=" + LrclibClient::urlEncode(lang),
            false);
        if (ar.ok) r = std::move(ar);
    }
    if (!r.ok) { error = r.error; return false; }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || !j.contains("data") || !j["data"].is_array() || j["data"].empty()) {
        error = "专辑数据为空";
        return false;
    }
    std::vector<ContainerItem> c_tmp;
    parseResourceNode(j["data"][0], eff_sf, c_tmp, tracks_out);
    if (!c_tmp.empty()) album_out = c_tmp.front();
    const auto& d0 = j["data"][0];
    if (d0.contains("relationships") && d0["relationships"].is_object() &&
        d0["relationships"].contains("tracks") && d0["relationships"]["tracks"].is_object() &&
        d0["relationships"]["tracks"].contains("data") && d0["relationships"]["tracks"]["data"].is_array()) {
        for (const auto& tr_node : d0["relationships"]["tracks"]["data"]) {
            parseResourceNode(tr_node, eff_sf, c_tmp, tracks_out);
        }
    }
    for (auto& tr : tracks_out) {
        if (tr.cover_url.empty() && !album_out.cover_url.empty()) tr.cover_url = album_out.cover_url;
    }
    return true;
}

bool AdamClient::fetchCatalogPlaylist(const std::string& playlist_id,
                                      const std::string& sf,
                                      ContainerItem& pl_out,
                                      std::vector<MusicTrack>& tracks_out,
                                      std::string& error) {
    std::string dummy_err;
    pushTokensToEngine(dummy_err);
    std::string eff_sf = effectiveStorefront(sf);
    std::string lang;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        lang = cfg_.language;
    }

    const bool is_personal_mix = (playlist_id.rfind("pl.pm-", 0) == 0);
    HttpResponse r;
    if (is_personal_mix && (allowLocalAdamDrmFallback() || !cachedOrEnvDeveloperToken().empty())) {
        // Go engine's /api/v1/catalog/playlists omits Music-User-Token, which breaks personal mixes.
        r = ampApiGet(
            "/v1/catalog/" + eff_sf + "/playlists/" + LrclibClient::urlEncode(playlist_id) +
                "?include=tracks&l=" + LrclibClient::urlEncode(lang),
            false);
    }
    if (!r.ok) {
        r = engineRequest("GET", "/api/v1/catalog/playlists/" + playlist_id + "?sf=" + eff_sf);
    }
    if (!r.ok && (allowLocalAdamDrmFallback() || !cachedOrEnvDeveloperToken().empty())) {
        HttpResponse ar = ampApiGet(
            "/v1/catalog/" + eff_sf + "/playlists/" + LrclibClient::urlEncode(playlist_id) +
                "?include=tracks&l=" + LrclibClient::urlEncode(lang),
            false);
        if (ar.ok) r = std::move(ar);
    }
    if (!r.ok) { error = r.error; return false; }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || !j.contains("data") || !j["data"].is_array() || j["data"].empty()) {
        error = "歌单数据为空";
        return false;
    }
    std::vector<ContainerItem> c_tmp;
    parseResourceNode(j["data"][0], eff_sf, c_tmp, tracks_out);
    if (!c_tmp.empty()) pl_out = c_tmp.front();
    const auto& d0 = j["data"][0];
    if (d0.contains("relationships") && d0["relationships"].is_object() &&
        d0["relationships"].contains("tracks") && d0["relationships"]["tracks"].is_object() &&
        d0["relationships"]["tracks"].contains("data") && d0["relationships"]["tracks"]["data"].is_array()) {
        for (const auto& tr_node : d0["relationships"]["tracks"]["data"]) {
            parseResourceNode(tr_node, eff_sf, c_tmp, tracks_out);
        }
    }
    if (pl_out.cover_url.empty() && !tracks_out.empty() && !tracks_out.front().cover_url.empty()) {
        pl_out.cover_url = tracks_out.front().cover_url;
    }
    return true;
}

bool AdamClient::fetchCatalogArtist(const std::string& artist_id,
                                    const std::string& sf,
                                    ContainerItem& artist_out,
                                    std::vector<ContainerItem>& albums_out,
                                    std::vector<MusicTrack>& top_songs_out,
                                    std::string& error) {
    std::string dummy_err;
    pushTokensToEngine(dummy_err);
    std::string eff_sf = effectiveStorefront(sf);
    std::string lang;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        lang = cfg_.language;
    }
    HttpResponse r = engineRequest("GET", "/api/v1/catalog/artists/" + artist_id + "?sf=" + eff_sf);
    if (!r.ok && (allowLocalAdamDrmFallback() || !cachedOrEnvDeveloperToken().empty())) {
        HttpResponse ar = ampApiGet(
            "/v1/catalog/" + eff_sf + "/artists/" + LrclibClient::urlEncode(artist_id) +
                "?include=albums,songs&l=" + LrclibClient::urlEncode(lang),
            false);
        if (ar.ok) r = std::move(ar);
    }
    if (!r.ok) { error = r.error; return false; }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || !j.contains("data") || !j["data"].is_array() || j["data"].empty()) {
        error = "艺人数据为空";
        return false;
    }
    std::vector<ContainerItem> c_tmp;
    parseResourceNode(j["data"][0], eff_sf, c_tmp, top_songs_out);
    if (!c_tmp.empty()) artist_out = c_tmp.front();
    const auto& d0 = j["data"][0];
    if (d0.contains("relationships") && d0["relationships"].is_object()) {
        const auto& rels = d0["relationships"];
        if (rels.contains("albums") && rels["albums"].contains("data")) {
            for (const auto& a : rels["albums"]["data"]) {
                parseResourceNode(a, eff_sf, albums_out, top_songs_out);
            }
        }
        if (rels.contains("songs") && rels["songs"].contains("data")) {
            for (const auto& s : rels["songs"]["data"]) {
                parseResourceNode(s, eff_sf, albums_out, top_songs_out);
            }
        }
    }
    return true;
}

FeedOutcome AdamClient::searchCatalog(const std::string& query,
                                      const std::string& sf,
                                      const std::string& types) {
    FeedOutcome out;
    out.feed_id = "search";
    if (trimCopy(query).empty()) {
        out.ok = true;
        return out;
    }
    std::string eff_sf = effectiveStorefront(sf);
    std::string lang;
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        lang = cfg_.language;
    }
    std::string path = "/v1/catalog/" + eff_sf + "/search?term=" + LrclibClient::urlEncode(query) +
                       "&types=" + LrclibClient::urlEncode(types) +
                       "&limit=15&l=" + LrclibClient::urlEncode(lang);
    HttpResponse r = ampApiGet(path, false);
    if (!r.ok) {
        out.error = r.error;
        return out;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || !j.contains("results") || !j["results"].is_object()) {
        out.error = "Apple Music 搜索响应格式无效";
        return out;
    }
    const auto& res = j["results"];
    for (const char* key : {"songs", "music-videos", "albums", "playlists", "artists"}) {
        if (res.contains(key) && res[key].is_object() &&
            res[key].contains("data") && res[key]["data"].is_array()) {
            for (const auto& item : res[key]["data"]) {
                parseResourceNode(item, eff_sf, out.containers, out.tracks);
            }
        }
    }
    out.ok = true;
    return out;
}

namespace {

bool isNumericSongId(const std::string& s) {
    if (s.empty()) return false;
    for (char ch : s) {
        if (!std::isdigit(static_cast<unsigned char>(ch))) return false;
    }
    return true;
}

bool isSafePlaylistId(const std::string& s) {
    if (s.empty()) return false;
    for (char ch : s) {
        unsigned char uch = static_cast<unsigned char>(ch);
        if (!std::isalnum(uch) && ch != '.' && ch != '_' && ch != '-') return false;
    }
    return true;
}

std::string safeJsonString(const nlohmann::json& j, const char* key, const std::string& def = "") {
    if (!j.is_object() || !j.contains(key)) return def;
    const auto& v = j[key];
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    return def;
}

} // namespace

bool AdamClient::preflightCatalogSongs(const std::vector<std::string>& track_ids,
                                       const std::string& storefront,
                                       std::vector<MusicTrack>& verified_tracks_out,
                                       std::vector<std::string>& unplayable_ids_out,
                                       std::string& error,
                                       bool,
                                       const std::string&,
                                       const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    verified_tracks_out.clear();
    unplayable_ids_out = track_ids;
    error.clear();
    const std::string sf = toLowerCopy(storefront.empty() ? "cn" : storefront);
    if (sf.size() != 2 || !std::all_of(sf.begin(), sf.end(), [](char letter) {
            return letter >= 'a' && letter <= 'z';
        }) || !std::all_of(track_ids.begin(), track_ids.end(), isNumericSongId)) {
        error = "Invalid storefront or numeric catalog song ID";
        return false;
    }
    if (track_ids.empty()) return true;
    if (cachedOrEnvDeveloperToken().empty()) {
        error = "auth_required: Developer token required for live catalog verification";
        return false;
    }
    std::unordered_map<std::string, MusicTrack> found;
    try {
        for (size_t offset = 0; offset < track_ids.size(); offset += 100) {
            std::string ids;
            for (size_t index = offset; index < std::min(track_ids.size(), offset + 100); ++index) {
                if (!ids.empty()) ids += ",";
                ids += track_ids[index];
            }
            auto response = ampApiGet("/v1/catalog/" + sf + "/songs?ids=" + ids, false, cancel);
            if (!response.ok) {
                error = response.error;
                return false;
            }
            auto body = nlohmann::json::parse(response.body);
            if (!body.is_object() || body.contains("errors") ||
                !body.contains("data") || !body["data"].is_array()) {
                error = "Invalid live catalog response";
                return false;
            }
            for (const auto& node : body["data"]) {
                if (!node.is_object() || node.value("type", "") != "songs" ||
                    !node.contains("attributes") || !node["attributes"].is_object()) continue;
                const auto& attributes = node["attributes"];
                if (!attributes.contains("playParams") || !attributes["playParams"].is_object() ||
                    attributes["playParams"].value("id", "") != node.value("id", "") ||
                    attributes["playParams"].value("kind", "") != "song" ||
                    !attributes.value("isStreamable", true)) continue;
                std::vector<ContainerItem> containers;
                std::vector<MusicTrack> tracks;
                parseResourceNode(node, sf, containers, tracks);
                for (auto& track : tracks) found[track.id] = std::move(track);
            }
        }
    } catch (const nlohmann::json::exception&) {
        error = "Malformed live catalog response";
        return false;
    }
    if (cancel && cancel->isCancelled()) {
        error = "cancelled";
        return false;
    }
    unplayable_ids_out.clear();
    for (const auto& id : track_ids) {
        auto track = found.find(id);
        if (track == found.end()) unplayable_ids_out.push_back(id);
        else verified_tracks_out.push_back(track->second);
    }
    return true;
}

bool AdamClient::appendLibraryPlaylistTracks(const std::string& playlist_id,
                                             const std::vector<std::string>& song_ids,
                                             std::string& error,
                                             const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    error.clear();
    if (!isSafePlaylistId(playlist_id)) {
        error = "Invalid Apple Music library playlist ID";
        return false;
    }
    std::vector<std::string> valid_ids;
    for (const auto& raw : song_ids) {
        std::string id = trimCopy(raw);
        if (!isNumericSongId(id)) {
            error = "Invalid catalog song ID: " + raw;
            return false;
        }
        valid_ids.push_back(id);
    }
    if (valid_ids.empty()) return true;

    for (size_t i = 0; i < valid_ids.size(); i += 25) {
        size_t end = std::min(valid_ids.size(), i + 25);
        nlohmann::json data_arr = nlohmann::json::array();
        for (size_t j = i; j < end; ++j) {
            data_arr.push_back({{"id", valid_ids[j]}, {"type", "songs"}});
        }
        nlohmann::json body = {{"data", std::move(data_arr)}};
        HttpResponse r = ampApiPost("/v1/me/library/playlists/" + playlist_id + "/tracks", body.dump(), true, cancel);
        if (r.ok && !r.body.empty()) {
            auto response = nlohmann::json::parse(r.body, nullptr, false);
            if (!response.is_object() || response.contains("errors")) {
                r.ok = false;
                r.error = "Invalid append response; reconcile by live readback before retrying";
            }
        }
        if (!r.ok) {
            error = r.error.empty() ? "Failed to append tracks to Apple Music playlist" : r.error;
            return false;
        }
    }
    return true;
}

bool AdamClient::createLibraryPlaylist(const std::string& name,
                                       const std::string& description,
                                       const std::vector<std::string>& song_ids,
                                       std::string& playlist_id_out,
                                       std::string& error,
                                       const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    playlist_id_out.clear();
    error.clear();
    if (trimCopy(name).empty()) {
        error = "Playlist name cannot be empty";
        return false;
    }
    for (const auto& raw : song_ids) {
        if (!isNumericSongId(trimCopy(raw))) {
            error = "Invalid catalog song ID: " + raw;
            return false;
        }
    }

    nlohmann::json body = {
        {"attributes", {
            {"name", name},
            {"description", description},
        }},
    };
    size_t initial_count = std::min<size_t>(song_ids.size(), 25);
    if (initial_count > 0) {
        nlohmann::json tracks_data = nlohmann::json::array();
        for (size_t i = 0; i < initial_count; ++i) {
            tracks_data.push_back({{"id", trimCopy(song_ids[i])}, {"type", "songs"}});
        }
        body["relationships"] = {{"tracks", {{"data", std::move(tracks_data)}}}};
    }

    HttpResponse r = ampApiPost("/v1/me/library/playlists", body.dump(), true, cancel);
    if (!r.ok) {
        error = r.error.empty() ? "Failed to create Apple Music library playlist" : r.error;
        return false;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object() || j.contains("errors") || !j.contains("data") || !j["data"].is_array() || j["data"].empty() ||
        !j["data"][0].is_object()) {
        error = "Invalid Apple Music create playlist response";
        return false;
    }
    playlist_id_out = safeJsonString(j["data"][0], "id");
    if (!isSafePlaylistId(playlist_id_out)) {
        error = "Apple Music create playlist response missing playlist id";
        return false;
    }
    if (song_ids.size() > initial_count) {
        std::vector<std::string> rest(song_ids.begin() + static_cast<ptrdiff_t>(initial_count), song_ids.end());
        return appendLibraryPlaylistTracks(playlist_id_out, rest, error, cancel);
    }
    return true;
}

bool AdamClient::fetchLibraryPlaylistCatalogIdsFromAmp(const std::string& playlist_id,
                                                       std::vector<std::string>& catalog_ids_out,
                                                       std::string& error,
                                                       const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    catalog_ids_out.clear();
    error.clear();
    if (!isSafePlaylistId(playlist_id)) {
        error = "Invalid Apple Music library playlist ID";
        return false;
    }

    std::vector<std::string> complete_ids;
    std::unordered_set<std::string> visited_paths;
    std::string path = "/v1/me/library/playlists/" + playlist_id + "/tracks?limit=100&include=catalog";
    while (!path.empty()) {
        if (!visited_paths.insert(path).second || visited_paths.size() > 1000) {
            error = "Pagination repeated while reading Apple Music playlist tracks";
            return false;
        }
        HttpResponse r = ampApiGet(path, true, cancel);
        if (!r.ok) {
            error = r.error.empty() ? "Failed to fetch Apple Music playlist tracks" : r.error;
            return false;
        }
        auto j = nlohmann::json::parse(r.body, nullptr, false);
        if (!j.is_object() || j.contains("errors") || !j.contains("data") || !j["data"].is_array()) {
            error = "Invalid Apple Music playlist tracks response";
            return false;
        }
        for (const auto& tr : j["data"]) {
            std::unordered_set<std::string> identities;
            if (safeJsonString(tr, "type") == "songs") {
                identities.insert(safeJsonString(tr, "id"));
            }
            if (tr.contains("attributes") && tr["attributes"].is_object()) {
                const auto& attr = tr["attributes"];
                if (attr.contains("playParams") && attr["playParams"].is_object()) {
                    auto catalog_id = safeJsonString(attr["playParams"], "catalogId");
                    if (!catalog_id.empty()) identities.insert(catalog_id);
                }
            }
            if (tr.contains("relationships") && tr["relationships"].is_object()) {
                const auto& rels = tr["relationships"];
                if (rels.contains("catalog") && rels["catalog"].is_object() &&
                    rels["catalog"].contains("data") && rels["catalog"]["data"].is_array()) {
                    for (const auto& cat : rels["catalog"]["data"]) {
                        if (safeJsonString(cat, "type") == "songs") {
                            identities.insert(safeJsonString(cat, "id"));
                        }
                    }
                }
            }
            if (identities.size() != 1 || !isNumericSongId(*identities.begin())) {
                error = "Missing or ambiguous catalog identity in live playlist";
                return false;
            }
            complete_ids.push_back(*identities.begin());
        }
        if (j.contains("next") && !j["next"].is_null() && !j["next"].is_string()) {
            error = "Invalid playlist pagination";
            return false;
        }
        path = jStr(j, "next");
        const std::string expected = "/v1/me/library/playlists/" + playlist_id + "/tracks?";
        if (!path.empty() && path.rfind(expected, 0) != 0) {
            error = "Unexpected playlist pagination path or host";
            return false;
        }
    }
    if (cancel && cancel->isCancelled()) {
        error = "cancelled";
        return false;
    }
    catalog_ids_out = std::move(complete_ids);
    return true;
}

bool AdamClient::fetchLibraryPlaylistFromAmp(const std::string& playlist_id,
                                            ContainerItem& playlist_out,
                                            std::string& error,
                                            const std::shared_ptr<omnidesk::CancelToken>& cancel) {
    error.clear();
    if (!isSafePlaylistId(playlist_id)) {
        error = "Invalid Apple Music library playlist ID";
        return false;
    }
    auto response = ampApiGet("/v1/me/library/playlists/" + playlist_id, true, cancel);
    if (!response.ok) {
        error = response.error;
        return false;
    }
    try {
        auto body = nlohmann::json::parse(response.body);
        if (!body.is_object() || body.contains("errors") || !body.at("data").is_array() ||
            body["data"].size() != 1) throw std::runtime_error("Invalid playlist");
        const auto& node = body["data"][0];
        const auto& attributes = node.at("attributes");
        if (node.at("id") != playlist_id || node.at("type") != "library-playlists" ||
            !attributes.at("name").is_string() || attributes["name"].get<std::string>().empty() ||
            attributes.at("canEdit") != true) throw std::runtime_error("Invalid or uneditable playlist");
        playlist_out.id = playlist_id;
        playlist_out.name = attributes["name"].get<std::string>();
        playlist_out.type = "library-playlists";
        return true;
    } catch (const std::exception&) {
        error = "Live playlist identity, name or edit permission could not be verified";
        return false;
    }
}

namespace {

ExportJobInfo parseExportJobJson(const nlohmann::json& j) {
    ExportJobInfo job;
    if (!j.is_object()) return job;
    job.job_id = jStr(j, "jobId");
    job.asset_id = jStr(j, "assetId");
    job.phase = jStr(j, "phase", "queued");
    job.percent = jInt(j, "percent", 0);
    job.queue_index = jInt(j, "queueIndex", 0);
    job.priority = jInt(j, "priority", 0);
    job.title = jStr(j, "title");
    job.artist = jStr(j, "artistName");
    job.artwork_url = formatArtworkUrl(jStr(j, "artworkUrl"), 300);
    job.bytes_done = jInt64(j, "bytesDone", 0);
    job.bytes_total = jInt64(j, "bytesTotal", 0);
    job.output = jStr(j, "output");
    job.error = jStr(j, "error");
    job.source = jStr(j, "source");
    return job;
}

} // namespace

bool AdamClient::enqueueExportJob(const std::string& asset_id,
                                  const std::string& storefront,
                                  bool is_video,
                                  bool is_playlist,
                                  bool is_library_playlist,
                                  bool convert_to_flac,
                                  const std::string& output_dir,
                                  const std::string& hint_title,
                                  const std::string& hint_artist,
                                  const std::string& hint_artwork,
                                  ExportJobInfo& job_out,
                                  std::string& error) {
    job_out = ExportJobInfo{};
    error.clear();
    if (trimCopy(asset_id).empty()) {
        error = "导出任务缺少曲目/歌单 assetId";
        return false;
    }
    if (!ensureEngineRunning(error)) {
        return false;
    }

    std::string dev = getDeveloperToken(false);
    std::string mut = resolveMediaUserTokenWithFallback();
    std::string eff_sf = effectiveStorefront(storefront);
    std::string out_dir = trimCopy(output_dir).empty()
                              ? expandUserPath("~/Music/AppleMusic")
                              : expandUserPath(trimCopy(output_dir));
    std::error_code ec;
    fs::create_directories(out_dir, ec);

    std::string lang = "zh-CN";
    {
        std::lock_guard<std::mutex> lk(cfg_mutex_);
        if (!cfg_.language.empty()) lang = cfg_.language;
    }

    nlohmann::json req = {
        {"assetId", trimCopy(asset_id)},
        {"storefront", eff_sf},
        {"language", lang},
        {"outputDir", out_dir},
        {"filenameTemplate", "{album_artist}/{album}/{track_number:02d} - {title}"},
        {"capabilities", {
            {"lossless", !is_video},
            {"atmos", false},
            {"video", is_video},
            {"playlist", is_playlist},
            {"libraryPlaylist", is_library_playlist},
        }},
        {"options", {
            {"embedArtwork", true},
            {"artworkSize", 3000},
            {"embedLyrics", true},
            {"lrcFormat", "lrc"},
            {"lrcType", "lyrics"},
            {"saveLrcSidecar", true},
            {"overwritePolicy", "skip"},
            {"convertToFlac", convert_to_flac && !is_video},
            {"keepOriginal", false},
        }},
    };
    if (!dev.empty()) req["token"] = dev;
    if (!mut.empty()) req["mut"] = mut;
    if (!hint_title.empty()) req["hintTitle"] = hint_title;
    if (!hint_artist.empty()) req["hintArtist"] = hint_artist;
    if (!hint_artwork.empty()) req["hintArtwork"] = hint_artwork;

    HttpResponse r = engineRequest("POST", "/api/v1/export", req.dump(),
                                   "application/json", "application/json", 15000);
    if (!r.ok) {
        error = r.error.empty() ? "提交无损导出任务失败" : r.error;
        return false;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (!j.is_object()) {
        error = "导出任务响应格式无效";
        return false;
    }
    job_out = parseExportJobJson(j);
    return true;
}

bool AdamClient::listExportJobs(std::vector<ExportJobInfo>& jobs_out, std::string& error) {
    jobs_out.clear();
    error.clear();
    if (!isPortReachable()) {
        return true;
    }
    HttpResponse r = engineRequest("GET", "/api/v1/export", "", "application/json", "application/json", 5000);
    if (!r.ok) {
        error = r.error;
        return false;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (j.is_array()) {
        for (const auto& item : j) {
            if (item.is_object()) jobs_out.push_back(parseExportJobJson(item));
        }
    } else if (j.is_object() && j.contains("jobs") && j["jobs"].is_array()) {
        for (const auto& item : j["jobs"]) {
            if (item.is_object()) jobs_out.push_back(parseExportJobJson(item));
        }
    }
    return true;
}

bool AdamClient::cancelExportJob(const std::string& job_id, std::string& error) {
    error.clear();
    if (trimCopy(job_id).empty()) {
        error = "缺少 jobId";
        return false;
    }
    HttpResponse r = engineRequest("DELETE", "/api/v1/export/" + LrclibClient::urlEncode(trimCopy(job_id)));
    if (!r.ok) {
        error = r.error;
        return false;
    }
    return true;
}

bool AdamClient::retryExportJob(const std::string& job_id, ExportJobInfo& job_out, std::string& error) {
    job_out = ExportJobInfo{};
    error.clear();
    if (trimCopy(job_id).empty()) {
        error = "缺少 jobId";
        return false;
    }
    HttpResponse r = engineRequest("POST", "/api/v1/export/" + LrclibClient::urlEncode(trimCopy(job_id)) + "/retry", "{}");
    if (!r.ok) {
        error = r.error;
        return false;
    }
    auto j = nlohmann::json::parse(r.body, nullptr, false);
    if (j.is_object()) job_out = parseExportJobJson(j);
    return true;
}

bool AdamClient::fetchCacheStats(nlohmann::json& out, std::string& error) {
    out = nlohmann::json::object();
    error.clear();
    HttpResponse r = engineRequest("GET", "/api/v1/cache/stats", "", "application/json", "application/json", 4000);
    if (!r.ok) {
        error = r.error;
        return false;
    }
    out = nlohmann::json::parse(r.body, nullptr, false);
    if (!out.is_object()) {
        out = nlohmann::json::object();
    }
    return true;
}

} // namespace music_plugin
