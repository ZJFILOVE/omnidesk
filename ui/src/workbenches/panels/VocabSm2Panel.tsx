import React from 'react';
import { FocusButton, HAIRLINE_BORDER, SectionLabel } from '../../components/WorkbenchLayout';
import type { VocabCard } from '../../data/mock';

export type Sm2Grade = 'again' | 'hard' | 'good' | 'easy';

export interface VocabSm2PanelProps {
  vocabCards: VocabCard[];
  activeVocab: VocabCard;
  vocabRevealed: boolean;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onSelectCard: (id: string) => void;
  onToggleReveal: () => void;
  onRateSm2: (grade: Sm2Grade) => void;
  onClipJump: (card: VocabCard) => void;
}

const SM2_BUTTONS: { grade: Sm2Grade; label: string }[] = [
  { grade: 'again', label: '生疏' },
  { grade: 'hard', label: '困难' },
  { grade: 'good', label: '记得' },
  { grade: 'easy', label: '熟练' },
];

export const VocabSm2Panel: React.FC<VocabSm2PanelProps> = ({
  vocabCards,
  activeVocab,
  vocabRevealed,
  activeFocusId,
  onFocus,
  onSelectCard,
  onToggleReveal,
  onRateSm2,
  onClipJump,
}) => (
  <div className="flex flex-col gap-4 p-4">
    <div>
      <SectionLabel className="mb-2">SM-2 生词复习队列 ({vocabCards.length})</SectionLabel>
      <div className="flex flex-wrap gap-1.5">
        {vocabCards.map((card) => (
          <FocusButton
            key={card.id}
            focusId={`book:vocab:card:${card.id}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onSelectCard(card.id)}
            selected={activeVocab.id === card.id}
            tone="book"
            className="px-2 py-1 font-mono text-[11px]"
          >
            {card.word}
          </FocusButton>
        ))}
      </div>
    </div>

    <div
      className="flex flex-col gap-2.5 rounded-lg border p-3.5"
      style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-lg font-bold tracking-tight">{activeVocab.word}</div>
          <div className="font-mono text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
            {activeVocab.phonetic} · {activeVocab.pos}
          </div>
        </div>
        <span
          className="rounded px-2 py-0.5 font-mono text-[10px]"
          style={{
            backgroundColor: 'var(--status-ok-bg, #E7F5ED)',
            color: 'var(--status-ok-fg, #1E7244)',
          }}
        >
          {activeVocab.dueLabel}
        </span>
      </div>

      <div className="text-xs italic" style={{ color: 'var(--text-muted, #66625D)' }}>
        “{activeVocab.contextSentence}”
      </div>

      <FocusButton
        focusId="book:vocab:reveal"
        activeFocusId={activeFocusId}
        onFocus={onFocus}
        onActivate={onToggleReveal}
        surface="soft"
        className="px-2.5 py-1 text-xs font-medium"
      >
        {vocabRevealed ? '隐藏释义与词源' : '显示释义与词源'}
      </FocusButton>

      {vocabRevealed ? (
        <div
          className="flex flex-col gap-1.5 border-t pt-2.5 text-xs"
          style={{ borderColor: HAIRLINE_BORDER }}
        >
          <div className="font-semibold">{activeVocab.meaningZh}</div>
          <div style={{ color: 'var(--text-muted, #66625D)' }}>
            词源：{activeVocab.etymology}
          </div>
          <div className="font-mono text-[10px]" style={{ color: 'var(--text-subtle, #8A847D)' }}>
            来源：{activeVocab.bookTitle} · EF {activeVocab.easeFactor} · 间隔{' '}
            {activeVocab.intervalDays}d
          </div>
        </div>
      ) : null}

      <div className="grid grid-cols-4 gap-1.5 pt-1">
        {SM2_BUTTONS.map((btn) => (
          <FocusButton
            key={btn.grade}
            focusId={`book:sm2:${btn.grade}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onRateSm2(btn.grade)}
            surface="surface"
            className="py-1.5 text-center text-xs font-medium"
          >
            {btn.label}
          </FocusButton>
        ))}
      </div>
    </div>

    {activeVocab.nasClip ? (
      <div
        className="flex flex-col gap-2 rounded-lg border p-3 text-xs"
        style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
      >
        <div className="flex items-center justify-between font-semibold">
          <span>飞牛 NAS 原声台词切片</span>
          <span className="font-mono text-[10px]">
            {activeVocab.nasClip.showTitle} · {activeVocab.nasClip.episode}
          </span>
        </div>
        <div>“{activeVocab.nasClip.lineEn}”</div>
        <div style={{ color: 'var(--text-muted, #66625D)' }}>
          {activeVocab.nasClip.lineZh}
        </div>
        <FocusButton
          focusId="book:vocab:clip-jump"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() => onClipJump(activeVocab)}
          selected
          tone="book"
          className="px-2.5 py-1.5 font-mono text-[11px] font-medium"
        >
          跳播原片 @ {activeVocab.nasClip.timestampLabel}
        </FocusButton>
      </div>
    ) : null}
  </div>
);

export default VocabSm2Panel;
