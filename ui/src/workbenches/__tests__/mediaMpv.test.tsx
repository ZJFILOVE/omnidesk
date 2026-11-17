// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { MpvPlaybackState, TauriEvent } from '../../api';
import { MPV_FRAME_CHANNEL, MPV_STATE_CHANNEL } from '../../api';
import type { MediaItem } from '../../data/mock';
import { MediaWorkbench } from '../MediaWorkbench';
import { resolveMediaStream } from '../useMpvPlayback';

const stoppedState: MpvPlaybackState = {
  available: true,
  initialized: false,
  playing: false,
  paused: true,
  stopped: true,
  buffering: false,
  positionSec: 0,
  durationSec: 0,
  speed: 1,
  volume: 85,
  videoWidth: 0,
  videoHeight: 0,
  fps: 0,
  videoBitrate: 0,
  decoderDropFrames: 0,
  outputDropFrames: 0,
  videoCodec: '',
  audioCodec: '',
  audioSamplerate: 0,
  audioChannels: 0,
  cacheDurationSec: 0,
  hwdecCurrent: '',
  hwdecActive: false,
  lastError: '',
  currentUrl: '',
  itemId: '',
  title: '',
  artist: '',
  album: '',
  coverUrl: '',
  ipcSocketPath: null,
};

const playingState: MpvPlaybackState = {
  ...stoppedState,
  initialized: true,
  playing: true,
  paused: false,
  stopped: false,
  positionSec: 12,
  durationSec: 600,
  hwdecCurrent: 'vaapi',
  hwdecActive: true,
  currentUrl: 'https://www.youtube.com/watch?v=kCc8FmEb1nY',
  itemId: 'media-yt-attention',
  title: 'Attention Is All You Need',
};

// Playback tests supply a real-shaped domain response rather than relying on demo fallback.
function mediaSourceResponse(args?: Record<string, unknown>) {
  if (args?.op === 'domain.snapshot') return {
    ok: true, data: { media_sources: [{ source_id: 'plugin-youtube', title: 'YouTube' }] }, error: '',
  };
  const params = args?.params as Record<string, unknown> | undefined;
  if (args?.op === 'domain.invoke') return {
    ok: true, error: '', data: { result: params?.method === 'fetch_items' ? [{
      id: playingState.itemId, source_id: 'plugin-youtube', title: playingState.title,
      kind: 'video', play_url: playingState.currentUrl, duration_sec: 600,
    }] : [] },
  };
  return { ok: true, data: {}, error: '' };
}

afterEach(() => {
  cleanup();
  delete window.__TAURI__;
  vi.restoreAllMocks();
});

describe('MediaWorkbench real mpv integration', () => {
  it('resolves plugin streams and strips the web Referer for Android media', async () => {
    const invoke = vi.fn(async () => ({
      ok: true,
      provider_plugin_id: 'plugin-bilibili',
      data: {
        play_url: 'https://upos.example/video.m4s',
        audio_url: 'https://upos.example/audio.m4s',
        headers: { 'User-Agent': 'Bilibili Android' },
        quality: '4k',
        android_media: true,
      },
      error: '',
    }));
    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<
          NonNullable<Window['__TAURI__']>['core']
        >['invoke'],
      },
    };
    const item: MediaItem = {
      id: 'BV1android',
      sourceId: 'plugin-bilibili',
      kind: 'video',
      title: 'Android 高画质',
      subtitle: '',
      creator: '',
      durationSec: 120,
      durationLabel: '2:00',
      aspect: '16:9',
      badge: 'Bilibili',
      thumbGradient: '',
      thumbTag: 'Bilibili',
      playUrl: 'https://www.bilibili.com/video/BV1android',
      httpHeaders: { Referer: 'https://www.bilibili.com/' },
      summary: '',
      streamResolver: {
        capability: 'bilibili.media.v1',
        payload: { action: 'resolve_stream', item_id: 'BV1android', emit_open: false },
        qualityOptions: ['auto', '4k', '1080p'],
      },
    };

    const result = await resolveMediaStream(item, '4k');
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.stream.url).toBe('https://upos.example/video.m4s');
    expect(result.stream.audioUrl).toBe('https://upos.example/audio.m4s');
    expect(result.stream.httpHeaders).toEqual({ 'User-Agent': 'Bilibili Android' });
    expect(result.stream.quality).toBe('4k');
    expect(invoke).toHaveBeenCalledWith('call', {
      args: expect.objectContaining({
        op: 'capability.call',
        params: expect.objectContaining({
          capability: 'bilibili.media.v1',
          provider_plugin_id: 'plugin-bilibili',
          payload: expect.objectContaining({ quality: '4k', emit_open: false }),
        }),
      }),
    });
  });

  it('fails closed instead of falling back to the web page when resolution fails', async () => {
    window.__TAURI__ = {
      core: {
        invoke: vi.fn(async () => ({
          ok: false,
          provider_plugin_id: 'plugin-youtube',
          data: {},
          error: 'yt-dlp 解析失败',
          error_code: 'service_offline',
        })) as unknown as NonNullable<
          NonNullable<Window['__TAURI__']>['core']
        >['invoke'],
      },
    };
    const item: MediaItem = {
      id: 'video-id-01',
      sourceId: 'plugin-youtube',
      kind: 'video',
      title: '失败流',
      subtitle: '',
      creator: '',
      durationSec: 0,
      durationLabel: '--:--',
      aspect: '16:9',
      badge: 'YouTube',
      thumbGradient: '',
      thumbTag: 'YouTube',
      playUrl: 'https://www.youtube.com/watch?v=video-id-01',
      summary: '',
      streamResolver: {
        capability: 'media.youtube.v1',
        payload: { action: 'resolve_stream', video_id: 'video-id-01' },
        qualityOptions: ['best', '1080p'],
      },
    };

    const result = await resolveMediaStream(item);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).toContain('取流失败');
    expect(result.message).not.toContain('youtube.com/watch');
  });

  it('opens, pauses, seeks, changes volume and follows omnidesk:mpv-state', async () => {
    let state = stoppedState;
    const handlers = new Map<string, (event: TauriEvent<unknown>) => void>();
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call') return mediaSourceResponse(payload?.args);
      if (cmd === 'mpv_status') {
        return { ok: true, provider_plugin_id: 'mpv', data: state, error: '' };
      }
      if (cmd === 'mpv_open') {
        const requestedStart = payload?.args?.startSeconds;
        state = {
          ...playingState,
          positionSec:
            typeof requestedStart === 'number' ? requestedStart : playingState.positionSec,
        };
        return { ok: true, provider_plugin_id: 'mpv', data: state, error: '' };
      }
      if (cmd === 'mpv_pause') {
        state = { ...state, playing: false, paused: true };
        return { ok: true, provider_plugin_id: 'mpv', data: state, error: '' };
      }
      if (cmd === 'mpv_seek') {
        state = { ...state, positionSec: Number(payload?.args?.seconds ?? 0) };
        return { ok: true, provider_plugin_id: 'mpv', data: state, error: '' };
      }
      if (cmd === 'mpv_volume') {
        state = { ...state, volume: Number(payload?.args?.volume ?? state.volume) };
        return { ok: true, provider_plugin_id: 'mpv', data: state, error: '' };
      }
      if (cmd === 'mpv_set_speed') {
        state = { ...state, speed: Number(payload?.args?.speed ?? state.speed) };
        return { ok: true, provider_plugin_id: 'mpv', data: state, error: '' };
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<
          NonNullable<Window['__TAURI__']>['core']
        >['invoke'],
      },
      event: {
        listen: vi.fn(async (name: string, cb: (event: TauriEvent<unknown>) => void) => {
          handlers.set(name, cb);
          return vi.fn();
        }),
      },
    };

    render(<MediaWorkbench />);

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('mpv_status');
    });

    fireEvent.click(await screen.findByRole('button', { name: '开始播放' }));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('mpv_open', {
        args: expect.objectContaining({
          url: 'https://www.youtube.com/watch?v=kCc8FmEb1nY',
          itemId: 'media-yt-attention',
          hwdec: true,
        }),
      });
    });
    await waitFor(() => {
      expect(screen.getByText('MPV')).not.toBeNull();
      expect(screen.getByText('00:12')).not.toBeNull();
    });

    fireEvent.change(screen.getByLabelText('播放时间轴'), { target: { value: '90' } });
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('mpv_seek', {
        args: { seconds: 90, relative: false },
      });
    });

    fireEvent.change(screen.getByLabelText('音量'), { target: { value: '40' } });
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('mpv_volume', { args: { volume: 40 } });
    });

    fireEvent.click(screen.getByRole('button', { name: '1.5×' }));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('mpv_set_speed', { args: { speed: 1.5 } });
      expect(screen.getByRole('button', { name: '1.5×' }).getAttribute('aria-pressed')).toBe(
        'true',
      );
    });

    fireEvent.click(screen.getByRole('button', { name: '暂停播放' }));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('mpv_pause');
    });

    act(() => {
      handlers.get(MPV_STATE_CHANNEL)?.({
        event: MPV_STATE_CHANNEL,
        id: 1,
        payload: { ...state, positionSec: 42, playing: true, paused: false },
      });
    });
    await waitFor(() => {
      expect(screen.getByText('00:42')).not.toBeNull();
    });
  });

  it('switches to the embedded libmpv canvas when the render API is available', async () => {
    const handlers = new Map<string, (event: TauriEvent<unknown>) => void>();
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call') return mediaSourceResponse(payload?.args);
      if (cmd === 'mpv_status') {
        return { ok: true, provider_plugin_id: 'mpv', data: stoppedState, error: '' };
      }
      if (cmd === 'mpv_embed_probe') {
        return {
          ok: true,
          provider_plugin_id: 'libmpv',
          data: {
            available: true,
            backend: 'libmpv-sw',
            apiVersion: '2.5',
            detail: 'libmpv 2.5 software-render API ready',
          },
          error: '',
        };
      }
      if (cmd === 'mpv_embed_enable') {
        return { ok: true, provider_plugin_id: 'libmpv', data: playingState, error: '' };
      }
      if (cmd === 'mpv_embed_set_viewport') {
        return {
          ok: true,
          provider_plugin_id: 'libmpv',
          data: { width: 960, height: 540, fps: 24 },
          error: '',
        };
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<
          NonNullable<Window['__TAURI__']>['core']
        >['invoke'],
      },
      event: {
        listen: vi.fn(async (name: string, cb: (event: TauriEvent<unknown>) => void) => {
          handlers.set(name, cb);
          return vi.fn();
        }),
      },
    };

    render(<MediaWorkbench />);

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('mpv_embed_enable');
      expect(screen.getByTestId('mpv-canvas')).not.toBeNull();
      expect(screen.getByText('LIBMPV')).not.toBeNull();
    });
    expect(handlers.has(MPV_FRAME_CHANNEL)).toBe(true);
  });
});
