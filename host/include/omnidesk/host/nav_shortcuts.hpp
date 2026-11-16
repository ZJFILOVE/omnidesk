#pragma once

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <string>
#include <unordered_set>
#include <vector>

#include "omnidesk/domain_registry.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {

enum class NavGroupId : int {
    Media = 0,
    CommunityWeb = 1,
    Book = 2,
};

struct AssignedPluginTab {
    WorkspaceTabSpec spec;
    NavGroupId group = NavGroupId::CommunityWeb;
    int sort_rank = 500;
    char alt_key = '\0';
    std::string shortcut_label;
    std::string icon_kind = "extension";
    const char* domain_token = "domain/text";
};

inline const char* navGroupTitle(NavGroupId g) {
    switch (g) {
        case NavGroupId::Media:        return "影音扩展";
        case NavGroupId::CommunityWeb: return "社区与网页";
        case NavGroupId::Book:         return "书库与精校";
    }
    return "扩展工作区";
}

inline const char* navGroupDomainToken(NavGroupId g) {
    switch (g) {
        case NavGroupId::Media:        return "domain/media";
        case NavGroupId::CommunityWeb: return "domain/text";
        case NavGroupId::Book:         return "domain/book";
    }
    return "domain/core";
}

inline const char* domainColorToken(ContentDomain d) {
    switch (d) {
        case ContentDomain::Media: return "domain/media";
        case ContentDomain::Text:  return "domain/text";
        case ContentDomain::Book:  return "domain/book";
        case ContentDomain::Core:  return "domain/core";
    }
    return "domain/core";
}

inline std::string nvgColorToHex(const NVGcolor& c) {
    const int r = std::clamp(static_cast<int>(c.r * 255.0f + 0.5f), 0, 255);
    const int g = std::clamp(static_cast<int>(c.g * 255.0f + 0.5f), 0, 255);
    const int b = std::clamp(static_cast<int>(c.b * 255.0f + 0.5f), 0, 255);
    char buf[10];
    std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", r, g, b);
    return buf;
}

inline NVGcolor blendColor(const NVGcolor& base, const NVGcolor& tint, float alpha) {
    const float a = std::clamp(alpha, 0.0f, 1.0f);
    return nvgRGBAf(
        base.r * (1.0f - a) + tint.r * a,
        base.g * (1.0f - a) + tint.g * a,
        base.b * (1.0f - a) + tint.b * a,
        1.0f);
}

namespace detail {

inline std::string toLowerAscii(const std::string& s) {
    std::string out = s;
    for (char& ch : out) {
        if (ch >= 'A' && ch <= 'Z') {
            ch = static_cast<char>(ch - 'A' + 'a');
        }
    }
    return out;
}

inline bool containsToken(const std::string& hay_lower, const char* needle_lower) {
    return hay_lower.find(needle_lower) != std::string::npos;
}

struct CanonicalPluginProfile {
    NavGroupId group;
    int rank;
    char preferred_alt;
    const char* icon_kind;
    const char* domain_token;
};

inline bool matchCanonicalProfile(const WorkspaceTabSpec& spec, CanonicalPluginProfile& out) {
    const std::string pid = toLowerAscii(spec.plugin_id);
    const std::string tid = toLowerAscii(spec.tab_id);
    const std::string title = toLowerAscii(spec.title);
    const std::string combined = pid + "|" + tid + "|" + title;

    // 1. Media extensions
    if (containsToken(combined, "bilibili") || spec.title.find("B站") != std::string::npos) {
        out = {NavGroupId::Media, 10, 'B', "bilibili", "domain/media"};
        return true;
    }
    if (containsToken(combined, "youtube")) {
        out = {NavGroupId::Media, 20, 'Y', "youtube", "domain/media"};
        return true;
    }
    if (containsToken(combined, "music")) {
        out = {NavGroupId::Media, 30, 'M', "music", "domain/media"};
        return true;
    }
    if (containsToken(combined, "arr-imdb") || containsToken(combined, "imdb") ||
        spec.title.find("选片") != std::string::npos) {
        out = {NavGroupId::Media, 40, 'Q', "film_queue", "domain/media"};
        return true;
    }
    if (containsToken(combined, "fnos") || containsToken(combined, "nas")) {
        out = {NavGroupId::Media, 50, 'N', "nas", "domain/media"};
        return true;
    }

    // 2. Community & Web extensions
    if (containsToken(combined, "chrome") || containsToken(combined, "web_reader") ||
        spec.title.find("网页") != std::string::npos) {
        out = {NavGroupId::CommunityWeb, 10, 'W', "globe", "domain/text"};
        return true;
    }
    if (containsToken(combined, "blog") || spec.title.find("博主") != std::string::npos) {
        out = {NavGroupId::CommunityWeb, 20, 'S', "rss", "domain/text"};
        return true;
    }
    if (containsToken(combined, "recipe") || spec.title.find("配方") != std::string::npos) {
        out = {NavGroupId::CommunityWeb, 30, 'R', "sliders", "domain/text"};
        return true;
    }
    if (containsToken(combined, "journal")) {
        out = {NavGroupId::CommunityWeb, 40, 'J', "journal", "domain/text"};
        return true;
    }
    if (containsToken(combined, "zhihu")) {
        out = {NavGroupId::CommunityWeb, 50, 'H', "forum", "domain/text"};
        return true;
    }
    if (containsToken(combined, "linuxdo")) {
        out = {NavGroupId::CommunityWeb, 60, 'L', "forum", "domain/text"};
        return true;
    }
    if (containsToken(combined, "reddit")) {
        out = {NavGroupId::CommunityWeb, 70, 'D', "forum", "domain/text"};
        return true;
    }
    if (containsToken(combined, "v2ex")) {
        out = {NavGroupId::CommunityWeb, 80, 'V', "forum", "domain/text"};
        return true;
    }
    if (containsToken(combined, "shuiyuan")) {
        out = {NavGroupId::CommunityWeb, 90, 'U', "forum", "domain/text"};
        return true;
    }
    if (pid == "plugin-x" || tid == "tab-x") {
        out = {NavGroupId::CommunityWeb, 100, 'X', "forum", "domain/text"};
        return true;
    }

    // 3. Book & Library extensions
    if (containsToken(combined, "ebook") || spec.title.find("生词") != std::string::npos) {
        out = {NavGroupId::Book, 10, 'E', "ebook", "domain/book"};
        return true;
    }
    if (containsToken(combined, "pdf") || containsToken(combined, "babeldoc") ||
        spec.title.find("精校") != std::string::npos) {
        out = {NavGroupId::Book, 20, 'P', "pdf_split", "domain/book"};
        return true;
    }
    if (containsToken(combined, "zlib") || containsToken(combined, "z-library")) {
        out = {NavGroupId::Book, 30, 'Z', "library", "domain/book"};
        return true;
    }
    if (containsToken(combined, "anna")) {
        out = {NavGroupId::Book, 40, 'A', "archive", "domain/book"};
        return true;
    }

    return false;
}

inline std::string stripKnownPrefix(std::string s) {
    for (const char* pfx : {"omnidesk-plugin-", "plugin-", "plugin_", "tab-", "tab_"}) {
        const std::string p(pfx);
        if (s.rfind(p, 0) == 0) {
            s = s.substr(p.size());
            break;
        }
    }
    return s;
}

inline char pickUnusedKeyFromText(const std::string& text, const std::unordered_set<char>& used) {
    for (unsigned char uc : text) {
        if (std::isalpha(uc)) {
            const char upper = static_cast<char>(std::toupper(uc));
            if (used.find(upper) == used.end()) {
                return upper;
            }
        }
    }
    return '\0';
}

} // namespace detail

inline std::vector<AssignedPluginTab> assignPluginNavTabs(
    const std::vector<WorkspaceTabSpec>& raw_tabs) {
    struct Candidate {
        AssignedPluginTab item;
        char preferred_key = '\0';
    };

    std::vector<Candidate> list;
    list.reserve(raw_tabs.size());

    for (const auto& t : raw_tabs) {
        Candidate c;
        c.item.spec = t;
        detail::CanonicalPluginProfile prof{};
        if (detail::matchCanonicalProfile(t, prof)) {
            c.item.group = prof.group;
            c.item.sort_rank = prof.rank;
            c.item.icon_kind = prof.icon_kind;
            c.item.domain_token = prof.domain_token;
            c.preferred_key = prof.preferred_alt;
        } else {
            switch (t.domain) {
                case ContentDomain::Media:
                    c.item.group = NavGroupId::Media;
                    c.item.icon_kind = "media";
                    c.item.domain_token = "domain/media";
                    break;
                case ContentDomain::Book:
                    c.item.group = NavGroupId::Book;
                    c.item.icon_kind = "book";
                    c.item.domain_token = "domain/book";
                    break;
                case ContentDomain::Text:
                case ContentDomain::Core:
                default:
                    c.item.group = NavGroupId::CommunityWeb;
                    c.item.icon_kind = "extension";
                    c.item.domain_token = "domain/text";
                    break;
            }
            c.item.sort_rank = 500;
        }
        list.push_back(std::move(c));
    }

    std::stable_sort(list.begin(), list.end(), [](const Candidate& a, const Candidate& b) {
        if (a.item.group != b.item.group) {
            return static_cast<int>(a.item.group) < static_cast<int>(b.item.group);
        }
        if (a.item.sort_rank != b.item.sort_rank) {
            return a.item.sort_rank < b.item.sort_rank;
        }
        if (a.item.spec.title != b.item.spec.title) {
            return a.item.spec.title < b.item.spec.title;
        }
        return a.item.spec.tab_id < b.item.spec.tab_id;
    });

    std::unordered_set<char> used_keys;
    // Pass 1: assign canonical preferred keys first so core plugins never lose their mnemonic
    for (auto& c : list) {
        if (c.preferred_key != '\0' && used_keys.find(c.preferred_key) == used_keys.end()) {
            c.item.alt_key = c.preferred_key;
            used_keys.insert(c.preferred_key);
        }
    }

    // Pass 2: assign deterministic fallback keys for any remaining/custom plugins
    for (auto& c : list) {
        if (c.item.alt_key != '\0') continue;
        char k = detail::pickUnusedKeyFromText(c.item.spec.title, used_keys);
        if (k == '\0') {
            k = detail::pickUnusedKeyFromText(
                detail::stripKnownPrefix(detail::toLowerAscii(c.item.spec.tab_id)), used_keys);
        }
        if (k == '\0') {
            k = detail::pickUnusedKeyFromText(
                detail::stripKnownPrefix(detail::toLowerAscii(c.item.spec.plugin_id)), used_keys);
        }
        if (k == '\0') {
            for (char ch = 'A'; ch <= 'Z'; ++ch) {
                if (used_keys.find(ch) == used_keys.end()) {
                    k = ch;
                    break;
                }
            }
        }
        if (k == '\0') {
            for (char ch : {'1', '2', '3', '4', '5', '6', '7', '8', '9', '0'}) {
                if (used_keys.find(ch) == used_keys.end()) {
                    k = ch;
                    break;
                }
            }
        }
        if (k != '\0') {
            c.item.alt_key = k;
            used_keys.insert(k);
        }
    }

    std::vector<AssignedPluginTab> out;
    out.reserve(list.size());
    for (auto& c : list) {
        if (c.item.alt_key != '\0') {
            c.item.shortcut_label = std::string("Alt+") + c.item.alt_key;
        }
        out.push_back(std::move(c.item));
    }
    return out;
}

inline std::string buildNavIconSvg(const std::string& kind, const std::string& stroke_hex) {
    const std::string head =
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" "
        "fill=\"none\" stroke=\"" +
        stroke_hex +
        "\" stroke-width=\"1.85\" stroke-linecap=\"round\" stroke-linejoin=\"round\">";
    const std::string tail = "</svg>";

    if (kind == "store") {
        return head +
               "<rect x=\"3\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.6\"/>"
               "<rect x=\"14\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.6\"/>"
               "<rect x=\"3\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.6\"/>"
               "<path d=\"M17.5 14v7M14 17.5h7\"/>" +
               tail;
    }
    if (kind == "media") {
        return head +
               "<rect x=\"2.5\" y=\"4\" width=\"19\" height=\"13.5\" rx=\"2.5\"/>"
               "<polygon points=\"10 8.2 15.2 10.8 10 13.4 10 8.2\" fill=\"" +
               stroke_hex + "\" stroke=\"none\"/>"
               "<path d=\"M8 21h8\"/>" +
               tail;
    }
    if (kind == "text") {
        return head +
               "<rect x=\"3.5\" y=\"3.5\" width=\"17\" height=\"17\" rx=\"2.5\"/>"
               "<path d=\"M7.5 8.5h9M7.5 12h9M7.5 15.5h5.5\"/>" +
               tail;
    }
    if (kind == "book") {
        return head +
               "<path d=\"M2.5 5.5A2.5 2.5 0 0 1 5 3h7v17H5a2.5 2.5 0 0 0-2.5 2V5.5z\"/>"
               "<path d=\"M21.5 5.5A2.5 2.5 0 0 0 19 3h-7v17h7a2.5 2.5 0 0 1 2.5 2V5.5z\"/>" +
               tail;
    }
    if (kind == "ai") {
        return head +
               "<path d=\"M20 13.5a2.5 2.5 0 0 1-2.5 2.5H8l-4.5 4V6.5A2.5 2.5 0 0 1 6 4h6.5\"/>"
               "<path d=\"M17.5 2.8l.9 2.3 2.3.9-2.3.9-.9 2.3-.9-2.3-2.3-.9 2.3-.9.9-2.3z\"/>"
               "<path d=\"M8 9.5h5M8 12.8h7.5\"/>" +
               tail;
    }
    if (kind == "bilibili") {
        return head +
               "<path d=\"M8 3.5l2.8 2.8M16 3.5l-2.8 2.8\"/>"
               "<rect x=\"3\" y=\"6.5\" width=\"18\" height=\"13\" rx=\"3\"/>"
               "<path d=\"M8.5 11.5v2.5M15.5 11.5v2.5\"/>" +
               tail;
    }
    if (kind == "youtube") {
        return head +
               "<rect x=\"2.5\" y=\"5\" width=\"19\" height=\"14\" rx=\"4\"/>"
               "<polygon points=\"10 9 15.5 12 10 15 10 9\" fill=\"" +
               stroke_hex + "\" stroke=\"none\"/>" +
               tail;
    }
    if (kind == "music") {
        return head +
               "<path d=\"M9 18V6l11-2v12\"/>"
               "<circle cx=\"6.5\" cy=\"18\" r=\"2.5\"/>"
               "<circle cx=\"17.5\" cy=\"16\" r=\"2.5\"/>" +
               tail;
    }
    if (kind == "film_queue") {
        return head +
               "<rect x=\"3\" y=\"4\" width=\"18\" height=\"16\" rx=\"2.4\"/>"
               "<path d=\"M3 9h18M8 4l2 5M14 4l2 5\"/>"
               "<path d=\"M9.5 13h6M9.5 16.5h4\"/>" +
               tail;
    }
    if (kind == "nas") {
        return head +
               "<rect x=\"3\" y=\"4\" width=\"18\" height=\"6.5\" rx=\"1.8\"/>"
               "<rect x=\"3\" y=\"13.5\" width=\"18\" height=\"6.5\" rx=\"1.8\"/>"
               "<circle cx=\"7\" cy=\"7.2\" r=\"1\" fill=\"" + stroke_hex + "\" stroke=\"none\"/>"
               "<circle cx=\"7\" cy=\"16.8\" r=\"1\" fill=\"" + stroke_hex + "\" stroke=\"none\"/>" +
               tail;
    }
    if (kind == "globe") {
        return head +
               "<circle cx=\"12\" cy=\"12\" r=\"9\"/>"
               "<path d=\"M3.5 12h17\"/>"
               "<path d=\"M12 3a13.5 13.5 0 0 1 3.6 9 13.5 13.5 0 0 1-3.6 9 13.5 13.5 0 0 1-3.6-9A13.5 13.5 0 0 1 12 3z\"/>" +
               tail;
    }
    if (kind == "rss") {
        return head +
               "<path d=\"M4 11a9 9 0 0 1 9 9\"/>"
               "<path d=\"M4 4a16 16 0 0 1 16 16\"/>"
               "<circle cx=\"5.5\" cy=\"18.5\" r=\"1.5\" fill=\"" + stroke_hex + "\" stroke=\"none\"/>" +
               tail;
    }
    if (kind == "sliders") {
        return head +
               "<path d=\"M4 7h9M17 7h3M4 17h3M11 17h9M4 12h16\"/>"
               "<circle cx=\"15\" cy=\"7\" r=\"2\"/>"
               "<circle cx=\"9\" cy=\"17\" r=\"2\"/>" +
               tail;
    }
    if (kind == "journal") {
        return head +
               "<path d=\"M12 4L3 8.5 12 13l9-4.5L12 4z\"/>"
               "<path d=\"M6.5 10.5V16c0 1.8 2.5 3.2 5.5 3.2s5.5-1.4 5.5-3.2v-5.5\"/>" +
               tail;
    }
    if (kind == "forum") {
        return head +
               "<path d=\"M20 14.5a2.5 2.5 0 0 1-2.5 2.5H8l-4 3.5V6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8z\"/>" +
               tail;
    }
    if (kind == "ebook") {
        return head +
               "<path d=\"M5 3.5h13A1.5 1.5 0 0 1 19.5 5v15.5H6.5a2 2 0 0 1-2-2V5A1.5 1.5 0 0 1 5 3.5z\"/>"
               "<path d=\"M10 3.5v7l2.5-1.8L15 10.5v-7\"/>"
               "<path d=\"M5 17.5h14.5\"/>" +
               tail;
    }
    if (kind == "pdf_split") {
        return head +
               "<rect x=\"3\" y=\"3.5\" width=\"18\" height=\"17\" rx=\"2.2\"/>"
               "<path d=\"M12 3.5v17M6.2 8.5h3.2M6.2 12h3.2M14.6 8.5h3.2M14.6 12h3.2M14.6 15.5h2.2\"/>" +
               tail;
    }
    if (kind == "library") {
        return head +
               "<rect x=\"3.5\" y=\"5\" width=\"4\" height=\"14.5\" rx=\"1\"/>"
               "<rect x=\"9.5\" y=\"4\" width=\"4\" height=\"15.5\" rx=\"1\"/>"
               "<path d=\"M15.5 5.5l3.6-.8 1.9 14.3-3.6.8-1.9-14.3z\"/>" +
               tail;
    }
    if (kind == "archive") {
        return head +
               "<rect x=\"2.5\" y=\"4\" width=\"19\" height=\"4.5\" rx=\"1.2\"/>"
               "<path d=\"M4.5 8.5v10a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-10\"/>"
               "<path d=\"M9.5 13h5\"/>" +
               tail;
    }
    if (kind == "search") {
        return head +
               "<circle cx=\"11\" cy=\"11\" r=\"6.8\"/>"
               "<path d=\"M16.2 16.2L20.5 20.5\"/>" +
               tail;
    }
    if (kind == "chevron_down") {
        return head + "<path d=\"M6 9l6 6 6-6\"/>" + tail;
    }
    if (kind == "chevron_right") {
        return head + "<path d=\"M9 6l6 6-6 6\"/>" + tail;
    }
    return head +
           "<path d=\"M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z\"/>"
           "<path d=\"M12 12l8-4.5M12 12v9M12 12L4 7.5\"/>" +
           tail;
}

} // namespace omnidesk::host
