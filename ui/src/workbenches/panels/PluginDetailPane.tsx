import React from 'react';
import { CheckCircle2, Play, Power, RefreshCw, ShieldCheck } from 'lucide-react';
import StateView from '../../components/StateView';
import { FocusButton, HAIRLINE_BORDER } from '../../components/WorkbenchLayout';
import { PanelSectionHeading, TagChip } from '../../components/WorkbenchPrimitives';
import type { PluginItem } from '../../data/mock';

export interface PluginDetailPaneProps {
  plugin: PluginItem;
  activeFocusId: string;
  busyAction?: string | null;
  onFocus: (id: string) => void;
  onToggleInstall: () => void;
  onToggleEnabled: () => void;
  onHotReload: () => void;
  onVerify: () => void;
}

export const PluginDetailPane: React.FC<PluginDetailPaneProps> = ({
  plugin,
  activeFocusId,
  busyAction = null,
  onFocus,
  onToggleInstall,
  onToggleEnabled,
  onHotReload,
  onVerify,
}) => (
  <div className="flex flex-col gap-5">
    {/* Plugin Overview & Lifecycle Controls */}
    <div
      className="flex flex-col gap-4 rounded-[18px] p-6"
      style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-2.5">
            <h2 className="text-[24px] leading-tight font-bold tracking-tight text-[var(--text-primary)]">
              {plugin.name}
            </h2>
            <span className="font-mono text-[14.5px] text-[var(--text-muted)]">
              v{plugin.version}
            </span>
          </div>
          <div className="mt-1.5 font-mono text-[14px] text-[var(--text-muted)]">
            {plugin.id} · {plugin.author} · {plugin.kind}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 font-mono text-[13.5px]">
          <span
            data-testid="plugin-health-badge"
            className="rounded-full px-3 py-0.5 font-medium"
            style={{
              backgroundColor: plugin.healthOk
                ? 'var(--status-ok-bg)'
                : 'var(--status-err-bg)',
              color: plugin.healthOk
                ? 'var(--status-ok-fg)'
                : 'var(--status-err-fg)',
            }}
          >
            health: {plugin.healthOk ? 'ok' : 'fault'}
          </span>
          <TagChip
            tone={
              plugin.verifyStatus === 'passed'
                ? 'ok'
                : plugin.verifyStatus === 'failed'
                  ? 'err'
                  : 'neutral'
            }
            mono
          >
            verify: {plugin.verifyStatus}
          </TagChip>
          {plugin.installed && plugin.hotReloadMs > 0 ? (
            <TagChip tone="neutral" mono>
              reload: {plugin.hotReloadMs}ms
            </TagChip>
          ) : null}
        </div>
      </div>

      {plugin.runtimeError ? (
        <StateView
          mode="error"
          variant="inline"
          testId="plugin-runtime-error"
          title="插件运行异常"
          error={plugin.runtimeError}
        />
      ) : null}

      <p className="max-w-[68ch] text-[16px] leading-[1.72] text-[var(--text-secondary)]">
        {plugin.description}
      </p>

      <div className="flex flex-wrap items-center gap-2 border-t pt-4" style={{ borderColor: HAIRLINE_BORDER }}>
        <FocusButton
          focusId="store:action:install"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onToggleInstall}
          selected={!plugin.installed}
          tone="ok"
          surface="soft"
          className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[14px] font-semibold"
        >
          <Power className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{plugin.installed ? '卸载插件' : '安装并启用插件'}</span>
        </FocusButton>
        {plugin.installed ? (
          <>
            <FocusButton
              focusId="store:action:toggle"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onToggleEnabled}
              selected
              tone={plugin.enabled ? 'warn' : 'ok'}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[14px] font-semibold"
            >
              <Play className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{plugin.enabled ? '停用插件' : '启用插件'}</span>
            </FocusButton>
            <FocusButton
              focusId="store:action:reload"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onHotReload}
              surface="soft"
              className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[14px] font-medium"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{busyAction === 'reload' ? '热重载中...' : '热重载 (.py)'}</span>
            </FocusButton>
          </>
        ) : null}
        <FocusButton
          focusId="store:action:verify"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onVerify}
          surface="soft"
          className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[14px] font-medium"
        >
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{busyAction === 'verify' ? '契约自检中...' : '运行契约自检'}</span>
        </FocusButton>
      </div>
    </div>

    {/* Capability & Permissions Strip */}
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <div
        className="flex flex-col gap-3.5 rounded-[16px] p-5"
        style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
      >
        <PanelSectionHeading title="能力契约边界" />
        <div>
          <div className="mb-1.5 text-[13.5px] font-medium text-[var(--text-muted)]">
            提供能力 (Provides)
          </div>
          <div className="flex flex-wrap gap-1.5">
            {plugin.provides.length ? (
              plugin.provides.map((cap) => (
                <TagChip key={cap} tone="ok" mono>
                  {cap}
                </TagChip>
              ))
            ) : (
              <span className="text-[14px] text-[var(--text-subtle)]">未声明提供能力</span>
            )}
          </div>
        </div>
        <div>
          <div className="mb-1.5 text-[13.5px] font-medium text-[var(--text-muted)]">
            可选依赖 (Consumes)
          </div>
          <div className="flex flex-wrap gap-1.5">
            {plugin.consumes.length ? (
              plugin.consumes.map((cap) => (
                <TagChip key={cap} tone="text" mono>
                  {cap}
                </TagChip>
              ))
            ) : (
              <span className="text-[14px] text-[var(--text-subtle)]">
                无外部强依赖（零耦合底座）
              </span>
            )}
          </div>
        </div>
      </div>

      <div
        className="flex flex-col gap-3.5 rounded-[16px] p-5"
        style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
      >
        <PanelSectionHeading
          title="声明式权限清单"
          action={<ShieldCheck className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />}
        />
        <div className="flex flex-wrap gap-1.5">
          {plugin.permissions.length ? (
            plugin.permissions.map((perm) => (
              <TagChip key={perm} tone="neutral" mono>
                {perm}
              </TagChip>
            ))
          ) : (
            <span className="text-[14px] text-[var(--text-subtle)]">无需额外权限</span>
          )}
        </div>
      </div>
    </div>
  </div>
);

export default PluginDetailPane;
