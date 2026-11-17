// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { TEXT_ARTICLES } from '../../data/mock';
import { BookWorkbench } from '../BookWorkbench';
import { TextWorkbench } from '../TextWorkbench';
import type { BilingualMode } from '../panels/CommentTreePanel';
import { TextArticleReader } from '../panels/TextArticleReader';

afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
});

function selectTextInside(element: Element, substring: string) {
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    const content = node.textContent ?? '';
    const idx = content.indexOf(substring);
    if (idx !== -1) {
      const range = document.createRange();
      range.setStart(node, idx);
      range.setEnd(node, idx + substring.length);
      range.getBoundingClientRect = () =>
        ({
          left: 120,
          top: 160,
          width: 80,
          height: 18,
          right: 200,
          bottom: 178,
          x: 120,
          y: 160,
          toJSON: () => ({}),
        }) as DOMRect;
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);
      return;
    }
    node = walker.nextNode();
  }
  throw new Error(`Could not find text "${substring}" inside element`);
}

describe('TextArticleReader & TextWorkbench', () => {
  it('renders article blocks with MarkdownRenderer + KaTeX and supports three-state bilingual toggle', () => {
    const article = TEXT_ARTICLES[0];
    function ReaderHarness() {
      const [mode, setMode] = useState<BilingualMode>('bilingual');
      return (
        <TextArticleReader
          article={article}
          bilingualMode={mode}
          activeFocusId=""
          onFocus={vi.fn()}
          onBilingualModeChange={setMode}
          onSendToBabelDoc={vi.fn()}
          onOpenChromeApp={vi.fn()}
          onSelectBlock={vi.fn()}
        />
      );
    }

    const { container } = render(<ReaderHarness />);

    expect(container.querySelector('.katex-display')).not.toBeNull();
    expect(container.querySelector('table')).not.toBeNull();
    expect(container.querySelector('[data-selection-source="original"]')).not.toBeNull();
    expect(container.querySelector('[data-selection-source="translation"]')).not.toBeNull();

    // Switch to 仅译文
    fireEvent.click(screen.getByRole('button', { name: '仅译文' }));
    expect(container.querySelector('[data-selection-source="original"]')).toBeNull();
    expect(container.querySelector('[data-selection-source="translation"]')).not.toBeNull();

    // Switch to 仅原文
    fireEvent.click(screen.getByRole('button', { name: '仅原文' }));
    expect(container.querySelector('[data-selection-source="original"]')).not.toBeNull();
    expect(container.querySelector('[data-selection-source="translation"]')).toBeNull();
  });

  it('triggers SelectionBubble only in original text and forwards actions to TextWorkbench callbacks', () => {
    const onOpenDock = vi.fn();
    const onAddVocab = vi.fn();

    render(<TextWorkbench onOpenDock={onOpenDock} onAddVocab={onAddVocab} />);

    const blocksContainer = screen.getByTestId('text-article-blocks');
    const translationRegion = blocksContainer.querySelector(
      '[data-block-id="blk-j2"] [data-selection-source="translation"]',
    )!;
    selectTextInside(translationRegion, '桥接词元');
    fireEvent.mouseUp(blocksContainer);
    expect(screen.queryByTestId('selection-bubble')).toBeNull();

    const originalRegion = blocksContainer.querySelector('[data-selection-source="original"]')!;
    selectTextInside(originalRegion, 'Empirical');
    fireEvent.mouseUp(blocksContainer);

    const bubble = screen.getByTestId('selection-bubble');
    expect(bubble.getAttribute('data-selected-text')).toBe('Empirical');

    fireEvent.click(within(bubble).getByRole('button', { name: '送 F8 伴读' }));
    expect(onOpenDock).toHaveBeenCalledTimes(1);
    expect(onOpenDock.mock.calls[0][0]).toContain('Empirical');

    // Also test 加入生词本 callback
    fireEvent.mouseDown(blocksContainer);
    selectTextInside(originalRegion, 'Observation');
    fireEvent.mouseUp(blocksContainer);
    fireEvent.click(screen.getByRole('button', { name: '加入生词本' }));
    expect(onAddVocab).toHaveBeenCalledWith(
      expect.objectContaining({ word: 'Observation' }),
    );
  });
});

describe('BookWorkbench', () => {
  it('supports SelectionBubble in left original column, adds to VocabSm2Panel, and preserves spread/SM-2 behavior', () => {
    const onAddVocab = vi.fn();
    const onOpenDock = vi.fn();

    render(<BookWorkbench onAddVocab={onAddVocab} onOpenDock={onOpenDock} />);

    expect(screen.getByText('SM-2 生词复习队列 (6)')).not.toBeNull();

    // Selecting in right translation column should NOT open SelectionBubble
    const rightCol = screen.getByTestId('book-translation-column');
    selectTextInside(rightCol, '注意力函数');
    fireEvent.mouseUp(rightCol);
    expect(screen.queryByTestId('selection-bubble')).toBeNull();

    // Selecting in left original column opens SelectionBubble
    const leftCol = screen.getByTestId('book-original-column');
    selectTextInside(leftCol, 'maintaining');
    fireEvent.mouseUp(leftCol);

    const bubble = screen.getByTestId('selection-bubble');
    expect(bubble.getAttribute('data-selected-text')).toBe('maintaining');

    // Add to vocab and verify VocabSm2Panel updates immediately
    fireEvent.click(within(bubble).getByRole('button', { name: '加入生词本' }));
    expect(onAddVocab).toHaveBeenCalledWith(
      expect.objectContaining({
        word: 'maintaining',
        sourceTitle: 'Attention Is All You Need (BabelDOC 双栏对照版)',
      }),
    );
    expect(screen.getByText('SM-2 生词复习队列 (7)')).not.toBeNull();
    expect(screen.getByTestId('book-status-message').textContent).toContain('已加入生词本「maintaining」');

    // Lookup etymology on existing card 'ephemeral' reveals it in VocabSm2Panel
    fireEvent.mouseDown(leftCol);
    selectTextInside(leftCol, 'ephemeral');
    fireEvent.mouseUp(leftCol);
    fireEvent.click(screen.getByRole('button', { name: '查词源' }));
    expect(screen.getByTestId('book-status-message').textContent).toContain('ephemeral');

    // Page turning still works
    fireEvent.click(screen.getByRole('button', { name: '下一跨页' }));
    expect(screen.getByText('p.4 (2/2)')).not.toBeNull();

    // SM-2 rating still works
    fireEvent.click(screen.getByRole('button', { name: '记得' }));
    expect(screen.getByTestId('book-status-message').textContent).toContain('SM-2 已更新');
  });

  it('renders code blocks in TextArticleReader and links SelectionBubble to F8 CompanionDock & BookWorkbench vocab across App', () => {
    const codeArticle = TEXT_ARTICLES.find((a) => a.id === 'text-linuxdo-uv')!;
    const { container: readerContainer } = render(
      <TextArticleReader
        article={codeArticle}
        bilingualMode="bilingual"
        activeFocusId=""
        onFocus={vi.fn()}
        onBilingualModeChange={vi.fn()}
        onSendToBabelDoc={vi.fn()}
        onOpenChromeApp={vi.fn()}
        onSelectBlock={vi.fn()}
      />,
    );
    expect(readerContainer.querySelector('[data-code-lang="python"]')).not.toBeNull();
    expect(readerContainer.querySelector('.hljs-keyword')).not.toBeNull();
    cleanup();

    render(<App />);

    // Switch to TextWorkbench (Ctrl+2)
    fireEvent.keyDown(window, { key: '2', ctrlKey: true });
    const blocks = screen.getByTestId('text-article-blocks');
    const orig = blocks.querySelector('[data-selection-source="original"]')!;
    selectTextInside(orig, 'Empirical');
    fireEvent.mouseUp(blocks);

    // Send to F8 companion dock
    fireEvent.click(screen.getByRole('button', { name: '送 F8 伴读' }));
    const dock = screen.getByTestId('companion-dock');
    expect(within(dock).getAllByText(/Empirical/).length).toBeGreaterThanOrEqual(1);

    // Add word from TextWorkbench and verify it propagates to BookWorkbench SM-2 queue
    fireEvent.mouseDown(blocks);
    selectTextInside(orig, 'Observation');
    fireEvent.mouseUp(blocks);
    fireEvent.click(screen.getByRole('button', { name: '加入生词本' }));

    fireEvent.keyDown(window, { key: '3', ctrlKey: true });
    expect(screen.getByText('SM-2 生词复习队列 (7)')).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Observation' })).not.toBeNull();
  });
});
