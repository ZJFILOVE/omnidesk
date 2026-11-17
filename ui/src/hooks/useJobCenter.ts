import { useCallback, useEffect, useRef, useState } from 'react';
import {
  cancelJob as cancelJobRpc,
  formatRpcError,
  isTauriAvailable,
  listJobs,
  type JobRecord,
  type RpcErrorInfo,
} from '../api';

export interface JobCenterState {
  jobs: JobRecord[];
  loading: boolean;
  error: RpcErrorInfo | null;
  lastUpdated: number | null;
  refresh: () => Promise<void>;
  cancel: (jobId: string) => Promise<void>;
}

const ACTIVE_STATES = new Set(['pending', 'running']);

export function isActiveJob(job: JobRecord): boolean {
  return ACTIVE_STATES.has(job.state);
}

export function useJobCenter(enabled = isTauriAvailable()): JobCenterState {
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<RpcErrorInfo | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!enabled) {
      setJobs([]);
      setError(null);
      return;
    }
    setLoading(true);
    const result = await listJobs();
    if (!mountedRef.current) return;
    setLoading(false);
    if (!result.available) {
      setError({
        code: 'tauri_unavailable',
        message: result.error?.message ?? '任务中心需要连接 Core',
      });
      return;
    }
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setError(null);
    setJobs(result.data.jobs);
    setLastUpdated(Date.now());
  }, [enabled]);

  const cancel = useCallback(
    async (jobId: string) => {
      if (!enabled) {
        setError({ code: 'tauri_unavailable', message: '离线预览不能取消后台任务' });
        return;
      }
      const result = await cancelJobRpc(jobId);
      if (!mountedRef.current) return;
      if (!result.ok) {
        setError(result.error);
        return;
      }
      setJobs((prev) =>
        prev.map((job) => (job.id === result.data.job.id ? result.data.job : job)),
      );
      setError(null);
    },
    [enabled],
  );

  useEffect(() => {
    if (!enabled) return undefined;
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [enabled, refresh]);

  return {
    jobs,
    loading,
    error,
    lastUpdated,
    refresh,
    cancel,
  };
}

export function jobErrorText(job: JobRecord): string {
  if (!job.error) return '';
  return job.errorCode ? `${formatRpcError({ code: job.errorCode, message: job.error })}` : job.error;
}
