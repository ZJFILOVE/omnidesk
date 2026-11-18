import { useEffect, useRef, useState } from 'react';
import { callCapability, formatRpcError, isTauriAvailable } from '../api';
import type { TranslationRequest } from './TranslationContext';

interface Settings {
  translation_backend: 'magpie' | 'codex' | 'agy';
  translation_codex_models: string;
  translation_agy_models: string;
  codex_bin: string;
  agy_bin: string;
  codex_model: string;
  agy_model: string;
  cli_available?: Record<string, boolean>;
  translation_gateway_url: string;
  effective_gateway_url?: string;
  translation_models: string;
  translation_concurrency: number;
  translation_batch_size: number;
}
interface TranslationResult { id: string; source: string; translation: string; served_model?: string }
const defaults: Settings = { translation_backend: 'magpie', translation_codex_models: '', translation_agy_models: '', codex_bin: '', agy_bin: '', codex_model: '', agy_model: '', translation_gateway_url: '', translation_models: '', translation_concurrency: 4, translation_batch_size: 4 };
const inputClass = 'min-h-11 w-full rounded-lg border border-[var(--border-line)] bg-[var(--bg-canvas)] px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]';

export function TranslationPanel({ open, request, onClose }: { open: boolean; request: TranslationRequest | null; onClose: () => void }) {
  const [settings, setSettings] = useState<Settings>(defaults);
  const [models, setModels] = useState<Array<{ id: string; name: string }>>([]);
  const [cliModels, setCliModels] = useState<{ codex: Array<{ id: string; name: string }>; agy: Array<{ id: string; name: string }> }>({ codex: [], agy: [] });
  const [text, setText] = useState('');
  const [language, setLanguage] = useState('zh-CN');
  const [results, setResults] = useState<TranslationResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const version = useRef(0);
  const autoStartedRef = useRef(false);
  const dialog = useRef<HTMLElement>(null);

  useEffect(() => {
    version.current += 1;
    autoStartedRef.current = false;
    setText(request?.segments.map(segment => segment.text).join('\n\n') ?? '');
    setResults([]); setError(''); setNotice(''); setBusy(false);
  }, [request]);
  const refreshCliModels = (isCancelled = () => false) => {
    void callCapability<{ codex?: Array<{ id: string; name: string }>; agy?: Array<{ id: string; name: string }> }>({
      capability: 'llm.translate.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'cli_models' },
    }).then(res => {
      if (isCancelled() || !res.ok) return;
      setCliModels({ codex: res.data.codex ?? [], agy: res.data.agy ?? [] });
    }).catch(() => {});
  };
  useEffect(() => {
    if (!open || !isTauriAvailable()) return;
    let cancelled = false;
    setLoading(true);
    void Promise.all([
      callCapability<Settings>({ capability: 'llm.translate.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'settings' } }),
      callCapability<{ models: Array<{ id: string; name: string }> }>({ capability: 'llm.router.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'list_models' } }),
    ]).then(([config, available]) => {
      if (cancelled) return;
      if (config.ok) setSettings({ ...defaults, ...config.data });
      else setError(formatRpcError(config.error));
      if (available.ok) setModels(available.data.models ?? []);
    }).catch(err => { if (!cancelled) setError(String(err)); }).finally(() => { if (!cancelled) setLoading(false); });
    refreshCliModels(() => cancelled);
    return () => { cancelled = true; };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, [open]);

  const modelKey = settings.translation_backend === 'magpie' ? 'translation_models' : settings.translation_backend === 'codex' ? 'translation_codex_models' : 'translation_agy_models';
  const activeModels = settings.translation_backend === 'magpie' ? models : settings.translation_backend === 'codex' ? cliModels.codex : cliModels.agy;
  const appendTranslationModel = (selectedId: string) => {
    if (!selectedId) return;
    const currentList = settings[modelKey].split(',').map(v => v.trim()).filter(Boolean);
    if (currentList.includes(selectedId)) return;
    setSettings({ ...settings, [modelKey]: [...currentList, selectedId].join(', ') });
  };
  const execute = async (saveOnly: boolean) => {
    if (busy) return;
    const current = version.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const config = await callCapability<Settings>({ capability: 'llm.translate.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'configure', ...settings } });
      if (current !== version.current) return;
      if (!config.ok) { setError(formatRpcError(config.error)); return; }
      setSettings({ ...defaults, ...config.data });
      if (saveOnly) {
        refreshCliModels();
        setNotice('翻译配置已保存，歌词、字幕和文章共用此配置。');
        return;
      }
      // Keep IDs and cue boundaries intact when translating from a reader.
      const segments = request?.segments ?? text.split(/\n\s*\n/).filter(value => value.trim()).map((value, index) => ({ id: String(index), text: value }));
      const result = await callCapability<{ results: TranslationResult[] }>({ capability: 'llm.translate.v1', providerPluginId: 'plugin-magpie-agent',
        timeoutMs: 300000, payload: { action: 'translate', segments, target_lang: language, context: request?.title ?? '',
          backend: settings.translation_backend, models: settings[modelKey].split(',').map(value => value.trim()).filter(Boolean),
          concurrency: settings.translation_concurrency, batch_size: settings.translation_batch_size } });
      if (current !== version.current) return;
      if (!result.ok) { setError(formatRpcError(result.error)); return; }
      if (!Array.isArray(result.data.results) || result.data.results.length !== segments.length) { setError('译文段数与原文不一致，请重试。'); return; }
      setResults(result.data.results);
      request?.onTranslated?.(result.data.results);
      setNotice(`已翻译 ${segments.length} 段${request?.onTranslated ? '，译文已显示在原界面' : ''}。`);
    } catch (err) { if (current === version.current) setError(String(err)); }
    finally { if (current === version.current) setBusy(false); }
  };
  useEffect(() => {
    if (!open || !request?.autoStart || loading || busy || autoStartedRef.current) return;
    autoStartedRef.current = true;
    void execute(false);
  });
  if (!open) return null;
  return <div className="fixed inset-0 z-[80] flex items-center justify-center bg-[var(--backdrop-scrim)] p-4">
    <section ref={dialog} role="dialog" aria-modal="true" aria-label="翻译中心" tabIndex={-1}
      onKeyDown={event => {
        if (event.key === 'Escape') { event.stopPropagation(); onClose(); }
        if (event.key === 'Tab') {
          const nodes = dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)');
          if (!nodes?.length) return;
          const first = nodes[0], last = nodes[nodes.length - 1];
          if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      }} className="flex max-h-[90vh] w-full max-w-5xl flex-col rounded-2xl bg-[var(--bg-elevated)] text-[var(--text-primary)] shadow-2xl">
      <header className="flex items-center justify-between gap-3 border-b border-[var(--border-subtle)] p-5">
        <div><h2 className="text-xl font-semibold">翻译中心</h2><p className="mt-1 text-sm text-[var(--text-muted)]">{request?.title || '歌词、字幕、文章与自由文本'}</p></div>
        <button type="button" className="min-h-11 rounded-lg px-4 hover:bg-[var(--hover-fill)]" onClick={onClose}>关闭翻译</button>
      </header>
      <div className="space-y-5 overflow-y-auto p-5">
        <fieldset disabled={busy || loading} className="grid gap-4 md:grid-cols-2">
          <label className="space-y-1 text-sm">翻译后端<select aria-label="翻译后端" className={inputClass} value={settings.translation_backend}
            onChange={event => setSettings({ ...settings, translation_backend: event.target.value as Settings['translation_backend'] })}>
            <option value="magpie">Magpie / 本地兼容 API</option><option value="codex">Codex CLI · 本机登录</option><option value="agy">AGY CLI · 本机登录</option></select></label>
          {settings.translation_backend === 'magpie' ? <label className="space-y-1 text-sm">翻译接口 / 端口（本地 OpenAI 兼容网关）<input className={inputClass} value={settings.translation_gateway_url}
            placeholder={settings.effective_gateway_url || '留空沿用当前网关'} onChange={event => setSettings({ ...settings, translation_gateway_url: event.target.value })} /></label> :
            <label className="space-y-1 text-sm">CLI 路径（留空自动检测）<input className={inputClass} value={settings[settings.translation_backend === 'codex' ? 'codex_bin' : 'agy_bin']}
              onChange={event => setSettings({ ...settings, [settings.translation_backend === 'codex' ? 'codex_bin' : 'agy_bin']: event.target.value })} /></label>}
          <label className="space-y-1 text-sm">翻译模型（与聊天独立，多个 ID 用逗号分隔）
            <div className="flex gap-2">
              <select aria-label="选择翻译模型" className={`${inputClass} w-44 shrink-0`} value=""
                onChange={event => appendTranslationModel(event.target.value)}>
                <option value="">{activeModels.length ? '选择添加模型…' : '暂无预设模型'}</option>
                {activeModels.map(model => <option key={model.id} value={model.id}>{model.name} ({model.id})</option>)}
              </select>
              <input className={inputClass} list="translation-models" value={settings[modelKey]}
                placeholder={settings.translation_backend === 'magpie' ? "留空使用当前路由" : "留空使用 CLI 默认模型"} onChange={event => setSettings({ ...settings, [modelKey]: event.target.value })} />
            </div>
            <datalist id="translation-models">{activeModels.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}</datalist></label>
          <label className="space-y-1 text-sm">并发请求数（1–8）<input type="number" min={1} max={8} className={inputClass} value={settings.translation_concurrency}
            onChange={event => setSettings({ ...settings, translation_concurrency: Number(event.target.value) })} /></label>
          <label className="space-y-1 text-sm">每批段数（1–32）<input type="number" min={1} max={32} className={inputClass} value={settings.translation_batch_size}
            onChange={event => setSettings({ ...settings, translation_batch_size: Number(event.target.value) })} /></label>
        </fieldset>
        {settings.translation_backend !== 'magpie' && (() => {
          const isCodex = settings.translation_backend === 'codex';
          const agentKey = isCodex ? 'codex_model' : 'agy_model';
          const agentLabel = isCodex ? 'Codex Agent 模型（与翻译独立）' : 'AGY Agent 模型（与翻译独立）';
          const placeholder = isCodex ? '留空使用 Codex 默认模型' : '留空使用 AGY 默认模型';
          const agentModels = isCodex ? cliModels.codex : cliModels.agy;
          const listId = isCodex ? 'codex-agent-models' : 'agy-agent-models';
          return <label className="block space-y-1 text-sm">{agentLabel}
            <div className="flex gap-2">
              <select aria-label={isCodex ? '选择 Codex Agent 模型' : '选择 AGY Agent 模型'} className={`${inputClass} w-56 shrink-0`} disabled={busy || loading}
                value={agentModels.some(m => m.id === settings[agentKey]) ? settings[agentKey] : ''}
                onChange={event => setSettings({ ...settings, [agentKey]: event.target.value })}>
                <option value="">{placeholder}</option>
                {agentModels.map(model => <option key={model.id} value={model.id}>{model.name} ({model.id})</option>)}
              </select>
              <input className={inputClass} disabled={busy || loading} list={listId} value={settings[agentKey]} placeholder={placeholder}
                onChange={event => setSettings({ ...settings, [agentKey]: event.target.value })} />
            </div>
            <datalist id={listId}>{agentModels.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}</datalist></label>;
        })()}
        <p className="text-sm text-[var(--text-muted)]">{settings.translation_backend === 'magpie' ? `实际接口：${settings.effective_gateway_url || '当前网关'}` : `${settings.translation_backend.toUpperCase()} CLI 直连，使用本机登录；每个并发任务独立会话${settings.cli_available?.[settings.translation_backend] === false ? '（尚未检测到 CLI，请设置路径）' : ''}`}。多模型按批次轮流分配，结果保持原文顺序。</p>
        <label className="block space-y-2 text-sm">原文{request ? `（${request.segments.length} 段，保持原行对应）` : '（空行分段）'}
          <textarea aria-label="待翻译原文" readOnly={Boolean(request)} disabled={busy} value={text} onChange={event => setText(event.target.value)} className={`${inputClass} min-h-36`} /></label>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">目标语言<select aria-label="翻译目标语言" disabled={busy} className={inputClass} value={language} onChange={event => setLanguage(event.target.value)}><option value="zh-CN">简体中文</option><option value="en">English</option><option value="ja">日本語</option></select></label>
          <button type="button" disabled={busy || loading || !isTauriAvailable()} onClick={() => void execute(true)} className="min-h-11 rounded-lg border border-[var(--border-line)] px-4 disabled:opacity-40">保存翻译配置</button>
          <button type="button" disabled={busy || loading || !text.trim() || !isTauriAvailable()} onClick={() => void execute(false)} className="min-h-11 rounded-lg bg-[var(--bg-soft)] px-4 font-semibold disabled:opacity-40">{busy ? '正在处理…' : results.length ? '重新翻译' : '开始翻译'}</button>
        </div>
        {error && <p role="alert" className="rounded-lg bg-[var(--status-err-bg)] p-3 text-[var(--status-err-fg)]">{error}</p>}
        <p role="status" aria-live="polite" className="text-sm text-[var(--text-muted)]">{busy ? '正在按并发配置翻译，完成后自动回填。' : loading ? '正在读取翻译配置…' : notice}</p>
        {results.map((result, index) => <div key={`${result.id}-${index}`} className="grid gap-3 rounded-lg border border-[var(--border-subtle)] p-4 md:grid-cols-2"><p className="whitespace-pre-wrap">{result.source}</p><div><p className="whitespace-pre-wrap">{result.translation}</p>{result.served_model && <p className="mt-2 text-xs text-[var(--text-muted)]">{result.served_model}</p>}</div></div>)}
      </div>
    </section>
  </div>;
}
