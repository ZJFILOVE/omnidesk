import { useEffect, useRef, useState } from 'react';
import { getTauriGlobal } from '../api';
import { ExternalLink } from './ExternalLink';
import { COMMUNITY_SITES, type CommunityNavigation, type CommunitySite } from '../utils/communityLinks';

export function EmbeddedWebReader({ active, site = 'linuxdo', navigation }: { active: boolean; site?: CommunitySite; navigation?: CommunityNavigation }) {
  const viewport = useRef<HTMLDivElement>(null);
  const commands = useRef<Promise<unknown>>(Promise.resolve());
  const openedRequest = useRef<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const community = COMMUNITY_SITES[site];
  const invoke = getTauriGlobal()?.core?.invoke;
  useEffect(() => {
    const node = viewport.current;
    if (!node || !invoke) return;
    let disposed = false;
    const update = () => {
      const bounds = node.getBoundingClientRect();
      const visible = active && bounds.width > 0 && bounds.height > 0;
      commands.current = commands.current.catch(() => {}).then(async () => {
        if (disposed) return;
        try {
          const target = visible && navigation?.site === site && openedRequest.current !== navigation.requestId ? navigation : undefined;
          setOpening(visible);
          await invoke('web_reader', { action: visible ? 'show' : 'hide', site, url: target?.url,
            x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
          if (!disposed) {
            if (target) openedRequest.current = target.requestId;
            setError(null);
          }
        } catch (err) { if (!disposed) setError(String(err)); }
        finally { if (!disposed) setOpening(false); }
      });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    window.addEventListener('resize', update);
    return () => {
      disposed = true;
      observer.disconnect();
      window.removeEventListener('resize', update);
      commands.current = commands.current.catch(() => {}).then(() => invoke('web_reader', { action: 'hide', site })).catch(() => {});
    };
  }, [active, invoke, site, navigation]);
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="flex flex-wrap items-center gap-3 border-b border-[var(--border-subtle)] px-6 py-2 text-[14px] text-[var(--text-muted)]">
      <button type="button" disabled={!invoke || !active} className="min-h-11 rounded px-3 hover:bg-[var(--hover-fill)]" onClick={() => void invoke?.('web_reader', { action: 'back', site }).catch((err) => setError(String(err)))}>网页后退</button>
      <button type="button" disabled={!invoke || !active} className="min-h-11 rounded px-3 hover:bg-[var(--hover-fill)]" onClick={() => void invoke?.('web_reader', { action: 'reload', site }).catch((err) => setError(String(err)))}>刷新网页</button>
      <span>{community.title} · 内嵌网页</span>
      <span role="status" className="inline-block h-5 w-20 shrink-0 whitespace-nowrap">{opening ? '正在打开…' : ''}</span>
      <ExternalLink href={navigation?.url ?? community.home} externalOnly className="underline">在 Chrome 打开原链接</ExternalLink>
    </div>
    <div ref={viewport} className="relative min-h-[240px] flex-1">
      {error || !invoke ? <p role="alert" className="p-6 text-[var(--status-err-fg)]">内嵌网页暂不可用，请重启更新后的桌面应用。{error}</p> : null}
    </div>
  </div>;
}
