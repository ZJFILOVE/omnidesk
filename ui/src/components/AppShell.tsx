import type { ComponentType, ReactNode } from 'react';
import {
  Blocks,
  BookOpen,
  Command,
  Crosshair,
  Film,
  Keyboard,
  Moon,
  Newspaper,
  PanelBottom,
  PanelRight,
  Sparkles,
  Sun,
} from 'lucide-react';
import { DOMAIN_CSS_VARS, THEME_PALETTES, WORKBENCHES, WORKBENCH_MAP } from '../theme';
import type { ActiveEntityContext, DockLayout, ThemeMode, WorkbenchId } from '../types';
import { FocusItem } from './FocusItem';
import { Keycap } from './Keycap';

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
  focusedId: string | null;
  isComposing: boolean;
  contextEntity: ActiveEntityContext | null;
  dockSlot?: ReactNode;
  children: ReactNode;
}

const WORKBENCH_ICONS: Record<WorkbenchId, ComponentType<{ className?: string }>> = {
  media: Film,
  text: Newspaper,
  book: BookOpen,
  ai: Sparkles,
  store: Blocks,
};

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
  focusedId,
  isComposing,
  contextEntity,
  dockSlot,
  children,
}: AppShellProps) {
  const activeMeta = WORKBENCH_MAP[activeWorkbench];
  const palette = THEME_PALETTES[theme];
  const activeAccent = DOMAIN_CSS_VARS[activeMeta.domain];

  const dockReserveStyle = dockOpen
    ? dockLayout === 'side'
      ? { paddingRight: 'min(380px, 100vw)' }
      : { paddingBottom: 'clamp(260px, 40vh, 420px)' }
    : undefined;

  return (
    <div
      data-theme={theme}
      style={dockReserveStyle}
      className="omni-theme-surface h-screen w-screen overflow-hidden flex flex-col bg-canvas text-ink transition-[padding] duration-150 ease-out"
    >
      {/* Top Shell Header */}
      <header className="h-11 shrink-0 border-b border-line bg-surface px-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="flex items-center gap-2 shrink-0">
            <span
              className="inline-flex h-5 w-5 items-center justify-center rounded-md border border-line bg-soft text-[11px] font-semibold"
              style={{ color: activeAccent }}
              aria-hidden="true"
            >
              万
            </span>
            <span className="text-[13px] font-semibold tracking-tight text-ink">
              万象 · OmniDesk
            </span>
            <span className="hidden sm:inline-flex rounded px-1.5 py-[1px] text-[10px] font-mono bg-soft text-muted border border-line">
              v2
            </span>
          </div>

          <span className="h-3.5 w-px bg-line shrink-0" aria-hidden="true" />

          <div className="flex items-center gap-2 min-w-0">
            <span
              className="h-2 w-2 rounded-full shrink-0"
              style={{ backgroundColor: activeAccent }}
              aria-hidden="true"
            />
            <span className="text-xs font-medium text-ink shrink-0">{activeMeta.label}</span>
            <span className="hidden xl:inline text-xs text-muted truncate">
              {activeMeta.description}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          <FocusItem
            as="button"
            id="shell-cmd-palette"
            zone="nav"
            label="打开命令面板 (Ctrl+K)"
            priority={5}
            onActivate={onOpenPalette}
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-soft/70 hover:bg-soft px-2 py-1 text-xs text-ink cursor-pointer transition-colors"
          >
            <Command className="h-3.5 w-3.5 text-muted" />
            <span className="hidden sm:inline">命令面板</span>
            <Keycap size="xs">Ctrl+K</Keycap>
          </FocusItem>

          <FocusItem
            as="button"
            id="shell-hints"
            zone="nav"
            label="屏幕焦点闪跳提示 (f)"
            priority={4}
            onActivate={onTriggerHints}
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-soft/70 hover:bg-soft px-2 py-1 text-xs text-ink cursor-pointer transition-colors"
          >
            <Crosshair className="h-3.5 w-3.5 text-muted" />
            <span className="hidden md:inline">闪跳</span>
            <Keycap size="xs">f</Keycap>
          </FocusItem>

          <FocusItem
            as="button"
            id="shell-dock-toggle"
            zone="nav"
            label="切换 F8 无遮罩伴读栏"
            priority={3}
            onActivate={onToggleDock}
            aria-pressed={dockOpen}
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-soft/70 hover:bg-soft px-2 py-1 text-xs text-ink cursor-pointer transition-colors"
            style={
              dockOpen
                ? {
                    borderColor: `color-mix(in srgb, ${activeAccent} 50%, var(--border-line))`,
                    backgroundColor: `color-mix(in srgb, ${activeAccent} 10%, var(--bg-surface))`,
                  }
                : undefined
            }
          >
            {dockLayout === 'side' ? (
              <PanelRight className="h-3.5 w-3.5 text-muted" />
            ) : (
              <PanelBottom className="h-3.5 w-3.5 text-muted" />
            )}
            <span className="hidden sm:inline">伴读栏</span>
            <Keycap size="xs" tone={dockOpen ? 'accent' : 'default'} domain={activeMeta.domain}>
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
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-soft/70 hover:bg-soft px-2 py-1 text-xs text-ink cursor-pointer transition-colors"
          >
            {theme === 'paper' ? (
              <Sun className="h-3.5 w-3.5 text-muted" />
            ) : (
              <Moon className="h-3.5 w-3.5 text-muted" />
            )}
            <span className="hidden sm:inline font-mono text-[11px]">
              {theme === 'paper' ? 'Paper' : 'Graphite'}
            </span>
            <Keycap size="xs">t</Keycap>
          </FocusItem>
        </div>
      </header>

      {/* Main Body: Left Rail + Workspace Stage + Unmasked F8 Dock */}
      <div className="flex-1 min-h-0 flex overflow-hidden">
        {/* Left Navigation Rail */}
        <aside
          aria-label="五大工作台导航"
          className="w-[70px] md:w-[172px] shrink-0 border-r border-line bg-surface flex flex-col justify-between py-2 px-1.5 md:px-2"
        >
          <nav className="flex flex-col gap-1" aria-label="工作台切换">
            {WORKBENCHES.map((wb) => {
              const Icon = WORKBENCH_ICONS[wb.id];
              const isCurrent = wb.id === activeWorkbench;
              const domainColor = DOMAIN_CSS_VARS[wb.domain];

              return (
                <FocusItem
                  key={wb.id}
                  as="button"
                  id={`nav-workbench-${wb.id}`}
                  zone="nav"
                  label={`${wb.label} (${wb.shortcut})`}
                  priority={20 - wb.index}
                  onActivate={() => onSelectWorkbench(wb.id)}
                  aria-current={isCurrent ? 'page' : undefined}
                  className="group relative w-full rounded-md px-2 py-2 flex flex-col md:flex-row items-center md:justify-between gap-1 md:gap-2 text-left cursor-pointer transition-colors border"
                  style={
                    isCurrent
                      ? {
                          backgroundColor: `color-mix(in srgb, ${domainColor} 10%, var(--bg-soft))`,
                          borderColor: `color-mix(in srgb, ${domainColor} 36%, var(--border-line))`,
                        }
                      : {
                          backgroundColor: 'transparent',
                          borderColor: 'transparent',
                        }
                  }
                >
                  {isCurrent && (
                    <span
                      className="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r-full"
                      style={{ backgroundColor: domainColor }}
                      aria-hidden="true"
                    />
                  )}
                  <span className="flex items-center gap-2 min-w-0">
                    <span
                      className="shrink-0 flex items-center justify-center"
                      style={{ color: isCurrent ? domainColor : 'var(--text-muted)' }}
                    >
                      <Icon className="h-4 w-4" />
                    </span>
                    <span className="hidden md:flex flex-col min-w-0">
                      <span
                        className={`text-xs truncate ${
                          isCurrent ? 'font-semibold text-ink' : 'font-medium text-ink/90'
                        }`}
                      >
                        {wb.label}
                      </span>
                      <span className="text-[10px] text-muted font-mono truncate">
                        {wb.shortLabel}
                      </span>
                    </span>
                  </span>

                  <Keycap
                    size="xs"
                    tone={isCurrent ? 'accent' : 'subtle'}
                    domain={wb.domain}
                    className="shrink-0"
                  >
                    {`Ctrl+${wb.index}`}
                  </Keycap>
                </FocusItem>
              );
            })}
          </nav>

          {/* Compact Theme & Layout Meta Footer */}
          <div className="hidden md:flex flex-col gap-1.5 rounded-md border border-line bg-soft/60 p-2 text-[11px] text-muted">
            <div className="flex items-center justify-between gap-1">
              <span className="font-mono text-[10px] truncate">{palette.label}</span>
              <span className="flex items-center gap-1" aria-hidden="true">
                {(['media', 'text', 'book', 'core'] as const).map((domain) => (
                  <span
                    key={domain}
                    className="h-2 w-2 rounded-full"
                    style={{ backgroundColor: palette.domains[domain] }}
                  />
                ))}
              </span>
            </div>
            <div className="flex items-center justify-between text-[10px]">
              <span>F8 伴读布局</span>
              <span className="font-mono text-ink/80">
                {dockLayout === 'side' ? '右侧分栏' : '底部抽屉'}
              </span>
            </div>
          </div>
        </aside>

        {/* Main Stage */}
        <main className="flex-1 min-w-0 min-h-0 flex flex-col bg-canvas overflow-hidden">
          {children}
        </main>
      </div>

      {dockSlot}

      {/* Bottom Keyboard Legend & Status Bar */}
      <footer className="h-7 shrink-0 border-t border-line bg-surface px-3 flex items-center justify-between gap-3 text-[11px] text-muted select-none">
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="flex items-center gap-1.5 min-w-0">
            <span
              className="h-1.5 w-1.5 rounded-full shrink-0"
              style={{
                backgroundColor: contextEntity
                  ? DOMAIN_CSS_VARS[contextEntity.domain]
                  : activeAccent,
              }}
              aria-hidden="true"
            />
            <span className="truncate text-ink/85">
              {contextEntity ? contextEntity.title : activeMeta.label}
            </span>
          </span>

          <span className="hidden sm:inline text-line">|</span>

          <span className="hidden sm:inline-flex items-center gap-1 font-mono text-[10px] text-muted truncate max-w-[220px]">
            <span>焦点:</span>
            <span className="text-ink/80 truncate">{focusedId ?? 'none'}</span>
          </span>

          <span
            className="inline-flex items-center gap-1 rounded px-1.5 py-[1px] text-[10px] font-medium shrink-0"
            style={
              isComposing
                ? {
                    backgroundColor: 'var(--status-warn-bg)',
                    color: 'var(--status-warn-fg)',
                  }
                : {
                    backgroundColor: 'var(--bg-soft)',
                    color: 'var(--text-muted)',
                  }
            }
          >
            <Keyboard className="h-3 w-3" />
            <span>{isComposing ? 'IME 组合中 · 单键已避让' : 'IME 守卫就绪'}</span>
          </span>
        </div>

        <div className="hidden lg:flex items-center gap-2.5 shrink-0">
          <span className="inline-flex items-center gap-1">
            <Keycap size="xs">hjkl</Keycap>
            <span>/</span>
            <Keycap size="xs">←↓↑→</Keycap>
            <span>导航</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <Keycap size="xs">Enter</Keycap>
            <span>激活</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <Keycap size="xs">f</Keycap>
            <span>提示</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <Keycap size="xs">/</Keycap>
            <span>搜索</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <Keycap size="xs">Ctrl+1..5</Keycap>
            <span>工作台</span>
          </span>
        </div>
      </footer>
    </div>
  );
}
