import { useCallback, useEffect, useRef, useState } from 'react';
import { OmniDeskWorkspace } from './OmniDeskWorkspace';
import { SpatialFocusProvider } from './hooks/useSpatialFocus';
import {
  applyTheme,
  getInitialTheme,
  nextTheme,
  resolveDockLayout,
} from './theme';
import type { DockLayout, FocusZone, ThemeMode, WorkbenchId } from './types';
import { DEFAULT_FOCUS_IDS } from './utils/contextEntity';
import { blurActiveEditable } from './utils/shortcuts';

const NAV_MAIN_ZONES: readonly FocusZone[] = ['nav', 'main'];
const NAV_MAIN_DOCK_ZONES: readonly FocusZone[] = ['nav', 'main', 'dock'];
const OVERLAY_ZONES: readonly FocusZone[] = ['overlay'];

export function App() {
  const [theme, setTheme] = useState<ThemeMode>(() => getInitialTheme());
  const [activeWorkbench, setActiveWorkbench] = useState<WorkbenchId>('media');
  const [dockOpen, setDockOpen] = useState(false);
  const [pendingDockPrompt, setPendingDockPrompt] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [hintOpen, setHintOpen] = useState(false);
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
  const handleOpenDock = useCallback((prompt?: string) => {
    if (prompt && prompt.trim()) setPendingDockPrompt(prompt.trim());
    setDockOpen(true);
  }, []);
  const handleConsumeDockPrompt = useCallback(() => setPendingDockPrompt(null), []);
  const handleToggleDock = useCallback(() => {
    setDockOpen((prev) => {
      if (prev) blurActiveEditable();
      return !prev;
    });
  }, []);
  const handleCloseDock = useCallback(() => {
    blurActiveEditable();
    setDockOpen(false);
  }, []);
  const handleTogglePalette = useCallback(() => setPaletteOpen((prev) => !prev), []);
  const handleOpenPalette = useCallback(() => setPaletteOpen(true), []);
  const handleClosePalette = useCallback(() => setPaletteOpen(false), []);
  const handleToggleHelp = useCallback(() => setHelpOpen((prev) => !prev), []);
  const handleOpenHelp = useCallback(() => setHelpOpen(true), []);
  const handleCloseHelp = useCallback(() => setHelpOpen(false), []);

  const allowedZones =
    paletteOpen || helpOpen || hintOpen
      ? OVERLAY_ZONES
      : dockOpen
        ? NAV_MAIN_DOCK_ZONES
        : NAV_MAIN_ZONES;
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
        onOpenDock={handleOpenDock}
        onToggleDock={handleToggleDock}
        onCloseDock={handleCloseDock}
        pendingDockPrompt={pendingDockPrompt}
        onConsumeDockPrompt={handleConsumeDockPrompt}
        paletteOpen={paletteOpen}
        onTogglePalette={handleTogglePalette}
        onClosePalette={handleClosePalette}
        onOpenPalette={handleOpenPalette}
        helpOpen={helpOpen}
        onToggleHelp={handleToggleHelp}
        onCloseHelp={handleCloseHelp}
        onOpenHelp={handleOpenHelp}
        hintOpen={hintOpen}
        onSetHintOpen={setHintOpen}
        lastFocusedRef={lastFocusedRef}
      />
    </SpatialFocusProvider>
  );
}

export default App;
