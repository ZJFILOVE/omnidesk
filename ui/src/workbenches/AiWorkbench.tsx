import React, { useEffect, useState } from 'react';
import WorkbenchLayout, {
  HAIRLINE_BORDER,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
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
import AiChatPane, { type RepeatPolicy } from './panels/AiChatPane';
import AiLedgerPanel from './panels/AiLedgerPanel';
import AiRouteSidebar from './panels/AiRouteSidebar';

function buildAssistantReply(
  promptText: string,
  mode: CurationMode,
  repeatPolicy: RepeatPolicy,
  route: MagpieRouteState,
  turnIndex: number,
): AiChatMessage {
  const shortModel = route.primaryModel.split('/')[1] ?? route.primaryModel;
  const now = new Date();
  const timestamp = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

  if (mode === 'daily_immutable') {
    return {
      id: `ai-msg-${turnIndex}`,
      role: 'assistant',
      timestamp,
      mode,
      servedModel: `${route.activeGroup} → ${shortModel}:${route.effort}`,
      content: `已按 daily_immutable 协议校验永久历史账本（since ${AI_ARCHIVE_SNAPSHOT.sinceDate}）并锁定两阶段事务包。候选条目全部通过未读中止守卫与信源核验，准备同步至每日账本与 p.YJXV7dEIeb3GzZX。`,
      cards: [
        {
          id: `rec-daily-${turnIndex}-1`,
          domain: 'text',
          title: `Daily Digest · ${promptText.slice(0, 26)}`,
          subtitle: '已通过永久账本去重 · Two-Phase PREPARED 事务已就绪',
          badge: '每日精选 · 2026-11-16',
          verifiedCn: true,
        },
        {
          id: `rec-daily-${turnIndex}-2`,
          domain: 'media',
          title: 'Blue in Green (Take 3) — Miles Davis & Bill Evans',
          subtitle: 'Apple Music 国区 verified · 满足艺人冷却期约束',
          badge: '每日精选 · 2026-11-16',
          verifiedCn: true,
        },
      ],
    };
  }

  return {
    id: `ai-msg-${turnIndex}`,
    role: 'assistant',
    timestamp,
    mode,
    servedModel: `${route.activeGroup} → ${shortModel}:${route.effort}`,
    content: `已按 interactive_ephemeral（${repeatPolicy}）检索本地双仓回灌索引并执行即时编排。record_permanent_history=false，绝不修改 obsidian-vault 账本或 p.YJXV7dEIeb3GzZX。`,
    cards: [
      {
        id: `rec-eph-${turnIndex}-1`,
        domain: 'media',
        title: 'Brad Mehldau Trio — Live at Village Vanguard',
        subtitle: `命中需求「${promptText.slice(0, 22)}」· 可写至 OmniDesk AI 灵感电台`,
        badge: 'AI 灵感推荐 · 经典重温',
        previouslyRecommendedOn: '2026-10-12',
        verifiedCn: true,
      },
      {
        id: `rec-eph-${turnIndex}-2`,
        domain: 'book',
        title: 'Entropy-Guided KV Cache Eviction — 审稿级精读回溯',
        subtitle: '已挂载相关术语至 BookWorkbench SM-2 闪卡列表',
        badge: 'AI 灵感推荐 · 经典重温',
        previouslyRecommendedOn: '2026-11-16',
      },
    ],
  };
}

export const AiWorkbench: React.FC<WorkbenchViewProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  onContextChange,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    'ai:composer:input',
    focusId,
    onFocus,
    focusedItemId,
  );
  const [route, setRoute] = useState<MagpieRouteState>(INITIAL_MAGPIE_ROUTE);
  const [curationMode, setCurationMode] = useState<CurationMode>('interactive_ephemeral');
  const [repeatPolicy, setRepeatPolicy] = useState<RepeatPolicy>('prefer_familiar');
  const [messages, setMessages] = useState<AiChatMessage[]>(INITIAL_AI_MESSAGES);
  const [composerText, setComposerText] = useState<string>('');
  const [pinnedCards, setPinnedCards] = useState<AiStructuredCard[]>(
    INITIAL_AI_MESSAGES[1]?.cards ?? [],
  );
  const [archiveSyncTime, setArchiveSyncTime] = useState<string>(AI_ARCHIVE_SNAPSHOT.lastSyncAt);
  const [auditLogs, setAuditLogs] = useState<string[]>([
    '22:40 · recommend.feed.v1 [interactive_ephemeral] → 命中 2 条经典重温',
    '22:35 · archive.rehydrate [ff-only] → obsidian-vault & music-daily 干净同步',
  ]);

  useEffect(() => {
    onContextChange?.({
      domain: 'core',
      workbench: 'ai',
      itemId: route.activeGroup,
      title: `Magpie · ${route.primaryModel}`,
      subtitle: `${curationMode} · ${route.effort}`,
      sourceId: 'plugin-magpie-agent',
    });
  }, [curationMode, onContextChange, route.activeGroup, route.effort, route.primaryModel]);

  const appendLog = (entry: string) => setAuditLogs((prev) => [entry, ...prev.slice(0, 5)]);

  const handleSendPrompt = (overridePrompt?: string, overrideMode?: CurationMode) => {
    const text = (overridePrompt ?? composerText).trim();
    if (!text) return;
    const effectiveMode = overrideMode ?? curationMode;
    const now = new Date();
    const ts = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
    const turn = messages.length + 1;
    const userMsg: AiChatMessage = {
      id: `ai-msg-u-${turn}`,
      role: 'user',
      timestamp: ts,
      mode: effectiveMode,
      content: text,
    };
    const replyMsg = buildAssistantReply(text, effectiveMode, repeatPolicy, route, turn + 1);
    setMessages((prev) => [...prev, userMsg, replyMsg]);
    if (!overridePrompt) setComposerText('');
    if (replyMsg.cards) {
      setPinnedCards((prev) => {
        const next = [...prev];
        for (const card of replyMsg.cards ?? []) {
          if (!next.some((c) => c.id === card.id)) next.unshift(card);
        }
        return next.slice(0, 6);
      });
    }
    appendLog(`${ts} · ai.curation.v1 [${effectiveMode}] via ${route.primaryModel}`);
  };

  const handleTogglePinCard = (card: AiStructuredCard) => {
    setPinnedCards((prev) => {
      const exists = prev.some((c) => c.id === card.id);
      appendLog(exists ? `已从反写暂存队列移除：${card.title}` : `已加入反写暂存队列：${card.title}`);
      return exists ? prev.filter((c) => c.id !== card.id) : [card, ...prev];
    });
  };

  const handleRunPreset = (preset: AiPresetItem) => {
    setCurationMode(preset.mode);
    handleSendPrompt(preset.prompt, preset.mode);
  };

  const handleRehydrate = () => {
    const now = new Date();
    const stamp = `2026-11-16 ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
    setArchiveSyncTime(stamp);
    appendLog(`${stamp.slice(11)} · 双仓 ff-only 快进回灌完成（耗时 24ms）`);
  };

  return (
    <WorkbenchLayout
      domain="core"
      title="Magpie 路由与双模 AI 策展工作台 · AiWorkbench"
      subtitle="127.0.0.1:3425 专属路由组 · 双仓全量历史回灌 · daily_immutable / interactive_ephemeral 硬隔离"
      badge="AI · Ctrl+4"
      headerActions={
        <div className="flex flex-wrap items-center gap-2">
          <span
            className="rounded border px-2 py-0.5 font-mono text-[11px]"
            style={{
              backgroundColor: 'var(--status-ok-bg, #E7F5ED)',
              color: 'var(--status-ok-fg, #1E7244)',
              borderColor: HAIRLINE_BORDER,
            }}
          >
            Magpie {route.gatewayHealthy ? 'ONLINE' : 'OFFLINE'} · {route.activeGroup}
          </span>
          <span
            className="rounded border px-2 py-0.5 font-mono text-[11px]"
            style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
          >
            {route.primaryModel} ({route.effort})
          </span>
        </div>
      }
      leftPane={
        <AiRouteSidebar
          route={route}
          archiveSyncTime={archiveSyncTime}
          activeFocusId={activeFocusId}
          onFocus={emitFocus}
          onSelectModel={(model) => {
            setRoute((prev) => ({ ...prev, primaryModel: model }));
            appendLog(`路由主模型切换 → ${model}`);
          }}
          onSelectEffort={(effort) => setRoute((prev) => ({ ...prev, effort }))}
          onSelectStrategy={(strategy) => setRoute((prev) => ({ ...prev, strategy }))}
          onRehydrate={handleRehydrate}
        />
      }
      mainPane={
        <AiChatPane
          curationMode={curationMode}
          repeatPolicy={repeatPolicy}
          messages={messages}
          pinnedCards={pinnedCards}
          composerText={composerText}
          activeFocusId={activeFocusId}
          onFocus={emitFocus}
          onModeChange={setCurationMode}
          onRepeatPolicyChange={setRepeatPolicy}
          onComposerChange={setComposerText}
          onSend={() => handleSendPrompt()}
          onRunPreset={handleRunPreset}
          onTogglePinCard={handleTogglePinCard}
        />
      }
      rightPane={
        <AiLedgerPanel
          curationMode={curationMode}
          pinnedCards={pinnedCards}
          auditLogs={auditLogs}
          activeFocusId={activeFocusId}
          onFocus={emitFocus}
          onTogglePinCard={handleTogglePinCard}
          onClearPinned={() => {
            setPinnedCards([]);
            appendLog('已清空待反写暂存队列');
          }}
        />
      }
      footer={
        <>
          <span>焦点锚点: {activeFocusId || '未锁定'}</span>
          <span>control.sock · ai.curation.v1 · recommend.feed.v1</span>
        </>
      }
    />
  );
};

export default AiWorkbench;
