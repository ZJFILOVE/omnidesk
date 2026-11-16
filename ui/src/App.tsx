import { useCallback, useEffect, useMemo, useRef, useState, type ComponentType } from 'react';
import { AppShell } from './components/AppShell';
import { CommandPalette } from './components/CommandPalette';
import { CompanionDock } from './components/CompanionDock';
import { HintOverlay, type HintOverlayTarget } from './components/HintOverlay';
import { BOOK_ITEMS, INITIAL_PLUGINS, MEDIA_ITEMS, TEXT_ARTICLES } from './data/mock';
import { useGlobalKeys } from './hooks/useGlobalKeys';
import { SpatialFocusProvider, useSpatialFocus } from './hooks/useSpatialFocus';
import {
  WORKBENCHES,
  WORKBENCH_MAP,
  applyTheme,
  getInitialTheme,
  nextTheme,
  resolveDockLayout,
} from './theme';
import type { ActiveEntityContext, DockLayout, FocusZone, ThemeMode, WorkbenchId } from './types';
import {
  AiWorkbench,
  BookWorkbench,
  MediaWorkbench,
  PluginStore,
  TextWorkbench,
  type WorkbenchFocusProps,
} from './workbenches';

const NAV_MAIN_ZONES: readonly FocusZone[] = ['nav', 'main'];
const NAV_MAIN_DOCK_ZONES: readonly FocusZone[] = ['nav', 'main', 'dock'];
const OVERLAY_ZONES: readonly FocusZone[] = ['overlay'];

const WORKBENCH_COMPONENTS: Record<WorkbenchId, ComponentType<WorkbenchFocusProps>> = {
  media: MediaWorkbench,
  text: TextWorkbench,
  book: BookWorkbench,
  ai: AiWorkbench,
  store: PluginStore,
};

const DEFAULT_FOCUS_IDS: Record<WorkbenchId, string> = {
  media: `media:card:${MEDIA_ITEMS[0]?.id ?? ''}`,
  text: `text:article:${TEXT_ARTICLES[0]?.id ?? ''}`,
  book: `book:shelf:${BOOK_ITEMS[0]?.id ?? ''}`,
  ai: 'ai:composer:input',
  store: `store:plugin:${INITIAL_PLUGINS[0]?.id ?? ''}`,
};

const SEARCH_FOCUS_IDS: Record<WorkbenchId, string> = {
  media: 'media:search',
  text: 'text:search',
  book: 'book:catalog:search',
  ai: 'ai:composer:input',
  store: 'store:search',
};

function resolveContextEntity(workbench: WorkbenchId, focusedId: string | null): ActiveEntityContext {
  const meta = WORKBENCH_MAP[workbench];
  if (focusedId?.startsWith('media:card:')) {
    const item = MEDIA_ITEMS.find((m) => `media:card:${m.id}` === focusedId);
    if (item) return { domain: 'media', workbench, itemId: item.id, title: item.title, subtitle: item.subtitle, sourceId: item.sourceId, kind: item.kind };
  }
  if (focusedId?.startsWith('text:article:')) {
    const article = TEXT_ARTICLES.find((a) => `text:article:${a.id}` === focusedId);
    if (article) return { domain: 'text', workbench, itemId: article.id, title: article.titleZh || article.title, subtitle: article.sourceName };
  }
  if (focusedId?.startsWith('book:shelf:')) {
    const book = BOOK_ITEMS.find((b) => `book:shelf:${b.id}` === focusedId);
    if (book) return { domain: 'book', workbench, itemId: book.id, title: book.title, subtitle: book.author };
  }
  if (focusedId?.startsWith('store:plugin:')) {
    const plugin = INITIAL_PLUGINS.find((p) => `store:plugin:${p.id}` === focusedId);
    if (plugin) return { domain: plugin.domain, workbench, itemId: plugin.id, title: plugin.name, subtitle: plugin.description };
  }
  return { domain: meta.domain, workbench, itemId: focusedId ?? meta.id, title: meta.label };
}

interface WorkspaceControllerProps {
  activeWorkbench: WorkbenchId;
  onSelectWorkbench: (id: WorkbenchId) => void;
  theme: ThemeMode;
  onSetTheme: (mode: ThemeMode) => void;
  onToggleTheme: () => void;
  dockOpen: boolean;
  dockLayout: DockLayout;
  onToggleDock: () => void;
  onCloseDock: () => void;
  paletteOpen: boolean;
  onTogglePalette: () => void;
  onClosePalette: () => void;
  onOpenPalette: () => void;
  lastFocusedRef: { current: Partial<Record<WorkbenchId, string>> };
}

function OmniDeskWorkspace({
  activeWorkbench,
  onSelectWorkbench,
  theme,
  onSetTheme,
  onToggleTheme,
  dockOpen,
  dockLayout,
  onToggleDock,
  onCloseDock,
  paletteOpen,
  onTogglePalette,
  onClosePalette,
  onOpenPalette,
  lastFocusedRef,
}: WorkspaceControllerProps) {
  const { focusedId, focusedZone, focusById, moveFocus, activateFocused, activateById, collectHintTargets, getFocusableIds } =
    useSpatialFocus();

  const [hintOpen, setHintOpen] = useState(false);
  const [hintTargets, setHintTargets] = useState<HintOverlayTarget[]>([]);
  const prevWorkbenchRef = useRef<WorkbenchId>(activeWorkbench);

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

    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      const raf = window.requestAnimationFrame(restoreFocus);
      return () => window.cancelAnimationFrame(raf);
    }
    restoreFocus();
    return undefined;
  }, [activeWorkbench, focusById, getFocusableIds, lastFocusedRef]);

  const openHintsNow = useCallback(() => {
    const rawTargets = collectHintTargets(dockOpen ? ['nav', 'main', 'dock'] : ['nav', 'main']);
    if (rawTargets.length === 0) return;
    const maxW = typeof window !== 'undefined' && window.innerWidth > 0 ? window.innerWidth : 1280;
    const maxH = typeof window !== 'undefined' && window.innerHeight > 0 ? window.innerHeight : 800;
    setHintTargets(
      rawTargets.map((t) => ({
        id: t.id,
        label: t.label ?? '',
        x: Math.max(24, Math.min(maxW - 24, t.rect.left + Math.min(28, t.rect.width * 0.5))),
        y: Math.max(20, Math.min(maxH - 20, t.rect.top + Math.min(18, t.rect.height * 0.5))),
      })),
    );
    setHintOpen(true);
  }, [collectHintTargets, dockOpen]);

  const handleTriggerHints = useCallback(() => {
    if (paletteOpen) {
      onClosePalette();
      if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(openHintsNow);
        return;
      }
    }
    openHintsNow();
  }, [onClosePalette, openHintsNow, paletteOpen]);

  const handleCloseHints = useCallback(() => {
    setHintOpen(false);
    setHintTargets([]);
  }, []);

  const handlePickHint = useCallback(
    (id: string) => {
      setHintOpen(false);
      setHintTargets([]);
      focusById(id, { scroll: true });
      activateById(id);
    },
    [activateById, focusById],
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
      if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(() => focusInput('book:catalog:search'));
      }
    }
  }, [activeWorkbench, focusById]);

  const handleCycleZone = useCallback(() => {
    if (focusedZone === 'nav') {
      const mainIds = getFocusableIds(['main']);
      if (mainIds[0]) focusById(mainIds[0], { scroll: true });
      return;
    }
    if (focusedZone === 'main' && dockOpen) {
      const dockTextarea = document.querySelector<HTMLElement>('[data-testid="companion-dock"] textarea');
      if (dockTextarea) {
        dockTextarea.focus();
        return;
      }
    }
    focusById(`nav-workbench-${activeWorkbench}`, { scroll: true });
  }, [activeWorkbench, dockOpen, focusById, focusedZone, getFocusableIds]);

  const handlePaletteCommand = useCallback(
    (commandId: string) => {
      if (commandId.startsWith('workbench:')) {
        const id = commandId.slice('workbench:'.length) as WorkbenchId;
        if (id in WORKBENCH_MAP) onSelectWorkbench(id);
        return;
      }
      const dispatch: Record<string, () => void> = {
        'theme:toggle': onToggleTheme,
        'theme:paper': () => onSetTheme('paper'),
        'theme:graphite': () => onSetTheme('graphite'),
        'dock:toggle': onToggleDock,
        'hints:open': handleTriggerHints,
        'search:focus': handleTriggerSearch,
        'focus:cycle-zone': handleCycleZone,
        'curate:sync-archive': () => onSelectWorkbench('ai'),
        'curate:run-daily': () => onSelectWorkbench('ai'),
        'plugin:verify-all': () => onSelectWorkbench('store'),
      };
      dispatch[commandId]?.();
    },
    [handleCycleZone, handleTriggerHints, handleTriggerSearch, onSelectWorkbench, onSetTheme, onToggleDock, onToggleTheme],
  );

  const { isComposing } = useGlobalKeys({
    paletteOpen,
    hintActive: hintOpen,
    onSelectWorkbench,
    onTogglePalette,
    onClosePalette,
    onToggleDock,
    onToggleTheme,
    onTriggerHints: handleTriggerHints,
    onCancelHints: handleCloseHints,
    onMove: moveFocus,
    onActivate: activateFocused,
    onTriggerSearch: handleTriggerSearch,
  });

  const handleWorkbenchFocus = useCallback((id: string) => {
    focusById(id, { scroll: false });
  }, [focusById]);

  const activeContextEntity = useMemo(
    () => resolveContextEntity(activeWorkbench, focusedId),
    [activeWorkbench, focusedId],
  );
  const activeMeta = WORKBENCH_MAP[activeWorkbench];

  return (
    <>
      <AppShell
        activeWorkbench={activeWorkbench}
        onSelectWorkbench={onSelectWorkbench}
        theme={theme}
        onToggleTheme={onToggleTheme}
        dockOpen={dockOpen}
        dockLayout={dockLayout}
        onToggleDock={onToggleDock}
        onOpenPalette={onOpenPalette}
        onTriggerHints={handleTriggerHints}
        focusedId={focusedId}
        isComposing={isComposing}
        contextEntity={activeContextEntity}
        dockSlot={
          <CompanionDock
            open={dockOpen}
            onClose={onCloseDock}
            layout={dockLayout}
            activeWorkbench={activeWorkbench}
            domain={activeMeta.domain}
            contextEntity={activeContextEntity}
            onOpenAiWorkbench={() => onSelectWorkbench('ai')}
            onOpenPluginStore={() => onSelectWorkbench('store')}
          />
        }
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
                focusId={isCurrent ? (focusedId ?? undefined) : undefined}
                onFocus={isCurrent ? handleWorkbenchFocus : undefined}
              />
            </div>
          );
        })}
      </AppShell>

      <CommandPalette
        open={paletteOpen}
        onClose={onClosePalette}
        onCommand={handlePaletteCommand}
        activeWorkbench={activeWorkbench}
        theme={theme}
        dockOpen={dockOpen}
      />

      <HintOverlay open={hintOpen} targets={hintTargets} onPick={handlePickHint} onClose={handleCloseHints} />
    </>
  );
}

export function App() {
  const [theme, setTheme] = useState<ThemeMode>(() => getInitialTheme());
  const [activeWorkbench, setActiveWorkbench] = useState<WorkbenchId>('media');
  const [dockOpen, setDockOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [dockLayout, setDockLayout] = useState<DockLayout>(() =>
    typeof window !== 'undefined' ? resolveDockLayout(window.innerWidth, window.innerHeight) : 'side',
  );

  const lastFocusedRef = useRef<Partial<Record<WorkbenchId, string>>>({ ...DEFAULT_FOCUS_IDS });

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const handleResize = () => setDockLayout(resolveDockLayout(window.innerWidth, window.innerHeight));
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  const handleToggleTheme = useCallback(() => setTheme((prev) => nextTheme(prev)), []);
  const handleToggleDock = useCallback(() => setDockOpen((prev) => !prev), []);
  const handleCloseDock = useCallback(() => setDockOpen(false), []);
  const handleTogglePalette = useCallback(() => setPaletteOpen((prev) => !prev), []);
  const handleOpenPalette = useCallback(() => setPaletteOpen(true), []);
  const handleClosePalette = useCallback(() => setPaletteOpen(false), []);

  const allowedZones = paletteOpen ? OVERLAY_ZONES : dockOpen ? NAV_MAIN_DOCK_ZONES : NAV_MAIN_ZONES;
  const preferredId = lastFocusedRef.current[activeWorkbench] ?? DEFAULT_FOCUS_IDS[activeWorkbench] ?? null;

  return (
    <SpatialFocusProvider allowedZones={allowedZones} scopeKey={activeWorkbench} preferredId={preferredId}>
      <OmniDeskWorkspace
        activeWorkbench={activeWorkbench}
        onSelectWorkbench={setActiveWorkbench}
        theme={theme}
        onSetTheme={setTheme}
        onToggleTheme={handleToggleTheme}
        dockOpen={dockOpen}
        dockLayout={dockLayout}
        onToggleDock={handleToggleDock}
        onCloseDock={handleCloseDock}
        paletteOpen={paletteOpen}
        onTogglePalette={handleTogglePalette}
        onClosePalette={handleClosePalette}
        onOpenPalette={handleOpenPalette}
        lastFocusedRef={lastFocusedRef}
      />
    </SpatialFocusProvider>
  );
}

export default App;
