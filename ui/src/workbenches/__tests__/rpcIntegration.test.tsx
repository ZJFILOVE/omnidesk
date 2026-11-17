// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import type { BusEventEnvelope, TauriEvent } from '../../api';
import { CompanionDock } from '../../components/CompanionDock';
import { BUS_EVENT_CHANNEL } from '../../hooks/useCoreEvents';
import { AiWorkbench } from '../AiWorkbench';
import { PluginStore } from '../PluginStore';

afterEach(() => {
  cleanup();
  delete window.__TAURI__;
  vi.restoreAllMocks();
});

describe('Offline preview behavior outside Tauri', () => {
  it('shows offline preview banner and forbids fake writes in PluginStore', () => {
    delete window.__TAURI__;
    render(<PluginStore />);

    expect(screen.getByTestId('store-offline-banner').textContent).toContain('离线预览：数据为示例');

    fireEvent.click(screen.getByRole('button', { name: '保存配置 (0600)' }));
    const status = screen.getByTestId('store-status-message');
    expect(status.textContent).toContain('离线预览');
    expect(status.textContent).not.toContain('已保存');

    fireEvent.click(screen.getByRole('button', { name: '热重载 (.py)' }));
    expect(screen.getByTestId('store-status-message').textContent).toContain('离线预览');
    expect(screen.getByTestId('store-status-message').textContent).not.toContain('热重载完成');

    fireEvent.click(screen.getByRole('button', { name: '运行契约自检' }));
    expect(screen.getByTestId('store-status-message').textContent).toContain('离线预览');
    expect(screen.getByTestId('store-status-message').textContent).not.toContain('自检通过');
  });

  it('clearly marks demo replies outside Tauri in AiWorkbench and CompanionDock', () => {
    delete window.__TAURI__;
    render(<AiWorkbench />);
    expect(screen.getByTestId('ai-offline-banner').textContent).toContain('离线预览：数据为示例');

    const aiInput = screen.getByLabelText('AI 策展对话输入框');
    fireEvent.change(aiInput, { target: { value: '推荐几篇 KV 论文' } });
    fireEvent.click(screen.getByRole('button', { name: '发送执行' }));
    expect(screen.getByText(/当前为浏览器静态示例演示/)).not.toBeNull();
    expect(screen.getAllByText(/离线预览示例/).length).toBeGreaterThanOrEqual(2);

    cleanup();
    render(<CompanionDock open={true} onClose={vi.fn()} />);
    expect(screen.getByTestId('dock-offline-banner').textContent).toContain('离线预览：数据为示例');
    const dockInput = screen.getByLabelText('伴读栏提问输入');
    fireEvent.change(dockInput, { target: { value: '总结当前内容' } });
    fireEvent.keyDown(dockInput, { key: 'Enter' });
    const dockMessages = within(screen.getByTestId('companion-messages'));
    expect(dockMessages.getByText(/当前为浏览器离线示例演示/)).not.toBeNull();
    expect(dockMessages.getAllByText(/离线预览示例/).length).toBeGreaterThanOrEqual(2);
  });
});

describe('PluginStore real Tauri RPC and bus health updates', () => {
  it('loads plugins via list_plugins, executes reload/verify/save via call/call_capability, and surfaces backend errors', async () => {
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call' && payload?.args?.op === 'capability.call') {
        cmd = 'call_capability'; payload = { args: payload.args.params as Record<string, unknown> };
      }
      if (cmd === 'list_plugins') {
        return {
          ok: true,
          provider_plugin_id: '',
          data: {
            source: 'control_socket',
            core_online: true,
            degraded: false,
            plugins: [
              {
                plugin_id: 'plugin-fnos-nas',
                state: 'loaded',
                enabled: true,
                error: '',
                reload_count: 1,
                name: 'Feiniu fnOS NAS Connector',
                version: '0.2.0',
                kind: 'hybrid',
                domain: 'media',
                provides: ['nas.connection.v1'],
                consumes: [],
              },
            ],
          },
          error: '',
        };
      }
      if (cmd === 'call') {
        const op = payload?.args?.op;
        if (op === 'plugin.reload') {
          return {
            ok: true,
            provider_plugin_id: 'plugin-fnos-nas',
            data: { plugin_id: 'plugin-fnos-nas', state: 'loaded', enabled: true, reload_count: 2 },
            error: '',
          };
        }
        if (op === 'plugin.verify') {
          return {
            ok: false,
            error_code: 'validation_error',
            error: 'pytest failed in test_fnos.py',
            provider_plugin_id: 'plugin-fnos-nas',
            data: { error_code: 'validation_error' },
          };
        }
      }
      if (cmd === 'call_capability') {
        return {
          ok: false,
          error_code: 'capability_not_found',
          error: "Capability 'plugin.config.v1' is not registered",
          provider_plugin_id: '',
          data: { error_code: 'capability_not_found' },
        };
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };

    render(<PluginStore />);

    await waitFor(() => {
      expect(screen.getByTestId('store-status-message').textContent).toContain('已从 Core 载入 1 个插件');
    });
    expect(invoke).toHaveBeenCalledWith('list_plugins', { args: {} });

    // Hot reload succeeds
    fireEvent.click(screen.getByRole('button', { name: '热重载 (.py)' }));
    await waitFor(() => {
      expect(screen.getByTestId('store-status-message').textContent).toContain('reload_count=2');
    });
    expect(invoke).toHaveBeenCalledWith('call', {
      args: { op: 'plugin.reload', params: { plugin_id: 'plugin-fnos-nas' } },
    });

    // Verify fails -> shows real error code & message, never success toast
    fireEvent.click(screen.getByRole('button', { name: '运行契约自检' }));
    await waitFor(() => {
      const msg = screen.getByTestId('store-status-message').textContent ?? '';
      expect(msg).toContain('validation_error');
      expect(msg).toContain('pytest failed');
      expect(msg).not.toContain('自检通过');
    });

    // Save config calls call_capability plugin.config.v1 and surfaces capability_not_found error
    fireEvent.click(screen.getByRole('button', { name: '保存配置 (0600)' }));
    await waitFor(() => {
      const msg = screen.getByTestId('store-status-message').textContent ?? '';
      expect(msg).toContain('capability_not_found');
      expect(msg).not.toContain('已保存');
    });
    expect(invoke).toHaveBeenCalledWith('call_capability', {
      args: expect.objectContaining({
        capability: 'plugin.config.v1',
        payload: expect.objectContaining({ action: 'set', plugin_id: 'plugin-fnos-nas' }),
      }),
    });
  });

  it('updates plugin health in PluginStore when omnidesk:bus emits plugin.fault in App', async () => {
    const handlers = new Map<string, (ev: TauriEvent<unknown>) => void>();
    const invoke = vi.fn(async (cmd: string) => {
      if (cmd === 'runtime_status') {
        return { shell: 'tauri-v2', appId: 'omnidesk', version: '0.1.0', platform: 'linux', status: 'skeleton', mpv: { available: false, initialized: false, backend: 'libmpv', hwdec: 'auto-safe', status: 'skeleton', detail: 'stub' }, mpris: { enabled: false, busName: 'org.mpris', status: 'skeleton' } };
      }
      if (cmd === 'core_status') {
        return { ok: true, provider_plugin_id: 'core', data: { online: true, degraded: false }, error: '' };
      }
      if (cmd === 'event_bridge_status') {
        return { ok: true, provider_plugin_id: '', data: { status: 'connected', supported: true, reason: 'ok', socket_path: null, forwarded_count: 0, last_event_topic: null, last_checked_epoch_ms: 1, bus_event_channel: BUS_EVENT_CHANNEL, status_event_channel: 'omnidesk:bridge-status' }, error: '' };
      }
      if (cmd === 'list_plugins') {
        return {
          ok: true,
          provider_plugin_id: '',
          data: {
            source: 'control_socket',
            core_online: true,
            degraded: false,
            plugins: [
              { plugin_id: 'plugin-magpie-agent', state: 'loaded', enabled: true, error: '', name: 'Magpie AI Agent Router', version: '0.2.0', kind: 'provider', domain: 'core', provides: ['llm.chat.v1'], consumes: [] },
            ],
          },
          error: '',
        };
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
      event: {
        listen: vi.fn(async (name: string, cb: (ev: TauriEvent<unknown>) => void) => {
          handlers.set(name, cb);
          return vi.fn();
        }),
      },
    };

    render(<App />);
    fireEvent.keyDown(window, { key: '5', ctrlKey: true });

    await waitFor(() => {
      expect(screen.getByTestId('plugin-health-badge').textContent).toBe('health: ok');
    });

    act(() => {
      const fault: BusEventEnvelope = {
        topic: 'plugin.fault',
        event_id: 'f-1',
        source_plugin_id: 'core',
        timestamp: 100,
        payload: { plugin_id: 'plugin-magpie-agent', component: 'capability:llm.chat.v1', error: 'Magpie crashed' },
      };
      handlers.get(BUS_EVENT_CHANNEL)?.({ event: BUS_EVENT_CHANNEL, id: 1, payload: fault });
    });

    await waitFor(() => {
      expect(screen.getByTestId('plugin-health-badge').textContent).toBe('health: fault');
      expect(screen.getByTestId('plugin-runtime-error').textContent).toContain('Magpie crashed');
    });
  });
});

describe('AiWorkbench & CompanionDock real capability RPC', () => {
  it('routes chart-style prompts through ai.curation.v1 and renders verified cards', async () => {
    const invokedCapabilities: string[] = [];
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call' && payload?.args?.op === 'capability.call') {
        cmd = 'call_capability'; payload = { args: payload.args.params as Record<string, unknown> };
      }
      if (cmd === 'call_capability') {
        const cap = String(payload?.args?.capability ?? '');
        invokedCapabilities.push(cap);
        const capPayload = payload?.args?.payload as Record<string, unknown> | undefined;
        if (cap === 'llm.router.v1') {
          return {
            ok: true,
            provider_plugin_id: 'plugin-magpie-agent',
            data: { gateway_running: true, online: true, group_id: 'omnidesk-active', primary_member: 'gemini/carbon:high', routing: 'order' },
            error: '',
          };
        }
        if (cap === 'ai.curation.v1') {
          expect(capPayload?.action).toBe('curate_interactive');
          expect(capPayload?.prompt).toContain('最火');
          return {
            ok: true,
            provider_plugin_id: 'plugin-magpie-agent',
            data: {
              mode: 'interactive_ephemeral',
              session_id: 'ix-chat-test',
              feed_written: true,
              playlist_synced: true,
              media_items: [
                { id: 'am-cn-9001', title: 'Chart Track 9001', creator: 'Artist A', subtitle: 'Artist A · 国区实时榜 #1' },
                { id: 'am-cn-9002', title: 'Chart Track 9002', creator: 'Artist B', subtitle: 'Artist B · 国区实时榜 #2' },
                { id: 'am-cn-9003', title: 'Chart Track 9003', creator: 'Artist C', subtitle: 'Artist C · 国区实时榜 #3' },
              ],
            },
            error: '',
          };
        }
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };

    render(<AiWorkbench />);
    await waitFor(() => {
      expect(screen.getByTestId('ai-gateway-badge').textContent).toContain('ONLINE');
    });

    const input = screen.getByLabelText('AI 策展对话输入框');
    fireEvent.change(input, { target: { value: '写三首目前最火的歌到歌单' } });
    fireEvent.click(screen.getByRole('button', { name: '发送执行' }));

    await waitFor(() => {
      expect(screen.getByText(/已反写 3 条推荐到卡片页/)).not.toBeNull();
    });
    expect(screen.getByText('Chart Track 9001')).not.toBeNull();
    expect(screen.getByText('Chart Track 9002')).not.toBeNull();
    expect(screen.getByText('Chart Track 9003')).not.toBeNull();
    expect(invokedCapabilities).not.toContain('llm.chat.v1');
  });

  it('requests media and text domains for paper + YouTube prompts', async () => {
    let capturedPayload: Record<string, unknown> | undefined;
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call' && payload?.args?.op === 'capability.call') {
        cmd = 'call_capability'; payload = { args: payload.args.params as Record<string, unknown> };
      }
      if (cmd === 'call_capability') {
        const cap = String(payload?.args?.capability ?? '');
        if (cap === 'llm.router.v1') {
          return {
            ok: true,
            provider_plugin_id: 'plugin-magpie-agent',
            data: { gateway_running: true, online: true, group_id: 'omnidesk-active', primary_member: 'gemini/carbon:high', routing: 'order' },
            error: '',
          };
        }
        if (cap === 'ai.curation.v1') {
          capturedPayload = payload?.args?.payload as Record<string, unknown>;
          return {
            ok: true,
            provider_plugin_id: 'plugin-magpie-agent',
            data: {
              mode: 'interactive_ephemeral',
              session_id: 'ix-mixed-test',
              feed_written: true,
              playlist_synced: false,
              media_items: [{ id: 'yt-1', title: 'KV Cache video', creator: 'YouTube', kind: 'video' }],
              text_items: [{ id: 'paper-1', title: 'KV Cache paper', author: 'Researcher' }],
            },
            error: '',
          };
        }
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };

    render(<AiWorkbench />);
    await waitFor(() => {
      expect(screen.getByTestId('ai-gateway-badge').textContent).toContain('ONLINE');
    });

    fireEvent.change(screen.getByLabelText('AI 策展对话输入框'), {
      target: { value: '结合 KV Cache 淘汰边界论文，推荐 2 篇同主题实证工作和对应 YouTube 讲解。' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送执行' }));

    await waitFor(() => {
      expect(capturedPayload?.domains).toEqual(['media', 'text']);
    });
  });

  it('sends llm.chat.v1 in AiWorkbench and surfaces real reply or real backend error', async () => {
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call' && payload?.args?.op === 'capability.call') {
        cmd = 'call_capability'; payload = { args: payload.args.params as Record<string, unknown> };
      }
      if (cmd === 'call_capability') {
        const cap = payload?.args?.capability;
        const capPayload = payload?.args?.payload as Record<string, unknown> | undefined;
        if (cap === 'llm.router.v1') {
          if (capPayload?.action === 'current_route') {
            return {
              ok: true,
              provider_plugin_id: 'plugin-magpie-agent',
              data: { gateway_running: true, online: true, group_id: 'omnidesk-active', primary_member: 'codex/gpt-6-luna:high', routing: 'order' },
              error: '',
            };
          }
          return { ok: true, provider_plugin_id: 'plugin-magpie-agent', data: { models: [], quotas: [] }, error: '' };
        }
        if (cap === 'llm.chat.v1') {
          const msgs = capPayload?.messages as Array<{ content: string }> | undefined;
          const last = msgs?.[msgs.length - 1]?.content ?? '';
          if (last.includes('fail')) {
            return {
              ok: false,
              error_code: 'quota_exceeded',
              error: '订阅配额已耗尽',
              provider_plugin_id: 'plugin-magpie-agent',
              data: { error_code: 'quota_exceeded' },
            };
          }
          return {
            ok: true,
            provider_plugin_id: 'plugin-magpie-agent',
            data: { reply: '真实后端回答：已核验归档。', served_model: 'codex/gpt-6-luna' },
            error: '',
          };
        }
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };

    render(<AiWorkbench />);
    await waitFor(() => {
      expect(screen.getByTestId('ai-gateway-badge').textContent).toContain('ONLINE');
    });

    const input = screen.getByLabelText('AI 策展对话输入框');
    fireEvent.change(input, { target: { value: '请检索今日归档' } });
    fireEvent.click(screen.getByRole('button', { name: '发送执行' }));

    await waitFor(() => {
      expect(screen.getByText('真实后端回答：已核验归档。')).not.toBeNull();
    });

    fireEvent.change(input, { target: { value: 'please fail' } });
    fireEvent.click(screen.getByRole('button', { name: '发送执行' }));

    await waitFor(() => {
      expect(screen.getByTestId('ai-error-banner').textContent).toContain('[quota_exceeded] 订阅配额已耗尽');
    });
  });

  it('selects nested auto route without sending an unsupported effort suffix', async () => {
    const setRoutePayloads: Array<Record<string, unknown>> = [];
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call' && payload?.args?.op === 'capability.call') {
        cmd = 'call_capability'; payload = { args: payload.args.params as Record<string, unknown> };
      }
      if (cmd === 'call_capability') {
        const cap = payload?.args?.capability;
        const capPayload = payload?.args?.payload as Record<string, unknown> | undefined;
        if (cap === 'llm.router.v1') {
          if (capPayload?.action === 'current_route') {
            return {
              ok: true,
              provider_plugin_id: 'plugin-magpie-agent',
              data: {
                gateway_running: true,
                online: true,
                group_id: 'omnidesk-active',
                primary_member: 'codex/gpt-6-luna:high',
                routing: 'order',
              },
              error: '',
            };
          }
          if (capPayload?.action === 'list_models') {
            return {
              ok: true,
              provider_plugin_id: 'plugin-magpie-agent',
              data: {
                models: [{ id: 'auto/glm-5.3-flash' }, { id: 'codex/gpt-6-luna' }],
                groups: [],
              },
              error: '',
            };
          }
          if (capPayload?.action === 'set_route') {
            setRoutePayloads.push(capPayload);
            return {
              ok: true,
              provider_plugin_id: 'plugin-magpie-agent',
              data: { primary_member: capPayload.model_id, routing: 'order' },
              error: '',
            };
          }
          return {
            ok: true,
            provider_plugin_id: 'plugin-magpie-agent',
            data: { quotas: [] },
            error: '',
          };
        }
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };

    render(<AiWorkbench />);
    await waitFor(() => {
      expect(screen.getByTestId('ai-gateway-badge').textContent).toContain('ONLINE');
    });

    fireEvent.click(await screen.findByRole('button', { name: /auto\/glm-5\.3-flash/ }));
    await waitFor(() => {
      expect(setRoutePayloads).toHaveLength(1);
      expect((screen.getByRole('button', { name: /^high$/ }) as HTMLButtonElement).disabled).toBe(
        true,
      );
    });

    expect(setRoutePayloads[0].model_id).toBe('auto/glm-5.3-flash');
    expect(setRoutePayloads[0].effort).toBeUndefined();
    expect(screen.queryByTestId('ai-error-banner')).toBeNull();
  });

  it('sends ai.agent.v1 in CompanionDock and surfaces real error on failure', async () => {
    const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call' && payload?.args?.op === 'capability.call') {
        cmd = 'call_capability'; payload = { args: payload.args.params as Record<string, unknown> };
      }
      if (cmd === 'call_capability') {
        const cap = payload?.args?.capability;
        const capPayload = payload?.args?.payload as Record<string, unknown> | undefined;
        if (cap === 'llm.router.v1') {
          return { ok: true, provider_plugin_id: 'plugin-magpie-agent', data: { gateway_running: true, primary_member: 'gemini-4-carbon:high' }, error: '' };
        }
        if (cap === 'ai.agent.v1') {
          if (String(capPayload?.prompt ?? '').includes('error')) {
            return {
              ok: false,
              error_code: 'service_offline',
              error: 'Magpie 网关未启动',
              provider_plugin_id: 'plugin-magpie-agent',
              data: { error_code: 'service_offline' },
            };
          }
          return {
            ok: true,
            provider_plugin_id: 'plugin-magpie-agent',
            data: { reply: '伴读真实回复：核心主句清晰。', route_group: 'group/omnidesk-active', served_model: 'gemini-4-carbon' },
            error: '',
          };
        }
      }
      return { ok: true, data: {}, error: '' };
    });

    window.__TAURI__ = {
      core: {
        invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
      },
    };

    render(
      <CompanionDock
        open={true}
        onClose={vi.fn()}
        activeWorkbench="book"
        domain="book"
        contextEntity={{ domain: 'book', workbench: 'book', itemId: 'b1', title: 'DDIA' }}
      />,
    );

    const textarea = screen.getByLabelText('伴读栏提问输入');
    fireEvent.change(textarea, { target: { value: '解释这一段' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });

    await waitFor(() => {
      expect(within(screen.getByTestId('companion-messages')).getByText('伴读真实回复：核心主句清晰。')).not.toBeNull();
    });
    expect(invoke).toHaveBeenCalledWith('call', {
      args: expect.objectContaining({ op: 'capability.call', params: expect.objectContaining({
        capability: 'ai.agent.v1', provider_plugin_id: 'plugin-magpie-agent',
        payload: expect.objectContaining({ action: 'chat', domain: 'book', prompt: '解释这一段' }),
      }) }),
    });

    fireEvent.change(textarea, { target: { value: 'trigger error' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });

    await waitFor(() => {
      expect(screen.getByTestId('dock-error-banner').textContent).toContain('[service_offline] Magpie 网关未启动');
    });
  });
});
