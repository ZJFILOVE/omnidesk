import React from 'react';
import { BookOpen, Plus } from 'lucide-react';
import StateView from '../../components/StateView';
import { FocusButton, HAIRLINE_BORDER, focusClass, focusableProps } from '../../components/WorkbenchLayout';
import { SearchInput, SegmentedPills } from '../../components/WorkbenchPrimitives';
import type { BookCatalogItem, BookItem } from '../../data/mock';

export interface BookShelfSidebarProps {
  books: BookItem[];
  catalog: BookCatalogItem[];
  selectedBookId: string;
  leftTab: 'shelf' | 'search';
  catalogQuery: string;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onTabChange: (tab: 'shelf' | 'search') => void;
  onCatalogQueryChange: (q: string) => void;
  onSelectBook: (book: BookItem) => void;
  onImportBook: (entry: BookCatalogItem) => void;
}

export const BookShelfSidebar: React.FC<BookShelfSidebarProps> = ({
  books,
  catalog,
  selectedBookId,
  leftTab,
  catalogQuery,
  activeFocusId,
  onFocus,
  onTabChange,
  onCatalogQueryChange,
  onSelectBook,
  onImportBook,
}) => (
  <div
    className="flex flex-col gap-4 border-b px-6 py-5"
    style={{
      backgroundColor: 'color-mix(in srgb, var(--bg-surface) 75%, var(--bg-canvas))',
      borderColor: HAIRLINE_BORDER,
    }}
  >
    <div className="flex flex-wrap items-center justify-between gap-3">
      <SegmentedPills
        options={[
          { id: 'shelf', label: '封面书架', count: books.length },
          { id: 'search', label: 'Z-Lib 搜书' },
        ]}
        value={leftTab}
        onChange={onTabChange}
        focusPrefix="book:tab:"
        activeFocusId={activeFocusId}
        onFocus={onFocus}
        tone="book"
        ariaLabel="书架与搜书切换"
      />

      {leftTab === 'search' ? (
        <SearchInput
          focusId="book:catalog:search"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          value={catalogQuery}
          onChange={onCatalogQueryChange}
          placeholder="检索 Z-Library / Anna's Archive..."
          ariaLabel="搜索书库"
          widthClass="w-72"
        />
      ) : (
        <span className="text-[14px] text-[var(--text-muted)]">
          2:3 书脊封面墙 · 选中书卷即进入双栏对照
        </span>
      )}
    </div>

    {leftTab === 'shelf' ? (
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-6">
        {books.map((book) => {
          const shelfId = `book:shelf:${book.id}`;
          const isSelected = selectedBookId === book.id;
          const isFocused = activeFocusId === shelfId;

          return (
            <article
              key={book.id}
              role="button"
              aria-pressed={isSelected}
              data-focused={isFocused ? 'true' : undefined}
              {...focusableProps(shelfId, activeFocusId, onFocus, () => onSelectBook(book))}
              className={focusClass(
                isFocused,
                'group flex cursor-pointer flex-col gap-2 rounded-[14px] p-2 text-left transition-colors hover:bg-[var(--hover-fill)]',
              )}
              style={{
                backgroundColor: isSelected
                  ? 'color-mix(in srgb, var(--domain-book) 11%, var(--bg-surface))'
                  : 'transparent',
              }}
            >
              <div
                className="relative flex flex-col justify-between overflow-hidden rounded-[8px] p-3 text-white"
                style={{
                  aspectRatio: '2 / 3',
                  background: book.coverGradient,
                  boxShadow: isSelected
                    ? '0 10px 24px rgba(0,0,0,0.2), inset 4px 0 0 rgba(255,255,255,0.32)'
                    : 'inset 3px 0 0 rgba(255,255,255,0.22), var(--shadow-page)',
                }}
              >
                <div className="flex items-center justify-between">
                  <span className="rounded bg-black/45 px-1.5 py-0.5 font-mono text-[12.5px] uppercase">
                    {book.format}
                  </span>
                  {isSelected ? <BookOpen className="h-3.5 w-3.5 text-white/90" aria-hidden="true" /> : null}
                </div>
                <div>
                  <div className="line-clamp-3 text-[14px] leading-snug font-semibold">
                    {book.title}
                  </div>
                  <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-black/30">
                    <div
                      className="h-full rounded-full bg-white/90"
                      style={{ width: `${Math.max(4, book.progressPct)}%` }}
                    />
                  </div>
                </div>
              </div>

              <div className="px-0.5">
                <div className="line-clamp-1 text-[14px] font-semibold text-[var(--text-primary)]">
                  {book.badge}
                </div>
                <div className="line-clamp-1 text-[13.5px] text-[var(--text-muted)]">
                  {book.language} · {book.progressPct}%
                </div>
              </div>
            </article>
          );
        })}
      </div>
    ) : catalog.length === 0 ? (
      <StateView
        mode="empty"
        variant="inline"
        title="未找到匹配的图书"
        description="尝试更换书名或作者关键词。"
        activeFocusId={activeFocusId}
        onFocus={onFocus}
        action={{
          label: '清空关键词',
          focusId: 'book:catalog:clear',
          onActivate: () => onCatalogQueryChange(''),
        }}
      />
    ) : (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {catalog.map((entry) => (
          <FocusButton
            key={entry.id}
            focusId={`book:catalog:${entry.id}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onImportBook(entry)}
            surface="surface"
            className="flex flex-col gap-1.5 p-4 text-left"
          >
            <div className="flex items-center justify-between text-[13.5px]">
              <span className="font-mono font-semibold" style={{ color: 'var(--domain-book)' }}>
                {entry.format.toUpperCase()} · {entry.sizeLabel}
              </span>
              <span className="inline-flex items-center gap-1 font-medium text-[var(--text-muted)]">
                <Plus className="h-3 w-3" aria-hidden="true" />
                <span>导入书架</span>
              </span>
            </div>
            <div className="text-[15.5px] font-semibold text-[var(--text-primary)]">
              {entry.title}
            </div>
            <div className="text-[14px] text-[var(--text-muted)]">{entry.author}</div>
          </FocusButton>
        ))}
      </div>
    )}
  </div>
);

export default BookShelfSidebar;
