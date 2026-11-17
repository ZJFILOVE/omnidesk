// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  call,
  callCapability,
  coreStatus,
  domainSources,
  emitEvent,
  eventBridgeStatus,
  formatRpcError,
  isTauriAvailable,
  listPlugins,
  readSource,
  runtimeStatus,
} from '../index';

afterEach(() => {
  delete window.__TAURI__;
  vi.restoreAllMocks();
});

describe('Typed Tauri RPC client', () => {
  it('returns { available: false } for all commands outside Tauri', async () => {
    delete window.__TAURI__;
    expect(isTauriAvailable()).toBe(false);

    const results = await Promise.all([
      runtimeStatus(),
      coreStatus(),
      listPlugins(),
      callCapability({ capability: 'ai.agent.v1', payload: { prompt: 'hi' } }),
      emitEvent({ topic: 'entity.media.open', payload: { item_id: 'm1' } }),
      readSource({ domain: 'media', sourceId: 'plugin-music' }),
      call({ op: 'ping' }),
      eventBridgeStatus(),
    ]);

    for (const res of results) {
      expect(res.available).toBe(false);
      expect(res.ok).toBe(false);
      expect(res.error?.code).toBe('tauri_unavailable');
    }
  });

  it('passes RawJsonArgs { args } wrapper and unwraps success envelopes inside Tauri', async () => {
    const invoke = vi.fn(async (cmd: string) => {
      if (cmd === 'runtime_status') {
        return {
          shell: 'tauri-v2',
          appId: 'omnidesk',
          version: '0.1.0',
          platform: 'linux',
          status: 'skeleton',
          mpv: { available: false, initialized: false, backend: 'libmpv', hwdec: 'auto-safe', status: 'skeleton', detail: 'stub' },
          mpris: { enabled: false, busName: 'org.mpris.MediaPlayer2.omnidesk', status: 'skeleton' },
        };
      }
      if (cmd === 'core_status') {
        return { ok: true, provider_plugin_id: 'core', data: { online: true, degraded: false }, error: '' };
      }
      if (cmd === 'event_bridge_status') {
        return { ok: true, provider_plugin_id: '', data: { status: 'connected', supported: true, forwarded_count: 3 }, error: '' };
      }
      return {
        ok: true,
        provider_plugin_id: 'plugin-magpie-agent',
        data: { echoCmd: cmd },
        error: '',
      };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };
    expect(isTauriAvailable()).toBe(true);

    const rt = await runtimeStatus();
    expect(rt.available).toBe(true);
    expect(rt.ok).toBe(true);
    if (rt.ok) expect(rt.data.shell).toBe('tauri-v2');
    expect(invoke).toHaveBeenCalledWith('runtime_status');

    const cs = await coreStatus();
    expect(cs.ok).toBe(true);
    expect(invoke).toHaveBeenCalledWith('core_status');

    const eb = await eventBridgeStatus();
    expect(eb.ok).toBe(true);
    expect(invoke).toHaveBeenCalledWith('event_bridge_status');

    await listPlugins({ liveOnly: true });
    expect(invoke).toHaveBeenCalledWith('list_plugins', { args: { liveOnly: true } });

    const cap = await callCapability({
      capability: 'ai.agent.v1',
      payload: { prompt: 'hello' },
      timeoutMs: 5000,
    });
    expect(cap.ok).toBe(true);
    if (cap.ok) expect(cap.providerPluginId).toBe('plugin-magpie-agent');
    expect(invoke).toHaveBeenCalledWith('call_capability', {
      args: { capability: 'ai.agent.v1', payload: { prompt: 'hello' }, timeoutMs: 5000 },
    });

    await emitEvent({ topic: 'entity.media.open', payload: { item_id: 'm1' } });
    expect(invoke).toHaveBeenCalledWith('emit_event', {
      args: { topic: 'entity.media.open', payload: { item_id: 'm1' } },
    });

    await readSource({ domain: 'text', sourceId: 'plugin-journals', payload: { page: 1 } });
    expect(invoke).toHaveBeenCalledWith('read_source', {
      args: { domain: 'text', sourceId: 'plugin-journals', payload: { page: 1 } },
    });

    await call({ op: 'plugin.reload', params: { plugin_id: 'plugin-demo' } });
    expect(invoke).toHaveBeenCalledWith('call', {
      args: { op: 'plugin.reload', params: { plugin_id: 'plugin-demo' } },
    });
  });

  it('maps source categories from domain.snapshot metadata', async () => {
    const invoke = vi.fn(async () => ({
      ok: true,
      provider_plugin_id: '',
      data: {
        media_sources: [
          {
            source_id: 'plugin-youtube',
            plugin_id: 'plugin-youtube',
            title: 'YouTube',
            category: '',
            priority: 20,
            metadata: {
              categories: ['subscriptions', 'trending', 'podcast', 'history'],
            },
          },
        ],
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

    const result = await domainSources('media');
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.data[0]?.categories).toEqual([
      'subscriptions',
      'trending',
      'podcast',
      'history',
    ]);
    expect(invoke).toHaveBeenCalledWith('call', {
      args: { op: 'domain.snapshot', params: { domain: 'media' } },
    });
  });

  it('normalizes backend error envelopes and rejected invoke calls with real code and message', async () => {
    const invoke = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        error_code: 'service_offline',
        error: 'Cannot connect to control.sock',
        provider_plugin_id: '',
        data: { error_code: 'service_offline', online: false },
      })
      .mockRejectedValueOnce(new Error('IPC bridge crashed'));

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };

    const backendErr = await callCapability({ capability: 'llm.chat.v1', payload: { prompt: 'hi' } });
    expect(backendErr.available).toBe(true);
    expect(backendErr.ok).toBe(false);
    expect(backendErr.error?.code).toBe('service_offline');
    expect(backendErr.error?.message).toBe('Cannot connect to control.sock');
    expect(formatRpcError(backendErr.error!)).toBe('[service_offline] Cannot connect to control.sock');

    const thrownErr = await call({ op: 'ping' });
    expect(thrownErr.available).toBe(true);
    expect(thrownErr.ok).toBe(false);
    expect(thrownErr.error?.code).toBe('ipc_invoke_error');
    expect(thrownErr.error?.message).toBe('IPC bridge crashed');
  });
});
