#include "omnidesk/markdown_view.hpp"

#include <SDL2/SDL.h>
#include <nanovg.h>
#include <yoga/YGNode.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <sstream>
#include <utility>

#include "omnidesk/image_cache.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk {
namespace {

constexpr const char* kEllipsis = "\xE2\x80\xA6";  // …

bool isAsciiSpace(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

bool isAsciiAlnum(char c) {
    const auto u = static_cast<unsigned char>(c);
    return std::isalnum(u) != 0;
}

std::string trimAscii(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && isAsciiSpace(s[b])) ++b;
    size_t e = s.size();
    while (e > b && isAsciiSpace(s[e - 1])) --e;
    return s.substr(b, e - b);
}

std::string trimRightCrLf(std::string s) {
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) {
        s.pop_back();
    }
    return s;
}

std::string toLowerAscii(std::string s) {
    for (char& c : s) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return s;
}

bool startsWith(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

size_t decodeUtf8(const std::string& s, size_t i, uint32_t& cp) {
    if (i >= s.size()) {
        cp = 0;
        return 0;
    }
    const auto c0 = static_cast<unsigned char>(s[i]);
    if ((c0 & 0x80u) == 0) {
        cp = c0;
        return 1;
    }
    if ((c0 & 0xE0u) == 0xC0u && i + 1 < s.size()) {
        cp = ((c0 & 0x1Fu) << 6) | (static_cast<unsigned char>(s[i + 1]) & 0x3Fu);
        return 2;
    }
    if ((c0 & 0xF0u) == 0xE0u && i + 2 < s.size()) {
        cp = ((c0 & 0x0Fu) << 12) |
             ((static_cast<unsigned char>(s[i + 1]) & 0x3Fu) << 6) |
             (static_cast<unsigned char>(s[i + 2]) & 0x3Fu);
        return 3;
    }
    if ((c0 & 0xF8u) == 0xF0u && i + 3 < s.size()) {
        cp = ((c0 & 0x07u) << 18) |
             ((static_cast<unsigned char>(s[i + 1]) & 0x3Fu) << 12) |
             ((static_cast<unsigned char>(s[i + 2]) & 0x3Fu) << 6) |
             (static_cast<unsigned char>(s[i + 3]) & 0x3Fu);
        return 4;
    }
    cp = c0;
    return 1;
}

bool isCjkOrWideCodepoint(uint32_t cp) {
    return (cp >= 0x2E80u && cp <= 0x9FFFu) ||    // CJK Radicals, Symbols, Unified Ideographs
           (cp >= 0xAC00u && cp <= 0xD7AFu) ||    // Hangul Syllables
           (cp >= 0xF900u && cp <= 0xFAFFu) ||    // CJK Compatibility Ideographs
           (cp >= 0xFE30u && cp <= 0xFE6Fu) ||    // CJK Compatibility Forms
           (cp >= 0xFF00u && cp <= 0xFFEFu) ||    // Fullwidth Forms
           (cp >= 0x20000u && cp <= 0x2FA1Fu) ||  // CJK Extensions B..F
           (cp >= 0x1F300u && cp <= 0x1FAFFu);    // Emoji
}

bool isNoLineStartPunct(uint32_t cp) {
    switch (cp) {
        case ',':
        case '.':
        case '!':
        case '?':
        case ';':
        case ':':
        case ')':
        case ']':
        case '}':
        case '%':
        case 0x3001u:  // 、
        case 0x3002u:  // 。
        case 0xFF0Cu:  // ，
        case 0xFF0Eu:  // ．
        case 0xFF01u:  // ！
        case 0xFF1Fu:  // ？
        case 0xFF1Bu:  // ；
        case 0xFF1Au:  // ：
        case 0xFF09u:  // ）
        case 0x300Bu:  // 》
        case 0x300Du:  // 」
        case 0x300Fu:  // 』
        case 0x3011u:  // 】
        case 0x201Du:  // ”
        case 0x2019u:  // ’
        case 0x2026u:  // …
        case 0x2014u:  // —
            return true;
        default:
            return false;
    }
}

uint32_t lastCodepointOf(const std::string& s) {
    if (s.empty()) return 0;
    size_t i = s.size() - 1;
    while (i > 0 && (static_cast<unsigned char>(s[i]) & 0xC0u) == 0x80u) {
        --i;
    }
    uint32_t cp = 0;
    decodeUtf8(s, i, cp);
    return cp;
}

uint32_t firstCodepointOf(const std::string& s) {
    if (s.empty()) return 0;
    uint32_t cp = 0;
    decodeUtf8(s, 0, cp);
    return cp;
}

bool parseFenceStart(const std::string& trimmed, char& fence_ch, size_t& fence_len, std::string& lang);
bool isFenceClose(const std::string& trimmed, char fence_ch, size_t fence_len);

std::string stripHtmlComments(const std::string& input) {
    std::vector<std::pair<size_t, size_t>> fenced_ranges;
    size_t offset = 0;
    size_t fence_start = 0;
    char fence_ch = 0;
    size_t fence_len = 0;
    bool in_fence = false;
    while (offset <= input.size()) {
        const size_t line_end = input.find('\n', offset);
        const size_t end = line_end == std::string::npos ? input.size() : line_end;
        const std::string trimmed = trimAscii(input.substr(offset, end - offset));
        if (!in_fence) {
            std::string lang;
            if (parseFenceStart(trimmed, fence_ch, fence_len, lang)) {
                in_fence = true;
                fence_start = offset;
            }
        } else if (isFenceClose(trimmed, fence_ch, fence_len)) {
            fenced_ranges.emplace_back(fence_start, end);
            in_fence = false;
        }
        if (line_end == std::string::npos) break;
        offset = line_end + 1;
    }
    if (in_fence) fenced_ranges.emplace_back(fence_start, input.size());

    std::string out;
    out.reserve(input.size());
    size_t pos = 0;
    size_t range_idx = 0;
    while (pos < input.size()) {
        const size_t open = input.find("<!--", pos);
        if (open == std::string::npos) {
            out.append(input, pos, std::string::npos);
            break;
        }
        while (range_idx < fenced_ranges.size() && fenced_ranges[range_idx].second <= open) {
            ++range_idx;
        }
        if (range_idx < fenced_ranges.size() &&
            open >= fenced_ranges[range_idx].first && open < fenced_ranges[range_idx].second) {
            out.append(input, pos, open + 4 - pos);
            pos = open + 4;
            continue;
        }
        const size_t close = input.find("-->", open + 4);
        if (close == std::string::npos) {
            out.append(input, pos, std::string::npos);
            break;
        }
        out.append(input, pos, open - pos);
        pos = close + 3;
    }
    return out;
}

std::string stripLeadingFrontmatter(const std::string& input) {
    if (!startsWith(input, "---\n") && !startsWith(input, "---\r\n")) {
        return input;
    }
    const size_t first_nl = input.find('\n');
    if (first_nl == std::string::npos) return input;
    size_t pos = first_nl + 1;
    bool saw_kv = false;
    while (pos < input.size()) {
        size_t line_end = input.find('\n', pos);
        const std::string line = trimRightCrLf(
            line_end == std::string::npos ? input.substr(pos) : input.substr(pos, line_end - pos));
        if (line == "---" || line == "...") {
            if (!saw_kv) return input;
            return line_end == std::string::npos ? std::string{} : input.substr(line_end + 1);
        }
        const size_t colon = line.find(':');
        if (colon != std::string::npos && colon > 0) {
            saw_kv = true;
        } else if (!trimAscii(line).empty() && !startsWith(trimAscii(line), "-") &&
                   !startsWith(trimAscii(line), "#")) {
            return input;
        }
        if (line_end == std::string::npos) break;
        pos = line_end + 1;
    }
    return input;
}

void replaceAllInPlace(std::string& s, const std::string& from, const std::string& to) {
    if (from.empty()) return;
    size_t pos = 0;
    while ((pos = s.find(from, pos)) != std::string::npos) {
        s.replace(pos, from.size(), to);
        pos += to.size();
    }
}

std::string mapSuperscriptChar(char c) {
    switch (c) {
        case '0': return "\xE2\x81\xB0";
        case '1': return "\xC2\xB9";
        case '2': return "\xC2\xB2";
        case '3': return "\xC2\xB3";
        case '4': return "\xE2\x81\xB4";
        case '5': return "\xE2\x81\xB5";
        case '6': return "\xE2\x81\xB6";
        case '7': return "\xE2\x81\xB7";
        case '8': return "\xE2\x81\xB8";
        case '9': return "\xE2\x81\xB9";
        case '+': return "\xE2\x81\xBA";
        case '-': return "\xE2\x81\xBB";
        case '=': return "\xE2\x81\xBC";
        case '(': return "\xE2\x81\xBD";
        case ')': return "\xE2\x81\xBE";
        case 'n': return "\xE2\x81\xBF";
        case 'i': return "\xE2\x81\xB1";
        default: return "";
    }
}

std::string mapSubscriptChar(char c) {
    switch (c) {
        case '0': return "\xE2\x82\x80";
        case '1': return "\xE2\x82\x81";
        case '2': return "\xE2\x82\x82";
        case '3': return "\xE2\x82\x83";
        case '4': return "\xE2\x82\x84";
        case '5': return "\xE2\x82\x85";
        case '6': return "\xE2\x82\x86";
        case '7': return "\xE2\x82\x87";
        case '8': return "\xE2\x82\x88";
        case '9': return "\xE2\x82\x89";
        case '+': return "\xE2\x82\x8A";
        case '-': return "\xE2\x82\x8B";
        case '=': return "\xE2\x82\x8C";
        case '(': return "\xE2\x82\x8D";
        case ')': return "\xE2\x82\x8E";
        case 'a': return "\xE2\x82\x90";
        case 'e': return "\xE2\x82\x91";
        case 'i': return "\xE1\xB5\xA2";
        case 'k': return "\xE2\x82\x96";
        case 'm': return "\xE2\x82\x98";
        case 'n': return "\xE2\x82\x99";
        case 'o': return "\xE2\x82\x92";
        case 'p': return "\xE2\x82\x9A";
        case 'r': return "\xE1\xB5\xA3";
        case 's': return "\xE2\x82\x9B";
        case 't': return "\xE2\x82\x9C";
        case 'x': return "\xE2\x82\x93";
        default: return "";
    }
}

struct StyleFlags {
    bool bold = false;
    bool italic = false;
    bool code = false;
    bool strikethrough = false;
    bool highlight = false;
    bool math = false;
    std::string link_url;
};

void pushSpanMerged(std::vector<MarkdownInlineSpan>& out,
                    const std::string& text,
                    const StyleFlags& flags) {
    if (text.empty()) return;
    if (!out.empty()) {
        auto& prev = out.back();
        if (prev.bold == flags.bold && prev.italic == flags.italic &&
            prev.code == flags.code && prev.strikethrough == flags.strikethrough &&
            prev.highlight == flags.highlight && prev.math == flags.math &&
            prev.link_url == flags.link_url) {
            prev.text += text;
            return;
        }
    }
    MarkdownInlineSpan span;
    span.text = text;
    span.link_url = flags.link_url;
    span.bold = flags.bold;
    span.italic = flags.italic;
    span.code = flags.code;
    span.strikethrough = flags.strikethrough;
    span.highlight = flags.highlight;
    span.math = flags.math;
    out.push_back(std::move(span));
}

bool matchBracketedLink(const std::string& s,
                        size_t open_bracket,
                        std::string& out_label,
                        std::string& out_url,
                        size_t& out_end) {
    if (open_bracket >= s.size() || s[open_bracket] != '[') return false;
    int depth = 0;
    size_t close_bracket = std::string::npos;
    for (size_t i = open_bracket; i < s.size(); ++i) {
        if (s[i] == '\\' && i + 1 < s.size()) {
            ++i;
            continue;
        }
        if (s[i] == '[') {
            ++depth;
        } else if (s[i] == ']') {
            --depth;
            if (depth == 0) {
                close_bracket = i;
                break;
            }
        } else if (s[i] == '\n') {
            return false;
        }
    }
    if (close_bracket == std::string::npos || close_bracket + 1 >= s.size() ||
        s[close_bracket + 1] != '(') {
        return false;
    }

    size_t url_start = close_bracket + 2;
    while (url_start < s.size() && (s[url_start] == ' ' || s[url_start] == '\t')) ++url_start;
    int paren_depth = 1;
    size_t close_paren = std::string::npos;
    bool in_quotes = false;
    char quote_ch = 0;
    for (size_t i = url_start; i < s.size(); ++i) {
        if (s[i] == '\\' && i + 1 < s.size()) {
            ++i;
            continue;
        }
        if (s[i] == '\n') return false;
        if (in_quotes) {
            if (s[i] == quote_ch) in_quotes = false;
            continue;
        }
        if (s[i] == '"' || s[i] == '\'') {
            in_quotes = true;
            quote_ch = s[i];
            continue;
        }
        if (s[i] == '(') {
            ++paren_depth;
        } else if (s[i] == ')') {
            --paren_depth;
            if (paren_depth == 0) {
                close_paren = i;
                break;
            }
        }
    }
    if (close_paren == std::string::npos) return false;

    std::string raw_dest = trimAscii(s.substr(url_start, close_paren - url_start));
    if (!raw_dest.empty() && raw_dest.front() == '<') {
        const size_t gt = raw_dest.find('>');
        if (gt != std::string::npos) {
            raw_dest = raw_dest.substr(1, gt - 1);
        }
    } else {
        // Strip optional title: url "title"
        size_t sp = 0;
        while (sp < raw_dest.size() && raw_dest[sp] != ' ' && raw_dest[sp] != '\t') ++sp;
        if (sp < raw_dest.size()) {
            raw_dest = raw_dest.substr(0, sp);
        }
    }
    out_label = s.substr(open_bracket + 1, close_bracket - open_bracket - 1);
    out_url = raw_dest;
    out_end = close_paren + 1;
    return true;
}

size_t findClosingDelimiter(const std::string& s,
                            size_t from,
                            const std::string& delim,
                            bool check_underscore_boundary) {
    const size_t n = delim.size();
    for (size_t i = from; i + n <= s.size(); ++i) {
        if (s[i] == '\\') {
            ++i;
            continue;
        }
        if (s[i] == '`') {
            size_t ticks = 1;
            while (i + ticks < s.size() && s[i + ticks] == '`') ++ticks;
            const std::string fence(ticks, '`');
            const size_t close_tick = s.find(fence, i + ticks);
            if (close_tick != std::string::npos) {
                i = close_tick + ticks - 1;
                continue;
            }
        }
        if (s.compare(i, n, delim) == 0) {
            if (i == from) continue;  // empty content
            // Don't match if preceded by space for emphasis
            if ((delim[0] == '*' || delim[0] == '_') && isAsciiSpace(s[i - 1])) {
                continue;
            }
            // Don't split *** when looking for * or ** if extra chars belong to longer run
            if (delim[0] == '*' || delim[0] == '_') {
                const char ch = delim[0];
                size_t run = n;
                while (i + run < s.size() && s[i + run] == ch) ++run;
                if (run > n && n == 2 && run >= 3) {
                    // Allow closing ** at the end of *** if needed
                }
            }
            if (check_underscore_boundary) {
                const size_t after = i + n;
                if (after < s.size() && isAsciiAlnum(s[after])) {
                    continue;
                }
            }
            return i;
        }
    }
    return std::string::npos;
}

void parseInlineRange(const std::string& s,
                      size_t begin,
                      size_t end,
                      StyleFlags flags,
                      std::vector<MarkdownInlineSpan>& out) {
    std::string plain;
    auto flushPlain = [&]() {
        if (!plain.empty()) {
            pushSpanMerged(out, plain, flags);
            plain.clear();
        }
    };

    size_t i = begin;
    while (i < end) {
        const char c = s[i];

        // 1. Backslash escape
        if (c == '\\' && i + 1 < end) {
            const char next = s[i + 1];
            if (next == '\n') {
                flushPlain();
                pushSpanMerged(out, "\n", flags);
                i += 2;
                continue;
            }
            if (std::ispunct(static_cast<unsigned char>(next))) {
                plain.push_back(next);
                i += 2;
                continue;
            }
        }

        // 2. Backtick code span
        if (c == '`') {
            size_t ticks = 1;
            while (i + ticks < end && s[i + ticks] == '`') ++ticks;
            const std::string fence(ticks, '`');
            size_t close = s.find(fence, i + ticks);
            if (close != std::string::npos && close < end) {
                std::string code_txt = s.substr(i + ticks, close - (i + ticks));
                if (code_txt.size() >= 2 && code_txt.front() == ' ' && code_txt.back() == ' ' &&
                    code_txt.find_first_not_of(' ') != std::string::npos) {
                    code_txt = code_txt.substr(1, code_txt.size() - 2);
                }
                flushPlain();
                StyleFlags cf = flags;
                cf.code = true;
                pushSpanMerged(out, code_txt, cf);
                i = close + ticks;
                continue;
            }
            plain.append(ticks, '`');
            i += ticks;
            continue;
        }

        // 3. Inline math $...$ (Pandoc/GFM rules to avoid $100 currency false positives)
        if (c == '$' && (i + 1 < end && s[i + 1] != '$' && !isAsciiSpace(s[i + 1]))) {
            const bool prev_digit = (i > begin && std::isdigit(static_cast<unsigned char>(s[i - 1])));
            if (!prev_digit) {
                size_t close = std::string::npos;
                for (size_t k = i + 1; k < end; ++k) {
                    if (s[k] == '\\' && k + 1 < end) {
                        ++k;
                        continue;
                    }
                    if (s[k] == '\n') break;
                    if (s[k] == '$' && !isAsciiSpace(s[k - 1])) {
                        const bool next_digit =
                            (k + 1 < end && std::isdigit(static_cast<unsigned char>(s[k + 1])));
                        if (!next_digit) {
                            close = k;
                            break;
                        }
                    }
                }
                if (close != std::string::npos && close > i + 1) {
                    const std::string raw_math = s.substr(i + 1, close - (i + 1));
                    // Only treat as math if it contains math syntax/letters rather than pure digits
                    bool has_non_digit = false;
                    for (char mc : raw_math) {
                        if (!std::isdigit(static_cast<unsigned char>(mc)) && mc != '.' && mc != ',') {
                            has_non_digit = true;
                            break;
                        }
                    }
                    if (has_non_digit) {
                        flushPlain();
                        StyleFlags mf = flags;
                        mf.math = true;
                        mf.italic = true;
                        pushSpanMerged(out, prettifyLatexMath(raw_math), mf);
                        i = close + 1;
                        continue;
                    }
                }
            }
        }

        // 4. Obsidian Wikilink [[target|alias]] or embedded ![[target]]
        if ((c == '[' && i + 1 < end && s[i + 1] == '[') ||
            (c == '!' && i + 2 < end && s[i + 1] == '[' && s[i + 2] == '[')) {
            const bool is_embed = (c == '!');
            const size_t start_inner = i + (is_embed ? 3 : 2);
            const size_t close = s.find("]]", start_inner);
            if (close != std::string::npos && close < end &&
                s.find('\n', start_inner) > close) {
                const std::string inner = s.substr(start_inner, close - start_inner);
                const size_t pipe = inner.find('|');
                std::string target = trimAscii(pipe == std::string::npos ? inner : inner.substr(0, pipe));
                std::string label =
                    trimAscii(pipe == std::string::npos ? inner : inner.substr(pipe + 1));
                if (pipe == std::string::npos) {
                    const size_t slash = label.find_last_of('/');
                    if (slash != std::string::npos && slash + 1 < label.size()) {
                        label = label.substr(slash + 1);
                    }
                }
                if (!label.empty()) {
                    flushPlain();
                    StyleFlags lf = flags;
                    lf.link_url = "wikilink:" + target;
                    pushSpanMerged(out, is_embed ? ("[图: " + label + "]") : label, lf);
                    i = close + 2;
                    continue;
                }
            }
        }

        // 5. Inline image ![alt](url)
        if (c == '!' && i + 1 < end && s[i + 1] == '[') {
            std::string alt, url;
            size_t next_pos = 0;
            if (matchBracketedLink(s.substr(0, end), i + 1, alt, url, next_pos)) {
                flushPlain();
                StyleFlags lf = flags;
                lf.link_url = url;
                const std::string label = trimAscii(alt).empty() ? "[图片]" : ("[图: " + trimAscii(alt) + "]");
                pushSpanMerged(out, label, lf);
                i = next_pos;
                continue;
            }
        }

        // 6. Standard Markdown Link [label](url)
        if (c == '[' && flags.link_url.empty()) {
            std::string label, url;
            size_t next_pos = 0;
            if (matchBracketedLink(s.substr(0, end), i, label, url, next_pos)) {
                flushPlain();
                StyleFlags lf = flags;
                lf.link_url = url;
                if (label.empty()) {
                    pushSpanMerged(out, url, lf);
                } else {
                    parseInlineRange(label, 0, label.size(), lf, out);
                }
                i = next_pos;
                continue;
            }
        }

        // 7. Angle-bracket autolink <https://...>
        if (c == '<' && flags.link_url.empty()) {
            const size_t gt = s.find('>', i + 1);
            if (gt != std::string::npos && gt < end) {
                const std::string candidate = s.substr(i + 1, gt - (i + 1));
                if ((startsWith(candidate, "http://") || startsWith(candidate, "https://")) &&
                    candidate.find_first_of(" \t\r\n") == std::string::npos) {
                    flushPlain();
                    StyleFlags lf = flags;
                    lf.link_url = candidate;
                    pushSpanMerged(out, candidate, lf);
                    i = gt + 1;
                    continue;
                }
            }
        }

        // 8. Bare URL https://... or http://...
        if (flags.link_url.empty() && (c == 'h' || c == 'H') &&
            (startsWith(s.substr(i, std::min<size_t>(8, end - i)), "https://") ||
             startsWith(s.substr(i, std::min<size_t>(7, end - i)), "http://"))) {
            const bool valid_boundary =
                (i == begin) || (!isAsciiAlnum(s[i - 1]) && s[i - 1] != '/');
            if (valid_boundary) {
                size_t uend = i;
                int open_parens = 0;
                while (uend < end) {
                    const unsigned char uc = static_cast<unsigned char>(s[uend]);
                    if (uc <= 0x20u || uc >= 0x7Fu || s[uend] == '<' || s[uend] == '>' ||
                        s[uend] == '"' || s[uend] == '`') {
                        break;
                    }
                    if (s[uend] == '(') ++open_parens;
                    if (s[uend] == ')') {
                        if (open_parens <= 0) break;
                        --open_parens;
                    }
                    ++uend;
                }
                while (uend > i && (s[uend - 1] == '.' || s[uend - 1] == ',' ||
                                    s[uend - 1] == ';' || s[uend - 1] == ':' ||
                                    s[uend - 1] == '!' || s[uend - 1] == '?')) {
                    --uend;
                }
                if (uend > i + 8) {
                    const std::string url = s.substr(i, uend - i);
                    flushPlain();
                    StyleFlags lf = flags;
                    lf.link_url = url;
                    pushSpanMerged(out, url, lf);
                    i = uend;
                    continue;
                }
            }
        }

        // 9. Strikethrough ~~...~~
        if (c == '~' && i + 1 < end && s[i + 1] == '~') {
            const size_t close = findClosingDelimiter(s.substr(0, end), i + 2, "~~", false);
            if (close != std::string::npos) {
                flushPlain();
                StyleFlags sf = flags;
                sf.strikethrough = true;
                parseInlineRange(s, i + 2, close, sf, out);
                i = close + 2;
                continue;
            }
        }

        // 10. Highlight ==...== (Obsidian)
        if (c == '=' && i + 1 < end && s[i + 1] == '=' &&
            (i + 2 < end && s[i + 2] != '=' && !isAsciiSpace(s[i + 2]))) {
            const size_t close = findClosingDelimiter(s.substr(0, end), i + 2, "==", false);
            if (close != std::string::npos) {
                flushPlain();
                StyleFlags hf = flags;
                hf.highlight = true;
                parseInlineRange(s, i + 2, close, hf, out);
                i = close + 2;
                continue;
            }
        }

        // 11. Bold / Italic (* and _)
        if (c == '*' || c == '_') {
            const bool is_under = (c == '_');
            const bool prev_alnum = (i > begin && isAsciiAlnum(s[i - 1]));
            if (!is_under || !prev_alnum) {
                size_t run = 1;
                while (i + run < end && s[i + run] == c) ++run;

                if (run >= 3 && i + 3 < end && !isAsciiSpace(s[i + 3])) {
                    const std::string d3(3, c);
                    const size_t close =
                        findClosingDelimiter(s.substr(0, end), i + 3, d3, is_under);
                    if (close != std::string::npos) {
                        flushPlain();
                        StyleFlags bf = flags;
                        bf.bold = true;
                        bf.italic = true;
                        parseInlineRange(s, i + 3, close, bf, out);
                        i = close + 3;
                        continue;
                    }
                }
                if (run >= 2 && i + 2 < end && !isAsciiSpace(s[i + 2])) {
                    const std::string d2(2, c);
                    const size_t close =
                        findClosingDelimiter(s.substr(0, end), i + 2, d2, is_under);
                    if (close != std::string::npos) {
                        flushPlain();
                        StyleFlags bf = flags;
                        bf.bold = true;
                        parseInlineRange(s, i + 2, close, bf, out);
                        i = close + 2;
                        continue;
                    }
                }
                if (run == 1 && i + 1 < end && !isAsciiSpace(s[i + 1])) {
                    const std::string d1(1, c);
                    const size_t close =
                        findClosingDelimiter(s.substr(0, end), i + 1, d1, is_under);
                    if (close != std::string::npos) {
                        flushPlain();
                        StyleFlags itf = flags;
                        itf.italic = true;
                        parseInlineRange(s, i + 1, close, itf, out);
                        i = close + 1;
                        continue;
                    }
                }
            }
        }

        plain.push_back(c);
        ++i;
    }

    flushPlain();
}

// --- Block-level parser helpers ---

size_t countLeadingIndentCols(const std::string& line, size_t& first_non_space) {
    size_t cols = 0;
    size_t i = 0;
    while (i < line.size()) {
        if (line[i] == ' ') {
            ++cols;
            ++i;
        } else if (line[i] == '\t') {
            cols += 4;
            ++i;
        } else {
            break;
        }
    }
    first_non_space = i;
    return cols;
}

bool isHorizontalRuleLine(const std::string& trimmed) {
    if (trimmed.size() < 3) return false;
    const char marker = trimmed.front();
    if (marker != '-' && marker != '*' && marker != '_') return false;
    int count = 0;
    for (char c : trimmed) {
        if (c == marker) {
            ++count;
        } else if (c != ' ' && c != '\t') {
            return false;
        }
    }
    return count >= 3;
}

bool parseAtxHeadingLine(const std::string& trimmed, int& out_level, std::string& out_text) {
    size_t hashes = 0;
    while (hashes < trimmed.size() && hashes < 7 && trimmed[hashes] == '#') {
        ++hashes;
    }
    if (hashes == 0 || hashes > 6) return false;
    if (hashes < trimmed.size() && trimmed[hashes] != ' ' && trimmed[hashes] != '\t') {
        return false;
    }
    std::string body = trimAscii(trimmed.substr(hashes));
    // Strip optional closing ATX hashes: "## Title ##"
    if (!body.empty() && body.back() == '#') {
        size_t p = body.size();
        while (p > 0 && body[p - 1] == '#') --p;
        if (p == 0 || body[p - 1] == ' ' || body[p - 1] == '\t') {
            body = trimAscii(body.substr(0, p));
        }
    }
    out_level = static_cast<int>(hashes);
    out_text = std::move(body);
    return true;
}

bool parseFenceStart(const std::string& trimmed, char& fence_ch, size_t& fence_len, std::string& lang) {
    if (trimmed.size() < 3) return false;
    const char ch = trimmed.front();
    if (ch != '`' && ch != '~') return false;
    size_t n = 0;
    while (n < trimmed.size() && trimmed[n] == ch) ++n;
    if (n < 3) return false;
    std::string info = trimAscii(trimmed.substr(n));
    if (ch == '`' && info.find('`') != std::string::npos) return false;
    size_t sp = 0;
    while (sp < info.size() && !isAsciiSpace(info[sp])) ++sp;
    fence_ch = ch;
    fence_len = n;
    lang = info.substr(0, sp);
    return true;
}

bool isFenceClose(const std::string& trimmed, char fence_ch, size_t fence_len) {
    if (trimmed.size() < fence_len) return false;
    size_t n = 0;
    while (n < trimmed.size() && trimmed[n] == fence_ch) ++n;
    if (n < fence_len) return false;
    return trimAscii(trimmed.substr(n)).empty();
}

bool parseListMarker(const std::string& line,
                     int& out_indent_level,
                     bool& out_ordered,
                     int& out_number,
                     int& out_task_state,
                     std::string& out_rest) {
    size_t idx = 0;
    const size_t indent_cols = countLeadingIndentCols(line, idx);
    if (idx >= line.size()) return false;
    if (isHorizontalRuleLine(trimAscii(line))) return false;

    const char c = line[idx];
    bool matched = false;
    bool ordered = false;
    int number = 1;
    size_t after_marker = idx;

    if ((c == '-' || c == '*' || c == '+') && idx + 1 < line.size() &&
        (line[idx + 1] == ' ' || line[idx + 1] == '\t')) {
        matched = true;
        ordered = false;
        after_marker = idx + 2;
    } else if (std::isdigit(static_cast<unsigned char>(c))) {
        size_t d = idx;
        while (d < line.size() && d - idx < 9 && std::isdigit(static_cast<unsigned char>(line[d]))) {
            ++d;
        }
        if (d < line.size() && (line[d] == '.' || line[d] == ')') && d + 1 < line.size() &&
            (line[d + 1] == ' ' || line[d + 1] == '\t')) {
            matched = true;
            ordered = true;
            try {
                number = std::stoi(line.substr(idx, d - idx));
            } catch (...) {
                number = 1;
            }
            after_marker = d + 2;
        }
    }

    if (!matched) return false;

    std::string rest = trimAscii(line.substr(after_marker));
    int task_state = -1;
    if (rest.size() >= 3 && rest[0] == '[' && rest[2] == ']' &&
        (rest.size() == 3 || rest[3] == ' ' || rest[3] == '\t')) {
        if (rest[1] == ' ') {
            task_state = 0;
            rest = trimAscii(rest.substr(3));
        } else if (rest[1] == 'x' || rest[1] == 'X') {
            task_state = 1;
            rest = trimAscii(rest.substr(3));
        }
    }

    out_indent_level = static_cast<int>(indent_cols / 2);
    out_ordered = ordered;
    out_number = number;
    out_task_state = task_state;
    out_rest = std::move(rest);
    return true;
}

std::vector<std::string> splitPipeRow(const std::string& raw_line) {
    std::string s = trimAscii(raw_line);
    if (!s.empty() && s.front() == '|') s.erase(s.begin());
    if (!s.empty() && s.back() == '|') {
        if (s.size() < 2 || s[s.size() - 2] != '\\') {
            s.pop_back();
        }
    }
    std::vector<std::string> cells;
    std::string cur;
    bool in_code = false;
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '\\' && i + 1 < s.size() && s[i + 1] == '|') {
            cur.push_back('|');
            ++i;
            continue;
        }
        if (s[i] == '`') {
            in_code = !in_code;
            cur.push_back('`');
            continue;
        }
        if (s[i] == '|' && !in_code) {
            cells.push_back(trimAscii(cur));
            cur.clear();
            continue;
        }
        cur.push_back(s[i]);
    }
    cells.push_back(trimAscii(cur));
    return cells;
}

bool parseTableDelimiterRow(const std::string& raw_line,
                            size_t expected_cols,
                            std::vector<MarkdownTableAlign>& out_aligns) {
    if (raw_line.find('|') == std::string::npos || raw_line.find('-') == std::string::npos) {
        return false;
    }
    const auto cells = splitPipeRow(raw_line);
    if (cells.empty() || (expected_cols > 0 && cells.size() != expected_cols)) {
        return false;
    }
    std::vector<MarkdownTableAlign> aligns;
    aligns.reserve(cells.size());
    for (const auto& cell : cells) {
        if (cell.empty()) return false;
        size_t b = 0;
        size_t e = cell.size();
        const bool left_colon = (cell.front() == ':');
        const bool right_colon = (cell.back() == ':' && cell.size() > 1);
        if (left_colon) ++b;
        if (right_colon) --e;
        if (e <= b) return false;
        size_t dashes = 0;
        for (size_t i = b; i < e; ++i) {
            if (cell[i] != '-') return false;
            ++dashes;
        }
        if (dashes < 1) return false;
        if (left_colon && right_colon) aligns.push_back(MarkdownTableAlign::Center);
        else if (right_colon) aligns.push_back(MarkdownTableAlign::Right);
        else aligns.push_back(MarkdownTableAlign::Left);
    }
    out_aligns = std::move(aligns);
    return true;
}

bool parseStandaloneImageLine(const std::string& trimmed,
                              std::string& out_url,
                              std::string& out_alt) {
    if (startsWith(trimmed, "![[") && trimmed.size() > 5 &&
        trimmed.substr(trimmed.size() - 2) == "]]") {
        const std::string inner = trimmed.substr(3, trimmed.size() - 5);
        const size_t pipe = inner.find('|');
        const std::string target = trimAscii(pipe == std::string::npos ? inner : inner.substr(0, pipe));
        const std::string alt = trimAscii(pipe == std::string::npos ? "" : inner.substr(pipe + 1));
        const std::string lower = toLowerAscii(target);
        if (lower.find(".png") != std::string::npos || lower.find(".jpg") != std::string::npos ||
            lower.find(".jpeg") != std::string::npos || lower.find(".webp") != std::string::npos ||
            lower.find(".gif") != std::string::npos || lower.find(".svg") != std::string::npos) {
            out_url = target;
            out_alt = alt;
            return true;
        }
    }
    if (startsWith(trimmed, "![")) {
        std::string alt, url;
        size_t end_pos = 0;
        if (matchBracketedLink(trimmed, 1, alt, url, end_pos) &&
            trimAscii(trimmed.substr(end_pos)).empty() && !url.empty()) {
            out_url = std::move(url);
            out_alt = trimAscii(alt);
            return true;
        }
    }
    return false;
}

bool parseCalloutHeader(const std::string& first_quote_line,
                        std::string& out_type,
                        std::string& out_title,
                        bool& out_foldable,
                        bool& out_collapsed) {
    const std::string t = trimAscii(first_quote_line);
    if (!startsWith(t, "[!")) return false;
    const size_t close = t.find(']', 2);
    if (close == std::string::npos || close <= 2) return false;
    std::string type = trimAscii(t.substr(2, close - 2));
    for (char c : type) {
        if (!isAsciiAlnum(c) && c != '-' && c != '_') return false;
    }
    size_t pos = close + 1;
    bool foldable = false;
    bool collapsed = false;
    if (pos < t.size() && (t[pos] == '+' || t[pos] == '-')) {
        foldable = true;
        collapsed = (t[pos] == '-');
        ++pos;
    }
    std::string title = trimAscii(t.substr(pos));
    out_type = toLowerAscii(type);
    if (title.empty()) {
        title = type;
        if (!title.empty()) {
            title[0] = static_cast<char>(std::toupper(static_cast<unsigned char>(title[0])));
        }
    }
    out_title = std::move(title);
    out_foldable = foldable;
    out_collapsed = collapsed;
    return true;
}

void joinSoftLine(std::string& dst, const std::string& next_line, bool force_newline) {
    if (dst.empty()) {
        dst = next_line;
        return;
    }
    if (force_newline) {
        dst.push_back('\n');
        dst += next_line;
        return;
    }
    const uint32_t prev_cp = lastCodepointOf(dst);
    const uint32_t next_cp = firstCodepointOf(next_line);
    if (isCjkOrWideCodepoint(prev_cp) && isCjkOrWideCodepoint(next_cp)) {
        dst += next_line;
    } else {
        dst.push_back(' ');
        dst += next_line;
    }
}

bool lineHasTrailingHardBreak(std::string& raw_line) {
    size_t spaces = 0;
    while (spaces < raw_line.size() && raw_line[raw_line.size() - 1 - spaces] == ' ') {
        ++spaces;
    }
    if (spaces >= 2) {
        raw_line.erase(raw_line.size() - spaces);
        return true;
    }
    if (!raw_line.empty() && raw_line.back() == '\\') {
        raw_line.pop_back();
        return true;
    }
    return false;
}

bool isBlockStarterAt(const std::vector<std::string>& lines, size_t idx) {
    if (idx >= lines.size()) return false;
    const std::string& raw = lines[idx];
    const std::string t = trimAscii(raw);
    if (t.empty()) return true;
    int h_lvl = 0;
    std::string h_txt;
    if (parseAtxHeadingLine(t, h_lvl, h_txt)) return true;
    char fc = 0;
    size_t fl = 0;
    std::string flang;
    if (parseFenceStart(t, fc, fl, flang)) return true;
    if (startsWith(t, "$$")) return true;
    if (startsWith(t, ">")) return true;
    if (isHorizontalRuleLine(t)) return true;
    std::string img_u, img_a;
    if (parseStandaloneImageLine(t, img_u, img_a)) return true;
    int li_ind = 0, li_num = 1, li_task = -1;
    bool li_ord = false;
    std::string li_rest;
    if (parseListMarker(raw, li_ind, li_ord, li_num, li_task, li_rest)) return true;
    if (idx + 1 < lines.size() && t.find('|') != std::string::npos) {
        const auto hdr = splitPipeRow(t);
        std::vector<MarkdownTableAlign> aligns;
        if (!hdr.empty() && parseTableDelimiterRow(lines[idx + 1], hdr.size(), aligns)) {
            return true;
        }
    }
    return false;
}

}  // namespace

std::string prettifyLatexMath(const std::string& latex) {
    std::string s = trimAscii(latex);
    if (s.empty()) return s;

    // Strip \text{...}, \mathrm{...}, \mathbf{...} wrappers preserving inner text
    for (const char* cmd : {"\\text{", "\\mathrm{", "\\mathbf{", "\\mathit{", "\\operatorname{"}) {
        const std::string prefix(cmd);
        size_t pos = 0;
        while ((pos = s.find(prefix, pos)) != std::string::npos) {
            const size_t inner_start = pos + prefix.size();
            int depth = 1;
            size_t k = inner_start;
            while (k < s.size() && depth > 0) {
                if (s[k] == '{') ++depth;
                else if (s[k] == '}') --depth;
                ++k;
            }
            if (depth == 0) {
                const std::string inner = s.substr(inner_start, k - 1 - inner_start);
                s.replace(pos, k - pos, inner);
                pos += inner.size();
            } else {
                pos += prefix.size();
            }
        }
    }

    // \frac{a}{b} -> (a)/(b)
    {
        size_t pos = 0;
        while ((pos = s.find("\\frac{", pos)) != std::string::npos) {
            auto readBraced = [&](size_t open_brace, std::string& out_val, size_t& after) -> bool {
                if (open_brace >= s.size() || s[open_brace] != '{') return false;
                int depth = 1;
                size_t k = open_brace + 1;
                while (k < s.size() && depth > 0) {
                    if (s[k] == '{') ++depth;
                    else if (s[k] == '}') --depth;
                    ++k;
                }
                if (depth != 0) return false;
                out_val = s.substr(open_brace + 1, k - 1 - (open_brace + 1));
                after = k;
                return true;
            };
            std::string num, den;
            size_t after_num = 0, after_den = 0;
            if (readBraced(pos + 5, num, after_num) && readBraced(after_num, den, after_den)) {
                const std::string repl =
                    (num.size() > 1 ? ("(" + num + ")") : num) + "/" +
                    (den.size() > 1 ? ("(" + den + ")") : den);
                s.replace(pos, after_den - pos, repl);
                pos += repl.size();
            } else {
                pos += 6;
            }
        }
    }

    static const std::pair<const char*, const char*> kSymbols[] = {
        {"\\Longleftrightarrow", "⟺"},
        {"\\Leftrightarrow", "⇔"},
        {"\\longrightarrow", "⟶"},
        {"\\rightarrow", "→"},
        {"\\leftarrow", "←"},
        {"\\Rightarrow", "⇒"},
        {"\\Leftarrow", "⇐"},
        {"\\mapsto", "↦"},
        {"\\to", "→"},
        {"\\times", "×"},
        {"\\cdot", "·"},
        {"\\pm", "±"},
        {"\\mp", "∓"},
        {"\\leq", "≤"},
        {"\\le", "≤"},
        {"\\geq", "≥"},
        {"\\ge", "≥"},
        {"\\neq", "≠"},
        {"\\ne", "≠"},
        {"\\approx", "≈"},
        {"\\equiv", "≡"},
        {"\\sim", "∼"},
        {"\\propto", "∝"},
        {"\\infty", "∞"},
        {"\\partial", "∂"},
        {"\\nabla", "∇"},
        {"\\sum", "∑"},
        {"\\prod", "∏"},
        {"\\int", "∫"},
        {"\\sqrt", "√"},
        {"\\in", "∈"},
        {"\\notin", "∉"},
        {"\\subset", "⊂"},
        {"\\subseteq", "⊆"},
        {"\\cup", "∪"},
        {"\\cap", "∩"},
        {"\\forall", "∀"},
        {"\\exists", "∃"},
        {"\\alpha", "α"},
        {"\\beta", "β"},
        {"\\gamma", "γ"},
        {"\\delta", "δ"},
        {"\\epsilon", "ε"},
        {"\\varepsilon", "ε"},
        {"\\zeta", "ζ"},
        {"\\eta", "η"},
        {"\\theta", "θ"},
        {"\\kappa", "κ"},
        {"\\lambda", "λ"},
        {"\\mu", "μ"},
        {"\\nu", "ν"},
        {"\\xi", "ξ"},
        {"\\pi", "π"},
        {"\\rho", "ρ"},
        {"\\sigma", "σ"},
        {"\\tau", "τ"},
        {"\\phi", "φ"},
        {"\\varphi", "φ"},
        {"\\chi", "χ"},
        {"\\psi", "ψ"},
        {"\\omega", "ω"},
        {"\\Delta", "Δ"},
        {"\\Gamma", "Γ"},
        {"\\Lambda", "Λ"},
        {"\\Omega", "Ω"},
        {"\\Phi", "Φ"},
        {"\\Pi", "Π"},
        {"\\Sigma", "Σ"},
        {"\\Theta", "Θ"},
        {"\\left", ""},
        {"\\right", ""},
        {"\\,", " "},
        {"\\;", " "},
        {"\\!", ""},
    };
    for (const auto& [src, dst] : kSymbols) {
        replaceAllInPlace(s, src, dst);
    }

    // Superscripts / subscripts: ^{...}, ^x, _{...}, _x
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size(); ++i) {
        if ((s[i] == '^' || s[i] == '_') && i + 1 < s.size()) {
            const bool is_sup = (s[i] == '^');
            std::string group;
            size_t next_i = i;
            if (s[i + 1] == '{') {
                const size_t close = s.find('}', i + 2);
                if (close != std::string::npos) {
                    group = s.substr(i + 2, close - (i + 2));
                    next_i = close;
                }
            } else {
                group.push_back(s[i + 1]);
                next_i = i + 1;
            }
            if (!group.empty()) {
                std::string mapped;
                bool all_mapped = true;
                for (char gc : group) {
                    const std::string m = is_sup ? mapSuperscriptChar(gc) : mapSubscriptChar(gc);
                    if (m.empty()) {
                        all_mapped = false;
                        break;
                    }
                    mapped += m;
                }
                if (all_mapped) {
                    out += mapped;
                    i = next_i;
                    continue;
                }
                if (s[i + 1] == '{') {
                    out.push_back(s[i]);
                    out += group;
                    i = next_i;
                    continue;
                }
            }
        }
        if (s[i] == '{' || s[i] == '}') continue;
        out.push_back(s[i]);
    }
    return out;
}

std::vector<MarkdownInlineSpan> parseMarkdownInlines(const std::string& markdown) {
    std::vector<MarkdownInlineSpan> out;
    if (markdown.empty()) return out;
    StyleFlags root_flags;
    parseInlineRange(markdown, 0, markdown.size(), root_flags, out);
    return out;
}

std::vector<MarkdownBlock> parseMarkdownBlocks(const std::string& markdown) {
    std::vector<MarkdownBlock> blocks;
    if (markdown.empty()) return blocks;

    std::string cleaned = stripHtmlComments(stripLeadingFrontmatter(markdown));
    std::vector<std::string> lines;
    {
        std::istringstream iss(cleaned);
        std::string line;
        while (std::getline(iss, line)) {
            lines.push_back(trimRightCrLf(std::move(line)));
        }
    }

    size_t i = 0;
    while (i < lines.size()) {
        const std::string& raw = lines[i];
        const std::string trimmed = trimAscii(raw);

        if (trimmed.empty()) {
            ++i;
            continue;
        }

        // 1. Fenced Code Block
        char fence_ch = 0;
        size_t fence_len = 0;
        std::string fence_lang;
        if (parseFenceStart(trimmed, fence_ch, fence_len, fence_lang)) {
            ++i;
            std::string code;
            while (i < lines.size()) {
                if (isFenceClose(trimAscii(lines[i]), fence_ch, fence_len)) {
                    ++i;
                    break;
                }
                if (!code.empty()) code.push_back('\n');
                code += lines[i];
                ++i;
            }
            MarkdownBlock blk;
            blk.kind = MarkdownBlockKind::CodeBlock;
            blk.code_lang = std::move(fence_lang);
            blk.text = std::move(code);
            blocks.push_back(std::move(blk));
            continue;
        }

        // 2. Display Math Block $$ ... $$
        if (startsWith(trimmed, "$$")) {
            std::string math_body;
            if (trimmed.size() > 4 && trimmed.substr(trimmed.size() - 2) == "$$") {
                math_body = trimAscii(trimmed.substr(2, trimmed.size() - 4));
                ++i;
            } else {
                const std::string rest_first = trimAscii(trimmed.substr(2));
                if (!rest_first.empty()) math_body = rest_first;
                ++i;
                while (i < lines.size()) {
                    const std::string mt = trimAscii(lines[i]);
                    if (mt == "$$") {
                        ++i;
                        break;
                    }
                    if (mt.size() >= 2 && mt.substr(mt.size() - 2) == "$$") {
                        const std::string before = trimAscii(mt.substr(0, mt.size() - 2));
                        if (!before.empty()) {
                            if (!math_body.empty()) math_body.push_back('\n');
                            math_body += before;
                        }
                        ++i;
                        break;
                    }
                    if (!math_body.empty()) math_body.push_back('\n');
                    math_body += lines[i];
                    ++i;
                }
            }
            MarkdownBlock blk;
            blk.kind = MarkdownBlockKind::MathBlock;
            blk.text = prettifyLatexMath(math_body);
            blocks.push_back(std::move(blk));
            continue;
        }

        // 3. ATX Heading
        int h_level = 1;
        std::string h_text;
        if (parseAtxHeadingLine(trimmed, h_level, h_text)) {
            MarkdownBlock blk;
            blk.kind = MarkdownBlockKind::Heading;
            blk.heading_level = h_level;
            blk.text = std::move(h_text);
            blocks.push_back(std::move(blk));
            ++i;
            continue;
        }

        // 4. Horizontal Rule
        if (isHorizontalRuleLine(trimmed)) {
            MarkdownBlock blk;
            blk.kind = MarkdownBlockKind::HorizontalRule;
            blocks.push_back(std::move(blk));
            ++i;
            continue;
        }

        // 5. Standalone Image
        std::string img_url, img_alt;
        if (parseStandaloneImageLine(trimmed, img_url, img_alt)) {
            MarkdownBlock blk;
            blk.kind = MarkdownBlockKind::Image;
            blk.image_url = std::move(img_url);
            blk.text = std::move(img_alt);
            blocks.push_back(std::move(blk));
            ++i;
            continue;
        }

        // 6. Blockquote / Obsidian Callout
        if (startsWith(trimmed, ">")) {
            std::vector<std::string> q_lines;
            while (i < lines.size()) {
                const std::string qt = trimAscii(lines[i]);
                if (qt.empty()) break;
                if (startsWith(qt, ">")) {
                    size_t off = 1;
                    if (off < qt.size() && qt[off] == ' ') ++off;
                    q_lines.push_back(qt.substr(off));
                    ++i;
                } else {
                    break;
                }
            }
            std::string c_type, c_title;
            bool c_foldable = false, c_collapsed = false;
            if (!q_lines.empty() &&
                parseCalloutHeader(q_lines.front(), c_type, c_title, c_foldable, c_collapsed)) {
                std::string inner_md;
                for (size_t qi = 1; qi < q_lines.size(); ++qi) {
                    if (qi > 1) inner_md.push_back('\n');
                    inner_md += q_lines[qi];
                }
                MarkdownBlock blk;
                blk.kind = MarkdownBlockKind::Callout;
                blk.callout_type = std::move(c_type);
                blk.callout_title = std::move(c_title);
                blk.callout_foldable = c_foldable;
                blk.callout_default_collapsed = c_collapsed;
                blk.children = parseMarkdownBlocks(inner_md);
                blocks.push_back(std::move(blk));
            } else {
                std::string inner_md;
                for (size_t qi = 0; qi < q_lines.size(); ++qi) {
                    if (qi > 0) inner_md.push_back('\n');
                    inner_md += q_lines[qi];
                }
                MarkdownBlock blk;
                blk.kind = MarkdownBlockKind::Blockquote;
                blk.children = parseMarkdownBlocks(inner_md);
                blocks.push_back(std::move(blk));
            }
            continue;
        }

        // 7. GFM Pipe Table
        if (i + 1 < lines.size() && trimmed.find('|') != std::string::npos) {
            auto headers = splitPipeRow(trimmed);
            std::vector<MarkdownTableAlign> aligns;
            if (!headers.empty() && parseTableDelimiterRow(lines[i + 1], headers.size(), aligns)) {
                MarkdownBlock blk;
                blk.kind = MarkdownBlockKind::Table;
                blk.table_headers = std::move(headers);
                blk.table_aligns = std::move(aligns);
                i += 2;
                while (i < lines.size()) {
                    const std::string rt = trimAscii(lines[i]);
                    if (rt.empty() || rt.find('|') == std::string::npos) break;
                    if (startsWith(rt, "#") || startsWith(rt, ">") || isHorizontalRuleLine(rt)) break;
                    auto row = splitPipeRow(rt);
                    if (row.size() < blk.table_headers.size()) {
                        row.resize(blk.table_headers.size());
                    } else if (row.size() > blk.table_headers.size()) {
                        row.resize(blk.table_headers.size());
                    }
                    blk.table_rows.push_back(std::move(row));
                    ++i;
                }
                blocks.push_back(std::move(blk));
                continue;
            }
        }

        // 8. Lists (unordered, ordered, task, nested, and loose)
        int li_indent = 0;
        bool li_ordered = false;
        int li_num = 1;
        int li_task = -1;
        std::string li_rest;
        if (parseListMarker(raw, li_indent, li_ordered, li_num, li_task, li_rest)) {
            MarkdownBlock blk;
            blk.kind = MarkdownBlockKind::List;

            while (i < lines.size()) {
                int cur_indent = 0;
                bool cur_ordered = false;
                int cur_num = 1;
                int cur_task = -1;
                std::string cur_rest;
                if (!parseListMarker(lines[i], cur_indent, cur_ordered, cur_num, cur_task, cur_rest)) {
                    break;
                }

                std::string first_line = lines[i];
                const bool first_hard_break = lineHasTrailingHardBreak(first_line);
                if (first_hard_break) {
                    parseListMarker(first_line, cur_indent, cur_ordered, cur_num, cur_task, cur_rest);
                }

                MarkdownListItem item;
                item.indent_level = cur_indent;
                item.ordered = cur_ordered;
                item.number = cur_num;
                item.task_state = cur_task;
                item.text = std::move(cur_rest);
                bool prev_hard_break = first_hard_break;
                ++i;

                // Continuation lines belonging to this list item
                while (i < lines.size()) {
                    if (trimAscii(lines[i]).empty()) break;
                    size_t non_sp = 0;
                    const size_t cols = countLeadingIndentCols(lines[i], non_sp);
                    int next_ind = 0, next_num = 1, next_task = -1;
                    bool next_ord = false;
                    std::string next_rest;
                    if (parseListMarker(lines[i], next_ind, next_ord, next_num, next_task, next_rest)) {
                        break;
                    }
                    if (cols >= 2) {
                        std::string cont = lines[i];
                        const bool hb = lineHasTrailingHardBreak(cont);
                        // Indented continuation line inside a list item (e.g. example sentence
                        // on next line): keep on a new line for readability!
                        joinSoftLine(item.text, trimAscii(cont), true);
                        prev_hard_break = hb;
                        ++i;
                        continue;
                    }
                    if (!isBlockStarterAt(lines, i)) {
                        std::string cont = lines[i];
                        const bool hb = lineHasTrailingHardBreak(cont);
                        joinSoftLine(item.text, trimAscii(cont), prev_hard_break);
                        prev_hard_break = hb;
                        ++i;
                        continue;
                    }
                    break;
                }

                blk.list_items.push_back(std::move(item));

                // Loose list check: allow a single blank line before the next list item
                if (i < lines.size() && trimAscii(lines[i]).empty()) {
                    size_t peek = i;
                    while (peek < lines.size() && trimAscii(lines[peek]).empty()) ++peek;
                    int next_ind = 0, next_num = 1, next_task = -1;
                    bool next_ord = false;
                    std::string next_rest;
                    if (peek < lines.size() && peek - i <= 2 &&
                        parseListMarker(lines[peek], next_ind, next_ord, next_num, next_task, next_rest) &&
                        next_ord == blk.list_items.front().ordered) {
                        i = peek;
                        continue;
                    }
                    break;
                }
            }

            blocks.push_back(std::move(blk));
            continue;
        }

        // 9. Paragraph (with Setext '=' H1 check)
        std::string para;
        bool prev_hard = false;
        int setext_level = 0;
        while (i < lines.size()) {
            const std::string pt = trimAscii(lines[i]);
            if (pt.empty()) break;
            if (!para.empty()) {
                // Setext '===' underline -> H1
                bool all_eq = pt.size() >= 3;
                for (char ec : pt) {
                    if (ec != '=') {
                        all_eq = false;
                        break;
                    }
                }
                if (all_eq) {
                    setext_level = 1;
                    ++i;
                    break;
                }
                if (isBlockStarterAt(lines, i)) break;
            }
            std::string cur_line = lines[i];
            const bool hb = lineHasTrailingHardBreak(cur_line);
            joinSoftLine(para, trimAscii(cur_line), prev_hard);
            prev_hard = hb;
            ++i;
        }
        if (!para.empty()) {
            MarkdownBlock blk;
            if (setext_level > 0) {
                blk.kind = MarkdownBlockKind::Heading;
                blk.heading_level = setext_level;
            } else {
                blk.kind = MarkdownBlockKind::Paragraph;
            }
            blk.text = std::move(para);
            blocks.push_back(std::move(blk));
        }
    }

    return blocks;
}

std::string stripMarkdownToPlainText(const std::string& markdown, bool single_line) {
    if (markdown.empty()) return "";
    const auto blocks = parseMarkdownBlocks(markdown);
    std::ostringstream oss;
    bool first_block = true;

    std::function<void(const MarkdownBlock&)> emitBlock = [&](const MarkdownBlock& blk) {
        auto emitInlines = [&](const std::string& md) {
            for (const auto& sp : parseMarkdownInlines(md)) {
                oss << sp.text;
            }
        };
        auto sep = [&]() {
            if (!first_block) {
                oss << (single_line ? " · " : "\n");
            }
            first_block = false;
        };

        switch (blk.kind) {
            case MarkdownBlockKind::Heading:
            case MarkdownBlockKind::Paragraph:
                sep();
                emitInlines(blk.text);
                break;
            case MarkdownBlockKind::CodeBlock:
            case MarkdownBlockKind::MathBlock:
                sep();
                oss << blk.text;
                break;
            case MarkdownBlockKind::List:
                for (const auto& item : blk.list_items) {
                    sep();
                    if (!single_line) {
                        if (item.ordered) oss << item.number << ". ";
                        else oss << "• ";
                    }
                    emitInlines(item.text);
                }
                break;
            case MarkdownBlockKind::Callout:
                if (!blk.callout_title.empty()) {
                    sep();
                    emitInlines(blk.callout_title);
                }
                for (const auto& child : blk.children) emitBlock(child);
                break;
            case MarkdownBlockKind::Blockquote:
                for (const auto& child : blk.children) emitBlock(child);
                break;
            case MarkdownBlockKind::Table:
                for (const auto& row : blk.table_rows) {
                    sep();
                    for (size_t c = 0; c < row.size(); ++c) {
                        if (c > 0) oss << " | ";
                        emitInlines(row[c]);
                    }
                }
                break;
            case MarkdownBlockKind::Image:
                if (!blk.text.empty()) {
                    sep();
                    oss << blk.text;
                }
                break;
            case MarkdownBlockKind::HorizontalRule:
                break;
        }
    };

    for (const auto& blk : blocks) {
        emitBlock(blk);
    }

    std::string res = oss.str();
    if (single_line) {
        for (char& c : res) {
            if (c == '\n' || c == '\r' || c == '\t') c = ' ';
        }
        std::string collapsed;
        collapsed.reserve(res.size());
        bool prev_sp = false;
        for (char c : res) {
            if (c == ' ') {
                if (!prev_sp) collapsed.push_back(' ');
                prev_sp = true;
            } else {
                collapsed.push_back(c);
                prev_sp = false;
            }
        }
        return trimAscii(collapsed);
    }
    return trimAscii(res);
}

std::string sanitizeMarkdownExcerpt(const std::string& markdown, size_t max_chars) {
    if (markdown.empty()) return "";
    const auto blocks = parseMarkdownBlocks(markdown);
    std::string out;

    auto isPureWikilinkBar = [](const std::string& md) {
        const auto spans = parseMarkdownInlines(md);
        if (spans.empty()) return true;
        for (const auto& sp : spans) {
            if (!startsWith(sp.link_url, "wikilink:")) {
                const std::string t = trimAscii(sp.text);
                if (!t.empty() && t != "·" && t != "|" && t != "/") {
                    return false;
                }
            }
        }
        return true;
    };

    std::function<void(const MarkdownBlock&)> collect = [&](const MarkdownBlock& blk) {
        if (!out.empty() && out.size() >= max_chars * 4) return;
        switch (blk.kind) {
            case MarkdownBlockKind::Heading:
                // Skip top-level H1 in excerpts (already shown as card title)
                if (blk.heading_level > 1 && out.empty()) {
                    out = blk.text;
                }
                break;
            case MarkdownBlockKind::Paragraph:
                if (isPureWikilinkBar(blk.text)) break;
                if (!out.empty()) out += " · ";
                out += blk.text;
                break;
            case MarkdownBlockKind::List:
                for (const auto& it : blk.list_items) {
                    if (!out.empty()) out += " · ";
                    out += it.text;
                    if (out.size() >= max_chars * 4) break;
                }
                break;
            case MarkdownBlockKind::Blockquote:
            case MarkdownBlockKind::Callout:
                for (const auto& ch : blk.children) collect(ch);
                break;
            case MarkdownBlockKind::MathBlock:
                if (!out.empty()) out += " ";
                out += blk.text;
                break;
            default:
                break;
        }
    };

    for (const auto& b : blocks) {
        collect(b);
        if (out.size() >= max_chars * 4) break;
    }
    if (out.empty()) {
        out = stripMarkdownToPlainText(markdown, true);
    }

    // Flatten newlines into spaces without breaking inline *italic* / **bold**
    for (char& c : out) {
        if (c == '\n' || c == '\r' || c == '\t') c = ' ';
    }
    return trimAscii(out);
}

// ============================================================================
// MarkdownRichTextView (NanoVG Inline Span Layout & Renderer)
// ============================================================================

struct MarkdownRichTextView::Impl {
    struct DrawRun {
        std::string text;
        std::string link_url;
        float x = 0.0f;
        float width = 0.0f;
        float text_x = 0.0f;
        bool bold = false;
        bool italic = false;
        bool code = false;
        bool strikethrough = false;
        bool highlight = false;
    };

    struct LayoutLine {
        std::vector<DrawRun> runs;
        float width = 0.0f;
    };

    struct LinkHitBox {
        float x = 0.0f;
        float y = 0.0f;
        float w = 0.0f;
        float h = 0.0f;
        std::string url;
        std::string label;
    };

    struct WrapAtom {
        std::string text;
        size_t span_idx = 0;
        bool is_newline = false;
        bool is_whitespace = false;
        bool pad_left = false;
        bool pad_right = false;
    };

    std::vector<MarkdownInlineSpan> spans;
    std::vector<WrapAtom> atoms;
    std::vector<LayoutLine> lines;
    std::vector<LinkHitBox> link_boxes;

    bool has_links = false;
    bool layout_dirty = true;
    float cached_wrap_width = -1.0f;
    float cached_max_line_width = 0.0f;
    float cached_total_height = 0.0f;
    float cached_ascender = 13.0f;
    float cached_line_height_px = 24.0f;
    float last_measured_width = -1.0f;
    int relayout_guard = 0;

    void rebuildSpans(const std::string& raw, const MarkdownStyle& style) {
        spans = parseMarkdownInlines(raw);
        for (auto& sp : spans) {
            if (style.bold_base) sp.bold = true;
            if (style.italic_base) sp.italic = true;
        }
        has_links = false;
        for (const auto& sp : spans) {
            if (!sp.link_url.empty()) {
                has_links = true;
                break;
            }
        }
        buildAtoms();
        layout_dirty = true;
        cached_wrap_width = -1.0f;
    }

    void buildAtoms() {
        atoms.clear();
        for (size_t si = 0; si < spans.size(); ++si) {
            const auto& sp = spans[si];
            const std::string& s = sp.text;
            const bool boxed = sp.code || sp.highlight;
            const size_t first_atom_idx = atoms.size();

            size_t i = 0;
            while (i < s.size()) {
                if (s[i] == '\n') {
                    atoms.push_back({"\n", si, true, false, false, false});
                    ++i;
                    continue;
                }
                if (s[i] == ' ' || s[i] == '\t') {
                    size_t j = i + 1;
                    while (j < s.size() && (s[j] == ' ' || s[j] == '\t')) ++j;
                    std::string ws(j - i, ' ');
                    atoms.push_back({std::move(ws), si, false, true, false, false});
                    i = j;
                    continue;
                }

                uint32_t cp = 0;
                const size_t adv = decodeUtf8(s, i, cp);
                if (isCjkOrWideCodepoint(cp)) {
                    std::string tok = s.substr(i, adv);
                    i += adv;
                    // Attach trailing line-end punctuation (kinsoku / 避头点) to this CJK glyph
                    while (i < s.size() && s[i] != '\n' && s[i] != ' ' && s[i] != '\t') {
                        uint32_t next_cp = 0;
                        const size_t nadv = decodeUtf8(s, i, next_cp);
                        if (!isNoLineStartPunct(next_cp)) break;
                        tok.append(s, i, nadv);
                        i += nadv;
                    }
                    atoms.push_back({std::move(tok), si, false, false, false, false});
                } else {
                    // Contiguous Latin/ASCII/symbol word up to space, newline, or CJK
                    size_t j = i + adv;
                    while (j < s.size() && s[j] != '\n' && s[j] != ' ' && s[j] != '\t') {
                        uint32_t ncp = 0;
                        const size_t nadv = decodeUtf8(s, j, ncp);
                        if (isCjkOrWideCodepoint(ncp)) break;
                        j += nadv;
                        // Allow breaking long URLs after '/' or '-' or '_'
                        if (!sp.link_url.empty() && j - i >= 12 &&
                            (s[j - 1] == '/' || s[j - 1] == '-' || s[j - 1] == '_' ||
                             s[j - 1] == '&' || s[j - 1] == '?')) {
                            break;
                        }
                    }
                    atoms.push_back({s.substr(i, j - i), si, false, false, false, false});
                    i = j;
                }
            }

            if (boxed && atoms.size() > first_atom_idx) {
                for (size_t k = first_atom_idx; k < atoms.size(); ++k) {
                    if (!atoms[k].is_newline) {
                        atoms[k].pad_left = true;
                        break;
                    }
                }
                for (size_t k = atoms.size(); k > first_atom_idx; --k) {
                    if (!atoms[k - 1].is_newline) {
                        atoms[k - 1].pad_right = true;
                        break;
                    }
                }
            }
        }
    }

    float measureTextSlice(NVGcontext* vg,
                           int font_id,
                           float font_px,
                           const MarkdownInlineSpan& sp,
                           const std::string& txt) const {
        if (txt.empty()) return 0.0f;
        if (!vg || font_id < 0) {
            size_t cjk = 0, ascii = 0;
            size_t p = 0;
            while (p < txt.size()) {
                uint32_t cp = 0;
                p += decodeUtf8(txt, p, cp);
                if (isCjkOrWideCodepoint(cp)) ++cjk;
                else ++ascii;
            }
            const float sz = sp.code ? font_px * 0.91f : font_px;
            return static_cast<float>(cjk) * sz + static_cast<float>(ascii) * sz * 0.54f;
        }
        const float sz = sp.code ? font_px * 0.91f : font_px;
        nvgFontFaceId(vg, font_id);
        nvgFontSize(vg, sz);
        nvgTextLetterSpacing(vg, sp.bold ? 0.25f : 0.0f);
        nvgTextAlign(vg, NVG_ALIGN_LEFT | NVG_ALIGN_BASELINE);
        float advance = nvgTextBounds(vg, 0.0f, 0.0f, txt.c_str(), nullptr, nullptr);
        if (sp.bold) advance += 0.65f;
        if (sp.italic) advance += 0.5f;
        nvgTextLetterSpacing(vg, 0.0f);
        return std::max(0.0f, advance);
    }

    void ensureLayout(NVGcontext* vg, float wrap_width, const MarkdownStyle& style) {
        const float font_px = style.font_size * brls::Label::FONT_SCALE;
        const float line_h = std::max(font_px * 1.15f, font_px * style.line_height);
        const float effective_wrap =
            (std::isnan(wrap_width) || wrap_width <= 0.0f) ? 1.0e7f : std::max(24.0f, wrap_width);

        if (!layout_dirty && std::abs(effective_wrap - cached_wrap_width) < 0.5f &&
            std::abs(line_h - cached_line_height_px) < 0.1f) {
            return;
        }

        cached_wrap_width = effective_wrap;
        cached_line_height_px = line_h;
        lines.clear();
        link_boxes.clear();
        cached_max_line_width = 0.0f;

        if (spans.empty() || atoms.empty()) {
            cached_total_height = 0.0f;
            layout_dirty = false;
            return;
        }

        int font_id = -1;
        if (vg) {
            font_id = brls::Application::getDefaultFont();
            nvgFontFaceId(vg, font_id);
            nvgFontSize(vg, font_px);
            nvgTextAlign(vg, NVG_ALIGN_LEFT | NVG_ALIGN_BASELINE);
            float asc = 0.0f, desc = 0.0f, lh = 0.0f;
            nvgTextMetrics(vg, &asc, &desc, &lh);
            cached_ascender = (asc > 1.0f) ? asc : (font_px * 0.78f);
        } else {
            cached_ascender = font_px * 0.78f;
        }

        constexpr float kPillPadX = 5.0f;
        constexpr float kPillMarginX = 1.5f;

        LayoutLine cur_line;
        float cursor_x = 0.0f;

        auto flushLine = [&]() {
            cur_line.width = cursor_x;
            cached_max_line_width = std::max(cached_max_line_width, cur_line.width);
            lines.push_back(std::move(cur_line));
            cur_line = LayoutLine{};
            cursor_x = 0.0f;
        };

        auto appendPieceToLine = [&](size_t span_idx,
                                     const std::string& piece,
                                     float text_w,
                                     bool pad_l,
                                     bool pad_r) {
            const auto& sp = spans[span_idx];
            const bool boxed = sp.code || sp.highlight;
            const float left_extra = (boxed && pad_l) ? (kPillPadX + kPillMarginX) : 0.0f;
            const float right_extra = (boxed && pad_r) ? (kPillPadX + kPillMarginX) : 0.0f;

            if (!cur_line.runs.empty() && !pad_l) {
                auto& prev = cur_line.runs.back();
                if (prev.bold == sp.bold && prev.italic == sp.italic &&
                    prev.code == sp.code && prev.strikethrough == sp.strikethrough &&
                    prev.highlight == sp.highlight && prev.link_url == sp.link_url) {
                    prev.text += piece;
                    const float remeasured = measureTextSlice(vg, font_id, font_px, sp, prev.text);
                    const float prev_left_pad = prev.text_x - prev.x;
                    prev.width = prev_left_pad + remeasured + (boxed && pad_r ? kPillPadX : 0.0f);
                    cursor_x = prev.x + prev.width + (boxed && pad_r ? kPillMarginX : 0.0f);
                    return;
                }
            }

            DrawRun run;
            run.text = piece;
            run.link_url = sp.link_url;
            run.bold = sp.bold;
            run.italic = sp.italic;
            run.code = sp.code;
            run.strikethrough = sp.strikethrough;
            run.highlight = sp.highlight;
            run.x = cursor_x + (boxed && pad_l ? kPillMarginX : 0.0f);
            run.text_x = cursor_x + left_extra;
            run.width = (boxed && pad_l ? kPillPadX : 0.0f) + text_w +
                        (boxed && pad_r ? kPillPadX : 0.0f);
            cursor_x = run.x + run.width + (boxed && pad_r ? kPillMarginX : 0.0f);
            cur_line.runs.push_back(std::move(run));
            (void)right_extra;
        };

        for (const auto& atom : atoms) {
            if (atom.is_newline) {
                flushLine();
                continue;
            }
            if (atom.is_whitespace && cur_line.runs.empty()) {
                continue;
            }

            const auto& sp = spans[atom.span_idx];
            const bool boxed = sp.code || sp.highlight;
            const float extra_w =
                (boxed && atom.pad_left ? (kPillPadX + kPillMarginX) : 0.0f) +
                (boxed && atom.pad_right ? (kPillPadX + kPillMarginX) : 0.0f);
            const float atom_text_w = measureTextSlice(vg, font_id, font_px, sp, atom.text);
            const float total_atom_w = atom_text_w + extra_w;

            if (!cur_line.runs.empty() && cursor_x + total_atom_w > effective_wrap + 0.5f) {
                if (atom.is_whitespace) {
                    flushLine();
                    continue;
                }
                flushLine();
            }

            // If a single long token (e.g. URL or unspaced string) exceeds effective_wrap,
            // split it along UTF-8 codepoint boundaries in O(N) time instead of O(N^2).
            if (cur_line.runs.empty() && total_atom_w > effective_wrap + 0.5f &&
                effective_wrap < 1.0e6f) {
                const float max_chunk_w = std::max(8.0f, effective_wrap - 8.0f);
                const float sz = sp.code ? font_px * 0.91f : font_px;

                std::vector<size_t> cp_offsets;
                cp_offsets.reserve(atom.text.size() + 1);
                for (size_t p = 0; p < atom.text.size();) {
                    cp_offsets.push_back(p);
                    uint32_t cp = 0;
                    p += decodeUtf8(atom.text, p, cp);
                }
                cp_offsets.push_back(atom.text.size());
                const size_t cp_count = cp_offsets.size() - 1;

                bool handled_by_glyphs = false;
                if (vg && font_id >= 0 && cp_count > 0) {
                    nvgFontFaceId(vg, font_id);
                    nvgFontSize(vg, sz);
                    nvgTextLetterSpacing(vg, sp.bold ? 0.25f : 0.0f);
                    nvgTextAlign(vg, NVG_ALIGN_LEFT | NVG_ALIGN_BASELINE);
                    std::vector<NVGglyphPosition> glyphs(cp_count);
                    const int nglyphs = nvgTextGlyphPositions(
                        vg,
                        0.0f,
                        0.0f,
                        atom.text.data(),
                        atom.text.data() + atom.text.size(),
                        glyphs.data(),
                        static_cast<int>(cp_count));
                    nvgTextLetterSpacing(vg, 0.0f);

                    if (nglyphs > 0) {
                        handled_by_glyphs = true;
                        const float style_extra =
                            (sp.bold ? 0.65f : 0.0f) + (sp.italic ? 0.5f : 0.0f);
                        int g_start = 0;
                        size_t byte_pos = 0;
                        bool first_chunk = true;
                        while (byte_pos < atom.text.size() && g_start < nglyphs) {
                            const float origin_x = glyphs[g_start].x;
                            int g_end = g_start + 1;
                            while (g_end < nglyphs &&
                                   (glyphs[g_end].maxx - origin_x + style_extra) <= max_chunk_w) {
                                ++g_end;
                            }
                            size_t chunk_end = (g_end < nglyphs)
                                                   ? static_cast<size_t>(glyphs[g_end].str -
                                                                         atom.text.data())
                                                   : atom.text.size();
                            if (chunk_end <= byte_pos) {
                                uint32_t dummy = 0;
                                chunk_end = byte_pos + decodeUtf8(atom.text, byte_pos, dummy);
                            }
                            const bool last_chunk = (chunk_end >= atom.text.size());
                            std::string chunk = atom.text.substr(byte_pos, chunk_end - byte_pos);
                            const float cw = measureTextSlice(vg, font_id, font_px, sp, chunk);
                            appendPieceToLine(atom.span_idx,
                                              chunk,
                                              cw,
                                              first_chunk && atom.pad_left,
                                              last_chunk && atom.pad_right);
                            first_chunk = false;
                            byte_pos = chunk_end;
                            g_start = g_end;
                            if (!last_chunk) {
                                flushLine();
                            }
                        }
                    }
                }

                if (!handled_by_glyphs) {
                    size_t idx = 0;
                    bool first_chunk = true;
                    while (idx < cp_count) {
                        size_t end_idx = idx;
                        float cw = 0.0f;
                        while (end_idx < cp_count) {
                            uint32_t cp = 0;
                            decodeUtf8(atom.text, cp_offsets[end_idx], cp);
                            const float w_cp = isCjkOrWideCodepoint(cp) ? sz : (sz * 0.54f);
                            if (end_idx > idx && cw + w_cp > max_chunk_w) {
                                break;
                            }
                            cw += w_cp;
                            ++end_idx;
                        }
                        if (end_idx == idx) {
                            ++end_idx;
                        }
                        const size_t b0 = cp_offsets[idx];
                        const size_t b1 = cp_offsets[end_idx];
                        const bool last_chunk = (end_idx >= cp_count);
                        appendPieceToLine(atom.span_idx,
                                          atom.text.substr(b0, b1 - b0),
                                          cw,
                                          first_chunk && atom.pad_left,
                                          last_chunk && atom.pad_right);
                        first_chunk = false;
                        idx = end_idx;
                        if (!last_chunk) {
                            flushLine();
                        }
                    }
                }
                continue;
            }

            appendPieceToLine(atom.span_idx, atom.text, atom_text_w, atom.pad_left, atom.pad_right);
        }

        if (!cur_line.runs.empty() || lines.empty()) {
            flushLine();
        }

        // Clamp to style.max_lines with clean UTF-8 ellipsis if configured
        if (style.max_lines > 0 && static_cast<int>(lines.size()) > style.max_lines) {
            lines.resize(static_cast<size_t>(style.max_lines));
            auto& last = lines.back();
            MarkdownInlineSpan ell_span;
            ell_span.bold = style.bold_base;
            const float ell_w = measureTextSlice(vg, font_id, font_px, ell_span, kEllipsis);
            while (!last.runs.empty() && last.width + ell_w > effective_wrap &&
                   effective_wrap < 1.0e6f) {
                auto& r = last.runs.back();
                if (r.text.empty()) {
                    last.runs.pop_back();
                    last.width = last.runs.empty() ? 0.0f : (last.runs.back().x + last.runs.back().width);
                    continue;
                }
                // Pop one UTF-8 codepoint from r.text
                size_t cut = r.text.size() - 1;
                while (cut > 0 && (static_cast<unsigned char>(r.text[cut]) & 0xC0u) == 0x80u) {
                    --cut;
                }
                r.text.erase(cut);
                if (r.text.empty()) {
                    last.runs.pop_back();
                } else {
                    MarkdownInlineSpan tmp;
                    tmp.bold = r.bold;
                    tmp.italic = r.italic;
                    tmp.code = r.code;
                    r.width = (r.text_x - r.x) + measureTextSlice(vg, font_id, font_px, tmp, r.text);
                }
                last.width = last.runs.empty() ? 0.0f : (last.runs.back().x + last.runs.back().width);
            }
            DrawRun ell_run;
            ell_run.text = kEllipsis;
            ell_run.x = last.width;
            ell_run.text_x = last.width;
            ell_run.width = ell_w;
            last.runs.push_back(std::move(ell_run));
            last.width += ell_w;
            cached_max_line_width = std::max(cached_max_line_width, last.width);
        }

        if (has_links) {
            for (size_t li = 0; li < lines.size(); ++li) {
                const float box_y = static_cast<float>(li) * line_h;
                for (const auto& run : lines[li].runs) {
                    if (!run.text.empty() && !run.link_url.empty()) {
                        link_boxes.push_back({
                            run.x,
                            box_y,
                            run.width,
                            line_h,
                            run.link_url,
                            run.text,
                        });
                    }
                }
            }
        }

        cached_total_height = static_cast<float>(lines.size()) * line_h;
        layout_dirty = false;
    }
};

namespace {

NVGcontext* safeGetNVGContext() {
    auto* plat = brls::Application::getPlatform();
    if (!plat || !plat->getVideoContext()) return nullptr;
    return plat->getVideoContext()->getNVGContext();
}

YGSize markdownRichTextMeasureFunc(YGNodeRef node,
                                   float width,
                                   YGMeasureMode widthMode,
                                   float height,
                                   YGMeasureMode heightMode) {
    auto* view = static_cast<MarkdownRichTextView*>(YGNodeGetContext(node));
    YGSize size{0.0f, 0.0f};
    if (!view || !view->impl()) return size;

    auto* impl = view->impl();
    const auto& style = view->markdownStyle();

    if (impl->spans.empty()) {
        if (widthMode == YGMeasureModeExactly) size.width = width;
        if (heightMode == YGMeasureModeExactly) size.height = height;
        return size;
    }

    if (widthMode == YGMeasureModeAtMost && (width == 0.0f || std::isnan(width))) {
        widthMode = YGMeasureModeUndefined;
        width = NAN;
    }

    const float avail_w =
        (widthMode == YGMeasureModeUndefined || std::isnan(width)) ? 1.0e7f : width;

    NVGcontext* vg = safeGetNVGContext();
    impl->ensureLayout(vg, avail_w, style);
    impl->last_measured_width = avail_w;

    if (widthMode == YGMeasureModeExactly) {
        size.width = width;
    } else if (widthMode == YGMeasureModeAtMost) {
        size.width = std::min(impl->cached_max_line_width + 1.0f, width);
    } else {
        size.width = impl->cached_max_line_width + 1.0f;
    }

    if (heightMode == YGMeasureModeExactly) {
        size.height = height;
    } else if (heightMode == YGMeasureModeAtMost) {
        size.height = std::min(impl->cached_total_height, height);
    } else {
        size.height = impl->cached_total_height;
    }

    return size;
}

void defaultOpenMarkdownLink(const std::string& url, const std::string& /*label*/) {
    if (url.empty()) return;
    if (startsWith(url, "http://") || startsWith(url, "https://")) {
        SDL_OpenURL(url.c_str());
    } else if (startsWith(url, "wikilink:")) {
        brls::Application::notify("关联笔记: " + url.substr(9));
    }
}

NVGcolor boostBoldColor(NVGcolor base, bool use_high_contrast) {
    if (use_high_contrast) {
        const NVGcolor text_col = ui::color("brls/text");
        base.r = base.r * 0.25f + text_col.r * 0.75f;
        base.g = base.g * 0.25f + text_col.g * 0.75f;
        base.b = base.b * 0.25f + text_col.b * 0.75f;
    }
    const float lum = 0.299f * base.r + 0.587f * base.g + 0.114f * base.b;
    if (lum > 0.5f) {
        base.r = std::min(1.0f, base.r * 1.06f);
        base.g = std::min(1.0f, base.g * 1.06f);
        base.b = std::min(1.0f, base.b * 1.06f);
    } else {
        base.r = std::max(0.0f, base.r * 0.72f);
        base.g = std::max(0.0f, base.g * 0.72f);
        base.b = std::max(0.0f, base.b * 0.72f);
    }
    return base;
}

}  // namespace

MarkdownRichTextView::MarkdownRichTextView()
    : impl_(std::make_unique<Impl>()) {
    YGNodeSetMeasureFunc(this->ygNode, markdownRichTextMeasureFunc);
    YGNodeStyleSetMaxWidthPercent(this->ygNode, 100);

    addGestureRecognizer(new brls::TapGestureRecognizer(
        [this](brls::TapGestureStatus status, brls::Sound*) {
            if (!impl_ || !impl_->has_links || !style_.interactive_links) return;
            if (status.state != brls::GestureState::END) return;
            const float lx = status.position.x - getX();
            const float ly = status.position.y - getY();
            for (const auto& box : impl_->link_boxes) {
                if (lx >= box.x - 2.0f && lx <= box.x + box.w + 2.0f &&
                    ly >= box.y - 2.0f && ly <= box.y + box.h + 2.0f) {
                    if (style_.on_link_click) {
                        style_.on_link_click(box.url, box.label);
                    } else {
                        defaultOpenMarkdownLink(box.url, box.label);
                    }
                    return;
                }
            }
        }));
}

MarkdownRichTextView::MarkdownRichTextView(const std::string& markdown, MarkdownStyle style)
    : MarkdownRichTextView() {
    style_ = std::move(style);
    setMarkdown(markdown);
}

MarkdownRichTextView::~MarkdownRichTextView() = default;

void MarkdownRichTextView::setMarkdown(const std::string& markdown) {
    raw_markdown_ = markdown;
    impl_->relayout_guard = 0;
    impl_->rebuildSpans(raw_markdown_, style_);
    invalidate();
}

void MarkdownRichTextView::setStyle(MarkdownStyle style) {
    style_ = std::move(style);
    impl_->relayout_guard = 0;
    impl_->rebuildSpans(raw_markdown_, style_);
    invalidate();
}

void MarkdownRichTextView::onLayout() {
    if (!impl_ || impl_->spans.empty()) return;
    const float w = getWidth();
    if (w > 1.0f && std::abs(w - impl_->cached_wrap_width) > 1.0f) {
        NVGcontext* vg = safeGetNVGContext();
        impl_->ensureLayout(vg, w, style_);
        if (std::abs(impl_->cached_total_height - getHeight()) > 1.0f &&
            impl_->relayout_guard < 2) {
            ++impl_->relayout_guard;
            YGNodeMarkDirty(this->ygNode);
        }
    }
}

brls::View* MarkdownRichTextView::hitTest(brls::Point point) {
    if (!impl_ || !impl_->has_links || !style_.interactive_links ||
        getAlpha() == 0.0f || getVisibility() != brls::Visibility::VISIBLE) {
        return nullptr;
    }
    if (!getFrame().pointInside(point)) {
        return nullptr;
    }
    const float lx = point.x - getX();
    const float ly = point.y - getY();
    for (const auto& box : impl_->link_boxes) {
        if (lx >= box.x - 2.0f && lx <= box.x + box.w + 2.0f &&
            ly >= box.y - 2.0f && ly <= box.y + box.h + 2.0f) {
            return this;
        }
    }
    return nullptr;
}

void MarkdownRichTextView::draw(NVGcontext* vg,
                                float x,
                                float y,
                                float width,
                                float /*height*/,
                                brls::Style /*style*/,
                                brls::FrameContext* /*ctx*/) {
    if (!vg || !impl_ || width <= 0.0f || impl_->spans.empty()) return;

    impl_->ensureLayout(vg, width, style_);
    const float line_h = impl_->cached_line_height_px;
    if (impl_->lines.empty() || line_h <= 0.0f) return;

    float clip_top = 0.0f;
    float clip_bottom = brls::Application::contentHeight > 0.0f
                            ? brls::Application::contentHeight
                            : 1.0e7f;
    for (brls::Box* p = getParent(); p != nullptr; p = p->getParent()) {
        float t = 0.0f, r = 0.0f, b = 0.0f, l = 0.0f;
        p->getCullingBounds(&t, &r, &b, &l);
        if (b > t) {
            clip_top = std::max(clip_top, t);
            clip_bottom = std::min(clip_bottom, b);
        }
    }
    if (clip_bottom <= clip_top) {
        return;
    }

    const int total_lines = static_cast<int>(impl_->lines.size());
    const int first_vis = std::clamp(
        static_cast<int>(std::floor((clip_top - 4.0f - y) / line_h)), 0, total_lines);
    const int last_vis = std::clamp(
        static_cast<int>(std::ceil((clip_bottom + 4.0f - y) / line_h)), 0, total_lines);
    if (first_vis >= last_vis) {
        return;
    }

    const int font_id = brls::Application::getDefaultFont();
    const float font_px = style_.font_size * brls::Label::FONT_SCALE;
    const float vertical_pad = std::max(0.0f, (line_h - font_px) * 0.5f);
    const float baseline_offset = vertical_pad + impl_->cached_ascender;
    const float bold_shift = std::clamp(font_px * 0.038f, 0.52f, 0.82f);

    const NVGcolor base_col = ui::color(style_.text_color_token);
    const NVGcolor link_col = ui::color(style_.link_color_token);
    const NVGcolor code_fg = ui::color(style_.code_fg_token);
    const NVGcolor code_bg = ui::color(style_.code_bg_token);
    const NVGcolor code_border = ui::color("desktop/line");
    const NVGcolor hl_bg = ui::color("status/warn_bg");

    for (size_t li = static_cast<size_t>(first_vis); li < static_cast<size_t>(last_vis); ++li) {
        const float line_top = y + static_cast<float>(li) * line_h;
        const float baseline_y = line_top + baseline_offset;

        for (const auto& run : impl_->lines[li].runs) {
            if (run.text.empty()) continue;

            const float pill_top = line_top + std::max(1.0f, vertical_pad * 0.55f);
            const float pill_h = std::max(font_px + 3.0f, line_h - vertical_pad * 1.1f);

            if (run.code) {
                nvgBeginPath(vg);
                nvgRoundedRect(vg, x + run.x, pill_top, run.width, pill_h, 4.5f);
                nvgFillColor(vg, a(code_bg));
                nvgFill(vg);
                nvgStrokeWidth(vg, 1.0f);
                nvgStrokeColor(vg, a(code_border));
                nvgStroke(vg);
            } else if (run.highlight) {
                nvgBeginPath(vg);
                nvgRoundedRect(vg, x + run.x, pill_top, run.width, pill_h, 4.0f);
                nvgFillColor(vg, a(hl_bg));
                nvgFill(vg);
            }

            NVGcolor fg = base_col;
            if (run.code) {
                fg = code_fg;
            } else if (!run.link_url.empty()) {
                fg = link_col;
            } else if (run.bold) {
                fg = boostBoldColor(base_col, !style_.bold_base);
            }

            const float draw_sz = run.code ? (font_px * 0.91f) : font_px;
            nvgFontFaceId(vg, font_id);
            nvgFontSize(vg, draw_sz);
            nvgFontQuality(vg, 1.0f);
            nvgTextLetterSpacing(vg, run.bold ? 0.25f : 0.0f);
            nvgFillColor(vg, a(fg));

            const float tx = x + run.text_x;
            if (run.italic) {
                nvgSave(vg);
                nvgTranslate(vg, tx, baseline_y);
                nvgSkewX(vg, -0.18f);
                nvgTextAlign(vg, NVG_ALIGN_LEFT | NVG_ALIGN_BASELINE);
                nvgText(vg, 0.0f, 0.0f, run.text.c_str(), nullptr);
                if (run.bold) {
                    nvgText(vg, bold_shift, 0.0f, run.text.c_str(), nullptr);
                }
                nvgRestore(vg);
            } else {
                nvgTextAlign(vg, NVG_ALIGN_LEFT | NVG_ALIGN_BASELINE);
                nvgText(vg, tx, baseline_y, run.text.c_str(), nullptr);
                if (run.bold) {
                    nvgText(vg, tx + bold_shift, baseline_y, run.text.c_str(), nullptr);
                    if (draw_sz >= 18.5f) {
                        nvgText(vg, tx + bold_shift * 0.5f, baseline_y - 0.2f, run.text.c_str(), nullptr);
                    }
                }
            }
            nvgTextLetterSpacing(vg, 0.0f);

            if (!run.link_url.empty()) {
                NVGcolor ul = fg;
                ul.a *= 0.52f;
                nvgBeginPath(vg);
                nvgMoveTo(vg, x + run.x, baseline_y + 2.5f);
                nvgLineTo(vg, x + run.x + run.width, baseline_y + 2.5f);
                nvgStrokeWidth(vg, 1.0f);
                nvgStrokeColor(vg, a(ul));
                nvgStroke(vg);
            }

            if (run.strikethrough) {
                const float sy = baseline_y - impl_->cached_ascender * 0.34f;
                nvgBeginPath(vg);
                nvgMoveTo(vg, x + run.x, sy);
                nvgLineTo(vg, x + run.x + run.width, sy);
                nvgStrokeWidth(vg, 1.25f);
                nvgStrokeColor(vg, a(fg));
                nvgStroke(vg);
            }
        }
    }
}

// ============================================================================
// MarkdownView (Composite Block-Level Markdown Widget)
// ============================================================================

namespace {

class AccentBlockBox final : public brls::Box {
public:
    AccentBlockBox(std::string bg_token, std::string bar_token, float padding = 12.0f)
        : brls::Box(brls::Axis::COLUMN),
          bg_token_(std::move(bg_token)),
          bar_token_(std::move(bar_token)) {
        setWidthPercentage(100);
        setPadding(padding, padding + 2.0f, padding, padding + 6.0f);
        setCornerRadius(8.0f);
        setBackgroundColor(ui::color(bg_token_));
        setBorderThickness(1.0f);
        setBorderColor(ui::color("desktop/line"));
    }

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style style,
              brls::FrameContext* ctx) override {
        brls::Box::draw(vg, x, y, width, height, style, ctx);
        if (vg && height > 6.0f) {
            nvgBeginPath(vg);
            nvgRoundedRect(vg, x + 1.0f, y + 4.0f, 3.5f, height - 8.0f, 1.75f);
            nvgFillColor(vg, a(ui::color(bar_token_)));
            nvgFill(vg);
        }
    }

private:
    std::string bg_token_;
    std::string bar_token_;
};

struct CalloutPalette {
    const char* bg_token;
    const char* fg_token;
    const char* badge;
};

CalloutPalette resolveCalloutPalette(const std::string& type) {
    if (type == "tip" || type == "hint" || type == "important" || type == "answer" ||
        type == "success" || type == "check" || type == "done") {
        return {"status/ok_bg", "status/ok_fg",
                type == "answer" ? "参考答案" : "提示"};
    }
    if (type == "warning" || type == "caution" || type == "attention" ||
        type == "question" || type == "help" || type == "faq") {
        return {"status/warn_bg", "status/warn_fg",
                (type == "question" || type == "faq") ? "问题" : "注意"};
    }
    if (type == "danger" || type == "error" || type == "bug" ||
        type == "failure" || type == "fail" || type == "missing") {
        return {"status/err_bg", "status/err_fg", "警示"};
    }
    if (type == "quote" || type == "cite" || type == "example") {
        return {"desktop/soft", "domain/text",
                type == "example" ? "示例" : "引用"};
    }
    return {"status/info_bg", "status/info_fg", "说明"};
}

brls::View* buildBlockView(const MarkdownBlock& blk,
                           const MarkdownStyle& style,
                           const std::shared_ptr<std::atomic<bool>>& alive);

void populateBlockChildren(brls::Box* container,
                           const std::vector<MarkdownBlock>& blocks,
                           const MarkdownStyle& style,
                           const std::shared_ptr<std::atomic<bool>>& alive) {
    for (size_t i = 0; i < blocks.size(); ++i) {
        brls::View* v = buildBlockView(blocks[i], style, alive);
        if (!v) continue;
        if (i > 0) {
            const float top_gap =
                (blocks[i].kind == MarkdownBlockKind::Heading) ? (style.block_gap * 1.6f)
                                                               : style.block_gap;
            v->setMarginTop(top_gap);
        }
        container->addView(v);
    }
}

brls::View* buildBlockView(const MarkdownBlock& blk,
                           const MarkdownStyle& style,
                           const std::shared_ptr<std::atomic<bool>>& alive) {
    switch (blk.kind) {
        case MarkdownBlockKind::Heading: {
            MarkdownStyle hs = style;
            hs.bold_base = true;
            hs.max_lines = 0;
            hs.line_height = 1.36f;
            switch (blk.heading_level) {
                case 1: hs.font_size = style.font_size * 1.30f; break;
                case 2: hs.font_size = style.font_size * 1.18f; break;
                case 3: hs.font_size = style.font_size * 1.07f; break;
                default: hs.font_size = style.font_size * 1.01f; break;
            }
            auto* wrap = new brls::Box(brls::Axis::COLUMN);
            wrap->setWidthPercentage(100);
            auto* hv = new MarkdownRichTextView(blk.text, hs);
            hv->setWidthPercentage(100);
            wrap->addView(hv);
            if (blk.heading_level <= 2) {
                auto* rule = new brls::Box(brls::Axis::ROW);
                rule->setWidthPercentage(100);
                rule->setHeight(1.0f);
                rule->setMarginTop(4.0f);
                rule->setBackgroundColor(ui::color("desktop/line"));
                wrap->addView(rule);
            }
            return wrap;
        }

        case MarkdownBlockKind::Paragraph: {
            auto* pv = new MarkdownRichTextView(blk.text, style);
            pv->setWidthPercentage(100);
            return pv;
        }

        case MarkdownBlockKind::List: {
            auto* list_box = new brls::Box(brls::Axis::COLUMN);
            list_box->setWidthPercentage(100);
            for (size_t idx = 0; idx < blk.list_items.size(); ++idx) {
                const auto& item = blk.list_items[idx];
                auto* row = new brls::Box(brls::Axis::ROW);
                row->setWidthPercentage(100);
                row->setAlignItems(brls::AlignItems::FLEX_START);
                if (idx > 0) {
                    row->setMarginTop(std::max(4.0f, style.block_gap * 0.6f));
                }
                const int depth = std::clamp(item.indent_level, 0, 5);
                if (depth > 0) {
                    row->setPaddingLeft(static_cast<float>(depth) * 18.0f);
                }

                std::string marker_str;
                std::string marker_color = "domain/text";
                if (item.task_state == 1) {
                    marker_str = "☑";
                    marker_color = "status/ok_fg";
                } else if (item.task_state == 0) {
                    marker_str = "☐";
                    marker_color = style.muted_color_token;
                } else if (item.ordered) {
                    marker_str = std::to_string(item.number) + ".";
                    marker_color = "domain/text";
                } else {
                    static const char* kBullets[] = {"•", "◦", "▪"};
                    marker_str = kBullets[depth % 3];
                    marker_color = "domain/text";
                }

                auto* marker_box = new brls::Box(brls::Axis::ROW);
                marker_box->setWidth(item.ordered ? 26.0f : 18.0f);
                marker_box->setShrink(0.0f);
                auto* marker_lbl = ui::makeLabel(
                    marker_str, style.font_size * 0.96f, marker_color, style.line_height);
                marker_lbl->setSingleLine(true);
                marker_box->addView(marker_lbl);
                row->addView(marker_box);

                MarkdownStyle item_style = style;
                item_style.max_lines = 0;
                auto* body_view = new MarkdownRichTextView(item.text, item_style);
                body_view->setGrow(1.0f);
                body_view->setShrink(1.0f);
                row->addView(body_view);

                list_box->addView(row);
            }
            return list_box;
        }

        case MarkdownBlockKind::Blockquote: {
            auto* qbox = new AccentBlockBox("desktop/soft", "domain/text", 11.0f);
            MarkdownStyle qs = style;
            qs.font_size = std::max(13.5f, style.font_size * 0.96f);
            qs.max_lines = 0;
            populateBlockChildren(qbox, blk.children, qs, alive);
            return qbox;
        }

        case MarkdownBlockKind::Callout: {
            const auto pal = resolveCalloutPalette(blk.callout_type);
            auto* cbox = new AccentBlockBox("desktop/soft", pal.fg_token, 12.0f);

            auto* hdr = new brls::Box(brls::Axis::ROW);
            hdr->setWidthPercentage(100);
            hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            hdr->setAlignItems(brls::AlignItems::CENTER);

            auto* left = new brls::Box(brls::Axis::ROW);
            left->setAlignItems(brls::AlignItems::CENTER);
            left->setGrow(1.0f);
            left->setShrink(1.0f);

            auto* pill = ui::makePill(pal.badge, pal.bg_token, pal.fg_token);
            pill->setMarginRight(8.0f);
            pill->setShrink(0.0f);
            left->addView(pill);

            MarkdownStyle ts = style;
            ts.font_size = std::max(14.0f, style.font_size * 0.96f);
            ts.bold_base = true;
            ts.text_color_token = pal.fg_token;
            ts.line_height = 1.35f;
            auto* title_v = new MarkdownRichTextView(blk.callout_title, ts);
            title_v->setGrow(1.0f);
            title_v->setShrink(1.0f);
            left->addView(title_v);
            hdr->addView(left);

            cbox->addView(hdr);
            if (!blk.children.empty()) {
                auto* body_wrap = new brls::Box(brls::Axis::COLUMN);
                body_wrap->setWidthPercentage(100);
                body_wrap->setMarginTop(8.0f);

                MarkdownStyle cs = style;
                cs.font_size = std::max(13.5f, style.font_size * 0.95f);
                cs.max_lines = 0;
                populateBlockChildren(body_wrap, blk.children, cs, alive);

                if (blk.callout_foldable) {
                    const bool start_collapsed = blk.callout_default_collapsed;
                    body_wrap->setVisibility(start_collapsed ? brls::Visibility::GONE
                                                             : brls::Visibility::VISIBLE);
                    auto state = std::make_shared<bool>(start_collapsed);
                    auto* toggle_btn =
                        ui::makeButton(start_collapsed ? "展开" : "收起", nullptr, false, 28.0f);
                    toggle_btn->setMarginLeft(8.0f);
                    toggle_btn->setShrink(0.0f);
                    toggle_btn->registerClickAction(
                        [alive, state, body_wrap, toggle_btn](brls::View*) {
                            if (!alive || !alive->load(std::memory_order_acquire)) return true;
                            *state = !*state;
                            body_wrap->setVisibility(*state ? brls::Visibility::GONE
                                                            : brls::Visibility::VISIBLE);
                            toggle_btn->setText(*state ? "展开" : "收起");
                            return true;
                        });
                    hdr->addView(toggle_btn);
                }
                cbox->addView(body_wrap);
            }
            return cbox;
        }

        case MarkdownBlockKind::CodeBlock: {
            auto* card = ui::makeCard("desktop/soft", 12.0f, 8.0f);
            if (!blk.code_lang.empty() || blk.text.size() >= 24) {
                auto* hdr = new brls::Box(brls::Axis::ROW);
                hdr->setWidthPercentage(100);
                hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
                hdr->setAlignItems(brls::AlignItems::CENTER);
                hdr->setMarginBottom(6.0f);
                hdr->addView(ui::makePill(
                    blk.code_lang.empty() ? "code" : blk.code_lang, "desktop/surface", "font/grey"));
                const std::string code_copy = blk.text;
                hdr->addView(ui::makeButton(
                    "复制",
                    [code_copy]() {
                        SDL_SetClipboardText(code_copy.c_str());
                        brls::Application::notify("已复制代码片段");
                    },
                    false,
                    28.0f));
                card->addView(hdr);
            }
            auto* code_lbl = ui::makeLabel(
                blk.text, std::max(13.5f, style.font_size * 0.90f), style.text_color_token, 1.48f);
            card->addView(code_lbl);
            return card;
        }

        case MarkdownBlockKind::MathBlock: {
            auto* mbox = new AccentBlockBox("desktop/soft", "domain/book", 12.0f);
            auto* mlbl = ui::makeLabel(
                blk.text, std::max(14.5f, style.font_size * 0.98f), style.text_color_token, 1.50f);
            mbox->addView(mlbl);
            return mbox;
        }

        case MarkdownBlockKind::Table: {
            const size_t ncols = blk.table_headers.size();
            if (ncols == 0) return nullptr;

            // Compute balanced proportional column widths from content length
            std::vector<float> weights(ncols, 8.0f);
            auto measureColChars = [](const std::string& s) -> float {
                const std::string plain = stripMarkdownToPlainText(s, true);
                size_t chars = 0;
                size_t p = 0;
                while (p < plain.size()) {
                    uint32_t cp = 0;
                    p += decodeUtf8(plain, p, cp);
                    chars += isCjkOrWideCodepoint(cp) ? 2 : 1;
                }
                return std::clamp(static_cast<float>(chars), 6.0f, 44.0f);
            };
            for (size_t c = 0; c < ncols; ++c) {
                weights[c] = std::max(weights[c], measureColChars(blk.table_headers[c]));
            }
            for (const auto& r : blk.table_rows) {
                for (size_t c = 0; c < std::min(ncols, r.size()); ++c) {
                    weights[c] = std::max(weights[c], measureColChars(r[c]));
                }
            }
            float total_w = 0.0f;
            for (float w : weights) total_w += w;
            if (total_w <= 0.0f) total_w = static_cast<float>(ncols);

            auto* tcard = new brls::Box(brls::Axis::COLUMN);
            tcard->setWidthPercentage(100);
            tcard->setCornerRadius(8.0f);
            tcard->setBorderThickness(1.0f);
            tcard->setBorderColor(ui::color("desktop/line"));
            tcard->setBackgroundColor(ui::color("desktop/surface"));

            auto buildRowView = [&](const std::vector<std::string>& cells, bool is_header) {
                auto* rbox = new brls::Box(brls::Axis::ROW);
                rbox->setWidthPercentage(100);
                rbox->setPadding(8.0f, 10.0f, 8.0f, 10.0f);
                if (is_header) {
                    rbox->setBackgroundColor(ui::color("desktop/soft"));
                }
                for (size_t c = 0; c < ncols; ++c) {
                    auto* cell_box = new brls::Box(brls::Axis::COLUMN);
                    const float pct = (weights[c] / total_w) * 100.0f;
                    cell_box->setWidthPercentage(pct);
                    if (c + 1 < ncols) {
                        cell_box->setPaddingRight(10.0f);
                    }
                    MarkdownStyle cs = style;
                    cs.font_size = std::max(13.5f, style.font_size * 0.92f);
                    cs.line_height = 1.48f;
                    cs.bold_base = is_header;
                    cs.max_lines = 0;
                    const std::string cell_txt = (c < cells.size()) ? cells[c] : "";
                    auto* cv = new MarkdownRichTextView(cell_txt, cs);
                    cv->setWidthPercentage(100);
                    cell_box->addView(cv);
                    rbox->addView(cell_box);
                }
                return rbox;
            };

            tcard->addView(buildRowView(blk.table_headers, true));
            for (const auto& row : blk.table_rows) {
                auto* div = new brls::Box(brls::Axis::ROW);
                div->setWidthPercentage(100);
                div->setHeight(1.0f);
                div->setBackgroundColor(ui::color("desktop/line"));
                tcard->addView(div);
                tcard->addView(buildRowView(row, false));
            }
            return tcard;
        }

        case MarkdownBlockKind::Image: {
            auto* fig = ui::makeCard("desktop/soft", 10.0f, 8.0f);
            fig->setAlignItems(brls::AlignItems::CENTER);
            const std::string norm_url =
                ImageCache::normalizeImageUrl(blk.image_url, style.base_url);
            if (!norm_url.empty()) {
                auto* img = new CachedImageView();
                img->setWidthPercentage(100);
                img->setHeight(260.0f);
                img->setScalingType(brls::ImageScalingType::FIT);
                img->setCornerRadius(6.0f);
                img->setImageUrl(norm_url, style.image_headers);
                fig->addView(img);
            }
            if (!blk.text.empty()) {
                MarkdownStyle caps = style;
                caps.font_size = std::max(13.0f, style.font_size * 0.86f);
                caps.text_color_token = style.muted_color_token;
                caps.italic_base = true;
                auto* cap = new MarkdownRichTextView(blk.text, caps);
                cap->setMarginTop(6.0f);
                fig->addView(cap);
            }
            return fig;
        }

        case MarkdownBlockKind::HorizontalRule:
            return ui::makeDivider(6.0f);
    }
    return nullptr;
}

}  // namespace

MarkdownView::MarkdownView()
    : brls::Box(brls::Axis::COLUMN),
      alive_(std::make_shared<std::atomic<bool>>(true)) {
    setWidthPercentage(100);
}

MarkdownView::MarkdownView(const std::string& markdown, MarkdownStyle style)
    : MarkdownView() {
    style_ = std::move(style);
    raw_markdown_ = markdown;
    rebuild();
}

MarkdownView::~MarkdownView() {
    if (alive_) {
        alive_->store(false, std::memory_order_release);
    }
}

void MarkdownView::setMarkdown(const std::string& markdown) {
    raw_markdown_ = markdown;
    rebuild();
}

void MarkdownView::setStyle(MarkdownStyle style) {
    style_ = std::move(style);
    rebuild();
}

void MarkdownView::rebuild() {
    clearViews();
    if (raw_markdown_.empty()) return;

    if (style_.max_lines > 0) {
        auto* inline_v = new MarkdownRichTextView(
            sanitizeMarkdownExcerpt(raw_markdown_, static_cast<size_t>(style_.max_lines) * 80),
            style_);
        inline_v->setWidthPercentage(100);
        addView(inline_v);
        return;
    }

    const auto blocks = parseMarkdownBlocks(raw_markdown_);
    populateBlockChildren(this, blocks, style_, alive_);
}

namespace ui {

MarkdownView* makeMarkdown(const std::string& markdown,
                           float font_size,
                           const std::string& color_token,
                           float line_height) {
    MarkdownStyle st;
    st.font_size = font_size;
    st.text_color_token = color_token;
    st.line_height = line_height;
    return new MarkdownView(markdown, std::move(st));
}

MarkdownRichTextView* makeMarkdownInline(const std::string& markdown,
                                         float font_size,
                                         const std::string& color_token,
                                         float line_height,
                                         int max_lines,
                                         bool interactive_links) {
    MarkdownStyle st;
    st.font_size = font_size;
    st.text_color_token = color_token;
    st.line_height = line_height;
    st.max_lines = max_lines;
    st.interactive_links = interactive_links;
    auto* v = new MarkdownRichTextView(markdown, std::move(st));
    v->setWidthPercentage(100);
    return v;
}

}  // namespace ui

}  // namespace omnidesk
