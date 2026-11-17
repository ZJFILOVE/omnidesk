import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Blocks, LoaderCircle, RefreshCw } from 'lucide-react';
import { formatRpcError, invokeWorkspaceAction, type WorkspaceTabSnapshot } from '../api';
import { DeclarativePanel } from '../components/DeclarativePanel';
import { FocusItem } from '../components/FocusItem';
import { DOMAIN_CSS_VARS, DOMAIN_LABELS } from '../theme';

type Dict = Record<string, unknown>;

export interface PluginWorkspaceProps {
  tab: WorkspaceTabSnapshot;
  isActive?: boolean;
  tauriAvailable?: boolean;
  onRefreshExtensions?: () => void;
}

export function PluginWorkspace({
  tab,
  isActive = true,
  tauriAvailable = true,
  onRefreshExtensions,
}: PluginWorkspaceProps) {
  const [schema, setSchema] = useState(tab.ui_schema ?? {});
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<Dict | null>(null);

  useEffect(() => {
    setSchema(tab.ui_schema ?? {});
  }, [tab]);

  const handleAction = useCallback(
    async (actionId: string, state: Dict) => {
      if (!isActive) return;
      if (!tauriAvailable) {
        setActionError('离线预览模式不会提交插件动作。');
        return;
      }
      setPendingAction(actionId);
      setActionError(null);
      const res = await invokeWorkspaceAction({
        kind: 'workspace_tab',
        specId: tab.tab_id,
        pluginId: tab.plugin_id,
        actionId,
        state,
        timeoutMs: 30000,
      });
      setPendingAction(null);
      if (!res.available) {
        setActionError('Core 控制通道不可用。');
        return;
      }
      if (!res.ok) {
        setActionError(formatRpcError(res.error));
        return;
      }
      setActionResult(res.data);
      const nextSchema = res.data.ui_schema;
      if (nextSchema && (typeof nextSchema === 'object' || Array.isArray(nextSchema))) {
        setSchema(nextSchema as Record<string, unknown> | Array<Record<string, unknown>>);
      }
    },
    [isActive, tab.plugin_id, tab.tab_id, tauriAvailable],
  );

  const resultText =
    actionResult && !actionResult.ui_schema
      ? JSON.stringify(actionResult.result ?? actionResult.data ?? actionResult, null, 2)
      : '';

  return (
    <div
      data-testid="plugin-workspace"
      data-plugin-tab-id={tab.tab_id}
      className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-[var(--bg-canvas)]"
    >
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--border-subtle)] px-6 py-4">
        <div className="flex min-w-0 items-start gap-3">
          <span
            className="mt-0.5 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px]"
            style={{
              color: DOMAIN_CSS_VARS[tab.domain],
              backgroundColor: `color-mix(in srgb, ${DOMAIN_CSS_VARS[tab.domain]} 12%, var(--bg-surface))`,
            }}
            aria-hidden="true"
          >
            <Blocks className="h-4.5 w-4.5" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate text-[22px] font-semibold tracking-tight text-[var(--text-primary)]">
                {tab.title}
              </h1>
              {tab.badge ? (
                <span className="rounded-full bg-[var(--bg-soft)] px-2.5 py-0.5 font-mono text-[12.5px] font-medium text-[var(--text-muted)]">
                  {tab.badge}
                </span>
              ) : null}
              <span className="rounded-full bg-[var(--bg-soft)] px-2.5 py-0.5 text-[12.5px] text-[var(--text-muted)]">
                {DOMAIN_LABELS[tab.domain]}
              </span>
            </div>
            <p className="mt-1 truncate font-mono text-[12.5px] text-[var(--text-subtle)]">
              {tab.plugin_id} · {tab.tab_id}
            </p>
          </div>
        </div>
        <FocusItem
          as="button"
          id={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}:refresh`}
          zone="main"
          label="刷新插件页签"
          onActivate={() => onRefreshExtensions?.()}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-[13.5px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          刷新
        </FocusItem>
      </header>

      {!tauriAvailable ? (
        <div className="mx-6 mt-4 flex items-center gap-2 rounded-[10px] bg-[var(--status-warn-bg)] px-3.5 py-2.5 text-[13.5px] text-[var(--status-warn-fg)]">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          离线预览模式：插件页签需要连接 Core 后才能执行动作。
        </div>
      ) : null}
      {actionError ? (
        <div
          role="alert"
          className="mx-6 mt-4 flex items-start gap-2 rounded-[10px] bg-[var(--status-err-bg)] px-3.5 py-2.5 text-[13.5px] text-[var(--status-err-fg)]"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{actionError}</span>
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4.5">
        <div id={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}:root`} data-focus-id={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}:root`}>
          <DeclarativePanel
            schema={schema}
            focusPrefix={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}`}
            zone="main"
            busy={pendingAction !== null}
            onAction={(actionId, state) => {
              void handleAction(actionId, state);
            }}
          />
        </div>

        {pendingAction ? (
          <div className="mt-3 inline-flex items-center gap-2 rounded-full bg-[var(--bg-soft)] px-3 py-1.5 text-[13px] text-[var(--text-muted)]">
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            正在执行 {pendingAction}…
          </div>
        ) : null}
        {resultText ? (
          <pre className="mt-3 max-h-64 overflow-auto rounded-[11px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3 font-mono text-[12.5px] leading-[1.55] text-[var(--text-secondary)]">
            {resultText}
          </pre>
        ) : null}
      </div>
    </div>
  );
}

export default PluginWorkspace;
