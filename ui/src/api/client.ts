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
  TauriGlobal,
  TauriUnlistenFn,
} from './types';

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
