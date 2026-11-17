import type { ReactNode } from 'react';
import { DOMAIN_CSS_VARS } from '../theme';
import type { ContentDomain } from '../types';

export interface KeycapProps {
  children?: ReactNode;
  combo?: string;
  keys?: readonly string[];
  split?: boolean;
  size?: 'xs' | 'sm' | 'md';
  tone?: 'default' | 'accent' | 'subtle';
  domain?: ContentDomain;
  className?: string;
}

const SIZE_CLASSES: Record<NonNullable<KeycapProps['size']>, string> = {
  xs: 'px-1.5 py-[2px] text-[12.5px] leading-none rounded-[5px] min-w-[19px] h-[19px]',
  sm: 'px-1.5 py-[2px] text-[13px] leading-none rounded-[5px] min-w-[21px] h-[21px]',
  md: 'px-2 py-1 text-[13.5px] leading-none rounded-[6px] min-w-[24px] h-[24px]',
};

export function Keycap({
  children,
  combo,
  keys,
  split = false,
  size = 'sm',
  tone = 'default',
  domain,
  className = '',
}: KeycapProps) {
  const accentColor = domain ? DOMAIN_CSS_VARS[domain] : 'var(--focus-ring)';
  const sizeClass = SIZE_CLASSES[size];

  const toneStyle =
    tone === 'accent'
      ? {
          borderColor: `color-mix(in srgb, ${accentColor} 45%, var(--keycap-border))`,
          color: accentColor,
          backgroundColor: `color-mix(in srgb, ${accentColor} 11%, var(--keycap-bg))`,
        }
      : undefined;

  const toneClass = tone === 'subtle' ? 'opacity-80' : '';

  const parts =
    keys && keys.length > 0
      ? keys
      : combo && split
        ? combo
            .split('+')
            .map((part) => part.trim())
            .filter(Boolean)
        : null;

  if (parts && parts.length > 1) {
    return (
      <span className={`inline-flex items-center gap-0.5 ${className}`.trim()}>
        {parts.map((part, idx) => (
          <kbd
            key={`${part}-${idx}`}
            style={toneStyle}
            className={`omni-keycap ${sizeClass} ${toneClass}`.trim()}
          >
            {part}
          </kbd>
        ))}
      </span>
    );
  }

  return (
    <kbd
      style={toneStyle}
      className={`omni-keycap ${sizeClass} ${toneClass} ${className}`.trim()}
    >
      {children ?? combo ?? parts?.[0] ?? ''}
    </kbd>
  );
}
