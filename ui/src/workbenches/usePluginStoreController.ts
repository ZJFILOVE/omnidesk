import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  call,
  callCapability,
  formatRpcError,
  isTauriAvailable,
  listPlugins,
  type BackendPluginRecord,
  type RpcErrorInfo,
} from '../api';
import { INITIAL_PLUGINS, type DomainKind, type PluginItem } from '../data/mock';
import type { PluginHealthEntry } from '../hooks/useCoreEvents';
import {
  applyHealthUpdates,
  buildDefaultConfigForSchema,
  mapBackendPlugins,
  splitConfigAndSecrets,
} from './pluginStoreHelpers';

export type DomainFilter = 'all' | DomainKind;

export function usePluginStoreController(pluginHealthMap?: Record<string, PluginHealthEntry>) {
  const [plugins, setPlugins] = useState<PluginItem[]>(INITIAL_PLUGINS);
  const [domainFilter, setDomainFilter] = useState<DomainFilter>('all');
  const [query, setQuery] = useState<string>('');
  const [selectedId, setSelectedId] = useState<string>(INITIAL_PLUGINS[0]?.id ?? '');
  const [configsByPlugin, setConfigsByPlugin] = useState<
    Record<string, Record<string, string | boolean | number>>
  >(() => Object.fromEntries(INITIAL_PLUGINS.map((p) => [p.id, { ...p.initialConfig }])));
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, boolean>>({});
  const [offlineMode, setOfflineMode] = useState<boolean>(() => !isTauriAvailable());
  const [backendBannerError, setBackendBannerError] = useState<RpcErrorInfo | null>(null);
  const [degradedNotice, setDegradedNotice] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [formMessage, setFormMessage] = useState<{ tone: 'ok' | 'err' | 'info'; text: string }>(() =>
    isTauriAvailable()
      ? { tone: 'info', text: '正在向 Core 请求插件列表...' }
      : { tone: 'info', text: '离线预览 · 只读示例模式' },
  );
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const loadPluginList = useCallback(async () => {
    const res = await listPlugins();
    if (!mountedRef.current) return;

    if (!res.available) {
      setOfflineMode(true);
      setBackendBannerError(null);
      setDegradedNotice(null);
      setPlugins(INITIAL_PLUGINS);
      setFormMessage({ tone: 'info', text: '离线预览 · 只读示例模式' });
      return;
    }

    setOfflineMode(false);
    if (!res.ok) {
      setBackendBannerError(res.error);
      setDegradedNotice(null);
      setPlugins(INITIAL_PLUGINS);
      setFormMessage({
        tone: 'err',
        text: `插件列表加载失败 ${formatRpcError(res.error)}（保留示例预览）`,
      });
      return;
    }

    setBackendBannerError(null);
    const records = Array.isArray(res.data.plugins) ? res.data.plugins : [];
    const mapped = mapBackendPlugins(records);
    setPlugins(mapped);
    setConfigsByPlugin((prev) => {
      const next = { ...prev };
      for (const p of mapped) {
        if (!next[p.id]) {
          next[p.id] = buildDefaultConfigForSchema(p.config_schema);
        }
      }
      return next;
    });
    if (mapped.length > 0) {
      setSelectedId((prevSel) => (mapped.some((p) => p.id === prevSel) ? prevSel : mapped[0].id));
    }

    if (!res.data.core_online || res.data.degraded) {
      setDegradedNotice(res.data.notice || 'Core 服务离线，当前为本地清单扫描只读视图');
      setFormMessage({
        tone: 'info',
        text: `已加载 ${mapped.length} 个本地清单（Core 离线降级：${res.data.source}）`,
      });
    } else {
      setDegradedNotice(null);
      setFormMessage({
        tone: 'ok',
        text: `已从 Core 载入 ${mapped.length} 个插件（${res.data.source}）`,
      });
    }
  }, []);

  useEffect(() => {
    void loadPluginList();
  }, [loadPluginList]);

  const effectivePlugins = useMemo(
    () => applyHealthUpdates(plugins, pluginHealthMap),
    [plugins, pluginHealthMap],
  );

  const filteredPlugins = useMemo(() => {
    const q = query.trim().toLowerCase();
    return effectivePlugins.filter((p) => {
      if (domainFilter !== 'all' && p.domain !== domainFilter) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        p.id.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.provides.some((cap) => cap.toLowerCase().includes(q))
      );
    });
  }, [effectivePlugins, domainFilter, query]);

  const selectedPlugin = useMemo(
    () =>
      effectivePlugins.find((p) => p.id === selectedId) ??
      filteredPlugins[0] ??
      effectivePlugins[0],
    [effectivePlugins, selectedId, filteredPlugins],
  );

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

  const handleSaveConfig = async () => {
    if (!selectedPlugin || busyAction) return;
    const err = validateRequiredFields(selectedPlugin);
    if (err) {
      setFormMessage({ tone: 'err', text: err });
      return;
    }

    const { plainConfig, secretValues } = splitConfigAndSecrets(selectedPlugin, currentConfig);
    setBusyAction('save');
    const res = await callCapability({
      capability: 'plugin.config.v1',
      payload: {
        action: 'set',
        plugin_id: selectedPlugin.id,
        config: plainConfig,
        secrets: secretValues,
      },
    });
    if (!mountedRef.current) return;
    setBusyAction(null);

    if (!res.available) {
      setOfflineMode(true);
      setFormMessage({ tone: 'err', text: '离线预览：当前未连接 Tauri 运行时，禁止伪造保存配置' });
      return;
    }
    if (!res.ok) {
      setFormMessage({ tone: 'err', text: `保存配置失败 ${formatRpcError(res.error)}` });
      return;
    }
    setFormMessage({
      tone: 'ok',
      text: `已保存 ${selectedPlugin.id} 配置（后端已确认持久化，含 ${Object.keys(secretValues).length} 项 0600 密钥）`,
    });
  };

  const handleResetConfig = () => {
    if (!selectedPlugin) return;
    const defaults = buildDefaultConfigForSchema(selectedPlugin.config_schema);
    setConfigsByPlugin((prev) => ({ ...prev, [selectedPlugin.id]: defaults }));
    setFormMessage({ tone: 'info', text: `已恢复 ${selectedPlugin.id} 的本地表单默认值（未写入后端）` });
  };

  const runLifecycleOp = async (
    actionKey: string,
    op: 'plugin.load' | 'plugin.unload',
    successText: string,
  ) => {
    if (!selectedPlugin || busyAction) return;
    setBusyAction(actionKey);
    const res = await call<Partial<BackendPluginRecord>>({
      op,
      params: { plugin_id: selectedPlugin.id },
    });
    if (!mountedRef.current) return;
    setBusyAction(null);

    if (!res.available) {
      setOfflineMode(true);
      setFormMessage({ tone: 'err', text: '离线预览：当前未连接 Tauri 运行时，禁止修改插件生命周期' });
      return;
    }
    if (!res.ok) {
      updatePlugin(selectedPlugin.id, (p) => ({
        ...p,
        healthOk: false,
        runtimeError: res.error.message,
      }));
      setFormMessage({ tone: 'err', text: `操作失败 ${formatRpcError(res.error)}` });
      return;
    }
    const nextState = String(res.data.state ?? (op === 'plugin.load' ? 'loaded' : 'unloaded'));
    updatePlugin(selectedPlugin.id, (p) => ({
      ...p,
      installed: true,
      enabled: typeof res.data.enabled === 'boolean' ? res.data.enabled : op === 'plugin.load',
      healthOk: nextState !== 'faulted' && nextState !== 'invalid' && !res.data.error,
      runtimeState: nextState,
      runtimeError: typeof res.data.error === 'string' ? res.data.error : '',
    }));
    setFormMessage({ tone: 'ok', text: successText });
  };

  const handleToggleInstall = () => {
    if (!selectedPlugin) return;
    const op = selectedPlugin.installed && selectedPlugin.enabled ? 'plugin.unload' : 'plugin.load';
    void runLifecycleOp(
      'install',
      op,
      op === 'plugin.load' ? `已加载并激活插件 ${selectedPlugin.id}` : `已卸载停用插件 ${selectedPlugin.id}`,
    );
  };

  const handleToggleEnabled = () => {
    if (!selectedPlugin || !selectedPlugin.installed) return;
    const op = selectedPlugin.enabled ? 'plugin.unload' : 'plugin.load';
    void runLifecycleOp(
      'toggle',
      op,
      op === 'plugin.load' ? `${selectedPlugin.id} 已启用` : `${selectedPlugin.id} 已停用（依赖方自动优雅降级）`,
    );
  };

  const handleHotReload = async () => {
    if (!selectedPlugin || !selectedPlugin.installed || busyAction) return;
    setBusyAction('reload');
    const startedAt = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const res = await call<Partial<BackendPluginRecord>>({
      op: 'plugin.reload',
      params: { plugin_id: selectedPlugin.id },
    });
    if (!mountedRef.current) return;
    setBusyAction(null);

    if (!res.available) {
      setOfflineMode(true);
      setFormMessage({ tone: 'err', text: '离线预览：当前未连接 Tauri 运行时，不可执行热重载' });
      return;
    }
    if (!res.ok) {
      updatePlugin(selectedPlugin.id, (p) => ({
        ...p,
        healthOk: false,
        runtimeError: res.error.message,
      }));
      setFormMessage({ tone: 'err', text: `热重载失败 ${formatRpcError(res.error)}` });
      return;
    }

    const nowMs = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const elapsedMs = Math.max(1, Math.round(nowMs - startedAt));
    const nextState = String(res.data.state ?? 'loaded');
    const reloadCount =
      typeof res.data.reload_count === 'number'
        ? res.data.reload_count
        : (selectedPlugin.reloadCount ?? 0) + 1;

    updatePlugin(selectedPlugin.id, (p) => ({
      ...p,
      enabled: typeof res.data.enabled === 'boolean' ? res.data.enabled : true,
      hotReloadMs: elapsedMs,
      healthOk: nextState === 'loaded' && !res.data.error,
      runtimeState: nextState,
      runtimeError: typeof res.data.error === 'string' ? res.data.error : '',
      reloadCount,
    }));
    setFormMessage({
      tone: 'ok',
      text: `${selectedPlugin.id} 热重载完成（state=${nextState} · reload_count=${reloadCount} · ${elapsedMs}ms）`,
    });
  };

  const handleVerifyContract = async () => {
    if (!selectedPlugin || busyAction) return;
    setBusyAction('verify');
    const res = await call<{
      manifest_ok?: boolean;
      lifecycle_ok?: boolean;
      tests_ok?: boolean;
      health?: { ok?: boolean };
    }>({
      op: 'plugin.verify',
      params: { plugin_id: selectedPlugin.id },
    });
    if (!mountedRef.current) return;
    setBusyAction(null);

    if (!res.available) {
      setOfflineMode(true);
      setFormMessage({ tone: 'err', text: '离线预览：当前未连接 Tauri 运行时，不可运行契约自检' });
      return;
    }
    if (!res.ok) {
      updatePlugin(selectedPlugin.id, (p) => ({
        ...p,
        verifyStatus: 'failed',
        healthOk: false,
        runtimeError: res.error.message,
      }));
      setFormMessage({ tone: 'err', text: `契约自检失败 ${formatRpcError(res.error)}` });
      return;
    }

    updatePlugin(selectedPlugin.id, (p) => ({
      ...p,
      verifyStatus: 'passed',
      healthOk: res.data.health?.ok !== false,
      runtimeError: '',
    }));
    setFormMessage({
      tone: 'ok',
      text: `${selectedPlugin.id} 契约自检通过（manifest=${String(res.data.manifest_ok ?? true)} · lifecycle=${String(res.data.lifecycle_ok ?? true)} · tests=${String(res.data.tests_ok ?? true)}）`,
    });
  };

  return {
    effectivePlugins,
    filteredPlugins,
    selectedPlugin,
    domainFilter,
    setDomainFilter,
    query,
    setQuery,
    setSelectedId,
    currentConfig,
    setConfigsByPlugin,
    revealedSecrets,
    setRevealedSecrets,
    offlineMode,
    backendBannerError,
    degradedNotice,
    busyAction,
    formMessage,
    setFormMessage,
    loadPluginList,
    handleSaveConfig,
    handleResetConfig,
    handleToggleInstall,
    handleToggleEnabled,
    handleHotReload,
    handleVerifyContract,
  };
}
