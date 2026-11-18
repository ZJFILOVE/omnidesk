import React, { useEffect, useRef, useState } from 'react';
import { getTauriGlobal, type MpvPlaybackState } from '../../api';
import { TranslationButton } from '../../components/TranslationContext';
import { MediaCover } from '../../components/MediaCover';
import {
  Disc3,
  Film,
  Maximize2,
  Minimize2,
  Pause,
  Play,
  Radio,
  RotateCcw,
  RotateCw,
  SkipForward,
  Subtitles,
  Volume2,
} from 'lucide-react';
import {
  EditorialCallout,
  PanelSectionHeading,
  TagChip,
} from '../../components/WorkbenchPrimitives';
import {
  FocusButton,
  focusClass,
  focusableInputProps,
} from '../../components/WorkbenchLayout';
import type { ChapterSegment, MediaItem } from '../../data/mock';
import { SubHunterAction } from './SubHunterAction';

export function formatMediaTime(sec: number): string {
  const safe = Math.max(0, Math.floor(sec));
  const m = Math.floor(safe / 60);
  const s = safe % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatSpeed(speed: number): string {
  return `${Number(speed.toFixed(2))}×`;
}

function formatBitrate(bps: number): string {
  if (!Number.isFinite(bps) || bps <= 0) return '—';
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(2)} Mbps`;
  return `${Math.round(bps / 1000)} kbps`;
}

export interface MediaInspectorProps {
  item: MediaItem;
  playing: boolean;
  currentSec: number;
  durationSec?: number;
  volume?: number;
  speed?: number;
  playbackBackend?: 'mpv' | 'libmpv' | 'preview';
  playbackStats?: MpvPlaybackState | null;
  videoSurface?: React.ReactNode;
  activeLyricIndex: number;
  lyricsLoading?: boolean;
  lyricsMessage?: string;
  onReloadLyrics?: () => void;
  nextSponsorSegment: ChapterSegment | null;
  inEphemeralQueue: boolean;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onTogglePlay: () => void;
  onSeek: (sec: number) => void;
  onVolumeChange?: (volume: number) => void;
  onSpeedChange?: (speed: number) => void;
  onSkipSponsor: (seg: ChapterSegment) => void;
  onQueueEphemeral: (id: string) => void;
  onStatusChange: (msg: string) => void;
}

export const MediaInspector: React.FC<MediaInspectorProps> = ({
  item,
  playing,
  currentSec,
  durationSec,
  volume = 85,
  speed = 1,
  playbackBackend = 'preview',
  playbackStats = null,
  videoSurface,
  activeLyricIndex,
  lyricsLoading = false,
  lyricsMessage = '',
  onReloadLyrics,
  nextSponsorSegment,
  inEphemeralQueue,
  activeFocusId,
  onFocus,
  onTogglePlay,
  onSeek,
  onVolumeChange,
  onSpeedChange,
  onSkipSponsor,
  onQueueEphemeral,
  onStatusChange,
}) => {
  const isMusic = item.kind === 'music';
  const lyricsViewport = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const viewport = lyricsViewport.current;
    const line = viewport?.children[activeLyricIndex] as HTMLElement | undefined;
    if (!viewport || !line || lyricsLoading) return;
    // Scroll only the lyric pane; keep playback controls and keyboard focus stable.
    const top = viewport.scrollTop + line.getBoundingClientRect().top - viewport.getBoundingClientRect().top
      - (viewport.clientHeight - line.offsetHeight) / 2;
    viewport.scrollTo?.({ top: Math.max(0, top), behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }, [item.id, item.lyrics, activeLyricIndex, lyricsLoading]);
  const [translations, setTranslations] = useState<Record<string, Record<string, string>>>({});
  const [videoFullscreen, setVideoFullscreen] = useState(false);
  const [showStats, setShowStats] = useState(false);
  const toggleVideoFullscreen = () => {
    setVideoFullscreen((prev) => {
      const next = !prev;
      const invoke = getTauriGlobal()?.core?.invoke;
      if (invoke) void invoke('player_fullscreen').catch(() => {});
      return next;
    });
  };
  useEffect(() => {
    if (isMusic) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing || e.keyCode === 229) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() ?? '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || target?.isContentEditable) {
        return;
      }
      if (videoFullscreen && e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        setVideoFullscreen(false);
        const invoke = getTauriGlobal()?.core?.invoke;
        if (invoke) void invoke('player_fullscreen').catch(() => {});
        return;
      }
      if (
        e.key === 'F3' &&
        e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !e.repeat
      ) {
        e.preventDefault();
        e.stopPropagation();
        setShowStats((prev) => !prev);
        return;
      }
      if (
        e.key.toLowerCase() === 'f' &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey &&
        !e.shiftKey &&
        !e.repeat
      ) {
        if (!videoFullscreen && document.querySelector('[role="dialog"], [aria-modal="true"]')) {
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        toggleVideoFullscreen();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [isMusic, videoFullscreen]);
  const totalSec =
    durationSec && Number.isFinite(durationSec) && durationSec > 0
      ? durationSec
      : item.durationSec;
  const durationLabel =
    Math.abs(totalSec - item.durationSec) < 0.5 ? item.durationLabel : formatMediaTime(totalSec);
  const progressPct = Math.min(100, Math.round((currentSec / Math.max(1, totalSec)) * 100));

  return (
    <div className="flex flex-col gap-5">
      {/* Hero Stage: 16:9 Cinema Frame or 1:1 Studio Album Listening Room */}
      {isMusic ? (
        <div
          className="relative overflow-hidden rounded-[20px] p-6 text-white"
          style={{
            background: item.thumbGradient,
            boxShadow: 'var(--shadow-soft)',
          }}
        >
          <div
            className="pointer-events-none absolute inset-0 opacity-25"
            style={{
              backgroundImage:
                'radial-gradient(circle at 20% 20%, rgba(255,255,255,0.45), transparent 60%)',
            }}
          />
          <div className="relative flex flex-col gap-5 sm:flex-row sm:items-center">
            <div
              className="relative flex h-44 w-44 shrink-0 flex-col justify-between overflow-hidden rounded-[14px] p-4 shadow-xl sm:h-48 sm:w-48"
              style={{
                background: 'rgba(15, 16, 20, 0.38)',
                backdropFilter: 'blur(12px)',
              }}
            >
              <MediaCover url={item.coverUrl} item={item} />
              <div className="relative flex items-center justify-between font-mono text-[13.5px] opacity-85">
                <span>ALAC</span>
                <Disc3 className="h-4 w-4" aria-hidden="true" />
              </div>
              <div>
                <div className="relative bg-black/50 rounded px-1 text-[13.5px]">{item.thumbTag}</div>
                <div className="relative mt-1 line-clamp-2 bg-black/50 rounded px-1 text-[16.5px] leading-snug font-semibold">
                  {item.creator}
                </div>
              </div>
            </div>

            <div className="flex min-w-0 flex-1 flex-col justify-between gap-3.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="rounded-full bg-black/40 px-3 py-0.5 font-mono text-[13.5px] backdrop-blur-xs">
                  {item.verificationLabel ?? item.sourceId}
                </span>
                <div className="flex items-center gap-1.5">
                  {playbackBackend !== 'preview' ? (
                    <span className="rounded-full bg-black/40 px-3 py-0.5 font-mono text-[13.5px] backdrop-blur-xs">
                      {playbackBackend === 'libmpv' ? 'LIBMPV' : 'MPV'}
                    </span>
                  ) : null}
                  <span className="rounded-full bg-white/92 px-3 py-0.5 font-mono text-[13.5px] font-semibold text-black">
                    {playing ? 'PLAYING' : 'PAUSED'}
                  </span>
                </div>
              </div>

              <div>
                <h2 className="text-[26px] leading-tight font-bold tracking-tight">{item.title}</h2>
                <p className="mt-1.5 text-[16px] text-white/85">{item.subtitle}</p>
              </div>

              <div className="flex items-center justify-between font-mono text-[14px] text-white/85">
                <span>{formatMediaTime(currentSec)}</span>
                <span>{durationLabel}</span>
              </div>
            </div>
          </div>

          <div className="absolute right-0 bottom-0 left-0 h-1 bg-black/25">
            <div className="h-full bg-white/90" style={{ width: `${progressPct}%` }} />
          </div>
        </div>
      ) : (
        <div
          onDoubleClick={toggleVideoFullscreen}
          className={
            videoFullscreen
              ? 'fixed inset-0 z-[70] flex flex-col overflow-hidden bg-black text-white'
              : 'relative overflow-hidden rounded-[20px] text-white'
          }
          style={
            videoFullscreen
              ? undefined
              : {
                  aspectRatio: '16 / 9',
                  background: videoSurface ? '#08090b' : item.thumbGradient,
                  boxShadow: 'var(--shadow-soft)',
                }
          }
        >
          {videoSurface ? (
            <div className="absolute inset-0 overflow-hidden bg-black">{videoSurface}</div>
          ) : <MediaCover url={item.coverUrl} item={item} />}
          <div
            className="pointer-events-none absolute inset-0"
            style={{
              background:
                'linear-gradient(180deg, rgba(12,13,16,0.28) 0%, rgba(12,13,16,0.06) 44%, rgba(12,13,16,0.74) 100%)',
            }}
          />
          <div className="relative flex h-full flex-col justify-between p-5 sm:p-6">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-black/45 px-3 py-1 font-mono text-[13.5px] backdrop-blur-xs">
                  <Film className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>{item.thumbTag}</span>
                </span>
                {item.nasMeta ? (
                  <span className="rounded-full bg-black/45 px-3 py-1 font-mono text-[13.5px] backdrop-blur-xs">
                    {item.nasMeta.resolution}
                  </span>
                ) : null}
              </div>

              <div className="flex items-center gap-2">
                <span className="rounded-full bg-black/45 px-3 py-1 font-mono text-[13.5px]">
                  {formatMediaTime(currentSec)} / {durationLabel}
                </span>
                <div className="flex items-center gap-1.5">
                  {playbackBackend !== 'preview' ? (
                    <span className="rounded-full bg-black/45 px-3 py-1 font-mono text-[13.5px] backdrop-blur-xs">
                      {playbackBackend === 'libmpv' ? 'LIBMPV' : 'MPV'}
                    </span>
                  ) : null}
                  <span className="rounded-full bg-white/92 px-3 py-0.5 font-mono text-[13.5px] font-semibold text-black">
                    {playing ? 'PLAYING' : 'PAUSED'}
                  </span>
                  <button
                    type="button"
                    onClick={() => setShowStats((prev) => !prev)}
                    aria-label={showStats ? '隐藏播放统计' : '显示播放统计'}
                    aria-pressed={showStats}
                    title="码率与播放统计 (Ctrl+F3)"
                    className={`inline-flex cursor-pointer items-center gap-1 rounded-full px-2.5 py-1 font-mono text-[12px] text-white transition-colors ${
                      showStats ? 'bg-[var(--domain-media)]' : 'bg-black/55 hover:bg-black/80'
                    }`}
                  >
                    <span>统计 (Ctrl+F3)</span>
                  </button>
                  <button
                    type="button"
                    onClick={toggleVideoFullscreen}
                    aria-label={videoFullscreen ? '退出全屏' : '全屏播放'}
                    title={videoFullscreen ? '退出全屏 (f / Esc)' : '全屏播放 (f)'}
                    className="inline-flex cursor-pointer items-center gap-1 rounded-full bg-black/55 px-2.5 py-1 text-[12.5px] text-white hover:bg-black/80"
                  >
                    {videoFullscreen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
                    <span>{videoFullscreen ? '退出全屏 (f)' : '全屏 (f)'}</span>
                  </button>
                </div>
              </div>
            </div>

            {showStats ? (
              <div
                data-testid="mpv-stats-overlay"
                className="pointer-events-none my-auto max-w-md self-start rounded-xl border border-white/15 bg-black/78 px-4 py-3 font-mono text-[12px] leading-relaxed text-white/95 shadow-xl backdrop-blur-sm"
              >
                <div className="mb-1.5 flex items-center justify-between gap-4 border-b border-white/15 pb-1 text-[11px] tracking-wider text-white/70 uppercase">
                  <span>Playback Stats ({playbackBackend === 'libmpv' ? 'libmpv' : playbackBackend === 'mpv' ? 'mpv' : 'preview'})</span>
                  <span>Ctrl+F3</span>
                </div>
                <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5">
                  <span className="text-white/60">Video</span>
                  <span>
                    {playbackStats?.videoCodec || '—'}
                    {playbackStats && playbackStats.videoWidth > 0 && playbackStats.videoHeight > 0
                      ? ` · ${playbackStats.videoWidth}×${playbackStats.videoHeight}`
                      : ''}
                    {playbackStats && playbackStats.fps > 0 ? ` @ ${playbackStats.fps.toFixed(2)} fps` : ''}
                  </span>
                  <span className="text-white/60">Bitrate</span>
                  <span>{formatBitrate(playbackStats?.videoBitrate ?? 0)}</span>
                  <span className="text-white/60">Hwdec</span>
                  <span>
                    {playbackStats?.hwdecActive && playbackStats.hwdecCurrent
                      ? playbackStats.hwdecCurrent
                      : 'software / no'}
                  </span>
                  <span className="text-white/60">Audio</span>
                  <span>
                    {playbackStats?.audioCodec || '—'}
                    {playbackStats?.audioSamplerate ? ` · ${playbackStats.audioSamplerate} Hz` : ''}
                    {playbackStats?.audioChannels ? ` · ${playbackStats.audioChannels} ch` : ''}
                  </span>
                  <span className="text-white/60">Dropped</span>
                  <span>
                    dec {playbackStats?.decoderDropFrames ?? 0} / out {playbackStats?.outputDropFrames ?? 0}
                  </span>
                  <span className="text-white/60">Cache</span>
                  <span>
                    {(playbackStats?.cacheDurationSec ?? 0).toFixed(1)}s · {formatSpeed(playbackStats?.speed ?? speed)}
                  </span>
                </div>
              </div>
            ) : null}

            {videoFullscreen ? (
              <div className="flex items-center gap-3 rounded-xl bg-black/65 px-4 py-2.5 backdrop-blur-xs">
                <button
                  type="button"
                  onClick={onTogglePlay}
                  aria-label={playing ? '暂停播放' : '开始播放'}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-[var(--domain-media)] px-3 py-1.5 text-sm font-semibold text-white"
                >
                  {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                  <span>{playing ? '暂停' : '播放'}</span>
                </button>
                <button
                  type="button"
                  onClick={() => onSeek(Math.max(0, currentSec - 15))}
                  className="cursor-pointer rounded-lg bg-white/15 px-2.5 py-1.5 font-mono text-xs hover:bg-white/25"
                >
                  -15s
                </button>
                <input
                  type="range"
                  min={0}
                  max={totalSec}
                  value={currentSec}
                  onChange={(e) => onSeek(Number(e.target.value))}
                  aria-label="全屏播放时间轴"
                  className="flex-1 cursor-pointer accent-[var(--domain-media)]"
                />
                <button
                  type="button"
                  onClick={() => onSeek(Math.min(totalSec, currentSec + 15))}
                  className="cursor-pointer rounded-lg bg-white/15 px-2.5 py-1.5 font-mono text-xs hover:bg-white/25"
                >
                  +15s
                </button>
              </div>
            ) : (
              <div className="max-w-2xl">
                <div className="text-[14.5px] font-medium text-white/85">
                  {item.creator} · {item.verificationLabel ?? item.sourceId}
                </div>
                <h2 className="mt-1 line-clamp-2 text-[23px] leading-snug font-bold tracking-tight">
                  {item.title}
                </h2>
                <p className="mt-1 line-clamp-1 text-[15.5px] text-white/85">{item.subtitle}</p>
              </div>
            )}
          </div>

          <div className="absolute right-0 bottom-0 left-0 h-1 bg-black/30">
            <div className="h-full bg-white/90" style={{ width: `${progressPct}%` }} />
          </div>
        </div>
      )}

      {/* Integrated Scrubber & Transport Controls */}
      <div
        className="flex flex-col gap-3 rounded-[16px] p-4"
        style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
      >
        <div className="flex items-center gap-3">
          <span className="w-12 font-mono text-[13.5px] text-[var(--text-muted)]">
            {formatMediaTime(currentSec)}
          </span>
          <input
            type="range"
            min={0}
            max={totalSec}
            value={currentSec}
            onChange={(e) => onSeek(Number(e.target.value))}
            aria-label="播放时间轴"
            data-focused={activeFocusId === 'media:player:seek' ? 'true' : undefined}
            {...focusableInputProps('media:player:seek', activeFocusId, onFocus)}
            className={focusClass(
              activeFocusId === 'media:player:seek',
              'flex-1 cursor-pointer accent-[var(--domain-media)]',
            )}
          />
          <span className="w-12 text-right font-mono text-[13.5px] text-[var(--text-muted)]">
            {durationLabel}
          </span>
        </div>

        {onVolumeChange ? (
          <div className="flex items-center gap-3">
            <Volume2
              className="h-4 w-4 shrink-0 text-[var(--text-muted)]"
              aria-hidden="true"
            />
            <span className="w-8 font-mono text-[13.5px] text-[var(--text-muted)]">音量</span>
            <input
              type="range"
              min={0}
              max={100}
              value={volume}
              onChange={(e) => onVolumeChange(Number(e.target.value))}
              aria-label="音量"
              data-focused={activeFocusId === 'media:player:volume' ? 'true' : undefined}
              {...focusableInputProps('media:player:volume', activeFocusId, onFocus)}
              className={focusClass(
                activeFocusId === 'media:player:volume',
                'flex-1 cursor-pointer accent-[var(--domain-media)]',
              )}
            />
            <span className="w-10 text-right font-mono text-[13.5px] text-[var(--text-muted)]">
              {volume}%
            </span>
          </div>
        ) : null}

        {onSpeedChange ? (
          <div className="flex items-center gap-3">
            <span className="w-12 shrink-0 text-[13.5px] text-[var(--text-muted)]">倍速</span>
            <input
              type="range"
              min={0.3}
              max={4}
              step={0.1}
              value={speed}
              onChange={(e) => onSpeedChange(Number(e.target.value))}
              aria-label="播放倍速"
              aria-valuetext={formatSpeed(speed)}
              data-focused={activeFocusId === 'media:player:speed' ? 'true' : undefined}
              {...focusableInputProps('media:player:speed', activeFocusId, onFocus)}
              className={focusClass(
                activeFocusId === 'media:player:speed',
                'min-w-0 flex-1 cursor-pointer accent-[var(--domain-media)]',
              )}
            />
            <span className="w-12 shrink-0 text-right font-mono text-[13.5px] text-[var(--text-subtle)]">
              {formatSpeed(speed)}
            </span>
          </div>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <FocusButton
              focusId="media:player:toggle"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onTogglePlay}
              selected
              tone="media"
              className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[14.5px] font-semibold"
            >
              {playing ? (
                <Pause className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <Play className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              <span>{playing ? '暂停播放' : '开始播放'}</span>
            </FocusButton>
            <FocusButton
              focusId="media:player:rew"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSeek(Math.max(0, currentSec - 15))}
              surface="soft"
              className="inline-flex items-center gap-1 px-3 py-2 font-mono text-[14px]"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              <span>-15s</span>
            </FocusButton>
            <FocusButton
              focusId="media:player:fwd"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSeek(Math.min(totalSec, currentSec + 15))}
              surface="soft"
              className="inline-flex items-center gap-1 px-3 py-2 font-mono text-[14px]"
            >
              <RotateCw className="h-3.5 w-3.5" aria-hidden="true" />
              <span>+15s</span>
            </FocusButton>
            {nextSponsorSegment ? (
              <FocusButton
                focusId="media:player:skip-sponsor"
                activeFocusId={activeFocusId}
                onFocus={onFocus}
                onActivate={() => onSkipSponsor(nextSponsorSegment)}
                selected
                tone="warn"
                className="inline-flex items-center gap-1.5 px-3 py-2 text-[14px] font-medium"
              >
                <SkipForward className="h-3.5 w-3.5" aria-hidden="true" />
                <span>空降跳过赞助</span>
              </FocusButton>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {item.kind === 'music' ? (
              <FocusButton
                focusId="media:action:queue"
                activeFocusId={activeFocusId}
                onFocus={onFocus}
                onActivate={() => onQueueEphemeral(item.id)}
                selected={inEphemeralQueue}
                tone="core"
                surface="soft"
                className="inline-flex items-center gap-1.5 px-3 py-2 text-[14px] font-medium"
              >
                <Radio className="h-3.5 w-3.5" aria-hidden="true" />
                <span>
                  {inEphemeralQueue
                    ? '已收录至「OmniDesk AI 灵感电台」'
                    : '+ 推送到「OmniDesk AI 灵感电台」'}
                </span>
              </FocusButton>
            ) : null}

            {item.kind === 'video' && (item.sourceId === 'plugin-youtube' || item.streamResolver?.providerPluginId === 'plugin-youtube') ? (
              <FocusButton
                focusId="media:action:asr"
                activeFocusId={activeFocusId}
                onFocus={onFocus}
                onActivate={() => onReloadLyrics?.()}
                surface="soft"
                className="inline-flex items-center gap-1.5 px-3 py-2 text-[14px] font-medium"
              >
                <Subtitles className="h-3.5 w-3.5" aria-hidden="true" />
                <span>刷新 YouTube 字幕</span>
              </FocusButton>
            ) : null}
          </div>
        </div>
      </div>

      {/* Editorial Synopsis */}
      <p className="max-w-[68ch] px-1 text-[16.5px] leading-[1.75] text-[var(--text-secondary)]">
        {item.summary}
      </p>

      {/* Synchronized lyrics and video subtitles share the same cue timeline. */}
      {item.kind === 'music' || item.kind === 'video' ? (
        <div className="flex flex-col gap-5">
          <div className="flex flex-wrap items-center gap-3">
            <TranslationButton label={isMusic ? '翻译歌词' : '翻译字幕'} disabled={lyricsLoading} request={{ title: `${item.title} · ${isMusic ? '歌词' : '字幕'}`,
              segments: (item.lyrics ?? []).map((line, index) => ({ id: String(index), text: line.original })),
              onTranslated: results => setTranslations(previous => ({ ...previous, [item.id]: Object.fromEntries(results.map(result => [result.id, result.translation])) })),
            }} />
            {isMusic && onReloadLyrics ? <button type="button" disabled={lyricsLoading} className="min-h-11 rounded-lg px-3 text-sm hover:bg-[var(--hover-fill)] disabled:opacity-40" onClick={onReloadLyrics}>刷新歌词</button> : null}
            {!item.lyrics?.length && !lyricsLoading ? <span className="text-sm text-[var(--text-muted)]">取得原文后即可翻译</span> : null}
          </div>
          {item.listeningCue ? (
            <EditorialCallout domain="media" title="两遍听法指南">
              <p>{item.listeningCue}</p>
            </EditorialCallout>
          ) : null}

          {lyricsLoading ? (
            <p className="px-1 text-[14.5px] text-[var(--text-muted)]">
              正在拉取{isMusic ? '歌词' : '字幕'}…
            </p>
          ) : item.lyrics?.length ? (
            <div className="flex flex-col gap-2.5">
              <PanelSectionHeading title={isMusic ? '双语歌词与乐段同步' : '双语字幕'} meta="点击跳转时间轴" />
              <div ref={lyricsViewport} role="region" aria-label={isMusic ? '同步歌词' : '同步字幕'} tabIndex={0} className="flex max-h-[55vh] min-h-0 flex-col gap-1.5 overflow-y-auto overscroll-contain">
                {item.lyrics.map((line, index) => {
                  const lyricId = `media:lyric:${item.id}:${index}`;
                  const isCurrent = index === activeLyricIndex;
                  return (
                    <FocusButton
                      key={lyricId}
                      focusId={lyricId}
                      activeFocusId={activeFocusId}
                      onFocus={onFocus}
                      onActivate={() => onSeek(line.timeSec)}
                      selected={isCurrent}
                      tone="media"
                      surface={isCurrent ? 'surface' : 'ghost'}
                      className="omni-focus-row shrink-0 px-4 py-3 text-left"
                    >
                      <div className="mb-1 flex items-center justify-between font-mono text-[13.5px] opacity-80">
                        <span>{formatMediaTime(line.timeSec)}</span>
                        {line.note ? <span>{line.note}</span> : null}
                      </div>
                      <div className="text-[16px] leading-[1.6] font-medium text-[var(--text-primary)]">
                        {line.original}
                      </div>
                      <div className="mt-0.5 text-[15px] leading-[1.7] text-[var(--text-muted)]">
                        {translations[item.id]?.[String(index)] ?? line.translation}
                      </div>
                    </FocusButton>
                  );
                })}
              </div>
            </div>
          ) : lyricsMessage ? (
            <p className="px-1 text-[14.5px] leading-[1.7] text-[var(--text-muted)]">
              {lyricsMessage}
            </p>
          ) : null}
        </div>
      ) : null}

      {/* Video Chapters & SponsorBlock Strip */}
      {item.chapters?.length ? (
        <div className="flex flex-col gap-2.5">
          <PanelSectionHeading title="章节与 SponsorBlock 时间轴" meta={`${item.chapters.length} 段`} />
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {item.chapters.map((ch) => {
              const active = currentSec >= ch.startSec && currentSec < ch.endSec;
              const isSponsor = ch.kind !== 'content';
              return (
                <FocusButton
                  key={ch.id}
                  focusId={`media:chapter:${ch.id}`}
                  activeFocusId={activeFocusId}
                  onFocus={onFocus}
                  onActivate={() => onSeek(ch.startSec)}
                  selected={active || isSponsor}
                  tone={isSponsor ? 'warn' : 'media'}
                  surface="surface"
                  className="flex items-center justify-between gap-2 px-3.5 py-2.5 text-left text-[14.5px]"
                >
                  <span className="truncate font-medium">{ch.label}</span>
                  <TagChip tone={isSponsor ? 'warn' : 'neutral'} mono>
                    {ch.kind === 'content' ? '章节' : 'Sponsor'}
                  </TagChip>
                </FocusButton>
              );
            })}
          </div>
        </div>
      ) : null}

      {/* NAS Metadata Panel */}
      {item.nasMeta ? (
        <div
          className="flex flex-col gap-3.5 rounded-[16px] p-5"
          style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
        >
          <PanelSectionHeading
            title="飞牛 fnOS 原盘信息"
            meta={item.nasMeta.imdbRating}
            action={
              <SubHunterAction
                item={item}
                activeFocusId={activeFocusId}
                onFocus={onFocus}
                onStatusChange={onStatusChange}
              />
            }
          />
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2.5 text-[14.5px] sm:grid-cols-2">
            <div>
              <dt className="text-[13.5px] text-[var(--text-subtle)]">视频编码</dt>
              <dd className="mt-0.5 font-mono text-[var(--text-primary)]">{item.nasMeta.codec}</dd>
            </div>
            <div>
              <dt className="text-[13.5px] text-[var(--text-subtle)]">分辨率与码率</dt>
              <dd className="mt-0.5 font-mono text-[var(--text-primary)]">{item.nasMeta.resolution}</dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-[13.5px] text-[var(--text-subtle)]">挂载路径</dt>
              <dd className="mt-0.5 truncate font-mono text-[var(--text-secondary)]">{item.nasMeta.path}</dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-[13.5px] text-[var(--text-subtle)]">字幕状态</dt>
              <dd className="mt-0.5 text-[var(--text-primary)]">{item.nasMeta.subtitleStatus}</dd>
            </div>
          </dl>
          {item.nasMeta.clipQuote ? (
            <EditorialCallout domain="book">
              <span className="italic">{item.nasMeta.clipQuote}</span>
            </EditorialCallout>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};

export default MediaInspector;
