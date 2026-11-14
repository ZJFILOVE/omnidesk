#include <borealis.hpp>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <string>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/host/shell_activity.hpp"
#include "omnidesk/ui_theme.hpp"

namespace fs = std::filesystem;

namespace {

void configureRuntimePaths(const char* argv0) {
    // Ensure BRLS_ASSET("...") resolves against compiled/staged resources/ directory
    std::error_code ec;
    if (!fs::exists(fs::current_path() / "resources" / "font" / "switch_font.ttf", ec)) {
        fs::path exe = fs::read_symlink("/proc/self/exe", ec);
        if (ec && argv0) {
            exe = fs::weakly_canonical(argv0, ec);
        }
        if (!exe.empty()) {
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
        } else if (a == "--smoke-ms" && i + 1 < argc) {
            smoke_ms = std::stoi(argv[++i]);
        } else if (a == "--domain" && i + 1 < argc) {
            std::string d = argv[++i];
            if (d == "media") initial_ws = omnidesk::host::WorkspaceId::Media;
            else if (d == "text") initial_ws = omnidesk::host::WorkspaceId::Text;
            else if (d == "book") initial_ws = omnidesk::host::WorkspaceId::Book;
            else if (d == "settings") initial_ws = omnidesk::host::WorkspaceId::Settings;
            else initial_ws = omnidesk::host::WorkspaceId::Store;
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

    brls::Application::createWindow("OmniDesk");
    brls::Application::setGlobalQuit(false);

    auto* shell = new omnidesk::host::WorkspaceShellActivity(initial_ws);
    brls::Application::pushActivity(shell);

    if (smoke_ms > 0) {
        brls::delay(smoke_ms, []() {
            brls::Application::quit();
        });
    }

    while (brls::Application::mainLoop()) {
    }

    kernel.shutdown();
    return 0;
}
