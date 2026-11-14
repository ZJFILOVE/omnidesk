#include "omnidesk/pm/installer.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <curl/curl.h>
#include <dlfcn.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <openssl/evp.h>
#include <sstream>
#include <sys/stat.h>
#include <unistd.h>
#include "omnidesk/plugin_abi.h"
#include "omnidesk/pm/catalog.hpp"
#include "omnidesk/pm/process.hpp"

namespace fs = std::filesystem;

namespace omnidesk::pm {
namespace {

struct ScopedDirCleanup {
    fs::path dir;
    ~ScopedDirCleanup() {
        if (!dir.empty()) {
            std::error_code ec;
            fs::remove_all(dir, ec);
        }
    }
};

std::string nowIsoUtc() {
    auto now = std::chrono::system_clock::now();
    std::time_t tt = std::chrono::system_clock::to_time_t(now);
    std::tm tm_utc{};
    gmtime_r(&tt, &tm_utc);
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm_utc);
    return std::string(buf);
}

std::string uniqueTag() {
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                  std::chrono::steady_clock::now().time_since_epoch())
                  .count();
    return std::to_string(getpid()) + "-" + std::to_string(ms);
}

std::string toLowerCopy(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

bool writeJsonAtomic(const std::string& path, const nlohmann::json& j) {
    std::error_code ec;
    fs::path p(path);
    if (p.has_parent_path()) {
        fs::create_directories(p.parent_path(), ec);
        if (ec) {
            return false;
        }
    }
    const std::string tmp = path + ".tmp." + std::to_string(getpid());
    {
        std::ofstream ofs(tmp, std::ios::trunc);
        if (!ofs.is_open()) {
            return false;
        }
        ofs << j.dump(2) << '\n';
        if (!ofs.good()) {
            return false;
        }
    }
    ::chmod(tmp.c_str(), 0600);
    fs::rename(tmp, path, ec);
    return !ec;
}

nlohmann::json readJsonOrObject(const std::string& path) {
    std::ifstream ifs(path);
    if (!ifs.is_open()) {
        return nlohmann::json::object();
    }
    try {
        nlohmann::json j = nlohmann::json::parse(ifs);
        return j.is_object() ? j : nlohmann::json::object();
    } catch (...) {
        return nlohmann::json::object();
    }
}

bool moveDirSafe(const fs::path& src, const fs::path& dst, std::error_code& ec) {
    ec.clear();
    if (dst.has_parent_path()) {
        fs::create_directories(dst.parent_path(), ec);
        if (ec) return false;
    }
    fs::rename(src, dst, ec);
    if (!ec) return true;
    if (ec != std::errc::cross_device_link) {
        return false;
    }

    // Cross-device move (e.g., /tmp tmpfs -> ~/.local ext4): copy to same-fs sibling,
    // rename atomically, then remove source.
    ec.clear();
    const fs::path same_fs_tmp = dst.parent_path() / (".move-" + uniqueTag());
    fs::remove_all(same_fs_tmp, ec);
    ec.clear();
    fs::copy(src, same_fs_tmp,
             fs::copy_options::recursive | fs::copy_options::copy_symlinks, ec);
    if (ec) {
        std::error_code cleanup_ec;
        fs::remove_all(same_fs_tmp, cleanup_ec);
        return false;
    }
    fs::rename(same_fs_tmp, dst, ec);
    if (ec) {
        std::error_code cleanup_ec;
        fs::remove_all(same_fs_tmp, cleanup_ec);
        return false;
    }
    std::error_code rm_ec;
    fs::remove_all(src, rm_ec);
    return true;
}

size_t curlWriteString(void* ptr, size_t size, size_t nmemb, void* userdata) {
    const size_t total = size * nmemb;
    auto* out = static_cast<std::string*>(userdata);
    out->append(static_cast<const char*>(ptr), total);
    return total;
}

size_t curlWriteFile(void* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* ofs = static_cast<std::ofstream*>(userdata);
    const size_t total = size * nmemb;
    ofs->write(static_cast<const char*>(ptr), static_cast<std::streamsize>(total));
    return ofs->good() ? total : 0;
}

bool readLocalPathOrFileUrl(const std::string& uri, std::string& out_path) {
    if (uri.rfind("file://", 0) == 0) {
        out_path = uri.substr(7);
        return true;
    }
    if (!uri.empty() && (uri.front() == '/' || uri.rfind("./", 0) == 0 || uri.rfind("../", 0) == 0)) {
        out_path = uri;
        return true;
    }
    return false;
}

bool fetchTextResource(const std::string& url, std::string& body, std::string& error, long timeout_ms = 5000) {
    std::string local_path;
    if (readLocalPathOrFileUrl(url, local_path)) {
        std::ifstream ifs(local_path, std::ios::binary);
        if (!ifs.is_open()) {
            error = "Cannot open local resource: " + local_path;
            return false;
        }
        std::ostringstream ss;
        ss << ifs.rdbuf();
        body = ss.str();
        return true;
    }

    CURL* curl = curl_easy_init();
    if (!curl) {
        error = "curl_easy_init() failed";
        return false;
    }
    body.clear();
    long http_code = 0;
    struct curl_slist* headers = nullptr;
    if (url.find("api.github.com") != std::string::npos ||
        url.find("/repos/") != std::string::npos) {
        headers = curl_slist_append(headers, "Accept: application/vnd.github+json");
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    }
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 2500L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeout_ms);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "OmniDesk-PM/0.1");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteString);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);

    CURLcode rc = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
    if (headers) curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (rc != CURLE_OK) {
        error = std::string("HTTP error: ") + curl_easy_strerror(rc);
        return false;
    }
    if (http_code < 200 || http_code >= 300) {
        error = "HTTP status " + std::to_string(http_code);
        return false;
    }
    return true;
}

bool downloadBinaryResource(const std::string& url,
                            const fs::path& dest,
                            std::string& error,
                            long timeout_ms = 30000) {
    std::string local_path;
    if (readLocalPathOrFileUrl(url, local_path)) {
        std::error_code ec;
        fs::copy_file(local_path, dest, fs::copy_options::overwrite_existing, ec);
        if (ec) {
            error = "Failed copying local archive: " + ec.message();
            return false;
        }
        return true;
    }

    CURL* curl = curl_easy_init();
    if (!curl) {
        error = "curl_easy_init() failed";
        return false;
    }

    const fs::path tmp = dest.string() + ".part";
    std::ofstream ofs(tmp, std::ios::binary | std::ios::trunc);
    if (!ofs.is_open()) {
        curl_easy_cleanup(curl);
        error = "Cannot write temp download file: " + tmp.string();
        return false;
    }

    long http_code = 0;
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 4000L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeout_ms);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "OmniDesk-PM/0.1");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteFile);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ofs);

    CURLcode rc = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
    curl_easy_cleanup(curl);
    ofs.close();

    std::error_code ec;
    if (rc != CURLE_OK) {
        fs::remove(tmp, ec);
        error = std::string("Download failed: ") + curl_easy_strerror(rc);
        return false;
    }
    if (http_code < 200 || http_code >= 300) {
        fs::remove(tmp, ec);
        error = "Download HTTP status " + std::to_string(http_code);
        return false;
    }
    fs::rename(tmp, dest, ec);
    if (ec) {
        fs::remove(tmp, ec);
        error = "Failed finalizing downloaded archive: " + ec.message();
        return false;
    }
    return true;
}

std::string parseGitHubOwnerRepo(const std::string& git_url) {
    std::string s = git_url;
    while (!s.empty() && s.back() == '/') s.pop_back();
    if (s.size() > 4 && s.substr(s.size() - 4) == ".git") {
        s.resize(s.size() - 4);
    }
    const std::string gh_https = "https://github.com/";
    const std::string gh_http = "http://github.com/";
    const std::string gh_ssh = "git@github.com:";
    if (s.rfind(gh_https, 0) == 0) s = s.substr(gh_https.size());
    else if (s.rfind(gh_http, 0) == 0) s = s.substr(gh_http.size());
    else if (s.rfind(gh_ssh, 0) == 0) s = s.substr(gh_ssh.size());
    else return "";

    auto slash = s.find('/');
    if (slash == std::string::npos || slash == 0 || slash + 1 >= s.size()) return "";
    if (s.find('/', slash + 1) != std::string::npos) return "";
    return s;
}

std::string extractSha256Hex(const std::string& text) {
    std::string norm = text;
    if (norm.rfind("sha256:", 0) == 0) norm = norm.substr(7);
    std::istringstream iss(norm);
    std::string tok;
    while (iss >> tok) {
        if (tok.rfind("sha256:", 0) == 0) tok = tok.substr(7);
        if (tok.size() == 64 &&
            std::all_of(tok.begin(), tok.end(), [](unsigned char c) { return std::isxdigit(c); })) {
            return toLowerCopy(tok);
        }
    }
    return "";
}

bool isSafeArchiveEntry(const std::string& entry) {
    if (entry.empty()) return false;
    if (entry.front() == '/') return false;
    std::stringstream ss(entry);
    std::string part;
    while (std::getline(ss, part, '/')) {
        if (part == "..") return false;
    }
    return true;
}

bool validateTarZstEntries(const std::string& tar_bin,
                           const fs::path& archive_path,
                           bool use_explicit_zstd_prog,
                           std::string& error) {
    std::vector<std::string> argv = {tar_bin};
    if (use_explicit_zstd_prog) {
        argv.push_back("-I");
        argv.push_back("zstd -dc");
    } else {
        argv.push_back("--zstd");
    }
    argv.push_back("-tf");
    argv.push_back(archive_path.string());

    auto res = runProcess(argv, "", {}, 30000);
    if (res.exit_code != 0) {
        error = "Invalid or corrupt .tar.zst archive: " +
                (res.output.empty() ? res.error : res.output);
        return false;
    }
    std::istringstream iss(res.output);
    std::string line;
    size_t count = 0;
    while (std::getline(iss, line)) {
        while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) line.pop_back();
        if (line.empty()) continue;
        if (!isSafeArchiveEntry(line)) {
            error = "Unsafe path inside .tar.zst archive: " + line;
            return false;
        }
        ++count;
    }
    if (count == 0) {
        error = "Empty .tar.zst archive";
        return false;
    }
    return true;
}

fs::path locateExtractedPackageRoot(const fs::path& unpack_dir) {
    if (fs::exists(unpack_dir / "plugin.json")) {
        return unpack_dir;
    }
    std::error_code ec;
    fs::path single_dir;
    int entries = 0;
    for (const auto& entry : fs::directory_iterator(unpack_dir, ec)) {
        ++entries;
        if (entry.is_directory(ec)) {
            single_dir = entry.path();
        }
    }
    if (entries == 1 && !single_dir.empty() && fs::exists(single_dir / "plugin.json")) {
        return single_dir;
    }
    return unpack_dir;
}

std::string findTool(const char* env_override,
                     const std::string& preferred_path,
                     const std::string& fallback_name) {
    if (env_override) {
        if (const char* env = std::getenv(env_override)) {
            if (*env && fs::exists(env)) {
                return env;
            }
        }
    }
    if (!preferred_path.empty() && fs::exists(preferred_path)) {
        return preferred_path;
    }
    return findExecutableInPath(fallback_name);
}

std::string normalizeLibStem(const std::string& id) {
    std::string s;
    for (char ch : id) {
        if (ch == '-' || ch == '.') {
            s += '_';
        } else {
            s += ch;
        }
    }
    return s;
}

std::string discoverBuiltSharedLib(const fs::path& build_dir, const PluginManifest& m) {
    if (!m.output_lib.empty()) {
        fs::path explicit_path = build_dir / m.output_lib;
        if (fs::exists(explicit_path) && fs::is_regular_file(explicit_path)) {
            return fs::weakly_canonical(explicit_path).string();
        }
        fs::path lib_rel = build_dir / "lib" / fs::path(m.output_lib).filename();
        if (fs::exists(lib_rel) && fs::is_regular_file(lib_rel)) {
            return fs::weakly_canonical(lib_rel).string();
        }
    }

    std::vector<fs::path> candidates;
    std::error_code ec;
    for (fs::recursive_directory_iterator it(build_dir, ec), end; it != end && !ec; it.increment(ec)) {
        const fs::path p = it->path();
        if (it->is_directory()) {
            if (p.filename() == "CMakeFiles" || p.filename() == ".git") {
                it.disable_recursion_pending();
            }
            continue;
        }
        if (p.extension() == ".so") {
            candidates.push_back(p);
        }
    }
    if (candidates.empty()) {
        return "";
    }

    const std::string target_stem = m.entry_target.empty() ? normalizeLibStem(m.id) : m.entry_target;
    for (const auto& c : candidates) {
        const std::string fname = c.filename().string();
        if (fname == m.id + ".so" || fname == "lib" + m.id + ".so" ||
            fname == target_stem + ".so" || fname == "lib" + target_stem + ".so") {
            return fs::weakly_canonical(c).string();
        }
    }
    return fs::weakly_canonical(candidates.front()).string();
}

std::string trimNewlines(std::string s) {
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ')) {
        s.pop_back();
    }
    return s;
}

nlohmann::json recordToJson(const InstalledPluginRecord& r) {
    return nlohmann::json{
        {"manifest", manifestToJson(r.manifest)},
        {"install_dir", r.install_dir},
        {"so_path", r.so_path},
        {"source_url", r.source_url},
        {"branch", r.branch},
        {"commit_sha", r.commit_sha},
        {"so_sha256", r.so_sha256},
        {"installed_at", r.installed_at},
        {"enabled", r.enabled},
        {"dev_local", r.dev_local},
        {"install_source", r.install_source},
        {"release_tag", r.release_tag},
        {"backup_versions", r.backup_versions},
    };
}

bool recordFromJson(const nlohmann::json& j, InstalledPluginRecord& out) {
    if (!j.is_object() || !j.contains("manifest")) {
        return false;
    }
    std::string err;
    if (!parseManifestJson(j["manifest"], out.manifest, err)) {
        return false;
    }
    out.install_dir = j.value("install_dir", "");
    out.so_path = j.value("so_path", "");
    out.source_url = j.value("source_url", "");
    out.branch = j.value("branch", "main");
    out.commit_sha = j.value("commit_sha", "");
    out.so_sha256 = j.value("so_sha256", "");
    out.installed_at = j.value("installed_at", "");
    out.enabled = j.value("enabled", true);
    out.dev_local = j.value("dev_local", false);
    out.install_source = j.value("install_source", out.dev_local ? "local-dev" : "source-build");
    out.release_tag = j.value("release_tag", "");
    out.backup_versions.clear();
    if (j.contains("backup_versions") && j["backup_versions"].is_array()) {
        for (const auto& v : j["backup_versions"]) {
            if (v.is_string()) {
                out.backup_versions.push_back(v.get<std::string>());
            }
        }
    }
    return true;
}

} // namespace

PluginInstaller::PluginInstaller(std::string data_root,
                                 std::string sdk_cmake_dir,
                                 std::string abi_checker_exe)
    : data_root_(std::move(data_root)),
      sdk_cmake_dir_(std::move(sdk_cmake_dir)),
      abi_checker_exe_(std::move(abi_checker_exe)) {
    std::error_code ec;
    fs::create_directories(pluginsDir(), ec);
    fs::create_directories(stagingDir(), ec);
    fs::create_directories(backupsDir(), ec);
}

std::string PluginInstaller::pluginsDir() const {
    return (fs::path(data_root_) / "plugins").string();
}

std::string PluginInstaller::stagingDir() const {
    if (const char* env = std::getenv("OMNIDESK_STAGING_DIR")) {
        if (*env) return env;
    }
    return "/tmp/omnidesk-staging";
}

std::string PluginInstaller::backupsDir() const {
    return (fs::path(data_root_) / "backups").string();
}

std::string PluginInstaller::lockFilePath() const {
    return (fs::path(data_root_) / "upstreams.lock.json").string();
}

std::string PluginInstaller::stateFilePath() const {
    return (fs::path(data_root_) / "state.json").string();
}

std::string PluginInstaller::computeSha256(const std::string& file_path) {
    std::ifstream ifs(file_path, std::ios::binary);
    if (!ifs.is_open()) {
        return "";
    }
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    if (!ctx) {
        return "";
    }
    if (EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr) != 1) {
        EVP_MD_CTX_free(ctx);
        return "";
    }
    char buf[8192];
    while (ifs.good()) {
        ifs.read(buf, sizeof(buf));
        std::streamsize n = ifs.gcount();
        if (n > 0) {
            EVP_DigestUpdate(ctx, buf, static_cast<size_t>(n));
        }
    }
    unsigned char hash[EVP_MAX_MD_SIZE];
    unsigned int hash_len = 0;
    EVP_DigestFinal_ex(ctx, hash, &hash_len);
    EVP_MD_CTX_free(ctx);

    std::ostringstream oss;
    for (unsigned int i = 0; i < hash_len; ++i) {
        oss << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(hash[i]);
    }
    return oss.str();
}

bool PluginInstaller::checkSoAbiInProcess(const std::string& so_path,
                                          const std::string& expected_id,
                                          std::string& error_out,
                                          nlohmann::json* info_out) {
    void* handle = dlopen(so_path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        const char* dlerr = dlerror();
        error_out = std::string("dlopen failed: ") + (dlerr ? dlerr : "unknown error");
        return false;
    }

    auto cleanup = [&]() {
        dlclose(handle);
    };

    auto abi_fn = reinterpret_cast<OmniDeskAbiInfoFn>(dlsym(handle, "omnidesk_plugin_abi_info"));
    auto create_fn = reinterpret_cast<OmniDeskPluginCreateFn>(dlsym(handle, "omnidesk_plugin_create"));
    auto destroy_fn = reinterpret_cast<OmniDeskPluginDestroyFn>(dlsym(handle, "omnidesk_plugin_destroy"));

    if (!abi_fn || !create_fn || !destroy_fn) {
        error_out = "Missing mandatory C ABI symbols (omnidesk_plugin_abi_info / create / destroy)";
        cleanup();
        return false;
    }

    const OmniDeskPluginInfo* info = abi_fn();
    if (!info) {
        error_out = "omnidesk_plugin_abi_info() returned null";
        cleanup();
        return false;
    }
    if (info->abi_version != OMNIDESK_PLUGIN_ABI_VERSION) {
        error_out = "ABI version mismatch: plugin=" + std::to_string(info->abi_version) +
                    " host=" + std::to_string(OMNIDESK_PLUGIN_ABI_VERSION);
        cleanup();
        return false;
    }
    if (info->struct_size < sizeof(OmniDeskPluginInfo)) {
        error_out = "OmniDeskPluginInfo struct_size too small";
        cleanup();
        return false;
    }
    if (!info->id || !*info->id) {
        error_out = "OmniDeskPluginInfo::id is empty";
        cleanup();
        return false;
    }
    if (!expected_id.empty() && expected_id != info->id) {
        error_out = std::string("Plugin ID mismatch: manifest='") + expected_id +
                    "' binary='" + info->id + "'";
        cleanup();
        return false;
    }

    // Smoke-test constructor & destructor without calling onLoad()
    void* instance = create_fn();
    if (!instance) {
        error_out = "omnidesk_plugin_create() returned nullptr";
        cleanup();
        return false;
    }
    destroy_fn(instance);

    if (info_out) {
        *info_out = nlohmann::json{
            {"abi_version", info->abi_version},
            {"id", info->id ? info->id : ""},
            {"name", info->name ? info->name : ""},
            {"version", info->version ? info->version : ""},
            {"kind", info->kind ? info->kind : ""},
            {"domain", info->domain ? info->domain : ""},
            {"sdk_version", info->sdk_version ? info->sdk_version : ""},
        };
    }

    cleanup();
    error_out.clear();
    return true;
}

bool PluginInstaller::verifyViaSubprocess(const std::string& so_path,
                                          const std::string& expected_id,
                                          std::string& log,
                                          std::string& error) const {
    std::string checker = abi_checker_exe_;
    if (checker.empty() || !fs::exists(checker)) {
        checker = findExecutableInPath("omnidesk-pm");
    }

    if (!checker.empty() && fs::exists(checker)) {
        std::vector<std::string> argv = {checker, "abi-check", so_path};
        if (!expected_id.empty()) {
            argv.push_back("--expect-id");
            argv.push_back(expected_id);
        }
        auto res = runProcess(argv, "", {}, 30000);
        log += "[abi-check subprocess]\n" + res.output + "\n";
        if (res.exit_code != 0) {
            error = "ABI check subprocess failed: " +
                    (res.output.empty() ? res.error : trimNewlines(res.output));
            return false;
        }
        return true;
    }

    nlohmann::json info;
    if (!checkSoAbiInProcess(so_path, expected_id, error, &info)) {
        log += "[abi-check in-process] FAILED: " + error + "\n";
        return false;
    }
    log += "[abi-check in-process] OK: " + info.dump() + "\n";
    return true;
}

std::optional<ReleaseArtifactSpec> PluginInstaller::findPrebuiltRelease(
    const std::string& normalized_git_url,
    const std::string& /*branch*/,
    const std::optional<ReleaseArtifactSpec>& hint) const {
    if (hint.has_value() && !hint->archive_url.empty()) {
        return *hint;
    }

    const std::string owner_repo = parseGitHubOwnerRepo(normalized_git_url);

    // 1. Optional local/curated prebuilt index JSON (keyed by git URL or owner/repo)
    if (const char* idx_path = std::getenv("OMNIDESK_PREBUILT_INDEX_FILE")) {
        if (*idx_path) {
            nlohmann::json idx = readJsonOrObject(idx_path);
            auto tryExtract = [&](const nlohmann::json& entry) -> std::optional<ReleaseArtifactSpec> {
                if (!entry.is_object()) return std::nullopt;
                ReleaseArtifactSpec spec;
                spec.archive_url = entry.value("archive_url", entry.value("tar_zst_url", ""));
                spec.sha256_url = entry.value("sha256_url", "");
                spec.sha256 = entry.value("sha256", "");
                spec.tag = entry.value("tag", "");
                if (!spec.archive_url.empty()) return spec;
                return std::nullopt;
            };
            if (!normalized_git_url.empty() && idx.contains(normalized_git_url)) {
                if (auto hit = tryExtract(idx[normalized_git_url])) return hit;
            }
            if (!owner_repo.empty() && idx.contains(owner_repo)) {
                if (auto hit = tryExtract(idx[owner_repo])) return hit;
            }
        }
    }

    // 2. GitHub Releases API (.tar.zst asset discovery)
    const char* disable_gh = std::getenv("OMNIDESK_DISABLE_GITHUB_API");
    if (disable_gh && (std::string(disable_gh) == "1" || std::string(disable_gh) == "true")) {
        return std::nullopt;
    }
    if (owner_repo.empty()) {
        return std::nullopt;
    }

    std::string api_base = "https://api.github.com";
    if (const char* env_base = std::getenv("OMNIDESK_GITHUB_API_BASE")) {
        if (*env_base) {
            api_base = env_base;
            while (!api_base.empty() && api_base.back() == '/') api_base.pop_back();
        }
    }

    std::string body;
    std::string fetch_err;
    const std::string release_api = api_base + "/repos/" + owner_repo + "/releases/latest";
    if (!fetchTextResource(release_api, body, fetch_err, 5000)) {
        return std::nullopt;
    }

    try {
        nlohmann::json rel = nlohmann::json::parse(body);
        if (!rel.is_object() || !rel.contains("assets") || !rel["assets"].is_array()) {
            return std::nullopt;
        }
        const std::string tag = rel.value("tag_name", "");
        std::string best_url;
        std::string best_name;
        std::string best_sha;
        int best_score = -1000;
        std::string fallback_sha_url;

        for (const auto& asset : rel["assets"]) {
            if (!asset.is_object()) continue;
            const std::string name = asset.value("name", "");
            const std::string url = asset.value("browser_download_url", "");
            const std::string low = toLowerCopy(name);
            if (low.find("sha256") != std::string::npos && !url.empty() &&
                low.find(".tar.zst") == std::string::npos) {
                if (fallback_sha_url.empty()) fallback_sha_url = url;
            }
            if (url.empty() || low.size() < 8 || low.substr(low.size() - 8) != ".tar.zst") {
                continue;
            }
            if (low.find("windows") != std::string::npos || low.find("win64") != std::string::npos ||
                low.find("darwin") != std::string::npos || low.find("macos") != std::string::npos) {
                continue;
            }
#if defined(__x86_64__) || defined(_M_X64)
            if (low.find("aarch64") != std::string::npos || low.find("arm64") != std::string::npos) {
                continue;
            }
#elif defined(__aarch64__) || defined(_M_ARM64)
            if (low.find("x86_64") != std::string::npos || low.find("amd64") != std::string::npos) {
                continue;
            }
#endif
            int score = 1;
            if (low.find("linux") != std::string::npos) score += 2;
#if defined(__x86_64__) || defined(_M_X64)
            if (low.find("x86_64") != std::string::npos || low.find("amd64") != std::string::npos) score += 3;
#elif defined(__aarch64__) || defined(_M_ARM64)
            if (low.find("aarch64") != std::string::npos || low.find("arm64") != std::string::npos) score += 3;
#endif
            if (score > best_score) {
                best_score = score;
                best_url = url;
                best_name = name;
                best_sha = extractSha256Hex(asset.value("digest", ""));
            }
        }

        if (best_url.empty()) {
            return std::nullopt;
        }
        ReleaseArtifactSpec spec;
        spec.archive_url = best_url;
        spec.sha256 = best_sha;
        spec.tag = tag;
        // Prefer exact sidecar <asset>.sha256 over generic SHA256SUMS
        for (const auto& asset : rel["assets"]) {
            if (!asset.is_object()) continue;
            const std::string name = asset.value("name", "");
            const std::string url = asset.value("browser_download_url", "");
            if (name == best_name + ".sha256" && !url.empty()) {
                spec.sha256_url = url;
                break;
            }
        }
        if (spec.sha256_url.empty()) {
            spec.sha256_url = fallback_sha_url;
        }
        return spec;
    } catch (...) {
        return std::nullopt;
    }
}

InstallResult PluginInstaller::installFromPrebuilt(const ReleaseArtifactSpec& release,
                                                   const std::string& source_url,
                                                   const std::string& branch,
                                                   const std::string& commit_sha,
                                                   ProgressCallback cb) {
    InstallResult out;
    out.install_source = "prebuilt-tar-zst";
    auto emit = [&](const std::string& stage, const std::string& detail) {
        out.log += "[" + stage + "] " + detail + "\n";
        if (cb) {
            cb(stage, detail);
        }
    };

    if (release.archive_url.empty()) {
        out.error = "Prebuilt release archive_url is empty";
        return out;
    }

    const std::string tar_bin = findExecutableInPath("tar");
    if (tar_bin.empty()) {
        out.error = "tar executable not found in PATH";
        return out;
    }

    const fs::path work_dir = fs::path(stagingDir()) / ("prebuilt-" + uniqueTag());
    ScopedDirCleanup cleanup_guard{work_dir};
    std::error_code ec;
    fs::create_directories(work_dir / "unpack", ec);
    if (ec) {
        out.error = "Failed creating /tmp prebuilt staging directory: " + ec.message();
        return out;
    }

    const fs::path archive_path = work_dir / "package.tar.zst";
    emit("prebuilt-fetch", "Fetching prebuilt package " + release.archive_url);
    std::string dl_err;
    if (!downloadBinaryResource(release.archive_url, archive_path, dl_err, 30000)) {
        out.error = dl_err;
        return out;
    }

    // Verify SHA-256 digest when provided directly or via sha256_url
    std::string expected_sha = extractSha256Hex(release.sha256);
    if (expected_sha.empty() && !release.sha256_url.empty()) {
        std::string sha_body;
        std::string sha_err;
        if (!fetchTextResource(release.sha256_url, sha_body, sha_err, 5000)) {
            out.error = "Failed fetching SHA-256 checksum: " + sha_err;
            return out;
        }
        // Match archive filename line if multi-line SHA256SUMS, else first valid digest
        const std::string fname = fs::path(release.archive_url).filename().string();
        std::istringstream lines(sha_body);
        std::string line;
        while (std::getline(lines, line)) {
            if (!fname.empty() && line.find(fname) != std::string::npos) {
                expected_sha = extractSha256Hex(line);
                if (!expected_sha.empty()) break;
            }
        }
        if (expected_sha.empty()) {
            expected_sha = extractSha256Hex(sha_body);
        }
    }
    if (!expected_sha.empty()) {
        const std::string actual_sha = toLowerCopy(computeSha256(archive_path.string()));
        if (actual_sha != expected_sha) {
            out.error = "SHA-256 mismatch for prebuilt .tar.zst (expected " +
                        expected_sha.substr(0, 12) + "..., got " +
                        (actual_sha.empty() ? std::string("empty") : actual_sha.substr(0, 12)) + "...)";
            return out;
        }
        emit("sha256", "Verified .tar.zst SHA-256=" + actual_sha.substr(0, 16) + "...");
    }

    // Safe path check prior to extraction (--zstd or fallback -I zstd)
    bool use_explicit_zstd = false;
    std::string tar_err;
    if (!validateTarZstEntries(tar_bin, archive_path, false, tar_err)) {
        const std::string zstd_bin = findExecutableInPath("zstd");
        if (!zstd_bin.empty() && validateTarZstEntries(tar_bin, archive_path, true, tar_err)) {
            use_explicit_zstd = true;
        } else {
            out.error = tar_err;
            return out;
        }
    }

    const fs::path unpack_dir = work_dir / "unpack";
    std::vector<std::string> extract_argv = {tar_bin};
    if (use_explicit_zstd) {
        extract_argv.push_back("-I");
        extract_argv.push_back("zstd -dc");
    } else {
        extract_argv.push_back("--zstd");
    }
    extract_argv.push_back("-xf");
    extract_argv.push_back(archive_path.string());
    extract_argv.push_back("-C");
    extract_argv.push_back(unpack_dir.string());

    auto ext_res = runProcess(extract_argv, "", {}, 30000);
    if (ext_res.exit_code != 0) {
        out.error = "tar --zstd extract failed: " +
                    (ext_res.output.empty() ? ext_res.error : trimNewlines(ext_res.output));
        return out;
    }

    const fs::path pkg_root = locateExtractedPackageRoot(unpack_dir);
    const fs::path manifest_path = pkg_root / "plugin.json";
    PluginManifest manifest;
    std::string manifest_err;
    if (!parseManifestFile(manifest_path.string(), manifest, manifest_err)) {
        out.error = "Invalid plugin.json inside prebuilt .tar.zst: " + manifest_err;
        return out;
    }

    const std::string built_so = discoverBuiltSharedLib(pkg_root, manifest);
    if (built_so.empty()) {
        out.error = "Prebuilt .tar.zst contains no plugin .so binary";
        return out;
    }

    const std::string eff_source =
        !source_url.empty() ? source_url : (!manifest.repo_url.empty() ? manifest.repo_url : release.archive_url);
    const std::string eff_branch =
        !branch.empty() ? branch : (!manifest.repo_branch.empty() ? manifest.repo_branch : "main");
    const std::string eff_commit = !commit_sha.empty() ? commit_sha : release.tag;
    const fs::path readme_path = pkg_root / "README.md";

    InstallResult res = activatePluginArtifact(
        manifest, built_so, manifest_path.string(),
        fs::exists(readme_path) ? readme_path.string() : "",
        eff_source, eff_branch, eff_commit, false,
        "prebuilt-tar-zst", release.tag, cb);
    res.log = out.log + res.log;
    res.used_prebuilt = res.ok;
    res.install_source = "prebuilt-tar-zst";
    return res;
}

InstallResult PluginInstaller::installFromGit(const std::string& git_source,
                                              const std::string& branch,
                                              ProgressCallback cb,
                                              const std::optional<ReleaseArtifactSpec>& release_hint) {
    InstallResult out;
    const std::string normalized = MarketplaceCatalog::normalizeGitSource(git_source);
    if (normalized.empty()) {
        out.error = "Empty git source URL";
        return out;
    }

    auto emit = [&](const std::string& stage, const std::string& detail) {
        out.log += "[" + stage + "] " + detail + "\n";
        if (cb) {
            cb(stage, detail);
        }
    };

    const std::string git_bin = findExecutableInPath("git");
    std::string fallback_reason;

    bool allow_prebuilt = true;
    if (const char* env_pre = std::getenv("OMNIDESK_PREBUILT_FIRST")) {
        if (std::string(env_pre) == "0" || std::string(env_pre) == "false") {
            allow_prebuilt = false;
        }
    }

    if (allow_prebuilt) {
        if (auto prebuilt = findPrebuiltRelease(normalized, branch, release_hint)) {
            std::string remote_sha;
            if (!git_bin.empty()) {
                const std::string ref = branch.empty() ? "HEAD" : ("refs/heads/" + branch);
                auto lsr = runProcess({git_bin, "ls-remote", normalized, ref}, "",
                                      {{"GIT_TERMINAL_PROMPT", "0"}}, 5000);
                if (lsr.exit_code == 0) {
                    std::istringstream iss(lsr.output);
                    iss >> remote_sha;
                }
            }
            emit("prebuilt", "Found prebuilt .tar.zst package (" + prebuilt->archive_url + ")");
            InstallResult pre_res = installFromPrebuilt(
                *prebuilt, normalized, branch.empty() ? "main" : branch, remote_sha, cb);
            if (pre_res.ok) {
                pre_res.log = out.log + pre_res.log;
                pre_res.used_prebuilt = true;
                return pre_res;
            }
            fallback_reason = pre_res.error;
            out.log += pre_res.log;
            emit("fallback", "Prebuilt .tar.zst unavailable/invalid (" + fallback_reason +
                                 "); compiling from source in " + stagingDir());
        }
    }

    if (git_bin.empty()) {
        out.error = "git executable not found in PATH" +
                    (fallback_reason.empty() ? "" : (" (prebuilt fallback reason: " + fallback_reason + ")"));
        return out;
    }

    const fs::path clone_dir = fs::path(stagingDir()) / ("clone-" + uniqueTag());
    ScopedDirCleanup cleanup_clone{clone_dir};
    std::error_code ec;
    fs::create_directories(clone_dir.parent_path(), ec);

    emit("fetch", "Cloning " + normalized + (branch.empty() ? "" : " (branch " + branch + ")"));
    std::vector<std::string> clone_argv = {git_bin, "clone", "--depth", "1", "--recurse-submodules"};
    if (!branch.empty()) {
        clone_argv.push_back("-b");
        clone_argv.push_back(branch);
    }
    clone_argv.push_back(normalized);
    clone_argv.push_back(clone_dir.string());

    auto clone_res = runProcess(clone_argv, "", {{"GIT_TERMINAL_PROMPT", "0"}}, 180000,
                                [&](const std::string& chunk) {
                                    out.log += chunk;
                                    if (cb) {
                                        cb("fetch", chunk);
                                    }
                                });
    if (clone_res.exit_code != 0) {
        out.error = "git clone failed: " +
                    (clone_res.output.empty() ? clone_res.error : trimNewlines(clone_res.output));
        return out;
    }

    auto sha_res = runProcess({git_bin, "-C", clone_dir.string(), "rev-parse", "HEAD"}, "", {}, 10000);
    const std::string commit_sha = trimNewlines(sha_res.output);

    std::string resolved_branch = branch;
    if (resolved_branch.empty()) {
        auto br_res = runProcess({git_bin, "-C", clone_dir.string(), "rev-parse", "--abbrev-ref", "HEAD"},
                                 "", {}, 10000);
        resolved_branch = trimNewlines(br_res.output);
        if (resolved_branch.empty() || resolved_branch == "HEAD") {
            resolved_branch = "main";
        }
    }

    InstallResult res =
        stageBuildAndActivate(clone_dir.string(), normalized, resolved_branch, commit_sha, false, cb);
    res.log = out.log + res.log;
    res.fallback_reason = fallback_reason;
    return res;
}

InstallResult PluginInstaller::installFromLocal(const std::string& local_dir, ProgressCallback cb) {
    InstallResult out;
    std::error_code ec;
    fs::path abs_dir = fs::weakly_canonical(local_dir, ec);
    if (ec || !fs::exists(abs_dir) || !fs::is_directory(abs_dir)) {
        out.error = "Local plugin directory does not exist: " + local_dir;
        return out;
    }

    std::string commit_sha = "local";
    std::string branch = "local";
    const std::string git_bin = findExecutableInPath("git");
    if (!git_bin.empty() && fs::exists(abs_dir / ".git")) {
        auto sha_res = runProcess({git_bin, "-C", abs_dir.string(), "rev-parse", "HEAD"}, "", {}, 5000);
        if (sha_res.exit_code == 0 && !trimNewlines(sha_res.output).empty()) {
            commit_sha = trimNewlines(sha_res.output);
        }
        auto br_res = runProcess({git_bin, "-C", abs_dir.string(), "rev-parse", "--abbrev-ref", "HEAD"},
                                 "", {}, 5000);
        if (br_res.exit_code == 0 && !trimNewlines(br_res.output).empty()) {
            branch = trimNewlines(br_res.output);
        }
    }

    return stageBuildAndActivate(abs_dir.string(), abs_dir.string(), branch, commit_sha, true, cb);
}

InstallResult PluginInstaller::stageBuildAndActivate(const std::string& source_dir,
                                                     const std::string& source_url,
                                                     const std::string& branch,
                                                     const std::string& commit_sha,
                                                     bool dev_local,
                                                     ProgressCallback cb) {
    InstallResult out;
    auto emit = [&](const std::string& stage, const std::string& detail) {
        out.log += "[" + stage + "] " + detail + "\n";
        if (cb) {
            cb(stage, detail);
        }
    };

    const fs::path manifest_path = fs::path(source_dir) / "plugin.json";
    PluginManifest manifest;
    std::string manifest_err;
    emit("manifest", "Validating " + manifest_path.string());
    if (!parseManifestFile(manifest_path.string(), manifest, manifest_err)) {
        out.error = manifest_err;
        return out;
    }

    out.plugin_id = manifest.id;
    out.version = manifest.version;
    out.commit_sha = commit_sha;

    const std::string cmake_bin =
        findTool("OMNIDESK_CMAKE_BIN", "/tmp/omnidesk-toolchain/bin/cmake", "cmake");
    if (cmake_bin.empty()) {
        out.error = "cmake executable not found";
        return out;
    }
    const std::string ninja_bin =
        findTool("OMNIDESK_NINJA_BIN", "/tmp/omnidesk-toolchain/bin/ninja", "ninja");

    const fs::path build_dir = fs::path(stagingDir()) / ("build-" + manifest.id + "-" + uniqueTag());
    std::error_code ec;
    fs::create_directories(build_dir, ec);

    // 1. CMake configure
    emit("configure", "Configuring CMake with OmniDeskSDK_DIR=" + sdk_cmake_dir_);
    std::vector<std::string> cfg_argv = {
        cmake_bin,
        "-S",
        source_dir,
        "-B",
        build_dir.string(),
        "-DCMAKE_BUILD_TYPE=Release",
    };
    if (!ninja_bin.empty()) {
        cfg_argv.push_back("-GNinja");
        cfg_argv.push_back("-DCMAKE_MAKE_PROGRAM=" + ninja_bin);
    }
    if (!sdk_cmake_dir_.empty()) {
        cfg_argv.push_back("-DOmniDeskSDK_DIR=" + sdk_cmake_dir_);
        cfg_argv.push_back("-DCMAKE_PREFIX_PATH=" + sdk_cmake_dir_);
    }
    for (const auto& arg : manifest.cmake_args) {
        cfg_argv.push_back(arg);
    }

    auto cfg_res = runProcess(cfg_argv, source_dir, {}, 120000, [&](const std::string& chunk) {
        out.log += chunk;
        if (cb) {
            cb("configure", chunk);
        }
    });
    if (cfg_res.exit_code != 0) {
        fs::remove_all(build_dir, ec);
        out.error = "CMake configure failed (" + std::to_string(cfg_res.exit_code) + ")";
        return out;
    }

    // 2. CMake build (include default/test targets when verify_cmd runs ctest)
    emit("build", "Building plugin target...");
    std::vector<std::string> build_argv = {cmake_bin, "--build", build_dir.string(), "--parallel"};
    if (!manifest.entry_target.empty() &&
        manifest.verify_cmd.find("ctest") == std::string::npos) {
        build_argv.push_back("--target");
        build_argv.push_back(manifest.entry_target);
    }
    auto build_res = runProcess(build_argv, source_dir, {}, 240000, [&](const std::string& chunk) {
        out.log += chunk;
        if (cb) {
            cb("build", chunk);
        }
    });
    if (build_res.exit_code != 0) {
        fs::remove_all(build_dir, ec);
        out.error = "CMake build failed (" + std::to_string(build_res.exit_code) + ")";
        return out;
    }

    // 3. Locate compiled .so
    const std::string built_so = discoverBuiltSharedLib(build_dir, manifest);
    if (built_so.empty()) {
        fs::remove_all(build_dir, ec);
        out.error = "Build succeeded but no .so shared library was produced in " + build_dir.string();
        return out;
    }
    emit("artifact", "Discovered plugin shared object: " + built_so);

    // 4. Optional verify_cmd
    if (!manifest.verify_cmd.empty()) {
        emit("verify", "Running verify_cmd: " + manifest.verify_cmd);
        const std::string verify_cwd =
            (manifest.verify_cmd.rfind("ctest", 0) == 0) ? build_dir.string() : source_dir;
        std::string tool_path = fs::path(cmake_bin).parent_path().string();
        if (const char* cur_path = std::getenv("PATH")) {
            tool_path += std::string(":") + cur_path;
        }
        auto vres = runProcess(
            {"/bin/sh", "-c", manifest.verify_cmd}, verify_cwd,
            {
                {"PATH", tool_path},
                {"OMNIDESK_BUILD_DIR", build_dir.string()},
                {"OMNIDESK_SOURCE_DIR", source_dir},
                {"OMNIDESK_PLUGIN_SO", built_so},
            },
            120000, [&](const std::string& chunk) {
                out.log += chunk;
                if (cb) {
                    cb("verify", chunk);
                }
            });
        if (vres.exit_code != 0) {
            fs::remove_all(build_dir, ec);
            out.error = "Plugin verify_cmd failed with code " + std::to_string(vres.exit_code);
            return out;
        }
    }

    InstallResult res = activatePluginArtifact(
        manifest, built_so, manifest_path.string(),
        fs::exists(fs::path(source_dir) / "README.md")
            ? (fs::path(source_dir) / "README.md").string()
            : "",
        source_url, branch, commit_sha, dev_local,
        dev_local ? "local-dev" : "source-build", "", cb);
    out.log += res.log;
    fs::remove_all(build_dir, ec);
    res.log = out.log;
    return res;
}

InstallResult PluginInstaller::activatePluginArtifact(const PluginManifest& manifest,
                                                      const std::string& built_so_path,
                                                      const std::string& manifest_path,
                                                      const std::string& readme_path,
                                                      const std::string& source_url,
                                                      const std::string& branch,
                                                      const std::string& commit_sha,
                                                      bool dev_local,
                                                      const std::string& install_source,
                                                      const std::string& release_tag,
                                                      ProgressCallback cb) {
    InstallResult out;
    out.plugin_id = manifest.id;
    out.version = manifest.version;
    out.commit_sha = commit_sha;
    out.install_source = install_source;
    out.used_prebuilt = (install_source == "prebuilt-tar-zst");

    auto emit = [&](const std::string& stage, const std::string& detail) {
        out.log += "[" + stage + "] " + detail + "\n";
        if (cb) {
            cb(stage, detail);
        }
    };

    // 1. Subprocess ABI verification
    emit("abi-check", "Verifying OmniDesk ABI v1 contract in isolated child process");
    std::string abi_err;
    if (!verifyViaSubprocess(built_so_path, manifest.id, out.log, abi_err)) {
        out.error = abi_err;
        return out;
    }

    // 2. Snapshot previous active version into backups/<id>/<stamp> if present
    std::error_code ec;
    const fs::path final_dir = fs::path(pluginsDir()) / manifest.id;
    std::vector<std::string> backups;
    bool preserve_enabled = true;
    if (auto existing = getInstalled(manifest.id)) {
        backups = existing->backup_versions;
        preserve_enabled = existing->enabled;
    }

    if (fs::exists(final_dir)) {
        const std::string backup_tag = uniqueTag();
        const fs::path backup_dst = fs::path(backupsDir()) / manifest.id / backup_tag;
        if (!moveDirSafe(final_dir, backup_dst, ec)) {
            out.error = "Failed to snapshot existing plugin to backup: " + ec.message();
            return out;
        }
        backups.push_back(backup_tag);
        while (backups.size() > 5) {
            fs::remove_all(fs::path(backupsDir()) / manifest.id / backups.front(), ec);
            backups.erase(backups.begin());
        }
    }

    // 3. Assemble stage_final on the SAME filesystem as pluginsDir() so final rename never hits EXDEV
    const fs::path stage_final = fs::path(pluginsDir()) / (".activating-" + manifest.id + "-" + uniqueTag());
    fs::create_directories(stage_final / "lib", ec);
    if (ec) {
        out.error = "Failed preparing activation staging directory: " + ec.message();
        return out;
    }

    const fs::path so_filename = fs::path(built_so_path).filename();
    const fs::path dest_so = stage_final / "lib" / so_filename;
    fs::copy_file(built_so_path, dest_so, fs::copy_options::overwrite_existing, ec);
    if (ec) {
        fs::remove_all(stage_final, ec);
        out.error = "Failed copying plugin binary: " + ec.message();
        return out;
    }
    fs::copy_file(manifest_path, stage_final / "plugin.json", fs::copy_options::overwrite_existing, ec);
    if (!readme_path.empty() && fs::exists(readme_path)) {
        fs::copy_file(readme_path, stage_final / "README.md", fs::copy_options::overwrite_existing, ec);
    }
    const fs::path src_root = fs::path(manifest_path).parent_path();
    if (fs::exists(src_root / "workers")) {
        fs::copy(src_root / "workers", stage_final / "workers",
                 fs::copy_options::recursive | fs::copy_options::overwrite_existing, ec);
    }
    if (fs::exists(src_root / "resources")) {
        fs::copy(src_root / "resources", stage_final / "resources",
                 fs::copy_options::recursive | fs::copy_options::overwrite_existing, ec);
    }

    InstalledPluginRecord rec;
    rec.manifest = manifest;
    rec.install_dir = final_dir.string();
    rec.so_path = (final_dir / "lib" / so_filename).string();
    rec.source_url = source_url;
    rec.branch = branch;
    rec.commit_sha = commit_sha;
    rec.so_sha256 = computeSha256(dest_so.string());
    rec.installed_at = nowIsoUtc();
    rec.enabled = preserve_enabled;
    rec.dev_local = dev_local;
    rec.install_source = install_source;
    rec.release_tag = release_tag;
    rec.backup_versions = backups;

    writeJsonAtomic((stage_final / "record.json").string(), recordToJson(rec));

    fs::create_directories(final_dir.parent_path(), ec);
    fs::rename(stage_final, final_dir, ec);
    if (ec) {
        fs::remove_all(stage_final, ec);
        out.error = "Failed activating plugin directory: " + ec.message();
        return out;
    }

    // 4. Persist state.json and upstreams.lock.json
    nlohmann::json state = loadStateFile();
    if (!state.contains("plugins") || !state["plugins"].is_object()) {
        state["plugins"] = nlohmann::json::object();
    }
    state["plugins"][manifest.id] = recordToJson(rec);
    saveStateFile(state);

    nlohmann::json lock = loadLockFile();
    if (!lock.contains("locks") || !lock["locks"].is_object()) {
        lock["locks"] = nlohmann::json::object();
    }
    lock["locks"][manifest.id] = {
        {"version", rec.manifest.version},
        {"source_url", rec.source_url},
        {"branch", rec.branch},
        {"commit_sha", rec.commit_sha},
        {"so_sha256", rec.so_sha256},
        {"install_source", rec.install_source},
        {"release_tag", rec.release_tag},
        {"locked_at", rec.installed_at},
    };
    saveLockFile(lock);

    emit("activate", "Activated " + rec.manifest.id + " v" + rec.manifest.version +
                         " [" + rec.install_source + "] (sha256=" +
                         rec.so_sha256.substr(0, 12) + "...)");

    out.ok = true;
    out.so_path = rec.so_path;
    return out;
}

UpdateCheckResult PluginInstaller::checkUpdate(const std::string& plugin_id,
                                               bool include_changelog) const {
    UpdateCheckResult res;
    res.plugin_id = plugin_id;
    auto rec = getInstalled(plugin_id);
    if (!rec.has_value()) {
        res.error = "Plugin not installed: " + plugin_id;
        return res;
    }
    res.local_commit = rec->commit_sha;
    if (rec->dev_local) {
        res.ok = true;
        res.update_available = false;
        res.remote_commit = rec->commit_sha;
        return res;
    }
    const std::string git_bin = findExecutableInPath("git");
    if (git_bin.empty()) {
        res.error = "git executable not found";
        return res;
    }
    const std::string ref =
        rec->branch.empty() ? "HEAD" : ("refs/heads/" + rec->branch);
    auto proc = runProcess({git_bin, "ls-remote", rec->source_url, ref}, "",
                           {{"GIT_TERMINAL_PROMPT", "0"}}, 15000);
    if (proc.exit_code != 0) {
        res.error = "git ls-remote failed: " + trimNewlines(proc.output);
        return res;
    }
    std::istringstream iss(proc.output);
    iss >> res.remote_commit;
    if (res.remote_commit.empty()) {
        res.error = "Remote ref not found: " + ref;
        return res;
    }
    res.ok = true;
    res.update_available = (res.remote_commit != res.local_commit);

    if (res.update_available) {
        if (auto pre = findPrebuiltRelease(
                MarketplaceCatalog::normalizeGitSource(rec->source_url), rec->branch, std::nullopt)) {
            res.prebuilt_available = true;
            res.prebuilt = *pre;
        }
    }

    if (res.update_available && include_changelog && !rec->source_url.empty()) {
        const fs::path radar_dir = fs::path(stagingDir()) / ("radar-" + plugin_id + "-" + uniqueTag());
        ScopedDirCleanup cleanup_radar{radar_dir};
        std::error_code ec;
        fs::create_directories(radar_dir, ec);
        if (!ec) {
            auto runGit = [&](std::vector<std::string> args, int timeout_ms = 15000) {
                std::vector<std::string> full = {git_bin, "-C", radar_dir.string()};
                full.insert(full.end(), args.begin(), args.end());
                return runProcess(full, "", {{"GIT_TERMINAL_PROMPT", "0"}}, timeout_ms);
            };
            runGit({"init", "-q"}, 5000);
            runGit({"remote", "add", "origin", rec->source_url}, 5000);
            auto f_remote = runGit({"fetch", "--depth=30", "origin", res.remote_commit}, 20000);
            if (f_remote.exit_code == 0) {
                const bool has_local_sha =
                    res.local_commit.size() >= 7 &&
                    std::all_of(res.local_commit.begin(), res.local_commit.end(),
                                [](unsigned char c) { return std::isxdigit(c); });
                bool range_ok = false;
                if (has_local_sha) {
                    runGit({"fetch", "--depth=30", "origin", res.local_commit}, 15000);
                    const std::string range = res.local_commit + ".." + res.remote_commit;
                    auto log_res = runGit({"log", "--oneline", "-n", "20", range}, 10000);
                    if (log_res.exit_code == 0 && !trimNewlines(log_res.output).empty()) {
                        range_ok = true;
                        res.changelog = log_res.output;
                        auto diff_res = runGit({"diff", "--stat", range}, 10000);
                        if (diff_res.exit_code == 0) {
                            res.diff_stat = diff_res.output;
                        }
                    } else {
                        // Retry slightly deeper fetch when history diverged or >30 commits behind
                        runGit({"fetch", "--depth=100", "origin", res.remote_commit}, 20000);
                        runGit({"fetch", "--depth=100", "origin", res.local_commit}, 20000);
                        log_res = runGit({"log", "--oneline", "-n", "20", range}, 10000);
                        if (log_res.exit_code == 0 && !trimNewlines(log_res.output).empty()) {
                            range_ok = true;
                            res.changelog = log_res.output;
                            auto diff_res = runGit({"diff", "--stat", range}, 10000);
                            if (diff_res.exit_code == 0) {
                                res.diff_stat = diff_res.output;
                            }
                        }
                    }
                }
                if (!range_ok) {
                    auto log_res = runGit({"log", "--oneline", "-n", "10", res.remote_commit}, 10000);
                    if (log_res.exit_code == 0) {
                        res.changelog = log_res.output;
                    }
                }
                if (!res.changelog.empty()) {
                    std::istringstream ls(res.changelog);
                    std::string line;
                    while (std::getline(ls, line)) {
                        if (!trimNewlines(line).empty()) ++res.ahead_commits;
                    }
                }
            }
        }
    }
    return res;
}

std::vector<UpdateCheckResult> PluginInstaller::checkAllUpdates(bool include_changelog) const {
    std::vector<UpdateCheckResult> results;
    for (const auto& rec : listInstalled()) {
        results.push_back(checkUpdate(rec.manifest.id, include_changelog));
    }
    return results;
}

InstallResult PluginInstaller::updatePlugin(const std::string& plugin_id, ProgressCallback cb) {
    InstallResult res;
    auto rec = getInstalled(plugin_id);
    if (!rec.has_value()) {
        res.error = "Plugin not installed: " + plugin_id;
        return res;
    }
    if (rec->dev_local) {
        return installFromLocal(rec->source_url, cb);
    }
    return installFromGit(rec->source_url, rec->branch, cb);
}

InstallResult PluginInstaller::rollbackPlugin(const std::string& plugin_id) {
    InstallResult out;
    out.plugin_id = plugin_id;

    auto rec = getInstalled(plugin_id);
    if (!rec.has_value()) {
        out.error = "Plugin not installed: " + plugin_id;
        return out;
    }
    if (rec->backup_versions.empty()) {
        out.error = "No previous backup available for " + plugin_id;
        return out;
    }

    const std::string target_tag = rec->backup_versions.back();
    const fs::path backup_src = fs::path(backupsDir()) / plugin_id / target_tag;
    if (!fs::exists(backup_src)) {
        out.error = "Backup directory missing: " + backup_src.string();
        return out;
    }

    nlohmann::json bjson = readJsonOrObject((backup_src / "record.json").string());
    InstalledPluginRecord restored;
    if (!recordFromJson(bjson, restored)) {
        out.error = "Invalid backup metadata in " + backup_src.string();
        return out;
    }

    std::vector<std::string> remaining_backups = rec->backup_versions;
    remaining_backups.pop_back();
    restored.backup_versions = remaining_backups;
    restored.enabled = rec->enabled;

    std::error_code ec;
    const fs::path active_dir = fs::path(pluginsDir()) / plugin_id;
    const fs::path scratch_old = fs::path(pluginsDir()) / (".rollback-old-" + plugin_id + "-" + uniqueTag());
    if (fs::exists(active_dir)) {
        if (!moveDirSafe(active_dir, scratch_old, ec)) {
            out.error = "Failed moving current active plugin aside: " + ec.message();
            return out;
        }
    }

    if (!moveDirSafe(backup_src, active_dir, ec)) {
        if (fs::exists(scratch_old)) {
            std::error_code revert_ec;
            moveDirSafe(scratch_old, active_dir, revert_ec);
        }
        out.error = "Failed restoring backup plugin: " + ec.message();
        return out;
    }
    fs::remove_all(scratch_old, ec);

    // Recompute paths to current active_dir in case backup stored active_dir too
    restored.install_dir = active_dir.string();
    const fs::path so_name = fs::path(restored.so_path).filename();
    restored.so_path = (active_dir / "lib" / so_name).string();
    writeJsonAtomic((active_dir / "record.json").string(), recordToJson(restored));

    nlohmann::json state = loadStateFile();
    state["plugins"][plugin_id] = recordToJson(restored);
    saveStateFile(state);

    nlohmann::json lock = loadLockFile();
    lock["locks"][plugin_id] = {
        {"version", restored.manifest.version},
        {"source_url", restored.source_url},
        {"branch", restored.branch},
        {"commit_sha", restored.commit_sha},
        {"so_sha256", restored.so_sha256},
        {"locked_at", nowIsoUtc()},
    };
    saveLockFile(lock);

    out.ok = true;
    out.version = restored.manifest.version;
    out.commit_sha = restored.commit_sha;
    out.so_path = restored.so_path;
    out.log = "Rolled back " + plugin_id + " to v" + restored.manifest.version + "\n";
    return out;
}

bool PluginInstaller::removePlugin(const std::string& plugin_id, std::string& error) {
    auto rec = getInstalled(plugin_id);
    if (!rec.has_value()) {
        error = "Plugin not installed: " + plugin_id;
        return false;
    }
    std::error_code ec;
    fs::remove_all(fs::path(pluginsDir()) / plugin_id, ec);
    if (ec) {
        error = "Failed removing plugin files: " + ec.message();
        return false;
    }
    fs::remove_all(fs::path(backupsDir()) / plugin_id, ec);

    nlohmann::json state = loadStateFile();
    if (state.contains("plugins") && state["plugins"].is_object()) {
        state["plugins"].erase(plugin_id);
        saveStateFile(state);
    }

    nlohmann::json lock = loadLockFile();
    if (lock.contains("locks") && lock["locks"].is_object()) {
        lock["locks"].erase(plugin_id);
        saveLockFile(lock);
    }
    error.clear();
    return true;
}

bool PluginInstaller::setEnabled(const std::string& plugin_id, bool enabled) {
    nlohmann::json state = loadStateFile();
    if (!state.contains("plugins") || !state["plugins"].contains(plugin_id)) {
        return false;
    }
    state["plugins"][plugin_id]["enabled"] = enabled;
    return saveStateFile(state);
}

bool PluginInstaller::isEnabled(const std::string& plugin_id) const {
    auto rec = getInstalled(plugin_id);
    return rec.has_value() && rec->enabled;
}

std::vector<InstalledPluginRecord> PluginInstaller::listInstalled() const {
    std::vector<InstalledPluginRecord> out;
    nlohmann::json state = loadStateFile();
    if (!state.contains("plugins") || !state["plugins"].is_object()) {
        return out;
    }
    for (auto it = state["plugins"].begin(); it != state["plugins"].end(); ++it) {
        InstalledPluginRecord rec;
        if (recordFromJson(it.value(), rec)) {
            out.push_back(std::move(rec));
        }
    }
    std::sort(out.begin(), out.end(), [](const InstalledPluginRecord& a, const InstalledPluginRecord& b) {
        return a.manifest.id < b.manifest.id;
    });
    return out;
}

std::optional<InstalledPluginRecord> PluginInstaller::getInstalled(const std::string& plugin_id) const {
    nlohmann::json state = loadStateFile();
    if (!state.contains("plugins") || !state["plugins"].contains(plugin_id)) {
        return std::nullopt;
    }
    InstalledPluginRecord rec;
    if (!recordFromJson(state["plugins"][plugin_id], rec)) {
        return std::nullopt;
    }
    return rec;
}

nlohmann::json PluginInstaller::loadLockFile() const {
    nlohmann::json j = readJsonOrObject(lockFilePath());
    if (!j.contains("locks") || !j["locks"].is_object()) {
        j["locks"] = nlohmann::json::object();
    }
    return j;
}

bool PluginInstaller::saveLockFile(const nlohmann::json& j) const {
    return writeJsonAtomic(lockFilePath(), j);
}

nlohmann::json PluginInstaller::loadStateFile() const {
    nlohmann::json j = readJsonOrObject(stateFilePath());
    if (!j.contains("plugins") || !j["plugins"].is_object()) {
        j["plugins"] = nlohmann::json::object();
    }
    return j;
}

bool PluginInstaller::saveStateFile(const nlohmann::json& j) const {
    return writeJsonAtomic(stateFilePath(), j);
}

} // namespace omnidesk::pm
