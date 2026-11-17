// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { TauriEvent } from '../../api';
import { BOOK_ITEMS, MEDIA_ITEMS, TEXT_ARTICLES } from '../../data/mock';
import { BookWorkbench } from '../../workbenches/BookWorkbench';
import { MediaWorkbench } from '../../workbenches/MediaWorkbench';
import { TextWorkbench } from '../../workbenches/TextWorkbench';
import { useLiveArticleDetail, useLiveBooks, useLiveMedia, useLiveTextFeed } from '../useLiveDomain';

const ok = (data: unknown) => ({ ok: true, data, error: '' });
type Params = { domain: string; source_id: string; method: string; payload: Record<string, unknown> };
function connect(read: (params: Params) => unknown | Promise<unknown>) {
  const handlers = new Map<string, (event: TauriEvent<unknown>) => void>();
  const unlisten = vi.fn();
  const invoke = vi.fn(async (_command: string, payload?: { args?: { op?: string; params?: Params } }) => {
    const params = payload?.args?.params;
    if (payload?.args?.op === 'domain.snapshot') return ok(Object.fromEntries(
      ['media', 'text', 'book'].map((domain) => [`${domain}_sources`, [
        { source_id: 'curator', title: '策展归档' }, { source_id: 'provider', title: '实际来源' },
      ]]),
    ));
    if (payload?.args?.op === 'domain.invoke' && params) {
      if (params.method === 'channels') return ok({ result: [{ id: 'tech', name: '技术' }, { id: 'all', name: '全部' }] });
      return read(params);
    }
    return ok({});
  });
  window.__TAURI__ = {
    core: { invoke: invoke as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'] },
    event: { listen: vi.fn(async (channel, callback) => {
      handlers.set(channel, callback as (event: TauriEvent<unknown>) => void);
      return unlisten;
    }) },
  };
  const emit = (channel: string, payload: unknown) => act(() => {
    handlers.get(channel)?.({ event: channel, id: 1, payload });
  });
  return { invoke, emit, unlisten };
}

afterEach(() => { cleanup(); delete window.__TAURI__; vi.restoreAllMocks(); });

describe('live workbench data flow', () => {
  it.each(['media', 'text'])('%s appends real provider pages, deduplicates and stops on an empty page', async (domain) => {
    const requests: Params[] = [];
    connect((params) => {
      requests.push(params);
      const page = params.payload.page;
      return ok({ result: page === 1 ? [{ id: 'one', title: '第一条' }] : page === 2 ?
        [{ id: 'one', title: '重复条目' }, { id: 'two', title: '第二条' }] : [] });
    });
    const { result } = renderHook(() => domain === 'media' ? useLiveMedia('search', '关键词', 'provider') : useLiveTextFeed('关键词', 'tech', 'provider'));
    await waitFor(() => expect(result.current.live).toBe(true));
    await act(async () => { result.current.pagination!.loadMore(); result.current.pagination!.loadMore(); });
    await waitFor(() => expect(result.current.items.map((item) => item.id)).toEqual(['one', 'two']));
    expect(requests.map((request) => request.payload.page)).toEqual([1, 2]);
    expect(requests[1]).toMatchObject({ source_id: 'provider', payload: { query: '关键词', page: 2 } });
    expect(result.current.pagination?.page).toBe(2);
    await act(async () => result.current.pagination!.loadMore());
    await waitFor(() => expect(result.current.pagination?.hasMore).toBe(false));
    expect(result.current.items).toHaveLength(2);
  });

  it('keeps loaded content on failure and retries the same next page', async () => {
    let fail = true;
    connect(({ payload }) => payload.page === 2 && fail ? { ok: false, error: '网络中断' } :
      ok({ result: [{ id: `item-${payload.page}` }] }));
    const { result } = renderHook(() => useLiveMedia());
    await waitFor(() => expect(result.current.live).toBe(true));
    await act(async () => result.current.pagination!.loadMore());
    await waitFor(() => expect(result.current.pagination?.loadMoreError).toBe('网络中断'));
    expect(result.current.items.map((item) => item.id)).toEqual(['item-1']);
    expect(result.current.pagination?.page).toBe(1);
    fail = false;
    await act(async () => result.current.pagination!.loadMore());
    await waitFor(() => expect(result.current.items.map((item) => item.id)).toEqual(['item-1', 'item-2']));
    expect(result.current.pagination?.loadMoreError).toBeNull();
  });

  it('discards an in-flight next page after switching filters and resets pagination on refresh', async () => {
    let finish!: (value: unknown) => void;
    connect(({ payload }) => payload.page === 2 ? new Promise((resolve) => { finish = resolve; }) :
      ok({ result: [{ id: String(payload.category) }] }));
    const { result, rerender } = renderHook(({ category }) => useLiveMedia(category), { initialProps: { category: 'old' } });
    await waitFor(() => expect(result.current.live).toBe(true));
    act(() => { result.current.pagination!.loadMore(); });
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    rerender({ category: 'new' });
    await waitFor(() => expect(result.current.items[0]?.id).toBe('new'));
    await act(async () => finish(ok({ result: [{ id: 'stale' }] })));
    expect(result.current.items.map((item) => item.id)).toEqual(['new']);
    expect(result.current.pagination?.page).toBe(1);
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.live).toBe(true));
    expect(result.current.pagination?.loadingMore).toBe(false);
  });

  it.each([TextWorkbench, MediaWorkbench])('loads the next page through the workbench button', async (Workbench) => {
    connect(({ method, payload }) => ok({ result: method === 'fetch_article' ? {} :
      [{ id: `item-${payload.page}`, title: `第${payload.page}批内容` }] }));
    render(<Workbench />);
    fireEvent.click(await screen.findByRole('button', { name: '加载下一页' }));
    await screen.findByText(/已加载 2 页/);
    expect(screen.getAllByText('第1批内容').length).toBeGreaterThan(0);
    expect(screen.getAllByText('第2批内容').length).toBeGreaterThan(0);
  });

  it.each([
    ['text', TextWorkbench, TEXT_ARTICLES[0].titleZh],
    ['book', BookWorkbench, BOOK_ITEMS[0].title],
    ['media', MediaWorkbench, MEDIA_ITEMS[0].title],
  ] as const)('%s keeps an empty backend response empty, without demo content', async (_, Workbench, demoTitle) => {
    connect(() => ok({ result: [] }));
    render(<Workbench />);
    await screen.findByText(/LIVE · 0 条/);
    expect(screen.queryByText(demoTitle)).toBeNull();
    expect(screen.getByRole('button', { name: '刷新数据' })).not.toBeNull();
  });

  it('selects text providers and their native channels, then routes article detail to the same provider', async () => {
    const { invoke } = connect(({ source_id, method, payload }) => ok({ result:
      method === 'fetch_article' ? { paragraphs: [{ original: '真实正文' }] } :
        source_id === 'provider' ? [{ id: 'real-post', title: '真实主题', category: 'v2ex', channel_id: payload.channel_id }] : [],
    }));
    render(<TextWorkbench />);
    await screen.findByText(/LIVE · 0 条/);
    fireEvent.change(screen.getByLabelText('文章来源'), { target: { value: 'provider' } });
    await screen.findByText('真实正文');
    fireEvent.click(screen.getByRole('button', { name: '技术' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('call', { args: expect.objectContaining({
      op: 'domain.invoke', params: expect.objectContaining({ source_id: 'provider', method: 'fetch_feed', payload: expect.objectContaining({ channel_id: 'tech' }) }),
    }) }));
    expect(screen.getAllByText('真实主题').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '全部' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('call', { args: expect.objectContaining({
      params: expect.objectContaining({ source_id: 'provider', method: 'fetch_feed', payload: expect.objectContaining({ channel_id: 'all' }) }),
    }) }));
  });

  it('switches book providers, including an empty shelf after a populated shelf', async () => {
    connect(({ source_id, method }) => ok({ result: method === 'fetch_spread' ? {} :
      source_id === 'curator' ? [{ id: 'real-book', title: '我的真实书籍' }] : [],
    }));
    render(<BookWorkbench />);
    await screen.findByText(/LIVE · 1 条/);
    fireEvent.change(screen.getByLabelText('书籍来源'), { target: { value: 'provider' } });
    await screen.findByText(/LIVE · 0 条/);
    expect(screen.queryByText('我的真实书籍')).toBeNull();
  });

  it('recovers after startup connection and refreshes relevant write-back events, then cleans up listeners', async () => {
    let items: unknown[] = [];
    const { emit, unlisten, invoke } = connect(() => ok({ result: items }));
    const { result, unmount } = renderHook(() => useLiveMedia());
    await waitFor(() => expect(result.current.live).toBe(true));
    items = [{ id: 'live-track', title: '入站推荐' }];
    emit('omnidesk:bus', { topic: 'entity.recommend.updated', payload: { domains: ['media'] } });
    await waitFor(() => expect(result.current.items[0]?.title).toBe('入站推荐'));
    const count = invoke.mock.calls.length;
    emit('omnidesk:bus', { topic: 'entity.recommend.updated', payload: { domains: ['text'] } });
    expect(invoke.mock.calls.length).toBe(count);
    items = [];
    emit('omnidesk:bridge-status', { status: 'connected' });
    await waitFor(() => expect(result.current.items).toEqual([]));
    await waitFor(() => expect(result.current.live).toBe(true));
    unmount();
    await waitFor(() => expect(unlisten).toHaveBeenCalledTimes(2));
  });

  it('reports provider failures, retaining the chosen source for retry and never silently falling back', async () => {
    connect(() => ({ ok: false, error_code: 'auth_required', error: '请登录' }));
    const { result } = renderHook(() => useLiveBooks('provider'));
    await waitFor(() => expect(result.current.error).toBe('请登录'));
    expect(result.current.sourceId).toBe('provider');
    expect(result.current.items).toEqual([]);
    expect(result.current.live).toBe(false);
  });

  it('discards stale responses when switching source and preserves the source used to read book pages', async () => {
    let finish!: (value: unknown) => void;
    connect(({ source_id }) => source_id === 'curator' ? new Promise((resolve) => { finish = resolve; }) :
      ok({ result: [{ id: 'book', title: '新来源', source_id: 'upstream-origin' }] }));
    const { result, rerender } = renderHook(({ source }) => useLiveBooks(source), { initialProps: { source: 'curator' } });
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    rerender({ source: 'provider' });
    await waitFor(() => expect(result.current.items[0]?.sourcePlugin).toBe('provider'));
    await act(async () => finish(ok({ result: [{ id: 'old', title: '过期响应' }] })));
    expect(result.current.items.map((book) => book.id)).toEqual(['book']);
  });

  it('clears the previous article body when the next article fails', async () => {
    connect(({ payload }) => payload.article_id === 'first' ? ok({ result: { paragraphs: [{ original: '上一篇正文' }] } }) :
      { ok: false, error: '读取失败' });
    const { result, rerender } = renderHook(({ id }) => useLiveArticleDetail(id, 'provider'), { initialProps: { id: 'first' } });
    await waitFor(() => expect(result.current.blocks).toHaveLength(1));
    rerender({ id: 'second' });
    await waitFor(() => expect(result.current.error).toBe('读取失败'));
    expect(result.current.blocks).toEqual([]);
  });
});
