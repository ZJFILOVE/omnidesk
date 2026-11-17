import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CommentNode } from '../data/mock';
import { isEditableTarget, useImeGuard } from '../hooks/useImeGuard';
import {
  flattenCommentTree,
  type BilingualMode,
  type FlattenedCommentItem,
} from '../utils/commentTree';
import CommentTreeNode from './CommentTreeNode';
import StateView from './StateView';
import { FocusButton } from './WorkbenchLayout';

export { countComments, countDescendants, flattenCommentTree } from '../utils/commentTree';
export type { BilingualMode, FlattenedCommentItem } from '../utils/commentTree';

export interface CommentTreeViewProps {
  articleId: string;
  comments: CommentNode[];
  bilingualMode?: BilingualMode;
  collapsedComments?: Record<string, boolean>;
  votedComments?: Record<string, boolean>;
  activeFocusId?: string;
  initialVisibleLimit?: number;
  pageSize?: number;
  loading?: boolean;
  error?: string | null;
  emptyMessage?: string;
  onFocus?: (id: string) => void;
  onToggleCollapse?: (id: string) => void;
  onToggleVote?: (id: string) => void;
  onRetry?: () => void;
}

const DEFAULT_PAGE_SIZE = 40;

export const CommentTreeView: React.FC<CommentTreeViewProps> = ({
  articleId,
  comments,
  bilingualMode = 'bilingual',
  collapsedComments,
  votedComments,
  activeFocusId = '',
  initialVisibleLimit = DEFAULT_PAGE_SIZE,
  pageSize = DEFAULT_PAGE_SIZE,
  loading = false,
  error = null,
  emptyMessage = '当前文章暂无社区跟帖，可在下方添加手写阅读批注。',
  onFocus,
  onToggleCollapse,
  onToggleVote,
  onRetry,
}) => {
  const [internalCollapsed, setInternalCollapsed] = useState<Record<string, boolean>>({});
  const [internalVoted, setInternalVoted] = useState<Record<string, boolean>>({});
  const [internalFocusId, setInternalFocusId] = useState<string>('');
  const [visibleLimit, setVisibleLimit] = useState<number>(initialVisibleLimit);

  const treeRef = useRef<HTMLDivElement | null>(null);
  const ime = useImeGuard();
  const imeRef = useRef(ime);
  imeRef.current = ime;

  useEffect(() => {
    setVisibleLimit(initialVisibleLimit);
  }, [articleId, initialVisibleLimit]);

  const effectiveCollapsed = collapsedComments ?? internalCollapsed;
  const effectiveVoted = votedComments ?? internalVoted;
  const effectiveFocusId = activeFocusId || internalFocusId;

  const emitFocus = useCallback(
    (id: string) => {
      if (!activeFocusId) {
        setInternalFocusId(id);
      }
      onFocus?.(id);
    },
    [activeFocusId, onFocus],
  );

  const handleToggleCollapse = useCallback(
    (id: string) => {
      if (onToggleCollapse) {
        onToggleCollapse(id);
        return;
      }
      setInternalCollapsed((prev) => ({ ...prev, [id]: !prev[id] }));
    },
    [onToggleCollapse],
  );

  const handleToggleVote = useCallback(
    (id: string) => {
      if (onToggleVote) {
        onToggleVote(id);
        return;
      }
      setInternalVoted((prev) => ({ ...prev, [id]: !prev[id] }));
    },
    [onToggleVote],
  );

  const flattened = useMemo(
    () => flattenCommentTree(comments, effectiveCollapsed),
    [comments, effectiveCollapsed],
  );
  const visibleItems = useMemo(
    () => flattened.slice(0, Math.max(1, visibleLimit)),
    [flattened, visibleLimit],
  );
  const remainingCount = Math.max(0, flattened.length - visibleItems.length);

  const handleTreeDirectionKey = useCallback(
    (item: FlattenedCommentItem, key: string): boolean => {
      const collapsed = Boolean(effectiveCollapsed[item.id]);
      const lower = key.toLowerCase();

      if (key === 'ArrowLeft' || lower === 'h') {
        if (item.hasChildren && !collapsed) {
          handleToggleCollapse(item.id);
          return true;
        }
        if (item.parentFocusId) {
          emitFocus(item.parentFocusId);
          return true;
        }
        return false;
      }

      if (key === 'ArrowRight' || lower === 'l') {
        if (item.hasChildren && collapsed) {
          handleToggleCollapse(item.id);
          return true;
        }
        if (item.hasChildren && item.firstChildFocusId) {
          emitFocus(item.firstChildFocusId);
          return true;
        }
        return false;
      }

      return false;
    },
    [effectiveCollapsed, emitFocus, handleToggleCollapse],
  );

  useEffect(() => {
    if (typeof window === 'undefined' || typeof document === 'undefined') {
      return undefined;
    }
    const activeItem = visibleItems.find(
      (item) => item.focusId === effectiveFocusId || item.toggleFocusId === effectiveFocusId,
    );
    if (!activeItem) {
      return undefined;
    }

    const handleWindowKeyDown = (event: KeyboardEvent) => {
      if (imeRef.current.shouldIgnoreShortcut(event)) return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (isEditableTarget(event.target)) return;
      if (
        document.querySelector(
          '[role="dialog"], [data-testid="hint-overlay"], [data-testid="selection-bubble"]',
        )
      ) {
        return;
      }
      if (
        event.target instanceof Element &&
        event.target.closest(`[data-focus-id="${activeItem.focusId}"]`)
      ) {
        return;
      }

      if (handleTreeDirectionKey(activeItem, event.key)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };

    window.addEventListener('keydown', handleWindowKeyDown, true);
    return () => {
      window.removeEventListener('keydown', handleWindowKeyDown, true);
    };
  }, [effectiveFocusId, handleTreeDirectionKey, visibleItems]);

  if (loading) {
    return (
      <StateView
        mode="loading"
        variant="inline"
        testId="comment-tree-loading"
        title="正在加载评论树…"
        description=""
      />
    );
  }

  if (error) {
    return (
      <StateView
        mode="error"
        variant="inline"
        testId="comment-tree-error"
        title="评论加载失败"
        error={error}
        activeFocusId={effectiveFocusId}
        onFocus={emitFocus}
        action={
          onRetry
            ? {
                label: '重试加载',
                focusId: `text:comment:retry:${articleId}`,
                onActivate: onRetry,
              }
            : undefined
        }
      />
    );
  }

  if (comments.length === 0) {
    return (
      <StateView
        mode="empty"
        variant="inline"
        testId="comment-tree-empty"
        title="暂无讨论"
        description={emptyMessage}
      />
    );
  }

  return (
    <div ref={treeRef} role="tree" aria-label="评论树" data-testid="comment-tree" className="flex flex-col gap-2.5">
      {visibleItems.map((item) => (
        <CommentTreeNode
          key={item.id}
          item={item}
          bilingualMode={bilingualMode}
          collapsed={Boolean(effectiveCollapsed[item.id])}
          voted={Boolean(effectiveVoted[item.id])}
          activeFocusId={effectiveFocusId}
          ime={ime}
          onFocus={emitFocus}
          onToggleCollapse={handleToggleCollapse}
          onToggleVote={handleToggleVote}
          onDirectionKey={handleTreeDirectionKey}
        />
      ))}

      {remainingCount > 0 ? (
        <FocusButton
          focusId={`text:comment:load-more:${articleId}`}
          activeFocusId={effectiveFocusId}
          onFocus={emitFocus}
          onActivate={() => setVisibleLimit((prev) => prev + Math.max(1, pageSize))}
          tone="text"
          surface="soft"
          className="mt-1 px-3 py-2 text-center text-[13.5px] font-medium"
        >
          加载更多评论 (还有 {remainingCount} 条可见)
        </FocusButton>
      ) : null}
    </div>
  );
};

export default CommentTreeView;
