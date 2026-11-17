import type { ComponentType, ReactNode } from 'react';
import {
  Archive,
  Blocks,
  BookOpen,
  CalendarDays,
  Command,
  Crosshair,
  Film,
  Globe,
  HardDrive,
  HelpCircle,
  Library,
  ListTodo,
  Moon,
  Music,
  Newspaper,
  PanelBottom,
  PanelRight,
  Puzzle,
  Rss,
  Settings,
  ShieldCheck,
  Sparkles,
  Sun,
} from 'lucide-react';
import { isTauriAvailable, type EventBridgeSnapshot, type RpcErrorInfo } from '../api';
import {
  DOMAIN_CSS_VARS,
  THEME_PALETTES,
  WORKBENCHES,
  resolveWorkbenchMeta,
} from '../theme';
import type {
  ActiveEntityContext,
  DockLayout,
  ThemeMode,
  WorkbenchId,
  WorkbenchMeta,
} from '../types';
import { AppShellFooter } from './AppShellFooter';
import { FocusItem } from './FocusItem';
import { Keycap } from './Keycap';
import { StatusBanner } from './StatusBanner';

export interface AppShellProps {
  activeWorkbench: WorkbenchId;
  onSelectWorkbench: (id: WorkbenchId) => void;
  theme: ThemeMode;
  onToggleTheme: () => void;
  dockOpen: boolean;
  dockLayout: DockLayout;
  onToggleDock: () => void;
  onOpenPalette: () => void;
  onTriggerHints: () => void;
  onToggleHelp?: () => void;
  onOpenAuth?: () => void;
  onOpenSettings?: () => void;
  onOpenJobs?: () => void;
  onOpenCuration?: () => void;
  activeJobCount?: number;
  focusedId: string | null;
  isComposing: boolean;
  contextEntity: ActiveEntityContext | null;
  workbenchMetas?: readonly WorkbenchMeta[];
  tauriAvailable?: boolean;
  coreOnline?: boolean;
  bridgeStatus?: EventBridgeSnapshot | null;
  statusError?: RpcErrorInfo | null;
  notice?: string | null;
  onDismissNotice?: () => void;
  miniPlayerSlot?: ReactNode;
  dockSlot?: ReactNode;
  children: ReactNode;
}

const WORKBENCH_ICONS: Record<string, ComponentType<{ className?: string }>> = {
  media: Film,
  text: Newspaper,
  book: BookOpen,
  ai: Sparkles,
  store: Blocks,
};

function pluginIcon(meta: WorkbenchMeta): ComponentType<{ className?: string }> {
  const key = `${meta.pluginId ?? ''} ${meta.label} ${meta.shortLabel}`.toLowerCase();
  if (key.includes('bilibili') || key.includes('youtube') || key.includes('imdb')) return Film;
  if (key.includes('music') || key.includes('歌词') || key.includes('daily 5')) return Music;
  if (key.includes('nas') || key.includes('fnos')) return HardDrive;
  if (key.includes('zlib') || key.includes('anna') || key.includes('archive')) return Archive;
  if (key.includes('ebook') || key.includes('epub')) return Library;
  if (key.includes('chrome') || key.includes('web')) return Globe;
  if (key.includes('blog') || key.includes('rss') || key.includes('feed')) return Rss;
  return Puzzle;
}

export function AppShell({
  activeWorkbench,
  onSelectWorkbench,
  theme,
  onToggleTheme,
  dockOpen,
  dockLayout,
  onToggleDock,
  onOpenPalette,
  onTriggerHints,
  onToggleHelp,
  onOpenAuth,
  onOpenSettings,
  onOpenJobs,
  onOpenCuration,
  activeJobCount = 0,
  focusedId,
  isComposing,
  contextEntity,
  workbenchMetas,
  tauriAvailable,
  coreOnline = false,
  bridgeStatus = null,
  statusError = null,
  notice = null,
  onDismissNotice,
  miniPlayerSlot,
  dockSlot,
  children,
}: AppShellProps) {
  const resolvedMetas = workbenchMetas && workbenchMetas.length > 0 ? workbenchMetas : WORKBENCHES;
  const activeMeta = resolveWorkbenchMeta(
    activeWorkbench,
    resolvedMetas.filter((meta) => meta.id.startsWith('plugin:')),
  );
  const palette = THEME_PALETTES[theme];
  const activeAccent = DOMAIN_CSS_VARS[activeMeta.domain];
  const inTauri = tauriAvailable ?? isTauriAvailable();

  const dockReserveStyle = dockOpen
    ? dockLayout === 'side'
      ? { paddingRight: 'min(var(--dock-side-width, 340px), 100vw)' }
      : { paddingBottom: 'var(--dock-bottom-height, clamp(236px, 34vh, 320px))' }
    : undefined;

  return (
    <div
      data-theme={theme}
      data-dock-open={dockOpen ? 'true' : 'false'}
      data-dock-layout={dockLayout}
      style={dockReserveStyle}
      className="omni-theme-surface omni-shell-frame h-screen w-screen overflow-hidden flex flex-col bg-canvas text-ink"
    >
      {/* Main Body: Tonal Left Rail + Quiet Utility Strip + Stage */}
      <div className="flex-1 min-h-0 flex overflow-hidden">
        <aside
          aria-label="工作台与插件页签导航"
          className="w-[64px] md:w-[210px] xl:w-[240px] shrink-0 flex flex-col justify-between py-3.5 px-2.5"
          style={{
            backgroundColor: 'var(--bg-sunken)',
            borderRight: '1px solid var(--border-subtle)',
          }}
        >
          <div className="flex flex-col gap-4">
            {/* Brand Lockup inside Sunken Rail */}
            <div className="flex items-center justify-center md:justify-start gap-2.5 px-1.5 pt-0.5">
              <span
                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] text-[14.5px] font-bold"
                style={{
                  backgroundColor: `color-mix(in srgb, ${activeAccent} 14%, var(--bg-surface))`,
                  color: activeAccent,
                  boxShadow: 'var(--shadow-page)',
                }}
                aria-hidden="true"
              >
                万
              </span>
              <div className="hidden md:flex flex-col min-w-0 leading-none">
                <span className="text-[15.5px] font-semibold tracking-tight text-ink">
                  OmniDesk
                </span>
                <span className="mt-1 font-mono text-[12.5px] text-subtle">Atelier v2</span>
              </div>
            </div>

            <nav className="flex min-h-0 flex-col gap-1 overflow-y-auto pr-0.5" aria-label="工作台切换">
              {resolvedMetas.map((wb, index) => {
                const Icon = WORKBENCH_ICONS[wb.id] ?? pluginIcon(wb);
                const isCurrent = wb.id === activeWorkbench;
                const domainColor = DOMAIN_CSS_VARS[wb.domain];
                const isPlugin = wb.id.startsWith('plugin:');
                const showPluginHeader =
                  isPlugin && (index === 0 || !resolvedMetas[index - 1]?.id.startsWith('plugin:'));

                return (
                  <div key={wb.id} className="contents">
                    {showPluginHeader ? (
                      <div className="hidden px-2.5 pt-3 pb-1 text-[11.5px] font-medium tracking-[0.08em] text-[var(--text-subtle)] md:block">
                        插件页签
                      </div>
                    ) : null}
                    <FocusItem
                      as="button"
                      id={`nav-workbench-${wb.id}`}
                      zone="nav"
                      label={`${wb.label}${wb.shortcut ? ` (${wb.shortcut})` : ''}`}
                      priority={isPlugin ? 8 : 20 - (wb.index ?? 0)}
                      onActivate={() => onSelectWorkbench(wb.id)}
                      aria-current={isCurrent ? 'page' : undefined}
                      className={[
                        'omni-focus-row group relative w-full rounded-[10px] px-2.5 py-2.5 flex flex-col md:flex-row items-center md:justify-between gap-1 md:gap-2 text-left cursor-pointer transition-colors hover:bg-[var(--hover-fill)]',
                        isPlugin ? 'md:pl-3' : '',
                      ].join(' ')}
                      style={
                        isCurrent
                          ? {
                              backgroundColor: 'var(--bg-surface)',
                              boxShadow: 'var(--shadow-page)',
                            }
                          : undefined
                      }
                    >
                      {isCurrent && (
                        <span
                          className="absolute left-0 top-2.5 bottom-2.5 w-[3px] rounded-r-full"
                          style={{ backgroundColor: domainColor }}
                          aria-hidden="true"
                        />
                      )}
                      <span className="flex items-center gap-2.5 min-w-0">
                        <span
                          className="shrink-0 flex items-center justify-center"
                          style={{ color: isCurrent ? domainColor : 'var(--text-muted)' }}
                        >
                          <Icon className="h-4 w-4" aria-hidden="true" />
                        </span>
                        <span className="hidden min-w-0 md:block">
                          <span
                            className={`block truncate text-[14.5px] leading-tight ${
                              isCurrent ? 'font-semibold text-ink' : 'font-medium text-ink-secondary'
                            }`}
                          >
                            {wb.label}
                          </span>
                          {isPlugin && wb.badge ? (
                            <span className="mt-0.5 block truncate font-mono text-[11.5px] text-[var(--text-subtle)]">
                              {wb.badge}
                            </span>
                          ) : null}
                        </span>
                      </span>

                      {wb.index || wb.shortcut ? (
                        <Keycap
                          size="xs"
                          tone={isCurrent ? 'accent' : 'subtle'}
                          domain={wb.domain}
                          className="hidden md:inline-flex shrink-0"
                        >
                          {wb.index ? `${wb.index}` : wb.shortcut.replace(/^Alt\+/, '')}
                        </Keycap>
                      ) : null}
                    </FocusItem>
                  </div>
                );
              })}
            </nav>
          </div>

          {/* Quiet Theme & Dock Status */}
          <div
            className="hidden md:flex flex-col gap-1.5 pt-3 px-2 text-[13.5px] text-muted"
            style={{ borderTop: '1px solid var(--border-subtle)' }}
          >
            <div className="flex items-center justify-between gap-1.5">
              <span className="truncate font-medium text-ink-secondary">
                {palette.mode === 'paper' ? 'Paper' : 'Graphite'}
              </span>
              <span
                className="h-2 w-2 rounded-full"
                style={{ backgroundColor: activeAccent }}
                aria-hidden="true"
              />
            </div>
            <div className="flex items-center justify-between text-[13.5px] text-subtle">
              <span>伴读停靠</span>
              <span>{dockLayout === 'side' ? '右侧' : '底部'}</span>
            </div>
          </div>
        </aside>

        {/* Workspace Column: Slim Utility Header + Optional Status Banner + Active Workbench */}
        <div className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden bg-canvas">
          <header
            className="h-10 shrink-0 px-6 flex items-center justify-between gap-3"
            style={{ borderBottom: '1px solid var(--border-subtle)' }}
          >
            <div className="flex items-center gap-2.5 min-w-0">
              <span
                className="h-2 w-2 rounded-full shrink-0 md:hidden"
                style={{ backgroundColor: activeAccent }}
                aria-hidden="true"
              />
              <span className="text-[14.5px] font-semibold text-ink md:hidden truncate">
                {activeMeta.label}
              </span>
              <span className="hidden md:inline-flex items-center gap-2 text-[14px] text-muted truncate">
                <span
                  className="h-1.5 w-1.5 rounded-full shrink-0"
                  style={{ backgroundColor: activeAccent }}
                  aria-hidden="true"
                />
                <span className="font-medium text-ink-secondary">{activeMeta.shortLabel}</span>
                {contextEntity && contextEntity.title !== activeMeta.label ? (
                  <>
                    <span className="text-subtle" aria-hidden="true">
                      /
                    </span>
                    <span className="truncate text-muted">{contextEntity.title}</span>
                  </>
                ) : null}
              </span>
            </div>

            <div className="flex items-center gap-1 shrink-0">
              <FocusItem
                as="button"
                id="shell-cmd-palette"
                zone="nav"
                label="打开命令面板 (Ctrl+K)"
                priority={5}
                onActivate={onOpenPalette}
                className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
              >
                <Command className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                <span className="hidden sm:inline">命令</span>
                <Keycap size="xs" tone="subtle">Ctrl+K</Keycap>
              </FocusItem>

              {onOpenCuration ? (
                <FocusItem
                  as="button"
                  id="shell-curation-center"
                  zone="nav"
                  label="推荐日历与定时调度"
                  priority={6}
                  onActivate={onOpenCuration}
                  className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
                >
                  <CalendarDays className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  <span className="hidden xl:inline">日历</span>
                </FocusItem>
              ) : null}

              {onOpenAuth ? (
                <FocusItem
                  as="button"
                  id="shell-auth-center"
                  zone="nav"
                  label="统一认证中心"
                  priority={6}
                  onActivate={onOpenAuth}
                  className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
                >
                  <ShieldCheck className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  <span className="hidden xl:inline">认证</span>
                </FocusItem>
              ) : null}

              {onOpenJobs ? (
                <FocusItem
                  as="button"
                  id="shell-job-center"
                  zone="nav"
                  label="任务中心"
                  priority={6}
                  onActivate={onOpenJobs}
                  className="relative inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
                >
                  <ListTodo className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  <span className="hidden xl:inline">任务</span>
                  {activeJobCount > 0 ? (
                    <span className="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--domain-core)] px-1 font-mono text-[10.5px] text-white">
                      {activeJobCount}
                    </span>
                  ) : null}
                </FocusItem>
              ) : null}

              {onOpenSettings ? (
                <FocusItem
                  as="button"
                  id="shell-settings-center"
                  zone="nav"
                  label="宿主设置中心"
                  priority={6}
                  onActivate={onOpenSettings}
                  className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
                >
                  <Settings className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  <span className="hidden xl:inline">设置</span>
                </FocusItem>
              ) : null}

              <FocusItem
                as="button"
                id="shell-hints"
                zone="nav"
                label="屏幕焦点闪跳提示 (f)"
                priority={4}
                onActivate={onTriggerHints}
                className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
              >
                <Crosshair className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                <span className="hidden md:inline">闪跳</span>
                <Keycap size="xs" tone="subtle">f</Keycap>
              </FocusItem>

              <FocusItem
                as="button"
                id="shell-dock-toggle"
                zone="nav"
                label="切换 F8 无遮罩伴读栏"
                priority={3}
                onActivate={onToggleDock}
                aria-pressed={dockOpen}
                className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
                style={
                  dockOpen
                    ? {
                        backgroundColor: `color-mix(in srgb, ${activeAccent} 12%, var(--bg-soft))`,
                        color: activeAccent,
                      }
                    : undefined
                }
              >
                {dockLayout === 'side' ? (
                  <PanelRight className="h-3.5 w-3.5" aria-hidden="true" />
                ) : (
                  <PanelBottom className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                <span className="hidden sm:inline">伴读栏</span>
                <Keycap size="xs" tone={dockOpen ? 'accent' : 'subtle'} domain={activeMeta.domain}>
                  F8
                </Keycap>
              </FocusItem>

              <FocusItem
                as="button"
                id="shell-theme-toggle"
                zone="nav"
                label="切换 Paper / Graphite 主题 (t / Alt+T)"
                priority={2}
                onActivate={onToggleTheme}
                className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
              >
                {theme === 'paper' ? (
                  <Sun className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                ) : (
                  <Moon className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                )}
                <span className="hidden sm:inline">
                  {theme === 'paper' ? 'Paper' : 'Graphite'}
                </span>
                <Keycap size="xs" tone="subtle">t</Keycap>
              </FocusItem>

              {onToggleHelp && (
                <FocusItem
                  as="button"
                  id="shell-help"
                  zone="nav"
                  label="查看快捷键帮助 (?)"
                  priority={1}
                  onActivate={onToggleHelp}
                  className="inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[14px] font-medium text-ink-secondary hover:bg-[var(--hover-fill)] hover:text-ink cursor-pointer transition-colors"
                >
                  <HelpCircle className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  <span className="hidden lg:inline">快捷键</span>
                  <Keycap size="xs" tone="subtle">?</Keycap>
                </FocusItem>
              )}
            </div>
          </header>

          {!inTauri ? (
            <StatusBanner mode="offline" testId="offline-preview-banner" detail="Browser Preview" />
          ) : statusError ? (
            <StatusBanner mode="error" testId="backend-error-banner" error={statusError} detail="Core IPC" />
          ) : null}
          {notice ? (
            <div
              className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--status-warn-bg)] px-6 py-1.5 text-[13.5px] text-[var(--status-warn-fg)]"
              role="status"
            >
              <span className="truncate">{notice}</span>
              {onDismissNotice ? (
                <button
                  type="button"
                  onClick={onDismissNotice}
                  className="cursor-pointer font-medium opacity-75 hover:opacity-100"
                >
                  关闭
                </button>
              ) : null}
            </div>
          ) : null}

          <main className="flex-1 min-w-0 min-h-0 flex flex-col bg-canvas overflow-hidden">
            {children}
          </main>
        </div>
      </div>

      {dockSlot}
      {miniPlayerSlot}

      <AppShellFooter
        activeMeta={activeMeta}
        activeAccent={activeAccent}
        contextEntity={contextEntity}
        focusedId={focusedId}
        isComposing={isComposing}
        inTauri={inTauri}
        coreOnline={coreOnline}
        bridgeStatus={bridgeStatus}
      />
    </div>
  );
}
