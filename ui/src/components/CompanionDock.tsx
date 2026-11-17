import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Bot, PanelBottom, PanelRight, Send, Sparkles, X } from 'lucide-react';
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
import { StatusBanner } from './StatusBanner';
import { PRESET_ACTIONS } from './companionHelpers';
import { useCompanionChat } from './useCompanionChat';

export type { CompanionMessage } from './companionHelpers';

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

export function resolveDockLayout(width?: number, height?: number): DockLayout {
  const w = width ?? (typeof window !== 'undefined' ? window.innerWidth : 1280);
  const h = height ?? (typeof window !== 'undefined' ? window.innerHeight : 800);
  if (h <= 0) {
    return 'side';
  }
  return w / h >= DOCK_ASPECT_THRESHOLD ? 'side' : 'bottom';
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
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const ime = useImeGuard();

  const workbenchMeta = WORKBENCH_MAP[activeWorkbench];
  const effectiveDomain: ContentDomain = domain ?? contextEntity?.domain ?? workbenchMeta.domain;
  const effectiveLayout: DockLayout = layout ?? manualLayout ?? responsiveLayout;
  const contextTitle = contextEntity?.title ?? `${workbenchMeta.label} (${workbenchMeta.shortLabel})`;

  const {
    tauriAvailable,
    draft,
    setDraft,
    isSending,
    dockError,
    liveRouteLabel,
    routeOnline,
    messages,
    submitPrompt,
  } = useCompanionChat({
    open,
    effectiveDomain,
    contextTitle,
    contextEntity,
  });

  const routeBadgeLabel = !tauriAvailable
    ? '离线预览示例'
    : liveRouteLabel ?? (routeOnline ? 'group/omnidesk-active' : 'Magpie 未就绪');

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

  useEffect(() => {
    if (!open || !pendingPrompt) {
      return;
    }
    void submitPrompt(pendingPrompt, 'chat');
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
      if (!isSending) {
        void submitPrompt(draft, 'chat');
      }
    }
  };

  const dockPositionClass =
    effectiveLayout === 'side'
      ? 'top-0 right-0 bottom-0 w-[var(--dock-side-width,clamp(310px,26vw,356px))] max-w-full border-l'
      : 'right-0 bottom-0 left-0 h-[var(--dock-bottom-height,clamp(236px,34vh,320px))] border-t';

  return (
    <aside
      role="complementary"
      aria-label="F8 AI 伴读栏"
      data-testid="companion-dock"
      data-dock-layout={effectiveLayout}
      style={{ boxShadow: 'var(--shadow-soft)' }}
      className={[
        'omni-theme-surface omni-drawer-motion fixed z-30 flex flex-col border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-primary)]',
        dockPositionClass,
      ].join(' ')}
    >
      <header className="flex items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span
            aria-hidden="true"
            className="h-2 w-2 shrink-0 rounded-full"
            style={{ backgroundColor: DOMAIN_CSS_VARS[effectiveDomain] }}
          />
          <Bot className="h-4 w-4 shrink-0 text-[var(--text-primary)]" aria-hidden="true" />
          <span className="truncate text-[14px] font-semibold">F8 伴读栏</span>
          <span className="rounded-full bg-[var(--bg-soft)] px-2.5 py-0.5 text-[12px] font-medium text-[var(--text-muted)]">
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
              className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
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
            title="关闭伴读栏 (Esc)"
            className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </header>

      {!tauriAvailable ? (
        <StatusBanner
          mode="offline"
          compact
          testId="dock-offline-banner"
          message="离线预览：数据为示例（未连接 Tauri）"
        />
      ) : dockError ? (
        <StatusBanner mode="error" compact testId="dock-error-banner" error={dockError} />
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-2 bg-[var(--bg-sunken)]/55 px-4 py-2 text-[12px]">
        <div className="flex min-w-0 items-center gap-1.5 text-[var(--text-muted)]">
          <Sparkles className="h-3.5 w-3.5 shrink-0 text-[var(--domain-core)]" aria-hidden="true" />
          <span className="truncate font-medium" title={contextTitle}>
            上下文：{contextTitle}
          </span>
        </div>
        <span
          className="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 font-mono text-[12px]"
          style={{
            backgroundColor:
              tauriAvailable && routeOnline ? 'var(--status-ok-bg)' : 'var(--status-warn-bg)',
            color:
              tauriAvailable && routeOnline ? 'var(--status-ok-fg)' : 'var(--status-warn-fg)',
          }}
          title={routeBadgeLabel}
        >
          {routeBadgeLabel}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5 border-b border-[var(--border-subtle)] px-4 py-2.5">
        {PRESET_ACTIONS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            disabled={isSending}
            onClick={() => void submitPrompt(`${preset.promptPrefix}：${contextTitle}`, preset.id)}
            className="cursor-pointer rounded-[8px] bg-[var(--bg-soft)] px-2.5 py-1 text-[12px] font-medium text-[var(--text-primary)] hover:brightness-95 disabled:opacity-50"
          >
            {preset.label}
          </button>
        ))}
        {onOpenAiWorkbench && (
          <button
            type="button"
            onClick={onOpenAiWorkbench}
            className="cursor-pointer rounded-[8px] px-2.5 py-1 text-[12px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            AI 工作台
          </button>
        )}
        {onOpenPluginStore && (
          <button
            type="button"
            onClick={onOpenPluginStore}
            className="cursor-pointer rounded-[8px] px-2.5 py-1 text-[12px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            插件中心
          </button>
        )}
      </div>

      <div
        data-testid="companion-messages"
        aria-live="polite"
        className="flex-1 space-y-3 overflow-y-auto px-4 py-3.5 text-[14px] leading-[1.72]"
      >
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={[
              'rounded-[12px] px-3.5 py-2.5',
              msg.role === 'user'
                ? 'ml-7 bg-[var(--bg-active)] text-[var(--text-primary)]'
                : 'mr-2 bg-[var(--bg-canvas)] text-[var(--text-primary)]',
            ].join(' ')}
            style={
              msg.isError
                ? {
                    backgroundColor: 'var(--status-err-bg)',
                    color: 'var(--status-err-fg)',
                  }
                : undefined
            }
          >
            <div className="mb-1 flex items-center justify-between gap-2 text-[12px] opacity-80">
              <span className="font-medium">
                {msg.role === 'user'
                  ? '你'
                  : msg.isError
                    ? 'OmniDesk Companion · 错误'
                    : msg.isDemo
                      ? 'OmniDesk Companion · 示例'
                      : 'OmniDesk Companion'}
              </span>
              <span>{msg.timestamp}</span>
            </div>
            <p className="whitespace-pre-wrap">{msg.content}</p>
            {msg.servedModel && (
              <div className="mt-1.5 font-mono text-[12px] opacity-75">{msg.servedModel}</div>
            )}
          </div>
        ))}
        {isSending && (
          <div
            data-testid="dock-pending-status"
            className="mr-2 rounded-[10px] bg-[var(--bg-soft)] px-3.5 py-2.5 text-[12.5px] text-[var(--text-muted)]"
          >
            正在等待 ai.agent.v1 返回...
          </div>
        )}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!isSending) {
            void submitPrompt(draft, 'chat');
          }
        }}
        className="border-t border-[var(--border-subtle)] bg-[var(--bg-canvas)] p-3.5"
      >
        <div className="rounded-[12px] border border-[var(--border-line)] bg-[var(--bg-surface)] p-2.5 focus-within:ring-2 focus-within:ring-[var(--focus-ring)]">
          <textarea
            ref={inputRef}
            rows={2}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            {...ime.compositionProps}
            aria-label="伴读栏提问输入"
            placeholder="针对当前阅读/媒体内容提问（Enter 发送，Shift+Enter 换行）..."
            className="w-full resize-none bg-transparent text-[13.5px] leading-[1.65] text-[var(--text-primary)] placeholder:text-[var(--text-subtle)] focus:outline-none"
          />
          <div className="mt-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-[12px] text-[var(--text-muted)]">
              <span>{effectiveLayout === 'side' ? '右侧分栏' : '底部分栏'}</span>
              {ime.isComposing && (
                <span className="rounded-full bg-[var(--status-info-bg)] px-2 py-0.5 text-[12px] text-[var(--status-info-fg)]">
                  IME 选词中
                </span>
              )}
            </div>
            <button
              type="submit"
              disabled={!draft.trim() || isSending}
              aria-label="发送伴读提问"
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] bg-[var(--bg-active)] px-3 py-1.5 text-[12.5px] font-medium text-[var(--text-primary)] hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-45"
            >
              <Send className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{isSending ? '发送中' : '发送'}</span>
            </button>
          </div>
        </div>
      </form>
    </aside>
  );
}

export default CompanionDock;
