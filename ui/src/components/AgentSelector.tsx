import type { useAgentSelection } from '../hooks/useAgentSelection';

export function AgentSelector({ selection, disabled = false }: { selection: ReturnType<typeof useAgentSelection>; disabled?: boolean }) {
  const isCli = selection.agent.id === 'codex' || selection.agent.id === 'agy';
  const listId = `agent-cli-models-${selection.agent.id}`;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex min-h-11 items-center gap-2 text-sm text-[var(--text-muted)]">
        <span>Agent</span>
        <select
          aria-label="选择 Agent"
          value={selection.agent.id}
          disabled={disabled}
          onChange={event => selection.select(event.target.value)}
          className="min-h-11 min-w-0 max-w-64 rounded-lg border border-[var(--border-line)] bg-[var(--bg-surface)] px-3 text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]"
        >
          {!selection.options.some(option => option.id === selection.agent.id) ? <option value={selection.agent.id} disabled>{selection.agent.name}</option> : null}
          {selection.options.map(option => <option key={option.id} value={option.id} disabled={!option.available}>{option.name}{!option.available ? ` · ${option.reason || '不可用'}` : ''}</option>)}
        </select>
      </label>
      {isCli ? (
        <label className="flex min-h-11 items-center gap-1.5 text-sm text-[var(--text-muted)]">
          <span>模型</span>
          <input
            aria-label="选择或输入 Agent 模型"
            list={listId}
            value={selection.model}
            disabled={disabled}
            placeholder="默认模型"
            onChange={event => selection.setModel(event.target.value)}
            className="min-h-11 w-44 rounded-lg border border-[var(--border-line)] bg-[var(--bg-surface)] px-2.5 text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]"
          />
          <datalist id={listId}>
            {selection.availableModels.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
          </datalist>
        </label>
      ) : null}
    </div>
  );
}
