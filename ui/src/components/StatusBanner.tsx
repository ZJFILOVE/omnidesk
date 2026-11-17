import React from 'react';
import { AlertTriangle, WifiOff } from 'lucide-react';
import { formatRpcError, type RpcErrorInfo } from '../api';

export interface StatusBannerProps {
  mode: 'offline' | 'error' | 'warn';
  message?: string;
  error?: RpcErrorInfo | null;
  detail?: string;
  testId?: string;
  compact?: boolean;
}

export const StatusBanner: React.FC<StatusBannerProps> = ({
  mode,
  message,
  error,
  detail,
  testId,
  compact = false,
}) => {
  const isError = mode === 'error';
  const style = isError
    ? {
        backgroundColor: 'var(--status-err-bg)',
        color: 'var(--status-err-fg)',
      }
    : {
        backgroundColor: 'color-mix(in srgb, var(--status-warn-bg) 72%, var(--bg-canvas))',
        color: 'var(--status-warn-fg)',
      };

  const text = error
    ? formatRpcError(error)
    : (message ?? '离线预览：数据为示例（未连接 Tauri 运行时，写入操作已禁用）');

  return (
    <div
      role={isError ? 'alert' : 'status'}
      data-testid={testId}
      data-state-mode={mode}
      className={[
        'shrink-0 flex items-center justify-between gap-3',
        compact
          ? 'rounded-[10px] px-3.5 py-2 text-[14px]'
          : 'border-b border-[var(--border-subtle)] px-6 py-1.5 text-[14px]',
      ].join(' ')}
      style={style}
    >
      <span className="flex min-w-0 items-center gap-2 font-medium">
        {mode === 'offline' ? (
          <WifiOff className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        ) : (
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        )}
        <span className="truncate">{text}</span>
      </span>
      {detail ? (
        <span className="shrink-0 font-mono text-[13.5px] opacity-80">{detail}</span>
      ) : null}
    </div>
  );
};

export default StatusBanner;
