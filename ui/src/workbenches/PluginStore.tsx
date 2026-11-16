import React, { useEffect, useMemo, useRef, useState } from 'react';
import WorkbenchLayout, {
  FocusButton,
  HAIRLINE_BORDER,
  SectionLabel,
  focusClass,
  focusableInputProps,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import {
  INITIAL_PLUGINS,
  type DomainKind,
  type PluginItem,
} from '../data/mock';
import PluginConfigPanel from './panels/PluginConfigPanel';
import PluginDetailPane from './panels/PluginDetailPane';

type DomainFilter = 'all' | DomainKind;

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
  onContextChange,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    `store:plugin:${INITIAL_PLUGINS[0]?.id ?? ''}`,
    focusId,
    onFocus,
    focusedItemId,
  );
  const [plugins, setPlugins] = useState<PluginItem[]>(INITIAL_PLUGINS);
  const [domainFilter, setDomainFilter] = useState<DomainFilter>('all');
  const [query, setQuery] = useState<string>('');
  const [selectedId, setSelectedId] = useState<string>(INITIAL_PLUGINS[0]?.id ?? '');
  const [configsByPlugin, setConfigsByPlugin] = useState<
    Record<string, Record<string, string | boolean | number>>
  >(() => Object.fromEntries(INITIAL_PLUGINS.map((p) => [p.id, { ...p.initialConfig }])));
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, boolean>>({});
  const [formMessage, setFormMessage] = useState<{ tone: 'ok' | 'err' | 'info'; text: string }>({
    tone: 'info',
    text: '0600 ConfigStore & SecretVault 就绪',
  });
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (searchTrigger && searchTrigger > 0) {
      searchRef.current?.focus();
    }
  }, [searchTrigger]);

  const filteredPlugins = useMemo(() => {
    const q = query.trim().toLowerCase();
    return plugins.filter((p) => {
      if (domainFilter !== 'all' && p.domain !== domainFilter) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        p.id.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.provides.some((cap) => cap.toLowerCase().includes(q))
      );
    });
  }, [plugins, domainFilter, query]);

  const selectedPlugin = useMemo(
    () => plugins.find((p) => p.id === selectedId) ?? filteredPlugins[0] ?? plugins[0],
    [plugins, selectedId, filteredPlugins],
  );

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

  const currentConfig = selectedPlugin
    ? configsByPlugin[selectedPlugin.id] ?? selectedPlugin.initialConfig
    : {};

  const updatePlugin = (pluginId: string, updater: (p: PluginItem) => PluginItem) => {
    setPlugins((prev) => prev.map((p) => (p.id === pluginId ? updater(p) : p)));
  };

  const validateRequiredFields = (plugin: PluginItem): string | null => {
    const cfg = configsByPlugin[plugin.id] ?? {};
    for (const field of plugin.config_schema) {
      if (field.required) {
        const val = cfg[field.key];
        if (val === undefined || String(val).trim() === '') {
          return `必填项「${field.label}」(${field.key}) 不能为空`;
        }
      }
    }
    return null;
  };

  const handleSaveConfig = () => {
    if (!selectedPlugin) return;
    const err = validateRequiredFields(selectedPlugin);
    if (err) {
      setFormMessage({ tone: 'err', text: err });
      return;
    }
    const secretCount = selectedPlugin.config_schema.filter(
      (f) => f.type === 'secret' && String(currentConfig[f.key] ?? '').trim() !== '',
    ).length;
    setFormMessage({
      tone: 'ok',
      text: `已保存 ${selectedPlugin.id} 配置（${secretCount} 个密钥隔离写入 0600 SecretVault）`,
    });
  };

  const handleResetConfig = () => {
    if (!selectedPlugin) return;
    const defaults: Record<string, string | boolean | number> = {};
    for (const field of selectedPlugin.config_schema) defaults[field.key] = field.default;
    setConfigsByPlugin((prev) => ({ ...prev, [selectedPlugin.id]: defaults }));
    setFormMessage({ tone: 'info', text: `已恢复 ${selectedPlugin.id} 的 Schema 默认配置` });
  };

  const handleToggleInstall = () => {
    if (!selectedPlugin) return;
    const installing = !selectedPlugin.installed;
    updatePlugin(selectedPlugin.id, (p) => ({
      ...p,
      installed: installing,
      enabled: installing,
      hotReloadMs: installing ? 15 : 0,
      verifyStatus: installing ? 'passed' : 'idle',
    }));
    setFormMessage({
      tone: 'ok',
      text: installing ? `已安装并激活纯 Python 插件 ${selectedPlugin.id}` : `已卸载插件 ${selectedPlugin.id}`,
    });
  };

  const handleToggleEnabled = () => {
    if (!selectedPlugin || !selectedPlugin.installed) return;
    const nextEnabled = !selectedPlugin.enabled;
    updatePlugin(selectedPlugin.id, (p) => ({
      ...p,
      enabled: nextEnabled,
      hotReloadMs: nextEnabled ? Math.max(8, p.hotReloadMs || 12) : 0,
    }));
    setFormMessage({
      tone: 'info',
      text: `${selectedPlugin.id} 已${nextEnabled ? '启用' : '停用（依赖方自动优雅降级）'}`,
    });
  };

  const handleHotReload = () => {
    if (!selectedPlugin || !selectedPlugin.installed) return;
    const nextMs = 8 + ((selectedPlugin.hotReloadMs + 5) % 19);
    updatePlugin(selectedPlugin.id, (p) => ({ ...p, hotReloadMs: nextMs, healthOk: true }));
    setFormMessage({ tone: 'ok', text: `importlib 命名空间热重载完成（${nextMs}ms，核心状态零丢失）` });
  };

  const handleVerifyContract = () => {
    if (!selectedPlugin) return;
    const err = validateRequiredFields(selectedPlugin);
    if (err) {
      updatePlugin(selectedPlugin.id, (p) => ({ ...p, verifyStatus: 'failed' }));
      setFormMessage({ tone: 'err', text: `契约自检失败：${err}` });
      return;
    }
    updatePlugin(selectedPlugin.id, (p) => ({ ...p, verifyStatus: 'passed', healthOk: true }));
    setFormMessage({ tone: 'ok', text: `${selectedPlugin.id} Pydantic 契约与 Capability 注册自检通过` });
  };

  return (
    <WorkbenchLayout
      domain="core"
      title="插件市场与 Schema 表单中心 · PluginStore"
      subtitle="零二进制 ABI 纯 Python 3.14 插件 · 秒级热重载 · 0600 SecretVault 隔离凭据"
      badge="Plugins · Ctrl+5"
      headerActions={
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索插件名、Capability ID..."
            aria-label="搜索插件"
            data-focused={activeFocusId === 'store:search' ? 'true' : undefined}
            {...focusableInputProps('store:search', activeFocusId, emitFocus)}
            className={focusClass(
              activeFocusId === 'store:search',
              'w-56 rounded-md border px-2.5 py-1 text-xs',
            )}
            style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
          />
          <span
            className="rounded border px-2 py-1 font-mono text-[11px]"
            style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
          >
            已启用 {plugins.filter((p) => p.enabled).length}/{plugins.length}
          </span>
        </div>
      }
      toolbar={
        <div className="flex w-full flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-1.5">
            {DOMAIN_TABS.map((tab) => (
              <FocusButton
                key={tab.id}
                focusId={`store:domain:${tab.id}`}
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                onActivate={() => setDomainFilter(tab.id)}
                selected={domainFilter === tab.id}
                tone="core"
                surface="surface"
                className="px-2.5 py-1 text-xs font-medium"
              >
                {tab.label}
              </FocusButton>
            ))}
          </div>
          <span
            className="font-mono text-[11px]"
            style={{
              color:
                formMessage.tone === 'err'
                  ? 'var(--status-err-fg, #B6263A)'
                  : formMessage.tone === 'ok'
                    ? 'var(--status-ok-fg, #1E7244)'
                    : 'var(--text-muted, #66625D)',
            }}
          >
            {formMessage.text}
          </span>
        </div>
      }
      leftPane={
        <div className="flex flex-col gap-2 p-3">
          <SectionLabel className="px-1">插件目录 ({filteredPlugins.length})</SectionLabel>
          {filteredPlugins.map((plugin) => (
            <FocusButton
              key={plugin.id}
              focusId={`store:plugin:${plugin.id}`}
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              onActivate={() => {
                setSelectedId(plugin.id);
                setFormMessage({ tone: 'info', text: `已载入 ${plugin.id} 配置 Schema` });
              }}
              selected={selectedPlugin?.id === plugin.id}
              tone="core"
              className="flex flex-col gap-1 p-3 text-left"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-xs font-semibold">{plugin.name}</span>
                <span
                  className="shrink-0 rounded px-1.5 py-0.5 font-mono text-[9px] font-semibold"
                  style={{
                    backgroundColor: !plugin.installed
                      ? 'var(--bg-soft, #F1EFEC)'
                      : plugin.enabled
                        ? 'var(--status-ok-bg, #E7F5ED)'
                        : 'var(--status-warn-bg, #FCF2DF)',
                    color: !plugin.installed
                      ? 'var(--text-muted, #66625D)'
                      : plugin.enabled
                        ? 'var(--status-ok-fg, #1E7244)'
                        : 'var(--status-warn-fg, #AC5F0C)',
                  }}
                >
                  {!plugin.installed ? '未安装' : plugin.enabled ? '运行中' : '已停用'}
                </span>
              </div>
              <div
                className="flex items-center justify-between font-mono text-[10px]"
                style={{ color: 'var(--text-muted, #66625D)' }}
              >
                <span>
                  {plugin.domain} · {plugin.kind}
                </span>
                <span>v{plugin.version}</span>
              </div>
            </FocusButton>
          ))}
        </div>
      }
      mainPane={
        selectedPlugin ? (
          <PluginDetailPane
            plugin={selectedPlugin}
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onToggleInstall={handleToggleInstall}
            onToggleEnabled={handleToggleEnabled}
            onHotReload={handleHotReload}
            onVerify={handleVerifyContract}
          />
        ) : null
      }
      rightPane={
        selectedPlugin ? (
          <PluginConfigPanel
            plugin={selectedPlugin}
            configValues={currentConfig}
            revealedSecrets={revealedSecrets}
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onUpdateField={(key, val) =>
              setConfigsByPlugin((prev) => ({
                ...prev,
                [selectedPlugin.id]: { ...(prev[selectedPlugin.id] ?? {}), [key]: val },
              }))
            }
            onToggleSecretReveal={(key) =>
              setRevealedSecrets((prev) => ({ ...prev, [key]: !prev[key] }))
            }
            onSave={handleSaveConfig}
            onReset={handleResetConfig}
          />
        ) : null
      }
      footer={
        <>
          <span>焦点锚点: {activeFocusId || '未锁定'}</span>
          <span>CapabilityRegistry · ConfigStore (0600) · SecretVault</span>
        </>
      }
    />
  );
};

export default PluginStore;
