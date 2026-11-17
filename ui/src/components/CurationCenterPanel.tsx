import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  LoaderCircle,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from 'lucide-react';
import {
  callCapability,
  formatRpcError,
  getDateBundle,
  isTauriAvailable,
  listArchiveDates,
  searchArchive,
  syncCurationArchive,
  type ArchiveDateStats,
  type CurationBundle,
  type CurationDomainFilter,
  type RpcErrorInfo,
} from '../api';
import type { WorkbenchId } from '../types';

const DOMAIN_OPTIONS: Array<{ id: CurationDomainFilter; label: string }> = [
  { id: 'all', label: '全部领域' },
  { id: 'music', label: '音乐' },
  { id: 'youtube', label: '视频' },
  { id: 'news', label: '新闻' },
  { id: 'reddit', label: 'Reddit' },
  { id: 'journal', label: '期刊' },
  { id: 'english', label: '英语' },
];

const PROJECTS = [
  {
    id: 'news-youtube-reddit',
    label: '新闻 / 视频 / Reddit',
    time: '08:00',
    counts: ['news', 'youtube', 'reddit'] as const,
  },
  { id: 'english', label: '英语精读', time: '08:30', counts: ['english'] as const },
  { id: 'music', label: '每日 5 首', time: '09:00', counts: ['music'] as const },
  { id: 'journal', label: '科学期刊', time: '09:30', counts: ['journal'] as const },
] as const;

type ProjectId = (typeof PROJECTS)[number]['id'];

function isoDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function todayIso(): string {
  return isoDate(new Date());
}

function addDays(date: string, delta: number): string {
  const value = new Date(`${date}T12:00:00`);
  value.setDate(value.getDate() + delta);
  return isoDate(value);
}

function dateCount(row: ArchiveDateStats | undefined, domain: CurationDomainFilter): number {
  if (!row || domain === 'all') {
    return row
      ? row.music + row.youtube + row.news + row.reddit + row.journal + row.english
      : 0;
  }
  return row[domain];
}

function projectCount(row: ArchiveDateStats | undefined, project: ProjectId): number {
  if (!row) return 0;
  return PROJECTS.find((entry) => entry.id === project)?.counts.reduce(
    (sum, key) => sum + row[key],
    0,
  ) ?? 0;
}

function calendarCells(monthCursor: string): string[] {
  const start = new Date(`${monthCursor}-01T12:00:00`);
  const firstWeekday = (start.getDay() + 6) % 7;
  const first = new Date(start);
  first.setDate(first.getDate() - firstWeekday);
  return Array.from({ length: 42 }, (_, index) => {
    const value = new Date(first);
    value.setDate(value.getDate() + index);
    return isoDate(value);
  });
}

function itemTitle(item: Record<string, unknown>): string {
  return String(item.title ?? item.name ?? item.item_id ?? item.id ?? '未命名条目');
}

function itemSubtitle(item: Record<string, unknown>): string {
  return String(
    item.creator ??
      item.author ??
      item.source_name ??
      item.subtitle ??
      item.source_id ??
      '',
  );
}

export interface CurationCenterPanelProps {
  open: boolean;
  onClose: () => void;
  onNavigateWorkbench?: (id: WorkbenchId, payload?: Record<string, unknown>) => void;
}

export function CurationCenterPanel({
  open,
  onClose,
  onNavigateWorkbench,
}: CurationCenterPanelProps) {
  const available = isTauriAvailable();
  const [domain, setDomain] = useState<CurationDomainFilter>('all');
  const [monthCursor, setMonthCursor] = useState(todayIso().slice(0, 7));
  const [selectedDate, setSelectedDate] = useState(todayIso());
  const [dates, setDates] = useState<ArchiveDateStats[]>([]);
  const [bundle, setBundle] = useState<CurationBundle | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<
    Array<{ domain: string; item: Record<string, unknown> }>
  >([]);
  const [loading, setLoading] = useState(false);
  const [bundleLoading, setBundleLoading] = useState(false);
  const [actionStatus, setActionStatus] = useState('');
  const [error, setError] = useState<RpcErrorInfo | null>(null);
  const [skillStatus, setSkillStatus] = useState<Record<string, unknown> | null>(null);

  const [project, setProject] = useState<ProjectId>('news-youtube-reddit');
  const [evidenceText, setEvidenceText] = useState('');
  const [payloadExtra, setPayloadExtra] = useState('');
  const [pipelineBusy, setPipelineBusy] = useState(false);

  const dateMap = useMemo(
    () => new Map(dates.map((entry) => [entry.date, entry])),
    [dates],
  );
  const filteredDates = useMemo(() => {
    if (domain === 'all') return dates;
    return dates.filter((entry) => dateCount(entry, domain) > 0);
  }, [dates, domain]);
  const todayRow = dateMap.get(todayIso());
  const selectedRow = dateMap.get(selectedDate);
  const cells = useMemo(() => calendarCells(monthCursor), [monthCursor]);

  const loadDates = useCallback(async () => {
    if (!available) {
      setDates([]);
      setError({ code: 'tauri_unavailable', message: '离线预览不可读取推荐归档' });
      return;
    }
    setLoading(true);
    setError(null);
    const result = await listArchiveDates({ limit: 0 });
    setLoading(false);
    if (!result.available) {
      setError({ code: 'tauri_unavailable', message: 'Core 控制通道不可用' });
      return;
    }
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setDates(result.data.dates);
    if (result.data.dates.length > 0) {
      setSelectedDate((previous) =>
        result.data.dates.some((entry) => entry.date === previous)
          ? previous
          : result.data.dates[0].date,
      );
    }
  }, [available]);

  useEffect(() => {
    if (!open) return;
    void loadDates();
    if (available) {
      void callCapability<Record<string, unknown>>({
        capability: 'ai.curation.v1',
        providerPluginId: 'plugin-magpie-agent',
        payload: { action: 'status' },
      }).then((result) => {
        if (result.ok) setSkillStatus(result.data);
      });
    }
  }, [available, loadDates, open]);

  useEffect(() => {
    if (!open || !available || !selectedDate) return undefined;
    let cancelled = false;
    setBundleLoading(true);
    void getDateBundle(selectedDate).then((result) => {
      if (cancelled) return;
      setBundleLoading(false);
      if (result.ok) {
        setBundle(result.data);
        setError(null);
      } else if (result.available) {
        setBundle(null);
        setError(result.error);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [available, open, selectedDate]);

  const gaps = useMemo(() => {
    const today = todayIso();
    const out: Array<{ date: string; project: ProjectId; label: string }> = [];
    for (let offset = 0; offset < 7; offset += 1) {
      const date = addDays(today, -offset);
      const row = dateMap.get(date);
      for (const entry of PROJECTS) {
        if (projectCount(row, entry.id) === 0) {
          out.push({ date, project: entry.id, label: entry.label });
        }
      }
    }
    return out;
  }, [dateMap]);

  const runArchiveSync = async () => {
    if (!available) {
      setActionStatus('离线预览不能同步双仓');
      return;
    }
    setActionStatus('正在同步 obsidian-vault 与 music-daily…');
    const result = await syncCurationArchive();
    if (!result.ok) {
      setActionStatus(`同步失败：${formatRpcError(result.error)}`);
      return;
    }
    setActionStatus(
      `双仓同步完成 · 索引 ${String(result.data.dates_indexed ?? '—')} 个日期`,
    );
    await loadDates();
  };

  const runSearch = async () => {
    const query = searchQuery.trim();
    if (!query) {
      setSearchResults([]);
      return;
    }
    if (!available) {
      setActionStatus('离线预览不能检索归档');
      return;
    }
    setLoading(true);
    const result = await searchArchive({ query, domain, date: selectedDate, limit: 60 });
    setLoading(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setSearchResults([
      ...result.data.media.map((item) => ({ domain: 'media', item })),
      ...result.data.texts.map((item) => ({ domain: 'text', item })),
      ...result.data.books.map((item) => ({ domain: 'book', item })),
    ]);
  };

  const runPipeline = async (dryRun: boolean) => {
    if (!available) {
      setActionStatus('离线预览不能触发每日流水线');
      return;
    }
    let evidence: Record<string, unknown>;
    let extra: Record<string, unknown>;
    try {
      evidence = JSON.parse(evidenceText || '{}') as Record<string, unknown>;
      extra = JSON.parse(payloadExtra || '{}') as Record<string, unknown>;
    } catch (err) {
      setActionStatus(`JSON 解析失败：${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    setPipelineBusy(true);
    setActionStatus(dryRun ? '正在执行发布事务 dry-run 校验…' : '正在执行每日不可变流水线…');
    const result = await callCapability<Record<string, unknown>>({
      capability: 'ai.curation.v1',
      providerPluginId: 'plugin-magpie-agent',
      payload: {
        ...extra,
        action: 'run_daily_pipeline',
        mode: 'daily_immutable',
        project,
        business_date: selectedDate,
        operation_id: `${project}:${selectedDate}`,
        qa_verified: true,
        publication_evidence: evidence,
        dry_run: dryRun,
      },
      timeoutMs: 300000,
    });
    setPipelineBusy(false);
    if (!result.ok) {
      setActionStatus(
        `${dryRun ? '校验' : '发布'}失败：[${result.error.code}] ${result.error.message}`,
      );
      return;
    }
    setActionStatus(
      dryRun
        ? 'dry-run 校验通过；未写入每日账本，也未同步歌单。'
        : `已发布 ${project} · ${selectedDate}。`,
    );
    await loadDates();
  };

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
        aria-label="推荐日历与定时调度"
        data-testid="curation-center-panel"
        className="omni-theme-surface flex max-h-[90vh] w-full max-w-6xl flex-col overflow-hidden rounded-[18px] bg-[var(--bg-elevated)] text-[var(--text-primary)]"
        style={{ boxShadow: 'var(--shadow-elevated)' }}
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border-subtle)] px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-[19px] font-semibold tracking-tight">
              <CalendarDays className="h-5 w-5 text-[var(--domain-core)]" />
              推荐日历与定时调度
            </h2>
            <p className="mt-1 text-[13.5px] text-[var(--text-muted)]">
              按日期与领域浏览归档、检查缺口、验证真实发布证据后执行补跑。
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void runArchiveSync()}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-[13.5px] text-[var(--text-muted)]"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              双仓同步
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭推荐日历"
              className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        {error ? (
          <div className="mx-5 mt-3 flex items-start gap-2 rounded-[9px] bg-[var(--status-err-bg)] px-3 py-2 text-[13.5px] text-[var(--status-err-fg)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              [{error.code}] {error.message}
            </span>
          </div>
        ) : null}
        {actionStatus ? (
          <div className="mx-5 mt-3 rounded-[9px] bg-[var(--bg-soft)] px-3 py-2 text-[13.5px] text-[var(--text-secondary)]">
            {actionStatus}
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
            <section className="rounded-[14px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    aria-label="上一个月"
                    onClick={() => {
                      const value = new Date(`${monthCursor}-01T12:00:00`);
                      value.setMonth(value.getMonth() - 1);
                      setMonthCursor(isoDate(value).slice(0, 7));
                    }}
                    className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[7px] hover:bg-[var(--hover-fill)]"
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </button>
                  <h3 className="min-w-28 text-center font-mono text-[15px] font-semibold">
                    {monthCursor}
                  </h3>
                  <button
                    type="button"
                    aria-label="下一个月"
                    onClick={() => {
                      const value = new Date(`${monthCursor}-01T12:00:00`);
                      value.setMonth(value.getMonth() + 1);
                      setMonthCursor(isoDate(value).slice(0, 7));
                    }}
                    className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[7px] hover:bg-[var(--hover-fill)]"
                  >
                    <ChevronRight className="h-4 w-4" />
                  </button>
                </div>
                <div className="flex flex-wrap gap-1">
                  {DOMAIN_OPTIONS.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      aria-pressed={domain === option.id}
                      onClick={() => setDomain(option.id)}
                      className="cursor-pointer rounded-full px-2.5 py-1 text-[12.5px] font-medium"
                      style={{
                        backgroundColor:
                          domain === option.id ? 'var(--bg-soft)' : 'transparent',
                        color:
                          domain === option.id
                            ? 'var(--text-primary)'
                            : 'var(--text-muted)',
                      }}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="mt-3 grid grid-cols-7 text-center text-[12px] font-medium text-[var(--text-subtle)]">
                {['一', '二', '三', '四', '五', '六', '日'].map((day) => (
                  <span key={day} className="py-1">
                    {day}
                  </span>
                ))}
              </div>
              <div className="grid grid-cols-7 gap-1">
                {cells.map((date) => {
                  const row = dateMap.get(date);
                  const count = dateCount(row, domain);
                  const inMonth = date.startsWith(monthCursor);
                  return (
                    <button
                      key={date}
                      type="button"
                      onClick={() => setSelectedDate(date)}
                      aria-pressed={selectedDate === date}
                      className="relative min-h-14 cursor-pointer rounded-[9px] border px-1.5 py-1 text-left transition-colors hover:bg-[var(--hover-fill)]"
                      style={{
                        opacity: inMonth ? 1 : 0.42,
                        borderColor:
                          selectedDate === date
                            ? 'var(--focus-ring)'
                            : 'var(--border-subtle)',
                        backgroundColor:
                          selectedDate === date
                            ? 'var(--focus-fill)'
                            : 'var(--bg-canvas)',
                      }}
                    >
                      <span className="font-mono text-[12.5px] text-[var(--text-secondary)]">
                        {Number(date.slice(-2))}
                      </span>
                      {count > 0 ? (
                        <span className="absolute bottom-1.5 left-1.5 right-1.5 flex items-center justify-between gap-1">
                          <span
                            className="h-1.5 w-1.5 rounded-full"
                            style={{ backgroundColor: 'var(--domain-core)' }}
                          />
                          <span className="font-mono text-[11.5px] text-[var(--text-muted)]">
                            {count}
                          </span>
                        </span>
                      ) : null}
                    </button>
                  );
                })}
              </div>

              <div className="mt-4 grid grid-cols-2 gap-2">
                {PROJECTS.map((entry) => {
                  const count = projectCount(todayRow, entry.id);
                  const ok = count > 0;
                  return (
                    <div
                      key={entry.id}
                      className="rounded-[9px] border border-[var(--border-subtle)] bg-[var(--bg-canvas)] px-3 py-2"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-[13px] font-medium">{entry.label}</span>
                        <span
                          className="rounded-full px-1.5 py-0.5 text-[11.5px]"
                          style={{
                            backgroundColor: ok
                              ? 'var(--status-ok-bg)'
                              : 'var(--status-warn-bg)',
                            color: ok
                              ? 'var(--status-ok-fg)'
                              : 'var(--status-warn-fg)',
                          }}
                        >
                          {ok ? '已发布' : '今日缺失'}
                        </span>
                      </div>
                      <p className="mt-1 font-mono text-[11.5px] text-[var(--text-subtle)]">
                        {entry.time} Asia/Shanghai · {count} 项
                      </p>
                    </div>
                  );
                })}
              </div>
            </section>

            <section className="rounded-[14px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-[15.5px] font-semibold">补跑队列</h3>
                <span className="rounded-full bg-[var(--status-warn-bg)] px-2 py-0.5 text-[12.5px] text-[var(--status-warn-fg)]">
                  {gaps.length} 个缺口
                </span>
              </div>
              <p className="mt-1 text-[12.5px] leading-[1.55] text-[var(--text-muted)]">
                仅检测近 7 天归档缺口。补跑必须提供上游 PUBLISHED 事务收据，不会伪造发布证据。
              </p>
              <div className="mt-2 max-h-40 space-y-1 overflow-y-auto">
                {gaps.length === 0 ? (
                  <p className="rounded-[8px] bg-[var(--status-ok-bg)] px-2.5 py-2 text-[13px] text-[var(--status-ok-fg)]">
                    近 7 天未发现缺失项目。
                  </p>
                ) : (
                  gaps.slice(0, 16).map((gap) => (
                    <button
                      key={`${gap.date}:${gap.project}`}
                      type="button"
                      onClick={() => {
                        setSelectedDate(gap.date);
                        setProject(gap.project);
                        setMonthCursor(gap.date.slice(0, 7));
                        setEvidenceText('');
                      }}
                      className="flex w-full cursor-pointer items-center justify-between gap-2 rounded-[8px] bg-[var(--bg-canvas)] px-2.5 py-1.5 text-left text-[12.5px] hover:bg-[var(--hover-fill)]"
                    >
                      <span className="font-mono text-[var(--text-muted)]">{gap.date}</span>
                      <span className="truncate">{gap.label}</span>
                      <Play className="h-3 w-3 shrink-0 text-[var(--domain-core)]" />
                    </button>
                  ))
                )}
              </div>
              <div className="mt-3 rounded-[9px] bg-[var(--bg-canvas)] px-2.5 py-2 text-[12.5px] text-[var(--text-muted)]">
                <span className="inline-flex items-center gap-1.5">
                  <ShieldCheck className="h-3.5 w-3.5" />
                  Skill：{skillStatus?.skill_available === true ? '可用' : '未确认'} · 每日歌单：
                  {skillStatus?.media_playlist_available === true ? '可用' : '未确认'}
                </span>
              </div>
            </section>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
            <section className="rounded-[14px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="font-mono text-[15.5px] font-semibold">{selectedDate}</h3>
                <span className="text-[12.5px] text-[var(--text-muted)]">
                  {selectedRow
                    ? `${dateCount(selectedRow, 'all')} 个条目`
                    : '无归档统计'}
                </span>
              </div>
              <div className="mt-2 flex gap-1.5">
                <input
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void runSearch();
                  }}
                  placeholder="跨领域检索当天归档…"
                  aria-label="检索推荐归档"
                  className="min-w-0 flex-1 rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-canvas)] px-3 py-1.5 text-[13.5px] outline-none focus:border-[var(--focus-ring)]"
                />
                <button
                  type="button"
                  onClick={() => void runSearch()}
                  disabled={loading}
                  className="inline-flex cursor-pointer items-center gap-1 rounded-[8px] bg-[var(--bg-soft)] px-2.5 py-1.5 text-[13px]"
                >
                  {loading ? (
                    <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Search className="h-3.5 w-3.5" />
                  )}{' '}
                  检索
                </button>
              </div>
              {bundleLoading ? (
                <div className="mt-5 flex items-center justify-center gap-2 py-8 text-[13.5px] text-[var(--text-muted)]">
                  <LoaderCircle className="h-4 w-4 animate-spin" /> 加载日期包…
                </div>
              ) : searchResults.length > 0 ? (
                <div className="mt-3 max-h-72 space-y-1.5 overflow-y-auto">
                  {searchResults.map((entry, index) => (
                    <button
                      key={`${entry.domain}:${String(entry.item.id ?? index)}`}
                      type="button"
                      onClick={() =>
                        onNavigateWorkbench?.(
                          entry.domain === 'media'
                            ? 'media'
                            : entry.domain === 'book'
                              ? 'book'
                              : 'text',
                          entry.item,
                        )
                      }
                      className="block w-full cursor-pointer rounded-[8px] bg-[var(--bg-canvas)] px-2.5 py-2 text-left hover:bg-[var(--hover-fill)]"
                    >
                      <span className="line-clamp-1 text-[13.5px] font-medium">
                        {itemTitle(entry.item)}
                      </span>
                      <span className="mt-0.5 line-clamp-1 text-[12.5px] text-[var(--text-muted)]">
                        {entry.domain} · {itemSubtitle(entry.item)}
                      </span>
                    </button>
                  ))}
                </div>
              ) : bundle &&
                bundle.media_items.length + bundle.text_items.length + bundle.book_items.length >
                  0 ? (
                <div className="mt-3 max-h-72 space-y-1.5 overflow-y-auto">
                  {[
                    ...bundle.media_items.map((item) => ({ domain: 'media', item })),
                    ...bundle.text_items.map((item) => ({ domain: 'text', item })),
                    ...bundle.book_items.map((item) => ({ domain: 'book', item })),
                  ].map((entry, index) => (
                    <button
                      key={`${entry.domain}:${String(entry.item.id ?? index)}`}
                      type="button"
                      onClick={() =>
                        onNavigateWorkbench?.(
                          entry.domain === 'media'
                            ? 'media'
                            : entry.domain === 'book'
                              ? 'book'
                              : 'text',
                          entry.item,
                        )
                      }
                      className="block w-full cursor-pointer rounded-[8px] bg-[var(--bg-canvas)] px-2.5 py-2 text-left hover:bg-[var(--hover-fill)]"
                    >
                      <span className="line-clamp-1 text-[13.5px] font-medium">
                        {itemTitle(entry.item)}
                      </span>
                      <span className="mt-0.5 line-clamp-1 text-[12.5px] text-[var(--text-muted)]">
                        {entry.domain} · {itemSubtitle(entry.item)}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="mt-3 rounded-[9px] border border-dashed border-[var(--border-line)] px-3 py-8 text-center text-[13px] text-[var(--text-muted)]">
                  该日期暂无已发布条目。可从补跑队列选择项目并提供发布证据。
                </div>
              )}
              {filteredDates.length > 0 ? (
                <p className="mt-2 text-[12px] text-[var(--text-subtle)]">
                  当前筛选共 {filteredDates.length} 个日期有记录。
                </p>
              ) : null}
            </section>

            <section className="rounded-[14px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <h3 className="text-[15.5px] font-semibold">证据校验与执行</h3>
              <p className="mt-1 text-[12.5px] leading-[1.55] text-[var(--text-muted)]">
                调用 ai.curation.v1 / run_daily_pipeline。发布证据必须是上游真实收据；
                缺证据会 fail-closed 返回 needs_verified_bundle。
              </p>
              <div className="mt-3 grid grid-cols-2 gap-2">
                <label className="text-[12.5px] font-medium text-[var(--text-muted)]">
                  项目
                  <select
                    value={project}
                    onChange={(event) => setProject(event.target.value as ProjectId)}
                    className="mt-1 w-full rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-canvas)] px-2.5 py-1.5 text-[13.5px] text-[var(--text-primary)]"
                  >
                    {PROJECTS.map((entry) => (
                      <option key={entry.id} value={entry.id}>
                        {entry.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-[12.5px] font-medium text-[var(--text-muted)]">
                  日期
                  <input
                    type="date"
                    value={selectedDate}
                    onChange={(event) => setSelectedDate(event.target.value)}
                    className="mt-1 w-full rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-canvas)] px-2.5 py-1.5 font-mono text-[13px] text-[var(--text-primary)]"
                  />
                </label>
              </div>
              <label className="mt-2 block text-[12.5px] font-medium text-[var(--text-muted)]">
                publication_evidence JSON（必填，需含 PUBLISHED 收据）
                <textarea
                  value={evidenceText}
                  onChange={(event) => setEvidenceText(event.target.value)}
                  rows={5}
                  spellCheck={false}
                  placeholder='{"status":"PUBLISHED","publication_verified":true,"operation_id":"...","receipt":{...}}'
                  className="mt-1 w-full resize-y rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-canvas)] px-2.5 py-2 font-mono text-[12px] text-[var(--text-primary)] outline-none focus:border-[var(--focus-ring)]"
                />
              </label>
              <label className="mt-2 block text-[12.5px] font-medium text-[var(--text-muted)]">
                额外载荷 JSON（可选：media_items / text_items / track_ids）
                <textarea
                  value={payloadExtra}
                  onChange={(event) => setPayloadExtra(event.target.value)}
                  rows={3}
                  spellCheck={false}
                  placeholder='{"media_items":[...],"text_items":[...]}'
                  className="mt-1 w-full resize-y rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-canvas)] px-2.5 py-2 font-mono text-[12px] text-[var(--text-primary)] outline-none focus:border-[var(--focus-ring)]"
                />
              </label>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={pipelineBusy || !available}
                  onClick={() => void runPipeline(true)}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-line)] px-3 py-1.5 text-[13.5px] font-medium hover:bg-[var(--hover-fill)] disabled:opacity-50"
                >
                  {pipelineBusy ? (
                    <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <ShieldCheck className="h-3.5 w-3.5" />
                  )}
                  dry-run 校验
                </button>
                <button
                  type="button"
                  disabled={pipelineBusy || !available}
                  onClick={() => void runPipeline(false)}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] bg-[var(--domain-core)] px-3 py-1.5 text-[13.5px] font-medium text-white hover:brightness-95 disabled:opacity-50"
                >
                  {pipelineBusy ? (
                    <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                  正式发布
                </button>
              </div>
            </section>
          </div>
        </div>
      </section>
    </div>
  );
}

export default CurationCenterPanel;
