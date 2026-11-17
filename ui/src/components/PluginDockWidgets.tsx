import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Blocks, LoaderCircle, RefreshCw } from 'lucide-react';
import {
  formatRpcError,
  invokeWorkspaceAction,
  type DockWidgetSnapshot,
} from '../api';
import type { ContentDomain } from '../types';
import { DeclarativePanel } from './DeclarativePanel';
import { FocusItem } from './FocusItem';

type Dict = Record<string, unknown>;

export interface PluginDockWidgetCardProps {
  widget: DockWidgetSnapshot;
  tauriAvailable: boolean;
  onRefreshExtensions?: () => void;
}

function PluginDockWidgetCard({
  widget,
  tauriAvailable,
  onRefreshExtensions,
}: PluginDockWidgetCardProps) {
  const [schema, setSchema] = useState(widget.ui_schema ?? {});
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    setSchema(widget.ui_schema ?? {});
  }, [widget]);

  const handleAction = useCallback(
    async (actionId: string, state: Dict) => {
      if (!tauriAvailable) {
        setActionError('离线预览模式不会提交组件动作。');
        return;
      }
      setPendingAction(actionId);
      setActionError(null);
      const res = await invokeWorkspaceAction({
        kind: 'dock_widget',
        specId: widget.widget_id,
        pluginId: widget.plugin_id,
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
      const nextSchema = res.data.ui_schema;
      if (nextSchema && (typeof nextSchema === 'object' || Array.isArray(nextSchema))) {
        setSchema(nextSchema as Record<string, unknown> | Array<Record<string, unknown>>);
      }
    },
    [tauriAvailable, widget.plugin_id, widget.widget_id],
  );

  return (
    <section
      data-testid="plugin-dock-widget"
      data-widget-id={widget.widget_id}
      className="rounded-[12px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3"
      style={{ boxShadow: 'var(--shadow-page)' }}
    >
      <header className="mb-2.5 flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] bg-[var(--bg-soft)] text-[var(--text-muted)]">
            <Blocks className="h-3.5 w-3.5" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h3 className="truncate text-[14px] font-semibold text-[var(--text-primary)]">
              {widget.title}
            </h3>
            <p className="mt-0.5 truncate font-mono text-[12px] text-[var(--text-subtle)]">
              {widget.plugin_id}
            </p>
          </div>
        </div>
        <FocusItem
          as="button"
          id={`plugin-dock:${widget.plugin_id}:${widget.widget_id}:refresh`}
          zone="dock"
          label="刷新插件组件"
          onActivate={() => onRefreshExtensions?.()}
          className="inline-flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-[7px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
        </FocusItem>
      </header>

      {actionError ? (
        <div
          role="alert"
          className="mb-2 flex items-start gap-1.5 rounded-[8px] bg-[var(--status-err-bg)] px-2.5 py-2 text-[12.5px] text-[var(--status-err-fg)]"
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{actionError}</span>
        </div>
      ) : null}

      <DeclarativePanel
        schema={schema}
        compact
        focusPrefix={`plugin-dock:${widget.plugin_id}:${widget.widget_id}`}
        zone="dock"
        busy={pendingAction !== null}
        onAction={(actionId, state) => {
          void handleAction(actionId, state);
        }}
      />

      {pendingAction ? (
        <div className="mt-2 inline-flex items-center gap-1.5 text-[12.5px] text-[var(--text-muted)]">
          <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          {pendingAction}
        </div>
      ) : null}
    </section>
  );
}

export interface PluginDockWidgetsProps {
  widgets: readonly DockWidgetSnapshot[];
  domain: ContentDomain;
  tauriAvailable: boolean;
  onRefreshExtensions?: () => void;
  onOpenPluginStore?: () => void;
}

export function PluginDockWidgets({
  widgets,
  domain,
  tauriAvailable,
  onRefreshExtensions,
  onOpenPluginStore,
}: PluginDockWidgetsProps) {
  const matching = widgets.filter(
    (widget) =>
      !widget.supported_domains ||
      widget.supported_domains.length === 0 ||
      widget.supported_domains.includes(domain),
  );

  if (matching.length === 0) {
    return (
      <div className="rounded-[12px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-4 py-5">
        <div className="flex items-center gap-2 text-[14.5px] font-semibold text-[var(--text-primary)]">
          <Blocks className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
          当前场景暂无插件组件
        </div>
        <p className="mt-1.5 text-[13.5px] leading-[1.6] text-[var(--text-muted)]">
          在插件中心安装 Apple Music、Bilibili、eBook、NAS 等插件后，它们声明的 F8 伴读组件会出现在这里。
        </p>
        {onOpenPluginStore ? (
          <button
            type="button"
            onClick={onOpenPluginStore}
            className="mt-3 cursor-pointer rounded-[8px] bg-[var(--bg-active)] px-3 py-1.5 text-[13px] font-medium text-[var(--text-primary)] hover:brightness-95"
          >
            前往插件中心
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-2.5" data-testid="plugin-dock-widgets">
      <div className="flex items-center justify-between px-0.5">
        <span className="text-[12.5px] font-medium text-[var(--text-muted)]">
          当前场景组件 · {domain}
        </span>
        <span className="font-mono text-[12px] text-[var(--text-subtle)]">{matching.length}</span>
      </div>
      {matching.map((widget) => (
        <PluginDockWidgetCard
          key={`${widget.plugin_id}:${widget.widget_id}`}
          widget={widget}
          tauriAvailable={tauriAvailable}
          onRefreshExtensions={onRefreshExtensions}
        />
      ))}
    </div>
  );
}

export default PluginDockWidgets;
