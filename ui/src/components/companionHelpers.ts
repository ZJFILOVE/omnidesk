import { DOMAIN_LABELS } from '../theme';
import type { ContentDomain } from '../types';

export interface CompanionMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  action?: 'chat' | 'summarize' | 'translate' | 'etymology' | 'clause_parse';
  servedModel?: string;
  isDemo?: boolean;
  isError?: boolean;
}

export interface PresetAction {
  id: 'summarize' | 'translate' | 'etymology' | 'clause_parse';
  label: string;
  promptPrefix: string;
}

export const PRESET_ACTIONS: readonly PresetAction[] = [
  { id: 'summarize', label: '总结要点', promptPrefix: '总结当前内容核心要点' },
  { id: 'translate', label: '双语精译', promptPrefix: '给出自然流畅的双语对照译文' },
  { id: 'etymology', label: '词源拆解', promptPrefix: '拆解关键术语词源与语境用法' },
  { id: 'clause_parse', label: '长难句解析', promptPrefix: '拆分句法结构并标注从句层次' },
];

export function nowTimeLabel(): string {
  const d = new Date();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

export function buildCompanionDemoReply(
  prompt: string,
  action: CompanionMessage['action'],
  domain: ContentDomain,
  contextTitle: string,
): string {
  const scopeLabel = `${DOMAIN_LABELS[domain]} · ${contextTitle}`;
  switch (action) {
    case 'summarize':
      return `[离线预览示例 · ${scopeLabel}] 核心提要：已提取当前焦点段落的关键论证与边界条件，可在不遮挡主舞台的前提下对照原文继续阅读。`;
    case 'translate':
      return `[离线预览示例 · ${scopeLabel}] 双语精译：已保留专有名词与数学/代码符号，并将长从句切分为符合中文学术阅读习惯的并列短句。`;
    case 'etymology':
      return `[离线预览示例 · ${scopeLabel}] 词源拆解：已按词根 + 词缀拆解高频术语并关联本地生词本（SM-2 复习队列与 NAS 台词索引）。`;
    case 'clause_parse':
      return `[离线预览示例 · ${scopeLabel}] 句法树：主句主干已标出，定语从句与分词状语已按意群分层，便于逐层精读。`;
    default:
      return `[离线预览示例 · ${scopeLabel}] 已收到「${prompt}」。当前为浏览器离线示例演示，未连接真实 Magpie / Core 后端。`;
  }
}
