import type {
  CallCapabilityArgs,
  CallRawArgs,
  CoreStatusData,
  EmitEventArgs,
  EmitEventData,
  EventBridgeSnapshot,
  ListPluginsArgs,
  ListPluginsData,
  MpvEmbedProbe,
  MpvFrameEnvelope,
  MpvOpenRequest,
  MpvPlaybackState,
  MpvProbeStatus,
  ReadSourceArgs,
  RpcErrorInfo,
  RpcResult,
  RuntimeStatus,
  SourceDomain,
  TauriGlobal,
  TauriUnlistenFn,
  WorkspaceSnapshotData,
  WorkspaceTabSnapshot,
  DockWidgetSnapshot,
  WorkspaceUiActionArgs,
} from './types';
import type { ContentDomain } from '../types';

export const MPV_STATE_CHANNEL = 'omnidesk:mpv-state';
export const MPV_FRAME_CHANNEL = 'omnidesk:mpv-frame';

function isRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === 'object' && val !== null && !Array.isArray(val);
}

function asNonEmptyString(val: unknown): string | undefined {
  return typeof val === 'string' && val.trim().length > 0 ? val : undefined;
}

export function getTauriGlobal(): TauriGlobal | null {
  if (typeof window === 'undefined') {
    return null;
  }
  const candidate = window.__TAURI__;
  return isRecord(candidate) ? (candidate as TauriGlobal) : null;
}

export function isTauriAvailable(): boolean {
  const tauri = getTauriGlobal();
  return typeof tauri?.core?.invoke === 'function';
}

export function isTauriEventsAvailable(): boolean {
  const tauri = getTauriGlobal();
  return typeof tauri?.event?.listen === 'function';
}

export function formatRpcError(error: RpcErrorInfo): string {
  return `[${error.code}] ${error.message}`;
}

function unavailableResult<T>(): RpcResult<T> {
  return {
    available: false,
    ok: false,
    error: {
      code: 'tauri_unavailable',
      message: '当前处于浏览器离线预览模式（未检测到 Tauri 运行时）',
    },
  };
}

function extractThrownError(err: unknown): RpcErrorInfo {
  if (isRecord(err)) {
    const code =
      asNonEmptyString(err.error_code) ??
      asNonEmptyString(err.code) ??
      'ipc_invoke_error';
    const message =
      asNonEmptyString(err.error) ??
      asNonEmptyString(err.message) ??
      JSON.stringify(err);
    return {
      code,
      message,
      data: isRecord(err.data) ? err.data : undefined,
    };
  }
  if (err instanceof Error) {
    return {
      code: 'ipc_invoke_error',
      message: err.message || err.name || 'Tauri IPC 调用失败',
    };
  }
  if (typeof err === 'string' && err.trim().length > 0) {
    return {
      code: 'ipc_invoke_error',
      message: err,
    };
  }
  return {
    code: 'ipc_invoke_error',
    message: '未知 IPC 调用异常',
  };
}

function normalizeEnvelope<T>(raw: unknown): RpcResult<T> {
  if (!isRecord(raw)) {
    return {
      available: true,
      ok: false,
      providerPluginId: '',
      error: {
        code: 'invalid_response',
        message: '后端返回了非对象响应',
      },
    };
  }

  if (typeof raw.ok === 'boolean') {
    const providerPluginId =
      typeof raw.provider_plugin_id === 'string' ? raw.provider_plugin_id : '';
    const dataObj = isRecord(raw.data) ? raw.data : undefined;

    if (raw.ok) {
      return {
        available: true,
        ok: true,
        data: (dataObj ?? {}) as T,
        providerPluginId,
        error: null,
      };
    }

    const detailObj = isRecord(raw.error_detail) ? raw.error_detail : undefined;
    const code =
      asNonEmptyString(raw.error_code) ??
      asNonEmptyString(dataObj?.error_code) ??
      asNonEmptyString(detailObj?.code) ??
      'rpc_error';
    const message =
      asNonEmptyString(raw.error) ??
      asNonEmptyString(detailObj?.message) ??
      '后端操作失败';

    return {
      available: true,
      ok: false,
      providerPluginId,
      data: dataObj,
      error: {
        code,
        message,
        providerPluginId: providerPluginId || undefined,
        data: dataObj,
      },
    };
  }

  return {
    available: true,
    ok: true,
    data: raw as T,
    providerPluginId: 'shell-tauri',
    error: null,
  };
}

function cleanDefined<T extends Record<string, unknown>>(obj: T): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v !== undefined) {
      out[k] = v;
    }
  }
  return out;
}

async function invokeTauri<T>(
  command: string,
  invokePayload?: Record<string, unknown>,
): Promise<RpcResult<T>> {
  const tauri = getTauriGlobal();
  if (typeof tauri?.core?.invoke !== 'function') {
    return unavailableResult<T>();
  }

  try {
    const raw =
      invokePayload === undefined
        ? await tauri.core.invoke(command)
        : await tauri.core.invoke(command, invokePayload);
    return normalizeEnvelope<T>(raw);
  } catch (err) {
    return {
      available: true,
      ok: false,
      providerPluginId: '',
      error: extractThrownError(err),
    };
  }
}

export async function runtimeStatus(): Promise<RpcResult<RuntimeStatus>> {
  return invokeTauri<RuntimeStatus>('runtime_status');
}

export interface HostPathsSnapshot {
  homeDir: string;
  configDir: string;
  dataDir: string;
  stateDir: string;
  runtimeDir?: string;
  pluginDirs: string[];
  controlSocket: string;
  mpvSocket: string;
}

export async function hostPaths(): Promise<RpcResult<HostPathsSnapshot>> {
  return invokeTauri<HostPathsSnapshot>('host_paths');
}

export async function coreStatus(): Promise<RpcResult<CoreStatusData>> {
  return invokeTauri<CoreStatusData>('core_status');
}

export async function eventBridgeStatus(): Promise<RpcResult<EventBridgeSnapshot>> {
  return invokeTauri<EventBridgeSnapshot>('event_bridge_status');
}

export async function mpvProbe(): Promise<RpcResult<MpvProbeStatus>> {
  return invokeTauri<MpvProbeStatus>('mpv_probe');
}

export async function mpvStatus(): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_status');
}

export async function mpvOpen(
  request: MpvOpenRequest,
): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_open', {
    args: cleanDefined({
      url: request.url,
      audioUrl: request.audioUrl,
      subtitleUrls: request.subtitleUrls,
      httpHeaders: request.httpHeaders,
      referrer: request.referrer,
      userAgent: request.userAgent,
      startSeconds: request.startSeconds,
      endSeconds: request.endSeconds,
      hwdec: request.hwdec,
      paused: request.paused,
      title: request.title,
      artist: request.artist,
      album: request.album,
      coverUrl: request.coverUrl,
      itemId: request.itemId,
    }),
  });
}

export async function mpvPause(): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_pause');
}

export async function mpvResume(): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_resume');
}

export async function mpvTogglePause(): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_toggle_pause');
}

export async function mpvStop(): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_stop');
}

export async function mpvSeek(
  seconds: number,
  relative = false,
): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_seek', {
    args: { seconds, relative },
  });
}

export async function mpvVolume(volume: number): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_volume', {
    args: { volume },
  });
}

export async function mpvSetSpeed(speed: number): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_set_speed', {
    args: { speed },
  });
}

export async function mpvEmbedProbe(): Promise<RpcResult<MpvEmbedProbe>> {
  return invokeTauri<MpvEmbedProbe>('mpv_embed_probe');
}

export async function mpvEmbedEnable(): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_embed_enable');
}

export async function mpvEmbedDisable(): Promise<RpcResult<MpvPlaybackState>> {
  return invokeTauri<MpvPlaybackState>('mpv_embed_disable');
}

export async function mpvEmbedSetViewport(
  width: number,
  height: number,
  fps = 24,
): Promise<RpcResult<{ width: number; height: number; fps: number }>> {
  return invokeTauri('mpv_embed_set_viewport', {
    args: { width, height, fps },
  });
}

export async function mpvEmbedSetActive(
  active: boolean,
): Promise<RpcResult<{ active: boolean }>> {
  return invokeTauri('mpv_embed_set_active', {
    args: { active },
  });
}

export type { MpvFrameEnvelope };

export async function listPlugins(
  args: ListPluginsArgs = {},
): Promise<RpcResult<ListPluginsData>> {
  return invokeTauri<ListPluginsData>('list_plugins', {
    args: cleanDefined({
      liveOnly: args.liveOnly,
      live_only: args.live_only,
    }),
  });
}

export async function callCapability<T = Record<string, unknown>>(
  args: CallCapabilityArgs,
): Promise<RpcResult<T>> {
  const providerPluginId = args.providerPluginId ?? args.provider_plugin_id;
  if (providerPluginId) {
    const timeoutMs = args.timeoutMs ?? args.timeout_ms;
    return call<T>({
      op: 'capability.call',
      params: cleanDefined({
        capability: args.capability,
        payload: args.payload ?? {},
        provider_plugin_id: providerPluginId,
        timeout: timeoutMs !== undefined ? timeoutMs / 1000 : undefined,
      }),
      timeoutMs: args.timeoutMs,
      timeout_ms: args.timeout_ms,
    });
  }
  return invokeTauri<T>('call_capability', {
    args: cleanDefined({
      capability: args.capability,
      payload: args.payload ?? {},
      timeoutMs: args.timeoutMs,
      timeout_ms: args.timeout_ms,
    }),
  });
}

export async function emitEvent(
  args: EmitEventArgs,
): Promise<RpcResult<EmitEventData>> {
  return invokeTauri<EmitEventData>('emit_event', {
    args: cleanDefined({
      topic: args.topic,
      payload: args.payload ?? {},
      timeoutMs: args.timeoutMs,
      timeout_ms: args.timeout_ms,
    }),
  });
}

export async function readSource<T = Record<string, unknown>>(
  args: ReadSourceArgs,
): Promise<RpcResult<T>> {
  return invokeTauri<T>('read_source', {
    args: cleanDefined({
      domain: args.domain,
      sourceId: args.sourceId,
      source_id: args.source_id,
      payload: args.payload ?? {},
      timeoutMs: args.timeoutMs,
      timeout_ms: args.timeout_ms,
    }),
  });
}

export async function call<T = Record<string, unknown>>(
  args: CallRawArgs,
): Promise<RpcResult<T>> {
  return invokeTauri<T>('call', {
    args: cleanDefined({
      op: args.op,
      params: args.params ?? {},
      timeoutMs: args.timeoutMs,
      timeout_ms: args.timeout_ms,
    }),
  });
}

export interface DomainSourceDescriptor {
  domain: SourceDomain;
  sourceId: string;
  pluginId: string;
  title: string;
  category: string;
  priority: number;
  categories: string[];
  metadata: Record<string, unknown>;
}

export interface DomainInvokeArgs {
  domain: SourceDomain;
  method: string;
  sourceId?: string;
  payload?: Record<string, unknown>;
  timeoutMs?: number;
}

function normalizeDomainSources(raw: unknown, domain: SourceDomain): DomainSourceDescriptor[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
    .map((entry) => {
      const metadata =
        entry.metadata && typeof entry.metadata === 'object' && !Array.isArray(entry.metadata)
          ? (entry.metadata as Record<string, unknown>)
          : {};
      const categories = Array.isArray(metadata.categories)
        ? metadata.categories.map(String).filter(Boolean)
        : [];
      return {
        domain,
        sourceId: String(entry.source_id ?? entry.sourceId ?? ''),
        pluginId: String(entry.plugin_id ?? entry.pluginId ?? ''),
        title: String(entry.title ?? ''),
        category: String(entry.category ?? ''),
        priority: Number(entry.priority ?? 0),
        categories,
        metadata,
      };
    })
    .filter((entry) => entry.sourceId.length > 0);
}

/** List every live workbench source registered for a domain, highest priority first. */
export async function domainSources(
  domain: SourceDomain,
): Promise<RpcResult<DomainSourceDescriptor[]>> {
  const result = await call<Record<string, unknown>>({
    op: 'domain.snapshot',
    params: { domain },
  });
  if (result.available && result.ok && result.data) {
    const data = result.data as Record<string, unknown>;
    const key = `${domain}_sources`;
    return {
      ...result,
      data: normalizeDomainSources(data[key] ?? data.sources, domain),
    } as RpcResult<DomainSourceDescriptor[]>;
  }
  if (result.available && !result.ok) {
    return result as RpcResult<DomainSourceDescriptor[]>;
  }
  return { available: false, ok: false, error: { code: 'service_offline', message: 'Tauri IPC unavailable' } };
}

function normalizeWorkspaceTabs(raw: unknown): WorkspaceTabSnapshot[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
    .map((entry) => {
      const domain = String(entry.domain ?? 'core').toLowerCase();
      const safeDomain = (
        ['media', 'text', 'book', 'core'] as const
      ).includes(domain as never)
        ? (domain as WorkspaceTabSnapshot['domain'])
        : 'core';
      return {
        tab_id: String(entry.tab_id ?? entry.tabId ?? ''),
        plugin_id: String(entry.plugin_id ?? entry.pluginId ?? ''),
        title: String(entry.title ?? entry.tab_id ?? '未命名页签'),
        domain: safeDomain,
        badge: typeof entry.badge === 'string' ? entry.badge : '',
        ui_schema: (
          Array.isArray(entry.ui_schema)
            ? entry.ui_schema
            : entry.ui_schema && typeof entry.ui_schema === 'object'
              ? (entry.ui_schema as Record<string, unknown>)
              : {}
        ) as WorkspaceTabSnapshot['ui_schema'],
      };
    })
    .filter((entry) => entry.tab_id.length > 0);
}

function normalizeDockWidgets(raw: unknown): DockWidgetSnapshot[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
    .map((entry) => {
      const domains = Array.isArray(entry.supported_domains)
        ? entry.supported_domains
            .map((domain) => String(domain).toLowerCase())
            .filter((domain): domain is ContentDomain =>
              (['media', 'text', 'book', 'core'] as const).includes(domain as never),
            )
        : undefined;
      return {
        widget_id: String(entry.widget_id ?? entry.widgetId ?? ''),
        plugin_id: String(entry.plugin_id ?? entry.pluginId ?? ''),
        title: String(entry.title ?? entry.widget_id ?? '未命名组件'),
        supported_domains: domains,
        ui_schema: (
          Array.isArray(entry.ui_schema)
            ? entry.ui_schema
            : entry.ui_schema && typeof entry.ui_schema === 'object'
              ? (entry.ui_schema as Record<string, unknown>)
              : {}
        ) as DockWidgetSnapshot['ui_schema'],
      };
    })
    .filter((entry) => entry.widget_id.length > 0);
}

/** Read every registered declarative workspace tab and F8 dock widget. */
export async function workspaceSnapshot(): Promise<RpcResult<WorkspaceSnapshotData>> {
  const result = await call<Record<string, unknown>>({
    op: 'domain.snapshot',
    params: {},
  });
  if (result.available && result.ok && result.data) {
    const data = result.data as Record<string, unknown>;
    return {
      ...result,
      data: {
        workspace_tabs: normalizeWorkspaceTabs(data.workspace_tabs),
        dock_widgets: normalizeDockWidgets(data.dock_widgets),
        media_sources: Array.isArray(data.media_sources)
          ? (data.media_sources as Array<Record<string, unknown>>)
          : [],
        text_sources: Array.isArray(data.text_sources)
          ? (data.text_sources as Array<Record<string, unknown>>)
          : [],
        book_sources: Array.isArray(data.book_sources)
          ? (data.book_sources as Array<Record<string, unknown>>)
          : [],
      },
    };
  }
  return result as RpcResult<WorkspaceSnapshotData>;
}

/** Dispatch an on_action callback owned by a plugin tab or F8 dock widget. */
export async function invokeWorkspaceAction(
  args: WorkspaceUiActionArgs,
): Promise<RpcResult<Record<string, unknown>>> {
  return call<Record<string, unknown>>({
    op: 'ui.action',
    params: cleanDefined({
      kind: args.kind,
      spec_id: args.specId,
      action_id: args.actionId,
      plugin_id: args.pluginId,
      state: args.state ?? {},
      timeout: args.timeoutMs !== undefined ? args.timeoutMs / 1000 : undefined,
    }),
    timeoutMs: args.timeoutMs,
  });
}

/** Invoke a whitelisted method on a live workbench source (text/book/media). */
export async function invokeDomain<T = unknown>(
  args: DomainInvokeArgs,
): Promise<RpcResult<T>> {
  const result = await call<{ result?: T }>({
    op: 'domain.invoke',
    params: cleanDefined({
      domain: args.domain,
      method: args.method,
      source_id: args.sourceId,
      payload: args.payload ?? {},
      timeout: args.timeoutMs ? args.timeoutMs / 1000 : undefined,
    }),
    timeoutMs: args.timeoutMs,
  });
  if (result.available && result.ok && result.data) {
    return { ...result, data: (result.data as { result?: T }).result as T } as RpcResult<T>;
  }
  return result as RpcResult<T>;
}

export interface JobRecord {
  id: string;
  kind: string;
  state: string;
  title: string;
  pluginId?: string;
  progress: number;
  stage: string;
  message?: string;
  completedSteps?: number | null;
  totalSteps?: number | null;
  result?: unknown;
  error?: string;
  errorCode?: string;
  metadata: Record<string, unknown>;
  createdAt?: number;
  updatedAt?: number;
  startedAt?: number | null;
  finishedAt?: number | null;
}

function normalizeJob(raw: Record<string, unknown>): JobRecord {
  const numberValue = (value: unknown): number | undefined =>
    typeof value === 'number' && Number.isFinite(value) ? value : undefined;
  const nullableNumber = (value: unknown): number | null =>
    typeof value === 'number' && Number.isFinite(value) ? value : null;
  const metadata =
    raw.metadata && typeof raw.metadata === 'object' && !Array.isArray(raw.metadata)
      ? (raw.metadata as Record<string, unknown>)
      : {};
  return {
    id: String(raw.id ?? raw.job_id ?? ''),
    kind: String(raw.kind ?? raw.type ?? ''),
    state: String(raw.state ?? raw.status ?? 'pending'),
    title: String(raw.title ?? raw.kind ?? '后台任务'),
    pluginId: raw.plugin_id ? String(raw.plugin_id) : undefined,
    progress:
      typeof raw.progress === 'number' && Number.isFinite(raw.progress)
        ? Math.max(0, Math.min(1, raw.progress))
        : 0,
    stage: String(raw.stage ?? raw.status ?? 'pending'),
    message: raw.message ? String(raw.message) : '',
    completedSteps:
      typeof raw.completed_steps === 'number'
        ? raw.completed_steps
        : typeof raw.completedSteps === 'number'
          ? raw.completedSteps
          : null,
    totalSteps:
      typeof raw.total_steps === 'number'
        ? raw.total_steps
        : typeof raw.totalSteps === 'number'
          ? raw.totalSteps
          : null,
    result: raw.result,
    error: raw.error ? String(raw.error) : '',
    errorCode: raw.error_code ? String(raw.error_code) : '',
    metadata,
    createdAt: numberValue(raw.created_at ?? raw.createdAt),
    updatedAt: numberValue(raw.updated_at ?? raw.updatedAt),
    startedAt: nullableNumber(raw.started_at ?? raw.startedAt),
    finishedAt: nullableNumber(raw.finished_at ?? raw.finishedAt),
  };
}

export async function submitJob(
  args: {
    kind: string;
    payload?: Record<string, unknown>;
    pluginId?: string;
    capability?: string;
    title?: string;
    totalSteps?: number;
    metadata?: Record<string, unknown>;
  },
): Promise<RpcResult<{ job: JobRecord }>> {
  const result = await call<{ job?: Record<string, unknown> }>({
    op: 'job.submit',
    params: cleanDefined({
      kind: args.kind,
      plugin_id: args.pluginId,
      capability: args.capability,
      title: args.title,
      total_steps: args.totalSteps,
      metadata: args.metadata,
      payload: args.payload ?? {},
    }),
  });
  if (result.available && result.ok && result.data?.job) {
    return { ...result, data: { job: normalizeJob(result.data.job) } } as RpcResult<{ job: JobRecord }>;
  }
  return result as RpcResult<{ job: JobRecord }>;
}

export async function listJobs(filters: {
  pluginId?: string;
  status?: string;
  kind?: string;
} = {}): Promise<RpcResult<{ jobs: JobRecord[] }>> {
  const result = await call<{ jobs?: Record<string, unknown>[] }>({
    op: 'job.list',
    params: cleanDefined({
      plugin_id: filters.pluginId,
      status: filters.status,
      kind: filters.kind,
    }),
  });
  if (result.available && result.ok) {
    const jobs = (result.data?.jobs ?? [])
      .map(normalizeJob)
      .sort((a, b) => (b.updatedAt ?? b.createdAt ?? 0) - (a.updatedAt ?? a.createdAt ?? 0));
    return { ...result, data: { jobs } } as RpcResult<{ jobs: JobRecord[] }>;
  }
  return result as RpcResult<{ jobs: JobRecord[] }>;
}

export async function getJob(id: string): Promise<RpcResult<{ job: JobRecord }>> {
  const result = await call<{ job?: Record<string, unknown> }>({ op: 'job.get', params: { id } });
  if (result.available && result.ok && result.data?.job) {
    return { ...result, data: { job: normalizeJob(result.data.job) } } as RpcResult<{ job: JobRecord }>;
  }
  return result as RpcResult<{ job: JobRecord }>;
}

export async function cancelJob(id: string): Promise<RpcResult<{ job: JobRecord }>> {
  const result = await call<{ job?: Record<string, unknown> }>({ op: 'job.cancel', params: { id } });
  if (result.available && result.ok && result.data?.job) {
    return { ...result, data: { job: normalizeJob(result.data.job) } } as RpcResult<{ job: JobRecord }>;
  }
  return result as RpcResult<{ job: JobRecord }>;
}

export interface SearchHit {
  id: string;
  domain: string;
  title: string;
  snippet: string;
  score: number;
  sourceId?: string;
  pluginId?: string;
  kind?: string;
  payload: Record<string, unknown>;
}

export async function searchIndex(args: {
  query: string;
  domain?: string;
  sourceId?: string;
  pluginId?: string;
  kind?: string;
  limit?: number;
  offset?: number;
  matchAny?: boolean;
}): Promise<RpcResult<{ hits: SearchHit[]; total: number }>> {
  return call<{ hits: SearchHit[]; total: number }>({
    op: 'search.query',
    params: cleanDefined({
      query: args.query,
      domain: args.domain,
      source_id: args.sourceId,
      plugin_id: args.pluginId,
      kind: args.kind,
      limit: args.limit,
      offset: args.offset,
      match_any: args.matchAny,
    }),
  });
}

export interface ArchiveDateStats {
  date: string;
  music: number;
  youtube: number;
  news: number;
  reddit: number;
  journal: number;
  english: number;
  anomalies?: Array<Record<string, unknown>>;
}

export interface CurationBundle {
  business_date: string;
  media_items: Array<Record<string, unknown>>;
  text_items: Array<Record<string, unknown>>;
  book_items: Array<Record<string, unknown>>;
}

export type CurationDomainFilter =
  | 'all'
  | 'music'
  | 'youtube'
  | 'news'
  | 'reddit'
  | 'journal'
  | 'english';

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter(
        (entry): entry is Record<string, unknown> =>
          typeof entry === 'object' && entry !== null && !Array.isArray(entry),
      )
    : [];
}

export async function listArchiveDates(args: {
  domain?: CurationDomainFilter;
  limit?: number;
} = {}): Promise<
  RpcResult<{
    totalDates: number;
    dates: ArchiveDateStats[];
    historyAnomalies: Array<Record<string, unknown>>;
  }>
> {
  const result = await callCapability<Record<string, unknown>>({
    capability: 'recommend.feed.v1',
    payload: {
      action: 'list_archive_dates',
      domain: args.domain && args.domain !== 'all' ? args.domain : undefined,
      limit: args.limit,
    },
  });
  if (result.available && result.ok) {
    const data = result.data ?? {};
    const dates = asRecordArray(data.dates).map((row) => ({
      date: String(row.date ?? ''),
      music: Number(row.music ?? 0),
      youtube: Number(row.youtube ?? 0),
      news: Number(row.news ?? 0),
      reddit: Number(row.reddit ?? 0),
      journal: Number(row.journal ?? 0),
      english: Number(row.english ?? 0),
      anomalies: asRecordArray(row.anomalies),
    }));
    return {
      ...result,
      data: {
        totalDates: Number(data.total_dates ?? dates.length),
        dates,
        historyAnomalies: asRecordArray(data.history_anomalies),
      },
    } as RpcResult<{
      totalDates: number;
      dates: ArchiveDateStats[];
      historyAnomalies: Array<Record<string, unknown>>;
    }>;
  }
  return result as RpcResult<{
    totalDates: number;
    dates: ArchiveDateStats[];
    historyAnomalies: Array<Record<string, unknown>>;
  }>;
}

export async function getDateBundle(date: string): Promise<RpcResult<CurationBundle>> {
  const result = await callCapability<Record<string, unknown>>({
    capability: 'recommend.feed.v1',
    payload: { action: 'get_date_bundle', business_date: date },
  });
  if (result.available && result.ok) {
    const data = result.data ?? {};
    return {
      ...result,
      data: {
        business_date: String(data.business_date ?? date),
        media_items: asRecordArray(data.media_items),
        text_items: asRecordArray(data.text_items),
        book_items: asRecordArray(data.book_items),
      },
    } as RpcResult<CurationBundle>;
  }
  return result as RpcResult<CurationBundle>;
}

export async function searchArchive(args: {
  query: string;
  domain?: CurationDomainFilter;
  date?: string;
  limit?: number;
}): Promise<
  RpcResult<{
    media: Array<Record<string, unknown>>;
    texts: Array<Record<string, unknown>>;
    books: Array<Record<string, unknown>>;
  }>
> {
  const result = await callCapability<Record<string, unknown>>({
    capability: 'recommend.feed.v1',
    payload: {
      action: 'search_archive',
      query: args.query,
      domain: args.domain && args.domain !== 'all' ? args.domain : undefined,
      date: args.date,
      limit: args.limit ?? 50,
    },
  });
  if (result.available && result.ok) {
    const data = result.data ?? {};
    const combined = asRecordArray(data.items ?? data.results);
    const nestedItem = (entry: Record<string, unknown>): Record<string, unknown> => {
      const nested = entry.item;
      return nested && typeof nested === 'object' && !Array.isArray(nested)
        ? (nested as Record<string, unknown>)
        : entry;
    };
    const combinedMedia = combined
      .filter((entry) => ['media', 'music', 'video', 'youtube'].includes(String(entry.domain ?? '')))
      .map(nestedItem);
    const combinedTexts = combined
      .filter((entry) =>
        ['text', 'news', 'reddit', 'journal', 'english'].includes(String(entry.domain ?? '')),
      )
      .map(nestedItem);
    const combinedBooks = combined
      .filter((entry) => String(entry.domain ?? '') === 'book')
      .map(nestedItem);
    const media = asRecordArray(data.media_items ?? data.media);
    const texts = asRecordArray(data.text_items ?? data.texts);
    const books = asRecordArray(data.book_items ?? data.books);
    return {
      ...result,
      data: {
        media: media.length > 0 ? media : combinedMedia,
        texts: texts.length > 0 ? texts : combinedTexts,
        books: books.length > 0 ? books : combinedBooks,
      },
    } as RpcResult<{
      media: Array<Record<string, unknown>>;
      texts: Array<Record<string, unknown>>;
      books: Array<Record<string, unknown>>;
    }>;
  }
  return result as RpcResult<{
    media: Array<Record<string, unknown>>;
    texts: Array<Record<string, unknown>>;
    books: Array<Record<string, unknown>>;
  }>;
}

export async function syncCurationArchive(args: {
  pullRemote?: boolean;
  forceFull?: boolean;
} = {}): Promise<RpcResult<Record<string, unknown>>> {
  return callCapability<Record<string, unknown>>({
    capability: 'recommend.feed.v1',
    payload: {
      action: 'bootstrap_and_rehydrate',
      pull_remote: args.pullRemote ?? true,
      clone_if_missing: true,
      force_full: args.forceFull ?? false,
    },
  });
}

export interface BookSearchResult {
  itemId: string;
  title: string;
  author: string;
  format: string;
  size: string;
  language: string;
  year: string;
  detailUrl: string;
  downloadToken: string;
  source: string;
  providerPluginId: string;
  raw: Record<string, unknown>;
}

export type BookSearchProvider = 'auto' | 'plugin-zlib' | 'plugin-annas-archive';

function normalizeBookSearchResult(
  raw: Record<string, unknown>,
  providerPluginId: string,
): BookSearchResult {
  return {
    itemId: String(raw.item_id ?? raw.id ?? raw.md5 ?? ''),
    title: String(raw.title ?? raw.name ?? raw.item_id ?? '未命名图书'),
    author: String(raw.author ?? raw.authors ?? '未知作者'),
    format: String(raw.format ?? raw.extension ?? 'epub').toLowerCase(),
    size: String(raw.size ?? raw.size_label ?? raw.filesize ?? ''),
    language: String(raw.language ?? raw.lang ?? ''),
    year: String(raw.year ?? ''),
    detailUrl: String(raw.detail_url ?? raw.url ?? ''),
    downloadToken: String(raw.download_token ?? raw.download ?? ''),
    source: String(raw.source ?? providerPluginId),
    providerPluginId,
    raw,
  };
}

export async function searchBooks(args: {
  query: string;
  provider?: BookSearchProvider;
  format?: string;
  limit?: number;
  page?: number;
}): Promise<RpcResult<{ results: BookSearchResult[]; providerPluginId: string }>> {
  const providers: string[] =
    args.provider && args.provider !== 'auto'
      ? [args.provider]
      : ['plugin-zlib', 'plugin-annas-archive'];
  let lastFailure: RpcResult<{ results: BookSearchResult[]; providerPluginId: string }> | null =
    null;
  let emptySuccess: RpcResult<{
    results: BookSearchResult[];
    providerPluginId: string;
  }> | null = null;
  for (const providerPluginId of providers) {
    const result = await callCapability<Record<string, unknown>>({
      capability: 'book.search.v1',
      providerPluginId,
      payload: {
        action: 'search',
        query: args.query,
        format: args.format || undefined,
        limit: args.limit ?? 20,
        page: args.page ?? 1,
      },
    });
    if (result.available && result.ok) {
      const results = asRecordArray(result.data?.results).map((entry) =>
        normalizeBookSearchResult(entry, providerPluginId),
      );
      const normalized = {
        ...result,
        data: { results, providerPluginId },
        providerPluginId,
      } as RpcResult<{ results: BookSearchResult[]; providerPluginId: string }>;
      if (results.length > 0) return normalized;
      emptySuccess = normalized;
      continue;
    }
    if (result.available && !result.ok) {
      lastFailure = result as RpcResult<{
        results: BookSearchResult[];
        providerPluginId: string;
      }>;
    }
  }
  return (
    emptySuccess ??
    lastFailure ?? {
      available: false,
      ok: false,
      error: { code: 'book_search_unavailable', message: '未找到可用的 book.search.v1 提供方' },
    }
  );
}

export async function bookSearchAction(
  action: 'detail' | 'download' | 'open_in_babeldoc' | 'open_in_ebook' | 'send_to_nas' | 'status',
  args: {
    item: Pick<
      BookSearchResult,
      'itemId' | 'title' | 'format' | 'detailUrl' | 'downloadToken' | 'providerPluginId'
    >;
    extra?: Record<string, unknown>;
  },
): Promise<RpcResult<Record<string, unknown>>> {
  return callCapability<Record<string, unknown>>({
    capability: 'book.search.v1',
    providerPluginId: args.item.providerPluginId,
    payload: {
      action,
      item_id: args.item.itemId,
      id: args.item.itemId,
      title: args.item.title,
      format: args.item.format,
      detail_url: args.item.detailUrl || undefined,
      download_token: args.item.downloadToken || undefined,
      ...(args.extra ?? {}),
    },
  });
}

export async function listenTauriEvent<T>(
  eventName: string,
  handler: (payload: T) => void,
): Promise<TauriUnlistenFn | null> {
  const tauri = getTauriGlobal();
  if (typeof tauri?.event?.listen !== 'function') {
    return null;
  }
  return tauri.event.listen<T>(eventName, (event) => {
    handler(event.payload);
  });
}
