import { Keyboard } from 'lucide-react';
import type { EventBridgeSnapshot } from '../api';
import { DOMAIN_CSS_VARS } from '../theme';
import type { ActiveEntityContext, WorkbenchMeta } from '../types';
import { Keycap } from './Keycap';

export interface AppShellFooterProps {
  activeMeta: WorkbenchMeta;
  activeAccent: string;
  contextEntity: ActiveEntityContext | null;
  focusedId: string | null;
  isComposing: boolean;
  inTauri: boolean;
  coreOnline: boolean;
  bridgeStatus: EventBridgeSnapshot | null;
}

export function AppShellFooter({
  activeMeta,
  activeAccent,
  contextEntity,
  focusedId,
  isComposing,
  inTauri,
  coreOnline,
  bridgeStatus,
}: AppShellFooterProps) {
  return (
    <footer
      className="h-8 shrink-0 border-t px-4 flex items-center justify-between gap-3 text-[13.5px] text-muted select-none"
      style={{
        backgroundColor: 'var(--bg-sunken)',
        borderColor: 'var(--border-subtle)',
      }}
    >
      <div className="flex items-center gap-2.5 min-w-0">
        <span className="flex items-center gap-2 min-w-0">
          <span
            className="h-2 w-2 rounded-full shrink-0"
            style={{
              backgroundColor: contextEntity
                ? DOMAIN_CSS_VARS[contextEntity.domain]
                : activeAccent,
            }}
            aria-hidden="true"
          />
          <span className="truncate font-medium text-ink">
            {contextEntity ? contextEntity.title : activeMeta.label}
          </span>
        </span>

        <span className="hidden sm:inline text-subtle" aria-hidden="true">
          ·
        </span>

        <span className="hidden sm:inline-flex items-center gap-1 font-mono text-[13.5px] text-subtle truncate max-w-[240px]">
          <span>焦点:</span>
          <span className="text-muted truncate">{focusedId ?? 'none'}</span>
        </span>

        <span
          data-testid="shell-runtime-status"
          className="hidden md:inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[13.5px] font-medium"
          style={{
            backgroundColor: !inTauri
              ? 'var(--status-warn-bg)'
              : coreOnline
                ? 'var(--status-ok-bg)'
                : 'var(--status-err-bg)',
            color: !inTauri
              ? 'var(--status-warn-fg)'
              : coreOnline
                ? 'var(--status-ok-fg)'
                : 'var(--status-err-fg)',
          }}
        >
          {!inTauri
            ? '离线预览'
            : `Core ${coreOnline ? '在线' : '离线'}${bridgeStatus ? ` · Bus:${bridgeStatus.status}` : ''}`}
        </span>

        <span
          className="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[13.5px] font-medium shrink-0"
          style={
            isComposing
              ? {
                  backgroundColor: 'var(--status-warn-bg)',
                  color: 'var(--status-warn-fg)',
                }
              : {
                  backgroundColor: 'var(--bg-soft)',
                  color: 'var(--text-muted)',
                }
          }
        >
          <Keyboard className="h-3 w-3" aria-hidden="true" />
          <span>{isComposing ? 'IME 选词避让中' : 'IME 就绪'}</span>
        </span>
      </div>

      <div className="hidden lg:flex items-center gap-3 shrink-0 text-[13.5px] text-muted">
        <span className="inline-flex items-center gap-1">
          <Keycap size="xs">hjkl</Keycap>
          <span>移动</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <Keycap size="xs">Enter</Keycap>
          <span>打开</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <Keycap size="xs">f</Keycap>
          <span>闪跳</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <Keycap size="xs">/</Keycap>
          <span>搜索</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <Keycap size="xs">?</Keycap>
          <span>帮助</span>
        </span>
      </div>
    </footer>
  );
}
