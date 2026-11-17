// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useCompanionChat } from '../useCompanionChat';
import { useAgentSelection } from '../../hooks/useAgentSelection';
import { callCapability } from '../../api';
vi.mock('../../api', () => ({
  isTauriAvailable: () => true,
  formatRpcError: (error: { message: string }) => error.message,
  listPlugins: async () => ({ ok: true, available: true, data: { plugins: [] } }),
  callCapability: vi.fn(async (args: { payload?: { action?: string } }) => ({
    ok: true, available: true, data: args.payload?.action === 'list_agents' ? { agents: [
      { id: 'builtin', name: '内置', available: true }, { id: 'codex', name: 'Codex', available: true },
    ] } : { reply: 'Codex 回答', served_model: 'Codex', sessions: [] },
  })),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('routes the selected agent with page references and synchronizes the other selector', async () => {
  const { result } = renderHook(() => useCompanionChat({ open: true, effectiveDomain: 'book', contextTitle: '书', restoreLastSessionOnOpen: false,
    contextEntity: { domain: 'book', workbench: 'book', itemId: 'book1', title: '书', sourceId: 'plugin-pdf-babeldoc', extra: { pageNumber: 12 } },
  }));
  const other = renderHook(() => useAgentSelection());
  await waitFor(() => expect(result.current.agentSelection.options).toHaveLength(2));
  act(() => result.current.agentSelection.select('codex'));
  expect(other.result.current.agent.id).toBe('codex');
  await act(async () => result.current.submitPrompt('分析当前页'));
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ capability: 'ai.agent.v1', providerPluginId: 'plugin-magpie-agent', payload: expect.objectContaining({
    agent_id: 'codex', prompt: '分析当前页', context_refs: [expect.objectContaining({ provider_plugin_id: 'plugin-pdf-babeldoc', payload: expect.objectContaining({ page_number: 12 }) })],
  }) }));
  expect(result.current.messages.at(-1)?.content).toBe('Codex 回答');
});
