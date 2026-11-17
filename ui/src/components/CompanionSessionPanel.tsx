import { Download, History, Plus, RefreshCw, Trash2, X } from 'lucide-react';
import type { AgentSessionSummary, RpcErrorInfo } from '../api';
import { sessionTimeLabel } from './companionHelpers';

export interface CompanionSessionPanelProps {
  sessions: AgentSessionSummary[];
  activeSessionId?: string | null;
  loading: boolean;
  busyId: string | null;
  error: RpcErrorInfo | null;
  notice: string | null;
  onRefresh: () => void;
  onNewSession: () => void;
  onOpenSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onExportSession: (id: string, format: 'markdown' | 'json') => void;
  onClose: () => void;
}

export function CompanionSessionPanel({
  sessions,
  activeSessionId,
  loading,
  busyId,
  error,
  notice,
  onRefresh,
  onNewSession,
  onOpenSession,
  onDeleteSession,
  onExportSession,
  onClose,
}: CompanionSessionPanelProps) {
  return (
    <section
      data-testid="companion-sessions"
      aria-label="会话记录"
      className="flex min-h-0 flex-1 flex-col bg-[var(--bg-canvas)]"
    >
      <div className="flex items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <History className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="text-[15.5px] font-semibold">会话记录</span>
          <span className="rounded-full bg-[var(--bg-soft)] px-2 py-0.5 text-[13px] text-[var(--text-muted)]">
            {sessions.length}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            aria-label="刷新会话记录"
            title="刷新会话记录"
            className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)] disabled:opacity-45"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭会话记录"
            className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      {error ? (
        <div
          data-testid="companion-session-error"
          role="alert"
          className="mx-4 mt-3 rounded-[10px] px-3 py-2 text-[13.5px]"
          style={{ backgroundColor: 'var(--status-err-bg)', color: 'var(--status-err-fg)' }}
        >
          [{error.code}] {error.message}
        </div>
      ) : null}
      {notice ? (
        <div
          data-testid="companion-session-notice"
          className="mx-4 mt-3 rounded-[10px] px-3 py-2 text-[13.5px]"
          style={{ backgroundColor: 'var(--status-info-bg)', color: 'var(--status-info-fg)' }}
        >
          {notice}
        </div>
      ) : null}

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {sessions.length === 0 && !loading ? (
          <div className="rounded-[12px] border border-dashed border-[var(--border-line)] px-4 py-8 text-center text-[14px] text-[var(--text-muted)]">
            还没有已保存的会话。发送第一条消息后会自动保存。
          </div>
        ) : null}
        {sessions.map((session) => {
          const active = session.id === activeSessionId;
          const busy = busyId === session.id;
          return (
            <div
              key={session.id}
              data-testid={`companion-session-${session.id}`}
              className="rounded-[12px] border px-3 py-2.5"
              style={{
                borderColor: active ? 'var(--domain-core)' : 'var(--border-subtle)',
                backgroundColor: active ? 'var(--bg-active)' : 'var(--bg-surface)',
              }}
            >
              <button
                type="button"
                disabled={busy}
                onClick={() => onOpenSession(session.id)}
                className="block w-full cursor-pointer text-left disabled:cursor-wait disabled:opacity-60"
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="min-w-0 flex-1 truncate text-[14.5px] font-semibold">
                    {session.title || '未命名会话'}
                  </span>
                  <span className="shrink-0 font-mono text-[12.5px] text-[var(--text-subtle)]">
                    {sessionTimeLabel(session.updated_at)}
                  </span>
                </div>
                <div className="mt-1 line-clamp-2 text-[13.5px] leading-[1.5] text-[var(--text-muted)]">
                  {session.last_message || session.context_title || '暂无消息预览'}
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5 font-mono text-[12.5px] text-[var(--text-subtle)]">
                  {session.context_title ? <span>{session.context_title}</span> : null}
                  <span>· {session.message_count} 条</span>
                  {session.served_model ? <span>· {session.served_model}</span> : null}
                </div>
              </button>
              <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-[var(--border-subtle)] pt-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onExportSession(session.id, 'markdown')}
                  aria-label={`导出 Markdown：${session.title}`}
                  className="inline-flex cursor-pointer items-center gap-1 rounded-[7px] px-2 py-1 text-[12.5px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)] disabled:opacity-45"
                >
                  <Download className="h-3 w-3" aria-hidden="true" />
                  Markdown
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onExportSession(session.id, 'json')}
                  aria-label={`导出 JSON：${session.title}`}
                  className="inline-flex cursor-pointer items-center gap-1 rounded-[7px] px-2 py-1 text-[12.5px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)] disabled:opacity-45"
                >
                  <Download className="h-3 w-3" aria-hidden="true" />
                  JSON
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onDeleteSession(session.id)}
                  aria-label={`删除会话：${session.title}`}
                  className="ml-auto inline-flex cursor-pointer items-center gap-1 rounded-[7px] px-2 py-1 text-[12.5px] text-[var(--status-err-fg)] hover:bg-[var(--status-err-bg)] disabled:opacity-45"
                >
                  <Trash2 className="h-3 w-3" aria-hidden="true" />
                  删除
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="border-t border-[var(--border-subtle)] p-3">
        <button
          type="button"
          onClick={onNewSession}
          className="inline-flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-[9px] bg-[var(--bg-active)] px-3 py-2 text-[14px] font-medium text-[var(--text-primary)] hover:brightness-95"
        >
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          新建会话
        </button>
        <p className="mt-2 text-center text-[12.5px] leading-[1.5] text-[var(--text-subtle)]">
          会话保存在本机插件数据目录，可随时导出或继续。
        </p>
      </div>
    </section>
  );
}

export default CompanionSessionPanel;
