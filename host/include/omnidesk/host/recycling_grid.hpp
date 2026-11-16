#pragma once

#include <borealis.hpp>
#include <cstddef>
#include <functional>
#include <string>
#include <unordered_map>
#include <vector>

namespace omnidesk::host {

class RecyclingGrid;

/**
 * Reusable cell base class for OmniDesk virtualized grids and lists.
 * Subclasses construct their child widget hierarchy once and rebind data
 * in-place inside `RecyclingGridDataSource::cellForRow(...)`.
 */
class RecyclingGridCell : public brls::Box {
public:
    explicit RecyclingGridCell(brls::Axis axis = brls::Axis::COLUMN);
    ~RecyclingGridCell() override = default;

    const std::string& getReuseId() const { return reuse_id_; }
    void setReuseId(const std::string& id) { reuse_id_ = id; }

    size_t getIndex() const { return item_index_; }
    void setIndex(size_t index) { item_index_ = index; }

    virtual void prepareForReuse() {}

private:
    std::string reuse_id_;
    size_t item_index_ = 0;
};

/**
 * Data source adapter supplying item counts, optional per-row heights,
 * and cell binding for `RecyclingGrid`.
 */
class RecyclingGridDataSource {
public:
    virtual ~RecyclingGridDataSource() = default;

    virtual size_t getItemCount() = 0;
    virtual RecyclingGridCell* cellForRow(RecyclingGrid* recycler, size_t index) = 0;

    /**
     * Optional custom height for `index` (when <= 0, falls back to
     * `recycler->estimatedRowHeight()`). For multi-column grids, the row height
     * is the maximum across items in that row.
     */
    virtual float heightForRow(RecyclingGrid* /*recycler*/, size_t /*index*/) {
        return -1.0f;
    }

    virtual void clearData() {}
};

/**
 * Viewport-virtualized vertical list & multi-column grid built on top of
 * `brls::ScrollingFrame`.
 *
 * Keeps only the visible viewport rows +/- `preFetchLine` (~15-25 cells)
 * mounted in the Yoga layout tree, using top/bottom padding spacers to preserve
 * exact scroll geometry, scrollbar ratios, and 2D keyboard/gamepad focus.
 *
 * Also supports optional scrolling header/footer/empty views and a static
 * stage slot (`setStaticContent`) so workbenches can keep a single stable
 * root scroll widget (`omnidesk/pane_primary`) across feed and detail modes.
 */
class RecyclingGrid : public brls::ScrollingFrame {
public:
    RecyclingGrid();
    ~RecyclingGrid() override;

    void registerCell(const std::string& id, std::function<RecyclingGridCell*()> allocator);
    RecyclingGridCell* dequeueReusableCell(const std::string& id);

    void setDataSource(RecyclingGridDataSource* source, bool own_source = true);
    RecyclingGridDataSource* getDataSource() const { return data_source_; }

    void setSpanCount(int span);
    int getSpanCount() const { return span_count_; }

    void setEstimatedRowHeight(float height);
    float estimatedRowHeight() const { return estimated_row_height_; }

    void setEstimatedRowSpace(float space);
    float estimatedRowSpace() const { return estimated_row_space_; }

    void setColSpace(float space);
    float colSpace() const { return col_space_; }

    void setPreFetchLine(int lines);
    int preFetchLine() const { return prefetch_lines_; }

    void setContentPadding(float top, float right, float bottom, float left);

    void setHeaderView(brls::View* view);
    void setFooterView(brls::View* view);
    void setEmptyView(brls::View* view);

    /**
     * Switches the scroll frame into non-virtualized static stage mode
     * (e.g. video player stage, PDF reader, EPUB reader), recycling any
     * mounted grid cells back into the pool. Calling `reloadData()` or
     * `setDataSource()` automatically exits static mode.
     */
    void setStaticContent(brls::View* view);
    bool isStaticMode() const { return static_mode_; }

    void setOnNextPage(std::function<void()> cb) { on_next_page_ = std::move(cb); }

    /**
     * Rebinds visible cells from the current data source.
     * When `reset_scroll == false`, preserves the current vertical scroll offset.
     */
    void reloadData(bool reset_scroll = false);

    /**
     * Incrementally extends row geometry when new items are appended to the
     * existing data source (e.g. infinite scroll pagination) without tearing
     * down currently visible cells.
     */
    void notifyDataChanged();

    /**
     * Scrolls so that `item_index` is visible inside the viewport and ensures
     * its row is mounted.
     */
    void scrollToItem(size_t item_index, bool animated = false);

    void draw(NVGcontext* vg,
              float x,
              float y,
              float width,
              float height,
              brls::Style style,
              brls::FrameContext* ctx) override;
    void onLayout() override;

    // Diagnostics & unit-test introspection
    size_t mountedCellCount() const;
    size_t mountedRowCount() const {
        return mounted_last_row_ >= mounted_first_row_
                   ? (mounted_last_row_ - mounted_first_row_)
                   : 0;
    }
    size_t pooledCellCount() const;
    size_t allocatedCellCount() const { return allocated_cell_count_; }
    size_t reusedCellCount() const { return reused_cell_count_; }
    size_t totalItemCount() const { return cached_item_count_; }
    float cellColumnWidth() const { return cached_cell_width_; }

private:
    class CellsContainerBox;
    class GridRowBox;

    void rebuildRowOffsets();
    float computeInnerWidth() const;
    float computeViewportHeight() const;
    void syncVisibleRows(bool force_rebind);
    void unmountAllRows();
    void unmountRowView(brls::View* row_view);
    brls::View* buildRowView(size_t row_idx, float inner_w);
    void queueCellForReuse(RecyclingGridCell* cell);
    void checkNextPageTrigger();

    RecyclingGridDataSource* data_source_ = nullptr;
    bool owns_source_ = false;

    int span_count_ = 1;
    float estimated_row_height_ = 140.0f;
    float estimated_row_space_ = 10.0f;
    float col_space_ = 12.0f;
    int prefetch_lines_ = 2;

    float pad_top_ = 0.0f;
    float pad_right_ = 0.0f;
    float pad_bottom_ = 0.0f;
    float pad_left_ = 0.0f;

    bool static_mode_ = false;
    float last_layout_width_ = 0.0f;
    float last_layout_height_ = 0.0f;
    float cached_cell_width_ = 0.0f;

    size_t cached_item_count_ = 0;
    size_t total_rows_ = 0;
    std::vector<float> row_offsets_;   // size == total_rows_ + 1 (row top offsets)
    std::vector<float> row_heights_;   // size == total_rows_ (pure row height without space)

    size_t mounted_first_row_ = 0;
    size_t mounted_last_row_ = 0;

    std::unordered_map<std::string, std::function<RecyclingGridCell*()>> allocators_;
    std::unordered_map<std::string, std::vector<RecyclingGridCell*>> reuse_pool_;
    std::vector<GridRowBox*> row_box_pool_;

    size_t allocated_cell_count_ = 0;
    size_t reused_cell_count_ = 0;

    std::function<void()> on_next_page_;
    size_t last_next_page_item_count_ = static_cast<size_t>(-1);

    brls::Box* content_root_ = nullptr;
    brls::Box* static_box_ = nullptr;
    brls::Box* virtual_wrap_ = nullptr;
    brls::Box* header_box_ = nullptr;
    brls::Box* empty_box_ = nullptr;
    CellsContainerBox* cells_box_ = nullptr;
    brls::Box* footer_box_ = nullptr;
};

}  // namespace omnidesk::host
