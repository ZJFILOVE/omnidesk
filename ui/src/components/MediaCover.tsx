import { useEffect, useState } from 'react';
import { callCapability, isTauriAvailable } from '../api';
import type { MediaItem } from '../data/mock';

const coverRequests = new Map<string, Promise<string | undefined>>();

export function isSafeImageUrl(url: string) {
  return /^https?:\/\//i.test(url) || /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/=]+$/i.test(url);
}

export function MediaCover({ url, item }: { url?: string; item?: MediaItem }) {
  const [failedUrl, setFailedUrl] = useState<string | undefined>();
  const [proxied, setProxied] = useState<{ url: string; data: string }>();
  const normalized = url?.replace(/^http:\/\/([^/]*\.)?hdslb\.com\//i, 'https://$1hdslb.com/').replace(/\{w\}|\{h\}/g, '600').replace(/\{f\}/g, 'jpg');
  const songId = item?.appleMusicSongId;
  const nasId = item?.sourceId === 'plugin-fnos-nas' ? item.id : undefined;
  const sourceKey = songId ? `adam:${songId}` : nasId ? `nas:${nasId}` : normalized;
  useEffect(() => {
    if (!isTauriAvailable() || (!songId && !nasId) || !sourceKey) return;
    let cancelled = false;
    if (!coverRequests.has(sourceKey)) {
      const request = callCapability<{ data_url?: string }>({
        capability: songId ? 'media.adam.v1' : 'media.source.v1',
        providerPluginId: songId ? 'plugin-music' : 'plugin-fnos-nas',
        payload: { action: 'cover', item_id: songId || nasId }, timeoutMs: 45000,
      }).then(res => res.ok && res.data.data_url && isSafeImageUrl(res.data.data_url) ? res.data.data_url : undefined).catch(() => undefined);
      if (coverRequests.size >= 64) coverRequests.delete(coverRequests.keys().next().value!);
      coverRequests.set(sourceKey, request);
    }
    void coverRequests.get(sourceKey)!.then(data => {
      if (data && !cancelled) setProxied({ url: sourceKey, data });
      if (!data) coverRequests.delete(sourceKey);
    });
    return () => { cancelled = true; };
  }, [songId, nasId, sourceKey]);
  useEffect(() => {
    if (!normalized || !/^https:\/\/i[1-4]?\.ytimg\.com\//.test(normalized) || !isTauriAvailable()) return;
    let cancelled = false;
    void callCapability<{ data_url?: string }>({ capability: 'media.youtube.v1', providerPluginId: 'plugin-youtube', payload: { action: 'cover', url: normalized }, timeoutMs: 15_000 }).then(res => {
      if (!cancelled && res.ok && res.data.data_url && isSafeImageUrl(res.data.data_url)) setProxied({ url: normalized, data: res.data.data_url });
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [normalized]);
  const src = proxied && proxied.url === sourceKey ? proxied.data : normalized;
  if (!src || !isSafeImageUrl(src)) return <span className="absolute inset-0 flex items-center justify-center text-xs text-white/80">暂无封面</span>;
  if (src === failedUrl) return <span className="absolute inset-0 flex items-center justify-center text-xs text-white/90">封面暂不可用</span>;
  return <img src={src} alt="" loading="lazy" referrerPolicy="no-referrer"
    className="pointer-events-none absolute inset-0 h-full w-full object-cover"
    onError={() => setFailedUrl(src)} />;
}
