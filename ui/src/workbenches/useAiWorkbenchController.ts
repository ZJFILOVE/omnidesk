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
  isNestedRouteRef,
  nowHHMM,
  parseMemberAndEffort,
  probeMagpieRouterState,
} from './aiWorkbenchHelpers';
import type { RepeatPolicy } from './panels/AiChatPane';

const CURATION_INTENT_RE =
  /推荐|来几首|来点|歌单|最火|热门|榜单|听什么|灵感电台|重温|往期|历史库|找.{0,6}(歌|曲|专辑|论文|书|视频)|recommend|playlist|top\s*songs?/i;

function looksLikeCurationPrompt(text: string): boolean {
  return CURATION_INTENT_RE.test(text);
}

function curationDomains(text: string): string[] | undefined {
  const wantsMedia =
    /歌|曲|音乐|专辑|歌单|听|爵士|器乐|jazz|playlist|song|music/i.test(text) ||
    /视频|讲解|youtube|bilibili|video|podcast/i.test(text);
  const wantsText = /论文|期刊|paper|journal|研究/i.test(text);
  const wantsBook = /书|book|阅读/i.test(text);
  const domains: string[] = [];
  if (wantsMedia) domains.push('media');
  if (wantsText) domains.push('text');
  if (wantsBook) domains.push('book');
  return domains.length > 0 ? domains : undefined;
}

function cardsFromCuration(data: Record<string, unknown>): AiStructuredCard[] {
  const cards: AiStructuredCard[] = [];
  const push = (raw: unknown, domain: AiStructuredCard['domain'], badge: string) => {
    if (!raw || typeof raw !== 'object') return;
    const item = raw as Record<string, unknown>;
    const id = String(item.id ?? '').trim();
    const title = String(item.title ?? '').trim();
    if (!id || !title) return;
    const subtitleParts = [
      item.creator ?? item.author ?? item.source_name,
      item.subtitle ?? item.source_badge,
    ]
      .map((value) => String(value ?? '').trim())
      .filter(Boolean);
    cards.push({
      id: `${domain}:${id}`,
      domain,
      title,
      subtitle: subtitleParts.join(' · ') || '已核验推荐',
      badge,
      verifiedCn: domain === 'media' ? true : undefined,
    });
  };
  for (const item of Array.isArray(data.media_items) ? data.media_items : []) push(item, 'media', '已核验');
  for (const item of Array.isArray(data.text_items) ? data.text_items : []) push(item, 'text', '已核验');
  for (const item of Array.isArray(data.book_items) ? data.book_items : []) push(item, 'book', '已核验');
  return cards;
}

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
      if (effectiveMode === 'interactive_ephemeral' && looksLikeCurationPrompt(text)) {
        const sessionId = `ix-chat-${Date.now()}`;
        const domains = curationDomains(text);
        const curation = await callCapability<Record<string, unknown>>({
          capability: 'ai.curation.v1',
          payload: {
            action: 'curate_interactive',
            mode: 'interactive_ephemeral',
            session_id: sessionId,
            title: text.slice(0, 40),
            prompt: text,
            repeat_policy: repeatPolicy,
            domains,
            sync_apple_music: 'ephemeral_playlist',
            enqueue_now: true,
            replace_session_items: false,
            auto_focus: true,
          },
          timeoutMs: 180000,
        });
        if (!mountedRef.current) return;
        setIsSending(false);
        const replyTs = nowHHMM();
        const cards = curation.available && curation.data ? cardsFromCuration(curation.data) : [];
        const feedWritten = Boolean(curation.data?.feed_written);
        if (curation.available && (curation.ok || feedWritten) && cards.length > 0) {
          const playlistSynced = Boolean(curation.data?.playlist_synced);
          setMessages((prev) => [
            ...prev,
            {
              id: `ai-msg-a-${turn}`,
              role: 'assistant',
              timestamp: replyTs,
              mode: effectiveMode,
              servedModel: 'ai.curation.v1 · 已核验',
              content: `已反写 ${cards.length} 条推荐到卡片页，并写入交互推荐池。灵感歌单${
                playlistSynced ? '已同步' : '未同步（卡片已保留）'
              }。会话：${sessionId}`,
              cards,
            },
          ]);
          appendLog(`${replyTs} · ai.curation.v1 → ${cards.length} 张卡片 · ${sessionId}`);
          return;
        }
        const errorCode = curation.available && !curation.ok ? curation.error.code : 'no_cards';
        setMessages((prev) => [
          ...prev,
          {
            id: `ai-msg-err-${turn}`,
            role: 'system',
            timestamp: replyTs,
            mode: effectiveMode,
            servedModel: `错误 · ${errorCode}`,
            isError: true,
            content:
              errorCode === 'no_verified_candidates'
                ? '没有通过归档溯源与 Apple Music 国区可播性核验的候选，已按规则拒绝生成占位推荐。'
                : errorCode === 'chart_unavailable'
                  ? `实时榜单不可用，已拒绝用旧归档冒充“最火”推荐：${
                      curation.error ? formatRpcError(curation.error) : '未知错误'
                    }`
                  : `策展反写失败：${
                      curation.error ? formatRpcError(curation.error) : '后端不可用或未返回错误详情'
                    }`,
          },
        ]);
        appendLog(`${replyTs} · ai.curation.v1 FAIL ${errorCode}`);
        return;
      }

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
      const nestedRoute = isNestedRouteRef(nextModel);
      const routeEffort = nestedRoute ? undefined : nextEffort;
      if (!isTauriAvailable()) {
        setRoute((prev) => ({
          ...prev,
          primaryModel: nextModel,
          effort: nestedRoute ? prev.effort : nextEffort,
          strategy: nextStrategy,
        }));
        appendLog(
          `离线预览：本地切换示例路由 → ${nextModel}${routeEffort ? `:${routeEffort}` : ''}`,
        );
        return;
      }
      const groupId = route.activeGroup.replace(/^group\//, '');
      const res = await callCapability({
        capability: 'llm.router.v1',
        payload: {
          action: 'set_route',
          group_id: groupId,
          model_id: nextModel,
          effort: routeEffort,
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
        effort: nestedRoute ? prev.effort : nextEffort,
        strategy: nextStrategy,
      }));
      appendLog(
        `${nowHHMM()} · llm.router.v1 set_route → ${nextModel}${routeEffort ? `:${routeEffort}` : ''}`,
      );
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
