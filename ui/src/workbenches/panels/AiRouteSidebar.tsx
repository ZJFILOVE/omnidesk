import React from 'react';
import { RefreshCw } from 'lucide-react';
import { FocusButton, HAIRLINE_BORDER } from '../../components/WorkbenchLayout';
import { PanelSectionHeading } from '../../components/WorkbenchPrimitives';
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
  <div className="flex flex-col gap-5 p-4.5">
    <div className="flex flex-col gap-2.5">
      <PanelSectionHeading title="Magpie 模型路由" meta={route.activeGroup} />
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
              surface={selected ? 'canvas' : 'ghost'}
              className="omni-focus-row px-3 py-2 text-left font-mono text-[12px]"
            >
              {selected ? '● ' : '○ '}
              {model}
            </FocusButton>
          );
        })}
      </div>

      <div className="mt-1">
        <div className="mb-1.5 text-[12px] font-medium text-[var(--text-muted)]">
          推理强度 (Effort)
        </div>
        <div className="grid grid-cols-4 gap-1.5">
          {EFFORT_OPTIONS.map((effort) => (
            <FocusButton
              key={effort}
              focusId={`ai:effort:${effort}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSelectEffort(effort)}
              selected={route.effort === effort}
              tone="core"
              surface="canvas"
              className="py-1.5 font-mono text-[12px]"
            >
              {effort}
            </FocusButton>
          ))}
        </div>
      </div>

      <div className="mt-1">
        <div className="mb-1.5 text-[12px] font-medium text-[var(--text-muted)]">
          调度策略 (Strategy)
        </div>
        <div className="grid grid-cols-4 gap-1.5">
          {STRATEGY_OPTIONS.map((strategy) => (
            <FocusButton
              key={strategy}
              focusId={`ai:strategy:${strategy}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onSelectStrategy(strategy)}
              selected={route.strategy === strategy}
              tone="core"
              surface="canvas"
              className="py-1.5 font-mono text-[12px]"
            >
              {strategy}
            </FocusButton>
          ))}
        </div>
      </div>
    </div>

    <div className="flex flex-col gap-2.5 border-t pt-4" style={{ borderColor: HAIRLINE_BORDER }}>
      <PanelSectionHeading title="实时订阅额度" />
      {route.quotas.map((q) => (
        <div key={q.model} className="text-[12px]">
          <div className="flex justify-between font-mono text-[12px] text-[var(--text-secondary)]">
            <span>
              {q.provider} · {q.model}
            </span>
            <span>
              {q.usedPct}% · {q.resetIn}
            </span>
          </div>
          <div
            className="mt-1.5 h-1.5 overflow-hidden rounded-full"
            style={{ backgroundColor: 'var(--bg-soft)' }}
          >
            <div
              className="h-full rounded-full"
              style={{ width: `${q.usedPct}%`, backgroundColor: 'var(--domain-core)' }}
            />
          </div>
        </div>
      ))}
    </div>

    <div
      className="flex flex-col gap-2.5 rounded-[14px] p-4 text-[12.5px]"
      style={{ backgroundColor: 'var(--bg-canvas)' }}
    >
      <div className="flex items-center justify-between font-semibold text-[var(--text-primary)]">
        <span>双仓历史回灌镜像</span>
        <span className="font-mono text-[12px] text-[var(--text-muted)]">
          {AI_ARCHIVE_SNAPSHOT.branchStatus}
        </span>
      </div>
      <div className="space-y-0.5 font-mono text-[12px] text-[var(--text-muted)]">
        <div>{AI_ARCHIVE_SNAPSHOT.obsidianRepo}</div>
        <div>{AI_ARCHIVE_SNAPSHOT.musicRepo}</div>
        <div>
          自 {AI_ARCHIVE_SNAPSHOT.sinceDate} · 同步于 {archiveSyncTime}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-1 font-mono text-[12px] text-[var(--text-secondary)]">
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
        surface="soft"
        className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-[12.5px] font-medium"
      >
        <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
        <span>增量快进同步双仓 (ff-only)</span>
      </FocusButton>
    </div>
  </div>
);

export default AiRouteSidebar;
