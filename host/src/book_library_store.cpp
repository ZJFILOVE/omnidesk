#include "omnidesk/host/book_library_store.hpp"

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <unordered_set>

#include <yoga/Yoga.h>

#include "omnidesk/file_lock.hpp"
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/ui_theme.hpp"

namespace fs = std::filesystem;

namespace omnidesk::host {
namespace {

int64_t nowEpochMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string asciiLower(std::string s) {
    for (char& c : s) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return s;
}

std::string asciiUpper(std::string s) {
    for (char& c : s) {
        c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }
    return s;
}

std::string trimWs(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    size_t e = s.size();
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

std::string expandHome(const std::string& raw) {
    std::string s = trimWs(raw);
    if (s.empty()) return "";
    if (s == "~" || s.rfind("~/", 0) == 0) {
        if (const char* h = std::getenv("HOME"); h && *h) {
            return (fs::path(h) / (s.size() > 2 ? s.substr(2) : "")).generic_string();
        }
    }
    return s;
}

std::string canonicalOrNormalized(const std::string& raw_path) {
    std::string expanded = expandHome(raw_path);
    if (expanded.empty()) return "";
    std::error_code ec;
    fs::path p = fs::weakly_canonical(fs::path(expanded), ec);
    if (ec || p.empty()) {
        return fs::path(expanded).lexically_normal().generic_string();
    }
    return p.generic_string();
}

uint64_t fnv1a64(const std::string& input) {
    uint64_t h = 14695981039346656037ULL;
    for (unsigned char c : input) {
        h ^= c;
        h *= 1099511628211ULL;
    }
    return h;
}

std::string hexU64(uint64_t v) {
    std::ostringstream oss;
    oss << std::hex << std::setfill('0') << std::setw(16) << v;
    return oss.str();
}

std::string makePdfDocHash(const std::string& canon_path) {
    uint64_t h1 = fnv1a64(canon_path);
    uint64_t h2 = fnv1a64("babeldoc:" + canon_path);
    return hexU64(h1) + hexU64(h2);
}

std::string resolveDataRootDir() {
    if (HostKernel::instance().isInitialized() && !HostKernel::instance().dataRoot().empty()) {
        return HostKernel::instance().dataRoot();
    }
    if (const char* env = std::getenv("OMNIDESK_DATA_HOME"); env && *env) {
        return env;
    }
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && *xdg) {
        return (fs::path(xdg) / "omnidesk").generic_string();
    }
    const char* home = std::getenv("HOME");
    return (fs::path(home ? home : ".") / ".local" / "share" / "omnidesk").generic_string();
}

bool isSupportedBookExt(const std::string& ext_raw) {
    std::string e = asciiLower(ext_raw);
    return e == ".epub" || e == ".pdf" || e == ".mobi" || e == ".azw" ||
           e == ".azw3" || e == ".prc" || e == ".fb2" || e == ".txt" || e == ".md";
}

bool isRasterCoverFile(const std::string& path) {
    if (path.empty()) return false;
    std::error_code ec;
    if (!fs::is_regular_file(path, ec)) return false;
    std::string ext = asciiLower(fs::path(path).extension().string());
    return ext == ".png" || ext == ".jpg" || ext == ".jpeg" ||
           ext == ".webp" || ext == ".bmp" || ext == ".gif";
}

nlohmann::json readJsonFileUnlocked(const std::string& path) {
    std::ifstream ifs(path);
    if (!ifs.is_open()) return nlohmann::json();
    auto j = nlohmann::json::parse(ifs, nullptr, false);
    return j.is_discarded() ? nlohmann::json() : j;
}

bool writeJsonFileAtomicUnlocked(const std::string& path, const nlohmann::json& j) {
    if (path.empty()) return false;
    std::error_code ec;
    fs::path dst(path);
    if (dst.has_parent_path()) {
        fs::create_directories(dst.parent_path(), ec);
        if (ec) return false;
    }
    fs::path tmp = dst.parent_path() / (".tmp." + dst.filename().string() + "." + std::to_string(::getpid()));
    std::string payload = j.dump(2, ' ', false, nlohmann::json::error_handler_t::replace) + "\n";
    int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;
    size_t written = 0;
    while (written < payload.size()) {
        ssize_t n = ::write(fd, payload.data() + written, payload.size() - written);
        if (n < 0) {
            ::close(fd);
            ::unlink(tmp.c_str());
            return false;
        }
        written += static_cast<size_t>(n);
    }
    ::fsync(fd);
    ::close(fd);
    if (::rename(tmp.c_str(), dst.c_str()) != 0) {
        ::unlink(tmp.c_str());
        return false;
    }
    return true;
}

nlohmann::json makeDefaultLibraryDoc() {
    const int64_t ts = nowEpochMs();
    return {
        {"version", 1},
        {"groups", nlohmann::json::array({
            {{"id", "grp-cs"}, {"name", "计算机与工程"}, {"created_at_ms", ts}, {"sort_order", 1}},
            {{"id", "grp-sci"}, {"name", "数理与科学"}, {"created_at_ms", ts}, {"sort_order", 2}},
            {{"id", "grp-lit"}, {"name", "人文与原版"}, {"created_at_ms", ts}, {"sort_order", 3}},
            {{"id", "grp-paper"}, {"name", "学术论文"}, {"created_at_ms", ts}, {"sort_order", 4}},
        })},
        {"watched_dirs", nlohmann::json::array()},
        {"books", nlohmann::json::object()},
        {"excerpts", nlohmann::json::array()},
    };
}

void ensureLibrarySchema(nlohmann::json& doc) {
    if (!doc.is_object()) {
        doc = makeDefaultLibraryDoc();
        return;
    }
    if (!doc.contains("groups") || !doc["groups"].is_array()) {
        doc["groups"] = makeDefaultLibraryDoc()["groups"];
    }
    if (!doc.contains("watched_dirs") || !doc["watched_dirs"].is_array()) {
        doc["watched_dirs"] = nlohmann::json::array();
    }
    if (!doc.contains("books") || !doc["books"].is_object()) {
        doc["books"] = nlohmann::json::object();
    }
    if (!doc.contains("excerpts") || !doc["excerpts"].is_array()) {
        doc["excerpts"] = nlohmann::json::array();
    }
}

std::string clipUtf8(const std::string& s, size_t max_bytes) {
    if (s.size() <= max_bytes) return s;
    size_t cut = max_bytes;
    while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0) == 0x80) --cut;
    return s.substr(0, cut) + "...";
}

std::string formatFileSizeShort(int64_t bytes) {
    if (bytes <= 0) return "";
    char buf[32]{};
    if (bytes >= 1024 * 1024) {
        std::snprintf(buf, sizeof(buf), "%.1f MB", static_cast<double>(bytes) / (1024.0 * 1024.0));
    } else {
        std::snprintf(buf, sizeof(buf), "%lld KB", static_cast<long long>(std::max<int64_t>(1, bytes / 1024)));
    }
    return std::string(buf);
}

std::string formatTimeDateShort(int64_t ms) {
    if (ms <= 0) return "";
    std::time_t tt = static_cast<std::time_t>(ms / 1000);
    std::tm tm_val{};
    localtime_r(&tt, &tm_val);
    char buf[32]{};
    std::strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M", &tm_val);
    return std::string(buf);
}

std::string sanitizeFilenameComponent(const std::string& raw) {
    std::string out;
    out.reserve(raw.size());
    for (unsigned char c : raw) {
        if (c == '/' || c == '\\' || c == ':' || c == '*' ||
            c == '?' || c == '"' || c == '<' || c == '>' || c == '|') {
            out.push_back('_');
        } else if (c >= 32) {
            out.push_back(static_cast<char>(c));
        }
    }
    out = trimWs(out);
    if (out.empty()) out = "OmniDesk_Book_Notes";
    if (out.size() > 96) out = clipUtf8(out, 96);
    return out;
}

bool runCommandCapture(const std::vector<std::string>& args,
                       std::string& out_stdout,
                       int timeout_ms = 8000) {
    if (args.empty()) return false;
    std::string exec_path = args[0];
    if (exec_path.find('/') == std::string::npos) {
        exec_path.clear();
        if (const char* path_env = std::getenv("PATH")) {
            std::stringstream path_stream(path_env);
            std::string dir;
            while (std::getline(path_stream, dir, ':')) {
                if (dir.empty()) continue;
                std::string candidate = dir + "/" + args[0];
                if (::access(candidate.c_str(), X_OK) == 0) {
                    exec_path = std::move(candidate);
                    break;
                }
            }
        }
        if (exec_path.empty()) return false;
    }
    std::vector<char*> argv;
    argv.reserve(args.size() + 1);
    for (const auto& arg : args) argv.push_back(const_cast<char*>(arg.c_str()));
    argv.push_back(nullptr);

    int pipefd[2]{-1, -1};
    if (::pipe2(pipefd, O_CLOEXEC) != 0) return false;

    pid_t pid = ::fork();
    if (pid < 0) {
        ::close(pipefd[0]);
        ::close(pipefd[1]);
        return false;
    }
    if (pid == 0) {
        ::setpgid(0, 0);
        ::close(pipefd[0]);
        ::dup2(pipefd[1], STDOUT_FILENO);
        ::fcntl(STDOUT_FILENO, F_SETFD, 0);
        int devnull = ::open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            ::dup2(devnull, STDERR_FILENO);
            ::close(devnull);
        }
        if (pipefd[1] != STDOUT_FILENO) ::close(pipefd[1]);
        ::execv(exec_path.c_str(), argv.data());
        _exit(127);
    }

    ::setpgid(pid, pid);
    ::close(pipefd[1]);
    int flags = ::fcntl(pipefd[0], F_GETFL, 0);
    if (flags >= 0) ::fcntl(pipefd[0], F_SETFL, flags | O_NONBLOCK);

    out_stdout.clear();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    int status = 0;
    bool exited = false;
    char buf[2048];

    while (std::chrono::steady_clock::now() < deadline) {
        while (true) {
            ssize_t n = ::read(pipefd[0], buf, sizeof(buf));
            if (n > 0) {
                out_stdout.append(buf, static_cast<size_t>(n));
            } else {
                break;
            }
        }
        pid_t w = ::waitpid(pid, &status, WNOHANG);
        if (w == pid) {
            exited = true;
            break;
        }
        ::usleep(15000);
    }

    while (true) {
        ssize_t n = ::read(pipefd[0], buf, sizeof(buf));
        if (n > 0) out_stdout.append(buf, static_cast<size_t>(n));
        else break;
    }
    ::close(pipefd[0]);

    if (!exited) {
        ::kill(-pid, SIGKILL);
        ::kill(pid, SIGKILL);
        ::waitpid(pid, &status, 0);
        return false;
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

}  // namespace

BookLibraryStore& BookLibraryStore::instance() {
    static BookLibraryStore inst;
    return inst;
}

BookLibraryStore::BookLibraryStore() = default;

std::string BookLibraryStore::libraryJsonPath() const {
    return (fs::path(resolveDataRootDir()) / "book_library.json").generic_string();
}

std::string BookLibraryStore::coversDirPath() const {
    std::string p = (fs::path(resolveDataRootDir()) / "covers").generic_string();
    std::error_code ec;
    fs::create_directories(p, ec);
    return p;
}

std::string BookLibraryStore::computeBookKey(const std::string& file_path,
                                             const std::string& source_id,
                                             const std::string& book_id) {
    std::string canon = canonicalOrNormalized(file_path);
    if (!canon.empty()) return canon;
    if (!source_id.empty() && !book_id.empty()) return source_id + ":" + book_id;
    return book_id;
}

std::string BookLibraryStore::computeBookKey(const BookItem& item) {
    return computeBookKey(item.file_path, item.source_id, item.id);
}

void BookLibraryStore::parseCleanFilenameTitleAuthor(const std::string& raw_stem,
                                                     const std::string& fallback_author,
                                                     std::string& out_title,
                                                     std::string& out_author) {
    std::string s = trimWs(raw_stem);
    if (s.empty()) {
        out_title = "未命名图书";
        out_author = fallback_author;
        return;
    }

    if (s.find(" -- ") != std::string::npos) {
        std::vector<std::string> parts;
        size_t pos = 0;
        while (pos < s.size()) {
            size_t sep = s.find(" -- ", pos);
            if (sep == std::string::npos) {
                parts.push_back(trimWs(s.substr(pos)));
                break;
            }
            parts.push_back(trimWs(s.substr(pos, sep - pos)));
            pos = sep + 4;
        }
        auto cleanUnderscores = [](std::string v) {
            for (size_t i = 1; i + 1 < v.size(); ++i) {
                if (v[i] == '_' && v[i + 1] == ' ') v[i] = ':';
            }
            return v;
        };
        if (!parts.empty() && !parts[0].empty()) {
            out_title = cleanUnderscores(parts[0]);
            if (parts.size() >= 2 && !parts[1].empty() &&
                asciiLower(parts[1]).find("anna") == std::string::npos) {
                out_author = parts[1];
            } else {
                out_author = fallback_author;
            }
            return;
        }
    }

    auto stripTrailingSource = [](std::string& str) {
        while (!str.empty() && str.back() == ')') {
            size_t open = str.rfind('(');
            if (open == std::string::npos) break;
            std::string inside = asciiLower(str.substr(open + 1, str.size() - open - 2));
            if (inside.find("z-lib") != std::string::npos ||
                inside.find("1lib") != std::string::npos ||
                inside.find("b-ok") != std::string::npos ||
                inside.find("libgen") != std::string::npos) {
                str = trimWs(str.substr(0, open));
            } else {
                break;
            }
        }
    };
    std::string z = s;
    stripTrailingSource(z);
    if (z.size() < s.size() && !z.empty()) {
        if (z.back() == ')') {
            size_t open = z.rfind('(');
            if (open != std::string::npos && open > 2) {
                std::string maybe_author = trimWs(z.substr(open + 1, z.size() - open - 2));
                std::string maybe_title = trimWs(z.substr(0, open));
                if (!maybe_title.empty() && !maybe_author.empty()) {
                    out_title = maybe_title;
                    out_author = maybe_author;
                    return;
                }
            }
        }
        out_title = z;
        out_author = fallback_author;
        return;
    }

    out_title = s;
    out_author = fallback_author;
}

void BookLibraryStore::loadUnlocked(nlohmann::json& out_doc) const {
    const std::string p = libraryJsonPath();
    std::error_code ec;
    if (!fs::is_regular_file(p, ec)) {
        out_doc = makeDefaultLibraryDoc();
        return;
    }
    out_doc = readJsonFileUnlocked(p);
    ensureLibrarySchema(out_doc);
}

bool BookLibraryStore::mutateAndPublish(const std::function<bool(nlohmann::json&)>& mutator) {
    const std::string p = libraryJsonPath();
    bool saved = false;
    {
        ScopedFileLock guard(p, FileLock::Mode::Exclusive, 4000);
        nlohmann::json doc;
        loadUnlocked(doc);
        if (!mutator(doc)) return false;
        saved = writeJsonFileAtomicUnlocked(p, doc);
    }
    if (saved && HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().publish("entity.book.library_changed", {
            {"timestamp_ms", nowEpochMs()},
        });
    }
    return saved;
}

void BookLibraryStore::reload() {
    const std::string p = libraryJsonPath();
    std::error_code ec;
    if (!fs::is_regular_file(p, ec)) {
        ScopedFileLock guard(p, FileLock::Mode::Exclusive, 2000);
        if (!fs::is_regular_file(p, ec)) {
            writeJsonFileAtomicUnlocked(p, makeDefaultLibraryDoc());
        }
    }
}

std::vector<BookShelfGroup> BookLibraryStore::listGroups() const {
    const std::string p = libraryJsonPath();
    ScopedFileLock guard(p, FileLock::Mode::Shared, 2000);
    nlohmann::json doc;
    loadUnlocked(doc);
    std::vector<BookShelfGroup> out;
    for (const auto& gj : doc["groups"]) {
        if (!gj.is_object()) continue;
        BookShelfGroup g;
        g.id = gj.value("id", "");
        g.name = gj.value("name", "");
        g.created_at_ms = gj.value("created_at_ms", int64_t{0});
        g.sort_order = gj.value("sort_order", 0);
        if (!g.id.empty() && !g.name.empty()) out.push_back(std::move(g));
    }
    std::sort(out.begin(), out.end(), [](const BookShelfGroup& a, const BookShelfGroup& b) {
        if (a.sort_order != b.sort_order) return a.sort_order < b.sort_order;
        return a.created_at_ms < b.created_at_ms;
    });
    return out;
}

BookShelfGroup BookLibraryStore::createGroup(const std::string& name) {
    std::string trimmed = trimWs(name);
    BookShelfGroup created;
    if (trimmed.empty()) return created;
    const int64_t ts = nowEpochMs();
    created.id = "grp-" + hexU64(fnv1a64(trimmed + ":" + std::to_string(ts))).substr(0, 8);
    created.name = trimmed;
    created.created_at_ms = ts;

    mutateAndPublish([&](nlohmann::json& doc) {
        for (const auto& gj : doc["groups"]) {
            if (gj.is_object() && gj.value("name", "") == trimmed) {
                created.id = gj.value("id", created.id);
                return false;
            }
        }
        int max_ord = 0;
        for (const auto& gj : doc["groups"]) {
            if (gj.is_object()) max_ord = std::max(max_ord, gj.value("sort_order", 0));
        }
        created.sort_order = max_ord + 1;
        doc["groups"].push_back({
            {"id", created.id},
            {"name", created.name},
            {"created_at_ms", created.created_at_ms},
            {"sort_order", created.sort_order},
        });
        return true;
    });
    return created;
}

bool BookLibraryStore::renameGroup(const std::string& group_id, const std::string& new_name) {
    std::string trimmed = trimWs(new_name);
    if (group_id.empty() || trimmed.empty()) return false;
    return mutateAndPublish([&](nlohmann::json& doc) {
        for (auto& gj : doc["groups"]) {
            if (gj.is_object() && gj.value("id", "") == group_id) {
                gj["name"] = trimmed;
                return true;
            }
        }
        return false;
    });
}

bool BookLibraryStore::deleteGroup(const std::string& group_id) {
    if (group_id.empty()) return false;
    return mutateAndPublish([&](nlohmann::json& doc) {
        bool removed = false;
        nlohmann::json next_groups = nlohmann::json::array();
        for (const auto& gj : doc["groups"]) {
            if (gj.is_object() && gj.value("id", "") == group_id) {
                removed = true;
                continue;
            }
            next_groups.push_back(gj);
        }
        if (!removed) return false;
        doc["groups"] = std::move(next_groups);
        for (auto it = doc["books"].begin(); it != doc["books"].end(); ++it) {
            if (it.value().is_object() && it.value().value("group_id", "") == group_id) {
                it.value()["group_id"] = "";
            }
        }
        return true;
    });
}

std::string BookLibraryStore::groupName(const std::string& group_id) const {
    if (group_id.empty()) return "";
    for (const auto& g : listGroups()) {
        if (g.id == group_id) return g.name;
    }
    return "";
}

std::vector<std::string> BookLibraryStore::watchedDirs() const {
    const std::string p = libraryJsonPath();
    ScopedFileLock guard(p, FileLock::Mode::Shared, 2000);
    nlohmann::json doc;
    loadUnlocked(doc);
    std::vector<std::string> out;
    for (const auto& d : doc["watched_dirs"]) {
        if (d.is_string() && !d.get<std::string>().empty()) {
            out.push_back(d.get<std::string>());
        }
    }
    return out;
}

bool BookLibraryStore::addWatchedDir(const std::string& raw_dir) {
    std::string canon = canonicalOrNormalized(raw_dir);
    std::error_code ec;
    if (canon.empty() || !fs::is_directory(canon, ec)) return false;
    return mutateAndPublish([&](nlohmann::json& doc) {
        for (const auto& d : doc["watched_dirs"]) {
            if (d.is_string() && canonicalOrNormalized(d.get<std::string>()) == canon) {
                return false;
            }
        }
        doc["watched_dirs"].push_back(canon);
        return true;
    });
}

bool BookLibraryStore::removeWatchedDir(const std::string& raw_dir) {
    std::string canon = canonicalOrNormalized(raw_dir);
    if (canon.empty()) return false;
    return mutateAndPublish([&](nlohmann::json& doc) {
        nlohmann::json next = nlohmann::json::array();
        bool changed = false;
        for (const auto& d : doc["watched_dirs"]) {
            if (d.is_string() && canonicalOrNormalized(d.get<std::string>()) == canon) {
                changed = true;
                continue;
            }
            next.push_back(d);
        }
        if (changed) doc["watched_dirs"] = std::move(next);
        return changed;
    });
}

BookShelfEntry BookLibraryStore::entryForBook(const BookItem& book) const {
    const std::string key = computeBookKey(book);
    BookShelfEntry e;
    e.key = key;
    e.book_id = book.id;
    e.source_id = book.source_id;
    e.file_path = canonicalOrNormalized(book.file_path);
    e.format = book.format;
    e.current_page = book.current_page;
    e.total_pages = book.total_pages;
    e.progress_percent = book.progress_percent;

    const std::string p = libraryJsonPath();
    ScopedFileLock guard(p, FileLock::Mode::Shared, 2000);
    nlohmann::json doc;
    loadUnlocked(doc);
    if (doc["books"].contains(key) && doc["books"][key].is_object()) {
        const auto& bj = doc["books"][key];
        e.title_override = bj.value("title_override", "");
        e.author_override = bj.value("author_override", "");
        e.cover_path = bj.value("cover_path", "");
        e.status_override = bj.value("status_override", "auto");
        e.group_id = bj.value("group_id", "");
        e.pinned = bj.value("pinned", false);
        e.pinned_at_ms = bj.value("pinned_at_ms", int64_t{0});
        e.removed_from_shelf = bj.value("removed_from_shelf", false);
        e.added_at_ms = bj.value("added_at_ms", int64_t{0});
        e.last_read_at_ms = bj.value("last_read_at_ms", int64_t{0});
        e.last_chapter_title = bj.value("last_chapter_title", "");
        e.current_page = std::max(1, bj.value("current_page", e.current_page));
        e.total_pages = std::max(e.current_page, bj.value("total_pages", e.total_pages));
        e.progress_percent = bj.value("progress_percent", e.progress_percent);
    }
    return e;
}

void BookLibraryStore::enrichAndMergeLibraryBooks(std::vector<BookItem>& io_books) const {
    const std::string p = libraryJsonPath();
    nlohmann::json doc;
    {
        ScopedFileLock guard(p, FileLock::Mode::Shared, 2000);
        loadUnlocked(doc);
    }
    const auto& books_map = doc["books"];
    const std::string covers_dir = coversDirPath();
    std::error_code ec;

    std::unordered_set<std::string> seen_keys;
    std::vector<BookItem> merged;
    merged.reserve(io_books.size() + books_map.size());

    for (auto& b : io_books) {
        if (b.source_id == "plugin-ai-curator") {
            merged.push_back(std::move(b));
            continue;
        }
        if (!b.file_path.empty()) {
            b.file_path = canonicalOrNormalized(b.file_path);
        }
        const std::string key = computeBookKey(b);
        if (!key.empty() && !seen_keys.insert(key).second) continue;

        if (!isRasterCoverFile(b.cover_path)) {
            b.cover_path.clear();
        }
        if (b.cover_path.empty() && b.format == "pdf" && !b.file_path.empty()) {
            fs::path cand = fs::path(covers_dir) / ("pdf_" + makePdfDocHash(b.file_path) + ".png");
            if (fs::is_regular_file(cand, ec)) {
                b.cover_path = cand.generic_string();
            }
        }

        if (books_map.contains(key) && books_map[key].is_object()) {
            const auto& bj = books_map[key];
            if (bj.value("removed_from_shelf", false)) continue;
            if (!bj.value("title_override", "").empty()) b.title = bj.value("title_override", "");
            if (!bj.value("author_override", "").empty()) b.author = bj.value("author_override", "");
            std::string saved_cov = bj.value("cover_path", "");
            if (b.cover_path.empty() && isRasterCoverFile(saved_cov)) {
                b.cover_path = saved_cov;
            }
            int sp = bj.value("current_page", 0);
            int stp = bj.value("total_pages", 0);
            if (stp > b.total_pages) b.total_pages = stp;
            if (sp > 0) b.current_page = std::clamp(sp, 1, std::max(1, b.total_pages));
            double spct = bj.value("progress_percent", -1.0);
            if (spct >= 0.0) {
                b.progress_percent = spct;
            } else if (b.total_pages > 1 && b.current_page > 1) {
                b.progress_percent = std::min(100.0, 100.0 * b.current_page / b.total_pages);
            }
        }
        merged.push_back(std::move(b));
    }

    // Also surface any local book explicitly registered in book_library.json or watched_dirs
    auto tryAddFromPath = [&](const std::string& raw_fp, const nlohmann::json* meta) {
        std::string fp = canonicalOrNormalized(raw_fp);
        if (fp.empty() || !fs::is_regular_file(fp, ec)) return;
        std::string ext = asciiLower(fs::path(fp).extension().string());
        if (!isSupportedBookExt(ext)) return;
        std::string fmt = ext.substr(1);
        if (fmt == "azw" || fmt == "prc") fmt = "mobi";
        if (fmt == "md") fmt = "txt";
        std::string src_id = (fmt == "pdf") ? "plugin-pdf-babeldoc" : "plugin-ebook";
        std::string bid = (fmt == "pdf")
                              ? ("pdf:" + hexU64(fnv1a64(fp)).substr(0, 12))
                              : (fmt + "-" + hexU64(fnv1a64(fp)).substr(0, 10));
        std::string key = computeBookKey(fp, src_id, bid);
        if (!seen_keys.insert(key).second) return;
        if (meta && meta->value("removed_from_shelf", false)) return;
        if (!meta && books_map.contains(key) && books_map[key].is_object() &&
            books_map[key].value("removed_from_shelf", false)) {
            return;
        }

        BookItem b;
        b.id = bid;
        b.source_id = src_id;
        b.format = fmt;
        b.file_path = fp;
        parseCleanFilenameTitleAuthor(fs::path(fp).stem().string(),
                                      fmt == "pdf" ? "本地 PDF" : "本地藏书",
                                      b.title, b.author);
        if (fmt == "pdf") {
            fs::path cand = fs::path(covers_dir) / ("pdf_" + makePdfDocHash(fp) + ".png");
            if (fs::is_regular_file(cand, ec)) b.cover_path = cand.generic_string();
        }
        const nlohmann::json* bj = meta;
        if (!bj && books_map.contains(key) && books_map[key].is_object()) {
            bj = &books_map[key];
        }
        if (bj) {
            if (!bj->value("book_id", "").empty()) b.id = bj->value("book_id", b.id);
            if (!bj->value("title_override", "").empty()) b.title = bj->value("title_override", "");
            if (!bj->value("author_override", "").empty()) b.author = bj->value("author_override", "");
            std::string sc = bj->value("cover_path", "");
            if (b.cover_path.empty() && isRasterCoverFile(sc)) b.cover_path = sc;
            b.total_pages = std::max(1, bj->value("total_pages", 1));
            b.current_page = std::clamp(bj->value("current_page", 1), 1, b.total_pages);
            double spct = bj->value("progress_percent", -1.0);
            b.progress_percent = spct >= 0.0
                                     ? spct
                                     : (b.total_pages > 1 && b.current_page > 1
                                            ? std::min(100.0, 100.0 * b.current_page / b.total_pages)
                                            : 0.0);
        }
        merged.push_back(std::move(b));
    };

    for (auto it = books_map.begin(); it != books_map.end(); ++it) {
        if (!it.value().is_object()) continue;
        std::string fp = it.value().value("file_path", "");
        if (!fp.empty()) tryAddFromPath(fp, &it.value());
    }
    for (const auto& wd : doc["watched_dirs"]) {
        if (!wd.is_string()) continue;
        for (const auto& cand : scanCandidateBooksInDir(wd.get<std::string>(), 2)) {
            tryAddFromPath(cand.file_path, nullptr);
        }
    }

    io_books = std::move(merged);
}

BookReadingStatus BookLibraryStore::effectiveStatus(const BookItem& book,
                                                    const BookShelfEntry& entry) {
    if (entry.status_override == "finished") return BookReadingStatus::Finished;
    if (entry.status_override == "reading") return BookReadingStatus::Reading;
    if (entry.status_override == "unread") return BookReadingStatus::Unread;

    if (book.progress_percent >= 98.5 ||
        (book.total_pages > 1 && book.current_page >= book.total_pages)) {
        return BookReadingStatus::Finished;
    }
    if (entry.last_read_at_ms > 0 || book.current_page > 1 || book.progress_percent >= 1.0) {
        return BookReadingStatus::Reading;
    }
    return BookReadingStatus::Unread;
}

std::string BookLibraryStore::statusDisplayLabel(BookReadingStatus st) {
    switch (st) {
        case BookReadingStatus::Unread: return "未读";
        case BookReadingStatus::Reading: return "在读";
        case BookReadingStatus::Finished: return "读完";
    }
    return "未读";
}

void BookLibraryStore::registerImportedFile(const std::string& file_path,
                                            const std::string& group_id) {
    std::string fp = canonicalOrNormalized(file_path);
    std::error_code ec;
    if (fp.empty() || !fs::is_regular_file(fp, ec)) return;
    std::string ext = asciiLower(fs::path(fp).extension().string());
    if (!isSupportedBookExt(ext)) return;
    std::string fmt = ext.substr(1);
    if (fmt == "azw" || fmt == "prc") fmt = "mobi";
    if (fmt == "md") fmt = "txt";
    std::string src_id = (fmt == "pdf") ? "plugin-pdf-babeldoc" : "plugin-ebook";
    std::string bid = (fmt == "pdf")
                          ? ("pdf:" + hexU64(fnv1a64(fp)).substr(0, 12))
                          : (fmt + "-" + hexU64(fnv1a64(fp)).substr(0, 10));
    std::string key = computeBookKey(fp, src_id, bid);
    const int64_t ts = nowEpochMs();

    mutateAndPublish([&](nlohmann::json& doc) {
        nlohmann::json& bj = doc["books"][key];
        if (!bj.is_object()) bj = nlohmann::json::object();
        bj["file_path"] = fp;
        bj["format"] = fmt;
        bj["source_id"] = bj.value("source_id", src_id);
        bj["book_id"] = bj.value("book_id", bid);
        bj["removed_from_shelf"] = false;
        if (bj.value("added_at_ms", int64_t{0}) <= 0) {
            bj["added_at_ms"] = ts;
        }
        if (!group_id.empty()) {
            bj["group_id"] = group_id;
        }
        return true;
    });
}

void BookLibraryStore::touchBookRead(const BookItem& book,
                                     int page_number,
                                     int total_pages,
                                     const std::string& chapter_title) {
    if (book.source_id == "plugin-ai-curator") return;
    const std::string key = computeBookKey(book);
    if (key.empty()) return;
    const int64_t ts = nowEpochMs();
    const int eff_total = std::max({1, book.total_pages, total_pages});
    const int eff_page = std::clamp(page_number, 1, eff_total);
    const double pct = (eff_total > 1)
                           ? std::min(100.0, 100.0 * static_cast<double>(eff_page) / eff_total)
                           : std::max(book.progress_percent, 5.0);

    mutateAndPublish([&](nlohmann::json& doc) {
        nlohmann::json& bj = doc["books"][key];
        if (!bj.is_object()) bj = nlohmann::json::object();
        bj["book_id"] = book.id;
        bj["source_id"] = book.source_id;
        if (!book.file_path.empty()) bj["file_path"] = canonicalOrNormalized(book.file_path);
        bj["format"] = book.format;
        if (isRasterCoverFile(book.cover_path)) bj["cover_path"] = book.cover_path;
        bj["removed_from_shelf"] = false;
        if (bj.value("added_at_ms", int64_t{0}) <= 0) bj["added_at_ms"] = ts;
        bj["last_read_at_ms"] = ts;
        if (!chapter_title.empty()) bj["last_chapter_title"] = chapter_title;
        bj["current_page"] = eff_page;
        bj["total_pages"] = eff_total;
        bj["progress_percent"] = pct;
        if (bj.value("status_override", "auto") == "unread") {
            bj["status_override"] = "reading";
        }
        if (eff_total > 1 && eff_page >= eff_total && bj.value("status_override", "auto") == "reading") {
            bj["status_override"] = "finished";
        }
        return true;
    });
}

void BookLibraryStore::setBookStatus(const BookItem& book, const std::string& status_override) {
    setBooksStatus({book}, status_override);
}

void BookLibraryStore::setBooksStatus(const std::vector<BookItem>& books,
                                      const std::string& status_override) {
    if (books.empty()) return;
    const int64_t ts = nowEpochMs();
    mutateAndPublish([&](nlohmann::json& doc) {
        for (const auto& book : books) {
            const std::string key = computeBookKey(book);
            if (key.empty()) continue;
            nlohmann::json& bj = doc["books"][key];
            if (!bj.is_object()) bj = nlohmann::json::object();
            bj["book_id"] = book.id;
            bj["source_id"] = book.source_id;
            if (!book.file_path.empty()) bj["file_path"] = canonicalOrNormalized(book.file_path);
            bj["format"] = book.format;
            if (bj.value("added_at_ms", int64_t{0}) <= 0) bj["added_at_ms"] = ts;
            bj["status_override"] = status_override;
            if (status_override == "finished") {
                bj["progress_percent"] = 100.0;
            } else if (status_override == "unread") {
                bj["progress_percent"] = 0.0;
                bj["current_page"] = 1;
            } else if (status_override == "reading" && bj.value("last_read_at_ms", int64_t{0}) <= 0) {
                bj["last_read_at_ms"] = ts;
            }
        }
        return true;
    });
}

void BookLibraryStore::setBookGroup(const BookItem& book, const std::string& group_id) {
    setBooksGroup({book}, group_id);
}

void BookLibraryStore::setBooksGroup(const std::vector<BookItem>& books,
                                     const std::string& group_id) {
    if (books.empty()) return;
    const int64_t ts = nowEpochMs();
    mutateAndPublish([&](nlohmann::json& doc) {
        for (const auto& book : books) {
            const std::string key = computeBookKey(book);
            if (key.empty()) continue;
            nlohmann::json& bj = doc["books"][key];
            if (!bj.is_object()) bj = nlohmann::json::object();
            bj["book_id"] = book.id;
            bj["source_id"] = book.source_id;
            if (!book.file_path.empty()) bj["file_path"] = canonicalOrNormalized(book.file_path);
            bj["format"] = book.format;
            if (bj.value("added_at_ms", int64_t{0}) <= 0) bj["added_at_ms"] = ts;
            bj["group_id"] = group_id;
        }
        return true;
    });
}

void BookLibraryStore::setBookPinned(const BookItem& book, bool pinned) {
    setBooksPinned({book}, pinned);
}

void BookLibraryStore::setBooksPinned(const std::vector<BookItem>& books, bool pinned) {
    if (books.empty()) return;
    const int64_t ts = nowEpochMs();
    mutateAndPublish([&](nlohmann::json& doc) {
        for (const auto& book : books) {
            const std::string key = computeBookKey(book);
            if (key.empty()) continue;
            nlohmann::json& bj = doc["books"][key];
            if (!bj.is_object()) bj = nlohmann::json::object();
            bj["book_id"] = book.id;
            bj["source_id"] = book.source_id;
            if (!book.file_path.empty()) bj["file_path"] = canonicalOrNormalized(book.file_path);
            bj["pinned"] = pinned;
            bj["pinned_at_ms"] = pinned ? ts : int64_t{0};
        }
        return true;
    });
}

void BookLibraryStore::setBooksRemovedFromShelf(const std::vector<BookItem>& books, bool removed) {
    if (books.empty()) return;
    mutateAndPublish([&](nlohmann::json& doc) {
        for (const auto& book : books) {
            const std::string key = computeBookKey(book);
            if (key.empty()) continue;
            nlohmann::json& bj = doc["books"][key];
            if (!bj.is_object()) bj = nlohmann::json::object();
            bj["book_id"] = book.id;
            bj["source_id"] = book.source_id;
            if (!book.file_path.empty()) bj["file_path"] = canonicalOrNormalized(book.file_path);
            bj["removed_from_shelf"] = removed;
        }
        return true;
    });
}

void BookLibraryStore::updateCachedCoverAndPages(const std::string& file_path,
                                                 const std::string& cover_path,
                                                 int total_pages) {
    std::string fp = canonicalOrNormalized(file_path);
    if (fp.empty()) return;
    mutateAndPublish([&](nlohmann::json& doc) {
        nlohmann::json& bj = doc["books"][fp];
        if (!bj.is_object()) bj = nlohmann::json::object();
        bj["file_path"] = fp;
        bool changed = false;
        if (isRasterCoverFile(cover_path) && bj.value("cover_path", "") != cover_path) {
            bj["cover_path"] = cover_path;
            changed = true;
        }
        if (total_pages > 1 && bj.value("total_pages", 1) < total_pages) {
            bj["total_pages"] = total_pages;
            changed = true;
        }
        return changed;
    });
}

BookExcerpt BookLibraryStore::addExcerpt(const BookItem& book,
                                         int page_number,
                                         const std::string& chapter_title,
                                         const std::string& quote_text,
                                         const std::string& thought_text,
                                         const std::string& ai_analysis) {
    BookExcerpt ex;
    ex.quote_text = trimWs(quote_text);
    ex.thought_text = trimWs(thought_text);
    ex.ai_analysis = trimWs(ai_analysis);
    if (ex.quote_text.empty() && ex.thought_text.empty()) return ex;

    const int64_t ts = nowEpochMs();
    ex.id = "ex-" + hexU64(fnv1a64(book.id + ":" + ex.quote_text + ":" + std::to_string(ts))).substr(0, 12);
    ex.book_key = computeBookKey(book);
    ex.book_id = book.id;
    ex.source_id = book.source_id;
    ex.file_path = canonicalOrNormalized(book.file_path);
    ex.book_title = book.title;
    ex.author = book.author;
    ex.chapter_title = chapter_title;
    ex.page_number = std::max(1, page_number);
    ex.created_at_ms = ts;

    mutateAndPublish([&](nlohmann::json& doc) {
        doc["excerpts"].insert(doc["excerpts"].begin(), nlohmann::json{
            {"id", ex.id},
            {"book_key", ex.book_key},
            {"book_id", ex.book_id},
            {"source_id", ex.source_id},
            {"file_path", ex.file_path},
            {"book_title", ex.book_title},
            {"author", ex.author},
            {"chapter_title", ex.chapter_title},
            {"page_number", ex.page_number},
            {"quote_text", ex.quote_text},
            {"thought_text", ex.thought_text},
            {"ai_analysis", ex.ai_analysis},
            {"created_at_ms", ex.created_at_ms},
        });
        return true;
    });
    return ex;
}

bool BookLibraryStore::deleteExcerpt(const std::string& excerpt_id) {
    if (excerpt_id.empty()) return false;
    if (excerpt_id.rfind("ebook-note:", 0) == 0 && HostKernel::instance().isInitialized() &&
        HostKernel::instance().capabilities().has("doc.open.ebook.v1")) {
        const std::string raw_id = excerpt_id.substr(11);
        auto r = HostKernel::instance().capabilities().call("doc.open.ebook.v1", {
            {"action", "delete_note"},
            {"note_id", raw_id},
        });
        if (r.ok && HostKernel::instance().isInitialized()) {
            HostKernel::instance().bus().publish("entity.book.library_changed", {{"timestamp_ms", nowEpochMs()}});
        }
        return r.ok;
    }
    return mutateAndPublish([&](nlohmann::json& doc) {
        nlohmann::json next = nlohmann::json::array();
        bool removed = false;
        for (const auto& ej : doc["excerpts"]) {
            if (ej.is_object() && ej.value("id", "") == excerpt_id) {
                removed = true;
                continue;
            }
            next.push_back(ej);
        }
        if (removed) doc["excerpts"] = std::move(next);
        return removed;
    });
}

std::vector<BookExcerpt> BookLibraryStore::listAllMergedExcerpts() const {
    std::vector<BookExcerpt> out;
    {
        const std::string p = libraryJsonPath();
        ScopedFileLock guard(p, FileLock::Mode::Shared, 2000);
        nlohmann::json doc;
        loadUnlocked(doc);
        for (const auto& ej : doc["excerpts"]) {
            if (!ej.is_object()) continue;
            BookExcerpt ex;
            ex.id = ej.value("id", "");
            ex.book_key = ej.value("book_key", "");
            ex.book_id = ej.value("book_id", "");
            ex.source_id = ej.value("source_id", "");
            ex.file_path = ej.value("file_path", "");
            ex.book_title = ej.value("book_title", "");
            ex.author = ej.value("author", "");
            ex.chapter_title = ej.value("chapter_title", "");
            ex.page_number = std::max(1, ej.value("page_number", 1));
            ex.quote_text = ej.value("quote_text", "");
            ex.thought_text = ej.value("thought_text", "");
            ex.ai_analysis = ej.value("ai_analysis", "");
            ex.created_at_ms = ej.value("created_at_ms", int64_t{0});
            if (!ex.id.empty()) out.push_back(std::move(ex));
        }
    }

    // Merge SentenceNotes saved by plugin-ebook (notes.json)
    std::error_code ec;
    fs::path ebook_notes = fs::path(resolveDataRootDir()) / "plugin-data" / "plugin-ebook" / "notes.json";
    if (fs::is_regular_file(ebook_notes, ec)) {
        nlohmann::json nj = readJsonFileUnlocked(ebook_notes.string());
        const nlohmann::json* arr = nullptr;
        if (nj.is_array()) arr = &nj;
        else if (nj.is_object() && nj.contains("notes") && nj["notes"].is_array()) arr = &nj["notes"];
        if (arr) {
            for (const auto& item : *arr) {
                if (!item.is_object()) continue;
                std::string nid = item.value("id", "");
                if (nid.empty()) continue;
                BookExcerpt ex;
                ex.id = "ebook-note:" + nid;
                ex.source_id = "plugin-ebook";
                ex.quote_text = item.value("sentence", "");
                ex.thought_text = item.value("follow_up_question", "");
                ex.ai_analysis = item.value("ai_interpretation", "");
                ex.created_at_ms = item.value("updated_at_ms", item.value("created_at_ms", int64_t{0}));
                if (item.contains("context") && item["context"].is_object()) {
                    const auto& cx = item["context"];
                    ex.book_id = cx.value("book_id", "");
                    ex.book_title = cx.value("book_title", "");
                    ex.author = cx.value("book_author", "");
                    ex.chapter_title = cx.value("chapter_title", "");
                    ex.page_number = std::max(1, cx.value("para_index", 1));
                }
                if (ex.book_title.empty()) ex.book_title = "未关联网页/自由长难句";
                out.push_back(std::move(ex));
            }
        }
    }

    std::sort(out.begin(), out.end(), [](const BookExcerpt& a, const BookExcerpt& b) {
        return a.created_at_ms > b.created_at_ms;
    });
    return out;
}

std::vector<BookExcerpt> BookLibraryStore::listMergedExcerptsForBook(const BookItem& book) const {
    const auto all = listAllMergedExcerpts();
    const std::string key = computeBookKey(book);
    std::vector<BookExcerpt> matched;
    for (const auto& ex : all) {
        if ((!key.empty() && ex.book_key == key) ||
            (!book.id.empty() && ex.book_id == book.id) ||
            (!book.title.empty() && ex.book_title == book.title)) {
            matched.push_back(ex);
        }
    }
    return matched;
}

size_t BookLibraryStore::countExcerptsForBook(const BookItem& book,
                                              const std::vector<BookExcerpt>& all_excerpts) const {
    const std::string key = computeBookKey(book);
    size_t cnt = 0;
    for (const auto& ex : all_excerpts) {
        if ((!key.empty() && ex.book_key == key) ||
            (!book.id.empty() && ex.book_id == book.id) ||
            (!book.title.empty() && ex.book_title == book.title)) {
            ++cnt;
        }
    }
    return cnt;
}

bool BookLibraryStore::exportBookMarkdown(const BookItem& book,
                                          const std::vector<BookExcerpt>& excerpts,
                                          const std::vector<VocabCard>& all_vocab,
                                          std::string& out_saved_path,
                                          std::string& out_error) const {
    const char* home = std::getenv("HOME");
    fs::path out_dir = fs::path(home ? home : "/tmp") / "Documents" / "OmniDesk-Notes";
    std::error_code ec;
    fs::create_directories(out_dir, ec);
    if (ec) {
        out_error = "无法创建导出目录: " + ec.message();
        return false;
    }

    fs::path dst = out_dir / (sanitizeFilenameComponent(book.title) + ".md");
    std::ofstream ofs(dst, std::ios::binary | std::ios::trunc);
    if (!ofs.is_open()) {
        out_error = "无法写入 Markdown 文件: " + dst.string();
        return false;
    }

    const BookShelfEntry entry = entryForBook(book);
    const std::string st_lbl = statusDisplayLabel(effectiveStatus(book, entry));

    ofs << "---\n";
    ofs << "title: \"" << book.title << "\"\n";
    ofs << "author: \"" << book.author << "\"\n";
    ofs << "format: \"" << book.format << "\"\n";
    ofs << "status: \"" << st_lbl << "\"\n";
    ofs << "progress: \"" << static_cast<int>(book.progress_percent) << "% (P."
        << book.current_page << "/" << book.total_pages << ")\"\n";
    ofs << "exported_at: \"" << formatTimeDateShort(nowEpochMs()) << "\"\n";
    ofs << "tags:\n  - omnidesk/reading\n  - weread-notes\n";
    ofs << "---\n\n";

    ofs << "# 《" << book.title << "》读书笔记\n\n";
    ofs << "- **作者**: " << (book.author.empty() ? "未知" : book.author) << "\n";
    ofs << "- **阅读状态**: " << st_lbl << " · 进度 " << static_cast<int>(book.progress_percent)
        << "% (第 " << book.current_page << " / " << book.total_pages << " 页)\n";
    if (!book.file_path.empty()) {
        ofs << "- **源文件**: `" << book.file_path << "`\n";
    }
    ofs << "\n---\n\n";

    ofs << "## 书摘、划线与 AI 精读 (" << excerpts.size() << ")\n\n";
    if (excerpts.empty()) {
        ofs << "_本书暂无划线书摘记录。_\n\n";
    } else {
        for (const auto& ex : excerpts) {
            ofs << "### ";
            if (!ex.chapter_title.empty()) ofs << ex.chapter_title << " · ";
            ofs << "P." << ex.page_number;
            if (ex.created_at_ms > 0) ofs << " (" << formatTimeDateShort(ex.created_at_ms) << ")";
            ofs << "\n\n";
            if (!ex.quote_text.empty()) {
                ofs << "> " << ex.quote_text << "\n\n";
            }
            if (!ex.thought_text.empty()) {
                ofs << "**想法 / 批注**：" << ex.thought_text << "\n\n";
            }
            if (!ex.ai_analysis.empty()) {
                ofs << "**AI 语境精读**：\n\n" << ex.ai_analysis << "\n\n";
            }
            ofs << "---\n\n";
        }
    }

    std::vector<VocabCard> book_vocab;
    for (const auto& vc : all_vocab) {
        if (!book.title.empty() && !vc.source_book.empty() &&
            asciiLower(vc.source_book).find(asciiLower(book.title)) != std::string::npos) {
            book_vocab.push_back(vc);
        }
    }

    ofs << "## 本书生词与词根卡片 (" << book_vocab.size() << ")\n\n";
    if (book_vocab.empty()) {
        ofs << "_暂无关联本书的 SM-2 生词记录。_\n";
    } else {
        for (const auto& vc : book_vocab) {
            ofs << "- **" << vc.word << "**";
            if (!vc.phonetic.empty()) ofs << " `" << vc.phonetic << "`";
            if (!vc.meaning.empty()) ofs << " — " << vc.meaning;
            ofs << "\n";
            if (!vc.etymology.empty()) ofs << "  - 词源: " << vc.etymology << "\n";
            if (!vc.context_sentence.empty()) ofs << "  - 原句: *" << vc.context_sentence << "*\n";
        }
    }

    ofs.flush();
    if (!ofs.good()) {
        out_error = "保存导出内容时出现 I/O 错误";
        return false;
    }
    out_saved_path = dst.generic_string();
    return true;
}

bool BookLibraryStore::extractPdfCoverAndPagesSync(const std::string& pdf_path,
                                                   const std::string& covers_dir,
                                                   std::string& out_cover_path,
                                                   int& out_total_pages) {
    out_cover_path.clear();
    out_total_pages = 0;
    std::string canon = canonicalOrNormalized(pdf_path);
    std::error_code ec;
    if (canon.empty() || !fs::is_regular_file(canon, ec)) return false;

    fs::create_directories(covers_dir, ec);
    const std::string doc_hash = makePdfDocHash(canon);
    const fs::path final_png = fs::path(covers_dir) / ("pdf_" + doc_hash + ".png");

    if (fs::is_regular_file(final_png, ec) && fs::file_size(final_png, ec) > 64) {
        out_cover_path = final_png.generic_string();
    } else {
        const fs::path prefix = fs::path(covers_dir) / (".tmp_pdf_" + doc_hash + "_" + std::to_string(::getpid()));
        const fs::path gen_png = fs::path(prefix.string() + ".png");
        std::string dummy;
        if (runCommandCapture({"pdftoppm", "-png", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "420",
                               canon, prefix.string()},
                              dummy, 9000) &&
            fs::is_regular_file(gen_png, ec)) {
            fs::rename(gen_png, final_png, ec);
            if (!ec && fs::is_regular_file(final_png, ec)) {
                out_cover_path = final_png.generic_string();
            } else {
                fs::remove(gen_png, ec);
            }
        } else {
            fs::remove(gen_png, ec);
        }
    }

    std::string info_out;
    if (runCommandCapture({"pdfinfo", canon}, info_out, 4000)) {
        std::istringstream iss(info_out);
        std::string line;
        while (std::getline(iss, line)) {
            if (line.rfind("Pages:", 0) == 0) {
                std::string num = trimWs(line.substr(6));
                try {
                    out_total_pages = std::max(1, std::stoi(num));
                } catch (...) {}
                break;
            }
        }
    }

    return !out_cover_path.empty() || out_total_pages > 0;
}

std::string BookLibraryStore::lookupCachedPdfPagePng(const std::string& pdf_path, int page_number) {
    std::string canon = canonicalOrNormalized(pdf_path);
    if (canon.empty()) return "";
    struct stat st {};
    if (::stat(canon.c_str(), &st) != 0 || !S_ISREG(st.st_mode)) return "";

    const int safe_page = std::max(1, page_number);
    const std::string doc_hash = makePdfDocHash(canon);
    char fname[64]{};
    std::snprintf(fname, sizeof(fname), "orig_p%04d_%lld.png",
                  safe_page, static_cast<long long>(st.st_mtime));

    const fs::path data_root(resolveDataRootDir());
    std::error_code ec;
    const fs::path plugin_png =
        data_root / "plugin-data" / "plugin-pdf-babeldoc" / "cache" / doc_hash / "pages" / fname;
    if (fs::is_regular_file(plugin_png, ec) && fs::file_size(plugin_png, ec) > 64) {
        return plugin_png.generic_string();
    }
    const fs::path host_png = data_root / "pdf-pages" / doc_hash / fname;
    if (fs::is_regular_file(host_png, ec) && fs::file_size(host_png, ec) > 64) {
        return host_png.generic_string();
    }
    return "";
}

bool BookLibraryStore::renderPdfPagePngSync(const std::string& pdf_path,
                                            int page_number,
                                            std::string& out_png_path,
                                            int& out_total_pages) {
    out_png_path.clear();
    out_total_pages = 0;
    std::string canon = canonicalOrNormalized(pdf_path);
    if (canon.empty()) return false;
    struct stat st {};
    if (::stat(canon.c_str(), &st) != 0 || !S_ISREG(st.st_mode)) return false;

    const int safe_page = std::max(1, page_number);
    out_png_path = lookupCachedPdfPagePng(canon, safe_page);
    if (out_png_path.empty()) {
        std::error_code ec;
        const std::string doc_hash = makePdfDocHash(canon);
        const fs::path pages_dir = fs::path(resolveDataRootDir()) / "pdf-pages" / doc_hash;
        fs::create_directories(pages_dir, ec);

        char fname[64]{};
        std::snprintf(fname, sizeof(fname), "orig_p%04d_%lld.png",
                      safe_page, static_cast<long long>(st.st_mtime));
        const fs::path final_png = pages_dir / fname;

        static std::atomic<uint64_t> tmp_seq{1};
        const uint64_t seq = tmp_seq.fetch_add(1, std::memory_order_relaxed);
        const fs::path prefix = pages_dir / (".tmp_p" + std::to_string(safe_page) + "_" +
                                             std::to_string(::getpid()) + "_" + std::to_string(seq));
        const fs::path gen_png = fs::path(prefix.string() + ".png");
        const std::string page_str = std::to_string(safe_page);
        std::string dummy;
        if (runCommandCapture({"pdftoppm", "-png", "-f", page_str, "-l", page_str,
                               "-singlefile", "-scale-to", "2000", canon, prefix.string()},
                              dummy, 15000) &&
            fs::is_regular_file(gen_png, ec) && fs::file_size(gen_png, ec) > 64) {
            fs::rename(gen_png, final_png, ec);
            if (!ec && fs::is_regular_file(final_png, ec)) {
                out_png_path = final_png.generic_string();
            } else {
                fs::remove(gen_png, ec);
            }
        } else {
            fs::remove(gen_png, ec);
        }
    }

    std::string info_out;
    if (runCommandCapture({"pdfinfo", canon}, info_out, 4000)) {
        std::istringstream iss(info_out);
        std::string line;
        while (std::getline(iss, line)) {
            if (line.rfind("Pages:", 0) == 0) {
                std::string num = trimWs(line.substr(6));
                try {
                    out_total_pages = std::max(1, std::stoi(num));
                } catch (...) {}
                break;
            }
        }
    }

    return !out_png_path.empty();
}

std::vector<LocalCandidateBook> BookLibraryStore::scanCandidateBooksInDir(const std::string& dir_path,
                                                                          int max_depth) {
    std::vector<LocalCandidateBook> out;
    std::string canon_dir = canonicalOrNormalized(dir_path);
    std::error_code ec;
    if (canon_dir.empty() || !fs::is_directory(canon_dir, ec)) return out;

    fs::recursive_directory_iterator it(canon_dir, fs::directory_options::skip_permission_denied, ec);
    fs::recursive_directory_iterator end;
    for (; it != end; it.increment(ec)) {
        if (ec) {
            ec.clear();
            continue;
        }
        if (it.depth() > max_depth) {
            it.disable_recursion_pending();
            continue;
        }
        if (it->is_symlink(ec)) {
            if (it->is_directory(ec)) it.disable_recursion_pending();
            continue;
        }
        const auto& p = it->path();
        std::string fname = p.filename().string();
        if (!fname.empty() && fname.front() == '.') {
            if (it->is_directory(ec)) it.disable_recursion_pending();
            continue;
        }
        if (!it->is_regular_file(ec)) continue;
        std::string ext = asciiLower(p.extension().string());
        if (!isSupportedBookExt(ext)) continue;

        LocalCandidateBook cand;
        cand.file_path = canonicalOrNormalized(p.string());
        cand.format = ext.substr(1);
        if (cand.format == "azw" || cand.format == "prc") cand.format = "mobi";
        if (cand.format == "md") cand.format = "txt";
        parseCleanFilenameTitleAuthor(p.stem().string(),
                                      asciiUpper(cand.format) + " 文档",
                                      cand.title, cand.author);
        cand.size_bytes = static_cast<int64_t>(fs::file_size(p, ec));
        auto ftime = fs::last_write_time(p, ec);
        if (!ec) {
            auto sctp = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
                ftime - fs::file_time_type::clock::now() + std::chrono::system_clock::now());
            cand.modified_epoch = std::chrono::duration_cast<std::chrono::seconds>(sctp.time_since_epoch()).count();
        }
        out.push_back(std::move(cand));
        if (out.size() >= 180) break;
    }

    std::sort(out.begin(), out.end(), [](const LocalCandidateBook& a, const LocalCandidateBook& b) {
        if (a.modified_epoch != b.modified_epoch) return a.modified_epoch > b.modified_epoch;
        return a.title < b.title;
    });
    return out;
}

// ============================================================================
// Reusable WeRead-Style UI Widgets
// ============================================================================

brls::Box* makeWeReadCoverBox(const BookItem& book,
                              const BookShelfEntry& entry,
                              float cover_height,
                              bool compact_thumb) {
    std::string cover_file = isRasterCoverFile(book.cover_path) ? book.cover_path
                                                                : (isRasterCoverFile(entry.cover_path) ? entry.cover_path : "");

    auto* frame = new brls::Box(brls::Axis::ROW);
    frame->setHeight(cover_height);
    if (compact_thumb) {
        frame->setWidth(std::round(cover_height * 0.70f));
        frame->setShrink(0.0f);
    } else {
        frame->setWidthPercentage(100);
    }
    frame->setCornerRadius(compact_thumb ? 6.0f : 8.0f);
    frame->setClipsToBounds(true);
    frame->setBorderThickness(1.0f);
    frame->setBorderColor(ui::color("desktop/line"));

    if (!cover_file.empty()) {
        frame->setBackgroundColor(ui::color("desktop/soft"));
        auto* col = new brls::Box(brls::Axis::COLUMN);
        col->setWidthPercentage(100);
        col->setHeightPercentage(100);

        const std::string img_url = ImageCache::normalizeImageUrl(cover_file);
        auto* img = new CachedImageView();
        img->setWidthPercentage(100);
        img->setHeightPercentage(100);
        img->setScalingType(brls::ImageScalingType::FIT);
        img->setImageUrl(img_url);
        col->addView(img);
        frame->addView(col);
        return frame;
    }

    // Editorial WeRead Typographic Bookcloth Fallback (with tactile left spine groove)
    static const struct { uint8_t r, g, b; uint8_t sr, sg, sb; } kPalettes[] = {
        {34, 49, 68,   23, 35, 50},   // Slate Navy
        {56, 44, 62,   39, 30, 44},   // Plum Ink
        {37, 58, 52,   25, 41, 36},   // Pine Cloth
        {66, 48, 38,   46, 33, 25},   // Warm Umber
        {44, 52, 66,   30, 36, 48},   // Oxford Charcoal
        {62, 42, 48,   44, 28, 33},   // Burgundy Binding
    };
    const size_t pal_idx = static_cast<size_t>(fnv1a64(book.title) % 6);
    const auto& pal = kPalettes[pal_idx];

    frame->setBackgroundColor(nvgRGB(pal.r, pal.g, pal.b));

    auto* spine = new brls::Box(brls::Axis::COLUMN);
    spine->setWidth(compact_thumb ? 5.0f : 9.0f);
    spine->setHeightPercentage(100);
    spine->setShrink(0.0f);
    spine->setBackgroundColor(nvgRGB(pal.sr, pal.sg, pal.sb));
    frame->addView(spine);

    auto* body = new brls::Box(brls::Axis::COLUMN);
    body->setGrow(1.0f);
    body->setShrink(1.0f);
    body->setHeightPercentage(100);
    body->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    body->setPadding(compact_thumb ? 6.0f : 12.0f);

    std::string fmt = asciiUpper(book.format.empty() ? "BOOK" : book.format);
    if (compact_thumb) {
        auto* tiny_t = new brls::Label();
        tiny_t->setText(clipUtf8(book.title, 22));
        tiny_t->setFontSize(11.0f);
        tiny_t->setLineHeight(1.22f);
        tiny_t->setTextColor(nvgRGBA(244, 246, 250, 235));
        body->addView(tiny_t);

        auto* tiny_f = new brls::Label();
        tiny_f->setText(fmt);
        tiny_f->setFontSize(9.5f);
        tiny_f->setTextColor(nvgRGBA(200, 210, 225, 190));
        body->addView(tiny_f);
    } else {
        auto* top_row = new brls::Box(brls::Axis::ROW);
        top_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        auto* fmt_lbl = new brls::Label();
        fmt_lbl->setText(fmt + (entry.pinned ? " · 置顶" : ""));
        fmt_lbl->setFontSize(11.5f);
        fmt_lbl->setTextColor(nvgRGBA(212, 222, 236, 210));
        top_row->addView(fmt_lbl);
        body->addView(top_row);

        auto* title_lbl = new brls::Label();
        title_lbl->setText(clipUtf8(book.title, 68));
        title_lbl->setFontSize(15.5f);
        title_lbl->setLineHeight(1.36f);
        title_lbl->setTextColor(nvgRGB(248, 250, 252));
        body->addView(title_lbl);

        auto* foot_lbl = new brls::Label();
        foot_lbl->setText(clipUtf8(book.author.empty() ? "OmniDesk Library" : book.author, 36));
        foot_lbl->setFontSize(12.0f);
        foot_lbl->setSingleLine(true);
        foot_lbl->setTextColor(nvgRGBA(203, 213, 225, 205));
        body->addView(foot_lbl);
    }

    frame->addView(body);
    return frame;
}

void openWeReadImportDialog(const std::string& current_group_id,
                            std::function<void(const std::vector<std::string>& imported_paths,
                                               bool open_first)> on_imported) {
    if (!brls::Application::getPlatform()) return;

    struct ImportState {
        std::string active_dir;
        std::string filter_ext;
        std::vector<LocalCandidateBook> candidates;
        std::unordered_set<std::string> selected_paths;
        brls::Dialog* dialog = nullptr;
        brls::Label* dir_label = nullptr;
        brls::Box* list_box = nullptr;
        brls::Button* import_sel_btn = nullptr;
    };
    auto st = std::make_shared<ImportState>();
    const char* home = std::getenv("HOME");
    const std::string home_s = home ? std::string(home) : "/tmp";

    std::error_code ec;
    fs::path dl_books = fs::path(home_s) / "Downloads" / "omnidesk-books";
    fs::path dl_dir = fs::path(home_s) / "Downloads";
    if (fs::is_directory(dl_books, ec) && !BookLibraryStore::scanCandidateBooksInDir(dl_books.string(), 1).empty()) {
        st->active_dir = dl_books.generic_string();
    } else if (fs::is_directory(dl_dir, ec)) {
        st->active_dir = dl_dir.generic_string();
    } else {
        st->active_dir = home_s;
    }

    auto* root = new brls::Box(brls::Axis::COLUMN);
    const float dlg_w = std::clamp(
        (brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f) - 180.0f,
        740.0f, 980.0f);
    root->setWidth(dlg_w);
    root->setHeight(580.0f);
    root->setPadding(20, 24, 14, 24);

    root->addView(ui::makeLabel("导入本地图书与关联常驻书库目录", 20.0f, "brls/text"));
    st->dir_label = ui::makeLabel("", 13.5f, "font/grey");
    st->dir_label->setMarginTop(4);
    st->dir_label->setMarginBottom(10);
    root->addView(st->dir_label);

    auto rebuildList = std::make_shared<std::function<void(bool)>>();

    // Preset directory bar
    auto* preset_row = new brls::Box(brls::Axis::ROW);
    YGNodeStyleSetFlexWrap(preset_row->getYGNode(), YGWrapWrap);
    preset_row->setShrink(0.0f);
    preset_row->setMarginBottom(8);

    const std::pair<std::string, std::string> presets[] = {
        {"~/Downloads", (fs::path(home_s) / "Downloads").generic_string()},
        {"Z-Lib 下载目录", (fs::path(home_s) / "Downloads" / "omnidesk-books").generic_string()},
        {"~/Books", (fs::path(home_s) / "Books").generic_string()},
        {"~/Documents", (fs::path(home_s) / "Documents").generic_string()},
    };
    for (const auto& [lbl, dir_p] : presets) {
        auto* pb = ui::makeButton(lbl, [st, dir_p, rebuildList]() {
            st->active_dir = dir_p;
            st->selected_paths.clear();
            (*rebuildList)(true);
        }, false, 32.0f);
        pb->setMarginRight(6);
        pb->setMarginBottom(6);
        preset_row->addView(pb);
    }

    auto* custom_dir_btn = ui::makeButton("输入路径 / 单文件...", [st, current_group_id, on_imported, rebuildList]() {
        if (auto* plat = brls::Application::getPlatform()) {
            plat->getImeManager()->openForText(
                [st, current_group_id, on_imported, rebuildList](std::string raw) {
                    brls::sync([st, current_group_id, on_imported, rebuildList, raw]() {
                        std::string p = canonicalOrNormalized(raw);
                        if (p.empty()) return;
                        std::error_code ec2;
                        if (fs::is_directory(p, ec2)) {
                            st->active_dir = p;
                            st->selected_paths.clear();
                            (*rebuildList)(true);
                        } else if (fs::is_regular_file(p, ec2)) {
                            BookLibraryStore::instance().registerImportedFile(p, current_group_id);
                            if (st->dialog) st->dialog->close();
                            if (on_imported) on_imported({p}, true);
                        } else {
                            brls::Application::notify("路径不存在: " + p);
                        }
                    });
                },
                "输入文件夹路径或电子书/PDF 文件路径",
                "支持 ~/Downloads、绝对目录或单个 .epub/.pdf/.mobi/.azw3 文件",
                256,
                st->active_dir);
        }
    }, false, 32.0f);
    custom_dir_btn->setMarginRight(6);
    custom_dir_btn->setMarginBottom(6);
    preset_row->addView(custom_dir_btn);
    root->addView(preset_row);

    // Action & Format row
    auto* act_row = new brls::Box(brls::Axis::ROW);
    act_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    act_row->setAlignItems(brls::AlignItems::CENTER);
    act_row->setShrink(0.0f);
    act_row->setMarginBottom(10);

    auto* fmt_chips = new brls::Box(brls::Axis::ROW);
    for (const auto& [ext_v, ext_lbl] : std::vector<std::pair<std::string, std::string>>{
             {"", "全部"}, {"epub", "EPUB"}, {"pdf", "PDF"}, {"mobi", "MOBI/AZW3"}, {"txt", "TXT"}}) {
        auto* fb = ui::makeButton(ext_lbl, [st, ext_v, rebuildList]() {
            st->filter_ext = ext_v;
            (*rebuildList)(false);
        }, false, 30.0f);
        fb->setMarginRight(6);
        fmt_chips->addView(fb);
    }
    act_row->addView(fmt_chips);

    auto* batch_btns = new brls::Box(brls::Axis::ROW);
    auto* sel_all_btn = ui::makeButton("全选当前列表", [st, rebuildList]() {
        for (const auto& c : st->candidates) {
            if (!st->filter_ext.empty() && c.format != st->filter_ext &&
                !(st->filter_ext == "mobi" && c.format == "azw3")) {
                continue;
            }
            st->selected_paths.insert(c.file_path);
        }
        (*rebuildList)(false);
    }, false, 32.0f);
    sel_all_btn->setMarginRight(8);
    batch_btns->addView(sel_all_btn);

    auto* watch_btn = ui::makeButton("关联为常驻扫描目录", [st, on_imported]() {
        if (BookLibraryStore::instance().addWatchedDir(st->active_dir)) {
            brls::Application::notify("已将目录加入常驻自动扫描书库: " + st->active_dir);
        } else {
            brls::Application::notify("该目录已在常驻扫描列表中或不可访问");
        }
        if (st->dialog) st->dialog->close();
        if (on_imported) on_imported({}, false);
    }, false, 32.0f);
    watch_btn->setMarginRight(8);
    batch_btns->addView(watch_btn);

    st->import_sel_btn = ui::makeButton("导入所选 (0)", [st, current_group_id, on_imported]() {
        if (st->selected_paths.empty()) {
            brls::Application::notify("请先点击勾选要导入的书籍，或点击单行右侧「导入阅读」");
            return;
        }
        std::vector<std::string> list(st->selected_paths.begin(), st->selected_paths.end());
        std::sort(list.begin(), list.end());
        for (const auto& fp : list) {
            BookLibraryStore::instance().registerImportedFile(fp, current_group_id);
        }
        if (st->dialog) st->dialog->close();
        brls::Application::notify("已导入 " + std::to_string(list.size()) + " 本书籍至书架");
        if (on_imported) on_imported(list, false);
    }, true, 32.0f);
    batch_btns->addView(st->import_sel_btn);
    act_row->addView(batch_btns);
    root->addView(act_row);

    auto* scroll = new brls::ScrollingFrame();
    scroll->setGrow(1.0f);
    scroll->setShrink(1.0f);
    scroll->setMinHeight(0);
    scroll->setWidthPercentage(100);
    st->list_box = new brls::Box(brls::Axis::COLUMN);
    st->list_box->setWidthPercentage(100);
    scroll->setContentView(st->list_box);
    root->addView(scroll);

    *rebuildList = [st, current_group_id, on_imported, rebuildList](bool rescan) {
        if (rescan) {
            st->candidates = BookLibraryStore::scanCandidateBooksInDir(st->active_dir, 1);
        }
        if (st->dir_label) {
            st->dir_label->setText("当前浏览目录: " + st->active_dir +
                                   " · 发现 " + std::to_string(st->candidates.size()) + " 本可导入图书");
        }
        if (st->import_sel_btn) {
            st->import_sel_btn->setText("导入所选 (" + std::to_string(st->selected_paths.size()) + ")");
        }
        st->list_box->clearViews();
        int shown = 0;
        for (const auto& cand : st->candidates) {
            if (!st->filter_ext.empty() && cand.format != st->filter_ext &&
                !(st->filter_ext == "mobi" && cand.format == "azw3")) {
                continue;
            }
            const bool checked = st->selected_paths.count(cand.file_path) > 0;
            auto* row = ui::makeCard(checked ? "desktop/soft" : "desktop/surface", 10.0f, 8.0f);
            row->setAxis(brls::Axis::ROW);
            row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            row->setAlignItems(brls::AlignItems::CENTER);
            row->setMarginBottom(6);

            auto* left = new brls::Box(brls::Axis::COLUMN);
            left->setGrow(1.0f);
            left->setShrink(1.0f);
            left->setMarginRight(10);
            std::string prefix = checked ? "[已勾选] " : "[  ] ";
            left->addView(ui::makeLabel(prefix + clipUtf8(cand.title, 86), 14.5f,
                                        checked ? "domain/book" : "brls/text"));
            left->addView(ui::makeLabel(
                asciiUpper(cand.format) + " · " + cand.author +
                    (cand.size_bytes > 0 ? (" · " + formatFileSizeShort(cand.size_bytes)) : ""),
                12.5f, "font/grey"));
            row->addView(left);

            auto* btns = new brls::Box(brls::Axis::ROW);
            btns->setShrink(0.0f);
            auto* tog_btn = ui::makeButton(checked ? "取消勾选" : "勾选入架",
                [st, fp = cand.file_path, rebuildList]() {
                    if (st->selected_paths.count(fp)) st->selected_paths.erase(fp);
                    else st->selected_paths.insert(fp);
                    (*rebuildList)(false);
                }, checked, 30.0f);
            tog_btn->setMarginRight(6);
            btns->addView(tog_btn);

            auto* open_now = ui::makeButton("导入并阅读", [st, fp = cand.file_path, current_group_id, on_imported]() {
                BookLibraryStore::instance().registerImportedFile(fp, current_group_id);
                if (st->dialog) st->dialog->close();
                if (on_imported) on_imported({fp}, true);
            }, false, 30.0f);
            btns->addView(open_now);
            row->addView(btns);

            st->list_box->addView(row);
            ++shown;
        }
        if (shown == 0) {
            auto* empty = ui::makeCard("desktop/surface", 16.0f, 10.0f);
            empty->addView(ui::makeLabel(
                "此目录下未找到匹配的电子书或 PDF 文件 (.epub / .pdf / .mobi / .azw3 / .fb2 / .txt)。",
                14.5f, "font/grey"));
            st->list_box->addView(empty);
        }
    };

    (*rebuildList)(true);

    auto* dialog = new brls::Dialog(root);
    st->dialog = dialog;
    if (auto* applet = dynamic_cast<brls::Box*>(dialog->getView("brls/dialog/applet"))) {
        applet->setWidth(dlg_w);
        applet->setCornerRadius(14.0f);
        applet->setBorderThickness(1.0f);
        applet->setBorderColor(ui::color("desktop/line"));
        applet->setBackgroundColor(ui::color("brls/background"));
        applet->setClipsToBounds(true);
    }
    dialog->addButton("关闭 Esc", []() {});
    dialog->open();
}

void openWeReadBookDetailDialog(const BookItem& book,
                                const std::vector<VocabCard>& all_vocab,
                                std::function<void(const BookItem&, int page)> on_open_reader,
                                std::function<void(const BookItem&)> on_open_notebook,
                                std::function<void()> on_changed) {
    if (!brls::Application::getPlatform()) return;

    struct DetailState {
        brls::Dialog* dialog = nullptr;
        brls::Box* body = nullptr;
    };
    auto st = std::make_shared<DetailState>();

    const float dlg_w = std::clamp(
        (brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f) - 240.0f,
        680.0f, 860.0f);
    auto* root = new brls::Box(brls::Axis::COLUMN);
    root->setWidth(dlg_w);
    root->setPadding(20, 24, 14, 24);

    st->body = new brls::Box(brls::Axis::COLUMN);
    st->body->setWidthPercentage(100);
    root->addView(st->body);

    auto rebuild = std::make_shared<std::function<void()>>();
    *rebuild = [st, book, all_vocab, on_open_reader, on_open_notebook, on_changed, rebuild]() {
        st->body->clearViews();
        auto& lib = BookLibraryStore::instance();
        const BookShelfEntry entry = lib.entryForBook(book);
        const BookReadingStatus eff_st = BookLibraryStore::effectiveStatus(book, entry);
        const auto groups = lib.listGroups();
        const auto excerpts = lib.listMergedExcerptsForBook(book);

        auto* top = new brls::Box(brls::Axis::ROW);
        top->setMarginBottom(14);
        auto* cov = makeWeReadCoverBox(book, entry, 168.0f, true);
        cov->setMarginRight(18);
        top->addView(cov);

        auto* info = new brls::Box(brls::Axis::COLUMN);
        info->setGrow(1.0f);
        info->setShrink(1.0f);
        info->addView(ui::makeLabel(book.title, 19.5f, "brls/text", 1.35f));
        auto* auth = ui::makeLabel("作者: " + (book.author.empty() ? "未知作者" : book.author) +
                                       "  ·  格式: " + asciiUpper(book.format),
                                   14.0f, "font/grey");
        auth->setMarginTop(4);
        info->addView(auth);

        std::ostringstream stat_oss;
        stat_oss << "当前状态: " << BookLibraryStore::statusDisplayLabel(eff_st)
                 << "  ·  进度: P." << entry.current_page << "/" << std::max(1, entry.total_pages)
                 << " (" << static_cast<int>(book.progress_percent) << "%)"
                 << "  ·  本书笔记: " << excerpts.size() << " 条";
        if (entry.last_read_at_ms > 0) {
            stat_oss << "\n上次阅读: " << formatTimeDateShort(entry.last_read_at_ms);
            if (!entry.last_chapter_title.empty()) stat_oss << " · " << entry.last_chapter_title;
        }
        auto* stat_lbl = ui::makeLabel(stat_oss.str(), 13.5f, "domain/book", 1.45f);
        stat_lbl->setMarginTop(6);
        info->addView(stat_lbl);

        if (!book.file_path.empty()) {
            auto* path_lbl = ui::makeLabel(clipUtf8(book.file_path, 96), 12.0f, "font/grey");
            path_lbl->setMarginTop(4);
            info->addView(path_lbl);
        }

        auto* primary_btns = new brls::Box(brls::Axis::ROW);
        YGNodeStyleSetFlexWrap(primary_btns->getYGNode(), YGWrapWrap);
        primary_btns->setMarginTop(10);

        auto* read_btn = ui::makeButton("继续阅读 ▸", [st, book, entry, on_open_reader]() {
            if (st->dialog) st->dialog->close();
            if (on_open_reader) on_open_reader(book, std::max(1, entry.current_page));
        }, true, 36.0f);
        read_btn->setMarginRight(8);
        primary_btns->addView(read_btn);

        auto* note_btn = ui::makeButton("查看本书笔记 (" + std::to_string(excerpts.size()) + ")",
            [st, book, on_open_notebook]() {
                if (st->dialog) st->dialog->close();
                if (on_open_notebook) on_open_notebook(book);
            }, false, 36.0f);
        note_btn->setMarginRight(8);
        primary_btns->addView(note_btn);

        auto* pin_btn = ui::makeButton(entry.pinned ? "取消置顶" : "置顶到书架",
            [book, next_pin = !entry.pinned, on_changed, rebuild]() {
                BookLibraryStore::instance().setBookPinned(book, next_pin);
                if (on_changed) on_changed();
                (*rebuild)();
            }, false, 36.0f);
        pin_btn->setMarginRight(8);
        primary_btns->addView(pin_btn);

        auto* exp_btn = ui::makeButton("导出 Markdown (Obsidian)", [book, excerpts, all_vocab]() {
            std::string saved, err;
            if (BookLibraryStore::instance().exportBookMarkdown(book, excerpts, all_vocab, saved, err)) {
                brls::Application::notify("笔记已导出至: " + saved);
            } else {
                brls::Application::notify("导出失败: " + err);
            }
        }, false, 36.0f);
        primary_btns->addView(exp_btn);

        info->addView(primary_btns);
        top->addView(info);
        st->body->addView(top);

        // Status section
        auto* st_card = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        st_card->setMarginBottom(10);
        st_card->addView(ui::makeLabel("阅读状态（微信读书式书架流转）", 14.0f, "font/grey"));
        auto* st_row = new brls::Box(brls::Axis::ROW);
        st_row->setMarginTop(8);
        for (const auto& [code, lbl] : std::vector<std::pair<std::string, std::string>>{
                 {"unread", "标记未读"}, {"reading", "标记在读"}, {"finished", "标记读完"}, {"auto", "按页码自动判断"}}) {
            const bool active = (code == "auto" && entry.status_override == "auto") ||
                                (code == "unread" && eff_st == BookReadingStatus::Unread && entry.status_override == "unread") ||
                                (code == "reading" && eff_st == BookReadingStatus::Reading && entry.status_override == "reading") ||
                                (code == "finished" && eff_st == BookReadingStatus::Finished && entry.status_override == "finished");
            auto* sb = ui::makeButton(lbl, [book, code, on_changed, rebuild]() {
                BookLibraryStore::instance().setBookStatus(book, code);
                if (on_changed) on_changed();
                (*rebuild)();
            }, active, 32.0f);
            sb->setMarginRight(8);
            st_row->addView(sb);
        }
        st_card->addView(st_row);
        st->body->addView(st_card);

        // Folder / Group assignment section
        auto* grp_card = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        grp_card->setMarginBottom(10);
        grp_card->addView(ui::makeLabel("所属书架分组（文件夹）", 14.0f, "font/grey"));
        auto* grp_row = new brls::Box(brls::Axis::ROW);
        YGNodeStyleSetFlexWrap(grp_row->getYGNode(), YGWrapWrap);
        grp_row->setMarginTop(8);

        auto* ungrp = ui::makeButton("未分组", [book, on_changed, rebuild]() {
            BookLibraryStore::instance().setBookGroup(book, "");
            if (on_changed) on_changed();
            (*rebuild)();
        }, entry.group_id.empty(), 32.0f);
        ungrp->setMarginRight(6);
        ungrp->setMarginBottom(6);
        grp_row->addView(ungrp);

        for (const auto& g : groups) {
            auto* gb = ui::makeButton(g.name, [book, gid = g.id, on_changed, rebuild]() {
                BookLibraryStore::instance().setBookGroup(book, gid);
                if (on_changed) on_changed();
                (*rebuild)();
            }, entry.group_id == g.id, 32.0f);
            gb->setMarginRight(6);
            gb->setMarginBottom(6);
            grp_row->addView(gb);
        }

        auto* new_grp_btn = ui::makeButton("+ 新建分组并移入", [book, on_changed, rebuild]() {
            if (auto* plat = brls::Application::getPlatform()) {
                plat->getImeManager()->openForText(
                    [book, on_changed, rebuild](std::string gname) {
                        brls::sync([book, gname, on_changed, rebuild]() {
                            if (trimWs(gname).empty()) return;
                            auto g = BookLibraryStore::instance().createGroup(gname);
                            if (!g.id.empty()) {
                                BookLibraryStore::instance().setBookGroup(book, g.id);
                                if (on_changed) on_changed();
                                (*rebuild)();
                            }
                        });
                    },
                    "新建书架分组",
                    "例如：深度学习、英文原著、物理学教材",
                    48,
                    "");
            }
        }, false, 32.0f);
        new_grp_btn->setMarginBottom(6);
        grp_row->addView(new_grp_btn);
        grp_card->addView(grp_row);
        st->body->addView(grp_card);

        // Danger / Remove from shelf row (never deletes source file on disk)
        auto* foot_row = new brls::Box(brls::Axis::ROW);
        foot_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        foot_row->setAlignItems(brls::AlignItems::CENTER);
        foot_row->addView(ui::makeLabel("移出书架仅隐藏书架卡片，绝不删除磁盘原文件。", 12.5f, "font/grey"));
        foot_row->addView(ui::makeButton("从书架移出", [st, book, on_changed]() {
            BookLibraryStore::instance().setBooksRemovedFromShelf({book}, true);
            if (st->dialog) st->dialog->close();
            brls::Application::notify("已从书架移出《" + book.title + "》");
            if (on_changed) on_changed();
        }, false, 30.0f));
        st->body->addView(foot_row);
    };

    (*rebuild)();

    auto* dialog = new brls::Dialog(root);
    st->dialog = dialog;
    if (auto* applet = dynamic_cast<brls::Box*>(dialog->getView("brls/dialog/applet"))) {
        applet->setWidth(dlg_w);
        applet->setCornerRadius(14.0f);
        applet->setBorderThickness(1.0f);
        applet->setBorderColor(ui::color("desktop/line"));
        applet->setBackgroundColor(ui::color("brls/background"));
        applet->setClipsToBounds(true);
    }
    dialog->addButton("关闭 Esc", []() {});
    dialog->open();
}

void promptAddBookExcerptDialog(const BookItem& book,
                                int page_number,
                                const std::string& chapter_title,
                                const std::string& initial_quote,
                                std::function<void()> on_saved) {
    auto* plat = brls::Application::getPlatform();
    if (!plat) return;

    plat->getImeManager()->openForText(
        [book, page_number, chapter_title, on_saved](std::string quote) {
            brls::sync([book, page_number, chapter_title, quote = std::move(quote), on_saved]() {
                if (trimWs(quote).empty()) return;
                if (auto* plat2 = brls::Application::getPlatform()) {
                    plat2->getImeManager()->openForText(
                        [book, page_number, chapter_title, quote, on_saved](std::string thought) {
                            brls::sync([book, page_number, chapter_title, quote, thought = std::move(thought), on_saved]() {
                                BookLibraryStore::instance().addExcerpt(
                                    book, page_number, chapter_title, quote, thought);
                                brls::Application::notify("已保存书摘与想法至笔记本");
                                if (on_saved) on_saved();
                            });
                        },
                        "写下你的读书想法 / 批注（可留空直接保存划线）",
                        "类似微信读书「写想法」，将归档在该书笔记本下",
                        512,
                        "");
                }
            });
        },
        "摘录原文划线 · 《" + clipUtf8(book.title, 36) + "》 P." + std::to_string(std::max(1, page_number)),
        "输入或确认原文句子",
        1024,
        initial_quote);
}

}  // namespace omnidesk::host
