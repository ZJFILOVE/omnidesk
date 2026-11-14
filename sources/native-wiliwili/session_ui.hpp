#pragma once
#include <functional>
#include <nlohmann/json.hpp>
namespace SessionUi {
void open(std::function<void()> onChanged = {});
nlohmann::json diagnostics();
}
