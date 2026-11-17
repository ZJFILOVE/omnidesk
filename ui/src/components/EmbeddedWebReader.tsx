import { useEffect, useRef, useState } from 'react';
import { getTauriGlobal } from '../api';
import { ExternalLink } from './ExternalLink';

export function EmbeddedWebReader({ active }: { active: boolean }) {
  const viewport = useRef<HTMLDivElement>(null);
  const commands = useRef<Promise<unknown>>(Promise.resolve());
  const [error, setError] = useState<string | null>(null);
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
          await invoke('web_reader', { action: visible ? 'show' : 'hide', x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
          if (!disposed) setError(null);
        } catch (err) { if (!disposed) setError(String(err)); }
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
      commands.current = commands.current.catch(() => {}).then(() => invoke('web_reader', { action: 'hide' })).catch(() => {});
    };
  }, [active, invoke]);
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="flex flex-wrap items-center gap-3 border-b border-[var(--border-subtle)] px-6 py-2 text-[14px] text-[var(--text-muted)]">
      <button type="button" className="min-h-11 rounded px-3 hover:bg-[var(--hover-fill)]" onClick={() => void invoke?.('web_reader', { action: 'back' }).catch((err) => setError(String(err)))}>网页后退</button>
      <button type="button" className="min-h-11 rounded px-3 hover:bg-[var(--hover-fill)]" onClick={() => void invoke?.('web_reader', { action: 'reload' }).catch((err) => setError(String(err)))}>刷新网页</button>
      <span>linux.do · 内嵌网页</span>
      <ExternalLink href="https://linux.do/" className="underline">在 Chrome 打开</ExternalLink>
    </div>
    <div ref={viewport} className="relative min-h-[240px] flex-1">
      {error || !invoke ? <p role="alert" className="p-6 text-[var(--status-err-fg)]">内嵌网页暂不可用，请重启更新后的桌面应用。{error}</p> : null}
    </div>
  </div>;
}
