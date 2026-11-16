#include "omnidesk/host/recycling_grid.hpp"

#include <algorithm>
#include <cmath>

namespace omnidesk::host {

namespace {

bool isFocusInside(brls::View* subtree) {
    if (!subtree) return false;
    for (brls::View* cur = brls::Application::getCurrentFocus(); cur != nullptr;
         cur = cur->getParent()) {
        if (cur == subtree) {
            return true;
        }
    }
    return false;
}

void destroyDetachedView(brls::View* v) {
    if (!v) return;
    v->setParent(nullptr);
    if (v->isPtrLocked()) {
        v->freeView();
    } else {
        delete v;
    }
}

}  // namespace

// ============================================================================
// RecyclingGridCell
// ============================================================================

RecyclingGridCell::RecyclingGridCell(brls::Axis axis) : brls::Box(axis) {
    setShrink(0.0f);
}

// ============================================================================
// Internal Row & Container Boxes
// ============================================================================

class RecyclingGrid::GridRowBox final : public brls::Box {
public:
    GridRowBox() : brls::Box(brls::Axis::ROW) {
        setWidthPercentage(100);
        setShrink(0.0f);
        setAlignItems(brls::AlignItems::STRETCH);
    }

    void setSuppressInvalidate(bool suppress) { suppress_invalidate_ = suppress; }

    void invalidate() override {
        if (!suppress_invalidate_) {
            brls::Box::invalidate();
        }
    }

    void onChildFocusGained(brls::View* directChild, brls::View* focusedView) override {
        const auto& kids = getChildren();
        for (size_t i = 0; i < kids.size(); ++i) {
            if (kids[i] == directChild) {
                last_focused_col_ = i;
                break;
            }
        }
        brls::Box::onChildFocusGained(directChild, focusedView);
    }

    size_t lastFocusedCol() const { return last_focused_col_; }

    void detachCell(brls::View* cell) {
        if (!cell) return;
        if (this->getLastFocusedView() == cell) {
            this->setLastFocusedView(nullptr);
        }
        const bool prev = suppress_invalidate_;
        suppress_invalidate_ = true;
        brls::Box::removeView(cell, false);
        cell->setParent(nullptr);
        suppress_invalidate_ = prev;
    }

private:
    bool suppress_invalidate_ = false;
    size_t last_focused_col_ = 0;
};

class RecyclingGrid::CellsContainerBox final : public brls::Box {
public:
    explicit CellsContainerBox(RecyclingGrid* owner)
        : brls::Box(brls::Axis::COLUMN), owner_(owner) {
        setWidthPercentage(100);
        setShrink(0.0f);
    }

    void setBatchUpdating(bool batch) { batch_updating_ = batch; }

    void invalidate() override {
        if (!batch_updating_) {
            brls::Box::invalidate();
        }
    }

    void detachRow(brls::View* row_view) {
        if (!row_view) return;
        if (isFocusInside(row_view)) {
            brls::Application::giveFocus(nullptr);
        }
        if (this->getLastFocusedView() == row_view) {
            this->setLastFocusedView(nullptr);
        }
        brls::Box::removeView(row_view, false);
        row_view->setParent(nullptr);
    }

    brls::View* getParentNavigationDecision(brls::View* from,
                                            brls::View* newFocus,
                                            brls::FocusDirection direction) override {
        if (owner_ && owner_->getSpanCount() > 1 && newFocus &&
            (direction == brls::FocusDirection::UP ||
             direction == brls::FocusDirection::DOWN)) {
            auto* from_row = dynamic_cast<GridRowBox*>(from);
            GridRowBox* target_row = nullptr;
            for (brls::View* cur = newFocus; cur != nullptr && cur != this;
                 cur = cur->getParent()) {
                if (auto* r = dynamic_cast<GridRowBox*>(cur)) {
                    target_row = r;
                    break;
                }
            }
            if (from_row && target_row && !target_row->getChildren().empty()) {
                const size_t col =
                    std::min(from_row->lastFocusedCol(), target_row->getChildren().size() - 1);
                if (brls::View* candidate = target_row->getChildren()[col]->getDefaultFocus()) {
                    return candidate;
                }
            }
        }
        return brls::Box::getParentNavigationDecision(from, newFocus, direction);
    }

private:
    RecyclingGrid* owner_ = nullptr;
    bool batch_updating_ = false;
};

// ============================================================================
// RecyclingGrid
// ============================================================================

RecyclingGrid::RecyclingGrid() {
    setScrollingBehavior(brls::ScrollingBehavior::NATURAL);

    content_root_ = new brls::Box(brls::Axis::COLUMN);
    content_root_->setWidthPercentage(100);

    static_box_ = new brls::Box(brls::Axis::COLUMN);
    static_box_->setWidthPercentage(100);
    static_box_->setVisibility(brls::Visibility::GONE);
    content_root_->addView(static_box_);

    virtual_wrap_ = new brls::Box(brls::Axis::COLUMN);
    virtual_wrap_->setWidthPercentage(100);

    header_box_ = new brls::Box(brls::Axis::COLUMN);
    header_box_->setWidthPercentage(100);
    header_box_->setVisibility(brls::Visibility::GONE);
    virtual_wrap_->addView(header_box_);

    empty_box_ = new brls::Box(brls::Axis::COLUMN);
    empty_box_->setWidthPercentage(100);
    empty_box_->setVisibility(brls::Visibility::GONE);
    virtual_wrap_->addView(empty_box_);

    cells_box_ = new CellsContainerBox(this);
    virtual_wrap_->addView(cells_box_);

    footer_box_ = new brls::Box(brls::Axis::COLUMN);
    footer_box_->setWidthPercentage(100);
    footer_box_->setVisibility(brls::Visibility::GONE);
    virtual_wrap_->addView(footer_box_);

    content_root_->addView(virtual_wrap_);
    setContentView(content_root_);
}

RecyclingGrid::~RecyclingGrid() {
    if (owns_source_ && data_source_) {
        delete data_source_;
        data_source_ = nullptr;
    }
    for (auto& kv : reuse_pool_) {
        for (RecyclingGridCell* cell : kv.second) {
            destroyDetachedView(cell);
        }
        kv.second.clear();
    }
    for (GridRowBox* row_box : row_box_pool_) {
        destroyDetachedView(row_box);
    }
    row_box_pool_.clear();
}

void RecyclingGrid::registerCell(const std::string& id,
                                 std::function<RecyclingGridCell*()> allocator) {
    allocators_[id] = std::move(allocator);
    reuse_pool_.try_emplace(id);
}

RecyclingGridCell* RecyclingGrid::dequeueReusableCell(const std::string& id) {
    auto pool_it = reuse_pool_.find(id);
    if (pool_it != reuse_pool_.end() && !pool_it->second.empty()) {
        RecyclingGridCell* cell = pool_it->second.back();
        pool_it->second.pop_back();
        ++reused_cell_count_;
        return cell;
    }

    auto alloc_it = allocators_.find(id);
    if (alloc_it == allocators_.end() || !alloc_it->second) {
        return nullptr;
    }
    RecyclingGridCell* cell = alloc_it->second();
    if (cell) {
        cell->setReuseId(id);
        ++allocated_cell_count_;
    }
    return cell;
}

void RecyclingGrid::setDataSource(RecyclingGridDataSource* source, bool own_source) {
    if (data_source_ != source && owns_source_ && data_source_) {
        delete data_source_;
    }
    data_source_ = source;
    owns_source_ = own_source;
    reloadData(true);
}

void RecyclingGrid::setSpanCount(int span) {
    const int clamped = std::max(1, span);
    if (span_count_ == clamped) return;
    span_count_ = clamped;
    if (!static_mode_ && data_source_) {
        reloadData(false);
    }
}

void RecyclingGrid::setEstimatedRowHeight(float height) {
    const float clamped = std::max(16.0f, height);
    if (std::abs(estimated_row_height_ - clamped) < 0.5f) return;
    estimated_row_height_ = clamped;
}

void RecyclingGrid::setEstimatedRowSpace(float space) {
    estimated_row_space_ = std::max(0.0f, space);
}

void RecyclingGrid::setColSpace(float space) {
    col_space_ = std::max(0.0f, space);
}

void RecyclingGrid::setPreFetchLine(int lines) {
    prefetch_lines_ = std::clamp(lines, 1, 8);
}

void RecyclingGrid::setContentPadding(float top, float right, float bottom, float left) {
    pad_top_ = top;
    pad_right_ = right;
    pad_bottom_ = bottom;
    pad_left_ = left;
    if (content_root_) {
        content_root_->setPadding(top, right, bottom, left);
    }
}

void RecyclingGrid::setHeaderView(brls::View* view) {
    if (!header_box_) return;
    header_box_->clearViews();
    if (view) {
        header_box_->addView(view);
        header_box_->setVisibility(brls::Visibility::VISIBLE);
    } else {
        header_box_->setVisibility(brls::Visibility::GONE);
    }
    if (content_root_) content_root_->invalidate();
}

void RecyclingGrid::setFooterView(brls::View* view) {
    if (!footer_box_) return;
    footer_box_->clearViews();
    if (view) {
        footer_box_->addView(view);
        footer_box_->setVisibility(brls::Visibility::VISIBLE);
    } else {
        footer_box_->setVisibility(brls::Visibility::GONE);
    }
    if (content_root_) content_root_->invalidate();
}

void RecyclingGrid::setEmptyView(brls::View* view) {
    if (!empty_box_) return;
    empty_box_->clearViews();
    if (view) {
        empty_box_->addView(view);
    }
    const bool show_empty = !static_mode_ && cached_item_count_ == 0 &&
                            !empty_box_->getChildren().empty();
    empty_box_->setVisibility(show_empty ? brls::Visibility::VISIBLE
                                         : brls::Visibility::GONE);
    if (content_root_) content_root_->invalidate();
}

void RecyclingGrid::setStaticContent(brls::View* view) {
    unmountAllRows();
    static_mode_ = true;
    if (virtual_wrap_) {
        virtual_wrap_->setVisibility(brls::Visibility::GONE);
    }
    if (static_box_) {
        static_box_->clearViews();
        if (view) {
            static_box_->addView(view);
        }
        static_box_->setVisibility(brls::Visibility::VISIBLE);
    }
    setContentOffsetY(0.0f, false);
    if (content_root_) {
        content_root_->invalidate();
    }
}

float RecyclingGrid::computeInnerWidth() const {
    float w = const_cast<RecyclingGrid*>(this)->getWidth();
    if (std::isnan(w) || w <= 1.0f) {
        w = brls::Application::contentWidth > 0.0f
                ? brls::Application::contentWidth * 0.72f
                : 920.0f;
    }
    return std::max(64.0f, w - pad_left_ - pad_right_);
}

float RecyclingGrid::computeViewportHeight() const {
    float h = const_cast<RecyclingGrid*>(this)->getHeight();
    if (std::isnan(h) || h <= 1.0f) {
        h = brls::Application::contentHeight > 0.0f
                ? brls::Application::contentHeight
                : 800.0f;
    }
    return std::max(120.0f, h);
}

void RecyclingGrid::rebuildRowOffsets() {
    cached_item_count_ = data_source_ ? data_source_->getItemCount() : 0;
    const size_t s = static_cast<size_t>(std::max(1, span_count_));
    const float inner_w = computeInnerWidth();
    cached_cell_width_ = (s <= 1)
        ? inner_w
        : std::max(32.0f, (inner_w - static_cast<float>(s - 1) * col_space_) / static_cast<float>(s));
    total_rows_ = (cached_item_count_ + s - 1) / s;

    row_heights_.assign(total_rows_, estimated_row_height_);
    row_offsets_.assign(total_rows_ + 1, 0.0f);

    float acc = 0.0f;
    for (size_t r = 0; r < total_rows_; ++r) {
        row_offsets_[r] = acc;
        float row_h = estimated_row_height_;
        if (data_source_) {
            const size_t base = r * s;
            const size_t lim = std::min(cached_item_count_, base + s);
            float custom_max = -1.0f;
            for (size_t idx = base; idx < lim; ++idx) {
                const float ch = data_source_->heightForRow(this, idx);
                if (ch > 0.0f) {
                    custom_max = std::max(custom_max, ch);
                }
            }
            if (custom_max > 0.0f) {
                row_h = custom_max;
            }
        }
        row_heights_[r] = row_h;
        acc += row_h + ((r + 1 < total_rows_) ? estimated_row_space_ : 0.0f);
    }
    row_offsets_[total_rows_] = acc;
}

void RecyclingGrid::queueCellForReuse(RecyclingGridCell* cell) {
    if (!cell) return;
    cell->prepareForReuse();
    if (!cell->getReuseId().empty()) {
        reuse_pool_[cell->getReuseId()].push_back(cell);
    } else {
        destroyDetachedView(cell);
    }
}

void RecyclingGrid::unmountRowView(brls::View* row_view) {
    if (!row_view || !cells_box_) return;
    cells_box_->detachRow(row_view);

    if (auto* grid_row = dynamic_cast<GridRowBox*>(row_view)) {
        std::vector<brls::View*> kids = grid_row->getChildren();
        for (brls::View* child : kids) {
            grid_row->detachCell(child);
            if (auto* cell = dynamic_cast<RecyclingGridCell*>(child)) {
                queueCellForReuse(cell);
            } else {
                destroyDetachedView(child);
            }
        }
        row_box_pool_.push_back(grid_row);
    } else if (auto* cell = dynamic_cast<RecyclingGridCell*>(row_view)) {
        queueCellForReuse(cell);
    } else {
        destroyDetachedView(row_view);
    }
}

void RecyclingGrid::unmountAllRows() {
    if (!cells_box_) return;
    cells_box_->setBatchUpdating(true);
    while (!cells_box_->getChildren().empty()) {
        unmountRowView(cells_box_->getChildren().back());
    }
    cells_box_->setPaddingTop(0.0f);
    cells_box_->setPaddingBottom(0.0f);
    cells_box_->setBatchUpdating(false);
    mounted_first_row_ = 0;
    mounted_last_row_ = 0;
}

brls::View* RecyclingGrid::buildRowView(size_t row_idx, float inner_w) {
    if (!data_source_ || row_idx >= total_rows_) return nullptr;

    const size_t s = static_cast<size_t>(std::max(1, span_count_));
    const float row_h = row_heights_[row_idx];
    const float margin_bottom = (row_idx + 1 < total_rows_) ? estimated_row_space_ : 0.0f;

    if (s == 1) {
        cached_cell_width_ = inner_w;
        RecyclingGridCell* cell = data_source_->cellForRow(this, row_idx);
        if (!cell) {
            cell = new RecyclingGridCell();
        }
        cell->setIndex(row_idx);
        cell->setWidthPercentage(100);
        cell->setHeight(row_h);
        cell->setMarginRight(0.0f);
        cell->setMarginBottom(margin_bottom);
        return cell;
    }

    const float cell_w =
        std::max(32.0f, (inner_w - static_cast<float>(s - 1) * col_space_) / static_cast<float>(s));
    cached_cell_width_ = cell_w;

    GridRowBox* row_box = nullptr;
    if (!row_box_pool_.empty()) {
        row_box = row_box_pool_.back();
        row_box_pool_.pop_back();
    } else {
        row_box = new GridRowBox();
    }

    row_box->setSuppressInvalidate(true);
    row_box->setHeight(row_h);
    row_box->setMarginBottom(margin_bottom);

    const size_t base = row_idx * s;
    const size_t lim = std::min(cached_item_count_, base + s);
    for (size_t idx = base; idx < lim; ++idx) {
        RecyclingGridCell* cell = data_source_->cellForRow(this, idx);
        if (!cell) {
            cell = new RecyclingGridCell();
        }
        cell->setIndex(idx);
        cell->setDimensions(cell_w, row_h);
        cell->setMarginBottom(0.0f);
        cell->setMarginRight((idx + 1 < base + s) ? col_space_ : 0.0f);
        row_box->addView(cell);
    }
    row_box->setSuppressInvalidate(false);
    return row_box;
}

void RecyclingGrid::syncVisibleRows(bool force_rebind) {
    if (static_mode_ || !cells_box_ || !content_root_) return;

    if (!data_source_ || total_rows_ == 0) {
        unmountAllRows();
        if (empty_box_) {
            empty_box_->setVisibility(empty_box_->getChildren().empty()
                                          ? brls::Visibility::GONE
                                          : brls::Visibility::VISIBLE);
        }
        cells_box_->setVisibility(brls::Visibility::GONE);
        content_root_->invalidate();
        return;
    }

    if (empty_box_) {
        empty_box_->setVisibility(brls::Visibility::GONE);
    }
    cells_box_->setVisibility(brls::Visibility::VISIBLE);

    const float inner_w = computeInnerWidth();
    const float view_h = computeViewportHeight();
    const float header_h =
        (header_box_ && header_box_->getVisibility() == brls::Visibility::VISIBLE)
            ? std::max(0.0f, header_box_->getHeight())
            : 0.0f;

    const float scroll_y = std::max(0.0f, getContentOffsetY());
    const float local_top = std::max(0.0f, scroll_y - pad_top_ - header_h);
    const float local_bottom = std::max(local_top + 1.0f, scroll_y - pad_top_ - header_h + view_h);

    auto top_it = std::upper_bound(row_offsets_.begin(), row_offsets_.end(), local_top);
    size_t first_vis = (top_it == row_offsets_.begin())
                           ? 0
                           : static_cast<size_t>(std::distance(row_offsets_.begin(), top_it) - 1);
    first_vis = std::min(first_vis, total_rows_ - 1);

    auto bot_it = std::lower_bound(row_offsets_.begin(), row_offsets_.end(), local_bottom);
    size_t last_vis = static_cast<size_t>(std::distance(row_offsets_.begin(), bot_it));
    last_vis = std::clamp(last_vis, first_vis + 1, total_rows_);

    const size_t pf = static_cast<size_t>(std::max(1, prefetch_lines_));
    const size_t target_first = (first_vis > pf) ? (first_vis - pf) : 0;
    const size_t target_last = std::min(total_rows_, last_vis + pf);

    if (!force_rebind && target_first == mounted_first_row_ && target_last == mounted_last_row_ &&
        cells_box_->getChildren().size() == (mounted_last_row_ - mounted_first_row_)) {
        return;
    }

    bool had_cell_focus = false;
    size_t focused_item_idx = static_cast<size_t>(-1);
    if (isFocusInside(cells_box_)) {
        had_cell_focus = true;
        for (brls::View* cur = brls::Application::getCurrentFocus();
             cur != nullptr && cur != cells_box_; cur = cur->getParent()) {
            if (auto* c = dynamic_cast<RecyclingGridCell*>(cur)) {
                focused_item_idx = c->getIndex();
                break;
            }
        }
    }

    cells_box_->setBatchUpdating(true);

    const bool disjoint =
        force_rebind || mounted_first_row_ == mounted_last_row_ ||
        target_last <= mounted_first_row_ || target_first >= mounted_last_row_;

    if (disjoint) {
        while (!cells_box_->getChildren().empty()) {
            unmountRowView(cells_box_->getChildren().back());
        }
        mounted_first_row_ = target_first;
        mounted_last_row_ = target_first;
        for (size_t r = target_first; r < target_last; ++r) {
            if (brls::View* rv = buildRowView(r, inner_w)) {
                cells_box_->addView(rv);
            }
            ++mounted_last_row_;
        }
    } else {
        while (mounted_first_row_ < target_first && !cells_box_->getChildren().empty()) {
            unmountRowView(cells_box_->getChildren().front());
            ++mounted_first_row_;
        }
        while (mounted_last_row_ > target_last && !cells_box_->getChildren().empty()) {
            unmountRowView(cells_box_->getChildren().back());
            --mounted_last_row_;
        }
        while (mounted_first_row_ > target_first) {
            --mounted_first_row_;
            if (brls::View* rv = buildRowView(mounted_first_row_, inner_w)) {
                cells_box_->addView(rv, 0);
            }
        }
        while (mounted_last_row_ < target_last) {
            if (brls::View* rv = buildRowView(mounted_last_row_, inner_w)) {
                cells_box_->addView(rv);
            }
            ++mounted_last_row_;
        }
    }

    const float total_virtual_h = row_offsets_[total_rows_];
    cells_box_->setPaddingTop(row_offsets_[mounted_first_row_]);
    cells_box_->setPaddingBottom(
        std::max(0.0f, total_virtual_h - row_offsets_[mounted_last_row_]));
    cells_box_->setBatchUpdating(false);

    if (had_cell_focus && brls::Application::getCurrentFocus() == nullptr) {
        brls::View* restore_target = nullptr;
        if (focused_item_idx != static_cast<size_t>(-1)) {
            for (brls::View* rv : cells_box_->getChildren()) {
                if (auto* cell = dynamic_cast<RecyclingGridCell*>(rv)) {
                    if (cell->getIndex() == focused_item_idx) {
                        restore_target = cell->getDefaultFocus();
                        break;
                    }
                } else if (auto* gr = dynamic_cast<GridRowBox*>(rv)) {
                    for (brls::View* ch : gr->getChildren()) {
                        if (auto* cell = dynamic_cast<RecyclingGridCell*>(ch)) {
                            if (cell->getIndex() == focused_item_idx) {
                                restore_target = cell->getDefaultFocus();
                                break;
                            }
                        }
                    }
                    if (restore_target) break;
                }
            }
        }
        if (!restore_target) {
            restore_target = cells_box_->getDefaultFocus();
        }
        if (restore_target) {
            brls::Application::giveFocus(restore_target);
        }
    }

    content_root_->invalidate();
}

void RecyclingGrid::reloadData(bool reset_scroll) {
    if (static_mode_) {
        static_mode_ = false;
        if (static_box_) {
            static_box_->clearViews();
            static_box_->setVisibility(brls::Visibility::GONE);
        }
        if (virtual_wrap_) {
            virtual_wrap_->setVisibility(brls::Visibility::VISIBLE);
        }
    }

    last_next_page_item_count_ = static_cast<size_t>(-1);
    rebuildRowOffsets();

    if (reset_scroll) {
        setContentOffsetY(0.0f, false);
    }

    syncVisibleRows(true);

    if (!reset_scroll && content_root_) {
        const float max_scroll =
            std::max(0.0f, content_root_->getHeight() - computeViewportHeight());
        if (getContentOffsetY() > max_scroll) {
            setContentOffsetY(max_scroll, false);
            syncVisibleRows(false);
        }
    }
}

void RecyclingGrid::notifyDataChanged() {
    if (static_mode_) return;

    const size_t prev_rows = total_rows_;
    const size_t prev_items = cached_item_count_;
    rebuildRowOffsets();

    if (cached_item_count_ != prev_items) {
        last_next_page_item_count_ = static_cast<size_t>(-1);
    }

    // If items were removed or the last previously mounted row had fewer than
    // span_count_ cells and just gained sibling items, do a clean in-place rebind.
    const size_t s = static_cast<size_t>(std::max(1, span_count_));
    const bool tail_row_incomplete =
        prev_rows > 0 && (prev_items % s != 0) && mounted_last_row_ == prev_rows;
    if (cached_item_count_ < prev_items || tail_row_incomplete) {
        syncVisibleRows(true);
        return;
    }

    // Update previous last row's marginBottom if new rows were appended below it
    if (prev_rows > 0 && total_rows_ > prev_rows &&
        prev_rows - 1 >= mounted_first_row_ && prev_rows - 1 < mounted_last_row_) {
        const size_t child_idx = (prev_rows - 1) - mounted_first_row_;
        if (child_idx < cells_box_->getChildren().size()) {
            cells_box_->getChildren()[child_idx]->setMarginBottom(estimated_row_space_);
        }
    }

    if (cells_box_ && total_rows_ > 0) {
        cells_box_->setBatchUpdating(true);
        cells_box_->setPaddingBottom(
            std::max(0.0f, row_offsets_[total_rows_] - row_offsets_[mounted_last_row_]));
        cells_box_->setBatchUpdating(false);
    }
    syncVisibleRows(false);
    if (content_root_) {
        content_root_->invalidate();
    }
}

void RecyclingGrid::scrollToItem(size_t item_index, bool animated) {
    if (static_mode_ || total_rows_ == 0 || item_index >= cached_item_count_) return;

    const size_t s = static_cast<size_t>(std::max(1, span_count_));
    const size_t row = item_index / s;
    const float header_h =
        (header_box_ && header_box_->getVisibility() == brls::Visibility::VISIBLE)
            ? std::max(0.0f, header_box_->getHeight())
            : 0.0f;
    const float row_top = pad_top_ + header_h + row_offsets_[row];
    const float row_bot = row_top + row_heights_[row];
    const float view_h = computeViewportHeight();
    const float cur_y = getContentOffsetY();

    float target_y = cur_y;
    if (row_top < cur_y) {
        target_y = row_top;
    } else if (row_bot > cur_y + view_h) {
        target_y = std::max(0.0f, row_bot - view_h + pad_bottom_);
    }

    if (std::abs(target_y - cur_y) > 0.5f) {
        setContentOffsetY(target_y, animated);
    }
    syncVisibleRows(false);
}

void RecyclingGrid::checkNextPageTrigger() {
    if (static_mode_ || !on_next_page_ || cached_item_count_ == 0 ||
        last_next_page_item_count_ == cached_item_count_ || !content_root_) {
        return;
    }
    const float content_h = content_root_->getHeight();
    const float view_h = getHeight();
    if (view_h <= 1.0f || content_h <= view_h) return;

    const float threshold =
        std::max(120.0f, (estimated_row_height_ + estimated_row_space_) * 2.0f);
    if (getContentOffsetY() + view_h + threshold >= content_h) {
        last_next_page_item_count_ = cached_item_count_;
        auto cb = on_next_page_;
        brls::sync([cb]() { cb(); });
    }
}

void RecyclingGrid::onLayout() {
    brls::ScrollingFrame::onLayout();

    const float w = getWidth();
    const float h = getHeight();
    if (static_mode_ || w <= 1.0f || h <= 1.0f) return;

    const bool width_changed = std::abs(w - last_layout_width_) > 1.0f;
    last_layout_width_ = w;

    if (width_changed) {
        rebuildRowOffsets();
    }
    syncVisibleRows(width_changed);
}

void RecyclingGrid::draw(NVGcontext* vg,
                         float x,
                         float y,
                         float width,
                         float height,
                         brls::Style style,
                         brls::FrameContext* ctx) {
    if (!static_mode_) {
        syncVisibleRows(false);
        checkNextPageTrigger();
    }
    brls::ScrollingFrame::draw(vg, x, y, width, height, style, ctx);
}

size_t RecyclingGrid::mountedCellCount() const {
    if (!cells_box_) return 0;
    if (span_count_ <= 1) {
        return cells_box_->getChildren().size();
    }
    size_t count = 0;
    for (brls::View* row_view : cells_box_->getChildren()) {
        if (auto* r = dynamic_cast<GridRowBox*>(row_view)) {
            count += r->getChildren().size();
        } else {
            ++count;
        }
    }
    return count;
}

size_t RecyclingGrid::pooledCellCount() const {
    size_t count = 0;
    for (const auto& kv : reuse_pool_) {
        count += kv.second.size();
    }
    return count;
}

}  // namespace omnidesk::host
