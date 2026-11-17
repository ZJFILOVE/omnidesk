import React from 'react';
import type { ImeGuard } from '../hooks/useImeGuard';
import type { BilingualMode, FlattenedCommentItem } from '../utils/commentTree';
import MarkdownRenderer from './MarkdownRenderer';
import { FocusButton, HAIRLINE_BORDER, focusClass } from './WorkbenchLayout';

export interface CommentTreeNodeProps {
  item: FlattenedCommentItem;
  bilingualMode: BilingualMode;
  collapsed: boolean;
  voted: boolean;
  activeFocusId: string;
  ime: ImeGuard;
  onFocus: (id: string) => void;
  onToggleCollapse: (id: string) => void;
  onToggleVote: (id: string) => void;
  onDirectionKey: (item: FlattenedCommentItem, key: string) => boolean;
}

export const CommentTreeNode: React.FC<CommentTreeNodeProps> = ({
  item,
  bilingualMode,
  collapsed,
  voted,
  activeFocusId,
  ime,
  onFocus,
  onToggleCollapse,
  onToggleVote,
  onDirectionKey,
}) => {
  const { node, depth, hasChildren, descendantCount } = item;
  const isNodeFocused = activeFocusId === item.focusId;
  const indentPx = depth > 0 ? Math.min(depth, 6) * 14 : 0;

  return (
    <div
      role="treeitem"
      aria-level={depth + 1}
      aria-expanded={hasChildren ? !collapsed : undefined}
      data-comment-id={node.id}
      data-depth={depth}
      className="flex flex-col gap-1"
      style={{
        marginLeft: `${indentPx}px`,
        paddingLeft: depth > 0 ? '10px' : '0px',
        borderLeft: depth > 0 ? `2px solid ${HAIRLINE_BORDER}` : 'none',
      }}
    >
      <div
        className="flex flex-col gap-2 rounded-[12px] p-3.5"
        style={{
          backgroundColor: 'var(--bg-canvas, #F6F5F3)',
          boxShadow: isNodeFocused ? 'inset 0 0 0 1.5px var(--focus-ring)' : undefined,
        }}
      >
        <div className="flex items-start justify-between gap-2">
          <div
            data-focus-id={item.focusId}
            data-focused={isNodeFocused ? 'true' : undefined}
            tabIndex={-1}
            onClick={(e) => {
              onFocus(item.focusId);
              const target = e.target as HTMLElement | null;
              if (target?.closest('a, button')) return;
              if (typeof window !== 'undefined' && window.getSelection()?.toString().trim()) {
                return;
              }
              if (hasChildren && e.detail === 0) {
                onToggleCollapse(node.id);
              }
            }}
            onFocus={(e) => {
              if (e.target === e.currentTarget && item.focusId !== activeFocusId) {
                onFocus(item.focusId);
              }
            }}
            onKeyDown={(e) => {
              if (ime.shouldIgnoreShortcut(e)) return;
              if (!e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
                if (onDirectionKey(item, e.key)) {
                  e.preventDefault();
                  e.stopPropagation();
                  return;
                }
              }
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                e.stopPropagation();
                onFocus(item.focusId);
                if (hasChildren) onToggleCollapse(node.id);
              }
            }}
            className={focusClass(
              isNodeFocused,
              'min-w-0 flex-1 cursor-pointer rounded-[8px] px-1.5 py-0.5 text-left',
            )}
          >
            <div className="mb-1.5 flex flex-wrap items-center gap-2 text-[13.5px]">
              <span className="font-mono font-semibold text-[var(--text-primary)]">{node.author}</span>
              {node.roleBadge ? (
                <span
                  className="rounded-full px-2 py-0.5 text-[13.5px] font-medium"
                  style={{
                    backgroundColor: 'var(--status-info-bg, #E7F0FA)',
                    color: 'var(--status-info-fg, #185194)',
                  }}
                >
                  {node.roleBadge}
                </span>
              ) : null}
              <span style={{ color: 'var(--text-subtle, #635E56)' }}>{node.timeAgo}</span>
              {depth > 0 ? (
                <span className="font-mono text-[13.5px]" style={{ color: 'var(--text-subtle, #635E56)' }}>
                  L{depth}
                </span>
              ) : null}
            </div>

            {bilingualMode !== 'zh' ? <MarkdownRenderer content={node.original} compact /> : null}
            {node.translated && bilingualMode !== 'en' ? (
              <div
                className={bilingualMode !== 'zh' ? 'mt-2 border-t pt-2' : ''}
                style={{ borderColor: HAIRLINE_BORDER }}
              >
                <MarkdownRenderer content={node.translated} compact muted />
              </div>
            ) : null}
          </div>

          <div className="flex shrink-0 items-center gap-1">
            <FocusButton
              focusId={item.voteFocusId}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onToggleVote(node.id)}
              selected={voted}
              tone="text"
              surface="surface"
              className="px-2 py-1 font-mono text-[13.5px]"
            >
              ▲ {node.score + (voted ? 1 : 0)}
            </FocusButton>
            {hasChildren ? (
              <FocusButton
                focusId={item.toggleFocusId}
                activeFocusId={activeFocusId}
                onFocus={onFocus}
                onActivate={() => onToggleCollapse(node.id)}
                surface="surface"
                className="px-2 py-1 text-[13.5px] font-medium"
              >
                {collapsed ? `展开 (${descendantCount})` : '折叠'}
              </FocusButton>
            ) : null}
          </div>
        </div>

        {hasChildren && collapsed ? (
          <div
            data-testid={`comment-collapsed-summary-${node.id}`}
            className="rounded-[8px] px-3 py-1.5 text-[13.5px]"
            style={{
              backgroundColor: 'var(--bg-soft, #E6E2DA)',
              color: 'var(--text-muted, #59544D)',
            }}
          >
            已折叠 · 含 {descendantCount} 条后代回复（按 l / → 展开）
          </div>
        ) : null}
      </div>
    </div>
  );
};

export default CommentTreeNode;
