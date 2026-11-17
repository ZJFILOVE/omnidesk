import { useCallback, useEffect, useRef, useState } from 'react';
import {
  callCapability,
  formatRpcError,
  isTauriAvailable,
  type BusEventEnvelope,
  type LlmChatRequestPayload,
  type LlmChatResponseData,
  type RpcErrorInfo,
} from '../api';
import {
  AI_ARCHIVE_SNAPSHOT,
  INITIAL_AI_MESSAGES,
  INITIAL_MAGPIE_ROUTE,
  type AiChatMessage,
  type AiPresetItem,
  type AiStructuredCard,
  type CurationMode,
  type MagpieRouteState,
} from '../data/mock';
import {
  buildOfflineDemoReply,
  nowHHMM,
  parseMemberAndEffort,
  probeMagpieRouterState,
} from './aiWorkbenchHelpers';
import type { RepeatPolicy } from './panels/AiChatPane';

export function useAiWorkbenchController(lastBusEvent?: BusEventEnvelope | null) {
  const tauriAvailable = isTauriAvailable();
  const [route, setRoute] = useState<MagpieRouteState>(() => ({
    ...INITIAL_MAGPIE_ROUTE,
    gatewayHealthy: false,
  }));
  const [curationMode, setCurationMode] = useState<CurationMode>('interactive_ephemeral');
  const [repeatPolicy, setRepeatPolicy] = useState<RepeatPolicy>('prefer_familiar');
  const [messages, setMessages] = useState<AiChatMessage[]>(() =>
    tauriAvailable
      ? []
      : INITIAL_AI_MESSAGES.map((m) =>
          m.role === 'assistant' ? { ...m, isDemo: true } : m,
        ),
  );
  const [composerText, setComposerText] = useState<string>('');
  const [pinnedCards, setPinnedCards] = useState<AiStructuredCard[]>(() =>
    tauriAvailable ? [] : INITIAL_AI_MESSAGES[1]?.cards ?? [],
  );
  const [archiveSyncTime, setArchiveSyncTime] = useState<string>(
    tauriAvailable ? '未同步' : AI_ARCHIVE_SNAPSHOT.lastSyncAt,
  );
  const [isSending, setIsSending] = useState<boolean>(false);
  const [bannerError, setBannerError] = useState<RpcErrorInfo | null>(null);
  const [auditLogs, setAuditLogs] = useState<string[]>(() =>
    tauriAvailable
      ? ['已连接 Tauri · 等待查询 Magpie 路由状态...']
      : ['离线预览：以下日志为静态示例，未连接真实后端'],
  );

  const mountedRef = useRef(true);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const turnSeqRef = useRef(messages.length + 1);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const appendLog = useCallback((entry: string) => {
    setAuditLogs((prev) => [entry, ...prev.slice(0, 7)]);
  }, []);

  useEffect(() => {
    if (!isTauriAvailable()) {
      return undefined;
    }
    let cancelled = false;
    void probeMagpieRouterState().then(([routeRes, modelsRes, quotasRes]) => {
      if (cancelled || !mountedRef.current) return;

      if (routeRes.ok) {
        setBannerError(null);
        const parsed = routeRes.data.primary_member
          ? parseMemberAndEffort(routeRes.data.primary_member)
          : null;
        const strategy = routeRes.data.routing;
        setRoute((prev) => ({
          ...prev,
          gatewayUrl: routeRes.data.gateway_url || prev.gatewayUrl,
          gatewayHealthy: Boolean(routeRes.data.gateway_running ?? routeRes.data.online),
          activeGroup: routeRes.data.group_id
            ? routeRes.data.group_id.startsWith('group/')
              ? routeRes.data.group_id
              : `group/${routeRes.data.group_id}`
            : prev.activeGroup,
          primaryModel: parsed?.model || prev.primaryModel,
          effort: parsed?.effort ?? prev.effort,
          strategy:
            strategy === 'order' || strategy === 'smart' || strategy === 'usage' || strategy === 'pace'
              ? strategy
              : prev.strategy,
          fallbackChain: Array.isArray(routeRes.data.fallbacks)
            ? routeRes.data.fallbacks
            : prev.fallbackChain,
        }));
        appendLog(
          `${nowHHMM()} · llm.router.v1 current_route → ${routeRes.data.primary_member || 'ok'}`,
        );
      } else if (routeRes.available) {
        setRoute((prev) => ({ ...prev, gatewayHealthy: false }));
        setBannerError(routeRes.error);
        appendLog(`${nowHHMM()} · llm.router.v1 探测失败 ${formatRpcError(routeRes.error)}`);
      }

      if (modelsRes.ok && Array.isArray(modelsRes.data.models) && modelsRes.data.models.length > 0) {
        const modelIds = modelsRes.data.models
          .map((m) => (typeof m === 'string' ? m : String(m.id || m.model_id || '')))
          .filter(Boolean);
        if (modelIds.length > 0) {
          setRoute((prev) => ({ ...prev, availableModels: modelIds }));
        }
      }

      if (quotasRes.ok && Array.isArray(quotasRes.data.quotas)) {
        const quotas = quotasRes.data.quotas.map((q) => ({
          provider: String(q.provider ?? 'Magpie'),
          model: String(q.model ?? ''),
          usedPct: Number(q.usedPct ?? q.used_pct ?? 0),
          resetIn: String(q.resetIn ?? q.reset_in ?? '-'),
        }));
        setRoute((prev) => ({ ...prev, quotas }));
      }
    });

    return () => {
      cancelled = true;
    };
  }, [appendLog]);

  useEffect(() => {
    if (!lastBusEvent || lastBusEvent.topic !== 'entity.ai.route_changed') return;
    const p = lastBusEvent.payload;
    const rawPrimary = typeof p.primary_member === 'string' ? p.primary_member : '';
    const parsed = rawPrimary ? parseMemberAndEffort(rawPrimary) : null;
    setRoute((prev) => ({
      ...prev,
      activeGroup:
        typeof p.group_id === 'string'
          ? p.group_id.startsWith('group/')
            ? p.group_id
            : `group/${p.group_id}`
          : prev.activeGroup,
      primaryModel: parsed?.model || prev.primaryModel,
      effort: parsed?.effort ?? prev.effort,
    }));
  }, [lastBusEvent]);

  const handleSendPrompt = useCallback(
    async (overridePrompt?: string, overrideMode?: CurationMode) => {
      const text = (overridePrompt ?? composerText).trim();
      if (!text || isSending) return;
      const effectiveMode = overrideMode ?? curationMode;
      const ts = nowHHMM();
      const turn = turnSeqRef.current++;

      const userMsg: AiChatMessage = {
        id: `ai-msg-u-${turn}`,
        role: 'user',
        timestamp: ts,
        mode: effectiveMode,
        content: text,
      };
      const priorHistory = messagesRef.current
        .filter((m) => !m.isError && !m.isDemo && (m.role === 'user' || m.role === 'assistant'))
        .slice(-8)
        .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content }));

      setMessages((prev) => [...prev, userMsg]);
      if (!overridePrompt) setComposerText('');
      setBannerError(null);

      if (!isTauriAvailable()) {
        const demoReply = buildOfflineDemoReply(text, effectiveMode, repeatPolicy, route, turn);
        setMessages((prev) => [...prev, demoReply]);
        appendLog(`${ts} · 离线示例演示 [${effectiveMode}]（未发送至后端）`);
        return;
      }

      setIsSending(true);
      const payload: LlmChatRequestPayload = {
        system_prompt: `你是 OmniDesk AI 工作台助手（mode=${effectiveMode}, repeat_policy=${repeatPolicy}）。严禁编造未核验的曲目、DOI 或链接。`,
        messages: [...priorHistory, { role: 'user', content: text }],
        reasoning_effort: route.effort,
        route_override: route.activeGroup.startsWith('group/')
          ? route.activeGroup
          : `group/${route.activeGroup}`,
        stream: false,
        timeout_ms: 60000,
      };

      const res = await callCapability<LlmChatResponseData>({
        capability: 'llm.chat.v1',
        payload: payload as unknown as Record<string, unknown>,
        timeoutMs: 60000,
      });
      if (!mountedRef.current) return;
      setIsSending(false);

      const replyTs = nowHHMM();
      if (!res.available) {
        const demoReply = buildOfflineDemoReply(text, effectiveMode, repeatPolicy, route, turn);
        setMessages((prev) => [...prev, demoReply]);
        return;
      }

      if (!res.ok) {
        setBannerError(res.error);
        setMessages((prev) => [
          ...prev,
          {
            id: `ai-msg-err-${turn}`,
            role: 'system',
            timestamp: replyTs,
            mode: effectiveMode,
            servedModel: `错误 · ${res.error.code}`,
            isError: true,
            content: `请求 llm.chat.v1 失败 ${formatRpcError(res.error)}`,
          },
        ]);
        appendLog(`${replyTs} · llm.chat.v1 FAIL ${formatRpcError(res.error)}`);
        return;
      }

      const replyText =
        typeof res.data.reply === 'string' && res.data.reply.trim().length > 0
          ? res.data.reply
          : '后端返回空正文';
      const servedModel = String(
        res.data.served_model || res.data.model || res.data.route_group || route.primaryModel,
      );
      setMessages((prev) => [
        ...prev,
        {
          id: `ai-msg-a-${turn}`,
          role: 'assistant',
          timestamp: replyTs,
          mode: effectiveMode,
          servedModel,
          content: replyText,
        },
      ]);
      appendLog(`${replyTs} · llm.chat.v1 [${effectiveMode}] → ${servedModel}`);
    },
    [appendLog, composerText, curationMode, isSending, repeatPolicy, route],
  );

  const updateRemoteRoute = useCallback(
    async (
      nextModel: string,
      nextEffort: MagpieRouteState['effort'],
      nextStrategy: MagpieRouteState['strategy'],
    ) => {
      if (!isTauriAvailable()) {
        setRoute((prev) => ({
          ...prev,
          primaryModel: nextModel,
          effort: nextEffort,
          strategy: nextStrategy,
        }));
        appendLog(`离线预览：本地切换示例路由 → ${nextModel}:${nextEffort}`);
        return;
      }
      const groupId = route.activeGroup.replace(/^group\//, '');
      const res = await callCapability({
        capability: 'llm.router.v1',
        payload: {
          action: 'set_route',
          group_id: groupId,
          model_id: nextModel,
          effort: nextEffort,
          routing: nextStrategy,
          stays: 'turn',
          fallbacks: route.fallbackChain,
        },
      });
      if (!mountedRef.current) return;
      if (!res.available) return;
      if (!res.ok) {
        setBannerError(res.error);
        appendLog(`${nowHHMM()} · set_route FAIL ${formatRpcError(res.error)}`);
        return;
      }
      setBannerError(null);
      setRoute((prev) => ({
        ...prev,
        gatewayHealthy: true,
        primaryModel: nextModel,
        effort: nextEffort,
        strategy: nextStrategy,
      }));
      appendLog(`${nowHHMM()} · llm.router.v1 set_route → ${nextModel}:${nextEffort}`);
    },
    [appendLog, route.activeGroup, route.fallbackChain],
  );

  const handleTogglePinCard = (card: AiStructuredCard) => {
    setPinnedCards((prev) => {
      const exists = prev.some((c) => c.id === card.id);
      appendLog(exists ? `已从反写暂存队列移除：${card.title}` : `已加入反写暂存队列：${card.title}`);
      return exists ? prev.filter((c) => c.id !== card.id) : [card, ...prev];
    });
  };

  const handleRunPreset = (preset: AiPresetItem) => {
    setCurationMode(preset.mode);
    void handleSendPrompt(preset.prompt, preset.mode);
  };

  const handleRehydrate = async () => {
    if (!isTauriAvailable()) {
      setBannerError({
        code: 'tauri_unavailable',
        message: '离线预览模式不可执行双仓同步（未连接 Tauri 运行时）',
      });
      appendLog(`${nowHHMM()} · 离线预览禁止执行双仓回灌`);
      return;
    }
    const res = await callCapability<Record<string, unknown>>({
      capability: 'recommend.feed.v1',
      payload: { action: 'bootstrap_and_rehydrate', pull_remote: true },
    });
    if (!mountedRef.current) return;
    if (!res.available) return;
    if (!res.ok) {
      setBannerError(res.error);
      appendLog(`${nowHHMM()} · bootstrap_and_rehydrate FAIL ${formatRpcError(res.error)}`);
      return;
    }
    setArchiveSyncTime(new Date().toISOString().slice(0, 16).replace('T', ' '));
    setBannerError(null);
    appendLog(`${nowHHMM()} · recommend.feed.v1 双仓回灌完成`);
  };

  return {
    tauriAvailable,
    route,
    curationMode,
    setCurationMode,
    repeatPolicy,
    setRepeatPolicy,
    messages,
    composerText,
    setComposerText,
    pinnedCards,
    setPinnedCards,
    archiveSyncTime,
    isSending,
    bannerError,
    auditLogs,
    appendLog,
    handleSendPrompt,
    updateRemoteRoute,
    handleTogglePinCard,
    handleRunPreset,
    handleRehydrate,
  };
}
