import { LoadMoreButton, type LoadMoreState } from '../../components/LoadMoreButton';
import React from 'react';
import CommentTreeView, { countComments, type BilingualMode } from '../../components/CommentTreeView';
import { PanelSectionHeading } from '../../components/WorkbenchPrimitives';
import {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableInputProps,
  isImeComposing,
} from '../../components/WorkbenchLayout';
import type { CommentNode } from '../../data/mock';

export { countComments, type BilingualMode };

export interface CommentTreePanelProps {
  pagination?: LoadMoreState;
  articleId: string;
  comments: CommentNode[];
  bilingualMode: BilingualMode;
  collapsedComments: Record<string, boolean>;
  votedComments: Record<string, boolean>;
  notes: string[];
  noteDraft: string;
  activeFocusId: string;
  loading?: boolean;
  error?: string | null;
  onFocus: (id: string) => void;
  onToggleCollapse: (id: string) => void;
  onToggleVote: (id: string) => void;
  onRetryComments?: () => void;
  onNoteDraftChange: (val: string) => void;
  onAddNote: () => void;
}

export const CommentTreePanel: React.FC<CommentTreePanelProps> = ({
  pagination,
  articleId,
  comments,
  bilingualMode,
  collapsedComments,
  votedComments,
  notes,
  noteDraft,
  activeFocusId,
  loading = false,
  error = null,
  onFocus,
  onToggleCollapse,
  onToggleVote,
  onRetryComments,
  onNoteDraftChange,
  onAddNote,
}) => (
  <div className="flex flex-col gap-5 p-4.5">
    <PanelSectionHeading
      title="讨论与审稿意见"
      meta={`${countComments(comments)} 条`}
    />

    <CommentTreeView
      articleId={articleId}
      comments={comments}
      bilingualMode={bilingualMode}
      collapsedComments={collapsedComments}
      votedComments={votedComments}
      activeFocusId={activeFocusId}
      loading={loading && comments.length === 0}
      error={comments.length === 0 ? error : null}
      onFocus={onFocus}
      onToggleCollapse={onToggleCollapse}
      onToggleVote={onToggleVote}
      onRetry={onRetryComments}
    />

    {pagination && (pagination.hasMore || (comments.length > 0 && pagination.loadMoreError)) ? <LoadMoreButton id={`text:comments:next:${articleId}`} pagination={pagination} /> : null}

    <div className="mt-2 flex flex-col gap-2.5 border-t pt-4" style={{ borderColor: HAIRLINE_BORDER }}>
      <PanelSectionHeading title="个人批注 · ## Notes" meta={`${notes.length}`} />
      {notes.map((note, idx) => (
        <div
          key={`${articleId}-note-${idx}`}
          className="rounded-[12px] p-3.5 text-[15px] leading-[1.7]"
          style={{ backgroundColor: 'var(--bg-canvas)' }}
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
          'w-full rounded-[10px] border p-2.5 text-[15px] leading-[1.65]',
        )}
        style={{
          backgroundColor: 'var(--bg-canvas)',
          borderColor: 'var(--border-line)',
        }}
      />
      <FocusButton
        focusId="text:note:add"
        activeFocusId={activeFocusId}
        onFocus={onFocus}
        onActivate={onAddNote}
        selected
        tone="text"
        className="px-3 py-2 text-[14px] font-medium"
      >
        保存至 ## Notes
      </FocusButton>
    </div>
  </div>
);

export default CommentTreePanel;
