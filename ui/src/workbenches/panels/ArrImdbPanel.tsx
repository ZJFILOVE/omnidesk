import { useCallback, useEffect, useState } from 'react';
import { Download, HardDrive, RefreshCw, SearchCheck } from 'lucide-react';
import { callCapability, isTauriAvailable } from '../../api';
import type { MediaItem } from '../../data/mock';

type DownloadMode = 'speed' | 'size';
type ArrService = 'auto' | 'radarr' | 'sonarr';

interface DownloadQueueEntry {
  service?: string;
  title?: string;
  status?: string;
  progress?: string;
  size?: string;
  eta?: string;
}

interface LocalStatusState {
  loading: boolean;
  checked: boolean;
  matched: boolean;
  message: string;
}

export function ArrImdbPanel({
  item,
  onStatusChange,
}: {
  item: MediaItem;
  onStatusChange: (message: string) => void;
}) {
  const imdbMatch = item.id.match(/^tt\d{5,12}$/) ?? item.playUrl.match(/tt\d{5,12}/);
  const imdbId = imdbMatch ? imdbMatch[0] : '';
  const isTv =
    item.badge.toLowerCase().startsWith('tv') ||
    item.subtitle.includes('剧集') ||
    item.subtitle.includes('Sonarr');

  const [mode, setMode] = useState<DownloadMode>('speed');
  const [service, setService] = useState<ArrService>(isTv ? 'sonarr' : 'radarr');
  const [busy, setBusy] = useState(false);
  const [localState, setLocalState] = useState<LocalStatusState>({
    loading: false,
    checked: false,
    matched: false,
    message: '',
  });
  const [resultMsg, setResultMsg] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  const [queueSummary, setQueueSummary] = useState('');
  const [queueItems, setQueueItems] = useState<DownloadQueueEntry[]>([]);

  useEffect(() => {
    setService(isTv ? 'sonarr' : 'radarr');
    setResultMsg('');
    setErrorMsg('');
  }, [item.id, isTv]);

  const checkLocal = useCallback(async () => {
    if (!isTauriAvailable()) return;
    setLocalState({ loading: true, checked: false, matched: false, message: '' });
    try {
      const res = await callCapability<{
        matched?: boolean;
        exists?: boolean;
        best_match?: { title?: string; path?: string };
      }>({
        capability: 'media.source.v1',
        providerPluginId: 'plugin-arr-imdb',
        payload: {
          action: 'local_status',
          ...(imdbId ? { imdb_id: imdbId } : {}),
          title: item.title,
          media_type: isTv ? 'series' : 'movie',
        },
        timeoutMs: 12_000,
      });
      if (!res.ok) {
        setLocalState({
          loading: false,
          checked: true,
          matched: false,
          message: res.error?.message || '本地库核验不可用',
        });
        return;
      }
      const matched = Boolean(res.data?.matched || res.data?.exists);
      const matchTitle = res.data?.best_match?.title || res.data?.best_match?.path || '';
      setLocalState({
        loading: false,
        checked: true,
        matched,
        message: matched
          ? `本地已收录${matchTitle ? `：${matchTitle}` : ''}`
          : '本地片库暂未收录，可直接推送下载',
      });
    } catch {
      setLocalState({
        loading: false,
        checked: true,
        matched: false,
        message: '本地库核验失败',
      });
    }
  }, [imdbId, item.title, isTv]);

  const refreshQueue = useCallback(async () => {
    if (!isTauriAvailable()) return;
    try {
      const res = await callCapability<{
        summary?: string;
        items?: DownloadQueueEntry[];
      }>({
        capability: 'media.source.v1',
        providerPluginId: 'plugin-arr-imdb',
        payload: { action: 'download_status' },
        timeoutMs: 15_000,
      });
      if (res.ok) {
        setQueueSummary(res.data?.summary || '');
        setQueueItems(Array.isArray(res.data?.items) ? res.data.items : []);
      }
    } catch {
      // Non-fatal: queue polling shouldn't block detail view
    }
  }, []);

  useEffect(() => {
    void checkLocal();
    void refreshQueue();
  }, [checkLocal, refreshQueue]);

  const runDownloadAction = async (action: 'download_preview' | 'download_submit') => {
    if (!isTauriAvailable()) {
      setErrorMsg('当前为离线预览模式，无法连接 Radarr / Sonarr');
      return;
    }
    setBusy(true);
    setErrorMsg('');
    setResultMsg('');
    try {
      const res = await callCapability<{
        service?: string;
        submitted?: boolean;
        started?: boolean;
        grabbed_release?: { title?: string; seeders?: number; size_gb?: number };
        candidates?: Array<{ title?: string; seeders?: number; size_gb?: number }>;
        error?: string;
      }>({
        capability: 'media.source.v1',
        providerPluginId: 'plugin-arr-imdb',
        payload: {
          action,
          item_id: imdbId || item.id,
          ...(imdbId ? { imdb_id: imdbId } : {}),
          term: item.title,
          category: item.badge || (isTv ? 'tv_popular' : 'popular'),
          service,
          download_mode: mode,
        },
        timeoutMs: 30_000,
      });
      if (!res.ok) {
        const msg = res.error?.message || '下载联动请求失败';
        setErrorMsg(msg);
        onStatusChange(msg);
        return;
      }
      const grabbed = res.data?.grabbed_release || res.data?.candidates?.[0];
      const relInfo = grabbed?.title
        ? `${grabbed.title}${grabbed.seeders != null ? ` (做种 ${grabbed.seeders})` : ''}`
        : '已匹配最佳资源';
      const msg =
        action === 'download_preview'
          ? `预览 (${res.data?.service || service})：${relInfo}`
          : res.data?.started
            ? `已提交至 ${res.data?.service || service} 并开始抓种：${relInfo}`
            : `已添加到 ${res.data?.service || service}：${relInfo}`;
      setResultMsg(msg);
      onStatusChange(msg);
      if (action === 'download_submit') {
        void refreshQueue();
      }
    } catch {
      setErrorMsg('下载器联动请求异常，请检查 Radarr / Sonarr / qBittorrent 配置');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-label="Radarr / Sonarr 下载联动"
      className="flex flex-col gap-3 rounded-2xl border border-[var(--border-line)] bg-[var(--bg-surface)] p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
          <HardDrive className="h-4 w-4 text-[var(--domain-media)]" aria-hidden="true" />
          <span>片源匹配与下载联动（Radarr / Sonarr + qBittorrent）</span>
        </div>
        <button
          type="button"
          onClick={() => void checkLocal()}
          disabled={localState.loading}
          className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-[var(--border-line)] px-2.5 text-xs hover:bg-[var(--hover-fill)] disabled:opacity-50"
        >
          <SearchCheck className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{localState.loading ? '核验中…' : '核验本地库'}</span>
        </button>
      </div>

      {localState.checked && (
        <p
          role="status"
          className={`text-xs ${localState.matched ? 'text-emerald-400' : 'text-[var(--text-muted)]'}`}
        >
          {localState.message}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-[var(--text-muted)]">下载器：</span>
        {(['radarr', 'sonarr', 'auto'] as const).map((svc) => (
          <button
            key={svc}
            type="button"
            onClick={() => setService(svc)}
            className={`min-h-8 rounded-lg border px-2.5 ${
              service === svc
                ? 'border-[var(--domain-media)] bg-[var(--domain-media)]/15 text-[var(--text-primary)]'
                : 'border-[var(--border-line)] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]'
            }`}
          >
            {svc === 'radarr' ? 'Radarr 电影' : svc === 'sonarr' ? 'Sonarr 剧集' : '自动判断'}
          </button>
        ))}

        <span className="ml-2 text-[var(--text-muted)]">策略：</span>
        {(['speed', 'size'] as const).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setMode(m)}
            className={`min-h-8 rounded-lg border px-2.5 ${
              mode === m
                ? 'border-[var(--domain-media)] bg-[var(--domain-media)]/15 text-[var(--text-primary)]'
                : 'border-[var(--border-line)] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]'
            }`}
          >
            {m === 'speed' ? '极速做种优先' : '画质体积优先'}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void runDownloadAction('download_preview')}
          className="inline-flex min-h-10 items-center gap-1.5 rounded-xl border border-[var(--border-line)] px-3.5 text-sm hover:bg-[var(--hover-fill)] disabled:opacity-50"
        >
          <span>预览最佳资源</span>
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void runDownloadAction('download_submit')}
          className="inline-flex min-h-10 items-center gap-1.5 rounded-xl bg-[var(--domain-media)] px-4 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          <Download className="h-4 w-4" aria-hidden="true" />
          <span>{busy ? '处理中…' : '推送到下载器'}</span>
        </button>
        <button
          type="button"
          onClick={() => void refreshQueue()}
          className="inline-flex min-h-10 items-center gap-1.5 rounded-xl border border-[var(--border-line)] px-3 text-sm hover:bg-[var(--hover-fill)]"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          <span>刷新队列</span>
        </button>
      </div>

      {resultMsg && (
        <p role="status" className="text-xs text-emerald-400">
          {resultMsg}
        </p>
      )}
      {errorMsg && (
        <p role="alert" className="text-xs text-rose-400">
          {errorMsg}
        </p>
      )}

      {(queueSummary || queueItems.length > 0) && (
        <div className="mt-1 border-t border-[var(--border-line)] pt-2.5">
          <p className="mb-1.5 text-xs font-medium text-[var(--text-muted)]">
            下载队列{queueSummary ? ` · ${queueSummary}` : ''}
          </p>
          <ul className="space-y-1 text-xs text-[var(--text-secondary)]">
            {queueItems.slice(0, 6).map((q, idx) => (
              <li key={`${q.title ?? 'q'}:${idx}`} className="flex items-center justify-between gap-3 truncate">
                <span className="truncate">
                  [{q.service || 'arr'}] {q.title || '未知任务'}
                </span>
                <span className="shrink-0 text-[var(--text-muted)]">
                  {[q.progress, q.eta, q.status].filter(Boolean).join(' · ')}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
