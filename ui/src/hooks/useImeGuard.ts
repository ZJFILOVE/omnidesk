import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export interface ImeKeyEventLike {
  isComposing?: boolean;
  keyCode?: number;
  which?: number;
  key?: string;
  code?: string;
  target?: EventTarget | null;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  nativeEvent?: {
    isComposing?: boolean;
    keyCode?: number;
    which?: number;
    key?: string;
  };
}

export interface UseImeGuardOptions {
  /** Optional post-compositionend grace window in ms for Safari/WebKitGTK quirks (default: 0). */
  postCompositionGraceMs?: number;
}

export interface ImeCompositionProps {
  onCompositionStart: () => void;
  onCompositionUpdate: () => void;
  onCompositionEnd: () => void;
}

export interface ImeGuard {
  isComposing: boolean;
  isComposingRef: { readonly current: boolean };
  shouldIgnoreShortcut: (event?: ImeKeyEventLike | null) => boolean;
  shouldIgnoreGlobalShortcut: (
    event?: ImeKeyEventLike | null,
    options?: { ignoreInEditable?: boolean },
  ) => boolean;
  onCompositionStart: () => void;
  onCompositionUpdate: () => void;
  onCompositionEnd: () => void;
  resetComposition: () => void;
  compositionProps: ImeCompositionProps;
}

export function isImeComposingEvent(event?: ImeKeyEventLike | null): boolean {
  if (!event) {
    return false;
  }
  if (event.isComposing || event.nativeEvent?.isComposing) {
    return true;
  }
  if (event.key === 'Process' || event.nativeEvent?.key === 'Process') {
    return true;
  }
  return (
    event.keyCode === 229 ||
    event.which === 229 ||
    event.nativeEvent?.keyCode === 229 ||
    event.nativeEvent?.which === 229
  );
}

export function isEditableTarget(target?: EventTarget | null): boolean {
  if (!target || typeof target !== 'object') {
    return false;
  }
  const el = target as {
    tagName?: string;
    isContentEditable?: boolean;
    getAttribute?: (name: string) => string | null;
  };
  if (el.isContentEditable) {
    return true;
  }
  if (typeof el.getAttribute === 'function' && el.getAttribute('contenteditable') === 'true') {
    return true;
  }
  const tag = typeof el.tagName === 'string' ? el.tagName.toUpperCase() : '';
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

/**
 * Tracks active IME composition state (e.g. Fcitx5 / Rime) via
 * `compositionstart`, `compositionupdate`, and `compositionend` so single-key
 * shortcuts and enter-to-submit actions do not hijack candidate selection.
 */
export function useImeGuard(options?: UseImeGuardOptions): ImeGuard {
  const graceMs = options?.postCompositionGraceMs ?? 0;
  const [isComposing, setIsComposing] = useState(false);
  const isComposingRef = useRef(false);
  const lastCompositionEndAtRef = useRef(0);

  const onCompositionStart = useCallback(() => {
    isComposingRef.current = true;
    setIsComposing(true);
  }, []);

  const onCompositionUpdate = useCallback(() => {
    if (!isComposingRef.current) {
      isComposingRef.current = true;
      setIsComposing(true);
    }
  }, []);

  const onCompositionEnd = useCallback(() => {
    isComposingRef.current = false;
    lastCompositionEndAtRef.current = Date.now();
    setIsComposing(false);
  }, []);

  const resetComposition = useCallback(() => {
    isComposingRef.current = false;
    lastCompositionEndAtRef.current = 0;
    setIsComposing(false);
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined;
    }

    const handleWindowBlur = (event: FocusEvent) => {
      if (!event.target || event.target === window || event.target === document) {
        onCompositionEnd();
      }
    };

    const handleVisibilityChange = () => {
      if (typeof document !== 'undefined' && document.hidden && isComposingRef.current) {
        onCompositionEnd();
      }
    };

    window.addEventListener('compositionstart', onCompositionStart, true);
    window.addEventListener('compositionupdate', onCompositionUpdate, true);
    window.addEventListener('compositionend', onCompositionEnd, true);
    window.addEventListener('blur', handleWindowBlur);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', handleVisibilityChange);
    }

    return () => {
      window.removeEventListener('compositionstart', onCompositionStart, true);
      window.removeEventListener('compositionupdate', onCompositionUpdate, true);
      window.removeEventListener('compositionend', onCompositionEnd, true);
      window.removeEventListener('blur', handleWindowBlur);
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', handleVisibilityChange);
      }
    };
  }, [onCompositionStart, onCompositionUpdate, onCompositionEnd]);

  const shouldIgnoreShortcut = useCallback(
    (event?: ImeKeyEventLike | null) => {
      if (isComposingRef.current) {
        return true;
      }
      if (graceMs > 0 && lastCompositionEndAtRef.current > 0) {
        if (Date.now() - lastCompositionEndAtRef.current < graceMs) {
          return true;
        }
      }
      return isImeComposingEvent(event);
    },
    [graceMs],
  );

  const shouldIgnoreGlobalShortcut = useCallback(
    (event?: ImeKeyEventLike | null, opts?: { ignoreInEditable?: boolean }) => {
      if (shouldIgnoreShortcut(event)) {
        return true;
      }
      if (opts?.ignoreInEditable && event && isEditableTarget(event.target)) {
        return true;
      }
      return false;
    },
    [shouldIgnoreShortcut],
  );

  const compositionProps = useMemo<ImeCompositionProps>(
    () => ({
      onCompositionStart,
      onCompositionUpdate,
      onCompositionEnd,
    }),
    [onCompositionStart, onCompositionUpdate, onCompositionEnd],
  );

  return useMemo<ImeGuard>(
    () => ({
      isComposing,
      isComposingRef,
      shouldIgnoreShortcut,
      shouldIgnoreGlobalShortcut,
      onCompositionStart,
      onCompositionUpdate,
      onCompositionEnd,
      resetComposition,
      compositionProps,
    }),
    [
      isComposing,
      shouldIgnoreShortcut,
      shouldIgnoreGlobalShortcut,
      onCompositionStart,
      onCompositionUpdate,
      onCompositionEnd,
      resetComposition,
      compositionProps,
    ],
  );
}

export default useImeGuard;
