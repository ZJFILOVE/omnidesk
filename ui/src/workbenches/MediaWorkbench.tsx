import React, { useEffect, useMemo, useRef, useState } from 'react';
import ContentCard from '../components/ContentCard';
import WorkbenchLayout, {
  FocusButton,
  HAIRLINE_BORDER,
  SectionLabel,
  focusClass,
  focusableInputProps,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import { MEDIA_ITEMS, type ChapterSegment, type MediaItem } from '../data/mock';
import MediaInspector from './panels/MediaInspector';

type MediaFilterId =
  | 'all'
  | 'video'
  | 'music'
  | 'nas'
  | 'daily_immutable'
  | 'interactive_ephemeral';

const FILTER_TABS: { id: MediaFilterId; label: string; hint: string }[] = [
  { id: 'all', label: '全部媒体', hint: 'Video · Music · NAS' },
  { id: 'video', label: '视频精选', hint: 'YouTube & B站' },
  { id: 'music', label: 'Apple Music', hint: '每日 5 首 & 双语词' },
  { id: 'nas', label: '飞牛 NAS 原盘', hint: '4K DoVi & 台词切片' },
  { id: 'daily_immutable', label: '每日账本', hint: 'daily_immutable' },
  { id: 'interactive_ephemeral', label: '灵感重温', hint: 'interactive_ephemeral' },
];

export const MediaWorkbench: React.FC<WorkbenchViewProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  searchTrigger,
  onContextChange,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    'media:card:media-yt-attention',
    focusId,
    onFocus,
    focusedItemId,
  );
  const [activeFilter, setActiveFilter] = useState<MediaFilterId>('all');
  const [query, setQuery] = useState<string>('');
  const [selectedId, setSelectedId] = useState<string>(MEDIA_ITEMS[0]?.id ?? '');
  const [playing, setPlaying] = useState<boolean>(false);
  const [currentSec, setCurrentSec] = useState<number>(64);
  const [ephemeralQueue, setEphemeralQueue] = useState<string[]>(['media-music-daily-2']);
  const [actionStatus, setActionStatus] = useState<string>('libmpv hwdec=auto-safe 就绪');
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (searchTrigger && searchTrigger > 0) {
      searchRef.current?.focus();
    }
  }, [searchTrigger]);

  const filteredItems = useMemo(() => {
    const q = query.trim().toLowerCase();
    return MEDIA_ITEMS.filter((item) => {
      if (activeFilter === 'video' && item.kind !== 'video') return false;
      if (activeFilter === 'music' && item.kind !== 'music') return false;
      if (activeFilter === 'nas' && item.kind !== 'nas') return false;
      if (activeFilter === 'daily_immutable' && item.curationMode !== 'daily_immutable') {
        return false;
      }
      if (
        activeFilter === 'interactive_ephemeral' &&
        item.curationMode !== 'interactive_ephemeral'
      ) {
        return false;
      }
      if (!q) return true;
      return (
        item.title.toLowerCase().includes(q) ||
        item.subtitle.toLowerCase().includes(q) ||
        item.creator.toLowerCase().includes(q) ||
        item.summary.toLowerCase().includes(q)
      );
    });
  }, [activeFilter, query]);

  const selectedItem: MediaItem | undefined = useMemo(
    () =>
      MEDIA_ITEMS.find((item) => item.id === selectedId) ??
      filteredItems[0] ??
      MEDIA_ITEMS[0],
    [selectedId, filteredItems],
  );

  useEffect(() => {
    if (!onContextChange) return;
    if (!selectedItem) {
      onContextChange(null);
      return;
    }
    onContextChange({
      domain: 'media',
      workbench: 'media',
      itemId: selectedItem.id,
      title: selectedItem.title,
      subtitle: selectedItem.subtitle,
      sourceId: selectedItem.sourceId,
      kind: selectedItem.kind,
      url: selectedItem.playUrl,
    });
  }, [onContextChange, selectedItem]);

  useEffect(() => {
    if (!playing || !selectedItem) return undefined;
    const timer = window.setInterval(() => {
      setCurrentSec((prev) => (prev + 1 >= selectedItem.durationSec ? 0 : prev + 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [playing, selectedItem]);

  const activeLyricIndex = useMemo(() => {
    if (!selectedItem?.lyrics?.length) return -1;
    let idx = 0;
    for (let i = 0; i < selectedItem.lyrics.length; i += 1) {
      if (selectedItem.lyrics[i].timeSec <= currentSec) idx = i;
    }
    return idx;
  }, [selectedItem, currentSec]);

  const nextSponsorSegment = useMemo(() => {
    if (!selectedItem?.chapters?.length) return null;
    return (
      selectedItem.chapters.find(
        (ch) => (ch.kind === 'sponsor' || ch.kind === 'ad') && ch.endSec > currentSec,
      ) ?? null
    );
  }, [selectedItem, currentSec]);

  const progressPct = selectedItem
    ? Math.min(100, Math.round((currentSec / Math.max(1, selectedItem.durationSec)) * 100))
    : 0;

  const handleSelectMedia = (item: MediaItem) => {
    setSelectedId(item.id);
    setCurrentSec(0);
    setPlaying(true);
    setActionStatus(`已挂载至 libmpv 视口：${item.title}`);
  };

  const handleSkipSponsor = (seg: ChapterSegment) => {
    setCurrentSec(seg.endSec);
    setActionStatus(`SponsorBlock 已跳过：${seg.label}`);
  };

  const handleQueueEphemeral = (id: string) => {
    setEphemeralQueue((prev) => (prev.includes(id) ? prev : [...prev, id]));
    setActionStatus('已加入「OmniDesk AI 灵感电台」（未触碰每日歌单）');
  };

  return (
    <WorkbenchLayout
      domain="media"
      title="影视与无损音乐工作台 · MediaWorkbench"
      subtitle="YouTube / B站硬解、Apple Music 国区双语歌词与飞牛 fnOS 原盘"
      badge="Media · Ctrl+1"
      headerActions={
        <div className="flex items-center gap-2">
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="检索片名、艺人、YouTube / NAS..."
            aria-label="搜索媒体条目"
            data-focused={activeFocusId === 'media:search' ? 'true' : undefined}
            {...focusableInputProps('media:search', activeFocusId, emitFocus)}
            className={focusClass(
              activeFocusId === 'media:search',
              'w-56 rounded-md border px-2.5 py-1 text-xs transition-colors',
            )}
            style={{
              backgroundColor: 'var(--bg-canvas, #F6F5F3)',
              borderColor: HAIRLINE_BORDER,
              color: 'var(--text-primary, #222120)',
            }}
          />
          <span
            className="rounded border px-2 py-1 font-mono text-[11px]"
            style={{
              borderColor: HAIRLINE_BORDER,
              backgroundColor: 'var(--bg-soft, #F1EFEC)',
              color: 'var(--text-muted, #66625D)',
            }}
          >
            灵感电台队列：{ephemeralQueue.length}
          </span>
        </div>
      }
      leftPane={
        <div className="flex flex-col gap-4 p-4">
          <div>
            <SectionLabel className="mb-2">媒体源与策展通道</SectionLabel>
            <div className="flex flex-col gap-1">
              {FILTER_TABS.map((tab) => (
                <FocusButton
                  key={tab.id}
                  focusId={`media:filter:${tab.id}`}
                  activeFocusId={activeFocusId}
                  onFocus={emitFocus}
                  onActivate={() => setActiveFilter(tab.id)}
                  selected={activeFilter === tab.id}
                  tone="media"
                  className="w-full px-3 py-2 text-left"
                >
                  <div className="text-xs font-semibold">{tab.label}</div>
                  <div className="font-mono text-[10px]" style={{ color: 'var(--text-muted, #66625D)' }}>
                    {tab.hint}
                  </div>
                </FocusButton>
              ))}
            </div>
          </div>

          <div
            className="rounded-lg border p-3 text-xs"
            style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
          >
            <div className="mb-1 font-semibold">歌单反写隔离门禁</div>
            <p className="leading-relaxed" style={{ color: 'var(--text-muted, #66625D)' }}>
              <span className="font-mono">p.YJXV7dEIeb3GzZX</span>{' '}
              仅接受每日不重复精选同步；即时点播与重温仅写入「OmniDesk AI 灵感电台」。
            </p>
          </div>
        </div>
      }
      mainPane={
        <div className="p-5">
          <div className="mb-3 flex items-center justify-between">
            <span className="text-xs font-medium" style={{ color: 'var(--text-muted, #66625D)' }}>
              共 {filteredItems.length} 项媒体内容 · 16:9 卡片视图
            </span>
            <span className="font-mono text-[11px]" style={{ color: 'var(--text-muted, #66625D)' }}>
              {actionStatus}
            </span>
          </div>

          {filteredItems.length === 0 ? (
            <div
              className="rounded-lg border p-8 text-center text-sm"
              style={{
                backgroundColor: 'var(--bg-surface, #FCFBF9)',
                borderColor: HAIRLINE_BORDER,
                color: 'var(--text-muted, #66625D)',
              }}
            >
              没有匹配的媒体条目，尝试清空搜索或切换左侧媒体源。
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {filteredItems.map((item) => {
                const isSelected = selectedItem?.id === item.id;
                const isEphemeral = item.curationMode === 'interactive_ephemeral';
                const isDaily = item.curationMode === 'daily_immutable';
                const inQueue = ephemeralQueue.includes(item.id);
                return (
                  <ContentCard
                    key={item.id}
                    focusId={`media:card:${item.id}`}
                    activeFocusId={activeFocusId}
                    selected={isSelected}
                    onFocus={emitFocus}
                    onActivate={() => handleSelectMedia(item)}
                    aspect="16:9"
                    coverGradient={item.thumbGradient}
                    coverTag={item.thumbTag}
                    meta={item.durationLabel}
                    progressPct={isSelected ? progressPct : undefined}
                    coverOverlay={
                      <div className="flex items-center justify-between gap-2 text-[10px]">
                        <span className="rounded bg-black/55 px-1.5 py-0.5 font-mono">
                          {item.kind === 'music'
                            ? 'ALAC · 1:1 封面舞台'
                            : item.kind === 'nas'
                              ? item.nasMeta?.resolution ?? '4K HDR'
                              : '16:9 硬解流'}
                        </span>
                        {isSelected ? (
                          <span className="rounded bg-white/90 px-1.5 py-0.5 font-mono font-semibold text-black">
                            {playing ? 'PLAYING' : 'PAUSED'}
                          </span>
                        ) : null}
                      </div>
                    }
                    badge={item.badge}
                    badgeTone={isDaily ? 'ok' : isEphemeral ? 'core' : 'media'}
                    secondaryBadge={
                      item.previouslyRecommendedOn
                        ? `经典重温 · ${item.previouslyRecommendedOn}`
                        : inQueue
                          ? '已在灵感电台'
                          : undefined
                    }
                    title={item.title}
                    subtitle={item.subtitle}
                    description={item.summary}
                    footer={
                      <>
                        <span className="truncate font-mono">{item.creator}</span>
                        <span className="truncate font-mono text-[10px]">
                          {item.verificationLabel ?? item.sourceId}
                        </span>
                      </>
                    }
                  />
                );
              })}
            </div>
          )}
        </div>
      }
      rightPane={
        selectedItem ? (
          <MediaInspector
            item={selectedItem}
            playing={playing}
            currentSec={currentSec}
            activeLyricIndex={activeLyricIndex}
            nextSponsorSegment={nextSponsorSegment}
            inEphemeralQueue={ephemeralQueue.includes(selectedItem.id)}
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onTogglePlay={() => setPlaying((p) => !p)}
            onSeek={(sec) => {
              setCurrentSec(sec);
              setPlaying(true);
            }}
            onSkipSponsor={handleSkipSponsor}
            onQueueEphemeral={handleQueueEphemeral}
            onStatusChange={setActionStatus}
          />
        ) : null
      }
      footer={
        <>
          <span>焦点锚点: {activeFocusId || '未锁定'}</span>
          <span>Space/Enter 激活 · SponsorBlock · libmpv 2.5</span>
        </>
      }
    />
  );
};

export default MediaWorkbench;
