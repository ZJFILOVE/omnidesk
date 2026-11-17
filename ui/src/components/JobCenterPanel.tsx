import { useMemo, useState } from 'react';
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  X,
} from 'lucide-react';
import type { JobRecord } from '../api';
import { isActiveJob, jobErrorText, type JobCenterState } from '../hooks/useJobCenter';

function stateLabel(state: string): string {
  switch (state) {
    case 'pending':
      return '排队中';
    case 'running':
      return '执行中';
    case 'completed':
      return '已完成';
    case 'failed':
      return '失败';
    case 'cancelled':
      return '已取消';
    default:
      return state || '未知';
  }
}

function stateTone(state: string): { bg: string; fg: string } {
  if (state === 'completed') return { bg: 'var(--status-ok-bg)', fg: 'var(--status-ok-fg)' };
  if (state === 'failed') return { bg: 'var(--status-err-bg)', fg: 'var(--status-err-fg)' };
  if (state === 'cancelled') return { bg: 'var(--bg-soft)', fg: 'var(--text-muted)' };
  return { bg: 'var(--status-warn-bg)', fg: 'var(--status-warn-fg)' };
}

function formatTime(value?: number | null): string {
  if (!value) return '—';
  return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
}

function JobResult({ job }: { job: JobRecord }) {
  const [open, setOpen] = useState(false);
  if (job.result === undefined || job.result === null) return null;
  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="cursor-pointer text-[13px] font-medium text-[var(--focus-ring)]"
      >
        {open ? '收起结果' : '查看结果'}
      </button>
      {open ? (
        <pre className="mt-2 max-h-48 overflow-auto rounded-[9px] bg-[var(--bg-sunken)] p-3 font-mono text-[12.5px] leading-[1.55] text-[var(--text-secondary)]">
          {JSON.stringify(job.result, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

export interface JobCenterPanelProps {
  open: boolean;
  onClose: () => void;
  state: JobCenterState;
}

export function JobCenterPanel({ open, onClose, state }: JobCenterPanelProps) {
  const [filter, setFilter] = useState<'all' | 'active' | 'terminal'>('all');
  const jobs = useMemo(() => {
    if (filter === 'active') return state.jobs.filter(isActiveJob);
    if (filter === 'terminal') return state.jobs.filter((job) => !isActiveJob(job));
    return state.jobs;
  }, [filter, state.jobs]);
  const activeCount = state.jobs.filter(isActiveJob).length;

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-[var(--backdrop-scrim)] p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label="任务中心"
        data-testid="job-center-panel"
        className="omni-theme-surface flex max-h-[82vh] w-full max-w-3xl flex-col overflow-hidden rounded-[18px] bg-[var(--bg-elevated)] text-[var(--text-primary)]"
        style={{ boxShadow: 'var(--shadow-elevated)' }}
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border-subtle)] px-5 py-4">
          <div>
            <h2 className="text-[19px] font-semibold tracking-tight">任务中心</h2>
            <p className="mt-0.5 text-[13.5px] text-[var(--text-muted)]">
              JobCenter 实时状态 · {activeCount} 个执行中 · 共 {state.jobs.length} 个
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void state.refresh()}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-[13.5px] text-[var(--text-muted)] hover:text-[var(--text-primary)]"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${state.loading ? 'animate-spin' : ''}`} />
              刷新
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭任务中心"
              className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="flex gap-1.5 border-b border-[var(--border-subtle)] px-5 py-2.5">
          {(
            [
              ['all', `全部 ${state.jobs.length}`],
              ['active', `进行中 ${activeCount}`],
              ['terminal', `已结束 ${state.jobs.length - activeCount}`],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              aria-pressed={filter === id}
              onClick={() => setFilter(id)}
              className="cursor-pointer rounded-full px-3 py-1 text-[13.5px] font-medium"
              style={{
                backgroundColor: filter === id ? 'var(--bg-soft)' : 'transparent',
                color: filter === id ? 'var(--text-primary)' : 'var(--text-muted)',
              }}
            >
              {label}
            </button>
          ))}
        </div>

        {state.error ? (
          <div className="mx-5 mt-3 flex items-start gap-2 rounded-[9px] bg-[var(--status-err-bg)] px-3 py-2 text-[13.5px] text-[var(--status-err-fg)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              [{state.error.code}] {state.error.message}
            </span>
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {jobs.length === 0 ? (
            <div className="rounded-[12px] border border-dashed border-[var(--border-line)] px-5 py-12 text-center text-[14px] text-[var(--text-muted)]">
              暂无后台任务。双仓同步、批量巡检等操作提交后会在这里显示进度与结果。
            </div>
          ) : (
            <div className="flex flex-col gap-2.5">
              {jobs.map((job) => {
                const tone = stateTone(job.state);
                const active = isActiveJob(job);
                return (
                  <article
                    key={job.id}
                    className="rounded-[12px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="truncate text-[15.5px] font-semibold">
                            {job.title || job.kind}
                          </h3>
                          <span
                            className="rounded-full px-2 py-0.5 text-[12.5px] font-medium"
                            style={{ backgroundColor: tone.bg, color: tone.fg }}
                          >
                            {stateLabel(job.state)}
                          </span>
                          <span className="rounded-full bg-[var(--bg-soft)] px-2 py-0.5 font-mono text-[12.5px] text-[var(--text-muted)]">
                            {job.kind}
                          </span>
                        </div>
                        <p className="mt-1 font-mono text-[12.5px] text-[var(--text-subtle)]">
                          {job.id.slice(0, 12)} · {job.pluginId || 'core'} · {job.stage}
                        </p>
                      </div>
                      {active ? (
                        <button
                          type="button"
                          onClick={() => void state.cancel(job.id)}
                          className="inline-flex cursor-pointer items-center gap-1 rounded-[7px] border border-[var(--border-line)] px-2 py-1 text-[13px] text-[var(--status-err-fg)] hover:bg-[var(--status-err-bg)]"
                        >
                          <Ban className="h-3.5 w-3.5" />
                          取消
                        </button>
                      ) : job.state === 'completed' ? (
                        <CheckCircle2 className="h-4 w-4 text-[var(--status-ok-fg)]" />
                      ) : null}
                    </div>

                    <div className="mt-3 flex items-center gap-2">
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--bg-soft)]">
                        <div
                          className="h-full rounded-full transition-[width]"
                          style={{
                            width: `${Math.max(2, Math.round(job.progress * 100))}%`,
                            backgroundColor: tone.fg,
                          }}
                        />
                      </div>
                      <span className="w-12 text-right font-mono text-[12.5px] text-[var(--text-muted)]">
                        {Math.round(job.progress * 100)}%
                      </span>
                      {job.totalSteps ? (
                        <span className="font-mono text-[12.5px] text-[var(--text-subtle)]">
                          {job.completedSteps ?? 0}/{job.totalSteps}
                        </span>
                      ) : null}
                    </div>

                    {job.message ? (
                      <p className="mt-2 text-[13.5px] text-[var(--text-muted)]">{job.message}</p>
                    ) : null}
                    {active && state.loading ? (
                      <span className="mt-2 inline-flex items-center gap-1 text-[12.5px] text-[var(--text-subtle)]">
                        <LoaderCircle className="h-3 w-3 animate-spin" /> 同步中
                      </span>
                    ) : null}
                    {job.error ? (
                      <p className="mt-2 rounded-[8px] bg-[var(--status-err-bg)] px-2.5 py-2 text-[13px] text-[var(--status-err-fg)]">
                        {jobErrorText(job)}
                      </p>
                    ) : null}
                    <JobResult job={job} />
                    <p className="mt-2 font-mono text-[12px] text-[var(--text-subtle)]">
                      创建 {formatTime(job.createdAt)} · 更新 {formatTime(job.updatedAt)}
                    </p>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

export default JobCenterPanel;
