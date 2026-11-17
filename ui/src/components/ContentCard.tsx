import React from 'react';
import { focusClass, focusableProps } from './WorkbenchLayout';

export type CardBadgeTone = 'ok' | 'core' | 'media' | 'text' | 'book' | 'neutral';

export interface ContentCardProps {
  focusId: string;
  activeFocusId?: string;
  focused?: boolean;
  selected?: boolean;
  onFocus?: (id: string) => void;
  onActivate?: (id: string) => void;
  title: string;
  subtitle?: string;
  description?: string;
  badge?: string;
  badgeTone?: CardBadgeTone;
  secondaryBadge?: string;
  meta?: string;
  coverGradient?: string;
  coverTag?: string;
  coverOverlay?: React.ReactNode;
  aspect?: '16:9' | '1:1' | '2:3' | 'none';
  progressPct?: number;
  footer?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}

const BADGE_STYLES: Record<CardBadgeTone, { bg: string; fg: string }> = {
  ok: { bg: 'var(--status-ok-bg, #E7F5ED)', fg: 'var(--status-ok-fg, #1E7244)' },
  core: { bg: 'var(--status-warn-bg, #FCF2DF)', fg: 'var(--status-warn-fg, #AC5F0C)' },
  media: { bg: 'var(--bg-active, #F9E7ED)', fg: 'var(--domain-media, #BA345A)' },
  text: { bg: 'var(--status-info-bg, #E8F2FD)', fg: 'var(--domain-text, #2260AA)' },
  book: { bg: 'var(--status-ok-bg, #E7F5ED)', fg: 'var(--domain-book, #1E7244)' },
  neutral: { bg: 'var(--bg-soft, #F1EFEC)', fg: 'var(--text-muted, #66625D)' },
};

const ASPECT_RATIO_CSS: Record<Exclude<ContentCardProps['aspect'], undefined | 'none'>, string> = {
  '16:9': '16 / 9',
  '1:1': '1 / 1',
  '2:3': '2 / 3',
};

export const ContentCard: React.FC<ContentCardProps> = ({
  focusId,
  activeFocusId,
  focused,
  selected = false,
  onFocus,
  onActivate,
  title,
  subtitle,
  description,
  badge,
  badgeTone = 'neutral',
  secondaryBadge,
  meta,
  coverGradient,
  coverTag,
  coverOverlay,
  aspect = '16:9',
  progressPct,
  footer,
  children,
  className = '',
}) => {
  const isFocused = focused ?? activeFocusId === focusId;
  const badgeStyle = BADGE_STYLES[badgeTone];

  return (
    <article
      role="button"
      aria-pressed={selected}
      data-focused={isFocused ? 'true' : undefined}
      {...focusableProps(
        focusId,
        activeFocusId,
        onFocus,
        onActivate ? () => onActivate(focusId) : undefined,
      )}
      className={focusClass(
        isFocused,
        `group flex cursor-pointer flex-col overflow-hidden rounded-[14px] text-left transition-colors ${className}`,
      )}
      style={{
        backgroundColor: selected
          ? 'var(--bg-soft, #E6E2DA)'
          : 'var(--bg-surface, #FCFBF9)',
        boxShadow: 'var(--shadow-page)',
      }}
    >
      {aspect !== 'none' ? (
        <div
          className={`relative w-full overflow-hidden ${
            aspect === '16:9' ? 'aspect-video' : aspect === '1:1' ? 'aspect-square' : ''
          }`}
          style={{
            aspectRatio: ASPECT_RATIO_CSS[aspect],
            background:
              coverGradient ??
              'linear-gradient(135deg, #2B3A4A 0%, #4E4237 60%, #7C304E 100%)',
          }}
        >
          <div
            className="absolute inset-0 opacity-20"
            style={{
              backgroundImage:
                'radial-gradient(circle at 80% 20%, rgba(255,255,255,0.45), transparent 55%)',
            }}
          />

          <div className="relative flex h-full flex-col justify-between p-3 text-white">
            <div className="flex items-start justify-between gap-2">
              {coverTag ? (
                <span className="rounded bg-black/50 px-2 py-0.5 font-mono text-[11.5px] font-medium tracking-wide backdrop-blur-xs">
                  {coverTag}
                </span>
              ) : (
                <span />
              )}
              {meta ? (
                <span className="rounded bg-black/60 px-2 py-0.5 font-mono text-[11.5px] font-semibold">
                  {meta}
                </span>
              ) : null}
            </div>

            {coverOverlay ? <div className="mt-auto">{coverOverlay}</div> : null}
          </div>

          {typeof progressPct === 'number' ? (
            <div className="absolute right-0 bottom-0 left-0 h-1 bg-black/35">
              <div
                className="h-full bg-white/90"
                style={{ width: `${Math.max(0, Math.min(100, progressPct))}%` }}
              />
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-1 flex-col gap-1.5 p-3.5">
        {(badge || secondaryBadge) && (
          <div className="flex flex-wrap items-center gap-1.5">
            {badge ? (
              <span
                className="inline-flex items-center rounded px-1.5 py-0.5 text-[11.5px] font-medium"
                style={{ backgroundColor: badgeStyle.bg, color: badgeStyle.fg }}
              >
                {badge}
              </span>
            ) : null}
            {secondaryBadge ? (
              <span
                className="inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[11.5px]"
                style={{
                  backgroundColor: 'var(--bg-soft, #F1EFEC)',
                  color: 'var(--text-muted, #66625D)',
                }}
              >
                {secondaryBadge}
              </span>
            ) : null}
          </div>
        )}

        <h3 className="line-clamp-2 text-[15px] leading-snug font-semibold">
          {title}
        </h3>

        {subtitle ? (
          <p
            className="line-clamp-1 text-xs"
            style={{ color: 'var(--text-muted, #66625D)' }}
          >
            {subtitle}
          </p>
        ) : null}

        {description ? (
          <p
            className="line-clamp-2 text-xs leading-relaxed"
            style={{ color: 'var(--text-subtle, #8A847D)' }}
          >
            {description}
          </p>
        ) : null}

        {children}

        {footer ? (
          <div
            className="mt-auto flex items-center justify-between gap-2 border-t pt-2 text-[12.5px]"
            style={{
              borderColor: 'var(--border-line, rgba(36, 33, 29, 0.12))',
              color: 'var(--text-muted, #66625D)',
            }}
          >
            {footer}
          </div>
        ) : null}
      </div>
    </article>
  );
};

export default ContentCard;
