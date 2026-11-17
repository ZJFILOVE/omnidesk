import { useEffect, useState } from 'react';
import { callCapability, isTauriAvailable } from '../api';

export function isSafeImageUrl(url: string) {
  return /^https?:\/\//i.test(url) || /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/=]+$/i.test(url);
}

export function MediaCover({ url }: { url?: string }) {
  const [failedUrl, setFailedUrl] = useState<string | undefined>();
  const [proxied, setProxied] = useState<{ url: string; data: string }>();
  const normalized = url?.replace(/^http:\/\/([^/]*\.)?hdslb\.com\//i, 'https://$1hdslb.com/').replace(/\{w\}|\{h\}/g, '600').replace(/\{f\}/g, 'jpg');
  useEffect(() => {
    if (!normalized || !/^https:\/\/i[1-4]?\.ytimg\.com\//.test(normalized) || !isTauriAvailable()) return;
    let cancelled = false;
    void callCapability<{ data_url?: string }>({ capability: 'media.youtube.v1', providerPluginId: 'plugin-youtube', payload: { action: 'cover', url: normalized }, timeoutMs: 15_000 }).then(res => {
      if (!cancelled && res.ok && res.data.data_url && isSafeImageUrl(res.data.data_url)) setProxied({ url: normalized, data: res.data.data_url });
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [normalized]);
  if (!normalized || !isSafeImageUrl(normalized)) return <span className="absolute inset-0 flex items-center justify-center text-xs text-white/80">暂无封面</span>;
  const src = proxied?.url === normalized ? proxied.data : normalized;
  if (src === failedUrl) return <span className="absolute inset-0 flex items-center justify-center text-xs text-white/90">封面暂不可用</span>;
  return <img src={src} alt="" loading="lazy" referrerPolicy="no-referrer"
    className="pointer-events-none absolute inset-0 h-full w-full object-cover"
    onError={() => setFailedUrl(src)} />;
}
