import React from 'react';
import {
  Disc3,
  Film,
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

export interface MediaInspectorProps {
  item: MediaItem;
  playing: boolean;
  currentSec: number;
  durationSec?: number;
  volume?: number;
  playbackBackend?: 'mpv' | 'libmpv' | 'preview';
  videoSurface?: React.ReactNode;
  activeLyricIndex: number;
  nextSponsorSegment: ChapterSegment | null;
  inEphemeralQueue: boolean;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onTogglePlay: () => void;
  onSeek: (sec: number) => void;
  onVolumeChange?: (volume: number) => void;
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
  playbackBackend = 'preview',
  videoSurface,
  activeLyricIndex,
  nextSponsorSegment,
  inEphemeralQueue,
  activeFocusId,
  onFocus,
  onTogglePlay,
  onSeek,
  onVolumeChange,
  onSkipSponsor,
  onQueueEphemeral,
  onStatusChange,
}) => {
  const isMusic = item.kind === 'music';
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
              <div className="flex items-center justify-between font-mono text-[13.5px] opacity-85">
                <span>ALAC</span>
                <Disc3 className="h-4 w-4" aria-hidden="true" />
              </div>
              <div>
                <div className="text-[13.5px] opacity-85">{item.thumbTag}</div>
                <div className="mt-1 line-clamp-2 text-[16.5px] leading-snug font-semibold">
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
          className="relative overflow-hidden rounded-[20px] text-white"
          style={{
            aspectRatio: '16 / 9',
            background: videoSurface ? '#08090b' : item.thumbGradient,
            boxShadow: 'var(--shadow-soft)',
          }}
        >
          {videoSurface ? (
            <div className="absolute inset-0 overflow-hidden bg-black">{videoSurface}</div>
          ) : null}
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
                </div>
              </div>
            </div>

            <div className="max-w-2xl">
              <div className="text-[14.5px] font-medium text-white/85">
                {item.creator} · {item.verificationLabel ?? item.sourceId}
              </div>
              <h2 className="mt-1 line-clamp-2 text-[23px] leading-snug font-bold tracking-tight">
                {item.title}
              </h2>
              <p className="mt-1 line-clamp-1 text-[15.5px] text-white/85">{item.subtitle}</p>
            </div>
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

            {item.kind === 'video' ? (
              <FocusButton
                focusId="media:action:asr"
                activeFocusId={activeFocusId}
                onFocus={onFocus}
                onActivate={() =>
                  onStatusChange('FunASR SenseVoiceSmall 离线双语字幕检查点已验证')
                }
                surface="soft"
                className="inline-flex items-center gap-1.5 px-3 py-2 text-[14px] font-medium"
              >
                <Subtitles className="h-3.5 w-3.5" aria-hidden="true" />
                <span>触发 FunASR 离线字幕校验 (speech.asr.v1)</span>
              </FocusButton>
            ) : null}
          </div>
        </div>
      </div>

      {/* Editorial Synopsis */}
      <p className="max-w-[68ch] px-1 text-[16.5px] leading-[1.75] text-[var(--text-secondary)]">
        {item.summary}
      </p>

      {/* Music Listening Cue + Synchronized Bilingual Lyrics Folio */}
      {item.kind === 'music' ? (
        <div className="flex flex-col gap-5">
          {item.listeningCue ? (
            <EditorialCallout domain="media" title="两遍听法指南">
              <p>{item.listeningCue}</p>
            </EditorialCallout>
          ) : null}

          {item.lyrics?.length ? (
            <div className="flex flex-col gap-2.5">
              <PanelSectionHeading title="双语歌词与乐段同步" meta="点击跳转时间轴" />
              <div className="flex flex-col gap-1.5">
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
                      className="omni-focus-row px-4 py-3 text-left"
                    >
                      <div className="mb-1 flex items-center justify-between font-mono text-[13.5px] opacity-80">
                        <span>{formatMediaTime(line.timeSec)}</span>
                        {line.note ? <span>{line.note}</span> : null}
                      </div>
                      <div className="text-[16px] leading-[1.6] font-medium text-[var(--text-primary)]">
                        {line.original}
                      </div>
                      <div className="mt-0.5 text-[15px] leading-[1.7] text-[var(--text-muted)]">
                        {line.translation}
                      </div>
                    </FocusButton>
                  );
                })}
              </div>
            </div>
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
