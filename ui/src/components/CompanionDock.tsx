import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Bot, PanelBottom, PanelRight, Send, Sparkles, X } from 'lucide-react';
import { INITIAL_AI_MESSAGES, INITIAL_MAGPIE_ROUTE } from '../data/mock';
import { useImeGuard } from '../hooks/useImeGuard';
import {
  DOCK_ASPECT_THRESHOLD,
  DOMAIN_CSS_VARS,
  DOMAIN_LABELS,
  WORKBENCH_MAP,
} from '../theme';
import type {
  ActiveEntityContext,
  ContentDomain,
  DockLayout,
  WorkbenchId,
} from '../types';

export interface CompanionMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  action?: 'chat' | 'summarize' | 'translate' | 'etymology' | 'clause_parse';
  servedModel?: string;
}

export interface CompanionDockProps {
  open: boolean;
  onClose: () => void;
  layout?: DockLayout;
  activeWorkbench?: WorkbenchId;
  domain?: ContentDomain;
  contextEntity?: ActiveEntityContext | null;
  pendingPrompt?: string | null;
  onConsumePrompt?: () => void;
  onOpenAiWorkbench?: () => void;
  onOpenPluginStore?: () => void;
}

interface PresetAction {
  id: 'summarize' | 'translate' | 'etymology' | 'clause_parse';
  label: string;
  promptPrefix: string;
}

const PRESET_ACTIONS: readonly PresetAction[] = [
  { id: 'summarize', label: '总结要点', promptPrefix: '总结当前内容核心要点' },
  { id: 'translate', label: '双语精译', promptPrefix: '给出自然流畅的双语对照译文' },
  { id: 'etymology', label: '词源拆解', promptPrefix: '拆解关键术语词源与语境用法' },
  { id: 'clause_parse', label: '长难句解析', promptPrefix: '拆分句法结构并标注从句层次' },
];

export function resolveDockLayout(width?: number, height?: number): DockLayout {
  const w = width ?? (typeof window !== 'undefined' ? window.innerWidth : 1280);
  const h = height ?? (typeof window !== 'undefined' ? window.innerHeight : 800);
  if (h <= 0) {
    return 'side';
  }
  return w / h >= DOCK_ASPECT_THRESHOLD ? 'side' : 'bottom';
}

function nowTimeLabel(): string {
  const d = new Date();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

function buildCompanionReply(
  prompt: string,
  action: CompanionMessage['action'],
  domain: ContentDomain,
  contextTitle: string,
): string {
  const scopeLabel = `${DOMAIN_LABELS[domain]} · ${contextTitle}`;
  switch (action) {
    case 'summarize':
      return `[${scopeLabel}] 核心提要：已提取当前焦点段落的关键论证与边界条件，可在不遮挡主舞台的前提下对照原文继续阅读。`;
    case 'translate':
      return `[${scopeLabel}] 双语精译：已保留专有名词与数学/代码符号，并将长从句切分为符合中文学术阅读习惯的并列短句。`;
    case 'etymology':
      return `[${scopeLabel}] 词源拆解：已按词根 + 词缀拆解高频术语并关联本地生词本（SM-2 复习队列与 NAS 台词索引）。`;
    case 'clause_parse':
      return `[${scopeLabel}] 句法树：主句主干已标出，定语从句与分词状语已按意群分层，便于逐层精读。`;
    default:
      return `[${scopeLabel}] 已收到「${prompt}」。当前按 interactive_ephemeral 伴读会话处理，不影响每日精选历史账本。`;
  }
}

export function CompanionDock({
  open,
  onClose,
  layout,
  activeWorkbench = 'book',
  domain,
  contextEntity,
  pendingPrompt,
  onConsumePrompt,
  onOpenAiWorkbench,
  onOpenPluginStore,
}: CompanionDockProps) {
  const [responsiveLayout, setResponsiveLayout] = useState<DockLayout>(() => resolveDockLayout());
  const [manualLayout, setManualLayout] = useState<DockLayout | null>(null);
  const [draft, setDraft] = useState('');
  const [messages, setMessages] = useState<CompanionMessage[]>(() => [
    {
      id: 'dock-welcome',
      role: 'assistant',
      timestamp: INITIAL_AI_MESSAGES[1]?.timestamp ?? '22:40',
      servedModel: `${INITIAL_MAGPIE_ROUTE.activeGroup} → ${INITIAL_MAGPIE_ROUTE.primaryModel}:${INITIAL_MAGPIE_ROUTE.effort}`,
      content:
        'F8 无遮罩伴读栏已就绪。支持总结要点、双语精译、词源拆解与长难句解析；输入法选词期间自动屏蔽 Enter 提交。',
    },
  ]);

  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const msgCounterRef = useRef(1);
  const ime = useImeGuard();

  const workbenchMeta = WORKBENCH_MAP[activeWorkbench];
  const effectiveDomain: ContentDomain = domain ?? contextEntity?.domain ?? workbenchMeta.domain;
  const effectiveLayout: DockLayout = layout ?? manualLayout ?? responsiveLayout;
  const contextTitle = contextEntity?.title ?? `${workbenchMeta.label} (${workbenchMeta.shortLabel})`;
  const routeSummary = `${INITIAL_MAGPIE_ROUTE.activeGroup} → ${INITIAL_MAGPIE_ROUTE.primaryModel}:${INITIAL_MAGPIE_ROUTE.effort}`;

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined;
    }
    const handleResize = () => {
      setResponsiveLayout(resolveDockLayout(window.innerWidth, window.innerHeight));
    };
    handleResize();
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, []);

  const submitPrompt = useCallback(
    (rawText: string, action: CompanionMessage['action'] = 'chat') => {
      const trimmed = rawText.trim();
      if (!trimmed) {
        return;
      }
      const nextId = msgCounterRef.current++;
      const ts = nowTimeLabel();
      const userMsg: CompanionMessage = {
        id: `dock-user-${nextId}`,
        role: 'user',
        content: trimmed,
        timestamp: ts,
        action,
      };
      const assistantMsg: CompanionMessage = {
        id: `dock-assistant-${nextId}`,
        role: 'assistant',
        content: buildCompanionReply(trimmed, action, effectiveDomain, contextTitle),
        timestamp: ts,
        action,
        servedModel: routeSummary,
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setDraft('');
    },
    [effectiveDomain, contextTitle, routeSummary],
  );

  useEffect(() => {
    if (!open || !pendingPrompt) {
      return;
    }
    submitPrompt(pendingPrompt, 'chat');
    onConsumePrompt?.();
  }, [open, pendingPrompt, onConsumePrompt, submitPrompt]);

  if (!open) {
    return null;
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (ime.shouldIgnoreShortcut(event)) {
      return;
    }

    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }

    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submitPrompt(draft, 'chat');
    }
  };

  const dockPositionClass =
    effectiveLayout === 'side'
      ? 'top-0 right-0 bottom-0 w-full max-w-[380px] border-l'
      : 'right-0 bottom-0 left-0 h-[40vh] min-h-[260px] max-h-[420px] border-t';

  return (
    <aside
      role="complementary"
      aria-label="F8 AI 伴读栏"
      data-testid="companion-dock"
      data-dock-layout={effectiveLayout}
      className={[
        'omni-theme-surface fixed z-30 flex flex-col border-[var(--border-line)] bg-[var(--bg-surface)] text-[var(--text-primary)] shadow-lg',
        dockPositionClass,
      ].join(' ')}
    >
      <header className="flex items-center justify-between gap-2 border-b border-[var(--border-line)] bg-[var(--bg-canvas)] px-3.5 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <span
            aria-hidden="true"
            className="h-2.5 w-2.5 shrink-0 rounded-full"
            style={{ backgroundColor: DOMAIN_CSS_VARS[effectiveDomain] }}
          />
          <Bot className="h-4 w-4 shrink-0 text-[var(--text-primary)]" aria-hidden="true" />
          <span className="truncate text-sm font-semibold">F8 伴读栏</span>
          <span className="rounded border border-[var(--border-line)] bg-[var(--bg-soft)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--text-muted)]">
            {DOMAIN_LABELS[effectiveDomain]}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {!layout && (
            <button
              type="button"
              onClick={() =>
                setManualLayout((prev) =>
                  (prev ?? responsiveLayout) === 'side' ? 'bottom' : 'side',
                )
              }
              aria-label={effectiveLayout === 'side' ? '切换为底部分栏' : '切换为右侧分栏'}
              title={effectiveLayout === 'side' ? '切换为底部分栏' : '切换为右侧分栏'}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-[var(--text-muted)] hover:border-[var(--border-line)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
            >
              {effectiveLayout === 'side' ? (
                <PanelBottom className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <PanelRight className="h-3.5 w-3.5" aria-hidden="true" />
              )}
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭伴读栏"
            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-[var(--text-muted)] hover:border-[var(--border-line)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-line)] bg-[var(--bg-soft)] px-3.5 py-2 text-[11px]">
        <div className="flex min-w-0 items-center gap-1.5 text-[var(--text-muted)]">
          <Sparkles className="h-3.5 w-3.5 shrink-0 text-[var(--domain-core)]" aria-hidden="true" />
          <span className="truncate" title={contextTitle}>
            上下文：{contextTitle}
          </span>
        </div>
        <span
          className="inline-flex items-center gap-1 rounded border border-[var(--border-line)] bg-[var(--status-ok-bg)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--status-ok-fg)]"
          title={routeSummary}
        >
          {INITIAL_MAGPIE_ROUTE.primaryModel}:{INITIAL_MAGPIE_ROUTE.effort}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5 border-b border-[var(--border-line)] px-3.5 py-2">
        {PRESET_ACTIONS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            onClick={() => submitPrompt(`${preset.promptPrefix}：${contextTitle}`, preset.id)}
            className="rounded-md border border-[var(--border-line)] bg-[var(--bg-canvas)] px-2 py-1 text-xs text-[var(--text-primary)] hover:border-[var(--border-strong)] hover:bg-[var(--hover-fill)]"
          >
            {preset.label}
          </button>
        ))}
        {onOpenAiWorkbench && (
          <button
            type="button"
            onClick={onOpenAiWorkbench}
            className="rounded-md border border-[var(--border-line)] bg-[var(--bg-soft)] px-2 py-1 text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]"
          >
            AI 工作台
          </button>
        )}
        {onOpenPluginStore && (
          <button
            type="button"
            onClick={onOpenPluginStore}
            className="rounded-md border border-[var(--border-line)] bg-[var(--bg-soft)] px-2 py-1 text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]"
          >
            插件中心
          </button>
        )}
      </div>

      <div
        data-testid="companion-messages"
        aria-live="polite"
        className="flex-1 space-y-2.5 overflow-y-auto px-3.5 py-3 text-xs leading-relaxed"
      >
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={[
              'rounded-lg border px-3 py-2',
              msg.role === 'user'
                ? 'ml-6 border-[var(--border-line)] bg-[var(--bg-active)] text-[var(--text-primary)]'
                : 'mr-3 border-[var(--border-line)] bg-[var(--bg-canvas)] text-[var(--text-primary)]',
            ].join(' ')}
          >
            <div className="mb-1 flex items-center justify-between gap-2 text-[10px] text-[var(--text-muted)]">
              <span className="font-medium">{msg.role === 'user' ? '你' : 'OmniDesk Companion'}</span>
              <span>{msg.timestamp}</span>
            </div>
            <p className="whitespace-pre-wrap">{msg.content}</p>
            {msg.servedModel && (
              <div className="mt-1.5 font-mono text-[10px] text-[var(--text-subtle)]">
                {msg.servedModel}
              </div>
            )}
          </div>
        ))}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submitPrompt(draft, 'chat');
        }}
        className="border-t border-[var(--border-line)] bg-[var(--bg-canvas)] p-3"
      >
        <div className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] p-2 focus-within:ring-2 focus-within:ring-[var(--focus-ring)]">
          <textarea
            ref={inputRef}
            rows={2}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            {...ime.compositionProps}
            aria-label="伴读栏提问输入"
            placeholder="针对当前阅读/视频内容提问（Enter 发送，Shift+Enter 换行）..."
            className="w-full resize-none bg-transparent text-xs text-[var(--text-primary)] placeholder:text-[var(--text-subtle)] focus:outline-none"
          />
          <div className="mt-1.5 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-[10px] text-[var(--text-muted)]">
              <span>{effectiveLayout === 'side' ? '右侧分栏' : '底部分栏'}</span>
              {ime.isComposing && (
                <span className="rounded bg-[var(--status-info-bg)] px-1.5 py-0.5 text-[var(--status-info-fg)]">
                  IME 选词中
                </span>
              )}
            </div>
            <button
              type="submit"
              disabled={!draft.trim()}
              aria-label="发送伴读提问"
              className="inline-flex items-center gap-1 rounded-md border border-[var(--border-line)] bg-[var(--bg-active)] px-2.5 py-1 text-xs font-medium text-[var(--text-primary)] hover:border-[var(--border-strong)] disabled:cursor-not-allowed disabled:opacity-45"
            >
              <Send className="h-3 w-3" aria-hidden="true" />
              <span>发送</span>
            </button>
          </div>
        </div>
      </form>
    </aside>
  );
}

export default CompanionDock;
