import { isEditableTarget } from '../hooks/useImeGuard';

export interface SelectionRect {
  left: number;
  top: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
}

export interface SelectionSnapshot {
  text: string;
  contextSentence: string;
  rect: SelectionRect;
  signature: string;
}

export const VIEWPORT_MARGIN = 10;
export const SELECTION_GAP = 8;
export const DEFAULT_BUBBLE_WIDTH = 420;
export const DEFAULT_BUBBLE_HEIGHT = 40;

export function clampBubblePosition(
  rect: Pick<SelectionRect, 'left' | 'top' | 'width' | 'bottom'>,
  bubbleSize: { width: number; height: number },
  viewport: { width: number; height: number },
): { left: number; top: number } {
  const width = bubbleSize.width > 0 ? bubbleSize.width : DEFAULT_BUBBLE_WIDTH;
  const height = bubbleSize.height > 0 ? bubbleSize.height : DEFAULT_BUBBLE_HEIGHT;
  const maxLeft = Math.max(VIEWPORT_MARGIN, viewport.width - width - VIEWPORT_MARGIN);
  const maxTop = Math.max(VIEWPORT_MARGIN, viewport.height - height - VIEWPORT_MARGIN);

  const centeredLeft = rect.left + rect.width / 2 - width / 2;
  const left = Math.min(maxLeft, Math.max(VIEWPORT_MARGIN, Math.round(centeredLeft)));

  const aboveTop = rect.top - height - SELECTION_GAP;
  const belowTop = rect.bottom + SELECTION_GAP;
  const preferredTop = aboveTop >= VIEWPORT_MARGIN ? aboveTop : belowTop;
  const top = Math.min(maxTop, Math.max(VIEWPORT_MARGIN, Math.round(preferredTop)));

  return { left, top };
}

function getElementFromNode(node: Node | null): Element | null {
  if (!node) return null;
  if (node.nodeType === Node.ELEMENT_NODE) {
    return node as Element;
  }
  return node.parentElement;
}

export function extractContextSentence(node: Node | null, selectedText: string): string {
  const startEl = getElementFromNode(node);
  const blockEl = startEl?.closest('p, blockquote, li, pre, h1, h2, h3, h4, div');
  const raw = (blockEl?.textContent ?? selectedText).replace(/\s+/g, ' ').trim();
  if (!raw) return selectedText;
  const sentences = raw.split(/(?<=[.!?。！？])\s+/);
  const needle = selectedText.toLowerCase();
  const matched = sentences.find((sentence) => sentence.toLowerCase().includes(needle));
  const candidate = matched ?? raw;
  return candidate.length > 240 ? `${candidate.slice(0, 237)}...` : candidate;
}

export function readSelectionSnapshot(
  container: HTMLElement | null,
  allowedSelector?: string,
): SelectionSnapshot | null {
  if (!container || typeof window === 'undefined' || typeof window.getSelection !== 'function') {
    return null;
  }
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    return null;
  }

  const text = selection.toString().replace(/\s+/g, ' ').trim();
  if (!text) {
    return null;
  }

  const range = selection.getRangeAt(0);
  const startNode = range.startContainer;
  const endNode = range.endContainer;
  if (!container.contains(startNode) || !container.contains(endNode)) {
    return null;
  }

  const startEl = getElementFromNode(startNode);
  const endEl = getElementFromNode(endNode);
  if (isEditableTarget(startEl) || startEl?.closest('input, textarea, [contenteditable="true"]')) {
    return null;
  }

  if (allowedSelector) {
    if (!startEl?.closest(allowedSelector)) {
      return null;
    }
    if (endEl?.closest('[data-selection-source="translation"]')) {
      return null;
    }
  }

  const rawRect = range.getBoundingClientRect();
  const clientRect =
    rawRect.width > 0 || rawRect.height > 0
      ? rawRect
      : range.getClientRects?.()[0] ?? startEl?.getBoundingClientRect();

  const rect: SelectionRect = clientRect
    ? {
        left: clientRect.left,
        top: clientRect.top,
        width: clientRect.width,
        height: clientRect.height,
        right: clientRect.right,
        bottom: clientRect.bottom,
      }
    : { left: 24, top: 24, width: 0, height: 0, right: 24, bottom: 24 };

  const contextSentence = extractContextSentence(range.commonAncestorContainer, text);
  const signature = `${text}|${range.startOffset}|${range.endOffset}`;

  return { text, contextSentence, rect, signature };
}
