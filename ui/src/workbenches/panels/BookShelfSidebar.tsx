import React from 'react';
import {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableInputProps,
  focusableProps,
} from '../../components/WorkbenchLayout';
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
  <div className="flex flex-col gap-3 p-3.5">
    <div className="grid grid-cols-2 gap-1.5">
      {(
        [
          { id: 'shelf', label: `书架 (${books.length})` },
          { id: 'search', label: 'Z-Lib 搜书' },
        ] as const
      ).map((tab) => (
        <FocusButton
          key={tab.id}
          focusId={`book:tab:${tab.id}`}
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() => onTabChange(tab.id)}
          selected={leftTab === tab.id}
          tone="book"
          className="px-2 py-1.5 text-xs font-semibold"
        >
          {tab.label}
        </FocusButton>
      ))}
    </div>

    {leftTab === 'shelf' ? (
      <div className="grid grid-cols-2 gap-2.5">
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
                'flex cursor-pointer flex-col overflow-hidden rounded-lg border text-left transition-colors',
              )}
              style={{
                backgroundColor: isSelected
                  ? 'var(--status-ok-bg, #E7F5ED)'
                  : 'var(--bg-canvas, #F6F5F3)',
                borderColor: isSelected ? 'var(--domain-book, #1E7244)' : HAIRLINE_BORDER,
              }}
            >
              <div
                className="relative flex flex-col justify-between p-2 text-white"
                style={{ aspectRatio: '2 / 3', background: book.coverGradient }}
              >
                <span className="self-start rounded bg-black/50 px-1.5 py-0.5 font-mono text-[9px] uppercase">
                  {book.format}
                </span>
                <div>
                  <div className="line-clamp-3 text-[11px] leading-snug font-semibold">
                    {book.title}
                  </div>
                  <div className="mt-1 font-mono text-[9px] opacity-80">{book.progressPct}%</div>
                </div>
              </div>
              <div className="p-2">
                <div className="line-clamp-1 text-[10px] font-medium">{book.badge}</div>
                <div
                  className="line-clamp-1 font-mono text-[9px]"
                  style={{ color: 'var(--text-muted, #66625D)' }}
                >
                  {book.language}
                </div>
              </div>
            </article>
          );
        })}
      </div>
    ) : (
      <div className="flex flex-col gap-2.5">
        <input
          type="search"
          value={catalogQuery}
          onChange={(e) => onCatalogQueryChange(e.target.value)}
          placeholder="检索 Z-Library / Anna's..."
          aria-label="搜索书库"
          data-focused={activeFocusId === 'book:catalog:search' ? 'true' : undefined}
          {...focusableInputProps('book:catalog:search', activeFocusId, onFocus)}
          className={focusClass(
            activeFocusId === 'book:catalog:search',
            'w-full rounded-md border px-2.5 py-1.5 text-xs',
          )}
          style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
        />
        {catalog.map((entry) => (
          <FocusButton
            key={entry.id}
            focusId={`book:catalog:${entry.id}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onImportBook(entry)}
            className="flex flex-col gap-1 p-2.5 text-left"
          >
            <div className="flex items-center justify-between font-mono text-[10px]">
              <span style={{ color: 'var(--domain-book, #1E7244)' }}>
                {entry.format.toUpperCase()} · {entry.sizeLabel}
              </span>
              <span>+ 导入</span>
            </div>
            <div className="text-xs font-semibold">{entry.title}</div>
            <div className="text-[11px]" style={{ color: 'var(--text-muted, #66625D)' }}>
              {entry.author}
            </div>
          </FocusButton>
        ))}
      </div>
    )}
  </div>
);

export default BookShelfSidebar;
