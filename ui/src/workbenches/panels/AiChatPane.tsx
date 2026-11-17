import React from 'react';
import { Pin, Send } from 'lucide-react';
import StateView from '../../components/StateView';
import {
  FocusButton,
  focusClass,
  focusableInputProps,
  focusableProps,
  isImeComposing,
} from '../../components/WorkbenchLayout';
import { TagChip } from '../../components/WorkbenchPrimitives';
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
  isSending?: boolean;
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
  isSending = false,
  activeFocusId,
  onFocus,
  onModeChange,
  onRepeatPolicyChange,
  onComposerChange,
  onSend,
  onRunPreset,
  onTogglePinCard,
}) => (
  <div className="mx-auto flex h-full min-h-0 w-full max-w-[780px] flex-col justify-between gap-4 px-6 py-5 sm:px-8">
    {/* Mode & Repeat Policy Header */}
    <div className="flex flex-col gap-2.5">
      <div
        className="flex flex-wrap items-center justify-between gap-2.5 rounded-[14px] px-4 py-2.5"
        style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
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
            className="px-3 py-1.5 text-[14px] font-semibold"
          >
            可重复灵感 · interactive_ephemeral
          </FocusButton>
          <FocusButton
            focusId="ai:mode:daily"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onModeChange('daily_immutable')}
            selected={curationMode === 'daily_immutable'}
            tone="ok"
            surface="soft"
            className="px-3 py-1.5 text-[14px] font-semibold"
          >
            每日账本 · daily_immutable
          </FocusButton>
        </div>
        <span className="font-mono text-[13.5px] text-[var(--text-muted)]">
          {curationMode === 'daily_immutable'
            ? '永久账本去重 + 歌单同步'
            : '只读历史 · 零仓库污染'}
        </span>
      </div>

      {curationMode === 'interactive_ephemeral' ? (
        <div className="flex flex-wrap items-center gap-1.5 px-1">
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
              className="px-2.5 py-1 font-mono text-[13.5px]"
            >
              {REPEAT_LABELS[pol]}
            </FocusButton>
          ))}
        </div>
      ) : null}
    </div>

    {/* Conversation Stream */}
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1 max-h-[44vh] @[860px]/workbench:max-h-none">
      {messages.length === 0 && !isSending ? (
        <StateView
          mode="empty"
          title="开始一次策展对话"
          description="点击下方快捷指令或输入提示词，所有回复均来自真实模型路由或明确的离线示例标注。"
        />
      ) : null}

      {messages.map((msg) => {
        const msgFocusId = `ai:msg:${msg.id}`;
        const isUser = msg.role === 'user';
        return (
          <div key={msg.id} className="flex flex-col gap-3">
            <div
              data-focused={activeFocusId === msgFocusId ? 'true' : undefined}
              {...focusableProps(msgFocusId, activeFocusId, onFocus)}
              className={focusClass(
                activeFocusId === msgFocusId,
                `rounded-[16px] px-5 py-4 ${isUser ? 'ml-12' : 'mr-4'}`,
              )}
              style={{
                backgroundColor: msg.isError
                  ? 'var(--status-err-bg)'
                  : isUser
                    ? 'var(--bg-soft)'
                    : 'var(--bg-surface)',
                color: msg.isError ? 'var(--status-err-fg)' : 'var(--text-primary)',
                boxShadow: !isUser && !msg.isError ? 'var(--shadow-page)' : undefined,
              }}
            >
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[13.5px]">
                <span
                  className="font-semibold"
                  style={{
                    color: msg.isError
                      ? 'var(--status-err-fg)'
                      : isUser
                        ? 'var(--text-primary)'
                        : 'var(--domain-core)',
                  }}
                >
                  {isUser
                    ? '你'
                    : msg.isError
                      ? msg.servedModel ?? 'Backend Error'
                      : msg.isDemo
                        ? `${msg.servedModel ?? '示例预览'} · DEMO`
                        : msg.servedModel ?? 'OmniDesk Agent'}
                </span>
                <div className="flex items-center gap-2">
                  {msg.mode ? (
                    <TagChip tone={msg.mode === 'daily_immutable' ? 'ok' : 'warn'} mono>
                      {msg.mode}
                    </TagChip>
                  ) : null}
                  <span className="font-mono text-[13.5px] text-[var(--text-subtle)]">
                    {msg.timestamp}
                  </span>
                </div>
              </div>
              <p className="whitespace-pre-wrap text-[16.5px] leading-[1.76]">{msg.content}</p>
            </div>

            {msg.cards?.length ? (
              <div className="mr-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
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
                      className="flex flex-col gap-1.5 p-4 text-left"
                    >
                      <div className="flex items-center justify-between gap-2 text-[13.5px]">
                        <span className="truncate font-mono font-semibold" style={{ color: 'var(--domain-core)' }}>
                          {card.domain.toUpperCase()} · {card.badge}
                        </span>
                        <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap font-medium">
                          <Pin className="h-3 w-3 shrink-0" aria-hidden="true" />
                          <span>{isPinned ? '已暂存 ✓' : '+ 反写暂存'}</span>
                        </span>
                      </div>
                      <div className="text-[15.5px] font-semibold text-[var(--text-primary)]">
                        {card.title}
                      </div>
                      <div className="text-[14.5px] leading-[1.55] text-[var(--text-muted)]">{card.subtitle}</div>
                    </FocusButton>
                  );
                })}
              </div>
            ) : null}
          </div>
        );
      })}

      {isSending ? (
        <div className="mr-4">
          <StateView
            mode="loading"
            variant="inline"
            testId="ai-pending-status"
            title="正在等待 llm.chat.v1 响应..."
            description=""
          />
        </div>
      ) : null}
    </div>

    {/* Composer Dock */}
    <div
      className="flex flex-col gap-3 rounded-[16px] p-4"
      style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-soft)' }}
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
            className="px-3 py-1 text-[13.5px] font-medium"
          >
            {preset.label}
          </FocusButton>
        ))}
      </div>

      <div className="flex items-end gap-2.5">
        <textarea
          rows={2}
          value={composerText}
          onChange={(e) => onComposerChange(e.target.value)}
          onKeyDown={(e) => {
            if (isImeComposing(e)) return;
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              if (!isSending) onSend();
            }
          }}
          placeholder="输入策展指令或问题（支持重温历史佳作；Enter 发送，Shift+Enter 换行）..."
          aria-label="AI 策展对话输入框"
          data-focused={activeFocusId === 'ai:composer:input' ? 'true' : undefined}
          {...focusableInputProps('ai:composer:input', activeFocusId, onFocus)}
          className={focusClass(
            activeFocusId === 'ai:composer:input',
            'min-w-0 flex-1 rounded-[12px] border p-3 text-[16px] leading-[1.65]',
          )}
          style={{
            backgroundColor: 'var(--bg-canvas)',
            borderColor: 'var(--border-line)',
          }}
        />
        <FocusButton
          focusId="ai:composer:send"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onSend}
          selected
          tone="core"
          className="inline-flex shrink-0 items-center gap-1.5 px-4 py-2.5 text-[14.5px] font-semibold"
        >
          <Send className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{isSending ? '执行中...' : '发送执行'}</span>
        </FocusButton>
      </div>
    </div>
  </div>
);

export default AiChatPane;
