import React from 'react';
import { BookMirrorSettings } from '../../components/BookMirrorSettings';
import { MediaCover } from '../../components/MediaCover';
import { AlertTriangle, BookOpen, LoaderCircle, Plus } from 'lucide-react';
import type { BookSearchProvider } from '../../api';
import StateView from '../../components/StateView';
import { FocusButton, HAIRLINE_BORDER, focusClass, focusableProps } from '../../components/WorkbenchLayout';
import { SearchInput, SegmentedPills } from '../../components/WorkbenchPrimitives';
import type { BookCatalogItem, BookItem } from '../../data/mock';

// Keep aligned with plugin-ebook's SUPPORTED_EXTENSIONS / CALIBRE_FORMATS.
const EBOOK_FORMATS = new Set<BookCatalogItem['format']>([
  'epub', 'txt', 'mobi', 'azw3', 'azw', 'azw4', 'fb2', 'fbz', 'rtf',
  'html', 'htm', 'htmlz', 'docx', 'odt', 'cbz', 'cbr', 'cb7', 'chm',
  'djvu', 'lit', 'lrf', 'prc', 'pdb', 'pml', 'rb', 'snb', 'tcr', 'txtz', 'kepub',
]);

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
  onMirrorChanged?: () => void;
  searchLoading?: boolean;
  searchError?: string | null;
  searchProvider?: BookSearchProvider;
  onSearchProviderChange?: (provider: BookSearchProvider) => void;
  onSelectBook: (book: BookItem) => void;
  onImportBook: (entry: BookCatalogItem) => void;
  onCatalogAction?: (
    entry: BookCatalogItem,
    action: 'download' | 'open_in_babeldoc' | 'open_in_ebook' | 'send_to_nas',
  ) => void;
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
  onMirrorChanged,
  searchLoading = false,
  searchError = null,
  searchProvider = 'auto',
  onSearchProviderChange,
  onSelectBook,
  onImportBook,
  onCatalogAction,
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
          { id: 'search', label: '搜书' },
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

    {leftTab === 'search' && onSearchProviderChange ? (
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SegmentedPills
          options={[
            { id: 'auto', label: '自动回退' },
            { id: 'plugin-zlib', label: 'Z-Library' },
            { id: 'plugin-annas-archive', label: "Anna's Archive" },
          ]}
          value={searchProvider}
          onChange={(value) => onSearchProviderChange(value as BookSearchProvider)}
          focusPrefix="book:provider:"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          tone="book"
          ariaLabel="图书搜索提供方"
        />
        <span className="inline-flex items-center gap-1.5 text-[12.5px] text-[var(--text-muted)]">
          {searchLoading ? (
            <>
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> 正在搜索图书…
            </>
          ) : (
            'Z-Library / Anna’s Archive'
          )}
        </span>
      </div>
    ) : null}

    {leftTab === 'search' && onMirrorChanged && <BookMirrorSettings onChanged={onMirrorChanged} />}

    {leftTab === 'search' && searchError ? (
      <div role="alert" className="flex items-start gap-2 rounded-[9px] bg-[var(--status-err-bg)] px-3 py-2 text-[13px] text-[var(--status-err-fg)]">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>{searchError}</span>
      </div>
    ) : null}

    {leftTab === 'shelf' ? (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(120px,160px))] gap-4">
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
                <MediaCover url={book.coverUrl} />
                <div className="relative flex items-center justify-between">
                  <span className="rounded bg-black/45 px-1.5 py-0.5 font-mono text-[12.5px] uppercase">
                    {book.format}
                  </span>
                  {isSelected ? <BookOpen className="h-3.5 w-3.5 text-white/90" aria-hidden="true" /> : null}
                </div>
                <div className="relative rounded bg-black/40 p-1">
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
        mode={searchLoading ? 'loading' : searchError ? 'error' : 'empty'}
        variant="inline"
        title={searchLoading ? '正在搜索图书…' : searchError ? '图书搜索未完成' : catalogQuery.trim().length >= 2 ? '未找到匹配的图书' : '输入书名、作者或 ISBN 开始检索'}
        description={
          searchLoading ? '正在连接图书源，请稍候。'
            : searchError ? '连接图书源失败，请检查网络或切换提供方后重试。'
            : catalogQuery.trim().length >= 2
            ? '可更换关键词或切换图书源继续搜索。'
            : '至少输入 2 个字符，结果来自 Z-Library / Anna’s Archive。'
        }
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
          <article key={entry.id} className="flex flex-col gap-1.5">
            <FocusButton
              focusId={`book:catalog:${entry.id}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onImportBook(entry)}
              surface="surface"
              className="flex flex-col gap-1.5 p-4 text-left"
            >
              <div className="flex items-center justify-between text-[13.5px]">
                <span className="font-mono font-semibold" style={{ color: 'var(--domain-book)' }}>
                  {entry.format === 'unknown' ? '格式未知' : entry.format.toUpperCase()} · {entry.sizeLabel}
                </span>
                <span className="inline-flex items-center gap-1 font-medium text-[var(--text-muted)]">
                  <Plus className="h-3 w-3" aria-hidden="true" />
                  <span>{entry.providerPluginId === 'plugin-annas-archive' ? '查看文件与来源' : entry.providerPluginId ? '下载并导入' : '导入书架'}</span>
                </span>
              </div>
              <div className="text-[15.5px] font-semibold text-[var(--text-primary)]">
                {entry.title}
              </div>
              <div className="text-[14px] text-[var(--text-muted)]">{entry.author}</div>
              <div className="truncate text-[12.5px] text-[var(--text-subtle)]">
                {entry.sourceName}
              </div>
            </FocusButton>
            {entry.providerPluginId && onCatalogAction ? (
              <div className="grid auto-cols-fr grid-flow-col gap-1">
                {entry.format === 'pdf' ? <button
                  type="button"
                  onClick={() => onCatalogAction(entry, 'open_in_babeldoc')}
                  className="cursor-pointer rounded-[7px] bg-[var(--bg-canvas)] px-1.5 py-1 text-[12px] text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  BabelDOC 翻译
                </button> : null}
                {EBOOK_FORMATS.has(entry.format) ? <button
                  type="button"
                  onClick={() => onCatalogAction(entry, 'open_in_ebook')}
                  className="cursor-pointer rounded-[7px] bg-[var(--bg-canvas)] px-1.5 py-1 text-[12px] text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  {entry.format.toUpperCase()} 阅读
                </button> : null}
                <button
                  type="button"
                  onClick={() => onCatalogAction(entry, 'send_to_nas')}
                  className="cursor-pointer rounded-[7px] bg-[var(--bg-canvas)] px-1.5 py-1 text-[12px] text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  送 NAS
                </button>
              </div>
            ) : null}
          </article>
        ))}
      </div>
    )}
  </div>
);

export default BookShelfSidebar;
