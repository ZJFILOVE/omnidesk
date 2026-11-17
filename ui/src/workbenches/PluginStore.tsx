import React, { useEffect, useRef } from 'react';
import { RefreshCw } from 'lucide-react';
import StateView from '../components/StateView';
import { StatusBanner } from '../components/StatusBanner';
import WorkbenchLayout, {
  FocusButton,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import {
  PanelSectionHeading,
  SearchInput,
  SegmentedPills,
  SelectableRow,
  TagChip,
} from '../components/WorkbenchPrimitives';
import { INITIAL_PLUGINS } from '../data/mock';
import PluginConfigPanel from './panels/PluginConfigPanel';
import PluginDetailPane from './panels/PluginDetailPane';
import { usePluginStoreController, type DomainFilter } from './usePluginStoreController';

const DOMAIN_TABS: { id: DomainFilter; label: string }[] = [
  { id: 'all', label: '全部插件' },
  { id: 'core', label: 'Core 底座' },
  { id: 'media', label: 'Media 影视音乐' },
  { id: 'text', label: 'Text 文字期刊' },
  { id: 'book', label: 'Book 书本词库' },
];

export const PluginStore: React.FC<WorkbenchViewProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  searchTrigger,
  pluginHealthMap,
  onContextChange,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    `store:plugin:${INITIAL_PLUGINS[0]?.id ?? ''}`,
    focusId,
    onFocus,
    focusedItemId,
  );
  const ctrl = usePluginStoreController(pluginHealthMap);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (searchTrigger && searchTrigger > 0) {
      searchRef.current?.focus();
    }
  }, [searchTrigger]);

  const { selectedPlugin } = ctrl;
  useEffect(() => {
    if (!onContextChange) return;
    if (!selectedPlugin) {
      onContextChange(null);
      return;
    }
    onContextChange({
      domain: 'core',
      workbench: 'store',
      itemId: selectedPlugin.id,
      title: selectedPlugin.name,
      subtitle: `v${selectedPlugin.version} · ${selectedPlugin.kind}`,
      sourceId: selectedPlugin.id,
      kind: selectedPlugin.domain,
    });
  }, [onContextChange, selectedPlugin]);

  const enabledCount = ctrl.effectivePlugins.filter((p) => p.enabled).length;

  return (
    <WorkbenchLayout
      domain="core"
      testId="workbench-store"
      title="插件中心"
      subtitle="零二进制 ABI 纯 Python 3.14 插件 · 秒级热重载 · 0600 SecretVault 隔离凭据"
      leftWidthClass="@[860px]/workbench:w-[296px]"
      headerActions={
        <div className="flex flex-wrap items-center gap-2">
          <SearchInput
            focusId="store:search"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            value={ctrl.query}
            onChange={ctrl.setQuery}
            placeholder="搜索插件、Capability..."
            ariaLabel="搜索插件"
            inputRef={searchRef}
            widthClass="w-56"
          />
          <FocusButton
            focusId="store:action:refresh"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => void ctrl.loadPluginList()}
            surface="soft"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12.5px] font-medium"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            <span>刷新列表</span>
          </FocusButton>
          <TagChip tone="neutral" mono>
            已启用 {enabledCount}/{ctrl.effectivePlugins.length}
          </TagChip>
        </div>
      }
      toolbar={
        <div className="flex w-full flex-col gap-2">
          {ctrl.offlineMode ? (
            <StatusBanner
              mode="offline"
              compact
              testId="store-offline-banner"
              message="离线预览：数据为示例（未连接 Tauri，不可执行保存/热重载/自检）"
            />
          ) : ctrl.backendBannerError ? (
            <StatusBanner
              mode="error"
              compact
              testId="store-error-banner"
              error={ctrl.backendBannerError}
            />
          ) : ctrl.degradedNotice ? (
            <StatusBanner
              mode="warn"
              compact
              testId="store-warn-banner"
              message={ctrl.degradedNotice}
            />
          ) : null}

          <div className="flex w-full flex-wrap items-center justify-between gap-2.5">
            <SegmentedPills
              options={DOMAIN_TABS}
              value={ctrl.domainFilter}
              onChange={ctrl.setDomainFilter}
              focusPrefix="store:domain:"
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              tone="core"
              ariaLabel="插件领域筛选"
            />
            <span
              data-testid="store-status-message"
              role={ctrl.formMessage.tone === 'err' ? 'alert' : 'status'}
              className="text-[12.5px] font-medium"
              style={{
                color:
                  ctrl.formMessage.tone === 'err'
                    ? 'var(--status-err-fg, #AA1D33)'
                    : ctrl.formMessage.tone === 'ok'
                      ? 'var(--status-ok-fg, #165E37)'
                      : 'var(--text-muted, #59544D)',
              }}
            >
              {ctrl.formMessage.text}
            </span>
          </div>
        </div>
      }
      leftPane={
        <div className="flex flex-col gap-3 p-4">
          <PanelSectionHeading title="插件目录" meta={`${ctrl.filteredPlugins.length} 项`} />
          {ctrl.filteredPlugins.length === 0 ? (
            <StateView
              mode="empty"
              title="没有匹配的插件"
              description="尝试清空搜索词或切换领域标签。"
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              action={{
                label: '显示全部插件',
                focusId: 'store:empty:reset',
                onActivate: () => {
                  ctrl.setQuery('');
                  ctrl.setDomainFilter('all');
                },
              }}
            />
          ) : (
            <div className="flex flex-col gap-1 px-1.5 py-1">
              {ctrl.filteredPlugins.map((plugin) => {
                const statusTone = !plugin.healthOk
                  ? 'err'
                  : !plugin.installed
                    ? 'neutral'
                    : plugin.enabled
                      ? 'ok'
                      : 'warn';
                const statusLabel = !plugin.healthOk
                  ? '异常'
                  : !plugin.installed
                    ? '未安装'
                    : plugin.enabled
                      ? '运行中'
                      : '已停用';

                return (
                  <SelectableRow
                    key={plugin.id}
                    focusId={`store:plugin:${plugin.id}`}
                    activeFocusId={activeFocusId}
                    selected={selectedPlugin?.id === plugin.id}
                    domain={plugin.domain}
                    onFocus={emitFocus}
                    onActivate={() => {
                      ctrl.setSelectedId(plugin.id);
                      ctrl.setFormMessage({ tone: 'info', text: `已载入 ${plugin.id} 配置 Schema` });
                    }}
                    title={plugin.name}
                    trailing={<TagChip tone={statusTone}>{statusLabel}</TagChip>}
                    meta={
                      <span className="font-mono text-[12px]">
                        {plugin.domain} · {plugin.kind} · v{plugin.version}
                      </span>
                    }
                  />
                );
              })}
            </div>
          )}
        </div>
      }
      mainPane={
        selectedPlugin ? (
          <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-6 sm:p-7">
            <PluginDetailPane
              plugin={selectedPlugin}
              activeFocusId={activeFocusId}
              busyAction={ctrl.busyAction}
              onFocus={emitFocus}
              onToggleInstall={ctrl.handleToggleInstall}
              onToggleEnabled={ctrl.handleToggleEnabled}
              onHotReload={() => void ctrl.handleHotReload()}
              onVerify={() => void ctrl.handleVerifyContract()}
            />

            <PluginConfigPanel
              plugin={selectedPlugin}
              configValues={ctrl.currentConfig}
              revealedSecrets={ctrl.revealedSecrets}
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              onUpdateField={(key, val) =>
                ctrl.setConfigsByPlugin((prev) => ({
                  ...prev,
                  [selectedPlugin.id]: { ...(prev[selectedPlugin.id] ?? {}), [key]: val },
                }))
              }
              onToggleSecretReveal={(key) =>
                ctrl.setRevealedSecrets((prev) => ({ ...prev, [key]: !prev[key] }))
              }
              onSave={() => void ctrl.handleSaveConfig()}
              onReset={ctrl.handleResetConfig}
            />
          </div>
        ) : (
          <div className="mx-auto max-w-2xl p-8">
            <StateView mode="empty" title="未选定插件" description="从左侧插件目录选择一项查看详情与配置。" />
          </div>
        )
      }
    />
  );
};

export default PluginStore;
