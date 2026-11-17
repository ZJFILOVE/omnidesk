import React, { useCallback, useState } from 'react';
import type { BusEventEnvelope, RuntimeStatus } from '../api';
import type { PluginHealthEntry } from '../hooks/useCoreEvents';

export type WorkbenchDomain = 'media' | 'text' | 'book' | 'core';
export type ToneKind = 'neutral' | 'media' | 'text' | 'book' | 'core' | 'ok' | 'warn' | 'err';

export interface WorkbenchEntityContext {
  domain: WorkbenchDomain;
  workbench: 'media' | 'text' | 'book' | 'ai' | 'store';
  itemId: string;
  title: string;
  subtitle?: string;
  sourceId?: string;
  kind?: string;
  url?: string;
  extra?: Record<string, unknown>;
}

export interface VocabAddPayload {
  word: string;
  contextSentence?: string;
  sourceTitle?: string;
}

export interface WorkbenchViewProps {
  focusId?: string;
  onFocus?: (id: string) => void;
  isActive?: boolean;
  theme?: 'paper' | 'graphite';
  focusedItemId?: string | null;
  searchTrigger?: number;
  mediaToggleTrigger?: number;
  tauriAvailable?: boolean;
  coreOnline?: boolean;
  runtimeStatus?: RuntimeStatus | null;
  pluginHealthMap?: Record<string, PluginHealthEntry>;
  lastBusEvent?: BusEventEnvelope | null;
  navigationPayload?: Record<string, unknown> | null;
  onContextChange?: (ctx: WorkbenchEntityContext | null) => void;
  onOpenDock?: (prompt?: string) => void;
  onAddVocab?: (payload: VocabAddPayload) => void;
  sharedVocab?: readonly VocabAddPayload[];
  onNavigateWorkbench?: (
    id: 'media' | 'text' | 'book' | 'ai' | 'store',
    payload?: Record<string, unknown>,
  ) => void;
}

export const DOMAIN_META: Record<
  WorkbenchDomain,
  { label: string; colorVar: string; fallback: string }
> = {
  media: { label: 'Media', colorVar: 'var(--domain-media, #A41F48)', fallback: '#A41F48' },
  text: { label: 'Text', colorVar: 'var(--domain-text, #185194)', fallback: '#185194' },
  book: { label: 'Book', colorVar: 'var(--domain-book, #165E37)', fallback: '#165E37' },
  core: { label: 'Core', colorVar: 'var(--domain-core, #8F4906)', fallback: '#8F4906' },
};

export const TONE_STYLES: Record<ToneKind, { bg: string; fg: string; border: string }> = {
  neutral: {
    bg: 'var(--bg-soft, #E6E2DA)',
    fg: 'var(--text-primary, #1C1B19)',
    border: 'var(--border-line, rgba(28, 27, 25, 0.09))',
  },
  media: {
    bg: 'color-mix(in srgb, var(--domain-media, #A41F48) 12%, var(--bg-surface, #FCFBF9))',
    fg: 'var(--domain-media, #A41F48)',
    border: 'color-mix(in srgb, var(--domain-media, #A41F48) 28%, transparent)',
  },
  text: {
    bg: 'color-mix(in srgb, var(--domain-text, #185194) 11%, var(--bg-surface, #FCFBF9))',
    fg: 'var(--domain-text, #185194)',
    border: 'color-mix(in srgb, var(--domain-text, #185194) 26%, transparent)',
  },
  book: {
    bg: 'color-mix(in srgb, var(--domain-book, #165E37) 11%, var(--bg-surface, #FCFBF9))',
    fg: 'var(--domain-book, #165E37)',
    border: 'color-mix(in srgb, var(--domain-book, #165E37) 26%, transparent)',
  },
  core: {
    bg: 'color-mix(in srgb, var(--domain-core, #8F4906) 12%, var(--bg-surface, #FCFBF9))',
    fg: 'var(--domain-core, #8F4906)',
    border: 'color-mix(in srgb, var(--domain-core, #8F4906) 28%, transparent)',
  },
  ok: {
    bg: 'var(--status-ok-bg, #E7F3EC)',
    fg: 'var(--status-ok-fg, #165E37)',
    border: 'color-mix(in srgb, var(--status-ok-fg, #165E37) 26%, transparent)',
  },
  warn: {
    bg: 'var(--status-warn-bg, #F9EFDA)',
    fg: 'var(--status-warn-fg, #874605)',
    border: 'color-mix(in srgb, var(--status-warn-fg, #874605) 26%, transparent)',
  },
  err: {
    bg: 'var(--status-err-bg, #FCE9ED)',
    fg: 'var(--status-err-fg, #AA1D33)',
    border: 'color-mix(in srgb, var(--status-err-fg, #AA1D33) 26%, transparent)',
  },
};

export const HAIRLINE_BORDER = 'var(--border-subtle, rgba(28, 27, 25, 0.055))';

export function focusClass(isFocused: boolean, baseClass = ''): string {
  const focusTokens = isFocused ? 'is-focused omni-focus-ring' : '';
  return baseClass ? (focusTokens ? `${baseClass} ${focusTokens}` : baseClass) : focusTokens;
}

export function isImeComposing(event: React.KeyboardEvent<HTMLElement>): boolean {
  return Boolean(event.nativeEvent?.isComposing || event.key === 'Process' || event.keyCode === 229);
}

export function useWorkbenchFocus(
  defaultFocusId: string,
  focusId?: string,
  onFocusChange?: (id: string) => void,
  focusedItemId?: string | null,
): readonly [string, (id: string) => void] {
  const [localFocusId, setLocalFocusId] = useState<string>(defaultFocusId);
  const isControlled = focusId !== undefined || focusedItemId !== undefined;
  const activeFocusId =
    focusId !== undefined
      ? focusId
      : focusedItemId !== undefined
        ? (focusedItemId ?? '')
        : localFocusId;

  const emitFocus = useCallback(
    (id: string) => {
      if (!isControlled) {
        setLocalFocusId(id);
      }
      onFocusChange?.(id);
    },
    [isControlled, onFocusChange],
  );
  return [activeFocusId, emitFocus] as const;
}

export interface FocusableAttrs {
  'data-focus-id': string;
  tabIndex: -1;
  onClick: () => void;
  onFocus: (event: React.FocusEvent<HTMLElement>) => void;
  onKeyDown: (event: React.KeyboardEvent<HTMLElement>) => void;
}

export function focusableProps(
  id: string,
  activeFocusId: string | undefined,
  onFocusChange?: (id: string) => void,
  onActivate?: () => void,
): FocusableAttrs {
  return {
    'data-focus-id': id,
    tabIndex: -1,
    onClick: () => {
      onFocusChange?.(id);
      onActivate?.();
    },
    onFocus: (event) => {
      if (event.target === event.currentTarget && id !== activeFocusId) {
        onFocusChange?.(id);
      }
    },
    onKeyDown: (event) => {
      if (isImeComposing(event)) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onFocusChange?.(id);
        onActivate?.();
      }
    },
  };
}

export interface FocusableInputAttrs {
  'data-focus-id': string;
  tabIndex: -1;
  onClick: () => void;
  onFocus: (event: React.FocusEvent<HTMLElement>) => void;
}

export function focusableInputProps(
  id: string,
  activeFocusId: string | undefined,
  onFocusChange?: (id: string) => void,
): FocusableInputAttrs {
  return {
    'data-focus-id': id,
    tabIndex: -1,
    onClick: () => {
      if (id !== activeFocusId) {
        onFocusChange?.(id);
      }
    },
    onFocus: (event) => {
      if (event.target === event.currentTarget && id !== activeFocusId) {
        onFocusChange?.(id);
      }
    },
  };
}

export interface FocusButtonProps {
  id?: string;
  focusId: string;
  activeFocusId: string;
  onFocus?: (id: string) => void;
  onActivate?: () => void;
  selected?: boolean;
  disabled?: boolean;
  tone?: ToneKind;
  surface?: 'canvas' | 'surface' | 'soft' | 'ghost';
  role?: React.AriaRole;
  ariaChecked?: boolean;
  ariaLabel?: string;
  ariaPressed?: boolean;
  className?: string;
  style?: React.CSSProperties;
  children: React.ReactNode;
}

const SURFACE_BG: Record<'canvas' | 'surface' | 'soft' | 'ghost', string> = {
  canvas: 'var(--bg-canvas, #F6F5F3)',
  surface: 'var(--bg-surface, #FCFBF9)',
  soft: 'var(--bg-soft, #E6E2DA)',
  ghost: 'transparent',
};

export const FocusButton: React.FC<FocusButtonProps> = ({
  id,
  focusId,
  activeFocusId,
  onFocus,
  onActivate,
  selected = false,
  disabled = false,
  tone = 'neutral',
  surface = 'soft',
  role,
  ariaChecked,
  ariaLabel,
  ariaPressed,
  className = '',
  style,
  children,
}) => {
  const isFocused = activeFocusId === focusId;
  const toneStyle = TONE_STYLES[tone];

  return (
    <button
      id={id}
      type="button"
      disabled={disabled}
      role={role}
      aria-label={ariaLabel}
      aria-checked={ariaChecked}
      aria-pressed={ariaPressed ?? (role ? undefined : selected)}
      data-focused={isFocused ? 'true' : undefined}
      {...focusableProps(focusId, activeFocusId, onFocus, disabled ? undefined : onActivate)}
      className={focusClass(
        isFocused,
        `rounded-[8px] transition-colors ${
          disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer hover:brightness-[0.97]'
        } ${className}`,
      )}
      style={{
        backgroundColor: selected ? toneStyle.bg : SURFACE_BG[surface],
        color: selected ? toneStyle.fg : 'var(--text-primary, #1C1B19)',
        boxShadow: selected ? `inset 0 0 0 1px ${toneStyle.border}` : undefined,
        ...style,
      }}
    >
      {children}
    </button>
  );
};

export const SectionLabel: React.FC<{ children: React.ReactNode; className?: string }> = ({
  children,
  className = '',
}) => (
  <div
    className={`text-[13.5px] font-semibold tracking-[0.015em] ${className}`}
    style={{ color: 'var(--text-muted, #59544D)' }}
  >
    {children}
  </div>
);

export interface WorkbenchLayoutProps {
  domain: WorkbenchDomain;
  title: string;
  subtitle?: string;
  badge?: string;
  editorialHeader?: boolean;
  headerActions?: React.ReactNode;
  toolbar?: React.ReactNode;
  leftPane?: React.ReactNode;
  leftWidthClass?: string;
  mainPane: React.ReactNode;
  rightPane?: React.ReactNode;
  rightWidthClass?: string;
  footer?: React.ReactNode;
  testId?: string;
  className?: string;
}

export const WorkbenchLayout: React.FC<WorkbenchLayoutProps> = ({
  domain,
  title,
  subtitle,
  badge,
  editorialHeader = false,
  headerActions,
  toolbar,
  leftPane,
  leftWidthClass = '@[860px]/workbench:w-[256px]',
  mainPane,
  rightPane,
  rightWidthClass = '@[860px]/workbench:w-[336px]',
  footer,
  testId,
  className = '',
}) => {
  const meta = DOMAIN_META[domain];

  return (
    <section
      data-testid={testId}
      className={`omni-theme-surface @container/workbench flex h-full min-h-0 flex-col overflow-hidden ${className}`}
      style={{
        backgroundColor: 'var(--bg-canvas, #F6F5F3)',
        color: 'var(--text-primary, #1C1B19)',
      }}
    >
      {/* Unified Quiet Workbench Header + Optional Integrated Toolbar */}
      <div className="shrink-0 border-b" style={{ borderColor: HAIRLINE_BORDER }}>
        <header className="flex flex-wrap items-center justify-between gap-4 px-6 pt-4 pb-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2.5">
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: meta.colorVar }}
                aria-hidden="true"
              />
              <h1
                className={`truncate text-[22px] leading-snug font-bold tracking-tight ${
                  editorialHeader ? 'omni-editorial-title text-[24px]' : ''
                }`}
              >
                {title}
              </h1>
              {badge ? (
                <span
                  className="hidden sm:inline-flex shrink-0 items-center rounded-full px-2.5 py-0.5 font-mono text-[13.5px] font-medium"
                  style={{
                    backgroundColor: 'var(--bg-soft, #E6E2DA)',
                    color: 'var(--text-muted, #59544D)',
                  }}
                >
                  {badge}
                </span>
              ) : null}
            </div>
            {subtitle ? (
              <p
                className="mt-0.5 truncate pl-4.5 text-[14.5px]"
                style={{ color: 'var(--text-muted, #59544D)' }}
              >
                {subtitle}
              </p>
            ) : null}
          </div>

          {headerActions ? (
            <div className="flex flex-wrap items-center gap-2">{headerActions}</div>
          ) : null}
        </header>

        {toolbar ? (
          <div className="flex flex-wrap items-center justify-between gap-3 px-6 pb-3">
            {toolbar}
          </div>
        ) : null}
      </div>

      {/* Responsive Split Stage: scrolls cleanly when stacked (<860px, e.g. F8 dock open at 1280x800) */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto @[860px]/workbench:flex-row @[860px]/workbench:overflow-hidden">
        {leftPane ? (
          <aside
            className={`flex w-full shrink-0 flex-col border-b @[860px]/workbench:min-h-0 @[860px]/workbench:overflow-y-auto @[860px]/workbench:border-r @[860px]/workbench:border-b-0 ${leftWidthClass}`}
            style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
          >
            {leftPane}
          </aside>
        ) : null}

        <main
          className="flex min-h-[440px] min-w-0 shrink-0 flex-col @[860px]/workbench:min-h-0 @[860px]/workbench:flex-1 @[860px]/workbench:shrink @[860px]/workbench:overflow-y-auto"
          style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)' }}
        >
          {mainPane}
        </main>

        {rightPane ? (
          <aside
            className={`flex w-full shrink-0 flex-col border-t @[860px]/workbench:min-h-0 @[860px]/workbench:overflow-y-auto @[860px]/workbench:border-t-0 @[860px]/workbench:border-l ${rightWidthClass}`}
            style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
          >
            {rightPane}
          </aside>
        ) : null}
      </div>

      {footer ? (
        <footer
          className="flex flex-wrap items-center justify-between gap-2 border-t px-6 py-1.5 font-mono text-[13.5px]"
          style={{
            backgroundColor: 'var(--bg-surface, #FCFBF9)',
            borderColor: HAIRLINE_BORDER,
            color: 'var(--text-muted, #59544D)',
          }}
        >
          {footer}
        </footer>
      ) : null}
    </section>
  );
};

export default WorkbenchLayout;
