import { useEffect, useState } from 'react';
import { invokeDomain, isTauriAvailable } from '../api';
import type { LyricLine, MediaItem } from '../data/mock';

interface LyricLinePayload {
  time_sec?: number;
  original?: string;
  translation?: string;
  note?: string;
}

type LyricsPayload =
  | LyricLinePayload[]
  | {
      count?: number;
      reason?: string;
      lines?: LyricLinePayload[];
    };

export interface MediaLyricsState {
  lines: LyricLine[];
  loading: boolean;
  message: string;
}

function toLyricLines(payload: LyricsPayload | undefined): LyricLine[] {
  const rawLines = Array.isArray(payload) ? payload : (payload?.lines ?? []);
  return rawLines
    .map((line) => ({
      timeSec: typeof line.time_sec === 'number' ? line.time_sec : 0,
      original: line.original ?? '',
      translation: line.translation ?? '',
      note: line.note,
    }))
    .filter((line) => line.original.trim().length > 0);
}

export function useMediaLyrics(item: MediaItem | undefined): MediaLyricsState {
  const [lines, setLines] = useState<LyricLine[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const songId = item?.appleMusicSongId ?? '';

  useEffect(() => {
    if (!item || item.kind !== 'music' || !songId || !isTauriAvailable()) {
      setLines([]);
      setLoading(false);
      setMessage(item?.kind === 'music' ? '当前曲目没有可查询的 Apple Music 编号' : '');
      return undefined;
    }

    let cancelled = false;
    setLines([]);
    setLoading(true);
    setMessage('正在从 Apple Music / Adam 拉取歌词…');
    void invokeDomain<LyricsPayload>({
      domain: 'media',
      sourceId: 'plugin-music',
      method: 'fetch_lyrics',
      payload: { item_id: songId },
      timeoutMs: 45000,
    })
      .then((result) => {
        if (cancelled) return;
        if (!result.available) {
          setMessage('歌词接口不可用：核心守护进程未连接');
          return;
        }
        if (!result.ok) {
          setMessage(`歌词拉取失败：${result.error.message}`);
          return;
        }
        const parsed = toLyricLines(result.data);
        setLines(parsed);
        setMessage(
          parsed.length > 0
            ? `${parsed.length} 行歌词 · 来源 Apple Music / Adam / LRCLIB`
            : !Array.isArray(result.data) && result.data?.reason === 'no_lyrics_available'
              ? '该曲目没有可用歌词（已尝试本地缓存、Adam 原生歌词与 LRCLIB）'
              : '该曲目没有可用歌词',
        );
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setMessage(`歌词拉取失败：${error instanceof Error ? error.message : String(error)}`);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [item, songId]);

  return { lines, loading, message };
}
