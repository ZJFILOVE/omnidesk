import { useCallback, useEffect, useRef, useState } from 'react';
import {
  MPV_STATE_CHANNEL,
  callCapability,
  formatRpcError,
  isTauriAvailable,
  listenTauriEvent,
  mpvEmbedEnable,
  mpvEmbedProbe,
  mpvOpen,
  mpvPause,
  mpvResume,
  mpvSeek,
  mpvSetSpeed,
  mpvStatus,
  mpvVolume,
  type MpvPlaybackState,
  type RpcResult,
  type TauriUnlistenFn,
} from '../api';
import type { MediaItem } from '../data/mock';

export interface MpvActionResult {
  ok: boolean;
  message: string;
  state?: MpvPlaybackState;
}

export interface MpvPlaybackController {
  enabled: boolean;
  state: MpvPlaybackState | null;
  activeItem: MediaItem | null;
  queue: MediaItem[];
  lastMusic: MediaItem | null;
  playing: boolean;
  positionSec: number;
  durationSec: number | null;
  volume: number;
  speed: number;
  busy: boolean;
  embedded: boolean;
  embedDetail: string;
  open: (
    item: MediaItem,
    options?: { paused?: boolean; startSeconds?: number; quality?: string },
  ) => Promise<MpvActionResult>;
  toggle: (item: MediaItem) => Promise<MpvActionResult>;
  seek: (item: MediaItem, seconds: number) => Promise<MpvActionResult>;
  setVolume: (volume: number) => Promise<MpvActionResult>;
  setSpeed: (speed: number) => Promise<MpvActionResult>;
  setQueue: (items: MediaItem[]) => void;
  next: () => Promise<MpvActionResult>;
  previous: () => Promise<MpvActionResult>;
  resumeLastMusic: () => Promise<MpvActionResult>;
}

interface ResolvedMediaStream {
  url: string;
  audioUrl?: string;
  httpHeaders?: Record<string, string>;
  httpProxy?: string;
  quality?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function isMpvPlaybackState(value: unknown): value is MpvPlaybackState {
  if (!isRecord(value)) return false;
  return (
    typeof value.available === 'boolean' &&
    typeof value.initialized === 'boolean' &&
    typeof value.playing === 'boolean' &&
    typeof value.positionSec === 'number' &&
    typeof value.durationSec === 'number'
  );
}

function unwrapMpvState(value: unknown): MpvPlaybackState | null {
  if (isMpvPlaybackState(value)) return value;
  if (isRecord(value) && isMpvPlaybackState(value.data)) return value.data;
  return null;
}

function stateSummary(state: MpvPlaybackState): string {
  if (state.lastError) return `mpv 错误：${state.lastError}`;
  if (!state.available) return 'mpv 不可用';
  if (state.stopped) return 'mpv 已停止';
  return state.paused ? 'mpv 已暂停' : 'mpv 播放中';
}

function formatSpeed(speed: number): string {
  return `${Number(speed.toFixed(2))}×`;
}

function stringHeaders(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  const out: Record<string, string> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === 'string' && key.trim() && entry.trim()) {
      out[key] = entry;
    }
  }
  return out;
}

export async function resolveMediaStream(
  item: MediaItem,
  quality?: string,
): Promise<{ ok: true; stream: ResolvedMediaStream } | { ok: false; message: string }> {
  const resolver = item.streamResolver;
  if (!resolver) {
    return {
      ok: true,
      stream: {
        url: item.playUrl,
        audioUrl: item.audioUrl,
        httpHeaders: item.httpHeaders,
        quality: item.qualityLabel,
      },
    };
  }
  if (!resolver.capability) {
    return { ok: false, message: '媒体条目缺少取流能力声明' };
  }
  const result = await callCapability<Record<string, unknown>>({
    capability: resolver.capability,
    providerPluginId: resolver.providerPluginId ?? item.sourceId,
    payload: {
      ...resolver.payload,
      ...(quality ? { quality } : {}),
      title: item.title,
    },
    timeoutMs: 120_000,
  });
  if (!result.available) {
    return { ok: false, message: '取流服务不可用：Core 控制通道未连接' };
  }
  if (!result.ok) {
    return { ok: false, message: `取流失败 ${formatRpcError(result.error)}` };
  }
  const data = result.data;
  const url = [data.play_url, data.video_url, data.url]
    .find((entry): entry is string => typeof entry === 'string' && entry.trim().length > 0);
  if (!url) {
    return { ok: false, message: '取流成功但插件没有返回可播放地址' };
  }
  const audioUrl =
    typeof data.audio_url === 'string' && data.audio_url !== url
      ? data.audio_url
      : undefined;
  const httpHeaders = {
    ...stringHeaders(item.httpHeaders),
    ...stringHeaders(data.headers ?? data.http_headers),
  };
  if (data.android_media === true) {
    for (const key of Object.keys(httpHeaders)) {
      if (key.toLowerCase() === 'referer' || key.toLowerCase() === 'referrer') {
        delete httpHeaders[key];
      }
    }
  }
  return {
    ok: true,
    stream: {
      url,
      audioUrl,
      httpHeaders,
      httpProxy: typeof data.http_proxy === 'string' ? data.http_proxy : undefined,
      quality: typeof data.quality === 'string' ? data.quality : item.qualityLabel,
    },
  };
}

export function useMpvPlayback(options: { enabled?: boolean } = {}): MpvPlaybackController {
  const enabled = options.enabled ?? isTauriAvailable();
  const [state, setState] = useState<MpvPlaybackState | null>(null);
  const [activeItem, setActiveItem] = useState<MediaItem | null>(null);
  const [queue, setQueueState] = useState<MediaItem[]>([]);
  const [lastMusic, setLastMusic] = useState<MediaItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [embedded, setEmbedded] = useState(false);
  const [embedDetail, setEmbedDetail] = useState('');
  const pendingRef = useRef(0);
  const mountedRef = useRef(true);
  const revisionRef = useRef(0);
  const embeddedRef = useRef(false);
  const embedAttemptRef = useRef<Promise<boolean> | null>(null);
  const embedUnavailableRef = useRef(false);
  const queueRef = useRef<MediaItem[]>([]);
  const lastMusicRef = useRef<MediaItem | null>(null);
  const qualityRef = useRef<string | undefined>(undefined);

  const applyState = useCallback((next: MpvPlaybackState) => {
    revisionRef.current += 1;
    setState(next);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setState(null);
      setEmbedded(false);
      setEmbedDetail('');
      embeddedRef.current = false;
      return undefined;
    }

    let disposed = false;
    let unlisten: TauriUnlistenFn | null = null;

    void (async () => {
      const off = await listenTauriEvent<unknown>(MPV_STATE_CHANNEL, (payload) => {
        if (disposed) return;
        const next = unwrapMpvState(payload);
        if (next) applyState(next);
      });
      if (disposed) {
        if (off) void off();
        return;
      }
      unlisten = off;

      const revisionAtStart = revisionRef.current;
      const current = await mpvStatus();
      if (disposed || !current.ok) return;
      if (revisionRef.current !== revisionAtStart) return;
      const next = unwrapMpvState(current.data);
      if (next) applyState(next);
    })();

    return () => {
      disposed = true;
      if (unlisten) void unlisten();
    };
  }, [applyState, enabled]);

  useEffect(() => {
    const itemId = state?.itemId;
    if (!itemId) return;
    const matched = queueRef.current.find((item) => item.id === itemId);
    if (matched) setActiveItem(matched);
  }, [state?.itemId]);

  const ensureEmbedded = useCallback(async (): Promise<boolean> => {
    if (!enabled || embedUnavailableRef.current) return false;
    if (embeddedRef.current) return true;
    if (embedAttemptRef.current) return embedAttemptRef.current;

    embedAttemptRef.current = (async () => {
      const probe = await mpvEmbedProbe();
      if (!probe.ok || !probe.data.available) {
        embedUnavailableRef.current = true;
        if (mountedRef.current) {
          setEmbedDetail(
            probe.ok
              ? probe.data.detail || 'libmpv 软件渲染不可用'
              : `libmpv 探测失败 ${formatRpcError(probe.error)}`,
          );
        }
        return false;
      }

      const enable = await mpvEmbedEnable();
      if (!enable.ok) {
        embedUnavailableRef.current = true;
        if (mountedRef.current) {
          setEmbedDetail(`内嵌渲染启用失败 ${formatRpcError(enable.error)}`);
        }
        return false;
      }
      embeddedRef.current = true;
      if (mountedRef.current) {
        setEmbedded(true);
        setEmbedDetail(probe.data.detail);
      }
      const next = unwrapMpvState(enable.data);
      if (next) applyState(next);
      return true;
    })().finally(() => {
      embedAttemptRef.current = null;
    });

    return embedAttemptRef.current;
  }, [applyState, enabled]);

  useEffect(() => {
    if (enabled) {
      void ensureEmbedded();
    }
  }, [enabled, ensureEmbedded]);

  const run = useCallback(
    async (
      operation: () => Promise<RpcResult<MpvPlaybackState>>,
      successMessage: (next: MpvPlaybackState) => string,
    ): Promise<MpvActionResult> => {
      pendingRef.current += 1;
      if (mountedRef.current) setBusy(true);
      try {
        const result = await operation();
        if (!mountedRef.current) {
          return { ok: false, message: '组件已卸载' };
        }
        if (!result.ok) {
          return {
            ok: false,
            message: `mpv 操作失败 ${formatRpcError(result.error)}`,
          };
        }
        const next = unwrapMpvState(result.data);
        if (next) {
          applyState(next);
          return { ok: true, message: successMessage(next), state: next };
        }
        return { ok: true, message: 'mpv 操作已提交' };
      } finally {
        pendingRef.current -= 1;
        if (mountedRef.current && pendingRef.current <= 0) setBusy(false);
      }
    },
    [applyState],
  );

  const open = useCallback(
    async (
      item: MediaItem,
      options?: { paused?: boolean; startSeconds?: number; quality?: string },
    ): Promise<MpvActionResult> => {
      if (!enabled) {
        return { ok: false, message: '离线预览：当前没有连接 Tauri/mpv' };
      }
      await ensureEmbedded();
      const hasQualityOverride =
        options !== undefined && Object.prototype.hasOwnProperty.call(options, 'quality');
      const remembered = qualityRef.current;
      const requestedQuality = hasQualityOverride
        ? options.quality || undefined
        : remembered && item.streamResolver?.qualityOptions.includes(remembered)
          ? remembered
          : undefined;
      if (hasQualityOverride) qualityRef.current = options.quality || undefined;
      const resolved = await resolveMediaStream(item, requestedQuality);
      if (!resolved.ok) {
        return { ok: false, message: resolved.message };
      }
      setActiveItem(item);
      if (item.kind === 'music') {
        lastMusicRef.current = item;
        setLastMusic(item);
      }
      const stream = resolved.stream;
      return run(
        () =>
          mpvOpen({
            url: stream.url,
            audioUrl: stream.audioUrl,
            httpHeaders: stream.httpHeaders,
            httpProxy: stream.httpProxy,
            itemId: item.id,
            title: item.title,
            artist: item.creator,
            album: item.subtitle,
            paused: options?.paused ?? false,
            startSeconds: options?.startSeconds,
            hwdec: true,
          }),
        (next) =>
          `${stateSummary(next)}：${item.title}${
            stream.quality ? ` · ${stream.quality}` : ''
          }${
            embeddedRef.current ? ' · 内嵌画布' : ''
          }${
            next.hwdecCurrent ? ` · hwdec=${next.hwdecCurrent}` : ''
          }`,
      );
    },
    [enabled, ensureEmbedded, run],
  );

  const toggle = useCallback(
    async (item: MediaItem): Promise<MpvActionResult> => {
      if (!enabled) {
        return { ok: false, message: '离线预览：当前没有连接 Tauri/mpv' };
      }
      await ensureEmbedded();
      if (!state?.initialized || state.stopped) {
        return open(item, { paused: false });
      }
      const shouldResume = state.paused;
      return run(
        () => (shouldResume ? mpvResume() : mpvPause()),
        (next) => `${stateSummary(next)}：${item.title}`,
      );
    },
    [enabled, ensureEmbedded, open, run, state?.initialized, state?.paused, state?.stopped],
  );

  const seek = useCallback(
    async (item: MediaItem, seconds: number): Promise<MpvActionResult> => {
      if (!enabled) {
        return { ok: false, message: '离线预览：当前没有连接 Tauri/mpv' };
      }
      await ensureEmbedded();
      if (!state?.initialized || state.stopped) {
        return open(item, { paused: false, startSeconds: seconds });
      }
      return run(
        () => mpvSeek(seconds),
        (next) => `mpv 已跳转至 ${Math.floor(next.positionSec)}s：${item.title}`,
      );
    },
    [enabled, ensureEmbedded, open, run, state?.initialized, state?.stopped],
  );

  const setVolume = useCallback(
    async (volume: number): Promise<MpvActionResult> => {
      if (!enabled) {
        return { ok: false, message: '离线预览：当前没有连接 Tauri/mpv' };
      }
      await ensureEmbedded();
      const clamped = Math.max(0, Math.min(100, Math.round(volume)));
      return run(
        () => mpvVolume(clamped),
        (next) => `mpv 音量 ${next.volume}%`,
      );
    },
    [enabled, ensureEmbedded, run],
  );

  const setSpeed = useCallback(
    async (speed: number): Promise<MpvActionResult> => {
      if (!enabled) {
        return { ok: false, message: '离线预览：当前没有连接 Tauri/mpv' };
      }
      await ensureEmbedded();
      const clamped = Math.max(0.25, Math.min(4, Number(speed.toFixed(2))));
      return run(
        () => mpvSetSpeed(clamped),
        (next) => `mpv 播放速度 ${formatSpeed(next.speed)}`,
      );
    },
    [enabled, ensureEmbedded, run],
  );

  const setQueue = useCallback((items: MediaItem[]) => {
    queueRef.current = items;
    setQueueState(items);
  }, []);

  const next = useCallback(async (): Promise<MpvActionResult> => {
    const items = queueRef.current;
    const currentId = activeItem?.id ?? state?.itemId;
    const index = items.findIndex((item) => item.id === currentId);
    const target = items.length > 0 ? items[(index + 1 + items.length) % items.length] : undefined;
    if (!target) return { ok: false, message: '播放队列为空' };
    return open(target, { paused: false });
  }, [activeItem?.id, open, state?.itemId]);

  const previous = useCallback(async (): Promise<MpvActionResult> => {
    const items = queueRef.current;
    const currentId = activeItem?.id ?? state?.itemId;
    const index = items.findIndex((item) => item.id === currentId);
    const target =
      items.length > 0
        ? items[(index <= 0 ? items.length - 1 : index - 1) % items.length]
        : undefined;
    if (!target) return { ok: false, message: '播放队列为空' };
    return open(target, { paused: false });
  }, [activeItem?.id, open, state?.itemId]);

  const resumeLastMusic = useCallback(async (): Promise<MpvActionResult> => {
    const item = lastMusicRef.current;
    if (!item) return { ok: false, message: '没有可恢复的上次音乐' };
    return open(item, { paused: false });
  }, [open]);

  return {
    enabled,
    state,
    activeItem,
    queue,
    lastMusic,
    playing: Boolean(state?.playing && !state.paused && !state.stopped),
    positionSec: state?.positionSec ?? 0,
    durationSec: state?.durationSec && state.durationSec > 0 ? state.durationSec : null,
    volume: state?.volume ?? 85,
    speed: state?.speed && state.speed > 0 ? state.speed : 1,
    busy,
    embedded,
    embedDetail,
    open,
    toggle,
    seek,
    setVolume,
    setSpeed,
    setQueue,
    next,
    previous,
    resumeLastMusic,
  };
}
