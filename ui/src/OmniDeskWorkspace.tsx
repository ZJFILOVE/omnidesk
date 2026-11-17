import { playerShortcut, PLAYER_SHORTCUT_EVENT } from './utils/playerShortcuts';
import { useCallback, useEffect, useMemo, useRef, useState, type ComponentType } from 'react';
import { AppShell } from './components/AppShell';
import { AuthCenterPanel } from './components/AuthCenterPanel';
import { CurationCenterPanel } from './components/CurationCenterPanel';
import { type HintOverlayTarget } from './components/HintOverlay';
import { HostSettingsPanel } from './components/HostSettingsPanel';
import { JobCenterPanel } from './components/JobCenterPanel';
import { MiniPlayerBar } from './components/MiniPlayerBar';
import { OverlayLayer } from './components/OverlayLayer';
import type { VocabAddPayload, WorkbenchEntityContext } from './components/WorkbenchLayout';
import {
  call,
  getTauriGlobal,
  formatRpcError,
  listPlugins,
  submitJob,
} from './api';
import { useAuthCenter } from './hooks/useAuthCenter';
import { useCoreEvents } from './hooks/useCoreEvents';
import { useGlobalKeys } from './hooks/useGlobalKeys';
import { isActiveJob, useJobCenter } from './hooks/useJobCenter';
import { useSpatialFocus } from './hooks/useSpatialFocus';
import { useWorkspaceExtensions } from './hooks/useWorkspaceExtensions';
import {
  WORKBENCHES,
  assignPluginWorkbenches,
  pluginWorkbenchId,
  resolveWorkbenchMeta,
} from './theme';
import type {
  BuiltinWorkbenchId,
  DockLayout,
  ThemeMode,
  WorkbenchId,
  WorkbenchMeta,
} from './types';
import {
  defaultFocusIdFor,
  resolveContextEntity,
  searchFocusIdFor,
} from './utils/contextEntity';
import { buildClampedHintTargets, scheduleFrame } from './utils/hintTargets';
import { ALT_SHORTCUT_MAP, blurActiveEditable } from './utils/shortcuts';
import {
  AiWorkbench,
  BookWorkbench,
  MediaWorkbench,
  PluginStore,
  PluginWorkspace,
  TextWorkbench,
  type WorkbenchFocusProps,
} from './workbenches';
import { useMpvPlayback } from './workbenches/useMpvPlayback';

const WORKBENCH_COMPONENTS: Record<BuiltinWorkbenchId, ComponentType<WorkbenchFocusProps>> = {
  media: MediaWorkbench,
  text: TextWorkbench,
  book: BookWorkbench,
  ai: AiWorkbench,
  store: PluginStore,
};

const BUILTIN_WORKBENCHES = WORKBENCHES as readonly (WorkbenchMeta & {
  id: BuiltinWorkbenchId;
})[];

export interface WorkspaceControllerProps {
  activeWorkbench: WorkbenchId;
  onSelectWorkbench: (id: WorkbenchId) => void;
  theme: ThemeMode;
  onSetTheme: (mode: ThemeMode) => void;
  onToggleTheme: () => void;
  dockOpen: boolean;
  dockLayout: DockLayout;
  onOpenDock: (prompt?: string) => void;
  onToggleDock: () => void;
  onCloseDock: () => void;
  pendingDockPrompt: string | null;
  onConsumeDockPrompt: () => void;
  paletteOpen: boolean;
  onTogglePalette: () => void;
  onClosePalette: () => void;
  onOpenPalette: () => void;
  helpOpen: boolean;
  onToggleHelp: () => void;
  onCloseHelp: () => void;
  onOpenHelp: () => void;
  hintOpen: boolean;
  onSetHintOpen: (open: boolean) => void;
  lastFocusedRef: { current: Partial<Record<WorkbenchId, string>> };
}

export function OmniDeskWorkspace(props: WorkspaceControllerProps) {
  const {
    activeWorkbench, onSelectWorkbench, theme, onSetTheme, onToggleTheme,
    dockOpen, dockLayout, onOpenDock, onToggleDock, onCloseDock,
    pendingDockPrompt, onConsumeDockPrompt, paletteOpen, onTogglePalette,
    onClosePalette, onOpenPalette, helpOpen, onToggleHelp, onCloseHelp,
    onOpenHelp, hintOpen, onSetHintOpen, lastFocusedRef,
  } = props;
  const {
    focusedId, focusedZone, focusById, moveFocus, activateFocused,
    activateById, collectHintTargets, getFocusableIds,
  } = useSpatialFocus();

  const {
    tauriAvailable,
    runtime,
    coreOnline,
    bridgeStatus,
    pluginHealthMap,
    lastBusEvent,
    statusError,
  } = useCoreEvents();
  const jobCenter = useJobCenter(tauriAvailable);
  const authCenter = useAuthCenter(tauriAvailable);
  const mpv = useMpvPlayback();

  const {
    workspaceTabs,
    dockWidgets,
    loading: extensionsLoading,
    loaded: extensionsLoaded,
    refresh: refreshExtensions,
  } = useWorkspaceExtensions(lastBusEvent?.event_id ?? null);

  const [hintTargets, setHintTargets] = useState<HintOverlayTarget[]>([]);
  const [sharedVocab, setSharedVocab] = useState<VocabAddPayload[]>([]);
  const [mediaToggleTrigger, setMediaToggleTrigger] = useState(0);
  const [jobCenterOpen, setJobCenterOpen] = useState(false);
  const [authCenterOpen, setAuthCenterOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [curationOpen, setCurationOpen] = useState(false);
  const [commandNotice, setCommandNotice] = useState<string | null>(null);
  const [workbenchContexts, setWorkbenchContexts] = useState<Partial<Record<BuiltinWorkbenchId, WorkbenchEntityContext | null>>>({});
  const contextCallbacks = useMemo(() => Object.fromEntries(BUILTIN_WORKBENCHES.map((wb) => [
    wb.id, (context: WorkbenchEntityContext | null) => setWorkbenchContexts((previous) => ({ ...previous, [wb.id]: context })),
  ])) as Record<BuiltinWorkbenchId, (context: WorkbenchEntityContext | null) => void>, []);
  const prevWorkbenchRef = useRef<WorkbenchId>(activeWorkbench);
  const lastBuiltinRef = useRef<BuiltinWorkbenchId>('media');

  const pluginAssignments = useMemo(
    () => assignPluginWorkbenches(workspaceTabs, Object.keys(ALT_SHORTCUT_MAP)),
    [workspaceTabs],
  );
  const workbenchMetas = useMemo<WorkbenchMeta[]>(
    () => [...BUILTIN_WORKBENCHES, ...pluginAssignments.workbenches],
    [pluginAssignments.workbenches],
  );
  const activeMeta = useMemo(
    () => resolveWorkbenchMeta(activeWorkbench, pluginAssignments.workbenches),
    [activeWorkbench, pluginAssignments.workbenches],
  );
  const activePluginTab = useMemo(
    () => workspaceTabs.find((tab) => pluginWorkbenchId(tab) === activeWorkbench),
    [activeWorkbench, workspaceTabs],
  );

  const handleAddVocab = useCallback((entry: VocabAddPayload) => {
    setSharedVocab((prev) =>
      prev.some((v) => v.word.toLowerCase() === entry.word.toLowerCase()) ? prev : [entry, ...prev],
    );
  }, []);

  const selectWorkbenchClean = useCallback(
    (id: WorkbenchId) => {
      blurActiveEditable();
      onSelectWorkbench(id);
    },
    [onSelectWorkbench],
  );

  useEffect(() => {
    if (focusedId && focusedZone === 'main') {
      lastFocusedRef.current[activeWorkbench] = focusedId;
    }
  }, [activeWorkbench, focusedId, focusedZone, lastFocusedRef]);

  useEffect(() => {
    if (!activeWorkbench.startsWith('plugin:')) {
      lastBuiltinRef.current = activeWorkbench as BuiltinWorkbenchId;
      return;
    }
    if (!extensionsLoaded || extensionsLoading || activePluginTab) return;
    onSelectWorkbench(lastBuiltinRef.current);
  }, [activePluginTab, activeWorkbench, extensionsLoaded, extensionsLoading, onSelectWorkbench]);

  useEffect(() => {
    if (prevWorkbenchRef.current === activeWorkbench) return;
    prevWorkbenchRef.current = activeWorkbench;

    const restoreFocus = () => {
      const saved = lastFocusedRef.current[activeWorkbench];
      if (saved && focusById(saved, { scroll: false })) return;
      const preferred = defaultFocusIdFor(activeWorkbench);
      if (preferred && focusById(preferred, { scroll: false })) return;
      const mainIds = getFocusableIds(['main']);
      if (mainIds[0] && focusById(mainIds[0], { scroll: false })) return;
      focusById(`nav-workbench-${activeWorkbench}`, { scroll: false });
    };

    return scheduleFrame(restoreFocus);
  }, [activeWorkbench, focusById, getFocusableIds, lastFocusedRef]);

  const openHintsNow = useCallback(() => {
    const rawTargets = collectHintTargets(dockOpen ? ['nav', 'main', 'dock'] : ['nav', 'main']);
    if (rawTargets.length === 0) return;
    setHintTargets(buildClampedHintTargets(rawTargets));
    onSetHintOpen(true);
  }, [collectHintTargets, dockOpen, onSetHintOpen]);

  const handleTriggerHints = useCallback(() => {
    if (paletteOpen) {
      onClosePalette();
      scheduleFrame(openHintsNow);
      return;
    }
    openHintsNow();
  }, [onClosePalette, openHintsNow, paletteOpen]);

  const handleCloseHints = useCallback(() => {
    onSetHintOpen(false);
    setHintTargets([]);
  }, [onSetHintOpen]);

  const handlePickHint = useCallback(
    (id: string) => {
      onSetHintOpen(false);
      setHintTargets([]);
      focusById(id, { scroll: true });
      activateById(id);
    },
    [activateById, focusById, onSetHintOpen],
  );

  const handleTriggerSearch = useCallback(() => {
    const focusInput = (id: string): boolean => {
      if (!focusById(id, { scroll: true })) return false;
      const el = document.querySelector<HTMLElement>(`[data-focus-id="${id}"]`);
      if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) el.focus();
      return true;
    };
    if (focusInput(searchFocusIdFor(activeWorkbench))) return;
    if (activeWorkbench === 'book') {
      document.querySelector<HTMLElement>('[data-focus-id="book:tab:search"]')?.click();
      scheduleFrame(() => focusInput('book:catalog:search'));
    }
  }, [activeWorkbench, focusById]);

  const handleCycleZone = useCallback(() => {
    const dockEl =
      typeof document !== 'undefined'
        ? document.querySelector<HTMLElement>('[data-testid="companion-dock"]')
        : null;
    const activeEl = typeof document !== 'undefined' ? document.activeElement : null;
    const inDock =
      Boolean(dockEl && activeEl && dockEl.contains(activeEl)) || focusedZone === 'dock';

    if (inDock) {
      blurActiveEditable();
      focusById(`nav-workbench-${activeWorkbench}`, { scroll: true });
      return;
    }

    if (focusedZone === 'nav') {
      blurActiveEditable();
      const saved = lastFocusedRef.current[activeWorkbench];
      if (saved && focusById(saved, { scroll: true })) return;
      const mainIds = getFocusableIds(['main']);
      if (mainIds[0]) focusById(mainIds[0], { scroll: true });
      return;
    }

    if (dockOpen) {
      blurActiveEditable();
      const dockTextarea = document.querySelector<HTMLElement>(
        '[data-testid="companion-dock"] textarea',
      );
      if (dockTextarea) {
        dockTextarea.focus();
        return;
      }
    }

    blurActiveEditable();
    focusById(`nav-workbench-${activeWorkbench}`, { scroll: true });
  }, [activeWorkbench, dockOpen, focusById, focusedZone, getFocusableIds, lastFocusedRef]);

  const handleSpace = useCallback(() => {
    if (activeWorkbench === 'media') {
      setMediaToggleTrigger((n) => n + 1);
      return;
    }
    activateFocused();
  }, [activateFocused, activeWorkbench]);

  const handleAltShortcut = useCallback(
    (letter: string): boolean => {
      const pluginTarget = pluginAssignments.shortcutMap[letter];
      if (pluginTarget) {
        blurActiveEditable();
        onSelectWorkbench(pluginTarget);
        return true;
      }
      const target = ALT_SHORTCUT_MAP[letter];
      if (!target) return false;
      blurActiveEditable();
      onSelectWorkbench(target.workbench);
      if (target.focusId) {
        scheduleFrame(() => {
          if (!activateById(target.focusId!)) {
            focusById(target.focusId!, { scroll: true });
          }
        });
      }
      return true;
    },
    [activateById, focusById, onSelectWorkbench, pluginAssignments.shortcutMap],
  );

  const handleSyncArchive = useCallback(async () => {
    if (!tauriAvailable) {
      setCommandNotice('离线预览不能同步双仓归档');
      return;
    }
    const result = await submitJob({
      kind: 'curation.sync_archive',
      capability: 'recommend.feed.v1',
      pluginId: 'plugin-ai-curator',
      title: '同步 obsidian-vault & music-daily 历史归档',
      payload: {
        action: 'bootstrap_and_rehydrate',
        pull_remote: true,
        clone_if_missing: true,
      },
      metadata: { source: 'command-palette' },
    });
    if (!result.ok) {
      setCommandNotice(`归档同步提交失败：${formatRpcError(result.error)}`);
      return;
    }
    setCommandNotice(`已提交后台归档同步任务 ${result.data.job.id.slice(0, 10)}`);
    setJobCenterOpen(true);
    await jobCenter.refresh();
  }, [jobCenter.refresh, tauriAvailable]);

  const handleVerifyAllPlugins = useCallback(async () => {
    if (!tauriAvailable) {
      setCommandNotice('离线预览不能执行插件契约巡检');
      return;
    }
    const listed = await listPlugins({ liveOnly: true });
    if (!listed.ok) {
      setCommandNotice(`读取插件目录失败：${formatRpcError(listed.error)}`);
      return;
    }
    const plugins = listed.data.plugins;
    setCommandNotice(`正在巡检 ${plugins.length} 个插件的能力契约…`);
    const failures: string[] = [];
    for (const plugin of plugins) {
      const verified = await call({
        op: 'plugin.verify',
        params: { plugin_id: plugin.plugin_id },
      });
      if (!verified.ok) failures.push(`${plugin.plugin_id}: ${verified.error.message}`);
    }
    setCommandNotice(
      failures.length === 0
        ? `插件巡检完成：${plugins.length}/${plugins.length} 通过`
        : `插件巡检完成：${plugins.length - failures.length}/${plugins.length} 通过 · ${failures[0]}`,
    );
  }, [tauriAvailable]);

  const handlePaletteCommand = useCallback(
    (commandId: string) => {
      if (commandId.startsWith('workbench:')) {
        const id = commandId.slice('workbench:'.length) as WorkbenchId;
        if (workbenchMetas.some((meta) => meta.id === id)) selectWorkbenchClean(id);
        return;
      }
      if (commandId.startsWith('fts:')) {
        const [, domain] = commandId.split(':');
        if (domain === 'recommend') {
          setCurationOpen(true);
          setCommandNotice('已打开跨域检索结果所属的推荐归档日历');
          return;
        }
        const target: WorkbenchId =
          domain === 'media'
            ? 'media'
            : domain === 'book'
              ? 'book'
              : domain === 'text'
                ? 'text'
                : 'ai';
        selectWorkbenchClean(target);
        setCommandNotice(`已打开跨域检索结果所属工作台：${target}`);
        return;
      }
      const dispatch: Record<string, () => void> = {
        'theme:toggle': onToggleTheme,
        'theme:paper': () => onSetTheme('paper'),
        'theme:graphite': () => onSetTheme('graphite'),
        'dock:toggle': onToggleDock,
        'hints:open': handleTriggerHints,
        'help:open': onOpenHelp,
        'search:focus': handleTriggerSearch,
        'focus:cycle-zone': handleCycleZone,
        'curate:sync-archive': () => {
          void handleSyncArchive();
        },
        'curate:run-daily': () => setCurationOpen(true),
        'curate:calendar': () => setCurationOpen(true),
        'plugin:verify-all': () => {
          void handleVerifyAllPlugins();
        },
        'auth:center': () => setAuthCenterOpen(true),
        'settings:open': () => setSettingsOpen(true),
        'jobs:open': () => setJobCenterOpen(true),
      };
      dispatch[commandId]?.();
    },
    [
      handleCycleZone,
      handleSyncArchive,
      handleTriggerHints,
      handleVerifyAllPlugins,
      handleTriggerSearch,
      onOpenHelp,
      onSetTheme,
      onToggleDock,
      onToggleTheme,
      selectWorkbenchClean,
      workbenchMetas,
    ],
  );

  const { isComposing } = useGlobalKeys({
    paletteOpen,
    hintActive: hintOpen,
    helpOpen,
    onSelectWorkbench: selectWorkbenchClean,
    onTogglePalette,
    onClosePalette,
    onToggleDock,
    onToggleTheme,
    onToggleHelp,
    onCloseHelp,
    onTriggerHints: handleTriggerHints,
    onCancelHints: handleCloseHints,
    onMove: moveFocus,
    onActivate: activateFocused,
    onSpace: handleSpace,
    onPlayerKey: (event) => {
      if (activeWorkbench !== 'media' || settingsOpen || authCenterOpen || jobCenterOpen || curationOpen) return false;
      if (event.target instanceof Element && event.target.closest('[data-focus-zone="dock"], [role="dialog"]')) return false;
      if (event.key === 'F11') {
        if (!event.repeat) void Promise.resolve().then(() => {
          const invoke = getTauriGlobal()?.core?.invoke;
          if (invoke) return invoke('player_fullscreen');
          if (document.fullscreenElement) return document.exitFullscreen?.();
          return document.documentElement.requestFullscreen?.();
        }).catch((error) => setCommandNotice(`全屏失败：${String(error)}`));
        return true;
      }
      const action = playerShortcut(event);
      if (!action) return false;
      if (!event.repeat || ['back', 'forward', 'quieter', 'louder'].includes(action)) {
        window.dispatchEvent(new CustomEvent(PLAYER_SHORTCUT_EVENT, { detail: action }));
      }
      return true;
    },
    onCycleZone: handleCycleZone,
    onAltShortcut: handleAltShortcut,
    onTriggerSearch: handleTriggerSearch,
  });

  const handleWorkbenchFocus = useCallback(
    (id: string) => {
      focusById(id, { scroll: false });
    },
    [focusById],
  );

  const activeContextEntity = useMemo(
    () => workbenchContexts[activeWorkbench as BuiltinWorkbenchId] ?? resolveContextEntity(activeWorkbench, focusedId, activeMeta),
    [activeMeta, activeWorkbench, focusedId, workbenchContexts],
  );
  const authSummary = useMemo(
    () => ({
      authenticated: authCenter.providers.filter((entry) => entry.kind === 'authenticated').length,
      warnings: authCenter.providers.filter(
        (entry) => entry.kind === 'guest_cookie_only' || entry.kind === 'optional_guest',
      ).length,
      missing: authCenter.providers.filter((entry) => entry.kind === 'missing_required').length,
    }),
    [authCenter.providers],
  );
  const activeJobCount = useMemo(
    () => jobCenter.jobs.filter(isActiveJob).length,
    [jobCenter.jobs],
  );

  return (
    <>
      <AppShell
        activeWorkbench={activeWorkbench}
        onSelectWorkbench={selectWorkbenchClean}
        theme={theme}
        onToggleTheme={onToggleTheme}
        dockOpen={dockOpen}
        dockLayout={dockLayout}
        onToggleDock={onToggleDock}
        onOpenPalette={onOpenPalette}
        onTriggerHints={handleTriggerHints}
        onToggleHelp={onToggleHelp}
        onOpenAuth={() => setAuthCenterOpen(true)}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenJobs={() => setJobCenterOpen(true)}
        onOpenCuration={() => setCurationOpen(true)}
        activeJobCount={activeJobCount}
        focusedId={focusedId}
        isComposing={isComposing}
        contextEntity={activeContextEntity}
        workbenchMetas={workbenchMetas}
        tauriAvailable={tauriAvailable}
        coreOnline={coreOnline}
        bridgeStatus={bridgeStatus}
        statusError={statusError}
        notice={commandNotice}
        onDismissNotice={() => setCommandNotice(null)}
        miniPlayerSlot={
          <MiniPlayerBar
            controller={mpv}
            onOpenMedia={() => selectWorkbenchClean('media')}
          />
        }
      >
        {BUILTIN_WORKBENCHES.map((wb) => {
          const Panel = WORKBENCH_COMPONENTS[wb.id];
          const isCurrent = activeWorkbench === wb.id;
          return (
            <div
              key={wb.id}
              hidden={!isCurrent}
              aria-hidden={!isCurrent}
              style={{ display: isCurrent ? 'flex' : 'none' }}
              className="h-full w-full flex-1 min-h-0 flex-col"
            >
              <Panel
                isActive={isCurrent}
                theme={theme}
                focusId={isCurrent ? (focusedId ?? undefined) : undefined}
                onFocus={isCurrent ? handleWorkbenchFocus : undefined}
                onOpenDock={isCurrent ? onOpenDock : undefined}
                onAddVocab={handleAddVocab}
                onContextChange={contextCallbacks[wb.id]}
                sharedVocab={sharedVocab}
                onNavigateWorkbench={(wbId) => selectWorkbenchClean(wbId)}
                onOpenPluginSettings={(pluginId) => {
                  const tab = workspaceTabs.find((entry) => entry.plugin_id === pluginId);
                  if (tab) {
                    selectWorkbenchClean(pluginWorkbenchId(tab));
                  }
                }}
                mediaToggleTrigger={wb.id === 'media' ? mediaToggleTrigger : undefined}
                tauriAvailable={tauriAvailable}
                coreOnline={coreOnline}
                runtimeStatus={runtime}
                pluginHealthMap={pluginHealthMap}
                lastBusEvent={lastBusEvent}
                mpvController={mpv}
                onOpenCuration={() => setCurationOpen(true)}
              />
            </div>
          );
        })}
        {workspaceTabs.map((tab) => {
          const tabWorkbenchId = pluginWorkbenchId(tab);
          const isCurrent = activeWorkbench === tabWorkbenchId;
          return (
            <div
              key={tabWorkbenchId}
              hidden={!isCurrent}
              aria-hidden={!isCurrent}
              style={{ display: isCurrent ? 'flex' : 'none' }}
              className="h-full w-full flex-1 min-h-0 flex-col"
            >
              <PluginWorkspace
                tab={tab}
                isActive={isCurrent}
                webviewVisible={!paletteOpen && !helpOpen && !hintOpen && !settingsOpen && !authCenterOpen && !jobCenterOpen && !curationOpen && !(dockOpen && dockLayout === 'bottom')}
                tauriAvailable={tauriAvailable}
                onRefreshExtensions={() => {
                  void refreshExtensions();
                }}
              />
            </div>
          );
        })}
      </AppShell>

      <OverlayLayer
        commandPaletteOpen={paletteOpen}
        onCloseCommandPalette={onClosePalette}
        onToggleCommandPalette={onTogglePalette}
        onCommand={handlePaletteCommand}
        companionOpen={dockOpen}
        onCloseCompanion={onCloseDock}
        onToggleCompanion={onToggleDock}
        keyboardHelpOpen={helpOpen}
        onCloseKeyboardHelp={onCloseHelp}
        onToggleKeyboardHelp={onToggleHelp}
        hintsOpen={hintOpen}
        hintTargets={hintTargets}
        onPickHint={handlePickHint}
        onCloseHints={handleCloseHints}
        onToggleHints={handleTriggerHints}
        activeWorkbench={activeWorkbench}
        theme={theme}
        onToggleTheme={onToggleTheme}
        dockLayout={dockLayout}
        dockOpen={dockOpen}
        domain={activeMeta.domain}
        contextEntity={activeContextEntity}
        dockWidgets={dockWidgets}
        pendingPrompt={pendingDockPrompt}
        onConsumePrompt={onConsumeDockPrompt}
        onRefreshExtensions={() => {
          void refreshExtensions();
        }}
        onOpenAiWorkbench={() => selectWorkbenchClean('ai')}
        onOpenPluginStore={() => selectWorkbenchClean('store')}
        workbenchMetas={workbenchMetas}
        enableGlobalShortcuts={false}
      />
      <JobCenterPanel
        open={jobCenterOpen}
        onClose={() => setJobCenterOpen(false)}
        state={jobCenter}
      />
      <AuthCenterPanel
        open={authCenterOpen}
        onClose={() => setAuthCenterOpen(false)}
        state={authCenter}
      />
      <CurationCenterPanel
        open={curationOpen}
        onClose={() => setCurationOpen(false)}
        onNavigateWorkbench={(id, payload) => {
          selectWorkbenchClean(id);
          setCommandNotice(
            `已从推荐归档打开 ${id}${payload && 'title' in payload ? ` · ${String(payload.title ?? '')}` : ''}`,
          );
        }}
      />
      <HostSettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        theme={theme}
        runtime={runtime}
        coreOnline={coreOnline}
        bridgeStatus={bridgeStatus}
        authSummary={authSummary}
        onOpenAuth={() => {
          setSettingsOpen(false);
          setAuthCenterOpen(true);
        }}
        onOpenCuration={() => {
          setSettingsOpen(false);
          setCurationOpen(true);
        }}
        onOpenJobs={() => {
          setSettingsOpen(false);
          setJobCenterOpen(true);
        }}
      />
    </>
  );
}
