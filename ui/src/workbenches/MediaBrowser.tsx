import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Play, UserRound, RefreshCw, Music2 } from 'lucide-react';
import { callCapability } from '../api';
import { MediaCover } from '../components/MediaCover';
import { toMediaItem } from '../hooks/useLiveDomain';
import type { MediaItem } from '../data/mock';
import { AdamAccount } from './AdamAccount';

type User = { id: string; name: string; cover_url?: string; summary?: string };
type Dynamic = { id: string; name: string; mid: string; avatar?: string; published: string; text: string; images: string[]; video?: unknown };
type BrowseData = { items?: unknown[]; cards?: Dynamic[]; users?: User[]; profile?: User; has_more?: boolean; subscriptions?: { channel_id: string; title: string; description?: string }[] };
type Destination = { id: string; name: string; type: string };
const button = 'min-h-11 rounded-xl px-3 py-2 text-sm hover:bg-[var(--bg-surface)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--domain-media)]';

export function MediaBrowser({ sourceId, category, items, onPlay, footer }: {
  sourceId: string; category: string; items: MediaItem[]; onPlay: (item: MediaItem) => void; footer?: React.ReactNode;
}) {
  const [path, setPath] = useState<Destination[]>([]);
  const [data, setData] = useState<BrowseData>({});
  const [page, setPage] = useState(1);
  const [nonce, setNonce] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const target = path.at(-1);
  const bili = sourceId === 'plugin-bilibili';
  const music = sourceId === 'plugin-music';
  const custom = Boolean(target || (bili && ['dynamic', 'followings'].includes(category)) || (sourceId === 'plugin-youtube' && category === 'channels'));
  const capability = bili ? 'bilibili.media.v1' : music ? 'media.adam.v1' : 'media.youtube.v1';
  useEffect(() => {
    generation.current += 1;
    setPath([]); setPage(1); setData({}); setError('');
  }, [sourceId, category]);
  useEffect(() => {
    if (!custom) { setLoading(false); return; }
    const seq = ++generation.current;
    setLoading(true); setError('');
    const payload = target
      ? bili ? { action: 'space', mid: target.id, page } : music ? { action: 'collection', id: target.id, type: target.type, page } : { action: 'fetch_items', category: 'channels', query: target.id, page }
      : bili ? { action: category === 'dynamic' ? 'dynamics' : 'followings', page } : { action: 'list_subscriptions' };
    void callCapability<BrowseData>({ capability, providerPluginId: sourceId, payload, timeoutMs: 60_000 }).then((res) => {
      if (seq !== generation.current) return;
      if (!res.ok) { setError(res.error.message); return; }
      const next = res.data;
      if (next.subscriptions) next.users = next.subscriptions.map(u => ({ id: u.channel_id, name: u.title, summary: u.description }));
      setData(prev => page === 1 ? next : { ...next, profile: next.profile?.name ? next.profile : prev.profile,
        items: [...(prev.items ?? []), ...(next.items ?? [])], cards: [...(prev.cards ?? []), ...(next.cards ?? [])], users: [...(prev.users ?? []), ...(next.users ?? [])] });
    }).catch(() => { if (seq === generation.current) setError('内容加载失败，请重试'); }).finally(() => { if (seq === generation.current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [sourceId, category, target?.id, target?.type, custom, page, nonce, capability]);
  const navigate = (next: Destination) => { setPath(prev => [...prev, next]); setPage(1); setData({}); };
  const creator = (item: MediaItem) => item.creatorId && navigate({ id: item.creatorId, name: item.creator, type: music ? 'artists' : 'creator' });
  const shown = custom ? (data.items ?? []).map(toMediaItem).filter((item): item is MediaItem => item !== null) : items;
  const open = (item: MediaItem) => item.collection ? navigate({ ...item.collection, name: item.title }) : onPlay(item);
  function card(item: MediaItem) {
    return <article key={item.id} className="group min-w-0">
      <button type="button" onClick={() => open(item)} className="block w-full rounded-xl text-left focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[var(--domain-media)]" aria-label={`${item.collection ? '打开' : '播放'} ${item.title}`}>
        <div className={`relative overflow-hidden rounded-xl ${music ? 'aspect-square' : 'aspect-video'}`} style={{ background: item.thumbGradient }}>
          <MediaCover url={item.coverUrl} />
          <span className="absolute inset-0 flex items-center justify-center bg-black/0 text-white opacity-0 transition-opacity group-hover:bg-black/15 group-hover:opacity-100 group-focus-within:opacity-100 motion-reduce:transition-none"><Play size={32} fill="currentColor" aria-hidden="true" /></span>
          {!music && <span className="absolute bottom-2 right-2 rounded bg-black/75 px-1.5 py-0.5 text-xs text-white">{item.durationLabel}</span>}
        </div>
        <h3 className="mt-3 line-clamp-2 text-[15px] font-semibold leading-6 text-[var(--text-primary)]">{item.title}</h3>
      </button>
      <button type="button" className={`${button} -ml-3 max-w-full truncate text-[var(--text-muted)] disabled:cursor-default`} disabled={!item.creatorId} onClick={() => creator(item)} aria-label={`${music ? '艺人' : 'UP / 频道'}：${item.creator}`}>
        {item.creator || item.subtitle}
      </button>
      {music && <p className="truncate text-xs text-[var(--text-muted)]">{item.collection ? ({ albums: '专辑', playlists: '歌单', 'library-playlists': '资料库歌单', artists: '艺人' }[item.collection.type] ?? '合集') : item.durationLabel}</p>}
    </article>;
  }
  return <section className="min-w-0 p-5 md:p-7" aria-label={music ? 'Apple Music 浏览' : bili ? 'B站浏览' : 'YouTube 浏览'} aria-busy={loading}>
    <div className="mb-5 flex flex-wrap items-center gap-3">
      {target && <button className={button} onClick={() => { setPath(prev => prev.slice(0, -1)); setPage(1); setData({}); }}><ArrowLeft className="mr-2 inline" size={16} aria-hidden="true" />返回</button>}
      <div className="min-w-0 flex-1"><h2 className="text-xl font-semibold">{target?.name || (music ? '你的音乐空间' : category === 'dynamic' ? '关注动态' : category === 'followings' ? '我关注的 UP 主' : bili ? '发现好视频' : '发现与订阅')}</h2><p className="mt-1 text-sm text-[var(--text-muted)]">{target ? data.profile?.summary || '浏览作品与最新内容' : music ? '专辑、歌单与歌曲 · 点击封面开始聆听' : '点击封面播放 · 点击作者进入主页'}</p></div>
      {music && <AdamAccount />}
      {custom && <button aria-label="刷新当前页面" className={button} disabled={loading} onClick={() => { setPage(1); setNonce(n => n + 1); }}><RefreshCw size={18} aria-hidden="true" /></button>}
    </div>
    {error && <div role="alert" className="mb-5 rounded-xl border border-[var(--border-line)] p-4"><p>{error}</p><button className={button} onClick={() => setNonce(n => n + 1)}>重试</button></div>}
    {loading && page === 1 && <p role="status" className="py-8 text-[var(--text-muted)]">正在加载内容…</p>}
    {!!data.users?.length && custom && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">{data.users.map(u => <button key={u.id} onClick={() => navigate({ id: u.id, name: u.name, type: 'creator' })} className={`${button} flex items-center gap-4 border border-[var(--border-line)] p-4 text-left`}>
      <div className="relative flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-full bg-[var(--bg-surface)]"><UserRound aria-hidden="true" /><MediaCover url={u.cover_url} /></div><div className="min-w-0"><h3 className="truncate font-semibold">{u.name}</h3><p className="mt-1 line-clamp-2 text-xs text-[var(--text-muted)]">{u.summary || '查看主页与投稿'}</p></div>
    </button>)}</div>}
    {!!data.cards?.length && custom && <div className="mx-auto max-w-3xl space-y-5">{data.cards.map(d => <article key={d.id} className="rounded-2xl border border-[var(--border-line)] p-5">
      <button onClick={() => navigate({ id: d.mid, name: d.name, type: 'creator' })} className={`${button} -ml-3 flex items-center gap-3`}><div className="relative h-10 w-10 overflow-hidden rounded-full bg-[var(--bg-surface)]"><MediaCover url={d.avatar} /></div><span className="text-left"><strong className="block">{d.name}</strong><span className="text-xs text-[var(--text-muted)]">{d.published}</span></span></button>
      {d.text && <p className="my-3 whitespace-pre-wrap break-words text-sm leading-7">{d.text}</p>}
      {!!d.images.length && <div className="my-3 grid grid-cols-2 gap-2 sm:grid-cols-3">{d.images.map((url, i) => <div key={`${url}:${i}`} className="relative aspect-square overflow-hidden rounded-lg bg-[var(--bg-surface)]"><MediaCover url={url} /></div>)}</div>}
      {Boolean(d.video) && (() => { const video = toMediaItem(d.video); return video ? card(video) : null; })()}
    </article>)}</div>}
    {!!shown.length && <div className={`grid grid-cols-1 gap-x-5 gap-y-6 ${music ? 'min-[480px]:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-5' : 'sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4'}`}>{shown.map(card)}</div>}
    {!loading && !error && !shown.length && !(custom && (data.cards?.length || data.users?.length)) && <div className="py-16 text-center text-[var(--text-muted)]"><Music2 className="mx-auto mb-3" aria-hidden="true" /><p>当前分类暂无内容</p><p className="mt-2 text-sm">可以切换分类、搜索，或检查账号登录状态。</p></div>}
    {custom ? (data.has_more || (!music && !bili && target && shown.length > 0)) && <button className={`${button} mt-6 w-full border border-[var(--border-line)]`} disabled={loading} onClick={() => setPage(p => p + 1)}>{loading ? '加载中…' : '加载更多'}</button> : footer}
  </section>;
}
