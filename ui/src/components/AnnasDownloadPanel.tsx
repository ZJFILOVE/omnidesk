import { useEffect, useRef, useState } from 'react';
import { callCapability, formatRpcError } from '../api';
import type { BookCatalogItem } from '../data/mock';

type Source = { kind: 'fast' | 'slow' | 'external' | 'direct'; label: string; download_token: string };
type Detail = { title: string; author: string; format: string; size: string; md5: string; description: string; download_options: Source[]; member_api_available: boolean; download_supported?: boolean };
const button = 'min-h-11 rounded-lg border border-[var(--border-line)] px-4 py-2 text-sm disabled:opacity-40';
export function AnnasDownloadPanel({ entry, onClose, onDownloaded }: { entry: BookCatalogItem; onClose: () => void; onDownloaded: (path: string) => void }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [refresh, setRefresh] = useState(0);
  const dialog = useRef<HTMLElement>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; dialog.current?.focus(); return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false;
    setBusy(true); setError('');
    void callCapability<Detail>({ capability: 'book.search.v1', providerPluginId: 'plugin-annas-archive', payload: { action: 'detail', item_id: entry.itemId } }).then(result => {
      if (cancelled) return;
      if (result.ok) { setDetail(result.data); setSelected(''); }
      else setError(formatRpcError(result.error));
    }).catch(err => { if (!cancelled) setError(String(err)); }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [entry.itemId, refresh]);
  useEffect(() => {
    if (!retryAt) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [retryAt]);
  const source = detail?.download_options.find(option => option.download_token === selected);
  const remaining = Math.max(0, Math.ceil((retryAt - now) / 1000));
  const run = async (browser: boolean) => {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await callCapability<Record<string, unknown>>({ capability: 'book.search.v1', providerPluginId: 'plugin-annas-archive', timeoutMs: 300000,
        payload: { action: browser ? 'open_browser' : 'download', item_id: entry.itemId, download_token: selected || undefined, format: detail?.format || undefined } });
      if (!alive.current) return;
      if (!result.ok) {
        setError(formatRpcError(result.error));
        const wait = Number(result.data?.wait_seconds || 0);
        if (wait > 0) { setNow(Date.now()); setRetryAt(Date.now() + wait * 1000); }
        return;
      }
      if (browser) setNotice('已打开来源页面；完成验证或等待后，返回刷新详情或重试下载。');
      else { onDownloaded(String(result.data.file_path || '')); }
    } catch (err) { if (alive.current) setError(String(err)); }
    finally { if (alive.current) setBusy(false); }
  };
  return <div className="fixed inset-0 z-[70] flex items-center justify-center bg-[var(--backdrop-scrim)] p-4">
    <section ref={dialog} tabIndex={-1} role="dialog" aria-modal="true" aria-label="Anna 文件与下载来源"
      onKeyDown={event => {
        if (event.key === 'Escape' && !busy) onClose();
        if (event.key === 'Tab') {
          const nodes = dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)');
          if (!nodes?.length) return;
          const first = nodes[0], last = nodes[nodes.length - 1];
          if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      }} className="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-xl bg-[var(--bg-elevated)] p-6 text-[var(--text-primary)] shadow-2xl">
      <div className="flex items-center justify-between gap-3"><h2 className="text-lg font-semibold">Anna 文件与下载来源</h2><button className={button} disabled={busy} onClick={onClose}>关闭</button></div>
      <h3 className="mt-4 font-semibold">{detail?.title || entry.title}</h3>
      <p className="mt-2 text-sm text-[var(--text-muted)]">{detail?.author} · {detail?.format?.toUpperCase()} {detail?.size}</p>
      <p className="mt-2 break-all font-mono text-xs">MD5：{entry.itemId}</p>
      <p className="mt-3 text-sm text-[var(--text-muted)]">此记录对应一个文件。切换下载来源不会改变文件格式；其他版本请从搜索结果选择。</p>
      {detail?.description && <p className="mt-3 max-h-32 overflow-auto text-sm">{detail.description}</p>}
      <fieldset disabled={busy} className="mt-4 space-y-2"><legend className="mb-2 font-medium">选择下载来源</legend>
        {detail?.member_api_available && <label className="flex min-h-11 items-center gap-2 rounded-lg border border-[var(--border-line)] p-3"><input type="radio" name="anna-source" checked={!selected} onChange={() => setSelected('')} />会员 API · 自动选择服务器</label>}
        {detail?.download_options.map(option => <label key={option.download_token} className="flex min-h-11 items-center gap-2 rounded-lg border border-[var(--border-line)] p-3">
          <input type="radio" name="anna-source" checked={selected === option.download_token} onChange={() => { setSelected(option.download_token); setRetryAt(0); }} />
          <span>{option.label}<span className="ml-2 text-xs text-[var(--text-muted)]">{({ fast: '会员快速下载', slow: '可能需要等待或验证', external: '前往外部来源', direct: '文件链接' })[option.kind]}</span></span>
        </label>)}
        {detail && !detail.download_options.length && !detail.member_api_available && <p>该文件详情没有提供可用下载来源。</p>}
      </fieldset>
      {detail?.download_supported === false && <p className="mt-3 text-sm">当前格式请在来源站点下载，应用暂不支持此格式的文件校验。</p>}
      {error && <p role="alert" className="mt-3 text-sm text-[var(--status-err-fg)]">{error}</p>}
      <p role="status" className="mt-3 text-sm">{busy ? '正在处理…' : remaining ? `请等待 ${remaining} 秒后重试` : notice}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        <button className={button} disabled={busy} onClick={() => void run(true)}>在 Chrome 打开 / 完成验证</button>
        <button className={button} disabled={busy} onClick={() => setRefresh(v => v + 1)}>刷新详情</button>
        <button className={button} disabled={busy || remaining > 0 || !detail || detail.download_supported === false || (!selected && !detail.member_api_available) || source?.kind === 'external' || (source?.kind === 'fast' && !detail.member_api_available)} onClick={() => void run(false)}>下载并校验文件</button>
      </div>
    </section>
  </div>;
}
