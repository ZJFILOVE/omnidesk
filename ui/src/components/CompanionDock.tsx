import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import {
  Blocks,
  Bot,
  History,
  Maximize2,
  PanelBottom,
  PanelRight,
  Plus,
  Send,
  Sparkles,
  X,
} from 'lucide-react';
import type { DockWidgetSnapshot } from '../api';
import { useImeGuard } from '../hooks/useImeGuard';
import {
  DOCK_ASPECT_THRESHOLD,
  DOMAIN_CSS_VARS,
  DOMAIN_LABELS,
  resolveWorkbenchMeta,
} from '../theme';
import type {
  ActiveEntityContext,
  ContentDomain,
  DockLayout,
  WorkbenchId,
} from '../types';
import { FocusItem } from './FocusItem';
import { MarkdownRenderer } from './MarkdownRenderer';
import { PluginDockWidgets } from './PluginDockWidgets';
import { StatusBanner } from './StatusBanner';
import { CompanionSessionPanel } from './CompanionSessionPanel';
import type { CompanionMessage } from './companionHelpers';
import { useCompanionChat } from './useCompanionChat';

export type { CompanionMessage } from './companionHelpers';

export interface CompanionDockProps {
  open: boolean;
  onClose: () => void;
  layout?: DockLayout;
  activeWorkbench?: WorkbenchId;
  domain?: ContentDomain;
  contextEntity?: ActiveEntityContext | null;
  dockWidgets?: readonly DockWidgetSnapshot[];
  pendingPrompt?: string | null;
  onConsumePrompt?: () => void;
  onOpenAiWorkbench?: () => void;
  onOpenPluginStore?: () => void;
  onRefreshExtensions?: () => void;
}

type DockTab = 'chat' | 'tools';

interface DomainStarter {
  title: string;
  subtitle: string;
  prompt: string;
  send?: boolean;
}

const DOMAIN_STARTERS: Record<ContentDomain, DomainStarter[]> = {
  media: [
    {
      title: '章节时间轴与核心看点',
      subtitle: '基于当前影音上下文生成结构化摘要',
      prompt: '请基于当前影音上下文生成时间轴章节摘要与核心看点。',
      send: true,
    },
    {
      title: '双语歌词意象与语法',
      subtitle: '拆解修辞、文化背景与自然意译',
      prompt: '请分析当前曲目歌词的意象、语法结构并给出双语意译。',
    },
    {
      title: 'AI 灵感策展',
      subtitle: '把主题推荐反写回媒体工作台与歌单',
      prompt: '为雨夜写代码的场景推荐 5 首耐听器乐，并整理为可播放的灵感歌单。',
    },
  ],
  text: [
    {
      title: '提炼长文结论与论据',
      subtitle: '归纳架构、关键实现与边界条件',
      prompt: '请提炼当前长文的核心结论、论据与关键实现细节。',
      send: true,
    },
    {
      title: '汇总讨论共识与分歧',
      subtitle: '梳理高赞回复和争议焦点',
      prompt: '请汇总当前讨论区的高赞共识、核心分歧与延伸阅读线索。',
    },
    {
      title: '生成研究摘要卡片',
      subtitle: '研究问题、方法、指标与局限性',
      prompt: '请把当前论文整理成结构化研究摘要卡片。',
    },
  ],
  book: [
    {
      title: '检索并下载到书架',
      subtitle: '跨源检索 EPUB / PDF 并自动入架',
      prompt: '请帮我检索并下载到书架（优先 EPUB/PDF）：',
    },
    {
      title: '长难句与词根拆解',
      subtitle: '从句结构、学术句式与记忆提示',
      prompt: '请拆解当前段落的长难句结构，并给出核心词根记忆提示。',
      send: true,
    },
    {
      title: '提取生词与双语例句',
      subtitle: '生成释义、语境例句并加入 SM-2',
      prompt: '请提取当前阅读上下文中的高价值词汇并生成释义与例句。',
    },
  ],
  core: [
    {
      title: 'AI 灵感策展',
      subtitle: '围绕主题反写媒体、文字与书单',
      prompt: '推荐近期值得精读的系统架构与编译器优化深度长文。',
    },
    {
      title: '诊断路由与配额',
      subtitle: '读取 Magpie 分组、模型与余量',
      prompt: '请读取本机 Magpie 可用路由、模型分组与配额余量。',
      send: true,
    },
    {
      title: '检查插件健康拓扑',
      subtitle: '定位故障插件与缺失依赖',
      prompt: '请诊断当前 OmniDesk 已装载插件能力与依赖健康状态。',
    },
  ],
};

const DOMAIN_QUICK_PROMPTS: Record<ContentDomain, Array<{ label: string; prompt: string }>> = {
  media: [
    { label: '章节摘要', prompt: '请基于当前影音上下文生成章节时间轴摘要。' },
    { label: '双语歌词', prompt: '请分析当前曲目歌词意象并给出双语意译。' },
    { label: '歌单策展', prompt: '请围绕当前主题推荐 5 首曲目并整理为歌单。' },
  ],
  text: [
    { label: '提炼结论', prompt: '请提炼当前长文的核心结论、论据与边界条件。' },
    { label: '共识分歧', prompt: '请汇总当前讨论区的共识、分歧与延伸线索。' },
    { label: '研究卡片', prompt: '请把当前论文整理成结构化研究摘要卡片。' },
  ],
  book: [
    { label: '检索入架', prompt: '请检索当前主题的 EPUB/PDF 并整理入架建议。' },
    { label: '长难句', prompt: '请拆解当前段落的长难句结构并标注从句层次。' },
    { label: '生词例句', prompt: '请提取当前上下文的高价值词汇并生成双语例句。' },
  ],
  core: [
    { label: '路由诊断', prompt: '请读取本机 Magpie 可用路由、模型分组与配额余量。' },
    { label: '插件健康', prompt: '请诊断当前 OmniDesk 已装载插件能力与依赖健康状态。' },
    { label: '灵感策展', prompt: '请围绕当前主题生成一组跨媒体、文字与书单的策展建议。' },
  ],
};

export function resolveDockLayout(width?: number, height?: number): DockLayout {
  const w = width ?? (typeof window !== 'undefined' ? window.innerWidth : 1280);
  const h = height ?? (typeof window !== 'undefined' ? window.innerHeight : 800);
  if (h <= 0) {
    return 'side';
  }
  return w / h >= DOCK_ASPECT_THRESHOLD ? 'side' : 'bottom';
}

function sessionTitleFromMessages(messages: readonly CompanionMessage[]): string {
  const firstUser = messages.find((message) => message.role === 'user');
  if (!firstUser) return '新对话';
  const normalized = firstUser.content.replace(/\s+/g, ' ').trim();
  return normalized.length > 18 ? `${normalized.slice(0, 18)}…` : normalized;
}

function MessageTurn({ message }: { message: CompanionMessage }) {
  const isUser = message.role === 'user';
  return (
    <article
      className={`rounded-[13px] border px-3.5 py-3 ${
        isUser
          ? 'ml-6 border-[var(--border-subtle)] bg-[var(--bg-active)]'
          : 'mr-1 border-[var(--border-subtle)] bg-[var(--bg-canvas)]'
      }`}
    >
      <header className="mb-1.5 flex items-center justify-between gap-2 text-[12.5px] text-[var(--text-muted)]">
        <span className="inline-flex min-w-0 items-center gap-1.5 font-medium">
          {!isUser ? (
            <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-[var(--bg-soft)]">
              <Sparkles className="h-3 w-3" aria-hidden="true" />
            </span>
          ) : null}
          <span className="truncate">
            {isUser
              ? '你'
              : message.isError
                ? 'OmniDesk Companion · 错误'
                : message.isDemo
                  ? 'OmniDesk Companion · 示例'
                  : 'OmniDesk Companion'}
          </span>
        </span>
        <span className="shrink-0 font-mono">{message.timestamp}</span>
      </header>
      {message.isError ? (
        <p className="whitespace-pre-wrap text-[14.5px] leading-[1.7]">{message.content}</p>
      ) : (
        <MarkdownRenderer content={message.content} compact />
      )}
      {message.servedModel ? (
        <footer className="mt-2 border-t border-[var(--border-subtle)] pt-1.5 font-mono text-[11.75px] text-[var(--text-subtle)]">
          {message.servedModel}
        </footer>
      ) : null}
    </article>
  );
}

export function CompanionDock({
  open,
  onClose,
  layout,
  activeWorkbench = 'book',
  domain,
  contextEntity,
  dockWidgets = [],
  pendingPrompt,
  onConsumePrompt,
  onOpenAiWorkbench,
  onOpenPluginStore,
  onRefreshExtensions,
}: CompanionDockProps) {
  const [responsiveLayout, setResponsiveLayout] = useState<DockLayout>(() => resolveDockLayout());
  const [manualLayout, setManualLayout] = useState<DockLayout | null>(null);
  const [activeTab, setActiveTab] = useState<DockTab>('chat');
  const [showSessions, setShowSessions] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const ime = useImeGuard();

  const workbenchMeta = resolveWorkbenchMeta(activeWorkbench);
  const effectiveDomain: ContentDomain = domain ?? contextEntity?.domain ?? workbenchMeta.domain;
  const effectiveLayout: DockLayout = layout ?? manualLayout ?? responsiveLayout;
  const contextTitle = contextEntity?.title ?? `${workbenchMeta.label} (${workbenchMeta.shortLabel})`;
  const matchingWidgets = useMemo(
    () =>
      dockWidgets.filter(
        (widget) =>
          !widget.supported_domains ||
          widget.supported_domains.length === 0 ||
          widget.supported_domains.includes(effectiveDomain),
      ),
    [dockWidgets, effectiveDomain],
  );

  const {
    tauriAvailable,
    draft,
    setDraft,
    isSending,
    dockError,
    liveRouteLabel,
    routeOnline,
    messages,
    sessionId,
    activeSession,
    sessions,
    sessionsLoading,
    sessionError,
    sessionNotice,
    sessionBusyId,
    refreshSessions,
    openSession,
    startNewSession,
    deleteSession,
    exportSession,
    submitPrompt,
    resetConversation,
  } = useCompanionChat({
    open,
    effectiveDomain,
    contextTitle,
    contextEntity,
    restoreLastSessionOnOpen: !pendingPrompt,
  });

  const hasConversation = messages.some((message) => message.role === 'user');
  const sessionTitle = activeSession?.title || sessionTitleFromMessages(messages);
  const routeBadgeLabel = !tauriAvailable
    ? '离线预览'
    : liveRouteLabel ?? (routeOnline ? 'group/omnidesk-active' : 'Magpie 未就绪');
  const starters = DOMAIN_STARTERS[effectiveDomain];
  const quickPrompts = DOMAIN_QUICK_PROMPTS[effectiveDomain];

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
    setActiveTab('chat');
    void submitPrompt(pendingPrompt, 'chat');
    onConsumePrompt?.();
  }, [open, pendingPrompt, onConsumePrompt, submitPrompt]);

  useEffect(() => {
    if (!open || !hasConversation) return;
    const host = messagesRef.current;
    if (host) host.scrollTop = host.scrollHeight;
  }, [hasConversation, isSending, messages, open]);

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
      if (!isSending && draft.trim()) {
        void submitPrompt(draft, 'chat');
      }
    }
  };

  const startNewConversation = () => {
    resetConversation();
    setDraft('');
    setActiveTab('chat');
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const useStarter = (starter: DomainStarter) => {
    setActiveTab('chat');
    if (starter.send) {
      void submitPrompt(starter.prompt, 'chat');
      return;
    }
    setDraft(starter.prompt);
    requestAnimationFrame(() => inputRef.current?.focus());
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
      <header className="flex items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3.5 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]"
            style={{
              color: DOMAIN_CSS_VARS[effectiveDomain],
              backgroundColor: `color-mix(in srgb, ${DOMAIN_CSS_VARS[effectiveDomain]} 12%, var(--bg-soft))`,
            }}
            aria-hidden="true"
          >
            <Sparkles className="h-4 w-4" />
          </span>
          <button
            type="button"
            onClick={() => {
              setShowSessions(true);
              void refreshSessions();
            }}
            className="min-w-0 text-left"
            title="查看会话记录"
          >
            <span className="block truncate text-[14.5px] font-semibold text-[var(--text-primary)]">
              {sessionTitle}
            </span>
            <span className="block truncate text-[12px] text-[var(--text-muted)]">
              F8 伴读 · {DOMAIN_LABELS[effectiveDomain]}
            </span>
          </button>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <FocusItem
            as="button"
            id="dock:sessions"
            zone="dock"
            label="打开会话记录"
            aria-label="打开会话记录"
            title="会话记录"
            onActivate={() => {
              setShowSessions(true);
              void refreshSessions();
            }}
            className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            <History className="h-3.5 w-3.5" aria-hidden="true" />
          </FocusItem>
          <FocusItem
            as="button"
            id="dock:new-chat"
            zone="dock"
            label="开启新对话"
            onActivate={startNewConversation}
            className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
          </FocusItem>
          {onOpenAiWorkbench ? (
            <FocusItem
              as="button"
              id="dock:expand-ai"
              zone="dock"
              label="展开到 AI 工作台"
              onActivate={onOpenAiWorkbench}
              className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
            >
              <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
            </FocusItem>
          ) : null}
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

      <div className="flex items-center justify-between gap-2 border-b border-[var(--border-subtle)] bg-[var(--bg-sunken)]/55 px-3.5 py-2">
        <div
          role="tablist"
          aria-label="F8 伴读栏分区"
          className="inline-flex items-center gap-0.5 rounded-[12px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-0.5"
        >
          <FocusItem
            as="button"
            id="dock:tab:chat"
            zone="dock"
            label="AI 伴读"
            onActivate={() => setActiveTab('chat')}
            role="tab"
            aria-selected={activeTab === 'chat'}
            className={`inline-flex cursor-pointer items-center gap-1.5 rounded-[10px] px-2.5 py-1 text-[13px] font-medium ${
              activeTab === 'chat'
                ? 'bg-[var(--status-info-bg)] text-[var(--status-info-fg)]'
                : 'text-[var(--text-muted)] hover:bg-[var(--hover-fill)]'
            }`}
          >
            <Bot className="h-3.5 w-3.5" aria-hidden="true" />
            AI 伴读
          </FocusItem>
          <FocusItem
            as="button"
            id="dock:tab:tools"
            zone="dock"
            label={`插件组件 ${matchingWidgets.length}`}
            onActivate={() => setActiveTab('tools')}
            role="tab"
            aria-selected={activeTab === 'tools'}
            className={`inline-flex cursor-pointer items-center gap-1.5 rounded-[10px] px-2.5 py-1 text-[13px] font-medium ${
              activeTab === 'tools'
                ? 'bg-[var(--bg-active)] text-[var(--text-primary)]'
                : 'text-[var(--text-muted)] hover:bg-[var(--hover-fill)]'
            }`}
          >
            <Blocks className="h-3.5 w-3.5" aria-hidden="true" />
            组件{matchingWidgets.length > 0 ? ` · ${matchingWidgets.length}` : ''}
          </FocusItem>
        </div>

        <span
          className="inline-flex min-w-0 max-w-[45%] items-center gap-1 truncate rounded-full px-2 py-0.5 font-mono text-[11.5px]"
          style={{
            backgroundColor:
              tauriAvailable && routeOnline ? 'var(--status-ok-bg)' : 'var(--status-warn-bg)',
            color:
              tauriAvailable && routeOnline ? 'var(--status-ok-fg)' : 'var(--status-warn-fg)',
          }}
          title={routeBadgeLabel}
        >
          <span
            className="h-1.5 w-1.5 shrink-0 rounded-full bg-current"
            aria-hidden="true"
          />
          <span className="truncate">{routeBadgeLabel}</span>
        </span>
      </div>

      {activeTab === 'chat' ? (
        <>
          <div className="flex shrink-0 items-center gap-1.5 border-b border-[var(--border-subtle)] px-3.5 py-2 text-[12.5px] text-[var(--text-muted)]">
            <Sparkles className="h-3.5 w-3.5 shrink-0 text-[var(--domain-core)]" aria-hidden="true" />
            <span className="truncate" title={contextTitle}>
              上下文：{contextTitle}
              {activeSession ? ` · 会话：${activeSession.title}` : ''}
            </span>
          </div>

          <div
            ref={messagesRef}
            data-testid="companion-messages"
            aria-live="polite"
            className={
              hasConversation
                ? 'min-h-0 flex-1 space-y-2.5 overflow-y-auto px-3.5 py-3'
                : 'min-h-0 flex-1 overflow-y-auto px-4 py-4'
            }
          >
            {!hasConversation ? (
              <>
              <div className="flex flex-col items-center text-center">
                <span className="inline-flex h-11 w-11 items-center justify-center rounded-full bg-[var(--status-info-bg)]">
                  <Sparkles className="h-5.5 w-5.5 text-[var(--focus-ring)]" aria-hidden="true" />
                </span>
                <h2 className="mt-2.5 text-[16.5px] font-semibold tracking-tight text-[var(--text-primary)]">
                  今天想伴读什么，或策展灵感？
                </h2>
                <span className="mt-2 inline-flex items-center gap-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-0.5 text-[12px] text-[var(--text-muted)]">
                  <span
                    className="h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: DOMAIN_CSS_VARS[effectiveDomain] }}
                    aria-hidden="true"
                  />
                  当前域 · {DOMAIN_LABELS[effectiveDomain]}
                </span>
              </div>

              <div className="mt-4 space-y-2">
                {starters.map((starter) => (
                  <FocusItem
                    key={starter.title}
                    as="button"
                    id={`dock:starter:${starter.title}`}
                    zone="dock"
                    label={starter.title}
                    onActivate={() => useStarter(starter)}
                    className="w-full cursor-pointer rounded-[11px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2.5 text-left transition-colors hover:bg-[var(--hover-fill)]"
                  >
                    <span className="block text-[13.75px] font-semibold text-[var(--text-primary)]">
                      {starter.title}
                    </span>
                    <span className="mt-0.5 block text-[12.5px] leading-[1.45] text-[var(--text-muted)]">
                      {starter.subtitle}
                    </span>
                  </FocusItem>
                ))}
              </div>
              </>
            ) : (
              <>
              {messages.map((message) => (
                <MessageTurn key={message.id} message={message} />
              ))}
              {isSending && (
                <div
                  data-testid="dock-pending-status"
                  className="mr-1 rounded-[10px] bg-[var(--bg-soft)] px-3.5 py-2.5 text-[13.5px] text-[var(--text-muted)]"
                >
                  正在等待 ai.agent.v1 返回...
                </div>
              )}
              </>
            )}
          </div>

          {hasConversation ? (
            <div className="flex shrink-0 flex-wrap gap-1.5 border-b border-[var(--border-subtle)] px-3.5 py-2">
              {quickPrompts.map((quick) => (
                <FocusItem
                  key={quick.label}
                  as="button"
                  id={`dock:quick:${effectiveDomain}:${quick.label}`}
                  zone="dock"
                  label={quick.label}
                  disabled={isSending}
                  onActivate={() => void submitPrompt(quick.prompt, 'chat')}
                  className="cursor-pointer rounded-[8px] bg-[var(--bg-soft)] px-2.5 py-1 text-[12.75px] font-medium text-[var(--text-primary)] hover:brightness-95 disabled:opacity-50"
                >
                  {quick.label}
                </FocusItem>
              ))}
              {onOpenPluginStore ? (
                <button
                  type="button"
                  onClick={onOpenPluginStore}
                  className="cursor-pointer rounded-[8px] px-2 py-1 text-[12.75px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]"
                >
                  插件中心
                </button>
              ) : null}
            </div>
          ) : null}

          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!isSending) {
                void submitPrompt(draft, 'chat');
              }
            }}
            className="shrink-0 border-t border-[var(--border-subtle)] bg-[var(--bg-canvas)] p-3"
          >
            <div className="rounded-[12px] border border-[var(--border-line)] bg-[var(--bg-surface)] p-2.5 focus-within:ring-2 focus-within:ring-[var(--focus-ring)]">
              <textarea
                ref={inputRef}
                rows={hasConversation ? 2 : 3}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={handleKeyDown}
                {...ime.compositionProps}
                aria-label="伴读栏提问输入"
                placeholder="针对当前阅读/媒体内容提问（Enter 发送，Shift+Enter 换行）..."
                className="w-full resize-none bg-transparent text-[14.5px] leading-[1.62] text-[var(--text-primary)] placeholder:text-[var(--text-subtle)] focus:outline-none"
              />
              <div className="mt-1.5 flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2 text-[12px] text-[var(--text-muted)]">
                  <span>{effectiveLayout === 'side' ? '右侧分栏' : '底部分栏'}</span>
                  {ime.isComposing && (
                    <span className="rounded-full bg-[var(--status-info-bg)] px-1.5 py-0.5 text-[11.5px] text-[var(--status-info-fg)]">
                      IME
                    </span>
                  )}
                </div>
                <button
                  type="submit"
                  disabled={!draft.trim() || isSending}
                  aria-label="发送伴读提问"
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] bg-[var(--bg-active)] px-3 py-1.5 text-[13.5px] font-medium text-[var(--text-primary)] hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  <Send className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>{isSending ? '发送中' : '发送'}</span>
                </button>
              </div>
            </div>
          </form>
        </>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto px-3.5 py-3">
          <PluginDockWidgets
            widgets={dockWidgets}
            domain={effectiveDomain}
            tauriAvailable={tauriAvailable}
            onRefreshExtensions={onRefreshExtensions}
            onOpenPluginStore={onOpenPluginStore}
          />
        </div>
      )}

      {showSessions ? (
        <div className="absolute inset-0 z-20 flex bg-[var(--bg-canvas)]">
          <CompanionSessionPanel
            sessions={sessions}
            activeSessionId={sessionId}
            loading={sessionsLoading}
            busyId={sessionBusyId}
            error={sessionError}
            notice={sessionNotice}
            onRefresh={() => void refreshSessions()}
            onNewSession={() => {
              startNewSession();
              setActiveTab('chat');
              setShowSessions(false);
            }}
            onOpenSession={(id) => {
              void openSession(id).then((restored) => {
                if (restored) {
                  setActiveTab('chat');
                  setShowSessions(false);
                }
              });
            }}
            onDeleteSession={(id) => void deleteSession(id)}
            onExportSession={(id, format) => void exportSession(id, format)}
            onClose={() => setShowSessions(false)}
          />
        </div>
      ) : null}
    </aside>
  );
}

export default CompanionDock;
