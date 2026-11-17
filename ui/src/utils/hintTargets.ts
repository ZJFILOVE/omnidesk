import type { HintOverlayTarget } from '../components/HintOverlay';
import type { HintTarget } from '../types';

export function buildClampedHintTargets(rawTargets: readonly HintTarget[]): HintOverlayTarget[] {
  const maxW = typeof window !== 'undefined' && window.innerWidth > 0 ? window.innerWidth : 1280;
  const maxH = typeof window !== 'undefined' && window.innerHeight > 0 ? window.innerHeight : 800;
  return rawTargets.map((t) => ({
    id: t.id,
    label: t.label ?? '',
    x: Math.max(24, Math.min(maxW - 24, t.rect.left + Math.min(28, t.rect.width * 0.5))),
    y: Math.max(20, Math.min(maxH - 20, t.rect.top + Math.min(18, t.rect.height * 0.5))),
  }));
}

export function scheduleFrame(fn: () => void): () => void {
  if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
    const raf = window.requestAnimationFrame(fn);
    return () => window.cancelAnimationFrame(raf);
  }
  fn();
  return () => {};
}
