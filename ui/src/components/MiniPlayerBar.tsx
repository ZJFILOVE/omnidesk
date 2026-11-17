import {
  ChevronLeft,
  ChevronRight,
  ListMusic,
  Pause,
  Play,
  RotateCcw,
  SkipForward,
  Volume2,
} from 'lucide-react';
import { useMemo } from 'react';
import type { MediaItem } from '../data/mock';
import { useMediaLyrics } from '../workbenches/useMediaLyrics';
import type { MpvPlaybackController } from '../workbenches/useMpvPlayback';

function clock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0:00';
  const total = Math.floor(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export interface MiniPlayerBarProps {
  controller: MpvPlaybackController;
  onOpenMedia?: (item: MediaItem) => void;
}

export function MiniPlayerBar({ controller, onOpenMedia }: MiniPlayerBarProps) {
  const item = controller.activeItem;
  const lyrics = useMediaLyrics(item ?? undefined);
  const position = controller.positionSec;
  const duration = controller.durationSec ?? item?.durationSec ?? 0;
  const currentLyric = useMemo(() => {
    if (!lyrics.lines.length) return null;
    let found = lyrics.lines[0];
    for (const line of lyrics.lines) {
      if (line.timeSec <= position) found = line;
    }
    return found;
  }, [lyrics.lines, position]);
  const sponsor = useMemo(
    () =>
      item?.chapters?.find(
        (chapter) =>
          (chapter.kind === 'sponsor' || chapter.kind === 'ad') &&
          chapter.startSec <= position + 2 &&
          chapter.endSec > position,
      ) ?? null,
    [item?.chapters, position],
  );

  if (!item) return null;
  const canRestoreMusic =
    controller.lastMusic !== null &&
    controller.lastMusic.id !== item.id &&
    controller.lastMusic.kind === 'music';

  return (
    <div
      data-testid="mini-player-bar"
      className="shrink-0 border-t px-4 py-2"
      style={{
        borderColor: 'var(--border-subtle)',
        backgroundColor: 'var(--bg-surface)',
      }}
    >
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => onOpenMedia?.(item)}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5 rounded-[8px] px-1 py-0.5 text-left hover:bg-[var(--hover-fill)]"
          title="返回媒体工作台"
        >
          <span
            className="flex h-9 w-9 shrink-0 items-end justify-end overflow-hidden rounded-[7px] p-1 text-white"
            style={{ background: item.thumbGradient }}
          >
            <ListMusic className="h-3.5 w-3.5 opacity-90" />
          </span>
          <span className="min-w-0">
            <span className="block truncate text-[13.5px] font-semibold text-[var(--text-primary)]">
              {item.title}
            </span>
            <span className="block truncate text-[12px] text-[var(--text-muted)]">
              {currentLyric
                ? `${currentLyric.original}${currentLyric.translation ? ` · ${currentLyric.translation}` : ''}`
                : `${item.creator}${item.subtitle ? ` · ${item.subtitle}` : ''}`}
            </span>
          </span>
        </button>

        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            aria-label="上一首"
            onClick={() => void controller.previous()}
            className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[7px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button
            type="button"
            aria-label={controller.playing ? '暂停' : '播放'}
            onClick={() => void controller.toggle(item)}
            className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-full bg-[var(--domain-media)] text-white"
          >
            {controller.playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          </button>
          <button
            type="button"
            aria-label="下一首"
            onClick={() => void controller.next()}
            className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[7px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>

        <div className="hidden min-w-0 flex-1 items-center gap-2 md:flex">
          <span className="w-10 text-right font-mono text-[11.5px] text-[var(--text-subtle)]">
            {clock(position)}
          </span>
          <input
            type="range"
            min={0}
            max={Math.max(1, duration)}
            step={1}
            value={Math.min(position, Math.max(1, duration))}
            onChange={(event) => void controller.seek(item, Number(event.target.value))}
            aria-label="播放进度"
            className="min-w-0 flex-1 accent-[var(--domain-media)]"
          />
          <span className="w-10 font-mono text-[11.5px] text-[var(--text-subtle)]">
            {clock(duration)}
          </span>
        </div>

        {sponsor ? (
          <button
            type="button"
            onClick={() => void controller.seek(item, sponsor.endSec)}
            className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-[7px] bg-[var(--status-warn-bg)] px-2 py-1 text-[12px] font-medium text-[var(--status-warn-fg)]"
          >
            <SkipForward className="h-3 w-3" /> 跳过广告
          </button>
        ) : null}

        {canRestoreMusic ? (
          <button
            type="button"
            onClick={() => void controller.resumeLastMusic()}
            className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-[7px] bg-[var(--bg-soft)] px-2 py-1 text-[12px] text-[var(--text-secondary)]"
            title={`恢复 ${controller.lastMusic?.title ?? '上次音乐'}`}
          >
            <RotateCcw className="h-3 w-3" /> 恢复音乐
          </button>
        ) : null}

        <label className="hidden shrink-0 items-center gap-1.5 lg:flex">
          <Volume2 className="h-3.5 w-3.5 text-[var(--text-muted)]" />
          <input
            type="range"
            min={0}
            max={100}
            step={1}
            value={controller.volume}
            onChange={(event) => void controller.setVolume(Number(event.target.value))}
            aria-label="音量"
            className="w-20 accent-[var(--domain-media)]"
          />
        </label>
      </div>
    </div>
  );
}

export default MiniPlayerBar;
