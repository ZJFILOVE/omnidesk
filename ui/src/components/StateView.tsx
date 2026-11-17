import React from 'react';
import { AlertTriangle, Inbox, Loader2, WifiOff } from 'lucide-react';
import { formatRpcError, type RpcErrorInfo } from '../api';
import { FocusButton } from './WorkbenchLayout';

export type StateViewMode = 'loading' | 'empty' | 'error' | 'offline';

export interface StateViewAction {
  label: string;
  focusId?: string;
  onActivate: () => void;
}

export interface StateViewProps {
  mode: StateViewMode;
  title?: string;
  description?: string;
  error?: RpcErrorInfo | string | null;
  detail?: string;
  variant?: 'panel' | 'inline';
  action?: StateViewAction;
  activeFocusId?: string;
  onFocus?: (id: string) => void;
  testId?: string;
  className?: string;
}

const DEFAULT_TITLES: Record<StateViewMode, string> = {
  loading: '正在同步…',
  empty: '暂无匹配内容',
  error: '请求未完成',
  offline: '离线预览模式',
};

const DEFAULT_DESCRIPTIONS: Record<StateViewMode, string> = {
  loading: '正在读取最新状态，请稍候。',
  empty: '可尝试清空搜索词或切换筛选分组。',
  error: '请检查后台服务状态后重试。',
  offline: '当前处于浏览器预览，未连接 Tauri 运行时，写入操作已禁用。',
};

export const StateView: React.FC<StateViewProps> = ({
  mode,
  title,
  description,
  error,
  detail,
  variant = 'panel',
  action,
  activeFocusId = '',
  onFocus,
  testId,
  className = '',
}) => {
  const isError = mode === 'error';
  const isLoading = mode === 'loading';
  const resolvedTitle = title ?? DEFAULT_TITLES[mode];
  const resolvedDescription =
    typeof error === 'string' && error.trim()
      ? error
      : error && typeof error === 'object'
        ? formatRpcError(error)
        : (description ?? DEFAULT_DESCRIPTIONS[mode]);

  const palette =
    mode === 'error'
      ? {
          bg: 'var(--status-err-bg)',
          fg: 'var(--status-err-fg)',
          iconBg: 'color-mix(in srgb, var(--status-err-fg) 13%, transparent)',
        }
      : mode === 'offline'
        ? {
            bg: 'var(--status-warn-bg)',
            fg: 'var(--status-warn-fg)',
            iconBg: 'color-mix(in srgb, var(--status-warn-fg) 13%, transparent)',
          }
        : {
            bg: 'var(--bg-surface)',
            fg: 'var(--text-primary)',
            iconBg: 'var(--bg-soft)',
          };

  const renderIcon = () => {
    if (isLoading) {
      return (
        <Loader2
          className="h-4 w-4 animate-spin motion-reduce:animate-none"
          aria-hidden="true"
        />
      );
    }
    if (mode === 'offline') {
      return <WifiOff className="h-4 w-4" aria-hidden="true" />;
    }
    if (isError) {
      return <AlertTriangle className="h-4 w-4" aria-hidden="true" />;
    }
    return <Inbox className="h-4 w-4" aria-hidden="true" />;
  };

  if (variant === 'inline') {
    return (
      <div
        role={isError ? 'alert' : 'status'}
        aria-busy={isLoading || undefined}
        data-testid={testId}
        data-state-mode={mode}
        className={`flex flex-wrap items-center justify-between gap-3 rounded-[12px] px-4 py-2.5 text-[13px] ${className}`}
        style={{ backgroundColor: palette.bg, color: palette.fg }}
      >
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full"
            style={{ backgroundColor: palette.iconBg }}
          >
            {renderIcon()}
          </span>
          <div className="min-w-0 leading-[1.55]">
            <span className="font-semibold">{resolvedTitle}</span>
            {resolvedDescription ? (
              <span className="ml-2 text-[12.5px] opacity-90">{resolvedDescription}</span>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {detail ? <span className="font-mono text-[12px] opacity-80">{detail}</span> : null}
          {action ? (
            <FocusButton
              focusId={action.focusId ?? `state:action:${mode}`}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={action.onActivate}
              tone={isError ? 'err' : 'neutral'}
              surface="surface"
              className="px-3 py-1.5 text-[12.5px] font-medium"
            >
              {action.label}
            </FocusButton>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div
      role={isError ? 'alert' : 'status'}
      aria-busy={isLoading || undefined}
      data-testid={testId}
      data-state-mode={mode}
      className={`flex flex-col items-center justify-center gap-3.5 rounded-[16px] px-7 py-11 text-center ${className}`}
      style={{
        backgroundColor: palette.bg,
        color: palette.fg,
        boxShadow: 'var(--shadow-page)',
      }}
    >
      <span
        className="inline-flex h-11 w-11 items-center justify-center rounded-full"
        style={{ backgroundColor: palette.iconBg }}
      >
        {renderIcon()}
      </span>
      <div className="max-w-md space-y-1.5">
        <div className="text-[15.5px] font-semibold">{resolvedTitle}</div>
        {resolvedDescription ? (
          <p
            className="text-[13.5px] leading-[1.72]"
            style={{
              color:
                mode === 'empty' || mode === 'loading' ? 'var(--text-muted)' : 'inherit',
              opacity: mode === 'error' || mode === 'offline' ? 0.92 : 1,
            }}
          >
            {resolvedDescription}
          </p>
        ) : null}
      </div>

      {isLoading ? (
        <div className="mt-1 flex w-48 flex-col gap-2 opacity-65" aria-hidden="true">
          <div className="h-2 w-full rounded-full bg-[var(--bg-soft)] animate-pulse motion-reduce:animate-none" />
          <div className="mx-auto h-2 w-3/4 rounded-full bg-[var(--bg-soft)] animate-pulse motion-reduce:animate-none" />
        </div>
      ) : null}

      {action ? (
        <FocusButton
          focusId={action.focusId ?? `state:action:${mode}`}
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={action.onActivate}
          tone={isError ? 'err' : 'neutral'}
          surface="soft"
          className="mt-1 px-4 py-1.5 text-[13px] font-medium"
        >
          {action.label}
        </FocusButton>
      ) : null}
    </div>
  );
};

export default StateView;
