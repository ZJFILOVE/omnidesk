#include "omnidesk/pm/catalog.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <curl/curl.h>
#include <filesystem>
#include <fstream>

namespace omnidesk::pm {
namespace {

std::string toLower(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

size_t curlWriteCallback(void* contents, size_t size, size_t nmemb, void* userp) {
    const size_t total = size * nmemb;
    auto* out = static_cast<std::string*>(userp);
    out->append(static_cast<const char*>(contents), total);
    return total;
}

std::vector<std::string> readStringArray(const nlohmann::json& j, const char* key) {
    std::vector<std::string> res;
    if (!j.contains(key) || !j[key].is_array()) {
        return res;
    }
    for (const auto& item : j[key]) {
        if (item.is_string()) {
            res.push_back(item.get<std::string>());
        }
    }
    return res;
}

CatalogItem parseCatalogEntry(const nlohmann::json& j) {
    CatalogItem item;
    item.id = j.value("id", "");
    item.name = j.value("name", item.id);
    item.version = j.value("version", "0.1.0");
    item.kind = parsePluginKind(j.value("kind", "provider"));
    item.domain = parseContentDomain(j.value("domain", "core"));
    item.author = j.value("author", "");
    item.description = j.value("description", "");
    if (j.contains("repository") && j["repository"].is_object()) {
        item.repo_url = j["repository"].value("url", "");
        item.repo_branch = j["repository"].value("branch", "main");
    } else {
        item.repo_url = j.value("repo_url", "");
        item.repo_branch = j.value("repo_branch", "main");
    }
    item.homepage = j.value("homepage", "");
    if (j.contains("release") && j["release"].is_object()) {
        const auto& rel = j["release"];
        item.release.archive_url = rel.value("archive_url", rel.value("tar_zst_url", ""));
        item.release.sha256_url = rel.value("sha256_url", "");
        item.release.sha256 = rel.value("sha256", "");
        item.release.tag = rel.value("tag", "");
    }
    item.provides = readStringArray(j, "provides");
    item.consumes = readStringArray(j, "consumes");
    item.official = j.value("official", true);
    return item;
}

} // namespace

bool MarketplaceCatalog::loadFromFile(const std::string& path) {
    std::ifstream ifs(path);
    if (!ifs.is_open()) {
        return false;
    }
    try {
        nlohmann::json j = nlohmann::json::parse(ifs);
        return loadFromJson(j);
    } catch (...) {
        return false;
    }
}

bool MarketplaceCatalog::loadFromJson(const nlohmann::json& j) {
    const nlohmann::json* arr = nullptr;
    if (j.is_array()) {
        arr = &j;
    } else if (j.is_object() && j.contains("plugins") && j["plugins"].is_array()) {
        arr = &j["plugins"];
    } else {
        return false;
    }

    for (const auto& entry : *arr) {
        if (!entry.is_object()) {
            continue;
        }
        CatalogItem parsed = parseCatalogEntry(entry);
        if (parsed.id.empty()) {
            continue;
        }
        auto it = std::find_if(items_.begin(), items_.end(), [&](const CatalogItem& existing) {
            return existing.id == parsed.id;
        });
        if (it != items_.end()) {
            *it = std::move(parsed);
        } else {
            items_.push_back(std::move(parsed));
        }
    }
    return true;
}

bool MarketplaceCatalog::mergeRemoteUrl(const std::string& url, std::string& error) {
    if (url.empty()) {
        error = "Catalog URL is empty";
        return false;
    }

    CURL* curl = curl_easy_init();
    if (!curl) {
        error = "curl_easy_init() failed";
        return false;
    }

    std::string body;
    long http_code = 0;
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10000L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "OmniDesk-PM/0.1");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);

    CURLcode rc = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
    curl_easy_cleanup(curl);

    if (rc != CURLE_OK) {
        error = std::string("HTTP request failed: ") + curl_easy_strerror(rc);
        return false;
    }
    if (http_code < 200 || http_code >= 300) {
        error = "HTTP status " + std::to_string(http_code) + " fetching catalog";
        return false;
    }

    try {
        nlohmann::json j = nlohmann::json::parse(body);
        if (!loadFromJson(j)) {
            error = "Remote catalog JSON has invalid schema";
            return false;
        }
    } catch (const std::exception& e) {
        error = std::string("Failed to parse remote catalog JSON: ") + e.what();
        return false;
    }

    error.clear();
    return true;
}

std::vector<CatalogItem> MarketplaceCatalog::filter(std::optional<ContentDomain> domain,
                                                    const std::string& query) const {
    const std::string needle = toLower(query);
    std::vector<CatalogItem> out;
    out.reserve(items_.size());

    for (const auto& item : items_) {
        if (domain.has_value() && item.domain != *domain) {
            continue;
        }
        if (needle.empty()) {
            out.push_back(item);
            continue;
        }
        auto matches = [&](const std::string& hay) {
            return toLower(hay).find(needle) != std::string::npos;
        };
        bool ok = matches(item.id) || matches(item.name) || matches(item.description) ||
                  matches(item.author) || matches(toString(item.domain)) ||
                  matches(toString(item.kind));
        if (!ok) {
            for (const auto& p : item.provides) {
                if (matches(p)) {
                    ok = true;
                    break;
                }
            }
        }
        if (!ok) {
            for (const auto& c : item.consumes) {
                if (matches(c)) {
                    ok = true;
                    break;
                }
            }
        }
        if (ok) {
            out.push_back(item);
        }
    }
    return out;
}

std::optional<CatalogItem> MarketplaceCatalog::findById(const std::string& id) const {
    for (const auto& item : items_) {
        if (item.id == id) {
            return item;
        }
    }
    return std::nullopt;
}

std::string MarketplaceCatalog::normalizeGitSource(const std::string& input) {
    if (input.empty()) {
        return "";
    }
    if (input.rfind("~/", 0) == 0) {
        if (const char* home = std::getenv("HOME")) {
            return std::string(home) + input.substr(1);
        }
    }
    if (input.find("://") != std::string::npos || input.rfind("git@", 0) == 0 ||
        input.front() == '/' || input.rfind("./", 0) == 0 || input.rfind("../", 0) == 0) {
        return input;
    }
    if (std::filesystem::exists(input)) {
        return std::filesystem::weakly_canonical(input).string();
    }
    if (input.rfind("github.com/", 0) == 0) {
        std::string url = "https://" + input;
        if (url.size() < 4 || url.substr(url.size() - 4) != ".git") {
            url += ".git";
        }
        return url;
    }
    const auto slash = input.find('/');
    if (slash != std::string::npos && input.find('/', slash + 1) == std::string::npos &&
        input.find(' ') == std::string::npos) {
        std::string url = "https://github.com/" + input;
        if (url.size() < 4 || url.substr(url.size() - 4) != ".git") {
            url += ".git";
        }
        return url;
    }
    return input;
}

} // namespace omnidesk::pm
