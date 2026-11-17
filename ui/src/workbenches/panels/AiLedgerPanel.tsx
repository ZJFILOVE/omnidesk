import React from 'react';
import StateView from '../../components/StateView';
import { FocusButton, HAIRLINE_BORDER } from '../../components/WorkbenchLayout';
import { EditorialCallout, PanelSectionHeading } from '../../components/WorkbenchPrimitives';
import type { AiStructuredCard, CurationMode } from '../../data/mock';

export interface AiLedgerPanelProps {
  curationMode: CurationMode;
  pinnedCards: AiStructuredCard[];
  auditLogs: string[];
  activeFocusId: string;
  onFocus: (id: string) => void;
  onTogglePinCard: (card: AiStructuredCard) => void;
  onClearPinned: () => void;
}

export const AiLedgerPanel: React.FC<AiLedgerPanelProps> = ({
  curationMode,
  pinnedCards,
  auditLogs,
  activeFocusId,
  onFocus,
  onTogglePinCard,
  onClearPinned,
}) => (
  <div className="flex flex-col gap-5 p-4.5">
    <EditorialCallout domain="core" title="双模反写防火墙">
      <div className="flex flex-col gap-1 font-mono text-[13.5px] text-[var(--text-muted)]">
        <div>模式：{curationMode}</div>
        <div>
          历史账本：
          {curationMode === 'daily_immutable' ? '读写 + 事务保护' : '只读禁止写入'}
        </div>
        <div>
          歌单目标：
          {curationMode === 'daily_immutable'
            ? 'p.YJXV7dEIeb3GzZX'
            : 'OmniDesk AI 灵感电台 (硬隔离)'}
        </div>
      </div>
    </EditorialCallout>

    <div className="flex flex-col gap-2.5">
      <PanelSectionHeading
        title="待反写工作台队列"
        meta={`${pinnedCards.length} 项`}
        action={
          pinnedCards.length > 0 ? (
            <FocusButton
              focusId="ai:action:clear-pinned"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onClearPinned}
              surface="soft"
              className="px-2.5 py-1 text-[13.5px]"
            >
              清空
            </FocusButton>
          ) : undefined
        }
      />

      {pinnedCards.length === 0 ? (
        <StateView
          mode="empty"
          variant="inline"
          title="暂存队列为空"
          description="点击对话中的推荐卡片可暂存到此处。"
        />
      ) : (
        pinnedCards.map((card) => (
          <FocusButton
            key={card.id}
            focusId={`ai:pinned:${card.id}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onTogglePinCard(card)}
            surface="canvas"
            className="p-3.5 text-left text-[14.5px]"
          >
            <div className="flex items-center justify-between font-mono text-[13.5px]">
              <span style={{ color: 'var(--domain-core)' }}>{card.domain}</span>
              <span className="text-[var(--text-muted)]">移除 ×</span>
            </div>
            <div className="mt-1 font-medium text-[var(--text-primary)]">{card.title}</div>
          </FocusButton>
        ))
      )}
    </div>

    <div className="flex flex-col gap-2 border-t pt-4" style={{ borderColor: HAIRLINE_BORDER }}>
      <PanelSectionHeading title="Capability 调度轨迹" />
      {auditLogs.map((log, idx) => (
        <div
          key={`log-${idx}`}
          className="rounded-[10px] px-3 py-2 font-mono text-[13.5px] leading-[1.6]"
          style={{
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-muted)',
          }}
        >
          {log}
        </div>
      ))}
    </div>
  </div>
);

export default AiLedgerPanel;
