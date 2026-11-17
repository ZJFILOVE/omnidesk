import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, Blocks, LoaderCircle, RefreshCw } from 'lucide-react';
import { formatRpcError, invokeWorkspaceAction, type WorkspaceTabSnapshot } from '../api';
import { DeclarativePanel } from '../components/DeclarativePanel';
import { FocusItem } from '../components/FocusItem';
import { DOMAIN_CSS_VARS, DOMAIN_LABELS } from '../theme';
import { PluginResultView, resolveBody } from './PluginResultView';
import { LoadMoreButton } from '../components/LoadMoreButton';
import { EmbeddedWebReader } from '../components/EmbeddedWebReader';

type Dict = Record<string, unknown>;

const collectActionIds = (schema: unknown): string[] => {
  const found: string[] = [];
  const visit = (node: unknown) => {
    if (Array.isArray(node)) {
      node.forEach(visit);
      return;
    }
    if (!node || typeof node !== 'object') return;
    const record = node as Dict;
    if (Array.isArray(record.actions)) {
      for (const action of record.actions) {
        if (action && typeof action === 'object') {
          const id = String((action as Dict).id ?? '');
          if (id) found.push(id);
        }
      }
    }
    visit(record.children);
  };
  visit(schema);
  return found;
};

export interface PluginWorkspaceProps {
  tab: WorkspaceTabSnapshot;
  isActive?: boolean;
  tauriAvailable?: boolean;
  onRefreshExtensions?: () => void;
  webviewVisible?: boolean;
}

export function PluginWorkspace({
  tab,
  isActive = true,
  tauriAvailable = true,
  onRefreshExtensions,
  webviewVisible = true,
}: PluginWorkspaceProps) {
  const [schema, setSchema] = useState(tab.ui_schema ?? {});
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<Dict | null>(null);
  const [lastActionId, setLastActionId] = useState('');
  const autoLoadedRef = useRef<string>('');
  const [feedRequest, setFeedRequest] = useState<{ actionId: string; state: Dict; page: number; hasMore: boolean } | null>(null);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const requestVersion = useRef(0);
  const busyRef = useRef(false);
  const [webMode, setWebMode] = useState(tab.plugin_id === 'plugin-linuxdo');
  const scrollRef = useRef<HTMLDivElement>(null);
  const [listSnapshot, setListSnapshot] = useState<{ result: Dict | null; actionId: string; feed: typeof feedRequest; scrollTop: number } | null>(null);

  useEffect(() => () => { requestVersion.current += 1; busyRef.current = false; }, [tab.plugin_id, tab.tab_id]);

  useEffect(() => {
    setSchema(tab.ui_schema ?? {});
  }, [tab]);

  const handleAction = useCallback(
    async (actionId: string, state: Dict, append = false) => {
      if (!isActive || busyRef.current) return;
      if (!tauriAvailable) {
        setActionError('离线预览模式不会提交插件动作。');
        return;
      }
      setPendingAction(actionId);
      busyRef.current = true;
      const version = ++requestVersion.current;
      setLoadingMore(append);
      setLoadMoreError(null);
      setActionError(null);
      const fail = (message: string) => append ? setLoadMoreError(message) : setActionError(message);
      try {
        const res = await invokeWorkspaceAction({
          kind: 'workspace_tab',
          specId: tab.tab_id,
          pluginId: tab.plugin_id,
          actionId,
          state,
          timeoutMs: 30000,
        });
        if (version !== requestVersion.current) return;
        if (!res.available) {
          fail('Core 控制通道不可用。');
          return;
        }
        if (!res.ok) {
          fail(formatRpcError(res.error));
          return;
        }
        if (res.data.ok === false) {
          const code = String(res.data.error_code ?? 'plugin_action_failed');
          const message = String(res.data.error ?? '插件动作执行失败');
          fail(`[${code}] ${message}`);
          return;
        }
        const body = resolveBody(res.data);
        const isFeed = ['fetch_feed', 'fetch_items', 'search', 'refresh'].includes(actionId) && Array.isArray(body.items);
        if (isFeed) {
          const incoming = body.items as Dict[];
          const previous = append ? (resolveBody(actionResult).items as Dict[] ?? []) : [];
          const key = (item: Dict) => String(item.id ?? item.url ?? JSON.stringify(item));
          const seen = new Set(previous.map(key));
          const additions = incoming.filter((item) => {
            const id = key(item);
            if (seen.has(id)) return false;
            seen.add(id);
            return true;
          });
          const page = typeof state.page === 'number' ? state.page : 1;
          setFeedRequest({ actionId, state, page: append && additions.length === 0 ? feedRequest?.page ?? 1 : page,
            hasMore: body.has_more !== false && body.is_end !== true && additions.length > 0 });
          setActionResult({ ...res.data, result: { ...body, items: [...previous, ...additions] } });
        } else {
          if (actionId === 'fetch_article' && feedRequest) {
            setListSnapshot({ result: actionResult, actionId: lastActionId, feed: feedRequest, scrollTop: scrollRef.current?.scrollTop ?? 0 });
            scrollRef.current?.scrollTo?.({ top: 0 });
          }
          setFeedRequest(null);
          setActionResult(res.data);
        }
        setLastActionId(actionId);
        const nextSchema = res.data.ui_schema;
        if (nextSchema && (typeof nextSchema === 'object' || Array.isArray(nextSchema))) {
          setSchema(nextSchema as Record<string, unknown> | Array<Record<string, unknown>>);
        }
      } catch (err) {
        if (version === requestVersion.current) fail(err instanceof Error ? err.message : String(err));
      } finally {
        if (version === requestVersion.current) {
          busyRef.current = false;
          setPendingAction(null);
          setLoadingMore(false);
        }
      }
    },
    [isActive, tab.plugin_id, tab.tab_id, tauriAvailable, actionResult, feedRequest, lastActionId],
  );

  useEffect(() => {
    if (!isActive || !tauriAvailable || webMode) return;
    const actions = collectActionIds(schema);
    const autoAction = ['fetch_feed', 'fetch_items', 'list_books', 'refresh'].find((id) =>
      actions.includes(id),
    );
    if (!autoAction) return;
    const key = `${tab.plugin_id}:${tab.tab_id}:${autoAction}`;
    if (autoLoadedRef.current === key) return;
    autoLoadedRef.current = key;
    void handleAction(autoAction, {});
  }, [handleAction, isActive, schema, tab.plugin_id, tab.tab_id, tauriAvailable, webMode]);

  return (
    <div
      data-testid="plugin-workspace"
      data-plugin-tab-id={tab.tab_id}
      className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-[var(--bg-canvas)]"
    >
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--border-subtle)] px-6 py-4">
        <div className="flex min-w-0 items-start gap-3">
          <span
            className="mt-0.5 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px]"
            style={{
              color: DOMAIN_CSS_VARS[tab.domain],
              backgroundColor: `color-mix(in srgb, ${DOMAIN_CSS_VARS[tab.domain]} 12%, var(--bg-surface))`,
            }}
            aria-hidden="true"
          >
            <Blocks className="h-4.5 w-4.5" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate text-[22px] font-semibold tracking-tight text-[var(--text-primary)]">
                {tab.title}
              </h1>
              {tab.badge ? (
                <span className="rounded-full bg-[var(--bg-soft)] px-2.5 py-0.5 font-mono text-[12.5px] font-medium text-[var(--text-muted)]">
                  {tab.badge}
                </span>
              ) : null}
              <span className="rounded-full bg-[var(--bg-soft)] px-2.5 py-0.5 text-[12.5px] text-[var(--text-muted)]">
                {DOMAIN_LABELS[tab.domain]}
              </span>
            </div>
            <p className="mt-1 truncate font-mono text-[12.5px] text-[var(--text-subtle)]">
              {tab.plugin_id} · {tab.tab_id}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
        {tab.plugin_id === 'plugin-linuxdo' ? <button type="button" onClick={() => setWebMode((value) => !value)}
          className="min-h-11 rounded-lg border border-[var(--border-line)] px-3 text-[14px] text-[var(--text-primary)]">
          {webMode ? '切换帖子列表' : '内嵌网页'}
        </button> : null}
        <FocusItem
          as="button"
          id={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}:refresh`}
          zone="main"
          label="刷新插件页签"
          onActivate={() => onRefreshExtensions?.()}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-[13.5px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          刷新
        </FocusItem>
        </div>
      </header>

      {webMode && tab.plugin_id === 'plugin-linuxdo' ? <EmbeddedWebReader active={isActive && webviewVisible} /> : <>

      {!tauriAvailable ? (
        <div className="mx-6 mt-4 flex items-center gap-2 rounded-[10px] bg-[var(--status-warn-bg)] px-3.5 py-2.5 text-[13.5px] text-[var(--status-warn-fg)]">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          离线预览模式：插件页签需要连接 Core 后才能执行动作。
        </div>
      ) : null}
      {actionError ? (
        <div
          role="alert"
          className="mx-6 mt-4 flex items-start gap-2 rounded-[10px] bg-[var(--status-err-bg)] px-3.5 py-2.5 text-[13.5px] text-[var(--status-err-fg)]"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{actionError}</span>
        </div>
      ) : null}

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-6 py-4.5">
        {listSnapshot && !feedRequest ? <FocusItem as="button" id={`plugin-workspace:${tab.tab_id}:back`}
          disabled={pendingAction !== null} onActivate={() => {
            setActionResult(listSnapshot.result);
            setLastActionId(listSnapshot.actionId);
            setFeedRequest(listSnapshot.feed);
            setActionError(null);
            const top = listSnapshot.scrollTop;
            requestAnimationFrame(() => { if (scrollRef.current) scrollRef.current.scrollTop = top; });
            setListSnapshot(null);
          }} className="mb-3 min-h-11 rounded-lg border border-[var(--border-line)] px-4 py-2 text-[var(--text-primary)] hover:bg-[var(--hover-fill)]">返回列表</FocusItem> : null}
        <div id={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}:root`} data-focus-id={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}:root`}>
          <DeclarativePanel
            schema={schema}
            focusPrefix={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}`}
            zone="main"
            busy={pendingAction !== null}
            onAction={(actionId, state) => {
              void handleAction(actionId, state);
            }}
          />
        </div>

        {pendingAction ? (
          <div className="mt-3 inline-flex items-center gap-2 rounded-full bg-[var(--bg-soft)] px-3 py-1.5 text-[13px] text-[var(--text-muted)]">
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            正在执行 {pendingAction}…
          </div>
        ) : null}
        <PluginResultView payload={actionResult} actionId={lastActionId}
          onOpenItem={tab.domain === 'text' && feedRequest ? (item) => {
            void handleAction('fetch_article', { article_id: item.id || item.url, url: item.url });
          } : undefined} />
        {feedRequest ? <LoadMoreButton id={`plugin-workspace:${tab.plugin_id}:${tab.tab_id}:more`}
          disabled={pendingAction !== null || !isActive || !tauriAvailable}
          pagination={{ ...feedRequest, loadingMore, loadMoreError, loadMore: () => {
            const state: Dict = { ...feedRequest.state, page: feedRequest.page + 1 };
            const body = resolveBody(actionResult);
            if (typeof body.next_cursor === 'string') state.cursor = body.next_cursor;
            if (typeof body.after === 'string') state.after = body.after;
            if ('force_refresh' in state) state.force_refresh = false;
            void handleAction(feedRequest.actionId, state, true);
          } }} /> : null}
      </div>
      </>}
    </div>
  );
}

export default PluginWorkspace;
