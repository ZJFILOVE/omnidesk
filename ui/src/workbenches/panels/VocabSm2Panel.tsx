import React from 'react';
import { FocusButton, HAIRLINE_BORDER } from '../../components/WorkbenchLayout';
import { PanelSectionHeading, TagChip } from '../../components/WorkbenchPrimitives';
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
  <div className="flex flex-col gap-5 p-4.5">
    <div>
      <PanelSectionHeading title={`SM-2 生词复习队列 (${vocabCards.length})`} className="mb-2.5" />
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
            surface="canvas"
            className="px-2.5 py-1 font-mono text-[13.5px]"
          >
            {card.word}
          </FocusButton>
        ))}
      </div>
    </div>

    <div
      className="flex flex-col gap-3 rounded-[14px] p-4"
      style={{ backgroundColor: 'var(--bg-canvas)', boxShadow: 'var(--shadow-page)' }}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[22px] font-bold tracking-tight text-[var(--text-primary)]">
            {activeVocab.word}
          </div>
          <div className="mt-0.5 font-mono text-[13.5px] text-[var(--text-muted)]">
            {activeVocab.phonetic} · {activeVocab.pos}
          </div>
        </div>
        <TagChip tone="ok">{activeVocab.dueLabel}</TagChip>
      </div>

      <p className="text-[15px] leading-[1.7] italic text-[var(--text-secondary)]">
        “{activeVocab.contextSentence}”
      </p>

      <FocusButton
        focusId="book:vocab:reveal"
        activeFocusId={activeFocusId}
        onFocus={onFocus}
        onActivate={onToggleReveal}
        surface="soft"
        className="px-3 py-1.5 text-[14px] font-medium"
      >
        {vocabRevealed ? '隐藏释义与词源' : '显示释义与词源'}
      </FocusButton>

      {vocabRevealed ? (
        <div
          className="flex flex-col gap-2 border-t pt-3 text-[15px] leading-[1.7]"
          style={{ borderColor: HAIRLINE_BORDER }}
        >
          <div className="font-semibold text-[var(--text-primary)]">{activeVocab.meaningZh}</div>
          <div className="text-[var(--text-secondary)]">词源：{activeVocab.etymology}</div>
          <div className="font-mono text-[13.5px] text-[var(--text-subtle)]">
            来源：{activeVocab.bookTitle} · EF {activeVocab.easeFactor} · 间隔 {activeVocab.intervalDays}d
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
            className="py-2 text-center text-[14px] font-medium"
          >
            {btn.label}
          </FocusButton>
        ))}
      </div>
    </div>

    {activeVocab.nasClip ? (
      <div
        className="flex flex-col gap-2.5 rounded-[14px] p-4 text-[15px] leading-[1.7]"
        style={{ backgroundColor: 'var(--bg-sunken)' }}
      >
        <div className="flex items-center justify-between font-semibold text-[var(--text-primary)]">
          <span>飞牛 NAS 原声切片</span>
          <span className="font-mono text-[13.5px] text-[var(--text-muted)]">
            {activeVocab.nasClip.showTitle} · {activeVocab.nasClip.episode}
          </span>
        </div>
        <div className="text-[var(--text-primary)]">“{activeVocab.nasClip.lineEn}”</div>
        <div className="text-[14px] text-[var(--text-muted)]">{activeVocab.nasClip.lineZh}</div>
        <FocusButton
          focusId="book:vocab:clip-jump"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={() => onClipJump(activeVocab)}
          selected
          tone="book"
          className="px-3 py-2 font-mono text-[13.5px] font-medium"
        >
          跳播原片 @ {activeVocab.nasClip.timestampLabel}
        </FocusButton>
      </div>
    ) : null}
  </div>
);

export default VocabSm2Panel;
