import { useEffect, useMemo, useState } from 'react';
import { Crosshair, X } from 'lucide-react';
import { useImeGuard } from '../hooks/useImeGuard';

export interface HintOverlayTarget {
  id: string;
  label: string;
  x: number;
  y: number;
}

export interface HintOverlayProps {
  open: boolean;
  targets: HintOverlayTarget[];
  onPick: (id: string) => void;
  onClose: () => void;
}

interface AssignedHint {
  target: HintOverlayTarget;
  code: string;
  showLabel: boolean;
}

const HINT_ALPHABET = 'ASDFJKLGHQWERUIOPZXCVBNM';
const SHORT_CODE_RE = /^[A-Za-z0-9]{1,2}$/;

export function buildHintCodes(count: number): string[] {
  if (count <= 0) {
    return [];
  }
  const len = HINT_ALPHABET.length;
  if (count <= len) {
    return Array.from({ length: count }, (_, idx) => HINT_ALPHABET[idx]);
  }

  let prefixReserve = 1;
  while (len - prefixReserve + prefixReserve * len < count && prefixReserve < len) {
    prefixReserve += 1;
  }

  const singleCount = Math.max(0, len - prefixReserve);
  const codes: string[] = [];

  for (let i = 0; i < singleCount && codes.length < count; i += 1) {
    codes.push(HINT_ALPHABET[i]);
  }
  for (let p = singleCount; p < len && codes.length < count; p += 1) {
    const prefix = HINT_ALPHABET[p];
    for (let s = 0; s < len && codes.length < count; s += 1) {
      codes.push(`${prefix}${HINT_ALPHABET[s]}`);
    }
  }
  return codes;
}

function assignHints(targets: HintOverlayTarget[]): AssignedHint[] {
  if (targets.length === 0) {
    return [];
  }

  const normalizedLabels = targets.map((t) => t.label.trim().toUpperCase());
  const allValidShort = normalizedLabels.every((lbl) => SHORT_CODE_RE.test(lbl));
  const uniqueShort = new Set(normalizedLabels).size === normalizedLabels.length;

  const prefixFree =
    allValidShort &&
    uniqueShort &&
    normalizedLabels.every((code, idx) =>
      normalizedLabels.every((other, otherIdx) => idx === otherIdx || !other.startsWith(code)),
    );

  if (prefixFree) {
    return targets.map((target, idx) => ({
      target,
      code: normalizedLabels[idx],
      showLabel: false,
    }));
  }

  const generated = buildHintCodes(targets.length);
  return targets.map((target, idx) => {
    const code = generated[idx] ?? String(idx + 1);
    const trimmed = target.label.trim();
    return {
      target,
      code,
      showLabel: trimmed.length > 0 && trimmed.toUpperCase() !== code,
    };
  });
}

export function HintOverlay({ open, targets, onPick, onClose }: HintOverlayProps) {
  const [typed, setTyped] = useState('');
  const ime = useImeGuard();

  const assigned = useMemo(() => assignHints(targets), [targets]);

  useEffect(() => {
    setTyped('');
  }, [open, targets]);

  useEffect(() => {
    if (!open || typeof window === 'undefined') {
      return undefined;
    }

    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (ime.shouldIgnoreShortcut(event)) {
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        setTyped('');
        onClose();
        return;
      }

      if (event.key === 'Backspace') {
        event.preventDefault();
        event.stopPropagation();
        setTyped((prev) => prev.slice(0, -1));
        return;
      }

      if (event.ctrlKey || event.metaKey || event.altKey) {
        return;
      }

      if (!/^[a-zA-Z0-9]$/.test(event.key)) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();

      const ch = event.key.toUpperCase();
      const candidate = `${typed}${ch}`;

      const exact = assigned.find((item) => item.code === candidate);
      if (exact) {
        setTyped('');
        onPick(exact.target.id);
        return;
      }

      const hasPrefix = assigned.some((item) => item.code.startsWith(candidate));
      if (hasPrefix) {
        setTyped(candidate);
        return;
      }

      if (typed.length > 0) {
        const fallbackExact = assigned.find((item) => item.code === ch);
        if (fallbackExact) {
          setTyped('');
          onPick(fallbackExact.target.id);
          return;
        }
        if (assigned.some((item) => item.code.startsWith(ch))) {
          setTyped(ch);
          return;
        }
      }

      setTyped('');
    };

    window.addEventListener('keydown', handleKeyDown, true);
    return () => {
      window.removeEventListener('keydown', handleKeyDown, true);
    };
  }, [open, typed, assigned, onPick, onClose, ime]);

  if (!open) {
    return null;
  }

  const matchingCount = assigned.filter((item) => item.code.startsWith(typed)).length;

  return (
    <div
      role="dialog"
      aria-modal="false"
      aria-label="全屏字母闪跳导航"
      data-testid="hint-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) {
          setTyped('');
          onClose();
        }
      }}
      className="fixed inset-0 z-40 bg-[var(--backdrop-scrim)]/45"
    >
      <div
        style={{ boxShadow: 'var(--shadow-elevated)' }}
        className="pointer-events-none fixed top-3 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2.5 rounded-full border border-[var(--border-strong)] bg-[var(--bg-elevated)] px-4 py-1.5 text-[14px] text-[var(--text-primary)]"
      >
        <Crosshair className="h-3.5 w-3.5 text-[var(--focus-ring)]" aria-hidden="true" />
        <span className="font-medium">字母闪跳</span>
        {typed ? (
          <kbd className="rounded border border-[var(--focus-ring)] bg-[var(--focus-fill)] px-1.5 py-0.5 font-mono text-[12.5px] font-semibold text-[var(--focus-ring)]">
            {typed}
          </kbd>
        ) : (
          <span className="text-[var(--text-muted)]">输入按键直达目标</span>
        )}
        <span className="text-[12.5px] text-[var(--text-subtle)]">
          {matchingCount}/{assigned.length}
        </span>
          <button
            type="button"
            onClick={() => {
              setTyped('');
              onClose();
            }}
            aria-label="退出字母闪跳"
            title="退出字母闪跳 (Esc)"
            className="pointer-events-auto inline-flex h-6 w-6 cursor-pointer items-center justify-center rounded-full text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
      </div>

      {assigned.map(({ target, code, showLabel }) => {
        const isMatch = code.startsWith(typed);
        const matchedPrefix = typed && isMatch ? code.slice(0, typed.length) : '';
        const remainingCode = typed && isMatch ? code.slice(typed.length) : code;

        return (
          <button
            key={target.id}
            type="button"
            data-hint-id={target.id}
            data-hint-code={code}
            aria-label={showLabel ? `${code}: ${target.label}` : target.label || code}
            onClick={(e) => {
              e.stopPropagation();
              setTyped('');
              onPick(target.id);
            }}
            style={{ left: `${target.x}px`, top: `${target.y}px` }}
            className={[
              'fixed -translate-x-1/2 -translate-y-1/2 inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border px-2 py-0.5 text-xs shadow-sm transition-opacity',
              isMatch
                ? 'border-[var(--focus-ring)] bg-[var(--bg-elevated)] text-[var(--text-primary)] ring-2 ring-[var(--focus-glow)]'
                : 'pointer-events-none border-[var(--border-line)] bg-[var(--bg-soft)] text-[var(--text-subtle)] opacity-25',
            ].join(' ')}
          >
            <kbd className="omni-keycap rounded-[4px] px-1.5 py-0.5 text-[12.5px] font-bold">
              {matchedPrefix && (
                <span className="text-[var(--focus-ring)]">{matchedPrefix}</span>
              )}
              <span>{remainingCode}</span>
            </kbd>
            {showLabel && (
              <span className="max-w-[148px] truncate text-[13px] font-medium text-[var(--text-primary)]">
                {target.label}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export default HintOverlay;
