import { useCallback, useEffect, useRef, useState } from 'react';
import { callCapability, domainSources, isTauriAvailable, listPlugins } from '../api';
import type { CommentNode } from '../data/mock';

type Dict = Record<string, unknown>;
const object = (value: unknown): Dict => value && typeof value === 'object' ? value as Dict : {};
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];

export function mapComments(value: unknown): CommentNode[] {
  return array(value).map((entry, index) => {
    const node = object(entry);
    return {
      id: String(node.id ?? `comment-${index}`), author: String(node.author ?? '匿名'),
      roleBadge: String(node.role_badge ?? '') || undefined,
      timeAgo: String(node.created_at ?? ''), score: Number(node.score ?? 0),
      original: String(node.body ?? node.original ?? ''),
      translated: String(node.translated_body ?? node.translated ?? '') || undefined,
      children: mapComments(node.replies ?? node.children),
    };
  });
}

export function mergeComments(previous: CommentNode[], incoming: CommentNode[]): CommentNode[] {
  const merged = new Map(previous.map(node => [node.id, node]));
  for (const node of incoming) {
    const old = merged.get(node.id);
    merged.set(node.id, old ? { ...old, ...node, children: mergeComments(old.children ?? [], node.children ?? []) } : node);
  }
  return [...merged.values()];
}

/** Provider-owned pagination: never invent a cursor or reuse a different source's comments. */
export function nextCommentRequests(data: Dict, request: Dict, provider: string): Dict[] {
  if (provider === 'plugin-reddit') {
    const children = array(data.more_stubs).flatMap(stub => array(object(stub).children));
    const requests: Dict[] = [];
    for (let i = 0; i < children.length; i += 50) requests.push({
      action: 'expand_more', link_id: data.link_fullname, children: children.slice(i, i + 50),
    });
    return requests;
  }
  if (provider === 'plugin-zhihu' && object(data.paging).is_end === false) {
    const next = object(data.paging).next;
    let offset: number | null = null;
    if (typeof next === 'string' && next) {
      try {
        const raw = new URL(next).searchParams.get('offset');
        if (raw !== null && /^\d+$/.test(raw)) offset = Number(raw);
      } catch { /* Use the provider's returned count for legacy endpoints. */ }
    }
    offset ??= Number(data.offset ?? request.offset ?? 0) + Number(data.limit ?? data.count ?? 0);
    return offset > Number(request.offset ?? 0) ? [{ ...request, offset }] : [];
  }
  if (provider === 'plugin-v2ex' && Number(data.total_pages) > Number(request.page ?? 1)) {
    return [{ ...request, page: Number(request.page ?? 1) + 1 }];
  }
  if (data.has_more === true && typeof data.next_offset === 'number' && data.next_offset > Number(request.offset ?? 0)) {
    return [{ ...request, offset: data.next_offset }];
  }
  if (provider === 'plugin-x' && data.has_more === true && typeof data.next_cursor === 'string' && data.next_cursor && data.next_cursor !== request.cursor) return [{ ...request, cursor: data.next_cursor }];
  return [];
}

export function useLiveComments(articleId: string, sourceId?: string, explicitProvider?: string) {
  const [comments, setComments] = useState<CommentNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [supported, setSupported] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(0);
  const [nonce, setNonce] = useState(0);
  const generation = useRef(0);
  const busy = useRef(false);
  const providerRef = useRef('');
  const pending = useRef<Dict[]>([]);
  const rawComments = useRef<unknown[]>([]);

  const fetchPage = useCallback(async (provider: string, request: Dict, version: number, append: boolean) => {
    if (busy.current || generation.current !== version) return;
    busy.current = true;
    setLoading(true); setError(null);
    try {
      const payload = request.action === 'expand_more' ? { ...request, comments: rawComments.current } : request;
      const result = await callCapability<Dict>({ capability: 'text.comments.v1', providerPluginId: provider, payload, timeoutMs: 60000 });
      if (version !== generation.current) return;
      if (!result.available || !result.ok) {
        setError(result.error?.message || '评论服务不可用');
        return;
      }
      const data = result.data;
      pending.current.shift();
      pending.current.push(...nextCommentRequests(data, request, provider));
      const nodes = mapComments(data.comments);
      rawComments.current = array(data.comments);
      setComments(previous => append ? mergeComments(previous, nodes) : nodes);
      setHasMore(pending.current.length > 0);
      setPage(previous => append ? previous + 1 : 1);
    } catch (err) {
      if (version === generation.current) setError(String(err));
    } finally {
      if (version === generation.current) { busy.current = false; setLoading(false); }
    }
  }, []);

  useEffect(() => {
    const version = ++generation.current;
    busy.current = false; pending.current = []; rawComments.current = [];
    setComments([]); setError(null); setSupported(false); setHasMore(false); setPage(0); setLoading(false);
    if (!articleId || (!sourceId && !explicitProvider) || !isTauriAvailable()) return;
    setLoading(true);
    void (async () => {
      const [sources, plugins] = await Promise.all([domainSources('text'), listPlugins()]);
      if (version !== generation.current) return;
      const provider = explicitProvider || (sources.ok ? sources.data.find(source => source.sourceId === sourceId)?.pluginId : undefined);
      const plugin = plugins.ok ? plugins.data.plugins.find(item => item.plugin_id === provider) : undefined;
      if (!sources.ok || !plugins.ok) { setLoading(false); setError('无法读取评论数据源，请重试'); return; }
      if (!provider || !plugin?.provides?.includes('text.comments.v1')) { setLoading(false); return; }
      setSupported(true); providerRef.current = provider;
      const request = { action: 'fetch_comments', article_id: articleId, offset: 0, page: 1, limit: 40, expand_more: false };
      pending.current = [request];
      await fetchPage(provider, request, version, false);
    })().catch(err => { if (generation.current === version) { setLoading(false); setError(String(err)); } });
    return () => { generation.current++; };
  }, [articleId, sourceId, explicitProvider, nonce, fetchPage]);

  const loadMore = useCallback(() => {
    const request = pending.current[0];
    if (request) void fetchPage(providerRef.current, request, generation.current, page > 0);
  }, [fetchPage, page]);
  return { comments, loading, error, supported, retry: () => setNonce(n => n + 1),
    pagination: { page, hasMore, loadingMore: loading, loadMoreError: error, loadMore } };
}
