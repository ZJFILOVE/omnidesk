#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <vector>

namespace omnidesk::host {

enum class AuthStatusKind {
    Authenticated = 0,       // Verified login credentials / active session
    GuestCookieOnly = 1,     // Cookie exists, but required login token is missing (anonymous guest cookie)
    OptionalGuest = 2,       // Works in public/guest mode; login unlocks personalized/member features
    MissingRequired = 3,     // Required credentials not configured yet
    NoAuthNeeded = 4,        // Local/open-access plugin requiring no login
};

enum class AuthMethodKind {
    BrowserCookie = 0,       // Supports browser login + automatic CDP cookie capture
    NativeOrServiceKey = 1,  // Native account/2FA/QR UI or local service API key
    NoAuth = 2,              // No authentication needed
};

struct PluginAuthReport {
    std::string plugin_id;
    std::string title;
    std::string category_label;
    AuthMethodKind method = AuthMethodKind::BrowserCookie;
    AuthStatusKind status = AuthStatusKind::OptionalGuest;
    bool is_installed = false;
    bool is_loaded = false;
    bool supports_browser_cookie_login = false;
    bool allows_custom_login_url = false;
    std::string default_login_url;
    std::string secret_key_for_cookie;
    std::string status_badge_text;
    std::string status_bg_token;
    std::string status_fg_token;
    std::string credential_source;
    std::string detail_summary;
    std::string where_to_auth_hint;
};

struct AuthSummaryCounts {
    int total_auth_plugins = 0;
    int authenticated_count = 0;
    int guest_cookie_warning_count = 0;
    int optional_guest_count = 0;
    int missing_required_count = 0;
    int no_auth_count = 0;
};

struct AuthCenterCallbacks {
    std::function<void(const std::string& plugin_id)> on_open_plugin_config;
    std::function<void(const std::string& tab_id)> on_open_custom_tab;
    std::function<void()> on_open_ai_workbench;
};

/**
 * Host-level Unified Authentication Inspector & Browser Auto-Cookie Harvester.
 *
 * - Audits real login status across all OmniDesk plugins (strictly distinguishing real login
 *   cookies like z_c0 / auth_token+ct0 / _t / A2 / SESSDATA / SAPISID / remix_userkey from
 *   anonymous visitor cookies like guest_id / edgebucket).
 * - Launches a dedicated visible Chrome `--app` login window with local loopback CDP
 *   (`Storage.getCookies`) to automatically detect login completion, extract HttpOnly cookies,
 *   persist them to `SecretVault` (0600) + `plugin-chrome-web` (`web.cookies.v1`), and hot-reload
 *   the target plugin without manual DevTools copying.
 */
class AuthCenterManager {
public:
    using ChangeListener = std::function<void()>;

    static AuthCenterManager& instance();

    void stop();

    std::vector<PluginAuthReport> inspectAll() const;
    PluginAuthReport inspectPlugin(const std::string& plugin_id) const;
    AuthSummaryCounts summarizeCounts(const std::vector<PluginAuthReport>& reports) const;

    bool supportsBrowserCookieLogin(const std::string& plugin_id) const;
    bool allowsCustomLoginUrl(const std::string& plugin_id) const;
    std::string defaultLoginUrlForPlugin(const std::string& plugin_id) const;

    /**
     * Provides an accurate human-readable status diagnosis for Secret config fields
     * (e.g., warning when SecretVault only holds anonymous guest cookies, or showing green
     * when active via Chrome CookieStore / cookies.txt). Returns true if handled.
     */
    bool diagnoseCookieSecretField(const std::string& plugin_id,
                                   const std::string& field_key,
                                   std::string& out_summary,
                                   std::string& out_color_token) const;

    void startBrowserCookieLogin(
        const std::string& plugin_id,
        const std::string& custom_url_override = "",
        std::function<void(bool ok, const std::string& message)> on_finished = nullptr);

    void finishActiveLoginCaptureNow();
    void cancelActiveBrowserLogin();
    bool isBrowserLoginRunning(std::string* out_plugin_id = nullptr,
                               std::string* out_status_text = nullptr) const;

    bool syncCookiesFromExistingStores(const std::string& plugin_id, std::string& out_message);
    bool clearPluginCookieAuth(const std::string& plugin_id, std::string& out_message);

    void triggerAsyncLiveVerification(std::function<void()> on_updated = nullptr);

    uint64_t subscribeChanges(ChangeListener listener);
    void unsubscribeChanges(uint64_t token);

private:
    AuthCenterManager() = default;
    ~AuthCenterManager();

    void notifyChanges();

    mutable std::mutex mutex_;
    std::unordered_map<std::string, nlohmann::json> live_probe_cache_;
    bool probe_running_ = false;

    mutable std::mutex login_mutex_;
    std::condition_variable login_cv_;
    uint64_t login_seq_ = 0;
    bool login_running_ = false;
    bool login_force_capture_now_ = false;
    bool login_cancel_requested_ = false;
    int login_chrome_pid_ = -1;
    std::string login_plugin_id_;
    std::string login_status_text_;
    int active_bg_threads_ = 0;
    bool stopping_ = false;

    mutable std::mutex listener_mutex_;
    uint64_t next_listener_id_ = 1;
    std::vector<std::pair<uint64_t, ChangeListener>> listeners_;
};

class AuthCenterDialog {
public:
    static void open(AuthCenterCallbacks callbacks = {},
                     const std::string& initial_focus_plugin_id = "");
};

} // namespace omnidesk::host
