import React from 'react';
import {
  FocusButton,
  HAIRLINE_BORDER,
  SectionLabel,
  focusClass,
  focusableInputProps,
  isImeComposing,
} from '../../components/WorkbenchLayout';
import type { CommentNode } from '../../data/mock';

export type BilingualMode = 'bilingual' | 'zh' | 'en';

export function countComments(nodes: CommentNode[]): number {
  return nodes.reduce(
    (sum, node) => sum + 1 + (node.children ? countComments(node.children) : 0),
    0,
  );
}

export interface CommentTreePanelProps {
  articleId: string;
  comments: CommentNode[];
  bilingualMode: BilingualMode;
  collapsedComments: Record<string, boolean>;
  votedComments: Record<string, boolean>;
  notes: string[];
  noteDraft: string;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onToggleCollapse: (id: string) => void;
  onToggleVote: (id: string) => void;
  onNoteDraftChange: (val: string) => void;
  onAddNote: () => void;
}

export const CommentTreePanel: React.FC<CommentTreePanelProps> = ({
  articleId,
  comments,
  bilingualMode,
  collapsedComments,
  votedComments,
  notes,
  noteDraft,
  activeFocusId,
  onFocus,
  onToggleCollapse,
  onToggleVote,
  onNoteDraftChange,
  onAddNote,
}) => {
  const renderNodes = (nodes: CommentNode[], depth = 0): React.ReactNode =>
    nodes.map((node) => {
      const collapsed = Boolean(collapsedComments[node.id]);
      const voted = Boolean(votedComments[node.id]);
      const hasChildren = Boolean(node.children?.length);

      return (
        <div
          key={node.id}
          className="flex flex-col gap-2"
          style={{
            marginLeft: depth > 0 ? '14px' : '0',
            paddingLeft: depth > 0 ? '10px' : '0',
            borderLeft: depth > 0 ? `1px solid ${HAIRLINE_BORDER}` : 'none',
          }}
        >
          <div
            className="rounded-lg border p-3"
            style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
          >
            <div className="mb-1.5 flex items-center justify-between gap-2 text-[11px]">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono font-semibold">{node.author}</span>
                {node.roleBadge ? (
                  <span
                    className="rounded px-1.5 py-0.5 text-[10px] font-medium"
                    style={{
                      backgroundColor: 'var(--status-info-bg, #E8F2FD)',
                      color: 'var(--status-info-fg, #2260AA)',
                    }}
                  >
                    {node.roleBadge}
                  </span>
                ) : null}
                <span style={{ color: 'var(--text-subtle, #8A847D)' }}>{node.timeAgo}</span>
              </div>
              <div className="flex items-center gap-1">
                <FocusButton
                  focusId={`text:comment:vote:${node.id}`}
                  activeFocusId={activeFocusId}
                  onFocus={onFocus}
                  onActivate={() => onToggleVote(node.id)}
                  selected={voted}
                  tone="text"
                  surface="surface"
                  className="px-1.5 py-0.5 font-mono text-[10px]"
                >
                  ▲ {node.score + (voted ? 1 : 0)}
                </FocusButton>
                {hasChildren ? (
                  <FocusButton
                    focusId={`text:comment:toggle:${node.id}`}
                    activeFocusId={activeFocusId}
                    onFocus={onFocus}
                    onActivate={() => onToggleCollapse(node.id)}
                    surface="surface"
                    className="px-1.5 py-0.5 font-mono text-[10px]"
                  >
                    {collapsed ? `展开 (${node.children?.length})` : '折叠'}
                  </FocusButton>
                ) : null}
              </div>
            </div>

            {bilingualMode !== 'zh' ? (
              <p className="text-xs leading-relaxed">{node.original}</p>
            ) : null}
            {node.translated && bilingualMode !== 'en' ? (
              <p className="mt-1 text-xs leading-relaxed" style={{ color: 'var(--text-muted, #66625D)' }}>
                {node.translated}
              </p>
            ) : null}
          </div>

          {hasChildren && !collapsed && node.children ? renderNodes(node.children, depth + 1) : null}
        </div>
      );
    });

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center justify-between">
        <SectionLabel>可折叠评论树 ({countComments(comments)})</SectionLabel>
        <span className="font-mono text-[10px]" style={{ color: 'var(--text-subtle, #8A847D)' }}>
          CommentTreeView
        </span>
      </div>

      {comments.length > 0 ? (
        <div className="flex flex-col gap-2.5">{renderNodes(comments)}</div>
      ) : (
        <div
          className="rounded-lg border p-3 text-xs"
          style={{
            backgroundColor: 'var(--bg-canvas, #F6F5F3)',
            borderColor: HAIRLINE_BORDER,
            color: 'var(--text-muted, #66625D)',
          }}
        >
          当前文章暂无社区跟帖，可在下方添加手写阅读批注。
        </div>
      )}

      <div className="mt-2 flex flex-col gap-2 border-t pt-4" style={{ borderColor: HAIRLINE_BORDER }}>
        <div className="text-xs font-semibold">个人 ## Notes 批注（受保护区）</div>
        {notes.map((note, idx) => (
          <div
            key={`${articleId}-note-${idx}`}
            className="rounded border p-2 text-xs leading-relaxed"
            style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
          >
            {note}
          </div>
        ))}
        <textarea
          rows={2}
          value={noteDraft}
          onChange={(e) => onNoteDraftChange(e.target.value)}
          onKeyDown={(e) => {
            if (isImeComposing(e)) return;
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              onAddNote();
            }
          }}
          placeholder="追加阅读笔记（写入 <!-- AUTO:END --> 之后）..."
          aria-label="追加个人阅读笔记"
          data-focused={activeFocusId === 'text:note:input' ? 'true' : undefined}
          {...focusableInputProps('text:note:input', activeFocusId, onFocus)}
          className={focusClass(
            activeFocusId === 'text:note:input',
            'w-full rounded-md border p-2 text-xs',
          )}
          style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
        />
        <FocusButton
          focusId="text:note:add"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onAddNote}
          selected
          tone="text"
          className="px-3 py-1.5 text-xs font-medium"
        >
          保存至 ## Notes
        </FocusButton>
      </div>
    </div>
  );
};

export default CommentTreePanel;
