import { useAgentSelection } from '../hooks/useAgentSelection';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  callCapability,
  formatRpcError,
  isTauriAvailable,
  type AgentSessionExport,
  type AgentSessionRecord,
  type AgentSessionSummary,
  type AiAgentRequestPayload,
  type AiAgentResponseData,
  type RpcErrorInfo,
} from '../api';
import { INITIAL_AI_MESSAGES } from '../data/mock';
import type { ActiveEntityContext, ContentDomain } from '../types';
import {
  buildCompanionDemoReply,
  nowTimeLabel,
  sessionTimeLabel,
  type CompanionMessage,
} from './companionHelpers';

const LAST_SESSION_KEY = 'omnidesk.companion.lastSessionId';

export interface UseCompanionChatOptions {
  open: boolean;
  effectiveDomain: ContentDomain;
  contextTitle: string;
  contextEntity?: ActiveEntityContext | null;
  restoreLastSessionOnOpen?: boolean;
}

function welcomeMessage(tauriAvailable: boolean): CompanionMessage {
  return {
    id: 'dock-welcome',
    role: 'assistant',
    timestamp: INITIAL_AI_MESSAGES[1]?.timestamp ?? '22:40',
    servedModel: tauriAvailable
      ? 'group/omnidesk-active · ai.agent.v1'
      : '离线预览示例（未连接 Tauri）',
    isDemo: !tauriAvailable,
    content:
      'F8 无遮罩伴读栏已就绪。支持总结要点、双语精译、词源拆解与长难句解析；输入法选词期间自动屏蔽 Enter 提交。',
  };
}

function recordToMessages(session: AgentSessionRecord, tauriAvailable: boolean): CompanionMessage[] {
  const restored = (session.messages ?? [])
    .filter((message) => message.role === 'user' || message.role === 'assistant')
    .map<CompanionMessage>((message) => ({
      id: message.id,
      role: message.role === 'user' ? 'user' : 'assistant',
      content: message.content,
      timestamp: sessionTimeLabel(message.created_at),
      action: message.action as CompanionMessage['action'],
      servedModel: message.is_error
        ? `错误 · ${message.error_code || 'agent_error'}`
        : message.served_model || message.route_group || undefined,
      isError: message.is_error,
    }));
  return restored.length > 0 ? restored : [welcomeMessage(tauriAvailable)];
}

function readLastSessionId(): string | null {
  try {
    return window.localStorage.getItem(LAST_SESSION_KEY);
  } catch {
    return null;
  }
}

function writeLastSessionId(sessionId: string | null): void {
  try {
    if (sessionId) {
      window.localStorage.setItem(LAST_SESSION_KEY, sessionId);
    } else {
      window.localStorage.removeItem(LAST_SESSION_KEY);
    }
  } catch {
    return;
  }
}

function downloadExport(exported: AgentSessionExport): boolean {
  if (typeof document === 'undefined') {
    return false;
  }
  const blob = new Blob([exported.content], { type: exported.mime_type });
  const objectUrl =
    typeof URL.createObjectURL === 'function' ? URL.createObjectURL(blob) : '';
  const dataUrl = `data:${exported.mime_type},${encodeURIComponent(exported.content)}`;
  const link = document.createElement('a');
  link.href = objectUrl || dataUrl;
  link.download = exported.filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  try {
    link.click();
    return true;
  } catch {
    return false;
  } finally {
    link.remove();
    if (objectUrl && typeof URL.revokeObjectURL === 'function') {
      URL.revokeObjectURL(objectUrl);
    }
  }
}

export function useCompanionChat({
  open,
  effectiveDomain,
  contextTitle,
  contextEntity,
  restoreLastSessionOnOpen = true,
}: UseCompanionChatOptions) {
  const agentSelection = useAgentSelection();
  const tauriAvailable = isTauriAvailable();
  const [draft, setDraft] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [dockError, setDockError] = useState<RpcErrorInfo | null>(null);
  const [liveRouteLabel, setLiveRouteLabel] = useState<string | null>(null);
  const [routeOnline, setRouteOnline] = useState<boolean>(false);
  const [messages, setMessages] = useState<CompanionMessage[]>(() => [welcomeMessage(tauriAvailable)]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [activeSession, setActiveSession] = useState<AgentSessionSummary | null>(null);
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionError, setSessionError] = useState<RpcErrorInfo | null>(null);
  const [sessionNotice, setSessionNotice] = useState<string | null>(null);
  const [sessionBusyId, setSessionBusyId] = useState<string | null>(null);

  const msgCounterRef = useRef(1);
  const mountedRef = useRef(true);
  const restoreAttemptedRef = useRef(false);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const sessionIdRef = useRef<string | null>(sessionId);
  sessionIdRef.current = sessionId;

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

  const refreshSessions = useCallback(async () => {
    if (!isTauriAvailable()) {
      setSessions([]);
      return;
    }
    setSessionsLoading(true);
    const res = await callCapability<AiAgentResponseData>({
      capability: 'ai.agent.v1',
      payload: { session_action: 'list', limit: 50 },
    });
    if (!mountedRef.current) return;
    setSessionsLoading(false);
    if (res.available && res.ok && Array.isArray(res.data.sessions)) {
      setSessions(res.data.sessions);
      setSessionError(null);
      return;
    }
    if (res.available && !res.ok) {
      setSessionError(res.error);
    }
  }, []);

  const openSession = useCallback(async (id: string) => {
    if (!isTauriAvailable()) {
      return false;
    }
    setSessionBusyId(id);
    setSessionError(null);
    setSessionNotice(null);
    const res = await callCapability<AiAgentResponseData>({
      capability: 'ai.agent.v1',
      payload: { session_action: 'get', session_id: id },
    });
    if (!mountedRef.current) return false;
    setSessionBusyId(null);
    if (!res.available || !res.ok) {
      if (res.available) {
        setSessionError(res.error);
      }
      return false;
    }
    const record = res.data.session as AgentSessionRecord | undefined;
    if (!record || !Array.isArray(record.messages)) {
      setSessionError({
        code: 'invalid_session_record',
        message: '后端未返回可恢复的会话消息',
      });
      return false;
    }
    setMessages(recordToMessages(record, true));
    setSessionId(record.id);
    setActiveSession(record);
    writeLastSessionId(record.id);
    setDockError(null);
    return true;
  }, []);

  const startNewSession = useCallback(() => {
    setMessages([welcomeMessage(isTauriAvailable())]);
    setSessionId(null);
    setActiveSession(null);
    setSessionNotice('已开始新会话');
    setSessionError(null);
    setDockError(null);
    writeLastSessionId(null);
  }, []);

  const deleteSession = useCallback(
    async (id: string) => {
      if (!isTauriAvailable()) {
        return false;
      }
      setSessionBusyId(id);
      setSessionError(null);
      const res = await callCapability<AiAgentResponseData>({
        capability: 'ai.agent.v1',
        payload: { session_action: 'delete', session_id: id },
      });
      if (!mountedRef.current) return false;
      setSessionBusyId(null);
      if (!res.available || !res.ok) {
        if (res.available) {
          setSessionError(res.error);
        }
        return false;
      }
      if (sessionIdRef.current === id) {
        setMessages([welcomeMessage(true)]);
        setSessionId(null);
        setActiveSession(null);
        writeLastSessionId(null);
      }
      setSessionNotice('会话已删除');
      await refreshSessions();
      return true;
    },
    [refreshSessions],
  );

  const exportSession = useCallback(async (id: string, format: 'markdown' | 'json') => {
    if (!isTauriAvailable()) {
      return false;
    }
    setSessionBusyId(id);
    setSessionError(null);
    const res = await callCapability<AiAgentResponseData>({
      capability: 'ai.agent.v1',
      payload: { session_action: 'export', session_id: id, session_format: format },
    });
    if (!mountedRef.current) return false;
    setSessionBusyId(null);
    if (!res.available || !res.ok) {
      if (res.available) {
        setSessionError(res.error);
      }
      return false;
    }
    const exported =
      res.data.filename && res.data.content && res.data.mime_type && res.data.format
        ? (res.data as AgentSessionExport)
        : null;
    if (!exported || !downloadExport(exported)) {
      setSessionError({
        code: 'export_failed',
        message: '会话已导出但浏览器未能启动下载',
      });
      return false;
    }
    setSessionNotice(`已导出 ${exported.filename}`);
    return true;
  }, []);

  useEffect(() => {
    if (
      !open ||
      !tauriAvailable ||
      !restoreLastSessionOnOpen ||
      restoreAttemptedRef.current
    ) {
      return;
    }
    restoreAttemptedRef.current = true;
    const savedId = readLastSessionId();
    if (!savedId) {
      void refreshSessions();
      return;
    }
    void openSession(savedId).then((restored) => {
      if (!restored) {
        writeLastSessionId(null);
      }
      void refreshSessions();
    });
  }, [open, openSession, refreshSessions, restoreLastSessionOnOpen, tauriAvailable]);

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
      const contextRefs = contextEntity?.sourceId && ['book', 'media', 'text'].includes(effectiveDomain)
        ? [
            {
              capability:
                effectiveDomain === 'book'
                  ? 'book.source.v1'
                  : effectiveDomain === 'media'
                    ? (typeof contextEntity.extra?.contextCapability === 'string' ? contextEntity.extra.contextCapability : 'media.source.v1')
                    : 'text.source.v1',
              label: contextEntity.title,
              provider_plugin_id: typeof contextEntity.extra?.contextProvider === 'string' ? contextEntity.extra.contextProvider : contextEntity.sourceId.startsWith('plugin-') ? contextEntity.sourceId : undefined,
              payload:
                effectiveDomain === 'book'
                  ? {
                      action: 'fetch_spread',
                      book_id: contextEntity.itemId,
                      page_number: contextEntity.extra?.pageNumber ?? 1,
                      source_id: contextEntity.sourceId,
                    }
                  : effectiveDomain === 'media'
                    ? {
                        action: contextEntity.extra?.contextCapability === 'media.youtube.v1' || contextEntity.extra?.contextCapability === 'media.bilibili.v1' ? 'fetch_subtitles' : contextEntity.kind === 'music' ? 'fetch_lyrics' : 'fetch_chapters',
                        bilingual: false,
                        item_id: contextEntity.extra?.contextItemId ?? contextEntity.itemId,
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
      if (effectiveDomain === 'text' && contextRefs && contextEntity?.extra?.commentsProvider) {
        contextRefs.push({ capability: 'text.comments.v1', label: `${contextEntity.title} · 评论区`,
          provider_plugin_id: String(contextEntity.extra.commentsProvider),
          payload: { action: 'fetch_comments', article_id: contextEntity.itemId, source_id: contextEntity.sourceId ?? '' } });
      }
      const payload: AiAgentRequestPayload = {
        agent_id: agentSelection.agent.id,
        action: action ?? 'chat',
        domain: effectiveDomain,
        context: contextLabel,
        context_title: contextLabel,
        context_refs: contextRefs,
        session_id: sessionIdRef.current ?? undefined,
        prompt: trimmed,
        messages: priorHistory,
        timeout_ms: 60000,
      };

      const res = await callCapability<AiAgentResponseData>({
        capability: 'ai.agent.v1',
        providerPluginId: agentSelection.agent.provider,
        payload: payload as unknown as Record<string, unknown>,
        timeoutMs: 300000,
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

      const responseData = res.data ?? {};
      const responseSessionId =
        typeof responseData.session_id === 'string' ? responseData.session_id : '';
      if (responseSessionId) {
        setSessionId(responseSessionId);
        writeLastSessionId(responseSessionId);
        if (responseData.session) {
          setActiveSession(responseData.session as AgentSessionSummary);
        }
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
        if (responseData.persisted) {
          void refreshSessions();
        }
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
      if (responseData.persisted) {
        void refreshSessions();
      }
    },
    [agentSelection.agent, contextEntity, contextTitle, effectiveDomain, liveRouteLabel, refreshSessions],
  );

  const resetConversation = useCallback(() => {
    setDraft('');
    setIsSending(false);
    startNewSession();
  }, [startNewSession]);

  return {
    agentSelection,
    tauriAvailable,
    draft,
    setDraft,
    isSending,
    dockError,
    liveRouteLabel,
    routeOnline,
    messages,
    sessionId,
    activeSession,
    sessions,
    sessionsLoading,
    sessionError,
    sessionNotice,
    sessionBusyId,
    refreshSessions,
    openSession,
    startNewSession,
    deleteSession,
    exportSession,
    submitPrompt,
    resetConversation,
  };
}
