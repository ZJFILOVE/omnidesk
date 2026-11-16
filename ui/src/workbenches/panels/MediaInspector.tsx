import React from 'react';
import {
  FocusButton,
  HAIRLINE_BORDER,
  SectionLabel,
  focusClass,
  focusableInputProps,
} from '../../components/WorkbenchLayout';
import type { ChapterSegment, MediaItem } from '../../data/mock';

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
  activeLyricIndex: number;
  nextSponsorSegment: ChapterSegment | null;
  inEphemeralQueue: boolean;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onTogglePlay: () => void;
  onSeek: (sec: number) => void;
  onSkipSponsor: (seg: ChapterSegment) => void;
  onQueueEphemeral: (id: string) => void;
  onStatusChange: (msg: string) => void;
}

export const MediaInspector: React.FC<MediaInspectorProps> = ({
  item,
  playing,
  currentSec,
  activeLyricIndex,
  nextSponsorSegment,
  inEphemeralQueue,
  activeFocusId,
  onFocus,
  onTogglePlay,
  onSeek,
  onSkipSponsor,
  onQueueEphemeral,
  onStatusChange,
}) => (
  <div className="flex flex-col gap-4 p-4">
    <div
      className="relative overflow-hidden rounded-lg border p-4 text-white"
      style={{
        aspectRatio: '16 / 9',
        background: item.thumbGradient,
        borderColor: HAIRLINE_BORDER,
      }}
    >
      <div className="flex h-full flex-col justify-between">
        <div className="flex items-center justify-between gap-2">
          <span className="rounded bg-black/55 px-2 py-0.5 font-mono text-[10px]">
            libmpv · {item.kind.toUpperCase()}
          </span>
          <span className="rounded bg-black/55 px-2 py-0.5 font-mono text-[10px]">
            {formatMediaTime(currentSec)} / {item.durationLabel}
          </span>
        </div>
        <div>
          <div className="text-xs opacity-80">{item.creator}</div>
          <div className="line-clamp-2 text-sm font-semibold">{item.title}</div>
        </div>
      </div>
    </div>

    <div className="flex flex-col gap-2">
      <input
        type="range"
        min={0}
        max={item.durationSec}
        value={currentSec}
        onChange={(e) => onSeek(Number(e.target.value))}
        aria-label="播放时间轴"
        data-focused={activeFocusId === 'media:player:seek' ? 'true' : undefined}
        {...focusableInputProps('media:player:seek', activeFocusId, onFocus)}
        className={focusClass(
          activeFocusId === 'media:player:seek',
          'w-full cursor-pointer accent-rose-600',
        )}
      />
      <div className="flex flex-wrap items-center gap-1.5">
        <FocusButton
          focusId="media:player:toggle"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onTogglePlay}
          selected
          tone="media"
          className="px-3 py-1.5 text-xs font-semibold"
        >
          {playing ? '暂停播放' : '开始播放'}
        </FocusButton>
        <FocusButton
          focusId="media:player:rew"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() => onSeek(Math.max(0, currentSec - 15))}
          surface="surface"
          className="px-2.5 py-1.5 font-mono text-xs"
        >
          -15s
        </FocusButton>
        <FocusButton
          focusId="media:player:fwd"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() => onSeek(Math.min(item.durationSec, currentSec + 15))}
          surface="surface"
          className="px-2.5 py-1.5 font-mono text-xs"
        >
          +15s
        </FocusButton>
        {nextSponsorSegment ? (
          <FocusButton
            focusId="media:player:skip-sponsor"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onSkipSponsor(nextSponsorSegment)}
            selected
            tone="warn"
            className="px-2.5 py-1.5 text-xs font-medium"
          >
            空降跳过赞助
          </FocusButton>
        ) : null}
      </div>
    </div>

    {item.kind === 'music' ? (
      <div className="flex flex-col gap-3">
        <FocusButton
          focusId="media:action:queue"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() => onQueueEphemeral(item.id)}
          selected={inEphemeralQueue}
          tone="core"
          surface="soft"
          className="px-3 py-1.5 text-xs font-medium"
        >
          {inEphemeralQueue
            ? '已收录至「OmniDesk AI 灵感电台」'
            : '+ 推送到「OmniDesk AI 灵感电台」'}
        </FocusButton>

        {item.listeningCue ? (
          <div
            className="rounded-lg border p-3 text-xs leading-relaxed"
            style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
          >
            <div className="mb-1 font-semibold">两遍听法指南 · Listening Cue</div>
            <p style={{ color: 'var(--text-muted, #66625D)' }}>{item.listeningCue}</p>
          </div>
        ) : null}

        {item.lyrics?.length ? (
          <div className="flex flex-col gap-1.5">
            <SectionLabel>双语歌词 / 乐段同步（点击跳转）</SectionLabel>
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
                  className="p-2.5 text-left"
                >
                  <div className="mb-0.5 flex items-center justify-between font-mono text-[10px]">
                    <span>{formatMediaTime(line.timeSec)}</span>
                    {line.note ? <span>{line.note}</span> : null}
                  </div>
                  <div className="text-xs font-medium">{line.original}</div>
                  <div className="mt-0.5 text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
                    {line.translation}
                  </div>
                </FocusButton>
              );
            })}
          </div>
        ) : null}
      </div>
    ) : null}

    {item.chapters?.length ? (
      <div className="flex flex-col gap-1.5">
        <SectionLabel>章节与 SponsorBlock 时间轴</SectionLabel>
        {item.chapters.map((ch) => {
          const active = currentSec >= ch.startSec && currentSec < ch.endSec;
          return (
            <FocusButton
              key={ch.id}
              focusId={`media:chapter:${ch.id}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSeek(ch.startSec)}
              selected={active || ch.kind !== 'content'}
              tone={ch.kind !== 'content' ? 'warn' : 'media'}
              className="flex items-center justify-between px-2.5 py-2 text-left text-xs"
            >
              <span className="font-medium">{ch.label}</span>
              <span className="font-mono text-[10px]">
                {ch.kind === 'content' ? '章节' : 'Sponsor'}
              </span>
            </FocusButton>
          );
        })}
      </div>
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
        className="px-3 py-2 text-xs font-medium"
      >
        触发 FunASR 离线字幕校验 (speech.asr.v1)
      </FocusButton>
    ) : null}

    {item.nasMeta ? (
      <div
        className="flex flex-col gap-2 rounded-lg border p-3 text-xs"
        style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
      >
        <div className="flex items-center justify-between font-semibold">
          <span>飞牛 fnOS 原盘元数据</span>
          <span className="font-mono">{item.nasMeta.imdbRating}</span>
        </div>
        <div className="font-mono text-[11px]" style={{ color: 'var(--text-muted, #66625D)' }}>
          <div>{item.nasMeta.codec}</div>
          <div>{item.nasMeta.resolution}</div>
          <div className="truncate">{item.nasMeta.path}</div>
          <div>{item.nasMeta.subtitleStatus}</div>
        </div>
        {item.nasMeta.clipQuote ? (
          <div
            className="rounded border p-2 italic"
            style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
          >
            {item.nasMeta.clipQuote}
          </div>
        ) : null}
        <FocusButton
          focusId="media:action:subhunter"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() =>
            onStatusChange(`SubHunter + ffsubsync 已对齐并保护人工字幕：${item.title}`)
          }
          surface="surface"
          className="px-3 py-1.5 text-xs font-medium"
        >
          运行 SubHunter 字幕检索与时间轴对齐
        </FocusButton>
      </div>
    ) : null}
  </div>
);

export default MediaInspector;
