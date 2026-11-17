import { useCallback, useEffect, useRef, useState } from 'react';
import {
  callCapability,
  formatRpcError,
  isTauriAvailable,
  type AiAgentRequestPayload,
  type AiAgentResponseData,
  type RpcErrorInfo,
} from '../api';
import { INITIAL_AI_MESSAGES } from '../data/mock';
import type { ActiveEntityContext, ContentDomain } from '../types';
import {
  buildCompanionDemoReply,
  nowTimeLabel,
  type CompanionMessage,
} from './companionHelpers';

export interface UseCompanionChatOptions {
  open: boolean;
  effectiveDomain: ContentDomain;
  contextTitle: string;
  contextEntity?: ActiveEntityContext | null;
}

export function useCompanionChat({
  open,
  effectiveDomain,
  contextTitle,
  contextEntity,
}: UseCompanionChatOptions) {
  const tauriAvailable = isTauriAvailable();
  const [draft, setDraft] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [dockError, setDockError] = useState<RpcErrorInfo | null>(null);
  const [liveRouteLabel, setLiveRouteLabel] = useState<string | null>(null);
  const [routeOnline, setRouteOnline] = useState<boolean>(false);
  const [messages, setMessages] = useState<CompanionMessage[]>(() => [
    {
      id: 'dock-welcome',
      role: 'assistant',
      timestamp: INITIAL_AI_MESSAGES[1]?.timestamp ?? '22:40',
      servedModel: tauriAvailable
        ? 'group/omnidesk-active · ai.agent.v1'
        : '离线预览示例（未连接 Tauri）',
      isDemo: !tauriAvailable,
      content:
        'F8 无遮罩伴读栏已就绪。支持总结要点、双语精译、词源拆解与长难句解析；输入法选词期间自动屏蔽 Enter 提交。',
    },
  ]);

  const msgCounterRef = useRef(1);
  const mountedRef = useRef(true);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!open || !isTauriAvailable()) {
      return undefined;
    }
    let cancelled = false;
    void callCapability<{
      summary?: string;
      primary_member?: string;
      gateway_running?: boolean;
      online?: boolean;
    }>({
      capability: 'llm.router.v1',
      payload: { action: 'current_route' },
    }).then((res) => {
      if (cancelled || !mountedRef.current) return;
      if (res.ok) {
        const online = Boolean(res.data.gateway_running ?? res.data.online ?? true);
        setRouteOnline(online);
        setLiveRouteLabel(
          String(res.data.primary_member || res.data.summary || 'group/omnidesk-active'),
        );
      } else if (res.available) {
        setRouteOnline(false);
        setLiveRouteLabel(`Magpie 离线 (${res.error.code})`);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const submitPrompt = useCallback(
    async (rawText: string, action: CompanionMessage['action'] = 'chat') => {
      const trimmed = rawText.trim();
      if (!trimmed) {
        return;
      }

      const nextId = msgCounterRef.current++;
      const ts = nowTimeLabel();
      const userMsg: CompanionMessage = {
        id: `dock-user-${nextId}`,
        role: 'user',
        content: trimmed,
        timestamp: ts,
        action,
      };
      const priorHistory = messagesRef.current
        .filter((m) => m.id !== 'dock-welcome' && !m.isError && !m.isDemo)
        .slice(-6)
        .map((m) => ({ role: m.role, content: m.content }));

      setMessages((prev) => [...prev, userMsg]);
      setDraft('');
      setDockError(null);

      if (!isTauriAvailable()) {
        setMessages((prev) => [
          ...prev,
          {
            id: `dock-assistant-${nextId}`,
            role: 'assistant',
            content: buildCompanionDemoReply(trimmed, action, effectiveDomain, contextTitle),
            timestamp: ts,
            action,
            isDemo: true,
            servedModel: '离线预览示例（未连接 Tauri）',
          },
        ]);
        return;
      }

      setIsSending(true);
      const contextLabel = contextEntity
        ? `${contextEntity.title}${contextEntity.subtitle ? ` — ${contextEntity.subtitle}` : ''}`
        : contextTitle;
      const contextRefs = contextEntity
        ? [
            {
              capability:
                effectiveDomain === 'book'
                  ? 'book.source.v1'
                  : effectiveDomain === 'media'
                    ? 'media.source.v1'
                    : 'text.source.v1',
              label: contextEntity.title,
              payload:
                effectiveDomain === 'book'
                  ? {
                      action: 'fetch_spread',
                      book_id: contextEntity.itemId,
                      source_id: contextEntity.sourceId,
                    }
                  : effectiveDomain === 'media'
                    ? {
                        action: 'fetch_chapters',
                        item_id: contextEntity.itemId,
                        source_id: contextEntity.sourceId,
                      }
                    : {
                        action: 'fetch_article',
                        article_id: contextEntity.itemId,
                        source_id: contextEntity.sourceId,
                      },
            },
          ]
        : undefined;
      const payload: AiAgentRequestPayload = {
        action: action ?? 'chat',
        domain: effectiveDomain,
        context: contextLabel,
        context_refs: contextRefs,
        prompt: trimmed,
        messages: priorHistory,
        timeout_ms: 60000,
      };

      const res = await callCapability<AiAgentResponseData>({
        capability: 'ai.agent.v1',
        payload: payload as unknown as Record<string, unknown>,
        timeoutMs: 60000,
      });
      if (!mountedRef.current) return;
      setIsSending(false);

      const replyTs = nowTimeLabel();
      if (!res.available) {
        setMessages((prev) => [
          ...prev,
          {
            id: `dock-assistant-${nextId}`,
            role: 'assistant',
            content: buildCompanionDemoReply(trimmed, action, effectiveDomain, contextTitle),
            timestamp: replyTs,
            action,
            isDemo: true,
            servedModel: '离线预览示例（未连接 Tauri）',
          },
        ]);
        return;
      }

      if (!res.ok) {
        setDockError(res.error);
        setMessages((prev) => [
          ...prev,
          {
            id: `dock-assistant-${nextId}`,
            role: 'assistant',
            content: `调用 ai.agent.v1 失败 ${formatRpcError(res.error)}`,
            timestamp: replyTs,
            action,
            isError: true,
            servedModel: `错误 · ${res.error.code}`,
          },
        ]);
        return;
      }

      const replyText =
        typeof res.data.reply === 'string' && res.data.reply.trim().length > 0
          ? res.data.reply
          : '后端未返回回复正文';
      const served = res.data.route_group
        ? `${res.data.route_group} → ${res.data.served_model || res.data.model || 'active'}`
        : String(res.data.served_model || res.data.model || liveRouteLabel || 'ai.agent.v1');

      setMessages((prev) => [
        ...prev,
        {
          id: `dock-assistant-${nextId}`,
          role: 'assistant',
          content: replyText,
          timestamp: replyTs,
          action,
          servedModel: served,
        },
      ]);
    },
    [contextEntity, contextTitle, effectiveDomain, liveRouteLabel],
  );

  return {
    tauriAvailable,
    draft,
    setDraft,
    isSending,
    dockError,
    liveRouteLabel,
    routeOnline,
    messages,
    submitPrompt,
  };
}
