import { createContext, useContext } from 'react';
import { Languages } from 'lucide-react';

export interface TranslationSegment { id: string; text: string }
export interface TranslationRequest {
  title: string;
  segments: TranslationSegment[];
  onTranslated?: (results: Array<{ id: string; translation: string }>) => void;
}
export const TranslationContext = createContext<((request: TranslationRequest) => void) | null>(null);

export function TranslationButton({ request, label = '翻译', disabled = false }: { request: TranslationRequest; label?: string; disabled?: boolean }) {
  const open = useContext(TranslationContext);
  return <button type="button" disabled={disabled || !open || !request.segments.some(segment => segment.text.trim())}
    onClick={() => open?.(request)}
    className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-[var(--border-line)] px-3 text-sm font-medium text-[var(--text-primary)] hover:bg-[var(--hover-fill)] disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]">
    <Languages className="h-4 w-4" aria-hidden="true" />{label}
  </button>;
}
