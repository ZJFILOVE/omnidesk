import { applyFontScale, getFontScale } from '../utils/fontScale';
import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CalendarDays,
  ChevronRight,
  Database,
  LoaderCircle,
  RefreshCw,
  Settings,
  ShieldCheck,
  X,
} from 'lucide-react';
import {
  call,
  callCapability,
  formatRpcError,
  hostPaths,
  isTauriAvailable,
  listPlugins,
  type BackendPluginRecord,
  type EventBridgeSnapshot,
  type HostPathsSnapshot,
  type RpcErrorInfo,
  type RuntimeStatus,
} from '../api';
import type { ThemeMode } from '../types';

type Dict = Record<string, unknown>;

function text(value: unknown, fallback = '—'): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function boolText(value: unknown): string {
  return value === true ? '已配置' : value === false ? '未配置' : '未知';
}

export interface HostSettingsPanelProps {
  open: boolean;
  onClose: () => void;
  theme: ThemeMode;
  runtime: RuntimeStatus | null;
  coreOnline: boolean;
  bridgeStatus: EventBridgeSnapshot | null;
  authSummary: { authenticated: number; warnings: number; missing: number };
  onOpenAuth: () => void;
  onOpenCuration: () => void;
  onOpenJobs: () => void;
}

export function HostSettingsPanel({
  open,
  onClose,
  theme,
  runtime,
  coreOnline,
  bridgeStatus,
  authSummary,
  onOpenAuth,
  onOpenCuration,
  onOpenJobs,
}: HostSettingsPanelProps) {
  const [fontScale, setFontScale] = useState(getFontScale);
  const available = isTauriAvailable();
  const [plugins, setPlugins] = useState<BackendPluginRecord[]>([]);
  const [capabilityCount, setCapabilityCount] = useState<number | null>(null);
  const [curation, setCuration] = useState<Dict>({});
  const [router, setRouter] = useState<Dict>({});
  const [chrome, setChrome] = useState<Dict>({});
  const [hostDirs, setHostDirs] = useState<HostPathsSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<RpcErrorInfo | null>(null);

  useEffect(() => {
    if (!open || !available) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);
    void Promise.all([
      listPlugins(),
      hostPaths(),
      call<{ capabilities?: unknown[] }>({ op: 'capability.list', params: {} }),
      callCapability<Dict>({
        capability: 'recommend.feed.v1',
        payload: { action: 'status' },
      }),
      callCapability<Dict>({
        capability: 'llm.router.v1',
        payload: { action: 'status' },
      }),
      callCapability<Dict>({
        capability: 'web.embed.v1',
        providerPluginId: 'plugin-chrome-web',
        payload: { action: 'status' },
      }),
    ]).then(([pluginResult, pathsResult, capabilityResult, curationResult, routerResult, chromeResult]) => {
      if (cancelled) return;
      setLoading(false);
      if (pluginResult.ok) setPlugins(pluginResult.data.plugins);
      if (pathsResult.ok) setHostDirs(pathsResult.data);
      if (capabilityResult.ok) {
        setCapabilityCount(
          Array.isArray(capabilityResult.data.capabilities)
            ? capabilityResult.data.capabilities.length
            : null,
        );
      }
      if (curationResult.ok) setCuration(curationResult.data);
      if (routerResult.ok) setRouter(routerResult.data);
      if (chromeResult.ok) setChrome(chromeResult.data);
      const firstFailure = [pluginResult, pathsResult, capabilityResult, curationResult, routerResult, chromeResult]
        .find((result) => result.available && !result.ok);
      if (firstFailure && !firstFailure.ok) setError(firstFailure.error);
    });
    return () => {
      cancelled = true;
    };
  }, [available, open]);

  if (!open) return null;
  const loadedPlugins = plugins.filter((plugin) => plugin.state === 'loaded').length;
  const primaryModel = text(
    router.primary_model ?? router.model_id ?? router.model,
    '未上报',
  );

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-[var(--backdrop-scrim)] p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label="宿主设置中心"
        data-testid="host-settings-panel"
        className="omni-theme-surface flex max-h-[86vh] w-full max-w-4xl flex-col overflow-hidden rounded-[18px] bg-[var(--bg-elevated)] text-[var(--text-primary)]"
        style={{ boxShadow: 'var(--shadow-elevated)' }}
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border-subtle)] px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-[19px] font-semibold tracking-tight">
              <Settings className="h-5 w-5 text-[var(--domain-core)]" />
              宿主设置中心
            </h2>
            <p className="mt-1 text-[13.5px] text-[var(--text-muted)]">
              运行时、XDG 数据目录、统一认证、AI 路由、策展调度与本地插件目录。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭宿主设置中心"
            className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <label className="mx-5 mt-4 flex flex-wrap items-center gap-3 text-base">
          界面字号
          <select aria-label="界面字号" value={fontScale} onChange={(event) => { const value = Number(event.target.value); setFontScale(value); applyFontScale(value); }} className="min-h-11 rounded bg-[var(--bg-soft)] px-3">
            {[1, 1.1, 1.2, 1.3, 1.4, 1.6].map((scale) => <option key={scale} value={scale}>{Math.round(scale * 100)}%{scale === 1.2 ? '（默认）' : ''}</option>)}
          </select>
        </label>
        {!available ? (
          <div className="mx-5 mt-3 flex items-center gap-2 rounded-[9px] bg-[var(--status-warn-bg)] px-3 py-2 text-[13.5px] text-[var(--status-warn-fg)]">
            <AlertTriangle className="h-4 w-4" />
            离线预览：只显示静态运行时信息，路径与远端目录不可读取。
          </div>
        ) : null}
        {error ? (
          <div className="mx-5 mt-3 flex items-start gap-2 rounded-[9px] bg-[var(--status-err-bg)] px-3 py-2 text-[13.5px] text-[var(--status-err-fg)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{formatRpcError(error)}</span>
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {loading ? (
            <div className="mb-3 flex items-center gap-2 text-[13px] text-[var(--text-muted)]">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> 正在汇总宿主状态…
            </div>
          ) : null}

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <section className="rounded-[13px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <h3 className="text-[15px] font-semibold">运行时</h3>
              <dl className="mt-2 space-y-1.5 text-[13px]">
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">Shell</dt>
                  <dd className="font-mono">{runtime?.shell ?? '未连接'}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">版本</dt>
                  <dd className="font-mono">{runtime?.version ?? '—'}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">Core / Bus</dt>
                  <dd className="font-mono">
                    {coreOnline ? 'online' : 'offline'} / {bridgeStatus?.status ?? 'unknown'}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">mpv</dt>
                  <dd className="font-mono">
                    {runtime?.mpv?.available ? runtime.mpv.backend : 'unavailable'}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">主题</dt>
                  <dd className="font-mono">{theme}</dd>
                </div>
              </dl>
            </section>

            <section className="rounded-[13px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <h3 className="flex items-center gap-1.5 text-[15px] font-semibold">
                <Database className="h-4 w-4" /> 数据与插件目录
              </h3>
              <dl className="mt-2 space-y-1.5 text-[13px]">
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">插件</dt>
                  <dd className="font-mono">
                    {plugins.length} 个 / {loadedPlugins} loaded
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">能力</dt>
                  <dd className="font-mono">{capabilityCount ?? '—'}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">Chrome 桥接</dt>
                  <dd className="font-mono">
                    {text(chrome.status ?? chrome.state, 'unknown')}
                  </dd>
                </div>
              </dl>
              <p className="mt-2 break-all rounded-[8px] bg-[var(--bg-canvas)] px-2.5 py-2 font-mono text-[11.5px] text-[var(--text-muted)]">
                {text(curation.curation_dir, '策展目录未上报')}
              </p>
              <dl className="mt-2 space-y-1 rounded-[8px] bg-[var(--bg-canvas)] px-2.5 py-2 font-mono text-[11.5px]">
                <div className="flex gap-2">
                  <dt className="w-16 shrink-0 text-[var(--text-subtle)]">config</dt>
                  <dd className="min-w-0 break-all text-[var(--text-muted)]">
                    {hostDirs?.configDir ?? '—'}
                  </dd>
                </div>
                <div className="flex gap-2">
                  <dt className="w-16 shrink-0 text-[var(--text-subtle)]">data</dt>
                  <dd className="min-w-0 break-all text-[var(--text-muted)]">
                    {hostDirs?.dataDir ?? '—'}
                  </dd>
                </div>
                <div className="flex gap-2">
                  <dt className="w-16 shrink-0 text-[var(--text-subtle)]">state</dt>
                  <dd className="min-w-0 break-all text-[var(--text-muted)]">
                    {hostDirs?.stateDir ?? '—'}
                  </dd>
                </div>
                <div className="flex gap-2">
                  <dt className="w-16 shrink-0 text-[var(--text-subtle)]">runtime</dt>
                  <dd className="min-w-0 break-all text-[var(--text-muted)]">
                    {hostDirs?.runtimeDir ?? '—'}
                  </dd>
                </div>
                <div className="flex gap-2">
                  <dt className="w-16 shrink-0 text-[var(--text-subtle)]">socket</dt>
                  <dd className="min-w-0 break-all text-[var(--text-muted)]">
                    {hostDirs?.controlSocket ?? '—'}
                  </dd>
                </div>
              </dl>
              <p className="mt-1 break-all font-mono text-[11.5px] text-[var(--text-subtle)]">
                obsidian: {text(curation.obsidian_vault_dir, '—')}
              </p>
              <p className="mt-0.5 break-all font-mono text-[11.5px] text-[var(--text-subtle)]">
                music-daily: {text(curation.music_daily_dir, '—')}
              </p>
            </section>

            <section className="rounded-[13px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <h3 className="flex items-center gap-1.5 text-[15px] font-semibold">
                <ShieldCheck className="h-4 w-4" /> 统一认证摘要
              </h3>
              <div className="mt-2 grid grid-cols-3 gap-1.5 text-center">
                <div className="rounded-[8px] bg-[var(--status-ok-bg)] px-2 py-2 text-[var(--status-ok-fg)]">
                  <div className="font-mono text-[17px] font-semibold">{authSummary.authenticated}</div>
                  <div className="text-[11.5px]">登录</div>
                </div>
                <div className="rounded-[8px] bg-[var(--status-warn-bg)] px-2 py-2 text-[var(--status-warn-fg)]">
                  <div className="font-mono text-[17px] font-semibold">{authSummary.warnings}</div>
                  <div className="text-[11.5px]">警告</div>
                </div>
                <div className="rounded-[8px] bg-[var(--status-err-bg)] px-2 py-2 text-[var(--status-err-fg)]">
                  <div className="font-mono text-[17px] font-semibold">{authSummary.missing}</div>
                  <div className="text-[11.5px]">缺失</div>
                </div>
              </div>
              <button
                type="button"
                onClick={onOpenAuth}
                className="mt-3 flex w-full cursor-pointer items-center justify-between rounded-[8px] bg-[var(--bg-canvas)] px-2.5 py-2 text-[13px] hover:bg-[var(--hover-fill)]"
              >
                打开统一认证中心
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </section>

            <section className="rounded-[13px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <h3 className="text-[15px] font-semibold">AI 路由与工作流</h3>
              <dl className="mt-2 space-y-1.5 text-[13px]">
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">主模型</dt>
                  <dd className="truncate font-mono">{primaryModel}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">Skill</dt>
                  <dd className="font-mono">{boolText(curation.skill_available)}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">历史索引</dt>
                  <dd className="font-mono">{String(curation.dates_indexed ?? '—')} 天</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">日报条目</dt>
                  <dd className="font-mono">
                    {String(curation.daily_media_count ?? 0)} media /{' '}
                    {String(curation.daily_text_count ?? 0)} text
                  </dd>
                </div>
              </dl>
              <div className="mt-3 grid grid-cols-2 gap-1.5">
                <button
                  type="button"
                  onClick={onOpenCuration}
                  className="inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-[8px] bg-[var(--bg-canvas)] px-2 py-2 text-[13px] hover:bg-[var(--hover-fill)]"
                >
                  <CalendarDays className="h-3.5 w-3.5" /> 推荐日历
                </button>
                <button
                  type="button"
                  onClick={onOpenJobs}
                  className="inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-[8px] bg-[var(--bg-canvas)] px-2 py-2 text-[13px] hover:bg-[var(--hover-fill)]"
                >
                  <RefreshCw className="h-3.5 w-3.5" /> 任务中心
                </button>
              </div>
            </section>
          </div>

          <section className="mt-3 rounded-[13px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <h3 className="text-[15px] font-semibold">本地插件目录（Marketplace 合并视图）</h3>
            <p className="mt-1 text-[12.5px] text-[var(--text-muted)]">
              当前显示 Core 合并后的在线目录；远端目录不可达时保留本地已发现插件。
            </p>
            <div className="mt-2 grid grid-cols-1 gap-1.5 md:grid-cols-2">
              {plugins.length === 0 ? (
                <p className="text-[13px] text-[var(--text-muted)]">未读取到插件目录。</p>
              ) : (
                plugins.slice(0, 24).map((plugin) => (
                  <div
                    key={plugin.plugin_id}
                    className="flex items-center justify-between gap-2 rounded-[8px] bg-[var(--bg-canvas)] px-2.5 py-1.5 text-[12.5px]"
                  >
                    <span className="truncate font-mono">{plugin.plugin_id}</span>
                    <span className="shrink-0 text-[var(--text-muted)]">{plugin.state}</span>
                  </div>
                ))
              )}
            </div>
          </section>
        </div>
      </section>
    </div>
  );
}

export default HostSettingsPanel;
