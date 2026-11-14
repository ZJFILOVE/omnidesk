#pragma once

#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <vector>
#include "omnidesk/export.h"
#include "omnidesk/pm/manifest.hpp"
#include "omnidesk/types.hpp"

namespace omnidesk::pm {

struct CatalogItem {
    std::string id;
    std::string name;
    std::string version;
    PluginKind kind = PluginKind::Provider;
    ContentDomain domain = ContentDomain::Core;
    std::string author;
    std::string description;
    std::string repo_url;
    std::string repo_branch = "main";
    std::string homepage;
    ReleaseArtifactSpec release;
    std::vector<std::string> provides;
    std::vector<std::string> consumes;
    bool official = true;
};

/**
 * Loads curated marketplace catalog JSON and merges user/remote catalog sources.
 * Normalizes GitHub "owner/repo" shorthand into full HTTPS git clone URLs.
 */
class OMNIDESK_API MarketplaceCatalog {
public:
    bool loadFromFile(const std::string& path);
    bool loadFromJson(const nlohmann::json& j);
    bool mergeRemoteUrl(const std::string& url, std::string& error);

    std::vector<CatalogItem> items() const { return items_; }
    std::vector<CatalogItem> filter(std::optional<ContentDomain> domain,
                                    const std::string& query) const;
    std::optional<CatalogItem> findById(const std::string& id) const;

    static std::string normalizeGitSource(const std::string& input);

private:
    std::vector<CatalogItem> items_;
};

} // namespace omnidesk::pm
