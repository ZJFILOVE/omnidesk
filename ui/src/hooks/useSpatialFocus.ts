import {
  createContext, createElement, useCallback, useContext, useEffect,
  useMemo, useRef, useState, type ReactNode,
} from 'react';
import type { Direction, FocusNodeRegistration, FocusRect, FocusZone, HintTarget } from '../types';

export interface SpatialRectNode {
  id: string;
  element: HTMLElement;
  zone: FocusZone;
  label?: string;
  priority: number;
  rect: FocusRect;
  onActivate?: () => void;
}

export interface SpatialFocusContextValue {
  focusedId: string | null;
  focusedZone: FocusZone | null;
  focusById: (id: string, options?: { scroll?: boolean }) => boolean;
  moveFocus: (direction: Direction) => string | null;
  activateFocused: () => boolean;
  activateById: (id: string) => boolean;
  registerNode: (node: FocusNodeRegistration) => () => void;
  collectHintTargets: (zones?: FocusZone[]) => HintTarget[];
  getFocusableIds: (zones?: FocusZone[]) => string[];
  ensureFocus: (preferredIds?: readonly (string | null | undefined)[]) => string | null;
}

const DEFAULT_ZONES: FocusZone[] = ['nav', 'main'];
const HINT_ALPHABET = 'asdfjklghqweruioptyzxcvbnm';

export const SpatialFocusContext = createContext<SpatialFocusContextValue | null>(null);

export function toFocusRect(r: DOMRect | FocusRect): FocusRect {
  return { left: r.left, top: r.top, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
}

function parseZone(raw: string | undefined): FocusZone {
  return raw === 'nav' || raw === 'main' || raw === 'dock' || raw === 'overlay' ? raw : 'main';
}

function isElementVisible(element: HTMLElement): boolean {
  if (!element.isConnected) return false;
  for (let cur: HTMLElement | null = element; cur && cur !== document.documentElement; cur = cur.parentElement) {
    if (cur.hidden || cur.getAttribute('aria-hidden') === 'true' || cur.style.display === 'none' || cur.style.visibility === 'hidden') {
      return false;
    }
  }
  if (typeof window !== 'undefined' && typeof window.getComputedStyle === 'function') {
    const computed = window.getComputedStyle(element);
    if (computed.display === 'none' || computed.visibility === 'hidden' || computed.visibility === 'collapse') return false;
  }
  return true;
}

function isElementDisabled(element: HTMLElement, explicitDisabled?: boolean): boolean {
  return Boolean(
    explicitDisabled ||
      element.hasAttribute('disabled') ||
      element.getAttribute('aria-disabled') === 'true' ||
      element.dataset.focusDisabled === 'true',
  );
}

export function generateHintCodes(count: number): string[] {
  if (count <= 0) return [];
  const base = HINT_ALPHABET.length;
  if (count <= base) return HINT_ALPHABET.slice(0, count).split('');
  const prefixCount = Math.min(base, Math.ceil((count - base) / (base - 1)));
  const singleCount = count <= base * base ? base - prefixCount : 0;
  const codes = HINT_ALPHABET.slice(0, singleCount).split('');
  for (const prefix of HINT_ALPHABET.slice(singleCount)) {
    for (const ch of HINT_ALPHABET) {
      codes.push(prefix + ch);
      if (codes.length >= count) return codes;
    }
  }
  return codes;
}

function intervalGap(aStart: number, aEnd: number, bStart: number, bEnd: number): number {
  return aEnd >= bStart && bEnd >= aStart ? 0 : bStart > aEnd ? bStart - aEnd : aStart - bEnd;
}

export function findNearestFocusId(currentId: string, nodes: readonly SpatialRectNode[], direction: Direction): string | null {
  const current = nodes.find((node) => node.id === currentId);
  if (!current || current.rect.width <= 0 || current.rect.height <= 0) return null;

  const a = current.rect;
  const ax = a.left + a.width / 2;
  const ay = a.top + a.height / 2;
  const horizontal = direction === 'left' || direction === 'right';
  const forward = direction === 'right' || direction === 'down';
  const aCenter = horizontal ? ax : ay;
  const aBack = horizontal ? a.left : a.top;
  const aFront = horizontal ? a.right : a.bottom;
  const aSpan = horizontal ? a.width : a.height;

  let bestId: string | null = null;
  let bestScore = Number.POSITIVE_INFINITY;

  for (const candidate of nodes) {
    if (candidate.id === currentId) continue;
    const b = candidate.rect;
    if (b.width <= 0 || b.height <= 0) continue;
    if (current.element.contains(candidate.element) || candidate.element.contains(current.element)) continue;

    const bx = b.left + b.width / 2;
    const by = b.top + b.height / 2;
    const bCenter = horizontal ? bx : by;
    const bBack = horizontal ? b.left : b.top;
    const bFront = horizontal ? b.right : b.bottom;
    const bSpan = horizontal ? b.width : b.height;

    const centerDelta = forward ? bCenter - aCenter : aCenter - bCenter;
    const edgeCleared = forward ? bBack >= aFront - 4 : bFront <= aBack + 4;
    if (centerDelta <= 1 || (!edgeCleared && centerDelta < Math.max(4, Math.min(aSpan, bSpan) * 0.45))) continue;

    const primaryDist = forward
      ? bBack >= aFront ? bBack - aFront : Math.max(0, centerDelta * 0.5)
      : aBack >= bFront ? aBack - bFront : Math.max(0, centerDelta * 0.5);
    const orthoGap = horizontal ? intervalGap(a.top, a.bottom, b.top, b.bottom) : intervalGap(a.left, a.right, b.left, b.right);
    const orthoCenter = horizontal ? Math.abs(by - ay) : Math.abs(bx - ax);
    const score = primaryDist + orthoGap * 2.2 + orthoCenter * 0.15 + centerDelta * 0.001;

    if (score < bestScore) {
      bestScore = score;
      bestId = candidate.id;
    }
  }
  return bestId;
}

function pickDefaultNode(nodes: readonly SpatialRectNode[]): SpatialRectNode | null {
  if (nodes.length === 0) return null;
  const rank = (z: FocusZone) => (z === 'overlay' ? 0 : z === 'main' ? 1 : z === 'dock' ? 2 : 3);
  return [...nodes].sort((a, b) => {
    const zDiff = rank(a.zone) - rank(b.zone);
    if (zDiff !== 0) return zDiff;
    if (b.priority !== a.priority) return b.priority - a.priority;
    const aSized = a.rect.width > 0 && a.rect.height > 0 ? 0 : 1;
    const bSized = b.rect.width > 0 && b.rect.height > 0 ? 0 : 1;
    if (aSized !== bSized) return aSized - bSized;
    return Math.abs(a.rect.top - b.rect.top) > 6 ? a.rect.top - b.rect.top : a.rect.left - b.rect.left;
  })[0] ?? null;
}

function scrollIfAvailable(el: HTMLElement, scroll?: boolean): void {
  if (scroll !== false && typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
}

function collectSpatialNodes(registered: Map<string, FocusNodeRegistration>, zones: readonly FocusZone[]): SpatialRectNode[] {
  if (typeof document === 'undefined') return [];
  const activeZones = new Set(zones);
  const merged = new Map<string, SpatialRectNode>();

  for (const reg of registered.values()) {
    const zone = reg.zone ?? parseZone(reg.element.dataset.focusZone);
    if (!activeZones.has(zone) || isElementDisabled(reg.element, reg.disabled) || !isElementVisible(reg.element)) continue;
    merged.set(reg.id, {
      id: reg.id,
      element: reg.element,
      zone,
      label: reg.label ?? reg.element.dataset.focusLabel,
      priority: reg.priority ?? 0,
      rect: toFocusRect(reg.element.getBoundingClientRect()),
      onActivate: reg.onActivate,
    });
  }

  for (const el of document.querySelectorAll<HTMLElement>('[data-focus-id]')) {
    const id = el.dataset.focusId;
    if (!id || merged.has(id)) continue;
    const zone = parseZone(el.dataset.focusZone);
    if (!activeZones.has(zone) || isElementDisabled(el) || !isElementVisible(el)) continue;
    merged.set(id, {
      id,
      element: el,
      zone,
      label: el.dataset.focusLabel,
      priority: Number(el.dataset.focusPriority ?? '0') || 0,
      rect: toFocusRect(el.getBoundingClientRect()),
    });
  }
  return Array.from(merged.values());
}

export interface SpatialFocusProviderProps {
  children: ReactNode;
  allowedZones?: readonly FocusZone[];
  scopeKey?: string;
  preferredId?: string | null;
  onFocusChange?: (id: string | null, zone: FocusZone | null) => void;
}

export function SpatialFocusProvider({
  children,
  allowedZones = DEFAULT_ZONES,
  scopeKey,
  preferredId,
  onFocusChange,
}: SpatialFocusProviderProps) {
  const registeredRef = useRef<Map<string, FocusNodeRegistration>>(new Map());
  const allowedZonesRef = useRef<readonly FocusZone[]>(allowedZones);
  allowedZonesRef.current = allowedZones;

  const [focusedId, setFocusedIdState] = useState<string | null>(null);
  const focusedIdRef = useRef<string | null>(null);
  const lastTaggedElRef = useRef<HTMLElement | null>(null);
  const onFocusChangeRef = useRef(onFocusChange);
  onFocusChangeRef.current = onFocusChange;

  const resolveNodes = useCallback(
    (zones?: readonly FocusZone[]) => collectSpatialNodes(registeredRef.current, zones ?? allowedZonesRef.current),
    [],
  );

  const commitFocus = useCallback(
    (nextId: string | null, nodes?: readonly SpatialRectNode[]) => {
      if (focusedIdRef.current === nextId) return;
      focusedIdRef.current = nextId;
      setFocusedIdState(nextId);
      const targetNode = nextId ? (nodes ?? resolveNodes()).find((n) => n.id === nextId) : null;
      onFocusChangeRef.current?.(nextId, targetNode?.zone ?? null);
    },
    [resolveNodes],
  );

  const ensureFocus = useCallback(
    (preferredIds?: readonly (string | null | undefined)[]): string | null => {
      const nodes = resolveNodes();
      if (nodes.length === 0) { commitFocus(null, nodes); return null; }
      const currentId = focusedIdRef.current;
      if (currentId && nodes.some((n) => n.id === currentId)) return currentId;
      const matched = preferredIds?.find((id) => id && nodes.some((n) => n.id === id));
      const nextId = matched ?? pickDefaultNode(nodes)?.id ?? null;
      commitFocus(nextId, nodes);
      return nextId;
    },
    [commitFocus, resolveNodes],
  );

  const focusById = useCallback(
    (id: string, options?: { scroll?: boolean }): boolean => {
      const nodes = resolveNodes();
      const target = nodes.find((node) => node.id === id);
      if (!target) return false;
      commitFocus(target.id, nodes);
      scrollIfAvailable(target.element, options?.scroll);
      return true;
    },
    [commitFocus, resolveNodes],
  );

  const moveFocus = useCallback(
    (direction: Direction): string | null => {
      const nodes = resolveNodes();
      if (nodes.length === 0) return null;
      const currentId = focusedIdRef.current;
      if (!currentId || !nodes.some((n) => n.id === currentId)) {
        const fallback = pickDefaultNode(nodes);
        if (!fallback) return null;
        commitFocus(fallback.id, nodes);
        scrollIfAvailable(fallback.element, true);
        return fallback.id;
      }
      const nextId = findNearestFocusId(currentId, nodes, direction);
      if (!nextId) return null;
      const target = nodes.find((n) => n.id === nextId);
      commitFocus(nextId, nodes);
      if (target) scrollIfAvailable(target.element, true);
      return nextId;
    },
    [commitFocus, resolveNodes],
  );

  const activateById = useCallback(
    (id: string): boolean => {
      const nodes = resolveNodes();
      const target = nodes.find((node) => node.id === id);
      if (!target) return false;
      commitFocus(target.id, nodes);
      if (target.onActivate) { target.onActivate(); return true; }
      const el = target.element;
      const isTextControl =
        (el instanceof HTMLInputElement && !['checkbox', 'radio', 'button', 'submit'].includes(el.type)) ||
        el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
      if (isTextControl) el.focus();
      else el.click();
      return true;
    },
    [commitFocus, resolveNodes],
  );

  const activateFocused = useCallback(() => (focusedIdRef.current ? activateById(focusedIdRef.current) : false), [activateById]);

  const registerNode = useCallback(
    (node: FocusNodeRegistration) => {
      registeredRef.current.set(node.id, node);
      queueMicrotask(() => { if (!focusedIdRef.current) ensureFocus(preferredId ? [preferredId] : undefined); });
      return () => {
        if (registeredRef.current.get(node.id) === node) registeredRef.current.delete(node.id);
        queueMicrotask(() => { if (focusedIdRef.current === node.id) ensureFocus(preferredId ? [preferredId] : undefined); });
      };
    },
    [ensureFocus, preferredId],
  );

  const collectHintTargets = useCallback(
    (zones?: FocusZone[]): HintTarget[] => {
      const hasVp = typeof window !== 'undefined' && window.innerWidth > 0 && window.innerHeight > 0;
      const visible = resolveNodes(zones).filter(
        (n) =>
          n.rect.width > 0 &&
          n.rect.height > 0 &&
          (!hasVp || (n.rect.bottom > 0 && n.rect.top < window.innerHeight && n.rect.right > 0 && n.rect.left < window.innerWidth)),
      );
      const sorted = [...visible].sort((a, b) => (Math.abs(a.rect.top - b.rect.top) > 10 ? a.rect.top - b.rect.top : a.rect.left - b.rect.left));
      const codes = generateHintCodes(sorted.length);
      return sorted.map((n, idx) => ({ id: n.id, code: codes[idx] ?? String(idx + 1), label: n.label, zone: n.zone, rect: n.rect }));
    },
    [resolveNodes],
  );

  const getFocusableIds = useCallback((zones?: FocusZone[]) => resolveNodes(zones).map((n) => n.id), [resolveNodes]);

  useEffect(() => {
    const prevEl = lastTaggedElRef.current;
    if (prevEl && prevEl.dataset.focusId !== focusedId) {
      prevEl.removeAttribute('data-spatial-focused');
      lastTaggedElRef.current = null;
    }
    if (!focusedId || typeof document === 'undefined') return;
    const match = resolveNodes().find((n) => n.id === focusedId);
    if (match) {
      match.element.setAttribute('data-spatial-focused', 'true');
      lastTaggedElRef.current = match.element;
    }
  }, [focusedId, resolveNodes, scopeKey]);

  useEffect(() => {
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      const raf = window.requestAnimationFrame(() => ensureFocus(preferredId ? [preferredId] : undefined));
      return () => window.cancelAnimationFrame(raf);
    }
    ensureFocus(preferredId ? [preferredId] : undefined);
    return undefined;
  }, [allowedZones, ensureFocus, preferredId, scopeKey]);

  useEffect(() => {
    if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') return undefined;
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      queueMicrotask(() => {
        scheduled = false;
        const current = focusedIdRef.current;
        if (!current || !resolveNodes().some((n) => n.id === current)) ensureFocus(preferredId ? [preferredId] : undefined);
      });
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-focus-id', 'hidden', 'disabled', 'aria-hidden', 'aria-disabled'],
    });
    return () => observer.disconnect();
  }, [ensureFocus, preferredId, resolveNodes]);

  const focusedZone = useMemo<FocusZone | null>(
    () => (focusedId ? resolveNodes().find((n) => n.id === focusedId)?.zone ?? null : null),
    [focusedId, resolveNodes],
  );

  const value = useMemo<SpatialFocusContextValue>(
    () => ({ focusedId, focusedZone, focusById, moveFocus, activateFocused, activateById, registerNode, collectHintTargets, getFocusableIds, ensureFocus }),
    [activateById, activateFocused, collectHintTargets, ensureFocus, focusById, focusedId, focusedZone, getFocusableIds, moveFocus, registerNode],
  );

  return createElement(SpatialFocusContext.Provider, { value }, children);
}

export function useOptionalSpatialFocus(): SpatialFocusContextValue | null {
  return useContext(SpatialFocusContext);
}

export function useSpatialFocus(): SpatialFocusContextValue {
  const ctx = useContext(SpatialFocusContext);
  if (!ctx) throw new Error('useSpatialFocus must be used within a SpatialFocusProvider');
  return ctx;
}
