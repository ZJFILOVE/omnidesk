import { useEffect, useRef } from 'react';
import type { Direction, WorkbenchId } from '../types';
import { isEditableElement } from '../utils/shortcuts';
import { useImeGuard } from './useImeGuard';
import { scrollReader } from '../utils/readerScroll';

const WORKBENCH_SHORTCUT_MAP: Record<string, WorkbenchId> = {
  '1': 'media',
  '2': 'text',
  '3': 'book',
  '4': 'ai',
  '5': 'store',
};

export interface UseGlobalKeysOptions {
  paletteOpen: boolean;
  hintActive: boolean;
  helpOpen?: boolean;
  onSelectWorkbench: (id: WorkbenchId) => void;
  onTogglePalette: () => void;
  onClosePalette: () => void;
  onToggleDock: () => void;
  onToggleTheme: () => void;
  onToggleHelp?: () => void;
  onCloseHelp?: () => void;
  onTriggerHints: () => void;
  onCancelHints: () => void;
  onMove: (direction: Direction) => string | null;
  onActivate: () => boolean;
  onSpace?: () => void;
  onPlayerKey?: (event: KeyboardEvent) => boolean;
  onCycleZone?: () => void;
  onAltShortcut?: (letter: string) => boolean | void;
  onTriggerSearch: () => void;
}

export function useGlobalKeys(options: UseGlobalKeysOptions): { isComposing: boolean } {
  const ime = useImeGuard();
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const imeRef = useRef(ime);
  imeRef.current = ime;

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (imeRef.current.shouldIgnoreShortcut(event)) {
        return;
      }

      const opts = optionsRef.current;
      const key = event.key;
      const lowerKey = key.toLowerCase();
      const ctrlOrMeta = (event.ctrlKey || event.metaKey) && !event.altKey;

      if (ctrlOrMeta && !event.shiftKey) {
        const targetWorkbench = WORKBENCH_SHORTCUT_MAP[key];
        if (targetWorkbench) {
          event.preventDefault();
          if (!event.repeat) {
            if (opts.hintActive) opts.onCancelHints();
            if (opts.paletteOpen) opts.onClosePalette();
            if (opts.helpOpen) opts.onCloseHelp?.();
            opts.onSelectWorkbench(targetWorkbench);
          }
          return;
        }

        if (lowerKey === 'k') {
          event.preventDefault();
          if (!event.repeat) {
            if (opts.hintActive) opts.onCancelHints();
            if (opts.helpOpen) opts.onCloseHelp?.();
            opts.onTogglePalette();
          }
          return;
        }
      }

      if (ctrlOrMeta && key === 'Tab') {
        event.preventDefault();
        if (!event.repeat) {
          if (opts.hintActive) opts.onCancelHints();
          if (opts.paletteOpen) opts.onClosePalette();
          if (opts.helpOpen) opts.onCloseHelp?.();
          opts.onCycleZone?.();
        }
        return;
      }

      if (!event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && key === 'F8') {
        event.preventDefault();
        if (!event.repeat) {
          if (opts.hintActive) opts.onCancelHints();
          if (opts.helpOpen) opts.onCloseHelp?.();
          opts.onToggleDock();
        }
        return;
      }

      if (
        event.altKey &&
        !event.ctrlKey &&
        !event.metaKey &&
        !event.shiftKey &&
        /^[a-z0-9]$/.test(lowerKey)
      ) {
        if (lowerKey === 't') {
          event.preventDefault();
          if (!event.repeat) {
            opts.onToggleTheme();
          }
          return;
        }
        if (opts.onAltShortcut) {
          if (!event.repeat) {
            const handled = opts.onAltShortcut(lowerKey);
            if (handled !== false) {
              if (opts.hintActive) opts.onCancelHints();
              if (opts.paletteOpen) opts.onClosePalette();
              if (opts.helpOpen) opts.onCloseHelp?.();
              event.preventDefault();
            }
          }
          return;
        }
      }

      if (opts.paletteOpen) {
        if (key === 'Escape' && !event.defaultPrevented) {
          event.preventDefault();
          opts.onClosePalette();
        }
        return;
      }

      if (opts.hintActive) {
        if (key === 'Escape' && !event.defaultPrevented) {
          event.preventDefault();
          opts.onCancelHints();
        }
        return;
      }

      if (opts.helpOpen) {
        if ((key === 'Escape' || key === '?') && !event.defaultPrevented) {
          event.preventDefault();
          if (!event.repeat) {
            opts.onCloseHelp?.();
          }
        }
        return;
      }

      if (isEditableElement(event.target)) {
        if (key === 'Escape' && event.target instanceof HTMLElement) {
          event.target.blur();
        }
        return;
      }

      if (event.defaultPrevented) {
        return;
      }

      if (event.ctrlKey || event.metaKey || event.altKey) {
        return;
      }

      if (scrollReader(event)) {
        event.preventDefault();
        return;
      }

      if (opts.onPlayerKey?.(event)) {
        event.preventDefault();
        return;
      }

      if (key === 'ArrowLeft' || (lowerKey === 'h' && !event.shiftKey)) {
        const moved = opts.onMove('left');
        if (moved !== null || lowerKey === 'h') event.preventDefault();
        return;
      }

      if (key === 'ArrowDown' || (lowerKey === 'j' && !event.shiftKey)) {
        const moved = opts.onMove('down');
        if (moved !== null || lowerKey === 'j') event.preventDefault();
        return;
      }

      if (key === 'ArrowUp' || (lowerKey === 'k' && !event.shiftKey)) {
        const moved = opts.onMove('up');
        if (moved !== null || lowerKey === 'k') event.preventDefault();
        return;
      }

      if (key === 'ArrowRight' || (lowerKey === 'l' && !event.shiftKey)) {
        const moved = opts.onMove('right');
        if (moved !== null || lowerKey === 'l') event.preventDefault();
        return;
      }

      if (key === 'Enter' && !event.shiftKey) {
        if (opts.onActivate()) {
          event.preventDefault();
        }
        return;
      }

      if ((key === ' ' || event.code === 'Space') && !event.shiftKey) {
        event.preventDefault();
        if (!event.repeat) {
          if (opts.onSpace) {
            opts.onSpace();
          } else {
            opts.onActivate();
          }
        }
        return;
      }

      if (key === '?') {
        event.preventDefault();
        if (!event.repeat) {
          opts.onToggleHelp?.();
        }
        return;
      }

      if (lowerKey === 'f' && !event.shiftKey) {
        event.preventDefault();
        if (!event.repeat) opts.onTriggerHints();
        return;
      }

      if (lowerKey === 't' && !event.shiftKey) {
        event.preventDefault();
        if (!event.repeat) opts.onToggleTheme();
        return;
      }

      if (key === '/' && !event.shiftKey) {
        event.preventDefault();
        if (!event.repeat) opts.onTriggerSearch();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, []);

  return { isComposing: ime.isComposing };
}
