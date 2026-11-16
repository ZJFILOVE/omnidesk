import { useEffect, useRef } from 'react';
import type { Direction, WorkbenchId } from '../types';
import { isEditableTarget as isImeEditableTarget, useImeGuard } from './useImeGuard';

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
  onSelectWorkbench: (id: WorkbenchId) => void;
  onTogglePalette: () => void;
  onClosePalette: () => void;
  onToggleDock: () => void;
  onToggleTheme: () => void;
  onTriggerHints: () => void;
  onCancelHints: () => void;
  onMove: (direction: Direction) => string | null;
  onActivate: () => boolean;
  onTriggerSearch: () => void;
}

function isEditableElement(target: EventTarget | null): boolean {
  if (isImeEditableTarget(target)) {
    return true;
  }
  return target instanceof HTMLElement && target.closest('[contenteditable="true"]') !== null;
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
            opts.onSelectWorkbench(targetWorkbench);
          }
          return;
        }

        if (lowerKey === 'k') {
          event.preventDefault();
          if (!event.repeat) {
            if (opts.hintActive) opts.onCancelHints();
            opts.onTogglePalette();
          }
          return;
        }
      }

      if (!event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && key === 'F8') {
        event.preventDefault();
        if (!event.repeat) {
          if (opts.hintActive) opts.onCancelHints();
          opts.onToggleDock();
        }
        return;
      }

      if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && lowerKey === 't') {
        event.preventDefault();
        if (!event.repeat) {
          opts.onToggleTheme();
        }
        return;
      }

      if (opts.paletteOpen) {
        if (key === 'Escape') {
          event.preventDefault();
          opts.onClosePalette();
        }
        return;
      }

      if (opts.hintActive) {
        if (key === 'Escape') {
          event.preventDefault();
          opts.onCancelHints();
        }
        return;
      }

      if (isEditableElement(event.target)) {
        if (key === 'Escape' && event.target instanceof HTMLElement) {
          event.target.blur();
        }
        return;
      }

      if (event.ctrlKey || event.metaKey || event.altKey) {
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
