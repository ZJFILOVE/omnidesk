import { useCallback, useEffect, useMemo, useState } from 'react';
import { isEditableTarget, useImeGuard } from '../hooks/useImeGuard';
import { applyTheme, getInitialTheme, nextTheme } from '../theme';
import type {
  ActiveEntityContext,
  ContentDomain,
  DockLayout,
  ThemeMode,
  WorkbenchId,
} from '../types';
import { CommandPalette, type CommandPaletteItem } from './CommandPalette';
import { CompanionDock } from './CompanionDock';
import { HintOverlay, type HintOverlayTarget } from './HintOverlay';
import { KeyboardHelp } from './KeyboardHelp';

export interface OverlayLayerProps {
  commandPaletteOpen?: boolean;
  onCloseCommandPalette?: () => void;
  onToggleCommandPalette?: () => void;
  onCommand?: (commandId: string) => void;

  companionOpen?: boolean;
  onCloseCompanion?: () => void;
  onToggleCompanion?: () => void;

  keyboardHelpOpen?: boolean;
  onCloseKeyboardHelp?: () => void;
  onToggleKeyboardHelp?: () => void;

  hintsOpen?: boolean;
  hintTargets?: HintOverlayTarget[];
  onPickHint?: (id: string) => void;
  onCloseHints?: () => void;
  onToggleHints?: () => void;

  activeWorkbench?: WorkbenchId;
  theme?: ThemeMode;
  onToggleTheme?: () => void;
  dockLayout?: DockLayout;
  dockOpen?: boolean;
  domain?: ContentDomain;
  contextEntity?: ActiveEntityContext | null;
  pendingPrompt?: string | null;
  onConsumePrompt?: () => void;
  onOpenAiWorkbench?: () => void;
  onOpenPluginStore?: () => void;
  commands?: CommandPaletteItem[];
  enableGlobalShortcuts?: boolean;
}

function collectDomHintTargets(): HintOverlayTarget[] {
  if (typeof document === 'undefined') {
    return [];
  }
  const nodes = document.querySelectorAll<HTMLElement>('[data-focus-id]');
  const targets: HintOverlayTarget[] = [];
  nodes.forEach((el) => {
    const id = el.getAttribute('data-focus-id')?.trim();
    if (!id) {
      return;
    }
    const rect = el.getBoundingClientRect();
    const label =
      el.getAttribute('data-focus-label') ??
      el.getAttribute('aria-label') ??
      id;
    targets.push({
      id,
      label,
      x: Math.round(rect.left + Math.min(rect.width * 0.5, 28)),
      y: Math.round(rect.top + Math.min(rect.height * 0.5, 20)),
    });
  });
  return targets;
}

export function OverlayLayer({
  commandPaletteOpen,
  onCloseCommandPalette,
  onToggleCommandPalette,
  onCommand,
  companionOpen,
  onCloseCompanion,
  onToggleCompanion,
  keyboardHelpOpen,
  onCloseKeyboardHelp,
  onToggleKeyboardHelp,
  hintsOpen,
  hintTargets,
  onPickHint,
  onCloseHints,
  onToggleHints,
  activeWorkbench,
  theme,
  onToggleTheme,
  dockLayout,
  dockOpen,
  domain,
  contextEntity,
  pendingPrompt,
  onConsumePrompt,
  onOpenAiWorkbench,
  onOpenPluginStore,
  commands,
  enableGlobalShortcuts = true,
}: OverlayLayerProps) {
  const [internalPaletteOpen, setInternalPaletteOpen] = useState(false);
  const [internalCompanionOpen, setInternalCompanionOpen] = useState(false);
  const [internalHelpOpen, setInternalHelpOpen] = useState(false);
  const [internalHintsOpen, setInternalHintsOpen] = useState(false);
  const ime = useImeGuard();

  const isPaletteControlled = commandPaletteOpen !== undefined;
  const isCompanionControlled = companionOpen !== undefined;
  const isHelpControlled = keyboardHelpOpen !== undefined;
  const isHintsControlled = hintsOpen !== undefined;

  const effectivePaletteOpen = isPaletteControlled ? commandPaletteOpen : internalPaletteOpen;
  const effectiveCompanionOpen = isCompanionControlled ? companionOpen : internalCompanionOpen;
  const effectiveHelpOpen = isHelpControlled ? keyboardHelpOpen : internalHelpOpen;
  const effectiveHintsOpen = isHintsControlled ? hintsOpen : internalHintsOpen;

  const closePalette = useCallback(() => {
    if (!isPaletteControlled) {
      setInternalPaletteOpen(false);
    }
    onCloseCommandPalette?.();
  }, [isPaletteControlled, onCloseCommandPalette]);

  const closeCompanion = useCallback(() => {
    if (!isCompanionControlled) {
      setInternalCompanionOpen(false);
    }
    onCloseCompanion?.();
  }, [isCompanionControlled, onCloseCompanion]);

  const closeHelp = useCallback(() => {
    if (!isHelpControlled) {
      setInternalHelpOpen(false);
    }
    onCloseKeyboardHelp?.();
  }, [isHelpControlled, onCloseKeyboardHelp]);

  const closeHints = useCallback(() => {
    if (!isHintsControlled) {
      setInternalHintsOpen(false);
    }
    onCloseHints?.();
  }, [isHintsControlled, onCloseHints]);

  const handleCommand = useCallback(
    (commandId: string) => {
      onCommand?.(commandId);
      if (!isCompanionControlled && commandId === 'dock:toggle') {
        setInternalCompanionOpen((prev) => !prev);
      }
      if (!isHelpControlled && commandId === 'help:open') {
        setInternalHelpOpen(true);
      }
      if (!isHintsControlled && commandId === 'hints:open') {
        setInternalHintsOpen(true);
      }
      if (commandId === 'theme:toggle') {
        if (onToggleTheme) {
          onToggleTheme();
        } else if (!onCommand) {
          applyTheme(nextTheme(theme ?? getInitialTheme()));
        }
      } else if (!onCommand && commandId === 'theme:paper') {
        applyTheme('paper');
      } else if (!onCommand && commandId === 'theme:graphite') {
        applyTheme('graphite');
      }
    },
    [
      onCommand,
      isCompanionControlled,
      isHelpControlled,
      isHintsControlled,
      onToggleTheme,
      theme,
    ],
  );

  const handlePickHint = useCallback(
    (id: string) => {
      if (!isHintsControlled) {
        setInternalHintsOpen(false);
      }
      onPickHint?.(id);
    },
    [isHintsControlled, onPickHint],
  );

  useEffect(() => {
    if (!enableGlobalShortcuts || typeof window === 'undefined') {
      return undefined;
    }

    const handleGlobalKeyDown = (event: globalThis.KeyboardEvent) => {
      if (ime.shouldIgnoreShortcut(event)) {
        return;
      }

      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
        if (!isPaletteControlled || onToggleCommandPalette) {
          event.preventDefault();
          if (onToggleCommandPalette) {
            onToggleCommandPalette();
          } else {
            setInternalPaletteOpen((prev) => !prev);
          }
        }
        return;
      }

      if (event.key === 'F8') {
        if (!isCompanionControlled || onToggleCompanion) {
          event.preventDefault();
          if (onToggleCompanion) {
            onToggleCompanion();
          } else {
            setInternalCompanionOpen((prev) => !prev);
          }
        }
        return;
      }

      if (
        isEditableTarget(event.target) ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        effectivePaletteOpen ||
        effectiveHintsOpen
      ) {
        return;
      }

      if (event.key === '?') {
        if (!isHelpControlled || onToggleKeyboardHelp) {
          event.preventDefault();
          if (onToggleKeyboardHelp) {
            onToggleKeyboardHelp();
          } else {
            setInternalHelpOpen((prev) => !prev);
          }
        }
        return;
      }

      if (event.key.toLowerCase() === 'f' && !effectiveHelpOpen) {
        if (!isHintsControlled || onToggleHints) {
          event.preventDefault();
          if (onToggleHints) {
            onToggleHints();
          } else {
            setInternalHintsOpen((prev) => !prev);
          }
        }
      }
    };

    window.addEventListener('keydown', handleGlobalKeyDown);
    return () => {
      window.removeEventListener('keydown', handleGlobalKeyDown);
    };
  }, [
    enableGlobalShortcuts,
    ime,
    isPaletteControlled,
    isCompanionControlled,
    isHelpControlled,
    isHintsControlled,
    onToggleCommandPalette,
    onToggleCompanion,
    onToggleKeyboardHelp,
    onToggleHints,
    effectivePaletteOpen,
    effectiveHintsOpen,
    effectiveHelpOpen,
  ]);

  const resolvedHintTargets = useMemo<HintOverlayTarget[]>(() => {
    if (hintTargets !== undefined) {
      return hintTargets;
    }
    if (!effectiveHintsOpen) {
      return [];
    }
    return collectDomHintTargets();
  }, [hintTargets, effectiveHintsOpen]);

  return (
    <div data-testid="overlay-layer" className="contents">
      <CompanionDock
        open={effectiveCompanionOpen}
        onClose={closeCompanion}
        layout={dockLayout}
        activeWorkbench={activeWorkbench}
        domain={domain}
        contextEntity={contextEntity}
        pendingPrompt={pendingPrompt}
        onConsumePrompt={onConsumePrompt}
        onOpenAiWorkbench={onOpenAiWorkbench}
        onOpenPluginStore={onOpenPluginStore}
      />

      <HintOverlay
        open={effectiveHintsOpen}
        targets={resolvedHintTargets}
        onPick={handlePickHint}
        onClose={closeHints}
      />

      <CommandPalette
        open={effectivePaletteOpen}
        onClose={closePalette}
        onCommand={handleCommand}
        activeWorkbench={activeWorkbench}
        theme={theme}
        dockOpen={dockOpen ?? effectiveCompanionOpen}
        commands={commands}
      />

      <KeyboardHelp
        open={effectiveHelpOpen}
        onClose={closeHelp}
        activeWorkbench={activeWorkbench}
      />
    </div>
  );
}

export default OverlayLayer;
