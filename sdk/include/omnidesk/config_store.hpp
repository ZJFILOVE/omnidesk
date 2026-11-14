#pragma once

#include <mutex>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <vector>
#include "omnidesk/export.h"
#include "omnidesk/types.hpp"

namespace omnidesk {

/**
 * Atomic JSON configuration store under <config_root>/plugins/<plugin_id>.json
 * and host settings under <config_root>/host.json.
 * Enforces 0600 file permissions.
 */
class OMNIDESK_API ConfigStore {
public:
    explicit ConfigStore(std::string config_root);

    const std::string& configRoot() const { return config_root_; }

    nlohmann::json loadHostConfig() const;
    bool saveHostConfig(const nlohmann::json& cfg) const;

    nlohmann::json loadPluginConfig(const std::string& plugin_id) const;
    bool savePluginConfig(const std::string& plugin_id, const nlohmann::json& cfg) const;
    bool removePluginConfig(const std::string& plugin_id) const;

    std::string getPluginValue(const std::string& plugin_id,
                               const std::string& key,
                               const std::string& fallback = "") const;
    bool setPluginValue(const std::string& plugin_id,
                        const std::string& key,
                        const nlohmann::json& value) const;

private:
    std::string hostConfigPath() const;
    std::string pluginConfigPath(const std::string& plugin_id) const;

    std::string config_root_;
    mutable std::mutex mutex_;
};

/**
 * Local 0600-protected SecretVault for plugin API keys, NAS passwords, and tokens.
 * Values are stored isolated by plugin_id + key and never emitted in diagnostics.
 */
class OMNIDESK_API SecretVault {
public:
    explicit SecretVault(std::string config_root);

    bool setSecret(const std::string& plugin_id,
                   const std::string& key,
                   const std::string& value);
    std::optional<std::string> getSecret(const std::string& plugin_id,
                                         const std::string& key) const;
    bool hasSecret(const std::string& plugin_id, const std::string& key) const;
    bool deleteSecret(const std::string& plugin_id, const std::string& key);
    bool purgePlugin(const std::string& plugin_id);
    std::vector<std::string> listKeys(const std::string& plugin_id) const;

private:
    nlohmann::json loadAllLocked() const;
    bool saveAllLocked(const nlohmann::json& root) const;

    std::string vault_path_;
    mutable std::mutex mutex_;
};

} // namespace omnidesk
