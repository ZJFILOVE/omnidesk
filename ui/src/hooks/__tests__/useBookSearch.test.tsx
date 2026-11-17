// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { searchBooks } from '../../api';
import { useBookSearch } from '../useBookSearch';

vi.mock('../../api', () => ({ isTauriAvailable: () => true, searchBooks: vi.fn(), formatRpcError: () => '搜索失败' }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('loads subsequent search pages from the resolved provider and resets when the query changes', async () => {
  vi.mocked(searchBooks).mockImplementation(async ({ page, query }) => ({
    available: true, ok: true, providerPluginId: 'plugin-annas-archive', error: null,
    data: { providerPluginId: 'plugin-annas-archive', results: [{ itemId: `${query}-${page}`, title: query }] },
  } as Awaited<ReturnType<typeof searchBooks>>));
  const { result, rerender } = renderHook(({ query }) => useBookSearch(query, 'auto', true), { initialProps: { query: 'physics' } });
  await waitFor(() => expect(result.current.results).toHaveLength(1));
  await act(async () => result.current.pagination.loadMore());
  expect(searchBooks).toHaveBeenLastCalledWith({ query: 'physics', provider: 'plugin-annas-archive', limit: 24, page: 2 });
  expect(result.current.results.map((item) => item.itemId)).toEqual(['physics-1', 'physics-2']);
  rerender({ query: 'history' });
  await waitFor(() => expect(result.current.results[0]?.itemId).toBe('history-1'));
  expect(result.current.pagination.page).toBe(1);
  expect(result.current.results).toHaveLength(1);
});
