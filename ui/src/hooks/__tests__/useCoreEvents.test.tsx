// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { BusEventEnvelope, EventBridgeSnapshot, TauriEvent } from '../../api';
import { BRIDGE_STATUS_CHANNEL, BUS_EVENT_CHANNEL, useCoreEvents } from '../useCoreEvents';

afterEach(() => {
  cleanup();
  delete window.__TAURI__;
  vi.restoreAllMocks();
});

describe('useCoreEvents', () => {
  it('stays inert outside Tauri without calling invoke or listen', () => {
    delete window.__TAURI__;
    const { result } = renderHook(() => useCoreEvents());

    expect(result.current.tauriAvailable).toBe(false);
    expect(result.current.coreOnline).toBe(false);
    expect(result.current.bridgeStatus).toBeNull();
  });

  it('listens to omnidesk:bus and omnidesk:bridge-status, updates plugin health, and unlistens on unmount', async () => {
    const handlers = new Map<string, (ev: TauriEvent<unknown>) => void>();
    const unlistenBus = vi.fn();
    const unlistenBridge = vi.fn();

    const invoke = vi.fn(async (cmd: string) => {
      if (cmd === 'runtime_status') {
        return { shell: 'tauri-v2', appId: 'omnidesk', version: '0.1.0', platform: 'linux', status: 'skeleton', mpv: { available: false, initialized: false, backend: 'libmpv', hwdec: 'auto-safe', status: 'skeleton', detail: 'stub' }, mpris: { enabled: false, busName: 'org.mpris', status: 'skeleton' } };
      }
      if (cmd === 'core_status') {
        return { ok: true, provider_plugin_id: 'core', data: { online: true, degraded: false }, error: '' };
      }
      if (cmd === 'event_bridge_status') {
        return { ok: true, provider_plugin_id: '', data: { status: 'degraded', supported: false, reason: 'probing', socket_path: null, forwarded_count: 0, last_event_topic: null, last_checked_epoch_ms: 1, bus_event_channel: BUS_EVENT_CHANNEL, status_event_channel: BRIDGE_STATUS_CHANNEL }, error: '' };
      }
      return { ok: true, data: {}, error: '' };
    });

    const listen = vi.fn(async (channel: string, cb: (ev: TauriEvent<unknown>) => void) => {
      handlers.set(channel, cb);
      return channel === BUS_EVENT_CHANNEL ? unlistenBus : unlistenBridge;
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
      event: { listen },
    };

    const { result, unmount } = renderHook(() => useCoreEvents());

    await waitFor(() => {
      expect(result.current.coreOnline).toBe(true);
      expect(listen).toHaveBeenCalledTimes(2);
    });
    expect(handlers.has(BUS_EVENT_CHANNEL)).toBe(true);
    expect(handlers.has(BRIDGE_STATUS_CHANNEL)).toBe(true);

    act(() => {
      const snap: EventBridgeSnapshot = {
        status: 'connected',
        supported: true,
        reason: 'Subscribed',
        socket_path: '/run/user/1000/omnidesk/control.sock',
        forwarded_count: 1,
        last_event_topic: 'plugin.fault',
        last_checked_epoch_ms: 123,
        bus_event_channel: BUS_EVENT_CHANNEL,
        status_event_channel: BRIDGE_STATUS_CHANNEL,
      };
      handlers.get(BRIDGE_STATUS_CHANNEL)?.({ event: BRIDGE_STATUS_CHANNEL, id: 1, payload: snap });
    });

    expect(result.current.bridgeStatus?.status).toBe('connected');
    expect(result.current.bridgeStatus?.supported).toBe(true);

    act(() => {
      const faultEvent: BusEventEnvelope = {
        topic: 'plugin.fault',
        event_id: 'evt-fault-1',
        source_plugin_id: 'core',
        timestamp: 1000,
        payload: {
          plugin_id: 'plugin-fnos-nas',
          component: 'capability:nas.connection.v1',
          error: 'NAS mount unreachable',
        },
      };
      handlers.get(BUS_EVENT_CHANNEL)?.({ event: BUS_EVENT_CHANNEL, id: 2, payload: faultEvent });
    });

    expect(result.current.pluginHealthMap['plugin-fnos-nas']).toMatchObject({
      pluginId: 'plugin-fnos-nas',
      healthOk: false,
      state: 'faulted',
      lastError: 'NAS mount unreachable',
    });

    act(() => {
      const recoverEvent: BusEventEnvelope = {
        topic: 'plugin.state_changed',
        event_id: 'evt-reload-1',
        source_plugin_id: 'core',
        timestamp: 1005,
        payload: { plugin_id: 'plugin-fnos-nas', state: 'reloaded' },
      };
      handlers.get(BUS_EVENT_CHANNEL)?.({ event: BUS_EVENT_CHANNEL, id: 3, payload: recoverEvent });
    });

    expect(result.current.pluginHealthMap['plugin-fnos-nas']).toMatchObject({
      pluginId: 'plugin-fnos-nas',
      healthOk: true,
      state: 'reloaded',
      lastError: '',
    });

    unmount();
    expect(unlistenBus).toHaveBeenCalledTimes(1);
    expect(unlistenBridge).toHaveBeenCalledTimes(1);
  });

  it('calls unlisten immediately if unmounted before async listen() resolves', async () => {
    const unlistenDeferreds: Array<(fn: () => void) => void> = [];
    const unlistenA = vi.fn();
    const unlistenB = vi.fn();

    window.__TAURI__ = {
      core: {
        invoke: vi.fn(async () => ({ ok: true, data: {}, error: '' })) as unknown as NonNullable<
          NonNullable<Window['__TAURI__']>['core']
        >['invoke'],
      },
      event: {
        listen: vi.fn(
          () =>
            new Promise<() => void>((resolve) => {
              unlistenDeferreds.push(resolve);
            }),
        ),
      },
    };

    const { unmount } = renderHook(() => useCoreEvents({ pollOnMount: false }));
    unmount();

    await act(async () => {
      unlistenDeferreds[0]?.(unlistenA);
      unlistenDeferreds[1]?.(unlistenB);
      await Promise.resolve();
    });

    expect(unlistenA).toHaveBeenCalledTimes(1);
    expect(unlistenB).toHaveBeenCalledTimes(1);
  });
});
