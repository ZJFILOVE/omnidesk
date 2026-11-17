// @vitest-environment jsdom
import { act, renderHook, waitFor, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useLiveComments, nextCommentRequests } from '../useLiveComments';
import { callCapability, domainSources, listPlugins } from '../../api';

vi.mock('../../api', () => ({
  isTauriAvailable: () => true,
  callCapability: vi.fn(), domainSources: vi.fn(), listPlugins: vi.fn(),
}));
const ok = (data: unknown) => ({ ok: true, available: true, data });
afterEach(() => { cleanup(); vi.resetAllMocks(); });
function setup() {
  vi.mocked(domainSources).mockResolvedValue(ok([{ sourceId: 'forum', pluginId: 'plugin-linuxdo' }]) as never);
  vi.mocked(listPlugins).mockResolvedValue(ok({ plugins: [{ plugin_id: 'plugin-linuxdo', provides: ['text.comments.v1'] }] }) as never);
}
it('loads the dedicated provider and preserves comments when a later page fails', async () => {
  setup();
  vi.mocked(callCapability).mockResolvedValueOnce(ok({ comments: [{ id: 'one', body: '第一条', replies: [{ id: 'reply', body: '回复' }] }], next_offset: 40, has_more: true }) as never)
    .mockResolvedValueOnce({ ok: false, available: true, error: { message: '网络错误' } } as never)
    .mockResolvedValueOnce(ok({ comments: [{ id: 'two', body: '第二条' }], has_more: false }) as never);
  const { result } = renderHook(() => useLiveComments('topic-1', 'forum'));
  await waitFor(() => expect(result.current.comments[0]?.original).toBe('第一条'));
  expect(result.current.comments[0].children?.[0].original).toBe('回复');
  await act(async () => result.current.pagination.loadMore());
  await waitFor(() => expect(result.current.error).toBe('网络错误'));
  expect(result.current.comments).toHaveLength(1);
  await act(async () => result.current.pagination.loadMore());
  await waitFor(() => expect(result.current.comments).toHaveLength(2));
  expect(vi.mocked(callCapability).mock.calls[2][0]).toMatchObject({ providerPluginId: 'plugin-linuxdo', payload: { article_id: 'topic-1', offset: 40 } });
});
it('ignores the old article response when the reader switches articles', async () => {
  setup();
  let resolveOld: (value: unknown) => void = () => {};
  vi.mocked(callCapability).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve as never; }))
    .mockResolvedValueOnce(ok({ comments: [{ id: 'new', body: '新文章评论' }] }) as never);
  const { result, rerender } = renderHook(({ id }) => useLiveComments(id, 'forum'), { initialProps: { id: 'old' } });
  await waitFor(() => expect(callCapability).toHaveBeenCalledTimes(1));
  rerender({ id: 'new' });
  await waitFor(() => expect(result.current.comments[0]?.id).toBe('new'));
  await act(async () => resolveOld(ok({ comments: [{ id: 'old', body: '旧评论' }] })));
  expect(result.current.comments.map(c => c.id)).toEqual(['new']);
});
it('uses provider cursors without an endless repeat and expands Reddit in batches', () => {
  expect(nextCommentRequests({ has_more: true, next_cursor: 'c2' }, { cursor: 'c1' }, 'plugin-x')[0].cursor).toBe('c2');
  expect(nextCommentRequests({ has_more: true, next_cursor: 'c1' }, { cursor: 'c1' }, 'plugin-x')).toEqual([]);
  const next = nextCommentRequests({ link_fullname: 't3_id', more_stubs: [{ children: Array.from({ length: 75 }, (_, i) => String(i)) }] }, {}, 'plugin-reddit');
  expect(next).toHaveLength(2); expect(next[0].children).toHaveLength(50); expect(next[1].children).toHaveLength(25);
});
