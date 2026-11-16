import React from 'react';
import { FocusButton, HAIRLINE_BORDER } from '../../components/WorkbenchLayout';
import type { PluginItem } from '../../data/mock';

export interface PluginDetailPaneProps {
  plugin: PluginItem;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onToggleInstall: () => void;
  onToggleEnabled: () => void;
  onHotReload: () => void;
  onVerify: () => void;
}

export const PluginDetailPane: React.FC<PluginDetailPaneProps> = ({
  plugin,
  activeFocusId,
  onFocus,
  onToggleInstall,
  onToggleEnabled,
  onHotReload,
  onVerify,
}) => (
  <div className="flex flex-col gap-4 p-5">
    <div
      className="flex flex-col gap-3 rounded-lg border p-4"
      style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-bold">{plugin.name}</h2>
            <span className="font-mono text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
              v{plugin.version}
            </span>
          </div>
          <div className="mt-1 font-mono text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
            {plugin.id} · {plugin.author} · {plugin.kind}
          </div>
        </div>
        <div className="flex items-center gap-1.5 font-mono text-[11px]">
          <span
            className="rounded px-2 py-0.5"
            style={{
              backgroundColor:
                plugin.verifyStatus === 'passed'
                  ? 'var(--status-ok-bg, #E7F5ED)'
                  : plugin.verifyStatus === 'failed'
                    ? 'var(--status-err-bg, #FCEAEE)'
                    : 'var(--bg-soft, #F1EFEC)',
              color:
                plugin.verifyStatus === 'passed'
                  ? 'var(--status-ok-fg, #1E7244)'
                  : plugin.verifyStatus === 'failed'
                    ? 'var(--status-err-fg, #B6263A)'
                    : 'var(--text-muted, #66625D)',
            }}
          >
            verify: {plugin.verifyStatus}
          </span>
          {plugin.installed ? (
            <span className="rounded px-2 py-0.5" style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)' }}>
              reload: {plugin.hotReloadMs}ms
            </span>
          ) : null}
        </div>
      </div>

      <p className="text-xs leading-relaxed" style={{ color: 'var(--text-muted, #66625D)' }}>
        {plugin.description}
      </p>

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <FocusButton
          focusId="store:action:install"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onToggleInstall}
          selected={!plugin.installed}
          tone="ok"
          surface="soft"
          className="px-3 py-1.5 text-xs font-semibold"
        >
          {plugin.installed ? '卸载插件' : '安装并启用插件'}
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
              className="px-3 py-1.5 text-xs font-semibold"
            >
              {plugin.enabled ? '停用插件' : '启用插件'}
            </FocusButton>
            <FocusButton
              focusId="store:action:reload"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onHotReload}
              surface="soft"
              className="px-3 py-1.5 text-xs font-medium"
            >
              热重载 (.py)
            </FocusButton>
          </>
        ) : null}
        <FocusButton
          focusId="store:action:verify"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onVerify}
          surface="soft"
          className="px-3 py-1.5 text-xs font-medium"
        >
          运行契约自检
        </FocusButton>
      </div>
    </div>

    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <div
        className="rounded-lg border p-4"
        style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
      >
        <div className="mb-2 text-xs font-semibold">Capability 契约边界</div>
        <div className="mb-2">
          <div
            className="mb-1 font-mono text-[10px] uppercase"
            style={{ color: 'var(--text-muted, #66625D)' }}
          >
            Provides (提供能力)
          </div>
          <div className="flex flex-wrap gap-1.5">
            {plugin.provides.map((cap) => (
              <span
                key={cap}
                className="rounded px-2 py-0.5 font-mono text-[11px]"
                style={{
                  backgroundColor: 'var(--status-ok-bg, #E7F5ED)',
                  color: 'var(--status-ok-fg, #1E7244)',
                }}
              >
                {cap}
              </span>
            ))}
          </div>
        </div>
        <div>
          <div
            className="mb-1 font-mono text-[10px] uppercase"
            style={{ color: 'var(--text-muted, #66625D)' }}
          >
            Consumes (可选依赖)
          </div>
          <div className="flex flex-wrap gap-1.5">
            {plugin.consumes.length ? (
              plugin.consumes.map((cap) => (
                <span
                  key={cap}
                  className="rounded px-2 py-0.5 font-mono text-[11px]"
                  style={{
                    backgroundColor: 'var(--status-info-bg, #E8F2FD)',
                    color: 'var(--status-info-fg, #2260AA)',
                  }}
                >
                  {cap}
                </span>
              ))
            ) : (
              <span className="font-mono text-[11px]" style={{ color: 'var(--text-subtle, #8A847D)' }}>
                无外部强依赖（零耦合底座）
              </span>
            )}
          </div>
        </div>
      </div>

      <div
        className="rounded-lg border p-4"
        style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
      >
        <div className="mb-2 text-xs font-semibold">声明式权限清单</div>
        <div className="flex flex-wrap gap-1.5">
          {plugin.permissions.map((perm) => (
            <span
              key={perm}
              className="rounded border px-2 py-0.5 font-mono text-[11px]"
              style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
            >
              {perm}
            </span>
          ))}
        </div>
      </div>
    </div>
  </div>
);

export default PluginDetailPane;
