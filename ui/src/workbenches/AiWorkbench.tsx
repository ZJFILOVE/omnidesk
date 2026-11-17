import React, { useEffect, useState } from 'react';
import { SlidersHorizontal } from 'lucide-react';
import { StatusBanner } from '../components/StatusBanner';
import WorkbenchLayout, {
  FocusButton,
  HAIRLINE_BORDER,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import { SegmentedPills, TagChip } from '../components/WorkbenchPrimitives';
import AiChatPane from './panels/AiChatPane';
import AiLedgerPanel from './panels/AiLedgerPanel';
import AiRouteSidebar from './panels/AiRouteSidebar';
import { useAiWorkbenchController } from './useAiWorkbenchController';

type AiDrawerTab = 'route' | 'ledger';

export const AiWorkbench: React.FC<WorkbenchViewProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  lastBusEvent,
  onOpenCuration,
  onContextChange,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    'ai:composer:input',
    focusId,
    onFocus,
    focusedItemId,
  );
  const [drawerOpen, setDrawerOpen] = useState<boolean>(true);
  const [drawerTab, setDrawerTab] = useState<AiDrawerTab>('route');
  const ctrl = useAiWorkbenchController(lastBusEvent);
  const { route, curationMode } = ctrl;

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

  return (
    <WorkbenchLayout
      domain="core"
      testId="workbench-ai"
      title="AI 策展工作室"
      subtitle="对话主舞台 · Magpie 路由调度 · 双模反写隔离"
      rightWidthClass="@[860px]/workbench:w-[320px]"
      headerActions={
        <div className="flex flex-wrap items-center gap-2">
          <span
            data-testid="ai-gateway-badge"
            className="rounded-full px-2.5 py-1 font-mono text-[13.5px] font-medium"
            style={{
              backgroundColor: !ctrl.tauriAvailable
                ? 'var(--status-warn-bg)'
                : route.gatewayHealthy
                  ? 'var(--status-ok-bg)'
                  : 'var(--status-err-bg)',
              color: !ctrl.tauriAvailable
                ? 'var(--status-warn-fg)'
                : route.gatewayHealthy
                  ? 'var(--status-ok-fg)'
                  : 'var(--status-err-fg)',
            }}
          >
            {!ctrl.tauriAvailable
              ? '离线预览 · 示例路由'
              : `Magpie ${route.gatewayHealthy ? 'ONLINE' : 'OFFLINE'} · ${route.activeGroup}`}
          </span>
          <TagChip tone="neutral" mono>
            {route.primaryModel} ({route.effort})
          </TagChip>
          <FocusButton
            focusId="ai:drawer:toggle"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setDrawerOpen((v) => !v)}
            selected={drawerOpen}
            tone="core"
            surface="soft"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[14px] font-medium"
          >
            <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{drawerOpen ? '收起路由与账本' : '路由与账本'}</span>
          </FocusButton>
        </div>
      }
      toolbar={
        !ctrl.tauriAvailable ? (
          <div className="w-full">
            <StatusBanner
              mode="offline"
              compact
              testId="ai-offline-banner"
              message="离线预览：数据为示例（发送消息仅作本地示例演示，未连接 Magpie / Core）"
            />
          </div>
        ) : ctrl.bannerError ? (
          <div className="w-full">
            <StatusBanner mode="error" compact testId="ai-error-banner" error={ctrl.bannerError} />
          </div>
        ) : undefined
      }
      mainPane={
        <AiChatPane
          curationMode={curationMode}
          repeatPolicy={ctrl.repeatPolicy}
          messages={ctrl.messages}
          pinnedCards={ctrl.pinnedCards}
          composerText={ctrl.composerText}
          isSending={ctrl.isSending}
          activeFocusId={activeFocusId}
          onFocus={emitFocus}
          onModeChange={ctrl.setCurationMode}
          onRepeatPolicyChange={ctrl.setRepeatPolicy}
          onComposerChange={ctrl.setComposerText}
          onSend={() => void ctrl.handleSendPrompt()}
          onRunPreset={ctrl.handleRunPreset}
          onTogglePinCard={ctrl.handleTogglePinCard}
        />
      }
      rightPane={
        drawerOpen ? (
          <div className="flex flex-col">
            <div
              className="flex items-center justify-between border-b px-4 py-3"
              style={{ borderColor: HAIRLINE_BORDER }}
            >
              <SegmentedPills
                options={[
                  { id: 'route', label: '路由与额度' },
                  { id: 'ledger', label: '反写账本', count: ctrl.pinnedCards.length },
                ]}
                value={drawerTab}
                onChange={setDrawerTab}
                focusPrefix="ai:drawer-tab:"
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                tone="core"
                ariaLabel="AI 次级侧栏切换"
              />
            </div>

            {drawerTab === 'route' ? (
              <AiRouteSidebar
                route={route}
                archiveSyncTime={ctrl.archiveSyncTime}
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                onSelectModel={(model) =>
                  void ctrl.updateRemoteRoute(model, route.effort, route.strategy)
                }
                onSelectEffort={(effort) =>
                  void ctrl.updateRemoteRoute(route.primaryModel, effort, route.strategy)
                }
                onSelectStrategy={(strategy) =>
                  void ctrl.updateRemoteRoute(route.primaryModel, route.effort, strategy)
                }
                onRehydrate={() => void ctrl.handleRehydrate()}
                onOpenCuration={onOpenCuration}
              />
            ) : (
              <AiLedgerPanel
                curationMode={curationMode}
                pinnedCards={ctrl.pinnedCards}
                auditLogs={ctrl.auditLogs}
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                onTogglePinCard={ctrl.handleTogglePinCard}
                onClearPinned={() => {
                  ctrl.setPinnedCards([]);
                  ctrl.appendLog('已清空待反写暂存队列');
                }}
              />
            )}
          </div>
        ) : undefined
      }
    />
  );
};

export default AiWorkbench;
