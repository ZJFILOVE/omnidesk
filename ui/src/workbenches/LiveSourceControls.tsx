import { isTauriAvailable } from '../api';
import type { LiveFeed } from '../hooks/useLiveDomain';
import { FocusButton, focusableProps } from '../components/WorkbenchLayout';

/** Source selection and recovery stay available even when a provider is empty or offline. */
export function LiveSourceControls({ domain, feed, onSourceChange, activeFocusId, onFocus }: {
  domain: 'media' | 'text' | 'book';
  feed: LiveFeed<unknown>;
  onSourceChange?: (sourceId: string) => void;
  activeFocusId: string;
  onFocus: (id: string) => void;
}) {
  if (!isTauriAvailable()) return null;
  const label = domain === 'text' ? '文章来源' : domain === 'book' ? '书籍来源' : '媒体来源';
  return (
    <div className="flex w-full flex-wrap items-center gap-2 text-[14px] text-[var(--text-muted)]" aria-busy={feed.loading}>
      {onSourceChange && feed.sources.length > 0 ? (
        <label className="flex min-w-0 items-center gap-2">
          <span>{label}</span>
          <select
            {...focusableProps(`${domain}:source`, activeFocusId, onFocus)}
            aria-label={label}
            value={feed.sourceId}
            onChange={(event) => onSourceChange(event.target.value)}
            className="max-w-full rounded-lg border border-[var(--border-line)] bg-[var(--bg-surface)] px-3 py-2 text-[var(--text-primary)]"
          >
            {feed.sources.map((source) => <option key={source.sourceId} value={source.sourceId}>{source.title || source.sourceId}</option>)}
          </select>
        </label>
      ) : null}
      <span role={feed.error ? 'alert' : 'status'} className={feed.error ? 'text-[var(--status-err-fg)]' : ''}>
        {feed.loading ? '正在读取真实数据…' : feed.error || (feed.live ? `LIVE · ${feed.items.length} 条${feed.items.length === 0 ? '，当前来源暂无内容，可切换来源或配置插件。' : ''}` : '等待数据源连接')}
      </span>
      <FocusButton focusId={`${domain}:source:reload`} activeFocusId={activeFocusId} onFocus={onFocus}
        onActivate={feed.reload} surface="soft" className="px-3 py-2" disabled={feed.loading}>
        刷新数据
      </FocusButton>
    </div>
  );
}
