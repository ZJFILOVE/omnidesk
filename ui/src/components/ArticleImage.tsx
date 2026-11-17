import { useState } from 'react';
import { ExternalLink } from './ExternalLink';
import { isSafeImageUrl } from './MediaCover';

/** Shared figure rendering for plugin results and the bilingual reader. */
export function ArticleImage({ url, alt = '', caption = '' }: { url: string; alt?: string; caption?: string }) {
  const [failedUrl, setFailedUrl] = useState('');
  if (!isSafeImageUrl(url)) return null;
  return (
    <figure className="my-5 overflow-hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-canvas)] p-3">
      {failedUrl === url ? (
        <p className="py-6 text-center text-sm text-[var(--text-muted)]">图片加载失败 · <ExternalLink href={url} className="underline">打开原图</ExternalLink></p>
      ) : (
        <ExternalLink href={url} aria-label={alt ? `打开原图：${alt}` : '打开原图'}>
          <img src={url} alt={alt || caption || '正文图片'} loading="lazy" decoding="async" referrerPolicy="no-referrer"
            className="mx-auto max-h-[75vh] max-w-full object-contain" onError={() => setFailedUrl(url)} />
        </ExternalLink>
      )}
      {caption ? <figcaption className="mt-3 text-center text-sm leading-relaxed text-[var(--text-muted)]">{caption}</figcaption> : null}
    </figure>
  );
}
