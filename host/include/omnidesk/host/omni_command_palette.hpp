#pragma once

#include <functional>
#include <nlohmann/json.hpp>
#include <string>

namespace omnidesk::host {

struct CommandPaletteCallbacks {
    std::function<void(const std::string& command, const nlohmann::json& payload)> on_execute_command;
    std::function<void(const std::string& tab_id)> on_open_workspace_tab;
    std::function<void(const std::string& word)> on_open_vocab;
};

/**
 * Global Ctrl+K Command & Cross-Domain FTS5 Search Palette.
 * Searches across fnOS NAS dialogue clips, local EPUB/PDF bookshelves,
 * Obsidian past curations, Apple Music reviews, SM-2 vocabulary cards,
 * and built-in/plugin quick commands via `search.index.v1`.
 */
class OmniCommandPalette {
public:
    static void open(const std::string& initial_query = "",
                     CommandPaletteCallbacks callbacks = {});
};

} // namespace omnidesk::host
