import { isEditableTarget } from '../hooks/useImeGuard';
import type { WorkbenchId } from '../types';

export interface AltTabTarget {
  workbench: WorkbenchId;
  focusId?: string;
  label: string;
}

export const ALT_SHORTCUT_MAP: Record<string, AltTabTarget> = {
  m: { workbench: 'media', label: '影视音乐工作台' },
  v: { workbench: 'media', focusId: 'media:filter:video', label: '视频精选' },
  y: { workbench: 'media', focusId: 'media:filter:music', label: 'Apple Music' },
  n: { workbench: 'media', focusId: 'media:filter:nas', label: '飞牛 NAS 原盘' },
  j: { workbench: 'text', focusId: 'text:channel:journal', label: '科学期刊' },
  r: { workbench: 'text', focusId: 'text:channel:reddit', label: 'Reddit 精选' },
  l: { workbench: 'text', focusId: 'text:channel:linuxdo', label: 'Linux.do' },
  z: { workbench: 'text', focusId: 'text:channel:zhihu', label: '知乎 / 博客' },
  b: { workbench: 'book', focusId: 'book:tab:shelf', label: '书本 PDF 书架' },
  s: { workbench: 'book', focusId: 'book:tab:search', label: '搜书目录' },
  a: { workbench: 'ai', label: 'AI 工作台' },
  p: { workbench: 'store', label: '插件中心' },
  c: { workbench: 'store', focusId: 'store:domain:core', label: 'Core 插件' },
};

export function isEditableElement(target: EventTarget | null): boolean {
  if (isEditableTarget(target)) {
    return true;
  }
  return target instanceof HTMLElement && target.closest('[contenteditable="true"]') !== null;
}

export function blurActiveEditable(): boolean {
  if (typeof document === 'undefined') {
    return false;
  }
  const active = document.activeElement;
  if (active instanceof HTMLElement && isEditableElement(active)) {
    active.blur();
    return true;
  }
  return false;
}
