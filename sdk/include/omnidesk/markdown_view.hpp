#pragma once

#include <borealis.hpp>
#include <functional>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "omnidesk/export.h"

namespace omnidesk {

struct OMNIDESK_API MarkdownInlineSpan {
    std::string text;
    std::string link_url;
    bool bold = false;
    bool italic = false;
    bool code = false;
    bool strikethrough = false;
    bool highlight = false;
    bool math = false;
};

struct OMNIDESK_API MarkdownListItem {
    int indent_level = 0;
    bool ordered = false;
    int number = 1;
    int task_state = -1;  ///< -1: normal item, 0: unchecked [ ], 1: checked [x]
    std::string text;
};

enum class MarkdownTableAlign {
    Left,
    Center,
    Right,
};

enum class MarkdownBlockKind {
    Heading,
    Paragraph,
    List,
    Blockquote,
    Callout,
    CodeBlock,
    MathBlock,
    Table,
    Image,
    HorizontalRule,
};

struct OMNIDESK_API MarkdownBlock {
    MarkdownBlockKind kind = MarkdownBlockKind::Paragraph;
    int heading_level = 1;
    std::string text;
    std::string code_lang;
    std::string image_url;
    std::string callout_type;
    std::string callout_title;
    bool callout_foldable = false;
    bool callout_default_collapsed = false;
    std::vector<MarkdownListItem> list_items;
    std::vector<std::string> table_headers;
    std::vector<MarkdownTableAlign> table_aligns;
    std::vector<std::vector<std::string>> table_rows;
    std::vector<MarkdownBlock> children;
};

struct OMNIDESK_API MarkdownStyle {
    float font_size = 16.5f;
    float line_height = 1.62f;
    std::string text_color_token = "brls/text";
    std::string muted_color_token = "font/grey";
    std::string link_color_token = "domain/text";
    std::string code_fg_token = "brls/accent";
    std::string code_bg_token = "desktop/soft";
    float block_gap = 8.0f;
    int max_lines = 0;  ///< 0 = unlimited, >0 = clamp with trailing ellipsis
    bool bold_base = false;
    bool italic_base = false;
    bool interactive_links = true;
    std::string base_url;
    std::unordered_map<std::string, std::string> image_headers;
    std::function<void(const std::string& url, const std::string& label)> on_link_click;
};

/// Parse inline CommonMark + GFM + Obsidian spans (**bold**, *italic*, `code`, ~~strike~~,
/// ==highlight==, [label](url), [[target|alias]], autolinks, and inline $math$).
OMNIDESK_API std::vector<MarkdownInlineSpan> parseMarkdownInlines(const std::string& markdown);

/// Parse block-level CommonMark + GFM + Obsidian Markdown (headings, paragraphs, nested/loose
/// ordered & unordered & task lists, blockquotes, `[!callout]` foldable blocks, pipe tables,
/// fenced code blocks, `$$` math blocks, standalone images, and horizontal rules).
OMNIDESK_API std::vector<MarkdownBlock> parseMarkdownBlocks(const std::string& markdown);

/// Strip Markdown formatting into clean human-readable plain text (for previews, search, popovers).
OMNIDESK_API std::string stripMarkdownToPlainText(const std::string& markdown,
                                                  bool single_line = false);

/// Clean up common LaTeX math commands/subscripts/superscripts into crisp Unicode symbols.
OMNIDESK_API std::string prettifyLatexMath(const std::string& latex);

/// Convert multi-block Markdown into clean single-paragraph inline Markdown suitable for feed excerpts
/// (preserving *italic*, **bold**, `code`, while stripping `#` headings, callout headers, and nav bars).
OMNIDESK_API std::string sanitizeMarkdownExcerpt(const std::string& markdown,
                                                 size_t max_chars = 160);

/// Custom NanoVG inline rich-text view supporting bold, oblique italic, inline code pills,
/// highlights, strikethrough, clickable links, CJK/word wrapping, and line-clamping.
class OMNIDESK_API MarkdownRichTextView : public brls::View {
public:
    MarkdownRichTextView();
    MarkdownRichTextView(const std::string& markdown, MarkdownStyle style = {});
    ~MarkdownRichTextView() override;

    void setMarkdown(const std::string& markdown);
    void setStyle(MarkdownStyle style);
    const std::string& markdown() const { return raw_markdown_; }
    const MarkdownStyle& markdownStyle() const { return style_; }

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style style,
              brls::FrameContext* ctx) override;
    void onLayout() override;
    brls::View* hitTest(brls::Point point) override;

    struct Impl;
    Impl* impl() { return impl_.get(); }

private:
    std::string raw_markdown_;
    MarkdownStyle style_;
    std::unique_ptr<Impl> impl_;
};

/// Composite Borealis block-level Markdown document / fragment view.
class OMNIDESK_API MarkdownView : public brls::Box {
public:
    MarkdownView();
    explicit MarkdownView(const std::string& markdown, MarkdownStyle style = {});
    ~MarkdownView() override;

    void setMarkdown(const std::string& markdown);
    void setStyle(MarkdownStyle style);
    const std::string& markdown() const { return raw_markdown_; }
    const MarkdownStyle& markdownStyle() const { return style_; }

private:
    void rebuild();

    std::string raw_markdown_;
    MarkdownStyle style_;
    std::shared_ptr<std::atomic<bool>> alive_;
};

namespace ui {

OMNIDESK_API MarkdownView* makeMarkdown(const std::string& markdown,
                                        float font_size = 16.5f,
                                        const std::string& color_token = "brls/text",
                                        float line_height = 1.62f);

OMNIDESK_API MarkdownRichTextView* makeMarkdownInline(const std::string& markdown,
                                                      float font_size = 16.0f,
                                                      const std::string& color_token = "brls/text",
                                                      float line_height = 1.45f,
                                                      int max_lines = 0,
                                                      bool interactive_links = true);

}  // namespace ui

}  // namespace omnidesk
