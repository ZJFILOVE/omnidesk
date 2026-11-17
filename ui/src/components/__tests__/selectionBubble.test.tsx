// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useRef } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SelectionBubble, clampBubblePosition } from '../SelectionBubble';

const originalInnerWidth = window.innerWidth;
const originalInnerHeight = window.innerHeight;

afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    writable: true,
    value: originalInnerWidth,
  });
  Object.defineProperty(window, 'innerHeight', {
    configurable: true,
    writable: true,
    value: originalInnerHeight,
  });
});

function SelectionHarness(props: {
  allowedSelector?: string;
  onLookupEtymology?: (text: string, ctx: string) => void;
  onParseClause?: (text: string, ctx: string) => void;
  onAddToVocab?: (text: string, ctx: string) => void;
  onSendToCompanion?: (text: string, ctx: string) => void;
  onCopy?: (text: string) => void;
  onClose?: () => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  return (
    <div>
      <div ref={containerRef} data-testid="selection-container">
        <div data-selection-source="original">
          <p data-testid="original-p">
            The arrangement feels deliberately ephemeral, like chalk on wet slate.
          </p>
        </div>
        <div data-selection-source="translation">
          <p data-testid="translated-p">这种安排刻意给人转瞬即逝之感。</p>
        </div>
      </div>
      <div data-testid="outside-area">Outside region</div>
      <SelectionBubble containerRef={containerRef} {...props} />
    </div>
  );
}

function selectTextInNode(
  el: HTMLElement,
  text: string,
  mockRect: Partial<DOMRect> = { left: 160, top: 180, width: 90, height: 18, right: 250, bottom: 198 },
) {
  const textNode = el.firstChild;
  if (!textNode) throw new Error('Missing text node');
  const full = textNode.textContent ?? '';
  const start = Math.max(0, full.indexOf(text));
  const end = start + text.length;
  const range = document.createRange();
  range.setStart(textNode, start);
  range.setEnd(textNode, end);
  range.getBoundingClientRect = () =>
    ({
      left: 0,
      top: 0,
      width: 0,
      height: 0,
      right: 0,
      bottom: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
      ...mockRect,
    }) as DOMRect;
  const sel = window.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
}

describe('SelectionBubble', () => {
  it('stays hidden when selection is empty or collapsed', () => {
    render(<SelectionHarness />);
    fireEvent.mouseUp(screen.getByTestId('selection-container'));
    expect(screen.queryByTestId('selection-bubble')).toBeNull();
  });

  it('shows all required actions on non-empty selection and clamps coordinates inside viewport', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1280 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 800 });

    render(<SelectionHarness />);
    selectTextInNode(screen.getByTestId('original-p'), 'ephemeral', {
      left: 1240,
      top: 785,
      width: 80,
      height: 18,
      right: 1320,
      bottom: 803,
    });
    fireEvent.mouseUp(screen.getByTestId('selection-container'));

    const bubble = screen.getByTestId('selection-bubble');
    expect(bubble.getAttribute('data-selected-text')).toBe('ephemeral');
    expect(screen.getByRole('button', { name: '查词源' })).not.toBeNull();
    expect(screen.getByRole('button', { name: '拆长难句' })).not.toBeNull();
    expect(screen.getByRole('button', { name: '加入生词本' })).not.toBeNull();
    expect(screen.getByRole('button', { name: '送 F8 伴读' })).not.toBeNull();
    expect(screen.getByRole('button', { name: '复制选区' })).not.toBeNull();
    expect(screen.getByRole('button', { name: '关闭划词气泡' })).not.toBeNull();

    const leftPx = Number.parseInt(bubble.style.left, 10);
    const topPx = Number.parseInt(bubble.style.top, 10);
    expect(leftPx).toBeGreaterThanOrEqual(10);
    expect(leftPx).toBeLessThanOrEqual(1280 - 420 - 10);
    expect(topPx).toBeGreaterThanOrEqual(10);
    expect(topPx).toBeLessThanOrEqual(800 - 40 - 10);

    const clampedTopLeft = clampBubblePosition(
      { left: -40, top: 2, width: 20, bottom: 20 },
      { width: 420, height: 40 },
      { width: 1000, height: 700 },
    );
    expect(clampedTopLeft.left).toBe(10);
    expect(clampedTopLeft.top).toBe(28);
  });

  it('does not trigger during IME composition or inside translation-only areas', () => {
    render(<SelectionHarness allowedSelector='[data-selection-source="original"]' />);

    act(() => {
      window.dispatchEvent(new Event('compositionstart'));
    });
    selectTextInNode(screen.getByTestId('original-p'), 'ephemeral');
    fireEvent.mouseUp(screen.getByTestId('selection-container'));
    expect(screen.queryByTestId('selection-bubble')).toBeNull();

    act(() => {
      window.dispatchEvent(new Event('compositionend'));
    });

    selectTextInNode(screen.getByTestId('translated-p'), '转瞬即逝');
    fireEvent.mouseUp(screen.getByTestId('selection-container'));
    expect(screen.queryByTestId('selection-bubble')).toBeNull();
  });

  it('supports keyboard arrow navigation, Enter execution, Escape, and outside click dismiss', () => {
    const onAddToVocab = vi.fn();
    const onSendToCompanion = vi.fn();
    const onClose = vi.fn();

    render(
      <SelectionHarness
        onAddToVocab={onAddToVocab}
        onSendToCompanion={onSendToCompanion}
        onClose={onClose}
      />,
    );

    selectTextInNode(screen.getByTestId('original-p'), 'ephemeral');
    fireEvent.mouseUp(screen.getByTestId('selection-container'));
    expect(screen.getByTestId('selection-bubble')).not.toBeNull();

    // Move right twice: 0 (查词源) -> 1 (拆长难句) -> 2 (加入生词本)
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    const vocabBtn = screen.getByRole('button', { name: '加入生词本' });
    expect(vocabBtn.getAttribute('data-active')).toBe('true');

    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onAddToVocab).toHaveBeenCalledWith(
      'ephemeral',
      'The arrangement feels deliberately ephemeral, like chalk on wet slate.',
    );
    expect(screen.queryByTestId('selection-bubble')).toBeNull();

    // Re-open and close with Escape
    fireEvent.mouseDown(screen.getByTestId('selection-container'));
    selectTextInNode(screen.getByTestId('original-p'), 'chalk');
    fireEvent.mouseUp(screen.getByTestId('selection-container'));
    expect(screen.getByTestId('selection-bubble')).not.toBeNull();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('selection-bubble')).toBeNull();
    expect(onClose).toHaveBeenCalled();

    // Re-open and close by clicking outside
    fireEvent.mouseDown(screen.getByTestId('selection-container'));
    selectTextInNode(screen.getByTestId('original-p'), 'slate');
    fireEvent.mouseUp(screen.getByTestId('selection-container'));
    expect(screen.getByTestId('selection-bubble')).not.toBeNull();
    fireEvent.mouseDown(screen.getByTestId('outside-area'));
    fireEvent.mouseUp(screen.getByTestId('outside-area'));
    expect(screen.queryByTestId('selection-bubble')).toBeNull();
  });
});
