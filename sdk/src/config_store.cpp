#include "omnidesk/config_store.hpp"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <sstream>

namespace fs = std::filesystem;

namespace omnidesk {
namespace {

std::atomic<uint64_t> g_atomic_write_seq{1};

bool writeSecureAtomic(const std::string& target_path, const std::string& content) {
    std::error_code ec;
    fs::path p(target_path);
    fs::create_directories(p.parent_path(), ec);
    if (ec) return false;
    ::chmod(p.parent_path().c_str(), 0700);

    std::string tmp_path = target_path + ".tmp." + std::to_string(::getpid()) + "." +
                           std::to_string(g_atomic_write_seq.fetch_add(1, std::memory_order_relaxed));
    int fd = ::open(tmp_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;

    size_t total = 0;
    while (total < content.size()) {
        ssize_t n = ::write(fd, content.data() + total, content.size() - total);
        if (n <= 0) {
            ::close(fd);
            ::unlink(tmp_path.c_str());
            return false;
        }
        total += static_cast<size_t>(n);
    }
    ::fchmod(fd, 0600);
    ::fsync(fd);
    ::close(fd);

    if (::rename(tmp_path.c_str(), target_path.c_str()) != 0) {
        ::unlink(tmp_path.c_str());
        return false;
    }
    ::chmod(target_path.c_str(), 0600);
    return true;
}

nlohmann::json readJsonFile(const std::string& path) {
    std::ifstream ifs(path);
    if (!ifs.is_open()) return nlohmann::json::object();
    try {
        nlohmann::json j;
        ifs >> j;
        if (j.is_object()) return j;
    } catch (...) {
    }
    return nlohmann::json::object();
}

} // namespace

ConfigStore::ConfigStore(std::string config_root)
    : config_root_(std::move(config_root)) {
    std::error_code ec;
    fs::create_directories(fs::path(config_root_) / "plugins", ec);
}

std::string ConfigStore::hostConfigPath() const {
    return (fs::path(config_root_) / "host.json").string();
}

std::string ConfigStore::pluginConfigPath(const std::string& plugin_id) const {
    return (fs::path(config_root_) / "plugins" / (plugin_id + ".json")).string();
}

nlohmann::json ConfigStore::loadHostConfig() const {
    std::lock_guard lock(mutex_);
    return readJsonFile(hostConfigPath());
}

bool ConfigStore::saveHostConfig(const nlohmann::json& cfg) const {
    std::lock_guard lock(mutex_);
    return writeSecureAtomic(hostConfigPath(), cfg.dump(2) + "\n");
}

nlohmann::json ConfigStore::loadPluginConfig(const std::string& plugin_id) const {
    std::lock_guard lock(mutex_);
    return readJsonFile(pluginConfigPath(plugin_id));
}

bool ConfigStore::savePluginConfig(const std::string& plugin_id, const nlohmann::json& cfg) const {
    std::lock_guard lock(mutex_);
    return writeSecureAtomic(pluginConfigPath(plugin_id), cfg.dump(2) + "\n");
}

bool ConfigStore::removePluginConfig(const std::string& plugin_id) const {
    std::lock_guard lock(mutex_);
    std::error_code ec;
    return fs::remove(pluginConfigPath(plugin_id), ec);
}

std::string ConfigStore::getPluginValue(const std::string& plugin_id,
                                        const std::string& key,
                                        const std::string& fallback) const {
    auto cfg = loadPluginConfig(plugin_id);
    if (!cfg.contains(key) || cfg[key].is_null()) return fallback;
    if (cfg[key].is_string()) return cfg[key].get<std::string>();
    if (cfg[key].is_boolean()) return cfg[key].get<bool>() ? "true" : "false";
    if (cfg[key].is_number_integer()) return std::to_string(cfg[key].get<int64_t>());
    return cfg[key].dump();
}

bool ConfigStore::setPluginValue(const std::string& plugin_id,
                                 const std::string& key,
                                 const nlohmann::json& value) const {
    std::lock_guard lock(mutex_);
    auto cfg = readJsonFile(pluginConfigPath(plugin_id));
    cfg[key] = value;
    return writeSecureAtomic(pluginConfigPath(plugin_id), cfg.dump(2) + "\n");
}

SecretVault::SecretVault(std::string config_root)
    : vault_path_((fs::path(std::move(config_root)) / "vault.json").string()) {}

nlohmann::json SecretVault::loadAllLocked() const {
    return readJsonFile(vault_path_);
}

bool SecretVault::saveAllLocked(const nlohmann::json& root) const {
    return writeSecureAtomic(vault_path_, root.dump(2) + "\n");
}

bool SecretVault::setSecret(const std::string& plugin_id,
                            const std::string& key,
                            const std::string& value) {
    if (plugin_id.empty() || key.empty()) return false;
    std::lock_guard lock(mutex_);
    auto root = loadAllLocked();
    if (!root.contains(plugin_id) || !root[plugin_id].is_object()) {
        root[plugin_id] = nlohmann::json::object();
    }
    root[plugin_id][key] = value;
    return saveAllLocked(root);
}

std::optional<std::string> SecretVault::getSecret(const std::string& plugin_id,
                                                  const std::string& key) const {
    std::lock_guard lock(mutex_);
    auto root = loadAllLocked();
    if (!root.contains(plugin_id) || !root[plugin_id].is_object()) return std::nullopt;
    if (!root[plugin_id].contains(key) || !root[plugin_id][key].is_string()) return std::nullopt;
    return root[plugin_id][key].get<std::string>();
}

bool SecretVault::hasSecret(const std::string& plugin_id, const std::string& key) const {
    auto v = getSecret(plugin_id, key);
    return v.has_value() && !v->empty();
}

bool SecretVault::deleteSecret(const std::string& plugin_id, const std::string& key) {
    std::lock_guard lock(mutex_);
    auto root = loadAllLocked();
    if (root.contains(plugin_id) && root[plugin_id].is_object()) {
        root[plugin_id].erase(key);
        return saveAllLocked(root);
    }
    return true;
}

bool SecretVault::purgePlugin(const std::string& plugin_id) {
    std::lock_guard lock(mutex_);
    auto root = loadAllLocked();
    root.erase(plugin_id);
    return saveAllLocked(root);
}

std::vector<std::string> SecretVault::listKeys(const std::string& plugin_id) const {
    std::lock_guard lock(mutex_);
    std::vector<std::string> keys;
    auto root = loadAllLocked();
    if (root.contains(plugin_id) && root[plugin_id].is_object()) {
        for (auto it = root[plugin_id].begin(); it != root[plugin_id].end(); ++it) {
            keys.push_back(it.key());
        }
    }
    return keys;
}

} // namespace omnidesk
