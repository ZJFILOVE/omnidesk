import { useCallback, useEffect, useRef, useState } from 'react';
import {
  coreStatus as fetchCoreStatus,
  eventBridgeStatus as fetchEventBridgeStatus,
  isTauriAvailable,
  isTauriEventsAvailable,
  listenTauriEvent,
  runtimeStatus as fetchRuntimeStatus,
  type BusEventEnvelope,
  type CoreStatusData,
  type EventBridgeSnapshot,
  type RpcErrorInfo,
  type RuntimeStatus,
  type TauriUnlistenFn,
} from '../api';

export const BUS_EVENT_CHANNEL = 'omnidesk:bus';
export const BRIDGE_STATUS_CHANNEL = 'omnidesk:bridge-status';

export interface PluginHealthEntry {
  pluginId: string;
  state?: string;
  healthOk: boolean;
  lastError?: string;
  component?: string;
  updatedAt: number;
}

export interface UseCoreEventsOptions {
  pollOnMount?: boolean;
  onBusEvent?: (event: BusEventEnvelope) => void;
  onBridgeStatus?: (status: EventBridgeSnapshot) => void;
}

export interface UseCoreEventsResult {
  tauriAvailable: boolean;
  runtime: RuntimeStatus | null;
  core: CoreStatusData | null;
  coreOnline: boolean;
  bridgeStatus: EventBridgeSnapshot | null;
  pluginHealthMap: Record<string, PluginHealthEntry>;
  lastBusEvent: BusEventEnvelope | null;
  recentBusEvents: BusEventEnvelope[];
  statusError: RpcErrorInfo | null;
  refreshStatus: () => Promise<void>;
}

function asString(val: unknown): string {
  return typeof val === 'string' ? val : '';
}

export function useCoreEvents(options: UseCoreEventsOptions = {}): UseCoreEventsResult {
  const { pollOnMount = true, onBusEvent, onBridgeStatus } = options;
  const tauriAvailable = isTauriAvailable();

  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [core, setCore] = useState<CoreStatusData | null>(null);
  const [coreOnline, setCoreOnline] = useState(false);
  const [bridgeStatus, setBridgeStatus] = useState<EventBridgeSnapshot | null>(null);
  const [pluginHealthMap, setPluginHealthMap] = useState<Record<string, PluginHealthEntry>>({});
  const [lastBusEvent, setLastBusEvent] = useState<BusEventEnvelope | null>(null);
  const [recentBusEvents, setRecentBusEvents] = useState<BusEventEnvelope[]>([]);
  const [statusError, setStatusError] = useState<RpcErrorInfo | null>(null);

  const disposedRef = useRef(false);
  const onBusEventRef = useRef(onBusEvent);
  onBusEventRef.current = onBusEvent;
  const onBridgeStatusRef = useRef(onBridgeStatus);
  onBridgeStatusRef.current = onBridgeStatus;

  const refreshStatus = useCallback(async () => {
    if (!isTauriAvailable()) {
      setCoreOnline(false);
      setRuntime(null);
      setCore(null);
      setBridgeStatus(null);
      setStatusError(null);
      return;
    }

    const [rtRes, coreRes, bridgeRes] = await Promise.all([
      fetchRuntimeStatus(),
      fetchCoreStatus(),
      fetchEventBridgeStatus(),
    ]);
    if (disposedRef.current) return;

    if (rtRes.ok) {
      setRuntime(rtRes.data);
    }

    if (coreRes.ok) {
      setCore(coreRes.data);
      setCoreOnline(Boolean(coreRes.data.online));
      if (coreRes.data.event_bridge) {
        setBridgeStatus(coreRes.data.event_bridge);
      }
      setStatusError(null);
    } else if (coreRes.available) {
      const dataOnline = Boolean(coreRes.data && coreRes.data.online === true);
      setCoreOnline(dataOnline);
      if (coreRes.data && typeof coreRes.data.event_bridge === 'object' && coreRes.data.event_bridge) {
        setBridgeStatus(coreRes.data.event_bridge as EventBridgeSnapshot);
      }
      setStatusError(coreRes.error);
    }

    if (bridgeRes.ok) {
      setBridgeStatus(bridgeRes.data);
    }
  }, []);

  useEffect(() => {
    disposedRef.current = false;

    if (!isTauriAvailable()) {
      return () => {
        disposedRef.current = true;
      };
    }

    if (pollOnMount) {
      void refreshStatus();
    }

    if (!isTauriEventsAvailable()) {
      return () => {
        disposedRef.current = true;
      };
    }

    const unlisteners: TauriUnlistenFn[] = [];
    const register = (promise: Promise<TauriUnlistenFn | null>) => {
      void promise
        .then((unlisten) => {
          if (!unlisten) return;
          if (disposedRef.current) {
            void Promise.resolve(unlisten()).catch(() => {});
          } else {
            unlisteners.push(unlisten);
          }
        })
        .catch(() => {});
    };

    register(
      listenTauriEvent<EventBridgeSnapshot>(BRIDGE_STATUS_CHANNEL, (snapshot) => {
        if (disposedRef.current || !snapshot || typeof snapshot !== 'object') return;
        setBridgeStatus(snapshot);
        if (snapshot.status === 'connected') {
          setCoreOnline(true);
          setStatusError(null);
        } else if (snapshot.status === 'offline') {
          setCoreOnline(false);
        }
        onBridgeStatusRef.current?.(snapshot);
      }),
    );

    register(
      listenTauriEvent<BusEventEnvelope>(BUS_EVENT_CHANNEL, (envelope) => {
        if (disposedRef.current || !envelope || typeof envelope !== 'object') return;
        setLastBusEvent(envelope);
        setRecentBusEvents((prev) => [envelope, ...prev].slice(0, 20));

        const payload = envelope.payload ?? {};
        if (envelope.topic === 'plugin.fault') {
          const pluginId = asString(payload.plugin_id) || envelope.source_plugin_id;
          if (pluginId) {
            setPluginHealthMap((prev) => ({
              ...prev,
              [pluginId]: {
                pluginId,
                state: 'faulted',
                healthOk: false,
                lastError: asString(payload.error) || '插件运行异常',
                component: asString(payload.component) || undefined,
                updatedAt: Date.now(),
              },
            }));
          }
        } else if (envelope.topic === 'plugin.state_changed') {
          const pluginId = asString(payload.plugin_id);
          const state = asString(payload.state);
          if (pluginId) {
            const healthy = state === 'loaded' || state === 'reloaded';
            setPluginHealthMap((prev) => ({
              ...prev,
              [pluginId]: {
                pluginId,
                state: state || prev[pluginId]?.state,
                healthOk: healthy,
                lastError: healthy ? '' : prev[pluginId]?.lastError,
                updatedAt: Date.now(),
              },
            }));
          }
        }

        onBusEventRef.current?.(envelope);
      }),
    );

    return () => {
      disposedRef.current = true;
      for (const unlisten of unlisteners.splice(0, unlisteners.length)) {
        try {
          void Promise.resolve(unlisten()).catch(() => {});
        } catch {
          // Ignore unlisten errors on teardown.
        }
      }
    };
  }, [pollOnMount, refreshStatus]);

  return {
    tauriAvailable,
    runtime,
    core,
    coreOnline,
    bridgeStatus,
    pluginHealthMap,
    lastBusEvent,
    recentBusEvents,
    statusError,
    refreshStatus,
  };
}

export default useCoreEvents;
