// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentSessionRecord, AgentSessionSummary } from '../../api';
import { CompanionDock } from '../CompanionDock';

const storage = new Map<string, string>();
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, String(value)),
    removeItem: (key: string) => storage.delete(key),
    clear: () => storage.clear(),
  },
});

const summary: AgentSessionSummary = {
  id: 'session-1',
  session_id: 'session-1',
  title: 'DDIA 一致性',
  domain: 'book',
  context_title: 'DDIA · Chapter 2',
  message_count: 2,
  created_at: '2026-11-17T02:00:00.000Z',
  updated_at: '2026-11-17T02:05:00.000Z',
  model: 'gemini-4-carbon',
  served_model: 'gemini-4-carbon',
  route_group: 'group/omnidesk-active',
  last_message: '线性一致性要求每次读取都能看到最近一次已完成的写入。',
  last_role: 'assistant',
};

const record: AgentSessionRecord = {
  ...summary,
  schema_version: 1,
  messages: [
    {
      id: 'message-1',
      role: 'user',
      content: '请解释线性一致性',
      created_at: '2026-11-17T02:00:00.000Z',
    },
    {
      id: 'message-2',
      role: 'assistant',
      content: '线性一致性要求每次读取都能看到最近一次已完成的写入。',
      created_at: '2026-11-17T02:05:00.000Z',
      served_model: 'gemini-4-carbon',
      route_group: 'group/omnidesk-active',
    },
  ],
};

function installTauriMock() {
  let deleted = false;
  const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
      if (cmd === 'call' && payload?.args?.op === 'capability.call') {
        cmd = 'call_capability'; payload = { args: payload.args.params as Record<string, unknown> };
      }
    if (cmd !== 'call_capability') {
      return { ok: true, provider_plugin_id: '', data: {}, error: '' };
    }
    const capability = String(payload?.args?.capability || '');
    const request = (payload?.args?.payload || {}) as Record<string, unknown>;
    if (capability === 'llm.router.v1') {
      return {
        ok: true,
        provider_plugin_id: 'plugin-magpie-agent',
        data: { gateway_running: true, primary_member: 'gemini-4-carbon:high' },
        error: '',
      };
    }
    if (capability !== 'ai.agent.v1') {
      return { ok: true, provider_plugin_id: '', data: {}, error: '' };
    }
    if (request.session_action === 'list') {
      return {
        ok: true,
        provider_plugin_id: 'plugin-magpie-agent',
        data: { sessions: deleted ? [] : [summary], count: deleted ? 0 : 1 },
        error: '',
      };
    }
    if (request.session_action === 'get') {
      return {
        ok: true,
        provider_plugin_id: 'plugin-magpie-agent',
        data: { session_id: summary.id, session: record },
        error: '',
      };
    }
    if (request.session_action === 'export') {
      const format = request.session_format === 'json' ? 'json' : 'markdown';
      return {
        ok: true,
        provider_plugin_id: 'plugin-magpie-agent',
        data: {
          session_id: summary.id,
          format,
          mime_type: format === 'json' ? 'application/json' : 'text/markdown',
          filename: `${summary.id}.${format === 'json' ? 'json' : 'md'}`,
          content: format === 'json' ? '{"id":"session-1"}' : '# DDIA 一致性\n',
        },
        error: '',
      };
    }
    if (request.session_action === 'delete') {
      deleted = true;
      return {
        ok: true,
        provider_plugin_id: 'plugin-magpie-agent',
        data: { session_id: summary.id, deleted: true },
        error: '',
      };
    }
    return {
      ok: true,
      provider_plugin_id: 'plugin-magpie-agent',
      data: {
        reply: '已继续解释：线性一致性与可串行化不是同一个概念。',
        served_model: 'gemini-4-carbon',
        route_group: 'group/omnidesk-active',
        session_id: summary.id,
        persisted: true,
        session: { ...summary, message_count: 4, last_message: '已继续解释' },
      },
      error: '',
    };
  });
  window.__TAURI__ = {
    core: {
      invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
    },
  };
  return invoke;
}

describe('CompanionDock persisted sessions', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    delete window.__TAURI__;
    vi.restoreAllMocks();
  });

  it('lists saved conversations, restores one, and continues with its session_id', async () => {
    const invoke = installTauriMock();
    render(<CompanionDock open={true} onClose={vi.fn()} activeWorkbench="book" domain="book" />);

    fireEvent.click(screen.getByRole('button', { name: '打开会话记录' }));
    expect(screen.getByTestId('companion-sessions')).not.toBeNull();
    await waitFor(() => {
      expect(screen.getByText('DDIA 一致性')).not.toBeNull();
    });

    fireEvent.click(screen.getByText('DDIA 一致性'));
    await waitFor(() => {
      const messages = within(screen.getByTestId('companion-messages'));
      expect(
        messages.getByText('线性一致性要求每次读取都能看到最近一次已完成的写入。'),
      ).not.toBeNull();
    });
    expect(screen.queryByTestId('companion-sessions')).toBeNull();

    const input = screen.getByLabelText('伴读栏提问输入');
    fireEvent.change(input, { target: { value: '继续解释可串行化' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(
        within(screen.getByTestId('companion-messages')).getByText(
          '已继续解释：线性一致性与可串行化不是同一个概念。',
        ),
      ).not.toBeNull();
    });
    expect(invoke).toHaveBeenCalledWith('call', {
      args: expect.objectContaining({ op: 'capability.call', params: expect.objectContaining({
        capability: 'ai.agent.v1', provider_plugin_id: 'plugin-magpie-agent',
        payload: expect.objectContaining({ session_id: 'session-1', prompt: '继续解释可串行化' }),
      }) }),
    });
  });

  it('exports Markdown and deletes a saved conversation', async () => {
    const invoke = installTauriMock();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:session-export'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });

    render(<CompanionDock open={true} onClose={vi.fn()} activeWorkbench="book" domain="book" />);
    fireEvent.click(screen.getByRole('button', { name: '打开会话记录' }));
    await waitFor(() => {
      expect(screen.getByText('DDIA 一致性')).not.toBeNull();
    });

    fireEvent.click(screen.getByRole('button', { name: '导出 Markdown：DDIA 一致性' }));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('call_capability', {
        args: expect.objectContaining({
          capability: 'ai.agent.v1',
          payload: expect.objectContaining({
            session_action: 'export',
            session_id: 'session-1',
            session_format: 'markdown',
          }),
        }),
      });
    });
    expect(click).toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '删除会话：DDIA 一致性' }));
    await waitFor(() => {
      expect(screen.getByText('还没有已保存的会话。发送第一条消息后会自动保存。')).not.toBeNull();
    });
  });

  it('restores the last active conversation after the dock is reopened', async () => {
    window.localStorage.setItem('omnidesk.companion.lastSessionId', 'session-1');
    installTauriMock();

    render(<CompanionDock open={true} onClose={vi.fn()} activeWorkbench="book" domain="book" />);

    await waitFor(() => {
      expect(
        within(screen.getByTestId('companion-messages')).getByText(
          '线性一致性要求每次读取都能看到最近一次已完成的写入。',
        ),
      ).not.toBeNull();
    });
    expect(screen.getByText(/会话：DDIA 一致性/)).not.toBeNull();
  });
});
