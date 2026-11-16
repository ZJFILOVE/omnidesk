import React from 'react';
import {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableInputProps,
  focusableProps,
  isImeComposing,
} from '../../components/WorkbenchLayout';
import {
  AI_QUICK_PRESETS,
  type AiChatMessage,
  type AiPresetItem,
  type AiStructuredCard,
  type CurationMode,
} from '../../data/mock';

export type RepeatPolicy = 'allow_any' | 'prefer_familiar' | 'session_dedup_only';

const REPEAT_LABELS: Record<RepeatPolicy, string> = {
  allow_any: 'allow_any · 全网混排',
  prefer_familiar: 'prefer_familiar · 优先重温',
  session_dedup_only: 'session_dedup_only · 本轮去重',
};

export interface AiChatPaneProps {
  curationMode: CurationMode;
  repeatPolicy: RepeatPolicy;
  messages: AiChatMessage[];
  pinnedCards: AiStructuredCard[];
  composerText: string;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onModeChange: (mode: CurationMode) => void;
  onRepeatPolicyChange: (policy: RepeatPolicy) => void;
  onComposerChange: (val: string) => void;
  onSend: () => void;
  onRunPreset: (preset: AiPresetItem) => void;
  onTogglePinCard: (card: AiStructuredCard) => void;
}

export const AiChatPane: React.FC<AiChatPaneProps> = ({
  curationMode,
  repeatPolicy,
  messages,
  pinnedCards,
  composerText,
  activeFocusId,
  onFocus,
  onModeChange,
  onRepeatPolicyChange,
  onComposerChange,
  onSend,
  onRunPreset,
  onTogglePinCard,
}) => (
  <div className="flex h-full min-h-0 flex-col justify-between gap-3 p-5">
    <div className="flex flex-col gap-2.5">
      <div
        className="flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3"
        style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <FocusButton
            focusId="ai:mode:interactive"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onModeChange('interactive_ephemeral')}
            selected={curationMode === 'interactive_ephemeral'}
            tone="core"
            surface="soft"
            className="px-2.5 py-1 text-xs font-semibold"
          >
            interactive_ephemeral · 可重复灵感
          </FocusButton>
          <FocusButton
            focusId="ai:mode:daily"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onModeChange('daily_immutable')}
            selected={curationMode === 'daily_immutable'}
            tone="ok"
            surface="soft"
            className="px-2.5 py-1 text-xs font-semibold"
          >
            daily_immutable · 每日不重复账本
          </FocusButton>
        </div>
        <span className="font-mono text-[10px]" style={{ color: 'var(--text-muted, #66625D)' }}>
          {curationMode === 'daily_immutable'
            ? '永久账本去重 + p.YJXV7dEIeb3GzZX 同步'
            : 'record_permanent_history=false · 零仓库污染'}
        </span>
      </div>

      {curationMode === 'interactive_ephemeral' ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {(['allow_any', 'prefer_familiar', 'session_dedup_only'] as const).map((pol) => (
            <FocusButton
              key={pol}
              focusId={`ai:repeat:${pol}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onRepeatPolicyChange(pol)}
              selected={repeatPolicy === pol}
              tone="core"
              surface="surface"
              className="px-2 py-0.5 font-mono text-[10px]"
            >
              {REPEAT_LABELS[pol]}
            </FocusButton>
          ))}
        </div>
      ) : null}
    </div>

    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1">
      {messages.map((msg) => {
        const msgFocusId = `ai:msg:${msg.id}`;
        const isUser = msg.role === 'user';
        return (
          <div key={msg.id} className="flex flex-col gap-2">
            <div
              data-focused={activeFocusId === msgFocusId ? 'true' : undefined}
              {...focusableProps(msgFocusId, activeFocusId, onFocus)}
              className={focusClass(
                activeFocusId === msgFocusId,
                `rounded-lg border p-3.5 ${isUser ? 'ml-8' : 'mr-4'}`,
              )}
              style={{
                backgroundColor: isUser ? 'var(--bg-soft, #F1EFEC)' : 'var(--bg-surface, #FCFBF9)',
                borderColor: HAIRLINE_BORDER,
              }}
            >
              <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 font-mono text-[10px]">
                <span
                  className="font-semibold uppercase"
                  style={{
                    color: isUser ? 'var(--text-primary, #222120)' : 'var(--domain-core, #AC5F0C)',
                  }}
                >
                  {isUser ? 'User Prompt' : msg.servedModel ?? 'OmniDesk Agent'}
                </span>
                <div className="flex items-center gap-1.5">
                  {msg.mode ? (
                    <span
                      className="rounded px-1.5 py-0.5 text-[9px]"
                      style={{
                        backgroundColor:
                          msg.mode === 'daily_immutable'
                            ? 'var(--status-ok-bg, #E7F5ED)'
                            : 'var(--status-warn-bg, #FCF2DF)',
                        color:
                          msg.mode === 'daily_immutable'
                            ? 'var(--status-ok-fg, #1E7244)'
                            : 'var(--status-warn-fg, #AC5F0C)',
                      }}
                    >
                      {msg.mode}
                    </span>
                  ) : null}
                  <span style={{ color: 'var(--text-subtle, #8A847D)' }}>{msg.timestamp}</span>
                </div>
              </div>
              <p className="text-xs leading-relaxed">{msg.content}</p>
            </div>

            {msg.cards?.length ? (
              <div className="mr-4 grid grid-cols-1 gap-2 md:grid-cols-2">
                {msg.cards.map((card) => {
                  const isPinned = pinnedCards.some((c) => c.id === card.id);
                  return (
                    <FocusButton
                      key={card.id}
                      focusId={`ai:card:${card.id}`}
                      activeFocusId={activeFocusId}
                      onFocus={onFocus}
                      onActivate={() => onTogglePinCard(card)}
                      selected={isPinned}
                      tone="core"
                      surface="surface"
                      className="flex flex-col gap-1 p-3 text-left"
                    >
                      <div className="flex items-center justify-between gap-2 font-mono text-[10px]">
                        <span style={{ color: 'var(--domain-core, #AC5F0C)' }}>
                          {card.domain.toUpperCase()} · {card.badge}
                        </span>
                        <span>{isPinned ? '已暂存 ✓' : '+ 反写暂存'}</span>
                      </div>
                      <div className="text-xs font-semibold">{card.title}</div>
                      <div className="text-[11px]" style={{ color: 'var(--text-muted, #66625D)' }}>
                        {card.subtitle}
                      </div>
                    </FocusButton>
                  );
                })}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>

    <div
      className="flex flex-col gap-2 rounded-lg border p-3"
      style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
    >
      <div className="flex flex-wrap gap-1.5">
        {AI_QUICK_PRESETS.map((preset) => (
          <FocusButton
            key={preset.id}
            focusId={`ai:preset:${preset.id}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onRunPreset(preset)}
            surface="soft"
            className="px-2 py-1 text-[11px] font-medium"
          >
            {preset.label}
          </FocusButton>
        ))}
      </div>

      <div className="flex items-end gap-2">
        <textarea
          rows={2}
          value={composerText}
          onChange={(e) => onComposerChange(e.target.value)}
          onKeyDown={(e) => {
            if (isImeComposing(e)) return;
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
          placeholder="输入策展指令或问题（支持重温历史佳作；Enter 发送，Shift+Enter 换行）..."
          aria-label="AI 策展对话输入框"
          data-focused={activeFocusId === 'ai:composer:input' ? 'true' : undefined}
          {...focusableInputProps('ai:composer:input', activeFocusId, onFocus)}
          className={focusClass(
            activeFocusId === 'ai:composer:input',
            'min-w-0 flex-1 rounded-md border p-2 text-xs',
          )}
          style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
        />
        <FocusButton
          focusId="ai:composer:send"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onSend}
          selected
          tone="core"
          className="shrink-0 px-3.5 py-2 text-xs font-semibold"
        >
          发送执行
        </FocusButton>
      </div>
    </div>
  </div>
);

export default AiChatPane;
