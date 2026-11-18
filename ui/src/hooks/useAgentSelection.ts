import { useEffect, useState } from 'react';
import { callCapability, isTauriAvailable, listPlugins } from '../api';

export interface AgentOption { id: string; name: string; available: boolean; tools?: boolean; reason?: string; provider?: string }
export interface CliModelOption { id: string; name: string }
const DEFAULT_AGENTS: AgentOption[] = [
  { id: 'codex', name: 'Codex · MCP 工具', available: true, tools: true, provider: 'plugin-magpie-agent' },
  { id: 'agy', name: 'AGY · MCP 工具', available: true, tools: true, provider: 'plugin-magpie-agent' },
];

function readSavedModel(agentId: string): string {
  try { return window.localStorage?.getItem(`omnidesk.agent_model.${agentId}`) || ''; } catch { return ''; }
}

function readSavedAgent(): string {
  try {
    const saved = window.localStorage?.getItem('omnidesk.agent');
    return saved && saved !== 'builtin' ? saved : 'codex';
  } catch {
    return 'codex';
  }
}

export function useAgentSelection() {
  const [options, setOptions] = useState<AgentOption[]>(DEFAULT_AGENTS);
  const [cliModels, setCliModels] = useState<{ codex: CliModelOption[]; agy: CliModelOption[] }>({ codex: [], agy: [] });
  const [configuredDefaults, setConfiguredDefaults] = useState<{ codex: string; agy: string }>({ codex: '', agy: '' });
  const [selected, setSelected] = useState(readSavedAgent);
  const [modelsByAgent, setModelsByAgent] = useState<Record<string, string>>(() => ({
    codex: readSavedModel('codex'),
    agy: readSavedModel('agy'),
  }));

  useEffect(() => {
    const update = (event: Event) => {
      const value = (event as CustomEvent<string>).detail;
      if (typeof value === 'string') setSelected(value);
    };
    const updateModel = (event: Event) => {
      const detail = (event as CustomEvent<{ agentId: string; model: string }>).detail;
      if (detail?.agentId) {
        setModelsByAgent((prev) => ({ ...prev, [detail.agentId]: detail.model }));
      }
    };
    window.addEventListener('omnidesk:agent-selection', update);
    window.addEventListener('omnidesk:agent-model', updateModel);
    return () => {
      window.removeEventListener('omnidesk:agent-selection', update);
      window.removeEventListener('omnidesk:agent-model', updateModel);
    };
  }, []);

  useEffect(() => {
    if (!isTauriAvailable()) return;
    let cancelled = false;
    void Promise.all([
      callCapability<{ agents: AgentOption[] }>({ capability: 'ai.agent.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'list_agents' } }),
      listPlugins(),
      callCapability<{ codex?: CliModelOption[]; agy?: CliModelOption[] }>({ capability: 'llm.translate.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'cli_models' } }).catch(() => null),
      callCapability<{ codex_model?: string; agy_model?: string }>({ capability: 'llm.translate.v1', providerPluginId: 'plugin-magpie-agent', payload: { action: 'settings' } }).catch(() => null),
    ]).then(([result, plugins, cliRes, settingsRes]) => {
      if (cancelled) return;
      const choices = result.ok && Array.isArray(result.data.agents)
        ? result.data.agents.map(agent => ({ ...agent, provider: 'plugin-magpie-agent' })) : DEFAULT_AGENTS;
      if (plugins.ok && Array.isArray(plugins.data?.plugins)) for (const plugin of plugins.data.plugins) {
        if (plugin.plugin_id !== 'plugin-magpie-agent' && plugin.state === 'loaded' && plugin.provides?.includes('ai.agent.v1')) {
          choices.push({ id: plugin.plugin_id, provider: plugin.plugin_id, name: plugin.name || plugin.plugin_id, available: true });
        }
      }
      setOptions(choices);
      setSelected((prev) => {
        const cur = choices.find((c) => c.id === prev);
        if (cur && cur.available) return prev;
        const firstAvailable = choices.find((c) => c.available);
        return firstAvailable ? firstAvailable.id : prev;
      });
      if (cliRes && cliRes.ok) {
        setCliModels({ codex: cliRes.data.codex ?? [], agy: cliRes.data.agy ?? [] });
      }
      if (settingsRes && settingsRes.ok) {
        setConfiguredDefaults({ codex: settingsRes.data.codex_model ?? '', agy: settingsRes.data.agy_model ?? '' });
      }
    }).catch(() => { /* Retain explicit built-in fallback, never impersonate an unavailable agent. */ });
    return () => { cancelled = true; };
  }, []);

  const agent = options.find(item => item.id === selected) ?? { id: selected, name: `${selected} · 不可用`, available: false };
  const isCli = agent.id === 'codex' || agent.id === 'agy';
  const model = isCli ? (modelsByAgent[agent.id] || configuredDefaults[agent.id as 'codex' | 'agy'] || '') : '';
  const availableModels = agent.id === 'codex' ? cliModels.codex : agent.id === 'agy' ? cliModels.agy : [];

  return {
    options,
    agent,
    model,
    availableModels,
    select: (id: string) => {
      try { window.localStorage?.setItem('omnidesk.agent', id); } catch { /* Session selection still works. */ }
      setSelected(id);
      window.dispatchEvent(new CustomEvent('omnidesk:agent-selection', { detail: id }));
    },
    setModel: (nextModel: string) => {
      const targetId = agent.id;
      try { window.localStorage?.setItem(`omnidesk.agent_model.${targetId}`, nextModel); } catch { /* Session selection still works. */ }
      setModelsByAgent((prev) => ({ ...prev, [targetId]: nextModel }));
      window.dispatchEvent(new CustomEvent('omnidesk:agent-model', { detail: { agentId: targetId, model: nextModel } }));
    },
  };
}
