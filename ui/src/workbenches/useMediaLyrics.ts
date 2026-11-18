import { useEffect, useState } from 'react';
import { callCapability, invokeDomain, isTauriAvailable } from '../api';
import type { LyricLine, MediaItem } from '../data/mock';

interface LyricLinePayload {
  time_sec?: number;
  start_ms?: number;
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
      cues?: LyricLinePayload[];
    };

export interface MediaLyricsState {
  lines: LyricLine[];
  loading: boolean;
  message: string;
  reload: () => void;
}

function toLyricLines(payload: LyricsPayload | undefined): LyricLine[] {
  const rawLines = Array.isArray(payload) ? payload : (payload?.lines ?? payload?.cues ?? []);
  return rawLines
    .map((line) => ({
      timeSec: typeof line.time_sec === 'number' ? line.time_sec : typeof line.start_ms === 'number' ? line.start_ms / 1000 : 0,
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
  const [revision, setRevision] = useState(0);
  const songId = item?.appleMusicSongId ?? '';
  const youtube = item?.kind === 'video' && (item.sourceId === 'plugin-youtube' || item.streamResolver?.providerPluginId === 'plugin-youtube');
  const videoId = youtube ? String(item?.streamResolver?.payload?.video_id ?? item?.streamResolver?.payload?.item_id ?? item?.id ?? '') : '';
  const kind = item?.kind;

  useEffect(() => {
    if ((!songId && !videoId) || !isTauriAvailable()) {
      setLines([]);
      setLoading(false);
      setMessage(kind === 'music' ? '当前曲目没有可查询的 Apple Music 编号' : '');
      return undefined;
    }

    let cancelled = false;
    setLines([]);
    setLoading(true);
    const label = videoId ? '字幕' : '歌词';
    setMessage(videoId ? '正在拉取 YouTube 原始字幕…' : '正在从 Apple Music / Adam 拉取歌词…');
    const fetch = videoId ? callCapability<LyricsPayload>({ capability: 'media.youtube.v1', providerPluginId: 'plugin-youtube',
      payload: { action: 'fetch_subtitles', video_id: videoId, bilingual: false }, timeoutMs: 60000,
    }) : invokeDomain<LyricsPayload>({
      domain: 'media',
      sourceId: 'plugin-music',
      method: 'fetch_lyrics',
      payload: { item_id: songId },
      timeoutMs: 45000,
    });
    void fetch
      .then((result) => {
        if (cancelled) return;
        if (!result.available) {
          setMessage(`${label}接口不可用：核心守护进程未连接`);
          return;
        }
        if (!result.ok) {
          setMessage(`${label}拉取失败：${result.error.message}`);
          return;
        }
        const parsed = toLyricLines(result.data);
        setLines(parsed);
        setMessage(
          parsed.length > 0
            ? `${parsed.length} 行${label} · 来源 ${videoId ? 'YouTube' : 'Apple Music / Adam / LRCLIB'}`
            : !Array.isArray(result.data) && result.data?.reason === 'no_lyrics_available'
              ? '该曲目没有可用歌词（已尝试本地缓存、Adam 原生歌词与 LRCLIB）'
              : `没有可用${label}`,
        );
        const untranslated = parsed
          .map((line, index) => ({ id: String(index), text: line.original, hasTranslation: Boolean(line.translation) }))
          .filter((entry) => !entry.hasTranslation && entry.text.trim().length > 0);
        if (untranslated.length > 0) {
          void Promise.resolve(
            callCapability<{ results?: Array<{ id: string; translation: string }> }>({
              capability: 'llm.translate.v1',
              providerPluginId: 'plugin-magpie-agent',
              payload: {
                action: 'cache_lookup',
                segments: untranslated.map(({ id, text }) => ({ id, text })),
                target_lang: 'zh-CN',
              },
            }),
          )
            .then((cacheRes) => {
              if (cancelled || !cacheRes?.ok || !Array.isArray(cacheRes.data?.results) || cacheRes.data.results.length === 0) {
                return;
              }
              const byIndex = new Map(cacheRes.data.results.map((entry) => [Number(entry.id), entry.translation]));
              setLines((prev) =>
                prev.map((line, index) => {
                  const cached = byIndex.get(index);
                  return cached && !line.translation ? { ...line, translation: cached } : line;
                }),
              );
            })
            .catch(() => {});
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setMessage(`${label}拉取失败：${error instanceof Error ? error.message : String(error)}`);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [songId, videoId, kind, revision]);

  return { lines, loading, message, reload: () => setRevision(value => value + 1) };
}
