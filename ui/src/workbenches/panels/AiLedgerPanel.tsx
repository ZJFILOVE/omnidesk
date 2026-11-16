import React from 'react';
import { FocusButton, HAIRLINE_BORDER, SectionLabel } from '../../components/WorkbenchLayout';
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
  <div className="flex flex-col gap-4 p-4">
    <div
      className="rounded-lg border p-3 text-xs"
      style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
    >
      <div className="mb-1.5 font-semibold">双模反写防火墙状态</div>
      <div className="flex flex-col gap-1 font-mono text-[10px]">
        <div>模式：{curationMode}</div>
        <div>
          永久历史账本：
          {curationMode === 'daily_immutable' ? '读写+事务保护' : '只读禁止写入'}
        </div>
        <div>
          歌单目标：
          {curationMode === 'daily_immutable'
            ? 'p.YJXV7dEIeb3GzZX (门禁校验后)'
            : 'OmniDesk AI 灵感电台 (硬隔离)'}
        </div>
      </div>
    </div>

    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <SectionLabel>待反写工作台队列 ({pinnedCards.length})</SectionLabel>
        <FocusButton
          focusId="ai:action:clear-pinned"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onClearPinned}
          surface="surface"
          className="px-2 py-0.5 font-mono text-[10px]"
        >
          清空
        </FocusButton>
      </div>

      {pinnedCards.map((card) => (
        <FocusButton
          key={card.id}
          focusId={`ai:pinned:${card.id}`}
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() => onTogglePinCard(card)}
          className="p-2.5 text-left text-xs"
        >
          <div className="flex items-center justify-between font-mono text-[10px]">
            <span style={{ color: 'var(--domain-core, #AC5F0C)' }}>{card.domain}</span>
            <span>移除 ×</span>
          </div>
          <div className="mt-0.5 font-medium">{card.title}</div>
        </FocusButton>
      ))}
    </div>

    <div className="flex flex-col gap-1.5 border-t pt-3" style={{ borderColor: HAIRLINE_BORDER }}>
      <SectionLabel>Capability 调度轨迹</SectionLabel>
      {auditLogs.map((log, idx) => (
        <div
          key={`log-${idx}`}
          className="rounded border px-2 py-1.5 font-mono text-[10px] leading-snug"
          style={{
            backgroundColor: 'var(--bg-canvas, #F6F5F3)',
            borderColor: HAIRLINE_BORDER,
            color: 'var(--text-muted, #66625D)',
          }}
        >
          {log}
        </div>
      ))}
    </div>
  </div>
);

export default AiLedgerPanel;
