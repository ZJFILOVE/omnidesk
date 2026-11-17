import React from 'react';
import { Search, X } from 'lucide-react';
import {
  DOMAIN_META,
  FocusButton,
  TONE_STYLES,
  focusClass,
  focusableInputProps,
  focusableProps,
  type ToneKind,
  type WorkbenchDomain,
} from './WorkbenchLayout';

export interface SearchInputProps {
  focusId: string;
  activeFocusId: string;
  onFocus: (id: string) => void;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
  inputRef?: React.RefObject<HTMLInputElement | null>;
  widthClass?: string;
  className?: string;
}

export const SearchInput: React.FC<SearchInputProps> = ({
  focusId,
  activeFocusId,
  onFocus,
  value,
  onChange,
  placeholder,
  ariaLabel,
  inputRef,
  widthClass = 'w-56',
  className = '',
}) => {
  const isFocused = activeFocusId === focusId;

  return (
    <div className={`relative flex items-center ${widthClass} ${className}`}>
      <Search
        className="pointer-events-none absolute left-3 h-3.5 w-3.5"
        style={{ color: 'var(--text-subtle)' }}
        aria-hidden="true"
      />
      <input
        ref={inputRef}
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        data-focused={isFocused ? 'true' : undefined}
        {...focusableInputProps(focusId, activeFocusId, onFocus)}
        className={focusClass(
          isFocused,
          'w-full rounded-[9px] border py-1.5 pr-8 pl-8.5 text-[14.5px] placeholder:text-[var(--text-subtle)]',
        )}
        style={{
          backgroundColor: 'var(--bg-surface)',
          borderColor: 'var(--border-line)',
          color: 'var(--text-primary)',
        }}
      />
      {value ? (
        <button
          type="button"
          onClick={() => onChange('')}
          aria-label={`清空${ariaLabel}`}
          title={`清空${ariaLabel}`}
          className="absolute right-1.5 inline-flex h-6 w-6 cursor-pointer items-center justify-center rounded-[6px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
};

export interface PillOption<T extends string> {
  id: T;
  label: string;
  count?: number;
}

export interface SegmentedPillsProps<T extends string> {
  options: readonly PillOption<T>[];
  value: T;
  onChange: (value: T) => void;
  focusPrefix: string;
  activeFocusId: string;
  onFocus: (id: string) => void;
  tone?: ToneKind;
  ariaLabel?: string;
  className?: string;
}

export function SegmentedPills<T extends string>({
  options,
  value,
  onChange,
  focusPrefix,
  activeFocusId,
  onFocus,
  tone = 'neutral',
  ariaLabel,
  className = '',
}: SegmentedPillsProps<T>) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={`inline-flex flex-wrap items-center gap-1 rounded-[10px] p-1 ${className}`}
      style={{ backgroundColor: 'var(--bg-sunken)' }}
    >
      {options.map((opt) => {
        const selected = opt.id === value;
        return (
          <FocusButton
            key={opt.id}
            focusId={`${focusPrefix}${opt.id}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onChange(opt.id)}
            selected={selected}
            tone={tone}
            surface={selected ? 'surface' : 'ghost'}
            className="px-3 py-1 text-[14px] font-medium"
          >
            <span>{opt.label}</span>
            {typeof opt.count === 'number' ? (
              <span className="ml-1.5 font-mono text-[13.5px] opacity-75">{opt.count}</span>
            ) : null}
          </FocusButton>
        );
      })}
    </div>
  );
}

export const TagChip: React.FC<{
  children: React.ReactNode;
  tone?: ToneKind;
  mono?: boolean;
  className?: string;
}> = ({ children, tone = 'neutral', mono = false, className = '' }) => {
  const s = TONE_STYLES[tone];
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[13.5px] font-medium ${
        mono ? 'font-mono' : ''
      } ${className}`}
      style={{ backgroundColor: s.bg, color: s.fg }}
    >
      {children}
    </span>
  );
};

export const PanelSectionHeading: React.FC<{
  title: string;
  meta?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}> = ({ title, meta, action, className = '' }) => (
  <div className={`flex items-center justify-between gap-2 ${className}`}>
    <div className="flex min-w-0 items-baseline gap-2">
      <h2 className="truncate text-[15.5px] font-semibold text-[var(--text-primary)]">{title}</h2>
      {meta ? (
        <span className="shrink-0 font-mono text-[13.5px] text-[var(--text-muted)]">{meta}</span>
      ) : null}
    </div>
    {action ? <div className="shrink-0">{action}</div> : null}
  </div>
);

export interface SelectableRowProps {
  focusId: string;
  activeFocusId: string;
  selected: boolean;
  domain: WorkbenchDomain;
  onFocus: (id: string) => void;
  onActivate: () => void;
  title: string;
  meta?: React.ReactNode;
  eyebrow?: React.ReactNode;
  leading?: React.ReactNode;
  trailing?: React.ReactNode;
  className?: string;
}

/**
 * Shared tonal list/directory/queue item across workbenches.
 * Uses left domain accent bar + surface tint + omni-focus-row instead of bordered cards.
 */
export const SelectableRow: React.FC<SelectableRowProps> = ({
  focusId,
  activeFocusId,
  selected,
  domain,
  onFocus,
  onActivate,
  title,
  meta,
  eyebrow,
  leading,
  trailing,
  className = '',
}) => {
  const isFocused = activeFocusId === focusId;
  const domainColor = DOMAIN_META[domain].colorVar;

  return (
    <article
      role="button"
      aria-pressed={selected}
      data-focused={isFocused ? 'true' : undefined}
      {...focusableProps(focusId, activeFocusId, onFocus, onActivate)}
      className={focusClass(
        isFocused,
        `omni-focus-row group relative flex cursor-pointer items-center gap-3 rounded-[10px] px-3 py-2.5 text-left transition-colors hover:bg-[var(--hover-fill)] ${className}`,
      )}
      style={{
        backgroundColor: selected
          ? `color-mix(in srgb, ${domainColor} 10%, var(--bg-surface))`
          : 'transparent',
      }}
    >
      {selected ? (
        <span
          aria-hidden="true"
          className="absolute top-2.5 bottom-2.5 left-0 w-[3px] rounded-r-full"
          style={{ backgroundColor: domainColor }}
        />
      ) : null}

      {leading ? <div className="shrink-0">{leading}</div> : null}

      <div className="min-w-0 flex-1">
        {eyebrow ? (
          <div className="mb-0.5 flex items-center justify-between gap-2 text-[13.5px]">
            {eyebrow}
          </div>
        ) : null}
        <h3 className="line-clamp-2 text-[15px] leading-[1.42] font-semibold text-[var(--text-primary)]">
          {title}
        </h3>
        {meta ? (
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[13.5px] text-[var(--text-muted)]">
            {meta}
          </div>
        ) : null}
      </div>

      {trailing ? <div className="shrink-0">{trailing}</div> : null}
    </article>
  );
};

export const EditorialCallout: React.FC<{
  domain?: WorkbenchDomain;
  title?: string;
  children: React.ReactNode;
  className?: string;
}> = ({ domain = 'text', title, children, className = '' }) => {
  const accent = DOMAIN_META[domain].colorVar;
  return (
    <div
      className={`border-l-[3px] py-1.5 pl-4 text-[16px] leading-[1.75] ${className}`}
      style={{
        borderColor: accent,
        backgroundColor: 'color-mix(in srgb, var(--bg-surface) 65%, transparent)',
        color: 'var(--text-secondary)',
      }}
    >
      {title ? (
        <div className="mb-1 text-[14.5px] font-semibold text-[var(--text-primary)]">{title}</div>
      ) : null}
      {children}
    </div>
  );
};
