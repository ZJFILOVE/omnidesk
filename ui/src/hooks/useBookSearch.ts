import { useEffect, useRef, useState } from 'react';
import { formatRpcError, isTauriAvailable, searchBooks, type BookSearchProvider, type BookSearchResult } from '../api';

export function useBookSearch(query: string, provider: BookSearchProvider, enabled: boolean, refreshKey = 0) {
  const [results, setResults] = useState<BookSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const version = useRef(0);
  const pending = useRef(false);
  const nextCursor = useRef<{ page: number; offset: number } | undefined>(undefined);
  const resolvedProvider = useRef<BookSearchProvider>(provider);

  useEffect(() => {
    const current = ++version.current;
    pending.current = false;
    nextCursor.current = undefined;
    setResults([]);
    setLoading(false);
    setError(null);
    setPage(1);
    setHasMore(false);
    setLoadingMore(false);
    setLoadMoreError(null);
    if (!enabled || !isTauriAvailable() || query.trim().length < 2) return;
    setLoading(true);
    const timer = window.setTimeout(async () => {
      try {
        const result = await searchBooks({ query: query.trim(), provider, limit: 24, page: 1 });
        if (current !== version.current) return;
        if (!result.ok) {
          setError(formatRpcError(result.error));
          return;
        }
        resolvedProvider.current = result.data.providerPluginId as BookSearchProvider;
        nextCursor.current = result.data.nextCursor;
        setResults(result.data.results);
        setHasMore(result.data.hasMore ?? result.data.results.length > 0);
      } catch (err) {
        if (current === version.current) setError(String(err));
      } finally {
        if (current === version.current) setLoading(false);
      }
    }, 320);
    return () => { version.current += 1; window.clearTimeout(timer); };
  }, [query, provider, enabled, refreshKey]);

  const loadMore = async () => {
    if (pending.current || loading || !hasMore) return;
    const current = version.current;
    pending.current = true;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const result = await searchBooks({ query: query.trim(), provider: resolvedProvider.current, limit: 24, page: page + 1, cursor: nextCursor.current });
      if (current !== version.current) return;
      if (!result.ok) {
        setLoadMoreError(formatRpcError(result.error));
        return;
      }
      nextCursor.current = result.data.nextCursor;
      const seen = new Set(results.map((item) => item.itemId));
      const next = result.data.results.filter((item) => {
        if (seen.has(item.itemId)) return false;
        seen.add(item.itemId);
        return true;
      });
      setResults((previous) => [...previous, ...next]);
      if (next.length) setPage(page + 1);
      setHasMore(result.data.hasMore ?? next.length > 0);
    } catch (err) {
      if (current === version.current) setLoadMoreError(String(err));
    } finally {
      if (current === version.current) { pending.current = false; setLoadingMore(false); }
    }
  };
  return { results, loading, error, pagination: { page, hasMore, loadingMore, loadMoreError, loadMore } };
}
