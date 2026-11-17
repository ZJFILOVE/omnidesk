import { callCapability } from '../api';
import type { AiChatMessage, CurationMode, MagpieRouteState } from '../data/mock';
import type { RepeatPolicy } from './panels/AiChatPane';

export function nowHHMM(): string {
  const now = new Date();
  return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
}

export function parseMemberAndEffort(
  rawMember: string,
): { model: string; effort?: MagpieRouteState['effort'] } {
  const idx = rawMember.lastIndexOf(':');
  if (idx > 0) {
    const model = rawMember.slice(0, idx);
    const suffix = rawMember.slice(idx + 1);
    if (suffix === 'low' || suffix === 'medium' || suffix === 'high' || suffix === 'xhigh') {
      return { model, effort: suffix };
    }
  }
  return { model: rawMember };
}

export function buildOfflineDemoReply(
  promptText: string,
  mode: CurationMode,
  repeatPolicy: RepeatPolicy,
  route: MagpieRouteState,
  turnIndex: number,
): AiChatMessage {
  const shortModel = route.primaryModel.split('/')[1] ?? route.primaryModel;
  const timestamp = nowHHMM();

  return {
    id: `ai-msg-${turnIndex}`,
    role: 'assistant',
    timestamp,
    mode,
    isDemo: true,
    servedModel: `离线预览示例 (${route.activeGroup} · ${shortModel})`,
    content:
      mode === 'daily_immutable'
        ? `[离线预览示例 · 未执行真实管线] 收到指令「${promptText}」。当前未连接 Tauri / Core 运行时，无法读取或写入 daily_immutable 历史账本与 p.YJXV7dEIeb3GzZX。`
        : `[离线预览示例 · ${repeatPolicy}] 收到指令「${promptText}」。当前为浏览器静态示例演示，未连接真实 Magpie 网关（127.0.0.1:3425）。`,
  };
}

export async function probeMagpieRouterState() {
  return Promise.all([
    callCapability<{
      gateway_url?: string;
      gateway_running?: boolean;
      online?: boolean;
      group_id?: string;
      primary_member?: string;
      routing?: string;
      fallbacks?: string[];
    }>({
      capability: 'llm.router.v1',
      payload: { action: 'current_route' },
    }),
    callCapability<{
      models?: Array<{ id?: string; model_id?: string } | string>;
    }>({
      capability: 'llm.router.v1',
      payload: { action: 'list_models' },
    }),
    callCapability<{
      quotas?: Array<{
        provider?: string;
        model?: string;
        used_pct?: number;
        usedPct?: number;
        reset_in?: string;
        resetIn?: string;
      }>;
    }>({
      capability: 'llm.router.v1',
      payload: { action: 'list_quotas' },
    }),
  ]);
}
