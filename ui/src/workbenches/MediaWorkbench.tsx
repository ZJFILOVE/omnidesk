import React, { useEffect, useMemo, useRef, useState } from 'react';
import { callCapability, isTauriAvailable, mpvEmbedSetActive } from '../api';
import MpvCanvas from '../components/MpvCanvas';
import StateView from '../components/StateView';
import WorkbenchLayout, {
  HAIRLINE_BORDER,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import {
  PanelSectionHeading,
  SearchInput,
  SegmentedPills,
  SelectableRow,
  TagChip,
} from '../components/WorkbenchPrimitives';
import { MEDIA_ITEMS, type ChapterSegment, type MediaItem } from '../data/mock';
import { useLiveMedia } from '../hooks/useLiveDomain';
import { enqueueEphemeralTrack, runMediaInspectorCapability } from './mediaActions';
import { MediaSourceToolbar } from './MediaSourceToolbar';
import MediaInspector from './panels/MediaInspector';
import { useMediaLyrics } from './useMediaLyrics';
import { useMpvPlayback, type MpvPlaybackController } from './useMpvPlayback';

type MediaFilterId =
  | 'all'
  | 'video'
  | 'music'
  | 'nas'
  | 'daily_immutable'
  | 'interactive_ephemeral';

const FILTER_OPTIONS: { id: MediaFilterId; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'video', label: '视频精选' },
  { id: 'music', label: 'Apple Music' },
  { id: 'nas', label: '飞牛 NAS' },
  { id: 'daily_immutable', label: '每日账本' },
  { id: 'interactive_ephemeral', label: '灵感重温' },
];

export const MediaWorkbench: React.FC<WorkbenchViewProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  searchTrigger,
  mediaToggleTrigger,
  runtimeStatus,
  mpvController,
  isActive = true,
  onContextChange,
  onOpenPluginSettings,
}: WorkbenchViewProps & { mpvController?: MpvPlaybackController }) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    'media:card:media-yt-attention',
    focusId,
    onFocus,
    focusedItemId,
  );
  const [activeFilter, setActiveFilter] = useState<MediaFilterId>('all');
  const [activeSourceId, setActiveSourceId] = useState<string>('');
  const [activeCategory, setActiveCategory] = useState<string>('');
  const [query, setQuery] = useState<string>('');
  const [backendQuery, setBackendQuery] = useState<string>('');
  const [quality, setQuality] = useState<string>('');
  const [selectedId, setSelectedId] = useState<string>(MEDIA_ITEMS[0]?.id ?? '');
  const liveMedia = useLiveMedia(activeCategory, backendQuery, activeSourceId);
  const categoryOptions = liveMedia.categories;
  const effectiveCategory =
    activeCategory && categoryOptions.includes(activeCategory)
      ? activeCategory
      : (categoryOptions[0] ?? activeCategory);
  const mediaItems = useMemo(
    () => (liveMedia.live && liveMedia.items.length > 0 ? liveMedia.items : MEDIA_ITEMS),
    [liveMedia.items, liveMedia.live],
  );
  const [previewPlaying, setPreviewPlaying] = useState<boolean>(false);
  const [previewSec, setPreviewSec] = useState<number>(64);
  const [previewVolume, setPreviewVolume] = useState<number>(85);
  const [volumeDraft, setVolumeDraft] = useState<number | null>(null);
  const [ephemeralQueue, setEphemeralQueue] = useState<string[]>(['media-music-daily-2']);
  const [actionStatus, setActionStatus] = useState<string>(() =>
    isTauriAvailable()
      ? (runtimeStatus?.mpv?.detail ?? 'libmpv 硬解管线就绪')
      : '离线预览：示例媒体与模拟时间轴',
  );
  const localMpv = useMpvPlayback({
    enabled: !mpvController && isTauriAvailable(),
  });
  const mpv = mpvController ?? localMpv;
  const searchRef = useRef<HTMLInputElement | null>(null);
  const lastToggleRef = useRef<number>(mediaToggleTrigger ?? 0);
  const mountedRef = useRef(true);
  const volumeTimerRef = useRef<number | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (volumeTimerRef.current !== null) {
        window.clearTimeout(volumeTimerRef.current);
        volumeTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (runtimeStatus?.mpv?.detail) {
      setActionStatus(runtimeStatus.mpv.detail);
    }
  }, [runtimeStatus?.mpv?.detail]);

  useEffect(() => {
    if (searchTrigger && searchTrigger > 0) {
      searchRef.current?.focus();
    }
  }, [searchTrigger]);

  useEffect(() => {
    if (liveMedia.sources.length === 0) return;
    const chosenSourceId = activeSourceId || liveMedia.sourceId;
    if (!activeSourceId && chosenSourceId) {
      setActiveSourceId(chosenSourceId);
      return;
    }
    const source = liveMedia.sources.find((entry) => entry.sourceId === chosenSourceId);
    const nextCategories = source?.categories ?? [];
    if (nextCategories.length === 0) {
      if (activeCategory) setActiveCategory('');
      return;
    }
    if (!activeCategory || !nextCategories.includes(activeCategory)) {
      setActiveCategory(nextCategories[0]);
    }
  }, [activeCategory, activeSourceId, liveMedia.sourceId, liveMedia.sources]);

  useEffect(() => {
    const trimmed = query.trim();
    if (effectiveCategory !== 'search') {
      setBackendQuery('');
      return undefined;
    }
    const timer = window.setTimeout(() => setBackendQuery(trimmed), 350);
    return () => window.clearTimeout(timer);
  }, [effectiveCategory, query]);

  useEffect(() => {
    if (mediaToggleTrigger !== undefined && mediaToggleTrigger !== lastToggleRef.current) {
      lastToggleRef.current = mediaToggleTrigger;
      void handleTogglePlay();
    }
  }, [mediaToggleTrigger]);

  useEffect(() => {
    if (!mpv.enabled) return;
    const activeItemId = mpv.state?.itemId;
    if (!activeItemId || activeItemId === selectedId) return;
    if (mediaItems.some((item) => item.id === activeItemId)) {
      setSelectedId(activeItemId);
    }
  }, [mediaItems, mpv.enabled, mpv.state?.itemId, selectedId]);

  useEffect(() => {
    if (mpv.state?.lastError) {
      setActionStatus(`mpv 错误：${mpv.state.lastError}`);
    }
  }, [mpv.state?.lastError]);

  useEffect(() => {
    if (mpv.embedded) {
      setActionStatus(
        mpv.embedDetail ? `libmpv 内嵌画布就绪 · ${mpv.embedDetail}` : 'libmpv 内嵌画布就绪',
      );
    } else if (mpv.embedDetail) {
      setActionStatus(mpv.embedDetail);
    }
  }, [mpv.embedDetail, mpv.embedded]);

  useEffect(() => {
    if (mpv.state?.volume === undefined) return;
    setVolumeDraft(null);
  }, [mpv.state?.volume]);

  const filteredItems = useMemo(() => {
    const q = query.trim().toLowerCase();
    return mediaItems.filter((item) => {
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
  }, [activeFilter, mediaItems, query]);

  useEffect(() => {
    mpv.setQueue(filteredItems);
  }, [filteredItems, mpv.setQueue]);

  const selectedItem: MediaItem | undefined = useMemo(
    () =>
      mediaItems.find((item) => item.id === selectedId) ??
      filteredItems[0] ??
      mediaItems[0],
    [mediaItems, selectedId, filteredItems],
  );
  const qualityOptions = selectedItem?.streamResolver?.qualityOptions ?? [];
  const qualityKey = qualityOptions.join('|');
  useEffect(() => {
    if (qualityOptions.length === 0) {
      if (quality) setQuality('');
      return;
    }
    setQuality((previous) => {
      if (qualityOptions.includes(previous)) return previous;
      const reported = selectedItem?.qualityLabel?.toLowerCase() ?? '';
      return qualityOptions.includes(reported) ? reported : '';
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qualityKey, selectedItem?.id, selectedItem?.qualityLabel]);

  const activeSource = useMemo(
    () =>
      liveMedia.sources.find(
        (entry) => entry.sourceId === (activeSourceId || liveMedia.sourceId),
      ),
    [activeSourceId, liveMedia.sourceId, liveMedia.sources],
  );
  const settingsPluginId =
    activeSource?.pluginId ||
    (selectedItem?.sourceId.startsWith('plugin-') ? selectedItem.sourceId : '');
  const settingsLabel =
    settingsPluginId === 'plugin-bilibili'
      ? 'B站登录/插件设置'
      : settingsPluginId === 'plugin-youtube'
        ? 'YouTube 登录/设置'
        : '';
  const mediaLyrics = useMediaLyrics(selectedItem);
  const displayItem = useMemo(
    () =>
      selectedItem && mediaLyrics.lines.length > 0
        ? { ...selectedItem, lyrics: mediaLyrics.lines }
        : selectedItem,
    [mediaLyrics.lines, selectedItem],
  );

  const currentSec = mpv.enabled ? mpv.positionSec : previewSec;
  const effectiveDuration =
    mpv.enabled && mpv.durationSec ? mpv.durationSec : (selectedItem?.durationSec ?? 0);
  const playing = mpv.enabled ? mpv.playing : previewPlaying;
  const volume = volumeDraft ?? (mpv.enabled ? mpv.volume : previewVolume);
  const speed = mpv.enabled ? mpv.speed : 1;

  useEffect(() => {
    if (!mpv.embedded) return undefined;
    const shouldRender = Boolean(isActive && selectedItem?.kind === 'video');
    void mpvEmbedSetActive(shouldRender);
    return () => {
      if (shouldRender) {
        void mpvEmbedSetActive(false);
      }
    };
  }, [isActive, mpv.embedded, selectedItem?.kind]);

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
    if (mpv.enabled || !previewPlaying || !selectedItem) return undefined;
    const timer = window.setInterval(() => {
      setPreviewSec((prev) => (prev + 1 >= selectedItem.durationSec ? 0 : prev + 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [mpv.enabled, previewPlaying, selectedItem]);

  const activeLyricIndex = useMemo(() => {
    if (!displayItem?.lyrics?.length) return -1;
    let idx = 0;
    for (let i = 0; i < displayItem.lyrics.length; i += 1) {
      if (displayItem.lyrics[i].timeSec <= currentSec) idx = i;
    }
    return idx;
  }, [displayItem, currentSec]);

  const nextSponsorSegment = useMemo(() => {
    if (!selectedItem?.chapters?.length) return null;
    return (
      selectedItem.chapters.find(
        (ch) => (ch.kind === 'sponsor' || ch.kind === 'ad') && ch.endSec > currentSec,
      ) ?? null
    );
  }, [selectedItem, currentSec]);

  const handleSourceChange = (sourceId: string) => {
    setActiveSourceId(sourceId);
    setActiveCategory('');
    setActiveFilter('all');
    setSelectedId('');
    setQuality('');
  };

  const handleCategoryChange = (category: string) => {
    setActiveCategory(category);
    setSelectedId('');
  };

  const handleQualityChange = async (nextQuality: string) => {
    setQuality(nextQuality);
    if (!selectedItem) return;
    const resolver = selectedItem.streamResolver;
    if (nextQuality && resolver?.settingsAction && isTauriAvailable()) {
      void callCapability({
        capability: resolver.capability,
        providerPluginId: selectedItem.sourceId,
        payload: { action: resolver.settingsAction, quality: nextQuality },
        timeoutMs: 15_000,
      });
    }
    if (!mpv.enabled) {
      setActionStatus(`已选择画质：${nextQuality || '插件默认'}`);
      return;
    }
    setActionStatus(`正在以 ${nextQuality || '插件默认'} 画质重新取流…`);
    const result = await mpv.open(selectedItem, {
      quality: nextQuality,
      paused: !playing,
      startSeconds: currentSec > 0 ? currentSec : undefined,
    });
    if (mountedRef.current) {
      setActionStatus(result.message);
    }
  };

  const handleSelectMedia = async (item: MediaItem) => {
    setSelectedId(item.id);
    if (!mpv.enabled) {
      setPreviewSec(0);
      setPreviewPlaying(true);
      setActionStatus(`离线预览播放：${item.title}（示例模拟时间轴）`);
      return;
    }
    const requestedQuality =
      quality && item.streamResolver?.qualityOptions.includes(quality)
        ? quality
        : undefined;
    const result = await mpv.open(item, {
      paused: false,
      quality: requestedQuality ?? '',
    });
    if (mountedRef.current) {
      setActionStatus(result.message);
    }
  };

  async function seekTo(seconds: number, successStatus?: string) {
    if (!selectedItem) return;
    const total = effectiveDuration > 0 ? effectiveDuration : seconds;
    const target = Math.max(0, total > 0 ? Math.min(seconds, total) : seconds);
    if (!mpv.enabled) {
      setPreviewSec(target);
      setPreviewPlaying(true);
      setActionStatus(successStatus ?? `离线预览跳转至 ${Math.floor(target)}s`);
      return;
    }
    const result = await mpv.seek(selectedItem, target);
    if (mountedRef.current) {
      setActionStatus(result.ok && successStatus ? successStatus : result.message);
    }
  }

  async function handleTogglePlay() {
    if (!selectedItem) return;
    if (!mpv.enabled) {
      setPreviewPlaying((prev) => {
        const next = !prev;
        setActionStatus(next ? '离线预览播放中（Space 控制）' : '离线预览已暂停（Space 控制）');
        return next;
      });
      return;
    }
    const result = await mpv.toggle(selectedItem);
    if (mountedRef.current) {
      setActionStatus(result.message);
    }
  }

  const handleSeek = (seconds: number) => {
    void seekTo(seconds);
  };

  const handleSkipSponsor = (seg: ChapterSegment) => {
    void seekTo(seg.endSec, `SponsorBlock 已跳过：${seg.label}`);
  };

  const handleVolumeChange = (nextVolume: number) => {
    setVolumeDraft(nextVolume);
    if (!mpv.enabled) {
      setPreviewVolume(nextVolume);
      setActionStatus(`离线预览音量 ${nextVolume}%`);
      return;
    }
    if (volumeTimerRef.current !== null) {
      window.clearTimeout(volumeTimerRef.current);
    }
    volumeTimerRef.current = window.setTimeout(() => {
      volumeTimerRef.current = null;
      void mpv.setVolume(nextVolume).then((result) => {
        if (!mountedRef.current) return;
        if (!result.ok) setVolumeDraft(null);
        setActionStatus(result.message);
      });
    }, 120);
  };

  const handleSpeedChange = (nextSpeed: number) => {
    if (!mpv.enabled) {
      setActionStatus('离线预览：倍速仅对真实 mpv 播放生效');
      return;
    }
    void mpv.setSpeed(nextSpeed).then((result) => {
      if (mountedRef.current) setActionStatus(result.message);
    });
  };

  const handleQueueEphemeral = async (id: string) => {
    const result = await enqueueEphemeralTrack(selectedItem);
    if (!mountedRef.current) return;
    if (result.ok) {
      setEphemeralQueue((prev) => (prev.includes(id) ? prev : [...prev, id]));
    }
    setActionStatus(result.message);
  };

  const handleInspectorAction = async (statusMsg: string) => {
    const msg = await runMediaInspectorCapability(statusMsg);
    if (mountedRef.current) {
      setActionStatus(msg);
    }
  };

  return (
    <WorkbenchLayout
      domain="media"
      testId="workbench-media"
      title="影视与音乐"
      subtitle="16:9 精播舞台 · Apple Music 双语歌词 · 飞牛 NAS 原盘"
      headerActions={
        <div className="flex flex-wrap items-center gap-2.5">
          <SearchInput
            focusId="media:search"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            value={query}
            onChange={setQuery}
            placeholder="检索片名、艺人、来源..."
            ariaLabel="搜索媒体条目"
            inputRef={searchRef}
            widthClass="w-56"
          />
          <TagChip tone="neutral">灵感电台 · {ephemeralQueue.length}</TagChip>
          {mpv.embedded ? <TagChip tone="core">LIBMPV 内嵌</TagChip> : null}
        </div>
      }
      toolbar={
        <div className="flex w-full flex-col gap-2.5">
          <MediaSourceToolbar
            sources={liveMedia.sources}
            sourceId={activeSourceId || liveMedia.sourceId}
            onSourceChange={handleSourceChange}
            categories={categoryOptions}
            category={effectiveCategory}
            onCategoryChange={handleCategoryChange}
            qualityOptions={qualityOptions}
            quality={quality}
            onQualityChange={(next) => {
              void handleQualityChange(next);
            }}
            settingsLabel={settingsLabel || undefined}
            onOpenSettings={
              settingsLabel && onOpenPluginSettings
                ? () => onOpenPluginSettings(settingsPluginId)
                : undefined
            }
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
          />
          <div className="flex w-full flex-wrap items-center justify-between gap-2.5">
            <SegmentedPills
              options={FILTER_OPTIONS}
              value={activeFilter}
              onChange={setActiveFilter}
              focusPrefix="media:filter:"
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              tone="media"
              ariaLabel="媒体源筛选"
            />
            <span className="text-[14px] text-[var(--text-muted)]">
              {liveMedia.live ? (
                <span className="mr-2 font-mono text-[13.5px] text-[var(--accent-ok)]">
                  LIVE ·{' '}
                  {liveMedia.sources.find((s) => s.sourceId === liveMedia.sourceId)?.title ??
                    liveMedia.sourceId}
                </span>
              ) : null}
              {actionStatus}
            </span>
          </div>
        </div>
      }
      mainPane={
        <div className="flex flex-col gap-6 p-6 @[860px]/workbench:flex-row">
          {/* Primary Cinema / Studio Stage */}
          <div className="min-w-0 flex-1">
            {displayItem ? (
              <MediaInspector
                item={displayItem}
                playing={playing}
                currentSec={currentSec}
                durationSec={effectiveDuration}
                volume={volume}
                speed={speed}
                playbackBackend={mpv.embedded ? 'libmpv' : mpv.enabled ? 'mpv' : 'preview'}
                videoSurface={
                  selectedItem.kind === 'video' && mpv.embedded ? (
                    <MpvCanvas active={mpv.embedded && isActive} />
                  ) : undefined
                }
                activeLyricIndex={activeLyricIndex}
                lyricsLoading={mediaLyrics.loading}
                lyricsMessage={mediaLyrics.message}
                nextSponsorSegment={nextSponsorSegment}
                inEphemeralQueue={ephemeralQueue.includes(selectedItem.id)}
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                onTogglePlay={() => void handleTogglePlay()}
                onSeek={handleSeek}
                onVolumeChange={handleVolumeChange}
                onSpeedChange={handleSpeedChange}
                onSkipSponsor={handleSkipSponsor}
                onQueueEphemeral={(id) => void handleQueueEphemeral(id)}
                onStatusChange={(msg) => void handleInspectorAction(msg)}
              />
            ) : (
              <StateView mode="empty" title="未选择播放条目" description="从右侧队列中选择一项以开始播放。" />
            )}
          </div>

          {/* Side Playback Queue */}
          <aside
            aria-label="播放队列"
            className="flex shrink-0 flex-col gap-3.5 @[860px]/workbench:w-[316px] @[860px]/workbench:border-l @[860px]/workbench:pl-6"
            style={{ borderColor: HAIRLINE_BORDER }}
          >
            <PanelSectionHeading title="播放队列" meta={`${filteredItems.length} 项`} />

            {filteredItems.length === 0 ? (
              <StateView
                mode="empty"
                title="没有匹配的媒体条目"
                description="尝试清空搜索词或切换媒体源通道。"
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                action={{
                  label: '重置筛选条件',
                  focusId: 'media:empty:reset',
                  onActivate: () => {
                    setQuery('');
                    setActiveFilter('all');
                  },
                }}
              />
            ) : (
              <div className="flex flex-col gap-1.5 px-1.5 py-1">
                {filteredItems.map((item) => {
                  const isSelected = selectedItem?.id === item.id;
                  const inQueue = ephemeralQueue.includes(item.id);
                  const isMusic = item.kind === 'music';

                  return (
                    <SelectableRow
                      key={item.id}
                      focusId={`media:card:${item.id}`}
                      activeFocusId={activeFocusId}
                      selected={isSelected}
                      domain="media"
                      onFocus={emitFocus}
                      onActivate={() => void handleSelectMedia(item)}
                      title={item.title}
                      leading={
                        <div
                          className="relative flex items-end justify-end overflow-hidden rounded-[8px] p-1 text-white"
                          style={{
                            width: isMusic ? '52px' : '84px',
                            height: isMusic ? '52px' : '48px',
                            background: item.thumbGradient,
                            boxShadow: 'var(--shadow-page)',
                          }}
                        >
                          <span className="rounded bg-black/65 px-1.5 py-[1px] font-mono text-[12.5px] leading-tight">
                            {item.durationLabel}
                          </span>
                        </div>
                      }
                      meta={
                        <>
                          <span className="truncate">{item.creator}</span>
                          {item.curationMode === 'daily_immutable' ? (
                            <span style={{ color: 'var(--status-ok-fg)' }}>· 每日精选</span>
                          ) : item.previouslyRecommendedOn ? (
                            <span style={{ color: 'var(--status-warn-fg)' }}>· 重温</span>
                          ) : inQueue ? (
                            <span style={{ color: 'var(--domain-core)' }}>· 灵感电台</span>
                          ) : null}
                        </>
                      }
                    />
                  );
                })}
              </div>
            )}

            <p
              className="mt-2 border-t pt-3 text-[13.5px] leading-[1.68] text-[var(--text-subtle)]"
              style={{ borderColor: HAIRLINE_BORDER }}
            >
              歌单隔离：<span className="font-mono">p.YJXV7dEIeb3GzZX</span> 仅接受每日不重复精选；即时点播与重温仅写入「OmniDesk AI 灵感电台」。
            </p>
          </aside>
        </div>
      }
    />
  );
};

export default MediaWorkbench;
