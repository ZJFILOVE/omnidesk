#include <borealis.hpp>
#include <atomic>
#include <cctype>
#include <cstdlib>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/shell_activity.hpp"
#include "omnidesk/dso_view_lease.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/ui_theme.hpp"

namespace fs = std::filesystem;

namespace {

struct MappedFontRegion {
    void* addr = nullptr;
    size_t size = 0;

    MappedFontRegion() = default;
    MappedFontRegion(void* a, size_t s) : addr(a), size(s) {}
    MappedFontRegion(const MappedFontRegion&) = delete;
    MappedFontRegion& operator=(const MappedFontRegion&) = delete;
    MappedFontRegion(MappedFontRegion&& other) noexcept : addr(other.addr), size(other.size) {
        other.addr = nullptr;
        other.size = 0;
    }
    MappedFontRegion& operator=(MappedFontRegion&& other) noexcept {
        if (this != &other) {
            if (addr && size > 0) {
                ::munmap(addr, size);
            }
            addr = other.addr;
            size = other.size;
            other.addr = nullptr;
            other.size = 0;
        }
        return *this;
    }
    ~MappedFontRegion() {
        if (addr && size > 0) {
            ::munmap(addr, size);
        }
    }
};

struct MappedFontStore {
    std::mutex mu;
    std::vector<MappedFontRegion> regions;
};

// Constructed before brls::Application::init() so mappings outlive atexit(Application::exit)
MappedFontStore g_mapped_fonts;

bool loadFontZeroCopyOrFallback(const std::string& slot_name, const std::string& ttf_path) {
    if (ttf_path.empty()) return false;
    const int fd = ::open(ttf_path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        struct stat st {};
        if (::fstat(fd, &st) == 0 && st.st_size > 0) {
            const size_t sz = static_cast<size_t>(st.st_size);
            void* mapped = ::mmap(nullptr, sz, PROT_READ, MAP_PRIVATE, fd, 0);
            ::close(fd);
            if (mapped != MAP_FAILED) {
                if (brls::Application::loadFontFromMemory(slot_name, mapped, sz, false)) {
                    std::lock_guard<std::mutex> lk(g_mapped_fonts.mu);
                    g_mapped_fonts.regions.emplace_back(mapped, sz);
                    return true;
                }
                ::munmap(mapped, sz);
            }
        } else {
            ::close(fd);
        }
    }
    return brls::Application::loadFontFromFile(slot_name, ttf_path);
}

void attachFallbackFont(NVGcontext* vg,
                        int base_font,
                        const std::string& slot_name,
                        const std::string& ttf_path) {
    if (!vg || base_font < 0 || ttf_path.empty()) return;
    std::error_code ec;
    if (!fs::exists(ttf_path, ec)) return;
    int fid = brls::Application::getFont(slot_name);
    if (fid < 0 && loadFontZeroCopyOrFallback(slot_name, ttf_path)) {
        fid = brls::Application::getFont(slot_name);
    }
    if (fid >= 0 && fid != base_font) {
        nvgAddFallbackFontId(vg, base_font, fid);
    }
}

void configureRuntimePaths(const char* argv0) {
    // Ensure BRLS_ASSET("...") resolves against compiled/staged resources/ directory
    std::error_code ec;
    if (!fs::exists(fs::current_path() / "resources" / "font" / "switch_font.ttf", ec)) {
        fs::path exe = fs::read_symlink("/proc/self/exe", ec);
        if (ec && argv0) {
            exe = fs::weakly_canonical(argv0, ec);
        }
        if (ec || exe.empty()) {
            // keep current path
        } else {
            fs::path exe_dir = exe.parent_path();
            if (fs::exists(exe_dir / "resources" / "font" / "switch_font.ttf", ec)) {
                fs::current_path(exe_dir, ec);
            }
        }
    }
}

void printHelp() {
    std::cout <<
        "OmniDesk Desktop Workbench\n\n"
        "Usage:\n"
        "  omnidesk [--domain store|media|text|book] [--headless-check] [--smoke-ms <ms>]\n";
}

bool parseSmokeMs(const std::string& raw, int& out_ms) {
    if (raw.empty() || std::isspace(static_cast<unsigned char>(raw.front()))) {
        return false;
    }
    try {
        size_t consumed = 0;
        const int parsed = std::stoi(raw, &consumed, 10);
        if (consumed != raw.size() || parsed < 0) {
            return false;
        }
        out_ms = parsed;
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

} // namespace

int main(int argc, char** argv) {
    bool headless_check = false;
    int smoke_ms = 0;
    auto initial_ws = omnidesk::host::WorkspaceId::Store;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--help" || a == "-h") {
            printHelp();
            return 0;
        } else if (a == "--headless-check") {
            headless_check = true;
        } else if (a == "--smoke-ms") {
            if (i + 1 >= argc || !parseSmokeMs(argv[++i], smoke_ms)) {
                std::cerr << "Invalid --smoke-ms value: expected a non-negative integer in milliseconds\n";
                return 1;
            }
        } else if (a.rfind("--smoke-ms=", 0) == 0) {
            if (!parseSmokeMs(a.substr(std::string("--smoke-ms=").size()), smoke_ms)) {
                std::cerr << "Invalid --smoke-ms value: expected a non-negative integer in milliseconds\n";
                return 1;
            }
        } else if (a == "--domain" && i + 1 < argc) {
            std::string d = argv[++i];
            if (d == "media") initial_ws = omnidesk::host::WorkspaceId::Media;
            else if (d == "text") initial_ws = omnidesk::host::WorkspaceId::Text;
            else if (d == "book") initial_ws = omnidesk::host::WorkspaceId::Book;
            else if (d == "settings") initial_ws = omnidesk::host::WorkspaceId::Settings;
            else initial_ws = omnidesk::host::WorkspaceId::Store;
        }
    }

    if (!headless_check && smoke_ms == 0) {
        const char* multi_env = std::getenv("OMNIDESK_ALLOW_MULTI_INSTANCE");
        const bool allow_multi = multi_env && std::string(multi_env) == "1";
        if (!allow_multi) {
            const auto ping = omnidesk::pm::sendControlRequest({{"op", "ping"}}, 600);
            if (ping.transport_ok && ping.ok) {
                const int remote_pid = ping.data.is_object() ? ping.data.value("pid", 0) : 0;
                std::cout << "OmniDesk 已在运行中 (PID " << remote_pid
                          << ")，已复用现有工作台实例。\n";
                return 0;
            }
        }
    }

    auto& kernel = omnidesk::host::HostKernel::instance();
    if (!kernel.init(argc > 0 ? argv[0] : nullptr)) {
        std::cerr << "Failed initializing OmniDesk HostKernel\n";
        return 1;
    }

    if (headless_check) {
        std::cout << "OmniDesk HostKernel OK\n"
                  << "  catalog_items: " << kernel.catalog().items().size() << "\n"
                  << "  installed_plugins: " << kernel.installer().listInstalled().size() << "\n"
                  << "  capabilities: " << kernel.capabilities().listAll().size() << "\n"
                  << "  sdk_cmake_dir: " << kernel.sdkCmakeDir() << "\n"
                  << "  resources_dir: " << kernel.resourcesDir() << "\n";
        kernel.shutdown();
        return 0;
    }

    configureRuntimePaths(argc > 0 ? argv[0] : nullptr);

    // Apply saved Paper/Graphite theme preference before window creation
    nlohmann::json host_cfg = kernel.config().loadHostConfig();
    const std::string saved_theme = host_cfg.value("theme", "light");
    brls::Platform::APP_LOCALE_DEFAULT = brls::LOCALE_ZH_HANS;

    brls::Logger::setLogLevel(brls::LogLevel::LOG_INFO);
    if (!brls::Application::init()) {
        std::cerr << "Unable to initialize Borealis Application\n";
        kernel.shutdown();
        return 1;
    }

    if (auto* plat = brls::Application::getPlatform()) {
        plat->setThemeVariant(saved_theme == "dark" ? brls::ThemeVariant::DARK : brls::ThemeVariant::LIGHT);
    }
    omnidesk::ui::applyDesignSystemTheme();

    auto ui_loop_alive = std::make_shared<std::atomic<bool>>(true);

    brls::Application::createWindow("OmniDesk");
    if (NVGcontext* vg = brls::Application::getNVGContext()) {
        const int base_font = brls::Application::getFont(brls::FONT_REGULAR);
        if (base_font >= 0) {
            // Essential CJK + Emoji fallbacks mounted zero-copy via mmap before first frame
            attachFallbackFont(vg, base_font, brls::FONT_EMOJI, kernel.assetPath("font/emoji.ttf"));
            attachFallbackFont(vg, base_font, "omnidesk_cjk", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc");

            // Secondary symbol / math / IPA fallbacks deferred until after first paint
            brls::delay(100, [ui_loop_alive, base_font]() {
                if (!ui_loop_alive->load(std::memory_order_acquire)) return;
                NVGcontext* delayed_vg = brls::Application::getNVGContext();
                if (!delayed_vg) return;
                attachFallbackFont(delayed_vg, base_font, "omnidesk_symbols2", "/usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf");
                attachFallbackFont(delayed_vg, base_font, "omnidesk_math", "/usr/share/fonts/noto/NotoSansMath-Regular.ttf");
                attachFallbackFont(delayed_vg, base_font, "omnidesk_latin_ipa", "/usr/share/fonts/noto/NotoSans-Regular.ttf");
                attachFallbackFont(delayed_vg, base_font, "omnidesk_symbols1", "/usr/share/fonts/noto/NotoSansSymbols-Regular.ttf");
            });
        }
    }
    brls::Application::setGlobalQuit(false);
    brls::Application::setAutomaticDeactivation(true);
    brls::Application::setDeactivatedTime(300);
    brls::Application::setDeactivatedFPS(8);
    omnidesk::DsoViewLeaseRegistry::instance().markUiThread();
    kernel.startDesktopServices();
    brls::Application::getExitEvent()->subscribe([ui_loop_alive]() {
        ui_loop_alive->store(false, std::memory_order_release);
        omnidesk::MPVCore::instance().shutdown();
        omnidesk::ImageCache::instance().shutdown();
    });

    auto* shell = new omnidesk::host::WorkspaceShellActivity(initial_ws);
    brls::Application::pushActivity(shell);

    if (smoke_ms > 0) {
        brls::delay(smoke_ms, []() {
            brls::Application::quit();
        });
    }

    while (brls::Application::mainLoop()) {
    }
    ui_loop_alive->store(false, std::memory_order_release);

    kernel.shutdown();
    return 0;
}
