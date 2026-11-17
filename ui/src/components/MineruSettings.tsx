import { useState } from 'react';
import { callCapability, formatRpcError, isTauriAvailable } from '../api';

interface Config {
  mineru_wrapper: string;
  mineru_python: string;
  mineru_token_file: string;
  token_configured?: boolean;
  token_file_available?: boolean;
  wrapper_available?: boolean;
  python_available?: boolean;
}
export function MineruSettings() {
  const [config, setConfig] = useState<Config>({ mineru_wrapper: '', mineru_python: '', mineru_token_file: '' });
  const [token, setToken] = useState('');
  const [showToken, setShowToken] = useState(false);
  const [clearToken, setClearToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const execute = async (save: boolean) => {
    if (busy) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await callCapability<Config>({ capability: 'book.bilingual_pdf.v1', providerPluginId: 'plugin-pdf-babeldoc',
        payload: save ? { action: 'configure_mineru', mineru_wrapper: config.mineru_wrapper, mineru_python: config.mineru_python,
          mineru_token_file: config.mineru_token_file, ...(token ? { mineru_api_token: token } : {}), clear_token: clearToken }
          : { action: 'mineru_settings' } });
      if (!result.ok) { setError(formatRpcError(result.error)); return; }
      setConfig(result.data); setLoaded(true); setToken(''); setClearToken(false); setShowToken(false);
      setMessage(save ? 'MinerU 配置已保存；尚未上传 PDF。' : '配置已读取。');
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  const input = 'min-h-11 w-full rounded-lg border border-[var(--border-line)] bg-[var(--bg-canvas)] px-3 text-sm';
  return <details className="w-full rounded-lg border border-[var(--border-subtle)] p-3" onToggle={event => {
    if (event.currentTarget.open && !loaded && !busy && isTauriAvailable()) void execute(false);
    if (!event.currentTarget.open) { setToken(''); setShowToken(false); }
  }}>
    <summary className="cursor-pointer text-sm font-medium">MinerU 设置 · 账号与解析接口</summary>
    <div className="mt-3 space-y-3 text-sm">
      <p className="text-[var(--text-muted)]">使用学案工作台同款 mineru.net 云端适配器，仅在点击“上传当前页”后发送该页。已有解析缓存会直接复用。</p>
      <fieldset disabled={busy || !loaded} className="grid gap-3 md:grid-cols-2">
        <label>API Token（留空保留已有密钥）<input aria-label="MinerU API Token" autoComplete="off" type={showToken ? 'text' : 'password'}
          className={input} value={token} onChange={event => setToken(event.target.value)} placeholder={config.token_configured ? '已配置，不回显' : '输入 MinerU Token'} /></label>
        <label>现有 Token 文件路径（可选）<input className={input} value={config.mineru_token_file} placeholder="留空使用 GLM-OCR 默认 Token 文件"
          onChange={event => setConfig({ ...config, mineru_token_file: event.target.value })} /></label>
        <label>MinerU 适配器路径<input className={input} value={config.mineru_wrapper} placeholder="留空复用学案工作台默认 mineru_api.py"
          onChange={event => setConfig({ ...config, mineru_wrapper: event.target.value })} /></label>
        <label>Python 路径（需安装 PyMuPDF）<input className={input} value={config.mineru_python} placeholder="留空使用 Qwen PDF 虚拟环境"
          onChange={event => setConfig({ ...config, mineru_python: event.target.value })} /></label>
        <label className="flex min-h-11 items-center gap-2"><input type="checkbox" checked={showToken} onChange={event => setShowToken(event.target.checked)} />显示本次输入的 Token</label>
        <label className="flex min-h-11 items-center gap-2"><input type="checkbox" checked={clearToken} onChange={event => setClearToken(event.target.checked)} />清除密钥库中的 MinerU Token</label>
      </fieldset>
      {loaded && <p>密钥库：{config.token_configured ? '已配置' : '未配置'} · Token 文件：{config.token_file_available ? '可用' : '未找到'} · 适配器：{config.wrapper_available ? '可用' : '未找到'} · Python：{config.python_available ? '可用' : '未找到'}</p>}
      <div className="flex gap-3"><button type="button" disabled={busy || !loaded} className="min-h-11 rounded-lg bg-[var(--bg-soft)] px-4 disabled:opacity-40" onClick={() => void execute(true)}>保存 MinerU 配置</button>
        <button type="button" disabled={busy || !isTauriAvailable()} className="min-h-11 rounded-lg px-4" onClick={() => void execute(false)}>重新读取</button></div>
      {error && <p role="alert" className="text-[var(--status-err-fg)]">{error}</p>}
      <p role="status">{busy ? '正在处理配置…' : message}</p>
    </div>
  </details>;
}
