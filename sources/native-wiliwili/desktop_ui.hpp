#pragma once
#include <borealis.hpp>
#include <nlohmann/json.hpp>

// Desktop keyboard behavior shared by every native page.
namespace DesktopUi {
void init();
void openSearch(const std::string& initial = "");
void showHelp();
void cancelHints();
nlohmann::json diagnostics();
}
