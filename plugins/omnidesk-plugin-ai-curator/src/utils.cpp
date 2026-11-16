#include "ai_curator/utils.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <ctime>
#include <errno.h>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <openssl/sha.h>
#include <openssl/evp.h>
#include <iomanip>
#include <sys/file.h>
#include <sys/stat.h>
#include <poll.h>
#include <signal.h>
#include <sstream>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace fs = std::filesystem;

namespace ai_curator {

void requireSafePath(const fs::path& path) {
    const std::string raw = path.string();
    if (raw.empty()) throw std::runtime_error("Empty path forbidden");
    const auto absolute = fs::absolute(path);
    fs::path prefix;
    for (const auto& part : absolute) {
        if (part == "..") throw std::runtime_error("Parent path traversal forbidden");
        prefix /= part;
        const auto status = fs::symlink_status(prefix);
        if (fs::is_symlink(status)) throw std::runtime_error("Symlink cache path forbidden: " + prefix.string());
    }
}

CacheLock::CacheLock(const fs::path& path) {
    requireSafePath(path);
    fs::create_directories(path.parent_path());
    descriptor_ = ::open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (descriptor_ < 0) throw std::runtime_error("Cannot open cache lock: " + path.string());
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    while (::flock(descriptor_, LOCK_EX | LOCK_NB) != 0) {
        if ((errno != EINTR && errno != EWOULDBLOCK) || std::chrono::steady_clock::now() >= deadline) {
            ::close(descriptor_);
            descriptor_ = -1;
            throw std::runtime_error("Cannot acquire cache lock: " + path.string());
        }
        ::poll(nullptr, 0, 5);
    }
}

CacheLock::~CacheLock() {
    if (descriptor_ >= 0) ::close(descriptor_);
}

std::string sha256(const std::string& text) {
    unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256(reinterpret_cast<const unsigned char*>(text.data()), text.size(), digest);
    std::ostringstream output;
    for (const auto byte : digest) output << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(byte);
    return output.str();
}

std::string gitBlobSha1(const std::string& bytes) {
    const std::string header = "blob " + std::to_string(bytes.size()) + '\0';
    unsigned char digest[SHA_DIGEST_LENGTH];
    std::string payload;
    payload.reserve(header.size() + bytes.size());
    payload.append(header);
    payload.append(bytes);
    unsigned int digest_len = 0;
    EVP_Digest(payload.data(), payload.size(), digest, &digest_len, EVP_sha1(), nullptr);
    std::ostringstream output;
    for (unsigned int i = 0; i < digest_len; ++i) {
        output << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(digest[i]);
    }
    return output.str();
}

std::string canonicalJsonCompact(const nlohmann::json& j) {
    return j.dump(-1, ' ', false);
}

bool safeIdentifier(const std::string& text) {
    return !text.empty() && text.size() <= 96 &&
           std::isalnum(static_cast<unsigned char>(text.front())) &&
           std::all_of(text.begin(), text.end(), [](unsigned char byte) {
               return (byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
                      (byte >= '0' && byte <= '9') || byte == '-' || byte == '_';
           });
}

std::string trim(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && static_cast<unsigned char>(s[b]) <= ' ') {
        ++b;
    }
    size_t e = s.size();
    while (e > b && static_cast<unsigned char>(s[e - 1]) <= ' ') {
        --e;
    }
    return s.substr(b, e - b);
}

std::string toLowerAscii(const std::string& s) {
    std::string out = s;
    for (char& ch : out) {
        if (ch >= 'A' && ch <= 'Z') {
            ch = static_cast<char>(ch - 'A' + 'a');
        }
    }
    return out;
}

bool startsWith(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

bool endsWith(const std::string& s, const std::string& suffix) {
    return s.size() >= suffix.size() &&
           s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::string normalizeForDedup(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (unsigned char ch : s) {
        if (std::isalnum(ch)) {
            out.push_back(static_cast<char>(std::tolower(ch)));
        } else if (ch >= 0x80) {
            out.push_back(static_cast<char>(ch));
        }
    }
    return out;
}

std::string sanitizeFilenameComponent(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (unsigned char ch : s) {
        if (std::isalnum(ch) || ch == '-' || ch == '_' || ch == '.') {
            out.push_back(static_cast<char>(ch));
        } else {
            out.push_back('_');
        }
    }
    while (!out.empty() && out.front() == '.') {
        out.erase(out.begin());
    }
    if (out.empty()) {
        out = "item";
    }
    return out;
}

std::string currentShanghaiDate() {
    const std::time_t now = std::time(nullptr);
    const std::time_t shanghai = now + 8 * 3600;
    std::tm tm_val{};
    gmtime_r(&shanghai, &tm_val);
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d",
                  tm_val.tm_year + 1900, tm_val.tm_mon + 1, tm_val.tm_mday);
    return std::string(buf);
}

bool isValidIsoDate(const std::string& s) {
    if (s.size() != 10) return false;
    for (size_t i = 0; i < 10; ++i) {
        if (i == 4 || i == 7) {
            if (s[i] != '-') return false;
        } else if (!std::isdigit(static_cast<unsigned char>(s[i]))) {
            return false;
        }
    }
    const int month = std::stoi(s.substr(5, 2));
    const int day = std::stoi(s.substr(8, 2));
    const int year = std::stoi(s.substr(0, 4));
    if (year < 1 || month < 1 || month > 12) return false;
    const bool leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    const int month_days[] = {31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    return day >= 1 && day <= month_days[month - 1];
}

std::optional<std::string> readFileText(const std::string& path) {
    std::ifstream ifs(path, std::ios::in | std::ios::binary);
    if (!ifs.is_open()) {
        return std::nullopt;
    }
    std::ostringstream oss;
    oss << ifs.rdbuf();
    if (ifs.bad()) {
        return std::nullopt;
    }
    return oss.str();
}

bool writeFileAtomicLocked(const std::string& path, const std::string& content, std::string* error_out) {
    int descriptor = -1;
    std::string temporary;
    try {
        requireSafePath(path);
        fs::create_directories(fs::path(path).parent_path());
        temporary = path + ".tmp.XXXXXX";
        descriptor = ::mkstemp(temporary.data());
        if (descriptor < 0) throw std::runtime_error("Cannot create temporary cache file");
        size_t written = 0;
        while (written < content.size()) {
            const auto count = ::write(descriptor, content.data() + written, content.size() - written);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::runtime_error("Cache write failed");
            written += static_cast<size_t>(count);
        }
        if (::fsync(descriptor) != 0) throw std::runtime_error("Cache fsync failed");
        ::close(descriptor);
        descriptor = -1;
        fs::rename(temporary, path);
        const int directory = ::open(fs::path(path).parent_path().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (directory >= 0) {
            ::fsync(directory);
            ::close(directory);
        }
        return true;
    } catch (const std::exception& error) {
        if (descriptor >= 0) ::close(descriptor);
        if (!temporary.empty()) {
            std::error_code ignored;
            fs::remove(temporary, ignored);
        }
        if (error_out) *error_out = error.what();
        return false;
    }
}

bool writeFileAtomic(const std::string& path, const std::string& content, std::string* error_out) {
    try {
        requireSafePath(path);
        const fs::path lock_root =
            fs::temp_directory_path() / ("omnidesk-ai-curator-locks-" + std::to_string(::geteuid()));
        fs::create_directories(lock_root);
        ::chmod(lock_root.c_str(), 0700);
        CacheLock lock((lock_root / (sha256(fs::absolute(path).string()) + ".lock")).string());
        return writeFileAtomicLocked(path, content, error_out);
    } catch (const std::exception& error) {
        if (error_out) *error_out = error.what();
        return false;
    }
}

std::optional<nlohmann::json> readJsonFile(const std::string& path, std::string* error_out) {
    auto txt = readFileText(path);
    if (!txt.has_value()) {
        if (error_out) *error_out = "Cannot read file: " + path;
        return std::nullopt;
    }
    try {
        return nlohmann::json::parse(*txt);
    } catch (const std::exception& e) {
        if (error_out) *error_out = std::string("JSON parse error in ") + path + ": " + e.what();
        return std::nullopt;
    }
}

bool writeJsonFileAtomicLocked(const std::string& path, const nlohmann::json& j, int indent, std::string* error_out) {
    return writeFileAtomicLocked(path, j.dump(indent) + "\n", error_out);
}

bool writeJsonFileAtomic(const std::string& path, const nlohmann::json& j, int indent, std::string* error_out) {
    return writeFileAtomic(path, j.dump(indent) + "\n", error_out);
}

static bool isValidYtChar(char ch) {
    return std::isalnum(static_cast<unsigned char>(ch)) || ch == '-' || ch == '_';
}

std::string extractYouTubeVideoId(const std::string& url) {
    auto extract11 = [](const std::string& src, size_t start) -> std::string {
        if (start + 11 > src.size()) return "";
        std::string cand = src.substr(start, 11);
        for (char c : cand) {
            if (!isValidYtChar(c)) return "";
        }
        if (start + 11 < src.size() && isValidYtChar(src[start + 11])) {
            return "";
        }
        return cand;
    };
    for (const char* marker : {"v=", "youtu.be/", "/embed/", "/shorts/"}) {
        size_t pos = url.find(marker);
        if (pos != std::string::npos) {
            std::string id = extract11(url, pos + std::char_traits<char>::length(marker));
            if (!id.empty()) return id;
        }
    }
    return "";
}

std::string extractAppleMusicSongId(const std::string& url) {
    size_t q = url.find("?i=");
    if (q == std::string::npos) q = url.find("&i=");
    if (q != std::string::npos) {
        size_t start = q + 3;
        size_t end = start;
        while (end < url.size() && std::isdigit(static_cast<unsigned char>(url[end]))) ++end;
        if (end > start) return url.substr(start, end - start);
    }
    size_t pos = url.find("/song/");
    if (pos != std::string::npos) {
        size_t last_slash = url.find_last_of('/');
        if (last_slash != std::string::npos && last_slash + 1 < url.size()) {
            size_t end = last_slash + 1;
            while (end < url.size() && std::isdigit(static_cast<unsigned char>(url[end]))) ++end;
            if (end > last_slash + 1) return url.substr(last_slash + 1, end - (last_slash + 1));
        }
    }
    return "";
}

std::string extractRedditPostId(const std::string& url) {
    size_t pos = url.find("/comments/");
    if (pos == std::string::npos) return "";
    size_t start = pos + 10;
    size_t end = start;
    while (end < url.size() && std::isalnum(static_cast<unsigned char>(url[end]))) ++end;
    if (end > start) return url.substr(start, end - start);
    return "";
}

std::string extractDoiFromText(const std::string& text) {
    size_t search_from = 0;
    while (true) {
        size_t pos = text.find("10.", search_from);
        if (pos == std::string::npos) return "";
        // Verify registrant prefix digits followed by '/'
        size_t cur = pos + 3;
        if (cur < text.size() && std::isdigit(static_cast<unsigned char>(text[cur]))) {
            while (cur < text.size() && (std::isdigit(static_cast<unsigned char>(text[cur])) || text[cur] == '.')) {
                ++cur;
            }
            if (cur < text.size() && text[cur] == '/') {
                size_t end = cur + 1;
                while (end < text.size()) {
                    unsigned char c = static_cast<unsigned char>(text[end]);
                    if (c <= ' ' || c == ')' || c == ']' || c == '>' || c == '"' || c == '\'' || c == ',') {
                        break;
                    }
                    ++end;
                }
                std::string doi = text.substr(pos, end - pos);
                while (!doi.empty() && (doi.back() == '.' || doi.back() == ';')) {
                    doi.pop_back();
                }
                if (doi.size() > 7) return toLowerAscii(doi);
            }
        }
        search_from = pos + 3;
    }
}

std::vector<std::string> extractUrlsFromText(const std::string& text) {
    std::vector<std::string> urls;
    size_t pos = 0;
    while (true) {
        size_t h = text.find("https://", pos);
        size_t h2 = text.find("http://", pos);
        size_t next = std::min(h, h2);
        if (next == std::string::npos) break;
        size_t end = next;
        while (end < text.size()) {
            unsigned char c = static_cast<unsigned char>(text[end]);
            if (c <= ' ' || c == ')' || c == ']' || c == '>' || c == '"' || c == '\'' || c == '`') {
                break;
            }
            ++end;
        }
        std::string u = text.substr(next, end - next);
        while (!u.empty() && (u.back() == '.' || u.back() == ',' || u.back() == ';')) {
            u.pop_back();
        }
        if (!u.empty() && std::find(urls.begin(), urls.end(), u) == urls.end()) {
            urls.push_back(std::move(u));
        }
        pos = end;
    }
    return urls;
}

std::string stripMarkdownLinks(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size();) {
        if (s[i] == '[') {
            size_t close_b = s.find(']', i + 1);
            if (close_b != std::string::npos && close_b + 1 < s.size() && s[close_b + 1] == '(') {
                size_t close_p = s.find(')', close_b + 2);
                if (close_p != std::string::npos) {
                    out.append(s.substr(i + 1, close_b - (i + 1)));
                    i = close_p + 1;
                    continue;
                }
            }
        }
        out.push_back(s[i]);
        ++i;
    }
    return out;
}

std::string utf8Truncate(const std::string& s, size_t max_chars) {
    if (s.empty() || max_chars == 0) return "";
    size_t count = 0;
    size_t i = 0;
    while (i < s.size() && count < max_chars) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        size_t char_len = 1;
        if ((c & 0x80) == 0) char_len = 1;
        else if ((c & 0xE0) == 0xC0) char_len = 2;
        else if ((c & 0xF0) == 0xE0) char_len = 3;
        else if ((c & 0xF8) == 0xF0) char_len = 4;
        if (i + char_len > s.size()) break;
        i += char_len;
        ++count;
    }
    if (i >= s.size()) return s;
    return s.substr(0, i) + "…";
}

void parseFrontmatter(const std::string& md,
                      std::unordered_map<std::string, std::string>& out_kv,
                      std::string& out_body) {
    out_kv.clear();
    if (!startsWith(md, "---\n") && !startsWith(md, "---\r\n")) {
        out_body = md;
        return;
    }
    size_t start = md.find('\n');
    if (start == std::string::npos) {
        out_body = md;
        return;
    }
    size_t end_marker = md.find("\n---", start + 1);
    if (end_marker == std::string::npos) {
        out_body = md;
        return;
    }
    const std::string fm = md.substr(start + 1, end_marker - (start + 1));
    std::istringstream iss(fm);
    std::string line;
    while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == ' ' || line[0] == '\t' || line[0] == '-') continue;
        size_t colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string key = trim(line.substr(0, colon));
        std::string val = trim(line.substr(colon + 1));
        if (val.size() >= 2 && ((val.front() == '"' && val.back() == '"') ||
                                (val.front() == '\'' && val.back() == '\''))) {
            val = val.substr(1, val.size() - 2);
        }
        out_kv[key] = val;
    }
    size_t body_start = md.find('\n', end_marker + 1);
    if (body_start == std::string::npos) {
        out_body.clear();
    } else {
        out_body = md.substr(body_start + 1);
    }
}

std::string extractUserNotesAfterAutoEnd(const std::string& md) {
    std::string tail;
    const std::string end_marker = "<!-- AUTO:END -->";
    size_t pos = md.find(end_marker);
    if (pos != std::string::npos) {
        tail = md.substr(pos + end_marker.size());
    } else {
        for (const char* hdr : {"## My Notes", "## Notes", "### Notes"}) {
            size_t hpos = md.find(hdr);
            if (hpos != std::string::npos) {
                tail = md.substr(hpos);
                break;
            }
        }
    }
    tail = trim(tail);
    if (tail.empty()) return "";

    // Strip leading Notes heading if present
    std::istringstream iss(tail);
    std::string line;
    std::ostringstream cleaned;
    bool first_line = true;
    while (std::getline(iss, line)) {
        std::string t = trim(line);
        if (first_line && (t == "## Notes" || t == "### Notes" || t == "## My Notes")) {
            first_line = false;
            continue;
        }
        first_line = false;
        if (t == "> 这里可以直接添加你自己的判断、疑问、关联笔记；每日自动更新不会覆盖这一部分。") {
            continue;
        }
        cleaned << line << "\n";
    }
    return trim(cleaned.str());
}

bool updateMarkdownPreservingNotes(const std::string& path,
                                   const std::string& new_auto_block,
                                   std::string* error_out) {
    const std::string start_marker = "<!-- AUTO:START -->";
    const std::string end_marker = "<!-- AUTO:END -->";

    std::string normalized_auto = trim(new_auto_block);
    std::string wrapped_auto;
    if (startsWith(normalized_auto, start_marker) && endsWith(normalized_auto, end_marker)) {
        wrapped_auto = normalized_auto;
    } else {
        wrapped_auto = start_marker + "\n" + normalized_auto + "\n" + end_marker;
    }

    auto existing = readFileText(path);
    if (!existing.has_value()) {
        return writeFileAtomic(path, wrapped_auto + "\n\n## Notes\n", error_out);
    }

    const std::string& content = *existing;
    const size_t s_pos = content.find(start_marker);
    const size_t e_pos = content.find(end_marker);

    if (s_pos != std::string::npos && e_pos != std::string::npos && e_pos > s_pos) {
        std::string prefix = content.substr(0, s_pos);
        std::string suffix = content.substr(e_pos + end_marker.size());
        return writeFileAtomic(path, prefix + wrapped_auto + suffix, error_out);
    }

    // Fallback if no AUTO markers exist: preserve any trailing ## Notes / ## My Notes / ### Notes
    size_t notes_pos = std::string::npos;
    for (const char* hdr : {"\n## My Notes", "\n## Notes", "\n### Notes"}) {
        size_t p = content.find(hdr);
        if (p != std::string::npos) {
            notes_pos = p + 1;
            break;
        }
    }
    std::string suffix = (notes_pos != std::string::npos)
                             ? ("\n\n" + content.substr(notes_pos))
                             : "\n\n## Notes\n";
    std::unordered_map<std::string, std::string> fm_kv;
    std::string body_unused;
    parseFrontmatter(content, fm_kv, body_unused);
    std::string prefix;
    if (startsWith(content, "---\n")) {
        size_t end_fm = content.find("\n---", 4);
        if (end_fm != std::string::npos) {
            size_t after_fm = content.find('\n', end_fm + 1);
            if (after_fm != std::string::npos) {
                prefix = content.substr(0, after_fm + 1) + "\n";
            }
        }
    }
    return writeFileAtomic(path, prefix + wrapped_auto + suffix, error_out);
}

ProcessResult runProcess(const std::vector<std::string>& args,
                         const std::string& cwd,
                         int timeout_ms) {
    ProcessResult res;
    if (args.empty()) {
        res.spawn_error = "Empty argument list";
        return res;
    }

    int out_pipe[2]{-1, -1};
    int err_pipe[2]{-1, -1};
    if (::pipe(out_pipe) != 0 || ::pipe(err_pipe) != 0) {
        if (out_pipe[0] >= 0) { ::close(out_pipe[0]); ::close(out_pipe[1]); }
        if (err_pipe[0] >= 0) { ::close(err_pipe[0]); ::close(err_pipe[1]); }
        res.spawn_error = "pipe() failed";
        return res;
    }

    pid_t pid = ::fork();
    if (pid < 0) {
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        res.spawn_error = "fork() failed";
        return res;
    }

    if (pid == 0) {
        ::close(out_pipe[0]);
        ::close(err_pipe[0]);
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::dup2(err_pipe[1], STDERR_FILENO);
        ::close(out_pipe[1]);
        ::close(err_pipe[1]);
        if (!cwd.empty()) {
            if (::chdir(cwd.c_str()) != 0) {
                _exit(127);
            }
        }
        std::vector<char*> argv;
        argv.reserve(args.size() + 1);
        for (const auto& a : args) {
            argv.push_back(const_cast<char*>(a.c_str()));
        }
        argv.push_back(nullptr);
        ::execvp(argv[0], argv.data());
        _exit(127);
    }

    ::close(out_pipe[1]);
    ::close(err_pipe[1]);

    ::fcntl(out_pipe[0], F_SETFL, O_NONBLOCK);
    ::fcntl(err_pipe[0], F_SETFL, O_NONBLOCK);

    bool out_open = true;
    bool err_open = true;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    char buf[4096];

    while (out_open || err_open) {
        auto now = std::chrono::steady_clock::now();
        if (now >= deadline) {
            res.timed_out = true;
            ::kill(pid, SIGKILL);
            break;
        }
        int remain_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now).count());
        struct pollfd fds[2];
        nfds_t nfds = 0;
        if (out_open) fds[nfds++] = {out_pipe[0], POLLIN, 0};
        if (err_open) fds[nfds++] = {err_pipe[0], POLLIN, 0};
        int pr = ::poll(fds, nfds, std::min(remain_ms, 200));
        if (pr < 0 && errno == EINTR) continue;
        for (nfds_t i = 0; i < nfds; ++i) {
            int fd = fds[i].fd;
            std::string* target = (fd == out_pipe[0]) ? &res.stdout_text : &res.stderr_text;
            while (true) {
                ssize_t n = ::read(fd, buf, sizeof(buf));
                if (n > 0) {
                    target->append(buf, static_cast<size_t>(n));
                } else if (n == 0) {
                    if (fd == out_pipe[0]) out_open = false;
                    else err_open = false;
                    break;
                } else {
                    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                    if (fd == out_pipe[0]) out_open = false;
                    else err_open = false;
                    break;
                }
            }
        }
    }

    ::close(out_pipe[0]);
    ::close(err_pipe[0]);

    int status = 0;
    while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    if (WIFEXITED(status)) {
        res.exit_code = WEXITSTATUS(status);
    } else if (WIFSIGNALED(status)) {
        res.exit_code = 128 + WTERMSIG(status);
    }
    return res;
}

} // namespace ai_curator
