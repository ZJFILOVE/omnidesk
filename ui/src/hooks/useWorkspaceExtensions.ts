import { useCallback, useEffect, useRef, useState } from 'react';
import {
  isTauriAvailable,
  workspaceSnapshot,
  type DockWidgetSnapshot,
  type RpcErrorInfo,
  type WorkspaceTabSnapshot,
} from '../api';

export interface WorkspaceExtensionsState {
  workspaceTabs: WorkspaceTabSnapshot[];
  dockWidgets: DockWidgetSnapshot[];
  loading: boolean;
  loaded: boolean;
  error: RpcErrorInfo | null;
  refresh: () => Promise<void>;
}

export function useWorkspaceExtensions(refreshKey?: string | number | null): WorkspaceExtensionsState {
  const [workspaceTabs, setWorkspaceTabs] = useState<WorkspaceTabSnapshot[]>([]);
  const [dockWidgets, setDockWidgets] = useState<DockWidgetSnapshot[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<RpcErrorInfo | null>(null);
  const mountedRef = useRef(true);
  const requestSeqRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!isTauriAvailable()) {
      if (!mountedRef.current) return;
      setWorkspaceTabs([]);
      setDockWidgets([]);
      setLoading(false);
      setLoaded(true);
      setError(null);
      return;
    }
    const seq = ++requestSeqRef.current;
    setLoading(true);
    setError(null);
    const res = await workspaceSnapshot();
    if (!mountedRef.current || seq !== requestSeqRef.current) return;
    setLoading(false);
    setLoaded(true);
    if (!res.available) {
      setError(res.error);
      return;
    }
    if (!res.ok) {
      setError(res.error);
      return;
    }
    setWorkspaceTabs(res.data.workspace_tabs);
    setDockWidgets(res.data.dock_widgets);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (refreshKey === undefined || refreshKey === null) return;
    void refresh();
  }, [refresh, refreshKey]);

  return {
    workspaceTabs,
    dockWidgets,
    loading,
    loaded,
    error,
    refresh,
  };
}

export default useWorkspaceExtensions;
