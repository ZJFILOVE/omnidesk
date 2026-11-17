import { useState } from 'react';
import { ExternalLink } from './ExternalLink';
import { isSafeImageUrl } from './MediaCover';

/** Shared figure rendering for plugin results and the bilingual reader. */
export function ArticleImage({ url, alt = '', caption = '', inline = false }: { url: string; alt?: string; caption?: string; inline?: boolean }) {
  const [failedUrl, setFailedUrl] = useState('');
  if (!isSafeImageUrl(url)) return null;
  const Container = inline ? 'span' : 'figure';
  const Caption = inline ? 'span' : 'figcaption';
  return (
    <Container className="my-5 block overflow-hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-canvas)] p-3">
      {failedUrl === url ? (
        <span role="status" className="block py-6 text-center text-sm text-[var(--text-muted)]">图片加载失败 · <ExternalLink href={url} className="underline">打开原图</ExternalLink></span>
      ) : (
        <ExternalLink href={url} aria-label={alt ? `打开原图：${alt}` : '打开原图'}>
          <img src={url} alt={alt || caption || '正文图片'} loading="lazy" decoding="async" referrerPolicy="no-referrer"
            className="mx-auto max-h-[75vh] max-w-full object-contain" onError={() => setFailedUrl(url)} />
        </ExternalLink>
      )}
      {caption ? <Caption className="mt-3 block text-center text-sm leading-relaxed text-[var(--text-muted)]">{caption}</Caption> : null}
    </Container>
  );
}
