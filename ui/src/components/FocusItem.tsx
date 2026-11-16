import {
  useCallback,
  useEffect,
  useRef,
  type HTMLAttributes,
  type MouseEvent,
  type ReactNode,
  type Ref,
} from 'react';
import { useOptionalSpatialFocus } from '../hooks/useSpatialFocus';
import type { FocusZone } from '../types';

export interface FocusItemProps extends Omit<HTMLAttributes<HTMLElement>, 'id'> {
  id: string;
  zone?: FocusZone;
  label?: string;
  disabled?: boolean;
  priority?: number;
  as?: 'div' | 'button' | 'article' | 'li' | 'section';
  type?: 'button' | 'submit' | 'reset';
  onActivate?: () => void;
  ref?: Ref<HTMLElement>;
  children: ReactNode;
}

export function FocusItem({
  id,
  zone = 'main',
  label,
  disabled = false,
  priority = 0,
  as = 'div',
  type = 'button',
  onActivate,
  onClick,
  className = '',
  tabIndex,
  ref,
  children,
  ...rest
}: FocusItemProps) {
  const focusCtx = useOptionalSpatialFocus();
  const elementRef = useRef<HTMLElement | null>(null);
  const onActivateRef = useRef(onActivate);
  onActivateRef.current = onActivate;
  const hasActivate = Boolean(onActivate);

  const isFocused = focusCtx?.focusedId === id;

  const setMergedRef = useCallback(
    (node: HTMLElement | null) => {
      elementRef.current = node;
      if (!ref) {
        return;
      }
      if (typeof ref === 'function') {
        ref(node);
      } else {
        (ref as { current: HTMLElement | null }).current = node;
      }
    },
    [ref],
  );

  useEffect(() => {
    const el = elementRef.current;
    if (!focusCtx || !el) {
      return undefined;
    }

    return focusCtx.registerNode({
      id,
      element: el,
      zone,
      label,
      disabled,
      priority,
      onActivate: hasActivate ? () => onActivateRef.current?.() : undefined,
    });
  }, [disabled, focusCtx, hasActivate, id, label, priority, zone]);

  const handleClick = useCallback(
    (event: MouseEvent<HTMLElement>) => {
      if (disabled) {
        event.preventDefault();
        return;
      }

      focusCtx?.focusById(id, { scroll: false });
      onClick?.(event);

      if (event.defaultPrevented || !onActivateRef.current) {
        return;
      }

      if (
        event.target instanceof HTMLElement &&
        elementRef.current &&
        event.target !== elementRef.current
      ) {
        const closestInteractive = event.target.closest('button, a, input, select, textarea');
        if (closestInteractive && closestInteractive !== elementRef.current) {
          return;
        }
      }

      onActivateRef.current();
    },
    [disabled, focusCtx, id, onClick],
  );

  const Component = as;
  const resolvedClassName = [
    isFocused ? 'omni-focus-ring' : '',
    disabled ? 'opacity-50 cursor-not-allowed' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  const sharedProps = {
    ref: setMergedRef,
    'data-focus-id': id,
    'data-focus-zone': zone,
    'data-focus-label': label,
    'data-focus-disabled': disabled ? 'true' : undefined,
    'data-focus-priority': priority !== 0 ? String(priority) : undefined,
    'data-focused': isFocused ? 'true' : 'false',
    'aria-current': isFocused ? ('true' as const) : undefined,
    'aria-disabled': disabled ? true : undefined,
    tabIndex: disabled ? -1 : (tabIndex ?? (as === 'button' ? 0 : -1)),
    onClick: handleClick,
    className: resolvedClassName,
    ...rest,
  };

  if (Component === 'button') {
    return (
      <button type={type} disabled={disabled} {...sharedProps}>
        {children}
      </button>
    );
  }

  if (Component === 'article') {
    return <article {...sharedProps}>{children}</article>;
  }

  if (Component === 'li') {
    return <li {...sharedProps}>{children}</li>;
  }

  if (Component === 'section') {
    return <section {...sharedProps}>{children}</section>;
  }

  return <div {...sharedProps}>{children}</div>;
}
