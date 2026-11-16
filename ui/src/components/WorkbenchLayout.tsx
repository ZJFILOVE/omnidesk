import React, { useCallback, useState } from 'react';

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

export interface WorkbenchViewProps {
  focusId?: string;
  onFocus?: (id: string) => void;
  isActive?: boolean;
  theme?: 'paper' | 'graphite';
  focusedItemId?: string | null;
  searchTrigger?: number;
  navigationPayload?: Record<string, unknown> | null;
  onContextChange?: (ctx: WorkbenchEntityContext | null) => void;
  onOpenDock?: (prompt?: string) => void;
  onNavigateWorkbench?: (
    id: 'media' | 'text' | 'book' | 'ai' | 'store',
    payload?: Record<string, unknown>,
  ) => void;
}

export const DOMAIN_META: Record<
  WorkbenchDomain,
  { label: string; colorVar: string; fallback: string }
> = {
  media: { label: 'Media', colorVar: 'var(--domain-media, #BA345A)', fallback: '#BA345A' },
  text: { label: 'Text', colorVar: 'var(--domain-text, #2260AA)', fallback: '#2260AA' },
  book: { label: 'Book', colorVar: 'var(--domain-book, #1E7244)', fallback: '#1E7244' },
  core: { label: 'Core', colorVar: 'var(--domain-core, #AC5F0C)', fallback: '#AC5F0C' },
};

export const TONE_STYLES: Record<ToneKind, { bg: string; fg: string; border: string }> = {
  neutral: {
    bg: 'var(--bg-soft, #F1EFEC)',
    fg: 'var(--text-primary, #222120)',
    border: 'var(--border-line, rgba(36, 33, 29, 0.12))',
  },
  media: {
    bg: 'var(--bg-active, #F9E7ED)',
    fg: 'var(--domain-media, #BA345A)',
    border: 'var(--domain-media, #BA345A)',
  },
  text: {
    bg: 'var(--status-info-bg, #E8F2FD)',
    fg: 'var(--domain-text, #2260AA)',
    border: 'var(--domain-text, #2260AA)',
  },
  book: {
    bg: 'var(--status-ok-bg, #E7F5ED)',
    fg: 'var(--domain-book, #1E7244)',
    border: 'var(--domain-book, #1E7244)',
  },
  core: {
    bg: 'var(--status-warn-bg, #FCF2DF)',
    fg: 'var(--domain-core, #AC5F0C)',
    border: 'var(--domain-core, #AC5F0C)',
  },
  ok: {
    bg: 'var(--status-ok-bg, #E7F5ED)',
    fg: 'var(--status-ok-fg, #1E7244)',
    border: 'var(--status-ok-fg, #1E7244)',
  },
  warn: {
    bg: 'var(--status-warn-bg, #FCF2DF)',
    fg: 'var(--status-warn-fg, #AC5F0C)',
    border: 'var(--status-warn-fg, #AC5F0C)',
  },
  err: {
    bg: 'var(--status-err-bg, #FCEAEE)',
    fg: 'var(--status-err-fg, #B6263A)',
    border: 'var(--status-err-fg, #B6263A)',
  },
};

export const HAIRLINE_BORDER = 'var(--border-line, rgba(36, 33, 29, 0.12))';

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
  tone?: ToneKind;
  surface?: 'canvas' | 'surface' | 'soft';
  role?: React.AriaRole;
  ariaChecked?: boolean;
  className?: string;
  style?: React.CSSProperties;
  children: React.ReactNode;
}

const SURFACE_BG: Record<'canvas' | 'surface' | 'soft', string> = {
  canvas: 'var(--bg-canvas, #F6F5F3)',
  surface: 'var(--bg-surface, #FCFBF9)',
  soft: 'var(--bg-soft, #F1EFEC)',
};

export const FocusButton: React.FC<FocusButtonProps> = ({
  id,
  focusId,
  activeFocusId,
  onFocus,
  onActivate,
  selected = false,
  tone = 'neutral',
  surface = 'canvas',
  role,
  ariaChecked,
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
      role={role}
      aria-checked={ariaChecked}
      data-focused={isFocused ? 'true' : undefined}
      {...focusableProps(focusId, activeFocusId, onFocus, onActivate)}
      className={focusClass(isFocused, `cursor-pointer rounded-md border transition-colors ${className}`)}
      style={{
        backgroundColor: selected ? toneStyle.bg : SURFACE_BG[surface],
        color: selected ? toneStyle.fg : 'var(--text-primary, #222120)',
        borderColor: selected ? toneStyle.border : HAIRLINE_BORDER,
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
    className={`font-mono text-[11px] font-semibold tracking-wider uppercase ${className}`}
    style={{ color: 'var(--text-muted, #66625D)' }}
  >
    {children}
  </div>
);

export interface WorkbenchLayoutProps {
  domain: WorkbenchDomain;
  title: string;
  subtitle?: string;
  badge?: string;
  headerActions?: React.ReactNode;
  toolbar?: React.ReactNode;
  leftPane?: React.ReactNode;
  leftWidthClass?: string;
  mainPane: React.ReactNode;
  rightPane?: React.ReactNode;
  rightWidthClass?: string;
  footer?: React.ReactNode;
  className?: string;
}

export const WorkbenchLayout: React.FC<WorkbenchLayoutProps> = ({
  domain,
  title,
  subtitle,
  badge,
  headerActions,
  toolbar,
  leftPane,
  leftWidthClass = 'lg:w-[272px]',
  mainPane,
  rightPane,
  rightWidthClass = 'lg:w-[388px]',
  footer,
  className = '',
}) => {
  const meta = DOMAIN_META[domain];

  return (
    <section
      className={`omni-theme-surface flex h-full min-h-0 flex-col overflow-hidden ${className}`}
      style={{
        backgroundColor: 'var(--bg-canvas, #F6F5F3)',
        color: 'var(--text-primary, #222120)',
      }}
    >
      <header
        className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3"
        style={{ backgroundColor: SURFACE_BG.surface, borderColor: HAIRLINE_BORDER }}
      >
        <div className="flex min-w-0 items-center gap-3">
          <span
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-[11px] font-semibold tracking-wide uppercase"
            style={{
              color: meta.colorVar,
              borderColor: HAIRLINE_BORDER,
              backgroundColor: SURFACE_BG.soft,
            }}
          >
            <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: meta.colorVar }} />
            {badge ?? meta.label}
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-[15px] font-semibold tracking-tight">{title}</h1>
            {subtitle ? (
              <p className="truncate text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
                {subtitle}
              </p>
            ) : null}
          </div>
        </div>
        {headerActions ? <div className="flex flex-wrap items-center gap-2">{headerActions}</div> : null}
      </header>

      {toolbar ? (
        <div
          className="flex flex-wrap items-center justify-between gap-2 border-b px-5 py-2"
          style={{ backgroundColor: SURFACE_BG.soft, borderColor: HAIRLINE_BORDER }}
        >
          {toolbar}
        </div>
      ) : null}

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden lg:flex-row">
        {leftPane ? (
          <aside
            className={`flex min-h-0 shrink-0 flex-col overflow-y-auto border-b lg:border-r lg:border-b-0 ${leftWidthClass}`}
            style={{ backgroundColor: SURFACE_BG.surface, borderColor: HAIRLINE_BORDER }}
          >
            {leftPane}
          </aside>
        ) : null}

        <main
          className="min-h-0 min-w-0 flex-1 overflow-y-auto"
          style={{ backgroundColor: SURFACE_BG.canvas }}
        >
          {mainPane}
        </main>

        {rightPane ? (
          <aside
            className={`flex min-h-0 shrink-0 flex-col overflow-y-auto border-t lg:border-t-0 lg:border-l ${rightWidthClass}`}
            style={{ backgroundColor: SURFACE_BG.surface, borderColor: HAIRLINE_BORDER }}
          >
            {rightPane}
          </aside>
        ) : null}
      </div>

      {footer ? (
        <footer
          className="flex flex-wrap items-center justify-between gap-2 border-t px-5 py-1.5 font-mono text-[11px]"
          style={{
            backgroundColor: SURFACE_BG.surface,
            borderColor: HAIRLINE_BORDER,
            color: 'var(--text-muted, #66625D)',
          }}
        >
          {footer}
        </footer>
      ) : null}
    </section>
  );
};

export default WorkbenchLayout;
