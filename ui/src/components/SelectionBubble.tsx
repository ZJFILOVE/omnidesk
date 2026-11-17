import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { BookPlus, Copy, Languages, Search, Sparkles, X } from 'lucide-react';
import { useImeGuard } from '../hooks/useImeGuard';
import {
  DEFAULT_BUBBLE_HEIGHT,
  DEFAULT_BUBBLE_WIDTH,
  clampBubblePosition,
  readSelectionSnapshot,
  type SelectionSnapshot,
} from '../utils/selection';

export { clampBubblePosition } from '../utils/selection';

export type SelectionActionType = 'etymology' | 'clause' | 'vocab' | 'companion' | 'copy';

export interface SelectionActionEvent {
  action: SelectionActionType;
  text: string;
  contextSentence: string;
}

export interface SelectionBubbleProps {
  containerRef: React.RefObject<HTMLElement | null>;
  allowedSelector?: string;
  disabled?: boolean;
  onLookupEtymology?: (text: string, contextSentence: string) => void;
  onParseClause?: (text: string, contextSentence: string) => void;
  onAddToVocab?: (text: string, contextSentence: string) => void;
  onSendToCompanion?: (text: string, contextSentence: string) => void;
  onCopy?: (text: string) => void;
  onAction?: (event: SelectionActionEvent) => void;
  onClose?: () => void;
}

interface BubbleActionItem {
  id: SelectionActionType | 'close';
  label: string;
  ariaLabel: string;
}

const BUBBLE_ACTIONS: readonly BubbleActionItem[] = [
  { id: 'etymology', label: '查词源', ariaLabel: '查词源' },
  { id: 'clause', label: '拆长难句', ariaLabel: '拆长难句' },
  { id: 'vocab', label: '加入生词本', ariaLabel: '加入生词本' },
  { id: 'companion', label: '送 F8 伴读', ariaLabel: '送 F8 伴读' },
  { id: 'copy', label: '复制', ariaLabel: '复制选区' },
  { id: 'close', label: '关闭', ariaLabel: '关闭划词气泡' },
];

function renderActionIcon(actionId: BubbleActionItem['id']) {
  switch (actionId) {
    case 'etymology':
      return <Search className="h-3.5 w-3.5" aria-hidden="true" />;
    case 'clause':
      return <Languages className="h-3.5 w-3.5" aria-hidden="true" />;
    case 'vocab':
      return <BookPlus className="h-3.5 w-3.5" aria-hidden="true" />;
    case 'companion':
      return <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />;
    case 'copy':
      return <Copy className="h-3.5 w-3.5" aria-hidden="true" />;
    case 'close':
      return <X className="h-3.5 w-3.5" aria-hidden="true" />;
  }
}

export const SelectionBubble: React.FC<SelectionBubbleProps> = ({
  containerRef,
  allowedSelector,
  disabled = false,
  onLookupEtymology,
  onParseClause,
  onAddToVocab,
  onSendToCompanion,
  onCopy,
  onAction,
  onClose,
}) => {
  const [snapshot, setSnapshot] = useState<SelectionSnapshot | null>(null);
  const [activeIndex, setActiveIndex] = useState<number>(0);
  const [measuredSize, setMeasuredSize] = useState<{ width: number; height: number }>({
    width: DEFAULT_BUBBLE_WIDTH,
    height: DEFAULT_BUBBLE_HEIGHT,
  });

  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const isPointerDownInContainerRef = useRef(false);
  const suppressedOutsideRef = useRef(false);
  const dismissedSignatureRef = useRef<string | null>(null);
  const ime = useImeGuard();
  const imeRef = useRef(ime);
  imeRef.current = ime;

  const closeBubble = useCallback(
    (dismissCurrent = true) => {
      if (dismissCurrent && snapshot) {
        dismissedSignatureRef.current = snapshot.signature;
      }
      setSnapshot(null);
      setActiveIndex(0);
      onClose?.();
    },
    [onClose, snapshot],
  );

  const evaluateSelection = useCallback(
    (forceShow = false) => {
      if (disabled || imeRef.current.isComposingRef.current) {
        setSnapshot(null);
        return;
      }
      const next = readSelectionSnapshot(containerRef.current, allowedSelector);
      if (!next) {
        setSnapshot(null);
        return;
      }
      if (!forceShow && dismissedSignatureRef.current === next.signature) {
        return;
      }
      dismissedSignatureRef.current = null;
      setSnapshot(next);
      setActiveIndex(0);
    },
    [allowedSelector, containerRef, disabled],
  );

  const executeAction = useCallback(
    (actionId: BubbleActionItem['id']) => {
      if (!snapshot) return;
      if (actionId === 'close') {
        closeBubble(true);
        return;
      }

      const { text, contextSentence } = snapshot;
      if (actionId === 'copy') {
        if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text).catch(() => {});
        }
        onCopy?.(text);
      } else if (actionId === 'etymology') {
        onLookupEtymology?.(text, contextSentence);
      } else if (actionId === 'clause') {
        onParseClause?.(text, contextSentence);
      } else if (actionId === 'vocab') {
        onAddToVocab?.(text, contextSentence);
      } else if (actionId === 'companion') {
        onSendToCompanion?.(text, contextSentence);
      }

      onAction?.({ action: actionId, text, contextSentence });
      closeBubble(true);
    },
    [
      closeBubble,
      onAction,
      onAddToVocab,
      onCopy,
      onLookupEtymology,
      onParseClause,
      onSendToCompanion,
      snapshot,
    ],
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container || disabled || typeof document === 'undefined') {
      return undefined;
    }

    const handleContainerMouseDown = () => {
      isPointerDownInContainerRef.current = true;
      suppressedOutsideRef.current = false;
      dismissedSignatureRef.current = null;
      setSnapshot(null);
    };

    const handleContainerMouseUp = () => {
      isPointerDownInContainerRef.current = false;
      suppressedOutsideRef.current = false;
      evaluateSelection(true);
    };

    const handleContainerKeyUp = (event: KeyboardEvent) => {
      if (imeRef.current.shouldIgnoreShortcut(event) || event.key === 'Escape') {
        return;
      }
      suppressedOutsideRef.current = false;
      evaluateSelection(false);
    };

    const handleDocumentMouseDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (bubbleRef.current && target && bubbleRef.current.contains(target)) {
        return;
      }
      if (container.contains(target)) {
        return;
      }
      isPointerDownInContainerRef.current = false;
      suppressedOutsideRef.current = true;
      setSnapshot(null);
    };

    const handleDocumentMouseUp = () => {
      if (!isPointerDownInContainerRef.current) return;
      isPointerDownInContainerRef.current = false;
      evaluateSelection(true);
    };

    const handleSelectionChange = () => {
      if (
        isPointerDownInContainerRef.current ||
        suppressedOutsideRef.current ||
        imeRef.current.isComposingRef.current
      ) {
        return;
      }
      evaluateSelection(false);
    };

    container.addEventListener('mousedown', handleContainerMouseDown);
    container.addEventListener('mouseup', handleContainerMouseUp);
    container.addEventListener('keyup', handleContainerKeyUp);
    document.addEventListener('mousedown', handleDocumentMouseDown, true);
    document.addEventListener('mouseup', handleDocumentMouseUp);
    document.addEventListener('selectionchange', handleSelectionChange);

    return () => {
      container.removeEventListener('mousedown', handleContainerMouseDown);
      container.removeEventListener('mouseup', handleContainerMouseUp);
      container.removeEventListener('keyup', handleContainerKeyUp);
      document.removeEventListener('mousedown', handleDocumentMouseDown, true);
      document.removeEventListener('mouseup', handleDocumentMouseUp);
      document.removeEventListener('selectionchange', handleSelectionChange);
    };
  }, [containerRef, disabled, evaluateSelection]);

  useEffect(() => {
    if (!snapshot || typeof window === 'undefined') {
      return undefined;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (imeRef.current.shouldIgnoreShortcut(event)) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;

      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeBubble(true);
        return;
      }

      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        event.preventDefault();
        event.stopPropagation();
        setActiveIndex((prev) => (prev + 1) % BUBBLE_ACTIONS.length);
        return;
      }

      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        event.preventDefault();
        event.stopPropagation();
        setActiveIndex((prev) => (prev - 1 + BUBBLE_ACTIONS.length) % BUBBLE_ACTIONS.length);
        return;
      }

      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        event.stopPropagation();
        const chosen = BUBBLE_ACTIONS[activeIndex];
        if (chosen) executeAction(chosen.id);
      }
    };

    window.addEventListener('keydown', handleKeyDown, true);
    return () => {
      window.removeEventListener('keydown', handleKeyDown, true);
    };
  }, [activeIndex, closeBubble, executeAction, snapshot]);

  useLayoutEffect(() => {
    if (!snapshot || !bubbleRef.current) return;
    const rect = bubbleRef.current.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      setMeasuredSize((prev) =>
        prev.width === rect.width && prev.height === rect.height
          ? prev
          : { width: rect.width, height: rect.height },
      );
    }
  }, [snapshot]);

  if (!snapshot || disabled) {
    return null;
  }

  const viewport = {
    width: typeof window !== 'undefined' && window.innerWidth > 0 ? window.innerWidth : 1280,
    height: typeof window !== 'undefined' && window.innerHeight > 0 ? window.innerHeight : 800,
  };
  const position = clampBubblePosition(snapshot.rect, measuredSize, viewport);

  return (
    <div
      ref={bubbleRef}
      role="toolbar"
      aria-label="划词操作气泡"
      data-testid="selection-bubble"
      data-selected-text={snapshot.text}
      className="omni-theme-surface fixed z-40 flex max-w-[calc(100vw-20px)] flex-wrap items-center gap-1 rounded-[12px] p-1.5"
      style={{
        left: `${position.left}px`,
        top: `${position.top}px`,
        backgroundColor: 'var(--bg-elevated, #FFFFFF)',
        color: 'var(--text-primary, #1C1B19)',
        boxShadow: 'var(--shadow-elevated)',
      }}
    >
      {BUBBLE_ACTIONS.map((item, idx) => {
        const isActive = idx === activeIndex;
        return (
          <button
            key={item.id}
            type="button"
            aria-label={item.ariaLabel}
            data-selection-action={item.id}
            data-active={isActive ? 'true' : 'false'}
            onMouseDown={(e) => e.preventDefault()}
            onMouseEnter={() => setActiveIndex(idx)}
            onClick={() => executeAction(item.id)}
            className={`inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[12px] font-medium transition-colors ${
              isActive ? 'omni-focus-ring' : 'hover:bg-[var(--hover-fill)]'
            }`}
            style={{
              backgroundColor: isActive
                ? 'var(--focus-fill, rgba(29, 78, 216, 0.08))'
                : 'transparent',
              color: 'var(--text-primary, #1F1E1C)',
            }}
          >
            {renderActionIcon(item.id)}
            <span>{item.label}</span>
          </button>
        );
      })}
    </div>
  );
};

export default SelectionBubble;
