import { useState } from 'react';
import { ExternalLink } from './ExternalLink';
import { isSafeImageUrl } from './MediaCover';
import { callCapability, isTauriAvailable } from '../api';

const SHUIYUAN_IMAGE_HOSTS = new Set([
  'shuiyuan.sjtu.edu.cn',
  'shuiyuan.s3.jcloud.sjtu.edu.cn',
  'mmbiz.qpic.cn',
  'mmbiz.qlogo.cn',
]);

function useShuiyuanImageRecovery(url: string) {
  const [failedUrl, setFailedUrl] = useState('');
  const [proxy, setProxy] = useState<{ url: string; data: string }>();
  const [loadingUrl, setLoadingUrl] = useState('');
  const src = proxy?.url === url ? proxy.data : url;

  const recover = async () => {
    if (loadingUrl === url) return;
    let host = '';
    try {
      host = new URL(url).hostname;
    } catch {
      setFailedUrl(url);
      return;
    }
    if (!SHUIYUAN_IMAGE_HOSTS.has(host) || proxy?.url === url || !isTauriAvailable()) {
      setFailedUrl(url);
      return;
    }
    setLoadingUrl(url);
    try {
      const result = await callCapability<{ data_url?: string }>({
        capability: 'text.source.v1',
        providerPluginId: 'plugin-shuiyuan',
        payload: { action: 'image', url },
        timeoutMs: 20000,
      });
      if (result.ok && result.data.data_url && isSafeImageUrl(result.data.data_url)) {
        setProxy({ url, data: result.data.data_url });
        setFailedUrl('');
      } else {
        setFailedUrl(url);
      }
    } catch {
      setFailedUrl(url);
    } finally {
      setLoadingUrl('');
    }
  };

  return { src, failedUrl, loadingUrl, recover };
}

/** Inline Discourse/Shuiyuan emoji image with authenticated proxy fallback. */
export function InlineEmoji({ url, alt = '' }: { url: string; alt?: string }) {
  const { src, failedUrl, recover } = useShuiyuanImageRecovery(url);
  if (!isSafeImageUrl(url)) {
    return alt ? <span>{alt}</span> : null;
  }
  if (failedUrl === url) {
    return (
      <span title={alt || undefined} className="inline-block text-[0.92em] text-[var(--text-muted)]">
        {alt || '🖼️'}
      </span>
    );
  }
  return (
    <img
      src={src}
      alt={alt || 'emoji'}
      title={alt || undefined}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      className="mx-0.5 inline-block h-[1.25em] w-[1.25em] align-[-0.22em] object-contain"
      onError={() => void recover()}
    />
  );
}

/** Shared figure rendering for plugin results and the bilingual reader. */
export function ArticleImage({ url, alt = '', caption = '', inline = false }: { url: string; alt?: string; caption?: string; inline?: boolean }) {
  const { src, failedUrl, loadingUrl, recover } = useShuiyuanImageRecovery(url);
  if (!isSafeImageUrl(url)) return null;
  const Container = inline ? 'span' : 'figure';
  const Caption = inline ? 'span' : 'figcaption';
  return (
    <Container className="my-5 block overflow-hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-canvas)] p-3">
      {failedUrl === url ? (
        <span role="status" className="block py-6 text-center text-sm text-[var(--text-muted)]">图片加载失败 · <ExternalLink href={url} className="underline">打开原图</ExternalLink></span>
      ) : (
        <ExternalLink href={url} aria-label={alt ? `打开原图：${alt}` : '打开原图'}>
          <img src={src} alt={alt || caption || '正文图片'} loading="lazy" decoding="async" referrerPolicy="no-referrer"
            className="mx-auto max-h-[75vh] max-w-full object-contain" onError={() => void recover()} />
          {loadingUrl === url ? <span role="status">正在通过水源加载图片…</span> : null}
        </ExternalLink>
      )}
      {caption ? <Caption className="mt-3 block text-center text-sm leading-relaxed text-[var(--text-muted)]">{caption}</Caption> : null}
    </Container>
  );
}
