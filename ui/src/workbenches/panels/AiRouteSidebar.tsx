import React from 'react';
import { FocusButton, HAIRLINE_BORDER, SectionLabel } from '../../components/WorkbenchLayout';
import { AI_ARCHIVE_SNAPSHOT, type MagpieRouteState } from '../../data/mock';

const EFFORT_OPTIONS: MagpieRouteState['effort'][] = ['low', 'medium', 'high', 'xhigh'];
const STRATEGY_OPTIONS: MagpieRouteState['strategy'][] = ['order', 'smart', 'usage', 'pace'];

export interface AiRouteSidebarProps {
  route: MagpieRouteState;
  archiveSyncTime: string;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onSelectModel: (model: string) => void;
  onSelectEffort: (effort: MagpieRouteState['effort']) => void;
  onSelectStrategy: (strategy: MagpieRouteState['strategy']) => void;
  onRehydrate: () => void;
}

export const AiRouteSidebar: React.FC<AiRouteSidebarProps> = ({
  route,
  archiveSyncTime,
  activeFocusId,
  onFocus,
  onSelectModel,
  onSelectEffort,
  onSelectStrategy,
  onRehydrate,
}) => (
  <div className="flex flex-col gap-4 p-4">
    <div className="flex flex-col gap-2">
      <SectionLabel>Magpie 模型路由控制</SectionLabel>
      <div className="flex flex-col gap-1">
        {route.availableModels.map((model) => {
          const selected = route.primaryModel === model;
          return (
            <FocusButton
              key={model}
              focusId={`ai:model:${model}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSelectModel(model)}
              selected={selected}
              tone="core"
              className="px-2.5 py-1.5 text-left font-mono text-[11px]"
            >
              {selected ? '● ' : '○ '}
              {model}
            </FocusButton>
          );
        })}
      </div>

      <div className="mt-1">
        <div
          className="mb-1 font-mono text-[10px] uppercase"
          style={{ color: 'var(--text-muted, #66625D)' }}
        >
          推理强度 (Effort)
        </div>
        <div className="grid grid-cols-4 gap-1">
          {EFFORT_OPTIONS.map((effort) => (
            <FocusButton
              key={effort}
              focusId={`ai:effort:${effort}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSelectEffort(effort)}
              selected={route.effort === effort}
              tone="core"
              className="py-1 font-mono text-[10px]"
            >
              {effort}
            </FocusButton>
          ))}
        </div>
      </div>

      <div className="mt-1">
        <div
          className="mb-1 font-mono text-[10px] uppercase"
          style={{ color: 'var(--text-muted, #66625D)' }}
        >
          调度策略 (Strategy)
        </div>
        <div className="grid grid-cols-4 gap-1">
          {STRATEGY_OPTIONS.map((strategy) => (
            <FocusButton
              key={strategy}
              focusId={`ai:strategy:${strategy}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSelectStrategy(strategy)}
              selected={route.strategy === strategy}
              tone="core"
              className="py-1 font-mono text-[10px]"
            >
              {strategy}
            </FocusButton>
          ))}
        </div>
      </div>
    </div>

    <div className="flex flex-col gap-2 border-t pt-3" style={{ borderColor: HAIRLINE_BORDER }}>
      <SectionLabel>实时订阅额度</SectionLabel>
      {route.quotas.map((q) => (
        <div key={q.model} className="text-xs">
          <div className="flex justify-between font-mono text-[10px]">
            <span>
              {q.provider} · {q.model}
            </span>
            <span>
              {q.usedPct}% · {q.resetIn}
            </span>
          </div>
          <div
            className="mt-1 h-1.5 overflow-hidden rounded-full"
            style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)' }}
          >
            <div
              className="h-full rounded-full"
              style={{ width: `${q.usedPct}%`, backgroundColor: 'var(--domain-core, #AC5F0C)' }}
            />
          </div>
        </div>
      ))}
    </div>

    <div
      className="flex flex-col gap-2 rounded-lg border p-3 text-xs"
      style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
    >
      <div className="flex items-center justify-between font-semibold">
        <span>双仓历史回灌镜像</span>
        <span className="font-mono text-[10px]">{AI_ARCHIVE_SNAPSHOT.branchStatus}</span>
      </div>
      <div className="font-mono text-[10px]" style={{ color: 'var(--text-muted, #66625D)' }}>
        <div>{AI_ARCHIVE_SNAPSHOT.obsidianRepo}</div>
        <div>{AI_ARCHIVE_SNAPSHOT.musicRepo}</div>
        <div>
          自 {AI_ARCHIVE_SNAPSHOT.sinceDate} · 同步于 {archiveSyncTime}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-1 font-mono text-[10px]">
        <span>Music: {AI_ARCHIVE_SNAPSHOT.counts.musicDaily}</span>
        <span>YouTube: {AI_ARCHIVE_SNAPSHOT.counts.youtubeDaily}</span>
        <span>Journal: {AI_ARCHIVE_SNAPSHOT.counts.journalDaily}</span>
        <span>Vocab: {AI_ARCHIVE_SNAPSHOT.counts.englishVocab}</span>
      </div>
      <FocusButton
        focusId="ai:action:rehydrate"
        activeFocusId={activeFocusId}
        onFocus={onFocus}
        onActivate={onRehydrate}
        surface="surface"
        className="px-2.5 py-1 text-[11px] font-medium"
      >
        增量快进同步双仓 (ff-only)
      </FocusButton>
    </div>
  </div>
);

export default AiRouteSidebar;
