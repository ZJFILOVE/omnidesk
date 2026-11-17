import { useEffect, useState } from 'react';
import { callCapability, formatRpcError, isTauriAvailable } from '../api';

type MirrorState = { mirror_url: string; mirrors: Array<{ url: string; label: string }> };
const field = 'min-h-11 rounded-lg border border-[var(--border-line)] bg-[var(--bg-canvas)] px-3 text-sm';
function ProviderMirrors({ provider, name, onChanged }: { provider: string; name: string; onChanged: () => void }) {
  const [state, setState] = useState<MirrorState | null>(null);
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    if (!isTauriAvailable()) return;
    let cancelled = false;
    void callCapability<MirrorState>({ capability: 'book.search.v1', providerPluginId: provider, payload: { action: 'mirrors' } }).then(result => {
      if (cancelled) return;
      if (result.ok) { setState(result.data); setUrl(result.data.mirror_url); }
      else setError(formatRpcError(result.error));
    }).catch(err => { if (!cancelled) setError(String(err)); });
    return () => { cancelled = true; };
  }, [provider]);
  const save = async () => {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await callCapability<MirrorState>({ capability: 'book.search.v1', providerPluginId: provider, payload: { action: 'configure_mirror', mirror_url: url } });
      if (!result.ok) { setError(formatRpcError(result.error)); return; }
      setState(result.data); setUrl(result.data.mirror_url); setMessage('已保存，使用新镜像重新搜索。'); onChanged();
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  const openAnna = async () => {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await callCapability({ capability: 'book.search.v1', providerPluginId: provider, payload: { action: 'open_browser' } });
      if (!result.ok) setError(formatRpcError(result.error));
      else setMessage('已打开当前 Anna 页面；完成验证后返回重新搜索。');
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  return <fieldset disabled={busy || !state} className="space-y-2">
    <legend className="mb-2 text-sm font-medium">{name}</legend>
    <div className="flex flex-wrap gap-2">
      <select aria-label={`${name} 镜像`} className={`${field} flex-1`} value={state?.mirrors.some(m => m.url === url) ? url : ''} onChange={e => setUrl(e.target.value)}>
        <option value="">自定义地址</option>{state?.mirrors.map(m => <option key={m.url} value={m.url}>{m.label}</option>)}
      </select>
      <input aria-label={`${name} 镜像地址`} type="url" placeholder="https://…" className={`${field} min-w-64 flex-1`} value={url} onChange={e => setUrl(e.target.value)} />
      <button type="button" disabled={busy || !state || !url.trim()} onClick={() => void save()} className={`${field} disabled:opacity-40`}>{busy ? '正在保存…' : `应用 ${name} 镜像`}</button>
    </div>
    <p className="text-xs text-[var(--text-muted)]">当前：{state?.mirror_url || '正在读取…'}</p>
    {provider === 'plugin-annas-archive' && <button type="button" className={field} onClick={() => void openAnna()}>在 Chrome 打开 Anna / 完成验证</button>}
    {error && <p role="alert" className="text-sm text-[var(--status-err-fg)]">{error}</p>}
    <p role="status" className="text-sm text-[var(--text-muted)]">{message}</p>
  </fieldset>;
}
export function BookMirrorSettings({ onChanged }: { onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  return <div className="rounded-lg border border-[var(--border-subtle)] p-3">
    <button type="button" aria-expanded={open} className="min-h-11 text-sm" onClick={() => setOpen(!open)}>镜像与自定义地址</button>
    {open && <div className="space-y-4 pt-2">
      <ProviderMirrors provider="plugin-zlib" name="Z-Library" onChanged={onChanged} />
      <ProviderMirrors provider="plugin-annas-archive" name="Anna’s Archive" onChanged={onChanged} />
      <p className="text-xs text-[var(--text-muted)]">入口能否访问取决于当前网络与站点验证。安娜 .is 未列入当前官方镜像清单。切换域名后使用对应域名的 Chrome 登录状态。</p>
    </div>}
  </div>;
}
