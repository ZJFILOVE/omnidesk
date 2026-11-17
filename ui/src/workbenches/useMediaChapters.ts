import { useEffect, useState } from 'react';
import { callCapability, isTauriAvailable } from '../api';
import type { ChapterSegment, MediaItem } from '../data/mock';

export function useMediaChapters(item: MediaItem | undefined, active: boolean) {
  const [segments, setSegments] = useState<ChapterSegment[]>([]);
  const [message, setMessage] = useState('');
  useEffect(() => {
    setSegments([]); setMessage('');
    if (!active || !item || !['plugin-bilibili', 'plugin-youtube'].includes(item.sourceId) || !isTauriAvailable()) return;
    let cancelled = false;
    const platform = item.sourceId === 'plugin-bilibili' ? 'bilibili' : 'youtube';
    const id = String(item.streamResolver?.payload.item_id || item.streamResolver?.payload.video_id || item.id);
    setMessage('正在查询广告片段…');
    void callCapability<{ segments?: Record<string, unknown>[] }>({ capability: 'player.timeline.v1', providerPluginId: 'plugin-sponsorblock', payload: { action: 'fetch', platform, video_id: id }, timeoutMs: 20_000 }).then(res => {
      if (cancelled) return;
      if (!res.ok) { setMessage('广告片段服务暂不可用'); return; }
      const rows = (res.data.segments ?? []).flatMap((s, i) => {
        const start = Number(s.start_sec ?? s.start), end = Number(s.end_sec ?? s.end);
        if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) return [];
        const kind: ChapterSegment['kind'] = s.kind === 'content' ? 'content' : s.kind === 'ad' ? 'ad' : 'sponsor';
        return [{ id: String(s.id ?? `sponsor-${i}`), startSec: start, endSec: end, label: String(s.label ?? s.category ?? '赞助片段'), kind }];
      });
      setSegments(rows); setMessage(rows.length ? `已识别 ${rows.length} 个标记片段` : '暂无社区标记的广告片段');
    }).catch(() => { if (!cancelled) setMessage('广告片段服务暂不可用'); });
    return () => { cancelled = true; };
  }, [item?.id, item?.sourceId, active]);
  return { segments, message };
}
