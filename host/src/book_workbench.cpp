#include "omnidesk/host/book_workbench.hpp"
#include "omnidesk/host/async_load.hpp"
#include "omnidesk/host/curation_calendar_dialog.hpp"
#include "omnidesk/host/recycling_grid.hpp"
#include "omnidesk/host/ui_comfort.hpp"

#include <SDL.h>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <sstream>
#include <yoga/Yoga.h>
#include "omnidesk/host/kernel.hpp"
#include "omnidesk/image_cache.hpp"
#include "omnidesk/markdown_view.hpp"
#include "omnidesk/selection_action_popover.hpp"
#include "omnidesk/ui_theme.hpp"

namespace omnidesk::host {
namespace {

constexpr const char* kPdfPageImageBlockId = "__pdf_page_image__";

void wakePdfSdlEventLoop() {
    if (SDL_WasInit(SDL_INIT_EVENTS) == 0) return;
    static const Uint32 kWakeEvt = SDL_RegisterEvents(1);
    if (kWakeEvt == static_cast<Uint32>(-1)) return;
    SDL_Event ev{};
    ev.type = kWakeEvt;
    SDL_PushEvent(&ev);
}

class PdfPageImageView final : public brls::View {
public:
    PdfPageImageView() : alive_(std::make_shared<std::atomic<bool>>(true)) {
        YGNodeSetMeasureFunc(this->ygNode, &PdfPageImageView::measureFunc);
        YGNodeSetNodeType(this->ygNode, YGNodeTypeDefault);
        setFocusable(true);
        setHighlightCornerRadius(6.0f);
    }

    ~PdfPageImageView() override {
        alive_->store(false, std::memory_order_release);
        if (pending_cancel_) {
            pending_cancel_->cancel();
            pending_cancel_.reset();
        }
        if (texture_ > 0) {
            if (auto* vg = brls::Application::getNVGContext()) {
                nvgDeleteImage(vg, texture_);
            }
            texture_ = 0;
        }
    }

    void setPageImage(const std::string& raw_path, int page_number) {
        page_number_ = std::max(1, page_number);
        const std::string url = ImageCache::normalizeImageUrl(raw_path);
        if (url == current_url_ && (texture_ > 0 || url.empty())) {
            return;
        }
        if (pending_cancel_) {
            pending_cancel_->cancel();
            pending_cancel_.reset();
        }
        current_url_ = url;
        failed_ = false;
        if (url.empty()) {
            return;
        }
        auto alive = alive_;
        pending_cancel_ = ImageCache::instance().fetchDecoded(
            url,
            [this, alive, url](DecodedImage img) {
                brls::sync([this, alive, url, img = std::move(img)]() {
                    brls::Application::setActiveEvent(true);
                    if (!alive->load(std::memory_order_acquire) || url != current_url_) return;
                    if (!img.ok || img.rgba.empty() || img.width <= 0 || img.height <= 0) {
                        failed_ = true;
                        return;
                    }
                    auto* plat = brls::Application::getPlatform();
                    if (!plat || !plat->getVideoContext()) return;
                    NVGcontext* vg = plat->getVideoContext()->getNVGContext();
                    if (!vg) return;
                    int tex = nvgCreateImageRGBA(
                        vg, img.width, img.height, NVG_IMAGE_GENERATE_MIPMAPS, img.rgba.data());
                    if (tex <= 0) {
                        failed_ = true;
                        return;
                    }
                    if (texture_ > 0) {
                        nvgDeleteImage(vg, texture_);
                    }
                    texture_ = tex;
                    failed_ = false;
                    const float new_aspect =
                        static_cast<float>(img.height) / static_cast<float>(std::max(1, img.width));
                    if (std::abs(new_aspect - aspect_ratio_) > 0.002f) {
                        aspect_ratio_ = new_aspect;
                        invalidate();
                    }
                });
                wakePdfSdlEventLoop();
            },
            {},
            nullptr,
            1600,
            2400);
    }

    void draw(NVGcontext* vg, float x, float y, float width, float height,
              brls::Style /*style*/, brls::FrameContext* /*ctx*/) override {
        if (!vg || width <= 1.0f || height <= 1.0f) return;

        NVGpaint shadow = nvgBoxGradient(
            vg, x, y + 2.0f, width, height, 6.0f, 10.0f,
            nvgRGBA(0, 0, 0, 68), nvgRGBA(0, 0, 0, 0));
        nvgBeginPath(vg);
        nvgRect(vg, x - 6.0f, y - 4.0f, width + 12.0f, height + 14.0f);
        nvgRoundedRect(vg, x, y, width, height, 6.0f);
        nvgPathWinding(vg, NVG_HOLE);
        nvgFillPaint(vg, shadow);
        nvgFill(vg);

        nvgBeginPath(vg);
        nvgRoundedRect(vg, x, y, width, height, 6.0f);
        nvgFillColor(vg, texture_ > 0 ? nvgRGB(255, 255, 255) : ui::color("desktop/soft"));
        nvgFill(vg);

        if (texture_ > 0) {
            NVGpaint paint = nvgImagePattern(vg, x, y, width, height, 0.0f, texture_, 1.0f);
            nvgBeginPath(vg);
            nvgRoundedRect(vg, x, y, width, height, 6.0f);
            nvgFillPaint(vg, paint);
            nvgFill(vg);
        } else {
            nvgFontSize(vg, 15.0f);
            nvgTextAlign(vg, NVG_ALIGN_CENTER | NVG_ALIGN_MIDDLE);
            nvgFillColor(vg, ui::color("font/grey"));
            const std::string msg = failed_
                ? ("无法渲染第 " + std::to_string(page_number_) + " 页 PDF 图像")
                : ("正在渲染 PDF 第 " + std::to_string(page_number_) + " 页…");
            nvgText(vg, x + width * 0.5f, y + std::min(height * 0.5f, 180.0f), msg.c_str(), nullptr);
        }

        nvgBeginPath(vg);
        nvgRoundedRect(vg, x + 0.5f, y + 0.5f, width - 1.0f, height - 1.0f, 6.0f);
        nvgStrokeWidth(vg, 1.0f);
        nvgStrokeColor(vg, ui::color("desktop/line"));
        nvgStroke(vg);
    }

    static View* create() { return new PdfPageImageView(); }

private:
    static YGSize measureFunc(YGNodeRef node,
                              float width,
                              YGMeasureMode widthMode,
                              float height,
                              YGMeasureMode heightMode) {
        auto* self = static_cast<PdfPageImageView*>(YGNodeGetContext(node));
        const float aspect =
            (self && self->aspect_ratio_ > 0.05f) ? self->aspect_ratio_ : 1.36f;

        float out_w = 680.0f;
        if (widthMode != YGMeasureModeUndefined && !std::isnan(width) && width > 1.0f) {
            out_w = width;
        }
        float out_h = std::round(out_w * aspect);
        if (heightMode == YGMeasureModeExactly && !std::isnan(height) && height > 1.0f) {
            out_h = height;
        } else if (heightMode == YGMeasureModeAtMost && !std::isnan(height) && height > 1.0f) {
            out_h = std::min(out_h, height);
        }
        return YGSize{out_w, out_h};
    }

    int texture_ = 0;
    int page_number_ = 1;
    float aspect_ratio_ = 1.36f;
    bool failed_ = false;
    std::string current_url_;
    std::shared_ptr<CancelToken> pending_cancel_;
    std::shared_ptr<std::atomic<bool>> alive_;
};

void wrapFlexRow(brls::Box* box) {
    ui_comfort::enableFlexWrap(box, 6.0f);
}

std::string clipTitleUtf8(const std::string& s, size_t max_chars) {
    size_t chars = 0;
    size_t i = 0;
    while (i < s.size() && chars < max_chars) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        size_t len = 1;
        if ((c & 0x80) == 0) len = 1;
        else if ((c & 0xE0) == 0xC0) len = 2;
        else if ((c & 0xF0) == 0xE0) len = 3;
        else if ((c & 0xF8) == 0xF0) len = 4;
        if (i + len > s.size()) break;
        i += len;
        ++chars;
    }
    if (i >= s.size()) return s;
    return s.substr(0, i) + "…";
}

std::string toLowerCopy(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

bool sameVocabCard(const VocabCard& a, const VocabCard& b) {
    return a.word == b.word && a.lemma == b.lemma && a.phonetic == b.phonetic &&
           a.etymology == b.etymology && a.meaning == b.meaning &&
           a.context_sentence == b.context_sentence && a.source_book == b.source_book &&
           a.interval_days == b.interval_days && a.repetitions == b.repetitions &&
           a.ease_factor == b.ease_factor;
}

void mergeRefreshedVocab(std::vector<VocabCard>& current,
                         std::vector<VocabCard> loaded,
                         const std::vector<VocabCard>& previous) {
    std::unordered_map<std::string, const VocabCard*> previous_by_key;
    previous_by_key.reserve(previous.size());
    for (const auto& card : previous) {
        previous_by_key[toLowerCopy(card.word)] = &card;
    }

    std::unordered_map<std::string, size_t> loaded_by_key;
    loaded_by_key.reserve(loaded.size());
    for (size_t i = 0; i < loaded.size(); ++i) {
        loaded_by_key[toLowerCopy(loaded[i].word)] = i;
    }

    std::vector<VocabCard> merged = std::move(loaded);
    for (const auto& local : current) {
        const std::string key = toLowerCopy(local.word);
        const auto previous_it = previous_by_key.find(key);
        const bool changed = previous_it == previous_by_key.end() ||
                             !sameVocabCard(local, *previous_it->second);
        const auto loaded_it = loaded_by_key.find(key);
        if (loaded_it == loaded_by_key.end()) {
            merged.push_back(local);
        } else if (changed) {
            merged[loaded_it->second] = local;
        }
    }
    current = std::move(merged);
}

std::string extractTranslationText(const std::string& raw) {
    const std::string open_tag = "<translation>";
    const std::string close_tag = "</translation>";
    const size_t start = raw.find(open_tag);
    if (start == std::string::npos) {
        return raw;
    }
    const size_t body_start = start + open_tag.size();
    const size_t end = raw.find(close_tag, body_start);
    if (end == std::string::npos) {
        return raw.substr(body_start);
    }
    return raw.substr(body_start, end - body_start);
}

std::string findContextSentenceForWord(const BookSpread& spread, const std::string& word) {
    const std::string needle = toLowerCopy(word);
    std::string fallback;
    for (const auto& blk : spread.blocks) {
        if (blk.original.empty() || blk.kind == "image") continue;
        if (fallback.empty() && blk.kind == "paragraph") {
            fallback = blk.original;
        }
        if (!needle.empty() && toLowerCopy(blk.original).find(needle) != std::string::npos) {
            return blk.original;
        }
    }
    if (!fallback.empty()) return fallback;
    for (const auto& blk : spread.blocks) {
        if (!blk.original.empty() && blk.kind != "image") return blk.original;
    }
    return {};
}

std::string formatClipSeconds(double sec) {
    if (std::isnan(sec) || std::isinf(sec) || sec <= 0.0) {
        return "00:00";
    }
    const int total = std::max(0, static_cast<int>(sec));
    const int m = total / 60;
    const int s = total % 60;
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", m, s);
    return std::string(buf);
}

std::vector<BookItem> buildShowcaseBooks() {
    return {
        {"book-pdf-1", "plugin-pdf-babeldoc", "Designing Data-Intensive Applications (PDF)", "Martin Kleppmann",
         "pdf", "/nas/books/ddia.pdf", "", 142, 560, 25.4, true, 18, true},
        {"book-pdf-2", "plugin-pdf-babeldoc", "Attention Is All You Need · 论文精读", "Vaswani et al.",
         "pdf", "/nas/papers/transformer.pdf", "", 4, 15, 26.6, true, 9, true},
        {"book-epub-1", "plugin-ebook", "The Hitchhiker's Guide to the Galaxy", "Douglas Adams",
         "epub", "/nas/books/h2g2.epub", "", 68, 224, 30.3, true, 34, true},
        {"book-azw3-1", "plugin-ebook", "Dune (Annotated Bilingual Companion)", "Frank Herbert",
         "azw3", "/nas/books/dune.azw3", "", 112, 688, 16.2, true, 41, false},
    };
}

BookSpread buildShowcaseSpread(const BookItem& book, int page_num) {
    BookSpread s;
    s.book_id = book.id;
    s.page_number = std::max(1, page_num);
    s.total_pages = book.total_pages;
    s.chapter_title = (book.format == "pdf") ? "BabelDOC 版面保真对照页" : "电子书伴读双语精析页";
    s.blocks = {
        {"b1", "heading",
         "Section " + std::to_string(s.page_number) + " · Structured Systems & Memory Models",
         "第 " + std::to_string(s.page_number) + " 节 · 结构化系统与记忆模型"},
        {"b2", "paragraph",
         "Reliable systems continue to work correctly even when individual components experience partial degradation, network partitions, or transient latency spikes.",
         "即使个别组件遭遇局部降级、网络分区或瞬时延迟抖动，可靠的系统依然能够保持整体行为的正确性与数据一致性。",
         {"degradation", "transient", "partition"}},
        {"b3", "formula",
         "P(recall | t, I) = exp(-t / (I * ease_factor))",
         "间隔重复回忆概率公式：由 SM-2 记忆引擎根据反馈动态调整难度系数。",
         {"ease_factor"}},
    };
    return s;
}

std::vector<VocabCard> buildShowcaseVocab() {
    return {
        {"ephemeral", "ephemeral", "/ɪˈfɛmərəl/", "epi- (upon) + hemera (day) -> 朝生暮死的、短暂的",
         "短暂的；瞬息的；临时性的",
         "Containers spawn ephemeral staging directories inside tmpfs.",
         "The Hitchhiker's Guide to the Galaxy", 4, 2, 2.5},
        {"degradation", "degrade", "/ˌdɛɡrəˈdeɪʃən/", "de- (down) + gradus (step) -> 降级、退化",
         "降级；性能退化；磨损",
         "Graceful degradation preserves core reading continuity.",
         "Designing Data-Intensive Applications", 7, 3, 2.6},
    };
}

bool focusIsInsideBookView(brls::View* ancestor) {
    if (!ancestor) return false;
    brls::View* focus = brls::Application::getCurrentFocus();
    while (focus) {
        if (focus == ancestor) return true;
        focus = focus->getParent();
    }
    return false;
}

float estimateBookStageWidth(const brls::View* self) {
    const float cw = brls::Application::contentWidth > 0 ? brls::Application::contentWidth : 1280.0f;
    float avail = cw - 260.0f - 48.0f;
    auto* v = const_cast<brls::View*>(self);
    if (v && v->getParent() && v->getParent()->getParent()) {
        if (auto* split = dynamic_cast<brls::Box*>(v->getParent()->getParent()->getParent())) {
            if (split->getAxis() == brls::Axis::ROW) {
                for (auto* ch : split->getChildren()) {
                    if (ch != v->getParent()->getParent() &&
                        ch->getVisibility() == brls::Visibility::VISIBLE) {
                        avail -= 388.0f;
                    }
                }
            }
        }
    }
    return std::max(320.0f, avail);
}

int computeBookshelfColumns(float stage_w) {
    if (stage_w < 680.0f) return 2;
    if (stage_w < 960.0f) return 3;
    if (stage_w < 1420.0f) return 4;
    if (stage_w < 1950.0f) return 5;
    return 6;
}

uint64_t fnv1a64Cover(const std::string& input) {
    uint64_t h = 14695981039346656037ULL;
    for (unsigned char c : input) {
        h ^= c;
        h *= 1099511628211ULL;
    }
    return h;
}

std::string asciiUpperCopy(std::string s) {
    for (char& c : s) {
        c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }
    return s;
}

bool isRasterCoverFileLocal(const std::string& path) {
    if (path.empty()) return false;
    std::error_code ec;
    if (!std::filesystem::is_regular_file(path, ec)) return false;
    const std::string ext = toLowerCopy(std::filesystem::path(path).extension().string());
    return ext == ".png" || ext == ".jpg" || ext == ".jpeg" ||
           ext == ".webp" || ext == ".bmp" || ext == ".gif";
}

size_t estimateWrappedLines(const std::string& text, float avail_px, float font_size) {
    if (text.empty()) return 0;
    const float safe_w = std::max(140.0f, avail_px);
    const float avg_ascii_w = std::max(5.5f, font_size * 0.54f);
    size_t lines = 1;
    float line_w = 0.0f;
    size_t i = 0;
    while (i < text.size()) {
        const unsigned char c = static_cast<unsigned char>(text[i]);
        if (c == '\n') {
            ++lines;
            line_w = 0.0f;
            ++i;
            continue;
        }
        size_t len = 1;
        float glyph_w = avg_ascii_w;
        if ((c & 0x80) == 0) {
            len = 1;
        } else if ((c & 0xE0) == 0xC0) {
            len = 2;
            glyph_w = font_size * 0.90f;
        } else if ((c & 0xF0) == 0xE0) {
            len = 3;
            glyph_w = font_size * 1.02f;
        } else if ((c & 0xF8) == 0xF0) {
            len = 4;
            glyph_w = font_size * 1.05f;
        }
        line_w += glyph_w;
        if (line_w > safe_w) {
            ++lines;
            line_w = glyph_w;
        }
        i = std::min(text.size(), i + len);
    }
    return lines;
}

class ReusablePillView final : public brls::Box {
public:
    ReusablePillView() : brls::Box(brls::Axis::ROW) {
        setPadding(3.0f, 8.0f, 3.0f, 8.0f);
        setCornerRadius(6.0f);
        setAlignItems(brls::AlignItems::CENTER);
        setShrink(0.0f);
        lbl_ = ui::makeLabel("", 11.5f, "font/grey");
        lbl_->setSingleLine(true);
        addView(lbl_);
    }

    void bind(const std::string& text,
              const std::string& bg_token,
              const std::string& fg_token) {
        setBackgroundColor(ui::color(bg_token));
        lbl_->setText(text);
        lbl_->setTextColor(ui::color(fg_token));
    }

private:
    brls::Label* lbl_ = nullptr;
};

class ReusableWeReadCoverView final : public brls::Box {
public:
    ReusableWeReadCoverView(float cover_height, bool compact_thumb)
        : brls::Box(brls::Axis::ROW), compact_thumb_(compact_thumb) {
        setHeight(cover_height);
        if (compact_thumb_) {
            setWidth(std::round(cover_height * 0.70f));
            setShrink(0.0f);
        } else {
            setWidthPercentage(100);
        }
        setCornerRadius(compact_thumb_ ? 6.0f : 8.0f);
        setClipsToBounds(true);
        setBorderThickness(1.0f);
        setBorderColor(ui::color("desktop/line"));

        img_wrap_ = new brls::Box(brls::Axis::COLUMN);
        img_wrap_->setWidthPercentage(100);
        img_wrap_->setHeightPercentage(100);
        img_ = new CachedImageView();
        img_->setWidthPercentage(100);
        img_->setHeightPercentage(100);
        img_->setScalingType(brls::ImageScalingType::FIT);
        img_wrap_->addView(img_);
        addView(img_wrap_);

        cloth_wrap_ = new brls::Box(brls::Axis::ROW);
        cloth_wrap_->setWidthPercentage(100);
        cloth_wrap_->setHeightPercentage(100);

        spine_ = new brls::Box(brls::Axis::COLUMN);
        spine_->setWidth(compact_thumb_ ? 5.0f : 9.0f);
        spine_->setHeightPercentage(100);
        spine_->setShrink(0.0f);
        cloth_wrap_->addView(spine_);

        auto* body = new brls::Box(brls::Axis::COLUMN);
        body->setGrow(1.0f);
        body->setShrink(1.0f);
        body->setHeightPercentage(100);
        body->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        body->setPadding(compact_thumb_ ? 6.0f : 12.0f);

        if (compact_thumb_) {
            tiny_title_ = new brls::Label();
            tiny_title_->setFontSize(11.0f);
            tiny_title_->setLineHeight(1.22f);
            tiny_title_->setTextColor(nvgRGBA(244, 246, 250, 235));
            body->addView(tiny_title_);

            tiny_fmt_ = new brls::Label();
            tiny_fmt_->setFontSize(9.5f);
            tiny_fmt_->setTextColor(nvgRGBA(200, 210, 225, 190));
            body->addView(tiny_fmt_);
        } else {
            auto* top_row = new brls::Box(brls::Axis::ROW);
            top_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            fmt_lbl_ = new brls::Label();
            fmt_lbl_->setFontSize(11.5f);
            fmt_lbl_->setTextColor(nvgRGBA(212, 222, 236, 210));
            top_row->addView(fmt_lbl_);
            body->addView(top_row);

            title_lbl_ = new brls::Label();
            title_lbl_->setFontSize(15.5f);
            title_lbl_->setLineHeight(1.36f);
            title_lbl_->setTextColor(nvgRGB(248, 250, 252));
            body->addView(title_lbl_);

            foot_lbl_ = new brls::Label();
            foot_lbl_->setFontSize(12.0f);
            foot_lbl_->setSingleLine(true);
            foot_lbl_->setTextColor(nvgRGBA(203, 213, 225, 205));
            body->addView(foot_lbl_);
        }

        cloth_wrap_->addView(body);
        addView(cloth_wrap_);
    }

    void bind(const BookItem& book, const BookShelfEntry& entry, float cover_height) {
        setHeight(cover_height);
        if (compact_thumb_) {
            setWidth(std::round(cover_height * 0.70f));
        }

        const std::string cover_file =
            isRasterCoverFileLocal(book.cover_path)
                ? book.cover_path
                : (isRasterCoverFileLocal(entry.cover_path) ? entry.cover_path : "");

        if (!cover_file.empty()) {
            const std::string img_url = ImageCache::normalizeImageUrl(cover_file);
            setBackgroundColor(ui::color("desktop/soft"));
            cloth_wrap_->setVisibility(brls::Visibility::GONE);
            img_wrap_->setVisibility(brls::Visibility::VISIBLE);
            if (img_url != bound_url_) {
                bound_url_ = img_url;
                img_->setImageUrl(img_url);
            }
            return;
        }

        if (!bound_url_.empty()) {
            bound_url_.clear();
            img_->setImageUrl("");
        }
        img_wrap_->setVisibility(brls::Visibility::GONE);
        cloth_wrap_->setVisibility(brls::Visibility::VISIBLE);

        static const struct {
            uint8_t r, g, b;
            uint8_t sr, sg, sb;
        } kPalettes[] = {
            {34, 49, 68, 23, 35, 50},   // Slate Navy
            {56, 44, 62, 39, 30, 44},   // Plum Ink
            {37, 58, 52, 25, 41, 36},   // Pine Cloth
            {66, 48, 38, 46, 33, 25},   // Warm Umber
            {44, 52, 66, 30, 36, 48},   // Oxford Charcoal
            {62, 42, 48, 44, 28, 33},   // Burgundy Binding
        };
        const size_t pal_idx = static_cast<size_t>(fnv1a64Cover(book.title) % 6);
        const auto& pal = kPalettes[pal_idx];

        setBackgroundColor(nvgRGB(pal.r, pal.g, pal.b));
        spine_->setBackgroundColor(nvgRGB(pal.sr, pal.sg, pal.sb));

        const std::string fmt = asciiUpperCopy(book.format.empty() ? "BOOK" : book.format);
        if (compact_thumb_) {
            tiny_title_->setText(clipTitleUtf8(book.title, 22));
            tiny_fmt_->setText(fmt);
        } else {
            fmt_lbl_->setText(fmt + (entry.pinned ? " · 置顶" : ""));
            title_lbl_->setText(clipTitleUtf8(book.title, 68));
            foot_lbl_->setText(clipTitleUtf8(
                book.author.empty() ? "OmniDesk Library" : book.author, 36));
        }
    }

private:
    bool compact_thumb_ = false;
    std::string bound_url_;
    brls::Box* img_wrap_ = nullptr;
    CachedImageView* img_ = nullptr;
    brls::Box* cloth_wrap_ = nullptr;
    brls::Box* spine_ = nullptr;
    brls::Label* tiny_title_ = nullptr;
    brls::Label* tiny_fmt_ = nullptr;
    brls::Label* fmt_lbl_ = nullptr;
    brls::Label* title_lbl_ = nullptr;
    brls::Label* foot_lbl_ = nullptr;
};

class BookGridCell final : public RecyclingGridCell {
public:
    BookGridCell() {
        card_ = ui::makeCard("desktop/surface", 12.0f, 12.0f);
        card_->setWidthPercentage(100);
        card_->setHeightPercentage(100);
        card_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

        cover_click_ = new brls::Box(brls::Axis::COLUMN);
        cover_click_->setWidthPercentage(100);
        cover_click_->setFocusable(true);
        cover_click_->setHighlightCornerRadius(8.0f);
        cover_click_->registerClickAction([this](brls::View*) {
            if (on_primary_) on_primary_();
            return true;
        });

        cover_ = new ReusableWeReadCoverView(218.0f, false);
        cover_->setMarginBottom(10.0f);
        cover_click_->addView(cover_);

        batch_pill_ = new ReusablePillView();
        batch_pill_->setMarginBottom(6.0f);
        cover_click_->addView(batch_pill_);

        title_lbl_ = ui::makeLabel("", 15.0f, "brls/text", 1.34f);
        cover_click_->addView(title_lbl_);

        author_lbl_ = ui::makeLabel("", 13.0f, "font/grey");
        author_lbl_->setSingleLine(true);
        author_lbl_->setMarginTop(4.0f);
        cover_click_->addView(author_lbl_);

        card_->addView(cover_click_);

        bottom_box_ = new brls::Box(brls::Axis::COLUMN);
        bottom_box_->setWidthPercentage(100);
        bottom_box_->setShrink(0.0f);

        prog_track_ = new brls::Box(brls::Axis::ROW);
        prog_track_->setWidthPercentage(100);
        prog_track_->setHeight(4.0f);
        prog_track_->setCornerRadius(2.0f);
        prog_track_->setClipsToBounds(true);
        prog_track_->setBackgroundColor(ui::color("desktop/soft"));
        prog_track_->setMarginTop(6.0f);

        prog_fill_ = new brls::Box(brls::Axis::ROW);
        prog_fill_->setHeightPercentage(100);
        prog_fill_->setCornerRadius(2.0f);
        prog_fill_->setBackgroundColor(ui::color("domain/book"));
        prog_track_->addView(prog_fill_);
        bottom_box_->addView(prog_track_);

        footer_row_ = new brls::Box(brls::Axis::ROW);
        footer_row_->setWidthPercentage(100);
        footer_row_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        footer_row_->setAlignItems(brls::AlignItems::CENTER);
        footer_row_->setMarginTop(6.0f);

        status_lbl_ = ui::makeLabel("", 12.5f, "font/grey");
        status_lbl_->setShrink(1.0f);
        status_lbl_->setSingleLine(true);
        footer_row_->addView(status_lbl_);

        more_btn_ = ui::makeButton("详情", [this]() {
            if (on_detail_) on_detail_();
        }, false, 26.0f);
        more_btn_->setShrink(0.0f);
        more_btn_->setMarginLeft(6.0f);
        footer_row_->addView(more_btn_);

        bottom_box_->addView(footer_row_);
        card_->addView(bottom_box_);
        addView(card_);
    }

    void prepareForReuse() override {
        on_primary_ = nullptr;
        on_detail_ = nullptr;
    }

    void bindShelf(const BookWorkbench::ShelfDisplayItem& item,
                   bool batch_mode,
                   bool is_selected,
                   const std::string& group_name,
                   std::function<void()> on_primary,
                   std::function<void()> on_detail) {
        on_primary_ = std::move(on_primary);
        on_detail_ = std::move(on_detail);

        card_->setBackgroundColor(ui::color(is_selected ? "status/ok_bg" : "desktop/surface"));
        card_->setPadding(12.0f);
        cover_->bind(item.book, item.entry, 218.0f);

        if (batch_mode) {
            batch_pill_->bind(is_selected ? "已勾选" : "点击勾选",
                              is_selected ? "domain/book" : "desktop/soft",
                              is_selected ? "brls/text" : "font/grey");
            batch_pill_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            batch_pill_->setVisibility(brls::Visibility::GONE);
        }

        title_lbl_->setFontSize(15.0f);
        title_lbl_->setText(clipTitleUtf8(item.book.title, 52));
        author_lbl_->setFontSize(13.0f);
        author_lbl_->setText(item.book.author.empty() ? "本地藏书" : item.book.author);

        bottom_box_->setVisibility(brls::Visibility::VISIBLE);
        const float read_pct = (item.status == BookReadingStatus::Finished)
                                   ? 100.0f
                                   : static_cast<float>(item.book.progress_percent);
        if (read_pct > 0.5f) {
            prog_fill_->setWidthPercentage(std::clamp(read_pct, 2.0f, 100.0f));
            prog_track_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            prog_track_->setVisibility(brls::Visibility::GONE);
        }

        std::string st_txt = BookLibraryStore::statusDisplayLabel(item.status);
        if (item.status == BookReadingStatus::Reading) {
            st_txt += " " + std::to_string(static_cast<int>(std::round(item.book.progress_percent))) + "%";
        } else if (item.book.total_pages > 1) {
            st_txt += " · " + std::to_string(item.book.total_pages) + "页";
        }
        if (item.excerpt_count > 0) {
            st_txt += " · 笔记" + std::to_string(item.excerpt_count);
        }
        status_lbl_->setText(st_txt);
        status_lbl_->setTextColor(ui::color(
            item.status == BookReadingStatus::Reading ? "domain/book" : "font/grey"));

        if (batch_mode) {
            more_btn_->setVisibility(brls::Visibility::GONE);
        } else {
            more_btn_->setText(group_name.empty() ? "详情" : group_name);
            more_btn_->setVisibility(brls::Visibility::VISIBLE);
        }
    }

    void bindDailyEnglish(const BookItem& book, std::function<void()> on_open) {
        on_primary_ = std::move(on_open);
        on_detail_ = nullptr;

        card_->setBackgroundColor(ui::color("desktop/surface"));
        card_->setPadding(14.0f);
        BookShelfEntry dummy;
        cover_->bind(book, dummy, 212.0f);
        batch_pill_->setVisibility(brls::Visibility::GONE);

        title_lbl_->setFontSize(15.5f);
        title_lbl_->setText(clipTitleUtf8(book.title, 54));
        author_lbl_->setFontSize(13.5f);
        author_lbl_->setText(book.author);
        bottom_box_->setVisibility(brls::Visibility::GONE);
    }

private:
    brls::Box* card_ = nullptr;
    brls::Box* cover_click_ = nullptr;
    ReusableWeReadCoverView* cover_ = nullptr;
    ReusablePillView* batch_pill_ = nullptr;
    brls::Label* title_lbl_ = nullptr;
    brls::Label* author_lbl_ = nullptr;
    brls::Box* bottom_box_ = nullptr;
    brls::Box* prog_track_ = nullptr;
    brls::Box* prog_fill_ = nullptr;
    brls::Box* footer_row_ = nullptr;
    brls::Label* status_lbl_ = nullptr;
    brls::Button* more_btn_ = nullptr;
    std::function<void()> on_primary_;
    std::function<void()> on_detail_;
};

class BookListCell final : public RecyclingGridCell {
public:
    BookListCell() {
        row_card_ = ui::makeCard("desktop/surface", 12.0f, 10.0f);
        row_card_->setAxis(brls::Axis::ROW);
        row_card_->setWidthPercentage(100);
        row_card_->setHeightPercentage(100);
        row_card_->setAlignItems(brls::AlignItems::CENTER);
        row_card_->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

        left_click_ = new brls::Box(brls::Axis::ROW);
        left_click_->setGrow(1.0f);
        left_click_->setShrink(1.0f);
        left_click_->setAlignItems(brls::AlignItems::CENTER);
        left_click_->setFocusable(true);
        left_click_->setHighlightCornerRadius(8.0f);
        left_click_->registerClickAction([this](brls::View*) {
            if (on_primary_) on_primary_();
            return true;
        });

        thumb_ = new ReusableWeReadCoverView(74.0f, true);
        thumb_->setMarginRight(14.0f);
        left_click_->addView(thumb_);

        auto* meta = new brls::Box(brls::Axis::COLUMN);
        meta->setGrow(1.0f);
        meta->setShrink(1.0f);

        auto* title_row = new brls::Box(brls::Axis::ROW);
        title_row->setAlignItems(brls::AlignItems::CENTER);

        pinned_pill_ = new ReusablePillView();
        pinned_pill_->bind("置顶", "status/ok_bg", "domain/book");
        pinned_pill_->setMarginRight(6.0f);
        title_row->addView(pinned_pill_);

        group_pill_ = new ReusablePillView();
        group_pill_->setMarginRight(6.0f);
        title_row->addView(group_pill_);

        title_lbl_ = ui::makeLabel("", 16.0f, "brls/text");
        title_lbl_->setSingleLine(true);
        title_lbl_->setShrink(1.0f);
        title_row->addView(title_lbl_);
        meta->addView(title_row);

        sub_lbl_ = ui::makeLabel("", 13.5f, "font/grey");
        sub_lbl_->setMarginTop(4.0f);
        sub_lbl_->setSingleLine(true);
        meta->addView(sub_lbl_);

        left_click_->addView(meta);
        row_card_->addView(left_click_);

        right_wrap_ = new brls::Box(brls::Axis::ROW);
        right_wrap_->setShrink(0.0f);
        right_wrap_->setAlignItems(brls::AlignItems::CENTER);
        right_wrap_->setMarginLeft(12.0f);

        batch_pill_ = new ReusablePillView();
        right_wrap_->addView(batch_pill_);

        actions_row_ = new brls::Box(brls::Axis::ROW);
        actions_row_->setAlignItems(brls::AlignItems::CENTER);

        read_btn_ = ui::makeButton("阅读", [this]() {
            if (on_read_) on_read_();
        }, true, 32.0f);
        read_btn_->setMarginRight(6.0f);
        actions_row_->addView(read_btn_);

        nb_btn_ = ui::makeButton("笔记", [this]() {
            if (on_notebook_) on_notebook_();
        }, false, 32.0f);
        nb_btn_->setMarginRight(6.0f);
        actions_row_->addView(nb_btn_);

        detail_btn_ = ui::makeButton("详情 · 分组", [this]() {
            if (on_detail_) on_detail_();
        }, false, 32.0f);
        actions_row_->addView(detail_btn_);

        right_wrap_->addView(actions_row_);
        row_card_->addView(right_wrap_);
        addView(row_card_);
    }

    void prepareForReuse() override {
        on_primary_ = nullptr;
        on_read_ = nullptr;
        on_notebook_ = nullptr;
        on_detail_ = nullptr;
    }

    void bind(const BookWorkbench::ShelfDisplayItem& item,
              bool batch_mode,
              bool is_selected,
              const std::string& group_name,
              std::function<void()> on_primary,
              std::function<void()> on_read,
              std::function<void()> on_notebook,
              std::function<void()> on_detail) {
        on_primary_ = std::move(on_primary);
        on_read_ = std::move(on_read);
        on_notebook_ = std::move(on_notebook);
        on_detail_ = std::move(on_detail);

        row_card_->setBackgroundColor(ui::color(is_selected ? "status/ok_bg" : "desktop/surface"));
        thumb_->bind(item.book, item.entry, 74.0f);

        pinned_pill_->setVisibility(item.entry.pinned ? brls::Visibility::VISIBLE
                                                      : brls::Visibility::GONE);
        if (!group_name.empty()) {
            group_pill_->bind(group_name, "desktop/soft", "font/grey");
            group_pill_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            group_pill_->setVisibility(brls::Visibility::GONE);
        }

        title_lbl_->setText(item.book.title);

        const std::string fmt_up = asciiUpperCopy(item.book.format);
        std::string sub =
            (item.book.author.empty() ? "未知作者" : item.book.author) + " · " + fmt_up +
            " · " + BookLibraryStore::statusDisplayLabel(item.status) +
            " (P." + std::to_string(std::max(1, item.book.current_page)) + "/" +
            std::to_string(std::max(1, item.book.total_pages)) + ", " +
            std::to_string(static_cast<int>(std::round(item.book.progress_percent))) + "%)";
        if (item.excerpt_count > 0) {
            sub += " · 书摘 " + std::to_string(item.excerpt_count) + " 条";
        }
        if (item.book.vocab_count > 0) {
            sub += " · 生词 " + std::to_string(item.book.vocab_count);
        }
        sub_lbl_->setText(sub);

        if (batch_mode) {
            batch_pill_->bind(is_selected ? "已勾选" : "未选",
                              is_selected ? "domain/book" : "desktop/soft",
                              is_selected ? "brls/text" : "font/grey");
            batch_pill_->setVisibility(brls::Visibility::VISIBLE);
            actions_row_->setVisibility(brls::Visibility::GONE);
        } else {
            batch_pill_->setVisibility(brls::Visibility::GONE);
            actions_row_->setVisibility(brls::Visibility::VISIBLE);
        }
    }

private:
    brls::Box* row_card_ = nullptr;
    brls::Box* left_click_ = nullptr;
    ReusableWeReadCoverView* thumb_ = nullptr;
    ReusablePillView* pinned_pill_ = nullptr;
    ReusablePillView* group_pill_ = nullptr;
    brls::Label* title_lbl_ = nullptr;
    brls::Label* sub_lbl_ = nullptr;
    brls::Box* right_wrap_ = nullptr;
    ReusablePillView* batch_pill_ = nullptr;
    brls::Box* actions_row_ = nullptr;
    brls::Button* read_btn_ = nullptr;
    brls::Button* nb_btn_ = nullptr;
    brls::Button* detail_btn_ = nullptr;
    std::function<void()> on_primary_;
    std::function<void()> on_read_;
    std::function<void()> on_notebook_;
    std::function<void()> on_detail_;
};

class BookStageDataSource final : public RecyclingGridDataSource {
public:
    enum class Mode {
        ShelfGrid,
        ShelfList,
        DailyEnglish,
    };

    struct Callbacks {
        std::function<Mode()> get_mode;
        std::function<bool()> is_batch_mode;
        std::function<bool(const std::string&)> is_selected;
        std::function<void(const BookItem&)> on_open_book;
        std::function<void(const std::string&)> on_toggle_batch;
        std::function<void(const BookItem&)> on_open_detail;
        std::function<void(const BookItem&)> on_open_notebook;
    };

    BookStageDataSource(const std::vector<BookWorkbench::ShelfDisplayItem>* shelf_items,
                        const std::vector<BookItem>* english_items,
                        Callbacks cbs)
        : shelf_items_(shelf_items),
          english_items_(english_items),
          cbs_(std::move(cbs)) {}

    size_t getItemCount() override {
        const Mode m = cbs_.get_mode ? cbs_.get_mode() : Mode::ShelfGrid;
        if (m == Mode::DailyEnglish) {
            return english_items_ ? english_items_->size() : 0;
        }
        return shelf_items_ ? shelf_items_->size() : 0;
    }

    RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) override {
        const Mode m = cbs_.get_mode ? cbs_.get_mode() : Mode::ShelfGrid;
        if (m == Mode::DailyEnglish) {
            auto* cell = dynamic_cast<BookGridCell*>(recycler->dequeueReusableCell("book_grid"));
            if (!cell) cell = new BookGridCell();
            if (english_items_ && index < english_items_->size()) {
                const BookItem book = (*english_items_)[index];
                cell->bindDailyEnglish(book, [cbs = cbs_, book]() {
                    if (cbs.on_open_book) cbs.on_open_book(book);
                });
            }
            return cell;
        }

        const bool batch = cbs_.is_batch_mode && cbs_.is_batch_mode();
        if (m == Mode::ShelfList) {
            auto* cell = dynamic_cast<BookListCell*>(recycler->dequeueReusableCell("book_list"));
            if (!cell) cell = new BookListCell();
            if (shelf_items_ && index < shelf_items_->size()) {
                const auto& item = (*shelf_items_)[index];
                const bool sel = cbs_.is_selected && cbs_.is_selected(item.entry.key);
                const std::string gname = BookLibraryStore::instance().groupName(item.entry.group_id);
                const BookItem book = item.book;
                const std::string key = item.entry.key;
                cell->bind(
                    item, batch, sel, gname,
                    [cbs = cbs_, batch, book, key]() {
                        if (batch) {
                            if (cbs.on_toggle_batch) cbs.on_toggle_batch(key);
                        } else {
                            if (cbs.on_open_book) cbs.on_open_book(book);
                        }
                    },
                    [cbs = cbs_, book]() {
                        if (cbs.on_open_book) cbs.on_open_book(book);
                    },
                    [cbs = cbs_, book]() {
                        if (cbs.on_open_notebook) cbs.on_open_notebook(book);
                    },
                    [cbs = cbs_, book]() {
                        if (cbs.on_open_detail) cbs.on_open_detail(book);
                    });
            }
            return cell;
        }

        auto* cell = dynamic_cast<BookGridCell*>(recycler->dequeueReusableCell("book_grid"));
        if (!cell) cell = new BookGridCell();
        if (shelf_items_ && index < shelf_items_->size()) {
            const auto& item = (*shelf_items_)[index];
            const bool sel = cbs_.is_selected && cbs_.is_selected(item.entry.key);
            const std::string gname = BookLibraryStore::instance().groupName(item.entry.group_id);
            const BookItem book = item.book;
            const std::string key = item.entry.key;
            cell->bindShelf(
                item, batch, sel, gname,
                [cbs = cbs_, batch, book, key]() {
                    if (batch) {
                        if (cbs.on_toggle_batch) cbs.on_toggle_batch(key);
                    } else {
                        if (cbs.on_open_book) cbs.on_open_book(book);
                    }
                },
                [cbs = cbs_, book]() {
                    if (cbs.on_open_detail) cbs.on_open_detail(book);
                });
        }
        return cell;
    }

private:
    const std::vector<BookWorkbench::ShelfDisplayItem>* shelf_items_ = nullptr;
    const std::vector<BookItem>* english_items_ = nullptr;
    Callbacks cbs_;
};

class NotebookBookSelectorCell final : public RecyclingGridCell {
public:
    NotebookBookSelectorCell() {
        card_ = ui::makeCard("desktop/soft", 10.0f, 8.0f);
        card_->setWidthPercentage(100);
        card_->setHeightPercentage(100);
        card_->setJustifyContent(brls::JustifyContent::CENTER);
        card_->setFocusable(true);
        card_->registerClickAction([this](brls::View*) {
            if (on_select_) on_select_();
            return true;
        });

        title_lbl_ = ui::makeLabel("", 14.0f, "brls/text");
        title_lbl_->setSingleLine(true);
        card_->addView(title_lbl_);

        sub_lbl_ = ui::makeLabel("", 12.0f, "font/grey");
        sub_lbl_->setMarginTop(2.0f);
        sub_lbl_->setSingleLine(true);
        card_->addView(sub_lbl_);

        addView(card_);
    }

    void prepareForReuse() override { on_select_ = nullptr; }

    void bind(const BookWorkbench::NotebookBookGroup& group,
              bool active,
              std::function<void()> on_select) {
        on_select_ = std::move(on_select);
        card_->setBackgroundColor(ui::color(active ? "status/ok_bg" : "desktop/soft"));
        title_lbl_->setText(group.book.title);
        title_lbl_->setTextColor(ui::color(active ? "domain/book" : "brls/text"));
        sub_lbl_->setText("书摘 " + std::to_string(group.excerpt_count) +
                          " · 生词 " + std::to_string(group.vocab_count));
    }

private:
    brls::Box* card_ = nullptr;
    brls::Label* title_lbl_ = nullptr;
    brls::Label* sub_lbl_ = nullptr;
    std::function<void()> on_select_;
};

class BookNotebookBooksDataSource final : public RecyclingGridDataSource {
public:
    BookNotebookBooksDataSource(const std::vector<BookWorkbench::NotebookBookGroup>* groups,
                                std::string active_key,
                                std::function<void(const std::string&)> on_select)
        : groups_(groups),
          active_key_(std::move(active_key)),
          on_select_(std::move(on_select)) {}

    size_t getItemCount() override { return groups_ ? groups_->size() : 0; }

    RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) override {
        auto* cell =
            dynamic_cast<NotebookBookSelectorCell*>(recycler->dequeueReusableCell("nb_book"));
        if (!cell) cell = new NotebookBookSelectorCell();
        if (groups_ && index < groups_->size()) {
            const auto& g = (*groups_)[index];
            const std::string key = g.key;
            cell->bind(g, !active_key_.empty() && active_key_ == key, [cb = on_select_, key]() {
                if (cb) cb(key);
            });
        }
        return cell;
    }

private:
    const std::vector<BookWorkbench::NotebookBookGroup>* groups_ = nullptr;
    std::string active_key_;
    std::function<void(const std::string&)> on_select_;
};

class NotebookExcerptCell final : public RecyclingGridCell {
public:
    NotebookExcerptCell() {
        card_ = ui::makeCard("desktop/soft", 14.0f, 10.0f);
        card_->setWidthPercentage(100);
        card_->setHeightPercentage(100);
        card_->setClipsToBounds(true);

        auto* top_row = new brls::Box(brls::Axis::ROW);
        top_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        top_row->setAlignItems(brls::AlignItems::CENTER);
        top_row->setShrink(0.0f);

        loc_lbl_ = ui::makeLabel("", 13.0f, "domain/book");
        loc_lbl_->setSingleLine(true);
        loc_lbl_->setShrink(1.0f);
        top_row->addView(loc_lbl_);

        auto* btns = new brls::Box(brls::Axis::ROW);
        btns->setShrink(0.0f);

        jmp_btn_ = ui::makeButton("跳转原文", [this]() {
            if (on_jump_) on_jump_();
        }, false, 26.0f);
        jmp_btn_->setMarginRight(6.0f);
        btns->addView(jmp_btn_);

        del_btn_ = ui::makeButton("删除", [this]() {
            if (on_delete_) on_delete_();
        }, false, 26.0f);
        btns->addView(del_btn_);

        top_row->addView(btns);
        card_->addView(top_row);

        quote_lbl_ = ui::makeLabel("", 15.5f, "brls/text", 1.52f);
        quote_lbl_->setMarginTop(8.0f);
        card_->addView(quote_lbl_);

        thought_lbl_ = ui::makeLabel("", 14.0f, "domain/book", 1.48f);
        thought_lbl_->setMarginTop(6.0f);
        card_->addView(thought_lbl_);

        ai_md_ = ui::makeMarkdown("", 13.5f, "font/grey", 1.48f);
        ai_md_->setMarginTop(8.0f);
        card_->addView(ai_md_);

        addView(card_);
    }

    void prepareForReuse() override {
        on_jump_ = nullptr;
        on_delete_ = nullptr;
    }

    void bind(const BookExcerpt& ex,
              std::function<void()> on_jump,
              std::function<void()> on_delete) {
        on_jump_ = std::move(on_jump);
        on_delete_ = std::move(on_delete);

        std::string loc = ex.book_title;
        if (!ex.chapter_title.empty()) loc += " · " + ex.chapter_title;
        if (ex.page_number > 0) loc += " · P." + std::to_string(ex.page_number);
        loc_lbl_->setText(loc);

        const bool can_jump = !ex.book_id.empty() || !ex.file_path.empty();
        jmp_btn_->setVisibility(can_jump ? brls::Visibility::VISIBLE : brls::Visibility::GONE);

        const bool can_del = ex.id.rfind("ebook-note:", 0) != 0;
        del_btn_->setVisibility(can_del ? brls::Visibility::VISIBLE : brls::Visibility::GONE);

        if (!ex.quote_text.empty()) {
            quote_lbl_->setText("「" + ex.quote_text + "」");
            quote_lbl_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            quote_lbl_->setVisibility(brls::Visibility::GONE);
        }

        if (!ex.thought_text.empty()) {
            thought_lbl_->setText("想法: " + ex.thought_text);
            thought_lbl_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            thought_lbl_->setVisibility(brls::Visibility::GONE);
        }

        if (!ex.ai_analysis.empty()) {
            ai_md_->setMarkdown(ex.ai_analysis);
            ai_md_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            if (!ai_md_->markdown().empty()) ai_md_->setMarkdown("");
            ai_md_->setVisibility(brls::Visibility::GONE);
        }
    }

private:
    brls::Box* card_ = nullptr;
    brls::Label* loc_lbl_ = nullptr;
    brls::Button* jmp_btn_ = nullptr;
    brls::Button* del_btn_ = nullptr;
    brls::Label* quote_lbl_ = nullptr;
    brls::Label* thought_lbl_ = nullptr;
    MarkdownView* ai_md_ = nullptr;
    std::function<void()> on_jump_;
    std::function<void()> on_delete_;
};

class NotebookVocabCell final : public RecyclingGridCell {
public:
    NotebookVocabCell() {
        card_ = ui::makeCard("desktop/soft", 12.0f, 10.0f);
        card_->setWidthPercentage(100);
        card_->setHeightPercentage(100);
        card_->setClipsToBounds(true);
        card_->setFocusable(true);

        word_lbl_ = ui::makeLabel("", 15.5f, "domain/book");
        word_lbl_->setSingleLine(true);
        card_->addView(word_lbl_);

        meaning_lbl_ = ui::makeLabel("", 14.0f, "brls/text");
        meaning_lbl_->setMarginTop(4.0f);
        card_->addView(meaning_lbl_);

        ctx_lbl_ = ui::makeLabel("", 13.0f, "font/grey", 1.44f);
        ctx_lbl_->setMarginTop(4.0f);
        card_->addView(ctx_lbl_);

        addView(card_);
    }

    void bind(const VocabCard& vc) {
        word_lbl_->setText(
            vc.word + (vc.phonetic.empty() ? "" : ("  " + vc.phonetic)) +
            "  · 间隔 " + std::to_string(vc.interval_days) + " 天");

        if (!vc.meaning.empty()) {
            meaning_lbl_->setText(vc.meaning);
            meaning_lbl_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            meaning_lbl_->setVisibility(brls::Visibility::GONE);
        }

        if (!vc.context_sentence.empty()) {
            ctx_lbl_->setText("原句: " + vc.context_sentence);
            ctx_lbl_->setVisibility(brls::Visibility::VISIBLE);
        } else {
            ctx_lbl_->setVisibility(brls::Visibility::GONE);
        }
    }

private:
    brls::Box* card_ = nullptr;
    brls::Label* word_lbl_ = nullptr;
    brls::Label* meaning_lbl_ = nullptr;
    brls::Label* ctx_lbl_ = nullptr;
};

class BookNotebookDataSource final : public RecyclingGridDataSource {
public:
    BookNotebookDataSource(int sub_tab,
                           const std::vector<BookExcerpt>* excerpts,
                           const std::vector<VocabCard>* vocab,
                           std::function<void(const BookExcerpt&)> on_jump,
                           std::function<void(const std::string&)> on_delete)
        : sub_tab_(sub_tab),
          excerpts_(excerpts),
          vocab_(vocab),
          on_jump_(std::move(on_jump)),
          on_delete_(std::move(on_delete)) {}

    size_t getItemCount() override {
        if (sub_tab_ == 0) return excerpts_ ? excerpts_->size() : 0;
        return vocab_ ? vocab_->size() : 0;
    }

    float heightForRow(RecyclingGrid* recycler, size_t index) override {
        const float inner_w = std::max(260.0f, recycler->cellColumnWidth() - 32.0f);
        if (sub_tab_ == 0) {
            if (!excerpts_ || index >= excerpts_->size()) return 120.0f;
            const auto& ex = (*excerpts_)[index];
            float h = 58.0f;  // card padding + header row
            if (!ex.quote_text.empty()) {
                const size_t q_lines = estimateWrappedLines("「" + ex.quote_text + "」", inner_w, 15.5f);
                h += 8.0f + static_cast<float>(q_lines) * 24.5f;
            }
            if (!ex.thought_text.empty()) {
                const size_t t_lines = estimateWrappedLines("想法: " + ex.thought_text, inner_w, 14.0f);
                h += 6.0f + static_cast<float>(t_lines) * 21.5f;
            }
            if (!ex.ai_analysis.empty()) {
                const size_t a_lines = estimateWrappedLines(ex.ai_analysis, inner_w, 13.5f);
                h += 14.0f + static_cast<float>(a_lines) * 21.5f;
            }
            return std::max(72.0f, h);
        }
        if (!vocab_ || index >= vocab_->size()) return 92.0f;
        const auto& vc = (*vocab_)[index];
        float h = 48.0f;
        if (!vc.meaning.empty()) {
            h += 4.0f + static_cast<float>(estimateWrappedLines(vc.meaning, inner_w, 14.0f)) * 21.0f;
        }
        if (!vc.context_sentence.empty()) {
            h += 4.0f + static_cast<float>(
                estimateWrappedLines("原句: " + vc.context_sentence, inner_w, 13.0f)) * 20.0f;
        }
        return std::max(64.0f, h);
    }

    RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) override {
        if (sub_tab_ == 0) {
            auto* cell =
                dynamic_cast<NotebookExcerptCell*>(recycler->dequeueReusableCell("nb_excerpt"));
            if (!cell) cell = new NotebookExcerptCell();
            if (excerpts_ && index < excerpts_->size()) {
                const BookExcerpt ex = (*excerpts_)[index];
                cell->bind(
                    ex,
                    [cb = on_jump_, ex]() {
                        if (cb) cb(ex);
                    },
                    [cb = on_delete_, id = ex.id]() {
                        if (cb) cb(id);
                    });
            }
            return cell;
        }

        auto* cell = dynamic_cast<NotebookVocabCell*>(recycler->dequeueReusableCell("nb_vocab"));
        if (!cell) cell = new NotebookVocabCell();
        if (vocab_ && index < vocab_->size()) {
            cell->bind((*vocab_)[index]);
        }
        return cell;
    }

private:
    int sub_tab_ = 0;
    const std::vector<BookExcerpt>* excerpts_ = nullptr;
    const std::vector<VocabCard>* vocab_ = nullptr;
    std::function<void(const BookExcerpt&)> on_jump_;
    std::function<void(const std::string&)> on_delete_;
};

} // namespace

BookWorkbench::BookWorkbench(std::function<void(const std::string&)> on_entity_focus,
                             std::function<void()> on_open_marketplace)
    : brls::Box(brls::Axis::COLUMN),
      on_entity_focus_(std::move(on_entity_focus)),
      on_open_marketplace_(std::move(on_open_marketplace)) {
    setGrow(1.0f);
    setPadding(22, 24, 18, 24);
    setBackgroundColor(ui::color("brls/background"));

    auto* header = new brls::Box(brls::Axis::ROW);
    header->setId("omnidesk/pane_header");
    header->setShrink(0.0f);
    header->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    header->setAlignItems(brls::AlignItems::CENTER);
    header->setMarginBottom(12);

    auto* left = new brls::Box(brls::Axis::COLUMN);
    left->setShrink(1.0f);
    left->addView(ui::makeLabel("书库与深度伴读 · WeRead Library", 24.0f, "brls/text"));
    status_label_ = ui::makeLabel("微信读书式书架分组 · 真实封面提取 · 书摘与想法笔记本 · BabelDOC 双栏 · SM-2 生词本",
                                   14.5f, "font/grey");
    status_label_->setMarginTop(3);
    left->addView(status_label_);
    header->addView(left);

    auto* right = new brls::Box(brls::Axis::ROW);
    right->setShrink(0.0f);
    right->setAlignItems(brls::AlignItems::CENTER);
    wrapFlexRow(right);
    date_filter_btn_ = ui::makeButton("日历查找", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptDateFilter();
    }, false, 35.0f);
    date_filter_btn_->setMarginRight(8);
    right->addView(date_filter_btn_);

    auto* imp_btn = ui::makeButton("+ 导入书籍", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptImportLocalBook();
    }, false, 35.0f);
    imp_btn->setMarginRight(8);
    right->addView(imp_btn);

    auto* s_btn = ui::makeButton("检索书库 /", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptSearch();
    }, false, 35.0f);
    s_btn->setMarginRight(8);
    right->addView(s_btn);
    right->addView(ui::makeButton("阅读插件管理", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire) && on_open_marketplace_) on_open_marketplace_();
    }, false, 35.0f));
    header->addView(right);
    addView(header);

    sub_tabs_box_ = new brls::Box(brls::Axis::ROW);
    sub_tabs_box_->setId("omnidesk/pane_filters");
    sub_tabs_box_->setShrink(0.0f);
    wrapFlexRow(sub_tabs_box_);
    sub_tabs_box_->setMarginBottom(12);
    addView(sub_tabs_box_);

    stage_grid_ = new RecyclingGrid();
    stage_grid_->setId("omnidesk/pane_primary");
    stage_grid_->setGrow(1.0f);
    stage_grid_->setShrink(1.0f);
    stage_grid_->setMinHeight(0);
    stage_grid_->setWidthPercentage(100);
    stage_grid_->setColSpace(14.0f);
    stage_grid_->setEstimatedRowSpace(14.0f);
    stage_grid_->setEstimatedRowHeight(376.0f);
    stage_grid_->registerCell("book_grid", []() { return new BookGridCell(); });
    stage_grid_->registerCell("book_list", []() { return new BookListCell(); });

    BookStageDataSource::Callbacks stage_cbs;
    stage_cbs.get_mode = [this]() {
        if (sub_mode_ == SubMode::DailyEnglish) return BookStageDataSource::Mode::DailyEnglish;
        if (shelf_view_mode_ == ShelfViewMode::List) return BookStageDataSource::Mode::ShelfList;
        return BookStageDataSource::Mode::ShelfGrid;
    };
    stage_cbs.is_batch_mode = [this]() { return batch_mode_; };
    stage_cbs.is_selected = [this](const std::string& key) {
        return batch_selected_keys_.count(key) > 0;
    };
    stage_cbs.on_open_book = [this, alive = alive_](const BookItem& book) {
        brls::sync([this, alive, book]() {
            if (alive->load(std::memory_order_acquire)) openBook(book);
        });
    };
    stage_cbs.on_toggle_batch = [this, alive = alive_](const std::string& key) {
        brls::sync([this, alive, key]() {
            if (alive->load(std::memory_order_acquire)) toggleBatchSelection(key);
        });
    };
    stage_cbs.on_open_detail = [this, alive = alive_](const BookItem& book) {
        brls::sync([this, alive, book]() {
            if (alive->load(std::memory_order_acquire)) openBookDetailModal(book);
        });
    };
    stage_cbs.on_open_notebook = [this, alive = alive_](const BookItem& book) {
        brls::sync([this, alive, book]() {
            if (!alive->load(std::memory_order_acquire)) return;
            notebook_book_key_ = BookLibraryStore::computeBookKey(book);
            setSubMode(SubMode::Notebook);
        });
    };
    stage_grid_->setDataSource(
        new BookStageDataSource(&visible_shelf_items_, &visible_english_items_, std::move(stage_cbs)));
    addView(stage_grid_);

    resize_sub_ = brls::Application::getWindowSizeChangedEvent()->subscribe([this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) {
            onViewportLayoutChanged();
        }
    });

    refresh();

    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        download_sub_token_ = k.bus().subscribe("entity.book.downloaded", "host.book_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
        covers_sub_token_ = k.bus().subscribe("entity.book.covers_updated", "host.book_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
        recommend_sub_token_ = k.bus().subscribe("entity.recommend.updated", "host.book_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
        books_changed_sub_token_ = k.bus().subscribe("domain.books.changed", "host.book_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (alive->load(std::memory_order_acquire)) {
                        refresh();
                    }
                });
            });
        library_sub_token_ = k.bus().subscribe("entity.book.library_changed", "host.book_workbench",
            [this, alive = alive_](const std::string&, const nlohmann::json&) {
                brls::sync([this, alive]() {
                    if (!alive->load(std::memory_order_acquire) ||
                        suppress_library_bus_.load(std::memory_order_acquire)) {
                        return;
                    }
                    BookLibraryStore::instance().reload();
                    cached_excerpts_ = BookLibraryStore::instance().listAllMergedExcerpts();
                    if (sub_mode_ == SubMode::Bookshelf || sub_mode_ == SubMode::Notebook) {
                        refresh();
                    }
                });
            });
    }
}

BookWorkbench::~BookWorkbench() {
    brls::Application::getWindowSizeChangedEvent()->unsubscribe(resize_sub_);
    alive_->store(false, std::memory_order_release);
    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        if (download_sub_token_ != 0) k.bus().unsubscribe(download_sub_token_);
        if (covers_sub_token_ != 0) k.bus().unsubscribe(covers_sub_token_);
        if (recommend_sub_token_ != 0) k.bus().unsubscribe(recommend_sub_token_);
        if (books_changed_sub_token_ != 0) k.bus().unsubscribe(books_changed_sub_token_);
        if (library_sub_token_ != 0) k.bus().unsubscribe(library_sub_token_);
    }
    download_sub_token_ = 0;
    covers_sub_token_ = 0;
    recommend_sub_token_ = 0;
    books_changed_sub_token_ = 0;
    library_sub_token_ = 0;
    library_request_->cancel();
    spread_request_->cancel();
    pdf_page_request_->cancel();
    action_request_->cancel();
    cover_request_->cancel();
}

void BookWorkbench::onViewportLayoutChanged() {
    if (getVisibility() != brls::Visibility::VISIBLE ||
        (sub_mode_ != SubMode::Bookshelf && sub_mode_ != SubMode::DailyEnglish)) {
        return;
    }
    if (sub_mode_ == SubMode::Bookshelf && shelf_view_mode_ != ShelfViewMode::Grid) {
        return;
    }
    const int cols = computeBookshelfColumns(estimateBookStageWidth(this));
    if (cols != last_bookshelf_cols_) {
        rebuildContent();
    }
}

void BookWorkbench::refresh() {
    library_request_->cancel();
    library_request_ = CancelToken::create();
    auto& kernel = HostKernel::instance();
    const uint64_t vocab_seq_before_load = vocab_mutation_seq_;
    const std::vector<VocabCard> vocab_snapshot = vocab_cards_;
    books_.clear();
    preview_books_.clear();

    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().bookSources();
        if (!sources.empty()) {
            status_label_->setText("正在同步书库、分组与书摘笔记…");
            finishRefresh();
            loadUiInBackground(kernel.uiTasks(), "book.library", library_request_,
                [sources](const auto& cancel) {
                    struct LoadedPayload {
                        std::vector<BookItem> books;
                        std::vector<VocabCard> vocab;
                        std::vector<BookExcerpt> excerpts;
                    };
                    LoadedPayload payload;
                    for (const auto& source : sources) {
                        if (cancel->isCancelled()) break;
                        auto books = source->listBooks("", "");
                        for (auto& book : books) {
                            if (book.source_id.empty()) book.source_id = source->sourceId();
                        }
                        payload.books.insert(payload.books.end(), books.begin(), books.end());
                        if (cancel->isCancelled()) break;
                        auto vocab = source->listVocab();
                        payload.vocab.insert(payload.vocab.end(), vocab.begin(), vocab.end());
                    }
                    if (!cancel->isCancelled()) {
                        BookLibraryStore::instance().reload();
                        BookLibraryStore::instance().enrichAndMergeLibraryBooks(payload.books);
                        payload.excerpts = BookLibraryStore::instance().listAllMergedExcerpts();
                    }
                    return payload;
                },
                [this, alive = alive_, sources, vocab_snapshot, vocab_seq_before_load](auto payload) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    books_ = std::move(payload.books);
                    if (vocab_mutation_seq_ == vocab_seq_before_load) {
                        vocab_cards_ = std::move(payload.vocab);
                    } else {
                        mergeRefreshedVocab(vocab_cards_, std::move(payload.vocab), vocab_snapshot);
                    }
                    cached_excerpts_ = std::move(payload.excerpts);
                    mergeHostVocabFallback();

                    size_t shelf_count = 0;
                    size_t reading_count = 0;
                    for (const auto& b : books_) {
                        if (b.source_id == "plugin-ai-curator") continue;
                        ++shelf_count;
                        const auto entry = BookLibraryStore::instance().entryForBook(b);
                        if (BookLibraryStore::effectiveStatus(b, entry) == BookReadingStatus::Reading) {
                            ++reading_count;
                        }
                    }
                    const bool has_library_source = std::any_of(sources.begin(), sources.end(), [](const auto& source) {
                        return source->sourceId() != "plugin-ai-curator";
                    });
                    if (!has_library_source && shelf_count == 0) {
                        preview_books_ = buildShowcaseBooks();
                        books_.insert(books_.end(), preview_books_.begin(), preview_books_.end());
                        shelf_count = preview_books_.size();
                    }
                    status_label_->setText(
                        "藏书 " + std::to_string(shelf_count) + " 本 · 在读 " +
                        std::to_string(reading_count) + " 本 · 书摘笔记 " +
                        std::to_string(cached_excerpts_.size()) + " 条 · SM-2 生词 " +
                        std::to_string(vocab_cards_.size()) + " 个");
                    finishRefresh();
                    scheduleMissingPdfCovers();
                });
            return;
        }
    }

    {
        BookLibraryStore::instance().reload();
        BookLibraryStore::instance().enrichAndMergeLibraryBooks(books_);
        cached_excerpts_ = BookLibraryStore::instance().listAllMergedExcerpts();
        if (books_.empty()) {
            preview_books_ = buildShowcaseBooks();
            books_ = preview_books_;
        }
        vocab_cards_ = buildShowcaseVocab();
        mergeHostVocabFallback();
        status_label_->setText("藏书 " + std::to_string(books_.size()) + " 本 · 书摘笔记 " +
                               std::to_string(cached_excerpts_.size()) + " 条");
    }

    finishRefresh();
    scheduleMissingPdfCovers();
}

void BookWorkbench::scheduleMissingPdfCovers() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    std::vector<std::string> missing_pdfs;
    for (const auto& b : books_) {
        if (b.source_id == "plugin-ai-curator") continue;
        if (toLowerCopy(b.format) != "pdf") continue;
        if (!b.cover_path.empty()) {
            std::error_code ec;
            if (std::filesystem::exists(b.cover_path, ec)) continue;
        }
        if (b.file_path.empty()) continue;
        std::error_code ec;
        if (!std::filesystem::exists(b.file_path, ec)) continue;
        if (std::find(missing_pdfs.begin(), missing_pdfs.end(), b.file_path) == missing_pdfs.end()) {
            missing_pdfs.push_back(b.file_path);
            if (missing_pdfs.size() >= 20) break;
        }
    }
    if (missing_pdfs.empty()) return;

    cover_request_->cancel();
    cover_request_ = CancelToken::create();
    auto req = cover_request_;
    const std::string covers_dir = BookLibraryStore::instance().coversDirPath();

    loadInBackground(kernel.jobs(), "提取 PDF 书封与页数", req,
        [missing_pdfs, covers_dir](const auto& cancel) {
            struct ExtractedItem {
                std::string pdf_path;
                std::string cover_path;
                int total_pages = 0;
            };
            std::vector<ExtractedItem> results;
            for (const auto& pdf : missing_pdfs) {
                if (cancel->isCancelled()) break;
                std::string out_cover;
                int out_pages = 0;
                if (BookLibraryStore::extractPdfCoverAndPagesSync(pdf, covers_dir, out_cover, out_pages)) {
                    results.push_back({pdf, out_cover, out_pages});
                }
            }
            return results;
        },
        [this, alive = alive_, req](auto results) {
            if (!alive->load(std::memory_order_acquire) || cover_request_ != req) return;
            if (results.empty()) return;
            beginLibraryBusSuppression();
            for (const auto& r : results) {
                BookLibraryStore::instance().updateCachedCoverAndPages(r.pdf_path, r.cover_path, r.total_pages);
            }
            endLibraryBusSuppression();
            BookLibraryStore::instance().enrichAndMergeLibraryBooks(books_);
            if (sub_mode_ == SubMode::Bookshelf || sub_mode_ == SubMode::Notebook) {
                rebuildContent();
            }
        });
}

void BookWorkbench::finishRefresh() {
    english_latest_date_.clear();
    size_t shelf_book_count = 0;
    for (const auto& book : books_) {
        if (book.source_id == "plugin-ai-curator") {
            if (book.id.rfind("english-date-", 0) == 0 && book.id.size() == 23) {
                english_latest_date_ = std::max(english_latest_date_, book.id.substr(13));
            }
        } else {
            ++shelf_book_count;
        }
    }
    if (date_filter_btn_) {
        date_filter_btn_->setVisibility(sub_mode_ == SubMode::DailyEnglish ? brls::Visibility::VISIBLE : brls::Visibility::GONE);
        date_filter_btn_->setText(english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : "英语日历");
    }
    sub_tabs_box_->clearViews();
    struct Tab {
        SubMode m;
        std::string label;
    };
    const Tab tabs[] = {
        {SubMode::Bookshelf, "书架 Library (" + std::to_string(shelf_book_count) + ")"},
        {SubMode::Notebook, "笔记本 · 书摘 (" + std::to_string(cached_excerpts_.size()) + ")"},
        {SubMode::DailyEnglish, "每日英语"},
        {SubMode::PdfBabelDoc, "PDF 双栏精读"},
        {SubMode::EpubCompanion, "电子书伴读"},
        {SubMode::VocabReview, "SM-2 生词本 (" + std::to_string(vocab_cards_.size()) + ")"},
    };
    for (const auto& t : tabs) {
        auto* btn = ui_comfort::makeFilterChip(t.label, [this, alive = alive_, m = t.m]() {
            brls::sync([this, alive, m]() {
                if (alive->load(std::memory_order_acquire)) setSubMode(m);
            });
        }, sub_mode_ == t.m, "domain/book", 32.0f);
        btn->setMarginRight(7);
        sub_tabs_box_->addView(btn);
    }

    rebuildContent();
}

void BookWorkbench::setSubMode(SubMode m) {
    if (sub_mode_ == m) return;
    sub_mode_ = m;
    reset_stage_scroll_next_ = true;
    finishRefresh();
}

void BookWorkbench::openBookDetailModal(const BookItem& book) {
    openWeReadBookDetailDialog(
        book, vocab_cards_,
        [this, alive = alive_](const BookItem& target, int page) {
            if (!alive->load(std::memory_order_acquire)) return;
            BookItem copy = target;
            copy.current_page = std::max(1, page);
            openBook(copy);
        },
        [this, alive = alive_](const BookItem& target) {
            if (!alive->load(std::memory_order_acquire)) return;
            notebook_book_key_ = BookLibraryStore::computeBookKey(target);
            setSubMode(SubMode::Notebook);
        },
        [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            refresh();
        });
}

std::vector<BookItem> BookWorkbench::collectBatchSelectedBooks() const {
    std::vector<BookItem> selected;
    auto& store = BookLibraryStore::instance();
    for (const auto& b : books_) {
        if (b.source_id == "plugin-ai-curator") continue;
        const auto entry = store.entryForBook(b);
        if (entry.removed_from_shelf) continue;
        if (batch_selected_keys_.count(entry.key) > 0) {
            selected.push_back(b);
        }
    }
    return selected;
}

void BookWorkbench::toggleBatchSelection(const std::string& key) {
    if (key.empty()) return;
    if (batch_selected_keys_.count(key) > 0) {
        batch_selected_keys_.erase(key);
    } else {
        batch_selected_keys_.insert(key);
    }
    if (batch_count_label_) {
        const size_t n = collectBatchSelectedBooks().size();
        batch_count_label_->setText(
            "批量整理模式 · 已勾选 " + std::to_string(n) + " 本（点击卡片即可勾选/取消）");
    }
    if (stage_grid_) {
        stage_grid_->reloadData(false);
    }
}

void BookWorkbench::rebuildContent() {
    if (!stage_grid_) return;
    const bool had_focus = focusIsInsideBookView(stage_grid_);
    if (had_focus) {
        brls::Application::giveFocus(nullptr);
    }
    batch_count_label_ = nullptr;
    notebook_books_grid_ = nullptr;
    notebook_grid_ = nullptr;

    std::error_code pdf_ec;
    const bool has_local_pdf =
        sub_mode_ == SubMode::PdfBabelDoc && has_active_book_ &&
        !active_book_.file_path.empty() &&
        std::filesystem::is_regular_file(active_book_.file_path, pdf_ec);

    if ((sub_mode_ == SubMode::PdfBabelDoc || sub_mode_ == SubMode::EpubCompanion) &&
        (!has_active_book_ || (active_spread_.blocks.empty() && !has_local_pdf))) {
        auto* empty_card = ui::makeCard("desktop/surface", 20.0f, 12.0f);
        empty_card->addView(ui::makeLabel(
            has_active_book_ ? "书页正在准备或尚未加载，可重试加载或返回书架。"
                             : "尚未打开正在阅读的书籍，请先从「书架 Library」选择一本图书或导入本地文件。",
            15.5f, "font/grey"));
        auto* act_row = new brls::Box(brls::Axis::ROW);
        act_row->setMarginTop(12);
        auto* back_btn = ui::makeButton("返回书架 Library", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) setSubMode(SubMode::Bookshelf);
        }, true, 35.0f);
        back_btn->setMarginRight(8);
        act_row->addView(back_btn);
        if (has_active_book_) {
            act_row->addView(ui::makeButton("重试加载书页", [this, alive = alive_]() {
                if (!alive->load(std::memory_order_acquire)) return;
                loadSpread(active_spread_.page_number);
                rebuildContent();
            }, false, 35.0f));
        }
        empty_card->addView(act_row);
        stage_grid_->setStaticContent(empty_card);
        reset_stage_scroll_next_ = false;
        if (had_focus) {
            if (auto* df = stage_grid_->getDefaultFocus()) brls::Application::giveFocus(df);
        }
        return;
    }

    switch (sub_mode_) {
        case SubMode::Bookshelf: renderBookshelf(); break;
        case SubMode::Notebook: renderNotebook(); break;
        case SubMode::DailyEnglish: renderDailyEnglishShelf(); break;
        case SubMode::PdfBabelDoc: renderPdfBabelDoc(); break;
        case SubMode::EpubCompanion: renderEpubCompanion(); break;
        case SubMode::VocabReview: renderVocabReview(); break;
    }
    reset_stage_scroll_next_ = false;

    if (had_focus && brls::Application::getCurrentFocus() == nullptr) {
        if (auto* df = stage_grid_->getDefaultFocus()) {
            brls::Application::giveFocus(df);
        }
    }
}

void BookWorkbench::renderDailyEnglishShelf() {
    auto* dates = new brls::Box(brls::Axis::ROW);
    wrapFlexRow(dates);
    dates->setMarginBottom(12);
    auto* latest = ui_comfort::makeFilterChip(
        "最新一期" + (english_latest_date_.empty() ? std::string{} : " · " + english_latest_date_),
        [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            english_date_mode_ = EnglishDateMode::Latest;
            reset_stage_scroll_next_ = true;
            finishRefresh();
        },
        english_date_mode_ == EnglishDateMode::Latest, "domain/book", 32.0f);
    latest->setMarginRight(8);
    dates->addView(latest);
    auto* selected = ui_comfort::makeFilterChip(
        english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : "选择日期",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) promptDateFilter();
        },
        english_date_mode_ == EnglishDateMode::Selected, "domain/book", 32.0f);
    selected->setMarginRight(8);
    dates->addView(selected);
    auto* all_btn = ui_comfort::makeFilterChip("全部归档", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        english_date_mode_ = EnglishDateMode::All;
        reset_stage_scroll_next_ = true;
        finishRefresh();
    }, english_date_mode_ == EnglishDateMode::All, "domain/book", 32.0f);
    dates->addView(all_btn);

    const float stage_w = estimateBookStageWidth(this);
    const int cols = computeBookshelfColumns(stage_w);
    last_bookshelf_cols_ = cols;

    visible_english_items_.clear();
    const std::string q_low = toLowerCopy(english_search_query_);
    const std::string date =
        english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : english_latest_date_;

    for (const auto& book : books_) {
        if (book.source_id != "plugin-ai-curator") continue;
        if (english_date_mode_ != EnglishDateMode::All &&
            (date.empty() || book.id != "english-date-" + date)) continue;
        if (!q_low.empty() &&
            toLowerCopy(book.title).find(q_low) == std::string::npos &&
            toLowerCopy(book.author).find(q_low) == std::string::npos) continue;
        visible_english_items_.push_back(book);
    }

    stage_grid_->setHeaderView(dates);
    stage_grid_->setFooterView(nullptr);
    if (visible_english_items_.empty()) {
        stage_grid_->setEmptyView(
            ui::makeLabel("此日期暂无英语课程，可选择其他日期或查看全部归档。", 15.0f, "font/grey"));
    } else {
        stage_grid_->setEmptyView(nullptr);
    }
    stage_grid_->setSpanCount(cols);
    stage_grid_->setColSpace(14.0f);
    stage_grid_->setEstimatedRowSpace(14.0f);
    stage_grid_->setEstimatedRowHeight(326.0f);
    stage_grid_->reloadData(reset_stage_scroll_next_);
}

void BookWorkbench::renderBookshelf() {
    auto& store = BookLibraryStore::instance();
    const auto groups = store.listGroups();

    std::vector<ShelfDisplayItem> all_shelf;
    size_t count_reading = 0;
    size_t count_unread = 0;
    size_t count_finished = 0;

    for (const auto& b : books_) {
        if (b.source_id == "plugin-ai-curator") continue;
        ShelfDisplayItem item;
        item.book = b;
        item.entry = store.entryForBook(b);
        if (item.entry.removed_from_shelf) continue;
        item.status = BookLibraryStore::effectiveStatus(b, item.entry);
        item.excerpt_count = store.countExcerptsForBook(b, cached_excerpts_);
        if (item.status == BookReadingStatus::Reading) ++count_reading;
        else if (item.status == BookReadingStatus::Finished) ++count_finished;
        else ++count_unread;
        all_shelf.push_back(std::move(item));
    }

    auto* header_wrap = new brls::Box(brls::Axis::COLUMN);
    header_wrap->setWidthPercentage(100);

    // 1. Top "最近阅读 · Continue Reading" strip (WeRead header shelf)
    if (!batch_mode_ && search_query_.empty() && !all_shelf.empty()) {
        std::vector<ShelfDisplayItem> recent = all_shelf;
        std::sort(recent.begin(), recent.end(), [](const ShelfDisplayItem& a, const ShelfDisplayItem& b) {
            if (a.entry.last_read_at_ms != b.entry.last_read_at_ms) {
                return a.entry.last_read_at_ms > b.entry.last_read_at_ms;
            }
            return a.book.progress_percent > b.book.progress_percent;
        });

        std::vector<ShelfDisplayItem> active_recent;
        for (const auto& r : recent) {
            if (r.entry.last_read_at_ms > 0 || r.book.progress_percent > 0.5 || r.book.current_page > 1) {
                active_recent.push_back(r);
                if (active_recent.size() >= 3) break;
            }
        }

        if (!active_recent.empty()) {
            auto* recent_banner = ui::makeCard("desktop/surface", 14.0f, 12.0f);
            recent_banner->setMarginBottom(14);

            auto* banner_hdr = new brls::Box(brls::Axis::ROW);
            banner_hdr->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            banner_hdr->setAlignItems(brls::AlignItems::CENTER);
            banner_hdr->setMarginBottom(10);
            banner_hdr->addView(ui::makeLabel("最近阅读 · Continue Reading", 16.0f, "brls/text"));

            auto* banner_right = new brls::Box(brls::Axis::ROW);
            banner_right->setAlignItems(brls::AlignItems::CENTER);
            auto* nb_jump = ui::makeButton(
                "笔记本 · 书摘 (" + std::to_string(cached_excerpts_.size()) + ")",
                [this, alive = alive_]() {
                    if (alive->load(std::memory_order_acquire)) setSubMode(SubMode::Notebook);
                }, false, 30.0f);
            banner_right->addView(nb_jump);
            banner_hdr->addView(banner_right);
            recent_banner->addView(banner_hdr);

            auto* recent_row = new brls::Box(brls::Axis::ROW);
            recent_row->setWidthPercentage(100);
            for (size_t i = 0; i < active_recent.size(); ++i) {
                const auto& r = active_recent[i];
                auto* r_card = ui::makeCard("desktop/soft", 10.0f, 10.0f);
                r_card->setAxis(brls::Axis::ROW);
                r_card->setWidth(0);
                r_card->setGrow(1.0f);
                r_card->setShrink(1.0f);
                r_card->setAlignItems(brls::AlignItems::CENTER);
                if (i + 1 < active_recent.size()) r_card->setMarginRight(12);
                r_card->setFocusable(true);

                auto* mini_cov = makeWeReadCoverBox(r.book, r.entry, 76.0f, true);
                mini_cov->setMarginRight(10);
                r_card->addView(mini_cov);

                auto* r_meta = new brls::Box(brls::Axis::COLUMN);
                r_meta->setGrow(1.0f);
                r_meta->setShrink(1.0f);
                auto* rt = ui::makeLabel(clipTitleUtf8(r.book.title, 48), 14.5f, "brls/text");
                r_meta->addView(rt);

                std::string sub = "读至 P." + std::to_string(std::max(1, r.book.current_page)) + "/" +
                                  std::to_string(std::max(1, r.book.total_pages)) + " · " +
                                  std::to_string(static_cast<int>(std::round(r.book.progress_percent))) + "%";
                auto* rs = ui::makeLabel(sub, 12.5f, "domain/book");
                rs->setMarginTop(4);
                r_meta->addView(rs);
                r_card->addView(r_meta);

                r_card->registerClickAction([this, alive = alive_, book = r.book](brls::View*) {
                    brls::sync([this, alive, book]() {
                        if (alive->load(std::memory_order_acquire)) openBook(book);
                    });
                    return true;
                });
                recent_row->addView(r_card);
            }
            recent_banner->addView(recent_row);
            header_wrap->addView(recent_banner);
        }
    }

    // 2. WeRead Status + Custom Folder Group Filter Bar
    auto* group_card = ui::makeCard("desktop/surface", 12.0f, 10.0f);
    group_card->setMarginBottom(12);

    auto* status_row = new brls::Box(brls::Axis::ROW);
    wrapFlexRow(status_row);
    status_row->setAlignItems(brls::AlignItems::CENTER);
    status_row->setMarginBottom(8);

    const struct {
        const char* key;
        std::string label;
    } status_tabs[] = {
        {"all", "全部 (" + std::to_string(all_shelf.size()) + ")"},
        {"reading", "在读 (" + std::to_string(count_reading) + ")"},
        {"unread", "未读 (" + std::to_string(count_unread) + ")"},
        {"finished", "读完 (" + std::to_string(count_finished) + ")"},
    };
    for (const auto& st : status_tabs) {
        auto* btn = ui_comfort::makeFilterChip(st.label, [this, alive = alive_, k = std::string(st.key)]() {
            brls::sync([this, alive, k]() {
                if (!alive->load(std::memory_order_acquire)) return;
                status_filter_ = k;
                rebuildContent();
            });
        }, status_filter_ == st.key, "domain/book", 29.0f);
        btn->setMarginRight(6);
        status_row->addView(btn);
    }

    auto* sep_lbl = ui::makeLabel("  分组:", 12.8f, "font/grey");
    sep_lbl->setMarginRight(6);
    status_row->addView(sep_lbl);

    auto addGroupChip = [this, status_row](const std::string& gid, const std::string& label) {
        auto* btn = ui_comfort::makeFilterChip(label, [this, alive = alive_, gid]() {
            brls::sync([this, alive, gid]() {
                if (!alive->load(std::memory_order_acquire)) return;
                group_filter_ = gid;
                rebuildContent();
            });
        }, group_filter_ == gid, "domain/book", 29.0f);
        btn->setMarginRight(6);
        status_row->addView(btn);
    };
    addGroupChip("__all__", "全部分组");
    addGroupChip("__ungrouped__", "未分组");
    for (const auto& g : groups) {
        addGroupChip(g.id, g.name);
    }

    auto* new_grp_btn = ui::makeButton("+ 新建分组", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptCreateGroup();
    }, false, 29.0f);
    new_grp_btn->setMarginRight(6);
    status_row->addView(new_grp_btn);

    if (group_filter_ != "__all__" && group_filter_ != "__ungrouped__") {
        const std::string active_gid = group_filter_;
        const std::string active_gname = store.groupName(active_gid);
        auto* ren_btn = ui::makeButton("重命名分组", [this, alive = alive_, active_gid, active_gname]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (auto* plat = brls::Application::getPlatform()) {
                plat->getImeManager()->openForText(
                    [this, alive, active_gid](std::string new_name) {
                        brls::sync([this, alive, active_gid, new_name = std::move(new_name)]() {
                            if (!alive->load(std::memory_order_acquire) || new_name.empty()) return;
                            beginLibraryBusSuppression();
                            BookLibraryStore::instance().renameGroup(active_gid, new_name);
                            endLibraryBusSuppression();
                            rebuildContent();
                        });
                    },
                    "重命名书架分组", "输入新的分组名称", 64, active_gname);
            }
        }, false, 29.0f);
        ren_btn->setMarginRight(6);
        status_row->addView(ren_btn);

        auto* del_btn = ui::makeButton("删除此分组", [this, alive = alive_, active_gid]() {
            brls::sync([this, alive, active_gid]() {
                if (!alive->load(std::memory_order_acquire)) return;
                beginLibraryBusSuppression();
                BookLibraryStore::instance().deleteGroup(active_gid);
                endLibraryBusSuppression();
                group_filter_ = "__all__";
                brls::Application::notify("已删除分组（组内书籍已归入未分组）");
                rebuildContent();
            });
        }, false, 29.0f);
        status_row->addView(del_btn);
    }
    group_card->addView(status_row);

    // 3. Format + Sort + Grid/List View Toggle + Batch + Import Row
    auto* ctrl_row = new brls::Box(brls::Axis::ROW);
    ctrl_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    ctrl_row->setAlignItems(brls::AlignItems::CENTER);
    wrapFlexRow(ctrl_row);

    auto* fmt_chips = new brls::Box(brls::Axis::ROW);
    fmt_chips->setAlignItems(brls::AlignItems::CENTER);
    wrapFlexRow(fmt_chips);
    const struct { const char* fmt; const char* label; } filters[] = {
        {"", "全部格式"},
        {"epub", "EPUB"},
        {"pdf", "PDF"},
        {"mobi", "MOBI"},
        {"azw3", "AZW3"},
        {"fb2", "FB2"},
        {"txt", "TXT"},
    };
    for (const auto& f : filters) {
        auto* btn = ui_comfort::makeFilterChip(f.label, [this, alive = alive_, fmt = std::string(f.fmt)]() {
            brls::sync([this, alive, fmt]() {
                if (!alive->load(std::memory_order_acquire)) return;
                format_filter_ = fmt;
                rebuildContent();
            });
        }, format_filter_ == f.fmt, "domain/book", 29.0f);
        btn->setMarginRight(6);
        fmt_chips->addView(btn);
    }
    if (!search_query_.empty()) {
        auto* clear_q = ui_comfort::makeFilterChip("检索: " + search_query_ + " ×", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                search_query_.clear();
                rebuildContent();
            });
        }, true, "domain/book", 29.0f);
        fmt_chips->addView(clear_q);
    }
    ctrl_row->addView(fmt_chips);

    auto* right_tools = new brls::Box(brls::Axis::ROW);
    wrapFlexRow(right_tools);
    right_tools->setAlignItems(brls::AlignItems::CENTER);

    std::string sort_label = "排序: 最近阅读";
    if (shelf_sort_mode_ == ShelfSortMode::RecentAdded) sort_label = "排序: 最近加入";
    else if (shelf_sort_mode_ == ShelfSortMode::ProgressDesc) sort_label = "排序: 阅读进度";
    else if (shelf_sort_mode_ == ShelfSortMode::TitleAsc) sort_label = "排序: 书名字母";

    auto* sort_btn = ui::makeButton(sort_label, [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (shelf_sort_mode_ == ShelfSortMode::RecentRead) shelf_sort_mode_ = ShelfSortMode::RecentAdded;
            else if (shelf_sort_mode_ == ShelfSortMode::RecentAdded) shelf_sort_mode_ = ShelfSortMode::ProgressDesc;
            else if (shelf_sort_mode_ == ShelfSortMode::ProgressDesc) shelf_sort_mode_ = ShelfSortMode::TitleAsc;
            else shelf_sort_mode_ = ShelfSortMode::RecentRead;
            rebuildContent();
        });
    }, false, 29.0f);
    sort_btn->setMarginRight(6);
    right_tools->addView(sort_btn);

    auto* grid_btn = ui_comfort::makeFilterChip("宫格书架", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            shelf_view_mode_ = ShelfViewMode::Grid;
            rebuildContent();
        });
    }, shelf_view_mode_ == ShelfViewMode::Grid, "domain/book", 29.0f);
    grid_btn->setMarginRight(4);
    right_tools->addView(grid_btn);

    auto* list_btn = ui_comfort::makeFilterChip("紧凑列表", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            shelf_view_mode_ = ShelfViewMode::List;
            rebuildContent();
        });
    }, shelf_view_mode_ == ShelfViewMode::List, "domain/book", 29.0f);
    list_btn->setMarginRight(6);
    right_tools->addView(list_btn);

    auto* batch_btn = ui_comfort::makeFilterChip(batch_mode_ ? "退出整理" : "批量整理", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            batch_mode_ = !batch_mode_;
            if (!batch_mode_) batch_selected_keys_.clear();
            rebuildContent();
        });
    }, batch_mode_, "domain/book", 29.0f);
    batch_btn->setMarginRight(6);
    right_tools->addView(batch_btn);

    auto* add_local_btn = ui::makeButton("+ 导入本地书籍", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptImportLocalBook();
    }, true, 29.0f);
    right_tools->addView(add_local_btn);

    ctrl_row->addView(right_tools);
    group_card->addView(ctrl_row);
    header_wrap->addView(group_card);

    // 4. Filter & Sort Books
    const std::string q_low = toLowerCopy(search_query_);
    visible_shelf_items_.clear();
    for (const auto& item : all_shelf) {
        if (status_filter_ == "reading" && item.status != BookReadingStatus::Reading) continue;
        if (status_filter_ == "unread" && item.status != BookReadingStatus::Unread) continue;
        if (status_filter_ == "finished" && item.status != BookReadingStatus::Finished) continue;

        if (group_filter_ == "__ungrouped__" && !item.entry.group_id.empty()) continue;
        if (group_filter_ != "__all__" && group_filter_ != "__ungrouped__" &&
            item.entry.group_id != group_filter_) continue;

        if (!format_filter_.empty() && toLowerCopy(item.book.format) != format_filter_) continue;
        if (!q_low.empty() &&
            toLowerCopy(item.book.title).find(q_low) == std::string::npos &&
            toLowerCopy(item.book.author).find(q_low) == std::string::npos &&
            toLowerCopy(item.book.format).find(q_low) == std::string::npos) continue;

        visible_shelf_items_.push_back(item);
    }

    std::sort(visible_shelf_items_.begin(), visible_shelf_items_.end(),
              [this](const ShelfDisplayItem& a, const ShelfDisplayItem& b) {
                  if (a.entry.pinned != b.entry.pinned) return a.entry.pinned > b.entry.pinned;
                  if (a.entry.pinned && a.entry.pinned_at_ms != b.entry.pinned_at_ms) {
                      return a.entry.pinned_at_ms > b.entry.pinned_at_ms;
                  }
                  switch (shelf_sort_mode_) {
                      case ShelfSortMode::RecentRead:
                          if (a.entry.last_read_at_ms != b.entry.last_read_at_ms) {
                              return a.entry.last_read_at_ms > b.entry.last_read_at_ms;
                          }
                          if (a.book.progress_percent != b.book.progress_percent) {
                              return a.book.progress_percent > b.book.progress_percent;
                          }
                          return toLowerCopy(a.book.title) < toLowerCopy(b.book.title);
                      case ShelfSortMode::RecentAdded:
                          if (a.entry.added_at_ms != b.entry.added_at_ms) {
                              return a.entry.added_at_ms > b.entry.added_at_ms;
                          }
                          return toLowerCopy(a.book.title) < toLowerCopy(b.book.title);
                      case ShelfSortMode::ProgressDesc:
                          if (a.book.progress_percent != b.book.progress_percent) {
                              return a.book.progress_percent > b.book.progress_percent;
                          }
                          return toLowerCopy(a.book.title) < toLowerCopy(b.book.title);
                      case ShelfSortMode::TitleAsc:
                          return toLowerCopy(a.book.title) < toLowerCopy(b.book.title);
                  }
                  return false;
              });

    // 5. Batch Management Action Bar
    if (batch_mode_) {
        const size_t initial_sel_count = collectBatchSelectedBooks().size();

        auto* batch_bar = ui::makeCard("desktop/soft", 12.0f, 10.0f);
        batch_bar->setAxis(brls::Axis::ROW);
        wrapFlexRow(batch_bar);
        batch_bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
        batch_bar->setAlignItems(brls::AlignItems::CENTER);
        batch_bar->setMarginBottom(12);

        batch_count_label_ = ui::makeLabel(
            "批量整理模式 · 已勾选 " + std::to_string(initial_sel_count) + " 本（点击卡片即可勾选/取消）",
            14.0f, "domain/book");
        batch_bar->addView(batch_count_label_);

        auto* b_acts = new brls::Box(brls::Axis::ROW);
        wrapFlexRow(b_acts);

        auto addBatchBtn = [b_acts](const std::string& label, bool primary, std::function<void()> cb) {
            auto* b = ui::makeButton(label, std::move(cb), primary, 30.0f);
            b->setMarginRight(6);
            b->setMarginBottom(4);
            b_acts->addView(b);
        };

        addBatchBtn("全选当前筛选", false, [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                for (const auto& it : visible_shelf_items_) {
                    batch_selected_keys_.insert(it.entry.key);
                }
                if (batch_count_label_) {
                    batch_count_label_->setText(
                        "批量整理模式 · 已勾选 " +
                        std::to_string(collectBatchSelectedBooks().size()) +
                        " 本（点击卡片即可勾选/取消）");
                }
                if (stage_grid_) stage_grid_->reloadData(false);
            });
        });
        addBatchBtn("清空选择", false, [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                batch_selected_keys_.clear();
                if (batch_count_label_) {
                    batch_count_label_->setText(
                        "批量整理模式 · 已勾选 0 本（点击卡片即可勾选/取消）");
                }
                if (stage_grid_) stage_grid_->reloadData(false);
            });
        });
        addBatchBtn("移入分组...", true, [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            const auto selected_books = collectBatchSelectedBooks();
            if (selected_books.empty()) {
                brls::Application::notify("请先点击书籍卡片勾选至少一本书");
                return;
            }
            promptBatchMoveGroup(selected_books);
        });
        addBatchBtn("标为在读", false, [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                const auto selected_books = collectBatchSelectedBooks();
                if (selected_books.empty()) return;
                beginLibraryBusSuppression();
                BookLibraryStore::instance().setBooksStatus(selected_books, "reading");
                endLibraryBusSuppression();
                BookLibraryStore::instance().enrichAndMergeLibraryBooks(books_);
                batch_selected_keys_.clear();
                batch_mode_ = false;
                rebuildContent();
            });
        });
        addBatchBtn("标为读完", false, [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                const auto selected_books = collectBatchSelectedBooks();
                if (selected_books.empty()) return;
                beginLibraryBusSuppression();
                BookLibraryStore::instance().setBooksStatus(selected_books, "finished");
                endLibraryBusSuppression();
                BookLibraryStore::instance().enrichAndMergeLibraryBooks(books_);
                batch_selected_keys_.clear();
                batch_mode_ = false;
                rebuildContent();
            });
        });
        addBatchBtn("批量置顶", false, [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                const auto selected_books = collectBatchSelectedBooks();
                if (selected_books.empty()) return;
                beginLibraryBusSuppression();
                BookLibraryStore::instance().setBooksPinned(selected_books, true);
                endLibraryBusSuppression();
                batch_selected_keys_.clear();
                batch_mode_ = false;
                rebuildContent();
            });
        });
        addBatchBtn("移出书架", false, [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                const auto selected_books = collectBatchSelectedBooks();
                if (selected_books.empty()) return;
                beginLibraryBusSuppression();
                BookLibraryStore::instance().setBooksRemovedFromShelf(selected_books, true);
                endLibraryBusSuppression();
                batch_selected_keys_.clear();
                batch_mode_ = false;
                brls::Application::notify("已移出书架（原始本地文件完好保留）");
                refresh();
            });
        });
        batch_bar->addView(b_acts);
        header_wrap->addView(batch_bar);
    }

    stage_grid_->setHeaderView(header_wrap);
    stage_grid_->setFooterView(nullptr);

    if (visible_shelf_items_.empty()) {
        auto* empty_box = ui::makeCard("desktop/surface", 24.0f, 12.0f);
        empty_box->addView(ui::makeLabel(
            all_shelf.empty()
                ? "你的书架还是空的 · 像微信读书一样导入本地 EPUB / PDF / MOBI 藏书吧"
                : "当前分组或筛选条件下暂无匹配书籍",
            16.5f, "brls/text"));
        auto* hint = ui::makeLabel(
            "支持一键扫描 ~/Downloads、~/Books、~/Documents，自动清洗书名/作者并提取高清书封。",
            14.0f, "font/grey");
        hint->setMarginTop(6);
        empty_box->addView(hint);

        auto* e_acts = new brls::Box(brls::Axis::ROW);
        e_acts->setMarginTop(14);
        auto* imp = ui::makeButton("+ 浏览本地目录并导入书籍", [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) promptImportLocalBook();
        }, true, 36.0f);
        imp->setMarginRight(8);
        e_acts->addView(imp);

        if (!all_shelf.empty()) {
            e_acts->addView(ui::makeButton("重置筛选条件", [this, alive = alive_]() {
                brls::sync([this, alive]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    status_filter_ = "all";
                    group_filter_ = "__all__";
                    format_filter_.clear();
                    search_query_.clear();
                    rebuildContent();
                });
            }, false, 36.0f));
        }
        empty_box->addView(e_acts);
        stage_grid_->setEmptyView(empty_box);
    } else {
        stage_grid_->setEmptyView(nullptr);
    }

    if (shelf_view_mode_ == ShelfViewMode::Grid) {
        const float stage_w = estimateBookStageWidth(this);
        const int cols = computeBookshelfColumns(stage_w);
        last_bookshelf_cols_ = cols;
        stage_grid_->setSpanCount(cols);
        stage_grid_->setColSpace(14.0f);
        stage_grid_->setEstimatedRowSpace(14.0f);
        stage_grid_->setEstimatedRowHeight(batch_mode_ ? 404.0f : 376.0f);
    } else {
        stage_grid_->setSpanCount(1);
        stage_grid_->setColSpace(0.0f);
        stage_grid_->setEstimatedRowSpace(8.0f);
        stage_grid_->setEstimatedRowHeight(100.0f);
    }
    stage_grid_->reloadData(reset_stage_scroll_next_);
}

void BookWorkbench::renderNotebook() {
    auto& store = BookLibraryStore::instance();

    visible_notebook_groups_.clear();
    auto matchesBookVocab = [](const BookItem& b, const VocabCard& vc) {
        if (vc.source_book.empty()) return false;
        const std::string sb = toLowerCopy(vc.source_book);
        const std::string bt = toLowerCopy(b.title);
        return !bt.empty() && (sb == bt || sb.find(bt) != std::string::npos || bt.find(sb) != std::string::npos);
    };

    for (const auto& b : books_) {
        if (b.source_id == "plugin-ai-curator") continue;
        NotebookBookGroup g;
        g.book = b;
        g.key = BookLibraryStore::computeBookKey(b);
        g.excerpt_count = store.countExcerptsForBook(b, cached_excerpts_);
        for (const auto& vc : vocab_cards_) {
            if (matchesBookVocab(b, vc)) ++g.vocab_count;
        }
        visible_notebook_groups_.push_back(std::move(g));
    }

    std::sort(visible_notebook_groups_.begin(), visible_notebook_groups_.end(),
              [](const NotebookBookGroup& a, const NotebookBookGroup& b) {
                  if ((a.excerpt_count + a.vocab_count) != (b.excerpt_count + b.vocab_count)) {
                      return (a.excerpt_count + a.vocab_count) > (b.excerpt_count + b.vocab_count);
                  }
                  return toLowerCopy(a.book.title) < toLowerCopy(b.book.title);
              });

    if (notebook_book_key_.empty() && has_active_book_ && active_book_.source_id != "plugin-ai-curator") {
        notebook_book_key_ = BookLibraryStore::computeBookKey(active_book_);
    }

    const NotebookBookGroup* selected_group = nullptr;
    for (const auto& g : visible_notebook_groups_) {
        if (g.key == notebook_book_key_) {
            selected_group = &g;
            break;
        }
    }

    const float vh = brls::Application::contentHeight > 0.0f
                         ? brls::Application::contentHeight
                         : 800.0f;
    const float list_viewport_h = std::clamp(vh - 280.0f, 260.0f, 620.0f);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    // Left Column: Virtualized Book Notebook Selector
    auto* left_col = ui::makeCard("desktop/surface", 14.0f, 12.0f);
    left_col->setWidthPercentage(32);
    left_col->setMarginRight(14);
    left_col->addView(ui::makeLabel("笔记本 · 按书籍汇总", 17.5f, "brls/text"));
    auto* left_sub = ui::makeLabel("划线书摘、读后想法、AI 长难句精析与书中生词", 12.5f, "font/grey");
    left_sub->setMarginTop(3);
    left_sub->setMarginBottom(10);
    left_col->addView(left_sub);

    auto* all_btn = ui::makeButton(
        "全部书籍汇总 (" + std::to_string(cached_excerpts_.size()) + " 条书摘)",
        [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                notebook_book_key_.clear();
                rebuildContent();
            });
        },
        selected_group == nullptr, 34.0f);
    all_btn->setMarginBottom(8);
    left_col->addView(all_btn);

    notebook_books_grid_ = new RecyclingGrid();
    notebook_books_grid_->setWidthPercentage(100);
    notebook_books_grid_->setHeight(list_viewport_h);
    notebook_books_grid_->setSpanCount(1);
    notebook_books_grid_->setEstimatedRowHeight(58.0f);
    notebook_books_grid_->setEstimatedRowSpace(6.0f);
    notebook_books_grid_->registerCell("nb_book", []() { return new NotebookBookSelectorCell(); });
    notebook_books_grid_->setDataSource(new BookNotebookBooksDataSource(
        &visible_notebook_groups_,
        selected_group ? selected_group->key : std::string{},
        [this, alive = alive_](const std::string& k) {
            brls::sync([this, alive, k]() {
                if (!alive->load(std::memory_order_acquire)) return;
                notebook_book_key_ = k;
                rebuildContent();
            });
        }));
    left_col->addView(notebook_books_grid_);
    split->addView(left_col);

    // Right Column: Virtualized Selected Book Excerpts & Vocab + Obsidian Export
    auto* right_col = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    right_col->setWidthPercentage(66);

    visible_notebook_excerpts_.clear();
    visible_notebook_vocab_.clear();
    if (selected_group) {
        visible_notebook_excerpts_ = store.listMergedExcerptsForBook(selected_group->book);
        for (const auto& vc : vocab_cards_) {
            if (matchesBookVocab(selected_group->book, vc)) visible_notebook_vocab_.push_back(vc);
        }
    } else {
        visible_notebook_excerpts_ = cached_excerpts_;
        visible_notebook_vocab_ = vocab_cards_;
    }

    auto* hdr_row = new brls::Box(brls::Axis::ROW);
    hdr_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    hdr_row->setAlignItems(brls::AlignItems::CENTER);
    wrapFlexRow(hdr_row);

    auto* hdr_meta = new brls::Box(brls::Axis::COLUMN);
    hdr_meta->setShrink(1.0f);
    hdr_meta->addView(ui::makeLabel(
        selected_group ? selected_group->book.title : "全部藏书 · 书摘与精读总览",
        19.0f, "brls/text"));
    std::string meta_sub = selected_group
        ? ((selected_group->book.author.empty() ? "本地藏书" : selected_group->book.author) +
           " · 阅读进度 " + std::to_string(static_cast<int>(std::round(selected_group->book.progress_percent))) + "%")
        : ("共收录 " + std::to_string(visible_notebook_excerpts_.size()) + " 条书摘笔记 · " +
           std::to_string(visible_notebook_vocab_.size()) + " 个生词");
    auto* ms_lbl = ui::makeLabel(meta_sub, 13.5f, "font/grey");
    ms_lbl->setMarginTop(3);
    hdr_meta->addView(ms_lbl);
    hdr_row->addView(hdr_meta);

    if (selected_group) {
        const BookItem target_book = selected_group->book;
        auto* hdr_acts = new brls::Box(brls::Axis::ROW);
        wrapFlexRow(hdr_acts);
        hdr_acts->setMarginTop(4);

        auto* read_b = ui::makeButton("继续阅读", [this, alive = alive_, target_book]() {
            if (alive->load(std::memory_order_acquire)) openBook(target_book);
        }, false, 32.0f);
        read_b->setMarginRight(6);
        hdr_acts->addView(read_b);

        auto* add_n = ui::makeButton("+ 记书摘/想法", [this, alive = alive_, target_book]() {
            if (!alive->load(std::memory_order_acquire)) return;
            promptAddBookExcerptDialog(
                target_book, std::max(1, target_book.current_page), "", "",
                [this, alive]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    cached_excerpts_ = BookLibraryStore::instance().listAllMergedExcerpts();
                    finishRefresh();
                });
        }, false, 32.0f);
        add_n->setMarginRight(6);
        hdr_acts->addView(add_n);

        hdr_acts->addView(ui::makeButton(
            "导出 Markdown (Obsidian)",
            [this, alive = alive_, target_book]() {
                if (!alive->load(std::memory_order_acquire)) return;
                std::string saved_path;
                std::string err;
                if (BookLibraryStore::instance().exportBookMarkdown(
                        target_book, visible_notebook_excerpts_, vocab_cards_, saved_path, err)) {
                    brls::Application::notify("已导出读书笔记: " + saved_path);
                    status_label_->setText("读书笔记已导出至 " + saved_path);
                } else {
                    brls::Application::notify("导出失败: " + err);
                }
            },
            true, 32.0f));
        hdr_row->addView(hdr_acts);
    }
    right_col->addView(hdr_row);

    auto* sub_bar = new brls::Box(brls::Axis::ROW);
    sub_bar->setMarginTop(12);
    sub_bar->setMarginBottom(12);
    auto* tab_ex = ui::makeButton(
        "书摘 · 想法 · AI 精析 (" + std::to_string(visible_notebook_excerpts_.size()) + ")",
        [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                notebook_sub_tab_ = 0;
                rebuildContent();
            });
        },
        notebook_sub_tab_ == 0, 32.0f);
    tab_ex->setMarginRight(8);
    sub_bar->addView(tab_ex);

    sub_bar->addView(ui::makeButton(
        "关联 SM-2 生词 (" + std::to_string(visible_notebook_vocab_.size()) + ")",
        [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                notebook_sub_tab_ = 1;
                rebuildContent();
            });
        },
        notebook_sub_tab_ == 1, 32.0f));
    right_col->addView(sub_bar);

    notebook_grid_ = new RecyclingGrid();
    notebook_grid_->setWidthPercentage(100);
    notebook_grid_->setHeight(list_viewport_h);
    notebook_grid_->setSpanCount(1);
    notebook_grid_->setEstimatedRowHeight(notebook_sub_tab_ == 0 ? 124.0f : 86.0f);
    notebook_grid_->setEstimatedRowSpace(notebook_sub_tab_ == 0 ? 10.0f : 8.0f);
    notebook_grid_->registerCell("nb_excerpt", []() { return new NotebookExcerptCell(); });
    notebook_grid_->registerCell("nb_vocab", []() { return new NotebookVocabCell(); });

    if (notebook_sub_tab_ == 0) {
        auto* empty_n = ui::makeCard("desktop/soft", 16.0f, 10.0f);
        empty_n->addView(ui::makeLabel(
            "暂无书摘或长难句精读记录 · 在阅读界面点击「+ 书摘/想法」或「语境长难句精读」即可自动沉淀到这里。",
            14.5f, "font/grey", 1.5f));
        notebook_grid_->setEmptyView(empty_n);
    } else {
        auto* empty_v = ui::makeCard("desktop/soft", 16.0f, 10.0f);
        empty_v->addView(ui::makeLabel(
            "本书暂无关联生词 · 阅读时点击划词或「加入 SM-2 生词复习队列」即可归档。",
            14.5f, "font/grey"));
        notebook_grid_->setEmptyView(empty_v);
    }

    notebook_grid_->setDataSource(new BookNotebookDataSource(
        notebook_sub_tab_,
        &visible_notebook_excerpts_,
        &visible_notebook_vocab_,
        [this, alive = alive_](const BookExcerpt& ex) {
            brls::sync([this, alive, ex]() {
                if (!alive->load(std::memory_order_acquire)) return;
                openExternalBook({
                    {"book_id", ex.book_id},
                    {"source_id", ex.source_id},
                    {"file_path", ex.file_path},
                    {"title", ex.book_title},
                    {"page", std::max(1, ex.page_number)},
                });
            });
        },
        [this, alive = alive_](const std::string& id) {
            brls::sync([this, alive, id]() {
                if (!alive->load(std::memory_order_acquire)) return;
                beginLibraryBusSuppression();
                BookLibraryStore::instance().deleteExcerpt(id);
                endLibraryBusSuppression();
                cached_excerpts_ = BookLibraryStore::instance().listAllMergedExcerpts();
                finishRefresh();
            });
        }));
    right_col->addView(notebook_grid_);

    split->addView(right_col);
    stage_grid_->setStaticContent(split);
}

void BookWorkbench::openBook(const BookItem& book) {
    active_book_ = book;
    has_active_book_ = true;
    clip_jump_results_.clear();
    clip_jump_query_.clear();
    selected_word_idx_ = 0;
    custom_focus_word_.clear();
    if (book.source_id != "plugin-ai-curator") {
        beginLibraryBusSuppression();
        BookLibraryStore::instance().touchBookRead(
            book, std::max(1, book.current_page), std::max(1, book.total_pages), "");
        endLibraryBusSuppression();
        BookLibraryStore::instance().enrichAndMergeLibraryBooks(books_);
    }
    loadSpread(book.current_page);
    sub_mode_ = (book.format == "pdf") ? SubMode::PdfBabelDoc : SubMode::EpubCompanion;
    if (on_entity_focus_) {
        on_entity_focus_(book.title + " · P." + std::to_string(active_spread_.page_number) +
                         "/" + std::to_string(std::max(active_spread_.total_pages, book.total_pages)));
    }
    if (HostKernel::instance().isInitialized()) {
        HostKernel::instance().bus().publish("entity.book.open", {
                                                                     {"id", book.id},
                                                                     {"title", book.title},
                                                                     {"page", book.current_page},
                                                                     {"origin", "host_workbench"},
                                                                 });
        HostKernel::instance().bus().publish("host.focus_changed", {
                                                                       {"domain", "book"},
                                                                       {"id", book.id},
                                                                       {"title", book.title},
                                                                       {"subtitle", book.author},
                                                                       {"author", book.author},
                                                                       {"format", book.format},
                                                                       {"source_id", book.source_id},
                                                                       {"page_number", book.current_page},
                                                                       {"total_pages", book.total_pages},
                                                                       {"play_url", book.file_path},
                                                                       {"extra", nlohmann::json{
                                                                           {"vocab_count", book.vocab_count},
                                                                           {"progress_percent", book.progress_percent},
                                                                       }},
                                                                   });
    }
    finishRefresh();
}

void BookWorkbench::ensureLocalPdfPageRendered(int page_num) {
    const int safe_page = std::max(1, page_num);
    if (toLowerCopy(active_book_.format) != "pdf" || active_book_.file_path.empty()) {
        return;
    }
    std::error_code ec;
    if (!std::filesystem::is_regular_file(active_book_.file_path, ec)) {
        return;
    }

    const std::string cached_hit =
        BookLibraryStore::lookupCachedPdfPagePng(active_book_.file_path, safe_page);
    if (!cached_hit.empty()) {
        rendered_pdf_book_path_ = active_book_.file_path;
        rendered_pdf_page_num_ = safe_page;
        rendered_pdf_orig_png_ = cached_hit;
        return;
    }

    if (rendered_pdf_book_path_ != active_book_.file_path || rendered_pdf_page_num_ != safe_page) {
        rendered_pdf_book_path_ = active_book_.file_path;
        rendered_pdf_page_num_ = safe_page;
        rendered_pdf_orig_png_.clear();
    }

    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    pdf_page_request_->cancel();
    pdf_page_request_ = CancelToken::create();
    auto req = pdf_page_request_;
    const std::string target_path = active_book_.file_path;

    loadUiInBackground(kernel.uiTasks(), "book.pdf_page_render", req,
        [target_path, safe_page](const auto& cancel) {
            struct RenderRes {
                std::string png_path;
                int total_pages = 0;
            } res;
            if (cancel->isCancelled()) return res;
            BookLibraryStore::renderPdfPagePngSync(
                target_path, safe_page, res.png_path, res.total_pages);
            return res;
        },
        [this, alive = alive_, req, target_path, safe_page](auto res) {
            if (!alive->load(std::memory_order_acquire) || pdf_page_request_ != req) return;
            if (active_book_.file_path != target_path) return;
            if (res.total_pages > 0) {
                active_book_.total_pages = std::max(active_book_.total_pages, res.total_pages);
                active_spread_.total_pages = std::max(active_spread_.total_pages, res.total_pages);
            }
            if (!res.png_path.empty() &&
                std::max(1, active_spread_.page_number) == safe_page) {
                rendered_pdf_book_path_ = target_path;
                rendered_pdf_page_num_ = safe_page;
                rendered_pdf_orig_png_ = res.png_path;
                if (sub_mode_ == SubMode::PdfBabelDoc) {
                    rebuildContent();
                }
            }
        });
}

void BookWorkbench::loadSpread(int page_num) {
    spread_request_->cancel();
    spread_request_ = CancelToken::create();
    const int safe_page = std::max(1, page_num);
    active_spread_ = {};
    active_spread_.page_number = safe_page;
    active_spread_.total_pages = std::max(safe_page, active_book_.total_pages);
    active_book_.current_page = safe_page;

    const bool preview = std::any_of(preview_books_.begin(), preview_books_.end(), [this](const auto& book) {
        return book.id == active_book_.id && book.source_id == active_book_.source_id;
    });
    if (preview) {
        active_spread_ = buildShowcaseSpread(active_book_, safe_page);
        active_book_.current_page = active_spread_.page_number;
        status_label_->setText("布局预览 · 示例内容");
        return;
    }

    if (toLowerCopy(active_book_.format) == "pdf") {
        ensureLocalPdfPageRendered(safe_page);
    }

    auto& kernel = HostKernel::instance();
    if (kernel.isInitialized()) {
        const auto sources = kernel.domains().bookSources();
        std::shared_ptr<IBookSource> matched_src;
        for (const auto& src : sources) {
            if (!active_book_.source_id.empty() && src->sourceId() == active_book_.source_id) {
                matched_src = src;
                break;
            }
        }
        if (!matched_src && active_book_.source_id.empty()) {
            const std::string preferred = (active_book_.format == "pdf") ? "plugin-pdf-babeldoc" : "plugin-ebook";
            for (const auto& src : sources) {
                if (src->sourceId() == preferred) {
                    matched_src = src;
                    break;
                }
            }
        }
        if (matched_src) {
            status_label_->setText("正在加载书页…");
            loadUiInBackground(kernel.uiTasks(), "book.spread", spread_request_,
                [src = matched_src, book_id = active_book_.id, safe_page](const auto&) {
                    return src->fetchSpread(book_id, safe_page);
                },
                [this, alive = alive_](BookSpread spread) {
                    if (!alive->load(std::memory_order_acquire)) return;
                    status_label_->setText(spread.book_id.empty() || spread.blocks.empty() ? "书页加载失败，可重试" : "书页已加载");
                    if (!spread.book_id.empty() && !spread.blocks.empty()) {
                        active_book_.current_page = spread.page_number;
                        active_book_.total_pages = std::max(active_book_.total_pages, spread.total_pages);
                        for (const auto& b : spread.blocks) {
                            if (b.id == kPdfPageImageBlockId && !b.original.empty()) {
                                std::error_code ec;
                                if (std::filesystem::is_regular_file(b.original, ec)) {
                                    rendered_pdf_book_path_ = active_book_.file_path;
                                    rendered_pdf_page_num_ = spread.page_number;
                                    rendered_pdf_orig_png_ = b.original;
                                }
                                break;
                            }
                        }
                        active_spread_ = std::move(spread);
                        if (active_book_.source_id != "plugin-ai-curator") {
                            beginLibraryBusSuppression();
                            BookLibraryStore::instance().touchBookRead(
                                active_book_, active_book_.current_page,
                                active_book_.total_pages, active_spread_.chapter_title);
                            endLibraryBusSuppression();
                            BookLibraryStore::instance().enrichAndMergeLibraryBooks(books_);
                        }
                        if (HostKernel::instance().isInitialized()) {
                            HostKernel::instance().bus().publish("host.focus_changed", {
                                {"domain", "book"},
                                {"id", active_book_.id},
                                {"title", active_book_.title},
                                {"subtitle", active_book_.author},
                                {"author", active_book_.author},
                                {"format", active_book_.format},
                                {"source_id", active_book_.source_id},
                                {"page_number", active_book_.current_page},
                                {"total_pages", active_book_.total_pages},
                                {"play_url", active_book_.file_path},
                                {"extra", nlohmann::json{
                                    {"chapter_title", active_spread_.chapter_title},
                                    {"vocab_count", active_book_.vocab_count},
                                }},
                            });
                        }
                    }
                    rebuildContent();
                });
            return;
        }
    }
    if (toLowerCopy(active_book_.format) == "pdf" && !active_book_.file_path.empty()) {
        status_label_->setText("正在直接渲染本地原版 PDF 页面");
    } else {
        status_label_->setText("无法加载书页：对应阅读来源未连接");
    }
}

void BookWorkbench::renderPdfBabelDoc() {
    const int cur_page = std::max(1, active_spread_.page_number);
    const int total_pages = std::max(cur_page, std::max(active_spread_.total_pages, active_book_.total_pages));

    std::error_code fs_ec;
    const bool has_local_pdf_file =
        !active_book_.file_path.empty() &&
        std::filesystem::is_regular_file(active_book_.file_path, fs_ec);

    std::string orig_page_img;
    std::string trans_page_img;
    for (const auto& blk : active_spread_.blocks) {
        if (blk.id == kPdfPageImageBlockId) {
            if (!blk.original.empty() && std::filesystem::is_regular_file(blk.original, fs_ec)) {
                orig_page_img = blk.original;
            }
            if (!blk.translated.empty() && std::filesystem::is_regular_file(blk.translated, fs_ec)) {
                trans_page_img = blk.translated;
            }
            break;
        }
    }
    if (orig_page_img.empty() && has_local_pdf_file) {
        if (rendered_pdf_book_path_ == active_book_.file_path &&
            rendered_pdf_page_num_ == cur_page &&
            !rendered_pdf_orig_png_.empty() &&
            std::filesystem::is_regular_file(rendered_pdf_orig_png_, fs_ec)) {
            orig_page_img = rendered_pdf_orig_png_;
        } else {
            orig_page_img = BookLibraryStore::lookupCachedPdfPagePng(active_book_.file_path, cur_page);
            if (orig_page_img.empty()) {
                ensureLocalPdfPageRendered(cur_page);
            }
        }
    }

    const bool use_native_pdf_pages = !orig_page_img.empty() || has_local_pdf_file;

    auto* toolbar = ui::makeCard("desktop/surface", 12.0f, 10.0f);
    toolbar->setMarginBottom(12);
    auto* bar = new brls::Box(brls::Axis::ROW);
    bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    bar->setAlignItems(brls::AlignItems::CENTER);
    wrapFlexRow(bar);

    auto* left_bar = new brls::Box(brls::Axis::ROW);
    left_bar->setAlignItems(brls::AlignItems::CENTER);
    left_bar->setShrink(1.0f);
    auto* back_btn = ui::makeButton("← 书架", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) setSubMode(SubMode::Bookshelf);
    }, false, 32.0f);
    back_btn->setMarginRight(10);
    left_bar->addView(back_btn);

    auto* hdr_lbl = ui::makeLabel(
        active_book_.title + " · 第 " + std::to_string(cur_page) +
            " / " + std::to_string(total_pages) + " 页",
        16.0f, "brls/text");
    hdr_lbl->setSingleLine(true);
    left_bar->addView(hdr_lbl);
    bar->addView(left_bar);

    auto* nav = new brls::Box(brls::Axis::ROW);
    wrapFlexRow(nav);

    if (use_native_pdf_pages) {
        auto* mode_btn = ui::makeButton(
            pdf_single_page_mode_ ? "双栏对照" : "仅看原页",
            [this, alive = alive_]() {
                brls::sync([this, alive]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    pdf_single_page_mode_ = !pdf_single_page_mode_;
                    rebuildContent();
                });
            },
            false, 34.0f);
        mode_btn->setMarginRight(6);
        nav->addView(mode_btn);

        auto* zoom_out = ui::makeButton("−", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                pdf_zoom_pct_ = std::max(70, pdf_zoom_pct_ - 15);
                rebuildContent();
            });
        }, false, 34.0f);
        zoom_out->setMarginRight(4);
        nav->addView(zoom_out);

        auto* zoom_rst = ui::makeButton(std::to_string(pdf_zoom_pct_) + "%", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                pdf_zoom_pct_ = 100;
                rebuildContent();
            });
        }, false, 34.0f);
        zoom_rst->setMarginRight(4);
        nav->addView(zoom_rst);

        auto* zoom_in = ui::makeButton("+", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                if (!pdf_single_page_mode_ && pdf_zoom_pct_ >= 100) {
                    pdf_single_page_mode_ = true;
                    pdf_zoom_pct_ = 115;
                } else {
                    pdf_zoom_pct_ = std::min(130, pdf_zoom_pct_ + 15);
                }
                rebuildContent();
            });
        }, false, 34.0f);
        zoom_in->setMarginRight(6);
        nav->addView(zoom_in);
    }

    auto* prev = ui::makeButton("上一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (active_spread_.page_number > 1) loadSpread(active_spread_.page_number - 1);
            rebuildContent();
        });
    }, false, 34.0f);
    prev->setMarginRight(6);
    nav->addView(prev);

    auto* next = ui::makeButton("下一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            const int max_pg = std::max(1, std::max(active_spread_.total_pages, active_book_.total_pages));
            if (active_spread_.page_number < max_pg) {
                loadSpread(active_spread_.page_number + 1);
                rebuildContent();
            }
        });
    }, false, 34.0f);
    next->setMarginRight(6);
    nav->addView(next);

    auto* jump_p = ui::makeButton("跳页", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptJumpPage();
    }, false, 34.0f);
    jump_p->setMarginRight(6);
    nav->addView(jump_p);

    auto* note_add = ui::makeButton("+ 书摘/想法", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        std::string sample_quote;
        for (const auto& blk : active_spread_.blocks) {
            if (!blk.original.empty() && blk.kind != "image") {
                sample_quote = stripMarkdownToPlainText(blk.original);
                break;
            }
        }
        promptAddBookExcerptDialog(
            active_book_, std::max(1, active_spread_.page_number), active_spread_.chapter_title, sample_quote,
            [this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                cached_excerpts_ = BookLibraryStore::instance().listAllMergedExcerpts();
                finishRefresh();
            });
    }, false, 34.0f);
    note_add->setMarginRight(6);
    nav->addView(note_add);

    auto* nb_open = ui::makeButton("本书笔记", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        notebook_book_key_ = BookLibraryStore::computeBookKey(active_book_);
        setSubMode(SubMode::Notebook);
    }, false, 34.0f);
    nb_open->setMarginRight(6);
    nav->addView(nb_open);

    nav->addView(ui::makeButton(
        action_busy_ ? "精校中..." : "BabelDOC 精校此页",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) refineCurrentPdfPage();
        },
        true, 34.0f));
    bar->addView(nav);
    toolbar->addView(bar);

    auto* stage_wrap = new brls::Box(brls::Axis::COLUMN);
    stage_wrap->setWidthPercentage(100);
    stage_wrap->addView(toolbar);

    auto renderBookBlock = [this](const std::string& text,
                                  const std::string& kind,
                                  float body_size,
                                  const std::string& color_token,
                                  float line_height) -> brls::View* {
        if (kind == "image") {
            const std::string img_url = ImageCache::normalizeImageUrl(text, active_book_.file_path);
            if (!img_url.empty()) {
                auto* page_view = new PdfPageImageView();
                page_view->setWidthPercentage(100);
                page_view->setPageImage(img_url, std::max(1, active_spread_.page_number));
                return page_view;
            }
        }
        if (kind == "heading") {
            MarkdownStyle h_st;
            h_st.font_size = 19.0f;
            h_st.line_height = 1.45f;
            h_st.text_color_token = color_token;
            h_st.bold_base = true;
            return new MarkdownRichTextView(text, std::move(h_st));
        }
        MarkdownStyle st;
        st.font_size = body_size;
        st.line_height = line_height;
        st.text_color_token = color_token;
        st.block_gap = 6.0f;
        st.base_url = active_book_.file_path;
        return new MarkdownView(text, std::move(st));
    };

    if (use_native_pdf_pages) {
        if (pdf_single_page_mode_) {
            auto* center_wrap = new brls::Box(brls::Axis::COLUMN);
            center_wrap->setWidthPercentage(100);
            center_wrap->setAlignItems(brls::AlignItems::CENTER);

            const int single_w_pct =
                std::clamp(static_cast<int>(std::round(pdf_zoom_pct_ * 0.80f)), 52, 100);
            auto* single_card = ui::makeCard("desktop/surface", 16.0f, 12.0f);
            single_card->setWidthPercentage(static_cast<float>(single_w_pct));

            auto* hdr_row = new brls::Box(brls::Axis::ROW);
            hdr_row->setWidthPercentage(100);
            hdr_row->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
            hdr_row->setAlignItems(brls::AlignItems::CENTER);
            hdr_row->setMarginBottom(10);
            hdr_row->addView(ui::makePill(
                "原版 PDF 页面 · 第 " + std::to_string(cur_page) + " 页",
                "desktop/soft", "font/grey"));
            if (!trans_page_img.empty()) {
                hdr_row->addView(ui::makePill("已有中文精校页 · 可切换双栏对照", "status/ok_bg", "domain/book"));
            }
            single_card->addView(hdr_row);

            auto* pdf_view = new PdfPageImageView();
            pdf_view->setWidthPercentage(100);
            pdf_view->setPageImage(orig_page_img, cur_page);
            single_card->addView(pdf_view);

            center_wrap->addView(single_card);
            stage_wrap->addView(center_wrap);
            stage_grid_->setStaticContent(stage_wrap);
            return;
        }

        const int col_page_pct = std::clamp(pdf_zoom_pct_, 60, 100);

        auto* split = new brls::Box(brls::Axis::ROW);
        split->setWidthPercentage(100);
        split->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);

        auto* orig_col = ui::makeCard("desktop/surface", 16.0f, 12.0f);
        orig_col->setWidthPercentage(49.2f);
        orig_col->setAlignItems(brls::AlignItems::CENTER);

        auto* orig_hdr = new brls::Box(brls::Axis::ROW);
        orig_hdr->setWidthPercentage(100);
        orig_hdr->setMarginBottom(10);
        orig_hdr->addView(ui::makePill(
            "原版 PDF 页面 · 第 " + std::to_string(cur_page) + " 页",
            "desktop/soft", "font/grey"));
        orig_col->addView(orig_hdr);

        auto* orig_page_wrap = new brls::Box(brls::Axis::COLUMN);
        orig_page_wrap->setWidthPercentage(static_cast<float>(col_page_pct));
        auto* orig_pdf_view = new PdfPageImageView();
        orig_pdf_view->setWidthPercentage(100);
        orig_pdf_view->setPageImage(orig_page_img, cur_page);
        orig_page_wrap->addView(orig_pdf_view);
        orig_col->addView(orig_page_wrap);
        split->addView(orig_col);

        auto* trans_col = ui::makeCard("desktop/surface", 16.0f, 12.0f);
        trans_col->setWidthPercentage(49.2f);

        auto* trans_hdr = new brls::Box(brls::Axis::ROW);
        trans_hdr->setWidthPercentage(100);
        trans_hdr->setMarginBottom(10);
        trans_hdr->addView(ui::makePill(
            "BabelDOC 保真中文版面 · 第 " + std::to_string(cur_page) + " 页",
            "status/ok_bg", "domain/book"));
        trans_col->addView(trans_hdr);

        if (!trans_page_img.empty()) {
            trans_col->setAlignItems(brls::AlignItems::CENTER);
            auto* trans_page_wrap = new brls::Box(brls::Axis::COLUMN);
            trans_page_wrap->setWidthPercentage(static_cast<float>(col_page_pct));
            auto* trans_pdf_view = new PdfPageImageView();
            trans_pdf_view->setWidthPercentage(100);
            trans_pdf_view->setPageImage(trans_page_img, cur_page);
            trans_page_wrap->addView(trans_pdf_view);
            trans_col->addView(trans_page_wrap);
        } else {
            bool has_llm_text_trans = false;
            for (const auto& blk : active_spread_.blocks) {
                if (blk.id == kPdfPageImageBlockId || blk.kind == "image" || blk.translated.empty()) continue;
                if (blk.translated.rfind("[第 ", 0) == 0 &&
                    blk.translated.find("尚未经 BabelDOC 译制") != std::string::npos) {
                    continue;
                }
                has_llm_text_trans = true;
                break;
            }
            if (has_llm_text_trans) {
                for (const auto& blk : active_spread_.blocks) {
                    if (blk.id == kPdfPageImageBlockId || blk.kind == "image" || blk.translated.empty()) continue;
                    auto* p = renderBookBlock(blk.translated, blk.kind, 16.5f, "brls/text", 1.62f);
                    p->setMarginTop(10);
                    trans_col->addView(p);
                }
            } else {
                auto* hint_card = ui::makeCard("desktop/soft", 18.0f, 10.0f);
                hint_card->setWidthPercentage(100);
                hint_card->addView(ui::makeLabel(
                    action_busy_
                        ? ("正在通过 BabelDOC 精校译制第 " + std::to_string(cur_page) + " 页保真版面…")
                        : ("第 " + std::to_string(cur_page) + " 页尚未生成中文对照 PDF 页"),
                    16.5f, "brls/text"));
                auto* desc = ui::makeLabel(
                    "• 点击「BabelDOC 精校此页」可生成保留原书排版、公式与图表的中文对应 PDF 页；\n"
                    "• 若只需直接阅读原版 PDF，可点击「仅看原页」切换居中大幅原版书页视图。",
                    14.0f, "font/grey", 1.55f);
                desc->setMarginTop(8);
                hint_card->addView(desc);

                auto* act_row = new brls::Box(brls::Axis::ROW);
                wrapFlexRow(act_row);
                act_row->setMarginTop(14);
                auto* refine_btn = ui::makeButton(
                    action_busy_ ? "精校中..." : "BabelDOC 精校此页",
                    [this, alive = alive_]() {
                        if (alive->load(std::memory_order_acquire)) refineCurrentPdfPage();
                    },
                    true, 34.0f);
                refine_btn->setMarginRight(8);
                act_row->addView(refine_btn);

                act_row->addView(ui::makeButton(
                    "仅看原版 PDF 页",
                    [this, alive = alive_]() {
                        brls::sync([this, alive]() {
                            if (!alive->load(std::memory_order_acquire)) return;
                            pdf_single_page_mode_ = true;
                            rebuildContent();
                        });
                    },
                    false, 34.0f));
                hint_card->addView(act_row);
                trans_col->addView(hint_card);
            }
        }

        split->addView(trans_col);
        stage_wrap->addView(split);
        stage_grid_->setStaticContent(stage_wrap);
        return;
    }

    // Showcase / virtual spread fallback when no physical PDF file is attached
    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    auto* orig_col = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    orig_col->setWidthPercentage(49);
    orig_col->setMarginRight(12);
    orig_col->addView(ui::makePill("Original PDF Layout Block", "desktop/soft", "font/grey"));

    for (const auto& blk : active_spread_.blocks) {
        auto* p_box = new brls::Box(brls::Axis::COLUMN);
        p_box->setWidthPercentage(100);
        p_box->setPadding(6, 10, 6, 10);
        p_box->setCornerRadius(6.0f);
        p_box->setMarginTop(8);
        if (!blk.original.empty() && blk.kind != "image") {
            p_box->setFocusable(true);
            p_box->setHighlightCornerRadius(6.0f);
            p_box->registerClickAction([this, alive = alive_, text = blk.original](brls::View*) {
                if (!alive->load(std::memory_order_acquire)) return true;
                auto& k = HostKernel::instance();
                SelectionContext sctx;
                const std::string clean_txt = stripMarkdownToPlainText(text);
                sctx.context_sentence = clean_txt.empty() ? text : clean_txt;
                sctx.source_title = active_book_.title + " · P." +
                                    std::to_string(active_spread_.page_number) + "/" +
                                    std::to_string(std::max(active_spread_.total_pages, active_book_.total_pages));
                sctx.source_id = active_book_.source_id;
                sctx.book_id = active_book_.id;
                sctx.domain = ContentDomain::Book;
                if (k.isInitialized()) {
                    sctx.capabilities = &k.capabilities();
                    sctx.bus = &k.bus();
                    sctx.ui_tasks = &k.uiTasks();
                }
                SelectionActionPopover::show(std::move(sctx));
                return true;
            });
        }
        p_box->addView(renderBookBlock(blk.original, blk.kind, 16.5f, "brls/text", 1.62f));
        orig_col->addView(p_box);
    }
    split->addView(orig_col);

    auto* trans_col = ui::makeCard("desktop/surface", 20.0f, 12.0f);
    trans_col->setWidthPercentage(49);
    trans_col->addView(ui::makePill("BabelDOC 保真中文版面区块", "status/ok_bg", "domain/book"));
    for (const auto& blk : active_spread_.blocks) {
        auto* p = renderBookBlock(blk.translated, blk.kind, 16.5f, "brls/text", 1.62f);
        p->setMarginTop(12);
        trans_col->addView(p);
    }
    split->addView(trans_col);

    stage_wrap->addView(split);
    stage_grid_->setStaticContent(stage_wrap);
}

void BookWorkbench::renderEpubCompanion() {
    auto* toolbar = ui::makeCard("desktop/surface", 12.0f, 10.0f);
    toolbar->setMarginBottom(12);
    auto* bar = new brls::Box(brls::Axis::ROW);
    bar->setJustifyContent(brls::JustifyContent::SPACE_BETWEEN);
    bar->setAlignItems(brls::AlignItems::CENTER);
    wrapFlexRow(bar);

    auto* left_bar = new brls::Box(brls::Axis::ROW);
    left_bar->setAlignItems(brls::AlignItems::CENTER);
    left_bar->setShrink(1.0f);
    auto* back_btn = ui::makeButton("← 书架", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) setSubMode(SubMode::Bookshelf);
    }, false, 32.0f);
    back_btn->setMarginRight(10);
    left_bar->addView(back_btn);

    std::string hdr_txt = active_book_.title;
    if (!active_spread_.chapter_title.empty()) hdr_txt += " · " + active_spread_.chapter_title;
    hdr_txt += " · 第 " + std::to_string(std::max(1, active_spread_.page_number)) +
               " / " + std::to_string(std::max(1, active_spread_.total_pages)) + " 页";
    auto* hdr_lbl = ui::makeLabel(hdr_txt, 15.5f, "brls/text");
    hdr_lbl->setSingleLine(true);
    left_bar->addView(hdr_lbl);
    bar->addView(left_bar);

    auto* nav = new brls::Box(brls::Axis::ROW);
    wrapFlexRow(nav);
    auto* prev = ui::makeButton("上一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (active_spread_.page_number > 1) {
                selected_word_idx_ = 0;
                custom_focus_word_.clear();
                clip_jump_query_.clear();
                clip_jump_results_.clear();
                loadSpread(active_spread_.page_number - 1);
            }
            rebuildContent();
        });
    }, false, 34.0f);
    prev->setMarginRight(6);
    nav->addView(prev);

    auto* next = ui::makeButton("下一页", [this, alive = alive_]() {
        brls::sync([this, alive]() {
            if (!alive->load(std::memory_order_acquire)) return;
            if (active_spread_.page_number < std::max(1, active_spread_.total_pages)) {
                selected_word_idx_ = 0;
                custom_focus_word_.clear();
                clip_jump_query_.clear();
                clip_jump_results_.clear();
                loadSpread(active_spread_.page_number + 1);
                rebuildContent();
            }
        });
    }, false, 34.0f);
    next->setMarginRight(6);
    nav->addView(next);

    auto* jump_p = ui::makeButton("跳页", [this, alive = alive_]() {
        if (alive->load(std::memory_order_acquire)) promptJumpPage();
    }, false, 34.0f);
    jump_p->setMarginRight(6);
    nav->addView(jump_p);

    auto* note_add = ui::makeButton("+ 书摘/想法", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        std::string sample_quote;
        for (const auto& blk : active_spread_.blocks) {
            if (!blk.original.empty() && blk.kind != "image") {
                sample_quote = stripMarkdownToPlainText(blk.original);
                break;
            }
        }
        promptAddBookExcerptDialog(
            active_book_, std::max(1, active_spread_.page_number), active_spread_.chapter_title, sample_quote,
            [this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                cached_excerpts_ = BookLibraryStore::instance().listAllMergedExcerpts();
                finishRefresh();
            });
    }, false, 34.0f);
    note_add->setMarginRight(6);
    nav->addView(note_add);

    auto* nb_open = ui::makeButton("本书笔记", [this, alive = alive_]() {
        if (!alive->load(std::memory_order_acquire)) return;
        notebook_book_key_ = BookLibraryStore::computeBookKey(active_book_);
        setSubMode(SubMode::Notebook);
    }, false, 34.0f);
    nb_open->setMarginRight(6);
    nav->addView(nb_open);

    nav->addView(ui::makeButton(
        action_busy_ ? "翻译中..." : "AI 译本页",
        [this, alive = alive_]() {
            if (alive->load(std::memory_order_acquire)) translateCurrentEpubPage();
        },
        true, 34.0f));
    bar->addView(nav);
    toolbar->addView(bar);
    auto* stage_wrap = new brls::Box(brls::Axis::COLUMN);
    stage_wrap->setWidthPercentage(100);
    stage_wrap->addView(toolbar);

    auto* split = new brls::Box(brls::Axis::ROW);
    split->setWidthPercentage(100);

    // Left: Calm serif-like reader pane
    auto* reader = ui::makeCard("desktop/surface", 24.0f, 12.0f);
    reader->setWidthPercentage(58);
    reader->setMarginRight(14);
    std::string fmt_upper = active_book_.format.empty() ? "EBOOK" : active_book_.format;
    for (char& ch : fmt_upper) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    reader->addView(ui::makeLabel(active_book_.title + " · " + fmt_upper + " 伴读正文", 19.5f, "brls/text"));

    std::vector<std::string> highlight_words;
    for (const auto& blk : active_spread_.blocks) {
        auto* p_box = new brls::Box(brls::Axis::COLUMN);
        p_box->setWidthPercentage(100);
        p_box->setPadding(6, 10, 6, 10);
        p_box->setCornerRadius(6.0f);
        p_box->setMarginTop(8);
        if (!blk.original.empty() && blk.kind != "image") {
            p_box->setFocusable(true);
            p_box->setHighlightCornerRadius(6.0f);
            p_box->registerClickAction([this, alive = alive_, text = blk.original](brls::View*) {
                if (!alive->load(std::memory_order_acquire)) return true;
                auto& k = HostKernel::instance();
                SelectionContext sctx;
                const std::string clean_txt = stripMarkdownToPlainText(text);
                sctx.context_sentence = clean_txt.empty() ? text : clean_txt;
                sctx.source_title = active_book_.title + " · P." +
                                    std::to_string(active_spread_.page_number) + "/" +
                                    std::to_string(std::max(active_spread_.total_pages, active_book_.total_pages));
                sctx.source_id = active_book_.source_id;
                sctx.book_id = active_book_.id;
                sctx.domain = ContentDomain::Book;
                if (k.isInitialized()) {
                    sctx.capabilities = &k.capabilities();
                    sctx.bus = &k.bus();
                    sctx.ui_tasks = &k.uiTasks();
                }
                SelectionActionPopover::show(std::move(sctx));
                return true;
            });
        }
        if (blk.kind == "image") {
            const std::string img_url = ImageCache::normalizeImageUrl(blk.original, active_book_.file_path);
            if (!img_url.empty()) {
                auto* img = new CachedImageView();
                img->setWidthPercentage(100);
                img->setHeight(240.0f);
                img->setScalingType(brls::ImageScalingType::FIT);
                img->setCornerRadius(6.0f);
                img->setImageUrl(img_url);
                p_box->addView(img);
            }
        } else if (blk.kind == "heading") {
            MarkdownStyle h_st;
            h_st.font_size = 19.0f;
            h_st.line_height = 1.45f;
            h_st.text_color_token = "brls/text";
            h_st.bold_base = true;
            p_box->addView(new MarkdownRichTextView(blk.original, std::move(h_st)));
        } else {
            MarkdownStyle b_st;
            b_st.font_size = 17.0f;
            b_st.line_height = 1.68f;
            b_st.text_color_token = "brls/text";
            b_st.block_gap = 6.0f;
            b_st.base_url = active_book_.file_path;
            p_box->addView(new MarkdownView(blk.original, std::move(b_st)));
        }
        if (!blk.translated.empty()) {
            MarkdownStyle t_st;
            t_st.font_size = 15.5f;
            t_st.line_height = 1.65f;
            t_st.text_color_token = "font/grey";
            t_st.block_gap = 5.0f;
            auto* t = new MarkdownView(blk.translated, std::move(t_st));
            t->setMarginTop(6);
            p_box->addView(t);
        }
        reader->addView(p_box);
        for (const auto& hw : blk.highlighted_words) {
            if (std::find(highlight_words.begin(), highlight_words.end(), hw) == highlight_words.end()) {
                highlight_words.push_back(hw);
            }
        }
    }
    split->addView(reader);

    // Right: Sentence & Etymology Companion + NAS Clip Jump
    auto* comp = ui::makeCard("desktop/surface", 18.0f, 12.0f);
    comp->setWidthPercentage(40);
    comp->addView(ui::makeLabel("句子精析 · 词根记忆 & 影视联动", 18.0f, "brls/text"));

    const size_t word_cap = std::min<size_t>(highlight_words.size(), 8);
    brls::Box* word_row = nullptr;
    for (size_t i = 0; i < word_cap; ++i) {
        if (i % 4 == 0) {
            word_row = new brls::Box(brls::Axis::ROW);
            word_row->setMarginTop(i == 0 ? 10 : 6);
            comp->addView(word_row);
        }
        auto* b = ui::makeButton(highlight_words[i], [this, alive = alive_, i]() {
            brls::sync([this, alive, i]() {
                if (!alive->load(std::memory_order_acquire)) return;
                custom_focus_word_.clear();
                selected_word_idx_ = static_cast<int>(i);
                rebuildContent();
            });
        }, custom_focus_word_.empty() && selected_word_idx_ == static_cast<int>(i), 32.0f);
        b->setMarginRight(6);
        word_row->addView(b);
    }
    auto* lookup_row = new brls::Box(brls::Axis::ROW);
    lookup_row->setMarginTop(8);
    lookup_row->setMarginBottom(10);
    lookup_row->addView(ui::makeButton("查任意词 / 词组", [this, alive = alive_]() {
                                           if (alive->load(std::memory_order_acquire)) promptCustomWord();
                                       },
                                       !custom_focus_word_.empty(), 32.0f));
    comp->addView(lookup_row);

    const std::string focus_word =
        !custom_focus_word_.empty()
            ? custom_focus_word_
            : ((!highlight_words.empty() && selected_word_idx_ < static_cast<int>(word_cap))
            ? highlight_words[selected_word_idx_]
            : "degradation");
    const std::string raw_context_sentence = findContextSentenceForWord(active_spread_, focus_word);
    const std::string context_sentence = stripMarkdownToPlainText(raw_context_sentence);
    const std::string analysis_key = active_book_.id + "|" + toLowerCopy(focus_word);

    std::string known_phonetic;
    std::string known_etymology;
    std::string known_meaning;
    for (const auto& vc : vocab_cards_) {
        if (toLowerCopy(vc.word) == toLowerCopy(focus_word)) {
            known_phonetic = vc.phonetic;
            known_etymology = vc.etymology;
            known_meaning = vc.meaning;
            break;
        }
    }

    auto* ety_card = ui::makeCard("desktop/soft", 14.0f, 10.0f);
    ety_card->setMarginBottom(12);
    ety_card->addView(ui::makeLabel(
        "选中词条: " + focus_word + (known_phonetic.empty() ? "" : ("  " + known_phonetic)),
        17.0f, "domain/book"));
    std::ostringstream ety_oss;
    if (!known_meaning.empty()) {
        ety_oss << "- **词库释义：** " << known_meaning << "\n";
    }
    if (!known_etymology.empty()) {
        ety_oss << "- **词根词源：** " << known_etymology << "\n";
    }
    if (auto it = word_analysis_cache_.find(analysis_key); it != word_analysis_cache_.end() && !it->second.empty()) {
        ety_oss << "- **AI 语境拆解：**\n\n" << it->second << "\n\n";
    } else if (known_etymology.empty()) {
        ety_oss << "- **词根拆解：** 点击下方「AI 拆解词根与句法」即可结合当前段落语境生成词源与句法分析\n";
    }
    if (!context_sentence.empty()) {
        ety_oss << "- **语境原句：** " << context_sentence;
    }
    auto* ety_md = ui::makeMarkdown(ety_oss.str(), 14.5f, "font/grey", 1.50f);
    ety_md->setMarginTop(6);
    ety_card->addView(ety_md);
    comp->addView(ety_card);

    auto* ai_word_btn = ui::makeButton(
        action_busy_ ? "正在调用 AI 解析..." : "AI 拆解词根与句法 (llm.chat.v1)",
        [this, alive = alive_, focus_word, context_sentence]() {
            if (alive->load(std::memory_order_acquire)) analyzeFocusWord(focus_word, context_sentence);
        },
        false, 36.0f);
    ai_word_btn->setMarginBottom(8);
    comp->addView(ai_word_btn);

    if (!context_sentence.empty()) {
        const std::string skey = active_book_.id + "|" + std::to_string(active_spread_.page_number);
        if (auto sit = sentence_note_cache_.find(skey); sit != sentence_note_cache_.end() && !sit->second.empty()) {
            auto* s_card = ui::makeCard("desktop/soft", 10.0f, 8.0f);
            s_card->setMarginBottom(8);
            s_card->addView(ui::makeLabel("上下文长难句精读笔记:", 13.5f, "domain/book"));
            auto* s_md = ui::makeMarkdown(sit->second, 13.5f, "font/grey", 1.46f);
            s_md->setMarginTop(4);
            s_card->addView(s_md);
            comp->addView(s_card);
        }
        auto* interp_btn = ui::makeButton(
            action_busy_ ? "精读解析中..." : "语境长难句精读 (doc.open.ebook.v1)",
            [this, alive = alive_, context_sentence]() {
                if (alive->load(std::memory_order_acquire)) interpretCurrentSentence(context_sentence);
            },
            false, 36.0f);
        interp_btn->setMarginBottom(8);
        comp->addView(interp_btn);
    }

    auto* jump_btn = ui::makeButton("查找关联影视 / NAS 对话原声片段 (clip_jump.v1)",
                                    [this, alive = alive_, focus_word]() {
                                        if (alive->load(std::memory_order_acquire)) triggerClipJump(focus_word);
                                    }, true, 38.0f);
    comp->addView(jump_btn);

    auto* add_btn = ui::makeButton("加入 SM-2 生词复习队列", [this, alive = alive_, focus_word, context_sentence]() {
        if (alive->load(std::memory_order_acquire)) addWordToVocab(focus_word, context_sentence);
    }, false, 36.0f);
    add_btn->setMarginTop(8);
    comp->addView(add_btn);

    auto* pop_btn = ui::makeButton("划词动作浮层 (vocab / AI / 台词)", [this, alive = alive_, focus_word, context_sentence]() {
        if (!alive->load(std::memory_order_acquire)) return;
        auto& k = HostKernel::instance();
        SelectionContext sctx;
        sctx.selected_text = focus_word;
sctx.context_sentence = context_sentence;
sctx.source_title = active_book_.title + " · P." +
                    std::to_string(active_spread_.page_number) + "/" +
                    std::to_string(std::max(active_spread_.total_pages, active_book_.total_pages));
        sctx.source_id = active_book_.source_id;
        sctx.book_id = active_book_.id;
        sctx.domain = ContentDomain::Book;
        if (k.isInitialized()) {
            sctx.capabilities = &k.capabilities();
            sctx.bus = &k.bus();
            sctx.ui_tasks = &k.uiTasks();
        }
        SelectionActionPopover::show(std::move(sctx));
    }, false, 34.0f);
    pop_btn->setMarginTop(8);
    comp->addView(pop_btn);

    if (!clip_jump_query_.empty()) {
        auto* clip_box = ui::makeCard("desktop/soft", 12.0f, 8.0f);
        clip_box->setMarginTop(12);
        clip_box->addView(ui::makeLabel(
            "NAS 台词匹配: \"" + clip_jump_query_ + "\" (" +
                std::to_string(clip_jump_results_.size()) + " 条)",
            15.0f, "brls/text"));
        if (clip_jump_results_.empty()) {
            clip_box->addView(ui::makeLabel("未在已索引字幕中找到匹配对白。", 13.5f, "font/grey"));
        } else {
            for (const auto& clip : clip_jump_results_) {
                if (!clip.is_object()) continue;
                const std::string series = clip.value("series", clip.value("title", "NAS 视频"));
                const std::string ep = clip.value("episode", "");
                const double start_sec = clip.value("start_sec", 0.0);
                const double end_sec = clip.value("end_sec", start_sec + 4.0);
                const std::string line_en = clip.value("line_en", "");
                const std::string line_zh = clip.value("line_zh", "");
                const std::string play_url = clip.value("play_url", "");

                auto* c_row = ui::makeCard("desktop/surface", 10.0f, 8.0f);
                c_row->setMarginTop(8);
                c_row->addView(ui::makeLabel(
                    series + (ep.empty() ? "" : (" " + ep)) + " · " + formatClipSeconds(start_sec),
                    13.5f, "domain/media"));
                if (!line_en.empty()) {
                    c_row->addView(ui::makeLabel(line_en, 13.5f, "brls/text", 1.35f));
                }
                if (!line_zh.empty()) {
                    c_row->addView(ui::makeLabel(line_zh, 13.0f, "font/grey", 1.35f));
                }
                if (!play_url.empty()) {
                    auto* open_clip = ui::makeButton("▶ 跳播此原片片段", [series, ep, play_url, start_sec, end_sec]() {
                        if (!HostKernel::instance().isInitialized()) return;
                        const int start_sec_i = std::isfinite(start_sec) ? std::max(0, static_cast<int>(start_sec)) : 0;
                        HostKernel::instance().bus().publish("entity.media.open", {
                            {"item_id", "clip:" + series + ":" + std::to_string(start_sec_i)},
                            {"source_id", "plugin-fnos-nas"},
                            {"title", series + (ep.empty() ? "" : (" " + ep))},
                            {"kind", "nas"},
                            {"source_badge", "飞牛 NAS · 台词片段"},
                            {"play_url", play_url},
                            {"start_sec", start_sec},
                            {"end_sec", end_sec},
                        });
                    }, true, 30.0f);
                    open_clip->setMarginTop(6);
                    c_row->addView(open_clip);
                }
                clip_box->addView(c_row);
            }
        }
        comp->addView(clip_box);
    }

    split->addView(comp);
    stage_wrap->addView(split);
    stage_grid_->setStaticContent(stage_wrap);
}

void BookWorkbench::triggerClipJump(const std::string& word) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || !kernel.capabilities().has("media.clip_jump.v1")) {
        brls::Application::notify("未装载 media.clip_jump.v1 · 安装 plugin-fnos-nas 后可直达 NAS 本地字幕视频片段");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    status_label_->setText("正在检索 NAS 美剧台词片段: " + word + "...");
    loadUiInBackground(kernel.uiTasks(), "book.clip_jump", req,
        [word, book_title = active_book_.title](const auto&) {
            return HostKernel::instance().capabilities().call("media.clip_jump.v1", {
                {"query", word},
                {"book", book_title},
                {"limit", 5},
                {"auto_open_player", false},
            });
        },
        [this, alive = alive_, req, word](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            clip_jump_query_ = word;
            clip_jump_results_.clear();
            if (res.ok) {
                if (res.data.contains("clips") && res.data["clips"].is_array()) {
                    for (const auto& c : res.data["clips"]) {
                        clip_jump_results_.push_back(c);
                    }
                }
                const std::string summary = res.data.value("summary", "");
                status_label_->setText(summary.empty() ? "台词片段检索完成" : summary);
                brls::Application::notify(
                    clip_jump_results_.empty()
                        ? ("未找到 \"" + word + "\" 的影视台词片段")
                        : ("匹配到 " + std::to_string(clip_jump_results_.size()) + " 条影视台词，可点击跳播"));
            } else {
                status_label_->setText("台词检索失败: " + res.error);
                brls::Application::notify("台词检索失败: " + res.error);
            }
            rebuildContent();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消台词片段检索");
            rebuildContent();
        });
}

void BookWorkbench::renderVocabReview() {
    auto* deck = ui::makeCard("desktop/surface", 24.0f, 12.0f);
    if (vocab_cards_.empty()) {
        deck->addView(ui::makeLabel("生词本为空。", 18.0f, "font/grey"));
        stage_grid_->setStaticContent(deck);
        return;
    }

    const VocabCard& card = vocab_cards_[vocab_idx_ % vocab_cards_.size()];
    deck->addView(ui::makePill("SM-2 间隔重复队列 · " + std::to_string((vocab_idx_ % vocab_cards_.size()) + 1) +
                                   " / " + std::to_string(vocab_cards_.size()),
                               "status/ok_bg", "domain/book"));

    auto* w_lbl = ui::makeLabel(card.word + "   " + card.phonetic, 28.0f, "brls/text");
    w_lbl->setMarginTop(14);
    deck->addView(w_lbl);

    auto* ety = ui::makeLabel("词源提示: " + card.etymology, 16.0f, "font/grey");
    ety->setMarginTop(6);
    deck->addView(ety);

    if (!vocab_revealed_) {
        auto* rev = ui::makeButton("显示完整释义与书内原句", [this, alive = alive_]() {
            brls::sync([this, alive]() {
                if (!alive->load(std::memory_order_acquire)) return;
                vocab_revealed_ = true;
                rebuildContent();
            });
        }, true, 40.0f);
        rev->setMarginTop(16);
        deck->addView(rev);
    } else {
        auto* ans = ui::makeCard("desktop/soft", 16.0f, 10.0f);
        ans->setMarginTop(14);
        ans->addView(ui::makeLabel("释义: " + card.meaning, 18.0f, "brls/text"));
        auto* ctx = ui::makeLabel("原句 (" + card.source_book + "): " + card.context_sentence, 15.5f, "font/grey");
        ctx->setMarginTop(6);
        ans->addView(ctx);
        deck->addView(ans);

        auto* rating = new brls::Box(brls::Axis::ROW);
        rating->setMarginTop(14);
        struct RatingOption {
            const char* label;
            int quality;
            int days;
        };
        const RatingOption opts[] = {
            {"生疏 (1天)", 1, 1},
            {"困难 (3天)", 3, 3},
            {"掌握 (7天)", 4, 7},
            {"秒记 (14天)", 5, 14},
        };
        for (const auto& opt : opts) {
            auto* b = ui::makeButton(opt.label, [this, alive = alive_, q = opt.quality, days = opt.days]() {
                brls::sync([this, alive, q, days]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    reviewCurrentVocabCard(q, days);
                });
            }, opt.days == 7, 36.0f);
            b->setMarginRight(8);
            rating->addView(b);
        }
        deck->addView(rating);
    }

    stage_grid_->setStaticContent(deck);
}

void BookWorkbench::refineCurrentPdfPage() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;

    const bool has_pdf_cap = kernel.capabilities().has("book.bilingual_pdf.v1");
    const bool has_tr_cap = kernel.capabilities().has("llm.translate.v1");
    const bool has_chat_cap = kernel.capabilities().has("llm.chat.v1");
    if (!has_pdf_cap && !has_tr_cap && !has_chat_cap) {
        brls::Application::notify("未装载 book.bilingual_pdf.v1 或 llm 翻译插件，请先在插件市场安装");
        return;
    }

    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    pdf_single_page_mode_ = false;
    const BookItem target_book = active_book_;
    const BookSpread current_spread = active_spread_;
    const int target_page = std::max(1, current_spread.page_number);
    status_label_->setText("正在精校第 " + std::to_string(target_page) + " 页...");
    rebuildContent();

    if (has_pdf_cap) {
        const auto sources = kernel.domains().bookSources();
        loadInBackground(kernel.jobs(), "BabelDOC 精校第 " + std::to_string(target_page) + " 页", req,
            [target_book, target_page, sources](const auto& cancel) {
                nlohmann::json req = {
                    {"action", "translate_page"},
                    {"book_id", target_book.id},
                    {"id", target_book.id},
                    {"source", target_book.file_path},
                    {"page", target_page},
                    {"page_range", std::to_string(target_page)},
                };
                CapabilityResult res = HostKernel::instance().capabilities().callCancellable(
                    "book.bilingual_pdf.v1", req, cancel);
                BookSpread updated;
                if (res.ok) {
                    for (const auto& src : sources) {
                        if (cancel->isCancelled()) break;
                        if (!target_book.source_id.empty() && src->sourceId() != target_book.source_id) continue;
                        updated = src->fetchSpread(target_book.id, target_page);
                        if (!updated.book_id.empty()) break;
                    }
                    if (updated.book_id.empty()) {
                        auto sp_res = HostKernel::instance().capabilities().callCancellable(
                            "book.bilingual_pdf.v1",
                            {{"action", "fetch_spread"}, {"book_id", target_book.id}, {"page", target_page}},
                            cancel);
                        if (sp_res.ok && sp_res.data.contains("spread") && sp_res.data["spread"].is_object()) {
                            const auto& sj = sp_res.data["spread"];
                            updated.book_id = sj.value("book_id", target_book.id);
                            updated.chapter_title = sj.value("chapter_title", "");
                            updated.page_number = sj.value("page_number", target_page);
                            updated.total_pages = sj.value("total_pages", target_book.total_pages);
                            if (sj.contains("blocks") && sj["blocks"].is_array()) {
                                for (const auto& bj : sj["blocks"]) {
                                    if (!bj.is_object()) continue;
                                    BookPageBlock blk;
                                    blk.id = bj.value("id", "");
                                    blk.kind = bj.value("kind", "paragraph");
                                    blk.original = bj.value("original", "");
                                    blk.translated = bj.value("translated", "");
                                    if (bj.contains("highlighted_words") && bj["highlighted_words"].is_array()) {
                                        for (const auto& hw : bj["highlighted_words"]) {
                                            if (hw.is_string()) blk.highlighted_words.push_back(hw.template get<std::string>());
                                        }
                                    }
                                    updated.blocks.push_back(std::move(blk));
                                }
                            }
                        }
                    }
                }
                return std::pair<CapabilityResult, BookSpread>(std::move(res), std::move(updated));
            },
            [this, alive = alive_, req, target_book, target_page](auto result_pair) {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
                action_busy_ = false;
                const auto& res = result_pair.first;
                auto& updated = result_pair.second;
                if (res.ok) {
                    if (!updated.book_id.empty() && active_book_.id == target_book.id) {
                        active_spread_ = std::move(updated);
                    }
                    status_label_->setText("BabelDOC 已完成第 " + std::to_string(target_page) + " 页精校对照");
                    brls::Application::notify("BabelDOC 第 " + std::to_string(target_page) + " 页精校完成");
                } else {
                    status_label_->setText("BabelDOC 精校失败: " + res.error);
                    brls::Application::notify("BabelDOC 精校失败: " + res.error);
                }
                rebuildContent();
            },
            [this, alive = alive_, req]() {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
                action_busy_ = false;
                status_label_->setText("已取消 BabelDOC 精校");
                rebuildContent();
            });
        return;
    }

    loadInBackground(kernel.jobs(), "LLM 精译第 " + std::to_string(target_page) + " 页", req,
        [current_spread, has_tr_cap](const auto& cancel) {
            BookSpread out = current_spread;
            std::string last_err;
            bool any_ok = false;
            for (auto& blk : out.blocks) {
                if (cancel->isCancelled()) break;
                if (blk.original.empty() || blk.kind == "image") continue;
                CapabilityResult r;
                if (has_tr_cap) {
                    r = HostKernel::instance().capabilities().callCancellable("llm.translate.v1", {
                        {"text", blk.original},
                        {"source_lang", "auto"},
                        {"target_lang", "zh-CN"},
                    }, cancel);
                } else {
                    r = HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                        {"domain", "book"},
                        {"prompt", "请将以下学术段落精准译为简体中文，保留术语一致性：\n\n" + blk.original},
                    }, cancel);
                }
                if (r.ok) {
                    std::string tr = r.data.value("translation", r.data.value("reply", ""));
                    tr = extractTranslationText(tr);
                    if (!tr.empty()) {
                        blk.translated = std::move(tr);
                        any_ok = true;
                    }
                } else {
                    last_err = r.error;
                }
            }
            return std::tuple<bool, std::string, BookSpread>(any_ok, std::move(last_err), std::move(out));
        },
        [this, alive = alive_, req, target_book, target_page](auto tuple_res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            const bool ok = std::get<0>(tuple_res);
            const std::string& err = std::get<1>(tuple_res);
            auto& out = std::get<2>(tuple_res);
            if (ok && active_book_.id == target_book.id) {
                active_spread_ = std::move(out);
                status_label_->setText("已完成第 " + std::to_string(target_page) + " 页 AI 精译对照");
                brls::Application::notify("第 " + std::to_string(target_page) + " 页精译完成");
            } else {
                status_label_->setText("精译失败: " + (err.empty() ? "未返回有效译文" : err));
                brls::Application::notify("精译失败: " + (err.empty() ? "未返回有效译文" : err));
            }
            rebuildContent();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消 AI 精译");
            rebuildContent();
        });
}

void BookWorkbench::analyzeFocusWord(const std::string& word, const std::string& context_sentence) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    const bool has_vocab = kernel.capabilities().has("vocab.sm2.v1");
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    const bool has_agent = kernel.capabilities().has("ai.agent.v1");
    if (!has_chat && !has_agent) {
        brls::Application::notify("未装载 llm.chat.v1 / ai.agent.v1 插件，请先安装 plugin-magpie-agent");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    const std::string key = active_book_.id + "|" + toLowerCopy(word);
    const std::string title = active_book_.title;
    status_label_->setText("正在解析词条 \"" + word + "\" 的词根与语境句法...");
    rebuildContent();

    loadUiInBackground(kernel.uiTasks(), "book.word_analysis", req,
        [word, context_sentence, title, has_vocab, has_chat](const auto& cancel) {
            if (has_vocab) {
                auto vres = HostKernel::instance().capabilities().callCancellable("vocab.sm2.v1", {
                    {"action", "etymology"},
                    {"word", word},
                    {"book_title", title},
                    {"context_sentence", context_sentence},
                }, cancel);
                if (vres.ok && !vres.data.value("etymology", std::string{}).empty()) {
                    CapabilityResult wrapped = vres;
                    wrapped.data["reply"] = vres.data.value("etymology", "");
                    return wrapped;
                }
            }
            std::ostringstream prompt;
            prompt << "请结合以下英文书籍语境，简洁解析单词 \"" << word << "\"：\n"
                   << "1. 音标与语境释义\n"
                   << "2. 词根词缀拆解与记忆线索\n"
                   << "3. 在原句中的语法功能与搭配\n\n"
                   << "书名: " << title << "\n"
                   << "原句: " << context_sentence;
            if (has_chat) {
                return HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                    {"domain", "book"},
                    {"context", title},
                    {"prompt", prompt.str()},
                }, cancel);
            }
            return HostKernel::instance().capabilities().callCancellable("ai.agent.v1", {
                {"action", "chat"},
                {"domain", "book"},
                {"context", title},
                {"prompt", prompt.str()},
            }, cancel);
        },
        [this, alive = alive_, req, key, word](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            if (res.ok) {
                const std::string reply = res.data.value("reply", res.data.value("text", ""));
                if (!reply.empty()) {
                    word_analysis_cache_[key] = reply;
                    status_label_->setText("已完成词条 \"" + word + "\" 的语境词根解析");
                } else {
                    status_label_->setText("AI 未返回词根解析内容");
                }
            } else {
                status_label_->setText("词根解析失败: " + res.error);
                brls::Application::notify("词根解析失败: " + res.error);
            }
            rebuildContent();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消词根解析");
            rebuildContent();
        });
}

void BookWorkbench::translateCurrentEpubPage() {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized()) return;
    const bool has_ebook = kernel.capabilities().has("doc.open.ebook.v1");
    const bool has_tr = kernel.capabilities().has("llm.translate.v1");
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    if (!has_ebook && !has_tr && !has_chat) {
        brls::Application::notify("未装载翻译或 eBook 插件，请先在插件市场安装");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    const BookItem target_book = active_book_;
    const BookSpread current_spread = active_spread_;
    const int target_page = std::max(1, current_spread.page_number);
    status_label_->setText("正在翻译第 " + std::to_string(target_page) + " 页段落...");
    rebuildContent();

    if (has_ebook) {
        loadInBackground(kernel.jobs(), "eBook 双语翻译第 " + std::to_string(target_page) + " 页", req,
            [target_book, target_page](const auto& cancel) {
                return HostKernel::instance().capabilities().callCancellable("doc.open.ebook.v1", {
                    {"action", "translate_page"},
                    {"book_id", target_book.id},
                    {"page", target_page},
                }, cancel);
            },
            [this, alive = alive_, req, target_book, target_page](CapabilityResult res) {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
                action_busy_ = false;
                if (res.ok && res.data.contains("spread") && res.data["spread"].is_object() &&
                    active_book_.id == target_book.id) {
                    const auto& sj = res.data["spread"];
                    BookSpread sp;
                    sp.book_id = sj.value("book_id", target_book.id);
                    sp.chapter_title = sj.value("chapter_title", "");
                    sp.page_number = sj.value("page_number", target_page);
                    sp.total_pages = sj.value("total_pages", target_book.total_pages);
                    if (sj.contains("blocks") && sj["blocks"].is_array()) {
                        for (const auto& bj : sj["blocks"]) {
                            if (!bj.is_object()) continue;
                            BookPageBlock blk;
                            blk.id = bj.value("id", "");
                            blk.kind = bj.value("kind", "paragraph");
                            blk.original = bj.value("original", "");
                            blk.translated = bj.value("translated", "");
                            if (bj.contains("highlighted_words") && bj["highlighted_words"].is_array()) {
                                for (const auto& hw : bj["highlighted_words"]) {
                                    if (hw.is_string()) blk.highlighted_words.push_back(hw.get<std::string>());
                                }
                            }
                            sp.blocks.push_back(std::move(blk));
                        }
                    }
                    active_spread_ = std::move(sp);
                    status_label_->setText("已完成第 " + std::to_string(target_page) + " 页双语对照");
                    brls::Application::notify("本页双语对照翻译完成");
                } else {
                    status_label_->setText("本页翻译失败: " + res.error);
                    brls::Application::notify("本页翻译失败: " + res.error);
                }
                rebuildContent();
            },
            [this, alive = alive_, req]() {
                if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
                action_busy_ = false;
                status_label_->setText("已取消本页翻译");
                rebuildContent();
            });
        return;
    }

    loadInBackground(kernel.jobs(), "LLM 翻译第 " + std::to_string(target_page) + " 页", req,
        [current_spread, has_tr](const auto& cancel) {
            BookSpread out = current_spread;
            std::string last_err;
            bool any_ok = false;
            for (auto& blk : out.blocks) {
                if (cancel->isCancelled()) break;
                if (blk.original.empty() || blk.kind == "image") continue;
                CapabilityResult r = has_tr
                    ? HostKernel::instance().capabilities().callCancellable("llm.translate.v1", {
                          {"text", blk.original}, {"source_lang", "en"}, {"target_lang", "zh-CN"},
                      }, cancel)
                    : HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                          {"domain", "book"},
                          {"prompt", "请将以下英文原版书段落译为自然流畅的简体中文：\n\n" + blk.original},
                      }, cancel);
                if (r.ok) {
                    std::string tr = extractTranslationText(r.data.value("translation", r.data.value("reply", "")));
                    if (!tr.empty()) {
                        blk.translated = std::move(tr);
                        any_ok = true;
                    }
                } else {
                    last_err = r.error;
                }
            }
            return std::tuple<bool, std::string, BookSpread>(any_ok, std::move(last_err), std::move(out));
        },
        [this, alive = alive_, req, target_book, target_page](auto tup) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            if (std::get<0>(tup) && active_book_.id == target_book.id) {
                active_spread_ = std::move(std::get<2>(tup));
                status_label_->setText("已完成第 " + std::to_string(target_page) + " 页 AI 翻译");
            } else {
                status_label_->setText("翻译失败: " + std::get<1>(tup));
            }
            rebuildContent();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消 AI 翻译");
            rebuildContent();
        });
}

void BookWorkbench::interpretCurrentSentence(const std::string& sentence) {
    auto& kernel = HostKernel::instance();
    if (!kernel.isInitialized() || sentence.empty()) return;
    const bool has_ebook = kernel.capabilities().has("doc.open.ebook.v1");
    const bool has_chat = kernel.capabilities().has("llm.chat.v1");
    if (!has_ebook && !has_chat) {
        brls::Application::notify("未装载 doc.open.ebook.v1 或 llm.chat.v1");
        return;
    }
    action_request_->cancel();
    action_request_ = CancelToken::create();
    auto req = action_request_;
    action_busy_ = true;
    const std::string book_id = active_book_.id;
    const std::string skey = book_id + "|" + std::to_string(active_spread_.page_number);
    status_label_->setText("正在结合原书前后段上下文解析长难句...");
    rebuildContent();

    loadUiInBackground(kernel.uiTasks(), "book.sentence_interp", req,
        [sentence, book_id, has_ebook](const auto& cancel) {
            if (has_ebook) {
                return HostKernel::instance().capabilities().callCancellable("doc.open.ebook.v1", {
                    {"action", "interpret_sentence"},
                    {"sentence", sentence},
                    {"book_id", book_id},
                    {"presets", {"context", "syntax", "expressions"}},
                }, cancel);
            }
            return HostKernel::instance().capabilities().callCancellable("llm.chat.v1", {
                {"domain", "book"},
                {"prompt", "请拆解以下英文长难句的主干结构、地道表达并给出贴合语境的中文精译：\n\n" + sentence},
            }, cancel);
        },
        [this, alive = alive_, req, skey, sentence, has_ebook,
         target_book = active_book_, page_num = std::max(1, active_spread_.page_number),
         chap = active_spread_.chapter_title](CapabilityResult res) {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            if (res.ok) {
                std::string interp;
                if (res.data.contains("note") && res.data["note"].is_object()) {
                    interp = res.data["note"].value("ai_interpretation", "");
                } else {
                    interp = res.data.value("reply", res.data.value("text", ""));
                }
                if (!interp.empty()) {
                    sentence_note_cache_[skey] = interp;
                    if (!has_ebook || !res.data.contains("note")) {
                        beginLibraryBusSuppression();
                        BookLibraryStore::instance().addExcerpt(
                            target_book, page_num, chap, sentence, "", interp);
                        endLibraryBusSuppression();
                    }
                    cached_excerpts_ = BookLibraryStore::instance().listAllMergedExcerpts();
                    status_label_->setText("已完成长难句上下文精读并收入「笔记本 · 书摘」");
                    brls::Application::notify("长难句精读已保存至笔记本");
                }
            } else {
                status_label_->setText("长难句精读失败: " + res.error);
                brls::Application::notify("精读失败: " + res.error);
            }
            finishRefresh();
        },
        [this, alive = alive_, req]() {
            if (!alive->load(std::memory_order_acquire) || action_request_ != req) return;
            action_busy_ = false;
            status_label_->setText("已取消长难句精读");
            rebuildContent();
        });
}

void BookWorkbench::addWordToVocab(const std::string& word, const std::string& context_sentence) {
    if (word.empty()) return;
    const std::string lower_w = toLowerCopy(word);
    const std::string analysis_key = active_book_.id + "|" + lower_w;
    std::string cached_ety;
    if (auto it = word_analysis_cache_.find(analysis_key); it != word_analysis_cache_.end()) {
        cached_ety = it->second;
    }

    bool exists = false;
    for (auto& vc : vocab_cards_) {
        if (toLowerCopy(vc.word) == lower_w) {
            if (vc.context_sentence.empty() && !context_sentence.empty()) {
                vc.context_sentence = context_sentence;
            }
            if (vc.etymology.empty() && !cached_ety.empty()) {
                vc.etymology = cached_ety;
            }
            exists = true;
            break;
        }
    }
    if (!exists) {
        VocabCard card;
        card.word = word;
        card.lemma = lower_w;
        card.etymology = cached_ety.empty() ? "待 AI 拆解词根词源" : cached_ety;
        card.meaning = "源自《" + active_book_.title + "》";
        card.context_sentence = context_sentence;
        card.source_book = active_book_.title;
        vocab_cards_.push_back(std::move(card));
    }
    ++vocab_mutation_seq_;
    saveHostVocabFallback();

    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        k.bus().publish("entity.vocab.add", {
            {"word", word},
            {"book", active_book_.title},
            {"context_sentence", context_sentence},
        });
        if (k.capabilities().has("vocab.sm2.v1")) {
            loadUiInBackground(k.uiTasks(), "book.vocab_add", CancelToken::create(),
                [word, title = active_book_.title, context_sentence, cached_ety](const auto&) {
                    return HostKernel::instance().capabilities().call("vocab.sm2.v1", {
                        {"action", "add"},
                        {"word", word},
                        {"source_book", title},
                        {"context_sentence", context_sentence},
                        {"etymology", cached_ety},
                    });
                },
                [alive = alive_](CapabilityResult) {});
        }
    }

    brls::Application::notify(exists ? ("生词已在复习队列中: " + word) : ("已添加至 SM-2 生词本: " + word));
    finishRefresh();
}

void BookWorkbench::reviewCurrentVocabCard(int quality, int selected_days) {
    if (vocab_cards_.empty()) return;
    VocabCard& card = vocab_cards_[vocab_idx_ % vocab_cards_.size()];

    if (quality < 3) {
        card.repetitions = 0;
        card.interval_days = 1;
    } else {
        card.repetitions += 1;
        card.interval_days = selected_days;
    }
    const double qd = static_cast<double>(quality);
    const double new_ef = card.ease_factor + (0.1 - (5.0 - qd) * (0.08 + (5.0 - qd) * 0.02));
    card.ease_factor = std::max(1.3, std::round(new_ef * 100.0) / 100.0);

    const std::string reviewed_word = card.word;
    const int final_interval = card.interval_days;
    const int final_reps = card.repetitions;
    const double final_ef = card.ease_factor;

    ++vocab_mutation_seq_;
    saveHostVocabFallback();

    if (HostKernel::instance().isInitialized()) {
        auto& k = HostKernel::instance();
        if (k.capabilities().has("vocab.sm2.v1")) {
            loadUiInBackground(k.uiTasks(), "book.vocab_review", CancelToken::create(),
                [reviewed_word, quality, final_interval, final_reps, final_ef](const auto&) {
                    return HostKernel::instance().capabilities().call("vocab.sm2.v1", {
                        {"action", "review"},
                        {"word", reviewed_word},
                        {"quality", quality},
                        {"interval_days", final_interval},
                        {"repetitions", final_reps},
                        {"ease_factor", final_ef},
                    });
                },
                [alive = alive_](CapabilityResult) {});
        }
    }

    vocab_idx_ = (vocab_idx_ + 1) % vocab_cards_.size();
    vocab_revealed_ = false;
    rebuildContent();
}

void BookWorkbench::mergeHostVocabFallback() {
    auto& k = HostKernel::instance();
    if (!k.isInitialized()) return;
    const nlohmann::json saved = k.config().loadPluginConfig("host-book-vocab");
    if (!saved.is_object() || !saved.contains("cards") || !saved["cards"].is_array()) {
        return;
    }
    for (const auto& j : saved["cards"]) {
        if (!j.is_object()) continue;
        const std::string w = j.value("word", "");
        if (w.empty()) continue;
        const std::string lw = toLowerCopy(w);
        bool updated = false;
        for (auto& existing : vocab_cards_) {
            if (toLowerCopy(existing.word) == lw) {
                existing.interval_days = j.value("interval_days", existing.interval_days);
                existing.repetitions = j.value("repetitions", existing.repetitions);
                existing.ease_factor = j.value("ease_factor", existing.ease_factor);
                updated = true;
                break;
            }
        }
        if (!updated) {
            VocabCard c;
            c.word = w;
            c.lemma = j.value("lemma", lw);
            c.phonetic = j.value("phonetic", "");
            c.etymology = j.value("etymology", "");
            c.meaning = j.value("meaning", "");
            c.context_sentence = j.value("context_sentence", "");
            c.source_book = j.value("source_book", "");
            c.interval_days = j.value("interval_days", 1);
            c.repetitions = j.value("repetitions", 0);
            c.ease_factor = j.value("ease_factor", 2.5);
            vocab_cards_.push_back(std::move(c));
        }
    }
}

void BookWorkbench::saveHostVocabFallback() const {
    auto& k = HostKernel::instance();
    if (!k.isInitialized()) return;
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& c : vocab_cards_) {
        arr.push_back({
            {"word", c.word},
            {"lemma", c.lemma},
            {"phonetic", c.phonetic},
            {"etymology", c.etymology},
            {"meaning", c.meaning},
            {"context_sentence", c.context_sentence},
            {"source_book", c.source_book},
            {"interval_days", c.interval_days},
            {"repetitions", c.repetitions},
            {"ease_factor", c.ease_factor},
        });
    }
    k.config().savePluginConfig("host-book-vocab", {{"cards", std::move(arr)}});
}

void BookWorkbench::beginLibraryBusSuppression() {
    ++suppress_library_bus_depth_;
    suppress_library_bus_.store(true, std::memory_order_release);
}

void BookWorkbench::endLibraryBusSuppression() {
    if (suppress_library_bus_depth_ == 0) return;
    --suppress_library_bus_depth_;
    if (suppress_library_bus_depth_ != 0) return;

    const uint64_t generation = ++suppress_library_bus_generation_;
    auto alive = alive_;
    brls::sync([this, alive, generation]() {
        if (!alive->load(std::memory_order_acquire)) return;
        if (suppress_library_bus_depth_ != 0 ||
            suppress_library_bus_generation_ != generation) {
            return;
        }
        suppress_library_bus_.store(false, std::memory_order_release);
    });
}

void BookWorkbench::openExternalBook(const nlohmann::json& payload) {
    if (!payload.is_object()) return;
    if (payload.value("sub_mode", "") == "vocab") {
        const std::string target_word = toLowerCopy(payload.value("word", ""));
        if (!target_word.empty()) {
            for (size_t i = 0; i < vocab_cards_.size(); ++i) {
                if (toLowerCopy(vocab_cards_[i].word) == target_word) {
                    vocab_idx_ = i;
                    break;
                }
            }
        }
        sub_mode_ = SubMode::VocabReview;
        vocab_revealed_ = !target_word.empty();
        finishRefresh();
        return;
    }
    const std::string bid = payload.value("book_id", payload.value("id", ""));
    std::string file_path = payload.value("file_path", payload.value("source", payload.value("path", "")));
    {
        auto b = file_path.find_first_not_of(" \t\r\n");
        auto e = file_path.find_last_not_of(" \t\r\n");
        file_path = (b == std::string::npos) ? std::string{} : file_path.substr(b, e - b + 1);
        if (file_path.size() >= 2 &&
            ((file_path.front() == '"' && file_path.back() == '"') ||
             (file_path.front() == '\'' && file_path.back() == '\''))) {
            file_path = file_path.substr(1, file_path.size() - 2);
        }
        if (file_path.rfind("file://", 0) == 0) {
            file_path.erase(0, 7);
        }
        if (!file_path.empty() && file_path[0] == '~' && (file_path.size() == 1 || file_path[1] == '/')) {
            if (const char* home = std::getenv("HOME")) {
                file_path = std::string(home) + file_path.substr(1);
            }
        }
    }
    const std::string title = payload.value("title", "");
    const std::string fmt = toLowerCopy(payload.value("format", ""));
    const int page = std::max(1, payload.value("page", 1));
    const std::string source_id = payload.value("source_id", "");

    BookItem matched;
    bool found = false;
    for (const auto& b : books_) {
        if (!source_id.empty() && b.source_id != source_id) continue;
        if ((!bid.empty() && b.id == bid) ||
            (!file_path.empty() && b.file_path == file_path)) {
            matched = b;
            found = true;
            break;
        }
    }
    const bool curator_request = source_id == "plugin-ai-curator" ||
        (found && matched.source_id == "plugin-ai-curator") ||
        (source_id.empty() && (bid.rfind("english-date-", 0) == 0 || bid == "english-archive-all"));
    if (!found) {
        matched.id = !bid.empty() ? bid : ("ext-book:" + file_path);
        matched.source_id = curator_request ? "plugin-ai-curator" : source_id;
        matched.file_path = file_path;
        if (!fmt.empty()) {
            matched.format = fmt;
        } else {
            std::string ext = toLowerCopy(std::filesystem::path(file_path).extension().string());
            if (!ext.empty() && ext.front() == '.') ext.erase(ext.begin());
            if (ext == "azw" || ext == "prc") ext = "mobi";
            if (ext == "md") ext = "txt";
            matched.format = ext.empty() ? "epub" : ext;
        }
        if (!title.empty()) {
            matched.title = title;
        } else if (!file_path.empty()) {
            std::string clean_t;
            std::string clean_a;
            BookLibraryStore::parseCleanFilenameTitleAuthor(
                std::filesystem::path(file_path).stem().string(), "", clean_t, clean_a);
            matched.title = clean_t;
            if (!clean_a.empty()) matched.author = clean_a;
        } else {
            matched.title = "外部电子书";
        }
        matched.current_page = page;
        matched.total_pages = std::max(page, payload.value("total_pages", 1));
    } else {
        matched.current_page = page;
    }

    if (payload.value("sub_mode", "") == "notebook") {
        notebook_book_key_ = BookLibraryStore::computeBookKey(matched);
        sub_mode_ = SubMode::Notebook;
        finishRefresh();
        return;
    }

    if (!curator_request && !file_path.empty()) {
        std::error_code ec;
        if (std::filesystem::exists(file_path, ec)) {
            beginLibraryBusSuppression();
            BookLibraryStore::instance().registerImportedFile(file_path);
            endLibraryBusSuppression();
        }
    }

    auto& kernel = HostKernel::instance();
    if (!found && !curator_request && !file_path.empty() && matched.format == "pdf" &&
        kernel.isInitialized() && kernel.capabilities().has("book.bilingual_pdf.v1")) {
        action_request_->cancel();
        action_request_ = CancelToken::create();
        loadInBackground(kernel.jobs(), "导入 PDF 到书架", action_request_,
            [file_path, title, page](const auto& cancel) {
                auto res = HostKernel::instance().capabilities().callCancellable("book.bilingual_pdf.v1", {
                    {"action", "import_pdf"},
                    {"source", file_path},
                    {"title", title},
                }, cancel);
                nlohmann::json sp_data;
                std::string resolved_id = res.data.value("book_id", "");
                if (res.ok && !resolved_id.empty()) {
                    auto sp = HostKernel::instance().capabilities().callCancellable("book.bilingual_pdf.v1", {
                        {"action", "fetch_spread"},
                        {"book_id", resolved_id},
                        {"page", page},
                    }, cancel);
                    if (sp.ok) sp_data = std::move(sp.data);
                }
                return std::pair<CapabilityResult, nlohmann::json>(std::move(res), std::move(sp_data));
            },
            [this, alive = alive_, matched, page](auto rp) mutable {
                if (!alive->load(std::memory_order_acquire)) return;
                const auto& res = rp.first;
                const auto& sp_data = rp.second;
                if (res.ok && !res.data.value("book_id", std::string{}).empty()) {
                    matched.id = res.data.value("book_id", matched.id);
                    matched.source_id = "plugin-pdf-babeldoc";
                    matched.total_pages = std::max(matched.total_pages, res.data.value("total_pages", matched.total_pages));
                }
                active_book_ = matched;
                has_active_book_ = true;
                sub_mode_ = SubMode::PdfBabelDoc;
                if (sp_data.contains("spread") && sp_data["spread"].is_object()) {
                    const auto& sj = sp_data["spread"];
                    BookSpread sp;
                    sp.book_id = sj.value("book_id", matched.id);
                    sp.chapter_title = sj.value("chapter_title", "");
                    sp.page_number = sj.value("page_number", page);
                    sp.total_pages = sj.value("total_pages", matched.total_pages);
                    if (sj.contains("blocks") && sj["blocks"].is_array()) {
                        for (const auto& bj : sj["blocks"]) {
                            if (!bj.is_object()) continue;
                            BookPageBlock blk;
                            blk.id = bj.value("id", "");
                            blk.kind = bj.value("kind", "paragraph");
                            blk.original = bj.value("original", "");
                            blk.translated = bj.value("translated", "");
                            sp.blocks.push_back(std::move(blk));
                        }
                    }
                    active_spread_ = std::move(sp);
                    ensureLocalPdfPageRendered(std::max(1, active_spread_.page_number));
                } else {
                    loadSpread(page);
                }
                beginLibraryBusSuppression();
                BookLibraryStore::instance().touchBookRead(
                    active_book_, std::max(1, active_spread_.page_number),
                    std::max(1, std::max(active_spread_.total_pages, active_book_.total_pages)),
                    active_spread_.chapter_title);
                endLibraryBusSuppression();
                if (on_entity_focus_) {
                    on_entity_focus_(active_book_.title + " · P." +
                                     std::to_string(active_spread_.page_number) + "/" +
                                     std::to_string(std::max(active_spread_.total_pages,
                                                             active_book_.total_pages)));
                }
                refresh();
            });
        return;
    }

    if (!found && !curator_request && !file_path.empty() && matched.format != "pdf" &&
        kernel.isInitialized() && kernel.capabilities().has("doc.open.ebook.v1")) {
        action_request_->cancel();
        action_request_ = CancelToken::create();
        loadInBackground(kernel.jobs(), "导入电子书到书架", action_request_,
            [file_path, page](const auto& cancel) {
                return HostKernel::instance().capabilities().callCancellable("doc.open.ebook.v1", {
                    {"action", "import_ebook"},
                    {"file_path", file_path},
                    {"page", page},
                }, cancel);
            },
            [this, alive = alive_, matched, page](CapabilityResult res) mutable {
                if (!alive->load(std::memory_order_acquire)) return;
                if (res.ok && !res.data.value("book_id", std::string{}).empty()) {
                    matched.id = res.data.value("book_id", matched.id);
                    matched.source_id = "plugin-ebook";
                    matched.total_pages = std::max(matched.total_pages, res.data.value("total_pages", matched.total_pages));
                }
                active_book_ = matched;
                has_active_book_ = true;
                sub_mode_ = SubMode::EpubCompanion;
                if (res.ok && res.data.contains("spread") && res.data["spread"].is_object()) {
                    const auto& sj = res.data["spread"];
                    BookSpread sp;
                    sp.book_id = sj.value("book_id", matched.id);
                    sp.chapter_title = sj.value("chapter_title", "");
                    sp.page_number = sj.value("page_number", page);
                    sp.total_pages = sj.value("total_pages", matched.total_pages);
                    if (sj.contains("blocks") && sj["blocks"].is_array()) {
                        for (const auto& bj : sj["blocks"]) {
                            if (!bj.is_object()) continue;
                            BookPageBlock blk;
                            blk.id = bj.value("id", "");
                            blk.kind = bj.value("kind", "paragraph");
                            blk.original = bj.value("original", "");
                            blk.translated = bj.value("translated", "");
                            if (bj.contains("highlighted_words") && bj["highlighted_words"].is_array()) {
                                for (const auto& hw : bj["highlighted_words"]) {
                                    if (hw.is_string()) blk.highlighted_words.push_back(hw.get<std::string>());
                                }
                            }
                            sp.blocks.push_back(std::move(blk));
                        }
                    }
                    active_spread_ = std::move(sp);
                } else {
                    loadSpread(page);
                }
                beginLibraryBusSuppression();
                BookLibraryStore::instance().touchBookRead(
                    active_book_, std::max(1, active_spread_.page_number),
                    std::max(1, std::max(active_spread_.total_pages, active_book_.total_pages)),
                    active_spread_.chapter_title);
                endLibraryBusSuppression();
                if (on_entity_focus_) {
                    on_entity_focus_(active_book_.title + " · P." +
                                     std::to_string(active_spread_.page_number) + "/" +
                                     std::to_string(std::max(active_spread_.total_pages,
                                                             active_book_.total_pages)));
                }
                refresh();
            });
        return;
    }

    active_book_ = matched;
    has_active_book_ = true;
    sub_mode_ = (matched.format == "pdf") ? SubMode::PdfBabelDoc : SubMode::EpubCompanion;
    if (on_entity_focus_) {
        on_entity_focus_(matched.title + " · P." + std::to_string(page) + "/" +
                         std::to_string(matched.total_pages));
    }
    loadSpread(page);
    finishRefresh();
}

void BookWorkbench::promptSearch() {
    const bool daily = sub_mode_ == SubMode::DailyEnglish;
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_, daily](std::string q) {
                brls::sync([this, alive, daily, q = std::move(q)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (daily) english_search_query_ = q;
                    else search_query_ = q;
                    refresh();
                });
            },
            daily ? "检索每日英语" : "检索本地与 NAS 书库",
            daily ? "按课程名称或日期过滤" : "支持按书名、作者或格式过滤",
            128,
            daily ? english_search_query_ : search_query_);
    }
}

void BookWorkbench::promptJumpPage() {
    if (auto* plat = brls::Application::getPlatform()) {
        const int max_p = std::max(1, active_spread_.total_pages);
        plat->getImeManager()->openForText(
            [this, alive = alive_, max_p](std::string val) {
                brls::sync([this, alive, val = std::move(val), max_p]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    try {
                        int target = std::clamp(std::stoi(val), 1, max_p);
                        selected_word_idx_ = 0;
                        custom_focus_word_.clear();
                        clip_jump_query_.clear();
                        clip_jump_results_.clear();
                        loadSpread(target);
                        rebuildContent();
                    } catch (...) {
                        brls::Application::notify("请输入有效页码 (1-" + std::to_string(max_p) + ")");
                    }
                });
            },
            "跳转页码 (1-" + std::to_string(max_p) + ")",
            "输入目标页码",
            16,
            std::to_string(std::max(1, active_spread_.page_number)));
    }
}

void BookWorkbench::promptCustomWord() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string w) {
                brls::sync([this, alive, w = std::move(w)]() {
                    if (!alive->load(std::memory_order_acquire)) return;
                    if (!w.empty()) {
                        custom_focus_word_ = w;
                        rebuildContent();
                    }
                });
            },
            "查词与句法/词源拆解",
            "输入任意英文单词或短语",
            64,
            custom_focus_word_);
    }
}

void BookWorkbench::applyDateFilter(const std::string& date) {
    if (date.empty()) return;
    english_selected_date_ = date;
    english_date_mode_ = EnglishDateMode::Selected;
    sub_mode_ = SubMode::DailyEnglish;
    refresh();
}

void BookWorkbench::promptDateFilter() {
    const std::string init_date = english_date_mode_ == EnglishDateMode::Selected ? english_selected_date_ : english_latest_date_;
    CurationCalendarDialog::open({
        .initial_date = init_date,
        .initial_domain = "english",
        .on_apply_date_filter = [this, alive = alive_](const std::string& d) {
            if (!alive->load(std::memory_order_acquire)) return;
            applyDateFilter(d);
        },
        .on_clear_date_filter = [this, alive = alive_]() {
            if (!alive->load(std::memory_order_acquire)) return;
            english_date_mode_ = EnglishDateMode::All;
            sub_mode_ = SubMode::DailyEnglish;
            refresh();
        },
    });
}

void BookWorkbench::promptCreateGroup() {
    if (auto* plat = brls::Application::getPlatform()) {
        plat->getImeManager()->openForText(
            [this, alive = alive_](std::string name) {
                brls::sync([this, alive, name = std::move(name)]() {
                    if (!alive->load(std::memory_order_acquire) || name.empty()) return;
                    beginLibraryBusSuppression();
                    const auto g = BookLibraryStore::instance().createGroup(name);
                    endLibraryBusSuppression();
                    if (!g.id.empty()) {
                        group_filter_ = g.id;
                        brls::Application::notify("已创建书架分组: " + g.name);
                        rebuildContent();
                    }
                });
            },
            "新建微信读书式书架分组",
            "例如：计算机系统 / 物理与数学 / 英文原版 / 论文精读",
            64,
            "");
    }
}

void BookWorkbench::promptBatchMoveGroup(const std::vector<BookItem>& selected_books) {
    if (selected_books.empty()) return;
    auto* backdrop = new brls::Box(brls::Axis::COLUMN);
    backdrop->setWidthPercentage(100);
    backdrop->setHeightPercentage(100);
    backdrop->setJustifyContent(brls::JustifyContent::CENTER);
    backdrop->setAlignItems(brls::AlignItems::CENTER);
    backdrop->setBackgroundColor(nvgRGBA(10, 14, 22, 185));

    auto* card = ui::makeCard("desktop/surface", 22.0f, 14.0f);
    card->setWidth(520.0f);
    card->addView(ui::makeLabel(
        "将已选 " + std::to_string(selected_books.size()) + " 本书移入分组",
        19.0f, "brls/text"));

    auto* grp_wrap = new brls::Box(brls::Axis::ROW);
    wrapFlexRow(grp_wrap);
    grp_wrap->setMarginTop(14);
    grp_wrap->setMarginBottom(14);

    auto applyMove = [this, alive = alive_, selected_books](const std::string& gid, const std::string& gname) {
        brls::Application::popActivity();
        brls::sync([this, alive, selected_books, gid, gname]() {
            if (!alive->load(std::memory_order_acquire)) return;
            beginLibraryBusSuppression();
            BookLibraryStore::instance().setBooksGroup(selected_books, gid);
            endLibraryBusSuppression();
            batch_selected_keys_.clear();
            batch_mode_ = false;
            group_filter_ = gid.empty() ? "__ungrouped__" : gid;
            brls::Application::notify("已移入分组: " + (gname.empty() ? "未分组" : gname));
            rebuildContent();
        });
    };

    auto* ungrp = ui::makeButton("移回「未分组」", [applyMove]() { applyMove("", "未分组"); }, false, 34.0f);
    ungrp->setMarginRight(8);
    ungrp->setMarginBottom(8);
    grp_wrap->addView(ungrp);

    for (const auto& g : BookLibraryStore::instance().listGroups()) {
        auto* gb = ui::makeButton(g.name, [applyMove, gid = g.id, gname = g.name]() {
            applyMove(gid, gname);
        }, true, 34.0f);
        gb->setMarginRight(8);
        gb->setMarginBottom(8);
        grp_wrap->addView(gb);
    }

    auto* create_and_move = ui::makeButton("+ 新建分组并移入...", [this, alive = alive_, selected_books]() {
        if (auto* plat = brls::Application::getPlatform()) {
            plat->getImeManager()->openForText(
                [this, alive, selected_books](std::string name) {
                    brls::sync([this, alive, selected_books, name = std::move(name)]() {
                        if (!alive->load(std::memory_order_acquire) || name.empty()) return;
                        brls::Application::popActivity();
                        beginLibraryBusSuppression();
                        const auto g = BookLibraryStore::instance().createGroup(name);
                        if (!g.id.empty()) {
                            BookLibraryStore::instance().setBooksGroup(selected_books, g.id);
                            group_filter_ = g.id;
                        }
                        endLibraryBusSuppression();
                        batch_selected_keys_.clear();
                        batch_mode_ = false;
                        rebuildContent();
                    });
                },
                "新建分组并移入所选书籍", "输入新分组名称", 64, "");
        }
    }, false, 34.0f);
    create_and_move->setMarginBottom(8);
    grp_wrap->addView(create_and_move);
    card->addView(grp_wrap);

    auto* cancel_row = new brls::Box(brls::Axis::ROW);
    cancel_row->setJustifyContent(brls::JustifyContent::FLEX_END);
    cancel_row->addView(ui::makeButton("取消", []() { brls::Application::popActivity(); }, false, 34.0f));
    card->addView(cancel_row);

    backdrop->addView(card);
    backdrop->registerAction("关闭", brls::ControllerButton::BUTTON_B, [](brls::View*) {
        brls::Application::popActivity();
        return true;
    });
    auto* act = new brls::Activity(backdrop);
    brls::Application::pushActivity(act, brls::TransitionAnimation::FADE);
}

void BookWorkbench::importSelectedLocalPaths(const std::vector<std::string>& paths, bool open_first) {
    if (paths.empty()) {
        refresh();
        return;
    }
    const std::string target_gid =
        (group_filter_ != "__all__" && group_filter_ != "__ungrouped__") ? group_filter_ : "";

    auto& kernel = HostKernel::instance();
    const bool has_ebook = kernel.isInitialized() && kernel.capabilities().has("doc.open.ebook.v1");
    const bool has_pdf = kernel.isInitialized() && kernel.capabilities().has("book.bilingual_pdf.v1");

    beginLibraryBusSuppression();
    for (const auto& p : paths) {
        BookLibraryStore::instance().registerImportedFile(p, target_gid);
    }
    endLibraryBusSuppression();

    if (open_first && paths.size() == 1) {
        openExternalBook({{"file_path", paths.front()}, {"page", 1}});
        return;
    }

    if (kernel.isInitialized() && (has_ebook || has_pdf)) {
        loadInBackground(kernel.jobs(), "批量导入本地藏书 (" + std::to_string(paths.size()) + " 本)", CancelToken::create(),
            [paths, has_ebook, has_pdf](const auto& cancel) {
                for (const auto& p : paths) {
                    if (cancel->isCancelled()) break;
                    std::string ext = toLowerCopy(std::filesystem::path(p).extension().string());
                    if (ext == ".pdf" && has_pdf) {
                        HostKernel::instance().capabilities().callCancellable(
                            "book.bilingual_pdf.v1", {{"action", "import_pdf"}, {"source", p}}, cancel);
                    } else if (ext != ".pdf" && has_ebook) {
                        HostKernel::instance().capabilities().callCancellable(
                            "doc.open.ebook.v1", {{"action", "import_ebook"}, {"file_path", p}, {"page", 1}}, cancel);
                    }
                }
                return paths.size();
            },
            [this, alive = alive_, open_first, first_path = paths.front()](size_t count) {
                if (!alive->load(std::memory_order_acquire)) return;
                brls::Application::notify("已导入 " + std::to_string(count) + " 本书籍至书架");
                if (open_first && !first_path.empty()) {
                    openExternalBook({{"file_path", first_path}, {"page", 1}});
                } else {
                    refresh();
                }
            });
        return;
    }

    brls::Application::notify("已加入书架: " + std::to_string(paths.size()) + " 本书籍");
    refresh();
}

void BookWorkbench::promptImportLocalBook() {
    const std::string target_gid =
        (group_filter_ != "__all__" && group_filter_ != "__ungrouped__") ? group_filter_ : "";
    openWeReadImportDialog(
        target_gid,
        [this, alive = alive_](const std::vector<std::string>& paths, bool open_first) {
            if (!alive->load(std::memory_order_acquire)) return;
            importSelectedLocalPaths(paths, open_first);
        });
}

} // namespace omnidesk::host
