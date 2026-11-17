// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CommentNode } from '../../data/mock';
import { CommentTreeView, countDescendants } from '../CommentTreeView';

afterEach(() => {
  cleanup();
});

const NESTED_COMMENTS: CommentNode[] = [
  {
    id: 'c1',
    author: 'alice',
    roleBadge: 'Author',
    timeAgo: '2h ago',
    score: 10,
    original: 'Root argument on entropy bounds.',
    translated: '关于熵边界的核心论证。',
    children: [
      {
        id: 'c1-1',
        author: 'bob',
        timeAgo: '1h ago',
        score: 5,
        original: 'Child reply checking Triton kernel.',
        translated: '检查 Triton 算子的子回复。',
        children: [
          {
            id: 'c1-1-1',
            author: 'carol',
            timeAgo: '30m ago',
            score: 2,
            original: 'Grandchild reply on FP32 accumulation.',
            translated: '关于 FP32 累加的孙级回复。',
          },
          {
            id: 'c1-1-2',
            author: 'dave',
            timeAgo: '20m ago',
            score: 1,
            original: 'Second grandchild reply.',
            translated: '第二条孙级回复。',
          },
        ],
      },
    ],
  },
];

describe('CommentTreeView', () => {
  it('renders nested indentation, stable focusIds, recursive descendant count on collapse, and upvotes', () => {
    expect(countDescendants(NESTED_COMMENTS[0])).toBe(3);

    render(<CommentTreeView articleId="art-1" comments={NESTED_COMMENTS} />);

    const items = screen.getAllByRole('treeitem');
    expect(items).toHaveLength(4);
    expect(items[0].getAttribute('data-depth')).toBe('0');
    expect(items[1].getAttribute('data-depth')).toBe('1');
    expect(items[2].getAttribute('data-depth')).toBe('2');
    expect(items[2].style.marginLeft).toBe('28px');

    const rootFocusNode = document.querySelector('[data-focus-id="text:comment:node:c1"]');
    expect(rootFocusNode).not.toBeNull();

    // Vote increment
    const voteBtn = screen.getByRole('button', { name: '▲ 10' });
    fireEvent.click(voteBtn);
    expect(screen.getByRole('button', { name: '▲ 11' })).not.toBeNull();

    // Collapse root: hides 3 descendants and shows total descendant count (3)
    const collapseBtn = document.querySelector('[data-focus-id="text:comment:toggle:c1"]')!;
    fireEvent.click(collapseBtn);

    expect(screen.getAllByRole('treeitem')).toHaveLength(1);
    expect(screen.getByText('展开 (3)')).not.toBeNull();
    expect(screen.getByTestId('comment-collapsed-summary-c1').textContent).toContain('3 条后代回复');

    // Expand back
    fireEvent.click(screen.getByText('展开 (3)'));
    expect(screen.getAllByRole('treeitem')).toHaveLength(4);
  });

  it('supports h/l and ArrowLeft/ArrowRight collapse/expand and respects IME composition', () => {
    render(<CommentTreeView articleId="art-1" comments={NESTED_COMMENTS} />);
    const rootNode = document.querySelector('[data-focus-id="text:comment:node:c1"]') as HTMLElement;

    // IME composing ignores h
    act(() => {
      window.dispatchEvent(new Event('compositionstart'));
    });
    fireEvent.keyDown(rootNode, { key: 'h' });
    expect(screen.getAllByRole('treeitem')).toHaveLength(4);
    act(() => {
      window.dispatchEvent(new Event('compositionend'));
    });

    // Press h to collapse c1
    fireEvent.keyDown(rootNode, { key: 'h' });
    expect(screen.getAllByRole('treeitem')).toHaveLength(1);
    expect(screen.getByText('展开 (3)')).not.toBeNull();

    // Press ArrowRight to expand c1
    fireEvent.keyDown(rootNode, { key: 'ArrowRight' });
    expect(screen.getAllByRole('treeitem')).toHaveLength(4);

    // Press ArrowLeft to collapse c1 again
    fireEvent.keyDown(rootNode, { key: 'ArrowLeft' });
    expect(screen.getAllByRole('treeitem')).toHaveLength(1);

    // Press l to expand c1
    fireEvent.keyDown(rootNode, { key: 'l' });
    expect(screen.getAllByRole('treeitem')).toHaveLength(4);
  });

  it('supports bilingual mode switching (bilingual, zh, en)', () => {
    const { rerender } = render(
      <CommentTreeView articleId="art-1" comments={NESTED_COMMENTS} bilingualMode="bilingual" />,
    );
    expect(screen.getByText('Root argument on entropy bounds.')).not.toBeNull();
    expect(screen.getByText('关于熵边界的核心论证。')).not.toBeNull();

    rerender(<CommentTreeView articleId="art-1" comments={NESTED_COMMENTS} bilingualMode="zh" />);
    expect(screen.queryByText('Root argument on entropy bounds.')).toBeNull();
    expect(screen.getByText('关于熵边界的核心论证。')).not.toBeNull();

    rerender(<CommentTreeView articleId="art-1" comments={NESTED_COMMENTS} bilingualMode="en" />);
    expect(screen.getByText('Root argument on entropy bounds.')).not.toBeNull();
    expect(screen.queryByText('关于熵边界的核心论证。')).toBeNull();
  });

  it('windows long comment trees with Load More button', () => {
    const longList: CommentNode[] = Array.from({ length: 55 }, (_, i) => ({
      id: `bulk-${i}`,
      author: `user_${i}`,
      timeAgo: '1h ago',
      score: i,
      original: `Comment item #${i}`,
    }));

    render(
      <CommentTreeView
        articleId="long-art"
        comments={longList}
        initialVisibleLimit={20}
        pageSize={20}
      />,
    );

    expect(screen.getAllByRole('treeitem')).toHaveLength(20);
    const loadMore = screen.getByRole('button', { name: /加载更多评论.*35/ });
    fireEvent.click(loadMore);
    expect(screen.getAllByRole('treeitem')).toHaveLength(40);
  });

  it('distinguishes empty state from error state with retry callback', () => {
    const onRetry = vi.fn();
    const { rerender } = render(<CommentTreeView articleId="art-empty" comments={[]} />);

    expect(screen.getByTestId('comment-tree-empty')).not.toBeNull();
    expect(screen.queryByTestId('comment-tree-error')).toBeNull();

    rerender(
      <CommentTreeView
        articleId="art-err"
        comments={[]}
        error="HTTP 503 上游站点限流"
        onRetry={onRetry}
      />,
    );

    expect(screen.queryByTestId('comment-tree-empty')).toBeNull();
    const errBox = screen.getByTestId('comment-tree-error');
    expect(errBox.getAttribute('role')).toBe('alert');
    expect(errBox.textContent).toContain('HTTP 503 上游站点限流');

    fireEvent.click(screen.getByRole('button', { name: '重试加载' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
