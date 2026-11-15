#pragma once

#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>
#include <filesystem>

namespace ai_curator {

class CacheLock {
public:
    explicit CacheLock(const std::filesystem::path& path);
    ~CacheLock();
    CacheLock(const CacheLock&) = delete;
    CacheLock& operator=(const CacheLock&) = delete;
private:
    int descriptor_ = -1;
};

void requireSafePath(const std::filesystem::path& path);
std::string sha256(const std::string& text);
std::string gitBlobSha1(const std::string& bytes);
std::string canonicalJsonCompact(const nlohmann::json& j);
bool safeIdentifier(const std::string& text);

struct ProcessResult {
    int exit_code = -1;
    std::string stdout_text;
    std::string stderr_text;
    bool timed_out = false;
    std::string spawn_error;
};

std::string trim(const std::string& s);
std::string toLowerAscii(const std::string& s);
bool startsWith(const std::string& s, const std::string& prefix);
bool endsWith(const std::string& s, const std::string& suffix);
std::string normalizeForDedup(const std::string& s);
std::string sanitizeFilenameComponent(const std::string& s);

std::string currentShanghaiDate();
bool isValidIsoDate(const std::string& s);

std::optional<std::string> readFileText(const std::string& path);
bool writeFileAtomicLocked(const std::string& path, const std::string& content, std::string* error_out = nullptr);
bool writeFileAtomic(const std::string& path, const std::string& content, std::string* error_out = nullptr);
std::optional<nlohmann::json> readJsonFile(const std::string& path, std::string* error_out = nullptr);
bool writeJsonFileAtomicLocked(const std::string& path, const nlohmann::json& j, int indent = 2, std::string* error_out = nullptr);
bool writeJsonFileAtomic(const std::string& path, const nlohmann::json& j, int indent = 2, std::string* error_out = nullptr);

std::string extractYouTubeVideoId(const std::string& url);
std::string extractAppleMusicSongId(const std::string& url);
std::string extractRedditPostId(const std::string& url);
std::string extractDoiFromText(const std::string& text);
std::vector<std::string> extractUrlsFromText(const std::string& text);
std::string stripMarkdownLinks(const std::string& s);
std::string utf8Truncate(const std::string& s, size_t max_chars);

void parseFrontmatter(const std::string& md,
                      std::unordered_map<std::string, std::string>& out_kv,
                      std::string& out_body);

std::string extractUserNotesAfterAutoEnd(const std::string& md);

bool updateMarkdownPreservingNotes(const std::string& path,
                                   const std::string& new_auto_block,
                                   std::string* error_out = nullptr);

ProcessResult runProcess(const std::vector<std::string>& args,
                         const std::string& cwd = "",
                         int timeout_ms = 60000);

} // namespace ai_curator
