import { useEffect, useState } from 'react';
import { callCapability, isTauriAvailable, listPlugins } from '../api';

export interface AgentOption { id: string; name: string; available: boolean; tools?: boolean; reason?: string; provider?: string }
const DEFAULT: AgentOption = { id: 'builtin', name: 'OmniDesk 内置助手', available: true, provider: 'plugin-magpie-agent' };

export function useAgentSelection() {
  const [options, setOptions] = useState<AgentOption[]>([DEFAULT]);
  const [selected, setSelected] = useState(() => { try { return window.localStorage?.getItem('omnidesk.agent') || 'builtin'; } catch { return 'builtin'; } });
  useEffect(() => {
    const update = (event: Event) => {
      const value = (event as CustomEvent<string>).detail;
      if (typeof value === 'string') setSelected(value);
    };
    window.addEventListener('omnidesk:agent-selection', update);
    return () => window.removeEventListener('omnidesk:agent-selection', update);
  }, []);
  useEffect(() => {
    if (!isTauriAvailable()) return;
    let cancelled = false;
    void Promise.all([
      callCapability<{ agents: AgentOption[] }>({ capability: 'ai.agent.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'list_agents' } }),
      listPlugins(),
    ]).then(([result, plugins]) => {
      if (cancelled) return;
      const choices = result.ok && Array.isArray(result.data.agents)
        ? result.data.agents.map(agent => ({ ...agent, provider: 'plugin-magpie-agent' })) : [DEFAULT];
      if (plugins.ok && Array.isArray(plugins.data?.plugins)) for (const plugin of plugins.data.plugins) {
        if (plugin.plugin_id !== 'plugin-magpie-agent' && plugin.state === 'loaded' && plugin.provides?.includes('ai.agent.v1')) {
          choices.push({ id: plugin.plugin_id, provider: plugin.plugin_id, name: plugin.name || plugin.plugin_id, available: true });
        }
      }
      setOptions(choices);
    }).catch(() => { /* Retain explicit built-in fallback, never impersonate an unavailable agent. */ });
    return () => { cancelled = true; };
  }, []);
  const agent = options.find(item => item.id === selected) ?? { id: selected, name: `${selected} · 不可用`, available: false };
  return { options, agent, select: (id: string) => { try { window.localStorage?.setItem('omnidesk.agent', id); } catch { /* Session selection still works. */ } setSelected(id); window.dispatchEvent(new CustomEvent('omnidesk:agent-selection', { detail: id })); } };
}
