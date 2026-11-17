import { useCallback, useEffect, useMemo, useRef, useState, type ComponentType } from 'react';
import { AppShell } from './components/AppShell';
import { type HintOverlayTarget } from './components/HintOverlay';
import { OverlayLayer } from './components/OverlayLayer';
import type { VocabAddPayload } from './components/WorkbenchLayout';
import { useCoreEvents } from './hooks/useCoreEvents';
import { useGlobalKeys } from './hooks/useGlobalKeys';
import { useSpatialFocus } from './hooks/useSpatialFocus';
import { WORKBENCHES, WORKBENCH_MAP } from './theme';
import type { DockLayout, ThemeMode, WorkbenchId } from './types';
import {
  DEFAULT_FOCUS_IDS,
  SEARCH_FOCUS_IDS,
  resolveContextEntity,
} from './utils/contextEntity';
import { buildClampedHintTargets, scheduleFrame } from './utils/hintTargets';
import { ALT_SHORTCUT_MAP, blurActiveEditable } from './utils/shortcuts';
import {
  AiWorkbench,
  BookWorkbench,
  MediaWorkbench,
  PluginStore,
  TextWorkbench,
  type WorkbenchFocusProps,
} from './workbenches';

const WORKBENCH_COMPONENTS: Record<WorkbenchId, ComponentType<WorkbenchFocusProps>> = {
  media: MediaWorkbench,
  text: TextWorkbench,
  book: BookWorkbench,
  ai: AiWorkbench,
  store: PluginStore,
};

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

  const [hintTargets, setHintTargets] = useState<HintOverlayTarget[]>([]);
  const [sharedVocab, setSharedVocab] = useState<VocabAddPayload[]>([]);
  const [mediaToggleTrigger, setMediaToggleTrigger] = useState(0);
  const prevWorkbenchRef = useRef<WorkbenchId>(activeWorkbench);

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
    if (prevWorkbenchRef.current === activeWorkbench) return;
    prevWorkbenchRef.current = activeWorkbench;

    const restoreFocus = () => {
      const saved = lastFocusedRef.current[activeWorkbench];
      if (saved && focusById(saved, { scroll: false })) return;
      const preferred = DEFAULT_FOCUS_IDS[activeWorkbench];
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
    if (focusInput(SEARCH_FOCUS_IDS[activeWorkbench])) return;
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
    [activateById, focusById, onSelectWorkbench],
  );

  const handlePaletteCommand = useCallback(
    (commandId: string) => {
      if (commandId.startsWith('workbench:')) {
        const id = commandId.slice('workbench:'.length) as WorkbenchId;
        if (id in WORKBENCH_MAP) selectWorkbenchClean(id);
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
        'curate:sync-archive': () => selectWorkbenchClean('ai'),
        'curate:run-daily': () => selectWorkbenchClean('ai'),
        'plugin:verify-all': () => selectWorkbenchClean('store'),
      };
      dispatch[commandId]?.();
    },
    [
      handleCycleZone,
      handleTriggerHints,
      handleTriggerSearch,
      onOpenHelp,
      onSetTheme,
      onToggleDock,
      onToggleTheme,
      selectWorkbenchClean,
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
    () => resolveContextEntity(activeWorkbench, focusedId),
    [activeWorkbench, focusedId],
  );
  const activeMeta = WORKBENCH_MAP[activeWorkbench];

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
        focusedId={focusedId}
        isComposing={isComposing}
        contextEntity={activeContextEntity}
        tauriAvailable={tauriAvailable}
        coreOnline={coreOnline}
        bridgeStatus={bridgeStatus}
        statusError={statusError}
      >
        {WORKBENCHES.map((wb) => {
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
                sharedVocab={sharedVocab}
                onNavigateWorkbench={(wbId) => selectWorkbenchClean(wbId)}
                mediaToggleTrigger={wb.id === 'media' ? mediaToggleTrigger : undefined}
                tauriAvailable={tauriAvailable}
                coreOnline={coreOnline}
                runtimeStatus={runtime}
                pluginHealthMap={pluginHealthMap}
                lastBusEvent={lastBusEvent}
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
        pendingPrompt={pendingDockPrompt}
        onConsumePrompt={onConsumeDockPrompt}
        onOpenAiWorkbench={() => selectWorkbenchClean('ai')}
        onOpenPluginStore={() => selectWorkbenchClean('store')}
        enableGlobalShortcuts={false}
      />
    </>
  );
}
