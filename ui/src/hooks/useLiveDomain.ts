import { useCallback, useEffect, useRef, useState } from 'react';
import {
  callCapability,
  domainSources,
  invokeDomain,
  isTauriAvailable,
  type DomainSourceDescriptor,
  type RpcResult,
} from '../api';
import type {
  BookItem,
  BookSpread,
  CommentNode,
  MediaItem,
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
  'from-sky-500/25 via-cyan-400/10 to-transparent',
  'from-amber-500/25 via-orange-400/10 to-transparent',
  'from-emerald-500/25 via-teal-400/10 to-transparent',
  'from-violet-500/25 via-fuchsia-400/10 to-transparent',
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

function toBlocks(raw: unknown): TextArticle['blocks'] {
  return arr(raw).map((entry, index) => {
    const block = dict(entry);
    const kind = str(block.kind, 'paragraph');
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
    fmt === 'pdf' || fmt === 'english_daily' ? fmt : 'epub';
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
    }));
    const right = blocks.map((block, i) => ({
      id: str(block.id, `${bookId}-p${index}-r${i}`),
      text: str(block.translated, str(block.original)),
    }));
    return {
      page: Math.max(1, num(spread.page_number, index + 1)),
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
  return {
    id,
    sourceId: str(item.source_id, 'media.source.v1'),
    kind: kind === 'music' || kind === 'nas' ? kind : 'video',
    title: str(item.title, id),
    subtitle: str(item.subtitle),
    creator: str(item.creator),
    durationSec: duration,
    durationLabel: formatDuration(duration),
    aspect: item.aspect === '1:1' ? '1:1' : '16:9',
    badge: str(item.source_badge, 'LIVE'),
    curationMode: (item.curation_mode as MediaItem['curationMode']) ?? undefined,
    businessDate: (item.business_date as string) || undefined,
    previouslyRecommendedOn: (item.previously_recommended_on as string) || undefined,
    thumbGradient: gradientFor(id),
    thumbTag: str(item.source_badge, kind.toUpperCase()),
    playUrl: str(item.play_url),
    summary: str(item.summary),
    listeningCue: str(item.listening_cue) || undefined,
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
  sourceId: string;
  live: boolean;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

function useLiveSource<T>(
  domain: 'text' | 'book' | 'media',
  method: string,
  payload: Dict,
  mapItem: (raw: unknown, sourceId: string) => T | null,
  deps: unknown[],
): LiveFeed<T> {
  const [items, setItems] = useState<T[]>([]);
  const [sources, setSources] = useState<DomainSourceDescriptor[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [live, setLive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
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
    if (!isTauriAvailable()) {
      setLive(false);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
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
        setLive(false);
        setLoading(false);
        return;
      }
      const chosen = (sourceId && list.some((s) => s.sourceId === sourceId) ? sourceId : list[0].sourceId);
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
      // An empty payload means the source is reachable but has nothing configured yet;
      // keep the workbench on its offline sample data instead of showing an empty LIVE view.
      setLive(mapped.length > 0);
    })().catch((err: unknown) => {
      if (cancelled || !mounted.current) return;
      setLoading(false);
      setLive(false);
      setError(err instanceof Error ? err.message : String(err));
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, method, payloadKey, sourceId, nonce, ...deps]);

  return { items, sources, sourceId, live, loading, error, reload };
}

export function useLiveTextFeed(query = '', channelId = ''): LiveFeed<TextArticle> {
  return useLiveSource<TextArticle>(
    'text',
    'fetch_feed',
    { query, channel_id: channelId, page: 1 },
    (raw, sid) => toTextArticle(raw, sid),
    [query, channelId],
  );
}

export function useLiveBooks(): LiveFeed<BookItem> {
  return useLiveSource<BookItem>('book', 'list_books', {}, (raw) => toBookItem(raw), []);
}

export function useLiveMedia(category = '', query = ''): LiveFeed<MediaItem> {
  return useLiveSource<MediaItem>('media', 'fetch_items', { category, query, page: 1 }, (raw) => toMediaItem(raw), [
    category,
    query,
  ]);
}

export interface LiveArticleDetail {
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

  return { blocks, comments, loading, error };
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
    setLoading(true);
    setError(null);
    (async () => {
      const res = await invokeDomain<Dict>({
        domain: 'book',
        sourceId,
        method: 'fetch_spread',
        payload: { book_id: bookId, page_number: page },
        timeoutMs: 30000,
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
    (async () => {
      const res = await callCapability<{ cards?: unknown[] }>({
        capability: 'vocab.sm2.v1',
        payload: { action: 'list' },
      });
      if (cancelled) return;
      if (!res.available) {
        setCards(fallback);
        setLive(false);
        return;
      }
      if (!res.ok) {
        setError(res.error.message);
        setCards(fallback);
        setLive(false);
        return;
      }
      const mapped = arr(res.data.cards)
        .map(toVocabCard)
        .filter((c): c is VocabCard => c !== null);
      setCards(mapped.length > 0 ? mapped : fallback);
      setLive(true);
    })().catch(() => {
      if (!cancelled) setCards(fallback);
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
