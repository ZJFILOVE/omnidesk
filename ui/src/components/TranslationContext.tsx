import { createContext, useContext, useEffect, useRef } from 'react';
import { Languages } from 'lucide-react';

export interface TranslationSegment { id: string; text: string }
export interface TranslationRequest {
  title: string;
  segments: TranslationSegment[];
  onTranslated?: (results: Array<{ id: string; translation: string }>) => void;
  autoStart?: boolean;
}
export const TranslationContext = createContext<((request: TranslationRequest) => void) | null>(null);

const activeTriggers = new Set<() => boolean>();

export function triggerActiveTranslation(): boolean {
  for (const trigger of Array.from(activeTriggers).reverse()) {
    if (trigger()) return true;
  }
  return false;
}

export function useTranslationTrigger(trigger: () => boolean, active = true): void {
  const ref = useRef(trigger);
  ref.current = trigger;
  useEffect(() => {
    if (!active) return;
    const fn = () => ref.current();
    activeTriggers.add(fn);
    return () => { activeTriggers.delete(fn); };
  }, [active]);
}

export function TranslationButton({ request, label = '翻译', disabled = false }: { request: TranslationRequest; label?: string; disabled?: boolean }) {
  const open = useContext(TranslationContext);
  const canTranslate = !disabled && Boolean(open) && request.segments.some(segment => segment.text.trim());
  const latestRef = useRef({ canTranslate, open, request });
  latestRef.current = { canTranslate, open, request };

  useEffect(() => {
    const trigger = () => {
      const cur = latestRef.current;
      if (!cur.canTranslate || !cur.open) return false;
      cur.open({ ...cur.request, autoStart: true });
      return true;
    };
    activeTriggers.add(trigger);
    return () => { activeTriggers.delete(trigger); };
  }, []);

  return <button type="button" disabled={!canTranslate}
    onClick={() => open?.(request)}
    className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-[var(--border-line)] px-3 text-sm font-medium text-[var(--text-primary)] hover:bg-[var(--hover-fill)] disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]">
    <Languages className="h-4 w-4" aria-hidden="true" />{label}
  </button>;
}
