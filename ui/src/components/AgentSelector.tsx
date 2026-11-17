import type { useAgentSelection } from '../hooks/useAgentSelection';

export function AgentSelector({ selection, disabled = false }: { selection: ReturnType<typeof useAgentSelection>; disabled?: boolean }) {
  return <label className="flex min-h-11 items-center gap-2 text-sm text-[var(--text-muted)]">
    <span>Agent</span>
    <select aria-label="选择 Agent" value={selection.agent.id} disabled={disabled}
      onChange={event => selection.select(event.target.value)}
      className="min-h-11 min-w-0 max-w-64 rounded-lg border border-[var(--border-line)] bg-[var(--bg-surface)] px-3 text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]">
      {!selection.options.some(option => option.id === selection.agent.id) ? <option value={selection.agent.id} disabled>{selection.agent.name}</option> : null}
      {selection.options.map(option => <option key={option.id} value={option.id} disabled={!option.available}>{option.name}{!option.available ? ` · ${option.reason || '不可用'}` : ''}</option>)}
    </select>
  </label>;
}
