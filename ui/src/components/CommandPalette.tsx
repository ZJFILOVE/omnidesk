import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Command, CornerDownLeft, Search, X } from 'lucide-react';
import { isTauriAvailable, searchIndex, type SearchHit } from '../api';
import { useImeGuard } from '../hooks/useImeGuard';
import { DOMAIN_CSS_VARS, WORKBENCHES } from '../theme';
import type { ContentDomain, ThemeMode, WorkbenchId, WorkbenchMeta } from '../types';

export type CommandPaletteCategory =
  | 'workbench'
  | 'theme'
  | 'dock'
  | 'navigation'
  | 'search'
  | 'plugin';

export interface CommandPaletteItem {
  id: string;
  title: string;
  subtitle?: string;
  category: CommandPaletteCategory;
  shortcut?: string;
  domain?: ContentDomain;
  keywords?: string[];
  action?: () => void;
}

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onCommand?: (commandId: string) => void;
  activeWorkbench?: WorkbenchId;
  theme?: ThemeMode;
  dockOpen?: boolean;
  commands?: CommandPaletteItem[];
  workbenches?: readonly WorkbenchMeta[];
  initialQuery?: string;
}

const CATEGORY_LABELS: Record<CommandPaletteCategory, string> = {
  workbench: '工作台',
  theme: '外观主题',
  dock: '伴读栏',
  navigation: '键盘导航',
  search: '跨域检索',
  plugin: '插件与策展',
};

const CORE_COMMANDS: readonly CommandPaletteItem[] = [
  { id: 'dock:toggle', title: '开关 F8 无遮罩 AI 伴读栏', subtitle: '随宽高比 (>= 1.45) 在右侧与底部之间自适应停靠', category: 'dock', shortcut: 'F8', domain: 'core', keywords: ['companion', 'dock', 'ai', 'f8', '伴读'] },
  { id: 'hints:open', title: '触发全屏字母闪跳 (Hint Overlay)', subtitle: '显示可聚焦元素字母角标，按对应字母立即跳转', category: 'navigation', shortcut: 'f', domain: 'core', keywords: ['hint', 'jump', 'focus', '闪跳', '定位'] },
  { id: 'help:open', title: '查看键盘导航与快捷键帮助', subtitle: '包含工作台快捷键、2D 空间焦点与 IME 隔离说明', category: 'navigation', shortcut: '?', domain: 'core', keywords: ['help', 'keyboard', 'shortcuts', '帮助', '快捷键'] },
  { id: 'search:focus', title: '聚焦当前工作台搜索框', subtitle: '按标题、来源或关键词即时过滤当前列表', category: 'search', shortcut: '/', domain: 'core', keywords: ['search', 'filter', '搜索', '过滤'] },
  { id: 'focus:cycle-zone', title: '在主舞台与 F8 伴读栏间切换焦点区', subtitle: '切换主内容区域与伴读输入区域', category: 'navigation', shortcut: 'Ctrl+Tab', domain: 'core', keywords: ['zone', 'focus', 'tab', '焦点'] },
  { id: 'theme:toggle', title: '切换 Paper / Graphite 主题', subtitle: 'Paper #F6F5F3 纸感亮色 ↔ Graphite #181718 石墨深色', category: 'theme', shortcut: 't', domain: 'core', keywords: ['theme', 'paper', 'graphite', 'dark', 'light', '主题'] },
  { id: 'theme:paper', title: '切换至 Paper 纸感亮色 (#F6F5F3)', subtitle: '暖纸底色与柔和层级的日光阅读画布', category: 'theme', domain: 'core', keywords: ['paper', 'light', '亮色'] },
  { id: 'theme:graphite', title: '切换至 Graphite 石墨深色 (#181718)', subtitle: '低眩光石墨深色护眼模式', category: 'theme', domain: 'core', keywords: ['graphite', 'dark', '深色'] },
  { id: 'curate:sync-archive', title: '同步 obsidian-vault & music-daily 历史归档', subtitle: '提交真实后台 Job 并快进拉取双仓、回灌历史推荐记录', category: 'plugin', domain: 'core', keywords: ['obsidian', 'music-daily', 'rehydrate', '同步', '归档', 'job'] },
  { id: 'curate:calendar', title: '打开推荐日历与定时工作流', subtitle: '按日期/领域浏览归档、检查缺口并校验发布证据', category: 'plugin', domain: 'core', keywords: ['calendar', 'daily', 'scheduler', '日历', '调度', '补跑'] },
  { id: 'curate:run-daily', title: '补跑每日不重复策展管线 (daily_immutable)', subtitle: '打开证据校验面板；不会用按钮切换页面冒充执行', category: 'plugin', domain: 'core', keywords: ['daily', 'curation', '策展', '精选', '补跑'] },
  { id: 'plugin:verify-all', title: '巡检零 ABI Python 插件健康状态', subtitle: '逐个调用 plugin.verify，并显示真实失败码', category: 'plugin', domain: 'core', keywords: ['plugin', 'health', 'verify', '插件', '巡检'] },
  { id: 'auth:center', title: '打开统一认证中心', subtitle: '聚合真实登录态、匿名 Cookie 警告与 Chrome 抓取', category: 'plugin', domain: 'core', keywords: ['auth', 'login', 'cookie', '认证', '登录', 'chrome'] },
  { id: 'jobs:open', title: '打开后台任务中心', subtitle: '查看 JobCenter 进度、阶段、取消与结果', category: 'navigation', domain: 'core', keywords: ['job', 'task', 'progress', '任务', '进度', '取消'] },
  { id: 'settings:open', title: '打开宿主设置中心', subtitle: '运行时、XDG 路径、认证、AI 路由与插件目录', category: 'navigation', domain: 'core', keywords: ['settings', 'paths', 'xdg', '设置', '路径'] },
];

export function buildPaletteCommands(
  workbenches: readonly WorkbenchMeta[] = WORKBENCHES,
): CommandPaletteItem[] {
  const workbenchCommands: CommandPaletteItem[] = workbenches.map((wb) => ({
    id: `workbench:${wb.id}`,
    title: `切换到 ${wb.label} (${wb.shortLabel})`,
    subtitle: wb.description,
    category: 'workbench',
    shortcut: wb.shortcut,
    domain: wb.domain,
    keywords: [wb.id, wb.shortLabel, wb.label, 'workbench', '切换'],
  }));
  return [...workbenchCommands, ...CORE_COMMANDS];
}

export function buildDefaultPaletteCommands(): CommandPaletteItem[] {
  return buildPaletteCommands(WORKBENCHES);
}

export const DEFAULT_PALETTE_COMMANDS: readonly CommandPaletteItem[] =
  buildDefaultPaletteCommands();

function matchesQuery(cmd: CommandPaletteItem, normalizedQuery: string): boolean {
  if (!normalizedQuery) {
    return true;
  }
  const haystack = [
    cmd.id,
    cmd.title,
    cmd.subtitle ?? '',
    cmd.category,
    CATEGORY_LABELS[cmd.category],
    cmd.shortcut ?? '',
    ...(cmd.keywords ?? []),
  ]
    .join(' ')
    .toLowerCase();

  return normalizedQuery
    .split(/\s+/)
    .filter(Boolean)
    .every((token) => haystack.includes(token));
}

export function CommandPalette({
  open,
  onClose,
  onCommand,
  activeWorkbench,
  theme,
  dockOpen,
  commands,
  workbenches,
  initialQuery = '',
}: CommandPaletteProps) {
  const [query, setQuery] = useState(initialQuery);
  const [activeIndex, setActiveIndex] = useState(0);
  const [remoteHits, setRemoteHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const ime = useImeGuard();

  const resolvedCommands = useMemo<readonly CommandPaletteItem[]>(
    () =>
      commands && commands.length > 0
        ? commands
        : buildPaletteCommands(workbenches ?? WORKBENCHES),
    [commands, workbenches],
  );

  const filteredCommands = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return resolvedCommands.filter((cmd) => matchesQuery(cmd, normalized));
  }, [resolvedCommands, query]);

  useEffect(() => {
    const normalized = query.trim();
    if (!open || normalized.length === 0 || !isTauriAvailable()) {
      setRemoteHits([]);
      setSearching(false);
      return undefined;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setSearching(true);
      void searchIndex({ query: normalized, limit: 14 })
        .then((result) => {
          if (cancelled) return;
          setSearching(false);
          setRemoteHits(result.available && result.ok ? result.data.hits : []);
        })
        .catch(() => {
          if (!cancelled) {
            setSearching(false);
            setRemoteHits([]);
          }
        });
    }, 220);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [open, query]);

  const remoteItems = useMemo<CommandPaletteItem[]>(
    () =>
      remoteHits.map((hit) => ({
        id: `fts:${hit.domain}:${hit.id}`,
        title: hit.title || hit.id,
        subtitle: hit.snippet || `${hit.domain}${hit.sourceId ? ` · ${hit.sourceId}` : ''}`,
        category: 'search',
        domain: ['media', 'text', 'book', 'core'].includes(hit.domain)
          ? (hit.domain as ContentDomain)
          : 'core',
        keywords: [hit.id, hit.domain, hit.sourceId ?? '', hit.pluginId ?? ''],
      })),
    [remoteHits],
  );
  const displayItems = useMemo(
    () => [...filteredCommands, ...remoteItems],
    [filteredCommands, remoteItems],
  );

  useEffect(() => {
    if (!open) {
      return;
    }
    setQuery(initialQuery);
    setActiveIndex(0);
    inputRef.current?.focus();
  }, [open, initialQuery]);

  useEffect(() => {
    if (displayItems.length === 0) {
      setActiveIndex(0);
    } else if (activeIndex >= displayItems.length) {
      setActiveIndex(displayItems.length - 1);
    }
  }, [displayItems.length, activeIndex]);

  if (!open) {
    return null;
  }

  const executeCommand = (cmd: CommandPaletteItem) => {
    cmd.action?.();
    onCommand?.(cmd.id);
    onClose();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (ime.shouldIgnoreShortcut(event)) {
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === 'ArrowDown' || (event.ctrlKey && event.key.toLowerCase() === 'n')) {
      event.preventDefault();
      if (displayItems.length > 0) {
        setActiveIndex((prev) => (prev + 1) % displayItems.length);
      }
      return;
    }
    if (event.key === 'ArrowUp' || (event.ctrlKey && event.key.toLowerCase() === 'p')) {
      event.preventDefault();
      if (displayItems.length > 0) {
        setActiveIndex((prev) => (prev - 1 + displayItems.length) % displayItems.length);
      }
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setActiveIndex(0);
      return;
    }
    if (event.key === 'End') {
      event.preventDefault();
      if (displayItems.length > 0) {
        setActiveIndex(displayItems.length - 1);
      }
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      const target = displayItems[activeIndex] ?? displayItems[0];
      if (target) {
        executeCommand(target);
      }
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-[var(--backdrop-scrim)] px-4 pt-[11vh]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) {
          onClose();
        }
      }}
      onKeyDown={handleKeyDown}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="全局命令面板"
        data-testid="command-palette-dialog"
        style={{ boxShadow: 'var(--shadow-elevated)' }}
        className="omni-theme-surface omni-drawer-motion w-full max-w-2xl overflow-hidden rounded-[18px] bg-[var(--bg-elevated)] text-[var(--text-primary)]"
      >
        <div className="flex items-center gap-3 border-b border-[var(--border-subtle)] px-5 py-3.5">
          <Search className="h-4 w-4 shrink-0 text-[var(--text-muted)]" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            {...ime.compositionProps}
            placeholder="搜索命令、台词、书架、往期精选、乐评与生词卡 (Ctrl+K)..."
            aria-label="命令搜索"
            aria-activedescendant={
              displayItems[activeIndex]
                ? `omni-cmd-${displayItems[activeIndex].id}`
                : undefined
            }
            className="w-full bg-transparent text-[16px] text-[var(--text-primary)] placeholder:text-[var(--text-subtle)] focus:outline-none"
          />
          {ime.isComposing && (
            <span className="shrink-0 rounded-full bg-[var(--status-info-bg)] px-2.5 py-0.5 text-[13.5px] font-medium text-[var(--status-info-fg)]">
              IME
            </span>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭命令面板"
            title="关闭命令面板 (Esc)"
            className="inline-flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {(activeWorkbench || theme || typeof dockOpen === 'boolean') && (
          <div className="flex flex-wrap items-center gap-3 bg-[var(--bg-sunken)]/55 px-5 py-2 text-[13.5px] text-[var(--text-muted)]">
            {activeWorkbench && <span>当前工作台：{activeWorkbench}</span>}
            {theme && <span>主题：{theme}</span>}
            {typeof dockOpen === 'boolean' && (
              <span>伴读栏：{dockOpen ? '已展开' : '已收起'}</span>
            )}
          </div>
        )}

        <div role="listbox" aria-label="可用命令列表" className="max-h-[54vh] overflow-y-auto p-2.5">
          {displayItems.length === 0 ? (
            <div className="px-4 py-10 text-center text-[15.5px] text-[var(--text-muted)]">
              {searching ? '正在检索工作台、归档与命令…' : `未找到匹配“${query}”的内容`}
            </div>
          ) : (
            displayItems.map((cmd, idx) => {
              const selected = idx === activeIndex;
              const domainColor = cmd.domain ? DOMAIN_CSS_VARS[cmd.domain] : 'var(--domain-core)';
              return (
                <button
                  key={cmd.id}
                  id={`omni-cmd-${cmd.id}`}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onMouseEnter={() => setActiveIndex(idx)}
                  onClick={() => executeCommand(cmd)}
                  className={[
                    'flex w-full cursor-pointer items-center justify-between gap-3 rounded-[10px] px-3.5 py-2.5 text-left transition-colors',
                    selected
                      ? 'bg-[var(--focus-fill)] ring-2 ring-inset ring-[var(--focus-ring)]'
                      : 'hover:bg-[var(--hover-fill)]',
                  ].join(' ')}
                >
                  <div className="flex min-w-0 items-start gap-3">
                    <span
                      aria-hidden="true"
                      className="mt-2 h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: domainColor }}
                    />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-[15.5px] font-medium text-[var(--text-primary)]">
                          {cmd.title}
                        </span>
                        <span className="shrink-0 rounded-full bg-[var(--bg-soft)] px-2.5 py-0.5 text-[13.5px] text-[var(--text-muted)]">
                          {CATEGORY_LABELS[cmd.category]}
                        </span>
                      </div>
                      {cmd.subtitle && (
                        <p className="mt-0.5 truncate text-[14px] text-[var(--text-muted)]">
                          {cmd.subtitle}
                        </p>
                      )}
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    {cmd.shortcut && (
                      <kbd className="omni-keycap rounded-[5px] px-1.5 py-0.5 text-[13.5px]">
                        {cmd.shortcut}
                      </kbd>
                    )}
                    {selected && (
                      <CornerDownLeft className="h-3.5 w-3.5 text-[var(--focus-ring)]" aria-hidden="true" />
                    )}
                  </div>
                </button>
              );
            })
          )}
        </div>

        <div className="flex items-center justify-between border-t border-[var(--border-subtle)] bg-[var(--bg-sunken)]/55 px-5 py-2 text-[13.5px] text-[var(--text-muted)]">
          <span className="inline-flex items-center gap-1.5">
            <Command className="h-3.5 w-3.5" aria-hidden="true" />
            <span>↑↓ 导航 · Enter 执行 · Esc 关闭</span>
          </span>
          <span>
            {searching ? '检索中… · ' : ''}共 {displayItems.length} 项
          </span>
        </div>
      </div>
    </div>
  );
}

export default CommandPalette;
