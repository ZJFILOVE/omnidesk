import { useState } from 'react';

export function isSafeImageUrl(url: string) {
  return /^https?:\/\//i.test(url) || /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/=]+$/i.test(url);
}

export function MediaCover({ url }: { url?: string }) {
  const [failedUrl, setFailedUrl] = useState<string | undefined>();
  if (!url || url === failedUrl || !isSafeImageUrl(url)) return null;
  return <img src={url} alt="" loading="lazy" referrerPolicy="no-referrer"
    className="pointer-events-none absolute inset-0 h-full w-full object-cover"
    onError={() => setFailedUrl(url)} />;
}
