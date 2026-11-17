import { ChevronDown, LoaderCircle } from 'lucide-react';
import { FocusItem } from './FocusItem';

export interface LoadMoreState {
  page: number;
  hasMore: boolean;
  loadingMore: boolean;
  loadMoreError: string | null;
  loadMore: () => void;
}

/** Keep already loaded content in place while requesting the next provider page. */
export function LoadMoreButton({ id, pagination, disabled = false }: {
  id: string;
  pagination: LoadMoreState;
  disabled?: boolean;
}) {
  const { page, hasMore, loadingMore, loadMoreError, loadMore } = pagination;
  return (
    <div className="flex w-full flex-col items-center gap-2 py-3" aria-busy={loadingMore}>
      {loadMoreError ? <p role="alert" className="text-[13px] text-[var(--status-err-fg)]">{loadMoreError}</p> : null}
      {hasMore ? (
        <FocusItem as="button" id={id} zone="main" disabled={disabled || loadingMore}
          onActivate={loadMore}
          className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg border border-[var(--border-line)] bg-[var(--bg-surface)] px-4 py-2 text-[14px] text-[var(--text-primary)] transition-colors hover:bg-[var(--hover-fill)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]">
          {loadingMore ? <LoaderCircle className="h-4 w-4 motion-safe:animate-spin" aria-hidden="true" /> : <ChevronDown className="h-4 w-4" aria-hidden="true" />}
          {loadingMore ? '正在加载下一页…' : loadMoreError ? '重试下一页' : '加载下一页'}
        </FocusItem>
      ) : null}
      <span role="status" className="text-[13px] text-[var(--text-muted)]">
        已加载 {page} 页{!hasMore ? ' · 没有更多内容了' : ''}
      </span>
    </div>
  );
}
