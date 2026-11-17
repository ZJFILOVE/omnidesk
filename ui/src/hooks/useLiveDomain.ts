import { useLiveComments } from './useLiveComments';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { LoadMoreState } from '../components/LoadMoreButton';
import {
  callCapability,
  domainSources,
  invokeDomain,
  isTauriAvailable,
  listenTauriEvent,
  type BusEventEnvelope,
  type EventBridgeSnapshot,
  type DomainSourceDescriptor,
  type RpcResult,
} from '../api';
import type {
  BookItem,
  BookSpread,
  CommentNode,
  MediaItem,
  StreamResolverDescriptor,
  TextArticle,
  VocabCard,
} from '../data/mock';

type Dict = Record<string, unknown>;

const str = (v: unknown, fallback = ''): string =>
  typeof v === 'string' && v.length > 0 ? v : fallback;
const num = (v: unknown, fallback = 0): number =>
  typeof v === 'number' && Number.isFinite(v) ? v : fallback;
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const dict = (v: unknown): Dict => (v && typeof v === 'object' ? (v as Dict) : {});

function toStringMap(value: unknown): Record<string, string> | undefined {
  const raw = dict(value);
  const out: Record<string, string> = {};
  for (const [key, entry] of Object.entries(raw)) {
    if (typeof entry === 'string' && key.trim() && entry.trim()) {
      out[key] = entry;
    }
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

function toStreamResolver(value: unknown): StreamResolverDescriptor | undefined {
  const raw = dict(value);
  const capability = str(raw.capability);
  if (!capability) return undefined;
  return {
    capability,
    providerPluginId: str(raw.provider_plugin_id) || undefined,
    payload: dict(raw.payload),
    qualityOptions: arr(raw.quality_options)
      .map((entry) => (isRecord(entry) ? str(entry.value) : str(entry)))
      .filter(Boolean),
    settingsAction: str(raw.settings_action) || undefined,
  };
}

function isRecord(value: unknown): value is Dict {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function formatDuration(sec: number): string {
  if (sec <= 0) return '--:--';
  const total = Math.round(sec);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

const GRADIENTS = [
  'linear-gradient(135deg, #263849, #326278)',
  'linear-gradient(135deg, #493728, #88603a)',
  'linear-gradient(135deg, #263d35, #37745b)',
  'linear-gradient(135deg, #352c49, #6d4f84)',
];

function gradientFor(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
  return GRADIENTS[hash % GRADIENTS.length];
}

export function textChannelFor(sourceId: string, category: string): TextArticle['channelId'] {
  const key = `${sourceId} ${category}`.toLowerCase();
  if (key.includes('reddit')) return 'reddit';
  if (key.includes('linuxdo')) return 'linuxdo';
  if (key.includes('zhihu')) return 'zhihu';
  if (key.includes('journal') || key.includes('arxiv') || key.includes('pubmed') || key.includes('nature')) {
    return 'journal';
  }
  return 'news';
}

export function toTextArticle(raw: unknown, fallbackSource = ''): TextArticle | null {
  const item = dict(raw);
  const id = str(item.id);
  if (!id) return null;
  const sourceId = str(item.source_id, fallbackSource);
  return {
    id,
    channelId: textChannelFor(sourceId, str(item.category)),
    sourceName: str(item.source_name, sourceId || 'Live source'),
    title: str(item.title, id),
    titleZh: str(item.title_zh, str(item.title, id)),
    author: str(item.author, str(item.authors)),
    publishedAt: str(item.published_at),
    readTime: str(item.read_time, `${Math.max(1, Math.round(str(item.excerpt).length / 320))} 分钟`),
    curationMode: (item.curation_mode as TextArticle['curationMode']) ?? undefined,
    businessDate: (item.business_date as string) || undefined,
    previouslyRecommendedOn: (item.previously_recommended_on as string) || undefined,
    doi: str(item.doi) || undefined,
    journalName: str(item.journal_name) || undefined,
    impactScore: str(item.impact_score) || undefined,
    tags: arr(item.tags).map((t) => String(t)),
    summaryZh: str(item.summary_zh, str(item.excerpt)),
    blocks: [],
    comments: [],
  };
}

export function toBlocks(raw: unknown): TextArticle['blocks'] {
  return arr(raw).map((entry, index) => {
    const block = dict(entry);
    const attachment = /^\[图片附件\]\s+(https?:\/\/\S+)(?:\s+\((.*)\))?$/u.exec(str(block.original, str(block.text)));
    const kind = attachment ? 'image' : str(block.kind, 'paragraph');
    const safeKind = (
      ['heading', 'paragraph', 'quote', 'code', 'formula', 'image'] as const
    ).includes(kind as never)
      ? (kind as TextArticle['blocks'][number]['kind'])
      : 'paragraph';
    return {
      id: str(block.id, `block-${index}`),
      kind: safeKind,
      original: str(block.original, str(block.text)),
      translated: str(block.translated) || undefined,
      language: str(block.language) || undefined,
      imageUrl: attachment?.[1] || str(block.image_url, str(block.imageUrl)) || undefined,
      alt: attachment?.[2] || str(block.alt) || undefined,
      caption: str(block.caption) || undefined,
    };
  });
}

function toComments(raw: unknown): CommentNode[] {
  return arr(raw).map((entry, index) => {
    const node = dict(entry);
    return {
      id: str(node.id, `comment-${index}`),
      author: str(node.author, '匿名'),
      roleBadge: str(node.role_badge) || undefined,
      timeAgo: str(node.created_at),
      score: num(node.score),
      original: str(node.body, str(node.original)),
      translated: str(node.translated_body, str(node.translated)) || undefined,
      children: toComments(node.replies ?? node.children),
    };
  });
}

export function toBookItem(raw: unknown): BookItem | null {
  const item = dict(raw);
  const id = str(item.id);
  if (!id) return null;
  const fmt = str(item.format, 'epub');
  const format: BookItem['format'] =
    fmt as BookItem['format'];
  return {
    id,
    title: str(item.title, id),
    author: str(item.author, '未知作者'),
    format,
    language: str(item.language),
    progressPct: num(item.progress_percent),
    totalPages: Math.max(1, num(item.total_pages, 1)),
    currentPage: Math.max(1, num(item.current_page, 1)),
    coverGradient: gradientFor(id),
    coverUrl: str(dict(item.extra).cover_data_url, str(item.cover_url)) || undefined,
    badge: str(item.badge, format.toUpperCase()),
    sourcePlugin: str(item.source_id, 'book.source.v1'),
    bilingualReady: Boolean(item.bilingual_ready),
    spreads: toBookSpreads(item.spreads, id),
  };
}

function toBookSpreads(raw: unknown, bookId: string): BookSpread[] {
  return arr(raw).map((entry, index) => {
    const spread = dict(entry);
    const blocks = arr(spread.blocks).map(dict);
    const left = blocks.map((block, i) => ({
      id: str(block.id, `${bookId}-p${index}-l${i}`),
      text: str(block.original, str(block.translated)),
      keyTerms: arr(block.highlighted_words).map(String),
      imageUrl: str(block.image_url) || undefined,
    }));
    const right = blocks.filter((block) => str(block.translated) || str(block.translated_image_url)).map((block, i) => ({
      id: str(block.id, `${bookId}-p${index}-r${i}`),
      text: str(block.translated),
      imageUrl: str(block.translated_image_url) || undefined,
    }));
    return {
      page: Math.max(1, num(spread.page_number, index + 1)),
      totalPages: Math.max(1, num(spread.total_pages, 1)),
      chapterTitle: str(spread.chapter_title, `第 ${index + 1} 页`),
      leftTitle: '原文',
      leftParagraphs: left,
      rightTitle: '译文',
      rightParagraphs: right,
    };
  });
}

export function toMediaItem(raw: unknown): MediaItem | null {
  const item = dict(raw);
  const id = str(item.id);
  if (!id) return null;
  const kind = str(item.kind, 'video');
  const duration = num(item.duration_sec);
  const extra = dict(item.extra);
  const playUrl = str(item.play_url);
  let youtubeId = '';
  try {
    const url = new URL(playUrl);
    if (['youtube.com', 'www.youtube.com', 'm.youtube.com'].includes(url.hostname)) youtubeId = url.searchParams.get('v') ?? '';
    if (url.hostname === 'youtu.be') youtubeId = url.pathname.slice(1);
  } catch { /* Local files do not need a resolver. */ }
  const inferredYoutubeResolver: StreamResolverDescriptor | undefined = /^[a-zA-Z0-9_-]{11}$/.test(youtubeId) ? {
    capability: 'media.youtube.v1', providerPluginId: 'plugin-youtube',
    payload: { action: 'resolve_stream', item_id: youtubeId }, qualityOptions: ['best', '1080p', '720p', '480p'],
  } : undefined;

  const httpHeaders =
    toStringMap(item.http_headers) ??
    toStringMap(item.headers) ??
    toStringMap(extra.http_headers);
  const appleMusicSongId =
    str(extra.apple_music_song_id) ||
    str(extra.song_id) ||
    playUrl.match(/music\.apple\.com\/[a-z]{2}\/song\/(?:[^/]+\/)?(\d+)/)?.[1] ||
    '';
  return {
    id,
    sourceId: str(item.source_id, 'media.source.v1'),
    kind: kind === 'music' || kind === 'nas' ? kind : 'video',
    title: str(item.title, id),
    subtitle: str(item.subtitle),
    creator: str(item.creator),
    creatorId: (typeof extra.mid === 'number' ? String(extra.mid) : str(extra.mid, str(extra.channel_id, str(extra.creator_id)))) || undefined,
    collection: typeof dict(extra.collection).id === 'string' ? {
      id: str(dict(extra.collection).id), type: str(dict(extra.collection).type),
    } : undefined,
    durationSec: duration,
    durationLabel: formatDuration(duration),
    aspect: item.aspect === '1:1' ? '1:1' : '16:9',
    badge: str(item.source_badge, 'LIVE'),
    curationMode: (item.curation_mode as MediaItem['curationMode']) ?? undefined,
    businessDate: (item.business_date as string) || undefined,
    previouslyRecommendedOn: (item.previously_recommended_on as string) || undefined,
    thumbGradient: gradientFor(id),
    coverUrl: str(item.cover_url, str(item.coverUrl)) || undefined,
    thumbTag: str(item.source_badge, kind.toUpperCase()),
    playUrl,
    audioUrl: str(item.audio_url) || undefined,
    httpHeaders,
    qualityLabel: str(item.quality_label) || undefined,
    streamResolver: toStreamResolver(extra.stream_resolver) ?? inferredYoutubeResolver,
    summary: str(item.summary),
    listeningCue: str(item.listening_cue) || undefined,
    appleMusicSongId: appleMusicSongId || undefined,
    chapters: arr(item.chapters).map((entry, index) => {
      const ch = dict(entry);
      return {
        id: str(ch.id, `ch-${index}`),
        startSec: num(ch.start_sec),
        endSec: num(ch.end_sec, num(ch.start_sec)),
        label: str(ch.label, `片段 ${index + 1}`),
        kind: (ch.kind as 'content' | 'sponsor' | 'ad') ?? 'content',
      };
    }),
  };
}

export function toVocabCard(raw: unknown): VocabCard | null {
  const card = dict(raw);
  const word = str(card.word, str(card.lemma));
  if (!word) return null;
  return {
    id: str(card.id, `vocab-${word.toLowerCase()}`),
    word,
    phonetic: str(card.phonetic),
    pos: str(card.pos, 'term'),
    meaningZh: str(card.meaning, str(card.meaning_zh)),
    etymology: str(card.etymology),
    contextSentence: str(card.context_sentence),
    bookTitle: str(card.source_book, str(card.book_title)),
    sourceDate: str(card.source_date),
    intervalDays: num(card.interval_days, 1),
    easeFactor: num(card.ease_factor, 2.5),
    dueLabel: str(card.due_label, '待复习'),
  };
}

export interface LiveFeed<T> {
  items: T[];
  sources: DomainSourceDescriptor[];
  categories: string[];
  channels: { id: string; label: string }[];
  sourceId: string;
  live: boolean;
  loading: boolean;
  error: string | null;
  reload: () => void;
  pagination?: LoadMoreState;
}

function useLiveSource<T extends { id: string }>(
  domain: 'text' | 'book' | 'media',
  method: string,
  payload: Dict,
  mapItem: (raw: unknown, sourceId: string) => T | null,
  deps: unknown[],
  preferredSourceId = '',
): LiveFeed<T> {
  const [items, setItems] = useState<T[]>([]);
  const [sources, setSources] = useState<DomainSourceDescriptor[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [channels, setChannels] = useState<{ id: string; label: string }[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [live, setLive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const requestVersion = useRef(0);
  const morePending = useRef(false);
  const paginated = method === 'fetch_feed' || method === 'fetch_items';
  const payloadKey = JSON.stringify(payload);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let disposed = false;
    const unlisteners: (() => void | Promise<void>)[] = [];
    const register = async (subscription: Promise<(() => void | Promise<void>) | null>) => {
      try {
        const unlisten = await subscription;
        if (!unlisten) return;
        if (disposed) await unlisten();
        else unlisteners.push(unlisten);
      } catch {
        // Manual refresh remains available if the event bridge is unavailable.
      }
    };
    void register(listenTauriEvent<BusEventEnvelope>('omnidesk:bus', (event) => {
      const domains = event.payload?.domains;
      if (event.topic === 'plugin.state_changed' ||
          (event.topic === 'entity.recommend.updated' &&
            (!Array.isArray(domains) || domains.includes(domain)))) reload();
    }));
    void register(listenTauriEvent<EventBridgeSnapshot>('omnidesk:bridge-status', (event) => {
      if (event.status === 'connected') reload();
    }));
    return () => {
      disposed = true;
      for (const unlisten of unlisteners) void Promise.resolve().then(unlisten).catch(() => {});
    };
  }, [domain, reload]);

  useEffect(() => {
    if (!isTauriAvailable()) {
      setLive(false);
      setLoading(false);
      return;
    }
    let cancelled = false;
    requestVersion.current += 1;
    morePending.current = false;
    setPage(1);
    setHasMore(false);
    setLoadingMore(false);
    setLoadMoreError(null);
    setLoading(true);
    setError(null);
    setItems([]);
    setLive(false);
    (async () => {
      const sourcesRes = await domainSources(domain);
      if (cancelled || !mounted.current) return;
      if (!sourcesRes.available || !sourcesRes.ok) {
        setError(sourcesRes.available ? sourcesRes.error.message : null);
        setLive(false);
        setLoading(false);
        return;
      }
      const list = sourcesRes.data;
      setSources(list);
      if (list.length === 0) {
        setCategories([]);
        setChannels([]);
        setSourceId('');
        setError('尚无已启用的数据源，请在插件中心启用对应插件后刷新。');
        setLive(false);
        setLoading(false);
        return;
      }
      const chosen = preferredSourceId || list[0].sourceId;
      setSourceId(chosen);
      if (!list.some((s) => s.sourceId === chosen)) {
        setCategories([]);
        setChannels([]);
        setError('所选数据源已停用，请切换来源或在插件中心重新启用。');
        setLoading(false);
        return;
      }
      setCategories(list.find((source) => source.sourceId === chosen)?.categories ?? []);
      if (domain === 'text') {
        const channelResult = await invokeDomain<unknown[]>({ domain, sourceId: chosen, method: 'channels' });
        if (cancelled || !mounted.current) return;
        setChannels(channelResult.ok ? arr(channelResult.data).map((entry) => {
          const channel = dict(entry);
          return { id: str(channel.id), label: str(channel.name, str(channel.id)) };
        }).filter((channel) => channel.id) : []);
      }
      const res: RpcResult<unknown> = await invokeDomain({
        domain,
        sourceId: chosen,
        method,
        payload: JSON.parse(payloadKey) as Dict,
        timeoutMs: 30000,
      });
      if (cancelled || !mounted.current) return;
      setLoading(false);
      if (!res.available) {
        setLive(false);
        return;
      }
      if (!res.ok) {
        setError(res.error.message);
        setLive(false);
        return;
      }
      const mapped = arr(res.data)
        .map((entry) => mapItem(entry, chosen))
        .filter((entry): entry is T => entry !== null);
      setSourceId(chosen);
      setItems(mapped);
      setHasMore(paginated && mapped.length > 0);
      // A successful empty response is still live data, never a request for demo content.
      setLive(true);
    })().catch((err: unknown) => {
      if (cancelled || !mounted.current) return;
      setLoading(false);
      setLive(false);
      setError(err instanceof Error ? err.message : String(err));
    });
    return () => {
      cancelled = true;
      requestVersion.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, method, payloadKey, nonce, preferredSourceId, ...deps]);

  const loadMore = useCallback(async () => {
    if (!paginated || !live || loading || !hasMore || morePending.current) return;
    const version = requestVersion.current;
    morePending.current = true;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const res = await invokeDomain<unknown>({
        domain, sourceId, method,
        payload: { ...JSON.parse(payloadKey), page: page + 1 },
        timeoutMs: 30000,
      });
      if (version !== requestVersion.current || !mounted.current) return;
      if (!res.ok) {
        setLoadMoreError(res.available ? res.error.message : '数据源连接不可用，请重试。');
        return;
      }
      const seen = new Set(items.map((item) => item.id));
      const next = arr(res.data).map((entry) => mapItem(entry, sourceId))
        .filter((item): item is T => {
          if (!item || seen.has(item.id)) return false;
          seen.add(item.id);
          return true;
        });
      setItems((previous) => [...previous, ...next]);
      if (next.length > 0) setPage(page + 1);
      // Some sources repeat their first page when exhausted; avoid an endless loop.
      setHasMore(next.length > 0);
    } catch (err) {
      if (version === requestVersion.current && mounted.current) {
        setLoadMoreError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (version === requestVersion.current && mounted.current) {
        morePending.current = false;
        setLoadingMore(false);
      }
    }
  }, [domain, sourceId, method, payloadKey, page, items, live, loading, hasMore, paginated, mapItem]);

  return { items, sources, categories, channels, sourceId, live, loading, error, reload,
    pagination: paginated && live ? { page, hasMore, loadingMore, loadMoreError, loadMore } : undefined };
}

export function useLiveTextFeed(query = '', channelId = '', sourceId = ''): LiveFeed<TextArticle> {
  return useLiveSource<TextArticle>(
    'text',
    'fetch_feed',
    { query, channel_id: channelId, page: 1 },
    (raw, sid) => toTextArticle(raw, sid),
    [query, channelId],
    sourceId,
  );
}

export function useLiveBooks(sourceId = ''): LiveFeed<BookItem> {
  return useLiveSource<BookItem>('book', 'list_books', {}, (raw, sid) => {
    const book = toBookItem(raw);
    return book ? { ...book, sourcePlugin: sid } : null;
  }, [], sourceId);
}

export function useLiveMedia(
  category = '',
  query = '',
  sourceId = '',
): LiveFeed<MediaItem> {
  return useLiveSource<MediaItem>(
    'media',
    'fetch_items',
    { category, query, page: 1 },
    (raw) => toMediaItem(raw),
    [category, query],
    sourceId,
  );
}

export interface LiveArticleDetail {
  discussion: ReturnType<typeof useLiveComments>;
  blocks: TextArticle['blocks'];
  comments: CommentNode[];
  loading: boolean;
  error: string | null;
}

/** Fetch one article body/comments from the live text source that produced it. */
export function useLiveArticleDetail(
  articleId: string,
  sourceId?: string,
): LiveArticleDetail {
  const discussion = useLiveComments(articleId, sourceId);
  const [blocks, setBlocks] = useState<TextArticle['blocks']>([]);
  const [comments, setComments] = useState<CommentNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!articleId || !isTauriAvailable()) {
      setBlocks([]);
      setComments([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setBlocks([]);
    setComments([]);
    setLoading(true);
    setError(null);
    (async () => {
      const res = await invokeDomain<Dict>({
        domain: 'text',
        sourceId,
        method: 'fetch_article',
        payload: { article_id: articleId },
        timeoutMs: 30000,
      });
      if (cancelled) return;
      setLoading(false);
      if (!res.available) return;
      if (!res.ok) {
        setError(res.error.message);
        return;
      }
      const data = dict(res.data);
      setBlocks(toBlocks(data.paragraphs ?? data.blocks));
      setComments(toComments(data.comments));
    })().catch((err: unknown) => {
      if (cancelled) return;
      setLoading(false);
      setError(err instanceof Error ? err.message : String(err));
    });
    return () => {
      cancelled = true;
    };
  }, [articleId, sourceId]);

  return { blocks, comments: discussion.supported ? discussion.comments : comments, loading, error, discussion };
}

export interface LiveSpread {
  spreads: BookSpread[];
  loading: boolean;
  error: string | null;
}

/** Fetch a page spread from the live book source. */
export function useLiveSpread(bookId: string, page = 1, sourceId?: string): LiveSpread {
  const [spreads, setSpreads] = useState<BookSpread[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!bookId || !isTauriAvailable()) {
      setSpreads([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setSpreads([]);
    setLoading(true);
    setError(null);
    (async () => {
      const res = await invokeDomain<Dict>({
        domain: 'book',
        sourceId,
        method: 'fetch_spread',
        payload: { book_id: bookId, page_number: page },
        timeoutMs: 120000,
      });
      if (cancelled) return;
      setLoading(false);
      if (!res.available) return;
      if (!res.ok) {
        setError(res.error.message);
        return;
      }
      setSpreads(toBookSpreads([res.data], bookId));
    })().catch((err: unknown) => {
      if (cancelled) return;
      setLoading(false);
      setError(err instanceof Error ? err.message : String(err));
    });
    return () => {
      cancelled = true;
    };
  }, [bookId, page, sourceId]);

  return { spreads, loading, error };
}

export interface VocabPersistence {
  cards: VocabCard[];
  live: boolean;
  error: string | null;
  add: (card: Partial<VocabCard> & { word: string }) => Promise<VocabCard | null>;
  review: (word: string, quality: number) => Promise<VocabCard | null>;
  reload: () => void;
}

/** Persist SM-2 cards through the backend `vocab.sm2.v1` capability. */
export function usePersistentVocab(fallback: VocabCard[]): VocabPersistence {
  const [cards, setCards] = useState<VocabCard[]>([]);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!isTauriAvailable()) {
      setCards(fallback);
      setLive(false);
      return;
    }
    let cancelled = false;
    setError(null);
    (async () => {
      const res = await callCapability<{ cards?: unknown[] }>({
        capability: 'vocab.sm2.v1',
        payload: { action: 'list' },
      });
      if (cancelled) return;
      if (!res.available) {
        setCards([]);
        setLive(false);
        return;
      }
      if (!res.ok) {
        setError(res.error.message);
        setCards([]);
        setLive(false);
        return;
      }
      const mapped = arr(res.data.cards)
        .map(toVocabCard)
        .filter((c): c is VocabCard => c !== null);
      setCards(mapped);
      setLive(true);
    })().catch((err: unknown) => {
      if (cancelled) return;
      setCards([]);
      setLive(false);
      setError(err instanceof Error ? err.message : String(err));
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce]);

  const add = useCallback(async (input: Partial<VocabCard> & { word: string }) => {
    const res = await callCapability<{ card?: unknown }>({
      capability: 'vocab.sm2.v1',
      payload: {
        action: 'add',
        card: {
          word: input.word,
          meaning: input.meaningZh,
          context_sentence: input.contextSentence,
          source_book: input.bookTitle,
          phonetic: input.phonetic,
          pos: input.pos,
        },
      },
    });
    if (!res.available) return null;
    if (!res.ok) {
      setError(res.error.message);
      return null;
    }
    const card = toVocabCard(res.data.card);
    if (card) {
      setCards((prev) => [card, ...prev.filter((c) => c.word.toLowerCase() !== card.word.toLowerCase())]);
      setLive(true);
    }
    return card;
  }, []);

  const review = useCallback(async (word: string, quality: number) => {
    const res = await callCapability<{ card?: unknown }>({
      capability: 'vocab.sm2.v1',
      payload: { action: 'review', word, quality },
    });
    if (!res.available) return null;
    if (!res.ok) {
      setError(res.error.message);
      return null;
    }
    const card = toVocabCard(res.data.card);
    if (card) {
      setCards((prev) => prev.map((c) => (c.word.toLowerCase() === card.word.toLowerCase() ? card : c)));
      setLive(true);
    }
    return card;
  }, []);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { cards, live, error, add, review, reload };
}

export interface NoteRecord {
  id: string;
  domain: string;
  itemId: string;
  text: string;
  createdAt: string;
}

const NOTE_STORE_KEY = 'omnidesk.notes.v1';

/** Notes persist to localStorage today and stay schema-compatible with a future notes capability. */
export function usePersistentNotes(domain: 'text' | 'book') {
  const [notesByItem, setNotesByItem] = useState<Record<string, NoteRecord[]>>({});

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(NOTE_STORE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Record<string, NoteRecord[]>;
      if (parsed && typeof parsed === 'object') setNotesByItem(parsed);
    } catch {
      /* corrupt or unavailable storage must never break the workbench */
    }
  }, []);

  const persist = useCallback((next: Record<string, NoteRecord[]>) => {
    setNotesByItem(next);
    try {
      window.localStorage.setItem(NOTE_STORE_KEY, JSON.stringify(next));
    } catch {
      /* quota/unavailable storage degrades to in-memory notes */
    }
  }, []);

  const addNote = useCallback(
    (itemId: string, text: string) => {
      const trimmed = text.trim();
      if (!itemId || !trimmed) return;
      const record: NoteRecord = {
        id: `note-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        domain,
        itemId,
        text: trimmed,
        createdAt: new Date().toISOString(),
      };
      persist({
        ...notesByItem,
        [itemId]: [...(notesByItem[itemId] ?? []), record],
      });
    },
    [domain, notesByItem, persist],
  );

  return { notesByItem, addNote };
}
