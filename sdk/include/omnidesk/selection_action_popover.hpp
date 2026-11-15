#pragma once

#include <functional>
#include <string>
#include <vector>

#include "omnidesk/export.h"
#include "omnidesk/types.hpp"

namespace omnidesk {

class CapabilityRegistry;
class EntityBus;
class UiTaskRunner;

struct OMNIDESK_API SelectionContext {
    std::string selected_text;
    std::string context_sentence;
    std::string source_title;
    std::string source_id;
    ContentDomain domain = ContentDomain::Text;
    std::string book_id;
    CapabilityRegistry* capabilities = nullptr;
    EntityBus* bus = nullptr;
    UiTaskRunner* ui_tasks = nullptr;
    std::function<void(const std::string& prompt, const std::string& context)> on_send_to_ai;
    std::function<void(const std::string& query)> on_search_global;
};

/**
 * Unified text-selection action popover for TextWorkbench, CommentTreeView,
 * BookWorkbench, and MediaWorkbench lyrics/subtitles:
 * - Add to SM-2 deck (vocab.sm2.v1 + entity.vocab.add)
 * - Send to F8 AI Companion Dock (entity.ai.companion_ask)
 * - Search fnOS NAS dialogue clips & jump-play (media.clip_jump.v1)
 * - Inline translation (llm.translate.v1 / llm.chat.v1)
 * - Global Ctrl+K jump (entity.search.open) & clipboard copy
 */
class OMNIDESK_API SelectionActionPopover {
public:
    static void show(SelectionContext ctx);

    static std::vector<std::string> extractSelectableTokens(const std::string& text,
                                                            size_t max_tokens = 12);
};

} // namespace omnidesk
